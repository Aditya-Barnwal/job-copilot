"""NoDesk remote-jobs RSS feed (board connector; ~10 newest items).

  GET https://nodesk.co/remote-jobs/index.xml

robots.txt (re-checked 2026-11-12) allows all, but carries Cloudflare
Content-Signals "search=yes, ai-train=no, use=reference" that the site makes a
condition of access. So this board is `store: minimal` by default (D11 and
use=reference): title / company / link only, nothing fed to training, no
description kept. Titles are "Role at Company"; items carry no location.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .base import Connector
from .realworkfromanywhere import (
    fetch_feed, keep_description, rfc822_iso, safe_feed_root, split_role_at_company, text_of,
)
from ..models import Job, REMOTE, COUNTRY_UNKNOWN
from ..normalize import strip_html


class NoDeskConnector(Connector):
    source = "nodesk"
    URL = "https://nodesk.co/remote-jobs/index.xml"

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
            if urlsplit(link).hostname not in ("nodesk.co", "www.nodesk.co") or link in seen:
                continue
            seen.add(link)
            title, company_name = split_role_at_company(text_of(it.find("title")))
            posted = rfc822_iso(text_of(it.find("pubDate")))
            jobs.append(Job(
                source=self.source,
                company=company_name,
                company_sector=None,
                external_id=urlsplit(link).path.rstrip("/").rsplit("/", 1)[-1] or link,
                title=title,
                location_text="Remote",
                workplace_type=REMOTE,
                country_bucket=COUNTRY_UNKNOWN,
                url=link,
                description_text=strip_html(it.findtext("description")) if full else "",
                posted_at=posted,
                apply_url=link,
                attribution_name=attr.get("name") or "NoDesk",
                attribution_url=link or attr.get("url") or "https://nodesk.co",
                raw={"title": text_of(it.find("title")), "link": link, "pubDate": posted},
            ))
        return jobs
