"""Phase 2 persistence + queue: migration v3, evaluations, statuses/history,
the daily queue, all matches, the excluded view and the tracker.

eligibility / fit are faked here (workstreams E and F own the real ones)."""
from __future__ import annotations

import sqlite3
import sys
import time
import types
from types import SimpleNamespace

import pytest

import jobcopilot
from jobcopilot import ingest as ingest_mod
from jobcopilot import migrations, store
from jobcopilot import queue as jq
from jobcopilot.filters import FilterConfig, FilterResult
from jobcopilot.models import Job

DAY = 86400.0


def make_job(ext: str, company: str = "Acme", title: str = "Software Engineer", **kw) -> Job:
    base = dict(source="greenhouse", company=company, company_sector="tech", external_id=ext,
                title=title, location_text=kw.pop("location_text", "Bengaluru"),
                workplace_type="onsite", country_bucket="IN",
                url=f"https://example.test/{company}/{ext}", description_text="Python, SQL")
    base.update(kw)
    return Job(**base)


def seed(ext: str, *, company: str = "Acme", lane: str = "main", fit: float | None = 50.0,
         reasons: list | None = None, status: str | None = None, yoe_min: int | None = None,
         **kw) -> str:
    job = make_job(ext, company=company, **kw)
    r = FilterResult(job=job, package_estimated=True, country_uncertain=False)
    r.evaluation = {"lane": lane, "excluded": int(lane == "excluded"),
                    "exclusion_reasons": reasons or [], "flags": [], "min_yoe_required": yoe_min}
    store.upsert_jobs([r])
    if fit is not None:
        store.set_fit(job.key, {"fit_score_v2": fit, "fit_components": {"components": {"skills": fit},
                                                                        "weights": {"skills": 1.0}},
                                "matched_skills": ["python"], "missing_skills": ["go"],
                                "fit_why": f"fit {fit}"})
    if status:
        store.set_status(job.key, status)
    return job.key


@pytest.fixture
def cfg():
    return {"queue": {"daily_target_min": 3, "daily_target_max": 5, "max_per_company": 2,
                      "extended_top_n": 2, "near_miss_max": 5},
            "yoe": {"max_required": 2}, "companies": {"include_extended": True}}


# --- migration v3 --------------------------------------------------------------

def test_v3_upgrades_a_legacy_v2_db_and_keeps_rows(tmp_path):
    db = tmp_path / "legacy.db"
    assert migrations.migrate(db, steps=migrations.MIGRATIONS[:2]) == 2
    con = sqlite3.connect(db)
    con.execute("INSERT INTO jobs(key, source, company, title, status, notes) "
                "VALUES ('k', 'greenhouse', 'Acme', 'SWE', 'applied', 'my note')")
    con.execute("INSERT INTO companies(name, normalized_name, ats_evidence) VALUES ('Acme', 'acme', 'probe')")
    con.commit(); con.close()

    assert store.init_db(db) == migrations.latest_version()
    assert store.init_db(db) == migrations.latest_version()   # idempotent
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    assert {"lane", "excluded", "exclusion_reasons", "flags", "yoe_max", "yoe_evidence",
            "fresher_friendly", "india_eligible", "salary_annual_inr", "fit_score_v2",
            "fit_components", "matched_skills", "missing_skills", "fit_why", "scored_at",
            "submitted_at", "submit_source", "submit_proof_path", "status_changed_at"} <= cols
    ccols = {r[1] for r in con.execute("PRAGMA table_info(companies)")}
    assert {"probed_at", "probe_job_count", "probe_india_count"} <= ccols
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "queue_items", "status_history"} <= tables
    assert con.execute("SELECT status, notes, excluded FROM jobs").fetchone() == ("submitted", "my note", 0)   # v1 name renamed by migration 8
    assert con.execute("SELECT ats_evidence FROM companies").fetchone() == ("probe",)


# --- evaluations + statuses -----------------------------------------------------

def test_upsert_stores_evaluation_and_keeps_user_state():
    key = seed("1", lane="excluded", reasons=[{"rule": "yoe", "detail": "3+ years", "excerpt": "3+ yrs"}])
    store.set_status(key, "interested", notes="ping recruiter")
    job = store.get_job(key)
    assert job["excluded"] == 1 and job["lane"] == "excluded"
    assert job["exclusion_reasons"][0]["excerpt"] == "3+ yrs"
    seed("1", lane="main", fit=None)          # re-ingest: decision changes, user state stays
    job = store.get_job(key)
    assert job["lane"] == "main" and job["excluded"] == 0
    assert job["status"] == "interested" and job["notes"] == "ping recruiter"
    with pytest.raises(ValueError):
        store.set_evaluation(key, {"bogus": 1})


