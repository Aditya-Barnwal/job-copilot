"""Daily queue ("Today's best"), all matches, the excluded view and the tracker.

Reads what the pipeline stored (eligibility decision + v2 fit score per job)
and answers the questions the UI asks:

  * build_daily_queue(): 15-25 main-lane jobs ranked by fit, at most N per
    company, never padded with excluded jobs; a short "near-miss" list shown
    apart; the extended (watchlist) lane gets its own top-N. Persisted per day
    in queue_items, idempotently.
  * list_matches(): every eligible job, paginated (the queue is just the top).
  * list_excluded(): excluded jobs with their reasons (D8).
  * tracker(): status columns with dates, the ready-to-submit list with
    reminders and expiry warnings, and follow-ups due after submission.

Jobs the user submitted (or later) or dismissed never appear in the queue or
in matches. Scoring (fit.score) and re-evaluation of legacy rows are here too
so the nightly run can catch up on anything the ingest step missed.
Knobs live under `queue:` in config/filters.yaml; defaults in QUEUE_DEFAULTS.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from typing import Any, Iterable, Optional

from . import config as _config
from . import store

log = logging.getLogger("jobcopilot.queue")

QUEUE_DEFAULTS: dict[str, Any] = {
    "daily_target_min": 15,
    "daily_target_max": 25,
    "max_per_company": 3,
    "extended_top_n": 15,
    "min_fit_score": 0,            # main-lane jobs below this go to near-miss, not the queue
    "near_miss_max": 10,
    "near_miss_rules": ["yoe", "salary"],   # exclusions counted as "soft" for near-miss
    "ready_reminder_days": 2,
    "followup_days": 7,
    "gone_after_days": 3,          # not seen by ingest for this long -> posting probably gone
    "reapply_cooldown_days": 90,   # after a rejection, wait this long before applying there again (L#6)
}

# Hidden from Today's best and All matches: submitted or later, or dismissed.
HIDDEN_CANONICAL = frozenset(store.SUBMITTED_OR_LATER | {"not_interested"})

_SCORE_SQL = f"COALESCE(j.fit_score_v2, {store._FIT_SQL})"
_LIVE_SQL = "j.gone_at IS NULL"     # store.retire_unlisted: every source stopped listing it


def _age_sql(now: float) -> str:
    """Posting age in days NOW (SQL twin of _age_days, for ordering): from an
    absolute posted_at when SQLite can parse it; else the stored posted_age_days
    (computed when the row was last seen) aged by the time since; else first_seen."""
    n = repr(float(now))
    jd = repr(float(now) / 86400.0 + 2440587.5)      # julian day of `now`
    return (f"(CASE WHEN julianday(j.posted_at) IS NOT NULL "
            f"THEN MAX(0, {jd} - julianday(j.posted_at)) "
            f"WHEN j.posted_age_days IS NOT NULL "
            f"THEN j.posted_age_days + MAX(0, {n} - COALESCE(j.last_seen, {n})) / 86400.0 "
            f"WHEN j.first_seen IS NOT NULL THEN MAX(0, {n} - j.first_seen) / 86400.0 END)")


def _fresh_sql(now: float) -> str:
    a = _age_sql(now)
    return (f"(CASE WHEN {a} IS NULL THEN 4 WHEN {a} <= 30 THEN 0 WHEN {a} <= 90 THEN 1 "
            f"WHEN {a} <= 180 THEN 2 ELSE 3 END)")


def _parse_posted(posted_at: Any) -> Optional[float]:
    """An absolute posted_at (ISO date/datetime) as epoch seconds; None for
    relative strings ('Posted 3 Days Ago') or anything unparseable."""
    if not posted_at:
        return None
    s = str(posted_at).strip()
    if "posted" in s.lower() or not s[:1].isdigit():
        return None
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()


def _age_days(row: dict, now: float) -> Optional[int]:
    """Age at read time: from an absolute posted_at; else the stored age aged
    since the row was last seen (relative Workday strings); else first_seen."""
    ts = _parse_posted(row.get("posted_at"))
    if ts is not None:
        return max(0, int((now - ts) // 86400))
    stored, seen = row.get("posted_age_days"), row.get("last_seen")
    if stored is not None:
        drift = max(0.0, now - float(seen)) if seen else 0.0
        return int(stored) + int(drift // 86400)
    if row.get("first_seen"):
        return max(0, int((now - float(row["first_seen"])) // 86400))
    return None
_SELECT = ("SELECT j.*, c.tier AS company_tier, " + _SCORE_SQL + " AS rank_score "
           "FROM jobs j LEFT JOIN companies c ON c.id = j.company_id")


def queue_config(cfg: Optional[dict] = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else _config.filters()
    return {**QUEUE_DEFAULTS, **(cfg.get("queue") or {})}


def today(now: Optional[float] = None) -> str:
    """YYYY-MM-DD in the schedule's time zone (config/schedule.yaml)."""
    tzname = _config.schedule().get("timezone") or "Asia/Kolkata"
    try:
        from zoneinfo import ZoneInfo
        tz: Any = ZoneInfo(tzname)
    except Exception:  # noqa: BLE001 — unknown zone / no tzdata: fall back to local time
        tz = None
    return _dt.datetime.fromtimestamp(time.time() if now is None else now, tz).strftime("%Y-%m-%d")


