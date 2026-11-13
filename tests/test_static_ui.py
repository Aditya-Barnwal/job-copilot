"""Light checks on the vanilla-JS UI: the v2 tabs exist, they call the v2
endpoints, errors are shown as plain text, and the markup stays compatible
with the strict CSP (no inline scripts, handlers or styles)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from server import app as server_app

STATIC = Path(server_app.STATIC_DIR)
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
NODE = shutil.which("node")


def test_tabs_and_panels_exist():
    for tab in ("today", "matches", "excluded", "tracker", "resume", "answers", "browse"):
        assert f'data-tab="{tab}"' in HTML, tab
        assert f'id="tab-{tab}"' in HTML, tab
    # Today is the default tab.
    assert re.search(r'data-tab="today" class="tab active"', HTML)
    assert re.search(r'id="tab-today" class="tab-panel active"', HTML)


def test_tab_names_and_subtitles():
    assert "Saved answers" in HTML and "Answer Pack" not in HTML
    assert "Hidden roles" in HTML and "config/filters.yaml" in HTML
    assert "Your base résumés + tailored versions per job" in HTML
    assert "Answers the app uses to fill application forms" in HTML
    # every panel has a one-line subtitle
    assert HTML.count('class="subtitle"') >= 7


def test_js_uses_v2_endpoints():
    for endpoint in ("/api/queue?lane=main", "/api/queue?lane=extended", "/api/queue?lane=near_miss",
                     "/api/matches?", "/api/excluded?", "/api/tracker", "/api/answers/review",
                     "/api/runs", "/api/runs/daily", "/history", "/status", "include_excluded"):
        assert endpoint in JS, endpoint


def test_legacy_functionality_kept():
    for el in ('id="run-ingest"', 'id="run-score"', 'id="export-csv"', 'id="f-hide-stale"',
               'id="f-hide-senior"', 'id="resume-form"', 'id="answer-form"', 'id="tailor-modal"'):
        assert el in HTML, el
    assert "/api/export.csv" in JS


def test_tracker_columns_and_actions():
    for label in ("Found", "Tailored", "Ready to submit", "Submitted", "OA", "Interview", "Offer", "Rejected"):
        assert f'"{label}"' in JS, label
    assert "✓ Submitted" in JS and 'data-action="undo"' in JS


def test_checkboxes_sit_next_to_labels():
    # checkbox inside its label, before the text; the CSS no longer gives
    # checkboxes the text-input min-width that pushed them away from the label.
    for cid in ("f-hide-stale", "f-hide-senior"):
        assert re.search(rf'<label class="chk" for="{cid}"><input id="{cid}" type="checkbox" /><span>', HTML)
    assert '.filters input:not([type="checkbox"])' in CSS
    assert re.search(r"\.chk input\[type=\"checkbox\"\]\s*\{[^}]*min-width:\s*0", CSS)


def test_demo_mode_disables_tailor_and_fake_links():
    assert "Not available in the public demo — run Job-Copilot locally to tailor" in JS
    assert "Sample job — the link is not real in the demo" in JS
    assert ".demo-banner" in JS and 'provider === "demo"' in JS


def test_no_inline_scripts_handlers_or_styles():
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", HTML)
    assert not re.search(r"\son[a-z]+\s*=", HTML, re.I)
    assert not re.search(r"\sstyle\s*=", HTML, re.I)
    assert not re.search(r"\sstyle\s*=\s*[\"']", JS)
    assert not re.search(r"\son[a-z]+\s*=\s*[\"']", JS)
    assert ".style." not in JS     # CSSOM writes are allowed but keep styling in styles.css
    assert "alert(" not in JS      # toasts, not blocking alerts


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_js_parses_and_errors_are_friendly():
    m = re.search(r"^function friendlyError\(.*?^}\n", JS, re.S | re.M)
    assert m
    script = m.group(0) + """
const out = [
  friendlyError(403, {detail: "demo: disabled — tailoring needs an LLM", demo: true}, "/api/jobs/x/tailor"),
  friendlyError(0, null, "/api/queue"),
  friendlyError(409, {detail: {error: "a daily run is already running", run_id: 3}}, "/api/runs/daily"),
  friendlyError(400, {detail: "invalid status"}, "/api/jobs/x/status"),
  friendlyError(422, {detail: [{msg: "field required"}]}, "/api/x"),
  friendlyError(500, null, "/api/x"),
];
console.log(JSON.stringify(out));
"""
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    import json
    out = json.loads(r.stdout)
    assert out[0] == "Not available in the public demo — run Job-Copilot locally to tailor"
    assert out[1] == "Couldn't reach the app — is it running?"
    assert out[2] == "a daily run is already running"
    assert out[3] == "invalid status"
    assert out[4] == "Invalid request: field required"
    assert "{" not in "".join(out)
    # whole file parses
    r = subprocess.run([NODE, "--check", str(STATIC / "app.js")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
