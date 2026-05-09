"""OVF deployment and post-deploy guest network configuration.

Deploys a pre-built OVF template (with open-vm-tools pre-installed) into
vCenter, maps the NIC to the target DPG, and configures static networking
via the VMware Guest Operations API — no ISO boot, no CD drives.
"""

from __future__ import annotations

import logging
import os
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import requests
import urllib3

logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_OVF_NS = "http://schemas.dmtf.org/ovf/envelope/1"


def read_ovf_network_name(ovf_path: str) -> str:
    """Return the first network name from the OVF NetworkSection."""
    tree = ET.parse(ovf_path)
    root = tree.getroot()
    for net in root.iter(f"{{{_OVF_NS}}}Network"):
        name = net.get(f"{{{_OVF_NS}}}name", "")
        if name:
            return name
    return ""


def _find_dpg_obj(content: Any, dpg_key: str, vim: Any) -> Optional[Any]:
    """Find a DistributedVirtualPortgroup object by portgroup key."""
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.dvs.DistributedVirtualPortgroup], True
    )
    try:
        for dpg in view.view:
            if str(getattr(dpg, "key", "")) == dpg_key:
                return dpg
    finally:
        view.Destroy()
    return None


def _upload_file(url: str, file_path: str, session_id: str = "") -> None:
    """Upload a file to an HttpNfcLease upload URL via HTTPS PUT.

    For VMDK files vSphere NFC expects Content-Type application/x-vnd.vmware-streamVmdk.
    The NFC ticket is embedded in the URL path — no extra auth header is needed.
    Must NOT have Expect: 100-continue and must have explicit Content-Length.
    """
    url = url.replace("*", "0")

    # Choose correct Content-Type based on file extension.
    if file_path.lower().endswith(".vmdk"):
        content_type = "application/x-vnd.vmware-streamVmdk"
    else:
        content_type = "application/octet-stream"

    file_size = os.path.getsize(file_path)
    with open(file_path, "rb") as fh:
        session = requests.Session()
        req = requests.Request(
            "PUT",
            url,
            data=fh,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(file_size),
                # vSphere NFC pre-creates the VMDK stub during ImportVApp; this
                # header instructs the NFC server to overwrite the existing file.
                "Overwrite": "t",
            },
        )
        prepped = session.prepare_request(req)
        prepped.headers.pop("Expect", None)
        resp = session.send(prepped, verify=False, timeout=(30, 1200))

    if resp.status_code not in (200, 201):
        body_preview = resp.text[:300] if resp.text else ""
        raise RuntimeError(
            f"OVF file upload failed: HTTP {resp.status_code} for {os.path.basename(file_path)}"
            + (f" — {body_preview}" if body_preview else "")
        )


def _lease_keepalive(lease: Any, stop_event: threading.Event) -> None:
    """Send periodic Progress() heartbeats so the HttpNfcLease does not expire."""
    while not stop_event.is_set():
        try:
            lease.HttpNfcLeaseProgress(50)
        except Exception:
            pass
        stop_event.wait(30)


