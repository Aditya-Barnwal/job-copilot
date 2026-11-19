"""SafePage: the only handle adapters get on a browser page.

Writes:  fill, select, check, upload, click (guarded), next_step (Next /
         Continue allowlist only), goto (denylist + same-site), highlight,
         open_application (once per run, description page -> form; see it).
Reads:   fields, form_fields, buttons, apply_controls, labels, text, url,
         title, meta, count, has_value, find_next, find_submit, frame_urls.

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
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Optional

from . import detect
from .guard import (ConsentRefused, DeniedHost, DeniedHostHalt, Guard, Halted, guard_for, HoneypotRefused,
                    ImplausibleValue, OffSite,
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
// Unit hint: the unit words shown beside a box ("Years" / "Months" suffix boxes,
// an "INR" currency picker, an "Annual" tag, CSS ::before/::after text). Climbs at
// most 5 ancestors and stops before one that holds another field (a select whose
// chosen option is itself a unit, like a currency picker, doesn't count). Only
// words from this fixed vocabulary are returned: never page content or values.
const __UNIT_RX = /(?<![a-z])(per\s+(?:month|annum|year|week|day|hour)|p\.\s?a\.|years?|yrs?|months?|mos|weeks?|days?|lpa|lakhs?|lacs?|crores?|annual(?:ly)?|annum|yearly|monthly|inr|usd|eur|gbp|sgd|aud|cad|aed|rupees?|dollars?)(?![a-z])|([₹$€£])/gi;
const __unitWords = (t, out) => {
  const s = String(t || '').trim(); if (!s || s.length > 40) return;
  for (const m of s.matchAll(__UNIT_RX)) { const w = (m[1] || m[2]).toLowerCase().replace(/\s+/g, ' '); if (!out.includes(w)) out.push(w); }
};
const __isUnitText = (t) => { const s = String(t || '').trim(); if (!s || s.length > 40) return false;
  const o = []; __unitWords(s, o); return o.length > 0 && s.replace(__UNIT_RX, '').replace(/[\s()\[\]:.,\/-]/g, '') === ''; };
const __selText = (s) => (s.selectedIndex >= 0 && s.options[s.selectedIndex]) ? __txt(s.options[s.selectedIndex]) : '';
const __pseudo = (n, out) => { for (const ps of ['::before', '::after']) { let c = '';
  try { c = getComputedStyle(n, ps).content || ''; } catch (e) {}
  if (c && c !== 'none' && c !== 'normal' && /^["']/.test(c)) __unitWords(c.replace(/^["']|["']$/g, ''), out); } };
const __unitHint = (el) => {
  const tag = el.tagName.toLowerCase(), typ = (el.type || '').toLowerCase();
  if (!(tag === 'select' || (tag === 'input' && ['text', 'number', 'search', ''].includes(typ)))) return '';
  const own = new Set(Array.from(el.labels || []));
  const words = [];
  __unitWords(el.getAttribute('placeholder'), words);
  let node = el;
  for (let depth = 0; depth < 5 && node.parentElement; depth++) {
    const up = node.parentElement;
    if (up === document.body || ['FORM', 'FIELDSET', 'BODY', 'HTML'].includes(up.tagName)) break;
    const others = Array.from(up.querySelectorAll('input, select, textarea')).filter(c => c !== el
      && !['hidden', 'submit', 'button', 'reset', 'image'].includes((c.type || '').toLowerCase())
      && !(c.tagName === 'INPUT' && c.readOnly)
      && !(c.tagName === 'SELECT' && __isUnitText(__selText(c))));
    if (others.length) break;
    let budget = 200;
    __pseudo(up, words);
    const tw = document.createTreeWalker(up, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {acceptNode: (n) => {
      if (--budget < 0) return NodeFilter.FILTER_REJECT;
      if (n.nodeType === 3) { const p = n.parentElement;
        if (p && (!p.checkVisibility || p.checkVisibility())) __unitWords(n.nodeValue, words);
        return NodeFilter.FILTER_SKIP; }
      if (n === el || own.has(n)) return NodeFilter.FILTER_REJECT;
      if (n.tagName === 'SELECT') { const t = __selText(n); if (__isUnitText(t)) __unitWords(t, words); return NodeFilter.FILTER_REJECT; }
      if (['OPTION', 'SCRIPT', 'STYLE', 'TEXTAREA', 'NOSCRIPT', 'TEMPLATE', 'BUTTON'].includes(n.tagName)) return NodeFilter.FILTER_REJECT;
      if (n.tagName === 'INPUT') { if (n.readOnly && __isUnitText(n.value)) __unitWords(n.value, words); return NodeFilter.FILTER_REJECT; }
      if (n.checkVisibility && !n.checkVisibility()) return NodeFilter.FILTER_REJECT;
      __pseudo(n, words);
      return NodeFilter.FILTER_ACCEPT; }});
    while (tw.nextNode()) {}
    node = up;
  }
  return words.join(', ');
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
    tag, type: tag === 'select' ? 'select' : (tag === 'textarea' ? 'textarea' : typ), type_attr: typeAttr, el_type: typ,
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
    inputmode: (el.getAttribute('inputmode') || '').toLowerCase(), pattern: el.getAttribute('pattern') || '',
    min: el.getAttribute('min') || '', max: el.getAttribute('max') || '', step: el.getAttribute('step') || '',
    unit_hint: buttonish ? '' : __unitHint(el),
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


_DIGITS_PATTERN_RE = re.compile(r"^\^?(\[0-9\]|\\d)[*+{]")


def numeric_problem(value: Any, meta: dict, label: str = "") -> Optional[str]:
    """The last check before typing into a numeric field (type=number, a box whose
    label or unit hint names a unit, or a digits-only box asking for experience,
    notice or salary): why `value` must not go in, or None. Uses the field's own
    label, the plan's label for it, its placeholder, unit hint and min / max / step."""
    from .. import answers as A
    labels = " ; ".join(dict.fromkeys(str(x) for x in (label, meta.get("label"), meta.get("placeholder")) if x))
    digits = (meta.get("inputmode") in ("numeric", "decimal")
              or bool(_DIGITS_PATTERN_RE.match(str(meta.get("pattern") or ""))))
    return A.implausible_number(value, label=labels, hint=meta.get("unit_hint") or "",
                                numeric=meta.get("type") == "number", digits_only=digits,
                                bounds=(meta.get("min"), meta.get("max"), meta.get("step")))


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


