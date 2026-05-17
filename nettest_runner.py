#!/usr/bin/env python3
"""CLI entry-point for the vCenter network policy test runner.

All business logic lives in nettest/runner.py.
This file is responsible only for argument parsing, config loading,
and bridging into the handler.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Dict, TypeVar

_T = TypeVar("_T")

from nettest.runner import RunConfig, execute_run
from nettest.policy import ALL_PHASE_IDS

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="vCenter ICMP policy test runner")
    parser.add_argument("--config", default="", help="Path to JSON config file")
    parser.add_argument("--input", default="input.csv", help="Input file: txt/csv/xlsx")
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

    # ── Probe communication channel ───────────────────────────────────────────
    parser.add_argument(
        "--poll-method",
        choices=["guestinfo", "serial", "vsock"],
        default="guestinfo",
        help="How the controller communicates with test VMs: "
             "guestinfo (vmtools/extraConfig, default), "
             "serial (TCP-backed virtual serial port, no vmtools needed), "
             "vsock (AF_VSOCK over VMCI, no vmtools needed; controller must run on same ESXi host)",
    )
    parser.add_argument(
        "--serial-probe-host", default="",
        help="Controller IP reachable from ESXi for serial-port TCP backing. "
             "Auto-detected when empty (outbound IP toward vCenter).",
    )
    parser.add_argument(
        "--serial-base-port", type=int, default=10000,
        help="First TCP port for serial-port backing; each VM gets base+index.",
    )
    parser.add_argument(
        "--vsock-base-port", type=int, default=9000,
        help="VMCI/vsock port the guest connects to on VMADDR_CID_HOST (CID=2).",
    )

    # ── VM boot method ────────────────────────────────────────────────────────
    parser.add_argument(
        "--boot-method",
        choices=["ovf", "memboot"],
        default="memboot",
        help="VM boot method: "
             "memboot (diskless mini ISO boot — no VMDK, ~13 MB ISO uploaded once per cluster — default), "
             "ovf (deploy OVF seed VM per cluster + linked clones)",
    )
    parser.add_argument(
        "--memboot-iso-path", default="",
        help="Path to the mini nettest ISO (built with ovf/build_mini_iso.sh). "
             "Required when --boot-method=memboot. "
             "May also be a pre-staged datastore path like '[DatastoreName] nettest-iso/nettest-mini.iso'.",
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


def run() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    raw_cfg = _load_config(args.config)

    # CLI flags win; config fills unset defaults
    def _get(attr: str, default: _T) -> _T:
        val = getattr(args, attr, default)
        if val == default and attr in raw_cfg and raw_cfg[attr] not in ("", None):
            return raw_cfg[attr]  # type: ignore[return-value]
        return val  # type: ignore[return-value]

    cfg = RunConfig(
        run_id=_get("run_id", ""),
        input=_get("input", "input.csv"),
        probe_mode=_get("probe_mode", "dry-run"),
        probe_timeout_sec=_get("probe_timeout_sec", 1),
        retry_mode=_get("retry_mode", "failed-only"),
        max_retries=_get("max_retries", 1),
        allow_vrf=_get("allow_vrf", []),
        cluster_mngt_token=_get("cluster_mngt_token", "MNGT"),
        vm_prefix=_get("vm_prefix", "nettest"),
        vcenter_host=_get("vcenter_host", ""),
        vcenter_user=_get("vcenter_user", ""),
        vcenter_password=_get("vcenter_password", ""),
        datastore=_get("datastore", ""),
        resource_pool=_get("resource_pool", ""),
        ovf_path=_get("ovf_path", ""),
        random_seed=_get("random_seed", None),
        cleanup_on_failure=bool(_get("cleanup_on_failure", False)),
        execute_vcenter=bool(_get("execute_vcenter", False)),
        phased_testing=bool(_get("phased_testing", False)),
        vms_per_subnet=_get("vms_per_subnet", 1),
        max_vms_per_phase=_get("max_vms_per_phase", 20),
        vrf_links=_get("vrf_links", []),
        phases=_get("phases", ""),
        poll_method=_get("poll_method", "guestinfo"),
        serial_probe_host=_get("serial_probe_host", ""),
        serial_base_port=_get("serial_base_port", 10000),
        vsock_base_port=_get("vsock_base_port", 9000),
        boot_method=_get("boot_method", "memboot"),
        memboot_iso_path=_get("memboot_iso_path", ""),
    )

    import json as _json

    def _result_cb(result: Dict) -> None:
        print(f"__RESULT__:{_json.dumps(result, ensure_ascii=True)}", flush=True)

    def _error_cb(err: Dict) -> None:
        print(f"__ERROR__:{_json.dumps(err, ensure_ascii=True)}", flush=True)

    return execute_run(cfg, result_cb=_result_cb, error_cb=_error_cb)


if __name__ == "__main__":
    raise SystemExit(run())

