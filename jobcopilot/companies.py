"""Company records + tier signals (plan Phase 1, `companies` table).

One row per employer, whoever found it (config, yc-oss, Bangalore Startup Map,
the funding watcher, the ATS detector). Rows are matched by website domain
first, then by normalised name, so "Razorpay Software Pvt Ltd" from one source
and "razorpay.com" from another become one record.

Every tier comes with human-readable reasons naming their source
("yc-oss: W23", "bsm: Series B", "config: brand") so a tier filter can always
explain an exclusion (D8). A tier set by hand in config is sticky: it is stored
as the reason "config: <tier>" and no computed signal overrides it.

Identity rules (Phase 1 review):
  * Shared hosts (app stores, Google Docs/Forms, link-in-bio, site builders,
    *.vercel.app, medium.com, ...) are never a company's domain: usable_domain()
    returns '' for them and every subdomain of them.
  * Same normalised name + two different real domains = two companies. The
    `normalized_name` column is UNIQUE, so the second one is stored under the
    disambiguated key '<name>#<domain>' (e.g. 'atlas#atlashq.in'); compute_tier
    and get_company() strip everything from '#' on. Callers may also pass an
    explicit `match_key` (the funding watcher stores an ambiguous name-only
    signal as '<name>#funding-rss' rather than guessing which company it is).
  * config/companies.yaml + target_companies.yaml are the human source of
    truth: a config row always replaces the ATS fields (clearing stale ones),
    no other source overwrites a config-set ATS, and a company deleted from
    config loses its config tier, its 'config' signal and its config-granted
    ATS trust (ats_confidence drops to UNVERIFIED_CONFIDENCE, so it leaves live
    ingest; to block a company outright keep it in config with enabled: false).
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

# Hosts many unrelated companies share as their "website" — never an identity
# domain. Matched suffix-aware: the host itself AND every subdomain of it
# (acme.vercel.app, play.google.com/...), so google.com itself stays usable.
_GENERIC_DOMAINS = frozenset({
    # social / profiles / directories
    "linkedin.com", "lnkd.in", "facebook.com", "fb.com", "fb.me", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "youtu.be", "t.me", "wa.me", "whatsapp.com",
    "angel.co", "wellfound.com", "crunchbase.com", "ycombinator.com", "producthunt.com",
    "github.com", "gitlab.com", "bitbucket.org", "linktr.ee", "bio.link", "beacons.ai",
    # app stores
    "play.google.com", "apps.apple.com", "itunes.apple.com", "testflight.apple.com",
    "chrome.google.com", "chromewebstore.google.com",
    # Google docs / forms / sites / short links
    "docs.google.com", "drive.google.com", "forms.google.com", "sites.google.com",
    "maps.google.com", "g.page", "goo.gl", "g.co", "forms.gle", "bit.ly", "tinyurl.com",
    # scheduling / forms
    "calendly.com", "cal.com", "typeform.com", "tally.so", "jotform.com", "airtable.com",
    # site builders / static hosting
    "notion.site", "notion.so", "wixsite.com", "webflow.io", "carrd.co", "framer.website",
    "framer.ai", "framer.app", "super.site", "bubbleapps.io", "softr.app", "github.io",
    "gitlab.io", "vercel.app", "netlify.app", "herokuapp.com", "pages.dev", "web.app",
    "firebaseapp.com", "replit.app", "repl.co", "glitch.me", "onrender.com", "fly.dev",
    "wordpress.com", "blogspot.com", "myshopify.com", "gitbook.io", "readthedocs.io",
    "squarespace.com", "weebly.com", "godaddysites.com", "mystrikingly.com",
    # blogs / newsletters
    "medium.com", "substack.com", "beehiiv.com", "mailchi.mp", "hashnode.dev",
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


def is_shared_host(domain: Optional[str]) -> bool:
    """True for a shared host or any subdomain of one (acme.vercel.app, play.google.com)."""
    d = (domain or "").lower().strip(".")
    parts = d.split(".")
    return any(".".join(parts[i:]) in _GENERIC_DOMAINS for i in range(len(parts)))


def usable_domain(url_or_domain: Optional[str]) -> str:
    """domain_of(), but '' for shared hosts (linkedin.com/company/x is not a domain)."""
    d = domain_of(url_or_domain)
    return "" if (not d or "." not in d or is_shared_host(d)) else d


def base_key(normalized_name: Optional[str]) -> str:
    """'atlas#atlashq.in' -> 'atlas' (strip a disambiguation suffix)."""
    return str(normalized_name or "").split("#", 1)[0]


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
    # brands: a plain string is a distinctive name (loose match: "Google India").
    # A mapping {name, domains, aliases} is an AMBIGUOUS common-word brand
    # ("Zoom", "Linear"): it matches when the company's domain is one of its
    # domains, or - only when no domain is known - on the exact normalised name.
    loose: set[str] = set()
    exact: dict[str, set[str]] = {}
    for b in r.get("brands") or []:
        if isinstance(b, Mapping):
            names = [b.get("name"), *(b.get("aliases") or [])]
            doms = {usable_domain(d) for d in b.get("domains") or []} - {""}
            for n in names:
                if n and normalize_company_name(str(n)):
                    exact.setdefault(normalize_company_name(str(n)), set()).update(doms)
        elif b:
            loose.add(normalize_company_name(str(b)))
    r["brands"], r["brand_exact"] = loose, exact
    # brand domains match EXACTLY (list a subdomain explicitly if it should count);
    # a shared host listed by mistake is ignored, never matched.
    bd = set()
    for d in r.get("brand_domains") or []:
        dom = domain_of(str(d)) if d else ""
        if dom and not is_shared_host(dom):
            bd.add(dom)
        elif dom:
            log.warning("brands.yaml: brand_domains entry %s is a shared host; ignored", dom)
    r["brand_domains"] = bd
    r["body_shop_names"] = {normalize_company_name(b) for b in r.get("body_shop_names") or [] if b}
    r["body_shop_allow"] = {normalize_company_name(b) for b in r.get("body_shop_allow") or [] if b}
    r["body_shop_patterns"] = [
        (p, re.compile(r"\b" + r"\s+".join(map(re.escape, normalize_company_name(p).split())) + r"\b"))
        for p in r.get("body_shop_patterns") or [] if p and normalize_company_name(p)
    ]
    r["_prepared"] = True
    return r


def _domain_in(domain: str, domains: set[str]) -> bool:
    """Exact domain match only: careers.flipkart.com counts only if listed itself."""
    return bool(domain) and domain in domains


def _brand_match(norm: str, core: str, domain: str, r: Mapping[str, Any]) -> bool:
    if norm in r["brands"] or core in r["brands"]:
        return True
    for key in (norm, core):
        doms = r.get("brand_exact", {}).get(key)
        if doms is None:
            continue
        if domain:                       # domain known: it must agree
            if domain in doms:
                return True
        elif key == norm:                # no domain: exact name only, never the core
            return True
    return any(domain and domain in doms for doms in r.get("brand_exact", {}).values())


def compute_tier(company: Mapping[str, Any], rules: Mapping[str, Any]) -> tuple[str, list[str]]:
    """(tier, reasons) for one company dict. Pure: no I/O.

    Order: a config tier (sticky) > body-shop name > brand > Public (unless a
    body-shop keyword matches) > funded > body-shop keyword > unknown. Keywords
    only exclude when nothing positive is known, so a funded product startup
    with "staffing" in its name is not thrown out - but a "Public" stage alone
    is not a positive enough signal to skip them (listed IT-services firms).
    """
    r = prepare_rules(rules)
    name = company.get("name") or ""
    # fresh key from the name: a stored key may predate the current normaliser
    # ('d e shaw' vs 'de shaw') or carry a '#domain' disambiguation suffix
    norm = normalize_company_name(name) or base_key(company.get("normalized_name"))
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

    if _domain_in(domain, r["brand_domains"]):
        return "brand", [f"brands.yaml: brand domain {domain}"]
    if _brand_match(norm, core, domain, r):
        return "brand", [f"brands.yaml: brand '{name}'"]

    def keyword_hit() -> Optional[str]:
        if allowed:
            return None
        for raw, pat in r["body_shop_patterns"]:
            if pat.search(norm):
                return f"brands.yaml: staffing/services keyword '{raw}' in '{name}'"
        return None

    stage = company.get("funding_stage")
    stage_src = "bsm" if "bsm" in sources else ("funding-rss" if "funding-rss" in sources else "stage")
    if stage and str(stage).strip().lower() == "public":
        hit = keyword_hit()
        return ("excluded", [hit]) if hit else ("brand", [f"{stage_src}: Public"])

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

    hit = keyword_hit()
    if hit:
        return "excluded", [hit]

    why = [f"{stage_src}: {stage} (below {r['funded_min_stage']})"] if stage else []
    return "unknown", why or ["no brand, YC or funding signal"]


# --------------------------------------------------------------------- DB

def _by_key(conn, key: str) -> Optional[Any]:
    return conn.execute("SELECT * FROM companies WHERE normalized_name = ?", (key,)).fetchone()


def _find(conn, norm: str, domain: str) -> tuple[Optional[Any], str]:
    """(matching row or None, normalized_name key to insert under).

    Domain first. Then name - but a same-named row with a DIFFERENT real domain
    is a different company, stored under '<norm>#<domain>'."""
    if domain:
        row = conn.execute("SELECT * FROM companies WHERE domain = ? ORDER BY id LIMIT 1",
                           (domain,)).fetchone()
        if row:
            return row, norm
    row = _by_key(conn, norm)
    if row is not None and domain:
        other = usable_domain(row["domain"] or row["website"])
        if other and other != domain:
            key = f"{norm}#{domain}"
            return _by_key(conn, key), key
    return row, norm


def name_candidates(name: str, db_path: Any = None) -> list[dict]:
    """Every row whose (disambiguated) key has this normalised name."""
    norm = normalize_company_name(name)
    if not norm:
        return []
    like = norm.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "#%"
    with store.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM companies WHERE normalized_name = ? "
                            "OR normalized_name LIKE ? ESCAPE '\\' ORDER BY id", (norm, like)).fetchall()
    return [_decode(r) for r in rows]


def _is_config_ats(evidence: Any) -> bool:
    ev = str(evidence or "")
    return ev == "config" or ev.startswith("config (")


def upsert_company(name: str, *, db_path: Any = None, match_key: Optional[str] = None,
                   company_id: Optional[int] = None, **fields: Any) -> int:
    """Insert or merge one company; returns its id.

    Extra conveniences: `signal_source="yc-oss"` appends one source;
    `verified=True/False` sets ats_confidence when it isn't given. Lists
    (signal_sources, tier_reasons, investors) are merged, never replaced.
    Scalars from the caller win, except: website/domain are fill-only, ATS
    fields are not overwritten by a lower-confidence guess (nor, from a
    non-config caller, when config set them), and a tier set by config is never
    replaced by a non-config caller. A config caller passing `ats` replaces
    every ATS field, clearing the ones it leaves out.

    `company_id` updates exactly that row (LookupError if it is gone - never a
    new row); `match_key` matches/inserts by that stored key only.
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
        if company_id is not None:
            row, key = conn.execute("SELECT * FROM companies WHERE id = ?",
                                    (int(company_id),)).fetchone(), norm
            if row is None:
                raise LookupError(f"company id {company_id} not found")
        elif match_key:
            row, key = _by_key(conn, match_key), match_key
        else:
            row, key = _find(conn, norm, domain)
        if row is None:
            vals = {k: v for k, v in fields.items() if v is not None}
            for f in _JSON_LIST_FIELDS:
                if f in vals:
                    vals[f] = json.dumps(_as_list(vals[f]), ensure_ascii=False)
            vals.update(name=name, normalized_name=key, first_seen=now, last_seen=now, updated_at=now)
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
            (new_conf if new_conf is not None else 0.0) < (old.get("ats_confidence") or 0.0)
            or (not from_config and _is_config_ats(old.get("ats_evidence"))))
        if from_config and fields.get("ats") is not None:
            keep_ats = False      # config is the human source of truth for the ATS
            for k in _ATS_FIELDS:
                if k != "ats_checked_at" and fields.get(k) is None and old.get(k) is not None:
                    upd[k] = None
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
                # a stale shared-host value (play.google.com) counts as empty
                if not old.get(k) or (not usable_domain(old.get(k)) and usable_domain(v)):
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
            row = _by_key(conn, normalize_company_name(q))
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
            if c.get("domain") and not usable_domain(c["domain"]):   # legacy shared-host domain
                conn.execute("UPDATE companies SET domain = NULL WHERE id = ?", (c["id"],))
                c["domain"] = None
            tier, reasons = compute_tier(c, r)
            counts[tier] = counts.get(tier, 0) + 1
            if tier != c.get("tier") or reasons != c["tier_reasons"]:
                conn.execute("UPDATE companies SET tier = ?, tier_reasons = ?, updated_at = ? WHERE id = ?",
                             (tier, json.dumps(reasons, ensure_ascii=False), time.time(), c["id"]))
    log.info("tiers recomputed: %s", counts)
    return counts


