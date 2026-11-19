"""Numbers typed into forms are in the unit the form asks for (docs/DESIGN.md §11-12).

A real Keka form got "210" for "Experience (in years)" (the digits of "2 years 10
months"), "2027" for "Available To Join (in days)" (a year from the start date)
and "12.5" for an INR / Annual salary box (LPA). These tests pin the fix:

  * answers.answer_question gives one plain number in the asked unit, or leaves
    the field for the person when the unit can't be told;
  * answers.implausible_number, the check SpecAdapter / GenericAdapter make at
    plan time and SafePage.fill makes right before typing;
  * the Keka fixtures (sanitized real forms, served on 127.0.0.1) end with
    2 / 10 / 30 / 1250000 / 1800000, or the salaries empty when no period shows.

Only the fake profile.example answers are used; nothing touches the network."""
from __future__ import annotations

import datetime as dt

import pytest

from jobcopilot import answers as A
from jobcopilot.answers import Answer
from jobcopilot.autofill import service
from jobcopilot.autofill.adapters.base import PlanContext
from jobcopilot.autofill.adapters.keka import KekaAdapter
from jobcopilot.autofill.guard import ImplausibleValue

from autofill_helpers import FormServer

TODAY = dt.date(2026, 11, 19)          # profile.example: current_start_date 2024-01-15 -> 2y 10m
JOB_IN = {"key": "j", "company": "Example", "country_bucket": "IN"}
MONTHS_0_11 = [str(i) for i in range(12)]


@pytest.fixture(scope="module")
def bank():
    return A.load(A.example_answers_path())


def ask(bank, q, ft="text", options=None, hint="", job=None, digits_only=False):
    return A.answer_question(q, field_type=ft, options=options, answers=bank, today=TODAY,
                             unit_hint=hint, job=job, digits_only=digits_only, allow_open=False)


def typed(a: Answer):
    assert not a.needs_user and a.value not in ("", None), (a.value, a.note)
    return a.value


def left(a: Answer):
    assert a.value == "" and a.needs_user, (a.value, a.note)
    return a.note


# --------------------------------------------------------------------------- unit words

def test_hint_and_label_units():
    assert A.hint_units("years") == {"years"}
    assert A.hint_units("inr, annual") == {"INR", "annual"}
    assert A.hint_units("inr, per month") == {"INR", "monthly"}
    assert A.label_units("Experience (in years)") == {"years"}
    assert A.label_units("Available To Join (in days)") == {"days"}
    assert A.label_units("Notice period (days)") == {"days"}
    assert A.label_units("How many years of work experience do you have?") == {"years"}
    assert "lpa" in A.label_units("Current CTC (in lakhs per annum)")
    assert A.label_units("Graduation year") == set()               # a year, not a count
    assert A.label_units("When can you start (month)?") == set()   # singular: not a count unit


def test_duration_months():
    assert A.duration_months("2 years 10 months") == 34
    assert A.duration_months("1 year 9 months") == 21
    assert A.duration_months("18 months") == 18
    assert A.duration_months("3") == 36
    for bad in ("2+ years", "1-2 years", "about 3 years", ""):
        assert A.duration_months(bad) is None


# --------------------------------------------------------------------------- experience

@pytest.mark.parametrize("q, ft, hint", [
    ("Experience (in years)", "text", "years"),      # Keka: text box + "Years" suffix
    ("Experience (in years)", "text", ""),           # the label alone says years
    ("Years of experience", "text", "years"),        # ClickPost: spec question + suffix
    ("Total years of experience", "number", ""),
    ("How many years of work experience do you have?", "text", ""),
])
def test_experience_whole_years(bank, q, ft, hint):
    a = ask(bank, q, ft, hint=hint)
    assert typed(a) == 2                              # 2y 10m: whole years, rounded down
    assert "rounded down" in a.note


def test_experience_months(bank):
    assert typed(ask(bank, "Total experience (in months)", "number")) == 34
    # Keka's Months picker beside the Years box: the months beyond the full years
    assert typed(ask(bank, "Months of experience (beyond full years)", "select", MONTHS_0_11,
                     hint="months")) == "10"


