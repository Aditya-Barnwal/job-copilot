"""Generated writing + answers bank (Phase 3, workstream C).

    POST /api/jobs/{key}/cover-letter   {base?}            -> Draft
    GET  /api/jobs/{key}/cover-letter/saved                -> the saved letter + approval state
    POST /api/jobs/{key}/cover-letter/save     {text}      -> checks run, stored, NOT approved
    POST /api/jobs/{key}/cover-letter/approve  {sha256, confirm_flags?} -> approved
    GET  /api/artifacts/{id}/cover-letter/pdf              -> the rendered PDF
    POST /api/jobs/{key}/email          {kind}             -> Draft (application | follow_up)
    POST /api/answers/preview           {question, field_type?, options?, job_key?, required?} -> Answer
    GET  /api/answers/review                               -> review_report()

Drafts are shown to the user before use; nothing is sent or submitted here.
A saved letter is used by autofill only after its own approval
(jobcopilot/cover_letters.py, docs/DESIGN.md §10).
No confirmed facts → 409 with a "confirm your facts first" message.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from jobcopilot import answers as answers_mod
from jobcopilot import cover_letters
from jobcopilot import facts as facts_mod
from jobcopilot import store
from jobcopilot.writing import WritingError, generate_cover_letter, generate_email
from server.deps import enrich

log = logging.getLogger("jobcopilot.server.writing")
router = APIRouter()


def _job(key: str) -> dict:
    job = store.get_job(key)
    if not job:
        raise HTTPException(status_code=404, detail="not found")
    return enrich([job])[0]


def _facts():
    try:
        return facts_mod.load()
    except facts_mod.FactsError:
        return None


def _bank():
    try:
        return answers_mod.load()
    except answers_mod.AnswersError as exc:
        raise HTTPException(status_code=409, detail=f"answers.yaml unavailable: {exc}")


def _respond(draft) -> dict:
    if draft.status == "refused":
        raise HTTPException(status_code=409, detail=" ".join(draft.warnings))
    return draft.to_dict()


@router.post("/api/jobs/{key}/cover-letter")
def cover_letter_endpoint(key: str, body: dict = Body(default={})):
    job = _job(key)
    base = (body or {}).get("base")
    if base is not None and not isinstance(base, str):
        raise HTTPException(status_code=400, detail="base must be a string")
    facts = _facts()
    if facts is None:
        raise HTTPException(status_code=409, detail="no résumé facts file — confirm your facts first")
    try:
        draft = generate_cover_letter(job, facts=facts, answers=_bank(), base=base or None)
    except WritingError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return _respond(draft)


def _letter_call(fn, *a, **kw) -> dict:
    try:
        return fn(*a, **kw)
    except cover_letters.LetterError as exc:
        raise HTTPException(status_code=exc.http, detail=exc.detail)


@router.get("/api/jobs/{key}/cover-letter/saved")
def cover_letter_saved(key: str):
    return _letter_call(cover_letters.status, key)


@router.post("/api/jobs/{key}/cover-letter/save")
def cover_letter_save(key: str, body: dict = Body(...)):
    text = (body or {}).get("text")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="text must be a string")
    if not store.get_job(key):
        raise HTTPException(status_code=404, detail="not found")
    facts = _facts()
    if facts is None:
        raise HTTPException(status_code=409, detail="no résumé facts file — confirm your facts first")
    return _letter_call(cover_letters.save, key, text, facts=facts, answers=_bank())


@router.post("/api/jobs/{key}/cover-letter/approve")
def cover_letter_approve(key: str, body: dict = Body(...)):
    body = body or {}
    confirm = body.get("confirm_flags", False)
    if not isinstance(confirm, bool):
        raise HTTPException(status_code=400, detail="confirm_flags must be true or false")
    if not store.get_job(key):
        raise HTTPException(status_code=404, detail="not found")
    facts = _facts()
    if facts is None:
        raise HTTPException(status_code=409, detail="no résumé facts file — confirm your facts first")
    out = _letter_call(cover_letters.approve, key, sha=body.get("sha256"), confirm_flags=confirm,
                       facts=facts, answers=_bank())
    if out.get("needs_confirmation"):
        raise HTTPException(status_code=409, detail=out)
    return out


@router.get("/api/artifacts/{artifact_id}/cover-letter/pdf")
def cover_letter_pdf(artifact_id: int):
    p = cover_letters.pdf_path(artifact_id)     # only a PDF under data/applications is served
    if p is None:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(p), media_type="application/pdf", filename="cover_letter.pdf")


@router.post("/api/jobs/{key}/email")
def email_endpoint(key: str, body: dict = Body(default={})):
    kind = (body or {}).get("kind") or "application"
    if kind not in ("application", "follow_up"):
        raise HTTPException(status_code=400, detail="kind must be application or follow_up")
    job = _job(key)
    facts = _facts()
    if facts is None:
        raise HTTPException(status_code=409, detail="no résumé facts file — confirm your facts first")
    try:
        draft = generate_email(job, kind=kind, facts=facts, answers=_bank())
    except WritingError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return _respond(draft)


@router.post("/api/answers/preview")
def answers_preview(body: dict = Body(...)):
    q = (body or {}).get("question")
    if not isinstance(q, str) or not q.strip():
        raise HTTPException(status_code=400, detail="question required")
    ft = body.get("field_type") or "text"
    if ft not in answers_mod.FIELD_TYPES:
        raise HTTPException(status_code=400, detail=f"field_type must be one of {answers_mod.FIELD_TYPES}")
    options = body.get("options")
    if options is not None and (not isinstance(options, list)
                                or not all(isinstance(o, str) for o in options)):
        raise HTTPException(status_code=400, detail="options must be a list of strings")
    job = _job(body["job_key"]) if body.get("job_key") else None
    required = body.get("required")
    ans = answers_mod.answer_question(q, field_type=ft, options=options, job=job, answers=_bank(),
                                      facts=_facts(),
                                      required=required if isinstance(required, bool) else None)
    return ans.to_dict()


@router.get("/api/answers/review")
def answers_review():
    return answers_mod.review_report(_bank())
