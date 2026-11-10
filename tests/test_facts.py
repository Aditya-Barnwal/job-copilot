"""Résumé facts: schema, validator, usable view and CLI. Offline; uses the fake
profile.example/ (and validates the real git-ignored profile/ only if present).

Run:  python -m pytest tests/test_facts.py -q
"""
from __future__ import annotations

import copy
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobcopilot import facts as F  # noqa: E402

EXAMPLE_DIR = ROOT / "profile.example"
EXAMPLE = EXAMPLE_DIR / "resume_facts.yaml"
EXAMPLE_BASES = EXAMPLE_DIR / "base_resumes" / "bases.yaml"
REAL = ROOT / "profile" / "resume_facts.yaml"


@pytest.fixture
def data() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def bases() -> dict[str, F.Base]:
    return F.load_bases(EXAMPLE_BASES)


def errors_for(d: dict, bases: dict | None = None) -> list[str]:
    return F.validate(F.parse(copy.deepcopy(d)), bases)


def has_error(errors: list[str], *needles: str) -> bool:
    return any(all(n in e for n in needles) for e in errors)


# --------------------------------------------------------------------------- validator

def test_example_validates_cleanly(bases):
    assert F.validate(F.load(EXAMPLE), bases) == []


def test_duplicate_id(data):
    data["experience"][0]["bullets"][1]["id"] = "exp.acme.b1"
    assert has_error(errors_for(data), "exp.acme.b1", "duplicate id")


def test_duplicate_id_across_fact_and_claim(data):
    data["skills"]["cloud"][0]["claims"][0]["id"] = "skill.aws"
    assert has_error(errors_for(data), "duplicate id")


def test_bad_status(data):
    data["experience"][0]["bullets"][0]["status"] = "yes"
    assert has_error(errors_for(data), "exp.acme.b1", "status 'yes'")


def test_missing_status(data):
    del data["education"][0]["status"]
    assert has_error(errors_for(data), "edu.esu", "status '(missing)'")


def test_pending_needs_until_and_reason(data):
    b2 = data["projects"][0]["bullets"][1]
    del b2["pending_until"]
    errs = errors_for(data)
    assert has_error(errs, "proj.widgetbot.b2", "pending_until")
    del b2["pending_reason"]
    assert has_error(errors_for(data), "proj.widgetbot.b2", "pending_reason")


def test_pending_claim_needs_until(data):
    del data["projects"][0]["bullets"][0]["claims"][0]["pending_until"]
    assert has_error(errors_for(data), "proj.widgetbot.b1.ecs", "pending_until")


def test_metric_not_in_text(data):
    data["experience"][0]["bullets"][0]["metrics"] = ["200+", "99%"]
    errs = errors_for(data)
    assert has_error(errs, "exp.acme.b1", "'99%'", "does not appear")
    assert not has_error(errs, "'200+'")


def test_metric_in_a_variant_only_is_accepted(data):
    # The real DUM-E bullet: "29%" exists only in the ml_ai wording.
    b = data["experience"][0]["bullets"][1]
    b["variants"]["ml_ai"] += " Saved 12 hours."
    b["metrics"].append("12 hours")
    assert errors_for(data) == []


def test_metric_without_a_number(data):
    data["experience"][0]["bullets"][0]["metrics"] = ["Go"]
    assert has_error(errors_for(data), "metric 'Go'", "no number")


def test_required_fields(data):
    data["experience"][0]["bullets"][0]["text"] = ""
    data["experience"][0]["title"] = ""
    data["person"]["name"] = ""
    errs = errors_for(data)
    assert has_error(errs, "exp.acme.b1", "`text` is required")
    assert has_error(errs, "exp.acme", "`title` is required")
    assert has_error(errs, "person.name")


def test_schema_version(data):
    data["schema_version"] = 2
    assert has_error(errors_for(data), "schema_version")


def test_sources_must_be_known_bases(data):
    data["experience"][0]["bullets"][0]["sources"] = ["backend_sde", "frontend"]
    assert has_error(errors_for(data), "exp.acme.b1", "unknown source 'frontend'")
    data["experience"][0]["bullets"][0]["sources"] = []
    assert has_error(errors_for(data), "exp.acme.b1", "at least one base")


