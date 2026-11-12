"""speedyapply new-grad lists on GitHub (board connector).

  GET https://raw.githubusercontent.com/<repo>/<branch>/<file>.md

Each file is a README of markdown tables

  | Company | Position | Location | [Salary |] Posting | Age |

where Company and Posting are HTML links and Age is "5d". The repo is
renamed every season (2027-SWE-College-Jobs today), so `repo` lives in
config. No licence: we store the employer facts only, credit the repo on
every job ("via speedyapply (GitHub)"), and Apply goes to the employer link.

`files` in the board config says which rows to keep per file:
  all            every row (NEW_GRAD_INTL.md)
  remote         only rows whose location says Remote (NEW_GRAD_USA.md)
  india_or_remote  India locations or Remote
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .base import Connector
from ..http import HttpError
from ..models import Job
from ..normalize import infer_country_bucket, infer_workplace_type

log = logging.getLogger("jobcopilot.connectors.speedyapply")

RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
DEFAULT_REPO = "speedyapply/2027-SWE-College-Jobs"
DEFAULT_FILES = ({"path": "NEW_GRAD_INTL.md", "keep": "all"},
                 {"path": "NEW_GRAD_USA.md", "keep": "remote"})
KEEP_MODES = ("all", "remote", "india_or_remote")

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_PATH_RE = re.compile(r"^(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:md|json)$")
_HREF_RE = re.compile(r"<a\s[^>]*?href=\"([^\"]+)\"", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_AGE_RE = re.compile(r"^\s*(\d+)\s*(h|d|w|mo|m|y)\b", re.I)
_REMOTE_RE = re.compile(r"\bremote\b", re.I)
INDIA_RE = re.compile(
    r"\b(?:india|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|gurgaon|gurugram|noida|"
    r"new delhi|delhi|kolkata|ahmedabad|kochi|coimbatore|thiruvananthapuram|trivandrum|"
    r"indore|jaipur)\b", re.I)

# Tracking / referral parameters added by list maintainers, never needed to apply.
_TRACKING_PARAMS = {"ref", "referrer", "referral", "src", "source", "gh_src", "utm_source",
                    "utm_medium", "utm_campaign", "utm_content", "utm_term", "lever-source",
                    "lever-origin"}
_REDIRECT_PARAMS = ("url", "u", "redirect", "redirect_url", "target", "dest", "destination")


def validate_github_path(repo: str, branch: str, path: str) -> str:
    """Raw-file URL for config values, each part validated and quoted."""
    if not _REPO_RE.fullmatch(repo or "") or ".." in repo:
        raise ValueError(f"invalid GitHub repo {repo!r}")
    if not _REF_RE.fullmatch(branch or "") or ".." in branch:
        raise ValueError(f"invalid GitHub branch {branch!r}")
    if not _PATH_RE.fullmatch(path or "") or ".." in path:
        raise ValueError(f"invalid repo file path {path!r}")
    owner, name = repo.split("/")
    q = lambda s: urllib.parse.quote(s, safe="")  # noqa: E731
    return RAW.format(repo=f"{q(owner)}/{q(name)}", branch=q(branch),
                      path="/".join(q(p) for p in path.split("/")))


def clean_apply_url(url: Optional[str]) -> Optional[str]:
    """Unwrap redirectors ("...?url=https://employer/...") and drop referral /
    utm parameters, so Apply goes straight to the employer's page."""
    if not url:
        return None
    url = html.unescape(url.strip())
    for _ in range(3):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return None
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        inner = next((v for k, v in query if k.lower() in _REDIRECT_PARAMS
                      and v.lower().startswith(("http://", "https://"))), None)
        if inner is None:
            kept = [(k, v) for k, v in query if k.lower() not in _TRACKING_PARAMS
                    and not k.lower().startswith("utm_")]
            return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(kept)))
        url = inner
    return None   # redirect chain deeper than 3: not an employer link we trust


def keep_location(location: str, mode: str) -> bool:
    if mode == "remote":
        return bool(_REMOTE_RE.search(location))
    if mode == "india_or_remote":
        return bool(_REMOTE_RE.search(location) or INDIA_RE.search(location))
    return True


