"""API routes: /api/vcenter/*, /plugin.json, /plugin.zip"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from nettest.vcenter_utils import connect_vcenter_auto, connect_vcenter_with_clone_ticket, connect_vcenter_with_session, disconnect_vcenter
from nettest.api.deps import _read_config, _write_config, get_session_password

router = APIRouter()

_PLUGIN_KEY     = "com.nettest.vcnet"
_PLUGIN_NAME    = "vCenter Network Test"
_PLUGIN_VERSION = "1.0.0"


# ── vCenter REST API inventory (used when a vmware-api-session-id token is present) ──

def _vc_rest_session(host: str, api_session: str):
    """Return a requests.Session pre-configured for vCenter REST API calls."""
    try:
        import requests, urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        raise RuntimeError("requests library is required: pip install requests")
    s = requests.Session()
    s.verify = False
    s.headers.update({
        "vmware-api-session-id": api_session,
        "Content-Type": "application/json",
    })
    return s


def _exchange_soap_for_rest(host: str, soap_token: str) -> str | None:
    """Exchange a vCenter SOAP session cookie for a REST API session ID.

    vCenter 7.x substitutes a SOAP session token via {vmwareApiSessionId} in
    the plugin manifest URI.  That token cannot be used directly as a
    vmware-api-session-id REST header, but it CAN be used as a Cookie to POST
    /rest/com/vmware/cis/session and receive a proper REST session ID.

    Returns the REST session ID string, or None on any failure.
    """
    import requests, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        r = requests.post(
            f"https://{host}/rest/com/vmware/cis/session",
            headers={
                "Cookie": f'vmware_soap_session="{soap_token}"',
                "Content-Type": "application/json",
            },
            verify=False, timeout=10,
        )
        _inv_log.info("SOAP→REST promotion POST /rest/com/vmware/cis/session → %d", r.status_code)
        if r.ok:
            val = r.json()
            if isinstance(val, dict) and "value" in val:
                val = val["value"]
            if isinstance(val, str) and val:
                return val
    except Exception as exc:
        _inv_log.warning("SOAP→REST promotion failed: %s", exc)
    return None


def _fetch_pg_vlan_rest(sess, base: str, pg_id: str) -> str:
    """Return the VLAN tag for a distributed portgroup via REST (7.x and 8.x).

    Tries the vSphere 8.x path first, falls back to the 7.x path, and returns
    an empty string if neither endpoint is available or the portgroup has no
    simple VLAN tag (e.g. trunk).
    """
    for path in [
        f"{base}/api/vcenter/dvs/portgroup/{pg_id}",
        f"{base}/rest/vcenter/dvswitch/portgroup/{pg_id}",
    ]:
        try:
            r = sess.get(path, timeout=5)
            if not r.ok:
                continue
            data = r.json()
            # /rest/ wraps response in {"value": {...}}
            if "value" in data:
                data = data["value"]
            spec = data.get("spec") or {}
            vlan_cfg = spec.get("vlan") or {}
            # Simple VLAN tag
            tag = vlan_cfg.get("tag") or vlan_cfg.get("vlan_id")
            if tag and int(tag) != 0:
                return str(int(tag))
            # Trunk / range — signal as "trunk" so the UI knows
            if vlan_cfg.get("ranges") or vlan_cfg.get("vlan_ranges"):
                return "trunk"
        except Exception:
            pass
    return ""


def _inventory_via_rest(host: str, api_session: str) -> dict:
    """Build the standard inventory dict using the vSphere REST API.

    Supports both vSphere 8.x (/api/ prefix, bare JSON arrays) and
    vSphere 7.x (/rest/ prefix, responses wrapped in {"value": ...}).
    Tries /api/ first; on 401 falls back to /rest/ so the same token
    works regardless of vCenter version.
    """
    base = f"https://{host}"
    s = _vc_rest_session(host, api_session)

    # ── Detect API prefix (8.x /api/ vs 7.x /rest/) ──────────────────────────
    api_prefix: str | None = None
    last_status: int = 0
    for prefix in [f"{base}/api", f"{base}/rest"]:
        probe = s.get(f"{prefix}/vcenter/datacenter", timeout=15)
        last_status = probe.status_code
        if probe.status_code == 401:
            _inv_log.info("REST probe %s/vcenter/datacenter → 401, trying next prefix", prefix)
            continue
        if probe.ok:
            api_prefix = prefix
            break
        # Any other non-ok status (404, 500, …) — skip this prefix
        _inv_log.info("REST probe %s/vcenter/datacenter → %d, trying next prefix", prefix, probe.status_code)

    if api_prefix is None:
        raise HTTPException(
            status_code=401,
            detail=f"vCenter REST session rejected by both /api/ and /rest/ endpoints "
                   f"(last HTTP status: {last_status}). "
                   "Please reload the plugin or re-enter credentials in Config."
        )

    def _unwrap(r) -> list:
        """Unwrap vSphere 7.x {"value": [...]} envelope if present."""
        data = r.json()
        if isinstance(data, dict) and "value" in data:
            return data["value"]
        return data if isinstance(data, list) else []

    dcs: list = _unwrap(probe)   # probe response already fetched above
    result: dict = {"datacenters": [], "clusters": {}, "portgroups": {}}
    dc_id_to_name: dict[str, str] = {}
    for dc in dcs:
        dc_name = dc["name"]
        dc_id   = dc["datacenter"]
        dc_id_to_name[dc_id] = dc_name
        result["datacenters"].append(dc_name)
        result["clusters"][dc_name]   = []
        result["portgroups"][dc_name] = {}

    for dc_id, dc_name in dc_id_to_name.items():
        cl_resp = s.get(f"{api_prefix}/vcenter/cluster",
                        params={"filter.datacenters": dc_id}, timeout=15)
        cl_resp.raise_for_status()
        for cl in _unwrap(cl_resp):
            cl_id   = cl["cluster"]
            cl_name = cl["name"]
            result["clusters"][dc_name].append(cl_name)

            net_resp = s.get(f"{api_prefix}/vcenter/network",
                             params={"filter.types": "DISTRIBUTED_PORTGROUP",
                                     "filter.clusters": cl_id}, timeout=15)
            pg_map: dict[str, str] = {}
            if net_resp.ok:
                for pg in _unwrap(net_resp):
                    pg_map[pg["network"]] = pg["name"]

            pg_list = []
            for pg_id, pg_name in pg_map.items():
                vlan = _fetch_pg_vlan_rest(s, base, pg_id)
                pg_list.append({"name": pg_name, "vlan": vlan})

            pg_list.sort(key=lambda x: (
                0 if x["vlan"].isdigit() else 1,
                int(x["vlan"]) if x["vlan"].isdigit() else 0,
                x["name"],
            ))
            result["portgroups"][dc_name][cl_name] = pg_list

    return result


# ── vCenter Inventory ─────────────────────────────────────────────────────────
_inv_log = logging.getLogger(__name__)


def _inventory_via_si(si) -> dict:
    """Build inventory dict from a connected pyVmomi ServiceInstance."""
    from pyVmomi import vim

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
            for host_obj in hosts:
                runtime = getattr(host_obj, "runtime", None)
                if str(getattr(runtime, "connectionState", "")).lower() != "connected":
                    continue
                if bool(getattr(runtime, "inMaintenanceMode", False)):
                    continue
                for net in (getattr(host_obj, "network", None) or []):
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


@router.get("/api/vcenter/inventory")
def api_vcenter_inventory(
    x_vcenter_clone_ticket: str | None = Header(default=None),
    x_vcenter_session: str | None = Header(default=None),
    x_session_token: str = Header(default=""),
):
    """Return datacenters, clusters, and distributed portgroups from vCenter.

    Auth priority:
      0. X-Vcenter-Clone-Ticket → Broadcom "ticket clone" flow (recommended).
         Frontend calls vsphereClient.auth.acquireCloneTicket(), passes ticket here;
         backend calls SessionManager.CloneSession(ticket) for an authenticated SOAP session.
      1. X-Vcenter-Session → REST vmware-api-session-id (tries /api/ then /rest/).
         On REST 401, retries as a SOAP session cookie (legacy {vmwareApiSessionId} path).
      2. X-Session-Token → pyVmomi SOAP with username/password from Config (standalone).
    """
    _inv_log.info(
        "inventory: clone_ticket=%s X-Vcenter-Session=%s X-Session-Token=%s",
        "present" if x_vcenter_clone_ticket else "absent",
        f"present({len(x_vcenter_session)}chars)" if x_vcenter_session else "absent",
        "present" if x_session_token else "absent",
    )
    cfg  = _read_config()
    host = str(cfg.get("vcenter_host", "")).strip()
    if not host:
        raise HTTPException(status_code=400, detail="vcenter_host must be set in Config first")

    # ── Path 0: clone ticket (Broadcom-recommended plugin session delegation) ──
    if x_vcenter_clone_ticket:
        try:
            si = connect_vcenter_with_clone_ticket(host=host, clone_ticket=x_vcenter_clone_ticket)
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"CloneSession failed: {exc}")
        try:
            return _inventory_via_si(si)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"vCenter inventory error (clone session): {exc}")
        finally:
            disconnect_vcenter(si)

    # ── Path 1 + 1b: plugin session token (REST / legacy SOAP cookie) ──────────
    if x_vcenter_session:
        # 1a. Try as REST vmware-api-session-id (8.x SDK / 7.x REST-format tokens)
        rest_exc: Exception | None = None
        try:
            return _inventory_via_rest(host=host, api_session=x_vcenter_session)
        except HTTPException as e:
            if e.status_code != 401:
                raise
            rest_exc = e
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"vCenter REST API error: {e}")

        # 1b. REST returned 401 → token is likely a SOAP session (vCenter 7.x
        #     substitutes the SOAP cookie via {vmwareApiSessionId} in the manifest URI).
        #     Try to promote it to a REST session via POST /rest/com/vmware/cis/session.
        _inv_log.info(
            "REST session rejected (401); attempting SOAP→REST session promotion for %s", host
        )
        promoted = _exchange_soap_for_rest(host, x_vcenter_session)
        if promoted:
            _inv_log.info("SOAP→REST promotion succeeded; retrying REST inventory")
            try:
                return _inventory_via_rest(host=host, api_session=promoted)
            except Exception as promo_exc:
                _inv_log.warning("Promoted REST session inventory failed: %s", promo_exc)

        # 1c. Fall back to injecting the token directly as a SOAP session cookie.
        _inv_log.info("Falling back to direct SOAP session cookie injection")
        try:
            si = connect_vcenter_with_session(host=host, soap_session_id=x_vcenter_session)
        except Exception as e:
            raise HTTPException(
                status_code=401,
                detail=f"vCenter session invalid for REST, SOAP promotion, and direct SOAP paths. "
                       f"REST: {rest_exc}. SOAP: {e}"
            )
        try:
            return _inventory_via_si(si)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"vCenter SOAP inventory error: {e}")
        finally:
            disconnect_vcenter(si)

    # ── Path 2: pyVmomi SOAP (standalone mode with saved credentials) ──────────
    try:
        from pyVmomi import vim  # noqa: F401
    except ImportError:
        raise HTTPException(status_code=500, detail="pyvmomi not installed")

    user = str(cfg.get("vcenter_user", "")).strip()
    pwd  = get_session_password(x_session_token.strip())
    if not user:
        raise HTTPException(status_code=400, detail="vcenter_user must be set in Config first")
    if not pwd:
        raise HTTPException(
            status_code=401,
            detail="No vCenter credentials available. "
                   "Please save your password in the Config page."
        )

    try:
        si = connect_vcenter_auto(host=host, user=user, pwd=pwd)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"vCenter connection failed: {exc}")
    try:
        return _inventory_via_si(si)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"vCenter inventory error: {exc}")
    finally:
        disconnect_vcenter(si)


# ── Plugin manifest helpers ───────────────────────────────────────────────────
def _plugin_manifest_dict() -> dict:
    # Embed the configured vCenter host in the plugin URI so the Angular app can
    # load the vSphere Client SDK even when document.referrer is not available
    # (common in some browser/vCenter combinations).  Without the SDK the app
    # cannot call acquireCloneTicket() and must rely on URL substitution alone.
    cfg     = _read_config()
    vc_host = str(cfg.get("vcenter_host", "")).strip()
    base_uri = "index.html?plugin=1&vmwareApiSessionId={vmwareApiSessionId}"
    if vc_host:
        base_uri += f"&vcHost={vc_host}"
    return {
        "manifestVersion": "1.2.0",
        "requirements": {"plugin.api.version": "1.2.0"},
        "configuration": {
            "nameKey": "plugin.info.name",
            "icon": {"name": "network-globe"},
        },
        "global": {"view": {"uri": base_uri}},
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
def api_plugin_register(body: PluginRegisterIn, x_vcenter_session: str = Header(default=""), x_session_token: str = Header(default="")):
    cfg = _read_config()
    vcenter_host     = cfg.get("vcenter_host", "")
    vcenter_user     = cfg.get("vcenter_user", "")
    vcenter_password = get_session_password(x_session_token.strip())
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
def api_plugin_unregister(body: PluginKeyIn, x_vcenter_session: str = Header(default=""), x_session_token: str = Header(default="")):
    cfg = _read_config()
    vcenter_host     = cfg.get("vcenter_host", "")
    vcenter_user     = cfg.get("vcenter_user", "")
    vcenter_password = get_session_password(x_session_token.strip())
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


@router.post("/api/vcenter/esxi/firewall/remoteSerialPort")
def api_enable_remote_serial_port(x_vcenter_session: str = Header(default="")):
    """Enable the remoteSerialPort firewall ruleset on all connected ESXi hosts."""
    from nettest.vcenter_utils import enable_esxi_firewall_ruleset
    cfg = _read_config()
    vcenter_host     = cfg.get("vcenter_host", "")
    vcenter_user     = cfg.get("vcenter_user", "")
    vcenter_password = cfg.get("vcenter_password", "")
    session_id = x_vcenter_session.strip()
    if not vcenter_host:
        raise HTTPException(status_code=400, detail="vcenter-not-configured")
    try:
        si = connect_vcenter_auto(host=vcenter_host, user=vcenter_user, pwd=vcenter_password, session_id=session_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    try:
        results = enable_esxi_firewall_ruleset(si, "remoteSerialPort")
        all_ok = all(v == "ok" for v in results.values())
        return {"ok": all_ok, "hosts": results}
    finally:
        disconnect_vcenter(si)
