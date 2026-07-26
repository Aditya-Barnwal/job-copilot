"""FastAPI backend for job-copilot.

Phases A+B+C: persistence, REST API, static UI.
Phase D (scoring) and E (tailoring) drop into the placeholder endpoints later.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Body
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from jobcopilot import store
from jobcopilot.filters import FilterConfig, DEFAULT_TARGET_ROLES, apply_filters
from jobcopilot.ingest import load_companies, ingest, setup_logging
from jobcopilot.resume import extract_text as extract_pdf_text
from jobcopilot import scoring, tailor as tailor_mod, llm, versions as versions_mod
from jobcopilot import email_draft


app = FastAPI(title="job-copilot")

DATA_DIR = ROOT / "data"
STATIC_DIR = Path(__file__).resolve().parent / "static"


_COMPANY_META: dict = {}


def _load_company_meta() -> None:
    global _COMPANY_META
    try:
        companies = load_companies(str(ROOT / "companies.yaml"))
        _COMPANY_META = {
            c["name"]: {
                "apply_method": c.get("apply_method"),
                "contact_email": c.get("contact_email"),
                "funded_startup": c.get("funded_startup", False),
            }
            for c in companies if c.get("name")
        }
    except Exception:
        _COMPANY_META = {}


def _enrich(rows: list[dict]) -> list[dict]:
    for r in rows:
        meta = _COMPANY_META.get(r.get("company")) or {}
        for k, v in meta.items():
            if v is not None:
                r[k] = v
    return rows


@app.on_event("startup")
def _startup() -> None:
    setup_logging(DATA_DIR)
    store.init_db()
    _load_company_meta()


# --------- meta + ingest --------------------------------------------------

@app.get("/api/meta")
def get_meta_summary():
    resume = store.get_resume()
    return {
        "last_ingest": store.get_meta("last_ingest"),
        "status_counts": store.status_counts(),
        "resume_loaded": resume is not None,
        "resume_filename": resume.get("filename") if resume else None,
        "version_count": len(store.list_versions()),
    }


@app.post("/api/ingest")
def run_ingest(body: Optional[dict] = Body(default=None)):
    body = body or {}
    companies_path = body.get("companies") or str(ROOT / "companies.yaml")
    companies = load_companies(companies_path)
    jobs = ingest(companies, fixtures_dir=None)

    target_roles = body.get("roles") or list(DEFAULT_TARGET_ROLES)
    sectors = set(body["sectors"]) if body.get("sectors") else None
    config = FilterConfig(
        min_lpa=float(body.get("min_lpa", 15.0)),
        sectors=sectors,
        usd_to_inr=float(body.get("usd_to_inr", 83.0)),
        target_roles=target_roles,
        max_age_days=body.get("max_age_days"),
    )
    results = apply_filters(jobs, config)

    # Part 0: Workday detail fetch for passed jobs lacking a description
    from jobcopilot.ingest import enrich_workday_descriptions
    filled_keys = enrich_workday_descriptions(results, companies)

    inserted, updated = store.upsert_jobs(results)
    store.set_meta("last_ingest", str(time.time()))

    # Re-score any jobs whose description was newly filled (if a resume exists)
    rescored = 0
    if filled_keys and store.get_resume():
        try:
            rescored = scoring.score_keys(filled_keys).get("scored", 0)
        except Exception:
            rescored = 0

    return {
        "ingested_raw": len(jobs),
        "passed": len(results),
        "inserted": inserted,
        "updated": updated,
        "workday_details_filled": len(filled_keys),
        "rescored": rescored,
    }


# --------- jobs -----------------------------------------------------------

@app.get("/api/jobs")
def list_jobs_endpoint(
    status: Optional[str] = None,
    sector: Optional[str] = None,
    country_bucket: Optional[str] = None,
    workplace_type: Optional[str] = None,
    role_family: Optional[str] = None,
    freshness: Optional[str] = None,
    q: Optional[str] = None,
    min_score: Optional[float] = None,
    max_age_days: Optional[int] = None,
    hide_senior_high: bool = False,
    max_yoe: Optional[int] = None,
    limit: int = 500,
):
    rows = store.list_jobs(
        status=status, sector=sector, country_bucket=country_bucket,
        workplace_type=workplace_type, role_family=role_family,
        freshness=freshness, q=q, min_score=min_score,
        max_age_days=max_age_days, hide_senior_high=hide_senior_high,
        limit=limit,
    )
    if max_yoe is not None:
        rows = [
            r for r in rows
            if (r.get("min_yoe_required") is None
                or r.get("yoe_is_preferred")
                or r["min_yoe_required"] <= max_yoe)
        ]
    return {"jobs": _enrich(rows), "count": len(rows)}


@app.get("/api/jobs/{key}")
def get_job_endpoint(key: str):
    job = store.get_job(key)
    if not job:
        raise HTTPException(status_code=404, detail="not found")
    return _enrich([job])[0]


@app.post("/api/jobs/{key}/draft-email")
def draft_email_endpoint(key: str):
    job = store.get_job(key)
    if not job:
        raise HTTPException(status_code=404, detail="not found")
    enriched = _enrich([job])[0]
    if (enriched.get("apply_method") or "") != "email" or not enriched.get("contact_email"):
        raise HTTPException(
            status_code=400,
            detail="this job is not flagged apply_method:email in companies.yaml",
        )
    # email_draft uses store.get_job which doesn't know contact_email;
    # patch it onto the dict it'll re-fetch by writing the company meta
    # transiently into the answer pack? Cleaner: pass through via a
    # tiny wrapper that copies the contact_email onto the job dict.
    job_copy = dict(enriched)
    try:
        # Reach in: monkeypatch store.get_job locally for the call.
        from jobcopilot import store as _s
        original = _s.get_job
        def _patched(k, **kw):
            r = original(k, **kw)
            if r and r.get("key") == key:
                r["contact_email"] = enriched["contact_email"]
                r["apply_method"] = "email"
            return r
        _s.get_job = _patched
        try:
            result = email_draft.draft(key)
        finally:
            _s.get_job = original
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/jobs/{key}/status")
def set_status_endpoint(key: str, body: dict = Body(...)):
    status = body.get("status")
    if status not in ("new", "interested", "ready", "applied", "rejected", "not_interested"):
        raise HTTPException(status_code=400, detail="invalid status")
    ok = store.set_status(key, status, notes=body.get("notes"))
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "status": status}


# --------- resume / scoring placeholders (Phase D) ------------------------

@app.post("/api/resume")
async def upload_resume(file: UploadFile = File(...)):
    """Accept a .pdf (preferred) or .txt resume; extract text; store it."""
    data = await file.read()
    fname = file.filename or "resume"
    text = ""
    if fname.lower().endswith(".pdf") or (data[:4] == b"%PDF"):
        try:
            text = extract_pdf_text(data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"PDF parse failed: {exc}")
    else:
        text = data.decode("utf-8", errors="ignore")
    if not text.strip():
        raise HTTPException(status_code=400, detail="no text extracted from upload")
    store.set_resume(text, fname)
    return {"ok": True, "filename": fname, "chars": len(text)}


@app.get("/api/user-yoe")
def get_user_yoe():
    v = store.get_meta("user_yoe")
    return {"user_yoe": int(v) if v and v.isdigit() else None}


@app.post("/api/user-yoe")
def set_user_yoe(body: dict = Body(...)):
    val = body.get("user_yoe")
    if val is None or val == "":
        store.set_meta("user_yoe", "")
        return {"ok": True, "user_yoe": None}
    try:
        n = int(val)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="user_yoe must be an integer or null")
    if n < 0 or n > 50:
        raise HTTPException(status_code=400, detail="user_yoe out of range")
    store.set_meta("user_yoe", str(n))
    return {"ok": True, "user_yoe": n}


@app.post("/api/score")
def score_all_endpoint():
    summary = scoring.score_all()
    if summary.get("error"):
        raise HTTPException(status_code=400, detail=summary["error"])
    return summary


# --------- tailoring placeholders (Phase E) -------------------------------

# --- Profile -------------------------------------------------------------

@app.get("/api/profile")
def profile_get():
    return store.get_profile()


@app.post("/api/profile")
def profile_set(body: dict = Body(...)):
    return store.set_profile(body or {})


# --- Answer pack CRUD ----------------------------------------------------

@app.get("/api/answer-pack")
def answer_pack_list():
    return {"items": store.get_answer_pack()}


@app.post("/api/answer-pack")
def answer_pack_upsert(body: dict = Body(...)):
    q = (body.get("q") or "").strip()
    a = (body.get("a") or "").strip()
    if not q or not a:
        raise HTTPException(status_code=400, detail="q and a required")
    return {"items": store.upsert_answer(q, a)}


@app.delete("/api/answer-pack")
def answer_pack_delete(q: str):
    return {"items": store.delete_answer(q)}


# --- generic meta KV (style sample etc.) --------------------------------

@app.get("/api/meta-kv")
def meta_kv_get(k: str):
    return {"k": k, "v": store.get_meta(k) or ""}


@app.post("/api/meta-kv")
def meta_kv_set(body: dict = Body(...)):
    k = (body.get("k") or "").strip()
    v = body.get("v") or ""
    if not k:
        raise HTTPException(status_code=400, detail="k required")
    store.set_meta(k, v)
    return {"ok": True, "k": k}


# --- CSV export ----------------------------------------------------------

@app.get("/api/export.csv")
def export_csv(
    status: Optional[str] = None,
    sector: Optional[str] = None,
    country_bucket: Optional[str] = None,
    workplace_type: Optional[str] = None,
    role_family: Optional[str] = None,
    freshness: Optional[str] = None,
    q: Optional[str] = None,
    min_score: Optional[float] = None,
    max_age_days: Optional[int] = None,
    hide_senior_high: bool = False,
    max_yoe: Optional[int] = None,
):
    rows = store.list_jobs(
        status=status, sector=sector, country_bucket=country_bucket,
        workplace_type=workplace_type, role_family=role_family,
        freshness=freshness, q=q, min_score=min_score,
        max_age_days=max_age_days, hide_senior_high=hide_senior_high,
        limit=5000,
    )
    if max_yoe is not None:
        rows = [
            r for r in rows
            if (r.get("min_yoe_required") is None
                or r.get("yoe_is_preferred")
                or r["min_yoe_required"] <= max_yoe)
        ]
    import csv, io
    cols = ["company", "title", "role_family", "workplace_type", "country_bucket",
            "comp_currency", "comp_max", "posted_age_days", "freshness",
            "match_score", "ats_coverage", "fit_score", "min_yoe_required",
            "yoe_is_preferred", "yoe_gap", "seniority_high", "status", "url"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=jobs.csv"})


@app.post("/api/funding/run")
def funding_run():
    from jobcopilot import funding
    out = ROOT / "discovered_companies.yaml"
    return funding.run(out_path=str(out))


@app.get("/api/llm/health")
def llm_health():
    try:
        return llm.health()
    except Exception as exc:
        return {"provider": llm.PROVIDER, "reachable": False,
                "model_ok": False, "hint": f"{type(exc).__name__}: {exc}"}


@app.get("/api/jobs/{key}/resume-options")
def resume_options_endpoint(key: str):
    result = tailor_mod.resume_options(key)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/api/jobs/{key}/tailor-suggestion")
def tailor_suggestion(key: str):
    result = tailor_mod.suggest(key)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/jobs/{key}/tailor")
def tailor_job(key: str, body: Optional[dict] = Body(default=None)):
    body = body or {}
    approved = body.get("approved_keywords") or []
    try:
        result = tailor_mod.tailor(key, approved_keywords=approved)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/jobs/{key}/approve")
def approve_job(key: str, body: dict = Body(...)):
    job = store.get_job(key)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    reuse_id = body.get("reuse_version_id")
    if reuse_id is not None:
        version = store.get_version(int(reuse_id))
        if not version:
            raise HTTPException(status_code=404, detail="version not found")
        store.link_job_version(key, int(reuse_id))
        store.set_status(key, "ready")
        return {"ok": True, "mode": "reuse", "version_id": version["id"],
                "pdf_path": version["pdf_path"]}

    tailored_text = body.get("tailored_text")
    if not tailored_text:
        raise HTTPException(status_code=400, detail="missing tailored_text or reuse_version_id")

    required = job.get("required_keywords") or []
    new_have = scoring.extract_keywords(tailored_text)
    new_coverage, _ = scoring.compute_coverage(required, new_have)

    label = body.get("label") or f"{job.get('company','')} · {job.get('title','')[:40]}"
    version = versions_mod.save_tailored_version(
        label=label,
        role_family=job.get("role_family"),
        source_job_key=key,
        source_company=job.get("company"),
        tailored_text=tailored_text,
        target_keywords=required,
        base_ats_coverage=new_coverage,
    )
    store.link_job_version(key, int(version["id"]))
    store.set_status(key, "ready")
    return {"ok": True, "mode": "fresh", "version_id": version["id"],
            "pdf_path": version["pdf_path"], "new_ats_coverage": new_coverage}


@app.get("/api/resume-versions")
def list_versions_endpoint():
    return {"versions": store.list_versions()}


@app.get("/api/jobs/{key}/apply-pack")
def apply_pack(key: str, resume: Optional[str] = None):
    """Returns everything a user needs to fill a form by hand: chosen resume
    (path + download + filename), profile fields, answer-pack items, posting
    URL. `resume` may be `original`, `version:<id>`, or omitted (defaults to
    the linked version if any, else original)."""
    job = store.get_job(key)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    chosen = {"source": None, "label": None, "pdf_url": None, "pdf_path": None,
              "filename": None}

    def _use_version(v: dict) -> None:
        chosen["source"] = "version"
        chosen["version_id"] = v["id"]
        chosen["label"] = v.get("label") or f"version {v['id']}"
        chosen["pdf_url"] = f"/api/resume-versions/{v['id']}/download"
        chosen["pdf_path"] = v.get("pdf_path")
        if v.get("pdf_path"):
            chosen["filename"] = Path(v["pdf_path"]).name

    def _use_original() -> None:
        r = store.get_resume()
        if r:
            chosen["source"] = "original"
            chosen["label"] = r.get("filename") or "Original"
            chosen["filename"] = r.get("filename")
            # The original PDF bytes aren't stored on disk in this build;
            # we only kept extracted text. So no path/download URL; the
            # user uploads it themselves. Flag this honestly.
            chosen["pdf_url"] = None
            chosen["pdf_path"] = None
            chosen["note"] = "original resume text is stored, but the PDF file isn't kept on disk. Attach your local original PDF."

    if resume == "original":
        _use_original()
    elif resume and resume.startswith("version:"):
        try:
            vid = int(resume.split(":", 1)[1])
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid resume selector")
        v = store.get_version(vid)
        if not v:
            raise HTTPException(status_code=404, detail="version not found")
        _use_version(v)
    else:
        # Default: linked version if any, else original.
        vid = job.get("applied_version_id")
        if vid:
            v = store.get_version(int(vid))
            if v:
                _use_version(v)
        if chosen["source"] is None:
            _use_original()

    return {
        "posting_url": job.get("url"),
        "chosen_resume": chosen,
        "profile": store.get_profile(),
        "answer_pack": store.get_answer_pack(),
        # Back-compat with the older keys the current UI reads:
        "pdf_url": chosen["pdf_url"],
        "version_label": chosen["label"],
    }


@app.get("/api/resume-versions/{vid}/download")
def download_version(vid: int):
    v = store.get_version(vid)
    if not v or not v.get("pdf_path"):
        raise HTTPException(status_code=404, detail="not found")
    p = Path(v["pdf_path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="pdf missing on disk")
    return FileResponse(str(p), media_type="application/pdf", filename=p.name)


# --------- static UI ------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"ok": True, "message": "UI not built yet"}
