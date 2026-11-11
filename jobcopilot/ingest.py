"""Ingestion orchestrator + CLI.

One pipeline for the CLI, `make smoke-live` and POST /api/ingest:

  roster (companies.yaml + target_companies.yaml + discovered ATS companies)
  + enabled job boards  ->  per-source fetch (failure-isolated, health recorded)
  ->  drop expired listings  ->  dedupe.merge()  ->  apply_filters()  ->  store.

A failing source logs an error and never aborts the run (D7); robots.txt
refusals, do-not-automate hosts and missing connectors are reported apart from
real errors so the summary says *why* nothing came back.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

import yaml

from . import config as _config
from .connectors import REGISTRY
from .http import HostDenied, RobotsDisallowed, RobotsUnavailable
from .filters import (
    FilterConfig,
    DEFAULT_TARGET_ROLES,
    apply_filters,
    FilterResult,
    passes_role,
)
from .models import Job
from .normalize import freshness_bucket, normalize_company_name, posted_age_days


log = logging.getLogger("jobcopilot.ingest")


def setup_logging(log_dir: Path) -> None:
    """Attach FileHandler + StreamHandler to the ROOT jobcopilot logger.

    All descendant loggers (`jobcopilot.scoring`, `.tailor`, `.llm`, `.email`,
    etc.) inherit these handlers via propagation. Idempotent."""
    root = logging.getLogger("jobcopilot")
    if root.handlers:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        log_dir / "jobcopilot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)
    root.addHandler(sh)

    root.setLevel(logging.INFO)
    # Don't double-log via the plain root logger.
    root.propagate = False
    root.info("logging initialized · file=%s", (log_dir / "jobcopilot.log").resolve())


def load_companies(path: str | Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("companies", [])


def _load_fixture(fixtures_dir: Path, source: str) -> Any:
    fixture_path = fixtures_dir / f"{source}.json"
    with open(fixture_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def enrich_workday_descriptions(
    results: list[FilterResult],
    companies: list[dict[str, Any]],
    *,
    delay: float | None = None,
    max_per_company: int | None = None,
    db_path: Any = None,
) -> list[str]:
    """For Workday jobs that passed filters and have no description, fetch
    detail per posting. Failure-isolated per posting; cached in DB. Returns
    list of job keys whose description was newly filled."""
    from .connectors.workday import WorkdayConnector
    from . import store as _store

    # The shared HTTP client already spaces requests per host (config/sources.yaml,
    # default 1 req/s), so the extra sleep is off by default — set
    # WORKDAY_DETAIL_DELAY_SEC to add more padding on top of it.
    delay = float(delay if delay is not None
                  else os.environ.get("WORKDAY_DETAIL_DELAY_SEC", "0"))
    cap = int(max_per_company if max_per_company is not None
              else os.environ.get("WORKDAY_DETAIL_MAX", "60"))

    by_company = {c["name"]: c for c in companies}
    connector = WorkdayConnector()
    filled: list[str] = []
    per_company_count: dict[str, int] = {}

    for r in results:
        j = r.job
        if j.source != "workday" or (j.description_text or "").strip():
            continue
        company_cfg = by_company.get(j.company)
        if not company_cfg:
            continue

        # cache: skip if DB already has a non-empty description for this key
        existing = _store.get_job(j.key, db_path=db_path) if db_path is not None else _store.get_job(j.key)
        if existing and (existing.get("description_text") or "").strip():
            j.description_text = existing["description_text"]
            continue

        n = per_company_count.get(j.company, 0)
        if n >= cap:
            continue

        try:
            text = connector.fetch_detail(j, company_cfg)
        except Exception as exc:  # noqa: BLE001 — fail-isolate per posting
            log.warning("workday detail skip key=%s exc=%s", j.key, exc)
            time.sleep(delay)
            per_company_count[j.company] = n + 1
            continue

        per_company_count[j.company] = n + 1
        time.sleep(delay)

        if text:
            j.description_text = text
            filled.append(j.key)

    if filled:
        log.info(
            "workday details filled=%d per_company=%s",
            len(filled), per_company_count,
        )
    return filled


def _annotate_age(jobs: list[Job]) -> None:
    for j in jobs:
        age = posted_age_days(j.posted_at, j.source)
        j.posted_age_days = age
        j.freshness = freshness_bucket(age)


@dataclass
class SourceOutcome:
    """What one company board or job board gave us this run, and why."""
    source: str            # source_state key: "<connector>:<normalised company>" or the board name
    kind: str              # "company" | "board"
    name: str              # display name
    connector: str
    status: str            # ok | manual | robots_disallowed | robots_unavailable | denied_host | unavailable | error
    items: int = 0
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _company_state_key(company: dict[str, Any]) -> str:
    return f"{company.get('connector')}:{normalize_company_name(company.get('name'))}"


def _run_source(label: str, name: str, connector_name: str, source_key: str,
                call: Callable[[], list[Job]], outcomes: list[SourceOutcome]) -> list[Job]:
    """Run one fetch; turn every failure into a reported outcome, never a raise.

    `label` is "company" or "board" — it prefixes the log line so the log
    format Phase 0 tests rely on ("company=<name> ... reason=...") is kept.
    """
    kind = label

    def out(status: str, reason: str = "", items: int = 0) -> None:
        outcomes.append(SourceOutcome(source_key, kind, name, connector_name, status, items, reason))

    try:
        return_jobs = call()
    # Nothing was sent in any of the next three cases; the run carries on (D7).
    except HostDenied as exc:
        # Config points at a DO-NOT-AUTOMATE host: link out to it instead.
        print(f"  · denied {name:<16} (do-not-automate host; {exc})", file=sys.stderr)
        log.warning("skip %s=%s connector=%s reason=denied_host exc=%s", label, name, connector_name, exc)
        out("denied_host", str(exc))
        return []
    except RobotsUnavailable as exc:
        # robots.txt couldn't be read, so the host is treated as disallowed. That is a
        # failure to report, not the site saying no.
        when = ("will retry in a few minutes" if exc.transient
                else "will retry when the robots cache expires")
        print(f"  · error  {name:<16} (robots.txt unavailable, host treated as "
              f"disallowed; {when}: {exc})", file=sys.stderr)
        log.error("error %s=%s connector=%s reason=robots_unavailable transient=%s exc=%s",
                  label, name, connector_name, exc.transient, exc)
        out("robots_unavailable", f"{exc} ({when})")
        return []
    except RobotsDisallowed as exc:
        # Not a failure: the site asked crawlers not to fetch this, so we don't.
        print(f"  · skip   {name:<16} ({exc})", file=sys.stderr)
        log.info("skip %s=%s connector=%s reason=robots_disallowed exc=%s", label, name, connector_name, exc)
        out("robots_disallowed", str(exc))
        return []
    except Exception as exc:  # noqa: BLE001 — one bad source never aborts the run
        print(f"  · error  {name:<16} ({type(exc).__name__}: {exc})", file=sys.stderr)
        log.error("error %s=%s connector=%s exc=%s", label, name, connector_name, exc)
        out("error", f"{type(exc).__name__}: {exc}")
        return []
    jobs = list(return_jobs or [])
    out("ok", items=len(jobs))
    return jobs


def ingest(
    companies: list[dict[str, Any]],
    fixtures_dir: Path | None = None,
    outcomes: Optional[list[SourceOutcome]] = None,
) -> list[Job]:
    """Fetch every company board in `companies`; one failing board never aborts.

    `outcomes`, when given, receives one SourceOutcome per company.
    """
    outcomes = outcomes if outcomes is not None else []
    seen: set[str] = set()
    jobs: list[Job] = []

    for company in companies:
        name = company.get("name", "?")
        connector_name = company.get("connector")
        if connector_name in (None, "manual", "none"):
            note = company.get("note", "no public ATS feed — handle by hand")
            print(f"  · skip   {name:<16} ({note})", file=sys.stderr)
            log.info("skip company=%s reason=%s", name, note)
            outcomes.append(SourceOutcome(_company_state_key(company), "company", name,
                                          str(connector_name or "manual"), "manual", 0, note))
            continue

        connector_cls = REGISTRY.get(connector_name)
        if connector_cls is None:
            msg = f"unknown connector '{connector_name}'"
            print(f"  · skip   {name:<16} ({msg})", file=sys.stderr)
            log.warning("skip company=%s reason=%s", name, msg)
            outcomes.append(SourceOutcome(_company_state_key(company), "company", name,
                                          connector_name, "unavailable", 0, msg))
            continue

        connector = connector_cls()

        def call(connector=connector, company=company) -> list[Job]:
            if fixtures_dir is not None:
                return connector.parse(_load_fixture(fixtures_dir, connector.source), company)
            return connector.fetch(company)

        company_jobs = _run_source("company", name, connector_name, _company_state_key(company),
                                   call, outcomes)
        if outcomes[-1].status != "ok":
            continue

        added = 0
        for job in company_jobs:
            if job.key in seen:
                continue
            seen.add(job.key)
            jobs.append(job)
            added += 1
        outcomes[-1].items = added
        print(f"  · ok     {name:<16} {added:>4} roles via {connector.source}", file=sys.stderr)
        log.info("ingest company=%s connector=%s raw=%d added=%d",
                 name, connector.source, len(company_jobs), added)

    _annotate_age(jobs)
    return jobs


def _board_fixture(fixtures_dir: Path, name: str) -> Optional[Path]:
    """tests/fixtures/<board>.json, or .xml for the RSS boards."""
    for ext in (".json", ".xml"):
        p = Path(fixtures_dir) / f"{name}{ext}"
        if p.exists():
            return p
    return None


def _read_fixture(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else text


def ingest_boards(
    boards: dict[str, Any],
    fixtures_dir: Path | None = None,
    outcomes: Optional[list[SourceOutcome]] = None,
) -> list[Job]:
    """Fetch every enabled board in config.sources()["boards"].

    A board whose connector isn't installed yet is reported as `unavailable`;
    in fixture mode a board without tests/fixtures/<board>.json is skipped.
    """
    outcomes = outcomes if outcomes is not None else []
    jobs: list[Job] = []
    for name, cfg in (boards or {}).items():
        cfg = cfg or {}
        if not cfg.get("enabled", False):
            continue
        connector_cls = REGISTRY.get(name)
        if connector_cls is None:
            msg = f"no connector registered for board '{name}'"
            log.warning("skip board=%s reason=%s", name, msg)
            outcomes.append(SourceOutcome(name, "board", name, name, "unavailable", 0, msg))
            continue
        fixture = _board_fixture(fixtures_dir, name) if fixtures_dir is not None else None
        if fixtures_dir is not None and fixture is None:
            outcomes.append(SourceOutcome(name, "board", name, name, "unavailable", 0, "no fixture"))
            continue
        connector = connector_cls()

        def call(connector=connector, cfg=cfg, fixture=fixture) -> list[Job]:
            if fixture is not None:
                return connector.parse(_read_fixture(fixture), cfg)
            return connector.fetch(cfg)

        got = _run_source("board", name, name, name, call, outcomes)
        if outcomes[-1].status == "ok":
            print(f"  · ok     {name:<16} {len(got):>4} roles (board)", file=sys.stderr)
            log.info("ingest board=%s raw=%d", name, len(got))
        jobs.extend(got)
    _annotate_age(jobs)
    return jobs


# --- roster ------------------------------------------------------------------

def _is_manual(company: dict[str, Any]) -> bool:
    return company.get("connector") in (None, "manual", "none")


def company_roster(
    companies_path: str | Path | None = None,
    targets_path: str | Path | None = None,
    *,
    include_discovered: bool = True,
    db_path: Any = None,
) -> list[dict[str, Any]]:
    """companies.yaml + target_companies.yaml + discovered ATS companies.

    De-duplicated by normalised name: the first entry wins, except that a
    later entry with a real connector replaces an earlier `manual` one (a
    target file that found the board beats "handle by hand").
    """
    roster: list[dict[str, Any]] = []
    index: dict[str, int] = {}

    def add(c: dict[str, Any]) -> None:
        key = normalize_company_name(c.get("name"))
        if not key:
            return
        if key not in index:
            index[key] = len(roster)
            roster.append(c)
        elif _is_manual(roster[index[key]]) and not _is_manual(c):
            roster[index[key]] = c

    paths = [Path(companies_path) if companies_path else _config.companies_path()]
    paths.append(Path(targets_path) if targets_path else _config.config_dir() / "target_companies.yaml")
    for p in paths:
        if p.exists():
            for c in load_companies(p):
                add(c)
    if include_discovered:
        try:
            from . import companies as _companies
            discovered = _companies.enabled_ats_companies(db_path=db_path)
        except Exception as exc:  # noqa: BLE001 — module may not exist yet / DB not seeded
            log.info("discovered companies unavailable (%s: %s)", type(exc).__name__, exc)
            discovered = []
        for c in discovered:
            if normalize_company_name(c.get("name")) not in index:
                add(c)
    return roster


# --- pipeline ------------------------------------------------------------------

@dataclass
class IngestRun:
    roster: list[dict[str, Any]]
    raw_count: int
    merged: list[Job]
    results: list[FilterResult]
    outcomes: list[SourceOutcome] = field(default_factory=list)
    expired_dropped: int = 0


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _drop_expired(jobs: list[Job], today: str) -> tuple[list[Job], int]:
    keep = [j for j in jobs if not (j.expires_at and str(j.expires_at)[:10] < today)]
    return keep, len(jobs) - len(keep)


def _link_companies(jobs: list[Job], db_path: Any = None) -> None:
    """Set job.company_id from the companies table when that module exists."""
    try:
        from . import companies as _companies
    except Exception as exc:  # noqa: BLE001
        log.debug("companies module unavailable: %s", exc)
        return
    cache: dict[str, Optional[int]] = {}
    for j in jobs:
        if j.company not in cache:
            try:
                row = _companies.get_company(j.company, db_path=db_path)
            except Exception as exc:  # noqa: BLE001
                log.warning("company lookup failed company=%s exc=%s", j.company, exc)
                row = None
            cache[j.company] = (row or {}).get("id")
        if cache[j.company] is not None:
            j.company_id = cache[j.company]  # type: ignore[attr-defined]


def _record_health(outcomes: list[SourceOutcome], db_path: Any = None) -> None:
    from . import store as _store

    now = time.time()
    for o in outcomes:
        if o.status == "manual":
            continue
        fields: dict[str, Any] = {"last_run": now, "items": o.items}
        if o.status == "ok":
            fields.update(last_ok=now, last_error=None)
        else:
            fields["last_error"] = f"{o.status}: {o.reason}"[:500]
        try:
            _store.set_source_state(o.source, db_path=db_path, **fields)
        except Exception as exc:  # noqa: BLE001 — health is best-effort
            log.warning("source_state write failed source=%s exc=%s", o.source, exc)


def collect_and_filter(
    *,
    companies: Optional[list[dict[str, Any]]] = None,
    companies_path: str | Path | None = None,
    targets_path: str | Path | None = None,
    include_discovered: Optional[bool] = None,
    include_boards: bool = True,
    boards: Optional[dict[str, Any]] = None,
    filter_cfg: Optional[FilterConfig] = None,
    fixtures_dir: Path | None = None,
    record_health: Optional[bool] = None,
    db_path: Any = None,
) -> IngestRun:
    """Fetch every source, drop expired listings, merge duplicates, filter.

    Fixture runs default to no discovered companies and no health writes, so
    they stay deterministic and never touch live state.
    """
    offline = fixtures_dir is not None
    include_discovered = (not offline) if include_discovered is None else include_discovered
    record_health = (not offline) if record_health is None else record_health

    roster = companies if companies is not None else company_roster(
        companies_path, targets_path, include_discovered=include_discovered, db_path=db_path)
    outcomes: list[SourceOutcome] = []
    jobs = ingest(roster, fixtures_dir=fixtures_dir, outcomes=outcomes)
    if include_boards:
        board_cfg = boards if boards is not None else (_config.sources().get("boards") or {})
        jobs += ingest_boards(board_cfg, fixtures_dir=fixtures_dir, outcomes=outcomes)
    if record_health:
        _record_health(outcomes, db_path)

    raw_count = len(jobs)
    jobs, expired = _drop_expired(jobs, _today())
    _link_companies(jobs, db_path)

    from .dedupe import merge
    merged = merge(jobs)
    _annotate_age(merged)
    results = apply_filters(merged, filter_cfg or FilterConfig())
    log.info("pipeline raw=%d expired=%d merged=%d passed=%d",
             raw_count, expired, len(merged), len(results))
    return IngestRun(roster, raw_count, merged, results, outcomes, expired)


def persist(run: IngestRun, *, enrich_workday: bool = True, rescore: bool = True,
            db_path: Any = None) -> dict[str, Any]:
    """Workday detail fill, upsert, expired purge, rescoring of filled jobs."""
    from . import store as _store

    filled = (enrich_workday_descriptions(run.results, run.roster, db_path=db_path)
              if enrich_workday else [])
    inserted, updated = _store.upsert_jobs(run.results, db_path=db_path)
    purged = _store.purge_expired(_today(), db_path=db_path)
    _store.set_meta("last_ingest", str(time.time()), db_path)

    rescored = 0
    if rescore and filled and _store.get_resume(db_path):
        try:
            from . import scoring
            rescored = scoring.score_keys(filled).get("scored", 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("rescore after workday fill failed: %s", exc)
    return {"inserted": inserted, "updated": updated, "workday_details_filled": len(filled),
            "rescored": rescored, "purged_expired": purged}


_PROBLEM_STATUSES = {"robots_disallowed", "robots_unavailable", "denied_host", "unavailable", "error"}


def summarize(run: IngestRun, persisted: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The API/CLI summary. Phase 0 keys first (the UI reads them)."""
    persisted = persisted or {}
    by_connector: dict[str, int] = {}
    for o in run.outcomes:
        if o.status == "ok":
            by_connector[o.connector] = by_connector.get(o.connector, 0) + o.items
    return {
        "ingested_raw": run.raw_count,
        "passed": len(run.results),
        "inserted": persisted.get("inserted", 0),
        "updated": persisted.get("updated", 0),
        "workday_details_filled": persisted.get("workday_details_filled", 0),
        "rescored": persisted.get("rescored", 0),
        "merged": len(run.merged),
        "duplicates_merged": run.raw_count - run.expired_dropped - len(run.merged),
        "expired_dropped": run.expired_dropped,
        "purged_expired": persisted.get("purged_expired", 0),
        "by_connector": by_connector,
        "sources": {o.source: {k: v for k, v in o.as_dict().items() if k != "source"}
                    for o in run.outcomes if o.status != "manual"},
        "skipped": [{"source": o.source, "name": o.name, "kind": o.kind, "status": o.status,
                     "reason": o.reason} for o in run.outcomes if o.status in _PROBLEM_STATUSES],
        "manual": [o.name for o in run.outcomes if o.status == "manual"],
    }


