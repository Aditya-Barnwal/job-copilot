"""SQLite persistence for jobs, statuses, and resume versions.

The DB is the source of truth across runs. Ingest upserts; status / scores /
applied-version-links set by the human or by scoring are preserved across
re-ingests.

Paths are resolved on every call, never bound as argument defaults: a default
bound at import time once let a test write into the real DB, and it made
`monkeypatch.setattr(store, "DEFAULT_DB_PATH", ...)` silently ineffective.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import socket
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from . import migrations
from .filters import FilterResult

log = logging.getLogger("jobcopilot.store")

_REPO_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_IMPORT_DB_PATH = Path(os.environ.get("JOBCOPILOT_DB") or (_REPO_DATA_DIR / "jobcopilot.db"))
_IMPORT_RESUMES_DIR = _REPO_DATA_DIR / "resumes"

# Public knobs. Reassigning one (tests, scripts) beats the env var; left alone,
# the env var is re-read on every call so `JOBCOPILOT_DB=... python ...` and
# `monkeypatch.setenv` both work without re-importing this module.
DEFAULT_DB_PATH: Path = _IMPORT_DB_PATH
RESUMES_DIR: Path = _IMPORT_RESUMES_DIR


def resolve_db_path(db_path: Path | str | None = None) -> Path:
    """Explicit arg > reassigned DEFAULT_DB_PATH > $JOBCOPILOT_DB > default."""
    if db_path is not None:
        return Path(db_path)
    if Path(DEFAULT_DB_PATH) != _IMPORT_DB_PATH:
        return Path(DEFAULT_DB_PATH)
    env = os.environ.get("JOBCOPILOT_DB")
    return Path(env) if env else Path(DEFAULT_DB_PATH)


def data_dir() -> Path:
    """Directory holding the DB; logs and generated files live beside it."""
    return resolve_db_path().parent


def resolve_resumes_dir() -> Path:
    """Reassigned RESUMES_DIR > $JOBCOPILOT_RESUMES_DIR > <data dir>/resumes.

    Defaulting next to the DB keeps one data root: point JOBCOPILOT_DB at a
    temp dir and generated PDFs follow it instead of landing in the repo.
    """
    if Path(RESUMES_DIR) != _IMPORT_RESUMES_DIR:
        return Path(RESUMES_DIR)
    env = os.environ.get("JOBCOPILOT_RESUMES_DIR")
    if env:
        return Path(env)
    return data_dir() / "resumes"


_REAL_DATA_DIR = (Path(__file__).resolve().parent.parent / "data").resolve()


def _refuse_real_db_under_tests(p: Path) -> None:
    """Belt and braces: under pytest, never open the owner's real data/ database,
    even if a test undoes the conftest's isolation by mistake."""
    if "PYTEST_CURRENT_TEST" not in os.environ:
        return
    try:
        real = Path(p).resolve().is_relative_to(_REAL_DATA_DIR)
    except OSError:
        real = False
    if real:
        raise RuntimeError(f"refusing to open the real database under tests: {p}")


@contextmanager
def connect(db_path: Path | str | None = None):
    p = resolve_db_path(db_path)
    _refuse_real_db_under_tests(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 30 s busy timeout + WAL: the server reads (and the UI writes statuses)
    # while the nightly run holds a write transaction.
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:   # e.g. a read-only copy; the default journal still works
        pass
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> int:
    """Create or upgrade the schema; returns the schema version now on disk."""
    return migrations.migrate(resolve_db_path(db_path))


# --- jobs ---------------------------------------------------------------

_POSTING_FIELDS = (
    "source", "company", "company_sector", "external_id", "title", "role_family",
    "location_text", "workplace_type", "country_bucket", "url", "description_text",
    "department", "comp_currency", "comp_min", "comp_max", "comp_interval",
    "comp_summary", "posted_at", "posted_age_days", "freshness",
    "package_estimated", "country_uncertain", "seniority_high",
    "min_yoe_required", "yoe_is_preferred",
    # v2 (Phase 1)
    "apply_url", "region_restrictions", "timezone_restrictions", "tz_requirement_text",
    "attribution_name", "attribution_url", "expires_at", "canonical_key", "company_id",
)
_JSON_LIST_FIELDS = ("region_restrictions", "timezone_restrictions")

# v3 (Phase 2): the eligibility decision + fit score, written beside the posting.
EVALUATION_FIELDS = (
    "lane", "excluded", "exclusion_reasons", "flags", "min_yoe_required", "yoe_max",
    "yoe_is_preferred", "yoe_evidence", "fresher_friendly", "india_eligible",
    "salary_annual_inr", "eval_cfg_hash",
)
FIT_FIELDS = ("fit_score_v2", "fit_components", "matched_skills", "missing_skills", "fit_why")
_V3_JSON_FIELDS = ("exclusion_reasons", "flags", "fit_components", "matched_skills", "missing_skills")

# Tracker statuses (Phase 2). The v1 values stay valid and map onto these.
TRACKER_STATUSES = ("found", "tailored", "ready_to_submit", "submitted", "oa", "interview",
                    "offer", "rejected", "not_interested")
LEGACY_STATUS_MAP = {"new": "found", "interested": "found", "ready": "ready_to_submit",
                     "applied": "submitted"}
ALL_STATUSES = TRACKER_STATUSES + tuple(LEGACY_STATUS_MAP)
SUBMITTED_OR_LATER = frozenset({"submitted", "oa", "interview", "offer", "rejected"})
STATUS_SOURCES = ("user", "detected", "system")


def statuses_with_canonical(canon: Iterable[str]) -> list[str]:
    """Every stored value (new + legacy) whose tracker column is in `canon`."""
    want = set(canon)
    return [s for s in ALL_STATUSES if LEGACY_STATUS_MAP.get(s, s) in want]


def _json_or_none(v: Any) -> Optional[str]:
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, default=str)


def _write_evaluation(conn: sqlite3.Connection, key: str, evaluation: dict, now: float) -> None:
    bad = set(evaluation) - set(EVALUATION_FIELDS)
    if bad:
        raise ValueError(f"unknown evaluation fields: {sorted(bad)}")
    vals = {k: (_json_or_none(v) if k in _V3_JSON_FIELDS else
                (int(v) if isinstance(v, bool) else v)) for k, v in evaluation.items()}
    vals["evaluated_at"] = now
    cols = ", ".join(f"{k} = ?" for k in vals)
    conn.execute(f"UPDATE jobs SET {cols} WHERE key = ?", (*vals.values(), key))


