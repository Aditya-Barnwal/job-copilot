"""Phase 5 API for the React UI: review card data, inline edits, the answers bank
writer, fact confirmation, screenshots, the tracker CSV, résumés and the
"Sign in to portals" window.

Every write here is an explicit user action from the UI. Demo mode refuses all
of them (the demo middleware in app.py 403s any write it doesn't allow; the
handlers below also check, so a mounted-without-middleware app is safe too).
Nothing here submits an application.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

from jobcopilot import answers as answers_mod
from jobcopilot import config, demo, store
from jobcopilot import facts as facts_mod
from jobcopilot import queue as jq
from jobcopilot import tailor_v2
from jobcopilot.autofill import service

from ..deps import csv_safe

log = logging.getLogger("jobcopilot.server.ui")
router = APIRouter()


def _no_demo(what: str = "this") -> None:
    if demo.enabled():
        raise HTTPException(status_code=403, detail=f"{what} is off in the public demo")


def _reply(out: dict) -> Any:
    code = out.pop("http", 200)
    if code == 404:
        raise HTTPException(status_code=404, detail=out.get("detail") or "not found")
    if code != 200:
        return JSONResponse(status_code=code, content=out)
    return out


# --------------------------------------------------------------------------- app config

@router.get("/api/ui/config")
def ui_config():
    """What the UI needs before it renders: demo mode, the banner text."""
    return {"demo": demo.enabled(), "banner": demo.BANNER if demo.enabled() else None,
            "autofill": not demo.enabled()}


# --------------------------------------------------------------------------- files

def _served_png(raw: Optional[str]) -> Optional[Path]:
    """A screenshot path from the DB, served only when it is a .png under the data
    dir's applications/ or proofs/ folder (no traversal through a stored path)."""
    if not raw or not isinstance(raw, str) or "\x00" in raw:
        return None
    roots = [store.applications_dir().resolve(), (store.data_dir() / "proofs").resolve()]
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = roots[1] / p
    p = p.resolve()
    if p.suffix.lower() != ".png" or not any(p.is_relative_to(r) for r in roots) or not p.is_file():
        return None
    return p


