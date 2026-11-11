"""Regression tests for robots.txt handling (RFC 9309) and the do-not-automate
denylist in jobcopilot/http.py.

Each robots case goes through the public PoliteClient API with a fake requests
adapter, so nothing here touches a socket and the tests don't depend on how
robots.txt is parsed internally.
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import pytest
import requests
import urllib3.util.connection
from requests.structures import CaseInsensitiveDict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobcopilot import http as jhttp  # noqa: E402
from jobcopilot.http import (  # noqa: E402
    HostDenied,
    PoliteClient,
    RobotsDisallowed,
    RobotsUnavailable,
)

UA = "job-copilot/2.0 (personal, non-commercial; test)"
A = "https://a.test"
B = "https://b.test"


# ------------------------------------------------------------------ harness

@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No real network, no real config, no shared default client."""

    def guarded(address, *args, **kwargs):
        raise AssertionError(f"test tried to open a socket: {address[0]}")

    monkeypatch.setattr(urllib3.util.connection, "create_connection", guarded)
    monkeypatch.delenv(jhttp.RECORD_ENV, raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sources.yaml").write_text(f"http:\n  user_agent: \"{UA}\"\n", encoding="utf-8")
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
        return [s for s in self.sleeps if s > 0.01]


@pytest.fixture
def ft() -> FakeTime:
    return FakeTime()


Reply = Any  # (status, headers, body) | callable(request) -> that | Exception instance


class Web(requests.adapters.BaseAdapter):
    """Fake internet: `routes[url-without-query]` is a list of replies served in
    order (the last one repeats). Unknown URLs answer 200 "ok", unknown
    robots.txt answers 404. An Exception instance as a reply is raised."""

    def __init__(self, routes: Optional[dict[str, Any]] = None) -> None:
        super().__init__()
        self.routes: dict[str, list[Any]] = {k: (v if isinstance(v, list) else [v])
                                             for k, v in (routes or {}).items()}
        self.sent: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):  # noqa: D401 — requests adapter API
        self.sent.append(request)
        key = request.url.split("?")[0]
        replies = self.routes.get(key)
        if replies:
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
        elif key.endswith("/robots.txt"):
            reply = (404, {}, b"")
        else:
            reply = (200, {}, b"ok")
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, Exception):
            raise reply
        status, headers, body = reply
        resp = requests.Response()
        resp.status_code, resp.reason = status, "fake"
        resp.headers = CaseInsensitiveDict(headers)
        resp._content, resp._content_consumed = body, True
        resp.url, resp.request, resp.encoding = request.url, request, "utf-8"
        return resp

    def close(self) -> None:
        pass

    def urls(self) -> list[str]:
        return [r.url for r in self.sent]

    def hosts(self) -> list[str]:
        return [urlsplit(r.url).hostname for r in self.sent]


def make_client(ft: FakeTime, web: Web, **overrides: Any) -> PoliteClient:
    sess = requests.Session()
    sess.trust_env = False
    sess.mount("https://", web)
    sess.mount("http://", web)
    opts: dict[str, Any] = dict(
        user_agent=UA, timeout_sec=5, default_rate_per_sec=1000.0, max_retries=3,
        session=sess, clock=ft.clock, sleep=ft.sleep, wallclock=ft.wall,
        rng=random.Random(7),
    )
    opts.update(overrides)
    return PoliteClient(**opts)


def fetches(client: PoliteClient, url: str) -> bool:
    try:
        client.get(url)
        return True
    except RobotsDisallowed:
        return False


def robots_allows(ft: FakeTime, robots: bytes, path: str) -> bool:
    web = Web({f"{A}/robots.txt": (200, {}, robots)})
    allowed = fetches(make_client(ft, web), A + path)
    sent_target = any(urlsplit(u).path != "/robots.txt" for u in web.urls())
    assert sent_target == allowed  # a refused URL is never sent
    return allowed


# ------------------------------------------------------- robots.txt parsing