def _hidden_statuses() -> list[str]:
    return store.statuses_with_canonical(HIDDEN_CANONICAL)


def _placeholders(items: list) -> str:
    return ", ".join("?" * len(items))


def _num(v: Any) -> Optional[float]:
    return None if v is None else round(float(v), 1)


def summarize_job(row: dict, now: Optional[float] = None) -> dict[str, Any]:
    """The card fields the queue / matches / tracker views share."""
    now = time.time() if now is None else now
    store.decode_v3(row)
    from .normalize import freshness_bucket
    age = _age_days(row, now)
    comps = row.get("fit_components") or {}
    return {
        "key": row["key"], "title": row.get("title"), "company": row.get("company"),
        "company_id": row.get("company_id"), "tier": row.get("company_tier"),
        "lane": row.get("lane"), "status": row.get("status") or "new",
        "tracker_status": store.canonical_status(row.get("status")),
        "location": row.get("location_text"), "workplace_type": row.get("workplace_type"),
        "country_bucket": row.get("country_bucket"),
        "source": row.get("source"), "attribution_name": row.get("attribution_name"),
        "attribution_url": row.get("attribution_url"), "url": row.get("url"),
        "apply_url": row.get("apply_url"),
        "age_days": age, "freshness": freshness_bucket(age), "expires_at": row.get("expires_at"),
        "posting_gone": row.get("gone_at") is not None, "gone_at": row.get("gone_at"),
        "fit_score": _num(row.get("rank_score", row.get("fit_score_v2"))),
        "fit_score_v2": _num(row.get("fit_score_v2")),
        "fit_components": comps.get("components") if isinstance(comps, dict) else None,
        "why": row.get("fit_why"),
        "matched_skills": row.get("matched_skills") or [],
        "missing_skills": row.get("missing_skills") or [],
        "yoe": {"min": row.get("min_yoe_required"), "max": row.get("yoe_max"),
                "preferred": bool(row.get("yoe_is_preferred")), "evidence": row.get("yoe_evidence")},
        "fresher_friendly": bool(row.get("fresher_friendly")),
        "india_eligible": None if row.get("india_eligible") is None else bool(row["india_eligible"]),
        "salary_annual_inr": row.get("salary_annual_inr"),
        "comp_summary": row.get("comp_summary"),
        "tz_requirement": row.get("tz_requirement_text"),
        "timezone_restrictions": _json_list(row.get("timezone_restrictions")),
        "region_restrictions": _json_list(row.get("region_restrictions")),
        "flags": row.get("flags") or [],
    }


def _json_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            out = json.loads(v)
            return out if isinstance(out, list) else []
        except ValueError:
            return []
    return []


