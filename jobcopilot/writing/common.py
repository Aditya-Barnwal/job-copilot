"""Shared machinery for generated writing: inputs, prompt blocks, grounding,
fresh-conversation retries and the Draft result.

Grounding (fail closed): the model returns paragraphs that cite fact ids. Each
sentence is checked with grounding v3 `check_lines` against
  * its paragraph's cited facts (a cited bullet brings its role/project header),
  * `_role`: the target company name, role title and a few function words,
  * `_jd`:   the job description + company record — ONLY for sentences that
             aren't about the applicant (no I / my / me, and no name of one of
             the applicant's employers / projects). A company claim may come
             from the JD; a claim about the applicant may not.
Applicant sentences also get the capability-claim check
(`grounding.capability_claims`): "experience with X", "scaled … to
production", "ownership", "showcasing Y", "this demonstrates …" need the
claimed thing in the CITED facts (never the JD), and "At <project> … showcasing
Y" needs Y in that project's own facts.
A skill, number, ownership/title claim or proper noun that none of those
supports flags the sentence; a cited id that isn't a usable (confirmed) fact
flags it too. Any flag → status `needs_review`. Nothing is sent anywhere: the
draft is shown to the user before use.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from .. import facts as F
from .. import grounding, llm
from . import style

log = logging.getLogger("jobcopilot.writing")

REFUSE_NO_FACTS = ("No confirmed résumé facts yet — confirm your facts first "
                   "(python -m jobcopilot.facts review), then generate again.")
PSEUDO_IDS = ("_role", "_jd")
# Appended to every writing prompt: fact ids are slugs, not names.
ID_RULE = (" Fact ids like [proj.x] are references only: never write an id, or a name "
           "made from one, in the text; use the names exactly as the fact text writes them.")
_FUNCTION_WORDS = "I I'm I've I'd I'll Thank Thanks Hello Hi Dear Hiring Manager Team"
_JD_LIMIT = 6000


class WritingError(RuntimeError):
    """The model could not produce a usable draft (all attempts failed)."""


@dataclass
class Draft:
    kind: str                                   # cover_letter | email_application | email_follow_up | answer
    status: str                                 # ok | needs_review | refused
    text: str = ""
    subject: str = ""
    to: str = ""
    paragraphs: list[dict] = field(default_factory=list)
    grounding: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    word_count: int = 0
    fact_ids: list[str] = field(default_factory=list)
    attempts: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def refused(kind: str, reason: str = REFUSE_NO_FACTS) -> Draft:
    return Draft(kind=kind, status="refused", warnings=[reason],
                 grounding={"clean": False, "lines": [], "flags": {}})


# --------------------------------------------------------------------------- inputs

def resolve_facts(facts: Any = None, base: Optional[str] = None) -> list:
    """Usable (confirmed) facts from a ResumeFacts, a list of Facts, or None
    (= load profile/resume_facts.yaml). Anything unloadable → [] (refuse)."""
    if facts is None:
        try:
            facts = F.load()
        except F.FactsError:
            return []
    if isinstance(facts, F.ResumeFacts):
        try:
            return F.usable_facts(facts, base)
        except Exception as exc:  # noqa: BLE001 — fail closed
            log.warning("usable_facts failed: %s", type(exc).__name__)
            return []
    return [f for f in (facts or []) if getattr(f, "status", "") == "confirmed"
            and not getattr(f, "malformed", False)]


def answers_data(answers: Any = None) -> dict:
    """The answers mapping from an AnswersBank, a dict, or None (= load)."""
    from .. import answers as A
    if answers is None:
        try:
            answers = A.load()
        except A.AnswersError:
            return {}
    return answers.data if isinstance(answers, A.AnswersBank) else (answers or {})


def company_record(job: dict, company: Any = None) -> dict:
    if isinstance(company, dict):
        return company
    name = company if isinstance(company, str) else (job or {}).get("company")
    if not name:
        return {}
    try:
        from .. import companies
        return companies.get_company(name) or {"name": name}
    except Exception:  # noqa: BLE001 — no DB / no table: just the name
        return {"name": name}


_COMPANY_FIELDS = ("name", "sector", "funding_stage", "yc_batch", "one_liner",
                   "description", "tagline", "hq", "location")


def company_text(rec: dict) -> str:
    parts = []
    for k in _COMPANY_FIELDS:
        v = rec.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            parts.append(f"{k}: {v}")
    return "\n".join(parts)


def sign_values(data: dict) -> dict[str, str]:
    p = data.get("personal") or {}
    links = p.get("links") or {}
    return {"name": str(p.get("full_name") or "").strip(),
            "first_name": str(p.get("first_name") or "").strip(),
            "phone": str(p.get("phone") or "").strip(),
            "email": str(p.get("email") or "").strip(),
            **{k: str(links.get(k) or "").strip() for k in ("linkedin", "github", "portfolio")}}


def signoff_block(data: dict, warnings: list[str]) -> str:
    """writing.signoff with {name}/{phone}/{email}/{linkedin}/... filled in.
    A one-line sign-off ("Best regards") gets the name on the next line."""
    vals = sign_values(data)
    raw = str(((data.get("writing") or {}).get("signoff")) or "").strip()
    if not raw:
        warnings.append("writing.signoff is empty in answers.yaml; used the default.")
        raw = style.settings()["signoff_default"]
    out = re.sub(r"\{([a-z_]+)\}", lambda m: vals.get(m.group(1), m.group(0)), raw)
    lines = [ln.rstrip() for ln in out.splitlines()]
    # a placeholder whose value is empty leaves a dangling separator
    lines = [re.sub(r"^\s*[|·•]\s*|\s*[|·•]\s*$", "", ln) for ln in lines]
    lines = [ln for ln in lines if ln.strip()]
    if re.search(r"\{[a-z_]+\}", "\n".join(lines)):
        warnings.append("writing.signoff has an unknown {placeholder}.")
    if len(lines) == 1 and vals["name"] and vals["name"] not in lines[0]:
        first = lines[0].rstrip(",")
        lines = [first + ",", vals["name"]]
    return "\n".join(lines)


# --------------------------------------------------------------------------- prompt blocks

def _relevance(f, terms: list[str]) -> int:
    text = f.text.lower()
    return sum(1 for t in terms if t and grounding._has_term(text, t))


def facts_block(usable: list, job: dict, *, limit: int = 24) -> str:
    """<applicant_facts> lines `[id] text`; bullets ranked by overlap with the
    JD's keywords. Contact details are never included."""
    terms = [str(t).lower() for t in (job.get("required_keywords") or [])
             + (job.get("matched_skills") or []) if isinstance(t, str)]
    by_id = {f.id: f for f in usable}
    headers = [f for f in usable if f.kind != "bullet" and f.section != "skills"]
    bullets = sorted((f for f in usable if f.kind == "bullet"),
                     key=lambda f: -_relevance(f, terms))[:limit]
    skills = [f for f in usable if f.section == "skills"]
    rows = [f"[{f.id}] {f.text}" for f in headers]
    for b in bullets:
        head = by_id.get(b.parent)
        rows.append(f"[{b.id}] (part of {head.id if head else b.parent}) {b.text}")
    if skills:
        rows.append("Skills: " + "; ".join(f"[{s.id}] {s.text}" for s in skills))
    return "<applicant_facts>\n" + "\n".join(rows) + "\n</applicant_facts>"


