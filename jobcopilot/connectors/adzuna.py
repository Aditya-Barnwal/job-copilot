"""Adzuna search API connector.

  GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}
    ?app_id=...&app_key=...&results_per_page=50&what=<query>&where=<location>

Search-style connector: a "company" in companies.yaml is actually a *query*,
e.g. `{ name: "Adzuna IN SWE", connector: adzuna, what: "software engineer" }`.
Each result's actual company name comes from the API row (not the query name).

If `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` are absent we skip live — the parse path
is still unit-tested against a fixture.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from ..models import Job, Compensation
from ..normalize import strip_html, infer_workplace_type, infer_country_bucket


def _iso(dt_str: Optional[str]) -> Optional[str]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).isoformat()
    except Exception:
        return dt_str


class AdzunaConnector(Connector):
    source = "adzuna"
    BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        app_id = os.getenv("ADZUNA_APP_ID")
        app_key = os.getenv("ADZUNA_APP_KEY")
        if not app_id or not app_key:
            raise RuntimeError("Adzuna skipped (no keys)")
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": int(company.get("results_per_page", 50)),
            "what": company.get("what") or "software engineer",
        }
        if company.get("where"):
            params["where"] = company["where"]
        if company.get("salary_min"):
            params["salary_min"] = company["salary_min"]
        url = self.BASE.format(country=company.get("country", "in"),
                               page=int(company.get("page", 1)))
        # app_key rides in the query string; PoliteClient redacts it from logs/errors.
        return self.client.get_json(url, params=params, headers=DEFAULT_HEADERS,
                                    timeout=self.timeout)

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        jobs: list[Job] = []
        for r in (raw or {}).get("results", []) or []:
            employer = ((r.get("company") or {}).get("display_name") or "Unknown").strip()
            location = ((r.get("location") or {}).get("display_name") or "").strip()
            min_s = r.get("salary_min")
            max_s = r.get("salary_max")
            comp = None
            if min_s or max_s:
                comp = Compensation(
                    currency="INR" if company.get("country", "in") == "in" else None,
                    min_value=float(min_s) if min_s else None,
                    max_value=float(max_s) if max_s else None,
                    interval="1 YEAR",
                    summary=r.get("salary_is_predicted") and "predicted" or None,
                    source="adzuna",
                    is_estimate=bool(r.get("salary_is_predicted")),
                )
            description = strip_html(r.get("description"))
            jobs.append(
                Job(
                    source=self.source,
                    company=employer,
                    company_sector=company.get("sector"),
                    external_id=str(r.get("id") or r.get("redirect_url") or r.get("title")),
                    title=r.get("title", "") or "",
                    location_text=location,
                    workplace_type=infer_workplace_type(location),
                    country_bucket=infer_country_bucket(location),
                    url=r.get("redirect_url", "") or "",
                    description_text=description,
                    department=(r.get("category") or {}).get("label") if isinstance(r.get("category"), dict) else None,
                    compensation=comp,
                    posted_at=_iso(r.get("created")),
                    raw=r,
                )
            )
        return jobs