# --- catch-up: evaluate legacy rows, score what is unscored ------------------

def evaluate_pending(*, db_path: Any = None, cfg: Optional[dict] = None,
                     limit: Optional[int] = None) -> dict[str, Any]:
    """Run eligibility on every stored, still-listed row whose decision is
    missing (rows stored before Phase 2, fallback decisions) or was made with a
    different filters config (eval_cfg_hash != hash of the current one)."""
    try:
        from .eligibility import evaluate
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"eligibility unavailable ({type(exc).__name__})", "evaluated": 0}
    from .ingest import _decision_row, eligibility_config
    try:
        from .companies import get_company
    except Exception:  # noqa: BLE001
        get_company = None  # type: ignore[assignment]
    cfg = cfg if cfg is not None else eligibility_config()
    cfg_hash = store.config_hash(cfg)
    sql = ("SELECT * FROM jobs WHERE gone_at IS NULL AND (lane IS NULL OR eval_cfg_hash IS NOT ?) "
           "ORDER BY last_seen DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with store.connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql, (cfg_hash,)).fetchall()]
    out = {"evaluated": 0, "errors": 0, "excluded": 0, "config_hash": cfg_hash}
    cache: dict[str, Optional[dict]] = {}
    for row in rows:
        store._decode_json_lists(row)
        name = row.get("company") or ""
        if name not in cache:
            try:
                cache[name] = get_company(name, db_path=db_path) if get_company else None
            except Exception:  # noqa: BLE001
                cache[name] = None
        try:
            ev = _decision_row(evaluate(row, company=cache[name], cfg=cfg))
            ev["eval_cfg_hash"] = cfg_hash
            store.set_evaluation(row["key"], ev, db_path)
        except Exception as exc:  # noqa: BLE001 — one bad row never aborts
            out["errors"] += 1
            log.warning("evaluate_pending key=%s exc=%s", row.get("key"), exc)
            continue
        out["evaluated"] += 1
        out["excluded"] += ev["excluded"]
    return out


def stored_decision(row: dict) -> dict[str, Any]:
    """The eligibility Decision as stored on a jobs row (fit.score takes a dict),
    so YOE / fresher / India / salary come from eligibility, not re-derived."""
    def opt_bool(v: Any) -> Optional[bool]:
        return None if v is None else bool(v)
    return {"lane": row.get("lane"),
            "reasons": list(row.get("exclusion_reasons") or []) + list(row.get("flags") or []),
            "yoe_min": row.get("min_yoe_required"), "yoe_max": row.get("yoe_max"),
            "yoe_is_preferred": bool(row.get("yoe_is_preferred")),
            "yoe_evidence": row.get("yoe_evidence"),
            "fresher_friendly": bool(row.get("fresher_friendly")),
            "india_eligible": opt_bool(row.get("india_eligible")),
            "salary_ok": None, "salary_annual_inr": row.get("salary_annual_inr")}


def _fit_row(result: Any) -> dict[str, Any]:
    return {
        "fit_score_v2": round(float(result.score), 1),
        "fit_components": {"components": dict(getattr(result, "components", {}) or {}),
                           "weights": dict(getattr(result, "weights", {}) or {}),
                           "flags": list(getattr(result, "flags", []) or []),
                           "role_family": getattr(result, "role_family", None)},
        "matched_skills": list(getattr(result, "matched_skills", []) or []),
        "missing_skills": list(getattr(result, "missing_skills", []) or []),
        "fit_why": getattr(result, "why", None),
    }


def score_pending(keys: Optional[Iterable[str]] = None, *, db_path: Any = None,
                  profile: Any = None, cfg: Optional[dict] = None) -> dict[str, Any]:
    """fit.score every non-excluded job in `keys`, or (keys=None) every
    non-excluded job not scored since its last evaluation. One Profile per
    call (fit caches the résumé embedding on it); the company row and the
    stored eligibility decision are passed through."""
    try:
        from . import fit as _fit
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"fit unavailable ({type(exc).__name__})", "scored": 0}
    if profile is None:
        profile = _fit.load_profile()
    base = ("SELECT j.*, c.tier AS company_tier, c.funding_stage AS c_funding_stage, "
            "c.yc_batch AS c_yc_batch, c.yc_top_company AS c_yc_top_company FROM jobs j "
            "LEFT JOIN companies c ON c.id = j.company_id WHERE COALESCE(j.excluded, 0) = 0 "
            "AND j.lane IS NOT NULL")
    with store.connect(db_path) as conn:
        if keys is None:
            rows = conn.execute(base + " AND (j.scored_at IS NULL OR "
                                "j.scored_at < COALESCE(j.evaluated_at, 0))").fetchall()
        else:
            wanted = list(dict.fromkeys(keys))
            rows = []
            for i in range(0, len(wanted), 500):
                chunk = wanted[i:i + 500]
                rows += conn.execute(base + f" AND j.key IN ({_placeholders(chunk)})", chunk).fetchall()
    out = {"scored": 0, "errors": 0}
    for r in rows:
        job = store.decode_v3(store._decode_json_lists(dict(r)))
        job["tier"] = job.get("company_tier")
        company = None
        if job.get("company_id") is not None and job.get("company_tier") is not None:
            company = {"tier": job["company_tier"], "funding_stage": job.pop("c_funding_stage", None),
                       "yc_batch": job.pop("c_yc_batch", None),
                       "yc_top_company": job.pop("c_yc_top_company", None)}
        try:
            result = _fit.score(job, profile=profile, cfg=cfg, company=company,
                                decision=stored_decision(job))
            store.set_fit(job["key"], _fit_row(result), db_path)
            out["scored"] += 1
        except Exception as exc:  # noqa: BLE001
            out["errors"] += 1
            log.warning("fit failed key=%s exc=%s", job.get("key"), exc)
    return out