def _png(p: Optional[Path]) -> FileResponse:
    if p is None:
        raise HTTPException(status_code=404, detail="no screenshot")
    return FileResponse(str(p), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/api/autofill/sessions/{session_id}/screenshot")
def session_screenshot(session_id: int):
    row = store.get_autofill_session(int(session_id))
    if row is None:
        raise HTTPException(status_code=404, detail="no such session")
    return _png(_served_png(row.get("screenshot_path")))


@router.get("/api/jobs/{key}/proof")
def submission_proof(key: str):
    job = store.get_job(key)
    if not job:
        raise HTTPException(status_code=404, detail="not found")
    return _png(_served_png(job.get("submit_proof_path")))


def _session_view(row: dict) -> dict:
    out = dict(row)
    out.pop("pid", None)
    shot = out.pop("screenshot_path", None)
    out["has_screenshot"] = _served_png(shot) is not None
    return out


@router.get("/api/jobs/{key}/autofill/sessions")
def job_sessions(key: str, limit: int = 10):
    if store.get_job(key) is None:
        raise HTTPException(status_code=404, detail="not found")
    rows = store.list_autofill_sessions(job_key=key, limit=max(1, min(int(limit), 50)))
    return {"sessions": [_session_view(r) for r in rows]}


# --------------------------------------------------------------------------- review card

@router.get("/api/artifacts/{artifact_id}/review")
def artifact_review(artifact_id: int):
    out = tailor_v2.review(artifact_id)
    if out is None:
        raise HTTPException(status_code=404, detail="version not found")
    return out


@router.post("/api/jobs/{key}/artifacts/{artifact_id}/edit")
def artifact_edit(key: str, artifact_id: int, body: dict = Body(...)):
    """{rephrase: {bullet fact id: text | null}} -> a NEW version (re-grounded, no LLM)."""
    _no_demo("editing")
    return _reply(tailor_v2.edit(key, artifact_id, (body or {}).get("rephrase")))


@router.post("/api/jobs/{key}/artifacts/{artifact_id}/reject")
def artifact_reject(key: str, artifact_id: int, body: Optional[dict] = Body(default=None)):
    """{reason?: short text, retailor?: bool (default true), base?: str}. Marks the
    version rejected, keeps your reason with it, then (unless retailor is false)
    starts tailoring a new version in the background with the reason as guidance
    for the model, never as facts. Poll /api/jobs/{key}/tailor/v2/status."""
    _no_demo("rejecting a version")
    body = body or {}
    reason, base, again = body.get("reason"), body.get("base"), body.get("retailor", True)
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(status_code=400, detail="reason must be text")
    if len(reason or "") > 2000:
        raise HTTPException(status_code=400, detail="reason is too long")
    if base is not None and not isinstance(base, str):
        raise HTTPException(status_code=400, detail="base must be a string")
    if not isinstance(again, bool):
        raise HTTPException(status_code=400, detail="retailor must be true or false")
    out = tailor_v2.reject(key, artifact_id, reason=reason)
    if out.get("ok") and again:
        from .tailor import start_retailor
        out["tailoring"] = start_retailor(key, guidance=out.get("reason"), base=base,
                                          from_artifact_id=out["artifact_id"])
    return _reply(out)


# --------------------------------------------------------------------------- needs you / answers bank

_RESUME_LABEL = re.compile(r"(?<![A-Za-z])(resume|résumé|cv|curriculum vitae)(?![A-Za-z])", re.I)
_COVER_LABEL = re.compile(r"\bcover\s*letter\b", re.I)
_FILE_REASONS = ("needs approved résumé", "needs approved resume", "no approved cover letter",
                 "file upload", "upload is not an approved file")
_QUALIFIER = re.compile(r"^(?:optional|required)\s+|\s+(?:optional|required)$")


def _file_need(label: str, reason: str, kind: str, ftype: str) -> Optional[str]:
    """'resume' | 'cover_letter' | 'file' when the field is a file (or the cover-letter
    box), which the answers bank never fills; None for a typeable question."""
    r = (reason or "").strip().lower()
    if not (kind == "upload" or ftype == "file" or r.startswith(_FILE_REASONS)):
        return None
    if r == "no approved cover letter" or _COVER_LABEL.search(label):
        return "cover_letter"
    if r.startswith(("needs approved résumé", "needs approved resume")) or _RESUME_LABEL.search(label):
        return "resume"
    return "file"


def _question_key(label: str) -> str:
    """Dedupe key: answers.norm plus a trailing / leading "required" / "optional"."""
    k = answers_mod.norm(label)
    prev = None
    while prev != k:
        prev, k = k, _QUALIFIER.sub("", k).strip()
    return k


def _typeable(label: str) -> bool:
    return bool(re.search(r"[A-Za-z]{2}", label)) and len(label) <= 500


_ACTION_TEXT = {"resume": ("needs an approved résumé", "resume"),
                "cover_letter": ("needs an approved cover letter", "letter"),
                "file": ("asks for a file you attach yourself", "apply")}


def _bank_or_none():
    try:
        return answers_mod.load()
    except answers_mod.AnswersError:
        return None


@router.get("/api/answers/unanswered")
def unanswered(limit: int = 200):
    """Form questions the fill plans could not answer from your answers bank,
    grouped by question (normalised), with the jobs that ask them. A question your
    bank now answers (e.g. you just saved it) is left out."""
    bank = _bank_or_none()
    groups: dict[str, dict] = {}
    actions: dict[tuple, dict] = {}
    files_ok: dict[str, dict] = {}
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT p.job_key, p.plan, p.created_at, j.title, j.company, j.status, j.gone_at "
            "FROM fill_plans p JOIN jobs j ON j.key = p.job_key "
            "WHERE p.unresolved_count > 0 ORDER BY p.created_at DESC LIMIT 500").fetchall()
    import json as _json

    def approved_now(job_key: str) -> dict:
        """The job's approved files right now (the stored plan may predate an approval)."""
        if job_key not in files_ok:
            try:
                files_ok[job_key] = service.approved_files(store.get_job(job_key) or {"key": job_key})[0]
            except Exception:  # noqa: BLE001 — unknown: keep the row, it is only a reminder
                files_ok[job_key] = {}
        return files_ok[job_key]

    seen_jobs: set[str] = set()
    for r in rows:
        if r["job_key"] in seen_jobs:
            continue                      # one plan per job: the newest
        seen_jobs.add(r["job_key"])
        if store.is_submitted_or_later(r["status"]) or store.canonical_status(r["status"]) == "not_interested":
            continue
        if r["gone_at"]:
            continue                      # the posting is closed: nothing to answer
        try:
            plan = _json.loads(r["plan"]) if r["plan"] else {}
        except ValueError:
            continue
        acts = {a.get("label"): a for step in plan.get("steps") or [] if isinstance(step, list)
                for a in step if isinstance(a, dict)}
        snap = {f.get("label"): f for step in (plan.get("snapshot") or {}).get("steps") or []
                if isinstance(step, list) for f in step if isinstance(f, dict)}
        for u in plan.get("unresolved") or []:
            label = (u.get("label") or "").strip() if isinstance(u, dict) else ""
            if not label:
                continue
            act, field = acts.get(label) or {}, snap.get(label) or {}
            kind = act.get("kind") or ""
            ftype = field.get("type") or {"select": "select", "radio": "radio", "check": "checkbox"}.get(kind, "text")
            need = _file_need(label, u.get("reason") or act.get("note") or "", kind, ftype)
            if need:
                # a file (or the cover-letter box) is never an answers-bank question:
                # it is an action on the job's review card, until it is approved
                have = approved_now(r["job_key"])
                if (need == "resume" and have.get("resume")) or (need == "cover_letter" and have.get("cover_letter")):
                    continue
                akey = (r["job_key"], need, label.lower() if need == "file" else "")
                if akey not in actions:
                    text, tab = _ACTION_TEXT[need]
                    actions[akey] = {"kind": need, "label": label, "text": text, "tab": tab,
                                     "required": bool(u.get("required") or act.get("required")),
                                     "job": {"key": r["job_key"], "title": r["title"], "company": r["company"]}}
                continue
            if ftype == "hidden" or field.get("hidden") or not _typeable(label):
                continue
            key = _question_key(label)
            if not key or answers_mod.is_secret_question(label):
                continue
            if service.is_consent_label(label, "check" if ftype in ("checkbox", "multi") else "text"):
                continue      # consent and attestations: only you tick those, in the form
            ftype = {"multi": "checkbox"}.get(ftype, ftype)
            if ftype not in answers_mod.FIELD_TYPES:
                ftype = "text"
            options = [str(o) for o in field.get("options") or [] if str(o).strip()]
            g = groups.setdefault(key, {"question": label, "field_type": ftype, "options": options,
                                        "required": False, "reasons": [], "jobs": []})
            g["required"] = g["required"] or bool(u.get("required") or act.get("required")
                                                  or field.get("required"))
            reason = u.get("reason")
            if reason and reason not in g["reasons"]:
                g["reasons"].append(reason)
            if all(j["key"] != r["job_key"] for j in g["jobs"]):
                g["jobs"].append({"key": r["job_key"], "title": r["title"], "company": r["company"]})
    out = []
    for g in groups.values():
        if bank is not None:
            a = answers_mod.answer_question(g["question"], field_type=g["field_type"],
                                            options=g["options"] or None, answers=bank, allow_open=False)
            if a.value not in ("", None) and not a.needs_user:
                continue
        out.append(g)
    out.sort(key=lambda g: (not g["required"], -len(g["jobs"]), g["question"].lower()))
    order = {"resume": 0, "cover_letter": 1, "file": 2}
    acts_out = sorted(actions.values(), key=lambda a: (order[a["kind"]], not a["required"],
                                                       str(a["job"]["company"] or "").lower()))
    return {"questions": out[: max(1, min(int(limit), 500))], "actions": acts_out[:500],
            "bank_available": bank is not None}


