"""Offline tests for the Phase 1b ATS connectors (Recruitee, Workable, Rippling,
Teamtailor, Pinpoint, Personio, BambooHR, Keka) and the slug/tenant hardening of
the older Greenhouse / Lever / SmartRecruiters / Gem / Workday connectors.

tests/fixtures/more_ats/ holds trimmed real responses captured 2026-11-12 (bodies
cut to a few hundred chars, emails/phones scrubbed), except workable_acme.json,
which follows the documented widget shape because the two live accounts checked
had no open jobs. FakeClient records every URL, so "which requests would this
connector make" is asserted without any network.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jobcopilot.connectors.bamboohr import BambooHRConnector
from jobcopilot.connectors.gem import GemConnector
from jobcopilot.connectors.greenhouse import GreenhouseConnector
from jobcopilot.connectors.keka import KekaConnector, org_id_from_html
from jobcopilot.connectors.lever import LeverConnector
from jobcopilot.connectors.personio import PersonioConnector
from jobcopilot.connectors.pinpoint import PinpointConnector
from jobcopilot.connectors.recruitee import RecruiteeConnector, utc_text_to_iso
from jobcopilot.connectors.rippling import RipplingConnector
from jobcopilot.connectors.smartrecruiters import SmartRecruitersConnector
from jobcopilot.connectors.teamtailor import TeamtailorConnector, xml_root
from jobcopilot.connectors.workable import WorkableConnector
from jobcopilot.connectors.workday import WorkdayConnector
from jobcopilot.http import RobotsTxt
from jobcopilot.models import HYBRID, IN, ONSITE, REMOTE, UNKNOWN, Job

FIX = Path(__file__).resolve().parent / "fixtures" / "more_ats"
ORG = "ba76ae62-6aee-446a-a292-a7c27c5304cc"


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class _Resp:
    def __init__(self, body: str) -> None:
        self.text = body


class FakeClient:
    """get / get_json / post_json that serve canned bodies by URL and log each call."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes, self.calls = routes, []

    def _hit(self, url: str, params: Any = None) -> Any:
        self.calls.append((url, dict(params) if params else None))
        return self.routes[url]

    def get_json(self, url: str, params: Any = None, **_: Any) -> Any:
        val = self._hit(url, params)
        return val(params) if callable(val) else val

    def get(self, url: str, params: Any = None, **_: Any) -> _Resp:
        return _Resp(self._hit(url, params))

    def post_json(self, url: str, **kw: Any) -> Any:
        self.calls.append((url, kw.get("json")))
        return self.routes[url]

    @property
    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


# ------------------------------------------------------------------ Recruitee

def test_recruitee_parse() -> None:
    jobs = RecruiteeConnector().parse(load("recruitee_box8.json"),
                                      {"name": "Box8", "slug": "box8", "sector": "foodtech"})
    assert len(jobs) == 3 and all(j.source == "recruitee" and j.company_sector == "foodtech" for j in jobs)
    a = jobs[0]
    assert a.title == "Talent Acquisition Specialist" and a.external_id == "316595"
    assert a.location_text == "Bangalore, Karnātaka, India" and a.country_bucket == IN
    assert a.workplace_type == ONSITE and a.department == "Operations"
    assert a.url == "https://box8.recruitee.com/o/area-business-partner"
    assert a.apply_url == a.url + "/c/new"
    assert a.posted_at == "2025-04-07T17:08:31+00:00" and a.description_text
    assert "<" not in a.description_text