def job_block(job: dict, rec: dict) -> str:
    title = job.get("title") or ""
    company = job.get("company") or rec.get("name") or ""
    jd = (job.get("description_text") or "")[:_JD_LIMIT]
    must = [k for k in (job.get("required_keywords") or []) if isinstance(k, str)][:15]
    out = [f"ROLE: {title}", f"COMPANY: {company}"]
    if must:
        out.append("JD KEYWORDS: " + ", ".join(must))
    out.append(style.fence(jd, "untrusted_job_description"))
    ct = company_text(rec)
    if ct:
        out.append(style.fence(ct, "untrusted_company_record"))
    return "\n".join(out)


# --------------------------------------------------------------------------- grounding

def _fallback_check_lines(lines: list[dict], fmap: dict) -> dict:
    """Used only if grounding v3 isn't importable: the v2 checks per line."""
    verdicts, flags = [], {}
    for i, ln in enumerate(lines):
        ids = ln["fact_ids"]
        unknown = [x for x in ids if x not in fmap]
        ev = "\n".join(getattr(fmap[x], "text", fmap[x]) if not isinstance(fmap[x], str) else fmap[x]
                       for x in ids if x in fmap)
        g = grounding.check_grounding(ev, ln["text"], [])
        v = {"index": i, "text": ln["text"], "fact_ids": ids, "unknown_facts": unknown,
             "unsupported_skills": g["unsupported_skills"], "new_numbers": g["new_numbers"],
             "inflated_claims": g["inflated_claims"], "new_proper_nouns": []}
        v["offending"] = [t for k in ("unknown_facts", "unsupported_skills", "new_numbers",
                                      "inflated_claims") for t in v[k]]
        v["supported"] = not v["offending"]
        for k in ("unknown_facts", "unsupported_skills", "new_numbers", "inflated_claims"):
            flags.setdefault(k, []).extend(t for t in v[k] if t not in flags.get(k, []))
        verdicts.append(v)
    bad = [v["index"] for v in verdicts if not v["supported"]]
    return {"clean": not bad, "lines": verdicts, "flags": flags, "unsupported_lines": bad}