# --- the daily queue ----------------------------------------------------------

def _company_key(row: dict) -> str:
    return f"id:{row['company_id']}" if row.get("company_id") is not None else \
        "name:" + (row.get("company") or "").strip().lower()


def _candidates(conn: Any, lane: str, day: str, hidden: list[str], now: float) -> list[dict]:
    sql = (_SELECT + f" WHERE COALESCE(j.excluded, 0) = 0 AND j.lane = ? AND {_LIVE_SQL} "
           f"AND COALESCE(j.status, 'new') NOT IN ({_placeholders(hidden)}) "
           "AND (j.expires_at IS NULL OR j.expires_at = '' OR substr(j.expires_at, 1, 10) >= ?) "
           f"ORDER BY rank_score IS NULL, rank_score DESC, {_fresh_sql(now)}, j.last_seen DESC")
    return [dict(r) for r in conn.execute(sql, (lane, *hidden, day)).fetchall()]


def _pick(rows: list[dict], top: int, cap: int, min_fit: float) -> tuple[list[dict], list[dict]]:
    """(picked, near-misses): ranked rows up to `top`, <= cap per company."""
    picked: list[dict] = []
    near: list[dict] = []
    per: dict[str, int] = {}
    for r in rows:
        if len(picked) >= top:
            break
        score = r.get("rank_score")
        if min_fit and (score is None or score < min_fit):
            near.append({**r, "near_miss_reason": f"fit below {min_fit:g}"})
            continue
        ck = _company_key(r)
        if per.get(ck, 0) >= cap:
            near.append({**r, "near_miss_reason": f"company already has {cap} in the queue"})
            continue
        per[ck] = per.get(ck, 0) + 1
        picked.append(r)
    return picked, near


