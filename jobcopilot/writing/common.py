"""Shared machinery for generated writing: inputs, prompt blocks, grounding,
fresh-conversation retries and the Draft result.

Grounding (fail closed): the model returns paragraphs that cite fact ids. Each
sentence is checked with grounding v3 `check_lines`:
  * A sentence may use the job description + company record (`_jd`) ONLY when
    it names the target company or clearly refers to it (the role / the team /
    you; it / its / they / their right after such a sentence) AND carries no
    applicant marker: first person, the applicant's name, "the applicant /
    candidate", an applicant employer / project name or distinctive word, an
    agentless passive past tense ("was led end to end"), or a skill /
    ownership claim that the paragraph's cited facts make.
  * Every other sentence is checked against its paragraph's cited facts only
    (a cited bullet brings its header), with the capability-claim check on.
    When it names exactly one of the applicant's employers / projects, the
    evidence narrows to that header and its own cited bullets (a detail can't
    move between experiences); naming none while the cited facts span several
    experiences flags `mixed_attribution`; naming two flags `mixed_experiences`.
  * The target company and role title are never evidence. In an applicant
    sentence they are allowed only as names in forward-looking spots ("the
    <title> role at <company>", "join <company>"); "at <company>" or "as a
    <title>" before a past-tense verb flags `role_misuse`.
Drafts are also rejected (→ retry → needs_review) for markdown, a URL / e-mail
address that isn't the applicant's own (answers.yaml links / signature or the
facts), non-Latin / confusable / zero-width characters, and an evidence
paragraph that mixes experiences. Retry prompts name only paragraph / sentence
numbers and the kind of problem, never the rejected text. Nothing is sent
anywhere: the draft is shown to the user before use.
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


class Issue(str):
    """A problem with a draft. The string is user-facing (it may quote the
    draft); `.model` is what a retry prompt says — paragraph / sentence numbers
    and the kind of problem only, never the rejected text."""
    model: str
    soft: bool          # a style problem (an AI tell): retried and flagged, but a draft
                        # with fewer grounding / safety problems always wins (best_attempt)

    def __new__(cls, user: str, model: Optional[str] = None, *, soft: bool = False) -> "Issue":
        obj = super().__new__(cls, user)
        obj.model = user if model is None else model
        obj.soft = soft
        return obj


def hard_issues(issues: list) -> list:
    """The issues that decide a draft's status: everything but style (soft)
    issues. An AI tell alone never makes a draft needs_review; it is retried
    and shown as a warning."""
    return [i for i in issues if not getattr(i, "soft", False)]


def _rank(issues: list) -> tuple[int, int]:
    """(hard problems, style problems): how best_attempt compares drafts."""
    soft = sum(1 for i in issues if getattr(i, "soft", False))
    return len(issues) - soft, soft


# Flag key → how a retry prompt names it (no tokens from the rejected draft).
FLAG_CLASSES = {
    "unsupported_skills": "a skill or tool the cited facts don't state",
    "new_numbers": "a number or quantity (digits or words) not in the cited facts",
    "inflated_claims": "an ownership, seniority, title or scope claim the cited facts don't make",
    "new_proper_nouns": "a name the cited facts don't contain",
    "unsupported_claims": "an experience / capability claim or interpretation the cited facts don't state",
    "low_similarity": "wording too far from the cited facts",
    "unknown_facts": "a fact id that is not a confirmed fact",
    "mixed_attribution": "facts from more than one employer/project without naming which one",
    "mixed_experiences": "two employers/projects in one sentence",
    "role_misuse": "the target company or role title used as the applicant's past employer or title",
}
EXTRA_FLAG_KEYS = ("mixed_attribution", "mixed_experiences", "role_misuse")


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


def role_names(job: dict, rec: dict) -> tuple[str, str]:
    """(title, company), sanitised like fenced text: these come from the web."""
    return (style.clean_field(job.get("title") or ""),
            style.clean_field(job.get("company") or rec.get("name") or ""))


def job_block(job: dict, rec: dict) -> str:
    title, company = role_names(job, rec)
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
    text = grounding.fold_confusables(text)                    # NFKC, zero-width out, look-alikes
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


# --------------------------------------------------------------------------- applicant headers

# Words in employer / project names that say nothing about WHICH one it is.
_GENERIC_NAME_WORDS = frozenset("""
inc ltd llc llp plc corp corporation co company companies pvt private limited labs lab
technologies technology tech systems solutions software services service group holdings
global international university college institute school academy the and of for at in
project projects app apps platform team hackathon
""".split())


def _root(fid: str, by_id: dict) -> Optional[str]:
    """The employer / project / education header a fact belongs to (None for skills)."""
    f = by_id.get(fid)
    if f is None or getattr(f, "kind", "") == "skill" or getattr(f, "section", "") == "skills":
        return None
    parent = getattr(f, "parent", "") or ""
    return parent if parent in by_id else (None if parent else fid)


def header_info(usable: list, *, exclude_words: str = "") -> dict[str, dict]:
    """{header id: {"names": [full names], "words": [distinctive words]}} for the
    applicant's employers / projects / schools. A distinctive word is a
    capitalised name word that isn't generic ("Acme" in "Acme Widgets Inc.")
    and isn't a word of `exclude_words` (the target company / title)."""
    excl = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]*", fold(exclude_words))}
    out: dict[str, dict] = {}
    for f in usable:
        if getattr(f, "kind", "") in ("bullet", "skill") or getattr(f, "section", "") == "skills":
            continue
        names = []
        for a in ("name", "company", "institution"):
            n = str(getattr(f, a, "") or "").strip()
            if len(n) >= 3 and n not in names:
                names.append(n)
        if not names:
            continue
        words = []
        for n in names:
            for w in re.findall(r"[A-Za-z][A-Za-z0-9+#]*", fold(n)):
                if (len(w) >= 3 and w[0].isupper() and w.lower() not in _GENERIC_NAME_WORDS
                        and w.lower() not in excl and w not in words):
                    words.append(w)
        out[f.id] = {"names": names, "words": words}
    return out