def test_experience_whole_year_ladder(bank):
    opts = ["Fresher", "Less than a year", "1 year", "2 years", "3 years", "4 years", "5 years or more"]
    assert typed(ask(bank, "How many years of work experience do you have?", "select", opts)) == "2 years"
    assert A._experience_option(0, opts)[0] == "Fresher"
    assert A._experience_option(0.5, opts)[0] == "Less than a year"
    assert A._experience_option(7, opts)[0] == "5 years or more"
    assert A._experience_option(2.83, ["1 year", "2 years", "5 years"])[0] is None   # not a ladder


@pytest.mark.parametrize("q, ft, opts, hint", [
    ("Total experience", "number", None, ""),                         # no unit at all
    ("Experience (in years)", "text", None, "months"),               # label and suffix disagree
    ("Total experience (years and months)", "number", None, ""),     # two units, one box
    ("Experience (in years)", "select", MONTHS_0_11, "months"),
])
def test_experience_unit_unknown_is_left(bank, q, ft, opts, hint):
    left(ask(bank, q, ft, opts, hint=hint))


def test_experience_text_box_keeps_words(bank):
    assert typed(ask(bank, "Total Experience")) == "2 years 10 months"


# --------------------------------------------------------------------------- notice / available to join

@pytest.mark.parametrize("q, ft, hint, want", [
    ("Available To Join (in days)", "number", "", 30),
    ("Available To Join (in days)", "text", "", 30),
    ("What is your notice period (in days)?", "number", "", 30),
    ("Notice period", "text", "days", 30),
    ("Notice period (in months)", "number", "", 1),
])
def test_notice_in_the_asked_unit(bank, q, ft, hint, want):
    a = ask(bank, q, ft, hint=hint)
    assert typed(a) == want and a.source.endswith("notice_period_days")


@pytest.mark.parametrize("q, ft, hint", [
    ("Available to join", "number", ""),          # no unit: never days by default
    ("Notice period", "number", ""),
    ("Earliest start date", "number", ""),        # never a date (or its year) in a count box
    ("Notice period", "text", "weeks"),           # 30 days isn't a whole number of weeks
])
def test_notice_unit_unknown_is_left(bank, q, ft, hint):
    a = ask(bank, q, ft, hint=hint)
    left(a)
    assert "2027" not in str(a.value)


def test_available_to_join_select_uses_notice(bank):
    a = ask(bank, "Available to join", "select", ["Immediately", "Within 15 days", "Within 30 days", "60 days"])
    assert typed(a) == "Within 30 days"


def test_earliest_start_date_still_a_date(bank):
    assert typed(ask(bank, "Earliest start date", "date")) == "2027-01-04"


# --------------------------------------------------------------------------- salary

@pytest.mark.parametrize("q, ft, hint, want", [
    ("Current Salary", "text", "inr, annual", 1250000),        # Attero: INR picker + Annual box
    ("Current Salary", "text", "inr, per month", 104167),
    ("Current CTC (INR per annum)", "number", "", 1250000),
    ("Current CTC (INR)", "number", "", 1250000),              # CTC is annual
    ("Current CTC (in LPA)", "number", "", 12.5),
    ("What's your current salary? (in lakhs per annum)", "text", "", 12.5),
    ("Expected Salary", "text", "inr, annual", 1800000),
    ("Expected CTC (in lakhs)", "number", "", 18),
    ("Please share your current annual CTC (INR)", "text", "", 1250000),      # Ashby: a text box
    ("Please share your expected annual CTC (INR)", "text", "", 1800000),
])
def test_salary_in_the_asked_unit(bank, q, ft, hint, want):
    assert typed(ask(bank, q, ft, hint=hint, job=JOB_IN)) == want


@pytest.mark.parametrize("q, ft, hint", [
    ("Current Salary", "text", "inr"),          # ClickPost: INR but no period shown
    ("Expected Salary", "text", "inr"),
    ("Current CTC", "number", ""),              # lakhs or rupees? can't tell
    ("Expected CTC", "number", ""),
    ("Current salary", "number", "usd, annual"),
    ("Current salary", "number", "inr, per hour"),
])
def test_salary_unit_unknown_is_left(bank, q, ft, hint):
    left(ask(bank, q, ft, hint=hint, job=JOB_IN))


