"""Discovery importers on offline fixtures: yc-oss, Bangalore Startup Map, funding recency."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import requests

from jobcopilot import companies as co
from jobcopilot import funding
from jobcopilot.discovery import bsm, yc
from jobcopilot.discovery.__main__ import main as cli_main

FIX = Path(__file__).parent / "fixtures"


def _resp(status: int, body: bytes = b"", headers: dict | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body
    r.headers.update(headers or {})
    r.encoding = "utf-8"
    return r


class FakeClient:
    """Stands in for PoliteClient: serves fixtures by URL, records every call."""

    def __init__(self, routes: dict[str, bytes], etag: str = 'W/"v1"'):
        self.routes, self.etag, self.calls = routes, etag, []

    def get(self, url, headers=None, **_):
        self.calls.append((url, dict(headers or {})))
        if (headers or {}).get("If-None-Match") == self.etag:
            return _resp(304)
        if url not in self.routes:
            return _resp(404)
        return _resp(200, self.routes[url], {"ETag": self.etag})


# ---------------------------------------------------------------- yc-oss

def test_yc_parse_fixture():
    rows = {r["name"]: r for r in yc.parse(json.loads((FIX / "yc_oss_hiring.json").read_text()))}
    assert rows["LiteLLM"]["yc_batch"] == "W23"
    assert rows["Razorpay"]["yc_top_company"] is True and rows["Razorpay"]["sector"] == "fintech"
    assert rows["Emergent"]["website"] == "https://emergent.sh"
    assert rows["PNOĒ"]["website"] is None


def test_yc_run_upserts_and_skips_unchanged():
    url = "https://yc-oss.github.io/api/companies/hiring.json"
    client = FakeClient({url: (FIX / "yc_oss_hiring.json").read_bytes()})
    out = yc.run(client=client, url=url)
    assert out["upserted"] == 5 and not out["unchanged"]
    lite = co.get_company("litellm.ai")
    assert lite["tier"] == "funded" and "yc-oss: W23" in lite["tier_reasons"]
    assert lite["signal_sources"] == ["yc-oss"]
    assert co.get_company("Razorpay")["tier"] == "brand"          # brands.yaml beats YC
    again = yc.run(client=client, url=url)
    assert again["unchanged"] is True
    assert client.calls[-1][1]["If-None-Match"] == 'W/"v1"'        # conditional GET across runs


def test_yc_failure_is_reported_not_raised():
    class Boom(FakeClient):
        def get(self, url, headers=None, **_):
            from jobcopilot.http import HttpConnectionError
            raise HttpConnectionError("down")
    out = yc.run(client=Boom({}), url="https://yc-oss.github.io/x.json")
    assert "error" in out
    assert co.get_source_state("yc-oss")["last_error"]


# ------------------------------------------------------------------- BSM

HOME = "https://bangalorestartupmap.com/"


def test_bsm_parse_home_fixture():
    startups, roles = bsm.parse_home((FIX / "bsm_home.html").read_text(encoding="utf-8"))
    assert {s["slug"] for s in startups} >= {"razorpay", "groww", "10club", "accel-india"}
    assert set(roles) == {"razorpay", "groww", "bimakavach"}
    fields = {f["name"]: f for f in map(bsm.company_fields, startups) if f}
    assert "Accel" not in " ".join(fields)                          # VCs skipped
    assert fields["Razorpay"]["funding_stage"] == "Series C+"
    assert fields["10Club"]["website"] is None                      # "" -> None
    assert fields["Razorpay"]["hq_location"] == "Bengaluru"


def test_bsm_parse_company_page_fixture():
    hint = bsm.parse_company_page((FIX / "bsm_company.html").read_text(encoding="utf-8"))
    assert hint["ats"] == "greenhouse"
    assert hint["ats_slug"] == "razorpaysoftwareprivatelimited"
    assert hint["ats_evidence"].startswith("bsm role id")


def _bsm_client() -> FakeClient:
    page = (FIX / "bsm_company.html").read_bytes()
    return FakeClient({HOME: (FIX / "bsm_home.html").read_bytes(),
                       HOME + "company/razorpay": page,
                       HOME + "company/groww": b"<html>no roles</html>",
                       HOME + "company/bimakavach": b"<html>no roles</html>"})


def test_bsm_run_diff_logic_and_spacing():
    client, sleeps = _bsm_client(), []
    t = iter(range(0, 1000, 1))
    out = bsm.run(client=client, delay_sec=5, sleep=sleeps.append, clock=lambda: float(next(t)))
    assert out["upserted"] == 5 and out["pages_fetched"] == 3 and out["deferred"] == 0
    assert all(s >= 3.9 for s in sleeps) and len(sleeps) == 3          # >= 5 s apart, homepage too
    assert not any("/api/" in u for u, _ in client.calls)
    rp = co.get_company("razorpay.com")
    assert (rp["ats"], rp["ats_slug"]) == ("greenhouse", "razorpaysoftwareprivatelimited")
    assert rp["ats_confidence"] < co.VERIFIED_CONFIDENCE              # a hint, not verified
    assert "bsm" in rp["signal_sources"] and rp["funding_stage"] == "Series C+"
    assert co.get_company("Bimakavach")["tier"] == "funded"           # Series A >= series_a
    assert co.get_company("10Club")["tier"] == "unknown"              # Seed

    # second run, homepage unchanged -> 304, no company page refetched
    n = len(client.calls)
    out2 = bsm.run(client=client, delay_sec=5, sleep=sleeps.append)
    assert out2["unchanged"] and out2["pages_fetched"] == 0
    assert len(client.calls) == n + 1

    # one company's roles change -> only that company's page is fetched
    html = (FIX / "bsm_home.html").read_text(encoding="utf-8")
    _, roles = bsm.parse_home(html)
    changed = html.replace('posted_day\\": 2', 'posted_day\\": 1', 1)   # one role's date
    _, roles2 = bsm.parse_home(changed)
    changed_slugs = [s for s in roles if roles[s] != roles2[s]]
    assert len(changed_slugs) == 1
    client.routes[HOME] = changed.encode("utf-8")
    client.etag = 'W/"v2"'
    n = len(client.calls)
    out3 = bsm.run(client=client, delay_sec=0, sleep=sleeps.append)
    fetched = [u for u, _ in client.calls[n:] if "/company/" in u]
    assert fetched == [HOME + "company/" + changed_slugs[0]] and out3["pages_fetched"] == 1


def test_bsm_cap_defers_pages_to_next_run():
    client = _bsm_client()
    out = bsm.run(client=client, max_company_pages=1, delay_sec=0, sleep=lambda s: None)
    assert out["pages_fetched"] == 1 and out["deferred"] == 2
    out2 = bsm.run(client=client, max_company_pages=5, delay_sec=0, sleep=lambda s: None)
    assert out2["unchanged"] and out2["pages_fetched"] == 2 and out2["deferred"] == 0


# --------------------------------------------------------------- funding

def test_funding_recency_enforced():
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    fresh = format_datetime(now - timedelta(days=30))
    stale = format_datetime(now - timedelta(days=800))
    assert funding.is_recent(fresh, 540, now=now)
    assert not funding.is_recent(stale, 540, now=now)
    assert not funding.is_recent("", 540, now=now)
    assert not funding.is_recent("not a date", 540, now=now)
    assert funding.is_recent("2026-10-01T10:00:00Z", 540, now=now)


def test_funding_run_skips_stale_items_and_records_companies(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    items = [
        {"title": "Fresh raises Series B", "link": "https://e.x/1", "description": "",
         "pub_date": format_datetime(now - timedelta(days=10))},
        {"title": "Old raises Series C", "link": "https://e.x/2", "description": "",
         "pub_date": format_datetime(now - timedelta(days=900))},
    ]
    monkeypatch.setattr(funding, "FEEDS", ["https://feed.example/rss"])
    monkeypatch.setattr(funding, "_parse_rss", lambda url: items)
    asked: list[str] = []

    def fake_llm(title, desc):
        asked.append(title)
        return {"company": title.split()[0], "stage": "series b", "amount_usd": 3e7,
                "investors": ["Accel"], "sector": "tech"}
    monkeypatch.setattr(funding, "_ask_llm", fake_llm)
    out = funding.run(out_path=tmp_path / "discovered.yaml")
    assert asked == ["Fresh raises Series B"]                 # stale item never reaches the LLM
    assert out["qualified"] == 1 and out["recorded"] == 1
    c = co.get_company("Fresh")
    assert c["tier"] == "funded" and "funding-rss" in c["signal_sources"]
    assert any(r.startswith("funding-rss:") for r in c["tier_reasons"])
    assert "Fresh" in (tmp_path / "discovered.yaml").read_text()   # yaml kept for back-compat


# ------------------------------------------------------------------- CLI

def test_cli_tiers_prints_counts(capsys):
    co.upsert_company("Google")
    assert cli_main(["tiers"]) == 0
    assert '"brand": 1' in capsys.readouterr().out


# ------------------------------------------------- Phase 1 review regressions

def test_yc_keeps_stored_validators_when_response_lacks_them():   # logic-10
    url = "https://yc-oss.github.io/api/companies/hiring.json"
    body = (FIX / "yc_oss_hiring.json").read_bytes()
    yc.run(client=FakeClient({url: body}), url=url)
    assert co.get_source_state("yc-oss")["etag"] == 'W/"v1"'

    class NoValidators(FakeClient):
        def get(self, url, headers=None, **_):
            self.calls.append((url, dict(headers or {})))
            return _resp(200, self.routes[url])
    yc.run(client=NoValidators({url: body}), url=url)               # same hash
    assert co.get_source_state("yc-oss")["etag"] == 'W/"v1"'
    yc.run(client=NoValidators({url: b"[]"}), url=url)              # changed body
    assert co.get_source_state("yc-oss")["etag"] == 'W/"v1"'


def test_bsm_hint_updates_the_homepage_row_not_a_phantom():   # logic-4
    rid = co.upsert_company("Razorpay Software Pvt Ltd", website="https://razorpay.com",
                            signal_source="yc-oss")
    bsm.run(client=_bsm_client(), delay_sec=0, sleep=lambda s: None)
    assert co.get_company("Razorpay") is None                          # no phantom "Razorpay" row
    rows = [c for c in co.list_companies() if "razorpay" in c["normalized_name"]]
    assert [c["id"] for c in rows] == [rid]
    assert rows[0]["ats_slug"] == "razorpaysoftwareprivatelimited"


def test_bsm_first_company_page_is_spaced_from_homepage():   # safety-6
    client, sleeps = _bsm_client(), []
    t = iter(range(0, 1000, 1))
    bsm.run(client=client, delay_sec=5, sleep=sleeps.append, clock=lambda: float(next(t)))
    assert len(sleeps) == 3 and all(s > 0 for s in sleeps)       # homepage -> page 1 too
    from jobcopilot import config
    assert config.sources()["http"]["host_rates"]["bangalorestartupmap.com"] <= 0.2


def _seed_bsm_state(pending: dict) -> None:
    co.put_source_state("bsm", etag='W/"v1"', content_hash="x",
                        state={"hashes": {}, "pending": pending, "names": {}})


def test_bsm_refuses_unsafe_slugs_and_api_urls():   # safety-7
    assert bsm.company_url(HOME, "razorpay") == HOME + "company/razorpay"
    for bad in ("../api/roles", "a/b", "Razorpay", "-x", "x?y=1", "", "a%2fb"):
        assert bsm.company_url(HOME, bad) is None, bad
    assert bsm.company_url("https://bangalorestartupmap.com/api/", "x") is None
    _seed_bsm_state({"../api/roles": "h1", "ok-slug": "h2"})
    client = _bsm_client()
    bsm.run(client=client, delay_sec=0, sleep=lambda s: None)
    assert not any("/api/" in u for u, _ in client.calls)
    assert any(u.endswith("/company/ok-slug") for u, _ in client.calls)


def test_bsm_pending_oldest_first_and_failures_dropped():   # logic-5
    _seed_bsm_state({"zzz-old": {"hash": "h", "since": 100.0, "attempts": 0},
                     "aaa-new": {"hash": "h", "since": 200.0, "attempts": 0}})
    client = _bsm_client()          # neither slug has a route -> 404
    out = bsm.run(client=client, max_company_pages=1, delay_sec=0, sleep=lambda s: None)
    pages = [u for u, _ in client.calls if "/company/" in u]
    assert pages == [HOME + "company/zzz-old"]                   # oldest first, not alphabetical
    assert out["dropped"] == 1                                    # 404 -> dropped, not retried
    pend = co.get_source_state("bsm")["state"]["pending"]
    assert set(pend) == {"aaa-new"}

    class Flaky(FakeClient):
        def get(self, url, headers=None, **kw):
            if "/company/" in url:
                self.calls.append((url, {}))
                from jobcopilot.http import HttpConnectionError
                raise HttpConnectionError("timeout")
            return super().get(url, headers, **kw)
    flaky, t0 = Flaky(_bsm_client().routes), 1_000_000.0
    for i in range(10):
        bsm.run(client=flaky, delay_sec=0, sleep=lambda s: None, now=t0 + i * 7 * 86400)
    tries = [u for u, _ in flaky.calls if u.endswith("/company/aaa-new")]
    assert 1 <= len(tries) <= bsm.MAX_PAGE_ATTEMPTS
    assert "aaa-new" not in co.get_source_state("bsm")["state"]["pending"]


# ------------------------------------------------------------- ATS detect

class FakeResult:
    def __init__(self, **kw):
        base = dict(ats=None, slug=None, tenant=None, wd_server=None, site=None, region=None,
                    confidence=0.0, evidence="", verified=False, job_count=None)
        base.update(kw)
        self.__dict__.update(base)


def test_detect_step_writes_results_and_respects_caps():   # logic-7
    from jobcopilot.discovery import detect as det
    a = co.upsert_company("Alpha", website="https://alpha.io", funding_stage="Public")
    b = co.upsert_company("Beta", website="https://beta.io", yc_batch="W24")
    c = co.upsert_company("Gamma", website="https://gamma.io", funding_stage="Seed")   # unknown
    d = co.upsert_company("Delta", website="https://delta.io", yc_batch="W23",
                          ats="lever", ats_slug="delta", ats_confidence=1.0)          # verified
    e = co.upsert_company("Eps", yc_batch="S22")                                       # no URL
    co.recompute_tiers()
    seen: list[str] = []

    def fake(url, *, client=None, verify=True):
        assert verify is True
        seen.append(url)
        if "alpha" in url:
            return FakeResult(ats="greenhouse", slug="alpha", confidence=0.85, verified=True,
                              evidence="link; verified 12 jobs", job_count=12)
        return FakeResult(ats="ashby", slug="beta", confidence=0.7, evidence="detail link")

    out = det.run(detector=fake, limit=40, now=1000.0)
    assert sorted(seen) == ["https://alpha.io", "https://beta.io"] and out["checked"] == 2
    ra, rb = co.get_company("alpha.io"), co.get_company("beta.io")
    assert (ra["ats"], ra["ats_slug"], ra["ats_confidence"]) == ("greenhouse", "alpha", 1.0)
    assert (rb["ats"], rb["ats_confidence"]) == ("ashby", 0.7)
    assert ra["ats_checked_at"] == rb["ats_checked_at"] == 1000.0 and rb["ats_evidence"]
    assert co.get_company("gamma.io")["ats_checked_at"] is None
    # re-run within 14 days -> nothing re-checked; after 14 days Beta (still unverified) is
    seen.clear()
    assert det.run(detector=fake, now=1000.0 + 86400)["checked"] == 0
    assert det.run(detector=fake, now=1000.0 + 15 * 86400, limit=1)["checked"] == 1
    assert seen == ["https://beta.io"]

    # one failing detection never aborts the run
    co.upsert_company("Zeta", website="https://zeta.io", yc_batch="W21")
    co.recompute_tiers()

    def boom(url, **_):
        raise RuntimeError("x")
    out = det.run(detector=boom, now=1000.0 + 40 * 86400)
    assert out["errors"] >= 1 and co.get_company("zeta.io")["ats_checked_at"] is not None


def test_cli_has_detect_step(monkeypatch):
    from jobcopilot.discovery import detect as det
    monkeypatch.setattr(det, "run", lambda **kw: {"checked": 0, "limit": kw.get("limit")})
    assert cli_main(["detect"]) == 0


# ---------------------------------------------------------- funding (more)

def test_funding_bad_llm_output_never_aborts(monkeypatch, tmp_path):   # logic-8
    now = datetime.now(timezone.utc)
    items = [{"title": f"T{i}", "link": f"https://e.x/{i}", "description": "",
              "pub_date": format_datetime(now - timedelta(days=5))} for i in range(6)]
    monkeypatch.setattr(funding, "FEEDS", ["https://feed.example/rss"])
    monkeypatch.setattr(funding, "_parse_rss", lambda url: items)
    recs = iter([
        ["not", "a", "dict"],
        {"company": "Nully", "stage": None, "amount_usd": "lots", "investors": [None, "Accel"],
         "sector": "tech"},
        {"company": "Bstage", "stage": 3, "amount_usd": None, "investors": None, "sector": "tech"},
        {"company": {"x": 1}, "stage": "series b"},
        {"company": "Goodco", "stage": "Series B", "amount_usd": "25000000",
         "investors": ["Sequoia", None], "sector": "fintech"},
        None,
    ])
    monkeypatch.setattr(funding, "_ask_llm", lambda t, d: next(recs))
    out = funding.run(out_path=tmp_path / "d.yaml")
    assert out["qualified"] == 1 and out["recorded"] == 1
    g = co.get_company("Goodco")
    assert g["tier"] == "funded" and g["investors"] == ["Sequoia"]


def test_funding_name_only_match_must_be_unambiguous():   # logic-3
    co.upsert_company("Atlas", website="https://atlas.so", signal_source="yc-oss")
    co.upsert_company("Atlas", website="https://atlashq.in", signal_source="bsm", funding_stage="Seed")
    co.upsert_company("Solo", website="https://solo.dev", signal_source="bsm", funding_stage="Seed")
    funding.record_companies([
        {"name": "Atlas", "stage": "series b", "investors": ["Accel"], "sector": "tech"},
        {"name": "Solo", "stage": "series b", "investors": ["Accel"], "sector": "tech"},
    ])
    assert "funding-rss" not in co.get_company("atlas.so")["signal_sources"]
    assert "funding-rss" not in co.get_company("atlashq.in")["signal_sources"]
    assert co.get_company("atlas.so")["tier"] != "funded"
    assert co.get_company("atlashq.in")["tier"] != "funded"
    sep = [c for c in co.list_companies() if c["normalized_name"].startswith("atlas#funding")]
    assert len(sep) == 1 and "funding-rss" in sep[0]["signal_sources"]
    assert "funding-rss" in co.get_company("solo.dev")["signal_sources"]   # unambiguous: merged
