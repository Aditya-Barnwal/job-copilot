"""Posting liveness (jobcopilot/liveness.py): the per-ATS API checks, the page
fallback, retiring dead jobs, the nightly queue pass with refill, the
check-live endpoint and autofill's refusal before a browser opens.

No network: every check gets a fake client (and liveness.check itself refuses
the real client under pytest)."""
from __future__ import annotations

import functools
import json

import pytest
import requests

from jobcopilot import http as H
from jobcopilot import liveness, store
from jobcopilot import queue as jq
from jobcopilot.filters import FilterResult
from jobcopilot.models import Job

WD_DEAD = ("https://mastercard.wd1.myworkdayjobs.com/en-US/corporatecareers/job/Pune-India/"
           "Software-Engineer-I_R-294428-1")


class Resp:
    def __init__(self, status=200, body="", url=""):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.url = url

    def json(self):
        return json.loads(self.text)


class FakeClient:
    """routes: {url-prefix: Resp | Exception}; records every GET (no POST exists here)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def is_denied(self, url):
        return "linkedin.com" in url

    def get(self, url, **kw):
        self.calls.append(url)
        for prefix, r in self.routes.items():
            if url.startswith(prefix):
                if isinstance(r, Exception):
                    raise r
                if not r.url:
                    r.url = url
                return r
        raise AssertionError(f"unexpected GET {url}")


@pytest.fixture(autouse=True)
def _fresh_cache():
    liveness.clear_cache()
    yield
    liveness.clear_cache()


def job(url, **kw):
    return {"key": "k1", "url": url, "apply_url": url, "company": "Acme", **kw}


# --------------------------------------------------------------------------- Workday (the Mastercard case)

WD_API = "https://mastercard.wd1.myworkdayjobs.com/wday/cxs/mastercard/corporatecareers/job/Pune-India/"


def test_workday_removed_posting_is_gone_even_though_the_page_is_200():
    c = FakeClient({WD_API: Resp(403, {"errorCode": "S22", "httpStatus": 403, "message": "permission denied"})})
    v = liveness.check(job(WD_DEAD), c)
    assert v["live"] is False and "S22" in v["reason"] and v["via"] == "api:workday"
    assert c.calls == [WD_API + "Software-Engineer-I_R-294428-1"]
    assert set(v) >= {"live", "reason", "checked_at"}


@pytest.mark.parametrize("resp,live", [
    (Resp(404, ""), False),
    (Resp(200, {"jobPostingInfo": {"title": "SE I", "posted": True, "canApply": True}}), True),
    (Resp(200, {"jobPostingInfo": {"title": "SE I", "posted": False}}), False),
    (Resp(200, {"hiringOrganization": {}}), False),
])
def test_workday_cxs_answers(resp, live):
    assert liveness.check(job(WD_DEAD), FakeClient({WD_API: resp}))["live"] is live


def test_workday_waf_403_or_outage_is_unknown_not_gone():
    shell = "<html><head><title></title></head><body><div id=root></div></body></html>"
    page = Resp(200, shell)
    for api in (Resp(403, "<html>Access denied</html>"), Resp(503, "busy")):
        c = FakeClient({WD_API: api, WD_DEAD: page})
        v = liveness.check(job(WD_DEAD), c)
        assert v["live"] is None, v


def test_network_error_and_robots_refusal_are_unknown():
    for exc in (H.HttpConnectionError("down"), requests.ConnectionError("down"),
                H.RobotsDisallowed(WD_DEAD), H.RetryLater(WD_DEAD, 30)):
        v = liveness.check(job(WD_DEAD), FakeClient({WD_API: exc}))
        assert v["live"] is None, (exc, v)


# --------------------------------------------------------------------------- Greenhouse / Lever / Ashby / Keka / SR

def test_greenhouse_api_by_id():
    url = "https://job-boards.greenhouse.io/acme/jobs/123"
    api = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123"
    assert liveness.check(job(url), FakeClient({api: Resp(404)}))["live"] is False
    assert liveness.check(job(url), FakeClient({api: Resp(200, {"id": 123})}))["live"] is True


def test_greenhouse_employer_page_uses_the_company_board(monkeypatch):
    from jobcopilot.autofill import service
    monkeypatch.setattr(service, "greenhouse_ids", lambda j, u, db=None: ("okta", "7906186"))
    api = "https://boards-api.greenhouse.io/v1/boards/okta/jobs/7906186"
    v = liveness.check(job("https://www.okta.com/company/careers/opportunity/7906186?gh_jid=7906186"),
                       FakeClient({api: Resp(404)}))
    assert v["live"] is False and v["via"] == "api:greenhouse"


def test_lever_api_by_id():
    uid = "7301faf3-d7ec-40d4-b49a-07ed20fc9691"
    api = f"https://api.lever.co/v0/postings/cred/{uid}"
    for url in (f"https://jobs.lever.co/cred/{uid}", f"https://jobs.lever.co/cred/{uid}/apply"):
        assert liveness.check(job(url), FakeClient({api: Resp(404)}))["live"] is False
        assert liveness.check(job(url), FakeClient({api: Resp(200, {"id": uid})}))["live"] is True


def test_ashby_board_list_and_one_call_per_board():
    a, b = "42f18ccd-c4c8-4a85-8c1f-de12c575fe87", "49bdf081-6e20-4323-8c73-6d6b19544ff5"
    api = "https://api.ashbyhq.com/posting-api/job-board/notion"
    c = FakeClient({api: Resp(200, {"jobs": [{"id": a}]})})
    assert liveness.check(job(f"https://jobs.ashbyhq.com/notion/{a}"), c)["live"] is True
    assert liveness.check(job(f"https://jobs.ashbyhq.com/notion/{b}/application"), c)["live"] is False
    assert c.calls == [api]


def test_keka_active_list():
    shell = '<html><img src="/ats/documents/52d63d06-bc61-437e-a156-09e99afce260/logo.png"></html>'
    c = FakeClient({"https://attero.keka.com/careers/api/embedjobs/default/active/": Resp(200, [{"id": 84493}]),
                    "https://attero.keka.com/careers/": Resp(200, shell)})
    assert liveness.check(job("https://attero.keka.com/careers/jobdetails/84493"), c)["live"] is True
    v = liveness.check(job("https://attero.keka.com/careers/applyjob/1"), c)
    assert v["live"] is False and v["via"] == "api:keka"
    assert len(c.calls) == 2                              # shell + list, once for the tenant


def test_keka_list_unavailable_is_unknown_and_the_page_is_not_asked():
    c = FakeClient({"https://attero.keka.com/careers/": Resp(503)})
    v = liveness.check(job("https://attero.keka.com/careers/jobdetails/84493"), c)
    assert v["live"] is None and all("jobdetails" not in u for u in c.calls)


def test_smartrecruiters_api():
    url = "https://jobs.smartrecruiters.com/Acme/743999912345678-backend-engineer"
    api = "https://api.smartrecruiters.com/v1/companies/Acme/postings/743999912345678"
    assert liveness.check(job(url), FakeClient({api: Resp(404)}))["live"] is False


# --------------------------------------------------------------------------- the page fallback

@pytest.mark.parametrize("body,live", [
    ("<html><body><h1>The page you are looking for doesn't exist.</h1></body></html>", False),
    ("<p>Sorry! This position has been filled.</p>", False),
    ("<p>This job is no longer available.</p>", False),
    ("<p>Job not found</p>", False),
    ("<p>We are no longer accepting applications for this role.</p>", False),
    ("<h1>Backend Engineer</h1><p>Apply now!</p>", True),
    ("<script>t={missing:'Job not found'}</script><h1>Backend Engineer</h1>", True),   # script text ignored
])
def test_page_markers(body, live):
    url = "https://careers.example.com/jobs/42"
    assert liveness.check(job(url), FakeClient({url: Resp(200, body)}))["live"] is live


@pytest.mark.parametrize("code,live", [(404, False), (410, False), (403, None), (429, None), (500, None)])
def test_page_status_codes(code, live):
    url = "https://careers.example.com/jobs/42"
    assert liveness.check(job(url), FakeClient({url: Resp(code, "")}))["live"] is live


def test_board_error_redirect_is_gone():
    url = "https://careers.example.com/jobs/42"
    r = Resp(200, "<h1>Open roles</h1>", url="https://careers.example.com/jobs?error=true")
    assert liveness.check(job(url), FakeClient({url: r}))["live"] is False


def test_denied_host_and_no_url_are_not_checked():
    c = FakeClient({})
    assert liveness.check(job("https://www.linkedin.com/jobs/view/1"), c)["live"] is None
    assert liveness.check({"key": "x", "url": ""}, c)["live"] is None
    assert c.calls == []


def test_real_client_is_refused_under_pytest():
    v = liveness.check(job("https://job-boards.greenhouse.io/acme/jobs/1"))
    assert v["live"] is None and "tests" in v["reason"]


# --------------------------------------------------------------------------- store + nightly pass

def seed(ext, *, company, fit, url=None, lane="main"):
    j = Job(source="greenhouse", company=company, company_sector="tech", external_id=ext,
            title="Software Engineer", location_text="Bengaluru", workplace_type="onsite",
            country_bucket="IN", url=url or f"https://careers.{company.lower()}.example/jobs/{ext}",
            description_text="Python")
    r = FilterResult(job=j, package_estimated=True, country_uncertain=False)
    r.evaluation = {"lane": lane, "excluded": 0, "exclusion_reasons": [], "flags": [], "min_yoe_required": None}
    store.upsert_jobs([r])
    store.set_fit(j.key, {"fit_score_v2": fit, "fit_components": {"components": {"skills": fit},
                                                                  "weights": {"skills": 1.0}},
                          "matched_skills": [], "missing_skills": [], "fit_why": ""})
    return j.key


def test_mark_gone_sets_gone_at_and_leaves_the_queue():
    k = seed("1", company="Acme", fit=90)
    jq.build_daily_queue(day="2026-11-19")
    assert [i["key"] for i in jq.list_queue(day="2026-11-19")["items"]] == [k]
    assert liveness.mark_gone(k, {"checked_at": 123.0})
    row = store.get_job(k)
    assert row["gone_at"] == 123.0 and row["fill_status"] == "gone"
    assert jq.list_queue(day="2026-11-19")["items"] == []
    assert liveness.mark_gone("nope") is False


def test_check_queue_retires_dead_jobs_and_refills(monkeypatch):
    cfg = {"queue": {"daily_target_min": 1, "daily_target_max": 2, "max_per_company": 2,
                     "extended_top_n": 0, "near_miss_max": 0},
           "yoe": {"max_required": 2}, "companies": {"include_extended": False}}
    monkeypatch.setattr(jq, "build_daily_queue", functools.partial(jq.build_daily_queue, cfg=cfg))
    a = seed("a", company="Alpha", fit=90)
    b = seed("b", company="Beta", fit=80)
    c = seed("c", company="Gamma", fit=70)
    d = seed("d", company="Delta", fit=60)
    jq.build_daily_queue(day="2026-11-19")
    assert [i["key"] for i in jq.list_queue(day="2026-11-19")["items"]] == [a, b]
    dead = "<p>This position has been filled.</p>"
    client = FakeClient({
        "https://careers.alpha.example/": Resp(200, dead),           # a: gone
        "https://careers.beta.example/": Resp(200, "<h1>ok</h1>"),
        "https://careers.gamma.example/": Resp(404),                 # c (refill): gone too
        "https://careers.delta.example/": Resp(200, "<h1>ok</h1>"),
    })
    out = liveness.check_queue(day="2026-11-19", client=client)
    assert out["gone_keys"] == [a, c] and out["refills"] == 2 and out["checked"] == 4
    assert [i["key"] for i in jq.list_queue(day="2026-11-19")["items"]] == [b, d]
    assert store.get_job(a)["gone_at"] and store.get_job(c)["gone_at"] and not store.get_job(b)["gone_at"]
    assert out["queue"]["counts"]["main"] == 2


def test_unknown_never_retires(monkeypatch):
    k = seed("u", company="Acme", fit=90)
    jq.build_daily_queue(day="2026-11-19")
    out = liveness.check_queue(day="2026-11-19",
                               client=FakeClient({"https://careers.acme.example/": H.HttpConnectionError("x")}))
    assert out["unknown"] == 1 and out["gone"] == 0 and not store.get_job(k)["gone_at"]
    assert out["queue"] is None


# --------------------------------------------------------------------------- API + autofill refusal

def test_check_live_endpoint(monkeypatch):
    from starlette.testclient import TestClient
    from server import app as server_app
    k = seed("e", company="Acme", fit=90)
    verdict = {"live": False, "reason": "the posting page says: “Job not found”", "checked_at": 5.0, "via": "page"}
    monkeypatch.setattr(liveness, "cached_check", lambda j, client=None, db_path=None: dict(verdict))
    with TestClient(server_app.app) as tc:
        r = tc.post(f"/api/jobs/{k}/check-live")
        assert r.status_code == 200 and r.json() == {"live": False, "reason": verdict["reason"],
                                                     "checked_at": 5.0}
        assert store.get_job(k)["gone_at"] == 5.0
        assert tc.post("/api/jobs/nope/check-live").status_code == 404
        verdict.update(live=None, reason="couldn't reach the site")
        k2 = seed("f", company="Beta", fit=80)
        r = tc.post(f"/api/jobs/{k2}/check-live")
        assert r.json()["live"] is None and not store.get_job(k2)["gone_at"]


def test_autofill_refuses_a_closed_posting_before_opening_a_browser(monkeypatch, tmp_path):
    from jobcopilot.autofill import service
    k = seed("g", company="Acme", fit=90, url="https://job-boards.greenhouse.io/acme/jobs/9")
    calls = []

    class Backend:
        def check_live(self, j, db_path=None):
            calls.append("check_live")
            return {"live": False, "reason": "Greenhouse says the posting is gone (HTTP 404)",
                    "checked_at": 7.0, "via": "api:greenhouse"}

        def open_headed(self, cfg):          # must never be reached
            calls.append("open_headed")
            raise AssertionError("browser opened for a closed posting")

    monkeypatch.setattr(service, "BACKEND", Backend())
    monkeypatch.setattr(service, "_LIVE", {})
    with pytest.raises(service.AutofillRefused) as e:
        service.launch_fill(k, background=False)
    assert e.value.code == "gone" and e.value.detail.startswith("this posting is closed")
    assert calls == ["check_live"] and store.get_job(k)["gone_at"] == 7.0
    assert service.needs_you() == []                          # no session was opened


def test_autofill_goes_ahead_when_liveness_is_unknown(monkeypatch):
    from jobcopilot.autofill import service
    k = seed("h", company="Acme", fit=90, url="https://job-boards.greenhouse.io/acme/jobs/10")

    class Backend:
        def check_live(self, j, db_path=None):
            return {"live": None, "reason": "couldn't reach the site", "checked_at": 1.0}

    monkeypatch.setattr(service, "BACKEND", Backend())
    monkeypatch.setattr(service, "_LIVE", {})
    monkeypatch.setattr(service, "_worker", lambda live: None)
    assert service.launch_fill(k, background=False) > 0
    assert not store.get_job(k)["gone_at"]
