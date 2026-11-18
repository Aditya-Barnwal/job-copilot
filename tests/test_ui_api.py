"""Phase 5 API (server/routers/ui.py) and the writers behind it: saving an answer
to answers.yaml, confirming a résumé fact, the tracker CSV (formula-injection
guard), re-apply cooldowns, the review card's diff / edit / reject, screenshot
confinement, the sign-in browser endpoint and the SPA serving. Temp profile dirs
only (JOBCOPILOT_PROFILE_DIR); never the real profile/."""
from __future__ import annotations

import csv
import io
import shutil
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from jobcopilot import answers as A
from jobcopilot import facts as F
from jobcopilot import llm, store, tailor_v2
from jobcopilot import queue as jq
from jobcopilot.filters import FilterResult
from jobcopilot.models import Job
from server import app as server_app
from server import deps
from server.deps import csv_safe

EXAMPLE = Path(__file__).resolve().parent.parent / "profile.example"


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    def _refuse(*_a, **_kw):
        raise AssertionError("tests must not call a real LLM")
    monkeypatch.setattr(llm, "complete", _refuse)
    monkeypatch.setattr(llm, "complete_json", _refuse)


@pytest.fixture
def profile(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "profile"
    shutil.copytree(EXAMPLE, d)
    monkeypatch.setenv("JOBCOPILOT_PROFILE_DIR", str(d))
    return d


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "_company_meta", {})
    monkeypatch.setattr(server_app, "setup_logging", lambda _dir: None)
    with TestClient(server_app.app) as c:
        yield c


def seed(company="Acme", ext="1", title="Backend Engineer", status=None) -> str:
    job = Job(source="greenhouse", company=company, company_sector="tech", external_id=ext, title=title,
              location_text="Bengaluru", workplace_type="onsite", country_bucket="IN",
              url=f"https://job-boards.greenhouse.io/{company.lower()}/jobs/{ext}",
              description_text="Python, Go, REST APIs")
    store.upsert_jobs([FilterResult(job=job, package_estimated=False, country_uncertain=False)])
    if status:
        store.set_status(job.key, status)
    return job.key


# --------------------------------------------------------------------------- answers writer

def test_save_custom_answer_keeps_the_rest_of_the_file_byte_for_byte(profile):
    p = profile / "answers.yaml"
    before = p.read_text(encoding="utf-8")
    out = A.save_custom_answer("What is your favourite colour? *", "Green", path=p)
    assert out == {"question": "What is your favourite colour?", "answer": "Green", "replaced": False,
                   "file": "answers.yaml"}
    after = p.read_text(encoding="utf-8")
    assert after.startswith(before)                             # nothing above was touched (comments included)
    assert "custom_answers:                 # approved" in after
    data = yaml.safe_load(after)
    assert data["custom_answers"] == [{"question": "What is your favourite colour?", "answer": "Green"}]
    assert {k: v for k, v in data.items() if k != "custom_answers"} == yaml.safe_load(before)


def test_save_custom_answer_replaces_the_same_question_and_autofill_uses_it(profile):
    p = profile / "answers.yaml"
    A.save_custom_answer("Favourite colour?", "Green", path=p)
    A.save_custom_answer("Line two?", 'a\nb "q"', path=p)
    out = A.save_custom_answer("favourite   COLOUR", "Blue", path=p)
    assert out["replaced"] is True
    bank = A.load(p)
    assert len(bank.data["custom_answers"]) == 2
    a = A.answer_question("Favourite colour?", answers=bank)
    assert (a.value, a.needs_user, a.rule, a.status) == ("Blue", False, "custom", "approved")
    assert A.answer_question("Line two?", answers=bank).value == 'a\nb "q"'
    sel = A.answer_question("Favourite colour?", field_type="select", options=["Red", "Blue"], answers=bank)
    assert sel.value == "Blue"
    assert A.answer_question("Favourite colour?", field_type="select", options=["Red", "Pink"],
                             answers=bank).value == ""          # no matching option: left for you


