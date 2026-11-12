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
                                   "Backend Developer (Go)", "Associate Member of Technical Staff", "SDE 2"])
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
    # E1: a placeless unknown is kept but never in the main lane
    assert d.lane == "extended" and {"flag:country_unknown", "flag:geo_placeless"} <= rules(d)
    assert d.india_eligible is None
    d = ev(job(location_text="", workplace_type="unknown", country_bucket="unknown"),
           geo__keep_unknown_country=False)
    assert "geo" in rules(d)


def test_unknown_country_with_us_only_region_excluded():
    d = ev(job(country_bucket="unknown", workplace_type="remote", location_text="Remote",
               region_restrictions=["USA Only"]))
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


# =============================================================================
# Phase 2 review fixes (real titles / locations from the reviewer's run)
# =============================================================================
FUNDED = {"name": "Acme", "tier": "funded"}
WATCH = {"name": "Acme", "tier": "watchlist"}


# --- E1: unknown-country geo ---------------------------------------------------
@pytest.mark.parametrize("loc,wtype", [
    ("Remote US", "remote"), ("US-WA-Bellevue", "onsite"), ("US-CA-Menlo Park", "onsite"),
    ("Shenzhen, China", "onsite"), ("Mexico City, MX", "onsite"), ("NYC, Sea, SF, Tor", "unknown"),
    ("Peninsula (Onsite)", "onsite"), ("sf", "unknown"), ("Remote - US", "remote"),
    ("Mountain View, CA", "hybrid"), ("Trento", "onsite"),
])
def test_e1_unknown_bucket_places_outside_india_excluded(loc, wtype):
    d = ev(job(location_text=loc, workplace_type=wtype, country_bucket="unknown"))
    assert "geo" in rules(d), (loc, d.reasons)
    assert_d8(d)


@pytest.mark.parametrize("loc", ["Mohali, IND", "Bengaluru", "Remote - India", "Gurugram, HR", "Mysuru"])
def test_e1_unknown_bucket_india_places_kept(loc):
    d = ev(job(location_text=loc, workplace_type="onsite", country_bucket="unknown"))
    assert "geo" not in rules(d) and d.lane == "main", d.reasons


@pytest.mark.parametrize("loc", ["Remote", "", "Hybrid", "2 Locations", "In-Office", "N/A"])
def test_e1_placeless_unknown_goes_to_extended_never_main(loc):
    d = ev(job(location_text=loc, workplace_type="remote", country_bucket="unknown"))
    assert d.lane == "extended" and "flag:geo_placeless" in rules(d), d.reasons


def test_e1_placeless_with_worldwide_or_india_hq_stays_main():
    d = ev(job(location_text="Anywhere in the World", workplace_type="remote", country_bucket="unknown"))
    assert d.lane == "main" and d.india_eligible is True
    d = ev(job(location_text="Remote", workplace_type="remote", country_bucket="unknown"),
           company={"name": "Acme", "tier": "brand", "hq_location": "Bengaluru, India"})
    assert d.lane == "main" and "flag:geo_placeless" not in rules(d)


# --- E2/E3: role gate + the approved taxonomy -----------------------------------
@pytest.mark.parametrize("title", [
    "Data Validator – AI/ML Systems", "GenAI Compliance Operations & Programs Associate",
    "Marketing AI Engineer", "Sales Engineer", "Support Engineer", "Member of Technical Staff, Customer Success",
    "Data Analyst Recruiter", "AI Trainer - Software Engineering", "Business Analyst", "Disputes Analyst",
    "Financial Analyst", "Technical Program Manager", "Product Manager, AI", "Data Annotator",
    "Design Automation DA", "Frontend Engineer", "Android Developer", "ETL Developer", "Drools Developer",
    "Mechanical Engineer",
])
def test_e2_non_target_titles_excluded_with_role_reason(title):
    d = ev(job(title=title))
    assert "role" in rules(d), d.reasons
    assert_d8(d)