def _soft_excluded(conn: Any, qcfg: dict, cfg: dict, hidden: list[str], now: float) -> list[dict]:
    soft = tuple(str(x) for x in qcfg["near_miss_rules"])
    max_req = int(((cfg.get("yoe") or {}).get("max_required", 2)))
    sql = (_SELECT + f" WHERE j.excluded = 1 AND j.lane = 'excluded' AND {_LIVE_SQL} "
           f"AND COALESCE(j.status, 'new') NOT IN ({_placeholders(hidden)}) "
           f"ORDER BY rank_score IS NULL, rank_score DESC, {_fresh_sql(now)}, j.last_seen DESC LIMIT 2000")
    out = []
    for r in conn.execute(sql, hidden).fetchall():
        row = store.decode_v3(dict(r))
        rules = [str(x.get("rule") or "") for x in row.get("exclusion_reasons") or []]
        if not rules or not all(rule.startswith(soft) for rule in rules):
            continue
        if any(rule.startswith("yoe") for rule in rules):
            if row.get("min_yoe_required") is None or row["min_yoe_required"] > max_req + 1:
                continue
        row["near_miss_reason"] = "excluded only by: " + ", ".join(sorted(set(rules)))
        out.append(row)
    return out


def build_daily_queue(day: Optional[str] = None, cfg: Optional[dict] = None,
                      db_path: Any = None) -> dict[str, Any]:
    """Build and persist today's queue; safe to re-run (ranks update, statuses stay)."""
    cfg = cfg if cfg is not None else _config.filters()
    q = queue_config(cfg)
    day = day or today()
    tmin, tmax = int(q["daily_target_min"]), int(q["daily_target_max"])
    cap, min_fit = int(q["max_per_company"]), float(q["min_fit_score"] or 0)
    hidden = _hidden_statuses()
    companies_cfg = cfg.get("companies") or {}
    extended_on = bool(companies_cfg.get("include_extended", True))
    now = time.time()
    with store.connect(db_path) as conn:
        main, near = _pick(_candidates(conn, "main", day, hidden, now), tmax, cap, min_fit)
        extended: list[dict] = []
        if extended_on:
            extended, _ = _pick(_candidates(conn, "extended", day, hidden, now),
                                int(q["extended_top_n"]), cap, 0)
        near += _soft_excluded(conn, q, cfg, hidden, now)
    near.sort(key=lambda r: (r.get("rank_score") is None, -(r.get("rank_score") or 0)))
    near = near[: int(q["near_miss_max"])]

    items = [(r["key"], "main", i + 1, r.get("rank_score")) for i, r in enumerate(main)]
    items += [(r["key"], "extended", i + 1, r.get("rank_score")) for i, r in enumerate(extended)]
    taken = {k for k, *_ in items}
    items += [(r["key"], "near_miss", i + 1, r.get("rank_score"))
              for i, r in enumerate(near) if r["key"] not in taken]
    written = store.replace_queue(day, items, db_path)

    def cards(rows: list[dict]) -> list[dict]:
        out = []
        for i, r in enumerate(rows):
            c = summarize_job(dict(r), now)
            c["rank"] = i + 1
            if r.get("near_miss_reason"):
                c["near_miss_reason"] = r["near_miss_reason"]
            out.append(c)
        return out

    shortfall = max(0, tmin - len(main))
    if shortfall:
        log.info("queue day=%s has %d main jobs, %d short of the %d target", day, len(main), shortfall, tmin)
    return {"day": day, "target": {"min": tmin, "max": tmax, "max_per_company": cap},
            "main": cards(main), "extended": cards(extended), "near_miss": cards(near),
            "shortfall": shortfall,
            "counts": {"main": len(main), "extended": len(extended), "near_miss": len(near)},
            "persisted": written}


