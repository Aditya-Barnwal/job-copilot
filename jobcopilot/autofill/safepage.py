"""SafePage: the only handle adapters get on a browser page.

Writes:  fill, select, check, upload, click (guarded), next_step (Next /
         Continue allowlist only), goto (denylist + same-site), highlight.
Reads:   fields, buttons, labels, text, url, title, meta, count, has_value,
         find_next, find_submit, frame_urls.

There is deliberately no submit / press / type-key / evaluate / raw-click
method, and the raw Playwright page is never handed out. Anything
submit-like raises SubmitGuardViolation; consent boxes raise
ConsentRefused; honeypots raise HoneypotRefused; password fields raise
PasswordRefused. Logs carry field labels and sources, never values.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from . import detect
from .guard import (ConsentRefused, DeniedHost, Guard, Halted, guard_for, HoneypotRefused, OffSite,
                    PasswordRefused, SafePageError, SubmitGuardViolation, host_matches, host_of,
                    is_consent_label, is_denied, is_next_like, is_submit_like, site_of, SUBMIT_RE,
                    norm_name)

log = logging.getLogger("jobcopilot.autofill.safepage")

ACTION_TIMEOUT_MS = 10_000
NAV_TIMEOUT_MS = 30_000

# --------------------------------------------------------------------------- in-page inspector
# Returns structure only: labels, names, types, option texts, flags. Never the
# current value of a text field (that may be personal data); only has_value.
_LIB = r"""
const __txt = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
const __q = (v) => '"' + String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
const __uniq = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
const __byIds = (ids) => ids.split(/\s+/).map(i => document.getElementById(i)).filter(Boolean).map(__txt).join(' ').trim();
const __labelOf = (el) => {
  const lb = el.getAttribute('aria-labelledby'); if (lb) { const t = __byIds(lb); if (t) return t; }
  const al = el.getAttribute('aria-label'); if (al && al.trim()) return al.trim();
  if (el.id) { try { const l = document.querySelector('label[for=' + __q(el.id) + ']'); if (l && __txt(l)) return __txt(l); } catch (e) {} }
  const wrap = el.closest('label'); if (wrap && __txt(wrap)) return __txt(wrap);
  if (el.labels && el.labels.length && __txt(el.labels[0])) return __txt(el.labels[0]);
  if (el.placeholder) return el.placeholder.trim();
  if (el.title) return el.title.trim();
  return '';
};
const __groupLabel = (el) => {
  const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg && __txt(lg)) return __txt(lg); }
  const g = el.closest('[role=radiogroup],[role=group]');
  if (g) { const lb = g.getAttribute('aria-labelledby'); if (lb) { const t = __byIds(lb); if (t) return t; }
           const al = g.getAttribute('aria-label'); if (al) return al.trim(); }
  return '';
};
const __accName = (el) => {
  const lb = el.getAttribute('aria-labelledby'); if (lb) { const t = __byIds(lb); if (t) return t; }
  const al = el.getAttribute('aria-label'); if (al && al.trim()) return al.trim();
  const t = __txt(el); if (t) return t.slice(0, 200);
  if (el.tagName === 'INPUT' && ['submit','button','reset','image'].includes((el.type||'').toLowerCase())) return (el.value || el.alt || '').trim();
  return (el.title || '').trim();
};
const __selector = (el) => {
  const tag = el.tagName.toLowerCase();
  if (el.id && __uniq('#' + CSS.escape(el.id))) return '#' + CSS.escape(el.id);
  const typ = (el.getAttribute('type') || '').toLowerCase();
  if (el.name) {
    let s = tag + '[name=' + __q(el.name) + ']';
    if ((typ === 'radio' || typ === 'checkbox') && el.hasAttribute('value')) s += '[value=' + __q(el.getAttribute('value')) + ']';
    if (__uniq(s)) return s;
  }
  const ai = el.getAttribute('data-automation-id'); if (ai && __uniq('[data-automation-id=' + __q(ai) + ']')) return '[data-automation-id=' + __q(ai) + ']';
  const parts = []; let n = el;
  while (n && n.nodeType === 1 && n !== document.documentElement) {
    if (n !== el && n.id && __uniq('#' + CSS.escape(n.id))) { parts.unshift('#' + CSS.escape(n.id)); break; }
    let i = 1, s = n; while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) i++;
    parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + i + ')'); n = n.parentElement;
  }
  return parts.join(' > ');
};
const __meta = (el) => {
  const tag = el.tagName.toLowerCase();
  const typ = (el.type || '').toLowerCase();
  const typeAttr = (el.getAttribute('type') || '').toLowerCase();
  const cs = getComputedStyle(el), r = el.getBoundingClientRect();
  const vis = el.checkVisibility ? el.checkVisibility({opacityProperty: true, visibilityProperty: true}) : (cs.display !== 'none' && cs.visibility !== 'hidden');
  const tiny = r.width <= 1 || r.height <= 1;
  const docW = Math.max(document.documentElement.scrollWidth, window.innerWidth);
  const off = (r.right + window.scrollX) <= 0 || (r.bottom + window.scrollY) <= 0 || (r.left + window.scrollX) > docW + 50;
  const buttonish = tag === 'button' || tag === 'a' || ['submit','button','reset','image'].includes(typ) || el.getAttribute('role') === 'button';
  const label = __labelOf(el);
  const m = {
    tag, type: tag === 'select' ? 'select' : (tag === 'textarea' ? 'textarea' : typ), type_attr: typeAttr,
    name: el.getAttribute('name') || '', id: el.id || '', label, group_label: __groupLabel(el),
    name_text: buttonish ? __accName(el) : label, aria_label: el.getAttribute('aria-label') || '',
    text: buttonish ? __txt(el).slice(0, 200) : '', title: el.title || '',
    value: (buttonish && tag === 'input') ? (el.value || '') : ((typ === 'radio' || typ === 'checkbox') ? (el.getAttribute('value') || '') : ''),
    automation_id: el.getAttribute('data-automation-id') || '', role: el.getAttribute('role') || '',
    in_form: !!el.form || !!el.closest('form'), form_attr: el.getAttribute('form') || '',
    formaction: el.getAttribute('formaction') || '', href: tag === 'a' ? (el.getAttribute('href') || '') : '',
    required: !!(el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label)),
    hidden: !vis || tiny, offscreen: off, aria_hidden: !!el.closest('[aria-hidden=true]'),
    tabindex: el.getAttribute('tabindex') || '', autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
    placeholder: el.getAttribute('placeholder') || '', accept: el.getAttribute('accept') || '',
    multiple: !!el.multiple, checked: !!el.checked, disabled: !!el.disabled, readonly: !!el.readOnly,
    has_value: (tag === 'select') ? el.selectedIndex > 0 : ((typ === 'checkbox' || typ === 'radio') ? !!el.checked : !!(el.value && String(el.value).length) || (typ === 'file' && el.files && el.files.length > 0)),
    selector: __selector(el),
  };
  if (tag === 'select') m.options = Array.from(el.options).map(o => ({label: __txt(o) || o.label || '', value: o.value, disabled: !!o.disabled}));
  return m;
};
"""
_JS_META = "(el) => {" + _LIB + " return __meta(el); }"
_JS_FIELDS = ("() => {" + _LIB + " return Array.from(document.querySelectorAll('input, select, textarea'))"
              ".filter(el => !['hidden','submit','button','reset','image'].includes((el.type||'').toLowerCase()))"
              ".map(__meta); }")
_JS_BUTTONS = ("() => {" + _LIB + " return Array.from(document.querySelectorAll("
               "'button, input[type=submit], input[type=button], input[type=image], a[href], [role=button]'))"
               ".map(__meta); }")
_JS_HIGHLIGHT = r"""(el, a) => {
  el.setAttribute('data-jc-highlight', a.kind);
  el.style.setProperty('outline', '3px solid ' + a.color, 'important');
  el.style.setProperty('outline-offset', '2px', 'important');
  let box = document.getElementById('__jc_overlay');
  if (!box) { box = document.createElement('div'); box.id = '__jc_overlay';
    box.setAttribute('aria-hidden', 'true');
    box.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
    document.documentElement.appendChild(box); }
  const r = el.getBoundingClientRect();
  const b = document.createElement('span');
  b.textContent = a.text;
  b.style.cssText = 'position:absolute;pointer-events:none;font:600 11px/1.4 system-ui,sans-serif;color:#fff;' +
    'padding:1px 6px;border-radius:8px;white-space:nowrap;background:' + a.color + ';' +
    'left:' + (r.left + window.scrollX) + 'px;top:' + Math.max(0, r.top + window.scrollY - 18) + 'px;';
  box.appendChild(b);
}"""
_HIGHLIGHT = {"needs_you": ("#d97706", "needs you"), "uncertain": ("#d97706", "needs you"),
              "submit": ("#15803d", "Submit: press it yourself"), "error": ("#b91c1c", "check this")}


# A group legend that asks for agreement (vs "I identify as (select all)").
_STRONG_CONSENT_RE = re.compile(r"authori[sz]|declare|hereby|accept|agree|acknowledge|confirm|"
                                r"certify|consent|attest|terms|privacy|policy|gdpr")


def applications_root() -> Path:
    """data/applications (next to the DB; tests point the DB at tmp_path)."""
    env = os.environ.get("JOBCOPILOT_APPLICATIONS_DIR")
    if env:
        return Path(env)
    from .. import store
    return Path(store.resolve_db_path()).parent / "applications"


def job_dir(job_key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(job_key or "job")).strip("._")[:120] or "job"
    return applications_root() / safe


class SafePage:
    __slots__ = ("_SafePage__page", "guard", "allowed_hosts", "allowed_uploads", "_halted",
                 "_site", "confirmation_url_patterns")

    def __init__(self, page: Any, *, submit_patterns: Iterable[Any] = (),
                 allow_post_patterns: Iterable[Any] = (), hosts: Iterable[str] = (),
                 guard: Optional[Guard] = None) -> None:
        self.__page = page
        if guard is not None:
            guard.add_patterns(submit=submit_patterns, allow_post=allow_post_patterns)
            self.guard = guard.install()
        else:
            self.guard = guard_for(page.context, submit_patterns=submit_patterns,
                                   allow_post_patterns=allow_post_patterns)
        self._halted: Optional[str] = "handed over to the user" if self.guard.released else None
        self.allowed_hosts: list[str] = list(hosts or ())
        self.allowed_uploads: Optional[set[str]] = None
        self._site: Optional[str] = None
        page.set_default_timeout(ACTION_TIMEOUT_MS)

    def __repr__(self) -> str:
        return f"SafePage(host={host_of(self.url)!r}, halted={self._halted!r})"

    # ------------------------------------------------------------------ state
    @property
    def halted(self) -> Optional[str]:
        return self._halted

    def halt(self, reason: str) -> None:
        """Stop writing for the rest of this session (CAPTCHA, login wall...)."""
        if not self._halted:
            self._halted = reason
            log.info("safepage halted: %s", reason)

    def handover(self) -> None:
        """Give the browser to the user: guards off so THEIR click on Submit
        works; this SafePage never writes again."""
        self.guard.release()
        self.halt("handed over to the user")

    def _writable(self) -> None:
        if self._halted:
            raise Halted(self._halted)

    # ------------------------------------------------------------------ resolution
    def _one(self, locator: str) -> Any:
        if not isinstance(locator, str) or not locator.strip():
            raise SafePageError("empty locator")
        if "enter-frame" in locator or "frame_locator" in locator:
            raise SafePageError("locators may not reach into frames")
        loc = self.__page.locator(locator)
        n = loc.count()
        if n != 1:
            raise SafePageError(f"locator matched {n} elements (need exactly 1)")
        return loc

    def meta(self, locator: str) -> dict:
        return self._one(locator).evaluate(_JS_META)

    # ------------------------------------------------------------------ writes
    def fill(self, locator: str, value: str, *, label: str = "", source: str = "") -> None:
        self._writable()
        loc = self._one(locator)
        m = loc.evaluate(_JS_META)
        if m["type"] == "password" or m["autocomplete"] in ("current-password", "new-password"):
            raise PasswordRefused("password fields are never typed")
        if detect.honeypot(m):
            raise HoneypotRefused(f"honeypot field refused: {m.get('name') or m.get('id')}")
        if m["tag"] not in ("input", "textarea") or m["type"] in (
                "file", "checkbox", "radio", "submit", "button", "image", "reset", "hidden"):
            raise SafePageError(f"fill() needs a text field, got {m['tag']}/{m['type']}")
        if m["disabled"] or m["readonly"]:
            raise SafePageError("field is disabled / read-only")
        loc.fill(str(value))
        log.info("filled %r from %s", label or m.get("label") or m.get("name"), source or "?")

    def select(self, locator: str, value: str, *, label: str = "", source: str = "") -> None:
        """Native <select>: by option label, then by value. Otherwise a custom
        combobox: open it (guarded click) and pick the role=option by name."""
        self._writable()
        loc = self._one(locator)
        m = loc.evaluate(_JS_META)
        if detect.honeypot(m):
            raise HoneypotRefused("honeypot select refused")
        if m["tag"] == "select":
            labels = [o["label"] for o in m.get("options", [])]
            if value in labels:
                loc.select_option(label=value)
            elif value in [o["value"] for o in m.get("options", [])]:
                loc.select_option(value=value)
            else:
                raise SafePageError("option not present")
        else:
            self._guarded_click(loc, m)
            opt = self.__page.get_by_role("option", name=str(value), exact=True)
            if opt.count() != 1:
                raise SafePageError(f"combobox option matched {opt.count()} elements")
            om = opt.evaluate(_JS_META)
            if is_submit_like(om):
                raise SubmitGuardViolation("option looks like a submit control")
            opt.click()
        log.info("selected %r from %s", label or m.get("label") or m.get("name"), source or "?")

    def check(self, locator: str, *, label: str = "", source: str = "") -> None:
        """Tick a checkbox or choose a radio. Consent / attestation boxes are refused."""
        self._writable()
        loc = self._one(locator)
        m = loc.evaluate(_JS_META)
        kind = m["type"] if m["type"] in ("checkbox", "radio") else (m.get("role") or "")
        if kind not in ("checkbox", "radio"):
            raise SafePageError("check() needs a checkbox or radio")
        texts = [m.get("label"), m.get("aria_label"), label]
        group = m.get("group_label") or ""
        if kind == "checkbox" and (any(is_consent_label(t) for t in texts if t)
                                   or (group and _STRONG_CONSENT_RE.search(group.lower()))):
            raise ConsentRefused("consent / attestation checkbox: only you can tick this")
        if kind == "radio" and any(is_consent_label(t, kind="radio") for t in texts if t):
            raise ConsentRefused("consent radio: only you can choose this")
        if m["disabled"]:
            raise SafePageError("control is disabled")
        if not m["checked"]:
            if m["hidden"]:
                loc.set_checked(True, force=True)
            else:
                loc.check()
        log.info("checked %r from %s", label or m.get("label") or m.get("name"), source or "?")

    def choose(self, group_locator: str, option: str, *, label: str = "", source: str = "") -> None:
        """Pick one radio / checkbox out of a group locator (e.g. [name="q1"])
        by its option label (or value attribute); then check() it."""
        self._writable()
        loc = self.__page.locator(group_locator)
        n = loc.count()
        if n == 0:
            raise SafePageError("group locator matched nothing")
        metas = [loc.nth(i).evaluate(_JS_META) for i in range(min(n, 60))]
        want = norm_name(option)
        hits = [m for m in metas if norm_name(m.get("label")) == want] or \
               [m for m in metas if want and norm_name(m.get("value")) == want]
        if len(hits) != 1:
            raise SafePageError(f"option matched {len(hits)} controls in the group")
        self.check(hits[0]["selector"], label=label, source=source)

    def upload(self, locator: str, path: str, *, label: str = "", source: str = "") -> None:
        self._writable()
        p = Path(path).expanduser()
        if not p.is_file():
            raise SafePageError("upload file missing")
        if self.allowed_uploads is not None and str(p.resolve()) not in self.allowed_uploads:
            raise SafePageError("upload path is not one of the plan's files")
        loc = self._one(locator)
        m = loc.evaluate(_JS_META)
        if m["tag"] != "input" or m["type"] != "file":
            raise SafePageError("upload() needs an <input type=file>")
        loc.set_input_files(str(p))
        log.info("attached %s to %r", source or "file", label or m.get("label") or m.get("name"))

    def click(self, locator: str) -> None:
        """Guarded click for non-submitting controls (Add another, open a
        section). Submit-like controls raise SubmitGuardViolation; links that
        leave the page are refused (use goto)."""
        self._writable()
        loc = self._one(locator)
        self._guarded_click(loc, loc.evaluate(_JS_META))

    def _guarded_click(self, loc: Any, m: dict) -> None:
        if is_submit_like(m):
            log.warning("refused click on a submit-like control (%s)", m.get("tag"))
            raise SubmitGuardViolation("refusing to click a submit-like control")
        href = (m.get("href") or "").strip()
        if m["tag"] == "a" and href and not href.startswith("#") and not href.lower().startswith("javascript:void"):
            raise SafePageError("links that navigate are not clicked; use goto()")
        if m["tag"] == "input" and m["type"] in ("password",):
            raise PasswordRefused("password fields are never touched")
        loc.click()

    def next_step(self, locator: str) -> None:
        """Press a Next / Continue / Save-and-continue control, nothing else."""
        self._writable()
        loc = self._one(locator)
        m = loc.evaluate(_JS_META)
        if is_submit_like(m):
            log.warning("refused next_step on a submit-like control")
            raise SubmitGuardViolation("next_step refused: control is submit-like")
        if not is_next_like(m):
            raise SubmitGuardViolation("next_step refused: not a Next / Continue control")
        href = (m.get("href") or "").strip()
        if m["tag"] == "a" and href and not href.startswith("#"):
            raise SafePageError("next_step on a navigating link refused; use goto()")
        with self.guard.allow_navigation():
            loc.click()
            self.__page.wait_for_timeout(400)
            try:
                self.__page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 — SPA steps don't load anything
                pass
        log.info("pressed %r", norm_name(m.get("name_text")))

    def goto(self, url: str) -> None:
        """Open a URL on the application's own site. Do-not-automate hosts
        raise DeniedHost (link out instead); other sites raise OffSite."""
        self._writable()
        if not re.match(r"^https?://", url or "", re.I):
            raise SafePageError("only http(s) URLs")
        entry = is_denied(url)
        if entry:
            log.warning("refused goto: %s is on the do-not-automate list", entry)
            raise DeniedHost(f"do-not-automate site ({entry}): link out only")
        host = host_of(url)
        if self._site is None:
            self._site = site_of(host)
        elif site_of(host) != self._site and not host_matches(host, self.allowed_hosts):
            raise OffSite(f"refusing to leave the application site for {host}")
        with self.guard.allow_navigation():
            self.__page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        final = host_of(self.__page.url)
        if final and is_denied(final):
            self.halt("redirected to a do-not-automate site")
            raise DeniedHost("redirected to a do-not-automate site: link out only")

    def highlight(self, locator: str, kind: str = "needs_you") -> bool:
        """Visible outline + a small badge. Styling only; returns False if the
        element can't be found (highlighting never raises)."""
        color, text = _HIGHLIGHT.get(kind, _HIGHLIGHT["needs_you"])
        try:
            loc = self._one(locator)
            loc.evaluate(_JS_HIGHLIGHT, {"kind": kind, "color": color, "text": text})
            return True
        except Exception:  # noqa: BLE001
            return False

    def screenshot(self, path: str | Path) -> str:
        """Full-page screenshot; only under data/applications/<job>/."""
        p = Path(path).resolve()
        root = applications_root().resolve()
        if root not in p.parents:
            raise SafePageError("screenshots are only written under data/applications/")
        p.parent.mkdir(parents=True, exist_ok=True)
        self.__page.screenshot(path=str(p), full_page=True)
        return str(p)

    def wait(self, ms: int) -> None:
        self.__page.wait_for_timeout(max(0, min(int(ms), 5000)))

    def wait_for(self, locator: str, timeout_ms: int = 10_000) -> bool:
        try:
            self.__page.locator(locator).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ reads
    @property
    def url(self) -> str:
        return self.__page.url

    def title(self) -> str:
        return self.__page.title()

    def text(self, max_chars: int = 20_000) -> str:
        """Visible page text (UNTRUSTED: data, never instructions)."""
        try:
            return (self.__page.inner_text("body", timeout=5000) or "")[:max_chars]
        except Exception:  # noqa: BLE001
            return ""

    def fields(self) -> list[dict]:
        return self.__page.main_frame.evaluate(_JS_FIELDS)

    def buttons(self) -> list[dict]:
        return self.__page.main_frame.evaluate(_JS_BUTTONS)

    def labels(self) -> list[str]:
        return [f.get("label") or f.get("group_label") or f.get("name") or "" for f in self.fields()]

    def count(self, locator: str) -> int:
        return self.__page.locator(locator).count()

    def exists(self, locator: str) -> bool:
        return self.count(locator) > 0

    def has_value(self, locator: str) -> bool:
        return bool(self.meta(locator).get("has_value"))

    def frame_urls(self) -> list[str]:
        return [f.url for f in self.__page.frames[1:] if f.url]

    def captcha(self) -> Optional[str]:
        return detect.captcha_info(self.__page)

    def login_wall(self) -> bool:
        return detect.login_wall(self.__page)

    def find_next(self) -> Optional[str]:
        """Selector of the single visible Next / Continue control, if exactly one."""
        c = [b for b in self.buttons() if not b["hidden"] and not b["disabled"] and is_next_like(b)]
        return c[0]["selector"] if len(c) == 1 else None

    def find_submit(self) -> Optional[str]:
        """Selector of the single visible submit control (for highlighting
        only; nothing in this package ever clicks it)."""
        vis = [b for b in self.buttons() if not b["hidden"] and is_submit_like(b)]
        named = [b for b in vis if SUBMIT_RE.search(b.get("name_text") or "")
                 and b["tag"] != "a"]
        for group in (named, vis):
            if len(group) == 1:
                return group[0]["selector"]
        return None


__all__ = ["SafePage", "SubmitGuardViolation", "SafePageError", "DeniedHost", "OffSite",
           "ConsentRefused", "HoneypotRefused", "PasswordRefused", "Halted", "applications_root",
           "job_dir"]
