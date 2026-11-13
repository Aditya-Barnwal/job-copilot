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
import unicodedata

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


# --- confusables / invisible characters ---------------------------------------
# A Cyrillic "е" in "Lеd" or a zero-width space in "Kaf\u200bka" would slip past
# ASCII token checks. For CHECKING, known Cyrillic / Greek look-alikes fold to
# Latin and format (Cf) characters are dropped; generated text that contains
# them is rejected outright (`confusable_problem`).
_CONFUSABLE_MAP = str.maketrans({
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p",
    "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s", "і": "i", "ї": "i", "ј": "j",
    "һ": "h", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ӏ": "l", "ɡ": "g", "ь": "b",
    "А": "A", "В": "B", "Е": "E", "Ё": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X", "Ѕ": "S", "І": "I", "Ї": "I",
    "Ј": "J", "Һ": "H", "Ԁ": "D", "Ԛ": "Q", "Ԝ": "W", "Ӏ": "I",
    # Greek
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x", "γ": "y", "ω": "w",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M",
    "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    # Latin look-alikes outside ASCII
    "ı": "i", "ȷ": "j", "ʟ": "L", "ɪ": "I",
})
_LATIN_MAX = 0x17F          # Basic Latin + Latin-1 + Latin Extended-A


def fold_confusables(text: str) -> str:
    """NFKC, drop format characters (zero-width etc.), map known Cyrillic /
    Greek look-alikes to Latin. For checking only — never shown to the user."""
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf")
    return t.translate(_CONFUSABLE_MAP)


def confusable_problem(text: str, evidence: str = "") -> str | None:
    """'non-Latin or confusable characters' when `text` (after NFKC) has a
    format / zero-width character, or a letter outside Latin / Latin-1 /
    Latin Extended-A that `evidence` (the cited facts) doesn't contain."""
    t = unicodedata.normalize("NFKC", text or "")
    ev = set(unicodedata.normalize("NFKC", evidence or ""))
    if any(unicodedata.category(ch) == "Cf" for ch in t) or any(
            ch.isalpha() and ord(ch) > _LATIN_MAX and ch not in ev for ch in t):
        return "non-Latin or confusable characters"
    return None


def _fold_for_tokens(text: str) -> str:
    """Accent-fold (é -> e), fold look-alikes / drop zero-width characters, and
    normalise Unicode hyphens/dashes — for tokenising only."""
    t = unicodedata.normalize("NFKD", fold_confusables(text)).translate(_DASHES)
    return "".join(ch for ch in t if not unicodedata.combining(ch))


# Markup and links in generated text (shared by tailoring and writing).
MARKUP_RE = re.compile(r"\*\*|__|`|^\s*#|^\s*[-*•]\s|\[[^\]]*\]\([^)]*\)|<[a-z/!][^>]*>|[\\{}]",
                       re.I | re.M)
URL_RE = re.compile(r"(?i:\b(?:https?://|www\.)[^\s<>\"')]+)|(?i:\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+)"
                    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|io|ai|dev|org|net|co|app|in|me|xyz|us|uk|"
                    r"gg|ly|sh|so|tech|site|info|biz)(?:/[^\s<>\"')]*)?(?![A-Za-z0-9])")


def links_in(text: str) -> list[str]:
    """URLs / e-mail addresses / bare domains in `text`, trailing punctuation trimmed."""
    return [m.group(0).rstrip(".,;:!?") for m in URL_RE.finditer(text or "")]


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


# Vague quantities ("millions of", "dozens", "in half", "twice"): a number
# claim without digits. Those with an exact value map to its digit forms; the
# rest ("hundreds", "a handful") only match the same words in the evidence.
_VAGUE_RE = re.compile(
    r"(?<![a-z0-9-])(?:(?:several|a\s+few|few|many)\s+(?:hundred|thousand|million|billion|lakh|crore)s?"
    r"|hundreds|thousands|millions|billions|lakhs|crores|dozens|tens\s+of|scores\s+of"
    r"|a\s+dozen|dozen|a\s+handful|handful|in\s+half|half|a\s+third|one[\s-]third"
    r"|two[\s-]thirds|a\s+quarter|one[\s-]quarter|three[\s-]quarters|twice|thrice)(?![a-z])")
