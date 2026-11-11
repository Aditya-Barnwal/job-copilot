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
    ("R$", "BRL"), ("USD", "USD"), ("EUR", "EUR"), ("GBP", "GBP"), ("CAD", "CAD"),
    ("AUD", "AUD"), ("INR", "INR"), ("BRL", "BRL"), ("€", "EUR"), ("£", "GBP"),
    ("₹", "INR"), ("$", "USD"),
)
_KNOWN_DOLLAR_PREFIXES = {"US", "CA", "C", "A", "AU", "R"}
_DOLLAR_PREFIX_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{1,3})\$")
# A number with optional ./, groups, and a k/m suffix that TOUCHES it and ends
# at a word boundary: "60k", "1.2M" yes; "$2000 min", "4,000 monthly" no.
_AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)*)(?:([kKmM])(?![A-Za-z]))?")
_RANGE_GAP_RE = re.compile(r"\s*(?:[-–—~]|to)\s*(?:[A-Za-z]{0,3}\$|[€£₹]|[A-Z]{3})?\s*", re.I)

# Same idea as remoteok.MAX_PLAUSIBLE_USD_YEAR, per currency (rough units per
# USD): an annualised figure above this is a typo or a misread, so we keep the
# text only rather than a wrong number.
MAX_PLAUSIBLE_USD_YEAR = 5_000_000
_UNITS_PER_USD = {"USD": 1, "EUR": 1, "GBP": 1, "CAD": 1.5, "AUD": 1.6, "INR": 100, "BRL": 6}
_PER_YEAR = {"1 HOUR": 2080, "1 WEEK": 52, "1 MONTH": 12, "1 YEAR": 1}


def rfc822_to_iso(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip()).isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def _number(num: str) -> Optional[float]:
    """'60.000' / '60,000' -> 60000 (a 3-digit group is thousands), '1.5' ->
    1.5, '1.234,56' -> 1234.56. Anything else ambiguous -> None."""
    if re.fullmatch(r"\d{1,2}(?:,\d{2})+,\d{3}", num):   # Indian grouping: 12,00,000
        return float(num.replace(",", ""))
    parts = re.split(r"([.,])", num)
    out, decimal = parts[0], None
    for i in range(1, len(parts), 2):
        group = parts[i + 1]
        last = i + 2 >= len(parts)
        if len(group) == 3 and decimal is None:
            out += group
        elif last and decimal is None:
            decimal = group
        else:
            return None
    try:
        return float(out + ("." + decimal if decimal else ""))
    except ValueError:
        return None


def _amount(num: str, suffix: Optional[str]) -> Optional[float]:
    v = _number(num)
    if v is None:
        return None
    return v * {"k": 1e3, "m": 1e6}.get((suffix or "").lower(), 1.0)


def _currency(s: str) -> Optional[str]:
    for m in _DOLLAR_PREFIX_RE.finditer(s):
        if m.group(1).upper() not in _KNOWN_DOLLAR_PREFIXES:
            return None            # MX$, NZ$, S$ ...: not USD, and not ours to guess
    return next((code for sym, code in _CURRENCIES if sym in s.upper() or sym in s), None)


def parse_salary_text(text: Optional[str]) -> Optional[Compensation]:
    """Best effort on strings like 'US$54k - US$90k' or '€60,000 per year'.

    Numbers are kept only when the currency is stated (a bare number would be
    read as INR downstream) and the annualised figure is plausible; otherwise
    the text is kept as a summary with is_estimate True, so nothing pretends
    to be a figure it isn't.
    """
    if not text or not text.strip():
        return None
    s = text.strip()
    summary_only = Compensation(summary=s, source="jobspresso", is_estimate=True)
    cur = _currency(s)
    matches = list(_AMOUNT_RE.finditer(s))[:2]
    values = [_amount(m.group(1), m.group(2)) for m in matches]
    if cur is None or not values or any(v is None for v in values):
        return summary_only
    # "60-80k": the trailing k/m on the last number of a range covers the range.
    if len(matches) == 2 and matches[1].group(2) and not matches[0].group(2) \
            and _RANGE_GAP_RE.fullmatch(s[matches[0].end():matches[1].start()]):
        values[0] = _amount(matches[0].group(1), matches[1].group(2))
    values = [v for v in values if v]
    if not values:
        return summary_only
    low = s.lower()
    interval = ("1 HOUR" if re.search(r"hour|/\s*hr\b|/\s*h\b", low) else
                "1 MONTH" if "month" in low else
                "1 WEEK" if "week" in low else "1 YEAR")
    cap = MAX_PLAUSIBLE_USD_YEAR * _UNITS_PER_USD.get(cur, 1)
    if max(values) * _PER_YEAR[interval] > cap:
        return summary_only
    return Compensation(currency=cur, min_value=min(values), max_value=max(values),
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
