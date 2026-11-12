"""Rippling ATS public job board API.

  GET https://ats.rippling.com/api/v2/board/{slug}/jobs?page=N
  -> {"items": [job, ...], "page": 0, "pageSize": 20, "totalItems": 8, "totalPages": 1}

`slug` is the board in ats.rippling.com/<slug>/jobs. Each item has id, name,
url, department.name and locations[] (name, country, countryCode, city,
workplaceType ON_SITE / HYBRID / REMOTE). No body or posting date in the list.

Paging: page 0 is what the board serves by default (checked live 2026-11-12,
koreai-india-careers: 8 jobs, 1 page). Further pages use `page=N`; that name is
not confirmed live, so paging stops as soon as a reply's `page` is not the one
asked for or brings nothing new, and never goes past `max` jobs (default 200).

ROBOTS (checked live 2026-11-12): ats.rippling.com disallows /internal/ only.
"""
from __future__ import annotations

import logging
from typing import Any

from .base import Connector, DEFAULT_HEADERS
from ..ats_detect import path_id
from ..models import Job, REMOTE, HYBRID, ONSITE
from ..normalize import normalize_workplace, infer_workplace_type, infer_country_bucket

log = logging.getLogger("jobcopilot.connectors.rippling")

DEFAULT_MAX_JOBS = 200


def _workplace(locs: list[dict], location: str) -> str:
    """One type for the posting: any remote location makes it remote-eligible."""
    kinds = {normalize_workplace(l.get("workplaceType")) for l in locs} - {None}
    for kind in (REMOTE, HYBRID, ONSITE):
        if kind in kinds:
            return kind
    return infer_workplace_type(location)


class RipplingConnector(Connector):
    source = "rippling"
    BASE = "https://ats.rippling.com/api/v2/board/{slug}/jobs"

    @classmethod
    def list_url(cls, slug: str) -> str:
        return cls.BASE.format(slug=path_id(slug, "Rippling board"))

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        url = self.list_url(company["slug"])
        max_jobs = int(company.get("max", DEFAULT_MAX_JOBS))
        items: list[dict] = []
        seen: set[str] = set()
        page, total = 0, 0
        while True:
            params = {"page": page} if page else None
            data = self.client.get_json(url, params=params, headers=DEFAULT_HEADERS,
                                        timeout=self.timeout) or {}
            batch = [i for i in data.get("items") or [] if str(i.get("id")) not in seen]
            seen.update(str(i.get("id")) for i in batch)
            items.extend(batch)
            total = int(data.get("totalItems") or len(items))
            pages = int(data.get("totalPages") or 1)
            if (not batch or int(data.get("page") or 0) != page or page + 1 >= pages
                    or len(items) >= max_jobs):
                break
            page += 1
        if total > len(items):
            log.info("rippling %s: kept %d of %d jobs", company.get("name"), len(items), total)
        return {"items": items[:max_jobs], "totalItems": total}

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        jobs: list[Job] = []
        for j in (raw or {}).get("items") or []:
            locs = [l for l in (j.get("locations") or []) if isinstance(l, dict)]
            names = list(dict.fromkeys(str(l.get("name") or "").strip() for l in locs))
            location = "; ".join(n for n in names if n)
            jobs.append(Job(
                source=self.source,
                company=company["name"],
                company_sector=company.get("sector"),
                external_id=str(j.get("id") or ""),
                title=j.get("name", "") or "",
                location_text=location,
                workplace_type=_workplace(locs, location),
                country_bucket=infer_country_bucket(location),
                url=j.get("url") or "",
                description_text="",  # list has no body
                department=(j.get("department") or {}).get("name") or None,
                compensation=None,
                posted_at=None,
                raw=j,
            ))
        return jobs
