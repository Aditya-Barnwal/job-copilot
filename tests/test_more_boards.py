"""Offline tests for the Phase 1b board connectors (Jobicy, Real Work From
Anywhere, 4dayweek, CryptoJobsList, Hasjob, Remote.com, Arbeitnow, NoDesk).

Fixtures in tests/fixtures/more_boards/ are trimmed real responses captured
2026-11-12 (descriptions cut to ~300 chars, emails/phones scrubbed); robots/
holds the robots.txt files as fetched that day, so a rule change shows up as a
diff here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jobcopilot.connectors.arbeitnow import ArbeitnowConnector
from jobcopilot.connectors.cryptojobslist import CryptoJobsListConnector
from jobcopilot.connectors.fourdayweek import FourDayWeekConnector, query_params as fdw_params
from jobcopilot.connectors.hasjob import HasjobConnector, workplace_of
from jobcopilot.connectors.jobicy import JobicyConnector, query_params as jobicy_params
from jobcopilot.connectors.nodesk import NoDeskConnector
from jobcopilot.connectors.realworkfromanywhere import (
    MAX_FEED_BYTES, RealWorkFromAnywhereConnector, UnsafeFeed, safe_feed_root, split_role_at_company,
)
from jobcopilot.connectors.remotecom import RemoteComConnector
from jobcopilot.http import HttpError, RobotsTxt
from jobcopilot.models import HYBRID, IN, ONSITE, REMOTE, UNKNOWN

FIX = Path(__file__).resolve().parent / "fixtures" / "more_boards"
UA = "job-copilot/2.0 (personal, non-commercial)"

BOARDS: dict[str, dict[str, Any]] = {
    "jobicy": {"attribution": {"name": "Jobicy", "url": "https://jobicy.com"}, "apply_rule": "source_link"},
    "realworkfromanywhere": {"attribution": {"name": "Real Work From Anywhere",
                                             "url": "https://www.realworkfromanywhere.com"},
                             "apply_rule": "prefer_ats", "store": "minimal"},
    "fourdayweek": {"attribution": {"name": "4 Day Week", "url": "https://4dayweek.io"}, "apply_rule": "source_link"},
    "cryptojobslist": {"attribution": {"name": "CryptoJobsList", "url": "https://cryptojobslist.com"},
                       "apply_rule": "prefer_ats", "store": "minimal"},
    "hasjob": {"attribution": {"name": "Hasjob", "url": "https://hasjob.co"},
               "apply_rule": "source_link", "store": "minimal"},
    "remotecom": {"attribution": {"name": "Remote", "url": "https://apply.remote.com"}, "apply_rule": "source_link"},
    "arbeitnow": {"attribution": {"name": "Arbeitnow", "url": "https://www.arbeitnow.com"},
                  "apply_rule": "source_link", "remote_only": True},
    "nodesk": {"attribution": {"name": "NoDesk", "url": "https://nodesk.co"},
               "apply_rule": "prefer_ats", "store": "minimal"},
}


def _json(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _bytes(name: str) -> bytes:
    return (FIX / name).read_bytes()


class _FakeClient:
    """Stands in for PoliteClient: records calls; a param value 'boom' fails."""

    def __init__(self, payload: Any = None, body: bytes = b"") -> None:
        self.payload, self.body = payload, body
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if "boom" in (params or {}).values():
            raise HttpError("boom")
        return self.payload

    def get(self, url, *, headers=None, timeout=None, max_bytes=None):
        self.calls.append((url, {"max_bytes": max_bytes, "accept": (headers or {}).get("Accept")}))

        class _Resp:
            content = self.body
        return _Resp()


def _common(jobs, source: str, attribution: str) -> None:
    assert jobs, source
    for j in jobs:
        assert j.source == source
        assert j.attribution_name == attribution
        assert j.attribution_url and j.attribution_url.startswith("https://")
        assert j.apply_url and j.apply_url.startswith("https://")
        assert j.title and j.external_id and j.url
        assert "/go/" not in j.url and "/go/" not in (j.apply_url or "")


# --------------------------------------------------------------------- Jobicy

def test_jobicy_parse_apply_is_original_url_and_salary():
    jobs = JobicyConnector().parse(_json("jobicy.json"), BOARDS["jobicy"])
    _common(jobs, "jobicy", "Jobicy")
    assert len(jobs) == 5
    for j in jobs:   # terms: credit with a direct link; Apply -> original job URL
        assert j.apply_url == j.url == j.attribution_url
        assert j.url.startswith("https://jobicy.com/jobs/")
        assert j.workplace_type == REMOTE
        assert "companyLogo" not in j.raw
    by_id = {j.external_id: j for j in jobs}
    assert by_id["155031"].region_restrictions == ["APAC", "EMEA"]   # "APAC,  EMEA"
    au = by_id["161302"].compensation
    assert (au.currency, au.min_value, au.max_value, au.interval) == ("AUD", 140000.0, 190000.0, "1 YEAR")
    assert by_id["155000"].compensation.interval == "1 MONTH"
    assert by_id["161303"].compensation is None


def test_jobicy_dedupes_across_queries_and_isolates_failures():
    payload = _json("jobicy.json")
    assert len(JobicyConnector().parse([payload, payload], {})) == 5
    fake = _FakeClient(payload)
    pages = JobicyConnector(client=fake).fetch_raw(
        {"queries": [{"geo": "apac", "count": 50}, {"geo": "boom"}, {"count": 50}]})
    assert len(pages) == 2
    assert fake.calls[0] == ("https://jobicy.com/api/v2/remote-jobs", {"geo": "apac", "count": 50})


def test_jobicy_default_queries_are_apac_and_worldwide():
    fake = _FakeClient({"jobs": []})
    JobicyConnector(client=fake).fetch_raw({})
    assert [c[1] for c in fake.calls] == [{"geo": "apac", "count": 50}, {"count": 50}]


@pytest.mark.parametrize("bad", [{"q": "python"}, {"s": "x"}, {"geo": "apac&q=x"}, {"count": 500},
                                 {"tag": "../etc"}, "apac"])
def test_jobicy_query_validation_refuses_robots_disallowed_or_odd_params(bad):
    with pytest.raises((ValueError, TypeError)):
        jobicy_params(bad)


# ------------------------------------------------------- Real Work From Anywhere

def test_rwfa_parse_worldwide_minimal_and_repairs_bare_ampersand():
    body = _bytes("realworkfromanywhere.xml")
    assert b"&type=" in body                       # real feed: unescaped & in an attribute
    jobs = RealWorkFromAnywhereConnector().parse(body, BOARDS["realworkfromanywhere"])
    _common(jobs, "realworkfromanywhere", "Real Work From Anywhere")
    assert len(jobs) == 4
    first = jobs[0]
    assert first.company == "Hillside Piers Ltd"
    assert first.title == "Junior Market Specialist / Analyst (Remote)"
    assert first.region_restrictions == ["Worldwide"] and first.workplace_type == REMOTE
    assert first.posted_at.startswith("2026-11-11T08:13:20")
    assert all(j.description_text == "" for j in jobs)           # store: minimal (D11)
    assert set(first.raw) == {"title", "link", "author", "pubDate"}


def test_rwfa_store_full_keeps_description():
    jobs = RealWorkFromAnywhereConnector().parse(_bytes("realworkfromanywhere.xml"), {"store": "full"})
    assert "Hillside Piers" in jobs[0].description_text


def test_rwfa_never_uses_go_links():
    body = _bytes("realworkfromanywhere.xml").decode("utf-8")
    go = "https://www.realworkfromanywhere.com/go/123"
    # Item 1: only the <link> is a /go/ redirect -> fall back to the /jobs/ guid.
    first = "https://www.realworkfromanywhere.com/jobs/junior-market-specialist-analyst-remote-hillside-piers-ltd-368"
    body = body.replace(f"<link>{first}</link>", f"<link>{go}</link>", 1)
    # Item 2: both link and guid are /go/ -> the item is dropped, not followed.
    body = body.replace("https://www.realworkfromanywhere.com/jobs/cloud-engineer-chess-com-6175", go)
    jobs = RealWorkFromAnywhereConnector().parse(body.encode(), {})
    assert len(jobs) == 3
    assert jobs[0].url.startswith("https://www.realworkfromanywhere.com/jobs/junior-market")
    assert all("/go/" not in j.url for j in jobs)


def test_feed_fetch_uses_size_cap_and_rss_accept():
    fake = _FakeClient(body=_bytes("realworkfromanywhere.xml"))
    raw = RealWorkFromAnywhereConnector(client=fake).fetch_raw({})
    assert raw.startswith(b"<?xml")
    url, info = fake.calls[0]
    assert url == "https://www.realworkfromanywhere.com/rss.xml"
    assert info["max_bytes"] == MAX_FEED_BYTES == 5 * 1024 * 1024
    assert "rss" in info["accept"]


# ------------------------------------------------------------ safe feed parsing

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
<rss><channel><item><title>&lol2;</title><link>https://nodesk.co/remote-jobs/x/</link></item></channel></rss>"""


