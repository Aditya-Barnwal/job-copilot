"""Pure text helpers for board region / time-zone fields.

Remote boards describe who may apply in free text ("Europe, LATAM, APAC",
"USA Only", "Time zone: CET (+/- 3 hours)"). These helpers only *split and
extract* that text so it can be stored on Job.region_restrictions /
tz_requirement_text / timezone_restrictions verbatim. They never decide
eligibility: Phase 2 (filters) owns that, so nothing here knows about India.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Optional

# Country names that contain " and " and must not be split on it.
_AND_NAMES = (
    "bosnia and herzegovina", "trinidad and tobago", "antigua and barbuda",
    "saint kitts and nevis", "st kitts and nevis", "saint vincent and the grenadines",
    "sao tome and principe", "são tomé and príncipe", "turks and caicos islands",
    "heard island and mcdonald islands", "saint pierre and miquelon",
    "wallis and futuna", "svalbard and jan mayen",
)
_SEPARATORS = (",", ";", "/", "|", "\n")
_AND_RE = re.compile(r"\s+(?:and|&)\s+", re.I)

# A token is time-zone text when it names a zone or talks about zones/overlap.
# Upper-case-only abbreviations, so "et" / "pt" inside words don't match.
_TZ_ABBR_RE = re.compile(r"\b(?:EST|EDT|PST|PDT|CST|CDT|MST|MDT|CET|CEST|EET|EEST|WET|BST|IST|AEST|AEDT|SGT|JST)\b")
# Two-letter zones double as US states / country codes ("Hartford, CT",
# "Lisbon, PT", "Helena, MT"), so they only count as a zone next to another
# zone cue, or when they are the whole field ("PT", "ET/PT").
_TZ_WEAK_RE = re.compile(r"\b(?:ET|PT|CT|MT)\b")
_TZ_CUE_RE = re.compile(r"\b(?:time|zones?|hours?|hrs?|overlap|utc|gmt)\b|[+\-−±]\s*\d|\+\s*/\s*-", re.I)
_ANY_ABBR = r"(?:ET|PT|CT|MT|EST|EDT|PST|PDT|CST|CDT|MST|MDT|CET|CEST|EET|EEST|WET|BST|IST|AEST|AEDT|SGT|JST)"
_ABBR_ONLY_RE = re.compile(rf"\s*{_ANY_ABBR}(?:\s*(?:[,/;|&]|\bor\b|\band\b)\s*{_ANY_ABBR})*\s*")
_TRAILING_PAREN_RE = re.compile(r"^(?P<head>.*\S)\s*\((?P<inner>[^()]*)\)\s*$", re.S)
_TZ_WORD_RE = re.compile(r"\b(?:utc|gmt|time\s*-?\s*zones?|timezones?)\b", re.I)
_UTC_OFFSET_RE = re.compile(
    r"\b(?:utc|gmt)\s*([+\-−])\s*(\d{1,2})(?:[:.](\d{2}))?", re.I
)


def _split_outside_parens(text: str) -> list[str]:
    """Split on , ; / | newline, but never inside (...), so
    "CET (+/- 3 hours)" stays one piece."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and ch in _SEPARATORS:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _split_and(token: str) -> list[str]:
    if "(" in token:
        return [token]
    # Shield " and " inside known country names, split, then restore.
    shielded = token
    for name in _AND_NAMES:
        shielded = re.sub(re.escape(name), lambda m: m.group(0).replace(" ", "\x00"),
                          shielded, flags=re.I)
    return [p.replace("\x00", " ") for p in _AND_RE.split(shielded)]