def test_save_custom_answer_existing_block_in_the_middle_of_the_file(tmp_path):
    p = tmp_path / "answers.yaml"
    p.write_text("schema_version: 2\n# about me\npersonal:   # approved\n  first_name: \"A\"\n\n"
                 "custom_answers:   # approved\n  - question: \"Q1\"\n    answer: \"x\"\n\n"
                 "# tail comment\nwriting:\n  signoff: \"B\"\n", encoding="utf-8")
    A.save_custom_answer("Q2", "y", path=p)
    text = p.read_text(encoding="utf-8")
    assert text.startswith("schema_version: 2\n# about me\npersonal:   # approved\n")
    assert text.endswith("\n\n# tail comment\nwriting:\n  signoff: \"B\"\n")
    assert yaml.safe_load(text)["custom_answers"] == [{"question": "Q1", "answer": "x"}, {"question": "Q2", "answer": "y"}]


@pytest.mark.parametrize("q,a", [
    ("Enter your password", "hunter2"), ("Aadhaar number", "1234"), ("PAN card number", "x"),
    ("Credit card number", "x"), ("Anything", " ".join(["4" + "0" * 3] * 4)), ("", "x"), ("Q", "   "),
    ("Q" * 600, "x"),
])
def test_save_custom_answer_refuses_secrets_and_bad_input(profile, q, a):
    p = profile / "answers.yaml"
    before = p.read_text(encoding="utf-8")
    with pytest.raises(A.AnswerSaveError):
        A.save_custom_answer(q, a, path=p)
    assert p.read_text(encoding="utf-8") == before


def test_save_custom_answer_refuses_an_unexpected_block_shape(tmp_path):
    p = tmp_path / "answers.yaml"
    p.write_text("schema_version: 2\ncustom_answers: {a: 1}\n", encoding="utf-8")
    with pytest.raises(A.AnswerSaveError):
        A.save_custom_answer("Q", "x", path=p)
    assert p.read_text(encoding="utf-8") == "schema_version: 2\ncustom_answers: {a: 1}\n"


def test_unanswered_lists_plan_questions_and_save_endpoint_clears_them(client, profile):
    key = seed()
    plan = {"steps": [[{"label": "First Name", "kind": "fill", "source": "answers.yaml:personal.first_name",
                        "needs_user": False, "approved": True},
                       {"label": "Years with Kafka?", "kind": "select", "source": "none", "needs_user": True,
                        "approved": False, "required": True},
                       {"label": "I agree to the privacy policy", "kind": "check", "source": "none",
                        "needs_user": True}]],
            "unresolved": [{"label": "Years with Kafka?", "reason": "no match", "required": True},
                           {"label": "I agree to the privacy policy", "reason": "consent"},
                           {"label": "Your password", "reason": "x"}],
            "snapshot": {"steps": [[{"label": "Years with Kafka?", "type": "select", "options": ["0", "1-2", "3+"]}]]}}
    store.save_fill_plan(key, plan, ats="greenhouse", url="u", unresolved_count=3, created_at=time.time(),
                         stale_after=None)
    qs = client.get("/api/answers/unanswered").json()["questions"]
    assert [q["question"] for q in qs] == ["Years with Kafka?"]   # consent + secrets never listed
    assert qs[0]["options"] == ["0", "1-2", "3+"] and qs[0]["required"] and qs[0]["jobs"][0]["key"] == key
    r = client.post("/api/answers/save", json={"question": "Years with Kafka?", "answer": "1-2"})
    assert r.status_code == 200 and r.json()["ok"]
    assert client.get("/api/answers/unanswered").json()["questions"] == []
    assert "Years with Kafka?" in (profile / "answers.yaml").read_text(encoding="utf-8")
    bad = client.post("/api/answers/save", json={"question": "Bank account number", "answer": "1"})
    assert bad.status_code == 400


def test_answer_save_needs_the_csrf_header(client, profile):
    r = client.post("/api/answers/save", json={"question": "Q", "answer": "A"}, headers={"X-JobCopilot": ""})
    assert r.status_code == 403
    assert "custom_answers" not in (profile / "answers.yaml").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- facts

