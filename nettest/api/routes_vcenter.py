"""API routes: /api/vcenter/*, /plugin.json, /plugin.zip"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from nettest.vcenter_utils import connect_vcenter_auto, disconnect_vcenter
from nettest.api.deps import _read_config, _write_config

router = APIRouter()

_PLUGIN_KEY     = "com.nettest.vcnet"
_PLUGIN_NAME    = "vCenter Network Test"
_PLUGIN_VERSION = "1.0.0"


# ── vCenter Inventory ─────────────────────────────────────────────────────────
@router.get("/api/vcenter/inventory")
def api_vcenter_inventory(x_vcenter_session: str | None = Header(default=None)):
    """Return datacenters, clusters, and distributed/standard portgroups from vCenter."""
    cfg = _read_config()
    host = str(cfg.get("vcenter_host", "")).strip()

    if not host:
        raise HTTPException(status_code=400, detail="vcenter_host must be set in Config first")

    try:
        from pyVmomi import vim
    except ImportError:
        raise HTTPException(status_code=500, detail="pyvmomi not installed")

    try:
        if x_vcenter_session:
            si = connect_vcenter_auto(host=host, session_id=x_vcenter_session)
        else:
            user = str(cfg.get("vcenter_user", "")).strip()
            pwd  = str(cfg.get("vcenter_password", "")).strip()
            if not user:
                raise HTTPException(status_code=400, detail="vcenter_user must be set in Config first")
            si = connect_vcenter_auto(host=host, user=user, pwd=pwd)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"vCenter connection failed: {exc}")

    try:
        content = si.RetrieveContent()
        result: dict = {"datacenters": [], "clusters": {}, "portgroups": {}}

        def _pg_vlan(obj) -> str:
            cfg_obj = getattr(obj, "config", None)
            if not cfg_obj:
                return ""
            dp = getattr(cfg_obj, "defaultPortConfig", None)
            if not dp:
                return ""
            vlan_cfg = getattr(dp, "vlan", None)
            if vlan_cfg is None:
                return ""
            if hasattr(vlan_cfg, "vlanId") and isinstance(vlan_cfg.vlanId, int):
                return str(vlan_cfg.vlanId)
            if hasattr(vlan_cfg, "vlanId"):
                ranges = vlan_cfg.vlanId
                if hasattr(ranges, "__iter__"):
                    parts = []
                    for r in ranges:
                        s, e = getattr(r, "start", "?"), getattr(r, "end", "?")
                        parts.append(str(s) if s == e else f"{s}-{e}")
                    return ",".join(parts) if parts else "trunk"
            return ""

        def _iter_folder(folder):
            for child in (getattr(folder, "childEntity", None) or []):
                if isinstance(child, vim.Folder):
                    yield from _iter_folder(child)
                else:
                    yield child

        for dc_obj in _iter_folder(content.rootFolder):
            if not isinstance(dc_obj, vim.Datacenter):
                continue
            dc_name = str(dc_obj.name)
            result["datacenters"].append(dc_name)
            result["clusters"][dc_name] = []
            result["portgroups"][dc_name] = {}

            dc_dpg_map: dict = {}
            for net_obj in _iter_folder(dc_obj.networkFolder):
                if not isinstance(net_obj, vim.dvs.DistributedVirtualPortgroup):
                    continue
                pg_name = str(net_obj.name)
                if getattr(getattr(net_obj, "config", None), "uplink", False):
                    continue
                dpg_key = str(getattr(net_obj, "key", ""))
                if dpg_key:
                    dc_dpg_map[dpg_key] = {"name": pg_name, "vlan": _pg_vlan(net_obj)}

            for compute_obj in _iter_folder(dc_obj.hostFolder):
                if not isinstance(compute_obj, (vim.ClusterComputeResource, vim.ComputeResource)):
                    continue
                cl_name = str(compute_obj.name)
                result["clusters"][dc_name].append(cl_name)

                accessible_keys: set = set()
                hosts = getattr(compute_obj, "host", []) or []
                for host in hosts:
                    runtime = getattr(host, "runtime", None)
                    if str(getattr(runtime, "connectionState", "")).lower() != "connected":
                        continue
                    if bool(getattr(runtime, "inMaintenanceMode", False)):
                        continue
                    for net in (getattr(host, "network", None) or []):
                        k = str(getattr(net, "key", ""))
                        if k in dc_dpg_map:
                            accessible_keys.add(k)

                pg_list = [dc_dpg_map[k] for k in accessible_keys]
                pg_list.sort(key=lambda x: (
                    0 if x["vlan"].isdigit() else 1,
                    int(x["vlan"]) if x["vlan"].isdigit() else 0,
                    x["name"],
                ))
                result["portgroups"][dc_name][cl_name] = pg_list

        return result
    finally:
        disconnect_vcenter(si)


# ── Plugin manifest helpers ───────────────────────────────────────────────────
def _plugin_manifest_dict() -> dict:
    return {
        "manifestVersion": "1.2.0",
        "requirements": {"plugin.api.version": "1.2.0"},
        "configuration": {
            "nameKey": "plugin.info.name",
            "icon": {"name": "network-globe"},
        },
        "global": {"view": {"uri": "index.html?plugin=1"}},
        "definitions": {
            "i18n": {
                "locales": ["en-US"],
                "definitions": {
                    "plugin.info.name": {"en-US": _PLUGIN_NAME},
                    "plugin.nav.label": {"en-US": _PLUGIN_NAME},
                },
            },
        },
    }


@router.get("/plugin.json", include_in_schema=False)
def plugin_manifest():
    return JSONResponse(_plugin_manifest_dict())


@router.get("/plugin.zip", include_in_schema=False)
def plugin_zip():
    manifest_bytes = json.dumps(_plugin_manifest_dict(), indent=2).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("plugin.json", manifest_bytes)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="plugin.zip"'},
    )


# ── Plugin registration ───────────────────────────────────────────────────────
def _fetch_ssl_thumbprint(url: str) -> str:
    import ssl
    import socket
    import hashlib
    from urllib.parse import urlparse
    p = urlparse(url)
    host = p.hostname or "127.0.0.1"
    port = p.port or 443
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=5) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            der = ssock.getpeercert(binary_form=True)
    if not der:
        raise ValueError("Could not retrieve peer certificate")
    sha1 = hashlib.sha1(der).hexdigest().upper()
    return ":".join(sha1[i:i+2] for i in range(0, len(sha1), 2))


def _vcenter_extension_obj(vim, plugin_url: str, ext_key: str, ext_name: str, version: str, thumbprint: str):
    desc = vim.Description(label=ext_name, summary=f"{ext_name} — Network Policy Test")
    client = vim.Extension.ClientInfo(
        url=f"{plugin_url}/plugin.zip",
        company="NetTest",
        version=version,
        description=vim.Description(label=ext_name, summary=ext_name),
        type="vsphere-client-remote",
    )
    server = vim.Extension.ServerInfo(
        url=plugin_url,
        description=vim.Description(label=ext_name, summary=ext_name),
        company="NetTest",
        type="SOAP",
        adminEmail=["admin@nettest.local"],
        serverThumbprint=thumbprint,
    )
    return vim.Extension(
        key=ext_key,
        description=desc,
        version=version,
        client=[client],
        server=[server],
        lastHeartbeatTime=datetime.now(timezone.utc),
    )


class PluginRegisterIn(BaseModel):
    plugin_url: str
    plugin_key: str = _PLUGIN_KEY
    plugin_name: str = _PLUGIN_NAME
    plugin_version: str = _PLUGIN_VERSION
    ssl_thumbprint: str = ""


@router.post("/api/vcenter/plugin/register")
def api_plugin_register(body: PluginRegisterIn, x_vcenter_session: str = Header(default="")):
    cfg = _read_config()
    vcenter_host     = cfg.get("vcenter_host", "")
    vcenter_user     = cfg.get("vcenter_user", "")
    vcenter_password = cfg.get("vcenter_password", "")
    session_id = x_vcenter_session.strip()
    if not vcenter_host:
        raise HTTPException(400, "vCenter host not configured")
    if not session_id and (not vcenter_user or not vcenter_password):
        raise HTTPException(400, "vCenter credentials not configured")

    try:
        from pyVmomi import vim
        si = connect_vcenter_auto(host=vcenter_host, user=vcenter_user, pwd=vcenter_password, session_id=session_id)
    except Exception as exc:
        raise HTTPException(502, f"vCenter connect failed: {exc}")

    try:
        mgr = si.content.extensionManager
        thumbprint = body.ssl_thumbprint.strip()
        if not thumbprint:
            try:
                thumbprint = _fetch_ssl_thumbprint(body.plugin_url)
            except Exception as te:
                raise HTTPException(502, f"Could not fetch SSL thumbprint from {body.plugin_url}: {te}")
        ext = _vcenter_extension_obj(vim, body.plugin_url, body.plugin_key, body.plugin_name, body.plugin_version, thumbprint)
        existing = mgr.FindExtension(body.plugin_key)
        if existing:
            mgr.UpdateExtension(ext)
            action = "updated"
        else:
            mgr.RegisterExtension(ext)
            action = "registered"
        cfg2 = _read_config()
        cfg2["plugin_url"] = body.plugin_url
        _write_config(cfg2)
        return {"ok": True, "action": action, "key": body.plugin_key, "url": body.plugin_url, "thumbprint": thumbprint}
    except Exception as exc:
        raise HTTPException(500, f"Plugin registration failed: {exc}")
    finally:
        disconnect_vcenter(si)


class PluginKeyIn(BaseModel):
    plugin_key: str = _PLUGIN_KEY


@router.get("/api/vcenter/plugin/thumbprint")
def api_plugin_thumbprint(url: str):
    if not url.startswith("https://"):
        raise HTTPException(400, "URL must start with https://")
    try:
        tp = _fetch_ssl_thumbprint(url)
        return {"thumbprint": tp, "url": url}
    except Exception as exc:
        raise HTTPException(502, f"Could not reach {url}: {exc}")


@router.post("/api/vcenter/plugin/unregister")
def api_plugin_unregister(body: PluginKeyIn, x_vcenter_session: str = Header(default="")):
    cfg = _read_config()
    vcenter_host     = cfg.get("vcenter_host", "")
    vcenter_user     = cfg.get("vcenter_user", "")
    vcenter_password = cfg.get("vcenter_password", "")
    session_id = x_vcenter_session.strip()
    if not vcenter_host:
        raise HTTPException(400, "vCenter host not configured")
    if not session_id and (not vcenter_user or not vcenter_password):
        raise HTTPException(400, "vCenter credentials not configured")

    try:
        si = connect_vcenter_auto(host=vcenter_host, user=vcenter_user, pwd=vcenter_password, session_id=session_id)
    except Exception as exc:
        raise HTTPException(502, f"vCenter connect failed: {exc}")

    try:
        mgr = si.content.extensionManager
        existing = mgr.FindExtension(body.plugin_key)
        if not existing:
            return {"ok": True, "action": "not-found", "key": body.plugin_key}
        mgr.UnregisterExtension(body.plugin_key)
        return {"ok": True, "action": "unregistered", "key": body.plugin_key}
    except Exception as exc:
        raise HTTPException(500, f"Plugin unregistration failed: {exc}")
    finally:
        disconnect_vcenter(si)


@router.get("/api/vcenter/plugin/status")
def api_plugin_status(plugin_key: str = _PLUGIN_KEY, x_vcenter_session: str = Header(default="")):
    cfg = _read_config()
    vcenter_host     = cfg.get("vcenter_host", "")
    vcenter_user     = cfg.get("vcenter_user", "")
    vcenter_password = cfg.get("vcenter_password", "")
    session_id = x_vcenter_session.strip()
    if not vcenter_host:
        return {"registered": False, "reason": "vcenter-not-configured"}
    if not session_id and (not vcenter_user or not vcenter_password):
        return {"registered": False, "reason": "vcenter-not-configured"}

    try:
        si = connect_vcenter_auto(host=vcenter_host, user=vcenter_user, pwd=vcenter_password, session_id=session_id)
    except Exception as exc:
        return {"registered": False, "reason": str(exc)}

    try:
        mgr = si.content.extensionManager
        ext = mgr.FindExtension(plugin_key)
        if ext:
            url = (ext.client[0].url if ext.client else "")
            return {"registered": True, "key": plugin_key, "version": ext.version, "url": url}
        return {"registered": False, "key": plugin_key}
    finally:
        disconnect_vcenter(si)
