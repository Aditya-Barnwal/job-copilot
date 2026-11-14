"""Cross-source dedupe: one role seen on Himalayas and on the employer's Ashby
board is ONE job with two sources.

Two keys, applied in order (docs/DESIGN.md §5):

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
#
# Both tiers are DERIVED from the connector registry (a new connector can't be
# forgotten): every REGISTRY source that is not a board or an aggregator is an
# employer ATS. Imported lazily because jobcopilot.connectors is a heavy import.
_AGGREGATORS = ("adzuna", "jsearch")   # many employers, but not in connectors.BOARD_SOURCES
_ATS_ORDER = ("greenhouse", "lever", "ashby", "workday", "smartrecruiters", "gem")
# Board preference, best first: the boards that expose real employer apply links
# and full descriptions lead; community lists scraped from GitHub / HN come last.
_BOARD_ORDER = ("himalayas", "remoteok", "workingnomads", "weworkremotely", "jobspresso",
                "adzuna", "jsearch", "remotecom", "jobicy", "arbeitnow", "fourdayweek",
                "nodesk", "realworkfromanywhere", "cryptojobslist", "hasjob",
                "hn_hiring", "speedyapply", "simplify")
_tiers_cache: Optional[tuple[tuple[str, ...], tuple[str, ...]]] = None


def _tiers() -> tuple[tuple[str, ...], tuple[str, ...]]:
    global _tiers_cache
    if _tiers_cache is None:
        from .connectors import BOARD_SOURCES as declared_boards, REGISTRY
        boards_all = set(declared_boards) | set(_AGGREGATORS)
        ats = [s for s in _ATS_ORDER if s in REGISTRY]
        ats += [s for s in REGISTRY if s not in boards_all and s not in ats]
        boards = [s for s in _BOARD_ORDER if s in boards_all or s in REGISTRY]
        boards += sorted(boards_all - set(boards))
        _tiers_cache = (tuple(ats), tuple(boards))
    return _tiers_cache


def __getattr__(name: str):   # PEP 562: dedupe.ATS_SOURCES / BOARD_SOURCES stay importable
    if name == "ATS_SOURCES":
        return _tiers()[0]
    if name == "BOARD_SOURCES":
        return _tiers()[1]
    raise AttributeError(name)


def _ranks() -> dict[str, int]:
    ats, boards = _tiers()
    return {s: i for i, s in enumerate(ats + boards)}

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
    ranks = _ranks()
    return ranks.get((source or "").lower(), len(ranks))


def is_ats(source: Optional[str]) -> bool:
    return (source or "").lower() in _tiers()[0]


def _fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower().replace("&", " and ")


def normalize_title(title: Optional[str]) -> str:
    """'Sr. Software Engineer II (Remote)' -> 'senior software engineer 2'."""
    words = _NON_ALNUM.sub(" ", _GENDER_TAG.sub(" ", _fold(title or ""))).split()
    out = [_TITLE_ALIASES.get(w, w) for w in words if w not in _TITLE_NOISE]
    return " ".join(out)


def location_bucket(job: Job) -> str:
    """'remote:<country bucket>' for remote roles, else '<country bucket>:<first city>'.

    Remote roles ignore the city because boards rewrite remote locations
    freely ("Worldwide", "Remote - US", "Anywhere"), but keep the IN / non-IN
    bucket so an India posting never merges with a US-only copy. A remote role
    whose country is unknown may still join one known bucket in merge(), only
    when that is unambiguous; same-source postings are protected separately.
    """
    if (job.workplace_type or "").lower() == REMOTE:
        return f"remote:{job.country_bucket or 'unknown'}"
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
_TENANT_SCOPED_IDS = {"workday", "bamboohr", "personio", "pinpoint", "keka"}  # ids only unique per employer


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


# Region tokens that name the same place. Only the spellings boards actually
# use; anything else compares by its folded text.
_REGION_ALIASES = {
    "us": "us", "usa": "us", "u s": "us", "u s a": "us", "the u s": "us", "united states": "us",
    "united states of america": "us", "america": "us",
    "uk": "uk", "u k": "uk", "united kingdom": "uk", "great britain": "uk", "britain": "uk",
    "in": "in", "india": "in", "eu": "europe", "european union": "europe",
}
_REGION_NOISE = re.compile(r"\b(?:only|remote|based|residents?|citizens?|in|from)\b")


def _region_token(token: str) -> str:
    t = " ".join(_NON_ALNUM.sub(" ", _fold(token)).split())
    if t in _REGION_ALIASES:
        return _REGION_ALIASES[t]
    t = " ".join(_REGION_NOISE.sub(" ", t).split())
    return _REGION_ALIASES.get(t, t)


def region_set(job: Job) -> frozenset[str]:
    return frozenset(x for x in (_region_token(r) for r in job.region_restrictions or []) if x)


def regions_conflict(a: Iterable[Job], b: Iterable[Job]) -> bool:
    """Both sides restrict regions and share none ('USA Only' vs 'Canada').
    Disjoint counts as a conflict: a missed merge beats a hidden role."""
    ra = frozenset().union(*(region_set(j) for j in a))
    rb = frozenset().union(*(region_set(j) for j in b))
    return bool(ra and rb and not (ra & rb))


# Location words that say nothing about WHERE ("Remote", "Worldwide").
_PLACELESS = {"remote", "hybrid", "onsite", "on", "site", "wfh", "work", "from", "home",
              "anywhere", "worldwide", "global", "globally", "world", "the", "in", "only",
              "fully", "first", "unknown", "location", "locations", "n", "a", "tbd"}


def has_place_signal(job: Job) -> bool:
    """True when the job says where it is: a known country bucket, or location
    text naming something beyond 'Remote' / 'Worldwide'."""
    if (job.country_bucket or "unknown") != "unknown":
        return True
    words = _NON_ALNUM.sub(" ", _fold(job.location_text or "")).split()
    return any(w not in _PLACELESS for w in words)


def _richness(job: Job) -> tuple[int, int]:
    comp = job.compensation
    has_comp = int(bool(comp and (comp.min_value is not None or comp.max_value is not None)))
    return has_comp, len(job.description_text or "")


# Fields copied onto the primary when it lacks them.
_FILL_FIELDS = ("description_text", "department", "compensation", "posted_at", "location_text",
                "company_sector", "region_restrictions", "timezone_restrictions",
                "tz_requirement_text", "attribution_name", "attribution_url")


_REGION_FIELDS = ("region_restrictions", "timezone_restrictions", "tz_requirement_text")


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == "unknown"


def _merge_group(group: list[Job]) -> Job:
    ordered = sorted(group, key=lambda j: (source_rank(j.source), tuple(-x for x in _richness(j))))
    primary = copy.copy(ordered[0])
    others = ordered[1:]
    # A board's "USA Only" must not become the restriction of an employer
    # posting that already says where it is ("Remote - India").
    own_place = has_place_signal(primary)
    for f in _FILL_FIELDS:
        if own_place and f in _REGION_FIELDS:
            continue
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

    # Apply rule (docs/DESIGN.md §5): the employer's ATS when we found it there independently,
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


def _component_info(group: list[Job]) -> Optional[dict[str, Any]]:
    """What a merge group looks like for the cross-bucket stage, or None when
    it is not eligible (on-site / hybrid, or mixed country buckets)."""
    buckets = {j.country_bucket for j in group if (j.country_bucket or "unknown") != "unknown"}
    if len(buckets) > 1:
        return None
    work = {(j.workplace_type or "unknown").lower() for j in group}
    if work == {REMOTE}:
        kind = "board_remote" if not any(is_ats(j.source) for j in group) else "remote"
    elif work == {"unknown"} and any(is_ats(j.source) for j in group):
        kind = "ats_unknown"
    else:
        return None
    return {"kind": kind, "bucket": next(iter(buckets), "unknown"),
            "placeless": not any(has_place_signal(j) for j in group)}


def _cross_match(a: Optional[dict[str, Any]], b: Optional[dict[str, Any]]) -> bool:
    if a is None or b is None:
        return False
    kinds = {a["kind"], b["kind"]}
    if kinds <= {"remote", "board_remote"}:
        # remote + remote: only an unknown country joining a known one (equal
        # buckets were already merged by key, or are same-source on purpose)
        if (a["bucket"] == "unknown") == (b["bucket"] == "unknown"):
            return False
    elif kinds != {"ats_unknown", "board_remote"}:
        return False      # ATS 'unknown' only ever matches a board's remote copy
    if a["bucket"] == b["bucket"]:
        return True
    unknown = a if a["bucket"] == "unknown" else b if b["bucket"] == "unknown" else None
    return bool(unknown and unknown["placeless"])


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
    def blocked(ra: int, rb: int) -> bool:
        return regions_conflict((jobs[m] for m in members[ra]), (jobs[m] for m in members[rb]))

    # 2) same company + title + location bucket, across sources only, never
    # across conflicting region restrictions
    by_canon: dict[str, list[int]] = {}
    for i, j in enumerate(jobs):
        by_canon.setdefault(canonical_key(j), []).append(i)
    for idxs in by_canon.values():
        for i in idxs[1:]:
            for first in idxs:
                if first == i:
                    break
                if find(first) != find(i) and not blocked(find(first), find(i)):
                    union(first, i, distinct_sources=True)

    # 3) the matches a single key can't express, made only when unambiguous
    # (each side has exactly one candidate):
    #   * a remote role with an unknown country joining one IN / non-IN group;
    #   * an employer ATS posting with workplace 'unknown' (common on Greenhouse
    #     and Workday) and its remote board copy in the same country bucket.
    by_role: dict[tuple[str, str], set[int]] = {}
    for i, j in enumerate(jobs):
        by_role.setdefault((normalize_company_name(j.company), normalize_title(j.title)), set()).add(i)
    for idxs in by_role.values():
        roots = sorted({find(i) for i in idxs})
        if len(roots) < 2:
            continue
        info = {r: _component_info([jobs[m] for m in members[r]]) for r in roots}
        cands: dict[int, set[int]] = {r: set() for r in roots}
        for x, a in enumerate(roots):
            for b in roots[x + 1:]:
                if (_cross_match(info[a], info[b]) and not (srcs(a) & srcs(b))
                        and not blocked(a, b)):
                    cands[a].add(b)
                    cands[b].add(a)
        for a in roots:
            if len(cands[a]) == 1:
                (b,) = cands[a]
                if len(cands[b]) == 1:
                    union(a, b, distinct_sources=True)

    out = [_merge_group([jobs[m] for m in sorted(members[r])]) for r in sorted(members)]
    if len(out) != len(jobs):
        log.info("dedupe merged %d records into %d jobs", len(jobs), len(out))
    return out