def list_queue(day: Optional[str] = None, lane: Optional[str] = "main",
               db_path: Any = None) -> dict[str, Any]:
    """The stored queue for `day` (default: the latest built day). Jobs submitted
    or dismissed since the queue was built, and postings no longer listed, are left out."""
    if day is None:
        days = store.queue_days(db_path)
        day = days[0] if days else None
    if day is None:
        return {"day": None, "lane": lane, "items": [], "count": 0}
    hidden = _hidden_statuses()
    sql = (_SELECT.replace("SELECT j.*", "SELECT q.lane AS queue_lane, q.rank AS queue_rank, "
                           "q.status AS queue_status, q.fit_score AS queue_fit, j.*")
           + f" JOIN queue_items q ON q.job_key = j.key WHERE q.day = ? AND q.rank IS NOT NULL AND {_LIVE_SQL} "
           f"AND COALESCE(j.status, 'new') NOT IN ({_placeholders(hidden)})")
    args: list[Any] = [day, *hidden]
    if lane:
        sql += " AND q.lane = ?"
        args.append(lane)
    sql += " ORDER BY q.lane, q.rank"
    with store.connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    items = []
    for r in rows:
        c = summarize_job(r)
        c.update(rank=r["queue_rank"], queue_lane=r["queue_lane"], queue_status=r["queue_status"])
        items.append(c)
    return {"day": day, "lane": lane, "items": items, "count": len(items)}


# --- all matches / excluded / fit ---------------------------------------------

def _like(value: str) -> str:
    return "%" + value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_matches(*, lane: Optional[str] = "main", sort: str = "fit", q: Optional[str] = None,
                 location: Optional[str] = None, source: Optional[str] = None,
                 tier: Optional[str] = None, limit: int = 50, offset: int = 0,
                 db_path: Any = None) -> dict[str, Any]:
    """Every eligible (non-excluded) job, paginated; submitted+/dismissed left out."""
    hidden = _hidden_statuses()
    where = ["COALESCE(j.excluded, 0) = 0", "j.lane IS NOT NULL", "j.lane != 'excluded'", _LIVE_SQL,
             f"COALESCE(j.status, 'new') NOT IN ({_placeholders(hidden)})"]
    args: list[Any] = list(hidden)
    if lane and lane != "all":
        where.append("j.lane = ?"); args.append(lane)
    if q:
        where.append("(LOWER(j.title) LIKE ? ESCAPE '\\' OR LOWER(j.company) LIKE ? ESCAPE '\\')")
        args += [_like(q), _like(q)]
    if location:
        where.append("LOWER(COALESCE(j.location_text, '')) LIKE ? ESCAPE '\\'"); args.append(_like(location))
    if source:
        where.append("(j.source = ? OR EXISTS (SELECT 1 FROM job_sources s "
                     "WHERE s.job_key = j.key AND s.source = ?))")
        args += [source, source]
    if tier:
        where.append("c.tier = ?"); args.append(tier)
    now = time.time()
    order = (f"COALESCE({_age_sql(now)}, 99999), j.first_seen DESC, rank_score DESC"
             if sort == "age" else
             f"rank_score IS NULL, rank_score DESC, {_fresh_sql(now)}, j.last_seen DESC")
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    clause = " WHERE " + " AND ".join(where)
    with store.connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM jobs j LEFT JOIN companies c ON c.id = j.company_id"
                             + clause, args).fetchone()[0]
        rows = [dict(r) for r in conn.execute(_SELECT + clause + f" ORDER BY {order} LIMIT ? OFFSET ?",
                                              (*args, limit, offset)).fetchall()]
    return {"total": int(total), "limit": limit, "offset": offset, "sort": sort, "lane": lane,
            "items": [summarize_job(r, now) for r in rows]}


def list_excluded(rule: Optional[str] = None, limit: int = 100, offset: int = 0,
                  db_path: Any = None) -> dict[str, Any]:
    """Excluded jobs with every reason (D8), newest first, plus counts per rule."""
    limit = max(1, min(int(limit), 1000))
    counts: dict[str, int] = {}
    items: list[dict] = []
    matched = 0
    with store.connect(db_path) as conn:
        for r in conn.execute(_SELECT + " WHERE j.excluded = 1 ORDER BY j.last_seen DESC, j.key"):
            row = dict(r)
            reasons = store.decode_v3({"exclusion_reasons": row.get("exclusion_reasons")})["exclusion_reasons"]
            rules = {str(x.get("rule") or "?") for x in reasons}
            for x in rules:
                counts[x] = counts.get(x, 0) + 1
            if rule and rule not in rules:
                continue
            matched += 1
            if matched <= offset or len(items) >= limit:
                continue
            card = summarize_job(row)
            card["reasons"] = reasons
            items.append(card)
    return {"rule": rule, "total": matched, "limit": limit, "offset": offset,
            "by_rule": dict(sorted(counts.items(), key=lambda kv: -kv[1])), "items": items}


