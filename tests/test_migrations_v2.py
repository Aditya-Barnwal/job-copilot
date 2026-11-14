"""Migration v2 (Phase 1): additive, row-preserving, idempotent."""
from __future__ import annotations

import sqlite3

from jobcopilot import migrations, store


def _cols(db, table):
    with sqlite3.connect(db) as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def test_v1_db_upgrades_to_v2_keeping_rows(tmp_path):
    db = tmp_path / "v1.db"
    assert migrations.migrate(db, migrations.MIGRATIONS[:1]) == 1
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO jobs(key, source, company, title, status) VALUES "
                  "('greenhouse:Acme:1', 'greenhouse', 'Acme', 'SWE', 'applied')")

    assert store.init_db(db) == migrations.latest_version() >= 2
    assert store.init_db(db) == migrations.latest_version()  # idempotent

    assert {"canonical_key", "apply_url", "region_restrictions", "timezone_restrictions",
            "attribution_name", "attribution_url", "expires_at", "company_id"} <= _cols(db, "jobs")
    assert {"job_key", "source", "external_id"} <= _cols(db, "job_sources")
    assert {"normalized_name", "ats", "ats_slug", "tier", "tier_reasons", "yc_batch"} <= _cols(db, "companies")
    assert {"source", "etag", "last_ok"} <= _cols(db, "source_state")
    job = store.get_job("greenhouse:Acme:1", db_path=db)
    assert job["status"] == "submitted"           # v1 "applied" renamed by migration 8


def test_job_sources_unique_per_source_listing(tmp_path):
    db = tmp_path / "u.db"
    store.init_db(db)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO job_sources(job_key, source, external_id) VALUES ('k', 'himalayas', 'x1')")
        try:
            c.execute("INSERT INTO job_sources(job_key, source, external_id) VALUES ('k2', 'himalayas', 'x1')")
            raised = False
        except sqlite3.IntegrityError:
            raised = True
    assert raised
