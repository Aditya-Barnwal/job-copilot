"""The submit guard: three independent layers so nothing we run can submit
an application (non-negotiable 1).

1. Code level   SafePage has no submit / press / raw-click method; its
                guarded click() and next_step() refuse anything submit-like
                (is_submit_like) and raise SubmitGuardViolation.
2. DOM level    an init script, in every frame of every page, cancels
                `submit` events and HTMLFormElement.submit()/requestSubmit()
                while the session is in fill mode, so a page that submits
                itself on input / change / Enter / a timer gets nowhere.
3. Network      a context route that, in fill mode, aborts every non-GET
                request (default deny; an adapter may allow specific
                save-progress / upload endpoints, never its submit_patterns),
                every POST navigation, every main-frame navigation we didn't
                start (goto / next_step), and every request to a
                do-not-automate host.

handover() turns layers 2 and 3 off so the user's own physical click on
Submit works normally; after that the session never writes again.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any, Iterable, Optional, Pattern

log = logging.getLogger("jobcopilot.autofill.guard")


class SafePageError(RuntimeError):
    """Base class: a SafePage refused an action."""


class SubmitGuardViolation(SafePageError):
    """Something tried to click / activate a submit-like control."""


class DeniedHost(SafePageError):
    """A do-not-automate site (LinkedIn, Indeed, ...): link out only."""


class OffSite(SafePageError):
    """A navigation off the application's own site."""


class ConsentRefused(SafePageError):
    """A consent / attestation checkbox: only the user ticks it."""


class HoneypotRefused(SafePageError):
    """A bot trap (hidden / off-screen / beecatcher-style field)."""


class PasswordRefused(SafePageError):
    """Passwords are never typed."""


class Halted(SafePageError):
    """The session stopped writing (CAPTCHA / login wall / handed over)."""


# --------------------------------------------------------------------------- words

SUBMIT_RE = re.compile(
    r"\b(submit|submitting|apply|applying|send|finish|confirm|complete|done|finali[sz]e|"
    r"place order|sign ?up|register|create (an )?account|i agree)\b", re.I)
NEXT_RE = re.compile(
    r"^(next|continue|next step|next page|save (and|&) continue|save (and|&) next|"
    r"continue to next (step|page))$", re.I)
CONSENT_RE = re.compile(
    r"^(i|im|i am|i have|i will|i do|we)\b|\bby (checking|clicking|ticking|selecting|submitting)\b|"
    r"\bauthori[sz](e|es|ing|ation)\b|\bdeclare\b|\bhereby\b|\baccept\b|\bagree\b|\backnowledge\b|"
    r"\bconfirm that\b|\bcertify\b|\bconsent\b|\bterms\b|\bprivacy\b|\bpolicy\b|\battest\b|"
    r"\bundertak(e|ing)\b|\bpermission\b|\bgdpr\b|\bdata processing\b")
RADIO_CONSENT_RE = re.compile(r"\b(i agree|i accept|i consent|i acknowledge|i certify|i confirm|"
                              r"i attest|i authori[sz]e)\b")


