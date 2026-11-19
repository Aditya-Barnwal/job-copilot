"""Is a posting still open? A read-only check before anything is tailored,
shown as ready or opened for autofill.

    check(job, client=None) -> {"live": True | False | None, "reason": str, "checked_at": ts, "via": str}

`live` is False only on clear evidence that the posting is gone; a network
error, a robots.txt refusal, a rate limit or an answer we can't read is None
(unknown), never "gone". All I/O goes through the polite client
(jobcopilot/http.py: robots.txt, per-host and per-platform rate limits, the
do-not-automate list). GETs only.

How, in order:
  1. The ATS's own public API for that posting, picked by the URL
     (ats_detect.detect_from_url):
       Workday     CXS job detail  GET <host>/wday/cxs/<tenant>/<site>/job/...
                   404 / 410, or a Workday error answer (JSON errorCode, e.g. a
                   403 "S22" for a removed posting), or no jobPostingInfo /
                   posted=false -> gone
       Greenhouse  GET boards-api.greenhouse.io/v1/boards/<board>/jobs/<id>   404 -> gone
       Lever       GET api.lever.co/v0/postings/<site>/<id>                   404 -> gone
       Ashby       GET api.ashbyhq.com/posting-api/job-board/<org>: id not listed -> gone
       SmartRecruiters GET api.smartrecruiters.com/v1/companies/<co>/postings/<id> 404 -> gone
       Keka        the tenant's active-jobs list (careers/api/embedjobs/...): id not listed -> gone
                   (a closed Keka posting page still renders, so the page can't tell)
  2. Otherwise the posting page itself: 404 / 410 -> gone; a 200 page whose
     visible text says "no longer available", "doesn't exist", "position has
     been filled", "job not found"... -> gone; else live.

mark_gone(key) sets jobs.gone_at (the same column store.retire_unlisted uses),
so the queue, the Browse list and autofill all leave the job alone.
check_queue(day) is the nightly pass (daily.py): check today's main and
watchlist jobs, retire the dead ones, refill the queue from the next ranked
jobs (queue.build_daily_queue) and check the newcomers too.
"""
from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from typing import Any, Optional

from . import store

log = logging.getLogger("jobcopilot.liveness")

PAGE_MAX_BYTES = 3_000_000
API_MAX_BYTES = 8_000_000
DEADLINE_SEC = 30.0
CACHE_TTL_SEC = 600.0              # a verdict is reused this long (card open -> Autofill click)
MAX_REFILL_ROUNDS = 3

# Clear "this posting is gone" wording on a page that still answers 200.
_DEAD_TEXT = re.compile(
    r"page you('re| are) looking for (does ?n[o']?t|does not|no longer) exists?"
    r"|(job|position|posting|role|opening|vacancy|requisition|listing)s? you('re| are) looking for "
    r"(is no longer|has been|could ?n[o']?t be|can ?n[o']?t be|was not|is not available)"
    r"|(job|position|posting|role|opening|vacancy|requisition|listing)[^.!?\n]{0,60}\bno longer "
    r"(available|active|open|exists|listed|accepting)"
    r"|no longer accepting applications"
    r"|(job|position|posting|role|opening|vacancy|requisition)s? (has|have) (already )?been (filled|closed|removed)"
    r"|(job|position|posting|role|requisition) (is|was) (now )?(closed|filled|expired)"
    r"|(job|position|posting|vacancy|opening|page) not found"
    r"|(job|position|posting|page) (does ?n[o']?t|does not) exist"
    r"|(job|position|posting|listing) (has )?expired"
    r"|applications (are|is) (now )?closed",
    re.I)
_SCRIPT_STYLE = re.compile(r"<(script|style|noscript|template)\b.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

_cache: dict[str, tuple[float, dict]] = {}
_lists: dict[str, tuple[float, Any]] = {}       # per-board job lists (Ashby, Keka) within a pass
_lock = threading.Lock()


# --------------------------------------------------------------------------- helpers

def _verdict(live: Optional[bool], reason: str, via: str) -> dict:
    return {"live": live, "reason": reason, "checked_at": time.time(), "via": via}


def posting_url(job: dict) -> str:
    """The link a person would open: the posting URL, else the apply link."""
    for k in ("url", "apply_url"):
        v = str(job.get(k) or "").strip()
        if v:
            return v
    return ""


def visible_text(page_html: str, limit: int = 200_000) -> str:
    """Page text without scripts, styles or tags (whitespace collapsed)."""
    t = _SCRIPT_STYLE.sub(" ", (page_html or "")[:PAGE_MAX_BYTES])
    t = _html.unescape(_TAG.sub(" ", t))
    return re.sub(r"\s+", " ", t).strip()[:limit]


def dead_page_reason(page_html_or_text: str) -> Optional[str]:
    """The phrase that says the posting is gone, if the page shows one."""
    text = page_html_or_text or ""
    if "<" in text:
        text = visible_text(text)
    m = _DEAD_TEXT.search(text[:100_000])
    return m.group(0).strip()[:120] if m else None


def _status(exc: Exception) -> Optional[int]:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None)


