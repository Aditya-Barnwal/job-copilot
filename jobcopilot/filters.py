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

Phase 2: the per-job rules live in `eligibility.evaluate()` (lanes + D8 reasons).
`apply_filters` attaches that Decision to every FilterResult; FilterConfig.mode
"eligibility" makes it keep/drop by the Decision, "legacy" (default) keeps v1 rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Optional

from .models import Job, IN, NON_IN, REMOTE
from .yoe import extract_min_yoe  # noqa: F401  (re-exported for v1 callers)

if TYPE_CHECKING:
    from .eligibility import Decision


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
    # v2 additions (full-stack / GenAI / developer titles); generic words last so the
    # specific families above win the returned name.
    "software developer",
    "full stack",
    "full-stack",
    "fullstack",
    "backend",
    "back-end",
    "genai",
    "gen ai",
    "generative ai",
    "llm engineer",
    "applied ai",
    "ai developer",
    "ai/ml",
    "machine learning",
    "deep learning",
    "nlp engineer",
    "research engineer",
    "member of technical staff",
    "python developer",
    "java developer",
    "golang developer",
    "node.js developer",
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
    # Reject 4-digit-year false positives by masking them out first; "Member of
    # Technical Staff" is an IC title at AI labs, not a Staff-level one.
    raw = _YEAR_LIKE.sub("", title)
    raw = re.sub(r"member\s+of\s+(?:the\s+)?technical\s+staff", "MTS", raw, flags=re.I)
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
    # Phase 2: "legacy" keeps the v1 keep/drop rules above (each result still gets a
    # .decision); "eligibility" keeps exactly the jobs eligibility.evaluate() does not
    # exclude, using config/filters.yaml (overridden by min_lpa/usd_to_inr/target_roles/
    # keep_unknown_country set here).
    mode: str = "legacy"
    cfg: Optional[dict] = None          # filters config; None = config.filters()
    companies: Optional[dict] = None    # {company_id or company name: company row} for tiers


@dataclass
class FilterResult:
    job: Job
    package_estimated: bool
    country_uncertain: bool
    decision: Optional["Decision"] = None   # eligibility.Decision (lane + reasons)


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


def eligibility_cfg(config: FilterConfig) -> dict:
    """config/filters.yaml merged with the explicit FilterConfig knobs."""
    import copy
    from . import config as _config
    cfg = copy.deepcopy(config.cfg) if config.cfg is not None else _config.filters()
    cfg.setdefault("roles", {})["families"] = list(config.target_roles)
    cfg.setdefault("salary_floors", {})["INR"] = float(config.min_lpa) * 100_000
    cfg.setdefault("fx_to_inr", {})["USD"] = float(config.usd_to_inr)
    cfg.setdefault("geo", {})["keep_unknown_country"] = bool(config.keep_unknown_country)
    return cfg


def _company_for(job: Job, companies: Optional[dict]) -> Optional[dict]:
    if not companies:
        return None
    return companies.get(job.company_id) or companies.get(job.company)


def apply_filters(jobs: Iterable[Job], config: FilterConfig | None = None) -> list[FilterResult]:
    """Keep the jobs that pass; each FilterResult carries its eligibility Decision.

    Use eligibility.evaluate() directly when the excluded jobs (with reasons) are needed.
    """
    from .eligibility import evaluate

    config = config or FilterConfig()
    cfg = eligibility_cfg(config)
    results: list[FilterResult] = []
    for job in jobs:
        decision = evaluate(job, company=_company_for(job, config.companies), cfg=cfg)
        if config.mode == "eligibility":
            if decision.excluded:
                continue
            if config.max_age_days is not None and job.posted_age_days is not None \
                    and job.posted_age_days > config.max_age_days:
                continue
            _stamp(job, decision)
            results.append(FilterResult(
                job=job, package_estimated=decision.salary_ok is None,
                country_uncertain=(job.country_bucket not in (IN, NON_IN)), decision=decision))
            continue

        family = passes_role(job.title, config.target_roles)
        if family is None:
            continue
        _stamp(job, decision)
        job.role_family = family

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
                decision=decision,
            )
        )
    return results


def _stamp(job: Job, decision: "Decision") -> None:
    """Copy the YOE v2 / seniority reading onto the Job (stored by store.upsert_jobs)."""
    job.role_family = decision.role_family or job.role_family
    job.seniority_high = decision.seniority_high
    job.min_yoe_required = decision.yoe_min
    job.yoe_is_preferred = decision.yoe_is_preferred
