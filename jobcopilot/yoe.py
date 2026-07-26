"""Years-of-experience extractor for job descriptions.

Regex-based, guarded against the obvious false positives that turn this kind
of extractor into a mess (founding years, team-size numbers, graduation years).
Returns the *lower* bound when a range is given; tags `is_preferred` when the
surrounding clause flags the requirement as soft.
"""
from __future__ import annotations

import re
from typing import Optional


_YEARS_RE = re.compile(
    r"""
    (?P<lo>\d{1,2})       # primary number
    \s*\+?\s*
    (?:to|-|–|—|or)?\s*
    (?P<hi>\d{1,2})?\s*
    years?
    """,
    re.I | re.X,
)

# Context signals that confirm a "X years" mention is about job-experience
# rather than (say) company tenure or product age.
_EXPERIENCE_CONTEXT = (
    "experience", "exp.", "preferred", "required", "minimum", "min ",
    "in software", "in python", "in java", "in engineering", "of software",
    "background in", "expertise", "working with", "hands-on", "hands on",
)

_FALSE_POS_TOKENS = (
    "founded", "since", "combined", "team", "collectively", "graduat",
    "cohort", "established", "company has", "we have been",
)

_SOFT_TOKENS = (
    "preferred", "nice to have", "a plus", "ideally", "bonus",
    "would be a plus", "good to have",
)


def _windowed(text: str, start: int, end: int, before: int = 60, after: int = 60) -> str:
    return text[max(0, start - before): min(len(text), end + after)].lower()


def extract_min_yoe(description_text: Optional[str]) -> tuple[Optional[int], bool]:
    """Return (min_yoe, is_preferred). If nothing valid matches, returns (None, False)."""
    if not description_text:
        return None, False
    text = description_text
    best: Optional[int] = None
    best_is_pref = False

    for m in _YEARS_RE.finditer(text):
        try:
            lo = int(m.group("lo"))
        except (TypeError, ValueError):
            continue
        # Sanity: yoe must be plausible.
        if lo > 30 or lo < 0:
            continue
        # 4-digit-year guard: yoe is captured before "years", but a phrase like
        # "2024 years of operation" can't sneak past because the regex requires
        # 1–2 digits. Still, defensively skip when the lo is "too year-like".
        if lo >= 1900:
            continue
        window = _windowed(text, m.start(), m.end())
        if any(t in window for t in _FALSE_POS_TOKENS):
            continue
        # Must look like an experience requirement, not a stray "X years"
        # such as "2 years from now" or "X years guarantee".
        if not any(t in window for t in _EXPERIENCE_CONTEXT):
            continue
        is_pref = any(t in window for t in _SOFT_TOKENS)

        # Prefer the *strictest* (largest) required floor unless all options
        # are preferred — that biases toward the bar a junior would actually
        # bump into.
        if best is None:
            best, best_is_pref = lo, is_pref
        elif is_pref and not best_is_pref:
            continue  # don't let a soft mention overwrite a hard one
        elif (not is_pref) and best_is_pref:
            best, best_is_pref = lo, is_pref
        elif lo > best:
            best, best_is_pref = lo, is_pref

    return best, best_is_pref
