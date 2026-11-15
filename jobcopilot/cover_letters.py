"""Saved and approved cover letters (docs/DESIGN.md §10).

    status(job_key) -> dict                      # the saved letter for the review card
    save(job_key, text) -> dict                  # check, store, render; never approves
    approve(job_key, sha=..., confirm_flags=...) -> dict
    approved_letter(job) -> dict | None          # what autofill may use, or None

A drafted letter lives in the browser until it is saved. Saving stores it with
the job's current artifact version (the approved one when there is one, else
the newest version under review), in that version's directory under
data/applications/, as text plus a PDF rendered with the résumé's LaTeX
pipeline (text only when TeX isn't installed). The edited text goes through the
same checks as a generated draft (grounding against confirmed facts, length,
clichés, AI tells, metric drift, markup / links / look-alike characters), but
it is never changed: problems are reported, not fixed.

Approval is separate from the résumé's. Saving always clears it; approving
records a fingerprint of the exact text approved. Autofill uses a letter only
when the artifact is the job's approved version, the letter is approved, and
the files on disk still match the approved fingerprint.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from . import store

log = logging.getLogger("jobcopilot.cover_letters")

MAX_CHARS = 8000
TEXT_NAME, PDF_NAME = "cover_letter.txt", "cover_letter.pdf"
TEMPLATE = Path(__file__).resolve().parent / "resume" / "templates" / "cover_letter.tex.j2"
SOURCE = "approved_cover_letter"            # FieldAction.source of a pasted, approved letter
_USABLE = ("ok", "needs_review", "approved", "rejected")


class LetterError(Exception):
    def __init__(self, detail: str, http: int = 400):
        super().__init__(detail)
        self.detail, self.http = detail, http


# --------------------------------------------------------------------------- helpers

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def normalise(text: Any) -> str:
    """Line endings and trailing spaces only; the words are never touched."""
    if not isinstance(text, str):
        raise LetterError("text must be a string")
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = "\n".join(line.rstrip() for line in t.split("\n")).strip("\n").strip()
    if not t:
        raise LetterError("the cover letter is empty")
    if len(t) > MAX_CHARS:
        raise LetterError(f"the cover letter is longer than {MAX_CHARS} characters")
    if "\x00" in t:
        raise LetterError("the cover letter contains a NUL character")
    return t


def _inside(path: Any, root: Path) -> Optional[Path]:
    """`path` resolved, when it is a file under `root`; else None."""
    if not path:
        return None
    try:
        p = Path(path).resolve()
    except (OSError, RuntimeError):
        return None
    return p if p.is_relative_to(root) and p.is_file() else None


def current_artifact(job: dict, db_path: Any = None) -> Optional[dict]:
    """The version a cover letter belongs to, as the review card picks it: the
    job's approved (linked) version, else the newest one not rejected."""
    arts = [a for a in store.list_artifacts(job["key"], db_path) if a.get("status") in _USABLE]
    linked = next((a for a in arts if a["id"] == job.get("artifact_id")), None)
    return linked or next((a for a in arts if a.get("status") != "rejected"), None)


def _letter_dir(job_key: str, art: dict, db_path: Any) -> Path:
    from .tailor_v2 import artifact_dir
    root = store.applications_dir(db_path).resolve()
    d = artifact_dir(job_key, int(art["version"]), db_path).resolve()
    if not d.is_relative_to(root) or d == root:
        raise LetterError("refusing to write outside the applications directory", 500)
    return d


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- checks

_SALUTATION_RE = re.compile(r"^(dear|hello|hi|hey|greetings|to)\b[^\n]{0,60}$", re.I)
_CLOSING_RE = re.compile(r"^(?:(?:best|kind|warm|many)\s+)?(?:regards|wishes|thanks)\b|"
                         r"^(?:sincerely|thank you|thanks|cheers|respectfully|yours)\b", re.I)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _is_signoff(par: str, data: dict) -> bool:
    """A closing line followed only by your own name / contact details."""
    from .writing import common
    lines = [ln.strip() for ln in par.split("\n") if ln.strip()]
    if not lines or len(lines) > 6 or not _CLOSING_RE.match(lines[0]) or len(lines[0].split()) > 5:
        return False
    own = {_squash(v) for v in common.sign_values(data).values() if v}
    for ln in lines[1:]:
        parts = [x for x in re.split(r"\s*[|·•,]\s*", ln) if x.strip()]
        if not parts or any(_squash(x) not in own for x in parts):
            return False
    return True


