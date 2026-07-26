"""Ingestion orchestrator + CLI."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .connectors import REGISTRY
from .filters import (
    FilterConfig,
    DEFAULT_TARGET_ROLES,
    apply_filters,
    FilterResult,
    passes_role,
)
from .models import Job
from .normalize import posted_age_days, freshness_bucket


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

    delay = float(delay if delay is not None
                  else os.environ.get("WORKDAY_DETAIL_DELAY_SEC", "0.7"))
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


def ingest(
    companies: list[dict[str, Any]],
    fixtures_dir: Path | None = None,
) -> list[Job]:
    seen: set[str] = set()
    jobs: list[Job] = []

    for company in companies:
        connector_name = company.get("connector")
        if connector_name in (None, "manual", "none"):
            note = company.get("note", "no public ATS feed — handle by hand")
            print(f"  · skip   {company['name']:<16} ({note})", file=sys.stderr)
            log.info("skip company=%s reason=%s", company.get("name"), note)
            continue

        connector_cls = REGISTRY.get(connector_name)
        if connector_cls is None:
            msg = f"unknown connector '{connector_name}'"
            print(f"  · skip   {company['name']:<16} ({msg})", file=sys.stderr)
            log.warning("skip company=%s reason=%s", company.get("name"), msg)
            continue

        connector = connector_cls()
        try:
            if fixtures_dir is not None:
                raw = _load_fixture(fixtures_dir, connector.source)
                company_jobs = connector.parse(raw, company)
            else:
                company_jobs = connector.fetch(company)
        except Exception as exc:  # noqa: BLE001
            print(f"  · error  {company['name']:<16} ({type(exc).__name__}: {exc})", file=sys.stderr)
            log.error("error company=%s connector=%s exc=%s", company.get("name"), connector_name, exc)
            continue

        added = 0
        for job in company_jobs:
            if job.key in seen:
                continue
            seen.add(job.key)
            jobs.append(job)
            added += 1
        print(
            f"  · ok     {company['name']:<16} {added:>4} roles via {connector.source}",
            file=sys.stderr,
        )
        log.info(
            "ingest company=%s connector=%s raw=%d added=%d",
            company.get("name"), connector.source, len(company_jobs), added,
        )

    _annotate_age(jobs)
    return jobs


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
    parser.add_argument("--companies", default=str(here / "companies.yaml"))
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
    args = parser.parse_args(argv)

    setup_logging(here / "data")

    companies = load_companies(args.companies)
    fixtures_dir = Path(args.fixtures) if args.fixtures else None

    print(f"Loading {len(companies)} companies from {args.companies}", file=sys.stderr)
    if fixtures_dir:
        print(f"FIXTURE MODE — reading {fixtures_dir} (no network)", file=sys.stderr)

    jobs = ingest(companies, fixtures_dir=fixtures_dir)

    target_roles = (
        [r.strip() for r in args.roles.split(",") if r.strip()]
        if args.roles else list(DEFAULT_TARGET_ROLES)
    )
    sectors = set(s.strip() for s in args.sectors.split(",")) if args.sectors else None
    config = FilterConfig(
        min_lpa=args.min_lpa,
        sectors=sectors,
        usd_to_inr=args.usd_inr,
        target_roles=target_roles,
        max_age_days=args.max_age_days,
    )

    role_kept = sum(1 for j in jobs if passes_role(j.title, target_roles))
    log.info(
        "totals raw=%d role_matched=%d (pre-filter visibility)",
        len(jobs), role_kept,
    )

    results = apply_filters(jobs, config)
    log.info("filter passed=%d", len(results))

    write_jsonl(results, args.out)
    print_summary(results, total_ingested=len(jobs), role_kept=role_kept)
    print(f"\nWrote {len(results)} roles to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
