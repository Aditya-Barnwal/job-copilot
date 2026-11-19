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

Phase 2: the per-job rules live in `eligibility.evaluate()` (lanes + exclusion reasons, docs/DESIGN.md §6).
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


# ---------------------------------------------------------------------------
# Role taxonomy (Phase 2 fix E2/E3/E12 + the user's approved families).
#
# Each family is a list of (pattern, needs_noun) rules. needs_noun=True means
# the pattern only counts when the title also has an engineering noun
# (engineer / developer / programmer), so "AI Trainer" or "Backend Ops
# Associate" never match. Tiers come from config/filters.yaml roles.families:
#   primary | secondary | startup_only | off
# startup_only families are targets only at funded / watchlist companies
# (eligibility decides; it knows the company tier).
# ---------------------------------------------------------------------------
TIER_PRIMARY, TIER_SECONDARY, TIER_STARTUP, TIER_OFF = "primary", "secondary", "startup_only", "off"
TIERS = (TIER_PRIMARY, TIER_SECONDARY, TIER_STARTUP, TIER_OFF)

_ENG_NOUN = (r"\b(?:engineer(?:s|ing)?|developers?|programmers?)\b|"
             r"(?<![a-z])(?:sde|swe|sdet|mle|fde|sre|a?mts)(?:i{1,3})?(?![a-z])|"
             r"\bmember\s+of\s+(?:the\s+)?technical\s+staff\b")
_ENG_NOUN_RE = re.compile(_ENG_NOUN, re.I)