def split_frame(text: str, data: dict, salutation: str) -> tuple[list[str], list[str]]:
    """(body paragraphs, salutation / sign-off paragraphs). Only a short greeting
    line and a sign-off made of a closing plus your own details are left out of
    the grounding check; everything else is body and is checked."""
    from .writing import common, style
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    frame: list[str] = []
    if paras and "\n" not in paras[0] and (
            _squash(paras[0]) == _squash(salutation)
            or (_SALUTATION_RE.match(paras[0]) and style.word_count(paras[0]) <= 8)):
        frame.append(paras.pop(0))
    if paras and (_squash(paras[-1]) == _squash(common.signoff_block(data, []))
                  or _is_signoff(paras[-1], data)):
        frame.append(paras.pop())
    return paras, frame


def _infer_ids(par: str, usable: list, hinfo: dict) -> list[str]:
    """Fact ids a hand-edited paragraph is checked against: the facts of the one
    employer / project it names (all named ones when it names several, which
    the checks then flag), else of the one it shares the most words with.
    Skills are never added: a detail can't move between experiences."""
    from . import grounding
    from .writing import common
    by_id = {f.id: f for f in usable}
    groups: dict[str, list[str]] = {}
    for f in usable:
        r = common._root(f.id, by_id)
        if r:
            groups.setdefault(r, []).append(f.id)
    roots = [h for h in common.named_headers(par, hinfo) if h in groups]
    if not roots and groups:
        words = grounding._content_tokens(par)
        best = max(groups, key=lambda r: len(words & grounding._content_tokens(
            " ".join(common._fact_text(by_id[x]) for x in groups[r]))))
        shared = words & grounding._content_tokens(
            " ".join(common._fact_text(by_id[x]) for x in groups[best]))
        roots = [best] if len(shared) >= 2 else []
    return [x for r in roots for x in groups[r]]


def check_text(text: str, job: dict, *, facts: Any = None, answers: Any = None,
               base: Optional[str] = None, company: Any = None) -> dict:
    """The generated-draft checks, run on text the person wrote, without
    changing it. Returns {status: ok | needs_review | refused, warnings,
    word_count, max_words, ai_tells, grounding: {clean, flags, unsupported}}.
    AI tells (style.find_ai_tells) are flagged in the warnings and `ai_tells`
    but, as for a generated draft, don't change the status on their own; the
    text is never touched."""
    from .writing import common, style
    cl = style.settings()["cover_letter"]
    max_words = int(cl["max_words"])
    usable = common.resolve_facts(facts, base)
    if not usable:
        return {"status": "refused", "warnings": [common.REFUSE_NO_FACTS], "max_words": max_words,
                "word_count": style.word_count(text), "grounding": {"clean": False, "flags": {}}}
    data = common.answers_data(answers)
    rec = common.company_record(job, company)
    title, comp = common.role_names(job, rec)
    hinfo = common.header_info(usable, exclude_words=f"{comp} {title}")
    body, frame = split_frame(text, data, str(cl.get("salutation") or ""))
    if not body:
        return {"status": "needs_review", "warnings": ["no letter body to check"],
                "max_words": max_words, "word_count": 0,
                "grounding": {"clean": False, "flags": {}, "unsupported": []}}
    paragraphs = [{"text": p, "fact_ids": _infer_ids(p, usable, hinfo)} for p in body]
    built, issues = common.assemble(paragraphs, usable, job=job, rec=rec, data=data,
                                    max_words=max_words, rewrite=False)
    warnings = [str(i) for i in issues]
    cliches = list(dict.fromkeys(built["removed"] + style.find_cliches(" ".join(frame))))
    if cliches:
        warnings.append("cliché(s) to reword: " + ", ".join(cliches))
    if built["normalised"]:
        warnings.append("number(s) written differently from your facts: " + ", ".join(built["normalised"]))
    g = built.get("grounding") or {}
    status = "ok" if not warnings and g.get("clean") else "needs_review"
    # AI tells are flagged in the warnings, never rewritten, and (as for a draft)
    # don't change the status on their own: they're style, not grounding.
    tells = style.find_ai_tells("\n\n".join(body))
    warnings += [f"reads like AI-written text: {r}" for r in tells]
    return {"status": status, "warnings": warnings, "word_count": built["words"], "max_words": max_words,
            "ai_tells": tells,
            "grounding": {"clean": bool(g.get("clean")),
                          "flags": {k: v for k, v in (g.get("flags") or {}).items() if v},
                          "unsupported": [v.get("text") for v in g.get("lines") or []
                                          if not v.get("supported")]}}


# --------------------------------------------------------------------------- PDF