@pytest.mark.parametrize("title,family", [
    # primary (>= 2 per family)
    ("Software Engineer I", "sde"), ("SDE-2", "sde"), ("SWE II", "sde"), ("Graduate Engineer Trainee", "sde"),
    ("GET - Software", "sde"), ("Associate Software Engineer", "sde"), ("Analyst Programmer", "sde"),
    ("Analyst I Software Engineering", "sde"), ("Quantitative Analyst / Software Developer", "sde"),
    ("Backend Engineer", "backend"), ("Golang Developer", "backend"),
    ("Java Developer", "java"), ("Spring Boot Developer", "java"),
    ("Python Developer", "python"), ("Django Developer", "python"),
    ("AI Engineer", "ai"), ("Applied AI Engineer", "ai"), ("GenAI Engineer", "ai"), ("LLM Engineer", "ai"),
    ("Agentic AI Engineer", "ai"), ("Prompt Engineer", "ai"), ("Voice AI Engineer", "ai"),
    ("Speech Engineer", "ai"), ("Conversational AI Developer", "ai"),
    ("NLP Engineer", "nlp"), ("Natural Language Processing Engineer", "nlp"),
    ("ML Engineer", "ml"), ("MLE II", "ml"), ("Machine Learning Engineer", "ml"),
    ("CV Engineer", "cv"), ("Computer Vision Engineer", "cv"),
    ("Data Engineer", "data_engineering"), ("Big Data Engineer", "data_engineering"),
    ("Data Scientist", "data_science"), ("DS II", "data_science"), ("DS - ML Platform", "data_science"),
    ("Applied Scientist", "data_science"),
    ("FDE - Bengaluru", "fde"), ("Software Engineer, Forward Deployed", "fde"),
    ("Forward-Deployed Software Engineer", "fde"), ("Forward Deployed AI Engineer", "fde"),
    ("Founding Engineer", "founding"), ("Product Engineer", "founding"),
    ("Associate Member of Technical Staff", "amts"), ("AMTS", "amts"),
    # secondary
    ("Data Analyst", "data_analyst"), ("Associate Data Analyst", "data_analyst"), ("Data Analyst II", "data_analyst"),
    ("Product Data Analyst", "data_analyst"), ("DA (Bengaluru)", "data_analyst"),
    ("Analytics Engineer", "analytics_engineering"), ("Analytics Engineer II", "analytics_engineering"),
    ("MLOps Engineer", "mlops"), ("ML Ops Engineer", "mlops"),
    ("Full Stack Developer", "fullstack"), ("Analyst - Full Stack Developer", "fullstack"),
    # E12: generic SWE with a non-family suffix stays sde
    ("Software Engineer - Infrastructure", "sde"), ("Software Engineer, Quality", "sde"),
])
def test_e3_target_families(title, family):
    d = ev(job(title=title))
    assert "role" not in rules(d), d.reasons
    assert d.role_family == family


@pytest.mark.parametrize("title,family", [
    ("Platform Engineer", "platform"), ("Cloud Engineer (GCP)", "cloud"), ("AWS Engineer", "cloud"),
    ("Integration Engineer", "integration"), ("API Engineer", "integration"),
    ("Research Engineer, AI", "research_engineering"), ("Research Scientist", "research_science"),
    ("SRE II", "sre"), ("Site Reliability Engineer", "sre"), ("Observability Engineer", "observability"),
    ("DevOps Engineer", "devops"), ("DevSecOps Engineer", "devops"), ("SDET", "qa"),
    ("Software Engineer in Test", "qa"), ("QA Automation Engineer", "qa"), ("Solutions Engineer", "solutions"),
])
def test_startup_only_families(title, family):
    brand = ev(job(title=title))
    r = next(x for x in brand.reasons if x["rule"] == "role")
    assert r["detail"] == "not a target role (startup-only family)" and brand.excluded
    for co in (FUNDED, WATCH):
        d = ev(job(title=title), company=co)
        assert "role" not in rules(d) and "flag:role:startup-only" in rules(d), d.reasons
        assert d.role_family == family
    # unknown / missing company tier -> excluded too
    assert "role" in rules(ev(job(title=title), company=None))


def test_devops_at_google_excluded():
    d = ev(job(title="DevOps Engineer"), company={"name": "Google", "tier": "brand"})
    assert "role" in rules(d) and d.excluded


def test_mts_is_senior_amts_kept():
    for t in ("Member of Technical Staff", "MTS", "Senior MTS", "Lead MTS", "MTS, Machine Learning"):
        d = ev(job(title=t))
        assert any(r["rule"] == "role" and r["detail"] == "MTS treated as senior; AMTS is kept"
                   for r in d.reasons), (t, d.reasons)
    assert "role" not in rules(ev(job(title="Associate Member of Technical Staff")))


