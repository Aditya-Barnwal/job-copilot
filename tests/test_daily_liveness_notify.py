"""daily.py: the liveness step (before tailoring, refilled queue reported), the
tailor step's summary (results is {job_key: status}; a model stop is an
error on the Runs screen), and the macOS notification (osascript mocked).
No network, no LLM, no real notification."""
from __future__ import annotations

import pytest

from jobcopilot import daily, liveness, store
from jobcopilot import ingest as ingest_mod


@pytest.fixture(autouse=True)
def _no_real_profile(tmp_path, monkeypatch):
    empty = tmp_path / "empty-profile"
    empty.mkdir()
    monkeypatch.setenv("JOBCOPILOT_PROFILE_DIR", str(empty))


def _summary():
    return {"ingested_raw": 1, "merged": 1, "passed": 1, "eligible": 1, "excluded": 0,
            "inserted": 1, "updated": 0, "skipped": [],
            "sources": {"a": {"status": "ok", "items": 1, "kind": "board", "name": "a",
                              "connector": "a", "reason": ""}}}


# --------------------------------------------------------------------------- tailor summary

def test_tailor_summary_counts_statuses_from_the_results_dict():
    out = {"skipped": False, "day": "2026-11-19", "tailored": 3,
           "results": {"k1": "ok", "k2": "ok", "k3": "error"}}
    s = daily.summarize_tailor(out)
    assert s["by_status"] == {"ok": 2, "error": 1} and s["tailored"] == 3 and "stopped" not in s
    assert daily.summarize_tailor({"results": [{"status": "ok"}]})["by_status"] == {"ok": 1}
    assert daily.summarize_tailor({"skipped": True, "results": {}})["by_status"] == {}


def test_tailor_stopped_by_the_model_shows_as_an_error_step(monkeypatch):
    from jobcopilot import tailor_v2
    monkeypatch.setattr(ingest_mod, "run_pipeline", lambda **kw: _summary())
    monkeypatch.setattr(tailor_v2, "tailor_queue", lambda **kw: {
        "skipped": False, "day": "2026-11-19", "tailored": 1, "results": {"k1": "ok"},
        "stopped": "model unavailable", "reason": "ConnectionError"})
    res = daily.run(skip_discovery=True)
    by = {s["name"]: s for s in res["steps"]}
    t = by["tailor"]
    assert t["status"] == "error" and t["error"] == "tailor: stopped, model unavailable (ConnectionError)"
    assert t["result"]["by_status"] == {"ok": 1} and t["result"]["stopped"] == "model unavailable"
    assert res["status"] == "partial"
    row = store.get_run(res["run_id"])
    assert row["status"] == "partial" and "tailor: stopped, model unavailable" in row["error"]


def test_tailor_clean_run_stays_ok(monkeypatch):
    from jobcopilot import tailor_v2
    monkeypatch.setattr(ingest_mod, "run_pipeline", lambda **kw: _summary())
    monkeypatch.setattr(tailor_v2, "tailor_queue", lambda **kw: {
        "skipped": False, "day": "2026-11-19", "tailored": 2, "results": {"a": "ok", "b": "ok"}})
    res = daily.run(skip_discovery=True)
    t = {s["name"]: s for s in res["steps"]}["tailor"]
    assert t["status"] == "ok" and t["result"]["by_status"] == {"ok": 2}


# --------------------------------------------------------------------------- liveness step

