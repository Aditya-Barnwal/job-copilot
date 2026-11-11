"""ATS detector: pure HTML/URL fingerprints plus detect() driven by a fake client.

Nothing here touches the network: FakeClient serves saved pages from
tests/fixtures/ats and records every URL it was asked for.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest
import requests

from jobcopilot import ats_detect
from jobcopilot.ats_detect import AtsResult, detect, detect_from_html, to_company_config
from jobcopilot.http import HttpConnectionError, HttpStatusError, RobotsDisallowed

FIX = Path(__file__).parent / "fixtures" / "ats"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class _Resp:
    def __init__(self, url: str, text: str) -> None:
        self.url, self.text = url, text


class FakeClient:
    """Just enough of PoliteClient for detect(): get / get_json / post_json / is_denied."""

    def __init__(self, pages: Optional[dict[str, Any]] = None,
                 api: Optional[dict[str, Any]] = None, denied: tuple[str, ...] = ()) -> None:
        self.pages = pages or {}   # url -> html  or (final_url, html)  or Exception
        self.api = api or {}       # url prefix -> json  or Exception
        self.denied = denied
        self.calls: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    def is_denied(self, url: str) -> bool:
        return any(d in url for d in self.denied)

    def get(self, url: str, **kw: Any) -> _Resp:
        self.calls.append(url)
        self.kwargs.append(kw)
        page = self.pages.get(url)
        if page is None:
            raise HttpConnectionError(f"no route to {url}")
        if isinstance(page, Exception):
            raise page
        final, text = page if isinstance(page, tuple) else (url, page)
        return _Resp(final, text)

    def _json(self, url: str) -> Any:
        self.calls.append(url)
        for prefix, val in self.api.items():
            if url.startswith(prefix):
                if isinstance(val, Exception):
                    raise val
                return val
        raise HttpConnectionError(f"no api for {url}")

    def get_json(self, url: str, **_: Any) -> Any:
        return self._json(url)

    def post_json(self, url: str, **_: Any) -> Any:
        return self._json(url)


def _status_error(url: str, code: int) -> HttpStatusError:
    r = requests.Response()
    r.status_code, r.url = code, url
    return HttpStatusError("GET", url, r)


# ------------------------------------------------------------ URL patterns

@pytest.mark.parametrize("url, expect", [
    ("https://jobs.ashbyhq.com/litellm", {"ats": "ashby", "slug": "litellm"}),
    ("https://jobs.ashbyhq.com/revenuecat/1f2e3d?utm_source=x", {"ats": "ashby", "slug": "revenuecat"}),
    ("https://jobs.lever.co/cred/abc-123", {"ats": "lever", "slug": "cred"}),
    ("https://jobs.eu.lever.co/acme", {"ats": "lever", "slug": "acme", "region": "eu"}),
    ("https://boards.greenhouse.io/stripe", {"ats": "greenhouse", "slug": "stripe", "region": None}),
    ("https://job-boards.greenhouse.io/postman/jobs/7747644003", {"ats": "greenhouse", "slug": "postman"}),
    ("https://job-boards.eu.greenhouse.io/groww", {"ats": "greenhouse", "slug": "groww", "region": "eu"}),
    ("https://boards.greenhouse.io/embed/job_board?for=vercel", {"ats": "greenhouse", "slug": "vercel"}),
    ("https://boards-api.greenhouse.io/v1/boards/digitalocean98/jobs", {"ats": "greenhouse", "slug": "digitalocean98"}),
    ("https://circle.wd1.myworkdayjobs.com/en-US/Circle", {"ats": "workday", "tenant": "circle", "wd_server": "wd1", "site": "Circle"}),
    ("https://adobe.wd5.myworkdayjobs.com/external_experienced/job/x", {"ats": "workday", "tenant": "adobe", "site": "external_experienced"}),
    ("https://paypal.wd1.myworkdayjobs.com/wday/cxs/paypal/jobs/jobs", {"ats": "workday", "site": "jobs"}),
    ("https://careers.smartrecruiters.com/Freshworks", {"ats": "smartrecruiters", "slug": "Freshworks"}),
    ("https://jobs.smartrecruiters.com/Freshworks/744000101010101", {"ats": "smartrecruiters", "slug": "Freshworks"}),
    ("https://jobs.gem.com/promptql", {"ats": "gem", "slug": "promptql"}),
    ("https://pinelabsgroup.turbohire.co/careers", {"ats": "turbohire", "slug": "pinelabsgroup"}),
])
def test_url_patterns(url: str, expect: dict) -> None:
    r = ats_detect.detect_from_url(url)
    assert r is not None
    for k, v in expect.items():
        assert getattr(r, k) == v, (k, r)
    assert r.confidence == ats_detect.CONF_URL


@pytest.mark.parametrize("url", [
    "https://boards.greenhouse.io/embed/job_app?token=123",   # no slug present
    "https://www.turbohire.co/pricing",
    "https://example.com/careers",
])
def test_url_without_board_id(url: str) -> None:
    assert ats_detect.detect_from_url(url) is None


# ------------------------------------------------------- HTML fingerprints

def test_html_greenhouse_embed_script() -> None:
    r = detect_from_html(fx("openzeppelin_job.html"), "https://www.openzeppelin.com/jobs?gh_jid=6123456")
    assert (r.ats, r.slug, r.region) == ("greenhouse", "openzeppelin", None)
    assert r.confidence == ats_detect.CONF_LINK


def test_html_gh_jid_without_slug_flags_greenhouse_but_needs_detail() -> None:
    r = detect_from_html(fx("openzeppelin_careers.html"), "https://www.openzeppelin.com/jobs")
    assert r.ats == "greenhouse" and r.slug is None
    assert "gh_jid" in r.evidence


def test_html_phenom_apply_url_beats_footer_links() -> None:
    r = detect_from_html(fx("phenom_circle.html"), "https://careers.circle.com/us/en/search-results")
    assert (r.ats, r.tenant, r.wd_server, r.site) == ("workday", "circle", "wd1", "Circle")
    assert "Phenom" in r.evidence


def test_html_bundle_board_token() -> None:
    r = detect_from_html(fx("emergent_bundle.js"), "")
    assert (r.ats, r.slug) == ("greenhouse", "emergentlabsinc")
    assert "BOARD_TOKEN" in r.evidence


def test_html_escaped_json_and_entities() -> None:
    html = '<script>var x={"url":"https:\\/\\/jobs.ashbyhq.com\\/aiprise\\/abc"}</script>'
    assert detect_from_html(html, "https://aiprise.com/careers").slug == "aiprise"
    html2 = '<a href="https://boards.greenhouse.io/embed/job_board?b=1&amp;for=razorpaysoftwareprivatelimited">'
    assert detect_from_html(html2, "https://razorpay.com/jobs").slug == "razorpaysoftwareprivatelimited"


def test_html_most_cited_board_wins() -> None:
    html = ('<a href="https://jobs.lever.co/partner">x</a>'
            + '<a href="https://jobs.ashbyhq.com/supabase/1">a</a>' * 3)
    r = detect_from_html(html, "https://supabase.com/careers")
    assert (r.ats, r.slug) == ("ashby", "supabase")
    assert r.confidence < ats_detect.CONF_LINK      # a rival candidate lowers confidence
    assert "other candidate" in r.evidence


def test_html_nothing_found() -> None:
    r = detect_from_html(fx("plain_no_ats.html"), "https://example.com/careers")
    assert r.ats is None and r.confidence == 0.0
    assert r.evidence


# ------------------------------------------------- detect() with fake client

GH_LIST = "https://boards-api.greenhouse.io/v1/boards/"


def test_detect_url_only_needs_no_page_fetch_and_verifies_once() -> None:
    c = FakeClient(api={"https://api.ashbyhq.com/posting-api/job-board/litellm": {"jobs": [{}] * 15}})
    r = detect("https://jobs.ashbyhq.com/litellm", client=c)
    assert (r.ats, r.slug, r.verified, r.job_count) == ("ashby", "litellm", True, 15)
    assert c.calls == ["https://api.ashbyhq.com/posting-api/job-board/litellm"]


def test_detect_follows_redirect_final_url() -> None:
    c = FakeClient(pages={"https://www.litellm.ai/careers": ("https://jobs.ashbyhq.com/litellm", "<html></html>")})
    r = detect("https://www.litellm.ai/careers", client=c, verify=False)
    assert (r.ats, r.slug) == ("ashby", "litellm")
    assert "redirected" in r.evidence


def test_detect_emergent_iframe_then_bundle() -> None:
    c = FakeClient(
        pages={
            "https://emergent.sh/careers": fx("emergent_careers.html"),
            "https://jobs.emergent.example/": fx("emergent_iframe.html"),
            "https://jobs.emergent.example/assets/index-4f2a9c.js": fx("emergent_bundle.js"),
        },
        api={GH_LIST + "emergentlabsinc/jobs": {"jobs": [{}] * 51}},
    )
    r = detect("https://emergent.sh/careers", client=c)
    assert (r.ats, r.slug, r.verified, r.job_count) == ("greenhouse", "emergentlabsinc", True, 51)
    assert "iframe bundle" in r.evidence


def test_detect_openzeppelin_gh_jid_follows_one_detail_page() -> None:
    c = FakeClient(
        pages={
            "https://www.openzeppelin.com/jobs": fx("openzeppelin_careers.html"),
            "https://www.openzeppelin.com/jobs?gh_jid=6123456": fx("openzeppelin_job.html"),
        },
        api={GH_LIST + "openzeppelin/jobs": {"jobs": [{}] * 7}},
    )
    r = detect("https://www.openzeppelin.com/jobs", client=c)
    assert (r.ats, r.slug, r.job_count) == ("greenhouse", "openzeppelin", 7)
    assert r.confidence <= ats_detect.CONF_DETAIL
    assert sum("gh_jid" in u for u in c.calls) == 1


def test_detect_phenom_home_goes_to_search_results() -> None:
    c = FakeClient(
        pages={
            "https://careers.circle.com/us/en": fx("phenom_home.html"),
            "https://careers.circle.com/us/en/search-results": fx("phenom_circle.html"),
        },
        api={"https://circle.wd1.myworkdayjobs.com/wday/cxs/circle/Circle/jobs": {"total": 61}},
    )
    r = detect("https://careers.circle.com/us/en", client=c)
    assert (r.ats, r.tenant, r.site, r.job_count, r.verified) == ("workday", "circle", "Circle", 61, True)


def test_detect_eu_greenhouse_verifies_against_connector_endpoint() -> None:
    c = FakeClient(api={GH_LIST + "groww/jobs": {"jobs": [{}] * 9}})
    r = detect("https://job-boards.eu.greenhouse.io/groww", client=c)
    assert (r.region, r.job_count) == ("eu", 9)
    assert c.calls == [GH_LIST + "groww/jobs"]


def test_detect_nothing_found_is_manual() -> None:
    c = FakeClient(pages={"https://example.com/careers": fx("plain_no_ats.html")})
    r = detect("https://example.com/careers", client=c)
    assert r.ats == "manual" and not r.verified
    assert len(c.calls) <= ats_detect.MAX_FETCHES


@pytest.mark.parametrize("exc", [
    HttpConnectionError("dns failure"),
    RobotsDisallowed("https://example.com/careers"),
])
def test_detect_network_or_robots_failure_returns_none(exc: Exception) -> None:
    c = FakeClient(pages={"https://example.com/careers": exc})
    r = detect("https://example.com/careers", client=c)
    assert r.ats is None and r.confidence == 0.0
    assert "fetch failed" in r.evidence


def test_detect_denied_host_is_never_fetched() -> None:
    c = FakeClient(denied=("workatastartup.com",))
    r = detect("https://www.workatastartup.com/companies/x", client=c)
    assert r.ats is None and c.calls == []
    assert "do-not-automate" in r.evidence


def test_verify_404_keeps_detection_but_marks_it_doubtful() -> None:
    c = FakeClient(api={GH_LIST + "plaid/jobs": _status_error(GH_LIST + "plaid/jobs", 404)})
    r = detect("https://boards.greenhouse.io/plaid", client=c)
    assert r.ats == "greenhouse" and not r.verified and r.job_count is None
    assert r.confidence <= 0.2 and "HTTP 404" in r.evidence


def test_verify_robots_block_is_reported_not_raised() -> None:
    url = "https://api.smartrecruiters.com/v1/companies/Freshworks/postings"
    c = FakeClient(api={url: RobotsDisallowed(url)})
    r = detect("https://careers.smartrecruiters.com/Freshworks", client=c)
    assert r.ats == "smartrecruiters" and not r.verified
    assert "verify not possible" in r.evidence


# ------------------------------------------------------- to_company_config

def test_to_company_config_rows() -> None:
    gh = AtsResult("greenhouse", slug="groww", region="eu", verified=True, job_count=9, evidence="x")
    row = to_company_config(gh, "Groww", sector="fintech", tier="funded")
    assert row["connector"] == "greenhouse" and row["slug"] == "groww" and row["region"] == "eu"
    assert row["verify"] is False and row["sector"] == "fintech" and row["tier"] == "funded"
    assert "9 jobs" in row["note"]

    wd = AtsResult("workday", tenant="circle", wd_server="wd1", site="Circle")
    row = to_company_config(wd, "Circle")
    assert {k: row[k] for k in ("connector", "tenant", "wd_server", "site")} == {
        "connector": "workday", "tenant": "circle", "wd_server": "wd1", "site": "Circle"}
    assert row["verify"] is True and "slug" not in row and "region" not in row

    th = to_company_config(AtsResult("turbohire", slug="pinelabsgroup"), "Setu")
    assert th["connector"] == "manual" and th["ats"] == "turbohire"
    assert to_company_config(AtsResult(), "Nobody")["connector"] == "manual"
    # extra overrides anything computed
    assert to_company_config(gh, "Groww", note="hand note")["note"] == "hand note"


def test_detected_rows_load_in_registry_connectors() -> None:
    """A detected row must carry exactly the keys its connector reads."""
    from jobcopilot.connectors.gem import GemConnector
    from jobcopilot.connectors.smartrecruiters import SmartRecruitersConnector
    for conn, ats in ((GemConnector, "gem"), (SmartRecruitersConnector, "smartrecruiters")):
        row = to_company_config(AtsResult(ats, slug="acme"), "Acme")
        assert conn.list_url(row["slug"]).count("acme") == 1


# --------------------------------------------- ReDoS / untrusted input (safety-1)

import time as _time  # noqa: E402

_N = 2_000_000


def _fill(unit: str) -> str:
    return (unit * (_N // len(unit) + 1))[:_N]


ADVERSARIAL = {
    "word_run": "a" * _N,
    "dashes": _fill("a-"),
    "dots": _fill("a."),
    "workday_prefix": _fill("a.wd1."),
    "workday_long_label": _fill("a" * 63 + ".wd" + "1" * 50),
    "workday_cxs": _fill("a.wd1.myworkdayjobs.com/wday/cxs/"),
    "turbohire": _fill("a.turbohire.co"),
    "gh_embed": _fill("boards.greenhouse.io/embed/job_board?"),
    "open_a_tags": _fill("<a "),
    "open_a_long": _fill("<a " + "x" * 2047),
    "open_iframes": _fill("<iframe "),
    "open_scripts": _fill("<script "),
    "unclosed_apply": '"applyUrl":"' + "x" * _N,
    "phenom": _fill('"refNum"' + " " * 20),
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_patterns_are_linear_on_2mb_adversarial_input(name):
    # Before the fix 'a' * 40_000 alone took ~20 s in _scan (quadratic backtracking).
    text = ADVERSARIAL[name]
    t0 = _time.perf_counter()
    ats_detect._scan(text)
    for rx in (ats_detect._IFRAME, ats_detect._SCRIPT, ats_detect._HREF, ats_detect._APPLY_URL):
        rx.findall(text)
    assert _time.perf_counter() - t0 < 1.0


def test_detect_from_html_on_2mb_adversarial_page_is_fast():
    page = "<html>" + ADVERSARIAL["workday_cxs"] + ADVERSARIAL["open_a_tags"]
    t0 = _time.perf_counter()
    detect_from_html(page, "https://careers.example.com/")
    assert _time.perf_counter() - t0 < 1.0


def test_bounded_patterns_still_find_real_boards():
    html = ('<a class="btn primary" href="https://acme.wd5.myworkdayjobs.com/en-US/External">x</a>'
            '<iframe title="jobs" src="https://boards.greenhouse.io/embed/job_board?utm=x&amp;for=acme"></iframe>')
    wd, gh = html.split("</a>")
    assert (detect_from_html(wd, "").tenant, detect_from_html(wd, "").site) == ("acme", "External")
    assert detect_from_html(gh, "").slug == "acme"
    assert ats_detect._IFRAME.findall(html) == [
        "https://boards.greenhouse.io/embed/job_board?utm=x&amp;for=acme"]


# --------------------------------------- slugs from untrusted HTML (safety-4)

@pytest.mark.parametrize("html", [
    '<a href="https://jobs.ashbyhq.com/..%2F..%2Fadmin">jobs</a>',
    '<a href="https://jobs.ashbyhq.com/acme%2F..%2F..%3Fx%3D1">jobs</a>',
    '<a href="https://jobs.ashbyhq.com/%2e%2e">jobs</a>',
    '<a href="https://api.ashbyhq.com/posting-api/job-board/a%2Fb">jobs</a>',
])
def test_ashby_slug_that_would_change_the_api_path_is_refused(html):
    r = detect_from_html(html, "https://careers.example.com/")
    assert r.ats != "ashby" and r.slug is None


def test_ashby_slug_with_dots_still_accepted():
    r = detect_from_html('<a href="https://jobs.ashbyhq.com/acme.ai/123">x</a>', "")
    assert (r.ats, r.slug) == ("ashby", "acme.ai")


def test_verify_refuses_unsafe_ids_without_any_request():
    client = FakeClient()
    r = AtsResult("ashby", slug="../../admin", confidence=0.9)
    ats_detect.verify_result(r, client)
    assert client.calls == [] and not r.verified and r.confidence <= 0.2
    r = AtsResult("workday", tenant="acme", wd_server="wd1", site="x/../../y", confidence=0.9)
    ats_detect.verify_result(r, client)
    assert client.calls == []


def test_ashby_list_url_keeps_slug_in_one_segment():
    from jobcopilot.connectors.ashby import AshbyConnector
    base = "https://api.ashbyhq.com/posting-api/job-board/"
    assert AshbyConnector.list_url("acme") == base + "acme"
    assert AshbyConnector.list_url("a/../b?x=1") == base + "a%2F..%2Fb%3Fx%3D1"
    for bad in ("", ".", ".."):
        with pytest.raises(ValueError):
            AshbyConnector.list_url(bad)


# ------------------------------------------- fetch caps and budget (safety-2)

def test_fetches_pass_size_and_time_caps():
    page = '<iframe src="https://careers.example.com/frame"></iframe>'
    frame = '<script src="/app.js"></script>'
    client = FakeClient(pages={
        "https://careers.example.com/": page,
        "https://careers.example.com/frame": frame,
        "https://careers.example.com/app.js": "nothing here",
    })
    detect("https://careers.example.com/", client=client, verify=False)
    by_url = dict(zip(client.calls, client.kwargs))
    html_kw = by_url["https://careers.example.com/"]
    js_kw = by_url["https://careers.example.com/app.js"]
    assert html_kw["max_bytes"] == ats_detect.MAX_HTML_BYTES
    assert js_kw["max_bytes"] == ats_detect.MAX_JS_BYTES
    assert all(0 < kw["deadline_sec"] <= ats_detect.FETCH_DEADLINE_SEC for kw in client.kwargs)


def test_fetcher_stops_when_the_time_budget_is_spent():
    now = [0.0]
    client = FakeClient(pages={"https://a.example/": "x", "https://b.example/": "y"})
    f = ats_detect._Fetcher(client, AtsResult(), clock=lambda: now[0])
    assert f.get("https://a.example/") is not None
    now[0] = ats_detect.DETECT_BUDGET_SEC + 1
    assert f.get("https://b.example/") is None
    assert client.calls == ["https://a.example/"]
    assert "time budget" in f.result.evidence


def test_too_large_page_is_a_note_not_a_crash():
    from jobcopilot.http import ResponseTooLarge
    client = FakeClient(pages={"https://careers.example.com/":
                               ResponseTooLarge("https://careers.example.com/", 2_000_000)})
    r = detect("https://careers.example.com/", client=client, verify=False)
    assert r.ats is None and "ResponseTooLarge" in r.evidence
