"""Cover letters: short, specific, grounded in confirmed facts (docs/WRITING_GUIDE.md).

    generate_cover_letter(job, *, facts=None, answers=None, base=None, company=None) -> Draft

Structure: a specific opening tied to the role/company (company claims come
from the JD or company record only), two evidence paragraphs that map two or
three JD must-haves to confirmed facts (each citing fact ids), and a short
close; the sign-off comes from answers.yaml `writing.signoff`. Body ≤ 180
words by default (config/writing.yaml `cover_letter.max_words`).

Fail closed: no confirmed facts → status `refused` and the model is never
called. Any grounding flag, leak, over-length body or too few cited facts →
`needs_review` with warnings. Clichés are deleted sentence by sentence.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import common, style
from .common import Draft

log = logging.getLogger("jobcopilot.writing.cover_letter")

SYSTEM = (
    "You draft short software-engineering cover letters from a fixed list of "
    "applicant facts. Rules:\n"
    "1. Every statement about the applicant must come from <applicant_facts>; "
    "cite the ids you used in that paragraph's fact_ids. Never add a skill, "
    "number, employer, title, project or result that the cited facts don't state. "
    "If the role needs something the facts don't show, leave it out.\n"
    "2. Statements about the company or role may only use the JD or company record. "
    "Never invent company facts (funding, customers, size, news, mission).\n"
    "3. Structure: opening (1-2 sentences naming the role and one specific thing "
    "from the JD that the applicant's facts connect to); two evidence paragraphs, "
    "each mapping one or two JD must-haves to one concrete fact; closing (1-2 "
    "sentences: what you'd like to discuss, thanks). No salutation, no sign-off, "
    "no placeholders.\n"
    "4. Plain, direct, specific. No clichés (e.g. 'I am writing to express', "
    "'passionate about', 'team player', 'perfect fit', 'proven track record').\n"
    "5. Keep numbers exactly as the facts write them.\n"
    + style.UNTRUSTED_RULE + common.ID_RULE +
    "\nReturn ONLY JSON: {\"paragraphs\": [{\"text\": str, \"fact_ids\": [str]}], "
    "\"subject\": str}"
)

PROMPT = """{facts}

{job}

TASK: write the cover letter body in {lo}-{hi} words (hard limit {max_words}), as
4 paragraphs: opening, evidence, evidence, closing. Evidence paragraphs must
cite at least one fact id each. "subject" is a short email subject naming the role.
"""


def generate_cover_letter(job: dict, *, facts: Any = None, answers: Any = None,
                          base: Optional[str] = None, company: Any = None) -> Draft:
    usable = common.resolve_facts(facts, base)
    if not usable:
        return common.refused("cover_letter")
    data = common.answers_data(answers)
    cfg = style.settings()
    cl = cfg["cover_letter"]
    max_words = int(cl["max_words"])
    lo, hi = cl.get("target_words") or [int(max_words * 0.7), max_words - 10]
    rec = common.company_record(job, company)
    user = PROMPT.format(facts=common.facts_block(usable, job), job=common.job_block(job, rec),
                         lo=lo, hi=hi, max_words=max_words)
    min_facts = int(cl.get("min_facts_cited", 2))

    def build(parsed):
        paras, subject, problems = common.parse_paragraphs(parsed)
        if problems:
            return None, problems
        built, issues = common.assemble(paras, usable, job=job, rec=rec, data=data,
                                        max_words=max_words)
        cited = built["grounding"].get("cited_fact_ids") or []
        if len(cited) < min_facts:
            issues.append(f"cites {len(cited)} fact(s); at least {min_facts} expected")
        built["subject"] = subject
        return built, issues

    built, issues, n = common.best_attempt(SYSTEM, user, build,
                                           attempts=int(cfg["max_attempts"]))
    warnings = list(issues)
    if built["removed"]:
        warnings.append("removed cliché sentence(s): " + ", ".join(dict.fromkeys(built["removed"])))
    signoff = common.signoff_block(data, warnings)
    body = "\n\n".join(p["text"] for p in built["paragraphs"])
    text = f"{cl['salutation']}\n\n{body}\n\n{signoff}".strip()
    title = job.get("title") or "the role"
    subject = built["subject"] or f"Application: {title}"
    if len(subject) > 90 or style.find_leaks(subject):
        subject = f"Application: {title}"
    status = "ok" if not issues and built["grounding"].get("clean") else "needs_review"
    log.info("cover_letter job=%s status=%s words=%d attempts=%d cited=%d",
             job.get("key"), status, built["words"], n,
             len(built["grounding"].get("cited_fact_ids") or []))
    return Draft(kind="cover_letter", status=status, text=text, subject=subject,
                 paragraphs=built["paragraphs"], grounding=built["grounding"],
                 warnings=warnings, word_count=built["words"],
                 fact_ids=built["grounding"].get("cited_fact_ids") or [], attempts=n)
