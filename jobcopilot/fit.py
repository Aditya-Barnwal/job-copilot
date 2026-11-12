"""Fit score 0-100 with visible components and a one-line "why" (Phase 2).

    profile = load_profile()                 # résumé facts + config, once per run
    result  = score(job_dict, profile=profile, company=company_row, decision=decision)

Components (each 0-100), weighted by config/scoring.yaml `weights`:
    skills     JD must-have / nice-to-have coverage by your skills (synonym-aware)
    role       title family vs your target families
    seniority  fresher / new-grad / YOE floor vs your YOE
    location   Bengaluru > other Indian metro > remote India-eligible > India > unknown
    company    tier: brand > funded (YC / Series B+ higher) > watchlist
    salary     disclosed pay vs the per-currency floor; undisclosed = neutral 50
    freshness  posting age
    semantic   MiniLM cosine résumé vs JD; optional (cfg `semantic: false`, no
               encoder, or the model failing to load -> skipped and the other
               weights renormalised)

Deterministic and LLM-free. Profile skills come from résumé facts whose status is
confirmed OR unconfirmed (scoring ranks jobs, it never writes a claim), but NEVER
from pending facts or pending claims (e.g. Docker / ECS until Phase 3b lands):
those are cut by facts.usable_facts() exactly as tailoring cuts them.
"""
from __future__ import annotations

import copy
import dataclasses
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import yaml

from . import skills as SK

log = logging.getLogger("jobcopilot.fit")

COMPONENTS = ("skills", "role", "seniority", "location", "company", "salary", "freshness", "semantic")

DEFAULTS: dict[str, Any] = {
    "weights": {"skills": 0.30, "role": 0.15, "seniority": 0.15, "location": 0.10,
                "company": 0.10, "salary": 0.05, "freshness": 0.05, "semantic": 0.10},
    "semantic": True,
    "semantic_cosine_range": [0.15, 0.65],   # MiniLM résumé/JD cosines map linearly onto 0-100
    "skills": {"must_weight": 0.7, "nice_weight": 0.3, "no_skills_score": 50, "top_n": 8,
               "min_description_chars": 15, "no_description_semantic": 50},
    "role": {
        # tier from filters.yaml roles.families -> points
        "tier_scores": {"primary": 100, "secondary": 85, "startup_only": 60},
        # families that are "off" (normally excluded by eligibility) and non-family titles
        "scores": {"frontend": 55, "mobile": 45, "other_engineering": 40, "other": 20},
    },
    "seniority": {"fresher": 100, "at_or_below_user": 100, "one_above_user": 85,
                  "preferred_stretch": 60, "preferred_far": 40, "hard_above": 20,
                  "unknown": 70, "junior_title": 95, "senior_title": 15, "intern": 50},
    "location": {"preferred_city": 100, "remote_india_eligible": 90, "india_metro": 85,
                 "india_other": 75, "unknown": 60, "remote_abroad_unclear": 55, "not_eligible": 10,
                 "india_metros": ["hyderabad", "pune", "chennai", "mumbai", "delhi", "new delhi",
                                  "gurgaon", "gurugram", "noida", "ncr", "kolkata", "ahmedabad",
                                  "navi mumbai", "thane", "kochi", "coimbatore", "jaipur", "indore"]},
    "company": {"brand": 100, "funded": 90, "funded_strong": 95, "yc_top": 98, "watchlist": 60,
                "unknown": 40, "excluded": 0, "missing": 50},
    "salary": {"missing": 50, "below_floor": 20, "at_floor": 75, "full_marks_ratio": 2.0,
               "unknown_currency": 50},
    "freshness": {"steps": [[7, 100], [14, 90], [30, 75], [60, 50]], "older": 30, "unknown": 60},
    "why_bands": [[75, "Strong match"], [55, "Good match"], [40, "Partial match"], [0, "Weak match"]],
}

DEFAULT_TARGET_FAMILIES = ["sde", "backend", "java", "python", "ai", "nlp", "ml", "cv",
                           "data_engineering", "data_science", "fde", "founding", "amts"]


# ------------------------------------------------------------------- config