def test_liveness_runs_after_the_queue_and_before_tailoring(monkeypatch):
    from jobcopilot import tailor_v2
    order = []
    monkeypatch.setattr(ingest_mod, "run_pipeline", lambda **kw: _summary())
    refilled = {"day": "2026-11-19", "counts": {"main": 4, "extended": 0, "near_miss": 0},
                "shortfall": 0, "target": {"min": 1, "max": 5}, "main": [], "extended": [], "near_miss": []}

    def fake_check_queue(day=None, db_path=None, **kw):
        order.append(("liveness", day))
        return {"day": day, "checked": 5, "gone": 1, "unknown": 0, "refills": 1, "gone_keys": ["dead"],
                "queue": refilled}
    monkeypatch.setattr(liveness, "check_queue", fake_check_queue)
    monkeypatch.setattr(tailor_v2, "tailor_queue", lambda **kw: order.append(("tailor", None)) or
                        {"skipped": True, "results": {}})
    res = daily.run(skip_discovery=True, day="2026-11-19")
    names = [s["name"] for s in res["steps"]]
    assert names.index("queue") < names.index("liveness") < names.index("tailor")
    assert order == [("liveness", "2026-11-19"), ("tailor", None)]
    lv = {s["name"]: s for s in res["steps"]}["liveness"]
    assert lv["status"] == "ok" and lv["result"]["gone"] == 1 and "queue" not in lv["result"]
    assert res["queue"] is refilled                         # the summary/notification see the refill


def test_liveness_is_skipped_in_fixture_mode(monkeypatch):
    monkeypatch.setattr(liveness, "check_queue", lambda **kw: pytest.fail("network in fixture mode"))
    monkeypatch.setattr(ingest_mod, "run_pipeline", lambda **kw: _summary())
    res = daily.run(no_network=True, skip_discovery=True)
    lv = {s["name"]: s for s in res["steps"]}["liveness"]
    assert lv["status"] == "skipped"


# --------------------------------------------------------------------------- notification

def _result(n):
    return {"run_id": 1, "status": "ok", "all_sources_failed": False, "steps": [],
            "queue": {"counts": {"main": n, "extended": 2, "near_miss": 0}}}


def test_notification_text_and_osascript_call():
    calls = []
    ok = daily.notify_done(_result(7), runner=lambda argv, **kw: calls.append((argv, kw)), platform="darwin")
    assert ok and len(calls) == 1
    argv, kw = calls[0]
    assert argv[0].endswith("osascript") and argv[1] == "-e"
    assert argv[2] == ('display notification "Job-Copilot: 7 roles on today\'s list — open '
                       'http://127.0.0.1:8000" with title "Job-Copilot"')
    assert kw.get("check") is False and kw.get("timeout")
    assert daily.notification_text(_result(1)).startswith("Job-Copilot: 1 role on today's list")
    assert daily.notification_text({"queue": None}).startswith("Job-Copilot: 0 roles")


def test_notification_only_on_macos_and_when_enabled(monkeypatch):
    calls = []
    run = lambda argv, **kw: calls.append(argv)  # noqa: E731
    assert daily.notify_done(_result(3), runner=run, platform="linux") is False
    monkeypatch.setattr(daily, "schedule_config", lambda: {"notify": False})
    assert daily.notify_done(_result(3), runner=run, platform="darwin") is False
    assert calls == []


def test_notification_failure_is_ignored():
    def boom(argv, **kw):
        raise OSError("no osascript")
    assert daily.notify_done(_result(3), runner=boom, platform="darwin") is False


def test_scheduled_main_notifies_and_unscheduled_does_not(monkeypatch):
    monkeypatch.setattr(ingest_mod, "setup_logging", lambda _d: None)
    monkeypatch.setattr(daily, "scheduled_skip_reason", lambda **kw: None)
    seen = []
    monkeypatch.setattr(daily, "notify_done", lambda result, **kw: seen.append(result["queue"]))

    def fake_run(**kw):
        store.update_run(kw["run_id"], status="ok", finished=True)
        return {**_result(4), "run_id": kw["run_id"]}
    monkeypatch.setattr(daily, "run", fake_run)
    monkeypatch.setattr(daily, "print_summary", lambda r: None)
    assert daily.main(["--scheduled", "--skip-discovery"]) == 0
    assert daily.main(["--skip-discovery"]) == 0
    assert len(seen) == 1 and seen[0]["counts"]["main"] == 4
