"""API routes: /api/run and /api/run/{run_id}/*"""
from __future__ import annotations

import asyncio
import json
import logging
import queue as _queue
import threading
import traceback
from datetime import datetime, timezone
from typing import List

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import nettest.db as _db
from nettest.runner import RunConfig, execute_run
from nettest.vcenter_utils import connect_vcenter_auto, disconnect_vcenter, get_all_vms_by_moid, delete_vm
from nettest.api.deps import WORKSPACE, ARTIFACTS, _runs, _read_config

router = APIRouter()


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
    vrf_links: List[str] = []  # "FROM:TO" or "FROM:TO:FAIL"
    # Probe communication channel (guestinfo | serial | vsock)
    poll_method: str = "guestinfo"
    serial_probe_host: str = ""
    serial_base_port: int = 10000
    vsock_base_port: int = 9000
    # VM boot method (ovf | memboot)
    boot_method: str = "memboot"
    memboot_iso_path: str = ""


@router.post("/api/run")
def api_start_run(req: RunIn, x_vcenter_session: str = Header(default="")):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    q: _queue.Queue = _queue.Queue()
    _runs[run_id] = {"queue": q, "returncode": None}
    _db.insert_run(run_id, run_id)

    cfg_dict = _read_config()
    vc_host = str(cfg_dict.get("vcenter_host", "")).strip()
    # Plugin mode: session header supplied — skip credentials
    session_id = x_vcenter_session.strip()
    vc_user = "" if session_id else str(cfg_dict.get("vcenter_user", "")).strip()
    vc_pass = "" if session_id else str(cfg_dict.get("vcenter_password", "")).strip()

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
                phased_testing=req.phased_testing,
                vms_per_subnet=req.vms_per_subnet,
                max_vms_per_phase=req.max_vms_per_phase,
                phases=req.phases,
                vrf_links=list(req.vrf_links),
                poll_method=req.poll_method,
                serial_probe_host=req.serial_probe_host,
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
            rc = execute_run(
                cfg,
                log_cb=lambda line: q.put(line),
                result_cb=lambda r: _db.finish_run(run_id, r),
                error_cb=lambda e: _db.finish_run_error(run_id, e),
                objects_cb=lambda reg: _db.upsert_created_objects(run_id, reg),
            )
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


@router.post("/api/run/{run_id}/cleanup")
def api_cleanup_run(run_id: str, x_vcenter_session: str = Header(default="")):
    """Delete all VMs recorded in the created_objects table for a run."""
    registry = _db.get_created_objects(run_id)
    if registry.get("cleaned"):
        return {"cleaned": 0, "failed": 0, "skipped": 0, "detail": "already cleaned"}
    moids = [m for m in registry.get("vms", []) if m and m != "dry-run-moid"]
    if not moids:
        _db.mark_cleaned(run_id)
        return {"cleaned": 0, "failed": 0, "skipped": 0, "detail": "no real VMs to delete"}

    cfg = _read_config()
    vcenter_host     = cfg.get("vcenter_host", "")
    vcenter_user     = cfg.get("vcenter_user", "")
    vcenter_password = cfg.get("vcenter_password", "")
    vm_prefix        = cfg.get("vm_prefix", "nettest")
    session_id       = x_vcenter_session.strip()

    if not vcenter_host:
        raise HTTPException(400, "vCenter host not configured")
    if not session_id and (not vcenter_user or not vcenter_password):
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

        failed_moids = {f["moid"] for f in failures}
        registry["vms"] = [m for m in registry["vms"] if m in failed_moids]
        _db.upsert_created_objects(run_id, registry)
        if not registry["vms"]:
            _db.mark_cleaned(run_id)

        return {"cleaned": cleaned, "failed": failed, "skipped": skipped, "failures": failures}
    finally:
        disconnect_vcenter(si)
