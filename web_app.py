#!/usr/bin/env python3
"""
Web UI for vCenter Network Policy Test Runner.

Start:  ./.venv/bin/uvicorn web_app:app --host 127.0.0.1 --port 5000
Open:   http://localhost:5000
"""
from __future__ import annotations

import asyncio
import csv
import json
import queue as _queue
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from nettest.vcenter_utils import connect_vcenter_direct, disconnect_vcenter, get_all_vms_by_moid, delete_vm
from nettest.paths import get_workspace, get_static_dir

# ── Paths ─────────────────────────────────────────────────────────────────────
WORKSPACE   = get_workspace()
ARTIFACTS   = WORKSPACE / "artifacts"
CONFIG_FILE = WORKSPACE / "nettest.config.json"
STATIC_DIR  = get_static_dir()
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="vCenter Network Test")

# Active run registry: run_id -> {queue, returncode, pid}
_runs: Dict[str, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _read_config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}

def _input_path() -> Path:
    val = str(_read_config().get("input", "") or "").strip()
    return WORKSPACE / (val if val else "input.csv")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(str(STATIC_DIR / "index.html"), status_code=204)


# ── Config ────────────────────────────────────────────────────────────────────
@app.get("/api/config")
def api_get_config():
    return _read_config()

class ConfigIn(BaseModel):
    data: dict

@app.put("/api/config")
def api_put_config(body: ConfigIn):
    CONFIG_FILE.write_text(json.dumps(body.data, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


# ── File uploads ────────────────────────────────────────────────────────────────
@app.post("/api/upload/input")
async def api_upload_input(file: UploadFile = File(...)):
    """Upload an input file (csv/xlsx/txt) and save to workspace root."""
    name = Path(file.filename or "").name
    if not name:
        raise HTTPException(400, "No filename")
    dest = WORKSPACE / name
    dest.write_bytes(await file.read())
    cfg = _read_config()
    cfg["input"] = name
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "path": name}

@app.post("/api/upload/ovf")
async def api_upload_ovf(files: List[UploadFile] = File(...)):
    """Upload all OVF bundle files (.ovf, .vmdk, .nvram, .mf, etc.) to workspace/ovf/."""
    ovf_dir = WORKSPACE / "ovf"
    ovf_dir.mkdir(exist_ok=True)
    ovf_name = None
    for file in files:
        name = Path(file.filename or "").name
        if not name:
            continue
        (ovf_dir / name).write_bytes(await file.read())
        if name.lower().endswith(".ovf"):
            ovf_name = name
    if not ovf_name:
        raise HTTPException(400, "No .ovf file found in uploaded files")
    rel = f"./ovf/{ovf_name}"
    cfg = _read_config()
    cfg["ovf_path"] = rel
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "path": rel}


# ── Input CSV ─────────────────────────────────────────────────────────────────
@app.get("/api/input")
def api_get_input():
    p = _input_path()
    if not p.exists():
        return {"rows": [], "path": str(p)}
    rows = []
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({k.strip().lower(): str(v).strip() for k, v in row.items()})
    return {"rows": rows, "path": str(p)}

class InputIn(BaseModel):
    rows: List[Dict[str, str]]

@app.put("/api/input")
def api_put_input(body: InputIn):
    p = _input_path()
    fields = ["vlan", "subnet", "gw", "vrf", "cluster", "datacenter"]
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in body.rows:
            w.writerow({k: row.get(k, "") for k in fields})
    return {"ok": True}