def _norm(s: Any) -> str:
    s = str(s or "").lower().replace("’", "").replace("'", "").replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_name(s: Any) -> str:
    """Accessible name for Next matching: lower, arrows/punctuation stripped, & kept."""
    s = str(s or "").lower().replace("’", "").replace("'", "")
    s = re.sub(r"[^a-z0-9&]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _names(meta: dict) -> list[str]:
    return [str(meta.get(k) or "") for k in
            ("name_text", "text", "aria_label", "value", "title", "id", "name", "automation_id")]


def is_submit_like(meta: dict) -> bool:
    """True for anything that could submit the form or the application.
    `meta` comes from SafePage's element inspector."""
    tag = (meta.get("tag") or "").lower()
    typ = (meta.get("type_attr") or "").lower()
    if tag == "input" and typ in ("submit", "image"):
        return True
    if tag == "button" and typ in ("", "submit") and (meta.get("in_form") or meta.get("form_attr")):
        return True                     # a typeless <button> inside a form submits it
    if meta.get("formaction") or (tag in ("button", "input") and typ == "submit"):
        return True
    if _text_entry(tag, typ, (meta.get("role") or "").lower()):
        return False                    # a field labelled "...applying..." is not a button
    for t in _names(meta):
        if t and SUBMIT_RE.search(t.replace("_", " ").replace("-", " ")):
            return True
    return False


_TEXT_INPUT_TYPES = frozenset({"", "text", "email", "tel", "search", "url", "number", "password",
                               "date", "month", "week", "time", "datetime-local"})
_TEXT_ROLES = frozenset({"combobox", "textbox", "searchbox", "listbox", "spinbutton"})


def _text_entry(tag: str, typ: str, role: str) -> bool:
    """Fields you type into or pick from. Their *label* can say "applying" without
    them being able to submit anything; the structural checks above still apply."""
    if tag in ("textarea", "select"):
        return True
    if tag == "input":
        return typ in _TEXT_INPUT_TYPES
    return tag not in ("button", "a") and role in _TEXT_ROLES


def is_next_like(meta: dict) -> bool:
    name = norm_name(meta.get("name_text") or meta.get("aria_label") or meta.get("text")
                     or meta.get("value") or "")
    return bool(NEXT_RE.match(name)) and not is_submit_like(meta)


def is_consent_label(text: Any, *, kind: str = "checkbox") -> bool:
    t = _norm(text)
    if not t:
        return False
    if kind == "radio":
        return bool(RADIO_CONSENT_RE.search(t))
    return bool(CONSENT_RE.search(t))


# --------------------------------------------------------------------------- hosts

def _norm_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


def denied_hosts() -> tuple[str, ...]:
    from .. import http as H
    extra: Iterable[str] = ()
    try:
        from .. import config
        extra = (config.sources().get("http") or {}).get("denied_hosts") or ()
    except Exception:  # noqa: BLE001 — config problems never shrink the builtin floor
        extra = ()
    return tuple(dict.fromkeys([*H.BUILTIN_DENIED_HOSTS, *[_norm_host(h) for h in extra]]))


def host_of(url: str) -> str:
    try:
        return _norm_host(urllib.parse.urlsplit(url).hostname or "")
    except ValueError:
        return ""


def is_denied(url_or_host: str) -> Optional[str]:
    """The denylist entry the URL's host equals or is a subdomain of, if any."""
    host = host_of(url_or_host) if "://" in url_or_host else _norm_host(url_or_host)
    labels = host.split(".")
    deny = set(denied_hosts())
    for i in range(len(labels)):
        suffix = ".".join(labels[i:])
        if suffix in deny:
            return suffix
    # Country sites of the same brand (glassdoor.co.in, indeed.co.uk, linkedin.cn): stricter
    # than the HTTP client's exact-suffix rule, on purpose: autofill never touches them.
    brand = site_of(host).split(".")[0]
    for d in deny:
        if brand and brand == site_of(d).split(".")[0]:
            return d
    return None


def site_of(host: str) -> str:
    """Registrable-ish domain: last two labels (three for co.uk-style). IPs as-is."""
    host = _norm_host(host)
    if re.fullmatch(r"[\d.]+|\[[0-9a-f:]+\]", host) or "." not in host:
        return host
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "ac", "org", "net", "gov"):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    host = _norm_host(host)
    for p in patterns:
        p = _norm_host(p).lstrip("*").lstrip(".")
        if p and (host == p or host.endswith("." + p)):
            return True
    return False


# --------------------------------------------------------------------------- DOM block

_DOM_BLOCK = r"""
(() => {
  const OFF = '__jcoff_%(token)s';
  if (window[Symbol.for(OFF)]) return;
  try { Object.defineProperty(window, Symbol.for(OFF), {value: true}); } catch (e) {}
  let blocked = true;
  window.addEventListener(OFF, () => { blocked = false; }, {once: true});
  const say = (what) => { try { console.debug('[jobcopilot-guard] blocked ' + what); } catch (e) {} };
  window.addEventListener('submit', (e) => {
    if (blocked) { e.preventDefault(); e.stopImmediatePropagation(); say('submit event'); }
  }, true);
  const P = HTMLFormElement.prototype;
  const s = P.submit, r = P.requestSubmit;
  P.submit = function () { if (blocked) { say('form.submit()'); return; } return s.apply(this, arguments); };
  if (r) P.requestSubmit = function () { if (blocked) { say('form.requestSubmit()'); return; } return r.apply(this, arguments); };
})();
"""
_DOM_RELEASE = "() => { window.dispatchEvent(new Event('__jcoff_%(token)s')); }"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _compile(patterns: Iterable[Any]) -> list[Pattern]:
    out = []
    for p in patterns or ():
        out.append(p if hasattr(p, "search") else re.compile(str(p), re.I))
    return out


