"""GenericAdapter: the fallback for any form no ATS adapter claims.

Field discovery: label[for] / wrapping label / aria-labelledby / aria-label /
placeholder / name (SafePage.fields), radio + checkbox groups by name with
their legend. Each field → answers.answer_question(label, field_type,
options, job=...); résumé / cover-letter file inputs → the approved files by
label / name / accept. Below min_confidence, unapproved (needs_user) or no
answer → left EMPTY and highlighted. Honeypots are never filled; consent
boxes are never ticked; password fields mean a login wall.

Also accepts the service's snapshot shape ({"steps": [[{label, type,
locator, options: [str], required, hidden}]]}) and a dict ctx ({job, files,
min_confidence, answer}).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from . import detect
from .adapters.base import Adapter, PlanContext, register
from .guard import is_consent_label, is_denied
from .plan import COVER_TEXT_SOURCE, FieldAction, FillPlan, is_cover_letter_text_field

log = logging.getLogger("jobcopilot.autofill.generic")

DEFAULT_MIN_CONFIDENCE = 0.8
_RESUME_RE = re.compile(r"r[eé]sum[eé]|\bcv\b|curriculum", re.I)
_COVER_RE = re.compile(r"cover", re.I)
_PLACEHOLDER_RE = re.compile(r"^\s*(--+|select|choose|please select|pick)\b.*$|^\s*$", re.I)
_FT = {"textarea": "textarea", "number": "number", "date": "date", "select": "select",
       "radio": "radio", "checkbox": "checkbox", "multi": "checkbox"}


def _ctx_parts(ctx: Any) -> tuple[Callable[..., Any], dict, float, str, str]:
    """(answer(q, field_type=, options=, required=), files, min_conf, job_key, url)."""
    if isinstance(ctx, PlanContext):
        return (ctx.answer, dict(ctx.files or {}),
                float(getattr(ctx, "min_confidence", None) or DEFAULT_MIN_CONFIDENCE), ctx.job_key, ctx.url)
    ctx = dict(ctx or {})
    job = ctx.get("job") or {}
    fn = ctx.get("answer") or ctx.get("answer_fn")
    if fn is None:
        from .. import answers as A

        def fn(q, **kw):  # noqa: E306
            return A.answer_question(q, job=job or None, answers=ctx.get("answers"),
                                     facts=ctx.get("facts"), **kw)
    return (fn, dict(ctx.get("files") or {}), float(ctx.get("min_confidence") or DEFAULT_MIN_CONFIDENCE),
            str(ctx.get("job_key") or job.get("key") or job.get("job_key") or ""),
            str(ctx.get("url") or job.get("apply_url") or job.get("url") or ""))


def _ctx_cover(ctx: Any) -> Optional[str]:
    """The approved cover letter's text from a PlanContext or dict ctx (or None)."""
    v = ctx.get("cover_letter_text") if isinstance(ctx, dict) else getattr(ctx, "cover_letter_text", None)
    return v if isinstance(v, str) and v.strip() else None