def test_role_tiers_are_config_driven():
    assert "role" not in rules(ev(job(title="Frontend Engineer"), roles__families={"frontend": "secondary"}))
    assert "role" in rules(ev(job(title="Data Engineer"), roles__families={"data_engineering": "off"}))
    assert "role" not in rules(ev(job(title="DevOps Engineer"), roles__families={"devops": "primary"}))
    assert "role" in rules(ev(job(title="Data Analyst"), roles__allow_analyst_titles=False))


@pytest.mark.parametrize("title", ["Swedish Speaker", "SDEX Coordinator", "Fdesk Agent", "Dashboard Analyst",
                                   "Codes Inspector", "Gettysburg Office", "CV Specialist", "NLP"])
def test_acronyms_only_as_tokens(title):
    from jobcopilot.filters import engineering_signal, passes_role
    assert passes_role(title) is None, title
    if title not in ("CV Specialist",):
        assert engineering_signal(title) is False, title


def test_engineering_signal_helper_for_ingest():
    from jobcopilot.filters import engineering_signal
    for t in ("Member of Technical Staff", "ETL Developer", "Frontend Engineer", "Data Analyst", "FDE"):
        assert engineering_signal(t) is True, t          # stored as an auditable exclusion
    for t in ("Marketing AI Engineer", "Data Validator – AI/ML Systems", "Account Executive", "Business Analyst"):
        assert engineering_signal(t) is False, t


# --- E4: degree ---------------------------------------------------------------------
@pytest.mark.parametrize("title,text", [
    ("Software Engineer - AI/ML - PhD - Early Career - 2026", OK_TEXT),
    ("Research Scientist (PhD)", OK_TEXT),
    ("MSc Graduate Program - Data Science", OK_TEXT),
    ("Data Scientist", "Minimum qualifications\n- PhD in Computer Science, Statistics or a related field.\n"
                       "- 1+ years of experience in Python."),
    ("ML Engineer", "Requirements\n- Master's degree in Computer Science or related field.\n- Python"),
    ("Data Scientist", "A Ph.D. in a quantitative field is required for this role."),
])
def test_e4_required_masters_or_phd_excluded(title, text):
    d = ev(job(title=title, description_text=text))
    assert "degree" in rules(d), d.reasons
    assert_d8(d)


@pytest.mark.parametrize("text", [
    "Requirements\n- Bachelor's or Master's degree in Computer Science.",
    "Requirements\n- BS/MS/PhD in Computer Science or a related field.",
    "Requirements\n- B.Tech/BE in CS. Master's degree preferred.",
    "Requirements\n- PhD in ML, or 2+ years of industry experience.",
    "Requirements\n- Master's degree in CS or equivalent practical experience.",
    "Requirements\n- MS or PhD is a plus.",
    "Requirements\n- Ideally a Master's in Statistics.",
    "Nice to have\n- Master's degree in Computer Science.",
    "About us\nOur founders hold PhDs from Stanford.",
    "Requirements\n- Strong MS Excel skills and a degree in CS.",
    "Requirements\n- 1+ years of Python.",
])
def test_e4_degree_alternatives_and_soft_cues_eligible(text):
    d = ev(job(title="Data Scientist", description_text=text))
    assert "degree" not in rules(d), d.reasons


# --- E9: shared seniority regex -------------------------------------------------------
@pytest.mark.parametrize("title,senior", [
    ("Software Engineer - Leadership Development Program", False), ("Data Architecture Engineer", False),
    ("VP, Data Engineer", True), ("Tech Lead, Backend", True), ("Staff Engineer", True),
    ("Member of Technical Staff", False), ("Sr. Software Engineer", True), ("Head of AI", True),
    ("Principal ML Engineer", True), ("Engineering Manager", True), ("Software Engineer 2026", False),
])
def test_e9_seniority_word_boundaries_shared(title, senior):
    from jobcopilot.fit import _senior_title
    from jobcopilot.yoe import title_level
    assert is_seniority_high(title) is senior
    assert _senior_title(title) is senior
    assert (title_level(title) == "senior") is senior