# (family, [(regex, needs_noun, case_sensitive)]) - order breaks ties inside a tier
# (more specific families first).
_FAMILY_RULES: list[tuple[str, list[tuple[str, bool, bool]]]] = [
    ("fde", [(r"(?<![a-z])fde(?![a-z])", False, False),
             (r"\bforward[\s-]+deployed\b", True, False)]),
    ("rules_engine", [(r"\brules?[\s-]+engine\b|\bbrms\b|\bdrools\b", True, False)]),
    ("ai", [(r"\b(?:ai|a\.i\.|gen\s?ai|generative\s+ai|llms?|agentic|ai\s+agents?|prompt|conversational|"
             r"voice|speech|artificial\s+intelligence)\b", True, False)]),
    ("nlp", [(r"\bnlp\b|\bnatural\s+language\b", True, False)]),
    ("cv", [(r"\bcomputer\s+vision\b", True, False),
            (r"(?<![a-z])cv\s+(?:engineer|developer)s?\b", False, False)]),
    ("ml", [(r"\bmachine\s+learning\b|\bml\b|\bdeep\s+learning\b", True, False),
            (r"(?<![a-z])mle(?![a-z])", False, False)]),
    ("data_science", [
        (r"\b(?:data|applied|ml|machine\s+learning|ai|decision|nlp|quantitative)\s*"
         r"(?:(?:&|and|/)\s*(?:data|applied|ml|ai)\s*)?scientists?\b", False, False),
        (r"\bdata\s+science\b", True, False),
        (r"(?<![A-Za-z0-9])DS(?![A-Za-z])", False, True)]),
    ("data_engineering", [(r"\bdata\s+engineer(?:s|ing)?\b", False, False),
                          (r"\bbig\s+data\b|\bdata\s+(?:pipelines?|platform)\b", True, False)]),
    ("etl", [(r"\betl\b|\belt\b", True, False)]),
    ("java", [(r"\bjava\b|\bspring(?:\s+boot)?\b|\bj2ee\b", True, False)]),
    ("python", [(r"\bpython\b|\bdjango\b", True, False)]),
    ("backend", [(r"\bback[\s-]?end\b|\bserver[\s-]side\b|\bdistributed\s+systems\b|"
                  r"\b(?:golang|go|node(?:\.js)?|nodejs|ruby|rails|php|c\+\+|c#|\.net|scala|rust)\s+"
                  r"(?:developers?|engineers?)\b", True, False)]),
    ("founding", [(r"\bfounding\b", True, False),
                  (r"\bproduct\s+engineers?\b", False, False)]),
    ("data_analyst", [(r"\bdata\s+analysts?\b", False, False),
                      (r"(?<![A-Za-z0-9])DA(?![A-Za-z])", False, True)]),
    ("analytics_engineering", [(r"\banalytics\s+engineers?\b", False, False)]),
    ("mlops", [(r"\bml\s*ops\b|\bllm\s*ops\b", False, False)]),
    ("fullstack", [(r"\bfull[\s-]?stack\b", True, False)]),
    ("research_science", [(r"\bresearch\s+scientists?\b", False, False),
                          (r"\b(?:ai|ml|machine\s+learning|deep\s+learning|nlp|llm)\s+researchers?\b", False, False)]),
    ("research_engineering", [(r"\bresearch\s+engineers?\b", False, False)]),
    ("platform", [(r"\bplatform\s+(?:software\s+)?engineer(?:s|ing)?\b", False, False)]),
    ("cloud", [(r"\b(?:cloud|gcp|aws|azure)\s+(?:engineers?|developers?)\b", False, False)]),
    ("integration", [(r"\bintegrations?\s+(?:engineers?|developers?)\b|\bapi\s+(?:engineers?|developers?)\b",
                      False, False)]),
    ("sre", [(r"(?<![a-z])sre(?![a-z])|\bsite\s+reliability\b", False, False)]),
    ("observability", [(r"\bobservability\b", True, False)]),
    ("devops", [(r"\bdev\s*(?:sec\s*)?ops\b", False, False)]),
    ("qa", [(r"(?<![a-z])sdet(?![a-z])", False, False),
            (r"\bsoftware\s+(?:development\s+)?engineers?\s+in\s+test\b", False, False),
            (r"\b(?:qa|test|testing|quality\s+assurance)\s+automation\b|\bautomation\s+(?:qa|test(?:ing)?)\b|"
             r"\b(?:qa|test)\s+engineers?\b", True, False)]),
    ("solutions", [(r"\bsolutions?\s+engineers?\b", False, False)]),
    ("mobile", [(r"\b(?:android|ios|mobile|flutter|react\s+native)\b", True, False)]),
    ("frontend", [(r"\bfront[\s-]?end\b|\bui\s+(?:engineers?|developers?)\b|\breact(?:\.js)?\s+developers?\b|"
                   r"\bweb\s+developers?\b|\bangular\s+developers?\b|\bjavascript\s+developers?\b", True, False)]),
    ("amts", [(r"\bassociate\s+member\s+of\s+(?:the\s+)?technical\s+staff\b|(?<![a-z])amts(?![a-z])",
               False, False)]),
    ("mts", [(r"\bmember\s+of\s+(?:the\s+)?technical\s+staff\b|(?<![a-z])mts(?![a-z])", False, False)]),
    # Generic software engineering: last, so a specialisation in the title wins (E12).
    ("sde", [(r"\bsoftware\s+(?:development\s+)?(?:engineer(?:s|ing)?|developers?)\b|"
              r"(?<![a-z])(?:sde|swe)(?:i{1,3})?(?![a-z])|\bprogrammers?\b|\bapplications?\s+developers?\b|"
              r"\b(?:graduate|associate|trainee)\s+engineer\b|\bengineer(?:ing)?\s+trainee\b|"
              r"^\W*(?:junior\s+)?(?:engineer|developer)\s*(?:[-,]?\s*(?:i|ii|1|2)\b|$)", False, False),
             (r"(?<![A-Za-z])GET(?![A-Za-z])", False, True)]),
]
FAMILY_NAMES: list[str] = [f for f, _ in _FAMILY_RULES]
# A family whose title keyword contains another family's ("ML Ops", "Research Engineer, AI",
# "SRE, ML Platform", "QA Automation (Python)") is the more specific reading: it wins even
# when the contained family has a better tier.
_SUBSUMES: dict[str, set[str]] = {
    "mlops": {"ml", "ai"},
    "research_engineering": {"ai", "ml", "nlp", "cv"},
    "research_science": {"ai", "ml", "nlp", "cv", "data_science"},
    "sre": {"ai", "ml", "backend", "python", "java"},
    "observability": {"ai", "ml", "backend"},
    "devops": {"ai", "ml", "backend", "python", "java"},
    "qa": {"ai", "ml", "backend", "python", "java", "fullstack"},
}
_GENERIC_FAMILIES = {"sde"}

# The user's approved taxonomy (2026-11-12). Edit tiers in config/filters.yaml.
DEFAULT_ROLE_TIERS: dict[str, str] = {
    **{f: TIER_PRIMARY for f in ("sde", "backend", "java", "python", "ai", "nlp", "ml", "cv",
                                 "data_engineering", "data_science", "fde", "founding", "amts")},
    **{f: TIER_SECONDARY for f in ("data_analyst", "analytics_engineering", "mlops", "fullstack")},
    **{f: TIER_STARTUP for f in ("platform", "cloud", "integration", "research_engineering",
                                 "research_science", "sre", "observability", "devops", "qa", "solutions")},
    # off: MTS reads as senior (AMTS is kept); ETL Developer is covered by data_engineering;
    # rules-engine titles were dropped; mobile / frontend-only are not targets.
    **{f: TIER_OFF for f in ("mts", "etl", "rules_engine", "mobile", "frontend")},
}
# Detail shown on the "role" exclusion for an off family (default: "not a target role family").
OFF_FAMILY_DETAIL = {"mts": "MTS treated as senior; AMTS is kept"}

