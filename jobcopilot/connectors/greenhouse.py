"""Greenhouse public job board API.

  GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

Public, no auth. `content=true` includes the (HTML-entity-encoded) description.
The slug is the board token, e.g. the "acme" in boards.greenhouse.io/acme.

EU-hosted boards (company config `region: eu`, e.g. Groww). Their pages live on
job-boards.eu.greenhouse.io, but there is no separate public EU API host.
Checked live 2026-11-11:
  boards-api.eu.greenhouse.io                  NXDOMAIN
  api.eu.greenhouse.io/v1/boards/groww/jobs    HTTP 401 (Harvest gateway, needs a key)
  boards-api.greenhouse.io/v1/boards/groww/jobs  200, 9 jobs, each absolute_url on
                                               job-boards.eu.greenhouse.io
So `region: eu` uses the same public API. It changes only the board host used to
build a job link when a posting has no absolute_url. REGION_BASES keeps the
region -> endpoint mapping in one place in case Greenhouse ever splits the API.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from ..models import Job
from ..normalize import strip_html, infer_workplace_type, infer_country_bucket

US, EU = "us", "eu"


def board_region(company: dict[str, Any]) -> str:
    """'us' (default) or 'eu' from the company row. Raises ValueError on anything
    else, so a typo fails that one company loudly instead of hitting the wrong board."""
    region = str(company.get("region") or US).strip().lower()
    if region not in (US, EU):
        raise ValueError(f"greenhouse region must be 'us' or 'eu', got {region!r}")
    return region


class GreenhouseConnector(Connector):
    source = "greenhouse"
    BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    # Region -> list endpoint. EU boards are served by the US public API (see module docstring).
    REGION_BASES: dict[str, str] = {EU: "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"}
    # Where a human opens the board; used only when a posting lacks absolute_url.
    BOARD_HOSTS: dict[str, str] = {
        US: "https://job-boards.greenhouse.io",
        EU: "https://job-boards.eu.greenhouse.io",
    }

    @classmethod
    def list_url(cls, slug: str, region: Optional[str] = None) -> str:
        """Jobs-list endpoint for a board. ats_detect uses this to verify a detection
        against the very endpoint ingest will call."""
        region = board_region({"region": region})
        template = cls.BASE if region == US else cls.REGION_BASES[region]
        return template.format(slug=slug)

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        url = self.list_url(company["slug"], company.get("region"))
        return self.client.get_json(
            url, params={"content": "true"}, headers=DEFAULT_HEADERS, timeout=self.timeout
        )

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        board_host = self.BOARD_HOSTS[board_region(company)]
        slug = company.get("slug") or ""
        jobs: list[Job] = []
        for j in (raw or {}).get("jobs", []):
            location = ((j.get("location") or {}).get("name") or "").strip()
            departments = j.get("departments") or []
            department = departments[0].get("name") if departments else None
            url = j.get("absolute_url") or ""
            if not url and slug and j.get("id"):
                url = f"{board_host}/{slug}/jobs/{j['id']}"
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
                    url=url,
                    description_text=strip_html(j.get("content")),
                    department=department,
                    compensation=None,  # Greenhouse public feed rarely carries pay
                    # first_published is the real posting date; updated_at moves on
                    # every edit, which made old roles look fresh. Older payloads lack it.
                    posted_at=j.get("first_published") or j.get("updated_at"),
                    raw=j,
                )
            )
        return jobs
