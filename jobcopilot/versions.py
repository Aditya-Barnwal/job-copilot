"""Resume version writing — PDF generation + add_version helper."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from . import store


RESUMES_DIR = store.RESUMES_DIR


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_").lower()
    return s[:48] or "resume"


def write_pdf(text: str, out_path: Path) -> Path:
    """Render plain text into a simple PDF (reportlab). One column, wrapped."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        leftMargin=54, rightMargin=54, topMargin=54, bottomMargin=54,
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["Normal"],
                          fontName="Helvetica", fontSize=10, leading=13)
    story = []
    for para in (text or "").split("\n\n"):
        para = para.replace("\n", "<br/>").replace("&", "&amp;")
        story.append(Paragraph(para, body))
        story.append(Spacer(1, 6))
    doc.build(story)
    return out_path


def save_tailored_version(
    *, label: str, role_family: Optional[str], source_job_key: Optional[str],
    source_company: Optional[str], tailored_text: str,
    target_keywords: list[str], base_ats_coverage: float,
) -> dict:
    """Generate PDF + write a row in resume_versions. Returns the version row."""
    ts = int(time.time())
    fname = f"{_slug(source_company or 'resume')}_{_slug(role_family or label)}_{ts}.pdf"
    out_path = RESUMES_DIR / fname
    write_pdf(tailored_text, out_path)
    vid = store.add_version(
        label=label,
        role_family=role_family,
        source_job_key=source_job_key,
        source_company=source_company,
        tailored_text=tailored_text,
        pdf_path=str(out_path),
        target_keywords=target_keywords,
        base_ats_coverage=base_ats_coverage,
    )
    v = store.get_version(vid)
    return v or {"id": vid, "pdf_path": str(out_path)}
