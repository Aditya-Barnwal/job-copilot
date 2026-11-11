"""Offline tests for the Phase 1 job-board connectors and regions helpers.

Fixtures are trimmed real responses captured 2026-11-11 (descriptions cut,
emails/phones scrubbed). In himalayas.json the LAST job's expiryDate was set
to 1700000000 (2023) on purpose, to exercise drop_expired.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from jobcopilot.connectors.himalayas import HimalayasConnector
from jobcopilot.connectors.jobspresso import JobspressoConnector, parse_salary_text
from jobcopilot.connectors.remoteok import RemoteOKConnector
from jobcopilot.connectors.weworkremotely import WeWorkRemotelyConnector, split_title
from jobcopilot.connectors.workingnomads import WorkingNomadsConnector
from jobcopilot.http import RobotsDisallowed
from jobcopilot.models import REMOTE
from jobcopilot.regions import (
    is_timezone_text, parse_utc_offsets, partition_location, regions_from_list, split_regions,
)

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)

BOARDS: dict[str, dict[str, Any]] = {
    "himalayas": {"attribution": {"name": "Himalayas", "url": "https://himalayas.app"},
                  "apply_rule": "source_link", "queries": ["software engineer"],
                  "max_pages_per_query": 2, "drop_expired": True},
    "workingnomads": {"attribution": {"name": "Working Nomads", "url": "https://www.workingnomads.com"},
                      "apply_rule": "prefer_ats", "categories": ["development"]},
    "jobspresso": {"attribution": {"name": "Jobspresso", "url": "https://jobspresso.co"},
                   "apply_rule": "prefer_ats", "store": "minimal"},
    "remoteok": {"attribution": {"name": "Remote OK", "url": "https://remoteok.com"},
                 "apply_rule": "prefer_ats"},
    "weworkremotely": {"attribution": {"name": "We Work Remotely", "url": "https://weworkremotely.com"},
                       "apply_rule": "prefer_ats", "store": "minimal",
                       "feeds": ["https://weworkremotely.com/a.rss", "https://weworkremotely.com/b.rss"]},
}


def _json(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _common(jobs, source: str, attribution: str) -> None:
    assert jobs
    for j in jobs:
        assert j.source == source
        assert j.company and j.title and j.external_id and j.url
        assert j.company_sector is None
        assert j.workplace_type == REMOTE
        assert j.attribution_name == attribution
        assert j.attribution_url == j.url          # the specific listing, followable
        assert j.apply_url
    assert len({j.external_id for j in jobs}) == len(jobs)


# ---------------------------------------------------------------- Himalayas

def test_himalayas_parse_and_expiry():
    raw = _json("himalayas.json")
    jobs = HimalayasConnector().parse(raw, BOARDS["himalayas"], now=NOW)
    _common(jobs, "himalayas", "Himalayas")
    assert len(raw["jobs"]) == 6 and len(jobs) == 5        # expired one dropped
    expired_guid = raw["jobs"][-1]["guid"]
    assert expired_guid not in {j.external_id for j in jobs}
    for j, src in zip(jobs, raw["jobs"]):
        assert j.apply_url == src["applicationLink"]         # terms: applicationLink
        assert j.url == src["guid"]
        assert j.expires_at and j.posted_at.startswith("2026-")
        assert j.description_text and "<" not in j.description_text

    first = jobs[0]
    assert first.company == "Alien Intellect"
    assert first.region_restrictions == [] and first.location_text == "Remote"
    assert first.timezone_restrictions == [3.5]
    c = first.compensation
    assert (c.currency, c.min_value, c.max_value, c.interval, c.is_estimate) == \
        ("USD", 2000.0, 3000.0, "1 MONTH", False)

    us = jobs[1]
    assert us.region_restrictions == ["United States"] and us.country_bucket == "non-IN"
    assert -5.0 in us.timezone_restrictions
    assert jobs[-1].compensation is None                     # salary not disclosed


def test_himalayas_keeps_expired_when_disabled():
    raw = _json("himalayas.json")
    jobs = HimalayasConnector().parse(raw, {**BOARDS["himalayas"], "drop_expired": False}, now=NOW)
    assert len(jobs) == 6


def test_himalayas_dedupes_across_query_pages():
    raw = _json("himalayas.json")
    jobs = HimalayasConnector().parse([raw, raw], BOARDS["himalayas"], now=NOW)
    assert len(jobs) == 5


class _FakeClient:
    """Stands in for PoliteClient: records calls, refuses page >= 2 like
    Himalayas' robots.txt does today."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict, dict]] = []

    def get_json(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if (params or {}).get("page", 1) > 1:
            raise RobotsDisallowed(url)
        return self.payload


def test_himalayas_fetch_pages_and_key(monkeypatch):
    full_page = {"jobs": [{"guid": f"g{i}"} for i in range(20)]}
    fake = _FakeClient(full_page)
    monkeypatch.setenv("HIMALAYAS_API_KEY", "test-key")
    board = {**BOARDS["himalayas"], "queries": ["a", "b"]}
    pages = HimalayasConnector(client=fake).fetch_raw(board)
    assert len(pages) == 2                                   # page 1 of each query
    assert [c[1] for c in fake.calls] == [{"q": "a"}, {"q": "a", "page": 2},
                                          {"q": "b"}, {"q": "b", "page": 2}]
    assert fake.calls[0][2]["Authorization"] == "Bearer test-key"


def test_himalayas_fetch_stops_on_short_page(monkeypatch):
    monkeypatch.delenv("HIMALAYAS_API_KEY", raising=False)
    fake = _FakeClient({"jobs": [{"guid": "only"}]})
    HimalayasConnector(client=fake).fetch_raw(BOARDS["himalayas"])
    assert len(fake.calls) == 1 and "Authorization" not in fake.calls[0][2]


def test_himalayas_fetch_raises_when_every_query_fails():
    class Refuse(_FakeClient):
        def get_json(self, url, **kw):
            raise RobotsDisallowed(url)
    with pytest.raises(RobotsDisallowed):
        HimalayasConnector(client=Refuse(None)).fetch_raw(BOARDS["himalayas"])


# ------------------------------------------------------------ Working Nomads

def test_workingnomads_parse():
    raw = _json("workingnomads.json")
    jobs = WorkingNomadsConnector().parse(raw, BOARDS["workingnomads"])
    _common(jobs, "workingnomads", "Working Nomads")
    assert len(raw) == 4 and len(jobs) == 2                  # non-development filtered out
    lemon, proxify = jobs
    assert lemon.external_id == "2011124"
    assert lemon.region_restrictions == ["Europe", "LATAM", "APAC", "the U.S.", "Canada"]
    assert lemon.apply_url == lemon.url                      # /job/go/ is the outbound link
    assert proxify.region_restrictions == []
    assert proxify.tz_requirement_text == "Time zone: CET (+/- 3 hours)"
    assert lemon.description_text and lemon.posted_at.startswith("2026-11-10")
    # No category filter -> every item.
    assert len(WorkingNomadsConnector().parse(raw, {})) == 4


# ---------------------------------------------------------------- Remote OK

def test_remoteok_parse():
    raw = _json("remoteok.json")
    assert "legal" in raw[0]
    jobs = RemoteOKConnector().parse(raw, BOARDS["remoteok"])
    _common(jobs, "remoteok", "Remote OK")
    assert len(jobs) == len(raw) - 1                          # legal notice skipped
    assert all(j.external_id != "" and "legal" not in j.raw for j in jobs)
    assert all("logo" not in j.raw and "company_logo" not in j.raw for j in jobs)
    buoyant = jobs[0]
    assert "—" in buoyant.title and "â" not in buoyant.title  # mojibake repaired
    assert buoyant.compensation is None                       # salary 0 = none
    warsaw = next(j for j in jobs if j.location_text == "Warsaw")
    assert warsaw.region_restrictions == ["Warsaw"]
    # Pebl's 114,000,000 "USD/year" is implausible -> no number kept.
    assert next(j for j in jobs if j.company == "Pebl").compensation is None


def test_remoteok_salary_kept_when_plausible():
    item = {"id": "1", "company": "X", "position": "Eng", "url": "https://remoteok.com/remote-jobs/x-1",
            "salary_min": "90000", "salary_max": 0}
    job = RemoteOKConnector().parse([{"legal": "..."}, item], BOARDS["remoteok"])[0]
    c = job.compensation
    assert (c.currency, c.min_value, c.max_value, c.interval) == ("USD", 90000.0, None, "1 YEAR")


# ---------------------------------------------------------------- Jobspresso

def test_jobspresso_parse_minimal():
    jobs = JobspressoConnector().parse(_text("jobspresso.xml"), BOARDS["jobspresso"])
    _common(jobs, "jobspresso", "Jobspresso")
    assert len(jobs) == 4
    assert all(j.description_text == "" for j in jobs)        # store: minimal
    assert all(set(j.raw) <= {"title", "link", "company", "location", "salary", "pubDate", "post_id"}
               for j in jobs)
    hub = jobs[0]
    assert (hub.company, hub.external_id) == ("HubSpot", "165640")
    assert hub.posted_at == "2026-11-11T07:27:46+00:00"
    assert hub.apply_url == hub.url
    c = hub.compensation
    assert (c.currency, c.min_value, c.max_value, c.is_estimate) == ("USD", 54000.0, 90000.0, False)
    ramp = jobs[1]
    assert ramp.region_restrictions == ["United States", "Canada"]
    assert next(j for j in jobs if j.company == "Netflix").compensation is None


@pytest.mark.parametrize("text,expected", [
    ("US$54k - US$90k", ("USD", 54000.0, 90000.0, "1 YEAR", False)),
    ("€60,000 – €80,000 per year", ("EUR", 60000.0, 80000.0, "1 YEAR", False)),
    ("$40/hour", ("USD", 40.0, 40.0, "1 HOUR", False)),
    ("Competitive", (None, None, None, None, True)),
    ("80k - 100k", (None, None, None, None, True)),          # no currency -> no number
])
def test_jobspresso_salary_text(text, expected):
    c = parse_salary_text(text)
    assert (c.currency, c.min_value, c.max_value, c.interval, c.is_estimate) == expected
    assert c.summary == text
    assert parse_salary_text("") is None


# ---------------------------------------------------------- We Work Remotely

def test_weworkremotely_parse_minimal():
    body = _text("weworkremotely.xml")
    jobs = WeWorkRemotelyConnector().parse([body, body], BOARDS["weworkremotely"])  # 2 feeds, same items
    _common(jobs, "weworkremotely", "We Work Remotely")
    assert len(jobs) == 4                                     # cross-feed dedupe
    assert all(j.description_text == "" for j in jobs)
    assert all("description" not in j.raw for j in jobs)
    current = jobs[0]
    assert (current.company, current.title) == ("Current", "Freelance Marketo Email Engineer")
    assert current.external_id == "current-freelance-marketo-email-engineer"
    assert current.region_restrictions == ["Anywhere in the World"]
    assert current.apply_url == current.url
    assert current.posted_at == "2026-10-19T07:31:25+00:00"
    assert current.department == "Front-End Programming"
    usa = next(j for j in jobs if j.location_text == "USA Only")
    assert usa.region_restrictions == ["USA Only"]


def test_weworkremotely_expires_at_and_title_split():
    rss = ("<rss><channel><item><title>Acme: Backend Engineer: Go</title>"
           "<region>USA Only</region><link>https://weworkremotely.com/remote-jobs/acme-backend</link>"
           "<pubDate>Mon, 19 Oct 2026 07:31:25 +0000</pubDate>"
           "<expires_at>2026-12-01T00:00:00Z</expires_at></item></channel></rss>")
    job = WeWorkRemotelyConnector().parse(rss, BOARDS["weworkremotely"])[0]
    assert (job.company, job.title) == ("Acme", "Backend Engineer: Go")
    assert job.expires_at == "2026-12-01T00:00:00Z"
    assert split_title("No colon here") == ("", "No colon here")


def test_weworkremotely_fetch_uses_every_feed():
    class Resp:
        text = _text("weworkremotely.xml")

    class Fake:
        def __init__(self):
            self.urls: list[str] = []

        def get(self, url, **kw):
            self.urls.append(url)
            return Resp()

    fake = Fake()
    bodies = WeWorkRemotelyConnector(client=fake).fetch_raw(BOARDS["weworkremotely"])
    assert fake.urls == BOARDS["weworkremotely"]["feeds"] and len(bodies) == 2


# ------------------------------------------------------------------ regions

@pytest.mark.parametrize("text,expected", [
    ("Europe, LATAM, APAC, the U.S., Canada", ["Europe", "LATAM", "APAC", "the U.S.", "Canada"]),
    ("US/Canada; UK", ["US", "Canada", "UK"]),
    ("Europe and Africa", ["Europe", "Africa"]),
    ("Canada and Bosnia and Herzegovina", ["Canada", "Bosnia and Herzegovina"]),
    ("USA Only", ["USA Only"]),
    ("CET (+/- 3 hours)", ["CET (+/- 3 hours)"]),
    ("EMEA, emea, ", ["EMEA"]),
    ("", []),
    (None, []),
])
def test_split_regions(text, expected):
    assert split_regions(text) == expected


def test_partition_location():
    assert partition_location("Time zone: CET (+/- 3 hours)") == ([], "Time zone: CET (+/- 3 hours)")
    assert partition_location("EMEA; UTC-3 to UTC+2") == (["EMEA"], "UTC-3 to UTC+2")
    assert partition_location("Anywhere in the World") == (["Anywhere in the World"], None)
    assert partition_location(None) == ([], None)
    assert is_timezone_text("EST overlap") and not is_timezone_text("Estonia")


def test_parse_utc_offsets():
    assert parse_utc_offsets([-5, "3.5", 5.75, 5.75, 99]) == [-5.0, 3.5, 5.75]
    assert parse_utc_offsets("UTC+5:30 or GMT−3") == [-3.0, 5.5]
    assert parse_utc_offsets("CET (+/- 3 hours)") == []      # named zones aren't guessed
    assert parse_utc_offsets(None) == [] and parse_utc_offsets(2) == [2.0]


def test_regions_from_list():
    assert regions_from_list(["United States", {"name": "Canada"}, "united states"]) == \
        ["United States", "Canada"]
    assert regions_from_list(None) == []