def test_status_history_submit_fields_and_undo():
    key = seed("1")
    t0 = 1_000_000.0
    assert store.set_status(key, "ready_to_submit", at=t0)
    assert store.set_status(key, "submitted", source="detected", proof_path="/tmp/p.png",
                            note="confirmation page", at=t0 + 60)
    job = store.get_job(key)
    assert job["submitted_at"] == t0 + 60 and job["submit_source"] == "detected"
    assert job["submit_proof_path"] == "/tmp/p.png"
    assert store.set_status(key, "interview", at=t0 + 120)
    assert store.get_job(key)["submitted_at"] == t0 + 60      # later stages keep the submit stamp
    # undo back to ready: history keeps everything, submit stamp cleared
    assert store.set_status(key, "ready_to_submit", at=t0 + 180)
    job = store.get_job(key)
    assert job["submitted_at"] is None and job["submit_source"] is None
    hist = store.status_history(key)
    assert [h["status"] for h in hist] == ["ready_to_submit", "submitted", "interview", "ready_to_submit"]
    assert hist[1]["source"] == "detected" and hist[1]["note"] == "confirmation page"
    assert store.set_status("nope", "found") is False
    with pytest.raises(ValueError):
        store.set_status(key, "found", source="robot")


def test_legacy_statuses_map_to_tracker_columns():
    assert store.canonical_status("new") == "found"
    assert store.canonical_status("ready") == "ready_to_submit"
    assert store.canonical_status("applied") == "submitted"
    assert store.is_submitted_or_later("applied") and store.is_submitted_or_later("offer")
    assert not store.is_submitted_or_later("ready")
    key = seed("1")
    store.set_status(key, "applied")
    assert store.get_job(key)["submitted_at"] is not None


# --- daily queue -----------------------------------------------------------------

def test_queue_ranks_caps_per_company_and_never_pads(cfg):
    seed("a1", company="A", fit=90); seed("a2", company="A", fit=89); seed("a3", company="A", fit=88)
    seed("b1", company="B", fit=70)
    seed("x1", company="X", lane="excluded", fit=None,
         reasons=[{"rule": "geo", "detail": "US only", "excerpt": "US only"}])
    q = jq.build_daily_queue(day="2026-11-12", cfg=cfg)
    assert [c["key"] for c in q["main"]] == ["greenhouse:A:a1", "greenhouse:A:a2", "greenhouse:B:b1"]
    assert q["shortfall"] == 0
    assert q["main"][0]["why"] == "fit 90" and q["main"][0]["matched_skills"] == ["python"]
    assert any(c["key"] == "greenhouse:A:a3" and "company" in c["near_miss_reason"] for c in q["near_miss"])
    assert all(c["key"] != "greenhouse:X:x1" for c in q["main"] + q["near_miss"])   # hard exclusion
    cfg["queue"]["daily_target_min"] = 5
    q = jq.build_daily_queue(day="2026-11-12", cfg=cfg)
    assert q["counts"]["main"] == 3 and q["shortfall"] == 2       # reported, not padded


def test_queue_skips_acted_jobs_and_lists_soft_near_misses(cfg):
    seed("ok", fit=60)
    seed("done", fit=99, status="submitted")
    seed("meh", fit=98, status="not_interested")
    seed("old", fit=97, status="applied")
    seed("y3", lane="excluded", fit=None, yoe_min=3,
         reasons=[{"rule": "yoe", "detail": "3+ years", "excerpt": "3 years"}])
    seed("y6", lane="excluded", fit=None, yoe_min=6,
         reasons=[{"rule": "yoe", "detail": "6+ years", "excerpt": "6 years"}])
    q = jq.build_daily_queue(day="2026-11-12", cfg=cfg)
    assert [c["key"] for c in q["main"]] == ["greenhouse:Acme:ok"]
    near = {c["key"]: c for c in q["near_miss"]}
    assert "greenhouse:Acme:y3" in near and "greenhouse:Acme:y6" not in near
    assert "yoe" in near["greenhouse:Acme:y3"]["near_miss_reason"]


