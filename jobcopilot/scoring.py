"""Scoring + ATS keyword match.

Two pieces:
  1. Cosine similarity between the resume and (title + description) per job,
     surfaced as `match_score` in [0, 100].
  2. A heuristic ATS keyword extractor that does NOT call an LLM (per the
     user's directive for Phase D). It pulls skill-like terms from a curated
     vocabulary that actually appear in the job text; coverage is then computed
     against the resume.

Both numbers, plus the resolved `required_keywords` and `missing_keywords`, are
written back via `store.set_scores`. The required_keywords are logged per job
so extraction quality is visible (it's the noisiest part of the pipeline).
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Optional


log = logging.getLogger("jobcopilot.scoring")


# ---------------------------------------------------------------------------
# Embedding model: lazy singleton so import doesn't pay for it.
# ---------------------------------------------------------------------------

_MODEL = None
_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def get_model():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        log.info("loading embedding model %s", _MODEL_NAME)
        _MODEL = SentenceTransformer(_MODEL_NAME)
    return _MODEL


def cosine_match_score(resume_vec, job_vec) -> float:
    """Cosine similarity → score in [0, 100], clamped."""
    import numpy as np
    a = np.asarray(resume_vec).reshape(-1)
    b = np.asarray(job_vec).reshape(-1)
    denom = float((np.linalg.norm(a) * np.linalg.norm(b)) or 1.0)
    cos = float(np.dot(a, b) / denom)
    # Map [-1, 1] -> [0, 100] but clamp; resume/JD cosines almost always
    # land in [0.2, 0.7] for MiniLM, so a bare percentage of cosine is fine.
    return max(0.0, min(100.0, cos * 100.0))


# ---------------------------------------------------------------------------
# Heuristic keyword extractor.
#
# Curated vocabulary of skill-like terms. Multi-word phrases (e.g. "machine
# learning") and single tokens (e.g. "python") are both supported. Matching
# is case-insensitive, on word boundaries for single tokens, and substring for
# multi-word phrases (already implicitly word-bounded by the surrounding
# spaces).
#
# This list is intentionally biased toward SWE / ML / data roles since that
# matches the user's target_roles. Add to it as you see gaps in real ingests.
# ---------------------------------------------------------------------------

TECH_VOCAB: list[str] = [
    # languages
    "python", "java", "kotlin", "scala", "go", "golang", "rust", "c++", "c#",
    "javascript", "typescript", "swift", "objective-c", "ruby", "php",
    "r", "matlab", "sql", "bash", "shell", "perl",
    # web / frontend
    "react", "react.js", "vue", "vue.js", "angular", "next.js", "svelte",
    "html", "css", "tailwind", "sass", "redux", "graphql",
    # backend / frameworks
    "django", "flask", "fastapi", "node", "node.js", "express", "spring",
    "spring boot", ".net", "rails", "laravel", "gin", "grpc", "rest",
    "microservices",
    # data / databases
    "postgres", "postgresql", "mysql", "mariadb", "sqlite", "mongodb",
    "redis", "cassandra", "dynamodb", "elasticsearch", "snowflake",
    "bigquery", "redshift", "clickhouse", "kafka", "rabbitmq", "spark",
    "hadoop", "hive", "airflow", "dbt", "etl", "data warehouse",
    "data pipeline", "data modeling",
    # ml / ai
    "machine learning", "deep learning", "nlp", "natural language processing",
    "computer vision", "transformers", "pytorch", "tensorflow", "keras",
    "scikit-learn", "sklearn", "xgboost", "lightgbm", "hugging face",
    "huggingface", "llm", "rag", "fine-tuning", "fine tuning", "embeddings",
    "mlops", "feature engineering", "model serving", "vector database",
    # cloud / devops
    "aws", "amazon web services", "gcp", "google cloud", "azure", "docker",
    "kubernetes", "k8s", "terraform", "ansible", "jenkins", "circleci",
    "github actions", "gitlab ci", "helm", "prometheus", "grafana",
    "datadog", "splunk", "ci/cd", "linux", "unix",
    # general SWE
    "git", "agile", "scrum", "tdd", "code review", "system design",
    "distributed systems", "api design", "oauth", "jwt", "rest api",
    "websocket", "kafka streams", "event-driven", "design patterns",
    # data science
    "pandas", "numpy", "jupyter", "tableau", "power bi", "looker", "matplotlib",
    "seaborn", "plotly", "statistics", "experimentation", "a/b testing",
]


# Tokens short enough to need word-boundary matching (avoid false positives
# inside other words). All entries lowercased.
_SHORT_TOKENS = {
    "r", "go", "k8s", "sql", "aws", "gcp", "llm", "rag", "nlp", "tdd",
    "ci/cd", "rest", "git", "node", "c#", "c++",
}


def _has_term(text: str, term: str) -> bool:
    t = text.lower()
    term_low = term.lower()
    if term_low in _SHORT_TOKENS or " " not in term_low:
        # word-boundary match (allowing # + + . inside the term itself)
        pattern = r"(?<![A-Za-z0-9])" + re.escape(term_low) + r"(?![A-Za-z0-9])"
        return re.search(pattern, t) is not None
    return term_low in t


def extract_keywords(text: str, vocab: Iterable[str] = TECH_VOCAB) -> list[str]:
    """Return the set of vocab terms that appear in `text` (sorted, dedup)."""
    if not text:
        return []
    seen: set[str] = set()
    for term in vocab:
        if _has_term(text, term):
            seen.add(term.lower())
    return sorted(seen)


def compute_coverage(required: list[str], resume_keywords: list[str]) -> tuple[float, list[str]]:
    """Return (coverage_0_to_1, missing_keywords)."""
    if not required:
        return 0.0, []
    req = set(k.lower() for k in required)
    have = set(k.lower() for k in resume_keywords)
    matched = req & have
    missing = sorted(req - have)
    return len(matched) / len(req), missing


# ---------------------------------------------------------------------------
# Orchestrator: score every job in the DB against the stored resume.
# ---------------------------------------------------------------------------

def score_keys(keys: list[str], db_path: Optional[str] = None) -> dict:
    """Score (or re-score) just the given job keys."""
    from . import store

    if not keys:
        return {"scored": 0, "skipped": 0}
    resume = store.get_resume() if db_path is None else store.get_resume(db_path)
    if not resume or not resume.get("text"):
        return {"scored": 0, "skipped": len(keys), "error": "no resume uploaded"}

    resume_text = resume["text"]
    resume_keywords = extract_keywords(resume_text)
    model = get_model()
    resume_vec = model.encode(resume_text, normalize_embeddings=True)

    scored = 0
    for k in keys:
        j = store.get_job(k) if db_path is None else store.get_job(k, db_path=db_path)
        if not j:
            continue
        text = (j.get("title") or "") + "\n" + (j.get("description_text") or "")
        if not text.strip():
            continue
        job_vec = model.encode(text, normalize_embeddings=True)
        score = cosine_match_score(resume_vec, job_vec)
        required = extract_keywords(text)
        coverage, missing = compute_coverage(required, resume_keywords)
        if db_path is None:
            store.set_scores(k, score, coverage, required, missing)
        else:
            store.set_scores(k, score, coverage, required, missing, db_path=db_path)
        scored += 1
    return {"scored": scored, "resume_keyword_count": len(resume_keywords)}


def score_all(db_path: Optional[str] = None) -> dict:
    """Embed resume + every job; compute match_score, ats_coverage, keywords.

    Returns a small summary dict.
    """
    from . import store

    resume = store.get_resume() if db_path is None else store.get_resume(db_path)
    if not resume or not resume.get("text"):
        return {"scored": 0, "skipped": 0, "error": "no resume uploaded"}

    resume_text = resume["text"]
    resume_keywords = extract_keywords(resume_text)
    log.info("resume keywords (%d): %s", len(resume_keywords), resume_keywords)

    model = get_model()
    resume_vec = model.encode(resume_text, normalize_embeddings=True)

    jobs = store.list_jobs(limit=10_000) if db_path is None else store.list_jobs(db_path=db_path, limit=10_000)
    scored = 0
    skipped = 0

    for j in jobs:
        text = (j.get("title") or "") + "\n" + (j.get("description_text") or "")
        if not text.strip():
            skipped += 1
            continue
        job_vec = model.encode(text, normalize_embeddings=True)
        score = cosine_match_score(resume_vec, job_vec)

        required = extract_keywords(text)
        coverage, missing = compute_coverage(required, resume_keywords)

        log.info(
            "scored key=%s score=%.1f required(%d)=%s missing(%d)=%s",
            j["key"], score, len(required), required, len(missing), missing,
        )

        if db_path is None:
            store.set_scores(j["key"], score, coverage, required, missing)
        else:
            store.set_scores(j["key"], score, coverage, required, missing, db_path=db_path)
        scored += 1

    return {
        "scored": scored,
        "skipped": skipped,
        "resume_keyword_count": len(resume_keywords),
    }
