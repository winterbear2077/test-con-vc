"""API routes: /api/run and /api/run/{run_id}/*"""
from __future__ import annotations

import asyncio
import json
import queue as _queue
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import nettest.db as _db
from nettest.vcenter_utils import connect_vcenter_direct, disconnect_vcenter, get_all_vms_by_moid, delete_vm
from nettest.api.deps import WORKSPACE, ARTIFACTS, CONFIG_FILE, _runs, _read_config

router = APIRouter()


# ── Run model ─────────────────────────────────────────────────────────────────
class RunIn(BaseModel):
    execute_vcenter: bool = False
    probe_mode: str = "dry-run"
    max_retries: int = 0
    cleanup_on_failure: bool = False
    phased_testing: bool = False
    vms_per_subnet: int = 1
    max_vms_per_phase: int = 20
    phases: str = ""           # comma-separated phase IDs, empty = all
    vrf_links: List[str] = []  # "FROM:TO" or "FROM:TO:FAIL"


@router.post("/api/run")
def api_start_run(req: RunIn):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    q: _queue.Queue = _queue.Queue()
    _runs[run_id] = {"queue": q, "returncode": None}
    _db.insert_run(run_id, run_id)

    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--_runner", "--config", str(CONFIG_FILE), "--run-id", run_id]
    else:
        cmd = [sys.executable, "-u", "nettest_runner.py", "--config", str(CONFIG_FILE), "--run-id", run_id]

    # Pass vCenter credentials via CLI so they never need to live in the JSON file
    cfg = _read_config()
    vc_host = str(cfg.get("vcenter_host", "")).strip()
    vc_user = str(cfg.get("vcenter_user", "")).strip()
    vc_pass = str(cfg.get("vcenter_password", "")).strip()
    if vc_host:
        cmd += ["--vcenter-host", vc_host]
    if vc_user:
        cmd += ["--vcenter-user", vc_user]
    if vc_pass:
        cmd += ["--vcenter-password", vc_pass]

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

    def _worker() -> None:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(WORKSPACE),
        )
        _runs[run_id]["pid"] = proc.pid
        if proc.stdout is None:
            q.put(None)
            return
        for line in proc.stdout:
            if line.startswith("__RESULT__:"):
                try:
                    _db.finish_run(run_id, json.loads(line[len("__RESULT__:"):].strip()))
                except Exception:
                    pass
                continue
            if line.startswith("__ERROR__:"):
                try:
                    _db.finish_run_error(run_id, line[len("__ERROR__:"):].strip())
                except Exception:
                    pass
                continue
            q.put(line)
        proc.wait()
        _runs[run_id]["returncode"] = proc.returncode
        q.put(None)  # sentinel

    threading.Thread(target=_worker, daemon=True).start()
    return {"run_id": run_id}


@router.get("/api/run/{run_id}/stream")
async def api_stream_run(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404, "Run not found")
    run = _runs[run_id]
    q = run["queue"]
    loop = asyncio.get_event_loop()

    def _get_next():
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


@router.get("/api/run/{run_id}/result")
def api_get_result(run_id: str):
    data = _db.get_result(run_id)
    if data:
        return data
    raise HTTPException(404, "Result not found")


@router.post("/api/run/{run_id}/cleanup")
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
    vcenter_host     = cfg.get("vcenter_host", "")
    vcenter_user     = cfg.get("vcenter_user", "")
    vcenter_password = cfg.get("vcenter_password", "")
    vm_prefix        = cfg.get("vm_prefix", "nettest")

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

        failed_moids = {f["moid"] for f in failures}
        registry["vms"] = [m for m in registry["vms"] if m in failed_moids]
        registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

        return {"cleaned": cleaned, "failed": failed, "skipped": skipped, "failures": failures}
    finally:
        disconnect_vcenter(si)
