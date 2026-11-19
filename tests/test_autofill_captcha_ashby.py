"""Only a CAPTCHA the person can see pauses a fill; Ashby's page may make its
exact-named GraphQL READS while filling, never a submit (docs/DESIGN.md §12).

Browser tests run headless against LOCAL fixture pages on 127.0.0.1 (never the
internet). Challenge frames are served locally under the providers' URL shapes
(/newassets.hcaptcha/..., /recaptcha/api2/anchor...). Nothing here interacts
with a challenge frame, and every test checks what reached the server."""
from __future__ import annotations

import re

import pytest

from jobcopilot.answers import Answer
from jobcopilot.autofill import detect, guard as G
from jobcopilot.autofill.adapters import greenhouse as gh
from jobcopilot.autofill.adapters.ashby import AshbyAdapter
from jobcopilot.autofill.adapters.base import PlanContext
from jobcopilot.autofill.adapters.lever import LeverAdapter
from jobcopilot.autofill.executor import run_plan
from jobcopilot.autofill.generic import GenericAdapter

from autofill_helpers import FormServer

NAME, FIRST, EMAIL = "Alex Example", "Alex", "alex@example.com"


def stub_answer(question, *, field_type="text", options=None, required=None, **_):
    q = question.lower()
    if "full name" in q or q.strip() in ("name",):
        return Answer(NAME, "answers.yaml:personal.full_name", 1.0, False)
    if "first name" in q:
        return Answer(FIRST, "answers.yaml:personal.first_name", 1.0, False)
    if "email" in q:
        return Answer(EMAIL, "answers.yaml:personal.email", 1.0, False)
    return Answer("", "none", 0.0, True, "no matching answer")


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


def _val(sess, sel):
    return sess.page.input_value(sel)


def _settle(sess):
    sess.page.wait_for_timeout(300)        # let the (local, 404) challenge frames attach


# --------------------------------------------------------------------------- 1. CAPTCHA: visible only

def test_lever_invisible_hcaptcha_is_not_a_pause_and_the_form_fills(sess, server):
    server.requests.clear()
    ad = LeverAdapter()
    safe = sess.safe_page(**ad.safe_page_kwargs())
    url = server.url("lever_invisible_hcaptcha.html")
    safe.goto(url)
    _settle(sess)
    assert any("newassets.hcaptcha" in f.url for f in sess.page.frames)      # the frames are there
    assert safe.captcha() is None                                            # ...but not seen
    ctx = PlanContext(job_key="lv", url=url, answer_fn=stub_answer)
    out = ad.run(safe, ad.build_plan(ad.snapshot(safe), ctx), ctx)
    assert _val(sess, "[data-qa=name-input]") == NAME and _val(sess, "[data-qa=email-input]") == EMAIL
    assert "CAPTCHA" not in out.reason and out.filled >= 2
    assert server.posts() == []


def test_invisible_recaptcha_badge_is_not_a_pause(sess, server):
    server.requests.clear()
    safe = sess.safe_page()
    url = server.url("recaptcha_invisible_badge.html")
    safe.goto(url)
    _settle(sess)
    assert any("/recaptcha/api2/anchor" in f.url for f in sess.page.frames)
    assert safe.captcha() is None
    ad = GenericAdapter()
    ctx = PlanContext(job_key="b", url=url, answer_fn=stub_answer)
    out = run_plan(safe, ad.build_plan(ad.snapshot(safe), ctx), adapter=ad, ctx=ctx)
    assert _val(sess, "#first") == FIRST and _val(sess, "#email") == EMAIL
    assert out.status != "needs_you" or "CAPTCHA" not in out.reason
    assert server.posts() == []