def set_evaluation(key: str, evaluation: dict, db_path: Path | str | None = None) -> None:
    """Store an eligibility decision (see EVALUATION_FIELDS) on an existing row."""
    with connect(db_path) as conn:
        _write_evaluation(conn, key, evaluation, time.time())


def set_fit(key: str, fit: dict, db_path: Path | str | None = None,
            scored_at: Optional[float] = None) -> None:
    """Store a v2 fit result (see FIT_FIELDS); lists/dicts are JSON-encoded."""
    bad = set(fit) - set(FIT_FIELDS)
    if bad:
        raise ValueError(f"unknown fit fields: {sorted(bad)}")
    vals = {k: (_json_or_none(v) if k in _V3_JSON_FIELDS else v) for k, v in fit.items()}
    vals["scored_at"] = time.time() if scored_at is None else scored_at
    cols = ", ".join(f"{k} = ?" for k in vals)
    with connect(db_path) as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE key = ?", (*vals.values(), key))


def decode_v3(row: dict) -> dict:
    """JSON-decode the v3 columns in place (bad JSON -> empty value)."""
    for f in _V3_JSON_FIELDS:
        if f not in row:
            continue
        v = row[f]
        empty: Any = {} if f == "fit_components" else []
        if isinstance(v, str) and v:
            try:
                row[f] = json.loads(v)
            except ValueError:
                row[f] = empty
        elif v is None:
            row[f] = empty
    return row


def _row_from_filter_result(r: FilterResult) -> dict[str, Any]:
    from .dedupe import canonical_key  # local: dedupe imports models only, but keep store light

    j = r.job
    comp = j.compensation
    return dict(
        key=j.key,
        source=j.source,
        company=j.company,
        company_sector=j.company_sector,
        external_id=j.external_id,
        title=j.title,
        role_family=j.role_family,
        location_text=j.location_text,
        workplace_type=j.workplace_type,
        country_bucket=j.country_bucket,
        url=j.url,
        description_text=j.description_text,
        department=j.department,
        comp_currency=(comp.currency if comp else None),
        comp_min=(comp.min_value if comp else None),
        comp_max=(comp.max_value if comp else None),
        comp_interval=(comp.interval if comp else None),
        comp_summary=(comp.summary if comp else None),
        posted_at=j.posted_at,
        posted_age_days=j.posted_age_days,
        freshness=j.freshness,
        package_estimated=int(r.package_estimated),
        country_uncertain=int(r.country_uncertain),
        seniority_high=int(j.seniority_high),
        min_yoe_required=j.min_yoe_required,
        yoe_is_preferred=int(j.yoe_is_preferred),
        apply_url=j.apply_url or j.url or None,
        region_restrictions=json.dumps(list(j.region_restrictions or [])),
        timezone_restrictions=json.dumps(list(j.timezone_restrictions or [])),
        tz_requirement_text=j.tz_requirement_text,
        attribution_name=j.attribution_name,
        attribution_url=j.attribution_url,
        expires_at=j.expires_at,
        canonical_key=getattr(j, "canonical_key", None) or canonical_key(j),
        company_id=getattr(j, "company_id", None),
    )


def _find_existing(conn: sqlite3.Connection, row: dict, occurrences: list[dict]) -> Optional[sqlite3.Row]:
    """The stored row this job belongs to: same key, a known listing, or the
    same canonical role from a DIFFERENT source (two postings from one source
    are two roles, see dedupe.py)."""
    found = conn.execute("SELECT key, source FROM jobs WHERE key = ?", (row["key"],)).fetchone()
    if found:
        return found
    mine_co = _norm_company(row.get("company"))
    for o in occurrences:
        # A listing id belongs to ONE employer: a hit on another company's row is
        # an id collision (per-tenant ids, shared fixtures), never the same job.
        for eid in (str(o["external_id"]), _qualified_id(mine_co, o["external_id"])):
            hit = conn.execute(
                "SELECT j.key, j.source, j.company FROM job_sources s JOIN jobs j ON j.key = s.job_key "
                "WHERE s.source = ? AND s.external_id = ?", (o["source"], eid),
            ).fetchone()
            if hit and _norm_company(hit["company"]) == mine_co:
                return hit
    mine = {o["source"] for o in occurrences}
    my_regions = _regions_of(row["region_restrictions"])
    for cand in conn.execute("SELECT key, source, region_restrictions FROM jobs WHERE canonical_key = ?",
                             (row["canonical_key"],)).fetchall():
        theirs = {r["source"] for r in conn.execute(
            "SELECT source FROM job_sources WHERE job_key = ?", (cand["key"],))}
        theirs.add(cand["source"])
        if theirs & mine:
            continue
        # 'USA Only' and 'Canada' copies are two roles (same rule as dedupe.merge)
        their_regions = _regions_of(cand["region_restrictions"])
        if my_regions and their_regions and not (my_regions & their_regions):
            continue
        return cand
    return None


def _regions_of(value: Any) -> frozenset[str]:
    from .dedupe import _region_token

    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            value = []
    return frozenset(t for t in (_region_token(str(v)) for v in value or []) if t)


def _norm_company(name: Any) -> str:
    from .normalize import normalize_company_name
    return normalize_company_name(name or "")


def _qualified_id(norm_company: str, external_id: Any) -> str:
    return f"{norm_company}/{external_id}"


def _listing_id(conn: sqlite3.Connection, job_key: str, source: str, external_id: Any) -> str:
    """external_id as stored in job_sources for this job. job_sources is
    UNIQUE(source, external_id); when another employer's job already holds the
    bare id, this one is stored company-qualified instead of stealing it."""
    eid = str(external_id)
    held = conn.execute("SELECT s.job_key, j.company FROM job_sources s LEFT JOIN jobs j ON j.key = s.job_key "
                        "WHERE s.source = ? AND s.external_id = ?", (source, eid)).fetchone()
    if not held or held["job_key"] == job_key:
        return eid
    mine = conn.execute("SELECT company FROM jobs WHERE key = ?", (job_key,)).fetchone()
    mine_co = _norm_company(mine["company"] if mine else "")
    if held["company"] is None or _norm_company(held["company"]) == mine_co:
        return eid
    return _qualified_id(mine_co, eid)


