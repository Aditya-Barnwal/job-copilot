"""Autofill orchestration: nightly fill plans, the on-click headed fill, and
passive submission detection (Phase 4, D3).

    plan_for_job(job_key, *, db_path=None) -> FillPlan     # read-only; never types
    launch_fill(job_key, *, db_path=None) -> int            # session id; runs in a thread
    session_status(session_id, *, db_path=None) -> dict | None
    handover(session_id) -> bool                            # lift the DOM submit block
    watch_for_confirmation(session) -> str                  # passive URL/text polling
    plan_today(day=None, *, limit=None, db_path=None) -> dict   # daily.py hook

What this module guarantees, whatever an adapter returns:
  * nothing here clicks, presses or submits anything; the only page actions are
    the SafePage calls the adapter/executor makes (workstream K), and the plan
    it hands them has been through `enforce_policy`: a value is kept only when
    answers.answer_question said needs_user=False with confidence >=
    `min_confidence`; consent/attestation checkboxes are always left alone;
    uploads must be the job's APPROVED résumé / cover-letter files.
  * denylisted hosts (http.py's built-in list + config) are never opened: the
    caller gets a link-out instead (AutofillRefused code 'denylisted').
  * planning is read-only: the Greenhouse public questions API where possible,
    otherwise a headless page load whose every non-GET request is aborted.
  * logs carry field labels, sources and counts — never values.
"""
from __future__ import annotations

import logging
import os
import queue as _queue
import re
import threading
import time
import urllib.parse
import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .. import config as _config
from .. import store

log = logging.getLogger("jobcopilot.autofill.service")

from .plan import FieldAction, FillPlan, Outcome  # workstream K's types


def _outcome_dict(o: Any) -> dict:
    if isinstance(o, dict):
        return dict(o)
    if hasattr(o, "to_dict"):
        return o.to_dict()
    return {k: getattr(o, k, None) for k in ("status", "reason", "filled", "left_empty", "highlighted",
                                             "submit_selector", "screenshot", "step_reached")}


# --------------------------------------------------------------------------- config

DEFAULTS: dict[str, Any] = {
    "browser": {"headed": True, "channel": "chrome", "profile_dir": "~/.jobcopilot/browser-profile"},
    "min_confidence": 0.8,
    "freshness_hours": 6,
    "plan_stale_hours": 24,
    "nightly_limit": 25,
    "timeouts": {"page_load_sec": 30, "fill_sec": 180, "watch_min": 180, "watch_poll_sec": 2},
    "denied_hosts_extra": [],
}


def settings() -> dict[str, Any]:
    """config/autofill.yaml merged over DEFAULTS (a broken file raises, loudly)."""
    raw = _config.load("autofill")
    return _config._deep_merge(DEFAULTS, raw)


class AutofillRefused(Exception):
    """A fill / plan the service won't do. `code` is machine-readable, `http` the API status."""

    def __init__(self, code: str, detail: str, *, http: int = 409, link_out: Optional[str] = None,
                 session_id: Optional[int] = None):
        super().__init__(detail)
        self.code, self.detail, self.http = code, detail, http
        self.link_out, self.session_id = link_out, session_id

    def to_dict(self) -> dict:
        d = {"code": self.code, "detail": self.detail}
        if self.link_out:
            d["link_out"] = self.link_out
        if self.session_id is not None:
            d["session_id"] = self.session_id
        return d


# --------------------------------------------------------------------------- hosts / ATS

def _denied_hosts(cfg: Optional[dict] = None) -> frozenset[str]:
    from .. import http as _http
    cfg = cfg if cfg is not None else settings()
    extra = (_config.sources().get("http") or {}).get("denied_hosts") or []
    hosts = [*_http.BUILTIN_DENIED_HOSTS, *extra, *(cfg.get("denied_hosts_extra") or [])]
    return frozenset(str(h).strip().lower().strip(".") for h in hosts if str(h).strip())


def denied_host(url: str, cfg: Optional[dict] = None) -> Optional[str]:
    """The do-not-automate entry for `url`, if any: the SAME rule the browser
    guard enforces (guard.is_denied: the host or a subdomain, plus country
    sites of a denied brand such as indeed.co.uk / linkedin.cn, plus lnkd.in),
    with this config's extra hosts. A URL that isn't plain http(s) is treated
    as denied (never automated)."""
    from .guard import is_denied
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return "invalid-url"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "invalid-url"
    return is_denied(parts.hostname, extra=_denied_hosts(cfg))


def target_url(job: dict) -> str:
    """Where the application form lives: the apply link, else the posting."""
    for k in ("apply_url", "url"):
        v = (job.get(k) or "").strip()
        if v:
            return v
    return ""


_ATS_HOSTS = (
    ("greenhouse", re.compile(r"(^|\.)greenhouse\.io$")),
    ("lever", re.compile(r"(^|\.)lever\.co$")),
    ("ashby", re.compile(r"(^|\.)ashbyhq\.com$")),
    ("workday", re.compile(r"(^|\.)(myworkdayjobs|myworkdaysite|workday)\.com$")),
)


def detect_ats(url: str, job: Optional[dict] = None) -> str:
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    for name, rx in _ATS_HOSTS:
        if rx.search(host):
            return name
    if "gh_jid=" in (url or ""):
        return "greenhouse"
    src = ((job or {}).get("source") or "").lower()
    return src if src in ("greenhouse", "lever", "ashby", "workday") else "generic"


def adapter_for(url: str, ats: str) -> Any:
    """The registered adapter for this URL (workstream K's registry), or None."""
    try:
        from . import adapters as _ad  # type: ignore
    except Exception:  # noqa: BLE001 - adapters not built yet
        return None
    for name in ("adapter_for", "get_adapter", "pick_adapter", "for_url", "resolve"):
        fn = getattr(_ad, name, None)
        if callable(fn):
            try:
                return fn(url)
            except Exception as exc:  # noqa: BLE001
                log.warning("adapter lookup failed ats=%s: %s", ats, type(exc).__name__)
                return None
    return None


