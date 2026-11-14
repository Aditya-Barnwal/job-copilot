"""Run a FillPlan through a SafePage, page by page, and stop before Submit.

    run_plan(page_or_safe, plan, *, adapter=None, ctx=None, screenshot_dir=None) -> Outcome
    (alias: execute)

Per step: CAPTCHA / login-wall check → type every action whose value is
approved (needs_user False, confidence ≥ min) → leave everything else EMPTY
and highlighted ("needs you") → press Next only through SafePage.next_step.
On the last page it highlights the single Submit control (never clicks it),
takes a screenshot under data/applications/<job>/ and returns:

    ready      every required field filled, exactly one Submit found
    needs_you  CAPTCHA / login wall / uncertain required answers / no unique Submit
    blocked    do-not-automate or off-site URL, required data missing (missing data ⇒ blocked)
    error      something broke (type name only; no values)

The caller hands the browser to the user with SafePage.handover() (or
guard.handover(page)); only then can the user's own click submit.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any, Optional

from .guard import (ConsentRefused, DeniedHost, Halted, HoneypotRefused, OffSite, SafePageError,
                    SubmitGuardViolation)
from .plan import FieldAction, FillPlan, Outcome
from .safepage import SafePage, job_dir

log = logging.getLogger("jobcopilot.autofill.executor")

DEFAULT_MIN_CONFIDENCE = 0.8
MAX_STEPS = 12
_TRUE = {"true", "yes", "1", "on", "checked"}


def _strip(url: str) -> str:
    return (url or "").split("#", 1)[0].rstrip("/")


def usable(a: FieldAction, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> bool:
    """May this action's value be typed? Only approved, confident values."""
    if a.kind == "upload":
        return bool(a.value) and not a.needs_user
    return (not a.needs_user and a.value not in (None, "")
            and float(a.confidence or 0) >= float(min_confidence))


class _Run:
    def __init__(self, safe: SafePage, plan: FillPlan, adapter: Any, min_confidence: float) -> None:
        self.safe, self.plan, self.adapter = safe, plan, adapter
        self.min_conf = min_confidence
        self.filled = 0
        self.left_empty = 0
        self.highlighted: list[str] = []
        self.missing_required: list[str] = []      # no data at all  -> blocked
        self.uncertain_required: list[str] = []    # data needs you  -> needs_you

    def mark(self, a: FieldAction, *, missing: bool, kind: str = "needs_you") -> None:
        self.left_empty += 1
        if self.safe.highlight(a.locator, kind) and a.label not in self.highlighted:
            self.highlighted.append(a.label)
        if a.required:
            (self.missing_required if missing else self.uncertain_required).append(a.label)

    def do(self, a: FieldAction) -> None:
        safe = self.safe
        if a.kind == "upload":
            path = (self.plan.files or {}).get(a.value or "")
            if not usable(a) or not path or not Path(path).is_file():
                return self.mark(a, missing=True)
            safe.upload(a.locator, path, label=a.label, source=f"file:{a.value}")
        elif not usable(a, self.min_conf):
            return self.mark(a, missing=(a.source in ("", "none") and float(a.confidence or 0) == 0))
        elif a.kind == "fill":
            safe.fill(a.locator, str(a.value), label=a.label, source=a.source)
        elif a.kind == "select":
            safe.select(a.locator, str(a.value), label=a.label, source=a.source)
        elif a.kind in ("check", "radio"):
            v = str(a.value)
            if safe.count(a.locator) > 1:             # a group: pick the option by label
                safe.choose(a.locator, v, label=a.label, source=a.source)
            elif a.kind == "radio" or v.strip().lower() in _TRUE:
                safe.check(a.locator, label=a.label, source=a.source)
            else:
                return                                # "false": leave unticked, nothing to do
        self.filled += 1


def _outcome(status: str, reason: str, run: Optional[_Run] = None, **kw: Any) -> Outcome:
    return Outcome(status, reason, run.filled if run else 0, run.left_empty if run else 0,
                   list(run.highlighted) if run else [], **kw)


def _signature(safe: SafePage) -> tuple:
    try:
        return (_strip(safe.url), tuple(f.get("selector") for f in safe.fields() if not f.get("hidden")))
    except Exception:  # noqa: BLE001
        return (_strip(safe.url),)


def _screenshot(safe: SafePage, plan: FillPlan, status: str, screenshot_dir: Any) -> Optional[str]:
    name = f"autofill-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{status}.png"
    for d in ([Path(screenshot_dir)] if screenshot_dir else []) + [job_dir(plan.job_key)]:
        try:
            return safe.screenshot(d / name)
        except Exception:  # noqa: BLE001 — outside data/applications, or the page is gone
            continue
    return None


