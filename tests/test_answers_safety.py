"""Safety of the answer matcher: when a keyword rule can't be sure what a yes/no
or legal question means, it leaves the field for the user (empty + needs_user).
Uses an inline fake bank only (never profile/answers.yaml)."""
from __future__ import annotations

import datetime as dt

import pytest

from jobcopilot import answers as A

TODAY = dt.date(2026, 11, 12)
JOB_IN = {"key": "k", "company": "Foo Corp", "title": "Backend Engineer", "country_bucket": "IN",
          "source": "greenhouse"}
JOB_US = {**JOB_IN, "country_bucket": "non-IN", "source": "lever"}

# A user who is an Indian citizen and NEEDS sponsorship anywhere else.
BANK = A.as_bank({
    "personal": {"full_name": "Test Person", "first_name": "Test", "last_name": "Person"},
    "work_authorization": {"india": "Indian citizen", "other_countries": "Requires visa sponsorship",
                           "requires_sponsorship_outside_india": True},
    "location_preferences": {"willing_to_relocate": True, "current_location": "Springfield",
                             "work_modes": ["hybrid", "remote"]},
    "screening_defaults": {"background_check_ok": True, "over_18": True, "relatives_at_company": False},
    "availability": {"notice_period": "30 days", "notice_period_days": 30, "currently_serving_notice": False},
    "employment": {"current_employer": "Acme Labs", "currently_employed": True, "current_title": "Engineer"},
    "compensation": {"current_ctc": {"total_lpa": 12.5, "fixed_lpa": 11},
                     "expected_ctc": {"inr_floor_lpa": 15, "inr_default_lpa_by_tier": {"default": 18}}},
    "education": [{"institution": "Example University"}],
})
YN = ["Yes", "No"]


def ask(q, ft="text", options=None, job=JOB_IN):
    return A.answer_question(q, field_type=ft, options=options, job=job, answers=BANK, facts=[], today=TODAY)


def typed(a) -> bool:
    """What autofill would type: needs_user False and confidence >= 0.8."""
    return not a.needs_user and a.confidence >= 0.8 and a.value not in ("", None, [])


def assert_left_for_user(a, q=""):
    assert a.value == "" and a.needs_user and not typed(a), (q, a.value, a.rule, a.note)


# --------------------------------------------------------------------------- 1. negation

NEGATED = [
    ("Are you able to work in the US without visa sponsorship?", JOB_US),
    ("Can you work for us without requiring sponsorship now or in the future?", JOB_US),
    ("Will you be able to start without needing a work visa?", JOB_US),
    ("Do you not require visa sponsorship?", JOB_US),
    ("You don't need sponsorship to work in the US, correct?", JOB_US),
    ("Will you no longer require visa sponsorship after joining?", JOB_US),
    ("Are you NOT authorized to work in India?", JOB_IN),
    ("Are you unable to work legally in India?", JOB_IN),
    ("Are you legally authorized to work in India other than through a visa?", JOB_IN),
    ("Would you be unwilling to relocate?", JOB_IN),
    ("Are you not willing to relocate?", JOB_IN),
    ("Would you never relocate?", JOB_IN),
    ("Is it true that you are not willing to undergo a background check?", JOB_IN),
    ("Are you unwilling to undergo a background check?", JOB_IN),
    ("Can't you undergo a background check?", JOB_IN),
    ("Are you under 18 years of age?", JOB_IN),
    ("Are you less than 18 years old?", JOB_IN),
    ("Are you a minor (under the age of 18)?", JOB_IN),
    ("Are you currently not serving your notice period?", JOB_IN),
    ("Are you not currently employed?", JOB_IN),
    ("Do you have no relatives working at the company?", JOB_IN),
    ("Are you comfortable working in a hybrid model except Fridays?", JOB_IN),
]


@pytest.mark.parametrize("q, job", NEGATED, ids=[q[:50] for q, _ in NEGATED])
@pytest.mark.parametrize("ft, options", [("radio", YN), ("select", YN), ("text", None)])
def test_negated_yes_no_is_left_for_user(q, job, ft, options):
    assert_left_for_user(ask(q, ft, options, job), q)


