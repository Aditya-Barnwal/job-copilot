"""Light checks on the React UI source (web/): every screen exists and calls the
endpoints it needs, demo mode switches actions off, the safety copy is there,
and the build settings keep the strict CSP. The built page itself is covered
by tests/test_api.py and the Playwright e2e test (tests/test_ui_e2e.py)."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SRC = WEB / "src"


def read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


ALL = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.ts*"))


def test_every_screen_is_routed():
    app = read("App.tsx")
    for path in ("/today", "/needs-you", "/tracker", "/matches", "/watchlist", "/hidden", "/runs",
                 "/resumes", "/facts", "/portals"):
        assert f'"{path}"' in app, path
    assert "<ReviewDrawer />" in app


def test_screens_call_their_endpoints():
    for endpoint in ("/api/queue?lane=", "/api/matches?", "/api/excluded?", "/api/tracker", "/api/runs",
                     "/api/runs/daily", "/autofill/sessions", "/handover", "/api/autofill/needs-you",
                     "/api/answers/unanswered", "/api/answers/save", "/api/answers/review", "/api/facts",
                     "/confirm", "/api/resumes", "/api/tracker/export.csv", "/api/portals/open",
                     "/tailor/v2", "/approve", "/reject", "/edit", "/review", "/cover-letter", "/fill-plan",
                     "/status", "/api/answer-pack", "/api/profile", "/api/user-yoe", "/api/resume"):
        assert endpoint in ALL, endpoint


def test_tracker_columns_in_order():
    src = read("screens/Tracker.tsx")
    labels = re.findall(r'\["(\w+)", "([^"]+)"\]', src.split("const NEXT")[0])
    assert [s for s, _ in labels] == ["found", "tailored", "ready_to_submit", "submitted", "oa",
                                      "interview", "offer", "rejected"]
    assert "Re-apply cooldowns" in src and "Reminders" in src and "Export CSV" in src


def test_open_and_submit_explains_the_submit_lock_and_never_submits():
    src = read("screens/review/ApplyTab.tsx")
    assert "Unlock Submit" in src
    assert "Until you unlock, even your own click on Submit is blocked" in src
    assert "Mark submitted" in src
    for status in ("ready_to_submit", "needs_you", "blocked", "submitted", "error"):
        assert status in src, status
    # the UI has no way to submit a form: only the documented endpoints are called
    assert not re.search(r"/submit\b", ALL.replace("/api/autofill/sessions", ""))


def test_demo_mode_disables_refused_actions_and_fake_links():
    api = read("lib/api.ts")
    assert "Not available in the public demo — run Job-Copilot locally to tailor" in api
    assert "Sample job — the link is not real in the demo" in api
    assert ".demo-banner" in read("lib/app.tsx")           # the server-injected banner switches demo on
    card = read("components/JobCard.tsx")
    assert "DEMO_LINK_TIP" in card and "disabled" in card
    # every screen with a write action checks demo mode
    for rel in ("screens/review/ResumeTab.tsx", "screens/review/ApplyTab.tsx", "screens/review/AnswersTab.tsx",
                "screens/review/CoverLetterTab.tsx", "screens/NeedsYou.tsx", "screens/Facts.tsx",
                "screens/Portals.tsx", "screens/Resumes.tsx", "screens/Today.tsx"):
        assert "useDemo()" in read(rel), rel


def test_keyboard_support():
    card = read("components/JobCard.tsx")
    for key in ('j:', 'k:', 'o:', 'x:'):
        assert key in card, key
    drawer = read("screens/review/ReviewDrawer.tsx")
    assert '"Escape"' in drawer and 'aria-modal="true"' in drawer and "trapTab" in drawer
    shell = read("components/Shell.tsx")
    assert "Skip to content" in shell and '"?"' in shell
    css = read("index.css")
    assert ":focus-visible" in css and "prefers-reduced-motion" in css


def test_themes_and_fonts():
    css = read("index.css")
    assert '[data-theme="dark"]' in css and "@fontsource-variable/fraunces" in css
    assert "@fontsource-variable/inter" in css
    init = (WEB / "public" / "theme-init.js").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in init and "localStorage" in init


def test_dependencies_are_pinned_and_locked():
    pkg = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    for section in ("dependencies", "devDependencies"):
        for name, ver in pkg[section].items():
            assert re.fullmatch(r"\d+\.\d+\.\d+", ver), f"{name} is not pinned: {ver}"
    assert (WEB / "package-lock.json").exists()
    cfg = (WEB / "vite.config.ts").read_text(encoding="utf-8")
    assert 'outDir: "../server/static/dist"' in cfg and "changeOrigin: false" in cfg
