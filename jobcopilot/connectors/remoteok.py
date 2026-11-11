"""Remote OK public API (board connector).

  GET https://remoteok.com/api

Element [0] of the array is a legal notice, not a job, and is skipped. Terms:
a followable link back to the listing and "Remote OK" named as the source
wherever the data is shown (attribution_* fields), no logos (dropped from
raw). robots.txt sets Crawl-delay 1, which PoliteClient honours.

salary_min / salary_max are USD per year; 0 means "not given".
"""
from __future__ import annotations

from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from ..models import Job, Compensation, REMOTE
from ..normalize import strip_html, infer_country_bucket
from ..regions import partition_location, parse_utc_offsets

_LOGO_KEYS = ("logo", "company_logo", "image")


def _demojibake(text: Optional[str]) -> str:
    """Remote OK double-encodes some UTF-8 ('â\\x80\\x94' for an em dash).
    Undo it only when the round trip is clean, else keep the text as is."""
    if not text:
        return ""
    if not any(0x80 <= ord(ch) <= 0xFF for ch in text):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


# Posters sometimes type local-currency or garbage figures (seen: 114,000,000)
# into the USD/year fields; above this we keep no number rather than a wrong one.
MAX_PLAUSIBLE_USD_YEAR = 5_000_000


def _money(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if 0 < f <= MAX_PLAUSIBLE_USD_YEAR else None


def _compensation(j: dict[str, Any]) -> Optional[Compensation]:
    lo, hi = _money(j.get("salary_min")), _money(j.get("salary_max"))
    if lo is None and hi is None:
        return None
    return Compensation(currency="USD", min_value=lo, max_value=hi, interval="1 YEAR",
                        source="remoteok", is_estimate=False)


class RemoteOKConnector(Connector):
    source = "remoteok"
    URL = "https://remoteok.com/api"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return self.client.get_json(self.URL, headers=DEFAULT_HEADERS, timeout=self.timeout)

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        board = company or {}
        attr = board.get("attribution") or {}
        jobs: list[Job] = []
        for j in raw or []:
            # Element 0 is the legal notice; anything without an id isn't a job.
            if not isinstance(j, dict) or "legal" in j or not j.get("id"):
                continue
            location = _demojibake(j.get("location")).strip()
            regions, tz_text = partition_location(location)
            listing = (j.get("url") or "").strip()
            if not listing and j.get("slug"):
                listing = f"https://remoteok.com/remote-jobs/{j['slug']}"
            jobs.append(Job(
                source=self.source,
                company=_demojibake(j.get("company")).strip(),
                company_sector=None,
                external_id=str(j["id"]),
                title=_demojibake(j.get("position")).strip(),
                location_text=location or "Remote",
                workplace_type=REMOTE,
                country_bucket=infer_country_bucket(location),
                url=listing,
                description_text=strip_html(_demojibake(j.get("description"))),
                compensation=_compensation(j),
                posted_at=j.get("date") or None,
                apply_url=(j.get("apply_url") or "").strip() or listing or None,
                region_restrictions=regions,
                timezone_restrictions=parse_utc_offsets(tz_text) if tz_text else [],
                tz_requirement_text=tz_text,
                attribution_name=attr.get("name") or "Remote OK",
                attribution_url=listing or attr.get("url") or "https://remoteok.com",
                raw={k: v for k, v in j.items() if k not in _LOGO_KEYS},
            ))
        return jobs