def test_visible_checkbox_pauses_and_nothing_is_typed(sess, server):
    server.requests.clear()
    safe = sess.safe_page()
    url = server.url("recaptcha_visible_checkbox.html")
    safe.goto(url)
    _settle(sess)
    assert safe.captcha() and "challenge" in safe.captcha()
    ad = GenericAdapter()
    ctx = PlanContext(job_key="v", url=url, answer_fn=stub_answer)
    out = run_plan(safe, ad.build_plan(ad.snapshot(safe), ctx), adapter=ad, ctx=ctx)
    assert out.status == "needs_you" and "CAPTCHA" in out.reason
    assert _val(sess, "#first") == "" and _val(sess, "#email") == ""
    assert server.posts() == []


def test_challenge_turning_visible_mid_fill_stops_the_spec_adapter(sess, server):
    server.requests.clear()
    ad = LeverAdapter()
    safe = sess.safe_page(**ad.safe_page_kwargs())
    url = server.url("lever_invisible_hcaptcha.html") + "?reveal=1"
    safe.goto(url)
    _settle(sess)
    assert safe.captcha() is None                      # hidden at the start: the fill begins
    ctx = PlanContext(job_key="lv2", url=url, answer_fn=stub_answer)
    out = ad.run(safe, ad.build_plan(ad.snapshot(safe), ctx), ctx)
    filled = [_val(sess, "[data-qa=name-input]"), _val(sess, "[data-qa=email-input]")]
    assert filled.count("") >= 1                       # typing stopped once it appeared
    assert out.status == "needs_you" and "CAPTCHA" in out.reason
    assert safe.halted and server.posts() == []


def test_challenge_turning_visible_mid_fill_stops_the_executor(sess, server):
    server.requests.clear()
    safe = sess.safe_page()
    url = server.url("lever_invisible_hcaptcha.html") + "?reveal=1"
    safe.goto(url)
    _settle(sess)
    ad = GenericAdapter()
    ctx = PlanContext(job_key="g2", url=url, answer_fn=stub_answer)
    out = run_plan(safe, ad.build_plan(ad.snapshot(safe), ctx), adapter=ad, ctx=None)
    assert out.status == "needs_you" and "CAPTCHA" in out.reason
    assert [_val(sess, "[data-qa=name-input]"), _val(sess, "[data-qa=email-input]")].count("") >= 1
    assert server.posts() == []


@pytest.mark.parametrize("style, seen", [
    ("width:304px;height:78px", True),                          # the checkbox
    ("width:400px;height:580px", True),                         # an image challenge
    ("width:304px;height:78px;display:none", False),
    ("width:304px;height:78px;visibility:hidden", False),
    ("width:304px;height:78px;opacity:0", False),
    ("width:0;height:0;border:0", False),
    ("width:304px;height:78px;position:absolute;top:-10000px", False),
    ("width:304px;height:78px;position:absolute;left:-10000px", False),
    ("width:20px;height:20px", False),                          # too small to be a challenge
])
def test_frame_visibility_rules(sess, server, style, seen):
    page = sess.page
    page.goto(server.url("generic_form.html"))
    page.evaluate("""(st) => { const f = document.createElement('iframe');
        f.src = '/newassets.hcaptcha/static/hcaptcha.html#frame=checkbox'; f.setAttribute('style', st);
        document.body.appendChild(f); }""", style)
    page.wait_for_timeout(300)
    assert bool(detect.captcha_info(page)) is seen


def test_invisible_size_param_never_pauses(sess, server):
    page = sess.page
    page.goto(server.url("generic_form.html"))
    page.evaluate("""() => { const f = document.createElement('iframe');
        f.src = '/recaptcha/api2/anchor?k=x&size=invisible'; f.style.cssText = 'width:304px;height:78px';
        document.body.appendChild(f); }""")
    page.wait_for_timeout(300)
    assert detect.captcha_info(page) is None


# --------------------------------------------------------------------------- 2. Ashby: exact read ops

