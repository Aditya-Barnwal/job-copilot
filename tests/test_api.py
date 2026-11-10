"""HTTP API: route table, server-side grounding gate on /approve (F4), the
draft-email path (F5), and the empty-model-reply errors (F3).

Offline: the store is the tmp DB from conftest.py, companies.yaml is a tmp
file, and every LLM entry point is stubbed to fail loudly unless a test
supplies a fake reply.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.app as server_app
from jobcopilot import email_draft, llm, store
from jobcopilot.filters import FilterResult
from jobcopilot.models import Job
from server import deps

ORIGINAL = ("Backend engineer. Built REST APIs in Python and Django on AWS with "
            "Postgres. Cut p95 latency by 30% across 4 services.")
CLEAN = ("Backend engineer. Built Python and Django REST APIs with Postgres on "
         "AWS. Cut p95 latency by 30% across 4 services.")
# kubernetes is a job "missing keyword" -> inserted_unsupported;
# terraform is outside it -> unsupported_skills.
FABRICATED = ORIGINAL + " Deployed them with Terraform onto Kubernetes."
FABRICATED_MORE = FABRICATED + " Led a team of 5."   # + inflated_claims, new_numbers

COMPANIES_YAML = """companies:
  - name: Acme
    connector: greenhouse
    slug: acme
    apply_method: email
    contact_email: jobs@acme.example.com
    funded_startup: true
  - name: Foo
    connector: greenhouse
    slug: foo
