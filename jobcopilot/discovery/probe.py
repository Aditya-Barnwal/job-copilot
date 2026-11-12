"""Rotating India probe: verify seeded employer boards, a bounded slice per run (Phase 1b).

The seed datasets name ~30k employer boards with no country tags; polling all
of them every day would break the low-volume rule. Instead each run calls the
list endpoint of at most `limit` boards (config/seeds.yaml `probe`; 4000 per
run, at the user's request) ONCE, through the board's own connector, and keeps
only the boards that show at least one relevant India / APAC / worldwide-remote
role. A probed board is re-probed after `stale_days` (7), so the whole seed
set cycles through in about a week.

The real politeness bounds are per system, not the 4000 total:
  * PoliteClient rate-limits every host to 1 req/s, and every platform family
    (all <tenant>.recruitee.com, *.myworkdayjobs.com, teamtailor, pinpoint,
    personio, bamboohr, keka, plus the shared-host ATSs) shares ONE 1 req/s
    bucket, robots.txt fetches included;
  * a run sends at most `max_per_platform_per_run` (300) requests to one
    subdomain-per-company platform and at most `max_per_host_per_run` (3000) to
    any other single host;
  * a host (or platform) that answers 429 / asks us to wait too long is dropped
    for the rest of the run; a robots disallow drops that host only.

Queue order: overlap with lists we already trust (simplify-newgrad,
speedyapply, yc-oss, bsm), then brand/funded tier, then never- / oldest-probed.
Only boards whose tier live ingest accepts (co.ingest_tiers():
config/filters.yaml companies.require_tier + the extended watchlist) are queued: a board that can never be ingested is not
worth a request. A board is due when it was never probed and its ATS is not yet
verified, or when the probe itself last looked more than `stale_days` ago (or,
after a transient failure, more than ~1 day ago). Rows whose ATS came from
config or a verifying detector are never touched.

What a probe writes on the companies row (no dedicated columns exist yet):
  * ats_checked_at = probed_at
  * ats_evidence   = "probe: <date> jobs=N india_or_remote=K -> active|inactive"
  * ats_confidence = VERIFIED_CONFIDENCE when K >= 1 (live ingest picks it up);
    a board the probe activated earlier drops back to UNVERIFIED when K == 0
    on a successful probe, when the board is gone (404 / 410), or after
    `max_consecutive_failures` (3) failed probes in a row.
  * a transient failure (HTTP 5xx / 429 / 401 / 403, timeout, any other error)
    never demotes: confidence is kept, the evidence says
    "probe: <date> -> error (<why>) fails=N", and ats_checked_at is backdated so
    the board is due again in ~`retry_after_days` (1) instead of `stale_days`.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

from .. import companies as co
from .. import config, store
from ..http import HttpError, HttpStatusError, RetryLater, RobotsDisallowed
from ..models import IN, NON_IN, REMOTE
from ..filters import DEFAULT_TARGET_ROLES, is_seniority_high, passes_role
from ..normalize import infer_country_bucket
from ..regions import split_regions

log = logging.getLogger("jobcopilot.discovery.probe")

SOURCE = "india-probe"
EVIDENCE_PREFIX = "probe:"
DEFAULTS: dict[str, Any] = {
    "enabled": True, "limit": 4000, "max_per_host_per_run": 3000,
    "max_per_platform_per_run": 300, "stale_days": 7, "retry_after_days": 1,
    "max_consecutive_failures": 3, "require_tier": None,
    "require_role_match": True, "skip_senior": True, "target_roles": None,
    "workday_max_jobs": 20, "priority_signals": ["simplify-newgrad", "speedyapply", "yc-oss", "bsm"],
    "priority_tiers": ["brand", "funded"], "skip_ats": ["smartrecruiters"],
}
_NOT_PROBED = frozenset({"adzuna", "jsearch", "manual"})

# --------------------------------------------------------------- India rule (pure)

_GLOBAL_RE = re.compile(r"\b(?:worldwide|anywhere|global(?:ly)?|apac|asia(?:[- ]pacific)?|"
                        r"south[- ]asia|international)\b", re.I)
_REMOTE_RE = re.compile(r"\b(?:remote(?:[- ]first)?|work from home|wfh|distributed|"
                        r"fully|only|100%|flexible|location|based|role|position|team)\b", re.I)


def is_india_or_remote(job: Any) -> bool:
    """True if a role is open to someone in India: an Indian location, or "remote"
    / a stand-alone global word naming no other geography ("Remote" yes,
    "Remote - US" no, "Remote (Worldwide)" yes, "Global" / "APAC" yes). A global
    word inside a physical place name ("Global Delivery Hub, Chicago") does not count."""
    loc = str(getattr(job, "location_text", "") or "")
    regions = [str(r) for r in getattr(job, "region_restrictions", None) or []]
    text = " / ".join([loc, *regions])
    bucket = infer_country_bucket(text)
    if bucket == IN:
        return True
    remote = getattr(job, "workplace_type", None) == REMOTE or re.search(r"\bremote\b", text, re.I)
    if remote and bucket == NON_IN:
        return False
    rest = [t for t in split_regions(_REMOTE_RE.sub(" ", _GLOBAL_RE.sub(" ", text)))
            if re.sub(r"[\W_]+", "", t)]
    if remote:
        return not rest
    return bool(_GLOBAL_RE.search(text)) and not rest and bucket != NON_IN


# --------------------------------------------------------------- queue

def _relevant_title(job: Any, cfg: Mapping[str, Any]) -> bool:
    if not cfg.get("require_role_match", True):
        return True
    title = str(getattr(job, "title", "") or "")
    if not passes_role(title, cfg.get("target_roles") or DEFAULT_TARGET_ROLES):
        return False
    return not (cfg.get("skip_senior", True) and is_seniority_high(title))


def probe_config() -> dict:
    return {**DEFAULTS, **(config.load("seeds").get("probe") or {})}


def _probed(c: Mapping[str, Any]) -> bool:
    return str(c.get("ats_evidence") or "").startswith(EVIDENCE_PREFIX)


def _registry(skip: Iterable[str]) -> dict[str, type]:
    from ..connectors import BOARD_SOURCES, REGISTRY
    return {k: v for k, v in REGISTRY.items()
            if k not in BOARD_SOURCES and k not in _NOT_PROBED and k not in set(skip)}


def company_board(c: Mapping[str, Any], *, workday_max: int = 20) -> Optional[dict]:
    """Connector input for a companies row, or None if its board ids are unusable."""
    ats = (c.get("ats") or "").lower()
    base = {"name": c["name"], "sector": c.get("sector") or "tech", "tier": c.get("tier"),
            "company_id": c.get("id")}
    if ats == "workday":
        t, s, wd = c.get("ats_tenant"), c.get("ats_site"), (c.get("ats_wd_server") or "wd1")
        if not (t and s and re.fullmatch(r"wd\d{1,3}", wd) and _ok(t) and _ok(s)):
            return None
        return {**base, "tenant": t, "wd_server": wd, "site": s, "max": int(workday_max)}
    slug = c.get("ats_slug")
    if not (slug and _ok(slug)):
        return None
    out = {**base, "slug": slug}
    if c.get("ats_region"):
        out["region"] = c["ats_region"]
    return out


def _ok(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", str(value)) is not None


def board_host(ats: str, board: Mapping[str, Any], connector: Any = None) -> str:
    """The host a probe of this board hits; unknown shapes share one key per ATS
    (conservative: they then share the per-host cap)."""
    if ats == "workday":
        return f"{board['tenant']}.{board['wd_server']}.myworkdayjobs.com".lower()
    slug = urllib.parse.quote(str(board.get("slug") or ""), safe="")
    url = None
    attempts = (lambda: connector.list_url(slug),                       # signature varies per connector
                lambda: connector.BASE.format(slug=slug),
                lambda: connector.PAGE.format(slug=slug))               # keka: list_url needs an org id
    for attempt in attempts:
        try:
            url = attempt() if connector is not None else None
        except Exception:   # noqa: BLE001 — try the next shape, then fall back to the per-ATS key
            url = None
        if isinstance(url, str) and url:
            break
    host = urllib.parse.urlsplit(url).hostname if isinstance(url, str) and url else None
    return (host or f"{ats}:*").lower()


def cap_key(host: str, client: Any = None) -> str:
    """Accounting key for the per-run caps: the platform for subdomain-per-company
    platforms (a.recruitee.com and b.recruitee.com are one system), else the host."""
    from ..http import SUBDOMAIN_PLATFORMS, platform_of
    host = host.lower()
    plat = platform_of(host, SUBDOMAIN_PLATFORMS)
    return f"platform:{plat}" if plat else host


def _fail_count(c: Mapping[str, Any]) -> int:
    m = re.search(r"\bfails=(\d+)", str(c.get("ats_evidence") or "")) if _probed(c) else None
    return int(m.group(1)) if m else 0


def candidates(*, db_path: Any = None, cfg: Optional[Mapping[str, Any]] = None,
               supported: Optional[Iterable[str]] = None, now: Optional[float] = None) -> list[dict]:
    """Every due row, best first (not capped; run_probe applies limit + host caps).
    Only boards whose tier live ingest accepts (co.ingest_tiers(): require_tier + watchlist) are queued."""
    cfg = {**DEFAULTS, **(cfg or {})}
    require_tier = set(cfg.get("require_tier") or co.ingest_tiers())
    now = time.time() if now is None else float(now)
    cutoff = now - float(cfg["stale_days"]) * 86400
    supported = set(supported if supported is not None else _registry(cfg["skip_ats"]))
    prio_sig, prio_tier = set(cfg["priority_signals"]), set(cfg["priority_tiers"])
    with store.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM companies WHERE ats IS NOT NULL AND ats != '' "
                            "AND COALESCE(enabled, 1) = 1").fetchall()
    out: list[dict] = []
    for raw in rows:
        c = co._decode(raw)
        if (c.get("ats") or "").lower() not in supported or "config" in c["signal_sources"]:
            continue
        if str(c.get("ats_evidence") or "").startswith("config"):
            continue
        if c.get("tier") not in require_tier:   # live ingest would never read this board
            continue
        if _probed(c):
            if (c.get("ats_checked_at") or 0) >= cutoff:
                continue
        elif (c.get("ats_confidence") or 0.0) >= co.VERIFIED_CONFIDENCE:
            continue          # verified by the detector: already live, not ours to judge
        overlap = len(prio_sig.intersection(c["signal_sources"]))
        c["_prio"] = (-overlap, 0 if c.get("tier") in prio_tier else 1,
                      (c.get("ats_checked_at") or 0) if _probed(c) else 0, c["id"])
        out.append(c)
    out.sort(key=lambda c: c["_prio"])
    return out


# --------------------------------------------------------------- record

def _record(c: Mapping[str, Any], *, now: float, db_path: Any, jobs: Optional[int],
            india: Optional[int], note: str, transient: bool = False,
            cfg: Optional[Mapping[str, Any]] = None) -> str:
    """Write one probe outcome; returns 'active' / 'inactive' / 'retry'.

    `transient` (HTTP 5xx / 429 / 401 / 403, timeout, any other error) never demotes:
    confidence is kept and ats_checked_at is backdated so the board is due again
    after `retry_after_days`. Only the Nth consecutive failure demotes."""
    cfg = {**DEFAULTS, **(cfg or {})}
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    old_conf = c.get("ats_confidence") or 0.0
    checked = now
    if transient:
        fails = _fail_count(c) + 1
        if fails >= int(cfg["max_consecutive_failures"]):
            active, outcome = False, "inactive"
            ev = f"{EVIDENCE_PREFIX} {day} -> inactive ({note}; {fails} failures in a row) fails={fails}"
        else:
            active, outcome = None, "retry"
            ev = f"{EVIDENCE_PREFIX} {day} -> error ({note}) fails={fails} retry-soon"
            checked = now - (float(cfg["stale_days"]) - float(cfg["retry_after_days"])) * 86400
    else:
        active = bool(india)
        outcome = "active" if active else "inactive"
        counts = f" jobs={jobs} india_or_remote={india}" if jobs is not None else ""
        ev = f"{EVIDENCE_PREFIX} {day}{counts} -> {outcome}"
        ev = ev + (f" ({note})" if note else "")
    ev = ev[:500]
    if active:
        conf = max(old_conf, co.VERIFIED_CONFIDENCE)
    elif active is None:
        conf = old_conf                       # transient: keep what we knew
    elif _probed(c) and old_conf >= co.VERIFIED_CONFIDENCE:
        conf = co.UNVERIFIED_CONFIDENCE       # we activated it; the India roles are gone
    else:
        conf = old_conf
    with store.connect(db_path) as conn:
        conn.execute("UPDATE companies SET ats_confidence = ?, ats_checked_at = ?, ats_evidence = ?, "
                     "updated_at = ? WHERE id = ? AND COALESCE(ats_evidence, '') NOT LIKE 'config%'",
                     (conf, checked, ev, now, int(c["id"])))
    return outcome


# --------------------------------------------------------------- run

def run_probe(limit: Optional[int] = None, *, client: Any = None, db_path: Any = None,
              cfg: Optional[Mapping[str, Any]] = None,
              connectors: Optional[Mapping[str, Callable[..., Any]]] = None,
              now: Optional[float] = None) -> dict:
    """Probe up to `limit` due boards (one list call each). Never raises per board."""
    cfg = {**probe_config(), **(cfg or {})}
    if not cfg.get("enabled", True):
        return {"skipped": "disabled in config/seeds.yaml"}
    limit = int(cfg["limit"] if limit is None else limit)
    per_host = int(cfg["max_per_host_per_run"])
    per_platform = int(cfg["max_per_platform_per_run"])
    now = time.time() if now is None else float(now)
    registry = dict(connectors) if connectors is not None else _registry(cfg["skip_ats"])
    if client is None and connectors is None:
        from ..http import default_client
        client = default_client()
    out: dict[str, Any] = {"probed": 0, "active": 0, "inactive": 0, "gone": 0, "errors": 0,
                           "host_capped": 0, "host_blocked": 0, "retry": 0, "limit": limit,
                           "hosts": {}}
    hosts: dict[str, int] = {}
    blocked: set[str] = set()
    instances: dict[str, Any] = {}
    due = candidates(db_path=db_path, cfg=cfg, supported=registry, now=now)
    out["due"] = len(due)
    for c in due:
        if out["probed"] >= limit:
            break
        ats = c["ats"].lower()
        board = company_board(c, workday_max=cfg["workday_max_jobs"])
        if board is None:
            continue
        if ats not in instances:
            instances[ats] = registry[ats](client)
        conn = instances[ats]
        host = board_host(ats, board, conn)
        key = cap_key(host)
        if host in blocked or key in blocked:
            out["host_blocked"] += 1
            continue
        if hosts.get(key, 0) >= (per_platform if key.startswith("platform:") else per_host):
            out["host_capped"] += 1
            continue
        hosts[key] = hosts.get(key, 0) + 1
        out["probed"] += 1
        try:
            jobs = conn.parse(conn.fetch_raw(board), board)
        except (RetryLater, RobotsDisallowed) as exc:   # the host's call, not the board's
            # RetryLater: the shared bucket is exhausted, so the whole platform backs off;
            # a robots disallow is per tenant origin.
            blocked.add(key if isinstance(exc, RetryLater) else host)
            out["probed"] -= 1
            hosts[key] -= 1
            out["host_blocked"] += 1
            log.warning("probe: dropping %s for this run: %s", key, exc)
            continue
        except HttpStatusError as exc:
            if exc.status_code in (404, 410):
                out["gone"] += 1
                _record(c, now=now, db_path=db_path, jobs=None, india=None, cfg=cfg,
                        note=f"HTTP {exc.status_code} board gone")
            else:   # 5xx / 429 / 401 / 403 ...: not the board's verdict
                out["errors"] += 1
                out[_record(c, now=now, db_path=db_path, jobs=None, india=None, cfg=cfg,
                            transient=True, note=f"HTTP {exc.status_code}")] += 1
            continue
        except Exception as exc:   # noqa: BLE001 — one bad board never sinks the run
            out["errors"] += 1
            log.warning("probe %s (%s) failed: %s", c["name"], ats, exc)
            out[_record(c, now=now, db_path=db_path, jobs=None, india=None, cfg=cfg,
                        transient=True, note=f"error {type(exc).__name__}"[:120])] += 1
            continue
        # A board only counts when it has a role you could actually take: open to
        # India / worldwide-remote AND a target-role title (SDE / backend / ML / AI /
        # data ...). Optionally ignores senior+ titles, so more boards means more
        # relevant jobs, not more noise.
        india = sum(1 for j in jobs if is_india_or_remote(j) and _relevant_title(j, cfg))
        out[_record(c, now=now, db_path=db_path, jobs=len(jobs), india=india, note="", cfg=cfg)] += 1
    out["hosts"] = dict(sorted(hosts.items(), key=lambda kv: -kv[1])[:10])
    co.put_source_state(SOURCE, db_path, last_run=now, last_ok=now, last_error=None,
                        items=out["probed"], state={k: v for k, v in out.items() if k != "hosts"})
    log.info("india probe: %s", {k: v for k, v in out.items() if k != "hosts"})
    return out


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m jobcopilot.discovery.probe", description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=None, help="boards per run (default: config)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(json.dumps(run_probe(limit=args.limit), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
