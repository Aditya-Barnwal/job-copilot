"""Employer job-board seeds from open datasets (Phase 1b, config/seeds.yaml).

Public lists of "company X uses ATS Y with board slug Z" (kalil0321/ats-scrapers,
colophon-group/jobseek, the zshah101 internships list) plus the employer boards
named in SimplifyJobs' new-grad listings. Each becomes a companies row with its
ATS at UNVERIFIED confidence: nothing here reaches live ingest until the
rotating India probe (discovery/probe.py) has called the board once and found
at least one India / APAC / worldwide-remote role on it.

Why so careful:
  * the datasets are third-party and unverified, so every slug is validated
    (it ends up in an API URL path) and a dataset never overwrites an ATS some
    other source (config, the detector, Bangalore Startup Map) already set;
  * two names for one board ("1910" / "1910 Genetics") must not become two
    rows polling the same board, so rows dedupe by board key as well as name;
  * an unchanged dataset (304 or same sha256, kept in source_state) is skipped,
    so a daily run costs one conditional GET per file.
Datasets without a licence (SimplifyJobs) give facts only: employer name +
board slug, credited by name in ats_evidence.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from .. import companies as co
from .. import config, store
from ..ats_detect import detect_from_url
from ..http import HttpError, PoliteClient, default_client
from ..normalize import normalize_company_name

log = logging.getLogger("jobcopilot.discovery.seeds")

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
WD_SERVER_RE = re.compile(r"^wd\d{1,3}$", re.I)
DEFAULT_ATS = ("greenhouse", "ashby", "lever", "workday", "gem", "workable", "recruitee",
               "rippling", "teamtailor", "pinpoint", "personio", "bamboohr", "keka")
# Hosts where the board slug is the first DNS label: <slug>.<suffix>
_SUBDOMAIN_ATS = {
    "recruitee.com": "recruitee", "teamtailor.com": "teamtailor", "pinpointhq.com": "pinpoint",
    "jobs.personio.de": "personio", "jobs.personio.com": "personio",
    "bamboohr.com": "bamboohr", "keka.com": "keka",
}
# Hosts where the slug is the first path segment: <host>/<slug>
_PATH_ATS = {"apply.workable.com": "workable", "ats.rippling.com": "rippling"}
_NOT_SLUGS = frozenset({"www", "app", "api", "jobs", "careers", "embed", "static", "cdn", "assets"})
_LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Za-z]{2}$")


def valid_slug(value: Any) -> bool:
    return isinstance(value, str) and SLUG_RE.fullmatch(value) is not None


@dataclass
class Seed:
    """One employer board from a dataset. Build with make_seed() (validates)."""
    name: str
    ats: str
    slug: Optional[str] = None
    tenant: Optional[str] = None       # workday
    wd_server: Optional[str] = None    # workday
    site: Optional[str] = None         # workday
    region: Optional[str] = None       # greenhouse/lever "eu", personio "de"
    website: Optional[str] = None
    sources: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)

    def board_key(self) -> tuple:
        if self.ats == "workday":
            return ("workday", (self.tenant or "").lower(), (self.site or "").lower())
        return (self.ats, (self.slug or "").lower())

    def ats_fields(self) -> dict[str, Any]:
        return {"ats": self.ats, "ats_slug": self.slug, "ats_tenant": self.tenant,
                "ats_wd_server": self.wd_server, "ats_site": self.site, "ats_region": self.region}


def make_seed(name: Any, ats: Any, *, slug: Any = None, tenant: Any = None, wd_server: Any = None,
              site: Any = None, region: Any = None, website: Any = None,
              allowed: Iterable[str] = DEFAULT_ATS) -> Optional[Seed]:
    """A validated Seed, or None when the row is unusable (unknown ATS, bad id, no name)."""
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    ats = str(ats or "").strip().lower()
    if not name or len(name) > 200 or ats not in set(allowed):
        return None
    if ats == "workday":
        wd = str(wd_server or "").lower()
        if not (valid_slug(tenant) and valid_slug(site) and WD_SERVER_RE.fullmatch(wd)):
            return None
        return Seed(name, ats, tenant=str(tenant).lower(), wd_server=wd, site=site,
                    website=website or None)
    if not valid_slug(slug) or str(slug).lower() in _NOT_SLUGS:
        return None
    reg = str(region).lower() if region else None
    return Seed(name, ats, slug=slug, region=reg, website=website or None)


def seed_from_url(name: Any, url: Any, allowed: Iterable[str] = DEFAULT_ATS) -> Optional[Seed]:
    """Employer board named by a URL (a job or board link), or None. No network."""
    url = str(url or "").strip()[:2048]
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    for suffix, ats in _SUBDOMAIN_ATS.items():
        if host.endswith("." + suffix):
            label = host[: -len(suffix) - 1]
            if "." in label:
                return None
            region = "de" if suffix == "jobs.personio.de" else None
            return make_seed(name, ats, slug=label, region=region, allowed=allowed)
    if host in _PATH_ATS:
        if segs and _LOCALE_RE.match(segs[0]):
            segs = segs[1:]
        return make_seed(name, _PATH_ATS[host], slug=segs[0] if segs else None, allowed=allowed)
    res = detect_from_url(url)      # greenhouse / ashby / lever / workday / gem (+ others we skip)
    if res is None or res.ats not in set(allowed):
        return None
    if res.ats == "workday":
        return make_seed(name, "workday", tenant=res.tenant, wd_server=res.wd_server,
                         site=res.site, allowed=allowed)
    return make_seed(name, res.ats, slug=res.slug, region=res.region, allowed=allowed)


# ------------------------------------------------------------------ parsers (pure)

def _text(data: Any) -> str:
    return data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else str(data)


def parse_kalil_csv(data: Any, ats: str, allowed: Iterable[str] = DEFAULT_ATS) -> list[Seed]:
    """ats-scrapers ats-companies/<ats>.csv: name,slug,url (keka: name,url).
    Workday slugs there are 'tenant/site' without the wdN server, so Workday
    (and any row without a slug) is read from the board URL instead."""
    out: list[Seed] = []
    for row in csv.DictReader(io.StringIO(_text(data))):
        name, slug, url = row.get("name"), (row.get("slug") or "").strip(), row.get("url")
        if ats == "workday" or not slug:
            s = seed_from_url(name, url, allowed)
            s = s if s is not None and s.ats == ats else None
        else:
            region = "de" if ats == "personio" and ".jobs.personio.de" in (url or "") else None
            if ats in ("greenhouse", "lever") and re.search(r"\.eu\.(greenhouse\.io|lever\.co)", url or ""):
                region = "eu"
            s = make_seed(name, ats, slug=slug, region=region, allowed=allowed)
        if s is not None:
            out.append(s)
    return out


def parse_jobseek_names(data: Any) -> dict[str, tuple[str, Optional[str]]]:
    """jobseek companies.csv -> {company_slug: (display name, website)}."""
    out: dict[str, tuple[str, Optional[str]]] = {}
    for row in csv.DictReader(io.StringIO(_text(data))):
        slug = (row.get("slug") or "").strip()
        if slug and (row.get("name") or "").strip():
            out[slug] = (row["name"].strip(), (row.get("website") or "").strip() or None)
    return out


def _name_from_slug(slug: str) -> str:
    return " ".join(p.capitalize() for p in re.split(r"[-_]+", slug) if p)


def parse_jobseek_csv(data: Any, names: Optional[Mapping[str, tuple[str, Optional[str]]]] = None,
                      allowed: Iterable[str] = DEFAULT_ATS) -> list[Seed]:
    """jobseek boards.csv. monitor_type names the ATS; monitor_config holds the
    token/slug/tenant; everything else (dom, sitemap, api_sniffer, ...) is skipped."""
    names = names or {}
    allowed = set(allowed)
    out: list[Seed] = []
    for row in csv.DictReader(io.StringIO(_text(data))):
        ats = (row.get("monitor_type") or "").strip().lower()
        if ats not in allowed:
            continue
        cslug = (row.get("company_slug") or "").strip()
        name, website = names.get(cslug) or (_name_from_slug(cslug), None)
        try:
            cfg = json.loads(row.get("monitor_config") or "{}")
        except ValueError:
            cfg = {}
        cfg = cfg if isinstance(cfg, dict) else {}
        url = row.get("board_url") or ""
        slug = cfg.get("token") or cfg.get("slug") or cfg.get("tenant")
        s = seed_from_url(name, url, allowed)
        if s is not None and s.ats != ats:
            s = None
        if s is None and slug and ats != "workday":
            region = "de" if ats == "personio" and ".jobs.personio.de" in url else None
            s = make_seed(name, ats, slug=slug, region=region, allowed=allowed)
        if s is not None:
            s.website = website
            out.append(s)
    return out


def parse_zshah_json(data: Any, allowed: Iterable[str] = DEFAULT_ATS) -> list[Seed]:
    """zshah101 data/companies.json: [{name, slug, ats, wd?, site?}]; Workday's slug is the tenant."""
    if not isinstance(data, list):
        raise ValueError("zshah companies.json: expected a JSON list")
    out: list[Seed] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ats = str(item.get("ats") or "").lower()
        if ats == "workday":
            s = make_seed(item.get("name"), ats, tenant=item.get("slug"), wd_server=item.get("wd"),
                          site=item.get("site"), allowed=allowed)
        else:
            s = make_seed(item.get("name"), ats, slug=item.get("slug"), allowed=allowed)
        if s is not None:
            out.append(s)
    return out


