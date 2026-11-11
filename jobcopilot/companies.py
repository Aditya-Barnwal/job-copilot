"""Company records + tier signals (plan Phase 1, `companies` table).

One row per employer, whoever found it (config, yc-oss, Bangalore Startup Map,
the funding watcher, the ATS detector). Rows are matched by website domain
first, then by normalised name, so "Razorpay Software Pvt Ltd" from one source
and "razorpay.com" from another become one record.

Every tier comes with human-readable reasons naming their source
("yc-oss: W23", "bsm: Series B", "config: brand") so a tier filter can always
explain an exclusion (D8). A tier set by hand in config is sticky: it is stored
as the reason "config: <tier>" and no computed signal overrides it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

from . import config, store
from .normalize import domain_of, normalize_company_name

log = logging.getLogger("jobcopilot.companies")

TIERS = ("brand", "funded", "unknown", "excluded")
VERIFIED_CONFIDENCE = 0.9      # ats_confidence at/above which an ATS counts as verified
UNVERIFIED_CONFIDENCE = 0.5    # a guess (config `verify: true`, a BSM role-id hint)

_JSON_LIST_FIELDS = ("signal_sources", "tier_reasons", "investors")
_FIELDS = frozenset({
    "domain", "website", "careers_url", "ats", "ats_slug", "ats_tenant", "ats_wd_server",
    "ats_site", "ats_region", "ats_confidence", "ats_evidence", "ats_checked_at",
    "tier", "tier_reasons", "funding_stage", "investors", "yc_batch", "yc_top_company",
    "team_size", "sector", "hq_location", "signal_sources", "apply_channel", "enabled",
    "notes",
})
_ATS_FIELDS = ("ats", "ats_slug", "ats_tenant", "ats_wd_server", "ats_site", "ats_region",
               "ats_confidence", "ats_evidence", "ats_checked_at")
_FILL_ONLY = ("domain", "website")          # first source wins; config can set them first
# Descriptive fields a person may have curated: discovery only fills them when
# empty, config always overwrites (sector also drives the tech/fintech filter).
_SOFT = ("sector", "notes", "hq_location")
# "config: brand" or "config: brand (public company, USDC issuer)"
_CONFIG_TIER_RE = re.compile(r"^config: (brand|funded|unknown|excluded)(?: \(.*\))?$")

# Hosts many unrelated companies share as their "website" — never match on them.
_GENERIC_DOMAINS = frozenset({
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "github.com",
    "medium.com", "notion.site", "wixsite.com", "angel.co", "wellfound.com",
    "crunchbase.com", "ycombinator.com", "bit.ly", "linktr.ee", "sites.google.com",
    "youtube.com", "substack.com", "carrd.co", "webflow.io", "framer.website",
})
# Trailing words dropped for a looser brand / body-shop name match ("Google India").
_CORE_SUFFIXES = frozenset({"india", "technologies", "technology", "software", "internet", "online"})

# Funding stages -> ordinal; "public" is handled as brand-ish (see compute_tier).
_STAGE_LEVELS = {
    "pre seed": 0, "preseed": 0, "angel": 0, "seed": 1, "pre series a": 1,
    "series a": 2, "series b": 3, "series c": 4, "series c+": 4, "series d": 5,
    "series e": 6, "series f": 7, "growth": 4, "late": 5,
}
_SEASONS = {"winter": "W", "summer": "S", "spring": "X", "fall": "F", "autumn": "F"}

BRANDS_FILE = "brands.yaml"


# ----------------------------------------------------------------- helpers

def _as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _merge_lists(old: Iterable, new: Iterable) -> list:
    out: list = []
    for v in list(old) + list(new):
        if v not in out and v not in (None, ""):
            out.append(v)
    return out


def _decode(row: Any) -> dict:
    d = dict(row)
    for f in _JSON_LIST_FIELDS:
        d[f] = _as_list(d.get(f))
    d["yc_top_company"] = bool(d.get("yc_top_company"))
    d["enabled"] = bool(d.get("enabled")) if d.get("enabled") is not None else True
    return d


def usable_domain(url_or_domain: Optional[str]) -> str:
    """domain_of(), but '' for shared hosts (linkedin.com/company/x is not a domain)."""
    d = domain_of(url_or_domain)
    return "" if (not d or d in _GENERIC_DOMAINS or "." not in d) else d


def stage_level(stage: Optional[str]) -> Optional[int]:
    """Ordinal of a funding stage ('Series B' -> 3, 'series_a' -> 2); None if not a round."""
    if not stage:
        return None
    s = re.sub(r"[_\-]+", " ", str(stage).strip().lower())
    s = re.sub(r"\s+", " ", s)
    return _STAGE_LEVELS.get(s)


def short_yc_batch(batch: Optional[str]) -> Optional[str]:
    """'Winter 2023' -> 'W23', 'Spring 2025' -> 'X25'; already-short or odd values pass through."""
    if not batch:
        return None
    m = re.fullmatch(r"\s*([A-Za-z]+)\s+(\d{4})\s*", str(batch))
    if m and m.group(1).lower() in _SEASONS:
        return f"{_SEASONS[m.group(1).lower()]}{m.group(2)[2:]}"
    b = str(batch).strip()
    return None if b.lower() in ("", "unspecified", "none") else b


def _core(norm: str) -> str:
    words = norm.split()
    while len(words) > 1 and words[-1] in _CORE_SUFFIXES:
        words.pop()
    return " ".join(words)


# ------------------------------------------------------------------- rules

def load_rules(brands_path: Optional[Path] = None) -> dict:
    """Tier rules = filters.yaml `companies` + config/brands.yaml, ready for compute_tier."""
    rules = dict(config.filters().get("companies") or {})
    path = brands_path or (config.config_dir() / BRANDS_FILE)
    brands: dict = {}
    if path.exists():
        brands = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        log.warning("%s missing: no brand / body-shop signals", path)
    shops = brands.get("body_shops") or {}
    rules.update({
        "brands": brands.get("brands") or [],
        "brand_domains": brands.get("brand_domains") or [],
        "body_shop_names": shops.get("names") or [],
        "body_shop_patterns": shops.get("patterns") or [],
        "body_shop_allow": shops.get("allow") or [],
    })
    return prepare_rules(rules)


def prepare_rules(rules: Mapping[str, Any]) -> dict:
    """Normalise raw rule lists once (idempotent), so compute_tier stays cheap per row."""
    if rules.get("_prepared"):
        return dict(rules)
    r = dict(rules)
    r["require_tier"] = list(r.get("require_tier") or ["brand", "funded"])
    r["funded_min_stage"] = r.get("funded_min_stage") or "series_a"
    r["yc_counts_as_funded"] = bool(r.get("yc_counts_as_funded", True))
    r["brands"] = {normalize_company_name(b) for b in r.get("brands") or [] if b}
    r["brand_domains"] = {domain_of(d) for d in r.get("brand_domains") or [] if d}
    r["body_shop_names"] = {normalize_company_name(b) for b in r.get("body_shop_names") or [] if b}
    r["body_shop_allow"] = {normalize_company_name(b) for b in r.get("body_shop_allow") or [] if b}
    r["body_shop_patterns"] = [
        (p, re.compile(r"\b" + r"\s+".join(map(re.escape, normalize_company_name(p).split())) + r"\b"))
        for p in r.get("body_shop_patterns") or [] if p and normalize_company_name(p)
    ]
    r["_prepared"] = True
    return r


def _domain_in(domain: str, domains: set[str]) -> bool:
    return bool(domain) and any(domain == d or domain.endswith("." + d) for d in domains)


def compute_tier(company: Mapping[str, Any], rules: Mapping[str, Any]) -> tuple[str, list[str]]:
    """(tier, reasons) for one company dict. Pure: no I/O.

    Order: a config tier (sticky) > body-shop name > brand > funded > body-shop
    keyword > unknown. Keywords only exclude when nothing positive is known, so a
    funded product startup with "staffing" in its name is not thrown out.
    """
    r = prepare_rules(rules)
    name = company.get("name") or ""
    norm = company.get("normalized_name") or normalize_company_name(name)
    core = _core(norm)
    domain = usable_domain(company.get("domain") or company.get("website"))
    reasons_in = [str(x) for x in _as_list(company.get("tier_reasons"))]
    sources = [str(x) for x in _as_list(company.get("signal_sources"))]

    for reason in reasons_in:
        m = _CONFIG_TIER_RE.match(reason)
        if m:
            return m.group(1), [reason]

    allowed = norm in r["body_shop_allow"] or core in r["body_shop_allow"]
    if not allowed and (norm in r["body_shop_names"] or core in r["body_shop_names"]):
        return "excluded", [f"brands.yaml: IT-services/staffing firm '{name}'"]

    if norm in r["brands"] or core in r["brands"]:
        return "brand", [f"brands.yaml: brand '{name}'"]
    if _domain_in(domain, r["brand_domains"]):
        return "brand", [f"brands.yaml: brand domain {domain}"]

    stage = company.get("funding_stage")
    stage_src = "bsm" if "bsm" in sources else ("funding-rss" if "funding-rss" in sources else "stage")
    if stage and str(stage).strip().lower() == "public":
        return "brand", [f"{stage_src}: Public"]

    funded: list[str] = []
    yc = company.get("yc_batch")
    if yc and r["yc_counts_as_funded"]:
        top = " (top company)" if company.get("yc_top_company") else ""
        funded.append(f"yc-oss: {yc}{top}")
    lvl, need = stage_level(stage), stage_level(r["funded_min_stage"])
    if lvl is not None and need is not None and lvl >= need:
        funded.append(f"{stage_src}: {stage}")
    if "funding-rss" in sources and not any(x.startswith("funding-rss:") for x in funded):
        funded.append("funding-rss: qualified by funding watcher"
                      + (f" ({stage})" if stage else ""))
    if funded:
        return "funded", funded

    if not allowed:
        for raw, pat in r["body_shop_patterns"]:
            if pat.search(norm):
                return "excluded", [f"brands.yaml: staffing/services keyword '{raw}' in '{name}'"]

    why = [f"{stage_src}: {stage} (below {r['funded_min_stage']})"] if stage else []
    return "unknown", why or ["no brand, YC or funding signal"]


# --------------------------------------------------------------------- DB

def _find(conn, norm: str, domain: str) -> Optional[Any]:
    if domain:
        row = conn.execute("SELECT * FROM companies WHERE domain = ? ORDER BY id LIMIT 1",
                           (domain,)).fetchone()
        if row:
            return row
    return conn.execute("SELECT * FROM companies WHERE normalized_name = ?", (norm,)).fetchone()


def upsert_company(name: str, *, db_path: Any = None, **fields: Any) -> int:
    """Insert or merge one company; returns its id.

    Extra conveniences: `signal_source="yc-oss"` appends one source;
    `verified=True/False` sets ats_confidence when it isn't given. Lists
    (signal_sources, tier_reasons, investors) are merged, never replaced.
    Scalars from the caller win, except: website/domain are fill-only, ATS
    fields are not overwritten by a lower-confidence guess, and a tier set by
    config is never replaced by a non-config caller.
    """
    if not name or not str(name).strip():
        raise ValueError("company name required")
    name = re.sub(r"\s+", " ", str(name)).strip()
    src = fields.pop("signal_source", None)
    verified = fields.pop("verified", None)
    unknown = set(fields) - _FIELDS
    if unknown:
        raise TypeError(f"upsert_company: unknown field(s) {sorted(unknown)}")
    if src:
        fields["signal_sources"] = _merge_lists(_as_list(fields.get("signal_sources")), [src])
    if verified is not None and fields.get("ats") and fields.get("ats_confidence") is None:
        fields["ats_confidence"] = 1.0 if verified else UNVERIFIED_CONFIDENCE
    if "tier" in fields and fields["tier"] not in TIERS and fields["tier"] is not None:
        raise ValueError(f"tier must be one of {TIERS}")
    for b in ("yc_top_company", "enabled"):
        if fields.get(b) is not None:
            fields[b] = int(bool(fields[b]))
    domain = usable_domain(fields.get("domain") or fields.get("website"))
    fields["domain"] = domain or None
    norm = normalize_company_name(name)
    now = time.time()

    with store.connect(db_path) as conn:
        row = _find(conn, norm, domain)
        if row is None:
            vals = {k: v for k, v in fields.items() if v is not None}
            for f in _JSON_LIST_FIELDS:
                if f in vals:
                    vals[f] = json.dumps(_as_list(vals[f]), ensure_ascii=False)
            vals.update(name=name, normalized_name=norm, first_seen=now, last_seen=now, updated_at=now)
            cols = ", ".join(vals)
            cur = conn.execute(f"INSERT INTO companies ({cols}) VALUES ({', '.join('?' * len(vals))})",
                               tuple(vals.values()))
            return int(cur.lastrowid)

        old = _decode(row)
        upd: dict[str, Any] = {}
        sticky = any(_CONFIG_TIER_RE.match(str(x)) for x in old["tier_reasons"])
        from_config = "config" in _as_list(fields.get("signal_sources"))
        new_conf = fields.get("ats_confidence")
        keep_ats = bool(old.get("ats")) and fields.get("ats") is not None and (
            (new_conf if new_conf is not None else 0.0) < (old.get("ats_confidence") or 0.0))
        for k, v in fields.items():
            if v is None:
                continue
            if k in _JSON_LIST_FIELDS:
                base = old[k]
                if k == "tier_reasons" and from_config:   # config tier edited -> replace marker
                    base = [x for x in base if not _CONFIG_TIER_RE.match(str(x))]
                merged = _merge_lists(base, _as_list(v))
                if merged != old[k]:
                    upd[k] = json.dumps(merged, ensure_ascii=False)
            elif k in _FILL_ONLY:
                if not old.get(k):
                    if k == "domain" and conn.execute(
                            "SELECT 1 FROM companies WHERE domain = ? AND id != ?",
                            (v, old["id"])).fetchone():
                        continue
                    upd[k] = v
                elif k == "domain" and v != old[k]:
                    log.debug("company %r: keeping domain %s (got %s)", old["name"], old[k], v)
            elif k in _SOFT and old.get(k) and not from_config:
                continue
            elif k in _ATS_FIELDS and keep_ats:
                continue
            elif k == "tier" and sticky and not from_config:
                continue
            elif v != old.get(k):
                upd[k] = v
        upd["last_seen"] = upd["updated_at"] = now
        sets = ", ".join(f"{k} = ?" for k in upd)
        conn.execute(f"UPDATE companies SET {sets} WHERE id = ?", (*upd.values(), old["id"]))
        return int(old["id"])


def get_company(name_or_domain: str, db_path: Any = None) -> Optional[dict]:
    """Look up by domain (if it looks like one) or by normalised name; None if absent."""
    if not name_or_domain:
        return None
    q = name_or_domain.strip()
    dom = usable_domain(q) if (" " not in q and "." in q) else ""
    with store.connect(db_path) as conn:
        row = None
        if dom:
            row = conn.execute("SELECT * FROM companies WHERE domain = ? ORDER BY id LIMIT 1",
                               (dom,)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM companies WHERE normalized_name = ?",
                               (normalize_company_name(q),)).fetchone()
        return _decode(row) if row else None


def list_companies(*, tier: str | Iterable[str] | None = None, enabled: Optional[bool] = None,
                   db_path: Any = None) -> list[dict]:
    where, args = [], []
    if tier is not None:
        tiers = [tier] if isinstance(tier, str) else list(tier)
        where.append(f"tier IN ({', '.join('?' * len(tiers))})")
        args.extend(tiers)
    if enabled is not None:
        where.append("COALESCE(enabled, 1) = ?")
        args.append(int(enabled))
    sql = "SELECT * FROM companies" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY name"
    with store.connect(db_path) as conn:
        return [_decode(r) for r in conn.execute(sql, args).fetchall()]


def recompute_tiers(db_path: Any = None, rules: Optional[Mapping[str, Any]] = None) -> dict[str, int]:
    """Recompute every company's tier + reasons; returns counts per tier."""
    r = prepare_rules(rules) if rules is not None else load_rules()
    counts = {t: 0 for t in TIERS}
    with store.connect(db_path) as conn:
        rows = [_decode(x) for x in conn.execute("SELECT * FROM companies").fetchall()]
        for c in rows:
            tier, reasons = compute_tier(c, r)
            counts[tier] = counts.get(tier, 0) + 1
            if tier != c.get("tier") or reasons != c["tier_reasons"]:
                conn.execute("UPDATE companies SET tier = ?, tier_reasons = ?, updated_at = ? WHERE id = ?",
                             (tier, json.dumps(reasons, ensure_ascii=False), time.time(), c["id"]))
    log.info("tiers recomputed: %s", counts)
    return counts


