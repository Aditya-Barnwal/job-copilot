"""Cross-source dedupe: one role seen on Himalayas and on the employer's Ashby
board is ONE job with two sources.

Two keys, applied in order (PLAN.md L#6):

  * url_key       - the same normalised URL (posting URL or apply link) is the
                    same job, whatever the sources say about it;
  * canonical_key - hash of normalised company + title + location bucket, for
                    the common case where a board copies the role but links
                    to its own page.

Merging is deliberately conservative: a wrong merge hides a real role, a missed
merge only shows one twice. So levels stay distinct ("Engineer II" vs "III"),
and two different postings from the SAME source are never merged by canonical
key (Ashby boards often list one role per region with identical titles).
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re
import unicodedata
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

from .models import Job, REMOTE
from .normalize import domain_of, normalize_company_name

log = logging.getLogger("jobcopilot.dedupe")

# Employer ATS first: their data is the employer's own. Boards after, Himalayas
# first among them because its terms make its applicationLink THE apply link.
ATS_SOURCES: tuple[str, ...] = ("greenhouse", "lever", "ashby", "workday", "smartrecruiters", "gem")
BOARD_SOURCES: tuple[str, ...] = ("himalayas", "remoteok", "workingnomads", "weworkremotely",
                                  "jobspresso", "adzuna", "jsearch")
_RANK = {s: i for i, s in enumerate(ATS_SOURCES + BOARD_SOURCES)}

# Query params that only say where a click came from. Anything else (gh_jid,
# job ids on custom career pages) can identify the posting, so it is kept.
_TRACKING_PARAMS = {
    "ref", "referrer", "source", "src", "gh_src", "lever-source", "lever-origin",
    "lever-source[]", "trk", "gclid", "fbclid", "mc_cid", "mc_eid", "utm", "via",
}

# Title tokens that never distinguish two roles. Kept short on purpose.
_TITLE_NOISE = {"remote", "hybrid", "onsite", "wfh"}
_GENDER_TAG = re.compile(r"\b[mfwdx]\s*/\s*[mfwdx](?:\s*/\s*[mfwdx])?\b")   # "(m/f/d)"
_TITLE_ALIASES = {"sr": "senior", "jr": "junior", "snr": "senior", "ii": "2", "iii": "3", "iv": "4",
                  "engg": "engineering"}
_CITY_ALIASES = {"bengaluru": "bangalore", "gurugram": "gurgaon", "bombay": "mumbai",
                 "new delhi": "delhi", "sf": "san francisco", "nyc": "new york"}
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def source_rank(source: Optional[str]) -> int:
    """Lower is better; unknown sources sort after every known one."""
    return _RANK.get((source or "").lower(), len(_RANK))


def is_ats(source: Optional[str]) -> bool:
    return (source or "").lower() in ATS_SOURCES


def _fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower().replace("&", " and ")


def normalize_title(title: Optional[str]) -> str:
    """'Sr. Software Engineer II (Remote)' -> 'senior software engineer 2'."""
    words = _NON_ALNUM.sub(" ", _GENDER_TAG.sub(" ", _fold(title or ""))).split()
    out = [_TITLE_ALIASES.get(w, w) for w in words if w not in _TITLE_NOISE]
    return " ".join(out)


def location_bucket(job: Job) -> str:
    """'remote' for remote roles, else '<country bucket>:<first city>'.

    Remote roles collapse to one bucket because boards rewrite remote locations
    freely ("Worldwide", "Remote - US", "Anywhere"); same-source postings are
    protected from merging separately (see merge()).
    """
    if (job.workplace_type or "").lower() == REMOTE:
        return "remote"
    first = (job.location_text or "").split(",")[0].split("/")[0]
    city = " ".join(_NON_ALNUM.sub(" ", _fold(first)).split())
    city = _CITY_ALIASES.get(city, city)
    return f"{job.country_bucket or 'unknown'}:{city or 'unknown'}"


def canonical_key(job: Job) -> str:
    """Stable 16-hex hash of company + title + location bucket."""
    basis = "|".join((normalize_company_name(job.company), normalize_title(job.title), location_bucket(job)))
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def url_key(url: Optional[str]) -> str:
    """Scheme-less, lowercase-host, slash-trimmed URL with tracking params dropped.

    'HTTPS://www.Jobs.Example.com/a/b/?utm_source=x&gh_jid=1#top'
      -> 'jobs.example.com/a/b?gh_jid=1'
    """
    if not url or not url.strip():
        return ""
    raw = url.strip()
    parts = urlsplit(raw if "://" in raw else "https://" + raw)
    host = domain_of(raw)
    if not host:
        return ""
    path = re.sub(r"/{2,}", "/", parts.path or "").rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")]
    q = urlencode(sorted(query))
    return f"{host}{path}" + (f"?{q}" if q else "")


# Sources whose ids are only unique inside one employer's tenant (Workday
# requisition ids like "R-12345" repeat across companies). job_sources is
# UNIQUE(source, external_id), so those ids are qualified with the company.
_TENANT_SCOPED_IDS = {"workday"}


def listing_id(job: Job) -> str:
    """The id stored in job_sources.external_id: globally unique per source."""
    if (job.source or "").lower() in _TENANT_SCOPED_IDS:
        return f"{normalize_company_name(job.company)}/{job.external_id}"
    return str(job.external_id)


def occurrence(job: Job) -> dict[str, Any]:
    """Where a single record was seen: one job_sources row."""
    return {
        "source": job.source,
        "external_id": listing_id(job),
        "url": job.url or None,
        "apply_url": job.apply_url or None,
        "attribution_name": job.attribution_name,
        "attribution_url": job.attribution_url,
    }


def sources_of(job: Job) -> list[dict[str, Any]]:
    """The occurrences merge() attached, or just the job's own."""
    attached = getattr(job, "sources", None)
    return list(attached) if attached else [occurrence(job)]