def test_salary_text_box_keeps_lpa(bank):
    assert typed(ask(bank, "Current CTC", job=JOB_IN)) == "12.5 LPA"


RANGES = ["Less than 3 LPA", "3 - 6 LPA", "6 - 9 LPA", "9 - 12 LPA", "12 - 15 LPA", "15 - 18 LPA",
          "18 - 21 LPA", "21 - 24 LPA"]


def test_salary_range_options(bank):
    assert typed(ask(bank, "Current Salary", "select", RANGES, job=JOB_IN)) == "12 - 15 LPA"
    # 18 LPA sits on an edge: the range starting there, never "15 - 18" (undersells)
    assert typed(ask(bank, "Expected Salary", "select", RANGES, job=JOB_IN)) == "18 - 21 LPA"
    left(ask(bank, "Expected Salary", "select", ["Less than 3 LPA", "3 - 6 LPA"], job=JOB_IN))


def test_lpa_range_parsing():
    assert A._lpa_range("Less than 3 LPA") == (0.0, 3.0, True, False)
    assert A._lpa_range("24+ LPA") == (24.0, A.math.inf, True, True)
    assert A._lpa_range("15L - 20L") == (15.0, 20.0, True, True)
    assert A._lpa_range("1 - 1.5 Cr") == (100.0, 150.0, True, True)
    assert A._lpa_range("10,00,000 - 15,00,000") == (10.0, 15.0, True, True)
    assert A._lpa_range("3-6") is None and A._lpa_range("3-6", lpa_label=True) == (3.0, 6.0, True, True)
    assert A._lpa_range("$50k-$100k") is None and A._lpa_range("Negotiable") is None


# --------------------------------------------------------------------------- number boxes in general

def test_numeric_answer_is_never_concatenated_digits(bank):
    data = dict(bank.data)
    data["employment"] = {**data["employment"], "current_start_date": "", "total_experience": "2 years 10 months"}
    b = A.AnswersBank(data=data, statuses=None)
    assert typed(ask(b, "Experience (in years)", "text", hint="years")) == 2
    assert typed(ask(b, "Experience (months, beyond full years)", "select", MONTHS_0_11, hint="months")) == "10"
    data["employment"]["total_experience"] = "2+ years"
    left(ask(A.AnswersBank(data=data, statuses=None), "Experience (in years)", "number"))


def test_custom_answer_with_words_is_not_typed_into_a_number_box(bank):
    data = dict(bank.data)
    data["custom_answers"] = [{"question": "Relocation buffer (in days)", "answer": "about 2 weeks"}]
    left(ask(A.AnswersBank(data=data, statuses=None), "Relocation buffer (in days)", "number"))


# --------------------------------------------------------------------------- the pre-typing check

@pytest.mark.parametrize("value, label, hint, numeric", [
    ("210", "Experience (in years)", "years", False),
    ("2 years 10 months", "Experience", "years", False),
    ("12", "Months of experience (beyond full years)", "months", False),
    ("2027", "Available To Join (in days)", "", True),
    ("400", "Notice period (in days)", "", True),
    ("12.5", "Current Salary", "inr, annual", False),
    ("12.5", "Current Salary", "inr", False),
    ("1250000", "Current CTC (in LPA)", "", False),
    ("2027", "Number of days", "", True),
    ("-3", "Years of experience", "", True),
])
def test_implausible_values_are_refused(value, label, hint, numeric):
    why = A.implausible_number(value, label=label, hint=hint, numeric=numeric)
    assert why and value not in why                      # the reason never carries the value


@pytest.mark.parametrize("value, label, hint, numeric", [
    ("2", "Experience (in years)", "years", False),
    ("10", "Months of experience (beyond full years)", "months", False),
    ("30", "Available To Join (in days)", "", True),
    ("1250000", "Current Salary", "inr, annual", False),
    ("12.5", "Current CTC (in LPA)", "", False),
    ("2023", "Graduation year", "", True),               # a year field may hold a year
    ("+1 555 0100", "Mobile phone", "", False),          # not a numeric field: not checked
])
def test_plausible_values_pass(value, label, hint, numeric):
    assert A.implausible_number(value, label=label, hint=hint, numeric=numeric) is None


