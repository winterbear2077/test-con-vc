"""API routes: /api/input and /api/vrf-rules"""
from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter
from pydantic import BaseModel

import nettest.db as _db
from nettest.api.deps import (
    _input_path,
    _parse_input_file,
    _sync_networks_to_csv,
)

router = APIRouter()


# ── Input Networks ────────────────────────────────────────────────────────────
@router.get("/api/input")
def api_get_input():
    rows = _db.get_networks()
    # Fallback: read from file if DB is empty
    if not rows:
        p = _input_path()
        if p.exists():
            rows = _parse_input_file(p)
            if rows:
                _db.save_networks(rows)
    return {"rows": rows, "path": str(_input_path())}


class InputIn(BaseModel):
    rows: List[Dict[str, str]]


@router.put("/api/input")
def api_put_input(body: InputIn):
    _db.save_networks(body.rows)
    _sync_networks_to_csv(body.rows)
    return {"ok": True}


# ── VRF Rules ─────────────────────────────────────────────────────────────────
class VrfRulesIn(BaseModel):
    rules: List[Dict]


@router.get("/api/vrf-rules")
def api_get_vrf_rules():
    return {"rules": _db.get_vrf_rules()}


@router.put("/api/vrf-rules")
def api_put_vrf_rules(body: VrfRulesIn):
    _db.save_vrf_rules(body.rules)
    return {"ok": True}