def test_recruitee_flags_salary_and_unpublished() -> None:
    raw = {"offers": [
        {"id": 1, "title": "Remote dev", "status": "published", "remote": True, "location": "",
         "careers_url": "https://x.recruitee.com/o/a", "published_at": "2026-10-23 14:59:10 UTC",
         "salary": {"min": "100000", "max": "150000", "currency": "EUR", "period": "year"}},
        {"id": 2, "title": "Hybrid", "hybrid": True, "location": "Pune, India"},
        {"id": 3, "title": "Draft", "status": "draft"},
    ]}
    a, b = RecruiteeConnector().parse(raw, {"name": "X", "slug": "x"})
    assert a.workplace_type == REMOTE and b.workplace_type == HYBRID
    assert a.posted_at == "2026-10-23T14:59:10+00:00"
    assert (a.compensation.currency, a.compensation.min_value, a.compensation.max_value,
            a.compensation.interval) == ("EUR", 100000.0, 150000.0, "1 YEAR")
    assert utc_text_to_iso(None) is None and utc_text_to_iso("2026-01-02") == "2026-01-02"


# ------------------------------------------------------------------- Workable

def test_workable_parse() -> None:
    jobs = WorkableConnector().parse(load("workable_acme.json"), {"name": "Acme", "slug": "acme"})
    a, b, c = jobs
    assert a.external_id == "A1B2C3D4E5" and a.workplace_type == REMOTE and a.country_bucket == IN
    assert a.apply_url.endswith("/apply") and a.department == "Engineering" and a.posted_at == "2026-11-02"
    # hidden locations are never shown; telecommuting=false is not 'onsite'
    assert b.location_text == "Bengaluru, Karnataka, India; Pune, Maharashtra, India"
    assert "Secret" not in b.location_text and b.workplace_type == UNKNOWN
    assert c.country_bucket == "non-IN" and c.department is None


# ------------------------------------------------------------------- Rippling

def test_rippling_parse_and_workplace_types() -> None:
    raw = load("rippling_koreai.json")
    jobs = RipplingConnector().parse(raw, {"name": "Kore.ai", "slug": "koreai-india-careers"})
    assert jobs[0].title == "Cloud & Infrastructure Security Architect"
    assert jobs[0].location_text == "Hyderabad, India" and jobs[0].country_bucket == IN
    assert jobs[0].workplace_type == ONSITE and jobs[0].department == "Common Shared Services"
    raw["items"][0]["locations"].append({"name": "Remote (India)", "workplaceType": "REMOTE"})
    assert RipplingConnector().parse(raw, {"name": "K"})[0].workplace_type == REMOTE