def parse_simplify_json(data: Any, allowed: Iterable[str] = DEFAULT_ATS) -> list[Seed]:
    """SimplifyJobs listings.json -> one Seed per (employer, board) found in apply URLs."""
    if not isinstance(data, list):
        raise ValueError("simplify listings.json: expected a JSON list")
    out: list[Seed] = []
    seen: set[tuple] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        s = seed_from_url(item.get("company_name"), item.get("url"), allowed)
        if s is None:
            continue
        key = (normalize_company_name(s.name), s.board_key())
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


# ------------------------------------------------------------------ merge + store

def merge_seeds(seeds: Iterable[Seed], ats_order: Iterable[str] = DEFAULT_ATS) -> list[Seed]:
    """One Seed per employer name AND per board: the same board under two names
    merges into the first; a name with several boards keeps the highest-priority
    ATS (connectors we have today first). Sources/datasets are unioned."""
    rank = {a: i for i, a in enumerate(ats_order)}
    by_name: dict[str, Seed] = {}
    for s in seeds:
        norm = normalize_company_name(s.name)
        if not norm:
            continue
        cur = by_name.get(norm)
        if cur is None:
            by_name[norm] = s
            continue
        if rank.get(s.ats, 99) < rank.get(cur.ats, 99):
            s.sources, s.datasets = _union(s.sources, cur.sources), _union(s.datasets, cur.datasets)
            s.website = s.website or cur.website
            by_name[norm] = s
        else:
            cur.sources, cur.datasets = _union(cur.sources, s.sources), _union(cur.datasets, s.datasets)
            cur.website = cur.website or s.website
    by_board: dict[tuple, Seed] = {}
    for s in by_name.values():
        cur = by_board.get(s.board_key())
        if cur is None:
            by_board[s.board_key()] = s
        else:
            cur.sources, cur.datasets = _union(cur.sources, s.sources), _union(cur.datasets, s.datasets)
    return list(by_board.values())