def test_sponsorship_negation_never_types_yes_for_a_user_who_needs_it():
    a = ask("Are you able to work in the US without visa sponsorship?", "radio", YN, JOB_US)
    assert a.value != "Yes"
    assert_left_for_user(a)


POSITIVE = [
    ("Will you now or in the future require visa sponsorship?", JOB_US, "Yes", "sponsorship"),
    ("Do you require visa sponsorship to work in the US?", JOB_US, "Yes", "sponsorship"),
    ("Do you require sponsorship? (Yes/No)", JOB_US, "Yes", "sponsorship"),
    ("Will you require visa sponsorship to work in India?", JOB_IN, "No", "sponsorship"),
    ("Are you legally authorized to work in India?", JOB_IN, "Yes", "work_auth"),
    ("Are you willing to relocate?", JOB_IN, "Yes", "relocate"),
    ("Are you willing to undergo a background check?", JOB_IN, "Yes", "background"),
    ("Are you at least 18 years of age?", JOB_IN, "Yes", "over_18"),
    ("Are you over the age of 18?", JOB_IN, "Yes", "over_18"),
    ("Are you 18 years or older?", JOB_IN, "Yes", "over_18"),
    ("Are you currently serving your notice period?", JOB_IN, "No", "notice"),
    ("Are you currently employed?", JOB_IN, "Yes", "currently_employed"),
    ("Do you have any relatives working at the company?", JOB_IN, "No", "relatives"),
    ("Are you comfortable working in a hybrid model?", JOB_IN, "Yes", "work_mode"),
]


@pytest.mark.parametrize("q, job, expected, rule", POSITIVE, ids=[p[0][:50] for p in POSITIVE])
@pytest.mark.parametrize("ft, options", [("radio", YN), ("select", YN), ("text", None)])
def test_plain_positive_yes_no_still_answered(q, job, expected, rule, ft, options):
    a = ask(q, ft, options, job)
    assert a.value == expected and typed(a), (a.value, a.rule, a.note)
    assert a.rule == rule


# --------------------------------------------------------------------------- 2. consent / two subjects

CONSENT = [
    "Do you authorize us to conduct a background check?",
    "Do you authorise Foo Corp to run a background verification?",
    "Do you consent to a background check?",
    "Do you agree to a background check being conducted?",
    "Do you acknowledge that a background check will be conducted?",
    "Do you certify that you are legally authorized to work in India?",
    "Can you attest that you do not require sponsorship?",
    "Do you declare that you are willing to relocate?",
    "Do you give us permission to contact your current employer?",
    "Do you give your authorization for a background check?",
    "Are you willing to undergo a drug test?",
    "Are you willing to undergo drug screening?",
    "Are you willing to undergo a drug test and background check?",
    "Do you agree to our Terms?",
]


@pytest.mark.parametrize("q", CONSENT, ids=[q[:50] for q in CONSENT])
@pytest.mark.parametrize("ft, options", [("radio", YN), ("select", YN), ("checkbox", ["Yes"]),
                                         ("text", None)])
def test_consent_and_authorisation_never_answered(q, ft, options):
    a = ask(q, ft, options)
    assert_left_for_user(a, q)
    assert a.rule == "consent"


TWO_SUBJECTS = [
    ("Are you willing to undergo a background check and reference check?", JOB_IN),
    ("Are you willing to relocate and work from our office five days a week?", JOB_IN),
    ("Are you willing to relocate & travel up to 50%?", JOB_IN),
    ("Are you legally authorized to work in India and Singapore?", JOB_IN),
    ("Are you okay with a background verification which includes contacting your current employer?", JOB_IN),
    ("Are you willing to undergo a background check as well as a credit check?", JOB_IN),
]


