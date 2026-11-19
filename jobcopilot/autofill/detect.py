"""Read-only detectors: CAPTCHA, login walls, confirmation pages, honeypots.

None of these touches a challenge frame: CAPTCHA detection reads frame URLs
(known to the browser, not the page) and attributes in the top document.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Iterable, Optional

# Frame URLs of CAPTCHA / bot-challenge providers. A match is only a reason to
# pause when the person can SEE the challenge (docs/DESIGN.md §12): its iframe
# element is rendered, on screen and big enough to be a challenge. Invisible
# widgets (Lever's invisible hCaptcha, the size=invisible reCAPTCHA badge)
# only challenge when the person presses Submit themselves; we fill around
# them and never interact with any challenge frame. A challenge that becomes
# visible later is caught by the re-check before every field.
_CAPTCHA_FRAME_RE = re.compile(
    r"(/recaptcha/(api2|enterprise)/(anchor|bframe))|hcaptcha\.com|newassets\.hcaptcha|"
    r"challenges\.cloudflare\.com|/cdn-cgi/challenge-platform/|arkoselabs\.com|funcaptcha\.com|"
    r"captcha-delivery\.com|geo\.captcha", re.I)
_INVISIBLE_RE = re.compile(r"[?&#]size=invisible\b", re.I)
_CAPTCHA_DOM = (".g-recaptcha:not([data-size=invisible]), .h-captcha:not([data-size=invisible]), "
                ".cf-turnstile, #challenge-form, #challenge-stage, #FunCaptcha, [data-arkose], "
                "iframe[title*='captcha' i]:not([title*='invisible' i]), "
                "iframe[src*='recaptcha/api2/anchor'], iframe[src*='recaptcha/enterprise/anchor'], "
                "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe'], "
                "iframe[src*='hcaptcha'], iframe[src*='challenges.cloudflare.com'], iframe[src*='arkoselabs']")
# Smallest box a person can solve: a compact Turnstile is ~130x120, the reCAPTCHA /
# hCaptcha checkbox ~300x78, an image challenge ~400x580.
MIN_CHALLENGE_W, MIN_CHALLENGE_H = 100, 40

# Visibility of one element in its own document, read from the top-level DOM of
# that document (never from inside a challenge frame).
_JS_SEEN = r"""(el, a) => {
  if (!el || !el.isConnected) return false;
  if (el.checkVisibility && !el.checkVisibility({opacityProperty: true, visibilityProperty: true})) return false;
  const s = getComputedStyle(el);
  if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') === 0) return false;
  const r = el.getBoundingClientRect();
  if (r.width < a.w || r.height < a.h) return false;
  const W = Math.max(document.documentElement.scrollWidth, window.innerWidth);
  const H = Math.max(document.documentElement.scrollHeight, window.innerHeight);
  const x = r.left + window.scrollX, y = r.top + window.scrollY;
  if (x + r.width <= 0 || y + r.height <= 0 || x >= W || y >= H) return false;
  if (a.badge && (el.closest('.grecaptcha-badge') || /[?&#]size=invisible\b/i.test(el.getAttribute('src') || ''))) return false;
  return true;
}"""

_JS_CAPTCHA = r"""(a) => {
  const seen = (el, w, h) => { if (el.checkVisibility && !el.checkVisibility({opacityProperty: true, visibilityProperty: true})) return false;
    const s = getComputedStyle(el); if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') === 0) return false;
    const r = el.getBoundingClientRect(); if (r.width < w || r.height < h) return false;
    const W = Math.max(document.documentElement.scrollWidth, window.innerWidth), H = Math.max(document.documentElement.scrollHeight, window.innerHeight);
    const x = r.left + window.scrollX, y = r.top + window.scrollY;
    return !(x + r.width <= 0 || y + r.height <= 0 || x >= W || y >= H); };
  const quiet = (f) => /[?&#]size=invisible\b/i.test(f.getAttribute('src') || '') || !!f.closest('.grecaptcha-badge');
  for (const el of document.querySelectorAll(a.sel)) {
    if (el.tagName === 'IFRAME') { if (!quiet(el) && seen(el, a.w, a.h)) return true; continue; }
    if (el.closest('.grecaptcha-badge') || !seen(el, a.w, a.h)) continue;
    // a widget box that only holds invisible / hidden challenge frames is not a challenge yet
    const frames = Array.from(el.querySelectorAll('iframe'));
    if (frames.length && frames.every(f => quiet(f) || !seen(f, a.w, a.h))) continue;
    return true;
  }
  return false;
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


def _frame_seen(frame: Any) -> bool:
    """Is this challenge frame's iframe element visible to the person? Its own
    element must be rendered, on screen, big enough and not the invisible
    reCAPTCHA badge; every enclosing iframe must be rendered too. Read from the
    parent documents only. Can't tell (an element we can't read) = visible."""
    try:
        el = frame.frame_element()
    except Exception:  # noqa: BLE001
        return True
    try:
        if not el.evaluate(_JS_SEEN, {"w": MIN_CHALLENGE_W, "h": MIN_CHALLENGE_H, "badge": True}):
            return False
        parent = frame.parent_frame
        for _ in range(8):                         # enclosing iframes (nested challenge hosts)
            if parent is None or parent.parent_frame is None:
                return True
            if not parent.frame_element().evaluate(_JS_SEEN, {"w": 1, "h": 1, "badge": False}):
                return False
            parent = parent.parent_frame
        return True
    except Exception:  # noqa: BLE001 — unreadable: assume the person sees it (pause)
        return True


def captcha_info(page: Any) -> Optional[str]:
    """Why the page shows a challenge the person can see, or None. Hidden,
    zero-size, off-screen and size=invisible challenge frames don't count."""
    try:
        for frame in page.frames:
            u = frame.url or ""
            if not u or not _CAPTCHA_FRAME_RE.search(u) or _INVISIBLE_RE.search(u):
                continue
            if frame is page.main_frame or _frame_seen(frame):    # a challenge page itself: always
                return f"challenge frame on {urllib.parse.urlsplit(u).hostname or 'page'}"
        if page.main_frame.evaluate(_JS_CAPTCHA, {"sel": _CAPTCHA_DOM, "w": MIN_CHALLENGE_W,
                                                   "h": MIN_CHALLENGE_H}):
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