def _richness(job: Job) -> tuple[int, int]:
    comp = job.compensation
    has_comp = int(bool(comp and (comp.min_value is not None or comp.max_value is not None)))
    return has_comp, len(job.description_text or "")


# Fields copied onto the primary when it lacks them.
_FILL_FIELDS = ("description_text", "department", "compensation", "posted_at", "location_text",
                "company_sector", "region_restrictions", "timezone_restrictions",
                "tz_requirement_text", "attribution_name", "attribution_url")


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == "unknown"


def _merge_group(group: list[Job]) -> Job:
    ordered = sorted(group, key=lambda j: (source_rank(j.source), tuple(-x for x in _richness(j))))
    primary = copy.copy(ordered[0])
    others = ordered[1:]
    for f in _FILL_FIELDS:
        if _empty(getattr(primary, f)):
            for o in others:
                if not _empty(getattr(o, f)):
                    setattr(primary, f, copy.copy(getattr(o, f)))
                    break
    # workplace + country travel together: a board saying "remote, IN" fills both.
    if primary.workplace_type == "unknown" or primary.country_bucket == "unknown":
        for o in others:
            if primary.workplace_type == "unknown" and o.workplace_type != "unknown":
                primary.workplace_type = o.workplace_type
            if primary.country_bucket == "unknown" and o.country_bucket != "unknown":
                primary.country_bucket = o.country_bucket
    # A board's expiry describes the board listing, not the employer's posting.
    if not is_ats(primary.source) and not primary.expires_at:
        primary.expires_at = next((o.expires_at for o in others if o.expires_at), None)

    # D11 apply rule: the employer's ATS when we found it there independently,
    # else the best board's own apply link (Himalayas: its applicationLink).
    if is_ats(primary.source):
        primary.apply_url = primary.url or primary.apply_url
    else:
        primary.apply_url = primary.apply_url or primary.url

    occ: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for j in ordered:
        for o in sources_of(j):
            k = (o["source"], o["external_id"])
            if k not in seen:
                seen.add(k)
                occ.append(o)
    primary.sources = occ  # type: ignore[attr-defined]
    primary.canonical_key = canonical_key(primary)  # type: ignore[attr-defined]
    return primary


def merge(jobs: Iterable[Job]) -> list[Job]:
    """Collapse duplicates; return one primary per real role, input order kept.

    Each primary carries `.sources` (every occurrence) and `.canonical_key`.
    """
    jobs = list(jobs)
    parent = list(range(len(jobs)))
    members: dict[int, list[int]] = {i: [i] for i in range(len(jobs))}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def srcs(root: int) -> set[str]:
        return {jobs[m].source for m in members[root]}

    def union(a: int, b: int, *, distinct_sources: bool) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if distinct_sources and srcs(ra) & srcs(rb):
            return
        ra, rb = min(ra, rb), max(ra, rb)
        parent[rb] = ra
        members[ra].extend(members.pop(rb))

    # 1a) the same listing twice (source + company + id, as Phase 0 keyed it)
    by_id: dict[str, int] = {}
    for i, j in enumerate(jobs):
        k = j.key
        if k in by_id:
            union(by_id[k], i, distinct_sources=False)
        else:
            by_id[k] = i
    # 1b) a posting URL seen again as another source's posting URL or apply
    # link. Never apply-link to apply-link: boards often point every role of a
    # company at one generic careers page, which says nothing about the role.
    by_url: dict[str, int] = {}
    for i, j in enumerate(jobs):
        k = url_key(j.url)
        if k:
            if k in by_url:
                union(by_url[k], i, distinct_sources=True)
            else:
                by_url[k] = i
    for i, j in enumerate(jobs):
        k = url_key(j.apply_url)
        if k and k in by_url:
            union(by_url[k], i, distinct_sources=True)
    # 2) same company + title + location, across sources only
    by_canon: dict[str, list[int]] = {}
    for i, j in enumerate(jobs):
        by_canon.setdefault(canonical_key(j), []).append(i)
    for idxs in by_canon.values():
        for i in idxs[1:]:
            for first in idxs:
                if first == i:
                    break
                union(first, i, distinct_sources=True)

    out = [_merge_group([jobs[m] for m in sorted(members[r])]) for r in sorted(members)]
    if len(out) != len(jobs):
        log.info("dedupe merged %d records into %d jobs", len(jobs), len(out))
    return out