def test_extended_lane_is_separate_and_capped(cfg):
    seed("m", fit=50)
    for i in range(4):
        seed(f"e{i}", company=f"W{i}", lane="extended", fit=80 - i)
    q = jq.build_daily_queue(day="2026-11-12", cfg=cfg)
    assert [c["key"] for c in q["main"]] == ["greenhouse:Acme:m"]
    assert [c["key"] for c in q["extended"]] == ["greenhouse:W0:e0", "greenhouse:W1:e1"]
    cfg["companies"]["include_extended"] = False
    assert jq.build_daily_queue(day="2026-11-12", cfg=cfg)["extended"] == []


def test_queue_is_idempotent_and_keeps_item_status(cfg):
    a = seed("a", fit=60); b = seed("b", company="B", fit=50)
    jq.build_daily_queue(day="2026-11-12", cfg=cfg)
    with store.connect() as conn:
        conn.execute("UPDATE queue_items SET status = 'seen' WHERE job_key = ?", (a,))
    store.set_fit(b, {"fit_score_v2": 95})
    jq.build_daily_queue(day="2026-11-12", cfg=cfg)
    with store.connect() as conn:
        rows = {r["job_key"]: dict(r) for r in conn.execute("SELECT * FROM queue_items")}
    assert len(rows) == 2 and rows[b]["rank"] == 1 and rows[a]["rank"] == 2
    assert rows[a]["status"] == "seen"
    listed = jq.list_queue()
    assert listed["day"] == "2026-11-12" and [i["key"] for i in listed["items"]] == [b, a]
    store.set_status(b, "submitted")             # submitted today -> gone from today's best
    assert [i["key"] for i in jq.list_queue(day="2026-11-12")["items"]] == [a]


# --- matches / excluded / fit ----------------------------------------------------

def test_matches_paginates_filters_and_hides_submitted():
    for i in range(7):
        seed(f"m{i}", company=f"C{i}", fit=50 + i, posted_age_days=i,
             location_text="Pune" if i == 0 else "Bengaluru")
    seed("sub", fit=99, status="oa")
    seed("ext", company="W", lane="extended", fit=80)
    seed("exc", lane="excluded", fit=None, reasons=[{"rule": "tier", "detail": "unknown"}])
    page = jq.list_matches(limit=3)
    assert page["total"] == 7 and [i["fit_score"] for i in page["items"]] == [56.0, 55.0, 54.0]
    assert [i["fit_score"] for i in jq.list_matches(limit=3, offset=6)["items"]] == [50.0]
    assert jq.list_matches(sort="age", limit=1)["items"][0]["key"] == "greenhouse:C0:m0"
    assert jq.list_matches(location="pune")["total"] == 1
    assert jq.list_matches(q="c3")["total"] == 1
    assert jq.list_matches(source="greenhouse")["total"] == 7
    assert jq.list_matches(source="himalayas")["total"] == 0
    assert jq.list_matches(lane="extended")["total"] == 1
    assert jq.list_matches(lane="all")["total"] == 8
    assert jq.list_matches(q="100%_")["total"] == 0          # LIKE wildcards are literal


def test_excluded_view_lists_reasons_and_filters_by_rule():
    seed("y", lane="excluded", fit=None, reasons=[{"rule": "yoe", "detail": "5+", "excerpt": "5 years"}])
    seed("g", lane="excluded", fit=None,
         reasons=[{"rule": "geo", "detail": "US only", "excerpt": "US"}, {"rule": "yoe", "detail": "4+"}])
    seed("ok", fit=50)
    out = jq.list_excluded()
    assert out["total"] == 2 and out["by_rule"] == {"yoe": 2, "geo": 1}
    only_geo = jq.list_excluded(rule="geo")
    assert [i["key"] for i in only_geo["items"]] == ["greenhouse:Acme:g"]
    assert only_geo["items"][0]["reasons"][0]["excerpt"] == "US"


def test_job_fit_components():
    key = seed("1", fit=72.5)
    fit = jq.job_fit(key)
    assert fit["fit_score"] == 72.5 and fit["components"] == {"skills": 72.5}
    assert fit["weights"] == {"skills": 1.0} and fit["missing_skills"] == ["go"]
    assert jq.job_fit("nope") is None


# --- tracker -------------------------------------------------------------------

