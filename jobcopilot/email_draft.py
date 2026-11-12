"""Email drafter for `apply_method: email` companies (e.g. BalkanID) — v1 endpoint.

POST /api/jobs/{key}/draft-email keeps its contract ({to, subject, body,
grounding} or {error}); the drafting itself lives in
jobcopilot/writing/email.py (`draft_from_resume_text`, grounded against the
uploaded résumé). The facts-grounded v2 drafts (application / follow-up) are
`writing.email.generate_email`, served by POST /api/jobs/{key}/email.
Never auto-sends; any unsupported claim or fabricated number is flagged and
Approve is blocked until the user confirms.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import llm, store  # noqa: F401 — `llm` kept: tests patch email_draft.llm.complete_json
from .writing import email as writing_email

log = logging.getLogger("jobcopilot.email")


def draft(job_key: str, *, contact_email: Optional[str] = None,
          apply_method: Optional[str] = None) -> dict:
    """Draft an application email for `job_key`.

    `contact_email` / `apply_method` come from companies.yaml, not the jobs
    table, so the caller passes them in.
    """
    job = store.get_job(job_key)
    if not job:
        return {"error": "job not found"}
    resume = store.get_resume()
    if not resume or not resume.get("text"):
        return {"error": "no resume uploaded"}

    method = apply_method or job.get("apply_method")
    if method is not None and method != "email":
        return {"error": "this job is not flagged apply_method:email in companies.yaml"}
    contact_email = (contact_email or job.get("contact_email") or "").strip()
    if not contact_email:
        return {"error": "this job has no contact_email; check companies.yaml apply_method"}

    tailored = ""
    vid = job.get("applied_version_id")
    if vid:
        v = store.get_version(int(vid))
        if v:
            tailored = v.get("tailored_text") or ""

    return writing_email.draft_from_resume_text(
        job, resume_text=resume["text"], contact_email=contact_email,
        tailored_text=tailored, style_sample=store.get_meta("user_email_style") or "",
        answer_pack=store.get_answer_pack())