# --------------------------------------------------------------------------- "open the application"
# A posting's description page often shows the form only after an "Apply"
# control is pressed (Keka: "Apply for this job"). Apply-like words are
# submit-like to the guarded click() on purpose, so open_application() is the one
# narrow, explicit way past that page. The control's whole accessible name must
# be one of these phrases ("Submit application" never matches).
APPLY_OPEN_RE = re.compile(
    r"^(apply|apply now|apply here|apply online|apply for (this|the) (job|position|role|opening|vacancy)|"
    r"apply to (this|the) (job|position|role)|apply for job|apply for position|"
    r"im interested|i am interested)$")
# Hosts an "Apply" link may lead to besides the posting's own site: the
# application form often lives on the ATS (a company page -> boards.greenhouse.io).
ATS_APPLY_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "myworkdaysite.com",
                   "keka.com", "smartrecruiters.com", "workable.com", "recruitee.com", "teamtailor.com",
                   "pinpointhq.com", "jobs.personio.com", "jobs.personio.de", "bamboohr.com",
                   "rippling.com", "gem.com", "turbohire.co")
# A search box (site header) is not an application form field.
_SEARCH_RE = re.compile(r"\bsearch\b", re.I)


class SafePage:
    __slots__ = ("_SafePage__page", "guard", "allowed_hosts", "allowed_uploads", "_halted",
                 "_site", "confirmation_url_patterns", "_opened_application")

    def __init__(self, page: Any, *, submit_patterns: Iterable[Any] = (),
                 allow_post_patterns: Iterable[Any] = (), hosts: Iterable[str] = (),
                 guard: Optional[Guard] = None, allow_graphql_reads: Iterable[Any] = ()) -> None:
        self.__page = page
        if guard is not None:
            guard.add_patterns(submit=submit_patterns, allow_post=allow_post_patterns,
                               allow_graphql_reads=allow_graphql_reads)
            self.guard = guard.install()
        else:
            self.guard = guard_for(page.context, submit_patterns=submit_patterns,
                                   allow_post_patterns=allow_post_patterns,
                                   allow_graphql_reads=allow_graphql_reads)
        self._halted: Optional[str] = "handed over to the user" if self.guard.released else None
        self.allowed_hosts: list[str] = list(hosts or ())
        self.allowed_uploads: Optional[set[str]] = None
        self._site: Optional[str] = None
        self._opened_application = False
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
        self.check_site()

    def denied_site(self) -> Optional[str]:
        """Why this page may not be written to because of where it IS (not where
        we asked to go): the guard saw a frame land on a do-not-automate host,
        or the page / one of its frames is on one now (a redirect chain the
        route handler never sees). None when the page is fine."""
        if self.guard.tripped:
            return self.guard.tripped
        urls = [self.__page.url]
        try:
            urls += [f.url for f in self.__page.frames[1:]]
        except Exception:  # noqa: BLE001
            pass
        for u in urls:
            if u and u.startswith(("http:", "https:")):
                entry = is_denied(u)
                if entry:
                    return f"do-not-automate site ({entry}): link out only"
        return None

    def check_site(self) -> None:
        """Raise DeniedHostHalt (and stop writing for good) when the page is on
        a do-not-automate host; nothing is typed there."""
        why = self.denied_site()
        if why:
            self.halt(why)
            log.warning("safepage: %s; writes stop", why)
            raise DeniedHostHalt(why)

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
        why = numeric_problem(value, m, label)
        if why:
            log.info("refused %r: %s", label or m.get("label") or m.get("name"), why)
            raise ImplausibleValue(why)
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
        if m["tag"] == "select" and (m.get("hidden") or m.get("offscreen") or m.get("aria_hidden")):
            raise SafePageError("hidden select: left for you (never set behind your back)")
        if any(is_consent_label(t) for t in (m.get("label"), m.get("group_label"), m.get("aria_label"), label) if t):
            raise ConsentRefused("consent / attestation question: only you can answer this")
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
        # A radio: its option text ("I agree") AND the question it answers (legend,
        # aria-labelledby group, the label passed in) use the checkbox consent rule.
        if kind == "radio" and (any(is_consent_label(t, kind="radio") for t in texts if t)
                                or any(is_consent_label(t) for t in (group, label) if t)):
            raise ConsentRefused("consent radio: only you can choose this")
        if detect.honeypot(m):
            raise HoneypotRefused("honeypot control left alone")
        if m["hidden"] or m.get("offscreen") or m.get("aria_hidden"):
            raise SafePageError("hidden control: left for you (never force-ticked)")
        if m["disabled"]:
            raise SafePageError("control is disabled")
        if not m["checked"]:
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
        p = Path(path).expanduser().resolve()      # check and upload the SAME resolved path
        if not p.is_file():
            raise SafePageError("upload file missing")
        if self.allowed_uploads is not None and str(p) not in self.allowed_uploads:
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
        self.check_site()                 # where did Continue land? (redirects skip the route)

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
        self.check_site()                 # the final URL of any redirect chain, frames included

    def open_application(self) -> str:
        """Get from a posting's description page to its application form, once.

        Why this exists: on many ATSs (Keka, company career pages) the form only
        appears after pressing "Apply" / "Apply for this job". Those words are
        submit-like, so click() and next_step() refuse them, and they must keep
        doing so: on a form page "Apply" IS the final submit. This method is the
        only, explicit exception, and it is narrow by construction:

          * it refuses on any page that already shows form fields (a form page's
            "Apply" may submit it); only a page with nothing to fill qualifies;
          * it acts on exactly one visible control whose WHOLE accessible name is
            an "open the application" phrase (APPLY_OPEN_RE), never one it is handed;
          * an <a href> is never clicked: its URL is opened with a plain GET
            navigation, and only to the posting's own site or a known ATS host
            (ATS_APPLY_HOSTS / the adapter's hosts), never a do-not-automate host;
          * anything else is clicked only when it is not an <input>, not inside a
            <form> (nor tied to one with form=), has no type="submit" attribute and
            no formaction: with no form owner a click can't submit anything. A
            typeless <button> outside every form is allowed for that reason;
          * the guard stays fully on: every non-GET request is still aborted
            (a page that POSTs as it loads gets nowhere), the denylist is still
            checked, and check_site() halts the session if a redirect lands on a
            denied host; the click only opens a navigation window, like next_step;
          * it can be used once per SafePage (one run), even if it fails.

        Returns the URL the page is on afterwards. Raises SafePageError (or a
        subclass) and does nothing when any rule isn't met."""
        self._writable()
        if self._opened_application:
            raise SafePageError("open_application() is allowed once per run")
        self._opened_application = True          # set first: a failed attempt doesn't earn a second
        if self.form_fields():
            raise SafePageError("this page already has form fields: nothing to open")
        cands = self.apply_controls()
        if len(cands) != 1:
            raise SafePageError(f"found {len(cands)} 'Apply' controls (need exactly 1)")
        m = cands[0]
        here = self.__page.url
        href = (m.get("href") or "").strip() if m.get("tag") == "a" else ""
        if href and not href.startswith("#") and not href.lower().startswith("javascript:"):
            url = urllib.parse.urljoin(here, href)
            if not re.match(r"^https?://", url, re.I):
                raise SafePageError("only http(s) links are opened")
            entry = is_denied(url)
            if entry:
                raise DeniedHost(f"do-not-automate site ({entry}): link out only")
            if not self._apply_host_ok(host_of(url), here):
                raise OffSite(f"the Apply link leaves for {host_of(url)}: open it yourself")
            with self.guard.allow_navigation():
                self.__page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        else:
            loc = self._one(m["selector"])
            m = loc.evaluate(_JS_META)               # re-read the element we will actually click
            if not APPLY_OPEN_RE.match(norm_name(m.get("name_text"))):
                raise SafePageError("the control changed under us")
            if m["tag"] not in ("button", "a") and m.get("role") != "button":
                raise SubmitGuardViolation("open_application only presses a button or link")
            if m.get("in_form") or m.get("form_attr"):
                raise SubmitGuardViolation("the Apply control belongs to a form: it may submit it")
            if m.get("formaction") or (m.get("type_attr") or "").lower() in ("submit", "image"):
                raise SubmitGuardViolation("the Apply control is a submit button")
            if m.get("disabled") or m.get("hidden"):
                raise SafePageError("the Apply control is hidden or disabled")
            with self.guard.allow_navigation():
                loc.click()
                self.__page.wait_for_timeout(500)
                try:
                    self.__page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
                except Exception:  # noqa: BLE001 — a modal / SPA view loads nothing
                    pass
        self.check_site()                        # a redirect chain's end, frames included
        if not self._apply_host_ok(host_of(self.__page.url), here):
            self.halt("the Apply control left the application site")
            raise OffSite(f"the Apply control led to {host_of(self.__page.url)}: open it yourself")
        self.settle()
        log.info("opened the application form (%s)", "link" if href else "button")
        return self.__page.url

    def _apply_host_ok(self, host: str, here: str) -> bool:
        cur = host_of(here)
        return bool(host) and (host == cur or site_of(host) == site_of(cur)
                               or host_matches(host, self.allowed_hosts)
                               or host_matches(host, ATS_APPLY_HOSTS))

    def settle(self, timeout_ms: int = 5000) -> None:
        """Let a client-rendered page finish loading (read-only wait; capped)."""
        try:
            self.__page.wait_for_load_state("networkidle", timeout=max(0, min(int(timeout_ms), 10_000)))
        except Exception:  # noqa: BLE001 — busy pages never go idle
            pass

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

    def form_fields(self) -> list[dict]:
        """Fields a person could fill on this page: visible, enabled inputs,
        selects and textareas (file inputs even when styled away), search boxes
        excluded. Empty on a description page whose form isn't open yet."""
        out = []
        for f in self.fields():
            if f.get("type") == "file":
                out.append(f)
                continue
            if f.get("hidden") or f.get("offscreen") or f.get("aria_hidden") or f.get("disabled"):
                continue
            if f.get("type") == "search" or f.get("role") == "searchbox":
                continue
            hay = " ".join(str(f.get(k) or "") for k in ("label", "name", "id", "placeholder", "aria_label"))
            if _SEARCH_RE.search(hay.replace("_", " ").replace("-", " ")):
                continue
            out.append(f)
        return out

    def apply_controls(self) -> list[dict]:
        """Visible, enabled controls whose whole name is an "open the
        application" phrase. Links to the same URL count once."""
        out: list[dict] = []
        seen: set[str] = set()
        for b in self.buttons():
            if b.get("hidden") or b.get("aria_hidden") or b.get("disabled"):
                continue
            if not APPLY_OPEN_RE.match(norm_name(b.get("name_text"))):
                continue
            href = (b.get("href") or "").strip() if b.get("tag") == "a" else ""
            if href and not href.startswith("#") and not href.lower().startswith("javascript:"):
                key = urllib.parse.urljoin(self.__page.url, href).split("#", 1)[0]
                if key in seen:
                    continue
                seen.add(key)
            out.append(b)
        return out

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
        # The description page's own "Apply for this job" (a button with no form
        # owner, still on screen behind a modal form) opens the form; it can't
        # submit one. Set aside when there are other candidates.
        rest = [b for b in vis if not (APPLY_OPEN_RE.match(norm_name(b.get("name_text")))
                                       and not b.get("in_form") and not b.get("form_attr")
                                       and not b.get("formaction") and b["tag"] != "input"
                                       and (b.get("type_attr") or "").lower() != "submit")]
        vis = rest or vis
        named = [b for b in vis if SUBMIT_RE.search(b.get("name_text") or "")
                 and b["tag"] != "a"]
        for group in (named, vis):
            if len(group) == 1:
                return group[0]["selector"]
        return None


__all__ = ["SafePage", "SubmitGuardViolation", "SafePageError", "DeniedHost", "DeniedHostHalt", "OffSite",
           "ConsentRefused", "HoneypotRefused", "PasswordRefused", "Halted", "applications_root",
           "job_dir"]