def _deep_merge(base: dict, override: Mapping | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """config/scoring.yaml merged over DEFAULTS (missing file -> defaults)."""
    if path is None:
        from . import config
        path = config.config_dir() / "scoring.yaml"
    p = Path(path)
    data: dict = {}
    if p.exists():
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{p}: top level must be a mapping")
    return _deep_merge(DEFAULTS, data)


def _resolve_cfg(cfg: Mapping | None) -> dict[str, Any]:
    return load_config() if cfg is None else _deep_merge(DEFAULTS, cfg)


def _filters(cfg: Mapping) -> dict[str, Any]:
    if isinstance(cfg.get("filters"), Mapping):
        from . import config
        return _deep_merge(config.DEFAULTS["filters"], cfg["filters"])
    from . import config
    return config.filters()


# ------------------------------------------------------------------- titles

_INTERN_RE = re.compile(r"\b(intern|internship|apprentice|apprenticeship|co-?op)\b", re.I)


def classify_title(title: Optional[str], roles_cfg: Optional[Mapping] = None) -> str:
    """Role family of a title from the ONE taxonomy in filters (same gate as eligibility):
    'Backend Engineer' -> 'backend', 'Software Engineer, Infrastructure' -> 'sde',
    'Mechanical Engineer' -> 'other_engineering', 'Marketing AI Engineer' / 'Data Validator'
    -> 'other'. Internships are reported separately via is_intern()."""
    from .filters import role_match
    m = role_match(title or "", dict(roles_cfg) if roles_cfg is not None else None)
    return m[0] if m else "other"


def is_intern(title: Optional[str]) -> bool:
    # "Graduate Engineer Trainee" / "Trainee Software Engineer" are full-time fresher roles.
    return bool(_INTERN_RE.search(title or ""))


_JUNIOR_TITLE = re.compile(r"\b(junior|jr\.?|associate|entry[\s-]level|graduate|new grad|fresher|"
                           r"early career|university grad)\b|\b(?:sde|engineer|developer|swe)\s*(?:i|1)\b", re.I)
_FRESHER_TEXT = re.compile(r"\b(freshers?|new grads?|new graduates?|recent graduates?|entry[\s-]level|"
                           r"early[\s-]career|campus hire|graduate program|0\s*[-–to]+\s*[12]\s*(?:\+\s*)?years?|"
                           r"0\+?\s*years?|no (?:prior )?experience (?:is )?required|class of 202\d|202[4-7] grad)",
                           re.I)


# ------------------------------------------------------------------ profile

def _senior_title(title: str) -> bool:
    from .yoe import is_senior_title
    return is_senior_title(title)


@dataclass
class Profile:
    skills: set[str] = field(default_factory=set)          # canonical, closed under skills.IMPLIES
    direct_skills: set[str] = field(default_factory=set)   # named by a fact (before IMPLIES)
    role_families: list[str] = field(default_factory=lambda: list(DEFAULT_TARGET_FAMILIES))  # primary tier
    roles_cfg: dict = field(default_factory=dict)          # filters.yaml `roles` (tiers per family)
    user_yoe: int = 1
    preferred_cities: list[str] = field(default_factory=lambda: ["bengaluru", "bangalore"])
    resume_text: str = ""
    _resume_vec: Any = field(default=None, repr=False, compare=False)


def _scoring_facts(facts: Any) -> list:
    """Facts usable for SCORING: confirmed + unconfirmed, never pending.

    Promotes unconfirmed facts and claims to confirmed on a copy and runs the
    same fail-closed usable_facts() that tailoring uses, so pending facts and
    pending claims (and anything that can't be cut exactly) stay out."""
    from . import facts as F
    promoted = copy.deepcopy(facts)
    for f in promoted.facts():
        if f.status == "unconfirmed":
            f.status = "confirmed"
        for c in f.claims:
            if c.status == "unconfirmed":
                c.status = "confirmed"
    return F.usable_facts(promoted)


def load_profile(facts_path: Path | str | None = None, *, cfg: Mapping | None = None) -> Profile:
    """Profile for scoring from résumé facts + config.

    skills: canonical skills named by usable-for-scoring facts (skill facts,
    bullet skill tags, project/hackathon tech lines, experience/project bullet
    text), closed under skills.IMPLIES. A missing or unreadable facts file
    gives an empty skill set (the skills component then scores 0 coverage)."""
    from . import facts as F
    c = _resolve_cfg(cfg)
    flt = _filters(c)
    prof = Profile(
        user_yoe=int((flt.get("yoe") or {}).get("user_yoe") or 1),
        preferred_cities=[str(x).lower() for x in (flt.get("geo") or {}).get("india_preferred_cities") or []],
    )
    from .filters import TIER_PRIMARY, role_tiers
    prof.roles_cfg = dict(flt.get("roles") or {})
    prof.role_families = [f for f, t in role_tiers(prof.roles_cfg).items() if t == TIER_PRIMARY]
    try:
        rf = F.load(facts_path)
    except Exception as e:  # noqa: BLE001 - a broken facts file must not break ranking
        log.warning("fit: résumé facts unavailable (%s); scoring without skills", type(e).__name__)
        return prof
    usable = _scoring_facts(rf)
    found: list[str] = []
    texts: list[str] = []
    for f in usable:
        texts.extend(t for t in f.all_texts() if t)
        found.extend(SK.normalize(f.skills))
        found.extend(SK.normalize(getattr(f, "tech", []) or []))
        for t in f.all_texts():
            found.extend(SK.find_skills(t))
    prof.direct_skills = set(found)
    prof.skills = SK.expand(found)
    prof.resume_text = "\n".join(dict.fromkeys(texts))
    return prof


# ------------------------------------------------------------------- result

@dataclass
class FitResult:
    score: float
    components: dict[str, float]
    weights: dict[str, float]
    matched_skills: list[str]
    missing_skills: list[str]
    why: str
    flags: list[str] = field(default_factory=list)     # e.g. "low_confidence_skills", "semantic_skipped"
    role_family: str = "other"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------- job access

def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """First non-None of obj[name] / obj.name."""
    if obj is None:
        return default
    for n in names:
        v = obj.get(n) if isinstance(obj, Mapping) else getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _job_text(job: Mapping) -> str:
    return f"{job.get('title') or ''}\n{job.get('description_text') or ''}"


# --------------------------------------------------------------- components

def _skills_component(jd: SK.SkillHits, profile: Profile, c: dict) -> tuple[float, list[str], list[str], bool]:
    """Coverage of the JD's requirement units; an any-of unit ("Python, Java, or Go") counts
    as met when the profile has any one of them, and as ONE miss otherwise."""
    kc = c["skills"]
    have = profile.skills
    must_u, nice_u = SK.units(jd, "must"), SK.units(jd, "nice")
    top = int(kc.get("top_n") or 8)
    if not must_u and not nice_u:
        return float(kc["no_skills_score"]), [], [], True

    def split(units: list[tuple[str, ...]]) -> tuple[list[str], list[str], int]:
        hit, miss, n = [], [], 0
        for u in units:
            got = [x for x in u if x in have]
            if got:
                n += 1
                hit.extend(got)
            else:
                miss.extend(u)
        return hit, miss, n

    m_hit, m_miss, m_n = split(must_u)
    n_hit, n_miss, n_n = split(nice_u)
    matched = list(dict.fromkeys(m_hit + n_hit))[:top]
    missing = list(dict.fromkeys(m_miss + n_miss))[:top]
    parts = []
    if must_u:
        parts.append((float(kc["must_weight"]), m_n / len(must_u)))
    if nice_u:
        parts.append((float(kc["nice_weight"]), n_n / len(nice_u)))
    wsum = sum(w for w, _ in parts) or 1.0
    return 100.0 * sum(w * v for w, v in parts) / wsum, matched, missing, False


def _role_component(title: str, profile: Profile, c: dict) -> tuple[float, str]:
    from .filters import TIER_OFF, role_tiers
    rc = c["role"]
    fam = classify_title(title, profile.roles_cfg)
    tier = role_tiers(profile.roles_cfg).get(fam, TIER_OFF)
    tscores = rc.get("tier_scores") or {}
    if tier != TIER_OFF and tier in tscores:
        return float(tscores[tier]), fam
    sc = rc.get("scores") or {}
    default = sc.get("other_engineering", 40) if fam != "other" else sc.get("other", 20)
    return float(sc.get(fam, default)), fam


def _seniority_component(job: Mapping, decision: Any, profile: Profile, c: dict) -> tuple[float, str]:
    sc = c["seniority"]
    title = job.get("title") or ""
    text = _job_text(job)
    yoe = _get(decision, "yoe_min")
    if yoe is None:
        yoe = _get(job, "yoe_min", "min_yoe_required")
    pref = bool(_get(decision, "yoe_is_preferred", default=_get(job, "yoe_is_preferred", default=False)))
    fresher = _get(decision, "fresher_friendly")
    if fresher is None:
        fresher = _get(job, "fresher_friendly")
    if fresher is None:
        fresher = bool(_JUNIOR_TITLE.search(title) and re.search(r"(?i)graduate|new grad|fresher|entry", title)) \
            or bool(_FRESHER_TEXT.search(text))
    senior = _senior_title(title) or bool(job.get("seniority_high"))
    u = profile.user_yoe
    if is_intern(title):
        return float(sc["intern"]), "internship"
    if senior:
        return float(sc["senior_title"]), "senior title"
    if fresher or (yoe is not None and int(yoe) == 0):
        return float(sc["fresher"]), "new-grad friendly"
    if yoe is None:
        if _JUNIOR_TITLE.search(title):
            return float(sc["junior_title"]), "junior title"
        return float(sc["unknown"]), ""
    y = int(yoe)
    if y <= u:
        return float(sc["at_or_below_user"]), f"{y}+ yrs"
    if y == u + 1:
        return float(sc["one_above_user"]), f"{y}+ yrs"
    if pref:
        return float(sc["preferred_stretch"] if y <= u + 2 else sc["preferred_far"]), f"{y}+ yrs preferred"
    return float(sc["hard_above"]), f"{y}+ yrs required"


def _india_eligible(job: Mapping, decision: Any) -> Optional[bool]:
    v = _get(decision, "india_eligible")
    if v is None:
        v = _get(job, "india_eligible")
    if v is not None:
        return bool(v)
    regions = job.get("region_restrictions") or []
    if isinstance(regions, str):
        regions = [regions]
    blob = " ".join(str(r) for r in regions).lower() + " " + str(job.get("location_text") or "").lower()
    if re.search(r"\b(india|apac|asia|worldwide|anywhere|global)\b", blob):
        return True
    if re.search(r"\b(us|usa|united states|eu|europe|uk|canada|americas|latam|emea)\b(?:[\s-]*only)?", blob):
        return False
    return None


def _location_component(job: Mapping, decision: Any, profile: Profile, c: dict) -> tuple[float, str]:
    lc = c["location"]
    loc = str(job.get("location_text") or "").lower()
    bucket = job.get("country_bucket") or "unknown"
    wp = (job.get("workplace_type") or "unknown").lower()
    def has(city: str) -> bool:
        return re.search(r"(?<![a-z])" + re.escape(city) + r"(?![a-z])", loc) is not None
    pref = next((city for city in profile.preferred_cities if has(city)), None)
    if pref and bucket != "non-IN":
        return float(lc["preferred_city"]), "Bengaluru" if pref in ("bengaluru", "bangalore") else pref.title()
    elig = _india_eligible(job, decision)
    if bucket == "IN":
        if wp == "remote":
            return float(lc["remote_india_eligible"]), "Remote (India)"
        metro = next((m for m in lc["india_metros"] if has(m)), None)
        if metro:
            return float(lc["india_metro"]), metro.title()
        return float(lc["india_other"]), "India"
    if bucket == "non-IN":
        if wp != "remote" or elig is False:
            return float(lc["not_eligible"]), "not India-eligible"
        if elig:
            return float(lc["remote_india_eligible"]), "Remote (India OK)"
        return float(lc["remote_abroad_unclear"]), "Remote abroad (eligibility unclear)"
    if wp == "remote" and elig:
        return float(lc["remote_india_eligible"]), "Remote (India OK)"
    return float(lc["unknown"]), ""


def _company_component(job: Mapping, company: Optional[Mapping], c: dict) -> tuple[float, str]:
    cc = c["company"]
    tier = _get(company, "tier") or _get(job, "company_tier", "tier")
    if not tier:
        return float(cc["missing"]), ""
    tier = str(tier).lower()
    if tier == "funded":
        from .companies import short_yc_batch, stage_level
        yc = short_yc_batch(_get(company, "yc_batch") or _get(job, "yc_batch"))
        stage = _get(company, "funding_stage") or _get(job, "funding_stage")
        if bool(_get(company, "yc_top_company") or _get(job, "yc_top_company")):
            return float(cc["yc_top"]), f"funded (YC {yc})" if yc else "funded (YC top company)"
        lvl = stage_level(stage)
        if yc:
            return float(cc["funded_strong"]), f"funded (YC {yc})"
        if lvl is not None and lvl >= 3:
            return float(cc["funded_strong"]), f"funded ({stage})"
        return float(cc["funded"]), f"funded ({stage})" if stage else "funded"
    if tier in cc:
        return float(cc[tier]), tier if tier in ("brand", "watchlist") else ""
    return float(cc["unknown"]), ""


_INTERVAL_MULT = (("HOUR", 2080), ("DAY", 260), ("WEEK", 52), ("MONTH", 12))


def annual_salary(job: Mapping) -> tuple[Optional[float], Optional[str]]:
    """(annual amount in its own currency, currency) using the midpoint of min/max."""
    comp = job.get("compensation") if isinstance(job.get("compensation"), Mapping) else None
    cur = (comp or {}).get("currency") or job.get("comp_currency")
    lo = (comp or {}).get("min_value") if comp else job.get("comp_min")
    hi = (comp or {}).get("max_value") if comp else job.get("comp_max")
    interval = str(((comp or {}).get("interval") if comp else job.get("comp_interval")) or "").upper()
    vals = [float(v) for v in (lo, hi) if isinstance(v, (int, float)) and v > 0]
    if not vals:
        return None, None
    val = sum(vals) / len(vals)
    for key, mult in _INTERVAL_MULT:
        if key in interval:
            val *= mult
            break
    return val, (str(cur).upper() if cur else "INR")


def _salary_component(job: Mapping, decision: Any, flt: Mapping, c: dict) -> tuple[float, str]:
    sc = c["salary"]
    floors = {k.upper(): float(v) for k, v in (flt.get("salary_floors") or {}).items()}
    fx = {k.upper(): float(v) for k, v in (flt.get("fx_to_inr") or {}).items()}
    fx["INR"] = 1.0
    val, cur = annual_salary(job)
    ratio: Optional[float] = None
    if val is not None and cur in floors and floors[cur] > 0:
        ratio = val / floors[cur]
    elif val is not None and cur in fx and floors.get("INR"):
        ratio = val * fx[cur] / floors["INR"]
    elif val is None:
        inr = _get(decision, "salary_annual_inr")
        if inr is None:
            inr = _get(job, "salary_annual_inr")
        if inr is not None and floors.get("INR"):
            ratio = float(inr) / floors["INR"]
        else:
            return float(sc["missing"]), "no salary listed"
    if ratio is None:
        return float(sc["unknown_currency"]), ""
    if ratio < 1.0:
        return float(sc["below_floor"]), "pay below floor"
    full = max(1.0001, float(sc["full_marks_ratio"]))
    at = float(sc["at_floor"])
    return min(100.0, at + (100.0 - at) * (ratio - 1.0) / (full - 1.0)), ""


def _age_days(job: Mapping, now: Optional[datetime]) -> Optional[float]:
    age = job.get("posted_age_days")
    if isinstance(age, (int, float)):
        return float(age)
    posted = job.get("posted_at")
    if not posted:
        return None
    try:
        dt = datetime.fromisoformat(str(posted).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, ((now or datetime.now(timezone.utc)) - dt).total_seconds() / 86400.0)


def _freshness_component(job: Mapping, c: dict, now: Optional[datetime]) -> float:
    fc = c["freshness"]
    age = _age_days(job, now)
    if age is None:
        return float(fc["unknown"])
    for limit, pts in fc["steps"]:
        if age <= float(limit):
            return float(pts)
    return float(fc["older"])


Encoder = Callable[[str], Any]


def _default_encoder() -> Optional[Encoder]:
    try:
        from .scoring import get_model
        model = get_model()
    except Exception as e:  # noqa: BLE001 - model missing / offline -> skip the component
        log.info("fit: semantic component skipped (%s)", type(e).__name__)
        return None
    return lambda text: model.encode(text, normalize_embeddings=True)


def _semantic_component(job: Mapping, profile: Profile, c: dict,
                        encoder: Optional[Encoder]) -> Optional[float]:
    if not c.get("semantic") or not profile.resume_text:
        return None
    enc = encoder or _default_encoder()
    if enc is None:
        return None
    text = (job.get("description_text") or "").strip()
    if not text:
        return None
    try:
        import numpy as np
        if profile._resume_vec is None or encoder is not None:
            profile._resume_vec = enc(profile.resume_text)
        a = np.asarray(profile._resume_vec, dtype=float).reshape(-1)
        b = np.asarray(enc(_job_text(job)), dtype=float).reshape(-1)
        cos = float(np.dot(a, b) / (float(np.linalg.norm(a) * np.linalg.norm(b)) or 1.0))
    except Exception as e:  # noqa: BLE001
        log.info("fit: semantic encode failed (%s)", type(e).__name__)
        return None
    lo, hi = (float(x) for x in c["semantic_cosine_range"])
    return max(0.0, min(100.0, 100.0 * (cos - lo) / ((hi - lo) or 1.0)))


# -------------------------------------------------------------------- score

def normalise_weights(weights: Mapping[str, float], present: set[str]) -> dict[str, float]:
    """Weights of the components actually computed, rescaled to sum to 1."""
    w = {k: max(0.0, float(weights.get(k, 0) or 0)) for k in COMPONENTS if k in present}
    total = sum(w.values())
    if total <= 0:
        return {k: 1.0 / len(w) for k in w} if w else {}
    return {k: v / total for k, v in w.items()}


def _why(total: float, c: dict, matched: list[str], missing: list[str], missing_must: list[str],
         notes: list[str]) -> str:
    band = next((label for floor, label in c["why_bands"] if total >= float(floor)), "Match")
    if matched:
        head = f"{band}: {', '.join(matched[:3])}"
    elif missing:
        head = f"{band}: none of the listed skills matched"
    else:
        head = f"{band}: no skills listed"
    bits = [head] + [n for n in notes if n]
    if missing_must:
        bits.append("missing " + ", ".join(missing_must[:2]))
    return " · ".join(bits)


def score(job: Mapping | Any, *, profile: Profile, cfg: Mapping | None = None,
          company: Mapping | None = None, decision: Any = None,
          encoder: Optional[Encoder] = None, now: Optional[datetime] = None) -> FitResult:
    """Fit of one job (store row dict, Job.to_dict(), or a Job) for `profile`.

    company:  companies-table row (tier / funding_stage / yc_batch / yc_top_company);
              falls back to job keys company_tier / tier / funding_stage / yc_batch.
    decision: eligibility.Decision (or dict) — its yoe / fresher / india / salary
              fields win over re-deriving them from the job.
    encoder:  text -> vector, for the semantic component (tests inject a fake;
              None loads MiniLM lazily via scoring.get_model()).
    """
    if not isinstance(job, Mapping):
        job = job.to_dict() if hasattr(job, "to_dict") else dict(vars(job))
    c = _resolve_cfg(cfg)
    flt = _filters(c)
    flags: list[str] = []

    desc = (job.get("description_text") or "").strip()
    no_desc = len(desc) < int(c["skills"].get("min_description_chars", 15) or 0)
    # E10: JD skills come from the description only (a title is not a requirements list).
    jd = SK.extract(desc, company=job.get("company")) if not no_desc else SK.SkillHits()
    comp: dict[str, float] = {}
    comp["skills"], matched, missing, low_conf = _skills_component(jd, profile, c)
    if no_desc:
        flags.append("no_description")
    elif low_conf:
        flags.append("low_confidence_skills")
    comp["role"], family = _role_component(job.get("title") or "", profile, c)
    comp["seniority"], sen_note = _seniority_component(job, decision, profile, c)
    comp["location"], loc_note = _location_component(job, decision, profile, c)
    comp["company"], co_note = _company_component(job, company, c)
    comp["salary"], pay_note = _salary_component(job, decision, flt, c)
    comp["freshness"] = _freshness_component(job, c, now)
    sem = None if no_desc else _semantic_component(job, profile, c, encoder)
    if no_desc and c.get("semantic"):
        # Neutral instead of dropped: dropping would renormalise the other (high) components
        # upward and let an empty JD outrank real ones.
        comp["semantic"] = float(c["skills"].get("no_description_semantic", 50))
    elif sem is None:
        flags.append("semantic_skipped")
    else:
        comp["semantic"] = sem

    weights = normalise_weights(c["weights"], set(comp))
    total = round(sum(comp[k] * weights.get(k, 0.0) for k in comp), 1)
    comp = {k: round(v, 1) for k, v in comp.items()}
    missing_must = [s for unit in SK.units(jd, "must") if not any(x in profile.skills for x in unit)
                    for s in unit[:1]]
    why = _why(total, c, matched, missing, missing_must, [sen_note, loc_note, co_note,
                                                 pay_note if pay_note == "pay below floor" else ""])
    return FitResult(score=total, components=comp, weights={k: round(v, 4) for k, v in weights.items()},
                     matched_skills=matched, missing_skills=missing, why=why, flags=flags,
                     role_family=family)
