"""Find which ATS (and which board id) a company's careers page uses.

Companies keep moving between ATSs and board tokens (v1's 404s were mostly
stale slugs), so instead of guessing we read the careers page the way a human
would and look for the ATS's fingerprints, in the order docs/SOURCES.md lists:

  1. the URL itself / final URL after redirects (jobs.ashbyhq.com/<slug>, ...)
  2. regexes over href/src/raw HTML (incl. JSON "applyUrl" on Phenom front ends)
  3. <iframe src> -> that page -> its JS bundles, grepped for the Greenhouse token
  4. gh_jid links or one job-detail link -> that page
  5. nothing -> "manual"

then confirm with ONE call to the very endpoint the connector will use, so a
detection is only trusted once a board actually answers. All I/O goes through
PoliteClient (robots, rate limits, denylist); a detection costs at most
MAX_FETCHES page loads plus one API call. Network/robots failures never raise:
they come back as ats=None with the reason in `evidence`.
"""
from __future__ import annotations

import html as _html
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from .http import HttpError, HttpStatusError, PoliteClient, default_client

log = logging.getLogger("jobcopilot.ats_detect")

MAX_FETCHES = 6          # page/JS loads per detect() call, excluding the verify call
# Untrusted pages: hard caps on what we download (PoliteClient max_bytes), on how
# long one fetch and a whole detect() may take, and on how much text the regexes see.
MAX_HTML_BYTES = 2_000_000
MAX_JS_BYTES = 3_000_000
FETCH_DEADLINE_SEC = 20.0
DETECT_BUDGET_SEC = 90.0
SCAN_HTML_CHARS = 1_000_000
SCAN_JS_CHARS = 2_000_000
MAX_URL_CHARS = 8_192

API_ATS = ("greenhouse", "lever", "ashby", "workday", "smartrecruiters", "gem")


@dataclass
class AtsResult:
    ats: Optional[str] = None            # greenhouse|lever|ashby|workday|smartrecruiters|gem|turbohire|manual|None
    slug: Optional[str] = None
    tenant: Optional[str] = None         # workday
    wd_server: Optional[str] = None      # workday, e.g. "wd1"
    site: Optional[str] = None           # workday
    region: Optional[str] = None         # "eu" or None
    confidence: float = 0.0
    evidence: str = ""
    verified: bool = False
    job_count: Optional[int] = None
    _notes: list[str] = field(default_factory=list, repr=False, compare=False)

    def note(self, msg: str) -> None:
        self._notes.append(msg)
        self.evidence = "; ".join(self._notes)


# --------------------------------------------------------------- patterns
# Each yields (ats, groups). Slug charsets follow the ATSs' own URL rules.
# Every pattern runs on untrusted text, so none may backtrack super-linearly: an
# unbounded run is never followed by a literal unless the run starts right after
# a literal, host labels before a literal domain are {1,63} and may only start
# where a label starts (lookbehind), and attribute/query spans are bounded.
_GH = re.compile(
    r"(?:job-)?boards(?P<eu>\.eu)?\.greenhouse\.io/"
    r"(?:embed/job_(?:board(?:/js)?|app)\?(?:[^\"'<>\s]{0,256}?&(?:amp;)?)?for=)?(?P<slug>[\w-]+)", re.I)
_GH_API = re.compile(r"boards-api(?P<eu>\.eu)?\.greenhouse\.io/v1/boards/(?P<slug>[\w-]+)", re.I)
_GH_TOKEN = re.compile(r"""BOARD_TOKEN["']?\s*[:=]\s*["'](?P<slug>[\w-]+)["']""")
_GH_JID = re.compile(r"[?&](?:amp;)?gh_jid=(\d+)", re.I)
_ASHBY = re.compile(r"jobs\.ashbyhq\.com/(?P<slug>[\w.%-]+)", re.I)
_ASHBY_API = re.compile(r"api\.ashbyhq\.com/posting-api/job-board/(?P<slug>[\w.%-]+)", re.I)
_LEVER = re.compile(r"jobs(?P<eu>\.eu)?\.lever\.co/(?P<slug>[\w.-]+)", re.I)
_WD_CXS = re.compile(
    r"(?<![\w-])(?P<tenant>[\w-]{1,63})\.(?P<wd>wd\d{1,3})\.myworkdayjobs\.com"
    r"/wday/cxs/[\w-]{1,100}/(?P<site>[\w-]+)", re.I)
