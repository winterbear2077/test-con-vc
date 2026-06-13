#!/usr/bin/env python3
"""
Web UI for vCenter Network Policy Test Runner.

Start:  ./.venv/bin/uvicorn web_app:app --host 127.0.0.1 --port 5000
Open:   http://localhost:5000
"""
from __future__ import annotations

import logging
import re as _re

# Configure root logger before uvicorn captures it, so worker thread
# errors and nettest package logs propagate to stderr for diagnostics.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import nettest.db as _db
from nettest.api.deps import WORKSPACE, ARTIFACTS, CONFIG_FILE, STATIC_DIR, _read_config
from nettest.api import routes_config, routes_networks, routes_vcenter, routes_run, routes_history

# ── Directories ───────────────────────────────────────────────────────────────
STATIC_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACTS.mkdir(parents=True, exist_ok=True)

# ── SQLite init ───────────────────────────────────────────────────────────────
_db.init(WORKSPACE / "nettest.db")
_db.migrate_from_artifacts(ARTIFACTS)
_db.migrate_config_from_file(CONFIG_FILE)
_db.migrate_networks_from_csv(WORKSPACE / "input.csv")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="vCenter Network Test")

# allow_origin_regex=".*" echoes the request's Origin header back, which is
# required when allow_credentials=True (the CORS spec forbids wildcard + credentials).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Ensure CORS headers are present even on unhandled 500 responses ───────────
# FastAPI's default exception handler bypasses the CORS middleware, so cross-origin
# callers (e.g. the vCenter plugin iframe) would receive a CORS-blocked error page.
@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    origin = request.headers.get("origin", "")
    headers = {"Access-Control-Allow-Origin": origin or "*"} if origin else {}
    logging.getLogger(__name__).exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers=headers,
    )

app.include_router(routes_config.router)
app.include_router(routes_networks.router)
app.include_router(routes_vcenter.router)
app.include_router(routes_run.router)
app.include_router(routes_history.router)


# ── Serve SPA ─────────────────────────────────────────────────────────────────
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(str(STATIC_DIR / "index.html"), status_code=204)


@app.get("/")
@app.get("/index.html")
def root(request: Request):
    """Serve index.html, replacing the <base> tag so assets resolve correctly
    when the page is iframed by vCenter from a different origin."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    request_origin = f"{request.url.scheme}://{request.url.netloc}"

    # Detect iframe / plugin context:
    #   1. URL has ?sessionId= or ?plugin=1  (vSphere Client passes these)
    #   2. Referer header is from a different origin than this server
    # In those cases use plugin_url so EventSource / API calls resolve correctly.
    # For direct browser access neither condition holds → use request origin.
    params = request.query_params
    referer = request.headers.get("referer", "")
    referer_origin = ""
    try:
        from urllib.parse import urlparse as _up
        referer_origin = _up(referer).scheme + "://" + _up(referer).netloc if referer else ""
    except Exception:
        pass

    in_iframe = bool(params.get("sessionId") or params.get("plugin")) or (
        bool(referer_origin) and referer_origin != request_origin
    )

    plugin_url = _read_config().get("plugin_url", "").rstrip("/") if in_iframe else ""
    base_origin = plugin_url if plugin_url else request_origin
    base_tag = f'<base href="{base_origin}/">'
    html, n = _re.subn(r'<base\s[^>]*>', base_tag, html, count=1)
    if not n:
        html = html.replace("<head>", f"<head>\n  {base_tag}", 1)
    return HTMLResponse(html)


# Mount at "/" so Angular assets resolve relative to the SPA's base href.
app.mount("/", StaticFiles(directory=str(STATIC_DIR)), name="static")
