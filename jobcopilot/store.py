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
)


def _row_from_filter_result(r: FilterResult) -> dict[str, Any]:
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
    )


def upsert_jobs(results: Iterable[FilterResult], db_path: Path | str | None = None) -> tuple[int, int]:
    """Insert new keys; update posting fields + last_seen on existing keys.

    Preserves status, notes, scores, applied_version_id on update.
    Returns (inserted, updated).
    """
    now = time.time()
    inserted = 0
    updated = 0
    with connect(db_path) as conn:
        for r in results:
            row = _row_from_filter_result(r)
            existing = conn.execute("SELECT key FROM jobs WHERE key = ?", (row["key"],)).fetchone()
            if existing:
                set_clause = ", ".join(f"{f} = :{f}" for f in _POSTING_FIELDS)
                row["_last"] = now
                conn.execute(
                    f"UPDATE jobs SET {set_clause}, last_seen = :_last WHERE key = :key",
                    row,
                )
                updated += 1
            else:
                row["first_seen"] = now
                row["last_seen"] = now
                cols = list(row.keys())
                conn.execute(
                    f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
                    row,
                )
                inserted += 1
    return inserted, updated


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
# keep the two in sync.
_FIT_SQL = (
    "ROUND(CASE WHEN match_score IS NOT NULL AND ats_coverage IS NOT NULL "
    "THEN 0.5 * match_score + 0.5 * (ats_coverage * 100.0) "
    "ELSE match_score END, 1)"
)


def _fit_score(ms: Optional[float], ats: Optional[float]) -> Optional[float]:
    if ms is not None and ats is not None:
        return round(0.5 * ms + 0.5 * (ats * 100.0), 1)
    if ms is not None:
        return round(ms, 1)
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
    return [_derive(_decode_keywords(r), user_yoe) for r in rows]


def get_job(key: str, db_path: Path | str | None = None) -> Optional[dict]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()
        user_yoe = _user_yoe(conn)
    if not row:
        return None
    return _derive(_decode_keywords(dict(row)), user_yoe)


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
