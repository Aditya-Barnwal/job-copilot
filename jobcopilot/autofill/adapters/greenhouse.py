"""Greenhouse adapter, plus SpecAdapter: the shared declarative engine every
per-ATS adapter (greenhouse, lever, ashby, workday) is built on (L#3).

An adapter is a YAML spec (specs/<ats>.yaml: hosts, field selectors, uploads,
honeypots, never-touch selectors, submit selectors, final-submit network
patterns, confirmation markers, steps) plus a few lines of Python.

Values come ONLY from answers.answer_question (needs_user False, confidence at
or above the floor) or the job's approved résumé / cover-letter files. Open
questions, consent boxes, honeypots, unapproved or uncertain answers are left
EMPTY and HIGHLIGHTED. The run drives the browser only through SafePage, stops
before Submit, highlights the single Submit control and never clicks it: there
is no code path here that submits. Logs carry labels and sources, never values.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from .. import detect
from ..guard import SafePageError, SubmitGuardViolation, Halted, is_consent_label
from ..plan import FieldAction, FillPlan, Outcome
from .base import Adapter, register

log = logging.getLogger("jobcopilot.autofill.adapters")

SPEC_DIR = Path(__file__).resolve().parent / "specs"
DEFAULT_MIN_CONFIDENCE = 0.8
_SPEC_KEYS = {"ats", "hosts", "url_patterns", "form", "fields", "custom_questions", "comboboxes",
              "uploads", "never", "honeypots", "invisible_captcha", "submit", "errors",
              "submit_patterns", "allow_post", "confirmation", "steps", "login_wall"}
_TEXTLIKE = {"text", "textarea", "email", "tel", "url", "number", "date", "search", ""}
_ANS_TYPE = {"textarea": "textarea", "number": "number", "date": "date", "select": "select",
             "combobox": "select", "radio": "radio", "checkbox": "checkbox", "multi": "checkbox"}
_KIND = {"select": "select", "combobox": "select", "radio": "radio", "checkbox": "check",
         "multi": "check", "file": "upload"}
_MARKS_RE = re.compile(r"[*✱]|\((required|optional)\)", re.I)
_RESUME_RE = re.compile(r"\b(resume|résumé|cv|curriculum vitae)\b", re.I)
_COVER_RE = re.compile(r"\bcover[\s_-]*letter\b", re.I)
_ATTR_RE = re.compile(r'\[([\w-]+)\s*(\^|\$|\*)?=\s*"((?:[^"\\]|\\.)*)"\]|#([\w-]+)')


# --------------------------------------------------------------------------- spec

def load_spec(name: str) -> dict:
    """Read and sanity-check specs/<name>.yaml (unknown keys are an error)."""
    p = SPEC_DIR / f"{name}.yaml"
    spec = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(spec, dict) or spec.get("ats") != name:
        raise ValueError(f"{p.name}: 'ats' must be {name!r}")
    unknown = set(spec) - _SPEC_KEYS
    if unknown:
        raise ValueError(f"{p.name}: unknown keys {sorted(unknown)}")
    for pat in spec.get("submit_patterns") or []:
        re.compile(pat["url_regex"])
    return spec


def clean_label(s: Any) -> str:
    return re.sub(r"\s+", " ", _MARKS_RE.sub(" ", str(s or ""))).strip()


def _attrs(selector: str) -> list[tuple[str, str, str]]:
    """Simple attribute tests in a CSS selector: [a="v"], [a^="v"], #id."""
    out = []
    for m in _ATTR_RE.finditer(selector or ""):
        if m.group(4):
            out.append(("id", "", m.group(4)))
        else:
            out.append((m.group(1), m.group(2) or "", m.group(3).replace('\\"', '"')))
    return out


def _attr_ok(have: str, op: str, want: str) -> bool:
    if not have:
        return False
    return {"": have == want, "^": have.startswith(want), "$": have.endswith(want),
            "*": want in have}[op]