# Non-target functions: rejected even when an engineering noun is present
# ("Marketing AI Engineer", "Sales Engineer", "Support Engineer", "Customer Success MTS").
_TEAM_WORDS = r"(?!\s+(?:platform|tools?|tooling|systems?|infra\w*|automation|tech(?:nology)?|engineering))"
_HARD_FUNCTION_RE = re.compile(
    r"\b(?:marketing|marketer|sales|pre-?sales|support|helpdesk|help\s+desk)\b" + _TEAM_WORDS + "|"
    r"\bcustomer\s+(?:success|support|care|service)\b|\brecruit(?:er|ers|ing|ment)\b|\btalent\s+acquisition\b|"
    r"\btrainers?\b|\btutors?\b|\bannotators?\b|\blabell?ers?\b|\bvalidators?\b|\braters?\b|"
    r"\b(?:product|program|programme|project|delivery|account)\s+managers?\b|\bteachers?\b|\binstructors?\b|"
    r"\bdesign\s+automation\b|\bdigital\s+assistant\b",
    re.I)
_COMPILED_RULES = [(fam, [(re.compile(rx, 0 if cs else re.I), noun) for rx, noun, cs in rules])
                   for fam, rules in _FAMILY_RULES]

# Kept for v1 callers (ingest / discovery pass it to passes_role): the family names.
# A custom phrase that is not a family name still matches (as an extra target).
DEFAULT_TARGET_ROLES: list[str] = [f for f, t in DEFAULT_ROLE_TIERS.items() if t != TIER_OFF]


def role_tiers(roles_cfg: Optional[dict] = None) -> dict[str, str]:
    """Family -> tier from a filters.yaml `roles` block (missing families keep their defaults).

    `roles.families` may be the v2 mapping {family: tier} or a v1 list of phrases (ignored
    here; phrases are extra matches, see passes_role). allow_analyst_titles: false turns the
    data_analyst family off."""
    tiers = dict(DEFAULT_ROLE_TIERS)
    rc = roles_cfg or {}
    fams = rc.get("families")
    if isinstance(fams, dict):
        for k, v in fams.items():
            v = TIER_OFF if v is None or v is False else str(v).strip().lower()
            if k in tiers and v in TIERS:
                tiers[k] = v
    if rc.get("allow_analyst_titles") is False:
        tiers["data_analyst"] = TIER_OFF
    return tiers


def _family_hits(title: str) -> list[str]:
    noun = bool(_ENG_NOUN_RE.search(title))
    out = []
    for fam, rules in _COMPILED_RULES:
        for rx, needs_noun in rules:
            if (noun or not needs_noun) and rx.search(title):
                out.append(fam)
                break
    return out


def engineering_signal(title: Optional[str]) -> bool:
    """True when the title is an engineering role: an engineering noun or a family token
    (SDE / SWE / MLE / SDET / SRE / FDE / MTS / GET / DS / DA, data scientist, data analyst ...)
    and no non-target function (sales / marketing / support / customer success / recruiter /
    trainer / tutor / annotator / validator / product-program-project manager)."""
    t = title or ""
    if not t.strip() or _HARD_FUNCTION_RE.search(t):
        return False
    return bool(_ENG_NOUN_RE.search(t) or _family_hits(t))


# Non-software engineering disciplines. A generic title such as "Engineer I - Aerodynamics"
# otherwise passes as software engineering, so a discipline word makes it other_engineering
# (off) unless the title also says it is software / data / ML work.
_NON_SOFTWARE_DISCIPLINE_RE = re.compile(
    r"\b(?:aero(?:dynamics?|space|nautical|structures?)|avionics?|structures?|structural|"
    r"mechanical|mechatronics?|civil|electrical|electronics?|hardware|firmware|pcb|vlsi|asic|fpga|"
    r"analog|rf|antenna|propulsion|thermal|cfd|fea|cae|cad|hvac|piping|chemical|metallurg\w*|"
    r"manufacturing|production|process\s+engineer|instrumentation|power\s+(?:systems?|electronics)|"
    r"automotive\s+design|flight\s+(?:dynamics|test)|mission\s+systems|gnc|payload|"
    r"geotechnical|surveying|mining|petroleum|biomedical\s+engineer)\b", re.I)