def test_ashby_allow_list_is_exact_names_only():
    rules = AshbyAdapter.allow_graphql_reads
    assert rules and all(isinstance(ops, frozenset) for _, ops in rules)
    for endpoint, ops in rules:
        assert endpoint.startswith("^https://jobs\\.ashbyhq\\.com/")      # one host, https, anchored
        for name in ops:
            assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)          # a name, not a pattern
            assert not re.search(r"submit|create|apply|update", name, re.I)
    assert {"ApiJobPosting", "ApiOrganizationFromHostedJobsPageName"} <= set(rules[0][1])
    assert "allow_graphql_reads" in AshbyAdapter().safe_page_kwargs()


@pytest.mark.parametrize("bad", [["ApiJob*"], ["Api.*"], ["ApiJobPosting|ApiSubmitX"], ["ApiSubmitApplication"],
                                 ["ApiCreateThing"], ["ApiApplyNow"], ["ApiUpdateDraft"], [""]])
def test_allow_list_refuses_patterns_and_write_words(bad):
    with pytest.raises(ValueError):
        G.read_op_names(bad)


def test_spec_loader_refuses_a_bad_allow_list(tmp_path, monkeypatch):
    src = (gh.SPEC_DIR / "ashby.yaml").read_text(encoding="utf-8")
    monkeypatch.setattr(gh, "SPEC_DIR", tmp_path)
    for swap in ("ApiJobPosting, ApiSubmitSingleApplicationFormAction", "ApiJob.*",
                 "ApiJobPosting, ApiCreateFileUploadHandle"):
        (tmp_path / "ashby.yaml").write_text(
            src.replace("ApiJobPosting, ApiOrganizationFromHostedJobsPageName", swap), encoding="utf-8")
        with pytest.raises(ValueError):
            gh.load_spec("ashby")
    (tmp_path / "ashby.yaml").write_text(src.replace("'^https://jobs", "'^https?://jobs"), encoding="utf-8")
    with pytest.raises(ValueError):
        gh.load_spec("ashby")                                   # plain http is never allowed


READ_URL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
READ_BODY = ('{"operationName":"ApiJobPosting","variables":{"id":"x"},"query":"query ApiJobPosting($id: String!) '
             '{ jobPosting(id: $id) { id updatedAt applicationForm { ...F } } } fragment F on FormRender { id }"}')


def test_graphql_read_rule_unit():
    rules = G._graphql_rules(AshbyAdapter.allow_graphql_reads)
    assert G.graphql_read_allowed(READ_URL, READ_BODY, rules)
    bad = [
        (READ_URL, READ_BODY.replace("query ApiJobPosting", "mutation ApiJobPosting")),
        (READ_URL, READ_BODY.replace('"operationName":"ApiJobPosting"', '"operationName":"ApiJobPostingX"')),
        (READ_URL + "X", READ_BODY),                                          # prefix of an allowed name
        (READ_URL + "&debug=1", READ_BODY),                                   # extra URL parameter
        (READ_URL.replace("https://jobs.ashbyhq.com", "https://evil.example"), READ_BODY),
        (READ_URL, READ_BODY.replace("{ id updatedAt", "{ id Submit")),       # a write word in the body
        (READ_URL, READ_BODY.replace("fragment F on FormRender { id }",
                                     "fragment F on FormRender { id } mutation M { x }")),
        (READ_URL, '{"operationName":"ApiJobPosting","query":"{ jobPosting { id } }"}'),   # anonymous
        (READ_URL, "operationName=ApiJobPosting"),                            # not JSON
        (READ_URL, ""),
    ]
    for url, body in bad:
        assert not G.graphql_read_allowed(url, body, rules), (url, body[:80])


def test_disguised_submit_is_caught_by_the_body_patterns():
    body = ('{"operationName":"ApiSubmitSingleApplicationFormAction","query":'
            '"mutation ApiSubmitSingleApplicationFormAction($i: In!) { submit(input: $i) { id } }"}')
    pats = [re.compile(p, re.I) for p in AshbyAdapter.submit_patterns]
    assert not any(p.search(READ_URL) for p in pats)                      # the URL looks like a read
    assert any(p.search(body) for p in pats)                              # the body is a submit