def test_field_bounds_and_step():
    assert A.implausible_number("70", label="Experience (in years)", numeric=True, bounds=("0", "60", "1"))
    assert A.implausible_number("2.5", label="Experience (in years)", numeric=True, bounds=("", "", "1"))
    assert A.implausible_number("2", label="Experience (in years)", numeric=True, bounds=("0", "60", "1")) is None


def test_digits_only_box_is_checked_only_for_quantities():
    assert A.implausible_number("2 years", label="Experience", digits_only=True)
    assert A.implausible_number("+1 555 0100", label="Phone", digits_only=True) is None


# --------------------------------------------------------------------------- Keka fixtures (browser)

@pytest.fixture(scope="module")
def server():
    s = FormServer().start()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    pw = sync_playwright().start()
    b = None
    for kw in ({"channel": "chrome"}, {}):
        try:
            b = pw.chromium.launch(headless=True, **kw)
            break
        except Exception:  # noqa: BLE001
            continue
    if b is None:
        pw.stop()
        pytest.skip("no browser available")
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def sess(browser):
    from jobcopilot.autofill.session import BrowserSession, _context_kwargs
    s = BrowserSession(browser.new_context(**_context_kwargs()))
    yield s
    s.context.close()


def _keka_run(sess, server, page, bank, answer_fn=service.answer_no_llm):
    server.requests.clear()
    ad = KekaAdapter()
    safe = sess.safe_page(**ad.safe_page_kwargs())
    url = server.url(page)
    safe.goto(url)
    ctx = PlanContext(job_key="keka-units", job=JOB_IN, url=url, answers=bank, facts=[],
                      answer_fn=answer_fn, today=TODAY)
    plan = ad.build_plan(ad.snapshot(safe), ctx)
    out = ad.run(safe, plan, ctx)
    sess.page.wait_for_timeout(200)
    return safe, plan, out


def _value(sess, sel):
    return sess.page.eval_on_selector(sel, "e => e.tagName === 'SELECT' ? e.options[e.selectedIndex].text : e.value")


def test_keka_unit_hints_are_read(sess, server):
    safe = sess.safe_page()
    safe.goto(server.url("keka_apply_annual.html"))
    hints = {f["selector"]: f["unit_hint"] for f in safe.fields()}
    assert hints["#workExperience"] == "years"
    assert hints['select[name="workExperience.months"]'] == "months"
    assert set(A.hint_units(hints["#currentSalary"])) == {"INR", "annual"}
    assert set(A.hint_units(hints["#expectedSalary"])) == {"INR", "annual"}
    assert hints["#firstName"] == "" and hints['#mobilePhone\\.number'] == ""


def test_keka_attero_fields_get_numbers_in_their_units(sess, server, bank):
    _, plan, out = _keka_run(sess, server, "keka_apply_annual.html", bank)
    assert _value(sess, "#workExperience") == "2"
    assert _value(sess, 'select[name="workExperience.months"]') == "10"
    assert _value(sess, "#availability") == "30"
    assert _value(sess, "#currentSalary") == "1250000"
    assert _value(sess, "#expectedSalary") == "1800000"
    assert _value(sess, '[id="mobilePhone.number"]') == "555 0100"   # beside its own code picker
    assert server.posts() == []


def test_keka_without_a_period_leaves_salaries_for_you(sess, server, bank):
    _, plan, out = _keka_run(sess, server, "keka_apply.html", bank)
    assert _value(sess, "#workExperience") == "2"
    assert _value(sess, 'select[name="workExperience.months"]') == "10"
    assert _value(sess, "#availability") == "30"
    assert _value(sess, "#currentSalary") == "" and _value(sess, "#expectedSalary") == ""
    by = {a.label: a for a in plan.actions()}
    assert by["Current Salary"].value is None and "per year or per month" in by["Current Salary"].note
    assert sess.page.get_attribute("#currentSalary", "data-jc-highlight") == "needs_you"
    assert server.posts() == []