def _merge_rows(conn, keep: dict, drop: dict) -> None:
    """Fold row `drop` into row `keep` (same company), re-point jobs, delete `drop`.
    Lists are merged; scalars only fill gaps; the stronger ATS wins."""
    upd: dict[str, Any] = {}
    for f in _JSON_LIST_FIELDS:
        merged = _merge_lists(keep[f], drop[f])
        if merged != keep[f]:
            upd[f] = json.dumps(merged, ensure_ascii=False)
    take_ats = bool(drop.get("ats")) and (drop.get("ats_confidence") or 0.0) > (keep.get("ats_confidence") or 0.0)
    for k in _FIELDS - set(_JSON_LIST_FIELDS) - {"enabled", "yc_top_company"}:
        if k in _ATS_FIELDS:
            if take_ats and drop.get(k) != keep.get(k):
                upd[k] = drop.get(k)
        elif keep.get(k) in (None, "") and drop.get(k) not in (None, ""):
            upd[k] = drop[k]
    if drop.get("yc_top_company") and not keep.get("yc_top_company"):
        upd["yc_top_company"] = 1
    if drop.get("enabled") is False and keep.get("enabled") is not False:
        upd["enabled"] = 0                       # a disable on either copy sticks (fail-closed)
    for k, agg in (("first_seen", min), ("last_seen", max)):
        vals = [v for v in (keep.get(k), drop.get(k)) if v is not None]
        if vals and agg(vals) != keep.get(k):
            upd[k] = agg(vals)
    if upd:
        sets = ", ".join(f"{k} = ?" for k in upd)
        conn.execute(f"UPDATE companies SET {sets} WHERE id = ?", (*upd.values(), keep["id"]))
    conn.execute("UPDATE jobs SET company_id = ? WHERE company_id = ?", (keep["id"], drop["id"]))
    conn.execute("DELETE FROM companies WHERE id = ?", (drop["id"],))
    keep.update({k: (json.loads(v) if k in _JSON_LIST_FIELDS else v) for k, v in upd.items()})


