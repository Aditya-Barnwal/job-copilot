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

ANSWER_SECTIONS = {"personal", "current_address", "permanent_address", "education", "employment",
                   "compensation", "availability", "location_preferences", "work_authorization",
                   "screening_defaults", "voluntary_disclosures", "writing"}


@pytest.mark.parametrize("path", [EXAMPLE_DIR / "answers.yaml", ROOT / "profile" / "answers.yaml"],
                         ids=["example", "real"])
def test_answers_template_shape(path):
    """answers.yaml schema v2 (jobcopilot/answers.py): sections + required keys.
    Keys only — values are never asserted or printed."""
    if not path.exists():
        pytest.skip("profile/ is personal and git-ignored (absent in CI)")
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert d["schema_version"] == 2
    assert ANSWER_SECTIONS <= set(d)
    assert {"full_name", "first_name", "last_name", "email", "phone", "links"} <= set(d["personal"])
    assert {"india", "other_countries", "requires_sponsorship_outside_india"} <= set(d["work_authorization"])
    ec = d["compensation"]["expected_ctc"]
    assert {"inr_floor_lpa", "inr_default_lpa_by_tier", "inr_free_text", "rule_when_range_posted"} <= set(ec)
    assert set(ec["inr_default_lpa_by_tier"]) == {"brand", "funded", "watchlist", "default"}
    assert {"notice_period", "notice_period_days", "earliest_start"} <= set(d["availability"])
    assert {"how_did_you_hear", "previously_worked_at", "previously_interviewed_with"} <= set(d["screening_defaults"])
    assert "signoff" in d["writing"]
    assert isinstance(d["education"], list) and d["education"]


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


# --------------------------------------------------------------------------- fail-closed regressions
# Review findings #13 #14 #18 #19 #20 #21 #22 #29 #30: usable_facts() must never
# return wording from an unconfirmed/pending claim, and validate() must say why
# a fact was left out.

def usable_ids(d: dict, base: str | None = None) -> dict[str, F.Fact]:
    return {f.id: f for f in F.usable_facts(F.parse(copy.deepcopy(d)), base=base)}


def widget_b1(d: dict) -> dict:
    return d["projects"][0]["bullets"][0]


def test_claim_span_occurring_twice_is_rejected_and_fact_dropped(data):   # 13
    b = widget_b1(data)
    b["text"] = ("Built a FastAPI chat backend on AWS ECS that answers 5 kinds of widget "
                 "question; autoscaled on AWS ECS.")
    del b["variants"]
    b["skills"] = ["FastAPI"]
    b["claims"][0]["text"] = "AWS ECS"
    assert has_error(errors_for(data), "proj.widgetbot.b1.ecs", "2 times")
    u = usable_ids(data)
    assert "proj.widgetbot.b1" not in u
    assert all("ECS" not in f.text for f in u.values() if f.id.startswith("proj.widgetbot"))


def test_claim_variant_span_occurring_twice_is_rejected(data):            # 13
    b = widget_b1(data)
    b["variants"]["ml_ai"] += " Also deployed on AWS ECS."
    b["claims"][0]["variants"] = {"ml_ai": "deployed on AWS ECS"}
    assert has_error(errors_for(data), "proj.widgetbot.b1.ecs", "'ml_ai' variant", "2 times")
    assert "proj.widgetbot.b1" not in usable_ids(data, base="ml_ai")


@pytest.mark.parametrize("empty", ["", None])
def test_empty_or_null_claim_variant_is_rejected_and_fact_dropped(data, empty):   # 14
    widget_b1(data)["claims"][0]["variants"] = {"ml_ai": empty}
    assert has_error(errors_for(data), "proj.widgetbot.b1.ecs", "'ml_ai'", "empty")
    for base in (None, "ml_ai", "backend_sde"):
        u = usable_ids(data, base)
        assert "proj.widgetbot.b1" not in u, base


def test_empty_claim_text_never_validates(data):                          # 14
    c = widget_b1(data)["claims"][0]
    c["text"] = None
    assert has_error(errors_for(data), "proj.widgetbot.b1.ecs", "exactly one")
    assert "proj.widgetbot.b1" not in usable_ids(data)