def run_pipeline(*, enrich_workday: bool = True, rescore: bool = True, store_results: bool = True,
                 db_path: Any = None, **collect_kwargs: Any) -> dict[str, Any]:
    """collect_and_filter() + persist() + summarize(): what /api/ingest runs."""
    run = collect_and_filter(db_path=db_path, **collect_kwargs)
    persisted = (persist(run, enrich_workday=enrich_workday, rescore=rescore, db_path=db_path)
                 if store_results else None)
    return summarize(run, persisted)


def print_source_table(summary: dict[str, Any], out: Any = None) -> None:
    """Per-source counts, problems last — what `make smoke-live` shows."""
    out = out or sys.stdout
    rows = sorted(summary["sources"].items(), key=lambda kv: (kv[1]["status"] != "ok", kv[0]))
    print(f"\n{'SOURCE':<36}{'KIND':<9}{'STATUS':<20}{'ITEMS':>6}  REASON", file=out)
    for key, o in rows:
        print(f"{key[:35]:<36}{o['kind']:<9}{o['status']:<20}{o['items']:>6}  {o['reason'][:60]}", file=out)
    print(f"\nby connector: {json.dumps(summary['by_connector'], sort_keys=True)}", file=out)
    print(f"raw={summary['ingested_raw']} merged={summary['merged']} "
          f"duplicates={summary['duplicates_merged']} expired={summary['expired_dropped']} "
          f"passed={summary['passed']} problems={len(summary['skipped'])} "
          f"manual={len(summary['manual'])}", file=out)