# --------------------------------------------------------------------------- files

def approved_files(job: dict, db_path: Any = None) -> tuple[dict[str, str], list[dict]]:
    """({'resume': pdf, 'cover_letter'?: file}, unresolved) from the job's APPROVED artifact only."""
    art = store.get_artifact(int(job["artifact_id"]), db_path) if job.get("artifact_id") else None
    if not art or art.get("status") != "approved" or art.get("job_key") != job.get("key"):
        return {}, [{"label": "Résumé", "reason": "needs approved résumé", "required": True}]
    root = store.applications_dir(db_path).resolve()
    files: dict[str, str] = {}
    for kind, col in (("resume", "pdf_path"), ("cover_letter", "cover_letter_path")):
        p = art.get(col)
        if p and Path(p).is_file() and Path(p).resolve().is_relative_to(root):
            files[kind] = str(Path(p).resolve())
    if "resume" not in files:
        return files, [{"label": "Résumé", "reason": "needs approved résumé (PDF missing)", "required": True}]
    return files, []


# --------------------------------------------------------------------------- snapshots
# A snapshot is {"source": "greenhouse_api" | "page", "url", "steps": [[field, ...]]};
# field = {label, name, locator, type, required, options, hidden}. type is one of
# text | textarea | number | date | select | radio | checkbox | multi | file.

_GH_PATH = re.compile(r"/(?P<board>[\w-]+)/jobs/(?P<id>\d+)")


def greenhouse_ids(job: dict, url: str, db_path: Any = None) -> Optional[tuple[str, str]]:
    """(board_token, job_id) for the public questions API, when we can tell."""
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower()
    q = urllib.parse.parse_qs(parts.query)
    if host.endswith("greenhouse.io"):
        m = _GH_PATH.search(parts.path)
        if m:
            return m.group("board"), m.group("id")
        if q.get("for") and q.get("token"):          # embed/job_app?for=<board>&token=<id>
            return q["for"][0], q["token"][0]
    jid = (q.get("gh_jid") or [None])[0]
    if (job.get("source") or "") == "greenhouse":
        jid = jid or job.get("external_id")
    if not jid:
        return None
    from .. import companies as _companies
    try:
        rec = _companies.get_company(job.get("company") or "", db_path=db_path)
    except Exception:  # noqa: BLE001
        rec = None
    if rec and (rec.get("ats") or "") == "greenhouse" and rec.get("ats_slug"):
        return rec["ats_slug"], str(jid)
    return None


def fetch_greenhouse_questions(board: str, job_id: str) -> Optional[dict]:
    """GET boards-api …/jobs/{id}?questions=true through the polite client (rate
    limit, robots, denylist). None when the posting is gone (404/410)."""
    from .. import http as _http
    url = (f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board, safe='')}"
           f"/jobs/{urllib.parse.quote(str(job_id), safe='')}")
    try:
        return _http.default_client().get_json(url, params={"questions": "true"})
    except _http.HttpStatusError as exc:
        if exc.status_code in (404, 410):
            return None
        raise


_GH_TYPES = {"input_text": "text", "textarea": "textarea", "input_file": "file",
             "multi_value_single_select": "select", "multi_value_multi_select": "multi",
             "input_hidden": None}


def _css_attr(name: str, value: str) -> str:
    return f'[{name}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"]'


def snapshot_from_greenhouse(data: dict, url: str) -> dict:
    """Greenhouse questions-API JSON -> snapshot (one step). A question with both a
    file and a paste-text field (Resume/CV, Cover Letter) keeps only the file field."""
    fields: list[dict] = []

    def add(label: str, required: bool, f: dict) -> None:
        typ = _GH_TYPES.get(f.get("type") or "", "text")
        if typ is None:
            return
        name = str(f.get("name") or "")
        if not name:
            return
        opts = [str(v.get("label")) for v in (f.get("values") or []) if v.get("label") is not None]
        fields.append({"label": label, "name": name, "type": typ, "required": bool(required),
                       "options": opts, "hidden": False,
                       "locator": _css_attr("name" if typ == "multi" else "id", name)})

    groups = list(data.get("questions") or []) + list(data.get("location_questions") or [])
    for c in data.get("compliance") or []:
        groups += list(c.get("questions") or [])
    for q in groups:
        label = str(q.get("label") or "").strip()
        fs = list(q.get("fields") or [])
        if any(f.get("type") == "input_file" for f in fs):
            fs = [f for f in fs if f.get("type") == "input_file"]
        for f in fs:
            add(label, bool(q.get("required")), f)
    demo = data.get("demographic_questions") or {}
    for q in demo.get("questions") or []:
        typ = {"multi_value_single_select": "select", "multi_value_multi_select": "multi"}.get(
            q.get("type") or "", "text")
        opts = [str(o.get("label")) for o in (q.get("answer_options") or []) if o.get("label")]
        name = f"demographic_{q.get('id')}"
        fields.append({"label": str(q.get("label") or "").strip(), "name": name, "type": typ,
                       "required": bool(q.get("required")), "options": opts, "hidden": False,
                       "locator": _css_attr("id", name), "demographic": True})
    return {"source": "greenhouse_api", "url": url, "steps": [fields]}


# --------------------------------------------------------------------------- resolving values

_CONSENT_RX = re.compile(
    r"\b(i (agree|consent|accept|acknowledge|certify|confirm|attest|understand)|consent|privacy (policy|notice)|"
    r"terms (and|&) conditions|terms of (use|service)|attest|certif(y|ication)|acknowledg|gdpr|"
    r"data (processing|retention)|true and (correct|complete)|accurate (and|&) complete)", re.I)
