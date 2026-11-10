"""Server-side half of the no-fabrication gate on POST /approve.

The browser shows grounding flags and asks the user to confirm them, but a
check that only runs in the browser can be skipped by any client. So the
server re-runs grounding on the exact text being approved and refuses unless
the request explicitly confirms every flagged item. Fail-closed: a missing or
malformed confirmation counts as "not confirmed".
"""
from __future__ import annotations

from typing import Any

FLAG_KEYS = ("inserted_unsupported", "unsupported_skills", "new_numbers", "inflated_claims")


def _norm(item: Any) -> str:
    return str(item).strip().lower()


def unconfirmed_flags(grounding: dict, confirmed: Any) -> dict[str, list[str]]:
    """Flagged items not covered by `confirmed`, per list; {} means all covered.

    `confirmed` must mirror the grounding shape ({list_name: [items]}); each
    list is matched case-insensitively as a superset of the flagged items.
    """
    given = confirmed if isinstance(confirmed, dict) else {}
    out: dict[str, list[str]] = {}
    for key in FLAG_KEYS:
        raw = given.get(key)
        ok = {_norm(x) for x in raw} if isinstance(raw, list) else set()
        missing = [x for x in (grounding.get(key) or []) if _norm(x) not in ok]
        if missing:
            out[key] = missing
    return out
