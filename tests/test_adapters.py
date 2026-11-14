"""Per-ATS adapters (Greenhouse, Lever, Ashby, Workday) against LOCAL fixture
forms served on 127.0.0.1 — never the internet. Browser tests run headless
Chrome (channel="chrome") or Playwright's Chromium, and skip cleanly when no
browser is available (CI).

Every browser test asserts the non-negotiables: known fields filled from
answers.yaml (fake profile.example data), résumé attached, unknown custom
question left EMPTY and HIGHLIGHTED, consent never ticked, honeypot untouched,
the single Submit control highlighted and never clicked, and the server
received ZERO final-submit POSTs.
"""
from __future__ import annotations

import http.server
import re
import threading
from pathlib import Path

import pytest

from jobcopilot import answers as A
from jobcopilot.autofill import detect
from jobcopilot.autofill.adapters import adapter_for, load_all
from jobcopilot.autofill.adapters.ashby import AshbyAdapter
from jobcopilot.autofill.adapters.base import PlanContext
from jobcopilot.autofill.adapters.greenhouse import GreenhouseAdapter, load_spec
from jobcopilot.autofill.adapters.lever import LeverAdapter
from jobcopilot.autofill.adapters.workday import WorkdayAdapter

ROOT = Path(__file__).resolve().parent.parent
FORMS = ROOT / "tests" / "fixtures" / "forms"
EXAMPLE_ANSWERS = ROOT / "profile.example" / "answers.yaml"
LEVER_PATH = "/examplecorp/0f1e2d3c-aaaa-4bbb-8ccc-000000000001/apply"
ROUTES = {"/greenhouse.html": "greenhouse.html", LEVER_PATH: "lever.html",
          "/ashby.html": "ashby.html", "/workday_apply.html": "workday_apply.html",
          "/workday_signin.html": "workday_signin.html"}
UNKNOWN_Q = "Which internal tool would you rebuild first, and why?"


# --------------------------------------------------------------------------- local server

