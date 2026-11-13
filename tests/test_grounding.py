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


def test_bare_number_words_are_flagged_but_one_idioms_are_not():
    # R3W-1: "two engineers", "three services" are quantity claims; "one of" is an idiom.
    tailored = BASE + " One of two engineers on three services."
    g = check_grounding(BASE, tailored, job_missing_keywords=[])
    assert g["new_numbers"] == ["three", "two"], g["new_numbers"]
    g = check_grounding(BASE + " Ran 2 engineers on 3 services.", tailored, job_missing_keywords=[])
    assert g["new_numbers"] == [], g["new_numbers"]           # same value in digits


# --- grounding v3: per-line checks against cited facts ------------------------

from jobcopilot.grounding import check_lines, token_overlap, unconfirmed_line_flags  # noqa: E402

FACTS = {
    "b1": "Built a Go order service handling 200+ requests per second behind a REST API.",
    "b2": "Cut nightly batch runtime by 30% by moving widget pricing to PostgreSQL.",
    "h1": "Acme Widgets Inc. | Springfield | Software Engineer | Jan 2024 – Present",
}


def _one(text, ids=("b1",), **kw):
    return check_lines([{"text": text, "fact_ids": list(ids), **kw}], FACTS)


def test_v3_verbatim_lines_are_supported():
    g = check_lines([{"text": t, "fact_ids": [k]} for k, t in FACTS.items()], FACTS)
    assert g["clean"] and g["unsupported_lines"] == [], g["flags"]


def test_v3_line_is_checked_only_against_its_cited_facts():
    # "PostgreSQL" and "30%" are true of b2, but this line cites b1.
    g = _one("Built a Go order service on PostgreSQL handling 200+ requests per second; 30% faster.")
    assert "PostgreSQL" in g["flags"]["unsupported_skills"] and "30%" in g["flags"]["new_numbers"]
    g = _one("Built a Go order service on PostgreSQL handling 200+ requests per second; 30% faster.",
             ids=("b1", "b2"))
    assert not g["flags"]["unsupported_skills"] and not g["flags"]["new_numbers"]


def test_v3_flags_numbers_including_spelled_out():
    g = _one("Built a Go order service handling 200+ requests per second, doubling throughput.")
    assert "doubling" in g["flags"]["new_numbers"] and not g["clean"]
    assert "500" in _one("Built a Go order service handling 500+ requests per second.")["flags"]["new_numbers"]


def test_v3_flags_claims_titles_and_proper_nouns():
    g = _one("Led a Go order service at Stripe as Senior Engineer handling 200+ requests per second.")
    f = g["flags"]
    assert "led" in f["inflated_claims"] and "senior (title)" in f["inflated_claims"]
    assert f["new_proper_nouns"] == ["Stripe"]
    assert g["lines"][0]["offending"] and not g["lines"][0]["supported"]


def test_v3_sentence_initial_verb_is_not_a_proper_noun():
    g = _one("Engineered a Go order service handling 200+ requests per second behind a REST API.",
             rephrased=True)
    assert g["flags"]["new_proper_nouns"] == [] and g["clean"], g["flags"]


def test_v3_unknown_or_missing_fact_fails_closed():
    g = check_lines([{"text": "Anything.", "fact_ids": ["nope"]},
                     {"text": "Anything.", "fact_ids": []}], FACTS)
    assert g["flags"]["unknown_facts"] == ["nope", "(no fact cited)"] and g["unsupported_lines"] == [0, 1]


def test_v3_attested_keywords_allowed_but_never_numbers():
    line = [{"text": "Built a Go order service on Kubernetes at 900 rps.", "fact_ids": ["b1"]}]
    g = check_lines(line, FACTS, attested=["kubernetes", "900"])
    assert g["flags"]["unsupported_skills"] == [] and g["flags"]["new_numbers"] == ["900"]


