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
    # Phase 2 (server/routers/queue.py)
    ("GET", "/api/queue"), ("GET", "/api/matches"), ("GET", "/api/excluded"),
    ("GET", "/api/runs"), ("GET", "/api/runs/{run_id}"), ("POST", "/api/runs/daily"),
    ("GET", "/api/jobs/{key}/fit"), ("GET", "/api/jobs/{key}/history"), ("GET", "/api/tracker"),
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


WEB = Path(server_app.ROOT) / "web"
WEB_SRC = WEB / "src"
DIST_INDEX = Path(server_app.DIST_DIR) / "index.html"
NODE = shutil.which("node")


def _web_sources() -> dict[str, str]:
    return {str(p.relative_to(WEB_SRC)): p.read_text(encoding="utf-8")
            for p in sorted(WEB_SRC.rglob("*")) if p.suffix in (".ts", ".tsx")}


def test_security_headers_on_ui_static_and_api(client):
    # Review finding #2: no CSP meant an attribute-injection XSS could exfiltrate
    # the profile and self-confirm /approve. A strict CSP is the backstop.
    for method, path in (("GET", "/"), ("GET", "/today"), ("GET", "/assets/missing.js"),
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


def _assert_csp_safe_html(html: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script>"
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "inline event handler"
    assert not re.search(r"\sstyle\s*=", html, re.I), "inline style attribute"
    assert not re.search(r"(src|href)\s*=\s*[\"']https?://", html, re.I), "external script/style/font"


def test_ui_is_compatible_with_strict_csp():
    _assert_csp_safe_html((WEB / "index.html").read_text(encoding="utf-8"))
    if DIST_INDEX.exists():                                   # the built page (make ui)
        _assert_csp_safe_html(DIST_INDEX.read_text(encoding="utf-8"))
    for name, src in _web_sources().items():
        assert "dangerouslySetInnerHTML" not in src, name
        assert not re.search(r"\beval\(|new Function\(|\.innerHTML\s*=|insertAdjacentHTML", src), name
        assert "fonts.googleapis" not in src and "cdn." not in src, name
    css = (WEB_SRC / "index.css").read_text(encoding="utf-8")
    assert "@import url(" not in css and "googleapis" not in css      # fonts are self-hosted (@fontsource)
    cfg = (WEB / "vite.config.ts").read_text(encoding="utf-8")
    assert "assetsInlineLimit: 0" in cfg                      # no data: fonts (font-src is 'self')


def test_ui_links_only_go_to_http_or_same_origin():
    """Every href in the React UI is a vetted value: safeHttpUrl() output, a router
    path, or a same-origin /api/ URL; never a raw string from job data."""
    allowed = re.compile(r"^(href|to|current\.pdf_url|v\.pdf_url|`/api/[^`]*`|\"/api/[^\"]*\")$")
    for name, src in _web_sources().items():
        for m in re.finditer(r"href=\{(`[^`]*`|[^}`]*)\}", src):
            assert allowed.match(m.group(1).strip()), f"{name}: href={{{m.group(1)}}}"
        if name != "lib/api.ts":                                # its comment names the schemes it refuses
            assert "javascript:" not in src, name
    job = _web_sources()["components/JobCard.tsx"]
    assert job.count("safeHttpUrl(") >= 3                       # posting, attribution, keyboard 'o'


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_ui_http_only_links_and_friendly_errors():
    api_ts = WEB_SRC / "lib" / "api.ts"
    script = f"""
globalThis.window = {{ location: {{ href: "http://127.0.0.1:8000/" }} }};
const m = await import({json.dumps(api_ts.as_uri())});
console.log(JSON.stringify({{
  bad: ["javascript:alert(1)", "  JaVaScRiPt:alert(1)", "java\tscript:alert(1)",
        "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)", "", null].map(m.safeHttpUrl),
  https: m.safeHttpUrl("https://boards.example.com/jobs/1?a=1&b=2"),
  rel: m.safeHttpUrl("/api/resume-versions/3/download"),
  net: m.friendlyError(0, null, "/api/x"),
  demo: m.friendlyError(403, {{demo: true}}, "/api/jobs/k/tailor/v2"),
  detail: m.friendlyError(409, {{detail: "Confirm every flag"}}),
  server: m.friendlyError(500, null),
}}));
"""
    r = subprocess.run([NODE, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", script],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0 and "strip-types" in r.stderr:
        pytest.skip("this node can't strip TypeScript types")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["bad"] == [None] * 7
    assert out["https"] == "https://boards.example.com/jobs/1?a=1&b=2"
    assert out["rel"] == "http://127.0.0.1:8000/api/resume-versions/3/download"
    assert out["net"] == "Couldn't reach the app — is it running?"
    assert "public demo" in out["demo"] and out["detail"] == "Confirm every flag"
    assert "server error 500" in out["server"]


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
                 "new_numbers": ["5"], "inflated_claims": ["led", "team of (scope)"]}
    r = approve(client, key, tailored_text=FABRICATED_MORE, confirmed_flags=confirmed)
    assert r.status_code == 200, r.text
    assert len(store.list_versions()) == 1


def test_approve_with_partial_confirmation_is_rejected(client):
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    confirmed = {"inserted_unsupported": ["kubernetes"], "unsupported_skills": ["terraform"],
                 "inflated_claims": ["led", "team of (scope)"]}   # new_numbers not confirmed
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


# --- Phase 2: queue / matches / excluded / runs / fit / tracker ----------------

def seed_v2(ext: str, *, company: str = "Foo", lane: str = "main", fit: float | None = 50.0,
            reasons: list | None = None) -> str:
    job = Job(source="greenhouse", company=company, company_sector="tech", external_id=ext,
              title="Backend Engineer", location_text="Bengaluru", workplace_type="onsite",
              country_bucket="IN", url=f"https://example.test/jobs/{ext}", description_text="Python")
    r = FilterResult(job=job, package_estimated=True, country_uncertain=False)
    r.evaluation = {"lane": lane, "excluded": int(lane == "excluded"),
                    "exclusion_reasons": reasons or [], "flags": []}
    store.upsert_jobs([r])
    if fit is not None:
        store.set_fit(job.key, {"fit_score_v2": fit, "fit_why": "good fit",
                                "fit_components": {"components": {"skills": fit}, "weights": {"skills": 1}},
                                "matched_skills": ["python"], "missing_skills": ["go"]})
    return job.key


def test_status_endpoint_tracker_statuses_history_and_undo(client):
    key = seed_job()
    r = client.post(f"/api/jobs/{key}/status", json={"status": "ready_to_submit"})
    assert r.status_code == 200 and r.json() == {"ok": True, "status": "ready_to_submit"}
    r = client.post(f"/api/jobs/{key}/status",
                    json={"status": "submitted", "source": "detected", "note": "thank-you page"})
    assert r.status_code == 200
    job = store.get_job(key)
    assert job["submit_source"] == "detected" and job["submitted_at"]
    assert client.post(f"/api/jobs/{key}/status", json={"status": "ready_to_submit"}).status_code == 200
    assert store.get_job(key)["submitted_at"] is None                      # undo
    hist = client.get(f"/api/jobs/{key}/history").json()["history"]
    assert [h["status"] for h in hist] == ["ready_to_submit", "submitted", "ready_to_submit"]
    assert hist[1]["note"] == "thank-you page"
    assert client.post(f"/api/jobs/{key}/status", json={"status": "applied", "notes": "v1"}).status_code == 200
    assert store.get_job(key)["notes"] == "v1"                              # v1 body still works
    assert client.post(f"/api/jobs/{key}/status",
                       json={"status": "found", "source": "robot"}).status_code == 400
    assert client.get("/api/jobs/nope/history").status_code == 404


def test_queue_and_matches_endpoints(client):
    from jobcopilot import queue as jq
    a = seed_v2("a", fit=80); b = seed_v2("b", company="Bar", fit=60)
    seed_v2("w", company="Wat", lane="extended", fit=70)
    jq.build_daily_queue(day="2026-11-12")
    r = client.get("/api/queue")
    assert r.status_code == 200
    body = r.json()
    assert body["day"] == "2026-11-12" and [i["key"] for i in body["items"]] == [a, b]
    item = body["items"][0]
    for k in ("fit_score", "why", "matched_skills", "missing_skills", "yoe", "location",
              "source", "attribution_name", "age_days", "rank"):
        assert k in item
    assert [i["company"] for i in client.get("/api/queue", params={"lane": "extended"}).json()["items"]] == ["Wat"]
    assert client.get("/api/queue", params={"lane": "bogus"}).status_code == 400

    m = client.get("/api/matches", params={"limit": 1}).json()
    assert m["total"] == 2 and [i["key"] for i in m["items"]] == [a]
    client.post(f"/api/jobs/{a}/status", json={"status": "submitted"})
    assert client.get("/api/matches").json()["total"] == 1                 # submitted: gone
    assert [i["key"] for i in client.get("/api/queue").json()["items"]] == [b]
    assert client.get("/api/matches", params={"sort": "nope"}).status_code == 400


def test_excluded_fit_and_tracker_endpoints(client):
    x = seed_v2("x", lane="excluded", fit=None,
                reasons=[{"rule": "geo", "detail": "US only", "excerpt": "Remote - US only"}])
    k = seed_v2("k", fit=66)
    ex = client.get("/api/excluded", params={"rule": "geo"}).json()
    assert ex["total"] == 1 and ex["items"][0]["key"] == x
    assert ex["items"][0]["reasons"][0]["excerpt"] == "Remote - US only"
    assert client.get("/api/excluded", params={"rule": "yoe"}).json()["total"] == 0
    fit = client.get(f"/api/jobs/{k}/fit").json()
    assert fit["fit_score"] == 66 and fit["components"] == {"skills": 66} and fit["why"] == "good fit"
    assert client.get("/api/jobs/nope/fit").status_code == 404
    client.post(f"/api/jobs/{k}/status", json={"status": "ready_to_submit"})
    t = client.get("/api/tracker").json()
    cols = {c["status"]: c for c in t["columns"]}
    assert cols["ready_to_submit"]["count"] == 1
    assert cols["ready_to_submit"]["items"][0]["status_changed_at"]
    assert t["ready_to_submit"][0]["days_waiting"] == 0 and t["ready_to_submit"][0]["reminder_due"] is False
    assert t["followups_due"] == []


def test_runs_endpoints_start_in_background_and_refuse_a_second(client, monkeypatch):
    import time as _time

    from jobcopilot import daily

    def fake_run(*, run_id, skip_discovery=False, **_kw):
        store.update_run(run_id, status="ok", summary={"skip": skip_discovery}, finished=True)
        return {"run_id": run_id}
    monkeypatch.setattr(daily, "run", fake_run)
    r = client.post("/api/runs/daily", json={"skip_discovery": True})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    for _ in range(200):
        if store.get_run(run_id)["status"] != "running":
            break
        _time.sleep(0.01)
    assert client.get(f"/api/runs/{run_id}").json()["summary"] == {"skip": True}
    runs = client.get("/api/runs").json()
    assert runs["runs"][0]["id"] == run_id and runs["running"] is None

    live = store.start_run("daily")
    r = client.post("/api/runs/daily")
    assert r.status_code == 409 and r.json()["detail"]["run_id"] == live
    assert client.get("/api/runs/99999").status_code == 404


# --- Phase 2 review fixes ------------------------------------------------------

def test_approve_never_moves_a_submitted_job_backwards(client):
    """P3: re-approving a résumé after submitting must keep 'submitted'."""
    key = seed_job()
    store.set_resume(ORIGINAL, "resume.txt")
    store.set_status(key, "submitted")
    r = approve(client, key, tailored_text=CLEAN)
    assert r.status_code == 200, r.text
    assert store.get_job(key)["status"] == "submitted"
    vid = r.json()["version_id"]
    assert approve(client, key, reuse_version_id=vid).status_code == 200
    assert store.get_job(key)["status"] == "submitted"
    store.set_status(key, "not_interested")               # dismissed stays dismissed too
    approve(client, key, tailored_text=CLEAN)
    assert store.get_job(key)["status"] == "not_interested"
    store.set_status(key, "tailored")                      # tailored -> ready still works
    approve(client, key, tailored_text=CLEAN)
    assert store.get_job(key)["status"] == "ready"


def test_status_endpoint_validates_notes_and_proof_path(client):
    """P9."""
    key = seed_job()
    post = lambda **b: client.post(f"/api/jobs/{key}/status", json={"status": "submitted", **b})  # noqa: E731
    assert post(notes=["x"]).status_code == 400
    assert post(note={"a": 1}).status_code == 400
    assert post(note="x" * 2001).status_code == 400
    assert post(proof_path="/etc/passwd").status_code == 400
    assert post(proof_path="../jobcopilot.db").status_code == 400
    assert post(proof_path=str(store.data_dir() / "proofs" / ".." / "x.png")).status_code == 400
    assert store.get_job(key)["status"] == "new"           # nothing was written
    proofs = store.data_dir() / "proofs"
    proofs.mkdir(parents=True, exist_ok=True)
    r = post(note="sent", proof_path=str(proofs / "acme" / "shot.png"))
    assert r.status_code == 200, r.text
    assert store.get_job(key)["submit_proof_path"] == "acme/shot.png"   # stored relative
    assert post(proof_path="acme/again.png").status_code == 200


def test_ingest_endpoint_starts_from_filters_yaml(client, monkeypatch, tmp_path):
    """P4: the API run uses the nightly config; only keys in the body override it."""
    import os
    cfg_dir = Path(os.environ["JOBCOPILOT_CONFIG_DIR"])
    (cfg_dir / "filters.yaml").write_text(
        "roles: {families: [data engineer]}\nsalary_floors: {INR: 2500000}\n"
        "fx_to_inr: {USD: 90}\ngeo: {keep_unknown_country: false}\n", encoding="utf-8")
    seen = []
    import server.routers.ingest as ingest_router
    monkeypatch.setattr(ingest_router, "run_pipeline", lambda **kw: seen.append(kw["filter_cfg"]) or {})
    assert client.post("/api/ingest", json={}).status_code == 200
    fc = seen[-1]
    assert (fc.min_lpa, fc.usd_to_inr, fc.target_roles, fc.keep_unknown_country) == \
        (25.0, 90.0, ["data engineer"], False)
    assert client.post("/api/ingest", json={"min_lpa": 10}).status_code == 200
    fc = seen[-1]
    assert (fc.min_lpa, fc.usd_to_inr, fc.target_roles) == (10.0, 90.0, ["data engineer"])
    assert client.post("/api/ingest", json={"min_lpa": "lots"}).status_code == 400


# --- Phase 3: tailoring v2 routes (server/routers/tailor.py) --------------------

import importlib.util  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from jobcopilot import tailor_v2  # noqa: E402

EXPECTED_ROUTES.update({
    ("POST", "/api/jobs/{key}/tailor/v2"), ("GET", "/api/jobs/{key}/tailor/v2/keywords"),
    ("POST", "/api/jobs/{key}/artifacts/{artifact_id}/approve"),
    ("GET", "/api/jobs/{key}/artifacts"), ("GET", "/api/artifacts/{artifact_id}/pdf"),
})
if importlib.util.find_spec("server.routers.writing") is not None:   # workstream C's router
    from server.routers import writing as _writing  # noqa: E402
    EXPECTED_ROUTES.update({(m, r.path) for r in _flatten(_writing.router.routes)
                            for m in (getattr(r, "methods", None) or ()) if m != "HEAD"})

EXAMPLE_PROFILE = Path(__file__).resolve().parent.parent / "profile.example"
B1 = "Built a Go order service handling 200+ requests per second behind a REST API."


@pytest.fixture(autouse=True)
def _no_real_profile(tmp_path, monkeypatch):
    """Every test in this module sees NO confirmed facts unless it asks for the
    example profile, so the v1 /tailor tests stay on v1 whatever the user's
    real profile/ holds (auto engine = v2 once facts are confirmed)."""
    monkeypatch.setenv("JOBCOPILOT_PROFILE_DIR", str(tmp_path / "no-profile"))
    monkeypatch.delenv(tailor_v2.ENGINE_ENV, raising=False)


@pytest.fixture
def example_profile(tmp_path, monkeypatch):
    d = tmp_path / "profile"
    shutil.copytree(EXAMPLE_PROFILE, d)
    monkeypatch.setenv("JOBCOPILOT_PROFILE_DIR", str(d))

    def build_content(base, facts, *, edit_plan=None):
        return {"base": base}

    def render(content, base, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "resume.tex").write_text("% fake\n", encoding="utf-8")
        (out_dir / "resume.pdf").write_bytes(b"%PDF-1.4 fake\n")
        return SimpleNamespace(tex_path=out_dir / "resume.tex", pdf_path=out_dir / "resume.pdf",
                               pages=1, text="", warnings=[])
    mod = SimpleNamespace(build_content=build_content, render=render, pick_base=lambda j: "backend_sde")
    monkeypatch.setattr(tailor_v2, "_render_mod", lambda: mod)
    return d


def _fake_plan(monkeypatch, rephrase=None):
    calls = []

    def fake(system, user, **_kw):
        calls.append(user)
        return {"order": [], "exclude": [], "rephrase": rephrase or {}, "gaps": []}, {"retries": 0}
    monkeypatch.setattr(llm, "complete_json", fake)
    return calls


def test_tailor_v2_refused_without_facts_is_409(client):
    key = seed_job()
    r = client.post(f"/api/jobs/{key}/tailor/v2", json={})
    assert r.status_code == 409
    assert r.json()["code"] == "no_confirmed_facts" and "Confirm" in r.json()["reasons"][0]
    assert client.post("/api/jobs/nope/tailor/v2", json={}).status_code == 404
    assert client.get(f"/api/jobs/{key}/tailor/v2/keywords").status_code == 409


def test_v1_tailor_routes_to_v2_on_request(client, example_profile, monkeypatch):
    key = seed_job()
    _fake_plan(monkeypatch)
    r = client.post(f"/api/jobs/{key}/tailor?v=2", json={})
    assert r.status_code == 200 and r.json()["engine"] == "v2"
    # auto: confirmed example facts -> v2 by default
    r = client.post(f"/api/jobs/{key}/tailor", json={})
    assert r.json()["engine"] == "v2"
    monkeypatch.setenv(tailor_v2.ENGINE_ENV, "v1")
    r = client.post(f"/api/jobs/{key}/tailor", json={})
    assert r.status_code == 400 and r.json()["detail"] == "no resume uploaded"   # v1 path


def test_tailor_v2_flow_keywords_tailor_approve_pdf(client, example_profile, monkeypatch):
    key = seed_job()
    kw = client.get(f"/api/jobs/{key}/tailor/v2/keywords").json()
    assert "Kubernetes" in kw["keywords"]["missing"]
    _fake_plan(monkeypatch, {"exp.acme.b1": B1 + " Cut costs by 40%."})
    r = client.post(f"/api/jobs/{key}/tailor/v2", json={"struck_keywords": ["Django"]})
    assert r.status_code == 200, r.text
    body = r.json()
    # the unsupported rephrase is reverted to the confirmed bullet (per-line fallback)
    assert body["status"] == "ok" and body["grounding"]["clean"]
    assert any(k.startswith("exp.acme.b1: kept original:") and "40%" in k for k in body["kept_original"])
    assert "Kubernetes" in body["missing_keywords"] and body["diff"]
    aid = body["artifact_id"]
    assert body["pdf_url"] == f"/api/artifacts/{aid}/pdf"
    pdf = client.get(body["pdf_url"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    arts = client.get(f"/api/jobs/{key}/artifacts").json()["artifacts"]
    assert [a["id"] for a in arts] == [aid] and arts[0]["status"] == "ok"

    # an artifact whose stored plan still prints an unsupported line (older version /
    # tampered row) must pass the server-side, per-token confirmation gate
    rf, bases, _ = tailor_v2._load(None)
    usable = tailor_v2.F.usable_facts(rf, body["base"])
    bad_plan = dict(body["edit_plan"], rephrase={"exp.acme.b1": B1 + " Cut costs by 40%."})
    lines = tailor_v2.apply_plan(usable, bases[body["base"]], bad_plan)
    store.update_artifact(aid, edit_plan=bad_plan, status="needs_review",
                          facts_hash=tailor_v2.facts_hash(usable, lines))

    r = client.post(f"/api/jobs/{key}/artifacts/{aid}/approve", json={})
    assert r.status_code == 409 and r.json()["unconfirmed"] == {"new_numbers": ["40%"]}
    r = client.post(f"/api/jobs/{key}/artifacts/{aid}/approve",
                    json={"confirmed_flags": {"new_numbers": ["40%"]}})
    assert r.status_code == 200, r.text
    assert store.get_job(key)["status"] == "ready_to_submit"
    assert store.get_job(key)["artifact_id"] == aid
    assert client.post(f"/api/jobs/{key}/artifacts/999/approve", json={}).status_code == 404


def test_v1_approve_routes_artifact_id_to_v2(client, example_profile, monkeypatch):
    key = seed_job()
    _fake_plan(monkeypatch)
    aid = client.post(f"/api/jobs/{key}/tailor/v2", json={}).json()["artifact_id"]
    r = approve(client, key, artifact_id=aid)
    assert r.status_code == 200 and r.json()["artifact_id"] == aid
    assert approve(client, key, artifact_id="x").status_code == 400


def test_tailor_v2_bad_body_is_400(client, example_profile, monkeypatch):
    key = seed_job()
    _fake_plan(monkeypatch)
    assert client.post(f"/api/jobs/{key}/tailor/v2", json={"approved_keywords": "go"}).status_code == 400
    assert client.post(f"/api/jobs/{key}/tailor/v2", json={"base": 3}).status_code == 400
    assert client.post(f"/api/jobs/{key}/tailor/v2", json={"base": "nope"}).status_code == 400


def test_artifact_pdf_is_confined_to_applications_dir(client, tmp_path):
    key = seed_job()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 secret\n")
    aid, _ = store.create_artifact(key, "backend_sde")
    store.update_artifact(aid, pdf_path=str(outside), status="ok")
    assert client.get(f"/api/artifacts/{aid}/pdf").status_code == 404
    sneaky = store.applications_dir() / ".." / "secret.pdf"
    store.update_artifact(aid, pdf_path=str(sneaky))
    assert client.get(f"/api/artifacts/{aid}/pdf").status_code == 404
    assert client.get("/api/artifacts/12345/pdf").status_code == 404
    assert client.get("/api/jobs/nope/artifacts").status_code == 404


def test_legacy_browse_hides_excluded_jobs_by_default(client):
    """The old Browse tab must not show jobs the eligibility rules excluded."""
    import sqlite3
    from jobcopilot import store
    db = store.resolve_db_path()
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO jobs(key, source, company, title, lane, excluded) VALUES "
                  "('t:sre', 'greenhouse', 'BigCo', 'Site Reliability Engineer', 'excluded', 1)")
        c.execute("INSERT INTO jobs(key, source, company, title, lane, excluded) VALUES "
                  "('t:sde', 'greenhouse', 'BigCo', 'Software Engineer', 'main', 0)")
    keys = {j["key"] for j in client.get("/api/jobs").json()["jobs"]}
    assert "t:sde" in keys and "t:sre" not in keys
    keys_all = {j["key"] for j in client.get("/api/jobs?include_excluded=true").json()["jobs"]}
    assert {"t:sde", "t:sre"} <= keys_all


# --- Phase 4: autofill routes (server/routers/autofill.py) ----------------------

from jobcopilot import demo  # noqa: E402
from jobcopilot.autofill import service as autofill_service  # noqa: E402

EXPECTED_ROUTES.update({
    # Phase 5 (server/routers/ui.py) + the built React UI (server/app.py)
    ("GET", "/assets/{rel:path}"), ("GET", "/{rel:path}"),
    ("GET", "/api/ui/config"), ("GET", "/api/autofill/sessions/{session_id}/screenshot"),
    ("GET", "/api/jobs/{key}/proof"), ("GET", "/api/jobs/{key}/autofill/sessions"),
    ("GET", "/api/artifacts/{artifact_id}/review"), ("POST", "/api/jobs/{key}/artifacts/{artifact_id}/edit"),
    ("POST", "/api/jobs/{key}/artifacts/{artifact_id}/reject"), ("GET", "/api/answers/unanswered"),
    ("POST", "/api/answers/save"), ("GET", "/api/facts"), ("POST", "/api/facts/{fact_id}/confirm"),
    ("GET", "/api/resumes"), ("GET", "/api/resumes/bases/{name}/source"),
    ("GET", "/api/tracker/export.csv"), ("GET", "/api/portals"), ("POST", "/api/portals/open"),
})
EXPECTED_ROUTES.update({
    ("POST", "/api/jobs/{key}/autofill"), ("GET", "/api/autofill/sessions/{session_id}"),
    ("GET", "/api/jobs/{key}/fill-plan"), ("POST", "/api/jobs/{key}/fill-plan/rebuild"),
    ("POST", "/api/autofill/sessions/{session_id}/handover"), ("GET", "/api/autofill/needs-you"),
})


def _seed_url(url: str, external_id: str) -> str:
    job = Job(source="greenhouse", company="Foo", company_sector="tech", external_id=external_id,
              title="Backend Engineer", location_text="Bengaluru", workplace_type="onsite",
              country_bucket="IN", url=url, description_text="Python")
    store.upsert_jobs([FilterResult(job=job, package_estimated=False, country_uncertain=False)])
    return job.key


def test_autofill_denylisted_is_400_with_link_out(client):
    url = "https://www.linkedin.com/jobs/view/99"
    key = _seed_url(url, "li1")
    r = client.post(f"/api/jobs/{key}/autofill")
    assert r.status_code == 400 and r.json()["code"] == "denylisted" and r.json()["link_out"] == url
    assert client.post(f"/api/jobs/{key}/fill-plan/rebuild").status_code == 400
    assert client.post("/api/jobs/nope/autofill").status_code == 404


def test_autofill_busy_409_and_session_routes(client):
    a = _seed_url("https://job-boards.greenhouse.io/foo/jobs/1", "a1")
    b = _seed_url("https://job-boards.greenhouse.io/foo/jobs/2", "b1")
    sid, _ = store.claim_autofill_session(a)
    store.update_autofill_session(sid, status="needs_you", outcome={"status": "needs_you", "reason": "CAPTCHA"})
    r = client.post(f"/api/jobs/{b}/autofill")
    assert r.status_code == 409 and r.json()["code"] == "busy" and r.json()["session_id"] == sid
    s = client.get(f"/api/autofill/sessions/{sid}").json()
    assert s["status"] == "needs_you" and s["open"] is True and s["live"] is False
    assert [x["id"] for x in client.get("/api/autofill/needs-you").json()["sessions"]] == [sid]
    assert client.post(f"/api/autofill/sessions/{sid}/handover").status_code == 409   # no window here
    assert client.get("/api/autofill/sessions/999").status_code == 404


def test_fill_plan_get_and_rebuild(client, monkeypatch):
    key = _seed_url("https://job-boards.greenhouse.io/foo/jobs/7", "p7")
    assert client.get(f"/api/jobs/{key}/fill-plan").status_code == 404
    monkeypatch.setattr(autofill_service, "fetch_greenhouse_questions", lambda b, j: {"questions": [
        {"label": "What is your favourite colour?", "required": False,
         "fields": [{"name": "question_1", "type": "input_text"}]}]})
    r = client.post(f"/api/jobs/{key}/fill-plan/rebuild")
    assert r.status_code == 200 and r.json()["unresolved_count"] == 2   # the question + no approved résumé
    assert client.get(f"/api/jobs/{key}/fill-plan").json()["plan"]["ats"] == "greenhouse"


def test_autofill_refused_in_demo_mode(client, monkeypatch):
    key = _seed_url("https://job-boards.greenhouse.io/foo/jobs/3", "d3")
    monkeypatch.setattr(demo, "enabled", lambda: True)
    assert client.post(f"/api/jobs/{key}/autofill").status_code == 403
    assert client.post(f"/api/jobs/{key}/fill-plan/rebuild").status_code == 403


# --------------------------------------------------------------------------- CSRF + DNS rebinding

def _live_session(monkeypatch, key: str) -> "autofill_service.LiveSession":
    sid, _ = store.claim_autofill_session(key)
    store.update_autofill_session(sid, status="ready_to_submit")
    live = autofill_service.LiveSession(sid, key, "greenhouse", None, autofill_service.settings())
    monkeypatch.setitem(autofill_service._LIVE, sid, live)
    return live


def test_cross_site_handover_and_fill_are_refused(client, monkeypatch):
    """The review's CSRF repro: a page on another origin POSTs hand-over / start-fill."""
    key = _seed_url("https://job-boards.greenhouse.io/foo/jobs/11", "c11")
    live = _live_session(monkeypatch, key)
    evil = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    for path in (f"/api/autofill/sessions/{live.id}/handover", f"/api/jobs/{key}/autofill",
                 f"/api/jobs/{key}/fill-plan/rebuild", f"/api/jobs/{key}/status"):
        r = client.post(path, headers=evil, json={"status": "applied"})
        assert r.status_code == 403 and "cross-origin" in r.json()["detail"], path
    assert live.commands.qsize() == 0                                  # the submit guard stayed up
    assert client.post(f"/api/autofill/sessions/{live.id}/handover",
                       headers={"Origin": "null"}).status_code == 403   # sandboxed iframe / file:
    # the app's own page (same Origin + X-JobCopilot) still works
    r = client.post(f"/api/autofill/sessions/{live.id}/handover")
    assert r.status_code == 200 and live.commands.qsize() == 1


def test_state_changes_need_origin_or_referer_and_the_custom_header(client):
    # a denylisted job: an accepted request ends in a quick 400 link-out, never a network call
    key = _seed_url("https://www.linkedin.com/jobs/view/12", "c12")
    path = f"/api/jobs/{key}/fill-plan/rebuild"
    assert client.post(path).status_code == 400                        # accepted (then refused: link-out)
    r = client.post(path, headers={"X-JobCopilot": "0"})
    assert r.status_code == 403 and "X-JobCopilot" in r.json()["detail"]
    client.headers.pop("Origin")
    try:
        r = client.post(path)                                          # no Origin, no Referer (curl-style)
        assert r.status_code == 403 and "Origin" in r.json()["detail"]
        r = client.post(path, headers={"Referer": "https://evil.example/x"})
        assert r.status_code == 403
        r = client.post(path, headers={"Referer": "http://testserver/"})
        assert r.status_code == 400 and r.json()["code"] == "denylisted"   # Referer fallback: own origin
        for method in ("put", "patch", "delete"):
            assert getattr(client, method)(path).status_code == 403
        assert client.get(f"/api/jobs/{key}").status_code == 200        # reads need neither
    finally:
        client.headers["Origin"] = "http://testserver"


def test_dns_rebinding_host_is_refused_everywhere(client, monkeypatch):
    """A rebound name (attacker.example -> 127.0.0.1) can't read plans or sessions."""
    key = _seed_url("https://job-boards.greenhouse.io/foo/jobs/13", "c13")
    bad = {"Host": "attacker.example", "Origin": "http://attacker.example"}
    for path in ("/", "/api/meta", f"/api/jobs/{key}/fill-plan", "/api/autofill/needs-you"):
        r = client.get(path, headers=bad)
        assert r.status_code == 400 and r.json()["detail"] == "unknown Host header", path
    assert client.post(f"/api/jobs/{key}/autofill", headers=bad).status_code == 400
    assert r.headers.get("content-security-policy")                   # refusals keep the headers
    for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "localhost"):
        assert client.get("/api/meta", headers={"Host": host}).status_code == 200, host
    # *.run.app only in the public demo (the Cloud Run deployment, docs/DEPLOY.md)
    run = {"Host": "job-copilot-demo-abc-uc.a.run.app"}
    assert client.get("/api/meta", headers=run).status_code == 400
    monkeypatch.setattr(demo, "enabled", lambda: True)
    assert client.get("/api/meta", headers=run).status_code == 200
    r = client.post(f"/api/jobs/{key}/status", json={"status": "applied"},
                    headers={**run, "Origin": "https://job-copilot-demo-abc-uc.a.run.app"})
    assert r.status_code != 403 or r.json().get("demo")                # the CSRF check passes on run.app


def test_allowed_hosts_env_adds_names(client, monkeypatch):
    monkeypatch.setenv("JOBCOPILOT_ALLOWED_HOSTS", "testserver, *.lan.example")
    assert client.get("/api/meta", headers={"Host": "mac.lan.example:8000"}).status_code == 200
    assert client.get("/api/meta", headers={"Host": "lan.example.evil.test"}).status_code == 400


def test_ui_sends_the_csrf_header():
    """One fetch wrapper (web/src/lib/api.ts) adds X-JobCopilot: 1 to every
    state-changing request, uploads included; nothing else calls fetch()."""
    api_ts = (WEB_SRC / "lib" / "api.ts").read_text(encoding="utf-8")
    assert 'headers["X-JobCopilot"] = "1"' in api_ts
    assert 'const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);' in api_ts
    for name, src in _web_sources().items():
        if name != "lib/api.ts":
            assert not re.search(r"\bfetch\(|XMLHttpRequest|sendBeacon", src), name