ROBOTS_CASES = [
    # (id, robots.txt, path, allowed)
    # finding #15: a UTF-8 BOM must not hide the first group
    ("bom", b"\xef\xbb\xbfUser-agent: *\nDisallow: /\n", "/jobs", False),
    ("bom-own-group", b"\xef\xbb\xbfUser-agent: job-copilot\nDisallow: /\n\nUser-agent: *\nAllow: /\n",
     "/jobs", False),
    # finding #16: groups for the same agent are merged; blank lines don't end a group
    ("duplicate-star-groups", b"User-agent: *\nDisallow: /admin/\n\nUser-agent: *\nDisallow: /jobs/\n",
     "/jobs/1", False),
    ("blank-line-after-ua", b"User-agent: *\n\nDisallow: /\n", "/jobs", False),
    ("own-groups-merged",
     b"User-agent: job-copilot\nDisallow: /a/\n\nUser-agent: job-copilot\nDisallow: /b/\n\nUser-agent: *\nAllow: /\n",
     "/b/x", False),
    ("multi-ua-group", b"User-agent: other\nUser-agent: *\nDisallow: /x\n", "/x/1", False),
    ("other-agent-only", b"User-agent: otherbot\nDisallow: /\n", "/jobs", True),
    # finding #17: longest match counts decoded octets, not percent-quoted ones
    ("wildcard-allow-vs-longer-disallow", b"User-agent: *\nDisallow: /api/private\nAllow: /*.json$\n",
     "/api/private/x.json", False),
    ("css-allow-vs-wp-admin", b"User-agent: *\nDisallow: /wp-admin/\nAllow: /*.css$\n",
     "/wp-admin/x.css", False),
    ("longer-allow-wins", b"User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/*.css$\n",
     "/wp-admin/x.css", True),
    ("tie-goes-to-allow", b"User-agent: *\nDisallow: /page\nAllow: /page\n", "/page", True),
    ("query-equals-not-inflated", b"User-agent: *\nDisallow: /search\nAllow: /s?q=\n",
     "/search?q=1", False),
    # finding #28: exact product-token match, falling back to *
    ("substring-token-job", b"User-agent: job\nAllow: /\n\nUser-agent: *\nDisallow: /\n", "/jobs", False),
    ("substring-token-copilot",
     b"User-agent: copilot\nDisallow: /\n\nUser-agent: job-copilot\nAllow: /\n", "/jobs", True),
    ("longer-token-not-ours", b"User-agent: job-copilot-pro\nDisallow: /\n", "/jobs", True),
    ("token-case-insensitive", b"User-agent: Job-Copilot\nDisallow: /\n\nUser-agent: *\nAllow: /\n",
     "/jobs", False),
    ("token-with-version", b"User-agent: job-copilot/2.0\nDisallow: /\n", "/jobs", False),
    ("own-group-overrides-star", b"User-agent: job-copilot\nAllow: /\n\nUser-agent: *\nDisallow: /\n",
     "/jobs", True),
    # percent-encoding and non-ASCII: compared decoded, both ways
    ("rule-encoded-target-plain", b"User-agent: *\nDisallow: /%62%61%7A\n", "/baz", False),
    ("rule-plain-target-encoded", b"User-agent: *\nDisallow: /baz\n", "/%62az", False),
    ("non-ascii-rule", "User-agent: *\nDisallow: /café\n".encode("utf-8"), "/caf%C3%A9/menu", False),
    ("literal-encoded-star-is-not-wildcard", b"User-agent: *\nDisallow: /a%2Ab\n", "/axxb", True),
    # wildcards and the end anchor
    ("anchor-blocks-exact", b"User-agent: *\nDisallow: /*.pdf$\n", "/a.pdf", False),
    ("anchor-ignores-query-suffix", b"User-agent: *\nDisallow: /*.pdf$\n", "/a.pdf?page=2", True),
    ("mid-wildcards", b"User-agent: *\nDisallow: /*/private/*.json\n", "/u/1/private/x.json?v=2", False),
    ("wildcard-miss", b"User-agent: *\nDisallow: /*/private/*.json\n", "/u/1/public/x.json", True),
    ("leading-star", b"User-agent: *\nDisallow: *feed=\n", "/x?feed=rss", False),
    # syntax
    ("crlf-and-comments", b"User-agent: * # everyone\r\nDisallow: /x # private\r\n", "/x/1", False),
    ("cr-only", b"User-agent: *\rDisallow: /x\r", "/x/1", False),
    ("empty-disallow-allows-all", b"User-agent: *\nDisallow:\n", "/x", True),
    ("missing-leading-slash", b"User-agent: *\nDisallow: private\n", "/private/1", False),
    ("rules-before-any-group-ignored", b"Disallow: /\n\nUser-agent: *\nAllow: /x\n", "/jobs", True),
    ("key-case-and-spacing", b"user-AGENT :*\nDISALLOW : /x\n", "/x", False),
]


