"""Helpers shared by the API routers.

Company metadata (apply_method, contact_email, funded_startup) lives in
companies.yaml rather than the jobs table, so job rows are enriched with it at
response time. It is loaded once by the app lifespan and lazily otherwise, so a
router still works when mounted without the lifespan (e.g. a bare TestClient).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from jobcopilot import config
from jobcopilot.ingest import load_companies

log = logging.getLogger("jobcopilot.server")

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

from jobcopilot.store import ALL_STATUSES as JOB_STATUSES  # tracker + legacy statuses (one list)

_company_meta: Optional[dict[str, dict[str, Any]]] = None


def load_company_meta(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """(Re)load company metadata. A bad or missing file means no enrichment,
    never a failed request."""
    global _company_meta
    try:
        companies = load_companies(str(path or config.companies_path()))
        meta = {
            c["name"]: {
                "apply_method": c.get("apply_method"),
                "contact_email": c.get("contact_email"),
                "funded_startup": c.get("funded_startup", False),
            }
            for c in companies if c.get("name")
        }
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        log.warning("company meta unavailable: %s", type(exc).__name__)
        meta = {}
    _company_meta = meta
    return meta


def company_meta() -> dict[str, dict[str, Any]]:
    return _company_meta if _company_meta is not None else load_company_meta()


def enrich(rows: list[dict]) -> list[dict]:
    """Copy non-null company metadata onto each job row (in place)."""
    meta_by_company = company_meta()
    for r in rows:
        meta = meta_by_company.get(r.get("company")) or {}
        for k, v in meta.items():
            if v is not None:
                r[k] = v
    return rows


def filter_max_yoe(rows: list[dict], max_yoe: Optional[int]) -> list[dict]:
    """Keep rows whose hard YOE floor is unknown, soft ("preferred"), or ≤ max_yoe."""
    if max_yoe is None:
        return rows
    return [
        r for r in rows
        if (r.get("min_yoe_required") is None
            or r.get("yoe_is_preferred")
            or r["min_yoe_required"] <= max_yoe)
    ]
