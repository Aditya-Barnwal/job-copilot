"""Fill-plan types shared by the planner (nightly, read-only), the executor
(on click, headed) and the review UI.

    FieldAction  one field: where (locator), what (value), why (source, confidence)
    FillPlan     one job: steps (one list of FieldActions per form page), the
                 fields nobody could answer (unresolved), and the local files
    Outcome      how a run ended: ready | blocked | needs_you | error

A FillPlan may carry a *proposed* value with needs_user=True so the review
UI can show it; the executor only ever types values with needs_user False
(non-negotiable 4). Plans live in the local DB / data/ dir only.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

KINDS = ("fill", "select", "check", "upload", "radio")
STATUSES = ("ready", "blocked", "needs_you", "error")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _known(cls, d: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in (d or {}).items() if k in names}


@dataclass
class FieldAction:
    locator: str                    # Playwright selector (CSS unless prefixed)
    label: str                      # the question as the form shows it
    kind: str                       # fill | select | check | upload | radio
    value: Optional[str]            # check: "true"/"false"; upload: a key of FillPlan.files
    source: str                     # answers.yaml:<path> | facts:<id> | file:resume | user | none
    confidence: float
    needs_user: bool
    note: str = ""
    required: bool = False

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"FieldAction.kind must be one of {KINDS}, got {self.kind!r}")
        self.confidence = float(self.confidence or 0.0)
        self.needs_user = bool(self.needs_user)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FieldAction":
        return cls(**_known(cls, d))

    def __repr__(self) -> str:      # never put the value in logs / tracebacks
        return (f"FieldAction(label={self.label!r}, kind={self.kind!r}, source={self.source!r}, "
                f"confidence={self.confidence}, needs_user={self.needs_user})")


@dataclass
class FillPlan:
    job_key: str
    ats: str
    url: str
    steps: list[list[FieldAction]] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)   # {label, reason, locator?, required?}
    files: dict[str, str] = field(default_factory=dict)     # 'resume' / 'cover_letter' -> path
    created_at: str = field(default_factory=_now)

    def actions(self) -> list[FieldAction]:
        return [a for step in self.steps for a in step]

    def to_dict(self) -> dict:
        return {"job_key": self.job_key, "ats": self.ats, "url": self.url,
                "steps": [[a.to_dict() for a in step] for step in self.steps],
                "unresolved": [dict(u) for u in self.unresolved],
                "files": dict(self.files), "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict) -> "FillPlan":
        d = dict(d or {})
        steps = [[a if isinstance(a, FieldAction) else FieldAction.from_dict(a) for a in step]
                 for step in d.get("steps") or []]
        return cls(job_key=str(d.get("job_key", "")), ats=str(d.get("ats", "")),
                   url=str(d.get("url", "")), steps=steps,
                   unresolved=list(d.get("unresolved") or []), files=dict(d.get("files") or {}),
                   created_at=str(d.get("created_at") or _now()))

    def __repr__(self) -> str:
        return (f"FillPlan(job_key={self.job_key!r}, ats={self.ats!r}, steps={len(self.steps)}, "
                f"actions={len(self.actions())}, unresolved={len(self.unresolved)})")


@dataclass
class Outcome:
    status: str                     # ready | blocked | needs_you | error
    reason: str
    filled: int = 0
    left_empty: int = 0
    highlighted: list[str] = field(default_factory=list)   # labels only, never values
    submit_selector: Optional[str] = None                  # highlighted, NEVER clicked
    screenshot: Optional[str] = None
    step_reached: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"Outcome.status must be one of {STATUSES}, got {self.status!r}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Outcome":
        return cls(**_known(cls, d))


# A pasted cover letter: the only free-text field autofill may type into, and
# only with the letter you approved (jobcopilot/cover_letters.py). The field
# must BE the cover-letter box: its label starts with "cover letter" (or "paste
# your cover letter"), or its name / id is cover_letter(_text). An open question
# that merely mentions a cover letter ("Anything else, e.g. a cover letter?")
# or asks "Why do you want to work here?" on its own is not one.
COVER_TEXT_SOURCE = "approved_cover_letter"
_COVER_LABEL_RE = re.compile(
    r"^(?:(?:please\s+)?(?:paste|enter|add|write|type|include)\s+)?(?:your\s+|a\s+)?cover[\s_-]*letter"
    r"(?:\s*(?:text|body|here|\((?:text|paste|optional)\)))?(?:\s*[:?.\-\u2013\u2014]\s*.*)?$", re.I | re.S)
_COVER_NAME_RE = re.compile(r"^(?:job_application\[)?cover[_-]?letter(?:[_-]?text)?\]?$", re.I)


def is_cover_letter_text_field(label: str, name: str = "", field_type: str = "textarea") -> bool:
    """True when this free-text field is the form's cover-letter box."""
    if field_type != "textarea":
        return False
    lab = re.sub(r"\(\s*(?:required|optional|mandatory)\s*\)|\*", " ", str(label or ""), flags=re.I)
    lab = re.sub(r"\s+", " ", lab).strip()
    return bool(_COVER_LABEL_RE.match(lab)) or bool(_COVER_NAME_RE.match(str(name or "").strip()))


__all__ = ["FieldAction", "FillPlan", "Outcome", "KINDS", "STATUSES", "COVER_TEXT_SOURCE",
           "is_cover_letter_text_field"]