def deploy_ovf_vm(
    si: Any,       # pyVmomi ServiceInstance (unused directly but kept for API symmetry)
    content: Any,
    vim: Any,
    ovf_path: str,
    vm_name: str,
    resource_pool: Any,
    datastore_obj: Any,
    vm_folder: Any,
    host_obj: Any,
    dpg_key: str,
) -> Any:
    """Deploy an OVF template as a new (powered-off) VM.

    Maps the first OVF network to the DPG identified by *dpg_key*.
    Returns the deployed ``vim.VirtualMachine`` managed object.
    """
    ovf_dir = os.path.dirname(os.path.abspath(ovf_path))
    with open(ovf_path, "r", encoding="utf-8") as fh:
        ovf_content = fh.read()

    ovf_network_name = read_ovf_network_name(ovf_path)

    # Find the target DPG object for network mapping.
    dpg_obj = _find_dpg_obj(content, dpg_key, vim)
    if dpg_obj is None:
        raise RuntimeError(f"DistributedVirtualPortgroup not found for key: {dpg_key!r}")

    # Build the import spec.
    cisp = vim.OvfManager.CreateImportSpecParams()
    cisp.entityName = vm_name
    cisp.diskProvisioning = vim.OvfManager.CreateImportSpecParams.DiskProvisioningType.thin

    if ovf_network_name:
        nm = vim.OvfManager.NetworkMapping()
        nm.name = ovf_network_name
        nm.network = dpg_obj
        cisp.networkMapping = [nm]

    spec_result = content.ovfManager.CreateImportSpec(
        ovf_content, resource_pool, datastore_obj, cisp
    )
    if getattr(spec_result, "error", None):
        raise RuntimeError(f"OVF CreateImportSpec errors: {spec_result.error}")

    import_spec = spec_result.importSpec
    # Override the entity name so it matches our run-id naming scheme.
    import_spec.configSpec.name = vm_name

    # Kick off the import — returns an HttpNfcLease.
    lease = resource_pool.ImportVApp(import_spec, vm_folder, host_obj)

    # Wait for lease to reach 'ready' state.
    deadline = time.time() + 120
    while time.time() < deadline:
        state = str(getattr(lease, "state", ""))
        if state == "ready":
            break
        if state == "error":
            raise RuntimeError(
                f"HttpNfcLease error: {getattr(lease, 'error', 'unknown')}"
            )
        time.sleep(1)
    else:
        raise RuntimeError("HttpNfcLease did not reach 'ready' state within 120 s")

    # Get the vCenter session key for NFC upload authentication.
    session_id = str(si.content.sessionManager.currentSession.key)

    # Map importKey → upload URL from the lease.
    device_urls: dict[str, str] = {}
    for du in getattr(lease.info, "deviceUrl", []):
        device_urls[str(du.importKey)] = str(du.url)

    # Start keepalive heartbeat thread.
    stop_evt = threading.Event()
    ka_thread = threading.Thread(
        target=_lease_keepalive, args=(lease, stop_evt), daemon=True
    )
    ka_thread.start()

    try:
        file_items = getattr(spec_result, "fileItem", []) or []
        for file_item in file_items:
            dev_id = str(file_item.deviceId)
            file_name = str(file_item.path)
            upload_url = device_urls.get(dev_id, "")
            if not upload_url:
                continue
            file_path = os.path.join(ovf_dir, file_name)
            if not os.path.isfile(file_path):
                raise RuntimeError(f"OVF referenced file not found: {file_path}")
            size_mb = os.path.getsize(file_path) // (1024 * 1024)
            logger.info("Uploading %s (%s MB) to %s...", file_name, size_mb, upload_url[:80])
            _upload_file(upload_url, file_path, session_id)

        stop_evt.set()
        ka_thread.join(timeout=5)
        lease.HttpNfcLeaseProgress(100)
        lease.HttpNfcLeaseComplete()

    except Exception:
        stop_evt.set()
        ka_thread.join(timeout=5)
        # Abort the lease so vCenter starts its own cleanup.
        try:
            lease.HttpNfcLeaseAbort()
        except Exception:
            pass
        # Explicitly destroy the partial VM so vCenter removes the datastore
        # directory.  lease.info.entity is the half-created VM object.
        try:
            from nettest.vcenter_utils import wait_for_task as _wft
            partial_vm = lease.info.entity
            if partial_vm is not None:
                ps = str(getattr(getattr(partial_vm, "runtime", None), "powerState", ""))
                if ps != "poweredOff":
                    try:
                        _wft(partial_vm.PowerOffVM_Task())
                    except Exception:
                        pass
                _wft(partial_vm.Destroy_Task())
        except Exception:
            pass
        raise

    return lease.info.entity


