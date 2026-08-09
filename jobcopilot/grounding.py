"""Deterministic anti-fabrication check.

After the model returns a tailored résumé (or cover email), this catches the
patterns a weak model is most likely to invent — WITHOUT using an LLM to judge
another LLM. Every check is a plain set-difference against the source résumé, so
a flag is always explainable ("this term/number/verb is in the tailored text but
not in your original") and never a probabilistic guess.

Four checks, chosen for high signal-to-noise (a noisy check trains the user to
reflexively tick "I confirm", which defeats the whole gate):

  1. inserted_unsupported — a job "missing keyword" now present in the tailored
     text but absent from the original AND not pre-approved by the user.
  2. unsupported_skills   — any known tech/tool term (from the scoring vocab)
     that appears in the tailored text, is absent from the original, and the
     user hasn't approved. Closes the hole where a fabricated skill isn't in the
     job's missing-keyword set, so check #1 would miss it.
  3. new_numbers          — numbers / percentages / years in the tailored text
     that weren't in the original. The classic fabricated-metric pattern.
  4. inflated_claims      — ownership / seniority verbs ("led", "owned",
     "architected", …) introduced in the tailored text but not in the original.
     Catches credit inflation that uses only existing vocabulary — e.g.
     "collaborated on" rewritten as "led" — which the keyword checks miss.

If any list is non-empty, `clean` is False and the UI blocks Approve until the
human explicitly confirms the additions. Nothing auto-submits, so the human is
always the final grounding layer; these checks exist to make that review cheap
and focused.

Deliberately NOT checked: superlative qualifiers ("expert", "extensive", …).
They appear too readily in legitimate résumé language, so flagging them would
raise the false-positive rate and erode trust in the gate. Signal-to-noise over
coverage — a check the user learns to ignore is worse than no check.
"""
from __future__ import annotations

import re

# Ownership / seniority verb families → the surface forms we match. A family is
# "introduced" if any of its forms appears in the tailored text but none appears
# in the original. Kept deliberately tight to credit-claiming verbs; common,
# low-signal verbs ("built", "worked", "helped", "developed") are excluded on
# purpose — they carry no seniority claim and would only add noise.
_OWNERSHIP_VERBS: dict[str, tuple[str, ...]] = {
    "led": ("led", "leads", "leading"),
    "owned": ("owned", "owns", "owning"),
    "architected": ("architected", "architecting", "architects"),
    "spearheaded": ("spearheaded", "spearheading", "spearheads"),
    "founded": ("founded", "founding", "founds", "co-founded", "cofounded"),
    "directed": ("directed", "directing", "directs"),
    "managed": ("managed", "managing", "manages"),
    "headed": ("headed", "heading", "heads"),
    "pioneered": ("pioneered", "pioneering", "pioneers"),
    "orchestrated": ("orchestrated", "orchestrating", "orchestrates"),
    "oversaw": ("oversaw", "overseeing", "oversees"),
    "mentored": ("mentored", "mentoring", "mentors"),
}


def _has_term(text: str, term: str) -> bool:
    return re.search(
        r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])",
        (text or "").lower(),
    ) is not None


def _numbers(text: str) -> set[str]:
    """Numeric tokens: integers, decimals, percentages, and shorthand like
    '10x' / '1m' / '5,000'. Commas are stripped so '5,000' and '5000' compare
    equal — otherwise a harmless reformat would flag as a fabricated number."""
    raw = re.findall(r"(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?(?:%|x|k|m|bn|b)?",
                     text or "", flags=re.I)
    return {tok.replace(",", "").lower() for tok in raw if tok}


def _ownership_families(text: str) -> set[str]:
    low = (text or "").lower()
    return {fam for fam, forms in _OWNERSHIP_VERBS.items()
            if any(_has_term(low, f) for f in forms)}


def _tech_vocab() -> list[str]:
    """Reuse the scoring layer's curated tech vocabulary so the two stay in
    sync. Imported lazily and defensively: grounding must still work if the
    scoring module (or its heavy deps) can't be imported."""
    try:
        from .scoring import TECH_VOCAB
        return list(TECH_VOCAB)
    except Exception:
        return []


def check_grounding(original_resume: str,
                    tailored_text: str,
                    job_missing_keywords: list[str],
                    user_approved_keywords: set[str] | None = None) -> dict:
    approved = {k.lower() for k in (user_approved_keywords or set())}
    missing = job_missing_keywords or []
    missing_lower = {k.lower() for k in missing}

    inserted_unsupported = [
        kw for kw in missing
        if _has_term(tailored_text, kw)
        and not _has_term(original_resume, kw)
        and kw.lower() not in approved
    ]

    # Fabricated skills OUTSIDE the job's missing-keyword set. Excludes anything
    # already reported by inserted_unsupported so a term is flagged once, not twice.
    unsupported_skills = sorted({
        term.lower() for term in _tech_vocab()
        if _has_term(tailored_text, term)
        and not _has_term(original_resume, term)
        and term.lower() not in approved
        and term.lower() not in missing_lower
    })

    new_numbers = sorted(_numbers(tailored_text) - _numbers(original_resume))

    inflated_claims = sorted(
        _ownership_families(tailored_text) - _ownership_families(original_resume)
    )

    return {
        "inserted_unsupported": inserted_unsupported,
        "unsupported_skills": unsupported_skills,
        "new_numbers": new_numbers,
        "inflated_claims": inflated_claims,
        "clean": not (inserted_unsupported or unsupported_skills
                      or new_numbers or inflated_claims),
    }
