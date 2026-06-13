from __future__ import annotations

import os
import random
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from nettest.models import NetworkBinding, NetworkRow, PlacementChoice
from nettest.vcenter_utils import vcenter_session


def validate_vcenter_requirements(args: Any) -> None:
    missing = []
    if not args.vcenter_host:
        missing.append("--vcenter-host")
    # In plugin (session) mode credentials are not required
    if not getattr(args, "vcenter_session_id", ""):
        if not args.vcenter_user:
            missing.append("--vcenter-user")
        if not args.vcenter_password:
            missing.append("--vcenter-password")

    boot_method = str(getattr(args, "boot_method", "ovf") or "ovf").strip().lower()
    if boot_method == "ovf":
        if not args.ovf_path:
            missing.append("--ovf-path")
    elif boot_method == "memboot":
        if not str(getattr(args, "memboot_iso_path", "") or ""):
            missing.append("--memboot-iso-path")
    else:
        raise RuntimeError(f"Unknown boot_method: {boot_method!r}. Use 'ovf' or 'memboot'.")

    if missing:
        raise RuntimeError("Missing required details for --execute-vcenter: " + ", ".join(missing))

    if boot_method == "ovf" and not os.path.isfile(str(args.ovf_path)):
        raise RuntimeError(f"OVF template file not found: {args.ovf_path}")

    if boot_method == "memboot":
        iso = str(getattr(args, "memboot_iso_path", "") or "")
        if iso and not iso.startswith("[") and not os.path.isfile(iso):
            raise RuntimeError(f"memboot ISO file not found: {iso}")


def _iter_clusters(folder: Any, vim: Any) -> Iterable[Any]:
    for child in getattr(folder, "childEntity", []):
        if isinstance(child, vim.Folder):
            yield from _iter_clusters(child, vim)
        elif isinstance(child, vim.ClusterComputeResource):
            yield child


def _find_datacenter(content: Any, datacenter_name: str, vim: Any) -> Any:
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datacenter], True)
    try:
        target = datacenter_name.strip().upper()
        for dc in view.view:
            if str(dc.name).strip().upper() == target:
                return dc
    finally:
        view.Destroy()
    return None


def _find_cluster_in_datacenter(dc: Any, cluster_name: str, vim: Any) -> Any:
    target = cluster_name.strip().upper()
    for cluster in _iter_clusters(dc.hostFolder, vim):
        if str(cluster.name).strip().upper() == target:
            return cluster
    return None


def _choose_host_and_datastore(cluster_obj: Any, datastore_preference: str, rng: random.Random) -> Tuple[str, str]:
    candidate_hosts = []
    for host in getattr(cluster_obj, "host", []):
        runtime = getattr(host, "runtime", None)
        state = str(getattr(runtime, "connectionState", "")).lower()
        in_maintenance = bool(getattr(runtime, "inMaintenanceMode", False))
        if state == "connected" and not in_maintenance:
            candidate_hosts.append(host)

    if not candidate_hosts:
        raise RuntimeError(f"No connected hosts available in cluster {cluster_obj.name}")

    selected_host = rng.choice(candidate_hosts)
    host_name = str(selected_host.name)

    accessible_datastores = []
    for ds in getattr(selected_host, "datastore", []):
        summary = getattr(ds, "summary", None)
        accessible = bool(getattr(summary, "accessible", True))
        if accessible:
            accessible_datastores.append(ds)

    if not accessible_datastores:
        raise RuntimeError(f"Host {host_name} has no accessible datastores")

    if datastore_preference:
        preferred = [ds for ds in accessible_datastores if str(ds.name) == datastore_preference]
        if not preferred:
            raise RuntimeError(
                f"Preferred datastore {datastore_preference} not accessible from host {host_name}"
            )
        selected_ds = preferred[0]
    else:
        selected_ds = rng.choice(accessible_datastores)

    return host_name, str(selected_ds.name)


def _iter_network_entities(folder: Any, vim: Any) -> Iterable[Any]:
    for child in getattr(folder, "childEntity", []):
        if isinstance(child, vim.Folder):
            yield from _iter_network_entities(child, vim)
        else:
            yield child


def _extract_vlan_ids_from_dpg(dpg: Any, vim: Any) -> List[int]:
    spec = None
    cfg = getattr(dpg, "config", None)
    default_cfg = getattr(cfg, "defaultPortConfig", None)
    if default_cfg is not None:
        spec = getattr(default_cfg, "vlan", None)
    if spec is None:
        return []

    ids: List[int] = []
    if isinstance(spec, vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec):
        vid = getattr(spec, "vlanId", None)
        if isinstance(vid, int):
            ids.append(vid)
    elif isinstance(spec, vim.dvs.VmwareDistributedVirtualSwitch.TrunkVlanSpec):
        ranges = getattr(spec, "vlanId", []) or []
        for rng in ranges:
            start = int(getattr(rng, "start", -1))
            end = int(getattr(rng, "end", -1))
            if start > 0 and end >= start:
                ids.extend(list(range(start, end + 1)))
    return sorted(set(ids))


def _cluster_has_dpg_access(cluster_obj: Any, dpg: Any) -> bool:
    dpg_key = str(getattr(dpg, "key", ""))
    for host in getattr(cluster_obj, "host", []):
        runtime = getattr(host, "runtime", None)
        state = str(getattr(runtime, "connectionState", "")).lower()
        in_maintenance = bool(getattr(runtime, "inMaintenanceMode", False))
        if state != "connected" or in_maintenance:
            continue
        for net_obj in getattr(host, "network", []):
            if str(getattr(net_obj, "key", "")) == dpg_key:
                return True
    return False


