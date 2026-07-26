"""Tailoring orchestrator.

Three operations:
  - suggest(job): reuse vs fresh decision (no LLM call for reuse)
  - tailor(job): one LLM call + grounding check + diff
  - approve(job, ...): version + PDF + link + Ready

Tailoring contract:
  System: you may only rephrase/reorder existing content; never invent skills,
  metrics, employers, dates. Missing keywords without resume basis go in `gaps`.
  Return JSON: { "tailored_text": str, "surfaced_keywords": [str], "gaps": [str] }
"""
from __future__ import annotations

import difflib
import logging
import os
from typing import Optional

from . import llm, scoring, store
from .grounding import check_grounding


log = logging.getLogger("jobcopilot.tailor")


REUSE_THRESHOLD = float(os.getenv("REUSE_THRESHOLD", "0.85"))


# ---------------------------------------------------------------------------

def _coverage_of_version_against_job(job: dict, version: dict) -> float:
    req = set(k.lower() for k in (job.get("required_keywords") or []))
    if not req:
        return 0.0
    tgt = set(k.lower() for k in (version.get("target_keywords") or []))
    matched = req & tgt
    return len(matched) / len(req)


def _coverage_of_text_against_job(job: dict, text: str) -> float:
    from . import scoring
    req = set(k.lower() for k in (job.get("required_keywords") or []))
    if not req:
        return 0.0
    have = set(k.lower() for k in scoring.extract_keywords(text or ""))
    return len(req & have) / len(req)


def resume_options(job_key: str) -> dict:
    """Ranked list of resumes for this job: original + every stored version,
    ordered by coverage of job.required_keywords. The best existing option is
    marked `recommended` only if its coverage ≥ REUSE_THRESHOLD. Always also
    includes a `tailor_fresh` option at the end."""
    job = store.get_job(job_key)
    if not job:
        return {"error": "job not found"}
    resume = store.get_resume()
    options: list[dict] = []

    if resume and resume.get("text"):
        cov = _coverage_of_text_against_job(job, resume["text"])
        options.append({
            "source": "original",
            "label": resume.get("filename") or "Original",
            "coverage_pct": round(cov * 100),
            "coverage": cov,
        })

    for v in store.list_versions(role_family=job.get("role_family")):
        cov = _coverage_of_version_against_job(job, v)
        options.append({
            "source": "version",
            "version_id": v["id"],
            "label": v.get("label") or f"version {v['id']}",
            "coverage_pct": round(cov * 100),
            "coverage": cov,
            "pdf_path": v.get("pdf_path"),
            "role_family": v.get("role_family"),
        })

    options.sort(key=lambda o: o["coverage"], reverse=True)

    # `recommended`: only the single best existing option, only if it meets
    # the reuse threshold. Ties broken by list order.
    if options and options[0]["coverage"] >= REUSE_THRESHOLD:
        options[0]["recommended"] = True
    for o in options:
        o.setdefault("recommended", False)

    options.append({"source": "tailor_fresh", "label": "Tailor a fresh version",
                    "coverage_pct": None, "recommended": False})

    log.info("resume_options key=%s options=%d recommended=%s",
             job_key, len(options) - 1,
             next((o["label"] for o in options if o.get("recommended")), None))
    return {"options": options, "reuse_threshold": REUSE_THRESHOLD}


def suggest(job_key: str) -> dict:
    """Reuse check → returns mode=reuse if a stored version already covers
    ≥REUSE_THRESHOLD of this job's required keywords; otherwise mode=fresh
    plus ranked candidates."""
    job = store.get_job(job_key)
    if not job:
        return {"error": "job not found"}
    required = job.get("required_keywords") or []
    if not required:
        return {"mode": "fresh", "reason": "no required keywords (run scoring first)",
                "candidates": []}

    versions = store.list_versions(role_family=job.get("role_family"))
    if not versions:
        versions = store.list_versions()  # fall back to any version

    ranked = []
    for v in versions:
        cov = _coverage_of_version_against_job(job, v)
        ranked.append({"id": v["id"], "label": v["label"], "role_family": v.get("role_family"),
                       "coverage": cov, "pdf_path": v.get("pdf_path"),
                       "base_ats_coverage": v.get("base_ats_coverage")})
    ranked.sort(key=lambda r: r["coverage"], reverse=True)

    best = ranked[0] if ranked else None
    log.info("suggest key=%s best=%s threshold=%s",
             job_key, (best and best["coverage"]), REUSE_THRESHOLD)

    if best and best["coverage"] >= REUSE_THRESHOLD:
        return {"mode": "reuse", "version": best, "coverage": best["coverage"],
                "candidates": ranked}
    return {"mode": "fresh", "candidates": ranked}


