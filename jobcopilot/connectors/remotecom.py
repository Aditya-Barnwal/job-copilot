"""Remote.com's own open roles (public JSON; one employer, board-shaped).

  GET https://apply.remote.com/api/public/jobs

Every listing is a role at Remote itself, all remote, tagged with regions
("EMEA", "LATAM, NORAM", "APAC"). The listing page is
https://apply.remote.com/jobs/<public_id> (seen in their open-roles page).
apply.remote.com/robots.txt (re-checked 2026-11-12) answers 200 with the SPA's
HTML, i.e. no rules -> allowed. `pay.rows` holds the posted range; numbers are
kept only for a single row with a currency (two rows = two locations' ranges,
kept as text only).
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import quote

from .base import Connector, DEFAULT_HEADERS
from ..models import Job, Compensation, REMOTE, HYBRID, ONSITE, UNKNOWN
from ..normalize import strip_html, infer_country_bucket
from ..regions import partition_location, parse_utc_offsets

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_WORKPLACE = {"remote": REMOTE, "hybrid": HYBRID, "onsite": ONSITE, "on-site": ONSITE, "office": ONSITE}
_RAW_DROP = ("pay_doc",)   # rich-text blob, large and duplicated by pay_text


def _money(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _compensation(j: dict[str, Any]) -> Optional[Compensation]:
    pay = j.get("pay") if isinstance(j.get("pay"), dict) else {}
    rows = [r for r in pay.get("rows") or [] if isinstance(r, dict)]
    summary = str(pay.get("range_text") or "").strip() or None
    if len(rows) == 1:
        r = rows[0]
        lo, hi = _money(r.get("min")), _money(r.get("max"))
        cur = str(r.get("currency") or "").strip().upper() or None
        if cur and (lo is not None or hi is not None):
            # Period isn't stated; ranges like "€57,500 to €64,650" are annual,
            # which is also what annual_inr assumes for interval None.
            return Compensation(currency=cur, min_value=lo, max_value=hi, interval=None,
                                summary=summary, source="remotecom", is_estimate=False)
    if summary:
        return Compensation(summary=summary, source="remotecom", is_estimate=False)
    return None


class RemoteComConnector(Connector):
    source = "remotecom"
    URL = "https://apply.remote.com/api/public/jobs"
    JOB_URL = "https://apply.remote.com/jobs/{id}"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return self.client.get_json(self.URL, headers=DEFAULT_HEADERS, timeout=self.timeout)

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        board = company or {}
        attr = board.get("attribution") or {}
        employer = str(board.get("company_name") or "Remote")
        jobs: list[Job] = []
        seen: set[str] = set()
        for j in (raw or {}).get("jobs") or []:
            if not isinstance(j, dict):
                continue
            pid = str(j.get("public_id") or "")
            if not _UUID_RE.match(pid) or pid in seen:
                continue
            if str(j.get("status") or "open") != "open" or j.get("accepting_applications") is False:
                continue
            seen.add(pid)
            listing = self.JOB_URL.format(id=quote(pid, safe=""))
            location = str(j.get("location") or "").strip()
            workplace = _WORKPLACE.get(str(j.get("workplace") or "").lower(), UNKNOWN)
            regions, tz_text = partition_location(location)
            jobs.append(Job(
                source=self.source,
                company=employer,
                company_sector=None,
                external_id=pid,
                title=str(j.get("title") or "").strip(),
                location_text=location or ("Remote" if workplace == REMOTE else ""),
                workplace_type=workplace,
                country_bucket=infer_country_bucket(location),
                url=listing,
                description_text=strip_html(j.get("description")),
                department=str(j.get("department") or "").strip() or None,
                compensation=_compensation(j),
                posted_at=str(j.get("opened_at") or "").strip() or None,
                apply_url=listing,
                region_restrictions=regions if workplace == REMOTE else [],
                timezone_restrictions=parse_utc_offsets(tz_text) if tz_text else [],
                tz_requirement_text=tz_text,
                attribution_name=attr.get("name") or "Remote",
                attribution_url=listing,
                raw={k: v for k, v in j.items() if k not in _RAW_DROP},
            ))
        return jobs
