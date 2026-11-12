"""Application and follow-up emails (docs/WRITING_GUIDE.md).

    generate_email(job, *, kind="application"|"follow_up", facts=None, answers=None,
                   company=None, today=None) -> Draft

Short (≤ 120 / ≤ 90 words), a subject line that names the role, a signature
built from answers.yaml, the same fact-citation grounding as cover letters.
A follow-up opens with the submitted date from the tracker (`submitted_at`)
and warns when it is earlier than the guide's one-week wait. Never sends.

`draft_from_resume_text()` is the v1 path kept for POST /api/jobs/{key}/draft-email
(jobcopilot/email_draft.py): grounded against the uploaded résumé text.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from .. import llm
from ..grounding import check_grounding
from . import common, style
from .common import Draft

log = logging.getLogger("jobcopilot.writing.email")

KINDS = ("application", "follow_up")

SYSTEM = (
    "You draft short job-application emails from a fixed list of applicant facts. "
    "Rules: every statement about the applicant comes from <applicant_facts> and "
    "cites its fact ids; never add a skill, number, employer, title or result the "
    "cited facts don't state. Company statements may only use the JD or company "
    "record. Plain and direct; no clichés ('I am writing to express', 'passionate "
    "about', 'perfect fit'); no greeting, no sign-off, no placeholders. "
    + style.UNTRUSTED_RULE + common.ID_RULE +
    " Return ONLY JSON: {\"paragraphs\": [{\"text\": str, \"fact_ids\": [str]}], "
    "\"subject\": str}"
)

TASKS = {
    "application": (
        "TASK: an application email body of 3-5 sentences, at most {max_words} words, "
        "in 2 paragraphs: (1) the role you are applying for and the single most "
        "relevant fact; (2) one more fact tied to a JD must-have, then a one-line "
        "close saying the résumé is attached. \"subject\" names the role."),
    "follow_up": (
        "TASK: the body of a polite follow-up on an application, at most {max_words} "
        "words, ONE paragraph of 2-3 sentences: restate interest with one relevant "
        "fact, then ask whether there is an update or anything else they need. Do "
        "not mention the application date (it is added for you). \"subject\" may be empty."),
}


def _fmt_date(ts: Any) -> tuple[str, Optional[dt.date]]:
    try:
        d = dt.datetime.fromtimestamp(float(ts)).date()
    except (TypeError, ValueError, OSError):
        return "", None
    return f"{d.day} {d.strftime('%B %Y')}", d


def signature(data: dict, cfg: dict, warnings: list[str]) -> str:
    """Sign-off + the configured fields (name, phone, links) from answers.yaml."""
    vals = common.sign_values(data)
    block = common.signoff_block(data, warnings)
    lines = block.splitlines()
    want = cfg["email"].get("signature_fields") or []
    extra = []
    if "phone" in want and vals["phone"] and vals["phone"] not in block:
        extra.append(vals["phone"])
    if "links" in want:
        extra += [vals[k] for k in ("linkedin", "github", "portfolio")
                  if vals[k] and vals[k] not in block]
    if vals["name"] and vals["name"] not in block:
        lines.append(vals["name"])
    return "\n".join(lines + extra)


def generate_email(job: dict, *, kind: str = "application", facts: Any = None,
                   answers: Any = None, company: Any = None,
                   today: Optional[dt.date] = None) -> Draft:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    dkind = f"email_{kind}"
    usable = common.resolve_facts(facts)
    if not usable:
        return common.refused(dkind)
    data = common.answers_data(answers)
    cfg = style.settings()
    ecfg = cfg["email"][kind]
    max_words = int(ecfg["max_words"])
    rec = common.company_record(job, company)
    title = job.get("title") or "the role"
    company_name = job.get("company") or rec.get("name") or ""
    pre_warnings: list[str] = []
    opener = ""
    if kind == "follow_up":
        when, d = _fmt_date(job.get("submitted_at"))
        if not d:
            pre_warnings.append("no submitted date in the tracker; mark the job Submitted first "
                                "or edit the opening line.")
            opener = f"I recently applied for the {title} role and wanted to follow up."
        else:
            opener = f"I applied for the {title} role on {when} and wanted to follow up."
            days = ((today or dt.date.today()) - d).days
            wait = int(ecfg.get("min_days_after_submit", 7))
            if days < wait:
                pre_warnings.append(f"only {days} day(s) since you applied; the guide suggests "
                                    f"waiting {wait}-14 days before a follow-up.")
    user = (common.facts_block(usable, job, limit=12) + "\n\n" + common.job_block(job, rec)
            + "\n\n" + TASKS[kind].format(max_words=max_words))
    extra = opener

    def build(parsed):
        paras, subject, problems = common.parse_paragraphs(parsed)
        if problems:
            return None, problems
        if opener:
            paras = [{"text": opener, "fact_ids": []}] + paras
        built, issues = common.assemble(paras, usable, job=job, rec=rec, data=data,
                                        max_words=max_words, extra_allowed=extra)
        built["subject"] = subject
        return built, issues

    built, issues, n = common.best_attempt(SYSTEM, user, build,
                                           attempts=int(cfg["max_attempts"]), max_tokens=900)
    warnings = pre_warnings + list(issues)
    if built["removed"]:
        warnings.append("removed cliché sentence(s): " + ", ".join(dict.fromkeys(built["removed"])))
    sig = signature(data, cfg, warnings)
    name = common.sign_values(data)["name"]
    if kind == "application":
        subject = built["subject"]
        if not subject or len(subject) > 80 or style.find_leaks(subject):
            subject = f"Application: {title}" + (f" at {company_name}" if company_name else "")
    else:
        subject = f"Following up: {title} application"
    if name and name not in subject:
        subject = f"{subject} — {name}"
    body = "\n\n".join(p["text"] for p in built["paragraphs"])
    greeting = f"Hello {company_name} team," if company_name else "Hello,"
    text = f"{greeting}\n\n{body}\n\n{sig}".strip()
    status = "ok" if not issues and built["grounding"].get("clean") else "needs_review"
    log.info("email kind=%s job=%s status=%s words=%d attempts=%d",
             kind, job.get("key"), status, built["words"], n)
    return Draft(kind=dkind, status=status, text=text, subject=subject,
                 to=str(job.get("contact_email") or ""), paragraphs=built["paragraphs"],
                 grounding=built["grounding"], warnings=warnings, word_count=built["words"],
                 fact_ids=built["grounding"].get("cited_fact_ids") or [], attempts=n)


# --------------------------------------------------------------------------- v1 path

LEGACY_SYSTEM = (
    "You write short, specific job-application emails. Style rules: "
    "4–6 sentences. No 'I am writing to express my interest'. No buzzword "
    "stacking. One concrete specific reason for THIS company tied to the role. "
    "Plain sign-off. Do not invent any project, employer, metric, or skill that "
    "is not already in the resume. If the role asks for something the resume "
    "cannot honestly support, omit it. "
    + style.UNTRUSTED_RULE.replace("<untrusted_company_record>", "the company name") +
    " Return ONLY valid JSON, no prose, no code fences."
)

LEGACY_PROMPT = """ORIGINAL RESUME (verbatim):
\"\"\"
{resume}
\"\"\"