def renormalize_names(db_path: Any = None) -> dict[str, int]:
    """Recompute normalized_name for every row with the CURRENT normaliser.

    Idempotent. Rows whose new base key collides are merged only when they are
    the same company: same real domain, or one side has no domain while the
    group holds at most one domain. Same name + different domains stay apart
    as '<base>#<domain>'. Name-only signal rows stored under a non-domain key
    ('<base>#funding-rss') are never merged into a domain row. Merged rows'
    jobs are re-pointed (jobs.company_id) before the duplicate is deleted.
    Returns {"rekeyed": n, "merged": n}."""
    out = {"rekeyed": 0, "merged": 0}
    with store.connect(db_path) as conn:
        rows = [_decode(r) for r in conn.execute("SELECT * FROM companies ORDER BY id").fetchall()]
        groups: dict[str, list[dict]] = {}
        for c in rows:
            base = normalize_company_name(c["name"]) or base_key(c["normalized_name"])
            groups.setdefault(base, []).append(c)
        final: dict[int, str] = {}
        for base, members in groups.items():
            by_dom: dict[str, list[dict]] = {}
            pinned: dict[str, list[dict]] = {}
            loose: list[dict] = []
            for c in members:
                dom = usable_domain(c.get("domain") or c.get("website"))
                suffix = c["normalized_name"].split("#", 1)[1] if "#" in c["normalized_name"] else ""
                if dom:
                    by_dom.setdefault(dom, []).append(c)
                elif suffix and "." not in suffix:
                    pinned.setdefault(suffix, []).append(c)
                else:
                    loose.append(c)
            clusters: list[tuple[str, list[dict]]] = [(d, cs) for d, cs in by_dom.items()]
            if len(by_dom) <= 1:
                if clusters:
                    clusters[0][1].extend(loose)
                elif loose:
                    clusters.append(("", loose))
            else:   # ambiguous: a domainless row cannot be told apart -> keep it alone
                clusters += [(f"id{c['id']}", [c]) for c in loose]
            clusters += [(sfx, cs) for sfx, cs in pinned.items()]
            # the base key goes to the cluster already holding it, else the oldest
            owner = next((i for i, (_, cs) in enumerate(clusters)
                          if any(c["normalized_name"] == base for c in cs)), None)
            if owner is None:
                owner = min(range(len(clusters)), key=lambda i: min(c["id"] for c in clusters[i][1]))
            for i, (tag, cs) in enumerate(clusters):
                cs.sort(key=lambda c: (c["normalized_name"] != base, c["id"]))
                keep = cs[0]
                for dup in cs[1:]:
                    _merge_rows(conn, keep, dup)
                    out["merged"] += 1
                final[keep["id"]] = base if i == owner else f"{base}#{tag}"
        stored = {c["id"]: c["normalized_name"] for c in rows}
        changed = {i: k for i, k in final.items() if k != stored[i]}
        for i in changed:            # two passes so the UNIQUE index never trips mid-way
            conn.execute("UPDATE companies SET normalized_name = ? WHERE id = ?", (f"__renorm#{i}", i))
        for i, k in changed.items():
            conn.execute("UPDATE companies SET normalized_name = ? WHERE id = ?", (k, i))
        out["rekeyed"] = len(changed)
    if out["rekeyed"] or out["merged"]:
        log.info("renormalize: %s", out)
    return out


