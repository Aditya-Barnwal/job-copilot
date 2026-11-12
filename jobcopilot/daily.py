"""The nightly run: `python -m jobcopilot.daily` (launchd fires it at 19:00, D2).

  discovery (config sync, yc-oss, Bangalore Startup Map, seeds, tiers, ATS
  detection, India probe capped per run)  ->  ingest (boards + company ATS;
  eligibility on every job, excluded ones stored with reasons; fit scores)
  ->  evaluate (catch-up for rows without a decision)  ->  score (catch-up)
  ->  queue (today's best)  ->  tailor / fill-plan (Phase 3 / 4 hooks, no-ops).

Every step is failure-isolated and recorded in the `runs` table as it
finishes, so the app can show progress. Exit code is non-zero only when every
source failed (or the ingest step itself crashed).

  --skip-discovery   reuse the companies already discovered
  --no-network       fixtures mode: no discovery, ingest from tests/fixtures
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from . import config as _config
from . import store

log = logging.getLogger("jobcopilot.daily")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures"
DEFAULT_PROBE_LIMIT = 1000          # config/schedule.yaml: daily_probe_limit
RUN_KIND = "daily"

StepFn = Callable[[], Any]


def schedule_config() -> dict[str, Any]:
    s = _config.schedule()
    return {"daily_probe_limit": int(s.get("daily_probe_limit") or DEFAULT_PROBE_LIMIT),
            "daily_detect_limit": s.get("daily_detect_limit"), **s}


# --- steps --------------------------------------------------------------------

def discovery_steps(*, no_network: bool, db_path: Any = None) -> dict[str, StepFn]:
    """Ordered discovery sub-steps. Offline: only the local ones (config, tiers)."""
    from . import companies as co

    steps: dict[str, StepFn] = {
        "config": lambda: {"synced": co.sync_config_companies(co.default_config_paths(), db_path=db_path)},
    }
    if not no_network:
        from .discovery import detect, probe, run_bsm, run_yc, seeds
        sched = schedule_config()
        detect_limit = sched.get("daily_detect_limit") or detect.DEFAULT_LIMIT
        steps.update({
            "yc": lambda: run_yc(db_path=db_path),
            "bsm": lambda: run_bsm(db_path=db_path),
            "seeds": lambda: seeds.run_seeds(db_path=db_path),
        })
        steps["tiers"] = lambda: co.recompute_tiers(db_path=db_path)
        steps["detect"] = lambda: detect.run(db_path=db_path, limit=int(detect_limit))
        steps["probe"] = lambda: probe.run_probe(int(sched["daily_probe_limit"]), db_path=db_path)
    else:
        steps["tiers"] = lambda: co.recompute_tiers(db_path=db_path)
    return steps


def _filter_cfg() -> Any:
    """The v1 FilterConfig the pipeline still uses for role matching + jsonl."""
    from .filters import DEFAULT_TARGET_ROLES, FilterConfig
    f = _config.filters()
    roles = list((f.get("roles") or {}).get("families") or DEFAULT_TARGET_ROLES)
    inr_floor = float((f.get("salary_floors") or {}).get("INR", 1_500_000))
    usd = float((f.get("fx_to_inr") or {}).get("USD", 83.0))
    keep_unknown = bool((f.get("geo") or {}).get("keep_unknown_country", True))
    return FilterConfig(min_lpa=inr_floor / 100_000, usd_to_inr=usd, target_roles=roles,
                        keep_unknown_country=keep_unknown)


def _compact_ingest(summary: dict) -> dict:
    by_status: dict[str, int] = {}
    for o in (summary.get("sources") or {}).values():
        by_status[o.get("status", "?")] = by_status.get(o.get("status", "?"), 0) + 1
    keep = ("ingested_raw", "merged", "passed", "eligible", "excluded", "inserted", "updated",
            "expired_dropped", "duplicates_merged", "purged_expired", "evaluation", "fit")
    out = {k: summary.get(k) for k in keep}
    out["sources_by_status"] = by_status
    out["skipped"] = (summary.get("skipped") or [])[:50]
    return out


def _all_sources_failed(summary: Optional[dict]) -> bool:
    if summary is None:
        return True
    sources = summary.get("sources") or {}
    return bool(sources) and not any(o.get("status") == "ok" for o in sources.values())


# --- runner -------------------------------------------------------------------

def run(*, skip_discovery: bool = False, no_network: bool = False,
        fixtures_dir: Path | str | None = None, db_path: Any = None,
        run_id: Optional[int] = None, day: Optional[str] = None,
        discovery: Optional[dict[str, StepFn]] = None,
        ingest_kwargs: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Run every step; return {run_id, status, all_sources_failed, steps, queue}.

    `run_id` lets the API create the runs row first (to return its id at once).
    `discovery` / `ingest_kwargs` are injection points for tests.
    """
    store.init_db(db_path)
    run_id = run_id if run_id is not None else store.start_run(RUN_KIND, db_path)
    summary: dict[str, Any] = {"mode": "fixtures" if no_network else "live",
                               "skip_discovery": skip_discovery, "steps": []}
    results: dict[str, Any] = {}

    def step(name: str, fn: Optional[StepFn], *, noop: Optional[str] = None) -> Any:
        rec: dict[str, Any] = {"name": name, "started_at": time.time()}
        value = None
        if noop is not None:
            rec.update(status="skipped", result={"note": noop})
        else:
            try:
                value = fn() if fn else None
                rec.update(status="ok", result=value)
            except Exception as exc:  # noqa: BLE001 — one failing step never aborts the run
                rec.update(status="error", error=f"{type(exc).__name__}: {exc}")
                log.error("daily step=%s failed: %s\n%s", name, exc, traceback.format_exc())
        rec["seconds"] = round(time.time() - rec["started_at"], 2)
        summary["steps"].append(rec)
        results[name] = rec
        store.update_run(run_id, summary=summary, db_path=db_path)
        return value

    # 1. discovery
    if skip_discovery:
        step("discovery", None, noop="--skip-discovery")
    else:
        subs = discovery if discovery is not None else step(
            "discovery.setup", lambda: discovery_steps(no_network=no_network, db_path=db_path))
        if results.get("discovery.setup", {}).get("status") == "ok":
            results["discovery.setup"]["result"] = list(subs or {})
        for name, fn in (subs or {}).items():
            step(f"discovery.{name}", fn)

    # 2. ingest (+ eligibility on every job + fit scores for the eligible)
    from . import ingest as _ingest
    kw: dict[str, Any] = {"filter_cfg": _filter_cfg(), "db_path": db_path}
    if no_network:
        kw.update(fixtures_dir=Path(fixtures_dir or DEFAULT_FIXTURES), enrich_workday=False)
    kw.update(ingest_kwargs or {})
    ingest_summary = step("ingest", lambda: _ingest.run_pipeline(**kw))
    if results["ingest"]["status"] == "ok":
        results["ingest"]["result"] = _compact_ingest(ingest_summary or {})
        store.update_run(run_id, summary=summary, db_path=db_path)

    # 3-4. catch-up for rows the ingest didn't decide/score (legacy rows, crashes)
    from . import queue as _queue
    step("evaluate", lambda: _queue.evaluate_pending(db_path=db_path))
    step("score", lambda: _queue.score_pending(db_path=db_path))

    # 5. today's queue
    built = step("queue", lambda: _queue.build_daily_queue(day=day, db_path=db_path))
    if built:
        results["queue"]["result"] = {k: built[k] for k in ("day", "counts", "shortfall", "target")}
        store.update_run(run_id, summary=summary, db_path=db_path)

    # 6-7. Phase 3 / 4 hooks
    step("tailor", None, noop="Phase 3: tailoring not built yet")
    step("fill_plan", None, noop="Phase 4: fill planning not built yet")

    failed_all = results["ingest"]["status"] != "ok" or _all_sources_failed(ingest_summary)
    errors = [r["name"] for r in summary["steps"] if r["status"] == "error"]
    status = "failed" if failed_all else ("partial" if errors else "ok")
    summary.update(all_sources_failed=failed_all, errors=errors)
    store.update_run(run_id, status=status, summary=summary, finished=True, db_path=db_path,
                     error=("; ".join(f"{r['name']}: {r.get('error')}" for r in summary["steps"]
                                      if r["status"] == "error") or None))
    return {"run_id": run_id, "status": status, "all_sources_failed": failed_all,
            "steps": summary["steps"], "queue": built}