def test_v3_similarity_guard():
    assert token_overlap(FACTS["b1"], FACTS["b1"]) == 1.0
    g = _one("Shipped a fast 200+ requests per second service.", rephrased=True)
    assert g["flags"]["low_similarity"] == ["b1"] and g["lines"][0]["similarity"] < 0.45
    g = _one("Built a REST API order service in Go handling 200+ requests per second.", rephrased=True)
    assert g["clean"] and g["lines"][0]["similarity"] >= 0.45


def test_v3_unconfirmed_flags_per_token():
    g = _one("Built a Go order service at Stripe handling 900 requests per second.")
    assert unconfirmed_line_flags(g, {"new_numbers": ["900"]}) == {"new_proper_nouns": ["Stripe"]}
    full = {k: list(v) for k, v in g["flags"].items()}
    assert unconfirmed_line_flags(g, full) == {}
    assert unconfirmed_line_flags(g, "yes") == {k: v for k, v in g["flags"].items() if v}
    bad = check_lines([{"text": "x", "fact_ids": ["nope"]}], FACTS)
    assert "unknown_facts" in unconfirmed_line_flags(bad, {"unknown_facts": ["nope"]})   # never confirmable


def test_check_lines_folds_accents_and_unicode_dashes():
    """'résumé' must not tokenise into the skill 'R'; 'LLM‑powered' (U+2011) is not a new proper noun."""
    from jobcopilot.grounding import check_lines
    facts = {"f1": "Engineered an LLM-powered resume-tailoring stage with versioned PDF storage."}
    out = check_lines([{"text": "Engineered an LLM‑powered résumé-tailoring stage with versioned PDF storage.",
                        "fact_ids": ["f1"]}], facts)
    assert out["clean"], out["lines"][0]


# --- capability claims (v3) ---------------------------------------------------
from jobcopilot.grounding import capability_claims  # noqa: E402


@pytest.mark.parametrize("text", [
    "Built a Go order service handling 200+ requests per second in production.",
    "Built an end-to-end Go order service handling 200+ requests per second.",
    "Owned a Go order service handling 200+ requests per second behind a REST API.",
    "Built a Go order service handling 200+ requests per second, showcasing deep expertise in Kafka streaming.",
    "Built a Go order service handling 200+ requests per second; this demonstrates strong backend skills.",
])
def test_v3_capability_claims_flag_resume_lines(text):
    g = _one(text, rephrased=True)
    assert not g["clean"]
    assert g["flags"]["unsupported_claims"] or g["flags"]["inflated_claims"], g["flags"]


def test_v3_capability_claims_supported_by_own_fact():
    facts = {"p": "Ran the end-to-end ingestion pipeline in production for 3 hospitals."}
    g = check_lines([{"text": facts["p"], "fact_ids": ["p"]}], facts)
    assert g["clean"], g["flags"]
    assert capability_claims("I have experience with the ingestion pipeline in production.", facts["p"]) == []


def test_v3_capability_claims_ignore_pseudo_context_and_can_be_skipped():
    facts = {"b1": FACTS["b1"], "_role": "Forward Deployed Engineer AI agents production"}
    line = {"text": "I bring experience scaling AI agents to production.", "fact_ids": ["b1", "_role"]}
    g = check_lines([line], facts)
    assert g["flags"]["unsupported_claims"], g["flags"]          # the role text is not evidence
    g = check_lines([dict(line, check_claims=False)], facts)
    assert g["flags"]["unsupported_claims"] == []


def test_v3_capability_claim_phrase_reported_once():
    out = capability_claims("This experience demonstrates my skills needed for end-to-end AI deployment.",
                            FACTS["b1"])
    assert len(out) == 1 and out[0].startswith("This experience demonstrates")


# --- Phase 3 review 3 (R3W-1 / R3W-4 / R3W-11) ---------------------------------

