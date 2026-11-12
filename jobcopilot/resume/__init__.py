"""LaTeX résumés (Phase 3, PLAN D4/D5): content from usable facts -> Jinja -> pdflatex.

    from jobcopilot.resume import build_content, render, pick_base, available_bases

This package replaced the v1 module `jobcopilot/resume.py` (PDF text
extraction); its `extract_text` is re-exported here so
`from jobcopilot.resume import extract_text` keeps working.
"""
from __future__ import annotations

import importlib.util as _ilu
from pathlib import Path as _Path

from jobcopilot.resume.content import (  # noqa: F401
    Contact, ContentError, FactsNotConfirmed, Header, Item, Line, ResumeContent, Section,
    SkillGroup, build_content,
)
from jobcopilot.resume.latex import LatexError, LatexUnavailable, latex_escape, latex_url  # noqa: F401
from jobcopilot.resume.render import (  # noqa: F401
    RenderResult, UnsupportedCharacters, available_bases, pick_base, render, template_meta,
)


def _legacy_extract():
    """Load the v1 jobcopilot/resume.py (shadowed by this package) for extract_text."""
    path = _Path(__file__).resolve().parent.parent / "resume.py"
    if not path.exists():
        return None
    spec = _ilu.spec_from_file_location("jobcopilot._resume_v1", path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.extract_text


_extract = _legacy_extract()
if _extract is not None:
    extract_text = _extract
else:  # pragma: no cover - the v1 module was moved/removed
    def extract_text(data: bytes) -> str:
        import io
        import re
        from pypdf import PdfReader
        if not data:
            return ""
        parts = []
        for page in PdfReader(io.BytesIO(data)).pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = re.sub(r"[ \t]+", " ", "\n".join(parts))
        return re.sub(r"\n{3,}", "\n\n", text).strip()
