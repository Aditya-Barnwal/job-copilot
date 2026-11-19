"""Regression tests for the Phase 4 autofill safety review (findings 1, 5-11).

Browser tests run headless against a LOCAL server on 127.0.0.1 (never the
internet). "localhost" stands in for a do-not-automate host (LinkedIn,
Indeed...): guard.denied_hosts is patched so the same local server, reached by
that name, is "denied". Nothing here submits anything."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from jobcopilot.answers import Answer
from jobcopilot.autofill import guard
from jobcopilot.autofill.adapters.base import PlanContext
from jobcopilot.autofill.executor import run_plan
from jobcopilot.autofill.generic import GenericAdapter
from jobcopilot.autofill.plan import FieldAction, FillPlan

EMAIL = "zyxwvut.q@example.com"

PAGES = {
    # finding 1: Continue -> /redir -> 302 to the denied host (the review's repro pattern)
    "/start.html": """<!doctype html><html><body><h1>Apply</h1>
<form id="f" action="/submit" method="post">
<label for="nm">Full name</label><input id="nm" name="name">
<button type="button" id="cont" onclick="location.href='/redir'">Continue</button>
</form></body></html>""",
    "/denied.html": """<!doctype html><html><body><h1>Easy Apply</h1>
<form action="/submit" method="post">
<label for="em">Email</label><input id="em" name="email" type="email">
<button type="submit">Submit application</button></form></body></html>""",
    "/framed.html": """<!doctype html><html><body>
<label for="em">Email</label><input id="em" name="email" type="email">
<iframe id="fr" src="/redir"></iframe></body></html>""",
    # finding 9: a GET form submitted through a pristine HTMLFormElement.submit from a
    # fresh iframe, fired by the Continue click (inside the navigation window)
    "/getform.html": """<!doctype html><html><body>
<form id="g" action="/get-submit" method="get"><input name="q" value="1"></form>
<button type="button" id="cont" onclick="
  const f = document.createElement('iframe'); document.body.appendChild(f);
  f.contentWindow.HTMLFormElement.prototype.submit.call(document.getElementById('g'));">Continue</button>
<a id="lnk" href="/start.html">elsewhere</a>
</body></html>""",
    "/linknav.html": """<!doctype html><html><body>
<button type="button" id="cont" onclick="location.href='/start.html?from=continue'">Continue</button>
</body></html>""",
    # finding 9: the release token must not be discoverable from the page
    "/selfoff.html": """<!doctype html><html><body>
<form id="f" action="/self-submit" method="get"><input name="q" value="1"></form>
<button type="button" id="x" onclick="
 const s = Object.getOwnPropertySymbols(window).map(k => Symbol.keyFor(k) || k.description || '')
   .find(k => k && k.indexOf('__jcoff_') === 0);
 window.__found = s || null; if (s) window.dispatchEvent(new Event(s));
 document.getElementById('f').requestSubmit();">Expand section</button>
</body></html>""",
    # finding 9: el.type, WebSocket
    "/beacon.html": """<!doctype html><html><body><h1>Apply</h1>
<form action="/submit" method="post">
<label for="nm">Full name</label><input id="nm" name="name">
<button type="button" id="add" onclick="try { window.__ws = new WebSocket('ws://' + location.host + '/ws'); } catch (e) {}">Add another</button>
<button type="bogus" id="icon" aria-label="arrow"><svg width="10" height="10"></svg></button>
</form></body></html>""",
    # findings 5 + 10
    "/controls.html": """<!doctype html><html><body><form action="/submit" method="post">
