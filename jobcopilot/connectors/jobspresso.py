"""Jobspresso RSS feed (board connector).

  GET https://jobspresso.co/feed/job_feed/   (path form: `?feed=` is robots-disallowed)

The feed only holds the ~10 newest posts (roughly 1-2 hours), hence
poll_hours: 2 in config. Terms are personal-use only, so this board is
`store: minimal`: title / company / location / salary / link are kept and the
description is NOT (description_text stays "", raw keeps only those fields).
Parsed with xml.etree; no HTML is ever scraped.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from .base import Connector
from ..models import Job, Compensation, REMOTE
from ..normalize import infer_country_bucket
from ..regions import partition_location, parse_utc_offsets

NS = {"jl": "https://jobspresso.co", "wp": "com-wordpress:feed-additions:1"}
RSS_HEADERS = {"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.1"}

_CURRENCIES = (  # longest symbols first so "CA$" wins over "$"
    ("CA$", "CAD"), ("C$", "CAD"), ("A$", "AUD"), ("AU$", "AUD"), ("US$", "USD"),
    ("USD", "USD"), ("EUR", "EUR"), ("GBP", "GBP"), ("CAD", "CAD"), ("AUD", "AUD"),
    ("INR", "INR"), ("€", "EUR"), ("£", "GBP"), ("₹", "INR"), ("$", "USD"),
)
_AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)*)\s*([kKmM])?")


def rfc822_to_iso(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip()).isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def _amount(num: str, suffix: Optional[str]) -> Optional[float]:
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return None
    mult = {"k": 1e3, "m": 1e6}.get((suffix or "").lower(), 1.0)
    return v * mult


def parse_salary_text(text: Optional[str]) -> Optional[Compensation]:
    """Best effort on strings like 'US$54k - US$90k' or '€60,000 per year'.

    Numbers are kept only when the currency is stated (a bare number would be
    read as INR downstream); otherwise the text is kept as a summary with
    is_estimate True, so nothing pretends to be a figure it isn't.
    """
    if not text or not text.strip():
        return None
    s = text.strip()
    cur = next((code for sym, code in _CURRENCIES if sym in s.upper() or sym in s), None)
    amounts = [a for a in (_amount(n, k) for n, k in _AMOUNT_RE.findall(s)) if a]
    if not amounts or cur is None:
        return Compensation(summary=s, source="jobspresso", is_estimate=True)
    low = s.lower()
    interval = ("1 HOUR" if re.search(r"hour|/\s*hr\b|/\s*h\b", low) else
                "1 MONTH" if "month" in low else
                "1 WEEK" if "week" in low else "1 YEAR")
    return Compensation(currency=cur, min_value=min(amounts[:2]), max_value=max(amounts[:2]),
                        interval=interval, summary=s, source="jobspresso", is_estimate=False)


class JobspressoConnector(Connector):
    source = "jobspresso"
    URL = "https://jobspresso.co/feed/job_feed/"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return self.client.get(self.URL, headers=RSS_HEADERS, timeout=self.timeout).text

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        board = company or {}
        attr = board.get("attribution") or {}
        if not raw:
            return []
        root = ET.fromstring(raw.encode("utf-8") if isinstance(raw, str) else raw)
        jobs: list[Job] = []
        for it in root.iterfind("./channel/item"):
            link = (it.findtext("link") or it.findtext("guid") or "").strip()
            company_name = (it.findtext("jl:company", namespaces=NS) or "").strip()
            location = (it.findtext("jl:location", namespaces=NS) or "").strip()
            salary = (it.findtext("jl:salary", namespaces=NS) or "").strip() or None
            post_id = (it.findtext("wp:post-id", namespaces=NS) or "").strip()
            title = (it.findtext("title") or "").strip()
            posted = rfc822_to_iso(it.findtext("pubDate"))
            regions, tz_text = partition_location(location)
            jobs.append(Job(
                source=self.source,
                company=company_name,
                company_sector=None,
                external_id=post_id or link or title,
                title=title,
                location_text=location or "Remote",
                workplace_type=REMOTE,
                country_bucket=infer_country_bucket(location),
                url=link,
                description_text="",          # store: minimal (personal-use terms)
                compensation=parse_salary_text(salary),
                posted_at=posted,
                apply_url=link or None,        # the feed carries no external apply link
                region_restrictions=regions,
                timezone_restrictions=parse_utc_offsets(tz_text) if tz_text else [],
                tz_requirement_text=tz_text,
                attribution_name=attr.get("name") or "Jobspresso",
                attribution_url=link or attr.get("url") or "https://jobspresso.co",
                raw={"title": title, "link": link, "company": company_name,
                     "location": location, "salary": salary, "pubDate": posted,
                     "post_id": post_id},
            ))
        return jobs