def write_probe_guestinfo(
    vim: Any,
    vm_obj: Any,
    ip_address: str,
    prefix_len: int,
    gateway: str,
    targets: List[str],
    ready: str = "wait",
) -> None:
    """Write probe parameters into VM extraConfig (guestinfo keys) via ReconfigVM_Task.

    Does NOT require vgauth — this is a pure vCenter API call.
    The VM's /etc/local.d/netprobe.start startup script reads these keys via:
        vmtoolsd --cmd "info-get guestinfo.nettest.ip"
    and writes results back to guestinfo.nettest.results / guestinfo.nettest.status.

    `ready` controls the probe trigger:
      "wait"  — VM will configure network then block until ready changes to "go"
      "go"    — VM will proceed immediately (use for cross-subnet VMs with no peers)
    """
    from nettest.vcenter_utils import wait_for_task

    extra = [
        vim.option.OptionValue(key="guestinfo.nettest.ip",      value=ip_address),
        vim.option.OptionValue(key="guestinfo.nettest.prefix",  value=str(prefix_len)),
        vim.option.OptionValue(key="guestinfo.nettest.gw",      value=gateway),
        vim.option.OptionValue(key="guestinfo.nettest.targets", value=",".join(targets)),
        vim.option.OptionValue(key="guestinfo.nettest.ready",   value=ready),
        vim.option.OptionValue(key="guestinfo.nettest.status",  value="pending"),
        vim.option.OptionValue(key="guestinfo.nettest.results", value=""),
    ]
    spec = vim.vm.ConfigSpec()
    spec.extraConfig = extra
    wait_for_task(vm_obj.ReconfigVM_Task(spec))


def write_probe_ready(vim: Any, vm_obj: Any) -> None:
    """Flip guestinfo.nettest.ready=go to unblock netprobe.start.

    Targets are already written before power-on via write_probe_guestinfo.
    This call only changes the ready flag so there is no vmtoolsd cache race.
    """
    from nettest.vcenter_utils import wait_for_task

    extra = [
        vim.option.OptionValue(key="guestinfo.nettest.ready", value="go"),
    ]
    spec = vim.vm.ConfigSpec()
    spec.extraConfig = extra
    wait_for_task(vm_obj.ReconfigVM_Task(spec))


