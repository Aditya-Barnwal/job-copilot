"""Deterministic anti-fabrication check.

After the model returns a tailored résumé (or cover email), this catches the
patterns a weak model is most likely to invent — WITHOUT using an LLM to judge
another LLM. Every check is a plain set-difference against the source résumé, so
a flag is always explainable ("this term/number/verb is in the tailored text but
not in your original") and never a probabilistic guess.

Four checks, chosen for high signal-to-noise (a noisy check trains the user to
reflexively tick "I confirm", which defeats the whole gate):

  1. inserted_unsupported — a job "missing keyword" now present in the tailored
     text but absent from the original AND not pre-approved by the user.
  2. unsupported_skills   — any known tech/tool term (from the scoring vocab)
     that appears in the tailored text, is absent from the original, and the
     user hasn't approved. Closes the hole where a fabricated skill isn't in the
     job's missing-keyword set, so check #1 would miss it.
  3. new_numbers          — numbers / percentages / years in the tailored text
     that weren't in the original. The classic fabricated-metric pattern.
     Spelled-out quantities count too ("doubled", "tenfold", "ten million",
     "forty percent"), unless the original already has the same value in
     digits or words.
  4. inflated_claims      — ownership / seniority verbs ("lead", "led",
     "own", "architected", …) and job titles ("Senior …", "Staff …",
     "Principal …", "Lead …", "Tech Lead", "Head of …") introduced in the
     tailored text but not in the original. Catches credit inflation that uses
     only existing vocabulary — e.g. "collaborated on" rewritten as "led" —
     which the keyword checks miss. Non-claim uses are skipped so the check
     stays quiet: causal "led to", "my own", "a leading fintech",
     "data-driven", "mentored by", "senior engineers" (other people), etc.

If any list is non-empty, `clean` is False and the UI blocks Approve until the
human explicitly confirms the additions. Nothing auto-submits, so the human is
always the final grounding layer; these checks exist to make that review cheap
and focused.

Deliberately NOT checked: superlative qualifiers ("expert", "extensive", …).
They appear too readily in legitimate résumé language, so flagging them would
raise the false-positive rate and erode trust in the gate. Signal-to-noise over
coverage — a check the user learns to ignore is worse than no check.
"""
from __future__ import annotations

import re

# Ownership / seniority verb families → the surface forms we match. A family is
# "introduced" if any of its forms is used as a claim in the tailored text but
# none is in the original. Kept deliberately tight to credit-claiming verbs;
# common, low-signal verbs ("built", "worked", "helped", "developed") are
# excluded on purpose — they carry no seniority claim and would only add noise.
# Base forms ("lead", "own", …) matter as much as past tense: "Lead a team" and
# "Own the roadmap" are the same claim as "Led" / "Owned".
_OWNERSHIP_VERBS: dict[str, tuple[str, ...]] = {
    "led": ("lead", "leads", "led", "leading"),
    "owned": ("own", "owns", "owned", "owning"),
    "architected": ("architect", "architects", "architected", "architecting"),
    "spearheaded": ("spearhead", "spearheads", "spearheaded", "spearheading"),
    "founded": ("found", "founds", "founded", "founding", "founder",
                "cofound", "cofounds", "cofounded", "cofounding", "cofounder"),
    "directed": ("direct", "directs", "directed", "directing"),
    "managed": ("manage", "manages", "managed", "managing"),
    "headed": ("head", "heads", "headed", "heading"),
    "pioneered": ("pioneer", "pioneers", "pioneered", "pioneering"),
    "orchestrated": ("orchestrate", "orchestrates", "orchestrated", "orchestrating"),
    "oversaw": ("oversee", "oversees", "oversaw", "overseen", "overseeing"),
    "mentored": ("mentor", "mentors", "mentored", "mentoring"),
}
_FORM_TO_FAMILY = {f: fam for fam, forms in _OWNERSHIP_VERBS.items() for f in forms}

