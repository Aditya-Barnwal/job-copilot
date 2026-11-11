"""Ingest orchestrator: roster, failure isolation, health, dedupe, API shape.

Fully offline: every connector is a fake registered into ingest.REGISTRY, the
config dir is a tmp dir, and the companies module is a stub (the real one may
not exist yet, or may read live state).
"""
from __future__ import annotations

import sys
import types

import pytest
import yaml

import jobcopilot
from jobcopilot import ingest as ingest_mod
from jobcopilot import store
from jobcopilot.filters import FilterConfig
from jobcopilot.http import HostDenied, RobotsDisallowed
from jobcopilot.models import Job


def _job(source, company, ext, title="Software Engineer", **kw) -> Job:
    return Job(source=source, company=company, company_sector="tech", external_id=ext,
               title=title, location_text="Remote", workplace_type="remote",
               country_bucket="unknown", url=kw.pop("url", f"https://{source}.test/{ext}"),
               description_text="Python services", **kw)


def _fake(outcome, source="ashby"):
    class Fake:
        def __init__(self):
            self.source = source

        def fetch(self, cfg):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome(cfg) if callable(outcome) else list(outcome)

    return Fake


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    (d / "companies.yaml").write_text(yaml.safe_dump({"companies": [
        {"name": "Acme", "connector": "ok", "slug": "acme"},
        {"name": "Boom", "connector": "boom"},
        {"name": "Walled", "connector": "robots"},
        {"name": "LinkedOut", "connector": "denied"},
        {"name": "Beta", "connector": "manual", "note": "by hand"},
    ]}), encoding="utf-8")
    (d / "target_companies.yaml").write_text(yaml.safe_dump({"companies": [
        {"name": "Acme Inc.", "connector": "ok", "slug": "dupe"},   # same company: ignored
        {"name": "Beta", "connector": "ok", "slug": "beta"},        # replaces the manual entry
    ]}), encoding="utf-8")
    (d / "sources.yaml").write_text(yaml.safe_dump({"boards": {
        "himalayas": {"enabled": True},
        "remoteok": {"enabled": True},        # no connector registered -> unavailable
        "jobspresso": {"enabled": False},
    }}), encoding="utf-8")
    monkeypatch.setenv("JOBCOPILOT_CONFIG_DIR", str(d))
    return d


@pytest.fixture
def fakes(monkeypatch):
    def ats(cfg):
        return [_job("ashby", cfg["name"], f"{cfg['slug']}-1", url=f"https://jobs.ashbyhq.com/{cfg['slug']}/1")]

    def board(_cfg):
        return [
            _job("himalayas", "Acme, Inc.", "h1", apply_url="https://himalayas.app/apply/h1",
                 attribution_name="Himalayas", attribution_url="https://himalayas.app/h1"),
            _job("himalayas", "Solo", "h2", apply_url="https://himalayas.app/apply/h2",
                 attribution_name="Himalayas", attribution_url="https://himalayas.app/h2"),
            _job("himalayas", "Gone", "h3", expires_at="2001-01-01"),
        ]

    monkeypatch.setattr(ingest_mod, "REGISTRY", {
        "ok": _fake(ats),
        "boom": _fake(RuntimeError("upstream 500")),
        "robots": _fake(RobotsDisallowed("https://walled.test/jobs")),
        "denied": _fake(HostDenied("https://www.linkedin.com/jobs", "do-not-automate host")),
        "himalayas": _fake(board, source="himalayas"),
    })
    stub = types.ModuleType("jobcopilot.companies")
    stub.enabled_ats_companies = lambda db_path=None: [
        {"name": "Disco", "connector": "ok", "slug": "disco"},
        {"name": "ACME", "connector": "ok", "slug": "acme-again"},   # already in config: skipped
    ]
    stub.get_company = lambda name, db_path=None: {"id": 7} if name == "Acme" else None
    monkeypatch.setitem(sys.modules, "jobcopilot.companies", stub)
    monkeypatch.setattr(jobcopilot, "companies", stub, raising=False)
    return stub


def test_roster_merges_files_and_discovered_by_name(cfg_dir, fakes):
    roster = ingest_mod.company_roster()
    names = [c["name"] for c in roster]
    assert names == ["Acme", "Boom", "Walled", "LinkedOut", "Beta", "Disco"]
    assert next(c for c in roster if c["name"] == "Beta")["connector"] == "ok"
    assert next(c for c in roster if c["name"] == "Acme")["slug"] == "acme"


