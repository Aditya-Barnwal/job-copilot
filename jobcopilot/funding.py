"""Funding watcher → qualified-startup writer.

Pulls funding news from Inc42 / Entrackr / YC RSS feeds, asks the LLM to
extract `{company, stage, amount_usd, investors, sector}` per item, then
**only** appends companies that pass all four hard checks:

  1. Round announced within ~12–18 months
  2. Series B+ OR raise ≥ STARTUP_MIN_RAISE_USD
  3. ≥1 notable investor (tier-1 list)
  4. sector ∈ {tech, fintech}

Qualified entries are written to the `companies` table (signal source
"funding-rss", which makes them tier "funded") and, for back-compat, to
`discovered_companies.yaml` — never to the curated companies.yaml. Their ATS
is unknown (funding feeds don't say), so the detector has to find it.

Recency (F12): items whose RSS pubDate is older than `recent_days`, or missing
/ unparseable, are dropped BEFORE the LLM call — an old round never qualifies
and never costs tokens.

If RSS is unreachable or the LLM is unavailable, this skips with a log line
rather than crashing the ingest. LLM output is untrusted: it is coerced
(dict check, str stage/sector, numeric-or-0 amount, null investors dropped)
and every item is handled in its own try/except.

A funding headline gives a name, never a domain, so record_companies() merges
into an existing company only when exactly ONE company has that name; with two
or more same-named companies (e.g. 'atlas' and 'atlas#atlashq.in') the round is
stored as its own row under the key '<name>#funding-rss' instead of marking a
possibly-wrong company funded.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import yaml

from . import llm


log = logging.getLogger("jobcopilot.funding")


FEEDS = [
    "https://inc42.com/feed/",
    "https://entrackr.com/feed/",
    # No ycombinator.com feed: YC's terms ban automated access to all its
    # subdomains (docs/SOURCES.md). YC companies come from the yc-oss dataset.
]


TIER1 = {
    "sequoia", "peak xv", "accel", "lightspeed", "matrix", "tiger global",
    "softbank", "general catalyst", "kleiner perkins", "andreessen horowitz",
    "a16z", "founders fund", "khosla", "elevation", "elevate", "nexus",
    "saif", "blume", "y combinator", "yc", "battery ventures", "iconiq",
    "insight partners", "redpoint", "greylock", "felicis", "bessemer",
    "stripes", "altimeter", "coatue", "drv", "norwest", "ribbit",
}


STAGE_LEVELS = {
    "pre-seed": 0, "preseed": 0, "seed": 1, "pre-series a": 1,
    "series a": 2, "series b": 3, "series c": 4, "series d": 5,
    "series e": 6, "series f": 7,
}


def _parse_rss(url: str) -> list[dict]:
    # Shared polite client: honest UA, robots.txt, per-host rate limit, retries.
    from .http import default_client
    r = default_client().get(url, timeout=10)
    items = []
    root = ET.fromstring(r.content)
    for it in root.iter("item"):
        items.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "pub_date": (it.findtext("pubDate") or "").strip(),
            "description": (it.findtext("description") or "").strip(),
        })
    return items


_EXTRACT_PROMPT = (
    "You extract structured info from a funding-news headline. "
    "Return ONLY valid JSON, no prose, no code fences. "
    "Schema: {\"company\": str, \"stage\": str, \"amount_usd\": number|null, "
    "\"investors\": [str], \"sector\": \"tech\"|\"fintech\"|\"other\"}. "
    "Stage examples: \"seed\", \"series a\", \"series b\", \"series c\". "
    "amount_usd is total USD raised (convert from inr/eur if needed). "
    "If a field is unknown, use null or an empty list."
)


def _pub_datetime(pub_date: str) -> Optional[datetime]:
    """RFC 822 pubDate (or ISO 8601) -> aware UTC datetime; None if unparseable."""
    if not pub_date:
        return None
    try:
        dt = parsedate_to_datetime(pub_date)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(pub_date.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_recent(pub_date: str, recent_days: int, now: Optional[datetime] = None) -> bool:
    """True only for a parseable pubDate within the last `recent_days` days."""
    dt = _pub_datetime(pub_date)
    if dt is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - timedelta(days=recent_days) <= dt <= now + timedelta(days=2)


def _num(v) -> float:
    """amount_usd from the LLM: a number (or numeric string) else 0."""
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v) if v == v and v > 0 else 0.0   # NaN / negative -> 0
    if isinstance(v, str):
        try:
            f = float(v.replace(",", "").replace("$", "").strip())
            return f if f == f and f > 0 else 0.0
        except ValueError:
            return 0.0
    return 0.0


def _coerce(rec) -> Optional[dict]:
    """Validate one LLM extraction; None if unusable."""
    if not isinstance(rec, dict):
        return None
    company = rec.get("company")
    if not isinstance(company, str) or not company.strip():
        return None
    inv = rec.get("investors")
    if isinstance(inv, str):
        inv = [inv]
    investors = [str(i).strip() for i in (inv if isinstance(inv, list) else [])
                 if i is not None and not isinstance(i, (dict, list)) and str(i).strip()]
    stage = rec.get("stage")
    sector = rec.get("sector")
    return {
        "company": re.sub(r"\s+", " ", company).strip()[:200],
        "stage": str(stage).strip()[:40] if stage is not None and not isinstance(stage, (dict, list)) else "",
        "amount_usd": _num(rec.get("amount_usd")),
        "investors": investors,
        "sector": str(sector).strip().lower() if isinstance(sector, str) else "",
    }


def record_companies(qualifiers: list[dict], db_path=None) -> int:
    """Upsert qualified companies into the companies table (tier "funded" via
    signal source funding-rss), then recompute tiers. Returns rows written.
    Name-only: an existing company is updated only if it is the ONLY one with
    that name; otherwise the round is kept as a separate '<name>#funding-rss' row."""
    from . import companies
    from .normalize import normalize_company_name
    n = 0
    for q in qualifiers:
        try:
            amount = _num(q.get("amount_usd"))
            note = "funding-rss: " + ", ".join(p for p in (
                str(q.get("stage") or ""),
                f"${amount / 1e6:.0f}M" if amount else "",
                str(q.get("source_url") or ""),
            ) if p)
            name = str(q["name"])
            fields = dict(db_path=db_path, signal_source="funding-rss",
                          funding_stage=str(q.get("stage") or "") or None,
                          investors=[str(i) for i in q.get("investors") or [] if i is not None],
                          sector=str(q.get("sector") or "tech"), notes=note)
            same = companies.name_candidates(name, db_path)
            if len(same) > 1:
                key = f"{normalize_company_name(name)}#funding-rss"
                log.info("funding: %r matches %d companies; stored separately as %s",
                         name, len(same), key)
                fields["notes"] = (note + f"; ambiguous name: {len(same)} companies called "
                                   f"{name!r}, not merged - link by hand")
                companies.upsert_company(name, match_key=key, **fields)
            elif same:
                companies.upsert_company(name, company_id=same[0]["id"], **fields)
            else:
                companies.upsert_company(name, **fields)
            n += 1
        except Exception as exc:   # noqa: BLE001 — one bad item never aborts the run
            log.warning("funding: could not record %r: %s", q.get("name"), exc)
    if n:
        companies.recompute_tiers(db_path)
    return n


def _ask_llm(headline: str, description: str) -> Optional[dict]:
    user = f"Headline: {headline}\nSummary: {description[:1000]}"
    try:
        raw = llm.complete(_EXTRACT_PROMPT, user, temperature=0.1, max_tokens=300)
        return llm.parse_json(raw)
    except Exception as exc:
        log.warning("llm extract failed: %s", exc)
        return None


def _qualifies(rec: dict, min_amount_usd: float, recent_days: int) -> bool:
    sector = (rec.get("sector") or "").lower()
    if sector not in ("tech", "fintech"):
        return False
    stage = (rec.get("stage") or "").lower().strip()
    stage_level = STAGE_LEVELS.get(stage)
    amount = rec.get("amount_usd") or 0
    investors = [i.lower() for i in (rec.get("investors") or [])]
    has_tier1 = any(any(t in inv for t in TIER1) for inv in investors)
    big_enough = (stage_level is not None and stage_level >= 3) or (amount and amount >= min_amount_usd)
    return bool(big_enough and has_tier1)


def run(out_path: str | Path = "discovered_companies.yaml",
        min_amount_usd: float | None = None, recent_days: int = 540,
        db_path=None) -> dict:
    """Returns a summary dict; writes/updates `out_path` with qualifiers."""
    min_amount_usd = float(min_amount_usd
                           if min_amount_usd is not None
                           else os.environ.get("STARTUP_MIN_RAISE_USD", "15000000"))

    all_items: list[dict] = []
    for url in FEEDS:
        try:
            items = _parse_rss(url)
            log.info("funding feed %s items=%d", url, len(items))
            all_items.extend(items)
        except Exception as exc:
            log.warning("funding feed unreachable %s: %s", url, exc)

    if not all_items:
        return {"feeds_total": 0, "qualified": 0, "skipped_reason": "no RSS reachable"}

    recent = [it for it in all_items if is_recent(it.get("pub_date") or "", recent_days)]
    log.info("funding: %d/%d items within %d days", len(recent), len(all_items), recent_days)
    qualifiers: list[dict] = []
    seen: set[str] = set()
    for it in recent[:60]:  # cap to keep LLM cost low
        try:
            rec = _coerce(_ask_llm(str(it.get("title") or ""), str(it.get("description") or "")))
            if not rec:
                continue
            company = rec["company"]
            if company.lower() in seen:
                continue
            seen.add(company.lower())
            if not _qualifies(rec, min_amount_usd, recent_days):
                continue
        except Exception as exc:   # noqa: BLE001 — one bad item never aborts the run
            log.warning("funding: skipped item %r: %s", it.get("title"), exc)
            continue
        qualifiers.append({
            "name": company,
            "sector": rec.get("sector") or "tech",
            "connector": "manual",
            "verify": True,
            "funded_startup": True,
            "stage": rec.get("stage"),
            "amount_usd": rec.get("amount_usd"),
            "investors": rec.get("investors") or [],
            "source_url": it.get("link"),
            "pub_date": it.get("pub_date"),
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "note": "discovered via funding watcher; verify ATS before enabling live ingest",
        })

    out = Path(out_path)
    existing: dict = {}
    if out.exists():
        try:
            existing = yaml.safe_load(out.read_text(encoding="utf-8")) or {}
        except Exception:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing_companies = {str(c["name"]).lower(): c for c in (existing.get("companies") or [])
                          if isinstance(c, dict) and c.get("name")}
    for q in qualifiers:
        existing_companies[q["name"].lower()] = {**existing_companies.get(q["name"].lower(), {}), **q}
    out.write_text(yaml.safe_dump(
        {"companies": list(existing_companies.values())}, sort_keys=False
    ), encoding="utf-8")

    recorded = record_companies(qualifiers, db_path)
    log.info("funding qualified=%d recorded=%d total_items=%d", len(qualifiers), recorded, len(all_items))
    return {"feeds_total": len(all_items), "recent": len(recent), "qualified": len(qualifiers),
            "recorded": recorded, "output": str(out)}
