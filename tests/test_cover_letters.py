"""Saved and approved cover letters (jobcopilot/cover_letters.py, docs/DESIGN.md §10):
the save endpoint re-runs the draft checks on your text without changing it,
writes only under data/applications, never approves; approval is its own step
and autofill (file upload and the cover-letter box) uses only an approved,
unchanged letter, never storing its text in fill plans or logs.

Offline: facts and answers come from a temp copy of profile.example (the fake
"Alex Example"); the PDF step is faked unless a test says otherwise."""
from __future__ import annotations

import copy
import json
import logging
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobcopilot import answers as A
from jobcopilot import cover_letters as CL
from jobcopilot import llm, store
from jobcopilot.autofill import service
from jobcopilot.autofill.adapters.base import PlanContext
from jobcopilot.autofill.adapters.lever import LeverAdapter
from jobcopilot.autofill.plan import COVER_TEXT_SOURCE, FieldAction, FillPlan, Outcome
from jobcopilot.filters import FilterResult
from jobcopilot.models import Job
from jobcopilot.tailor_v2 import artifact_dir
from jobcopilot.writing import common

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "profile.example"
BANK = A.load(EXAMPLE / "answers.yaml")
JD = "Initrode builds order pipelines in Go and PostgreSQL. Must have: Go, PostgreSQL, REST APIs."
MARK = "nightly batch runtime"           # a phrase that must never reach a plan row or a log


def letter(body_extra: str = "") -> str:
    sign = common.signoff_block(BANK.data, [])
    return ("Dear Hiring Manager,\n\n"
            "Initrode builds order pipelines in Go and PostgreSQL.\n\n"
            "At Acme Widgets Inc., I built a Go order service handling 200+ requests per second "
            "behind a REST API.\n\n"
            "At Acme Widgets Inc., I cut nightly batch runtime by 30% by moving widget pricing "
            f"to PostgreSQL.{body_extra}\n\n"
            "I would welcome the chance to discuss the role. Thank you for your time.\n\n" + sign)