_RESUME_RX = re.compile(r"\b(resume|résumé|cv|curriculum vitae)\b", re.I)
_COVER_RX = re.compile(r"\bcover\s*letter\b", re.I)
_KIND = {"text": "fill", "textarea": "fill", "number": "fill", "date": "fill", "select": "select",
         "radio": "radio", "checkbox": "check", "multi": "check", "file": "upload"}
_ANS_TYPE = {"multi": "checkbox", "file": "text"}


def is_consent_label(label: str, kind: str = "check") -> bool:
    """Consent / attestation wording. Radios and dropdowns also use the guard's
    checkbox rule on their question (it covers "authorize", "I am", "hereby"...)."""
    if _CONSENT_RX.search(label or ""):
        return True
    if kind in ("radio", "select"):
        from .guard import is_consent_label as guard_consent
        return guard_consent(label)
    return False


def _value_str(v: Any) -> Optional[str]:
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) or None
    return str(v)


def resolve_field(f: dict, job: dict, files: dict[str, str], cfg: dict, *, bank: Any = None,
                  facts: Any = None) -> Optional[FieldAction]:
    """One snapshot field -> a FieldAction (value None = left empty + highlighted).
    Returns None for a field that needs no action (a single checkbox answered 'no')."""
    from .. import answers as A
    label, typ = str(f.get("label") or f.get("name") or ""), str(f.get("type") or "text")
    kind, loc = _KIND.get(typ, "fill"), str(f.get("locator") or "")
    req = bool(f.get("required"))
    empty = lambda note, source="none": FieldAction(loc, label, kind, None, source, 0.0, True, note, req)  # noqa: E731

    if typ == "file":
        key = "cover_letter" if _COVER_RX.search(label) or "cover" in str(f.get("name") or "") else (
            "resume" if _RESUME_RX.search(label) or "resume" in str(f.get("name") or "") else None)
        if key and files.get(key):
            return FieldAction(loc, label, "upload", key, f"file:{key}", 1.0, False, "", req)
        if key == "resume":
            return empty("needs approved résumé")
        if key == "cover_letter":
            return empty("no approved cover letter")
        return empty("file upload: attach it yourself")
    if kind in ("check", "radio", "select") and is_consent_label(label, kind):
        return empty("consent / attestation: only you can tick this", "consent")
    if A.match_rule(label) == "open":   # free-text "why us?": yours to write, never auto-typed
        return empty("open question: write this yourself")
    a = A.answer_question(label, field_type=_ANS_TYPE.get(typ, typ), options=f.get("options") or None,
                          job=job, answers=bank, facts=facts, required=f.get("required"))
    value = _value_str(a.value)
    if a.needs_user or value is None or float(a.confidence or 0) < float(cfg["min_confidence"]):
        why = a.note or ("needs your approval" if a.needs_user else "low confidence")
        return FieldAction(loc, label, kind, None, a.source or "none", float(a.confidence or 0), True, why, req)
    if typ == "checkbox" and value == "false":
        return None
    return FieldAction(loc, label, kind, value, a.source, float(a.confidence), False, "", req)


def enforce_policy(plan: FillPlan, cfg: dict) -> FillPlan:
    """The service's safety net over ANY adapter's plan: blank every value that is
    uncertain, unapproved, consent-like, or an upload that isn't an approved file.
    `plan.files` must already be the job's approved files (plan_for_job sets it)."""
    floor = float(cfg["min_confidence"])
    for step in plan.steps:
        for a in step:
            reason = None
            if a.value is None:
                continue
            if a.needs_user or float(a.confidence or 0) < floor:
                reason = "needs your approval"
            elif a.kind in ("check", "radio", "select") and is_consent_label(a.label, a.kind):
                reason = "consent / attestation: only you can tick this"
            elif a.kind == "upload" and (a.value not in ("resume", "cover_letter")
                                         or not (plan.files or {}).get(a.value)):
                reason = "upload is not an approved file"
            if reason:
                a.value, a.needs_user, a.note = None, True, reason
    return plan


def _unresolved(plan: FillPlan, extra: list[dict]) -> list[dict]:
    out = [dict(u) for u in extra]
    seen = {u.get("label") for u in out}
    for i, step in enumerate(plan.steps):
        for a in step:
            if a.value is None and a.label not in seen:
                out.append({"label": a.label, "reason": a.note or "needs you", "step": i,
                            "locator": a.locator})
                seen.add(a.label)
    return out


def build_plan(job: dict, url: str, ats: str, snapshot: dict, files: dict[str, str],
               file_unresolved: list[dict], cfg: dict, *, bank: Any = None) -> FillPlan:
    """Snapshot -> FillPlan with values only from answers.answer_question / approved files."""
    from .. import answers as A
    if bank is None:
        bank = A.as_bank(None)
    steps: list[list[FieldAction]] = []
    for raw_step in snapshot.get("steps") or []:
        step = []
        for f in raw_step:
            if f.get("hidden"):
                continue      # hidden / off-screen inputs (honeypots) are never planned
            act = resolve_field(f, job, files, cfg, bank=bank)
            if act is not None:
                step.append(act)
        steps.append(step)
    plan = FillPlan(job_key=job["key"], ats=ats, url=url, steps=steps, unresolved=[],
                    files=dict(files))
    plan.unresolved = _unresolved(plan, file_unresolved)
    return plan


# --------------------------------------------------------------------------- browser backend

