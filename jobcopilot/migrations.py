"""Versioned SQLite schema migrations.

Why a table of ordered steps instead of ad-hoc `ALTER TABLE` checks: every
later phase adds columns/tables, and the DB on disk is the user's only copy of
their tracker. Each step runs exactly once, inside a transaction, and is
recorded in `schema_version`, so a half-applied upgrade can't leave the DB in
an unknown shape and "which schema am I on?" has a single answer.

Adding a step: append `(next_version, "short_name", fn)` to MIGRATIONS. `fn`
receives a connection that is already inside a transaction; it must not
commit. Never edit or reorder a step that has shipped — write a new one.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("jobcopilot.migrations")

MigrationFn = Callable[[sqlite3.Connection], None]
Migration = tuple[int, str, MigrationFn]


# --- helpers ---------------------------------------------------------------

def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> bool:
    """Idempotent ADD COLUMN — SQLite has no `ADD COLUMN IF NOT EXISTS`."""
    if column in _columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


# --- steps -----------------------------------------------------------------

# v1 is the schema v1 shipped with, *including* the two columns that used to be
# added by ad-hoc ALTERs. Every statement is idempotent so a pre-migrations DB
# (all tables present, no schema_version) is simply stamped as v1, and a DB
# from before the YOE columns gains them without losing rows.
_V1_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS jobs (
        key TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        company TEXT NOT NULL,
        company_sector TEXT,
        external_id TEXT,
        title TEXT NOT NULL,
        role_family TEXT,
        location_text TEXT,
        workplace_type TEXT,
        country_bucket TEXT,
        url TEXT,
        description_text TEXT,
        department TEXT,
        comp_currency TEXT,
        comp_min REAL,
        comp_max REAL,
        comp_interval TEXT,
        comp_summary TEXT,
        posted_at TEXT,
        posted_age_days INTEGER,
        freshness TEXT,
        package_estimated INTEGER DEFAULT 0,
        country_uncertain INTEGER DEFAULT 0,
        seniority_high INTEGER DEFAULT 0,
        min_yoe_required INTEGER,
        yoe_is_preferred INTEGER DEFAULT 0,
        match_score REAL,
        ats_coverage REAL,
        required_keywords TEXT,
        missing_keywords TEXT,
        applied_version_id INTEGER,
        status TEXT DEFAULT 'new',
        notes TEXT,
        first_seen REAL,
        last_seen REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_role ON jobs(role_family)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_freshness ON jobs(freshness)",
    """CREATE TABLE IF NOT EXISTS resume_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT,
        role_family TEXT,
        source_job_key TEXT,
        source_company TEXT,
        tailored_text TEXT,
        pdf_path TEXT,
        target_keywords TEXT,
        base_ats_coverage REAL,
        created_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT
    )""",
)


def _v1_baseline(conn: sqlite3.Connection) -> None:
    for stmt in _V1_STATEMENTS:
        conn.execute(stmt)
    # The two forward migrations v1 used to run on every startup.
    add_column_if_missing(conn, "jobs", "min_yoe_required", "INTEGER")
    add_column_if_missing(conn, "jobs", "yoe_is_preferred", "INTEGER DEFAULT 0")


MIGRATIONS: list[Migration] = [
    (1, "baseline", _v1_baseline),
]


def latest_version() -> int:
    return max(v for v, _, _ in MIGRATIONS)


# --- runner ----------------------------------------------------------------

_VERSION_TABLE = """CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at REAL NOT NULL
)"""


def current_version(conn: sqlite3.Connection) -> int:
    """0 for a brand-new or pre-migrations DB."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)


def _check_order(steps: list[Migration]) -> None:
    versions = [v for v, _, _ in steps]
    if versions != sorted(set(versions)) or (versions and versions[0] < 1):
        raise ValueError(f"migration versions must be unique, ascending, >= 1: {versions}")


def migrate(db_path: Path | str, steps: list[Migration] | None = None) -> int:
    """Apply every pending step in order; return the resulting version.

    Each step gets its own `BEGIN IMMEDIATE` transaction (DDL included — the
    connection is in manual-transaction mode) and the version is re-read after
    the write lock is taken, so two processes starting at once (server + the
    nightly job) can't apply the same step twice.
    """
    steps = MIGRATIONS if steps is None else steps
    _check_order(steps)
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    try:
        conn.execute(_VERSION_TABLE)
        for version, name, fn in steps:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if current_version(conn) >= version:
                    conn.execute("ROLLBACK")
                    continue
                fn(conn)
                conn.execute(
                    "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, time.time()),
                )
                conn.execute("COMMIT")
                log.info("schema migrated to v%d (%s)", version, name)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        final = current_version(conn)
        known = max((v for v, _, _ in steps), default=0)
        if final > known:
            # A newer checkout migrated this DB. Additive steps keep old code
            # working, so warn rather than refuse to start.
            log.warning("DB schema v%d is newer than this code knows (v%d)", final, known)
        return final
    finally:
        conn.close()
