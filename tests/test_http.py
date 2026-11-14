"""Offline tests for jobcopilot/http.py (docs/DESIGN.md §3) and the connectors that use it.

Network: only a stdlib http.server on 127.0.0.1 or a fake requests adapter.
An autouse guard fails any test that tries to open a socket elsewhere.
Time: a fake clock whose sleep() just advances it, so backoff and rate
limits are asserted exactly and the suite runs instantly.
"""
from __future__ import annotations

import json
import logging
import random
import socket
import sys
import threading
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import urllib3.util.connection
from requests.structures import CaseInsensitiveDict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobcopilot import http as jhttp  # noqa: E402
from jobcopilot.connectors import GreenhouseConnector, WorkdayConnector  # noqa: E402
from jobcopilot.connectors.base import DEFAULT_HEADERS  # noqa: E402
from jobcopilot.http import (  # noqa: E402
    HttpConnectionError,
    HttpStatusError,
    PoliteClient,
    RetryLater,
    RobotsDisallowed,
    RobotsUnavailable,
    TokenBucket,
    parse_retry_after,
    redact_url,
)

FIX = Path(__file__).resolve().parent / "fixtures"
UA = "job-copilot/2.0 (personal, non-commercial; test)"
JSON = {"Content-Type": "application/json"}
EXEMPT = {"127.0.0.1": "local test server"}

Reply = tuple[int, dict[str, str], bytes]


# ------------------------------------------------------------------ harness

@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No real network, no real config/data, no shared default client."""
    real = urllib3.util.connection.create_connection

    def guarded(address, *args, **kwargs):
        if address[0] not in ("127.0.0.1", "localhost"):
            raise AssertionError(f"test tried to reach the network: {address[0]}")
        return real(address, *args, **kwargs)

    monkeypatch.setattr(urllib3.util.connection, "create_connection", guarded)
    monkeypatch.delenv(jhttp.RECORD_ENV, raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sources.yaml").write_text(
        f"http:\n  user_agent: \"{UA}\"\n  default_rate_per_sec: 1000\n  max_retries: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JOBCOPILOT_CONFIG_DIR", str(cfg))
    jhttp.reset_default_client()
    yield
    jhttp.reset_default_client()


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds

    def wall(self) -> float:
        return 1_700_000_000.0 + self.now

    def big_sleeps(self) -> list[float]:
        """Sleeps that came from a real limit, not the 1 ms spacing of the fast default rate."""
        return [s for s in self.sleeps if s > 0.01]


class LocalServer:
    """Threaded 127.0.0.1 server. A route is a list of replies served in order
    (the last one repeats); a reply may be a callable of the recorded hit."""

    def __init__(self) -> None:
        self.routes: dict[str, list[Any]] = {}
        self.hits: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                hit = {
                    "method": self.command,
                    "path": parts.path,
                    "query": parse_qs(parts.query),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": self.rfile.read(length) if length else b"",
                }
                outer.hits.append(hit)
                replies = outer.routes.get(parts.path)
                if not replies:
                    reply: Any = (404, {}, b"not found")
                else:
                    reply = replies.pop(0) if len(replies) > 1 else replies[0]
                if callable(reply):
                    reply = reply(hit)
                status, headers, body = reply
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(0 if status == 304 else len(body)))
                self.end_headers()
                if status != 304:
                    self.wfile.write(body)

            do_GET = do_POST = _serve

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # A short poll interval keeps shutdown() (and so each test) fast.
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}{path}"

    def route(self, path: str, *replies: Any) -> None:
        self.routes[path] = list(replies)

    def paths(self) -> list[str]:
        return [h["path"] for h in self.hits]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = LocalServer()
    yield s
    s.close()


@pytest.fixture
def server2():
    s = LocalServer()
    yield s
    s.close()


@pytest.fixture
def ft() -> FakeTime:
    return FakeTime()


def make_client(ft: FakeTime, session: Optional[requests.Session] = None, **overrides: Any) -> PoliteClient:
    if session is None:
        session = requests.Session()
        session.trust_env = False  # ignore any system proxy for 127.0.0.1
    opts: dict[str, Any] = dict(
        user_agent=UA, timeout_sec=5, default_rate_per_sec=1000.0, max_retries=3,
        session=session, clock=ft.clock, sleep=ft.sleep, wallclock=ft.wall,
        rng=random.Random(7),
    )
    opts.update(overrides)
    return PoliteClient(**opts)


def dead_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeAdapter(requests.adapters.BaseAdapter):
    """Canned responses for https:// hosts (Workday, Greenhouse) with no sockets."""

    def __init__(self, routes: dict[tuple[str, str], Any],
                 fallback: Optional[Callable[[requests.PreparedRequest], Reply]] = None) -> None:
        super().__init__()
        self.routes = routes
        self.fallback = fallback
        self.sent: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):  # noqa: D401 — requests adapter API
        self.sent.append(request)
        reply = self.routes.get((request.method, request.url.split("?")[0]))
        if reply is None:
            reply = self.fallback(request) if self.fallback else (404, {}, b"")
        status, headers, body = reply(request) if callable(reply) else reply
        resp = requests.Response()
        resp.status_code, resp.reason = status, "fake"
        resp.headers = CaseInsensitiveDict(headers)
        resp._content, resp._content_consumed = body, True
        resp.url, resp.request, resp.encoding = request.url, request, "utf-8"
        return resp

    def close(self) -> None:
        pass


def fake_session(routes: dict[tuple[str, str], Any]) -> tuple[requests.Session, FakeAdapter]:
    adapter = FakeAdapter(routes)
    sess = requests.Session()
    sess.trust_env = False
    sess.mount("https://", adapter)
    return sess, adapter


