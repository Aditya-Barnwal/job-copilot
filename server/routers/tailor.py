"""Tailoring, approval (with the server-side grounding gate), email drafts,
and stored résumé versions."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from jobcopilot import email_draft, scoring, store
from jobcopilot import tailor as tailor_mod
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
def tailor_job(key: str, body: Optional[dict] = Body(default=None)):
    body = body or {}
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


@router.post("/api/jobs/{key}/approve")
def approve_job(key: str, body: dict = Body(...)):
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
        store.set_status(key, "ready", source="system")
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
    store.set_status(key, "ready", source="system")
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
