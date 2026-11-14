"""Phase 4 orchestration (jobcopilot/autofill/service.py): fill plans, sessions,
hand-off, submission detection. Offline: a fake browser backend + the tmp DB from
conftest.py; one optional headless smoke test against a LOCAL fixture page."""
from __future__ import annotations

import copy
import json
import http.server
import logging
import threading
from pathlib import Path

import pytest

from jobcopilot import answers as A
from jobcopilot import demo, llm, migrations, store
from jobcopilot.autofill import service
from jobcopilot.autofill.plan import FieldAction, FillPlan, Outcome
from jobcopilot.filters import FilterResult
from jobcopilot.models import Job

EXAMPLE_BANK = A.load(A.example_answers_path())
SECRET_VALUES = ("alex@example.com", "555 0100", "Example")

GH_QUESTIONS = {
    "id": 123, "title": "Backend Engineer",
    "questions": [
        {"label": "First Name", "required": True,
         "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
        {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text", "values": []}]},
        {"label": "Resume/CV", "required": True,
         "fields": [{"name": "resume", "type": "input_file", "values": []},
                    {"name": "resume_text", "type": "textarea", "values": []}]},
        {"label": "Cover Letter", "required": False,
         "fields": [{"name": "cover_letter", "type": "input_file", "values": []},
                    {"name": "cover_letter_text", "type": "textarea", "values": []}]},
        {"label": "Are you legally authorized to work in India?", "required": True,
         "fields": [{"name": "question_1", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"label": "Why do you want to work at Acme?", "required": True,
         "fields": [{"name": "question_2", "type": "textarea", "values": []}]},
        {"label": "What is your favourite colour?", "required": False,
         "fields": [{"name": "question_3", "type": "input_text", "values": []}]},
        {"label": "I agree to the privacy policy", "required": True,
         "fields": [{"name": "question_4", "type": "multi_value_multi_select",
                     "values": [{"label": "I agree", "value": 1}]}]},
        {"label": "Tracking", "required": False, "fields": [{"name": "gh_src", "type": "input_hidden"}]},
    ],
    "location_questions": [],
    "compliance": [],
}


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    def _refuse(*_a, **_kw):
        raise AssertionError("tests must not call a real LLM")
    monkeypatch.setattr(llm, "complete", _refuse)
    monkeypatch.setattr(llm, "complete_json", _refuse)
    monkeypatch.setenv("JOBCOPILOT_PROFILE_DIR", str(tmp_path / "no-profile"))   # never the real profile
    monkeypatch.setattr(service, "adapter_for", lambda url, ats: None)
    monkeypatch.setattr(service, "_LIVE", {})


class FakeBackend:
    """Stands in for the browser. `states` = successive (url, text) of the fill tab;
    None (or running out) = the user closed the window."""

    def __init__(self, outcome=None, states=(), snap=None):
        self.outcome = outcome or Outcome("ready", "stopped before submit", 3, 1, ["Why us?"], "#submit_app")
        self.states = list(states)
        self.snap = snap
        self.calls: list[str] = []
        self.plans: list[FillPlan] = []

    def snapshot(self, url, ats, cfg, adapter=None):
        self.calls.append("snapshot")
        return copy.deepcopy(self.snap)

    def open_headed(self, cfg):
        self.calls.append("open_headed")
        return {"fake": True}

    def execute(self, handle, adapter, plan, ctx, cfg):
        self.calls.append("execute")
        self.plans.append(plan)
        return self.outcome() if callable(self.outcome) else self.outcome

    def handover(self, handle):
        self.calls.append("handover")
        return True

    def tab_state(self, handle):
        return self.states.pop(0) if self.states else None

    def screenshot(self, handle, path):
        Path(path).write_bytes(b"\x89PNG fake")
        return str(path)

    def wait(self, handle, seconds):
        pass

    def close(self, handle):
        self.calls.append("close")


def cfg(**over):
    c = service.settings()
    c["timeouts"] = dict(c["timeouts"], watch_poll_sec=0.01, watch_min=1)
    c.update(over)
    return c


def seed_job(url="https://job-boards.greenhouse.io/acme/jobs/123", external_id="123",
             company="Acme", status=None) -> str:
    job = Job(source="greenhouse", company=company, company_sector="tech", external_id=external_id,
              title="Backend Engineer", location_text="Bengaluru", workplace_type="onsite",
              country_bucket="IN", url=url, description_text="Python, Django")
    store.upsert_jobs([FilterResult(job=job, package_estimated=False, country_uncertain=False)])
    if status:
        store.set_status(job.key, status)
    return job.key


def approve_artifact(key: str, *, cover: bool = False) -> Path:
    aid, v = store.create_artifact(key)
    d = store.applications_dir() / "x" / f"v{v}"
    d.mkdir(parents=True, exist_ok=True)
    pdf = d / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake\n")
    fields = {"status": "approved", "pdf_path": str(pdf)}
    if cover:
        (d / "cover_letter.pdf").write_bytes(b"%PDF-1.4 fake\n")
        fields["cover_letter_path"] = str(d / "cover_letter.pdf")
    store.update_artifact(aid, **fields)
    store.link_job_artifact(key, aid)
    return pdf


@pytest.fixture
def gh_api(monkeypatch):
    seen = []

    def fake(board, job_id):
        seen.append((board, job_id))
        return copy.deepcopy(GH_QUESTIONS)
    monkeypatch.setattr(service, "fetch_greenhouse_questions", fake)
    return seen


def actions(plan: FillPlan) -> dict[str, FieldAction]:
    return {a.label: a for step in plan.steps for a in step}


# --------------------------------------------------------------------------- schema

def test_migration_v6_tables_and_column():
    assert migrations.latest_version() >= 6
    with store.connect() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"fill_plans", "autofill_sessions"} <= tables and "fill_status" in cols


# --------------------------------------------------------------------------- planning

def test_plan_from_greenhouse_questions_api(gh_api, monkeypatch, caplog):
    fake = FakeBackend()
    monkeypatch.setattr(service, "BACKEND", fake)
    key = seed_job()
    pdf = approve_artifact(key)
    caplog.set_level(logging.DEBUG)
    plan = service.plan_for_job(key, cfg=cfg(), bank=EXAMPLE_BANK)

    assert gh_api == [("acme", "123")] and fake.calls == []      # API only: no browser at all
    acts = actions(plan)
    assert acts["First Name"].value == "Alex" and acts["First Name"].source.startswith("answers.yaml:")
    assert acts["First Name"].locator == '[id="first_name"]' and not acts["First Name"].needs_user
    auth = acts["Are you legally authorized to work in India?"]
    assert auth.kind == "select" and auth.value in ("Yes", "No") and not auth.needs_user
    assert acts["Resume/CV"].kind == "upload" and acts["Resume/CV"].value == "resume"
    assert plan.files["resume"] == str(pdf.resolve())
    assert not any(a.locator.endswith('"resume_text"]') for a in acts.values())   # file field wins
    assert "Tracking" not in acts                                               # hidden input skipped
    for label in ("Why do you want to work at Acme?", "What is your favourite colour?",
                  "I agree to the privacy policy", "Cover Letter"):
        assert acts[label].value is None and acts[label].needs_user, label
    assert acts["I agree to the privacy policy"].source == "consent"
    unresolved = {u["label"] for u in plan.unresolved}
    assert {"Why do you want to work at Acme?", "What is your favourite colour?"} <= unresolved

    row = store.get_fill_plan(key)
    assert row["ats"] == "greenhouse" and row["unresolved_count"] == len(plan.unresolved)
    # stored: field metadata + approval state only, never a value or a file path (L#4)
    stored = FillPlan.from_dict(row["plan"])
    assert stored.files == {} and row["plan"]["file_kinds"] == ["resume"]
    assert all(a.value is None for a in stored.actions() if a.kind != "upload")
    by_label = {a["label"]: a for s in row["plan"]["steps"] for a in s}
    assert by_label["First Name"]["approved"] is True and by_label["First Name"]["source"].startswith("answers.yaml:")
    assert by_label["Why do you want to work at Acme?"]["approved"] is False
    assert by_label["Resume/CV"]["value"] == "resume"                  # the file KEY, not a path
    assert "Alex" not in json.dumps(row["plan"]) and str(pdf) not in json.dumps(row["plan"])
    assert store.get_job(key)["fill_status"] == "planned_needs_you"
    for secret in SECRET_VALUES:                                   # labels + sources only in logs
        assert secret not in caplog.text


def test_unresolved_when_no_approved_resume(gh_api):
    key = seed_job()
    plan = service.plan_for_job(key, cfg=cfg(), bank=EXAMPLE_BANK)
    assert plan.files == {}
    assert actions(plan)["Resume/CV"].value is None
    assert any(u["reason"].startswith("needs approved résumé") for u in plan.unresolved)
    # a pending (unapproved) artifact doesn't count either
    aid, _ = store.create_artifact(key)
    store.update_artifact(aid, status="ok", pdf_path=str(store.applications_dir() / "nope.pdf"))
    store.link_job_artifact(key, aid)
    assert service.plan_for_job(key, cfg=cfg(), bank=EXAMPLE_BANK).files == {}


def test_page_snapshot_path_and_gone(monkeypatch):
    snap = {"source": "page", "url": "https://jobs.lever.co/acme/1/apply", "steps": [[
        {"label": "Email", "name": "email", "type": "text", "required": True, "options": [],
         "hidden": False, "locator": '[name="email"]'},
        {"label": "beecatcher", "name": "beecatcher", "type": "text", "required": False, "options": [],
         "hidden": True, "locator": '[name="beecatcher"]'}]]}
    fake = FakeBackend(snap=snap)
    monkeypatch.setattr(service, "BACKEND", fake)
    key = seed_job(url="https://jobs.lever.co/acme/1/apply", external_id="L1")
    plan = service.plan_for_job(key, cfg=cfg(), bank=EXAMPLE_BANK)
    assert plan.ats == "lever" and fake.calls == ["snapshot"]
    assert [a.label for a in plan.actions()] == ["Email"]          # the hidden honeypot is never planned
    fake.snap = None                                               # 404 on re-check -> gone
    with pytest.raises(service.AutofillRefused) as e:
        service.plan_for_job(key, cfg=cfg(), bank=EXAMPLE_BANK)
    assert e.value.code == "gone" and store.get_job(key)["fill_status"] == "gone"


def test_denylisted_host_is_link_out_only(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(service, "BACKEND", fake)
    monkeypatch.setattr(service, "fetch_greenhouse_questions",
                        lambda *a: pytest.fail("denylisted job must not be fetched"))
    for url in ("https://www.linkedin.com/jobs/view/1", "https://in.indeed.com/viewjob?jk=1",
                "https://www.workatastartup.com/jobs/1", "https://account.ycombinator.com/x"):
        key = seed_job(url=url, external_id=url[-12:])
        for call in (lambda: service.plan_for_job(key, cfg=cfg()), lambda: service.launch_fill(key, cfg=cfg())):
            with pytest.raises(service.AutofillRefused) as e:
                call()
            assert e.value.code == "denylisted" and e.value.http == 400 and e.value.link_out == url
    assert fake.calls == [] and store.list_autofill_sessions() == []
    assert service.denied_host("javascript:alert(1)") == "invalid-url"


def test_enforce_policy_blanks_anything_not_approved():
    plan = FillPlan("k", "generic", "https://x.test/apply", steps=[[
        FieldAction("#a", "Email", "fill", "v", "answers.yaml:personal.email", 1.0, False),
        FieldAction("#b", "Salary", "fill", "v", "answers.yaml:x", 0.5, False),
        FieldAction("#c", "Notice", "fill", "v", "answers.yaml:x", 1.0, True),
        FieldAction("#d", "I consent to the processing of my data", "check", "true", "x", 1.0, False),
        FieldAction("#e", "Resume", "upload", "/etc/passwd", "x", 1.0, False),
        FieldAction("#f", "Cover letter", "upload", "cover_letter", "x", 1.0, False),
        FieldAction("#g", "Resume", "upload", "resume", "file:resume", 1.0, False),
    ]], files={"resume": "/tmp/approved.pdf"})
    service.enforce_policy(plan, {"min_confidence": 0.8})
    kept = {a.locator: a.value for a in plan.actions()}
    assert kept == {"#a": "v", "#b": None, "#c": None, "#d": None, "#e": None, "#f": None, "#g": "resume"}
    assert all(a.needs_user for a in plan.actions() if a.value is None)


# --------------------------------------------------------------------------- sessions

def _launch(monkeypatch, fake, key=None, **cfg_over):
    monkeypatch.setattr(service, "BACKEND", fake)
    key = key or seed_job()
    sid = service.launch_fill(key, cfg=cfg(**cfg_over), background=False)
    return key, sid


def test_fill_ready_sets_ready_to_submit_then_window_closed(gh_api, monkeypatch):
    fake = FakeBackend(states=[("https://job-boards.greenhouse.io/acme/jobs/123", "Apply")] * 3)
    key = seed_job()
    approve_artifact(key)
    _, sid = _launch(monkeypatch, fake, key)
    assert fake.calls == ["open_headed", "execute", "close"]
    plan = fake.plans[0]
    assert plan.files.get("resume") and actions(plan)["First Name"].value is None   # no bank in profile dir
    job = store.get_job(key)
    assert store.canonical_status(job["status"]) == "ready_to_submit" and job["fill_status"] == "ready"
    s = service.session_status(sid)
    assert s["status"] == "closed" and s["outcome"]["submit_selector"] == "#submit_app"
    assert s["finished_at"] and Path(s["screenshot_path"]).is_file()
    assert Path(s["screenshot_path"]).is_relative_to(store.applications_dir())
    assert store.status_history(key)[-1]["source"] == "system"


def test_status_never_regresses_from_submitted(gh_api, monkeypatch):
    key = seed_job()

    def user_submits_meanwhile():
        store.set_status(key, "submitted", source="user")
        return Outcome("ready", "stopped before submit")
    _launch(monkeypatch, FakeBackend(outcome=user_submits_meanwhile), key)
    assert store.get_job(key)["status"] == "submitted"
    with pytest.raises(service.AutofillRefused) as e:          # and no new fill for a submitted job
        service.launch_fill(key, cfg=cfg(), background=False)
    assert e.value.code == "submitted" and e.value.http == 409


def test_needs_you_outcome_lists_and_does_not_advance(gh_api, monkeypatch):
    key = seed_job(status="tailored")
    fake = FakeBackend(outcome=Outcome("needs_you", "CAPTCHA: your turn"),
                       states=[("https://job-boards.greenhouse.io/acme/jobs/123", "form")])
    seen: dict = {}
    orig = fake.tab_state

    def tab_state(handle):      # while the window is open, the session is listed as needs-you
        seen.setdefault("needs", [r["id"] for r in service.needs_you()])
        return orig(handle)
    fake.tab_state = tab_state
    _, sid = _launch(monkeypatch, fake, key)
    assert seen["needs"] == [sid]
    assert store.get_job(key)["status"] == "tailored" and store.get_job(key)["fill_status"] == "needs_you"
    assert service.session_status(sid)["status"] == "closed" and service.needs_you() == []


def test_confirmation_detected_marks_submitted_with_proof(gh_api, monkeypatch):
    form = ("https://job-boards.greenhouse.io/acme/jobs/123", "Apply for this job. Thank you for applying!")
    done = ("https://job-boards.greenhouse.io/acme/jobs/123/confirmation", "Application submitted")
    fake = FakeBackend(states=[form, form, form, done])
    key, sid = _launch(monkeypatch, fake)
    job = store.get_job(key)
    assert job["status"] == "submitted" and job["submit_source"] == "detected"
    assert Path(job["submit_proof_path"]).is_file() and "confirmation-" in job["submit_proof_path"]
    assert Path(job["submit_proof_path"]).is_relative_to(store.applications_dir())
    assert service.session_status(sid)["status"] == "submitted" and job["fill_status"] == "submitted"
    assert store.status_history(key)[-1]["source"] == "detected"


def test_marker_already_on_form_page_is_not_a_submission(gh_api, monkeypatch):
    stuck = ("https://jobs.example.test/thanks/apply", "Thank you for applying soon")
    key, sid = _launch(monkeypatch, FakeBackend(states=[stuck] * 4),
                       seed_job(url="https://jobs.example.test/thanks/apply", external_id="T1"))
    assert store.get_job(key)["status"] != "submitted"


def test_watch_times_out(gh_api, monkeypatch):
    form = ("https://job-boards.greenhouse.io/acme/jobs/123", "form")
    fake = FakeBackend(states=[form] * 10_000)
    key, sid = _launch(monkeypatch, fake, timeouts={"page_load_sec": 1, "fill_sec": 1,
                                                    "watch_min": 0.0005, "watch_poll_sec": 0.01})
    assert service.session_status(sid)["status"] == "timed_out" and "close" in fake.calls


def test_handover_runs_on_the_worker_thread(gh_api, monkeypatch):
    form = ("https://job-boards.greenhouse.io/acme/jobs/123", "form")
    gate = threading.Event()
    fake = FakeBackend(states=[form] * 10_000)
    orig = fake.tab_state

    def tab_state(h):
        gate.set()
        return None if "handover" in fake.calls else orig(h)
    fake.tab_state = tab_state
    monkeypatch.setattr(service, "BACKEND", fake)
    key = seed_job()
    sid = service.launch_fill(key, cfg=cfg())                       # background thread
    assert gate.wait(5)
    assert service.handover(sid) is True
    service._LIVE.get(sid) and service._LIVE[sid].thread.join(5)
    assert "handover" in fake.calls and service.session_status(sid)["status"] == "closed"
    assert service.handover(sid) is False                         # window gone


def test_one_session_at_a_time_and_dead_pid_reaped(gh_api, monkeypatch):
    monkeypatch.setattr(service, "BACKEND", FakeBackend())
    a, b = seed_job(), seed_job(url="https://job-boards.greenhouse.io/acme/jobs/456", external_id="456")
    sid, _ = store.claim_autofill_session(a)
    with pytest.raises(service.AutofillRefused) as e:
        service.launch_fill(b, cfg=cfg(), background=False)
    assert e.value.code == "busy" and e.value.http == 409 and e.value.session_id == sid
    store.update_autofill_session(sid, pid=2 ** 22 + 12345)       # its server process is gone
    assert service.session_status(sid)["status"] == "abandoned"
    service.launch_fill(b, cfg=cfg(), background=False)


def test_demo_mode_refuses(monkeypatch):
    monkeypatch.setattr(demo, "enabled", lambda: True)
    with pytest.raises(service.AutofillRefused) as e:
        service.launch_fill(seed_job(), cfg=cfg())
    assert e.value.http == 403


def test_stale_plan_is_rebuilt_before_fill(gh_api, monkeypatch):
    key = seed_job()
    service.plan_for_job(key, cfg=cfg(), bank=EXAMPLE_BANK)
    with store.connect() as conn:
        conn.execute("UPDATE fill_plans SET created_at = 0, stale_after = 1 WHERE job_key = ?", (key,))
        conn.execute("UPDATE jobs SET last_seen = 0 WHERE key = ?", (key,))
    _launch(monkeypatch, FakeBackend(), key)
    assert len(gh_api) == 2                                        # the hand-off re-checked the posting
    assert store.get_fill_plan(key)["created_at"] > 1


# --------------------------------------------------------------------------- nightly

def test_plan_today_only_approved_and_isolated(gh_api, monkeypatch):
    from jobcopilot import queue as Q
    k1, k2, k3 = (seed_job(url=f"https://job-boards.greenhouse.io/acme/jobs/{i}", external_id=str(i))
                  for i in (1, 2, 3))
    approve_artifact(k1)
    approve_artifact(k3)
    monkeypatch.setattr(Q, "list_queue", lambda **kw: {"day": "2026-11-13",
                                                       "items": [{"key": k} for k in (k1, k2, k3)]})
    orig = service.fetch_greenhouse_questions

    def flaky(board, job_id):
        if job_id == "3":
            raise RuntimeError("boom")
        return orig(board, job_id)
    monkeypatch.setattr(service, "fetch_greenhouse_questions", flaky)
    out = service.plan_today(cfg=cfg())
    assert out["planned"] == 2 and out["by_status"] == {"needs_you": 1, "error": 1}
    assert store.get_fill_plan(k1) and not store.get_fill_plan(k2)


# --------------------------------------------------------------------------- real browser (optional)

FORM_HTML = b"""<!doctype html><html><body>
<form id="f" method="post" action="/submit">
  <label for="email">Email</label><input id="email" name="email" type="email" required>
  <label for="why">Why us?</label><textarea id="why" name="why"></textarea>
  <select id="auth" name="auth" aria-label="Authorized to work in India?"><option>Yes</option><option>No</option></select>
  <input name="beecatcher" type="text" style="position:absolute;left:-9999px">
  <input name="pw" type="password">
  <button type="submit">Submit application</button>
</form>
<script>setTimeout(() => document.getElementById('f').submit(), 50);</script>
</body></html>"""


def test_headless_snapshot_is_read_only_against_local_fixture():
    pytest.importorskip("playwright.sync_api")
    posts: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path.startswith("/apply") else 404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(FORM_HTML if self.path.startswith("/apply") else b"gone")

        def do_POST(self):
            posts.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        c = cfg()
        c["timeouts"] = dict(c["timeouts"], page_load_sec=15)
        try:
            snap = service.PlaywrightBackend().snapshot(f"{base}/apply", "generic", c)
        except Exception as exc:  # noqa: BLE001 - no browser installed (CI)
            pytest.skip(f"no browser available: {type(exc).__name__}")
        fields = {f["name"]: f for f in snap["steps"][0]}
        assert fields["email"]["label"] == "Email" and fields["email"]["locator"] == '[id="email"]'
        assert fields["auth"]["options"] == ["Yes", "No"]
        assert fields["beecatcher"]["hidden"] is True and "pw" not in fields
        assert posts == []                                # the page's own auto-submit was aborted
        assert service.PlaywrightBackend().snapshot(f"{base}/gone", "generic", c) is None
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- review findings 3 + 4

def _typed(plan: FillPlan) -> dict[str, str]:
    return {a.label: a.value for a in plan.actions() if a.value is not None and a.kind != "upload"}


def test_fill_re_resolves_values_from_current_answers_not_the_stored_plan(gh_api, monkeypatch):
    """Finding 3: a plan built while answers were approved must not type them once
    they aren't (the fill-time bank here is an empty profile dir)."""
    key = seed_job()
    approve_artifact(key)
    built = service.plan_for_job(key, cfg=cfg(), bank=EXAMPLE_BANK)
    assert _typed(built)                                          # approved when planned
    fake = FakeBackend(states=[None])
    _launch(monkeypatch, fake, key)
    assert _typed(fake.plans[0]) == {}                            # nothing approved NOW -> nothing typed
    assert len(gh_api) == 1                                       # fresh: re-resolved from the stored snapshot
    # ... and with the example profile approved now, the same stored plan fills again
    monkeypatch.setenv("JOBCOPILOT_PROFILE_DIR", str(Path(A.example_answers_path()).parent))
    fake2 = FakeBackend(states=[None])
    _launch(monkeypatch, fake2, key)
    assert _typed(fake2.plans[0]).get("First Name") == "Alex" and len(gh_api) == 1


def test_fill_uploads_only_the_currently_approved_resume(gh_api, monkeypatch):
    """Finding 3: résumé rejected after planning, a new one approved: the fill
    uploads the new one (plan.files recomputed at fill time)."""
    key = seed_job()
    old_pdf = approve_artifact(key)
    service.plan_for_job(key, cfg=cfg(), bank=EXAMPLE_BANK)
    store.update_artifact(store.get_job(key)["artifact_id"], status="rejected")
    new_pdf = approve_artifact(key)
    fake = FakeBackend(states=[None])
    _launch(monkeypatch, fake, key)
    assert fake.plans[0].files.get("resume") == str(new_pdf.resolve()) != str(old_pdf.resolve())
    # rejected without a replacement: no upload at all
    store.update_artifact(store.get_job(key)["artifact_id"], status="rejected")
    fake2 = FakeBackend(states=[None])
    _launch(monkeypatch, fake2, key)
    up = [a for a in fake2.plans[0].actions() if a.kind == "upload"]
    assert fake2.plans[0].files == {} and all(a.value is None for a in up)


def test_api_fill_plan_never_serves_values(gh_api):
    """Finding 4: GET /api/jobs/{key}/fill-plan returns metadata + approval state only,
    also for a legacy row that still has values in it."""
    from fastapi.testclient import TestClient
    from server.app import app
    key = seed_job()
    pdf = approve_artifact(key)
    service.plan_for_job(key, cfg=cfg(), bank=EXAMPLE_BANK)
    row = store.get_fill_plan(key)
    legacy = copy.deepcopy(row["plan"])
    legacy.pop("format")
    for a in legacy["steps"][0]:
        if a["label"] == "First Name":
            a["value"] = "Alex"
    legacy["files"] = {"resume": str(pdf)}
    store.save_fill_plan(key, legacy, ats=row["ats"], url=row["url"], unresolved_count=1,
                         created_at=row["created_at"], stale_after=row["stale_after"])
    with TestClient(app) as c:
        r = c.get(f"/api/jobs/{key}/fill-plan")
    assert r.status_code == 200
    body = json.dumps(r.json())
    for secret in ("Alex", "alex@example.com", str(pdf)):
        assert secret not in body
    acts = {a["label"]: a for s in r.json()["plan"]["steps"] for a in s}
    assert acts["First Name"]["value"] is None and acts["First Name"]["approved"] is True
    assert "snapshot" not in r.json()["plan"]
    # a legacy row (no snapshot) is never trusted for a fill: it is rebuilt
    assert service._fresh_plan(store.get_job(key), cfg(), None) is None


def test_migration_v7_strips_values_from_old_plans(tmp_path):
    db = tmp_path / "old.db"
    migrations.migrate(db, [m for m in migrations.MIGRATIONS if m[0] <= 6])
    old = {"job_key": "k", "ats": "generic", "url": "https://x.test", "files": {"resume": "/a/r.pdf"},
           "steps": [[{"locator": "#e", "label": "Email", "kind": "fill", "value": "alex@example.com",
                       "source": "answers.yaml:personal.email", "confidence": 1.0, "needs_user": False},
                      {"locator": "#cv", "label": "Resume", "kind": "upload", "value": "resume",
                       "source": "file:resume", "confidence": 1.0, "needs_user": False},
                      {"locator": "#w", "label": "Why", "kind": "fill", "value": None,
                       "source": "none", "confidence": 0.0, "needs_user": True}]]}
    with store.connect(db) as conn:
        conn.execute("INSERT INTO fill_plans(job_key, ats, url, plan, unresolved_count, created_at, stale_after)"
                     " VALUES ('k', 'generic', 'https://x.test', ?, 0, 1, 9e12)", (json.dumps(old),))
    assert migrations.migrate(db) == migrations.latest_version() >= 7
    row = store.get_fill_plan("k", db)
    assert "alex@example.com" not in json.dumps(row["plan"]) and row["plan"]["files"] == {}
    acts = row["plan"]["steps"][0]
    assert [a["value"] for a in acts] == [None, "resume", None]
    assert [a["approved"] for a in acts] == [True, True, False]
    assert row["stale_after"] == 0