# ── vCenter Inventory ─────────────────────────────────────────────────────────
@app.get("/api/vcenter/inventory")
def api_vcenter_inventory():
    """Return datacenters, clusters, and distributed/standard portgroups from vCenter."""
    cfg = _read_config()
    host = str(cfg.get("vcenter_host", "")).strip()
    user = str(cfg.get("vcenter_user", "")).strip()
    pwd  = str(cfg.get("vcenter_password", "")).strip()
    if not host or not user:
        raise HTTPException(status_code=400, detail="vcenter_host and vcenter_user must be set in Config first")

    try:
        from pyVmomi import vim
    except ImportError:
        raise HTTPException(status_code=500, detail="pyvmomi not installed")

    try:
        si = connect_vcenter_direct(host=host, user=user, pwd=pwd)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"vCenter connection failed: {exc}")

    try:
        content = si.RetrieveContent()
        # portgroups structure: {dc_name: {cluster_name: [{name, vlan}]}}
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

        # Traverse root → Datacenters
        for dc_obj in _iter_folder(content.rootFolder):
            if not isinstance(dc_obj, vim.Datacenter):
                continue
            dc_name = str(dc_obj.name)
            result["datacenters"].append(dc_name)
            result["clusters"][dc_name] = []
            result["portgroups"][dc_name] = {}

            # Build DPG key → {name, vlan} map for this DC's network folder
            dc_dpg_map: dict = {}  # key -> {name, vlan}
            for net_obj in _iter_folder(dc_obj.networkFolder):
                if not isinstance(net_obj, vim.dvs.DistributedVirtualPortgroup):
                    continue
                pg_name = str(net_obj.name)
                if getattr(getattr(net_obj, "config", None), "uplink", False):
                    continue
                dpg_key = str(getattr(net_obj, "key", ""))
                if dpg_key:
                    dc_dpg_map[dpg_key] = {"name": pg_name, "vlan": _pg_vlan(net_obj)}

            # For each cluster, collect DPG keys accessible to its connected hosts
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


# ── Run ───────────────────────────────────────────────────────────────────────
class RunIn(BaseModel):
    execute_vcenter: bool = False
    probe_mode: str = "dry-run"
    max_retries: int = 0
    cleanup_on_failure: bool = False
    phased_testing: bool = False
    vms_per_subnet: int = 1
    max_vms_per_phase: int = 20
    phases: str = ""          # comma-separated phase IDs, empty = all
    vrf_links: List[str] = [] # "FROM:TO" or "FROM:TO:FAIL"

@app.post("/api/run")
def api_start_run(req: RunIn):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    q: _queue.Queue = _queue.Queue()
    _runs[run_id] = {"queue": q, "returncode": None}

    # When running as a frozen binary, invoke the binary itself with --_runner
    # instead of calling python nettest_runner.py.
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--_runner", "--config", str(CONFIG_FILE),
               "--run-id", run_id]
    else:
        cmd = [sys.executable, "-u", "nettest_runner.py", "--config", str(CONFIG_FILE),
               "--run-id", run_id]
    if req.execute_vcenter:
        cmd.append("--execute-vcenter")
    cmd += ["--probe-mode", req.probe_mode, "--max-retries", str(req.max_retries)]
    if req.cleanup_on_failure:
        cmd.append("--cleanup-on-failure")
    if req.phased_testing:
        cmd.append("--phased-testing")
    cmd += ["--vms-per-subnet", str(req.vms_per_subnet)]
    cmd += ["--max-vms-per-phase", str(req.max_vms_per_phase)]
    if req.phases:
        cmd += ["--phases", req.phases]
    for vl in req.vrf_links:
        cmd += ["--vrf-links", vl]

    def _worker():
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(WORKSPACE),
        )
        _runs[run_id]["pid"] = proc.pid
        if proc.stdout is None:
            q.put(None)
            return
        for line in proc.stdout:
            q.put(line)
        proc.wait()
        _runs[run_id]["returncode"] = proc.returncode
        q.put(None)  # sentinel

    threading.Thread(target=_worker, daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/run/{run_id}/stream")
async def api_stream_run(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404, "Run not found")
    run = _runs[run_id]
    q = run["queue"]
    loop = asyncio.get_event_loop()

    def _get_next():
        """Blocking get with 1s timeout; retries until sentinel or run done."""
        while True:
            try:
                return q.get(timeout=1)
            except _queue.Empty:
                if run.get("returncode") is not None:
                    return None

    async def _generate():
        while True:
            line = await loop.run_in_executor(None, _get_next)
            if line is None:
                rc = run.get("returncode", 0)
                yield f"data: __DONE__:{rc}\n\n"
                break
            yield f"data: {json.dumps(line.rstrip())}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/run/{run_id}/result")