@router.post("/api/answers/save")
def save_answer(body: dict = Body(...)):
    """Save YOUR typed answer to one form question into profile/answers.yaml
    (custom_answers block, approved). Explicit user action only."""
    _no_demo("saving answers")
    body = body or {}
    try:
        out = answers_mod.save_custom_answer(body.get("question"), body.get("answer"))
    except answers_mod.AnswerSaveError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **out}


# --------------------------------------------------------------------------- facts

@router.get("/api/facts")
def facts_review():
    try:
        rf = facts_mod.load()
    except facts_mod.FactsError as exc:
        return {"available": False, "detail": str(exc).split(": ", 1)[-1], "facts": [], "errors": [],
                "counts": {}}
    bases = facts_mod.load_bases_for(rf.path) if rf.path else None
    rows = facts_mod.review_rows(rf)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"available": True, "file": rf.path.name if rf.path else None, "facts": rows,
            "errors": facts_mod.validate(rf, bases)[:50], "counts": counts,
            "usable": len(facts_mod.usable_facts(rf))}


@router.post("/api/facts/{fact_id}/confirm")
def facts_confirm(fact_id: str):
    """Mark one unconfirmed fact or claim confirmed (you checked it is true)."""
    _no_demo("confirming facts")
    try:
        return {"ok": True, **facts_mod.confirm(fact_id)}
    except facts_mod.FactsError as exc:
        raise HTTPException(status_code=400, detail=str(exc).split("resume_facts.yaml: ")[-1])


