"""Filter rules.

These encode the scope you defined:

  * Geo + work-type: inside India keep remote/onsite/hybrid; outside India keep
    only fully-remote roles. Unknown-country roles are kept and flagged, never
    silently dropped.
  * Sector: tech / fintech, taken from the company config (not the posting).
  * Salary floor: roles with disclosed pay must clear the floor; roles with no
    disclosed pay are KEPT and flagged `package_estimated=True`.
  * Role targeting: only keep titles matching the target role list. Multi-word
    phrases match as substrings; short tokens (sde/swe/ml/ai) match on word
    boundaries.
  * Age: never drops on age. `--max-age-days` is opt-in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .models import Job, IN, NON_IN, REMOTE
from .yoe import extract_min_yoe


DEFAULT_TARGET_ROLES: list[str] = [
    "software engineer",
    "software development engineer",
    "sde",
    "swe",
    "backend engineer",
    "back end engineer",
    "backend developer",
    "machine learning engineer",
    "ml engineer",
    "ai engineer",
    "ai/ml engineer",
    "applied scientist",
    "data scientist",
    "data engineer",
]

# Tokens that must match on word boundaries to avoid false positives.
_SHORT_TOKENS = {"sde", "swe", "ml", "ai", "ai/ml"}

_SENIORITY_HIGH = (
    "senior", " sr ", " sr.", "staff", "principal", "distinguished",
    "director", "architect", " lead", "manager", "head of", "vp ", "vice president",
)

# Numeric-level patterns (Part 1a). Conservative: III/IV/V+ and 3+ flag;
# I/II/1/2 do NOT hard-flag (over-hiding collapses the list).
_SENIORITY_LEVEL_RES = [
    re.compile(r"\bL[4-9]\b", re.I),
    re.compile(r"\bSDE\s*[3-9]\b", re.I),
    re.compile(r"\b(?:engineer|developer|scientist|sde|analyst|researcher)\s+(?:iii|iv|v|vi|[3-9])\b", re.I),
]
_YEAR_LIKE = re.compile(r"\b(19|20)\d{2}\b")


def _normalize_phrase(phrase: str) -> str:
    return re.sub(r"\s+", "_", phrase.strip().lower()).replace("/", "_")


def passes_role(title: str, target_roles: Iterable[str]) -> Optional[str]:
    """Return the matched role family (normalized), or None if no match."""
    if not title:
        return None
    t = title.lower()
    for phrase in target_roles:
        p = phrase.strip().lower()
        if not p:
            continue
        if p in _SHORT_TOKENS:
            if re.search(r"\b" + re.escape(p) + r"\b", t):
                return _normalize_phrase(p)
        else:
            if p in t:
                return _normalize_phrase(p)
    return None


def is_seniority_high(title: str) -> bool:
    if not title:
        return False
    # Reject 4-digit-year false positives by masking them out first.
    raw = _YEAR_LIKE.sub("", title)
    t = " " + raw.lower() + " "
    if any(s in t for s in _SENIORITY_HIGH):
        return True
    return any(p.search(raw) for p in _SENIORITY_LEVEL_RES)


@dataclass
class FilterConfig:
    min_lpa: float = 15.0
    sectors: Optional[set[str]] = None
    usd_to_inr: float = 83.0
    keep_unknown_country: bool = True
    target_roles: list[str] = field(default_factory=lambda: list(DEFAULT_TARGET_ROLES))
    max_age_days: Optional[int] = None  # opt-in; None = never drop on age


@dataclass
class FilterResult:
    job: Job
    package_estimated: bool
    country_uncertain: bool


def passes_geo(job: Job, keep_unknown: bool = True) -> bool:
    if job.country_bucket == IN:
        return True
    if job.country_bucket == NON_IN:
        return job.workplace_type == REMOTE
    return keep_unknown


def passes_salary(job: Job, min_lpa: float, usd_to_inr: float) -> tuple[bool, bool]:
    if job.compensation:
        inr = job.compensation.annual_inr(usd_to_inr)
        if inr is not None:
            return (inr / 100_000.0) >= min_lpa, False
    return True, True


def passes_sector(job: Job, sectors: Optional[set[str]]) -> bool:
    if not sectors:
        return True
    if not job.company_sector:
        return True
    return job.company_sector in sectors


def apply_filters(jobs: Iterable[Job], config: FilterConfig | None = None) -> list[FilterResult]:
    config = config or FilterConfig()
    results: list[FilterResult] = []
    for job in jobs:
        family = passes_role(job.title, config.target_roles)
        if family is None:
            continue
        job.role_family = family
        job.seniority_high = is_seniority_high(job.title)
        yoe, is_pref = extract_min_yoe(job.description_text)
        job.min_yoe_required = yoe
        job.yoe_is_preferred = is_pref

        if not passes_sector(job, config.sectors):
            continue
        if not passes_geo(job, config.keep_unknown_country):
            continue
        ok, estimated = passes_salary(job, config.min_lpa, config.usd_to_inr)
        if not ok:
            continue
        if config.max_age_days is not None and job.posted_age_days is not None:
            if job.posted_age_days > config.max_age_days:
                continue
        results.append(
            FilterResult(
                job=job,
                package_estimated=estimated,
                country_uncertain=(job.country_bucket not in (IN, NON_IN)),
            )
        )
    return results
