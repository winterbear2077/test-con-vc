"""API routes: /api/history"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
import nettest.db as _db

router = APIRouter()


@router.get("/api/history")
def api_get_history():
    return _db.get_history(limit=100)


@router.delete("/api/history/{run_id}")
def api_delete_history(run_id: str):
    _db.delete_run(run_id)
    return {"ok": True}