"""

# Every path + method the vanilla-JS UI (and anything else) may call. The
# router split must not add, drop or rename any of them.
EXPECTED_ROUTES = {
    ("GET", "/"),
    ("GET", "/api/meta"), ("GET", "/api/meta-kv"), ("POST", "/api/meta-kv"),
    ("GET", "/api/llm/health"),
    ("POST", "/api/ingest"), ("POST", "/api/score"), ("POST", "/api/funding/run"),
    ("GET", "/api/jobs"), ("GET", "/api/jobs/{key}"), ("POST", "/api/jobs/{key}/status"),
    ("GET", "/api/export.csv"), ("GET", "/api/jobs/{key}/apply-pack"),
    ("POST", "/api/resume"), ("GET", "/api/user-yoe"), ("POST", "/api/user-yoe"),
    ("GET", "/api/profile"), ("POST", "/api/profile"),
    ("GET", "/api/answer-pack"), ("POST", "/api/answer-pack"), ("DELETE", "/api/answer-pack"),
    ("GET", "/api/jobs/{key}/resume-options"), ("GET", "/api/jobs/{key}/tailor-suggestion"),
    ("POST", "/api/jobs/{key}/tailor"), ("POST", "/api/jobs/{key}/approve"),
    ("POST", "/api/jobs/{key}/draft-email"),
    ("GET", "/api/resume-versions"), ("GET", "/api/resume-versions/{vid}/download"),
}


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    def _refuse(*_a, **_kw):
        raise AssertionError("tests must not call a real LLM")
    monkeypatch.setattr(llm, "complete", _refuse)
    monkeypatch.setattr(llm, "complete_json", _refuse)


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "companies.yaml").write_text(COMPANIES_YAML, encoding="utf-8")
    monkeypatch.setenv("JOBCOPILOT_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(deps, "_company_meta", None)
    # Logging setup attaches global handlers; not what these tests are about.
    monkeypatch.setattr(server_app, "setup_logging", lambda _dir: None)
    with TestClient(server_app.app) as c:   # `with` runs the lifespan
        yield c


def seed_job(company: str = "Foo", external_id: str = "1",
             missing: list[str] | None = None) -> str:
    job = Job(
        source="greenhouse", company=company, company_sector="tech",
        external_id=external_id, title="Backend Engineer", location_text="Bengaluru",
        workplace_type="onsite", country_bucket="IN", url="https://example.test/jobs/1",
        description_text="Python, Django, Kubernetes",
    )
    store.upsert_jobs([FilterResult(job=job, package_estimated=False, country_uncertain=False)])
    store.set_scores(job.key, 60.0, 0.5, ["python", "django", "kubernetes"],
                     ["kubernetes"] if missing is None else missing)
    return job.key


def approve(client: TestClient, key: str, **body):
    return client.post(f"/api/jobs/{key}/approve", json=body)


def _flatten(routes):
    for r in routes:
        inner = getattr(r, "original_router", None)   # FastAPI >= 0.13x wraps includes
        if inner is not None:
            yield from _flatten(inner.routes)
        else:
            yield r


# --- app assembly ----------------------------------------------------------

def test_route_table_unchanged():
    actual = {(m, r.path) for r in _flatten(server_app.app.routes)
              for m in (getattr(r, "methods", None) or ()) if m != "HEAD"}
    docs = {("GET", p) for p in ("/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc")}
    assert actual - docs == EXPECTED_ROUTES


def test_lifespan_initialises_db_and_company_meta(client):
    assert deps._company_meta["Acme"]["contact_email"] == "jobs@acme.example.com"
    assert store.resolve_db_path().exists()


def test_meta_summary_shape(client):
    seed_job()
    r = client.get("/api/meta")
    assert r.status_code == 200
    assert r.json() == {"last_ingest": None, "status_counts": {"new": 1},
                        "resume_loaded": False, "resume_filename": None,
                        "version_count": 0}


def test_ui_does_not_hide_senior_roles_by_default():
    html = (Path(server_app.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    tag = next(line for line in html.splitlines() if 'id="f-hide-senior"' in line)
    assert "checked" not in tag


# --- jobs ------------------------------------------------------------------

def test_list_jobs_shape_and_company_enrichment(client):
    acme = seed_job(company="Acme")
    r = client.get("/api/jobs")
    assert r.status_code == 200
    data = r.json()
    assert set(data) == {"jobs", "count"} and data["count"] == 1
    job = data["jobs"][0]
    assert job["key"] == acme
    assert job["apply_method"] == "email" and job["funded_startup"] is True
    assert job["fit_score"] == 55.0   # 0.5*60 + 0.5*50
    assert job["missing_keywords"] == ["kubernetes"]


def test_list_jobs_min_score_uses_fit(client):
    seed_job()   # match 60, fit 55
    assert client.get("/api/jobs", params={"min_score": 58}).json()["count"] == 0
    assert client.get("/api/jobs", params={"min_score": 55}).json()["count"] == 1


def test_status_endpoint(client):
    key = seed_job()
    r = client.post(f"/api/jobs/{key}/status", json={"status": "interested"})
    assert r.status_code == 200 and r.json() == {"ok": True, "status": "interested"}
    assert store.get_job(key)["status"] == "interested"
    assert client.post(f"/api/jobs/{key}/status", json={"status": "bogus"}).status_code == 400
    assert client.post("/api/jobs/nope/status", json={"status": "new"}).status_code == 404


# --- approve: server-side grounding gate (F4) -------------------------------

def test_approve_clean_text_saves_version(client):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    r = approve(client, key, tailored_text=CLEAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["mode"] == "fresh"
    assert set(body) == {"ok", "mode", "version_id", "pdf_path", "new_ats_coverage"}
    assert Path(body["pdf_path"]).exists()
    assert store.get_job(key)["status"] == "ready"
    assert store.get_job(key)["applied_version_id"] == body["version_id"]


def test_approve_fabricated_skill_is_rejected_and_nothing_saved(client):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    r = approve(client, key, tailored_text=FABRICATED)
    assert r.status_code == 409
    payload = r.json()
    assert payload["grounding"]["clean"] is False
    assert payload["unconfirmed"] == {"inserted_unsupported": ["kubernetes"],
                                      "unsupported_skills": ["terraform"]}
    assert store.list_versions() == []
    assert store.get_job(key)["status"] == "new"
    assert not list(store.resolve_resumes_dir().glob("*.pdf"))


def test_approve_with_every_flag_confirmed_succeeds(client):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    confirmed = {"inserted_unsupported": ["Kubernetes"],   # case-insensitive
                 "unsupported_skills": ["TERRAFORM", "extra is fine"],
                 "new_numbers": ["5"], "inflated_claims": ["led"]}
    r = approve(client, key, tailored_text=FABRICATED_MORE, confirmed_flags=confirmed)
    assert r.status_code == 200, r.text
    assert len(store.list_versions()) == 1


def test_approve_with_partial_confirmation_is_rejected(client):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    confirmed = {"inserted_unsupported": ["kubernetes"], "unsupported_skills": ["terraform"],
                 "inflated_claims": ["led"]}   # new_numbers not confirmed
    r = approve(client, key, tailored_text=FABRICATED_MORE, confirmed_flags=confirmed)
    assert r.status_code == 409
    assert r.json()["unconfirmed"] == {"new_numbers": ["5"]}
    assert store.list_versions() == []


@pytest.mark.parametrize("bad", ["yes", ["kubernetes"], {"inserted_unsupported": "kubernetes"}])
def test_approve_malformed_confirmation_fails_closed(client, bad):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    r = approve(client, key, tailored_text=FABRICATED, confirmed_flags=bad)
    assert r.status_code == 409
    assert store.list_versions() == []


def test_approve_user_approved_keywords_are_honoured(client):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    r = approve(client, key, tailored_text=FABRICATED,
                approved_keywords=["kubernetes", "terraform"])
    assert r.status_code == 200, r.text


def test_approve_without_stored_resume_is_400(client):
    key = seed_job()
    r = approve(client, key, tailored_text=CLEAN)
    assert r.status_code == 400
    assert "resume" in r.json()["detail"]
    assert store.list_versions() == []


def test_approve_missing_text_is_400(client):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    assert approve(client, key).status_code == 400
    assert approve(client, key, tailored_text=["not", "a", "string"]).status_code == 400


def test_approve_reuse_path_unchanged(client):
    key = seed_job()
    vid = store.add_version("v", None, None, None, "t", "/tmp/never-read.pdf", [], 0.0)
    r = approve(client, key, reuse_version_id=vid)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "mode": "reuse", "version_id": vid,
                        "pdf_path": "/tmp/never-read.pdf"}
    assert store.get_job(key)["status"] == "ready"


def test_approve_unknown_job_is_404(client):
    assert approve(client, "nope", tailored_text=CLEAN).status_code == 404


# --- draft-email (F5) and empty model replies (F3) -------------------------

def test_draft_email_passes_contact_explicitly_without_patching_store(client, monkeypatch):
    key = seed_job(company="Acme")
    store.set_resume(ORIGINAL, "resume.txt")
    original_get_job = store.get_job
    seen: dict = {}

    def fake_complete_json(system, user, **_kw):
        seen["get_job_untouched"] = store.get_job is original_get_job
        return {"subject": "Backend Engineer", "body": "Built REST APIs in Python."}, {}

    monkeypatch.setattr(email_draft.llm, "complete_json", fake_complete_json)
    r = client.post(f"/api/jobs/{key}/draft-email")
    assert r.status_code == 200, r.text
    assert r.json()["to"] == "jobs@acme.example.com"
    assert seen["get_job_untouched"] is True
    assert store.get_job is original_get_job


def test_draft_email_rejects_non_email_company(client):
    key = seed_job(company="Foo")
    assert client.post(f"/api/jobs/{key}/draft-email").status_code == 400


def test_email_draft_unit_takes_contact_kwargs(monkeypatch):
    key = seed_job(company="Foo")   # jobs table itself has no contact_email
    store.set_resume(ORIGINAL, "resume.txt")
    monkeypatch.setattr(email_draft.llm, "complete_json",
                        lambda *a, **k: ({"subject": "s", "body": "Python REST APIs."}, {}))
    assert "error" in email_draft.draft(key)
    out = email_draft.draft(key, contact_email="hr@foo.example.com", apply_method="email")
    assert out["to"] == "hr@foo.example.com"
    assert "error" in email_draft.draft(key, contact_email="hr@foo.example.com", apply_method="form")


def test_draft_email_empty_body_is_clean_error(client, monkeypatch):
    key = seed_job(company="Acme")
    store.set_resume(ORIGINAL, "resume.txt")
    monkeypatch.setattr(email_draft.llm, "complete_json",
                        lambda *a, **k: ({"subject": "s"}, {"retries": 1}))
    r = client.post(f"/api/jobs/{key}/draft-email")
    assert r.status_code == 400
    assert r.json()["detail"] == "empty body from model"


def test_tailor_empty_reply_is_clean_error(client, monkeypatch):
    from jobcopilot import tailor as tailor_mod
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    monkeypatch.setattr(tailor_mod.llm, "complete_json",
                        lambda *a, **k: ({"tailored_text": "  "}, {"retries": 1}))
    r = client.post(f"/api/jobs/{key}/tailor", json={})
    assert r.status_code == 400
    assert r.json()["detail"] == "model returned empty tailored_text"


def test_tailor_non_object_reply_is_clean_error(client, monkeypatch):
    from jobcopilot import tailor as tailor_mod
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    monkeypatch.setattr(tailor_mod.llm, "complete_json", lambda *a, **k: (["x"], {}))
    r = client.post(f"/api/jobs/{key}/tailor", json={})
    assert r.status_code == 400
    assert "not an object" in r.json()["detail"]
