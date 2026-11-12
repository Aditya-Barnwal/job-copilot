"""Jobicy remote-jobs API (board connector).

  GET https://jobicy.com/api/v2/remote-jobs?count=N[&geo=apac][&industry=…][&tag=…]

Terms (the API's own friendlyNotice, docs/SOURCES.md): credit Jobicy with a
direct link, and every Apply button must go to the original job URL in the
feed (`url`) — so apply_url == url == attribution_url. robots.txt
(re-checked 2026-11-12) disallows `?q=` / `?s=` / `?p=` style search params,
none of which we send. One request per configured query, page 1 only.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from ..http import HttpError
from ..models import Job, Compensation, REMOTE, SOURCE_HOSTS, safe_url
from ..normalize import strip_html, infer_country_bucket
from ..regions import partition_location, parse_utc_offsets

log = logging.getLogger("jobcopilot.connectors.jobicy")

DEFAULT_QUERIES: list[dict[str, Any]] = [{"geo": "apac", "count": 50}, {"count": 50}]
_PARAM_KEYS = {"count", "geo", "industry", "tag"}
_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,39}$")
_PERIODS = {"yearly": "1 YEAR", "annual": "1 YEAR", "monthly": "1 MONTH",
            "weekly": "1 WEEK", "daily": "1 DAY", "hourly": "1 HOUR"}
_LOGO_KEYS = ("companyLogo",)


def query_params(q: Any) -> dict[str, Any]:
    """Validate one configured query. Unknown keys or odd values raise
    ValueError, so a config typo can't smuggle a robots-disallowed param."""
    if not isinstance(q, dict):
        raise ValueError(f"jobicy query must be a mapping, got {q!r}")
    out: dict[str, Any] = {}
    for k, v in q.items():
        if k not in _PARAM_KEYS:
            raise ValueError(f"jobicy: unsupported query param {k!r}")
        if k == "count":
            n = int(v)
            if not 1 <= n <= 100:
                raise ValueError(f"jobicy: count must be 1..100, got {v!r}")
            out[k] = n
        elif not _VALUE_RE.match(str(v)):
            raise ValueError(f"jobicy: bad value for {k}: {v!r}")
        else:
            out[k] = str(v)
    return out


def _money(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _compensation(j: dict[str, Any]) -> Optional[Compensation]:
    """Numbers only with a named currency (a bare number would be read as INR)."""
    lo, hi = _money(j.get("salaryMin")), _money(j.get("salaryMax"))
    cur = str(j.get("salaryCurrency") or "").strip().upper() or None
    if (lo is None and hi is None) or cur is None:
        return None
    period = _PERIODS.get(str(j.get("salaryPeriod") or "").strip().lower())
    return Compensation(currency=cur, min_value=lo, max_value=hi, interval=period,
                        source="jobicy", is_estimate=False)


def _first(value: Any) -> Optional[str]:
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value).strip() or None if value else None


class JobicyConnector(Connector):
    source = "jobicy"
    URL = "https://jobicy.com/api/v2/remote-jobs"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        """One payload per query. A failing query is logged and skipped; if all
        fail the last error is raised so the run marks the source failing."""
        queries = [query_params(q) for q in ((company or {}).get("queries") or DEFAULT_QUERIES)]
        pages: list[Any] = []
        last_err: Optional[Exception] = None
        for params in queries:
            try:
                pages.append(self.client.get_json(self.URL, params=params,
                                                  headers=DEFAULT_HEADERS, timeout=self.timeout))
            except HttpError as e:
                last_err = e
                log.warning("jobicy: query %r failed: %s", params, e)
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
            for j in (payload or {}).get("jobs") or []:
                if not isinstance(j, dict) or not j.get("id"):
                    continue
                external_id = str(j["id"])
                if external_id in seen:
                    continue   # same job matched by the APAC and the worldwide query
                seen.add(external_id)
                listing = safe_url(j.get("url"), hosts=SOURCE_HOSTS["jobicy"])
                if not listing:
                    continue   # terms: Apply must be the feed's own link; none valid, no job
                geo = " ".join(str(j.get("jobGeo") or "").split())
                regions, tz_text = partition_location(geo)
                jobs.append(Job(
                    source=self.source,
                    company=str(j.get("companyName") or "").strip(),
                    company_sector=None,
                    external_id=external_id,
                    title=str(j.get("jobTitle") or "").strip(),
                    location_text=geo or "Remote",
                    workplace_type=REMOTE,
                    country_bucket=infer_country_bucket(geo),
                    url=listing,
                    description_text=strip_html(j.get("jobDescription")) or strip_html(j.get("jobExcerpt")),
                    department=_first(j.get("jobIndustry")),
                    compensation=_compensation(j),
                    posted_at=str(j.get("pubDate") or "").strip() or None,
                    # Terms: Apply must go to the original job URL from the feed.
                    apply_url=listing,
                    region_restrictions=regions,
                    timezone_restrictions=parse_utc_offsets(tz_text) if tz_text else [],
                    tz_requirement_text=tz_text,
                    attribution_name=attr.get("name") or "Jobicy",
                    attribution_url=listing,
                    raw={k: v for k, v in j.items() if k not in _LOGO_KEYS},
                ))
        return jobs