def _workday_error(resp: Any) -> Optional[str]:
    """A Workday application error answer ({"errorCode": ...}), as opposed to a
    proxy / WAF page: the posting isn't there for the public site."""
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, dict) and data.get("errorCode"):
        return str(data.get("errorCode"))[:20]
    return None


def _cached_list(key: str, fetch: Any) -> Any:
    now = time.time()
    with _lock:
        hit = _lists.get(key)
        if hit and now - hit[0] < CACHE_TTL_SEC:
            return hit[1]
    data = fetch()
    with _lock:
        _lists[key] = (now, data)
    return data


# --------------------------------------------------------------------------- per-ATS API checks

_WD_URL = re.compile(
    r"^https://(?P<host>(?P<tenant>[\w-]{1,63})\.(?P<wd>wd\d{1,3})\.myworkdayjobs\.com)"
    r"/(?:[a-z]{2}-[A-Za-z]{2}/)?(?P<site>[\w-]+)(?P<path>/job/[^?#]+)", re.I)
_WD_SITE_URL = re.compile(      # wdN.myworkdaysite.com/<locale>/recruiting/<tenant>/<site>/job/...
    r"^https://(?P<host>(?P<wd>wd\d{1,3})\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Za-z]{2}/)?recruiting/"
    r"(?P<tenant>[\w-]{1,63})/(?P<site>[\w-]+)(?P<path>/job/[^?#]+)", re.I)


def _check_workday(url: str, client: Any) -> Optional[dict]:
    m = _WD_URL.match(url) or _WD_SITE_URL.match(url)
    if not m:
        return None
    api = f"https://{m['host']}/wday/cxs/{m['tenant']}/{m['site']}{m['path'].rstrip('/')}"
    resp = client.get(api, headers={"Accept": "application/json"}, raise_for_status=False,
                      max_bytes=API_MAX_BYTES, deadline_sec=DEADLINE_SEC)
    code = resp.status_code
    if code in (404, 410):
        return _verdict(False, f"Workday says the posting is gone (HTTP {code})", "api:workday")
    if code in (400, 403) and (err := _workday_error(resp)):
        return _verdict(False, f"Workday no longer shows this posting (error {err}, HTTP {code})",
                        "api:workday")
    if code != 200:
        return _verdict(None, f"Workday answered HTTP {code}; couldn't tell", "api:workday")
    try:
        info = (resp.json() or {}).get("jobPostingInfo")
    except ValueError:
        return _verdict(None, "Workday's answer wasn't readable", "api:workday")
    if not isinstance(info, dict) or not info:
        return _verdict(False, "Workday has no posting at this link", "api:workday")
    if info.get("posted") is False:
        return _verdict(False, "Workday marks this posting as not posted", "api:workday")
    return _verdict(True, "Workday lists this posting", "api:workday")


_GH_URL = re.compile(r"^https://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?P<board>[\w-]+)/jobs/(?P<id>\d+)", re.I)


def _greenhouse_ids(job: dict, url: str, db_path: Any) -> Optional[tuple[str, str]]:
    m = _GH_URL.match(url)
    if m:
        return m["board"], m["id"]
    try:
        from .autofill.service import greenhouse_ids
        return greenhouse_ids(job, url, db_path)
    except Exception:  # noqa: BLE001
        return None


