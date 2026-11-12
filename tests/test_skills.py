"""Skill taxonomy: synonyms, whole-term matching, must vs nice split."""
from __future__ import annotations

import pytest

from jobcopilot import scoring
from jobcopilot import skills as S


@pytest.mark.parametrize("text, skill", [
    ("Built on Spring Boot", "Spring Boot"),
    ("springboot microservices", "Spring Boot"),
    ("Java/Spring, Hibernate", "Spring Boot"),
    ("GCP", "GCP"), ("Google Cloud Platform", "GCP"), ("google cloud", "GCP"),
    ("BigQuery and Big Query", "BigQuery"),
    ("Drools", "Drools"), ("writing DRL", "Drools"), ("a rule engine", "Drools"),
    ("LLMs", "LLM"), ("large language models", "LLM"), ("GenAI", "LLM"), ("Generative AI apps", "LLM"),
    ("RAG pipelines", "RAG"), ("FastAPI", "FastAPI"),
    ("Postgres", "PostgreSQL"), ("PostgreSQL", "PostgreSQL"),
    ("k8s", "Kubernetes"), ("Kubernetes", "Kubernetes"),
    ("ML models", "Machine Learning"), ("machine learning", "Machine Learning"),
    ("JS and TS", "JavaScript"), ("JS and TS", "TypeScript"),
    ("C++", "C++"), ("cpp", "C++"), ("CI/CD", "CI/CD"), ("GitHub Actions", "CI/CD"),
    ("Built in Go.", "Go"), ("golang", "Go"), ("C/C++", "C"), ("Node.js", "Node.js"),
])
def test_synonyms(text, skill):
    assert skill in S.find_skills(text)


@pytest.mark.parametrize("text, absent", [
    ("JavaScript only", "Java"),
    ("C++ and C#", "C"), ("Objective-C", "C"), ("Series C funded", "C"), ("CI/CD", "C"),
    ("Go above and beyond. Go-to-market.", "Go"), ("we go to market", "Go"),
    ("Spring 2026 internship", "Spring Boot"), ("PostgreSQL", "SQL"), ("NoSQL", "SQL"),
    ("react to incidents", "React"), ("the rest of the team", "REST APIs"),
    ("React Native apps", "React"), ("at the helm", "Kubernetes"),
    ("Express your ideas", "Express"), ("Swift delivery", "Swift"),
])
def test_whole_term_negatives(text, absent):
    assert absent not in S.find_skills(text)


def test_canonical_and_expand():
    assert S.canonical("springboot") == "Spring Boot"
    assert S.canonical("REST API") == "REST APIs"
    assert S.canonical("AWS ECS") == "AWS ECS"
    assert S.canonical("nonsense-tool") is None
    have = S.expand(["Spring Boot", "PostgreSQL"])
    assert {"Java", "SQL"} <= have


JD = """About the role
You will build services in Kotlin.
Requirements:
- 1+ years with Java and Spring Boot
- Strong SQL skills
- Docker is a plus
Nice to have
- Kubernetes, GCP
Benefits
- Free Python lessons
"""


def test_must_vs_nice_sections():
    h = S.extract(JD)
    assert h.must == ["Java", "Spring Boot", "SQL"]
    # line-level "is a plus" inside Requirements; body-only Kotlin -> nice (a must section exists)
    assert set(h.nice) == {"Docker", "Kubernetes", "GCP", "Kotlin"}
    assert "Python" not in h.all          # benefits section ignored
    assert h.sections_found


def test_inline_headings_in_one_blob():
    h = S.extract("Responsibilities: build APIs in Python. Requirements: Java, Kafka. "
                  "Preferred qualifications: Redis, AWS.")
    assert h.must == ["Java", "Kafka"]
    assert set(h.nice) == {"Redis", "AWS", "Python"}


def test_no_sections_means_body_is_must():
    h = S.extract("We build with Go and Redis; experience with Terraform would be a plus.")
    assert h.must == ["Go", "Redis"] and h.nice == ["Terraform"]
    assert not h.sections_found


def test_skill_line_is_not_swallowed_as_heading():
    h = S.extract("Requirements\nStrong Python skills\nPreferred\nFastAPI")
    assert h.must == ["Python"] and h.nice == ["FastAPI"]


def test_must_anywhere_wins():
    h = S.extract("Must have: Java\nBonus: Java, Rust")
    assert h.must == ["Java"] and h.nice == ["Rust"]


def test_empty():
    assert S.extract("").all == [] and S.find_skills(None) == []


def test_scoring_vocab_extended_and_callers_still_work():
    assert len(scoring.TECH_VOCAB) > 230
    kws = scoring.extract_keywords("Java with Spring MVC, Drools, LangChain and ECS; JavaScript too")
    assert {"java", "spring mvc", "drools", "langchain", "ecs", "javascript"} <= set(kws)
    assert "java" in scoring.extract_keywords("Java") and "java" not in scoring.extract_keywords("JavaScript")
    cov, missing = scoring.compute_coverage(["java", "drools"], ["java"])
    assert cov == 0.5 and missing == ["drools"]


# ------------------------------------------------- Phase 2 review fixes (E11)

def test_e11_bullet_items_are_never_headings():
    h = S.extract("Requirements:\n- Python\n- Experience\n- Nice to have\n- Kafka")
    assert h.must == ["Python", "Kafka"] and h.nice == []


def test_e11_experience_lines_are_not_headings():
    h = S.extract("Requirements:\nExperience with Kafka and Go\nNot required: Rust\nPython")
    assert set(h.must) == {"Kafka", "Go", "Python"}


def test_e11_sentence_case_non_heading_line_is_content():
    # "Strong background in python" is not Title Case and not a heading phrase
    h = S.extract("Nice to have\n- Docker\nStrong backend skills\n- Rust")
    assert set(h.nice) == {"Docker", "Rust"} and h.must == []


def test_e11_any_of_group():
    h = S.extract("Requirements:\n- Python, Java, or Go\n- Kafka")
    assert ("Python", "Java", "Go") in h.must_groups
    assert S.units(h, "must") == [("Python", "Java", "Go"), ("Kafka",)]


def test_e11_employer_named_skill_dropped():
    h = S.extract("Requirements:\n- SQL and Snowflake", company="Snowflake Inc.")
    assert h.must == ["SQL"]


def test_e11_pre_heading_blurb_ignored_when_role_sections_follow():
    jd = ("Acme builds Kafka and Kubernetes tooling for banks.\n"
          "Requirements:\n- Python\n- SQL")
    h = S.extract(jd)
    assert h.must == ["Python", "SQL"] and "Kafka" not in h.all
    # no structure at all: the text is the role
    assert S.extract("We use Kafka and Python.").must == ["Kafka", "Python"]


@pytest.mark.parametrize("text,has", [
    ("Node.js + Express", True), ("Node/Express APIs", True), ("Express.js", True), ("ExpressJS", True),
    ("Express and Node services", True),
    ("Please Express interest via the form", False), ("We use American Express rails", False),
    ("Express your ideas", False),
])
def test_e11_express_only_as_framework(text, has):
    assert ("Express" in S.find_skills(text)) is has
