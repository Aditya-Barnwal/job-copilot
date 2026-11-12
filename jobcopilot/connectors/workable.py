"""Workable public job widget API.

  GET https://apply.workable.com/api/v1/widget/accounts/{slug}
  -> {"name", "description", "jobs": [job, ...]}   (every published job, one call)

`slug` is the account in apply.workable.com/<slug>. Each job has title,
shortcode, department, telecommuting (remote flag), locations[] (some marked
hidden), published_on, url and application_url. The widget carries no job body,
so description_text stays '' (same as SmartRecruiters / Workday lists).

ROBOTS (checked live 2026-11-12): apply.workable.com allows everything.
Fixture note: the two live accounts checked (automattic, postman) had 0 jobs, so
tests/fixtures/more_ats/workable_acme.json follows the documented widget shape.
"""
from __future__ import annotations

from typing import Any

from .base import Connector, DEFAULT_HEADERS
from ..ats_detect import path_id
from ..models import Job, REMOTE
from ..normalize import normalize_workplace, infer_workplace_type, infer_country_bucket


def _place(d: dict[str, Any]) -> str:
    parts: list[str] = []
    for k in ("city", "region", "state", "country"):
        v = str(d.get(k) or "").strip()
        if v and v not in parts:
            parts.append(v)
    return ", ".join(parts)


def _location(j: dict[str, Any]) -> str:
    places = [_place(l) for l in (j.get("locations") or [])
              if isinstance(l, dict) and not l.get("hidden")]
    places = list(dict.fromkeys(p for p in places if p))
    return "; ".join(places) if places else _place(j)


class WorkableConnector(Connector):
    source = "workable"
    BASE = "https://apply.workable.com/api/v1/widget/accounts/{slug}"

    @classmethod
    def list_url(cls, slug: str) -> str:
        return cls.BASE.format(slug=path_id(slug, "Workable account"))

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return self.client.get_json(self.list_url(company["slug"]), headers=DEFAULT_HEADERS,
                                    timeout=self.timeout)

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        jobs: list[Job] = []
        for j in (raw or {}).get("jobs") or []:
            location = _location(j)
            if j.get("telecommuting") is True or j.get("remote") is True:
                workplace = REMOTE
            else:  # telecommuting=false only means "not remote"; hybrid/onsite stay a guess
                workplace = normalize_workplace(j.get("workplace")) or infer_workplace_type(location)
            dept = j.get("department")
            jobs.append(Job(
                source=self.source,
                company=company["name"],
                company_sector=company.get("sector"),
                external_id=str(j.get("shortcode") or j.get("code") or j.get("id") or ""),
                title=j.get("title", "") or "",
                location_text=location,
                workplace_type=workplace,
                country_bucket=infer_country_bucket(location),
                url=j.get("url") or j.get("shortlink") or "",
                apply_url=j.get("application_url") or None,
                description_text="",  # widget has no body
                department=dept if isinstance(dept, str) and dept else None,
                compensation=None,
                posted_at=j.get("published_on") or j.get("created_at"),
                raw=j,
            ))
        return jobs
