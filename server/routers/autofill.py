"""Phase 4 autofill API: fill plans, the headed fill session, hand-over, needs-you.

Nothing here (or behind it) ever submits a form: a fill stops before Submit and
the user presses it. Demo mode refuses every write (403, also enforced by the
demo middleware in app.py).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from jobcopilot import demo, store
from jobcopilot.autofill import service

log = logging.getLogger("jobcopilot.server.autofill")

router = APIRouter()


def _refused(exc: service.AutofillRefused) -> JSONResponse:
    return JSONResponse(status_code=exc.http, content=exc.to_dict())


def _no_demo() -> None:
    if demo.enabled():
        raise HTTPException(status_code=403, detail="autofill is off in the public demo")


@router.post("/api/jobs/{key}/autofill")
def start_autofill(key: str):
    """Open the headed browser and fill the form; returns at once with the session id."""
    _no_demo()
    try:
        sid = service.launch_fill(key)
    except service.AutofillRefused as exc:
        return _refused(exc)
    return JSONResponse(status_code=202, content={"session_id": sid, "status": "starting"})


@router.get("/api/autofill/sessions/{session_id}")
def get_session(session_id: int):
    row = service.session_status(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such session")
    return row


@router.post("/api/autofill/sessions/{session_id}/handover")
def handover(session_id: int):
    """Lift the page-level submit block so YOUR click on Submit goes through."""
    _no_demo()
    row = service.session_status(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such session")
    if not row.get("open") or not service.handover(session_id):
        raise HTTPException(status_code=409, detail="that fill window is no longer open")
    return {"ok": True, "session_id": session_id}


@router.get("/api/autofill/needs-you")
def needs_you():
    return {"sessions": service.needs_you()}


@router.get("/api/jobs/{key}/fill-plan")
def get_fill_plan(key: str):
    if store.get_job(key) is None:
        raise HTTPException(status_code=404, detail="no such job")
    row = service.stored_plan(key)
    if row is None:
        raise HTTPException(status_code=404, detail="no fill plan yet")
    return row


@router.post("/api/jobs/{key}/fill-plan/rebuild")
def rebuild_fill_plan(key: str):
    """Rebuild now (read-only: questions API or a headless page load; never types)."""
    _no_demo()
    try:
        service.plan_for_job(key)
    except service.AutofillRefused as exc:
        return _refused(exc)
    return service.stored_plan(key)
