"""Phase 2 API: today's queue, all matches, excluded view, runs, fit, tracker.

POST /api/jobs/{key}/status lives here now (the tracker statuses + history);
it accepts every v1 status and body shape, so the old UI keeps working. This
router is included before routers/jobs.py, whose handler for the same path
is therefore shadowed.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException

from jobcopilot import queue as jq
from jobcopilot import store

router = APIRouter()
log = logging.getLogger("jobcopilot.server")

_LANES = ("main", "extended", "near_miss")


@router.get("/api/queue")
def get_queue(day: Optional[str] = None, lane: Optional[str] = "main"):
    if lane is not None and lane not in _LANES and lane != "all":
        raise HTTPException(status_code=400, detail="lane must be main, extended, near_miss or all")
    return jq.list_queue(day=day, lane=None if lane == "all" else lane)


@router.get("/api/matches")
def get_matches(lane: str = "main", sort: str = "fit", q: Optional[str] = None,
                location: Optional[str] = None, source: Optional[str] = None,
                tier: Optional[str] = None, limit: int = 50, offset: int = 0):
    if lane not in ("main", "extended", "all"):
        raise HTTPException(status_code=400, detail="lane must be main, extended or all")
    if sort not in ("fit", "age"):
        raise HTTPException(status_code=400, detail="sort must be fit or age")
    return jq.list_matches(lane=lane, sort=sort, q=q, location=location, source=source,
                           tier=tier, limit=limit, offset=offset)


@router.get("/api/excluded")
def get_excluded(rule: Optional[str] = None, limit: int = 100, offset: int = 0):
    return jq.list_excluded(rule=rule, limit=limit, offset=offset)


@router.get("/api/runs")
def get_runs(limit: int = 20):
    return {"runs": store.list_runs(limit=max(1, min(int(limit), 200))),
            "running": store.running_run("daily")}


@router.get("/api/runs/{run_id}")
def get_run(run_id: int):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


def _run_daily(run_id: int, skip_discovery: bool) -> None:
    from jobcopilot import daily
    try:
        daily.run(run_id=run_id, skip_discovery=skip_discovery)
    except Exception as exc:  # noqa: BLE001 — the run row must not stay 'running'
        log.exception("daily run %s crashed", run_id)
        store.update_run(run_id, status="failed", error=f"{type(exc).__name__}: {exc}", finished=True)


@router.post("/api/runs/daily")
def start_daily_run(body: Optional[dict] = Body(default=None)):
    """'Run now': start the nightly pipeline in a background thread; 409 if one is running."""
    body = body or {}
    run_id, live = store.claim_run("daily")     # atomic; shared with the CLI / LaunchAgent
    if run_id is None:
        raise HTTPException(status_code=409, detail={"error": "a daily run is already running",
                                                     "run_id": live["id"]})
    t = threading.Thread(target=_run_daily, args=(run_id, bool(body.get("skip_discovery"))),
                         name=f"daily-run-{run_id}", daemon=True)
    t.start()
    return {"ok": True, "run_id": run_id}


@router.get("/api/jobs/{key}/fit")
def get_fit(key: str):
    fit = jq.job_fit(key)
    if fit is None:
        raise HTTPException(status_code=404, detail="not found")
    return fit


@router.get("/api/jobs/{key}/history")
def get_status_history(key: str):
    if not store.get_job(key):
        raise HTTPException(status_code=404, detail="not found")
    return {"key": key, "history": store.status_history(key)}


MAX_NOTE_LEN = 2000


def _proof_path(raw: Any) -> Optional[str]:
    """A submission proof must live under <data dir>/proofs; stored relative to it."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or "\x00" in raw:
        raise HTTPException(status_code=400, detail="proof_path must be a string")
    root = (store.data_dir() / "proofs").resolve()
    p = Path(raw).expanduser()
    resolved = (p if p.is_absolute() else root / p).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise HTTPException(status_code=400, detail="proof_path must be inside the data dir's proofs/ folder")
    return resolved.relative_to(root).as_posix()


@router.post("/api/jobs/{key}/status")
def set_status(key: str, body: dict = Body(...)):
    """{status, source?: user|detected|system, note?, proof_path?} — also the v1
    body {status, notes?}. Every change is kept in status_history; undo = post
    the earlier status."""
    status = body.get("status")
    if status not in store.ALL_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    source = body.get("source") or "user"
    if source not in store.STATUS_SOURCES:
        raise HTTPException(status_code=400, detail="invalid source")
    for field in ("notes", "note"):
        v = body.get(field)
        if v is not None and not isinstance(v, str):
            raise HTTPException(status_code=400, detail=f"{field} must be a string")
        if isinstance(v, str) and len(v) > MAX_NOTE_LEN:
            raise HTTPException(status_code=400, detail=f"{field} is longer than {MAX_NOTE_LEN} characters")
    proof = _proof_path(body.get("proof_path"))
    ok = store.set_status(key, status, notes=body.get("notes"), source=source,
                          note=body.get("note"), proof_path=proof)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "status": status}


@router.get("/api/tracker")
def get_tracker(limit: int = 200):
    return jq.tracker(limit_per_column=max(1, min(int(limit), 1000)))