def test_tracker_columns_ready_reminders_and_followups():
    now = time.time()
    r1 = seed("r1"); r2 = seed("r2", expires_at="2000-01-01"); s1 = seed("s1"); s2 = seed("s2")
    seed("untouched")
    store.set_status(r1, "ready_to_submit", at=now - 3 * DAY)
    store.set_status(r2, "ready", at=now - 1 * DAY)              # legacy value, same column
    store.set_status(s1, "submitted", at=now - 8 * DAY)
    store.set_status(s2, "submitted", at=now - 2 * DAY)
    t = jq.tracker(now=now)
    cols = {c["status"]: c for c in t["columns"]}
    assert list(cols) == list(store.TRACKER_STATUSES)
    assert cols["found"]["count"] == 0                           # untouched 'new' isn't tracked
    assert {i["key"] for i in cols["ready_to_submit"]["items"]} == {r1, r2}
    assert cols["submitted"]["count"] == 2
    assert cols["submitted"]["items"][0]["dates"]["submitted"]
    ready = {i["key"]: i for i in t["ready_to_submit"]}
    assert ready[r1]["days_waiting"] == 3 and ready[r1]["reminder_due"] is True
    assert ready[r2]["reminder_due"] is False and ready[r2]["posting_expired"] is True
    assert ready[r2]["warning"] == "posting expired"
    assert [i["key"] for i in t["followups_due"]] == [s1]
    assert t["followups_due"][0]["days_since_submit"] == 8


def _age_listing(key: str, days: float) -> None:
    """Pretend every listing of `key` was last seen `days` ago."""
    at = time.time() - days * DAY
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET last_seen = ? WHERE key = ?", (at, key))
        conn.execute("UPDATE job_sources SET last_seen = ? WHERE job_key = ?", (at, key))


def test_ready_posting_gone_when_its_source_ran_without_it():
    key = seed("r")
    store.set_status(key, "ready_to_submit")
    _age_listing(key, 10)
    store.set_source_state("greenhouse:acme", last_ok=time.time())   # Acme's board ran fine today
    assert store.retire_unlisted(3) == {"gone": 1, "newly_gone": 1, "relisted": 0}
    item = jq.ready_to_submit()[0]
    assert item["posting_gone"] is True and item["warning"] == "posting no longer listed"
    card = {c["status"]: c for c in jq.tracker()["columns"]}["ready_to_submit"]["items"][0]
    assert card["key"] == key and card["warning"] == "posting no longer listed"


def test_posting_not_gone_when_its_source_failed_despite_a_recent_ingest():
    """P7: the global last_ingest says nothing about THIS job's source."""
    key = seed("r")
    store.set_status(key, "ready_to_submit")
    _age_listing(key, 10)
    store.set_meta("last_ingest", str(time.time()))                       # other sources ran
    store.set_source_state("greenhouse:acme", last_ok=time.time() - 9 * DAY,  # Acme's board broke
                           last_error="error: 500")
    assert store.retire_unlisted(3)["gone"] == 0
    item = jq.ready_to_submit()[0]
    assert item["posting_gone"] is False and item["warning"] is None


def test_gone_postings_leave_queue_and_matches_but_not_the_tracker(cfg):
    """P1: 4 of a day's 25 'fresh' main picks had not been listed for months."""
    live = seed("live", fit=60.0)
    old = seed("old", fit=90.0, posted_at="2026-06-01T00:00:00+00:00", posted_age_days=1,
               freshness="fresh")
    ready = seed("ready", fit=80.0)
    store.set_status(ready, "ready_to_submit")
    for k in (old, ready):
        _age_listing(k, 120)
    # no source_state at all (fixture runs): the board listing `live` today is the evidence
    assert store.retire_unlisted(3)["gone"] == 2
    q = jq.build_daily_queue(day="2026-11-12", cfg=cfg)
    assert [c["key"] for c in q["main"]] == [live]
    assert [i["key"] for i in jq.list_queue(day="2026-11-12")["items"]] == [live]
    assert [i["key"] for i in jq.list_matches()["items"]] == [live]
    assert jq.ready_to_submit()[0]["warning"] == "posting no longer listed"
    # listed again -> live again
    seed("old", fit=90.0)
    assert store.get_job(old)["gone_at"] is None
    assert store.retire_unlisted(3)["gone"] == 1


