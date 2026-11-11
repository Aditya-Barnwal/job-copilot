"""Offline tests: every connector's parser + the filter logic, run against
saved fixtures with no network. These also serve as living documentation of
exactly what each connector produces.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobcopilot.connectors import (
    GreenhouseConnector,
    LeverConnector,
    AshbyConnector,
    WorkdayConnector,
    AdzunaConnector,
    JsearchConnector,
)
from jobcopilot.filters import FilterConfig, apply_filters
from jobcopilot.models import IN, NON_IN, REMOTE, ONSITE, HYBRID

FIX = Path(__file__).resolve().parent / "fixtures"


def load(name):
    with open(FIX / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ----------------------------- parsers -----------------------------

def test_greenhouse_parse():
    jobs = GreenhouseConnector().parse(load("greenhouse.json"), {"name": "Uber", "sector": "tech"})
    assert len(jobs) == 3
    blr = next(j for j in jobs if "Bengaluru" in j.location_text)
    assert blr.country_bucket == IN
    assert blr.department == "Engineering"
    assert "Python" in blr.description_text and "<" not in blr.description_text  # html stripped
    us_remote = next(j for j in jobs if j.title == "Backend Engineer")
    assert us_remote.workplace_type == REMOTE
    assert us_remote.country_bucket == NON_IN


def test_lever_parse_uses_explicit_workplace_type():
    jobs = LeverConnector().parse(load("lever.json"), {"name": "Motorq", "sector": "tech"})
    assert len(jobs) == 4
    by_title = {j.title: j for j in jobs}
    assert by_title["Senior Software Engineer"].workplace_type == REMOTE
    assert by_title["Embedded Engineer"].workplace_type == ONSITE
    assert by_title["Solutions Architect"].workplace_type == HYBRID
    assert by_title["Embedded Engineer"].country_bucket == IN


def test_ashby_parse_compensation_and_listing_skip():
    jobs = AshbyConnector().parse(load("ashby.json"), {"name": "BalkanID", "sector": "tech"})
    # the isListed:false role must be dropped
    assert len(jobs) == 3
    senior = next(j for j in jobs if j.title == "Senior Software Engineer")
    assert senior.compensation is not None
    assert senior.compensation.currency == "USD"
    assert senior.compensation.max_value == 180000
    # equity tier must NOT be chosen as the salary number
    assert senior.compensation.min_value == 150000
    india = next(j for j in jobs if j.title == "Data Scientist")
    assert india.compensation.currency == "INR"
    assert round(india.compensation.annual_inr() / 100000) == 25  # 25 LPA


def test_workday_parse_builds_urls():
    company = {"name": "Adobe", "sector": "tech", "tenant": "adobe", "wd_server": "wd5", "site": "external_experienced"}
    jobs = WorkdayConnector().parse(load("workday.json"), company)
    assert len(jobs) == 3
    swe = next(j for j in jobs if j.title == "Software Engineer")
    assert swe.country_bucket == IN
    assert swe.external_id == "R-12345"
    assert swe.url.startswith("https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/")


# ----------------------------- filters -----------------------------

def _all_jobs():
    jobs = []
    jobs += GreenhouseConnector().parse(load("greenhouse.json"), {"name": "Uber", "sector": "tech"})
    jobs += LeverConnector().parse(load("lever.json"), {"name": "Motorq", "sector": "tech"})
    jobs += AshbyConnector().parse(load("ashby.json"), {"name": "BalkanID", "sector": "tech"})
    jobs += WorkdayConnector().parse(
        load("workday.json"),
        {"name": "Adobe", "sector": "tech", "tenant": "adobe", "wd_server": "wd5", "site": "external_experienced"},
    )
    return jobs


def test_geo_filter_drops_non_india_non_remote():
    jobs = _all_jobs()
    results = apply_filters(jobs, FilterConfig(min_lpa=15.0))
    titles = {r.job.title for r in results}
    # non-India + not remote -> dropped
    assert "Platform Engineer" not in titles      # NYC onsite
    assert "Solutions Architect" not in titles     # London hybrid
    assert "Product Designer" not in titles        # San Jose onsite
    # India (any type) + non-India remote -> kept
    assert "Software Engineer II" in titles        # Bengaluru onsite
    assert "Data Engineer" in titles               # Berlin remote


def test_salary_floor_drops_low_pay_but_keeps_undisclosed():
    jobs = _all_jobs()
    results = apply_filters(jobs, FilterConfig(min_lpa=15.0))
    titles = {r.job.title for r in results}
    # disclosed below 15 LPA -> dropped
    assert "Junior Support Analyst" not in titles
    # disclosed above 15 LPA -> kept, not flagged as estimate
    senior = next(
        r for r in results
        if r.job.title == "Senior Software Engineer" and r.job.company == "BalkanID"
    )
    assert senior.package_estimated is False
    # no disclosed pay -> kept and flagged
    swe = next(r for r in results if r.job.title == "Software Engineer")
    assert swe.package_estimated is True


def test_expected_total_passes():
    jobs = _all_jobs()
    assert len(jobs) == 13  # 3 + 4 + 3 (ashby drops 1 unlisted) + 3
    results = apply_filters(jobs, FilterConfig(min_lpa=15.0))
    # role-targeting now drops Embedded Engineer + Site Reliability Engineer
    # from the previous 9 -> 7
    assert len(results) == 7


def test_role_targeting_drops_non_target_titles():
    from jobcopilot.filters import passes_role, DEFAULT_TARGET_ROLES
    assert passes_role("Software Engineer II", DEFAULT_TARGET_ROLES) == "software_engineer"
    assert passes_role("Backend Engineer", DEFAULT_TARGET_ROLES) == "backend_engineer"
    assert passes_role("Data Scientist", DEFAULT_TARGET_ROLES) == "data_scientist"
    # "sde" only matches on word boundaries
    assert passes_role("SDE 2 - Search", DEFAULT_TARGET_ROLES) == "sde"
    assert passes_role("Inside Sales Rep", DEFAULT_TARGET_ROLES) is None
    assert passes_role("Product Designer", DEFAULT_TARGET_ROLES) is None
    assert passes_role("Embedded Engineer", DEFAULT_TARGET_ROLES) is None


def test_adzuna_parse():
    jobs = AdzunaConnector().parse(load("adzuna.json"), {"name": "Adzuna IN SWE", "sector": "tech"})
    assert len(jobs) == 2
    be = jobs[0]
    assert be.title.startswith("Backend Engineer")
    assert be.country_bucket == IN
    assert be.compensation is not None
    assert be.compensation.currency == "INR"
    assert be.compensation.max_value == 2500000
    assert "<" not in be.description_text  # html stripped


def test_jsearch_parse():
    jobs = JsearchConnector().parse(load("jsearch.json"), {"name": "JSearch IN SWE", "sector": "tech"})
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Software Engineer"
    assert j.country_bucket == IN
    assert j.compensation.currency == "INR"


def test_yoe_extractor():
    from jobcopilot.yoe import extract_min_yoe
    assert extract_min_yoe("5+ years of experience required") == (5, False)
    assert extract_min_yoe("Minimum 3 years experience in Python") == (3, False)
    assert extract_min_yoe("3-5 years of experience") == (3, False)
    assert extract_min_yoe("2+ years preferred") == (2, True)
    assert extract_min_yoe("Founded in 2015. We launched our service in 2018.") == (None, False)
    assert extract_min_yoe("10+ years combined team experience") == (None, False)
    assert extract_min_yoe("Graduating in 2025 with relevant coursework") == (None, False)
    assert extract_min_yoe("") == (None, False)
    assert extract_min_yoe(None) == (None, False)


def test_numeric_seniority():
    from jobcopilot.filters import is_seniority_high
    # FLAG
    assert is_seniority_high("Machine Learning Engineer 4") is True
    assert is_seniority_high("L5 Engineer") is True
    assert is_seniority_high("Software Developer III") is True
    assert is_seniority_high("SDE 3") is True
    assert is_seniority_high("Senior Backend Engineer") is True
    # DON'T flag
    assert is_seniority_high("Software Engineer") is False
    assert is_seniority_high("Software Engineer II") is False
    assert is_seniority_high("Software Engineer 2") is False
    assert is_seniority_high("Engineer L2") is False
    # 4-digit-year shouldn't cause a false positive
    assert is_seniority_high("Software Engineer 2025") is False


def test_resume_options_ranking_and_recommended_gate(tmp_path):
    """Ranks by coverage, marks best as recommended only if ≥ REUSE_THRESHOLD,
    and always appends a `tailor_fresh` option.

    Passes db_path explicitly to every store.* call to keep the test sandboxed
    (function-arg defaults bind at def time, so monkeypatch on DEFAULT_DB_PATH
    would not sandbox the writes; this style is verified isolation)."""
    from jobcopilot import store, tailor
    from jobcopilot.filters import FilterResult
    from jobcopilot.models import Job
    db = tmp_path / "opt.db"
    store.init_db(db)

    req = ["python","django","aws","postgres","redis","kafka","docker","react","go","kubernetes"]
    job = Job(
        source="greenhouse", company="Foo", company_sector="tech",
        external_id="1", title="Backend Engineer",
        location_text="Bengaluru", workplace_type="onsite", country_bucket="IN",
        url="https://x/y", description_text="d",
    )
    store.upsert_jobs([FilterResult(job=job, package_estimated=True, country_uncertain=False)], db_path=db)
    store.set_scores(job.key, 60.0, 0.5, req, [], db_path=db)
    store.set_resume("Python Django AWS Postgres Redis experience", "orig.pdf", db_path=db)
    store.add_version("v1", role_family=None, source_job_key=None,
                      source_company=None, tailored_text="…",
                      pdf_path=str(tmp_path / "v1.pdf"),
                      target_keywords=["python","django","aws"],
                      base_ats_coverage=0.3, db_path=db)
    store.add_version("v2 backend", role_family=None, source_job_key=None,
                      source_company=None, tailored_text="…",
                      pdf_path=str(tmp_path / "v2.pdf"),
                      target_keywords=["python","django","aws","postgres","redis","kafka","docker","react","go"],
                      base_ats_coverage=0.9, db_path=db)

    # `tailor.resume_options` reads via `store.get_job` / `store.list_versions`
    # which take an optional db_path. Patch the module-level defaults for the
    # duration of the call by injecting a tiny helper.
    import functools
    orig_get_job = store.get_job
    orig_get_resume = store.get_resume
    orig_list_versions = store.list_versions
    store.get_job = functools.partial(orig_get_job, db_path=db)
    store.get_resume = functools.partial(orig_get_resume, db_path=db)
    store.list_versions = functools.partial(orig_list_versions, db_path=db)
    try:
        result = tailor.resume_options(job.key)
    finally:
        store.get_job = orig_get_job
        store.get_resume = orig_get_resume
        store.list_versions = orig_list_versions

    opts = result["options"]
    assert opts[-1]["source"] == "tailor_fresh"
    covered_opts = [o for o in opts if o["source"] != "tailor_fresh"]
    covs = [o["coverage_pct"] for o in covered_opts]
    assert covs == sorted(covs, reverse=True)
    assert covered_opts[0]["label"] == "v2 backend"
    assert covered_opts[0]["recommended"] is True
    assert all(not o["recommended"] for o in covered_opts[1:])


def test_profile_and_answer_pack_roundtrip(tmp_path):
    from jobcopilot import store
    db = tmp_path / "ap.db"
    store.init_db(db)
    store.set_profile({"first_name": "Ada", "last_name": "L", "email": "a@example.com",
                       "phone": "+1", "linkedin_url": "https://li/ada",
                       "portfolio_url": "https://gh/ada"}, db_path=db)
    store.upsert_answer("Notice period", "30 days", db_path=db)
    p = store.get_profile(db_path=db)
    assert p["first_name"] == "Ada"
    assert p["email"] == "a@example.com"
    ans = store.get_answer_pack(db_path=db)
    assert any(a["q"] == "Notice period" and a["a"] == "30 days" for a in ans)


def test_parse_json_salvages_trailing_comma_and_fences():
    from jobcopilot.llm import parse_json
    # code fence
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    # trailing comma before }
    assert parse_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}
    # missing closing brace
    assert parse_json('{"a": 1, "b": 2') == {"a": 1, "b": 2}
    # prose wrapping an object
    assert parse_json('Sure, here is the JSON:\n{"tailored_text":"hi"}\nDone.') == {"tailored_text": "hi"}


def test_logging_setup_attaches_to_parent(tmp_path):
    import logging as _l
    from jobcopilot.ingest import setup_logging
    # Reset the parent logger so this test is independent.
    parent = _l.getLogger("jobcopilot")
    for h in list(parent.handlers):
        parent.removeHandler(h)
    setup_logging(tmp_path)
    child = _l.getLogger("jobcopilot.scoring")
    child.info("hello from scoring")
    # Force flush
    for h in parent.handlers:
        h.flush()
    log_path = tmp_path / "jobcopilot.log"
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert "hello from scoring" in contents


def test_freshness_bucketing():
    from jobcopilot.normalize import freshness_bucket, posted_age_days
    assert freshness_bucket(None) == "unknown"
    assert freshness_bucket(5) == "fresh"
    assert freshness_bucket(60) == "recent"
    assert freshness_bucket(120) == "aging"
    assert freshness_bucket(300) == "stale"
    # Workday relative strings
    assert posted_age_days("Posted Today", "workday") == 0
    assert posted_age_days("Posted 3 Days Ago", "workday") == 3
    assert posted_age_days("Posted 30+ Days Ago", "workday") == 30


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_normalize_company_name_and_domain():
    from jobcopilot.normalize import normalize_company_name as n, domain_of as d
    assert n("Emergent Labs, Inc.") == "emergent labs"
    assert n("LiteLLM (BerriAI)") == "litellm"
    assert n("Computer Age Management Services Ltd.") == "computer age management services"
    assert n("Société Générale") == "societe generale"
    assert n("Co") == "co" and n("") == ""
    assert d("https://www.Emergent.sh/careers") == "emergent.sh"
    assert d("revenuecat.com") == "revenuecat.com"
    assert d("https://jobs.ashbyhq.com:443/litellm") == "jobs.ashbyhq.com"


def test_ashby_compensation_nested_live_shape():
    """F2: the live Ashby API nests typed salary numbers in summaryComponents /
    compensationTiers[].components; the parser must read them, not just the text."""
    from jobcopilot.connectors.ashby import _parse_compensation
    comp = {
        "compensationTierSummary": "$180K – $220K • 0.1% – 0.2%",
        "summaryComponents": [
            {"compensationType": "EquityPercentage", "minValue": 0.1, "maxValue": 0.2},
            {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD",
             "minValue": 180000, "maxValue": 220000},
        ],
        "compensationTiers": [{"title": "Zone A", "components": [
            {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD",
             "minValue": 180000, "maxValue": 220000}]}],
    }
    c = _parse_compensation(comp)
    assert (c.currency, c.min_value, c.max_value, c.interval) == ("USD", 180000, 220000, "1 YEAR")
    assert c.summary.startswith("$180K")


def test_normalize_company_name_more_legal_suffixes():
    # correctness-6
    from jobcopilot.normalize import normalize_company_name as n
    assert n("Proxify AB") == "proxify"
    assert n("Posit Software, PBC") == "posit software"
    assert n("Celonis SE") == "celonis"
    assert n("Wolt Oy") == "wolt"
    assert n("Nokia Oyj") == n("Nokia") == "nokia"
    assert n("Kahoot! ASA") == n("Kahoot AS") == "kahoot"
    assert n("Booking.com B.V.") == n("Booking.com BV") == "booking com"
    assert n("Ferrari S.p.A.") == "ferrari" and n("Acme S.r.l.") == "acme"
    assert n("Doctolib SAS") == "doctolib" and n("Acme SARL") == "acme"
    assert n("Mercari K.K.") == "mercari" and n("Philips N.V.") == "philips"
    # never empty, and a bare suffix-looking name is kept
    assert n("AB") == "ab" and n("SAS") == "sas" and n("S.A.") == "sa"