DUP_CLAIMS_YAML = """\
schema_version: 1
person: {name: Alex Example}
projects:
  - id: proj.w
    name: W
    text: "W"
    sources: [backend_sde]
    status: confirmed
    bullets:
      - id: proj.w.b1
        text: "Built a FastAPI chat backend with a Redis cache; deployed on AWS ECS."
        sources: [backend_sde]
        status: confirmed
        claims:
          - {id: proj.w.b1.ecs, text: "; deployed on AWS ECS", status: pending,
             pending_until: phase-3b, pending_reason: not yet}
        claims:
          - {id: proj.w.b1.redis, text: " with a Redis cache", status: confirmed}
"""


def test_duplicate_yaml_keys_are_rejected(tmp_path, capsys):              # 18
    p = tmp_path / "resume_facts.yaml"
    p.write_text(DUP_CLAIMS_YAML, encoding="utf-8")
    with pytest.raises(F.FactsError, match=r"duplicate key .*line 17"):
        F.load(p)
    assert F.main(["check", str(p)]) == 1
    err = capsys.readouterr().err
    assert "duplicate key" in err and "Redis" not in err and "ECS" not in err


def test_duplicate_keys_in_bases_file_are_rejected(tmp_path):            # 18
    p = tmp_path / "bases.yaml"
    p.write_text("bases:\n  a: {roles: [x]}\n  a: {roles: [y]}\n", encoding="utf-8")
    with pytest.raises(F.FactsError, match="duplicate key"):
        F.load_bases(p)


def test_yaml_merge_keys_are_not_duplicates(tmp_path):
    p = tmp_path / "bases.yaml"
    p.write_text("x: &d {roles: [a], pdf: a.pdf}\nbases:\n  b:\n    <<: *d\n    pdf: b.pdf\n",
                 encoding="utf-8")
    assert F.load_bases(p)["b"].pdf == "b.pdf"


def role_with_pending_title(d: dict, span: str = "Senior") -> dict:
    r = d["experience"][0]
    r["text"] = "Acme Widgets Inc. | Springfield | Senior Software Engineer | Jan 2024 – Present"
    r["title"] = "Senior Software Engineer"
    r["claims"] = [{"id": "exp.acme.senior", "text": span, "status": "pending",
                    "pending_until": "phase-3", "pending_reason": "not official yet"}]
    return r


def test_pending_claim_is_cut_from_header_fields_too(data):               # 19
    role_with_pending_title(data)
    assert errors_for(data) == []
    acme = usable_ids(data)["exp.acme"]
    assert "Senior" not in acme.text and acme.title == "Software Engineer"
    assert acme.text == "Acme Widgets Inc. | Springfield | Software Engineer | Jan 2024 – Present"


def test_claim_across_a_header_field_boundary_drops_the_header(data):     # 19
    role_with_pending_title(data, span="Springfield | Senior")
    assert has_error(errors_for(data), "exp.acme", "`title`")
    u = usable_ids(data)
    assert "exp.acme" not in u and "exp.acme.b1" not in u


def test_claim_emptying_a_required_header_field_drops_the_header(data):   # 19
    role_with_pending_title(data, span="Senior Software Engineer")
    assert has_error(errors_for(data), "exp.acme", "`title`")
    assert "exp.acme" not in usable_ids(data)


def test_header_field_not_in_text_is_never_usable(data):                 # 19 / 21
    data["experience"][0]["title"] = "Senior Software Engineer"
    assert has_error(errors_for(data), "exp.acme", "`title` does not appear")
    u = usable_ids(data)
    assert "exp.acme" not in u and "exp.acme.b1" not in u