_SOFTWARE_WORD_RE = re.compile(
    r"\b(?:software|developer|sde|swe|backend|back[\s-]?end|full[\s-]?stack|data|ml|machine\s+learning|"
    r"ai|llm|cloud|devops|platform|web|api|python|java|golang|go|node|react|qa|sdet|test\s+automation)\b", re.I)


def non_software_discipline(title: Optional[str]) -> Optional[str]:
    """The discipline word when the title names non-software engineering, else None."""
    t = title or ""
    m = _NON_SOFTWARE_DISCIPLINE_RE.search(t)
    if m and not _SOFTWARE_WORD_RE.search(t):
        return m.group(0)
    return None


def role_match(title: Optional[str], roles_cfg: Optional[dict] = None) -> Optional[tuple[str, str]]:
    """(family, tier) for a title, or None when it is not an engineering title.

    tier is "off" for a family switched off (mobile / frontend by default) and for an
    engineering title with no known family ("Mechanical Engineer" -> ("other_engineering", "off")).
    A generic family (Software Engineer / MTS) loses to any specialisation in the title, and an
    off family (frontend / mobile) wins over the generic one: "Software Engineer, Frontend" is
    frontend-only, "Software Engineer, Infrastructure" stays sde."""
    t = title or ""
    if not engineering_signal(t):
        return None
    if non_software_discipline(t):
        return ("other_engineering", TIER_OFF)
    tiers = role_tiers(roles_cfg)
    hits = _family_hits(t)
    for fam, inner in _SUBSUMES.items():
        if fam in hits:
            hits = [h for h in hits if h not in inner]
    if not hits:
        return ("other_engineering", TIER_OFF)
    rank = {TIER_PRIMARY: 0, TIER_SECONDARY: 1, TIER_STARTUP: 2}
    if "mts" in hits and "amts" not in hits and tiers.get("mts") == TIER_OFF:
        return "mts", TIER_OFF       # "MTS, Machine Learning" is still an MTS title
    special = [h for h in hits if h not in _GENERIC_FAMILIES]
    enabled_special = [h for h in special if tiers.get(h, TIER_OFF) != TIER_OFF]
    if enabled_special:
        best = min(enabled_special, key=lambda h: (rank[tiers[h]], FAMILY_NAMES.index(h)))
        return best, tiers[best]
    if special:                      # only off families (frontend / mobile) besides the generic one
        return special[0], TIER_OFF
    enabled = [h for h in hits if tiers.get(h, TIER_OFF) != TIER_OFF]
    if enabled:
        best = min(enabled, key=lambda h: (rank[tiers[h]], FAMILY_NAMES.index(h)))
        return best, tiers[best]
    return hits[0], TIER_OFF


def _normalize_phrase(phrase: str) -> str:
    return re.sub(r"\s+", "_", phrase.strip().lower()).replace("/", "_")


def passes_role(title: str, target_roles: Optional[Iterable[str]] = None,
                roles_cfg: Optional[dict] = None) -> Optional[str]:
    """Matched role family, or None.

    Taxonomy first (tiers from `roles_cfg`; startup_only families pass here - eligibility
    applies the company-tier condition). Entries of `target_roles` that are not family names
    are extra phrases, matched on word boundaries, still behind engineering_signal()."""
    m = role_match(title, roles_cfg)
    if m and m[1] != TIER_OFF:
        return m[0]
    if not title or not engineering_signal(title):
        return None
    t = title.lower()
    for phrase in target_roles or []:
        p = str(phrase).strip().lower()
        if not p or p in DEFAULT_ROLE_TIERS:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(p) + r"s?(?![a-z0-9])", t):
            return _normalize_phrase(p)
    return None


def is_seniority_high(title: str) -> bool:
    """Senior+ title; ONE word-boundary regex shared with yoe.title_level and fit (E9)."""
    from .yoe import is_senior_title
    return is_senior_title(title)


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
    custom = [r for r in config.target_roles if str(r).strip().lower() not in DEFAULT_ROLE_TIERS]
    if custom:
        cfg.setdefault("roles", {})["extra_phrases"] = list(custom)
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

        family = passes_role(job.title, config.target_roles, (cfg.get("roles") or {}))
        if family is None or any(r["rule"] == "role" and "startup-only" in r["detail"]
                                 for r in decision.reasons):
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
