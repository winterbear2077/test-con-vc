"""API routes: /api/run and /api/run/{run_id}/*"""
from __future__ import annotations

import asyncio
import json
import logging
import queue as _queue
import threading
import traceback
from datetime import datetime, timezone
from typing import Any, List
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import nettest.db as _db
from nettest.input_handlers import validate_and_normalize
from nettest.runner import RunConfig, execute_run
from nettest.models import TestCase
from nettest.policy import assign_case_ids, generate_test_cases
from nettest.vcenter_utils import connect_vcenter_auto, disconnect_vcenter, get_all_vms_by_moid, delete_vm
from nettest.api.deps import WORKSPACE, ARTIFACTS, _runs, _read_config, get_session_password

router = APIRouter()


def _header_host(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "//" + s
    try:
        return str(urlparse(s).hostname or "").strip()
    except Exception:
        return ""


def _find_datacenter(content: Any, vim: Any, datacenter_name: str):
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datacenter], True)
    try:
        target = datacenter_name.strip().upper()
        for dc in view.view:
            if str(dc.name).strip().upper() == target:
                return dc
    finally:
        view.Destroy()
    return None


# ── Run model ─────────────────────────────────────────────────────────────────
class RunIn(BaseModel):
    execute_vcenter: bool = False
    probe_mode: str = "dry-run"
    max_retries: int = 0
    cleanup_on_failure: bool = False
    phased_testing: bool = False
    vms_per_subnet: int = 1
    max_vms_per_phase: int = 20
    phases: str = ""           # comma-separated phase IDs, empty = all
    testsuite: str = ""        # testsuite name; empty = default full run
    testcase_ids: List[str] = []
    vrf_links: List[str] = []  # kept for backward compatibility
    # Probe communication channel (guestinfo | serial | vsock)
    poll_method: str = "guestinfo"
    serial_probe_host: str = ""
    serial_base_port: int = 10000
    vsock_base_port: int = 9000
    # VM boot method (ovf | memboot)
    boot_method: str = "memboot"
    memboot_iso_path: str = ""


class CustomStepRunIn(BaseModel):
    rules: List[dict] = []
    execute_vcenter: bool = False
    probe_mode: str = "controller-gateway"


def _build_custom_cases_from_rules(rules: List[dict]) -> tuple[list[TestCase], dict[str, str], set[str]]:
    subnet_to_gw = {
        str(row.get("subnet", "")).strip(): str(row.get("gw", "")).strip()
        for row in _db.get_networks()
    }

    custom_cases: list[TestCase] = []
    custom_gateway_by_subnet: dict[str, str] = {}
    udp_phase_ids: set[str] = set()

    for idx, rule in enumerate(rules):
        src_subnet = str(rule.get("src_subnet", rule.get("srcSubnet", ""))).strip()
        dest = str(rule.get("dest", "")).strip()
        protocol = str(rule.get("protocol", "tcp")).strip().lower() or "tcp"
        try:
            port = int(rule.get("port", 80) or 80)
        except Exception:
            port = 80

        if not src_subnet or not dest:
            continue
        if protocol not in ("tcp", "udp", "icmp"):
            continue

        key_raw = f"{src_subnet}|{protocol}|{dest}|{port}"
        key = "".join(ch if ch.isalnum() else "-" for ch in key_raw).strip("-") or f"rule-{idx+1}"

        dest_ip = subnet_to_gw.get(dest, dest)
        custom_gateway_by_subnet[dest] = dest_ip

        step1_phase = f"custom-{idx}-step1"
        step2_phase = f"custom-{idx}-step2"
        case_base = f"custom-rule-{key}"

        custom_cases.append(TestCase(
            src_subnet=src_subnet,
            dst_subnet=dest,
            src_vrf="",
            dst_vrf="",
            expected="PASS",
            reason="custom-step1-icmp",
            phase=step1_phase,
            probe_type="icmp",
            case_id=f"{case_base}-step1",
        ))

        step2_probe_type = "tcp" if protocol == "tcp" else "icmp"
        custom_cases.append(TestCase(
            src_subnet=src_subnet,
            dst_subnet=dest,
            src_vrf="",
            dst_vrf="",
            expected="PASS",
            reason=f"custom-step2-{protocol}",
            phase=step2_phase,
            probe_type=step2_probe_type,
            tcp_ports=[port] if protocol == "tcp" else [],
            case_id=f"{case_base}-step2",
        ))

        if protocol == "udp":
            udp_phase_ids.add(step2_phase)

    return custom_cases, custom_gateway_by_subnet, udp_phase_ids


