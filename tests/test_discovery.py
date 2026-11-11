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
    assert all(s >= 3.9 for s in sleeps) and len(sleeps) == 2          # >= 5 s apart
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
