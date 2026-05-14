from __future__ import annotations

import atexit
import ssl
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional, Tuple


def connect_vcenter(args: Any) -> Any:
    """Create and return a vCenter service instance.

    Uses session cookie when ``args.vcenter_session_id`` is set (plugin mode),
    otherwise falls back to explicit username/password credentials.
    """
    session_id = getattr(args, "vcenter_session_id", "")
    if session_id:
        return connect_vcenter_with_session(
            host=str(args.vcenter_host),
            soap_session_id=session_id,
        )
    return connect_vcenter_direct(
        host=str(args.vcenter_host),
        user=str(args.vcenter_user),
        pwd=str(args.vcenter_password),
    )


def connect_vcenter_with_session(host: str, soap_session_id: str) -> Any:
    """Connect to vCenter by reusing an existing SOAP session cookie.

    The vSphere HTML5 Client passes the user's vmware_soap_session when
    loading a remote plugin.  This avoids re-entering credentials.
    """
    try:
        from pyVim import connect as _connect
        from pyVmomi import vim
    except ImportError as exc:
        raise RuntimeError("pyvmomi is required. Install: pip install pyvmomi") from exc

    ssl_ctx = ssl._create_unverified_context()
    stub = _connect.SoapStubAdapter(host=host, port=443, sslContext=ssl_ctx)
    si = vim.ServiceInstance("ServiceInstance", stub)
    # Inject the existing session cookie — pyVmomi honours this on subsequent calls
    stub.cookie = f'vmware_soap_session="{soap_session_id}"; Path=/; Secure; HttpOnly;'
    # Force a call to validate the session; raises if the cookie is stale/invalid
    _ = si.content  # noqa: F841
    atexit.register(_connect.Disconnect, si)
    return si


def connect_vcenter_direct(host: str, user: str, pwd: str) -> Any:
    """Create and return a vCenter service instance using explicit credentials."""
    try:
        from pyVim.connect import Disconnect, SmartConnect
    except ImportError as exc:
        raise RuntimeError("pyvmomi is required. Install: pip install pyvmomi") from exc

    ssl_ctx = ssl._create_unverified_context()
    si = SmartConnect(host=host, user=user, pwd=pwd, sslContext=ssl_ctx)
    atexit.register(Disconnect, si)
    return si


def connect_vcenter_auto(host: str, user: str = "", pwd: str = "", session_id: str = "") -> Any:
    """Connect via session cookie (plugin mode) or explicit credentials."""
    if session_id:
        return connect_vcenter_with_session(host=host, soap_session_id=session_id)
    return connect_vcenter_direct(host=host, user=user, pwd=pwd)


def disconnect_vcenter(si: Any) -> None:
    """Close an existing vCenter service instance connection."""
    try:
        from pyVim.connect import Disconnect
    except ImportError:
        return
    try:
        Disconnect(si)
    except Exception:
        pass


def wait_for_task(task: Any, timeout_sec: int = 180) -> Any:
    """Wait for a vSphere task, raise on error/timeout, return task result."""
    start = time.time()
    while True:
        info = getattr(task, "info", None)
        state = str(getattr(info, "state", ""))
        if state == "success":
            return getattr(info, "result", None)
        if state == "error":
            err = getattr(info, "error", None)
            msg = getattr(err, "msg", "task-failed") if err else "task-failed"
            raise RuntimeError(str(msg))
        if (time.time() - start) > timeout_sec:
            raise RuntimeError("task-timeout")
        time.sleep(1)


def get_all_vms_by_moid(content: Any, vim: Any) -> Dict[str, Any]:
    """Return a dict of moid -> VirtualMachine for every VM visible to this session."""
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        return {str(getattr(vm, "_moId", "")): vm for vm in view.view}
    finally:
        view.Destroy()


def delete_vm(
    vm_obj: Any,
    vm_prefix: str,
    timeout_poweroff: int = 60,
    timeout_destroy: int = 120,
) -> Tuple[bool, str]:
    """Power off (if needed) and destroy a VM.

    Safety: refuses to act if the VM name does not start with *vm_prefix*.

    Returns:
        (success, reason)  — reason is empty string on success.
    """
    vm_name = str(getattr(vm_obj, "name", ""))
    if not vm_name.startswith(vm_prefix):
        return False, "prefix-mismatch"

    try:
        ps = str(getattr(getattr(vm_obj, "runtime", None), "powerState", ""))
        if ps != "poweredOff":
            task = vm_obj.PowerOffVM_Task()
            deadline = time.time() + timeout_poweroff
            while time.time() < deadline:
                if str(getattr(task.info, "state", "")) in ("success", "error"):
                    break
                time.sleep(1)

        task = vm_obj.Destroy_Task()
        deadline = time.time() + timeout_destroy
        while time.time() < deadline:
            if str(getattr(task.info, "state", "")) in ("success", "error"):
                break
            time.sleep(1)

        if str(getattr(task.info, "state", "")) == "success":
            return True, ""
        return False, "destroy-task-did-not-succeed"
    except Exception as exc:
        return False, str(exc)


@contextmanager
def vcenter_session(args: Any):
    """Context manager for vCenter session lifecycle (from args namespace)."""
    si = connect_vcenter(args)
    try:
        yield si
    finally:
        disconnect_vcenter(si)
