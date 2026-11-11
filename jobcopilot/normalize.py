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


# A small, ext-friendly set of signals for the India / non-India geo bucket.
# This is intentionally not exhaustive; add to it as you see your real feeds.
_INDIA_SIGNALS = (
    "india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
    "mumbai", "gurgaon", "gurugram", "noida", "delhi", "kolkata",
    "ahmedabad", "kochi", "coimbatore", "thiruvananthapuram", "trivandrum",
    "indore", "jaipur",
)

# Strong non-India signals. Presence of one of these *without* an India signal
# pushes a posting into the non-IN bucket (where only remote roles survive).
_NON_INDIA_SIGNALS = (
    "united states", "usa", "u.s.", " uk", "united kingdom", "canada",
    "germany", "ireland", "france", "netherlands", "singapore", "australia",
    "poland", "spain", "israel", "japan", "san francisco", "new york",
    "seattle", "london", "berlin", "dublin", "toronto", "austin", "boston",
    "amsterdam", "sydney", "tel aviv",
)


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
    t = " " + location_text.lower() + " "
    if any(sig in t for sig in _INDIA_SIGNALS):
        return IN
    if any(sig in t for sig in _NON_INDIA_SIGNALS):
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
)
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