@pytest.mark.parametrize("body", [
    BILLION_LAUGHS,
    b'<?xml version="1.0"?><!doctype rss SYSTEM "http://evil/x.dtd"><rss/>',
    b'<rss><!ENTITY x "y"><channel/></rss>',
    BILLION_LAUGHS.decode(),                         # str input too
])
def test_safe_feed_root_refuses_dtd_and_entities(body):
    with pytest.raises(UnsafeFeed):
        safe_feed_root(body)


@pytest.mark.parametrize("cls", [RealWorkFromAnywhereConnector, CryptoJobsListConnector,
                                 HasjobConnector, NoDeskConnector])
def test_every_feed_connector_refuses_entity_expansion(cls):
    with pytest.raises(UnsafeFeed):
        cls().parse(BILLION_LAUGHS, {})


_UTF16_DTD = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel/></rss>'
_RSS_OK = '<?xml version="1.0"?><rss><channel><item><title>T</title></item></channel></rss>'


def _utf16_variants() -> list[bytes]:
    return [
        _UTF16_DTD.encode("utf-16"),                              # BOM, native order
        _UTF16_DTD.encode("utf-16-le"),                           # no BOM
        _UTF16_DTD.encode("utf-16-be"),
        b"\xff\xfe" + _UTF16_DTD.encode("utf-16-le"),
        b"\xfe\xff" + _UTF16_DTD.encode("utf-16-be"),
        _UTF16_DTD.encode("utf-32"),
        _UTF16_DTD.encode("utf-32-le"),
        _UTF16_DTD.encode("utf-32-be"),
        _RSS_OK.encode("utf-16"),                                 # even a clean one: refused
        b'<?xml version="1.0" encoding="UTF-16"?><rss/>',        # declared, ASCII bytes
        b'<?xml version="1.0" encoding=\'cp037\'?><rss/>',
        b'\xef\xbb\xbf<?xml version="1.0" encoding="utf-16"?><rss/>',
    ]