JOB TITLE: {title}
COMPANY: {company}
JOB DESCRIPTION:
{description}

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


def draft_from_resume_text(job: dict, *, resume_text: str, contact_email: str,
                           tailored_text: str = "", style_sample: str = "",
                           answer_pack: Optional[list[dict]] = None) -> dict:
    """v1 email draft grounded against the uploaded résumé text.
    Returns {to, subject, body, grounding, warnings} or {error}."""
    ans_text = "\n".join(f"- {a['q']}: {a['a']}" for a in answer_pack or [])
    user_prompt = LEGACY_PROMPT.format(
        resume=resume_text,
        title=job.get("title", ""),
        company=job.get("company", ""),
        description=style.fence((job.get("description_text") or "")[:4000],
                                "untrusted_job_description"),
        tailored_block=_block("TAILORED RESUME (use this voice/phrasing)", tailored_text),
        style_block=_block("USER EMAIL STYLE SAMPLE (mimic the voice, not the content)",
                           style_sample),
        answers_block=_block("ANSWER PACK (use only if directly relevant)", ans_text),
    )
    try:
        parsed, _ = llm.complete_json(LEGACY_SYSTEM, user_prompt, temperature=0.2, max_tokens=600)
    except Exception as exc:
        return {"error": str(exc)}
    if not isinstance(parsed, dict):
        return {"error": "model returned JSON that is not an object"}
    subject = (parsed.get("subject") or "").strip()
    body = (parsed.get("body") or "").strip()
    if not body:
        # No raw echo: the reply may quote résumé text.
        return {"error": "empty body from model"}
    grounding = check_grounding(original_resume=resume_text, tailored_text=body,
                                job_missing_keywords=job.get("missing_keywords") or [])
    warnings = [f"cliché: {c}" for c in style.find_cliches(body)]
    warnings += [f"leaked/template text: {x}" for x in style.find_leaks(body)]
    # Subject lines often carry the applicant's name — log only its length.
    log.info("email(v1) key=%s grounding_clean=%s subject_len=%d",
             job.get("key"), grounding["clean"], len(subject))
    return {"to": contact_email, "subject": subject, "body": body,
            "grounding": grounding, "warnings": warnings}
