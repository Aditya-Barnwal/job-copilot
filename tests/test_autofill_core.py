"""Autofill core: plan types, detectors, the submit guard, SafePage, executor,
generic adapter. Browser tests run headless against LOCAL fixture pages on
127.0.0.1 (never the internet) and skip cleanly when no browser exists.

The one place a test clicks Submit is test_handover_lets_the_human_submit,
which simulates the human's own mouse click AFTER handover()."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from jobcopilot.answers import Answer
from jobcopilot.autofill import detect, guard
from jobcopilot.autofill.adapters.base import PlanContext, adapter_for
from jobcopilot.autofill.generic import GenericAdapter, normalize
from jobcopilot.autofill.plan import FieldAction, FillPlan, Outcome

from autofill_helpers import FormServer

FIRST = "Zyxwvut"                      # distinctive values: grep-able in logs
EMAIL = "zyxwvut.q@example.com"


def stub_answer(question, *, field_type="text", options=None, required=None, **_):
    q = question.lower()
    if "first name" in q:
        return Answer(FIRST, "answers.yaml:personal.first_name", 1.0, False)
    if "email" in q:
        return Answer(EMAIL, "answers.yaml:personal.email", 1.0, False)
    if "country" in q:
        return Answer("India", "answers.yaml:personal.country", 0.95, False)
    if "sponsorship" in q:
        return Answer("No", "answers.yaml:work_auth.sponsorship", 0.95, False)
    if "why" in q:                     # open question: a draft, never auto-typed
        return Answer("Because reasons", "generated", 0.6, True, "draft for you")
    return Answer("", "none", 0.0, True, "no matching answer")


# --------------------------------------------------------------------------- no browser needed

def test_plan_roundtrip_and_repr_hides_values():
    a = FieldAction("#first", "First name", "fill", FIRST, "answers.yaml:x", 1.0, False, required=True)
    plan = FillPlan("job1", "generic", "http://x/apply", [[a]], [{"label": "Why", "reason": "open"}],
                    {"resume": "/tmp/r.pdf"})
    back = FillPlan.from_dict(plan.to_dict())
    assert back.to_dict() == plan.to_dict() and back.steps[0][0].required
    assert FIRST not in repr(a) and FIRST not in repr(plan)
    assert Outcome.from_dict(Outcome("ready", "ok", 1, 0, [], "#s", None, "step 1").to_dict()).status == "ready"
    with pytest.raises(ValueError):
        Outcome("submitted", "no such status")
    with pytest.raises(ValueError):
        FieldAction("#x", "X", "click", None, "none", 0, True)


def test_submit_like_and_next_like_rules():
    S = guard.is_submit_like
    assert S({"tag": "button", "type_attr": "button", "name_text": "Submit application"})
    assert S({"tag": "input", "type_attr": "submit", "value": "Continue"})
    assert S({"tag": "button", "type_attr": "", "in_form": True, "name_text": "Continue"})
    assert S({"tag": "button", "type_attr": "button", "aria_label": "Send"})
    assert S({"tag": "button", "type_attr": "button", "formaction": "/x", "name_text": "Next"})
    assert S({"tag": "a", "name_text": "Apply now"})
    assert not S({"tag": "button", "type_attr": "button", "name_text": "Next", "in_form": True})
    assert guard.is_next_like({"tag": "button", "type_attr": "button", "name_text": "Save & Continue →"})
    assert not guard.is_next_like({"tag": "button", "type_attr": "button", "name_text": "Continue to apply"})
    assert guard.is_consent_label("I agree to the Terms") and not guard.is_consent_label("First name")


def test_denylist_and_detectors():
    for u in ("https://www.linkedin.com/jobs/view/1", "https://in.indeed.com/x", "https://naukri.com",
              "https://www.glassdoor.co.in/x", "https://wellfound.com/x",
              "https://www.workatastartup.com/jobs/1", "https://news.ycombinator.com"):
        assert guard.is_denied(u), u
    assert not guard.is_denied("https://boards.greenhouse.io/acme/jobs/1")
    assert detect.honeypot({"name": "beecatcher"}) and detect.honeypot({"name": "website_url"})
    assert detect.honeypot({"tag": "input", "type": "text", "name": "x", "offscreen": True})
    assert not detect.honeypot({"tag": "input", "type": "file", "name": "resume", "hidden": True})
    assert not detect.honeypot({"tag": "input", "type": "text", "name": "first_name"})
    assert detect.confirmation_page("https://boards.greenhouse.io/a/jobs/1/confirmation", "", "greenhouse")
    assert detect.confirmation_page("https://jobs.lever.co/a/b/thanks", "", "lever")
    assert detect.confirmation_page("https://jobs.ashbyhq.com/a", "Application submitted! Thanks", "ashby")
    assert detect.confirmation_page(
        "https://x.wd5.myworkdayjobs.com/en-US/x/jobTasks/completed/application", "", "workday")
    assert not detect.confirmation_page("https://boards.greenhouse.io/a/jobs/1", "Apply for this job",
                                        "greenhouse")


def test_session_refuses_stealth_flags():
    from jobcopilot.autofill import session
    with pytest.raises(ValueError):
        session._check_args(["--disable-blink-features=AutomationControlled"])
    with pytest.raises(ValueError):
        session._check_args(["--user-agent=Mozilla/5.0 fake"])


def test_generic_plan_from_service_snapshot_shape():
    snap = {"source": "page", "steps": [[
        {"label": "First name", "name": "first_name", "type": "text", "required": True, "options": [],
         "hidden": False, "locator": "#first"},
        {"label": "Do you require visa sponsorship?", "name": "sponsor", "type": "radio",
         "required": False, "options": ["Yes", "No"], "hidden": False, "locator": '[name="sponsor"]'},
        {"label": "", "name": "beecatcher", "type": "text", "required": False, "options": [],
         "hidden": True, "locator": "#bee"},
        {"label": "Resume", "name": "resume", "type": "file", "required": True, "options": [],
         "hidden": False, "locator": "#resume"},
    ]]}
    plan = GenericAdapter().build_plan(snap, {"job": {"key": "j1"}, "files": {}, "min_confidence": 0.8,
                                               "answer": stub_answer})
    by = {a.label: a for a in plan.actions()}
    assert by["First name"].value == FIRST and not by["First name"].needs_user
    assert by["Do you require visa sponsorship?"].kind == "radio"
    assert by["Resume"].value is None and by["Resume"].needs_user          # no approved file
    assert any(u["reason"] == "honeypot" for u in plan.unresolved)
    assert adapter_for("https://example.org/careers/apply").name == "generic"


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


@pytest.fixture
def resume(tmp_path) -> str:
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-1.4\n% fixture\n")
    return str(p)


def _plan_for(safe, url, *, files=None):
    ad = GenericAdapter()
    safe.goto(url)
    return ad, ad.build_plan(ad.snapshot(safe), PlanContext(job_key="job-1", url=url, files=files or {},
                                                            answer_fn=stub_answer))


def _value(sess, sel):
    return sess.page.input_value(sel)


# --------------------------------------------------------------------------- (1) THE CORE SAFETY TEST

def test_hostile_page_never_submits(sess, server):
    server.requests.clear()
    safe = sess.safe_page()
    ad, plan = _plan_for(safe, server.url("generic_trap.html"))
    out = ad.run(safe, plan)
    sess.page.wait_for_timeout(1200)               # the page keeps trying (timer + delayed redirect)
    assert sess.page.evaluate("window.__attempts") > 0, "fixture never fired its auto-submit"
    assert _value(sess, "#first") == FIRST and _value(sess, "#email") == EMAIL
    assert server.posts() == [], server.posts()
    assert server.gets("/success.html") == 0
    assert "success" not in sess.page.url
    assert out.status in ("ready", "needs_you") and out.submit_selector == "#submit"
    assert sess.page.get_attribute("#submit", "data-jc-highlight") == "submit"
    assert any(b["kind"] in ("non-get", "unrequested-navigation", "post-navigation")
               for b in safe.guard.blocked)


# --------------------------------------------------------------------------- (2) refusals

@pytest.mark.parametrize("sel", ["#b-text", "#b-aria", "#b-formaction", "#b-apply", "#b-type",
                                 "#b-notype", "#b-input"])
def test_safepage_refuses_submit_like(sess, server, sel):
    server.requests.clear()
    safe = sess.safe_page()
    safe.goto(server.url("generic_buttons.html"))
    with pytest.raises(guard.SubmitGuardViolation):
        safe.next_step(sel)
    with pytest.raises((guard.SubmitGuardViolation, guard.SafePageError)):
        safe.click(sel)
    assert server.posts() == [] and server.gets("/success.html") == 0


def test_safepage_allows_only_harmless_clicks(sess, server):
    safe = sess.safe_page()
    safe.goto(server.url("generic_buttons.html"))
    safe.click("#b-add")
    assert sess.page.get_attribute("#b-add", "data-clicked") == "1"
    with pytest.raises(guard.SubmitGuardViolation):
        safe.next_step("#b-cancel")                 # not on the Next / Continue allowlist
    safe.next_step("#b-next")
    assert not hasattr(safe, "submit") and not hasattr(safe, "press") and not hasattr(safe, "page")


# --------------------------------------------------------------------------- (3) (6) (7) (8) full run

def test_full_run_consent_honeypot_uncertain_and_no_values_in_logs(sess, server, resume, caplog):
    caplog.set_level(logging.DEBUG)
    server.requests.clear()
    safe = sess.safe_page()
    ad, plan = _plan_for(safe, server.url("generic_form.html"), files={"resume": resume})
    out = ad.run(safe, plan)
    p = sess.page
    assert p.input_value("#first") == FIRST and p.input_value("#email") == EMAIL
    assert p.eval_on_selector("#country", "e => e.value") == "India"
    assert p.is_checked('input[name="sponsor"][value="no"]')
    assert p.eval_on_selector("#resume", "e => e.files.length") == 1
    assert not p.is_checked("#consent")                                  # (3)
    assert p.input_value("#hp1") == "" and p.input_value("#bee") == ""   # (6)
    assert p.input_value("#why") == ""                                   # (7)
    assert p.get_attribute("#why", "data-jc-highlight") == "needs_you"
    assert p.get_attribute("#consent", "data-jc-highlight") == "needs_you"
    assert p.get_attribute("#hp1", "data-jc-highlight") is None
    assert out.status == "ready", out
    assert out.submit_selector == "#submit" and out.filled == 5
    assert "Why do you want to work here?" in out.highlighted
    assert out.screenshot and Path(out.screenshot).is_file() and "/applications/job-1/" in out.screenshot
    assert server.posts() == []
    blob = caplog.text + repr(out.to_dict()) + repr(plan)
    assert FIRST not in blob and EMAIL not in blob                       # (8)
    assert "First name" in caplog.text                                   # labels are logged


def test_safepage_refuses_consent_and_honeypot_directly(sess, server):
    safe = sess.safe_page()
    safe.goto(server.url("generic_form.html"))
    with pytest.raises(guard.ConsentRefused):
        safe.check("#consent")
    with pytest.raises(guard.HoneypotRefused):
        safe.fill("#hp1", "x")
    with pytest.raises(guard.HoneypotRefused):
        safe.fill("#bee", "x")
    assert not sess.page.is_checked("#consent")


# --------------------------------------------------------------------------- (4) (5) CAPTCHA, login

def test_captcha_means_needs_you_and_nothing_typed(sess, server):
    safe = sess.safe_page()
    ad, plan = _plan_for(safe, server.url("generic_captcha.html"))
    out = ad.run(safe, plan)
    assert out.status == "needs_you" and "CAPTCHA" in out.reason and out.filled == 0
    assert _value(sess, "#first") == "" and _value(sess, "#email") == ""
    with pytest.raises(guard.Halted):
        safe.fill("#first", "x")


def test_captcha_appearing_mid_fill_stops_typing(sess, server):
    safe = sess.safe_page()
    ad, plan = _plan_for(safe, server.url("generic_captcha_late.html"))
    out = ad.run(safe, plan)
    assert out.status == "needs_you" and out.filled == 1
    assert _value(sess, "#first") == FIRST and _value(sess, "#email") == ""


def test_login_wall_means_needs_you(sess, server):
    safe = sess.safe_page()
    ad, plan = _plan_for(safe, server.url("generic_login.html"))
    assert any("login" in u["reason"] for u in plan.unresolved)
    out = ad.run(safe, plan)
    assert out.status == "needs_you" and "sign" in out.reason.lower()
    assert _value(sess, "#email") == "" and _value(sess, "#pw") == ""


# --------------------------------------------------------------------------- (9) denylist

def test_denylisted_url_is_link_out_only(sess):
    safe = sess.safe_page()
    with pytest.raises(guard.DeniedHost):
        safe.goto("https://www.linkedin.com/jobs/view/123")
    plan = FillPlan("job-x", "generic", "https://in.indeed.com/viewjob?jk=1", [[]])
    out = GenericAdapter().run(safe, plan)
    assert out.status == "blocked" and "link-out" in out.reason
    assert sess.page.url == "about:blank"


# --------------------------------------------------------------------------- multi-step

def test_multi_step_stops_on_review_page(sess, server):
    server.requests.clear()
    safe = sess.safe_page()
    safe.goto(server.url("generic_steps.html"))
    plan = FillPlan("job-2", "generic", server.url("generic_steps.html"), [
        [FieldAction("#first", "First name", "fill", FIRST, "answers.yaml:p", 1.0, False, required=True)],
        [FieldAction("#email", "Email", "fill", EMAIL, "answers.yaml:p", 1.0, False, required=True)],
    ])
    out = GenericAdapter().run(safe, plan)
    assert out.status == "ready", out
    assert out.step_reached == "step 2 of 2" and out.submit_selector == "#submit"
    assert _value(sess, "#email") == EMAIL and server.posts() == []


# --------------------------------------------------------------------------- (10) handover

def test_handover_lets_the_human_submit(sess, server, resume):
    server.requests.clear()
    safe = sess.safe_page()
    ad, plan = _plan_for(safe, server.url("generic_form.html"), files={"resume": resume})
    out = ad.run(safe, plan)
    assert out.status == "ready" and server.posts() == []
    sess.handover()
    with pytest.raises(guard.Halted):
        safe.fill("#first", "x")
    # --- SIMULATING THE HUMAN: their own physical click on Submit, after handover ---
    box = sess.page.locator(out.submit_selector).bounding_box()
    with sess.page.expect_navigation():
        sess.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    assert server.posts() == [("POST", "/submit")]
    assert sess.page.url.endswith("/success.html")
    assert detect.confirmation_page(sess.page.url, sess.page.inner_text("body"), "generic")


def test_persistent_profile_session_launches(tmp_path, server):
    """The real-use path (persistent profile, channel chrome), headless here. Runs in
    its own thread: one sync Playwright driver per thread."""
    import threading
    from jobcopilot.autofill import session
    result: dict = {}

    def run():
        try:
            s = session.launch(profile_dir=tmp_path / "profile", headless=True)
        except session.BrowserUnavailable:
            result["skip"] = True
            return
        try:
            with s:
                safe = s.safe_page()
                safe.goto(server.url("generic_form.html"))
                result["submit"] = safe.find_submit()
        except Exception as exc:  # noqa: BLE001
            result["error"] = repr(exc)

    t = threading.Thread(target=run)
    t.start()
    t.join(120)
    if result.get("skip"):
        pytest.skip("no browser available")
    assert result.get("submit") == "#submit", result
    assert (tmp_path / "profile").is_dir()