def _rippling_pages(total: int, per: int = 20) -> Any:
    def serve(params: Any) -> dict:
        page = int((params or {}).get("page", 0))
        n = max(0, min(per, total - page * per))
        return {"items": [{"id": f"id{page * per + i}", "name": "x"} for i in range(n)],
                "page": page, "pageSize": per, "totalItems": total, "totalPages": -(-total // per)}
    return serve


def test_rippling_paginates_politely_and_caps() -> None:
    url = RipplingConnector.list_url("acme")
    c = FakeClient({url: _rippling_pages(45)})
    raw = RipplingConnector(client=c).fetch_raw({"name": "A", "slug": "acme"})
    assert len(raw["items"]) == 45 and c.calls == [(url, None), (url, {"page": 1}), (url, {"page": 2})]
    c = FakeClient({url: _rippling_pages(500)})
    raw = RipplingConnector(client=c).fetch_raw({"name": "A", "slug": "acme", "max": 30})
    assert len(raw["items"]) == 30 and len(c.calls) == 2      # stops once the cap is reached


def test_rippling_stops_if_the_server_ignores_the_page_parameter() -> None:
    url = RipplingConnector.list_url("acme")
    same = {"items": [{"id": "a"}, {"id": "b"}], "page": 0, "totalItems": 99, "totalPages": 5}
    c = FakeClient({url: same})
    raw = RipplingConnector(client=c).fetch_raw({"name": "A", "slug": "acme"})
    assert len(raw["items"]) == 2 and len(c.calls) == 2


# ----------------------------------------------------------------- Teamtailor

def test_teamtailor_parse() -> None:
    jobs = TeamtailorConnector().parse(text("teamtailor_veoneerin.rss"), {"name": "Veoneer", "slug": "veoneerin"})
    a, b, c = jobs
    assert a.title.startswith("New Project Introduction") and a.external_id == "3b6d2dc8-0051-4af7-8e27-c6ece60cb987"
    assert a.location_text == "Ranjangaon, India" and a.country_bucket == IN and a.workplace_type == ONSITE
    assert a.url == "https://veoneerin.teamtailor.com/jobs/8570724-new-project-introduction-npi-engineer-5-8-years"
    assert a.posted_at == "2026-10-19T19:58:39+05:30" and "<" not in a.description_text
    assert b.department == "India Manufacturing Footprint" and a.department is None
    assert c.workplace_type == HYBRID and c.location_text == "Bengaluru, India"


def test_teamtailor_fully_remote_without_a_location_says_remote() -> None:
    rss = ('<rss xmlns:tt="https://teamtailor.com/locations"><channel><item><title>T</title>'
           '<link>https://a.teamtailor.com/jobs/1-t</link><guid>g1</guid><remoteStatus>fully</remoteStatus>'
           '</item></channel></rss>')
    (job,) = TeamtailorConnector().parse(rss, {"name": "A", "slug": "a"})
    assert job.workplace_type == REMOTE and job.location_text == "Remote"


@pytest.mark.parametrize("body", [
    '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel/></rss>',
    '<!DOCTYPE r><rss/>',
    '<?xml version="1.0"?><!-- <x --><!DOCTYPE r [<!ENTITY a "b">]><rss>&a;</rss>',   # comment hiding it
    '﻿<?xml version="1.0"?>\n<?pi x?><!--c-->\n<!doctype r><rss/>',
])
def test_xml_feeds_with_a_doctype_are_refused(body: str) -> None:
    with pytest.raises(ValueError, match="DOCTYPE"):
        xml_root(body)
    with pytest.raises(ValueError):
        TeamtailorConnector().parse(body, {"name": "A"})


_TT_UTF16 = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel/></rss>'


@pytest.mark.parametrize("body", [
    _TT_UTF16.encode("utf-16"), _TT_UTF16.encode("utf-16-le"), _TT_UTF16.encode("utf-16-be"),
    _TT_UTF16.encode("utf-32"), _TT_UTF16.encode("utf-32-be"),
    b"\xff\xfe" + _TT_UTF16.encode("utf-16-le"), b"\xfe\xff" + _TT_UTF16.encode("utf-16-be"),
    b'<?xml version="1.0" encoding="UTF-16"?><rss/>',
])
def test_xml_feeds_in_utf16_utf32_are_refused(body: bytes) -> None:
    """S5: a UTF-16 body with a DOCTYPE must not slip past the ASCII prolog check."""
    with pytest.raises(ValueError):
        xml_root(body)
    for conn in (TeamtailorConnector(), PersonioConnector()):
        with pytest.raises(ValueError):
            conn.parse(body, {"name": "A", "slug": "a"})


def test_xml_root_accepts_latin1_and_utf8_declarations() -> None:
    assert xml_root('<?xml version="1.0" encoding="UTF-8"?><r/>'.encode()).tag == "r"
    assert xml_root('<?xml version="1.0" encoding="iso-8859-1"?><r>é</r>'.encode("latin-1")).tag == "r"


def test_xml_root_accepts_cdata_that_mentions_doctype_and_empty_bodies() -> None:
    assert xml_root('<?xml version="1.0"?><r><![CDATA[<!DOCTYPE html>]]></r>').tag == "r"
    assert xml_root("") is None and xml_root(None) is None
    assert TeamtailorConnector().parse("", {"name": "A"}) == []


# ------------------------------------------------------------------- Pinpoint

def test_pinpoint_parse() -> None:
    a, b, c = PinpointConnector().parse(load("pinpoint_accenture.json"), {"name": "Accenture", "slug": "accenture"})
    assert a.title.startswith("Leeds Software Engineering") and a.country_bucket == "non-IN"
    assert a.location_text == "Leeds, United Kingdom" and a.workplace_type == ONSITE
    assert a.department == "Technology UK" and a.posted_at is None
    assert a.url.startswith("https://accenture.pinpointhq.com/en/postings/") and a.description_text
    assert "36,400" in a.compensation.summary
    assert c.workplace_type == HYBRID and c.compensation is None


# -------------------------------------------------------------------- Personio

def test_personio_parse() -> None:
    company = {"name": "1KOMMA5", "slug": "1komma5grad", "region": "de"}
    a, b, c = PersonioConnector().parse(text("personio_1komma5.xml"), company)
    assert a.external_id == "2800938" and a.title.startswith("(Junior) SEO")
    assert a.url == "https://1komma5grad.jobs.personio.de/job/2800938"
    assert a.location_text == "Berlin; Berlin (Remote)" and a.workplace_type == REMOTE
    assert a.department == "Marketing" and a.posted_at == "2026-09-17T07:57:12+00:00"
    assert "<" not in a.description_text and a.description_text
    assert b.location_text == "Remote" and b.workplace_type == REMOTE
    assert c.location_text == "Hamburg" and c.country_bucket in ("non-IN", UNKNOWN)
    com = PersonioConnector().parse(text("personio_1komma5.xml"), {"name": "X", "slug": "x"})
    assert com[0].url == "https://x.jobs.personio.com/job/2800938"


def test_personio_region_is_com_or_de_only() -> None:
    assert PersonioConnector.list_url("acme") == "https://acme.jobs.personio.com/xml"
    assert PersonioConnector.list_url("acme", "DE") == "https://acme.jobs.personio.de/xml"
    with pytest.raises(ValueError):
        PersonioConnector.list_url("acme", "evil.example/")
    c = FakeClient({"https://acme.jobs.personio.de/xml": text("personio_1komma5.xml")})
    jobs = PersonioConnector(client=c).fetch({"name": "A", "slug": "acme", "region": "de"})
    assert len(jobs) == 3 and c.urls == ["https://acme.jobs.personio.de/xml"]


# -------------------------------------------------------------------- BambooHR

def test_bamboohr_parse() -> None:
    a, b, c = BambooHRConnector().parse(load("bamboohr_curtinmaritime.json"),
                                        {"name": "Curtin Maritime", "slug": "curtinmaritime"})
    assert a.title == "Vessel Chef" and a.external_id == "201" and a.department == "Marine Transportation"
    assert a.location_text == "Long Beach, California" and a.workplace_type == ONSITE
    assert a.url == a.apply_url == "https://curtinmaritime.bamboohr.com/careers/201"
    # location comes from atsLocation when the legacy field is empty; locationType 1/2
    assert b.location_text == "Long Beach, California, United States" and b.workplace_type == REMOTE
    assert c.workplace_type == HYBRID


def test_bamboohr_empty_and_html_redirect_payloads_mean_no_jobs() -> None:
    p = BambooHRConnector()
    assert p.parse({"meta": {"totalCount": 0}, "result": []}, {"name": "T", "slug": "toggl"}) == []
    assert p.parse(None, {"name": "T", "slug": "toggl"}) == []
    assert p.parse({"result": [{"jobOpeningName": "no id"}]}, {"name": "T", "slug": "t"}) == []


# ------------------------------------------------------------------------ Keka

def test_keka_parse() -> None:
    a, b, c = KekaConnector().parse(load("keka_lambdatest_jobs.json"), {"name": "TestMu AI", "slug": "lambdatest"})
    assert a.title == "Senior Security Engineer" and a.external_id == "146267"
    assert a.location_text == "Noida, India" and a.country_bucket == IN
    assert a.url == a.apply_url == "https://lambdatest.keka.com/careers/jobdetails/146267"
    assert a.department == "Platform Team- DevOps & Data Center" and a.posted_at == "2026-11-02T09:12:56.437Z"
    assert a.compensation is None and "<" not in a.description_text and a.description_text
    assert b.location_text == "" and b.workplace_type == UNKNOWN
    assert "description" not in a.raw


def test_keka_salary_only_when_a_maximum_is_set() -> None:
    row = {"id": 1, "title": "T", "salaryRangeFormat": "INR 10-20 LPA",
           "salaryRange": {"currency": "INR", "minimum": 1000000, "maximum": 2000000, "salaryPeriod": 4}}
    (job,) = KekaConnector().parse([row], {"name": "A", "slug": "a"})
    comp = job.compensation
    assert (comp.currency, comp.min_value, comp.max_value, comp.interval) == ("INR", 1000000, 2000000, "1 YEAR")
    row["salaryRange"]["salaryPeriod"] = 2          # bi-weekly: no interval to claim
    assert KekaConnector().parse([row], {"name": "A", "slug": "a"})[0].compensation.interval is None


def test_keka_org_id_comes_from_the_shell_text_only() -> None:
    assert org_id_from_html(text("keka_lambdatest.html")) == ORG
    assert org_id_from_html("<html>nothing</html>") is None and org_id_from_html(None) is None


def test_keka_fetch_is_two_requests_both_under_careers_and_robots_allows_them() -> None:
    page, api = "https://lambdatest.keka.com/careers/", KekaConnector.list_url("lambdatest", ORG)
    c = FakeClient({page: text("keka_lambdatest.html"), api: load("keka_lambdatest_jobs.json")})
    jobs = KekaConnector(client=c).fetch({"name": "T", "slug": "lambdatest"})
    assert len(jobs) == 3 and c.urls == [page, api]
    assert api == f"https://lambdatest.keka.com/careers/api/embedjobs/default/active/{ORG}"
    robots = RobotsTxt("User-agent: *\nDisallow: /\nAllow: /careers\nAllow: /careers/\n",
                       "job-copilot/2.0 (personal, non-commercial)")
    assert all(robots.allows(u) for u in c.urls)
    assert not robots.allows(f"https://lambdatest.keka.com/ats/documents/{ORG}/careerportal/x.html")
    # a config row that carries org_id needs one request
    c = FakeClient({api: load("keka_lambdatest_jobs.json")})
    KekaConnector(client=c).fetch({"name": "T", "slug": "lambdatest", "org_id": ORG})
    assert c.urls == [api]


def test_keka_without_a_portal_or_with_a_bad_org_id_fails_loudly() -> None:
    c = FakeClient({"https://nobody.keka.com/careers/": "<html>login</html>"})
    with pytest.raises(ValueError, match="no Keka careers portal"):
        KekaConnector(client=c).fetch({"name": "N", "slug": "nobody"})
    with pytest.raises(ValueError):
        KekaConnector.list_url("lambdatest", "../../x")
    c = FakeClient({KekaConnector.list_url("a", ORG): {"error": "x"}})
    with pytest.raises(ValueError, match="not a list"):
        KekaConnector(client=c).fetch({"name": "A", "slug": "a", "org_id": ORG})


# ------------------------------------------- list URLs: validated, one segment

HOSTILE = ["", ".", "..", "a/b", "a/../b", "a?x=1", "a#f", "a b", "a@evil.example", "a%2Fb", "é", "a" * 101,
           "-a", "a\nb", None]
HOST_BASED = [(RecruiteeConnector, "https://{}.recruitee.com/api/offers/"),
              (TeamtailorConnector, "https://{}.teamtailor.com/jobs.rss"),
              (PinpointConnector, "https://{}.pinpointhq.com/postings.json"),
              (PersonioConnector, "https://{}.jobs.personio.com/xml"),
              (BambooHRConnector, "https://{}.bamboohr.com/careers/list"),
              (KekaConnector, None)]
PATH_BASED = [(WorkableConnector, "https://apply.workable.com/api/v1/widget/accounts/{}"),
              (RipplingConnector, "https://ats.rippling.com/api/v2/board/{}/jobs"),
              (GreenhouseConnector, "https://boards-api.greenhouse.io/v1/boards/{}/jobs"),
              (LeverConnector, "https://api.lever.co/v0/postings/{}"),
              (SmartRecruitersConnector, "https://api.smartrecruiters.com/v1/companies/{}/postings"),
              (GemConnector, "https://api.gem.com/job_board/v0/{}/job_posts/")]


def _url(conn: Any, slug: Any) -> str:
    return conn.list_url(slug, ORG) if conn is KekaConnector else conn.list_url(slug)


@pytest.mark.parametrize("conn, template", HOST_BASED)
def test_host_based_slugs_must_be_one_dns_label(conn: Any, template: Any) -> None:
    if template:
        assert _url(conn, "Acme-1") == template.format("acme-1")
    for bad in HOSTILE + ["a.b", "a_b", "a-", "evil.com/x"]:
        with pytest.raises(ValueError):
            _url(conn, bad)


@pytest.mark.parametrize("conn, template", PATH_BASED)
def test_path_based_slugs_are_validated_and_stay_in_one_segment(conn: Any, template: str) -> None:
    assert _url(conn, "acme.ai-2_x") == template.format("acme.ai-2_x")
    for bad in HOSTILE:
        with pytest.raises(ValueError):
            _url(conn, bad)


def test_lever_region_and_greenhouse_region() -> None:
    assert LeverConnector.list_url("cred") == "https://api.lever.co/v0/postings/cred"
    assert LeverConnector.list_url("acme", "eu") == "https://api.eu.lever.co/v0/postings/acme"
    with pytest.raises(ValueError):
        LeverConnector.list_url("acme", "mars")
    c = FakeClient({"https://api.eu.lever.co/v0/postings/acme": []})
    LeverConnector(client=c).fetch({"name": "A", "slug": "acme", "region": "eu"})
    assert c.urls == ["https://api.eu.lever.co/v0/postings/acme"]
    with pytest.raises(ValueError):
        GreenhouseConnector.list_url("../x", "us")


def test_fetch_raw_rejects_a_hostile_slug_before_any_request() -> None:
    for conn in (RecruiteeConnector, WorkableConnector, RipplingConnector, TeamtailorConnector,
                 PinpointConnector, PersonioConnector, BambooHRConnector, GreenhouseConnector,
                 LeverConnector, SmartRecruitersConnector, GemConnector, KekaConnector):
        c = FakeClient({})
        with pytest.raises(ValueError):
            conn(client=c).fetch_raw({"name": "X", "slug": "a/../../b", "org_id": ORG})
        assert c.calls == []


def test_every_new_connector_fetches_from_its_documented_url() -> None:
    cases = [
        (RecruiteeConnector, "https://box8.recruitee.com/api/offers/", load("recruitee_box8.json")),
        (WorkableConnector, "https://apply.workable.com/api/v1/widget/accounts/acme", load("workable_acme.json")),
        (PinpointConnector, "https://accenture.pinpointhq.com/postings.json", load("pinpoint_accenture.json")),
        (BambooHRConnector, "https://curtinmaritime.bamboohr.com/careers/list", load("bamboohr_curtinmaritime.json")),
        (TeamtailorConnector, "https://veoneerin.teamtailor.com/jobs.rss", text("teamtailor_veoneerin.rss")),
    ]
    for conn, url, payload in cases:
        slug = url.split("//")[1].split(".")[0] if "workable" not in url else "acme"
        c = FakeClient({url: payload})
        jobs = conn(client=c).fetch({"name": "X", "slug": slug})
        assert jobs and c.urls == [url], conn
        assert all(isinstance(j, Job) and j.title and j.external_id for j in jobs)


# -------------------------------------------- hardening of the older connectors

def test_greenhouse_gem_smartrecruiters_job_links_quote_ids() -> None:
    gh = GreenhouseConnector().parse({"jobs": [{"id": 1, "title": "t"}]}, {"name": "A", "slug": "acme"})
    assert gh[0].url == "https://job-boards.greenhouse.io/acme/jobs/1"
    gem = GemConnector().parse({"job_posts": [{"id": "x/../y", "title": "t"}]}, {"name": "A", "slug": "acme"})
    assert gem[0].url == "https://jobs.gem.com/acme/x%2F..%2Fy"
    sr = SmartRecruitersConnector().parse(
        {"content": [{"id": "123", "name": "t", "company": {"identifier": "A/B"}}]}, {"name": "A", "slug": "acme"})
    assert sr[0].url == "https://jobs.smartrecruiters.com/A%2FB/123"


def test_smartrecruiters_detail_follows_ref_only_on_its_own_api_host() -> None:
    conn = SmartRecruitersConnector
    own = "https://api.smartrecruiters.com/v1/companies/acme/postings/9"
    for ref, expect in ((own, own),
                        ("https://evil.example/steal", f"{own}"),
                        ("", own)):
        c = FakeClient({expect: {"jobAd": {"sections": {"jobDescription": {"text": "<p>Hi</p>"}}}}})
        job = Job(source="smartrecruiters", company="A", company_sector=None, external_id="9",
                  title="t", location_text="", workplace_type=UNKNOWN, country_bucket=UNKNOWN,
                  url="", description_text="", raw={"ref": ref})
        assert conn(client=c).fetch_detail(job, {"slug": "acme", "name": "A"}) == "Hi"
        assert c.urls == [expect]


def test_workday_endpoint_parts_are_validated() -> None:
    ok = {"tenant": "Adobe", "wd_server": "WD5", "site": "external_experienced"}
    assert WorkdayConnector.endpoint_parts(ok) == ("adobe", "wd5", "external_experienced")
    assert WorkdayConnector.endpoint_parts({"tenant": "a", "site": "s"})[1] == "wd1"
    for bad in ({**ok, "tenant": "a.evil.com"}, {**ok, "tenant": "a/b"}, {**ok, "wd_server": "wd5.evil"},
                {**ok, "wd_server": "x"}, {**ok, "site": "a/../b"}, {**ok, "site": ""}, {"tenant": "a"}):
        with pytest.raises(ValueError):
            WorkdayConnector.endpoint_parts(bad)
        c = FakeClient({})
        with pytest.raises(ValueError):
            WorkdayConnector(client=c).fetch_raw({"name": "X", **bad})
        assert c.calls == []


def test_workday_list_call_and_detail_stay_on_the_tenant_host() -> None:
    row = {"name": "Adobe", "tenant": "adobe", "wd_server": "wd5", "site": "ext"}
    list_url = "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/ext/jobs"
    page = {"total": 1, "jobPostings": [{"title": "t", "externalPath": "/job/x_1", "locationsText": "Noida"}]}
    c = FakeClient({list_url: page})
    jobs = WorkdayConnector(client=c).fetch(row)
    assert c.urls == [list_url] and jobs[0].url == "https://adobe.wd5.myworkdayjobs.com/en-US/ext/job/x_1"
    detail = "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/ext/job/x_1"
    c = FakeClient({detail: {"jobPostingInfo": {"jobDescription": "<p>Body</p>"}}})
    assert WorkdayConnector(client=c).fetch_detail(jobs[0], row) == "Body"
    assert c.urls == [detail]
    jobs[0].raw = {"externalPath": "@evil.example/x"}          # not a path on this host
    c = FakeClient({})
    assert WorkdayConnector(client=c).fetch_detail(jobs[0], row) == "" and c.calls == []
