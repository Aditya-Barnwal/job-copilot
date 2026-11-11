"""Himalayas remote-jobs search API (board connector).

  GET https://himalayas.app/jobs/api/search?q=<query>[&page=N]
  optional header  Authorization: Bearer $HIMALAYAS_API_KEY  (60/min, 10k/day)

Unlike the ATS connectors, `fetch_raw` / `parse` take the board's config dict
(config.sources()["boards"]["himalayas"]) and return jobs for many companies.

Terms (docs/SOURCES.md): every Apply must use `applicationLink`, every card
shows "via Himalayas", expired jobs are dropped, and only a few filtered pages
a day — never a crawl. Their robots.txt (checked 2026-11-11) disallows
`/jobs*&page=` and `/jobs/*?page=`, so PoliteClient refuses page >= 2 today;
we treat that as "stop paging this query", not an error, and get page 1 only.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from ..http import HttpError, RobotsDisallowed
from ..models import Job, Compensation, REMOTE
from ..normalize import strip_html, infer_country_bucket
from ..regions import regions_from_list, parse_utc_offsets

log = logging.getLogger("jobcopilot.connectors.himalayas")

PAGE_SIZE = 20
_PERIODS = {
    "annual": "1 YEAR", "yearly": "1 YEAR", "year": "1 YEAR",
    "monthly": "1 MONTH", "month": "1 MONTH",
    "weekly": "1 WEEK", "week": "1 WEEK",
    "daily": "1 DAY", "day": "1 DAY",
    "hourly": "1 HOUR", "hour": "1 HOUR",
}


def _epoch(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _iso(value: Any) -> Optional[str]:
    dt = _epoch(value)
    return dt.isoformat() if dt else None


def _money(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _compensation(j: dict[str, Any]) -> Optional[Compensation]:
    """Numbers only when Himalayas also names the currency: a bare number with
    no currency would be read as INR downstream, which would be fabricating."""
    lo, hi = _money(j.get("minSalary")), _money(j.get("maxSalary"))
    cur = (j.get("currency") or "").strip().upper() or None
    if (lo is None and hi is None) or cur is None:
        return None
    period = _PERIODS.get(str(j.get("salaryPeriod") or "").strip().lower())
    return Compensation(currency=cur, min_value=lo, max_value=hi, interval=period,
                        source="himalayas", is_estimate=False)


class HimalayasConnector(Connector):
    source = "himalayas"
    SEARCH = "https://himalayas.app/jobs/api/search"
    API_KEY_ENV = "HIMALAYAS_API_KEY"

    def _headers(self) -> dict[str, str]:
        headers = dict(DEFAULT_HEADERS)
        key = os.environ.get(self.API_KEY_ENV, "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        """One payload per (query, page). A query that fails is logged and
        skipped so one bad query doesn't lose the others; if every query
        fails the last error is raised so the run marks the source failing."""
        board = company
        queries = board.get("queries") or ["software engineer"]
        max_pages = max(1, int(board.get("max_pages_per_query") or 1))
        headers = self._headers()
        pages: list[Any] = []
        last_err: Optional[Exception] = None
        ok = False
        for q in queries:
            for page in range(1, max_pages + 1):
                params: dict[str, Any] = {"q": q}
                if page > 1:
                    params["page"] = page
                try:
                    payload = self.client.get_json(self.SEARCH, params=params,
                                                   headers=headers, timeout=self.timeout)
                except RobotsDisallowed as e:
                    if page == 1:
                        last_err = e
                        log.warning("himalayas: %r refused (%s)", q, e)
                    else:
                        log.info("himalayas: page %d of %r disallowed by robots.txt; page 1 only", page, q)
                    break
                except HttpError as e:
                    last_err = e
                    log.warning("himalayas: query %r page %d failed: %s", q, page, e)
                    break
                ok = True
                pages.append(payload)
                if len((payload or {}).get("jobs") or []) < PAGE_SIZE:
                    break
        if not ok and last_err is not None:
            raise last_err
        return pages

    def parse(self, raw: Any, company: dict[str, Any], *,
              now: Optional[datetime] = None) -> list[Job]:
        """`raw` is one search payload or a list of them (fetch_raw's output).
        `now` is injectable so the expiry test doesn't depend on the clock."""
        board = company or {}
        now = now or datetime.now(timezone.utc)
        drop_expired = board.get("drop_expired", True)
        attr = board.get("attribution") or {}
        payloads = raw if isinstance(raw, list) else [raw]

        jobs: list[Job] = []
        seen: set[str] = set()
        for payload in payloads:
            for j in (payload or {}).get("jobs") or []:
                listing = (j.get("guid") or "").strip()
                external_id = listing or str(j.get("applicationLink") or j.get("title"))
                if external_id in seen:
                    continue   # same job matched by two queries
                seen.add(external_id)

                expiry = _epoch(j.get("expiryDate"))
                if drop_expired and expiry is not None and expiry < now:
                    continue

                regions = regions_from_list(j.get("locationRestrictions"))
                location = ", ".join(regions) if regions else "Remote"
                jobs.append(Job(
                    source=self.source,
                    company=(j.get("companyName") or "").strip(),
                    company_sector=None,
                    external_id=external_id,
                    title=(j.get("title") or "").strip(),
                    location_text=location,
                    workplace_type=REMOTE,
                    country_bucket=infer_country_bucket(location),
                    url=listing or (j.get("applicationLink") or ""),
                    description_text=strip_html(j.get("description")) or (j.get("excerpt") or ""),
                    compensation=_compensation(j),
                    posted_at=_iso(j.get("pubDate")),
                    # Terms: Apply must go through applicationLink.
                    apply_url=j.get("applicationLink") or listing or None,
                    region_restrictions=regions,
                    timezone_restrictions=parse_utc_offsets(j.get("timezoneRestrictions")),
                    tz_requirement_text=None,
                    attribution_name=attr.get("name") or "Himalayas",
                    attribution_url=listing or attr.get("url") or "https://himalayas.app",
                    expires_at=expiry.isoformat() if expiry else None,
                    raw=j,
                ))
        return jobs