def render_tex(text: str) -> str:
    from .resume.render import _env
    paras = [[ln for ln in p.split("\n") if ln.strip()] for p in re.split(r"\n\s*\n", text) if p.strip()]
    return _env(TEMPLATE.parent).get_template(TEMPLATE.name).render(paragraphs=paras)


def render_pdf(text: str, out: Path) -> tuple[Optional[Path], list[str]]:
    """(pdf path or None, notes). TeX missing or failing leaves no PDF; the
    caller keeps the text. Characters TeX can't print are reported."""
    from .resume import latex as L
    notes: list[str] = []
    try:
        tex = render_tex(text)
        compiled = L.compile_tex(tex, jobname="cover_letter")
    except L.LatexUnavailable:
        return None, ["TeX isn't installed, so only the text was saved (no PDF)"]
    except L.LatexError as exc:
        log.warning("cover letter PDF failed: %s", exc)
        return None, [f"the PDF couldn't be rendered ({exc}); only the text was saved"]
    _atomic_write(out, compiled.pdf)
    try:
        pages, _ = L.pdf_pages_and_text(out)
        if pages > 1:
            notes.append(f"the PDF is {pages} pages")
    except Exception:  # noqa: BLE001 - the page count is a nicety
        pass
    missing = L.unsupported_chars(text)
    if missing:
        notes.append("the PDF leaves out characters it can't print: " + " ".join(missing))
    return out, notes


# --------------------------------------------------------------------------- state

def _state(art: Optional[dict], db_path: Any = None, job: Optional[dict] = None) -> dict:
    """The review card's view of an artifact's letter (no file paths)."""
    if not art:
        return {"saved": False, "artifact_id": None}
    meta = art.get("cover_letter_json") if isinstance(art.get("cover_letter_json"), dict) else None
    out: dict[str, Any] = {"saved": False, "artifact_id": art["id"], "artifact_version": art["version"],
                           "artifact_status": art.get("status")}
    if not meta:
        return out
    root = store.applications_dir(db_path).resolve()
    tp = _inside(meta.get("text_path"), root)
    text = tp.read_text(encoding="utf-8") if tp else ""
    sha = meta.get("sha256")
    intact = bool(tp) and sha256_text(text) == sha
    approved = bool(art.get("cover_letter_approved_at")) and art.get("cover_letter_approved_sha") == sha and intact
    out.update({
        "saved": bool(tp), "text": text, "sha256": sha, "saved_at": meta.get("saved_at"),
        "checks": meta.get("checks") or {}, "notes": meta.get("notes") or [],
        "pdf_url": (f"/api/artifacts/{art['id']}/cover-letter/pdf"
                    if _inside(meta.get("pdf_path"), root) else None),
        "approved": approved, "approved_at": art.get("cover_letter_approved_at") if approved else None,
        "intact": intact,
    })
    # what autofill will do with it: only the job's approved, linked version counts
    out["usable"] = (approved and art.get("status") == "approved"
                     and job is not None and job.get("artifact_id") == art["id"])
    return out


def _job(job_key: str, db_path: Any) -> dict:
    job = store.get_job(job_key, db_path)
    if not job:
        raise LetterError("job not found", 404)
    return job


def status(job_key: str, *, db_path: Any = None) -> dict:
    job = _job(job_key, db_path)
    art = current_artifact(job, db_path)
    out = _state(art, db_path, job)
    out["job_artifact_id"] = job.get("artifact_id")
    if art and not out.get("saved"):           # a letter saved with an older version
        for a in store.list_artifacts(job_key, db_path):
            if a["id"] != art["id"] and isinstance(a.get("cover_letter_json"), dict):
                prev = _state(a, db_path)
                if prev.get("saved"):
                    out["earlier"] = {"artifact_version": a["version"], "text": prev["text"]}
                    break
    return out


