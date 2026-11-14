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


__all__ = ["FieldAction", "FillPlan", "Outcome", "KINDS", "STATUSES"]
