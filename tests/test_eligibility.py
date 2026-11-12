"""eligibility.evaluate: every hard rule both ways, lanes per tier, D8 reasons."""
from __future__ import annotations

import copy

import pytest

from jobcopilot import config
from jobcopilot.eligibility import Decision, evaluate, ist_overlap_hours
from jobcopilot.filters import FilterConfig, apply_filters, is_seniority_high
from jobcopilot.models import Compensation, Job

BRAND = {"name": "Acme", "tier": "brand"}
OK_TEXT = "Requirements\n- 1+ years of experience building backend services in Python."


def cfg(**over) -> dict:
    c = copy.deepcopy(config.DEFAULTS["filters"])
    for path, value in over.items():
        node = c
        keys = path.split("__")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
    return c


def job(**kw) -> Job:
    base = dict(source="test", company="Acme", company_sector="tech", external_id="1",
                title="Software Engineer", location_text="Bengaluru, India", workplace_type="onsite",
                country_bucket="IN", url="https://example.com/j/1", description_text=OK_TEXT)
    base.update(kw)
    return Job(**base)


def ev(j, company=BRAND, **over) -> Decision:
    return evaluate(j, company=company, cfg=cfg(**over))


def rules(d: Decision) -> set[str]:
    return {r["rule"] for r in d.reasons}


def assert_d8(d: Decision) -> None:
    assert d.excluded and d.exclusions
    for r in d.exclusions:
        assert r["rule"] and r["detail"] and r["excerpt"], r


# --- baseline ---------------------------------------------------------------
def test_clean_india_job_is_main_lane():
    d = ev(job())
    assert d.lane == "main" and d.reasons == []
    assert d.india_eligible is True and d.salary_ok is None and d.yoe_min == 1


# --- role -------------------------------------------------------------------
@pytest.mark.parametrize("title", ["Software Engineer", "Full Stack Developer", "GenAI Engineer",
                                   "Backend Developer (Go)", "Member of Technical Staff", "SDE 2"])
def test_role_match(title):
    assert "role" not in rules(ev(job(title=title)))


@pytest.mark.parametrize("title", ["Product Designer", "Inside Sales Rep", "Software Engineering Intern"])
def test_role_excluded(title):
    d = ev(job(title=title))
    assert "role" in rules(d)
    assert_d8(d)


def test_internships_allowed_when_configured():
    assert "role" not in rules(ev(job(title="Software Engineering Intern"), roles__exclude_internships=False))


# --- yoe / seniority -------------------------------------------------------------
def test_yoe_hard_above_max_excluded():
    d = ev(job(description_text="Requirements\n- 3+ years of professional software experience."))
    assert "yoe" in rules(d) and d.yoe_min == 3
    assert_d8(d)
    assert "3+ years" in next(r for r in d.reasons if r["rule"] == "yoe")["excerpt"]


def test_yoe_within_max_kept_and_max_configurable():
    assert "yoe" not in rules(ev(job(description_text="2+ years of experience with Java.")))
    assert "yoe" not in rules(ev(job(description_text="3+ years of experience with Java."),
                                 yoe__max_required=3))


def test_yoe_preferred_only_is_flag():
    d = ev(job(description_text="Nice to have\n- 5+ years of experience with Kafka."))
    assert d.lane == "main" and "flag:yoe_preferred" in rules(d) and d.yoe_is_preferred


def test_affirm_case_kept():
    text = ("What We Look For\n· You have a total of 1.5+ years of experience as a software engineer.\n"
            "Pay Grade - L\nEquity Grade - 5")
    d = ev(job(title="Software Engineer II, Backend", description_text=text))
    assert d.lane == "main" and d.yoe_min_years == 1.5 and d.yoe_min == 1


def test_senior_title_excluded():
    d = ev(job(title="Senior Backend Engineer", description_text="5+ years of experience with Go."))
    assert {"seniority", "yoe"} <= rules(d)
    assert_d8(d)
    d = ev(job(title="Staff Software Engineer", description_text="Build things."))
    assert "seniority" in rules(d)


def test_senior_title_with_low_jd_yoe_is_flag_only():
    d = ev(job(title="Senior Software Engineer", description_text="Requirements\n- 2+ years of experience."))
    assert d.lane == "main" and "flag:seniority" in rules(d)
    d = ev(job(title="Senior Software Engineer", description_text="Requirements\n- 2+ years of experience."),
           seniority__allow_if_jd_yoe_within_max=False)
    assert "seniority" in rules(d)


def test_senior_rule_can_be_switched_off():
    assert "seniority" not in rules(ev(job(title="Senior Software Engineer", description_text="x"),
                                       seniority__exclude_senior_titles=False))


def test_mts_is_not_staff():
    assert is_seniority_high("Member of Technical Staff") is False
    assert is_seniority_high("Staff Engineer") is True