def _ingest_connectors() -> set[str]:
    """ATS names we can actually pull jobs from (search-style connectors excluded)."""
    from .connectors import REGISTRY
    return set(REGISTRY) - {"adzuna", "jsearch", "manual"}


def enabled_ats_companies(db_path: Any = None, rules: Optional[Mapping[str, Any]] = None,
                          *, include_config: bool = False) -> list[dict]:
    """Discovered companies ready for live ingest, as config/companies.yaml-style rows.

    Requires: enabled, a supported ATS verified to VERIFIED_CONFIDENCE, and a
    tier in rules.require_tier. `company_id` is included for linking jobs.
    Rows that come from config are skipped (the roster already reads the config
    files; re-adding them here could resurrect stale DB copies) unless
    include_config=True.
    """
    req = (prepare_rules(rules) if rules is not None
           else prepare_rules(config.filters().get("companies") or {}))["require_tier"]
    supported = _ingest_connectors()
    out: list[dict] = []
    for c in list_companies(tier=req, enabled=True, db_path=db_path):
        if not include_config and "config" in c["signal_sources"]:
            continue
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
    """Load config/companies.yaml + config/target_companies.yaml rows (missing files skipped).

    Rows tagged 'config' that are in none of `paths` any more are de-configured
    (see _deconfigure): the config files are the whole truth about config."""
    n = 0
    synced: set[int] = set()
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
            synced.add(cid)
            if not f.get("tier"):
                _drop_config_tier(cid, db_path)  # tier removed from config -> no longer sticky
            n += 1
    removed = _deconfigure(synced, db_path)
    if n or removed:
        recompute_tiers(db_path)
    return n


