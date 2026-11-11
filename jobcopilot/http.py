"""Polite HTTP client shared by every connector (plan decision D7).

Politeness only holds if it can't be skipped, so every network call a
connector makes goes through `PoliteClient`, which:

  * refuses DO-NOT-AUTOMATE hosts (`denied_hosts`, plus every subdomain) with
    `HostDenied` before any network I/O, on every redirect hop too;
  * sends the honest User-Agent from config/sources.yaml (callers can't override it);
  * rate-limits per host with a token bucket, slowing further when robots.txt
    sets Crawl-delay / Request-rate;
  * reads robots.txt before the first request to a host and refuses a
    disallowed URL with `RobotsDisallowed` (the request is never sent);
  * retries only 429 / 5xx / connection errors, honouring Retry-After, with
    exponential backoff + jitter; other 4xx are never retried;
  * revalidates GETs with ETag / Last-Modified and serves the cached body on 304;
  * follows redirects itself (GET/HEAD), so each hop is checked; caller headers
    such as API keys are dropped once a redirect leaves the original origin;
  * writes response bodies to $JOBCOPILOT_RECORD_DIR so test fixtures can be
    built from real traffic.

robots.txt handling (RFC 9309, conservative where it is vague). Our own parser,
not urllib.robotparser: BOM stripped; every group naming our exact product
token is merged (else every `*` group); blank lines don't end a group; longest
match on percent-decoded paths wins, ties go to Allow; `*` / `$` are matched in
linear time; fractional Crawl-delay is honoured.
  2xx             parse (first 500 KiB)
  3xx             followed (up to 5 hops, each hop checked against denied_hosts)
  401 / 403       disallow-all, cached for robots_ttl_sec (an auth wall is not consent)
  other 4xx       no robots file, so allow all (404 / 410 are the usual case)
  429 / 5xx / net retried, then disallow-all for robots_retry_ttl_sec (10 min),
                  so one blip doesn't block a host for a day in the long-lived server
Hosts in `robots_exempt_hosts` (documented public APIs only, each with a
reason) skip the robots check but are still rate-limited and still denylisted.
A key may be a bare host ("api.example.com": whole host) or host plus path
prefix ("api.example.com/public-api/": only paths at or under it, matched on
segment boundaries after percent-decoding; any '.'/'..' segment or backslash in
the path voids the exemption, so robots.txt decides as usual).

Every response body is read here, eagerly and bounded: `max_bytes` (default
`max_response_bytes`, 64 MiB) rejects an oversized Content-Length before reading
and stops a body that grows past it (ResponseTooLarge); `deadline_sec` (default
`request_deadline_sec`, 600 s) caps the wall-clock time of the whole logical
request, redirects, retries and waits included (RequestDeadlineExceeded), on
top of the per-socket-read `timeout_sec`.

Logs and error messages carry `redact_url()` output only: Adzuna puts its API
key in the query string, and requests' own exception text would leak it.
"""
from __future__ import annotations

import hashlib
import itertools
import json as _json
import logging
import math
import os
import random
import re
import secrets
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Optional

import http.client as http_client

import requests
import urllib3.exceptions as urllib3_exceptions
from requests.structures import CaseInsensitiveDict

from .config import DEFAULTS, ROOT

log = logging.getLogger("jobcopilot.http")

