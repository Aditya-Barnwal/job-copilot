"""Public demo mode: `JOBCOPILOT_DEMO=1` (the Cloud Run deploy, `make demo-local`).

The demo shows the app on fake data only. While a demo session is active:

  * the DB, résumé PDFs and logs live in a fresh temp dir (Cloud Run's disk is
    ephemeral anyway), built at startup from the committed fixtures in
    `tests/fixtures` by the normal fixture pipeline (`daily --no-network`);
  * the profile is a temp copy of `profile.example/` (the fake "Alex Example"),
    so the real `profile/` and `.env` are never read (`server/app.py` also skips
    `load_dotenv` in demo mode, and the image contains neither);
  * outbound network is refused: `socket.getaddrinfo` / `socket.connect` only
    accept loopback and unspecified addresses, so no connector, LLM SDK or
    model download can reach another host;
  * LLM calls raise `DemoDisabled` ("demo: disabled"), `llm.health()` returns a
    canned status, and the embedding model is never loaded (torch is never
    imported), so semantic scoring is skipped as it is when the model is absent;
  * secret-looking env vars (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`) are
    hidden from the process.

The HTTP side (`server/app.py`) adds a 403 "demo: disabled" for tailoring,
cover letters, emails, uploads and pipeline runs; tracker status changes are
allowed and only ever touch the ephemeral demo DB.

`session()` undoes everything on exit (env, patched functions, socket guard,
temp dir), so tests can run a demo app in-process.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import socket
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger("jobcopilot.demo")

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "tests" / "fixtures"
EXAMPLE_PROFILE = ROOT / "profile.example"
REAL_PROFILE = ROOT / "profile"
REAL_ENV = ROOT / ".env"

DEMO_ENV = "JOBCOPILOT_DEMO"
BANNER = "Demo — sample data, not a real job seeker"
DISABLED = "demo: disabled"

_TRUTHY = {"1", "true", "yes", "on"}
_SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)$", re.I)
_LOCAL_NAMES = {"localhost", "localhost.", "ip6-localhost", ""}


def enabled() -> bool:
    """True when JOBCOPILOT_DEMO is set to 1/true/yes/on (read at call time)."""
    return os.environ.get(DEMO_ENV, "").strip().lower() in _TRUTHY


class DemoDisabled(RuntimeError):
    """Raised by anything the public demo switches off (LLM calls, the network)."""


class NetworkBlocked(DemoDisabled, ConnectionRefusedError):
    """Outbound connection to a non-local host while demo mode is on."""


# --------------------------------------------------------------------------- network guard

def is_local_host(host: Any) -> bool:
    """Loopback / unspecified addresses and 'localhost' only."""
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    h = str(host).strip().lower().strip("[]")
    if h in _LOCAL_NAMES:
        return True
    h = h.split("%", 1)[0]          # IPv6 zone id
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def _refuse(host: Any) -> None:
    raise NetworkBlocked(f"{DISABLED}: outbound network to {host!r} is off in the public demo")


def _make_guards(orig_getaddrinfo: Callable, orig_connect: Callable, orig_connect_ex: Callable):
    def getaddrinfo(host, *args, **kwargs):
        if not is_local_host(host):
            _refuse(host)
        return orig_getaddrinfo(host, *args, **kwargs)

    def _check(sock: socket.socket, address: Any) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0] if isinstance(address, tuple) and address else address
            if not is_local_host(host):
                _refuse(host)

    def connect(self, address):
        _check(self, address)
        return orig_connect(self, address)

    def connect_ex(self, address):
        _check(self, address)
        return orig_connect_ex(self, address)

    return getaddrinfo, connect, connect_ex


# --------------------------------------------------------------------------- canned LLM

def _llm_disabled(*_a: Any, **_k: Any):
    raise DemoDisabled(f"{DISABLED}: LLM calls are off in the public demo")


def _llm_health() -> dict:
    return {"provider": "demo", "chain": ["demo"], "configured": None, "model": None,
            "reachable": False, "model_ok": False, "available_sample": [],
            "hint": f"{DISABLED}: no LLM in the public demo", "demo": True}


def _no_embedding_model(*_a: Any, **_k: Any):
    raise DemoDisabled(f"{DISABLED}: the embedding model is not loaded in the demo")


# --------------------------------------------------------------------------- session

@dataclass
class DemoState:
    root: Path
    db_path: Path
    profile_dir: Path
    build: dict = field(default_factory=dict)


class _Undo:
    """Records env + attribute changes so they can be reverted in reverse order."""

    def __init__(self) -> None:
        self._steps: list[Callable[[], None]] = []

    def setenv(self, name: str, value: Optional[str]) -> None:
        old = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        self._steps.append(lambda: os.environ.__setitem__(name, old) if old is not None
                           else os.environ.pop(name, None))

    def setattr(self, obj: Any, name: str, value: Any) -> None:
        had = name in vars(obj)     # False for an inherited method: undo deletes the override
        old = getattr(obj, name, None)
        setattr(obj, name, value)
        if had:
            self._steps.append(lambda: setattr(obj, name, old))
        else:
            self._steps.append(lambda: delattr(obj, name))

    def call(self, fn: Callable[[], None]) -> None:
        self._steps.append(fn)

    def undo(self) -> None:
        while self._steps:
            try:
                self._steps.pop()()
            except Exception:  # noqa: BLE001 — undo everything we can
                log.exception("demo: undo step failed")


def build_db(state: DemoState, *, day: Optional[str] = None) -> dict:
    """Fill the demo DB from the committed fixtures (no network, no LLM)."""
    from . import daily, store
    store.init_db(state.db_path)
    result = daily.run(no_network=True, fixtures_dir=FIXTURES_DIR, db_path=state.db_path, day=day)
    q = result.get("queue") or {}
    with store.connect(state.db_path) as conn:
        jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    out = {"run_id": result["run_id"], "status": result["status"],
           "queue_day": q.get("day"), "queue_counts": q.get("counts"), "jobs": jobs}
    log.info("demo DB built: %s", out)
    return out


@contextmanager
def session(*, build: bool = True, day: Optional[str] = None) -> Iterator[DemoState]:
    """Enter demo mode for the duration of the block (see module doc)."""
    from . import llm, scoring, store

    undo = _Undo()
    root = Path(tempfile.mkdtemp(prefix="jobcopilot-demo-"))
    undo.call(lambda: shutil.rmtree(root, ignore_errors=True))
    try:
        state = DemoState(root=root, db_path=root / "data" / "jobcopilot.db",
                          profile_dir=root / "profile")
        shutil.copytree(EXAMPLE_PROFILE, state.profile_dir)

        # 1. network guard first: nothing below may reach another host
        g, c, cx = _make_guards(socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex)
        undo.setattr(socket, "getaddrinfo", g)
        undo.setattr(socket.socket, "connect", c)
        undo.setattr(socket.socket, "connect_ex", cx)

        # 2. environment: temp data root, fake profile, no secrets, offline model hubs
        for name in [n for n in os.environ if _SECRET_ENV_RE.search(n)]:
            undo.setenv(name, None)
        undo.setenv(DEMO_ENV, "1")
        undo.setenv("JOBCOPILOT_NO_DOTENV", "1")
        undo.setenv("JOBCOPILOT_DB", str(state.db_path))
        undo.setenv("JOBCOPILOT_RESUMES_DIR", str(root / "data" / "resumes"))
        undo.setenv("JOBCOPILOT_PROFILE_DIR", str(state.profile_dir))
        undo.setenv("LLM_PROVIDER", "demo")
        undo.setenv("HF_HUB_OFFLINE", "1")
        undo.setenv("TRANSFORMERS_OFFLINE", "1")
        undo.setattr(store, "DEFAULT_DB_PATH", state.db_path)
        undo.setattr(store, "RESUMES_DIR", root / "data" / "resumes")

        # 3. no LLM, no embedding model (so torch is never imported)
        undo.setattr(llm, "complete", _llm_disabled)
        undo.setattr(llm, "complete_json", _llm_disabled)
        undo.setattr(llm, "available_models", _llm_disabled)
        undo.setattr(llm, "health", _llm_health)
        undo.setattr(llm, "PROVIDER", "demo")
        undo.setattr(scoring, "get_model", _no_embedding_model)

        if build:
            state.build = build_db(state, day=day)
        yield state
    finally:
        undo.undo()


# --------------------------------------------------------------------------- HTTP policy

# Writes the demo allows (all land in the ephemeral demo DB).
_WRITE_ALLOW = (
    re.compile(r"^/api/jobs/[^/]+/status$"),   # tracker status changes
    re.compile(r"^/api/meta-kv$"),             # UI preferences
    re.compile(r"^/api/user-yoe$"),
)
# Off for every method: these need an LLM and the owner's résumé facts.
_ALWAYS_OFF = re.compile(r"/(tailor|tailor-suggestion|cover-letter|draft-email|email|answers/preview)(/|$)")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def refusal(method: str, path: str) -> Optional[str]:
    """Why the demo refuses this request, or None when it is allowed."""
    if _ALWAYS_OFF.search(path):
        return (f"{DISABLED} — tailoring, cover letters and emails need an LLM and a real "
                "person's résumé facts; run job-copilot locally to use them")
    if method.upper() in _SAFE_METHODS:
        return None
    if any(p.match(path) for p in _WRITE_ALLOW):
        return None
    return (f"{DISABLED} — only tracker status changes are allowed in the public demo "
            "(they reset whenever the demo restarts)")


def inject_banner(html: str) -> str:
    """Add the demo banner right after <body> (styled by .demo-banner in styles.css)."""
    banner = f'<div class="demo-banner" role="status">{BANNER}</div>'
    m = re.search(r"<body[^>]*>", html, re.I)
    if not m:
        return banner + html
    return html[:m.end()] + "\n" + banner + html[m.end():]