# --- geo --------------------------------------------------------------------
def remote(**kw) -> Job:
    base = dict(location_text="Remote", workplace_type="remote", country_bucket="non-IN")
    base.update(kw)
    return job(**base)


@pytest.mark.parametrize("wtype", ["onsite", "hybrid", "remote", "unknown"])
def test_india_any_work_type(wtype):
    d = ev(job(workplace_type=wtype))
    assert "geo" not in rules(d) and d.india_eligible is True


def test_outside_india_not_remote_excluded():
    d = ev(job(location_text="New York, NY", workplace_type="onsite", country_bucket="non-IN"))
    assert "geo" in rules(d) and d.india_eligible is False
    assert_d8(d)


@pytest.mark.parametrize("regions", [["USA Only"], ["EU only"], ["Americas"], ["LATAM"], ["UK"],
                                     ["Europe"], ["United States", "Canada"]])
def test_restricted_regions_excluded(regions):
    d = ev(remote(region_restrictions=regions))
    assert "geo" in rules(d) and d.india_eligible is False
    assert_d8(d)


@pytest.mark.parametrize("regions", [["APAC"], ["Worldwide"], ["India"], ["Europe", "Asia"], ["Anywhere"],
                                     ["Global"], []])
def test_open_regions_kept(regions):
    d = ev(remote(region_restrictions=regions))
    assert "geo" not in rules(d) and d.india_eligible is True


@pytest.mark.parametrize("loc", ["Remote - US only", "Remote (USA)", "Berlin, Germany", "Remote, Europe",
                                 "Remote - LATAM"])
def test_location_text_restriction_excluded(loc):
    d = ev(remote(location_text=loc))
    assert "geo" in rules(d)
    assert_d8(d)


@pytest.mark.parametrize("loc", ["Remote", "Remote - Worldwide", "Anywhere", "Remote (APAC)",
                                 "Fully Remote", "Remote - India"])
def test_location_text_open_kept(loc):
    assert "geo" not in rules(ev(remote(location_text=loc)))


def test_description_restriction_excluded():
    d = ev(remote(description_text=OK_TEXT + "\nYou must be based in the United States."))
    assert "geo" in rules(d) and "United States" in next(r for r in d.reasons if r["rule"] == "geo")["excerpt"]
    assert "geo" not in rules(ev(remote(description_text=OK_TEXT + "\nWe hire anywhere in the world.")))
    assert "geo" not in rules(ev(remote(description_text=OK_TEXT + "\nYou must be based in the US."),
                                 geo__scan_description=False))


def test_ist_overlap_math():
    assert ist_overlap_hours(5.5) == 8
    assert ist_overlap_hours(1) == 3.5            # CET
    assert ist_overlap_hours(0) == 2.5            # UK
    assert ist_overlap_hours(-5) == 0             # US East


@pytest.mark.parametrize("offsets,ok", [([-8, -5], False), ([0], False), ([1], True), ([8], True),
                                        ([-5, 2], True)])
def test_timezone_offsets(offsets, ok):
    d = ev(remote(timezone_restrictions=offsets))
    assert ("geo" not in rules(d)) is ok


@pytest.mark.parametrize("tz,ok", [("CET (+/- 3 hours)", True), ("EST overlap required", False),
                                   ("UTC-8 to UTC-5", False), ("UTC+3 to UTC+9", True),
                                   ("Must work US Eastern time hours", False), ("GMT +/- 2h", True)])
def test_timezone_text(tz, ok):
    d = ev(remote(tz_requirement_text=tz))
    assert ("geo" not in rules(d)) is ok, d.reasons


def test_overlap_threshold_configurable():
    assert "geo" not in rules(ev(remote(timezone_restrictions=[0]), geo__min_ist_overlap_hours=2))


def test_unknown_country_kept_and_flagged():
    d = ev(job(location_text="", workplace_type="unknown", country_bucket="unknown"))
    assert d.lane == "main" and "flag:country_unknown" in rules(d) and d.india_eligible is None
    d = ev(job(location_text="", workplace_type="unknown", country_bucket="unknown"),
           geo__keep_unknown_country=False)
    assert "geo" in rules(d)


def test_unknown_country_with_us_only_region_excluded():
    d = ev(job(country_bucket="unknown", workplace_type="remote", region_restrictions=["USA Only"]))
    assert "geo" in rules(d)


# --- salary -----------------------------------------------------------------
def pay(cur, lo, hi, interval="1 YEAR", **kw) -> Job:
    return remote(compensation=Compensation(currency=cur, min_value=lo, max_value=hi, interval=interval), **kw)


@pytest.mark.parametrize("cur,lo,hi,ok", [
    ("INR", 800_000, 1_200_000, False), ("INR", 1_500_000, 2_200_000, True),
    ("USD", 15_000, 25_000, False), ("USD", 40_000, 60_000, True),
    ("EUR", 20_000, 25_000, False), ("EUR", 30_000, 45_000, True),
])
def test_salary_floor_per_currency(cur, lo, hi, ok):
    d = ev(pay(cur, lo, hi))
    assert d.salary_ok is ok
    assert ("salary" in rules(d)) is (not ok)
    assert d.salary_annual_inr == pytest.approx(hi * {"INR": 1, "USD": 83.0, "EUR": 90.0}[cur])
    if not ok:
        assert_d8(d)