class PlaywrightBackend:
    """Real browser work. Tests swap `service.BACKEND` for a fake; nothing in here
    clicks, presses or submits: page actions go through K's SafePage/executor."""

    # -- planning: headless, anonymous, read-only
    _FIELDS_JS = r"""() => {
      const txt = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
      const labelOf = (el) => {
        if (el.labels && el.labels.length) return txt(el.labels[0]);
        const by = el.getAttribute('aria-labelledby');
        if (by) return by.split(/\s+/).map(id => txt(document.getElementById(id))).join(' ').trim();
        return el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || el.id || '';
      };
      const hidden = (el) => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return el.type === 'hidden' || s.display === 'none' || s.visibility === 'hidden' ||
               el.getAttribute('aria-hidden') === 'true' || el.tabIndex < 0 && el.type !== 'file' ||
               (r.width === 0 && r.height === 0 && el.type !== 'file') || r.right < 0 || r.bottom < 0;
      };
      const out = [], radios = {};
      for (const el of document.querySelectorAll('input, select, textarea')) {
        const t = (el.tagName === 'SELECT') ? 'select' : (el.tagName === 'TEXTAREA') ? 'textarea' :
                  (el.type || 'text').toLowerCase();
        if (['submit', 'button', 'image', 'reset', 'password', 'hidden'].includes(t)) continue;
        const loc = el.id ? `[id="${CSS.escape(el.id)}"]` : el.name ? `[name="${CSS.escape(el.name)}"]` : null;
        if (!loc) continue;
        if (t === 'radio') {
          const g = radios[el.name] || (radios[el.name] = {label: txt(el.closest('fieldset')?.querySelector('legend')) || el.name,
            name: el.name, type: 'radio', required: el.required, options: [], hidden: hidden(el),
            locator: `[name="${CSS.escape(el.name)}"]`});
          g.options.push(labelOf(el));
          if (g.options.length === 1) out.push(g);
          continue;
        }
        out.push({label: labelOf(el), name: el.name || el.id, type: ['email', 'tel', 'url', 'search'].includes(t) ? 'text' : t,
                  required: el.required || el.getAttribute('aria-required') === 'true',
                  options: t === 'select' ? Array.from(el.options).map(o => txt(o)).filter(Boolean) : [],
                  hidden: hidden(el), locator: loc});
      }
      return out;
    }"""

    _NO_SUBMIT_JS = r"""(() => {
      const P = HTMLFormElement.prototype;
      P.submit = function () {}; P.requestSubmit = function () {};
      window.addEventListener('submit', (e) => { e.preventDefault(); e.stopImmediatePropagation(); }, true);
    })();"""

    def snapshot(self, url: str, ats: str, cfg: dict, adapter: Any = None) -> Optional[dict]:
        """Load the public form headless and list its fields. No typing, no clicks;
        scripted submits are stopped in the page and every non-GET request is
        aborted. None = posting gone. With an adapter, its own read-only
        snapshot (through K's guarded SafePage) rides along as 'adapter_snapshot'."""
        from playwright.sync_api import sync_playwright
        timeout = float(cfg["timeouts"]["page_load_sec"]) * 1000
        with sync_playwright() as pw:
            browser = self._launch_headless(pw)
            try:
                ctx = browser.new_context()
                # Read-only: a page that submits itself (scripted submit / requestSubmit /
                # a submit event) is stopped in the page, and any non-GET request that
                # still gets out is aborted below.
                ctx.add_init_script(self._NO_SUBMIT_JS)
                ctx.route("**/*", self._planner_route)
                page = ctx.new_page()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                self._refuse_if_denied(page, url)          # redirects never reach the route
                if resp is not None and resp.status in (404, 410):
                    return None
                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout, 10000))
                except Exception:  # noqa: BLE001 - busy pages never go idle; fields are there
                    pass
                self._refuse_if_denied(page, url)          # a late JS redirect counts too
                notes = []
                det = _detect()
                if det is not None:
                    if _safe_call(det, "captcha_present", page):
                        notes.append("captcha")
                    if _safe_call(det, "login_wall", page):
                        notes.append("login_wall")
                fields = page.evaluate(self._FIELDS_JS)
                adapter_snap = None
                sp_mod = _k_module("safepage")
                from_html = getattr(adapter, "snapshot_from_html", None) if adapter is not None else None
                if callable(from_html):
                    # Lever: card questions live in server-rendered JSON, not in labels
                    try:
                        snap = from_html(page.content(), page.url)
                        if any(snap.get("steps") or [[]]) or snap.get("fields"):
                            adapter_snap = snap
                    except Exception as exc:  # noqa: BLE001
                        log.warning("html snapshot failed ats=%s: %s", ats, type(exc).__name__)
                if adapter_snap is None and adapter is not None and sp_mod is not None \
                        and callable(getattr(adapter, "snapshot", None)):
                    try:
                        safe = sp_mod.SafePage(page, **adapter.safe_page_kwargs())
                        adapter_snap = adapter.snapshot(safe)
                    except Exception as exc:  # noqa: BLE001 - our own field list still stands
                        log.warning("adapter snapshot failed ats=%s: %s", ats, type(exc).__name__)
                if det is not None and hasattr(det, "honeypot"):
                    for f in fields:
                        if _safe_call(det, "honeypot", f):
                            f["hidden"] = True
                out = {"source": "page", "url": page.url, "steps": [fields], "notes": notes}
                if adapter_snap is not None:
                    out["adapter_snapshot"] = adapter_snap
                return out
            finally:
                browser.close()

    @staticmethod
    def _planner_route(route: Any) -> None:
        """Read-only planner: every non-GET and every request to a do-not-automate
        host is aborted (only the first URL of a redirect chain reaches here, so
        the final URL is checked separately)."""
        try:
            req = route.request
            if req.method not in ("GET", "HEAD", "OPTIONS") or (
                    req.url.startswith(("http:", "https:")) and denied_host(req.url)):
                return route.abort("blockedbyclient")
            return route.continue_()
        except Exception:  # noqa: BLE001 - fail closed
            try:
                route.abort("blockedbyclient")
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _refuse_if_denied(page: Any, url: str) -> None:
        final = page.url or ""
        entry = denied_host(final) if final.startswith(("http:", "https:")) else None
        if entry and entry != "invalid-url":
            log.warning("planner: the form redirected to a do-not-automate host (%s)", entry)
            raise AutofillRefused("denylisted", f"the application link leads to {entry}, which is never "
                                  "automated: open it and apply yourself", http=400, link_out=url)

    @staticmethod
    def _launch_headless(pw):
        try:
            return pw.chromium.launch(channel="chrome", headless=True)
        except Exception:  # noqa: BLE001 - no Chrome: Playwright's bundled Chromium
            return pw.chromium.launch(headless=True)

    # -- filling: headed persistent profile, owned by one worker thread
    def open_headed(self, cfg: dict) -> Any:
        """K's persistent-profile Chrome (no stealth flags; K's session refuses them)."""
        from .session import launch
        b = cfg["browser"]
        return {"k": launch(profile_dir=os.path.expanduser(b["profile_dir"]),
                            headed=bool(b["headed"]), channel=b.get("channel") or None)}

    @staticmethod
    def _page(handle: dict) -> Any:
        return getattr(handle.get("k"), "page", None)

    def execute(self, handle: dict, adapter: Any, plan: FillPlan, ctx: Any, cfg: dict) -> Any:
        """Run the plan through K's guarded SafePage + the adapter's executor.
        Uploads are pinned to the plan's (approved) files."""
        if adapter is None:
            return Outcome("error", "no adapter available")
        from .guard import DeniedHost, OffSite
        safe = handle["k"].safe_page(**adapter.safe_page_kwargs())
        safe.allowed_uploads = {str(Path(p).resolve()) for p in (plan.files or {}).values()}
        try:
            safe.goto(plan.url)
        except DeniedHost as exc:
            return Outcome("blocked", f"{exc}: open it yourself (link-out only)")
        except OffSite as exc:
            return Outcome("blocked", str(exc))
        return adapter.run(safe, plan, ctx)

    def handover(self, handle: dict) -> bool:
        k = handle.get("k")
        return bool(k.handover()) if k is not None else False

    def tab_state(self, handle: dict) -> Optional[tuple[str, str]]:
        """(url, visible text) of the fill tab, or None once the user closed it."""
        page = self._page(handle)
        try:
            if page is None or page.is_closed():
                return None
            return page.url, (page.inner_text("body", timeout=2000) or "")[:20000]
        except Exception:  # noqa: BLE001 - mid-navigation: try again next poll
            return ("", "") if page is not None and not page.is_closed() else None

    def screenshot(self, handle: dict, path: Path) -> Optional[str]:
        page = self._page(handle)
        try:
            page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("screenshot failed: %s", type(exc).__name__)
            return None

    def wait(self, handle: dict, seconds: float) -> None:
        page = self._page(handle)
        try:
            page.wait_for_timeout(seconds * 1000)   # keeps Playwright's event loop serviced
        except Exception:  # noqa: BLE001
            time.sleep(seconds)

    def close(self, handle: dict) -> None:
        k = handle.get("k")
        if k is not None:
            k.close()


