"""API routes: /api/config, /api/session, and /api/upload/*"""
from __future__ import annotations

from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

import nettest.db as _db
from nettest.api.deps import (
    WORKSPACE,
    _read_config,
    _write_config,
    _parse_input_file,
    _validate_rows,
    create_session,
    revoke_session,
)

router = APIRouter()


# ── Config ────────────────────────────────────────────────────────────────────
@router.get("/api/config")
def api_get_config():
    return _read_config()


@router.put("/api/config")
def api_put_config(body: dict):
    _write_config(body)
    return {"ok": True}


# ── Session (password exchanged once for a short-lived opaque token) ──────────
class SessionIn(BaseModel):
    vcenter_password: str = ""


@router.post("/api/session")
def api_create_session(body: SessionIn):
    """Exchange the vCenter password for a server-side session token (TTL 8 h).
    The password is never stored on disk and is never sent again after this call."""
    token = create_session(body.vcenter_password)
    return {"session_token": token}


@router.delete("/api/session/{token}")
def api_revoke_session(token: str):
    revoke_session(token)
    return {"ok": True}


# ── File uploads ──────────────────────────────────────────────────────────────
@router.post("/api/upload/input")
async def api_upload_input(file: UploadFile = File(...)):
    """Upload an input file (csv/xlsx/txt), validate, and save valid rows to DB."""
    name = Path(file.filename or "").name
    if not name:
        raise HTTPException(400, "No filename")
    dest = WORKSPACE / name
    dest.write_bytes(await file.read())
    cfg = _read_config()
    cfg["input"] = name
    _write_config(cfg)
    raw = _parse_input_file(dest)
    valid, rejected = _validate_rows(raw)
    _db.save_networks(valid)
    return {"ok": True, "path": name, "count": len(valid), "rejected": rejected}


@router.post("/api/upload/input/preview")
async def api_preview_input(file: UploadFile = File(...)):
    """Parse and validate an input file; return rows without persisting anything."""
    name = Path(file.filename or "").name
    if not name:
        raise HTTPException(400, "No filename")
    import tempfile, os
    suffix = Path(name).suffix.lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        raw = _parse_input_file(tmp_path)
    finally:
        os.unlink(tmp_path)
    valid, rejected = _validate_rows(raw)
    return {"rows": valid, "count": len(valid), "rejected": rejected}


@router.post("/api/upload/ovf")
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
    _write_config(cfg)
    return {"ok": True, "path": rel}


@router.post("/api/upload/iso")
async def api_upload_iso(file: UploadFile = File(...)):
    """Upload a memboot ISO file to workspace/ovf/."""
    name = Path(file.filename or "").name
    if not name:
        raise HTTPException(400, "No filename")
    if not name.lower().endswith(".iso"):
        raise HTTPException(400, "File must be a .iso")
    ovf_dir = WORKSPACE / "ovf"
    ovf_dir.mkdir(exist_ok=True)
    (ovf_dir / name).write_bytes(await file.read())
    rel = f"./ovf/{name}"
    cfg = _read_config()
    cfg["memboot_iso_path"] = rel
    _write_config(cfg)
    return {"ok": True, "path": rel}
