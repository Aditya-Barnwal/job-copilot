"""Hacker News "Ask HN: Who is hiring?" (board connector).

  1. GET https://hacker-news.firebaseio.com/v0/user/whoishiring.json
     -> `submitted` item ids, newest first (hiring / freelancer / wants-to-be-hired
     threads interleave), then GET v0/item/<id>.json until the title is
     "Ask HN: Who is hiring?" (at most `max_item_fetches`).
  2. GET https://hn.algolia.com/api/v1/items/<id>  -> the whole thread in ONE call.

Both are official public APIs (Firebase robots.txt allows *.json; Algolia has
no robots.txt). Only top-level comments are job posts; replies, deleted and
dead comments are skipped. Parsing is jobcopilot/hn_parse.py (regex, no LLM).
Email addresses are dropped everywhere; Apply is the post's http(s) link, or
the HN permalink when the post has none.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

from .base import Connector, DEFAULT_HEADERS
from .speedyapply import clean_apply_url
from ..hn_parse import MAX_ROLES, parse_post
from ..http import HttpError
from ..models import Job
from ..normalize import infer_country_bucket

log = logging.getLogger("jobcopilot.connectors.hn_hiring")

USER_URL = "https://hacker-news.firebaseio.com/v0/user/{user}.json"
ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
THREAD_URL = "https://hn.algolia.com/api/v1/items/{id}"
PERMALINK = "https://news.ycombinator.com/item?id={id}"
THREAD_MAX_BYTES = 10_000_000
_TITLE_RE = re.compile(r"^\s*Ask HN:\s*Who is hiring\?", re.I)
_MONTH_RE = re.compile(r"\(([A-Za-z]+ \d{4})\)")
_USER_RE = re.compile(r"^[A-Za-z0-9_-]{2,15}$")


class ThreadNotFound(HttpError):
    """No "Who is hiring?" thread among the account's latest submissions."""


def _item_id(value: Any) -> int:
    """HN ids are positive ints; anything else never reaches a URL."""
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
        raise ValueError(f"invalid HN item id {value!r}")
    n = int(value)
    if n <= 0:
        raise ValueError(f"invalid HN item id {value!r}")
    return n


def _q(value: Any) -> str:
    return urllib.parse.quote(str(value), safe="")


class HNHiringConnector(Connector):
    source = "hn_hiring"

    def find_thread(self, board: dict[str, Any]) -> tuple[int, str]:
        """(id, title) of the newest "Who is hiring?" thread."""
        user = board.get("user") or "whoishiring"
        if not _USER_RE.fullmatch(user):
            raise ValueError(f"invalid HN user {user!r}")
        info = self.client.get_json(USER_URL.format(user=_q(user)),
                                    headers=DEFAULT_HEADERS, timeout=self.timeout) or {}
        max_fetches = int(board.get("max_item_fetches") or 6)
        for raw_id in (info.get("submitted") or [])[:max_fetches]:
            item_id = _item_id(raw_id)
            item = self.client.get_json(ITEM_URL.format(id=_q(item_id)),
                                        headers=DEFAULT_HEADERS, timeout=self.timeout) or {}
            if item.get("deleted") or item.get("dead"):
                continue
            title = item.get("title") or ""
            if _TITLE_RE.match(title):
                return item_id, title
        raise ThreadNotFound(f"no 'Who is hiring?' thread in the last {max_fetches} posts by {user}")

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        board = company or {}
        thread_id = board.get("thread_id")
        if thread_id:
            thread_id = _item_id(thread_id)          # pinned in config (debugging)
        else:
            thread_id, title = self.find_thread(board)
            log.info("hn_hiring: thread %s %r", thread_id, title)
        return self.client.get_json(
            THREAD_URL.format(id=_q(thread_id)), headers=DEFAULT_HEADERS, timeout=self.timeout,
            max_bytes=int(board.get("max_bytes") or THREAD_MAX_BYTES),
            deadline_sec=float(board.get("deadline_sec") or 90))

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        """`raw` is the Algolia items payload for the thread."""
        board = company or {}
        thread = raw if isinstance(raw, dict) else {}
        thread_id = thread.get("id")
        title = thread.get("title") or ""
        month = _MONTH_RE.search(title)
        label = f"Hacker News — Who is hiring ({month.group(1)})" if month else "Hacker News — Who is hiring"
        attr_name = (board.get("attribution") or {}).get("name") or label
        if "(" not in attr_name and month:
            attr_name = f"{attr_name} ({month.group(1)})"
        max_roles = max(1, min(int(board.get("max_roles_per_post") or MAX_ROLES), MAX_ROLES))

        jobs: list[Job] = []
        skipped = {"not_top_level": 0, "deleted": 0, "unparsed": 0}
        for c in thread.get("children") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") != "comment" or (thread_id and c.get("parent_id") != thread_id):
                skipped["not_top_level"] += 1
                continue
            if not c.get("text") or not c.get("author") or c.get("deleted") or c.get("dead"):
                skipped["deleted"] += 1
                continue
            try:
                cid = _item_id(c.get("id"))
            except ValueError:
                skipped["unparsed"] += 1
                continue
            post, text = parse_post(c.get("text"), max_roles=max_roles)
            if not post.ok:
                skipped["unparsed"] += 1
                continue
            permalink = PERMALINK.format(id=cid)
            apply_url = clean_apply_url(post.apply_url) or permalink
            location = post.location_text
            for n, role in enumerate(post.roles or ["Open roles (see post)"], start=1):
                jobs.append(Job(
                    source=self.source,
                    company=post.company,
                    company_sector=None,
                    external_id=f"{cid}:{n}",
                    title=role,
                    location_text=location,
                    workplace_type=post.workplace_type,
                    country_bucket=infer_country_bucket(location),
                    url=permalink,
                    description_text=text,
                    compensation=post.salary,
                    posted_at=c.get("created_at"),
                    apply_url=apply_url,
                    region_restrictions=list(post.regions),
                    timezone_restrictions=list(post.tz_offsets),
                    tz_requirement_text=post.tz_text,
                    attribution_name=attr_name,
                    attribution_url=permalink,
                    # No author, no raw HTML: the stored record carries no emails.
                    raw={"id": cid, "story_id": thread_id, "created_at": c.get("created_at"),
                         "header": post.header},
                ))
        log.info("hn_hiring: %d jobs from thread %s, skipped %s", len(jobs), thread_id, skipped)
        return jobs