def write_jsonl(results: list[FilterResult], out_path: str | Path) -> None:
    with open(out_path, "w", encoding="utf-8") as fh:
        for r in results:
            row = r.job.to_dict()
            row["package_estimated"] = r.package_estimated
            row["country_uncertain"] = r.country_uncertain
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def print_summary(results: list[FilterResult], total_ingested: int, role_kept: int) -> None:
    print()
    print(
        f"Ingested {total_ingested} roles · "
        f"{role_kept} matched target roles · "
        f"{len(results)} passed all filters"
    )
    print("-" * 90)
    print(f"{'COMPANY':<16}{'WORK':<8}{'GEO':<10}{'PAY':<14}{'AGE':<10}{'TITLE'}")
    print("-" * 90)
    for r in results[:40]:
        j = r.job
        pay = "—"
        if j.compensation and j.compensation.annual_inr() is not None:
            pay = f"~{j.compensation.annual_inr() / 100_000:.0f} LPA"
        elif r.package_estimated:
            pay = "est."
        geo = j.country_bucket + ("?" if r.country_uncertain else "")
        title = (j.title[:30] + "…") if len(j.title) > 31 else j.title
        age = f"{j.posted_age_days}d" if j.posted_age_days is not None else "?"
        age_label = f"{age}/{j.freshness}"
        print(f"{j.company[:15]:<16}{j.workplace_type:<8}{geo:<10}{pay:<14}{age_label:<10}{title}")
    if len(results) > 40:
        print(f"... and {len(results) - 40} more (see the jsonl output)")


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Job-copilot ingestion")
    parser.add_argument("--companies", default=str(_config.companies_path()))
    parser.add_argument("--targets", default=None,
                        help="extra company list (default: config/target_companies.yaml if present)")
    parser.add_argument("--fixtures", default=None)
    parser.add_argument("--out", default=str(here / "jobs.jsonl"))
    parser.add_argument("--min-lpa", type=float, default=15.0)
    parser.add_argument("--sectors", default=None)
    parser.add_argument("--usd-inr", type=float, default=83.0)
    parser.add_argument(
        "--roles",
        default=None,
        help="comma-separated target role phrases; default = built-in list",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help="opt-in: drop postings older than this many days (default: keep all)",
    )
    parser.add_argument("--no-boards", action="store_true", help="skip job boards")
    parser.add_argument("--no-discovered", action="store_true",
                        help="skip companies found by discovery (companies table)")
    parser.add_argument("--store", action="store_true",
                        help="also upsert into the DB, as the UI's ingest button does")
    parser.add_argument("--smoke", action="store_true",
                        help="print per-source counts only: no jsonl, no DB writes")
    args = parser.parse_args(argv)

    setup_logging(here / "data")
    fixtures_dir = Path(args.fixtures) if args.fixtures else None
    if fixtures_dir:
        print(f"FIXTURE MODE — reading {fixtures_dir} (no network)", file=sys.stderr)

    target_roles = (
        [r.strip() for r in args.roles.split(",") if r.strip()]
        if args.roles else list(DEFAULT_TARGET_ROLES)
    )
    sectors = set(s.strip() for s in args.sectors.split(",")) if args.sectors else None
    filter_cfg = FilterConfig(
        min_lpa=args.min_lpa,
        sectors=sectors,
        usd_to_inr=args.usd_inr,
        target_roles=target_roles,
        max_age_days=args.max_age_days,
    )

    run = collect_and_filter(
        companies_path=args.companies,
        targets_path=args.targets,
        include_discovered=False if args.no_discovered else None,
        include_boards=not args.no_boards,
        filter_cfg=filter_cfg,
        fixtures_dir=fixtures_dir,
        record_health=False if args.smoke else None,
    )
    print(f"Loaded {len(run.roster)} companies", file=sys.stderr)
    persisted = persist(run) if (args.store and not args.smoke and fixtures_dir is None) else None
    summary = summarize(run, persisted)

    if args.smoke:
        print_source_table(summary)
        return 0

    role_kept = sum(1 for j in run.merged if passes_role(j.title, target_roles))
    log.info("totals raw=%d merged=%d role_matched=%d (pre-filter visibility)",
             run.raw_count, len(run.merged), role_kept)
    write_jsonl(run.results, args.out)
    print_summary(run.results, total_ingested=len(run.merged), role_kept=role_kept)
    print_source_table(summary, out=sys.stderr)
    if persisted:
        print(f"DB: inserted={persisted['inserted']} updated={persisted['updated']}", file=sys.stderr)
    print(f"\nWrote {len(run.results)} roles to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
