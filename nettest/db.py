"""SQLite persistence for run history and results."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

_lock = threading.Lock()
_db_path: Path | None = None

DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'running',
    probe_mode TEXT NOT NULL DEFAULT '',
    total      INTEGER NOT NULL DEFAULT 0,
    passed     INTEGER NOT NULL DEFAULT 0,
    failed     INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS created_objects (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    vms_json  TEXT NOT NULL DEFAULT '[]',
    nics_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    cleaned   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS networks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    datacenter TEXT NOT NULL DEFAULT '',
    cluster    TEXT NOT NULL DEFAULT '',
    pg         TEXT NOT NULL DEFAULT '',
    vlan       TEXT NOT NULL DEFAULT '',
    subnet     TEXT NOT NULL DEFAULT '',
    gw         TEXT NOT NULL DEFAULT '',
    vrf        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS vrf_rules (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    from_vrf TEXT NOT NULL DEFAULT '',
    to_vrf   TEXT NOT NULL DEFAULT '',
    action   TEXT NOT NULL DEFAULT 'PASS',
    comment  TEXT NOT NULL DEFAULT ''
);
"""


def init(db_path: Path) -> None:
    global _db_path
    _db_path = db_path
    with _connect() as con:
        con.executescript(DDL)
        # Migration: drop legacy column on older DBs (best-effort, SQLite ≥3.35)
        try:
            con.execute("ALTER TABLE runs DROP COLUMN created_objects_json")
        except sqlite3.OperationalError:
            pass  # column absent or SQLite too old — harmless
        # Migration: add pg column to networks if missing
        try:
            con.execute("ALTER TABLE networks ADD COLUMN pg TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # already exists
        # Migration: add cleaned column to created_objects if missing
        try:
            con.execute("ALTER TABLE created_objects ADD COLUMN cleaned INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # already exists


def _connect() -> sqlite3.Connection:
    assert _db_path is not None, "db.init() not called"
    con = sqlite3.connect(str(_db_path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def insert_run(run_id: str, created_at: str) -> None:
    with _lock, _connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO runs (run_id, created_at) VALUES (?, ?)",
            (run_id, created_at),
        )


def finish_run(run_id: str, result: dict) -> None:
    """Persist completed run data."""
    results_raw = result.get("Results", [])
    details = results_raw if isinstance(results_raw, list) else results_raw.get("details", [])
    total = len(details)
    passed = sum(1 for r in details if r.get("status") == "pass")
    failed = total - passed
    with _lock, _connect() as con:
        con.execute(
            """INSERT INTO runs (run_id, status, probe_mode, total, passed, failed, result_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                   status=excluded.status, probe_mode=excluded.probe_mode,
                   total=excluded.total, passed=excluded.passed, failed=excluded.failed,
                   result_json=excluded.result_json""",
            (
                run_id,
                result.get("FinalStatus", "unknown"),
                result.get("Plan", {}).get("probe_mode", ""),
                total, passed, failed,
                json.dumps(result, ensure_ascii=False),
                run_id,  # run_id is already a timestamp string; use as created_at fallback
            ),
        )


def finish_run_error(run_id: str, detail: "dict | str") -> None:
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except Exception:
            detail = {"error": detail}
    error_result = json.dumps({"FinalStatus": "error", **detail}, ensure_ascii=False)
    with _lock, _connect() as con:
        con.execute(
            """INSERT INTO runs (run_id, status, created_at, result_json)
               VALUES (?, 'error', ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                   status='error', result_json=excluded.result_json""",
            (run_id, run_id, error_result),
        )


def get_history(limit: int = 100) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """SELECT r.run_id, r.status, r.probe_mode, r.total, r.passed, r.failed,
                      COALESCE(co.cleaned, 0) AS vms_cleaned
               FROM runs r
               LEFT JOIN created_objects co ON co.run_id = r.run_id
               ORDER BY r.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_run(run_id: str) -> None:
    with _lock, _connect() as con:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("DELETE FROM runs WHERE run_id=?", (run_id,))


def upsert_created_objects(run_id: str, registry: dict) -> None:
    """Persist the created-objects registry in the dedicated table."""
    with _lock, _connect() as con:
        con.execute(
            """INSERT INTO created_objects (run_id, vms_json, nics_json, tags_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                   vms_json=excluded.vms_json,
                   nics_json=excluded.nics_json,
                   tags_json=excluded.tags_json""",
            (
                run_id,
                json.dumps(registry.get("vms", []), ensure_ascii=False),
                json.dumps(registry.get("nics", []), ensure_ascii=False),
                json.dumps(registry.get("tags", []), ensure_ascii=False),
            ),
        )


def get_created_objects(run_id: str) -> dict:
    """Return the created-objects registry for a run, or an empty registry."""
    with _connect() as con:
        row = con.execute(
            "SELECT vms_json, nics_json, tags_json, cleaned FROM created_objects WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if row:
        try:
            return {
                "vms":     json.loads(row[0] or "[]"),
                "nics":    json.loads(row[1] or "[]"),
                "tags":    json.loads(row[2] or "[]"),
                "cleaned": bool(row[3]),
            }
        except Exception:
            pass
    return {"vms": [], "nics": [], "tags": [], "cleaned": False}


def mark_cleaned(run_id: str) -> None:
    """Mark a run's VMs as cleaned so cleanup cannot be triggered again."""
    with _lock, _connect() as con:
        con.execute(
            "UPDATE created_objects SET cleaned=1 WHERE run_id=?",
            (run_id,),
        )


def get_result(run_id: str) -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT result_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    if row and row["result_json"]:
        return json.loads(row["result_json"])
    return None


def migrate_from_artifacts(artifacts_dir: Path) -> None:
    """Import existing file-based results into SQLite (one-time migration)."""
    if not artifacts_dir.exists():
        return
    with _connect() as con:
        existing = {r[0] for r in con.execute("SELECT run_id FROM runs").fetchall()}

    for d in sorted(artifacts_dir.iterdir()):
        if not d.is_dir() or d.name in existing:
            continue
        rf = d / "result.json"
        ef = d / "error.json"
        try:
            if rf.exists():
                data = json.loads(rf.read_text(encoding="utf-8"))
                finish_run(d.name, data)
            elif ef.exists():
                finish_run_error(d.name, "error")
        except Exception:
            pass


# ── Config ────────────────────────────────────────────────────────────────────

def get_config() -> dict:
    with _connect() as con:
        rows = con.execute("SELECT key, value FROM config").fetchall()
    if not rows:
        return {}
    return {r["key"]: json.loads(r["value"]) for r in rows}


def save_config(data: dict) -> None:
    with _lock, _connect() as con:
        con.execute("DELETE FROM config")
        for k, v in data.items():
            con.execute(
                "INSERT INTO config (key, value) VALUES (?, ?)",
                (k, json.dumps(v, ensure_ascii=False)),
            )


def migrate_config_from_file(config_path: Path) -> None:
    """One-time import from nettest.config.json into config table."""
    if not config_path.exists():
        return
    with _connect() as con:
        count = con.execute("SELECT COUNT(*) FROM config").fetchone()[0]
    if count > 0:
        return
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        save_config(data)
    except Exception:
        pass


# ── Networks ──────────────────────────────────────────────────────────────────

_NETWORK_FIELDS = ("datacenter", "cluster", "pg", "vlan", "subnet", "gw", "vrf")


def get_networks() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT datacenter, cluster, pg, vlan, subnet, gw, vrf FROM networks ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def save_networks(rows: list[dict]) -> None:
    with _lock, _connect() as con:
        con.execute("DELETE FROM networks")
        for row in rows:
            con.execute(
                "INSERT INTO networks (datacenter, cluster, pg, vlan, subnet, gw, vrf) VALUES (?,?,?,?,?,?,?)",
                tuple(str(row.get(f, "")).strip() for f in _NETWORK_FIELDS),
            )


def migrate_networks_from_csv(csv_path: Path) -> None:
    """One-time import from input CSV into networks table."""
    if not csv_path.exists():
        return
    with _connect() as con:
        count = con.execute("SELECT COUNT(*) FROM networks").fetchone()[0]
    if count > 0:
        return
    import csv as _csv
    rows = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                rows.append({k.strip().lower(): str(v).strip() for k, v in row.items()})
        if rows:
            save_networks(rows)
    except Exception:
        pass


# ── VRF Rules ─────────────────────────────────────────────────────────────────

def get_vrf_rules() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT id, from_vrf, to_vrf, action, comment FROM vrf_rules ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def save_vrf_rules(rules: list[dict]) -> None:
    with _lock, _connect() as con:
        con.execute("DELETE FROM vrf_rules")
        for r in rules:
            con.execute(
                "INSERT INTO vrf_rules (from_vrf, to_vrf, action, comment) VALUES (?,?,?,?)",
                (
                    str(r.get("fromVrf", r.get("from_vrf", ""))).strip(),
                    str(r.get("toVrf", r.get("to_vrf", ""))).strip(),
                    str(r.get("action", "PASS")).strip().upper(),
                    str(r.get("comment", "")).strip(),
                ),
            )

