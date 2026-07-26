"""Email drafter for `apply_method: email` companies (e.g. BalkanID).

Produces a *strong specific first draft* the user will personalize in ~30
seconds — not "looks 100% human." Never auto-sends. Grounding check applies
to the email body too: any unsupported claim or fabricated number is flagged
and Approve is blocked until the user confirms.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import llm, store
from .grounding import check_grounding


log = logging.getLogger("jobcopilot.email")


SYSTEM = (
    "You write short, specific job-application emails. Style rules: "
    "4–6 sentences. No 'I am writing to express my interest'. No buzzword "
    "stacking. One concrete specific reason for THIS company tied to the role. "
    "Plain sign-off. Do not invent any project, employer, metric, or skill that "
    "is not already in the resume. If the role asks for something the resume "
    "cannot honestly support, omit it. "
    "Return ONLY valid JSON, no prose, no code fences."
)


PROMPT = """ORIGINAL RESUME (verbatim):
\"\"\"
{resume}
\"\"\"

JOB TITLE: {title}
COMPANY: {company}
JOB DESCRIPTION:
\"\"\"
{description}
\"\"\"

{tailored_block}
{style_block}
{answers_block}

TASK: Draft a brief email body (4–6 sentences) and a concise subject line.
Tone: direct, specific, plain. Open with the role + one sentence of relevant
context from the resume. Add ONE specific reason for this company tied to the
role. Close with availability + sign-off.

Return JSON with exactly these keys:
{{
  "subject": "<subject line>",
  "body": "<email body>"
}}
"""


def _block(label: str, value: Optional[str]) -> str:
    if not value:
        return ""
    return f"\n{label}:\n\"\"\"\n{value}\n\"\"\"\n"


def draft(job_key: str) -> dict:
    job = store.get_job(job_key)
    if not job:
        return {"error": "job not found"}
    resume = store.get_resume()
    if not resume or not resume.get("text"):
        return {"error": "no resume uploaded"}

    contact_email = job.get("contact_email") or ""
    if not contact_email:
        return {"error": "this job has no contact_email; check companies.yaml apply_method"}

    # Pick tailored version if linked, else None
    tailored_block = ""
    vid = job.get("applied_version_id")
    if vid:
        v = store.get_version(int(vid))
        if v:
            tailored_block = _block("TAILORED RESUME (use this voice/phrasing)",
                                    v.get("tailored_text"))

    style = store.get_meta("user_email_style") or ""
    style_block = _block("USER EMAIL STYLE SAMPLE (mimic the voice, not the content)", style)

    answers = store.get_answer_pack()
    ans_text = "\n".join(f"- {a['q']}: {a['a']}" for a in answers) if answers else ""
    answers_block = _block("ANSWER PACK (use only if directly relevant)", ans_text)

    user_prompt = PROMPT.format(
        resume=resume["text"],
        title=job.get("title", ""),
        company=job.get("company", ""),
        description=(job.get("description_text") or "")[:4000],
        tailored_block=tailored_block,
        style_block=style_block,
        answers_block=answers_block,
    )

    try:
        parsed, _ = llm.complete_json(SYSTEM, user_prompt,
                                      temperature=0.2, max_tokens=600)
    except Exception as exc:
        return {"error": str(exc)}

    subject = (parsed.get("subject") or "").strip()
    body = (parsed.get("body") or "").strip()
    if not body:
        return {"error": "empty body from model", "raw": raw[:500]}

    # Grounding applies to the email body too. A cover email inventing a
    # project is worse than a résumé doing it.
    grounding = check_grounding(
        original_resume=resume["text"],
        tailored_text=body,
        job_missing_keywords=job.get("missing_keywords") or [],
    )

    log.info("email key=%s grounding_clean=%s subject=%r", job_key, grounding["clean"], subject)

    return {
        "to": contact_email,
        "subject": subject,
        "body": body,
        "grounding": grounding,
    }
