#!/usr/bin/env python3
"""vCenter network policy test runner entrypoint."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="vCenter ICMP policy test runner")
    parser.add_argument("--config", default="", help="Path to JSON config file")
    parser.add_argument("--input", default="input.csv", help="Input file: txt/csv/xlsx")
    parser.add_argument("--output-dir", default="artifacts", help="Output directory")
    parser.add_argument("--retry-mode", choices=["all", "failed-only"], default="failed-only")
    parser.add_argument("--max-retries", type=int, default=1, help="Retry attempts after first run")
    parser.add_argument(
        "--probe-mode",
        choices=["dry-run", "controller-gateway", "in-guest", "in-guest-ping"],
        default="dry-run",
        help="ICMP probe mode",
    )
    parser.add_argument("--probe-timeout-sec", type=int, default=1, help="ICMP timeout per ping")
    parser.add_argument(
        "--allow-vrf",
        action="append",
        default=[],
        help="Cross-VRF allowlist pair, format VRF_A:VRF_B (repeatable)",
    )
    parser.add_argument("--cluster-mngt-token", default="MNGT", help="Cluster token for ESXi management rows")
    parser.add_argument("--vm-prefix", default="nettest", help="Test VM name prefix")

    parser.add_argument("--vcenter-host", default="")
    parser.add_argument("--vcenter-user", default="")
    parser.add_argument("--vcenter-password", default="")
    parser.add_argument("--datastore", default="")
    parser.add_argument("--folder", default="")
    parser.add_argument("--resource-pool", default="")
    parser.add_argument("--ovf-path", default="", help="Path to the OVF template with pre-installed open-vm-tools")
    parser.add_argument("--random-seed", type=int, default=None, help="Seed for deterministic random host/datastore selection")

    parser.add_argument("--cleanup-on-failure", action="store_true", help="Delete created test resources even when run fails")
    parser.add_argument("--execute-vcenter", action="store_true", help="Enable real vCenter actions")

    # ── Phased testing ────────────────────────────────────────────────────────
    parser.add_argument(
        "--phased-testing", action="store_true",
        help="Run tests in sequential phases (intra-subnet → intra-VRF → cross-VRF), "
             "provisioning and cleaning up VMs between each phase",
    )
    parser.add_argument(
        "--vms-per-subnet", type=int, default=1,
        help="Number of VMs to provision per subnet (use >=2 to enable intra-subnet phase)",
    )
    parser.add_argument(
        "--max-vms-per-phase", type=int, default=20,
        help="Maximum VMs per phase; large phases are automatically batched",
    )
    parser.add_argument(
        "--vrf-links", action="append", default=[],
        help="VRF-to-VRF connectivity rule: FROM:TO (PASS) or FROM:TO:FAIL (repeatable)",
    )
    parser.add_argument(
        "--phases", default="",
        help="Comma-separated phase IDs to run (default: all). "
             f"Choices: {','.join(ALL_PHASE_IDS)}",
    )
    parser.add_argument(
        "--run-id", default="",
        help="Override the auto-generated run ID (used by the web UI to keep directory names in sync)",
    )
    return parser.parse_args()


def _load_config(config_path: str) -> Dict:
    if not config_path:
        return {}
    path = Path(config_path)
    if not path.exists():
        raise RuntimeError(f"Config file not found: {config_path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Config file must be a JSON object")
    return data


def _apply_config(args: argparse.Namespace, config: Dict) -> argparse.Namespace:
    # CLI always wins; config fills defaults.
    default_like = {
        "input": "input.csv",
        "output_dir": "artifacts",
        "retry_mode": "failed-only",
        "max_retries": 1,
        "probe_mode": "dry-run",
        "probe_timeout_sec": 1,
        "allow_vrf": [],
        "cluster_mngt_token": "MNGT",
        "vm_prefix": "nettest",
        "vcenter_host": "",
        "vcenter_user": "",
        "vcenter_password": "",
        "datastore": "",
        "folder": "",
        "resource_pool": "",
        "ovf_path": "",
        "random_seed": None,
        "cleanup_on_failure": False,
        "execute_vcenter": False,
        # phased testing
        "phased_testing": False,
        "vms_per_subnet": 1,
        "max_vms_per_phase": 20,
        "vrf_links": [],
        "phases": "",
    }

    for key, default_val in default_like.items():
        current = getattr(args, key, default_val)
        if current == default_val and key in config:
            config_val = config[key]
            # Don't let an empty string in config clobber a meaningful non-empty default
            # (e.g. "input": "" should not override the default "input.txt").
            if config_val == "" and default_val != "":
                continue
            setattr(args, key, config_val)

    return args


def now_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: Dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")


def run() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    config = _load_config(args.config)
    args = _apply_config(args, config)
    run_id = args.run_id if getattr(args, "run_id", "") else now_run_id()
    output_dir = Path(args.output_dir) / run_id
    ensure_dir(output_dir)

    created_registry_path = output_dir / "created-objects.json"
    created_registry: Dict[str, List[str]] = {"vms": [], "nics": [], "tags": []}
    write_json(created_registry_path, created_registry)

    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("Input file not found: %s", input_path)
        return 2

    try:
        effective_guest_ssh_password = "nettest-alpine"

        if args.execute_vcenter:
            validate_vcenter_requirements(args)

        raw_rows = load_input(input_path)
        accepted, rejected = validate_and_normalize(raw_rows, args.cluster_mngt_token)
        allowlist = parse_allowlist(args.allow_vrf)

        # Merge vrf_links from config (list of dicts) and --vrf-links CLI (list of strings)
        vrf_links_config = [v for v in (args.vrf_links or []) if isinstance(v, dict)]
        vrf_links_cli    = [v for v in (args.vrf_links or []) if isinstance(v, str)]
        vrf_links = parse_vrf_links(vrf_links_config) + parse_vrf_links_cli(vrf_links_cli)

        cases = generate_test_cases(accepted, allowlist, vrf_links)

        placements = plan_cluster_placements(accepted, args)
        network_bindings = plan_vlan_bindings(accepted, args)

        # Convert placement and binding lists to dicts for provisioning
        placements_dict = {
            (p.datacenter, p.cluster): p
            for p in placements
        }
        bindings_dict = {
            (b.datacenter, b.cluster, b.vlan): b
            for b in network_bindings
        }

        gateway_by_subnet = {r.subnet: r.gw for r in accepted}

        # Open a persistent vCenter session for provision + probe + cleanup so
        # the same ServiceInstance is available for Guest Operations API probing.
        vcenter_si = None
        if args.execute_vcenter:
            from nettest.vcenter_utils import connect_vcenter
            vcenter_si = connect_vcenter(args)

        all_results: List[TestResult] = []
        attempt_history: List[Dict[str, int]] = []
        all_vm_instances: List = []
        cleanup_result = None
        phase_summaries: List[Dict] = []

        # ── Phased testing ─────────────────────────────────────────────────────
        if args.phased_testing:
            enabled_phases_list = None
            if args.phases:
                enabled_phases_list = [p.strip() for p in str(args.phases).split(",") if p.strip()]

            phases = generate_phased_cases(
                accepted, allowlist, vrf_links,
                vms_per_subnet=args.vms_per_subnet,
                max_vms_per_phase=args.max_vms_per_phase,
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

                # Collect subnets needed by this phase
                phase_subnets = set()
                for c in phase.cases:
                    phase_subnets.add(c.src_subnet)
                    phase_subnets.add(c.dst_subnet)
                phase_rows = [r for r in accepted
                              if r.subnet in phase_subnets and r.mode == "vm-provisioned"]

                phase_vm_instances: List = []
                if args.probe_mode in ("in-guest", "in-guest-ping") and phase_rows:
                    total_vms = len(phase_rows) * phase.vms_per_subnet
                    logger.info("Provisioning %s VMs for phase...", total_vms)
                    phase_vm_instances = provision_test_vms(
                        phase_rows, placements_dict, bindings_dict, args, run_id,
                        cases=phase.cases, gateway_by_subnet=gateway_by_subnet,
                        vms_per_subnet=phase.vms_per_subnet,
                    )
                    all_vm_instances.extend(phase_vm_instances)
                    created_registry["vms"].extend(vm.moid for vm in phase_vm_instances)
                    write_json(created_registry_path, created_registry)
                    logger.info("Provisioned %s VMs", len(phase_vm_instances))

                phase_results = run_icmp_checks(
                    phase.cases,
                    execute_vcenter=args.execute_vcenter,
                    probe_mode=args.probe_mode,
                    gateway_by_subnet=gateway_by_subnet,
                    timeout_sec=args.probe_timeout_sec,
                    vm_instances=phase_vm_instances,
                    guest_ssh_user="root",
                    guest_ssh_password=effective_guest_ssh_password,
                    vcenter_si=vcenter_si,
                )
                all_results = merge_retry_results(all_results, phase_results)

                ph_passed = sum(1 for r in phase_results if r.status == "pass")
                ph_failed = sum(1 for r in phase_results if r.status != "pass")
                logger.info("Phase results: %s passed, %s failed", ph_passed, ph_failed)
                phase_summaries.append({
                    "phase_id":    phase.phase_id,
                    "name":        phase.name,
                    "batch_index": phase.batch_index,
                    "cases":       len(phase.cases),
                    "passed":      ph_passed,
                    "failed":      ph_failed,
                })

                if ph_failed:
                    # Phase failed — apply retention policy then stop
                    if phase_vm_instances:
                        if args.cleanup_on_failure:
                            logger.info("Phase failed — cleanup_on_failure set, cleaning up %s VMs...",
                                        len(phase_vm_instances))
                            cleanup_vms(phase_vm_instances, args, on_failure=True)
                        else:
                            logger.warning("Phase failed — retaining %s VMs for troubleshooting (use "
                                           "--cleanup-on-failure to override).", len(phase_vm_instances))
                    logger.warning("Stopping phased test run after phase [%s] failure.", phase.phase_id)
                    break

                # Phase passed — always cleanup between phases to limit live VM count
                if phase_vm_instances:
                    logger.info("Cleaning up %s phase VMs...", len(phase_vm_instances))
                    cleanup_vms(phase_vm_instances, args, on_failure=False)

        # ── Non-phased (classic) testing ───────────────────────────────────────
        else:
            vm_instances: List = []
            if args.probe_mode in ("in-guest", "in-guest-ping"):
                vm_rows = [r for r in accepted if r.mode == "vm-provisioned"]
                if vm_rows:
                    logger.info("Provisioning test VMs...")
                    vm_instances = provision_test_vms(
                        vm_rows, placements_dict, bindings_dict, args, run_id,
                        cases=cases, gateway_by_subnet=gateway_by_subnet,
                        vms_per_subnet=args.vms_per_subnet,
                    )
                    all_vm_instances.extend(vm_instances)
                    created_registry["vms"].extend(vm.moid for vm in vm_instances)
                    write_json(created_registry_path, created_registry)
                    logger.info("Provisioned %s test VMs", len(vm_instances))

            current_cases: List[TestCase] = list(cases)
            for attempt in range(args.max_retries + 1):
                if not current_cases:
                    break
                attempt_results = run_icmp_checks(
                    current_cases,
                    execute_vcenter=args.execute_vcenter,
                    probe_mode=args.probe_mode,
                    gateway_by_subnet=gateway_by_subnet,
                    timeout_sec=args.probe_timeout_sec,
                    vm_instances=vm_instances,
                    guest_ssh_user="root",
                    guest_ssh_password=effective_guest_ssh_password,
                    vcenter_si=vcenter_si,
                )
                all_results = merge_retry_results(all_results, attempt_results)
                failed = [r for r in all_results if r.status != "pass"]
                attempt_history.append({
                    "attempt": attempt,
                    "executed_cases": len(current_cases),
                    "failed_cases": len(failed),
                })
                if not failed or attempt >= args.max_retries:
                    break
                current_cases = select_retry_cases(cases, all_results, args.retry_mode)

            failed_results = [r for r in all_results if r.status != "pass"]
            success_classic = len(failed_results) == 0

            if vm_instances:
                logger.info("Cleaning up test VMs...")
                cleanup_result = cleanup_vms(vm_instances, args, on_failure=not success_classic)
                logger.info("VM cleanup completed: %s", cleanup_result)

        failed_results = [r for r in all_results if r.status != "pass"]
        success = len(failed_results) == 0

        parsed_input = {
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "mngt_esxi_skipped": [asdict(r) for r in accepted if r.mode == "mngt-esxi"],
            "vm_provisioned": [asdict(r) for r in accepted if r.mode == "vm-provisioned"],
            "rejected": rejected,
        }

        execution = {
            "input_file": str(input_path),
            "execute_vcenter": args.execute_vcenter,
            "placement_policy": "for each cluster without DRS, randomly pick a connected host, then randomly pick a datastore accessible by that host",
            "placement": {
                "datastore_preference": args.datastore,
                "folder": "datacenter-vm-root",
                "resource_pool": args.resource_pool,
            },
            "selected_placements": [asdict(p) for p in placements],
            "network_bindings": [asdict(b) for b in network_bindings],
            "vm_source": {
                "ovf_path": args.ovf_path,
                "vm_prefix": args.vm_prefix,
                "random_seed": args.random_seed,
            },
            "created_vms": [
                {
                    "vm_name": vm.vm_name,
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
            "created_registry_path": str(created_registry_path),
            "skipped_vm_provisioning_rows": [
                {
                    "subnet": r.subnet,
                    "cluster": r.cluster,
                    "reason": "cluster-is-mngt",
                }
                for r in accepted
                if r.mode == "mngt-esxi"
            ],
        }

        cleanup = {
            "success_cleanup_performed": success,
            "failure_cleanup_performed": bool((not success) and args.cleanup_on_failure),
            "cleanup_on_failure_requested": args.cleanup_on_failure,
            "retained_resources": [] if success else created_registry,
            "vm_cleanup": cleanup_result or {},
        }

        result_payload = {
            "Plan": {
                "policy": "same-vrf-pass/cross-vrf-fail-unless-allowlist",
                "probe": "icmp-only",
                "probe_mode": args.probe_mode,
                "retry_mode": args.retry_mode,
                "max_retries": args.max_retries,
                "phased_testing": args.phased_testing,
                "vms_per_subnet": args.vms_per_subnet,
                "max_vms_per_phase": args.max_vms_per_phase,
                "vrf_links": [asdict(l) for l in vrf_links],
                "enabled_phases": (
                    [p.strip() for p in str(args.phases).split(",") if p.strip()]
                    if args.phases else ALL_PHASE_IDS
                ),
            },
            "ParsedInput": parsed_input,
            "ExpectedPolicy": summarize_expected(cases),
            "Execution": execution,
            "Phases": phase_summaries if args.phased_testing else [],
            "Results": [asdict(r) for r in all_results],
            "Retry": {
                "mode": args.retry_mode,
                "history": attempt_history,
            },
            "Cleanup": cleanup,
            "NextSteps": [
                "Provide vCenter placement details and use --execute-vcenter for real provisioning.",
                "Review retained resources when failures occur.",
            ],
            "FinalStatus": "PASS" if success else "FAIL",
        }

        print(f"__RESULT__:{json.dumps(result_payload, ensure_ascii=True)}", flush=True)
        summary_lines = [
            f"Run ID: {run_id}",
            f"Input: {input_path}",
            f"Accepted rows: {parsed_input['accepted_count']}",
            f"MNGT skipped rows: {len(parsed_input['mngt_esxi_skipped'])}",
            f"Test cases: {len(cases)}",
            f"Phased testing: {'yes (' + str(len(phase_summaries)) + ' phases)' if args.phased_testing else 'no'}",
            f"Final status: {result_payload['FinalStatus']}",
        ]

        for _line in summary_lines:
            logger.info(_line)
        return 0 if success else 1

    except NotImplementedError as exc:
        error_payload = {
            "error": str(exc),
            "hint": "Run without --execute-vcenter to validate parsing/policy flow first.",
        }
        print(f"__ERROR__:{json.dumps(error_payload, ensure_ascii=True)}", flush=True)
        logger.error("Execution blocked: %s", exc)
        return 3
    except RuntimeError as exc:
        error_payload = {
            "error": str(exc),
            "type": "RuntimeError",
        }
        print(f"__ERROR__:{json.dumps(error_payload, ensure_ascii=True)}", flush=True)
        logger.error("Preflight failed: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        error_payload = {
            "error": str(exc),
            "type": type(exc).__name__,
        }
        print(f"__ERROR__:{json.dumps(error_payload, ensure_ascii=True)}", flush=True)
        logger.error("Unhandled error: %s", exc)
        return 4


if __name__ == "__main__":
    raise SystemExit(run())