def test_safepage_refuses_implausible_numbers_before_typing(sess, server):
    safe = sess.safe_page()
    safe.goto(server.url("keka_apply_annual.html"))
    for sel, bad in (("#workExperience", "210"), ("#workExperience", "2 years 10 months"),
                     ("#availability", "2027"), ("#currentSalary", "12.5")):
        with pytest.raises(ImplausibleValue):
            safe.fill(sel, bad, label="")
        assert _value(sess, sel) == ""
    safe.fill("#workExperience", "2")
    assert _value(sess, "#workExperience") == "2"


def bad_answer(question, *, field_type="text", options=None, required=None, **_):
    """The old wrong values, as if an answer slipped past the planner's checks."""
    q = question.lower()
    if "first name" in q:
        return Answer("Alex", "answers.yaml:personal.first_name", 1.0, False)
    if "experience (in years)" in q:
        return Answer("210", "answers.yaml:x", 1.0, False)
    if "available to join" in q:
        return Answer("2027", "answers.yaml:x", 1.0, False)
    if "current salary" in q:
        return Answer("12.5", "answers.yaml:x", 1.0, False)
    return Answer("", "none", 0.0, True, "no matching answer")


def test_wrong_numbers_never_reach_the_page(sess, server, bank):
    _, plan, out = _keka_run(sess, server, "keka_apply_annual.html", bank, answer_fn=bad_answer)
    by = {a.label: a for a in plan.actions()}
    for label in ("Experience (in years)", "Available To Join (in days)", "Current Salary"):
        assert by[label].value is None and "please answer it yourself" in by[label].note
    for sel in ("#workExperience", "#availability", "#currentSalary"):
        assert _value(sess, sel) == ""
        assert sess.page.get_attribute(sel, "data-jc-highlight") == "needs_you"
    assert _value(sess, "#firstName") == "Alex" and server.posts() == []


def test_run_time_check_names_the_field_in_the_outcome(sess, server, bank):
    """A plan made elsewhere (stale, or another planner) with a bad value: the
    executor's last check refuses it and says why, without the value."""
    from jobcopilot.autofill.plan import FieldAction, FillPlan
    server.requests.clear()
    ad = KekaAdapter()
    safe = sess.safe_page(**ad.safe_page_kwargs())
    url = server.url("keka_apply_annual.html")
    safe.goto(url)
    plan = FillPlan(job_key="k", ats="keka", url=url, steps=[[
        FieldAction("#workExperience", "Experience (in years)", "fill", "210", "answers.yaml:x", 1.0, False,
                    "", True)]], unresolved=[], files={})
    out = ad.run(safe, plan, None)
    assert _value(sess, "#workExperience") == ""
    assert "Experience (in years): the value is outside the plausible range" in out.reason
    assert "210" not in out.reason and out.status in ("needs_you", "blocked")
    assert server.posts() == []


def test_generic_executor_refuses_and_reports(sess, server):
    from jobcopilot.autofill.executor import run_plan
    from jobcopilot.autofill.generic import GenericAdapter
    from jobcopilot.autofill.plan import FieldAction, FillPlan
    server.requests.clear()
    safe = sess.safe_page()
    url = server.url("keka_apply_annual.html")
    safe.goto(url)
    plan = FillPlan(job_key="g", ats="generic", url=url, steps=[[
        FieldAction("#availability", "Available To Join (in days)", "fill", "2027", "answers.yaml:x", 1.0,
                    False, "", True),
        FieldAction("#firstName", "First Name", "fill", "Alex", "answers.yaml:x", 1.0, False, "", True)]],
        unresolved=[], files={})
    out = run_plan(safe, plan, adapter=GenericAdapter())
    assert _value(sess, "#availability") == "" and _value(sess, "#firstName") == "Alex"
    assert "Available To Join (in days): the value looks like a year" in out.reason
    assert "2027" not in out.reason and out.status == "needs_you"
    assert server.posts() == []