class _Server:
    def __init__(self) -> None:
        self.posts: list[str] = []          # paths of every non-GET request (like tests/autofill_helpers.py,
                                            # plus a route for Lever's POST-to-/apply URL)
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):      # quiet
                pass

            def do_GET(self):
                name = ROUTES.get(self.path.split("?")[0])
                if not name:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = (FORMS / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                outer.posts.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            do_PUT = do_PATCH = do_DELETE = do_POST     # every non-GET is counted

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def server():
    s = _Server()
    yield s
    s.close()


@pytest.fixture(scope="module")
def browser():
    sync = pytest.importorskip("playwright.sync_api")
    pw = sync.sync_playwright().start()
    b = None
    for kw in ({"channel": "chrome"}, {}):
        try:
            b = pw.chromium.launch(headless=True, **kw)
            break
        except Exception:  # noqa: BLE001
            continue
    if b is None:
        pw.stop()
        pytest.skip("no Chrome / Chromium available")
    yield b
    b.close()
    pw.stop()


@pytest.fixture()
def page(browser):
    ctx = browser.new_context()
    p = ctx.new_page()
    yield p
    ctx.close()


@pytest.fixture()
def bank():
    return A.load(EXAMPLE_ANSWERS)


@pytest.fixture()
def ctx(tmp_path, bank):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% fixture\n")
    return PlanContext(job_key="test-job", job={"key": "test-job", "company": "Examplecorp",
                                                 "title": "Software Engineer", "location": "Bengaluru, India"},
                       files={"resume": str(pdf)}, answers=bank, facts=[])


def _safe(page, adapter):
    from jobcopilot.autofill.safepage import SafePage
    return SafePage(page, **adapter.safe_page_kwargs())


def _value(page, sel):
    return page.eval_on_selector(sel, "el => el.value")


def _hl(page, sel):
    return page.eval_on_selector(sel, "el => el.getAttribute('data-jc-highlight')")


def _files(page, sel):
    return page.eval_on_selector(sel, "el => el.files.length")


def _checked_any(page, sel):
    return page.eval_on_selector_all(sel, "els => els.some(e => e.checked)")


# --------------------------------------------------------------------------- pure (no browser)

@pytest.mark.parametrize("name", ["greenhouse", "lever", "ashby", "workday"])
def test_specs_load_and_patterns_compile(name):
    spec = load_spec(name)
    assert spec["hosts"] and spec["submit"] and spec["submit_patterns"] and spec["confirmation"]
    for p in spec["submit_patterns"]:
        assert p["method"] in ("POST", "PUT")
        re.compile(p["url_regex"])


def test_registry_and_matching():
    names = {c.name for c in load_all()}
    assert {"greenhouse", "lever", "ashby", "workday"} <= names
    assert isinstance(adapter_for("https://job-boards.greenhouse.io/acme/jobs/123"), GreenhouseAdapter)
    assert isinstance(adapter_for("https://boards.greenhouse.io/embed/job_app?for=acme&token=1"), GreenhouseAdapter)
    assert isinstance(adapter_for("https://jobs.lever.co/acme/0f1e2d3c-aaaa/apply"), LeverAdapter)
    assert isinstance(adapter_for("https://jobs.ashbyhq.com/acme/x/application"), AshbyAdapter)
    assert isinstance(adapter_for("https://acme.wd5.myworkdayjobs.com/en-US/ext/job/X_R1"), WorkdayAdapter)
    assert not isinstance(adapter_for("https://www.linkedin.com/jobs/view/1"),
                          (GreenhouseAdapter, LeverAdapter, AshbyAdapter, WorkdayAdapter))


@pytest.mark.parametrize("cls,url,submit", [
    (GreenhouseAdapter, "https://job-boards.greenhouse.io/acme/jobs/4001", True),
    (GreenhouseAdapter, "https://boards.greenhouse.io/embed/acme/jobs/4001", True),
    (GreenhouseAdapter, "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4001", True),
    (LeverAdapter, "https://jobs.lever.co/acme/0f1e2d3c-aaaa-4bbb-8ccc-000000000001/apply", True),
    (LeverAdapter, "https://jobs.lever.co/acme/0f1e2d3c-aaaa-4bbb-8ccc-000000000001/apply/resume", False),
    (AshbyAdapter, "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction", True),
    (AshbyAdapter, "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting", False),
    (WorkdayAdapter, "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Ext/job/R1/apply/submit", True),
    (WorkdayAdapter, "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Ext/job/R1/apply/step?s=1", False),
])
def test_submit_patterns_hit_final_submit_only(cls, url, submit):
    hit = any(re.search(p, url, re.I) for p in cls.submit_patterns)
    assert hit is submit


def test_workday_step_saves_allowed_but_submit_never():
    save = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Ext/job/R1/apply/step"
    sub = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Ext/job/R1/apply/submit"
    assert any(re.search(p, save) for p in WorkdayAdapter.allow_post_patterns)
    assert any(re.search(p, sub) for p in WorkdayAdapter.submit_patterns)   # checked first by the guard


@pytest.mark.parametrize("cls,url,text,want", [
    (GreenhouseAdapter, "https://job-boards.greenhouse.io/embed/job_app/confirmation?for=a&token=1", "", True),
    (LeverAdapter, "https://jobs.lever.co/acme/abc/thanks", "", True),
    (AshbyAdapter, "https://jobs.ashbyhq.com/acme/x/application", "Thanks for applying! Your application was successfully submitted.", True),
    (WorkdayAdapter, "https://acme.wd5.myworkdayjobs.com/en-US/ext/job/x/apply", "Application Submitted", True),
    (LeverAdapter, "https://jobs.lever.co/acme/abc/apply", "Submit your application", False),
])
def test_confirmation_markers(cls, url, text, want):
    assert cls().confirmed(url, text) is want


def test_plan_values_only_from_approved_answers(ctx):
    snap = {"url": "https://job-boards.greenhouse.io/x/jobs/1", "steps": [[
        {"label": "First Name", "name": "first_name", "type": "text", "required": True,
         "options": [], "hidden": False, "locator": '[id="first_name"]'},
        {"label": UNKNOWN_Q, "name": "question_1", "type": "textarea", "required": True,
         "options": [], "hidden": False, "locator": '[id="question_1"]'},
        {"label": "Why do you want to work here?", "name": "question_2", "type": "textarea",
         "required": False, "options": [], "hidden": False, "locator": '[id="question_2"]'},
        {"label": "I agree to the privacy policy", "name": "gdpr", "type": "checkbox",
         "required": True, "options": [], "hidden": False, "locator": '[id="gdpr"]'},
        {"label": "Current location", "name": "loc", "type": "text", "required": True,
         "options": [], "hidden": False, "locator": '[id="loc"]'},          # proposed block in the example
        {"label": "Website", "name": "website", "type": "text", "required": False,
         "options": [], "hidden": True, "locator": '[name="website"]'},     # hidden text input: honeypot
        {"label": "Resume/CV", "name": "resume", "type": "file", "required": True,
         "options": [], "hidden": False, "locator": '[id="resume"]'},
    ]]}
    plan = GreenhouseAdapter().build_plan(snap, ctx)
    acts = {a.label: a for a in plan.actions()}
    assert acts["First Name"].value and not acts["First Name"].needs_user
    assert acts["First Name"].source.startswith("answers.yaml:")
    for lbl in (UNKNOWN_Q, "Why do you want to work here?", "I agree to the privacy policy", "Current location"):
        assert acts[lbl].value is None and acts[lbl].needs_user, lbl
    assert acts["I agree to the privacy policy"].source == "consent"
    assert "Website" not in acts                                  # honeypots are never planned
    assert acts["Resume/CV"].kind == "upload" and acts["Resume/CV"].value == "resume"
    assert {u["label"] for u in plan.unresolved} >= {UNKNOWN_Q, "Current location"}
    assert "alex" not in repr(plan).lower()


def test_lever_snapshot_reads_card_questions():
    html = (FORMS / "lever.html").read_text(encoding="utf-8")
    snap = LeverAdapter.snapshot_from_html(html, "https://jobs.lever.co" + LEVER_PATH)
    by = {f["label"]: f for f in snap["steps"][0]}
    q = "Are you legally authorized to work in the country for which you are applying?"
    assert by[q]["type"] == "radio" and by[q]["options"] == ["Yes", "No"]
    assert by[UNKNOWN_Q]["type"] == "textarea"
    assert by["Full name"]["locator"] == '[data-qa="name-input"]'
    assert any(f["type"] == "file" for f in snap["steps"][0])
    assert not any("baseTemplate" in f["name"] for f in snap["steps"][0])


def test_workday_plan_without_page(ctx):
    plan = WorkdayAdapter().build_plan(None, ctx)
    assert len(plan.steps) == 6
    first = {a.locator: a for a in plan.steps[0]}
    assert first['[data-automation-id="legalNameSection_firstName"]'].value
    assert first['[data-automation-id="phone-device-type"]'].value is None   # never guessed
    assert not any("beecatcher" in a.locator for a in plan.actions())
    assert any(a.kind == "upload" and a.value == "resume" for a in plan.steps[1])


# --------------------------------------------------------------------------- browser

def test_greenhouse_fills_and_stops_before_submit(page, server, ctx, bank, caplog):
    caplog.set_level("DEBUG")
    ad = GreenhouseAdapter()
    safe = _safe(page, ad)
    safe.goto(server.base + "/greenhouse.html")
    plan = ad.build_plan(ad.snapshot(safe), ctx)
    out = ad.run(safe, plan)

    assert _value(page, "#first_name") == bank.get("personal.first_name")[1]
    assert _value(page, "#last_name") == bank.get("personal.last_name")[1]
    assert _value(page, "#email") == bank.get("personal.email")[1]
    assert _value(page, "#school--0") == "Example State University"       # react-select style combobox
    assert _files(page, "#resume") == 1
    assert _value(page, "#question_1004") == "" and _hl(page, "#question_1004") == "needs_you"
    assert UNKNOWN_Q in out.highlighted
    assert not page.is_checked("#gdpr_consent")
    assert out.submit_selector == '#application-form button[type="submit"]'
    assert _hl(page, out.submit_selector) == "submit"
    assert out.status in ("ready", "blocked") and out.filled >= 5
    assert out.screenshot and Path(out.screenshot).is_file()
    # the page "invites" a submit: scripted requestSubmit / submit are cancelled by the guard
    page.evaluate("() => { const f = document.getElementById('application-form'); f.requestSubmit(); f.submit(); }")
    page.wait_for_timeout(300)
    assert server.posts == []
    assert "/confirmation" not in page.url
    assert bank.get("personal.email")[1] not in caplog.text          # labels + sources only, never values


def test_lever_fills_cards_and_never_clicks_submit(page, server, ctx, bank):
    ad = LeverAdapter()
    safe = _safe(page, ad)
    safe.goto(server.base + LEVER_PATH)
    html = (FORMS / "lever.html").read_text(encoding="utf-8")
    plan = ad.build_plan(ad.snapshot_from_html(html, server.base + LEVER_PATH), ctx)
    out = ad.run(safe, plan)

    assert out.status != "needs_you", out.reason          # the invisible hCaptcha isn't a challenge
    assert _value(page, '[data-qa="name-input"]') == bank.get("personal.full_name")[1]
    assert _value(page, '[data-qa="email-input"]') == bank.get("personal.email")[1]
    assert _value(page, '[name="urls[GitHub]"]') == bank.get("personal.links.github")[1]
    assert _files(page, '[data-qa="input-resume"]') == 1
    unknown = '[name="cards[33333333-aaaa-4bbb-8ccc-000000000003][field0]"]'
    assert _value(page, unknown) == "" and _hl(page, unknown) == "needs_you"
    assert _value(page, '[name="urls[Twitter]"]') == ""                 # not in answers.yaml
    consent = '[name="cards[44444444-aaaa-4bbb-8ccc-000000000004][field0]"]'
    assert not _checked_any(page, consent)                              # AI-notetaker consent radio
    sponsor = '[name="cards[11111111-aaaa-4bbb-8ccc-000000000001][field1]"]'
    assert _checked_any(page, sponsor) or "Will you now or in the future require sponsorship" in " ".join(out.highlighted)
    assert out.submit_selector == '[data-qa="btn-submit"]'
    assert _hl(page, out.submit_selector) == "submit"
    page.wait_for_timeout(200)
    assert server.posts == []


def test_ashby_fills_by_label_and_stops(page, server, ctx, bank):
    ad = AshbyAdapter()
    safe = _safe(page, ad)
    safe.goto(server.base + "/ashby.html")
    plan = ad.build_plan(ad.snapshot(safe), ctx)
    out = ad.run(safe, plan)

    assert _value(page, "#_systemfield_name") == bank.get("personal.full_name")[1]
    assert _value(page, "#_systemfield_email") == bank.get("personal.email")[1]
    assert _files(page, "#_systemfield_resume") == 1
    unknown = '[id="5f1c0a52-0004-4c4e-9a1d-000000000004"]'
    assert _value(page, unknown) == "" and _hl(page, unknown) == "needs_you"
    assert not page.is_checked("#_systemfield_data_consent")
    assert out.submit_selector == "button.ashby-application-form-submit-button"
    assert _hl(page, out.submit_selector) == "submit"
    assert page.locator(".ashby-application-form-success-container").count() == 0
    assert server.posts == []


def test_workday_walks_steps_to_review_and_stops(page, server, ctx, bank):
    ad = WorkdayAdapter()
    safe = _safe(page, ad)
    safe.goto(server.base + "/workday_apply.html")
    plan = ad.build_plan(ad.snapshot(safe), ctx)
    out = ad.run(safe, plan, ctx)

    assert out.step_reached == "Review", out.reason
    assert page.locator('[data-step="Review"].active').count() == 1
    assert _value(page, '[data-automation-id="legalNameSection_firstName"]') == bank.get("personal.first_name")[1]
    assert _value(page, '[data-automation-id="email"]') == bank.get("personal.email")[1]
    assert _files(page, '[data-automation-id="file-upload-input-ref"]') == 1
    # honeypot: never typed, never highlighted
    assert _value(page, '[data-automation-id="beecatcher"]') == ""
    assert _hl(page, '[data-automation-id="beecatcher"]') is None
    assert _value(page, '[data-automation-id="phone-device-type"]') == ""   # not in answers: left for you
    assert _value(page, "#primaryQuestionnaire--q3") == ""
    assert _hl(page, "#primaryQuestionnaire--q3") == "needs_you"
    assert not page.is_checked('[data-automation-id="agreementCheckbox"]')
    sub = '[data-automation-id="bottom-navigation-next-button"]'
    assert out.submit_selector == sub and page.inner_text(sub) == "Submit"
    assert _hl(page, sub) == "submit"
    assert not [p for p in server.posts if "submit" in p]
    assert len([p for p in server.posts if "/apply/step" in p]) == 5      # the five step saves
    assert out.status in ("ready", "blocked")


def test_workday_validation_error_pauses(page, server, ctx):
    ad = WorkdayAdapter()
    safe = _safe(page, ad)
    safe.goto(server.base + "/workday_apply.html?strict=1")
    out = ad.run(safe, ad.build_plan(None, ctx), ctx)
    assert out.status == "needs_you" and out.step_reached == "Voluntary Disclosures"
    assert not page.is_checked('[data-automation-id="agreementCheckbox"]')
    assert not [p for p in server.posts if "submit" in p]


def test_workday_sign_in_wall_is_needs_you(page, server, ctx):
    ad = WorkdayAdapter()
    safe = _safe(page, ad)
    safe.goto(server.base + "/workday_signin.html")
    out = ad.run(safe, ad.build_plan(None, ctx), ctx)
    assert out.status == "needs_you" and out.filled == 0
    assert _value(page, "#input-email") == "" and _value(page, "#input-password") == ""
    assert server.posts == []
    with pytest.raises(Exception):
        safe.fill("#input-email", "x")                 # halted: nothing is typed after the pause


def test_captcha_page_is_needs_you(page, server, ctx):
    ad = AshbyAdapter()
    safe = _safe(page, ad)
    safe.goto(server.base + "/ashby.html")
    page.evaluate("() => { const d = document.createElement('div'); d.className = 'h-captcha';"
                  " d.style.cssText = 'width:300px;height:80px';"
                  " d.innerHTML = '<iframe title=\"hCaptcha checkbox\" style=\"width:300px;height:78px\"></iframe>';"
                  " document.body.appendChild(d); }")
    out = ad.run(safe, ad.build_plan(ad.snapshot(safe), ctx))
    assert out.status == "needs_you" and out.filled == 0
    assert _value(page, "#_systemfield_name") == ""
    assert detect.captcha_present  # noqa: B018 - the detector is K's; the adapter only defers to it


def test_ashby_upload_allowed_submit_never():
    up = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiCreateFileUploadHandle"
    sub = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction"
    assert any(re.search(p, up) for p in AshbyAdapter.allow_post_patterns)
    assert not any(re.search(p, up) for p in AshbyAdapter.submit_patterns)
    assert not any(re.search(p, sub) for p in AshbyAdapter.allow_post_patterns)
    assert any(re.search(p, sub) for p in AshbyAdapter.submit_patterns)