def run_plan(page_or_safe: Any, plan: FillPlan, *, adapter: Any = None, ctx: Any = None,
             screenshot_dir: Any = None, min_confidence: Optional[float] = None,
             max_steps: int = MAX_STEPS) -> Outcome:
    if adapter is None:
        from .adapters import adapter_for
        adapter = adapter_for(plan.url)
    if min_confidence is None:
        min_confidence = float((ctx or {}).get("min_confidence", DEFAULT_MIN_CONFIDENCE)
                               if isinstance(ctx, dict) else DEFAULT_MIN_CONFIDENCE)
    if isinstance(page_or_safe, SafePage):
        safe = page_or_safe
    else:
        kw = adapter.safe_page_kwargs() if hasattr(adapter, "safe_page_kwargs") else {}
        safe = SafePage(page_or_safe, **kw)
    safe.allowed_uploads = {str(Path(p).expanduser().resolve()) for p in (plan.files or {}).values()
                            if p and Path(p).expanduser().is_file()}
    run = _Run(safe, plan, adapter, min_confidence)
    step_reached = None
    try:
        if plan.url and _strip(safe.url) != _strip(plan.url):
            try:
                safe.goto(plan.url)
            except DeniedHost:
                return _outcome("blocked", "do-not-automate site: open it yourself (link-out only)")
            except OffSite as exc:
                return _outcome("blocked", str(exc))
        markers = getattr(adapter, "confirmation_markers", None) or {}
        from . import detect
        i = 0
        while True:
            total = max(len(plan.steps), i + 1)
            step_reached = f"step {i + 1} of {total}"
            if detect.confirmation_page(safe.url, safe.text(4000), plan.ats, markers=markers):
                safe.halt("already on a confirmation page")
                return _outcome("needs_you", "the page already shows an application confirmation",
                                run, step_reached=step_reached)
            if (c := safe.captcha()):
                safe.halt("captcha")
                return _outcome("needs_you", f"CAPTCHA / bot check ({c}): solve it yourself", run,
                                screenshot=_screenshot(safe, plan, "needs_you", screenshot_dir),
                                step_reached=step_reached)
            if safe.login_wall():
                safe.halt("login wall")
                return _outcome("needs_you", "sign-in / account wall: sign in yourself (no passwords are typed)",
                                run, screenshot=_screenshot(safe, plan, "needs_you", screenshot_dir),
                                step_reached=step_reached)
            if i < len(plan.steps):
                actions = plan.steps[i]
            else:
                actions = adapter.replan(safe, i, ctx) if hasattr(adapter, "replan") else None
                actions = actions or []
            for a in actions:
                if (c := safe.captcha()):
                    safe.halt("captcha")
                    return _outcome("needs_you", f"CAPTCHA / bot check appeared ({c}): solve it yourself",
                                    run, screenshot=_screenshot(safe, plan, "needs_you", screenshot_dir),
                                    step_reached=step_reached)
                try:
                    run.do(a)
                except Halted:
                    raise
                except HoneypotRefused:
                    run.left_empty += 1                # never filled, never highlighted
                    log.info("honeypot left alone: %r", a.label)
                except ConsentRefused:
                    run.mark(a, missing=False)
                except (SubmitGuardViolation, SafePageError) as exc:
                    log.info("left empty %r: %s", a.label, type(exc).__name__)
                    run.mark(a, missing=False, kind="error")
                except Exception as exc:  # noqa: BLE001 — one bad field doesn't stop the run
                    log.info("left empty %r: %s", a.label, type(exc).__name__)
                    run.mark(a, missing=False, kind="error")
            if i == 0:
                for u in plan.unresolved or []:
                    if u.get("locator") and u.get("reason") not in ("honeypot", "hidden"):
                        if safe.highlight(u["locator"], "needs_you") and u.get("label") \
                                and u["label"] not in run.highlighted:
                            run.highlighted.append(u["label"])
            nxt = adapter.next_locator(safe, i) if hasattr(adapter, "next_locator") else safe.find_next()
            if nxt is None:
                if i + 1 < len(plan.steps):
                    return _outcome("needs_you", "couldn't find the Next button: continue yourself", run,
                                    screenshot=_screenshot(safe, plan, "needs_you", screenshot_dir),
                                    step_reached=step_reached)
                break                                   # the last page: stop before Submit
            if run.missing_required or run.uncertain_required:
                return _outcome("needs_you", "required fields on this page need you before Next", run,
                                screenshot=_screenshot(safe, plan, "needs_you", screenshot_dir),
                                step_reached=step_reached)
            if i + 1 >= len(plan.steps) and ctx is None:
                return _outcome("needs_you", "more pages follow: continue yourself from here", run,
                                screenshot=_screenshot(safe, plan, "needs_you", screenshot_dir),
                                step_reached=step_reached)
            if i + 1 >= max_steps:
                return _outcome("needs_you", "too many pages: continue yourself", run,
                                step_reached=step_reached)
            before = _signature(safe)
            try:
                safe.next_step(nxt)
            except SubmitGuardViolation:
                break                                   # "Next" was submit-like: treat as the end
            if _signature(safe) == before:
                return _outcome("needs_you", "Next didn't advance (the form may show errors)", run,
                                screenshot=_screenshot(safe, plan, "needs_you", screenshot_dir),
                                step_reached=step_reached)
            i += 1

        submit = adapter.submit_locator(safe) if hasattr(adapter, "submit_locator") else safe.find_submit()
        if submit:
            safe.highlight(submit, "submit")            # highlighted, NEVER clicked
        if run.missing_required:
            status, reason = "blocked", f"{len(run.missing_required)} required field(s) have no approved data"
        elif run.uncertain_required:
            status, reason = "needs_you", f"{len(run.uncertain_required)} required field(s) need your answer"
        elif not submit:
            status, reason = "needs_you", "couldn't find exactly one Submit button"
        else:
            status, reason = "ready", "filled; review and press Submit yourself"
        log.info("autofill %s: filled=%d left_empty=%d highlighted=%d", status, run.filled,
                 run.left_empty, len(run.highlighted))
        return _outcome(status, reason, run, submit_selector=submit,
                        screenshot=_screenshot(safe, plan, status, screenshot_dir),
                        step_reached=step_reached)
    except Halted as exc:
        return _outcome("needs_you", f"stopped: {exc}", run, step_reached=step_reached)
    except Exception as exc:  # noqa: BLE001
        log.warning("autofill run failed: %s", type(exc).__name__)
        return _outcome("error", f"autofill failed ({type(exc).__name__})", run, step_reached=step_reached)


execute = run_plan

__all__ = ["run_plan", "execute", "usable", "DEFAULT_MIN_CONFIDENCE"]