def _applicant_names(usable: list) -> list[str]:
    """Names of the applicant's employers / projects / hackathons (header facts)."""
    return [n for h in header_info(usable).values() for n in h["names"]]


def _name_re(name: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(map(re.escape, fold(name).split()))
                      + r"(?![A-Za-z0-9])", re.I)


def named_headers(sentence: str, hinfo: dict[str, dict]) -> list[str]:
    """Header ids whose full name (any case) or distinctive word (as written) the sentence uses."""
    s = fold(sentence)
    out = []
    for hid, h in hinfo.items():
        if any(_name_re(n).search(s) for n in h["names"]) or any(
                re.search(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])", s) for w in h["words"]):
            out.append(hid)
    return out


def about_applicant(sentence: str, names: list[str]) -> bool:
    """First person, or it names one of the applicant's employers / projects."""
    if style.is_first_person(sentence):
        return True
    s = fold(sentence)
    return any(_name_re(n).search(s) for n in names)


# --------------------------------------------------------------------------- sentence roles

_APPLICANT_WORDS_RE = re.compile(r"\b(?:the|this|our|an?)\s+(?:applicant|candidate)s?\b", re.I)
_PARTICIPLE = (r"(?:[a-z]{2,}ed|built|rebuilt|led|run|made|written|rewritten|grown|driven|taken|set|"
               r"cut|shipped|won|done|sold|brought|begun|held|kept|sent|spent|taught|known|shown|"
               r"given|seen|chosen|drawn|undertaken|overseen|put|split|met|won|beaten|thrown)")
_PASSIVE_RE = re.compile(r"\b(?:was|were|had\s+been|has\s+been|have\s+been|got)\s+"
                         r"(?:(?:also|then|later|first|successfully|fully|largely|[a-z]+ly)\s+)*"
                         + _PARTICIPLE + r"\b(?P<rest>[^.;!?]*)", re.I)
_EXPLICIT_REF_RE = re.compile(r"\b(?:(?:the|this|your)\s+(?:team|role|company|position|opening|product|"
                              r"platform|job|mission|business)|you|your)\b", re.I)
