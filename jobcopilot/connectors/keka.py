"""Keka careers portal (India-heavy ATS).

  1. GET https://{slug}.keka.com/careers/           -> an HTML shell; the tenant's
     organisation id appears in it as /ats/documents/<uuid>/... (we only READ that
     id from the text; /ats/documents/ itself is never requested)
  2. GET https://{slug}.keka.com/careers/api/embedjobs/default/active/{org_id}
     -> [job, ...]   (every active job, one call, with HTML body)

Both URLs sit under /careers, the only path robots.txt allows for us (checked
live 2026-11-12, lambdatest.keka.com: `Disallow: /`, `Allow: /careers`,
`Allow: /careers/`; PoliteClient enforces it on every call). The company row may
carry `org_id` to skip step 1 and make it a single request.

Job fields: title, departmentName, jobLocations[] (city, countryName), jobType
(1 part-time, 2 full-time), salaryRange {currency, minimum, maximum, salaryPeriod:
1 hourly, 3 monthly, 4 annual, 0 not given; Keka shows a range only when maximum
is set), publishedOn (ISO), description (HTML). Keka has no remote flag, so the
workplace is inferred from the location text; a posting with no location at all
stays 'unknown'. Job page: https://{slug}.keka.com/careers/jobdetails/{id}.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import quote

from .base import Connector, DEFAULT_HEADERS
from ..ats_detect import host_label
from ..models import Compensation, Job
from ..normalize import strip_html, infer_workplace_type, infer_country_bucket

_ORG_ID = re.compile(r"/ats/documents/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/", re.I)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_PERIODS = {1: "1 HOUR", 3: "1 MONTH", 4: "1 YEAR"}   # 2 = bi-weekly: no equivalent
HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"}


def org_id_from_html(html: Optional[str]) -> Optional[str]:
    """The organisation uuid in a Keka careers shell, or None (no portal / not Keka)."""
    m = _ORG_ID.search((html or "")[:500_000])
    return m.group(1).lower() if m else None


def _location(j: dict[str, Any]) -> str:
    places: list[str] = []
    for loc in j.get("jobLocations") or []:
        if not isinstance(loc, dict):
            continue
        text = ", ".join(str(v).strip() for v in
                         (loc.get("city") or loc.get("name"), loc.get("countryName")) if v)
        if text and text not in places:
            places.append(text)
    return "; ".join(places)


def _compensation(j: dict[str, Any]) -> Optional[Compensation]:
    sal = j.get("salaryRange")
    if not isinstance(sal, dict) or not sal.get("maximum"):
        return None
    return Compensation(currency=sal.get("currency"), min_value=sal.get("minimum"),
                        max_value=sal.get("maximum"), interval=_PERIODS.get(sal.get("salaryPeriod")),
                        summary=(j.get("salaryRangeFormat") or None), source="keka", is_estimate=False)


class KekaConnector(Connector):
    source = "keka"
    PAGE = "https://{slug}.keka.com/careers/"
    API = "https://{slug}.keka.com/careers/api/embedjobs/default/active/{org}"
    JOB_PAGE = "https://{slug}.keka.com/careers/jobdetails/{id}"

    @classmethod
    def page_url(cls, slug: str) -> str:
        return cls.PAGE.format(slug=host_label(slug, "Keka subdomain"))

    @classmethod
    def list_url(cls, slug: str, org_id: str) -> str:
        if not _UUID.fullmatch(str(org_id or "")):
            raise ValueError(f"unusable Keka org_id {org_id!r}")
        return cls.API.format(slug=host_label(slug, "Keka subdomain"), org=str(org_id).lower())

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        org = company.get("org_id")
        if not org:
            html = self.client.get(self.page_url(company["slug"]), headers=HTML_HEADERS,
                                   timeout=self.timeout).text
            org = org_id_from_html(html)
            if not org:
                raise ValueError(f"no Keka careers portal for {company['slug']!r}")
        data = self.client.get_json(self.list_url(company["slug"], org), headers=DEFAULT_HEADERS,
                                    timeout=self.timeout)
        if not isinstance(data, list):
            raise ValueError("unexpected Keka jobs payload (not a list)")
        return data

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        slug = host_label(company.get("slug"), "Keka subdomain") if company.get("slug") else ""
        jobs: list[Job] = []
        for j in raw or []:
            if not isinstance(j, dict) or j.get("id") in (None, "") or not j.get("title"):
                continue
            jid = str(j["id"])
            location = _location(j)
            url = self.JOB_PAGE.format(slug=slug, id=quote(jid, safe="")) if slug else ""
            jobs.append(Job(
                source=self.source,
                company=company["name"],
                company_sector=company.get("sector"),
                external_id=jid,
                title=str(j["title"]),
                location_text=location,
                workplace_type=infer_workplace_type(location),
                country_bucket=infer_country_bucket(location),
                url=url,
                apply_url=url or None,
                description_text=strip_html(j.get("description")),
                department=j.get("departmentName") or None,
                compensation=_compensation(j),
                posted_at=j.get("publishedOn"),
                raw={k: v for k, v in j.items() if k not in ("description", "excerpt")},
            ))
        return jobs