RECORD_ENV = "JOBCOPILOT_RECORD_DIR"
DEFAULT_USER_AGENT: str = DEFAULTS["sources"]["http"]["user_agent"]
# Always refused, whatever config says: config can add hosts, never remove these
# (non-negotiable 3, D10; docs/SOURCES.md). Pinned here as well as in config
# DEFAULTS so the floor can't disappear with a config edit.
BUILTIN_DENIED_HOSTS: tuple[str, ...] = tuple(dict.fromkeys((
    "workatastartup.com", "ycombinator.com", "linkedin.com", "naukri.com",
    "indeed.com", "wellfound.com", "glassdoor.com",
    *DEFAULTS.get("sources", {}).get("http", {}).get("denied_hosts", ()),
)))
MAX_REDIRECTS = 5
ROBOTS_MAX_BYTES = 500 * 1024          # RFC 9309 §2.5: parse at least 500 KiB
ROBOTS_MAX_REDIRECTS = 5               # RFC 9309 §2.3.1.2
ROBOTS_MAX_RETRIES = 2                 # per robots.txt fetch, on 429 / 5xx / network errors
ROBOTS_RETRY_TTL_SEC = 600.0           # how long a transient robots failure blocks a host
RETRY_AFTER_MAX_SEC = 24 * 3600.0      # cap on any Retry-After / penalty, so a host can't wedge us
CACHE_MAX_ENTRIES = 512                # in-memory LRU; the API server is long-lived
CACHE_MAX_BODY = 25 * 1024 * 1024
# Global body cap. The biggest legit payloads are single-call boards: Greenhouse
# content=true ships entity-escaped HTML (~15-20 KB/job), so a 1,500-job board is
# ~25-30 MB. 64 MiB leaves >2x headroom while still bounding memory.
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# Whole-request wall clock. Longest legit path: 4 attempts x 20 s timeouts plus
# 3 x max_wait_sec (120 s) of Retry-After waits = 440 s.
DEFAULT_REQUEST_DEADLINE_SEC = 600.0
ROBOTS_DEADLINE_SEC = 60.0             # per robots.txt attempt
_READ_CHUNK = 64 * 1024
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 429 and 503 mean "not processed", so they are safe to retry even for POST.
_RETRY_ANY_METHOD = frozenset({429, 503})
_NEVER_RETRY_5XX = frozenset({501, 505})  # permanent by definition
_SECRET_PARAMS = frozenset({
    "app_id", "app_key", "api_key", "apikey", "key", "token", "access_token",
    "auth", "secret", "client_secret", "password", "signature", "sig",
})
_UNCACHED_HEADERS = ("set-cookie", "content-encoding", "content-length", "transfer-encoding")
# Caller headers that may follow a redirect to another origin; everything else
# (API keys, Authorization, Cookie, ...) is dropped at that point.
_REDIRECT_SAFE_HEADERS = frozenset({"accept", "accept-language"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


# ----------------------------------------------------------------- errors

class HttpError(requests.RequestException):
    """Base class for everything this module raises."""


class RobotsDisallowed(HttpError):
    """robots.txt forbids this URL for our User-Agent. Nothing was sent."""

    def __init__(self, url: str, reason: str = "disallowed by robots.txt") -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"{reason}: {url}")


class RobotsUnavailable(RobotsDisallowed):
    """robots.txt couldn't be read (network, 429/5xx, 401/403), so the host is treated
    as disallowed. `transient` is True for network / 429 / 5xx failures, which are
    retried after `robots_retry_ttl_sec` rather than the full robots TTL."""

    def __init__(self, url: str, reason: str = "robots.txt unavailable", *,
                 transient: bool = False) -> None:
        self.transient = transient
        super().__init__(url, reason)


class HostDenied(RobotsDisallowed):
    """The host (or a parent domain) is on the DO-NOT-AUTOMATE list. Nothing was sent."""


class RetryLater(HttpError):
    """The host wants a longer wait (Retry-After / Crawl-delay) than `max_wait_sec`.

    We give up on the request rather than stall the nightly run or hammer the
    host; later requests to the same host fail fast until the wait has passed.
    """

    def __init__(self, url: str, wait_sec: float) -> None:
        self.url = url
        self.wait_sec = wait_sec
        super().__init__(f"host asked us to wait {wait_sec:.0f}s; giving up on {url}")


class HttpStatusError(HttpError, requests.HTTPError):
    """Final non-2xx/3xx response, after any retries. Still an `HTTPError` for v1 callers."""

    def __init__(self, method: str, url: str, response: requests.Response) -> None:
        self.status_code = response.status_code
        super().__init__(f"{method} {url} -> HTTP {response.status_code}", response=response)


class HttpConnectionError(HttpError, requests.ConnectionError):
    """Network failure that survived every retry."""


class InvalidJSON(HttpError):
    """get_json()/post_json() got a body that isn't JSON."""


class ResponseTooLarge(HttpError):
    """The body is (or announced itself as) bigger than `max_bytes`. The connection
    was dropped; nothing past the limit was kept in memory."""

    def __init__(self, url: str, limit: int, size: Optional[int] = None) -> None:
        self.url, self.limit, self.size = url, limit, size
        seen = f"Content-Length {size}" if size is not None else "body"
        super().__init__(f"{seen} exceeds max_bytes={limit}: {url}")


class RequestDeadlineExceeded(HttpError, requests.Timeout):
    """The request (all hops, retries and waits) ran past its wall-clock deadline."""

    def __init__(self, url: str, deadline_sec: Optional[float] = None) -> None:
        self.url = url
        self.deadline_sec = deadline_sec
        super().__init__(f"deadline exceeded: {url}")


class _BodyReadError(Exception):
    """Internal: the connection broke while reading a body (retryable like a connect error)."""


# ---------------------------------------------------------- small helpers

def _is_secret_param(name: str) -> bool:
    n = name.lower()
    return n in _SECRET_PARAMS or n.endswith(("_key", "_token", "_secret"))


def redact_url(url: str) -> str:
    """The URL with credential-looking query values (and any userinfo) replaced, safe to log."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "REDACTED@" + netloc.rsplit("@", 1)[1]
    query = parts.query
    if query:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        query = urllib.parse.urlencode(
            [(k, "REDACTED" if _is_secret_param(k) else v) for k, v in pairs]
        )
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc, query=query))


def _with_params(url: str, params: Optional[Mapping[str, Any]]) -> str:
    """Canonical full URL, so robots checks and cache keys see exactly what is sent."""
    return requests.Request("GET", url, params=params).prepare().url or url


def _norm_host(host: str) -> str:
    """Lowercase, no trailing dot: one name per host for buckets, robots and denylist."""
    return host.strip().lower().rstrip(".")


def _port(parts: urllib.parse.SplitResult) -> int:
    """Explicit or default port; -1 if the port is unparseable."""
    try:
        return parts.port or _DEFAULT_PORTS.get(parts.scheme, -1)
    except ValueError:
        return -1


def _origin(parts: urllib.parse.SplitResult) -> str:
    """Normalised scheme://host[:port] (default port and trailing dot dropped, no userinfo)."""
    host = _norm_host(parts.hostname or "")
    if ":" in host:
        host = f"[{host}]"
    port = _port(parts)
    if port < 0:
        raise ValueError("bad port")
    suffix = "" if port == _DEFAULT_PORTS.get(parts.scheme) else f":{port}"
    return f"{parts.scheme}://{host}{suffix}"


def _same_trust(a: urllib.parse.SplitResult, b: urllib.parse.SplitResult) -> bool:
    """May headers meant for `a` follow a redirect to `b`? Same host, and either the
    same scheme and port or a plain http -> https upgrade on default ports."""
    if _norm_host(a.hostname or "") != _norm_host(b.hostname or ""):
        return False
    if a.scheme == b.scheme:
        return _port(a) == _port(b) != -1
    return a.scheme == "http" and b.scheme == "https" and _port(a) == 80 and _port(b) == 443


def parse_retry_after(value: Optional[str], now: float) -> Optional[float]:
    """Seconds to wait from a Retry-After header, or None.

    RFC 9110: delta-seconds (digits only) or an HTTP-date. Anything else (`inf`,
    `nan`, `1e400`, `-5`, `5.5`) is treated as absent, so the caller falls back to
    backoff; the result is capped at RETRY_AFTER_MAX_SEC.
    """
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return min(float(int(value)), RETRY_AFTER_MAX_SEC)
    if value[:1].isdigit() or value[:1] in "+-.":
        return None  # numeric-looking but not delta-seconds
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        delta = when.timestamp() - now
    except (OverflowError, OSError, ValueError):
        return None
    return min(max(0.0, delta), RETRY_AFTER_MAX_SEC)


# ------------------------------------------------------------ rate limits

class TokenBucket:
    """Thread-safe token bucket for one host.

    `reserve()` never blocks: it takes a token (going into debt if needed) and
    returns how long the caller must sleep. The lock is never held while
    sleeping, and concurrent callers queue up at exactly 1/rate spacing.
    """

    def __init__(self, rate_per_sec: float, burst: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self._rate = float(rate_per_sec)
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._stamp = clock()
        self._lock = threading.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    def _refill(self, now: float) -> None:
        self._tokens = min(self._burst, self._tokens + (now - self._stamp) * self._rate)
        self._stamp = now

    def reserve(self) -> float:
        with self._lock:
            self._refill(self._clock())
            self._tokens -= 1.0
            return 0.0 if self._tokens >= 0 else -self._tokens / self._rate

    def refund(self) -> None:
        """Give back a reserved token the caller decided not to use."""
        with self._lock:
            self._tokens = min(self._burst, self._tokens + 1.0)

    def penalize(self, seconds: float) -> None:
        """Make the next reservation wait at least `seconds` (Retry-After / backoff).
        Capped at RETRY_AFTER_MAX_SEC: the bucket can never go into infinite debt."""
        if not math.isfinite(seconds) or seconds > RETRY_AFTER_MAX_SEC:
            seconds = RETRY_AFTER_MAX_SEC
        seconds = max(0.0, seconds)
        with self._lock:
            self._refill(self._clock())
            self._tokens = min(self._tokens, 1.0 - seconds * self._rate)

    def set_rate(self, rate_per_sec: float) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        with self._lock:
            self._refill(self._clock())
            self._rate = float(rate_per_sec)


# ------------------------------------------------------------- robots.txt

_UA_KEYS = frozenset({"user-agent", "useragent", "user agent"})
_ALLOW_KEYS = frozenset({"allow"})
# Misspellings are honoured (as Google does): reading more Disallow lines is the safe side.
_DISALLOW_KEYS = frozenset({"disallow", "dissallow", "dissalow", "disalow", "diasllow", "disallaw"})
_CRAWL_DELAY_KEYS = frozenset({"crawl-delay", "crawldelay", "crawl delay"})
_REQUEST_RATE_KEYS = frozenset({"request-rate", "requestrate", "request rate"})
_GROUP_KEYS = _ALLOW_KEYS | _DISALLOW_KEYS | _CRAWL_DELAY_KEYS | _REQUEST_RATE_KEYS
_TOKEN_RE = re.compile(r"[A-Za-z_-]+")
_DECIMAL_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)\Z")
_REQUEST_RATE_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*([smh]?)\Z", re.IGNORECASE)
_EOL_RE = re.compile(r"\r\n|\r|\n")
_UNIT_SEC = {"": 1, "s": 1, "m": 60, "h": 3600}


def product_token(user_agent: str) -> str:
    """'job-copilot/2.0 (personal ...)' -> 'job-copilot' (RFC 9309 §2.2.1), lowercased."""
    m = _TOKEN_RE.match(user_agent.strip())
    return m.group(0).lower() if m else ""


def _agent_of(value: str) -> str:
    """The product token a `User-agent:` line names: '*', a lowercased token, or ''."""
    v = value.strip()
    if v[:1] == "*" and (len(v) == 1 or v[1].isspace()):
        return "*"
    return product_token(v)


def _robots_target(url: str) -> bytes:
    """Path (never empty) plus '?query', percent-decoded to octets."""
    parts = urllib.parse.urlsplit(url)
    target = parts.path or "/"
    if parts.query or "?" in url.split("#", 1)[0]:
        target += "?" + parts.query
    return urllib.parse.unquote_to_bytes(target)


@dataclass(frozen=True)
class _RobotsRule:
    allow: bool
    parts: tuple[bytes, ...]   # literal pieces between `*` wildcards, percent-decoded
    anchored: bool             # the rule ended in `$`
    length: int                # decoded octets (a wildcard or `$` counts 1): longest wins

    @classmethod
    def parse(cls, allow: bool, value: str) -> Optional["_RobotsRule"]:
        if not value:
            return None  # `Disallow:` with no path disallows nothing
        if value[0] not in "/*":
            value = "/" + value
        anchored = value.endswith("$")
        if anchored:
            value = value[:-1]
        pieces = value.split("*")  # split before decoding, so %2A stays a literal '*'
        parts = tuple(urllib.parse.unquote_to_bytes(p) for p in pieces)
        length = sum(len(p) for p in parts) + len(pieces) - 1 + int(anchored)
        return cls(allow, parts, anchored, length)

    def matches(self, target: bytes) -> bool:
        """Greedy leftmost match of each literal piece: linear, never backtracks."""
        first, last = self.parts[0], self.parts[-1]
        if not target.startswith(first):
            return False
        if len(self.parts) == 1:
            return len(target) == len(first) if self.anchored else True
        pos = len(first)
        for piece in self.parts[1:-1]:
            i = target.find(piece, pos)
            if i < 0:
                return False
            pos = i + len(piece)
        if self.anchored:
            return len(target) - len(last) >= pos and target.endswith(last)
        return target.find(last, pos) >= 0


class RobotsTxt:
    """A robots.txt reduced to the rules that apply to one product token (RFC 9309)."""

    def __init__(self, text: str, user_agent: str) -> None:
        token = product_token(user_agent)
        groups: list[tuple[set[str], list[tuple[str, str]]]] = []
        members: Optional[list[tuple[str, str]]] = None
        collecting_agents = False
        for raw in _EOL_RE.split(text.lstrip("﻿")):
            line = raw.split("#", 1)[0]
            if ":" not in line:
                continue  # blank lines and junk don't end a group
            key, value = line.split(":", 1)
            key = " ".join(key.split()).lower()
            value = value.strip()
            if key in _UA_KEYS:
                if not collecting_agents:
                    members = []
                    groups.append((set(), members))
                    collecting_agents = True
                groups[-1][0].add(_agent_of(value))
            elif key in _GROUP_KEYS:
                collecting_agents = False
                if members is not None:  # lines before any User-agent belong to no group
                    members.append((key, value))
            # Sitemap and unknown keys are ignored and don't end the agent list.

        own = [m for agents, m in groups if token and token in agents]
        chosen = own or [m for agents, m in groups if "*" in agents]
        self.matched = "own" if own else ("*" if chosen else "none")
        self.rules: list[_RobotsRule] = []
        delays: list[float] = []
        rates: list[float] = []
        for group in chosen:
            for key, value in group:
                if key in _ALLOW_KEYS or key in _DISALLOW_KEYS:
                    rule = _RobotsRule.parse(key in _ALLOW_KEYS, value)
                    if rule is not None:
                        self.rules.append(rule)
                elif key in _CRAWL_DELAY_KEYS:
                    if _DECIMAL_RE.match(value):
                        d = float(value)
                        if math.isfinite(d) and d > 0:
                            delays.append(d)
                elif key in _REQUEST_RATE_KEYS:
                    m = _REQUEST_RATE_RE.match(value)
                    if m:
                        n, secs = int(m.group(1)), int(m.group(2)) * _UNIT_SEC[m.group(3).lower()]
                        if n > 0 and secs > 0:
                            rates.append(n / secs)
        self.crawl_delay: Optional[float] = max(delays) if delays else None
        self.request_rate: Optional[float] = min(rates) if rates else None

    def allows(self, url: str) -> bool:
        """Longest matching rule wins; on a tie Allow wins; no match means allowed."""
        target = _robots_target(url)
        best, verdict = -1, True
        for rule in self.rules:
            if rule.length >= best and rule.matches(target):
                if rule.length > best or rule.allow:
                    best, verdict = rule.length, rule.allow
        return verdict

    def max_rate(self) -> Optional[float]:
        """Requests per second this file asks for (Crawl-delay / Request-rate), if any."""
        rates = [r for r in (1.0 / self.crawl_delay if self.crawl_delay else None,
                             self.request_rate) if r]
        return min(rates) if rates else None


@dataclass
class _RobotsRules:
    robots: Optional[RobotsTxt]   # None: a blanket verdict (see allow_all)
    allow_all: bool
    unavailable: bool
    transient: bool
    reason: str
    expires_at: float

    def allows(self, url: str) -> bool:
        return self.robots.allows(url) if self.robots is not None else self.allow_all


# ---------------------------------------------------------- response cache

@dataclass
class _CacheEntry:
    status: int
    headers: dict[str, str]
    body: bytes
    etag: Optional[str]
    last_modified: Optional[str]


class _ConditionalCache:
    """ETag / Last-Modified store: in-memory LRU, optionally mirrored to a directory."""

    def __init__(self, cache_dir: Optional[Path]) -> None:
        self._dir = cache_dir
        self._mem: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _paths(self, key: str) -> tuple[Path, Path]:
        assert self._dir is not None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]
        return self._dir / f"{digest}.json", self._dir / f"{digest}.body"

    def get(self, key: str) -> Optional[_CacheEntry]:
        with self._lock:
            entry = self._mem.get(key)
            if entry is not None:
                self._mem.move_to_end(key)
                return entry
        if self._dir is None:
            return None
        meta_path, body_path = self._paths(key)
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            entry = _CacheEntry(meta["status"], meta["headers"], body_path.read_bytes(),
                                meta.get("etag"), meta.get("last_modified"))
        except (OSError, ValueError, KeyError):
            return None
        self._remember(key, entry)
        return entry

    def _remember(self, key: str, entry: _CacheEntry) -> None:
        with self._lock:
            self._mem[key] = entry
            self._mem.move_to_end(key)
            while len(self._mem) > CACHE_MAX_ENTRIES:
                self._mem.popitem(last=False)

    def put(self, key: str, resp: requests.Response, label: str) -> None:
        etag = resp.headers.get("ETag")
        last_modified = resp.headers.get("Last-Modified")
        if "no-store" in resp.headers.get("Cache-Control", "").lower():
            return
        if not (etag or last_modified) or len(resp.content) > CACHE_MAX_BODY:
            return
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in _UNCACHED_HEADERS}
        entry = _CacheEntry(resp.status_code, headers, resp.content, etag, last_modified)
        self._remember(key, entry)
        if self._dir is not None:
            self._write(key, entry, label)

    def update_validators(self, key: str, resp304: requests.Response, label: str) -> None:
        entry = self.get(key)
        if entry is None:
            return
        etag = resp304.headers.get("ETag") or entry.etag
        last_modified = resp304.headers.get("Last-Modified") or entry.last_modified
        if (etag, last_modified) != (entry.etag, entry.last_modified):
            entry.etag, entry.last_modified = etag, last_modified
            if self._dir is not None:
                self._write(key, entry, label)

    def _write(self, key: str, entry: _CacheEntry, label: str) -> None:
        meta_path, body_path = self._paths(key)
        meta = {"url": label, "status": entry.status, "headers": entry.headers,
                "etag": entry.etag, "last_modified": entry.last_modified}
        try:
            for path, data in ((body_path, entry.body),
                               (meta_path, _json.dumps(meta, indent=1).encode("utf-8"))):
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, path)
        except OSError as exc:
            log.warning("http cache write failed for %s: %s", label, exc)