def _ingest_connectors() -> set[str]:
    """ATS names we can actually pull jobs from (search-style connectors excluded)."""
    from .connectors import REGISTRY
    return set(REGISTRY) - {"adzuna", "jsearch", "manual"}


def enabled_ats_companies(db_path: Any = None, rules: Optional[Mapping[str, Any]] = None) -> list[dict]:
    """Companies ready for live ingest, as config/companies.yaml-style rows.

    Requires: enabled, a supported ATS verified to VERIFIED_CONFIDENCE, and a
    tier in rules.require_tier. `company_id` is included for linking jobs.
    """
    req = (prepare_rules(rules) if rules is not None
           else prepare_rules(config.filters().get("companies") or {}))["require_tier"]
    supported = _ingest_connectors()
    out: list[dict] = []
    for c in list_companies(tier=req, enabled=True, db_path=db_path):
        ats = (c.get("ats") or "").lower()
        if ats not in supported or (c.get("ats_confidence") or 0.0) < VERIFIED_CONFIDENCE:
            continue
        row: dict[str, Any] = {"name": c["name"], "connector": ats, "sector": c.get("sector") or "tech",
                               "tier": c.get("tier"), "company_id": c["id"]}
        if ats == "workday":
            if not (c.get("ats_tenant") and c.get("ats_site")):
                continue
            row.update(tenant=c["ats_tenant"], wd_server=c.get("ats_wd_server") or "wd1", site=c["ats_site"])
        else:
            if not c.get("ats_slug"):
                continue
            row["slug"] = c["ats_slug"]
            if c.get("ats_region"):
                row["region"] = c["ats_region"]
        out.append(row)
    return out