@pytest.mark.parametrize("q, job", TWO_SUBJECTS, ids=[q[:50] for q, _ in TWO_SUBJECTS])
@pytest.mark.parametrize("ft, options", [("radio", YN), ("select", YN)])
def test_two_yes_no_subjects_left_for_user(q, job, ft, options):
    assert_left_for_user(ask(q, ft, options, job), q)


def test_two_questions_in_one_label_left_for_user():
    assert_left_for_user(ask("How much notice do you need to give? Also please share your current CTC"))
    assert_left_for_user(ask("Are you willing to relocate? Which city do you prefer?", "radio", YN))


def test_work_authorization_wording_is_not_consent():
    assert A.match_rule("Do you have permission to work in India?") == "work_auth"
    assert A.match_rule("Work authorization status") == "work_auth"


# --------------------------------------------------------------------------- 3. salary

@pytest.mark.parametrize("q, ft", [("What is your current salary expectation?", "text"),
                                   ("Current salary expectations (INR)", "number"),
                                   ("Current salary expectations (in LPA)", "number"),
                                   ("Desired salary given your current package", "text"),
                                   ("What are you looking for in terms of salary?", "text")])
def test_expectation_wins_over_current(q, ft):
    a = ask(q, ft)
    assert a.rule == "expected_ctc", (a.rule, a.note)
    assert "current_ctc" not in a.source
    if typed(a):
        assert a.value in ("18 LPA", 18, 1800000)


def test_current_salary_expectation_types_the_expectation():
    a = ask("What is your current salary expectation?")
    assert a.value == "18 LPA" and typed(a)


@pytest.mark.parametrize("q", ["Your expected salary vs current salary",
                               "Current CTC and expected CTC",
                               "CCTC / ECTC",
                               "What is your current CTC and what are you expecting?"])
def test_current_and_expected_together_is_ambiguous(q):
    assert_left_for_user(ask(q), q)
    assert_left_for_user(ask(q, "number"), q)


@pytest.mark.parametrize("q, ft, expected", [("Current CTC (in LPA)", "number", 12.5),
                                             ("Current CTC", "text", "12.5 LPA"),
                                             ("Expected CTC", "text", "18 LPA")])
def test_plain_salary_questions_still_work(q, ft, expected):
    a = ask(q, ft)
    assert a.value == expected and typed(a)


# --------------------------------------------------------------------------- 3b. yes/no vs text rules

YES_NO_ABOUT_A_VALUE = [
    "Are you subject to a non-compete with your current employer?",
    "Is your current location within 50 miles of our office?",
    "Will you be able to provide references from your current employer?",
    "Do you have a current employer?",
    "Can you start immediately?",
    "Is your notice period negotiable?",
    "Are you okay with the salary range of 10-12 LPA?",
    "Did you graduate from Example University?",
    "Is your current job title Engineer?",
]


@pytest.mark.parametrize("q", YES_NO_ABOUT_A_VALUE, ids=[q[:50] for q in YES_NO_ABOUT_A_VALUE])
@pytest.mark.parametrize("ft, options", [("text", None), ("radio", YN)])
def test_yes_no_question_never_gets_a_text_value(q, ft, options):
    a = ask(q, ft, options)
    assert_left_for_user(a, q)
    assert a.value not in ("Acme Labs", "Springfield")


@pytest.mark.parametrize("q, expected", [("Current employer", "Acme Labs"),
                                         ("Current location", "Springfield"),
                                         ("Who is your current or previous employer?", "Acme Labs"),
                                         ("Can you share your current CTC?", "12.5 LPA"),
                                         ("Could you please tell us your notice period?", "30 days")])
def test_value_questions_still_answered(q, expected):
    a = ask(q)
    assert a.value == expected and typed(a), (a.value, a.rule, a.note)


# --------------------------------------------------------------------------- last session's fixes

def test_name_of_something_is_not_the_users_name():
    for q in ("What is the name of your university?", "Name of your manager", "Team name"):
        assert ask(q).value != "Test Person", q
    for q in ("Full name", "Name", "Your name"):
        assert ask(q).value == "Test Person", q
