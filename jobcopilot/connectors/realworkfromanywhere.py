"""Real Work From Anywhere RSS feed (board connector).

  GET https://www.realworkfromanywhere.com/rss.xml

The board only lists roles open to applicants anywhere in the world, so every
job carries region_restrictions ["Worldwide"] — the board's own promise, which
Phase 2 can still overrule from the text. robots.txt (re-checked 2026-11-12)
disallows `/go/` (their outbound redirector): we never store or follow a /go/
link, only the item's /jobs/ page. RSS source -> `store: minimal` by default
(D11): description kept only when the board config says `store: full`.

This module also holds the small safe-feed helpers the other RSS / Atom board
connectors (cryptojobslist, hasjob, nodesk) share.
"""
from __future__ import annotations

import html
import html.entities
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import urlsplit

from .base import Connector
from ..http import PoliteClient
from ..models import Job, REMOTE
from ..normalize import strip_html, infer_country_bucket

log = logging.getLogger("jobcopilot.connectors.realworkfromanywhere")

# ----------------------------------------------------------- safe feed helpers

RSS_HEADERS = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.1"}
MAX_FEED_BYTES = 5 * 1024 * 1024   # a job feed is < 2 MB; anything bigger is not a feed

_DTD_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.I)
_CDATA_RE = re.compile(rb"<!\[CDATA\[.*?\]\]>", re.S)
_AMP_RE = re.compile(rb"&(?:(#[0-9]{1,7}|#x[0-9A-Fa-f]{1,6})|([A-Za-z][A-Za-z0-9]{0,31}))?;?")
_XML_ENTITIES = {b"amp", b"lt", b"gt", b"quot", b"apos"}


class UnsafeFeed(ValueError):
    """The document declares a DTD / entities (entity-expansion risk), uses an
    encoding the DTD guards cannot read, or is too big."""


_BOMS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")
_DECL_ENC_RE = re.compile(rb"<\?xml\s[^>]*?encoding\s*=\s*([\"'])(.*?)\1", re.I | re.S)
_SAFE_ENCODINGS = frozenset({b"utf-8", b"utf8", b"us-ascii", b"ascii", b"iso-8859-1", b"latin1",
                             b"latin-1", b"iso_8859-1"})


def check_encoding(data: bytes) -> None:
    """Refuse a body whose bytes the byte-level DOCTYPE / ENTITY guards cannot read.

    WHY: those guards look for ASCII '<!DOCTYPE', but expat also decodes UTF-16/32
    (and other declared encodings), where the same markup is spelled differently
    and would slip past. So: no UTF-16/32 BOM, no NUL in the first 4 bytes
    (BOM-less UTF-16/32), and a declared encoding must be utf-8 / us-ascii /
    iso-8859-1 (a declaration may also not hide behind an unreadable prolog)."""
    head = data[:4]
    if head.startswith(_BOMS) or b"\x00" in head:
        raise UnsafeFeed("feed is UTF-16/32 encoded; refusing to parse")
    start = 3 if data.startswith(b"\xef\xbb\xbf") else 0
    if data.startswith(b"<?xml", start):
        m = _DECL_ENC_RE.match(data[start:start + 512])
        if m and m.group(2).strip().lower() not in _SAFE_ENCODINGS:
            raise UnsafeFeed(f"feed declares encoding {m.group(2).decode('latin-1')!r}; refusing to parse")


def _fix_amp(m: re.Match[bytes]) -> bytes:
    """Make one '&…' well-formed XML: keep real references, turn HTML named
    entities (&rsquo; &nbsp;) into numeric ones, escape bare '&' (seen in
    realworkfromanywhere enclosure URLs)."""
    whole, num, name = m.group(0), m.group(1), m.group(2)
    if not whole.endswith(b";") or (num is None and name is None):
        return b"&amp;" + whole[1:]
    if num is not None or name in _XML_ENTITIES:
        return whole
    cp = html.entities.name2codepoint.get(name.decode("ascii"))
    return b"&#%d;" % cp if cp is not None else b"&amp;" + whole[1:]


