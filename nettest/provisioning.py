"""
VM provisioning and lifecycle management for network tests.
Handles creation, configuration, and cleanup of test VMs.
"""

from __future__ import annotations

import os
import time
import ipaddress
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from nettest.vcenter_utils import vcenter_session, wait_for_task
from nettest.ovf_deploy import (
    deploy_ovf_vm, write_probe_guestinfo, write_probe_ready, poll_probe_results,
    take_vm_snapshot, clone_vm_linked,
)


@dataclass
class VMInstance:
    """Represents a provisioned test VM."""
    datacenter: str
    cluster: str
    vm_name: str
    moid: str  # Managed Object ID
    subnet: str
    vlan: int
    ip_address: str
    mac_address: str
    dpg_name: str
    selection_policy: str
    base_iso_path: str = field(default="")   # Unused in OVF mode; kept for schema compatibility

    vmtools_ready: bool = field(default=False)  # True when VMware Tools reported guestToolsRunning
    probe_results: Dict[str, str] = field(default_factory=dict)  # {target_ip: "PASS"|"FAIL"} from guestinfo


def _wait_for_vmtools(vm_obj: Any, timeout_sec: int = 300) -> bool:
    """Poll vm.guest.toolsRunningStatus via vCenter API until guestToolsRunning or timeout.

    This does NOT require the controller to have direct network access to the VM —
    all communication goes through the vCenter API channel.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        guest = getattr(vm_obj, "guest", None)
        status = str(getattr(guest, "toolsRunningStatus", "") or "")
        if status == "guestToolsRunning":
            return True
        time.sleep(5)
    return False


def _wait_for_ssh(
    ip: str,
    username: str,
    password: str,
    timeout_sec: int = 180,
) -> bool:
    """Poll *ip*:22 until SSH authentication succeeds or *timeout_sec* elapses.

    Used as a fallback when VMware Tools is not available and the controller
    has direct network access to the VM.
    """
    try:
        import paramiko  # type: ignore
    except ImportError:
        return False

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                ip,
                username=username,
                password=password,
                timeout=5,
                allow_agent=False,
                look_for_keys=False,
            )
            ssh.close()
            return True
        except Exception:
            time.sleep(5)
    return False


def _allocate_ip_from_subnet(subnet_str: str, gateway_str: str, index: int = 0) -> str:
    """
    Allocate an IP address from a subnet, avoiding the gateway IP.
    
    Args:
        subnet_str: Subnet in CIDR notation or just network address (e.g., "172.20.20.0")
        gateway_str: Gateway IP address (e.g., "172.20.20.254")
        index: Index for multiple allocations in the same subnet (0-based)
    
    Returns:
        Allocated IP address as string.
    """
    try:
        # Parse subnet - handle both "172.20.20.0" and "172.20.20.0/24" formats
        if "/" not in subnet_str:
            # Assume /24 for Class C private networks
            network = ipaddress.ip_network(f"{subnet_str}/24", strict=False)
        else:
            network = ipaddress.ip_network(subnet_str, strict=False)
        
        gateway_ip = ipaddress.ip_address(gateway_str)
        
        # Allocate IPs starting from network address + 10 + index
        # This leaves room for network address, reserved IPs, and gateway
        allocated_ips: List[str] = []
        for ip in network.hosts():  # Excludes network and broadcast
            if ip != gateway_ip:
                allocated_ips.append(str(ip))
        
        if not allocated_ips:
            # Fallback if no valid IPs (shouldn't happen)
            return str(network.network_address + 10 + index)  # type: ignore
        
        # Start from the 10th usable IP to avoid low IPs
        start_idx = min(9, len(allocated_ips) - 1)
        target_idx = start_idx + index
        
        if target_idx >= len(allocated_ips):
            # Wrap around or use last available
            target_idx = len(allocated_ips) - 1
        
        return allocated_ips[target_idx]
    
    except Exception as exc:
        print(f"Warning: Failed to allocate IP from {subnet_str} (gw: {gateway_str}): {exc}")
        # Fallback: return a placeholder
        return f"10.0.0.{index + 1}"


def _find_datacenter(content: Any, datacenter_name: str, vim: Any) -> Any:
    for entity in getattr(content.rootFolder, "childEntity", []):
        if isinstance(entity, vim.Datacenter) and str(entity.name) == datacenter_name:
            return entity
    return None


def _find_cluster_in_datacenter(dc_obj: Any, cluster_name: str, vim: Any) -> Any:
    for entity in getattr(getattr(dc_obj, "hostFolder", None), "childEntity", []):
        if isinstance(entity, vim.ClusterComputeResource) and str(entity.name) == cluster_name:
            return entity
    return None


def _find_host_in_cluster(cluster_obj: Any, host_name: str) -> Any:
    for host in getattr(cluster_obj, "host", []):
        if str(getattr(host, "name", "")) == host_name:
            return host
    return None


def _find_datastore_in_dc(dc_obj: Any, datastore_name: str) -> Any:
    for ds in getattr(dc_obj, "datastore", []):
        if str(getattr(ds, "name", "")) == datastore_name:
            return ds
    return None


def _iter_folders(folder: Any, vim: Any):
    yield folder
    for child in getattr(folder, "childEntity", []):
        if isinstance(child, vim.Folder):
            yield from _iter_folders(child, vim)


def _find_vm_folder(dc_obj: Any, folder_name: str, vim: Any) -> Any:
    if not folder_name:
        return dc_obj.vmFolder
    for folder in _iter_folders(dc_obj.vmFolder, vim):
        if str(getattr(folder, "name", "")) == folder_name:
            return folder
    return None


def _iter_resource_pools(rp: Any):
    yield rp
    for child in getattr(rp, "resourcePool", []):
        yield from _iter_resource_pools(child)


def _find_resource_pool(cluster_obj: Any, pool_name: str) -> Any:
    root = getattr(cluster_obj, "resourcePool", None)
    if root is None:
        return None
    if not pool_name:
        return root
    for rp in _iter_resource_pools(root):
        if str(getattr(rp, "name", "")) == pool_name:
            return rp
    return None


def _find_dpg_and_switch_uuid(content: Any, dpg_key: str, vim: Any) -> tuple[Any, str]:
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.dvs.DistributedVirtualPortgroup], True)
    try:
        for dpg in view.view:
            if str(getattr(dpg, "key", "")) == dpg_key:
                dvs = getattr(getattr(dpg, "config", None), "distributedVirtualSwitch", None)
                uuid = str(getattr(dvs, "uuid", "")) if dvs is not None else ""
                return dpg, uuid
    finally:
        view.Destroy()
    return None, ""


def _wait_for_vm_ip(vm_obj: Any, timeout_sec: int = 300) -> str:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        guest = getattr(vm_obj, "guest", None)
        ip_addr = str(getattr(guest, "ipAddress", "") or "")
        if ip_addr and ip_addr.lower() != "unknown":
            return ip_addr
        time.sleep(3)
    return ""


def provision_test_vms(
    test_rows: List[Any],  # List of NetworkRow
    placements: Dict[tuple, Any],  # (datacenter, cluster) -> PlacementChoice
    network_bindings: Dict[tuple, Any],  # (datacenter, cluster, vlan) -> NetworkBinding
    args: Any,
    run_id: str,
    cases: Optional[List[Any]] = None,  # TestCase list for building per-VM target lists
    gateway_by_subnet: Optional[Dict[str, str]] = None,  # subnet -> gateway IP
    vms_per_subnet: int = 1,  # how many VMs to create per subnet row
) -> List[VMInstance]:
    """
    Provision test VMs for each unique network (VLAN/subnet/cluster).
    
    In dry-run mode, simulates VM instances.
    In execute-vcenter mode, creates actual VMs (requires proper vCenter access and pyvmomi).
    
    Args:
        test_rows: List of NetworkRow objects (only vm-provisioned mode)
        placements: Dict of placement choices keyed by (datacenter, cluster)
        network_bindings: Dict of network bindings keyed by (datacenter, cluster, vlan)
        args: Arguments (with vcenter_host, vcenter_user, vcenter_password, etc.)
        run_id: Unique run identifier for VM naming
    
    Returns:
        List of created VMInstance objects.
    """
    if not args.execute_vcenter:
        # In dry-run mode, simulate VM instances
        instances = []
        for i, row in enumerate(test_rows):
            placement_key = (row.datacenter, row.cluster)
            if placement_key not in placements:
                continue
            for vm_idx in range(max(1, vms_per_subnet)):
                vm_name = f"{args.vm_prefix}-{run_id}-{i:04d}-{vm_idx}"
                ip_addr = _allocate_ip_from_subnet(row.subnet, row.gw, vm_idx)
                instances.append(
                    VMInstance(
                        datacenter=row.datacenter,
                        cluster=row.cluster,
                        vm_name=vm_name,
                        moid="dry-run-moid",
                        subnet=row.subnet,
                        vlan=int(row.vlan),
                        ip_address=ip_addr,
                        mac_address="",
                        dpg_name="dry-run-dpg",
                        selection_policy="dry-run",
                        base_iso_path="",
                    )
                )
        return instances
    
    try:
        from pyVmomi import vim
    except ImportError as exc:
        raise RuntimeError("pyvmomi is required for --execute-vcenter. Install: pip install pyvmomi") from exc

    instances: List[VMInstance] = []
    unique_networks: Dict[tuple, Any] = {}
    for row in test_rows:
        network_key = (row.datacenter, row.cluster, row.vlan, row.subnet)
        if network_key not in unique_networks:
            unique_networks[network_key] = row

    root_pw = "nettest-alpine"

    ovf_path = str(getattr(args, "ovf_path", "") or "")
    if not ovf_path or not os.path.isfile(ovf_path):
        raise RuntimeError(
            f"OVF template not found: {ovf_path!r}. "
            "Set 'ovf_path' in config or pass --ovf-path."
        )

    with vcenter_session(args) as si:
        content = si.RetrieveContent()

        # ── Build one seed VM per (dc, cluster) for linked cloning ─────────────
        # Uploading a 300+ MB VMDK for every test VM is expensive.  Instead we
        # deploy the OVF once per cluster, take a memory-less snapshot, and
        # create all test VMs as linked clones (delta disks only — takes seconds).
        #
        # Seed key: (dc_name, cl_name).  Each seed is tracked for cleanup.
        seed_vm_map: Dict[tuple, Any] = {}          # key -> seed vm_obj
        seed_snap_map: Dict[tuple, Any] = {}        # key -> snapshot moRef
        seed_vm_instances: List[Any] = []           # for cleanup / registry

        unique_cluster_keys = {}
        for row in test_rows:
            ck = (row.datacenter, row.cluster)
            if ck not in unique_cluster_keys:
                unique_cluster_keys[ck] = row

        for (dc_name_s, cl_name_s), seed_row in unique_cluster_keys.items():
            placement_key_s = (dc_name_s, cl_name_s)
            if placement_key_s not in placements:
                continue
            placement_s = placements[placement_key_s]

            dc_obj_s = _find_datacenter(content, dc_name_s, vim)
            if dc_obj_s is None:
                raise RuntimeError(f"Datacenter not found: {dc_name_s}")
            cluster_obj_s = _find_cluster_in_datacenter(dc_obj_s, cl_name_s, vim)
            if cluster_obj_s is None:
                raise RuntimeError(f"Cluster not found: {cl_name_s}")
            avail_hosts_s = sorted(
                [h for h in getattr(cluster_obj_s, "host", [])
                 if str(getattr(getattr(h, "runtime", None), "connectionState", "")).lower() == "connected"
                 and not bool(getattr(getattr(h, "runtime", None), "inMaintenanceMode", False))],
                key=lambda h: str(h.name),
            )
            if not avail_hosts_s:
                raise RuntimeError(f"No connected hosts in cluster {cl_name_s}")
            host_obj_s = avail_hosts_s[0]
            datastore_obj_s = _find_datastore_in_dc(dc_obj_s, placement_s.datastore)
            if datastore_obj_s is None:
                raise RuntimeError(f"Datastore not found: {placement_s.datastore} in {dc_name_s}")
            resource_pool_s = _find_resource_pool(cluster_obj_s, str(args.resource_pool or ""))
            if resource_pool_s is None:
                raise RuntimeError(f"Resource pool not found in cluster {cl_name_s}")
            vm_folder_s = _find_vm_folder(dc_obj_s, "", vim)
            if vm_folder_s is None:
                raise RuntimeError(f"VM root folder not found in {dc_name_s}")

            seed_name = f"{args.vm_prefix}-{run_id}-seed-{cl_name_s.lower().replace(' ', '-')}"
            print(f"Deploying OVF seed VM '{seed_name}' for cluster {cl_name_s} "
                  f"(host={host_obj_s.name}, ds={datastore_obj_s.name})...", flush=True)

            # Use any accessible portgroup on this cluster for the seed NIC;
            # each clone will have its NIC reconfigured to the correct VLAN.
            first_bind_key = next(
                (k for k in network_bindings if k[0] == dc_name_s and k[1] == cl_name_s),
                None,
            )
            seed_dpg_key = network_bindings[first_bind_key].dpg_key if first_bind_key else ""

            seed_vm_obj = deploy_ovf_vm(
                si=si, content=content, vim=vim, ovf_path=ovf_path,
                vm_name=seed_name, resource_pool=resource_pool_s,
                datastore_obj=datastore_obj_s, vm_folder=vm_folder_s,
                host_obj=host_obj_s, dpg_key=seed_dpg_key,
            )
            print(f"  Seed deployed: {seed_name}; taking snapshot...", flush=True)
            snap_ref = take_vm_snapshot(vim, seed_vm_obj, snap_name="nettest-base")
            print(f"  Snapshot taken on {seed_name}", flush=True)

            seed_vm_map[(dc_name_s, cl_name_s)]  = seed_vm_obj
            seed_snap_map[(dc_name_s, cl_name_s)] = snap_ref
            seed_vm_instances.append(seed_vm_obj)

        for net_idx, (network_key, row) in enumerate(unique_networks.items()):
            dc_name, cl_name, vlan_id, subnet = network_key
            placement_key    = (dc_name, cl_name)
            network_bind_key = (dc_name, cl_name, row.vlan)
            if placement_key not in placements:
                raise RuntimeError(f"Missing placement for {dc_name}/{cl_name}")
            if network_bind_key not in network_bindings:
                raise RuntimeError(f"Missing network binding for {dc_name}/{cl_name}/{row.vlan}")

            placement       = placements[placement_key]
            network_binding = network_bindings[network_bind_key]

            dc_obj = _find_datacenter(content, dc_name, vim)
            if dc_obj is None:
                raise RuntimeError(f"Datacenter not found: {dc_name}")
            cluster_obj = _find_cluster_in_datacenter(dc_obj, cl_name, vim)
            if cluster_obj is None:
                raise RuntimeError(f"Cluster not found in datacenter {dc_name}: {cl_name}")
            vm_folder = _find_vm_folder(dc_obj, "", vim)
            if vm_folder is None:
                raise RuntimeError(f"Datacenter VM root folder not found: {dc_name}")
            resource_pool = _find_resource_pool(cluster_obj, str(args.resource_pool or ""))
            if resource_pool is None:
                raise RuntimeError(f"Resource pool not found in cluster {cl_name}: {args.resource_pool}")

            # Build sorted list of available hosts for round-robin placement.
            # Intra-subnet VMs are placed on different hosts so traffic traverses
            # the physical network rather than staying inside one hypervisor.
            avail_hosts = sorted(
                [h for h in getattr(cluster_obj, "host", [])
                 if str(getattr(getattr(h, "runtime", None), "connectionState", "")).lower() == "connected"
                 and not bool(getattr(getattr(h, "runtime", None), "inMaintenanceMode", False))],
                key=lambda h: str(h.name),
            )
            if not avail_hosts:
                raise RuntimeError(f"No connected hosts in cluster {cl_name}")

            net_obj = ipaddress.ip_network(
                subnet if "/" in subnet else f"{subnet}/24", strict=False
            )
            prefix_len = net_obj.prefixlen

            # Pre-allocate IPs for all VMs in this subnet
            alloc_ips = [
                _allocate_ip_from_subnet(subnet, row.gw, vm_idx)
                for vm_idx in range(max(1, vms_per_subnet))
            ]

            # Build complete per-VM probe target lists up front (intra + cross).
            # All targets are written BEFORE power-on so netprobe.start always
            # reads them from guestinfo without any caching race.
            per_vm_cross_targets: List[List[str]] = []
            per_vm_intra_targets: List[List[str]] = []
            for vm_idx in range(max(1, vms_per_subnet)):
                cross: List[str] = []
                intra: List[str] = []
                if cases and gateway_by_subnet:
                    seen: set = set()
                    for case in cases:
                        if str(getattr(case, "src_subnet", "")) != subnet:
                            continue
                        if int(getattr(case, "src_vm_index", 0)) != vm_idx:
                            continue
                        dst_subnet = str(getattr(case, "dst_subnet", ""))
                        if dst_subnet == subnet:
                            dst_vm_idx = int(getattr(case, "dst_vm_index", 0))
                            if 0 <= dst_vm_idx < len(alloc_ips):
                                ip = alloc_ips[dst_vm_idx]
                                if ip not in seen:
                                    seen.add(ip)
                                    intra.append(ip)
                        else:
                            gw = gateway_by_subnet.get(dst_subnet, "")
                            if gw and gw not in seen:
                                seen.add(gw)
                                cross.append(gw)
                per_vm_cross_targets.append(cross)
                per_vm_intra_targets.append(intra)

            has_intra = any(per_vm_intra_targets)

            # For intra-subnet probes, also flag VMs that are pure destinations.
            # VMware Tools can report "running" before /etc/local.d/netprobe.start
            # has finished configuring the network interface.  By making every
            # intra-subnet participant (source OR destination) use ready=wait we
            # guarantee that every VM is actively polling guestinfo — meaning its
            # network config step has already completed — before any peer pings it.
            intra_dst_vm_indices: set = set()
            if has_intra and cases:
                for case in cases:
                    if (str(getattr(case, "src_subnet", "")) == subnet
                            and str(getattr(case, "dst_subnet", "")) == subnet):
                        intra_dst_vm_indices.add(int(getattr(case, "dst_vm_index", 0)))

            # ── Phase 1: Deploy all VMs, write ALL targets before power-on ───
            # Writing complete targets before boot eliminates any vmtoolsd
            # read-cache race that would cause empty results.
            # VMs with intra targets (source) or intra destination roles use
            # ready=wait; pure cross-subnet VMs get ready=go immediately.
            # Each VM is placed on a different host (round-robin by vm_idx).
            vm_needs_wait: List[bool] = []
            subnet_vm_objs: List[Any] = []
            for vm_idx in range(max(1, vms_per_subnet)):
                alloc_ip      = alloc_ips[vm_idx]
                vm_name       = f"{args.vm_prefix}-{run_id}-net-{net_idx:04d}-{vm_idx}"
                intra_targets = per_vm_intra_targets[vm_idx]
                cross_targets = per_vm_cross_targets[vm_idx]
                all_targets   = intra_targets + cross_targets
                # ready=wait: VM is an intra-subnet source OR destination.
                # ready=go:   VM has no intra-subnet role at all.
                is_intra_participant = bool(intra_targets) or (vm_idx in intra_dst_vm_indices)
                ready_val = "wait" if (has_intra and is_intra_participant) else "go"
                vm_needs_wait.append(ready_val == "wait")

                # Round-robin host selection: spread VMs across cluster hosts
                host_obj_for_vm = avail_hosts[vm_idx % len(avail_hosts)]
                # Pick accessible datastore on this specific host
                datastore_obj_for_vm = None
                if placement.datastore:
                    datastore_obj_for_vm = _find_datastore_in_dc(dc_obj, placement.datastore)
                if datastore_obj_for_vm is None:
                    # Fallback: any accessible datastore on this host
                    for ds in getattr(host_obj_for_vm, "datastore", []):
                        if getattr(getattr(ds, "summary", None), "accessible", False):
                            datastore_obj_for_vm = ds
                            break
                if datastore_obj_for_vm is None:
                    raise RuntimeError(
                        f"No accessible datastore on host {host_obj_for_vm.name} in {cl_name}"
                    )

                print(f"Cloning VM '{vm_name}' from seed "
                      f"(ip={alloc_ip}, vm_idx={vm_idx}, host={host_obj_for_vm.name}, "
                      f"ds={datastore_obj_for_vm.name})...",
                      flush=True)
                seed_vm_obj = seed_vm_map[(dc_name, cl_name)]
                seed_snap   = seed_snap_map[(dc_name, cl_name)]
                vm_obj = clone_vm_linked(
                    vim=vim, content=content,
                    template_vm=seed_vm_obj, snapshot_ref=seed_snap,
                    vm_name=vm_name, host_obj=host_obj_for_vm,
                    datastore_obj=datastore_obj_for_vm,
                    resource_pool=resource_pool, vm_folder=vm_folder,
                    dpg_key=network_binding.dpg_key,
                )
                print(f"  Cloned: {vm_name}", flush=True)

                write_probe_guestinfo(
                    vim=vim, vm_obj=vm_obj,
                    ip_address=alloc_ip, prefix_len=prefix_len, gateway=row.gw,
                    targets=all_targets, ready=ready_val,
                )
                wait_for_task(vm_obj.PowerOnVM_Task())
                print(f"  Powered on: {vm_name} (ready={ready_val}, host={host_obj_for_vm.name})", flush=True)
                subnet_vm_objs.append(vm_obj)

            # ── Phase 2: Wait for ALL VMs in this subnet to be tools-ready ───
            vmtools_timeout = 300
            subnet_tools_ok: List[bool] = []
            for vm_idx, vm_obj in enumerate(subnet_vm_objs):
                vm_name = vm_obj.name
                print(f"Waiting for VMware Tools on {vm_name} (timeout={vmtools_timeout}s)...", flush=True)
                tools_ok = _wait_for_vmtools(vm_obj, vmtools_timeout)
                if not tools_ok:
                    print(f"Warning: VMware Tools not ready on {vm_name}", flush=True)
                else:
                    print(f"VMware Tools running on {vm_name}", flush=True)
                subnet_tools_ok.append(tools_ok)

            # ── Phase 3: Signal go to VMs that were held at ready=wait ────────
            # Targets were already written before power-on; only flip ready=go.
            # Both intra-sources and intra-destinations are signalled here so
            # that sources only start pinging once all destinations have their
            # network interfaces configured (destinations are past the ip-addr
            # step when they reach the polling loop).
            for vm_idx, vm_obj in enumerate(subnet_vm_objs):
                if vm_needs_wait[vm_idx]:
                    print(f"  Setting ready=go for {vm_obj.name}", flush=True)
                    write_probe_ready(vim=vim, vm_obj=vm_obj)

            # ── Phase 4: Poll each VM for probe results ────────────────────────
            for vm_idx, vm_obj in enumerate(subnet_vm_objs):
                alloc_ip = alloc_ips[vm_idx]
                vm_name  = f"{args.vm_prefix}-{run_id}-net-{net_idx:04d}-{vm_idx}"
                tools_ok = subnet_tools_ok[vm_idx]
                all_targets = per_vm_intra_targets[vm_idx] + per_vm_cross_targets[vm_idx]

                vm_probe_results: Dict[str, str] = {}
                if all_targets and tools_ok:
                    print(f"  Waiting for probe results from {vm_name} (timeout=180s)...", flush=True)
                    result = poll_probe_results(vm_obj, timeout_sec=180)
                    if result is not None:
                        vm_probe_results = result
                        print(f"  Probe results: {vm_probe_results}", flush=True)
                    else:
                        print(f"  Warning: no probe results received from {vm_name}", flush=True)

                instances.append(
                    VMInstance(
                        datacenter=dc_name,
                        cluster=cl_name,
                        vm_name=vm_name,
                        moid=str(getattr(vm_obj, "_moId", "")),
                        subnet=subnet,
                        vlan=int(vlan_id),
                        ip_address=alloc_ip,
                        mac_address="",
                        dpg_name=network_binding.dpg_name,
                        selection_policy=network_binding.resolution_policy,
                        base_iso_path="",
                        vmtools_ready=tools_ok,
                        probe_results=vm_probe_results,
                    )
                )
                print(f"Created {vm_name} (moid={instances[-1].moid}, ip={alloc_ip}) "
                      f"on VLAN {vlan_id} vm_idx={vm_idx}")

        # Append seed VMs at the end so the runner can clean them up last.
        # They are marked with a special dpg_name so callers can distinguish.
        for seed_obj in seed_vm_instances:
            instances.append(
                VMInstance(
                    datacenter="",
                    cluster="",
                    vm_name=str(seed_obj.name),
                    moid=str(getattr(seed_obj, "_moId", "")),
                    subnet="",
                    vlan=0,
                    ip_address="",
                    mac_address="",
                    dpg_name="__seed__",
                    selection_policy="seed",
                    base_iso_path="",
                    vmtools_ready=False,
                    probe_results={},
                )
            )

    return instances


def cleanup_vms(
    instances: List[VMInstance],
    args: Any,
    on_failure: bool = False,
) -> Dict[str, Any]:
    """
    Clean up provisioned test VMs.
    
    Args:
        instances: List of VMInstance objects to clean up
        args: Arguments (with vcenter_host, etc.)
        on_failure: If True and cleanup_on_failure is False, skip cleanup
    
    Returns:
        Dict with cleanup details.
    """
    if not args.execute_vcenter:
        return {
            "mode": "dry-run",
            "instances_cleaned": 0,
            "instances_retained": len(instances),
            "reason": "dry-run mode",
        }
    
    if on_failure and not args.cleanup_on_failure:
        return {
            "mode": "execute",
            "instances_cleaned": 0,
            "instances_retained": len(instances),
            "reason": "cleanup_on_failure=False; resources retained for troubleshooting",
            "retained_vms": [
                {"vm_name": inst.vm_name, "moid": inst.moid, "ip_address": inst.ip_address}
                for inst in instances
            ],
        }

    try:
        from pyVmomi import vim
    except Exception as exc:
        return {
            "mode": "execute",
            "instances_cleaned": 0,
            "instances_failed": len(instances),
            "instances_retained": len(instances),
            "reason": f"pyvmomi-import-failed: {exc}",
            "failed_vms": [
                {"vm_name": inst.vm_name, "moid": inst.moid, "reason": "pyvmomi-import-failed"}
                for inst in instances
            ],
        }

    def _find_vm(content: Any, inst: VMInstance) -> Optional[Any]:
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        try:
            # Prefer exact moid match.
            for vm in view.view:
                if str(getattr(vm, "_moId", "")) == str(inst.moid):
                    return vm
            # Fallback to exact VM name.
            for vm in view.view:
                if str(getattr(vm, "name", "")) == inst.vm_name:
                    return vm
            return None
        finally:
            view.Destroy()

    cleaned_count = 0
    failed_vms: List[Dict[str, str]] = []

    try:
        with vcenter_session(args) as si:
            content = si.RetrieveContent()
            for inst in instances:
                if not inst.vm_name.startswith(str(args.vm_prefix)):
                    failed_vms.append(
                        {
                            "vm_name": inst.vm_name,
                            "moid": inst.moid,
                            "reason": "unsafe-vm-name-prefix-mismatch",
                        }
                    )
                    continue

                vm_obj = _find_vm(content, inst)
                if vm_obj is None:
                    failed_vms.append(
                        {
                            "vm_name": inst.vm_name,
                            "moid": inst.moid,
                            "reason": "vm-not-found",
                        }
                    )
                    continue

                try:
                    power_state = str(getattr(getattr(vm_obj, "runtime", None), "powerState", ""))
                    if power_state and power_state != "poweredOff":
                        task = vm_obj.PowerOffVM_Task()
                        wait_for_task(task)

                    destroy_task = vm_obj.Destroy_Task()
                    wait_for_task(destroy_task)
                    cleaned_count += 1

                except Exception as exc:
                    failed_vms.append(
                        {
                            "vm_name": inst.vm_name,
                            "moid": inst.moid,
                            "reason": f"cleanup-failed:{exc}",
                        }
                    )
    except Exception as exc:
        return {
            "mode": "execute",
            "instances_cleaned": 0,
            "instances_failed": len(instances),
            "instances_retained": len(instances),
            "reason": f"vcenter-connect-failed: {exc}",
            "failed_vms": [
                {"vm_name": inst.vm_name, "moid": inst.moid, "reason": "vcenter-connect-failed"}
                for inst in instances
            ],
        }

    retained = len(instances) - cleaned_count
    result: Dict[str, Any] = {
        "mode": "execute",
        "instances_cleaned": cleaned_count,
        "instances_failed": len(failed_vms),
        "instances_retained": retained,
    }
    if failed_vms:
        result["failed_vms"] = failed_vms
    return result