def _k_module(name: str) -> Any:
    try:
        import importlib
        return importlib.import_module(f"jobcopilot.autofill.{name}")
    except Exception:  # noqa: BLE001
        return None


def _detect() -> Any:
    return _k_module("detect")


def _safe_call(mod: Any, name: str, *args: Any) -> bool:
    fn = getattr(mod, name, None)
    if not callable(fn):
        return False
    try:
        return bool(fn(*args))
    except Exception:  # noqa: BLE001
        return False


BACKEND: Any = PlaywrightBackend()


# --------------------------------------------------------------------------- plan_for_job

def _job_or_raise(job_key: str, db_path: Any) -> dict:
    job = store.get_job(job_key, db_path)
    if not job:
        raise AutofillRefused("not_found", "no such job", http=404)
    return job


def _check_url(job: dict, cfg: dict) -> str:
    url = target_url(job)
    if not url:
        raise AutofillRefused("no_url", "this job has no application link", http=400)
    entry = denied_host(url, cfg)
    if entry == "invalid-url":
        raise AutofillRefused("bad_url", "the application link is not a web page", http=400)
    if entry:
        raise AutofillRefused("denylisted", f"{entry} is never automated: open it and apply yourself",
                              http=400, link_out=url)
    return url


def answer_no_llm(question: str, **kw: Any) -> Any:
    """answers.answer_question, except open questions ("why us?") come back empty
    with needs_user=True without an LLM call: they're the user's to write."""
    from .. import answers as A
    if A.match_rule(question) == "open":
        return A.Answer("", "none", 0.0, True, "open question: write this yourself", "open")
    return A.answer_question(question, **kw)


def plan_context(job: dict, url: str, files: dict[str, str], bank: Any = None,
                 cfg: Optional[dict] = None) -> Any:
    from .adapters.base import PlanContext
    floor = float((cfg or {}).get("min_confidence") or DEFAULTS["min_confidence"])
    return PlanContext(job_key=job["key"], job=job, url=url, files=dict(files), answers=bank,
                       answer_fn=answer_no_llm, min_confidence=floor)


def _plan_from_snapshot(job: dict, url: str, ats: str, adapter: Any, snapshot: dict,
                        files: dict[str, str], file_unresolved: list[dict], cfg: dict, *,
                        bank: Any = None) -> FillPlan:
    """Snapshot (form structure only) -> FillPlan whose values come from the CURRENT
    answers.yaml / facts (bank None = load them now) and the given approved files."""
    plan = None
    if adapter is not None and snapshot.get("adapter_snapshot") is not None:
        try:
            plan = adapter.build_plan(snapshot["adapter_snapshot"], plan_context(job, url, files, bank, cfg))
            plan.job_key, plan.ats, plan.url = job["key"], plan.ats or ats, plan.url or url
        except Exception as exc:  # noqa: BLE001 - fall back to the generic resolver
            log.warning("adapter build_plan failed job=%s ats=%s: %s", job["key"], ats, type(exc).__name__)
            plan = None
    if plan is None:
        plan = build_plan(job, url, ats, snapshot, files, file_unresolved, cfg, bank=bank)
    plan.files = dict(files)
    enforce_policy(plan, cfg)
    plan.unresolved = _unresolved(plan, file_unresolved)
    for note in snapshot.get("notes") or []:
        plan.unresolved.append({"label": note, "reason": "the form page shows a "
                                f"{'CAPTCHA' if note == 'captcha' else 'sign-in wall'}: you'll handle it"})
    return plan


