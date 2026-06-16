"""API routes: /api/input, /api/vrf-rules, /api/custom-step-rules"""
from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter
from pydantic import BaseModel

import nettest.db as _db
from nettest.api.deps import (
    _parse_input_file,
)

router = APIRouter()


def _custom_step_testcases() -> list[dict]:
    subnet_to_gw = {
        str(row.get("subnet", "")).strip(): str(row.get("gw", "")).strip()
        for row in _db.get_networks()
    }
    out: list[dict] = []
    for idx, row in enumerate(_db.get_custom_step_rules()):
        rule_id = row.get("id")
        src = str(row.get("src_subnet", "")).strip()
        dest = str(row.get("dest", "")).strip()
        protocol = str(row.get("protocol", "tcp")).strip().lower() or "tcp"
        try:
            port = int(row.get("port", 80) or 80)
        except Exception:
            port = 80
        if not src or not dest or protocol not in ("tcp", "udp", "icmp"):
            continue

        key_raw = f"{src}|{protocol}|{dest}|{port}"
        key = "".join(ch if ch.isalnum() else "-" for ch in key_raw).strip("-") or f"rule-{idx+1}"

        target = subnet_to_gw.get(dest, dest)
        base = f"custom-rule-{key}"
        out.append(
            {
                "id": f"{base}-step1",
                "rule_id": rule_id,
                "step": 1,
                "phase": f"custom-{rule_id}-step1" if rule_id is not None else "custom-step1",
                "src_subnet": src,
                "dest": dest,
                "target": target,
                "protocol": "icmp",
                "port": 0,
                "label": f"{src} -> {dest} [step1 ping]",
            }
        )
        out.append(
            {
                "id": f"{base}-step2",
                "rule_id": rule_id,
                "step": 2,
                "phase": f"custom-{rule_id}-step2" if rule_id is not None else "custom-step2",
                "src_subnet": src,
                "dest": dest,
                "target": target,
                "protocol": protocol,
                "port": port if protocol == "tcp" else 0,
                "label": f"{src} -> {dest} [step2 {protocol}{(':'+str(port)) if protocol == 'tcp' else ''}]",
            }
        )
    return out


# ── Input Networks ────────────────────────────────────────────────────────────
@router.get("/api/input")
def api_get_input():
    rows = _db.get_networks()
    return {"rows": rows}


class InputIn(BaseModel):
    rows: List[Dict[str, str]]


@router.put("/api/input")
def api_put_input(body: InputIn):
    _db.save_networks(body.rows)
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


# ── Custom Step Rules ────────────────────────────────────────────────────────
class CustomStepRulesIn(BaseModel):
    rules: List[Dict]


@router.get("/api/custom-step-rules")
def api_get_custom_step_rules():
    return {"rules": _db.get_custom_step_rules()}


@router.put("/api/custom-step-rules")
def api_put_custom_step_rules(body: CustomStepRulesIn):
    _db.save_custom_step_rules(body.rules)
    return {"ok": True}


@router.get("/api/custom-step-testcases")
def api_get_custom_step_testcases():
    return {"testcases": _custom_step_testcases()}


class TestSuiteRowIn(BaseModel):
    name: str
    testcase_keys: List[str] = []


class TestSuitesIn(BaseModel):
    suites: List[TestSuiteRowIn]


@router.get("/api/testsuites")
def api_get_testsuites():
    return {"suites": _db.get_testsuites()}


@router.put("/api/testsuites")
def api_put_testsuites(body: TestSuitesIn):
    rows = [
        {
            "name": str(s.name).strip(),
            "testcase_keys": [str(x).strip() for x in (s.testcase_keys or []) if str(x).strip()],
        }
        for s in body.suites
    ]
    _db.save_testsuites(rows)
    return {"ok": True}