def _css_str(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def normalize(snapshot: Any) -> list[dict]:
    """Snapshot → items {label, question, type, required, options, locator, hidden,
    honeypot, name, accept, kind}, radio / checkbox groups merged."""
    if isinstance(snapshot, dict):
        raw = snapshot.get("fields")
        if raw is None:
            steps = snapshot.get("steps") or [[]]
            raw = steps[0] if steps else []
    else:
        raw = list(snapshot or [])
    items: list[dict] = []
    groups: dict[tuple[str, str], dict] = {}
    for f in raw:
        if "selector" not in f:                    # service shape: already one item per question
            t = (f.get("type") or "text").lower()
            items.append({"label": f.get("label") or f.get("name") or "", "question": f.get("label") or "",
                          "type": t, "required": bool(f.get("required")),
                          "options": [str(o) for o in f.get("options") or []],
                          "locator": f.get("locator") or "", "hidden": bool(f.get("hidden")),
                          "honeypot": detect.honeypot({"name": f.get("name"), "label": f.get("label"),
                                                       "type": t}),
                          "name": f.get("name") or "", "accept": f.get("accept") or "",
                          "unit_hint": str(f.get("unit_hint") or ""), "digits_only": False})
            continue
        t = f.get("type") or "text"
        own = f.get("label") or ""
        if t in ("radio", "checkbox") and f.get("name"):
            key = (t, f["name"])
            g = groups.get(key)
            if g is None:
                g = {"label": f.get("group_label") or own, "question": f.get("group_label") or "",
                     "type": t, "required": bool(f.get("required")), "options": [], "metas": [],
                     "locator": f'input[type="{t}"][name={_css_str(f["name"])}]', "hidden": False,
                     "honeypot": False, "name": f["name"], "accept": ""}
                groups[key] = g
                items.append(g)
            g["options"].append(own)
            g["metas"].append(f)
            g["required"] = g["required"] or bool(f.get("required"))
            continue
        opts = []
        if t == "select":
            opts = [o["label"] for o in f.get("options") or []
                    if not o.get("disabled") and not (o.get("value") in ("", None) and _PLACEHOLDER_RE.match(o["label"] or ""))
                    and not _PLACEHOLDER_RE.match(o["label"] or "")]
        items.append({"label": own or f.get("group_label") or f.get("name") or "",
                      "question": own or f.get("group_label") or "", "type": t,
                      "required": bool(f.get("required")), "options": opts,
                      "locator": f.get("selector") or "", "hidden": bool(f.get("hidden")),
                      "honeypot": detect.honeypot(f), "name": f.get("name") or "",
                      "accept": f.get("accept") or "", "unit_hint": str(f.get("unit_hint") or ""),
                      "digits_only": _digits_only(f)})
    for g in groups.values():                      # a lone checkbox is its own question
        metas = g.pop("metas")
        if g["type"] == "checkbox" and len(metas) == 1:
            m = metas[0]
            g["locator"] = m.get("selector") or g["locator"]
            g["question"] = m.get("group_label") or m.get("label") or ""
            g["label"] = m.get("label") or g["question"]
        if all(detect.honeypot(m) for m in metas):
            g["honeypot"] = True
        if all(m.get("hidden") or m.get("offscreen") or m.get("aria_hidden") for m in metas):
            g["hidden"] = True                     # an invisible group is never ticked
    return items


def _digits_only(meta: dict) -> bool:
    from .safepage import _DIGITS_PATTERN_RE
    return (str(meta.get("inputmode") or "") in ("numeric", "decimal")
            or bool(_DIGITS_PATTERN_RE.match(str(meta.get("pattern") or ""))))


def _value_str(v: Any) -> Optional[str]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return None
    return str(v)


@register
class GenericAdapter(Adapter):
    name = "generic"
    hosts = ()
    priority = -1000

    @classmethod
    def matches(cls, url: str) -> bool:
        return bool(re.match(r"^https?://", url or "")) and not is_denied(url)

    def build_plan(self, form_snapshot: Any, ctx: Any) -> FillPlan:
        answer, files, min_conf, job_key, url = _ctx_parts(ctx)
        if isinstance(form_snapshot, dict) and form_snapshot.get("url"):
            url = url or str(form_snapshot["url"])
        actions: list[FieldAction] = []
        unresolved: list[dict] = []

        def leave(it: dict, reason: str, kind: str, note: str = "", source: str = "none",
                  conf: float = 0.0) -> None:
            actions.append(FieldAction(it["locator"], it["label"], kind, None, source, conf, True,
                                       note or reason, it["required"]))

        for it in normalize(form_snapshot):
            t, label = it["type"], it["label"]
            if it["honeypot"]:
                unresolved.append({"label": label or it["name"], "reason": "honeypot",
                                   "locator": it["locator"]})
                continue
            if t == "password":
                unresolved.append({"label": label, "reason": "login wall: sign in yourself",
                                   "locator": it["locator"], "required": it["required"]})
                continue
            if it["hidden"] and t in ("checkbox", "radio", "select"):
                # an invisible box / radio / select is never ticked or set; listed for you
                leave(it, "hidden control: choose it yourself", {"checkbox": "check"}.get(t, t))
                continue
            if it["hidden"] and t != "file":
                continue                           # invisible text field: never touched
            if not it["locator"]:
                unresolved.append({"label": label, "reason": "no locator", "required": it["required"]})
                continue
            if t == "file":
                hay = " ".join([label, it["name"], it["accept"]])
                key = "cover_letter" if _COVER_RE.search(hay) else ("resume" if _RESUME_RE.search(hay) else "")
                if key and files.get(key):
                    actions.append(FieldAction(it["locator"], label, "upload", key, f"file:{key}", 1.0,
                                               False, "", it["required"]))
                else:
                    leave(it, "no approved file" if key else "file upload: attach it yourself", "upload")
                continue
            if t == "textarea" and is_cover_letter_text_field(label or it["question"], it["name"]):
                cover = _ctx_cover(ctx)
                if cover:
                    actions.append(FieldAction(it["locator"], label, "fill", cover, COVER_TEXT_SOURCE, 1.0,
                                               False, "", it["required"]))
                else:
                    leave(it, "no approved cover letter", "fill")
                continue
            question = it["question"] or label
            if not question.strip():
                unresolved.append({"label": it["name"] or "(unlabelled field)", "reason": "no label",
                                   "locator": it["locator"], "required": it["required"]})
                continue
            if t == "checkbox" and len(it["options"]) <= 1 and (
                    is_consent_label(label) or is_consent_label(question)):
                leave(it, "consent / attestation: only you can tick this", "check")
                continue
            if t in ("radio", "select") and (
                    is_consent_label(label) or is_consent_label(question)
                    or (t == "radio" and any(is_consent_label(o, kind="radio") for o in it["options"]))):
                leave(it, "consent / attestation: only you can choose this",
                      "radio" if t == "radio" else "select", source="consent")
                continue
            ft = _FT.get(t, "text")
            extra: dict = {}
            if it.get("unit_hint"):
                extra["unit_hint"] = it["unit_hint"]
            if it.get("digits_only"):
                extra["digits_only"] = True
            try:
                a = answer(question, field_type=ft, options=it["options"] or None,
                           required=it["required"], **extra)
            except Exception as exc:  # noqa: BLE001 — no answer is never a guess
                log.info("answer failed for %r: %s", label, type(exc).__name__)
                leave(it, "no answer", "check" if t == "checkbox" else "fill")
                continue
            ok = (not a.needs_user and float(a.confidence or 0) >= min_conf
                  and a.value not in (None, "", []))
            src, conf = a.source or "none", float(a.confidence or 0)
            note = a.note or ("needs your approval" if a.needs_user else "low confidence")
            if t == "checkbox":
                picked = a.value if isinstance(a.value, (list, tuple)) else (
                    [it["options"][0]] if it["options"] and _value_str(a.value) in ("true", "yes") else [])
                if len(it["options"]) <= 1:
                    if ok and picked:
                        actions.append(FieldAction(it["locator"], label, "check", "true", src, conf,
                                                   False, "", it["required"]))
                    elif not ok:
                        leave(it, note, "check", source=src, conf=conf)
                    continue
                if not ok:
                    leave(it, note, "check", source=src, conf=conf)
                    continue
                for opt in picked:
                    actions.append(FieldAction(it["locator"], f"{label}: {opt}", "check", str(opt), src,
                                               conf, False, "", False))
                continue
            kind = {"select": "select", "radio": "radio"}.get(t, "fill")
            value = _value_str(a.value)
            if not ok or value is None:
                leave(it, note, kind, source=src, conf=conf)
                continue
            if kind == "fill":                 # the same check SafePage.fill makes before typing
                from .. import answers as A
                why = A.implausible_number(value, label=" ; ".join(dict.fromkeys(x for x in (label, question) if x)),
                                           hint=it.get("unit_hint") or "", numeric=t == "number",
                                           digits_only=bool(it.get("digits_only")))
                if why:
                    leave(it, why + ": please answer it yourself", kind, source=src)
                    continue
            actions.append(FieldAction(it["locator"], label, kind, value, src, conf, False, a.note or "",
                                       it["required"]))
        log.info("generic plan: %d actions, %d unresolved", len(actions), len(unresolved))
        return FillPlan(job_key=job_key, ats="generic", url=url, steps=[actions],
                        unresolved=unresolved, files=files)


__all__ = ["GenericAdapter", "normalize"]