_SEARCH_CONNECTORS = {"adzuna", "jsearch"}


def _config_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    connector = str(row.get("connector") or "manual").lower()
    f: dict[str, Any] = {"signal_source": "config", "sector": row.get("sector"),
                         "website": row.get("website") or row.get("domain"),
                         "careers_url": row.get("careers_url"), "notes": row.get("note")}
    if row.get("enabled") is not None:
        f["enabled"] = bool(row["enabled"])
    f["ats"] = connector
    if connector == "manual":   # a human decision, so a discovery guess must not replace it
        f.update(ats_confidence=1.0, ats_evidence="config")
    else:
        f.update(ats_slug=row.get("slug"), ats_tenant=row.get("tenant"),
                 ats_wd_server=row.get("wd_server"), ats_site=row.get("site"),
                 ats_region=row.get("region"),
                 ats_confidence=UNVERIFIED_CONFIDENCE if row.get("verify") else 1.0,
                 ats_evidence="config (verify: true — guess)" if row.get("verify") else "config")
    f["apply_channel"] = row.get("apply_channel") or (
        "email" if row.get("apply_method") == "email" else "manual" if connector == "manual" else "ats")
    if row.get("yc_batch"):
        f["yc_batch"] = short_yc_batch(str(row["yc_batch"]))
    tier = row.get("tier") or ("funded" if row.get("funded_startup") else None)
    if tier:
        if tier not in TIERS:
            raise ValueError(f"config company {row.get('name')!r}: tier must be one of {TIERS}")
        f["tier"] = tier
        why = str(row.get("tier_reason") or "").strip().replace("(", "[").replace(")", "]")
        f["tier_reasons"] = [f"config: {tier}" + (f" ({why})" if why else "")]
    return f