def test_gone_needs_every_source_to_have_run_without_it():
    from jobcopilot.dedupe import occurrence
    job = make_job("m1")
    board = make_job("b1", source="remoteok")
    job.sources = [occurrence(job), occurrence(board)]
    store.upsert_jobs([FilterResult(job=job, package_estimated=True, country_uncertain=False)])
    _age_listing(job.key, 10)
    store.set_source_state("greenhouse:acme", last_ok=time.time())
    store.set_source_state("remoteok", last_ok=time.time() - 8 * DAY)   # board down since
    assert store.retire_unlisted(3)["gone"] == 0
    store.set_source_state("remoteok", last_ok=time.time())
    assert store.retire_unlisted(3)["gone"] == 1


def test_age_and_freshness_are_computed_at_read_time():
    now = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 160 * DAY))
    a = seed("a", posted_at=iso, posted_age_days=21, freshness="fresh")       # stale stored values
    w = seed("w", source="workday", title="Data Engineer", posted_at="Posted 3 Days Ago", posted_age_days=3, freshness="fresh")
    n = seed("n", title="Backend Engineer")                                  # no posted date
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET last_seen = ? WHERE key = ?", (now - 10 * DAY, w))
        conn.execute("UPDATE jobs SET first_seen = ? WHERE key = ?", (now - 40 * DAY, n))
    cards = {c["key"]: c for c in jq.list_matches(sort="age")["items"]}
    assert cards[a]["age_days"] == 160 and cards[a]["freshness"] == "aging"
    assert cards[w]["age_days"] == 13 and cards[w]["freshness"] == "fresh"
    assert cards[n]["age_days"] == 40 and cards[n]["freshness"] == "recent"
    assert [c["key"] for c in jq.list_matches(sort="age")["items"]] == [w, n, a]


def test_system_status_never_regresses_a_submission():
    key = seed("s")
    store.set_status(key, "submitted")
    assert store.set_status(key, "ready", source="system") is True
    assert store.get_job(key)["status"] == "submitted"
    assert [h["status"] for h in store.status_history(key)] == ["submitted"]
    store.set_status(key, "ready_to_submit")                     # the user may undo
    assert store.get_job(key)["status"] == "ready_to_submit"


def test_v4_backfills_tracker_dates(tmp_path):
    db = tmp_path / "v3.db"
    assert migrations.migrate(db, steps=migrations.MIGRATIONS[:3]) == 3
    con = sqlite3.connect(db)
    for k, st in (("a", "applied"), ("s", "submitted"), ("i", "interested"), ("r", "ready"), ("n", "new")):
        con.execute("INSERT INTO jobs(key, source, company, title, status, first_seen, last_seen) "
                    "VALUES (?, 'greenhouse', 'Acme', 'SWE', ?, 1000.0, 9000.0)", (k, st))
    con.execute("INSERT INTO status_history(job_key, status, at, source) VALUES ('r', 'ready', 5000.0, 'user')")
    con.commit(); con.close()
    assert store.init_db(db) == migrations.latest_version()
    rows = {r["key"]: r for r in (store.get_job(k, db_path=db) for k in "asirn")}
    assert rows["n"]["status_changed_at"] is None
    assert rows["i"]["status_changed_at"] == rows["a"]["status_changed_at"] == 1000.0
    assert rows["r"]["status_changed_at"] == 5000.0                          # history beats first_seen
    assert rows["a"]["submitted_at"] == rows["s"]["submitted_at"] == 1000.0
    assert rows["i"]["submitted_at"] is None
    hist = store.status_history("a", db_path=db)
    assert hist == [{"status": "submitted", "at": 1000.0, "source": "system",     # renamed by migration 8
                     "note": "submission date backfilled by migration v4"}]
    assert store.status_history("i", db_path=db) == []


def test_ready_since_falls_back_to_first_seen_not_last_seen():
    key = seed("r")
    store.set_status(key, "ready_to_submit")
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET status_changed_at = NULL, first_seen = 1000.0, last_seen = 9e9 "
                     "WHERE key = ?", (key,))
        conn.execute("DELETE FROM status_history WHERE job_key = ?", (key,))
    assert jq.ready_to_submit()[0]["ready_since"] == 1000.0


# --- scoring / evaluation catch-up + pipeline ------------------------------------

