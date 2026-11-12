"""Hasjob Atom feed (board connector; India-focused, low volume).

  GET https://hasjob.co/feed

robots.txt (re-checked 2026-11-12) only disallows /edit, /confirm, /withdraw
and /admin. Atom -> `store: minimal` by default (D11): title, location, link.

Listing URLs are https://hasjob.co/<employer-domain>/<hashid>, so the company
is the employer's domain (e.g. "hookstech.in"). The post body's display name
is not used: posters often type a person's name there.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .base import Connector
from .realworkfromanywhere import fetch_feed, keep_description, safe_feed_root, text_of
from ..models import Job, REMOTE, ONSITE, UNKNOWN, SOURCE_HOSTS, safe_url
from ..normalize import strip_html, infer_country_bucket
from ..regions import partition_location, parse_utc_offsets

ATOM = {"a": "http://www.w3.org/2005/Atom"}
_DOMAIN_RE = re.compile(r"^(?=.{3,253}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
_ONSITE_RE = re.compile(r"\bon-?site\b|\bno wfh\b|\bin[- ]office\b", re.I)
_REMOTE_RE = re.compile(r"\bremote\b|\banywhere\b|\bwfh\b|\bwork from home\b", re.I)


def workplace_of(location: str) -> str:
    """'Hyderabad, India (on-site, no WFH)' -> onsite; 'Anywhere' -> remote."""
    if _ONSITE_RE.search(location):
        return ONSITE
    if _REMOTE_RE.search(location):
        return REMOTE
    return UNKNOWN


def _link(entry: Any) -> str:
    for ln in entry.findall("a:link", ATOM):
        if ln.get("rel") in (None, "alternate") and ln.get("href"):
            return ln.get("href").strip()
    return text_of(entry.find("a:id", ATOM))


class HasjobConnector(Connector):
    source = "hasjob"
    URL = "https://hasjob.co/feed"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return fetch_feed(self.client, self.URL, self.timeout)

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        board = company or {}
        attr = board.get("attribution") or {}
        full = keep_description(board)
        jobs: list[Job] = []
        seen: set[str] = set()
        if not raw:
            return jobs
        for e in safe_feed_root(raw).iterfind("a:entry", ATOM):
            link = safe_url(_link(e), hosts=SOURCE_HOSTS["hasjob"])
            if not link or link in seen:
                continue
            parts = urlsplit(link)
            seen.add(link)
            segs = [s for s in parts.path.split("/") if s]
            domain = segs[0].lower() if segs and _DOMAIN_RE.match(segs[0].lower()) else ""
            location = text_of(e.find("a:location", ATOM))
            workplace = workplace_of(location)
            regions, tz_text = partition_location(location) if workplace == REMOTE else ([], None)
            posted = text_of(e.find("a:published", ATOM)) or text_of(e.find("a:updated", ATOM))
            jobs.append(Job(
                source=self.source,
                company=domain,
                company_sector=None,
                external_id="/".join(segs) or link,
                title=text_of(e.find("a:title", ATOM)),
                location_text=location,
                workplace_type=workplace,
                country_bucket=infer_country_bucket(location),
                url=link,
                description_text=strip_html(e.findtext("a:content", default="", namespaces=ATOM)) if full else "",
                posted_at=posted or None,
                apply_url=link,
                region_restrictions=regions,
                timezone_restrictions=parse_utc_offsets(tz_text) if tz_text else [],
                tz_requirement_text=tz_text,
                attribution_name=attr.get("name") or "Hasjob",
                attribution_url=link or attr.get("url") or "https://hasjob.co",
                raw={"title": text_of(e.find("a:title", ATOM)), "link": link,
                     "location": location, "published": posted},
            ))
        return jobs
