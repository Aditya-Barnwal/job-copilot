"""Fit score: profile loading, each component's edges, weights, end-to-end ordering.

Never loads the embedding model: semantic is disabled or a fake encoder is injected.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobcopilot import fit

EXAMPLE = Path(__file__).resolve().parent.parent / "profile.example" / "resume_facts.yaml"
NOSEM = {"semantic": False}


@pytest.fixture(scope="module")
def prof() -> fit.Profile:
    return fit.load_profile(EXAMPLE, cfg={"filters": {}})


def mk(**kw) -> dict:
    base = {"title": "Software Engineer", "description_text": "", "location_text": "",
            "country_bucket": "unknown", "workplace_type": "unknown"}
    base.update(kw)
    return base


def comp(job, prof, name, **kw):
    return fit.score(job, profile=prof, cfg=kw.pop("cfg", NOSEM), **kw).components[name]


# ------------------------------------------------------------------ profile

def test_profile_uses_confirmed_and_unconfirmed_never_pending(prof):
    # confirmed: Python, Go, PostgreSQL, FastAPI, REST API; unconfirmed: PyTorch, OpenCV, Flutter
    assert {"Python", "Go", "PostgreSQL", "FastAPI", "REST APIs", "PyTorch", "OpenCV", "Flutter",
            "AWS", "AWS Lambda"} <= prof.direct_skills
    # pending skill fact (Docker) and pending claims (Docker / AWS ECS / ", ECS") stay out
    assert "Docker" not in prof.skills and "AWS ECS" not in prof.skills
    assert "SQL" in prof.skills and "Machine Learning" in prof.skills      # implied
    assert "deployed on AWS ECS" not in prof.resume_text
    assert "grounding check" not in prof.resume_text                     # pending bullet
    assert "Trained a churn model" in prof.resume_text                   # unconfirmed header -> scoring-usable


def test_profile_config(prof, tmp_path, monkeypatch):
    assert prof.user_yoe == 1 and "bengaluru" in prof.preferred_cities
    assert prof.role_families == ["backend", "fullstack", "ml", "ai", "sde"]
    p = fit.load_profile(EXAMPLE, cfg={"filters": {"yoe": {"user_yoe": 2},
                                                   "roles": {"families": ["data engineer", "ml engineer"]}}})
    assert p.user_yoe == 2 and p.role_families == ["data_engineering", "ml"]


def test_missing_facts_file_degrades(tmp_path):
    p = fit.load_profile(tmp_path / "nope.yaml", cfg={"filters": {}})
    assert p.skills == set()
    r = fit.score(mk(description_text="Requirements: Java"), profile=p, cfg=NOSEM)
    assert r.components["skills"] == 0 and r.missing_skills == ["Java"]


# ------------------------------------------------------------------- skills

def test_skills_must_nice_weighting(prof):
    jd = "Requirements:\n- Go\n- Kafka\nNice to have:\n- PostgreSQL\n- Rust"
    r = fit.score(mk(description_text=jd), profile=prof, cfg=NOSEM)
    assert r.components["skills"] == pytest.approx(100 * (0.7 * 0.5 + 0.3 * 0.5))
    assert r.matched_skills == ["Go", "PostgreSQL"] and r.missing_skills == ["Kafka", "Rust"]


def test_skills_synonyms_count(prof):
    r = fit.score(mk(description_text="Must have: Postgres, golang, RESTful services"), profile=prof, cfg=NOSEM)
    assert r.components["skills"] == 100 and set(r.matched_skills) == {"PostgreSQL", "Go", "REST APIs"}


def test_skills_none_listed_is_neutral_and_flagged(prof):
    r = fit.score(mk(description_text="Join a great team."), profile=prof, cfg=NOSEM)
    assert r.components["skills"] == 50 and "low_confidence_skills" in r.flags


def test_top_n_and_must_first(prof):
    jd = ("Requirements: " + ", ".join(["Java", "Kafka", "Rust", "Scala", "Ruby", "PHP", "Elixir",
                                         "Haskell", "Kotlin"]) + "\nBonus: Python")
    r = fit.score(mk(description_text=jd), profile=prof, cfg=NOSEM)
    assert len(r.missing_skills) == 8 and r.missing_skills[0] == "Java"
    assert r.matched_skills == ["Python"]


# --------------------------------------------------------------------- role

@pytest.mark.parametrize("title, fam, pts", [
    ("Backend Engineer", "backend", 100), ("Full-Stack Developer", "fullstack", 100),
    ("Machine Learning Engineer", "ml", 100), ("GenAI Engineer", "ai", 100),
    ("Software Engineer", "sde", 85), ("Data Engineer", "data_engineering", 70),
    ("Frontend Engineer", "frontend", 55), ("QA Engineer", "qa", 35), ("Account Executive", "other", 20),
])
def test_role(prof, title, fam, pts):
    r = fit.score(mk(title=title), profile=prof, cfg=NOSEM)
    assert r.role_family == fam and r.components["role"] == pts


# ---------------------------------------------------------------- seniority

@pytest.mark.parametrize("job, pts", [
    (mk(description_text="Open to freshers and new grads."), 100),
    (mk(min_yoe_required=0), 100),
    (mk(min_yoe_required=1), 100),
    (mk(min_yoe_required=2), 85),
    (mk(min_yoe_required=3, yoe_is_preferred=True), 60),
    (mk(min_yoe_required=5, yoe_is_preferred=True), 40),
    (mk(min_yoe_required=3), 20),
    (mk(), 70),
    (mk(title="Associate Software Engineer"), 95),
    (mk(title="Senior Backend Engineer", min_yoe_required=1), 15),
    (mk(title="Software Engineering Intern"), 50),
])
def test_seniority(prof, job, pts):
    assert comp(job, prof, "seniority") == pts


def test_seniority_uses_decision(prof):
    d = SimpleNamespace(yoe_min=2, yoe_is_preferred=False, fresher_friendly=True)
    assert comp(mk(min_yoe_required=5), prof, "seniority", decision=d) == 100
    d = {"yoe_min": 3, "yoe_is_preferred": True, "fresher_friendly": False}
    assert comp(mk(), prof, "seniority", decision=d) == 60


# ----------------------------------------------------------------- location

@pytest.mark.parametrize("job, pts", [
    (mk(location_text="Bengaluru, Karnataka", country_bucket="IN", workplace_type="hybrid"), 100),
    (mk(location_text="Bangalore / Hyderabad", country_bucket="IN", workplace_type="onsite"), 100),
    (mk(location_text="Remote - India", country_bucket="IN", workplace_type="remote"), 90),
    (mk(location_text="Hyderabad", country_bucket="IN", workplace_type="onsite"), 85),
    (mk(location_text="Mysuru", country_bucket="IN", workplace_type="onsite"), 75),
    (mk(location_text="Remote", country_bucket="non-IN", workplace_type="remote",
        region_restrictions=["APAC"]), 90),
    (mk(location_text="Remote", country_bucket="non-IN", workplace_type="remote"), 55),
    (mk(location_text="Remote - US only", country_bucket="non-IN", workplace_type="remote"), 10),
    (mk(location_text="Berlin", country_bucket="non-IN", workplace_type="onsite"), 10),
    (mk(), 60),
])
def test_location(prof, job, pts):
    assert comp(job, prof, "location") == pts


def test_location_decision_overrides(prof):
    j = mk(location_text="Remote", country_bucket="non-IN", workplace_type="remote")
    assert comp(j, prof, "location", decision={"india_eligible": True}) == 90
    assert comp(j, prof, "location", decision={"india_eligible": False}) == 10


# ------------------------------------------------------------------ company

@pytest.mark.parametrize("company, pts", [
    ({"tier": "brand"}, 100), ({"tier": "funded"}, 90), ({"tier": "funded", "funding_stage": "Series A"}, 90),
    ({"tier": "funded", "funding_stage": "Series B"}, 95), ({"tier": "funded", "yc_batch": "Winter 2023"}, 95),
    ({"tier": "funded", "yc_batch": "W23", "yc_top_company": True}, 98),
    ({"tier": "watchlist"}, 60), ({"tier": "unknown"}, 40), (None, 50),
])
def test_company(prof, company, pts):
    assert comp(mk(), prof, "company", company=company) == pts


def test_company_from_job_keys(prof):
    assert comp(mk(company_tier="brand"), prof, "company") == 100


# ------------------------------------------------------------------- salary

@pytest.mark.parametrize("job, pts", [
    (mk(), 50),
    (mk(comp_currency="INR", comp_min=1_500_000, comp_max=1_500_000, comp_interval="1 YEAR"), 75),
    (mk(comp_currency="INR", comp_min=3_000_000, comp_max=3_000_000), 100),
    (mk(comp_currency="INR", comp_min=2_000_000, comp_max=2_500_000), pytest.approx(75 + 25 * 0.5)),
    (mk(comp_currency="INR", comp_min=800_000, comp_max=1_000_000), 20),
    (mk(comp_currency="USD", comp_min=10_000, comp_max=10_000, comp_interval="1 MONTH"), 100),
    (mk(comp_currency="USD", comp_min=10, comp_max=10, comp_interval="1 HOUR"), 20),
    (mk(comp_currency="EUR", comp_min=42_000, comp_max=42_000), pytest.approx(87.5)),
    (mk(compensation={"currency": "USD", "min_value": 60_000, "max_value": 60_000, "interval": "1 YEAR"}), 100),
    (mk(comp_currency="JPY", comp_min=5_000_000, comp_max=5_000_000), 50),
])
def test_salary(prof, job, pts):
    assert comp(job, prof, "salary", cfg={"semantic": False, "filters": {}}) == pts


def test_salary_from_decision(prof):
    assert comp(mk(), prof, "salary", cfg={"semantic": False, "filters": {}},
                decision={"salary_annual_inr": 3_000_000}) == 100


# ---------------------------------------------------------------- freshness

@pytest.mark.parametrize("age, pts", [(0, 100), (7, 100), (8, 90), (30, 75), (45, 50), (61, 30), (None, 60)])
def test_freshness(prof, age, pts):
    assert comp(mk(posted_age_days=age), prof, "freshness") == pts


def test_freshness_from_posted_at(prof):
    now = datetime(2026, 11, 12, tzinfo=timezone.utc)
    assert comp(mk(posted_at="2026-11-01T00:00:00Z"), prof, "freshness", now=now) == 90
    assert comp(mk(posted_at="garbage"), prof, "freshness") == 60


# ---------------------------------------------------- semantic + weights

class FakeEncoder:
    """Bag-of-words over a tiny vocabulary; no model."""
    VOCAB = ["python", "go", "postgresql", "ruby", "rails", "backend", "fastapi"]

    def __init__(self):
        self.calls = 0

    def __call__(self, text):
        self.calls += 1
        t = text.lower()
        return [float(t.count(w)) + 0.01 for w in self.VOCAB]


def test_semantic_with_fake_encoder(prof):
    enc = FakeEncoder()
    j = mk(description_text="Backend in Python, Go and PostgreSQL with FastAPI.")
    r = fit.score(j, profile=prof, cfg={"semantic": True}, encoder=enc)
    assert "semantic" in r.components and r.components["semantic"] > 50
    assert enc.calls >= 2 and "semantic_skipped" not in r.flags
    assert sum(r.weights.values()) == pytest.approx(1.0, abs=1e-3) and r.weights["semantic"] == pytest.approx(0.10)


def test_semantic_disabled_renormalises(prof):
    r = fit.score(mk(description_text="Python"), profile=prof, cfg=NOSEM)
    assert "semantic" not in r.components and "semantic_skipped" in r.flags
    assert sum(r.weights.values()) == pytest.approx(1.0, abs=1e-3)
    assert r.weights["skills"] == pytest.approx(0.30 / 0.90, abs=1e-3)
    assert r.score == pytest.approx(sum(r.components[k] * r.weights[k] for k in r.components), abs=0.2)


def test_model_unavailable_skips(prof, monkeypatch):
    def boom():
        raise ImportError("no model here")
    monkeypatch.setattr("jobcopilot.scoring.get_model", boom)
    r = fit.score(mk(description_text="Python"), profile=prof, cfg={"semantic": True})
    assert "semantic" not in r.components and "semantic_skipped" in r.flags


def test_normalise_weights_edges():
    assert fit.normalise_weights({"skills": 0, "role": 0}, {"skills", "role"}) == {"skills": 0.5, "role": 0.5}
    w = fit.normalise_weights({"skills": 3, "role": 1, "semantic": 6}, {"skills", "role"})
    assert w == {"skills": 0.75, "role": 0.25}


def test_custom_weights_from_cfg(prof):
    r = fit.score(mk(title="Backend Engineer"), profile=prof,
                  cfg={"semantic": False, "weights": {k: 0 for k in fit.COMPONENTS} | {"role": 1}})
    assert r.score == 100 and r.weights["role"] == 1


def test_load_config_file(tmp_path):
    p = tmp_path / "scoring.yaml"
    p.write_text("weights: {skills: 0.5}\nsemantic: false\n")
    c = fit.load_config(p)
    assert c["weights"]["skills"] == 0.5 and c["weights"]["role"] == 0.15 and c["semantic"] is False
    repo_cfg = fit.load_config(Path(__file__).resolve().parent.parent / "config" / "scoring.yaml")
    assert repo_cfg["weights"] == fit.DEFAULTS["weights"]


# ------------------------------------------------------------- end to end

BLR_JAVA = mk(title="Backend Engineer (SDE I)", location_text="Bengaluru, India", country_bucket="IN",
              workplace_type="hybrid", posted_age_days=2, min_yoe_required=1,
              description_text="Requirements:\n- Java, Spring Boot, PostgreSQL\n- REST APIs\n"
                               "Nice to have: Kafka, GCP")
US_RUBY = mk(title="Senior Ruby on Rails Engineer", location_text="Remote - US only", country_bucket="non-IN",
             workplace_type="remote", posted_age_days=40, min_yoe_required=5,
             description_text="Requirements: Ruby on Rails, Sidekiq, PostgreSQL. 5+ years experience.")
REMOTE_PY = mk(title="Software Engineer, Backend", location_text="Remote (APAC)", country_bucket="non-IN",
               workplace_type="remote", region_restrictions=["APAC"], posted_age_days=10,
               description_text="Must have: Python, FastAPI, Go. Bonus: Kubernetes.")


def test_end_to_end_ordering(prof):
    sc = {k: fit.score(j, profile=prof, cfg=NOSEM, company=c).score for k, j, c in [
        ("blr", BLR_JAVA, {"tier": "funded", "yc_batch": "W23"}),
        ("us", US_RUBY, {"tier": "brand"}),
        ("apac", REMOTE_PY, {"tier": "funded", "funding_stage": "Series B"}),
    ]}
    assert sc["blr"] > sc["us"] and sc["apac"] > sc["us"]
    assert sc["us"] < 50


def test_why_sentence(prof):
    j = dict(BLR_JAVA, description_text="Requirements: Go, PostgreSQL, REST APIs\nOpen to new grads.")
    r = fit.score(j, profile=prof, cfg=NOSEM, company={"tier": "funded", "yc_batch": "Winter 2023"})
    assert r.why.startswith("Strong match: Go, PostgreSQL, REST APIs")
    assert "new-grad friendly" in r.why and "Bengaluru" in r.why and "funded (YC W23)" in r.why
    r2 = fit.score(US_RUBY, profile=prof, cfg=NOSEM)
    assert "missing Ruby on Rails" in r2.why and r2.why.startswith("Weak match")


def test_deterministic_and_accepts_job_objects(prof):
    from jobcopilot.models import Job
    job = Job(source="greenhouse", company="Acme", company_sector="tech", external_id="1",
              title="Backend Engineer", location_text="Bengaluru", workplace_type="onsite",
              country_bucket="IN", url="https://example.com/j/1", description_text="Requirements: Go")
    a = fit.score(job, profile=prof, cfg=NOSEM)
    b = fit.score(job.to_dict(), profile=prof, cfg=NOSEM)
    assert a == b and a.components["skills"] == 100 and a.to_dict()["score"] == a.score
