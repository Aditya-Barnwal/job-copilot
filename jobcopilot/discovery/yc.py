"""YC companies from the community yc-oss dataset (docs/DESIGN.md §4).

YC's own sites (ycombinator.com, workatastartup.com) are never automated — the
HTTP client refuses them outright. yc-oss republishes YC's directory as static
JSON on GitHub Pages; we take only names, websites, batch and size from it to
seed the companies table. Jobs then come from each company's own ATS board.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .. import companies as co
from .. import config
from ..http import HttpError, PoliteClient, default_client

log = logging.getLogger("jobcopilot.discovery.yc")

SOURCE = "yc-oss"
DEFAULT_URL = "https://yc-oss.github.io/api/companies/hiring.json"


def _sector(item: dict) -> str:
    """'fintech' or 'tech' — the coarse value the sector filter understands."""
    text = " ".join([str(item.get("industry") or ""), str(item.get("subindustry") or "")]
                    + [str(x) for x in item.get("industries") or []]).lower()
    return "fintech" if "fintech" in text else "tech"


def parse(data: Any) -> list[dict]:
    """yc-oss company list -> [{'name': ..., <upsert_company fields>}]. Pure."""
    if not isinstance(data, list):
        raise ValueError("yc-oss: expected a JSON list of companies")
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict) or not (item.get("name") or "").strip():
            continue
        if str(item.get("status") or "").lower() in ("inactive", "dead"):
            continue
        industries = [str(x) for x in item.get("industries") or [] if x]
        regions = [str(x) for x in item.get("regions") or [] if x]
        notes = "; ".join(p for p in (
            f"yc-oss: {item['one_liner']}" if item.get("one_liner") else "",
            f"industries: {', '.join(industries)}" if industries else "",
            f"regions: {', '.join(regions)}" if regions else "",
        ) if p)
        team = item.get("team_size")
        out.append({
            "name": item["name"].strip(),
            "website": (item.get("website") or "").strip() or None,
            "yc_batch": co.short_yc_batch(item.get("batch")),
            "yc_top_company": bool(item.get("top_company")),
            "team_size": int(team) if isinstance(team, (int, float)) and team > 0 else None,
            "sector": _sector(item),
            "hq_location": (item.get("all_locations") or "").strip() or None,
            "notes": notes or None,
        })
    return out


def run(*, client: Optional[PoliteClient] = None, db_path: Any = None,
        url: Optional[str] = None, force: bool = False) -> dict:
    """Fetch hiring.json (conditional GET), upsert changed data, recompute tiers."""
    cfg = (config.sources().get("discovery") or {}).get("yc_oss") or {}
    if not cfg.get("enabled", True) and not force:
        return {"source": SOURCE, "skipped": "disabled in config/sources.yaml"}
    url = url or cfg.get("url") or DEFAULT_URL
    client = client or default_client()
    state = co.get_source_state(SOURCE, db_path)
    now = time.time()
    headers = {} if force else co.conditional_headers(state)
    try:
        resp = client.get(url, headers=headers or None)
        if resp.status_code == 304:
            co.put_source_state(SOURCE, db_path, last_run=now, last_ok=now, last_error=None)
            return {"source": SOURCE, "unchanged": True, "upserted": 0}
        # a response without validators keeps the stored ones (as bsm.py does)
        validators = {"etag": resp.headers.get("ETag") or state.get("etag"),
                      "last_modified": resp.headers.get("Last-Modified") or state.get("last_modified")}
        body = resp.content
        digest = co.content_hash(body)
        if digest == state.get("content_hash") and not force:
            co.put_source_state(SOURCE, db_path, last_run=now, last_ok=now, last_error=None,
                                **validators)
            return {"source": SOURCE, "unchanged": True, "upserted": 0}
        rows = parse(resp.json())
    except (HttpError, ValueError) as exc:
        log.error("yc-oss fetch/parse failed: %s", exc)
        co.put_source_state(SOURCE, db_path, last_run=now, last_error=str(exc)[:500])
        return {"source": SOURCE, "error": str(exc)}

    failed = 0
    for r in rows:
        name = r.pop("name")
        try:
            co.upsert_company(name, db_path=db_path, signal_source=SOURCE, **r)
        except (ValueError, TypeError) as exc:   # one bad row never sinks the import
            failed += 1
            log.warning("yc-oss: skipped %r: %s", name, exc)
    tiers = co.recompute_tiers(db_path)
    co.put_source_state(SOURCE, db_path, **validators, content_hash=digest,
                        last_run=now, last_ok=now, last_error=None, items=len(rows))
    log.info("yc-oss: %d companies upserted (%d failed)", len(rows) - failed, failed)
    return {"source": SOURCE, "unchanged": False, "upserted": len(rows) - failed,
            "failed": failed, "tiers": tiers}