def api_get_result(run_id: str):
    p = ARTIFACTS / run_id / "result.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    e = ARTIFACTS / run_id / "error.json"
    if e.exists():
        raise HTTPException(500, detail=json.loads(e.read_text(encoding="utf-8")))
    raise HTTPException(404, "Result not found")


@app.post("/api/run/{run_id}/cleanup")
def api_cleanup_run(run_id: str):
    """Delete all VMs recorded in created-objects.json for a run."""
    registry_path = ARTIFACTS / run_id / "created-objects.json"
    if not registry_path.exists():
        raise HTTPException(404, "No created-objects.json for this run")

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    moids = [m for m in registry.get("vms", []) if m and m != "dry-run-moid"]
    if not moids:
        return {"cleaned": 0, "failed": 0, "skipped": 0, "detail": "no real VMs to delete"}

    cfg = _read_config()
    vcenter_host = cfg.get("vcenter_host", "")
    vcenter_user = cfg.get("vcenter_user", "")
    vcenter_password = cfg.get("vcenter_password", "")
    vm_prefix = cfg.get("vm_prefix", "nettest")

    if not vcenter_host or not vcenter_user or not vcenter_password:
        raise HTTPException(400, "vCenter credentials not configured")

    try:
        from pyVmomi import vim as _vim
    except ImportError:
        raise HTTPException(500, "pyvmomi not installed")

    try:
        si = connect_vcenter_direct(host=vcenter_host, user=vcenter_user, pwd=vcenter_password)
    except Exception as exc:
        raise HTTPException(502, f"vCenter connect failed: {exc}")

    try:
        content = si.RetrieveContent()
        vm_by_moid = get_all_vms_by_moid(content, _vim)

        cleaned, failed, skipped = 0, 0, 0
        failures = []
        for moid in moids:
            vm_obj = vm_by_moid.get(moid)
            if vm_obj is None:
                skipped += 1
                continue
            ok, reason = delete_vm(vm_obj, vm_prefix)
            if ok:
                cleaned += 1
            elif reason == "prefix-mismatch":
                skipped += 1
                failures.append({"moid": moid, "name": str(getattr(vm_obj, "name", "")), "reason": reason})
            else:
                failed += 1
                failures.append({"moid": moid, "name": str(getattr(vm_obj, "name", "")), "reason": reason})

        # Persist: keep only moids that were NOT successfully deleted or not found
        failed_moids = {f["moid"] for f in failures}
        registry["vms"] = [m for m in registry["vms"] if m in failed_moids]
        registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

        return {"cleaned": cleaned, "failed": failed, "skipped": skipped, "failures": failures}
    finally:
        disconnect_vcenter(si)


# ── History ───────────────────────────────────────────────────────────────────
@app.get("/api/history")
def api_get_history():
    if not ARTIFACTS.exists():
        return []
    runs = []
    for d in sorted(ARTIFACTS.iterdir(), reverse=True):
        if not d.is_dir() or len(runs) >= 50:
            continue
        rf = d / "result.json"
        if rf.exists():
            try:
                data = json.loads(rf.read_text(encoding="utf-8"))
                results_raw = data.get("Results", [])
                details = results_raw if isinstance(results_raw, list) else results_raw.get("details", [])
                total  = len(details)
                passed = sum(1 for r in details if r.get("status") == "pass")
                failed = total - passed
                runs.append({
                    "run_id":     d.name,
                    "status":     data.get("FinalStatus", "unknown"),
                    "total":      total,
                    "passed":     passed,
                    "failed":     failed,
                    "probe_mode": data.get("Plan", {}).get("probe_mode", ""),
                })
            except Exception:
                pass
        elif (d / "error.json").exists():
            runs.append({
                "run_id": d.name, "status": "error",
                "total": 0, "passed": 0, "failed": 0, "probe_mode": "",
            })
    return runs


# ── Serve SPA ─────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return FileResponse(str(STATIC_DIR / "index.html"))

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
