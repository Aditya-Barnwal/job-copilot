"""HTTP API: route table, server-side grounding gate on /approve (F4), the
draft-email path (F5), and the empty-model-reply errors (F3).

Offline: the store is the tmp DB from conftest.py, companies.yaml is a tmp
file, and every LLM entry point is stubbed to fail loudly unless a test
supplies a fake reply.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
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
    ("GET", "/api/sources/health"),
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


APP_JS = Path(server_app.STATIC_DIR) / "app.js"
INDEX_HTML = Path(server_app.STATIC_DIR) / "index.html"
NODE = shutil.which("node")


def _js_function(src: str, name: str) -> str:
    m = re.search(rf"^function {name}\(.*?^}}\n", src, re.S | re.M)
    assert m, f"function {name} not found in app.js"
    return m.group(0)


def test_security_headers_on_ui_static_and_api(client):
    # Review finding #2: no CSP meant an attribute-injection XSS could exfiltrate
    # the profile and self-confirm /approve. A strict CSP is the backstop.
    for method, path in (("GET", "/"), ("GET", "/static/app.js"),
                         ("GET", "/api/meta"), ("POST", "/api/jobs/nope/status")):
        r = client.request(method, path, json={"status": "new"} if method == "POST" else None)
        csp = r.headers.get("content-security-policy", "")
        directives = {d.split()[0]: d.split()[1:] for d in csp.split(";") if d.strip()}
        assert directives.get("default-src") == ["'self'"], (path, csp)
        assert directives.get("script-src") == ["'self'"]
        assert directives.get("connect-src") == ["'self'"]
        assert directives.get("img-src") == ["'self'", "data:"]
        assert directives.get("style-src") == ["'self'"]
        assert directives.get("object-src") == ["'none'"]
        assert directives.get("frame-ancestors") == ["'none'"]
        assert "unsafe-" not in csp
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("referrer-policy") == "no-referrer"


def test_ui_is_compatible_with_strict_csp():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script>"
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "inline event handler"
    assert not re.search(r"\sstyle\s*=", html, re.I), "inline style attribute"
    js = APP_JS.read_text(encoding="utf-8")
    assert not re.search(r"\sstyle\s*=\s*[\"']", js), "inline style attribute in markup"
    assert not re.search(r"\son[a-z]+\s*=\s*[\"']", js), "inline event handler in markup"


def test_ui_builds_every_link_through_link_html():
    src = APP_JS.read_text(encoding="utf-8")
    outside = src.replace(_js_function(src, "linkHtml"), "")
    assert not re.search(r"href\s*=\s*[\"']", outside), "raw href= outside linkHtml()"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_ui_escaping_and_http_only_links():
    src = APP_JS.read_text(encoding="utf-8")
    fns = "\n".join(_js_function(src, n) for n in ("escapeHtml", "safeHttpUrl", "linkHtml"))
    script = fns + """
globalThis.window = { location: { href: "http://127.0.0.1:8000/" } };
console.log(JSON.stringify({
  esc: escapeHtml(`x" autofocus onfocus='alert(1)' <b>&`),
  bad: ["javascript:alert(1)", "  JaVaScRiPt:alert(1)", "java\tscript:alert(1)",
        "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)", "", null]
        .map(safeHttpUrl),
  https: safeHttpUrl("https://boards.example.com/jobs/1?a=1&b=2"),
  rel: safeHttpUrl("/api/resume-versions/3/download"),
  badLink: linkHtml("javascript:alert(1)", "open posting", 'target="_blank"'),
  goodLink: linkHtml('https://x.example/a"b', "open", 'target="_blank"'),
}));
"""
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["esc"] == "x&quot; autofocus onfocus=&#39;alert(1)&#39; &lt;b&gt;&amp;"
    assert out["bad"] == [None] * 7
    assert out["https"] == "https://boards.example.com/jobs/1?a=1&b=2"
    assert out["rel"] == "http://127.0.0.1:8000/api/resume-versions/3/download"
    assert "<a" not in out["badLink"] and "href" not in out["badLink"]
    assert "open posting" in out["badLink"]
    assert out["goodLink"].startswith('<a href="https://x.example/a%22b"')


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


def _version(text: str) -> int:
    return store.add_version("v", None, None, None, text, "/tmp/never-read.pdf", [], 0.0)


def test_approve_reuse_of_grounded_version_succeeds(client):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    vid = _version(CLEAN)
    r = approve(client, key, reuse_version_id=vid)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "mode": "reuse", "version_id": vid,
                        "pdf_path": "/tmp/never-read.pdf"}
    assert store.get_job(key)["status"] == "ready"
    assert store.get_job(key)["applied_version_id"] == vid


def test_approve_reuse_regrounds_against_current_resume(client):
    # Review finding #4: a stored version (e.g. approved against an older
    # resume, or a v1 version from before the server gate) must pass the same
    # gate as fresh text before it is linked to a job.
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    vid = _version(FABRICATED)
    r = approve(client, key, reuse_version_id=vid)
    assert r.status_code == 409, r.text
    payload = r.json()
    assert payload["grounding"]["clean"] is False
    assert payload["unconfirmed"] == {"inserted_unsupported": ["kubernetes"],
                                      "unsupported_skills": ["terraform"]}
    job = store.get_job(key)
    assert job["status"] == "new" and not job["applied_version_id"]


def test_approve_reuse_with_every_flag_confirmed_succeeds(client):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    vid = _version(FABRICATED)
    confirmed = {"inserted_unsupported": ["kubernetes"], "unsupported_skills": ["terraform"]}
    r = approve(client, key, reuse_version_id=vid, confirmed_flags=confirmed)
    assert r.status_code == 200, r.text
    assert store.get_job(key)["applied_version_id"] == vid


@pytest.mark.parametrize("bad", ["yes", ["kubernetes"], {"inserted_unsupported": "kubernetes"}])
def test_approve_reuse_malformed_confirmation_fails_closed(client, bad):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    r = approve(client, key, reuse_version_id=_version(FABRICATED), confirmed_flags=bad)
    assert r.status_code == 409
    assert store.get_job(key)["status"] == "new"


def test_approve_reuse_without_stored_resume_is_400(client):
    key = seed_job()
    r = approve(client, key, reuse_version_id=_version(CLEAN))
    assert r.status_code == 400
    assert "resume" in r.json()["detail"]
    assert store.get_job(key)["status"] == "new"


@pytest.mark.parametrize("text", ["", "   ", None])
def test_approve_reuse_of_version_without_text_fails_closed(client, text):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    r = approve(client, key, reuse_version_id=_version(text))
    assert r.status_code == 409, r.text
    assert store.get_job(key)["status"] == "new"


@pytest.mark.parametrize("bad_id", ["abc", 1.5, [1], {"id": 1}, True])
def test_approve_reuse_bad_version_id_is_400(client, bad_id):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    r = approve(client, key, reuse_version_id=bad_id)
    assert r.status_code == 400, r.text


def test_approve_reuse_unknown_version_is_404(client):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    assert approve(client, key, reuse_version_id=999).status_code == 404


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