def print_summary(result: dict[str, Any], out: Any = None) -> None:
    out = out or sys.stdout
    print(f"\njob-copilot daily run #{result['run_id']}: {result['status']}", file=out)
    for s in result["steps"]:
        extra = s.get("error") or ""
        if s["name"] == "ingest" and s["status"] == "ok":
            r = s["result"] or {}
            extra = (f"raw={r.get('ingested_raw')} merged={r.get('merged')} eligible={r.get('eligible')} "
                     f"excluded={r.get('excluded')} sources={r.get('sources_by_status')}")
        elif s["status"] == "skipped":
            extra = (s.get("result") or {}).get("note", "")
        print(f"  {s['name']:<22}{s['status']:<9}{s['seconds']:>7.1f}s  {str(extra)[:110]}", file=out)
    q = result.get("queue")
    if q:
        line = (f"\nqueue {q['day']}: main={q['counts']['main']} extended={q['counts']['extended']} "
                f"near-miss={q['counts']['near_miss']}")
        if q["shortfall"]:
            line += f" (short of the {q['target']['min']} target by {q['shortfall']}; not padded)"
        print(line, file=out)
        for c in q["main"][:10]:
            print(f"  {c['rank']:>2}. {c['fit_score'] if c['fit_score'] is not None else '-':>5}  "
                  f"{(c['company'] or '')[:18]:<19}{(c['title'] or '')[:50]}", file=out)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobcopilot.daily", description=__doc__.splitlines()[0])
    ap.add_argument("--skip-discovery", action="store_true", help="reuse already-discovered companies")
    ap.add_argument("--no-network", action="store_true", help="fixtures mode: no network at all")
    ap.add_argument("--fixtures", default=None, help=f"fixtures dir for --no-network (default {DEFAULT_FIXTURES})")
    ap.add_argument("--day", default=None, help="queue day YYYY-MM-DD (default: today in the schedule tz)")
    ap.add_argument("--db", default=None, help="SQLite path (default: $JOBCOPILOT_DB or data/jobcopilot.db; "
                                               "--no-network defaults to data/daily-fixtures.db)")
    args = ap.parse_args(argv)

    db_path: Any = args.db
    if db_path is None and args.no_network and not os.environ.get("JOBCOPILOT_DB"):
        # Fixture jobs must never land in your real tracker DB.
        db_path = store.data_dir() / "daily-fixtures.db"
    from .ingest import setup_logging
    setup_logging(store.data_dir() / "logs")
    store.init_db(db_path)
    live = store.running_run(RUN_KIND, db_path=db_path)
    if live:
        print(f"a daily run (#{live['id']}) is already running; not starting another", file=sys.stderr)
        return 0
    if args.no_network:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")   # the embedding model loads from cache only
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        print(f"FIXTURE MODE: no network; DB = {store.resolve_db_path(db_path)}", file=sys.stderr)
    result = run(skip_discovery=args.skip_discovery, no_network=args.no_network,
                 fixtures_dir=args.fixtures, day=args.day, db_path=db_path)
    print_summary(result)
    return 1 if result["all_sources_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