def _css_str(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _name_from_locator(loc: str) -> Optional[str]:
    for attr, op, val in _attrs(loc):
        if attr == "name" and op == "":
            return val
    return None


def _have(f: dict) -> dict:
    """Attributes known for a snapshot field: its own fields plus the plain
    [attr="v"] / #id tests in its locator (data-qa, data-automation-id, ...)."""
    have = {"id": f.get("id") or "", "name": f.get("name") or "",
            "data-automation-id": f.get("automation_id") or ""}
    for a, op, v in _attrs(f.get("locator") or ""):
        if op == "" and not have.get(a):
            have[a] = v
    return have


def _ctx(ctx: Any, key: str, default: Any = None) -> Any:
    if ctx is None:
        return default
    if isinstance(ctx, dict):
        return ctx.get(key, default)
    return getattr(ctx, key, default)


def _answer(ctx: Any, question: str, *, field_type: str, options: Optional[list],
            required: Optional[bool]) -> Any:
    """answers.answer_question through whatever the caller passed (PlanContext,
    the service's ctx dict, or nothing = profile/answers.yaml)."""
    kw = {"field_type": field_type, "options": options or None, "required": required}
    fn = getattr(ctx, "answer", None) if ctx is not None and not isinstance(ctx, dict) else None
    if callable(fn):
        return fn(question, **kw)
    fn = _ctx(ctx, "answer")
    if callable(fn):
        return fn(question, **kw)
    from ... import answers as A
    facts = _ctx(ctx, "facts", [])
    return A.answer_question(question, job=_ctx(ctx, "job"), answers=_ctx(ctx, "answers"),
                             facts=[] if facts is None else facts, **kw)


def _value_str(v: Any, kind: str) -> Optional[str]:
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (list, tuple)):
        items = [str(x) for x in v if str(x).strip()]
        if not items:
            return None
        return json.dumps(items) if kind == "check" else ", ".join(items)
    return str(v)


def _options(f: dict) -> list[str]:
    out = []
    for o in f.get("options") or []:
        t = o.get("label") if isinstance(o, dict) else o
        t = str(t or "").strip()
        if t and not re.fullmatch(r"(select|please select|select one|choose)\W*\.*", t, re.I):
            out.append(t)
    return out


# --------------------------------------------------------------------------- snapshot

def normalize_snapshot(snapshot: Any) -> list[list[dict]]:
    """Q's {"steps": [[field]]}, K's {"fields": [...]}, or a bare field list
    (SafePage.fields() metas) -> steps of normalised fields; radios and
    checkbox groups that arrive one element per option are grouped by name."""
    if snapshot is None:
        return []
    if isinstance(snapshot, dict):
        raw_steps = snapshot.get("steps")
        if raw_steps is None:
            raw_steps = [snapshot.get("fields") or []]
    else:
        raw_steps = [list(snapshot)]
    return [_group(step) for step in raw_steps]


def _group(raw: Iterable[dict]) -> list[dict]:
    out: list[dict] = []
    groups: dict[str, dict] = {}
    for f in raw or []:
        f = dict(f)
        typ = str(f.get("type") or "text").lower()
        name = str(f.get("name") or "")
        loc = str(f.get("locator") or f.get("selector") or "")
        label = clean_label(f.get("label") or "")
        meta_style = "selector" in f and "locator" not in f
        if typ in ("radio", "checkbox") and meta_style and name:
            multi = typ == "radio" or name.endswith("[]") or bool(f.get("group_label"))
            if multi:
                g = groups.get(name)
                if g is None:
                    g = groups[name] = {
                        "label": clean_label(f.get("group_label") or "") or name, "name": name,
                        "id": "", "type": "radio" if typ == "radio" else "multi",
                        "required": bool(f.get("required")), "options": [], "hidden": False,
                        "locator": f"[name={_css_str(name)}]", "role": "",
                        "automation_id": "", "has_value": False, "meta": f}
                    out.append(g)
                g["options"].append(label or str(f.get("value") or ""))
                g["required"] = g["required"] or bool(f.get("required"))
                g["has_value"] = g["has_value"] or bool(f.get("has_value"))
                continue
        out.append({"label": label or clean_label(f.get("group_label") or "") or name,
                    "name": name, "id": str(f.get("id") or ""), "type": typ,
                    "required": bool(f.get("required")), "options": _options(f),
                    "hidden": bool(f.get("hidden")), "locator": loc, "role": str(f.get("role") or ""),
                    "automation_id": str(f.get("automation_id") or ""),
                    "has_value": bool(f.get("has_value")), "meta": f})
    return out