_VAGUE_CANON = {"a dozen": {"12"}, "dozen": {"12"}, "in half": {"50%", "0.5x"},
                "half": {"50%", "0.5x"}, "a third": {"33%"}, "one third": {"33%"},
                "two thirds": {"66%", "67%"}, "a quarter": {"25%"}, "one quarter": {"25%"},
                "three quarters": {"75%"}, "twice": {"2x"}, "thrice": {"3x"}}
# Bare cardinals ("a team of five", "two services"). "one" in idioms ("one of",
# "no one", "one-off") is not a quantity claim.
_BARE_RE = re.compile(
    rf"(?<![a-z0-9-])(?:(?:{_alt(_TENS)})(?:[\s-](?:{_alt(_ONES)}))?|{_alt(_SMALL)})(?![a-z])")
_ONE_IDIOM_NEXT_RE = re.compile(r"\s*-|\s+(?:of|another|more|day|way|by|or|at|thing|point)\b")
_ONE_IDIOM_PREV_RE = re.compile(r"(?:\b(?:the|this|that|each|every|any|no|which|some|at|as|only|a)\s+)$")


def _spelled_numbers(text: str) -> dict[str, set[str]]:
    """Spelled-out quantities → the digit forms they're equivalent to, so
    'forty percent' matches an original '40%' and 'doubled' matches '2x'.
    Vague quantities ("millions of", "dozens") and bare cardinals ("five")
    count too; a vague one with no exact value maps to no digits, so only the
    same words in the evidence support it."""
    low = _fold_for_tokens(text or "").lower()
    out: dict[str, set[str]] = {}
    taken: list[tuple[int, int]] = []

    def free(m: re.Match) -> bool:
        if any(m.start() < e and s < m.end() for s, e in taken):
            return False
        taken.append((m.start(), m.end()))
        return True

    def key(m: re.Match) -> str:
        return re.sub(r"[\s-]+", " ", m.group(0))

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
        if free(m):
            out[re.sub(r"\s+", " ", m.group(0))] = canon
    for m in _MULTIPLIER_RE.finditer(low):
        if free(m):
            out[m.group(0)] = set(_MULTIPLIER_CANON[m.group(1)])
    for m in _FOLD_RE.finditer(low):
        word = m.group(1)
        n = 100 if word == "hundred" else (_TENS.get(word) or _SMALL[word])
        if free(m):
            out[m.group(0)] = {f"{n}x"}
    for m in _VAGUE_RE.finditer(low):
        if free(m):
            out[key(m)] = set(_VAGUE_CANON.get(key(m), ()))
    for m in _BARE_RE.finditer(low):
        if m.group(0) == "one" and (_ONE_IDIOM_NEXT_RE.match(low, m.end())
                                    or _ONE_IDIOM_PREV_RE.search(low[:m.start()])):
            continue
        if free(m):
            out[key(m)] = {str(_word_value(m.group(0)))}
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


# Scope claims: how much of the work was the applicant's, or how big it was.
# Like ownership verbs, each needs the same cue in the evidence.
_SCOPE_RES = tuple((re.compile(rx, re.I), fam) for rx, fam in (
    (r"\bteams?\s+of\b", "team of (scope)"),
    (r"\bsole(?:ly)?\b|\bby myself\b|\bon my own\b", "sole (scope)"),
    (r"\bsingle[\s-]?handed(?:ly)?\b", "single-handedly (scope)"),
    (r"\bindependently\b", "independently (scope)"),
    (r"\bsolo\b", "solo (scope)"),
    (r"\ball\s+of\s+(?:the|its|our|their)\b|\bentire\b|"
     r"\b(?:company|org|organi[sz]ation|team)[\s-]wide\b", "company-wide (scope)"),
    (r"\bsenior[\s-](?:grade|level|quality)\b", "senior (title)"),
))


