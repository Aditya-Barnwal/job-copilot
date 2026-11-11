"""Offline parse tests for SmartRecruiters, Gem and EU-hosted Greenhouse boards.

smartrecruiters.json and gem.json follow the documented response shapes: both
API hosts are robots-blocked for us (see the connector docstrings), so live
payloads could not be recorded. greenhouse_eu_groww.json is trimmed to the shape
of the live Groww payload (absolute_url on job-boards.eu.greenhouse.io).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jobcopilot.connectors.gem import GemConnector
from jobcopilot.connectors.greenhouse import GreenhouseConnector, board_region
from jobcopilot.connectors.smartrecruiters import SmartRecruitersConnector

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class _PagingClient:
    """Serves SmartRecruiters pages by offset and records requests."""

    def __init__(self, total: int) -> None:
        self.total, self.calls = total, []

    def get_json(self, url: str, params: dict | None = None, **_: Any) -> Any:
        params = params or {}
        self.calls.append(params)
        off, lim = int(params.get("offset", 0)), int(params.get("limit", 100))
        n = max(0, min(lim, self.total - off))
        return {"totalFound": self.total, "content": [{"id": str(off + i), "name": "x"} for i in range(n)]}


# ----------------------------------------------------------- SmartRecruiters

def test_smartrecruiters_parse() -> None:
    company = {"name": "Freshworks", "slug": "Freshworks", "sector": "tech"}
    jobs = SmartRecruitersConnector().parse(load("smartrecruiters.json"), company)
    assert [j.title for j in jobs] == [
        "Senior Software Engineer - Backend", "Site Reliability Engineer", "Product Manager"]
    a, b, c = jobs
    assert a.location_text == "Bengaluru, Karnataka, India" and a.country_bucket == "IN"
    assert a.workplace_type == "hybrid" and a.department == "Engineering"
    assert a.url == "https://jobs.smartrecruiters.com/Freshworks/744000101010101"
    assert a.posted_at == "2026-11-02T09:30:00.000Z" and a.description_text == ""
    assert b.location_text == "India (Remote)" and b.workplace_type == "remote"
    assert b.department == "Engineering"            # falls back to `function`
    assert c.country_bucket == "non-IN" and c.workplace_type == "unknown"
    assert all(j.source == "smartrecruiters" and j.company_sector == "tech" for j in jobs)


def test_smartrecruiters_paginates_politely_and_caps() -> None:
    client = _PagingClient(total=250)
    raw = SmartRecruitersConnector(client=client).fetch_raw({"name": "X", "slug": "X"})
    assert len(raw["content"]) == 250 and [p["offset"] for p in client.calls] == [0, 100, 200]
    client = _PagingClient(total=1000)
    raw = SmartRecruitersConnector(client=client).fetch_raw({"name": "X", "slug": "X", "max": 150})
    assert len(client.calls) == 2 and len(raw["content"]) == 200


def test_smartrecruiters_empty_payload() -> None:
    assert SmartRecruitersConnector().parse({}, {"name": "X", "slug": "X"}) == []
    assert SmartRecruitersConnector().parse(None, {"name": "X", "slug": "X"}) == []


# ----------------------------------------------------------------------- Gem

def test_gem_parse() -> None:
    company = {"name": "PromptQL", "slug": "promptql", "sector": "tech"}
    jobs = GemConnector().parse(load("gem.json"), company)
    a, b = jobs
    assert a.title == "Founding Engineer, Agents" and a.external_id == "4621552004"
    assert a.workplace_type == "hybrid" and a.country_bucket == "IN"
    assert a.description_text == "Build agent infrastructure." and a.department == "Engineering"
    assert a.posted_at == "2026-10-09T20:19:43.000Z"
    assert a.url == "https://jobs.gem.com/promptql/4621552004"
    # no location name -> offices; no absolute_url -> board link; no plain text -> stripped HTML
    assert b.location_text == "San Francisco" and b.workplace_type == "remote"
    assert b.url == "https://jobs.gem.com/promptql/4621552005"
    assert b.description_text == "Talk to developers." and b.posted_at == "2026-11-03T20:02:47.278Z"


def test_gem_accepts_wrapped_payload() -> None:
    raw = {"job_posts": load("gem.json")}
    assert len(GemConnector().parse(raw, {"name": "P", "slug": "promptql"})) == 2
    assert GemConnector().parse(None, {"name": "P", "slug": "promptql"}) == []


# ------------------------------------------------------------- Greenhouse EU

def test_greenhouse_eu_parse_and_link_fallback() -> None:
    company = {"name": "Groww", "slug": "groww", "region": "eu", "sector": "fintech"}
    a, b = GreenhouseConnector().parse(load("ats/greenhouse_eu_groww.json"), company)
    assert a.url.startswith("https://job-boards.eu.greenhouse.io/groww/jobs/")
    assert a.posted_at == "2026-10-28T10:00:00+05:30"        # first_published preferred
    assert "Java and Go" in a.description_text and a.department == "Engineering"
    assert b.url == "https://job-boards.eu.greenhouse.io/groww/jobs/4011001102"
    assert b.posted_at == "2026-11-06T10:00:00+05:30" and b.department is None
    assert a.country_bucket == "IN" and b.country_bucket == "IN"


def test_greenhouse_eu_uses_public_api_host() -> None:
    assert GreenhouseConnector.list_url("groww", "eu") == \
        "https://boards-api.greenhouse.io/v1/boards/groww/jobs"
    assert GreenhouseConnector.list_url("stripe") == \
        "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
    assert board_region({"region": "EU"}) == "eu" and board_region({}) == "us"
    with pytest.raises(ValueError):
        board_region({"region": "apac"})


# ------------------------------------------------------------- config rows

@pytest.mark.parametrize("fname", ["companies.yaml", "target_companies.yaml"])
def test_company_config_rows_are_complete(fname: str) -> None:
    """Every pulled row names a known connector, carries the keys it reads, and a tier."""
    import yaml
    rows = yaml.safe_load((FIX.parent.parent / "config" / fname).read_text(encoding="utf-8"))["companies"]
    need = {"greenhouse": {"slug"}, "lever": {"slug"}, "ashby": {"slug"}, "gem": {"slug"},
            "smartrecruiters": {"slug"}, "workday": {"tenant", "wd_server", "site"}}
    for row in rows:
        conn = row.get("connector")
        if conn == "manual":
            continue
        assert conn in need, row["name"]
        assert need[conn] <= row.keys(), row["name"]
        assert row.get("tier") in ("brand", "funded") and row.get("tier_reason"), row["name"]
        if conn == "greenhouse":
            board_region(row)  # raises on a bad region