def test_facts_review_and_confirm_edits_one_status_line(client, profile):
    p = profile / "resume_facts.yaml"
    before = p.read_text(encoding="utf-8").splitlines()
    d = client.get("/api/facts").json()
    assert d["available"] and any(f["id"] == "exp.acme.b3" and f["status"] == "unconfirmed" for f in d["facts"])
    r = client.post("/api/facts/exp.acme.b3/confirm")
    assert r.status_code == 200 and r.json()["changed"] is True
    after = p.read_text(encoding="utf-8").splitlines()
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(before) == len(after) and len(changed) == 1
    assert changed[0][0].strip() == "status: unconfirmed" and changed[0][1].strip() == "status: confirmed"
    assert F.load(p).by_id()["exp.acme.b3"].status == "confirmed"
    assert client.post("/api/facts/exp.acme.b3/confirm").json()["changed"] is False


def test_facts_confirm_refuses_pending_and_unknown(client, profile):
    p = profile / "resume_facts.yaml"
    before = p.read_text(encoding="utf-8")
    assert client.post("/api/facts/proj.widgetbot.b2/confirm").status_code == 400     # pending: not true yet
    assert client.post("/api/facts/nope.nope/confirm").status_code == 400
    assert client.post("/api/facts/..%2Fetc/confirm").status_code in (400, 404, 405)
    assert p.read_text(encoding="utf-8") == before


def test_confirm_header_with_bullets_after_status(tmp_path):
    src = (EXAMPLE / "resume_facts.yaml").read_text(encoding="utf-8")
    p = tmp_path / "resume_facts.yaml"
    p.write_text(src, encoding="utf-8")
    shutil.copytree(EXAMPLE / "base_resumes", tmp_path / "base_resumes")
    F.confirm("exp.globex", p)
    facts = F.load(p).by_id()
    assert facts["exp.globex"].status == "confirmed" and facts["exp.globex.b1"].status == "confirmed"


# --------------------------------------------------------------------------- tracker CSV + cooldowns

@pytest.mark.parametrize("v,want", [("=1+1", "'=1+1"), ("+cmd", "'+cmd"), ("-2", "'-2"), ("@SUM(A1)", "'@SUM(A1)"),
                                    ("\tx", "'\tx"), ("\rx", "'\rx"), ("ok", "ok"), (None, ""), (3, 3)])
def test_csv_safe(v, want):
    assert csv_safe(v) == want


def test_tracker_csv_guards_formula_injection(client):
    key = seed(company="=HYPERLINK(\"http://evil\")", title="@SUM(1)", status="submitted")
    r = client.get("/api/tracker/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    row = next(x for x in rows if x["status"] == "submitted")
    assert row["company"].startswith("'=") and row["title"] == "'@SUM(1)"
    assert row["submitted"] == time.strftime("%Y-%m-%d")
    assert key
    legacy = client.get("/api/export.csv").text
    assert "'=HYPERLINK" in legacy and "\n=HYPERLINK" not in legacy and ",=HYPERLINK" not in legacy


def test_reapply_cooldown_after_rejection(client):
    k1 = seed(company="Acme", ext="1")
    now = time.time()
    store.set_status(k1, "submitted", at=now - 30 * 86400)
    store.set_status(k1, "rejected", at=now - 10 * 86400)
    old = seed(company="Globex", ext="2")
    store.set_status(old, "rejected", at=now - 200 * 86400)
    cds = jq.reapply_cooldowns(now=now)
    assert [c["company"] for c in cds] == ["Acme"]
    assert cds[0]["days_left"] == 80 and cds[0]["cooldown_days"] == 90
    assert client.get("/api/tracker").json()["cooldowns"][0]["company"] == "Acme"


def test_queue_cards_carry_time_zone_fields():
    key = seed()
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET tz_requirement_text = ?, timezone_restrictions = ? WHERE key = ?",
                     ("4h overlap with IST", '["UTC+5:30"]', key))
        row = dict(conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone())
    card = jq.summarize_job(row)
    assert card["tz_requirement"] == "4h overlap with IST" and card["timezone_restrictions"] == ["UTC+5:30"]


# --------------------------------------------------------------------------- review card