@pytest.fixture
def fake_fit(monkeypatch):
    mod = types.ModuleType("jobcopilot.fit")
    calls = []

    def score(job, *, profile, cfg=None, company=None, decision=None):
        calls.append(job["key"])
        assert decision["lane"] == "main" and "yoe_min" in decision
        return SimpleNamespace(score=40.0 + len(job["title"]), components={"skills": 50.0},
                               weights={"skills": 1.0}, matched_skills=["python"],
                               missing_skills=[], why="ok", flags=["semantic_skipped"])
    mod.score = score
    mod.load_profile = lambda facts_path=None: object()
    monkeypatch.setitem(sys.modules, "jobcopilot.fit", mod)
    monkeypatch.setattr(jobcopilot, "fit", mod, raising=False)
    return calls


@pytest.fixture
def fake_eligibility(monkeypatch):
    mod = types.ModuleType("jobcopilot.eligibility")

    def evaluate(job, *, company=None, cfg=None):
        get = job.get if isinstance(job, dict) else (lambda k, d=None: getattr(job, k, d))
        if "Senior" in (get("title") or ""):
            return SimpleNamespace(lane="excluded", reasons=[
                {"rule": "yoe", "detail": "senior", "excerpt": get("title")},
                {"rule": "flag:no_salary", "detail": "no pay", "excerpt": None}],
                yoe_min=5, yoe_max=None, yoe_is_preferred=False, yoe_evidence="5+ years",
                fresher_friendly=False, india_eligible=True, salary_ok=None, salary_annual_inr=None,
                role_family=None, seniority_high=True)
        lane = "extended" if (company or {}).get("tier") == "watchlist" else "main"
        return SimpleNamespace(lane=lane, reasons=[], yoe_min=1, yoe_max=2, yoe_is_preferred=False,
                               yoe_evidence="1-2 years", fresher_friendly=True, india_eligible=True,
                               salary_ok=None, salary_annual_inr=None, role_family=None,
                               seniority_high=False)
    mod.evaluate = evaluate
    monkeypatch.setitem(sys.modules, "jobcopilot.eligibility", mod)
    monkeypatch.setattr(jobcopilot, "eligibility", mod, raising=False)
    return mod


def test_score_pending_only_scores_eligible_unscored(fake_fit):
    a = seed("a", fit=None)
    seed("x", lane="excluded", fit=None, reasons=[{"rule": "geo"}])
    out = jq.score_pending()
    assert out == {"scored": 1, "errors": 0} and fake_fit == [a]
    fit = jq.job_fit(a)
    assert fit["fit_score"] == 57.0 and fit["fit_flags"] == ["semantic_skipped"]
    assert jq.score_pending() == {"scored": 0, "errors": 0}     # nothing stale now


def test_evaluate_pending_decides_legacy_rows(fake_eligibility):
    job = make_job("old", title="Senior Engineer")
    store.upsert_jobs([FilterResult(job=job, package_estimated=True, country_uncertain=False)])
    assert store.get_job(job.key)["lane"] is None
    out = jq.evaluate_pending()
    assert out["evaluated"] == 1 and out["excluded"] == 1
    j = store.get_job(job.key)
    assert j["lane"] == "excluded" and j["yoe_evidence"] == "5+ years"
    assert [f["rule"] for f in j["flags"]] == ["flag:no_salary"]


def test_evaluate_pending_reevaluates_every_row_when_the_config_changes(fake_eligibility):
    a = seed("a", title="Senior Engineer")           # stored lane main, but the fake says excluded
    b = seed("b")
    gone = seed("g", title="Senior Engineer")
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET gone_at = 1 WHERE key = ?", (gone,))
    cfg_a, cfg_b = {"salary_floors": {"INR": 1}}, {"salary_floors": {"INR": 2}}
    out = jq.evaluate_pending(cfg=cfg_a)             # rows from before v4 carry no hash
    assert out["evaluated"] == 2 and store.get_job(a)["lane"] == "excluded"
    assert store.get_job(gone)["lane"] == "main"    # gone rows are left alone
    assert jq.evaluate_pending(cfg=cfg_a)["evaluated"] == 0          # same config: nothing to do
    assert jq.evaluate_pending(cfg=cfg_b)["evaluated"] == 2          # changed: ALL rows, not lane IS NULL
    assert store.get_job(b)["eval_cfg_hash"] == store.config_hash(cfg_b)


def test_ingest_and_catch_up_agree_on_the_config_hash(fake_eligibility, fake_fit):
    """The nightly ingest stamps the same hash the catch-up computes, so an
    unchanged filters.yaml never triggers a full re-evaluation."""
    run = ingest_mod.IngestRun(roster=[], raw_count=1, merged=[make_job("1")], results=[])
    run.evaluated, run.evaluation = ingest_mod.evaluate_jobs(run.merged, ingest_mod.default_filter_cfg())
    ingest_mod.persist(run, enrich_workday=False)
    assert store.get_job("greenhouse:Acme:1")["eval_cfg_hash"]
    assert jq.evaluate_pending()["evaluated"] == 0


