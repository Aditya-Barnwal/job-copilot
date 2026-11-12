"""BambooHR hosted careers list.

  GET https://{slug}.bamboohr.com/careers/list
  -> {"meta": {"totalCount": N}, "result": [opening, ...]}   (every open role, one call)

`slug` is the company subdomain (curtinmaritime.bamboohr.com -> "curtinmaritime").
Openings are sparse: jobOpeningName, departmentLabel, employmentStatusLabel,
location {city, state}, atsLocation {country, state, province, city} and
locationType ("0" on-site, "1" remote, "2" hybrid; null on old boards). No body
and no posting date: those live on /careers/{id}/detail, which we do not call
per job. Job page and apply form are both https://{slug}.bamboohr.com/careers/{id}.

An unknown subdomain answers 302 to bamboohr.com's marketing site (HTML), and a
board with careers switched off answers an empty `result`; both give no jobs.

ROBOTS (checked live 2026-11-12, toggl/zapier.bamboohr.com): `Disallow:` (nothing).
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .base import Connector, DEFAULT_HEADERS
from ..ats_detect import host_label
from ..models import Job, REMOTE, HYBRID, ONSITE
from ..normalize import infer_workplace_type, infer_country_bucket

_LOCATION_TYPES = {"0": ONSITE, "1": REMOTE, "2": HYBRID}


def _location(o: dict[str, Any]) -> str:
    """atsLocation (structured, newer) first, else the older location{city,state}."""
    for key in ("atsLocation", "location"):
        loc = o.get(key) or {}
        parts: list[str] = []
        for k in ("city", "state", "province", "country"):
            v = str(loc.get(k) or "").strip()
            if v and v not in parts:
                parts.append(v)
        if parts:
            return ", ".join(parts)
    return ""


class BambooHRConnector(Connector):
    source = "bamboohr"
    BASE = "https://{slug}.bamboohr.com/careers/list"
    JOB_PAGE = "https://{slug}.bamboohr.com/careers/{id}"

    @classmethod
    def list_url(cls, slug: str) -> str:
        return cls.BASE.format(slug=host_label(slug, "BambooHR subdomain"))

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return self.client.get_json(self.list_url(company["slug"]), headers=DEFAULT_HEADERS,
                                    timeout=self.timeout)

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        slug = host_label(company.get("slug"), "BambooHR subdomain") if company.get("slug") else ""
        jobs: list[Job] = []
        for o in (raw or {}).get("result") or []:
            if not isinstance(o, dict) or o.get("id") in (None, ""):
                continue
            oid = str(o["id"])
            location = _location(o)
            workplace = _LOCATION_TYPES.get(str(o.get("locationType")))
            if workplace is None:
                workplace = REMOTE if o.get("isRemote") else infer_workplace_type(location)
            if workplace == REMOTE and not location:
                location = "Remote"
            url = self.JOB_PAGE.format(slug=slug, id=quote(oid, safe="")) if slug else ""
            jobs.append(Job(
                source=self.source,
                company=company["name"],
                company_sector=company.get("sector"),
                external_id=oid,
                title=str(o.get("jobOpeningName") or ""),
                location_text=location,
                workplace_type=workplace,
                country_bucket=infer_country_bucket(location),
                url=url,
                apply_url=url or None,
                description_text="",  # list has no body
                department=o.get("departmentLabel") or None,
                compensation=None,
                posted_at=None,
                raw=o,
            ))
        return jobs
