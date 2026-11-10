"""Pipeline triggers: ingest, scoring, funding discovery."""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Body, HTTPException

from jobcopilot import config, scoring, store
from jobcopilot.filters import DEFAULT_TARGET_ROLES, FilterConfig, apply_filters
from jobcopilot.ingest import enrich_workday_descriptions, ingest, load_companies

from ..deps import ROOT

router = APIRouter()


@router.post("/api/ingest")
def run_ingest(body: Optional[dict] = Body(default=None)):
    body = body or {}
    companies_path = body.get("companies") or str(config.companies_path())
    companies = load_companies(companies_path)
    jobs = ingest(companies, fixtures_dir=None)

    target_roles = body.get("roles") or list(DEFAULT_TARGET_ROLES)
    sectors = set(body["sectors"]) if body.get("sectors") else None
    # Named filter_cfg, not `config`: rebinding `config` here would shadow the
    # module for the whole function and break config.companies_path() above.
    filter_cfg = FilterConfig(
        min_lpa=float(body.get("min_lpa", 15.0)),
        sectors=sectors,
        usd_to_inr=float(body.get("usd_to_inr", 83.0)),
        target_roles=target_roles,
        max_age_days=body.get("max_age_days"),
    )
    results = apply_filters(jobs, filter_cfg)

    # Workday detail fetch for passed jobs lacking a description
    filled_keys = enrich_workday_descriptions(results, companies)

    inserted, updated = store.upsert_jobs(results)
    store.set_meta("last_ingest", str(time.time()))

    # Re-score any jobs whose description was newly filled (if a resume exists)
    rescored = 0
    if filled_keys and store.get_resume():
        try:
            rescored = scoring.score_keys(filled_keys).get("scored", 0)
        except Exception:
            rescored = 0

    return {
        "ingested_raw": len(jobs),
        "passed": len(results),
        "inserted": inserted,
        "updated": updated,
        "workday_details_filled": len(filled_keys),
        "rescored": rescored,
    }


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