_PRONOUN_REF_RE = re.compile(r"\b(?:it|its|they|their|them)\b", re.I)
_FORWARD_CUE_RE = re.compile(
    r"\b(?:apply|applying|applied|application|role|position|opening|team|join|joining|contribute|"
    r"contributing|help|helping|bring|bringing|discuss|discussing|would|could|will|can|hope|"
    r"look\s+forward|welcome|interested|keen|eager|excited)\b|['’](?:d|ll)\b", re.I)
_PAST_VERB_RE = re.compile(
    r"\b(?!(?:interested|excited|based|focused|motivated|inspired|related|skilled|dedicated|"
    r"detailed|advanced|distributed|automated|experienced|qualified|located|headquartered|"
    r"funded|backed|listed|used|needed|required|preferred)\b)"
    r"(?:[a-z]{3,}ed|built|rebuilt|led|ran|made|wrote|rewrote|grew|drove|took|began|cut|shipped|"
    r"won|did|sold|brought|held|kept|sent|spent|taught|got|gave|chose|drew|oversaw|was|were|had)\b",
    re.I)


def _title_variants(title: str) -> list[str]:
    out = []
    for t in (title, re.split(r"\s*[(\[]", title)[0], re.split(r"\s+[-–|/,]\s*|,\s*", title)[0]):
        t = t.strip(" -,|/")
        if len(t) >= 3 and t not in out:
            out.append(t)
    return out


def role_mask(sentence: str, company: str, title: str, evidence: str = "") -> tuple[str, list[str]]:
    """(sentence for checking, role misuse found). The target title / company are
    masked out where they are forward-looking ("the <title> role", "applying for
    the <title>", "<company>" after apply / role / team / join / would …), so
    they're never read as a skill, number or claim; anywhere else they stay and
    must be grounded like any other word. "at <company>" or "as a <title>"
    followed by a past-tense verb is role misuse — unless `evidence` (the cited
    facts) names that company / title itself."""
    s = fold(sentence)
    company, title = fold(company), fold(title)
    misuse: list[str] = []
    for t in _title_variants(title):
        tr = r"\s+".join(map(re.escape, t.split()))
        m_as = re.search(r"\bas\s+(?:a|an|the)\s+" + tr + r"(?![A-Za-z0-9])", s, re.I)
        if m_as and _PAST_VERB_RE.search(s[m_as.end():]) and not grounding._has_term(evidence, t):
            misuse.append(f"as a {t}")
        s = re.sub(r"(\b(?:the|this|your|a|an|for|to)\s+)" + tr
                   + r"(?=\s+(?:role|position|opening|job|opportunity|team)\b)", r"\1", s, flags=re.I)
        s = re.sub(r"(\b(?:apply|applying|applied|application|interest)\s+(?:for|to|in)\s+"
                   r"(?:the\s+|this\s+|your\s+)?)" + tr + r"(?![A-Za-z0-9])", r"\1role", s, flags=re.I)
    if company:
        cr = r"\s+".join(map(re.escape, company.split()))
        for m in reversed(list(re.finditer(r"(?<![A-Za-z0-9])" + cr + r"(?:['’]s)?(?![A-Za-z0-9])", s, re.I))):
            before = s[:m.start()]
            if _FORWARD_CUE_RE.search(before):
                s = s[:m.start()] + "the company" + s[m.end():]
            elif re.search(r"\b(?:at|for|with|in)\s+$", before, re.I) and \
                    _PAST_VERB_RE.search(s[m.end():]) and not grounding._has_term(evidence, company):
                misuse.append(f"at {company}")
    return s, list(dict.fromkeys(misuse))


