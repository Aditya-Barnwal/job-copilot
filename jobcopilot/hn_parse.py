"""Deterministic parser for Hacker News "Who is hiring?" posts. No LLM.

Top-level comments follow a loose convention: the first line is a header of
pipe-separated fields in any order,

    Company | Role(s) | Location | REMOTE/ONSITE/HYBRID | Salary | URL

and the rest is free text. `parse_post` classifies each header field by what
it looks like (URL, salary, workplace, employment type, time zone, role,
place) instead of by position, because posters reorder them freely.

Privacy: email addresses (plain or "[at]"-obfuscated) and mailto: links are
removed from everything this module returns. Only http(s) links are kept.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Optional

from .models import Compensation, HYBRID, ONSITE, REMOTE, UNKNOWN
from .regions import is_timezone_text, parse_utc_offsets

MAX_ROLES = 5
EMAIL_PLACEHOLDER = "[email removed]"

# ------------------------------------------------------------------ text

_A_RE = re.compile(r"<a\s[^>]*?href=\"([^\"]*)\"[^>]*>(.*?)</a>", re.I | re.S)
_P_RE = re.compile(r"<p\s*/?>", re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_AT = "[@＠﹫]"                       # incl. fullwidth / small commercial at
_LOCAL = r"[\w.%+\-]+"               # \w is Unicode-aware: josé, müller, 田中
_DOMAIN = r"[\w\-]+(?:\.[\w\-]+)+"   # incl. IDN domains (münchen.de)
# "jane@example.com" and the spaced "jane @ example.com" (blanks only, no newlines).
_EMAIL_RE = re.compile(rf"(?:mailto:)?(?<![\w.%+\-]){_LOCAL}[ \t]*{_AT}[ \t]*{_DOMAIN}", re.I)
_OBF_EMAIL_RE = re.compile(
    rf"(?<![\w.%+\-]){_LOCAL}\s*[\[({{<]\s*at\s*[\])}}>]\s*[\w\-]+"
    r"(?:\s*(?:[\[({<]\s*dot\s*[\])}>]|\.)\s*[\w\-]+)+", re.I)
# "email jane at example dot com": only after an email / contact / mail word, so
# ordinary "looking at foo dot" prose is left alone.
_WORD_EMAIL_RE = re.compile(
    r"(\b(?:e-?mails?|contact|mail)\b[^\n.]{0,40}?)"
    r"(?<![\w.%+\-])" + _LOCAL + r"\s+at\s+[\w\-]+(?:\s+dot\s+[\w\-]+)+", re.I)
_URL_BODY = rf"(?:[^\s<>\"'|()\[\]]|{re.escape(EMAIL_PLACEHOLDER)})+"
_URL_RE = re.compile(rf"https?://{_URL_BODY}", re.I)


def scrub_emails(text: str) -> str:
    """Replace every email address (incl. "name [at] co [dot] com", spaced
    "name @ co.com", Unicode / IDN forms, and "email: name at co dot com")."""
    text = _OBF_EMAIL_RE.sub(EMAIL_PLACEHOLDER, text or "")
    text = _WORD_EMAIL_RE.sub(lambda m: m.group(1) + EMAIL_PLACEHOLDER, text)
    return _EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)


def comment_to_text(raw_html: Optional[str]) -> str:
    """HN comment HTML -> plain text. Links become their full href (HN truncates
    the visible text with "..."); mailto: links and emails are removed."""
    def link(m: re.Match) -> str:
        href = html.unescape(m.group(1)).strip()
        shown = html.unescape(_TAG_RE.sub("", m.group(2))).strip()
        if scrub_emails(href) != href:
            # a link carrying an email is dropped, not rewritten; its visible text
            # survives only when it is plain words (HN truncates URLs: "jane@ac...")
            return "" if (not shown or "@" in shown or shown.lower().startswith("http")) else shown
        if not href.lower().startswith(("http://", "https://")):
            return "" if href.lower().startswith("mailto:") else shown
        if not shown or shown == href or shown.endswith("...") or href.startswith(shown.rstrip(".")):
            return href
        return f"{shown} ({href})"

    s = _A_RE.sub(lambda m: link(m).replace("<", "&lt;"), raw_html or "")
    s = _P_RE.sub("\n\n", s)
    s = _BR_RE.sub("\n", s)
    s = html.unescape(_TAG_RE.sub("", s))
    s = scrub_emails(s)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _urls(text: str) -> list[str]:
    """http(s) links in `text`. A link that contained an email (now a placeholder
    in scrubbed text) is dropped whole: never stored with a marker spliced in."""
    out = []
    for u in _URL_RE.findall(text or ""):
        if EMAIL_PLACEHOLDER not in u and scrub_emails(u) == u:
            out.append(u.rstrip(".,;:)]"))
    return out


# ------------------------------------------------------------------ salary

_CODES = "USD|EUR|GBP|INR|CAD|AUD|NZD|SGD|HKD|CHF|SEK|NOK|DKK|PLN|JPY"
_CUR_SYMBOLS = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "₹": "INR",
                "C$": "CAD", "CA$": "CAD", "A$": "AUD", "AU$": "AUD", "NZ$": "NZD",
                "S$": "SGD", "SG$": "SGD", "HK$": "HKD"}
# "$" prefixed by a country letter pair is NOT US dollars ("CA$100k" is CAD).
_SYM_PAT = r"(?<![A-Za-z])(?:US|CA|AU|NZ|SG|HK|C|A|S)?\$|[€£₹]"
_ANY_SYM_RE = re.compile(_SYM_PAT)
_CUR_CODE_RE = re.compile(rf"\b({_CODES})\b", re.I)
_INR_WORD_RE = re.compile(r"\b(?:LPA|lakhs?|lacs?|crores?)\b|₹", re.I)
_NUM = (r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"            # 120,000 / 1,200.50
        r"|\d{1,3}(?:[   ]\d{3})+(?!\d)"   # 50 000
        r"|\d{1,3}(?:\.\d{3})+(?![\d.])"             # 70.000
        r"|\d+(?:\.\d+)?")
# The multiplier is part of the amount; the lookahead only rejects a number that
# continues ("1,5") or is a percentage, so "120k, equity" keeps its "k".
_AMOUNT_RE = re.compile(
    rf"(?<![\d.,])(?P<sym>{_SYM_PAT})?\s?(?P<num>{_NUM})\s?"
    r"(?P<mult>(?:[kKmMbB]|LPA|lpa|lakhs?|lacs?|crores?|cr|L)(?![a-zA-Z]))?(?!\d|[.,]\d|\s*%)")
_SALARY_CUE_RE = re.compile(
    rf"[$€£₹]\s?\d|\d\s?[kK]\b.*?(?:\b(?:{_CODES})\b|[€£₹])|"
    rf"\b(?:{_CODES})\s?\d|\d\s?(?:{_CODES})\b|\d\s?(?:LPA|lakhs?|lacs?)\b|"
    r"\b(?:salary|comp|compensation|base|OTE|stipend)\b.*\d", re.I)
_INTERVALS = (
    (re.compile(r"/\s*(?:mo|month)\b|per month|monthly|\bpm\b", re.I), "1 MONTH"),
    (re.compile(r"/\s*(?:h|hr|hour)\b|per hour|hourly", re.I), "1 HOUR"),
    (re.compile(r"/\s*day\b|per day|daily rate", re.I), "1 DAY"),
    (re.compile(r"/\s*(?:wk|week)\b|per week|weekly", re.I), "1 WEEK"),
)
_YEAR_CUE_RE = re.compile(r"/\s*(?:yr|year|annum)\b|per (?:year|annum)\b|\bannual(?:ly)?\b|\bp\.?a\.?\b", re.I)
_MULT = {"k": 1e3, "m": 1e6, "b": 1e9, "lpa": 1e5, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5,
         "lacs": 1e5, "l": 1e5, "cr": 1e7, "crore": 1e7, "crores": 1e7}
_PER_YEAR = {"1 HOUR": 2080, "1 DAY": 260, "1 WEEK": 52, "1 MONTH": 12}
# Plausibility cap on annual pay in the stated currency. Currencies with small
# units (INR, JPY) get a higher one; anything above is a misread, not a salary.
MAX_ANNUAL_PAY = 5_000_000
_BIG_UNIT_CURRENCIES = {"INR": 200_000_000, "JPY": 200_000_000}

# Funding / revenue / valuation talk is never pay ("$40M Series B", "Raised $12M").
_FUNDING_WORD = (r"raised|raising|raise|funding|funded|series\s+[a-f]\b|seed|round|valuation|"
                 r"valued|ARR|MRR|revenue|revenues|backed|unicorn|IPO")
_FUNDING_RE = re.compile(rf"\b(?:{_FUNDING_WORD})\b", re.I)
_PAY_WORD_RE = re.compile(
    r"\b(?:salary|comp|compensation|base|OTE|pay|stipend|wage|wages)\b|"
    r"/\s*(?:yr|year|annum|mo|month|hr|hour)\b|\bper (?:year|annum|month|hour)\b|\bannual(?:ly)?\b", re.I)
_K_AMOUNT_RE = re.compile(r"\d\s?[kK]\b")
_BIG_MULT_RE = re.compile(r"\d\s?[mMbB](?![a-zA-Z])")
_FUNDING_PAREN_RE = re.compile(rf"\([^()]*\b(?:{_FUNDING_WORD})\b[^()]*\)", re.I)
_FUNDING_BEFORE_RE = re.compile(rf"\b(?:{_FUNDING_WORD}|valued at|worth)\b[\s:@~]*(?:of|at|over|about|\W)*$", re.I)
_FUNDING_AFTER_RE = re.compile(rf"^\s*(?:[A-Za-z]+\s+)?(?:{_FUNDING_WORD})\b", re.I)
_RANGE_GAP_RE = re.compile(
    rf"^\s*(?:/\s*[A-Za-z]+)?\s*(?:{_CODES})?\s*(?:-|–|—|to|~|\.\.)\s*(?:{_CODES}\s*)?$", re.I)


def is_salary_text(text: str) -> bool:
    return bool(_SALARY_CUE_RE.search(text or ""))


def is_funding_text(text: str) -> bool:
    """A field about money raised / revenue / valuation rather than pay."""
    t = text or ""
    if _PAY_WORD_RE.search(t) or _K_AMOUNT_RE.search(t):
        return False
    return bool(_FUNDING_RE.search(t) or _BIG_MULT_RE.search(t))


def _to_float(num: str, has_mult: bool) -> float:
    n = num.replace(" ", " ").replace(" ", " ")
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", n):
        return float(n.replace(",", ""))
    if re.fullmatch(r"\d{1,3}(?: \d{3})+", n):
        return float(n.replace(" ", ""))
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", n) and not has_mult:
        return float(n.replace(".", ""))
    return float(n)


def _is_funding_amount(text: str, m: re.Match) -> bool:
    return bool(_FUNDING_BEFORE_RE.search(text[max(0, m.start() - 24):m.start()]) or
                _FUNDING_AFTER_RE.match(text[m.end():m.end() + 24]))


def parse_salary(text: str) -> Optional[Compensation]:
    """"$140K–$160K + equity", "70k - 100k EUR", "€5,000/month", "₹25-40 LPA",
    "€50 000 - €70 000", "70.000 - 90.000 EUR", "50k–70k €", "CA$100k".

    Numbers are only kept when the currency is stated: a bare number would be
    read as INR downstream (models.Compensation.annual_inr), i.e. fabricated.
    Funding / revenue figures ("$40M Series B") are not pay and yield None; M/B
    multipliers need salary words; annual pay above MAX_ANNUAL_PAY is rejected.
    """
    if not text or not is_salary_text(text):
        return None
    clean = _FUNDING_PAREN_RE.sub(" ", text)
    head = re.split(r"\s\+|\+\s|\bequity\b|\bplus\b|\bbonus\b", clean, maxsplit=1, flags=re.I)[0]
    pay_words = bool(_PAY_WORD_RE.search(text))
    amounts: list[tuple[re.Match, float, Optional[str]]] = []
    for m in _AMOUNT_RE.finditer(head):
        mult = (m.group("mult") or "").lower() or None
        if mult in ("m", "b") and not pay_words:
            continue                     # "$40M": funding or revenue, not pay
        if _is_funding_amount(head, m):
            continue
        amounts.append((m, _to_float(m.group("num"), bool(mult)), mult))
    if not amounts and (_FUNDING_RE.search(text) or _BIG_MULT_RE.search(text)):
        return None
    # First usable amount, then its range partner only if a range separator sits between.
    first = next(((m, v, mu) for m, v, mu in amounts if v * _MULT.get(mu or "", 1.0) >= 10), None)
    picked: list[tuple[re.Match, float, Optional[str]]] = [first] if first else []
    if first:
        nxt = next(((m, v, mu) for m, v, mu in amounts if m.start() >= first[0].end()), None)
        if nxt and _RANGE_GAP_RE.match(head[first[0].end():nxt[0].start()]):
            picked.append(nxt)
    # "€100–140k": the multiplier on the upper bound applies to the lower one too.
    if len(picked) == 2 and picked[0][2] is None and picked[1][2] and picked[0][1] < 1000:
        picked[0] = (picked[0][0], picked[0][1], picked[1][2])
    values = [v * _MULT.get(mu or "", 1.0) for _, v, mu in picked]

    code = _CUR_CODE_RE.search(text)
    sym = _ANY_SYM_RE.search(text)
    if code:
        cur: Optional[str] = code.group(1).upper()
    elif sym:
        cur = _CUR_SYMBOLS.get(sym.group(0).upper().replace("€", "€"))
    elif _INR_WORD_RE.search(text):
        cur = "INR"
    else:
        cur = None
    if cur is None and _INR_WORD_RE.search(text):
        cur = "INR"
    interval = next((iv for rx, iv in _INTERVALS if rx.search(text)), None)
    summary = text.strip()
    if cur is None or not values:
        return Compensation(summary=summary, source="hn", is_estimate=False)
    lo, hi = (min(values), max(values)) if len(values) == 2 else (values[0], values[0])
    if interval is None and hi >= 1000:
        interval = "1 YEAR"
    per_year = _PER_YEAR.get(interval or "", 1)
    if hi * per_year > _BIG_UNIT_CURRENCIES.get(cur, MAX_ANNUAL_PAY):
        return None                      # implausible pay: a misread, not a salary
    if re.search(r"\bup to\b", text, re.I) and len(values) == 1:
        lo = None  # type: ignore[assignment]
    return Compensation(currency=cur, min_value=lo, max_value=hi, interval=interval,
                        summary=summary, source="hn", is_estimate=False)


# ------------------------------------------------------------------ fields

_REMOTE_RE = re.compile(r"\bremote(?:ly)?\b|\bwfh\b|work from home", re.I)
_NOT_REMOTE_RE = re.compile(r"\b(?:no|not|non)[- ]remote\b", re.I)
_PARTIAL_REMOTE_RE = re.compile(
    r"part(?:ly|ial(?:ly)?)?[- ]remote|remote (?:days?|fridays?|option|ok)\b|"
    r"occasional(?:ly)? remote|some remote|for exceptional", re.I)
_ONSITE_RE = re.compile(r"\bon[- ]?site\b|\bin[- ]?office\b|\bin[- ]person\b|work[- ]from[- ]office", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_EMPLOYMENT_RE = re.compile(
    r"^\s*(?:full[- ]?time|part[- ]?time|contract(?:or|s)?|freelance|(?:summer )?internships?|"
    r"FT|PT|permanent|temporary|temp)\b", re.I)
_MISC_RE = re.compile(
    r"\b(?:visa|sponsorship|citizenship|citizens?|work authori[sz]ation|clearance|relocation)\b|"
    r"^\s*(?:seed|pre-seed|series [a-f]|yc [a-z]\d+|bootstrapped|profitable|stealth)\b", re.I)
_ROLE_RE = re.compile(
    r"engineer|developer|\bdevs?\b|scientist|designer|manager|\bPMs?\b|\bleads?\b|architect|"
    r"\bintern\b|researcher|\bresearch\b|analyst|\bSREs?\b|devops|founding|\bstaff\b|consultant|"
    r"admin|specialist|recruiter|account exec|\bAEs?\b|executive|director|\bhead of\b|\bVP\b|\bCTO\b|"
    r"programmer|technician|writer|counsel|\bMTS\b|member of technical|\bphd\b|postdoc|"
    r"positions|\broles\b|openings|engineering|\bCSMs?\b|marketer|marketing|\bsales\b|"
    r"\bops\b|operations|support|hacker|\bQA\b|tester", re.I)
_US_STATE_RE = re.compile(r",\s*(?:A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|"
                          r"N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b")
_PLACES = (
    "san francisco", "sf", "bay area", "new york", "nyc", "ny", "seattle", "boston", "austin",
    "los angeles", "la", "chicago", "denver", "miami", "atlanta", "dc", "washington",
    "palo alto", "mountain view", "menlo park", "sunnyvale", "san jose", "oakland", "berkeley",
    "london", "berlin", "munich", "paris", "amsterdam", "dublin", "zurich", "zürich", "stockholm",
    "copenhagen", "oslo", "helsinki", "lisbon", "madrid", "barcelona", "warsaw", "prague",
    "vienna", "toronto", "vancouver", "montreal", "sydney", "melbourne", "singapore",
    "tokyo", "tel aviv", "dubai", "india", "bengaluru", "bangalore", "hyderabad", "pune",
    "chennai", "mumbai", "gurgaon", "gurugram", "noida", "delhi", "kolkata", "usa", "us",
    "uk", "united states", "united kingdom", "canada", "germany", "france", "netherlands",
    "switzerland", "sweden", "spain", "portugal", "poland", "ireland", "israel", "japan",
    "australia", "brazil", "mexico", "europe", "eu", "emea", "apac", "latam",
)
_PLACE_RE = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(p) for p in _PLACES) + r")(?![\w-])", re.I)

# (label, pattern, flags) for who may apply to a remote role. Order = output order.
# US / UK / EU are matched case-sensitively so "join us" is not a region.
_REGION_PATTERNS = (
    ("Worldwide", r"\bworld\s*-?wide\b|\bglobal(?:ly)?\b|\banywhere\b|\bany time\s*zone", re.I),
    ("India", r"\bindia\b|\bbengaluru\b|\bbangalore\b", re.I),
    ("US", r"\bU\.?S\.?A?\.?(?![\w.])|\b[Uu]nited [Ss]tates\b|\bUNITED STATES\b", 0),
    ("Canada", r"\bcanada\b", re.I),
    ("North America", r"\bnorth america\b", re.I),
    ("Americas", r"\bamericas\b", re.I),
    ("LATAM", r"\blatam\b|\blatin america\b|\bsouth america\b", re.I),
    ("Europe", r"\b[Ee]urope\b|\bEUROPE\b|\bEU\b|\bEEA\b", 0),
    ("UK", r"\bUK\b|\b[Uu]nited [Kk]ingdom\b|\bUNITED KINGDOM\b", 0),
    ("EMEA", r"\bemea\b", re.I),
    ("APAC", r"\bapac\b|\basia\b", re.I),
)
_US_ONLY_RE = re.compile(r"\b(?:U\.?S\.?A?\.?|united states)\s*(?:only|citizen|citizenship|persons?)\b|"
                         r"\bonly\s+(?:in\s+)?(?:the\s+)?U\.?S\.?A?\b|work authori[sz]ation", re.I)


def regions_of(text: str) -> list[str]:
    """Canonical region labels named in `text` ("REMOTE (US only)" -> ["US Only"])."""
    out: list[str] = []
    for label, pat, flags in _REGION_PATTERNS:
        if re.search(pat, text or "", flags):
            if label == "US" and _US_ONLY_RE.search(text):
                label = "US Only"
            out.append(label)
    return out


def workplace_of(text: str) -> str:
    """REMOTE / HYBRID / ONSITE from a post's workplace fields. A remote
    alternative ("ONSITE or REMOTE (US)") wins; "part remote" does not."""
    remote = bool(_REMOTE_RE.search(text)) and not _NOT_REMOTE_RE.search(text)
    hybrid, onsite = bool(_HYBRID_RE.search(text)), bool(_ONSITE_RE.search(text))
    if remote and (hybrid or onsite) and _PARTIAL_REMOTE_RE.search(text):
        remote = False
    if remote:
        return REMOTE
    if hybrid:
        return HYBRID
    if onsite:
        return ONSITE
    return UNKNOWN


def _split_outside_parens(text: str, seps: str) -> list[str]:
    parts, depth, cur = [], 0, []
    i = 0
    pattern = re.compile(seps)
    while i < len(text):
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if depth == 0:
            m = pattern.match(text, i)
            if m and m.end() > i:
                parts.append("".join(cur))
                cur, i = [], m.end()
                continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def split_roles(text: str) -> list[str]:
    """"Engineers, Designers, PMs" -> 3 roles; "Backend Engineer, Foundations"
    stays one (a piece without a role word is a qualifier, not a role)."""
    text = re.sub(r"\s+", " ", text or "").strip(" .;,")
    if not text:
        return []
    parts = _split_outside_parens(text, r"\s*[,;]\s*|\s+\+\s+|\s+/\s+|\s+&\s+|\s+and\s+")
    if len(parts) > 1 and all(_ROLE_RE.search(p) for p in parts):
        return parts
    return [text]


def _classify(text: str) -> str:
    if is_funding_text(text):
        return "misc"   # "$40M Series B", "Raised $12M": money raised, not pay
    if is_salary_text(text):
        return "salary"
    if len(text.split()) > 12:   # header ran into the prose ("Full-Time MixRank processes ...")
        return "employment" if _EMPLOYMENT_RE.search(text) else "unknown"
    if _REMOTE_RE.search(text) or _HYBRID_RE.search(text) or _ONSITE_RE.search(text):
        return "workplace"
    if _EMPLOYMENT_RE.search(text):
        return "employment"
    if _MISC_RE.search(text):
        return "misc"
    if is_timezone_text(text):
        return "tz"
    if _ROLE_RE.search(text) and len(text.split()) <= 14:
        return "role"
    if _PLACE_RE.search(text) or _US_STATE_RE.search(text):
        return "location"
    return "unknown"


@dataclass
class ParsedPost:
    """What a header line says, field by field. `ok` is False for prose posts
    with no pipe-separated header: those are counted, not guessed at."""
    company: str = ""
    roles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    workplace_text: list[str] = field(default_factory=list)
    workplace_type: str = UNKNOWN
    regions: list[str] = field(default_factory=list)
    tz_text: Optional[str] = None
    tz_offsets: list[float] = field(default_factory=list)
    salary: Optional[Compensation] = None
    apply_url: Optional[str] = None
    header: str = ""
    ok: bool = False

    @property
    def location_text(self) -> str:
        return "; ".join(self.locations + self.workplace_text)


_HEADER_SEP_RE = r"\s*[|•]\s*"
# "REMOTE (US/UK/India) Full-time AI Risk De..." : keep the workplace lead, drop the prose.
_LEAD_WORKPLACE_RE = re.compile(
    r"^\s*((?:\d+%\s*)?(?:remote|on[- ]?site|hybrid)\b(?:\s*\([^)]*\))?)", re.I)
# Job seekers who post in the hiring thread by mistake ("Willing to relocate: Yes").
_SEEKER_RE = re.compile(
    r"\bwilling to relocate\b|\btechnical skills\s*:|\br[ée]sum[ée]\s*:|"
    r"\bI'?m an? (?:software|backend|frontend|full[- ]?stack|data|ml|senior|junior) [\w ]{0,20}"
    r"(?:engineer|developer)\b|\blooking for (?:a |my )?(?:new |next )?(?:role|job|position)", re.I)
_JOBBY_URL_RE = re.compile(r"job|career|apply|hiring|greenhouse|lever\.co|ashbyhq|workable|"
                           r"recruitee|rippling|bamboohr|breezy|teamtailor|work-at|/join", re.I)
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _salary_score(c: Compensation) -> int:
    """Prefer a field with real numbers, a range, and a yearly/interval cue."""
    has = c.min_value is not None or c.max_value is not None
    ranged = has and c.min_value is not None and c.min_value != c.max_value
    return 4 * has + 2 * ranged + (c.interval is not None)


def _header_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    if not lines:
        return ""
    head = lines[0]
    # "Acme | Engineer | NYC |\nhttps://acme.com Acme builds ..." : URL wrapped onto line 2.
    if head.endswith("|") and len(lines) > 1 and lines[1].lower().startswith("http"):
        head += " " + lines[1].split()[0]
    return head


def parse_header(header: str, body: str = "", *, max_roles: int = MAX_ROLES) -> ParsedPost:
    """Classify the fields of one header line. `body` (the rest of the post,
    plain text) is only used to find an apply link when the header has none."""
    post = ParsedPost(header=scrub_emails(header))
    fields = _split_outside_parens(post.header, _HEADER_SEP_RE)
    if len(fields) < 2:
        return post

    urls: list[str] = []
    classified: list[tuple[str, str]] = []
    for i, f in enumerate(fields):
        urls.extend(_urls(f))
        rest = _URL_RE.sub("", f).replace(EMAIL_PLACEHOLDER, "")
        rest = re.sub(r"\(\s*\)", "", rest).strip(" -–—:,.;")
        if len(rest.split()) > 12 and (lead := _LEAD_WORKPLACE_RE.match(rest)):
            rest = lead.group(1)
        if not rest:
            continue
        kind = _classify(rest)
        # Field 0 is the company unless it plainly is something else
        # ("Cologne, Germany | UMH | ..."); "Pond AI" has a role word but is a name.
        if i == 0 and (kind in ("role", "unknown", "misc", "tz") or
                       (kind == "location" and "," not in rest)):
            kind = "company"
        classified.append((kind, rest))

    company = next((t for k, t in classified if k == "company"), None)
    if company is None:
        company = next((t for k, t in classified if k == "unknown"), None)
        classified = [(k if not (k == "unknown" and t == company) else "company", t) for k, t in classified]
    company = _TRAILING_PAREN_RE.sub("", company or "").strip()
    if not company or len(company) > 80:
        return post   # prose, not a header: don't guess a company name
    post.company = company

    roles: list[str] = []
    salaries: list[Compensation] = []
    for kind, text in classified:
        if kind == "role":
            roles.extend(split_roles(text))
        elif kind == "location":
            post.locations.append(text)
        elif kind == "workplace":
            post.workplace_text.append(text)
        elif kind == "salary":
            comp = parse_salary(text)
            if comp is not None:
                salaries.append(comp)
        elif kind == "tz" and post.tz_text is None:
            post.tz_text = text
    post.salary = max(salaries, key=_salary_score, default=None)   # ties: first field wins
    if not roles:
        short = [t for k, t in classified if k == "unknown" and len(t.split()) <= 8]
        roles = split_roles(short[0]) if short else []
    post.roles = [r[:200] for r in roles[:max_roles]]

    wp = " | ".join(post.workplace_text)
    post.workplace_type = workplace_of(wp) if wp else UNKNOWN
    if post.workplace_type == REMOTE:
        remote_bits = [t for t in post.workplace_text if _REMOTE_RE.search(t)]
        post.regions = regions_of(" | ".join(remote_bits))
        if post.tz_text is None:
            post.tz_text = next((t for t in remote_bits if is_timezone_text(t)
                                 or re.search(r"time\s*zones?|timezones?", t, re.I)), None)
    post.tz_offsets = parse_utc_offsets(post.tz_text) if post.tz_text else []

    if _SEEKER_RE.search(body):
        return ParsedPost(header=post.header, company=post.company)   # a seeker, not a job
    body_urls = _urls(body)
    post.apply_url = (next((u for u in urls if _JOBBY_URL_RE.search(u)), None) or
                      next((u for u in body_urls if _JOBBY_URL_RE.search(u)), None) or
                      (urls[0] if urls else None))
    post.ok = True
    return post


def parse_post(raw_html: Optional[str], *, max_roles: int = MAX_ROLES) -> tuple[ParsedPost, str]:
    """One comment's HTML -> (ParsedPost, plain text with emails removed)."""
    text = comment_to_text(raw_html)
    header = _header_line(text)
    body = text[len(text.split("\n", 1)[0]):] if text else ""
    return parse_header(header, body, max_roles=max_roles), text