def _deconfigure(keep_ids: set[int], db_path: Any) -> int:
    """A company deleted from config: drop the 'config' signal + config tier, and
    the trust config gave its ATS (confidence -> UNVERIFIED, so enabled_ats_companies
    no longer offers it; the detector may re-verify it like any discovered row)."""
    n = 0
    now = time.time()
    with store.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM companies WHERE signal_sources LIKE '%\"config\"%'").fetchall()
        for raw in rows:
            c = _decode(raw)
            if c["id"] in keep_ids or "config" not in c["signal_sources"]:
                continue
            upd: dict[str, Any] = {
                "signal_sources": json.dumps([x for x in c["signal_sources"] if x != "config"]),
                "tier_reasons": json.dumps([x for x in c["tier_reasons"]
                                            if not _CONFIG_TIER_RE.match(str(x))], ensure_ascii=False),
                "updated_at": now,
            }
            if _is_config_ats(c.get("ats_evidence")):
                upd["ats_confidence"] = min(c.get("ats_confidence") or 0.0, UNVERIFIED_CONFIDENCE)
                upd["ats_evidence"] = f"removed from config (was: {c['ats_evidence']})"
            sets = ", ".join(f"{k} = ?" for k in upd)
            conn.execute(f"UPDATE companies SET {sets} WHERE id = ?", (*upd.values(), c["id"]))
            n += 1
    if n:
        log.info("sync_config_companies: %d company row(s) no longer in config de-configured", n)
    return n