def _applicant_marker(sentence: str, *, person: list[str], named: list[str], cited_ev: str,
                      company: str) -> bool:
    if style.is_first_person(sentence) or named or _APPLICANT_WORDS_RE.search(sentence):
        return True
    s = fold(sentence)
    if any(re.search(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])", s) for w in person):
        return True
    for m in _PASSIVE_RE.finditer(s):
        agent = re.match(r"\s*(?:[^,;]*?\s)?by\s+(.*)$", m.group("rest") or "", re.I)
        if not (agent and company and _name_re(company).match(agent.group(1).strip())):
            return True                                        # agentless (or not the company's)
    if cited_ev:
        ev_sk = {c.lower() for _, _, c in grounding._skill_spans(cited_ev)}
        if any(c.lower() in ev_sk for _, _, c in grounding._skill_spans(s)):
            return True
        if grounding._ownership_families(s) & grounding._ownership_families(cited_ev):
            return True
    return False


def _fact_text(f) -> str:
    return grounding._fact_evidence(f)


def ground_paragraphs(paragraphs: list[dict], usable: list, *, job: dict, rec: dict,
                      extra_allowed: str = "", applicant_names: Any = ()) -> dict:
    """Grounding verdicts per sentence (see the module doc). Paragraphs marked
    `fixed` (text built in code, e.g. a follow-up's date line) aren't checked."""
    by_id = {f.id: f for f in usable}
    title, company = role_names(job, rec)
    hinfo = header_info(usable, exclude_words=f"{company} {title}")
    person = [w for n in (applicant_names or ()) for w in re.findall(r"[A-Za-z][A-Za-z'-]+", fold(str(n)))
              if len(w) >= 2]
    names = [_FUNCTION_WORDS, extra_allowed]
    fmap: dict[str, Any] = dict(by_id)
    fmap["_role"] = ""        # no evidence: lets an uncited sentence be checked, never supports it
    fmap["_jd"] = fold("\n".join([(job.get("description_text") or "")[:_JD_LIMIT],
                                  company_text(rec), company, title]))
    lines: list[dict] = []
    extra: list[dict] = []                       # per line: {flag key: [tokens]}
    for pi, p in enumerate(paragraphs):
        if p.get("fixed"):
            continue
        ids = clean_ids(p.get("fact_ids"))
        for x in list(ids):                     # a bullet brings its header
            parent = getattr(by_id.get(x), "parent", "")
            if parent and parent in by_id and parent not in ids:
                ids.append(parent)
        roots = list(dict.fromkeys(r for r in (_root(x, by_id) for x in ids) if r))
        cited_ev = fold("\n".join(_fact_text(by_id[x]) for x in ids if x in by_id))
        prev_company = False
        for si, s in enumerate(style.split_sentences(p.get("text", ""))):
            # links are judged by assemble (own links only); their words aren't claims
            fs = grounding.URL_RE.sub(" link ", fold(s))
            named = named_headers(s, hinfo)
            marker = _applicant_marker(s, person=person, named=named, cited_ev=cited_ev, company=company)
            refers = bool((company and _name_re(company).search(fs)) or _EXPLICIT_REF_RE.search(fs)
                          or (prev_company and _PRONOUN_REF_RE.search(fs)))
            company_sentence = refers and not marker
            prev_company = company_sentence
            flags: dict[str, list[str]] = {}
            if company_sentence:
                line_ids, checked = ids + ["_role", "_jd"], fs
            else:
                checked, misuse = role_mask(fs, company, title, cited_ev)
                if misuse:
                    flags["role_misuse"] = misuse
                line_ids = list(ids)
                if len(named) == 1:
                    hid = named[0]
                    line_ids = [hid] + [x for x in ids if x != hid and _root(x, by_id) == hid]
                elif len(named) > 1:
                    flags["mixed_experiences"] = [" + ".join(hinfo[h]["names"][0] for h in named)]
                elif len(roots) > 1:
                    flags["mixed_attribution"] = [" + ".join(hinfo.get(r, {"names": [r]})["names"][0]
                                                             for r in roots)]
                line_ids = line_ids + ["_role"]
            lines.append({"text": checked, "fact_ids": line_ids, "paragraph": pi, "sentence": si,
                          "shown": s, "check_claims": not company_sentence, "names": names})
            extra.append(flags)
    check = getattr(grounding, "check_lines", None)
    result = check(lines, fmap) if check else _fallback_check_lines(lines, fmap)
    result.setdefault("flags", {})
    for k in EXTRA_FLAG_KEYS:
        result["flags"].setdefault(k, [])
    for v, ln, fl in zip(result.get("lines", []), lines, extra):
        v["paragraph"], v["sentence"] = ln["paragraph"], ln["sentence"]
        v["text"] = ln["shown"]
        v["fact_ids"] = [x for x in v.get("fact_ids", []) if x not in PSEUDO_IDS]
        for k in EXTRA_FLAG_KEYS:
            v[k] = fl.get(k, [])
            for t in v[k]:
                if t not in result["flags"][k]:
                    result["flags"][k].append(t)
                v.setdefault("offending", [])
                v["offending"].append(t)
        if any(fl.values()):
            v["supported"] = False
    result["unsupported_lines"] = [v["index"] for v in result.get("lines", []) if not v.get("supported")]
    result["clean"] = not result["unsupported_lines"]
    result["cited_fact_ids"] = sorted({x for ln in lines for x in ln["fact_ids"] if x in by_id})
    result["engine"] = "v3" if check else "v2-fallback"
    return result


