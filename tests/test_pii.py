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