def sync_config_companies(paths: list[Path], db_path: Any = None) -> int:
    """Load config/companies.yaml + config/target_companies.yaml rows (missing files skipped)."""
    n = 0
    for p in paths:
        p = Path(p)
        if not p.exists():
            log.info("sync_config_companies: %s not found, skipped", p)
            continue
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for row in data.get("companies") or []:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            if str(row.get("connector") or "").lower() in _SEARCH_CONNECTORS:
                continue  # a search query, not an employer
            f = _config_fields(row)
            cid = upsert_company(row["name"], db_path=db_path, **f)
            if not f.get("tier"):
                _drop_config_tier(cid, db_path)  # tier removed from config -> no longer sticky
            n += 1
    if n:
        recompute_tiers(db_path)
    return n


def _drop_config_tier(company_id: int, db_path: Any) -> None:
    with store.connect(db_path) as conn:
        row = conn.execute("SELECT tier_reasons FROM companies WHERE id = ?", (company_id,)).fetchone()
        reasons = _as_list(row["tier_reasons"]) if row else []
        kept = [x for x in reasons if not _CONFIG_TIER_RE.match(str(x))]
        if kept != reasons:
            conn.execute("UPDATE companies SET tier_reasons = ? WHERE id = ?",
                         (json.dumps(kept, ensure_ascii=False), company_id))