@router.post("/api/run")
def api_start_run(req: RunIn, request: Request, x_vcenter_session: str = Header(default=""), x_vcenter_host: str = Header(default=""), x_session_token: str = Header(default="")):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    q: _queue.Queue = _queue.Queue()
    cancel_event = threading.Event()
    _runs[run_id] = {"queue": q, "returncode": None, "cancel_event": cancel_event}
    _db.insert_run(run_id, run_id)

    cfg_dict = _read_config()
    # Plugin mode: session header supplied — host/credentials may come from headers
    session_id = x_vcenter_session.strip()
    vc_host = str(cfg_dict.get("vcenter_host", "")).strip() or x_vcenter_host.strip()
    if not vc_host and session_id:
        vc_host = (
            _header_host(request.headers.get("referer", ""))
            or _header_host(request.headers.get("origin", ""))
        )
    vc_user = "" if session_id else str(cfg_dict.get("vcenter_user", "")).strip()
    vc_pass = "" if session_id else get_session_password(x_session_token.strip())
    saved_custom_rules = _db.get_custom_step_rules()
    append_custom_cases, append_custom_gateway_by_subnet, udp_phase_ids = _build_custom_cases_from_rules(saved_custom_rules)

    selected_suite_name = str(req.testsuite or "").strip()
    selected_suite_case_ids: list[str] = []
    selected_suite_expectations: dict[str, str] = {}
    if selected_suite_name:
        suites = _db.get_testsuites()
        picked = next((s for s in suites if str(s.get("name", "")).strip() == selected_suite_name), None)
        if not picked:
            raise HTTPException(400, f"testsuite not found: {selected_suite_name}")
        testcase_rules = list(picked.get("testcase_rules") or [])
        if testcase_rules:
            for row in testcase_rules:
                case_id = str(row.get("testcase_key", "")).strip()
                if not case_id:
                    continue
                action = str(row.get("action", "ALLOW")).strip().upper() or "ALLOW"
                selected_suite_case_ids.append(case_id)
                selected_suite_expectations[case_id] = "PASS" if action != "DENY" else "FAIL"
        else:
            selected_suite_case_ids = list({
                str(x).strip()
                for x in (picked.get("testcase_keys") or [])
                if str(x).strip()
            })
            selected_suite_expectations = {case_id: "PASS" for case_id in selected_suite_case_ids}

        selected_suite_case_ids = list(dict.fromkeys(selected_suite_case_ids))

    def _worker() -> None:
        rc = 4
        try:
            cfg = RunConfig(
                run_id=run_id,
                rows=_db.get_networks(),
                probe_mode=req.probe_mode,
                max_retries=req.max_retries,
                cleanup_on_failure=req.cleanup_on_failure,
                execute_vcenter=req.execute_vcenter,
                phased_testing=(False if selected_suite_name else req.phased_testing),
                vms_per_subnet=req.vms_per_subnet,
                max_vms_per_phase=req.max_vms_per_phase,
                phases=req.phases,
                testsuite="",
                testcase_ids=(selected_suite_case_ids if selected_suite_name else []),
                testcase_expectations=(selected_suite_expectations if selected_suite_name else {}),
                vrf_links=list(req.vrf_links),
                append_custom_cases=append_custom_cases,
                append_custom_gateway_by_subnet=append_custom_gateway_by_subnet,
                poll_method=req.poll_method,
                serial_probe_host=req.serial_probe_host or str(cfg_dict.get("serial_probe_host", "") or ""),
                serial_base_port=req.serial_base_port,
                vsock_base_port=req.vsock_base_port,
                boot_method=req.boot_method or str(cfg_dict.get("boot_method", "ovf") or "ovf"),
                memboot_iso_path=req.memboot_iso_path or str(cfg_dict.get("memboot_iso_path", "") or ""),
                vcenter_host=vc_host,
                vcenter_user=vc_user,
                vcenter_password=vc_pass,
                vcenter_session_id=session_id,
                resource_pool=str(cfg_dict.get("resource_pool", "") or ""),
                ovf_path=str(cfg_dict.get("ovf_path", "") or ""),
                vm_prefix=str(cfg_dict.get("vm_prefix", "nettest") or "nettest"),
            )
            # memboot VMs have no vmtoolsd — mirror the CLI auto-switch:
            # guestinfo is incompatible with memboot; use serial instead.
            if cfg.boot_method == "memboot" and cfg.poll_method == "guestinfo":
                logger.warning(
                    "boot_method=memboot with poll_method=guestinfo is not supported "
                    "(no vmtoolsd in the memboot initramfs); switching to poll_method=serial"
                )
                cfg.poll_method = "serial"

            result_holder: dict = {"result": None}
            rc = execute_run(
                cfg,
                log_cb=lambda line: q.put(line),
                result_cb=lambda r: result_holder.__setitem__("result", r),
                error_cb=lambda e: _db.finish_run_error(run_id, e),
                objects_cb=lambda reg: _db.upsert_created_objects(run_id, reg),
                cancel_event=cancel_event,
            )

            out = result_holder.get("result") or {}
            if out and udp_phase_ids:
                for item in out.get("Results", []):
                    if item.get("phase") in udp_phase_ids:
                        item["actual"] = "UNKNOWN"
                        item["status"] = "fail"
                        item["reason"] = "udp-not-implemented-yet"
                out["FinalStatus"] = "PASS" if all(r.get("status") == "pass" for r in out.get("Results", [])) else "FAIL"
            if out:
                _db.finish_run(run_id, out)
        except Exception as exc:
            tb = traceback.format_exc()
            logger.error("Worker thread crashed for run %s: %s\n%s", run_id, exc, tb)
            _db.finish_run_error(run_id, {"error": str(exc), "type": type(exc).__name__, "traceback": tb})
            rc = 4
        finally:
            _runs[run_id]["returncode"] = rc
            q.put(None)  # always send sentinel

    threading.Thread(target=_worker, daemon=True).start()
    return {"run_id": run_id}


