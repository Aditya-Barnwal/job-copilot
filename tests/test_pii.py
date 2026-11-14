"""PII guard for tracked files (the repo is public)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_pii", Path(__file__).resolve().parent.parent / "scripts" / "check_pii.py")
check_pii = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_pii)  # type: ignore[union-attr]


def kinds(text, personal=None):
    return [k for _, k, _ in check_pii.scan_text(text, personal)]


def test_flags_personal_email_but_not_allowlisted():
    assert kinds("contact: someone@gmail.com") == ["email"]  # pii-ok
    assert kinds("alex@example.com and noreply@anthropic.com") == []


def test_flags_phone_and_indian_ids():
    assert "phone" in kinds("call +91 98765 43210")  # pii-ok
    assert "aadhaar-like" in kinds("id 2345 6789 0123")  # pii-ok
    assert "pan-like" in kinds("pan ABCDE1234F")  # pii-ok


def test_ignores_job_ids_and_plain_numbers():
    assert kinds('"id": 4751768005, "count": 1234567890') == []


def test_exact_personal_values_from_local_profile():
    personal = [("email", "me@mydomain.dev"), ("phone-digits", "9876543210")]  # pii-ok
    assert "your-email" in kinds("reach me at ME@mydomain.dev", personal)  # pii-ok
    assert "your-phone-digits" in kinds("ph: 98765-43210", personal)  # pii-ok


def test_opt_out_marker():
    assert kinds("someone@gmail.com  # pii-ok") == []


# --- review findings #3 and #6 ------------------------------------------------
import subprocess  # noqa: E402

import pytest  # noqa: E402

# Fake values only. Each source line that holds one carries the opt-out marker
# so this file itself scans clean; the files written below don't.
FAKE_NAME = "Jane Fakename"  # pii-ok
FAKE_EMAIL = "jane@mailbox.zz"  # pii-ok
FAKE_LINKEDIN = "https://www.linkedin.com/in/jane-fakename-123/"  # pii-ok
FAKE_MOBILE = "90000 11111"  # pii-ok
PROFILE_YAML = f"""person:
  name: "{FAKE_NAME}"
  email: "{FAKE_EMAIL}"
  phone: "+91 {FAKE_MOBILE}"
  links:
    linkedin: "{FAKE_LINKEDIN}"
    github: "https://github.com/Aditya-Barnwal"
