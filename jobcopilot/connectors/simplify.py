"""SimplifyJobs New-Grad-Positions listings.json on GitHub (board connector).

  GET https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json

One ~13 MB JSON array (~19k rows, US-centric) that backs the repo's README.
We keep only rows that are active, visible, posted in the last `max_age_days`,
and located in India or remote: that is what this user can act on, and it
keeps the stored set to ~100 rows instead of ~19k.

No licence: we keep the employer facts, credit the repo ("via SimplifyJobs
(GitHub)") and Apply goes to the employer link. Conditional GET: PoliteClient
sends If-None-Match from its ETag cache (persisted across runs only when
http.cache_dir is set), so an unchanged file costs a 304, not 13 MB.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from .base import Connector
from .speedyapply import (  # noqa: F401  (remote_regions is re-exported for callers/tests)
    INDIA_RE, WORLDWIDE_MIN_COUNTRIES, _REMOTE_IN_RE, clean_apply_url, remote_regions, validate_github_path)
from ..models import Job, REMOTE, UNKNOWN, IN
from ..normalize import infer_country_bucket, infer_workplace_type

log = logging.getLogger("jobcopilot.connectors.simplify")

DEFAULT_REPO = "SimplifyJobs/New-Grad-Positions"
DEFAULT_BRANCH = "dev"
DEFAULT_PATH = ".github/scripts/listings.json"
MAX_BYTES = 25_000_000
DEADLINE_SEC = 180.0

_REMOTE_RE = re.compile(r"\bremote\b", re.I)


def _wanted(locations: list[str]) -> bool:
    return any(INDIA_RE.search(loc) or _REMOTE_RE.search(loc) for loc in locations)


def _location_text(locations: list[str], regions: list[str]) -> str:
    others = [loc for loc in locations if not _REMOTE_IN_RE.match(loc)]
    remote = [f"Remote ({', '.join(regions)})"] if regions else []
    parts = others + remote
    return "; ".join(parts[:8]) + (f" (+{len(parts) - 8} more)" if len(parts) > 8 else "")


class SimplifyConnector(Connector):
    source = "simplify"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        board = company or {}
        url = validate_github_path(board.get("repo") or DEFAULT_REPO,
                                   board.get("branch") or DEFAULT_BRANCH,
                                   board.get("path") or DEFAULT_PATH)
        return self.client.get_json(
            url, headers={"Accept": "application/json"}, timeout=self.timeout,
            max_bytes=int(board.get("max_bytes") or MAX_BYTES),
            deadline_sec=float(board.get("deadline_sec") or DEADLINE_SEC))

    def parse(self, raw: Any, company: dict[str, Any], *,
              now: Optional[datetime] = None) -> list[Job]:
        board = company or {}
        now = now or datetime.now(timezone.utc)
        max_age = float(board.get("max_age_days") or 60)
        attr = board.get("attribution") or {}
        repo_url = attr.get("url") or f"https://github.com/{board.get('repo') or DEFAULT_REPO}"
        cutoff = now.timestamp() - max_age * 86400
        jobs: list[Job] = []
        counts = {"inactive": 0, "old": 0, "location": 0, "no_url": 0}
        for r in raw if isinstance(raw, list) else []:
            if not isinstance(r, dict):
                continue
            if not r.get("active") or r.get("is_visible") is False:
                counts["inactive"] += 1
                continue
            try:
                posted = float(r.get("date_posted") or 0)
            except (TypeError, ValueError):
                posted = 0.0
            if posted < cutoff:
                counts["old"] += 1
                continue
            locations = [str(x) for x in (r.get("locations") or []) if x]
            if not _wanted(locations):
                counts["location"] += 1
                continue
            apply_url = clean_apply_url(r.get("url"))
            if not (apply_url and r.get("company_name") and r.get("title")):
                counts["no_url"] += 1
                continue
            regions = remote_regions(locations)
            location = _location_text(locations, regions)
            remote = any(_REMOTE_RE.search(loc) for loc in locations)
            india = any(INDIA_RE.search(loc) for loc in locations) or "India" in regions
            degrees = ", ".join(r.get("degrees") or []) or "not stated"
            jobs.append(Job(
                source=self.source,
                company=(r.get("company_name") or "").strip(),
                company_sector=None,
                external_id=str(r.get("id") or apply_url),
                title=(r.get("title") or "").strip(),
                location_text=location,
                workplace_type=REMOTE if remote else (infer_workplace_type(location) or UNKNOWN),
                country_bucket=IN if india else infer_country_bucket(location),
                url=apply_url,
                description_text=(f"Category: {r.get('category') or 'n/a'}. "
                                  f"Sponsorship: {r.get('sponsorship') or 'n/a'}. Degrees: {degrees}."),
                posted_at=datetime.fromtimestamp(posted, tz=timezone.utc).isoformat(),
                apply_url=apply_url,
                region_restrictions=regions,
                attribution_name=attr.get("name") or "SimplifyJobs (GitHub)",
                attribution_url=repo_url,
                raw={k: v for k, v in r.items() if k != "locations"} | {"locations": locations[:20]},
            ))
        log.info("simplify: kept %d, skipped %s", len(jobs), counts)
        return jobs
