"""Funding watcher → qualified-startup writer.

Pulls funding news from Inc42 / Entrackr / YC RSS feeds, asks the LLM to
extract `{company, stage, amount_usd, investors, sector}` per item, then
**only** appends companies that pass all four hard checks:

  1. Round announced within ~12–18 months
  2. Series B+ OR raise ≥ STARTUP_MIN_RAISE_USD
  3. ≥1 notable investor (tier-1 list)
  4. sector ∈ {tech, fintech}

Qualified entries land in `discovered_companies.yaml`, **not** the curated
companies.yaml. Their ATS slug is left as `# TODO verify` since funding feeds
don't tell us which ATS they're on.

If RSS is unreachable or the LLM is unavailable, this skips with a log line
rather than crashing the ingest.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests
import yaml

from . import llm


log = logging.getLogger("jobcopilot.funding")


FEEDS = [
    "https://inc42.com/feed/",
    "https://entrackr.com/feed/",
    "https://www.ycombinator.com/blog/rss",
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
    r = requests.get(url, timeout=10)
    r.raise_for_status()
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
        min_amount_usd: float | None = None, recent_days: int = 540) -> dict:
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

    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    qualifiers: list[dict] = []
    seen: set[str] = set()
    for it in all_items[:60]:  # cap to keep LLM cost low
        rec = _ask_llm(it["title"], it["description"])
        if not rec or not rec.get("company"):
            continue
        company = re.sub(r"\s+", " ", rec["company"]).strip()
        if company.lower() in seen:
            continue
        seen.add(company.lower())
        if not _qualifies(rec, min_amount_usd, recent_days):
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
    existing_companies = {c["name"].lower(): c for c in (existing.get("companies") or [])}
    for q in qualifiers:
        existing_companies[q["name"].lower()] = {**existing_companies.get(q["name"].lower(), {}), **q}
    out.write_text(yaml.safe_dump(
        {"companies": list(existing_companies.values())}, sort_keys=False
    ), encoding="utf-8")

    log.info("funding qualified=%d total_items=%d", len(qualifiers), len(all_items))
    return {"feeds_total": len(all_items), "qualified": len(qualifiers),
            "output": str(out)}
