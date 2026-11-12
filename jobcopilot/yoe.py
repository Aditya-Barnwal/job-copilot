"""Years-of-experience extractor for job descriptions (YOE v2).

Regex-based and deterministic. What v2 adds over v1:

  * Section anchoring. Headings such as "Requirements", "Minimum qualifications",
    "What we look for", "Must have" open a REQUIRED section; "Preferred",
    "Nice to have", "Bonus" open a PREFERRED one; "Responsibilities",
    "About us", "Benefits" ... open an OTHER one. A bar found in the required
    section beats bars found elsewhere (company blurbs, other levels).
  * Formats: "1.5+", "0-2", "0–2", "2–12+", "1 to 3", "between 1 and 3",
    "at least one year", "one to two years", "three years (3)", "6+ months",
    "no more than 18 months" (a cap: min 0).
  * Fresher signals: fresher, new grad, recent graduates, graduate program,
    entry level, early career, no experience required, class of 2026; and
    negations ("if you are a new grad, please do not apply").
  * Title-aware choice (F10): on a junior title (II / I / Junior / Associate /
    New Grad ...) the LOWEST hard floor wins - the Affirm miss was
    "Software Engineer II ... 1.5+ years" read as 5. On senior and neutral
    titles the STRICTEST hard floor wins.
  * Evidence: the clause the bar came from (<= 160 chars).

The v1 guards stay: founding years, "since", combined/team totals, degree
lengths, bonds/contracts, "within 2 years of graduation" are never a YOE bar.
`extract_min_yoe` keeps its v1 signature for existing callers.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

EVIDENCE_MAX = 160

# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------
_DASHES = re.compile(r"[‐-―−﹘﹣－]")
_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "eighteen": 18,
}
_NUM_WORD_RE = re.compile(
    r"\b(" + "|".join(_NUM_WORDS) + r")\b(?=[^.\n]{0,25}?\b(?:years?|yrs?|months?)\b)", re.I)
_A_YEAR_RE = re.compile(r"\b(?:a|an)\s+(?=(?:full\s+)?(?:year|yr)\b)", re.I)
_DUP_PAREN_RE = re.compile(r"\b(\d{1,2})\s*\(\s*\1\s*\)")      # "3 (3) years" -> "3 years"


def _normalise(text: str) -> str:
    """Same-length-insensitive cleanup; positions are not reused afterwards."""
    t = _DASHES.sub("-", text).replace(" ", " ")
    t = _NUM_WORD_RE.sub(lambda m: str(_NUM_WORDS[m.group(1).lower()]), t)
    t = _A_YEAR_RE.sub("1 ", t)
    t = _DUP_PAREN_RE.sub(r"\1", t)
    # "three years (3) of experience" -> drop the echoed "(3)" after the unit
    t = re.sub(r"\b((?:years?|yrs?))\s*\(\s*\d{1,2}\s*\)", r"\1", t, flags=re.I)
    return t


# --------------------------------------------------------------------------
# The YOE mention itself
# --------------------------------------------------------------------------
_YOE_RE = re.compile(
    r"""
    (?P<prefix>\b(?:at\s+least|minimum(?:\s+of)?|min\.?|over|more\s+than|upwards\s+of|
                  between|no\s+more\s+than|not\s+more\s+than|up\s+to|maximum(?:\s+of)?|
                  max\.?|less\s+than|under|fewer\s+than)\s+)?
    (?<![\d.])(?P<lo>\d{1,2}(?:\.\d{1,2})?)
    (?:\s*\+)?
    (?:\s*(?:-|to|or|and)\s*(?P<hi>\d{1,2}(?:\.\d{1,2})?)(?:\s*\+)?)?
    \s*(?:\+|or\s+more|plus)?\s*
    (?P<unit>years?|yrs?|months?|mos)\b
    """,
    re.I | re.X,
)
_CAP_PREFIX = ("no more than", "not more than", "up to", "maximum", "max", "less than",
               "under", "fewer than")

# Context: the clause must be about job experience.
_EXPERIENCE_CTX = re.compile(
    r"experience|\bexp\b|\bexp\.|expertise|background|professional|industry|hands[- ]on|"
    r"track record|\bworking\b|\bwork(?:ed)?\s+(?:as|in|on|with)|\bbuilding\b|\bdeveloping\b|"
    r"\bcoding\b|\bprogramming\b|\bin\s+(?:a\s+|the\s+)?(?:software|engineering|backend|similar|"
    r"relevant|related|production)|\bas\s+an?\s+\w+|\brequired\b|\bminimum\b|\bpreferred\b",
    re.I)

# Guards: text right BEFORE the number that makes it not a YOE bar.
_GUARD_BEFORE = re.compile(
    r"(?:founded|since|combined|collectively|cohort|established|company\s+has|we\s+have\s+been|"
    r"we've\s+been|team\s+(?:has|have|with|brings)|over\s+the\s+(?:past|last|next)|"
    r"(?:in|for|during)\s+the\s+(?:past|last|next|first)|within(?:\s+the\s+(?:past|last))?|"
    r"for\s+over|graduated\s+(?:in|within)|bond\s+(?:of|for)|commit(?:ment)?\s+(?:of|to|for)|"
    r"(?:vesting|vests?|cliff)\s+(?:over|of)|warranty\s+of|contract\s+(?:of|for))"
    r"[^.\n\d]{0,12}$",
    re.I)
# ... and right AFTER the unit.
_GUARD_AFTER = re.compile(
    r"^[\s']*(?:old\b|ago\b|in\s+business|of\s+(?:operation|operations|history|existence|growth|"
    r"profitab\w*|study|school\w*|education|college|university|undergrad\w*|graduation|"
    r"service\s+bond)|since\s+graduat|after\s+graduat|post[- ]?graduat|"
    r"degree|college|university|program(?:me)?\b|course|bond\b|contract\b|warranty|vesting|"
    r"cliff|guarantee|commitment|running|strong\b|[^.\n]{0,30}\b(?:combined|collectively|"
    r"between\s+us|as\s+a\s+team))",
    re.I)

_SOFT_RE = re.compile(
    r"\bpreferred\b|\bpreferably\b|nice[- ]to[- ]have|\b(?:a|big|huge|strong|added)\s+plus\b|"
    r"\bideally\b|\bbonus\b|good[- ]to[- ]have|\bdesir(?:ed|able)\b|\badvantage(?:ous)?\b|"
    r"\bbeneficial\b|would\s+be\s+(?:great|nice|helpful|ideal)|not\s+(?:required|mandatory|a\s+must)|"
    r"\boptional\b",
    re.I)

# A bar stated for another level ("5+ years for senior candidates").
_OTHER_LEVEL_RE = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|lead|architect|manager|director)\b|\bL[4-9]\b|"
    r"\b(?:SDE|SE|engineer)\s*(?:III|IV|[3-9])\b", re.I)

# --------------------------------------------------------------------------
# Fresher signals
# --------------------------------------------------------------------------
_FRESHER_RE = re.compile(
    r"\bfreshers?\b|\bnew[- ]grad(?:uate)?s?\b|\bnew\s+college\s+grad(?:uate)?s?\b|"
    r"\brecent\s+(?:college\s+|university\s+)?grad(?:uate)?s?\b|"
    r"\b(?:university|college|campus)\s+(?:grad(?:uate)?s?|hires?|hiring|recruit\w*)\b|"
    r"\bgraduate\s+(?:engineer|program(?:me)?|role|position|hire|scheme|software|developer|trainee)\b|"
    r"\bearly[- ]career\b|\bentry[- ]level\b|"
    r"\bno\s+(?:prior\s+|previous\s+|professional\s+|work\s+|industry\s+)?experience\s+"
    r"(?:is\s+)?(?:required|necessary|needed)\b|\bclass\s+of\s+20\d\d\b|"
    r"\b(?:20\d\d|recent)\s+(?:pass[- ]?outs?|batch)\b",
    re.I)
_FRESHER_NEG_RE = re.compile(
    r"do\s+not\s+apply|don't\s+apply|not\s+(?:eligible|suitable|open|considered|considering|accepting|"
    r"a\s+(?:new[- ]grad|fresher|entry[- ]level))|no\s+freshers|freshers\s+need\s+not|"
    r"cannot\s+(?:consider|accept)|won't\s+be\s+considered|will\s+not\s+be\s+considered|"
    r"not\s+for\s+(?:freshers|new\s+grads)|is\s+not\s+an?\s+(?:entry|junior)",
    re.I)

_TITLE_FRESHER_RE = re.compile(
    r"\bintern(?:ship)?s?\b|\bnew[- ]grad|\bgraduate\b|\bentry[- ]level\b|\bearly[- ]career\b|"
    r"\bfreshers?\b|\btrainee\b|\bapprentice|\buniversity\b|\bcampus\b|"
    r"\b(?:engineer|developer|sde|swe|scientist)\s*[-,]?\s*(?:i|1)\b|\bsde\s*-?\s*(?:i|1)\b|\bL[12]\b",
    re.I)
_TITLE_JUNIOR_RE = re.compile(
    r"\bjunior\b|\bjr\.?\b|\bassociate\b|"
    r"\b(?:engineer|developer|sde|swe|scientist|analyst)\s*[-,]?\s*(?:ii|2)\b|\bsde\s*-?\s*(?:ii|2)\b|\bL3\b",
    re.I)
_TITLE_SENIOR_RE = re.compile(
    r"\bsenior\b|\bsr\b\.?|\bstaff\b|\bprincipal\b|\bdistinguished\b|\bdirector\b|\barchitect\b|"
    r"\blead\b|\bmanager\b|\bhead\s+of\b|\bvp\b|\bvice\s+president\b|\bL[4-9]\b|\bSDE\s*-?\s*(?:III|[3-9])\b|"
    r"\b(?:engineer|developer|scientist|sde|analyst|researcher)\s*[-,]?\s*(?:iii|iv|v|vi|[3-9])\b",
    re.I)

# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------
REQUIRED, PREFERRED, OTHER, NONE = "required", "preferred", "other", "none"

_PREF_HEAD = (
    r"preferred|nice[- ]to[- ]haves?|bonus(?:\s+points)?|good[- ]to[- ]haves?|pluses|extra\s+credit|"
    r"brownie\s+points|added\s+advantage|sets\s+you\s+apart|stand\s+out|even\s+better|"
    r"desired\s+(?:qualifications|skills|experience)|additional\s+(?:qualifications|skills)|"
    r"would\s+be\s+(?:great|nice)|ideally\s+you"
)
_REQ_HEAD = (
    r"requirements?|(?:minimum|basic|required|key|essential|job)?\s*qualifications?|"
    r"what\s+you(?:'ll|\s+will)?\s+need|what\s+you\s+bring|what\s+we(?:'re|\s+are)?\s+looking\s+for|"
    r"what\s+we\s+look\s+for|who\s+we(?:'re|\s+are)\s+looking\s+for|who\s+you\s+are|must[- ]haves?|"
    r"you\s+(?:have|should\s+have|will\s+have|bring|might\s+be\s+a\s+fit)|about\s+you|your\s+profile|"
    r"(?:skills|experience)\s+(?:and|&)\s+(?:experience|skills|qualifications)|eligibility|"
    r"desired\s+candidate\s+profile|candidate\s+profile|ideal\s+(?:candidate|profile)|"
    r"required\s+skills|experience\s+required|what\s+it\s+takes|to\s+be\s+successful"
)
_OTHER_HEAD = (
    r"responsibilities|what\s+you(?:'ll|\s+will)\s+(?:do|work\s+on|be\s+doing)|about\s+the\s+(?:role|"
    r"company|team|job)|the\s+role|role\s+overview|about\s+us|who\s+we\s+are|our\s+team|the\s+team|"
    r"benefits|perks|what\s+we\s+offer|why\s+join|compensation|pay\s+(?:range|transparency)|salary|"
    r"equal\s+(?:opportunity|employment)|how\s+to\s+apply|interview\s+process|our\s+(?:mission|values)|"
    r"day[- ]to[- ]day|in\s+this\s+role|your\s+(?:impact|mission|role)|job\s+description|overview"
)
_PREF_HEAD_RE = re.compile(r"\b(?:" + _PREF_HEAD + r")\b", re.I)
_REQ_HEAD_RE = re.compile(r"\b(?:" + _REQ_HEAD + r")\b", re.I)
_OTHER_HEAD_RE = re.compile(r"\b(?:" + _OTHER_HEAD + r")\b", re.I)
_REQ_START_RE = re.compile(r"^(?:" + _REQ_HEAD + r")\b", re.I)
_PREF_START_RE = re.compile(r"^(?:" + _PREF_HEAD + r")\b", re.I)
_OTHER_START_RE = re.compile(r"^(?:" + _OTHER_HEAD + r")\b", re.I)
_BULLET_RE = re.compile(r"^\s*(?:[-*•·▪●◦‣]|\d{1,2}[.)])\s+")
# Inline headings in one-line text: "Requirements: ... Nice to have: ..."
_INLINE_HEAD_RE = re.compile(
    r"(?:^|(?<=[.!?|])\s+|\s{2,})(?P<h>(?:minimum\s+|basic\s+|preferred\s+)?(?:requirements|qualifications|"
    r"preferred|nice[- ]to[- ]have|bonus(?:\s+points)?|good[- ]to[- ]have|must[- ]haves?|"
    r"responsibilities|about\s+us|benefits|what\s+you(?:'ll|\s+will)\s+do|what\s+you(?:'ll|\s+will)?\s+need))"
    r"\s*:",
    re.I)


def _heading_kind(line: str) -> Optional[str]:
    """Kind of a heading line, or None when the line is content."""
    raw = line.strip()
    if not raw or len(raw) > 70 or _BULLET_RE.match(raw):
        return None
    s = raw.strip("#*_ \t").rstrip(":").strip()
    if not s or s.endswith(".") or re.search(r"\d", s):
        return None
    colon = raw.rstrip().endswith(":")
    words = len(s.split())
    if not colon and words > 8:
        return None

    def has(full: re.Pattern, start: re.Pattern) -> bool:
        return bool(full.search(s)) if colon else bool(start.match(s))

    if has(_PREF_HEAD_RE, _PREF_START_RE) or (not colon and words <= 5 and _PREF_HEAD_RE.search(s)
                                               and _REQ_HEAD_RE.search(s)):
        return PREFERRED
    if has(_REQ_HEAD_RE, _REQ_START_RE):
        return REQUIRED
    if has(_OTHER_HEAD_RE, _OTHER_START_RE):
        return OTHER
    return None


def _inline_kind(h: str) -> str:
    if _PREF_HEAD_RE.search(h):
        return PREFERRED
    if _REQ_HEAD_RE.search(h):
        return REQUIRED
    return OTHER


def _section_events(text: str) -> list[tuple[int, str]]:
    events: list[tuple[int, str]] = []
    pos = 0
    for line in text.split("\n"):
        kind = _heading_kind(line)
        if kind:
            events.append((pos, kind))
        else:
            for m in _INLINE_HEAD_RE.finditer(line):
                events.append((pos + m.start("h"), _inline_kind(m.group("h"))))
        pos += len(line) + 1
    events.sort()
    return events


def _section_at(events: list[tuple[int, str]], pos: int) -> str:
    kind = NONE
    for p, k in events:
        if p > pos:
            break
        kind = k
    return kind


# --------------------------------------------------------------------------
# Clauses
# --------------------------------------------------------------------------
_CLAUSE_BREAK = re.compile(r"\n|[•·▪●|;]|[.!?](?=\s)")
_SUBCLAUSE_BREAK = re.compile(r"[,()\[\]]")


def _clause(text: str, start: int, end: int) -> tuple[int, int]:
    lo = 0
    for m in _CLAUSE_BREAK.finditer(text, 0, start):
        lo = m.end()
    m = _CLAUSE_BREAK.search(text, end)
    hi = m.start() if m else len(text)
    return lo, hi


def _subclause(text: str, lo: int, hi: int, start: int, end: int) -> str:
    a = lo
    for m in _SUBCLAUSE_BREAK.finditer(text, lo, start):
        a = m.end()
    m = _SUBCLAUSE_BREAK.search(text, end, hi)
    b = m.start() if m else hi
    return text[a:b]


def _evidence(text: str, lo: int, hi: int, start: int, end: int) -> str:
    s = text[lo:hi]
    rel_s, rel_e = start - lo, end - lo
    if len(s) > EVIDENCE_MAX:
        mid = (rel_s + rel_e) // 2
        a = max(0, min(mid - EVIDENCE_MAX // 2, len(s) - EVIDENCE_MAX))
        s = s[a:a + EVIDENCE_MAX]
    s = re.sub(r"\s+", " ", _BULLET_RE.sub("", s)).strip(" -*•·")
    return s[:EVIDENCE_MAX]


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
@dataclass
class YoeCandidate:
    min_years: float
    max_years: Optional[float]
    is_preferred: bool
    section: str
    evidence: str
    pos: int
    kind: str = "years"              # years | cap | fresher
    other_level: bool = False


@dataclass
class YoeResult:
    min_years: Optional[float] = None    # exact floor (1.5 for "1.5+")
    max_years: Optional[float] = None
    is_preferred: bool = False
    evidence: Optional[str] = None
    fresher_friendly: bool = False
    section: Optional[str] = None        # required | preferred | other | none
    ambiguous: bool = False              # several different floors survived anchoring
    title_level: str = "neutral"         # junior | senior | neutral
    candidates: list[YoeCandidate] = field(default_factory=list)

    @property
    def yoe_min(self) -> Optional[int]:
        return None if self.min_years is None else int(math.floor(self.min_years + 1e-9))

    @property
    def yoe_max(self) -> Optional[int]:
        return None if self.max_years is None else int(math.ceil(self.max_years - 1e-9))


def title_level(title: Optional[str]) -> str:
    """'senior', 'junior' or 'neutral' from the job title alone."""
    t = re.sub(r"\b(?:19|20)\d{2}\b", "", title or "")
    t = re.sub(r"member\s+of\s+(?:the\s+)?technical\s+staff", "mts", t, flags=re.I)
    if _TITLE_SENIOR_RE.search(t):
        return "senior"
    if _TITLE_FRESHER_RE.search(t) or _TITLE_JUNIOR_RE.search(t):
        return "junior"
    return "neutral"


def title_is_fresher(title: Optional[str]) -> bool:
    t = re.sub(r"\b(?:19|20)\d{2}\b", "", title or "")
    return bool(_TITLE_FRESHER_RE.search(t)) and not _TITLE_SENIOR_RE.search(t)


def _candidates(text: str) -> list[YoeCandidate]:
    events = _section_events(text)
    out: list[YoeCandidate] = []
    for m in _YOE_RE.finditer(text):
        lo_v = float(m.group("lo"))
        hi_v = float(m.group("hi")) if m.group("hi") else None
        months = m.group("unit").lower().startswith("m")
        if months:
            lo_v /= 12.0
            hi_v = hi_v / 12.0 if hi_v is not None else None
        if lo_v > 25 or (hi_v is not None and (hi_v > 40 or hi_v < lo_v)):
            hi_v = None if hi_v is not None and hi_v < lo_v else hi_v
            if lo_v > 25:
                continue
        before = text[max(0, m.start() - 45):m.start()]
        after = text[m.end():m.end() + 45]
        if _GUARD_BEFORE.search(before) or _GUARD_AFTER.match(after):
            continue
        c_lo, c_hi = _clause(text, m.start(), m.end())
        clause = text[c_lo:c_hi]
        # "... and 3 years of\nexperience": a wrapped line still counts.
        if not (_EXPERIENCE_CTX.search(clause) or _EXPERIENCE_CTX.search(re.split(r"[.!?;]", after[:30])[0])):
            continue
        prefix = re.sub(r"\s+", " ", (m.group("prefix") or "").strip().lower()).rstrip(".")
        kind = "years"
        if prefix in _CAP_PREFIX:
            kind, mn, mx = "cap", 0.0, (hi_v if hi_v is not None else lo_v)
        else:
            mn, mx = lo_v, hi_v
        sub = _subclause(text, c_lo, c_hi, m.start(), m.end())
        section = _section_at(events, m.start())
        pref = bool(_SOFT_RE.search(sub)) or section == PREFERRED
        out.append(YoeCandidate(
            min_years=mn, max_years=mx, is_preferred=pref, section=section,
            evidence=_evidence(text, c_lo, c_hi, m.start(), m.end()), pos=m.start(), kind=kind,
            other_level=bool(_OTHER_LEVEL_RE.search(sub))))
    for m in _FRESHER_RE.finditer(text):
        c_lo, c_hi = _clause(text, m.start(), m.end())
        if _FRESHER_NEG_RE.search(text[c_lo:c_hi]):
            continue
        section = _section_at(events, m.start())
        out.append(YoeCandidate(
            min_years=0.0, max_years=None, is_preferred=False, section=section,
            evidence=_evidence(text, c_lo, c_hi, m.start(), m.end()), pos=m.start(), kind="fresher"))
    out.sort(key=lambda c: c.pos)
    return out


def extract_yoe(description_text: Optional[str], title: Optional[str] = None) -> YoeResult:
    """Full YOE reading of a JD: floor, cap, preferred?, evidence, fresher-friendly."""
    level = title_level(title)
    t_fresh = title_is_fresher(title)
    if not description_text:
        return YoeResult(min_years=0.0 if t_fresh else None, fresher_friendly=t_fresh,
                         evidence=(f"title: {title}"[:EVIDENCE_MAX] if t_fresh else None),
                         title_level=level)
    text = _normalise(description_text)
    cands = _candidates(text)
    pool = list(cands)
    if level != "senior":
        same_level = [c for c in pool if not c.other_level]
        pool = same_level or pool
    hard = [c for c in pool if not c.is_preferred]
    use = hard or pool
    req = [c for c in use if c.section == REQUIRED]
    if req:
        use = req
    else:
        not_other = [c for c in use if c.section != OTHER]
        use = not_other or use
    fresher_text = any(c.kind == "fresher" for c in cands)
    if not use:
        return YoeResult(min_years=0.0 if t_fresh else None, fresher_friendly=t_fresh,
                         evidence=(f"title: {title}"[:EVIDENCE_MAX] if t_fresh else None),
                         title_level=level, candidates=cands)
    numeric = [c for c in use if c.kind != "fresher"]
    if level == "junior":
        chosen = min(use, key=lambda c: (c.min_years, c.kind == "fresher", c.pos))
    else:
        # Strictest bar; a fresher phrase only decides when no number does.
        chosen = max(numeric or use, key=lambda c: (c.min_years, -c.pos))
    floors = {round(c.min_years, 2) for c in use}
    fresher = (level != "senior") and (
        t_fresh or fresher_text or (not chosen.is_preferred and chosen.min_years < 1))
    return YoeResult(
        min_years=chosen.min_years, max_years=chosen.max_years, is_preferred=chosen.is_preferred,
        evidence=chosen.evidence, fresher_friendly=fresher, section=chosen.section,
        ambiguous=len(floors) > 1, title_level=level, candidates=cands)


def extract_min_yoe(description_text: Optional[str],
                    title: Optional[str] = None) -> tuple[Optional[int], bool]:
    """v1-compatible: (min_yoe as whole years, is_preferred); (None, False) when absent."""
    if not description_text:
        return None, False
    r = extract_yoe(description_text, title)
    if r.min_years is None:
        return None, False
    return r.yoe_min, r.is_preferred      # a fresher phrase alone reads as 0
