"""Arbeitnow job-board API (board connector; Germany-heavy).

  GET https://www.arbeitnow.com/api/job-board-api        (page 1 only, ~300 rows)

Terms (in the response's meta): free public API, don't abuse it, link back to
arbeitnow.com — url / apply_url / attribution_url are the Arbeitnow listing.
robots.txt (re-checked 2026-11-12) allows everything. Most rows are on-site
in Germany, which can never pass our geo rule, so only `remote: true` rows are
kept (config `remote_only`, default true). A bare "Remote" / "Remote job"
location is not a region, so those rows get no region_restrictions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from ..models import Job, REMOTE, UNKNOWN
from ..normalize import strip_html, infer_country_bucket
from ..regions import partition_location, parse_utc_offsets

_NOT_A_REGION = {"remote", "remote job", "remote jobs", "anywhere"}


def _iso(epoch: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class ArbeitnowConnector(Connector):
    source = "arbeitnow"
    URL = "https://www.arbeitnow.com/api/job-board-api"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        # Page 1 only: the low-volume rule, and page 1 is the newest ~300 rows.
        return self.client.get_json(self.URL, headers=DEFAULT_HEADERS, timeout=self.timeout)

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        board = company or {}
        attr = board.get("attribution") or {}
        remote_only = board.get("remote_only", True)
        jobs: list[Job] = []
        seen: set[str] = set()
        for j in (raw or {}).get("data") or []:
            if not isinstance(j, dict) or not j.get("slug"):
                continue
            remote = j.get("remote") is True
            if remote_only and not remote:
                continue
            slug = str(j["slug"])
            if slug in seen:
                continue
            seen.add(slug)
            listing = str(j.get("url") or "").strip()
            location = str(j.get("location") or "").strip()
            is_bare = location.lower() in _NOT_A_REGION
            regions, tz_text = ([], None) if is_bare else partition_location(location)
            tags = [str(t) for t in (j.get("tags") or []) if t]
            jobs.append(Job(
                source=self.source,
                company=str(j.get("company_name") or "").strip(),
                company_sector=None,
                external_id=slug,
                title=str(j.get("title") or "").strip(),
                location_text="Remote" if is_bare else (f"{location} (remote)" if remote and location else location),
                workplace_type=REMOTE if remote else UNKNOWN,
                country_bucket=infer_country_bucket(location),
                url=listing,
                description_text=strip_html(j.get("description")),
                department=tags[0] if tags else None,
                posted_at=_iso(j.get("created_at")),
                apply_url=listing or None,
                region_restrictions=regions if remote else [],
                timezone_restrictions=parse_utc_offsets(tz_text) if tz_text else [],
                tz_requirement_text=tz_text,
                attribution_name=attr.get("name") or "Arbeitnow",
                attribution_url=listing or attr.get("url") or "https://www.arbeitnow.com",
                raw=j,
            ))
        return jobs