def test_overlapping_claims_are_rejected_and_cut_as_a_union(data):        # 20
    b = widget_b1(data)
    b["text"] = ("Built a FastAPI chat backend that answers 5 kinds of widget question, "
                 "deployed on AWS ECS with Terraform.")
    del b["variants"]
    b["skills"] = ["FastAPI"]
    pend = {"status": "pending", "pending_until": "phase-3b", "pending_reason": "x"}
    b["claims"] = [{"id": "proj.widgetbot.b1.ecs", "text": "AWS ECS", **pend},
                   {"id": "proj.widgetbot.b1.tf", "text": ", deployed on AWS ECS with Terraform",
                    **pend}]
    assert has_error(errors_for(data), "proj.widgetbot.b1.ecs", "proj.widgetbot.b1.tf", "overlap")
    text = F._build_usable(F.parse(data).by_id()["proj.widgetbot.b1"], None).text
    assert text == "Built a FastAPI chat backend that answers 5 kinds of widget question."


def test_usable_fails_closed_when_claim_span_drifts(data):                # 21
    b = widget_b1(data)
    b["text"] = b["text"].replace("deployed on", "deployed to")
    errs = errors_for(data)
    assert has_error(errs, "proj.widgetbot.b1.ecs", "span")
    assert "proj.widgetbot.b1" not in usable_ids(data)


def test_usable_fails_closed_when_tech_claim_item_drifts(data):           # 21
    data["projects"][0]["tech"] = ["Python", "FastAPI", "Docker", "ECS"]
    assert has_error(errors_for(data), "proj.widgetbot.tech_ecs", "'AWS ECS'")
    u = usable_ids(data)
    assert "proj.widgetbot" not in u and "proj.widgetbot.b1" not in u


def test_bullets_need_their_header_usable_for_the_same_base(data):        # 21
    data["experience"][0]["sources"] = ["backend_sde"]
    for b in data["experience"][0]["bullets"]:
        b["sources"] = ["backend_sde"] if b["id"] == "exp.acme.b3" else ["backend_sde", "ml_ai"]
    data["experience"][0]["bullets"][1]["variants"] = {}
    u = usable_ids(data, base="ml_ai")
    assert "exp.acme" not in u
    assert not [i for i in u if i.startswith("exp.acme.")]


def test_bullet_of_a_header_without_id_is_not_usable(data):               # 21
    data["experience"][0]["id"] = ""
    assert not [i for i in usable_ids(data) if i.startswith("exp.acme")]


def test_duplicated_ids_are_never_usable(data):                            # 21
    data["skills"]["languages"][1]["id"] = "skill.python"
    u = usable_ids(data)
    assert "skill.python" not in u


def test_malformed_claims_block_drops_the_fact(data):                     # 21
    widget_b1(data)["claims"] = "; deployed on AWS ECS"
    assert has_error(errors_for(data), "claims", "must be a list")
    assert "proj.widgetbot.b1" not in usable_ids(data)
    widget_b1(data)["claims"] = ["; deployed on AWS ECS"]
    assert has_error(errors_for(data), "claims[0]", "mapping")
    assert "proj.widgetbot.b1" not in usable_ids(data)


def test_pending_tech_in_header_text_is_rejected_and_dropped(data):       # 22
    p = data["projects"][0]
    p["text"] = "WidgetBot — Example Chat Assistant | Python, FastAPI, Docker, AWS ECS"
    p["skills"] = ["Docker", "AWS ECS"]
    assert has_error(errors_for(data), "proj.widgetbot", "'Docker'", "text")
    u = usable_ids(data)
    assert "proj.widgetbot" not in u and "proj.widgetbot.b1" not in u


def test_pending_tech_covered_by_a_text_claim_is_cut(data):               # 22
    p = data["projects"][0]
    p["text"] = "WidgetBot — Example Chat Assistant | Python, FastAPI, Docker, AWS ECS"
    p["claims"].append({"id": "proj.widgetbot.tech_text", "text": ", Docker, AWS ECS",
                        "status": "pending", "pending_until": "phase-3b", "pending_reason": "x"})
    assert errors_for(data) == []
    w = usable_ids(data)["proj.widgetbot"]
    assert w.text == "WidgetBot — Example Chat Assistant | Python, FastAPI"
    assert w.tech == ["Python", "FastAPI"]


