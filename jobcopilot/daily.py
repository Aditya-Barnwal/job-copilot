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
    """The FilterConfig from config/filters.yaml (shared with POST /api/ingest)."""
    from .ingest import default_filter_cfg
    return default_filter_cfg()


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
    try:
        return _run_steps(run_id, skip_discovery=skip_discovery, no_network=no_network,
                          fixtures_dir=fixtures_dir, db_path=db_path, day=day,
                          discovery=discovery, ingest_kwargs=ingest_kwargs)
    except BaseException as exc:   # incl. KeyboardInterrupt / SystemExit: never leave 'running'
        log.error("daily run %s aborted: %s\n%s", run_id, exc, traceback.format_exc())
        try:
            store.update_run(run_id, status="failed", finished=True, db_path=db_path,
                             error=f"aborted: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001 — the original error matters more
            log.exception("could not mark run %s failed", run_id)
        raise


def _run_steps(run_id: int, *, skip_discovery: bool, no_network: bool,
               fixtures_dir: Path | str | None, db_path: Any, day: Optional[str],
               discovery: Optional[dict[str, StepFn]],
               ingest_kwargs: Optional[dict[str, Any]]) -> dict[str, Any]:
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

    def do_ingest() -> Any:
        # config is read inside the step: a broken filters.yaml fails this step, not the run
        kw: dict[str, Any] = {"filter_cfg": _filter_cfg(), "db_path": db_path}
        if no_network:
            kw.update(fixtures_dir=Path(fixtures_dir or DEFAULT_FIXTURES), enrich_workday=False)
        kw.update(ingest_kwargs or {})
        return _ingest.run_pipeline(**kw)
    ingest_summary = step("ingest", do_ingest)
    if results["ingest"]["status"] == "ok":
        results["ingest"]["result"] = _compact_ingest(ingest_summary or {})
        store.update_run(run_id, summary=summary, db_path=db_path)

    # 3-4. catch-up for rows the ingest didn't decide/score (legacy rows, crashes)
    from . import queue as _queue
    # postings every source stopped listing leave the queue (also done by the ingest;
    # repeated here so a failed ingest still retires against earlier runs)
    step("retire", lambda: store.retire_unlisted(
        float(_queue.queue_config()["gone_after_days"]), db_path=db_path))
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
                                               "--no-network: <data dir>/daily-fixtures.db)")
    ap.add_argument("--scheduled", action="store_true",
                    help="launched by the LaunchAgent: run only inside the daily slot, once a day")
    args = ap.parse_args(argv)

    db_path: Any = args.db
    if args.no_network:
        # Fixture jobs must never land in your real tracker DB: ignore $JOBCOPILOT_DB.
        db_path = Path(args.db) if args.db else store.data_dir() / "daily-fixtures.db"
        bad = _real_db_paths()
        if _same_path(db_path, bad):
            print(f"refusing --no-network: {db_path} is a real tracker DB; pass --db <scratch path>",
                  file=sys.stderr)
            return 2
    from .ingest import setup_logging
    setup_logging(store.data_dir() / "logs")
    store.init_db(db_path)
    if args.scheduled:
        why = scheduled_skip_reason(db_path=db_path)
        if why:
            log.info("scheduled daily run skipped: %s", why)
            print(f"scheduled run skipped: {why}", file=sys.stderr)
            return 0
    run_id, live = store.claim_run(RUN_KIND, db_path=db_path)
    if run_id is None:
        print(f"a daily run (#{live['id']}) is already running; not starting another", file=sys.stderr)
        return 0
    if args.no_network:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")   # the embedding model loads from cache only
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        print(f"FIXTURE MODE: no network; DB = {store.resolve_db_path(db_path)}", file=sys.stderr)
    result = run(skip_discovery=args.skip_discovery, no_network=args.no_network,
                 fixtures_dir=args.fixtures, day=args.day, db_path=db_path, run_id=run_id)
    print_summary(result)
    return 1 if result["all_sources_failed"] else 0


def _real_db_paths() -> list[Path]:
    """Every path that may be the user's real tracker DB."""
    out = [store._REPO_DATA_DIR / "jobcopilot.db", store.resolve_db_path()]
    env = os.environ.get("JOBCOPILOT_DB")
    if env:
        out.append(Path(env))
    return out


def _same_path(p: Any, others: list[Path]) -> bool:
    mine = Path(p).expanduser().resolve()
    return any(mine == Path(o).expanduser().resolve() for o in others)


# --- the scheduled slot (P8) ---------------------------------------------------

def _schedule_tz() -> Any:
    name = schedule_config().get("timezone") or "Asia/Kolkata"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return None


def slot_bounds(now: Optional[float] = None) -> tuple[float, float]:
    """(start, end) epoch seconds of today's run slot in the schedule tz:
    daily_run (HH:MM) for run_window_minutes (default 60)."""
    sched = schedule_config()
    h, _, m = str(sched.get("daily_run") or "19:00").partition(":")
    window = float(sched.get("run_window_minutes") or 60) * 60
    tz = _schedule_tz()
    import datetime as _dt
    local = _dt.datetime.fromtimestamp(time.time() if now is None else now, tz)
    start = local.replace(hour=int(h), minute=int(m or 0), second=0, microsecond=0)
    return start.timestamp(), start.timestamp() + window


def scheduled_skip_reason(now: Optional[float] = None, db_path: Any = None) -> Optional[str]:
    """Why a LaunchAgent firing should NOT run now (None = run). launchd fires in
    local time and the plist holds every local hour the slot can map to (DST),
    so this check keeps exactly one run per day, inside the slot."""
    now = time.time() if now is None else now
    start, end = slot_bounds(now)
    if not (start <= now < end):
        import datetime as _dt
        tz = _schedule_tz()
        fmt = lambda t: _dt.datetime.fromtimestamp(t, tz).strftime("%H:%M")  # noqa: E731
        return f"outside the daily slot {fmt(start)}-{fmt(end)} ({getattr(tz, 'key', 'local')})"
    for r in store.list_runs(kind=RUN_KIND, limit=20, db_path=db_path):
        if r["started_at"] >= start and r["status"] in ("running", "ok", "partial"):
            return f"run #{r['id']} already ran in today's slot ({r['status']})"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
