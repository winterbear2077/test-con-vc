"""Diskless VM provisioning via the nettest mini ISO.

Each test VM is created fresh from scratch with *no virtual disk* and boots
directly into the static ``netprobe`` binary from a small (~13 MB) CDROM ISO.

The ISO contains only:
  - Alpine virt kernel (``vmlinuz-virt``)
  - Custom initramfs: busybox-static + ``netprobe`` + vmci/vsock kernel modules
  - isolinux bootloader

There is no Alpine rootfs, no apkovl, no modloop.  PID 1 (``/init``) loads the
vmci/vsock modules and immediately ``exec``s ``/usr/local/bin/netprobe``.

Workflow:
  1. Build the ISO once on the controller:
         ovf/build_mini_iso.sh [output.iso]   # default: ovf/nettest-mini.iso
  2. Call ``ensure_iso_on_datastore()`` once per cluster to upload the ISO to
     vCenter so every host in the cluster can read it.
  3. Call ``create_memboot_vm()`` per test VM: creates the VM config (no disk,
     CDROM backed by the datastore ISO), attaches the probe communication device
     (serial port or VMCI depending on ``poll_method``), and powers on the VM.

The probe communication phases in ``provisioning.py`` are shared between the OVF
and memboot paths — only the VM creation step differs.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

import requests
import urllib3

logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── ISO datastore upload ───────────────────────────────────────────────────────

def _ds_path_to_url(vcenter_host: str, datacenter_name: str, ds_path: str) -> str:
    """Convert a ``[DatastoreName] path/to/file.iso`` path to an HTTPS upload URL."""
    # ds_path format: "[DatastoreName] dir/file.iso"
    bracket_end = ds_path.index("]")
    ds_name = ds_path[1:bracket_end].strip()
    file_path = ds_path[bracket_end + 1:].strip()
    encoded_ds   = requests.utils.quote(ds_name,   safe="")
    encoded_path = requests.utils.quote(file_path, safe="/")
    return (
        f"https://{vcenter_host}/folder/{encoded_path}"
        f"?dcPath={requests.utils.quote(datacenter_name, safe='')}"
        f"&dsName={encoded_ds}"
    )


def ensure_iso_on_datastore(
    si: Any,
    vcenter_host: str,
    datacenter_name: str,
    datastore_name: str,
    local_iso_path: str,
    remote_dir: str = "nettest-iso",
) -> str:
    """Upload the memboot ISO to a datastore if not already present.

    Returns the datastore path string ``[DatastoreName] dir/file.iso`` which
    can be used directly in a ``VirtualCdrom.IsoBackingInfo``.

    If *local_iso_path* already looks like a datastore path (starts with ``[``)
    it is returned as-is — the caller pre-staged the ISO themselves.
    """
    if local_iso_path.startswith("["):
        logger.info("ISO path is already a datastore path: %s", local_iso_path)
        return local_iso_path

    iso_name = os.path.basename(local_iso_path)
    ds_path  = f"[{datastore_name}] {remote_dir}/{iso_name}"
    url      = _ds_path_to_url(vcenter_host, datacenter_name, ds_path)

    session_key = str(si.content.sessionManager.currentSession.key)
    file_size   = os.path.getsize(local_iso_path)

    logger.info(
        "Uploading ISO %s (%d MB) to %s...",
        iso_name, file_size // (1024 * 1024), ds_path,
    )

    with open(local_iso_path, "rb") as fh:
        sess = requests.Session()
        req  = requests.Request(
            "PUT", url, data=fh,
            headers={
                "Content-Type":   "application/octet-stream",
                "Content-Length": str(file_size),
                "Cookie":         f"vmware_soap_session={session_key}",
                "Overwrite":      "t",
            },
        )
        prepped = sess.prepare_request(req)
        prepped.headers.pop("Expect", None)
        resp = sess.send(prepped, verify=False, timeout=(30, 600))

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"ISO upload failed: HTTP {resp.status_code} for {iso_name}"
            + (f" — {resp.text[:200]}" if resp.text else "")
        )

    logger.info("ISO uploaded: %s", ds_path)
    return ds_path


# ── VM creation ────────────────────────────────────────────────────────────────

def create_memboot_vm(
    vim: Any,
    content: Any,
    vm_name: str,
    host_obj: Any,
    datastore_obj: Any,
    resource_pool: Any,
    vm_folder: Any,
    dpg_key: str,
    iso_ds_path: str,
    # probe communication channel
    poll_method: str = "guestinfo",
    # serial: controller_ip + port (used only when poll_method=="serial")
    controller_ip: str = "",
    serial_port: int = 0,
    # hardware sizing
    memory_mb: int = 256,
    num_cpus: int = 1,
) -> Any:
    """Create and power on a diskless Alpine memboot VM.

    The VM has:
    - No virtual disk (boots entirely into RAM)
    - A SATA CDROM backed by *iso_ds_path*
    - One vmxnet3 NIC connected to the DPG identified by *dpg_key*
    - If ``poll_method="serial"``: a ``VirtualSerialPort`` (TCP client) added
      before power-on so the VM can reach the controller's probe server
    - If ``poll_method="vsock"``: a ``VirtualVMCIDevice`` added before power-on

    Returns the powered-on ``vim.VirtualMachine`` managed object.
    """
    from nettest.vcenter_utils import wait_for_task
    from nettest.ovf_deploy import _find_dpg_obj

    # ── Find the target DPG ───────────────────────────────────────────────────
    dpg_obj = _find_dpg_obj(content, dpg_key, vim)
    if dpg_obj is None:
        raise RuntimeError(f"DistributedVirtualPortgroup not found for key: {dpg_key!r}")
    dvs_uuid = str(dpg_obj.config.distributedVirtualSwitch.uuid)

    # ── Build VM config spec ──────────────────────────────────────────────────
    vm_file_info = vim.vm.FileInfo()
    vm_file_info.vmPathName = f"[{datastore_obj.name}]"

    devices: list = []
    next_key = -100  # use negative keys for new devices

    # SATA controller
    sata = vim.vm.device.VirtualAHCIController()
    sata.key           = next_key; next_key -= 1
    sata.busNumber     = 0
    sata.sharedBus     = vim.vm.device.VirtualSCSIController.Sharing.noSharing
    sata_spec          = vim.vm.device.VirtualDeviceSpec()
    sata_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
    sata_spec.device   = sata
    devices.append(sata_spec)
    sata_key = sata.key

    # CDROM on SATA
    cdrom = vim.vm.device.VirtualCdrom()
    cdrom.key            = next_key; next_key -= 1
    cdrom.controllerKey  = sata_key
    cdrom.unitNumber     = 0
    iso_backing          = vim.vm.device.VirtualCdrom.IsoBackingInfo()
    iso_backing.fileName = iso_ds_path
    cdrom.backing        = iso_backing
    conn                 = vim.vm.device.VirtualDevice.ConnectInfo()
    conn.startConnected  = True
    conn.connected       = True
    cdrom.connectable    = conn
    cdrom_spec           = vim.vm.device.VirtualDeviceSpec()
    cdrom_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
    cdrom_spec.device    = cdrom
    devices.append(cdrom_spec)

    # vmxnet3 NIC connected to target DPG
    nic         = vim.vm.device.VirtualVmxnet3()
    nic.key     = next_key; next_key -= 1
    nic_backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
    nic_port    = vim.dvs.PortConnection()
    nic_port.portgroupKey = dpg_obj.key
    nic_port.switchUuid   = dvs_uuid
    nic_backing.port      = nic_port
    nic.backing           = nic_backing
    nic_conn              = vim.vm.device.VirtualDevice.ConnectInfo()
    nic_conn.startConnected = True
    nic_conn.connected      = True
    nic.connectable         = nic_conn
    nic_spec                = vim.vm.device.VirtualDeviceSpec()
    nic_spec.operation      = vim.vm.device.VirtualDeviceSpec.Operation.add
    nic_spec.device         = nic
    devices.append(nic_spec)

    # Serial port (TCP client) — only for serial poll_method
    if poll_method == "serial":
        if not controller_ip or not serial_port:
            raise ValueError("controller_ip and serial_port are required for poll_method='serial'")
        serial_dev                     = vim.vm.device.VirtualSerialPort()
        serial_dev.key                 = next_key; next_key -= 1
        serial_backing                 = vim.vm.device.VirtualSerialPort.URIBackingInfo()
        serial_backing.serviceURI      = f"tcp://{controller_ip}:{serial_port}"
        serial_backing.direction       = "client"
        serial_backing.proxyURI        = ""
        serial_dev.backing             = serial_backing
        serial_dev.yieldOnPoll         = True
        serial_spec                    = vim.vm.device.VirtualDeviceSpec()
        serial_spec.operation          = vim.vm.device.VirtualDeviceSpec.Operation.add
        serial_spec.device             = serial_dev
        devices.append(serial_spec)
        logger.debug("Adding serial port TCP client -> %s:%s", controller_ip, serial_port)

    # VMCI device — only for vsock poll_method
    if poll_method == "vsock":
        vmci_dev                              = vim.vm.device.VirtualVMCIDevice()
        vmci_dev.key                          = next_key; next_key -= 1
        vmci_dev.allowUnrestrictedCommunication = False
        vmci_spec                             = vim.vm.device.VirtualDeviceSpec()
        vmci_spec.operation                   = vim.vm.device.VirtualDeviceSpec.Operation.add
        vmci_spec.device                      = vmci_dev
        devices.append(vmci_spec)
        logger.debug("Adding VMCI device for vsock")

    # Boot order: CDROM first
    boot_order = [vim.vm.BootOptions.BootableCdromDevice()]

    config_spec                  = vim.vm.ConfigSpec()
    config_spec.name             = vm_name
    config_spec.numCPUs          = num_cpus
    config_spec.memoryMB         = memory_mb
    config_spec.guestId          = "other6xLinux64Guest"
    config_spec.files            = vm_file_info
    config_spec.deviceChange     = devices
    config_spec.bootOptions      = vim.vm.BootOptions(bootOrder=boot_order)
    # Firmware: BIOS (Alpine virt ISO supports both; BIOS avoids Secure Boot complications)
    config_spec.firmware         = "bios"

    # ── Create the VM (powered off) ───────────────────────────────────────────
    logger.info(
        "Creating memboot VM '%s' (iso=%s, poll=%s, host=%s)...",
        vm_name, iso_ds_path, poll_method, host_obj.name,
    )
    vm_obj = wait_for_task(
        vm_folder.CreateVM_Task(
            config=config_spec,
            pool=resource_pool,
            host=host_obj,
        ),
        timeout_sec=120,
    )
    if vm_obj is None:
        raise RuntimeError(f"CreateVM_Task returned no result for '{vm_name}'")

    logger.info("Created memboot VM: %s (moid=%s)", vm_name, getattr(vm_obj, "_moId", "?"))
    return vm_obj