# ------------------------------------------------------------ record mode

_EXT_BY_TYPE = (("json", ".json"), ("html", ".html"), ("xml", ".xml"), ("rss", ".xml"), ("text", ".txt"))


def _slug(text: str, limit: int = 80) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "_", text).strip("_")[:limit]


class _Recorder:
    """Writes each final response body + a small metadata file, for building fixtures.

    Request bodies and headers are never written: they are where keys and
    (one day) personal data would live, and fixtures end up in a public repo.
    File names carry the pid and a per-instance random tag, and files are opened
    with O_EXCL, so two clients (or processes) never overwrite each other.
    """

    def __init__(self, root: Path, wallclock: Callable[[], float]) -> None:
        self.root = root
        self._wallclock = wallclock
        self._counter = itertools.count(1)
        self._tag = secrets.token_hex(3)
        self._lock = threading.Lock()

    def write(self, method: str, url: str, resp: requests.Response) -> None:
        with self._lock:
            n = next(self._counter)
        parts = urllib.parse.urlsplit(url)
        ctype = resp.headers.get("Content-Type", "")
        ext = next((e for k, e in _EXT_BY_TYPE if k in ctype.lower()), ".bin")
        stamp = datetime.fromtimestamp(self._wallclock(), tz=timezone.utc)
        stem = (f"{stamp:%Y%m%dT%H%M%SZ}-{n:04d}-p{os.getpid()}{self._tag}-"
                f"{method.lower()}-{_slug(parts.path) or 'root'}")
        try:
            port = parts.port
        except ValueError:
            port = None
        host = _norm_host(parts.hostname or "")  # never netloc: it can hold user:password
        host_dir = self.root / (_slug(host + (f"_{port}" if port else "")) or "unknown-host")
        meta = {
            "method": method,
            "url": redact_url(url),
            "status": resp.status_code,
            "content_type": ctype,
            "etag": resp.headers.get("ETag"),
            "last_modified": resp.headers.get("Last-Modified"),
            "from_cache": bool(getattr(resp, "from_cache", False)),
            "recorded_at": stamp.isoformat(),
            "body_file": stem + ext,
        }
        try:
            host_dir.mkdir(parents=True, exist_ok=True)
            with open(host_dir / (stem + ext), "xb") as fh:
                fh.write(resp.content)
            with open(host_dir / (stem + ".meta.json"), "x", encoding="utf-8") as fh:
                fh.write(_json.dumps(meta, indent=1))
        except OSError as exc:
            log.warning("record mode: could not write %s: %s", stem, exc)


