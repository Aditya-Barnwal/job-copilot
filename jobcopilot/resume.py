"""Resume PDF extraction."""
from __future__ import annotations

import io
import re

from pypdf import PdfReader


_WS = re.compile(r"[ \t]+")
_MULTINL = re.compile(r"\n{3,}")


def extract_text(data: bytes) -> str:
    """Extract readable text from a PDF byte blob."""
    if not data:
        return ""
    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n".join(parts)
    text = _WS.sub(" ", text)
    text = _MULTINL.sub("\n\n", text)
    return text.strip()
