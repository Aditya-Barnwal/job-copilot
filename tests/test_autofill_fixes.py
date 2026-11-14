"""Regressions from the Phase 4 integration review (guard labels, answer rules)."""
from jobcopilot import answers as A
from jobcopilot.autofill import guard


def test_text_field_labelled_applying_is_not_submit_like():
    meta = {"tag": "input", "type_attr": "text", "role": "combobox",
            "name_text": "Which office are you applying to?"}
    assert not guard.is_submit_like(meta)
    assert not guard.is_submit_like({"tag": "textarea", "name_text": "Why are you applying?"})


def test_buttons_are_still_submit_like():
    assert guard.is_submit_like({"tag": "button", "type_attr": "button", "text": "Submit application"})
    assert guard.is_submit_like({"tag": "a", "text": "Apply now"})
    assert guard.is_submit_like({"tag": "div", "role": "button", "text": "Submit"})
    assert guard.is_submit_like({"tag": "input", "type_attr": "submit", "value": "Go"})
    # a "combobox" that is really a submit input is still caught structurally
    assert guard.is_submit_like({"tag": "input", "type_attr": "submit", "role": "combobox"})


BANK = A.as_bank({"personal": {"full_name": "Test Person", "first_name": "Test", "last_name": "Person"},
                  "employment": {"current_employer": "Acme Labs"}})


def _ans(q):
    return A.answer_question(q, answers=BANK, facts=[], field_type="text")


def test_name_of_something_never_gets_the_users_name():
    for q in ("What is the name of your university?", "Name of your manager", "Team name"):
        assert _ans(q).value != "Test Person", q


def test_plain_name_questions_still_resolve():
    for q in ("Full name", "Name", "Name *", "Your name", "Legal name"):
        assert _ans(q).value == "Test Person", q


def test_current_or_previous_employer():
    assert _ans("Who is your current or previous employer?").value == "Acme Labs"
