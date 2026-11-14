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
    try:
        showcase = seed_showcase(state)
    except Exception as exc:  # noqa: BLE001 — the demo still works without the sample states
        log.warning("demo showcase not seeded: %s", type(exc).__name__)
        showcase = {}
    with store.connect(state.db_path) as conn:
        jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    out = {"run_id": result["run_id"], "status": result["status"],
           "queue_day": q.get("day"), "queue_counts": q.get("counts"), "jobs": jobs,
           "showcase": showcase}
    log.info("demo DB built: %s", out)
    return out


# --------------------------------------------------------------------------- showcase

_DAY = 86400.0


def _fa(label: str, kind: str, source: str, *, approved: bool, required: bool = False,
        note: str = "", value: Optional[str] = None) -> dict:
    """One fill-plan field as stored (values are never stored; uploads keep their key)."""
    return {"label": label, "kind": kind, "source": source, "value": value,
            "confidence": 1.0 if approved else 0.0, "needs_user": not approved, "approved": approved,
            "required": required, "note": note, "locator": ""}


def seed_showcase(state: DemoState, now: Optional[float] = None) -> dict:
    """Give the demo DB a lived-in look, on the fake profile only: tracker states with
    dates, two tailored résumé versions for the top job (built from profile.example's
    confirmed facts, no LLM), fill plans with open questions, and two sample fill
    sessions (no browser runs in the demo). Everything is sample data."""
    import json
    import time as _time
    from . import facts as F
    from . import queue as jq
    from . import store, tailor_v2
    from .grounding import check_lines

    db = state.db_path
    now = _time.time() if now is None else float(now)
    queue = jq.list_queue(lane="main", db_path=db).get("items") or []
    extra = jq.list_matches(lane="all", limit=60, db_path=db).get("items") or []
    in_queue = {j["key"] for j in queue}
    others = [j for j in extra if j["key"] not in in_queue]
    if len(queue) < 3 or len(others) < 6:
        return {}
    top, ready, needs = queue[0], queue[2], queue[4]

    def history(key: str, steps: list[tuple[str, float]], source: str = "user") -> None:
        for status, days_ago in steps:
            store.set_status(key, status, source=source if status not in ("tailored", "ready_to_submit") else "system",
                             at=now - days_ago * _DAY, db_path=db)

    # -- tailored versions for the top job (v1 flagged, v2 clean), and an approved one for `ready`
    rf = F.load(state.profile_dir / "resume_facts.yaml")
    bases = F.load_bases_for(rf.path) or {}

    def artifact(job: dict, plan_over: dict, status: str, *, approved: bool = False) -> Optional[int]:
        job_row = store.get_job(job["key"], db) or {}
        base = tailor_v2._pick_base(job_row, bases) or next(iter(bases), None)
        if not base:
            return None
        usable = F.usable_facts(rf, base)
        plan = dict(tailor_v2.EMPTY_PLAN, **plan_over)
        plan.setdefault("kept_original", [])
        plan["attested_keywords"], plan["struck_keywords"] = [], []
        lines = tailor_v2.apply_plan(usable, bases[base], plan)
        g = check_lines(lines, usable, attested=[])
        aid, _v = store.create_artifact(job["key"], base, db)
        store.update_artifact(aid, db, edit_plan=plan, grounding=g, pages=1,
                              status=status if g["clean"] or status != "ok" else "needs_review",
                              facts_hash=tailor_v2.facts_hash(usable, lines),
                              **({"approved_at": now - 3 * _DAY} if approved else {}))
        return aid

    reorder = {"order": ["exp.acme.b2", "exp.acme.b1"], "skills_order": ["skill.python", "skill.go"]}
    artifact(top, dict(reorder, rephrase={
        "exp.acme.b1": "Led a team building a Go order service handling 500+ requests per second behind a REST API."}),
        "needs_review")
    a2 = artifact(top, dict(reorder, rephrase={
        "exp.acme.b1": "Built a REST API for a Go order service handling 200+ requests per second."}), "ok")
    a3 = artifact(ready, reorder, "approved", approved=True)
    if a2:
        store.link_job_artifact(top["key"], a2, db)
    if a3:
        store.link_job_artifact(ready["key"], a3, db)

    # -- tracker states, oldest first
    history(top["key"], [("tailored", 0.2)])
    history(ready["key"], [("found", 5), ("tailored", 4), ("ready_to_submit", 3)])
    history(needs["key"], [("found", 1)])
    plan_tracker = [
        (others[0], [("found", 12), ("submitted", 9)]),
        (others[1], [("found", 4), ("submitted", 2)]),
        (others[2], [("found", 16), ("submitted", 12), ("oa", 4)]),
        (others[3], [("found", 26), ("submitted", 20), ("oa", 14), ("interview", 3)]),
        (others[4], [("found", 31), ("submitted", 25), ("rejected", 6)]),
        (others[5], [("found", 40), ("submitted", 33), ("interview", 16), ("offer", 1)]),
    ]
    for job, steps in plan_tracker:
        history(job["key"], steps)
    with store.connect(db) as conn:      # one "detected" submission, as the confirmation watcher records it
        conn.execute("UPDATE jobs SET submit_source = 'detected' WHERE key = ?", (others[1]["key"],))

    # -- fill plans (labels and sources only) + two sample sessions
    def save_plan(job: dict, steps: list[dict], unresolved: list[dict], snapshot: list[dict]) -> None:
        plan = {"job_key": job["key"], "ats": "greenhouse", "url": job.get("apply_url") or job.get("url") or "",
                "steps": [steps], "unresolved": unresolved, "files": {}, "file_kinds": ["resume"],
                "created_at": "", "format": 2, "target_url": job.get("apply_url") or "",
                "snapshot": {"source": "demo", "steps": [snapshot]}}
        store.save_fill_plan(job["key"], plan, ats="greenhouse", url=plan["url"],
                             unresolved_count=len(unresolved), created_at=now - 0.3 * _DAY,
                             stale_after=now + _DAY, db_path=db)

    company = top.get("company") or "the company"
    common = [
        _fa("First Name", "fill", "answers.yaml:personal.first_name", approved=True, required=True),
        _fa("Last Name", "fill", "answers.yaml:personal.last_name", approved=True, required=True),
        _fa("Email", "fill", "answers.yaml:personal.email", approved=True, required=True),
        _fa("Phone", "fill", "answers.yaml:personal.phone", approved=True),
        _fa("Resume/CV", "upload", "file:resume", approved=True, required=True, value="resume"),
        _fa("LinkedIn Profile", "fill", "answers.yaml:personal.links.linkedin", approved=True),
        _fa("Are you legally authorized to work in India?", "select", "answers.yaml:work_authorization.india",
            approved=True, required=True),
        _fa("Notice period", "fill", "answers.yaml:availability.notice_period", approved=True),
        _fa("How did you hear about us?", "select", "answers.yaml:screening_defaults.how_did_you_hear", approved=True),
    ]
    why_q = f"Why do you want to work at {company}?"
    reloc_q = "Are you open to relocating to Bengaluru?"
    save_plan(top, common + [
        _fa(why_q, "fill", "generated", approved=False, required=True, note="drafted from your facts; needs your review"),
        _fa(reloc_q, "radio", "answers.yaml:location_preferences.willing_to_relocate", approved=False,
            note="location_preferences is not approved yet"),
    ], [{"label": why_q, "reason": "open question: needs your answer", "required": True},
        {"label": reloc_q, "reason": "location_preferences is not approved yet"}],
        [{"label": reloc_q, "type": "radio", "options": ["Yes", "No"], "required": False}])
    save_plan(ready, common, [], [])
    kafka_q = "How many years of experience do you have with Kafka?"
    shift_q = "Are you comfortable with occasional on-call shifts?"
    save_plan(needs, common[:6] + [
        _fa(kafka_q, "select", "none", approved=False, required=True, note="no matching answer in answers.yaml"),
        _fa(shift_q, "radio", "none", approved=False, note="no matching answer in answers.yaml"),
    ], [{"label": kafka_q, "reason": "no matching answer in answers.yaml", "required": True},
        {"label": shift_q, "reason": "no matching answer in answers.yaml"}],
        [{"label": kafka_q, "type": "select", "options": ["None", "Less than 1 year", "1-2 years", "3+ years"], "required": True},
         {"label": shift_q, "type": "radio", "options": ["Yes", "No"], "required": False}])
    with store.connect(db) as conn:
        for job, status, outcome, ago in (
            (ready, "ready_to_submit", {"status": "ready", "reason": "stopped before Submit", "filled": 11,
                                        "left_empty": 0, "highlighted": [], "step_reached": "Application"}, 0.05),
            (needs, "needs_you", {"status": "needs_you", "reason": "required questions left for you", "filled": 7,
                                  "left_empty": 2, "highlighted": [kafka_q, shift_q], "step_reached": "Application"}, 0.1),
        ):
            conn.execute("INSERT INTO autofill_sessions(job_key, status, outcome, started_at, pid) "
                         "VALUES (?, ?, ?, ?, NULL)", (job["key"], status, json.dumps(outcome), now - ago * _DAY))
            conn.execute("UPDATE jobs SET fill_status = ? WHERE key = ?",
                         ("ready" if status == "ready_to_submit" else status, job["key"]))
    return {"tailored": top["key"], "ready": ready["key"], "needs_you": needs["key"],
            "tracked": len(plan_tracker) + 3}


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
    re.compile(r"^/api/jobs/[^/]+/unhide$"),   # undo "not interested" (a status change too)
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