_HYPHENS_RE = re.compile(r"[\u2010-\u2015\u2212]")


def ascii_hyphens(text: str) -> str:
    """U+2010–2015 and U+2212 → '-' (an em-dash flourish becomes ' - ')."""
    return _HYPHENS_RE.sub("-", text or "")


def fold(text: str) -> str:
    """ASCII-fold for checking only ("résumé" → "resume"): the checker splits
    words on non-ASCII letters, so "résumé" would otherwise read as the skill R."""
    text = re.sub(r"[\u2010-\u2015\u2212\u00ad]", "-", text or "")     # hyphens / dashes
    text = re.sub(r"[\u2018\u2019]", "'", text).replace("\u00a0", " ")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def clean_ids(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for x in raw:
        if isinstance(x, str) and x.strip() and x.strip() not in PSEUDO_IDS and x.strip() not in out:
            out.append(x.strip())
    return out


def _applicant_names(usable: list) -> list[str]:
    """Names of the applicant's employers / projects / hackathons (header facts)."""
    out: list[str] = []
    for f in usable:
        if getattr(f, "kind", "") == "bullet":
            continue
        for a in ("name", "company"):
            n = str(getattr(f, a, "") or "").strip()
            if len(n) >= 3 and n not in out:
                out.append(n)
    return out


def about_applicant(sentence: str, names: list[str]) -> bool:
    """First person, or it names one of the applicant's employers / projects."""
    if style.is_first_person(sentence):
        return True
    s = fold(sentence)
    return any(re.search(r"(?<![A-Za-z0-9])" + re.escape(fold(n)) + r"(?![A-Za-z0-9])", s, re.I)
               for n in names)


def ground_paragraphs(paragraphs: list[dict], usable: list, *, job: dict, rec: dict,
                      extra_allowed: str = "") -> dict:
    by_id = {f.id: f for f in usable}
    names = _applicant_names(usable)
    company = job.get("company") or rec.get("name") or ""
    role = " ".join([company, job.get("title") or "", _FUNCTION_WORDS, extra_allowed])
    fmap: dict[str, Any] = dict(by_id)
    fmap = {k: v for k, v in fmap.items()}
    fmap["_role"] = fold(role)
    fmap["_jd"] = fold("\n".join([(job.get("description_text") or "")[:_JD_LIMIT],
                                  company_text(rec), role]))
    lines: list[dict] = []
    for pi, p in enumerate(paragraphs):
        ids = clean_ids(p.get("fact_ids"))
        for x in list(ids):                     # a bullet brings its header
            parent = getattr(by_id.get(x), "parent", "")
            if parent and parent in by_id and parent not in ids:
                ids.append(parent)
        for s in style.split_sentences(p.get("text", "")):
            mine = about_applicant(s, names)
            ctx = ["_role"] if mine else ["_role", "_jd"]
            lines.append({"text": fold(s), "fact_ids": ids + ctx, "paragraph": pi, "shown": s,
                          "check_claims": mine})
    check = getattr(grounding, "check_lines", None)
    result = check(lines, fmap) if check else _fallback_check_lines(lines, fmap)
    for v, ln in zip(result.get("lines", []), lines):
        v["paragraph"] = ln["paragraph"]
        v["text"] = ln["shown"]
        v["fact_ids"] = [x for x in v.get("fact_ids", []) if x not in PSEUDO_IDS]
    result["cited_fact_ids"] = sorted({x for ln in lines for x in ln["fact_ids"]
                                       if x in by_id})
    result["engine"] = "v3" if check else "v2-fallback"
    return result


def parse_paragraphs(parsed: Any) -> tuple[list[dict], str, list[str]]:
    """(paragraphs, subject, problems) from {paragraphs: [{text, fact_ids}], subject}."""
    if not isinstance(parsed, dict):
        return [], "", ["model reply is not a JSON object"]
    raw = parsed.get("paragraphs")
    if not isinstance(raw, list) or not raw:
        return [], "", ["model reply has no paragraphs"]
    paras: list[dict] = []
    for p in raw:
        if isinstance(p, str):
            p = {"text": p, "fact_ids": []}
        if not isinstance(p, dict):
            continue
        text = re.sub(r"\s+", " ", ascii_hyphens(str(p.get("text") or ""))).strip()
        if text:
            paras.append({"text": text, "fact_ids": clean_ids(p.get("fact_ids"))})
    subject = re.sub(r"\s+", " ", str(parsed.get("subject") or "")).strip()
    return paras, subject, ([] if paras else ["model reply has no paragraph text"])


# --------------------------------------------------------------------------- attempts

def retry_note(issues: list[str]) -> str:
    """What a retry is told about the previous draft: its problems only, never
    its text (each attempt is still a fresh single-turn conversation)."""
    if not issues:
        return ""
    return ("\n\nA previous draft was rejected for: " + "; ".join(i[:300] for i in issues[:6])
            + ". Write a new draft that fixes every one of these and follows every rule.")


def best_attempt(system: str, user: str, build: Callable[[Any], tuple[Any, list[str]]], *,
                 attempts: int, max_tokens: int = 1200,
                 feedback: bool = True) -> tuple[Any, list[str], int]:
    """Call the model up to `attempts` times, each a FRESH conversation (L#2).
    `build(parsed)` → (candidate, issues). The first candidate with no issues
    wins; otherwise the one with the fewest. With `feedback`, a retry's prompt
    gets the previous attempt's issues appended (retry_note), never its reply.
    Raises WritingError if no attempt produced a candidate at all."""
    best: tuple[Any, list[str]] | None = None
    errors: list[str] = []
    last: list[str] = []
    n = 0
    for n in range(1, max(1, attempts) + 1):
        prompt = user + (retry_note(last) if feedback else "")
        try:
            parsed, _ = llm.complete_json(system, prompt, temperature=0.3, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001 — try again, then report
            errors.append(f"{type(exc).__name__}: {exc}")
            last = []
            continue
        cand, issues = build(parsed)
        last = list(issues)
        if cand is None:
            errors.append("; ".join(issues) or "unusable reply")
            continue
        if best is None or len(issues) < len(best[1]):
            best = (cand, issues)
        if not issues:
            break
    if best is None:
        raise WritingError("the model did not return a usable draft: " + " | ".join(errors[-2:]))
    return best[0], best[1], n


def assemble(paragraphs: list[dict], usable: list, *, job: dict, rec: dict, data: dict,
             max_words: int, extra_allowed: str = "") -> tuple[dict, list[str]]:
    """Clichés out, then length / leak / grounding / never_mention checks.
    Returns ({paragraphs, grounding, words, removed}, issues)."""
    paras, removed = style.remove_cliche_sentences(paragraphs)
    issues: list[str] = []
    if not paras:
        return {"paragraphs": [], "grounding": {"clean": False}, "words": 0, "removed": removed}, \
            ["nothing left after removing clichés"]
    body = "\n\n".join(p["text"] for p in paras)
    words = style.word_count(body)
    if words > max_words:
        issues.append(f"{words} words, {words - max_words} words over the {max_words}-word "
                      f"limit: cut at least {words - max_words} words")
    leaks = style.find_leaks(body)
    if leaks:
        issues.append("leaked/template text: " + ", ".join(sorted(set(leaks))[:5]))
    never = [t for t in ((data.get("writing") or {}).get("never_mention") or [])
             if isinstance(t, str) and t.strip() and grounding._has_term(body, t)]
    if never:
        issues.append("mentions a never_mention item: " + ", ".join(never))
    g = ground_paragraphs(paras, usable, job=job, rec=rec, extra_allowed=extra_allowed)
    if not g.get("clean"):
        bad = [t for v in g.get("lines", []) if not v.get("supported") for t in v.get("offending", [])]
        issues.append("ungrounded: " + ", ".join(dict.fromkeys(bad)))
        for v in g.get("lines", []):
            if not v.get("supported"):
                issues.append(f'unsupported sentence: "{v.get("text")}" '
                              f'({", ".join(dict.fromkeys(v.get("offending", [])))})')
    return {"paragraphs": paras, "grounding": g, "words": words, "removed": removed}, issues


# --------------------------------------------------------------------------- length

def trim_to_limit(paragraphs: list[dict], grounding_result: dict, usable: list,
                  max_words: int) -> tuple[list[dict], list[str]]:
    """Deterministic last resort for an over-long body: delete WHOLE sentences,
    least-evidenced first, until the body fits. Order: sentences grounding
    flagged; then extra (non-first) sentences of the opening / closing; then
    evidence sentences with the lowest overlap with their cited facts. The
    letter's first sentence and a paragraph's last remaining sentence are never
    removed, and no sentence is ever cut mid-way. Returns (paragraphs, removed
    sentences); the caller re-checks the result."""
    by_id = {f.id: f for f in usable}
    verdict = {(v.get("paragraph"), v.get("text")): v for v in grounding_result.get("lines", [])}
    sents = [[s for s in style.split_sentences(p.get("text", ""))] for p in paragraphs]
    n = len(paragraphs)
    cands = []
    for pi, ss in enumerate(sents):
        ev = "\n".join(getattr(by_id.get(x), "text", "") for x in paragraphs[pi].get("fact_ids") or [])
        edge = pi == 0 or (pi == n - 1 and n >= 3)
        for si, s in enumerate(ss):
            if pi == 0 and si == 0:
                continue
            v = verdict.get((pi, s)) or {}
            flagged = v.get("supported") is False
            tier = 0 if flagged else (1 if edge and si > 0 else (2 if not edge else 3))
            overlap = grounding.token_overlap(s, ev) if ev else 0.0
            cands.append((tier, overlap, -style.word_count(s), pi, si))
    cands.sort()
    removed: list[str] = []
    gone: set[tuple[int, int]] = set()

    def body_words() -> int:
        return sum(style.word_count(s) for pi, ss in enumerate(sents)
                   for si, s in enumerate(ss) if (pi, si) not in gone)

    for tier, _ov, _w, pi, si in cands:
        if body_words() <= max_words:
            break
        left = [j for j in range(len(sents[pi])) if (pi, j) not in gone]
        if left == [si]:
            continue                                   # keep every paragraph
        gone.add((pi, si))
        removed.append(sents[pi][si])
    out = []
    for pi, ss in enumerate(sents):
        kept = [s for si, s in enumerate(ss) if (pi, si) not in gone]
        if kept:
            out.append({**paragraphs[pi], "text": " ".join(kept)})
    return out, removed