def record_ats_detection(company_id: int, result: Any, *, checked_at: Optional[float] = None,
                         db_path: Any = None) -> None:
    """Store one ats_detect.detect() result on a company row.

    Always sets ats_checked_at + ats_evidence. The ATS fields are written with
    confidence 1.0 only when result.verified; an unverified detection is written
    with the detector's own confidence, and only if it is not weaker than what
    the row already holds. A config-set ATS is never touched."""
    now = time.time() if checked_at is None else float(checked_at)
    ev = str(getattr(result, "evidence", "") or "")
    with store.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (int(company_id),)).fetchone()
        if row is None:
            raise LookupError(f"company id {company_id} not found")
        old = _decode(row)
        upd: dict[str, Any] = {"ats_checked_at": now, "updated_at": now}
        config_ats = _is_config_ats(old.get("ats_evidence"))
        if not config_ats:
            upd["ats_evidence"] = ("ats_detect: " + ev)[:500]
        ats = getattr(result, "ats", None)
        verified = bool(getattr(result, "verified", False))
        conf = 1.0 if verified else float(getattr(result, "confidence", 0.0) or 0.0)
        if ats and not config_ats and (verified or conf >= (old.get("ats_confidence") or 0.0)):
            upd.update(ats=str(ats), ats_slug=getattr(result, "slug", None),
                       ats_tenant=getattr(result, "tenant", None),
                       ats_wd_server=getattr(result, "wd_server", None),
                       ats_site=getattr(result, "site", None),
                       ats_region=getattr(result, "region", None), ats_confidence=conf)
        sets = ", ".join(f"{k} = ?" for k in upd)
        conn.execute(f"UPDATE companies SET {sets} WHERE id = ?", (*upd.values(), int(company_id)))


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
