#!/usr/bin/env python3
"""Fail if personal data appears in files tracked by git (the repo is public).

Checks every file `git ls-files` reports (tracked + staged) for:
  * tracked documents / databases (.pdf .doc .docx .odt .db .sqlite .xls
    .xlsx …) anywhere except tests/fixtures/ — a résumé PDF or a copied
    jobcopilot.db can't be scanned reliably, so it fails outright
  * email addresses outside an allow-list of example / company domains
  * phone numbers in +<country> form (e.g. +91 98xxx xxxxx) and bare Indian
    mobiles (10 digits starting 6-9, optionally split 5+5); values of
    id-like keys, URL paths and decimals are not phones (ATS job ids)
  * Indian ID formats: Aadhaar (three groups of 4 digits) and PAN
    (5 letters, 4 digits, 1 letter)
  * secrets: Groq / OpenAI / Anthropic / Google / AWS / Slack keys, private
    key blocks, and KEY / SECRET / TOKEN / PASSWORD assignments with a real value
  * your exact email / phone / name / profile links from
    profile/resume_facts.yaml when that file exists locally — so the check
    knows your values without the repo ever containing them. The public
    GitHub handle in the repo URL is allow-listed, and the name may appear
    in LICENSE.

Files that aren't valid UTF-8 are decoded with replacement characters and
scanned anyway (never skipped).
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
# RFC 2606 / 6761 reserved TLDs: no real person can have a mailbox there.
RESERVED_EMAIL_TLDS = {"test", "example", "invalid", "localhost"}
# Images and fonts: binary, can't hold text worth scanning.
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2"}
# Documents and databases: can't be scanned reliably (compressed / binary) and
# are exactly where a résumé or the app DB would leak. Fail unless a fixture.
DOC_SUFFIXES = {".pdf", ".doc", ".docx", ".odt", ".db", ".sqlite", ".sqlite3",
                ".xls", ".xlsx"}
DOC_ALLOWED_PREFIXES = ("tests/fixtures/",)

# Public identifiers that legitimately appear in tracked files (repo URL in the
# HTTP User-Agent, LICENSE). A profile value equal to one isn't a leak.
PUBLIC_HANDLES = {"aditya-barnwal"}
# person.* values that may appear in specific tracked files.
PERSONAL_ALLOWED_IN = {"name": {"LICENSE"}}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
PHONE_RE = re.compile(r"\+\d{1,3}[\s-]?\(?\d{2,5}\)?[\s-]?\d{3,5}[\s-]?\d{3,5}")
# Bare Indian mobile: 10 digits starting 6-9, optionally split 5+5. Not part
# of a longer token, URL path, decimal, or hyphenated id ("req-7012345678").
MOBILE_IN_RE = re.compile(r"(?<![\w./#@+-])[6-9]\d{4}[ -]?\d{5}(?![\w/@-]|\.\d)")
# `"job_id": 7012345678`, `gh_jid=…`, `jobId: …` — the key right before a value.
_KEY_BEFORE_VALUE_RE = re.compile(r"""([A-Za-z][A-Za-z0-9_-]*)["']?\s*[:=]\s*\[?\s*["']?$""")
ID_KEY_WORDS = {"id", "ids", "jid", "uuid", "ref", "req", "key", "hash", "seq",
                "version", "ts", "timestamp", "epoch", "count", "total", "size", "bytes"}
AADHAAR_RE = re.compile(r"\b[2-9]\d{3}\s\d{4}\s\d{4}\b")
PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("secret-groq-key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("secret-openai-anthropic-key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}")),
    ("secret-google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("secret-aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("secret-slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("secret-private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
]
# KEY=value / export TOKEN="value" (env-file style), with a non-placeholder value.
SECRET_ASSIGN_RE = re.compile(
    r"""^\s*(?:export\s+)?[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD)"""
    r"""\s*=\s*['"]?([^\s'"#]{12,})""")
_PLACEHOLDER_RE = re.compile(r"your|xxx|example|changeme|placeholder|dummy|<|\$\{|\.\.\.", re.I)


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
    if not isinstance(person, dict):
        return []
    out: list[tuple[str, str]] = []
    for key in ("email", "phone", "name"):
        v = person.get(key)
        if isinstance(v, dict):
            v = v.get("text") or v.get("value")
        if isinstance(v, str) and len(v.strip()) >= (5 if key == "name" else 6):
            if _norm_link(v) not in PUBLIC_HANDLES:
                out.append((key, v.strip()))
    for link in _link_values(person.get("links")):
        norm = _norm_link(link)
        if len(norm) >= 6 and not _is_public_handle_link(norm):
            out.append(("link", norm))
    phone = next((v for k, v in out if k == "phone"), None)
    if phone:
        digits = re.sub(r"\D", "", phone)[-10:]
        if len(digits) == 10:
            out.append(("phone-digits", digits))
    return out


def _link_values(links) -> list[str]:
    """person.links as a dict, a list of strings, or a list of {url: …}."""
    items = links.values() if isinstance(links, dict) else links if isinstance(links, list) else []
    out = []
    for it in items:
        if isinstance(it, dict):
            it = it.get("url") or it.get("href") or it.get("value")
        if isinstance(it, str) and it.strip():
            out.append(it.strip())
    return out


def _norm_link(v: str) -> str:
    v = v.strip().lower()
    v = re.sub(r"^[a-z]+://", "", v)
    v = re.sub(r"^www\.", "", v)
    return v.rstrip("/")


def _is_public_handle_link(norm: str) -> bool:
    """github.com/<public handle> (the repo owner) appears in the repo URL."""
    host, _, path = norm.partition("/")
    return norm in PUBLIC_HANDLES or (host == "github.com" and path in PUBLIC_HANDLES)


def _is_id_value(prefix: str) -> bool:
    """True when the text before a number makes it an id-like key's value."""
    m = _KEY_BEFORE_VALUE_RE.search(prefix[-80:])
    if not m:
        return False
    words = re.split(r"[_-]|(?<=[a-z0-9])(?=[A-Z])", m.group(1))
    return words[-1].lower() in ID_KEY_WORDS


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
            if domain.rsplit(".", 1)[-1] in RESERVED_EMAIL_TLDS:
                continue
            if not any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS):
                findings.append((n, "email", _mask(m.group(0))))
        phone_spans = []
        for m in PHONE_RE.finditer(line):
            if len(re.sub(r"\D", "", m.group(0))) >= 10:
                findings.append((n, "phone", _mask(m.group(0))))
                phone_spans.append(m.span())
        for m in MOBILE_IN_RE.finditer(line):
            if any(a < m.end() and m.start() < b for a, b in phone_spans):
                continue   # already reported in +<country> form
            if not _is_id_value(line[:m.start()]):
                findings.append((n, "phone", _mask(m.group(0))))
        for m in AADHAAR_RE.finditer(line):
            findings.append((n, "aadhaar-like", _mask(m.group(0))))
        for m in PAN_RE.finditer(line):
            findings.append((n, "pan-like", _mask(m.group(0))))
        for kind, rx in SECRET_PATTERNS:
            for m in rx.finditer(line):
                if kind == "secret-openai-anthropic-key" and not re.search(r"\d", m.group(0)):
                    continue   # long hyphenated words, not a random key
                findings.append((n, kind, _mask(m.group(0))))
        m = SECRET_ASSIGN_RE.search(line)
        if m and not _PLACEHOLDER_RE.search(m.group(1)) \
                and re.search(r"\d", m.group(1)) and re.search(r"[A-Za-z]", m.group(1)) \
                and not any(k.startswith("secret-") for ln, k, _ in findings if ln == n):
            findings.append((n, "secret-assignment", _mask(m.group(1))))
        compact = re.sub(r"\D", "", line)
        for kind, value in personal or []:
            if kind == "phone-digits":
                hit = value in compact
            elif kind == "name":
                parts = [re.escape(p) for p in value.split()]
                hit = re.search(r"(?<![A-Za-z])" + r"\s+".join(parts) + r"(?![A-Za-z])",
                                line, re.I) is not None
            else:
                hit = value.lower() in line.lower()
            if hit:
                findings.append((n, f"your-{kind}", _mask(value)))
    return findings


def _read_text(path: Path) -> str:
    """Never skip a file for its encoding: UTF-16 by BOM, otherwise UTF-8 with
    replacement characters (a Latin-1 byte must not hide the rest)."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=root,
                         capture_output=True, check=True).stdout.decode()
    return [root / p for p in out.split("\0") if p]


MD_SALARY_RE = re.compile(r"(?i)(\b\d+(\.\d+)?\s*(lpa|lakhs?|lacs?|cr|crores?)\b|₹\s*\d|\bctc\b[^.\n]{0,40}\d)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    personal = _personal_values(root)

    total = 0
    for path in tracked_files(root):
        rel = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        if suffix in DOC_SUFFIXES:
            if not rel.startswith(DOC_ALLOWED_PREFIXES):
                print(f"{rel}: tracked-document ({suffix}) — documents and databases "
                      f"can't be scanned; keep them out of git (only tests/fixtures/ may hold them)")
                total += 1
            continue
        if suffix in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = _read_text(path)
        except OSError as exc:
            print(f"{rel}: unreadable ({exc.__class__.__name__}) — can't confirm it is clean")
            total += 1
            continue
        allowed_here = {k for k, files in PERSONAL_ALLOWED_IN.items() if rel in files}
        mine = [(k, v) for k, v in personal if k not in allowed_here]
        for n, kind, masked in scan_text(text, mine):
            print(f"{rel}:{n}: {kind} {masked}")
            total += 1
        if suffix == ".md":     # public docs never state a salary figure (CTC, floors, expectations)
            for n, line in enumerate(text.splitlines(), 1):
                if MD_SALARY_RE.search(line):
                    print(f"{rel}:{n}: salary-figure-in-doc (keep numbers in config/ or profile/)")
                    total += 1
    if total:
        print(f"\n{total} possible personal-data finding(s) in tracked files.", file=sys.stderr)
        return 1
    print(f"PII check: clean ({'with' if personal else 'without'} local profile values).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