def _artifact(key: str, rephrase: dict | None = None) -> int:
    rf = F.load()
    bases = F.load_bases_for(rf.path)
    base = "backend_sde"
    usable = F.usable_facts(rf, base)
    plan = dict(tailor_v2.EMPTY_PLAN, order=["exp.acme.b2", "exp.acme.b1"], rephrase=rephrase or {},
                kept_original=[], attested_keywords=[], struck_keywords=[])
    lines = tailor_v2.apply_plan(usable, bases[base], plan)
    aid, _ = store.create_artifact(key, base)
    store.update_artifact(aid, edit_plan=plan, status="ok", pages=1, facts_hash=tailor_v2.facts_hash(usable, lines),
                          grounding={"clean": True})
    return aid


def test_artifact_review_recomputes_lines_diff_and_grounding(client, profile):
    key = seed()
    aid = _artifact(key, {"exp.acme.b1": "Built a REST API for a Go order service handling 200+ requests per second."})
    r = client.get(f"/api/artifacts/{aid}/review").json()
    assert r["problem"] is None and not r["stale"] and r["grounding"]["clean"]
    ops = {d["fact_ids"][0]: d["op"] for d in r["diff"]}
    assert ops["exp.acme.b1"] == "rephrased" and ops["exp.acme.b2"] == "moved"
    assert "exp.acme.b1" in r["editable"]
    assert len(r["grounding"]["lines"]) == len(r["lines"])
    assert client.get("/api/artifacts/9999/review").status_code == 404


