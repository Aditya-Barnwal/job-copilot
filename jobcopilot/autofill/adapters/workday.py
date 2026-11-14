"""Workday adapter (<tenant>.wdN.myworkdayjobs.com), declarative: specs/workday.yaml.

Multi-step: My Information -> My Experience -> Application Questions ->
Voluntary Disclosures -> Self Identify -> Review. The active step is read from
the progress bar; the ONLY forward control is bottom-navigation-next-button
("Save and Continue", through SafePage.next_step's allowlist), and the run
STOPS on Review, where that button reads "Submit": it is highlighted, never
pressed. A sign-in / create-account wall pauses as needs_you (no typing, no
accounts). The 'beecatcher' honeypot is never planned or touched.

The nightly plan can't see the steps (they sit behind the candidate's
sign-in), so build_plan() plans the standard data-automation-id fields from
the spec; questionnaire fields found live are filled only when run() is given
the planning context (same answers rules), otherwise highlighted.
"""
from __future__ import annotations

from .base import register
from .greenhouse import SpecAdapter


@register
class WorkdayAdapter(SpecAdapter):
    spec_name = "workday"


__all__ = ["WorkdayAdapter"]
