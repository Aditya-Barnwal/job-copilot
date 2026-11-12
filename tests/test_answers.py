"""Answers bank (Phase 3, workstream C): matcher coverage over Workday /
Greenhouse / Lever / Ashby wording, option matching, tier-aware CTC,
sponsorship by country, experience maths, statuses, open questions.

Offline: profile.example/answers.yaml (fake "Alex Example"); LLM always faked.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from jobcopilot import answers as A
from jobcopilot import facts as F
from jobcopilot import llm

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "profile.example"
TODAY = dt.date(2026, 11, 12)
JOB_IN = {"key": "k", "company": "Foo Corp", "title": "Backend Engineer", "country_bucket": "IN",
          "source": "greenhouse", "description_text": "Foo Corp builds Go services."}
JOB_US = {**JOB_IN, "country_bucket": "non-IN", "source": "lever"}


@pytest.fixture(scope="module")
def bank():
    return A.load(EXAMPLE / "answers.yaml")


def ask(bank, q, ft="text", options=None, job=JOB_IN, **kw):
    return A.answer_question(q, field_type=ft, options=options, job=job, answers=bank, today=TODAY, **kw)


# --------------------------------------------------------------------------- schema

def test_example_is_schema_v2_and_mirrors_real_keys():
    d = yaml.safe_load((EXAMPLE / "answers.yaml").read_text(encoding="utf-8"))
    assert d["schema_version"] == 2
    for block in ("personal", "current_address", "permanent_address", "education", "employment",
                  "compensation", "availability", "location_preferences", "work_authorization",
                  "screening_defaults", "voluntary_disclosures", "writing"):
        assert block in d
    ec = d["compensation"]["expected_ctc"]
    assert {"inr_floor_lpa", "inr_default_lpa_by_tier", "inr_free_text", "rule_when_range_posted",
            "usd_floor", "usd_default", "eur_floor", "eur_default"} <= set(ec)
    assert set(ec["inr_default_lpa_by_tier"]) == {"brand", "funded", "watchlist", "default"}
    real = ROOT / "profile" / "answers.yaml"
    if real.exists():                       # keys only — values are never read into the test
        def keys(x, p=""):
            if isinstance(x, dict):
                return {k2 for k, v in x.items() for k2 in keys(v, f"{p}.{k}" if p else k)} | {p}
            return {p}
        r = yaml.safe_load(real.read_text(encoding="utf-8"))
        missing = keys(r) - keys(d)
        assert not missing, f"profile.example lacks keys: {sorted(missing)}"


def test_statuses_from_comments(bank):
    assert bank.status_of("personal.first_name") == "approved"
    assert bank.status_of("location_preferences.work_modes") == "proposed"
    assert bank.status_of("compensation.expected_ctc.inr_default_lpa_by_tier.brand") == "approved"
    assert bank.status_of("education[0].cgpa") == "approved"
    assert bank.status_of("nonexistent.key") == "proposed"          # no status = not approved
    assert A.parse_statuses("a:   # Proposed\n  b: 1  # approved\n  c: 2\n") == {"a": "proposed", "a.b": "approved"}
    assert A.as_bank({"x": 1}).status_of("x") == "approved"          # caller-vouched dict


# --------------------------------------------------------------------------- matcher coverage

# (question, field_type, options, expected value, expected source suffix)
CASES = [
    # names (Greenhouse / Lever / Workday legalNameSection)
    ("First Name", "text", None, "Alex", "personal.first_name"),
    ("Legal First Name*", "text", None, "Alex", "personal.first_name"),
    ("Given name(s)", "text", None, "Alex", "personal.first_name"),
    ("Last Name (required)", "text", None, "Example", "personal.last_name"),
    ("Surname / Family name", "text", None, "Example", "personal.last_name"),
    ("Full name", "text", None, "Alex Example", "personal.full_name"),
    ("Name", "text", None, "Alex Example", "personal.full_name"),
    ("Middle Name", "text", None, "", "personal.middle_name"),
    ("Preferred First Name", "text", None, "Alex", "personal.first_name"),
    # contact
    ("Email", "text", None, "alex@example.com", "personal.email"),
    ("E-mail address", "text", None, "alex@example.com", "personal.email"),
    ("Phone", "text", None, "+1 555 0100", "personal.phone"),
    ("Mobile number", "text", None, "+1 555 0100", "personal.phone"),
    ("Phone number (without country code)", "text", None, "555 0100", "personal.phone_national"),
    ("Country phone code", "select", ["India (+91)", "United States (+1)", "UK (+44)"], "United States (+1)",
     "personal.phone_country_code"),
    ("LinkedIn Profile", "text", None, "https://example.com/linkedin/alex-example", "personal.links.linkedin"),
    ("Github URL", "text", None, "https://example.com/github/alex-example", "personal.links.github"),
    ("Website", "text", None, "https://alex.example.com/", "personal.links.portfolio"),
    ("Portfolio / personal website", "text", None, "https://alex.example.com/", "personal.links.portfolio"),
    # address (Workday addressSection)
    ("Address Line 1", "text", None, "12 Example Street", "current_address.line1"),
    ("Address Line 2", "text", None, "Old Town", "current_address.locality"),
    ("City", "text", None, "Springfield", "current_address.city"),
    ("State/Province", "text", None, "Example State", "current_address.state"),
    ("Postal Code", "text", None, "00000", "current_address.postcode"),
    ("PIN code", "text", None, "00000", "current_address.postcode"),
    ("Country of residence", "text", None, "Exampleland", "current_address.country"),
    ("Permanent address - City", "text", None, "Shelbyville", "permanent_address.city"),
    ("Permanent Address District", "text", None, "Sample District", "permanent_address.district"),
    ("Current Address", "textarea", None,
     "12 Example Street, Old Town, Springfield, Example State, 00000, Exampleland", "current_address"),
    # personal
    ("Date of Birth", "date", None, "2001-04-15", "personal.date_of_birth"),
    ("What is your age?", "number", None, 25, "personal.date_of_birth"),
    ("Nationality", "text", None, "Examplish", "personal.nationality"),
    ("Marital Status", "select", ["Single", "Married", "Divorced"], "Single", "personal.marital_status"),
    ("Languages known", "checkbox", ["English", "Hindi", "Tamil"], ["English", "Hindi"], "personal.languages_spoken"),
    ("Are you at least 18 years of age?", "radio", ["Yes", "No"], "Yes", "screening_defaults.over_18"),
    ("Are you 18 years or older?", "text", None, "Yes", "screening_defaults.over_18"),
    # education
    ("School", "text", None, "Example State University", "education[0].institution"),
    ("University / College name", "text", None, "Example State University", "education[0].institution"),
    ("Degree", "select", ["Bachelor's Degree", "Master's Degree", "Doctorate"], "Bachelor's Degree", "education[0].degree"),
    ("Highest level of education completed", "select", ["High School", "Bachelor's", "Master's", "PhD"],
     "Bachelor's", "education[0].degree"),
    ("Discipline", "text", None, "Computer Science", "education[0].field"),
    ("Field of Study / Major", "text", None, "Computer Science", "education[0].field"),
    ("CGPA", "number", None, 8.4, "education[0].cgpa"),
    ("GPA (out of)", "number", None, 10, "education[0].cgpa_scale"),
    ("Graduation year", "number", None, 2023, "education[0].end_date"),
    ("Year of passing", "text", None, "2023", "education[0].end_date"),
    ("Expected graduation date", "date", None, "2023-05-01", "education[0].end_date"),
    # employment
    ("Current Employer", "text", None, "Acme Widgets Inc.", "employment.current_employer"),
    ("Current company", "text", None, "Acme Widgets Inc.", "employment.current_employer"),
    ("Current Job Title", "text", None, "Software Engineer", "employment.current_title"),
    ("Designation", "text", None, "Software Engineer", "employment.current_title"),
    ("Are you currently employed?", "radio", ["Yes", "No"], "Yes", "employment.currently_employed"),
    ("When did you start at your current company?", "date", None, "2024-01-15", "employment.current_start_date"),
    ("Total years of experience", "number", None, 2, "employment.current_start_date"),
    ("How many years of professional experience do you have?", "number", None, 2, "employment.current_start_date"),
    ("Total work experience", "text", None, "2 years 9 months", "employment.current_start_date"),
    ("Total experience (in months)", "number", None, 33, "employment.current_start_date"),
    ("Years of experience", "select", ["0-1 years", "1-3 years", "3-5 years", "5+ years"], "1-3 years",
     "employment.current_start_date"),
    # compensation
    ("Current CTC (in LPA)", "number", None, 12.5, "compensation.current_ctc.total_lpa"),
    ("Current fixed salary", "text", None, "11 LPA", "compensation.current_ctc.fixed_lpa"),
    ("Current CTC (INR per annum)", "number", None, 1250000, "compensation.current_ctc.total_lpa"),
    ("Expected CTC", "text", None, "18 LPA", "compensation.expected_ctc.inr_default_lpa_by_tier.default"),
    ("Expected CTC (in lakhs)", "number", None, 18, "compensation.expected_ctc.inr_default_lpa_by_tier.default"),
    ("What are your salary expectations?", "textarea", None,
     "18-25 LPA depending on the role and the overall package; open to discussion.",
     "compensation.expected_ctc.inr_free_text"),
    # availability
    ("Notice Period", "text", None, "30 days", "availability.notice_period"),
    ("What is your notice period (in days)?", "number", None, 30, "availability.notice_period_days"),
    ("Notice period", "select", ["Immediate", "15 days or less", "1 month", "2 months", "3 months"], "1 month",
     "availability.notice_period_days"),
    ("How soon can you join?", "select", ["Immediately", "Within 30 days", "31-60 days", "More than 60 days"],
     "Within 30 days", "availability.notice_period_days"),
    ("Are you currently serving your notice period?", "radio", ["Yes", "No"], "No",
     "availability.currently_serving_notice"),
    ("Earliest start date", "date", None, "2027-01-04", "availability.earliest_start"),
    ("When can you start?", "text", None, "2027-01-04", "availability.earliest_start"),
    # location / relocation / work mode
    ("Are you willing to relocate?", "radio", ["Yes", "No"], "Yes", "location_preferences.willing_to_relocate"),
    ("Preferred work location", "text", None, "Springfield, Example City", "location_preferences.relocation_cities"),
    ("Current location", "text", None, "Springfield, Exampleland", "location_preferences.current_location"),
    ("Are you comfortable working in a hybrid model?", "radio", ["Yes", "No"], "Yes", "location_preferences.work_modes"),
    ("Are you open to working onsite 5 days a week?", "radio", ["Yes", "No"], "Yes", "location_preferences.work_modes"),
    ("Preferred work arrangement", "checkbox", ["Remote", "Hybrid", "On-site"], ["Hybrid", "Remote", "On-site"],
     "location_preferences.work_modes"),
    # screening (Greenhouse / Lever / Ashby custom questions)
    ("Are you willing to undergo a background check?", "radio", ["Yes", "No"], "Yes",
     "screening_defaults.background_check_ok"),
    ("Have you previously worked for Foo Corp?", "radio", ["Yes", "No"], "No", "screening_defaults.previously_worked_at"),
    ("Are you a current or former employee of this company?", "radio", ["Yes", "No"], "No",
     "screening_defaults.previously_worked_at"),
    ("Have you interviewed with us before?", "radio", ["Yes", "No"], "No", "screening_defaults.previously_interviewed_with"),
    ("Do you have any relatives working at the company?", "radio", ["Yes", "No"], "No",
     "screening_defaults.relatives_at_company"),
    ("Were you referred by an employee?", "radio", ["Yes", "No"], "No", "screening_defaults.referral_name"),
    ("Referrer name", "text", None, "", "screening_defaults.referral_name"),
    ("How did you hear about this job?", "select", ["LinkedIn", "Company Website", "Referral", "Other"],
     "Company Website", "screening_defaults.how_did_you_hear"),
    ("How did you hear about us?", "text", None, "Company careers page", "screening_defaults.how_did_you_hear"),
    # EEO / voluntary self-identification
    ("Gender", "select", ["Male", "Female", "Non-binary", "Decline to self-identify"], "Male",
     "voluntary_disclosures.gender"),
    ("What is your gender identity?", "select", ["Man", "Woman", "Non-binary", "I don't wish to answer"], "Man",
     "voluntary_disclosures.gender"),
    ("Race", "select", ["American Indian or Alaska Native", "Asian", "Black or African American", "White",
                        "Two or More Races", "Decline To Self Identify"], "Asian", "voluntary_disclosures.race_ethnicity"),
    ("Ethnicity", "select", ["East Asian", "South Asian", "White", "Prefer not to say"], "South Asian",
     "voluntary_disclosures.race_ethnicity"),
    ("Are you Hispanic/Latino?", "select", ["Yes", "No", "Decline to self-identify"], "No",
     "voluntary_disclosures.hispanic_latino"),
    ("Veteran Status", "select", ["I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"],
     "I am not a protected veteran", "voluntary_disclosures.veteran_status"),
    ("Disability Status", "select", ["Yes, I have a disability (or previously had a disability)",
                                     "No, I do not have a disability and have not had one in the past",
                                     "I do not want to answer"],
     "No, I do not have a disability and have not had one in the past", "voluntary_disclosures.disability"),
    ("Sexual orientation", "select", ["Heterosexual or straight", "Gay or lesbian", "Bisexual", "Prefer not to say"],
     "Heterosexual or straight", "voluntary_disclosures.sexual_orientation"),
]


@pytest.mark.parametrize("q, ft, options, expected, src", CASES, ids=[c[0][:40] for c in CASES])
def test_matcher(bank, q, ft, options, expected, src):
    a = ask(bank, q, ft, options)
    assert a.value == expected, (a.rule, a.note)
    assert a.source == f"answers.yaml:{src}", a.source
    assert a.confidence > 0.5 or expected == ""


def test_matcher_has_enough_phrasings():
    assert len(CASES) >= 60


# --------------------------------------------------------------------------- never invent

@pytest.mark.parametrize("q", ["What is your favourite colour?", "Years of experience with Kubernetes",
                               "Which programming languages do you know?", "10th percentage",
                               "Twitter handle", "I agree to the privacy policy",
                               "I certify that the information above is true and complete",
                               "Please upload your transcript"])
def test_unknown_or_consent_left_empty(bank, q):
    a = ask(bank, q, "text")
    assert a.value == "" and a.needs_user and a.confidence == 0


def test_missing_key_is_empty_not_invented(bank):
    data = {**bank.data, "voluntary_disclosures": {}}
    a = A.answer_question("Veteran status", answers=data, today=TODAY)
    assert a.value == "" and a.needs_user and a.source == "none"


def test_proposed_block_needs_user(bank):
    a = ask(bank, "Are you willing to relocate?", "radio", ["Yes", "No"])
    assert a.value == "Yes" and a.needs_user and a.status == "proposed"
    assert not ask(bank, "First name").needs_user


def test_no_matching_option_is_empty(bank):
    a = ask(bank, "Gender", "select", ["Option A", "Option B"])
    assert a.value == "" and a.needs_user


def test_marital_only_when_required(bank):
    a = ask(bank, "Marital status", "select", ["Single", "Married"], required=False)
    assert a.value == "" and not a.needs_user
    assert ask(bank, "Marital status", required=True).value == "Single"


# --------------------------------------------------------------------------- option matching

@pytest.mark.parametrize("value, options, expected", [
    ("South Asian", ["White", "Asian", "Black"], "Asian"),
    ("South Asian", ["South Asian", "Asian"], "South Asian"),
    ("Male", ["Female", "Male"], "Male"),
    ("Man", ["Female", "Male", "Other"], "Male"),
    ("Prefer not to say", ["Yes", "No", "I do not wish to answer"], "I do not wish to answer"),
    (True, ["No, I am not", "Yes, I am"], "Yes, I am"),
    (False, ["Yes", "No"], "No"),
    ("India", ["United States", "IN", "UK"], "IN"),
    ("B.Tech in Computer Science", ["Bachelor's Degree", "Master's Degree"], "Bachelor's Degree"),
    ("Company careers page", ["LinkedIn", "Careers site", "Other"], "Careers site"),
])
def test_match_option(value, options, expected):
    assert A.match_option(value, options)[0] == expected


def test_match_option_no_match():
    assert A.match_option("Female", ["Male", "Other"]) == (None, 0.0)


# --------------------------------------------------------------------------- CTC

def test_expected_ctc_tier_aware(bank):
    brand = ask(bank, "Expected CTC (LPA)", "number", company={"tier": "brand"})
    funded = ask(bank, "Expected CTC (LPA)", "number", company={"tier": "funded"})
    assert (brand.value, funded.value) == (25, 18)
    assert brand.source.endswith("inr_default_lpa_by_tier.brand")


def test_expected_ctc_posted_range_upper_middle(bank):
    job = {**JOB_IN, "comp_currency": "INR", "comp_min": 1_600_000, "comp_max": 2_400_000, "comp_interval": "1 YEAR"}
    a = ask(bank, "Expected CTC", "text", job=job)
    assert a.value == "22 LPA" and "upper-middle" in a.note


def test_expected_ctc_never_below_floor(bank):
    job = {**JOB_IN, "comp_currency": "INR", "comp_min": 600_000, "comp_max": 1_000_000}
    a = ask(bank, "Expected CTC (in lakhs)", "number", job=job)
    assert a.value == 15 and "floor" in a.note


def test_expected_ctc_monthly_range_and_usd(bank):
    job = {**JOB_IN, "comp_currency": "INR", "comp_min": 150_000, "comp_max": 250_000, "comp_interval": "1 MONTH"}
    assert ask(bank, "Expected CTC", job=job).value == "27 LPA"          # 18L..30L/yr → 27
    us = ask(bank, "Desired salary (USD)", "number", job=JOB_US)
    assert us.value == 50000
    us_range = ask(bank, "Salary expectations", "number",
                   job={**JOB_US, "comp_currency": "USD", "comp_min": 30000, "comp_max": 38000})
    assert us_range.value == 40000                                        # 36k < floor 40k


def test_expected_ctc_rupees_absolute(bank):
    assert ask(bank, "Expected annual CTC in INR", "number").value == 1_800_000


# --------------------------------------------------------------------------- sponsorship / authorisation

def test_sponsorship_by_country(bank):
    q = "Will you now or in the future require sponsorship for employment visa status?"
    assert ask(bank, q, "radio", ["Yes", "No"], job=JOB_US).value == "Yes"
    assert ask(bank, q, "radio", ["Yes", "No"], job=JOB_IN).value == "Yes"   # fake: not authorised in India
    data = {**bank.data, "work_authorization": {**bank.data["work_authorization"], "india": "Authorised (citizen)"}}
    assert A.answer_question(q, field_type="radio", options=["Yes", "No"], job=JOB_IN,
                             answers=data).value == "No"
    assert ask(bank, q, "radio", ["Yes", "No"], job={**JOB_IN, "country_bucket": "unknown"}).value == ""


def test_work_authorisation_by_country(bank):
    us = ask(bank, "Are you legally authorized to work in the United States?", "radio", ["Yes", "No"], job=None)
    assert us.value == "No" and us.source.endswith("work_authorization.other_countries")  # "needs sponsorship elsewhere"
    india = ask(bank, "Are you authorized to work in India?", "radio", ["Yes", "No"], job=JOB_US)
    assert india.value == "No" and india.source.endswith("work_authorization.india")
    text = ask(bank, "Work authorization status", "text", job=JOB_US)
    assert text.value.startswith("Authorised to work in Exampleland")


# --------------------------------------------------------------------------- experience

def test_experience_computation(bank):
    assert A.months_between(dt.date(2024, 1, 15), dt.date(2026, 11, 12)) == 33
    assert A.months_between(dt.date(2024, 1, 15), dt.date(2026, 11, 15)) == 34
    assert A.fmt_years_months(13) == "1 year 1 month" and A.fmt_years_months(5) == "5 months"
    a = A.answer_question("Years of experience", field_type="number", answers=bank,
                          today=dt.date(2025, 1, 14))
    assert a.value == 0                                                    # 11 months → rounds down


def test_how_heard_board_name(bank):
    job = {**JOB_IN, "source": "himalayas", "attribution_name": "Himalayas"}
    assert ask(bank, "How did you hear about us?", job=job).value == "Himalayas"
    sel = ask(bank, "How did you hear about this role?", "select", ["LinkedIn", "Job Board", "Other"], job=job)
    assert sel.value == "Job Board" and "isn't offered" in sel.note
    assert ask(bank, "How did you hear about us?", job=None).value == ""


def test_previously_worked_matches_company(bank):
    job = {**JOB_IN, "company": "Acme Widgets"}
    assert ask(bank, "Have you ever worked for Acme Widgets?", "radio", ["Yes", "No"], job=job).value == "Yes"


# --------------------------------------------------------------------------- open questions

def test_open_question_grounded_and_needs_user(monkeypatch, bank):
    facts = F.load(EXAMPLE / "resume_facts.yaml")
    calls = []

    def fake(system, user, **_kw):
        calls.append(user)
        return {"paragraphs": [{"text": "At Acme Widgets Inc., I built a Go order service handling 200+ "
                                        "requests per second behind a REST API.",
                                "fact_ids": ["exp.acme.b1"]}]}, {}
    monkeypatch.setattr(llm, "complete_json", fake)
    monkeypatch.setattr("jobcopilot.writing.common.company_record",
                        lambda job, company=None: {"name": job.get("company")})
    a = A.answer_question("Why do you want to join Foo Corp?", field_type="textarea", job=JOB_IN,
                          answers=bank, facts=facts)
    assert a.source == "generated" and a.needs_user and a.value.startswith("At Acme")
    assert a.grounding["clean"] and "exp.acme.b1" in a.fact_ids
    assert "<untrusted_question>" in calls[0]


def test_open_question_fabrication_flagged(monkeypatch, bank):
    facts = F.load(EXAMPLE / "resume_facts.yaml")
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: (
        {"paragraphs": [{"text": "I scaled Kafka to 9 million events at Google.", "fact_ids": []}]}, {}))
    a = A.answer_question("Tell us about a project you are proud of", field_type="textarea",
                          answers=bank, facts=facts)
    assert a.needs_user and a.confidence == 0.2 and "NEEDS REVIEW" in a.note


def test_open_question_refuses_without_facts(monkeypatch, bank):
    facts = F.load(EXAMPLE / "resume_facts.yaml")
    for f in facts.facts():
        f.status = "unconfirmed"
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: pytest.fail("LLM called"))
    a = A.answer_question("Why this company?", field_type="textarea", answers=bank, facts=facts)
    assert a.value == "" and a.needs_user and "confirm your facts first" in a.note


# --------------------------------------------------------------------------- review + CLI

def test_review_report(bank):
    rep = A.review_report(bank, today=TODAY)
    paths = {r["path"]: r for r in rep["rows"]}
    assert paths["personal.first_name"]["status"] == "approved"
    assert paths["location_preferences.work_modes"]["status"] == "proposed"
    assert paths["education[0].cgpa"]["value"] == 8.4
    assert paths["school_marks"]["empty"]
    assert paths["total experience"]["value"] == "2 years 9 months" and paths["total experience"]["derived"]
    assert paths["expected CTC (brand tier, no posted range)"]["value"] == "25 LPA"
    assert rep["counts"]["proposed"] >= 4 and rep["schema_version"] == 2


def test_cli_review_and_ask(capsys):
    path = str(EXAMPLE / "answers.yaml")
    assert A.main(["review", path]) == 0
    out = capsys.readouterr().out
    assert "[APPROVED] personal.first_name = 'Alex'" in out and "[PROPOSED]" in out
    assert A.main(["ask", "Notice period", "--options", "Immediate|1 month", "--type", "select",
                   "--path", path]) == 0
    assert "value: '1 month'" in capsys.readouterr().out
    assert A.main(["review", str(EXAMPLE / "missing.yaml")]) == 1


# --------------------------------------------------------------------------- Phase 3 review fixes

def with_data(bank, **blocks):
    """A caller-vouched (approved) copy of the example with some blocks patched."""
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in bank.data.items()}
    for path, value in blocks.items():
        cur = data
        *parents, leaf = path.split("__")
        for p in parents:
            cur[p] = dict(cur.get(p) or {})
            cur = cur[p]
        cur[leaf] = value
    return data


def ask_d(data, q, ft="text", options=None, job=JOB_IN, **kw):
    return A.answer_question(q, field_type=ft, options=options, job=job, answers=data, today=TODAY, **kw)


def assert_asks_user(a):
    assert a.value == "" and a.needs_user, (a.value, a.rule, a.note)


@pytest.mark.parametrize("q, options", [
    ("I authorize Acme to conduct a background check", None),
    ("By checking this box, I authorize a background verification", None),
    ("I hereby declare that I am legally authorized to work in India", None),
    ("I hereby declare that I am legally authorized to work in India", ["Yes"]),
    ("Background check", ["I authorize Acme to conduct a background check"]),
    ("I am legally authorized to work in the United States", None),
    ("I accept the terms and conditions", None),
    ("I confirm that the information provided is accurate", None),
    ("Data privacy notice", ["I have read the privacy policy"]),
])
def test_r3_1_attestation_checkbox_never_ticked(bank, q, options):
    a = ask(bank, q, "checkbox", options)
    assert_asks_user(a)
    assert a.rule == "consent"


@pytest.mark.parametrize("q", ["Signature", "Type your full name to sign", "Please sign below",
                               "I authorize Acme to conduct a background check"])
def test_r3_1_signature_free_text_never_answered(bank, q):
    a = ask(bank, q, "text")
    assert_asks_user(a)
    assert a.rule == "consent"


def test_r3_1_choice_checkboxes_still_answered(bank):
    assert ask(bank, "Languages known", "checkbox", ["English", "Hindi", "Tamil"]).value == ["English", "Hindi"]
    a = ask(bank, "Preferred work arrangement", "checkbox", ["Remote", "Hybrid", "On-site"])
    assert set(a.value) == {"Remote", "Hybrid", "On-site"}


WA_OPTS = ["I will require sponsorship to work in India",
           "I am authorized to work in India without sponsorship"]


def test_r3_2_work_auth_options_by_meaning(bank):
    citizen = with_data(bank, work_authorization__india="Authorised (citizen)")
    a = ask_d(citizen, "Are you authorized to work in India?", "radio", WA_OPTS)
    assert a.value == "I am authorized to work in India without sponsorship" and not a.needs_user
    a = ask(bank, "Are you authorized to work in India?", "radio", WA_OPTS)      # fake: not authorised
    assert a.value == "I will require sponsorship to work in India"
    assert A._polarity("I am authorized to work in India without sponsorship") == "yes"


def test_r3_2_sponsorship_options_by_meaning(bank):
    q = "Will you require visa sponsorship?"
    opts = ["I do not require sponsorship", "I will require sponsorship"]
    assert ask(bank, q, "radio", opts, job=JOB_US).value == "I will require sponsorship"
    citizen = with_data(bank, work_authorization__india="Authorised (citizen)")
    assert ask_d(citizen, q, "radio", list(reversed(opts))).value == "I do not require sponsorship"
    assert ask_d(citizen, q, "radio", WA_OPTS).value == "I am authorized to work in India without sponsorship"


def test_r3_2_two_affirmative_options_ask_the_user(bank):
    citizen = with_data(bank, work_authorization__india="Authorised (citizen)")
    a = ask_d(citizen, "Are you authorized to work in India?", "radio",
              ["I am authorized to work in India", "I am eligible to work in India"])
    assert a.needs_user
    a = ask_d(citizen, "Are you authorized to work in India?", "radio",
              ["I am authorized but will require sponsorship later", "Other"])
    assert_asks_user(a)


COUNTRIES = ["Bhutan", "British Indian Ocean Territory", "India", "Indonesia", "United Kingdom",
             "United States", "United States Minor Outlying Islands"]


def test_r3_3_nationality_demonym_maps_to_country(bank):
    data = with_data(bank, personal__nationality="Indian", personal__citizenship="Indian")
    a = ask_d(data, "Nationality", "select", COUNTRIES)
    assert a.value == "India" and not a.needs_user
    a = ask_d(data, "Nationality", "select", [c for c in COUNTRIES if c != "India"])
    assert a.needs_user and a.value != "India"           # containment only: never autofilled
    assert ask_d(data, "Nationality", "select", ["American", "British", "Indian"]).value == "Indian"
    assert ask_d(data, "Nationality", "select", ["Asian Indian", "India"]).value == "India"
    us = with_data(bank, personal__nationality="American")
    assert ask_d(us, "Nationality", "select", COUNTRIES).value == "United States"
    res = with_data(bank, current_address__country="India")
    a = ask_d(res, "Country of residence", "select", COUNTRIES)
    assert a.value == "India" and not a.needs_user
    # race fallbacks still apply to race / ethnicity
    assert ask(bank, "Ethnicity", "select", ["White", "Asian", "Black"]).value == "Asian"


EXP_OPTS = ["0-6 months", "6 months - 1 year", "1-2 years", "2-5 years", "5+ years"]


def test_r3_4_experience_ranges_in_years(bank):
    data = with_data(bank, employment__current_start_date="2025-07-17")     # 15 months on TODAY
    assert ask_d(data, "Years of experience", "select", EXP_OPTS).value == "1-2 years"
    assert ask_d(data, "Total experience", "select",
                 ["Less than 1 year", "1-3 years", "3+ years"]).value == "1-3 years"
    assert ask_d(data, "Total experience", "select",
                 ["0-12 months", "12-24 months", "24+ months"]).value == "12-24 months"
    assert ask_d(data, "Years of experience", "number").value == 1
    assert ask_d(data, "Total experience (in months)", "number").value == 15
    a = ask_d(data, "Years of experience", "select", ["0-1 years", "2-3 years", "3+ years"])
    assert_asks_user(a)                                      # 1.25 years: no range holds it
    assert "no option covers" in a.note


def test_r3_4_experience_shared_edges(bank):
    data = with_data(bank, employment__current_start_date="2024-11-12")     # exactly 2 years
    assert ask_d(data, "Years of experience", "select", ["0-2 years", "2-4 years", "4+ years"]).value == "2-4 years"
    assert ask_d(data, "Years of experience", "select", ["Less than 2 years", "More than 2 years"]).value == ""


def test_r3_5_currency_never_falls_back_to_usd_default(bank):
    gbp_job = {**JOB_US, "comp_currency": "GBP", "comp_min": 50000, "comp_max": 70000}
    assert_asks_user(ask(bank, "Expected salary", "number", job=gbp_job))
    assert_asks_user(ask(bank, "Expected salary (£)", "number", job=JOB_US))
    assert_asks_user(ask(bank, "Desired salary in CAD", "number", job=JOB_US))
    assert_asks_user(ask(bank, "Expected salary", "number", job={**JOB_US, "country_bucket": "non-IN"}))


def test_r3_5_ctc_word_follows_job_currency(bank):
    usd = {**JOB_US, "comp_currency": "USD"}
    a = ask(bank, "Expected CTC", "number", job=usd)
    assert a.value == 50000 and "usd" in a.source
    remote = {**JOB_IN, "country_bucket": "unknown", "workplace_type": "remote"}
    assert_asks_user(ask(bank, "Expected CTC", "number", job=remote))
    assert_asks_user(ask(bank, "Expected CTC", "number", job={**remote, "remote": True}))
    a = ask(bank, "Expected CTC", "text", job={**remote, "comp_currency": "EUR"})
    assert a.value == "EUR 45,000 per year"
    assert ask(bank, "Expected CTC (in lakhs)", "number", job=usd).value == 18      # the question says LPA


@pytest.mark.parametrize("q, cur", [("Expected salary (£)", "GBP"), ("Expected salary in GBP", "GBP"),
                                    ("Desired salary (C$)", "CAD"), ("Desired salary (S$)", "SGD"),
                                    ("Desired salary (A$)", "AUD"), ("Desired salary (€)", "EUR"),
                                    ("Desired salary (US$)", "USD"), ("Desired salary ($)", "USD"),
                                    ("Expected salary (₹ per annum)", "INR")])
def test_r3_5_currency_detected_in_question(bank, q, cur):
    ec = dict(bank.data["compensation"]["expected_ctc"])
    for k, v in {"gbp": 41000, "cad": 72000, "sgd": 83000, "aud": 94000}.items():
        ec[f"{k}_default"] = v
    data = with_data(bank, compensation__expected_ctc=ec)
    a = ask_d(data, q, "text", job=JOB_US)
    if cur == "INR":
        assert a.value.endswith("LPA")
    else:
        assert a.value.startswith(f"{cur} "), (a.value, a.note)


VET_OPTS = ["Yes", "No, I am not a veteran", "Not applicable"]


def test_r3_6_not_applicable_is_its_own_class(bank):
    assert ask(bank, "Veteran status", "select", VET_OPTS).value == "No, I am not a veteran"
    data = with_data(bank, voluntary_disclosures__veteran_status="No")
    assert ask_d(data, "Veteran status", "select", list(reversed(VET_OPTS))).value == "No, I am not a veteran"
    for na in ("Not applicable", "N/A", "None of the above"):
        assert A._polarity(na) == "na"
    assert A._polarity("Prefer not to say") == "decline"
    assert A.match_option(False, ["Yes", "Not applicable", "No"])[0] == "No"
    assert A.match_option(False, ["Not applicable", "Yes"]) == (None, 0.0)
    o, score = A.match_option(False, ["No, never", "No, not currently", "Yes"])
    assert score < 0.6                                       # two different "no"s: ask


def test_r3_11_citizenship_checks_each_country_on_its_own_tokens(bank):
    us = with_data(bank, personal__citizenship="United States")
    assert ask_d(us, "Are you a UK citizen?", "radio", ["Yes", "No"], job=None).value == "No"
    assert ask_d(us, "Are you a US citizen?", "radio", ["Yes", "No"], job=None).value == "Yes"
    assert ask_d(us, "Are you a citizen of the United Kingdom?", "radio", ["Yes", "No"], job=None).value == "No"
    uk = with_data(bank, personal__citizenship="British")
    assert ask_d(uk, "Are you a UK citizen?", "radio", ["Yes", "No"], job=None).value == "Yes"
    assert ask_d(uk, "Are you a U.S. citizen?", "radio", ["Yes", "No"], job=None).value == "No"
    ind = with_data(bank, personal__citizenship="Indian")
    assert ask_d(ind, "Are you an Indian citizen?", "radio", ["Yes", "No"]).value == "Yes"