def split_regions(text: Optional[str]) -> list[str]:
    """'Europe, LATAM / APAC and Canada' -> ['Europe', 'LATAM', 'APAC', 'Canada'].

    Splits on , ; / | and the word "and" (outside parentheses, keeping
    country names like "Bosnia and Herzegovina"), trims each piece, drops
    empties and case-insensitive duplicates. Tokens keep their wording
    ("USA Only" stays "USA Only") so the filter sees what the board said.
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for piece in _split_outside_parens(str(text)):
        for tok in _split_and(piece.strip()):
            tok = tok.strip().strip(".-–— ").strip()
            # "the U.S." lost its final dot above; put abbreviation dots back.
            if re.fullmatch(r"(?:the\s+)?(?:[A-Za-z]\.){1,3}[A-Za-z]", tok):
                tok += "."
            if tok and tok.lower() not in seen:
                seen.add(tok.lower())
                out.append(tok)
    return out


def regions_from_list(values: Optional[Iterable[Any]]) -> list[str]:
    """Normalise a list field (Himalayas locationRestrictions: strings or
    {"name": ...} objects) into clean, de-duplicated strings."""
    out: list[str] = []
    for v in values or []:
        if isinstance(v, dict):
            v = v.get("name") or v.get("value") or ""
        for tok in split_regions(str(v)) if v else []:
            if tok.lower() not in {o.lower() for o in out}:
                out.append(tok)
    return out


def is_timezone_text(token: Optional[str], *, field: Optional[str] = None) -> bool:
    """True when a location token describes a time zone rather than a place
    ('Time zone: CET (+/- 3 hours)', 'UTC-5 to UTC+1', 'EST overlap').

    `field` is the whole location string the token came from (defaults to the
    token): a bare ET/PT/CT/MT is a zone only with another cue in the token
    or when the whole field is nothing but zone abbreviations.
    """
    if not token:
        return False
    if _TZ_WORD_RE.search(token) or _TZ_ABBR_RE.search(token) or re.search(r"\boverlap\b", token, re.I):
        return True
    if _TZ_WEAK_RE.search(token):
        whole = token if field is None else field
        return bool(_TZ_CUE_RE.search(token) or _ABBR_ONLY_RE.fullmatch(whole))
    return False


def partition_location(text: Optional[str]) -> tuple[list[str], Optional[str]]:
    """Split a free-text location field into (regions, tz_text).

    Region pieces go to the list; pieces that are really time-zone
    requirements are joined verbatim into tz_text (None when there are none).
    """
    if not text:
        return [], None
    text = str(text)
    regions: list[str] = []
    tz: list[str] = []
    # Whole field first: "Time zone: CET (+/- 3 hours)" must not be split at ':'.
    if is_timezone_text(text) and not any(s in text for s in (";", "|", "\n")):
        pieces = [p.strip() for p in _split_outside_parens(text) if p.strip()]
        # Judge each piece by its text outside (...): "CET (+/- 3 hours)" is a
        # zone, "India (IST)" is a place followed by a zone.
        outside = [re.sub(r"\([^()]*\)", " ", p).strip() or p for p in pieces]
        if all(is_timezone_text(o, field=text) for o in outside):
            return [], text.strip()
    # Then a trailing zone in parentheses: "India (IST)", "Americas (UTC-8 to UTC-3)".
    m = _TRAILING_PAREN_RE.match(text)
    if m and m.group("inner").strip() and is_timezone_text(m.group("inner"), field=text) \
            and not is_timezone_text(m.group("head"), field=text):
        head_regions, head_tz = partition_location(m.group("head"))
        tz_parts = [t for t in (head_tz, m.group("inner").strip()) if t]
        return head_regions, "; ".join(tz_parts)
    for tok in split_regions(text):
        (tz if is_timezone_text(tok, field=text) else regions).append(tok)
    return regions, ("; ".join(tz) if tz else None)


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if -12.0 <= f <= 14.0 else None


def parse_utc_offsets(value: Any) -> list[float]:
    """UTC offsets in hours, sorted and de-duplicated.

    Accepts what boards send: a list of numbers / numeric strings (Himalayas
    timezoneRestrictions, e.g. [-5, 3.5, 5.75]), a single number, or free text
    with explicit offsets ("UTC+5:30", "GMT-3", "UTC −8 to UTC+1"). Named
    zones without a number (CET, EST) are left alone: guessing DST would be
    fabricating, so they stay in tz_requirement_text for Phase 2.
    """
    found: list[float] = []
    if value is None:
        return []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        n = _num(value)
        found = [n] if n is not None else []
    elif isinstance(value, str):
        s = value.strip()
        n = _num(s) if re.fullmatch(r"[+\-]?\d+(?:\.\d+)?", s) else None
        if n is not None:
            found = [n]
        else:
            for sign, hh, mm in _UTC_OFFSET_RE.findall(s):
                off = int(hh) + (int(mm) / 60.0 if mm else 0.0)
                n = _num(-off if sign in "-−" else off)
                if n is not None:
                    found.append(n)
    elif isinstance(value, Iterable):
        for v in value:
            found.extend(parse_utc_offsets(v))
    return sorted(set(found))
