from __future__ import annotations

import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence

from nettest.models import TestCase, TestResult


def _ping_once(ip_addr: str, timeout_sec: int) -> bool:
    """Execute local ping command and return success."""
    # macOS ping: -W is wait time in ms; keep conservative timeout.
    wait_ms = max(timeout_sec * 1000, 1000)
    cmd = ["ping", "-c", "1", "-W", str(wait_ms), ip_addr]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc.returncode == 0


def _guest_ops_ping(
    si: Any,
    vm_obj: Any,
    target_ip: str,
    username: str,
    password: str,
    timeout_sec: int = 10,
) -> Optional[bool]:
    """Run ping inside a VM using VMware Guest Operations API.

    Communication goes entirely through the vCenter API channel —
    the controller does NOT need direct network access to the VM.

    Returns:
        True if ping succeeded, False if ping failed (ICMP blocked/unreachable),
        None if Guest Operations themselves failed (tools not ready, auth error).
    """
    try:
        from pyVmomi import vim  # type: ignore
    except ImportError:
        return None

    try:
        creds = vim.vm.guest.NamePasswordAuthentication(
            username=username,
            password=password,
        )
        pm = si.content.guestOperationsManager.processManager

        # Alpine path for ping
        ping_path = "/bin/ping"
        ping_args = f"-c 1 -W {max(timeout_sec, 1)} {target_ip}"

        prog_spec = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath=ping_path,
            arguments=ping_args,
            workingDirectory="/tmp",
        )
        pid = pm.StartProgramInGuest(vm_obj, creds, prog_spec)

        # Poll until process exits
        deadline = time.time() + timeout_sec + 5
        while time.time() < deadline:
            procs = pm.ListProcessesInGuest(vm_obj, creds, [pid])
            if procs and procs[0].exitCode is not None:
                return procs[0].exitCode == 0
            time.sleep(1)

        # Timed out waiting for process exit — kill it and treat as unknown
        try:
            pm.TerminateProcessInGuest(vm_obj, creds, pid)
        except Exception:
            pass
        return None

    except Exception as exc:
        print(f"Guest Ops ping failed (-> {target_ip}): {exc}")
        return None


def _find_vm_by_moid(si: Any, moid: str) -> Optional[Any]:
    """Retrieve a VirtualMachine managed object by its MOID."""
    try:
        from pyVmomi import vim  # type: ignore
    except ImportError:
        return None
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        for vm in view.view:
            if str(getattr(vm, "_moId", "")) == moid:
                return vm
    finally:
        view.Destroy()
    return None


def _ssh_ping_from_vm(
    vm_ip: str,
    target_ip: str,
    username: str = "root",
    password: str = "",
    timeout_sec: int = 5,
) -> Optional[bool]:
    """SSH fallback: ping from a VM when Guest Ops are unavailable.

    Only usable when the controller has direct network access to the VM.
    """
    try:
        import paramiko
    except ImportError:
        return None

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(vm_ip, username=username, password=password, timeout=timeout_sec, allow_agent=False, look_for_keys=False)
        cmd = f"ping -c 1 -W {timeout_sec} {target_ip}"
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout_sec + 5)
        exit_code = stdout.channel.recv_exit_status()
        ssh.close()
        return exit_code == 0
    except Exception as exc:
        print(f"SSH ping failed ({vm_ip} -> {target_ip}): {exc}")
        return None


