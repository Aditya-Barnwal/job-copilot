"""FastAPI app for job-copilot: wires the routers, startup, and the static UI.

Run with `uvicorn server.app:app`. Endpoints live in `server/routers/` (one
module per area) and shared helpers in `server/deps.py`; this file only
assembles them, so new phases add a router instead of growing one module.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import urllib.parse
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobcopilot import demo  # noqa: E402 — light (stdlib only); needed before .env

# Load .env before importing jobcopilot.llm (it reads the provider at import).
# Tests set JOBCOPILOT_NO_DOTENV so real API keys never enter the test process.
# Demo mode (JOBCOPILOT_DEMO=1, the public Cloud Run instance) never reads .env.
if not os.environ.get("JOBCOPILOT_NO_DOTENV") and not demo.enabled():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass

from jobcopilot import config, store  # noqa: E402
from jobcopilot.ingest import setup_logging  # noqa: E402

from . import deps  # noqa: E402
from .routers import ingest, jobs, meta, profile, queue, tailor  # noqa: E402

STATIC_DIR = deps.STATIC_DIR


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with ExitStack() as stack:
        # Demo mode: temp DB built from fixtures, fake profile, no network/LLM
        # (jobcopilot/demo.py). Entered first so everything below uses it.
        if demo.enabled():
            stack.enter_context(demo.session())
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
async def demo_policy(request: Request, call_next):
    """Demo mode only: 403 for tailoring/LLM/upload/pipeline endpoints (demo.refusal)."""
    if demo.enabled():
        why = demo.refusal(request.method, request.url.path)
        if why:
            return JSONResponse(status_code=403, content={"detail": why, "demo": True})
    return await call_next(request)


# --- local-API hardening (CSRF + DNS rebinding) ------------------------------
# The API drives a browser in the user's own profile (autofill, hand-over), so
# a web page the user happens to visit must not be able to reach it:
#   1. Host allowlist: 127.0.0.1 / localhost / ::1, plus *.run.app in the public
#      demo (JOBCOPILOT_DEMO=1 on Cloud Run) and anything listed in
#      JOBCOPILOT_ALLOWED_HOSTS (comma separated; "*.example.org" = suffix).
#      A rebound DNS name (evil.example -> 127.0.0.1) gets 400 on every route.
#   2. State-changing methods need Origin (or, failing that, Referer) equal to
#      this app's own origin, and
#   3. the custom header X-JobCopilot: 1, which a cross-site page can't send
#      without a CORS preflight (and the app answers no preflight).
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "X-JobCopilot"


def _hostname(netloc: str) -> str:
    try:
        return (urllib.parse.urlsplit("//" + netloc).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _netloc(value: str) -> str:
    """host[:port] of a URL or Host header, lowercased, default ports dropped."""
    try:
        parts = urllib.parse.urlsplit(value if "//" in value else "//" + value)
        host, port = (parts.hostname or "").lower().rstrip("."), parts.port
    except ValueError:
        return ""
    if not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return host if port in (None, 80, 443) else f"{host}:{port}"


def host_allowed(host_header: str) -> bool:
    host = _hostname(host_header or "")
    if not host:
        return False
    if host in _LOCAL_HOSTS:
        return True
    if demo.enabled() and host.endswith(".run.app"):    # the Cloud Run demo (docs/DEPLOY.md)
        return True
    for entry in os.environ.get("JOBCOPILOT_ALLOWED_HOSTS", "").split(","):
        entry = entry.strip().lower().rstrip(".")
        if not entry:
            continue
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


def csrf_refusal(request: Request) -> str | None:
    """Why a state-changing request is refused, or None when it is our own UI's."""
    own = _netloc(request.headers.get("host") or "")
    origin = request.headers.get("origin")
    if origin is not None:
        if origin.strip().lower() == "null" or _netloc(origin) != own:
            return "cross-origin request refused"
    else:
        referer = request.headers.get("referer")
        if not referer or _netloc(referer) != own:
            return "missing Origin: requests that change state must come from the app's own page"
    if request.headers.get(CSRF_HEADER) != "1":
        return f"missing {CSRF_HEADER} header"
    return None