def plan_for_job(job_key: str, *, db_path: Any = None, cfg: Optional[dict] = None,
                 bank: Any = None) -> FillPlan:
    """Build, store and return the job's fill plan. Read-only: the Greenhouse
    questions API where possible, else a headless page load (no typing, no clicks).
    Raises AutofillRefused (not_found | denylisted | gone | no_url)."""
    cfg = cfg or settings()
    job = _job_or_raise(job_key, db_path)
    url = _check_url(job, cfg)
    if job.get("gone_at"):
        raise AutofillRefused("gone", "the posting is no longer listed", http=409)
    ats = detect_ats(url, job)
    adapter = adapter_for(url, ats)
    files, file_unresolved = approved_files(job, db_path)

    snapshot = None
    gh = greenhouse_ids(job, url, db_path) if ats == "greenhouse" else None
    if gh:
        data = fetch_greenhouse_questions(*gh)
        if data is None:
            _gone(job_key, db_path)
        snapshot = snapshot_from_greenhouse(data, url)
    else:
        snapshot = BACKEND.snapshot(url, ats, cfg, adapter)
        if snapshot is None:
            _gone(job_key, db_path)

    plan = _plan_from_snapshot(job, url, ats, adapter, snapshot, files, file_unresolved, cfg, bank=bank)
    _save(plan, cfg, db_path, snapshot=snapshot, target=url)
    store.set_fill_status(job_key, "planned" if not plan.unresolved else "planned_needs_you", db_path)
    n_vals = sum(1 for s in plan.steps for a in s if a.value is not None)
    log.info("fill plan job=%s ats=%s source=%s fields=%d valued=%d unresolved=%d", job_key, ats,
             snapshot.get("source"), sum(len(s) for s in plan.steps), n_vals, len(plan.unresolved))
    return plan


def _gone(job_key: str, db_path: Any) -> None:
    store.set_fill_status(job_key, "gone", db_path)
    raise AutofillRefused("gone", "the posting is gone (404) — nothing to fill", http=409)


def _ts(created_at: Any) -> float:
    """FillPlan.created_at (ISO-8601 string) -> epoch seconds; now if unparseable."""
    if isinstance(created_at, (int, float)):
        return float(created_at)
    try:
        d = _dt.datetime.fromisoformat(str(created_at))
        return (d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)).timestamp()
    except ValueError:
        return time.time()


# Stored plans hold field metadata only (L#4): label, locator, kind, source /
# rule, confidence, approval state, plus the read-only form snapshot they were
# built from. Never a value: those are resolved from answers.yaml / facts /
# the approved files when a fill starts.
PLAN_FORMAT = 2


def _strip_values(d: dict) -> dict:
    """A FillPlan dict without any value: each action keeps `approved` (it had an
    approved value when planned) and uploads keep their file KEY, never a path."""
    d = dict(d or {})
    steps = []
    for step in d.get("steps") or []:
        out = []
        for a in step if isinstance(step, list) else []:
            if not isinstance(a, dict):
                continue
            a = dict(a)
            a.setdefault("approved", a.get("value") is not None and not a.get("needs_user"))
            if not (a.get("kind") == "upload" and a.get("value") in ("resume", "cover_letter")):
                a["value"] = None
            out.append(a)
        steps.append(out)
    d["steps"] = steps
    d["files"] = {}
    return d


def stored_plan_dict(plan: FillPlan, snapshot: Optional[dict] = None, target: str = "") -> dict:
    d = _strip_values(plan.to_dict())
    d["file_kinds"] = sorted(plan.files or {})
    d["format"] = PLAN_FORMAT
    d["target_url"] = target or plan.url
    if snapshot is not None:
        d["snapshot"] = snapshot
    return d


def _save(plan: FillPlan, cfg: dict, db_path: Any, *, snapshot: Optional[dict] = None,
          target: str = "") -> None:
    at = _ts(plan.created_at)
    store.save_fill_plan(plan.job_key, stored_plan_dict(plan, snapshot, target), ats=plan.ats, url=plan.url,
                         unresolved_count=len(plan.unresolved), created_at=at,
                         stale_after=at + float(cfg["plan_stale_hours"]) * 3600, db_path=db_path)


def stored_plan(job_key: str, db_path: Any = None) -> Optional[dict]:
    """The stored plan row for the API / UI: metadata only. Any value a row from
    before L#4 still carries is stripped; the form snapshot is left out."""
    row = store.get_fill_plan(job_key, db_path)
    if row is None:
        return None
    if isinstance(row.get("plan"), dict):
        plan = _strip_values(row["plan"])
        plan.pop("snapshot", None)
        row["plan"] = plan
    return row