@pytest.mark.parametrize("body", _utf16_variants())
def test_safe_feed_root_refuses_utf16_utf32_and_odd_encodings(body):
    """S5: the byte-level DOCTYPE guard cannot read UTF-16; the body must be refused."""
    with pytest.raises(UnsafeFeed):
        safe_feed_root(body)


@pytest.mark.parametrize("cls", [RealWorkFromAnywhereConnector, CryptoJobsListConnector,
                                 HasjobConnector, NoDeskConnector])
def test_every_feed_connector_refuses_utf16_doctype(cls):
    for body in _utf16_variants():
        with pytest.raises(UnsafeFeed):
            cls().parse(body, {})


@pytest.mark.parametrize("body", [
    '<?xml version="1.0" encoding="utf-8"?><rss><channel/></rss>'.encode(),
    '<?xml version=\'1.0\' encoding=\'ISO-8859-1\'?><rss><channel/></rss>'.encode("latin-1"),
    b'<?xml version="1.0" encoding="US-ASCII"?><rss><channel/></rss>',
    b'\xef\xbb\xbf<rss><channel/></rss>',
])
def test_safe_feed_root_still_accepts_utf8_ascii_latin1(body):
    assert safe_feed_root(body).tag == "rss"


def test_safe_feed_root_refuses_oversized_body():
    with pytest.raises(UnsafeFeed):
        safe_feed_root(b"<rss>" + b" " * MAX_FEED_BYTES + b"</rss>")


def test_safe_feed_root_repairs_html_entities_but_leaves_cdata_alone():
    root = safe_feed_root(b"<r><a>We&rsquo;re &nbsp;A & B &amp; C &bogus; &#38;</a>"
                          b"<b><![CDATA[x &rsquo; & y]]></b><c href='u?a=1&b=2'/></r>")
    assert root.findtext("a") == "We’re  A & B & C &bogus; &"
    assert root.findtext("b") == "x &rsquo; & y"
    assert root.find("c").get("href") == "u?a=1&b=2"


def test_split_role_at_company():
    assert split_role_at_company("Cloud Engineer at Chess.com") == ("Cloud Engineer", "Chess.com")
    assert split_role_at_company("Head of Data at Scale at Acme") == ("Head of Data at Scale", "Acme")
    assert split_role_at_company("Data at Scale at Acme Corp", "Acme Corp") == ("Data at Scale", "Acme Corp")
    assert split_role_at_company("Backend Engineer") == ("Backend Engineer", "")