def test_variant_base_must_be_a_source(data):
    data["experience"][0]["bullets"][1]["sources"] = ["backend_sde"]
    assert has_error(errors_for(data), "exp.acme.b2", "variant 'ml_ai'")


def test_bullet_sources_within_header(data):
    data["hackathons"][0]["bullets"][0]["sources"] = ["backend_sde", "ml_ai"]
    assert has_error(errors_for(data), "hack.hackexample.b1", "ml_ai", "header")


def test_bullet_id_must_extend_header_id(data):
    data["experience"][0]["bullets"][2]["id"] = "exp.other.b3"
    assert has_error(errors_for(data), "exp.other.b3", "start with 'exp.acme.'")


def test_id_format(data):
    data["skills"]["languages"][0]["id"] = "Skill Python"
    assert has_error(errors_for(data), "dotted lowercase")


def test_skill_tag_must_appear_in_text(data):
    data["experience"][0]["bullets"][0]["skills"] = ["Go", "Kubernetes"]
    errs = errors_for(data)
    assert has_error(errs, "exp.acme.b1", "'Kubernetes'")
    assert not has_error(errs, "'Go'")


def test_skill_tag_is_whole_term(data):
    # "Go" must not be satisfied by "Google"; tags are whole terms.
    b = data["experience"][0]["bullets"][1]
    b["text"] = b["text"].replace("PostgreSQL", "Google Cloud SQL")
    b["variants"]["ml_ai"] = b["variants"]["ml_ai"].replace("PostgreSQL", "Google Cloud SQL")
    b["skills"] = ["Go"]
    assert has_error(errors_for(data), "exp.acme.b2", "'Go'")


def test_header_fields_must_match_text(data):
    data["experience"][0]["title"] = "Senior Software Engineer"
    assert has_error(errors_for(data), "exp.acme", "`title` does not appear")


def test_research_kind(data):
    data["research"][0]["kind"] = "blog"
    assert has_error(errors_for(data), "res.widget_paper", "kind 'blog'")


def test_claim_span_must_be_in_text(data):
    data["projects"][0]["bullets"][0]["claims"][0]["text"] = "; deployed on GKE"
    assert has_error(errors_for(data), "proj.widgetbot.b1.ecs", "span")


def test_claim_span_must_be_in_each_variant(data):
    b = data["projects"][0]["bullets"][0]
    b["variants"]["ml_ai"] = b["variants"]["ml_ai"].replace("; deployed on AWS ECS", "; runs on ECS")
    errs = errors_for(data)
    assert has_error(errs, "proj.widgetbot.b1.ecs", "'ml_ai' variant")
    b["claims"][0]["variants"] = {"ml_ai": "; runs on ECS"}
    assert errors_for(data) == []


def test_claim_tech_must_be_in_tech_line(data):
    data["projects"][0]["claims"][0]["tech"] = ["Kubernetes"]
    assert has_error(errors_for(data), "proj.widgetbot.tech_ecs", "'Kubernetes'")


def test_claim_needs_text_or_tech(data):
    c = data["projects"][0]["claims"][0]
    c["text"] = "WidgetBot"
    assert has_error(errors_for(data), "proj.widgetbot.tech_ecs", "exactly one")


def test_malformed_entries_are_reported_not_raised(data):
    data["experience"].append("not a mapping")
    data["skills"]["languages"][0]["sources"] = "backend_sde"
    errs = errors_for(data)
    assert has_error(errs, "experience[2]", "mapping")
    assert has_error(errs, "sources", "must be a list")


def test_errors_never_quote_resume_text(data):
    data["experience"][0]["bullets"][0]["metrics"] = ["999"]
    data["experience"][0]["bullets"][0]["skills"] = ["Rust"]
    text = data["experience"][0]["bullets"][0]["text"]
    assert all(text not in e for e in errors_for(data))


# --------------------------------------------------------------------------- bases layout