def _fresh_plan(job: dict, cfg: dict, db_path: Any) -> Optional[FillPlan]:
    """A plan to hand off without re-fetching the form: rebuilt from the stored
    form snapshot when that is fresh enough (L#8), with every value re-resolved
    NOW from answers.yaml / facts and plan.files = the job's approved files now.
    None = rebuild from the live form (also for rows from before L#4)."""
    row = store.get_fill_plan(job["key"], db_path)
    if not row or not isinstance(row.get("plan"), dict):
        return None
    now = time.time()
    if row.get("stale_after") is not None and now > float(row["stale_after"]):
        return None
    seen = max(float(job.get("last_seen") or 0), float(row.get("created_at") or 0))
    if now - seen > float(cfg["freshness_hours"]) * 3600:
        return None
    d = row["plan"]
    snap = d.get("snapshot")
    if d.get("format") != PLAN_FORMAT or not isinstance(snap, dict):
        return None
    url = target_url(job)
    if not url or d.get("target_url") != url:
        return None                     # the job's link changed since: re-check the live form
    ats = d.get("ats") or detect_ats(url, job)
    files, file_unresolved = approved_files(job, db_path)
    try:
        plan = _plan_from_snapshot(job, d.get("url") or url, ats, adapter_for(url, ats), snap, files,
                                   file_unresolved, cfg)
    except Exception as exc:  # noqa: BLE001 - rebuild from the live form instead
        log.warning("re-resolving the stored plan failed job=%s: %s", job["key"], type(exc).__name__)
        return None
    plan.created_at = d.get("created_at") or plan.created_at
    return plan


# --------------------------------------------------------------------------- live sessions

@dataclass
class LiveSession:
    id: int
    job_key: str
    ats: str
    db_path: Any
    cfg: dict
    handle: Any = None
    thread: Optional[threading.Thread] = None
    commands: "_queue.Queue[str]" = field(default_factory=_queue.Queue)
    start_url: str = ""
    start_text: str = ""


_LIVE: dict[int, LiveSession] = {}
_LIVE_LOCK = threading.Lock()


def _shots_dir(job_key: str, db_path: Any) -> Path:
    from ..tailor_v2 import job_slug
    d = store.applications_dir(db_path) / job_slug(job_key)
    d.mkdir(parents=True, exist_ok=True)
    return d


def launch_fill(job_key: str, *, db_path: Any = None, cfg: Optional[dict] = None,
                background: bool = True) -> int:
    """Start a headed fill for the job; returns the session id at once (the work runs
    in a background thread that outlives the request). Refuses (AutofillRefused):
    demo mode 403, unknown job 404, denylisted host 400 + link_out, an open session
    409, an already-submitted job 409, a gone posting 409."""
    from .. import demo
    if demo.enabled():
        raise AutofillRefused("demo", "autofill is off in the public demo", http=403)
    cfg = cfg or settings()
    job = _job_or_raise(job_key, db_path)
    _check_url(job, cfg)
    if store.is_submitted_or_later(job.get("status")):
        raise AutofillRefused("submitted", "already submitted", http=409)
    if job.get("gone_at"):
        raise AutofillRefused("gone", "the posting is no longer listed", http=409)
    with _LIVE_LOCK:
        sid, open_row = store.claim_autofill_session(job_key, db_path)
        if sid is None:
            raise AutofillRefused("busy", "another fill window is open: finish or close it first",
                                  http=409, session_id=open_row["id"] if open_row else None)
        live = LiveSession(sid, job_key, detect_ats(target_url(job), job), db_path, cfg)
        _LIVE[sid] = live
    store.set_fill_status(job_key, "filling", db_path)
    if background:
        live.thread = threading.Thread(target=_worker, args=(live,), name=f"autofill-{sid}", daemon=True)
        live.thread.start()
    else:
        _worker(live)
    return sid


_SESSION_FOR_OUTCOME = {"ready": "ready_to_submit", "needs_you": "needs_you", "blocked": "blocked"}


def _worker(live: LiveSession) -> None:
    db, cfg, key = live.db_path, live.cfg, live.job_key
    final = "error"
    try:
        store.update_autofill_session(live.id, db, status="filling")
        job = _job_or_raise(key, db)
        plan = _fresh_plan(job, cfg, db)                     # values re-resolved from answers NOW
        if plan is None:
            plan = plan_for_job(key, db_path=db, cfg=cfg)   # also the live re-check (≤ 6 h)
        plan.files, _ = approved_files(store.get_job(key, db) or job, db)   # the approved files NOW
        enforce_policy(plan, cfg)                            # uploads not approved now are blanked
        live.ats = plan.ats or live.ats
        if "resume" not in (plan.files or {}):
            log.info("fill job=%s: no approved résumé; the résumé field stays empty", key)
        live.handle = BACKEND.open_headed(cfg)
        shots = _shots_dir(key, db)
        outcome = BACKEND.execute(live.handle, adapter_for(plan.url, plan.ats), plan,
                                  plan_context(job, plan.url, plan.files, cfg=cfg), cfg)
        od = _outcome_dict(outcome)
        status = _SESSION_FOR_OUTCOME.get(od.get("status") or "", "error")
        shot = od.get("screenshot") or BACKEND.screenshot(
            live.handle, shots / f"autofill-{live.id}-{int(time.time())}.png")
        store.update_autofill_session(live.id, db, status=status, outcome=od, screenshot_path=shot)
        log.info("fill job=%s session=%d outcome=%s filled=%s left_empty=%s", key, live.id,
                 od.get("status"), od.get("filled"), od.get("left_empty"))
        if status == "ready_to_submit":
            cur = store.get_job(key, db) or {}
            if store.canonical_status(cur.get("status")) not in ("ready_to_submit",):
                store.set_status(key, "ready_to_submit", source="system",
                                 note="form filled; waiting for your Submit", db_path=db)
            store.set_fill_status(key, "ready", db)
        else:
            store.set_fill_status(key, status, db)
        if status == "error":
            return
        state = BACKEND.tab_state(live.handle) or ("", "")
        live.start_url, live.start_text = state
        final = watch_for_confirmation(live)
    except AutofillRefused as exc:
        store.update_autofill_session(live.id, db, outcome={"status": "error", "reason": exc.detail,
                                                            "code": exc.code})
        log.info("fill job=%s session=%d refused: %s", key, live.id, exc.code)
    except Exception as exc:  # noqa: BLE001
        store.update_autofill_session(live.id, db, outcome={"status": "error",
                                                            "reason": type(exc).__name__})
        log.error("fill job=%s session=%d failed: %s", key, live.id, type(exc).__name__)
    finally:
        row = store.get_autofill_session(live.id, db) or {}
        if row.get("status") in ("starting", "filling"):      # never got to an outcome
            store.update_autofill_session(live.id, db, status="error")
            store.set_fill_status(key, "error", db)
        elif row.get("status") in store.AUTOFILL_OPEN_STATUSES:   # watch ended without a verdict
            store.update_autofill_session(live.id, db, status=final if final != "error" else "closed")
        store.update_autofill_session(live.id, db, finished_at=time.time())
        if live.handle is not None:
            BACKEND.close(live.handle)
        with _LIVE_LOCK:
            _LIVE.pop(live.id, None)


