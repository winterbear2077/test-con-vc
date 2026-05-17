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
from nettest.provisioning import provision_test_vms, cleanup_vms
from nettest.policy import (
    ALL_PHASE_IDS,
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


def now_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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
        phases: str = "",
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
        self.phases = phases
        self.poll_method = poll_method
        self.serial_probe_host = serial_probe_host
        self.serial_base_port = serial_base_port
        self.vsock_base_port = vsock_base_port
        self.boot_method = boot_method
        self.memboot_iso_path = memboot_iso_path


def execute_run(
    cfg: RunConfig,
    log_cb: Optional[Callable[[str], None]] = None,
    result_cb: Optional[Callable[[Dict], None]] = None,
    error_cb: Optional[Callable[[Dict], None]] = None,
    objects_cb: Optional[Callable[[Dict], None]] = None,
) -> int:
    """Run the network test with the given RunConfig.

    log_cb(line)        — called for each log line (optional)
    result_cb(result)   — called with the full result dict on success
    error_cb(error)     — called with error dict on failure
    objects_cb(registry) — called whenever created-objects registry changes
    Returns process-style exit code: 0=PASS, 1=FAIL, 2=input-error,
    3=not-implemented, 4=unhandled-error.
    """
    _log_handler = _setup_log_cb(log_cb)
    logger.info("Starting run %s  probe_mode=%s  vcenter=%s",
                cfg.run_id, cfg.probe_mode, cfg.execute_vcenter)

    created_registry: Dict[str, List[str]] = {"vms": [], "nics": [], "tags": []}

    def _persist_registry() -> None:
        if objects_cb:
            objects_cb(created_registry)

    if cfg.rows is not None:
        raw_rows: List[Dict[str, str]] = cfg.rows
        input_source = "<database>"
    else:
        input_path = Path(cfg.input)
        if not input_path.exists():
            err = {"error": f"Input file not found: {cfg.input}", "type": "InputNotFound",
                   "FinalStatus": "error"}
            logger.error("Input file not found: %s", input_path)
            if error_cb:
                error_cb(err)
            return 2
        raw_rows = load_input(input_path)
        input_source = str(input_path)

    try:
        effective_guest_ssh_password = "nettest-alpine"

        if cfg.execute_vcenter:
            validate_vcenter_requirements(cfg)

        accepted, rejected = validate_and_normalize(raw_rows, cfg.cluster_mngt_token)
        allowlist = parse_allowlist(cfg.allow_vrf)

        vrf_links_config = [v for v in (cfg.vrf_links or []) if isinstance(v, dict)]
        vrf_links_cli    = [v for v in (cfg.vrf_links or []) if isinstance(v, str)]
        vrf_links = parse_vrf_links(vrf_links_config) + parse_vrf_links_cli(vrf_links_cli)

        cases = generate_test_cases(accepted, allowlist, vrf_links)
        placements = plan_cluster_placements(accepted, cfg)
        network_bindings = plan_vlan_bindings(accepted, cfg)

        placements_dict = {(p.datacenter, p.cluster): p for p in placements}
        bindings_dict   = {(b.datacenter, b.cluster, b.vlan): b for b in network_bindings}
        gateway_by_subnet = {r.subnet: r.gw for r in accepted}

        vcenter_si = None
        if cfg.execute_vcenter:
            from nettest.vcenter_utils import connect_vcenter
            vcenter_si = connect_vcenter(cfg)

        all_results:     List[TestResult] = []
        attempt_history: List[Dict]       = []
        all_vm_instances: List            = []
        cleanup_result = None
        phase_summaries: List[Dict] = []

        # ── Initialise probe server (serial / vsock) ──────────────────────────
        # Attach the server object to cfg so provisioning.py can reach it via
        # getattr(args, "_serial_server") / getattr(args, "_vsock_server").
        _serial_srv = None
        _vsock_srv  = None
        if cfg.execute_vcenter and cfg.probe_mode in ("in-guest", "in-guest-ping"):
            if cfg.poll_method == "serial":
                from nettest.serial_probe import SerialProbeServer, detect_controller_ip
                host = cfg.serial_probe_host or detect_controller_ip(cfg.vcenter_host)
                _serial_srv = SerialProbeServer(server_ip=host, base_port=cfg.serial_base_port)
                cfg._serial_server = _serial_srv  # type: ignore[attr-defined]
                logger.info("SerialProbeServer listening on %s starting from port %s",
                            host, cfg.serial_base_port)
            elif cfg.poll_method == "vsock":
                from nettest.vsock_probe import VsockProbeServer
                _vsock_srv = VsockProbeServer(port=cfg.vsock_base_port)
                _vsock_srv.start()
                cfg._vsock_server = _vsock_srv  # type: ignore[attr-defined]
                logger.info("VsockProbeServer listening on vsock port %s", cfg.vsock_base_port)

        # ── Phased testing ────────────────────────────────────────────────────
        if cfg.phased_testing:
            enabled_phases_list = None
            if cfg.phases:
                enabled_phases_list = [p.strip() for p in cfg.phases.split(",") if p.strip()]

            phases = generate_phased_cases(
                accepted, allowlist, vrf_links,
                vms_per_subnet=cfg.vms_per_subnet,
                max_vms_per_phase=cfg.max_vms_per_phase,
                enabled_phases=enabled_phases_list,
            )
            logger.info("Phased testing: %s phase(s) generated", len(phases))

            for phase in phases:
                if not phase.cases:
                    continue
                sep = "=" * 60
                logger.info("")
                logger.info(sep)
                logger.info("Phase [%s]: %s", phase.phase_id, phase.name)
                logger.info("  %s", phase.description)
                logger.info("  Cases: %s  VMs/subnet: %s", len(phase.cases), phase.vms_per_subnet)
                logger.info(sep)

                phase_subnets = {c.src_subnet for c in phase.cases} | {c.dst_subnet for c in phase.cases}
                phase_rows = [r for r in accepted if r.subnet in phase_subnets and r.mode == "vm-provisioned"]

                phase_vm_instances: List = []
                if cfg.probe_mode in ("in-guest", "in-guest-ping") and phase_rows:
                    logger.info("Provisioning %s VMs for phase...", len(phase_rows) * phase.vms_per_subnet)

                    def _on_vm_created_phase(moid: str) -> None:
                        created_registry["vms"].append(moid)
                        all_vm_instances_ref = all_vm_instances  # noqa: F841 captured by closure
                        _persist_registry()

                    phase_vm_instances = provision_test_vms(
                        phase_rows, placements_dict, bindings_dict, cfg, cfg.run_id,
                        cases=phase.cases, gateway_by_subnet=gateway_by_subnet,
                        vms_per_subnet=phase.vms_per_subnet,
                        on_vm_created=_on_vm_created_phase,
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
                )
                all_results = merge_retry_results(all_results, phase_results)

                ph_passed = sum(1 for r in phase_results if r.status == "pass")
                ph_failed = sum(1 for r in phase_results if r.status != "pass")
                logger.info("Phase results: %s passed, %s failed", ph_passed, ph_failed)
                phase_summaries.append({
                    "phase_id": phase.phase_id, "name": phase.name,
                    "batch_index": phase.batch_index,
                    "cases": len(phase.cases), "passed": ph_passed, "failed": ph_failed,
                })

                if ph_failed:
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

        # ── Non-phased (classic) testing ──────────────────────────────────────
        else:
            vm_instances: List = []
            if cfg.probe_mode in ("in-guest", "in-guest-ping"):
                vm_rows = [r for r in accepted if r.mode == "vm-provisioned"]
                if vm_rows:
                    logger.info("Provisioning test VMs...")

                    def _on_vm_created(moid: str) -> None:
                        created_registry["vms"].append(moid)
                        _persist_registry()

                    vm_instances = provision_test_vms(
                        vm_rows, placements_dict, bindings_dict, cfg, cfg.run_id,
                        cases=cases, gateway_by_subnet=gateway_by_subnet,
                        vms_per_subnet=cfg.vms_per_subnet,
                        on_vm_created=_on_vm_created,
                    )
                    all_vm_instances.extend(vm_instances)
                    logger.info("Provisioned %s test VMs", len(vm_instances))

            current_cases: List[TestCase] = list(cases)
            for attempt in range(cfg.max_retries + 1):
                if not current_cases:
                    break
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
                )
                all_results = merge_retry_results(all_results, attempt_results)
                failed = [r for r in all_results if r.status != "pass"]
                attempt_history.append({
                    "attempt": attempt,
                    "executed_cases": len(current_cases),
                    "failed_cases": len(failed),
                })
                if not failed or attempt >= cfg.max_retries:
                    break
                current_cases = select_retry_cases(cases, all_results, cfg.retry_mode)

            failed_results = [r for r in all_results if r.status != "pass"]
            if vm_instances:
                logger.info("Cleaning up test VMs...")
                cleanup_result = cleanup_vms(vm_instances, cfg, on_failure=bool(failed_results))
                logger.info("VM cleanup completed: %s", cleanup_result)

        failed_results = [r for r in all_results if r.status != "pass"]
        success = len(failed_results) == 0

        if _vsock_srv is not None:
            _vsock_srv.stop()

        result_payload = {
            "Plan": {
                "policy": "same-vrf-pass/cross-vrf-fail-unless-allowlist",
                "probe": "icmp-only",
                "probe_mode": cfg.probe_mode,
                "retry_mode": cfg.retry_mode,
                "max_retries": cfg.max_retries,
                "phased_testing": cfg.phased_testing,
                "vms_per_subnet": cfg.vms_per_subnet,
                "max_vms_per_phase": cfg.max_vms_per_phase,
                "vrf_links": [asdict(l) for l in vrf_links],
                "enabled_phases": (
                    [p.strip() for p in cfg.phases.split(",") if p.strip()]
                    if cfg.phases else ALL_PHASE_IDS
                ),
            },
            "ParsedInput": {
                "accepted_count": len(accepted),
                "rejected_count": len(rejected),
                "mngt_esxi_skipped": [asdict(r) for r in accepted if r.mode == "mngt-esxi"],
                "vm_provisioned":    [asdict(r) for r in accepted if r.mode == "vm-provisioned"],
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
                "selected_placements":  [asdict(p) for p in placements],
                "network_bindings":     [asdict(b) for b in network_bindings],
                "vm_source": {
                    "ovf_path": cfg.ovf_path,
                    "vm_prefix": cfg.vm_prefix,
                    "random_seed": cfg.random_seed,
                },
                "created_vms": [
                    {
                        "vm_name": vm.vm_name, "moid": vm.moid,
                        "datacenter": vm.datacenter, "cluster": vm.cluster,
                        "subnet": vm.subnet, "vlan": vm.vlan,
                        "ip_address": vm.ip_address, "mac_address": vm.mac_address,
                        "dpg_name": vm.dpg_name,
                    }
                    for vm in all_vm_instances
                ],
                "skipped_vm_provisioning_rows": [
                    {"subnet": r.subnet, "cluster": r.cluster, "reason": "cluster-is-mngt"}
                    for r in accepted if r.mode == "mngt-esxi"
                ],
            },
            "Phases":  phase_summaries if cfg.phased_testing else [],
            "Results": [asdict(r) for r in all_results],
            "Retry": {"mode": cfg.retry_mode, "history": attempt_history},
            "Cleanup": {
                "success_cleanup_performed":  success,
                "failure_cleanup_performed":  bool((not success) and cfg.cleanup_on_failure),
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

        logger.info("Run ID: %s | Status: %s | Cases: %s | Passed: %s | Failed: %s",
                    cfg.run_id, result_payload["FinalStatus"], len(cases),
                    sum(1 for r in all_results if r.status == "pass"), len(failed_results))

        if result_cb is not None:
            result_cb(result_payload)
        _teardown_log_cb(_log_handler)
        return 0 if success else 1

    except NotImplementedError as exc:
        err = {"error": str(exc), "hint": "Run without execute_vcenter=True first."}
        logger.error("Execution blocked: %s", exc)
        if error_cb: error_cb(err)
        _teardown_log_cb(_log_handler)
        return 3
    except RuntimeError as exc:
        err = {"error": str(exc), "type": "RuntimeError"}
        logger.error("Preflight failed: %s", exc)
        if error_cb: error_cb(err)
        _teardown_log_cb(_log_handler)
        return 2
    except Exception as exc:  # noqa: BLE001
        err = {"error": str(exc), "type": type(exc).__name__}
        logger.error("Unhandled error: %s", exc, exc_info=True)
        if error_cb: error_cb(err)
        _teardown_log_cb(_log_handler)
        return 4


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
