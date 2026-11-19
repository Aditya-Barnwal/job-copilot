"""Reaching the application form from a posting's description page
(SafePage.open_application), the outcome rule (nothing filled is never
"ready"), and the Keka adapter.

Browser tests run headless against LOCAL fixture pages on 127.0.0.1 (never the
internet); the Keka fixtures are sanitized snapshots of real Keka pages. Nothing
here submits anything: every test also checks that no POST reached the server."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jobcopilot.answers import Answer
from jobcopilot.autofill import guard
from jobcopilot.autofill.adapters.base import PlanContext, adapter_for
from jobcopilot.autofill.adapters.keka import KekaAdapter
from jobcopilot.autofill.executor import apply_zero_fill_rule, run_plan
from jobcopilot.autofill.generic import GenericAdapter
from jobcopilot.autofill.guard import (DeniedHost, OffSite, SafePageError, SubmitGuardViolation,
                                       is_submit_like)
from jobcopilot.autofill.plan import NO_FORM_REASON, NOTHING_FILLED_REASON

from autofill_helpers import FormServer

FIRST = "Zyxwvut"
EMAIL = "zyxwvut.q@example.com"


def stub_answer(question, *, field_type="text", options=None, required=None, **_):
    q = question.lower()
    if "first name" in q:
        return Answer(FIRST, "answers.yaml:personal.first_name", 1.0, False)
    if "email" in q:
        return Answer(EMAIL, "answers.yaml:personal.email", 1.0, False)
    return Answer("", "none", 0.0, True, "no matching answer")


KEKA_ANSWERS = {
    "first name": FIRST, "last name": "Qwerty", "phone": "5550100001", "email": EMAIL,
    "gender": "Male", "years of experience": "2", "experience (in years)": "2", "months": "0",
    "current salary": "1200000", "expected salary": "1500000", "notice period": "30",
    "available to join": "30", "preferred location": "Bengaluru",
    "current location": "Bengaluru", "total experience": "2 years", "relevant experience": "2 years",
}


def keka_answer(question, *, field_type="text", options=None, required=None, **_):
    q = question.lower()
    for k, v in KEKA_ANSWERS.items():
        if k in q:
            return Answer(v, f"answers.yaml:{k}", 1.0, False)
    return Answer("", "none", 0.0, True, "no matching answer")


# --------------------------------------------------------------------------- browser fixtures

@pytest.fixture(scope="module")
def server():
    s = FormServer().start()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    pw = sync_playwright().start()
    b = None
    for kw in ({"channel": "chrome"}, {}):
        try:
            b = pw.chromium.launch(headless=True, **kw)
            break
        except Exception:  # noqa: BLE001
            continue
    if b is None:
        pw.stop()
        pytest.skip("no browser available")
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def sess(browser):
    from jobcopilot.autofill.session import BrowserSession, _context_kwargs
    s = BrowserSession(browser.new_context(**_context_kwargs()))
    yield s
    s.context.close()


def _ctx(url, answer=stub_answer, files=None):
    return PlanContext(job_key="job-1", url=url, files=files or {}, answer_fn=answer)


def _generic_run(sess, server, page, *, ctx=True):
    url = server.url(page)
    safe = sess.safe_page()
    safe.goto(url)
    ad = GenericAdapter()
    plan = ad.build_plan(ad.snapshot(safe), _ctx(url))
    return safe, run_plan(safe, plan, adapter=ad, ctx=_ctx(url) if ctx else None)


# --------------------------------------------------------------------------- the rule itself

def test_zero_fill_rule_is_never_ready():
    assert apply_zero_fill_rule("ready", "ok", 0, 0) == ("needs_you", NO_FORM_REASON)
    assert apply_zero_fill_rule("blocked", "x", 0, 0)[0] == "needs_you"        # no form: say so
    assert apply_zero_fill_rule("ready", "ok", 0, 3) == ("needs_you", NOTHING_FILLED_REASON)
    assert apply_zero_fill_rule("blocked", "missing", 0, 3) == ("blocked", "missing")
    assert apply_zero_fill_rule("ready", "ok", 1, 0) == ("ready", "ok")        # an attached file counts
    assert NO_FORM_REASON == ("no application form found on this page — open it from the posting's "
                              "Apply button")


def test_apply_words_are_still_submit_like_for_click():
    # open_application is the only way past "Apply": the ordinary guard is unchanged
    for name in ("Apply", "Apply now", "Apply for this job"):
        assert is_submit_like({"tag": "button", "type_attr": "button", "el_type": "button",
                               "name_text": name, "in_form": False})


# --------------------------------------------------------------------------- open_application: refusals

@pytest.mark.parametrize("page", ["apply_fake_submit.html", "apply_typeless_in_form.html",
                                  "apply_form_attr.html", "apply_formaction.html",
                                  "apply_input_submit.html"])
def test_fake_apply_that_would_submit_is_refused(sess, server, page):
    server.requests.clear()
    safe = sess.safe_page()
    safe.goto(server.url(page))
    with pytest.raises(SubmitGuardViolation):
        safe.open_application()
    sess.page.wait_for_timeout(300)
    assert server.posts() == [] and server.gets("/success.html") == 0
    assert sess.page.url.endswith(page)
    with pytest.raises(SafePageError, match="once per run"):      # and no second try
        safe.open_application()


def test_fake_apply_run_ends_needs_you_with_no_form_reason(sess, server):
    server.requests.clear()
    safe, out = _generic_run(sess, server, "apply_fake_submit.html")
    assert out.status == "needs_you" and out.reason.startswith(NO_FORM_REASON)
    assert out.filled == 0 and server.posts() == []
    assert sess.page.get_attribute("#fake", "data-jc-highlight") != "clicked"


def test_refuses_on_a_page_that_already_has_a_form(sess, server):
    safe = sess.safe_page()
    safe.goto(server.url("generic_form.html"))
    with pytest.raises(SafePageError, match="already has form fields"):
        safe.open_application()


def test_refuses_two_different_apply_controls(sess, server):
    server.requests.clear()
    safe = sess.safe_page()
    safe.goto(server.url("apply_two.html"))
    with pytest.raises(SafePageError, match="found 2"):
        safe.open_application()
    assert sess.page.url.endswith("apply_two.html") and server.posts() == []


def test_refuses_an_apply_link_off_site_or_to_a_denied_host(sess, server):
    safe = sess.safe_page()
    safe.goto(server.url("apply_offsite.html"))
    with pytest.raises(OffSite):
        safe.open_application()
    assert sess.page.url.endswith("apply_offsite.html")
    with pytest.raises(SafePageError, match="once per run"):
        safe.open_application()


def test_refuses_a_denied_apply_link(browser, server):
    from jobcopilot.autofill.session import BrowserSession, _context_kwargs
    s = BrowserSession(browser.new_context(**_context_kwargs()))
    try:
        safe = s.safe_page()
        safe.goto(server.url("apply_denied.html"))
        with pytest.raises(DeniedHost):
            safe.open_application()
        assert s.page.url.endswith("apply_denied.html")
    finally:
        s.context.close()


def test_no_apply_control_means_no_form_outcome(sess, server):
    server.requests.clear()
    safe, out = _generic_run(sess, server, "apply_none.html")
    assert out.status == "needs_you" and out.reason.startswith(NO_FORM_REASON)
    assert out.filled == 0 and server.posts() == []


# --------------------------------------------------------------------------- open_application: works, guard on

def test_apply_link_to_a_page_that_auto_posts_gets_zero_posts(sess, server):
    server.requests.clear()
    safe = sess.safe_page()
    safe.goto(server.url("apply_link.html"))
    landed = safe.open_application()              # two links to the same URL count once
    assert landed.endswith("apply_autopost.html")
    sess.page.wait_for_timeout(800)
    assert server.posts() == [] and server.gets("/success.html") == 0
    assert sess.page.url.endswith("apply_autopost.html")
    kinds = {b["kind"] for b in safe.guard.blocked}
    assert kinds & {"non-get", "post-navigation", "form-submission"}, kinds


def test_modal_apply_opens_the_form_and_fills_it_without_submitting(sess, server):
    server.requests.clear()
    safe, out = _generic_run(sess, server, "apply_modal.html")
    sess.page.wait_for_timeout(600)               # the page's own submit attempts fire
    assert sess.page.evaluate("window.__attempts") > 0
    assert sess.page.input_value("#first") == FIRST and sess.page.input_value("#email") == EMAIL
    assert out.status == "ready" and out.filled == 2 and out.submit_selector == "#submit"
    assert sess.page.get_attribute("#submit", "data-jc-highlight") == "submit"
    assert server.posts() == [] and server.gets("/success.html") == 0


def test_modal_apply_without_a_planning_context_is_not_ready(sess, server):
    server.requests.clear()
    safe, out = _generic_run(sess, server, "apply_modal.html", ctx=False)
    assert out.status != "ready" and out.filled == 0
    assert server.posts() == []


class _RedirServer:
    """/start.html has an Apply link to /go, which 302s to a do-not-automate host."""

    PAGE = b"""<!doctype html><html><body><h1>Role</h1><a href="/go">Apply now</a></body></html>"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        srv = self
        start_page = self.PAGE

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                srv.requests.append(("GET", self.path))
                if self.path.startswith("/go"):
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{srv.port}/landed.html")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = start_page if self.path.startswith("/start") else \
                    b"<!doctype html><form method=post action=/x><input name=e></form>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                srv.requests.append(("POST", self.path))
                self.send_response(204)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def test_apply_link_redirecting_to_a_denied_host_halts(sess, monkeypatch):
    srv = _RedirServer()
    try:
        real = guard.denied_hosts
        monkeypatch.setattr(guard, "denied_hosts", lambda: real() + ("localhost",))
        safe = sess.safe_page()
        safe.goto(f"http://127.0.0.1:{srv.port}/start.html")
        with pytest.raises(DeniedHost):
            safe.open_application()
        assert safe.halted and "do-not-automate" in safe.halted
        assert [r for r in srv.requests if r[0] != "GET"] == []
        with pytest.raises(Exception):
            safe.fill("input", "x")                  # never writes again
    finally:
        srv.httpd.shutdown()
        srv.httpd.server_close()