def _is_honeypot(f: dict) -> bool:
    m = dict(f.get("meta") or {})
    m.setdefault("name", f.get("name"))
    m.setdefault("id", f.get("id"))
    m.setdefault("automation_id", f.get("automation_id"))
    typ = f.get("type") or "text"
    m.setdefault("tag", "textarea" if typ == "textarea" else ("select" if typ == "select" else "input"))
    m.setdefault("type", "" if typ in ("textarea", "select") else typ)
    if detect.honeypot(m):
        return True
    return bool(f.get("hidden")) and typ in _TEXTLIKE


# --------------------------------------------------------------------------- run state

class _Run:
    def __init__(self) -> None:
        self.filled = 0
        self.left_empty = 0
        self.highlighted: list[str] = []
        self.missing: list[str] = []          # required, no approved value
        self.consent: list[str] = []          # yours to tick
        self.problems: list[str] = []
        self.seen: set[str] = set()           # normalised labels / ids / names already handled
        self.halted: Optional[str] = None

    def mark(self, *keys: str) -> None:
        for k in keys:
            if k:
                self.seen.add(clean_label(k).lower())

    def handled(self, *keys: str) -> bool:
        return any(k and clean_label(k).lower() in self.seen for k in keys)


# --------------------------------------------------------------------------- engine