def job_fit(key: str, db_path: Any = None) -> Optional[dict[str, Any]]:
    """Fit components + weights + skills for one job (None if absent)."""
    with store.connect(db_path) as conn:
        r = conn.execute("SELECT key, lane, excluded, exclusion_reasons, flags, fit_score_v2, "
                         "fit_components, matched_skills, missing_skills, fit_why, scored_at "
                         "FROM jobs WHERE key = ?", (key,)).fetchone()
    if not r:
        return None
    row = store.decode_v3(dict(r))
    comps = row.get("fit_components") or {}
    return {"key": key, "lane": row.get("lane"), "excluded": bool(row.get("excluded")),
            "exclusion_reasons": row.get("exclusion_reasons"), "flags": row.get("flags"),
            "fit_score": _num(row.get("fit_score_v2")), "components": comps.get("components") or {},
            "weights": comps.get("weights") or {}, "fit_flags": comps.get("flags") or [],
            "matched_skills": row.get("matched_skills"),
            "missing_skills": row.get("missing_skills"), "why": row.get("fit_why"),
            "scored_at": row.get("scored_at")}


# --- tracker ------------------------------------------------------------------

def _history_dates(conn: Any, keys: list[str]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        for h in conn.execute(f"SELECT job_key, status, MAX(at) AS at FROM status_history "
                              f"WHERE job_key IN ({_placeholders(chunk)}) GROUP BY job_key, status", chunk):
            out.setdefault(h["job_key"], {})[store.canonical_status(h["status"])] = h["at"]
    return out


def _days(now: float, since: Optional[float]) -> Optional[int]:
    return None if since is None else max(0, int((now - float(since)) // 86400))


def ready_to_submit(now: Optional[float] = None, cfg: Optional[dict] = None,
                    db_path: Any = None) -> list[dict]:
    """Jobs waiting in ready_to_submit: days waiting, a reminder after N days,
    and a warning when the posting expired or stopped being listed (gone_at:
    every source that listed it ran successfully since without it)."""
    now = time.time() if now is None else now
    q = queue_config(cfg)
    statuses = store.statuses_with_canonical(["ready_to_submit"])
    day = today(now)
    with store.connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            _SELECT + f" WHERE j.status IN ({_placeholders(statuses)}) ORDER BY j.status_changed_at",
            statuses).fetchall()]
        hist = _history_dates(conn, [r["key"] for r in rows])
    out = []
    for r in rows:
        since = r.get("status_changed_at") or hist.get(r["key"], {}).get("ready_to_submit") or r.get("first_seen")
        waiting = _days(now, since)
        expired = bool(r.get("expires_at") and str(r["expires_at"])[:10] < day)
        gone = r.get("gone_at") is not None
        card = summarize_job(r, now)
        card.update(ready_since=since, days_waiting=waiting,
                    reminder_due=waiting is not None and waiting >= int(q["ready_reminder_days"]),
                    posting_expired=expired, posting_gone=gone,
                    warning=("posting expired" if expired else
                             "posting no longer listed" if gone else None))
        out.append(card)
    return out


def followups(now: Optional[float] = None, cfg: Optional[dict] = None,
              db_path: Any = None, due_only: bool = False) -> list[dict]:
    """Submitted jobs (no OA/interview/offer/rejection yet); follow-up is due N days after submit."""
    now = time.time() if now is None else now
    days_after = int(queue_config(cfg)["followup_days"])
    statuses = store.statuses_with_canonical(["submitted"])
    with store.connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            _SELECT + f" WHERE j.status IN ({_placeholders(statuses)}) ORDER BY j.submitted_at",
            statuses).fetchall()]
    out = []
    for r in rows:
        sub = r.get("submitted_at") or r.get("status_changed_at")
        since = _days(now, sub)
        due = since is not None and since >= days_after
        if due_only and not due:
            continue
        card = summarize_job(r, now)
        card.update(submitted_at=sub, submit_source=r.get("submit_source"),
                    submit_proof_path=r.get("submit_proof_path"), days_since_submit=since,
                    followup_due=due, followup_due_at=(float(sub) + days_after * 86400) if sub else None)
        out.append(card)
    return out