# --------------------------------------------------------------------------- Keka

def test_keka_adapter_is_picked_for_keka_hosts():
    assert type(adapter_for("https://acme.keka.com/careers/applyjob/123")).__name__ == "KekaAdapter"
    assert type(adapter_for("https://acme.keka.com/careers/jobdetails/123")).__name__ == "KekaAdapter"


def test_keka_from_the_description_page_fills_and_leaves_captcha_resume_consent(sess, server, tmp_path):
    server.requests.clear()
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% fixture\n")
    ad = KekaAdapter()
    safe = sess.safe_page(**ad.safe_page_kwargs())
    url = server.url("keka_job.html")
    safe.goto(url)
    ctx = PlanContext(job_key="keka-1", url=url, files={"resume": str(pdf)}, answer_fn=keka_answer)
    plan = ad.build_plan(ad.snapshot(safe), ctx)
    assert plan.ats == "keka" and not plan.actions()          # a description page: nothing to plan
    out = ad.run(safe, plan, ctx)
    sess.page.wait_for_timeout(300)
    page = sess.page
    assert page.url.endswith("keka_apply.html")              # opened through "Apply for this job"
    assert page.input_value("#firstName") == FIRST and page.input_value("#lastName") == "Qwerty"
    assert page.input_value("#email") == EMAIL
    assert page.input_value("[id='mobilePhone.number']") == "5550100001"
    assert page.input_value("#currentLocation") == "Bengaluru"
    assert page.input_value("#captcha") == ""                 # the person types the CAPTCHA
    assert not page.is_checked("#candidateConsent")           # consent is never ticked
    assert page.evaluate("Array.from(document.querySelectorAll('input[type=file]'))"
                         ".every(i => i.files.length === 0)")  # Keka uploads at once: left for you
    assert out.status == "needs_you" and out.filled >= 8
    assert "CAPTCHA" in out.reason and "résumé" in out.reason
    assert out.submit_selector == "#jobApplicationSubmitButton"
    assert page.get_attribute("#jobApplicationSubmitButton", "data-jc-highlight") == "submit"
    assert server.posts() == [] and server.gets("/success.html") == 0


