"""YOE v2: section anchoring, formats, fresher signals, title-aware floors."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobcopilot.yoe import EVIDENCE_MAX, extract_min_yoe, extract_yoe, title_level

CASES = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "jd_snippets" / "cases.yaml").read_text(encoding="utf-8")
)["cases"]


def test_at_least_30_cases_including_affirm():
    assert len(CASES) >= 30
    assert any(c["id"].startswith("affirm_swe2") for c in CASES)
    assert len({c["id"] for c in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_snippet(case):
    r = extract_yoe(case["text"], case["title"])
    exp = case["expect"]
    for key, got in (("min", r.min_years), ("max", r.max_years)):
        if exp[key] is None:
            assert got is None, f"{key}: expected None, got {got} ({r.evidence!r})"
        else:
            assert got == pytest.approx(exp[key]), f"{key}: {got} vs {exp[key]} ({r.evidence!r})"
    assert r.is_preferred is exp["preferred"]
    assert r.fresher_friendly is exp["fresher"]
    if r.evidence is not None:
        assert len(r.evidence) <= EVIDENCE_MAX


def test_affirm_evidence_quotes_the_requirement():
    case = next(c for c in CASES if c["id"] == "affirm_swe2_1_5")
    r = extract_yoe(case["text"], case["title"])
    assert "1.5+ years" in r.evidence
    assert r.section == "required"
    assert r.yoe_min == 1 and r.min_years == 1.5


def test_v1_signature_kept():
    assert extract_min_yoe("5+ years of experience required") == (5, False)
    assert extract_min_yoe("2+ years preferred") == (2, True)
    assert extract_min_yoe("1.5+ years of experience as a software engineer") == (1, False)
    assert extract_min_yoe(None) == (None, False)
    assert extract_min_yoe("") == (None, False)


def test_long_clause_evidence_is_trimmed_around_the_match():
    text = "Requirements: " + "x " * 200 + "3+ years of experience with Go " + "y " * 200
    r = extract_yoe(text, "Backend Engineer")
    assert r.min_years == 3
    assert "3+ years" in r.evidence and len(r.evidence) <= EVIDENCE_MAX


def test_title_levels():
    assert title_level("Software Engineer II") == "junior"
    assert title_level("SDE 1") == "junior"
    assert title_level("Associate Software Engineer") == "junior"
    assert title_level("Senior Backend Engineer") == "senior"
    assert title_level("SDE 3") == "senior"
    assert title_level("Member of Technical Staff") == "neutral"
    assert title_level("Software Engineer") == "neutral"


def test_ambiguous_flag_when_several_floors_survive():
    r = extract_yoe("Requirements\n- 1+ years of Go experience\n- 3+ years of AWS experience", "Engineer II")
    assert r.min_years == 1 and r.ambiguous is True