def save(job_key: str, text: Any, *, facts: Any = None, answers: Any = None,
         db_path: Any = None, render: bool = True) -> dict:
    """Check and store the letter with the job's current artifact version.
    Clears any earlier cover-letter approval. Never approves."""
    t = normalise(text)
    job = _job(job_key, db_path)
    art = current_artifact(job, db_path)
    if not art:
        raise LetterError("tailor a résumé for this job first: the cover letter is saved with "
                          "that version", 409)
    checks = check_text(t, job, facts=facts, answers=answers, base=art.get("base"))
    if checks["status"] == "refused":
        raise LetterError(" ".join(checks["warnings"]), 409)
    d = _letter_dir(job_key, art, db_path)
    tp, pp = d / TEXT_NAME, d / PDF_NAME
    # The old approval goes first, so a crash part-way never leaves new text approved.
    store.update_artifact(art["id"], db_path, cover_letter_approved_at=None, cover_letter_approved_sha=None)
    _atomic_write(tp, t.encode("utf-8"))
    pp.unlink(missing_ok=True)               # a PDF of older text must never outlive it
    pdf, notes = render_pdf(t, pp) if render else (None, ["PDF rendering is off; only the text was saved"])
    upload = pdf or tp
    meta = {"text_path": str(tp), "pdf_path": str(pdf) if pdf else None, "sha256": sha256_text(t),
            "file_sha256": _sha_file(upload), "saved_at": time.time(), "checks": checks, "notes": notes}
    store.update_artifact(art["id"], db_path, cover_letter_json=meta, cover_letter_path=str(upload))
    log.info("cover letter saved job=%s artifact=%s status=%s words=%s pdf=%s", job_key, art["id"],
             checks["status"], checks.get("word_count"), bool(pdf))
    return _state(store.get_artifact(art["id"], db_path), db_path, job)


def approve(job_key: str, *, sha: Any, confirm_flags: bool = False, facts: Any = None,
            answers: Any = None, db_path: Any = None) -> dict:
    """Approve the saved letter. `sha` is the fingerprint of the text the person
    saw (from save/status), so a letter changed since can't be approved by an
    old click. The checks are re-run on the stored text against the current
    facts; any warning needs confirm_flags=True."""
    job = _job(job_key, db_path)
    art = current_artifact(job, db_path)
    st = _state(art, db_path, job)
    if not st.get("saved"):
        raise LetterError("save the cover letter first", 409)
    if not st.get("intact"):
        raise LetterError("the saved cover letter changed on disk; save it again", 409)
    if not isinstance(sha, str) or sha != st["sha256"]:
        raise LetterError("the saved cover letter changed since you looked at it; review it again", 409)
    meta = art["cover_letter_json"]
    root = store.applications_dir(db_path).resolve()
    up = _inside(art.get("cover_letter_path"), root)
    if not up or _sha_file(up) != meta.get("file_sha256"):
        raise LetterError("the cover letter file changed on disk; save it again", 409)
    checks = check_text(st["text"], job, facts=facts, answers=answers, base=art.get("base"))
    if checks["status"] == "refused":
        raise LetterError(" ".join(checks["warnings"]), 409)
    if checks["status"] != "ok" and confirm_flags is not True:
        return {**st, "checks": checks, "approved": False, "needs_confirmation": True,
                "detail": "the checks flagged this letter: confirm you've reviewed every flag, "
                          "or edit and save it again"}
    meta = dict(meta, checks=checks, confirmed_flags=checks["status"] != "ok")
    store.update_artifact(art["id"], db_path, cover_letter_json=meta,
                          cover_letter_approved_at=time.time(), cover_letter_approved_sha=st["sha256"])
    log.info("cover letter approved job=%s artifact=%s flags_confirmed=%s", job_key, art["id"],
             checks["status"] != "ok")
    return _state(store.get_artifact(art["id"], db_path), db_path, job)


def approved_letter(job: dict, db_path: Any = None) -> Optional[dict]:
    """{'path', 'text'} of the cover letter autofill may use, or None. Only the
    job's APPROVED, linked artifact counts; the letter must be approved and the
    files must still match the fingerprints taken when it was saved/approved."""
    aid = job.get("artifact_id")
    art = store.get_artifact(int(aid), db_path) if aid else None
    if not art or art.get("status") != "approved" or art.get("job_key") != job.get("key"):
        return None
    meta = art.get("cover_letter_json") if isinstance(art.get("cover_letter_json"), dict) else None
    sha = art.get("cover_letter_approved_sha")
    if not meta or not art.get("cover_letter_approved_at") or not sha or meta.get("sha256") != sha:
        return None
    root = store.applications_dir(db_path).resolve()
    tp, up = _inside(meta.get("text_path"), root), _inside(art.get("cover_letter_path"), root)
    if not tp or not up:
        return None
    text = tp.read_text(encoding="utf-8")
    if sha256_text(text) != sha or _sha_file(up) != meta.get("file_sha256"):
        log.warning("cover letter on disk no longer matches its approval job=%s", job.get("key"))
        return None
    return {"path": str(up), "text": text}


def pdf_path(artifact_id: int, db_path: Any = None) -> Optional[Path]:
    art = store.get_artifact(int(artifact_id), db_path)
    meta = (art or {}).get("cover_letter_json")
    if not isinstance(meta, dict):
        return None
    p = _inside(meta.get("pdf_path"), store.applications_dir(db_path).resolve())
    return p if p is not None and p.suffix.lower() == ".pdf" else None