def _check_greenhouse(job: dict, url: str, client: Any, db_path: Any) -> Optional[dict]:
    ids = _greenhouse_ids(job, url, db_path)
    if not ids:
        return None
    board, jid = ids
    api = (f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board, safe='')}"
           f"/jobs/{urllib.parse.quote(str(jid), safe='')}")
    resp = client.get(api, headers={"Accept": "application/json"}, raise_for_status=False,
                      max_bytes=API_MAX_BYTES, deadline_sec=DEADLINE_SEC)
    if resp.status_code in (404, 410):
        return _verdict(False, f"Greenhouse says the posting is gone (HTTP {resp.status_code})",
                        "api:greenhouse")
    if resp.status_code != 200:
        return _verdict(None, f"Greenhouse answered HTTP {resp.status_code}; couldn't tell", "api:greenhouse")
    return _verdict(True, "Greenhouse lists this posting", "api:greenhouse")


_LEVER_URL = re.compile(r"^https://jobs(?P<eu>\.eu)?\.lever\.co/(?P<site>[\w.-]+)/(?P<id>" + _UUID + ")", re.I)


def _check_lever(url: str, client: Any) -> Optional[dict]:
    m = _LEVER_URL.match(url)
    if not m:
        return None
    api = f"https://api{'.eu' if m['eu'] else ''}.lever.co/v0/postings/{m['site']}/{m['id'].lower()}"
    resp = client.get(api, headers={"Accept": "application/json"}, raise_for_status=False,
                      max_bytes=API_MAX_BYTES, deadline_sec=DEADLINE_SEC)
    if resp.status_code in (404, 410):
        return _verdict(False, f"Lever says the posting is gone (HTTP {resp.status_code})", "api:lever")
    if resp.status_code != 200:
        return _verdict(None, f"Lever answered HTTP {resp.status_code}; couldn't tell", "api:lever")
    return _verdict(True, "Lever lists this posting", "api:lever")


_ASHBY_URL = re.compile(r"^https://jobs\.ashbyhq\.com/(?P<org>[\w.%-]+)/(?P<id>" + _UUID + ")", re.I)


def _check_ashby(url: str, client: Any) -> Optional[dict]:
    m = _ASHBY_URL.match(url)
    if not m:
        return None
    org = m["org"]
    api = f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(urllib.parse.unquote(org), safe='')}"

    def fetch() -> Any:
        resp = client.get(api, headers={"Accept": "application/json"}, raise_for_status=False,
                          max_bytes=API_MAX_BYTES, deadline_sec=DEADLINE_SEC)
        if resp.status_code != 200:
            return ("status", resp.status_code)
        return ("ok", resp.json())
    kind, data = _cached_list("ashby:" + org.lower(), fetch)
    if kind != "ok":
        if data in (404, 410):
            return _verdict(False, f"Ashby has no job board {org!r} any more (HTTP {data})", "api:ashby")
        return _verdict(None, f"Ashby answered HTTP {data}; couldn't tell", "api:ashby")
    jobs = (data or {}).get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return _verdict(None, "Ashby's answer wasn't readable", "api:ashby")
    ids = {str(j.get("id") or "").lower() for j in jobs if isinstance(j, dict)}
    if m["id"].lower() in ids:
        return _verdict(True, "Ashby lists this posting", "api:ashby")
    return _verdict(False, "Ashby's job board no longer lists this posting", "api:ashby")


_SR_URL = re.compile(r"^https://(?:jobs|careers)\.smartrecruiters\.com/(?P<co>[\w-]+)/(?P<id>\d{6,})", re.I)


def _check_smartrecruiters(url: str, client: Any) -> Optional[dict]:
    m = _SR_URL.match(url)
    if not m:
        return None
    api = f"https://api.smartrecruiters.com/v1/companies/{m['co']}/postings/{m['id']}"
    resp = client.get(api, headers={"Accept": "application/json"}, raise_for_status=False,
                      max_bytes=API_MAX_BYTES, deadline_sec=DEADLINE_SEC)
    if resp.status_code in (404, 410):
        return _verdict(False, f"SmartRecruiters says the posting is gone (HTTP {resp.status_code})",
                        "api:smartrecruiters")
    if resp.status_code != 200:
        return _verdict(None, f"SmartRecruiters answered HTTP {resp.status_code}; couldn't tell",
                        "api:smartrecruiters")
    return _verdict(True, "SmartRecruiters lists this posting", "api:smartrecruiters")


_KEKA_URL = re.compile(r"^https://(?P<slug>[A-Za-z0-9-]{1,63})\.keka\.com/careers/(?:jobdetails|applyjob)/(?P<id>\d+)",
                       re.I)