def parse_paragraphs(parsed: Any) -> tuple[list[dict], str, list[str]]:
    """(paragraphs, subject, problems) from {paragraphs: [{text, fact_ids}]}.
    Text is NFKC-normalised (fullwidth digits → ASCII); format characters are
    kept so `assemble` can reject them. The model's subject is returned for
    compatibility only — subjects are built in code."""
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
        text = unicodedata.normalize("NFKC", str(p.get("text") or ""))
        text = re.sub(r"\s+", " ", ascii_hyphens(text)).strip()
        if text:
            paras.append({"text": text, "fact_ids": clean_ids(p.get("fact_ids"))})
    subject = re.sub(r"\s+", " ", str(parsed.get("subject") or "")).strip()
    return paras, subject, ([] if paras else ["model reply has no paragraph text"])


def raw_texts(parsed: Any) -> list[str]:
    """The model's paragraph texts before any clean-up (dashes kept, clichés
    still in): what the AI-tell check looks at, so a retry hears about habits
    the deterministic fixes would otherwise hide."""
    raw = parsed.get("paragraphs") if isinstance(parsed, dict) else None
    out = []
    for p in raw if isinstance(raw, list) else []:
        t = p if isinstance(p, str) else (p.get("text") if isinstance(p, dict) else "")
        t = re.sub(r"[ \t]+", " ", unicodedata.normalize("NFKC", str(t or ""))).strip()
        if t:
            out.append(t)
    return out


def tell_issues(texts: list[str]) -> list[Issue]:
    """One soft Issue per AI tell found in the paragraphs `texts`. The reasons
    are fixed labels (style.find_ai_tells), so the retry note never quotes the draft."""
    return [Issue(f"reads like AI-written text: {r}",
                  model=f"AI-sounding style: {r}; rewrite it in a plain, varied human voice",
                  soft=True)
            for r in style.find_ai_tells("\n\n".join(texts))]


def writing_sample(data: Optional[dict] = None, sample: Optional[str] = None) -> str:
    """The applicant's own writing, used only as a voice reference: `sample`
    when given, else answers.yaml `writing.style_sample`, else the writing
    sample saved on the Facts & answers screen (meta `user_email_style`).
    Capped in length; "" when there is none or it can't be read."""
    text = sample
    if text is None:
        text = str(((data or {}).get("writing") or {}).get("style_sample") or "")
    if not str(text).strip() and sample is None:
        try:
            from .. import store
            text = store.get_meta("user_email_style") or ""
        except Exception as exc:  # noqa: BLE001 — optional: no DB means no sample
            log.debug("no writing sample: %s", type(exc).__name__)
            text = ""
    text = str(text or "").strip()
    limit = int(style.settings().get("style_sample_max_chars") or 1500)
    return text[:limit].strip()


def sample_block(sample: str) -> str:
    """The fenced voice reference for a prompt ("" without a sample)."""
    if not sample:
        return ""
    return ("\n\n" + style.fence(sample, "untrusted_writing_sample")
            + "\nThe sample above shows how the applicant writes. Match its rhythm and tone "
              "only; take no facts, names, numbers or sentences from it.")


