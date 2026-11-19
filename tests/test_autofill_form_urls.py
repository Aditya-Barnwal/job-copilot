"""Autofill opens the application FORM, not the description (service.form_url /
application_url), and the service's outcome rule: a fill that filled nothing is
never "ready", whatever the adapter said. No network, no browser."""
from __future__ import annotations

from pathlib import Path

import pytest

from jobcopilot import store
from jobcopilot.autofill import service
from jobcopilot.autofill.plan import NO_FORM_REASON, Outcome

LEVER = "https://jobs.lever.co/cred/7301faf3-d7ec-40d4-b49a-07ed20fc9691"
ASHBY = "https://jobs.ashbyhq.com/notion/42f18ccd-c4c8-4a85-8c1f-de12c575fe87"


@pytest.mark.parametrize("url,want", [
    (LEVER, LEVER + "/apply"),
    (LEVER + "/", LEVER + "/apply"),
    (LEVER + "?lever-source=x", LEVER + "/apply?lever-source=x"),
    (LEVER + "/apply", LEVER + "/apply"),
    ("https://jobs.eu.lever.co/acme/7301faf3-d7ec-40d4-b49a-07ed20fc9691",
     "https://jobs.eu.lever.co/acme/7301faf3-d7ec-40d4-b49a-07ed20fc9691/apply"),
    (ASHBY, ASHBY + "/application"),
    (ASHBY + "/application", ASHBY + "/application"),
    ("https://attero.keka.com/careers/jobdetails/84493", "https://attero.keka.com/careers/applyjob/84493"),
    ("https://attero.keka.com/careers/applyjob/84493", "https://attero.keka.com/careers/applyjob/84493"),
    # the form is on these pages already
    ("https://job-boards.greenhouse.io/acme/jobs/123", "https://job-boards.greenhouse.io/acme/jobs/123"),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=1",
     "https://boards.greenhouse.io/embed/job_app?for=acme&token=1"),
    ("https://adobe.wd5.myworkdayjobs.com/en-US/external/job/X/Y_R1",
     "https://adobe.wd5.myworkdayjobs.com/en-US/external/job/X/Y_R1"),
    ("https://careers.example.com/jobs/1", "https://careers.example.com/jobs/1"),
    ("https://jobs.lever.co/cred", "https://jobs.lever.co/cred"),          # a board, not a posting
])
def test_form_url(url, want):
    assert service.form_url(url) == want


def test_target_url_prefers_apply_link_then_maps_to_the_form():
    assert service.target_url({"apply_url": LEVER, "url": "https://x.example/j"}) == LEVER + "/apply"
    assert service.target_url({"apply_url": "", "url": ASHBY}) == ASHBY + "/application"
    assert service.target_url({}) == ""


def _seed_company(name, slug, region=None):
    with store.connect() as conn:
        conn.execute("INSERT INTO companies(name, normalized_name, ats, ats_slug, ats_region) "
                     "VALUES (?, ?, 'greenhouse', ?, ?)", (name, name.lower(), slug, region))


def test_gh_jid_employer_page_opens_the_greenhouse_embed_form():
    _seed_company("Okta", "okta")
    url = "https://www.okta.com/company/careers/opportunity/7906186?gh_jid=7906186"
    job = {"key": "k", "company": "Okta", "source": "greenhouse", "url": url, "apply_url": url}
    assert service.application_url(job) == "https://boards.greenhouse.io/embed/job_app?for=okta&token=7906186"
    _seed_company("Euco", "euco", "eu")
    job = dict(job, company="Euco", url=url.replace("okta.com", "euco.example"), apply_url="")
    assert service.application_url(job).startswith("https://boards.eu.greenhouse.io/embed/job_app?for=euco&")


def test_gh_jid_without_a_known_board_is_left_alone():
    url = "https://careers.unknown.example/jobs?gh_jid=55"
    job = {"key": "k", "company": "Nobody", "source": "speedyapply", "url": url}
    assert service.application_url(job) == url


def test_plan_for_job_plans_the_lever_form_url(monkeypatch):
    from jobcopilot.filters import FilterResult
    from jobcopilot.models import Job
    seen = []

    class Fake:
        def snapshot(self, url, ats, cfg, adapter=None):
            seen.append(url)
            return {"source": "page", "url": url, "steps": [[]]}

    monkeypatch.setattr(service, "BACKEND", Fake())
    monkeypatch.setattr(service, "adapter_for", lambda url, ats: None)
    j = Job(source="lever", company="Cred", company_sector="tech", external_id="L1", title="SE",
            location_text="Bengaluru", workplace_type="onsite", country_bucket="IN", url=LEVER,
            description_text="x")
    store.upsert_jobs([FilterResult(job=j, package_estimated=False, country_uncertain=False)])
    plan = service.plan_for_job(j.key)
    assert seen == [LEVER + "/apply"] and plan.url == LEVER + "/apply"


# --------------------------------------------------------------------------- the outcome rule, service side

def test_service_never_reports_ready_with_nothing_filled():
    od = service.ready_needs_a_fill(Outcome("ready", "filled; review", 0, 0, [], "#apply-job").to_dict())
    assert od["status"] == "needs_you" and od["reason"] == NO_FORM_REASON
    od = service.ready_needs_a_fill(Outcome("ready", "filled", 2, 0).to_dict())
    assert od["status"] == "ready"
    od = service.ready_needs_a_fill(Outcome("blocked", "missing", 0, 0).to_dict())
    assert od["status"] == "blocked"


def test_zero_fill_session_is_needs_you_not_ready_to_submit(monkeypatch):
    """The Attero case: an adapter returns 'ready' with 0 fields on a description page."""
    from jobcopilot.filters import FilterResult
    from jobcopilot.models import Job

    class Fake:
        def __init__(self):
            self.calls = []

        def snapshot(self, url, ats, cfg, adapter=None):
            return {"source": "page", "url": url, "steps": [[]]}

        def open_headed(self, cfg):
            self.calls.append("open_headed")
            return {}

        def execute(self, handle, adapter, plan, ctx, cfg):
            return Outcome("ready", "filled; review and press Submit yourself", 0, 0, [], "#apply-job")

        def tab_state(self, handle):
            return None

        def screenshot(self, handle, path):
            Path(path).write_bytes(b"png")
            return str(path)

        def wait(self, handle, s):
            pass

        def close(self, handle):
            pass

        def handover(self, handle):
            return True

    fake = Fake()
    monkeypatch.setattr(service, "BACKEND", fake)
    monkeypatch.setattr(service, "adapter_for", lambda url, ats: None)
    monkeypatch.setattr(service, "_LIVE", {})
    j = Job(source="keka", company="Attero", company_sector="tech", external_id="84493", title="AI/ML Developer",
            location_text="Noida", workplace_type="onsite", country_bucket="IN",
            url="https://attero.keka.com/careers/jobdetails/84493", description_text="x")
    store.upsert_jobs([FilterResult(job=j, package_estimated=False, country_uncertain=False)])
    sid = service.launch_fill(j.key, background=False)
    s = service.session_status(sid)
    assert s["outcome"]["status"] == "needs_you" and s["outcome"]["reason"] == NO_FORM_REASON
    job = store.get_job(j.key)
    assert store.canonical_status(job["status"]) != "ready_to_submit" and job["fill_status"] == "needs_you"