REAL_RENDER = CL.render_pdf          # the autouse fixture fakes it; a test puts it back explicitly
GOOD = letter()
FLAGGED = letter(" I am passionate about distributed systems.").replace("200+", "5000+")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    def _refuse(*_a, **_kw):
        raise AssertionError("saving or approving a cover letter must not call an LLM")
    monkeypatch.setattr(llm, "complete", _refuse)
    monkeypatch.setattr(llm, "complete_json", _refuse)
    prof = tmp_path / "profile"
    shutil.copytree(EXAMPLE, prof)
    monkeypatch.setenv("JOBCOPILOT_PROFILE_DIR", str(prof))     # never the real profile
    monkeypatch.setattr(common, "company_record", lambda job, company=None: {"name": job.get("company")})
    monkeypatch.setattr(service, "_LIVE", {})

    def fake_pdf(text, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-1.4\n% fake cover letter\n")
        return out, []
    monkeypatch.setattr(CL, "render_pdf", fake_pdf)


def seed(url="https://jobs.example.com/apply/9", external_id="9", status="approved") -> str:
    job = Job(source="career_page", company="Initrode", company_sector="tech", external_id=external_id,
              title="Backend Engineer", location_text="Bengaluru", workplace_type="onsite",
              country_bucket="IN", url=url, description_text=JD)
    store.upsert_jobs([FilterResult(job=job, package_estimated=False, country_uncertain=False)])
    aid, v = store.create_artifact(job.key, "backend_sde")
    d = artifact_dir(job.key, v)
    d.mkdir(parents=True, exist_ok=True)
    (d / "resume.pdf").write_bytes(b"%PDF-1.4\n% resume\n")
    store.update_artifact(aid, status=status, pdf_path=str(d / "resume.pdf"))
    if status == "approved":
        store.link_job_artifact(job.key, aid)
    return job.key


def art_of(key: str) -> dict:
    return store.list_artifacts(key)[0]


def save_and_approve(key: str, text: str = GOOD, **kw) -> dict:
    st = CL.save(key, text, answers=BANK)
    return CL.approve(key, sha=st["sha256"], answers=BANK, **kw)


@pytest.fixture
def client():
    from server.routers import writing as writing_router
    app = FastAPI()
    app.include_router(writing_router.router)
    return TestClient(app)


# --------------------------------------------------------------------------- save

def test_save_runs_the_checks_and_writes_text_and_pdf_under_the_data_dir():
    key = seed()
    st = CL.save(key, GOOD.replace("\n", "\r\n") + "   \n", answers=BANK)
    assert st["saved"] and st["checks"]["status"] == "ok" and st["checks"]["grounding"]["clean"]
    assert st["approved"] is False and st["usable"] is False           # saving never approves
    art = art_of(key)
    d = artifact_dir(key, art["version"]).resolve()
    root = store.applications_dir().resolve()
    assert d.is_relative_to(root)
    assert (d / "cover_letter.txt").read_text(encoding="utf-8") == GOOD   # line endings only
    assert Path(art["cover_letter_path"]) == d / "cover_letter.pdf"
    assert art["cover_letter_json"]["checks"]["status"] == "ok"
    assert st["pdf_url"] == f"/api/artifacts/{art['id']}/cover-letter/pdf"
    assert "path" not in json.dumps({k: v for k, v in st.items() if k != "pdf_url"})   # no file paths out
    assert service.approved_files(store.get_job(key))[0].get("cover_letter") is None


def test_flagged_text_is_reported_never_rewritten():
    key = seed()
    st = CL.save(key, FLAGGED, answers=BANK)
    chk = st["checks"]
    assert chk["status"] == "needs_review" and not chk["grounding"]["clean"]
    assert "5000" in chk["grounding"]["flags"]["new_numbers"]
    assert any("passionate about" in w for w in chk["warnings"])
    assert st["text"] == FLAGGED                         # the cliché sentence and the number stay
    over = CL.save(key, letter(" " + "I built a Go order service. " * 40), answers=BANK)["checks"]
    assert over["status"] == "needs_review" and any("over the" in w for w in over["warnings"])


def test_save_refuses_without_an_artifact_or_facts(tmp_path):
    job = Job(source="career_page", company="Initrode", company_sector="tech", external_id="n",
              title="Backend Engineer", location_text="Bengaluru", workplace_type="onsite",
              country_bucket="IN", url="https://jobs.example.com/n", description_text=JD)
    store.upsert_jobs([FilterResult(job=job, package_estimated=False, country_uncertain=False)])
    with pytest.raises(CL.LetterError) as e:
        CL.save(job.key, GOOD, answers=BANK)
    assert e.value.http == 409 and "tailor" in e.value.detail
    key = seed(external_id="f")
    with pytest.raises(CL.LetterError) as e:
        CL.save(key, GOOD, facts=[], answers=BANK)
    assert e.value.http == 409
    for bad in ("", "   ", 42, "x" * (CL.MAX_CHARS + 1)):
        with pytest.raises(CL.LetterError):
            CL.save(key, bad, answers=BANK)
    assert not (artifact_dir(key, 1) / "cover_letter.txt").exists()


def test_no_tex_keeps_the_text_and_says_so(monkeypatch):
    from jobcopilot.resume import latex as L
    monkeypatch.setattr(CL, "render_pdf", REAL_RENDER)   # the real render step, but no pdflatex
    monkeypatch.setattr(L, "find_pdflatex", lambda cfg=None: None)
    key = seed()
    st = CL.save(key, GOOD, answers=BANK)
    art = art_of(key)
    assert st["saved"] and st["pdf_url"] is None and any("TeX" in n for n in st["notes"])
    assert art["cover_letter_path"].endswith("cover_letter.txt")
    # the text file is what gets uploaded once approved
    CL.approve(key, sha=st["sha256"], answers=BANK)
    assert service.approved_files(store.get_job(key))[0]["cover_letter"].endswith("cover_letter.txt")


def test_real_pdf_when_tex_is_installed(monkeypatch, tmp_path):
    from jobcopilot.resume import latex as L
    if not L.find_pdflatex():
        pytest.skip("no pdflatex on this machine")
    out, notes = REAL_RENDER(GOOD + "\n\n100% {braces} \\ & $5 #1 ~x ^y_z [opt]", tmp_path / "cl.pdf")
    assert out and out.read_bytes().startswith(b"%PDF")
    pages, text = L.pdf_pages_and_text(out)
    assert pages == 1 and "200+ requests per second" in text and "{braces}" in text


def test_path_traversal_is_impossible(client, tmp_path):
    root = store.applications_dir().resolve()
    for evil in ("../../../../etc/x", "..", "/abs/path", "a/../../b", "..\\..\\c"):
        d = CL._letter_dir(evil, {"version": 1}, None)
        assert d.is_relative_to(root) and d != root
    # an unknown key (even an encoded traversal) is a 404 and writes nothing
    r = client.post("/api/jobs/..%2F..%2Fetc/cover-letter/save", json={"text": GOOD})
    assert r.status_code == 404
    assert not any(p.name.startswith("cover_letter") for p in tmp_path.rglob("*"))
    # stored paths pointing outside data/applications are never read, served or uploaded
    key = seed()
    st = save_and_approve(key)
    outside = tmp_path / "outside.txt"
    outside.write_text(GOOD, encoding="utf-8")
    art = art_of(key)
    meta = dict(art["cover_letter_json"], text_path=str(outside), pdf_path="/etc/hosts")
    store.update_artifact(art["id"], cover_letter_json=meta)
    assert CL.approved_letter(store.get_job(key)) is None
    assert CL.status(key)["saved"] is False
    assert client.get(f"/api/artifacts/{art['id']}/cover-letter/pdf").status_code == 404
    store.update_artifact(art["id"], cover_letter_json=art["cover_letter_json"],
                          cover_letter_path="/etc/hosts")
    assert "cover_letter" not in service.approved_files(store.get_job(key))[0]
    assert st["approved"]


# --------------------------------------------------------------------------- approval

def test_approval_is_separate_and_saving_clears_it():
    key = seed()
    st = save_and_approve(key)
    assert st["approved"] and st["usable"]
    files, _ = service.approved_files(store.get_job(key))
    assert files["cover_letter"] == art_of(key)["cover_letter_path"]
    st2 = CL.save(key, GOOD.replace("Thank you for your time.", "Thank you."), answers=BANK)
    assert st2["approved"] is False and st2["usable"] is False
    assert CL.approved_letter(store.get_job(key)) is None
    assert "cover_letter" not in service.approved_files(store.get_job(key))[0]


def test_approve_needs_the_text_you_saw_and_confirmation_of_flags():
    key = seed()
    with pytest.raises(CL.LetterError) as e:
        CL.approve(key, sha="x", answers=BANK)
    assert e.value.http == 409                                         # nothing saved yet
    st = CL.save(key, FLAGGED, answers=BANK)
    with pytest.raises(CL.LetterError):
        CL.approve(key, sha="0" * 64, answers=BANK)                    # an old / wrong fingerprint
    out = CL.approve(key, sha=st["sha256"], answers=BANK)
    assert out["needs_confirmation"] and not out["approved"]
    assert CL.approved_letter(store.get_job(key)) is None
    out = CL.approve(key, sha=st["sha256"], confirm_flags=True, answers=BANK)
    assert out["approved"] and art_of(key)["cover_letter_json"]["confirmed_flags"] is True


def test_unapproved_or_tampered_letter_is_never_uploaded(monkeypatch):
    # saved but not approved
    key = seed()
    CL.save(key, GOOD, answers=BANK)
    assert CL.approved_letter(store.get_job(key)) is None
    # approved letter, but the résumé version isn't approved
    key2 = seed(external_id="nr", status="needs_review")
    st = CL.save(key2, GOOD, answers=BANK)
    assert CL.approve(key2, sha=st["sha256"], answers=BANK)["approved"]
    assert CL.status(key2)["usable"] is False
    assert CL.approved_letter(store.get_job(key2)) is None
    # approved, then the text or the PDF is changed on disk
    key3 = seed(external_id="t")
    save_and_approve(key3)
    art = art_of(key3)
    tp = Path(art["cover_letter_json"]["text_path"])
    tp.write_text(GOOD + "\nP.S. I also invented Go.", encoding="utf-8")
    assert CL.approved_letter(store.get_job(key3)) is None
    tp.write_text(GOOD, encoding="utf-8")
    assert CL.approved_letter(store.get_job(key3)) is not None
    Path(art["cover_letter_path"]).write_bytes(b"%PDF-1.4 swapped")
    assert CL.approved_letter(store.get_job(key3)) is None
    assert "cover_letter" not in service.approved_files(store.get_job(key3))[0]


# --------------------------------------------------------------------------- autofill

SNAP = {"source": "page", "url": "https://jobs.example.com/apply/9", "steps": [[
    {"label": "Email", "name": "email", "type": "text", "required": True, "options": [], "hidden": False,
     "locator": '[name="email"]'},
    {"label": "Cover letter", "name": "cl_file", "type": "file", "required": False, "options": [],
     "hidden": False, "locator": '[name="cl_file"]'},
    {"label": "Resume", "name": "resume", "type": "file", "required": True, "options": [], "hidden": False,
     "locator": '[name="resume"]'},
    {"label": "Cover letter text", "name": "cl_text", "type": "textarea", "required": False, "options": [],
     "hidden": False, "locator": '[name="cl_text"]'},
    {"label": "Why do you want to work here?", "name": "why", "type": "textarea", "required": True,
     "options": [], "hidden": False, "locator": '[name="why"]'},
    {"label": "Anything else? (a cover letter is welcome)", "name": "extra", "type": "textarea",
     "required": False, "options": [], "hidden": False, "locator": '[name="extra"]'},
]]}


class Backend:
    def __init__(self):
        self.calls, self.plans, self.ctxs = [], [], []

    def snapshot(self, url, ats, cfg, adapter=None):
        self.calls.append("snapshot")
        return copy.deepcopy(SNAP)

    def open_headed(self, cfg):
        return {"fake": True}

    def execute(self, handle, adapter, plan, ctx, cfg):
        self.plans.append(plan)
        self.ctxs.append(ctx)
        return Outcome("needs_you", "stopped before submit", 3, 1, ["Why do you want to work here?"])

    def tab_state(self, handle):
        return None

    def screenshot(self, handle, path):
        Path(path).write_bytes(b"\x89PNG fake")
        return str(path)

    def handover(self, handle):
        return True

    def wait(self, handle, seconds):
        pass

    def close(self, handle):
        pass


def _cfg():
    c = service.settings()
    c["timeouts"] = dict(c["timeouts"], watch_poll_sec=0.01, watch_min=1)
    return c


def acts(plan: FillPlan) -> dict[str, FieldAction]:
    return {a.label: a for a in plan.actions()}


def test_autofill_uses_the_approved_text_and_file_and_never_stores_the_text(monkeypatch, caplog):
    monkeypatch.setattr(service, "BACKEND", Backend())
    monkeypatch.setattr(service, "adapter_for", lambda url, ats: None)
    key = seed()
    save_and_approve(key)
    caplog.set_level(logging.DEBUG)
    plan = service.plan_for_job(key, cfg=_cfg(), bank=BANK)
    a = acts(plan)
    assert a["Cover letter text"].value == GOOD and a["Cover letter text"].source == COVER_TEXT_SOURCE
    assert not a["Cover letter text"].needs_user
    assert a["Cover letter"].kind == "upload" and a["Cover letter"].value == "cover_letter"
    assert plan.files["cover_letter"] == art_of(key)["cover_letter_path"]
    for open_q in ("Why do you want to work here?", "Anything else? (a cover letter is welcome)"):
        assert a[open_q].value is None and a[open_q].needs_user, open_q    # open questions stay yours
    row = store.get_fill_plan(key)
    dumped = json.dumps(row)
    assert MARK not in dumped and str(art_of(key)["cover_letter_path"]) not in dumped
    by = {x["label"]: x for s in row["plan"]["steps"] for x in s}
    assert by["Cover letter text"]["value"] is None and by["Cover letter text"]["approved"] is True
    assert by["Cover letter"]["value"] == "cover_letter"                  # the KEY, never a path
    assert MARK not in caplog.text
    assert MARK not in repr(plan) and MARK not in repr(service.plan_context(store.get_job(key), "u", {},
                                                                             cover_text=GOOD))
    assert service.stored_plan(key) and MARK not in json.dumps(service.stored_plan(key))


def test_autofill_leaves_the_box_empty_without_an_approved_letter(monkeypatch):
    monkeypatch.setattr(service, "BACKEND", Backend())
    monkeypatch.setattr(service, "adapter_for", lambda url, ats: None)
    key = seed()
    CL.save(key, GOOD, answers=BANK)                                        # saved, not approved
    plan = service.plan_for_job(key, cfg=_cfg(), bank=BANK)
    a = acts(plan)
    assert a["Cover letter text"].value is None and a["Cover letter text"].note == "no approved cover letter"
    assert a["Cover letter"].value is None and "cover_letter" not in plan.files


def test_fill_time_rechecks_approval(monkeypatch):
    """A letter re-saved after the nightly plan is not typed at fill time."""
    be = Backend()
    monkeypatch.setattr(service, "BACKEND", be)
    monkeypatch.setattr(service, "adapter_for", lambda url, ats: None)
    key = seed()
    save_and_approve(key)
    service.plan_for_job(key, cfg=_cfg(), bank=BANK)
    CL.save(key, GOOD.replace("Thank you for your time.", "Thanks."), answers=BANK)   # approval cleared
    service.launch_fill(key, cfg=_cfg(), background=False)
    plan = be.plans[-1]
    assert "cover_letter" not in plan.files
    assert all(x.source != COVER_TEXT_SOURCE or x.value is None for x in plan.actions())
    assert be.ctxs[-1].cover_letter_text is None
    # approve the new text: the next fill types exactly it
    CL.approve(key, sha=CL.status(key)["sha256"], answers=BANK)
    service.launch_fill(key, cfg=_cfg(), background=False)
    assert acts(be.plans[-1])["Cover letter text"].value.endswith(common.signoff_block(BANK.data, []))
    assert be.ctxs[-1].cover_letter_text == CL.status(key)["text"]


def test_enforce_policy_blanks_a_pasted_letter_that_is_not_the_approved_one():
    def plan():
        return FillPlan("k", "generic", "https://x.test/apply", steps=[[
            FieldAction("#a", "Cover letter", "fill", "approved text", COVER_TEXT_SOURCE, 1.0, False),
            FieldAction("#b", "Cover letter", "select", "approved text", COVER_TEXT_SOURCE, 1.0, False),
        ]])
    for cover in (None, "other text"):
        p = service.enforce_policy(plan(), {"min_confidence": 0.8}, cover)
        assert [a.value for a in p.actions()] == [None, None]
    p = service.enforce_policy(plan(), {"min_confidence": 0.8}, "approved text")
    assert [a.value for a in p.actions()] == ["approved text", None]


def test_lever_additional_information_box_gets_only_the_approved_letter():
    ad = LeverAdapter()
    f = {"label": "Add a cover letter or anything else you want to share.", "name": "comments",
         "id": "additional-information", "type": "textarea", "required": False,
         "locator": "#additional-information"}
    with_letter = ad.resolve_field(f, PlanContext(job_key="k", cover_letter_text="My letter"))
    assert with_letter.value == "My letter" and with_letter.source == COVER_TEXT_SOURCE
    without = ad.resolve_field(f, PlanContext(job_key="k"))
    assert without.value is None and without.needs_user


# --------------------------------------------------------------------------- API

def test_api_save_status_approve_and_pdf(client):
    key = seed()
    assert client.post(f"/api/jobs/{key}/cover-letter/save", json={"text": 5}).status_code == 400
    assert client.post("/api/jobs/missing/cover-letter/save", json={"text": GOOD}).status_code == 404
    r = client.post(f"/api/jobs/{key}/cover-letter/save", json={"text": FLAGGED})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["saved"] and not st["approved"] and st["checks"]["status"] == "needs_review"
    assert client.get(f"/api/jobs/{key}/cover-letter/saved").json()["sha256"] == st["sha256"]
    r = client.post(f"/api/jobs/{key}/cover-letter/approve", json={"sha256": st["sha256"]})
    assert r.status_code == 409 and r.json()["detail"]["needs_confirmation"]
    assert client.post(f"/api/jobs/{key}/cover-letter/approve",
                       json={"sha256": st["sha256"], "confirm_flags": "yes"}).status_code == 400
    r = client.post(f"/api/jobs/{key}/cover-letter/approve",
                    json={"sha256": st["sha256"], "confirm_flags": True})
    assert r.status_code == 200 and r.json()["approved"] and r.json()["usable"]
    pdf = client.get(st["pdf_url"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert client.get("/api/artifacts/999/cover-letter/pdf").status_code == 404


def test_status_offers_a_letter_saved_with_an_older_version():
    key = seed()
    CL.save(key, GOOD, answers=BANK)
    aid, v = store.create_artifact(key, "backend_sde")
    store.update_artifact(aid, status="approved", pdf_path=art_of(key)["pdf_path"])
    store.link_job_artifact(key, aid)
    st = CL.status(key)
    assert st["artifact_id"] == aid and not st["saved"]
    assert st["earlier"]["artifact_version"] == 1 and st["earlier"]["text"] == GOOD