def application_subject(job: dict, rec: dict) -> str:
    """The subject line, built in code from the (sanitised) title and company."""
    title, company = role_names(job, rec)
    title = title or "the role"
    return f"Application: {title} — {company}" if company else f"Application: {title}"


# --------------------------------------------------------------------------- attempts

def retry_note(issues: list[str]) -> str:
    """What a retry is told about the previous draft: its problems only, never
    its text (each attempt is still a fresh single-turn conversation). An Issue
    contributes its model-facing form (numbers + kind of problem)."""
    if not issues:
        return ""
    ordered = sorted(issues, key=lambda i: bool(getattr(i, "soft", False)))   # hard problems first
    told = list(dict.fromkeys(str(getattr(i, "model", i))[:300] for i in ordered))
    return ("\n\nA previous draft was rejected for: " + "; ".join(told[:10])
            + ". Write a new draft that fixes every one of these and follows every rule.")


def best_attempt(system: str, user: str, build: Callable[[Any], tuple[Any, list[str]]], *,
                 attempts: int, max_tokens: int = 1200,
                 feedback: bool = True) -> tuple[Any, list[str], int]:
    """Call the model up to `attempts` times, each a FRESH conversation (docs/DESIGN.md §9).
    `build(parsed)` → (candidate, issues). The first candidate with no issues
    wins; otherwise the one with the fewest hard issues, then the fewest style
    (soft) issues, so an AI tell never outweighs a grounding problem. With `feedback`, a retry's prompt
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
        if best is None or _rank(issues) < _rank(best[1]):
            best = (cand, issues)
        if not issues:
            break
    if best is None:
        raise WritingError("the model did not return a usable draft: " + " | ".join(errors[-2:]))
    return best[0], best[1], n


def _own_links(data: dict, usable: list) -> set[str]:
    """URLs / e-mail addresses the applicant's own data contains (answers.yaml
    personal links + e-mail + sign-off, and the confirmed facts)."""
    vals = sign_values(data)
    src = [vals.get(k, "") for k in ("email", "linkedin", "github", "portfolio")]
    src.append(str(((data.get("writing") or {}).get("signoff")) or ""))
    src += [_fact_text(f) for f in usable]
    own = set()
    for t in src:
        own |= {x.lower().rstrip("/") for x in grounding.links_in(t)}
        if t and "@" not in t and re.match(r"https?://", t.strip()):
            own.add(t.strip().lower().rstrip("/"))
    return own


def _sentence_issues(g: dict) -> list[Issue]:
    out: list[Issue] = []
    bad = [v for v in g.get("lines", []) if not v.get("supported")]
    if not bad:
        return out
    tokens = [t for v in bad for t in v.get("offending", [])]
    classes = []
    for v in bad:
        kinds = [FLAG_CLASSES.get(k, k) for k in list(grounding.LINE_FLAG_KEYS) + list(EXTRA_FLAG_KEYS)
                 if v.get(k)]
        classes.append(f"paragraph {v.get('paragraph', 0) + 1} sentence {v.get('sentence', 0) + 1}: "
                       + ", ".join(kinds or ["not supported by its cited facts"]))
    out.append(Issue("ungrounded: " + ", ".join(dict.fromkeys(tokens)),
                     model="ungrounded sentences (remove or rewrite each one strictly from its "
                           "paragraph's cited facts)"))
    for v, cls in zip(bad, classes):
        out.append(Issue(f'unsupported sentence: "{v.get("text")}" '
                         f'({", ".join(dict.fromkeys(v.get("offending", [])))})', model=cls))
    return out


def assemble(paragraphs: list[dict], usable: list, *, job: dict, rec: dict, data: dict,
             max_words: int, extra_allowed: str = "", rewrite: bool = True) -> tuple[dict, list[str]]:
    """Clichés out, metrics normalised, then length / leak / markup / link /
    character / mixed-experience / grounding / never_mention checks.
    Returns ({paragraphs, grounding, words, removed, normalised}, issues).

    rewrite=False checks text the person wrote (a saved cover letter) without
    changing it: cliché sentences stay in (and are still grounded), metric
    wording is left as written, and `removed` / `normalised` list what the
    draft path would have deleted or reworded, for the caller to flag."""
    fixed = [p for p in paragraphs if p.get("fixed")]
    if rewrite:
        paras, removed = style.remove_cliche_sentences([p for p in paragraphs if not p.get("fixed")])
    else:
        paras = [dict(p) for p in paragraphs if not p.get("fixed")]
        removed = [c for p in paras for c in style.find_cliches(p.get("text", ""))]
    paras = fixed + paras if fixed and paragraphs and paragraphs[0].get("fixed") else paras + fixed
    issues: list[str] = []
    empty = {"paragraphs": [], "grounding": {"clean": False}, "words": 0, "removed": removed,
             "normalised": []}
    if not [p for p in paras if not p.get("fixed")]:
        return empty, ["nothing left after removing clichés"]
    by_id = {f.id: f for f in usable}
    normalised: list[str] = []
    for p in paras:
        if p.get("fixed"):
            continue
        ids = [x for x in p.get("fact_ids") or [] if x in by_id]
        metrics = [m for x in ids for m in (getattr(by_id[x], "metrics", None) or [])]
        if metrics:
            text, changes = grounding.normalize_metrics(p["text"], metrics)
            if rewrite:
                p["text"] = text
            normalised += [f"'{c['from']}' -> '{c['to']}'" for c in changes]
    body = "\n\n".join(p["text"] for p in paras)
    words = style.word_count(body)
    if words > max_words:
        issues.append(f"{words} words, {words - max_words} words over the {max_words}-word "
                      f"limit: cut at least {words - max_words} words")
    leaks = style.find_leaks(body)
    if leaks:
        issues.append(Issue("leaked/template text: " + ", ".join(sorted(set(leaks))[:5]),
                            model="leaked or template text (an assistant phrase, placeholder, "
                                  "bracketed text or a fact id)"))
    own = _own_links(data, usable)
    for pi, p in enumerate(paras):
        if p.get("fixed"):
            continue
        n = pi + 1
        if grounding.MARKUP_RE.search(p["text"]):
            issues.append(Issue(f"paragraph {n} contains markdown or markup",
                                model=f"paragraph {n} contains markdown or markup (plain text only)"))
        links = [x for x in grounding.links_in(p["text"]) if x.lower().rstrip("/") not in own]
        if links:
            issues.append(Issue(f"paragraph {n} contains a URL or e-mail address: " + ", ".join(links[:3]),
                                model=f"paragraph {n} contains a URL or e-mail address (never include one)"))
        ev = "\n".join(_fact_text(by_id[x]) for x in p.get("fact_ids") or [] if x in by_id)
        if grounding.confusable_problem(p["text"], ev + "\n" + " ".join(role_names(job, rec))):
            issues.append(Issue(f"paragraph {n} contains non-Latin or confusable characters",
                                model=f"paragraph {n} contains non-Latin, look-alike or invisible "
                                      "characters (plain ASCII only)"))
        roots = list(dict.fromkeys(r for r in (_root(x, by_id) for x in p.get("fact_ids") or [])
                                   if r))
        if len(roots) > 1:
            hinfo = header_info(usable)
            issues.append(Issue(f"paragraph {n} mixes experiences: "
                                + " + ".join(hinfo.get(r, {"names": [r]})["names"][0] for r in roots),
                                model=f"paragraph {n} mixes experiences: each evidence paragraph must be "
                                      "about exactly ONE employer or project and cite only its facts"))
    never = [t for t in ((data.get("writing") or {}).get("never_mention") or [])
             if isinstance(t, str) and t.strip() and grounding._has_term(body, t)]
    if never:
        issues.append(Issue("mentions a never_mention item: " + ", ".join(never),
                            model="mentions an item from the applicant's never-mention list"))
    vals = sign_values(data)
    person = [vals["name"], vals["first_name"],
              str(((data.get("personal") or {}).get("last_name")) or "")]
    g = ground_paragraphs(paras, usable, job=job, rec=rec, extra_allowed=extra_allowed,
                          applicant_names=[x for x in person if x])
    if not g.get("clean"):
        issues += _sentence_issues(g)
    return {"paragraphs": paras, "grounding": g, "words": words, "removed": removed,
            "normalised": normalised}, issues


# --------------------------------------------------------------------------- length

_ANAPHOR_RE = re.compile(r"^[\"'(]?\s*(?:it|this|that|these|those|they|there|also|then|the\s+same|both|which)\b",
                         re.I)


def _leans_back(sentence: str) -> bool:
    """Starts with a word that points at the sentence before it ("It handled …")."""
    return bool(_ANAPHOR_RE.search(sentence)) or "also" in [
        w.lower() for w in re.findall(r"[A-Za-z']+", sentence)[:3]]


def trim_to_limit(paragraphs: list[dict], grounding_result: dict, usable: list,
                  max_words: int) -> tuple[list[dict], list[str]]:
    """Deterministic last resort for an over-long body: delete WHOLE sentences,
    least-evidenced first, until the body fits. Order: sentences grounding
    flagged; then extra sentences of the opening / closing; then evidence
    sentences with the lowest overlap with their cited facts. Never removed:
    a paragraph's first sentence; a sentence whose next kept sentence leans on
    it ("It …", "This …", "also" in its first 3 words, …); the only sentence
    naming the paragraph's employer / project. No sentence is ever cut mid-way.
    Returns (paragraphs, removed sentences); the body may still be over the
    limit — the caller re-checks it (→ needs_review)."""
    by_id = {f.id: f for f in usable}
    hinfo = header_info(usable)
    verdict = {(v.get("paragraph"), v.get("text")): v for v in grounding_result.get("lines", [])}
    sents = [style.split_sentences(p.get("text", "")) for p in paragraphs]
    n = len(paragraphs)
    cands = []
    naming: dict[tuple[int, int], set[str]] = {}
    for pi, ss in enumerate(sents):
        if paragraphs[pi].get("fixed"):
            continue
        ids = paragraphs[pi].get("fact_ids") or []
        roots = {r for r in (_root(x, by_id) for x in ids) if r}
        ev = "\n".join(getattr(by_id.get(x), "text", "") for x in ids)
        edge = pi == 0 or (pi == n - 1 and n >= 3)
        for si, s in enumerate(ss):
            naming[(pi, si)] = set(named_headers(s, {h: hinfo[h] for h in roots if h in hinfo}))
            if si == 0:
                continue
            v = verdict.get((pi, s)) or {}
            flagged = v.get("supported") is False
            tier = 0 if flagged else (1 if edge else 2)
            overlap = grounding.token_overlap(s, ev) if ev else 0.0
            cands.append((tier, overlap, -style.word_count(s), pi, si))
    cands.sort()
    removed: list[str] = []
    gone: set[tuple[int, int]] = set()

    def body_words() -> int:
        return sum(style.word_count(s) for pi, ss in enumerate(sents)
                   for si, s in enumerate(ss) if (pi, si) not in gone)

    def removable(pi: int, si: int) -> bool:
        nxt = next((j for j in range(si + 1, len(sents[pi])) if (pi, j) not in gone), None)
        if nxt is not None and _leans_back(sents[pi][nxt]):
            return False
        for h in naming.get((pi, si), ()):
            others = [j for j in range(len(sents[pi])) if j != si and (pi, j) not in gone
                      and h in naming.get((pi, j), ())]
            if not others:
                return False
        return True

    progress = True
    while progress and body_words() > max_words:
        progress = False
        for _tier, _ov, _w, pi, si in cands:
            if body_words() <= max_words:
                break
            if (pi, si) in gone or not removable(pi, si):
                continue
            gone.add((pi, si))
            removed.append(sents[pi][si])
            progress = True
            break                                  # re-check every rule after each removal
    out = []
    for pi, ss in enumerate(sents):
        kept = [s for si, s in enumerate(ss) if (pi, si) not in gone]
        if kept:
            out.append({**paragraphs[pi], "text": " ".join(kept)})
    return out, removed