def test_ashby_page_reads_pass_submits_never(sess, server):
    """Read allowed, submit blocked, even disguised under a read op's URL name."""
    server.requests.clear()
    local = (r"^http://127\.0\.0\.1:\d+/api/non-user-graphql\?", ["ApiJobPosting"])
    safe = sess.safe_page(submit_patterns=AshbyAdapter.submit_patterns,
                          allow_post_patterns=AshbyAdapter.allow_post_patterns, allow_graphql_reads=[local])
    safe.goto(server.url("ashby_graphql.html"))
    sess.page.wait_for_function("window.__done === 6", timeout=10_000)
    ok = sess.page.evaluate("window.__ok")
    assert ok == {"read": True, "submit": False, "disguised": False, "borrowed": False, "prefix": False,
                  "extra": False}
    assert server.posts() == [("POST", "/api/non-user-graphql?op=ApiJobPosting")]
    kinds = sorted(b["kind"] for b in safe.guard.blocked)
    assert kinds.count("submit-endpoint") == 2                            # submit + disguised
    assert kinds.count("non-get") == 3                                    # borrowed, prefix, extra


def test_ashby_spec_rule_never_matches_a_local_or_other_host(sess, server):
    server.requests.clear()
    ad = AshbyAdapter()
    safe = sess.safe_page(**ad.safe_page_kwargs())
    safe.goto(server.url("ashby_graphql.html"))
    sess.page.wait_for_function("window.__done === 6", timeout=10_000)
    assert server.posts() == []                       # the real rule is for jobs.ashbyhq.com only


# --------------------------------------------------------------------------- 3. relevant experience

@pytest.mark.parametrize("q, ft", [("Relevant Experience", "text"), ("Years of relevant experience", "number"),
                                   ("Total relevant experience (in years)", "text"),
                                   ("Relevant experience", "select"),
                                   ("Years of experience in a similar role", "number"),
                                   ("Domain experience (years)", "number")])
def test_relevant_experience_is_yours(q, ft):
    from jobcopilot import answers as A
    opts = ["0-1 years", "1-3 years", "3-5 years"] if ft == "select" else None
    a = A.answer_question(q, field_type=ft, options=opts, answers=A.load(A.example_answers_path()))
    assert a.value == "" and a.needs_user and "relevant" in a.note


def test_total_experience_is_still_answered():
    import datetime as dt
    from jobcopilot import answers as A
    bank = A.load(A.example_answers_path())
    a = A.answer_question("Total Experience", answers=bank, today=dt.date(2026, 11, 19))
    assert a.value == "2 years 10 months" and not a.needs_user


def test_relevant_experience_is_highlighted_on_keka(sess, server):
    import datetime as dt
    from jobcopilot import answers as A
    from jobcopilot.autofill import service
    from jobcopilot.autofill.adapters.keka import KekaAdapter
    server.requests.clear()
    ad = KekaAdapter()
    safe = sess.safe_page(**ad.safe_page_kwargs())
    url = server.url("keka_apply_annual.html")
    safe.goto(url)
    ctx = PlanContext(job_key="k", job={"country_bucket": "IN"}, url=url, answers=A.load(A.example_answers_path()),
                      facts=[], answer_fn=service.answer_no_llm, today=dt.date(2026, 11, 19))
    plan = ad.build_plan(ad.snapshot(safe), ctx)
    ad.run(safe, plan, ctx)
    rel = '[id="00000000-0000-4000-8000-000000000003"]'
    tot = '[id="00000000-0000-4000-8000-000000000002"]'
    assert _val(sess, rel) == "" and sess.page.get_attribute(rel, "data-jc-highlight") == "needs_you"
    assert _val(sess, tot) == "2 years 10 months"
    assert server.posts() == []