_WD = re.compile(
    r"(?<![\w-])(?P<tenant>[\w-]{1,63})\.(?P<wd>wd\d{1,3})\.myworkdayjobs\.com"
    r"/(?:[a-z]{2}-[A-Za-z]{2}/)?(?P<site>[\w-]+)", re.I)
_SR = re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/(?P<slug>[\w-]+)", re.I)
_GEM = re.compile(r"jobs\.gem\.com/(?P<slug>[\w-]+)", re.I)
_TURBOHIRE = re.compile(r"(?<![\w-])(?P<slug>[\w-]{1,63})\.turbohire\.co\b", re.I)
_PHENOM = re.compile(r"cdn\.phenompeople\.com|\"refNum\"\s{0,16}:", re.I)
_APPLY_URL = re.compile(r"\"applyUrl\"\s{0,16}:\s{0,16}\"([^\"]{1,2048})\"")


def _tag_attr(tag: str, attr: str) -> re.Pattern[str]:
    """<tag ... attr="value">: the span before the attribute stops at '<' or '>' and
    is bounded, so '<a <a <a ...' without a closing '>' stays linear."""
    return re.compile(rf"<{tag}\b[^<>]{{0,2048}}?\b{attr}\s{{0,16}}=\s{{0,16}}"
                      rf"[\"']([^\"'<>]{{1,2048}})[\"']", re.I)


_IFRAME = _tag_attr("iframe", "src")
_SCRIPT = _tag_attr("script", "src")
_HREF = _tag_attr("a", "href")
_JOB_LINK = re.compile(r"/(?:jobs?|careers?|positions?|openings?|roles?)/[\w%-]{3,}", re.I)

# Path words that sit where a slug would but are not board ids.
_NOT_SLUGS = frozenset({
    "embed", "v1", "js", "api", "job_app", "job_board", "jobs", "search", "assets",
    "static", "favicon", "images", "css", "www", "cdn", "careers", "wday", "en-us",
})

# Workday sites can legitimately be "jobs" (PayPal), so they get their own, shorter list.
_NOT_WD_SITES = frozenset({"wday", "en-us"})

# Cost of a wrong guess differs by signal; these feed `confidence`.
CONF_URL, CONF_LINK, CONF_APPLY_URL, CONF_BUNDLE, CONF_DETAIL = 0.95, 0.85, 0.8, 0.8, 0.7


def _clean(text: str) -> str:
    """Undo the escaping that hides URLs inside JSON/JS/HTML attributes."""
    text = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    return _html.unescape(text)


# Every id we pull out of a page ends up in an API URL path; anything else ('/',
# '..', '?', '%', non-ASCII) is refused, so a hostile page can't steer a request.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _valid_id(value: Optional[str]) -> bool:
    return bool(value) and _ID_RE.fullmatch(value) is not None


def _ok_slug(slug: Optional[str]) -> bool:
    return _valid_id(slug) and slug.lower() not in _NOT_SLUGS


def _scan(text: str) -> list[AtsResult]:
    """Every ATS fingerprint in `text`, strongest-first per ATS, unranked across ATSs."""
    found: list[AtsResult] = []
    for rx in (_GH_API, _GH):
        for m in rx.finditer(text):
            if _ok_slug(m.group("slug")):
                found.append(AtsResult("greenhouse", slug=m.group("slug"),
                                       region="eu" if m.group("eu") else None,
                                       evidence=m.group(0)))
    for rx in (_ASHBY_API, _ASHBY):
        for m in rx.finditer(text):
            slug = urllib.parse.unquote(m.group("slug")).rstrip(".")
            if _ok_slug(slug):
                found.append(AtsResult("ashby", slug=slug, evidence=m.group(0)))
    for m in _LEVER.finditer(text):
        if _ok_slug(m.group("slug")):
            found.append(AtsResult("lever", slug=m.group("slug"),
                                   region="eu" if m.group("eu") else None, evidence=m.group(0)))
    for rx in (_WD_CXS, _WD):
        for m in rx.finditer(text):
            if (_valid_id(m.group("tenant")) and _valid_id(m.group("site"))
                    and m.group("site").lower() not in _NOT_WD_SITES):
                found.append(AtsResult("workday", tenant=m.group("tenant").lower(),
                                       wd_server=m.group("wd").lower(), site=m.group("site"),
                                       evidence=m.group(0)))
    for rx, ats in ((_SR, "smartrecruiters"), (_GEM, "gem"), (_TURBOHIRE, "turbohire")):
        for m in rx.finditer(text):
            if _ok_slug(m.group("slug")):
                found.append(AtsResult(ats, slug=m.group("slug"), evidence=m.group(0)))
    return found