def age_days(text: Optional[str]) -> Optional[int]:
    """"0d" -> 0, "3w" -> 21, "2mo" -> 60, "5h" -> 0. None when unreadable."""
    m = _AGE_RE.match(text or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return {"h": 0, "d": n, "w": 7 * n, "mo": 30 * n, "m": 30 * n, "y": 365 * n}[unit]


def _cell_text(cell: str) -> str:
    text = _MD_LINK_RE.sub(r"\1", cell)
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def _cell_link(cell: str) -> Optional[str]:
    m = _HREF_RE.search(cell)
    if m:
        return m.group(1)
    m = _MD_LINK_RE.search(cell)
    return m.group(2) if m else None


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_tables(markdown: str) -> list[dict[str, Any]]:
    """Every data row of every `| Company | Position | ... |` table, keyed by
    lower-case header name, with `<name>_link` for cells that hold a link."""
    rows: list[dict[str, Any]] = []
    header: Optional[list[str]] = None
    section = None
    prev_company: Optional[str] = None
    for line in (markdown or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            section = s.lstrip("#").strip()
        if not s.startswith("|"):
            header = None
            continue
        cells = _split_row(s)
        if header is None:
            names = [c.lower() for c in cells]
            if "company" in names and ("position" in names or "role" in names):
                header = names
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue   # the |---|---| separator
        if len(cells) != len(header):
            continue
        row: dict[str, Any] = {"section": section}
        for name, cell in zip(header, cells):
            row[name] = _cell_text(cell)
            link = _cell_link(cell)
            if link:
                row[f"{name}_link"] = link
        if row.get("company") in ("↳", "") and prev_company:
            row["company"] = prev_company   # "same company as above"
        prev_company = row.get("company") or prev_company
        rows.append(row)
    return rows


class SpeedyApplyConnector(Connector):
    source = "speedyapply"

    @staticmethod
    def _files(board: dict[str, Any]) -> list[dict[str, str]]:
        files = board.get("files") or list(DEFAULT_FILES)
        out = []
        for f in files:
            f = {"path": f} if isinstance(f, str) else dict(f)
            keep = f.get("keep") or "all"
            if keep not in KEEP_MODES:
                raise ValueError(f"speedyapply: unknown keep mode {keep!r}")
            out.append({"path": str(f["path"]), "keep": keep})
        return out

    def fetch_raw(self, company: dict[str, Any]) -> dict[str, str]:
        """{file path: markdown}. One failing file is logged and skipped; if
        every file fails the last error is raised so the source shows failing."""
        board = company or {}
        repo = board.get("repo") or DEFAULT_REPO
        branch = board.get("branch") or "main"
        out: dict[str, str] = {}
        last_err: Optional[Exception] = None
        for f in self._files(board):
            url = validate_github_path(repo, branch, f["path"])
            try:
                resp = self.client.get(url, headers={"Accept": "text/plain"}, timeout=self.timeout)
            except HttpError as e:
                last_err = e
                log.warning("speedyapply: %s failed: %s", f["path"], e)
                continue
            out[f["path"]] = resp.text
        if not out and last_err is not None:
            raise last_err
        return out

    def parse(self, raw: Any, company: dict[str, Any], *,
              now: Optional[datetime] = None) -> list[Job]:
        """`raw` is fetch_raw's {path: markdown} (also the tests/fixtures shape)."""
        board = company or {}
        now = now or datetime.now(timezone.utc)
        attr = board.get("attribution") or {}
        repo = board.get("repo") or DEFAULT_REPO
        repo_url = attr.get("url") or f"https://github.com/{repo}"
        keep = {f["path"]: f["keep"] for f in self._files(board)}
        jobs: list[Job] = []
        seen: set[str] = set()
        for path, markdown in (raw or {}).items():
            mode = keep.get(path, "all")
            dropped = 0
            for row in parse_tables(markdown):
                location = row.get("location") or ""
                if not keep_location(location, mode):
                    dropped += 1
                    continue
                apply_url = clean_apply_url(row.get("posting_link"))
                title, name = row.get("position") or row.get("role") or "", row.get("company") or ""
                if not (apply_url and title and name):
                    continue
                external_id = hashlib.sha1(apply_url.encode("utf-8")).hexdigest()[:16]
                if external_id in seen:
                    continue
                seen.add(external_id)
                age = age_days(row.get("age"))
                posted = (now - timedelta(days=age)).date().isoformat() if age is not None else None
                jobs.append(Job(
                    source=self.source,
                    company=name,
                    company_sector=None,
                    external_id=external_id,
                    title=title,
                    location_text=location,
                    workplace_type=infer_workplace_type(location),
                    country_bucket=infer_country_bucket(location),
                    url=apply_url,
                    description_text="",
                    department=row.get("section"),
                    posted_at=posted,
                    posted_age_days=age,
                    apply_url=apply_url,
                    attribution_name=attr.get("name") or "speedyapply (GitHub)",
                    attribution_url=f"{repo_url}/blob/{board.get('branch') or 'main'}/{path}",
                    raw={**row, "file": path},
                ))
            if dropped:
                log.info("speedyapply: %s skipped %d rows outside keep=%s", path, dropped, mode)
        return jobs