@pytest.mark.parametrize("robots,path,allowed",
                         [c[1:] for c in ROBOTS_CASES], ids=[c[0] for c in ROBOTS_CASES])
def test_robots_rfc9309_cases(ft, robots, path, allowed):
    assert robots_allows(ft, robots, path) is allowed


def test_robots_wildcards_are_linear_time(ft):
    # finding #23: a backtracking regex took ~6 s for 7 wildcards and minutes for more.
    for n_stars in (7, 40):
        rule = "/" + "*a" * n_stars + "*b"
        web = Web({f"{A}/robots.txt": (200, {}, f"User-agent: *\nDisallow: {rule}\n".encode())})
        client = make_client(ft, web)
        start = time.perf_counter()
        assert fetches(client, A + "/" + "a" * 60) is True  # no 'b': the rule can't match
        assert time.perf_counter() - start < 0.5, f"{n_stars} wildcards"


def test_redirect_to_empty_path_with_query_is_robots_checked(ft):
    # finding #24: 'https://b.test?ref=1' used to become the robots target '%3Fref%3D1'.
    web = Web({
        f"{A}/go": (302, {"Location": f"{B}?ref=1"}, b""),
        f"{B}/robots.txt": (200, {}, b"User-agent: *\nDisallow: /\n"),
    })
    with pytest.raises(RobotsDisallowed):
        make_client(ft, web).get(f"{A}/go")
    assert web.urls()[-1] == f"{B}/robots.txt"
    assert not any(u.startswith(B) and "robots.txt" not in u for u in web.urls())


def test_fractional_crawl_delay_is_honoured(ft):
    # finding #25: the stdlib parser dropped non-integer Crawl-delay values.
    web = Web({f"{A}/robots.txt": (200, {}, b"User-agent: *\nCrawl-delay: 2.5\n")})
    client = make_client(ft, web, default_rate_per_sec=10.0)
    client.get(f"{A}/a")
    client.get(f"{A}/b")
    assert ft.big_sleeps() == [2.5, 2.5]


def test_crawl_delay_from_own_group_and_bad_values_ignored(ft):
    web = Web({f"{A}/robots.txt": (200, {}, (
        b"User-agent: *\nCrawl-delay: 1\n\n"
        b"User-agent: job-copilot\nCrawl-delay: nan\nCrawl-delay: -3\nCrawl-delay: 4\nAllow: /\n"
    ))})
    client = make_client(ft, web, default_rate_per_sec=10.0)
    client.get(f"{A}/a")
    client.get(f"{A}/b")
    assert ft.big_sleeps() == [4.0, 4.0]


# ------------------------------------------- robots fetch failures and TTLs

def test_transient_robots_failure_is_retried_within_the_fetch(ft):
    # finding #27: one connection blip used to block the host with no retry.
    web = Web({f"{A}/robots.txt": [requests.ConnectionError("blip"),
                                   (200, {}, b"User-agent: *\nDisallow: /private/\n")]})
    client = make_client(ft, web)
    assert fetches(client, f"{A}/jobs") is True
    assert fetches(client, f"{A}/private/x") is False
    assert web.urls().count(f"{A}/robots.txt") == 2


