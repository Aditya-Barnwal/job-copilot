"""Cover letters: short, specific, grounded in confirmed facts (docs/WRITING_GUIDE.md).

    generate_cover_letter(job, *, facts=None, answers=None, base=None, company=None,
                          style_sample=None) -> Draft

Shape: 3 or 4 short paragraphs, the model's choice: a concrete opening tied to
something the JD names (company claims come from the JD or company record
only), one or two evidence paragraphs that map JD must-haves to confirmed
facts (each citing fact ids), and a close naming one thing to talk about; the
sign-off comes from answers.yaml `writing.signoff`. Body ≤ 180 words by default
(config/writing.yaml `cover_letter.max_words`). The voice is plain and varied;
AI tells (style.find_ai_tells) in the model's reply are a retry issue and, if
they survive, a warning. The applicant's writing sample, when there is one, is
a fenced voice reference only and never evidence.

Fail closed: no confirmed facts → status `refused` and the model is never
called. Any grounding flag (incl. unsupported capability claims), leak,
over-length body, markdown / links / look-alike characters, an evidence
paragraph mixing experiences or too few cited facts → `needs_review`, with
each offending sentence quoted in the warnings (retries are told only the
paragraph / sentence numbers and the kind of problem). Clichés are deleted
sentence by sentence. The subject line is built in code from the title and
company; the model never writes it.

Word limit: an over-long draft is retried once (fresh conversation, told how
many words over); if the best draft is still over, whole sentences are trimmed
deterministically, least-evidenced first (`common.trim_to_limit`), and the
trim is reported in the warnings.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import common, style
from .common import Draft

log = logging.getLogger("jobcopilot.writing.cover_letter")

SYSTEM = (
    "You draft short software-engineering cover letters from a fixed list of "
    "applicant facts. They should read like a thoughtful engineer wrote them to "
    "another engineer, not like a template. Rules:\n"
    "1. Every statement about the applicant must come from <applicant_facts>; "
    "cite the ids you used in that paragraph's fact_ids. Never add a skill, "
    "number, employer, title, project or result that the cited facts don't state. "
    "If the role needs something the facts don't show, leave it out.\n"
    "2. Statements about the company or role may only use the JD or company record. "
    "Never invent company facts (funding, customers, size, news, mission), and no "
    "generic praise of the company.\n"
    "3. Shape: 3 or 4 short paragraphs, your choice, one idea per paragraph. Vary the "
    "shape to suit the facts; don't follow a formula.\n"
    "   a. Opening: start with a specific, concrete line about something the JD names (a "
    "product, a problem, a piece of work). Not a formula, and never 'I am applying for' or "
    "'I am writing'. A sentence that uses the JD must be about the company or the role only: "
    "name the company or refer to the role, the team or the posting ('your team', 'the "
    "posting'), with no I / my / me in it. A sentence about the applicant can never use the "
    "JD. Make clear which role this is somewhere in the opening paragraph.\n"
    "   b. Evidence, 1 or 2 paragraphs: each takes ONE JD must-have and one or two cited "
    "facts, and says what the applicant built or changed and why it mattered, in plain "
    "words, using only what the facts state. Two evidence paragraphs cover different "
    "must-haves and different facts.\n"
    "   c. Close: name ONE concrete thing the applicant would like to talk about, then a "
    "short thanks. The topic comes from a cited fact (cite its id) or from the JD (then "
    "write that sentence about the company or role with no I / my / me, e.g. 'How your "
    "team plans to ... would be a good thing to talk through.'). No new claims about the "
    "applicant's skills or experience, and no summary of the letter.\n"
    "   No salutation, no sign-off, no placeholders.\n"
    "4. State facts; never interpret them. No bridging sentences such as 'this "
    "demonstrates', 'showcasing', 'this experience shows', 'reinforcing my'. Never "
    "claim experience, expertise, ownership, end-to-end, scale or production use "
    "unless a cited fact states exactly that. A detail belongs to the project or "
    "employer whose fact states it; never attach it to another one.\n"
    "5. Voice. " + style.VOICE_RULE + " No clichés (e.g. 'I am writing to express', "
    "'passionate about', 'team player', 'perfect fit', 'proven track record'). "
    "Plain ASCII hyphens only; no em-dashes or en-dashes.\n"
    "6. Copy numbers, metrics and names exactly as the facts write them. Never write a "
    "quantity in words (five, dozens, millions, half, twice) and never add a scope claim "
    "(team of, sole, single-handedly, independently, entire).\n"
    "7. Each evidence paragraph is about exactly ONE employer or project and cites only its "
    "facts; name that employer or project in the paragraph. Never write the target company "
    "or role title as if it were the applicant's past employer or title.\n"
    "8. Plain text only: no markdown, no links, no e-mail addresses.\n"
    + style.UNTRUSTED_RULE + style.SAMPLE_RULE + common.ID_RULE +
    "\nReturn ONLY JSON: {\"paragraphs\": [{\"text\": str, \"fact_ids\": [str]}]}"
)

PROMPT = """{facts}

