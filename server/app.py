"""FastAPI app for job-copilot: wires the routers, startup, and the static UI.

Run with `uvicorn server.app:app`. Endpoints live in `server/routers/` (one
module per area) and shared helpers in `server/deps.py`; this file only
assembles them, so new phases add a router instead of growing one module.
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env before importing jobcopilot.llm (it reads the provider at import).
# Tests set JOBCOPILOT_NO_DOTENV so real API keys never enter the test process.
if not os.environ.get("JOBCOPILOT_NO_DOTENV"):
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass

from jobcopilot import config, store  # noqa: E402
from jobcopilot.ingest import setup_logging  # noqa: E402

from . import deps  # noqa: E402
from .routers import ingest, jobs, meta, profile, tailor  # noqa: E402

STATIC_DIR = deps.STATIC_DIR


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Logs go beside the DB, so a redirected store (tests, Docker volume)
    # never writes into the repo's data/ directory.
    setup_logging(store.data_dir())
    store.init_db()
    deps.load_company_meta(config.companies_path())
    yield


app = FastAPI(title="job-copilot", lifespan=lifespan)

# Browser hardening. The UI renders scraped job data and model output, so a
# strict CSP is the backstop if escaping ever misses a spot: scripts, styles
# and fetches only from this origin, no inline script or style attributes, no
# plugins, no framing. FastAPI's own /docs and /redoc pages load Swagger/ReDoc
# from a CDN, so they get every header except the CSP.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; style-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_CSP_EXEMPT_PREFIXES = ("/docs", "/redoc")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if not request.url.path.startswith(_CSP_EXEMPT_PREFIXES):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


for _router in (meta.router, ingest.router, jobs.router, profile.router, tailor.router):
    app.include_router(_router)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"ok": True, "message": "UI not built yet"}