def safe_feed_root(body: Any) -> ET.Element:
    """Parse an RSS / Atom body with xml.etree, safely.

    WHY: expat expands internal entities, so a '<!DOCTYPE … <!ENTITY …' feed
    could blow up memory ("billion laughs"). Any DTD is refused outright (no
    real job feed needs one), and so is anything over MAX_FEED_BYTES. Bare '&'
    and HTML named entities outside CDATA are repaired, since several boards
    ship them and expat would otherwise reject the whole feed.
    """
    data = body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
    if len(data) > MAX_FEED_BYTES:
        raise UnsafeFeed(f"feed is {len(data)} bytes (> {MAX_FEED_BYTES})")
    check_encoding(data)
    if _DTD_RE.search(data):
        raise UnsafeFeed("feed declares a DOCTYPE / ENTITY; refusing to parse")
    out: list[bytes] = []
    pos = 0
    for m in _CDATA_RE.finditer(data):
        out.append(_AMP_RE.sub(_fix_amp, data[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_AMP_RE.sub(_fix_amp, data[pos:]))
    return ET.fromstring(b"".join(out))


def fetch_feed(client: PoliteClient, url: str, timeout: Optional[float]) -> bytes:
    """GET a feed through the polite client with the feed size cap. Bytes, so
    expat honours the XML declaration's encoding instead of requests guessing."""
    return client.get(url, headers=RSS_HEADERS, timeout=timeout, max_bytes=MAX_FEED_BYTES).content


def rfc822_iso(value: Optional[str]) -> Optional[str]:
    if not value or not value.strip():
        return None
    try:
        return parsedate_to_datetime(value.strip()).isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def text_of(el: Optional[ET.Element]) -> str:
    """Element text with entities decoded (some feeds double-escape '&amp;amp;')."""
    if el is None or el.text is None:
        return ""
    return html.unescape(el.text).strip()


def split_role_at_company(title: str, company: str = "") -> tuple[str, str]:
    """'Cloud Engineer at Chess.com' -> ('Cloud Engineer', 'Chess.com').
    A known company is stripped from the end; otherwise split on the LAST
    ' at '. No ' at ' -> (title, ''): we don't invent an employer."""
    title = title.strip()
    if company and title.lower().endswith(" at " + company.lower()):
        return title[: -len(company) - 4].strip(), company
    role, sep, comp = title.rpartition(" at ")
    if not sep or not role.strip() or not comp.strip():
        return title, company
    return role.strip(), company or comp.strip()


def keep_description(board: dict[str, Any], default: str = "minimal") -> bool:
    """D11: RSS sources store minimal fields unless the config opts in."""
    return str(board.get("store") or default).strip().lower() == "full"


# ------------------------------------------------------------------ connector

def _is_go_link(url: str) -> bool:
    return urlsplit(url).path.startswith("/go/")


class RealWorkFromAnywhereConnector(Connector):
    source = "realworkfromanywhere"
    URL = "https://www.realworkfromanywhere.com/rss.xml"

    def fetch_raw(self, company: dict[str, Any]) -> Any:
        return fetch_feed(self.client, self.URL, self.timeout)

    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        board = company or {}
        attr = board.get("attribution") or {}
        full = keep_description(board)
        jobs: list[Job] = []
        seen: set[str] = set()
        if not raw:
            return jobs
        for it in safe_feed_root(raw).iterfind("./channel/item"):
            # robots: /go/ is disallowed -> only ever keep a /jobs/ page link.
            link = next((u for u in (text_of(it.find("link")), text_of(it.find("guid")))
                         if u.startswith("http") and not _is_go_link(u)), "")
            if not link or link in seen:
                continue
            seen.add(link)
            author = text_of(it.find("author"))
            title, company_name = split_role_at_company(text_of(it.find("title")), author)
            posted = rfc822_iso(text_of(it.find("pubDate")))
            desc = strip_html(it.findtext("description")) if full else ""
            jobs.append(Job(
                source=self.source,
                company=company_name,
                company_sector=None,
                external_id=urlsplit(link).path.rstrip("/").rsplit("/", 1)[-1] or link,
                title=title,
                location_text="Worldwide (remote)",
                workplace_type=REMOTE,
                country_bucket=infer_country_bucket("Worldwide"),
                url=link,
                description_text=desc,
                posted_at=posted,
                apply_url=link,               # never the /go/ redirector
                region_restrictions=["Worldwide"],
                attribution_name=attr.get("name") or "Real Work From Anywhere",
                attribution_url=link or attr.get("url") or "https://www.realworkfromanywhere.com",
                raw={"title": text_of(it.find("title")), "link": link, "author": author,
                     "pubDate": posted},
            ))
        return jobs
