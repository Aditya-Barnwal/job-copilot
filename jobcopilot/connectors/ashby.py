"""Ashby public job board API.

  GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true

Public, no auth, no pagination — every listed role comes back in one call.
Ashby is the only one of the four that can hand us real compensation numbers,
which is why we always request `includeCompensation=true`. It also gives an
explicit `workplaceType` plus an `isRemote` boolean.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from ..models import Job, Compensation, REMOTE
from ..normalize import (
    strip_html,
    normalize_workplace,
    infer_workplace_type,
    infer_country_bucket,
)


def _parse_compensation(comp: Optional[dict]) -> Optional[Compensation]:
    """Pull the salary tier out of Ashby's compensation object.

    Ashby returns compensation as one or more tiers, each tagged with a
    `compensationType` (Salary / Bonus / Equity). We want the Salary tier for
    the >15 LPA floor test; bonus and equity are noise for that purpose.
    """
    if not comp:
        return None

    tiers = comp.get("compensationTiers") or []
    summary = comp.get("compensationTierSummary") or comp.get("summary")

    # The live API (2026) nests the typed numbers one level down:
    # summaryComponents[] at the top, and compensationTiers[].components[].
    # Older payloads put compensationType/minValue directly on each tier.
    # Check the nested shapes first, then fall back to the flat one (F2).
    candidates: list[dict] = list(comp.get("summaryComponents") or [])
    for tier in tiers:
        candidates.extend(tier.get("components") or [])
    candidates.extend(tiers)

    salary_tier = None
    for tier in candidates:
        if str(tier.get("compensationType", "")).lower() == "salary" and (
                tier.get("minValue") is not None or tier.get("maxValue") is not None):
            salary_tier = tier
            break
    if salary_tier is None and tiers:
        # Some boards flatten components instead of typing them; fall back to
        # the first tier that actually carries numbers.
        for tier in tiers:
            if tier.get("minValue") is not None or tier.get("maxValue") is not None:
                salary_tier = tier
                break

    if salary_tier is None:
        # No numeric tier, but a human-readable summary may still exist.
        if summary:
            return Compensation(summary=summary, source="ashby", is_estimate=False)
        return None

    return Compensation(
        currency=salary_tier.get("currencyCode"),
        min_value=salary_tier.get("minValue"),
        max_value=salary_tier.get("maxValue"),
        interval=salary_tier.get("interval"),
        summary=summary,
        source="ashby",
        is_estimate=False,
    )


class AshbyConnector(Connector):
    source = "ashby"
    BASE = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

    @classmethod
    def list_url(cls, slug: str) -> str:
        """Jobs-list endpoint for a board, with the slug as ONE percent-encoded path
        segment: a slug that came from untrusted HTML must never add '/', '?' or a
        dot segment to a request on this robots-exempt host."""
        slug = str(slug or "").strip()
        if slug in ("", ".", ".."):
            raise ValueError(f"unusable Ashby slug {slug!r}")
        return cls.BASE.format(slug=urllib.parse.quote(slug, safe=""))

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        url = self.list_url(company["slug"])
        return self.client.get_json(
            url,
            params={"includeCompensation": "true"},
            headers=DEFAULT_HEADERS,
            timeout=self.timeout,
        )

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        jobs: list[Job] = []
        for j in (raw or {}).get("jobs", []):
            # Skip unlisted (draft/internal) postings Ashby flags for us.
            if not j.get("isListed", True):
                continue

            location = (j.get("location") or "").strip()
            workplace = normalize_workplace(j.get("workplaceType"))
            if workplace is None:
                workplace = REMOTE if j.get("isRemote") else infer_workplace_type(location)

            description = j.get("descriptionPlain") or strip_html(j.get("descriptionHtml"))
            external_id = str(j.get("id") or j.get("jobUrl") or j.get("title"))

            jobs.append(
                Job(
                    source=self.source,
                    company=company["name"],
                    company_sector=company.get("sector"),
                    external_id=external_id,
                    title=j.get("title", "") or "",
                    location_text=location,
                    workplace_type=workplace,
                    country_bucket=infer_country_bucket(location),
                    url=j.get("jobUrl", "") or "",
                    description_text=description,
                    department=j.get("department") or j.get("team"),
                    compensation=_parse_compensation(j.get("compensation")),
                    posted_at=j.get("publishedAt") or j.get("updatedAt"),
                    raw=j,
                )
            )
        return jobs