@router.get("/api/run/catalog")
def api_run_catalog():
    def _clustered_endpoint(subnet: str, cluster: str) -> str:
        s = str(subnet or "").strip()
        c = str(cluster or "").strip()
        return f"{s}({c})" if c else s

    rows = _db.get_networks()
    accepted, rejected = validate_and_normalize(rows, "MNGT")

    cases = generate_test_cases(accepted, set(), ())
    custom_rules = _db.get_custom_step_rules()
    custom_cases, _, _ = _build_custom_cases_from_rules(custom_rules)
    cases.extend(custom_cases)
    assign_case_ids(cases)

    suite_counts: dict[str, int] = {}
    for case in cases:
        suite_counts[case.phase] = suite_counts.get(case.phase, 0) + 1

    suite_order = ["network-connectivity"]
    seen = set(suite_order)
    extras = sorted([s for s in suite_counts.keys() if s not in seen])
    ordered_suites = [s for s in suite_order if s in suite_counts] + extras

    return {
        "summary": {
            "accepted_rows": len(accepted),
            "rejected_rows": len(rejected),
            "total_cases": len(cases),
        },
        "suites": [
            {"id": sid, "count": suite_counts[sid]}
            for sid in ordered_suites
        ],
        "testcases": [
            {
                "id": c.case_id,
                "suite": c.phase,
                "src_subnet": c.src_subnet,
                "dst_subnet": c.dst_subnet,
                "src_cluster": str(getattr(c, "src_cluster", "") or ""),
                "dst_cluster": str(getattr(c, "dst_cluster", "") or ""),
                "expected": c.expected,
                "reason": c.reason,
                "label": (
                    f"{_clustered_endpoint(c.src_subnet, str(getattr(c, 'src_cluster', '') or ''))} -> "
                    f"{_clustered_endpoint(c.dst_subnet, str(getattr(c, 'dst_cluster', '') or ''))} "
                    f"[{c.expected}] ({c.reason})"
                ),
            }
            for c in cases
        ],
    }