def _ownership_families(text: str) -> set[str]:
    text = _fold_for_tokens(text or "")
    toks = [_Tok(m) for m in _TOKEN_RE.finditer(text)]
    families, used = _title_claims(toks, text)
    for i, t in enumerate(toks):
        fam = _FORM_TO_FAMILY.get(t.low)
        if fam and fam not in families and i not in used and _is_claim(toks, i, text):
            families.add(fam)
    families |= {fam for rx, fam in _SCOPE_RES if rx.search(text)}
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
    original_resume = _fold_for_tokens(original_resume)
    tailored_text = _fold_for_tokens(tailored_text)
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


# =========================================================================== v3
# Grounding v3 (Phase 3, PLAN D4): structural, per line. Every output line of a
# tailored résumé cites the fact ids it came from, and each line is checked
# against the text of THOSE facts only (plus keywords the user attested), not
# against the whole résumé. A line is supported only when every tech term,
# number, ownership/title claim and proper noun in it appears in its cited
# facts. A line citing no fact, or a fact that isn't confirmed/usable, is
# unsupported. Any unsupported line makes the result unclean: fail closed.
#
# Similarity guard (Resume-Matcher idea): a rephrased line whose token overlap
# (Jaccard over stemmed content words) with its source facts is below
# SIMILARITY_FLOOR is flagged even if no single token is new — a rewrite that
# far from the source needs a human look.

SIMILARITY_FLOOR = 0.45

# Flag lists in a check_lines() result. Every one except `unknown_facts` can be
# confirmed token-by-token by the user at approval; a line citing a fact that
# isn't usable can never be approved (tailoring refuses unconfirmed facts).
LINE_FLAG_KEYS = ("unsupported_skills", "new_numbers", "inflated_claims",
                  "new_proper_nouns", "unsupported_claims", "low_similarity", "unknown_facts")
UNCONFIRMABLE_FLAGS = frozenset({"unknown_facts"})

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.'’+#-][A-Za-z0-9]+)*")
_SENTENCE_START_RE = re.compile(r"(?:^|[.!?:;•|(\"]\s*|\s[-–—]\s+)$")
_STOPWORDS = frozenset("""
a an the and or of to in on for with by at from as into via over under across
per is are was were be been being this that these those it its their our my
using used use than then so such which who whom while within without
""".split())
# Capitalised words that are not names. Kept small on purpose: a word outside
# the cited facts is flagged unless it is listed here or the user attests it.
_COMMON_CAPS = frozenset({"i", "a", "an", "the", "and", "or", "of", "in", "on",
                          "to", "for", "with", "by", "at", "as", "via"})


def _fact_evidence(f) -> str:
    """Everything a line citing `f` may draw on: its text, its tech line and
    its skill tags (each tag must already appear in the text — facts.py)."""
    if isinstance(f, str):
        return f
    parts = [getattr(f, "text", "") or ""]
    parts += list(getattr(f, "tech", []) or [])
    parts += list(getattr(f, "skills", []) or [])
    return " | ".join(p for p in parts if p)


def _fact_map(facts) -> dict:
    """{id: Fact | str} from a mapping, an iterable of Facts, or a ResumeFacts."""
    if facts is None:
        return {}
    if hasattr(facts, "by_id") and callable(facts.by_id):
        return dict(facts.by_id())
    if isinstance(facts, dict):
        return dict(facts)
    return {f.id: f for f in facts if getattr(f, "id", None)}


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _content_tokens(text: str) -> set[str]:
    return {_stem(w.lower()) for w in _WORD_RE.findall(text or "")
            if w.lower() not in _STOPWORDS} | set(_numbers(text))


def token_overlap(a: str, b: str) -> float:
    """Jaccard overlap of stemmed content words (+ numbers); 1.0 for two empties."""
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