def _pick(found: list[AtsResult], confidence: float) -> Optional[AtsResult]:
    """Most-cited board wins (a footer link to another company's board loses to the
    page's own repeated links); ties go to the first one seen."""
    if not found:
        return None
    counts: dict[tuple, int] = {}
    first: dict[tuple, AtsResult] = {}
    for r in found:
        key = (r.ats, (r.slug or "").lower(), r.tenant, (r.site or "").lower())
        counts[key] = counts.get(key, 0) + 1
        first.setdefault(key, r)
    key = max(counts, key=lambda k: counts[k])  # max() keeps the first of equals
    best = first[key]
    best.confidence = confidence
    best.note(f"matched {best.evidence!r} x{counts[key]}")
    if len(counts) > 1:
        best.note(f"{len(counts) - 1} other candidate(s) ignored")
        best.confidence = round(confidence - 0.1, 2)
    return best


def detect_from_url(url: str) -> Optional[AtsResult]:
    """Signal 1 without any network: the URL already names the board."""
    return _pick(_scan(_clean((url or "")[:MAX_URL_CHARS])), CONF_URL)


def detect_from_html(html: str, base_url: str, *, max_chars: int = SCAN_HTML_CHARS) -> AtsResult:
    """Pure: fingerprint one fetched page (signals 1-2). Returns ats=None when the
    page holds no board link; the fetch-driven follow-ups live in detect().
    Only the first `max_chars` characters are scanned."""
    text = _clean((html or "")[:max_chars])
    hit = detect_from_url(base_url) if base_url else None
    if hit:
        return hit
    # Phenom is only a front end: its job JSON carries the real ATS in applyUrl.
    if _PHENOM.search(text):
        apply_urls = " ".join(_APPLY_URL.findall(text))
        hit = _pick(_scan(apply_urls), CONF_APPLY_URL)
        if hit:
            hit.note("via Phenom applyUrl")
            return hit
    hit = _pick(_scan(text), CONF_LINK)
    if hit:
        return hit
    m = _GH_TOKEN.search(text)
    if m and _ok_slug(m.group("slug")) and ("greenhouse" in text.lower() or "boards-api" in text.lower()):
        r = AtsResult("greenhouse", slug=m.group("slug"), confidence=CONF_BUNDLE)
        r.note(f"BOARD_TOKEN {m.group('slug')!r} in script")
        return r
    r = AtsResult()
    if _GH_JID.search(text):
        r.ats, r.confidence = "greenhouse", 0.3
        r.note("gh_jid links but no board slug yet (needs a job-detail page)")
    elif _PHENOM.search(text):
        r.note("Phenom front end without applyUrl (try its search-results page)")
    else:
        r.note("no ATS fingerprint on page")
    return r


# ---------------------------------------------------------- network side

class _Fetcher:
    """Bounded page loader; turns every network/robots failure into a note.
    At most MAX_FETCHES loads, each capped in bytes and wall-clock time, and all
    of them together within DETECT_BUDGET_SEC."""

    def __init__(self, client: PoliteClient, result: AtsResult,
                 clock: Any = time.monotonic) -> None:
        self.client, self.result, self.used = client, result, 0
        self.seen: set[str] = set()
        self._clock = clock
        self._ends_at = clock() + DETECT_BUDGET_SEC

    def remaining(self) -> float:
        return self._ends_at - self._clock()

    def get(self, url: str, *, js: bool = False) -> Optional[tuple[str, str]]:
        if url in self.seen or self.used >= MAX_FETCHES:
            return None
        self.seen.add(url)
        if self.client.is_denied(url):
            self.result.note(f"skipped do-not-automate host: {url}")
            return None
        left = self.remaining()
        if left <= 0:
            self.result.note(f"time budget spent; skipped {url}"[:300])
            return None
        self.used += 1
        try:
            resp = self.client.get(url, headers={"Accept": "text/html,application/xhtml+xml,*/*"},
                                   max_bytes=MAX_JS_BYTES if js else MAX_HTML_BYTES,
                                   deadline_sec=min(FETCH_DEADLINE_SEC, left))
        except (HttpError, requests.RequestException, ValueError) as exc:
            self.result.note(f"fetch failed {url}: {type(exc).__name__}: {exc}"[:300])
            return None
        limit = SCAN_JS_CHARS if js else SCAN_HTML_CHARS
        body = resp.text[:limit] if resp.text else ""
        return (resp.url or url), body