@router.post("/api/run/custom-steps")
def api_start_custom_steps_run(req: CustomStepRunIn):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    q: _queue.Queue = _queue.Queue()
    cancel_event = threading.Event()
    _runs[run_id] = {"queue": q, "returncode": None, "cancel_event": cancel_event}
    _db.insert_run(run_id, run_id)

    if not req.rules:
        raise HTTPException(400, "rules is required and must contain at least one row")

    if req.probe_mode != "controller-gateway":
        raise HTTPException(400, "custom step run currently supports probe_mode=controller-gateway only")

    custom_cases, custom_gateway_by_subnet, udp_phase_ids = _build_custom_cases_from_rules(req.rules)
    if not custom_cases:
        raise HTTPException(400, "rules contains no valid rows")

    def _worker() -> None:
        rc = 4
        try:
            result_holder: dict = {"result": None}

            rc = execute_run(
                RunConfig(
                    run_id=run_id,
                    rows=[],
                    custom_cases=custom_cases,
                    custom_gateway_by_subnet=custom_gateway_by_subnet,
                    probe_mode=req.probe_mode,
                    execute_vcenter=req.execute_vcenter,
                    max_retries=0,
                    phased_testing=False,
                    vrf_links=[],
                ),
                log_cb=lambda line: q.put(line),
                result_cb=lambda r: result_holder.__setitem__("result", r),
                error_cb=lambda e: _db.finish_run_error(run_id, e),
                objects_cb=lambda reg: _db.upsert_created_objects(run_id, reg),
                cancel_event=cancel_event,
            )

            out = result_holder.get("result") or {}
            if out and udp_phase_ids:
                for item in out.get("Results", []):
                    if item.get("phase") in udp_phase_ids:
                        item["actual"] = "UNKNOWN"
                        item["status"] = "fail"
                        item["reason"] = "udp-not-implemented-yet"
                out["FinalStatus"] = "PASS" if all(r.get("status") == "pass" for r in out.get("Results", [])) else "FAIL"

            if out:
                _db.finish_run(run_id, out)
        except Exception as exc:
            tb = traceback.format_exc()
            logger.error("Worker thread crashed for run %s: %s\n%s", run_id, exc, tb)
            _db.finish_run_error(run_id, {"error": str(exc), "type": type(exc).__name__, "traceback": tb})
            rc = 4
        finally:
            _runs[run_id]["returncode"] = rc
            q.put(None)

    threading.Thread(target=_worker, daemon=True).start()
    return {"run_id": run_id}


@router.get("/api/run/{run_id}/stream")
async def api_stream_run(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404, "Run not found")
    run = _runs[run_id]
    q = run["queue"]
    loop = asyncio.get_running_loop()

    def _get_next():
        while True:
            try:
                return q.get(timeout=1)
            except _queue.Empty:
                if run.get("returncode") is not None:
                    return None

    async def _generate():
        while True:
            line = await loop.run_in_executor(None, _get_next)
            if line is None:
                rc = run.get("returncode", 0)
                yield f"data: __DONE__:{rc}\n\n"
                break
            yield f"data: {json.dumps(line.rstrip())}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/run/{run_id}/result")
def api_get_result(run_id: str):
    data = _db.get_result(run_id)
    if data:
        return data
    raise HTTPException(404, "Result not found")