{job}{sample}

TASK: write the cover letter body in {lo}-{hi} words. HARD LIMIT: {max_words} words
for the whole body; count them before you answer. 3 or 4 paragraphs: an opening, one
or two evidence paragraphs, and a close. Evidence paragraphs must cite at least one
fact id each.
"""


def generate_cover_letter(job: dict, *, facts: Any = None, answers: Any = None,
                          base: Optional[str] = None, company: Any = None,
                          style_sample: Optional[str] = None) -> Draft:
    usable = common.resolve_facts(facts, base)
    if not usable:
        return common.refused("cover_letter")
    data = common.answers_data(answers)
    cfg = style.settings()
    cl = cfg["cover_letter"]
    max_words = int(cl["max_words"])
    lo, hi = cl.get("target_words") or [int(max_words * 0.7), max_words - 10]
    rec = common.company_record(job, company)
    sample = common.writing_sample(data, style_sample)
    user = PROMPT.format(facts=common.facts_block(usable, job), job=common.job_block(job, rec),
                         sample=common.sample_block(sample), lo=lo, hi=hi, max_words=max_words)
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
        n_paras = len([p for p in built["paragraphs"] if not p.get("fixed")])
        if built["paragraphs"] and not 3 <= n_paras <= 4:
            issues.append(common.Issue(f"{n_paras} paragraph(s); the letter should have 3 or 4",
                                       model=f"{n_paras} paragraph(s); write 3 or 4", soft=True))
        issues += common.tell_issues(common.raw_texts(parsed))
        return built, issues

    built, issues, n = common.best_attempt(SYSTEM, user, build,
                                           attempts=int(cfg["max_attempts"]))
    trimmed: list[str] = []
    if built["words"] > max_words and built["paragraphs"]:
        paras, trimmed = common.trim_to_limit(built["paragraphs"], built["grounding"], usable,
                                              max_words)
        if trimmed:
            removed = built["removed"]
            built, issues = build({"paragraphs": paras})
            built["removed"] = removed + built["removed"]
    warnings = list(issues)
    if trimmed:
        warnings.append(f"trimmed {len(trimmed)} sentence(s) to fit {max_words} words: "
                        + " | ".join(f'"{t}"' for t in trimmed))
    if built["words"] > max_words:
        warnings.append(f"still over the {max_words}-word limit ({built['words']} words): "
                        "no more sentences could be removed safely; edit it by hand")
    if built.get("normalised"):
        warnings.append("normalised metric(s) to the facts' wording: " + ", ".join(built["normalised"]))
    if built["removed"]:
        warnings.append("removed cliché sentence(s): " + ", ".join(dict.fromkeys(built["removed"])))
    signoff = common.signoff_block(data, warnings)
    body = "\n\n".join(p["text"] for p in built["paragraphs"])
    text = f"{cl['salutation']}\n\n{body}\n\n{signoff}".strip()
    subject = common.application_subject(job, rec)          # built in code, never by the model
    status = ("ok" if not common.hard_issues(issues) and built["grounding"].get("clean")
              else "needs_review")                      # AI tells alone: warnings, not a status
    log.info("cover_letter job=%s status=%s words=%d attempts=%d cited=%d sample=%s",
             job.get("key"), status, built["words"], n,
             len(built["grounding"].get("cited_fact_ids") or []), bool(sample))
    return Draft(kind="cover_letter", status=status, text=text, subject=subject,
                 paragraphs=built["paragraphs"], grounding=built["grounding"],
                 warnings=warnings, word_count=built["words"],
                 fact_ids=built["grounding"].get("cited_fact_ids") or [], attempts=n)
