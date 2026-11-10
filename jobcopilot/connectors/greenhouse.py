"""Greenhouse public job board API.

  GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

Public, no auth. `content=true` includes the (HTML-entity-encoded) description.
The slug is the board token, e.g. the "acme" in boards.greenhouse.io/acme.
"""
from __future__ import annotations

from typing import Any

from .base import Connector, DEFAULT_HEADERS
from ..models import Job
from ..normalize import strip_html, infer_workplace_type, infer_country_bucket


class GreenhouseConnector(Connector):
    source = "greenhouse"
    BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        url = self.BASE.format(slug=company["slug"])
        return self.client.get_json(
            url, params={"content": "true"}, headers=DEFAULT_HEADERS, timeout=self.timeout
        )

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        jobs: list[Job] = []
        for j in (raw or {}).get("jobs", []):
            location = ((j.get("location") or {}).get("name") or "").strip()
            departments = j.get("departments") or []
            department = departments[0].get("name") if departments else None
            jobs.append(
                Job(
                    source=self.source,
                    company=company["name"],
                    company_sector=company.get("sector"),
                    external_id=str(j.get("id")),
                    title=j.get("title", "") or "",
                    location_text=location,
                    workplace_type=infer_workplace_type(location),
                    country_bucket=infer_country_bucket(location),
                    url=j.get("absolute_url", "") or "",
                    description_text=strip_html(j.get("content")),
                    department=department,
                    compensation=None,  # Greenhouse public feed rarely carries pay
                    posted_at=j.get("updated_at"),
                    raw=j,
                )
            )
        return jobs
