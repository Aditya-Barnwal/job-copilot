"""Working Nomads public JSON feed (board connector).

  GET https://www.workingnomads.com/api/exposed_jobs/

One call returns the ~36 most recent jobs across all categories; there is no
paging, so we filter to the configured `categories` locally. Terms: no resale
or competing product (personal use is fine). Location is free text that mixes
regions and time zones ("Europe, LATAM, APAC", "Time zone: CET (+/- 3 hours)"),
which regions.partition_location separates without judging eligibility.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from ..models import Job, REMOTE
from ..normalize import strip_html, infer_country_bucket
from ..regions import partition_location, parse_utc_offsets

_ID_RE = re.compile(r"/job/go/(\d+)")


def _external_id(j: dict[str, Any]) -> str:
    url = j.get("url") or ""
    m = _ID_RE.search(url)
    return m.group(1) if m else (url or str(j.get("title")))


class WorkingNomadsConnector(Connector):
    source = "workingnomads"
    URL = "https://www.workingnomads.com/api/exposed_jobs/"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return self.client.get_json(self.URL, headers=DEFAULT_HEADERS, timeout=self.timeout)

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        board = company or {}
        attr = board.get("attribution") or {}
        wanted = {str(c).strip().lower() for c in board.get("categories") or []}

        jobs: list[Job] = []
        for j in raw or []:
            if not isinstance(j, dict):
                continue
            if wanted and str(j.get("category_name") or "").strip().lower() not in wanted:
                continue
            location = (j.get("location") or "").strip()
            regions, tz_text = partition_location(location)
            # The /job/go/<id>/ link is both the listing and the outbound apply link.
            url = (j.get("url") or "").strip()
            jobs.append(Job(
                source=self.source,
                company=(j.get("company_name") or "").strip(),
                company_sector=None,
                external_id=_external_id(j),
                title=(j.get("title") or "").strip(),
                location_text=location or "Remote",
                workplace_type=REMOTE,
                country_bucket=infer_country_bucket(", ".join(regions)),
                url=url,
                description_text=strip_html(j.get("description")),
                department=j.get("category_name") or None,
                posted_at=j.get("pub_date") or None,
                apply_url=url or None,
                region_restrictions=regions,
                timezone_restrictions=parse_utc_offsets(tz_text) if tz_text else [],
                tz_requirement_text=tz_text,
                attribution_name=attr.get("name") or "Working Nomads",
                attribution_url=url or attr.get("url") or "https://www.workingnomads.com",
                raw=j,
            ))
        return jobs
