"""Read-only detectors: CAPTCHA, login walls, confirmation pages, honeypots.

None of these touches a challenge frame: CAPTCHA detection reads frame URLs
(known to the browser, not the page) and attributes in the top document.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Iterable, Optional

# Frame URLs of interactive challenges. Invisible reCAPTCHA (size=invisible /
# v3 badge) is not a challenge until the user submits, so it doesn't pause us.
_CAPTCHA_FRAME_RE = re.compile(
    r"(/recaptcha/(api2|enterprise)/(anchor|bframe))|hcaptcha\.com|newassets\.hcaptcha|"
    r"challenges\.cloudflare\.com|/cdn-cgi/challenge-platform/|arkoselabs\.com|funcaptcha\.com|"
    r"captcha-delivery\.com|geo\.captcha", re.I)
_INVISIBLE_RE = re.compile(r"[?&]size=invisible\b", re.I)
_CAPTCHA_DOM = (".g-recaptcha:not([data-size=invisible]), .h-captcha:not([data-size=invisible]), "
                ".cf-turnstile, #challenge-form, #challenge-stage, #FunCaptcha, [data-arkose], "
                "iframe[title*='captcha' i]:not([title*='invisible' i]), "
                "iframe[src*='recaptcha/api2/anchor'], iframe[src*='hcaptcha'], "
                "iframe[src*='challenges.cloudflare.com'], iframe[src*='arkoselabs']")

_JS_CAPTCHA = """(sel) => {
  const vis = (el) => { const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 1 && r.height > 1; };
  // Only a widget that is actually on screen counts: Lever's invisible hCaptcha
  // leaves a zero-height .h-captcha div on every page.
  return Array.from(document.querySelectorAll(sel)).some(vis);
}"""

_JS_LOGIN = """() => {
  const vis = (el) => { const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0; };
  const pw = Array.from(document.querySelectorAll('input[type=password]')).some(vis);
  const wd = !!document.querySelector('[data-automation-id=signInContent], [data-automation-id=createAccountLink], [data-automation-id=signInLink][aria-current], [data-automation-id=createAccountSubmitButton], [data-automation-id=utilityButtonSignIn][aria-expanded=true]');
  const heads = Array.from(document.querySelectorAll('h1, h2, [role=heading]')).filter(vis)
                 .map(h => (h.innerText || '').trim().toLowerCase()).join(' | ');
  return {pw, wd, heads: heads.slice(0, 2000)};
}"""
_LOGIN_HEAD_RE = re.compile(r"\b(sign in|log in|login|create (an )?account|sign up to apply|verify your email)\b")

# --------------------------------------------------------------------------- confirmation
CONFIRMATION_MARKERS: dict[str, dict[str, tuple[str, ...]]] = {
    "greenhouse": {"url": (r"/confirmation\b", r"[?&]submitted=true"),
                   "text": (r"thank you for applying", r"your application has been submitted",
                            r"application (was )?(successfully )?submitted")},
    "lever": {"url": (r"/thanks\b",),
              "text": (r"application submitted", r"thanks for applying", r"thank you for applying")},
    "ashby": {"url": (r"/application/?(submitted|success)",),
              "text": (r"application submitted", r"thank(s| you) for (applying|your application)",
                       r"successfully submitted")},
    "workday": {"url": (r"jobTasks/completed/application", r"/applicationComplete"),
                "text": (r"application submitted", r"you have successfully applied",
                         r"thank you for applying", r"your application has been submitted")},
    "generic": {"url": (),
                "text": (r"thank you for (applying|your application|submitting)",
                         r"thanks for applying",
                         r"(your )?application (has been |was )?(successfully )?(submitted|received)",
                         r"we(ve| have) received your application",
                         r"you have successfully applied")},
}


def captcha_present(page: Any) -> bool:
    """A visible interactive CAPTCHA / bot challenge (reCAPTCHA checkbox,
    hCaptcha, Turnstile, Arkose, DataDome) anywhere on the page."""
    return captcha_info(page) is not None


def captcha_info(page: Any) -> Optional[str]:
    try:
        for frame in page.frames:
            u = frame.url or ""
            if u and _CAPTCHA_FRAME_RE.search(u) and not _INVISIBLE_RE.search(u):
                return f"challenge frame on {urllib.parse.urlsplit(u).hostname or 'page'}"
        if page.main_frame.evaluate(_JS_CAPTCHA, _CAPTCHA_DOM):
            return "challenge widget on the page"
    except Exception:  # noqa: BLE001 — unreadable page: not evidence either way
        return None
    return None


def login_wall(page: Any) -> bool:
    """A sign-in / create-account wall (visible password field, Workday sign-in)."""
    try:
        info = page.main_frame.evaluate(_JS_LOGIN)
    except Exception:  # noqa: BLE001
        return False
    if info.get("pw") or info.get("wd"):
        return True
    return bool(_LOGIN_HEAD_RE.search(info.get("heads") or ""))


def confirmation_page(url: str, text: str, ats: str = "generic", *,
                      markers: Optional[dict] = None) -> bool:
    """True if `url` / visible `text` look like the ATS's 'application received' page.
    ATS URL markers count alone; text markers need to appear near the top."""
    ats = (ats or "generic").lower()
    sets = [CONFIRMATION_MARKERS.get(ats) or {}, CONFIRMATION_MARKERS["generic"], markers or {}]
    u = url or ""
    head = re.sub(r"\s+", " ", (text or "")[:4000]).lower()
    for m in sets:
        if any(re.search(p, u, re.I) for p in m.get("url", ()) or ()):
            return True
        if any(re.search(p, head, re.I) for p in m.get("text", ()) or ()):
            return True
    return False


# --------------------------------------------------------------------------- honeypots
_HONEYPOT_NAME_RE = re.compile(
    r"beecatcher|honey ?pot|honey_?pot|^hp[_-]|[_-]hp$|website_url|url_confirm|leave (this )?"
    r"(field )?(blank|empty)|do not (fill|enter)|if you are human|bot[_-]?(check|field|trap)|"
    r"\bnospam\b|address2_confirm|fax_only", re.I)
_TEXTLIKE = {"", "text", "email", "tel", "url", "number", "search", "date"}


def honeypot(meta: dict) -> bool:
    """True for a bot trap. `meta` keys (all optional): name, id, label,
    automation_id, autocomplete, tag, type, hidden, offscreen, aria_hidden,
    tabindex. File / checkbox / radio inputs and native selects are often
    visually hidden behind custom widgets, so visibility alone only flags
    text-like inputs and textareas."""
    meta = meta or {}
    for key in ("name", "id", "label", "automation_id", "placeholder"):
        if _HONEYPOT_NAME_RE.search(str(meta.get(key) or "")):
            return True
    tag = str(meta.get("tag") or "input").lower()
    typ = str(meta.get("type") or "").lower()
    textlike = tag == "textarea" or (tag == "input" and typ in _TEXTLIKE)
    if not textlike:
        return False
    if meta.get("aria_hidden") or meta.get("offscreen") or meta.get("hidden"):
        return True
    if str(meta.get("tabindex") or "") == "-1" and str(meta.get("autocomplete") or "").lower() == "off" \
            and not meta.get("label"):
        return True
    return False


def first_match(text: str, patterns: Iterable[str]) -> Optional[str]:
    for p in patterns:
        if re.search(p, text or "", re.I):
            return p
    return None


__all__ = ["captcha_present", "captcha_info", "login_wall", "confirmation_page", "honeypot",
           "CONFIRMATION_MARKERS"]
