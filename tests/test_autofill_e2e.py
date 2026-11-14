"""End to end through the real path: service.plan_for_job -> PlaywrightBackend.execute
-> K's session/SafePage/guard -> each ATS adapter, against LOCAL fixture forms
(127.0.0.1 only) with the fake profile.example. The only stand-ins are the
browser profile (a throwaway headless one, never ~/.jobcopilot) and the adapter
lookup (the fixtures live on 127.0.0.1, not on the ATS hosts).

The invariant: every non-GET the server sees is on the adapter's allow-list
(step saves, upload handshakes) and none is a submit; nothing is submitted."""
from __future__ import annotations

import re

import pytest

from jobcopilot import llm, store
from jobcopilot.autofill import service
from jobcopilot.autofill.adapters.ashby import AshbyAdapter
from jobcopilot.autofill.adapters.greenhouse import GreenhouseAdapter
from jobcopilot.autofill.adapters.lever import LeverAdapter
from jobcopilot.autofill.adapters.workday import WorkdayAdapter
from jobcopilot.autofill.session import launch_ephemeral
from jobcopilot.config import ROOT
from jobcopilot.filters import FilterResult
from jobcopilot.models import Job
from test_adapters import LEVER_PATH, _Server  # noqa: E402 - the request-counting fixture server

pytest.importorskip("playwright.sync_api")

EXAMPLE_EMAIL = "alex@example.com"          # profile.example's (fake) address

CASES = [
    ("greenhouse", "/greenhouse.html", GreenhouseAdapter),
    ("lever", LEVER_PATH, LeverAdapter),
    ("ashby", "/ashby.html", AshbyAdapter),
    ("workday", "/workday_apply.html", WorkdayAdapter),
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    def _refuse(*_a, **_kw):
        raise AssertionError("autofill must not call an LLM")
    monkeypatch.setattr(llm, "complete", _refuse)
    monkeypatch.setattr(llm, "complete_json", _refuse)
    monkeypatch.setenv("JOBCOPILOT_PROFILE_DIR", str(ROOT / "profile.example"))
    monkeypatch.setattr(service, "_LIVE", {})


def _seed(url: str) -> str:
    job = Job(source="career_page", company="Examplecorp", company_sector="tech", external_id="e2e-1",
              title="Software Engineer", location_text="Bengaluru", workplace_type="onsite",
              country_bucket="IN", url=url, description_text="Python, SQL")
    store.upsert_jobs([FilterResult(job=job, package_estimated=False, country_uncertain=False)])
    aid, v = store.create_artifact(job.key)
    d = store.applications_dir() / "e2e" / f"v{v}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "resume.pdf").write_bytes(b"%PDF-1.4\n% e2e fixture\n")
    store.update_artifact(aid, status="approved", pdf_path=str(d / "resume.pdf"))
    store.link_job_artifact(job.key, aid)
    return job.key


@pytest.mark.parametrize("ats,path,cls", CASES, ids=[c[0] for c in CASES])
def test_fill_end_to_end_never_submits(ats, path, cls, monkeypatch):
    srv = _Server()
    monkeypatch.setattr(service, "adapter_for", lambda url, a: cls())
    # the store rightly refuses loopback URLs, so the job keeps a public-looking one
    # and only the form lookup is pointed at the local server
    monkeypatch.setattr(service, "target_url", lambda job: srv.base + path)
    try:
        key = _seed("https://careers.example.com/jobs/e2e-1")
        cfg = service.settings()
        cfg["timeouts"] = dict(cfg["timeouts"], page_load_sec=15)
        try:
            plan = service.plan_for_job(key, cfg=cfg)
            handle = {"k": launch_ephemeral(headless=True)}
        except Exception as exc:  # noqa: BLE001 - no Chrome/Chromium on this machine
            if "Executable" in str(exc) or "browser" in str(exc).lower():
                pytest.skip(f"no browser: {type(exc).__name__}")
            raise
        try:
            job = store.get_job(key)
            outcome = service.BACKEND.execute(handle, cls(), plan,
                                              service.plan_context(job, plan.url, plan.files), cfg)
            od = service._outcome_dict(outcome)
            page = handle["k"].page
            emails = page.eval_on_selector_all(
                "input[type=email], input[name*=email i], input[id*=email i]", "els => els.map(e => e.value)")
            uploaded = page.eval_on_selector_all("input[type=file]",
                                                 "els => els.reduce((n, e) => n + e.files.length, 0)")
            final_url = page.url
        finally:
            service.BACKEND.close(handle)

        assert od["status"] in ("ready", "needs_you", "blocked"), od   # blocked = required Qs left for the user
        assert (od.get("filled") or 0) > 0, od
        assert EXAMPLE_EMAIL in emails or ats == "workday", emails   # Workday: email is on the sign-in step
        assert uploaded >= 1 or ats == "workday", "résumé not attached"
        # every non-GET is an allowed helper call, never a submit
        allow = [re.compile(p) for p in cls.allow_post_patterns]
        submit = [re.compile(p) for p in cls.submit_patterns]
        for p in srv.posts:
            full = srv.base + p
            assert not any(s.search(full) or s.search(p) for s in submit), f"submit POST reached server: {p}"
            assert any(a.search(full) or a.search(p) for a in allow), f"unexpected POST: {p}"
        assert "success" not in final_url and "thank" not in final_url.lower()
        # values never land in the stored outcome
        assert EXAMPLE_EMAIL not in repr(od)
    finally:
        srv.close()
