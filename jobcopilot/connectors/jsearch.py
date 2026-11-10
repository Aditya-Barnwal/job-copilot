"""JSearch (RapidAPI) connector — mirror of LinkedIn/Indeed/Glassdoor listings.

This is **discovery only.** The card opens the source link; the user applies
there manually. Never auto-apply.

  GET https://jsearch.p.rapidapi.com/search
    headers: X-RapidAPI-Key, X-RapidAPI-Host
    params:  query, page, num_pages, date_posted

Missing `RAPIDAPI_KEY` → skip live (parse still unit-tested via fixture).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from ..models import Job, Compensation
from ..normalize import strip_html, infer_workplace_type, infer_country_bucket


def _iso(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).isoformat()
    except Exception:
        return str(s)


class JsearchConnector(Connector):
    source = "jsearch"
    BASE = "https://jsearch.p.rapidapi.com/search"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        key = os.getenv("RAPIDAPI_KEY")
        if not key:
            raise RuntimeError("JSearch skipped (no RAPIDAPI_KEY)")
        params = {
            "query": company.get("query") or company.get("what") or "software engineer in India",
            "page": str(company.get("page", 1)),
            "num_pages": str(company.get("num_pages", 1)),
        }
        if company.get("date_posted"):
            params["date_posted"] = company["date_posted"]
        headers = {
            **DEFAULT_HEADERS,
            "X-RapidAPI-Key": key,
            "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
        }
        return self.client.get_json(self.BASE, params=params, headers=headers,
                                    timeout=self.timeout)

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        jobs: list[Job] = []
        for r in (raw or {}).get("data", []) or []:
            employer = (r.get("employer_name") or "Unknown").strip()
            city = r.get("job_city") or ""
            country = r.get("job_country") or ""
            location = ", ".join(p for p in (city, r.get("job_state") or "", country) if p)
            min_s = r.get("job_min_salary")
            max_s = r.get("job_max_salary")
            comp = None
            if min_s or max_s:
                comp = Compensation(
                    currency=r.get("job_salary_currency"),
                    min_value=float(min_s) if min_s else None,
                    max_value=float(max_s) if max_s else None,
                    interval=r.get("job_salary_period") or "1 YEAR",
                    source="jsearch",
                    is_estimate=False,
                )
            description = strip_html(r.get("job_description"))
            jobs.append(
                Job(
                    source=self.source,
                    company=employer,
                    company_sector=company.get("sector"),
                    external_id=str(r.get("job_id") or r.get("job_apply_link") or r.get("job_title")),
                    title=r.get("job_title", "") or "",
                    location_text=location,
                    workplace_type=("remote" if r.get("job_is_remote") else infer_workplace_type(location)),
                    country_bucket=infer_country_bucket(location),
                    url=r.get("job_apply_link") or r.get("job_google_link") or "",
                    description_text=description,
                    department=r.get("job_naics_name") or None,
                    compensation=comp,
                    posted_at=_iso(r.get("job_posted_at_datetime_utc")),
                    raw=r,
                )
            )
        return jobs