def test_artifact_edit_makes_a_new_flagged_version_and_never_reverts_your_text(client, profile):
    key = seed()
    aid = _artifact(key)
    text = "Led a Go order service handling 900+ requests per second."
    r = client.post(f"/api/jobs/{key}/artifacts/{aid}/edit", json={"rephrase": {"exp.acme.b1": text}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["version"] == 2 and out["status"] == "needs_review" and not out["grounding"]["clean"]
    new = store.get_artifact(out["artifact_id"])
    assert new["edit_plan"]["rephrase"]["exp.acme.b1"] == text and new["edit_plan"]["user_edited"] == ["exp.acme.b1"]
    assert store.get_artifact(aid)["status"] == "ok"                       # the old version is kept
    rev = client.get(f"/api/artifacts/{out['artifact_id']}/review").json()
    flagged = {t for v in rev["grounding"]["lines"] for t in v["offending"]}
    assert "900" in flagged or "900+" in flagged
    # approval stays gated: unconfirmed flags -> 409
    assert client.post(f"/api/jobs/{key}/artifacts/{out['artifact_id']}/approve", json={}).status_code == 409


@pytest.mark.parametrize("body", [{}, {"rephrase": {}}, {"rephrase": {"exp.acme": "x"}},
                                  {"rephrase": {"skill.go": "x"}}, {"rephrase": {"exp.acme.b1": 5}},
                                  {"rephrase": {"exp.acme.b1": "x" * 700}}])
def test_artifact_edit_bad_input(client, profile, body):
    key = seed()
    aid = _artifact(key)
    assert client.post(f"/api/jobs/{key}/artifacts/{aid}/edit", json=body).status_code == 400


def test_artifact_reject(client, profile):
    key = seed()
    aid = _artifact(key)
    assert client.post(f"/api/jobs/{key}/artifacts/{aid}/reject").json()["status"] == "rejected"
    assert store.get_artifact(aid)["status"] == "rejected"
    store.update_artifact(aid, status="approved")
    assert client.post(f"/api/jobs/{key}/artifacts/{aid}/reject").status_code == 409
    other = seed(ext="2")
    assert client.post(f"/api/jobs/{other}/artifacts/{aid}/reject").status_code == 404


# --------------------------------------------------------------------------- screenshots + sessions

def test_screenshots_are_served_only_from_the_data_dir(client, tmp_path):
    key = seed()
    good = store.applications_dir() / "acme" / "autofill-1.png"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_bytes(b"\x89PNG fake")
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"\x89PNG nope")
    with store.connect() as conn:
        for path in (str(good), str(outside), str(store.applications_dir() / ".." / ".." / "secret.png")):
            conn.execute("INSERT INTO autofill_sessions(job_key, status, started_at, screenshot_path) "
                         "VALUES (?, 'closed', ?, ?)", (key, time.time(), path))
    assert client.get("/api/autofill/sessions/1/screenshot").content == b"\x89PNG fake"
    assert client.get("/api/autofill/sessions/2/screenshot").status_code == 404
    assert client.get("/api/autofill/sessions/3/screenshot").status_code == 404
    sessions = client.get(f"/api/jobs/{key}/autofill/sessions").json()["sessions"]
    assert [s["has_screenshot"] for s in sessions] == [False, False, True]   # newest first
    assert all("screenshot_path" not in s and "pid" not in s for s in sessions)


def test_submission_proof_is_confined(client):
    key = seed()
    proof = store.data_dir() / "proofs" / "p.png"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_bytes(b"\x89PNG proof")
    store.set_status(key, "submitted", proof_path="p.png")
    assert client.get(f"/api/jobs/{key}/proof").content == b"\x89PNG proof"
    store.set_status(seed(ext="2"), "submitted", proof_path="/etc/passwd")
    assert client.get(f"/api/jobs/{seed(ext='2')}/proof").status_code == 404


# --------------------------------------------------------------------------- portals

def test_portals_open_launches_the_sign_in_window(client, monkeypatch):
    from jobcopilot.autofill import session as sess
    calls = []
    monkeypatch.setattr(sess, "open_for_sign_in", lambda **kw: calls.append(kw) or {"ok": True, "profile": "~/x"})
    r = client.post("/api/portals/open")
    assert r.status_code == 200 and calls == [{}]
    key = seed()
    with store.connect() as conn:          # a fill window holds the profile -> refused
        conn.execute("INSERT INTO autofill_sessions(job_key, status, started_at) VALUES (?, 'filling', ?)",
                     (key, time.time()))
    assert client.post("/api/portals/open").status_code == 409 and len(calls) == 1


def test_open_for_sign_in_runs_plain_chrome_on_the_profile(monkeypatch, tmp_path):
    import subprocess
    from jobcopilot.autofill import session as sess
    seen = {}
    monkeypatch.setattr(sess, "chrome_executable", lambda: "/fake/chrome")
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: seen.update(args=args, kw=kw))
    sess.open_for_sign_in(profile_dir=tmp_path / "bp")
    assert seen["args"][0] == "/fake/chrome" and seen["args"][-1] == "about:blank"
    assert f"--user-data-dir={tmp_path / 'bp'}" in seen["args"]
    assert not any("remote-debugging" in a or "automation" in a.lower() for a in seen["args"])
    monkeypatch.setattr(sess, "chrome_executable", lambda: None)
    with pytest.raises(sess.BrowserUnavailable):
        sess.open_for_sign_in(profile_dir=tmp_path / "bp")


def test_phase5_writes_are_refused_in_demo(client, monkeypatch, profile):
    monkeypatch.setenv("JOBCOPILOT_DEMO", "1")
    for path, body in (("/api/portals/open", {}), ("/api/answers/save", {"question": "Q", "answer": "A"}),
                       ("/api/facts/exp.acme.b3/confirm", {}), ("/api/jobs/k/artifacts/1/edit", {"rephrase": {"a": "b"}}),
                       ("/api/jobs/k/artifacts/1/reject", {})):
        r = client.post(path, json=body)
        assert r.status_code == 403, path
    assert client.get("/api/ui/config").json()["demo"] is True
    assert "custom_answers" not in (profile / "answers.yaml").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- SPA serving