# --------------------------------------------------------------------------- confirmation

_CONFIRM_URL = re.compile(r"/(confirmation|thanks|thank[-_]you|application[-_]submitted|submitted|success)\b"
                          r"|jobTasks/completed", re.I)
_CONFIRM_TEXT = re.compile(r"thank(s| you) for (applying|your application)|application (has been |was )?"
                           r"(submitted|received)|we('ve| have) received your application|"
                           r"your application (has been|was) (submitted|received)", re.I)


def is_confirmation(url: str, text: str, ats: str) -> bool:
    det = _detect()
    fn = getattr(det, "confirmation_page", None) if det else None
    if callable(fn):
        try:
            return bool(fn(url, text, ats))
        except Exception:  # noqa: BLE001
            pass
    return bool(_CONFIRM_URL.search(url or "") or _CONFIRM_TEXT.search(text or ""))


def watch_for_confirmation(session: LiveSession) -> str:
    """Passive: poll the fill tab's URL and text until the USER's own submission
    shows the ATS confirmation page (-> job 'submitted', source 'detected', proof
    screenshot), the user closes the window ('closed') or the watch times out.
    Never clicks or types; a marker already on the form page doesn't count.
    Also services hand-over requests (they must run on this thread)."""
    db, cfg = session.db_path, session.cfg
    deadline = time.time() + float(cfg["timeouts"]["watch_min"]) * 60
    poll = max(0.05, float(cfg["timeouts"]["watch_poll_sec"]))
    # A marker already on the form page (URL or boilerplate text) doesn't count;
    # only a signal that APPEARS after the fill does.
    base_url = is_confirmation(session.start_url, "", session.ats)
    base_text = is_confirmation("", session.start_text, session.ats)
    while time.time() < deadline:
        while True:
            try:
                cmd = session.commands.get_nowait()
            except _queue.Empty:
                break
            if cmd == "handover":
                ok = BACKEND.handover(session.handle)
                log.info("session=%d handover %s", session.id, "done" if ok else "unavailable")
        state = BACKEND.tab_state(session.handle)
        if state is None:
            store.update_autofill_session(session.id, db, status="closed")
            return "closed"
        url, text = state
        if url and ((not base_url and is_confirmation(url, "", session.ats))
                    or (not base_text and is_confirmation("", text, session.ats))):
            shot = BACKEND.screenshot(session.handle, _shots_dir(session.job_key, db)
                                      / f"confirmation-{session.id}-{int(time.time())}.png")
            store.set_status(session.job_key, "submitted", source="detected",
                             note="ATS confirmation page detected after your Submit",
                             proof_path=shot, db_path=db)
            store.set_fill_status(session.job_key, "submitted", db)
            store.update_autofill_session(session.id, db, status="submitted",
                                          **({"screenshot_path": shot} if shot else {}))
            log.info("session=%d job=%s submission detected", session.id, session.job_key)
            return "submitted"
        BACKEND.wait(session.handle, poll)
    store.update_autofill_session(session.id, db, status="timed_out")
    return "timed_out"


# --------------------------------------------------------------------------- queries / commands

def session_status(session_id: int, *, db_path: Any = None) -> Optional[dict]:
    row = store.get_autofill_session(int(session_id), db_path)
    if row is not None:
        row["live"] = int(session_id) in _LIVE
    return row


def handover(session_id: int) -> bool:
    """Ask the worker to lift the DOM-level submit block so YOUR click submits.
    False when that session has no open window in this process."""
    live = _LIVE.get(int(session_id))
    if live is None:
        return False
    live.commands.put("handover")
    return True


def needs_you(*, db_path: Any = None) -> list[dict]:
    """Open sessions waiting for the user (CAPTCHA, sign-in wall, missing data)."""
    return store.list_autofill_sessions(status=("needs_you", "blocked"), db_path=db_path)


# --------------------------------------------------------------------------- nightly hook

def plan_today(day: Optional[str] = None, *, limit: Optional[int] = None, db_path: Any = None,
               cfg: Optional[dict] = None) -> dict:
    """Build fill plans for today's main-queue jobs that have an approved résumé.
    One job failing never stops the others. Returns counts + per-job codes."""
    from .. import queue as _q
    cfg = cfg or settings()
    limit = int(cfg["nightly_limit"] if limit is None else limit)
    q = _q.list_queue(day=day, lane="main", db_path=db_path)
    results: dict[str, str] = {}
    for item in q.get("items", []):
        if len(results) >= max(0, limit):
            break
        key = item["key"]
        job = store.get_job(key, db_path) or {}
        if store.is_submitted_or_later(job.get("status")):
            continue
        files, _ = approved_files(job, db_path) if job else ({}, [])
        if "resume" not in files:
            continue
        try:
            plan = plan_for_job(key, db_path=db_path, cfg=cfg)
            results[key] = "planned" if not plan.unresolved else "needs_you"
        except AutofillRefused as exc:
            results[key] = exc.code
        except Exception as exc:  # noqa: BLE001
            log.error("fill plan job=%s failed: %s", key, type(exc).__name__)
            results[key] = "error"
    by: dict[str, int] = {}
    for v in results.values():
        by[v] = by.get(v, 0) + 1
    return {"day": q.get("day"), "planned": len(results), "by_status": by}
