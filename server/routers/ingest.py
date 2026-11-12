"""Pipeline triggers: ingest, scoring, funding discovery, source health."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, HTTPException

import dataclasses

from jobcopilot import scoring, store
from jobcopilot.ingest import default_filter_cfg, run_pipeline

from ..deps import ROOT

router = APIRouter()


@router.post("/api/ingest")
def run_ingest(body: Optional[dict] = Body(default=None)):
    """Run the shared orchestrator (jobcopilot.ingest.run_pipeline).

    Keeps the Phase 0 keys the UI reads (ingested_raw, passed, inserted,
    updated, workday_details_filled, rescored) and adds `sources`, `skipped`,
    `manual`, `by_connector` and the dedupe counts.
    """
    body = body or {}
    # Same config/filters.yaml baseline as the nightly run; only keys present
    # in the body override it.
    overrides: dict = {}
    try:
        if body.get("min_lpa") is not None:
            overrides["min_lpa"] = float(body["min_lpa"])
        if body.get("usd_to_inr") is not None:
            overrides["usd_to_inr"] = float(body["usd_to_inr"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="min_lpa / usd_to_inr must be numbers")
    if body.get("sectors"):
        overrides["sectors"] = set(body["sectors"])
    if body.get("roles"):
        overrides["target_roles"] = list(body["roles"])
    if "max_age_days" in body:
        overrides["max_age_days"] = body["max_age_days"]
    filter_cfg = dataclasses.replace(default_filter_cfg(), **overrides)
    return run_pipeline(
        companies_path=body.get("companies"),
        include_boards=bool(body.get("boards", True)),
        include_discovered=bool(body.get("discovered", True)),
        filter_cfg=filter_cfg,
    )


@router.get("/api/sources/health")
def sources_health():
    """Per-source health from source_state: last_run, last_ok, last_error, items."""
    return {"sources": store.list_source_state()}


@router.post("/api/score")
def score_all_endpoint():
    summary = scoring.score_all()
    if summary.get("error"):
        raise HTTPException(status_code=400, detail=summary["error"])
    return summary


@router.post("/api/funding/run")
def funding_run():
    from jobcopilot import funding
    out = ROOT / "discovered_companies.yaml"
    return funding.run(out_path=str(out))
