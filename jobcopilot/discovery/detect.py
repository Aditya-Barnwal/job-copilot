"""ATS detection step: find + verify the ATS of brand/funded companies (docs/DESIGN.md §4).

`python -m jobcopilot.discovery detect` (also part of `all`). Per run, at most
`limit` companies (default 40) are checked, oldest-checked first, skipping any
checked in the last `min_age_days` (default 14). A row qualifies when it is
enabled, tier brand/funded, has a careers_url or website on a real (non-shared)
domain, and has no verified ATS (ats_confidence < VERIFIED_CONFIDENCE).
Config-sourced rows are skipped: their ATS is the human's call (config wins on
every sync anyway).

Each URL goes to jobcopilot.ats_detect.detect(url, verify=True). Results are
stored by companies.record_ats_detection(): ATS fields at confidence 1.0 only
when the detector verified the board, otherwise at the detector's confidence;
ats_checked_at + ats_evidence are always written, also on failure, so a broken
site waits min_age_days instead of being retried every run.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from .. import companies as co
from .. import store

log = logging.getLogger("jobcopilot.discovery.detect")

DEFAULT_LIMIT = 40
DEFAULT_MIN_AGE_DAYS = 14
TIERS = ("brand", "funded")


def candidates(*, db_path: Any = None, limit: int = DEFAULT_LIMIT,
               min_age_days: float = DEFAULT_MIN_AGE_DAYS, now: Optional[float] = None) -> list[dict]:
    """Rows due for detection, oldest-checked (never-checked first), capped at `limit`."""
    now = time.time() if now is None else float(now)
    cutoff = now - float(min_age_days) * 86400
    with store.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM companies WHERE tier IN (?, ?) AND COALESCE(enabled, 1) = 1 "
            "AND COALESCE(ats_confidence, 0) < ? "
            "AND (ats_checked_at IS NULL OR ats_checked_at < ?) "
            "ORDER BY COALESCE(ats_checked_at, 0), id",
            (*TIERS, co.VERIFIED_CONFIDENCE, cutoff)).fetchall()
    out: list[dict] = []
    for raw in rows:
        c = co._decode(raw)
        if "config" in c["signal_sources"]:
            continue
        url = next((u for u in (c.get("careers_url"), c.get("website")) if u and co.usable_domain(u)), None)
        if not url:
            continue
        c["_detect_url"] = url if "://" in url else "https://" + url
        out.append(c)
        if len(out) >= max(0, int(limit)):
            break
    return out


def run(*, client: Any = None, db_path: Any = None, limit: int = DEFAULT_LIMIT,
        min_age_days: float = DEFAULT_MIN_AGE_DAYS, detector: Optional[Callable[..., Any]] = None,
        now: Optional[float] = None) -> dict:
    """Detect + store ATS for up to `limit` due companies. Never raises per company."""
    if detector is None:
        from ..ats_detect import detect as detector   # noqa: N813 — public API
    now = time.time() if now is None else float(now)
    out: dict[str, Any] = {"checked": 0, "verified": 0, "unverified": 0, "not_found": 0,
                           "errors": 0, "limit": int(limit)}
    for c in candidates(db_path=db_path, limit=limit, min_age_days=min_age_days, now=now):
        url = c["_detect_url"]
        try:
            res = detector(url, client=client, verify=True)
        except Exception as exc:   # noqa: BLE001 — one bad site never sinks the run
            out["errors"] += 1
            log.warning("detect %s (%s) failed: %s", c["name"], url, exc)
            res = type("Failed", (), {"ats": None, "verified": False, "confidence": 0.0,
                                      "evidence": f"detect error: {type(exc).__name__}: {exc}"[:300]})()
        try:
            co.record_ats_detection(c["id"], res, checked_at=now, db_path=db_path)
        except Exception as exc:   # noqa: BLE001
            out["errors"] += 1
            log.warning("detect %s: could not store result: %s", c["name"], exc)
            continue
        out["checked"] += 1
        if getattr(res, "verified", False):
            out["verified"] += 1
        elif getattr(res, "ats", None) and res.ats != "manual":
            out["unverified"] += 1
        else:
            out["not_found"] += 1
    log.info("ats detect: %s", out)
    return out