def default_config_paths() -> list[Path]:
    return [config.companies_path(), config.config_dir() / "target_companies.yaml"]


# ------------------------------------------------------- source_state store

def get_source_state(source: str, db_path: Any = None) -> dict:
    """source_state row as a dict (state JSON decoded); {} if never run."""
    with store.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM source_state WHERE source = ?", (source,)).fetchone()
    if not row:
        return {}
    d = dict(row)
    try:
        d["state"] = json.loads(d.get("state") or "{}")
    except ValueError:
        d["state"] = {}
    return d


def put_source_state(source: str, db_path: Any = None, **values: Any) -> None:
    """Upsert source_state columns; `state` may be a dict (stored as JSON)."""
    allowed = {"etag", "last_modified", "content_hash", "last_run", "last_ok", "last_error", "items", "state"}
    bad = set(values) - allowed
    if bad:
        raise TypeError(f"put_source_state: unknown column(s) {sorted(bad)}")
    if isinstance(values.get("state"), (dict, list)):
        values["state"] = json.dumps(values["state"], ensure_ascii=False, sort_keys=True)
    with store.connect(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO source_state (source) VALUES (?)", (source,))
        if values:
            sets = ", ".join(f"{k} = ?" for k in values)
            conn.execute(f"UPDATE source_state SET {sets} WHERE source = ?", (*values.values(), source))


def conditional_headers(state: Mapping[str, Any]) -> dict[str, str]:
    """If-None-Match / If-Modified-Since from the last run, so an unchanged
    source answers 304 even in a fresh process (PoliteClient's ETag cache is
    per process)."""
    h: dict[str, str] = {}
    if state.get("etag"):
        h["If-None-Match"] = str(state["etag"])
    if state.get("last_modified"):
        h["If-Modified-Since"] = str(state["last_modified"])
    return h


def content_hash(data: Any) -> str:
    """sha256 of bytes / str / JSON-able data (sorted keys) for change detection."""
    if not isinstance(data, (bytes, str)):
        data = json.dumps(data, sort_keys=True, ensure_ascii=False)
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()
