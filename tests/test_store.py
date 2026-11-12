"""Store + migrations: schema upgrades, call-time path resolution, filters.

Every test runs against the tmp DB the autouse fixture in conftest.py sets up.
"""
from __future__ import annotations

import inspect
import sqlite3
import time
from pathlib import Path

import pytest

from jobcopilot import migrations, store, versions
from jobcopilot.filters import FilterResult
from jobcopilot.models import Job


# The schema exactly as the first commit shipped it: no YOE columns, no
# schema_version table. Rows written with it must survive the upgrade.
LEGACY_SCHEMA = """
CREATE TABLE jobs (
    key TEXT PRIMARY KEY, source TEXT NOT NULL, company TEXT NOT NULL,
    company_sector TEXT, external_id TEXT, title TEXT NOT NULL, role_family TEXT,
    location_text TEXT, workplace_type TEXT, country_bucket TEXT, url TEXT,
    description_text TEXT, department TEXT, comp_currency TEXT, comp_min REAL,
    comp_max REAL, comp_interval TEXT, comp_summary TEXT, posted_at TEXT,
    posted_age_days INTEGER, freshness TEXT, package_estimated INTEGER DEFAULT 0,
    country_uncertain INTEGER DEFAULT 0, seniority_high INTEGER DEFAULT 0,
    match_score REAL, ats_coverage REAL, required_keywords TEXT,
    missing_keywords TEXT, applied_version_id INTEGER, status TEXT DEFAULT 'new',
    notes TEXT, first_seen REAL, last_seen REAL
);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE TABLE resume_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, role_family TEXT,
    source_job_key TEXT, source_company TEXT, tailored_text TEXT, pdf_path TEXT,
    target_keywords TEXT, base_ats_coverage REAL, created_at REAL
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _columns(db: Path, table: str) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _versions_applied(db: Path) -> list[tuple[int, str]]:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT version, name FROM schema_version ORDER BY version").fetchall()


def _seed_legacy(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO jobs(key, source, company, title, status, notes, match_score) "
            "VALUES ('gh:Foo:1', 'greenhouse', 'Foo', 'Backend Engineer', 'applied', 'called back', 61.5)"
        )
        conn.execute("INSERT INTO resume_versions(label, tailored_text) VALUES ('v1', 'text')")
        conn.execute("INSERT INTO meta(k, v) VALUES ('last_ingest', '123')")


def _job(external_id: str, title: str = "Backend Engineer", seniority_high: bool = False) -> FilterResult:
    job = Job(
        source="greenhouse", company="Foo", company_sector="tech",
        external_id=external_id, title=title, location_text="Bengaluru",
        workplace_type="onsite", country_bucket="IN", url="https://example.test/j",
        description_text="d", seniority_high=seniority_high,
    )
    return FilterResult(job=job, package_estimated=False, country_uncertain=False)


# --- migrations ----------------------------------------------------------

def _ALL_STEPS() -> list[tuple[int, str]]:
    return [(v, n) for v, n, _ in migrations.MIGRATIONS]


def test_fresh_db_migrates_to_latest(tmp_path):
    db = tmp_path / "fresh.db"
    assert store.init_db(db) == migrations.latest_version()
    assert _versions_applied(db) == _ALL_STEPS()
    cols = _columns(db, "jobs")
    assert {"min_yoe_required", "yoe_is_preferred", "match_score", "status"} <= cols
    assert _columns(db, "meta") == {"k", "v"}


def test_legacy_db_without_schema_version_upgrades_preserving_rows(tmp_path):
    db = tmp_path / "legacy.db"
    _seed_legacy(db)
    assert "min_yoe_required" not in _columns(db, "jobs")

    assert store.init_db(db) == migrations.latest_version()

    assert {"min_yoe_required", "yoe_is_preferred"} <= _columns(db, "jobs")
    job = store.get_job("gh:Foo:1", db_path=db)
    assert job["status"] == "applied"
    assert job["notes"] == "called back"
    assert job["match_score"] == 61.5
    assert job["yoe_is_preferred"] == 0  # column default applied to old rows
    assert [v["label"] for v in store.list_versions(db_path=db)] == ["v1"]
    assert store.get_meta("last_ingest", db_path=db) == "123"


def test_pre_migrations_db_with_all_columns_is_stamped_not_rebuilt(tmp_path):
    """The real on-disk DB: every v1 column already present via the old ad-hoc
    ALTERs, but no schema_version table."""
    db = tmp_path / "current.db"
    _seed_legacy(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE jobs ADD COLUMN min_yoe_required INTEGER")
        conn.execute("ALTER TABLE jobs ADD COLUMN yoe_is_preferred INTEGER DEFAULT 0")
        conn.execute("UPDATE jobs SET min_yoe_required = 3")
        before = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0]

    # The v1 baseline only stamps such a DB; later steps are additive by design.
    assert migrations.migrate(db, migrations.MIGRATIONS[:1]) == 1

    with sqlite3.connect(db) as conn:
        after = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0]
    assert after == before  # table untouched, only stamped
    assert store.get_job("gh:Foo:1", db_path=db)["min_yoe_required"] == 3


def test_migrate_is_idempotent_and_applies_new_steps_once(tmp_path):
    db = tmp_path / "idem.db"
    store.init_db(db)
    store.init_db(db)
    assert _versions_applied(db) == _ALL_STEPS()

    calls: list[int] = []

    def _v2(conn: sqlite3.Connection) -> None:
        calls.append(1)
        migrations.add_column_if_missing(conn, "jobs", "tier", "TEXT")

    steps = [*migrations.MIGRATIONS, (migrations.latest_version() + 1, "add_tier", _v2)]
    assert migrations.migrate(db, steps) == migrations.latest_version() + 1
    assert migrations.migrate(db, steps) == migrations.latest_version() + 1
    assert calls == [1]
    assert "tier" in _columns(db, "jobs")


def test_failing_step_rolls_back_ddl_and_version(tmp_path):
    db = tmp_path / "boom.db"
    store.init_db(db)

    def _boom(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE half_done (x INTEGER)")
        raise RuntimeError("step failed")

    with pytest.raises(RuntimeError):
        migrations.migrate(db, [*migrations.MIGRATIONS, (migrations.latest_version() + 1, "boom", _boom)])
    assert _versions_applied(db) == _ALL_STEPS()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'half_done'").fetchone() is None


def test_migration_versions_must_ascend(tmp_path):
    noop = lambda conn: None  # noqa: E731
    with pytest.raises(ValueError):
        migrations.migrate(tmp_path / "x.db", [(2, "b", noop), (1, "a", noop)])


# --- call-time path resolution (F6) --------------------------------------

def test_conftest_points_store_at_tmp(tmp_path):
    assert tmp_path in store.resolve_db_path().parents
    assert tmp_path in store.resolve_resumes_dir().parents


def test_no_public_store_function_binds_db_path_at_def_time():
    for name, fn in inspect.getmembers(store, inspect.isfunction):
        param = inspect.signature(fn).parameters.get("db_path")
        if param is not None and fn.__module__ == store.__name__:
            assert param.default is None, name


def test_db_path_attribute_resolved_at_call_time(tmp_path, monkeypatch):
    other = tmp_path / "other" / "x.db"
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", other)
    store.init_db()
    store.set_meta("probe", "1")
    assert other.exists()
    assert store.get_meta("probe", db_path=other) == "1"
    assert store.get_meta("probe", db_path=tmp_path / "data" / "jobcopilot.db") is None


def test_env_var_resolved_at_call_time_when_attribute_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", store._IMPORT_DB_PATH)
    monkeypatch.setattr(store, "RESUMES_DIR", store._IMPORT_RESUMES_DIR)
    monkeypatch.delenv("JOBCOPILOT_RESUMES_DIR")
    target = tmp_path / "env" / "y.db"
    monkeypatch.setenv("JOBCOPILOT_DB", str(target))
    assert store.resolve_db_path() == target
    # PDFs follow the DB when no explicit résumé dir is configured.
    assert store.resolve_resumes_dir() == target.parent / "resumes"


def test_saved_version_pdf_lands_in_redirected_dir_and_survives_markup(tmp_path):
    """F8: résumé text full of XML metacharacters must not crash reportlab."""
    text = "C++ & Go <5 ms p99 >99.9% uptime\nAT&T <b>not bold</b>\n\nSecond para & more"
    v = versions.save_tailored_version(
        label="t", role_family="backend_engineer", source_job_key=None,
        source_company="Foo & Co", tailored_text=text, target_keywords=[],
        base_ats_coverage=0.0,
    )
    pdf = Path(v["pdf_path"])
    assert pdf.exists() and tmp_path in pdf.parents

    from pypdf import PdfReader
    extracted = PdfReader(str(pdf)).pages[0].extract_text()
    assert "C++ & Go <5 ms" in extracted
    assert "<b>not bold</b>" in extracted


# --- list_jobs filters (F9) ----------------------------------------------

def test_min_score_filters_on_fit_score_not_match_score():
    store.upsert_jobs([_job("a"), _job("b"), _job("c"), _job("d")])
    store.set_scores("greenhouse:Foo:a", 80.0, 0.2, [], [])   # fit 50
    store.set_scores("greenhouse:Foo:b", 40.0, 0.9, [], [])   # fit 65
    with store.connect() as conn:                               # fit 70 (no ATS)
        conn.execute("UPDATE jobs SET match_score = 70 WHERE key = 'greenhouse:Foo:c'")
    # d is unscored: fit None, never passes a min_score filter

    rows = store.list_jobs(min_score=60)
    assert {r["external_id"] for r in rows} == {"b", "c"}
    assert all(r["fit_score"] >= 60 for r in rows)
    assert {r["external_id"] for r in store.list_jobs()} == {"a", "b", "c", "d"}


def test_hide_senior_high_only_when_asked():
    store.upsert_jobs([_job("jr"), _job("sr", title="Staff Engineer", seniority_high=True)])
    assert {r["external_id"] for r in store.list_jobs()} == {"jr", "sr"}
    assert {r["external_id"] for r in store.list_jobs(hide_senior_high=True)} == {"jr"}


def test_fit_score_rounding_matches_sql_filter(tmp_path):
    """Python display value and SQL min_score filter must round identically
    (62.5 match + 0.62 ATS -> exactly 62.25 -> shown 62.3, filter 62.3 keeps it)."""
    import sqlite3 as _sq
    db = tmp_path / "round.db"
    store.init_db(db)
    with _sq.connect(db) as c:
        c.execute("INSERT INTO jobs(key, source, company, title, match_score, ats_coverage) "
                  "VALUES ('k1', 'greenhouse', 'Acme', 'SWE', 62.5, 0.62)")
    job = store.get_job("k1", db_path=db)
    assert job["fit_score"] == 62.3
    assert [r["key"] for r in store.list_jobs(min_score=62.3, db_path=db)] == ["k1"]
    assert store.list_jobs(min_score=62.4, db_path=db) == []


# --- runs: atomic claim, abandoned rows (P2 / P10) ---------------------------

def test_claim_run_is_exclusive():
    rid, live = store.claim_run("daily")
    assert rid and live is None
    again, live = store.claim_run("daily")
    assert again is None and live["id"] == rid
    assert store.claim_run("other")[0]                 # per kind


def test_claim_run_is_atomic_across_threads():
    import threading
    got, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        got.append(store.claim_run("daily")[0])
    ts = [threading.Thread(target=go) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len([g for g in got if g]) == 1


def test_running_run_abandons_dead_or_stale_rows():
    import subprocess
    import sys as _sys
    proc = subprocess.Popen([_sys.executable, "-c", "pass"])
    proc.wait()                                         # a pid that no longer exists
    dead = store.start_run("daily")
    old = store.start_run("daily")
    with store.connect() as conn:
        conn.execute("UPDATE runs SET pid = ? WHERE id = ?", (proc.pid, dead))
        conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (time.time() - 7 * 3600, old))
    assert store.running_run("daily") is None
    for rid in (dead, old):
        row = store.get_run(rid)
        assert row["status"] == "failed" and row["error"].startswith("abandoned") and row["finished_at"]
    assert store.claim_run("daily")[0]


def test_connect_uses_wal_and_a_busy_timeout():
    with store.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
