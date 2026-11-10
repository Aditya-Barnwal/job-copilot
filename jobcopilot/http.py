"""Polite HTTP client shared by every connector (plan decision D7).

Politeness only holds if it can't be skipped, so every network call a
connector makes goes through `PoliteClient`, which:

  * sends the honest User-Agent from config/sources.yaml (callers can't override it);
  * rate-limits per host with a token bucket, slowing further when robots.txt
    sets Crawl-delay / Request-rate;
  * reads robots.txt before the first request to a host and refuses a
    disallowed URL with `RobotsDisallowed` (the request is never sent);
  * retries only 429 / 5xx / connection errors, honouring Retry-After, with
    exponential backoff + jitter; other 4xx are never retried;
  * revalidates GETs with ETag / Last-Modified and serves the cached body on 304;
  * writes response bodies to $JOBCOPILOT_RECORD_DIR so test fixtures can be
    built from real traffic.

robots.txt handling (RFC 9309, conservative where it is vague):
  2xx             parse; longest matching rule wins, ties go to Allow, `*`/`$` honoured
  401 / 403       disallow-all (stdlib behaviour; an auth wall is not consent)
  other 4xx       no robots file, so allow all (404 / 410 are the usual case)
  429 / 5xx / net unreachable: disallow-all for this process, with a warning
Hosts in `robots_exempt_hosts` (documented public APIs only, each with a
reason) skip the robots check but are still rate-limited.

Logs and error messages carry `redact_url()` output only: Adzuna puts its API
key in the query string, and requests' own exception text would leak it.
"""
from __future__ import annotations

import hashlib
import itertools
import json as _json
import logging
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.robotparser
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import requests
from requests.structures import CaseInsensitiveDict

from .config import DEFAULTS, ROOT

log = logging.getLogger("jobcopilot.http")

RECORD_ENV = "JOBCOPILOT_RECORD_DIR"
DEFAULT_USER_AGENT: str = DEFAULTS["sources"]["http"]["user_agent"]
MAX_REDIRECTS = 5
ROBOTS_MAX_BYTES = 500 * 1024          # RFC 9309 §2.5: parse at least 500 KiB
CACHE_MAX_ENTRIES = 512                # in-memory LRU; the API server is long-lived
CACHE_MAX_BODY = 25 * 1024 * 1024
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 429 and 503 mean "not processed", so they are safe to retry even for POST.
_RETRY_ANY_METHOD = frozenset({429, 503})
_NEVER_RETRY_5XX = frozenset({501, 505})  # permanent by definition
_SECRET_PARAMS = frozenset({
    "app_id", "app_key", "api_key", "apikey", "key", "token", "access_token",
    "auth", "secret", "client_secret", "password", "signature", "sig",
})
_UNCACHED_HEADERS = ("set-cookie", "content-encoding", "content-length", "transfer-encoding")


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
    """robots.txt couldn't be read (network, 5xx, 401/403), so the host is treated as disallowed."""


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


def parse_retry_after(value: Optional[str], now: float) -> Optional[float]:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date), or None."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - now)


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
        """Make the next reservation wait at least `seconds` (Retry-After / backoff)."""
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

def _robots_target(url: str) -> str:
    """Path+query quoted the same way robotparser quotes rule paths."""
    p = urllib.parse.urlparse(urllib.parse.unquote(url))
    target = urllib.parse.urlunparse(("", "", p.path, p.params, p.query, ""))
    return urllib.parse.quote(target) or "/"


def _rule_matches(rule_path: str, target: str) -> bool:
    # robotparser stores rules percent-quoted, so `*` is %2A and `$` is %24.
    anchored = rule_path.endswith("%24")
    core = rule_path[:-3] if anchored else rule_path
    pattern = ".*".join(re.escape(part) for part in core.split("%2A"))
    return re.match(pattern + ("$" if anchored else ""), target) is not None


