"""Deterministic anti-fabrication check (addendum-2 Part 1).

After the model returns a tailored resume, this catches the two patterns a weak
model is most likely to produce:
  1. inserted_unsupported — a job 'missing keyword' now present in the tailored
     text but absent from the original AND not pre-approved by the user.
  2. new_numbers — numbers/percentages/4-digit years in the tailored text that
     weren't in the original. Classic fabricated-metric pattern.

If either is non-empty, the UI blocks Approve until the human explicitly
confirms the additions.
"""
from __future__ import annotations

import re


def _has_term(text: str, term: str) -> bool:
    return re.search(
        r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])",
        (text or "").lower(),
    ) is not None


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\b\d+(?:\.\d+)?%?\b", text or ""))


def check_grounding(original_resume: str,
                    tailored_text: str,
                    job_missing_keywords: list[str],
                    user_approved_keywords: set[str] | None = None) -> dict:
    approved = {k.lower() for k in (user_approved_keywords or set())}
    inserted_unsupported = [
        kw for kw in (job_missing_keywords or [])
        if _has_term(tailored_text, kw)
        and not _has_term(original_resume, kw)
        and kw.lower() not in approved
    ]
    new_numbers = sorted(_numbers(tailored_text) - _numbers(original_resume))
    return {
        "inserted_unsupported": inserted_unsupported,
        "new_numbers": new_numbers,
        "clean": not inserted_unsupported and not new_numbers,
    }