def _union(a: Iterable[str], b: Iterable[str]) -> list[str]:
    out = list(a)
    out.extend(x for x in b if x not in out)
    return out


def _evidence(s: Seed, licences: Mapping[str, str]) -> str:
    parts = [f"{d} ({licences.get(d) or 'unlicensed'})" for d in s.datasets]
    return ("seed: " + ", ".join(parts))[:500]


def import_seeds(seeds: Iterable[Seed], *, db_path: Any = None,
                 licences: Optional[Mapping[str, str]] = None) -> dict[str, int]:
    """Upsert merged seeds into companies. Never overwrites an existing ATS:
    a row that already has one (any source) only gains the dataset signal."""
    licences = licences or {}
    with store.connect(db_path) as conn:
        rows = conn.execute("SELECT id, name, normalized_name, domain, ats, ats_slug, ats_tenant, "
                            "ats_site, signal_sources FROM companies").fetchall()
    by_norm = {r["normalized_name"]: dict(r) for r in rows}
    by_domain = {r["domain"]: dict(r) for r in rows if r["domain"]}
    by_board: dict[tuple, dict] = {}
    for r in rows:
        if r["ats"] == "workday" and r["ats_tenant"] and r["ats_site"]:
            by_board[("workday", r["ats_tenant"].lower(), r["ats_site"].lower())] = dict(r)
        elif r["ats"] and r["ats_slug"]:
            by_board[(r["ats"], r["ats_slug"].lower())] = dict(r)

    out = {"new": 0, "ats_added": 0, "signal_only": 0, "failed": 0}
    for s in seeds:
        norm = normalize_company_name(s.name)
        dom = co.usable_domain(s.website)
        row = by_board.get(s.board_key()) or by_norm.get(norm) or (by_domain.get(dom) if dom else None)
        try:
            if row is not None:
                cfg_row = "config" in co._as_list(row.get("signal_sources"))
                fields: dict[str, Any] = {"signal_sources": s.sources}
                if not row.get("ats") and not cfg_row:
                    fields.update(s.ats_fields(), ats_confidence=co.UNVERIFIED_CONFIDENCE,
                                  ats_evidence=_evidence(s, licences))
                    out["ats_added"] += 1
                    row["ats"] = s.ats
                    by_board[s.board_key()] = row
                else:
                    out["signal_only"] += 1
                co.upsert_company(row["name"], db_path=db_path, company_id=row["id"], **fields)
                continue
            cid = co.upsert_company(s.name, db_path=db_path, website=s.website,
                                    signal_sources=s.sources, ats_confidence=co.UNVERIFIED_CONFIDENCE,
                                    ats_evidence=_evidence(s, licences), **s.ats_fields())
            new = {"id": cid, "name": s.name, "ats": s.ats, "signal_sources": s.sources}
            by_norm[norm] = by_board[s.board_key()] = new
            if dom:
                by_domain[dom] = new
            out["new"] += 1
        except (ValueError, TypeError, LookupError) as exc:   # one bad row never sinks the import
            out["failed"] += 1
            log.warning("seeds: skipped %r: %s", s.name, exc)
    return out