def test_spa_serves_index_or_a_build_hint_and_never_shadows_the_api(client, monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    monkeypatch.setattr(server_app, "DIST_DIR", dist)
    r = client.get("/tracker")
    assert r.status_code == 200 and "make ui" in r.text and "<script" not in r.text
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<html><body><div id="root"></div><script type="module" src="/assets/a.js"></script></body></html>')
    (dist / "assets" / "a.js").write_text("console.log(1)")
    (dist / "theme-init.js").write_text("1")
    assert 'id="root"' in client.get("/").text and 'id="root"' in client.get("/today").text
    assert client.get("/assets/a.js").text == "console.log(1)"
    assert client.get("/theme-init.js").text == "1"
    assert "FastAPI" not in client.get("/assets/../../app.py").text            # URL normalised: never a file outside dist
    assert server_app._dist_file("../app.py") is None and server_app._dist_file("assets/../../app.py") is None
    assert client.get("/assets/nope.js").status_code == 404
    assert client.get("/api/nope").status_code == 404
    assert client.get("/api/nope").json() == {"detail": "Not Found"}
    monkeypatch.setenv("JOBCOPILOT_DEMO", "1")
    assert 'class="demo-banner"' in client.get("/tracker").text


# --------------------------------------------------------------------------- v1 statuses

def test_v1_status_names_are_migrated(tmp_path):
    from jobcopilot import migrations
    db = tmp_path / "old.db"
    store.init_db(db)
    keys = {}
    for i, st in enumerate(("interested", "ready", "applied", "new", "rejected")):
        job = Job(source="greenhouse", company=f"C{i}", company_sector="tech", external_id=str(i), title="T",
                  location_text="Bengaluru", workplace_type="onsite", country_bucket="IN", url=f"https://x/{i}",
                  description_text="")
        store.upsert_jobs([FilterResult(job=job, package_estimated=False, country_uncertain=False)], db_path=db)
        store.set_status(job.key, st, db_path=db)
        keys[st] = job.key
    with store.connect(db) as conn:          # pretend the DB predates v8
        conn.execute("DELETE FROM schema_version WHERE version >= 8")
    assert migrations.migrate(db) == migrations.latest_version()
    got = {st: store.get_job(k, db)["status"] for st, k in keys.items()}
    assert got == {"interested": "found", "ready": "ready_to_submit", "applied": "submitted",
                   "new": "new", "rejected": "rejected"}
    hist = {h["status"] for k in keys.values() for h in store.status_history(k, db)}
    assert not hist & {"interested", "ready", "applied"}


# --------------------------------------------------------------------------- hidden by you

def test_hidden_by_you_lists_and_unhide_restores_the_previous_status(client):
    k1 = seed(ext="1")
    now = time.time()
    store.set_status(k1, "submitted", at=now - 5 * 86400)
    store.set_status(k1, "not_interested", at=now - 86400)
    k2 = seed(ext="2")
    store.set_status(k2, "not_interested", at=now - 3600)       # hidden straight from the queue
    seed(ext="3")
    d = client.get("/api/hidden").json()
    assert [i["key"] for i in d["items"]] == [k2, k1] and d["total"] == 2
    one = next(i for i in d["items"] if i["key"] == k1)
    assert one["previous_status"] == "submitted" and abs(one["hidden_at"] - (now - 86400)) < 1
    assert next(i for i in d["items"] if i["key"] == k2)["previous_status"] == "found"   # fallback
    r = client.post(f"/api/jobs/{k1}/unhide")
    assert r.json() == {"ok": True, "status": "submitted"} and store.get_job(k1)["status"] == "submitted"
    assert client.post(f"/api/jobs/{k2}/unhide").json()["status"] == "found"
    assert client.get("/api/hidden").json()["items"] == []
    assert client.post(f"/api/jobs/{k1}/unhide").status_code == 409
    assert client.post("/api/jobs/nope/unhide").status_code == 404
    hist = store.status_history(k1)
    assert hist[-1]["status"] == "submitted" and hist[-1]["note"] == "unhidden"


def test_unhide_is_allowed_in_demo_like_other_status_changes():
    from jobcopilot import demo
    assert demo.refusal("POST", "/api/jobs/abc/unhide") is None
    assert demo.refusal("POST", "/api/jobs/abc/unhide/x") is not None


def test_portals_lists_check_by_hand_companies(client):
    d = client.get("/api/portals").json()
    names = {m["name"]: m for m in d["manual"]}
    assert "Snabbit" in names and "Porter (porter.in)" in names
    assert names["Porter (porter.in)"]["url"].startswith("https://porter.in/")
    assert all(not m["url"] or m["url"].startswith(("https://", "http://")) for m in d["manual"])
