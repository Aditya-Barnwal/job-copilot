"""Personio public job XML feed.

  GET https://{slug}.jobs.personio.com/xml     (or .jobs.personio.de)
  -> <workzag-jobs><position>...</position>...</workzag-jobs>

Each <position> has id, office + additionalOffices/office, department, name
(the title), jobDescriptions/jobDescription (name + HTML value), employmentType,
seniority, schedule, yearsOfExperience and createdAt. The feed is the whole
board in one call (1KOMMA5: 326 positions, 2.3 MB).

Two hosts exist; the company row says which with `region: de` (default `com`).
The job page is https://{slug}.jobs.personio.{tld}/job/{id}.

ROBOTS (checked live 2026-11-12, *.jobs.personio.com): `Allow: /`.
Checked live: 1komma5grad.jobs.personio.com/xml 200; dynamix-india 404 (gone).
"""
from __future__ import annotations

from typing import Any

from .base import Connector
from .teamtailor import xml_root
from ..ats_detect import host_label
from ..models import Job
from ..normalize import strip_html, infer_workplace_type, infer_country_bucket

XML_HEADERS = {"Accept": "application/xml, text/xml;q=0.9, */*;q=0.5"}
TLDS = ("com", "de")


def personio_tld(company: dict[str, Any]) -> str:
    """'com' (default) or 'de' from the row's `region`; anything else is a config error."""
    tld = str(company.get("region") or "com").strip().lower()
    if tld not in TLDS:
        raise ValueError(f"personio region must be 'com' or 'de', got {tld!r}")
    return tld


class PersonioConnector(Connector):
    source = "personio"
    BASE = "https://{slug}.jobs.personio.{tld}/xml"
    JOB_PAGE = "https://{slug}.jobs.personio.{tld}/job/{id}"

    @classmethod
    def list_url(cls, slug: str, region: Any = None) -> str:
        return cls.BASE.format(slug=host_label(slug, "Personio slug"),
                               tld=personio_tld({"region": region}))

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return self.client.get(self.list_url(company["slug"], company.get("region")),
                               headers=XML_HEADERS, timeout=self.timeout).text

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        root = xml_root(raw)
        jobs: list[Job] = []
        if root is None:
            return jobs
        slug = host_label(company.get("slug"), "Personio slug") if company.get("slug") else ""
        tld = personio_tld(company)
        for p in root.iterfind("position"):
            pid = (p.findtext("id") or "").strip()
            offices = [(p.findtext("office") or "").strip()]
            offices += [(o.text or "").strip() for o in p.iterfind("additionalOffices/office")]
            location = "; ".join(dict.fromkeys(o for o in offices if o))
            sections = []
            for d in p.iterfind("jobDescriptions/jobDescription"):
                name, body = (d.findtext("name") or "").strip(), strip_html(d.findtext("value"))
                if body:
                    sections.append(f"{name}\n{body}" if name else body)
            raw_fields = {c.tag: (c.text or "").strip() for c in p
                          if len(c) == 0 and c.tag not in ("name",)}
            jobs.append(Job(
                source=self.source,
                company=company["name"],
                company_sector=company.get("sector"),
                external_id=pid,
                title=(p.findtext("name") or "").strip(),
                location_text=location,
                workplace_type=infer_workplace_type(location),
                country_bucket=infer_country_bucket(location),
                url=self.JOB_PAGE.format(slug=slug, tld=tld, id=pid) if slug and pid.isdigit() else "",
                description_text="\n\n".join(sections),
                department=(p.findtext("department") or "").strip() or None,
                compensation=None,
                posted_at=(p.findtext("createdAt") or "").strip() or None,
                raw={**raw_fields, "offices": offices},
            ))
        return jobs