def _skill_spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, canonical) for every known skill / tech term in `text`."""
    spans: list[tuple[int, int, str]] = []
    try:
        from .skills import _matches
        spans += list(_matches(text))
    except Exception:
        pass
    low = (text or "").lower()
    for term in _tech_vocab():
        for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])", low):
            spans.append((m.start(), m.end(), term))
    return spans


def _attested_terms(attested) -> set[str]:
    out: set[str] = set()
    for k in attested or ():
        k = str(k).strip()
        if not k:
            continue
        out.add(k.lower())
        try:
            from .skills import canonical
            c = canonical(k)
            if c:
                out.add(c.lower())
        except Exception:
            pass
    return out


def _attested_words(attested) -> set[str]:
    return {w.lower() for k in attested or () for w in _WORD_RE.findall(str(k))}


def _plain(word: str) -> str:
    w = word.lower().replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _line_skills(text: str, evidence: str, attested: set[str]) -> list[str]:
    ev = {c.lower() for _, _, c in _skill_spans(evidence)}
    out: dict[str, str] = {}
    for _, _, canon in _skill_spans(text):
        c = canon.lower()
        if c in ev or c in attested or _has_term(evidence, canon):
            continue
        out.setdefault(c, canon)
    return list(out.values())


def _line_numbers(text: str, evidence: str) -> list[str]:
    ev_spelled = _spelled_numbers(evidence)
    ev_nums = _numbers(evidence).union(*ev_spelled.values())
    return sorted((_numbers(text) - ev_nums)
                  | {w for w, canon in _spelled_numbers(text).items()
                     if w not in ev_spelled and not (canon & ev_nums)})


def _line_proper_nouns(text: str, evidence: str, attested_words: set[str]) -> list[str]:
    """Capitalised / mixed-case words not in the cited facts. Sentence-initial
    words are skipped unless mixed-case ("OpenAI", "AWS"), since a rephrase
    normally opens with a capitalised verb; tech terms are left to the skill
    check so one token is never reported twice."""
    ev_words = {_plain(w) for w in _WORD_RE.findall(evidence or "")}
    covered = [(s, e) for s, e, _ in _skill_spans(text)]
    out: dict[str, None] = {}
    for m in _WORD_RE.finditer(text or ""):
        w = m.group(0)
        if not w[0].isupper() and not any(c.isupper() for c in w[1:]):
            continue
        if any(s <= m.start() < e for s, e in covered):
            continue
        mixed = any(c.isupper() for c in w[1:])
        if not mixed and _SENTENCE_START_RE.search(text[:m.start()]):
            continue
        p = _plain(w)
        if w.lower() in _TITLE_WORDS or w.lower() in _ROLE_NOUNS or p in _ROLE_NOUNS:
            continue    # titles are the claim check's job ("Senior Engineer")
        if p in ev_words or w.lower() in _COMMON_CAPS or w.lower() in attested_words \
                or p in attested_words:
            continue
        out.setdefault(w, None)
    return list(out)


_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), "-")


# --------------------------------------------------------------------------- capability claims
# A sentence about the applicant that uses a capability / experience cue
# ("experience with …", "scaled … to production", "end-to-end", "showcasing …",
# "expertise in …", "years of …", "track record …") must have the CLAIMED THING
# — the words after the cue, or the few words before it when the cue ends the
# clause ("my backend ownership") — supported by the text of the facts the line
# cites: at least CLAIM_SUPPORT_FLOOR of its content words must appear there.
# Some cues are claims in themselves (production, scaling, ownership, led,
# architected, end-to-end, years of) and also need the cue in the cited facts.
# When the sentence names one of the applicant's projects / employers before
# the cue ("At Fitness Redefined I built X, showcasing Y"), Y must come from
# THAT project's own facts (wrong-project attribution). Pseudo-facts whose id
# starts with "_" (a letter's role / JD context) never count as evidence here.
# Bridging generalisations ("This experience demonstrates …") are flagged
# outright: by construction they restate nothing a fact says.

CLAIM_SUPPORT_FLOOR = 0.6

_CLAIM_CUES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # (cue regex, family, regexes that must ALL match the evidence)
    # "my / years of / strong experience …" or "experience in|with|… X" / "experience scaling X";
    # not a forward-looking "applying these experiences to <the company>"
    (r"\b(?:my|years of|extensive|strong|deep|solid|hands[- ]on|prior|proven|professional)\s+"
     r"experiences?\b(?:\s+(?:in|with|of|on|at|across))?|"
     r"\bexperienced?\s+(?:in|with|of|on|at|across)\b|\bexperiences?\s+(?=[a-z]+ing\b)",
     "experience", ()),
    (r"\bscal(?:e|es|ed|ing)\b(?=[^.;:!?]{0,80}?\b(?:to|in|into)\s+production\b)", "scaling",
     (r"\bscal(?:e|es|ed|ing|able|ability)\b", r"\bproduction\b")),
    (r"\bproduction(?:[- ](?:grade|ready|scale))?\b", "production", (r"\bproduction\b",)),
    (r"\b(?:ownership|owned|owning)\b", "owned", (r"\bown(?:s|ed|ing|ership)?\b",)),
    (r"\bend[- ]to[- ]end\b", "end-to-end", (r"\bend[- ]to[- ]end\b",)),
    (r"\bled\b(?!\s+to\b)", "led", (r"\b(?:lead|leads|led|leading)\b",)),
    (r"\barchitect(?:ed|ing)\b", "architected", (r"\barchitect",)),
    (r"\b(?:experts?|expertise|proficien(?:t|cy)|extensive|in-depth|strong background|"
     r"deep(?!\s+(?:learning|link|links|dive|dives|research)\b))\b", "expertise", ()),
    (r"\b(?:showcas|demonstrat|highlight|reinforc|underscor|illustrat)\w*|\bprov(?:e|es|en|ing)\b",
     "showcasing", ()),
    (r"\byears? of\b", "years", (r"\byears?\b",)),
    (r"\btrack record\b(?:\s+(?:of|in))?", "track record", ()),
)
_CLAIM_CUE_RES = [(re.compile(rx, re.I), fam, tuple(re.compile(r, re.I) for r in req))
                  for rx, fam, req in _CLAIM_CUES]
# "This (experience|work) demonstrates …" — a generalisation, never a fact.
_BRIDGE_RE = re.compile(
    r"(?:^|[,;]\s*(?:and\s+)?|\band\s+)(?:all\s+of\s+)?(?:this|that|these|those|it|which|such)\b"
    r"(?:\s+[A-Za-z-]+){0,3}?\s+(?:demonstrat|showcas|shows?\b|prov|highlight|reflect|underscor|"
    r"reinforc|illustrat|speaks?\b)\w*", re.I)
_CLAIM_STOP_RE = re.compile(r"[.;:!?()]|,\s*(?:which|while|but|where|so|and\s+i)\b|\s[-–—]\s")
_CLAIM_FILLER = frozenset("""
skill skills ability abilities capability capabilities need needed needs require required
requirement requirements role roles team teams work experience experiences background knowledge
strong solid hands real world various multiple wide range kind type thing things area areas
field fields set level levels i i'm my me mine your you we our us also both well like more most
own applicant candidate job position company
""".split())
_CLAIM_BACK_STOP = _DET | _POSS | _STOPWORDS | {"i", "me", "and", "the"}


def _claim_stem(w: str) -> str:
    w = w.lower().replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    for suf in ("ing", "ed"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    return w[:-1] if len(w) > 4 and w.endswith("e") else w


def _claim_tokens(text: str) -> set[str]:
    """Content stems for claim support: hyphens/slashes split, filler dropped."""
    t = re.sub(r"[-/]", " ", text or "")
    out = {_claim_stem(w) for w in _WORD_RE.findall(t)
           if w.lower() not in _STOPWORDS and w.lower() not in _CLAIM_FILLER}
    return {w for w in out if w} | set(_numbers(text))


def _claim_object(text: str, start: int, end: int) -> tuple[str, str]:
    """(claimed thing, phrase as written) for a cue at text[start:end]: what
    follows it up to the clause end (<= 12 words), else up to 3 words before it
    when the cue ends the clause ("my backend ownership")."""
    rest = text[end:]
    m = _CLAIM_STOP_RE.search(rest)
    after = rest[: m.start()] if m else rest
    words = _WORD_RE.findall(after)[:12]
    if _claim_tokens(" ".join(words)):
        last = list(_WORD_RE.finditer(after))[len(words) - 1]
        return " ".join(words), text[start: end + last.end()].strip()
    before = list(_WORD_RE.finditer(text[:start]))
    back: list[re.Match] = []
    for w in reversed(before[-3:]):
        if w.group(0).lower() in _CLAIM_BACK_STOP:
            break
        back.insert(0, w)
    if not back:
        return "", text[start:end]
    return " ".join(w.group(0) for w in back), text[back[0].start(): end]


def _header_index(fmap: dict) -> tuple[dict[str, list[str]], dict[str, list]]:
    """({header id: [names]}, {header id: [its bullet facts]}) for named
    projects / employers / hackathons among the facts."""
    names: dict[str, list[str]] = {}
    kids: dict[str, list] = {}
    for fid, f in fmap.items():
        if isinstance(f, str) or str(fid).startswith("_"):
            continue
        parent = getattr(f, "parent", "")
        if parent:
            kids.setdefault(parent, []).append(f)
            continue
        ns = [str(getattr(f, a, "") or "").strip() for a in ("name", "company")]
        ns = [n for n in ns if len(n) >= 3]
        if ns:
            names[fid] = ns
    return names, kids


def capability_claims(text: str, evidence: str, *, fmap: dict | None = None,
                      header_index=None, floor: float = CLAIM_SUPPORT_FLOOR,
                      skip_families: set[str] = frozenset()) -> list[str]:
    """Unsupported capability / experience claims in `text` (an applicant
    sentence or résumé line), judged against `evidence` (the cited facts' text).
    Returns the offending claim phrases as written (see the block comment)."""
    text = _fold_for_tokens(text)
    evidence = _fold_for_tokens(evidence)
    ev_tokens = _claim_tokens(evidence)
    names, kids = header_index if header_index is not None else _header_index(fmap or {})
    out: dict[str, None] = {}

    for m in _BRIDGE_RE.finditer(text):
        clause = text[m.start():]
        stop = _CLAIM_STOP_RE.search(clause[len(m.group(0)):])
        phrase = clause[: len(m.group(0)) + (stop.start() if stop else len(clause))]
        out.setdefault(phrase.strip(" ,;").strip(), None)

    named: list[tuple[int, str, str]] = []          # (position, header id, name)
    for hid, ns in names.items():
        for n in ns:
            mm = re.search(r"(?<![A-Za-z0-9])" + re.escape(_fold_for_tokens(n)) + r"(?![A-Za-z0-9])",
                           text, re.I)
            if mm:
                named.append((mm.start(), hid, n))
                break
    for rx, fam, required in _CLAIM_CUE_RES:
        for m in rx.finditer(text):
            obj, phrase = _claim_object(text, m.start(), m.end())
            before = [x for x in named if x[0] < m.start()]
            if before:                              # "At <project> …, showcasing Y"
                _, hid, name = max(before)
                own = [fmap[hid]] if fmap and hid in fmap else []
                own += kids.get(hid, [])
                ev = _fold_for_tokens("\n".join(_fact_evidence(f) for f in own))
                where = f" (not in {name} facts)"
            else:
                ev, where = evidence, ""
            ev_t = _claim_tokens(ev) if before else ev_tokens
            if fam not in skip_families and not all(r.search(ev) for r in required):
                out.setdefault(phrase + where, None)
                continue
            obj_t = _claim_tokens(obj) - _claim_tokens(m.group(0))
            if obj_t and len(obj_t & ev_t) / len(obj_t) < floor:
                out.setdefault(phrase + where, None)
    # one report per claim: drop a phrase that is part of a longer flagged one
    kept: list[str] = []
    for ph in sorted(out, key=len, reverse=True):
        core = ph.split(" (not in ")[0].lower()
        if not any(core in k.lower() for k in kept):
            kept.append(ph)
    return [ph for ph in out if ph in kept]


def check_lines(lines: list[dict], facts, *, attested=None,
                similarity_floor: float = SIMILARITY_FLOOR,
                claim_floor: float = CLAIM_SUPPORT_FLOOR) -> dict:
    """Per-line grounding verdicts for a tailored résumé.

    `lines`: [{text, fact_ids, rephrased?: bool, source_text?: str}] in output
    order. `facts`: the USABLE facts (a list of Facts, a ResumeFacts, or a
    mapping id -> Fact | text); a cited id missing from it counts as an
    unconfirmed fact. `attested`: keywords the user says are true of them —
    allowed in any line as skills and proper nouns (never as numbers).

    Returns {clean, lines: [verdict], flags: {key: [tokens]}, unsupported_lines}.
    Each verdict: {index, text, fact_ids, supported, similarity, unknown_facts,
    unsupported_skills, new_numbers, inflated_claims, new_proper_nouns,
    low_similarity, offending}. The similarity guard applies to lines marked
    `rephrased` (or carrying `source_text`) and reports the line's fact ids.
    `unsupported_claims` (capability_claims) runs on every line unless it sets
    `check_claims: False` (a letter sentence that isn't about the applicant);
    its evidence is the cited facts whose id doesn't start with "_".
    A line may carry `names`: strings whose words are allowed as proper nouns
    in that line only — never as skills, numbers or claims.
    """
    fmap = _fact_map(facts)
    att_terms = _attested_terms(attested)
    att_words = _attested_words(attested)
    hidx = _header_index(fmap)
    verdicts: list[dict] = []
    agg: dict[str, dict[str, None]] = {k: {} for k in LINE_FLAG_KEYS}
    for i, line in enumerate(lines or []):
        raw_text = str((line or {}).get("text") or "")
        # Fold accents + Unicode dashes BEFORE tokenising: the word patterns are ASCII,
        # so "résumé" would otherwise split into "r", "sum" and false-flag the skill R.
        text = _fold_for_tokens(raw_text)
        ids = [str(x) for x in ((line or {}).get("fact_ids") or []) if x]
        unknown = [x for x in ids if x not in fmap]
        cited = [fmap[x] for x in ids if x in fmap]
        evidence = _fold_for_tokens("\n".join(_fact_evidence(f) for f in cited))
        v = {"index": i, "text": raw_text, "fact_ids": ids,
             "unknown_facts": unknown if ids else ["(no fact cited)"],
             "unsupported_skills": _line_skills(text, evidence, att_terms),
             "new_numbers": _line_numbers(text, evidence),
             "inflated_claims": sorted(_ownership_families(text) - _ownership_families(evidence)),
             "new_proper_nouns": _line_proper_nouns(text, evidence, att_words | {
                 w.lower() for n in (line.get("names") or ()) for w in _WORD_RE.findall(_fold_for_tokens(str(n)))}),
             "unsupported_claims": [], "similarity": None, "low_similarity": []}
        if line.get("check_claims", True):
            real = "\n".join(_fact_evidence(fmap[x]) for x in ids
                              if x in fmap and not x.startswith("_"))
            v["unsupported_claims"] = capability_claims(
                text, real, fmap=fmap, header_index=hidx, floor=claim_floor,
                skip_families=set(v["inflated_claims"]))
        if line.get("rephrased") or line.get("source_text"):
            src = str(line.get("source_text") or "\n".join(getattr(f, "text", str(f)) for f in cited))
            v["similarity"] = round(token_overlap(text, src), 3)
            if v["similarity"] < similarity_floor:
                v["low_similarity"] = list(ids) or ["(no fact cited)"]
        v["offending"] = [t for k in LINE_FLAG_KEYS for t in v[k]]
        v["supported"] = not v["offending"]
        for k in LINE_FLAG_KEYS:
            for t in v[k]:
                agg[k].setdefault(t, None)
        verdicts.append(v)
    flags = {k: list(d) for k, d in agg.items()}
    bad = [v["index"] for v in verdicts if not v["supported"]]
    return {"clean": not bad, "lines": verdicts, "flags": flags, "unsupported_lines": bad}


def unconfirmed_line_flags(grounding: dict, confirmed) -> dict[str, list[str]]:
    """Flags in a check_lines() result (plus any extra flag lists in it) that
    `confirmed` ({key: [tokens]}, case-insensitive) doesn't cover. Flags in
    UNCONFIRMABLE_FLAGS are always returned. Malformed input = nothing confirmed."""
    given = confirmed if isinstance(confirmed, dict) else {}
    out: dict[str, list[str]] = {}
    for key, items in (grounding.get("flags") or {}).items():
        raw = given.get(key)
        ok = ({str(x).strip().lower() for x in raw}
              if isinstance(raw, list) and key not in UNCONFIRMABLE_FLAGS else set())
        missing = [x for x in items or [] if str(x).strip().lower() not in ok]
        if missing:
            out[key] = missing
    return out


# --------------------------------------------------------------------------- metric normalisation
# A rephrase that writes a fact's metric another way ("over a million" for
# "1M+") is put back to the fact's exact token — but only when that is
# unambiguous. Never produces a value the facts don't have.

_NM_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
             "b": 1e9, "bn": 1e9, "billion": 1e9}
_NM_METRIC_RE = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|m|mn|b|bn|%)?\s*(\+)?$", re.I)
_NM_SPAN_RE = re.compile(
    r"(?<![A-Za-z0-9.,])(?P<pre>(?:over|more\s+than|upwards\s+of|in\s+excess\s+of|north\s+of)\s+)?"
    rf"(?P<num>\d{{1,3}}(?:,\d{{3}})+|\d+(?:\.\d+)?|an?|(?:{_alt(_TENS)})(?:[\s-](?:{_alt(_ONES)}))?|{_alt(_SMALL)})"
    r"(?:\s*(?P<unit>%|percent|per\s+cent|k|mn|m|bn|b|thousand|million|billion)(?![A-Za-z]))?"
    r"(?P<plus>\s*\+)?(?![A-Za-z0-9])", re.I)


def _nm_value(num: str, unit: str | None) -> tuple[float, str] | None:
    num = num.lower().replace(",", "")
    if num in ("a", "an"):
        if not unit or unit.lower() in ("%", "percent") or re.match(r"per\s+cent", unit, re.I):
            return None
        n = 1.0
    elif num[0].isdigit():
        n = float(num)
    else:
        n = float(_word_value(num))
    u = (unit or "").lower()
    if u in ("%", "percent") or u.startswith("per"):
        return n, "%"
    return n * _NM_SCALE.get(u, 1), ""


def normalize_metrics(text: str, metrics) -> tuple[str, list[dict]]:
    """Rewrite another spelling of a fact metric ("over 1 M", "over 1 million",
    "more than one million", "over a million") to the fact's exact token
    ("1M+"). Only when unambiguous: the token is not already in the text,
    exactly one span expresses that value (and its "+"/"over"), and no other
    metric in `metrics` shares the value. Returns (text, [{from, to}])."""
    text = text or ""
    parsed = []
    for tok in metrics or []:
        m = _NM_METRIC_RE.match(str(tok).strip())
        if not m:
            continue
        unit = m.group(2) or ""
        val = _nm_value(m.group(1), None if unit == "%" else unit)
        if unit == "%":
            val = (float(m.group(1).replace(",", "")), "%")
        if val is not None:
            parsed.append((str(tok).strip(), val, bool(m.group(3))))
    changes: list[dict] = []
    for tok, val, plus in parsed:
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])", text) or \
                sum(1 for _t, v, _p in parsed if v == val) > 1:
            continue
        spans = []
        for m in _NM_SPAN_RE.finditer(text):
            if not (m.group("pre") or m.group("plus") or m.group("unit") or not m.group("num")[0].isdigit()):
                continue                         # a bare digit run: nothing to normalise
            v = _nm_value(m.group("num"), m.group("unit"))
            if v is None or v != val or bool(m.group("pre") or m.group("plus")) != plus:
                continue
            spans.append(m)
        if len(spans) != 1:
            continue
        m = spans[0]
        old = m.group(0)
        if old.strip() == tok:
            continue
        text = text[:m.start()] + tok + text[m.end():]
        changes.append({"from": old.strip(), "to": tok})
    return text, changes