# ------------------------------------------------------------------ client

class PoliteClient:
    """requests.Session wrapper that every connector shares. See module docstring."""

    CONFIG_KEYS = (
        "user_agent", "timeout_sec", "default_rate_per_sec", "max_retries", "host_rates",
        "robots_exempt_hosts", "max_wait_sec", "backoff_base_sec", "backoff_max_sec",
        "robots_ttl_sec", "robots_retry_ttl_sec", "cache_dir", "denied_hosts",
        "max_response_bytes", "request_deadline_sec",
    )

    def __init__(
        self,
        *,
        user_agent: Optional[str] = None,
        timeout_sec: float = 20.0,
        default_rate_per_sec: float = 1.0,
        max_retries: int = 3,
        host_rates: Optional[Mapping[str, float]] = None,
        robots_exempt_hosts: Optional[Mapping[str, str]] = None,
        denied_hosts: Optional[Iterable[str]] = None,
        max_wait_sec: float = 120.0,
        backoff_base_sec: float = 1.0,
        backoff_max_sec: float = 60.0,
        robots_ttl_sec: float = 24 * 3600.0,
        robots_retry_ttl_sec: float = ROBOTS_RETRY_TTL_SEC,
        cache_dir: str | Path | None = None,
        record_dir: str | Path | None = None,
        max_response_bytes: Optional[int] = DEFAULT_MAX_RESPONSE_BYTES,
        request_deadline_sec: Optional[float] = DEFAULT_REQUEST_DEADLINE_SEC,
        session: Optional[requests.Session] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wallclock: Callable[[], float] = time.time,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.user_agent = (user_agent or DEFAULT_USER_AGENT).strip()
        self.timeout_sec = float(timeout_sec)
        self.default_rate_per_sec = float(default_rate_per_sec)
        self.max_retries = max(0, int(max_retries))
        self.max_wait_sec = float(max_wait_sec)
        self.backoff_base_sec = float(backoff_base_sec)
        self.backoff_max_sec = float(backoff_max_sec)
        self.robots_ttl_sec = float(robots_ttl_sec)
        self.robots_retry_ttl_sec = min(float(robots_retry_ttl_sec), self.robots_ttl_sec)
        if self.default_rate_per_sec <= 0:
            raise ValueError("default_rate_per_sec must be > 0")
        self.host_rates = {_norm_host(str(h)): float(r) for h, r in (host_rates or {}).items()}
        if any(r <= 0 for r in self.host_rates.values()):
            raise ValueError("host_rates values must be > 0")
        self.max_response_bytes = None if max_response_bytes is None else int(max_response_bytes)
        self.request_deadline_sec = None if request_deadline_sec is None else float(request_deadline_sec)
        if self.max_response_bytes is not None and self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be > 0")
        if self.request_deadline_sec is not None and not self.request_deadline_sec > 0:
            raise ValueError("request_deadline_sec must be > 0")
        self.robots_exempt_hosts = self._validate_exemptions(robots_exempt_hosts)
        self._exempt_rules: dict[str, list[tuple[str, str]]] = {}
        for key, reason in self.robots_exempt_hosts.items():
            host, _, rest = key.partition("/")
            self._exempt_rules.setdefault(host, []).append(("/" + rest if rest else "", reason))
        self.denied_hosts = self._validate_denied(denied_hosts)

        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = self.user_agent
        self._clock, self._sleep, self._wallclock = clock, sleep, wallclock
        self._rng = rng or random.Random()

        self._lock = threading.Lock()
        self._buckets: dict[str, TokenBucket] = {}            # by normalised host
        self._robots: dict[str, _RobotsRules] = {}            # by normalised origin
        self._robots_locks: dict[str, threading.Lock] = {}
        self._robots_rates: dict[str, dict[str, float]] = {}  # host -> origin -> req/s
        self._exempt_logged: set[str] = set()
        self._cache = _ConditionalCache(Path(cache_dir) if cache_dir else None)

        if record_dir is None:
            record_dir = os.environ.get(RECORD_ENV) or None
        self._recorder = _Recorder(Path(record_dir), wallclock) if record_dir else None
        if self._recorder:
            log.info("record mode on: responses saved under %s", self._recorder.root)

    @classmethod
    def from_config(cls, http_cfg: Optional[Mapping[str, Any]] = None, **overrides: Any) -> "PoliteClient":
        """Build from `config.sources()["http"]`; keyword overrides win (session, clock, ...)."""
        if http_cfg is None:
            from . import config
            http_cfg = config.sources().get("http") or {}
        kwargs: dict[str, Any] = {k: http_cfg[k] for k in cls.CONFIG_KEYS if http_cfg.get(k) is not None}
        if kwargs.get("cache_dir"):
            p = Path(kwargs["cache_dir"]).expanduser()
            kwargs["cache_dir"] = p if p.is_absolute() else ROOT / p
        kwargs.update(overrides)
        return cls(**kwargs)

    @staticmethod
    def _validate_exemptions(raw: Optional[Mapping[str, str]]) -> dict[str, str]:
        """{normalised "host" or "host/path/prefix/": reason}. A bad key raises:
        a widened exemption must never come from a typo."""
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise ValueError("robots_exempt_hosts must map host -> reason")
        out: dict[str, str] = {}
        for key, reason in raw.items():
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"robots_exempt_hosts[{key!r}] needs a reason string")
            text = str(key).strip()
            if "://" in text:
                parts = urllib.parse.urlsplit(text)
                if parts.query or parts.fragment:
                    raise ValueError(f"robots_exempt_hosts key {key!r} must be host[/path]")
                host, path = parts.hostname or "", parts.path
            else:
                host, _, path = text.partition("/")
                path = "/" + path if path else ""
            host = _norm_host(host)
            bad_path = any(c in path for c in "?#%\\") or any(
                seg in (".", "..") for seg in path.split("/"))
            if not host or "/" in host or bad_path:
                raise ValueError(f"robots_exempt_hosts key {key!r} must be host[/path]")
            if path.strip("/") == "":
                path = ""
            out[host + path] = reason.strip()
        return out

    @staticmethod
    def _path_under(url: str, prefix: str) -> bool:
        """Is the URL's path `prefix` or below it (segment boundary, percent-decoded)?
        Dot segments or backslashes anywhere in the path make the answer no."""
        try:
            path = urllib.parse.unquote(urllib.parse.urlsplit(url).path or "/")
        except ValueError:
            return False
        if "\\" in path or any(seg in (".", "..") for seg in path.split("/")):
            return False
        base = prefix.rstrip("/")
        return path == base or path.startswith(base + "/")

    def _exemption(self, url: str, host: str) -> Optional[tuple[str, str]]:
        """(matched key, reason) if robots.txt is not checked for this URL."""
        for prefix, reason in self._exempt_rules.get(host, ()):
            if not prefix or self._path_under(url, prefix):
                return host + prefix, reason
        return None

    @staticmethod
    def _validate_denied(raw: Any) -> frozenset[str]:
        """BUILTIN_DENIED_HOSTS plus the configured list. Bad entries raise ValueError:
        a client that can't tell which hosts are off limits must not be built."""
        if raw is None:
            items: list[Any] = []
        elif isinstance(raw, str):
            items = [raw]
        elif isinstance(raw, Mapping):
            items = list(raw.keys())
        elif isinstance(raw, Iterable):
            items = list(raw)
        else:
            raise ValueError("denied_hosts must be a list of host names")
        out: set[str] = set()
        for item in (*BUILTIN_DENIED_HOSTS, *items):
            if not isinstance(item, str):
                raise ValueError(f"denied_hosts entry {item!r} is not a host name")
            h = item.strip()
            if "://" in h:
                h = urllib.parse.urlsplit(h).hostname or ""
            h = _norm_host(h)
            if h.startswith("*."):
                h = h[2:]
            h = h.lstrip(".")
            try:
                h = h.encode("idna").decode("ascii")
            except UnicodeError:
                raise ValueError(f"denied_hosts entry {item!r} is not a host name") from None
            if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*", h):
                raise ValueError(f"denied_hosts entry {item!r} is not a host name")
            out.add(h)
        return frozenset(out)

    # ------------------------------------------------------------ public API

    def get(self, url: str, *, params: Optional[Mapping[str, Any]] = None,
            headers: Optional[Mapping[str, str]] = None, timeout: Optional[float] = None,
            **kwargs: Any) -> requests.Response:
        return self.request("GET", url, params=params, headers=headers, timeout=timeout, **kwargs)

    def post(self, url: str, *, json: Any = None, data: Any = None,
             headers: Optional[Mapping[str, str]] = None, timeout: Optional[float] = None,
             idempotent: bool = False, **kwargs: Any) -> requests.Response:
        """POST. Pass idempotent=True for read-only searches (e.g. Workday CXS) so
        5xx / connection errors may be retried; nothing here ever submits a form."""
        return self.request("POST", url, json=json, data=data, headers=headers,
                            timeout=timeout, idempotent=idempotent, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self._decode_json(self.get(url, **kwargs))

    def post_json(self, url: str, **kwargs: Any) -> Any:
        return self._decode_json(self.post(url, **kwargs))

    def close(self) -> None:
        self.session.close()

    def is_denied(self, url_or_host: str) -> bool:
        """True if the URL's host (or a bare host name) is on the do-not-automate list."""
        host = urllib.parse.urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
        return self._denied_match(_norm_host(host or "")) is not None

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        json: Any = None,
        data: Any = None,
        timeout: Optional[float] = None,
        idempotent: Optional[bool] = None,
        allow_redirects: bool = True,
        raise_for_status: bool = True,
        max_bytes: Optional[int] = None,
        deadline_sec: Optional[float] = None,
    ) -> requests.Response:
        """Send one logical request. Redirects (GET/HEAD only) are followed here, not by
        requests, so every hop gets its own denylist check, robots check and rate limit.
        Once a hop leaves the original origin (another host, port, or an https -> http
        downgrade), caller headers other than Accept / Accept-Language are dropped.

        `max_bytes` / `deadline_sec` override the client's max_response_bytes /
        request_deadline_sec for this call (see module docstring)."""
        method = method.upper()
        if max_bytes is None:
            max_bytes = self.max_response_bytes
        if deadline_sec is None:
            deadline_sec = self.request_deadline_sec
        deadline_at = None if deadline_sec is None else self._clock() + float(deadline_sec)
        limits = (max_bytes, deadline_at, deadline_sec)
        if idempotent is None:
            idempotent = method in _SAFE_METHODS
        target = _with_params(url, params)
        first = urllib.parse.urlsplit(target)
        hop_headers, hop_json, hop_data = headers, json, data
        for _hop in range(MAX_REDIRECTS + 1):
            resp = self._send(method, target, hop_headers, hop_json, hop_data, timeout, idempotent,
                              limits)
            if not (allow_redirects and resp.is_redirect and method in ("GET", "HEAD")):
                break
            nxt = self._redirect_target(target, resp)
            resp.close()
            if not _same_trust(first, urllib.parse.urlsplit(nxt)):
                if hop_headers:
                    hop_headers = {k: v for k, v in hop_headers.items()
                                   if k.lower() in _REDIRECT_SAFE_HEADERS}
                hop_json = hop_data = None
            target = nxt
        else:
            raise HttpError(f"more than {MAX_REDIRECTS} redirects: {redact_url(url)}")
        if self._recorder is not None:
            self._recorder.write(method, target, resp)
        if raise_for_status and resp.status_code >= 400:
            raise HttpStatusError(method, redact_url(target), resp)
        return resp

    # -------------------------------------------------------------- internals

    def _redirect_target(self, current: str, resp: requests.Response) -> str:
        """Absolute, canonical next URL (an empty path becomes '/', the host IDNA-encoded)."""
        location = self.session.get_redirect_target(resp) or resp.headers.get("Location", "")
        try:
            return _with_params(urllib.parse.urljoin(current, location), None)
        except (requests.RequestException, ValueError) as exc:
            raise HttpError(f"unusable redirect from {redact_url(current)}") from exc

    def _send(self, method: str, url: str, headers: Optional[Mapping[str, str]], json: Any,
              data: Any, timeout: Optional[float], idempotent: bool,
              limits: tuple[Optional[int], Optional[float], Optional[float]] = (None, None, None),
              ) -> requests.Response:
        parts = urllib.parse.urlsplit(url)
        label = redact_url(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise HttpError(f"unsupported URL: {label}")
        host = _norm_host(parts.hostname)
        self._check_denied(host, label)  # before any I/O, robots.txt included
        try:
            origin = _origin(parts)
        except ValueError:
            raise HttpError(f"unsupported URL: {label}") from None
        bucket = self._bucket(host)
        self._check_robots(url, label, origin, host, bucket)

        send_headers: CaseInsensitiveDict = CaseInsensitiveDict(headers or {})
        send_headers["User-Agent"] = self.user_agent   # honest UA, never caller-supplied
        cache_key = url if method == "GET" else None
        cached = self._cache.get(cache_key) if cache_key else None
        if cached is not None:
            if cached.etag:
                send_headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                send_headers["If-Modified-Since"] = cached.last_modified

        max_bytes, deadline_at, deadline_sec = limits
        attempt = 0
        while True:
            self._wait(bucket, label, deadline_at, deadline_sec)
            per_read = timeout or self.timeout_sec
            capped = False
            if deadline_at is not None:
                left = deadline_at - self._clock()
                if left <= 0:
                    raise RequestDeadlineExceeded(label, deadline_sec)
                if left < per_read:
                    per_read, capped = left, True
            try:
                resp = self.session.request(
                    method, url, headers=send_headers, json=json, data=data,
                    timeout=per_read, allow_redirects=False, stream=True,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if capped and isinstance(exc, requests.Timeout):
                    raise RequestDeadlineExceeded(label, deadline_sec) from exc
                if not idempotent or attempt >= self.max_retries:
                    raise HttpConnectionError(
                        f"{method} {label} failed after {attempt + 1} attempt(s): {type(exc).__name__}"
                    ) from exc
                delay = self._backoff(attempt)
                log.info("%s %s: %s; retry %d/%d in %.1fs", method, label,
                         type(exc).__name__, attempt + 1, self.max_retries, delay)
                bucket.penalize(delay)
                attempt += 1
                continue

            resp.from_cache = False  # type: ignore[attr-defined]
            if resp.status_code == 304 and cached is not None and cache_key is not None:
                log.debug("%s %s -> 304, served from cache", method, label)
                return self._from_cache(cache_key, cached, resp, label)
            if self._retryable(resp.status_code, idempotent):
                delay = self._retry_delay(resp, attempt)
                bucket.penalize(delay)  # every later request to this host waits too
                if attempt < self.max_retries:
                    log.info("%s %s -> HTTP %d; retry %d/%d in %.1fs", method, label,
                             resp.status_code, attempt + 1, self.max_retries, delay)
                    resp.close()
                    attempt += 1
                    continue
            try:
                self._read_body(resp, max_bytes, deadline_at, deadline_sec, label)
            except _BodyReadError as exc:
                cause = exc.__cause__ or exc
                if not idempotent or attempt >= self.max_retries:
                    raise HttpConnectionError(
                        f"{method} {label} body read failed after {attempt + 1} attempt(s): "
                        f"{type(cause).__name__}"
                    ) from cause
                delay = self._backoff(attempt)
                log.info("%s %s: body read %s; retry %d/%d in %.1fs", method, label,
                         type(cause).__name__, attempt + 1, self.max_retries, delay)
                bucket.penalize(delay)
                attempt += 1
                continue
            break

        if cache_key is not None and resp.status_code == 200:
            self._cache.put(cache_key, resp, label)
        log.debug("%s %s -> HTTP %d", method, label, resp.status_code)
        return resp

    def _wait(self, bucket: TokenBucket, label: str, deadline_at: Optional[float] = None,
              deadline_sec: Optional[float] = None) -> None:
        delay = bucket.reserve()
        if delay > self.max_wait_sec:
            bucket.refund()
            raise RetryLater(label, delay)
        if deadline_at is not None and self._clock() + delay >= deadline_at:
            bucket.refund()
            raise RequestDeadlineExceeded(label, deadline_sec)
        if delay > 0:
            self._sleep(delay)

    def _read_body(self, resp: requests.Response, max_bytes: Optional[int],
                   deadline_at: Optional[float], deadline_sec: Optional[float],
                   label: str, *, truncate: bool = False) -> bytes:
        """Read the whole body into resp._content, bounded in size and time.

        Reads with read1() so each step returns after one socket read, and checks
        the deadline between reads (the socket timeout is lowered to what is left).
        Over `max_bytes`: ResponseTooLarge, or with truncate=True keep the first
        max_bytes. Broken connections come back as _BodyReadError."""
        if getattr(resp, "_content_consumed", False) or resp._content is not False:
            body = resp.content or b""   # already in memory (preloading adapters, fakes)
            if max_bytes is not None and len(body) > max_bytes:
                if truncate:
                    return body[:max_bytes]
                resp.close()
                raise ResponseTooLarge(label, max_bytes)
            return body
        length = (resp.headers.get("Content-Length") or "").strip()
        if not truncate and max_bytes is not None and length.isdigit() and int(length) > max_bytes:
            resp.close()
            raise ResponseTooLarge(label, max_bytes, int(length))
        raw = resp.raw
        read1 = getattr(raw, "read1", None)
        chunks = None if read1 is not None else resp.iter_content(_READ_CHUNK)
        parts: list[bytes] = []
        size = 0
        try:
            while True:
                if deadline_at is not None:
                    left = deadline_at - self._clock()
                    if left <= 0:
                        raise RequestDeadlineExceeded(label, deadline_sec)
                    self._lower_socket_timeout(raw, left)
                try:
                    data = (read1(_READ_CHUNK, decode_content=True) if read1 is not None
                            else next(chunks, b""))  # type: ignore[arg-type]
                except (requests.RequestException, OSError, ValueError,
                        urllib3_exceptions.HTTPError, http_client.HTTPException) as exc:
                    if deadline_at is not None and self._clock() >= deadline_at:
                        raise RequestDeadlineExceeded(label, deadline_sec) from exc
                    raise _BodyReadError() from exc
                if not data:
                    break
                size += len(data)
                if max_bytes is not None and size > max_bytes:
                    if truncate:
                        parts.append(data[:max_bytes - (size - len(data))])
                        break
                    raise ResponseTooLarge(label, max_bytes)
                parts.append(data)
        except (ResponseTooLarge, RequestDeadlineExceeded, _BodyReadError):
            resp.close()
            raise
        body = b"".join(parts)
        resp._content, resp._content_consumed = body, True  # type: ignore[attr-defined]
        if truncate:
            resp.close()
        else:
            try:
                raw.release_conn()
            except Exception:  # noqa: BLE001 — best effort; the body is already in memory
                pass
        return body

    @staticmethod
    def _lower_socket_timeout(raw: Any, left: float) -> None:
        """Best effort: make the next socket read give up when the deadline does.
        urllib3 resets the timeout when the connection is reused."""
        sock = getattr(getattr(raw, "connection", None), "sock", None)
        if sock is None:
            return
        try:
            current = sock.gettimeout()
            if current is None or left < current:
                sock.settimeout(max(0.001, left))
        except (OSError, AttributeError):
            pass

    @staticmethod
    def _retryable(status: int, idempotent: bool) -> bool:
        if status in _RETRY_ANY_METHOD:
            return True
        return idempotent and 500 <= status < 600 and status not in _NEVER_RETRY_5XX

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with equal jitter: half fixed, half random."""
        base = min(self.backoff_max_sec, self.backoff_base_sec * (2 ** attempt))
        return base / 2 + self._rng.uniform(0, base / 2)

    def _retry_delay(self, resp: requests.Response, attempt: int) -> float:
        after = parse_retry_after(resp.headers.get("Retry-After"), self._wallclock())
        return after if after is not None else self._backoff(attempt)

    def _from_cache(self, key: str, entry: _CacheEntry, resp304: requests.Response,
                    label: str) -> requests.Response:
        self._cache.update_validators(key, resp304, label)
        r = requests.Response()
        r.status_code = entry.status
        r.reason = "OK"
        r._content = entry.body
        r._content_consumed = True  # no socket behind it; lets close()/iter_content work
        r.headers = CaseInsensitiveDict(entry.headers)
        for h in ("ETag", "Last-Modified", "Cache-Control", "Expires", "Date"):
            if h in resp304.headers:
                r.headers[h] = resp304.headers[h]
        r.encoding = requests.utils.get_encoding_from_headers(r.headers)
        r.url, r.request, r.elapsed = resp304.url, resp304.request, resp304.elapsed
        r.from_cache = True  # type: ignore[attr-defined]
        resp304.close()
        return r

    @staticmethod
    def _decode_json(resp: requests.Response) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            ctype = resp.headers.get("Content-Type", "?")
            raise InvalidJSON(f"{redact_url(resp.url or '')} returned non-JSON ({ctype})",
                              response=resp) from exc

    # ------------------------------------------------------ hosts and robots

    def _denied_match(self, host: str) -> Optional[str]:
        """The denylist entry `host` equals or is a subdomain of (label boundary), if any."""
        labels = host.split(".")
        for i in range(len(labels)):
            suffix = ".".join(labels[i:])
            if suffix in self.denied_hosts:
                return suffix
        return None

    def _check_denied(self, host: str, label: str) -> None:
        entry = self._denied_match(host)
        if entry is not None:
            log.warning("refusing %s: %s is on the do-not-automate list (denied_hosts)", label, entry)
            raise HostDenied(label, f"do-not-automate host ({entry}) refused")

    def _rate_for(self, host: str) -> float:
        """Exact host first, then the longest `.suffix` key (e.g. ".myworkdayjobs.com")."""
        if host in self.host_rates:
            return self.host_rates[host]
        suffixes = [k for k in self.host_rates
                    if k.startswith(".") and (host.endswith(k) or host == k[1:])]
        return self.host_rates[max(suffixes, key=len)] if suffixes else self.default_rate_per_sec

    def _bucket(self, host: str) -> TokenBucket:
        """One bucket per host name, whatever the scheme or port (D7: per-host limits)."""
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                bucket = self._buckets[host] = TokenBucket(self._rate_for(host), clock=self._clock)
            return bucket

    def _check_robots(self, url: str, label: str, origin: str, host: str,
                      bucket: TokenBucket) -> None:
        exempt = self._exemption(url, host)
        if exempt is not None:
            key, reason = exempt
            with self._lock:
                first = key not in self._exempt_logged
                self._exempt_logged.add(key)
            if first:
                log.info("robots.txt not checked for %s (exempt: %s)", key, reason)
            return
        rules = self._robots_for(origin, host, bucket)
        if not rules.allows(url):
            if rules.unavailable:
                raise RobotsUnavailable(label, rules.reason, transient=rules.transient)
            raise RobotsDisallowed(label, rules.reason)

    def _robots_for(self, origin: str, host: str, bucket: TokenBucket) -> _RobotsRules:
        with self._lock:
            lock = self._robots_locks.setdefault(origin, threading.Lock())
        with lock:  # one fetch per origin even when threads race
            rules = self._robots.get(origin)
            if rules is None or self._clock() >= rules.expires_at:
                rules = self._robots[origin] = self._fetch_robots(origin, bucket)
                self._apply_robots_rate(origin, host, bucket, rules)
            return rules

    def _fetch_robots(self, origin: str, bucket: TokenBucket) -> _RobotsRules:
        """GET <origin>/robots.txt (redirects followed here, each hop denylist-checked)
        and turn the answer into a verdict; see the table in the module docstring."""

        def verdict(robots: Optional[RobotsTxt], allow: bool, reason: str, *,
                    unavailable: bool = False, transient: bool = False) -> _RobotsRules:
            ttl = self.robots_retry_ttl_sec if transient else self.robots_ttl_sec
            return _RobotsRules(robots, allow, unavailable, transient, reason, self._clock() + ttl)

        def blocked(reason: str, transient: bool) -> _RobotsRules:
            ttl = self.robots_retry_ttl_sec if transient else self.robots_ttl_sec
            log.warning("%s for %s; treating host as disallowed for the next %.0f min",
                        reason, origin, ttl / 60)
            return verdict(None, False, reason, unavailable=True, transient=transient)

        url, hop_bucket = origin + "/robots.txt", bucket
        retries = min(self.max_retries, ROBOTS_MAX_RETRIES)
        attempt = redirects = 0
        while True:
            hop = urllib.parse.urlsplit(url)
            hop_host = _norm_host(hop.hostname or "")
            if hop.scheme not in ("http", "https") or not hop_host:
                return blocked("robots.txt redirected to an unsupported URL", True)
            denied = self._denied_match(hop_host)
            if denied is not None:
                return blocked(f"robots.txt redirects to do-not-automate host {denied}", False)

            self._wait(hop_bucket, redact_url(url))  # RetryLater propagates; nothing is cached
            robots_deadline = self._clock() + ROBOTS_DEADLINE_SEC
            retry_delay: Optional[float] = None
            try:
                resp = self.session.get(
                    url, timeout=self.timeout_sec, allow_redirects=False, stream=True,
                    headers={"User-Agent": self.user_agent, "Accept": "text/plain, */*;q=0.5"},
                )
                try:
                    status = resp.status_code
                    if resp.is_redirect:
                        redirects += 1
                        if redirects > ROBOTS_MAX_REDIRECTS:
                            return blocked(f"robots.txt: more than {ROBOTS_MAX_REDIRECTS} redirects", True)
                        try:
                            url = self._redirect_target(url, resp)
                        except HttpError:
                            return blocked("robots.txt: unusable redirect", True)
                        hop_bucket = self._bucket(_norm_host(urllib.parse.urlsplit(url).hostname or ""))
                        continue
                    if 200 <= status < 300:
                        try:
                            body = self._read_body(resp, ROBOTS_MAX_BYTES, robots_deadline,
                                                   ROBOTS_DEADLINE_SEC, redact_url(url),
                                                   truncate=True)
                        except _BodyReadError as exc:
                            raise requests.ConnectionError("robots.txt body read failed") from exc
                        robots = RobotsTxt(body.decode("utf-8-sig", errors="replace"), self.user_agent)
                        return verdict(robots, True, "disallowed by robots.txt")
                    if status in (401, 403):
                        return blocked(f"robots.txt answered HTTP {status}", False)
                    if 400 <= status < 500 and status != 429:
                        log.info("no robots.txt for %s (HTTP %d); all paths allowed", origin, status)
                        return verdict(None, True, "")
                    failure = f"robots.txt answered HTTP {status}"
                    if status == 429 or (500 <= status < 600 and status not in _NEVER_RETRY_5XX):
                        retry_delay = self._retry_delay(resp, attempt)
                    else:
                        return blocked(failure, True)  # 1xx, 3xx without Location, 501, 505 ...
                finally:
                    resp.close()
            except requests.RequestException as exc:  # connect, timeout, broken body ...
                failure = f"robots.txt unreachable ({type(exc).__name__})"
                retry_delay = self._backoff(attempt)

            if attempt >= retries:
                return blocked(failure, True)
            log.info("%s for %s; retry %d/%d in %.1fs", failure, origin, attempt + 1, retries, retry_delay)
            hop_bucket.penalize(retry_delay)
            attempt += 1

    def _apply_robots_rate(self, origin: str, host: str, bucket: TokenBucket,
                           rules: _RobotsRules) -> None:
        """Bucket rate = the slowest of the configured rate and every cached robots.txt
        on this host (http and https share one bucket but have their own robots.txt)."""
        asked = rules.robots.max_rate() if rules.robots is not None else None
        with self._lock:
            per_origin = self._robots_rates.setdefault(host, {})
            if asked:
                per_origin[origin] = asked
            else:
                per_origin.pop(origin, None)
            rate = min([self._rate_for(host), *per_origin.values()])
        if rate != bucket.rate:
            log.info("robots.txt for %s sets the pace: %.3g req/s", origin, rate)
            bucket.set_rate(rate)


# ------------------------------------------------------- process default

_default: Optional[PoliteClient] = None
_default_lock = threading.Lock()


def default_client() -> PoliteClient:
    """The process-wide client. Sharing one instance is what makes the per-host
    limits and the robots cache hold across every connector in a run."""
    global _default
    with _default_lock:
        if _default is None:
            _default = PoliteClient.from_config()
        return _default


def reset_default_client() -> None:
    """Drop the shared client so the next default_client() re-reads config."""
    global _default
    with _default_lock:
        if _default is not None:
            _default.close()
        _default = None