# --- context rules that keep the claim check quiet on non-claim uses --------
_DET = frozenset({"a", "an", "the"})
_POSS = frozenset({"my", "our", "their", "his", "her", "its", "your", "whose", "one's"})
# "with/by/under … the senior engineer" describes someone else.
_OTHERS = frozenset({"with", "by", "under", "from", "alongside", "among", "beside"})
# Past participles: "<form> by …" is passive — someone else did it.
_PASSIVE = frozenset({"led", "owned", "architected", "spearheaded", "founded",
                      "cofounded", "directed", "managed", "headed", "pioneered",
                      "orchestrated", "overseen", "mentored"})
# Nouns that can name another person's role ("my mentor", "with the architect").
_ROLE_OF_OTHERS = frozenset({"mentor", "architect", "founder", "cofounder"})
_MANAGE_TECH = frozenset({
    "state", "memory", "dependencies", "dependency", "secrets", "secret",
    "configuration", "configurations", "config", "configs", "sessions",
    "session", "connections", "packages", "cache", "caching", "environments",
    "lifecycle", "lifecycles", "service", "services", "database", "databases",
    "instance", "instances", "kubernetes", "cluster", "clusters", "identity",
    "identities", "keys",
})
_ORCHESTRATE_TECH = frozenset({
    "with", "via", "using", "through", "on", "in", "containers", "container",
    "pipelines", "pipeline", "workflows", "workflow", "jobs", "tasks",
    "services", "microservices", "deployments",
})
_DIRECT_TRAFFIC = frozenset({"traffic", "requests", "queries"})
_SKIP_NEXT: dict[str, frozenset[str]] = {
    # causal "lead to" / "led to" / "leading to" is not ownership
    "lead": frozenset({"to", "generation", "gen", "time", "times", "scoring",
                       "nurturing", "magnet", "magnets", "capture",
                       "qualification", "list", "lists", "routing", "poisoning"}),
    "leads": frozenset({"to"}), "led": frozenset({"to"}), "leading": frozenset({"to"}),
    "direct": frozenset({
        "impact", "access", "contact", "deposit", "hire", "hires", "message",
        "messages", "messaging", "communication", "communications", "feedback",
        "marketing", "sales", "traffic", "link", "links", "connection",
        "connections", "integration", "integrations", "exposure", "involvement",
        "interaction", "interactions", "experience", "support", "customer",
        "customers", "client", "clients", "user", "users", "input", "debit",
        "competitor", "competitors", "mail", "line", "lines"}),
    "directs": _DIRECT_TRAFFIC, "directing": _DIRECT_TRAFFIC,
    "directed": _DIRECT_TRAFFIC | {"graph", "graphs", "acyclic", "edge", "edges"},
    "head": frozenset({"node", "nodes", "start", "count", "office", "request", "requests"}),
    "manage": _MANAGE_TECH, "manages": _MANAGE_TECH,
    "managed": _MANAGE_TECH, "managing": _MANAGE_TECH,
    "orchestrate": _ORCHESTRATE_TECH, "orchestrates": _ORCHESTRATE_TECH,
    "orchestrated": _ORCHESTRATE_TECH, "orchestrating": _ORCHESTRATE_TECH,
}
_SKIP_PREV: dict[str, frozenset[str]] = {
    "own": _POSS,                                   # "my own", "their own"
    "lead": _OTHERS,                                # "with lead engineers"
    "leads": frozenset({"qualified", "sales", "inbound", "outbound", "marketing"}),
    "leading": _DET,                                # "a leading fintech"
    "heading": _DET,                                # "a section heading"
    "managed": _DET | {"fully", "self", "centrally", "aws", "gcp", "azure"},
    "direct": _DET | _POSS | {"with", "more", "most", "via", "through", "for",
                              "of", "in", "on"},
    "pioneer": _DET, "pioneering": _DET,            # "a pioneer in payments"
}
# "found" is usually the past tense of "find"; only "found a startup/club/…"
# is the founding claim.
_FOUND_OBJECTS = frozenset({
    "startup", "startups", "company", "companies", "club", "society", "chapter",
    "venture", "business", "firm", "organization", "organisation", "nonprofit",
    "community", "initiative", "lab", "agency", "studio", "team",
})