def test_bases_must_place_every_fact_once(data, bases):
    bases["backend_sde"].skills_categories[0].skills.remove("skill.go")
    assert has_error(errors_for(data, bases), "bases.backend_sde", "skill.go", "exactly once")


def test_bases_reject_fact_without_that_source(data, bases):
    bases["ml_ai"].skills_categories[0].skills.append("skill.go")
    assert has_error(errors_for(data, bases), "bases.ml_ai", "skill.go", "sources")


def test_bases_unknown_id_and_section(data, bases):
    bases["backend_sde"].projects.append("proj.nope")
    bases["backend_sde"].sections.append("awards")
    errs = errors_for(data, bases)
    assert has_error(errs, "proj.nope", "not a known Project")
    assert has_error(errs, "unknown section 'awards'")


def test_bases_tech_order_must_be_a_permutation(data, bases):
    bases["ml_ai"].tech_order["proj.widgetbot"] = ["Python", "FastAPI"]
    assert has_error(errors_for(data, bases), "tech_order.proj.widgetbot")


def test_bases_need_roles(data, bases):
    bases["ml_ai"].roles = []
    assert has_error(errors_for(data, bases), "bases.ml_ai", "roles")


def test_sources_checked_against_bases_file(data, bases):
    del bases["ml_ai"]
    assert has_error(errors_for(data, bases), "unknown source 'ml_ai'")


# --------------------------------------------------------------------------- usable view

def test_usable_facts_excludes_unconfirmed_and_pending():
    facts = F.load(EXAMPLE)
    usable = F.usable_facts(facts)
    ids = {f.id for f in usable}
    assert usable and all(f.status == "confirmed" for f in usable)
    by_id = facts.by_id()
    for fid, f in by_id.items():
        if f.status != "confirmed":
            assert fid not in ids, fid
    assert "proj.widgetbot.b2" not in ids          # pending
    assert "exp.acme.b3" not in ids                # unconfirmed
    assert "exp.globex.b1" not in ids              # confirmed, but its header isn't
    assert {"edu.esu", "exp.acme", "exp.acme.b1", "proj.widgetbot.b1", "skill.aws"} <= ids


def test_usable_facts_cuts_unconfirmed_claims():
    facts = F.load(EXAMPLE)
    u = {f.id: f for f in F.usable_facts(facts)}
    b1 = u["proj.widgetbot.b1"]
    assert "ECS" not in b1.text and b1.text.endswith("widget question.")
    assert "ECS" not in b1.variants["ml_ai"]
    assert b1.skills == ["FastAPI"] and b1.claims == []
    assert u["proj.widgetbot"].tech == ["Python", "FastAPI"]
    assert [b.id for b in u["proj.widgetbot"].bullets] == ["proj.widgetbot.b1"]
    assert u["skill.aws"].text == "AWS (Lambda)" and u["skill.aws"].skills == ["Lambda"]
    assert "skill.docker" not in u
    # The loaded facts themselves are untouched.
    assert "ECS" in facts.by_id()["proj.widgetbot.b1"].text


def test_confirmed_claim_is_kept(data):
    data["projects"][0]["bullets"][0]["claims"][0]["status"] = "confirmed"
    u = {f.id: f for f in F.usable_facts(F.parse(data))}
    assert u["proj.widgetbot.b1"].text.endswith("deployed on AWS ECS.")


def test_usable_facts_for_one_base():
    u = {f.id: f for f in F.usable_facts(F.load(EXAMPLE), base="ml_ai")}
    assert "skill.go" not in u and "hack.hackexample" not in u
    assert "feature-batch" in u["exp.acme.b2"].text and u["exp.acme.b2"].variants == {}
    assert "intent model" in u["proj.widgetbot.b1"].text and "ECS" not in u["proj.widgetbot.b1"].text


def test_fresh_prefill_has_nothing_usable(data):
    # A real file straight from the PDFs: everything unconfirmed -> nothing usable.
    raw = yaml.safe_dump(data)
    raw = re.sub(r"status: confirmed", "status: unconfirmed", raw)
    assert F.usable_facts(F.parse(yaml.safe_load(raw))) == []