# --------------------------------------------------------------- rate limit

def test_token_bucket_spacing_with_fake_clock(ft):
    bucket = TokenBucket(2.0, clock=ft.clock)
    for _ in range(3):
        wait = bucket.reserve()
        if wait:
            ft.sleep(wait)
    assert ft.sleeps == [0.5, 0.5]


def test_token_bucket_threads_queue_at_exact_spacing(ft):
    bucket = TokenBucket(2.0, clock=ft.clock)  # clock frozen: all arrive at once
    waits: list[float] = []
    threads = [threading.Thread(target=lambda: waits.append(bucket.reserve())) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(waits) == [0.0, 0.5, 1.0, 1.5, 2.0]


def test_client_spaces_requests_per_host(ft, server, server2):
    for s in (server, server2):
        s.route("/jobs", (200, JSON, b"[]"))
    client = make_client(ft, default_rate_per_sec=2.0, robots_exempt_hosts=EXEMPT)
    for _ in range(3):
        client.get(server.url("/jobs"))
    assert ft.sleeps == [0.5, 0.5]
    client.get(server2.url("/jobs"))  # same host on another port: same bucket
    assert ft.sleeps == [0.5, 0.5, 0.5]

    ft2 = FakeTime()
    sess, _ = _catch_all_session({})
    other = make_client(ft2, session=sess, default_rate_per_sec=2.0,
                        robots_exempt_hosts={"a.test": "test", "b.test": "test"})
    for url in ("https://a.test/x", "https://a.test/y", "https://b.test/x"):
        other.get(url)
    assert ft2.big_sleeps() == [0.5]  # b.test has its own bucket


def test_host_rates_override_default(ft, server):
    server.route("/jobs", (200, JSON, b"[]"))
    client = make_client(ft, default_rate_per_sec=10.0, host_rates={"127.0.0.1": 0.25},
                         robots_exempt_hosts=EXEMPT)
    client.get(server.url("/jobs"))
    client.get(server.url("/jobs"))
    assert ft.sleeps == [4.0]


def test_host_rates_suffix_key_matches_subdomains(ft):
    client = make_client(ft, host_rates={".myworkdayjobs.com": 0.5, "api.lever.co": 1.0})
    assert client._rate_for("paypal.wd1.myworkdayjobs.com") == 0.5
    assert client._rate_for("api.lever.co") == 1.0
    assert client._rate_for("boards-api.greenhouse.io") == 1000.0


def test_platform_tenants_share_one_bucket_robots_included(ft):
    """S1/L7: a.recruitee.com and b.recruitee.com are different hosts but one platform:
    robots.txt fetches and API calls of every tenant go through ONE 1 req/s bucket."""
    def reply(request):
        if request.url.endswith("/robots.txt"):
            return 404, {}, b""
        return 200, JSON, b"[]"
    sess, adapter = fake_session({})
    adapter.fallback = reply
    client = make_client(ft, session=sess, default_rate_per_sec=1000.0)
    assert client.platform_for("a.recruitee.com") == client.platform_for("b.recruitee.com") == "recruitee.com"
    assert client.platform_for("x.wd5.myworkdayjobs.com") == "myworkdayjobs.com"
    assert client.platform_for("acme.jobs.personio.de") == "personio.de"
    assert client.platform_for("evilrecruitee.com") is None and client.platform_for("example.org") is None
    for host in ("a", "b", "c", "d"):
        client.get(f"https://{host}.recruitee.com/api/offers/")
    # 4 robots.txt + 4 list calls = 8 requests through one bucket: 7 waits of 1 s
    assert len(adapter.sent) == 8 and len(ft.big_sleeps()) == 7
    assert ft.now - 1000.0 >= 7.0 - 1e-6


def test_platform_rate_is_configurable_and_other_hosts_are_unaffected(ft):
    sess, adapter = fake_session({})
    adapter.fallback = lambda r: (404, {}, b"") if r.url.endswith("/robots.txt") else (200, JSON, b"[]")
    client = make_client(ft, session=sess, platform_rates={"recruitee.com": 0.5})
    client.get("https://a.recruitee.com/x")
    client.get("https://b.recruitee.com/x")
    assert 2.0 in ft.big_sleeps()
    ft.sleeps.clear()
    client.get("https://one.example.org/x")
    client.get("https://two.example.org/x")
    assert ft.big_sleeps() == []        # unrelated hosts keep their own (fast) buckets


def test_one_tenants_crawl_delay_does_not_slow_the_platform(ft):
    def reply(request):
        if request.url == "https://slow.teamtailor.com/robots.txt":
            return 200, {}, b"User-agent: *\nCrawl-delay: 30\n"
        if request.url.endswith("/robots.txt"):
            return 404, {}, b""
        return 200, JSON, b"[]"
    sess, adapter = fake_session({})
    adapter.fallback = reply
    client = make_client(ft, session=sess, platform_rates={"teamtailor.com": 1000.0})
    client.get("https://slow.teamtailor.com/jobs.rss")
    client.get("https://fast.teamtailor.com/jobs.rss")
    client.get("https://fast.teamtailor.com/jobs.rss")
    assert ft.big_sleeps() == [30.0]    # the slow tenant's own bucket only; fast tenant never waits


# ------------------------------------------------------------------- robots

def test_robots_disallow_raises_and_makes_no_request(ft, server):
    server.route("/robots.txt", (200, {}, b"User-agent: *\nDisallow: /private/\n"))
    server.route("/private/jobs", (200, JSON, b"{}"))
    server.route("/public", (200, JSON, b"{}"))
    client = make_client(ft)
    with pytest.raises(RobotsDisallowed):
        client.get(server.url("/private/jobs"))
    assert server.paths() == ["/robots.txt"]
    client.get(server.url("/public"))
    assert server.paths() == ["/robots.txt", "/public"]  # robots.txt cached


def test_robots_longest_match_and_wildcards(ft, server):
    # The stdlib's can_fetch would allow /private/x (first match: "Allow: /") and
    # ignore the wildcards; RFC 9309 says the longest rule wins.
    server.route("/robots.txt", (200, {}, (
        b"User-agent: *\nAllow: /\nDisallow: /private/\n"
        b"Disallow: /*?feed=\nDisallow: /*.pdf$\n"
    )))
    for p in ("/private/x", "/jobs", "/a.pdf", "/feed/"):
        server.route(p, (200, {}, b"ok"))
    client = make_client(ft)
    for blocked in ("/private/x", "/jobs?feed=rss", "/a.pdf"):
        with pytest.raises(RobotsDisallowed):
            client.get(server.url(blocked))
    for allowed in ("/jobs", "/a.pdf?page=2", "/feed/"):
        assert client.get(server.url(allowed)).status_code == 200


def test_robots_group_for_our_user_agent_wins(ft, server):
    server.route("/robots.txt", (200, {}, b"User-agent: job-copilot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"))
    server.route("/jobs", (200, {}, b"ok"))
    with pytest.raises(RobotsDisallowed):
        make_client(ft).get(server.url("/jobs"))
    assert server.paths() == ["/robots.txt"]


def test_robots_404_allows_everything(ft, server):
    server.route("/jobs", (200, JSON, b"[]"))
    resp = make_client(ft).get(server.url("/jobs"))
    assert resp.status_code == 200
    assert server.paths() == ["/robots.txt", "/jobs"]


@pytest.mark.parametrize("status,fetches", [(401, 1), (403, 1), (429, 3), (500, 3), (503, 3)])
def test_robots_error_disallows_host(ft, server, status, fetches, caplog):
    # 401/403 are answers; 429/5xx are retried twice. Either way the result is cached
    # (briefly for 429/5xx, see test_http_robots.py) and /jobs is never fetched.
    server.route("/robots.txt", (status, {}, b"nope"))
    server.route("/jobs", (200, JSON, b"[]"))
    client = make_client(ft)
    with caplog.at_level("WARNING", logger="jobcopilot.http"):
        with pytest.raises(RobotsUnavailable):
            client.get(server.url("/jobs"))
    assert any("robots.txt" in r.message for r in caplog.records)
    with pytest.raises(RobotsUnavailable):
        client.get(server.url("/jobs"))
    assert server.paths() == ["/robots.txt"] * fetches


def test_robots_network_error_disallows(ft):
    url = f"http://127.0.0.1:{dead_port()}/jobs"
    with pytest.raises(RobotsUnavailable):
        make_client(ft).get(url)


def test_exempt_host_skips_robots_but_is_rate_limited(ft, server):
    server.route("/robots.txt", (200, {}, b"User-agent: *\nDisallow: /\n"))
    server.route("/api", (200, JSON, b"{}"))
    client = make_client(ft, default_rate_per_sec=1.0, robots_exempt_hosts=EXEMPT)
    client.get(server.url("/api"))
    client.get(server.url("/api"))
    assert server.paths() == ["/api", "/api"]
    assert ft.sleeps == [1.0]


def test_exemption_needs_a_reason(ft):
    with pytest.raises(ValueError):
        make_client(ft, robots_exempt_hosts={"api.adzuna.com": ""})
    with pytest.raises(ValueError):
        make_client(ft, robots_exempt_hosts=["api.adzuna.com"])


def test_crawl_delay_slows_the_host(ft, server):
    server.route("/robots.txt", (200, {}, b"User-agent: *\nCrawl-delay: 3\n"))
    server.route("/a", (200, {}, b"ok"))
    client = make_client(ft, default_rate_per_sec=10.0)
    client.get(server.url("/a"))
    client.get(server.url("/a"))
    assert ft.big_sleeps() == [3.0, 3.0]


def test_redirect_hop_gets_its_own_robots_check(ft, server, server2):
    server2.route("/robots.txt", (200, {}, b"User-agent: *\nDisallow: /new\n"))
    server2.route("/new", (200, {}, b"secret"))
    server.route("/old", (301, {"Location": server2.url("/new")}, b""))
    server.route("/a", (302, {"Location": "/b"}, b""))
    server.route("/b", (200, {}, b"landed"))
    client = make_client(ft)
    with pytest.raises(RobotsDisallowed):
        client.get(server.url("/old"))
    assert server2.paths() == ["/robots.txt"]
    assert client.get(server.url("/a")).text == "landed"


# ------------------------------------------------------------------ retries

def test_429_retry_after_seconds_then_success(ft, server):
    server.route("/jobs", (429, {"Retry-After": "2"}, b""), (200, JSON, b'{"ok": true}'))
    client = make_client(ft, robots_exempt_hosts=EXEMPT)
    assert client.get_json(server.url("/jobs")) == {"ok": True}
    assert server.paths() == ["/jobs", "/jobs"]
    assert ft.big_sleeps() == [2.0]


def test_429_retry_after_http_date(ft, server):
    when = formatdate(ft.wall() + 5, usegmt=True)
    server.route("/jobs", (429, {"Retry-After": when}, b""), (200, JSON, b"[]"))
    make_client(ft, robots_exempt_hosts=EXEMPT).get(server.url("/jobs"))
    assert ft.big_sleeps() == [pytest.approx(5.0, abs=0.05)]


def test_retry_after_beyond_max_wait_gives_up_and_fails_fast(ft, server):
    server.route("/slow", (429, {"Retry-After": "3600"}, b""))
    server.route("/other", (200, {}, b"ok"))
    client = make_client(ft, max_wait_sec=10, robots_exempt_hosts=EXEMPT)
    with pytest.raises(RetryLater):
        client.get(server.url("/slow"))
    with pytest.raises(RetryLater):
        client.get(server.url("/other"))  # same host: no request until the wait passes
    assert server.paths() == ["/slow"]
    assert not ft.big_sleeps()


def test_404_not_retried(ft, server):
    client = make_client(ft, robots_exempt_hosts=EXEMPT)
    with pytest.raises(HttpStatusError) as exc:
        client.get(server.url("/missing"))
    assert exc.value.status_code == 404
    assert isinstance(exc.value, requests.HTTPError)  # v1 callers' except clauses still work
    assert server.paths() == ["/missing"]
    assert not ft.big_sleeps()


def test_5xx_backoff_with_jitter_then_gives_up(ft, server):
    server.route("/flaky", (503, {}, b""))
    client = make_client(ft, max_retries=2, robots_exempt_hosts=EXEMPT)
    with pytest.raises(HttpStatusError) as exc:
        client.get(server.url("/flaky"))
    assert exc.value.status_code == 503
    assert server.paths() == ["/flaky"] * 3
    first, second = ft.big_sleeps()
    assert 0.5 <= first <= 1.0 and 1.0 <= second <= 2.0  # base 1s, equal jitter

    ft2 = FakeTime()  # same seed -> same jitter
    server.hits.clear()
    with pytest.raises(HttpStatusError):
        make_client(ft2, max_retries=2, robots_exempt_hosts=EXEMPT).get(server.url("/flaky"))
    assert ft2.big_sleeps() == [first, second]


def test_post_retries_only_when_safe(ft, server):
    client = make_client(ft, robots_exempt_hosts=EXEMPT)
    server.route("/search", (500, {}, b""), (200, JSON, b"{}"))
    with pytest.raises(HttpStatusError):
        client.post(server.url("/search"), json={"q": 1})
    assert server.paths() == ["/search"]

    server.hits.clear()
    server.route("/search", (500, {}, b""), (200, JSON, b'{"n": 1}'))
    assert client.post_json(server.url("/search"), json={"q": 1}, idempotent=True) == {"n": 1}
    assert server.paths() == ["/search"] * 2

    server.hits.clear()
    server.route("/search", (429, {"Retry-After": "1"}, b""), (200, JSON, b'{"n": 2}'))
    assert client.post_json(server.url("/search"), json={"q": 1}) == {"n": 2}  # 429 = not processed


def test_connection_error_retried_then_typed_error_without_secrets(ft):
    url = f"http://127.0.0.1:{dead_port()}/x"
    client = make_client(ft, max_retries=2, robots_exempt_hosts=EXEMPT)
    with pytest.raises(HttpConnectionError) as exc:
        client.get(url, params={"app_key": "SECRET123", "q": "python"})
    assert isinstance(exc.value, requests.ConnectionError)
    assert "SECRET123" not in str(exc.value)
    assert len(ft.big_sleeps()) == 2


# ------------------------------------------------------- honesty and cache

def test_honest_user_agent_always_sent(ft, server):
    server.route("/jobs", (200, JSON, b"[]"))
    client = make_client(ft)
    client.get(server.url("/jobs"), headers={"user-agent": "Mozilla/5.0 Chrome/124.0"})
    assert [h["headers"]["user-agent"] for h in server.hits] == [UA, UA]  # robots.txt + request
    assert "User-Agent" not in DEFAULT_HEADERS


def _etag_route(body: bytes) -> Callable[[dict], Reply]:
    def reply(hit: dict) -> Reply:
        if hit["headers"].get("if-none-match") == '"v1"':
            return 304, {"ETag": '"v1"'}, b""
        return 200, {"ETag": '"v1"', **JSON}, body
    return reply


def test_304_served_from_cache(ft, server):
    server.route("/feed", _etag_route(b'{"jobs": [1, 2]}'))
    client = make_client(ft, robots_exempt_hosts=EXEMPT)
    first = client.get(server.url("/feed"))
    second = client.get(server.url("/feed"))
    assert first.from_cache is False and second.from_cache is True
    assert second.status_code == 200 and second.json() == {"jobs": [1, 2]}
    assert "if-none-match" not in server.hits[0]["headers"]
    assert server.hits[1]["headers"]["if-none-match"] == '"v1"'
    second.close()  # a cache-built response must be safe to close


def test_last_modified_revalidation(ft, server):
    stamp = "Wed, 11 Nov 2026 10:00:00 GMT"

    def reply(hit: dict) -> Reply:
        if hit["headers"].get("if-modified-since") == stamp:
            return 304, {}, b""
        return 200, {"Last-Modified": stamp, **JSON}, b'{"v": 1}'

    server.route("/lm", reply)
    client = make_client(ft, robots_exempt_hosts=EXEMPT)
    client.get(server.url("/lm"))
    assert client.get_json(server.url("/lm")) == {"v": 1}
    assert server.hits[1]["headers"]["if-modified-since"] == stamp


def test_disk_cache_survives_a_new_client_and_holds_no_secrets(ft, server, tmp_path):
    server.route("/feed", _etag_route(b'{"jobs": [3]}'))
    cache_dir = tmp_path / "http-cache"
    params = {"app_key": "SECRET123", "q": "python"}
    make_client(ft, cache_dir=cache_dir, robots_exempt_hosts=EXEMPT).get(server.url("/feed"), params=params)
    fresh = make_client(FakeTime(), cache_dir=cache_dir, robots_exempt_hosts=EXEMPT)
    resp = fresh.get(server.url("/feed"), params=params)
    assert resp.from_cache is True and resp.json() == {"jobs": [3]}
    files = list(cache_dir.iterdir())
    assert files and all(b"SECRET123" not in f.read_bytes() for f in files)


def test_record_mode_writes_body_and_metadata(ft, server, tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    monkeypatch.setenv(jhttp.RECORD_ENV, str(rec))
    body = b'{"jobs": [{"id": 1}]}'
    server.route("/v1/jobs", (200, {**JSON, "ETag": '"e1"'}, body))
    make_client(ft).get(server.url("/v1/jobs"), params={"app_key": "SECRET123", "q": "python"})
    files = sorted(p for p in rec.rglob("*") if p.is_file())
    assert len(files) == 2  # robots.txt is not recorded
    data_file = next(p for p in files if p.name.endswith(".json") and not p.name.endswith(".meta.json"))
    meta_file = next(p for p in files if p.name.endswith(".meta.json"))
    assert data_file.read_bytes() == body
    meta = json.loads(meta_file.read_text())
    assert meta["status"] == 200 and meta["etag"] == '"e1"' and meta["method"] == "GET"
    assert meta["body_file"] == data_file.name
    assert "q=python" in meta["url"] and "SECRET123" not in meta_file.read_text()


# ------------------------------------------------------------------ helpers

def test_redact_url():
    out = redact_url("https://u:pw@api.example.com/v1?app_id=ID&app_key=K&what=python&api_token=T")
    assert "pw" not in out and "=ID" not in out and "=K" not in out and "=T" not in out
    assert "what=python" in out and "api.example.com" in out


def test_parse_retry_after():
    assert parse_retry_after("5", now=0) == 5.0
    assert parse_retry_after(formatdate(1000 + 30, usegmt=True), now=1000) == 30.0
    assert parse_retry_after(formatdate(10, usegmt=True), now=1000) == 0.0
    assert parse_retry_after("soon", now=0) is None
    assert parse_retry_after(None, now=0) is None


def test_default_client_is_shared_and_reads_config():
    a = jhttp.default_client()
    assert a is jhttp.default_client()
    assert a.user_agent == UA and a.max_retries == 1
    jhttp.reset_default_client()
    assert jhttp.default_client() is not a


# --------------------------------------------------------------- connectors

def test_parse_only_connector_builds_no_client():
    with open(FIX / "greenhouse.json", encoding="utf-8") as fh:
        raw = json.load(fh)
    conn = GreenhouseConnector()
    assert len(conn.parse(raw, {"name": "Uber", "sector": "tech"})) == 3
    assert conn._client is None and jhttp._default is None


def test_greenhouse_fetch_goes_through_client(ft, server):
    server.route("/v1/boards/uber/jobs", (200, JSON, (FIX / "greenhouse.json").read_bytes()))

    class LocalGreenhouse(GreenhouseConnector):
        BASE = server.url("/v1/boards/{slug}/jobs")

    jobs = LocalGreenhouse(client=make_client(ft)).fetch({"name": "Uber", "sector": "tech", "slug": "uber"})
    assert len(jobs) == 3
    assert server.paths() == ["/robots.txt", "/v1/boards/uber/jobs"]
    hit = server.hits[1]
    assert hit["query"] == {"content": ["true"]}
    assert hit["headers"]["user-agent"] == UA and hit["headers"]["accept"] == "application/json"


WD_HOST = "https://acme.wd1.myworkdayjobs.com"
WD_ROBOTS = b"Sitemap: https://wd1.myworkdaysite.com/x.xml\n\nUser-agent: *\nAllow: /jobs/\nDisallow: /refreshFacet/\n"


def test_workday_paginates_and_fetches_detail_politely(ft):
    def listing(req) -> Reply:
        offset = json.loads(req.body)["offset"]
        posts = [{"title": f"SWE {offset + i}", "externalPath": f"/job/x/{offset + i}",
                  "bulletFields": [f"R-{offset + i}"], "locationsText": "Bengaluru, India"}
                 for i in range(2 if offset == 0 else 1)]
        return 200, JSON, json.dumps({"total": 25, "jobPostings": posts}).encode()

    detail = {"jobPostingInfo": {"jobDescription": "<p>Build <b>things</b></p>"}}
    sess, adapter = fake_session({
        ("GET", f"{WD_HOST}/robots.txt"): (200, {}, WD_ROBOTS),
        ("POST", f"{WD_HOST}/wday/cxs/acme/External/jobs"): listing,
        ("GET", f"{WD_HOST}/wday/cxs/acme/External/job/x/0"): (200, JSON, json.dumps(detail).encode()),
    })
    company = {"name": "Acme", "sector": "tech", "tenant": "acme", "wd_server": "wd1", "site": "External"}
    conn = WorkdayConnector(client=make_client(ft, session=sess))
    jobs = conn.fetch(company)
    assert [j.external_id for j in jobs] == ["R-0", "R-1", "R-20"]
    assert "Build" in conn.fetch_detail(jobs[0], company)

    methods = [(r.method, urlsplit(r.url).path) for r in adapter.sent]
    assert methods[0] == ("GET", "/robots.txt") and methods.count(("GET", "/robots.txt")) == 1
    assert [json.loads(r.body)["offset"] for r in adapter.sent if r.method == "POST"] == [0, 20]
    assert all(r.headers["User-Agent"] == UA for r in adapter.sent)


def test_legacy_session_kwarg_and_positional_still_polite():
    gh_jobs = (FIX / "greenhouse.json").read_bytes()
    url = "https://boards-api.greenhouse.io/v1/boards/uber/jobs"
    company = {"name": "Uber", "sector": "tech", "slug": "uber"}
    for build in (lambda s: GreenhouseConnector(session=s), lambda s: GreenhouseConnector(s)):
        sess, adapter = fake_session({
            ("GET", "https://boards-api.greenhouse.io/robots.txt"): (200, {}, b"User-agent: *\nDisallow: /embed/\n"),
            ("GET", url): (200, JSON, gh_jobs),
        })
        assert len(build(sess).fetch(company)) == 3
        assert [urlsplit(r.url).path for r in adapter.sent] == ["/robots.txt", "/v1/boards/uber/jobs"]
        assert all(r.headers["User-Agent"] == UA for r in adapter.sent)  # UA from (test) config


def test_connector_surfaces_robots_disallow():
    sess, adapter = fake_session({
        ("GET", "https://boards-api.greenhouse.io/robots.txt"): (200, {}, b"User-agent: *\nDisallow: /v1/\n"),
    })
    with pytest.raises(RobotsDisallowed):
        GreenhouseConnector(session=sess).fetch({"name": "Uber", "slug": "uber"})
    assert len(adapter.sent) == 1


# ------------------------------------------------- review-fix regressions

def _catch_all_session(routes: dict[str, Reply]) -> tuple[requests.Session, list[requests.PreparedRequest]]:
    """http(s):// fake keyed by URL without query (any method); robots.txt is 404
    and any other URL answers 200 unless routed."""
    def fallback(request) -> Reply:
        key = request.url.split("?")[0]
        if key in routes:
            r = routes[key]
            return r(request) if callable(r) else r
        return (404, {}, b"") if key.endswith("/robots.txt") else (200, {}, b"ok")

    adapter = FakeAdapter({}, fallback=fallback)
    sess = requests.Session()
    sess.trust_env = False
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess, adapter.sent


SECRET_HEADERS = {"X-RapidAPI-Key": "SECRET-KEY-123", "Authorization": "Bearer TOK",
                  "Cookie": "sid=1", "Accept": "application/json"}


def test_cross_origin_redirect_drops_credential_headers(ft):
    # finding #5: caller headers (API keys) were replayed to whatever host Location named.
    sess, sent = _catch_all_session({
        "https://api.test/start": (302, {"Location": "https://attacker.test/collect"}, b""),
        "https://api.test/down": (302, {"Location": "http://api.test/plain"}, b""),
        "https://api.test/bounce": (302, {"Location": "https://attacker.test/back"}, b""),
        "https://attacker.test/back": (302, {"Location": "https://api.test/home"}, b""),
    })
    client = make_client(ft, session=sess)
    client.get("https://api.test/start", headers=SECRET_HEADERS)
    client.get("https://api.test/down", headers=SECRET_HEADERS)  # https -> http, same host
    client.get("https://api.test/bounce", headers=SECRET_HEADERS)  # leaves and comes back
    by_url = {r.url: r.headers for r in sent}
    for url in ("https://attacker.test/collect", "http://api.test/plain",
                "https://attacker.test/back", "https://api.test/home"):
        h = by_url[url]
        assert "X-RapidAPI-Key" not in h and "Authorization" not in h and "Cookie" not in h, url
        assert h["Accept"] == "application/json" and h["User-Agent"] == UA
    assert by_url["https://api.test/start"]["X-RapidAPI-Key"] == "SECRET-KEY-123"


def test_same_origin_and_https_upgrade_redirects_keep_headers(ft):
    sess, sent = _catch_all_session({
        "https://api.test/a": (301, {"Location": "/b"}, b""),
        "http://api.test/old": (301, {"Location": "https://api.test/new"}, b""),
    })
    client = make_client(ft, session=sess)
    client.get("https://api.test/a", headers=SECRET_HEADERS)
    client.get("http://api.test/old", headers=SECRET_HEADERS)
    by_url = {r.url: r.headers for r in sent}
    assert by_url["https://api.test/b"]["X-RapidAPI-Key"] == "SECRET-KEY-123"
    assert by_url["https://api.test/new"]["Authorization"] == "Bearer TOK"


def test_rate_limit_is_per_host_not_per_origin(ft):
    # finding #7: scheme, explicit default port and a trailing dot each got a fresh bucket.
    sess, sent = _catch_all_session({})
    client = make_client(ft, session=sess, default_rate_per_sec=1.0,
                         robots_exempt_hosts={"api.test": "test"})
    for url in ("https://api.test/x", "https://api.test:443/x", "https://API.test./x",
                "http://api.test/x"):
        client.get(url)
    assert ft.sleeps == [1.0, 1.0, 1.0]


def test_robots_cached_per_normalised_origin(ft):
    sess, sent = _catch_all_session({})
    client = make_client(ft, session=sess)
    for url in ("https://api.test/x", "https://api.test:443/y", "https://API.test./z",
                "http://api.test/x", "http://api.test:80/y"):
        client.get(url)
    robots = [r.url for r in sent if r.url.endswith("/robots.txt")]
    assert robots == ["https://api.test/robots.txt", "http://api.test/robots.txt"]


@pytest.mark.parametrize("value", ["inf", "Infinity", "-inf", "1e400", "nan", "NaN", "-5", "5.5",
                                   "0x10", "1_000", " 12 34"])
def test_parse_retry_after_rejects_non_delta_seconds(value):
    # finding #26: float() spellings put the host into infinite debt.
    assert parse_retry_after(value, now=0) is None


def test_parse_retry_after_is_clamped_to_a_finite_maximum():
    assert parse_retry_after("9" * 40, now=0) == jhttp.RETRY_AFTER_MAX_SEC
    assert parse_retry_after(formatdate(10 ** 11, usegmt=True), now=0) == jhttp.RETRY_AFTER_MAX_SEC
    assert parse_retry_after(" 7 ", now=0) == 7.0


def test_retry_after_inf_does_not_wedge_the_host(ft, server):
    server.route("/jobs", (429, {"Retry-After": "inf"}, b""), (200, JSON, b"[]"))
    client = make_client(ft, robots_exempt_hosts=EXEMPT)
    assert client.get(server.url("/jobs")).status_code == 200  # backoff, not RetryLater(inf)
    assert len(ft.big_sleeps()) == 1 and ft.big_sleeps()[0] <= 1.0
    bucket = TokenBucket(1.0, clock=ft.clock)
    bucket.penalize(float("inf"))  # defence in depth: never infinite debt
    assert bucket.reserve() <= jhttp.RETRY_AFTER_MAX_SEC + 1


def test_record_mode_two_clients_same_second_never_overwrite(ft, tmp_path, monkeypatch):
    # finding #31: the per-instance counter restarted at 1, so files collided.
    rec = tmp_path / "rec"
    monkeypatch.setenv(jhttp.RECORD_ENV, str(rec))
    sess_a, _ = _catch_all_session({"https://g.test/v1/boards/acme/jobs": (200, JSON, b'{"from":"A"}')})
    sess_b, _ = _catch_all_session({"https://g.test/v1/boards/acme/jobs": (200, JSON, b'{"from":"B"}')})
    make_client(ft, session=sess_a).get("https://g.test/v1/boards/acme/jobs")
    make_client(ft, session=sess_b).get("https://g.test/v1/boards/acme/jobs")
    bodies = sorted(p.read_bytes() for p in rec.rglob("*.json") if not p.name.endswith(".meta.json"))
    assert bodies == [b'{"from":"A"}', b'{"from":"B"}']
    assert len(list(rec.rglob("*.meta.json"))) == 2


def test_record_mode_host_dir_has_no_userinfo(ft, tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    monkeypatch.setenv(jhttp.RECORD_ENV, str(rec))
    sess, _ = _catch_all_session({})
    make_client(ft, session=sess).get("https://alice:s3cret@u.test:8443/x")
    dirs = [p.name for p in rec.iterdir()]
    assert dirs == ["u.test_8443"]
    assert not any("s3cret" in str(p) or "alice" in str(p) for p in rec.rglob("*"))
    assert all("s3cret" not in p.read_text() for p in rec.rglob("*.meta.json"))


# ---------------------------------------------------- ingest reporting (#8)

def _fake_connector(outcome: Any) -> type:
    class Fake:
        source = "fake"

        def fetch(self, company):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    return Fake


def test_ingest_reports_robots_unavailable_apart_from_disallow(monkeypatch, caplog, capsys):
    from jobcopilot import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "REGISTRY", {
        "down": _fake_connector(RobotsUnavailable("https://x.test/jobs",
                                                  "robots.txt unreachable (ConnectionError)",
                                                  transient=True)),
        "wall": _fake_connector(RobotsUnavailable("https://w.test/jobs", "robots.txt answered HTTP 403")),
        "says_no": _fake_connector(RobotsDisallowed("https://y.test/jobs")),
        "denied": _fake_connector(jhttp.HostDenied("https://www.linkedin.com/jobs",
                                                   "do-not-automate host (linkedin.com) refused")),
        "ok": _fake_connector([]),
    })
    companies = [{"name": n, "connector": n} for n in ("down", "wall", "says_no", "denied", "ok")]
    with caplog.at_level("INFO", logger="jobcopilot.ingest"):
        assert ingest_mod.ingest(companies) == []  # no exception: the run carries on

    def record(name: str) -> logging.LogRecord:
        return next(r for r in caplog.records if f"company={name} " in r.getMessage())

    assert record("down").levelno == logging.ERROR
    assert "robots_unavailable" in record("down").getMessage()
    assert "transient=True" in record("down").getMessage()
    assert record("wall").levelno == logging.ERROR
    assert record("says_no").levelno == logging.INFO
    assert "reason=robots_disallowed" in record("says_no").getMessage()
    assert record("denied").levelno == logging.WARNING
    assert "reason=denied_host" in record("denied").getMessage()
    assert "ingest company=ok" in caplog.text
    err = capsys.readouterr().err
    assert "robots.txt unavailable" in err and "will retry" in err
    assert "disallowed by robots.txt" in err
    assert "do-not-automate host" in err


# ------------------------------------------- size and time caps (safety-2)

class RawServer:
    """One-shot 127.0.0.1 server whose handler writes raw bytes to the socket, for
    bodies LocalServer can't produce (no Content-Length, trickled, gzip bombs)."""

    def __init__(self, handler: Callable[[socket.socket], None]) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.stop = threading.Event()
        self.requests = 0

        def run() -> None:
            self.sock.settimeout(0.05)
            while not self.stop.is_set():
                try:
                    conn, _ = self.sock.accept()
                except OSError:
                    continue
                with conn:
                    conn.settimeout(5)
                    buf = b""
                    while b"\r\n\r\n" not in buf:
                        got = conn.recv(4096)
                        if not got:
                            break
                        buf += got
                    self.requests += 1
                    try:
                        handler(conn)
                    except OSError:
                        pass  # client hung up, which is the point of most tests

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def url(self, path: str = "/x") -> str:
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}{path}"

    def close(self) -> None:
        self.stop.set()
        self.thread.join(2)
        self.sock.close()


def real_client(**overrides: Any) -> PoliteClient:
    session = requests.Session()
    session.trust_env = False
    opts: dict[str, Any] = dict(user_agent=UA, timeout_sec=5, default_rate_per_sec=1000.0,
                                max_retries=0, session=session, robots_exempt_hosts=EXEMPT)
    opts.update(overrides)
    return PoliteClient(**opts)


def test_default_caps_are_set_and_leave_normal_bodies_alone(ft, server):
    client = make_client(ft, robots_exempt_hosts=EXEMPT)
    assert client.max_response_bytes == jhttp.DEFAULT_MAX_RESPONSE_BYTES == 64 * 1024 * 1024
    assert client.request_deadline_sec == jhttp.DEFAULT_REQUEST_DEADLINE_SEC
    body = b"x" * 5000
    server.route("/big", (200, {}, body))
    assert client.get(server.url("/big"), max_bytes=5000).content == body  # == limit is fine


def test_oversized_content_length_rejected_before_reading(ft, server):
    server.route("/huge", (200, {}, b"x" * 100_000))
    client = make_client(ft, robots_exempt_hosts=EXEMPT)
    with pytest.raises(jhttp.ResponseTooLarge) as err:
        client.get(server.url("/huge"), max_bytes=1000)
    assert err.value.size == 100_000 and err.value.limit == 1000
    assert server.paths() == ["/huge"]  # not retried


def test_body_without_length_is_cut_off_at_max_bytes():
    def endless(conn: socket.socket) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n")
        for _ in range(2000):            # 128 MB if nobody stops reading
            conn.sendall(b"a" * 65536)

    srv = RawServer(endless)
    try:
        with pytest.raises(jhttp.ResponseTooLarge):
            real_client().get(srv.url(), max_bytes=200_000)
    finally:
        srv.close()


def test_gzip_bomb_is_capped_on_decoded_size():
    import gzip
    bomb = gzip.compress(b"\0" * (40 * 1024 * 1024))   # ~40 KB on the wire, 40 MB decoded

    def send_bomb(conn: socket.socket) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n"
                     b"Content-Length: " + str(len(bomb)).encode() + b"\r\nConnection: close\r\n\r\n")
        conn.sendall(bomb)

    srv = RawServer(send_bomb)
    try:
        with pytest.raises(jhttp.ResponseTooLarge):
            real_client().get(srv.url(), max_bytes=1_000_000)
    finally:
        srv.close()


