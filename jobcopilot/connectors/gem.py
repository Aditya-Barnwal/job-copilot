"""Gem public Job Board API (read-only listing only).

  GET https://api.gem.com/job_board/v0/{slug}/job_posts/
  -> [post, ...]  (Greenhouse-like: id, title, location.name, location_type,
                   departments, offices, content / content_plain, absolute_url,
                   first_published_at, updated_at, employment_type)

`slug` is the board's vanity path, the "<slug>" in jobs.gem.com/<slug>.
One call returns every live post with its description. The same API also has
an application-submit endpoint; this project never calls it (never auto-submit).

ROBOTS (checked live 2026-11-11): api.gem.com/robots.txt answers HTTP 403, which
PoliteClient treats as "disallow". The listing endpoint is Gem's documented
public job-board API (help.gem.com "The Job Board API"), the same situation as
api.ashbyhq.com, so it needs an exemption with that reason in
config/sources.yaml before this connector can run.
"""
from __future__ import annotations

from typing import Any

from urllib.parse import quote

from .base import Connector, DEFAULT_HEADERS
from ..ats_detect import path_id
from ..models import Job
from ..normalize import (
    strip_html, normalize_workplace, infer_workplace_type, infer_country_bucket,
)


def _posts(raw: Any) -> list[dict]:
    """The list endpoint returns a bare array; accept a wrapped form too in case it changes."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return raw.get("job_posts") or raw.get("jobs") or []
    return []


class GemConnector(Connector):
    source = "gem"
    BASE = "https://api.gem.com/job_board/v0/{slug}/job_posts/"
    BOARD = "https://jobs.gem.com/{slug}/{id}"

    @classmethod
    def list_url(cls, slug: str) -> str:
        return cls.BASE.format(slug=path_id(slug, "Gem board"))

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return self.client.get_json(
            self.list_url(company["slug"]), headers=DEFAULT_HEADERS, timeout=self.timeout
        )

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        slug = company.get("slug") or ""
        jobs: list[Job] = []
        for p in _posts(raw):
            location = ((p.get("location") or {}).get("name") or "").strip()
            if not location:
                offices = p.get("offices") or []
                location = "; ".join(o.get("name", "") for o in offices if o.get("name"))
            workplace = normalize_workplace(p.get("location_type")) or infer_workplace_type(location)
            departments = p.get("departments") or []
            pid = str(p.get("id") or "")
            url = p.get("absolute_url") or (self.BOARD.format(slug=quote(slug, safe=''), id=quote(pid, safe='')) if slug and pid else "")
            jobs.append(
                Job(
                    source=self.source,
                    company=company["name"],
                    company_sector=company.get("sector"),
                    external_id=pid,
                    title=p.get("title", "") or "",
                    location_text=location,
                    workplace_type=workplace,
                    country_bucket=infer_country_bucket(location),
                    url=url,
                    description_text=p.get("content_plain") or strip_html(p.get("content")),
                    department=departments[0].get("name") if departments else None,
                    compensation=None,  # Gem's job board API has no pay field
                    posted_at=p.get("first_published_at") or p.get("updated_at"),
                    raw=p,
                )
            )
        return jobs
