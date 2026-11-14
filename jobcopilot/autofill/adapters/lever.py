"""Lever adapter (jobs.lever.co/<site>/<id>/apply), declarative: specs/lever.yaml.

Lever's custom questions ("cards") carry no <label> the browser can tie to
the input; the question text lives in each card's hidden baseTemplate JSON
(name="cards[<uuid>][baseTemplate]"). snapshot_from_html() reads that JSON
from the server-rendered page (read-only, no browser) so the plan asks
answers.answer_question the real question.
"""
from __future__ import annotations

import html as _html
import json
import re
from html.parser import HTMLParser
from typing import Any

from .base import register
from .greenhouse import SpecAdapter, _css_str

_CARD_TYPES = {"text": "text", "textarea": "textarea", "dropdown": "select",
               "multiple-choice": "radio", "multiple-select": "multi"}
_CARD_NAME_RE = re.compile(r"^cards\[([0-9a-fA-F-]{8,})\]\[baseTemplate\]$")


class _Inputs(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.form_depth = 0
        self.items: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "form" and a.get("id") == "application-form":
            self.form_depth = 1
            return
        if self.form_depth and tag in ("input", "select", "textarea"):
            self.items.append({"tag": tag, **a})

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.form_depth:
            self.form_depth = 0


@register
class LeverAdapter(SpecAdapter):
    spec_name = "lever"

    @classmethod
    def snapshot_from_html(cls, page_html: str, url: str = "") -> dict:
        """Server-rendered apply page -> snapshot (one step). Page text is
        untrusted data: only labels, names, types and option texts are kept."""
        p = _Inputs()
        p.feed(page_html or "")
        fields: list[dict] = []
        by_qa = {}
        for e in cls.spec.get("fields") or []:
            m = re.search(r'\[data-qa="([^"]+)"\]', e["locator"])
            if m:
                by_qa[m.group(1)] = e.get("question")
            m = re.search(r'\[name="([^"]+)"\]', e["locator"])
            if m:
                by_qa["name:" + m.group(1)] = e.get("question")
        seen = set()
        for it in p.items:
            name, typ = it.get("name", ""), (it.get("type") or "text").lower()
            if it["tag"] == "input" and typ == "hidden":
                m = _CARD_NAME_RE.match(name)
                if not m:
                    continue
                try:
                    card = json.loads(_html.unescape(it.get("value") or "{}"))
                except ValueError:
                    continue
                for i, f in enumerate(card.get("fields") or []):
                    ftyp = _CARD_TYPES.get(str(f.get("type") or ""), "text")
                    fname = f"cards[{m.group(1)}][field{i}]"
                    fields.append({
                        "label": str(f.get("text") or "").strip(), "name": fname, "id": "",
                        "type": ftyp, "required": bool(f.get("required")),
                        "options": [str(o.get("text")) for o in f.get("options") or [] if o.get("text")],
                        "hidden": False, "locator": f"[name={_css_str(fname)}]", "card": True})
                    seen.add(fname)
                continue
            if not name or name in seen or typ in ("submit", "button", "hidden", "password") \
                    or name.startswith("cards["):
                continue
            seen.add(name)
            qa = it.get("data-qa", "")
            label = by_qa.get(qa) or by_qa.get("name:" + name) or ""
            m_url = re.fullmatch(r"urls\[([\w .-]+)\]", name)
            if not label and m_url:
                label = f"{m_url.group(1)} URL"
            if not label and name == "comments":
                label = "Additional information"
            if typ == "file":
                label = label or ("Resume/CV" if "resume" in name else name)
            ftyp = "textarea" if it["tag"] == "textarea" else ("select" if it["tag"] == "select" else (
                "file" if typ == "file" else "text"))
            loc = f'[data-qa={_css_str(qa)}]' if qa else f"[name={_css_str(name)}]"
            fields.append({"label": label or name, "name": name, "id": it.get("id", ""), "type": ftyp,
                           "required": "required" in it, "options": [], "hidden": False,
                           "locator": loc})
        return {"source": "lever_html", "url": url, "steps": [fields]}


__all__ = ["LeverAdapter"]
