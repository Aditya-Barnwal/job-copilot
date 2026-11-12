"""4dayweek.io public API v2 (board connector).

  GET https://4dayweek.io/api/v2/jobs?work_arrangement=remote&category=…&limit=N

robots.txt (re-checked 2026-11-12) allows /api/v1 and /api/v2 but disallows
the rest of /api/, so only /api/v2/jobs is ever called, page 1 only. Terms:
link back to 4dayweek.io, so url / apply_url / attribution_url are all the
listing's 4dayweek page (the API exposes no separate apply link).

Salaries are integer minor units (cents): 7000000 EUR/year = EUR 70,000.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import quote

from .base import Connector, DEFAULT_HEADERS
from ..http import HttpError
from ..models import Job, Compensation, REMOTE, HYBRID, ONSITE, UNKNOWN, SOURCE_HOSTS, safe_url
from ..normalize import strip_html, infer_country_bucket
from ..regions import parse_utc_offsets

log = logging.getLogger("jobcopilot.connectors.fourdayweek")

DEFAULT_QUERIES: list[dict[str, Any]] = [{"work_arrangement": "remote", "category": "engineering", "limit": 50}]
# `page` is deliberately absent: page 1 only (low-volume rule).
_PARAM_KEYS = {"category", "level", "schedule", "work_arrangement", "skills", "country",
               "salary_min", "salary_max", "posted_after", "q", "sort", "limit"}
_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.,:+-]{0,59}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")
_ARRANGEMENT = {"remote": REMOTE, "hybrid": HYBRID, "onsite": ONSITE, "on-site": ONSITE}
_PERIODS = {"year": "1 YEAR", "yearly": "1 YEAR", "month": "1 MONTH", "monthly": "1 MONTH",
            "week": "1 WEEK", "day": "1 DAY", "hour": "1 HOUR", "hourly": "1 HOUR"}
MAX_PLAUSIBLE_YEAR = 5_000_000   # same guard as remoteok: a bigger figure is a misread


def query_params(q: Any) -> dict[str, Any]:
    """Validate one configured query (unknown keys / odd values -> ValueError)."""
    if not isinstance(q, dict):
        raise ValueError(f"fourdayweek query must be a mapping, got {q!r}")
    out: dict[str, Any] = {}
    for k, v in q.items():
        if k not in _PARAM_KEYS:
            raise ValueError(f"fourdayweek: unsupported query param {k!r}")
        if k == "limit":
            n = int(v)
            if not 1 <= n <= 100:
                raise ValueError(f"fourdayweek: limit must be 1..100, got {v!r}")
            out[k] = n
        elif not _VALUE_RE.match(str(v)):
            raise ValueError(f"fourdayweek: bad value for {k}: {v!r}")
        else:
            out[k] = str(v)
    return out


def _minor(value: Any) -> Optional[float]:
    try:
        f = float(value) / 100.0
    except (TypeError, ValueError):
        return None
    return f if 0 < f <= MAX_PLAUSIBLE_YEAR else None


def _compensation(j: dict[str, Any]) -> Optional[Compensation]:
    lo, hi = _minor(j.get("salary_min")), _minor(j.get("salary_max"))
    cur = str(j.get("salary_currency") or "").strip().upper() or None
    if (lo is None and hi is None) or cur is None:
        return None
    period = _PERIODS.get(str(j.get("salary_period") or "").strip().lower())
    return Compensation(currency=cur, min_value=lo, max_value=hi, interval=period,
                        source="fourdayweek", is_estimate=False)


def _locations(j: dict[str, Any]) -> tuple[str, list[str]]:
    """('Sofia, Bulgaria; Cape Town, South Africa', ['Bulgaria', 'South Africa'])."""
    places: list[str] = []
    countries: list[str] = []
    for loc in j.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        city, country = str(loc.get("city") or "").strip(), str(loc.get("country") or "").strip()
        place = ", ".join(p for p in (city, country) if p)
        if place and place not in places:
            places.append(place)
        if country and country not in countries:
            countries.append(country)
    return "; ".join(places), countries


class FourDayWeekConnector(Connector):
    source = "fourdayweek"
    URL = "https://4dayweek.io/api/v2/jobs"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        queries = [query_params(q) for q in ((company or {}).get("queries") or DEFAULT_QUERIES)]
        pages: list[Any] = []
        last_err: Optional[Exception] = None
        for params in queries:
            try:
                pages.append(self.client.get_json(self.URL, params=params,
                                                  headers=DEFAULT_HEADERS, timeout=self.timeout))
            except HttpError as e:
                last_err = e
                log.warning("fourdayweek: query %r failed: %s", params, e)
        if not pages and last_err is not None:
            raise last_err
        return pages

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        board = company or {}
        attr = board.get("attribution") or {}
        payloads = raw if isinstance(raw, list) else [raw]
        jobs: list[Job] = []
        seen: set[str] = set()
        for payload in payloads:
            for j in (payload or {}).get("data") or []:
                if not isinstance(j, dict) or not (j.get("id") or j.get("slug")):
                    continue
                external_id = str(j.get("id") or j.get("slug"))
                if external_id in seen:
                    continue
                seen.add(external_id)
                slug = str(j.get("slug") or "")
                listing = safe_url(j.get("url"), hosts=SOURCE_HOSTS["fourdayweek"])
                if not listing and _SLUG_RE.match(slug):
                    listing = f"https://4dayweek.io/job/{quote(slug, safe='')}"
                if not listing:
                    continue   # terms: link back to 4dayweek.io; no valid link, no job
                co = j.get("company") if isinstance(j.get("company"), dict) else {}
                arrangement = _ARRANGEMENT.get(str(j.get("work_arrangement") or "").lower(), UNKNOWN)
                location, countries = _locations(j)
                regions = countries
                if not regions and arrangement == REMOTE and co.get("hires_worldwide"):
                    regions = ["Worldwide"]
                tzs = [str(t) for t in (j.get("timezones") or []) if t]
                tz_text = ", ".join(tzs) or None
                jobs.append(Job(
                    source=self.source,
                    company=str(co.get("name") or "").strip(),
                    company_sector=None,
                    external_id=external_id,
                    title=str(j.get("title") or "").strip(),
                    location_text=location or ("Remote" if arrangement == REMOTE else ""),
                    workplace_type=arrangement,
                    country_bucket=infer_country_bucket(location),
                    url=listing,
                    description_text=strip_html(j.get("description")),
                    department=str(j.get("category") or "").strip() or None,
                    compensation=_compensation(j),
                    posted_at=str(j.get("posted_at") or "").strip() or None,
                    apply_url=listing,   # link back required; no separate apply link
                    region_restrictions=regions if arrangement == REMOTE else [],
                    timezone_restrictions=parse_utc_offsets(tz_text) if tz_text else [],
                    tz_requirement_text=tz_text,
                    attribution_name=attr.get("name") or "4 Day Week",
                    attribution_url=listing,
                    raw={**{k: v for k, v in j.items() if k != "company"},
                         "company": {k: v for k, v in co.items() if k != "logo_url"}},
                ))
        return jobs
