"""Nightly run (jobcopilot.daily) + LaunchAgent generation (scripts/schedule.py).

Nothing here installs a LaunchAgent or touches the network."""
from __future__ import annotations

import importlib.util
import plistlib
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import jobcopilot
from jobcopilot import daily, store
from jobcopilot import ingest as ingest_mod

ROOT = Path(__file__).resolve().parent.parent


def _summary(statuses: dict[str, str]) -> dict:
    return {"ingested_raw": 3, "merged": 3, "passed": 2, "eligible": 2, "excluded": 1,
            "inserted": 3, "updated": 0, "sources": {k: {"status": v, "items": 1, "kind": "board",
                                                         "name": k, "connector": k, "reason": ""}
                                                     for k, v in statuses.items()},
            "skipped": []}


def test_steps_are_isolated_and_recorded(monkeypatch):
    monkeypatch.setattr(ingest_mod, "run_pipeline", lambda **kw: _summary({"a": "ok", "b": "error"}))

    def broken():
        raise RuntimeError("yc down")
    res = daily.run(discovery={"yc": broken, "tiers": lambda: {"brand": 3}})
    names = [s["name"] for s in res["steps"]]
    assert names == ["discovery.yc", "discovery.tiers", "ingest", "evaluate", "score", "queue",
                     "tailor", "fill_plan"]
    by = {s["name"]: s for s in res["steps"]}
    assert by["discovery.yc"]["status"] == "error" and "yc down" in by["discovery.yc"]["error"]
    assert by["discovery.tiers"]["status"] == "ok"
    assert by["ingest"]["result"]["sources_by_status"] == {"ok": 1, "error": 1}
    assert by["tailor"]["status"] == by["fill_plan"]["status"] == "skipped"
    assert res["status"] == "partial" and res["all_sources_failed"] is False
    row = store.get_run(res["run_id"])
    assert row["status"] == "partial" and row["finished_at"] and "yc down" in row["error"]
    assert [s["name"] for s in row["summary"]["steps"]] == names


def test_all_sources_failed_means_failed_run_and_exit_1(monkeypatch):
    monkeypatch.setattr(ingest_mod, "run_pipeline", lambda **kw: _summary({"a": "error", "b": "robots_unavailable"}))
    monkeypatch.setattr(ingest_mod, "setup_logging", lambda _d: None)
    res = daily.run(skip_discovery=True)
    assert res["status"] == "failed" and res["all_sources_failed"]
    assert res["steps"][0] == {**res["steps"][0], "name": "discovery", "status": "skipped"}
    assert daily.main(["--skip-discovery"]) == 1