# --- job titles --------------------------------------------------------------
_TITLE_WORDS = {"senior": "senior", "sr": "senior", "staff": "staff",
                "principal": "principal", "lead": "lead"}
_ROLE_NOUNS = frozenset({
    "engineer", "developer", "programmer", "scientist", "analyst", "architect",
    "manager", "consultant", "designer", "researcher", "specialist",
    "associate", "administrator", "technologist", "sde", "swe", "sre",
})
_ROLE_PLURALS = frozenset({w + "s" for w in _ROLE_NOUNS})
_LEAD_PREFIXES = frozenset({
    "tech", "technical", "team", "engineering", "project", "squad", "dev",
    "development", "module", "feature", "workstream", "frontend", "backend",
    "platform", "product", "qa", "ml", "data", "design",
})
# Words that end a title phrase: "senior to the engineer" is not a title.
_TITLE_BREAK = _DET | _POSS | {"to", "of", "for", "and", "or", "with", "in",
                               "on", "at", "by", "as"}

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?")
_CLAUSE_BREAK_RE = re.compile(r"[.;:!?\n\r•|]")


class _Tok:
    __slots__ = ("low", "raw", "start", "end")

    def __init__(self, m: re.Match):
        self.raw = m.group(0)
        self.low = self.raw.lower().replace("’", "'")
        self.start, self.end = m.start(), m.end()


def _has_term(text: str, term: str) -> bool:
    return re.search(
        r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])",
        (text or "").lower(),
    ) is not None


def _numbers(text: str) -> set[str]:
    """Numeric tokens: integers, decimals, percentages, and shorthand like
    '10x' / '1m' / '5,000'. Commas are stripped so '5,000' and '5000' compare
    equal — otherwise a harmless reformat would flag as a fabricated number."""
    raw = re.findall(r"(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?(?:%|x|k|m|bn|b)?",
                     text or "", flags=re.I)
    return {tok.replace(",", "").lower() for tok in raw if tok}


# --- spelled-out quantities ---------------------------------------------------
_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9}
_SMALL = {**_ONES, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
          "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000,
           "million": 1_000_000, "crore": 10_000_000, "crores": 10_000_000,
           "billion": 1_000_000_000}
_SHORT_SUFFIX = {"thousand": ("k",), "million": ("m",), "billion": ("b", "bn")}


def _alt(words) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


_SPELLED_QTY_RE = re.compile(
    rf"(?<![a-z0-9-])(?P<num>(?:{_alt(_TENS)})(?:[\s-](?:{_alt(_ONES)}))?|{_alt(_SMALL)}|a)"
    rf"(?:\s+(?P<hundred>hundred))?"
    rf"\s+(?P<unit>percent|per\s+cent|{_alt(_SCALES)})(?![a-z])"
)
_MULTIPLIER_RE = re.compile(
    r"(?<![a-z0-9-])(doubl|tripl|quadrupl|halv)(?:ed|ing)(?![a-z])")
_MULTIPLIER_CANON = {"doubl": {"2x", "100%"}, "tripl": {"3x", "200%"},
                     "quadrupl": {"4x", "300%"}, "halv": {"0.5x", "50%"}}
_FOLD_RE = re.compile(
    rf"(?<![a-z0-9-])({_alt({**_SMALL, **_TENS, 'hundred': 100})})[\s-]?fold(?![a-z])")


def _word_value(num: str) -> int:
    if num == "a":
        return 1
    parts = re.split(r"[\s-]+", num)
    return sum(_TENS.get(p, 0) or _SMALL.get(p, 0) for p in parts)