# ------------------------------------------------------------------- 4dayweek

def test_fourdayweek_parse_cents_regions_and_link_back():
    jobs = FourDayWeekConnector().parse(_json("fourdayweek.json"), BOARDS["fourdayweek"])
    _common(jobs, "fourdayweek", "4 Day Week")
    assert len(jobs) == 5
    for j in jobs:   # terms: link back to 4dayweek.io
        assert j.url.startswith("https://4dayweek.io/job/")
        assert j.apply_url == j.url == j.attribution_url
        assert "logo_url" not in j.raw["company"]
    bvnk = next(j for j in jobs if j.company == "BVNK")
    assert bvnk.workplace_type == REMOTE
    assert bvnk.region_restrictions == ["Bulgaria", "South Africa", "Poland", "Spain"]
    c = bvnk.compensation
    assert (c.currency, c.min_value, c.max_value, c.interval) == ("EUR", 70000.0, 110000.0, "1 YEAR")
    hybrid = next(j for j in jobs if j.company == "HR Wallingford")
    assert hybrid.workplace_type == HYBRID and hybrid.region_restrictions == []
    assert hybrid.tz_requirement_text == "GMT"
    assert hybrid.location_text == "Oxford, United Kingdom"


def test_fourdayweek_fetch_is_api_v2_page_one_only():
    fake = _FakeClient({"data": []})
    FourDayWeekConnector(client=fake).fetch_raw({})
    url, params = fake.calls[0]
    assert url == "https://4dayweek.io/api/v2/jobs"
    assert "page" not in params and params["work_arrangement"] == "remote"
    with pytest.raises(ValueError):
        fdw_params({"page": 2})
    with pytest.raises(ValueError):
        fdw_params({"q": "a&b=c"})


# ------------------------------------------------------------- CryptoJobsList

def test_cryptojobslist_parse_minimal():
    jobs = CryptoJobsListConnector().parse(_bytes("cryptojobslist.xml"), BOARDS["cryptojobslist"])
    _common(jobs, "cryptojobslist", "CryptoJobsList")
    assert len(jobs) == 4
    first = jobs[0]
    assert (first.company, first.title, first.location_text) == ("Jeeves", "KYC/B Onboarding Analyst", "São Paulo")
    assert first.workplace_type == UNKNOWN and first.region_restrictions == []
    remote = next(j for j in jobs if j.company == "Shakepay")
    assert remote.workplace_type == REMOTE and remote.region_restrictions == ["Canada"]
    assert all(j.description_text == "" and j.url.startswith("https://cryptojobslist.com/jobs/") for j in jobs)
    assert all("/apply" not in j.apply_url for j in jobs)


# ---------------------------------------------------------------------- Hasjob

def test_hasjob_parse_company_is_domain_and_workplace():
    jobs = HasjobConnector().parse(_bytes("hasjob.xml"), BOARDS["hasjob"])
    _common(jobs, "hasjob", "Hasjob")
    assert len(jobs) == 4
    first = jobs[0]
    assert first.company == "hookstech.in" and first.external_id == "hookstech.in/l38x2"
    assert first.posted_at.startswith("2026-11-10T12:11:19")
    onsite = next(j for j in jobs if j.company == "nestack.com")
    assert onsite.workplace_type == ONSITE and onsite.country_bucket == IN
    anywhere = [j for j in jobs if j.location_text == "Anywhere"]
    assert anywhere and all(j.workplace_type == REMOTE and j.region_restrictions == ["Anywhere"]
                            for j in anywhere)
    assert all(j.description_text == "" for j in jobs)
    assert all("Azim" not in json.dumps(j.raw) for j in jobs)   # poster's name never kept


@pytest.mark.parametrize("loc,want", [
    ("Hyderabad, India (on-site, no WFH)", ONSITE), ("Anywhere", REMOTE),
    ("Bangalore or Remote", REMOTE), ("Madurai", UNKNOWN),
])
def test_hasjob_workplace_of(loc, want):
    assert workplace_of(loc) == want


# ------------------------------------------------------------------ Remote.com