@router.post("/api/run/{run_id}/cancel")
def api_cancel_run(run_id: str):
    """Signal a running test to stop gracefully after the current step."""
    run = _runs.get(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    if run.get("returncode") is not None:
        return {"ok": False, "detail": "run already finished"}
    ev: threading.Event | None = run.get("cancel_event")
    if ev is None:
        raise HTTPException(500, "cancel_event missing for this run")
    ev.set()
    logger.info("Cancel requested for run %s", run_id)
    return {"ok": True, "run_id": run_id}


@router.post("/api/run/{run_id}/cleanup")
def api_cleanup_run(run_id: str, request: Request, x_vcenter_session: str = Header(default=""), x_vcenter_host: str = Header(default=""), x_session_token: str = Header(default="")):
    """Delete all VMs recorded in the created_objects table for a run."""
    registry = _db.get_created_objects(run_id)
    if registry.get("cleaned"):
        return {"cleaned": 0, "failed": 0, "skipped": 0, "detail": "already cleaned"}
    moids = [m for m in registry.get("vms", []) if m and m != "dry-run-moid"]
    iso_entries = [e for e in registry.get("isos", []) if isinstance(e, dict) and e.get("path")]
    if not moids and not iso_entries:
        _db.mark_cleaned(run_id)
        return {
            "cleaned": 0,
            "failed": 0,
            "skipped": 0,
            "iso_cleaned": 0,
            "iso_failed": 0,
            "detail": "no real VMs/ISOs to delete",
        }

    cfg = _read_config()
    session_id       = x_vcenter_session.strip()
    # In plugin mode the host may arrive via X-Vcenter-Host header; fall back to config
    vcenter_host     = str(cfg.get("vcenter_host", "")).strip() or x_vcenter_host.strip()
    if not vcenter_host and session_id:
        vcenter_host = (
            _header_host(request.headers.get("referer", ""))
            or _header_host(request.headers.get("origin", ""))
        )
    vcenter_user     = "" if session_id else str(cfg.get("vcenter_user", "")).strip()
    vcenter_password = "" if session_id else get_session_password(x_session_token.strip())
    vm_prefix        = cfg.get("vm_prefix", "nettest")

    if not session_id:
        # Standalone mode: host and credentials are required
        if not vcenter_host:
            raise HTTPException(400, "vCenter host not configured")
        if not vcenter_user or not vcenter_password:
            raise HTTPException(400, "vCenter credentials not configured")

    try:
        from pyVmomi import vim as _vim
    except ImportError:
        raise HTTPException(500, "pyvmomi not installed")

    try:
        si = connect_vcenter_auto(host=vcenter_host, user=vcenter_user, pwd=vcenter_password, session_id=session_id)
    except Exception as exc:
        raise HTTPException(502, f"vCenter connect failed: {exc}")

    try:
        content = si.RetrieveContent()
        vm_by_moid = get_all_vms_by_moid(content, _vim)

        cleaned, failed, skipped = 0, 0, 0
        iso_cleaned, iso_failed = 0, 0
        failures = []
        for moid in moids:
            vm_obj = vm_by_moid.get(moid)
            if vm_obj is None:
                skipped += 1
                continue
            ok, reason = delete_vm(vm_obj, vm_prefix)
            if ok:
                cleaned += 1
            elif reason == "prefix-mismatch":
                skipped += 1
                failures.append({"moid": moid, "name": str(getattr(vm_obj, "name", "")), "reason": reason})
            else:
                failed += 1
                failures.append({"moid": moid, "name": str(getattr(vm_obj, "name", "")), "reason": reason})

        failed_iso_entries = []
        for entry in iso_entries:
            ds_path = str(entry.get("path", "")).strip()
            dc_name = str(entry.get("datacenter", "")).strip()
            if not ds_path:
                continue
            try:
                dc_obj = _find_datacenter(content, _vim, dc_name) if dc_name else None
                task = content.fileManager.DeleteDatastoreFile_Task(name=ds_path, datacenter=dc_obj)
                task_result = str(getattr(task.info, "state", ""))
                if task_result != "success":
                    from nettest.vcenter_utils import wait_for_task
                    wait_for_task(task)
                iso_cleaned += 1
            except Exception as exc:
                iso_failed += 1
                failed_iso_entries.append(entry)
                failures.append({"iso_path": ds_path, "datacenter": dc_name, "reason": str(exc)})

        failed_moids = {f.get("moid") for f in failures if isinstance(f, dict) and f.get("moid")}
        registry["vms"] = [m for m in registry["vms"] if m in failed_moids]
        registry["isos"] = failed_iso_entries
        _db.upsert_created_objects(run_id, registry)
        if not registry["vms"] and not registry.get("isos"):
            _db.mark_cleaned(run_id)

        return {
            "cleaned": cleaned,
            "failed": failed,
            "skipped": skipped,
            "iso_cleaned": iso_cleaned,
            "iso_failed": iso_failed,
            "failures": failures,
        }
    finally:
        disconnect_vcenter(si)
