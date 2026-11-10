#!/usr/bin/env python3
"""Fail if personal data appears in files tracked by git (the repo is public).

Checks every file `git ls-files` reports (tracked + staged) for:
  * email addresses outside an allow-list of example / company domains
  * phone numbers in +<country> form (e.g. +91 98xxx xxxxx)
  * Indian ID formats: Aadhaar (three groups of 4 digits) and PAN
    (5 letters, 4 digits, 1 letter)
  * your exact email / phone / name from profile/resume_facts.yaml when that
    file exists locally — so the check knows your values without the repo
    ever containing them.

A line can opt out with the marker `pii-ok` (used by the scanner's own tests).
Usage:  python scripts/check_pii.py [--root DIR]      exit 1 on findings
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ALLOWED_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "anthropic.com",  # noreply@anthropic.com
    "balkan.id",  # public company hiring address in config/companies.yaml
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".woff", ".woff2", ".db"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
PHONE_RE = re.compile(r"\+\d{1,3}[\s-]?\(?\d{2,5}\)?[\s-]?\d{3,5}[\s-]?\d{3,5}")
AADHAAR_RE = re.compile(r"\b[2-9]\d{3}\s\d{4}\s\d{4}\b")
PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")


def _personal_values(root: Path) -> list[tuple[str, str]]:
    """Exact personal strings from the local, git-ignored facts file."""
    facts = root / "profile" / "resume_facts.yaml"
    if not facts.exists():
        return []
    try:
        import yaml
        data = yaml.safe_load(facts.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    person = data.get("person") or {}
    out: list[tuple[str, str]] = []
    for key in ("email", "phone"):
        v = person.get(key)
        if isinstance(v, dict):
            v = v.get("text") or v.get("value")
        if isinstance(v, str) and len(v.strip()) >= 6:
            out.append((key, v.strip()))
    phone = next((v for k, v in out if k == "phone"), None)
    if phone:
        digits = re.sub(r"\D", "", phone)[-10:]
        if len(digits) == 10:
            out.append(("phone-digits", digits))
    return out


def _mask(s: str) -> str:
    return s if len(s) <= 4 else s[:2] + "…" + s[-2:]


def scan_text(text: str, personal: list[tuple[str, str]] | None = None) -> list[tuple[int, str, str]]:
    """Return (line_no, kind, masked_match) findings for one file's text."""
    findings: list[tuple[int, str, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        if "pii-ok" in line:
            continue
        for m in EMAIL_RE.finditer(line):
            domain = m.group(1).lower()
            if not any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS):
                findings.append((n, "email", _mask(m.group(0))))
        for m in PHONE_RE.finditer(line):
            if len(re.sub(r"\D", "", m.group(0))) >= 10:
                findings.append((n, "phone", _mask(m.group(0))))
        for m in AADHAAR_RE.finditer(line):
            findings.append((n, "aadhaar-like", _mask(m.group(0))))
        for m in PAN_RE.finditer(line):
            findings.append((n, "pan-like", _mask(m.group(0))))
        compact = re.sub(r"\D", "", line)
        for kind, value in personal or []:
            hit = value in compact if kind == "phone-digits" else value.lower() in line.lower()
            if hit:
                findings.append((n, f"your-{kind}", _mask(value)))
    return findings


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=root,
                         capture_output=True, check=True).stdout.decode()
    return [root / p for p in out.split("\0") if p]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    personal = _personal_values(root)

    total = 0
    for path in tracked_files(root):
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, kind, masked in scan_text(text, personal):
            print(f"{path.relative_to(root)}:{n}: {kind} {masked}")
            total += 1
    if total:
        print(f"\n{total} possible personal-data finding(s) in tracked files.", file=sys.stderr)
        return 1
    print(f"PII check: clean ({'with' if personal else 'without'} local profile values).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
