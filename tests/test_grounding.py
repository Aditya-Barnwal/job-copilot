"""Anti-fabrication (grounding) checks — the gate that blocks a tailored résumé
from claiming things the original didn't. Pure functions, no network, no model.

Run:  python -m pytest tests/test_grounding.py -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobcopilot.grounding import check_grounding


ORIGINAL = (
    "Software engineer. Collaborated on a Python backend and REST APIs. "
    "Contributed to a data pipeline. Cut incident response from 2 hours to 15 minutes."
)


def test_clean_when_only_rewording_original():
    # Same facts, reordered words, no new keyword/number/verb → nothing to flag.
    tailored = ("Python software engineer who contributed to REST APIs and a "
                "backend, plus a data pipeline. Reduced incident response time.")
    g = check_grounding(ORIGINAL, tailored, job_missing_keywords=[])
    assert g["clean"], g


def test_new_number_is_flagged():
    tailored = ORIGINAL + " Improved throughput by 40%."
    g = check_grounding(ORIGINAL, tailored, job_missing_keywords=[])
    assert "40%" in g["new_numbers"]
    assert not g["clean"]


def test_comma_reformat_is_not_a_new_number():
    # '15' already present; adding '5,000' where original had '5000' must NOT flag.
    orig = ORIGINAL + " Handled 5000 events."
    tailored = ORIGINAL + " Handled 5,000 events."
    g = check_grounding(orig, tailored, job_missing_keywords=[])
    assert g["new_numbers"] == [], g["new_numbers"]


def test_inserted_missing_keyword_is_flagged():
    tailored = ORIGINAL + " Deep experience with Kubernetes."
    g = check_grounding(ORIGINAL, tailored, job_missing_keywords=["kubernetes"])
    assert "kubernetes" in g["inserted_unsupported"]
    assert not g["clean"]


def test_user_approved_keyword_is_not_flagged():
    tailored = ORIGINAL + " Familiar with Kubernetes."
    g = check_grounding(ORIGINAL, tailored, job_missing_keywords=["kubernetes"],
                        user_approved_keywords={"kubernetes"})
    assert g["inserted_unsupported"] == []
    # And it must not leak into unsupported_skills either.
    assert "kubernetes" not in g["unsupported_skills"]


def test_fabricated_skill_outside_missing_set_is_flagged():
    # 'terraform' is a real tech term the model invented; it's NOT in the job's
    # missing-keyword set, so inserted_unsupported would miss it — this is the
    # hole unsupported_skills exists to close.
    tailored = ORIGINAL + " Managed infrastructure with Terraform."
    g = check_grounding(ORIGINAL, tailored, job_missing_keywords=["graphql"])
    assert "terraform" in g["unsupported_skills"]
    assert "terraform" not in g["inserted_unsupported"]
    assert not g["clean"]


def test_no_double_report_between_checks():
    # A term in the missing set is reported by inserted_unsupported ONLY.
    tailored = ORIGINAL + " Built services in Go."
    g = check_grounding(ORIGINAL, tailored, job_missing_keywords=["go"])
    assert "go" in g["inserted_unsupported"]
    assert "go" not in g["unsupported_skills"]


def test_seniority_inflation_is_flagged():
    # "Collaborated on" (original) rewritten as "Led" — pure credit inflation
    # using no new vocabulary. The keyword/number checks can't see it.
    tailored = ("Software engineer. Led a Python backend and REST APIs. "
                "Contributed to a data pipeline. Cut incident response from 2 "
                "hours to 15 minutes.")
    g = check_grounding(ORIGINAL, tailored, job_missing_keywords=[])
    assert "led" in g["inflated_claims"]
    assert not g["clean"]


def test_ownership_verb_already_in_original_is_not_flagged():
    orig = "Led the platform team. Managed three engineers."
    tailored = "Led the platform team and managed three engineers effectively."
    g = check_grounding(orig, tailored, job_missing_keywords=[])
    assert g["inflated_claims"] == [], g["inflated_claims"]


def test_backward_compatible_keys_present():
    g = check_grounding(ORIGINAL, ORIGINAL, job_missing_keywords=[])
    for key in ("inserted_unsupported", "new_numbers", "clean",
                "unsupported_skills", "inflated_claims"):
        assert key in g
    assert g["clean"]


# --- review finding #1: base forms, title inflation, spelled-out numbers -----

BASE = "Backend engineer. Built REST APIs in Python. Wrote tests."


def test_finding1_base_form_and_title_inflation_scenario():
    # The exact shape from the review: every claim below is absent from BASE,
    # and before the fix check_grounding returned clean=True for all of it.
    tailored = ("Lead Backend Engineer and Head of Platform. Lead a team that "
                "would own and manage REST APIs. Mentor engineers. Tripled "
                "throughput for ten million users.")
    g = check_grounding(BASE, tailored, job_missing_keywords=[])
    assert not g["clean"]
    for fam in ("led", "owned", "managed", "mentored", "lead (title)", "head of (title)"):
        assert fam in g["inflated_claims"], (fam, g["inflated_claims"])
    assert "tripled" in g["new_numbers"] and "ten million" in g["new_numbers"]


def test_lead_a_team_base_form_is_flagged():
    g = check_grounding(BASE, BASE + " Lead a team of engineers.", job_missing_keywords=[])
    assert "led" in g["inflated_claims"]


def test_lead_software_engineer_title_is_flagged_once():
    g = check_grounding(BASE, "Lead Software Engineer. " + BASE, job_missing_keywords=[])
    assert g["inflated_claims"] == ["lead (title)"], g["inflated_claims"]



@pytest.mark.parametrize("phrase,family", [
    ("Own the payments service.", "owned"),
    ("Manage the release process.", "managed"),
    ("Mentor two interns.", "mentored"),
    ("Head the platform group.", "headed"),
    ("Architect the event pipeline.", "architected"),
    ("Direct the migration effort.", "directed"),
    ("Oversee the on-call rotation.", "oversaw"),
    ("Pioneer a new review process.", "pioneered"),
    ("Orchestrate the quarterly planning.", "orchestrated"),
    ("Spearhead the API redesign.", "spearheaded"),
    ("Found a developer-tools startup.", "founded"),
    ("Co-founded a campus coding club.", "founded"),
    ("Co-led the API redesign.", "led"),
    ("- Led the API redesign.", "led"),
])
def test_base_and_other_ownership_forms_are_flagged(phrase, family):
    g = check_grounding(BASE, BASE + " " + phrase, job_missing_keywords=[])
    assert family in g["inflated_claims"], (phrase, g["inflated_claims"])


@pytest.mark.parametrize("title,family", [
    ("Senior Software Engineer", "senior (title)"),
    ("Sr. Backend Engineer", "senior (title)"),
    ("Staff Engineer", "staff (title)"),
    ("Principal Machine Learning Engineer", "principal (title)"),
    ("Tech Lead", "lead (title)"),
    ("Head of Platform", "head of (title)"),
])
def test_title_inflation_is_flagged(title, family):
    g = check_grounding(BASE, title + ". " + BASE, job_missing_keywords=[])
    assert family in g["inflated_claims"], (title, g["inflated_claims"])


def test_title_already_in_original_is_not_flagged():
    orig = "Senior Software Engineer. Tech Lead for payments. " + BASE
    tailored = "Senior Backend Engineer and Tech Lead. " + BASE
    g = check_grounding(orig, tailored, job_missing_keywords=[])
    assert g["inflated_claims"] == [], g["inflated_claims"]


@pytest.mark.parametrize("phrase", [
    "Built data-driven dashboards.",
    "Caching changes lead to faster pages.",
    "The refactor led to fewer incidents.",
    "Small fixes leading to a 2x speedup.",       # '2x' is a number; checked below
    "Better alerts leads to calmer on-call.",
    "Joined a leading fintech.",
    "Built industry-leading search.",
    "Wrote my own test runner.",
    "Found and fixed a memory leak.",
    "Found that retries hid the bug.",
    "Collaborated with senior engineers on reviews.",
    "Paired with the staff engineer on design.",
    "Applied principal component analysis.",
    "Had direct impact on checkout conversion.",
    "Used a managed Kubernetes cluster.",
    "Used Redux to manage state.",
    "Modelled the deps as a directed acyclic graph.",
    "Was mentored by a senior engineer.",
    "Ran jobs orchestrated with Airflow.",
    "Built an LED status panel.",
    "Learned head-to-head benchmarking.",
    "Worked on lead generation tooling.",
])
def test_non_claim_uses_stay_clean(phrase):
    g = check_grounding(BASE + " 2x", BASE + " 2x " + phrase, job_missing_keywords=[])
    assert g["inflated_claims"] == [], (phrase, g["inflated_claims"])


@pytest.mark.parametrize("phrase,token", [
    ("Doubled throughput.", "doubled"),
    ("Tripled signups.", "tripled"),
    ("Served ten million users.", "ten million"),
    ("Cut costs by forty percent.", "forty percent"),
    ("Grew revenue twenty-five percent.", "twenty-five percent"),
    ("Saved two hundred thousand dollars.", "two hundred thousand"),
    ("Achieved a tenfold speedup.", "tenfold"),
])
def test_spelled_out_quantities_are_new_numbers(phrase, token):
    g = check_grounding(BASE, BASE + " " + phrase, job_missing_keywords=[])
    assert token in g["new_numbers"], (phrase, g["new_numbers"])
    assert not g["clean"]


def test_spelled_out_quantity_matching_original_digits_is_not_new():
    orig = BASE + " Cut costs by 40%. Served 10M users. Gave a 2x speedup."
    tailored = BASE + " Cut costs by forty percent. Served ten million users. Doubled speed."
    g = check_grounding(orig, tailored, job_missing_keywords=[])
    assert g["new_numbers"] == [], g["new_numbers"]


def test_plain_number_words_are_not_flagged():
    # Bare small numbers ("one of", "two services") are everyday prose.
    tailored = BASE + " One of two engineers on three services."
    g = check_grounding(BASE, tailored, job_missing_keywords=[])
    assert g["new_numbers"] == [], g["new_numbers"]