def _check_keka(url: str, client: Any) -> Optional[dict]:
    m = _KEKA_URL.match(url)
    if not m:
        return None
    from .connectors.keka import HTML_HEADERS, KekaConnector, org_id_from_html
    slug = m["slug"].lower()

    def fetch() -> Any:
        shell = client.get(KekaConnector.page_url(slug), headers=HTML_HEADERS, raise_for_status=False,
                           max_bytes=PAGE_MAX_BYTES, deadline_sec=DEADLINE_SEC)
        if shell.status_code != 200:
            return ("status", shell.status_code)
        org = org_id_from_html(shell.text)
        if not org:
            return ("status", "no careers portal")
        resp = client.get(KekaConnector.list_url(slug, org), headers={"Accept": "application/json"},
                          raise_for_status=False, max_bytes=API_MAX_BYTES, deadline_sec=DEADLINE_SEC)
        if resp.status_code != 200:
            return ("status", resp.status_code)
        return ("ok", resp.json())
    kind, data = _cached_list("keka:" + slug, fetch)
    if kind != "ok" or not isinstance(data, list):
        return _verdict(None, f"Keka's job list wasn't available ({data if kind != 'ok' else 'unreadable'})",
                        "api:keka")
    ids = {str(j.get("id")) for j in data if isinstance(j, dict)}
    if m["id"] in ids:
        return _verdict(True, "Keka lists this posting as active", "api:keka")
    return _verdict(False, "Keka's active job list no longer includes this posting", "api:keka")


def _api_check(job: dict, url: str, ats: Optional[str], client: Any, db_path: Any) -> Optional[dict]:
    if ats == "workday":
        return _check_workday(url, client)
    if ats == "greenhouse" or "gh_jid=" in url:
        return _check_greenhouse(job, url, client, db_path)
    if ats == "lever":
        return _check_lever(url, client)
    if ats == "ashby":
        return _check_ashby(url, client)
    if ats == "smartrecruiters":
        return _check_smartrecruiters(url, client)
    if ats == "keka":
        return _check_keka(url, client)
    return None


# --------------------------------------------------------------------------- the page itself

def _check_page(url: str, client: Any) -> dict:
    resp = client.get(url, headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"},
                      raise_for_status=False, max_bytes=PAGE_MAX_BYTES, deadline_sec=DEADLINE_SEC)
    code = resp.status_code
    if code in (404, 410):
        return _verdict(False, f"the posting page is gone (HTTP {code})", "page")
    if code != 200:
        return _verdict(None, f"the posting page answered HTTP {code}; couldn't tell", "page")
    final = str(getattr(resp, "url", "") or url)
    if re.search(r"[?&]error=true\b", final):
        return _verdict(False, "the posting redirected to the board's 'job not found' page", "page")
    why = dead_page_reason(resp.text or "")
    if why:
        return _verdict(False, f"the posting page says: “{why}”", "page")
    return _verdict(True, "the posting page loads", "page")


# --------------------------------------------------------------------------- public API

def check(job: dict, client: Any = None, *, db_path: Any = None) -> dict:
    """{live, reason, checked_at, via} for one job row (url / apply_url). Never
    raises for network trouble: that is live=None."""
    from . import http as _http
    from .ats_detect import detect_from_url
    url = posting_url(job)
    if not url or not re.match(r"^https?://", url, re.I):
        return _verdict(None, "no web link to check", "none")
    if client is None and "PYTEST_CURRENT_TEST" in os.environ:
        # belt and braces (like store's real-DB refusal): tests never reach the
        # internet; a test that wants a verdict passes its own fake client
        return _verdict(None, "not checked under tests (no network)", "none")
    client = client or _http.default_client()
    try:
        if client.is_denied(url):
            return _verdict(None, "a do-not-automate site: not checked (open it yourself)", "none")
    except Exception:  # noqa: BLE001
        pass
    hit = None
    try:
        hit = detect_from_url(url)
    except Exception:  # noqa: BLE001
        hit = None
    ats = getattr(hit, "ats", None)
    try:
        v = _api_check(job, url, ats, client, db_path)
        if v is None:                                   # no API for this link: the page decides
            return _check_page(url, client)
        if v["live"] is None and v["via"] != "api:keka":
            # the API couldn't tell; the page may still say plainly that it's gone
            # (a closed Keka page still renders, so it's never asked)
            page = _check_page(url, client)
            if page["live"] is False:
                return page
        return v
    except _http.RobotsDisallowed as exc:
        return _verdict(None, f"robots.txt doesn't let us check this site ({type(exc).__name__})", "none")
    except _http.HttpError as exc:
        return _verdict(None, f"couldn't reach the site ({type(exc).__name__})", "none")
    except Exception as exc:  # noqa: BLE001 — requests errors, bad JSON...: unknown, never gone
        return _verdict(None, f"couldn't check ({type(exc).__name__})", "none")


