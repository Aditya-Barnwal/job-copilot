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

    assert store.init_db(db) == 3 == migrations.latest_version()
    assert store.init_db(db) == 3            # idempotent
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
    assert con.execute("SELECT status, notes, excluded FROM jobs").fetchone() == ("applied", "my note", 0)
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


def test_ready_posting_gone_when_ingest_stopped_seeing_it():
    key = seed("r")
    store.set_status(key, "ready_to_submit")
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET last_seen = ? WHERE key = ?", (time.time() - 10 * DAY, key))
    store.set_meta("last_ingest", str(time.time()))
    item = jq.ready_to_submit()[0]
    assert item["posting_gone"] is True and item["warning"] == "posting no longer listed"


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
