"""Form autofill that STOPS BEFORE SUBMIT (Phase 4).

    plan.py      FieldAction / FillPlan / Outcome
    safepage.py  SafePage: the only page handle adapters get (no submit path)
    guard.py     DOM + network submit guard, denylist, handover()
    detect.py    CAPTCHA / login wall / confirmation page / honeypot
    session.py   headed persistent-profile Chrome (no stealth flags)
    executor.py  run a plan step by step, highlight, screenshot, Outcome
    generic.py   GenericAdapter (fallback)
    adapters/    per-ATS adapters + registry
    service.py   plan_for_job / launch_fill / watch_for_confirmation

Nothing here imports Playwright at import time.
"""
from .plan import FieldAction, FillPlan, Outcome

__all__ = ["FieldAction", "FillPlan", "Outcome"]
