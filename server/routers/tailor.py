"""Tailoring, approval (with the server-side grounding gate), email drafts,
and stored résumé versions."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from jobcopilot import demo, email_draft, scoring, store
from jobcopilot import tailor as tailor_mod
from jobcopilot import tailor_v2
from jobcopilot import versions as versions_mod
from jobcopilot.grounding import check_grounding

from ..deps import enrich
from ..grounding_gate import unconfirmed_flags

log = logging.getLogger("jobcopilot.server.tailor")

router = APIRouter()


@router.get("/api/jobs/{key}/resume-options")
def resume_options_endpoint(key: str):
    result = tailor_mod.resume_options(key)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/api/jobs/{key}/tailor-suggestion")
def tailor_suggestion(key: str):
    result = tailor_mod.suggest(key)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/api/jobs/{key}/tailor")
def tailor_job(key: str, body: Optional[dict] = Body(default=None),
               v: Optional[int] = Query(default=None)):
    body = body or {}
    if v == 2 or (v is None and tailor_v2.engine() == "v2"):
        return tailor_v2_endpoint(key, body)
    approved = body.get("approved_keywords") or []
    try:
        result = tailor_mod.tailor(key, approved_keywords=approved)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


def _parse_version_id(raw) -> int:
    # bool is an int subclass; True must not silently mean version 1.
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise HTTPException(status_code=400, detail="reuse_version_id must be an integer")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="reuse_version_id must be an integer")


def _grounding_gate(key: str, job: dict, text: str, body: dict) -> Optional[JSONResponse]:
    """Server-side grounding gate (F4), shared by fresh approval and reuse.

    Recomputed here on the exact text being linked to the job, against the
    CURRENT stored resume, so skipping or editing the browser-side check — or
    reusing a version approved against an older resume — can't get fabricated
    content attached to an application. Returns the 409 response to send, or
    None when the text is clean or every flag is explicitly confirmed.
    """
    approved = body.get("approved_keywords") or []
    if not isinstance(approved, list):
        raise HTTPException(status_code=400, detail="approved_keywords must be a list")

    resume = store.get_resume()
    if not resume or not resume.get("text"):
        raise HTTPException(
            status_code=400,
            detail="no stored resume to check this text against; upload your resume first",
        )
    grounding = check_grounding(
        original_resume=resume["text"],
        tailored_text=text,
        job_missing_keywords=job.get("missing_keywords") or [],
        user_approved_keywords={str(k) for k in approved},
    )
    if grounding["clean"]:
        return None
    unconfirmed = unconfirmed_flags(grounding, body.get("confirmed_flags"))
    if unconfirmed:
        log.info("approve blocked key=%s unconfirmed_flags=%d",
                 key, sum(len(v) for v in unconfirmed.values()))
        return JSONResponse(status_code=409, content={
            "detail": ("Grounding check flagged content that is not in your stored "
                       "resume. Confirm every flagged item or edit the text, then "
                       "approve again."),
            "grounding": grounding,
            "unconfirmed": unconfirmed,
        })
    log.info("approve key=%s grounding flags confirmed by user", key)
    return None


# Approving a résumé moves a job to ready only from these tracker columns; a job
# already ready, submitted (or later) or dismissed keeps its status.
_ADVANCE_FROM = frozenset({"found", "tailored"})


def _advance_to_ready(key: str, job: dict) -> None:
    if store.canonical_status(job.get("status")) in _ADVANCE_FROM:
        store.set_status(key, "ready", source="system")


@router.post("/api/jobs/{key}/approve")
def approve_job(key: str, body: dict = Body(...), v: Optional[int] = Query(default=None)):
    if v == 2 or body.get("artifact_id") is not None:
        aid = body.get("artifact_id")
        if aid is None:
            raise HTTPException(status_code=400, detail="artifact_id is required with v=2")
        return approve_artifact(key, _parse_int(aid, "artifact_id"), body)
    job = store.get_job(key)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    reuse_id = body.get("reuse_version_id")
    if reuse_id is not None:
        vid = _parse_version_id(reuse_id)
        version = store.get_version(vid)
        if not version:
            raise HTTPException(status_code=404, detail="version not found")
        text = version.get("tailored_text")
        if not isinstance(text, str) or not text.strip():
            # Nothing to re-check against the current resume: fail closed.
            return JSONResponse(status_code=409, content={
                "detail": ("This stored version has no text to check against your "
                           "current resume. Tailor a fresh version instead."),
            })
        blocked = _grounding_gate(key, job, text, body)
        if blocked is not None:
            return blocked
        store.link_job_version(key, vid)
        _advance_to_ready(key, job)
        return {"ok": True, "mode": "reuse", "version_id": version["id"],
                "pdf_path": version["pdf_path"]}

    tailored_text = body.get("tailored_text")
    if not isinstance(tailored_text, str) or not tailored_text.strip():
        raise HTTPException(status_code=400, detail="missing tailored_text or reuse_version_id")

    blocked = _grounding_gate(key, job, tailored_text, body)
    if blocked is not None:
        return blocked

    required = job.get("required_keywords") or []
    new_have = scoring.extract_keywords(tailored_text)
    new_coverage, _ = scoring.compute_coverage(required, new_have)

    label = body.get("label") or f"{job.get('company','')} · {job.get('title','')[:40]}"
    version = versions_mod.save_tailored_version(
        label=label,
        role_family=job.get("role_family"),
        source_job_key=key,
        source_company=job.get("company"),
        tailored_text=tailored_text,
        target_keywords=required,
        base_ats_coverage=new_coverage,
    )
    store.link_job_version(key, int(version["id"]))
    _advance_to_ready(key, job)
    return {"ok": True, "mode": "fresh", "version_id": version["id"],
            "pdf_path": version["pdf_path"], "new_ats_coverage": new_coverage}


@router.post("/api/jobs/{key}/draft-email")
def draft_email_endpoint(key: str):
    job = store.get_job(key)
    if not job:
        raise HTTPException(status_code=404, detail="not found")
    enriched = enrich([job])[0]
    if (enriched.get("apply_method") or "") != "email" or not enriched.get("contact_email"):
        raise HTTPException(
            status_code=400,
            detail="this job is not flagged apply_method:email in companies.yaml",
        )
    try:
        result = email_draft.draft(key, contact_email=enriched["contact_email"],
                                   apply_method="email")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/api/resume-versions")
def list_versions_endpoint():
    return {"versions": store.list_versions()}


@router.get("/api/resume-versions/{vid}/download")
def download_version(vid: int):
    v = store.get_version(vid)
    if not v or not v.get("pdf_path"):
        raise HTTPException(status_code=404, detail="not found")
    p = Path(v["pdf_path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="pdf missing on disk")
    return FileResponse(str(p), media_type="application/pdf", filename=p.name)


# --- tailoring v2 (Phase 3): edit plans over confirmed facts -----------------

def _parse_int(raw, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise HTTPException(status_code=400, detail=f"{name} must be an integer")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be an integer")


def _str_list(body: dict, name: str) -> list[str]:
    raw = body.get(name)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail=f"{name} must be a list")
    return [str(x) for x in raw]


_REFUSAL_HTTP = {"job_not_found": 404, "no_confirmed_facts": 409, "no_base": 400}


@router.get("/api/jobs/{key}/tailor/v2/keywords")
def tailor_v2_keywords(key: str, base: Optional[str] = None):
    """Missing keywords BEFORE tailoring, so the UI can strike or attest them."""
    out = tailor_v2.preview(key, base=base)
    if out["status"] == "refused":
        return JSONResponse(status_code=_REFUSAL_HTTP.get(out["code"], 400), content=out)
    return out


@router.post("/api/jobs/{key}/tailor/v2")
def tailor_v2_endpoint(key: str, body: Optional[dict] = Body(default=None)):
    body = body or {}
    base = body.get("base")
    if base is not None and not isinstance(base, str):
        raise HTTPException(status_code=400, detail="base must be a string")
    approved = _str_list(body, "approved_keywords")
    struck = _str_list(body, "struck_keywords")
    try:
        res = tailor_v2.tailor(key, base=base, approved_keywords=approved, struck_keywords=struck)
    except Exception as exc:
        log.error("tailor v2 failed key=%s: %s", key, exc)
        raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}")
    out = res.to_dict()
    out["engine"] = "v2"
    if res.status == "refused":
        return JSONResponse(status_code=_REFUSAL_HTTP.get(res.code or "", 400), content=out)
    return out


def start_retailor(key: str, *, guidance=None, base=None, from_artifact_id=None) -> dict:
    """Start a background re-tailoring for a job (shared by "Tailor again" and
    rejecting a version). Returns the tailoring state; {"state": "skipped", ...}
    when the posting is closed, so no LLM call is spent on it."""
    if guidance is not None and not isinstance(guidance, str):
        raise HTTPException(status_code=400, detail="guidance must be text")
    if base is not None and not isinstance(base, str):
        raise HTTPException(status_code=400, detail="base must be a string")
    job = store.get_job(key)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.get("gone_at"):
        return {"state": "skipped", "detail": "This posting is closed, so it wasn't tailored again."}
    if not tailor_v2.has_confirmed_facts():
        return {"state": "failed", "code": "no_confirmed_facts", "detail": tailor_v2.CONFIRM_FACTS_MSG}
    _started, state = tailor_v2.start_background(key, guidance=guidance, base=base,
                                                 from_artifact_id=from_artifact_id)
    return state


@router.post("/api/jobs/{key}/tailor/v2/start")
def tailor_v2_start(key: str, body: Optional[dict] = Body(default=None)):
    """"Tailor again": a new version in the background, with optional guidance
    (an opinion for the model, never a fact). Poll /tailor/v2/status."""
    body = body or {}
    if demo.enabled():
        raise HTTPException(status_code=403, detail="tailoring is off in the public demo")
    aid = body.get("from_artifact_id")
    aid = None if aid is None else _parse_int(aid, "from_artifact_id")
    if tailor_v2.background_state(key).get("state") == "running":
        return JSONResponse(status_code=409, content={"detail": "a new version is already being tailored",
                                                      "tailoring": tailor_v2.background_state(key)})
    state = start_retailor(key, guidance=body.get("guidance"), base=body.get("base"), from_artifact_id=aid)
    if state.get("state") == "skipped":
        return JSONResponse(status_code=409, content={"detail": state["detail"], "tailoring": state})
    if state.get("state") == "failed":
        return JSONResponse(status_code=409, content={"detail": state["detail"], "tailoring": state})
    return {"ok": True, "tailoring": state}


@router.get("/api/jobs/{key}/tailor/v2/status")
def tailor_v2_status(key: str):
    """The job's background tailoring: {state: idle | running | done | failed, ...}."""
    return tailor_v2.background_state(key)