# --------------------------------------------------------------------------- résumés

_BASE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _bases_dir() -> Path:
    return facts_mod.profile_dir() / "base_resumes"


@router.get("/api/resumes")
def resumes():
    """Your base résumés (LaTeX templates) and every tailored version per job."""
    bases_out = []
    try:
        bases = facts_mod.load_bases(_bases_dir() / "bases.yaml")
    except Exception:  # noqa: BLE001 — missing or invalid file: show none
        bases = {}
    d = _bases_dir()
    for name, b in bases.items():
        tpl = next((p for p in (d / f"{name}.tex.j2", d / f"{name}.tex") if p.is_file()), None)
        bases_out.append({"name": name, "roles": list(getattr(b, "roles", []) or []),
                          "sections": list(getattr(b, "sections", []) or []),
                          "template": tpl.name if tpl else None})
    by_job: dict[str, dict] = {}
    for a in store.list_artifacts(None):
        g = a.get("grounding") or {}
        entry = by_job.setdefault(a["job_key"], {"job_key": a["job_key"], "versions": []})
        entry["versions"].append({
            "id": a["id"], "version": a["version"], "base": a.get("base"), "status": a.get("status"),
            "pages": a.get("pages"), "created_at": a.get("created_at"), "approved_at": a.get("approved_at"),
            "grounding_clean": g.get("clean"),
            "pdf_url": f"/api/artifacts/{a['id']}/pdf" if a.get("pdf_path") else None})
    jobs = []
    for key, entry in by_job.items():
        job = store.get_job(key) or {}
        entry.update(title=job.get("title"), company=job.get("company"),
                     status=store.canonical_status(job.get("status")) if job else None)
        entry["versions"].sort(key=lambda v: v["version"], reverse=True)
        jobs.append(entry)
    jobs.sort(key=lambda e: max((v["created_at"] or 0) for v in e["versions"]), reverse=True)
    return {"bases": bases_out, "jobs": jobs, "legacy_versions": store.list_versions()}


@router.get("/api/resumes/bases/{name}/source")
def base_source(name: str):
    if not _BASE_NAME.match(name):
        raise HTTPException(status_code=404, detail="not found")
    d = _bases_dir().resolve()
    for p in (d / f"{name}.tex.j2", d / f"{name}.tex"):
        p = p.resolve()
        if p.is_relative_to(d) and p.is_file():
            return {"name": name, "file": p.name, "source": p.read_text(encoding="utf-8", errors="replace")}
    raise HTTPException(status_code=404, detail="no template for this base")


# --------------------------------------------------------------------------- hidden by you