def test_transient_robots_failure_is_cached_briefly_not_for_a_day(ft):
    # findings #8 / #27: a failed robots fetch used to block the host for robots_ttl_sec (24 h).
    web = Web({f"{A}/robots.txt": [requests.ConnectionError("down")] * 3 + [(404, {}, b"")]})
    client = make_client(ft, web, max_retries=3)
    with pytest.raises(RobotsUnavailable) as exc:
        client.get(f"{A}/jobs")
    assert exc.value.transient is True
    robots_fetches = web.urls().count(f"{A}/robots.txt")
    assert robots_fetches == 3  # first try + 2 retries

    ft.now += 60  # still inside the short negative-cache window: no new robots fetch
    with pytest.raises(RobotsUnavailable):
        client.get(f"{A}/jobs")
    assert web.urls().count(f"{A}/robots.txt") == robots_fetches

    ft.now += client.robots_retry_ttl_sec  # window over: robots.txt is fetched again
    assert fetches(client, f"{A}/jobs") is True
    assert web.urls()[-2:] == [f"{A}/robots.txt", f"{A}/jobs"]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_robots_5xx_and_429_retry_then_short_ttl(ft, status, caplog):
    web = Web({f"{A}/robots.txt": (status, {}, b"nope")})
    client = make_client(ft, web)
    with caplog.at_level("WARNING", logger="jobcopilot.http"):
        with pytest.raises(RobotsUnavailable) as exc:
            client.get(f"{A}/jobs")
    assert exc.value.transient is True
    assert any("robots.txt" in r.message for r in caplog.records)
    assert web.urls() == [f"{A}/robots.txt"] * 3  # never the target itself
    ft.now += client.robots_retry_ttl_sec + 1
    web.routes[f"{A}/robots.txt"] = [(200, {}, b"User-agent: *\nAllow: /\n")]
    assert fetches(client, f"{A}/jobs") is True


@pytest.mark.parametrize("status", [401, 403])
def test_robots_auth_wall_is_definitive(ft, status):
    web = Web({f"{A}/robots.txt": (status, {}, b"")})
    client = make_client(ft, web)
    with pytest.raises(RobotsUnavailable) as exc:
        client.get(f"{A}/jobs")
    assert exc.value.transient is False
    ft.now += 3600  # an auth wall isn't a blip: cached for the normal TTL
    with pytest.raises(RobotsUnavailable):
        client.get(f"{A}/jobs")
    assert web.urls() == [f"{A}/robots.txt"]
    ft.now += client.robots_ttl_sec
    with pytest.raises(RobotsUnavailable):
        client.get(f"{A}/jobs")
    assert web.urls() == [f"{A}/robots.txt"] * 2


def test_robots_redirect_is_followed_and_applies_to_the_first_host(ft):
    web = Web({
        f"{A}/robots.txt": (301, {"Location": f"{B}/robots.txt"}, b""),
        f"{B}/robots.txt": (200, {}, b"User-agent: *\nDisallow: /private/\n"),
    })
    client = make_client(ft, web)
    assert fetches(client, f"{A}/private/x") is False
    assert fetches(client, f"{A}/jobs") is True
    assert web.urls() == [f"{A}/robots.txt", f"{B}/robots.txt", f"{A}/jobs"]


def test_robots_redirect_loop_disallows(ft):
    web = Web({f"{A}/robots.txt": (302, {"Location": f"{A}/robots.txt"}, b"")})
    with pytest.raises(RobotsUnavailable):
        make_client(ft, web).get(f"{A}/jobs")
    assert f"{A}/jobs" not in web.urls()


# ------------------------------------------- do-not-automate host denylist