def _resolve_vlan_bindings_for_cluster(
    dc_obj: Any,
    cluster_obj: Any,
    vlans: Sequence[str],
    vim: Any,
) -> List[NetworkBinding]:
    needed = sorted({int(v) for v in vlans})
    if not needed:
        return []

    dpg_candidates = []
    for net_obj in _iter_network_entities(dc_obj.networkFolder, vim):
        if not isinstance(net_obj, vim.dvs.DistributedVirtualPortgroup):
            continue
        vlan_ids = _extract_vlan_ids_from_dpg(net_obj, vim)
        if not vlan_ids:
            continue
        if not _cluster_has_dpg_access(cluster_obj, net_obj):
            continue
        dpg_candidates.append((net_obj, set(vlan_ids)))

    out: List[NetworkBinding] = []
    for vlan in needed:
        matched = [obj for obj, ids in dpg_candidates if vlan in ids]
        if not matched:
            raise RuntimeError(
                f"No accessible DPG found for VLAN {vlan} in {dc_obj.name}/{cluster_obj.name}"
            )
        chosen = sorted(matched, key=lambda x: str(x.name))[0]
        out.append(
            NetworkBinding(
                datacenter=str(dc_obj.name),
                cluster=str(cluster_obj.name),
                vlan=str(vlan),
                dpg_name=str(chosen.name),
                dpg_key=str(getattr(chosen, "key", "")),
                resolution_policy="vlan-id-exact-match-and-cluster-accessible",
            )
        )
    return out


def _plan_dry_run_vlan_bindings(accepted_rows: Sequence[NetworkRow]) -> List[NetworkBinding]:
    vm_rows = [r for r in accepted_rows if r.mode == "vm-provisioned"]
    keys = sorted({(r.datacenter, r.cluster, r.vlan) for r in vm_rows})
    return [
        NetworkBinding(
            datacenter=dc,
            cluster=cl,
            vlan=vlan,
            dpg_name=f"dry-run-vlan-{vlan}",
            dpg_key="dry-run",
            resolution_policy="dry-run-placeholder",
        )
        for dc, cl, vlan in keys
    ]


def plan_cluster_placements(accepted_rows: Sequence[NetworkRow], args: Any) -> List[PlacementChoice]:
    vm_rows = [r for r in accepted_rows if r.mode == "vm-provisioned"]
    unique_targets = sorted({(r.datacenter, r.cluster) for r in vm_rows})
    if not unique_targets:
        return []

    if not args.execute_vcenter:
        return [
            PlacementChoice(
                datacenter=dc,
                cluster=cl,
                host="dry-run-unresolved",
                datastore=args.datastore or "dry-run-unresolved",
                selection_policy="cluster-random-host-then-host-accessible-datastore",
            )
            for dc, cl in unique_targets
        ]

    try:
        from pyVmomi import vim
    except ImportError as exc:
        raise RuntimeError("pyvmomi is required for --execute-vcenter. Install: pip install pyvmomi") from exc

    rng = random.Random(args.random_seed)
    choices: List[PlacementChoice] = []
    with vcenter_session(args) as si:
        content = si.RetrieveContent()
        for datacenter, cluster in unique_targets:
            dc_obj = _find_datacenter(content, datacenter, vim)
            if dc_obj is None:
                raise RuntimeError(f"Datacenter not found: {datacenter}")
            cluster_obj = _find_cluster_in_datacenter(dc_obj, cluster, vim)
            if cluster_obj is None:
                raise RuntimeError(f"Cluster not found in datacenter {datacenter}: {cluster}")

            host_name, datastore_name = _choose_host_and_datastore(cluster_obj, args.datastore, rng)
            choices.append(
                PlacementChoice(
                    datacenter=datacenter,
                    cluster=cluster,
                    host=host_name,
                    datastore=datastore_name,
                    selection_policy="cluster-random-host-then-host-accessible-datastore",
                )
            )

    return choices


def plan_vlan_bindings(accepted_rows: Sequence[NetworkRow], args: Any) -> List[NetworkBinding]:
    vm_rows = [r for r in accepted_rows if r.mode == "vm-provisioned"]
    if not vm_rows:
        return []

    if not args.execute_vcenter:
        return _plan_dry_run_vlan_bindings(accepted_rows)

    try:
        from pyVmomi import vim
    except ImportError as exc:
        raise RuntimeError("pyvmomi is required for --execute-vcenter. Install: pip install pyvmomi") from exc

    targets: Dict[Tuple[str, str], List[str]] = {}
    for row in vm_rows:
        key = (row.datacenter, row.cluster)
        targets.setdefault(key, []).append(row.vlan)

    bindings: List[NetworkBinding] = []
    with vcenter_session(args) as si:
        content = si.RetrieveContent()
        for (datacenter, cluster), vlans in sorted(targets.items()):
            dc_obj = _find_datacenter(content, datacenter, vim)
            if dc_obj is None:
                raise RuntimeError(f"Datacenter not found: {datacenter}")
            cluster_obj = _find_cluster_in_datacenter(dc_obj, cluster, vim)
            if cluster_obj is None:
                raise RuntimeError(f"Cluster not found in datacenter {datacenter}: {cluster}")
            bindings.extend(_resolve_vlan_bindings_for_cluster(dc_obj, cluster_obj, vlans, vim))

    return bindings