def test_pipeline_isolates_failures_records_health_and_dedupes(cfg_dir, fakes):
    summary = ingest_mod.run_pipeline(filter_cfg=FilterConfig(target_roles=["software engineer"]),
                                      enrich_workday=False)
    src = summary["sources"]
    assert src["ok:acme"]["status"] == "ok" and src["ok:acme"]["items"] == 1
    assert src["boom:boom"]["status"] == "error"
    assert src["robots:walled"]["status"] == "robots_disallowed"
    assert src["denied:linkedout"]["status"] == "denied_host"
    assert src["himalayas"]["status"] == "ok" and src["himalayas"]["items"] == 3
    assert src["remoteok"]["status"] == "unavailable"
    assert "jobspresso" not in src
    skipped = {s["source"]: s["status"] for s in summary["skipped"]}
    assert skipped == {"boom:boom": "error", "robots:walled": "robots_disallowed",
                       "denied:linkedout": "denied_host", "remoteok": "unavailable"}
    assert summary["manual"] == []          # Beta's manual entry was replaced

    # Acme, Beta, Disco from ATS + 3 board rows; 1 expired, 1 duplicate of Acme.
    assert summary["ingested_raw"] == 6
    assert summary["expired_dropped"] == 1
    assert summary["duplicates_merged"] == 1
    assert summary["merged"] == summary["passed"] == summary["inserted"] == 4
    assert summary["by_connector"] == {"ok": 3, "himalayas": 3}

    acme = store.get_job("ashby:Acme:acme-1")
    assert {s["source"] for s in acme["sources"]} == {"ashby", "himalayas"}
    assert acme["apply_url"] == "https://jobs.ashbyhq.com/acme/1"
    assert acme["company_id"] == 7
    solo = store.get_job("himalayas:Solo:h2")
    assert solo["apply_url"] == "https://himalayas.app/apply/h2"
    assert solo["attribution_name"] == "Himalayas"

    ok = store.get_source_state("ok:acme")
    assert ok["last_ok"] and ok["last_error"] is None and ok["items"] == 1
    boom = store.get_source_state("boom:boom")
    assert boom["last_ok"] is None and boom["last_error"].startswith("error: RuntimeError")
    assert store.get_source_state("robots:walled")["last_error"].startswith("robots_disallowed")

    # Re-run: same rows updated, user state kept.
    store.set_status("ashby:Acme:acme-1", "applied")
    again = ingest_mod.run_pipeline(filter_cfg=FilterConfig(target_roles=["software engineer"]),
                                    enrich_workday=False)
    assert again["inserted"] == 0 and again["updated"] == 4
    assert store.get_job("ashby:Acme:acme-1")["status"] == "applied"


def test_missing_companies_module_degrades_gracefully(cfg_dir, fakes, monkeypatch):
    broken = types.ModuleType("jobcopilot.companies")   # no functions at all
    monkeypatch.setitem(sys.modules, "jobcopilot.companies", broken)
    monkeypatch.setattr(jobcopilot, "companies", broken, raising=False)
    summary = ingest_mod.run_pipeline(filter_cfg=FilterConfig(target_roles=["software engineer"]),
                                      enrich_workday=False, include_boards=False)
    assert "ok:disco" not in summary["sources"]
    assert summary["sources"]["ok:acme"]["status"] == "ok"


def test_api_ingest_summary_and_sources_health(cfg_dir, fakes, monkeypatch):
    from fastapi.testclient import TestClient

    import server.app as server_app
    from server import deps

    monkeypatch.setattr(deps, "_company_meta", None)
    monkeypatch.setattr(server_app, "setup_logging", lambda _dir: None)
    with TestClient(server_app.app) as c:
        r = c.post("/api/ingest", json={"roles": ["software engineer"]})
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("ingested_raw", "passed", "inserted", "updated", "workday_details_filled",
                  "rescored", "sources", "skipped"):
            assert k in body
        assert isinstance(body["sources"], dict) and isinstance(body["skipped"], list)
        assert body["inserted"] == 4

        h = c.get("/api/sources/health")
        assert h.status_code == 200
        rows = {row["source"]: row for row in h.json()["sources"]}
        assert rows["ok:acme"]["items"] == 1 and rows["boom:boom"]["last_error"]