class Guard:
    """Installs and releases the DOM + network layers on one browser context."""

    def __init__(self, context: Any, *, submit_patterns: Iterable[Any] = (),
                 allow_post_patterns: Iterable[Any] = ()) -> None:
        self.context = context
        self.token = secrets.token_hex(16)
        self.submit_patterns = _compile(submit_patterns)
        self.allow_post_patterns = _compile(allow_post_patterns)
        self.active = False
        self.released = False
        self.blocked: list[dict] = []          # {kind, method, host} — never bodies or queries
        self._nav_depth = 0
        self._lock = threading.Lock()

    # -- install / release
    def install(self) -> "Guard":
        if self.active or self.released:
            return self
        self.context.add_init_script(_DOM_BLOCK % {"token": self.token})
        for page in list(self.context.pages):      # documents already open
            for frame in page.frames:
                try:
                    frame.evaluate(_DOM_BLOCK % {"token": self.token})
                except Exception:  # noqa: BLE001 — detached / cross-process frames load the init script
                    pass
        self.context.route("**/*", self._route)
        self.active = True
        return self

    def add_patterns(self, *, submit: Iterable[Any] = (), allow_post: Iterable[Any] = ()) -> None:
        self.submit_patterns += _compile(submit)
        self.allow_post_patterns += _compile(allow_post)

    def release(self) -> None:
        """handover(): stop blocking, so the user's own Submit works."""
        if not self.active:
            return
        self.active = False
        self.released = True
        try:
            self.context.unroute("**/*", self._route)
        except Exception:  # noqa: BLE001
            pass
        # New documents: an init script added later runs after ours and switches it off.
        self.context.add_init_script(
            "window.dispatchEvent(new Event('__jcoff_%s'));" % self.token)
        for page in list(self.context.pages):
            for frame in page.frames:
                try:
                    frame.evaluate(_DOM_RELEASE % {"token": self.token})
                except Exception:  # noqa: BLE001
                    pass

    # -- navigation window (SafePage.goto / next_step)
    def allow_navigation(self) -> "_NavWindow":
        return _NavWindow(self)

    # -- network layer
    def _record(self, kind: str, request: Any) -> None:
        entry = {"kind": kind, "method": request.method, "host": host_of(request.url),
                 "at": time.time()}
        self.blocked.append(entry)
        log.info("guard blocked %s %s request to %s", kind, entry["method"], entry["host"])

    def _is_main_frame_nav(self, request: Any) -> bool:
        try:
            return request.is_navigation_request() and request.frame.parent_frame is None
        except Exception:  # noqa: BLE001 — no frame (worker): treat as navigation, i.e. stricter
            return request.is_navigation_request()

    def _route(self, route: Any, request: Any) -> None:
        try:
            if not self.active:
                return route.fallback()
            url, method = request.url, request.method.upper()
            if url.startswith(("data:", "blob:", "about:")):
                return route.fallback()
            if is_denied(url):
                self._record("denied-host", request)
                return route.abort("blockedbyclient")
            if method not in _SAFE_METHODS:
                body = ""
                try:
                    body = (request.post_data or "")[:8192]
                except Exception:  # noqa: BLE001 — binary bodies
                    body = ""
                if request.is_navigation_request():
                    self._record("post-navigation", request)
                    return route.abort("aborted")       # ERR_ABORTED: no error page, form stays
                if any(p.search(url) or (body and p.search(body)) for p in self.submit_patterns):
                    self._record("submit-endpoint", request)
                    return route.abort("blockedbyclient")
                if any(p.search(url) for p in self.allow_post_patterns):
                    return route.fallback()
                self._record("non-get", request)
                return route.abort("blockedbyclient")
            if self._is_main_frame_nav(request) and self._nav_depth <= 0:
                self._record("unrequested-navigation", request)
                return route.abort("aborted")
            return route.fallback()
        except Exception:  # noqa: BLE001 — a failing guard must fail closed
            log.warning("guard route handler failed; aborting the request")
            try:
                route.abort("blockedbyclient")
            except Exception:  # noqa: BLE001
                pass


_GUARDS: dict[int, Guard] = {}


def guard_for(context: Any, *, submit_patterns: Iterable[Any] = (),
              allow_post_patterns: Iterable[Any] = ()) -> Guard:
    """The one Guard of a browser context (created + installed on first use)."""
    g = _GUARDS.get(id(context))
    if g is None or g.context is not context:
        g = Guard(context, submit_patterns=submit_patterns, allow_post_patterns=allow_post_patterns)
        _GUARDS[id(context)] = g
    else:
        g.add_patterns(submit=submit_patterns, allow_post=allow_post_patterns)
    return g.install() if not g.released else g


def handover(page_or_context: Any) -> bool:
    """Release the guard of this page's / context's browser context (the user
    takes over and submits). True if there was a guard to release."""
    ctx = getattr(page_or_context, "context", page_or_context)
    g = _GUARDS.get(id(ctx))
    if g is None or g.context is not ctx:
        return False
    g.release()
    return True


class _NavWindow:
    def __init__(self, guard: Guard) -> None:
        self.guard = guard

    def __enter__(self) -> "_NavWindow":
        with self.guard._lock:
            self.guard._nav_depth += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        with self.guard._lock:
            self.guard._nav_depth -= 1


__all__ = ["Guard", "guard_for", "handover", "SubmitGuardViolation", "SafePageError", "DeniedHost", "OffSite",
           "ConsentRefused", "HoneypotRefused", "PasswordRefused", "Halted",
           "is_submit_like", "is_next_like", "is_consent_label", "is_denied", "denied_hosts",
           "site_of", "host_of", "host_matches", "SUBMIT_RE", "NEXT_RE"]
