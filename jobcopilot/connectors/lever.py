"""Lever public postings API.

  GET https://api.lever.co/v0/postings/{slug}?mode=json

Public, no auth. Returns a top-level JSON *list* (not a dict). Lever is the
friendliest of the four here: it gives an explicit `workplaceType` field
(remote / on-site / hybrid) so we don't have to guess from location text.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from ..ats_detect import path_id
from ..models import Job
from ..normalize import (
    strip_html,
    normalize_workplace,
    infer_workplace_type,
    infer_country_bucket,
)


def _ms_to_iso(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return None


class LeverConnector(Connector):
    source = "lever"
    BASE = "https://api.lever.co/v0/postings/{slug}"
    BASE_EU = "https://api.eu.lever.co/v0/postings/{slug}"

    @classmethod
    def list_url(cls, slug: str, region: Optional[str] = None) -> str:
        """Postings endpoint for a board; `region` is None/'us' or 'eu'. The slug is
        validated and sent as ONE percent-encoded path segment."""
        region = str(region or "us").strip().lower()
        if region not in ("us", "eu"):
            raise ValueError(f"lever region must be 'us' or 'eu', got {region!r}")
        base = cls.BASE_EU if region == "eu" else cls.BASE
        return base.format(slug=path_id(slug, "Lever site"))

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        url = self.list_url(company["slug"], company.get("region"))
        return self.client.get_json(
            url, params={"mode": "json"}, headers=DEFAULT_HEADERS, timeout=self.timeout
        )

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        jobs: list[Job] = []
        for p in raw or []:
            categories = p.get("categories") or {}
            location = (categories.get("location") or "").strip()
            workplace = normalize_workplace(p.get("workplaceType")) or infer_workplace_type(location)
            description = p.get("descriptionPlain") or strip_html(p.get("description"))
            jobs.append(
                Job(
                    source=self.source,
                    company=company["name"],
                    company_sector=company.get("sector"),
                    external_id=str(p.get("id")),
                    title=p.get("text", "") or "",
                    location_text=location,
                    workplace_type=workplace,
                    country_bucket=infer_country_bucket(location),
                    url=p.get("hostedUrl", "") or "",
                    description_text=description,
                    department=categories.get("team"),
                    compensation=None,
                    posted_at=_ms_to_iso(p.get("createdAt")),
                    raw=p,
                )
            )
        return jobs