@dataclass
class _RobotsRules:
    parser: Optional[urllib.robotparser.RobotFileParser]
    allow_all: bool
    unavailable: bool
    reason: str
    fetched_at: float

    def allows(self, user_agent: str, url: str) -> bool:
        """RFC 9309 matching: the stdlib's can_fetch uses first-match and
        ignores wildcards, which would let `Allow: /` override `Disallow: /private/`."""
        if self.parser is None:
            return self.allow_all
        rp = self.parser
        entry = next((e for e in rp.entries if e.applies_to(user_agent)), rp.default_entry)
        if entry is None:
            return True
        target = _robots_target(url)
        best_len, verdict = -1, True
        for rule in entry.rulelines:
            if _rule_matches(rule.path, target):
                n = len(rule.path)
                if n > best_len or (n == best_len and rule.allowance):
                    best_len, verdict = n, rule.allowance
        return verdict


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
    """

    def __init__(self, root: Path, wallclock: Callable[[], float]) -> None:
        self.root = root
        self._wallclock = wallclock
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def write(self, method: str, url: str, resp: requests.Response) -> None:
        with self._lock:
            n = next(self._counter)
        parts = urllib.parse.urlsplit(url)
        ctype = resp.headers.get("Content-Type", "")
        ext = next((e for k, e in _EXT_BY_TYPE if k in ctype.lower()), ".bin")
        stamp = datetime.fromtimestamp(self._wallclock(), tz=timezone.utc)
        stem = f"{stamp:%Y%m%dT%H%M%SZ}-{n:04d}-{method.lower()}-{_slug(parts.path) or 'root'}"
        host_dir = self.root / _slug(parts.netloc)
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
            (host_dir / (stem + ext)).write_bytes(resp.content)
            (host_dir / (stem + ".meta.json")).write_text(_json.dumps(meta, indent=1), encoding="utf-8")
        except OSError as exc:
            log.warning("record mode: could not write %s: %s", stem, exc)


# ------------------------------------------------------------------ client

class PoliteClient:
    """requests.Session wrapper that every connector shares. See module docstring."""

    CONFIG_KEYS = (
        "user_agent", "timeout_sec", "default_rate_per_sec", "max_retries", "host_rates",
        "robots_exempt_hosts", "max_wait_sec", "backoff_base_sec", "backoff_max_sec",
        "robots_ttl_sec", "cache_dir",
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
        max_wait_sec: float = 120.0,
        backoff_base_sec: float = 1.0,
        backoff_max_sec: float = 60.0,
        robots_ttl_sec: float = 24 * 3600.0,
        cache_dir: str | Path | None = None,
        record_dir: str | Path | None = None,
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
        if self.default_rate_per_sec <= 0:
            raise ValueError("default_rate_per_sec must be > 0")
        self.host_rates = {str(h).lower(): float(r) for h, r in (host_rates or {}).items()}
        if any(r <= 0 for r in self.host_rates.values()):
            raise ValueError("host_rates values must be > 0")
        self.robots_exempt_hosts = self._validate_exemptions(robots_exempt_hosts)

        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = self.user_agent
        self._clock, self._sleep, self._wallclock = clock, sleep, wallclock
        self._rng = rng or random.Random()

        self._lock = threading.Lock()
        self._buckets: dict[str, TokenBucket] = {}
        self._robots: dict[str, _RobotsRules] = {}
        self._robots_locks: dict[str, threading.Lock] = {}
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
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise ValueError("robots_exempt_hosts must map host -> reason")
        out: dict[str, str] = {}
        for host, reason in raw.items():
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"robots_exempt_hosts[{host!r}] needs a reason string")
            out[str(host).lower()] = reason.strip()
        return out

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
    ) -> requests.Response:
        """Send one logical request. Redirects (GET/HEAD only) are followed here, not by
        requests, so every hop gets its own robots check and rate limit."""
        method = method.upper()
        if idempotent is None:
            idempotent = method in _SAFE_METHODS
        target = _with_params(url, params)
        for _hop in range(MAX_REDIRECTS + 1):
            resp = self._send(method, target, headers, json, data, timeout, idempotent)
            if not (allow_redirects and resp.is_redirect and method in ("GET", "HEAD")):
                break
            target = urllib.parse.urljoin(target, resp.headers["Location"])
            resp.close()
        else:
            raise HttpError(f"more than {MAX_REDIRECTS} redirects: {redact_url(url)}")
        if self._recorder is not None:
            self._recorder.write(method, target, resp)
        if raise_for_status and resp.status_code >= 400:
            raise HttpStatusError(method, redact_url(target), resp)
        return resp

    # -------------------------------------------------------------- internals

    def _send(self, method: str, url: str, headers: Optional[Mapping[str, str]], json: Any,
              data: Any, timeout: Optional[float], idempotent: bool) -> requests.Response:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise HttpError(f"unsupported URL: {redact_url(url)}")
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        host = parts.hostname.lower()
        label = redact_url(url)
        bucket = self._bucket(origin, host)
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

        attempt = 0
        while True:
            self._wait(bucket, label)
            try:
                resp = self.session.request(
                    method, url, headers=send_headers, json=json, data=data,
                    timeout=timeout or self.timeout_sec, allow_redirects=False,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
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
            break

        if cache_key is not None and resp.status_code == 200:
            self._cache.put(cache_key, resp, label)
        log.debug("%s %s -> HTTP %d", method, label, resp.status_code)
        return resp

    def _wait(self, bucket: TokenBucket, label: str) -> None:
        delay = bucket.reserve()
        if delay > self.max_wait_sec:
            bucket.refund()
            raise RetryLater(label, delay)
        if delay > 0:
            self._sleep(delay)

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

    def _rate_for(self, host: str) -> float:
        """Exact host first, then the longest `.suffix` key (e.g. ".myworkdayjobs.com")."""
        if host in self.host_rates:
            return self.host_rates[host]
        suffixes = [k for k in self.host_rates
                    if k.startswith(".") and (host.endswith(k) or host == k[1:])]
        return self.host_rates[max(suffixes, key=len)] if suffixes else self.default_rate_per_sec

    def _bucket(self, origin: str, host: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(origin)
            if bucket is None:
                bucket = self._buckets[origin] = TokenBucket(self._rate_for(host), clock=self._clock)
            return bucket

    def _check_robots(self, url: str, label: str, origin: str, host: str,
                      bucket: TokenBucket) -> None:
        reason = self.robots_exempt_hosts.get(host)
        if reason is not None:
            with self._lock:
                first = origin not in self._exempt_logged
                self._exempt_logged.add(origin)
            if first:
                log.info("robots.txt not checked for %s (exempt: %s)", host, reason)
            return
        rules = self._robots_for(origin, host, bucket)
        if not rules.allows(self.user_agent, url):
            err = RobotsUnavailable if rules.unavailable else RobotsDisallowed
            raise err(label, rules.reason)

    def _robots_for(self, origin: str, host: str, bucket: TokenBucket) -> _RobotsRules:
        with self._lock:
            lock = self._robots_locks.setdefault(origin, threading.Lock())
        with lock:  # one fetch per host even when threads race
            rules = self._robots.get(origin)
            if rules is None or self._clock() - rules.fetched_at > self.robots_ttl_sec:
                rules = self._robots[origin] = self._fetch_robots(origin, host, bucket)
            return rules

    def _fetch_robots(self, origin: str, host: str, bucket: TokenBucket) -> _RobotsRules:
        robots_url = origin + "/robots.txt"
        now = self._clock()

        def blanket(allow: bool, reason: str, unavailable: bool) -> _RobotsRules:
            return _RobotsRules(None, allow, unavailable, reason, now)

        try:
            self._wait(bucket, robots_url)
            resp = self.session.get(
                robots_url, timeout=self.timeout_sec, allow_redirects=True,
                headers={"User-Agent": self.user_agent, "Accept": "text/plain, */*;q=0.5"},
            )
        except RetryLater:
            raise
        except requests.RequestException as exc:
            log.warning("robots.txt unreachable for %s (%s); treating host as disallowed for this run",
                        origin, type(exc).__name__)
            return blanket(False, f"robots.txt unreachable ({type(exc).__name__})", True)

        status = resp.status_code
        try:
            if 200 <= status < 300:
                text = resp.content[:ROBOTS_MAX_BYTES].decode("utf-8", errors="replace")
                rp = urllib.robotparser.RobotFileParser(robots_url)
                rp.parse(text.splitlines())
                self._apply_robots_rate(origin, host, bucket, rp)
                return _RobotsRules(rp, True, False, "disallowed by robots.txt", now)
            if status in (401, 403) or status == 429 or status >= 500:
                log.warning("robots.txt for %s answered HTTP %d; treating host as disallowed for this run",
                            origin, status)
                return blanket(False, f"robots.txt answered HTTP {status}", True)
            if 400 <= status < 500:
                log.info("no robots.txt for %s (HTTP %d); all paths allowed", origin, status)
                return blanket(True, "", False)
            log.warning("robots.txt for %s: unexpected HTTP %d; treating host as disallowed", origin, status)
            return blanket(False, f"robots.txt answered HTTP {status}", True)
        finally:
            resp.close()

    def _apply_robots_rate(self, origin: str, host: str, bucket: TokenBucket,
                           rp: urllib.robotparser.RobotFileParser) -> None:
        rate = self._rate_for(host)
        delay = rp.crawl_delay(self.user_agent)
        if delay:
            rate = min(rate, 1.0 / float(delay))
        req_rate = rp.request_rate(self.user_agent)
        if req_rate and req_rate.requests > 0 and req_rate.seconds > 0:
            rate = min(rate, req_rate.requests / req_rate.seconds)
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