def _spelled_numbers(text: str) -> dict[str, set[str]]:
    """Spelled-out quantities → the digit forms they're equivalent to, so
    'forty percent' matches an original '40%' and 'doubled' matches '2x'."""
    low = (text or "").lower()
    out: dict[str, set[str]] = {}
    for m in _SPELLED_QTY_RE.finditer(low):
        n = _word_value(m.group("num"))
        if m.group("hundred"):
            n *= 100
        unit = re.sub(r"\s+", " ", m.group("unit"))
        if unit in ("percent", "per cent"):
            if m.group("num") == "a":
                continue
            canon = {f"{n}%"}
        else:
            canon = {str(n * _SCALES[unit])}
            canon |= {f"{n}{suf}" for suf in _SHORT_SUFFIX.get(unit, ())}
        out[re.sub(r"\s+", " ", m.group(0))] = canon
    for m in _MULTIPLIER_RE.finditer(low):
        out[m.group(0)] = set(_MULTIPLIER_CANON[m.group(1)])
    for m in _FOLD_RE.finditer(low):
        word = m.group(1)
        n = 100 if word == "hundred" else (_TENS.get(word) or _SMALL[word])
        out[m.group(0)] = {f"{n}x"}
    return out


# --- ownership / title claims -------------------------------------------------
def _same_clause(gap: str) -> bool:
    return not _CLAUSE_BREAK_RE.search(gap)


def _prev(toks: list[_Tok], i: int, text: str) -> _Tok | None:
    if i > 0 and _same_clause(text[toks[i - 1].end:toks[i].start]):
        return toks[i - 1]
    return None


def _next(toks: list[_Tok], i: int, text: str) -> _Tok | None:
    if i + 1 < len(toks) and _same_clause(text[toks[i].end:toks[i + 1].start]):
        return toks[i + 1]
    return None


def _someone_elses(toks: list[_Tok], i: int, text: str) -> bool:
    """True when the word at i names another person's role: "my mentor",
    "with the architect", "under a senior engineer", "Priya's mentor"."""
    p = _prev(toks, i, text)
    if p is None:
        return False
    if p.low in _POSS or p.low in _OTHERS or p.low.endswith("'s"):
        return True
    if p.low in _DET:
        pp = _prev(toks, i - 1, text)
        return pp is not None and (pp.low in _OTHERS or pp.low == "to")
    return False


def _title_claims(toks: list[_Tok], text: str) -> tuple[set[str], set[int]]:
    """Job titles claimed in the text → (families, token indices they use).

    Every title-shaped phrase consumes its tokens, even one describing someone
    else, so "Lead" in "Lead Engineer" is not also counted as the verb "lead".
    """
    found: set[str] = set()
    used: set[int] = set()

    def claim(fam: str, lo: int, hi: int) -> None:
        used.update(range(lo, hi + 1))
        if not _someone_elses(toks, lo, text):
            found.add(fam)

    for i, t in enumerate(toks):
        if i in used:
            continue
        # "Head of Platform"
        if t.low == "head":
            nxt = _next(toks, i, text)
            if nxt is not None and nxt.low == "of":
                claim("head of (title)", i, i + 1)
                continue
        # "Tech Lead", "Team Lead"
        if t.low == "lead" and i > 0 and toks[i - 1].low in _LEAD_PREFIXES \
                and (i - 1) not in used \
                and re.fullmatch(r"[ \t-]+", text[toks[i - 1].end:t.start]):
            nxt = _next(toks, i, text)
            if nxt is None or nxt.low not in _SKIP_NEXT["lead"]:
                claim("lead (title)", i - 1, i)
                continue
        # "Senior Software Engineer", "Sr. Backend Engineer", "Lead SDE"
        fam = _TITLE_WORDS.get(t.low)
        if fam is None:
            continue
        j = i + 1
        while j < len(toks) and j <= i + 4:
            if not re.fullmatch(r"\.?[ \t/&-]+", text[toks[j - 1].end:toks[j].start]):
                break
            w = toks[j].low
            if w in _ROLE_NOUNS:
                claim(f"{fam} (title)", i, j)
                break
            if w in _ROLE_PLURALS or w in _TITLE_BREAK:
                break   # "senior engineers" = other people; "lead the team" = verb
            j += 1
    return found, used