def test_salary_hourly_and_monthly_annualised():
    assert ev(pay("USD", 20, 25, "1 HOUR")).salary_ok is True       # 52,000/yr
    assert ev(pay("INR", 80_000, 100_000, "1 MONTH")).salary_ok is False   # 12 L/yr


def test_salary_other_currency_via_fx_or_flag():
    assert ev(pay("GBP", 20_000, 22_000), fx_to_inr__GBP=105.0).salary_ok is True    # 23.1 L >= 15 L
    d = ev(pay("GBP", 10_000, 12_000), fx_to_inr__GBP=105.0)
    assert d.salary_ok is False and "salary" in rules(d)
    d = ev(pay("JPY", 4_000_000, 6_000_000))
    assert d.salary_ok is None and "flag:salary_unknown_currency" in rules(d) and d.lane != "excluded"


def test_no_salary_kept_with_none():
    d = ev(job())
    assert d.salary_ok is None and d.salary_annual_inr is None and "salary" not in rules(d)


def test_salary_from_store_row_dict():
    row = {"title": "Backend Engineer", "description_text": OK_TEXT, "country_bucket": "IN",
           "workplace_type": "onsite", "location_text": "Pune", "comp_currency": "INR",
           "comp_min": 600_000, "comp_max": 900_000, "comp_interval": "1 YEAR",
           "region_restrictions": "[]", "timezone_restrictions": "[]", "company": "Acme",
           "company_tier": "funded"}
    d = evaluate(row, company=None, cfg=cfg())
    assert d.salary_ok is False and "salary" in rules(d) and d.company_tier == "funded"


# --- company tiers / lanes -------------------------------------------------------
@pytest.mark.parametrize("tier,lane", [("brand", "main"), ("funded", "main"), ("watchlist", "extended"),
                                       ("excluded", "excluded")])
def test_lane_per_tier(tier, lane):
    d = ev(job(), company={"name": "Acme", "tier": tier, "tier_reasons": ["body shop: name pattern"]})
    assert d.lane == lane and d.company_tier == tier
    if tier == "excluded":
        assert "company" in rules(d)
        assert_d8(d)


def test_watchlist_excluded_when_extended_lane_off():
    d = ev(job(), company={"name": "Acme", "tier": "watchlist"}, companies__include_extended=False)
    assert "company" in rules(d) and d.excluded


@pytest.mark.parametrize("company", [None, {"name": "Acme", "tier": "unknown"}])
def test_unknown_or_missing_company_goes_extended_with_flag(company):
    d = ev(job(), company=company)
    assert d.lane == "extended" and "flag:company:unverified" in rules(d)


def test_unknown_company_can_be_excluded_by_config():
    d = ev(job(), company=None, companies__unknown_tier_lane="excluded")
    assert "company" in rules(d) and d.excluded
    assert_d8(d)


def test_tier_from_job_dict_field():
    assert evaluate({"title": "Software Engineer", "country_bucket": "IN", "company_tier": "brand"},
                    cfg=cfg()).lane == "main"


# --- freshness ------------------------------------------------------------------
def test_stale_flag_never_excludes():
    d = ev(job(posted_age_days=90))
    assert d.lane == "main" and "flag:stale" in rules(d)
    assert "flag:stale" not in rules(ev(job(posted_age_days=10)))


# --- D8 + apply_filters --------------------------------------------------------------
def test_every_exclusion_carries_reason_and_flags_do_not_exclude():
    d = ev(remote(title="Senior Product Designer", region_restrictions=["USA Only"],
                  description_text="7+ years of experience.",
                  compensation=Compensation(currency="USD", min_value=10_000, max_value=12_000,
                                            interval="1 YEAR")),
           company={"name": "Shop", "tier": "excluded"})
    assert {"role", "yoe", "seniority", "geo", "salary", "company"} <= rules(d)
    assert_d8(d)


def test_apply_filters_attaches_decision_and_eligibility_mode_drops_excluded():
    jobs = [job(external_id="a"), job(external_id="b", title="Senior Software Engineer",
                                      description_text="6+ years of experience.")]
    legacy = apply_filters(jobs, FilterConfig(cfg=cfg()))
    assert len(legacy) == 2 and all(isinstance(r.decision, Decision) for r in legacy)
    strict = apply_filters(jobs, FilterConfig(cfg=cfg(), mode="eligibility",
                                              companies={"Acme": BRAND}))
    assert [r.job.external_id for r in strict] == ["a"]
    assert strict[0].decision.lane == "main" and strict[0].job.min_yoe_required == 1
