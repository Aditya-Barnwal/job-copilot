"""SQLite persistence for jobs, statuses, and resume versions.

The DB is the source of truth across runs. Ingest upserts; status / scores /
applied-version-links set by the human or by scoring are preserved across
re-ingests.

Paths are resolved on every call, never bound as argument defaults: a default
bound at import time once let a test write into the real DB, and it made
`monkeypatch.setattr(store, "DEFAULT_DB_PATH", ...)` silently ineffective.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from . import migrations
from .filters import FilterResult


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


@contextmanager
def connect(db_path: Path | str | None = None):
    p = resolve_db_path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
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
    for o in occurrences:
        hit = conn.execute(
            "SELECT j.key, j.source FROM job_sources s JOIN jobs j ON j.key = s.job_key "
            "WHERE s.source = ? AND s.external_id = ?", (o["source"], o["external_id"]),
        ).fetchone()
        if hit:
            return hit
    mine = {o["source"] for o in occurrences}
    for cand in conn.execute("SELECT key, source FROM jobs WHERE canonical_key = ?",
                             (row["canonical_key"],)).fetchall():
        theirs = {r["source"] for r in conn.execute(
            "SELECT source FROM job_sources WHERE job_key = ?", (cand["key"],))}
        theirs.add(cand["source"])
        if not (theirs & mine):
            return cand
    return None


def _write_sources(conn: sqlite3.Connection, job_key: str, occurrences: list[dict], now: float) -> None:
    for o in occurrences:
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
                        f"UPDATE jobs SET {set_clause}, last_seen = :_last WHERE key = :_target",
                        {**row, "_last": now, "_target": target},
                    )
                else:
                    conn.execute("UPDATE jobs SET last_seen = ?, "
                                 "company_id = COALESCE(company_id, ?) WHERE key = ?",
                                 (now, row["company_id"], target))
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
    return inserted, updated


def purge_expired(today: Optional[str] = None, db_path: Path | str | None = None) -> int:
    """Delete untouched ('new') rows whose source listing has expired (D11:
    Himalayas jobs past expiryDate are purged). Rows the user acted on stay."""
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
    return [_derive(_decode_json_lists(_decode_keywords(r)), user_yoe) for r in rows]


def get_job(key: str, db_path: Path | str | None = None) -> Optional[dict]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()
        user_yoe = _user_yoe(conn)
        sources = _sources_for(conn, key) if row else []
    if not row:
        return None
    job = _derive(_decode_json_lists(_decode_keywords(dict(row))), user_yoe)
    if not sources:  # rows stored before v2 have no job_sources yet
        sources = [{k: job.get(k) for k in ("source", "external_id", "url", "apply_url",
                                             "attribution_name", "attribution_url",
                                             "first_seen", "last_seen")}]
    job["sources"] = sources
    return job


def set_status(key: str, status: str, notes: Optional[str] = None,
               db_path: Path | str | None = None) -> bool:
    with connect(db_path) as conn:
        if notes is not None:
            cur = conn.execute("UPDATE jobs SET status = ?, notes = ? WHERE key = ?",
                               (status, notes, key))
        else:
            cur = conn.execute("UPDATE jobs SET status = ? WHERE key = ?", (status, key))
        return cur.rowcount > 0


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