class SpecAdapter(Adapter):
    """Declarative adapter: behaviour comes from specs/<spec_name>.yaml."""
    spec_name: str = ""
    spec: dict = {}
    multi_step: bool = False

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        if not cls.spec_name:
            return
        spec = load_spec(cls.spec_name)
        cls.spec = spec
        cls.name = spec["ats"]
        cls.hosts = tuple(spec.get("hosts") or ())
        cls.priority = 10
        cls.submit_patterns = tuple(p["url_regex"] for p in spec.get("submit_patterns") or ())
        cls.allow_post_patterns = tuple(spec.get("allow_post") or ())
        conf = spec.get("confirmation") or {}
        cls.confirmation_markers = {
            "url": tuple(conf.get("url_regex") or ()),
            "text": tuple(re.escape(t.lower()) for t in conf.get("text") or ())}
        cls.honeypot_selectors = tuple(spec.get("honeypots") or ())
        steps = spec.get("steps") or {}
        cls.next_selectors = (steps["next"],) if steps.get("next") else ()
        cls.multi_step = bool(steps)

    def __init__(self, ctx: Any = None) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ declarative bits
    @property
    def submit_selectors(self) -> list[str]:
        return list(self.spec.get("submit") or [])

    @property
    def step_order(self) -> list[str]:
        return list((self.spec.get("steps") or {}).get("order") or [])

    def confirmed(self, url: str, text: str) -> bool:
        """Is this the ATS's 'application received' page? (passive check)"""
        return detect.confirmation_page(url, text, self.name, markers=self.confirmation_markers)

    def _spec_fields(self, step: Optional[str] = None) -> list[dict]:
        fs = self.spec.get("fields") or []
        if isinstance(fs, dict):
            return list(fs.get(step) or []) if step else [e for v in fs.values() for e in (v or [])]
        return list(fs)

    def _spec_entry(self, f: dict, step: Optional[str] = None) -> Optional[dict]:
        have = _have(f)
        for e in self._spec_fields(step):
            if f.get("name") and f.get("name") in (e.get("names") or []):
                return e
            tests = _attrs(e.get("locator") or "")
            if tests and all(a in have and _attr_ok(have[a], op, v) for a, op, v in tests):
                return e
        return None

    def _matches_any(self, f: dict, selectors: Iterable[str]) -> bool:
        have = _have(f)
        for sel in selectors:
            tests = _attrs(sel)
            if tests and all(a in have and _attr_ok(have[a], op, v) for a, op, v in tests):
                return True
        return False

    def _upload_key(self, f: dict) -> Optional[str]:
        for key, u in (self.spec.get("uploads") or {}).items():
            if self._matches_any(f, [u.get("locator", "")]):
                return key
        text = f"{f.get('label')} {f.get('name')} {f.get('id')}"
        if _COVER_RE.search(text):
            return "cover_letter"
        if _RESUME_RE.search(text):
            return "resume"
        return None

    # ------------------------------------------------------------------ planning
    def resolve_field(self, f: dict, ctx: Any, *, step: Optional[str] = None) -> Optional[FieldAction]:
        """One normalised field -> FieldAction (value None = left empty + highlighted),
        or None when the field must not appear in the plan at all (honeypots, a
        checkbox correctly left unticked)."""
        from ... import answers as A
        if self._matches_any(f, self.honeypot_selectors) or _is_honeypot(f):
            return None
        typ = f.get("type") or "text"
        entry = self._spec_entry(f, step)
        if entry and entry.get("kind") == "combobox" or f.get("role") == "combobox":
            typ = "combobox" if typ in _TEXTLIKE else typ
        label = f.get("label") or (entry or {}).get("question") or f.get("name") or ""
        loc = f.get("locator") or (entry or {}).get("locator") or ""
        kind = _KIND.get(typ, "fill")
        req = bool(f.get("required"))

        def empty(note: str, source: str = "none", conf: float = 0.0) -> FieldAction:
            return FieldAction(loc, label, kind, None, source, conf, True, note, req)

        if typ == "file":
            key = self._upload_key(f)
            spec_up = (self.spec.get("uploads") or {}).get(key or "") or {}
            if spec_up.get("label") and (not f.get("label") or clean_label(f.get("label")).lower()
                                         in ("attach", "upload", "upload file", "select files")):
                label = spec_up["label"]
            files = _ctx(ctx, "files", {}) or {}
            if key and files.get(key):
                # value is the KEY ('resume' / 'cover_letter'); the run resolves it to plan.files[key]
                return FieldAction(loc, label, "upload", key, f"approved:{key}", 1.0, False, "", req)
            if key == "resume":
                return empty("needs the approved résumé PDF")
            if key == "cover_letter":
                return empty("no approved cover letter")
            return empty("file upload: attach it yourself")
        if self._matches_any(f, self.spec.get("never") or []):
            return empty("left for you (never filled automatically)", "never")
        if (entry or {}).get("skip"):
            return empty("not in answers.yaml: yours to choose")
        opts = list(f.get("options") or (entry or {}).get("options") or [])
        if kind in ("check", "radio"):
            texts = [label] + (opts if kind == "check" and len(opts) <= 1 else [])
            radio_consent = kind == "radio" and any(is_consent_label(o, kind="radio") for o in opts)
            if radio_consent or any(is_consent_label(t, kind="checkbox") for t in texts if t):
                return empty("consent / attestation: only you can choose this", "consent")
        question = label
        if entry and entry.get("question") and A.match_rule(label) is None:
            question = str(entry["question"])
        company = (_ctx(ctx, "job") or {}).get("company") or "this company"
        question = question.replace("{company}", str(company))
        rule = A.match_rule(question)
        if rule == "consent":
            return empty("consent / attestation: only you can choose this", "consent")
        if rule == "open":          # free text ("why us?"): yours to write, never auto-typed
            return empty("open question: write this yourself")
        ft = _ANS_TYPE.get(typ, "text")
        if typ == "checkbox" and opts and len(opts) > 1:
            ft = "checkbox"
        a = _answer(ctx, question, field_type=ft, options=opts if typ != "combobox" else None,
                    required=req)
        floor = float(_ctx(ctx, "min_confidence", DEFAULT_MIN_CONFIDENCE) or DEFAULT_MIN_CONFIDENCE)
        value = _value_str(a.value, kind)
        conf = float(a.confidence or 0.0)
        if a.needs_user or value is None or conf < floor:
            note = a.note or ("needs your approval" if a.needs_user else "low confidence")
            return empty(note, a.source or "none", conf)
        if typ == "checkbox" and not opts and value == "false":
            return None
        return FieldAction(loc, label, kind, value, a.source, conf, False, "", req)

    def build_plan(self, form_snapshot: Any, ctx: Any) -> FillPlan:
        """Read-only snapshot (+ the spec) -> FillPlan. Never touches a page."""
        steps_in = normalize_snapshot(form_snapshot)
        names = self.step_order
        if self.multi_step and names:
            steps: list[list[FieldAction]] = [[] for _ in names]
            covered: set[int] = set()
            src = (form_snapshot or {}).get("step") if isinstance(form_snapshot, dict) else None
            first = names.index(src) if src in names else 0
            for i, raw in enumerate(steps_in):
                k = min(first + i, len(names) - 1)
                raw = [f for f in raw if not f.get("hidden") or f.get("type") == "file"]  # other steps' fields
                for f in raw:
                    e = self._spec_entry(f, names[k])
                    if e is not None:
                        covered.add(id(e))
                    act = self.resolve_field(f, ctx, step=names[k])
                    if act:
                        steps[k].append(act)
            for k, step_name in enumerate(names):           # standard fields the snapshot couldn't see
                have = {a.locator for a in steps[k]}
                for e in self._spec_fields(step_name):
                    if e["locator"] in have or id(e) in covered:
                        continue
                    f = {"label": "", "name": "", "id": "", "type": _SPEC_TYPE.get(e.get("kind"), "text"),
                         "required": False, "options": e.get("options") or [], "hidden": False,
                         "locator": e["locator"], "role": "", "automation_id": "", "meta": {}}
                    for a, op, v in _attrs(e["locator"]):
                        if a == "data-automation-id" and op == "":
                            f["automation_id"] = v
                        if a == "name" and op == "":
                            f["name"] = v
                    act = self.resolve_field(f, ctx, step=step_name)
                    if act:
                        act.label = act.label or e.get("question") or e["locator"]
                        steps[k].append(act)
                for key, u in (self.spec.get("uploads") or {}).items():
                    if u.get("step") == step_name and not any(a.kind == "upload" for a in steps[k]):
                        act = self.resolve_field({"label": u.get("label", key), "name": key, "id": "",
                                                  "type": "file", "locator": u["locator"], "options": [],
                                                  "required": key == "resume", "meta": {}}, ctx)
                        if act:
                            steps[k].append(act)
        else:
            steps = [[a for a in (self.resolve_field(f, ctx) for f in raw) if a] for raw in steps_in] or [[]]
        files = {k: str(v) for k, v in (_ctx(ctx, "files", {}) or {}).items() if v}
        snap_url = form_snapshot.get("url", "") if isinstance(form_snapshot, dict) else ""
        plan = FillPlan(job_key=str(_ctx(ctx, "job_key") or (_ctx(ctx, "job") or {}).get("key") or ""),
                        ats=self.name, url=str(_ctx(ctx, "url") or snap_url or ""),
                        steps=steps, unresolved=[], files=files)
        for i, step in enumerate(plan.steps):
            for a in step:
                if a.value is None:
                    plan.unresolved.append({"label": a.label, "reason": a.note or "needs you",
                                            "locator": a.locator, "required": a.required, "step": i})
        log.info("plan ats=%s steps=%d actions=%d valued=%d unresolved=%d", self.name, len(plan.steps),
                 len(plan.actions()), sum(1 for a in plan.actions() if a.value is not None),
                 len(plan.unresolved))
        return plan

    # ------------------------------------------------------------------ running
    def run(self, safe: Any, plan: FillPlan, ctx: Any = None) -> Outcome:
        """Execute the plan through SafePage and stop before Submit. Live
        fields the plan didn't know are filled only when `ctx` is given
        (same answers rules), otherwise highlighted."""
        ctx = ctx if ctx is not None else self.ctx
        st = _Run()
        try:
            safe.allowed_uploads = {str(Path(p).expanduser().resolve()) for p in (plan.files or {}).values()}
        except Exception:  # noqa: BLE001 — a SafePage without the attribute: upload() still checks the file
            pass
        block = self._blocker(safe)
        if block:
            safe.halt(block)
            return Outcome("needs_you", block, step_reached=self._step_name(safe) or "start")
        if self.multi_step:
            return self._run_steps(safe, plan, ctx, st)
        self._fill(safe, plan.steps[0] if plan.steps else [], plan, st)
        if not st.halted:
            self._live(safe, st, ctx)
        return self._finish(safe, plan, st, "application form")

    # -- checks
    def _blocker(self, safe: Any) -> Optional[str]:
        for sel in self.spec.get("login_wall") or []:
            if self._count(safe, sel):
                return "sign-in / create-account wall: sign in yourself, then run again"
        if safe.login_wall():
            return "sign-in wall: sign in yourself, then run again"
        cap = safe.captcha()
        if cap and not self._invisible_captcha_only(safe, cap):
            return f"CAPTCHA / bot check ({cap}): your turn"
        return None

    def _invisible_captcha_only(self, safe: Any, reason: str) -> bool:
        """A spec-declared invisible captcha container (runs on the user's own
        Submit) with no challenge frame is not a reason to pause."""
        inv = self.spec.get("invisible_captcha") or []
        if not inv or not str(reason).startswith("challenge widget"):
            return False
        sel = ", ".join(inv)
        widgets = (".g-recaptcha, .h-captcha, .cf-turnstile, #challenge-form, #challenge-stage, "
                   "#FunCaptcha, [data-arkose]")
        try:
            return (self._count(safe, widgets) <= self._count(safe, sel)
                    and self._count(safe, ":is(%s) iframe" % sel) == 0
                    and self._count(safe, "iframe[src*='captcha'], iframe[src*='challenges.cloudflare.com']") == 0)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _count(safe: Any, sel: str) -> int:
        try:
            return int(safe.count(sel))
        except Exception:  # noqa: BLE001 — an unparsable selector matches nothing
            return 0

    def _never(self, safe: Any, loc: str) -> bool:
        for sel in list(self.spec.get("never") or []) + list(self.honeypot_selectors):
            if self._count(safe, f"{loc}:is({sel})"):
                return True
        return False

    # -- writing
    def _fill(self, safe: Any, actions: list[FieldAction], plan: FillPlan, st: _Run) -> None:
        floor = DEFAULT_MIN_CONFIDENCE if self.ctx is None else float(
            _ctx(self.ctx, "min_confidence", DEFAULT_MIN_CONFIDENCE) or DEFAULT_MIN_CONFIDENCE)
        for a in actions:
            if st.halted:
                break
            if self._count(safe, a.locator) == 0:
                log.info("not on this page: %r", a.label)       # e.g. a spec field this tenant doesn't use
                continue
            st.mark(a.label, *[v for k, _, v in _attrs(a.locator) if k in ("id", "name")])
            if (a.value is None or a.needs_user or (a.kind != "upload" and a.confidence < floor)):
                self._empty(safe, a, st)
                continue
            if self._never(safe, a.locator):
                self._empty(safe, a, st, note="left for you")
                continue
            try:
                self._apply(safe, a, plan)
                st.filled += 1
            except Halted as exc:
                st.halted = str(exc)
            except SubmitGuardViolation:
                log.warning("guard refused a control for %r (left empty)", a.label)
                st.problems.append(f"{a.label}: the control looked submit-like, left for you")
                self._empty(safe, a, st)
            except SafePageError as exc:
                log.info("left %r empty: %s", a.label, type(exc).__name__)
                self._empty(safe, a, st)
            except Exception as exc:  # noqa: BLE001 — Playwright timeouts etc.: never fatal, never guessed
                log.info("left %r empty: %s", a.label, type(exc).__name__)
                self._empty(safe, a, st)

    def _apply(self, safe: Any, a: FieldAction, plan: FillPlan) -> None:
        from ... import answers as A
        kw = {"label": a.label, "source": a.source}
        if a.kind == "upload":
            files = plan.files or {}
            path = files.get(a.value) if a.value in ("resume", "cover_letter") else None
            if not path:
                raise SafePageError("upload value must be 'resume' / 'cover_letter' with a plan file")
            safe.upload(a.locator, path, **kw)
            return
        if a.kind in ("radio", "check") and (a.kind == "radio" or a.value != "true"):
            values = [a.value] if a.kind == "radio" else _list_value(a.value)
            for v in values:
                safe.check(self._option_locator(safe, a.locator, v), **kw)
            return
        if a.kind == "check":
            safe.check(a.locator, **kw)
            return
        m = safe.meta(a.locator)
        combobox = m.get("role") == "combobox" or any(
            self._count(safe, f"{a.locator}:is({c})") for c in self.spec.get("comboboxes") or [])
        if a.kind == "select" or combobox:
            if m.get("tag") == "select":
                labels = [o["label"] for o in m.get("options") or [] if o.get("label") and o.get("value") != ""]
                pick = a.value if a.value in labels else None
                if pick is None:
                    pick, score = A.match_option(a.value, labels)
                    if not pick or score < 0.8:
                        raise SafePageError("no option matches the approved value")
                safe.select(a.locator, pick, **kw)
            else:
                safe.select(a.locator, a.value, **kw)
            return
        safe.fill(a.locator, a.value, **kw)

    def _option_locator(self, safe: Any, group_loc: str, value: str) -> str:
        """The single radio / checkbox in a group whose label (or value) is `value`."""
        if self._count(safe, group_loc) == 1:
            return group_loc
        name = _name_from_locator(group_loc)
        hits = [f for f in safe.fields()
                if f.get("type") in ("radio", "checkbox") and (name is None or f.get("name") == name)
                and value in (clean_label(f.get("label")), f.get("value"))]
        if len(hits) != 1:
            raise SafePageError(f"option matched {len(hits)} controls")
        return hits[0]["selector"]

    def _empty(self, safe: Any, a: FieldAction, st: _Run, note: str = "") -> None:
        st.left_empty += 1
        if a.label not in st.highlighted:
            st.highlighted.append(a.label)
        if a.source == "consent":
            st.consent.append(a.label)
        elif a.required:
            st.missing.append(a.label)
        loc = a.locator
        if not safe.highlight(loc, "needs_you") and self._count(safe, loc) > 1:
            safe.highlight(f"{loc} >> nth=0", "needs_you")

    def _live(self, safe: Any, st: _Run, ctx: Any, step: Optional[str] = None) -> None:
        """Visible fields the plan didn't cover: resolve them when ctx is given
        (same rules: approved answers only), otherwise just highlight the
        required ones that are still empty."""
        try:
            raw = [f for f in safe.fields() if not f.get("hidden") or f.get("type") == "file"]
        except Exception:  # noqa: BLE001
            return
        for f in _group(raw):
            if st.halted:
                return
            keys = (f["label"], f["id"], f["name"])
            if f.get("has_value") or st.handled(*keys):
                continue
            if self._matches_any(f, self.honeypot_selectors) or _is_honeypot(f):
                continue
            act = self.resolve_field(f, ctx, step=step) if ctx is not None else None
            st.mark(*keys)
            if act is not None and act.value is not None:
                self._fill(safe, [act], _plan_with_files(ctx), st)
            elif act is not None or f.get("required"):
                act = act or FieldAction(f["locator"], f["label"], _KIND.get(f["type"], "fill"), None,
                                         "none", 0.0, True, "not in the plan", True)
                act.required = act.required or bool(f.get("required"))
                self._empty(safe, act, st)

    # -- finishing
    def submit_locator(self, safe: Any) -> Optional[str]:
        for sel in self.submit_selectors:
            if self._count(safe, sel) == 1:
                return sel
        return safe.find_submit()

    def _step_name(self, safe: Any) -> Optional[str]:
        steps = self.spec.get("steps") or {}
        for sel in (steps.get("active"), steps.get("heading")):   # progress bar first, then the visible heading
            if not sel:
                continue
            for name in self.step_order:
                if self._count(safe, f"{sel}:visible:has-text({_css_str(name)})"):
                    return name
        return None

    def _finish(self, safe: Any, plan: FillPlan, st: _Run, step: str) -> Outcome:
        submit = self.submit_locator(safe)
        if submit and self._count(safe, submit) == 1:
            safe.highlight(submit, "submit")          # highlighted for the user; NEVER clicked
        else:
            submit = None
        errors = sum(self._count(safe, e) for e in self.spec.get("errors") or [])
        shot = None
        try:
            from ..safepage import job_dir
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            shot = safe.screenshot(job_dir(plan.job_key or "job") / f"fill-{stamp}.png")
        except Exception as exc:  # noqa: BLE001
            log.info("no screenshot: %s", type(exc).__name__)
        if st.halted:
            status, reason = "needs_you", f"stopped: {st.halted}"
        elif st.missing:
            status, reason = "blocked", "required fields without an approved answer: " + "; ".join(st.missing[:8])
        elif submit is None:
            status, reason = "blocked", "no single Submit control found; review the page yourself"
        else:
            status, reason = "ready", "filled; review the page and press Submit yourself"
        if st.consent:
            reason += "; tick yourself if you agree: " + "; ".join(st.consent[:4])
        if errors:
            reason += f"; the page shows {errors} error marker(s)"
        if st.problems:
            reason += "; " + "; ".join(st.problems[:3])
        log.info("run ats=%s status=%s filled=%d empty=%d step=%s", self.name, status, st.filled,
                 st.left_empty, step)
        return Outcome(status, reason, st.filled, st.left_empty, list(st.highlighted), submit, shot, step)

    # -- multi-step (Workday)
    def _run_steps(self, safe: Any, plan: FillPlan, ctx: Any, st: _Run) -> Outcome:
        steps = self.spec.get("steps") or {}
        order, stop = self.step_order, steps.get("stop_at")
        visits: dict[str, int] = {}
        name: Optional[str] = None
        for _ in range(int(steps.get("max_steps") or 10)):
            block = self._blocker(safe)
            if block:
                safe.halt(block)
                return Outcome("needs_you", block, st.filled, st.left_empty, list(st.highlighted),
                               None, None, name or "start")
            name = self._step_name(safe)
            if name is None:
                return Outcome("needs_you", "can't tell which application step this is; continue yourself",
                               st.filled, st.left_empty, list(st.highlighted), None, None, None)
            if name == stop:
                return self._finish(safe, plan, st, name)
            visits[name] = visits.get(name, 0) + 1
            if visits[name] > 1:
                return Outcome("needs_you", f"stuck on {name}; check the page", st.filled,
                               st.left_empty, list(st.highlighted), None, None, name)
            idx = order.index(name)
            self._fill(safe, plan.steps[idx] if idx < len(plan.steps) else [], plan, st)
            if not st.halted:
                self._live(safe, st, ctx, step=name)
            if st.halted:
                return self._finish(safe, plan, st, name)
            nxt = self.next_locator(safe, idx)
            if not nxt:
                return Outcome("needs_you", f"no Next control on {name}", st.filled, st.left_empty,
                               list(st.highlighted), None, None, name)
            try:
                safe.next_step(nxt)
            except SubmitGuardViolation:
                return Outcome("needs_you", f"the forward button on {name} looks like Submit; stopped",
                               st.filled, st.left_empty, list(st.highlighted), None, None, name)
            safe.wait(300)
            if self._step_name(safe) == name:
                errs = [e for e in self.spec.get("errors") or [] if self._count(safe, e)]
                for e in errs:
                    safe.highlight(f"{e} >> nth=0", "error")
                why = "the form shows validation errors" if errs else "the step didn't advance"
                return Outcome("needs_you", f"{why} on {name}: your turn", st.filled, st.left_empty,
                               list(st.highlighted), None, None, name)
        return Outcome("needs_you", "too many steps; continue yourself", st.filled, st.left_empty,
                       list(st.highlighted), None, None, name)