def _write_sources(conn: sqlite3.Connection, job_key: str, occurrences: list[dict], now: float) -> None:
    for o in occurrences:
        o = {**o, "external_id": _listing_id(conn, job_key, o["source"], o["external_id"])}
        conn.execute(
            "INSERT INTO job_sources(job_key, source, external_id, url, apply_url, "
            "attribution_name, attribution_url, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source, external_id) DO UPDATE SET job_key = excluded.job_key, "
            "url = excluded.url, apply_url = excluded.apply_url, "
            "attribution_name = excluded.attribution_name, "
            "attribution_url = excluded.attribution_url, last_seen = excluded.last_seen",
            (job_key, o["source"], str(o["external_id"]), o.get("url"), o.get("apply_url"),
             o.get("attribution_name"), o.get("attribution_url"), now, now),
        )


def upsert_jobs(results: Iterable[FilterResult], db_path: Path | str | None = None) -> tuple[int, int]:
    """Insert new roles; update posting fields + last_seen on known ones.

    A role already stored under another key (found via job_sources or the
    canonical key) is updated in place, so it stays ONE row with every source
    in job_sources. Posting fields are only overwritten by an equal-or-better
    source (employer ATS beats a board), so a board re-listing can't replace
    the ATS apply link. Preserves status, notes, scores, applied_version_id.
    Returns (inserted, updated).
    """
    from .dedupe import source_rank, sources_of

    now = time.time()
    inserted = 0
    updated = 0
    with connect(db_path) as conn:
        for r in results:
            row = _row_from_filter_result(r)
            occurrences = sources_of(r.job)
            existing = _find_existing(conn, row, occurrences)
            if existing:
                target = existing["key"]
                if source_rank(row["source"]) <= source_rank(existing["source"]):
                    set_clause = ", ".join(f"{f} = :{f}" for f in _POSTING_FIELDS)
                    conn.execute(
                        f"UPDATE jobs SET {set_clause}, last_seen = :_last, gone_at = NULL "
                        "WHERE key = :_target",
                        {**row, "_last": now, "_target": target},
                    )
                else:
                    # A lower-rank source still lists this role, so the row's
                    # expiry is the LATEST over live sources: one listing
                    # without an expiry (NULL) means it hasn't expired.
                    conn.execute("UPDATE jobs SET last_seen = ?, gone_at = NULL, "
                                 "company_id = COALESCE(company_id, ?), "
                                 "expires_at = CASE WHEN ? IS NULL OR expires_at IS NULL "
                                 "OR expires_at = '' THEN NULL "
                                 "WHEN ? > expires_at THEN ? ELSE expires_at END WHERE key = ?",
                                 (now, row["company_id"], row["expires_at"] or None,
                                  row["expires_at"], row["expires_at"], target))
                updated += 1
            else:
                target = row["key"]
                row["first_seen"] = now
                row["last_seen"] = now
                cols = list(row.keys())
                conn.execute(
                    f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
                    row,
                )
                inserted += 1
            _write_sources(conn, target, occurrences, now)
            evaluation = getattr(r, "evaluation", None)
            if evaluation:
                _write_evaluation(conn, target, evaluation, now)
            try:
                r.stored_key = target   # type: ignore[attr-defined]  # where this result lives now
            except AttributeError:      # a slotted / frozen result type
                pass
    return inserted, updated


def purge_expired(today: Optional[str] = None, db_path: Path | str | None = None) -> int:
    """Delete untouched ('new') rows whose source listing has expired (e.g.
    Himalayas jobs past expiryDate; docs/DESIGN.md §5). Rows the user acted on stay."""
    today = today or time.strftime("%Y-%m-%d")
    with connect(db_path) as conn:
        keys = [r["key"] for r in conn.execute(
            "SELECT key FROM jobs WHERE expires_at IS NOT NULL AND expires_at != '' "
            "AND substr(expires_at, 1, 10) < ? AND status = 'new' "
            "AND applied_version_id IS NULL", (today,))]
        for k in keys:
            conn.execute("DELETE FROM job_sources WHERE job_key = ?", (k,))
            conn.execute("DELETE FROM jobs WHERE key = ?", (k,))
    return len(keys)


def retire_unlisted(gone_after_days: float = 3, now: Optional[float] = None,
                    db_path: Path | str | None = None) -> dict[str, int]:
    """Set jobs.gone_at on postings no source lists any more; clear it on re-listing.

    A job is gone only when EVERY listing of it (job_sources row; the jobs row
    itself for pre-v2 rows) belongs to a source that had a successful run more
    than `gone_after_days` after the listing was last seen. A source's last
    success is the latest of source_state.last_ok (board name, or
    "<connector>:<company>") and the newest last_seen of any job that source
    listed for the same company. A source that failed, never ran, or isn't
    known keeps its jobs live: absence of evidence is not removal.
    """
    now = time.time() if now is None else float(now)
    grace = float(gone_after_days) * 86400
    with connect(db_path) as conn:
        state = {r["source"]: float(r["last_ok"]) for r in conn.execute(
            "SELECT source, last_ok FROM source_state WHERE last_ok IS NOT NULL")}
        rows = conn.execute(
            "SELECT j.key, j.company, j.source AS jsource, j.last_seen AS jseen, j.gone_at, "
            "s.source AS ssource, s.last_seen AS sseen FROM jobs j "
            "LEFT JOIN job_sources s ON s.job_key = j.key").fetchall()
        norm: dict[str, str] = {}
        listings: dict[str, list[tuple[str, str, Optional[float]]]] = {}
        gone_at: dict[str, Any] = {}
        proxy: dict[tuple[str, str], float] = {}
        for r in rows:
            co = norm.setdefault(r["company"] or "", _norm_company(r["company"]))
            src, seen = (r["ssource"], r["sseen"]) if r["ssource"] is not None else (r["jsource"], r["jseen"])
            listings.setdefault(r["key"], []).append((src, co, seen))
            gone_at[r["key"]] = r["gone_at"]
            if seen is not None:
                k = (src, co)
                proxy[k] = max(proxy.get(k, 0.0), float(seen))
        newly = relisted = total = 0
        for key, ls in listings.items():
            def gone_here(src: str, co: str, seen: Optional[float]) -> bool:
                if seen is None:
                    return False
                ok = max(state.get(src, 0.0), state.get(f"{src}:{co}", 0.0), proxy.get((src, co), 0.0))
                return ok > float(seen) + grace
            gone = all(gone_here(*x) for x in ls)
            total += gone
            if gone and gone_at[key] is None:
                conn.execute("UPDATE jobs SET gone_at = ? WHERE key = ?", (now, key))
                newly += 1
            elif not gone and gone_at[key] is not None:
                conn.execute("UPDATE jobs SET gone_at = NULL WHERE key = ?", (key,))
                relisted += 1
    return {"gone": total, "newly_gone": newly, "relisted": relisted}