def test_remotecom_parse_urls_regions_and_pay():
    jobs = RemoteComConnector().parse(_json("remotecom.json"), BOARDS["remotecom"])
    _common(jobs, "remotecom", "Remote")
    assert len(jobs) == 4
    for j in jobs:
        assert j.url == f"https://apply.remote.com/jobs/{j.external_id}" == j.apply_url
        assert j.company == "Remote" and j.workplace_type == REMOTE
        assert "pay_doc" not in j.raw
    first = jobs[0]
    assert first.region_restrictions == ["EMEA"]
    assert (first.compensation.currency, first.compensation.min_value, first.compensation.max_value) == \
        ("EUR", 57500.0, 64650.0)
    two_rows = next(j for j in jobs if j.region_restrictions == ["EMEA", "NORAM"])
    assert two_rows.compensation.min_value is None and "NORAM" in two_rows.compensation.summary


def test_remotecom_skips_bad_ids_and_closed_roles():
    payload = _json("remotecom.json")
    payload["jobs"][0]["public_id"] = "../../admin"
    payload["jobs"][1]["accepting_applications"] = False
    payload["jobs"][2]["status"] = "closed"
    assert len(RemoteComConnector().parse(payload, {})) == 1


# -------------------------------------------------------------------- Arbeitnow

def test_arbeitnow_keeps_only_remote_rows():
    raw = _json("arbeitnow.json")
    jobs = ArbeitnowConnector().parse(raw, BOARDS["arbeitnow"])
    _common(jobs, "arbeitnow", "Arbeitnow")
    assert len(raw["data"]) == 5 and len(jobs) == 3
    assert all(j.workplace_type == REMOTE for j in jobs)
    bare = [j for j in jobs if j.location_text == "Remote"]
    assert len(bare) == 2 and all(j.region_restrictions == [] for j in bare)
    berlin = next(j for j in jobs if "Berlin" in j.location_text)
    assert berlin.location_text == "Berlin (remote)" and berlin.region_restrictions == ["Berlin"]
    # Real data: one row links to the sister site arbeitnow.co.uk.
    assert all(j.url.split("/")[2] in ("www.arbeitnow.com", "www.arbeitnow.co.uk") for j in jobs)
    assert all(j.apply_url == j.url == j.attribution_url for j in jobs)
    assert jobs[0].posted_at.startswith("2026-")
    assert len(ArbeitnowConnector().parse(raw, {"remote_only": False})) == 5


def test_arbeitnow_fetch_page_one_only():
    fake = _FakeClient({"data": []})
    ArbeitnowConnector(client=fake).fetch_raw({})
    assert fake.calls == [("https://www.arbeitnow.com/api/job-board-api", {})]


# ---------------------------------------------------------------------- NoDesk

def test_nodesk_parse_repairs_html_entities():
    body = _bytes("nodesk.xml")
    assert b"&rsquo;" in body                        # real feed: an HTML entity expat rejects
    jobs = NoDeskConnector().parse(body, BOARDS["nodesk"])
    _common(jobs, "nodesk", "NoDesk")
    assert [(j.company, j.title) for j in jobs][:2] == [("LiveKit", "Solutions Architect"),
                                                         ("AutoMQ", "Senior Software Engineer")]
    assert jobs[2].title == "Senior Product Manager, Hybrid Platform & Deployment Models"
    assert all(j.description_text == "" for j in jobs)   # use=reference content signal
    full = NoDeskConnector().parse(body, {"store": "full"})
    assert "We\u2019re reinventing" in full[1].description_text


# ------------------------------------------------- robots.txt as fetched 2026-11-12

@pytest.mark.parametrize("host,url,allowed", [
    ("jobicy.com", "https://jobicy.com/api/v2/remote-jobs?count=50&geo=apac", True),
    ("jobicy.com", "https://jobicy.com/api/v2/remote-jobs?q=python", False),
    ("www.realworkfromanywhere.com", "https://www.realworkfromanywhere.com/rss.xml", True),
    ("www.realworkfromanywhere.com", "https://www.realworkfromanywhere.com/go/123", False),
    ("4dayweek.io", "https://4dayweek.io/api/v2/jobs?work_arrangement=remote&limit=50", True),
    ("4dayweek.io", "https://4dayweek.io/api/jobs", False),
    ("api.cryptojobslist.com", "https://api.cryptojobslist.com/jobs.rss", True),
    ("api.cryptojobslist.com", "https://api.cryptojobslist.com/api/jobs", False),
    ("hasjob.co", "https://hasjob.co/feed", True),
    ("www.arbeitnow.com", "https://www.arbeitnow.com/api/job-board-api", True),
    ("nodesk.co", "https://nodesk.co/remote-jobs/index.xml", True),
])
def test_endpoints_match_robots_snapshot(host, url, allowed):
    robots = RobotsTxt((FIX / "robots" / f"{host}.txt").read_text(encoding="utf-8"), UA)
    assert robots.allows(url) is allowed
