"""Header summary, generic meta KV, LLM health."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from jobcopilot import llm, store

router = APIRouter()


@router.get("/api/meta")
def get_meta_summary():
    resume = store.get_resume()
    return {
        "last_ingest": store.get_meta("last_ingest"),
        "status_counts": store.status_counts(),
        "resume_loaded": resume is not None,
        "resume_filename": resume.get("filename") if resume else None,
        "version_count": len(store.list_versions()),
    }


@router.get("/api/meta-kv")
def meta_kv_get(k: str):
    return {"k": k, "v": store.get_meta(k) or ""}


@router.post("/api/meta-kv")
def meta_kv_set(body: dict = Body(...)):
    k = (body.get("k") or "").strip()
    v = body.get("v") or ""
    if not k:
        raise HTTPException(status_code=400, detail="k required")
    store.set_meta(k, v)
    return {"ok": True, "k": k}


@router.get("/api/llm/health")
def llm_health():
    try:
        return llm.health()
    except Exception as exc:
        return {"provider": llm.PROVIDER, "reachable": False,
                "model_ok": False, "hint": f"{type(exc).__name__}: {exc}"}