def tracker(limit_per_column: int = 200, now: Optional[float] = None,
            cfg: Optional[dict] = None, db_path: Any = None) -> dict[str, Any]:
    """Kanban columns (one per tracker status) with dates. Untouched 'new' jobs
    are not in the tracker — they live in the queue / matches."""
    now = time.time() if now is None else now
    columns = []
    with store.connect(db_path) as conn:
        for col in store.TRACKER_STATUSES:
            statuses = [s for s in store.statuses_with_canonical([col]) if s != "new"]
            args = list(statuses)
            total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE status IN ({_placeholders(statuses)})",
                                 args).fetchone()[0]
            rows = [dict(r) for r in conn.execute(
                _SELECT + f" WHERE j.status IN ({_placeholders(statuses)}) "
                "ORDER BY COALESCE(j.status_changed_at, j.last_seen) DESC LIMIT ?",
                (*args, int(limit_per_column))).fetchall()]
            hist = _history_dates(conn, [r["key"] for r in rows])
            items = []
            for r in rows:
                card = summarize_job(r, now)
                changed = r.get("status_changed_at") or hist.get(r["key"], {}).get(col)
                card.update(status_changed_at=changed, days_in_status=_days(now, changed),
                            submitted_at=r.get("submitted_at"), submit_source=r.get("submit_source"),
                            submit_proof_path=r.get("submit_proof_path"),
                            dates=hist.get(r["key"], {}),
                            warning="posting no longer listed" if r.get("gone_at") is not None else None)
                items.append(card)
            columns.append({"status": col, "count": int(total), "items": items})
    return {"columns": columns,
            "ready_to_submit": ready_to_submit(now, cfg, db_path),
            "followups_due": followups(now, cfg, db_path, due_only=True),
            "cooldowns": reapply_cooldowns(now, cfg, db_path)}


def reapply_cooldowns(now: Optional[float] = None, cfg: Optional[dict] = None,
                      db_path: Any = None) -> list[dict]:
    """Companies that rejected you within `reapply_cooldown_days`: when you can apply
    there again. One row per company (its latest rejection), soonest-free first."""
    now = time.time() if now is None else now
    days = int(queue_config(cfg)["reapply_cooldown_days"])
    if days <= 0:
        return []
    statuses = store.statuses_with_canonical(["rejected"])
    with store.connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT key, company, company_id, title, status_changed_at, last_seen FROM jobs "
            f"WHERE status IN ({_placeholders(statuses)})", statuses).fetchall()]
        hist = _history_dates(conn, [r["key"] for r in rows])
    latest: dict[str, dict] = {}
    for r in rows:
        at = hist.get(r["key"], {}).get("rejected") or r.get("status_changed_at")
        if at is None:
            continue
        co = _company_key(r)
        if co not in latest or at > latest[co]["rejected_at"]:
            latest[co] = {"company": r.get("company"), "key": r["key"], "title": r.get("title"),
                          "rejected_at": float(at)}
    out = []
    for c in latest.values():
        until = c["rejected_at"] + days * 86400
        if until <= now:
            continue
        c.update(reapply_after=until, days_left=max(0, int(-(-(until - now) // 86400))),
                 cooldown_days=days)
        out.append(c)
    return sorted(out, key=lambda c: c["reapply_after"])