# ---------------------------------------------------------------------------

TAILOR_SYSTEM = (
    "You are an ATS resume optimizer. You may only REPHRASE and REORDER content "
    "that ALREADY EXISTS in the original resume. Do not invent any skill, tool, "
    "employer, certification, metric, percentage, or date that is not already "
    "present in the original resume. If the job requires a keyword the resume "
    "cannot support, put it in `gaps` — do not insert it. "
    "Return ONLY valid JSON, no prose, no code fences."
)


TAILOR_USER_TEMPLATE = """ORIGINAL RESUME (verbatim):
\"\"\"
{resume}
\"\"\"

JOB TITLE: {title}

JOB DESCRIPTION:
\"\"\"
{description}
\"\"\"

JOB REQUIRED KEYWORDS (extracted): {required}
JOB MISSING FROM RESUME (extracted): {missing}

TASK: Produce a tailored version of the resume that surfaces as many of the
job's required keywords as the original honestly supports. For each missing
keyword, if there is no basis in the original, add it to `gaps` (do NOT insert
it). Keep the resume the same approximate length.

Return JSON with exactly these keys:
{{
  "tailored_text": "<full resume text>",
  "surfaced_keywords": ["<keyword>", ...],
  "gaps": ["<keyword>", ...]
}}
"""


def _simple_diff(a: str, b: str) -> str:
    return "\n".join(difflib.unified_diff(
        (a or "").splitlines(), (b or "").splitlines(),
        fromfile="original", tofile="tailored", lineterm="",
    ))


def tailor(job_key: str, approved_keywords: Optional[list[str]] = None) -> dict:
    job = store.get_job(job_key)
    if not job:
        return {"error": "job not found"}
    resume = store.get_resume()
    if not resume or not resume.get("text"):
        return {"error": "no resume uploaded"}

    required = job.get("required_keywords") or []
    missing = job.get("missing_keywords") or []
    if not required:
        return {"error": "job has no required keywords; run scoring first"}

    user_prompt = TAILOR_USER_TEMPLATE.format(
        resume=resume["text"],
        title=job.get("title", ""),
        description=(job.get("description_text") or "")[:6000],
        required=", ".join(required),
        missing=", ".join(missing),
    )

    try:
        parsed, meta = llm.complete_json(TAILOR_SYSTEM, user_prompt,
                                         temperature=0.2, max_tokens=2200)
    except Exception as exc:
        return {"error": str(exc)}

    tailored_text = (parsed.get("tailored_text") or "").strip()
    if not tailored_text:
        return {"error": "model returned empty tailored_text", "raw": raw[:1000]}

    # Re-score the tailored text against the job's required keywords.
    tailored_have = scoring.extract_keywords(tailored_text)
    new_coverage, still_missing = scoring.compute_coverage(required, tailored_have)

    grounding = check_grounding(
        original_resume=resume["text"],
        tailored_text=tailored_text,
        job_missing_keywords=missing,
        user_approved_keywords=set(approved_keywords or []),
    )

    diff = _simple_diff(resume["text"], tailored_text)

    log.info(
        "tailor key=%s before_ats=%.2f after_ats=%.2f still_missing=%s grounding_clean=%s retries=%s",
        job_key, job.get("ats_coverage") or 0.0, new_coverage,
        still_missing, grounding["clean"], meta.get("retries"),
    )

    return {
        "tailored_text": tailored_text,
        "diff": diff,
        "before_ats_coverage": job.get("ats_coverage") or 0.0,
        "new_ats_coverage": new_coverage,
        "still_missing": still_missing,
        "gaps": parsed.get("gaps") or [],
        "surfaced_keywords": parsed.get("surfaced_keywords") or [],
        "grounding": grounding,
        "required_keywords": required,
    }
