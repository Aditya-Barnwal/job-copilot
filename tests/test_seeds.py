"""Seed datasets + rotating India probe, offline (tiny fixtures, fake client/connectors)."""
from __future__ import annotations

import json
from pathlib import Path

import requests

from jobcopilot import companies as co
from jobcopilot import store
from jobcopilot.discovery import probe, seeds
from jobcopilot.http import HttpStatusError, RetryLater
from jobcopilot.models import Job, ONSITE, REMOTE, UNKNOWN

FIX = Path(__file__).parent / "fixtures" / "seeds"
R = "https://raw.example/"


def _resp(status: int, body: bytes = b"", headers: dict | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code, r._content = status, body
    r.headers.update(headers or {})
    r.encoding = "utf-8"
    return r


class FakeClient:
    def __init__(self, routes: dict[str, bytes], etag: str = 'W/"v1"'):
        self.routes, self.etag, self.calls = routes, etag, []

    def get(self, url, headers=None, **_):
        self.calls.append(url)
        if (headers or {}).get("If-None-Match") == self.etag:
            return _resp(304)
        if url not in self.routes:
            return _resp(404)
        return _resp(200, self.routes[url], {"ETag": self.etag})


def _cfg() -> dict:
    return {"enabled": True, "datasets": {
        "ats-scrapers": {"format": "kalil_csv", "url": R + "kalil/{ats}.csv", "licence": "MIT",
                         "files": ["greenhouse", "workday", "keka", "smartrecruiters"]},
        "jobseek": {"format": "jobseek_csv", "url": R + "jobseek/boards.csv",
                    "names_url": R + "jobseek/companies.csv", "licence": "MIT (code)"},
        "zshah-internships": {"format": "zshah_json", "url": R + "zshah.json", "licence": "MIT"},
        "simplify-newgrad": {"format": "simplify_json", "url": R + "simplify.json", "licence": "none",
                             "signal_source": "simplify-newgrad"},
    }}


def _routes() -> dict[str, bytes]:
    rd = lambda n: (FIX / n).read_bytes()  # noqa: E731
    return {R + "kalil/greenhouse.csv": rd("kalil_greenhouse.csv"),
            R + "kalil/workday.csv": rd("kalil_workday.csv"),
            R + "kalil/keka.csv": rd("kalil_keka.csv"),
            R + "jobseek/boards.csv": rd("jobseek_boards.csv"),
            R + "jobseek/companies.csv": rd("jobseek_companies.csv"),
            R + "zshah.json": rd("zshah_companies.json"),
            R + "simplify.json": rd("simplify_listings.json")}


# ------------------------------------------------------------ parsers

def test_slug_validation():
    assert seeds.valid_slug("1910genetics") and seeds.valid_slug("a.b_c-d")
    for bad in ("", "../admin", "a b", "-x", "x/y", "x?y", "é", "a" * 101, None):
        assert not seeds.valid_slug(bad)
    assert seeds.make_seed("Acme", "greenhouse", slug="jobs") is None          # generic word
    assert seeds.make_seed("Acme", "smartrecruiters", slug="acme") is None     # robots-blocked
    assert seeds.make_seed("Acme", "workday", tenant="acme", wd_server="x9", site="ext") is None
    assert seeds.make_seed("Acme", "workday", tenant="acme", wd_server="wd5", site="../x") is None


def test_kalil_csv_formats():
    gh = seeds.parse_kalil_csv((FIX / "kalil_greenhouse.csv").read_bytes(), "greenhouse")
    assert [s.slug for s in gh] == ["postman", "stripe", "groww", "1910genetics", "1910genetics"]
    assert next(s for s in gh if s.slug == "groww").region == "eu"
    wd = seeds.parse_kalil_csv((FIX / "kalil_workday.csv").read_bytes(), "workday")
    assert [(s.tenant, s.wd_server, s.site) for s in wd] == [
        ("2020companies", "wd1", "external_careers"), ("3m", "wd1", "search")]
    keka = seeds.parse_kalil_csv((FIX / "kalil_keka.csv").read_bytes(), "keka")
    assert [s.slug for s in keka] == ["100", "abcoffee"]                       # nested host refused


def test_jobseek_zshah_simplify_formats():
    names = seeds.parse_jobseek_names((FIX / "jobseek_companies.csv").read_bytes())
    js = seeds.parse_jobseek_csv((FIX / "jobseek_boards.csv").read_bytes(), names)
    got = {(s.name, s.ats, s.slug or s.tenant) for s in js}
    assert got == {("1-800 Contacts", "greenhouse", "1800contacts"), ("100Ms", "pinpoint", "100ms"),
                   ("Agile Robots SE", "personio", "agile-robots-se"),
                   ("2020 Companies", "workday", "2020companies"), ("Stripe", "greenhouse", "stripe"),
                   ("Unnamed Co", "lever", "unnamedco")}                       # sitemap + SR skipped
    assert next(s for s in js if s.ats == "personio").region == "de"
    assert next(s for s in js if s.slug == "stripe").website == "https://stripe.com"
    z = seeds.parse_zshah_json(json.loads((FIX / "zshah_companies.json").read_bytes()))
    assert {(s.ats, s.slug or s.tenant) for s in z} == {
        ("greenhouse", "1800contacts"), ("ashby", "1password"), ("recruitee", "1x"), ("ashby", "1x"),
        ("workday", "23andme")}
    sp = seeds.parse_simplify_json(json.loads((FIX / "simplify_listings.json").read_bytes()))
    assert {(s.name, s.ats) for s in sp} == {("Postman", "greenhouse"), ("3M", "workday"), ("Plaid", "lever")}


def test_merge_dedupes_by_name_and_board():
    a = seeds.make_seed("1X", "recruitee", slug="1x")
    b = seeds.make_seed("1X", "ashby", slug="1x")
    c = seeds.make_seed("1910", "greenhouse", slug="1910genetics")
    d = seeds.make_seed("1910 Genetics", "greenhouse", slug="1910GENETICS")
    for s, src in ((a, "x"), (b, "y"), (c, "x"), (d, "y")):
        s.sources, s.datasets = [src], [src]
    m = seeds.merge_seeds([a, b, c, d])
    assert len(m) == 2
    onex = next(s for s in m if s.name == "1X")
    assert onex.ats == "ashby" and onex.sources == ["y", "x"]   # connector we have today wins
    assert next(s for s in m if s.slug == "1910genetics").sources == ["x", "y"]


# ------------------------------------------------------------ run_seeds

def test_run_seeds_imports_dedupes_and_skips_unchanged():
    # existing rows: a config company with its own ATS, and a yc row without one
    co.upsert_company("Stripe", signal_source="config", ats="greenhouse", ats_slug="stripe",
                      ats_confidence=1.0, ats_evidence="config")
    co.upsert_company("Postman", signal_source="yc-oss")
    client = FakeClient(_routes())
    res = seeds.run_seeds(client=client, cfg=_cfg())
    assert "error" not in res, res
    assert "seed:ats-scrapers:smartrecruiters" not in res["files"]                # never fetched
    stripe = co.get_company("Stripe")
    assert stripe["ats_confidence"] == 1.0 and stripe["ats_evidence"] == "config"    # untouched
    assert "seed:jobseek" in stripe["signal_sources"]
    postman = co.get_company("Postman")
    assert postman["ats"] == "greenhouse" and postman["ats_slug"] == "postman"
    assert postman["ats_confidence"] == co.UNVERIFIED_CONFIDENCE
    assert {"yc-oss", "seed:ats-scrapers", "simplify-newgrad"} <= set(postman["signal_sources"])
    assert "MIT" in postman["ats_evidence"]
    with store.connect() as conn:
        n1910 = conn.execute("SELECT COUNT(*) FROM companies WHERE ats_slug = '1910genetics'").fetchone()[0]
        contacts = conn.execute("SELECT COUNT(*) FROM companies WHERE ats_slug = '1800contacts'").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    assert n1910 == 1 and contacts == 1
    wd = co.get_company("3M")
    assert (wd["ats_tenant"], wd["ats_wd_server"], wd["ats_site"]) == ("3m", "wd1", "search")
    assert "simplify-newgrad" in wd["signal_sources"]
    assert co.get_company("Agile Robots SE")["ats_region"] == "de"
    assert co.get_company("Acosta Group") is None                                  # oracle skipped
    # nothing seeded is live yet
    assert all(c["ats_confidence"] < co.VERIFIED_CONFIDENCE for c in co.list_companies()
               if "config" not in c["signal_sources"])

    calls = len(client.calls)
    again = seeds.run_seeds(client=client, cfg=_cfg())
    assert again.get("merged") == 0 and all(f.get("unchanged") or "error" in f for f in again["files"].values())
    assert len(client.calls) - calls == 6          # one conditional GET per file, names not refetched
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == total


def test_same_body_new_etag_is_skipped():
    cfg = {"datasets": {"zshah-internships": _cfg()["datasets"]["zshah-internships"]}}
    seeds.run_seeds(client=FakeClient(_routes(), etag='"a"'), cfg=cfg)
    res = seeds.run_seeds(client=FakeClient(_routes(), etag='"b"'), cfg=cfg)
    assert res["files"]["seed:zshah-internships"] == {"unchanged": True}


# ------------------------------------------------------------ India rule

def _job(loc: str, wp: str = UNKNOWN, regions: list[str] | None = None) -> Job:
    return Job(source="t", company="c", company_sector="tech", external_id="1", title="SWE",
               location_text=loc, description_text="",
               workplace_type=wp, country_bucket="unknown", url="u",
               region_restrictions=regions or [])


def test_india_or_remote_rule():
    yes = [_job("Bengaluru, Karnataka"), _job("Remote", REMOTE), _job("", REMOTE),
           _job("Remote (Worldwide)"), _job("APAC"), _job("San Francisco; Pune"),
           _job("Anywhere"), _job("Remote", REMOTE, ["Asia"])]
    no = [_job("Remote - US", REMOTE), _job("San Francisco, CA", REMOTE), _job("London"),
          _job("Remote, Europe"), _job(""), _job("Remote - Canada"), _job("New York", ONSITE),
          _job("Remote", REMOTE, ["USA Only"])]
    assert all(probe.is_india_or_remote(j) for j in yes)
    assert not any(probe.is_india_or_remote(j) for j in no)


# ------------------------------------------------------------ probe

class FakeConn:
    """Per-slug canned job lists; records every board it was asked for."""
    BASE = "https://boards.fake/{slug}"
    boards: dict = {}
    calls: list = []

    def __init__(self, client=None):
        pass

    def fetch_raw(self, company):
        FakeConn.calls.append(company.get("slug") or company.get("tenant"))
        r = FakeConn.boards.get(company.get("slug") or company.get("tenant"), [])
        if isinstance(r, Exception):
            raise r
        return r

    def parse(self, raw, company):
        return [_job(loc, wp) for loc, wp in raw]


class OtherHost(FakeConn):
    BASE = "https://other.fake/{slug}"


def _seed(name, slug, *, ats="greenhouse", sources=("seed:x",), tier=None, conf=0.5, **kw):
    return co.upsert_company(name, ats=ats, ats_slug=slug, ats_confidence=conf,
                             ats_evidence=kw.pop("ev", "seed: x"), signal_sources=list(sources),
                             tier=tier, **kw)


def _reset(boards):
    FakeConn.boards, FakeConn.calls = boards, []


def test_probe_priority_order():
    _seed("Plain", "plain")
    _seed("Funded", "funded", tier="funded")
    _seed("Newgrad", "newgrad", sources=("seed:x", "simplify-newgrad"))
    _seed("Both", "both", sources=("simplify-newgrad", "yc-oss"))
    _seed("Old", "old", ev="probe: 2026-01-01 jobs=0 india_or_remote=0 -> inactive", ats_checked_at=1.0)
    _seed("Fresh", "fresh", ev="probe: x -> inactive", ats_checked_at=10**10)       # not due
    _seed("Verified", "verified", conf=1.0, ev="ats_detect: matched")              # not ours
    _seed("Cfg", "cfg", sources=("config",), ev="config")                         # config
    _seed("SR", "sr", ats="smartrecruiters")                                       # not supported
    order = [c["name"] for c in probe.candidates(supported={"greenhouse"}, now=10**9)]
    assert order == ["Both", "Newgrad", "Funded", "Plain", "Old"]


def test_probe_activates_only_india_relevant_and_demotes():
    a = _seed("IndiaCo", "india")
    b = _seed("UsCo", "us")
    c = _seed("Gone", "gone")
    d = _seed("WasActive", "was", conf=co.VERIFIED_CONFIDENCE, ev="probe: old -> active", ats_checked_at=1.0)
    resp = _resp(404)
    _reset({"india": [("Remote", REMOTE), ("Pune", ONSITE)], "us": [("Remote - US", REMOTE)],
            "gone": HttpStatusError("GET", "u", resp), "was": [("Berlin", ONSITE)]})
    out = probe.run_probe(limit=10, connectors={"greenhouse": FakeConn}, now=10**9,
                          cfg={"enabled": True})
    assert (out["probed"], out["active"], out["inactive"], out["gone"]) == (4, 1, 2, 1)
    get = {cid: r for r in co.list_companies() for cid in [r["id"]]}
    assert get[a]["ats_confidence"] == co.VERIFIED_CONFIDENCE
    assert "jobs=2 india_or_remote=2 -> active" in get[a]["ats_evidence"]
    assert get[b]["ats_confidence"] == co.UNVERIFIED_CONFIDENCE and get[b]["ats_checked_at"] == 10**9
    assert "HTTP 404" in get[c]["ats_evidence"]
    assert get[d]["ats_confidence"] == co.UNVERIFIED_CONFIDENCE
    # IndiaCo now reaches live ingest (tier permitting); the others don't
    live = {r["name"] for r in co.enabled_ats_companies(rules={"require_tier": ["unknown", "brand", "funded"]})}
    assert "IndiaCo" in live and not live & {"UsCo", "Gone", "WasActive"}
    # a second run right away probes nothing (all fresh)
    _reset({})
    assert probe.run_probe(connectors={"greenhouse": FakeConn}, now=10**9 + 60, cfg={"enabled": True})["probed"] == 0


def test_probe_caps_per_run_and_per_host():
    for i in range(6):
        _seed(f"G{i}", f"g{i}")
    for i in range(3):
        _seed(f"L{i}", f"l{i}", ats="lever")
    _reset({})
    out = probe.run_probe(connectors={"greenhouse": FakeConn, "lever": OtherHost}, now=10**9,
                          cfg={"enabled": True, "limit": 5, "max_per_host_per_run": 2})
    assert out["probed"] == 4 and len(FakeConn.calls) == 4        # 2 per host, 2 hosts
    assert out["hosts"] == {"boards.fake": 2, "other.fake": 2}
    _reset({})
    out = probe.run_probe(limit=1, connectors={"greenhouse": FakeConn, "lever": OtherHost},
                          now=10**9 + 1, cfg={"enabled": True, "max_per_host_per_run": 150})
    assert out["probed"] == 1 and len(FakeConn.calls) == 1


def test_probe_drops_host_on_retry_later():
    for i in range(3):
        _seed(f"G{i}", f"g{i}")
    _reset({"g0": RetryLater("u", 3600)})
    out = probe.run_probe(connectors={"greenhouse": FakeConn}, now=10**9, cfg={"enabled": True})
    assert out["probed"] == 0 and len(FakeConn.calls) == 1 and out["host_blocked"] == 3
    assert all(not str(c.get("ats_evidence")).startswith("probe:") for c in co.list_companies())


def test_board_host_and_workday_board():
    from jobcopilot.connectors import GreenhouseConnector, LeverConnector
    assert probe.board_host("greenhouse", {"slug": "x"}, GreenhouseConnector) == "boards-api.greenhouse.io"
    assert probe.board_host("lever", {"slug": "x"}, LeverConnector) == "api.lever.co"
    row = {"id": 1, "name": "3M", "ats": "workday", "ats_tenant": "3m", "ats_wd_server": "wd1",
           "ats_site": "search"}
    b = probe.company_board(row)
    assert b["max"] == 20 and probe.board_host("workday", b) == "3m.wd1.myworkdayjobs.com"
    assert probe.company_board({**row, "ats_site": "../x"}) is None


def test_probe_relevance_needs_target_role_and_not_senior():
    """A board is only 'active' for a role you could take: target-role title, not senior+."""
    from types import SimpleNamespace as NS
    from jobcopilot.discovery import probe
    cfg = {"require_role_match": True, "skip_senior": True}
    assert probe._relevant_title(NS(title="Software Engineer, Backend"), cfg)
    assert probe._relevant_title(NS(title="Machine Learning Engineer"), cfg)
    assert not probe._relevant_title(NS(title="Senior Software Engineer"), cfg)
    assert not probe._relevant_title(NS(title="Account Executive"), cfg)
    assert probe._relevant_title(NS(title="Account Executive"), {"require_role_match": False})
