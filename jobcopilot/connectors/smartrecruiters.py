"""SmartRecruiters public Posting API.

  GET https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=100&offset=N
  -> {"offset", "limit", "totalFound", "content": [posting, ...]}

`slug` is the company identifier as it appears in careers.smartrecruiters.com/<id>
(case matters: "Freshworks"). The list carries title, location (with remote /
hybrid flags), department and releasedDate, but no description; the body lives
on each posting's detail URL, so like Workday it is fetched on demand via
fetch_detail() for postings that survive the filters, not one call per row.

ROBOTS (checked live 2026-11-11): api.smartrecruiters.com/robots.txt is
  User-agent: LinkedInBot / Allow: /v1/companies/
  User-agent: *           / Disallow: /
PoliteClient enforces that, so this connector only runs if the host is
explicitly exempted in config/sources.yaml with a written reason. Until someone
makes that call, companies on SmartRecruiters stay `connector: manual`.
"""
from __future__ import annotations

import logging
from typing import Any

from .base import Connector, DEFAULT_HEADERS
from ..models import Job, REMOTE, HYBRID
from ..normalize import infer_workplace_type, infer_country_bucket, strip_html

log = logging.getLogger("jobcopilot.connectors.smartrecruiters")

PAGE_SIZE = 100          # API maximum
DEFAULT_MAX_JOBS = 300   # politeness cap: a few pages per company per run


def _location_text(loc: dict[str, Any]) -> str:
    """fullLocation is "City, Region, Country" but often has empty parts ("Jakarta, , Indonesia")."""
    full = loc.get("fullLocation") or ", ".join(
        str(loc.get(k) or "") for k in ("city", "region", "country")
    )
    parts = [p.strip() for p in str(full).split(",") if p.strip()]
    text = ", ".join(parts)
    if loc.get("remote") and "remote" not in text.lower():
        text = f"{text} (Remote)" if text else "Remote"
    return text


class SmartRecruitersConnector(Connector):
    source = "smartrecruiters"
    BASE = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    JOB_PAGE = "https://jobs.smartrecruiters.com/{slug}/{id}"

    @classmethod
    def list_url(cls, slug: str) -> str:
        return cls.BASE.format(slug=slug)

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        url = self.list_url(company["slug"])
        max_jobs = int(company.get("max", DEFAULT_MAX_JOBS))
        postings: list[dict] = []
        offset = 0
        total = 0
        while offset < max_jobs:
            data = self.client.get_json(
                url, params={"limit": PAGE_SIZE, "offset": offset},
                headers=DEFAULT_HEADERS, timeout=self.timeout,
            ) or {}
            page = data.get("content") or []
            postings.extend(page)
            total = int(data.get("totalFound") or 0)
            offset += PAGE_SIZE
            if not page or offset >= total:
                break
        if total > len(postings):
            log.info("smartrecruiters %s: kept %d of %d postings (max=%d)",
                     company.get("name"), len(postings), total, max_jobs)
        return {"content": postings, "totalFound": total}

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        slug = company.get("slug") or ""
        jobs: list[Job] = []
        for p in (raw or {}).get("content", []):
            loc = p.get("location") or {}
            location = _location_text(loc)
            if loc.get("remote"):
                workplace = REMOTE
            elif loc.get("hybrid"):
                workplace = HYBRID
            else:
                workplace = infer_workplace_type(location)
            ident = (p.get("company") or {}).get("identifier") or slug
            pid = str(p.get("id") or p.get("uuid") or "")
            jobs.append(
                Job(
                    source=self.source,
                    company=company["name"],
                    company_sector=company.get("sector"),
                    external_id=pid,
                    title=p.get("name", "") or "",
                    location_text=location,
                    workplace_type=workplace,
                    country_bucket=infer_country_bucket(location),
                    url=self.JOB_PAGE.format(slug=ident, id=pid) if pid else "",
                    description_text="",  # list has no body; fetch_detail() on demand
                    department=(p.get("department") or {}).get("label")
                    or (p.get("function") or {}).get("label"),
                    compensation=None,
                    posted_at=p.get("releasedDate"),
                    raw=p,
                )
            )
        return jobs

    def fetch_detail(self, job: Job, company: dict[str, Any]) -> str:
        """One posting's description text ('' if none). Failure-isolated like
        WorkdayConnector.fetch_detail: callers treat an exception as this posting only."""
        ref = (job.raw or {}).get("ref") or (
            f"{self.list_url(company['slug'])}/{job.external_id}" if job.external_id else ""
        )
        if not ref:
            return ""
        data = self.client.get_json(ref, headers=DEFAULT_HEADERS, timeout=self.timeout) or {}
        sections = ((data.get("jobAd") or {}).get("sections") or {})
        parts = [
            strip_html((sections.get(k) or {}).get("text"))
            for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
        ]
        return "\n\n".join(p for p in parts if p)
