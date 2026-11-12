"""Normalized data models shared by every connector.

Every connector, regardless of the ATS it talks to, returns a list of `Job`
objects with the same shape. That uniformity is the whole point: the filtering,
scoring, and tailoring layers downstream never need to know whether a role came
from Greenhouse, Lever, Ashby, or Workday.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit


# Allowed values for Job.workplace_type
REMOTE, ONSITE, HYBRID, UNKNOWN = "remote", "onsite", "hybrid", "unknown"
WORKPLACE_TYPES = {REMOTE, ONSITE, HYBRID, UNKNOWN}

# Allowed values for Job.country_bucket
IN, NON_IN, COUNTRY_UNKNOWN = "IN", "non-IN", "unknown"


# ---------------------------------------------------------------------------
# URL allowlist (S3). Every URL a third-party payload hands us ends up as a
# link the user clicks (or a fetch target), so Job.__post_init__ runs them all
# through safe_url: http(s) with a real public hostname, nothing else.
# ---------------------------------------------------------------------------
MAX_URL_LEN = 4096
_BAD_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")
_NUMERIC_HOST = re.compile(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+))*", re.I)
# Boards that have exactly one host: a link elsewhere is not that board's listing.
SOURCE_HOSTS: dict[str, tuple[str, ...]] = {
    "fourdayweek": ("4dayweek.io",),
    "jobicy": ("jobicy.com",),
    "arbeitnow": ("arbeitnow.com", "arbeitnow.co.uk"),   # the API links UK rows to .co.uk
    "nodesk": ("nodesk.co",),
    "hasjob": ("hasjob.co",),
}


def _public_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def safe_url(url: Any, *, hosts: Optional[Iterable[str]] = None) -> Optional[str]:
    """The URL if it is safe to store and link to, else None.

    Only http/https with a hostname, no userinfo, no whitespace / control
    characters / backslashes; not localhost; IP-literal hosts must be public
    (loopback, link-local, private, reserved and non-canonical forms such as
    "2130706433" or "0x7f.1" are rejected). With `hosts`, the host must equal
    one of them or be a subdomain of it.
    """
    if not isinstance(url, str):
        return None
    u = url.strip()
    if not u or len(u) > MAX_URL_LEN or _BAD_URL_CHARS.search(u):
        return None
    try:
        parts = urlsplit(u)
        host = (parts.hostname or "").lower().rstrip(".")
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        return None
    if host == "localhost" or host.endswith(".localhost"):
        return None
    if ":" in host or _NUMERIC_HOST.fullmatch(host):
        if not _public_ip(host):
            return None
    if hosts is not None:
        allowed = [h.lower().lstrip(".") for h in hosts]
        if not any(host == h or host.endswith("." + h) for h in allowed):
            return None
    return u


@dataclass
class Compensation:
    """Compensation as disclosed by a posting.

    Most postings disclose nothing, so this is usually None on a Job. When a
    source (currently Ashby) does provide numbers, we keep them raw and offer a
    best-effort INR annualization for the salary filter. That conversion is
    approximate by design — currency and interval handling are heuristics, not
    guarantees, which is why `is_estimate` exists.
    """
    currency: Optional[str] = None        # e.g. "USD", "INR"
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    interval: Optional[str] = None         # e.g. "1 YEAR", "1 HOUR", "1 MONTH"
    summary: Optional[str] = None          # human-readable string if the source gives one
    source: Optional[str] = None           # which connector/field produced this
    is_estimate: bool = False

    def annual_inr(self, usd_to_inr: float = 83.0) -> Optional[float]:
        """Best-effort annualized value in raw INR (not lakhs).

        Uses the upper bound when present (recruiters anchor high, and the
        filter is a floor test). Returns None when we genuinely can't convert —
        e.g. an unknown currency — so the caller never mistakes a guess for a
        real number.
        """
        val = self.max_value if self.max_value is not None else self.min_value
        if val is None:
            return None

        interval = (self.interval or "").upper()
        if "HOUR" in interval:
            val *= 2080          # 40h/week * 52 weeks
        elif "WEEK" in interval:
            val *= 52
        elif "MONTH" in interval:
            val *= 12
        elif "DAY" in interval:
            val *= 260

        cur = (self.currency or "").upper()
        if cur in ("", "INR"):
            return val
        if cur == "USD":
            return val * usd_to_inr
        # Unknown currency: refuse to guess.
        return None


@dataclass
class Job:
    """A single normalized job posting."""
    source: str                            # connector name: greenhouse/lever/ashby/workday
    company: str                           # display name from config
    company_sector: Optional[str]          # "tech" / "fintech" (from config, not the posting)
    external_id: str                       # stable id from the source
    title: str
    location_text: str
    workplace_type: str                    # remote/onsite/hybrid/unknown
    country_bucket: str                    # IN / non-IN / unknown
    url: str
    description_text: str
    department: Optional[str] = None
    compensation: Optional[Compensation] = None
    posted_at: Optional[str] = None        # ISO-ish string when available
    posted_age_days: Optional[int] = None  # approximate age in days; None if unknown
    freshness: str = "unknown"             # fresh | recent | aging | stale | unknown
    role_family: Optional[str] = None      # normalized matched target role
    seniority_high: bool = False           # staff/principal/director/lead/manager
    min_yoe_required: Optional[int] = None # parsed lower bound from JD
    yoe_is_preferred: bool = False         # soft mention ("preferred", etc.)
    # --- Phase 1: multi-source fields --------------------------------------
    apply_url: Optional[str] = None        # where Apply must go (board terms may require their link)
    region_restrictions: list[str] = field(default_factory=list)       # e.g. ["USA Only"], ["APAC"], ["Worldwide"]
    timezone_restrictions: list[float] = field(default_factory=list)   # UTC offsets the employer accepts
    tz_requirement_text: Optional[str] = None  # free-text TZ requirement as posted
    attribution_name: Optional[str] = None # e.g. "Himalayas" — shown as "via …" on the card
    attribution_url: Optional[str] = None  # followable link back to the listing on that source
    expires_at: Optional[str] = None       # ISO date when the source says the listing expires
    company_id: Optional[int] = None       # companies.id once linked
    sources: list[dict] = field(default_factory=list)  # every place this job was seen (dedupe.merge)
    canonical_key: Optional[str] = None    # cross-source identity (dedupe.canonical_key)
    raw: dict = field(default_factory=dict)  # original payload, kept for debugging

    def __post_init__(self) -> None:
        # Choke point for S3: no connector can store a javascript:/file:/internal URL.
        hosts = SOURCE_HOSTS.get((self.source or "").lower())
        self.url = safe_url(self.url, hosts=hosts) or ""
        self.apply_url = safe_url(self.apply_url, hosts=hosts)
        self.attribution_url = safe_url(self.attribution_url)

    @property
    def key(self) -> str:
        """Stable dedupe key across a single ingestion run."""
        return f"{self.source}:{self.company}:{self.external_id}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # raw can be large/noisy; keep it out of the slim export view
        d.pop("raw", None)
        return d