def run_icmp_checks(
    cases: Sequence[TestCase],
    execute_vcenter: bool,
    probe_mode: str,
    gateway_by_subnet: Dict[str, str],
    timeout_sec: int,
    vm_instances: Optional[List[Any]] = None,
    subnet_by_vm: Optional[Dict[str, str]] = None,
    guest_ssh_user: str = "root",
    guest_ssh_password: str = "",
    vcenter_si: Any = None,
) -> List[TestResult]:
    """Execute ICMP connectivity checks using the specified probe mode.

    Args:
        cases: Test cases to execute.
        execute_vcenter: Whether in vCenter execute mode.
        probe_mode: Probe mode (dry-run, controller-gateway, in-guest, in-guest-ping).
        gateway_by_subnet: Mapping of subnet -> gateway IP.
        timeout_sec: Timeout for each ping attempt.
        vm_instances: Optional list of provisioned VMInstance objects.
        subnet_by_vm: Unused; kept for backwards compatibility.
        guest_ssh_user: Guest OS username.
        guest_ssh_password: Guest OS password.
        vcenter_si: Open pyVmomi ServiceInstance; required for Guest Ops probe.

    Returns:
        List of TestResult objects.
    """

    if probe_mode == "dry-run":
        return [
            TestResult(
                src_subnet=c.src_subnet,
                dst_subnet=c.dst_subnet,
                expected=c.expected,
                actual=c.expected,
                status="pass",
                reason="dry-run-expected-as-actual",
            )
            for c in cases
        ]

    if probe_mode == "controller-gateway":
        # Ping from local controller to target gateways
        ping_cache: Dict[str, bool] = {}
        results: List[TestResult] = []
        for case in cases:
            dst_gw = gateway_by_subnet.get(case.dst_subnet, "")
            if not dst_gw:
                actual = "UNKNOWN"
                status = "fail"
                reason = "missing-dst-gateway"
            else:
                if dst_gw not in ping_cache:
                    ping_cache[dst_gw] = _ping_once(dst_gw, timeout_sec)
                ok = ping_cache[dst_gw]
                actual = "PASS" if ok else "FAIL"
                status = "pass" if actual == case.expected else "fail"
                reason = "controller-icmp-to-dst-gw"

            results.append(TestResult(
                src_subnet=case.src_subnet,
                dst_subnet=case.dst_subnet,
                expected=case.expected,
                actual=actual,
                status=status,
                reason=reason,
                phase=getattr(case, "phase", "intra-vrf"),
                src_vm_index=getattr(case, "src_vm_index", 0),
                dst_vm_index=getattr(case, "dst_vm_index", 0),
            ))
        return results

    if probe_mode in ("in-guest", "in-guest-ping"):
        if not vm_instances:
            if not execute_vcenter:
                return [
                    TestResult(
                        src_subnet=c.src_subnet,
                        dst_subnet=c.dst_subnet,
                        expected=c.expected,
                        actual=c.expected,
                        status="pass",
                        reason="dry-run-in-guest-expected-as-actual",
                        phase=getattr(c, "phase", "intra-vrf"),
                        src_vm_index=getattr(c, "src_vm_index", 0),
                        dst_vm_index=getattr(c, "dst_vm_index", 0),
                    )
                    for c in cases
                ]
            return [
                TestResult(
                    src_subnet=c.src_subnet,
                    dst_subnet=c.dst_subnet,
                    expected=c.expected,
                    actual="UNKNOWN",
                    status="fail",
                    reason="in-guest-probe-no-vm-instances",
                    phase=getattr(c, "phase", "intra-vrf"),
                    src_vm_index=getattr(c, "src_vm_index", 0),
                    dst_vm_index=getattr(c, "dst_vm_index", 0),
                )
                for c in cases
            ]

        # Build multi-VM index: subnet -> list of VMInstances ordered by vm_name
        from collections import defaultdict
        _subnet_vms: Dict[str, List[Any]] = defaultdict(list)
        for vm in vm_instances:
            _subnet_vms[vm.subnet].append(vm)
        for _s in _subnet_vms:
            _subnet_vms[_s].sort(key=lambda v: v.vm_name)

        if not execute_vcenter:
            return [
                TestResult(
                    src_subnet=c.src_subnet,
                    dst_subnet=c.dst_subnet,
                    expected=c.expected,
                    actual=c.expected,
                    status="pass",
                    reason="dry-run-in-guest-with-vms-expected-as-actual",
                    phase=getattr(c, "phase", "intra-vrf"),
                    src_vm_index=getattr(c, "src_vm_index", 0),
                    dst_vm_index=getattr(c, "dst_vm_index", 0),
                )
                for c in cases
            ]

        use_guest_ops = vcenter_si is not None

        results: List[TestResult] = []
        for case in cases:
            src_vm_idx  = getattr(case, "src_vm_index", 0)
            dst_vm_idx  = getattr(case, "dst_vm_index", 0)
            case_phase  = getattr(case, "phase", "intra-vrf")
            is_intra    = (case.src_subnet == case.dst_subnet)

            # ── Resolve source VM ─────────────────────────────────────────────
            src_vms = _subnet_vms.get(case.src_subnet, [])
            src_vm  = src_vms[src_vm_idx] if src_vm_idx < len(src_vms) else (src_vms[0] if src_vms else None)

            if not src_vm:
                results.append(TestResult(
                    src_subnet=case.src_subnet, dst_subnet=case.dst_subnet,
                    expected=case.expected, actual="UNKNOWN", status="fail",
                    reason="in-guest-probe-src-vm-not-found",
                    phase=case_phase, src_vm_index=src_vm_idx, dst_vm_index=dst_vm_idx,
                ))
                continue

            if not str(getattr(src_vm, "ip_address", "")):
                results.append(TestResult(
                    src_subnet=case.src_subnet, dst_subnet=case.dst_subnet,
                    expected=case.expected, actual="UNKNOWN", status="fail",
                    reason="in-guest-probe-src-vm-ip-missing",
                    phase=case_phase, src_vm_index=src_vm_idx, dst_vm_index=dst_vm_idx,
                ))
                continue

            # ── Resolve probe target IP ───────────────────────────────────────
            if is_intra:
                # Intra-subnet: target is the dst VM's IP
                dst_vms    = _subnet_vms.get(case.dst_subnet, [])
                dst_vm_obj = dst_vms[dst_vm_idx] if dst_vm_idx < len(dst_vms) else None
                probe_ip   = dst_vm_obj.ip_address if dst_vm_obj else ""
                if not probe_ip:
                    results.append(TestResult(
                        src_subnet=case.src_subnet, dst_subnet=case.dst_subnet,
                        expected=case.expected, actual="UNKNOWN", status="fail",
                        reason="in-guest-probe-dst-vm-ip-missing",
                        phase=case_phase, src_vm_index=src_vm_idx, dst_vm_index=dst_vm_idx,
                    ))
                    continue
            else:
                # Cross-subnet: target is the dst gateway
                probe_ip = gateway_by_subnet.get(case.dst_subnet, "")
                if not probe_ip:
                    results.append(TestResult(
                        src_subnet=case.src_subnet, dst_subnet=case.dst_subnet,
                        expected=case.expected, actual="UNKNOWN", status="fail",
                        reason="in-guest-probe-missing-dst-gateway",
                        phase=case_phase, src_vm_index=src_vm_idx, dst_vm_index=dst_vm_idx,
                    ))
                    continue

            ok: Optional[bool] = None
            probe_reason = "unknown"

            # Primary: guestinfo results written by netprobe.start inside the VM.
            probe_results = getattr(src_vm, "probe_results", None) or {}
            if probe_results:
                result_str = probe_results.get(probe_ip)
                if result_str == "PASS":
                    ok = True
                elif result_str == "FAIL":
                    ok = False
                probe_reason = "guestinfo-icmp-from-src-vm"

            # Fallback: Guest Ops ICMP when guestinfo gave no result.
            # SSH is not used (not available on the Alpine OVF build).
            if ok is None and use_guest_ops:
                vm_obj = _find_vm_by_moid(vcenter_si, src_vm.moid)
                if vm_obj is not None:
                    ok = _guest_ops_ping(
                        si=vcenter_si, vm_obj=vm_obj, target_ip=probe_ip,
                        username=guest_ssh_user, password=guest_ssh_password,
                        timeout_sec=timeout_sec,
                    )
                    probe_reason = "guest-ops-icmp-from-src-vm"
                else:
                    print(f"Warning: VM {src_vm.moid} not found via vCenter for Guest Ops ping")

            if ok is None:
                actual = "UNKNOWN"
                status = "fail"
                reason = probe_reason if probe_reason != "unknown" else "in-guest-probe-unavailable"
            else:
                actual = "PASS" if ok else "FAIL"
                status = "pass" if actual == case.expected else "fail"
                reason = probe_reason

            results.append(TestResult(
                src_subnet=case.src_subnet,
                dst_subnet=case.dst_subnet,
                expected=case.expected,
                actual=actual,
                status=status,
                reason=reason,
                phase=case_phase,
                src_vm_index=src_vm_idx,
                dst_vm_index=dst_vm_idx,
            ))

        return results

    # Unsupported probe mode
    return [
        TestResult(
            src_subnet=c.src_subnet,
            dst_subnet=c.dst_subnet,
            expected=c.expected,
            actual="UNKNOWN",
            status="fail",
            reason=f"unsupported-probe-mode:{probe_mode}",
            phase=getattr(c, "phase", "intra-vrf"),
            src_vm_index=getattr(c, "src_vm_index", 0),
            dst_vm_index=getattr(c, "dst_vm_index", 0),
        )
        for c in cases
    ]