# --------------------------------------------------------------------------- real profile

@pytest.mark.skipif(not REAL.exists(), reason="profile/ is personal and git-ignored (absent in CI)")
def test_real_profile_validates():
    facts = F.load(REAL)
    errors = F.validate(facts, F.load_bases_for(REAL))
    assert errors == []
    assert sum(1 for _ in facts.facts()) > 0


# --------------------------------------------------------------------------- answers template

ANSWER_KEYS = {"notice_period", "current_location", "willing_to_relocate", "work_authorization",
               "expected_ctc", "current_ctc", "earliest_start", "remote_preference",
               "why_company_template"}


@pytest.mark.parametrize("path", [EXAMPLE_DIR / "answers.yaml", ROOT / "profile" / "answers.yaml"],
                         ids=["example", "real"])
def test_answers_template_shape(path):
    if not path.exists():
        pytest.skip("profile/ is personal and git-ignored (absent in CI)")
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert d["schema_version"] == 1
    assert ANSWER_KEYS <= set(d)
    assert {"india", "other_countries"} <= set(d["work_authorization"])
    assert {"inr_lpa", "usd"} <= set(d["expected_ctc"])


def test_example_profile_holds_no_real_contact_data():
    for p in EXAMPLE_DIR.rglob("*"):
        if p.is_file():
            for domain in re.findall(r"[\w.+-]+@([\w.-]+)", p.read_text(encoding="utf-8")):
                assert domain == "example.com", p


# --------------------------------------------------------------------------- CLI

def test_cli_check_ok(capsys):
    assert F.main(["check", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "OK" in out and "facts by status" in out and "confirmed 11" in out


def test_cli_check_fails_on_errors(tmp_path, data, capsys):
    data["experience"][0]["bullets"][0]["status"] = "maybe"
    p = tmp_path / "resume_facts.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert F.main(["check", str(p)]) == 1
    assert "status 'maybe'" in capsys.readouterr().out


def test_cli_check_missing_and_invalid_files(tmp_path, capsys):
    assert F.main(["check", str(tmp_path / "nope.yaml")]) == 1
    bad = tmp_path / "bad.yaml"
    bad.write_text("person: {name: [secret text\n", encoding="utf-8")
    assert F.main(["check", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "not found" in err and "invalid YAML" in err and "secret text" not in err


def test_cli_default_path_uses_profile_dir_env(tmp_path, monkeypatch):
    shutil.copytree(EXAMPLE_DIR, tmp_path / "p")
    monkeypatch.setenv("JOBCOPILOT_PROFILE_DIR", str(tmp_path / "p"))
    assert F.default_facts_path() == tmp_path / "p" / "resume_facts.yaml"
    assert F.main(["check"]) == 0


def test_cli_review_lists_only_unconfirmed(capsys):
    assert F.main(["review", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert re.search(r"^\s+\d+\. exp\.acme\.b3 — Mentored", out, re.M)
    assert "proj.widgetbot.b1.ecs" in out and "pending until phase-3b" in out
    assert "exp.acme.b1 —" not in out                  # confirmed, no open claims
    assert "proj.widgetbot.b1 —" not in out            # confirmed; only its claim is listed
    numbers = [int(n) for n in re.findall(r"^\s+(\d+)\. ", out, re.M)]
    assert numbers == list(range(1, len(numbers) + 1)) and len(numbers) == 7 + 2 + 3


def test_module_entrypoint_exit_codes(tmp_path):
    ok = subprocess.run([sys.executable, "-m", "jobcopilot.facts", "check", str(EXAMPLE)],
                        cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    bad = tmp_path / "resume_facts.yaml"
    bad.write_text("schema_version: 1\nperson: {name: X}\neducation: [{id: edu.x}]\n",
                   encoding="utf-8")
    fail = subprocess.run([sys.executable, "-m", "jobcopilot.facts", "check", str(bad)],
                          cwd=ROOT, capture_output=True, text=True)
    assert fail.returncode == 1
