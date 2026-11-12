"""Teamtailor public jobs RSS feed.

  GET https://{slug}.teamtailor.com/jobs.rss
  -> RSS 2.0 with a `tt:` namespace: per <item> title, description (HTML),
     pubDate, link, remoteStatus (none / hybrid / temporary / fully), guid,
     tt:locations/tt:location (city, country), tt:department.

`slug` is the careers-site subdomain (veoneerin.teamtailor.com -> "veoneerin").
Companies on a custom domain still have the <slug>.teamtailor.com feed.

ROBOTS (checked live 2026-11-12, veoneerin.teamtailor.com): `User-Agent: *`
disallows /app/, /messages/, /messenger/, /facebook/tab/, /jobs/internal/ only.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from .base import Connector
from ..ats_detect import host_label
from ..models import Job, REMOTE, HYBRID, ONSITE
from ..normalize import strip_html, infer_workplace_type, infer_country_bucket

TT = "{https://teamtailor.com/locations}"
RSS_HEADERS = {"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"}
_REMOTE_STATUS = {"fully": REMOTE, "hybrid": HYBRID, "none": ONSITE}
_SKIP_WS = re.compile(rb"[ \t\r\n]*")


def _check_prolog(data: bytes) -> None:
    """Walk the XML prolog (declaration, comments, processing instructions) up to the
    root element and refuse a DOCTYPE. Walking it, rather than searching for the first
    '<x', matters: a comment such as '<!-- <x -->' must not hide a DOCTYPE after it."""
    pos = 3 if data.startswith(b"\xef\xbb\xbf") else 0
    while True:
        pos = _SKIP_WS.match(data, pos).end()
        if data.startswith(b"<?", pos):
            end = data.find(b"?>", pos)
        elif data.startswith(b"<!--", pos):
            end = data.find(b"-->", pos)
        elif data[pos:pos + 9].upper() == b"<!DOCTYPE":
            raise ValueError("feed declares a DOCTYPE; refused")
        else:
            return          # the root element (or junk the parser will reject)
        if end < 0:
            return          # unterminated: the parser raises ParseError
        pos = end + 2 if data.startswith(b"<?", pos) else end + 3


def xml_root(body: Any) -> Optional[ET.Element]:
    """Parse an untrusted feed. A DOCTYPE is refused outright: no job feed needs
    one, and it is how entity-expansion bombs get in. None for an empty body."""
    if not body:
        return None
    data = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    _check_prolog(data)
    return ET.fromstring(data)


def rfc822_to_iso(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip()).isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def _text(el: Optional[ET.Element], path: str) -> str:
    return ((el.findtext(path) if el is not None else None) or "").strip()


class TeamtailorConnector(Connector):
    source = "teamtailor"
    BASE = "https://{slug}.teamtailor.com/jobs.rss"

    @classmethod
    def list_url(cls, slug: str) -> str:
        return cls.BASE.format(slug=host_label(slug, "Teamtailor slug"))

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return self.client.get(self.list_url(company["slug"]), headers=RSS_HEADERS,
                               timeout=self.timeout).text

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        root = xml_root(raw)
        jobs: list[Job] = []
        if root is None:
            return jobs
        for it in root.iterfind("./channel/item"):
            places = []
            for loc in it.iterfind(f"{TT}locations/{TT}location"):
                text = ", ".join(t for t in (_text(loc, f"{TT}city"), _text(loc, f"{TT}country")) if t)
                text = text or _text(loc, f"{TT}name")
                if text and text not in places:
                    places.append(text)
            location = "; ".join(places)
            status = _text(it, "remoteStatus").lower()
            workplace = _REMOTE_STATUS.get(status) or infer_workplace_type(location)
            if workplace == REMOTE and not location:
                location = "Remote"
            link = _text(it, "link")
            jobs.append(Job(
                source=self.source,
                company=company["name"],
                company_sector=company.get("sector"),
                external_id=_text(it, "guid") or link,
                title=_text(it, "title"),
                location_text=location,
                workplace_type=workplace,
                country_bucket=infer_country_bucket(location),
                url=link,
                description_text=strip_html(it.findtext("description")),
                department=_text(it, f"{TT}department") or None,
                compensation=None,
                posted_at=rfc822_to_iso(it.findtext("pubDate")),
                raw={"guid": _text(it, "guid"), "link": link, "remoteStatus": status},
            ))
        return jobs