_SPEC_TYPE = {"fill": "text", "select": "select", "combobox": "combobox", "radio": "radio",
              "check": "checkbox", "upload": "file"}


def _list_value(v: Any) -> list[str]:
    try:
        out = json.loads(v)
        if isinstance(out, list):
            return [str(x) for x in out]
    except (TypeError, ValueError):
        pass
    return [s.strip() for s in str(v).split(",") if s.strip()]


def _plan_with_files(ctx: Any) -> FillPlan:
    files = {k: str(v) for k, v in (_ctx(ctx, "files", {}) or {}).items() if v}
    return FillPlan(job_key="", ats="", url="", files=files)


# --------------------------------------------------------------------------- Greenhouse

@register
class GreenhouseAdapter(SpecAdapter):
    """job-boards.greenhouse.io / boards.greenhouse.io forms (#first_name, #resume
    behind 'Attach', #question_<n> custom questions, react-select comboboxes)."""
    spec_name = "greenhouse"

    @staticmethod
    def snapshot_from_questions_api(data: dict, url: str = "") -> dict:
        """boards-api …/jobs/<id>?questions=true JSON -> snapshot (read-only plan source)."""
        from ..service import snapshot_from_greenhouse
        return snapshot_from_greenhouse(data, url)


__all__ = ["SpecAdapter", "GreenhouseAdapter", "load_spec", "normalize_snapshot", "clean_label",
           "SPEC_DIR"]