def wait_for_probe_status(
    vm_obj: Any,
    target_status: str,
    timeout_sec: int = 300,
) -> bool:
    """Block until guestinfo.nettest.status equals *target_status* or timeout.

    Returns True when the status is reached, False on timeout.
    Used to confirm a VM's network is fully configured before signalling go.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        extra = {
            e.key: e.value
            for e in (
                getattr(getattr(vm_obj, "config", None), "extraConfig", None) or []
            )
        }
        status = extra.get("guestinfo.nettest.status", "")
        if status == target_status:
            return True
        if str(status).startswith("error:"):
            logger.warning("VM %s reported error status: %s", vm_obj.name, status)
            return False
        time.sleep(3)
    logger.warning(
        "Timed out waiting for probe status '%s' on %s after %ss",
        target_status, vm_obj.name, timeout_sec,
    )
    return False


def poll_probe_results(
    vm_obj: Any,
    timeout_sec: int = 180,
) -> Optional[Dict[str, str]]:
    """Poll extraConfig until guestinfo.nettest.status == 'done', then parse results.

    Returns a dict of {target_ip: "PASS" | "FAIL"}, or None on timeout/error.
    Results string format (written by the VM): "ip1:PASS,ip2:FAIL,..."
    """
    deadline = time.time() + timeout_sec
    last_status = ""
    while time.time() < deadline:
        extra = {
            e.key: e.value
            for e in (
                getattr(getattr(vm_obj, "config", None), "extraConfig", None) or []
            )
        }
        status = extra.get("guestinfo.nettest.status", "pending")
        if status != last_status:
            logger.debug("probe status: %s", status)
            last_status = status
        if status == "done":
            raw = extra.get("guestinfo.nettest.results", "")
            results: Dict[str, str] = {}
            for pair in raw.split(","):
                pair = pair.strip()
                if ":" in pair:
                    ip, outcome = pair.split(":", 1)
                    results[ip.strip()] = outcome.strip().upper()
            return results
        if str(status).startswith("error:"):
            logger.error("VM probe script error: %s", status)
            return None
        time.sleep(5)
    logger.warning("Timed out waiting for probe results on %s after %ss", vm_obj.name, timeout_sec)
    return None


# ── Linked-clone helpers ───────────────────────────────────────────────────────

def take_vm_snapshot(vim: Any, vm_obj: Any, snap_name: str = "nettest-base") -> Any:
    """Take a memory-less snapshot and return the snapshot moRef.

    The VM must be powered off before calling this so no quiesce is needed.
    """
    from nettest.vcenter_utils import wait_for_task
    wait_for_task(
        vm_obj.CreateSnapshot_Task(
            name=snap_name,
            description="nettest linked-clone base snapshot",
            memory=False,
            quiesce=False,
        ),
        timeout_sec=120,
    )

    def _find(snap_list: list, name: str) -> Any:
        for s in snap_list:
            if s.name == name:
                return s.snapshot
            found = _find(s.childSnapshotList, name)
            if found:
                return found
        return None

    snap_ref = _find(
        getattr(getattr(vm_obj, "snapshot", None), "rootSnapshotList", []),
        snap_name,
    )
    if snap_ref is None:
        raise RuntimeError(f"Snapshot '{snap_name}' not found on {vm_obj.name} after creation")
    return snap_ref


def clone_vm_linked(
    vim: Any,
    content: Any,
    template_vm: Any,
    snapshot_ref: Any,
    vm_name: str,
    host_obj: Any,
    datastore_obj: Any,
    resource_pool: Any,
    vm_folder: Any,
    dpg_key: str,
) -> Any:
    """Create a linked clone from *template_vm* at *snapshot_ref*.

    The clone is placed on *host_obj* / *datastore_obj* with a delta disk.
    After cloning, the first ethernet NIC is reconfigured to *dpg_key*.
    Returns the powered-off cloned ``vim.VirtualMachine`` object.
    """
    from nettest.vcenter_utils import wait_for_task

    relocate = vim.vm.RelocateSpec()
    relocate.diskMoveType = "createNewChildDiskBacking"  # linked clone — delta disk only
    relocate.host = host_obj
    relocate.datastore = datastore_obj
    relocate.pool = resource_pool

    clone_spec = vim.vm.CloneSpec()
    clone_spec.location = relocate
    clone_spec.snapshot = snapshot_ref
    clone_spec.powerOn = False
    clone_spec.template = False

    cloned_vm = wait_for_task(
        template_vm.CloneVM_Task(folder=vm_folder, name=vm_name, spec=clone_spec),
        timeout_sec=300,
    )
    if cloned_vm is None:
        raise RuntimeError(f"CloneVM_Task returned no result for '{vm_name}'")

    # Reconfigure the first ethernet NIC to the target portgroup.
    dpg_obj = _find_dpg_obj(content, dpg_key, vim)
    if dpg_obj is None:
        raise RuntimeError(f"DistributedVirtualPortgroup not found for key: {dpg_key!r}")

    dvs_uuid = str(dpg_obj.config.distributedVirtualSwitch.uuid)

    nic_device = None
    for dev in getattr(getattr(cloned_vm, "config", None), "hardware", None) and \
               getattr(cloned_vm.config.hardware, "device", []) or []:
        if isinstance(dev, vim.vm.device.VirtualEthernetCard):
            nic_device = dev
            break

    if nic_device is not None:
        backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
        backing.port = vim.dvs.PortConnection()
        backing.port.portgroupKey = dpg_obj.key
        backing.port.switchUuid = dvs_uuid
        nic_device.backing = backing

        nic_spec = vim.vm.device.VirtualDeviceSpec()
        nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
        nic_spec.device = nic_device

        reconfig = vim.vm.ConfigSpec()
        reconfig.deviceChange = [nic_spec]
        wait_for_task(cloned_vm.ReconfigVM_Task(reconfig), timeout_sec=60)

    return cloned_vm
