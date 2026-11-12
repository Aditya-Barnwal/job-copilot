"""Job listing, detail, status, CSV export, and the manual-apply hand-off pack."""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from jobcopilot import store

from ..deps import enrich, filter_max_yoe

router = APIRouter()

_CSV_COLUMNS = [
    "company", "title", "role_family", "workplace_type", "country_bucket",
    "comp_currency", "comp_max", "posted_age_days", "freshness",
    "match_score", "ats_coverage", "fit_score", "min_yoe_required",
    "yoe_is_preferred", "yoe_gap", "seniority_high", "status", "url",
]


@router.get("/api/jobs")
def list_jobs_endpoint(
    status: Optional[str] = None,
    sector: Optional[str] = None,
    country_bucket: Optional[str] = None,
    workplace_type: Optional[str] = None,
    role_family: Optional[str] = None,
    freshness: Optional[str] = None,
    q: Optional[str] = None,
    min_score: Optional[float] = None,
    max_age_days: Optional[int] = None,
    hide_senior_high: bool = False,
    max_yoe: Optional[int] = None,
    limit: int = 500,
):
    rows = store.list_jobs(
        status=status, sector=sector, country_bucket=country_bucket,
        workplace_type=workplace_type, role_family=role_family,
        freshness=freshness, q=q, min_score=min_score,
        max_age_days=max_age_days, hide_senior_high=hide_senior_high,
        limit=limit,
    )
    rows = filter_max_yoe(rows, max_yoe)
    return {"jobs": enrich(rows), "count": len(rows)}


@router.get("/api/jobs/{key}")
def get_job_endpoint(key: str):
    job = store.get_job(key)
    if not job:
        raise HTTPException(status_code=404, detail="not found")
    return enrich([job])[0]


# POST /api/jobs/{key}/status lives in routers/queue.py (tracker statuses + history);
# it accepts the old request body unchanged.


@router.get("/api/export.csv")
def export_csv(
    status: Optional[str] = None,
    sector: Optional[str] = None,
    country_bucket: Optional[str] = None,
    workplace_type: Optional[str] = None,
    role_family: Optional[str] = None,
    freshness: Optional[str] = None,
    q: Optional[str] = None,
    min_score: Optional[float] = None,
    max_age_days: Optional[int] = None,
    hide_senior_high: bool = False,
    max_yoe: Optional[int] = None,
):
    rows = store.list_jobs(
        status=status, sector=sector, country_bucket=country_bucket,
        workplace_type=workplace_type, role_family=role_family,
        freshness=freshness, q=q, min_score=min_score,
        max_age_days=max_age_days, hide_senior_high=hide_senior_high,
        limit=5000,
    )
    rows = filter_max_yoe(rows, max_yoe)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_CSV_COLUMNS)
    for r in rows:
        w.writerow([r.get(c, "") for c in _CSV_COLUMNS])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=jobs.csv"})


@router.get("/api/jobs/{key}/apply-pack")
def apply_pack(key: str, resume: Optional[str] = None):
    """Returns everything a user needs to fill a form by hand: chosen resume
    (path + download + filename), profile fields, answer-pack items, posting
    URL. `resume` may be `original`, `version:<id>`, or omitted (defaults to
    the linked version if any, else original)."""
    job = store.get_job(key)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    chosen = {"source": None, "label": None, "pdf_url": None, "pdf_path": None,
              "filename": None}

    def _use_version(v: dict) -> None:
        chosen["source"] = "version"
        chosen["version_id"] = v["id"]
        chosen["label"] = v.get("label") or f"version {v['id']}"
        chosen["pdf_url"] = f"/api/resume-versions/{v['id']}/download"
        chosen["pdf_path"] = v.get("pdf_path")
        if v.get("pdf_path"):
            chosen["filename"] = Path(v["pdf_path"]).name

    def _use_original() -> None:
        r = store.get_resume()
        if r:
            chosen["source"] = "original"
            chosen["label"] = r.get("filename") or "Original"
            chosen["filename"] = r.get("filename")
            # Only the extracted text is stored, not the original PDF bytes,
            # so there is no path/download URL. Say so honestly.
            chosen["pdf_url"] = None
            chosen["pdf_path"] = None
            chosen["note"] = "original resume text is stored, but the PDF file isn't kept on disk. Attach your local original PDF."

    if resume == "original":
        _use_original()
    elif resume and resume.startswith("version:"):
        try:
            vid = int(resume.split(":", 1)[1])
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid resume selector")
        v = store.get_version(vid)
        if not v:
            raise HTTPException(status_code=404, detail="version not found")
        _use_version(v)
    else:
        # Default: linked version if any, else original.
        vid = job.get("applied_version_id")
        if vid:
            v = store.get_version(int(vid))
            if v:
                _use_version(v)
        if chosen["source"] is None:
            _use_original()

    return {
        "posting_url": job.get("url"),
        "chosen_resume": chosen,
        "profile": store.get_profile(),
        "answer_pack": store.get_answer_pack(),
        # Back-compat with the older keys the current UI reads:
        "pdf_url": chosen["pdf_url"],
        "version_label": chosen["label"],
    }
