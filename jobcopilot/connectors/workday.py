"""Workday CXS jobs API — one connector for every Workday tenant.

  POST https://{tenant}.{wd_server}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
  body: {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
  -> {"total": N, "jobPostings": [...]}

This is the medium-difficulty one. Three quirks worth knowing:

  1. It's a POST with a JSON body, not a GET — even for an unfiltered listing.
  2. Every tenant lives on a different data center: wd1 / wd3 / wd5 / ... You
     must use the right one (read it off the careers-page URL).
  3. The list response has no descriptions — only title, location, postedOn and
     an externalPath. Full text needs a follow-up GET per job: fetch_detail(),
     called only for postings that survive the filters.

robots.txt on *.myworkdayjobs.com (checked 2026-11-11) disallows only
/refreshFacet/, so the /wday/cxs/ paths used here are allowed.

Per-company config therefore needs: tenant, wd_server, site.
e.g. https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced
     -> tenant="adobe", wd_server="wd5", site="external_experienced"
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from .base import Connector, DEFAULT_HEADERS
from ..ats_detect import host_label, path_id
from ..models import Job
from ..normalize import infer_workplace_type, infer_country_bucket, strip_html


_WD_SERVER = re.compile(r"wd\d{1,3}")


class WorkdayConnector(Connector):
    source = "workday"

    @staticmethod
    def endpoint_parts(company: dict[str, Any]) -> tuple[str, str, str]:
        """(tenant, wd_server, site) from a config row, each validated for its place
        in the URL: tenant is a host label, wd_server is wdN, site is one path segment
        (returned percent-encoded). A bad row raises ValueError, never a stray request."""
        tenant = host_label(company.get("tenant"), "Workday tenant")
        wd_server = str(company.get("wd_server") or "wd1").strip().lower()
        if _WD_SERVER.fullmatch(wd_server) is None:
            raise ValueError(f"unusable Workday server {wd_server!r}")
        return tenant, wd_server, path_id(company.get("site"), "Workday site")

    def _endpoint(self, company: dict[str, Any]) -> tuple[str, str, str]:
        tenant, wd_server, site = self.endpoint_parts(company)
        host = f"https://{tenant}.{wd_server}.myworkdayjobs.com"
        list_url = f"{host}/wday/cxs/{tenant}/{site}/jobs"
        return host, site, list_url

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        host, site, list_url = self._endpoint(company)
        headers = {
            **DEFAULT_HEADERS,
            "Content-Type": "application/json",
            "Referer": f"{host}/en-US/{site}",
        }
        limit = 20
        offset = 0
        max_jobs = int(company.get("max", 200))
        postings: list[dict] = []

        while True:
            payload = {
                "appliedFacets": {},
                "limit": limit,
                "offset": offset,
                "searchText": company.get("search_text", ""),
            }
            # A read-only search, so retrying a 5xx can't create anything.
            data = self.client.post_json(
                list_url, json=payload, headers=headers, timeout=self.timeout,
                idempotent=True,
            ) or {}
            page = data.get("jobPostings", []) or []
            postings.extend(page)

            total = data.get("total", 0)
            offset += limit
            if not page or offset >= total or offset >= max_jobs:
                break

        return {"jobPostings": postings, "_host": host, "_site": site}

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        raw = raw or {}
        host = raw.get("_host") or self._endpoint(company)[0]
        site = raw.get("_site") or self._endpoint(company)[1]

        jobs: list[Job] = []
        for p in raw.get("jobPostings", []):
            location = (p.get("locationsText") or "").strip()
            external_path = p.get("externalPath", "") or ""
            # bulletFields[0] is usually the Job Req ID; fall back to the path.
            bullets = p.get("bulletFields") or []
            external_id = str(bullets[0]) if bullets else external_path
            url = f"{host}/en-US/{site}{external_path}" if external_path else host

            jobs.append(
                Job(
                    source=self.source,
                    company=company["name"],
                    company_sector=company.get("sector"),
                    external_id=external_id,
                    title=p.get("title", "") or "",
                    location_text=location,
                    workplace_type=infer_workplace_type(location),
                    country_bucket=infer_country_bucket(location),
                    url=url,
                    description_text="",  # list endpoint carries no body; fetched on demand later
                    department=None,
                    compensation=None,
                    posted_at=p.get("postedOn"),
                    raw=p,
                )
            )
        return jobs

    def fetch_detail(self, job: "Job", company: dict[str, Any]) -> str:
        """Fetch a single posting's full description text. Failure-isolated
        — caller should treat any exception as 'this one posting only',
        not the whole pass. Returns '' if nothing usable was found."""
        host, site, _ = self._endpoint(company)
        external_path = (job.raw or {}).get("externalPath") or ""
        if not external_path.startswith("/"):
            return ""  # from the payload: must stay a path on this tenant's host
        url = f"{host}/wday/cxs/{self.endpoint_parts(company)[0]}/{site}{external_path}"
        headers = {
            **DEFAULT_HEADERS,
            "Referer": f"{host}/en-US/{site}",
            "Accept": "application/json",
        }
        data = self.client.get_json(url, headers=headers, timeout=self.timeout) or {}
        info = data.get("jobPostingInfo") or {}
        return strip_html(info.get("jobDescription") or "")