def _abs(base: str, link: str) -> str:
    return urllib.parse.urljoin(base, _html.unescape(link.strip()))


def _merge(into: AtsResult, hit: AtsResult, how: str) -> AtsResult:
    hit._notes = into._notes + hit._notes + [how]
    hit.evidence = "; ".join(hit._notes)
    return hit


def _follow_iframes(f: _Fetcher, base: str, html: str, acc: AtsResult) -> Optional[AtsResult]:
    """Signal 3: an iframe pointing at a custom SPA whose JS bundle holds the token."""
    for src in _IFRAME.findall(html)[:2]:
        src = _abs(base, src)
        hit = detect_from_url(src)
        if hit:
            return _merge(acc, hit, f"iframe src {src}")
        page = f.get(src)
        if not page:
            continue
        final, body = page
        hit = detect_from_html(body, final)
        if hit.slug or hit.tenant:
            return _merge(acc, hit, f"inside iframe {final}")
        for js in _SCRIPT.findall(body)[:3]:
            got = f.get(_abs(final, js), js=True)
            if not got:
                continue
            hit = detect_from_html(got[1], "", max_chars=SCAN_JS_CHARS)
            if hit.slug or hit.tenant:
                hit.confidence = min(hit.confidence, CONF_BUNDLE)
                return _merge(acc, hit, f"in iframe bundle {got[0]}")
    return None


def _follow_detail(f: _Fetcher, base: str, html: str, acc: AtsResult) -> Optional[AtsResult]:
    """Signal 4: one gh_jid link (its page embeds `for=<slug>`), else one job link."""
    text = _clean(html)
    links = [_abs(base, h) for h in _HREF.findall(text) if not h.startswith("#")]
    gh = [u for u in links if _GH_JID.search(u)]
    host = urllib.parse.urlsplit(base).hostname
    jobs = [u for u in links if _JOB_LINK.search(urllib.parse.urlsplit(u).path)
            and urllib.parse.urlsplit(u).hostname == host and u.rstrip("/") != base.rstrip("/")]
    for url in (gh or jobs)[:1]:
        page = f.get(url)
        if not page:
            continue
        hit = detect_from_html(page[1], page[0])
        if hit.slug or hit.tenant or hit.ats == "turbohire":
            hit.confidence = min(hit.confidence, CONF_DETAIL)
            return _merge(acc, hit, f"via job page {page[0]}")
    return None


def _follow_phenom(f: _Fetcher, base: str, html: str, acc: AtsResult) -> Optional[AtsResult]:
    """Phenom careers homes often lack job JSON; their search-results page has applyUrl."""
    links = [_abs(base, h) for h in _HREF.findall(_clean(html)) if "search-results" in h]
    target = links[0] if links else _abs(base, "search-results")
    page = f.get(target)
    if not page:
        return None
    hit = detect_from_html(page[1], page[0])
    if hit.slug or hit.tenant:
        return _merge(acc, hit, f"Phenom search page {page[0]}")
    return None