@pytest.mark.parametrize("url", [
    "https://ycombinator.com/",
    "https://www.ycombinator.com/jobs",
    "https://news.ycombinator.com/item?id=1",
    "https://WWW.YCombinator.COM./companies",          # case + trailing dot
    "https://user:pw@www.ycombinator.com:8443/x",       # userinfo + port  (pii-ok: fake credentials)
    "http://www.workatastartup.com/jobs",
    "https://in.linkedin.com/jobs/view/1",
    "https://www.naukri.com/x",
])
def test_denied_hosts_refused_before_any_io(ft, url):
    web = Web({})
    client = make_client(ft, web, denied_hosts=["ycombinator.com"])
    with pytest.raises(HostDenied) as exc:
        client.get(url)
    assert isinstance(exc.value, RobotsDisallowed)  # old `except RobotsDisallowed` still catches it
    assert "pw" not in str(exc.value)
    assert web.sent == []  # not even robots.txt


@pytest.mark.parametrize("url", [
    "https://notycombinator.com/jobs",
    "https://ycombinator.com.evil.test/jobs",
    "https://ycombinator.co/jobs",
    "https://mylinkedin.com/x",
])
def test_denied_hosts_match_on_label_boundaries_only(ft, url):
    web = Web({})
    make_client(ft, web, denied_hosts=["ycombinator.com"]).get(url)
    assert web.sent[-1].url == url


def test_redirect_into_denied_host_is_refused(ft):
    web = Web({f"{A}/go": (302, {"Location": "https://news.ycombinator.com/item?id=1"}, b"")})
    with pytest.raises(HostDenied):
        make_client(ft, web).get(f"{A}/go")
    assert web.hosts() == ["a.test", "a.test"]  # robots.txt + /go, nothing to YC


def test_robots_redirect_into_denied_host_is_not_followed(ft):
    web = Web({f"{A}/robots.txt": (301, {"Location": "https://www.linkedin.com/robots.txt"}, b"")})
    client = make_client(ft, web)
    with pytest.raises(RobotsUnavailable) as exc:
        client.get(f"{A}/jobs")
    assert exc.value.transient is False
    assert web.urls() == [f"{A}/robots.txt"]


def test_builtin_denylist_applies_without_config_and_cannot_be_emptied(ft):
    web = Web({})
    for denied in (None, [], ["example-only.test"]):
        client = make_client(ft, web, denied_hosts=denied)
        with pytest.raises(HostDenied):
            client.get("https://www.linkedin.com/jobs")
        assert client.is_denied("https://wellfound.com/x") and not client.is_denied("https://a.test/")
    assert web.sent == []


def test_denied_hosts_read_from_config(ft, tmp_path, monkeypatch):
    cfg = tmp_path / "config2"
    cfg.mkdir()
    (cfg / "sources.yaml").write_text(
        f"http:\n  user_agent: \"{UA}\"\n  denied_hosts: [\"*.blocked.test\", \"https://other.test/x\"]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JOBCOPILOT_CONFIG_DIR", str(cfg))
    web = Web({})
    sess = requests.Session()
    sess.mount("https://", web)
    client = PoliteClient.from_config(session=sess, clock=ft.clock, sleep=ft.sleep)
    for url in ("https://blocked.test/", "https://a.blocked.test/x", "https://other.test/y",
                "https://www.ycombinator.com/"):
        with pytest.raises(HostDenied):
            client.get(url)
    assert web.sent == []
    jhttp.reset_default_client()
    with pytest.raises(HostDenied):
        jhttp.default_client().get("https://jobs.blocked.test/")


def test_denied_host_wins_over_robots_exemption(ft):
    web = Web({})
    client = make_client(ft, web, robots_exempt_hosts={"www.glassdoor.com": "never valid"})
    with pytest.raises(HostDenied):
        client.get("https://www.glassdoor.com/Jobs")
    assert web.sent == []


@pytest.mark.parametrize("bad", [[""], ["foo bar.com"], ["a.com/path"], [123], 42])
def test_bad_denied_hosts_config_refuses_to_build(ft, bad):
    with pytest.raises(ValueError):
        make_client(ft, Web({}), denied_hosts=bad)