def cached_check(job: dict, client: Any = None, *, db_path: Any = None,
                 max_age_sec: float = CACHE_TTL_SEC) -> dict:
    """check(), reusing a verdict for the same link from the last `max_age_sec`
    (the review card's check and the Autofill click a minute later)."""
    url = posting_url(job)
    now = time.time()
    with _lock:
        hit = _cache.get(url)
    if hit and now - hit[0] < max_age_sec and hit[1].get("live") is not None:
        return dict(hit[1])
    v = check(job, client, db_path=db_path)
    with _lock:
        _cache[url] = (now, v)
        if len(_cache) > 1000:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:200]:
                _cache.pop(k, None)
    return dict(v)


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _lists.clear()


def mark_gone(job_key: str, verdict: Optional[dict] = None, db_path: Any = None) -> bool:
    """Retire a job whose posting is closed: jobs.gone_at (kept if already set),
    and its fill state. True when the row exists."""
    at = float((verdict or {}).get("checked_at") or time.time())
    with store.connect(db_path) as conn:
        cur = conn.execute("UPDATE jobs SET gone_at = COALESCE(gone_at, ?) WHERE key = ?", (at, job_key))
        found = cur.rowcount > 0
    if found:
        try:
            store.set_fill_status(job_key, "gone", db_path)
        except Exception:  # noqa: BLE001 — a DB from before fill_status: gone_at is what counts
            pass
        log.info("liveness: job=%s gone (%s)", job_key, (verdict or {}).get("via") or "?")
    return found


def check_and_mark(job_key: str, *, db_path: Any = None, client: Any = None,
                   use_cache: bool = False) -> Optional[dict]:
    """Check one stored job and retire it when it's gone. None = no such job."""
    job = store.get_job(job_key, db_path)
    if not job:
        return None
    v = (cached_check if use_cache else check)(job, client, db_path=db_path)
    if v.get("live") is False:
        mark_gone(job_key, v, db_path)
    return v


def check_queue(day: Optional[str] = None, *, db_path: Any = None, client: Any = None,
                lanes: tuple[str, ...] = ("main", "extended"),
                max_rounds: int = MAX_REFILL_ROUNDS) -> dict:
    """The nightly pass: check every job in today's queue (main + watchlist),
    retire the gone ones, refill the queue from the next ranked jobs
    (queue.build_daily_queue skips gone rows) and check the newcomers, up to
    `max_rounds` refills. Returns counts, the gone keys and the rebuilt queue
    (None when nothing changed)."""
    from . import queue as _queue
    checked: set[str] = set()
    gone: list[str] = []
    unknown: list[str] = []
    rebuilt = None
    rounds = 0
    day_now = day
    while True:
        q = _queue.list_queue(day=day_now, lane=None, db_path=db_path)
        day_now = q.get("day") or day_now
        todo = [it["key"] for it in q.get("items", [])
                if it.get("queue_lane") in lanes and it["key"] not in checked]
        newly = 0
        for key in todo:
            checked.add(key)
            v = check_and_mark(key, db_path=db_path, client=client)
            if v is None:
                continue
            if v.get("live") is False:
                gone.append(key)
                newly += 1
            elif v.get("live") is None:
                unknown.append(key)
        if not newly or rounds >= max_rounds or day_now is None:
            break
        rounds += 1
        rebuilt = _queue.build_daily_queue(day=day_now, db_path=db_path)   # refill past the gone ones
    log.info("liveness: checked=%d gone=%d unknown=%d refills=%d", len(checked), len(gone),
             len(unknown), rounds)
    return {"day": day_now, "checked": len(checked), "gone": len(gone), "unknown": len(unknown),
            "refills": rounds, "gone_keys": gone, "queue": rebuilt}


__all__ = ["check", "cached_check", "check_and_mark", "check_queue", "mark_gone", "dead_page_reason",
           "visible_text", "posting_url", "clear_cache"]
