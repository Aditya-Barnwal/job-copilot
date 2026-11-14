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


def _v2_sources_companies(conn: sqlite3.Connection) -> None:
    """Phase 1: multi-source jobs, company records with tier signals, source state.

    - jobs gains the fields boards provide (apply link, region / time-zone
      restrictions, attribution, expiry) plus `canonical_key` for cross-source
      dedupe and `company_id` linking to `companies`.
    - job_sources keeps EVERY place a job was seen (a role on Himalayas and on
      the company's Ashby board is one job with two sources) — needed for
      attribution and for per-source apply-link rules (docs/SOURCES.md).
    - companies holds ATS detection results and tier signals (brand / funded /
      unknown / excluded) with the evidence for each, so a hard filter can
      always explain itself.
    - source_state remembers ETags and health per source for polite,
      diff-based refreshes (e.g. Bangalore Startup Map once a day).
    """
    for col, decl in (
        ("canonical_key", "TEXT"),
        ("apply_url", "TEXT"),
        ("region_restrictions", "TEXT"),      # JSON list[str], e.g. ["India", "APAC"]
        ("timezone_restrictions", "TEXT"),    # JSON list[float] of UTC offsets
        ("tz_requirement_text", "TEXT"),      # free-text TZ requirement as posted
        ("attribution_name", "TEXT"),         # e.g. "Himalayas"
        ("attribution_url", "TEXT"),          # followable link back to the source listing
        ("expires_at", "TEXT"),
        ("company_id", "INTEGER"),
    ):
        add_column_if_missing(conn, "jobs", col, decl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_canonical ON jobs(canonical_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_company_id ON jobs(company_id)")

    conn.execute("""CREATE TABLE IF NOT EXISTS job_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_key TEXT NOT NULL,
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        url TEXT,
        apply_url TEXT,
        attribution_name TEXT,
        attribution_url TEXT,
        first_seen REAL,
        last_seen REAL,
        UNIQUE(source, external_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_sources_job ON job_sources(job_key)")

    conn.execute("""CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        normalized_name TEXT NOT NULL UNIQUE,
        domain TEXT,
        website TEXT,
        careers_url TEXT,
        ats TEXT,                 -- greenhouse | lever | ashby | workday | smartrecruiters | gem | manual | ...
        ats_slug TEXT,
        ats_tenant TEXT,
        ats_wd_server TEXT,
        ats_site TEXT,
        ats_region TEXT,          -- e.g. 'eu' for EU-hosted Greenhouse boards
        ats_confidence REAL,
        ats_evidence TEXT,        -- how detection decided (for debugging)
        ats_checked_at REAL,
        tier TEXT DEFAULT 'unknown',   -- brand | funded | unknown | excluded
        tier_reasons TEXT,        -- JSON list of human-readable reasons with sources
        funding_stage TEXT,
        investors TEXT,           -- JSON list
        yc_batch TEXT,
        yc_top_company INTEGER DEFAULT 0,
        team_size INTEGER,
        sector TEXT,
        hq_location TEXT,
        signal_sources TEXT,      -- JSON list, e.g. ["config", "yc-oss", "bsm"]
        apply_channel TEXT,       -- ats | waas | email | manual
        enabled INTEGER DEFAULT 1,
        notes TEXT,
        first_seen REAL,
        last_seen REAL,
        updated_at REAL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_companies_tier ON companies(tier)")

    conn.execute("""CREATE TABLE IF NOT EXISTS source_state (
        source TEXT PRIMARY KEY,
        etag TEXT,
        last_modified TEXT,
        content_hash TEXT,
        last_run REAL,
        last_ok REAL,
        last_error TEXT,
        items INTEGER,
        state TEXT                -- JSON, source-specific (e.g. per-company hashes)
    )""")


def _v3_eligibility_queue(conn: sqlite3.Connection) -> None:
    """Phase 2: eligibility decisions, fit scores, the daily queue, runs, tracker.

    - jobs gains the eligibility Decision (lane, excluded + reasons, soft flags,
      YOE range/evidence, India eligibility, annual INR pay) so every exclusion
      is stored and explainable (D8), and the v2 fit score with its components.
    - jobs gains tracker fields: when the status last changed and, for a
      submission, when / how it was detected and where the proof lives (P4).
    - companies gains the probe's columns (probe currently writes ats_evidence).
    - runs records each nightly/manual run with per-step results; queue_items
      holds "today's best" per day; status_history keeps every status change
      (undo = set an earlier status; both rows stay).
    """
    for col, decl in (
        ("lane", "TEXT"),                     # main | extended | excluded
        ("excluded", "INTEGER DEFAULT 0"),
        ("exclusion_reasons", "TEXT"),        # JSON [{rule, detail, excerpt}]
        ("flags", "TEXT"),                    # JSON [{rule: "flag:...", detail, excerpt}]
        ("yoe_max", "INTEGER"),
        ("yoe_evidence", "TEXT"),
        ("fresher_friendly", "INTEGER"),
        ("india_eligible", "INTEGER"),
        ("salary_annual_inr", "REAL"),
        ("fit_score_v2", "REAL"),
        ("fit_components", "TEXT"),           # JSON {components: {...}, weights: {...}}
        ("matched_skills", "TEXT"),           # JSON list
        ("missing_skills", "TEXT"),           # JSON list
        ("fit_why", "TEXT"),
        ("scored_at", "REAL"),
        ("evaluated_at", "REAL"),
        ("status_changed_at", "REAL"),
        ("submitted_at", "REAL"),
        ("submit_source", "TEXT"),            # user | detected
        ("submit_proof_path", "TEXT"),
    ):
        add_column_if_missing(conn, "jobs", col, decl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_lane ON jobs(lane, excluded)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_fit_v2 ON jobs(fit_score_v2)")

    for col, decl in (("probed_at", "REAL"), ("probe_job_count", "INTEGER"),
                      ("probe_india_count", "INTEGER")):
        add_column_if_missing(conn, "companies", col, decl)

    conn.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,           -- daily | ...
        started_at REAL NOT NULL,
        finished_at REAL,
        status TEXT NOT NULL,         -- running | ok | partial | failed
        summary TEXT,                 -- JSON: per-step results + per-source health
        error TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS queue_items (
        day TEXT NOT NULL,            -- YYYY-MM-DD in the schedule's time zone
        job_key TEXT NOT NULL,
        lane TEXT NOT NULL,           -- main | extended | near_miss
        rank INTEGER,
        fit_score REAL,
        added_at REAL,
        status TEXT DEFAULT 'queued',
        PRIMARY KEY (day, job_key)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS status_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_key TEXT NOT NULL,
        status TEXT NOT NULL,
        at REAL NOT NULL,
        source TEXT NOT NULL,         -- user | detected | system
        note TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status_history_job ON status_history(job_key, at)")


def _v4_gone_runs_backfill(conn: sqlite3.Connection) -> None:
    """Phase 2 review fixes.

    - jobs.gone_at: set when every source that listed the job ran successfully
      long after it was last seen (store.retire_unlisted); such rows leave
      Today's best / All matches but stay in the tracker with a warning.
    - jobs.eval_cfg_hash: hash of the filters config an eligibility decision
      was made with, so a config change re-evaluates every stored row.
    - runs.pid / runs.hostname: lets a 'running' row whose process died be
      recognised as abandoned instead of blocking every later run.
    - Backfill tracker dates for rows that predate v3: status_changed_at for
      every non-new row, and submitted_at (+ a 'system' history row) for
      applied/submitted rows, so the tracker never falls back to last_seen.
    """
    for table, col, decl in (("jobs", "gone_at", "REAL"), ("jobs", "eval_cfg_hash", "TEXT"),
                             ("runs", "pid", "INTEGER"), ("runs", "hostname", "TEXT")):
        add_column_if_missing(conn, table, col, decl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_gone ON jobs(gone_at)")

    conn.execute(
        "UPDATE jobs SET status_changed_at = COALESCE("
        "  (SELECT MAX(h.at) FROM status_history h WHERE h.job_key = jobs.key AND h.status = jobs.status),"
        "  first_seen) "
        "WHERE status_changed_at IS NULL AND COALESCE(status, 'new') != 'new'")
    keys = [r[0] for r in conn.execute(
        "SELECT key FROM jobs WHERE submitted_at IS NULL AND status IN ('applied', 'submitted')")]
    conn.execute(
        "UPDATE jobs SET submitted_at = COALESCE(status_changed_at, first_seen) "
        "WHERE submitted_at IS NULL AND status IN ('applied', 'submitted')")
    for k in keys:
        conn.execute(
            "INSERT INTO status_history(job_key, status, at, source, note) "
            "SELECT key, status, COALESCE(submitted_at, ?), 'system', "
            "'submission date backfilled by migration v4' FROM jobs WHERE key = ?",
            (time.time(), k))


def _v5_application_artifacts(conn: sqlite3.Connection) -> None:
    """Phase 3: versioned application artifacts (tailoring v2).

    One row per tailored version of a job's application: the base résumé, the
    LLM's edit plan, the rendered files under data/applications/<slug>/v<N>/,
    the grounding verdicts and the review status (pending -> ok | needs_review
    -> approved, or failed). `facts_hash` fingerprints the cited facts so an
    approval after the facts changed is refused. jobs.artifact_id links the
    approved version to its job.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS application_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_key TEXT NOT NULL,
        version INTEGER NOT NULL,
        base TEXT,
        edit_plan TEXT,
        tex_path TEXT,
        pdf_path TEXT,
        cover_letter_path TEXT,
        answers_json TEXT,
        grounding TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        facts_hash TEXT,
        pages INTEGER,
        created_at REAL NOT NULL,
        approved_at REAL,
        UNIQUE(job_key, version)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_job ON application_artifacts(job_key)")
    add_column_if_missing(conn, "jobs", "artifact_id", "INTEGER")


def _v6_autofill(conn: sqlite3.Connection) -> None:
    """Phase 4: fill plans (built nightly, D3) and browser-fill sessions.

    - fill_plans: one current plan per job (field -> value, source, confidence,
      plus the unresolved fields); `stale_after` is when it must be rebuilt
      before a hand-off. Values are local only (data/ never leaves the Mac).
    - autofill_sessions: one row per headed fill. status runs
      starting -> filling -> ready_to_submit | needs_you | blocked | error, then
      submitted (the user's own click, detected) | closed | timed_out | abandoned.
      `pid` lets a row whose server process died be recognised as abandoned.
    - jobs.fill_status: the latest plan / fill state, for the queue and tracker.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS fill_plans (
        job_key TEXT PRIMARY KEY,
        ats TEXT,
        url TEXT,
        plan TEXT NOT NULL,           -- JSON FillPlan.to_dict()
        unresolved_count INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        stale_after REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS autofill_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_key TEXT NOT NULL,
        status TEXT NOT NULL,
        outcome TEXT,                 -- JSON Outcome (+ reason) — labels only, never values
        screenshot_path TEXT,
        started_at REAL NOT NULL,
        finished_at REAL,
        pid INTEGER
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_autofill_sessions_status "
                 "ON autofill_sessions(status, started_at)")
    add_column_if_missing(conn, "jobs", "fill_status", "TEXT")


def _v7_fill_plans_no_values(conn: sqlite3.Connection) -> None:
    """Phase 4 hardening: fill plans keep field metadata only, never values.

    A stored plan used to carry the typed values (phone, email, CTC...) and was
    re-used for up to a day, so an answer you un-approved later could still be
    typed. Plans now store labels, sources, confidence and approval state; the
    values are resolved from answers.yaml / facts / approved files at fill time.
    This step strips the values out of every existing row and marks it stale
    (stale_after = 0), so the next fill rebuilds it instead of trusting it.
    Upload actions keep their file KEY ('resume' / 'cover_letter'), not a path.
    """
    import json
    rows = conn.execute("SELECT job_key, plan FROM fill_plans").fetchall()
    for job_key, raw in rows:
        try:
            plan = json.loads(raw) if raw else {}
        except ValueError:
            plan = {}
        if not isinstance(plan, dict):
            plan = {}
        for step in plan.get("steps") or []:
            for a in step if isinstance(step, list) else []:
                if not isinstance(a, dict):
                    continue
                a["approved"] = a.get("value") is not None and not a.get("needs_user")
                if not (a.get("kind") == "upload" and a.get("value") in ("resume", "cover_letter")):
                    a["value"] = None
        plan["files"] = {}
        conn.execute("UPDATE fill_plans SET plan = ?, stale_after = 0 WHERE job_key = ?",
                     (json.dumps(plan, ensure_ascii=False, sort_keys=True), job_key))


MIGRATIONS: list[Migration] = [
    (1, "baseline", _v1_baseline),
    (2, "sources_companies", _v2_sources_companies),
    (3, "eligibility_queue_tracker", _v3_eligibility_queue),
    (4, "gone_runs_tracker_backfill", _v4_gone_runs_backfill),
    (5, "application_artifacts", _v5_application_artifacts),
    (6, "autofill", _v6_autofill),
    (7, "fill_plans_no_values", _v7_fill_plans_no_values),
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
        try:   # WAL: the server can read while the nightly run writes
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            log.warning("could not enable WAL on %s: %s", path, exc)
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
