"""Config loading: YAML files under `config/`, merged over safe defaults.

Files (all optional — a missing file means "use the defaults below"):
  config/companies.yaml   curated company list (legacy path ./companies.yaml still read)
  config/filters.yaml     hard filters + queue knobs
  config/sources.yaml     job sources on/off + HTTP politeness
  config/schedule.yaml    nightly run timing

`JOBCOPILOT_CONFIG_DIR` overrides the directory (used by tests).
Loaded fresh on every call — these files are tiny and edited by hand, so
there is no cache to go stale.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, dict[str, Any]] = {
    "filters": {
        "yoe": {"user_yoe": 1, "max_required": 2},
        # Annual floors in each currency. Roles with no disclosed pay are kept
        # (flagged, ranked lower) — most postings don't disclose.
        "salary_floors": {"INR": 1_500_000, "USD": 30_000, "EUR": 28_000},
        "fx_to_inr": {"USD": 83.0, "EUR": 90.0},
        "geo": {
            "india_allow": ["remote", "hybrid", "onsite", "unknown"],
            "india_preferred_cities": ["bengaluru", "bangalore"],
            "outside_india_allow": ["remote"],
            "outside_india_require_india_eligible": True,
            "min_ist_overlap_hours": 3,
            "keep_unknown_country": True,
            "workday_hours": 8,          # IST overlap = workday_hours - |offset - 5.5|
            "scan_description": True,    # "US only" / "must be based in the EU" in the JD body
        },
        "seniority": {"exclude_senior_titles": True, "allow_if_jd_yoe_within_max": True},
        "roles": {"families": [], "exclude_internships": True},  # empty -> built-in DEFAULT_TARGET_ROLES
        "companies": {
            "require_tier": ["brand", "funded"],
            "funded_min_stage": "series_a",
            "yc_counts_as_funded": True,
            # Extended lane: unvetted companies named by human-curated early-career lists.
            "watchlist": {"enabled": True, "signals": ["simplify-newgrad", "speedyapply", "zshah"],
                          "min_signals": 2},
            "include_extended": True,   # fetch them, in a separate lane from the main queue
            "unknown_tier_lane": "extended",   # extended (+flag company:unverified) | excluded
        },
        "freshness": {"stale_flag_days": 60},   # flag only; age never excludes
        "queue": {"daily_target_min": 15, "daily_target_max": 25, "max_per_company": 3},
    },
    "sources": {
        "http": {
            "user_agent": "job-copilot/2.0 (personal, non-commercial; "
                          "+https://github.com/Aditya-Barnwal/job-copilot)",
            "timeout_sec": 20,
            "default_rate_per_sec": 1.0,
            "max_retries": 3,
            "host_rates": {},            # {"api.example.com": 0.2}
            "robots_exempt_hosts": {},   # {"host": "reason"} — documented public APIs only
            # DO-NOT-AUTOMATE hosts (+ subdomains): refused by the client outright.
            "denied_hosts": ["workatastartup.com", "ycombinator.com", "linkedin.com",
                             "naukri.com", "indeed.com", "wellfound.com", "glassdoor.com"],
        },
        "boards": {},                    # per-board on/off + options (Phase 1)
    },
    "schedule": {
        "timezone": "Asia/Kolkata",
        "daily_run": "19:00",
        "rss_poll_hours": 2,
    },
}


def config_dir() -> Path:
    return Path(os.environ.get("JOBCOPILOT_CONFIG_DIR") or (ROOT / "config"))


def companies_path() -> Path:
    """config/companies.yaml, falling back to the legacy repo-root file."""
    p = config_dir() / "companies.yaml"
    if p.exists():
        return p
    legacy = ROOT / "companies.yaml"
    return legacy if legacy.exists() else p


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load(name: str) -> dict[str, Any]:
    """Return config `name` (filters/sources/schedule) merged over DEFAULTS."""
    base = DEFAULTS.get(name, {})
    path = config_dir() / f"{name}.yaml"
    if not path.exists():
        return copy.deepcopy(base)
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return _deep_merge(base, data)


def filters() -> dict[str, Any]:
    return load("filters")


def sources() -> dict[str, Any]:
    return load("sources")


def schedule() -> dict[str, Any]:
    return load("schedule")