"""


def make_repo(root: Path, files: dict, profile: str | None = PROFILE_YAML) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        (p.write_bytes if isinstance(content, bytes) else p.write_text)(content)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    if profile is not None:   # untracked, like the real git-ignored profile/
        (root / "profile").mkdir()
        (root / "profile" / "resume_facts.yaml").write_text(profile)
    return root


def run_scan(root: Path, capsys) -> tuple[int, str]:
    code = check_pii.main(["--root", str(root)])
    return code, capsys.readouterr().out


def test_tracked_documents_and_databases_fail_outside_fixtures(tmp_path, capsys):
    root = make_repo(tmp_path, {
        "resume.pdf": b"%PDF-1.4 binary",
        "docs/cv.docx": b"PK\x03\x04",
        "old/cv.doc": b"\xd0\xcf\x11\xe0",
        "backup.db": b"SQLite format 3\x00",
        "data.sqlite": b"SQLite format 3\x00",
        "sheet.xlsx": b"PK\x03\x04",
        "tests/fixtures/sample.pdf": b"%PDF-1.4 fixture",
        "README.md": "nothing personal here\n",
    }, profile=None)
    code, out = run_scan(root, capsys)
    assert code == 1
    for rel in ("resume.pdf", "docs/cv.docx", "old/cv.doc", "backup.db", "data.sqlite", "sheet.xlsx"):
        assert f"{rel}: tracked-document" in out, out
    assert "tests/fixtures/sample.pdf" not in out


def test_non_utf8_files_are_scanned_not_skipped(tmp_path, capsys):
    root = make_repo(tmp_path, {"export.csv": b"caf\xe9," + FAKE_EMAIL.encode() + b"\n"},
                     profile=None)
    code, out = run_scan(root, capsys)
    assert code == 1 and "export.csv:1: email" in out, out


def test_name_and_links_from_local_profile(tmp_path, capsys):
    root = make_repo(tmp_path, {
        "ABOUT.md": f"Built by {FAKE_NAME}.\nSee {FAKE_LINKEDIN.replace('https://www.', 'http://')}\n",
        "LICENSE": f"Copyright (c) 2026 {FAKE_NAME}\n",
        "config/sources.yaml": 'ua: "job-copilot/2.0 (+https://github.com/Aditya-Barnwal/job-copilot)"\n',
    })
    code, out = run_scan(root, capsys)
    assert code == 1
    assert "ABOUT.md:1: your-name" in out and "ABOUT.md:2: your-link" in out, out
    assert "LICENSE" not in out              # the name is expected in LICENSE
    assert "config/sources.yaml" not in out  # the public GitHub handle is allow-listed


def test_bare_indian_mobile_without_country_code(tmp_path, capsys):
    root = make_repo(tmp_path, {"contact.txt": f"Phone: {FAKE_MOBILE}\n"}, profile=None)
    code, out = run_scan(root, capsys)
    assert code == 1 and "contact.txt:1: phone" in out, out


@pytest.mark.parametrize("line", [
    "Phone: 90000 11111",   # pii-ok
    "mobile 9000011111",   # pii-ok
    "call 90000-11111.",   # pii-ok
    '"phone": "9000011111"',   # pii-ok
    "Jane,9000011111,Bengaluru",   # pii-ok
])
def test_bare_mobile_patterns_flagged(line):
    assert "phone" in kinds(line)


@pytest.mark.parametrize("line", [
    '"id": 7012345678',
    '"requisition_id": "6012345678"',
    '"internal_job_id": 8012345678,',
    '"jobId": 9012345678',
    "gh_jid=7012345678",
    "id: 7012345678",
    "https://boards.greenhouse.io/acme/jobs/7012345678",
    "score 0.7012345678",
    '"updated_at": 1749600000000',
    "req-7012345678",
])
def test_ats_ids_urls_and_decimals_are_not_mobiles(line):
    assert kinds(line) == [], line


# Secrets are assembled at runtime so no contiguous key sits in this file.
_K = "A1b2C3d4E5f6G7h8J9k0" * 2


@pytest.mark.parametrize("line", [
    "GROQ_API_KEY=" + "gsk" + "_" + _K,
    "key = '" + "sk" + "-ant-api03-" + _K + "'",
    "OPENAI_API_KEY=" + "sk" + "-proj-" + _K,
    "aws " + "AK" + "IA" + "ABCDEFGHIJ234567",
    "slack " + "xo" + "xb-" + "1234567890-abcdefghij",
    "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
    "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----",
    "maps " + "AI" + "za" + "Sy" + "A" * 33,
    "ADZUNA_APP_KEY=" + "9f8e7d6c5b4a39281706",
])
def test_secrets_are_flagged(line):
    assert any(k.startswith("secret") for k in kinds(line)), line


@pytest.mark.parametrize("line", [
    "GROQ_API_KEY=",
    "GROQ_API_KEY=gsk_your_key_here",
    "ADZUNA_APP_KEY=your-adzuna-key",
    'GROQ_API_KEY = os.environ.get("GROQ_API_KEY")',
    "a task-scheduler-and-runner-for-everything",
    "pip install scikit-learn",
    "TOKEN=${GITHUB_TOKEN}",
])
def test_secret_lookalikes_are_not_flagged(line):
    assert kinds(line) == [], line


def test_reserved_tlds_are_not_personal_email():
    # RFC 2606 / 6761 names can never be a real person's mailbox.
    assert kinds("https://alice:s3cret@u.test:8443/x and bob@host.invalid") == []
    assert kinds("carol@box.example and dave@dev.localhost") == []


REPO = Path(__file__).resolve().parent.parent


def _ignored(rel: str) -> bool:
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index", rel], cwd=REPO)
    assert r.returncode in (0, 1), r
    return r.returncode == 0


@pytest.mark.skipif(not (REPO / ".git").exists(), reason="not a git checkout")
def test_gitignore_covers_env_variants_databases_and_documents():
    for rel in (".env", ".env.local", ".env.production", "config/.env.dev",
                "my.db", "data.sqlite", "x.sqlite3", "resume.pdf", "cv.docx", "cv.doc"):
        assert _ignored(rel), rel
    for rel in (".env.example", "tests/fixtures/sample.pdf", "tests/fixtures/a/b.pdf"):
        assert not _ignored(rel), rel


def test_markdown_never_states_a_salary_figure():
    c = check_pii
    for bad in ("Expected CTC 18 LPA", "₹ 25,00,000", "current CTC is 12", "floor of 15 lakh"):
        assert c.MD_SALARY_RE.search(bad), bad
    for ok in ("roles with no salary are kept", "Phase 15 done", "expected CTC lives in profile/"):
        assert not c.MD_SALARY_RE.search(ok), ok
