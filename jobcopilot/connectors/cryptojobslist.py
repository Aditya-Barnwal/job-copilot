"""CryptoJobsList RSS feed (board connector).

  GET https://api.cryptojobslist.com/jobs.rss

robots.txt on api.cryptojobslist.com (re-checked 2026-11-12) disallows /api/
and /*/apply but not /jobs.rss. The terms page is unverified, so this RSS
source is `store: minimal` by default (board terms, docs/DESIGN.md §5): title, company (dc:creator),
location (media:location) and the cryptojobslist.com job link; no description.
Items carry no pubDate. Workplace is REMOTE only when the location says so.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .base import Connector
from .realworkfromanywhere import fetch_feed, keep_description, safe_feed_root, text_of
from ..models import Job, REMOTE, UNKNOWN
from ..normalize import strip_html, infer_country_bucket
from ..regions import partition_location, parse_utc_offsets

NS = {"dc": "http://purl.org/dc/elements/1.1/", "media": "http://search.yahoo.com/mrss/"}


class CryptoJobsListConnector(Connector):
    source = "cryptojobslist"
    URL = "https://api.cryptojobslist.com/jobs.rss"

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
        for it in safe_feed_root(raw).iterfind("./channel/item"):
            link = text_of(it.find("link")) or text_of(it.find("guid"))
            if not link.startswith("http") or link in seen:
                continue
            seen.add(link)
            location = text_of(it.find("media:location", NS))
            regions, tz_text = partition_location(location)
            remote = "remote" in location.lower() or "anywhere" in location.lower()
            # "Remote, Canada" -> the region is Canada, not the word "Remote".
            regions = [r for r in regions if r.strip().lower() != "remote"]
            jobs.append(Job(
                source=self.source,
                company=text_of(it.find("dc:creator", NS)),
                company_sector=None,
                external_id=urlsplit(link).path.rstrip("/").rsplit("/", 1)[-1] or link,
                title=text_of(it.find("title")),
                location_text=location,
                workplace_type=REMOTE if remote else UNKNOWN,
                country_bucket=infer_country_bucket(location),
                url=link,
                description_text=strip_html(it.findtext("description")) if full else "",
                apply_url=link,               # never the /apply path (robots)
                region_restrictions=regions if remote else [],
                timezone_restrictions=parse_utc_offsets(tz_text) if tz_text else [],
                tz_requirement_text=tz_text,
                attribution_name=attr.get("name") or "CryptoJobsList",
                attribution_url=link or attr.get("url") or "https://cryptojobslist.com",
                raw={"title": text_of(it.find("title")), "link": link,
                     "creator": text_of(it.find("dc:creator", NS)), "location": location},
            ))
        return jobs