def _hidden_info(key: str) -> tuple[Optional[float], str]:
    """(when you hid it, the status it had before) from status_history; the
    status falls back to 'found' when the history has nothing earlier."""
    hist = store.status_history(key)
    idx = max((i for i, h in enumerate(hist) if store.canonical_status(h["status"]) == "not_interested"),
              default=None)
    if idx is None:
        return None, "found"
    prev = next((h["status"] for h in reversed(hist[:idx])
                 if store.canonical_status(h["status"]) != "not_interested"), None)
    return hist[idx]["at"], prev or "found"


@router.get("/api/hidden")
def hidden_by_you(limit: int = 500):
    """Jobs you marked not interested, newest first, with when and what to restore."""
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            jq._SELECT + " WHERE j.status = 'not_interested' "
            "ORDER BY COALESCE(j.status_changed_at, j.last_seen) DESC LIMIT ?",
            (max(1, min(int(limit), 2000)),)).fetchall()]
    items = []
    for r in rows:
        card = jq.summarize_job(r)
        at, prev = _hidden_info(r["key"])
        card.update(hidden_at=at or r.get("status_changed_at"), previous_status=prev)
        items.append(card)
    return {"items": items, "total": len(items)}


@router.post("/api/jobs/{key}/unhide")
def unhide(key: str):
    """Put a job you hid back to the status it had before (history), else 'found'."""
    job = store.get_job(key)
    if not job:
        raise HTTPException(status_code=404, detail="not found")
    if store.canonical_status(job.get("status")) != "not_interested":
        raise HTTPException(status_code=409, detail="this job isn't hidden")
    _at, prev = _hidden_info(key)
    store.set_status(key, prev, source="user", note="unhidden")
    return {"ok": True, "status": prev}


# --------------------------------------------------------------------------- earlier days (Today)

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/api/queue/earlier")
def queue_earlier(days: int = 7, before: Optional[str] = None):
    """Previous queue days, newest first, below today's list: up to `days` days
    before `before` (default: the latest built day, i.e. today's list). Left out:
    postings that are gone or expired, jobs you hid (not interested), and jobs on
    today's list (any lane); a job queued on several days shows on its newest one.
    Jobs you acted on (tailored, ready, submitted, ...) stay, with their status."""
    days = max(1, min(int(days), 60))
    all_days = store.queue_days()
    if before is not None and not _DAY_RE.match(before):
        raise HTTPException(status_code=400, detail="before must be YYYY-MM-DD")
    current = before or (all_days[0] if all_days else None)
    if current is None:
        return {"today": None, "days": [], "has_more": False, "total_days": 0}
    earlier = [d for d in all_days if d < current]
    seen = {j["key"] for j in jq.list_queue(day=current, lane=None).get("items") or []}
    hidden = store.statuses_with_canonical(["not_interested"])
    now = time.time()
    today = jq.today(now)
    sql = (jq._SELECT.replace("SELECT j.*", "SELECT q.lane AS queue_lane, q.rank AS queue_rank, j.*")
           + " JOIN queue_items q ON q.job_key = j.key WHERE q.day = ? AND q.rank IS NOT NULL "
           f"AND {jq._LIVE_SQL} AND COALESCE(j.status, 'new') NOT IN ({', '.join('?' * len(hidden))}) "
           "AND (j.expires_at IS NULL OR j.expires_at = '' OR substr(j.expires_at, 1, 10) >= ?) "
           "ORDER BY CASE q.lane WHEN 'main' THEN 0 WHEN 'extended' THEN 1 ELSE 2 END, q.rank")
    out = []
    with store.connect() as conn:
        for d in earlier[:days]:
            items = []
            for r in conn.execute(sql, (d, *hidden, today)).fetchall():
                r = dict(r)
                if r["key"] in seen:
                    continue
                seen.add(r["key"])
                card = jq.summarize_job(r, now)
                card.update(rank=r["queue_rank"], queue_lane=r["queue_lane"], queue_day=d)
                items.append(card)
            if items:
                out.append({"day": d, "count": len(items), "items": items})
    return {"today": current, "days": out, "has_more": len(earlier) > days, "total_days": len(earlier)}


# --------------------------------------------------------------------------- tracker CSV