@pytest.mark.parametrize("text, token", [
    ("Built a Go order service handling 200+ requests per second for millions of users.", "millions"),
    ("Built a Go order service handling 200+ requests per second with a team of five.", "five"),
    ("Built a Go order service handling 200+ requests per second and cut costs in half.", "in half"),
    ("Built a Go order service handling 200+ requests per second for dozens of shops.", "dozens"),
    ("Built a Go order service handling 200+ requests per second for thousands of merchants.", "thousands"),
    ("Built a Go order service handling 200+ requests per second, twice as fast.", "twice"),
    ("Built a Go order service handling 200+ requests per second for several hundred shops.",
     "several hundred"),
])
def test_r3w1_quantities_in_words_are_numbers(text, token):
    g = _one(text)
    assert token in g["flags"]["new_numbers"], g["flags"]


def test_r3w1_word_quantity_supported_by_same_value_or_word():
    facts = {"f": "Cut cloud spend by 50% for hundreds of merchants with 5 engineers."}
    for t in ("Cut cloud spend in half for hundreds of merchants with five engineers.",):
        g = check_lines([{"text": t, "fact_ids": ["f"]}], facts)
        assert g["flags"]["new_numbers"] == [], g["flags"]


@pytest.mark.parametrize("text, fam", [
    ("Single-handedly built a Go order service handling 200+ requests per second.", "single-handedly (scope)"),
    ("Built a Go order service handling 200+ requests per second as the sole engineer.", "sole (scope)"),
    ("Independently built a Go order service handling 200+ requests per second.", "independently (scope)"),
    ("Built a Go order service handling 200+ requests per second in a team of 200+.", "team of (scope)"),
    ("Built a solo Go order service handling 200+ requests per second.", "solo (scope)"),
    ("Built a senior-grade Go order service handling 200+ requests per second.", "senior (title)"),
])
def test_r3w1_scope_claims_need_the_cue_in_evidence(text, fam):
    assert fam in _one(text)["flags"]["inflated_claims"]
    assert check_lines([{"text": text, "fact_ids": ["f"]}], {"f": text})["clean"]


@pytest.mark.parametrize("text, key", [
    ("Lеd a Go order service handling 200+ requests per second.", "inflated_claims"),     # Cyrillic е
    ("Built a Go order service handling 200+ requests per second on kаfka.", "unsupported_skills"),
    ("Built a Go order service handling 200+ requests per second on Kaf​ka.", "unsupported_skills"),
    ("Built a Go order service handling ９００ requests per second.", "new_numbers"),
])
def test_r3w4_confusables_fold_for_checking(text, key):
    assert _one(text)["flags"][key], _one(text)["flags"]


def test_r3w4_confusable_problem():
    from jobcopilot.grounding import confusable_problem
    assert confusable_problem("Lеd") == "non-Latin or confusable characters"
    assert confusable_problem("Kaf​ka") and confusable_problem("x﻿")
    assert confusable_problem("Zürich, Łódź, résumé") is None             # Latin-1 / Extended-A
    assert confusable_problem("Built Δ-sync", "Built Δ-sync for docs") is None   # in the cited fact


@pytest.mark.parametrize("text", ["Served over 1 M users.", "Served over 1 million users.",
                                  "Served more than one million users.", "Served over a million users."])
def test_r3w11_normalize_metrics(text):
    from jobcopilot.grounding import normalize_metrics
    out, changes = normalize_metrics(text, ["1M+", "30%"])
    assert out == "Served 1M+ users." and len(changes) == 1 and changes[0]["to"] == "1M+"


@pytest.mark.parametrize("text, metrics", [
    ("Served 1 million users.", ["1M+"]),                        # no "over": a different claim
    ("Served over 2 million users.", ["1M+"]),                   # another value: never invented
    ("Served over a million users and over a million calls.", ["1M+"]),   # ambiguous
    ("Served over a million users.", ["1M+", "1M"]),             # two metrics share the value
    ("Served 1M+ users, over a million calls.", ["1M+"]),        # token already there
])
def test_r3w11_normalize_metrics_only_when_unambiguous(text, metrics):
    from jobcopilot.grounding import normalize_metrics
    assert normalize_metrics(text, metrics) == (text, [])