def test_off_target_engineering_titles_are_stored_as_role_exclusions(fake_eligibility, fake_fit):
    from jobcopilot.filters import passes_role
    roles = ["software engineer"]
    jobs = [make_job("1"), make_job("2", title="Mechanical Design Engineer"),
            make_job("3", title="Account Executive")]
    assert passes_role(jobs[1].title, roles) is None and passes_role(jobs[2].title, roles) is None
    run = ingest_mod.IngestRun(roster=[], raw_count=3, merged=jobs, results=[])
    run.evaluated, run.evaluation = ingest_mod.evaluate_jobs(jobs, FilterConfig(target_roles=roles))
    assert run.evaluation["off_target"] == 2 and run.evaluation["off_target_stored"] == 1
    assert run.evaluation["by_rule"] == {"role": 1}
    assert ingest_mod.persist(run, enrich_workday=False)["inserted"] == 2
    eng = store.get_job("greenhouse:Acme:2")
    assert eng["excluded"] == 1 and eng["lane"] == "excluded"
    assert [r["rule"] for r in eng["exclusion_reasons"]] == ["role"]
    assert eng["fit_score_v2"] is None
    assert store.get_job("greenhouse:Acme:3") is None            # sales: counted, never stored
    assert jq.list_excluded(rule="role")["total"] == 1


def test_pipeline_stores_excluded_jobs_with_reasons_and_scores_the_rest(fake_eligibility, fake_fit):
    jobs = [make_job("1", title="Software Engineer"), make_job("2", title="Senior Software Engineer"),
            make_job("3", title="Sales Manager")]
    run = ingest_mod.IngestRun(roster=[], raw_count=3, merged=jobs, results=[])
    cfg = FilterConfig(target_roles=["software engineer"])
    run.evaluated, run.evaluation = ingest_mod.evaluate_jobs(jobs, cfg)
    assert run.evaluation["off_target"] == 1 and run.evaluation["excluded"] == 1
    assert run.evaluation["by_rule"] == {"yoe": 1}
    persisted = ingest_mod.persist(run, enrich_workday=False)
    assert persisted["inserted"] == 2 and persisted["fit"]["scored"] == 1
    sr = store.get_job("greenhouse:Acme:2")
    assert sr["excluded"] == 1 and sr["exclusion_reasons"][0]["rule"] == "yoe"
    assert sr["fit_score_v2"] is None
    assert store.get_job("greenhouse:Acme:1")["fit_score_v2"] is not None
    assert store.get_job("greenhouse:Acme:3") is None           # off-target title: counted, not stored


def test_eligibility_error_falls_back_per_job(monkeypatch, fake_eligibility):
    def boom(job, *, company=None, cfg=None):
        raise RuntimeError("bug")
    monkeypatch.setattr(fake_eligibility, "evaluate", boom)
    out, stats = ingest_mod.evaluate_jobs([make_job("1")], FilterConfig(target_roles=["software engineer"]))
    assert stats["errors"] == 1 and len(out) == 1
    assert out[0].evaluation["lane"] == "excluded"               # no company tier -> v1 fallback excludes
    assert out[0].evaluation["exclusion_reasons"][0]["rule"] == "tier"


def test_same_listing_id_at_two_employers_stays_two_rows():
    """job_sources is UNIQUE(source, external_id): a per-tenant id reused by another
    company must not merge the two jobs (that silently lost excluded rows, D8)."""
    a = seed("777", company="Okta", lane="excluded", fit=None, reasons=[{"rule": "geo"}])
    b = seed("777", company="Stripe", lane="main")
    assert a != b and store.get_job(a)["lane"] == "excluded" and store.get_job(b)["lane"] == "main"
    assert [s["external_id"] for s in store.job_sources(a)] == ["777"]
    assert [s["external_id"] for s in store.job_sources(b)] == ["stripe/777"]
    # re-ingest: each row is found again and updated in place, nothing is stolen
    seed("777", company="Okta", lane="excluded", fit=None, reasons=[{"rule": "geo"}])
    seed("777", company="Stripe", lane="main")
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 2
    assert jq.list_excluded()["total"] == 1
