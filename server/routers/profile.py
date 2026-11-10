"""The user's own data: base résumé upload, YOE, profile fields, answer pack."""
from __future__ import annotations

from fastapi import APIRouter, Body, File, HTTPException, UploadFile

from jobcopilot import store
from jobcopilot.resume import extract_text as extract_pdf_text

router = APIRouter()


@router.post("/api/resume")
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


@router.get("/api/user-yoe")
def get_user_yoe():
    v = store.get_meta("user_yoe")
    return {"user_yoe": int(v) if v and v.isdigit() else None}


@router.post("/api/user-yoe")
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


@router.get("/api/profile")
def profile_get():
    return store.get_profile()


@router.post("/api/profile")
def profile_set(body: dict = Body(...)):
    return store.set_profile(body or {})


@router.get("/api/answer-pack")
def answer_pack_list():
    return {"items": store.get_answer_pack()}


@router.post("/api/answer-pack")
def answer_pack_upsert(body: dict = Body(...)):
    q = (body.get("q") or "").strip()
    a = (body.get("a") or "").strip()
    if not q or not a:
        raise HTTPException(status_code=400, detail="q and a required")
    return {"items": store.upsert_answer(q, a)}


@router.delete("/api/answer-pack")
def answer_pack_delete(q: str):
    return {"items": store.delete_answer(q)}