@router.post("/api/jobs/{key}/artifacts/{artifact_id}/approve")
def approve_artifact(key: str, artifact_id: int, body: Optional[dict] = Body(default=None)):
    body = body or {}
    approved = body.get("approved_keywords")
    if approved is not None and not isinstance(approved, list):
        raise HTTPException(status_code=400, detail="approved_keywords must be a list")
    out = tailor_v2.approve(key, artifact_id, confirmed_flags=body.get("confirmed_flags"),
                            approved_keywords=approved)
    code = out.pop("http", 200)
    if code == 404:
        raise HTTPException(status_code=404, detail=out.get("detail"))
    if code != 200:
        return JSONResponse(status_code=code, content=out)
    return out


def _unchanged(plan) -> bool:
    """True when the version is the base résumé unchanged (recorded, or an empty
    plan stored before the reason was recorded)."""
    if not isinstance(plan, dict):
        return False
    return bool(plan.get("unchanged")) or not (tailor_v2._plan_has_edits(plan) or plan.get("one_page_dropped"))


def _artifact_summary(a: dict) -> dict:
    g = a.get("grounding") or {}
    return {"id": a["id"], "job_key": a["job_key"], "version": a["version"], "base": a.get("base"),
            "status": a.get("status"), "pages": a.get("pages"), "created_at": a.get("created_at"),
            "approved_at": a.get("approved_at"), "grounding_clean": g.get("clean"),
            "flags": g.get("flags"), "edit_plan": a.get("edit_plan"),
            "unchanged": _unchanged(a.get("edit_plan")),
            "pdf_url": f"/api/artifacts/{a['id']}/pdf" if a.get("pdf_path") else None}


@router.get("/api/jobs/{key}/artifacts")
def list_artifacts_endpoint(key: str):
    if not store.get_job(key):
        raise HTTPException(status_code=404, detail="job not found")
    return {"artifacts": [_artifact_summary(a) for a in store.list_artifacts(key)]}


@router.get("/api/artifacts/{artifact_id}/pdf")
def artifact_pdf(artifact_id: int):
    a = store.get_artifact(artifact_id)
    if not a or not a.get("pdf_path"):
        raise HTTPException(status_code=404, detail="not found")
    root = store.applications_dir().resolve()
    p = Path(a["pdf_path"]).resolve()
    # Only files under data/applications are ever served (no traversal via a stored path).
    if not p.is_relative_to(root) or p.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="not found")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="pdf missing on disk")
    return FileResponse(str(p), media_type="application/pdf",
                        filename=f"resume_v{a['version']}.pdf")
