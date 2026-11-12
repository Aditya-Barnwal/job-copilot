"""Pinpoint public postings feed.

  GET https://{slug}.pinpointhq.com/postings.json
  -> {"data": [posting, ...]}   (every live posting, one call, with HTML sections)

`slug` is the careers-site subdomain (accenture.pinpointhq.com -> "accenture").
Each posting has title, url, workplace_type (onsite / hybrid / remote),
location {name, city, province}, job.department.name, deadline_at, typed
compensation fields (when compensation_visible) and the body split into
description / key_responsibilities / skills_knowledge_expertise / benefits.
There is no publish date in the feed.

ROBOTS (checked 2026-11-12 survey, accenture.pinpointhq.com): disallows
/mydata, /admin and /companies only.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from ..ats_detect import host_label
from ..models import Compensation, Job
from ..normalize import strip_html, normalize_workplace, infer_workplace_type, infer_country_bucket

_SECTIONS = ("description", "key_responsibilities", "skills_knowledge_expertise", "benefits")
_FREQ = {"year": "1 YEAR", "annually": "1 YEAR", "month": "1 MONTH", "week": "1 WEEK",
         "day": "1 DAY", "hour": "1 HOUR"}


def _location(p: dict[str, Any]) -> str:
    loc = p.get("location") or {}
    parts: list[str] = []
    for k in ("name", "province"):
        v = str(loc.get(k) or "").strip()
        if v and v not in parts:
            parts.append(v)
    return ", ".join(parts)


def _num(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _compensation(p: dict[str, Any]) -> Optional[Compensation]:
    if not p.get("compensation_visible"):
        return None
    lo, hi = _num(p.get("compensation_minimum")), _num(p.get("compensation_maximum"))
    summary = (p.get("compensation") or "").strip() or None
    if lo is None and hi is None and not summary:
        return None
    freq = str(p.get("compensation_frequency") or "").lower()
    interval = next((v for k, v in _FREQ.items() if k in freq), None)
    return Compensation(currency=p.get("compensation_currency"),
                        min_value=lo, max_value=hi,
                        interval=interval, summary=summary, source="pinpoint", is_estimate=False)


class PinpointConnector(Connector):
    source = "pinpoint"
    BASE = "https://{slug}.pinpointhq.com/postings.json"

    @classmethod
    def list_url(cls, slug: str) -> str:
        return cls.BASE.format(slug=host_label(slug, "Pinpoint slug"))

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return self.client.get_json(self.list_url(company["slug"]), headers=DEFAULT_HEADERS,
                                    timeout=self.timeout)

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        jobs: list[Job] = []
        for p in (raw or {}).get("data") or []:
            location = _location(p)
            body = "\n\n".join(t for t in (strip_html(p.get(k)) for k in _SECTIONS) if t)
            dept = ((p.get("job") or {}).get("department") or {}).get("name")
            jobs.append(Job(
                source=self.source,
                company=company["name"],
                company_sector=company.get("sector"),
                external_id=str(p.get("id") or p.get("path") or ""),
                title=p.get("title", "") or "",
                location_text=location,
                workplace_type=(normalize_workplace(p.get("workplace_type"))
                                or infer_workplace_type(location)),
                country_bucket=infer_country_bucket(location),
                url=p.get("url") or "",
                description_text=body,
                department=dept or None,
                compensation=_compensation(p),
                posted_at=None,  # not in the feed
                expires_at=p.get("deadline_at") or None,
                raw=p,
            ))
        return jobs
