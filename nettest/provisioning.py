"""
VM provisioning and lifecycle management for network tests.
Handles creation, configuration, and cleanup of test VMs.
"""

from __future__ import annotations

import logging
import os
import random
import time
import ipaddress
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

from nettest.vcenter_utils import vcenter_session, wait_for_task
from nettest.ovf_deploy import (
    deploy_ovf_vm, write_probe_guestinfo, write_probe_ready, poll_probe_results,
    wait_for_probe_status, take_vm_snapshot, clone_vm_linked,
    add_serial_port_tcp, ensure_vmci_device, get_vmci_cid,
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
        logger.warning("Failed to allocate IP from %s (gw: %s): %s", subnet_str, gateway_str, exc)
        # Fallback: return a placeholder
        return f"10.0.0.{index + 1}"


def _query_used_ips_in_subnet(content: Any, vim: Any, subnet_str: str) -> Set[str]:
    """Query vCenter for all IPs currently assigned to VMs within *subnet_str*.

    Uses a ContainerView over all VirtualMachine objects and reads
    vm.guest.net[*].ipAddress — no SSH or Guest Ops required.
    Returns a set of IP address strings (IPv4 only).
    """
    try:
        if "/" not in subnet_str:
            network = ipaddress.ip_network(f"{subnet_str}/24", strict=False)
        else:
            network = ipaddress.ip_network(subnet_str, strict=False)
    except ValueError:
        return set()

    used: Set[str] = set()
    try:
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            for vm in view.view:
                guest = getattr(vm, "guest", None)
                for nic in getattr(guest, "net", None) or []:
                    for ip_str in getattr(nic, "ipAddress", None) or []:
                        try:
                            addr = ipaddress.ip_address(ip_str)
                            if isinstance(addr, ipaddress.IPv4Address) and addr in network:
                                used.add(ip_str)
                        except ValueError:
                            pass
        finally:
            view.Destroy()
    except Exception as exc:
        logger.warning("Could not query used IPs in %s: %s", subnet_str, exc)
    return used


def _pick_random_free_ips(
    subnet_str: str,
    gateway_str: str,
    count: int,
    used_ips: Set[str],
) -> List[str]:
    """Return *count* randomly chosen free host IPs from *subnet_str*.

    Excludes the network address, broadcast address, gateway, and any IP
    already present in *used_ips* (from vCenter query or prior allocations
    in this run).  Falls back to sequential allocation if the free pool is
    exhausted.
    """
    try:
        if "/" not in subnet_str:
            network = ipaddress.ip_network(f"{subnet_str}/24", strict=False)
        else:
            network = ipaddress.ip_network(subnet_str, strict=False)
        gateway_ip = ipaddress.ip_address(gateway_str)
    except ValueError as exc:
        logger.warning("Invalid subnet/gateway (%s/%s): %s", subnet_str, gateway_str, exc)
        return [f"10.0.0.{i + 1}" for i in range(count)]

    # Build candidate pool: all host IPs excluding gateway and already-used IPs.
    # Skip the first 9 usable hosts (.1-.9) to avoid common infrastructure IPs.
    all_hosts = list(network.hosts())
    skip_low = min(9, len(all_hosts))
    candidates = [
        str(ip) for ip in all_hosts[skip_low:]
        if ip != gateway_ip and str(ip) not in used_ips
    ]

    if len(candidates) >= count:
        chosen = random.sample(candidates, count)
    elif candidates:
        logger.warning(
            "Free IP pool in %s has only %d IPs, need %d; reusing pool",
            subnet_str, len(candidates), count,
        )
        # Allow repeats only as last resort
        chosen = [candidates[i % len(candidates)] for i in range(count)]
    else:
        logger.warning("No free IPs found in %s; falling back to sequential", subnet_str)
        chosen = [
            _allocate_ip_from_subnet(subnet_str, gateway_str, i) for i in range(count)
        ]
    return chosen


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
        _dry_run_used: Dict[str, Set[str]] = {}
        for i, row in enumerate(test_rows):
            placement_key = (row.datacenter, row.cluster)
            if placement_key not in placements:
                continue
            n_vms = max(1, vms_per_subnet)
            used_set = _dry_run_used.setdefault(row.subnet, set())
            ips = _pick_random_free_ips(row.subnet, row.gw, n_vms, used_set)
            used_set.update(ips)
            for vm_idx in range(n_vms):
                vm_name = f"{args.vm_prefix}-{run_id}-{i:04d}-{vm_idx}"
                ip_addr = ips[vm_idx]
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

    instances = []
    unique_networks: Dict[tuple, Any] = {}
    for row in test_rows:
        network_key = (row.datacenter, row.cluster, row.vlan, row.subnet)
        if network_key not in unique_networks:
            unique_networks[network_key] = row

    root_pw = "nettest-alpine"
    boot_method = str(getattr(args, "boot_method", "ovf") or "ovf")

    if boot_method == "ovf":
        ovf_path = str(getattr(args, "ovf_path", "") or "")
        if not ovf_path or not os.path.isfile(ovf_path):
            raise RuntimeError(
                f"OVF template not found: {ovf_path!r}. "
                "Set 'ovf_path' in config or pass --ovf-path."
            )
    elif boot_method == "memboot":
        memboot_iso_path = str(getattr(args, "memboot_iso_path", "") or "")
        if not memboot_iso_path:
            raise RuntimeError(
                "memboot_iso_path is required when boot_method='memboot'. "
                "Build it with ovf/build_mini_iso.sh and pass --memboot-iso-path."
            )
    else:
        raise RuntimeError(f"Unknown boot_method: {boot_method!r}. Use 'ovf' or 'memboot'.")

    with vcenter_session(args) as si:
        content = si.RetrieveContent()

        # ── Per-cluster initialisation ─────────────────────────────────────────
        # ovf:     deploy seed VM once per cluster → linked clones later
        # memboot: upload ISO once per cluster → create VMs directly later
        seed_vm_map: Dict[tuple, Any] = {}          # ovf:     (dc, cl) -> seed vm_obj
        seed_snap_map: Dict[tuple, Any] = {}        # ovf:     (dc, cl) -> snapshot moRef
        seed_vm_instances: List[Any] = []           # ovf:     for cleanup / registry
        iso_map: Dict[tuple, str] = {}              # memboot: (dc, cl) -> datastore iso path

        unique_cluster_keys: Dict[tuple, Any] = {}
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

            if boot_method == "ovf":
                resource_pool_s = _find_resource_pool(cluster_obj_s, str(args.resource_pool or ""))
                if resource_pool_s is None:
                    raise RuntimeError(f"Resource pool not found in cluster {cl_name_s}")
                vm_folder_s = _find_vm_folder(dc_obj_s, "", vim)
                if vm_folder_s is None:
                    raise RuntimeError(f"VM root folder not found in {dc_name_s}")

                seed_name = f"{args.vm_prefix}-{run_id}-seed-{cl_name_s.lower().replace(' ', '-')}"
                logger.info(
                    "Deploying OVF seed VM '%s' for cluster %s (host=%s, ds=%s)...",
                    seed_name, cl_name_s, host_obj_s.name, datastore_obj_s.name,
                )

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
                logger.info("Seed deployed: %s; taking snapshot...", seed_name)
                snap_ref = take_vm_snapshot(vim, seed_vm_obj, snap_name="nettest-base")
                logger.info("Snapshot taken on %s", seed_name)

                seed_vm_map[(dc_name_s, cl_name_s)]  = seed_vm_obj
                seed_snap_map[(dc_name_s, cl_name_s)] = snap_ref
                seed_vm_instances.append(seed_vm_obj)

            elif boot_method == "memboot":
                from nettest.memboot import ensure_iso_on_datastore
                ds_iso_path = ensure_iso_on_datastore(
                    si=si,
                    vcenter_host=str(args.vcenter_host),
                    datacenter_name=dc_name_s,
                    datastore_name=datastore_obj_s.name,
                    local_iso_path=memboot_iso_path,
                )
                iso_map[(dc_name_s, cl_name_s)] = ds_iso_path
                logger.info(
                    "Memboot ISO ready for cluster %s/%s: %s",
                    dc_name_s, cl_name_s, ds_iso_path,
                )

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

            # Pre-allocate IPs for all VMs in this subnet.
            # Query vCenter for IPs already in use so we never collide with
            # existing VMs on the same segment, then pick randomly from free pool.
            n_vms = max(1, vms_per_subnet)
            used_ips = _query_used_ips_in_subnet(content, vim, subnet)
            logger.info(
                "Subnet %s: %d IPs in use by existing VMs, picking %d free IPs randomly",
                subnet, len(used_ips), n_vms,
            )
            alloc_ips = _pick_random_free_ips(subnet, row.gw, n_vms, used_ips)

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

            # For intra-subnet probes also flag VMs that are pure destinations so
            # every participant uses the two-phase ready-handshake.
            intra_dst_vm_indices: set = set()
            if has_intra and cases:
                for case in cases:
                    if (str(getattr(case, "src_subnet", "")) == subnet
                            and str(getattr(case, "dst_subnet", "")) == subnet):
                        intra_dst_vm_indices.add(int(getattr(case, "dst_vm_index", 0)))

            poll_method = str(getattr(args, "poll_method", "guestinfo"))

            # ── Pre-compute serial / vsock resources for this subnet ───────────
            # Serial: reserve one TCP port per VM before cloning so the port is
            # known when we call add_serial_port_tcp (VM must be powered off).
            serial_ports: List[int] = []
            vsock_cids:   List[int] = []   # filled after power-on for vsock

            if poll_method == "serial":
                from nettest.serial_probe import SerialProbeServer, detect_controller_ip
                _serial_server: Any = getattr(args, "_serial_server", None)
                if _serial_server is None:
                    raise RuntimeError("SerialProbeServer not initialised on args._serial_server")
                for _ in range(max(1, vms_per_subnet)):
                    serial_ports.append(_serial_server.alloc_port())

            elif poll_method == "vsock":
                from nettest.vsock_probe import VsockProbeServer
                _vsock_server: Any = getattr(args, "_vsock_server", None)
                if _vsock_server is None:
                    raise RuntimeError("VsockProbeServer not initialised on args._vsock_server")

            # ── Phase 1: Clone all VMs and attach probe devices ───────────────
            # For guestinfo: write extraConfig before power-on (no vmtoolsd cache race).
            # For serial/vsock: attach device, then power on (no extraConfig needed).
            subnet_vm_objs: List[Any] = []
            vm_needs_wait:  List[bool] = []   # only meaningful for guestinfo mode

            for vm_idx in range(max(1, vms_per_subnet)):
                alloc_ip      = alloc_ips[vm_idx]
                vm_name       = f"{args.vm_prefix}-{run_id}-net-{net_idx:04d}-{vm_idx}"
                intra_targets = per_vm_intra_targets[vm_idx]
                cross_targets = per_vm_cross_targets[vm_idx]
                all_targets   = intra_targets + cross_targets
                is_intra_part = bool(intra_targets) or (vm_idx in intra_dst_vm_indices)

                # Round-robin host placement
                host_obj_for_vm = avail_hosts[vm_idx % len(avail_hosts)]
                datastore_obj_for_vm = None
                if placement.datastore:
                    datastore_obj_for_vm = _find_datastore_in_dc(dc_obj, placement.datastore)
                if datastore_obj_for_vm is None:
                    for ds in getattr(host_obj_for_vm, "datastore", []):
                        if getattr(getattr(ds, "summary", None), "accessible", False):
                            datastore_obj_for_vm = ds
                            break
                if datastore_obj_for_vm is None:
                    raise RuntimeError(
                        f"No accessible datastore on host {host_obj_for_vm.name} in {cl_name}"
                    )

                if boot_method == "ovf":
                    logger.info(
                        "Cloning VM '%s' from seed (ip=%s, vm_idx=%s, host=%s, ds=%s)...",
                        vm_name, alloc_ip, vm_idx, host_obj_for_vm.name, datastore_obj_for_vm.name,
                    )
                    seed_vm_obj_c = seed_vm_map[(dc_name, cl_name)]
                    seed_snap_c   = seed_snap_map[(dc_name, cl_name)]
                    vm_obj = clone_vm_linked(
                        vim=vim, content=content,
                        template_vm=seed_vm_obj_c, snapshot_ref=seed_snap_c,
                        vm_name=vm_name, host_obj=host_obj_for_vm,
                        datastore_obj=datastore_obj_for_vm,
                        resource_pool=resource_pool, vm_folder=vm_folder,
                        dpg_key=network_binding.dpg_key,
                    )
                    logger.info("Cloned: %s", vm_name)

                    # Attach probe device (ovf path only; memboot includes device in CreateVM_Task)
                    if poll_method == "guestinfo":
                        # Write probe config into extraConfig before power-on.
                        ready_val = "wait" if (has_intra and is_intra_part) else "go"
                        vm_needs_wait.append(ready_val == "wait")
                        write_probe_guestinfo(
                            vim=vim, vm_obj=vm_obj,
                            ip_address=alloc_ip, prefix_len=prefix_len, gateway=row.gw,
                            targets=all_targets, ready=ready_val,
                        )

                    elif poll_method == "serial":
                        vm_needs_wait.append(False)  # serial uses protocol sync
                        controller_ip = str(getattr(args, "serial_probe_host", "") or "")
                        if not controller_ip:
                            from nettest.serial_probe import detect_controller_ip
                            controller_ip = detect_controller_ip(str(args.vcenter_host))
                        add_serial_port_tcp(vim, vm_obj, controller_ip, serial_ports[vm_idx])

                    elif poll_method == "vsock":
                        vm_needs_wait.append(False)  # vsock uses protocol sync
                        ensure_vmci_device(vim, vm_obj)

                elif boot_method == "memboot":
                    # create_memboot_vm handles serial port / VMCI device internally
                    from nettest.memboot import create_memboot_vm
                    _c_ip, _s_port = "", 0
                    if poll_method == "serial":
                        _c_ip = str(getattr(args, "serial_probe_host", "") or "")
                        if not _c_ip:
                            from nettest.serial_probe import detect_controller_ip
                            _c_ip = detect_controller_ip(str(args.vcenter_host))
                        _s_port = serial_ports[vm_idx]
                    vm_obj = create_memboot_vm(
                        vim=vim, content=content,
                        vm_name=vm_name, host_obj=host_obj_for_vm,
                        datastore_obj=datastore_obj_for_vm,
                        resource_pool=resource_pool, vm_folder=vm_folder,
                        dpg_key=network_binding.dpg_key,
                        iso_ds_path=iso_map[(dc_name, cl_name)],
                        poll_method=poll_method,
                        controller_ip=_c_ip,
                        serial_port=_s_port,
                    )
                    vm_needs_wait.append(False)  # memboot always uses protocol sync

                wait_for_task(vm_obj.PowerOnVM_Task())
                logger.info(
                    "Powered on: %s (boot=%s, poll=%s, host=%s)",
                    vm_name, boot_method, poll_method, host_obj_for_vm.name,
                )
                subnet_vm_objs.append(vm_obj)

            # ── Phase 2 (guestinfo only): wait for VMware Tools ───────────────
            subnet_tools_ok: List[bool] = []
            if poll_method == "guestinfo":
                vmtools_timeout = 300
                for vm_obj in subnet_vm_objs:
                    logger.info("Waiting for VMware Tools on %s...", vm_obj.name)
                    tools_ok = _wait_for_vmtools(vm_obj, vmtools_timeout)
                    if not tools_ok:
                        logger.warning("VMware Tools not ready on %s", vm_obj.name)
                    else:
                        logger.info("VMware Tools running on %s", vm_obj.name)
                    subnet_tools_ok.append(tools_ok)
            else:
                # serial/vsock: no vmtools dependency — mark all as ready
                subnet_tools_ok = [True] * len(subnet_vm_objs)

            # ── Phase 3 (guestinfo only): two-phase ready handshake ───────────
            if poll_method == "guestinfo":
                for vm_idx, vm_obj in enumerate(subnet_vm_objs):
                    if vm_needs_wait[vm_idx]:
                        logger.info("Waiting for status=waiting on %s...", vm_obj.name)
                        ok = wait_for_probe_status(vm_obj, "waiting", timeout_sec=300)
                        if not ok:
                            logger.warning(
                                "VM %s did not reach status=waiting in time; "
                                "sending ready=go anyway",
                                vm_obj.name,
                            )
                for vm_idx, vm_obj in enumerate(subnet_vm_objs):
                    if vm_needs_wait[vm_idx]:
                        logger.info("Setting ready=go for %s", vm_obj.name)
                        write_probe_ready(vim=vim, vm_obj=vm_obj)

            # ── Phase 3 (vsock only): collect VMCI CIDs after power-on ────────
            if poll_method == "vsock":
                for vm_obj in subnet_vm_objs:
                    cid = get_vmci_cid(vm_obj)
                    vsock_cids.append(cid if cid is not None else -1)
                    logger.debug("VMCI CID for %s: %s", vm_obj.name, vsock_cids[-1])

            # ── Phase 4: Collect probe results ────────────────────────────────
            subnet_probe_results: List[Dict[str, str]] = [{} for _ in subnet_vm_objs]

            if poll_method == "guestinfo":
                for vm_idx, vm_obj in enumerate(subnet_vm_objs):
                    vm_name     = vm_obj.name
                    tools_ok    = subnet_tools_ok[vm_idx]
                    all_targets = per_vm_intra_targets[vm_idx] + per_vm_cross_targets[vm_idx]
                    if all_targets and tools_ok:
                        logger.info("Polling probe results from %s...", vm_name)
                        result = poll_probe_results(vm_obj, timeout_sec=180)
                        if result is not None:
                            subnet_probe_results[vm_idx] = result
                            logger.info("Probe results: %s", result)
                        else:
                            logger.warning("No probe results from %s", vm_name)

            elif poll_method == "serial":
                _serial_server = getattr(args, "_serial_server")
                configs = [
                    {
                        "port":    serial_ports[vi],
                        "ip":      alloc_ips[vi],
                        "prefix":  prefix_len,
                        "gw":      row.gw,
                        "targets": per_vm_intra_targets[vi] + per_vm_cross_targets[vi],
                    }
                    for vi in range(len(subnet_vm_objs))
                ]
                serial_results = _serial_server.run_subnet_probe(
                    configs, sync=has_intra,
                    connect_timeout=300, io_timeout=180,
                )
                for vi, sr in enumerate(serial_results):
                    if sr["error"]:
                        logger.warning("Serial probe error on vm_idx=%s: %s", vi, sr["error"])
                    subnet_probe_results[vi] = sr["results"]

            elif poll_method == "vsock":
                _vsock_server = getattr(args, "_vsock_server")
                configs = [
                    {
                        "cid":     vsock_cids[vi],
                        "ip":      alloc_ips[vi],
                        "prefix":  prefix_len,
                        "gw":      row.gw,
                        "targets": per_vm_intra_targets[vi] + per_vm_cross_targets[vi],
                    }
                    for vi in range(len(subnet_vm_objs))
                ]
                vsock_results = _vsock_server.run_subnet_probe(
                    configs, sync=has_intra,
                )
                for vi, vr in enumerate(vsock_results):
                    if vr["error"]:
                        logger.warning("Vsock probe error on vm_idx=%s: %s", vi, vr["error"])
                    subnet_probe_results[vi] = vr["results"]

            # ── Assemble VMInstance records ───────────────────────────────────
            for vm_idx, vm_obj in enumerate(subnet_vm_objs):
                alloc_ip = alloc_ips[vm_idx]
                vm_name  = f"{args.vm_prefix}-{run_id}-net-{net_idx:04d}-{vm_idx}"
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
                        vmtools_ready=subnet_tools_ok[vm_idx],
                        probe_results=subnet_probe_results[vm_idx],
                    )
                )
                logger.info("Created %s (moid=%s, ip=%s) on VLAN %s vm_idx=%s",
                            vm_name, instances[-1].moid, alloc_ip, vlan_id, vm_idx)

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
