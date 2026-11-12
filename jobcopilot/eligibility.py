"""Hard eligibility rules (Phase 2): one Decision per job, every exclusion explained (D8).

    evaluate(job, company=..., cfg=...) -> Decision(lane="main" | "extended" | "excluded", reasons=[...])

Rules come from config/filters.yaml (see the comments there):

  role       title must match a target role family; internships excluded (configurable)
  yoe        a HARD minimum above yoe.max_required excludes; a preferred-only bar is a flag
  seniority  senior+ titles are excluded unless the JD's hard YOE is within max_required
  geo        India: any work type. Outside India: fully remote AND open to someone in India
             (region list, time-zone offsets / text with >= min_ist_overlap_hours of overlap
             with IST, and the location text). Unknown country: kept + flagged.
  salary     disclosed pay below the floor for its currency excludes; no pay = kept, salary_ok None
  company    brand/funded -> main lane; watchlist -> extended lane; excluded tier -> excluded;
             unknown / not in the companies table -> extended lane + flag "company:unverified"
             (board jobs often name employers we have never tiered - nothing good is lost)
  freshness  never excludes; flags stale postings

Every reason is {"rule", "detail", "excerpt"}. Soft flags use rule "flag:<name>".
Pure: no DB, no network. Pass `cfg` (config.filters()) when evaluating many jobs.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

from .models import IN, NON_IN, Compensation, Job

IST_OFFSET = 5.5
LANE_MAIN, LANE_EXTENDED, LANE_EXCLUDED = "main", "extended", "excluded"
EXCERPT_MAX = 160


@dataclass
class Decision:
    lane: str
    reasons: list[dict] = field(default_factory=list)
    yoe_min: Optional[int] = None
    yoe_max: Optional[int] = None
    yoe_is_preferred: bool = False
    yoe_evidence: Optional[str] = None
    fresher_friendly: bool = False
    india_eligible: Optional[bool] = None
    salary_ok: Optional[bool] = None
    salary_annual_inr: Optional[float] = None
    # extras (not part of the cross-workstream contract, but handy downstream)
    yoe_min_years: Optional[float] = None     # exact floor, e.g. 1.5
    role_family: Optional[str] = None
    seniority_high: bool = False
    company_tier: Optional[str] = None

    @property
    def excluded(self) -> bool:
        return self.lane == LANE_EXCLUDED

    @property
    def exclusions(self) -> list[dict]:
        return [r for r in self.reasons if not r["rule"].startswith("flag:")]

    @property
    def flags(self) -> list[dict]:
        return [r for r in self.reasons if r["rule"].startswith("flag:")]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# field access (Job or a store row / plain dict)
# --------------------------------------------------------------------------
def _get(job: Any, name: str, default: Any = None) -> Any:
    if isinstance(job, Mapping):
        v = job.get(name, default)
    else:
        v = getattr(job, name, default)
    return default if v is None else v


def _as_list(v: Any) -> list:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except ValueError:
            return [v]
        return parsed if isinstance(parsed, list) else [parsed]
    return list(v) if isinstance(v, (list, tuple, set)) else [v]


def _compensation(job: Any) -> Optional[Compensation]:
    comp = _get(job, "compensation")
    if isinstance(comp, Compensation):
        return comp
    if isinstance(comp, Mapping):
        return Compensation(**{k: comp.get(k) for k in ("currency", "min_value", "max_value", "interval",
                                                        "summary") if k in comp})
    if isinstance(job, Mapping) and (job.get("comp_min") is not None or job.get("comp_max") is not None):
        return Compensation(currency=job.get("comp_currency"), min_value=job.get("comp_min"),
                            max_value=job.get("comp_max"), interval=job.get("comp_interval"),
                            summary=job.get("comp_summary"))
    return None


def _cut(s: Any) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:EXCERPT_MAX]


def _reason(rule: str, detail: str, excerpt: Any = "") -> dict:
    return {"rule": rule, "detail": detail, "excerpt": _cut(excerpt)}


# --------------------------------------------------------------------------
# geo
# --------------------------------------------------------------------------
_INDIA_OK_RE = re.compile(
    r"\b(?:india|indian|apac|asia|asia[- ]pacific|south\s+asia|worldwide|world\s*wide|anywhere|global(?:ly)?|"
    r"international|all\s+(?:countries|locations|regions)|any\s+(?:country|location|time\s*zone)|"
    r"\bist\b|bengaluru|bangalore|mumbai|delhi|new\s+delhi|ncr|hyderabad|pune|chennai|gurgaon|gurugram|"
    r"noida|kolkata|ahmedabad|kochi|jaipur)\b",
    re.I)
# Words that carry no place: "Remote", "Fully remote", "Work from home", "(Contract)".
_NO_PLACE_RE = re.compile(
    r"\b(?:remote|fully|full|100%|work\s+from\s+home|wfh|distributed|hybrid|onsite|on-site|office|"
    r"first|only|friendly|position|role|job|contract|part[- ]time|full[- ]time|flexible|optional|"
    r"based|location|locations|multiple|various|tbd|n/?a|unknown|other)\b|[\s,;/|()\-–—:+.&]+",
    re.I)
# Explicit "only candidates in X" phrases in the description.
_DESC_RESTRICT_RE = re.compile(
    r"\b(?:(?:us|u\.s\.|usa|uk|eu|europe|canada|north\s+america|americas|latam|emea)[- ]only\b|"
    r"(?:must|should)\s+(?:be\s+)?(?:based|located|living|residing|reside)\s+in\s+(?:the\s+)?"
    r"(?:us\b|u\.s\.|usa|united\s+states|uk\b|united\s+kingdom|eu\b|europe|european\s+union|canada|"
    r"north\s+america|latin\s+america|latam|americas|emea)|"
    r"(?:legally\s+)?authori[sz]ed\s+to\s+work\s+in\s+(?:the\s+)?(?:us\b|u\.s\.|usa|united\s+states|uk\b|"
    r"united\s+kingdom|canada|eu\b|european\s+union)|"
    r"(?:open|available)\s+(?:only\s+)?to\s+(?:candidates|residents|applicants)\s+(?:based\s+)?in\s+(?:the\s+)?"
    r"(?:us\b|u\.s\.|usa|united\s+states|uk\b|united\s+kingdom|eu\b|europe|canada|americas|latam))",
    re.I)

# Named zones (standard time; DST shifts by 1h and is within tolerance of the overlap test).
_ZONE_OFFSETS = {
    "EST": -5, "EDT": -4, "ET": -5, "CST": -6, "CDT": -5, "CT": -6, "MST": -7, "MDT": -6, "MT": -7,
    "PST": -8, "PDT": -7, "PT": -8, "AKST": -9, "HST": -10, "GMT": 0, "UTC": 0, "WET": 0, "BST": 1,
    "CET": 1, "CEST": 2, "EET": 2, "EEST": 3, "MSK": 3, "GST": 4, "IST": 5.5, "PKT": 5, "ICT": 7,
    "WIB": 7, "SGT": 8, "HKT": 8, "AWST": 8, "JST": 9, "KST": 9, "AEST": 10, "AEDT": 11, "NZST": 12,
}
_ZONE_RE = re.compile(r"\b(" + "|".join(sorted(_ZONE_OFFSETS, key=len, reverse=True)) + r")\b")
_ZONE_WORDS = {
    "eastern": -5, "central": -6, "mountain": -7, "pacific": -8, "us": -6, "u.s.": -6, "american": -6,
    "north american": -6, "americas": -6, "european": 1, "europe": 1, "uk": 0, "india": 5.5,
}
_ZONE_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_ZONE_WORDS, key=len, reverse=True)) +
    r")\s+(?:standard\s+)?(?:time\s*zones?|timezones?|time|hours|business\s+hours|working\s+hours)\b", re.I)
_TOLERANCE_RE = re.compile(r"(?:\+\s*/\s*-|±|plus\s+or\s+minus|\+-)\s*(\d{1,2})\s*(?:h|hours?|hrs?)", re.I)


def ist_overlap_hours(offset: float, workday_hours: float = 8.0) -> float:
    """Hours a 9-to-5-style local workday at `offset` overlaps an IST workday of the same length."""
    return max(0.0, workday_hours - abs(IST_OFFSET - float(offset)))


def _tz_text_offsets(text: str) -> tuple[list[float], float]:
    from .regions import parse_utc_offsets
    offs = list(parse_utc_offsets(text))
    offs += [float(_ZONE_OFFSETS[z]) for z in _ZONE_RE.findall(text)]
    offs += [float(_ZONE_WORDS[w.lower()]) for w in _ZONE_WORD_RE.findall(text)]
    tol = max((float(t) for t in _TOLERANCE_RE.findall(text)), default=0.0)
    return sorted(set(offs)), tol


def _best_overlap(offsets: list[float], tol: float, workday: float) -> float:
    best = 0.0
    for off in offsets:
        # Any accepted offset in [off - tol, off + tol]; the closest to IST counts.
        closest = min(max(IST_OFFSET, off - tol), off + tol)
        best = max(best, ist_overlap_hours(closest, workday))
    return best


def _place_left(text: str) -> str:
    """What remains of a location string once the no-place words are removed."""
    return _NO_PLACE_RE.sub(" ", text or "").strip()


def _geo(job: Any, gcfg: Mapping[str, Any], reasons: list[dict]) -> Optional[bool]:
    bucket = _get(job, "country_bucket", "unknown")
    wtype = (_get(job, "workplace_type", "unknown") or "unknown").lower()
    location = _get(job, "location_text", "") or ""
    regions = [str(r) for r in _as_list(_get(job, "region_restrictions")) if str(r).strip()]
    offsets = [float(o) for o in _as_list(_get(job, "timezone_restrictions"))
               if isinstance(o, (int, float)) or re.fullmatch(r"[+\-]?\d+(?:\.\d+)?", str(o))]
    tz_text = _get(job, "tz_requirement_text", "") or ""
    min_overlap = float(gcfg.get("min_ist_overlap_hours", 3) or 0)
    workday = float(gcfg.get("workday_hours", 8) or 8)

    if bucket == IN:
        allow = [str(a).lower() for a in gcfg.get("india_allow") or []]
        if allow and wtype not in allow:
            reasons.append(_reason("geo", f"India role with work type '{wtype}' not in india_allow", location))
            return True
        return True

    if bucket == NON_IN:
        allow = [str(a).lower() for a in gcfg.get("outside_india_allow") or ["remote"]]
        if wtype not in allow:
            reasons.append(_reason("geo", f"outside India and not remote ({wtype})", location))
            return False
    elif not gcfg.get("keep_unknown_country", True):
        reasons.append(_reason("geo", "country unknown (keep_unknown_country is off)", location))
        return None

    if bucket == NON_IN and not gcfg.get("outside_india_require_india_eligible", True):
        return None

    # --- who may apply: region list -> location text -> description phrases ---
    eligible: Optional[bool] = None
    if regions:
        ok = [r for r in regions if _INDIA_OK_RE.search(r)]
        if ok:
            eligible = True
        else:
            reasons.append(_reason("geo", "remote, but restricted to: " + ", ".join(regions)[:100],
                                   ", ".join(regions)))
            return False
    if eligible is None and location:
        from .regions import partition_location
        loc_regions, loc_tz = partition_location(location)
        if loc_tz:
            tz_text = "; ".join(t for t in (tz_text, loc_tz) if t)
        places = " ".join(loc_regions)
        if _INDIA_OK_RE.search(location):
            eligible = True
        elif bucket == NON_IN and _place_left(places):
            reasons.append(_reason("geo", "remote, but the location names a place outside India", location))
            return False
    if eligible is None and gcfg.get("scan_description", True):
        desc = _get(job, "description_text", "") or ""
        m = _DESC_RESTRICT_RE.search(desc)
        if m and not re.search(r"\b(?:worldwide|anywhere|global|india|apac)\b",
                               desc[max(0, m.start() - 200):m.end() + 200], re.I):
            reasons.append(_reason("geo", "remote, but the description restricts the location",
                                   desc[max(0, m.start() - 60):m.end() + 60]))
            return False

    # --- time zones ---
    if min_overlap > 0 and (offsets or tz_text):
        tol = 0.0
        all_offs = list(offsets)
        if tz_text:
            t_offs, tol = _tz_text_offsets(tz_text)
            all_offs += t_offs
        if all_offs:
            best = _best_overlap(sorted(set(all_offs)), tol, workday)
            if best < min_overlap:
                shown = tz_text or ", ".join(f"UTC{o:+g}" for o in sorted(set(all_offs)))
                reasons.append(_reason("geo", f"time zones overlap IST by {best:g}h "
                                              f"(< {min_overlap:g}h required)", shown))
                return False
            eligible = True if eligible is None else eligible

    if bucket != NON_IN:
        reasons.append(_reason("flag:country_unknown", "country unknown; kept (keep_unknown_country)", location))
        return eligible
    return True if eligible is None else eligible


# --------------------------------------------------------------------------
# salary
# --------------------------------------------------------------------------
def _salary(job: Any, cfg: Mapping[str, Any], reasons: list[dict]) -> tuple[Optional[bool], Optional[float]]:
    comp = _compensation(job)
    if comp is None or (comp.min_value is None and comp.max_value is None):
        return None, None
    cur = (comp.currency or "INR").upper()
    # Annualise in the posting's own currency (same rules as Compensation.annual_inr).
    native = Compensation(currency="INR", min_value=comp.min_value, max_value=comp.max_value,
                          interval=comp.interval).annual_inr()
    if native is None:
        return None, None
    fx = {str(k).upper(): float(v) for k, v in (cfg.get("fx_to_inr") or {}).items()}
    fx["INR"] = 1.0
    floors = {str(k).upper(): float(v) for k, v in (cfg.get("salary_floors") or {}).items()}
    inr = native * fx[cur] if cur in fx else None
    shown = comp.summary or f"{cur} {comp.min_value or ''}-{comp.max_value or ''} / {comp.interval or 'year'}"
    # Implausible numbers ("15" meaning 15 LPA) are a parsing problem, not a low salary.
    if (inr is not None and inr < 50_000) or (inr is None and native < 1_000):
        reasons.append(_reason("flag:salary_implausible", f"disclosed pay looks unparsed ({native:g} {cur}/yr)",
                               shown))
        return None, inr
    if cur in floors:
        floor, val = floors[cur], native
    elif inr is not None and "INR" in floors:
        floor, val, cur = floors["INR"], inr, "INR"
    else:
        reasons.append(_reason("flag:salary_unknown_currency", f"no floor or fx rate for {cur}", shown))
        return None, inr
    if val < floor:
        reasons.append(_reason("salary", f"pay {val:,.0f} {cur}/yr below floor {floor:,.0f} {cur}", shown))
        return False, inr
    return True, inr


# --------------------------------------------------------------------------
# company
# --------------------------------------------------------------------------
def _company(job: Any, company: Optional[Mapping[str, Any]], cfg: Mapping[str, Any],
             reasons: list[dict]) -> tuple[str, Optional[str]]:
    ccfg = cfg.get("companies") or {}
    tier = (company or {}).get("tier") if company else None
    tier = tier or _get(job, "company_tier") or _get(job, "tier")
    tier = str(tier).lower() if tier else None
    name = (company or {}).get("name") or _get(job, "company", "")
    why = ""
    if company and company.get("tier_reasons"):
        tr = company["tier_reasons"]
        tr = _as_list(tr)
        why = "; ".join(str(x) for x in tr[:2])
    main_tiers = [str(t).lower() for t in ccfg.get("require_tier") or ["brand", "funded"]]
    wl = ccfg.get("watchlist") or {}
    extended_on = bool(ccfg.get("include_extended", True))
    if tier in main_tiers:
        return LANE_MAIN, tier
    if tier == "watchlist":
        if extended_on and wl.get("enabled", True):
            return LANE_EXTENDED, tier
        reasons.append(_reason("company", "watchlist company; extended lane is switched off", name))
        return LANE_EXCLUDED, tier
    if tier == "excluded":
        reasons.append(_reason("company", "excluded tier (body shop / staffing / denylisted)",
                               f"{name}: {why}" if why else name))
        return LANE_EXCLUDED, tier
    # unknown tier, or employer not in the companies table
    lane_cfg = str(ccfg.get("unknown_tier_lane", LANE_EXTENDED)).lower()
    detail = "unknown company tier" if tier else "employer not in the companies table"
    if lane_cfg == LANE_EXTENDED and extended_on:
        reasons.append(_reason("flag:company:unverified", detail + "; extended lane", name))
        return LANE_EXTENDED, tier or "unknown"
    reasons.append(_reason("company", detail, name))
    return LANE_EXCLUDED, tier or "unknown"


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------
def _role_families(cfg: Mapping[str, Any]) -> list[str]:
    from .filters import DEFAULT_TARGET_ROLES
    fams = (cfg.get("roles") or {}).get("families") or []
    return [str(f) for f in fams] or list(DEFAULT_TARGET_ROLES)


_INTERN_RE = re.compile(r"\bintern(?:ship)?s?\b|\bco-?op\b|\bapprentice(?:ship)?\b", re.I)


def evaluate(job: Job | Mapping[str, Any], *, company: Optional[Mapping[str, Any]] = None,
             cfg: Optional[Mapping[str, Any]] = None) -> Decision:
    """Apply every hard rule to one job. Never raises on missing fields."""
    from .filters import is_seniority_high, passes_role
    from .yoe import extract_yoe

    if cfg is None:
        from . import config
        cfg = config.filters()
    reasons: list[dict] = []
    title = _get(job, "title", "") or ""

    # role
    rcfg = cfg.get("roles") or {}
    family = passes_role(title, _role_families(cfg))
    if family is None:
        reasons.append(_reason("role", "title does not match a target role family", title))
    elif rcfg.get("exclude_internships", True) and _INTERN_RE.search(title):
        reasons.append(_reason("role", "internship / co-op title (roles.exclude_internships)", title))

    # yoe + seniority
    ycfg = cfg.get("yoe") or {}
    max_req = float(ycfg.get("max_required", 2))
    y = extract_yoe(_get(job, "description_text", "") or "", title)
    hard_min = None if (y.min_years is None or y.is_preferred) else y.min_years
    if hard_min is not None and hard_min > max_req:
        reasons.append(_reason("yoe", f"requires {hard_min:g}+ years (max_required {max_req:g})", y.evidence))
    elif y.is_preferred and y.min_years is not None and y.min_years > max_req:
        reasons.append(_reason("flag:yoe_preferred", f"prefers {y.min_years:g}+ years (soft bar, kept)",
                               y.evidence))
    if y.ambiguous:
        reasons.append(_reason("flag:yoe_ambiguous", "several different YOE bars in the JD", y.evidence))

    senior = is_seniority_high(title)
    scfg = cfg.get("seniority") or {}
    if senior and scfg.get("exclude_senior_titles", True):
        if hard_min is not None and hard_min <= max_req and scfg.get("allow_if_jd_yoe_within_max", True):
            reasons.append(_reason("flag:seniority", f"senior title but the JD asks for {hard_min:g}+ years",
                                   title))
        else:
            reasons.append(_reason("seniority", "senior+ title" + (
                "" if hard_min is None else f" asking {hard_min:g}+ years"), title))

    # geo / salary / company / freshness
    india_ok = _geo(job, cfg.get("geo") or {}, reasons)
    salary_ok, salary_inr = _salary(job, cfg, reasons)
    lane, tier = _company(job, company, cfg, reasons)
    age = _get(job, "posted_age_days")
    stale_days = (cfg.get("freshness") or {}).get("stale_flag_days", 60)
    if isinstance(age, (int, float)) and stale_days is not None and age > float(stale_days):
        reasons.append(_reason("flag:stale", f"posted {int(age)} days ago (> {stale_days})",
                               _get(job, "posted_at", "")))

    if any(not r["rule"].startswith("flag:") for r in reasons):
        lane = LANE_EXCLUDED
    return Decision(
        lane=lane, reasons=reasons, yoe_min=y.yoe_min, yoe_max=y.yoe_max,
        yoe_is_preferred=y.is_preferred, yoe_evidence=y.evidence, fresher_friendly=y.fresher_friendly,
        india_eligible=india_ok, salary_ok=salary_ok, salary_annual_inr=salary_inr,
        yoe_min_years=y.min_years, role_family=family, seniority_high=senior, company_tier=tier)