def test_ordered_tech_keeps_only_usable_items(bases):                     # 22
    w = {f.id: f for f in F.usable_facts(F.load(EXAMPLE), base="ml_ai")}["proj.widgetbot"]
    assert F.ordered_tech(w, bases["ml_ai"]) == ["Python", "FastAPI"]
    full = F.load(EXAMPLE).by_id()["proj.widgetbot"]
    assert F.ordered_tech(full, bases["ml_ai"]) == ["Python", "Docker", "AWS ECS", "FastAPI"]
    assert F.ordered_tech(w, None) == ["Python", "FastAPI"]


def test_cut_only_tidies_the_seam(data):                                   # 29
    b = widget_b1(data)
    b["text"] = ("Built a chat backend in .NET (C#) that answers 5 kinds of widget question ;  "
                 "really ; deployed on AWS ECS.")
    del b["variants"]
    b["skills"] = [".NET", "C#"]
    assert errors_for(data) == []
    b1 = usable_ids(data)["proj.widgetbot.b1"]
    assert b1.text == ("Built a chat backend in .NET (C#) that answers 5 kinds of widget question ;  "
                       "really.")
    assert b1.skills == [".NET", "C#"]


def test_cut_tidies_both_sides_of_the_seam():
    assert F._cut("AWS (Lambda, ECS)", ", ECS") == "AWS (Lambda)"
    assert F._cut("AWS (ECS) and S3", "ECS") == "AWS and S3"
    assert F._cut("Go, Python, Rust.", "Rust") == "Go, Python."
    assert F._cut("Lead Senior Engineer", "Senior") == "Lead Engineer"
    assert F._cut("x", "y") is None and F._cut("a b a b", "a b") is None


def test_skill_terms_treat_plus_hash_dot_as_term_characters(data):        # 30
    b = data["experience"][0]["bullets"][0]
    b["text"] = "Rewrote the C++ pricing engine in ASP.NET, improving throughput by 150%."
    b["skills"] = ["C", "NET"]
    b["metrics"] = ["50%"]
    errs = errors_for(data)
    assert has_error(errs, "exp.acme.b1", "skill tag 'C'")
    assert has_error(errs, "exp.acme.b1", "skill tag 'NET'")
    assert has_error(errs, "exp.acme.b1", "metric '50%'")
    b1 = usable_ids(data)["exp.acme.b1"]
    assert b1.skills == [] and b1.metrics == []


def test_terms_still_match_at_sentence_end_and_with_separators():
    assert F._has_term("Built it in Go.", "Go")
    assert F._has_term("Used C/C++ and C#.", "C") and F._has_term("Used C/C++.", "C++")
    assert F._has_term("Ported to Node.js.", "Node.js")
    assert not F._has_term("Ported to Node.js.", "Node")
    assert F._has_term("GPA: 3.7", "3.7") and not F._has_term("GPA: 13.7", "3.7")
    assert not F._has_term("GPA: 3.75", "3.7")
    assert F._has_term("by 50%.", "50%") and not F._has_term("by 150%.", "50%")
    assert not F._has_term("500+ users", "500") and F._has_term("500+ users", "500+")
    assert not F._has_term("in C#", "C") and not F._has_term("CI/CD", "C")


def test_tech_claim_with_variant_spans_is_rejected_and_dropped(data):     # 14 / 21
    p = data["projects"][0]
    p["variants"] = {"ml_ai": "WidgetBot — Example Chat Assistant on ECS"}
    p["claims"][0]["variants"] = {"ml_ai": " on ECS"}
    assert has_error(errors_for(data), "proj.widgetbot.tech_ecs", "`variants` needs a `text`")
    assert "proj.widgetbot" not in usable_ids(data, base="ml_ai")


def test_validate_reports_every_fact_usable_facts_leaves_out():
    # Fail-closed omissions are never silent: a valid file loses no confirmed fact
    # except for an untrusted header or text that is entirely pending.
    facts = F.load(EXAMPLE)
    assert F.validate(facts) == []
    u = {f.id for f in F.usable_facts(facts)}
    for f in facts.facts():
        header = facts.by_id().get(f.parent)
        if f.confirmed and (not header or header.confirmed):
            assert f.id in u, f.id
