"""Bangalore Startup Map: daily company refresh (D12).

Per run: ONE conditional GET of the homepage. Its Next.js RSC payload
(`self.__next_f.push([1, "..."])` chunks) holds every startup (name, slug,
stage, sector, website) and a hiring map `roles{slug: [...]}`. Company pages
(`/company/<slug>`) are fetched only for hiring companies whose role list
changed since the last run, spaced >= company_page_delay_sec apart and capped
per run; the rest are deferred to the next run. A company page's role ids
("greenhouse:<board>:<id>") reveal the ATS — stored as an unverified hint for
the detector to confirm. `/api/` is never called (robots-disallowed) and their
role feed is never mirrored: jobs come from the ATS APIs.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from typing import Any, Callable, Optional
from urllib.parse import urljoin

from .. import companies as co
from .. import config
from ..http import HttpError, PoliteClient, default_client

log = logging.getLogger("jobcopilot.discovery.bsm")

SOURCE = "bsm"
DEFAULT_URL = "https://bangalorestartupmap.com/"
DEFAULT_MAX_COMPANY_PAGES = 40
DEFAULT_DELAY_SEC = 5.0

_PUSH_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)\s*</script>", re.S)
# Role ids look like "greenhouse:razorpaysoftwareprivatelimited:4749341005".
_ROLE_ID_RE = re.compile(r'"id"\s*:\s*"([a-z]+):([A-Za-z0-9_.\-]+):[^"]*"')
_KNOWN_ATS = frozenset({"greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable",
                        "recruitee", "gem", "breezy", "keka", "darwinbox", "zoho", "turbohire"})
_WORKDAY_RE = re.compile(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)")


def _undef(v: Any) -> Any:
    """RSC encodes JS undefined as the string '$undefined'."""
    return None if (v is None or (isinstance(v, str) and (v.startswith("$") or not v.strip()))) else v


def rsc_payload(html: str) -> str:
    """Concatenate the string chunks of every self.__next_f.push([1, "..."]) call."""
    parts: list[str] = []
    for raw in _PUSH_RE.findall(html):
        try:
            arr = json.loads(raw)
        except ValueError:
            continue
        if len(arr) > 1 and isinstance(arr[1], str):
            parts.append(arr[1])
    return "".join(parts)


def parse_home(html: str) -> tuple[list[dict], dict[str, list]]:
    """(startups, roles map) from the homepage. Raises ValueError if the shape changed."""
    payload = rsc_payload(html)
    props = None
    dec = json.JSONDecoder()
    for m in re.finditer(r'"startups"\s*:\s*\[', payload):
        # walk back to the '{' that opens the props object holding "startups"
        start = payload.rfind("{", 0, m.start())
        while start >= 0:
            try:
                obj, _ = dec.raw_decode(payload, start)
                if isinstance(obj, dict) and isinstance(obj.get("startups"), list):
                    props = obj
                    break
            except ValueError:
                pass
            start = payload.rfind("{", 0, start)
        if props:
            break
    if not props:
        raise ValueError("bsm: no startups[] in the homepage payload (site layout changed?)")
    roles = props.get("roles") if isinstance(props.get("roles"), dict) else {}
    return [s for s in props["startups"] if isinstance(s, dict)], roles


def company_fields(s: dict) -> Optional[dict]:
    """One startup entry -> upsert_company fields (None for VCs / nameless rows)."""
    name = (_undef(s.get("name")) or "").strip()
    if not name or (s.get("kind") and s.get("kind") != "startup"):
        return None
    sector = _undef(s.get("sector"))
    notes = "; ".join(p for p in (
        f"bsm: {_undef(s.get('tagline'))}" if _undef(s.get("tagline")) else "",
        f"sector: {sector}" if sector else "",
        f"area: {_undef(s.get('area'))}" if _undef(s.get("area")) else "",
    ) if p)
    return {
        "name": name,
        "website": _undef(s.get("website")),
        "funding_stage": _undef(s.get("stage")),
        "sector": "fintech" if sector and "fintech" in str(sector).lower() else "tech",
        "hq_location": "Bengaluru",
        "notes": notes or None,
    }


def parse_company_page(html: str) -> Optional[dict]:
    """ATS hint from a company page's role ids: {ats, ats_slug, [ats_region | workday parts],
    ats_evidence} for the most common ATS board, or None."""
    payload = rsc_payload(html) or html
    hits = Counter((a, s) for a, s in _ROLE_ID_RE.findall(payload) if a in _KNOWN_ATS)
    if not hits:
        return None
    (ats, slug), n = hits.most_common(1)[0]
    hint: dict[str, Any] = {"ats": ats, "ats_slug": slug,
                            "ats_evidence": f"bsm role id {ats}:{slug} ({n} role(s))"}
    if ats == "greenhouse" and re.search(r"(?:job-)?boards\.eu\.greenhouse\.io/" + re.escape(slug), payload):
        hint["ats_region"] = "eu"
    if ats == "workday":
        m = _WORKDAY_RE.search(payload)
        if m:
            hint.update(ats_tenant=m.group(1), ats_wd_server=m.group(2), ats_site=m.group(3))
    return hint


def role_hashes(roles: dict[str, list]) -> dict[str, str]:
    return {slug: co.content_hash(v) for slug, v in roles.items() if v}


def run(*, client: Optional[PoliteClient] = None, db_path: Any = None,
        max_company_pages: int = DEFAULT_MAX_COMPANY_PAGES, delay_sec: Optional[float] = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
        force: bool = False) -> dict:
    """Daily refresh. Returns counts; errors are logged and reported, never raised."""
    cfg = (config.sources().get("discovery") or {}).get("bangalore_startup_map") or {}
    if not cfg.get("enabled", True) and not force:
        return {"source": SOURCE, "skipped": "disabled in config/sources.yaml"}
    base = cfg.get("url") or DEFAULT_URL
    delay = float(delay_sec if delay_sec is not None
                  else cfg.get("company_page_delay_sec", DEFAULT_DELAY_SEC))
    client = client or default_client()
    st = co.get_source_state(SOURCE, db_path)
    state = st.get("state") or {}
    hashes: dict[str, str] = dict(state.get("hashes") or {})
    pending: dict[str, str] = dict(state.get("pending") or {})
    names: dict[str, str] = dict(state.get("names") or {})
    now = time.time()
    out: dict[str, Any] = {"source": SOURCE, "unchanged": False, "upserted": 0,
                           "pages_fetched": 0, "ats_hints": 0, "deferred": 0}

    # 1) homepage: one conditional GET
    validators: dict[str, Any] = {}
    digest = st.get("content_hash")
    try:
        resp = client.get(base, headers=(co.conditional_headers(st) or None) if not force else None)
        validators = {"etag": resp.headers.get("ETag") or st.get("etag"),
                      "last_modified": resp.headers.get("Last-Modified") or st.get("last_modified")}
        if resp.status_code == 304:
            out["unchanged"] = True
        else:
            startups, roles = parse_home(resp.text)
            digest = co.content_hash({"s": startups, "r": roles})
            if digest == st.get("content_hash") and not force:
                out["unchanged"] = True
            else:
                for s in startups:
                    f = company_fields(s)
                    if not f:
                        continue
                    nm = f.pop("name")
                    co.upsert_company(nm, db_path=db_path, signal_source=SOURCE, **f)
                    if s.get("slug"):
                        names[str(s["slug"])] = nm
                    out["upserted"] += 1
                for slug, h in role_hashes(roles).items():
                    if hashes.get(slug) != h:
                        pending[slug] = h
    except (HttpError, ValueError) as exc:
        log.error("bsm homepage failed: %s", exc)
        co.put_source_state(SOURCE, db_path, last_run=now, last_error=str(exc)[:500])
        out["error"] = str(exc)
        return out

    # 2) company pages for new / changed hiring companies only, spaced and capped
    todo = sorted(pending)
    last = None
    errors = 0
    for slug in todo[:max(0, int(max_company_pages))]:
        if last is not None:
            wait = delay - (clock() - last)
            if wait > 0:
                sleep(wait)
        last = clock()
        try:
            page = client.get(urljoin(base, f"company/{slug}"))
            hint = parse_company_page(page.text)
        except (HttpError, ValueError) as exc:
            errors += 1
            log.warning("bsm company page %s failed: %s", slug, exc)
            continue
        out["pages_fetched"] += 1
        hashes[slug] = pending.pop(slug)
        if hint and names.get(slug):
            co.upsert_company(names[slug], db_path=db_path, signal_source=SOURCE,
                              ats_confidence=co.UNVERIFIED_CONFIDENCE, ats_checked_at=now, **hint)
            out["ats_hints"] += 1
    out["deferred"] = len(pending)
    if pending:
        log.info("bsm: %d company page(s) deferred to the next run (cap %d): %s",
                 len(pending), max_company_pages, ", ".join(sorted(pending)[:10]))
    if errors:
        out["page_errors"] = errors
    if out["upserted"] or out["ats_hints"]:
        out["tiers"] = co.recompute_tiers(db_path)
    co.put_source_state(SOURCE, db_path, content_hash=digest, last_run=now, last_ok=now,
                        last_error=None, items=out["upserted"] or st.get("items"),
                        state={"hashes": hashes, "pending": pending, "names": names}, **validators)
    return out