def test_deadline_stops_a_slow_trickle_body():
    import time as _time

    def trickle(conn: socket.socket) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n")
        for _ in range(1000):            # 1 byte / 20 ms: each read is well inside timeout_sec
            conn.sendall(b"a")
            _time.sleep(0.02)

    srv = RawServer(trickle)
    try:
        t0 = _time.monotonic()
        with pytest.raises(jhttp.RequestDeadlineExceeded):
            real_client().get(srv.url(), deadline_sec=0.4)
        assert _time.monotonic() - t0 < 2.0
    finally:
        srv.close()


def test_deadline_covers_retry_waits(ft, server):
    server.route("/busy", (429, {"Retry-After": "30"}, b""), (200, JSON, b"{}"))
    client = make_client(ft, robots_exempt_hosts=EXEMPT)
    with pytest.raises(jhttp.RequestDeadlineExceeded):
        client.get(server.url("/busy"), deadline_sec=10)
    assert server.paths() == ["/busy"]
    assert client.get(server.url("/busy"), deadline_sec=100).status_code == 200


# ------------------------------------- path-scoped robots exemption (safety-4)

def test_exemption_with_path_prefix_covers_only_that_path(ft, server):
    server.route("/robots.txt", (200, {}, b"User-agent: *\nDisallow: /\n"))
    for p in ("/posting-api/job-board/acme", "/admin", "/posting-api/job-boardX"):
        server.route(p, (200, JSON, b"{}"))
    client = make_client(ft, robots_exempt_hosts={
        "127.0.0.1/posting-api/job-board/": "public job board API only"})
    assert client.get(server.url("/posting-api/job-board/acme")).status_code == 200
    for outside in ("/admin", "/posting-api/job-boardX",
                    "/posting-api/job-board/..%2F..%2Fadmin",
                    "/posting-api/job-board/%2e%2e/%2e%2e/admin"):
        with pytest.raises(RobotsDisallowed):
            client.get(server.url(outside))
    assert "/admin" not in server.paths()


def test_host_only_exemption_still_covers_every_path(ft, server):
    server.route("/robots.txt", (200, {}, b"User-agent: *\nDisallow: /\n"))
    server.route("/anything/here", (200, JSON, b"{}"))
    client = make_client(ft, robots_exempt_hosts={"127.0.0.1": "local test server"})
    assert client.get(server.url("/anything/here")).status_code == 200
    assert server.paths() == ["/anything/here"]


@pytest.mark.parametrize("key", ["api.x.com/a/../b", "api.x.com/a?b", "api.x.com/%2e%2e/",
                                 "/just/a/path", "api.x.com/a/./b"])
def test_exemption_key_must_be_host_and_clean_path(ft, key):
    with pytest.raises(ValueError):
        make_client(ft, robots_exempt_hosts={key: "reason"})