TRACKER_CSV_COLUMNS = ["company", "title", "status", "status_changed", "submitted", "days_in_status",
                       "location", "workplace_type", "source", "fit_score", "url", "found", "tailored",
                       "ready_to_submit", "oa", "interview", "offer", "rejected"]


def _iso(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts))) if ts else ""
    except (TypeError, ValueError):
        return ""


@router.get("/api/tracker/export.csv")
def tracker_csv():
    """Every tracked application with its dates. Cells that a spreadsheet would run
    as a formula (= + - @ tab CR) are prefixed with a single quote."""
    t = jq.tracker(limit_per_column=5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TRACKER_CSV_COLUMNS)
    for col in t["columns"]:
        for j in col["items"]:
            dates = j.get("dates") or {}
            row = {"company": j.get("company"), "title": j.get("title"), "status": col["status"],
                   "status_changed": _iso(j.get("status_changed_at")), "submitted": _iso(j.get("submitted_at")),
                   "days_in_status": j.get("days_in_status"), "location": j.get("location"),
                   "workplace_type": j.get("workplace_type"), "source": j.get("source"),
                   "fit_score": j.get("fit_score"), "url": j.get("apply_url") or j.get("url")}
            for s in ("found", "tailored", "ready_to_submit", "oa", "interview", "offer", "rejected"):
                row[s] = _iso(dates.get(s))
            w.writerow([csv_safe(row.get(c)) for c in TRACKER_CSV_COLUMNS])
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=job-copilot-tracker.csv"})


# --------------------------------------------------------------------------- sign in to portals

def _portal_links() -> list[dict]:
    out: list[dict] = []
    try:
        from jobcopilot.ingest import load_companies
        companies = load_companies(str(config.companies_path()))
    except Exception:  # noqa: BLE001
        companies = []
    for c in companies:
        if c.get("connector") == "workday" and c.get("tenant") and c.get("wd_server"):
            site = str(c.get("site") or "").strip("/")
            url = f"https://{c['tenant']}.{c['wd_server']}.myworkdayjobs.com/{site}"
            out.append({"name": c.get("name"), "portal": "Workday", "url": url})
    return sorted(out, key=lambda p: (p["portal"], str(p["name"]).lower()))


def _manual_companies() -> list[dict]:
    """Companies with no readable job board (connector: manual) - checked by hand.
    Only http(s) careers links are passed through; they are shown, never fetched."""
    import yaml
    from jobcopilot.models import safe_url
    out: list[dict] = []
    for path in (config.companies_path(), config.companies_path().parent / "target_companies.yaml"):
        try:
            rows = (yaml.safe_load(Path(path).read_text()) or {}).get("companies") or []
        except Exception:  # noqa: BLE001 - a missing / broken file just adds nothing
            continue
        for r in rows:
            if str(r.get("connector") or "").lower() != "manual" or r.get("enabled") is False:
                continue
            url = safe_url(r.get("careers_url")) or ""
            out.append({"name": str(r.get("name") or ""), "url": url, "tier": r.get("tier"),
                        "note": str(r.get("note") or "")[:200]})
    seen, uniq = set(), []
    for c in sorted(out, key=lambda c: c["name"].lower()):
        if c["name"].lower() not in seen:
            seen.add(c["name"].lower()); uniq.append(c)
    return uniq


@router.get("/api/portals")
def portals():
    from jobcopilot.autofill import session as sess
    return {"links": _portal_links(), "manual": _manual_companies(), "available": not demo.enabled(),
            "chrome": sess.chrome_executable() is not None if not demo.enabled() else False}


@router.post("/api/portals/open")
def portals_open():
    """Open the dedicated Chrome profile (~/.jobcopilot/browser-profile) with one
    blank tab, for you to sign in to portals yourself. Refused in the demo and
    while a fill window is using that profile."""
    _no_demo("the sign-in browser")
    from jobcopilot.autofill import session as sess
    busy = store.list_autofill_sessions(status=store.AUTOFILL_OPEN_STATUSES, limit=1)
    if busy:
        raise HTTPException(status_code=409,
                            detail="a fill window is using the browser profile; finish or close it first")
    try:
        return sess.open_for_sign_in()
    except sess.BrowserUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