def verify_result(result: AtsResult, client: PoliteClient) -> AtsResult:
    """ONE call to the connector's own list endpoint; fills verified/job_count.
    A 404 means the detection is wrong (stale slug), so confidence collapses."""
    if result.ats not in API_ATS:
        return result
    from .connectors.ashby import AshbyConnector
    from .connectors.gem import GemConnector
    from .connectors.greenhouse import GreenhouseConnector
    from .connectors.lever import LeverConnector
    from .connectors.smartrecruiters import SmartRecruitersConnector

    h = {"Accept": "application/json"}
    ids = [result.tenant, result.site] if result.ats == "workday" else [result.slug]
    if not all(_valid_id(i) for i in ids):
        result.note(f"verify skipped: unsafe board id {ids!r}"[:300])
        result.confidence = min(result.confidence, 0.2)
        return result
    if result.ats == "workday" and not re.fullmatch(r"wd\d{1,3}", result.wd_server or ""):
        result.note(f"verify skipped: unsafe Workday server {result.wd_server!r}"[:300])
        return result
    limits = {"deadline_sec": FETCH_DEADLINE_SEC}
    try:
        if result.ats == "greenhouse":
            data = client.get_json(GreenhouseConnector.list_url(result.slug, result.region),
                                   headers=h, **limits)
            count = len((data or {}).get("jobs") or [])
        elif result.ats == "ashby":
            data = client.get_json(AshbyConnector.list_url(result.slug), headers=h, **limits)
            count = len((data or {}).get("jobs") or [])
        elif result.ats == "lever":
            base = (LeverConnector.BASE if result.region != "eu"
                    else "https://api.eu.lever.co/v0/postings/{slug}")
            data = client.get_json(base.format(slug=result.slug), params={"mode": "json"},
                                   headers=h, **limits)
            count = len(data or [])
        elif result.ats == "smartrecruiters":
            data = client.get_json(SmartRecruitersConnector.list_url(result.slug),
                                   params={"limit": 1}, headers=h, **limits)
            count = int((data or {}).get("totalFound") or 0)
        elif result.ats == "gem":
            data = client.get_json(GemConnector.list_url(result.slug), headers=h, **limits)
            count = len(data) if isinstance(data, list) else len((data or {}).get("job_posts") or [])
        else:  # workday
            host = f"https://{result.tenant}.{result.wd_server}.myworkdayjobs.com"
            data = client.post_json(
                f"{host}/wday/cxs/{result.tenant}/{result.site}/jobs",
                json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                headers={**h, "Content-Type": "application/json"}, idempotent=True, **limits)
            count = int((data or {}).get("total") or 0)
    except HttpStatusError as exc:
        result.note(f"verify failed: HTTP {exc.status_code}")
        if exc.status_code == 404:
            result.confidence = min(result.confidence, 0.2)
        return result
    except (HttpError, requests.RequestException, ValueError) as exc:
        result.note(f"verify not possible: {type(exc).__name__}: {exc}"[:300])
        return result
    result.verified, result.job_count = True, count
    result.note(f"verified: {count} live jobs")
    return result


def detect(url: str, *, client: Optional[PoliteClient] = None, verify: bool = True) -> AtsResult:
    """Detect the ATS behind a careers URL (signals in docs/SOURCES.md order) and,
    with verify=True, confirm it with one API call. Never raises for network/robots
    trouble: an unreachable page gives ats=None with the reason in `evidence`."""
    client = client or default_client()
    hit = detect_from_url(url)
    if hit is None:
        acc = AtsResult()
        f = _Fetcher(client, acc)
        page = f.get(url)
        if page is None:
            acc.ats, acc.confidence = None, 0.0
            if not acc.evidence:
                acc.note(f"could not fetch {url}")
            return acc
        final, html = page
        if final != url:
            acc.note(f"redirected to {final}")
        hit = detect_from_html(html, final)
        hit = _merge(acc, hit, "") if hit.slug or hit.tenant or hit.ats == "turbohire" else None
        if hit is None:
            hit = (_follow_phenom(f, final, html, acc) if _PHENOM.search(_clean(html)) else None) \
                or _follow_iframes(f, final, html, acc) \
                or _follow_detail(f, final, html, acc)
        if hit is None:
            acc.ats = "manual"
            acc.note("no ATS found; apply by hand")
            return acc
        hit._notes = [n for n in hit._notes if n]
        hit.evidence = "; ".join(hit._notes)
    if verify:
        verify_result(hit, client)
    return hit


# ------------------------------------------------------------ config rows

def to_company_config(result: AtsResult, name: str, **extra: Any) -> dict[str, Any]:
    """A config/companies.yaml row for this detection. Unverified rows get
    `verify: true` so the curation workflow (and humans) know to check them."""
    row: dict[str, Any] = {"name": name}
    if result.ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "gem"):
        row.update(connector=result.ats, slug=result.slug)
        if result.region:
            row["region"] = result.region
    elif result.ats == "workday":
        row.update(connector="workday", tenant=result.tenant,
                   wd_server=result.wd_server, site=result.site)
    else:
        row["connector"] = "manual"
        if result.ats == "turbohire":
            row.update(ats="turbohire", slug=result.slug)
    row["verify"] = not result.verified
    bits = [f"ats_detect: {result.evidence}" if result.evidence else "ats_detect"]
    if result.job_count is not None:
        bits.append(f"{result.job_count} jobs")
    row["note"] = "; ".join(bits)[:300]
    row.update(extra)
    return row
