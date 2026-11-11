"""We Work Remotely category RSS feeds (board connector).

  GET https://weworkremotely.com/categories/<category>.rss   (one per `feeds` entry)

RSS only: the HTML is Cloudflare-challenged and the API terms forbid storing,
so this board is `store: minimal` — title / company / region / link, no
description (description_text stays "", raw keeps only those fields). Item
titles are "Company: Role"; `region` is e.g. "USA Only" or "Anywhere in the
World". Links are attributed back via attribution_url.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import urlsplit

from .base import Connector
from ..http import HttpError
from ..models import Job, REMOTE
from ..normalize import infer_country_bucket
from ..regions import partition_location, parse_utc_offsets

log = logging.getLogger("jobcopilot.connectors.weworkremotely")

RSS_HEADERS = {"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.1"}
DEFAULT_FEEDS = ["https://weworkremotely.com/categories/remote-programming-jobs.rss"]


def _iso(value: Optional[str]) -> Optional[str]:
    """RFC 822 (pubDate) or ISO (expires_at) -> ISO 8601."""
    if not value or not value.strip():
        return None
    v = value.strip()
    try:
        return parsedate_to_datetime(v).isoformat()
    except (TypeError, ValueError, IndexError):
        return v if v[:4].isdigit() else None


def split_title(title: str) -> tuple[str, str]:
    """'Lithic: Senior Software Engineer, Product Platform' -> ('Lithic', 'Senior …').
    No colon -> ('', title): we don't invent an employer."""
    company, sep, role = title.partition(":")
    if not sep or not role.strip():
        return "", title.strip()
    return company.strip(), role.strip()


def _slug(link: str) -> str:
    path = urlsplit(link).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else link


class WeWorkRemotelyConnector(Connector):
    source = "weworkremotely"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        """List of feed bodies. A failing feed is skipped (logged) unless all fail."""
        feeds = (company or {}).get("feeds") or DEFAULT_FEEDS
        bodies: list[str] = []
        last_err: Optional[Exception] = None
        for url in feeds:
            try:
                bodies.append(self.client.get(url, headers=RSS_HEADERS, timeout=self.timeout).text)
            except HttpError as e:
                last_err = e
                log.warning("weworkremotely: feed failed: %s", e)
        if not bodies and last_err is not None:
            raise last_err
        return bodies

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        board = company or {}
        attr = board.get("attribution") or {}
        bodies = raw if isinstance(raw, list) else [raw]
        jobs: list[Job] = []
        seen: set[str] = set()
        for body in bodies:
            if not body:
                continue
            root = ET.fromstring(body.encode("utf-8") if isinstance(body, str) else body)
            for it in root.iterfind("./channel/item"):
                link = (it.findtext("link") or it.findtext("guid") or "").strip()
                external_id = _slug(link) if link else (it.findtext("guid") or "").strip()
                if not external_id or external_id in seen:
                    continue   # same job listed in two category feeds
                seen.add(external_id)
                company_name, title = split_title(it.findtext("title") or "")
                region = (it.findtext("region") or "").strip()
                regions, tz_text = partition_location(region)
                category = (it.findtext("category") or "").strip() or None
                posted = _iso(it.findtext("pubDate"))
                expires = _iso(it.findtext("expires_at"))
                jobs.append(Job(
                    source=self.source,
                    company=company_name,
                    company_sector=None,
                    external_id=external_id,
                    title=title,
                    location_text=region or "Remote",
                    workplace_type=REMOTE,
                    country_bucket=infer_country_bucket(region),
                    url=link,
                    description_text="",       # store: minimal (terms)
                    department=category,
                    posted_at=posted,
                    apply_url=link or None,     # RSS has no separate apply link
                    region_restrictions=regions,
                    timezone_restrictions=parse_utc_offsets(tz_text) if tz_text else [],
                    tz_requirement_text=tz_text,
                    attribution_name=attr.get("name") or "We Work Remotely",
                    attribution_url=link or attr.get("url") or "https://weworkremotely.com",
                    expires_at=expires,
                    raw={"title": it.findtext("title"), "link": link, "region": region,
                         "category": category, "pubDate": posted, "expires_at": expires},
                ))
        return jobs
