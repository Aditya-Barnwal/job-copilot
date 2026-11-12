"""Small, dependency-light normalization helpers.

These turn the messy, source-specific strings every ATS hands back into the
controlled vocabularies the rest of the pipeline relies on. They are
deliberately heuristic — they will occasionally be wrong on an ambiguous
posting, which is exactly why a human confirm step exists downstream.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Optional

from .models import REMOTE, ONSITE, HYBRID, UNKNOWN, IN, NON_IN, COUNTRY_UNKNOWN


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTINL_RE = re.compile(r"\n{3,}")


def strip_html(value: Optional[str]) -> str:
    """Convert an HTML (or HTML-entity-encoded) blob to readable plain text."""
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.I)
    text = re.sub(r"</\s*(p|div|li|h[1-6])\s*>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)  # second pass for entities revealed after tag removal
    text = _WS_RE.sub(" ", text)
    text = _MULTINL_RE.sub("\n\n", text)
    return text.strip()


def normalize_workplace(value: Optional[str]) -> Optional[str]:
    """Map a source's explicit workplace-type string to our vocabulary.

    Returns None when the source gave nothing, so the caller can fall back to
    inferring from the location text.
    """
    if not value:
        return None
    v = value.strip().lower().replace("_", " ").replace("-", " ")
    if "remote" in v:
        return REMOTE
    if "hybrid" in v:
        return HYBRID
    if "on site" in v or "onsite" in v or "in office" in v or "office" in v:
        return ONSITE
    return None


def infer_workplace_type(location_text: Optional[str]) -> str:
    """Guess workplace type from a free-text location when no explicit field exists."""
    inferred = normalize_workplace(location_text)
    return inferred if inferred else UNKNOWN


# Signals for the India / non-India geo bucket. Every signal is matched as a
# WHOLE WORD (see _signal_re): "india" must not fire inside "Indianapolis" /
# "Indiana", nor "uk" inside "Ukraine". Case-sensitive codes ("US", ", CA",
# "MX") are handled separately by _US_RE / _CODE_RE.
_INDIA_COUNTRY = ("india", "bharat")
_INDIA_CITIES = (
    "bengaluru", "bangalore", "hyderabad", "secunderabad", "pune", "chennai", "madras",
    "mumbai", "bombay", "navi mumbai", "thane", "gurgaon", "gurugram", "noida", "greater noida",
    "delhi", "new delhi", "delhi ncr", "ncr", "faridabad", "ghaziabad", "kolkata", "calcutta",
    "ahmedabad", "gandhinagar", "kochi", "cochin", "coimbatore", "thiruvananthapuram", "trivandrum",
    "indore", "jaipur", "chandigarh", "mohali", "panchkula", "mysuru", "mysore", "bhubaneswar",
    "visakhapatnam", "vizag", "vijayawada", "nagpur", "nashik", "vadodara", "surat", "lucknow",
    "kanpur", "bhopal", "madurai", "tiruchirappalli", "trichy", "mangaluru", "mangalore", "hubli",
    "dehradun", "patna", "ranchi", "guwahati", "raipur", "goa", "panaji", "manipal", "vellore",
    "warangal", "amritsar", "ludhiana", "jodhpur", "udaipur", "karnataka", "maharashtra",
    "telangana", "tamil nadu", "kerala", "haryana", "uttar pradesh", "west bengal", "gujarat",
    "andhra pradesh", "rajasthan", "odisha", "punjab",
)
_INDIA_SIGNALS = _INDIA_COUNTRY + _INDIA_CITIES

# Strong non-India signals. Presence of one of these *without* an India signal
# pushes a posting into the non-IN bucket (where only remote roles survive).
_NON_INDIA_COUNTRIES = (
    # North America
    "united states", "united states of america", "usa", "u.s.", "u.s.a.", "america", "canada",
    "mexico", "méxico", "puerto rico",
    # US states (full names; "Indiana" is a state too and is NOT India)
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
    "british columbia", "ontario", "quebec", "québec", "alberta",
    # Latin America
    "brazil", "brasil", "argentina", "colombia", "chile", "peru", "uruguay", "paraguay",
    "ecuador", "bolivia", "venezuela", "costa rica", "panama", "guatemala", "dominican republic",
    "latam", "latin america", "south america", "north america", "americas", "central america",
    # Europe
    "uk", "united kingdom", "england", "scotland", "wales", "great britain", "ireland",
    "germany", "deutschland", "france", "netherlands", "the netherlands", "holland", "belgium",
    "luxembourg", "switzerland", "austria", "italy", "spain", "portugal", "greece", "malta",
    "cyprus", "iceland", "norway", "sweden", "denmark", "finland", "estonia", "latvia",
    "lithuania", "poland", "czech republic", "czechia", "slovakia", "hungary", "romania",
    "bulgaria", "serbia", "croatia", "slovenia", "bosnia", "montenegro", "albania",
    "north macedonia", "moldova", "ukraine", "belarus", "russia", "georgia", "armenia",
    "azerbaijan", "europe", "european union", "emea", "eu", "dach", "nordics", "baltics",
    # Middle East / Africa
    "israel", "turkey", "türkiye", "uae", "united arab emirates", "saudi arabia", "saudi",
    "ksa", "qatar", "kuwait", "bahrain", "oman", "jordan", "lebanon", "egypt", "morocco", "maroc",
    "tunisia", "algeria", "nigeria", "kenya", "ghana", "ethiopia", "rwanda", "uganda",
    "tanzania", "south africa", "botswana", "middle east", "africa", "mena",
    # Asia-Pacific (not India)
    "singapore", "australia", "new zealand", "japan", "china", "taiwan", "hong kong",
    "korea", "south korea", "philippines", "vietnam", "viet nam", "indonesia", "malaysia",
    "thailand", "pakistan", "bangladesh", "sri lanka", "nepal", "cambodia", "myanmar",
    "kazakhstan", "uzbekistan", "mongolia", "macau",
)
_NON_INDIA_CITIES = (
    # US / Canada
    "san francisco", "sf", "bay area", "sf bay area", "silicon valley", "new york", "new york city",
    "nyc", "seattle", "bellevue", "redmond", "kirkland", "austin", "dallas", "houston",
    "boston", "cambridge", "chicago", "los angeles", "santa monica", "san diego", "san jose",
    "san mateo", "palo alto", "menlo park", "mountain view", "sunnyvale", "santa clara",
    "cupertino", "redwood city", "milpitas", "oakland", "berkeley", "denver", "boulder",
    "atlanta", "miami", "orlando", "tampa", "phoenix", "salt lake city", "portland", "pittsburgh",
    "philadelphia", "washington dc", "washington, dc", "d.c.", "arlington", "reston", "bethesda",
    "baltimore", "raleigh", "durham", "charlotte", "nashville", "minneapolis", "detroit",
    "columbus", "indianapolis", "st. louis", "kansas city", "las vegas", "toronto", "vancouver",
    "montreal", "montréal", "ottawa", "waterloo", "kitchener", "calgary", "edmonton",
    # Latin America
    "mexico city", "ciudad de méxico", "guadalajara", "monterrey", "puebla", "são paulo",
    "sao paulo", "rio de janeiro", "belo horizonte", "florianópolis", "buenos aires",
    "bogotá", "bogota", "medellín", "medellin", "santiago", "lima", "montevideo",
    # Europe
    "london", "manchester", "edinburgh", "cambridge uk", "dublin", "berlin", "munich", "münchen",
    "hamburg", "frankfurt", "cologne", "paris", "amsterdam", "rotterdam", "utrecht", "brussels",
    "zurich", "zürich", "geneva", "vienna", "milan", "rome", "turin", "madrid", "barcelona",
    "valencia", "lisbon", "porto", "athens", "stockholm", "oslo", "copenhagen", "aarhus",
    "helsinki", "tallinn", "riga", "vilnius", "warsaw", "krakow", "kraków", "wroclaw", "wrocław",
    "prague", "brno", "bratislava", "budapest", "bucharest", "cluj", "sofia", "belgrade",
    "zagreb", "ljubljana", "kyiv", "kiev", "lviv", "minsk", "moscow", "reykjavík", "reykjavik",
    "canary wharf", "luxembourg city", "trento", "istanbul", "ankara",
    # Middle East / Africa
    "tel aviv", "jerusalem", "haifa", "dubai", "abu dhabi", "riyadh", "jeddah", "doha",
    "kuwait city", "cairo", "casablanca", "lagos", "nairobi", "cape town", "johannesburg",
    "beirut", "amman",
    # Asia-Pacific
    "sydney", "melbourne", "brisbane", "perth", "auckland", "tokyo", "osaka", "beijing",
    "shanghai", "shenzhen", "guangzhou", "hangzhou", "taipei", "seoul", "seongnam", "manila",
    "quezon city", "makati", "cebu", "ho chi minh city", "hanoi", "jakarta", "kuala lumpur",
    "petaling jaya", "bangkok", "karachi", "lahore", "islamabad", "dhaka", "colombo",
    "kathmandu",
)
_NON_INDIA_SIGNALS = _NON_INDIA_COUNTRIES + _NON_INDIA_CITIES

# Case-sensitive codes. "US" / "USA" / "U.S." anywhere ("Remote US", "US-WA-Bellevue",
# "REMOTE (US)"); ISO/US-state codes only after a comma or a "US-" / "XX-" prefix
# ("Denver, CO", "Mexico City, MX", "PL-Warsaw", "CH-Zurich"). Codes that are also
# Indian state codes or ambiguous (IN Indiana/India, TN, GA, OR, MN, HR, AS, MH, TG,
# AP, PY, ML, NL, SK, MZ, AR, BR, CT, UP, WB, KA, KL, UK) are left out here.
_US_RE = re.compile(r"(?<![A-Za-z])(?:US|USA|U\.S\.A?\.?|UNITED STATES)(?![A-Za-z])")
_US_STATE_CODES = (
    "AL AK AZ AR CA CO DE FL HI ID IL IA KS KY LA ME MD MA MI MS MO MT NE NV NH NJ NM NY NC ND "
    "OH OK PA RI SC SD TX UT VT VA WA WV WI WY DC BC ON QC AB"
).split()
_COUNTRY_CODES = (
    "MX CL CO PE UY EC CR GB IE DE FR BE LU CH AT IT ES PT GR IS NO SE DK FI EE LV LT PL CZ "
    "HU RO BG RS HR SI UA BY RU IL TR AE SA QA KW EG MA NG KE ZA SG AU NZ JP CN TW HK KR PH VN "
    "ID MY TH PK BD LK"
).split()
_AMBIGUOUS_CODES = {"IN", "TN", "GA", "OR", "MN", "HR", "AS", "MH", "TG", "AP", "PY", "ML",
                    "NL", "SK", "MZ", "AR", "BR", "CT", "UP", "WB", "KA", "KL"}
_CODES = sorted({c for c in _US_STATE_CODES + _COUNTRY_CODES if c not in _AMBIGUOUS_CODES})
_CODE_RE = re.compile(r"(?:,\s*|^|\(|\b(?:US|[A-Z]{2})-)(?:" + "|".join(_CODES) + r")(?![A-Za-z])"
                      r"|(?<![A-Za-z])(?:" + "|".join(_CODES) + r")-[A-Z][A-Za-z]")
_INDIA_CODE_RE = re.compile(r"(?<![A-Za-z])IND(?![A-Za-z])|(?:,\s*)IN\s*$")


def _signal_re(signals: tuple[str, ...]) -> "re.Pattern[str]":
    alts = sorted(set(signals), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(x) for x in alts) + r")(?!\w)")


_INDIA_COUNTRY_RE = _signal_re(_INDIA_COUNTRY)
_INDIA_CITY_RE = _signal_re(_INDIA_CITIES)
_NON_INDIA_COUNTRY_RE = _signal_re(_NON_INDIA_COUNTRIES)
_NON_INDIA_RE = _signal_re(_NON_INDIA_SIGNALS)


def has_india_signal(text: Optional[str]) -> bool:
    """True when `text` names India or an Indian city / state (whole words)."""
    if not text:
        return False
    t = text.lower()
    return bool(_INDIA_COUNTRY_RE.search(t) or _INDIA_CITY_RE.search(t) or _INDIA_CODE_RE.search(text))


def has_non_india_signal(text: Optional[str]) -> bool:
    """True when `text` names a place outside India (country, city, US state, code)."""
    if not text:
        return False
    return bool(_NON_INDIA_RE.search(text.lower()) or _US_RE.search(text) or _CODE_RE.search(text))


_WORKDAY_REL_RE = re.compile(r"(\d+)\s*\+?\s*day", re.I)


def posted_age_days(posted_at: Optional[str], source: Optional[str] = None) -> Optional[int]:
    """Approximate age in days for a job's posted timestamp.

    Handles ISO-ish strings (greenhouse updated_at / lever createdAt -> ms epoch
    rendered to ISO upstream / ashby publishedAt|updatedAt) and Workday's
    relative strings ('Posted Today', 'Posted 3 Days Ago', 'Posted 30+ Days Ago').
    """
    if not posted_at:
        return None
    s = str(posted_at).strip()

    if source == "workday" or "posted" in s.lower():
        low = s.lower()
        if "today" in low or "just" in low:
            return 0
        if "yesterday" in low:
            return 1
        m = _WORKDAY_REL_RE.search(low)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None

    try:
        clean = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0, int(delta.total_seconds() // 86400))
    except (ValueError, TypeError):
        return None


def freshness_bucket(age_days: Optional[int]) -> str:
    if age_days is None:
        return "unknown"
    if age_days <= 30:
        return "fresh"
    if age_days <= 90:
        return "recent"
    if age_days <= 180:
        return "aging"
    return "stale"


def infer_country_bucket(location_text: Optional[str]) -> str:
    """Bucket a location into IN / non-IN / unknown for the geo filter."""
    if not location_text:
        return COUNTRY_UNKNOWN
    t = location_text.lower()
    non_in_country = bool(_NON_INDIA_COUNTRY_RE.search(t) or _US_RE.search(location_text))
    if _INDIA_COUNTRY_RE.search(t) or _INDIA_CODE_RE.search(location_text):
        return IN
    if _INDIA_CITY_RE.search(t):
        # A bare city name is weaker than a stated country: "Delhi, NY, USA".
        return NON_IN if non_in_country else IN
    if has_non_india_signal(location_text):
        return NON_IN
    return COUNTRY_UNKNOWN


# ---------------------------------------------------------------------------
# Company identity (Phase 1). ONE normaliser shared by dedupe, the companies
# table and discovery importers, so "Emergent Labs, Inc.", "Emergent" and
# "emergent.sh" can be recognised as the same employer.
# ---------------------------------------------------------------------------

import unicodedata as _ud
from urllib.parse import urlsplit as _urlsplit

_LEGAL_SUFFIXES = (
    "incorporated", "inc", "llc", "llp", "ltd", "limited", "pvt", "private",
    "corp", "corporation", "co", "company", "gmbh", "ag", "sa", "bv", "plc",
    "pte", "pty", "holdings",
    # Nordic / EU / US-benefit / Japanese forms ("Proxify AB", "Wolt Oy",
    # "Celonis SE", "Posit Software, PBC", "Booking.com B.V.", "Mercari K.K.")
    "ab", "se", "oy", "oyj", "pbc", "as", "asa", "sas", "srl", "spa", "kk", "nv",
    "sarl", "bv",
)
# Dotted initialisms collapse to one word first, so "B.V." / "S.p.A." / "K.K."
# match "bv" / "spa" / "kk" instead of splitting into single letters.
_DOTTED_RE = re.compile(r"\b(?:[a-z]\.){2,}(?![a-z])|\b(?:[a-z]\.)+[a-z]\b")
_PAREN_RE = re.compile(r"\([^)]*\)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_company_name(name: Optional[str]) -> str:
    """Stable matching key for an employer name.

    Lower-cased, accents folded, parenthetical aliases dropped ("LiteLLM
    (BerriAI)" -> "litellm"), punctuation collapsed, trailing legal suffixes
    removed ("Emergent Labs, Inc." -> "emergent labs"). Never empty for a
    non-empty input: if stripping would remove everything, the folded form is
    returned instead.
    """
    if not name:
        return ""
    s = _ud.normalize("NFKD", name)
    s = "".join(ch for ch in s if not _ud.combining(ch)).lower()
    folded = _NON_ALNUM_RE.sub(" ", s).strip()
    s = _PAREN_RE.sub(" ", s)
    s = _DOTTED_RE.sub(lambda m: m.group(0).replace(".", ""), s)
    words = _NON_ALNUM_RE.sub(" ", s).split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words) or folded


def domain_of(url: Optional[str]) -> str:
    """Host of a URL (or bare domain) without scheme, port, 'www.' or a
    trailing dot: 'https://www.Emergent.sh/careers' -> 'emergent.sh'."""
    if not url:
        return ""
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    host = (_urlsplit(u).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host
