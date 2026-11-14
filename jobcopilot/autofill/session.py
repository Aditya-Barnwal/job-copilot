"""Browser sessions for autofill.

Real use: your installed Google Chrome (channel="chrome"), headed, on the
persistent profile ~/.jobcopilot/browser-profile, so portals you signed into
by hand stay signed in. No stealth / anti-detection flags, no fake user
agent: the browser says it is automated, honestly. Tests: headless, temp
profile, local fixture pages only.

Playwright is imported lazily so the slim demo image never loads it.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("jobcopilot.autofill.session")

PROFILE_DIR = Path(os.environ.get("JOBCOPILOT_BROWSER_PROFILE")
                   or Path.home() / ".jobcopilot" / "browser-profile")
DEFAULT_CHANNEL = "chrome"
VIEWPORT = {"width": 1280, "height": 900}
# Args we refuse to pass, ever (non-negotiable: no stealth).
FORBIDDEN_ARGS = ("AutomationControlled", "--user-agent", "disable-web-security",
                  "--disable-features=IsolateOrigins", "site-per-process")


class BrowserUnavailable(RuntimeError):
    pass


def _check_args(args: list[str]) -> list[str]:
    for a in args:
        if any(bad.lower() in a.lower() for bad in FORBIDDEN_ARGS):
            raise ValueError(f"refusing browser flag {a!r}: no stealth / anti-detection flags")
    return args


class BrowserSession:
    """Owns the Playwright driver, the context and its first page. Adapters
    never see `.page`; they get a SafePage via `safe_page()`."""

    def __init__(self, context: Any, *, playwright: Any = None, browser: Any = None) -> None:
        self.context = context
        self._pw = playwright
        self._browser = browser
        self.page = context.pages[0] if context.pages else context.new_page()
        self._safe = None

    def safe_page(self, **kw: Any):
        from .safepage import SafePage
        if self._safe is None:
            self._safe = SafePage(self.page, **kw)
        return self._safe

    def handover(self) -> bool:
        """Guards off for this context (whoever created the SafePage)."""
        from .guard import handover
        if self._safe is not None:
            self._safe.handover()
            return True
        return handover(self.context)

    def close(self) -> None:
        for closer in (getattr(self.context, "close", None), getattr(self._browser, "close", None),
                       getattr(self._pw, "stop", None)):
            if closer:
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _context_kwargs() -> dict:
    # service_workers="block": a worker could otherwise send requests our route can't see.
    return {"viewport": VIEWPORT, "service_workers": "block", "accept_downloads": False}


def launch(*, profile_dir: Optional[Path | str] = None, headless: Optional[bool] = None,
           headed: Optional[bool] = None, channel: Optional[str] = DEFAULT_CHANNEL,
           args: Optional[list[str]] = None) -> BrowserSession:
    """Persistent-profile Chrome. Headed by default (real use); pass
    headless=True or headed=False for an invisible window."""
    if headless is None:
        headless = (headed is False)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise BrowserUnavailable("playwright is not installed (pip install -r requirements-autofill.txt)") from exc
    profile = Path(profile_dir or PROFILE_DIR).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    try:
        kw = dict(_context_kwargs(), headless=headless, args=_check_args(list(args or [])))
        if channel:
            kw["channel"] = channel
        ctx = pw.chromium.launch_persistent_context(str(profile), **kw)
    except Exception as exc:
        pw.stop()
        raise BrowserUnavailable(f"could not start the browser: {type(exc).__name__}") from exc
    log.info("browser started (headless=%s, channel=%s)", headless, channel or "bundled")
    return BrowserSession(ctx, playwright=pw)


def launch_ephemeral(*, headless: bool = True, channel: Optional[str] = DEFAULT_CHANNEL) -> BrowserSession:
    """Throwaway, non-persistent context (read-only plan inspection, tests).
    Falls back to Playwright's bundled Chromium when Chrome isn't installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise BrowserUnavailable("playwright is not installed") from exc
    pw = sync_playwright().start()
    last: Optional[Exception] = None
    for ch in dict.fromkeys([channel, None]):
        try:
            browser = pw.chromium.launch(headless=headless, **({"channel": ch} if ch else {}))
            ctx = browser.new_context(**_context_kwargs())
            return BrowserSession(ctx, playwright=pw, browser=browser)
        except Exception as exc:  # noqa: BLE001
            last = exc
    pw.stop()
    raise BrowserUnavailable(f"no usable browser: {type(last).__name__ if last else '?'}")


# --------------------------------------------------------------------------- sign-in window

_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chrome")


def chrome_executable() -> Optional[str]:
    """Your installed Google Chrome, or None."""
    import shutil
    for p in _CHROME_PATHS:
        q = Path(p).expanduser()
        if q.is_file() and os.access(q, os.X_OK):
            return str(q)
    for name in _CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def open_for_sign_in(*, profile_dir: Optional[Path | str] = None) -> dict:
    """Open plain Google Chrome (not automated: no Playwright, no driver) on the
    dedicated profile with one blank tab, so you can sign in to Workday and other
    portals by hand. Nothing is typed or navigated for you; the sessions you
    create are what autofill later reuses. Returns {"ok", "profile"}; raises
    BrowserUnavailable when Chrome isn't installed."""
    import subprocess
    exe = chrome_executable()
    if not exe:
        raise BrowserUnavailable("Google Chrome is not installed")
    profile = Path(profile_dir or PROFILE_DIR).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    args = [exe, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--new-window", "about:blank"]
    subprocess.Popen(_check_args(args), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    log.info("sign-in window opened on the dedicated profile")
    return {"ok": True, "profile": "~/" + str(profile.relative_to(Path.home()))
            if profile.is_relative_to(Path.home()) else profile.name}


__all__ = ["BrowserSession", "BrowserUnavailable", "launch", "launch_ephemeral", "open_for_sign_in",
           "chrome_executable", "PROFILE_DIR"]