def _is_claim(toks: list[_Tok], i: int, text: str) -> bool:
    t = toks[i]
    w = t.low
    if t.raw in ("LED", "HEAD"):            # the diode / the git ref or HTTP verb
        return False
    prev, nxt = _prev(toks, i, text), _next(toks, i, text)
    # Hyphenated compounds ("data-driven", "industry-leading", "head-to-head",
    # "self-directed") aren't claims — except "co-led", "co-founded", ….
    if t.start >= 2 and text[t.start - 1] == "-" and text[t.start - 2].isalnum():
        if not (prev is not None and prev.low == "co"):
            return False
    if t.end + 1 < len(text) and text[t.end] == "-" and text[t.end + 1].isalnum():
        return False
    if w in _PASSIVE and nxt is not None and nxt.low == "by":
        return False
    if nxt is not None and nxt.low in _SKIP_NEXT.get(w, ()):
        return False
    if prev is not None and prev.low in _SKIP_PREV.get(w, ()):
        return False
    if w == "leads" and prev is not None and prev.low.isdigit():   # "500 leads"
        return False
    if w in _ROLE_OF_OTHERS and _someone_elses(toks, i, text):
        return False
    if w == "found":
        j, k = i, 0
        while k < 4:
            n = _next(toks, j, text)
            if n is None:
                return False
            if n.low in _FOUND_OBJECTS:
                return True
            j, k = j + 1, k + 1
        return False
    return True


def _ownership_families(text: str) -> set[str]:
    text = text or ""
    toks = [_Tok(m) for m in _TOKEN_RE.finditer(text)]
    families, used = _title_claims(toks, text)
    for i, t in enumerate(toks):
        fam = _FORM_TO_FAMILY.get(t.low)
        if fam and fam not in families and i not in used and _is_claim(toks, i, text):
            families.add(fam)
    return families


def _tech_vocab() -> list[str]:
    """Reuse the scoring layer's curated tech vocabulary so the two stay in
    sync. Imported lazily and defensively: grounding must still work if the
    scoring module (or its heavy deps) can't be imported."""
    try:
        from .scoring import TECH_VOCAB
        return list(TECH_VOCAB)
    except Exception:
        return []


def check_grounding(original_resume: str,
                    tailored_text: str,
                    job_missing_keywords: list[str],
                    user_approved_keywords: set[str] | None = None) -> dict:
    approved = {k.lower() for k in (user_approved_keywords or set())}
    missing = job_missing_keywords or []
    missing_lower = {k.lower() for k in missing}

    inserted_unsupported = [
        kw for kw in missing
        if _has_term(tailored_text, kw)
        and not _has_term(original_resume, kw)
        and kw.lower() not in approved
    ]

    # Fabricated skills OUTSIDE the job's missing-keyword set. Excludes anything
    # already reported by inserted_unsupported so a term is flagged once, not twice.
    unsupported_skills = sorted({
        term.lower() for term in _tech_vocab()
        if _has_term(tailored_text, term)
        and not _has_term(original_resume, term)
        and term.lower() not in approved
        and term.lower() not in missing_lower
    })

    # Digits plus spelled-out quantities. A spelled-out one is new only if
    # neither its words nor an equivalent digit form are in the original.
    orig_spelled = _spelled_numbers(original_resume)
    orig_numbers = _numbers(original_resume).union(*orig_spelled.values())
    new_numbers = sorted(
        (_numbers(tailored_text) - orig_numbers)
        | {words for words, canon in _spelled_numbers(tailored_text).items()
           if words not in orig_spelled and not (canon & orig_numbers)}
    )

    inflated_claims = sorted(
        _ownership_families(tailored_text) - _ownership_families(original_resume)
    )

    return {
        "inserted_unsupported": inserted_unsupported,
        "unsupported_skills": unsupported_skills,
        "new_numbers": new_numbers,
        "inflated_claims": inflated_claims,
        "clean": not (inserted_unsupported or unsupported_skills
                      or new_numbers or inflated_claims),
    }
