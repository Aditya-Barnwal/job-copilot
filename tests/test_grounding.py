"""Anti-fabrication (grounding) checks — the gate that blocks a tailored résumé
from claiming things the original didn't. Pure functions, no network, no model.

Run:  python -m pytest tests/test_grounding.py -q
"""
import sys
from pathlib import Path

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