# ------------------------------------------------------------------ fetch + run

def seeds_config() -> dict:
    return config.load("seeds")


def _fetch(client: PoliteClient, key: str, url: str, *, db_path: Any, force: bool) -> tuple[Optional[bytes], dict]:
    """(body, validators) or (None, validators) when unchanged (304 / same hash)."""
    state = co.get_source_state(key, db_path)
    headers = {} if force else co.conditional_headers(state)
    resp = client.get(url, headers=headers or None)
    validators = {"etag": resp.headers.get("ETag") or state.get("etag"),
                  "last_modified": resp.headers.get("Last-Modified") or state.get("last_modified")}
    if resp.status_code == 304:
        return None, validators
    body = resp.content
    validators["content_hash"] = co.content_hash(body)
    if not force and validators["content_hash"] == state.get("content_hash"):
        return None, validators
    return body, validators


def _parse(fmt: str, body: bytes, *, ats: Optional[str], names: Any, allowed: Iterable[str]) -> list[Seed]:
    if fmt == "kalil_csv":
        return parse_kalil_csv(body, ats or "", allowed)
    if fmt == "jobseek_csv":
        return parse_jobseek_csv(body, names, allowed)
    if fmt == "zshah_json":
        return parse_zshah_json(json.loads(body), allowed)
    if fmt == "simplify_json":
        return parse_simplify_json(json.loads(body), allowed)
    raise ValueError(f"unknown seed format {fmt!r}")


