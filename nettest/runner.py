"""Core test-run handler.

Both the CLI (nettest_runner.py) and the API (routes_run.py) call
`execute_run()`.  No argparse, no subprocess, no file I/O for results.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from nettest.input_handlers import load_input, validate_and_normalize
from nettest.models import TestCase, TestResult
from nettest.placement import plan_cluster_placements, plan_vlan_bindings, validate_vcenter_requirements
from nettest.probe import run_icmp_checks
from nettest.provisioning import provision_test_vms, cleanup_vms, reprobe_vm_instances
from nettest.policy import (
    ALL_PHASE_IDS,
    assign_case_ids,
    generate_phased_cases,
    generate_test_cases,
    merge_retry_results,
    parse_allowlist,
    parse_vrf_links,
    parse_vrf_links_cli,
    select_retry_cases,
    summarize_expected,
)

logger = logging.getLogger(__name__)


def _filter_cases_by_selection(
    cases: List[TestCase],
    testsuite: str,
    testcase_ids: List[str],
) -> List[TestCase]:
    selected = list(cases)
    suite = (testsuite or "").strip()
    if suite and suite != "all":
        selected = [c for c in selected if c.phase == suite]

    ids = {x.strip() for x in (testcase_ids or []) if str(x).strip()}
    if ids:
        selected = [c for c in selected if c.case_id in ids]
    return selected


def now_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _resolve_input_rows(
    cfg: "RunConfig",
    error_cb: Optional[Callable[[Dict], None]],
) -> tuple[Optional[List[Dict[str, str]]], str, Optional[int]]:
    if cfg.rows is not None:
        return cfg.rows, "<database>", None

    input_path = Path(cfg.input)
    if not input_path.exists():
        err = {
            "error": f"Input file not found: {cfg.input}",
            "type": "InputNotFound",
            "FinalStatus": "error",
        }
        logger.error("Input file not found: %s", input_path)
        if error_cb:
            error_cb(err)
        return None, "", 2

    return load_input(input_path), str(input_path), None


def _prepare_run_inputs(cfg: "RunConfig", raw_rows: List[Dict[str, str]]) -> Dict[str, Any]:
    if getattr(cfg, "custom_cases", None) is not None:
        cases = list(cfg.custom_cases or [])
        custom_gw = dict(getattr(cfg, "custom_gateway_by_subnet", {}) or {})
        assign_case_ids(cases, prefix="custom")
        filtered_cases = _filter_cases_by_selection(cases, cfg.testsuite, cfg.testcase_ids)
        return {
            "accepted": [],
            "rejected": [],
            "allowlist": set(),
            "vrf_links": [],
            "cases": filtered_cases,
            "placements": [],
            "network_bindings": [],
            "placements_dict": {},
            "bindings_dict": {},
            "gateway_by_subnet": custom_gw,
        }

    if cfg.execute_vcenter:
        validate_vcenter_requirements(cfg)

    accepted, rejected = validate_and_normalize(raw_rows, cfg.cluster_mngt_token)
    allowlist = parse_allowlist(cfg.allow_vrf)

    vrf_links_config = [v for v in (cfg.vrf_links or []) if isinstance(v, dict)]
    vrf_links_cli = [v for v in (cfg.vrf_links or []) if isinstance(v, str)]
    vrf_links = parse_vrf_links(vrf_links_config) + parse_vrf_links_cli(vrf_links_cli)

    cases = generate_test_cases(accepted, allowlist, vrf_links)
    placements = plan_cluster_placements(accepted, cfg)
    network_bindings = plan_vlan_bindings(accepted, cfg)

    placements_dict = {(p.datacenter, p.cluster): p for p in placements}
    bindings_dict = {(b.datacenter, b.cluster, b.vlan): b for b in network_bindings}
    gateway_by_subnet = {r.subnet: r.gw for r in accepted}

    appended_cases = list(getattr(cfg, "append_custom_cases", []) or [])
    appended_gw = dict(getattr(cfg, "append_custom_gateway_by_subnet", {}) or {})
    if appended_cases:
        cases = list(cases) + appended_cases
    assign_case_ids(cases)

    # Testsuite can turn each selected testcase into ALLOW(PASS) or DENY(FAIL).
    testcase_expectations = dict(getattr(cfg, "testcase_expectations", {}) or {})
    if testcase_expectations:
        for case in cases:
            override = str(testcase_expectations.get(case.case_id, "")).strip().upper()
            if override in ("PASS", "FAIL"):
                case.expected = override

    cases = _filter_cases_by_selection(cases, cfg.testsuite, cfg.testcase_ids)
    if appended_gw:
        gateway_by_subnet.update(appended_gw)

    return {
        "accepted": accepted,
        "rejected": rejected,
        "allowlist": allowlist,
        "vrf_links": vrf_links,
        "cases": cases,
        "placements": placements,
        "network_bindings": network_bindings,
        "placements_dict": placements_dict,
        "bindings_dict": bindings_dict,
        "gateway_by_subnet": gateway_by_subnet,
    }


def _connect_vcenter_if_needed(cfg: "RunConfig") -> Any:
    if not cfg.execute_vcenter:
        return None
    from nettest.vcenter_utils import connect_vcenter
    return connect_vcenter(cfg)


def _init_probe_servers(cfg: "RunConfig") -> tuple[Any, Any]:
    serial_srv = None
    vsock_srv = None

    if cfg.poll_method == "serial":
        from nettest.serial_probe import SerialProbeServer, detect_controller_ip

        host = cfg.serial_probe_host or detect_controller_ip(cfg.vcenter_host)
        serial_srv = SerialProbeServer(server_ip=host, base_port=cfg.serial_base_port)
        cfg._serial_server = serial_srv  # type: ignore[attr-defined]
        logger.info(
            "SerialProbeServer listening on %s starting from port %s",
            host,
            cfg.serial_base_port,
        )
    elif cfg.poll_method == "vsock":
        from nettest.vsock_probe import VsockProbeServer

        vsock_srv = VsockProbeServer(port=cfg.vsock_base_port)
        vsock_srv.start()
        cfg._vsock_server = vsock_srv  # type: ignore[attr-defined]
        logger.info("VsockProbeServer listening on vsock port %s", cfg.vsock_base_port)

    return serial_srv, vsock_srv


def _find_datacenter_for_iso_cleanup(content: Any, vim: Any, datacenter_name: str) -> Any:
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datacenter], True)
    try:
        target = str(datacenter_name or "").strip().upper()
        for dc in view.view:
            if str(getattr(dc, "name", "")).strip().upper() == target:
                return dc
    finally:
        view.Destroy()
    return None


def _cleanup_uploaded_isos(
    *,
    cfg: "RunConfig",
    created_registry: Dict[str, List[Any]],
    persist_registry: Callable[[], None],
    on_failure: bool,
) -> Dict[str, Any]:
    iso_entries = [e for e in created_registry.get("isos", []) if isinstance(e, dict) and e.get("path")]
    if not iso_entries:
        return {"cleaned": 0, "failed": 0, "retained": 0, "reason": "no-uploaded-isos"}

    if not cfg.execute_vcenter:
        return {
            "cleaned": 0,
            "failed": 0,
            "retained": len(iso_entries),
            "reason": "dry-run-mode",
        }

    if on_failure and not cfg.cleanup_on_failure:
        return {
            "cleaned": 0,
            "failed": 0,
            "retained": len(iso_entries),
            "reason": "cleanup_on_failure=False; isos retained for troubleshooting",
        }

    try:
        from pyVmomi import vim
        from nettest.vcenter_utils import vcenter_session, wait_for_task
    except Exception as exc:
        return {
            "cleaned": 0,
            "failed": len(iso_entries),
            "retained": len(iso_entries),
            "reason": f"pyvmomi-import-failed: {exc}",
        }

    cleaned = 0
    failed: List[Dict[str, str]] = []
    retained_entries: List[Dict[str, str]] = []

    try:
        with vcenter_session(cfg) as si:
            content = si.RetrieveContent()
            for entry in iso_entries:
                ds_path = str(entry.get("path", "")).strip()
                dc_name = str(entry.get("datacenter", "")).strip()
                if not ds_path:
                    continue
                try:
                    dc_obj = _find_datacenter_for_iso_cleanup(content, vim, dc_name) if dc_name else None
                    task = content.fileManager.DeleteDatastoreFile_Task(name=ds_path, datacenter=dc_obj)
                    state = str(getattr(getattr(task, "info", None), "state", ""))
                    if state != "success":
                        wait_for_task(task)
                    cleaned += 1
                except Exception as exc:
                    retained_entries.append({"datacenter": dc_name, "path": ds_path})
                    failed.append({"datacenter": dc_name, "path": ds_path, "reason": str(exc)})
    except Exception as exc:
        return {
            "cleaned": 0,
            "failed": len(iso_entries),
            "retained": len(iso_entries),
            "reason": f"vcenter-connect-failed: {exc}",
        }

    created_registry["isos"] = retained_entries
    persist_registry()

    out: Dict[str, Any] = {
        "cleaned": cleaned,
        "failed": len(failed),
        "retained": len(retained_entries),
    }
    if failed:
        out["failed_isos"] = failed
    return out


def _run_phased_testing(
    *,
    cfg: "RunConfig",
    accepted: List[Any],
    allowlist: Any,
    vrf_links: List[Any],
    placements_dict: Dict[Any, Any],
    bindings_dict: Dict[Any, Any],
    gateway_by_subnet: Dict[str, str],
    created_registry: Dict[str, List[Any]],
    persist_registry: Callable[[], None],
    all_vm_instances: List[Any],
    effective_guest_ssh_password: str,
    vcenter_si: Any,
    check_cancel: Callable[[str], None],
) -> tuple[List[TestResult], List[Dict[str, Any]]]:
    all_results: List[TestResult] = []
    phase_summaries: List[Dict[str, Any]] = []
    had_failure = False

    enabled_phases_list = None
    if cfg.phases:
        enabled_phases_list = [p.strip() for p in cfg.phases.split(",") if p.strip()]
    elif cfg.testsuite and cfg.testsuite != "all":
        enabled_phases_list = [cfg.testsuite]

    phases = generate_phased_cases(
        accepted,
        allowlist,
        vrf_links,
        vms_per_subnet=cfg.vms_per_subnet,
        max_vms_per_phase=cfg.max_vms_per_phase,
        enabled_phases=enabled_phases_list,
    )
    logger.info("Phased testing: %s phase(s) generated", len(phases))

    selected_case_ids = {x.strip() for x in (cfg.testcase_ids or []) if str(x).strip()}

    for phase in phases:
        if cfg.testsuite and cfg.testsuite != "all" and phase.phase_id != cfg.testsuite:
            continue
        if selected_case_ids:
            phase.cases = [c for c in phase.cases if c.case_id in selected_case_ids]
        if not phase.cases:
            continue

        check_cancel(f"before phase {phase.phase_id}")
        sep = "=" * 60
        logger.info("")
        logger.info(sep)
        logger.info("Phase [%s]: %s", phase.phase_id, phase.name)
        logger.info("  %s", phase.description)
        logger.info("  Cases: %s  VMs/subnet: %s", len(phase.cases), phase.vms_per_subnet)
        logger.info(sep)

        phase_subnets = {c.src_subnet for c in phase.cases} | {c.dst_subnet for c in phase.cases}
        phase_rows = [r for r in accepted if r.subnet in phase_subnets and r.mode == "vm-provisioned"]

        phase_vm_instances: List[Any] = []
        if cfg.execute_vcenter and cfg.probe_mode in ("in-guest", "in-guest-ping") and phase_rows:
            logger.info("Provisioning %s VMs for phase...", len(phase_rows) * phase.vms_per_subnet)

            def _on_vm_created_phase(moid: str) -> None:
                created_registry["vms"].append(moid)
                persist_registry()

            def _on_iso_created_phase(datacenter: str, ds_path: str) -> None:
                created_registry["isos"].append({"datacenter": datacenter, "path": ds_path})
                persist_registry()

            phase_vm_instances = provision_test_vms(
                phase_rows,
                placements_dict,
                bindings_dict,
                cfg,
                cfg.run_id,
                cases=phase.cases,
                gateway_by_subnet=gateway_by_subnet,
                vms_per_subnet=phase.vms_per_subnet,
                on_vm_created=_on_vm_created_phase,
                on_iso_created=_on_iso_created_phase,
                check_cancel=check_cancel,
            )
            all_vm_instances.extend(phase_vm_instances)
            logger.info("Provisioned %s VMs", len(phase_vm_instances))

        phase_results = run_icmp_checks(
            phase.cases,
            execute_vcenter=cfg.execute_vcenter,
            probe_mode=cfg.probe_mode,
            gateway_by_subnet=gateway_by_subnet,
            timeout_sec=cfg.probe_timeout_sec,
            vm_instances=phase_vm_instances,
            guest_ssh_user="root",
            guest_ssh_password=effective_guest_ssh_password,
            vcenter_si=vcenter_si,
            poll_method=cfg.poll_method,
            check_cancel=check_cancel,
        )
        all_results = merge_retry_results(all_results, phase_results)

        ph_passed = sum(1 for r in phase_results if r.status == "pass")
        ph_failed = sum(1 for r in phase_results if r.status != "pass")
        logger.info("Phase results: %s passed, %s failed", ph_passed, ph_failed)
        phase_summaries.append(
            {
                "phase_id": phase.phase_id,
                "name": phase.name,
                "batch_index": phase.batch_index,
                "cases": len(phase.cases),
                "passed": ph_passed,
                "failed": ph_failed,
            }
        )

        if ph_failed:
            had_failure = True
            if phase_vm_instances:
                if cfg.cleanup_on_failure:
                    cleanup_vms(phase_vm_instances, cfg, on_failure=True)
                else:
                    logger.warning("Phase failed — retaining VMs for troubleshooting.")
            logger.warning("Stopping after phase [%s] failure.", phase.phase_id)
            break

        if phase_vm_instances:
            logger.info("Cleaning up %s phase VMs...", len(phase_vm_instances))
            cleanup_vms(phase_vm_instances, cfg, on_failure=False)

    iso_cleanup = _cleanup_uploaded_isos(
        cfg=cfg,
        created_registry=created_registry,
        persist_registry=persist_registry,
        on_failure=had_failure,
    )
    logger.info("ISO cleanup completed: %s", iso_cleanup)

    return all_results, phase_summaries


def _run_classic_testing(
    *,
    cfg: "RunConfig",
    accepted: List[Any],
    cases: List[TestCase],
    placements_dict: Dict[Any, Any],
    bindings_dict: Dict[Any, Any],
    gateway_by_subnet: Dict[str, str],
    created_registry: Dict[str, List[Any]],
    persist_registry: Callable[[], None],
    all_vm_instances: List[Any],
    effective_guest_ssh_password: str,
    vcenter_si: Any,
    check_cancel: Callable[[str], None],
) -> tuple[List[TestResult], List[Dict[str, int]], Optional[Dict[str, Any]]]:
    all_results: List[TestResult] = []
    attempt_history: List[Dict[str, int]] = []
    cleanup_result: Optional[Dict[str, Any]] = None

    vm_instances: List[Any] = []
    if cfg.execute_vcenter and cfg.probe_mode in ("in-guest", "in-guest-ping"):
        # Provision only subnets participating in the selected cases.
        selected_subnets = {c.src_subnet for c in cases} | {c.dst_subnet for c in cases}
        vm_rows = [
            r for r in accepted
            if r.mode == "vm-provisioned" and r.subnet in selected_subnets
        ]
        if vm_rows:
            logger.info("Provisioning test VMs...")

            def _on_vm_created(moid: str) -> None:
                created_registry["vms"].append(moid)
                persist_registry()

            def _on_iso_created(datacenter: str, ds_path: str) -> None:
                created_registry["isos"].append({"datacenter": datacenter, "path": ds_path})
                persist_registry()

            vm_instances = provision_test_vms(
                vm_rows,
                placements_dict,
                bindings_dict,
                cfg,
                cfg.run_id,
                cases=cases,
                gateway_by_subnet=gateway_by_subnet,
                vms_per_subnet=cfg.vms_per_subnet,
                on_vm_created=_on_vm_created,
                on_iso_created=_on_iso_created,
                check_cancel=check_cancel,
            )
            all_vm_instances.extend(vm_instances)
            logger.info("Provisioned %s test VMs", len(vm_instances))

    current_cases: List[TestCase] = list(cases)
    for attempt in range(cfg.max_retries + 1):
        if not current_cases:
            break
        check_cancel(f"before attempt {attempt}")

        if (
            attempt > 0
            and cfg.execute_vcenter
            and cfg.probe_mode in ("in-guest", "in-guest-ping")
            and cfg.poll_method in ("serial", "vsock")
        ):
            logger.info("Retry attempt %s: re-running %s probe collection on existing VMs", attempt, cfg.poll_method)
            reprobe_vm_instances(
                instances=vm_instances,
                cases=current_cases,
                gateway_by_subnet=gateway_by_subnet,
                args=cfg,
                poll_method=cfg.poll_method,
                check_cancel=check_cancel,
            )

        attempt_results = run_icmp_checks(
            current_cases,
            execute_vcenter=cfg.execute_vcenter,
            probe_mode=cfg.probe_mode,
            gateway_by_subnet=gateway_by_subnet,
            timeout_sec=cfg.probe_timeout_sec,
            vm_instances=vm_instances,
            guest_ssh_user="root",
            guest_ssh_password=effective_guest_ssh_password,
            vcenter_si=vcenter_si,
            poll_method=cfg.poll_method,
            check_cancel=check_cancel,
        )
        all_results = merge_retry_results(all_results, attempt_results)
        failed = [r for r in all_results if r.status != "pass"]
        attempt_history.append(
            {
                "attempt": attempt,
                "executed_cases": len(current_cases),
                "failed_cases": len(failed),
            }
        )
        if not failed or attempt >= cfg.max_retries:
            break
        current_cases = select_retry_cases(cases, all_results, cfg.retry_mode)

    failed_results = [r for r in all_results if r.status != "pass"]
    if vm_instances:
        logger.info("Cleaning up test VMs...")
        cleanup_result = cleanup_vms(vm_instances, cfg, on_failure=bool(failed_results))
        logger.info("VM cleanup completed: %s", cleanup_result)

    iso_cleanup = _cleanup_uploaded_isos(
        cfg=cfg,
        created_registry=created_registry,
        persist_registry=persist_registry,
        on_failure=bool(failed_results),
    )
    logger.info("ISO cleanup completed: %s", iso_cleanup)
    if cleanup_result is None:
        cleanup_result = {}
    cleanup_result["iso_cleanup"] = iso_cleanup

    return all_results, attempt_history, cleanup_result


def _build_result_payload(
    *,
    cfg: "RunConfig",
    input_source: str,
    accepted: List[Any],
    rejected: List[Any],
    cases: List[TestCase],
    vrf_links: List[Any],
    placements: List[Any],
    network_bindings: List[Any],
    all_vm_instances: List[Any],
    all_results: List[TestResult],
    attempt_history: List[Dict[str, int]],
    phase_summaries: List[Dict[str, Any]],
    cleanup_result: Optional[Dict[str, Any]],
    created_registry: Dict[str, List[Any]],
) -> Dict[str, Any]:
    planned_vms: List[Dict[str, Any]] = []
    if not cfg.execute_vcenter:
        selected_subnets = {c.src_subnet for c in cases} | {c.dst_subnet for c in cases}
        vm_rows = [
            r for r in accepted
            if r.mode == "vm-provisioned" and r.subnet in selected_subnets
        ]
        for net_idx, row in enumerate(vm_rows, start=1):
            for vm_idx in range(max(1, int(cfg.vms_per_subnet))):
                planned_vms.append(
                    {
                        "vm_name": f"{cfg.vm_prefix}-{cfg.run_id}-net-{net_idx:04d}-{vm_idx}",
                        "vm_index": vm_idx,
                        "datacenter": row.datacenter,
                        "cluster": row.cluster,
                        "subnet": row.subnet,
                        "vlan": row.vlan,
                    }
                )

    failed_results = [r for r in all_results if r.status != "pass"]
    success = len(failed_results) == 0

    return {
        "Plan": {
            "policy": "observe-connectivity-by-default; testsuite allow/deny overrides expectations",
            "probe": "icmp-only",
            "probe_mode": cfg.probe_mode,
            "retry_mode": cfg.retry_mode,
            "max_retries": cfg.max_retries,
            "phased_testing": cfg.phased_testing,
            "vms_per_subnet": cfg.vms_per_subnet,
            "max_vms_per_phase": cfg.max_vms_per_phase,
            "vrf_links": [asdict(l) for l in vrf_links],
            "enabled_phases": (
                [p.strip() for p in cfg.phases.split(",") if p.strip()] if cfg.phases else ALL_PHASE_IDS
            ),
            "testsuite": cfg.testsuite,
            "testcase_expectations": dict(getattr(cfg, "testcase_expectations", {}) or {}),
        },
        "ParsedInput": {
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "mngt_esxi_skipped": [asdict(r) for r in accepted if r.mode == "mngt-esxi"],
            "vm_provisioned": [asdict(r) for r in accepted if r.mode == "vm-provisioned"],
            "rejected": rejected,
        },
        "ExpectedPolicy": summarize_expected(cases),
        "Execution": {
            "input_file": input_source,
            "execute_vcenter": cfg.execute_vcenter,
            "placement": {
                "datastore_preference": cfg.datastore,
                "folder": "datacenter-vm-root",
                "resource_pool": cfg.resource_pool,
            },
            "selected_placements": [asdict(p) for p in placements],
            "network_bindings": [asdict(b) for b in network_bindings],
            "vm_source": {
                "ovf_path": cfg.ovf_path,
                "vm_prefix": cfg.vm_prefix,
                "random_seed": cfg.random_seed,
            },
            "created_vms": [
                {
                    "host_name": vm.host_name,
                    "vm_name": vm.vm_name,
                    "vm_index": vm.vm_index,
                    "moid": vm.moid,
                    "datacenter": vm.datacenter,
                    "cluster": vm.cluster,
                    "subnet": vm.subnet,
                    "vlan": vm.vlan,
                    "ip_address": vm.ip_address,
                    "mac_address": vm.mac_address,
                    "dpg_name": vm.dpg_name,
                }
                for vm in all_vm_instances
            ],
            "planned_vms": planned_vms,
            "planned_vm_summary": {
                "count": len(planned_vms),
                "subnets": len({v["subnet"] for v in planned_vms}),
                "vms_per_subnet": max(1, int(cfg.vms_per_subnet)),
            },
            "skipped_vm_provisioning_rows": [
                {"subnet": r.subnet, "cluster": r.cluster, "reason": "cluster-is-mngt"}
                for r in accepted
                if r.mode == "mngt-esxi"
            ],
        },
        "Phases": phase_summaries if cfg.phased_testing else [],
        "Results": [asdict(r) for r in all_results],
        "Retry": {"mode": cfg.retry_mode, "history": attempt_history},
        "Cleanup": {
            "success_cleanup_performed": success,
            "failure_cleanup_performed": bool((not success) and cfg.cleanup_on_failure),
            "cleanup_on_failure_requested": cfg.cleanup_on_failure,
            "retained_resources": [] if success else created_registry,
            "vm_cleanup": cleanup_result or {},
        },
        "NextSteps": [
            "Provide vCenter placement details and use execute_vcenter=True for real provisioning.",
            "Review retained resources when failures occur.",
        ],
        "FinalStatus": "PASS" if success else "FAIL",
    }


class RunConfig:
    """All parameters for a single test run — no argparse dependency."""

    def __init__(
        self,
        *,
        run_id: str = "",
        input: str = "input.csv",
        probe_mode: str = "dry-run",
        probe_timeout_sec: int = 1,
        retry_mode: str = "failed-only",
        max_retries: int = 1,
        allow_vrf: Optional[List[str]] = None,
        cluster_mngt_token: str = "MNGT",
        vm_prefix: str = "nettest",
        vcenter_host: str = "",
        vcenter_user: str = "",
        vcenter_password: str = "",
        vcenter_session_id: str = "",
        rows: Optional[List[Dict[str, str]]] = None,
        datastore: str = "",
        resource_pool: str = "",
        ovf_path: str = "",
        random_seed: Optional[int] = None,
        cleanup_on_failure: bool = False,
        execute_vcenter: bool = False,
        phased_testing: bool = False,
        vms_per_subnet: int = 1,
        max_vms_per_phase: int = 20,
        vrf_links: Optional[List[Any]] = None,
        custom_cases: Optional[List[TestCase]] = None,
        custom_gateway_by_subnet: Optional[Dict[str, str]] = None,
        append_custom_cases: Optional[List[TestCase]] = None,
        append_custom_gateway_by_subnet: Optional[Dict[str, str]] = None,
        phases: str = "",
        testsuite: str = "",
        testcase_ids: Optional[List[str]] = None,
        testcase_expectations: Optional[Dict[str, str]] = None,
        # ── Probe communication channel ───────────────────────────────────────
        poll_method: str = "guestinfo",
        # serial: controller IP reachable by ESXi NFC (auto-detected when empty)
        serial_probe_host: str = "",
        # first TCP port for serial-port backing; each VM gets base+index
        serial_base_port: int = 10000,
        # vsock port the guest connects to on VMADDR_CID_HOST (CID=2)
        vsock_base_port: int = 9000,
        # ── VM boot method ────────────────────────────────────────────────────
        # "memboot": diskless mini ISO boot, no VMDK at all (default)
        # "ovf": deploy OVF seed VM per cluster + linked clones (legacy)
        boot_method: str = "memboot",
        # local ISO path (or pre-staged "[ds] path" string) for boot_method=memboot
        memboot_iso_path: str = "",
    ):
        self.run_id = run_id or now_run_id()
        self.input = input
        self.probe_mode = probe_mode
        self.probe_timeout_sec = probe_timeout_sec
        self.retry_mode = retry_mode
        self.max_retries = max_retries
        self.allow_vrf = allow_vrf or []
        self.cluster_mngt_token = cluster_mngt_token
        self.vm_prefix = vm_prefix
        self.vcenter_host = vcenter_host
        self.vcenter_user = vcenter_user
        self.vcenter_password = vcenter_password
        self.vcenter_session_id = vcenter_session_id
        self.rows = rows
        self.datastore = datastore
        self.resource_pool = resource_pool
        self.ovf_path = ovf_path
        self.random_seed = random_seed
        self.cleanup_on_failure = cleanup_on_failure
        self.execute_vcenter = execute_vcenter
        self.phased_testing = phased_testing
        self.vms_per_subnet = vms_per_subnet
        self.max_vms_per_phase = max_vms_per_phase
        self.vrf_links = vrf_links or []
        self.custom_cases = custom_cases
        self.custom_gateway_by_subnet = custom_gateway_by_subnet or {}
        self.append_custom_cases = append_custom_cases or []
        self.append_custom_gateway_by_subnet = append_custom_gateway_by_subnet or {}
        self.phases = phases
        self.testsuite = testsuite
        self.testcase_ids = testcase_ids or []
        self.testcase_expectations = testcase_expectations or {}
        self.poll_method = poll_method
        self.serial_probe_host = serial_probe_host
        self.serial_base_port = serial_base_port
        self.vsock_base_port = vsock_base_port
        self.boot_method = boot_method
        self.memboot_iso_path = memboot_iso_path


import threading as _threading


class RunCancelledError(Exception):
    """Raised internally when a cancel signal is received."""


def execute_run(
    cfg: RunConfig,
    log_cb: Optional[Callable[[str], None]] = None,
    result_cb: Optional[Callable[[Dict], None]] = None,
    error_cb: Optional[Callable[[Dict], None]] = None,
    objects_cb: Optional[Callable[[Dict], None]] = None,
    cancel_event: Optional[_threading.Event] = None,
) -> int:
    """Run the network test with the given RunConfig.

    log_cb(line)        — called for each log line (optional)
    result_cb(result)   — called with the full result dict on success
    error_cb(error)     — called with error dict on failure
    objects_cb(registry) — called whenever created-objects registry changes
    cancel_event        — set this event to interrupt the run gracefully
    Returns process-style exit code: 0=PASS, 1=FAIL, 2=input-error,
    3=not-implemented, 4=unhandled-error, 5=cancelled.
    """
    _cancel = cancel_event or _threading.Event()

    def _check_cancel(label: str = "") -> None:
        if _cancel.is_set():
            raise RunCancelledError(label or "run cancelled by user")

    _log_handler = _setup_log_cb(log_cb)
    logger.info(
        "Starting run %s  probe_mode=%s  vcenter=%s",
        cfg.run_id,
        cfg.probe_mode,
        cfg.execute_vcenter,
    )

    created_registry: Dict[str, List[Any]] = {"vms": [], "nics": [], "tags": [], "isos": []}

    def _persist_registry() -> None:
        if objects_cb:
            objects_cb(created_registry)

    raw_rows, input_source, early_exit = _resolve_input_rows(cfg, error_cb)
    if early_exit is not None:
        _teardown_log_cb(_log_handler)
        return early_exit

    _serial_srv = None
    _vsock_srv = None
    all_vm_instances: List[Any] = []

    try:
        effective_guest_ssh_password = "nettest-alpine"

        prepared = _prepare_run_inputs(cfg, raw_rows or [])
        accepted = prepared["accepted"]
        rejected = prepared["rejected"]
        allowlist = prepared["allowlist"]
        vrf_links = prepared["vrf_links"]
        cases = prepared["cases"]
        placements = prepared["placements"]
        network_bindings = prepared["network_bindings"]
        placements_dict = prepared["placements_dict"]
        bindings_dict = prepared["bindings_dict"]
        gateway_by_subnet = prepared["gateway_by_subnet"]

        if (cfg.testsuite or cfg.testcase_ids) and not cases and not cfg.phased_testing:
            raise RuntimeError("No test cases matched the selected testsuite/testcases")

        vcenter_si = _connect_vcenter_if_needed(cfg)
        _serial_srv, _vsock_srv = _init_probe_servers(cfg)

        attempt_history: List[Dict[str, int]] = []
        cleanup_result: Optional[Dict[str, Any]] = None
        phase_summaries: List[Dict[str, Any]] = []

        if cfg.phased_testing:
            all_results, phase_summaries = _run_phased_testing(
                cfg=cfg,
                accepted=accepted,
                allowlist=allowlist,
                vrf_links=vrf_links,
                placements_dict=placements_dict,
                bindings_dict=bindings_dict,
                gateway_by_subnet=gateway_by_subnet,
                created_registry=created_registry,
                persist_registry=_persist_registry,
                all_vm_instances=all_vm_instances,
                effective_guest_ssh_password=effective_guest_ssh_password,
                vcenter_si=vcenter_si,
                check_cancel=_check_cancel,
            )
        else:
            all_results, attempt_history, cleanup_result = _run_classic_testing(
                cfg=cfg,
                accepted=accepted,
                cases=cases,
                placements_dict=placements_dict,
                bindings_dict=bindings_dict,
                gateway_by_subnet=gateway_by_subnet,
                created_registry=created_registry,
                persist_registry=_persist_registry,
                all_vm_instances=all_vm_instances,
                effective_guest_ssh_password=effective_guest_ssh_password,
                vcenter_si=vcenter_si,
                check_cancel=_check_cancel,
            )

        if (cfg.testsuite or cfg.testcase_ids) and not all_results:
            raise RuntimeError("No test cases matched the selected testsuite/testcases")

        result_payload = _build_result_payload(
            cfg=cfg,
            input_source=input_source,
            accepted=accepted,
            rejected=rejected,
            cases=cases,
            vrf_links=vrf_links,
            placements=placements,
            network_bindings=network_bindings,
            all_vm_instances=all_vm_instances,
            all_results=all_results,
            attempt_history=attempt_history,
            phase_summaries=phase_summaries,
            cleanup_result=cleanup_result,
            created_registry=created_registry,
        )
        if not cfg.execute_vcenter:
            plan = result_payload.get("Execution", {}).get("planned_vm_summary", {})
            planned = result_payload.get("Execution", {}).get("planned_vms", [])
            logger.info(
                "Dry-run VM plan: total=%s subnets=%s vms_per_subnet=%s",
                plan.get("count", 0),
                plan.get("subnets", 0),
                plan.get("vms_per_subnet", cfg.vms_per_subnet),
            )
            for item in planned[:200]:
                logger.info(
                    "Plan VM: name=%s dc=%s cluster=%s subnet=%s vlan=%s idx=%s",
                    item.get("vm_name", ""),
                    item.get("datacenter", ""),
                    item.get("cluster", ""),
                    item.get("subnet", ""),
                    item.get("vlan", ""),
                    item.get("vm_index", 0),
                )
            if len(planned) > 200:
                logger.info("Plan VM: ... truncated, showing first 200 of %s", len(planned))
        failed_results = [r for r in all_results if r.status != "pass"]
        success = result_payload["FinalStatus"] == "PASS"

        logger.info(
            "Run ID: %s | Status: %s | Cases: %s | Passed: %s | Failed: %s",
            cfg.run_id,
            result_payload["FinalStatus"],
            len(cases),
            sum(1 for r in all_results if r.status == "pass"),
            len(failed_results),
        )

        if result_cb is not None:
            result_cb(result_payload)
        _teardown_log_cb(_log_handler)
        return 0 if success else 1

    except RunCancelledError as exc:
        err = {"error": str(exc), "type": "Cancelled", "FinalStatus": "cancelled"}
        logger.warning("Run %s cancelled: %s", cfg.run_id, exc)
        if all_vm_instances and cfg.execute_vcenter:
            logger.info("Cleaning up %s VMs after cancel...", len(all_vm_instances))
            try:
                cleanup_vms(all_vm_instances, cfg, on_failure=True)
            except Exception as ce:
                logger.warning("Cleanup after cancel failed: %s", ce)
        if error_cb:
            error_cb(err)
        _teardown_log_cb(_log_handler)
        return 5
    except NotImplementedError as exc:
        err = {"error": str(exc), "hint": "Run without execute_vcenter=True first."}
        logger.error("Execution blocked: %s", exc)
        if error_cb: error_cb(err)
        _teardown_log_cb(_log_handler)
        return 3
    except RuntimeError as exc:
        err = {"error": str(exc), "type": "RuntimeError"}
        logger.error("Run failed: %s", exc)
        if error_cb: error_cb(err)
        _teardown_log_cb(_log_handler)
        return 2
    except Exception as exc:  # noqa: BLE001
        err = {"error": str(exc), "type": type(exc).__name__}
        logger.error("Unhandled error: %s", exc, exc_info=True)
        if error_cb: error_cb(err)
        _teardown_log_cb(_log_handler)
        return 4
    finally:
        if _vsock_srv is not None:
            _vsock_srv.stop()
        # Release pre-bound serial probe sockets regardless of outcome.
        if _serial_srv is not None:
            _serial_srv.close()


# ── Per-thread log routing ────────────────────────────────────────────────────

class _CallbackHandler(logging.Handler):
    """Routes log records to a callback (used to stream log lines to the UI queue)."""

    def __init__(self, cb: Callable[[str], None]):
        super().__init__()
        self._cb = cb
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._cb(self.format(record) + "\n")
        except Exception:
            pass


def _setup_log_cb(log_cb: Optional[Callable[[str], None]]) -> Optional["_CallbackHandler"]:
    if log_cb is None:
        return None
    handler = _CallbackHandler(log_cb)
    pkg_logger = logging.getLogger("nettest")
    pkg_logger.addHandler(handler)
    pkg_logger.setLevel(logging.INFO)
    return handler


def _teardown_log_cb(handler: Optional["_CallbackHandler"]) -> None:
    if handler is not None:
        logging.getLogger("nettest").removeHandler(handler)
