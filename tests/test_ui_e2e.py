"""End-to-end check of the built React UI (local only).

FastAPI serves server/static/dist from a fixture DB (tests/fixtures + the fake
profile.example, built the way the demo builds it, but NOT in demo mode so the
autofill routes are live). The browser backend is a fake (as in
tests/test_autofill_service.py), so no real form is opened and nothing is ever
submitted: the "user" unlocks Submit and then marks the job submitted by hand.

Flow: Today queue -> review card -> Open & Submit -> Unlock Submit -> Mark
submitted -> Tracker shows Submitted. Plus a keyboard-only pass.

Skips cleanly when the UI isn't built (make ui), Playwright isn't installed or
Google Chrome is missing.
"""
from __future__ import annotations

import re
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

from jobcopilot import demo, llm, store
from jobcopilot.autofill import service
from jobcopilot.autofill.plan import FieldAction, FillPlan, Outcome

ROOT = Path(__file__).resolve().parent.parent
DIST_INDEX = ROOT / "server" / "static" / "dist" / "index.html"

pytestmark = pytest.mark.skipif(not DIST_INDEX.exists(), reason="UI not built (run `make ui`)")
sync_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")


class FakeBackend:
    """Stands in for the headed browser: 'fills' the form, then reports an open tab
    until the test ends. Records hand-over requests; never submits anything."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.open = True

    def snapshot(self, url, ats, cfg, adapter=None):
        return {"source": "fake", "url": url, "steps": [[]]}

    def open_headed(self, cfg):
        self.calls.append("open_headed")
        return {"fake": True}

    def execute(self, handle, adapter, plan, ctx, cfg):
        self.calls.append("execute")
        return Outcome("ready", "stopped before submit", 12, 1, ["Why do you want to work here?"],
                       "#submit_app", None, "Application")

    def handover(self, handle):
        self.calls.append("handover")
        return True

    def tab_state(self, handle):
        return ("https://jobs.example.test/apply/123", "Apply for this job") if self.open else None

    def screenshot(self, handle, path):
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        return str(path)

    def wait(self, handle, seconds):
        time.sleep(0.05)

    def close(self, handle):
        self.calls.append("close")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(tmp_path, monkeypatch, isolated_data_dir):
    """The app on a free local port, over a fixture DB, with the fake backend."""
    def _refuse(*_a, **_kw):
        raise AssertionError("the e2e test must not call an LLM")
    monkeypatch.setattr(llm, "complete", _refuse)
    monkeypatch.setattr(llm, "complete_json", _refuse)
    monkeypatch.delenv("JOBCOPILOT_DEMO", raising=False)
    monkeypatch.delenv("JOBCOPILOT_ALLOWED_HOSTS", raising=False)
    profile = tmp_path / "profile"
    shutil.copytree(ROOT / "profile.example", profile)
    monkeypatch.setenv("JOBCOPILOT_PROFILE_DIR", str(profile))
    state = demo.DemoState(root=tmp_path, db_path=store.resolve_db_path(), profile_dir=profile)
    built = demo.build_db(state)
    assert built["showcase"], "fixture showcase was not seeded"
    with store.connect() as conn:                       # no sample fill window left open
        conn.execute("DELETE FROM autofill_sessions")

    fake = FakeBackend()
    monkeypatch.setattr(service, "BACKEND", fake)
    monkeypatch.setattr(service, "_LIVE", {})
    monkeypatch.setattr(service, "adapter_for", lambda url, ats: None)

    def plan(job, cfg, db):
        return FillPlan(job["key"], "greenhouse", job.get("apply_url") or job.get("url") or "",
                        [[FieldAction("#first_name", "First Name", "fill", "Alex", "answers.yaml:personal.first_name",
                                      1.0, False)]])
    monkeypatch.setattr(service, "_fresh_plan", plan)
    cfg = service.settings()
    cfg["timeouts"] = dict(cfg["timeouts"], watch_poll_sec=0.05, watch_min=5)
    monkeypatch.setattr(service, "settings", lambda: cfg)

    from server import app as server_app
    monkeypatch.setattr(server_app, "setup_logging", lambda _dir: None)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(server_app.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield {"base": f"http://127.0.0.1:{port}", "fake": fake, "showcase": built["showcase"]}
    finally:
        fake.open = False                                   # ends the confirmation watch
        server.should_exit = True
        t.join(timeout=10)


@pytest.fixture
def page(served):
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Google Chrome is not available: {type(exc).__name__}")
        ctx = browser.new_context(viewport={"width": 1360, "height": 900})
        pg = ctx.new_page()
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "404" not in m.text else None)
        pg.set_default_timeout(10000)
        yield pg
        assert not errors, errors
        browser.close()


def test_queue_review_open_unlock_mark_submitted_tracker(served, page):
    key = served["showcase"]["ready"]                      # has an approved tailored résumé
    base = served["base"]
    page.goto(base + "/")
    page.wait_for_url(re.compile(r"/today$"))
    page.get_by_role("heading", name="Today's shortlist").wait_for()
    card = page.locator(f'article[id="job-{key}"]')
    card.wait_for()
    title = card.locator("h3").inner_text()
    card.get_by_role("button", name="Review").click()

    dialog = page.get_by_role("dialog")
    dialog.wait_for()
    assert title in dialog.get_by_role("heading", level=2).inner_text()
    dialog.get_by_text("Tailored résumé").wait_for()
    dialog.get_by_text(re.compile(r"This is the résumé autofill attaches")).wait_for()   # approved version shown

    dialog.get_by_role("tab", name="Open & Submit").click()
    dialog.get_by_role("button", name="Open & fill the form").click()
    dialog.get_by_role("heading", name="Ready for you").wait_for(timeout=15000)
    assert dialog.get_by_text("Why do you want to work here?").is_visible()
    assert dialog.get_by_text("Until you unlock, even your own click on Submit is blocked").is_visible()
    assert served["fake"].calls[:2] == ["open_headed", "execute"]
    page.locator('img[alt="Screenshot of the filled form"]').wait_for()

    dialog.get_by_role("button", name="Unlock Submit").click()
    dialog.get_by_text("Submit unlocked — press it in the browser").wait_for()
    for _ in range(100):
        if "handover" in served["fake"].calls:
            break
        time.sleep(0.05)
    assert "handover" in served["fake"].calls             # the guard was lifted; nothing was clicked

    dialog.get_by_role("button", name="Mark submitted").click()
    page.get_by_text("Marked as submitted").wait_for()
    assert store.canonical_status(store.get_job(key)["status"]) == "submitted"

    page.keyboard.press("Escape")
    dialog.wait_for(state="detached")
    page.get_by_role("link", name="Tracker").first.click()
    page.get_by_role("heading", name="Tracker", level=1).wait_for()
    col = page.get_by_role("listitem", name=re.compile(r"^Submitted: \d+"))
    col.get_by_text(title).first.wait_for()
    assert col.locator(f'article[id="job-{key}"]').count() == 1


def test_keyboard_only_navigation(served, page):
    base = served["base"]
    page.goto(base + "/today")
    page.get_by_role("heading", name="Today's shortlist").wait_for()
    page.locator("article[id^='job-']").first.wait_for()

    page.keyboard.press("Tab")                                  # first stop: the skip link
    assert page.evaluate("document.activeElement.textContent") == "Skip to content"

    page.keyboard.press("Escape")
    page.locator("body").focus()
    page.keyboard.press("j")
    first = page.evaluate("document.activeElement.id")
    assert first.startswith("job-")
    page.keyboard.press("j")
    second = page.evaluate("document.activeElement.id")
    assert second.startswith("job-") and second != first
    assert page.evaluate("document.activeElement.matches(':focus-visible')")
    page.keyboard.press("k")
    assert page.evaluate("document.activeElement.id") == first

    page.keyboard.press("Enter")                                # open the review card
    dialog = page.get_by_role("dialog")
    dialog.wait_for()
    assert page.evaluate("document.activeElement.getAttribute('role')") == "dialog"
    page.keyboard.press("4")
    assert dialog.get_by_role("tab", name="Open & Submit").get_attribute("aria-selected") == "true"
    page.keyboard.press("j")                                    # next job, drawer stays open
    page.wait_for_function("new URL(location.href).searchParams.get('job') !== %r" % first[4:])
    for _ in range(25):                                         # Tab stays inside the dialog
        page.keyboard.press("Tab")
        assert page.evaluate("!!document.activeElement.closest('[role=dialog]')")
    page.keyboard.press("Escape")
    dialog.wait_for(state="detached")
    assert page.evaluate("document.activeElement.id").startswith("job-")   # focus returns to the list

    page.keyboard.press("?")
    shortcuts = page.get_by_role("dialog", name="Keyboard shortcuts")
    shortcuts.wait_for()
    page.keyboard.press("Escape")
    shortcuts.wait_for(state="detached")

    page.keyboard.press("g")
    page.keyboard.press("p")
    page.get_by_role("heading", name="Tracker", level=1).wait_for()
    page.keyboard.press("g")
    page.keyboard.press("y")
    page.get_by_role("heading", name="Needs you", level=1).wait_for()
    # a question can be answered and saved with the keyboard alone
    field = page.get_by_label(re.compile("Kafka"))
    field.focus()
    field.select_option(index=2)
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.textContent").strip() == "Save to answers bank"
    page.keyboard.press("Enter")
    page.get_by_text(re.compile(r"Saved in your answers bank")).wait_for()


def test_hide_then_unhide_from_hidden_by_you(served, page):
    base = served["base"]
    page.goto(base + "/today")
    page.get_by_role("heading", name="Today's shortlist").wait_for()
    cards = page.locator("article[id^='job-']")
    cards.nth(3).wait_for()
    # Not interested + Undo in the toast
    undo_key = cards.nth(3).get_attribute("id")[4:]
    cards.nth(3).get_by_role("button", name=re.compile("^Not interested")).click()
    page.get_by_role("button", name="Undo").click()
    page.get_by_text("Undone.").wait_for()
    assert store.get_job(undo_key)["status"] == "new"
    # hide one for real
    card = cards.nth(1)
    key = card.get_attribute("id")[4:]
    title = card.locator("h3").inner_text()
    card.get_by_role("button", name=re.compile("^Not interested")).click()
    page.get_by_text(re.compile("Find it under Hidden roles")).wait_for()
    page.locator(f'article[id="job-{key}"]').wait_for(state="detached")     # off today's list
    assert store.get_job(key)["status"] == "not_interested"

    page.get_by_role("link", name="Hidden roles").first.click()
    page.get_by_role("heading", name="Hidden roles", level=1).wait_for()
    page.get_by_role("tab", name=re.compile("^Hidden by you")).click()
    row = page.locator(f'li[id="hidden-{key}"]')
    row.wait_for()
    assert title in row.inner_text() and "Hidden" in row.inner_text()
    row.get_by_role("button", name=re.compile("^Unhide")).click()
    row.wait_for(state="detached")
    page.get_by_text(re.compile("is back")).wait_for()
    assert store.canonical_status(store.get_job(key)["status"]) == "found"