def run_seeds(*, client: Optional[PoliteClient] = None, db_path: Any = None,
              cfg: Optional[Mapping[str, Any]] = None, force: bool = False,
              only: Optional[Iterable[str]] = None) -> dict:
    """Fetch every configured dataset (conditional GET), import the changed ones.
    Returns per-file counts; a failing file is logged + reported, never raised."""
    cfg = cfg if cfg is not None else (seeds_config().get("seeds") or {})
    if not cfg.get("enabled", True) and not force:
        return {"skipped": "disabled in config/seeds.yaml"}
    allowed = [str(a).lower() for a in (cfg.get("ats") or DEFAULT_ATS)]
    client = client or default_client()
    only = set(only) if only else None
    now = time.time()
    files: dict[str, dict] = {}
    pending: list[tuple[str, dict]] = []      # (state key, validators) saved after import
    collected: list[Seed] = []
    licences: dict[str, str] = {}
    for name, ds in (cfg.get("datasets") or {}).items():
        if only and name not in only:
            continue
        licences[name] = str(ds.get("licence") or "")
        signal = str(ds.get("signal_source") or f"seed:{name}")
        fmt, url = str(ds.get("format") or ""), str(ds.get("url") or "")
        names = None
        targets = ([(f"seed:{name}:{a}", url.format(ats=urllib.parse.quote(str(a), safe="")), str(a))
                    for a in ds.get("files") or [] if str(a).lower() in allowed]
                   if "{ats}" in url else [(f"seed:{name}", url, None)])
        for key, u, ats in targets:
            try:
                body, validators = _fetch(client, key, u, db_path=db_path, force=force)
                if body is None:
                    files[key] = {"unchanged": True}
                    co.put_source_state(key, db_path, last_run=now, last_ok=now, last_error=None,
                                        **{k: v for k, v in validators.items() if k != "content_hash"})
                    continue
                if fmt == "jobseek_csv" and names is None and ds.get("names_url"):
                    names = parse_jobseek_names(client.get(str(ds["names_url"])).content)
                seeds = _parse(fmt, body, ats=ats, names=names, allowed=allowed)
            except (HttpError, ValueError) as exc:
                log.error("seeds %s failed: %s", key, exc)
                co.put_source_state(key, db_path, last_run=now, last_error=str(exc)[:500])
                files[key] = {"error": str(exc)[:200]}
                continue
            for s in seeds:
                s.sources, s.datasets = [signal], [name]
            collected.extend(seeds)
            files[key] = {"rows": len(seeds)}
            pending.append((key, {**validators, "items": len(seeds)}))
    merged = merge_seeds(collected, allowed)
    counts = import_seeds(merged, db_path=db_path, licences=licences) if merged else {}
    for key, v in pending:     # only after a successful import: a crash re-imports next run
        co.put_source_state(key, db_path, last_run=now, last_ok=now, last_error=None, **v)
    out: dict[str, Any] = {"files": files, "seeds": len(collected), "merged": len(merged), **counts}
    if any("error" in f for f in files.values()):
        out["error"] = sorted(k for k, f in files.items() if "error" in f)
    log.info("seeds: %s", {k: v for k, v in out.items() if k != "files"})
    return out


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m jobcopilot.discovery.seeds", description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="ignore ETag/hash and re-import")
    ap.add_argument("--only", nargs="*", help="dataset names from config/seeds.yaml")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    res = run_seeds(force=args.force, only=args.only)
    print(json.dumps(res, sort_keys=True))
    return 1 if res.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