<fieldset><legend>Do you authorize us to run a background check?</legend>
<label><input type="radio" name="bg" value="yes"> Yes</label>
<label><input type="radio" name="bg" value="no"> No</label></fieldset>
<div role="radiogroup" aria-labelledby="dq"><span id="dq">I hereby declare the above is true</span>
<label><input type="radio" name="dc" value="y"> Yes</label>
<label><input type="radio" name="dc" value="n"> No</label></div>
<label for="ack">Do you accept the terms of our privacy policy?</label>
<select id="ack" name="ack"><option value="">Select</option><option>Yes</option><option>No</option></select>
<label for="ctry">Country</label>
<select id="ctry" name="ctry"><option value="">Select</option><option>India</option><option>Other</option></select>
<label><input type="checkbox" id="hid" name="hid" style="opacity:0"> Remote ok</label>
<label><input type="radio" id="hidr" name="hr" value="1" style="position:absolute;left:-9999px"> Yes</label>
<label><input type="checkbox" id="vis" name="vis"> Remote ok too</label>
<select id="hsel" name="hsel" style="display:none"><option value="">-</option><option>India</option></select>
<label for="cv">Resume</label><input type="file" id="cv" name="cv">
</form></body></html>""",
}


class _Server:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []      # (method, host, path)
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                srv.requests.append(("GET", self.headers.get("Host") or "", self.path))
                p = self.path.split("?", 1)[0]
                if p == "/redir":
                    return self._send(302, headers={"Location": f"http://localhost:{srv.port}/denied.html"})
                if p in PAGES:
                    return self._send(200, PAGES[p].encode())
                return self._send(404, b"nope")

            def do_POST(self):
                srv.requests.append(("POST", self.headers.get("Host") or "", self.path))
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                return self._send(204)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def paths(self, method: str = "GET") -> list[str]:
        return [p for m, _, p in self.requests if m == method]

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="module")
def server():
    s = _Server()
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
def localhost_denied(monkeypatch):
    """'localhost' plays a do-not-automate host for these tests."""
    monkeypatch.setattr(guard, "denied_hosts", lambda: ("localhost",))


def stub_answer(question, *, field_type="text", options=None, required=None, **_):
    q = question.lower()
    if "email" in q:
        return Answer(EMAIL, "answers.yaml:personal.email", 1.0, False)
    if "name" in q:
        return Answer("Zyx Wvut", "answers.yaml:personal.full_name", 1.0, False)
    if "country" in q:
        return Answer("India", "answers.yaml:personal.country", 0.9, False)
    return Answer("Yes", "answers.yaml:x", 1.0, False)       # would tick anything if allowed


def _ctx(url: str, **kw) -> PlanContext:
    return PlanContext(job_key="sec-1", url=url, answer_fn=stub_answer, **kw)


# --------------------------------------------------------------------------- finding 1: redirects

def test_continue_redirect_to_denied_host_types_nothing(sess, server, localhost_denied):
    """Continue -> /redir -> 302 to a denied host: the route never sees the final
    URL, but the executor stops (blocked) and nothing is typed on that page."""
    server.requests.clear()
    safe = sess.safe_page()
    start = server.url("/start.html")
    safe.goto(start)
    ad = GenericAdapter()
    plan = ad.build_plan(ad.snapshot(safe), _ctx(start))
    out = run_plan(safe, plan, adapter=ad, ctx=_ctx(start))       # ctx: it would re-plan page 2
    assert "denied.html" in sess.page.url                         # the redirect did happen ...
    assert sess.page.eval_on_selector("#em", "e => e.value") == ""   # ... and nothing was typed
    assert out.status == "blocked" and "do-not-automate" in out.reason, out
    assert any(b["kind"] == "denied-host-landed" for b in safe.guard.blocked)
    assert safe.halted and "do-not-automate" in safe.halted
    with pytest.raises(guard.Halted):
        safe.fill("#em", EMAIL)
    assert server.paths("POST") == []


def test_goto_that_redirects_to_denied_host_halts(sess, server, localhost_denied):
    safe = sess.safe_page()
    with pytest.raises(guard.DeniedHost):
        safe.goto(server.url("/redir"))
    with pytest.raises(guard.Halted):
        safe.fill("#em", EMAIL)
    assert sess.page.eval_on_selector("#em", "e => e.value") == ""


def test_frame_landing_on_denied_host_stops_writes(sess, server, localhost_denied):
    """An iframe whose first URL is allowed but which redirects to a denied host:
    framenavigated trips the guard and _writable() refuses every write."""
    safe = sess.safe_page()
    try:
        safe.goto(server.url("/framed.html"))     # raises if the frame already landed by then
    except guard.DeniedHostHalt:
        pass
    sess.page.wait_for_timeout(800)
    assert safe.guard.tripped and any(b["kind"] == "denied-host-landed" for b in safe.guard.blocked)
    with pytest.raises(guard.Halted):
        safe.fill("#em", EMAIL)
    assert sess.page.eval_on_selector("#em", "e => e.value") == ""


def _in_thread(fn):
    """Run fn in its own thread: one sync Playwright driver per thread (this
    module's browser fixture already owns the main one)."""
    box: dict = {}

    def run():
        try:
            box["ok"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc
    t = threading.Thread(target=run)
    t.start()
    t.join(120)
    return box


def test_planner_refuses_a_redirect_to_denied_host(server, localhost_denied):
    pytest.importorskip("playwright.sync_api")
    from jobcopilot.autofill import service
    c = service.settings()
    c["timeouts"] = dict(c["timeouts"], page_load_sec=15)
    box = _in_thread(lambda: service.PlaywrightBackend().snapshot(server.url("/redir"), "generic", c))
    exc = box.get("exc")
    if exc is not None and not isinstance(exc, service.AutofillRefused):
        pytest.skip(f"no browser available: {type(exc).__name__}")
    assert isinstance(exc, service.AutofillRefused), box
    assert exc.code == "denylisted" and exc.link_out == server.url("/redir")
    # the planner's route also aborts a direct request to the denied host
    server.requests.clear()
    box = _in_thread(lambda: service.PlaywrightBackend().snapshot(
        f"http://localhost:{server.port}/denied.html", "generic", c))
    assert "exc" in box
    assert not [r for r in server.requests if r[1].startswith("localhost")]


# --------------------------------------------------------------------------- finding 7: denylist rule

@pytest.mark.parametrize("url", ["https://uk.indeed.co.uk/viewjob?jk=1", "https://www.glassdoor.co.in/job/1",
                                 "https://www.linkedin.cn/jobs/1", "https://lnkd.in/abc123",
                                 "https://www.linkedin.com/jobs/view/1"])
def test_service_denied_host_matches_the_guard(url):
    from jobcopilot.autofill import service
    assert service.denied_host(url) and guard.is_denied(url)


def test_service_denied_host_keeps_config_extras_and_allows_ats():
    from jobcopilot.autofill import service
    assert service.denied_host("https://jobs.example-board.test/1",
                               {"denied_hosts_extra": ["example-board.test"]}) == "example-board.test"
    assert service.denied_host("https://job-boards.greenhouse.io/acme/jobs/1") is None
    assert service.denied_host("ftp://x.test/") == "invalid-url"
    assert "lnkd.in" in guard.denied_hosts()


# --------------------------------------------------------------------------- finding 9: outer layers

def test_get_form_submission_blocked_inside_the_continue_window(sess, server):
    server.requests.clear()
    safe = sess.safe_page()
    safe.goto(server.url("/getform.html"))
    safe.next_step("#cont")
    sess.page.wait_for_timeout(600)
    assert "/get-submit" not in " ".join(server.paths()), server.requests
    assert "getform.html" in sess.page.url


def test_ordinary_js_navigation_still_works_inside_the_continue_window(sess, server):
    safe = sess.safe_page()
    safe.goto(server.url("/linknav.html"))
    safe.next_step("#cont")
    assert sess.page.url.endswith("/start.html?from=continue")


def test_formdata_built_by_script_is_untouched(sess, server):
    safe = sess.safe_page()
    safe.goto(server.url("/getform.html"))
    keys = sess.page.evaluate("() => Array.from(new FormData(document.getElementById('g')).keys())")
    assert keys == ["q"]
    assert sess.page.evaluate("() => new FormData() instanceof FormData")


def test_real_form_submissions_carry_the_guard_marker(sess, server):
    """The formdata listener (it fires for every genuine submission, whichever
    realm's submit() started it) appends the secret marker the route aborts on;
    the DOM override covers a borrowed iframe submit() too."""
    safe = sess.safe_page()
    safe.goto(server.url("/getform.html"))
    keys = sess.page.evaluate("""() => { const fd = new FormData(); fd.append('q', '1');
        window.dispatchEvent(new FormDataEvent('formdata', {formData: fd, bubbles: true}));
        return Array.from(fd.keys()); }""")
    assert keys == ["q", safe.guard.form_mark]
    assert sess.page.evaluate("""() => { const f = document.createElement('iframe'); document.body.appendChild(f);
        return f.contentWindow.HTMLFormElement.prototype.submit.toString().includes('blocked'); }""")


def test_dom_release_token_is_not_discoverable(sess, server):
    server.requests.clear()
    safe = sess.safe_page()
    safe.goto(server.url("/selfoff.html"))
    names = sess.page.evaluate(
        "() => Object.getOwnPropertySymbols(window).map(k => Symbol.keyFor(k) || k.description || '')")
    assert not any(safe.guard.token in n for n in names)
    safe.click("#x")
    sess.page.wait_for_timeout(500)
    assert sess.page.evaluate("window.__found") is None
    assert "/self-submit" not in " ".join(server.paths()) and "selfoff" in sess.page.url
    # ... and the Python side can still release it (handover)
    sess.handover()
    sess.page.evaluate("document.getElementById('f').requestSubmit()")
    sess.page.wait_for_timeout(500)
    assert "/self-submit" in " ".join(server.paths())


def test_bogus_button_type_is_submit_like(sess, server):
    safe = sess.safe_page()
    safe.goto(server.url("/beacon.html"))
    m = safe.meta("#icon")
    assert m["type_attr"] == "bogus" and m["el_type"] == "submit"
    assert guard.is_submit_like(m)
    with pytest.raises(guard.SubmitGuardViolation):
        safe.click("#icon")
    assert guard.is_submit_like({"tag": "button", "type_attr": "bogus", "el_type": "submit", "in_form": True})


def test_websocket_blocked_while_filling(sess, server):
    server.requests.clear()
    safe = sess.safe_page()
    safe.goto(server.url("/beacon.html"))
    safe.click("#add")
    sess.page.wait_for_timeout(800)
    assert any(b["kind"] == "websocket" for b in safe.guard.blocked)
    assert sess.page.evaluate("window.__ws ? window.__ws.readyState : null") == 3      # CLOSED
    assert "/ws" not in server.paths()                                               # never reached


# --------------------------------------------------------------------------- findings 5 + 10

def test_consent_radio_and_select_by_question_are_refused(sess, server):
    safe = sess.safe_page()
    safe.goto(server.url("/controls.html"))
    with pytest.raises(guard.ConsentRefused):
        safe.choose('input[name="bg"]', "Yes")
    with pytest.raises(guard.ConsentRefused):
        safe.check('input[name="dc"][value="y"]')
    with pytest.raises(guard.ConsentRefused):
        safe.select("#ack", "Yes")
    checked = sess.page.evaluate("() => Array.from(document.querySelectorAll('input:checked')).length")
    assert checked == 0 and sess.page.eval_on_selector("#ack", "e => e.selectedIndex") == 0
    safe.select("#ctry", "India")                                # an ordinary question still works


def test_generic_plan_never_fills_consent_radios_or_selects(sess, server):
    safe = sess.safe_page()
    url = server.url("/controls.html")
    safe.goto(url)
    ad = GenericAdapter()
    plan = ad.build_plan(ad.snapshot(safe), _ctx(url))
    by = {a.label: a for a in plan.actions()}
    for q in ("Do you authorize us to run a background check?", "I hereby declare the above is true",
              "Do you accept the terms of our privacy policy?"):
        assert by[q].value is None and by[q].needs_user and by[q].source == "consent", q
    out = ad.run(safe, plan)
    assert sess.page.evaluate("() => document.querySelectorAll('input[name=bg]:checked, input[name=dc]:checked').length") == 0
    assert sess.page.eval_on_selector("#ack", "e => e.selectedIndex") == 0
    assert "Do you accept the terms of our privacy policy?" in out.highlighted


def test_hidden_checkbox_radio_select_are_never_ticked(sess, server):
    safe = sess.safe_page()
    url = server.url("/controls.html")
    safe.goto(url)
    for sel in ("#hid", "#hidr"):
        with pytest.raises(guard.SafePageError, match="hidden"):
            safe.check(sel)
    with pytest.raises(guard.SafePageError, match="hidden"):
        safe.select("#hsel", "India")
    with pytest.raises(guard.HoneypotRefused):                     # a honeypot-named box: refused as one
        sess.page.evaluate("document.querySelector('#vis').name = 'beecatcher'")
        safe.check("#vis")
    sess.page.evaluate("document.querySelector('#vis').name = 'vis'")
    assert not sess.page.is_checked("#hid") and not sess.page.is_checked("#hidr")
    safe.check("#vis")                                             # a visible box still works
    assert sess.page.is_checked("#vis")
    ad = GenericAdapter()
    plan = ad.build_plan(ad.snapshot(safe), _ctx(url))
    hidden = [a for a in plan.actions() if a.locator in ("#hid", "#hsel") or '"hr"' in a.locator]
    assert len(hidden) == 3 and all(a.value is None and a.needs_user for a in hidden)    # listed, never set
    out = ad.run(safe, plan)
    assert not sess.page.is_checked("#hid") and not sess.page.is_checked("#hidr")
    assert sess.page.eval_on_selector("#hsel", "e => e.selectedIndex") == 0 and out.status != "error"


# --------------------------------------------------------------------------- finding 11: upload TOCTOU

def test_upload_sends_the_resolved_checked_path(sess, server, tmp_path):
    real = tmp_path / "real_resume.pdf"
    real.write_bytes(b"%PDF-1.4\n")
    link = tmp_path / "link_resume.pdf"
    link.symlink_to(real)
    safe = sess.safe_page()
    safe.goto(server.url("/controls.html"))
    safe.allowed_uploads = {str(real.resolve())}
    safe.upload("#cv", str(link))
    assert sess.page.eval_on_selector("#cv", "e => e.files[0].name") == "real_resume.pdf"
    other = tmp_path / "other.pdf"
    other.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(guard.SafePageError):
        safe.upload("#cv", str(other))


# --------------------------------------------------------------------------- finding 8: min_confidence

def test_min_confidence_reaches_planners_and_live_fields():
    from jobcopilot.autofill import service
    from jobcopilot.autofill.adapters.greenhouse import GreenhouseAdapter
    ctx = service.plan_context({"key": "k"}, "https://x.test", {}, cfg={"min_confidence": 0.95})
    assert isinstance(ctx, PlanContext) and ctx.min_confidence == 0.95
    strict = _ctx("https://x.test", min_confidence=0.95)
    country = {"label": "Country", "type": "select", "locator": "#ctry", "options": ["India", "Other"],
               "name": "ctry", "id": "ctry", "required": True}
    act = GreenhouseAdapter().resolve_field(dict(country), strict)          # mid-fill path (~378)
    assert act.value is None and act.needs_user                             # 0.9 < 0.95
    assert GreenhouseAdapter().resolve_field(dict(country), _ctx("https://x.test")).value == "India"
    snap = {"fields": [{**country, "selector": "#ctry", "options": [{"label": "India", "value": "India"}],
                        "group_label": "", "hidden": False}]}
    assert GenericAdapter().build_plan(snap, strict).actions()[0].value is None
    assert GenericAdapter().build_plan(snap, _ctx("https://x.test")).actions()[0].value == "India"


def test_spec_adapter_fill_uses_the_run_ctx_floor():
    from jobcopilot.autofill.adapters.greenhouse import GreenhouseAdapter, _Run

    class FakeSafe:
        def __init__(self):
            self.filled = []

        def count(self, sel):
            return 1

        def fill(self, *a, **k):
            self.filled.append(a[0])

        def meta(self, loc):
            return {"tag": "input", "role": ""}

        def highlight(self, *a, **k):
            return True

        def captcha(self):              # the mid-fill re-check: no challenge on screen
            return None
    ad = GreenhouseAdapter()
    ad.ctx = _ctx("https://x.test", min_confidence=0.95)
    safe = FakeSafe()
    plan = FillPlan("k", "greenhouse", "https://x.test", [[
        FieldAction("#a", "Notice period", "fill", "30", "answers.yaml:x", 0.9, False)]])
    ad._fill(safe, plan.steps[0], plan, _Run())
    assert safe.filled == []


# --------------------------------------------------------------------------- finding 6: Workday network rules

class _Req:
    def __init__(self, url, method="POST", body="", nav=False):
        self.url, self.method, self.post_data, self._nav = url, method, body, nav
        self.frame = None

    def is_navigation_request(self):
        return self._nav


class _Route:
    def __init__(self):
        self.result = None

    def abort(self, *_):
        self.result = "abort"

    def fallback(self):
        self.result = "allow"


class _Ctx:
    pages = []

    def add_init_script(self, *_):
        pass

    def route(self, *_):
        pass


def _verdict(g, url, body=""):
    r = _Route()
    g._route(r, _Req(url, body=body))
    return r.result


def test_workday_allowlist_is_narrow_and_bodies_are_checked():
    from jobcopilot.autofill.adapters.workday import WorkdayAdapter
    kw = WorkdayAdapter().safe_page_kwargs()
    g = guard.Guard(_Ctx(), submit_patterns=kw["submit_patterns"], allow_post_patterns=kw["allow_post_patterns"])
    g.install()
    t = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External"
    assert _verdict(g, f"{t}/job/R-1/apply/step?section=x", "{}") == "allow"
    assert _verdict(g, f"{t}/job/R-1/apply/attachments", "") == "allow"
    assert _verdict(g, f"{t}/job/R-1/apply/submit", "{}") == "abort"
    assert _verdict(g, f"{t}/anything/else", "{}") == "abort"                  # was allowed before
    assert _verdict(g, f"{t}/userProfile", "{}") == "abort"
    assert _verdict(g, f"{t}/job/R-1/apply/step", '{"action": "submit"}') == "abort"
    assert _verdict(g, f"{t}/job/R-1/apply/save", "operation=finalize&x=1") == "abort"
    assert _verdict(g, "https://evil.test/wday/cxs/a/b/job/x/apply/step", "{}") == "abort"
    assert g.blocked[-3]["kind"] == "submit-endpoint"


def test_form_marker_is_blocked_by_the_route_even_for_gets():
    g = guard.Guard(_Ctx())
    g.install()
    r = _Route()
    g._nav_depth = 1                                     # inside a Continue window
    g._route(r, _Req(f"https://x.test/search?q=1&{g.form_mark}=1", method="GET", nav=True))
    assert r.result == "abort" and g.blocked[-1]["kind"] == "form-submission"
    assert g.form_token not in g._dom_script().split("MARK = ")[0]          # separate secrets
    assert g.token != g.form_token and g.install_id not in (g.token, g.form_token)


def test_upload_keys_and_paths_unaffected_for_fixture_plan(tmp_path):
    """enforce_policy still keeps an approved upload key (sanity for finding 3/4)."""
    from jobcopilot.autofill import service
    p = tmp_path / "r.pdf"
    p.write_bytes(b"%PDF")
    plan = FillPlan("k", "generic", "https://x.test", [[FieldAction("#cv", "Resume", "upload", "resume",
                                                                     "file:resume", 1.0, False)]],
                    files={"resume": str(p)})
    service.enforce_policy(plan, {"min_confidence": 0.8})
    assert plan.actions()[0].value == "resume" and Path(plan.files["resume"]).is_file()