def _canon(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _canon(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted((_canon(x) for x in v), key=repr)
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def config_hash(cfg: Any) -> str:
    """Stable short hash of a (filters) config dict, stored with each decision."""
    blob = json.dumps(_canon(cfg), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def job_sources(key: str, db_path: Path | str | None = None) -> list[dict]:
    """Every place this job was seen, oldest first."""
    with connect(db_path) as conn:
        return _sources_for(conn, key)


def _sources_for(conn: sqlite3.Connection, key: str) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT source, external_id, url, apply_url, attribution_name, attribution_url, "
            "first_seen, last_seen FROM job_sources WHERE job_key = ? ORDER BY first_seen, id",
            (key,)).fetchall()
    except sqlite3.OperationalError:  # a DB still on schema v1 (read before init_db)
        return []
    return [dict(r) for r in rows]


def _decode_json_lists(row: dict) -> dict:
    for f in _JSON_LIST_FIELDS:
        v = row.get(f)
        if isinstance(v, str) and v:
            try:
                row[f] = json.loads(v)
            except Exception:
                row[f] = []
        elif v is None and f in row:
            row[f] = []
    return row


def _decode_keywords(row: dict) -> dict:
    for f in ("required_keywords", "missing_keywords"):
        v = row.get(f)
        if isinstance(v, str) and v:
            try:
                row[f] = json.loads(v)
            except Exception:
                row[f] = []
        elif v is None:
            row[f] = []
    return row


# fit_score = 0.5·match + 0.5·ATS% (or match alone when ATS is unknown). The
# SQL twin below lets `min_score` filter on the number the UI actually shows;
# keep the two in sync. Both round with the SAME rule — floor(x·10 + 0.5) / 10
# on IEEE doubles — because Python's round() is half-to-even and SQLite's
# ROUND() is half-away-from-zero, which disagreed on values like 62.25.
# Scores are never negative, so this is plain half-up rounding.
_FIT_RAW_SQL = (
    "(CASE WHEN match_score IS NOT NULL AND ats_coverage IS NOT NULL "
    "THEN 0.5 * match_score + 0.5 * (ats_coverage * 100.0) "
    "ELSE match_score END)"
)
_FIT_SQL = f"(CAST({_FIT_RAW_SQL} * 10.0 + 0.5 AS INTEGER) / 10.0)"


def _round1(x: float) -> float:
    return math.floor(x * 10.0 + 0.5) / 10.0


def _fit_score(ms: Optional[float], ats: Optional[float]) -> Optional[float]:
    if ms is not None and ats is not None:
        return _round1(0.5 * ms + 0.5 * (ats * 100.0))
    if ms is not None:
        return _round1(ms)
    return None


def _user_yoe(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute("SELECT v FROM meta WHERE k='user_yoe'").fetchone()
    return int(row["v"]) if row and (row["v"] or "").isdigit() else None


def _derive(row: dict, user_yoe: Optional[int]) -> dict:
    """Add display-time derived fields: fit_score, ats_low_confidence, yoe_gap."""
    row["fit_score"] = _fit_score(row.get("match_score"), row.get("ats_coverage"))
    req = row.get("required_keywords") or []
    row["ats_low_confidence"] = bool(len(req) < 3 and req)
    # YOE gap
    min_yoe = row.get("min_yoe_required")
    pref = bool(row.get("yoe_is_preferred"))
    if user_yoe is not None and min_yoe is not None and not pref and min_yoe > user_yoe:
        row["yoe_gap"] = True
    else:
        row["yoe_gap"] = False
    return row


def list_jobs(
    status: Optional[str] = None,
    sector: Optional[str] = None,
    country_bucket: Optional[str] = None,
    workplace_type: Optional[str] = None,
    role_family: Optional[str] = None,
    freshness: Optional[str] = None,
    q: Optional[str] = None,
    min_score: Optional[float] = None,
    max_age_days: Optional[int] = None,
    hide_senior_high: bool = False,
    limit: int = 500,
    db_path: Path | str | None = None,
) -> list[dict]:
    where = []
    args: list[Any] = []
    if status:
        where.append("status = ?"); args.append(status)
    if sector:
        where.append("company_sector = ?"); args.append(sector)
    if country_bucket:
        where.append("country_bucket = ?"); args.append(country_bucket)
    if workplace_type:
        where.append("workplace_type = ?"); args.append(workplace_type)
    if role_family:
        where.append("role_family = ?"); args.append(role_family)
    if freshness:
        where.append("freshness = ?"); args.append(freshness)
    if q:
        where.append("(LOWER(title) LIKE ? OR LOWER(company) LIKE ?)")
        args += [f"%{q.lower()}%", f"%{q.lower()}%"]
    if min_score is not None:
        # Filter on the derived fit score the UI shows, not raw match_score.
        where.append(f"{_FIT_SQL} >= ?"); args.append(min_score)
    if max_age_days is not None:
        where.append("(posted_age_days IS NULL OR posted_age_days <= ?)")
        args.append(max_age_days)
    if hide_senior_high:
        where.append("seniority_high = 0")

    sql = "SELECT * FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (
        " ORDER BY match_score IS NULL, match_score DESC, "
        " CASE freshness WHEN 'fresh' THEN 0 WHEN 'recent' THEN 1 "
        " WHEN 'aging' THEN 2 WHEN 'stale' THEN 3 ELSE 4 END, "
        " last_seen DESC LIMIT ?"
    )
    args.append(limit)
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        user_yoe = _user_yoe(conn)
    return [_derive(decode_v3(_decode_json_lists(_decode_keywords(r))), user_yoe) for r in rows]


def get_job(key: str, db_path: Path | str | None = None) -> Optional[dict]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()
        user_yoe = _user_yoe(conn)
        sources = _sources_for(conn, key) if row else []
    if not row:
        return None
    job = _derive(decode_v3(_decode_json_lists(_decode_keywords(dict(row)))), user_yoe)
    if not sources:  # rows stored before v2 have no job_sources yet
        sources = [{k: job.get(k) for k in ("source", "external_id", "url", "apply_url",
                                             "attribution_name", "attribution_url",
                                             "first_seen", "last_seen")}]
    job["sources"] = sources
    return job


def canonical_status(status: Optional[str]) -> str:
    """Tracker column for a stored status (legacy values map onto the new ones)."""
    s = (status or "new").strip().lower()
    return LEGACY_STATUS_MAP.get(s, s)


def is_submitted_or_later(status: Optional[str]) -> bool:
    return canonical_status(status) in SUBMITTED_OR_LATER


def set_status(key: str, status: str, notes: Optional[str] = None,
               db_path: Path | str | None = None, *, source: str = "user",
               note: Optional[str] = None, proof_path: Optional[str] = None,
               at: Optional[float] = None) -> bool:
    """Set a job's tracker status; every change is appended to status_history.

    Legacy statuses (new/interested/ready/applied) are still accepted and
    stored as given. Reaching `submitted` (or later) stamps submitted_at and
    submit_source ('user' | 'detected') unless already set; going back to a
    pre-submit status (undo) clears them, and history keeps both rows.
    Returns False when the job does not exist.

    source='system' (automatic transitions, e.g. approving a tailored résumé)
    never moves a job backwards out of submitted-or-later: such a call is
    logged and ignored (returns True, nothing written). Only the user — or a
    detected submission event — can undo a submission.
    """
    if source not in STATUS_SOURCES:
        raise ValueError(f"unknown status source: {source!r}")
    now = time.time() if at is None else float(at)
    canon = canonical_status(status)
    with connect(db_path) as conn:
        row = conn.execute("SELECT status, submitted_at FROM jobs WHERE key = ?", (key,)).fetchone()
        if row is None:
            return False
        if (source == "system" and canonical_status(row["status"]) in SUBMITTED_OR_LATER
                and canon not in SUBMITTED_OR_LATER):
            log.warning("refused system status regression key=%s %s -> %s", key, row["status"], status)
            return True
        sets: dict[str, Any] = {"status": status}
        changed = (row["status"] or "new") != status
        if changed:
            sets["status_changed_at"] = now
        if notes is not None:
            sets["notes"] = notes
        if canon in SUBMITTED_OR_LATER:
            if row["submitted_at"] is None and canon != "rejected":
                sets["submitted_at"] = now
                sets["submit_source"] = "detected" if source == "detected" else "user"
            if proof_path:
                sets["submit_proof_path"] = proof_path
        elif canon in TRACKER_STATUSES:
            sets["submitted_at"] = None
            sets["submit_source"] = None
        cols = ", ".join(f"{k} = ?" for k in sets)
        conn.execute(f"UPDATE jobs SET {cols} WHERE key = ?", (*sets.values(), key))
        if changed or note or notes:
            conn.execute("INSERT INTO status_history(job_key, status, at, source, note) "
                         "VALUES (?, ?, ?, ?, ?)", (key, status, now, source, note or notes))
        return True


def status_history(key: str, db_path: Path | str | None = None) -> list[dict]:
    """Every recorded status change for a job, oldest first."""
    with connect(db_path) as conn:
        rows = conn.execute("SELECT status, at, source, note FROM status_history "
                            "WHERE job_key = ? ORDER BY at, id", (key,)).fetchall()
    return [dict(r) for r in rows]


def set_scores(key: str, match_score: float, ats_coverage: float,
               required_keywords: list[str], missing_keywords: list[str],
               db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET match_score = ?, ats_coverage = ?, "
            "required_keywords = ?, missing_keywords = ? WHERE key = ?",
            (match_score, ats_coverage,
             json.dumps(required_keywords), json.dumps(missing_keywords), key),
        )


def status_counts(db_path: Path | str | None = None) -> dict[str, int]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# --- source state (per-source health + conditional-fetch memory) --------

_SOURCE_STATE_FIELDS = ("etag", "last_modified", "content_hash", "last_run", "last_ok",
                        "last_error", "items", "state")


def _decode_state(row: dict) -> dict:
    v = row.get("state")
    if isinstance(v, str) and v:
        try:
            row["state"] = json.loads(v)
        except Exception:
            row["state"] = None
    return row


def get_source_state(source: str, db_path: Path | str | None = None) -> Optional[dict]:
    """The source_state row for `source` (`state` JSON-decoded), or None."""
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM source_state WHERE source = ?", (source,)).fetchone()
    return _decode_state(dict(row)) if row else None


def set_source_state(source: str, db_path: Path | str | None = None, **fields: Any) -> None:
    """Upsert only the given columns, leaving the rest (e.g. an ETag) intact.

    `state` may be any JSON-serialisable value. Unknown columns raise, so a typo
    can't silently drop health data.
    """
    bad = set(fields) - set(_SOURCE_STATE_FIELDS)
    if bad:
        raise ValueError(f"unknown source_state fields: {sorted(bad)}")
    if "state" in fields and fields["state"] is not None and not isinstance(fields["state"], str):
        fields["state"] = json.dumps(fields["state"])
    with connect(db_path) as conn:
        conn.execute("INSERT INTO source_state(source) VALUES (?) ON CONFLICT(source) DO NOTHING",
                     (source,))
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE source_state SET {cols} WHERE source = ?",
                         (*fields.values(), source))


def list_source_state(db_path: Path | str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM source_state ORDER BY source").fetchall()
    return [_decode_state(dict(r)) for r in rows]


# --- meta / resume base -------------------------------------------------

def set_meta(key: str, value: str, db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("INSERT INTO meta(k, v) VALUES(?, ?) "
                     "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, value))


def get_meta(key: str, db_path: Path | str | None = None) -> Optional[str]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else None


# --- profile -----------------------------------------------------------

PROFILE_FIELDS = (
    "first_name", "last_name", "email", "phone",
    "linkedin_url", "portfolio_url",
)


def get_profile(db_path: Path | str | None = None) -> dict:
    v = get_meta("profile", db_path) or "{}"
    try:
        data = json.loads(v)
    except Exception:
        data = {}
    return {k: (data.get(k) or "") for k in PROFILE_FIELDS}


def set_profile(profile: dict, db_path: Path | str | None = None) -> dict:
    cleaned = {k: (profile.get(k) or "").strip() for k in PROFILE_FIELDS}
    set_meta("profile", json.dumps(cleaned), db_path)
    return cleaned


# --- answer pack -------------------------------------------------------

def get_answer_pack(db_path: Path | str | None = None) -> list[dict]:
    v = get_meta("answer_pack", db_path) or "[]"
    try:
        items = json.loads(v)
        return items if isinstance(items, list) else []
    except Exception:
        return []


def upsert_answer(q: str, a: str, db_path: Path | str | None = None) -> list[dict]:
    items = get_answer_pack(db_path)
    items = [it for it in items if (it.get("q") or "").strip().lower() != q.strip().lower()]
    items.append({"q": q.strip(), "a": a.strip()})
    set_meta("answer_pack", json.dumps(items), db_path)
    return items


def delete_answer(q: str, db_path: Path | str | None = None) -> list[dict]:
    items = [it for it in get_answer_pack(db_path)
             if (it.get("q") or "").strip().lower() != q.strip().lower()]
    set_meta("answer_pack", json.dumps(items), db_path)
    return items


def set_resume(text: str, filename: str, db_path: Path | str | None = None) -> None:
    set_meta("resume_text", text, db_path)
    set_meta("resume_filename", filename, db_path)
    set_meta("resume_uploaded_at", str(time.time()), db_path)


def get_resume(db_path: Path | str | None = None) -> Optional[dict]:
    text = get_meta("resume_text", db_path)
    if not text:
        return None
    return {
        "text": text,
        "filename": get_meta("resume_filename", db_path),
        "uploaded_at": get_meta("resume_uploaded_at", db_path),
    }


# --- resume versions ----------------------------------------------------

def add_version(label: str, role_family: Optional[str], source_job_key: Optional[str],
                source_company: Optional[str], tailored_text: str, pdf_path: str,
                target_keywords: list[str], base_ats_coverage: float,
                db_path: Path | str | None = None) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO resume_versions(label, role_family, source_job_key, "
            "source_company, tailored_text, pdf_path, target_keywords, "
            "base_ats_coverage, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (label, role_family, source_job_key, source_company, tailored_text,
             pdf_path, json.dumps(target_keywords), base_ats_coverage, time.time()),
        )
        return int(cur.lastrowid)


def list_versions(role_family: Optional[str] = None,
                  db_path: Path | str | None = None) -> list[dict]:
    sql = "SELECT * FROM resume_versions"
    args: list[Any] = []
    if role_family:
        sql += " WHERE role_family = ?"; args.append(role_family)
    sql += " ORDER BY created_at DESC"
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    for r in rows:
        try:
            r["target_keywords"] = json.loads(r.get("target_keywords") or "[]")
        except Exception:
            r["target_keywords"] = []
    return rows


def get_version(version_id: int, db_path: Path | str | None = None) -> Optional[dict]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM resume_versions WHERE id = ?", (version_id,)).fetchone()
    if not row:
        return None
    r = dict(row)
    try:
        r["target_keywords"] = json.loads(r.get("target_keywords") or "[]")
    except Exception:
        r["target_keywords"] = []
    return r


def link_job_version(key: str, version_id: int,
                     db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE jobs SET applied_version_id = ? WHERE key = ?",
                     (version_id, key))



# --- application artifacts (Phase 3, tailoring v2) ------------------------

ARTIFACT_JSON_FIELDS = ("edit_plan", "grounding", "answers_json", "cover_letter_json")
ARTIFACT_FIELDS = ("base", "edit_plan", "tex_path", "pdf_path", "cover_letter_path",
                   "answers_json", "grounding", "status", "facts_hash", "pages", "approved_at",
                   "cover_letter_json", "cover_letter_approved_at", "cover_letter_approved_sha")


def applications_dir(db_path: Path | str | None = None) -> Path:
    """<data dir>/applications: the only place artifact files are written or served from."""
    return resolve_db_path(db_path).parent / "applications"


def _decode_artifact(row: dict) -> dict:
    for f in ARTIFACT_JSON_FIELDS:
        v = row.get(f)
        if isinstance(v, str) and v:
            try:
                row[f] = json.loads(v)
            except ValueError:
                row[f] = None
    return row


def _artifact_value(field_name: str, v: Any) -> Any:
    if field_name in ARTIFACT_JSON_FIELDS and v is not None and not isinstance(v, str):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return v


def create_artifact(job_key: str, base: Optional[str] = None,
                    db_path: Path | str | None = None) -> tuple[int, int]:
    """Reserve the next version for a job (status 'pending'); returns (id, version).
    The version is chosen and inserted in one write transaction, so two tailoring
    runs for one job can't get the same number."""
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM application_artifacts "
                           "WHERE job_key = ?", (job_key,)).fetchone()
        version = int(row[0]) + 1
        cur = conn.execute(
            "INSERT INTO application_artifacts(job_key, version, base, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)", (job_key, version, base, time.time()))
        return int(cur.lastrowid), version


def update_artifact(artifact_id: int, db_path: Path | str | None = None, **fields: Any) -> None:
    bad = set(fields) - set(ARTIFACT_FIELDS)
    if bad:
        raise ValueError(f"unknown artifact fields: {sorted(bad)}")
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect(db_path) as conn:
        conn.execute(f"UPDATE application_artifacts SET {cols} WHERE id = ?",
                     (*(_artifact_value(k, v) for k, v in fields.items()), artifact_id))


def get_artifact(artifact_id: int, db_path: Path | str | None = None) -> Optional[dict]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM application_artifacts WHERE id = ?",
                           (artifact_id,)).fetchone()
    return _decode_artifact(dict(row)) if row else None


def list_artifacts(job_key: Optional[str] = None,
                   db_path: Path | str | None = None) -> list[dict]:
    """A job's artifacts, newest version first (every job's when job_key is None)."""
    sql = "SELECT * FROM application_artifacts"
    args: list[Any] = []
    if job_key is not None:
        sql += " WHERE job_key = ?"
        args.append(job_key)
    sql += " ORDER BY job_key, version DESC"
    with connect(db_path) as conn:
        return [_decode_artifact(dict(r)) for r in conn.execute(sql, args).fetchall()]


def link_job_artifact(key: str, artifact_id: int,
                      db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE jobs SET artifact_id = ? WHERE key = ?", (artifact_id, key))

# --- runs (nightly / manual pipeline runs) --------------------------------

def _decode_run(row: dict) -> dict:
    v = row.get("summary")
    if isinstance(v, str) and v:
        try:
            row["summary"] = json.loads(v)
        except ValueError:
            row["summary"] = None
    return row


RUN_STALE_AFTER_SEC = 6 * 3600     # longer than any real run: a 'running' row this old crashed


def _insert_run(conn: sqlite3.Connection, kind: str, summary: Any) -> int:
    cur = conn.execute("INSERT INTO runs(kind, started_at, status, summary, pid, hostname) "
                       "VALUES (?, ?, 'running', ?, ?, ?)",
                       (kind, time.time(), _json_or_none(summary), os.getpid(), socket.gethostname()))
    return int(cur.lastrowid)


def start_run(kind: str, db_path: Path | str | None = None, summary: Any = None) -> int:
    """Insert a 'running' row unconditionally (see claim_run for the exclusive form)."""
    with connect(db_path) as conn:
        return _insert_run(conn, kind, summary)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, ValueError):
        return True     # exists but not ours / unknowable: assume alive
    return True


def _abandon_reason(row: sqlite3.Row, now: float, stale_after_sec: float) -> Optional[str]:
    if row["started_at"] is not None and float(row["started_at"]) < now - stale_after_sec:
        return f"abandoned: still 'running' after {stale_after_sec / 3600:g} h"
    pid, host = row["pid"], row["hostname"]
    if pid is not None and host == socket.gethostname() and not _pid_alive(pid):
        return f"abandoned: process {pid} is gone"
    return None


def _reap_and_find_live(conn: sqlite3.Connection, kind: str, stale_after_sec: float) -> Optional[sqlite3.Row]:
    """Mark dead/stale 'running' rows of `kind` failed; return the newest live one."""
    now = time.time()
    live = None
    for r in conn.execute("SELECT * FROM runs WHERE kind = ? AND status = 'running' "
                          "ORDER BY started_at DESC, id DESC", (kind,)).fetchall():
        why = _abandon_reason(r, now, stale_after_sec)
        if why:
            conn.execute("UPDATE runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                         (now, why, r["id"]))
            log.warning("run #%s marked failed (%s)", r["id"], why)
        elif live is None:
            live = r
    return live


def claim_run(kind: str, db_path: Path | str | None = None, summary: Any = None,
              stale_after_sec: float = RUN_STALE_AFTER_SEC) -> tuple[Optional[int], Optional[dict]]:
    """Atomically start a run of `kind` unless a live one exists.

    One write transaction (BEGIN IMMEDIATE): reap abandoned rows, check for a
    live run, insert ours. Returns (new_run_id, None) or (None, live_run). The
    CLI / LaunchAgent and POST /api/runs/daily both go through here, so two
    starters can never both get a run.
    """
    p = resolve_db_path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            live = _reap_and_find_live(conn, kind, stale_after_sec)
            if live is not None:
                conn.execute("COMMIT")      # keep the reaping
                return None, _decode_run(dict(live))
            run_id = _insert_run(conn, kind, summary)
            conn.execute("COMMIT")
            return run_id, None
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def update_run(run_id: int, *, status: Optional[str] = None, summary: Any = None,
               error: Optional[str] = None, finished: bool = False,
               db_path: Path | str | None = None) -> None:
    sets: dict[str, Any] = {}
    if status is not None:
        sets["status"] = status
    if summary is not None:
        sets["summary"] = _json_or_none(summary)
    if error is not None:
        sets["error"] = error[:2000]
    if finished:
        sets["finished_at"] = time.time()
    if not sets:
        return
    with connect(db_path) as conn:
        conn.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                     (*sets.values(), run_id))


def get_run(run_id: int, db_path: Path | str | None = None) -> Optional[dict]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _decode_run(dict(row)) if row else None


def list_runs(kind: Optional[str] = None, limit: int = 50,
              db_path: Path | str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM runs", []
    if kind:
        sql += " WHERE kind = ?"; args.append(kind)
    sql += " ORDER BY started_at DESC, id DESC LIMIT ?"; args.append(int(limit))
    with connect(db_path) as conn:
        return [_decode_run(dict(r)) for r in conn.execute(sql, args).fetchall()]


def running_run(kind: str, stale_after_sec: float = RUN_STALE_AFTER_SEC,
                db_path: Path | str | None = None) -> Optional[dict]:
    """The newest live 'running' run of `kind`. Rows older than stale_after_sec,
    or whose process (same host) is dead, are marked failed ('abandoned')."""
    with connect(db_path) as conn:
        row = _reap_and_find_live(conn, kind, stale_after_sec)
    return _decode_run(dict(row)) if row else None


# --- daily queue ----------------------------------------------------------

def replace_queue(day: str, items: Iterable[tuple[str, str, int, Optional[float]]],
                  db_path: Path | str | None = None) -> dict[str, int]:
    """Make `items` [(job_key, lane, rank, fit_score)] the queue for `day`.

    Idempotent: existing rows keep their status (only lane/rank/score change);
    rows no longer selected are deleted if untouched ('queued'), otherwise kept
    with rank NULL so a user's action on them is never lost.
    """
    now = time.time()
    items = list(items)
    keep = {k for k, _, _, _ in items}
    with connect(db_path) as conn:
        for job_key, lane, rank, fit in items:
            conn.execute(
                "INSERT INTO queue_items(day, job_key, lane, rank, fit_score, added_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued') ON CONFLICT(day, job_key) DO UPDATE SET "
                "lane = excluded.lane, rank = excluded.rank, fit_score = excluded.fit_score",
                (day, job_key, lane, rank, fit, now))
        old = conn.execute("SELECT job_key, status FROM queue_items WHERE day = ?", (day,)).fetchall()
        removed = kept = 0
        for r in old:
            if r["job_key"] in keep:
                continue
            if (r["status"] or "queued") == "queued":
                conn.execute("DELETE FROM queue_items WHERE day = ? AND job_key = ?", (day, r["job_key"]))
                removed += 1
            else:
                conn.execute("UPDATE queue_items SET rank = NULL WHERE day = ? AND job_key = ?",
                             (day, r["job_key"]))
                kept += 1
    return {"written": len(items), "removed": removed, "kept_with_status": kept}


def queue_days(db_path: Path | str | None = None) -> list[str]:
    with connect(db_path) as conn:
        return [r["day"] for r in conn.execute(
            "SELECT DISTINCT day FROM queue_items ORDER BY day DESC").fetchall()]


# --- fill plans + autofill sessions (Phase 4) -------------------------------
# Plans hold form values: they stay in the local DB and are never logged.

AUTOFILL_OPEN_STATUSES = ("starting", "filling", "needs_you", "blocked", "ready_to_submit")
AUTOFILL_SESSION_FIELDS = ("status", "outcome", "screenshot_path", "finished_at", "pid")


def save_fill_plan(job_key: str, plan: dict, *, ats: Optional[str], url: Optional[str],
                   unresolved_count: int, created_at: float, stale_after: Optional[float],
                   db_path: Path | str | None = None) -> None:
    """Store (replace) a job's current fill plan."""
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO fill_plans(job_key, ats, url, plan, unresolved_count, created_at, stale_after) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(job_key) DO UPDATE SET ats = excluded.ats, "
            "url = excluded.url, plan = excluded.plan, unresolved_count = excluded.unresolved_count, "
            "created_at = excluded.created_at, stale_after = excluded.stale_after",
            (job_key, ats, url, json.dumps(plan, ensure_ascii=False, sort_keys=True),
             int(unresolved_count), float(created_at), stale_after))


def get_fill_plan(job_key: str, db_path: Path | str | None = None) -> Optional[dict]:
    """{job_key, ats, url, plan (dict), unresolved_count, created_at, stale_after} or None."""
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM fill_plans WHERE job_key = ?", (job_key,)).fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["plan"] = json.loads(out["plan"]) if out.get("plan") else None
    except ValueError:
        out["plan"] = None
    return out


def set_fill_status(job_key: str, fill_status: Optional[str],
                    db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE jobs SET fill_status = ? WHERE key = ?", (fill_status, job_key))


def _decode_session(row: dict) -> dict:
    v = row.get("outcome")
    if isinstance(v, str) and v:
        try:
            row["outcome"] = json.loads(v)
        except ValueError:
            row["outcome"] = None
    row["open"] = row.get("status") in AUTOFILL_OPEN_STATUSES
    return row


def _reap_sessions(conn: sqlite3.Connection) -> None:
    """Open sessions whose process (this host) died can never finish: mark them abandoned."""
    for r in conn.execute("SELECT id, pid FROM autofill_sessions WHERE status IN "
                          f"({','.join('?' * len(AUTOFILL_OPEN_STATUSES))})",
                          AUTOFILL_OPEN_STATUSES).fetchall():
        if r["pid"] is not None and not _pid_alive(r["pid"]):
            conn.execute("UPDATE autofill_sessions SET status = 'abandoned', finished_at = ? "
                         "WHERE id = ?", (time.time(), r["id"]))


def claim_autofill_session(job_key: str, db_path: Path | str | None = None
                           ) -> tuple[Optional[int], Optional[dict]]:
    """Atomically start a session unless one is open: (new_id, None) or (None, open_session).
    One browser fill at a time (the persistent profile can only be opened once)."""
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _reap_sessions(conn)
        row = conn.execute("SELECT * FROM autofill_sessions WHERE status IN "
                           f"({','.join('?' * len(AUTOFILL_OPEN_STATUSES))}) "
                           "ORDER BY started_at DESC LIMIT 1", AUTOFILL_OPEN_STATUSES).fetchone()
        if row:
            return None, _decode_session(dict(row))
        cur = conn.execute("INSERT INTO autofill_sessions(job_key, status, started_at, pid) "
                           "VALUES (?, 'starting', ?, ?)", (job_key, time.time(), os.getpid()))
        return int(cur.lastrowid), None


def update_autofill_session(session_id: int, db_path: Path | str | None = None,
                            **fields: Any) -> None:
    bad = set(fields) - set(AUTOFILL_SESSION_FIELDS)
    if bad:
        raise ValueError(f"unknown session fields: {sorted(bad)}")
    if not fields:
        return
    if "outcome" in fields and fields["outcome"] is not None and not isinstance(fields["outcome"], str):
        fields["outcome"] = json.dumps(fields["outcome"], ensure_ascii=False, sort_keys=True)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect(db_path) as conn:
        conn.execute(f"UPDATE autofill_sessions SET {cols} WHERE id = ?", (*fields.values(), session_id))


def get_autofill_session(session_id: int, db_path: Path | str | None = None) -> Optional[dict]:
    with connect(db_path) as conn:
        _reap_sessions(conn)
        row = conn.execute("SELECT * FROM autofill_sessions WHERE id = ?", (session_id,)).fetchone()
    return _decode_session(dict(row)) if row else None


def list_autofill_sessions(status: Optional[Iterable[str]] = None, job_key: Optional[str] = None,
                           limit: int = 50, db_path: Path | str | None = None) -> list[dict]:
    """Sessions newest first, optionally filtered by status(es) and job."""
    sql, args = "SELECT * FROM autofill_sessions WHERE 1 = 1", []
    if status is not None:
        st = list(status)
        sql += f" AND status IN ({','.join('?' * len(st)) or 'NULL'})"
        args += st
    if job_key is not None:
        sql += " AND job_key = ?"
        args.append(job_key)
    sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
    args.append(int(limit))
    with connect(db_path) as conn:
        _reap_sessions(conn)
        return [_decode_session(dict(r)) for r in conn.execute(sql, args).fetchall()]
