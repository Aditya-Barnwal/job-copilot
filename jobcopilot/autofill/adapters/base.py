"""Adapter base class + registry.

An adapter knows one ATS: which hosts it serves, how to turn a read-only
form snapshot into a FillPlan, the network endpoints that would submit (the
guard aborts them), and how its confirmation page looks. It drives the
browser only through a SafePage; the default run() is the shared executor.

    @register
    class GreenhouseAdapter(Adapter):
        name = "greenhouse"
        hosts = ("boards.greenhouse.io", "job-boards.greenhouse.io")
        submit_patterns = (r"/applications?$",)
        confirmation_markers = {"url": (r"/confirmation",), "text": (r"thank you for applying",)}
        def build_plan(self, form_snapshot, ctx): ...

adapter_for(url) picks the first registered adapter whose matches(url) is
true (highest priority first) and falls back to GenericAdapter.
"""
from __future__ import annotations

import datetime as dt
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..guard import host_matches
from ..plan import FillPlan, Outcome


@dataclass
class PlanContext:
    """Everything a planner needs besides the form itself."""
    job_key: str
    job: dict = field(default_factory=dict)          # store.get_job(...) row
    url: str = ""
    files: dict = field(default_factory=dict)        # 'resume' / 'cover_letter' -> local path
    answers: Any = None                              # None = profile/answers.yaml
    facts: Any = None                                # None = profile/resume_facts.yaml
    answer_fn: Optional[Callable[..., Any]] = None   # default answers.answer_question
    today: Optional[dt.date] = None
    company: Any = None
    min_confidence: float = 0.8                      # config/autofill.yaml min_confidence
    # The APPROVED cover letter's text (cover_letters.approved_letter), for the
    # form's cover-letter box only; None = leave that box for you. Kept out of repr.
    cover_letter_text: Optional[str] = field(default=None, repr=False)

    def answer(self, question: str, *, field_type: str = "text", options: Optional[list] = None,
               required: Optional[bool] = None, **extra: Any) -> Any:
        """`extra`: unit_hint / digits_only when the field has them (answers.answer_question)."""
        fn = self.answer_fn
        if fn is None:
            from ... import answers as A
            fn = A.answer_question
        return fn(question, field_type=field_type, options=options, job=self.job or None,
                  answers=self.answers, facts=self.facts, required=required,
                  company=self.company, today=self.today, **extra)


class Adapter:
    name: str = "base"
    hosts: tuple[str, ...] = ()
    priority: int = 0
    submit_patterns: tuple[str, ...] = ()        # network: aborted always (URL or body regex)
    allow_post_patterns: tuple[str, ...] = ()    # network: benign non-GETs (uploads, save-progress)
    confirmation_markers: dict = {}              # {"url": (regex, ...), "text": (regex, ...)}
    honeypot_selectors: tuple[str, ...] = ()     # never filled, never highlighted
    next_selectors: tuple[str, ...] = ()         # tried in order before the generic Next search

    # -- matching
    @classmethod
    def matches(cls, url: str) -> bool:
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            return False
        return bool(cls.hosts) and host_matches(host, cls.hosts)

    # -- planning (read-only)
    def snapshot(self, safe: Any) -> dict:
        """Read-only form snapshot. Default: every field the page shows."""
        return {"url": safe.url, "fields": safe.fields()}

    def build_plan(self, form_snapshot: Any, ctx: PlanContext) -> FillPlan:
        raise NotImplementedError

    # -- execution
    def safe_page_kwargs(self) -> dict:
        return {"submit_patterns": self.submit_patterns,
                "allow_post_patterns": self.allow_post_patterns, "hosts": self.hosts}

    def next_locator(self, safe: Any, step_index: int) -> Optional[str]:
        for sel in self.next_selectors:
            if safe.count(sel) == 1:
                return sel
        return safe.find_next()

    def submit_locator(self, safe: Any) -> Optional[str]:
        return safe.find_submit()

    def replan(self, safe: Any, step_index: int, ctx: Optional[PlanContext]) -> Optional[list]:
        """Actions for a step the nightly plan couldn't see (multi-page forms)."""
        if ctx is None:
            return None
        try:
            plan = self.build_plan(self.snapshot(safe), ctx)
        except NotImplementedError:
            return None
        return plan.steps[0] if plan.steps else []

    def run(self, safe: Any, plan: FillPlan, ctx: Optional[PlanContext] = None) -> Outcome:
        from ..executor import run_plan
        return run_plan(safe, plan, adapter=self, ctx=ctx)


REGISTRY: list[type[Adapter]] = []


def register(cls: type[Adapter]) -> type[Adapter]:
    if cls not in REGISTRY:
        REGISTRY.append(cls)
        REGISTRY.sort(key=lambda c: -int(getattr(c, "priority", 0)))
    return cls


def get(name: str) -> Optional[type[Adapter]]:
    from . import load_all
    load_all()
    for cls in REGISTRY:
        if cls.name == name:
            return cls
    if name == "generic":
        from ..generic import GenericAdapter
        return GenericAdapter
    return None


def adapter_for(url: str) -> Adapter:
    from . import load_all
    load_all()
    for cls in REGISTRY:
        if cls.name != "generic" and cls.matches(url):
            return cls()
    from ..generic import GenericAdapter
    return GenericAdapter()


__all__ = ["Adapter", "PlanContext", "REGISTRY", "register", "adapter_for", "get"]
