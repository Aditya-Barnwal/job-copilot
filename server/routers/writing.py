"""Generated writing + answers bank (Phase 3, workstream C).

    POST /api/jobs/{key}/cover-letter   {base?}            -> Draft
    POST /api/jobs/{key}/email          {kind}             -> Draft (application | follow_up)
    POST /api/answers/preview           {question, field_type?, options?, job_key?, required?} -> Answer
    GET  /api/answers/review                               -> review_report()

Drafts are shown to the user before use; nothing is sent or submitted here.
No confirmed facts → 409 with a "confirm your facts first" message.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException

from jobcopilot import answers as answers_mod
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