# Registered between demo_policy and security_headers: it runs before the demo
# policy and the routes, and its refusals still get the security headers.
@app.middleware("http")
async def local_api_guard(request: Request, call_next):
    if not host_allowed(request.headers.get("host") or ""):
        return JSONResponse(status_code=400, content={"detail": "unknown Host header"})
    if request.method.upper() in _UNSAFE_METHODS:
        why = csrf_refusal(request)
        if why:
            return JSONResponse(status_code=403, content={"detail": why})
    return await call_next(request)


# Registered after demo_policy so it wraps it: refusals get the headers too.
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if not request.url.path.startswith(_CSP_EXEMPT_PREFIXES):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


# queue.router goes before jobs.router: its POST /api/jobs/{key}/status (tracker
# statuses + history, v1-compatible) must win over the older handler.
_ROUTERS = [meta.router, ingest.router, queue.router, jobs.router, profile.router, tailor.router]
# Phase 3 writing routes (cover letters, emails, answers) when that module exists.
# Only a MISSING module is skipped; an error inside it still fails loudly.
if importlib.util.find_spec("server.routers.writing") is not None:
    from .routers import writing  # noqa: E402
    _ROUTERS.append(writing.router)
# Phase 4 autofill routes (same rule: only a missing module is skipped).
if importlib.util.find_spec("server.routers.autofill") is not None:
    from .routers import autofill  # noqa: E402
    _ROUTERS.append(autofill.router)
# Phase 5 routes for the React UI (review card, answers writer, facts, portals, CSV).
from .routers import ui  # noqa: E402
_ROUTERS.append(ui.router)
for _router in _ROUTERS:
    app.include_router(_router)


# --- the React UI (web/ -> `make ui` -> server/static/dist) --------------------
# Built files only; Node is never needed at runtime. Paths under /api are never
# shadowed: the SPA fallback below is registered after every router and refuses
# /api/... itself. Unknown non-API paths get index.html (client-side routes).
DIST_DIR = STATIC_DIR / "dist"
_NOT_BUILT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Job-Copilot: build the UI</title></head>
<body>
<h1>Job-Copilot</h1>
<p>The web UI isn't built yet. Run <code>make ui</code> (needs Node 22 once, at build time),
then reload this page.</p>
<p>The API is running: try <a href="/api/meta">/api/meta</a>.</p>
</body></html>
"""


def _index_html() -> str:
    index = DIST_DIR / "index.html"
    try:
        html = index.read_text(encoding="utf-8")
    except OSError:
        html = _NOT_BUILT
    return demo.inject_banner(html) if demo.enabled() else html


def _dist_file(rel: str) -> Path | None:
    """A file inside dist/ (no traversal), or None."""
    if not rel or "\x00" in rel:
        return None
    root = DIST_DIR.resolve()
    p = (root / rel).resolve()
    return p if p.is_relative_to(root) and p.is_file() else None


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def root():
    return HTMLResponse(_index_html(), headers={"Cache-Control": "no-cache"})


@app.api_route("/assets/{rel:path}", methods=["GET", "HEAD"], include_in_schema=False)
def assets(rel: str):
    p = _dist_file("assets/" + rel)
    if p is None:
        return JSONResponse(status_code=404, content={"detail": "not found"})
    # Vite fingerprints asset names, so they can be cached for good.
    return FileResponse(str(p), headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.api_route("/{rel:path}", methods=["GET", "HEAD"], include_in_schema=False)
def spa(rel: str):
    if rel == "api" or rel.startswith(("api/", "docs", "redoc", "openapi.json")):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    p = _dist_file(rel) if "/" not in rel else None      # top-level public files (favicon, theme-init.js)
    if p is not None and p.name != "index.html":
        return FileResponse(str(p), headers={"Cache-Control": "no-cache"})
    return HTMLResponse(_index_html(), headers={"Cache-Control": "no-cache"})