def test_keka_missing_answers_block_and_still_name_the_captcha(sess, server):
    server.requests.clear()
    ad = KekaAdapter()
    safe = sess.safe_page(**ad.safe_page_kwargs())
    url = server.url("keka_apply.html")
    safe.goto(url)
    ctx = PlanContext(job_key="keka-2", url=url, answer_fn=stub_answer)
    out = ad.run(safe, ad.build_plan(ad.snapshot(safe), ctx), ctx)
    assert out.status == "blocked" and "Last Name" in out.reason and "CAPTCHA" in out.reason
    assert sess.page.input_value("#firstName") == FIRST and server.posts() == []


def test_keka_otp_step_is_needs_you(sess, server):
    ad = KekaAdapter()
    safe = sess.safe_page(**ad.safe_page_kwargs())
    url = server.url("keka_otp.html")
    safe.goto(url)
    ctx = PlanContext(job_key="keka-3", url=url, answer_fn=stub_answer)
    out = ad.run(safe, ad.build_plan(ad.snapshot(safe), ctx), ctx)
    assert out.status == "needs_you" and "OTP" in out.reason and out.filled == 0
    assert sess.page.input_value("#email") == ""


def test_keka_plan_from_the_sanitized_form(sess, server):
    ad = KekaAdapter()
    safe = sess.safe_page(**ad.safe_page_kwargs())
    url = server.url("keka_apply.html")
    safe.goto(url)
    plan = ad.build_plan(ad.snapshot(safe), PlanContext(job_key="k", url=url, answer_fn=keka_answer))
    by = {a.label: a for a in plan.actions()}
    assert by["First Name"].value == FIRST and by["Email"].value == EMAIL
    assert by["Captcha"].source == "yours" and by["Captcha"].value is None
    assert by["Expected Date of Joining"].source == "yours"
    assert not any(a.value for a in plan.actions() if "Consent" in a.locator or "consent" in a.label.lower())
    assert all(a.kind != "upload" or a.value is None for a in plan.actions())
    assert by["Gender"].value == "Male" and by["Preferred location"].value == "Bengaluru"
    labels = [a.label for a in plan.actions()]
    assert not any(x in labels for x in ("Keka", "Hyderabad", "Years", "Months"))
