"""Offline tests for the community job-list connectors (Phase 1b, workstream L).

Fixtures (captured 2026-11-12, trimmed, emails/phones scrubbed, HN authors renamed):
  hn_hiring.json    9 real top-level posts from "Who is hiring? (November 2026)"
                    + synthetic: a nested reply, a deleted post, a prose post,
                    a job-seeker post and an India post with emails.
  speedyapply.json  {file: markdown}; NEW_GRAD_INTL.md rows are real, plus two
                    synthetic Acme rows (tracking params, "↳"). NEW_GRAD_USA.md is
                    synthesised in the same format (the USA file was not fetched).
  simplify.json     9 rows of listings.json (Canonical's 200+ countries cut to 45
                    and its date moved to 10 days ago; two synthetic rows).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from jobcopilot.connectors.hn_hiring import HNHiringConnector, ThreadNotFound
from jobcopilot.connectors.simplify import SimplifyConnector, remote_regions
from jobcopilot.connectors.speedyapply import (
    SpeedyApplyConnector, age_days, clean_apply_url, parse_tables, validate_github_path,
)
from jobcopilot.hn_parse import (
    EMAIL_PLACEHOLDER, comment_to_text, parse_header, parse_post, parse_salary, scrub_emails,
    split_roles,
)
from jobcopilot.models import HYBRID, IN, ONSITE, REMOTE, UNKNOWN

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)

BOARDS: dict[str, dict[str, Any]] = {
    "hn_hiring": {"attribution": {"name": "Hacker News — Who is hiring",
                                  "url": "https://news.ycombinator.com/submitted?id=whoishiring"},
                  "max_item_fetches": 6, "max_roles_per_post": 5},
    "speedyapply": {"attribution": {"name": "speedyapply (GitHub)",
                                    "url": "https://github.com/speedyapply/2027-SWE-College-Jobs"},
                    "repo": "speedyapply/2027-SWE-College-Jobs", "branch": "main",
                    "files": [{"path": "NEW_GRAD_INTL.md", "keep": "all"},
                              {"path": "NEW_GRAD_USA.md", "keep": "remote"}]},
    "simplify": {"attribution": {"name": "SimplifyJobs (GitHub)",
                                 "url": "https://github.com/SimplifyJobs/New-Grad-Positions"},
                 "max_age_days": 60},
}


def _json(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _no_emails(jobs) -> None:
    for j in jobs:
        blob = json.dumps(j.to_dict() | {"raw": j.raw}, ensure_ascii=False, default=str)
        assert "@" not in blob.replace("@ ", ""), blob[:300]


# ------------------------------------------------------------------ HN header parser

@pytest.mark.parametrize("header,company,roles,wp,regions", [
    ("Simple Analytics | DevOps Engineer | REMOTE (worldwide, any timezone) | Full-time | "
     "€5,000/month | https://www.simpleanalytics.com/jobs/devops",
     "Simple Analytics", ["DevOps Engineer"], REMOTE, ["Worldwide"]),
    ("Synteq | Growth Lead & Systems Administrator | Austin, TX | REMOTE (US) | Full-time",
     "Synteq", ["Growth Lead", "Systems Administrator"], REMOTE, ["US"]),
    ("Open Education / Neon | Senior Frontend Engineer | Utrecht, The Netherlands | HYBRID",
     "Open Education / Neon", ["Senior Frontend Engineer"], HYBRID, []),
    ("Shepherd (Series B) | ONSITE | San Francisco, CA",
     "Shepherd", [], ONSITE, []),
    ("PostHog | Full-Time | Technical CSMs, SRE, Product Manager, AI Research Engineer | "
     "REMOTE (all remote) | Hiring GMT-8 to GMT+2 | PostHog makes your product self-driving.",
     "PostHog", ["Technical CSMs", "SRE", "Product Manager", "AI Research Engineer"], REMOTE, []),
    ("NEO Holdings (stealth) | Malibu, CA | ONSITE (SoCal preferred) or REMOTE (US) | Full Time",
     "NEO Holdings", [], REMOTE, ["US"]),
    ("Cologne, Germany | UMH | Product Engineer | Full-time | 70k - 100k EUR | ONSITE (part remote)",
     "UMH", ["Product Engineer"], ONSITE, []),
    ("Tandem Health | Engineers, Designers, PMs | On-site in Stockholm, Sweden | Full time",
     "Tandem Health", ["Engineers", "Designers", "PMs"], ONSITE, []),
    ("Equall | Backend Engineer, Foundations | New York, NY | ONSITE | $160K–$250K + equity",
     "Equall", ["Backend Engineer, Foundations"], ONSITE, []),
    ("ORFF Maestra | Prototype Developer; Technical Product Lead | REMOTE (US only) | Contract",
     "ORFF Maestra", ["Prototype Developer", "Technical Product Lead"], REMOTE, ["US Only"]),
    ("Distru (https://www.distru.com) | AI Product Engineer | AI Product Manager | "
     "REMOTE (Americas & Europe) | Full-Time",
     "Distru", ["AI Product Engineer", "AI Product Manager"], REMOTE, ["Americas", "Europe"]),
    ("Paramark | Software Engineer (L4) | San Francisco HYBRID (3 days/wk) or REMOTE (US) | $199K–$249K",
     "Paramark", ["Software Engineer (L4)"], REMOTE, ["US"]),
    ("Pond AI | San Francisco | Onsite | https://joinpond.ai",
     "Pond AI", [], ONSITE, []),
    ("Oscilar.com | Sr/Staff Software Engineers, Data Scientist | REMOTE (US/Canada/UK/India) "
     "Full-time AI Risk Decisioning platform for banks and fintechs everywhere",
     "Oscilar.com", ["Sr/Staff Software Engineers", "Data Scientist"], REMOTE,
     ["India", "US", "Canada", "UK"]),
    ("SentiLink | Engineering & Data Science | REMOTE, HYBRID, and ONSITE (UNITED STATES ONLY)",
     "SentiLink", ["Engineering & Data Science"], REMOTE, ["US Only"]),
    ("Acme Robotics | Backend Engineer | Bengaluru, India | ONSITE | ₹25–40 LPA | jobs@acme.example",
     "Acme Robotics", ["Backend Engineer"], ONSITE, []),
    ("Starbridge | Senior Product Engineer | NYC | Full-time",
     "Starbridge", ["Senior Product Engineer"], UNKNOWN, []),
    ("Evaboot | Agentic Python Engineer | REMOTE worldwide | $60k-$120k",
     "Evaboot", ["Agentic Python Engineer"], REMOTE, ["Worldwide"]),
])
def test_parse_header(header, company, roles, wp, regions):
    post = parse_header(header)
    assert post.ok
    assert post.company == company
    assert post.roles == roles
    assert post.workplace_type == wp
    assert post.regions == regions
    assert "@" not in post.header and "@" not in post.location_text


def test_header_details_salary_tz_location_urls():
    p = parse_header("Simple Analytics | DevOps Engineer | REMOTE (worldwide, any timezone) | "
                     "€5,000/month | https://www.simpleanalytics.com/jobs/devops")
    assert (p.salary.currency, p.salary.min_value, p.salary.interval) == ("EUR", 5000.0, "1 MONTH")
    assert p.apply_url == "https://www.simpleanalytics.com/jobs/devops"
    assert p.tz_text and "timezone" in p.tz_text
    p = parse_header("PostHog | SRE | REMOTE | Hiring GMT-8 to GMT+2")
    assert p.tz_offsets == [-8.0, 2.0]
    p = parse_header("Synteq | Engineer | Austin, TX | REMOTE (US)")
    assert p.location_text == "Austin, TX; REMOTE (US)"
    # Header without a link: a jobs link from the body wins over nothing.
    p = parse_header("Acme | Engineer | REMOTE", "See https://acme.example/careers/eng")
    assert p.apply_url == "https://acme.example/careers/eng"


@pytest.mark.parametrize("text,cur,lo,hi,interval", [
    ("$60K–$100K base + upside", "USD", 60000, 100000, "1 YEAR"),
    ("$175,000 - $300,000", "USD", 175000, 300000, "1 YEAR"),
    ("70k - 100k EUR", "EUR", 70000, 100000, "1 YEAR"),
    ("€100–140k + 0.5–1% equity", "EUR", 100000, 140000, "1 YEAR"),
    ("up to £90k + up to 1% equity", "GBP", None, 90000, "1 YEAR"),
    ("£30k tax-free annual stipend", "GBP", 30000, 30000, "1 YEAR"),
    ("€5,000/month (full-time)", "EUR", 5000, 5000, "1 MONTH"),
    ("$55-75/hr", "USD", 55, 75, "1 HOUR"),
    ("₹25–40 LPA", "INR", 2_500_000, 4_000_000, "1 YEAR"),
    ("CAD $120k-$150k", "CAD", 120000, 150000, "1 YEAR"),
])
def test_parse_salary(text, cur, lo, hi, interval):
    c = parse_salary(text)
    assert (c.currency, c.min_value, c.max_value, c.interval) == (cur, lo, hi, interval)
    assert c.summary == text and not c.is_estimate


def test_salary_without_currency_keeps_text_not_numbers():
    c = parse_salary("salary 120k-150k")
    assert c is not None and c.currency is None and c.max_value is None and c.summary
    assert parse_salary("Full-time, 32h") is None


def test_split_roles():
    assert split_roles("Engineers, Designers, PMs") == ["Engineers", "Designers", "PMs"]
    assert split_roles("Backend Engineer, Foundations") == ["Backend Engineer, Foundations"]
    assert split_roles("Field Engineers (Kubernetes, Security)") == ["Field Engineers (Kubernetes, Security)"]
    assert split_roles("AI Developer + Linux Admin (junior to mid)") == ["AI Developer", "Linux Admin (junior to mid)"]


def test_emails_are_dropped_everywhere():
    html_text = ('Acme | Engineer | REMOTE | jobs@acme.example<p>Mail <a href="mailto:a@b.example">me</a>'
                 ' or a.b [at] acme [dot] example, or see <a href="https:&#x2F;&#x2F;acme.example&#x2F;jobs">'
                 'https:&#x2F;&#x2F;acme.example&#x2F;j...</a>')
    post, text = parse_post(html_text)
    assert "@" not in text and "[at]" not in text and "mailto" not in text
    assert text.count(EMAIL_PLACEHOLDER) == 2
    assert "https://acme.example/jobs" in text        # full href, not the truncated label
    assert post.apply_url == "https://acme.example/jobs"
    assert scrub_emails("x y.z+tag@sub.acme.example z") == f"x {EMAIL_PLACEHOLDER} z"
    assert comment_to_text("a<p>b") == "a\n\nb"


def test_prose_and_seeker_posts_are_not_jobs():
    assert not parse_post("Foo Corp is hiring a computer vision engineer in Seattle.")[0].ok
    assert not parse_post("Some Person | Software Engineer | India (REMOTE)<p>"
                          "I&#x27;m a software engineer. Willing to relocate: Yes")[0].ok


def test_max_roles_cap():
    post = parse_header("Acme | Engineers, Designers, PMs, Analysts, Recruiters, Writers, Admins | REMOTE")
    assert len(post.roles) == 5


# ------------------------------------------------------------------ HN connector

def test_hn_parse_fixture():
    jobs = HNHiringConnector().parse(_json("hn_hiring.json"), BOARDS["hn_hiring"])
    ids = {j.external_id for j in jobs}
    assert len(ids) == len(jobs)
    assert not any(i.startswith(("90000001", "90000003", "90000004", "90000005")) for i in ids)
    for j in jobs:
        assert j.source == "hn_hiring" and j.company and j.title
        assert j.url.startswith("https://news.ycombinator.com/item?id=")
        assert j.attribution_url == j.url
        assert j.attribution_name == "Hacker News — Who is hiring (November 2026)"
        assert j.apply_url.startswith("https://")
        assert j.description_text and j.posted_at
    _no_emails(jobs)

    posthog = [j for j in jobs if j.company == "PostHog"]
    assert [j.external_id.split(":")[1] for j in posthog] == ["1", "2", "3", "4"]
    acme = [j for j in jobs if j.company == "Acme Robotics"]
    assert [j.title for j in acme] == ["Backend Engineer", "Data Engineer"]
    assert acme[0].country_bucket == IN and acme[0].workplace_type == ONSITE
    assert acme[0].compensation.currency == "INR"
    assert acme[0].apply_url.startswith("https://news.ycombinator.com/item?id=90000002")
    assert EMAIL_PLACEHOLDER in acme[0].description_text
    oscilar = next(j for j in jobs if j.company == "Oscilar.com")
    assert "India" in oscilar.region_restrictions and oscilar.workplace_type == REMOTE
    sa = next(j for j in jobs if j.company == "Simple Analytics")
    assert sa.region_restrictions == ["Worldwide"]


class _FakeClient:
    def __init__(self, routes: dict[str, Any]):
        self.routes, self.urls = routes, []

    def get_json(self, url, **kw):
        self.urls.append(url)
        return self.routes[url]

    def get(self, url, **kw):
        self.urls.append(url)
        body = self.routes[url]

        class R:
            text = body
        return R()


def test_hn_fetch_finds_latest_hiring_thread_then_one_thread_call():
    fb = "https://hacker-news.firebaseio.com/v0"
    thread = _json("hn_hiring.json")
    fake = _FakeClient({
        f"{fb}/user/whoishiring.json": {"submitted": [111, 222, 333]},
        f"{fb}/item/111.json": {"title": "Ask HN: Freelancer? Seeking freelancer? (November 2026)"},
        f"{fb}/item/222.json": {"title": "Ask HN: Who is hiring? (November 2026)"},
        "https://hn.algolia.com/api/v1/items/222": thread,
    })
    raw = HNHiringConnector(client=fake).fetch_raw(BOARDS["hn_hiring"])
    assert raw is thread
    assert fake.urls[-1] == "https://hn.algolia.com/api/v1/items/222" and len(fake.urls) == 4


def test_hn_fetch_gives_up_after_max_item_fetches():
    fb = "https://hacker-news.firebaseio.com/v0"
    routes = {f"{fb}/user/whoishiring.json": {"submitted": list(range(1, 20))}}
    routes |= {f"{fb}/item/{i}.json": {"title": "Ask HN: Who wants to be hired?"} for i in range(1, 20)}
    fake = _FakeClient(routes)
    with pytest.raises(ThreadNotFound):
        HNHiringConnector(client=fake).fetch_raw({"max_item_fetches": 3})
    assert len(fake.urls) == 4
    with pytest.raises(ValueError):
        HNHiringConnector(client=fake).fetch_raw({"thread_id": "1/../2"})


# ------------------------------------------------------------------ speedyapply

def test_speedyapply_row_parsing():
    rows = parse_tables(_json("speedyapply.json")["NEW_GRAD_INTL.md"])
    first = rows[0]
    assert first["company"] == "NVIDIA" and first["section"] == "FAANG+"
    assert first["posting_link"].startswith("https://nvidia.wd5.myworkdayjobs.com/")
    assert first["age"] == "0d" and first["company_link"] == "https://www.nvidia.com"
    assert any(r["section"] == "Quant" for r in rows)
    assert next(r for r in rows if r.get("position") == "Data Engineer")["company"] == "Acme"  # "↳"
    usa = parse_tables(_json("speedyapply.json")["NEW_GRAD_USA.md"])
    assert usa[0]["salary"] == "$130k/yr" and usa[0]["posting_link"]


def test_speedyapply_parse():
    jobs = SpeedyApplyConnector().parse(_json("speedyapply.json"), BOARDS["speedyapply"], now=NOW)
    by_title = {(j.company, j.title): j for j in jobs}
    assert len(by_title) == len(jobs) == len({j.external_id for j in jobs})   # duplicate row dropped
    assert ("OnsiteCo", "Software Engineer") not in by_title                   # USA: remote rows only
    remote = by_title[("RemoteCo", "Software Engineer, New Grad")]
    assert remote.apply_url == "https://job-boards.greenhouse.io/remoteco/jobs/1"
    assert remote.workplace_type == REMOTE
    acme = by_title[("Acme", "Software Engineer I")]
    assert acme.apply_url == "https://jobs.lever.co/acme/123"                  # utm/ref stripped
    assert acme.posted_age_days == 21 and acme.posted_at == "2026-10-22"
    assert acme.country_bucket == IN
    india = [j for j in jobs if j.country_bucket == IN]
    assert len(india) >= 3
    for j in jobs:
        assert j.attribution_name == "speedyapply (GitHub)"
        assert j.attribution_url.startswith("https://github.com/speedyapply/2027-SWE-College-Jobs/blob/main/")
        assert j.url == j.apply_url and "speedyapply" not in j.apply_url


def test_speedyapply_helpers():
    assert [age_days(x) for x in ("0d", "12d", "3w", "2mo", "5h", "?")] == [0, 12, 21, 60, 0, None]
    assert clean_apply_url("https://go.example/r?url=https%3A%2F%2Fjobs.example%2Fa%3Fgh_jid%3D1"
                           "%26utm_source%3Dx") == "https://jobs.example/a?gh_jid=1"
    assert clean_apply_url("javascript:alert(1)") is None
    assert validate_github_path("a/b", "main", "dir/x.md") == "https://raw.githubusercontent.com/a/b/main/dir/x.md"
    for bad in (("a/b/c", "main", "x.md"), ("a/b", "../x", "x.md"), ("a/b", "main", "../x.md"),
                ("a/b", "main", "x.sh")):
        with pytest.raises(ValueError):
            validate_github_path(*bad)


def test_speedyapply_fetch_reads_each_file():
    base = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/"
    fx = _json("speedyapply.json")
    fake = _FakeClient({base + k: v for k, v in fx.items()})
    raw = SpeedyApplyConnector(client=fake).fetch_raw(BOARDS["speedyapply"])
    assert raw == fx and len(fake.urls) == 2


# ------------------------------------------------------------------ simplify

def test_simplify_india_remote_filtering():
    jobs = SimplifyConnector().parse(_json("simplify.json"), BOARDS["simplify"], now=NOW)
    names = sorted(j.company for j in jobs)
    # Kept: India onsite (Collinear), worldwide remote (Canonical), Remote in USA (RainFocus),
    # Remote in India (Acme). Dropped: old (Varian), Indianapolis (not India), inactive
    # (Welocalize), US onsite (ICE), hidden RainFocus duplicate.
    assert names == ["Acme Labs", "Canonical", "Collinear AI", "RainFocus"]
    by = {j.company: j for j in jobs}
    assert by["Collinear AI"].country_bucket == IN
    assert by["Canonical"].region_restrictions == ["Worldwide"] and by["Canonical"].workplace_type == REMOTE
    assert by["RainFocus"].region_restrictions == ["USA"]
    acme = by["Acme Labs"]
    assert acme.apply_url == "https://jobs.ashbyhq.com/acme/1" and acme.country_bucket == IN
    assert "Sponsorship" in acme.description_text
    for j in jobs:
        assert j.attribution_name == "SimplifyJobs (GitHub)"
        assert j.attribution_url == "https://github.com/SimplifyJobs/New-Grad-Positions"
        assert j.external_id and j.posted_at


def test_simplify_remote_regions():
    assert remote_regions(["Remote in USA", "Remote in Canada", "NYC"]) == ["USA", "Canada"]
    assert remote_regions([f"Remote in C{i}" for i in range(45)]) == ["Worldwide"]


def test_simplify_fetch_uses_size_cap_and_deadline():
    seen: dict[str, Any] = {}

    class Fake:
        def get_json(self, url, **kw):
            seen.update(kw, url=url)
            return []

    SimplifyConnector(client=Fake()).fetch_raw(BOARDS["simplify"])
    assert seen["url"] == ("https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/"
                           ".github/scripts/listings.json")
    assert seen["max_bytes"] >= 20_000_000 and seen["deadline_sec"] > 0


# ------------------------------------------------- Phase 1b review fixes (L1/L2/L8/S4/L9/L10)

@pytest.mark.parametrize("text,cur,lo,hi", [
    ("$120k, equity", "USD", 120000, 120000),             # L1: trailing comma kept the "k"
    ("$100k-$130k, DOE", "USD", 100000, 130000),          # L1: bounds were swapped
    ("$100k - $130k, equity + bonus", "USD", 100000, 130000),
    ("$150-200k, 20 days PTO", "USD", 150000, 200000),    # a later bare number is not a bound
    ("€50 000 - €70 000", "EUR", 50000, 70000),
    ("€50 000 – €70 000", "EUR", 50000, 70000),
    ("70.000 - 90.000 EUR", "EUR", 70000, 90000),
    ("50k–70k €", "EUR", 50000, 70000),
    ("£55,000 - £65,000", "GBP", 55000, 65000),
    ("CA$100k", "CAD", 100000, 100000),                   # L8
    ("C$100k-120k", "CAD", 100000, 120000),
    ("A$120k", "AUD", 120000, 120000),
    ("AU$120k", "AUD", 120000, 120000),
    ("NZ$90k", "NZD", 90000, 90000),
    ("S$8,000/month", "SGD", 8000, 8000),
    ("HK$50k", "HKD", 50000, 50000),
    ("US$130k", "USD", 130000, 130000),
])
def test_parse_salary_review_strings(text, cur, lo, hi):
    c = parse_salary(text)
    assert c is not None and (c.currency, c.min_value, c.max_value) == (cur, lo, hi), text
    assert c.min_value <= c.max_value


def test_non_usd_dollars_have_no_inr_estimate_instead_of_wrong_usd():
    assert parse_salary("CA$100k").annual_inr() is None
    assert parse_salary("A$120k").annual_inr() is None
    assert parse_salary("$100k").annual_inr() == pytest.approx(100000 * 83.0)


@pytest.mark.parametrize("text", ["$40M Series B", "Raised $12M", "$12M seed round", "$2B valuation",
                                  "ARR $5M", "$40,000,000", "$40,000,000 salary"])
def test_funding_and_implausible_amounts_are_not_salary(text):
    assert parse_salary(text) is None


def test_funding_field_never_becomes_the_salary():
    p = parse_header("Acme | Backend Engineer | Series B ($40M raised) | $150-200k | REMOTE")
    assert p.salary and (p.salary.min_value, p.salary.max_value) == (150000, 200000)
    p = parse_header("Acme | $40M Series B | Backend Engineer | REMOTE")
    assert p.salary is None and p.roles == ["Backend Engineer"]
    p = parse_header("Acme | Raised $12M | Backend Engineer | $130-160k/yr | REMOTE")
    assert p.salary and p.salary.max_value == 160000
    # the funding amount sits in the same field as the real range
    p = parse_header("Acme | Backend Engineer | Series B ($40M raised) $150-200k/yr | REMOTE")
    assert p.salary and p.salary.max_value == 200000
    # two salary-looking fields: the one with a range / yearly cue wins
    p = parse_header("Acme | Backend Engineer | $30k signing | $150-200k | REMOTE")
    assert p.salary and (p.salary.min_value, p.salary.max_value) == (150000, 200000)


def test_m_multiplier_needs_salary_words():
    c = parse_salary("comp up to $1.2M")
    assert c and c.max_value == 1_200_000


@pytest.mark.parametrize("text", [
    "josé@example.com", "müller@example.de", "jane@münchen.de", "田中@例え.jp",  # pii-ok: fake scrubber test input
    "jane @ example.com", "jane@ example.com", "jane [at] example [dot] com",
    "email jane at example dot com", "contact: jane at example dot com", "mail me at jane at example dot com",
])
def test_scrub_emails_unicode_spaced_and_word_forms(text):
    out = scrub_emails(f"Reach us: {text} thanks")
    assert EMAIL_PLACEHOLDER in out
    for leak in ("jane", "mü", "josé", "田中", "example", "münchen"):
        assert leak not in out, (text, out)


def test_scrub_emails_leaves_ordinary_prose_alone():
    s = "We are looking at foo dot bar and meet at 5pm @home"
    assert scrub_emails(s) == s


def test_link_with_email_is_dropped_from_apply_candidates():
    html = ('Acme | Engineer | REMOTE | <a href="https://acme.com/apply?e=jane@acme.com">'  # pii-ok: fake scrubber test input
            'https://acme.com/apply?e=jane@ac...</a> and '
            '<a href="https://acme.com/jobs">https://acme.com/jobs</a><p>email jane at acme dot com')
    post, text = parse_post(html)
    assert post.apply_url == "https://acme.com/jobs"
    assert "jane" not in text and "jane" not in post.header
    # when the only link carries an email there is no apply link (the connector falls back)
    post, text = parse_post("Acme | Engineer | REMOTE | https://acme.com/apply/jane@acme.com<p>x")  # pii-ok: fake scrubber test input
    assert post.apply_url is None and "jane" not in text and "@" not in text


def test_hn_connector_no_email_survives_unicode_and_spaced_forms():
    thread = {"id": 1, "title": "Ask HN: Who is hiring? (November 2026)", "children": [{
        "id": 5, "type": "comment", "parent_id": 1, "author": "x", "created_at": "2026-11-01T00:00:00Z",
        "text": ('Acme | Backend Engineer | REMOTE (US) | <a href="https://acme.com/apply?u=josé@example.com">'
                 'https://acme.com/apply?u=jos...</a> | contact josé@example.com<p>Write jane @ example.com '
                 'or müller@example.de, email: jane at example dot com')}]}  # pii-ok: fake scrubber test input
    jobs = HNHiringConnector().parse(thread, BOARDS["hn_hiring"])
    assert jobs
    for j in jobs:
        blob = json.dumps([j.description_text, j.raw, j.apply_url, j.url, j.title, j.location_text],
                          ensure_ascii=False)
        for leak in ("@", "josé", "jane", "müller", "example.com", "example.de"):
            assert leak not in blob, (leak, blob)
        assert j.apply_url == j.url      # no email-free link left: HN permalink fallback


def test_speedyapply_split_row_unescapes_pipes_and_splits_on_unescaped_only():
    from jobcopilot.connectors.speedyapply import _split_row
    assert _split_row(r"| A \| B | Eng \| Ops | Remote | x | 1d |") == ["A | B", "Eng | Ops", "Remote", "x", "1d"]
    assert _split_row("| a || c | |") == ["a", "", "c", ""]


def test_speedyapply_counts_rows_with_wrong_cell_count():
    md = ("| Company | Position | Location | Posting | Age |\n|---|---|---|---|---|\n"
          "| Good | Eng | Remote in USA | <a href=\"https://g.example/1\">x</a> | 1d |\n"
          "| Bad | Eng | Remote in USA | extra | <a href=\"https://b.example/1\">x</a> | 1d |\n"
          "| Pipe | Eng \\| Ops | Remote | <a href=\"https://p.example/1\">x</a> | 2d |\n")
    stats: dict = {}
    rows = parse_tables(md, stats)
    assert [r["company"] for r in rows] == ["Good", "Pipe"] and stats == {"mismatched_rows": 1}
    assert rows[1]["position"] == "Eng | Ops"


@pytest.mark.parametrize("location,regions", [
    ("Remote in USA", ["USA"]),
    ("Remote in USA; Remote in Canada", ["USA", "Canada"]),
    ("Remote, India", ["India"]),
    ("United States (Remote)", ["United States"]),
    ("Remote", []),
])
def test_speedyapply_remote_rows_get_region_restrictions(location, regions):
    from jobcopilot.connectors.speedyapply import location_regions
    assert location_regions(location) == regions


def test_speedyapply_usa_remote_row_is_region_restricted_onsite_row_is_not():
    jobs = SpeedyApplyConnector().parse(_json("speedyapply.json"), BOARDS["speedyapply"], now=NOW)
    remote_us = next(j for j in jobs if j.company == "RemoteCo")
    assert remote_us.workplace_type == REMOTE and remote_us.region_restrictions == ["USA"]
    acme = next(j for j in jobs if j.company == "Acme" and j.title == "Software Engineer I")
    assert acme.region_restrictions == ["India"]
    assert all(j.region_restrictions == [] for j in jobs if j.workplace_type != REMOTE)


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "file:///etc/passwd", "http://169.254.169.254/x",
                                 "http://localhost:9/x", "https://u:p@example.com/x",
                                 "https://example.com/?url=http://127.0.0.1/admin"])
def test_clean_apply_url_rejects_unsafe_targets(bad):
    assert clean_apply_url(bad) is None
    assert clean_apply_url("https://example.com/a?utm_source=x") == "https://example.com/a"
