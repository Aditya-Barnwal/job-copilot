"""Recruitee public careers-site API.

  GET https://{slug}.recruitee.com/api/offers/
  -> {"offers": [offer, ...]}   (every published offer, one call, with HTML body)

`slug` is the subdomain of the company's careers site (box8.recruitee.com ->
"box8"). Each offer carries explicit remote / hybrid / on_site flags, a list of
locations, published_at ("2026-10-23 14:59:10 UTC"), an optional salary, and
both the posting page (careers_url) and the apply form (careers_apply_url).

ROBOTS (checked live 2026-11-12, box8.recruitee.com): `Disallow: /v/` only.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from ..ats_detect import host_label
from ..models import Compensation, Job, REMOTE, HYBRID, ONSITE
from ..normalize import strip_html, infer_workplace_type, infer_country_bucket

_PERIODS = {"year": "1 YEAR", "month": "1 MONTH", "week": "1 WEEK", "day": "1 DAY", "hour": "1 HOUR"}


def utc_text_to_iso(value: Optional[str]) -> Optional[str]:
    """'2026-10-23 14:59:10 UTC' -> '2026-10-23T14:59:10+00:00' (normalize reads ISO only)."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(" UTC"):
        text = text[:-4].replace(" ", "T", 1) + "+00:00"
    return text


def _location(o: dict[str, Any]) -> str:
    locs = [l for l in (o.get("locations") or []) if isinstance(l, dict)]
    if len(locs) > 1:
        parts = []
        for l in locs:
            text = ", ".join(str(l.get(k)).strip() for k in ("city", "country") if l.get(k))
            text = text or str(l.get("name") or "").strip()
            if text and text not in parts:
                parts.append(text)
        if parts:
            return "; ".join(parts)
    return str(o.get("location") or "").strip()


def _compensation(salary: Any) -> Optional[Compensation]:
    if not isinstance(salary, dict) or (salary.get("min") is None and salary.get("max") is None):
        return None

    def num(v: Any) -> Optional[float]:
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return Compensation(currency=salary.get("currency"), min_value=num(salary.get("min")),
                        max_value=num(salary.get("max")),
                        interval=_PERIODS.get(str(salary.get("period") or "").lower()),
                        source="recruitee", is_estimate=False)


class RecruiteeConnector(Connector):
    source = "recruitee"
    BASE = "https://{slug}.recruitee.com/api/offers/"

    @classmethod
    def list_url(cls, slug: str) -> str:
        return cls.BASE.format(slug=host_label(slug, "Recruitee slug"))

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return self.client.get_json(self.list_url(company["slug"]), headers=DEFAULT_HEADERS,
                                    timeout=self.timeout)

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        jobs: list[Job] = []
        for o in (raw or {}).get("offers") or []:
            if o.get("status") not in (None, "published"):
                continue
            location = _location(o)
            if o.get("remote"):
                workplace = REMOTE
            elif o.get("hybrid"):
                workplace = HYBRID
            elif o.get("on_site"):
                workplace = ONSITE
            else:
                workplace = infer_workplace_type(location)
            body = "\n\n".join(t for t in (strip_html(o.get("description")),
                                           strip_html(o.get("requirements"))) if t)
            jobs.append(Job(
                source=self.source,
                company=company["name"],
                company_sector=company.get("sector"),
                external_id=str(o.get("id") or o.get("guid") or o.get("slug") or ""),
                title=o.get("title", "") or "",
                location_text=location,
                workplace_type=workplace,
                country_bucket=infer_country_bucket(location),
                url=o.get("careers_url") or "",
                apply_url=o.get("careers_apply_url") or None,
                description_text=body,
                department=o.get("department") or None,
                compensation=_compensation(o.get("salary")),
                posted_at=utc_text_to_iso(o.get("published_at") or o.get("created_at")),
                expires_at=utc_text_to_iso(o.get("close_at")),
                raw=o,
            ))
        return jobs