def test_ingest_crash_counts_as_all_failed(monkeypatch):
    def boom(**kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(ingest_mod, "run_pipeline", boom)
    res = daily.run(skip_discovery=True)
    assert res["all_sources_failed"] and res["status"] == "failed"
    assert {s["name"]: s["status"] for s in res["steps"]}["queue"] == "ok"   # later steps still ran


def test_main_refuses_while_a_run_is_live(monkeypatch, capsys):
    monkeypatch.setattr(ingest_mod, "setup_logging", lambda _d: None)
    called = []
    monkeypatch.setattr(daily, "run", lambda **kw: called.append(kw))
    store.start_run("daily")
    assert daily.main([]) == 0 and called == []
    assert "already running" in capsys.readouterr().err


def test_no_network_defaults_to_a_separate_db(monkeypatch, tmp_path):
    monkeypatch.delenv("JOBCOPILOT_DB", raising=False)
    monkeypatch.setattr(ingest_mod, "setup_logging", lambda _d: None)
    seen = {}

    def fake_run(**kw):
        seen.update(kw)
        return {"run_id": 1, "status": "ok", "all_sources_failed": False, "steps": [], "queue": None}
    monkeypatch.setattr(daily, "run", fake_run)
    assert daily.main(["--no-network"]) == 0
    assert Path(seen["db_path"]).name == "daily-fixtures.db" and seen["no_network"] is True


def test_fixture_run_produces_a_ranked_queue(monkeypatch):
    """`make daily-fixtures`: offline, real eligibility, stand-in fit (no MiniLM)."""
    fit = types.ModuleType("jobcopilot.fit")
    fit.load_profile = lambda facts_path=None: None
    seen_decisions = []

    def score(job, *, profile, cfg=None, company=None, decision=None):
        seen_decisions.append(decision)
        return SimpleNamespace(score=float(len(job["title"]) % 50 + 30), components={"role": 80.0},
                               weights={"role": 1.0}, matched_skills=[], missing_skills=[], why="stand-in")
    fit.score = score
    monkeypatch.setitem(sys.modules, "jobcopilot.fit", fit)
    monkeypatch.setattr(jobcopilot, "fit", fit, raising=False)

    res = daily.run(no_network=True, day="2026-11-12")       # discovery = config sync + tiers only
    assert not res["all_sources_failed"], res
    by = {s["name"]: s for s in res["steps"]}
    assert by["discovery.config"]["status"] == by["discovery.tiers"]["status"] == "ok"
    assert by["ingest"]["status"] == "ok"
    ing = by["ingest"]["result"]
    assert ing["evaluation"]["mode"] == "eligibility" and ing["excluded"] > 0
    q = res["queue"]
    assert q["counts"]["main"] > 0
    scores = [c["fit_score"] for c in q["main"]]
    assert scores == sorted(scores, reverse=True)
    per_company: dict[str, int] = {}
    for c in q["main"]:
        per_company[c["company"]] = per_company.get(c["company"], 0) + 1
    assert max(per_company.values()) <= 3
    assert seen_decisions and all(d["lane"] in ("main", "extended") for d in seen_decisions)
    from jobcopilot import queue as jq
    stored = jq.list_queue(day="2026-11-12")["items"]
    assert [i["key"] for i in stored] == [c["key"] for c in q["main"]]
    assert [i["rank"] for i in stored] == list(range(1, len(stored) + 1))
    excluded = jq.list_excluded(limit=5)
    # every excluded job is stored with its reasons (D8) — fixtures reuse one board file
    # per ATS, so several companies share listing ids; those must not collapse into one row
    assert excluded["total"] == ing["excluded"] > 0 and excluded["items"][0]["reasons"]
    assert ing["inserted"] == ing["eligible"] + ing["excluded"]


# --- LaunchAgent ----------------------------------------------------------------

@pytest.fixture(scope="module")
def schedule_mod():
    spec = importlib.util.spec_from_file_location("jc_schedule", ROOT / "scripts" / "schedule.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plist_round_trip(schedule_mod, tmp_path):
    data = schedule_mod.render_plist(hour=19, minute=5, repo=ROOT)
    p = plistlib.loads(data)
    assert p["Label"] == "com.jobcopilot.daily"
    assert p["StartCalendarInterval"] == {"Hour": 19, "Minute": 5}
    assert p["ProgramArguments"] == [str(ROOT / ".venv" / "bin" / "python"), "-m", "jobcopilot.daily"]
    assert p["WorkingDirectory"] == str(ROOT)
    for k in ("StandardOutPath", "StandardErrorPath"):
        assert Path(p[k]).is_absolute() and Path(p[k]).parent == ROOT / "data" / "logs"
    assert all(Path(a).is_absolute() for a in p["ProgramArguments"][:1])
    assert p["RunAtLoad"] is False
    # paths with XML specials survive; relative paths are refused
    odd = tmp_path / "a&b <c>"
    assert plistlib.loads(schedule_mod.render_plist(hour=1, minute=0, repo=odd))["WorkingDirectory"] == str(odd)
    with pytest.raises(ValueError):
        schedule_mod.render_plist(hour=1, minute=0, repo=ROOT, python=Path("venv/bin/python"))


def test_plist_time_comes_from_schedule_config(schedule_mod, monkeypatch, tmp_path):
    cfg = tmp_path / "cfg"; cfg.mkdir()
    (cfg / "schedule.yaml").write_text('timezone: Asia/Kolkata\ndaily_run: "19:00"\n')
    monkeypatch.setenv("JOBCOPILOT_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        p = plistlib.loads(schedule_mod.config_plist(repo=ROOT))
        assert p["StartCalendarInterval"] == {"Hour": 13, "Minute": 30}     # 19:00 IST = 13:30 UTC
        assert schedule_mod.local_run_time("07:45", None) == (7, 45)
    finally:
        monkeypatch.undo()
        time.tzset()
    with pytest.raises(ValueError):
        schedule_mod.parse_hhmm("25:00")


def test_next_run(schedule_mod):
    import datetime as dt
    now = dt.datetime(2026, 11, 12, 20, 0)
    assert schedule_mod.next_run(19, 0, now) == dt.datetime(2026, 11, 13, 19, 0)
    assert schedule_mod.next_run(21, 15, now) == dt.datetime(2026, 11, 12, 21, 15)
