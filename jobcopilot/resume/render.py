"""Render ResumeContent through a base's Jinja2 LaTeX template into a 1-page PDF.

Templates live in `<profile>/base_resumes/<base>.tex.j2` and start with

    % roles: backend, sde, full-stack        (required: makes it a base)
    % facts_base: backend_sde                (optional: borrow that base's layout + wording)

so a new base is added by dropping in a file. Jinja delimiters are LaTeX-safe:
`((* block *))`, `((( value )))`, `((= comment =))`, and every printed value is
passed through `latex_escape` automatically (URLs through the `url` filter).

`render()` compiles twice with pdflatex, counts pages with pypdf, and runs the
one-page guard: while the PDF has more than one page it drops the
lowest-priority fact (edit-plan `priority`, else the last bullet of the last
multi-bullet hackathon / project / role) and re-renders, at most
`max_fit_tries` times. It then checks the PDF text round-trips for ATS parsers.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jinja2

from jobcopilot import facts as F
from jobcopilot.resume import latex as L
from jobcopilot.resume.content import (  # noqa: F401  (contract: importable from here)
    ContentError, FactsNotConfirmed, ResumeContent, build_content,
)

log = logging.getLogger("jobcopilot.resume")

_HEADER_RE = re.compile(r"^%\s*([a-z_]+)\s*:\s*(.*?)\s*$")


@dataclass
class RenderResult:
    tex_path: Path
    pdf_path: Path
    pages: int
    text: str
    warnings: list[str] = field(default_factory=list)
    content: ResumeContent | None = None
    dropped: list[str] = field(default_factory=list)
    ats_ratio: float = 1.0


# --------------------------------------------------------------------------- bases

def templates_dir(profile_dir: Path | str | None = None) -> Path:
    return Path(profile_dir or F.profile_dir()) / "base_resumes"


def template_path(base: str, profile_dir: Path | str | None = None) -> Path:
    return templates_dir(profile_dir) / f"{base}.tex.j2"


def template_meta(base: str, profile_dir: Path | str | None = None) -> dict[str, Any]:
    """`% key: value` header lines of a template (first comment block only)."""
    p = template_path(base, profile_dir)
    meta: dict[str, Any] = {"path": p, "roles": [], "facts_base": None}
    if not p.exists():
        return meta
    for line in p.read_text(encoding="utf-8").splitlines()[:20]:
        if not line.startswith("%"):
            break
        m = _HEADER_RE.match(line)
        if m and m.group(1) == "roles":
            meta["roles"] = [r.strip().lower() for r in m.group(2).split(",") if r.strip()]
        elif m and m.group(1) == "facts_base":
            meta["facts_base"] = m.group(2) or None
    return meta


def available_bases(profile_dir: Path | str | None = None) -> list[str]:
    """Every `*.tex.j2` in base_resumes/ whose header has a `% roles:` line."""
    d = templates_dir(profile_dir)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.tex.j2")):
        base = p.name[: -len(".tex.j2")]
        if template_meta(base, profile_dir)["roles"]:
            out.append(base)
    return out


def _norm_role(s: str) -> str:
    return re.sub(r"[\s_-]+", "", str(s).lower())


def pick_base(job: dict, *, profile_dir: Path | str | None = None,
              cfg: dict[str, Any] | None = None) -> str:
    """Base for a job: its role family (job['role_family'], else derived from the
    title) mapped onto a base's roles; title keywords as a fallback; else the
    configured default. Roles = template `% roles:` ∪ bases.yaml `roles`."""
    cfg = cfg if cfg is not None else L.load_config()
    bases = available_bases(profile_dir)
    if not bases:
        raise FileNotFoundError(f"no base templates (*.tex.j2 with '% roles:') in "
                                f"{templates_dir(profile_dir)}")
    roles: dict[str, set[str]] = {b: {_norm_role(r) for r in template_meta(b, profile_dir)["roles"]}
                                  for b in bases}
    layouts_p = templates_dir(profile_dir) / "bases.yaml"
    if layouts_p.exists():
        try:
            for k, lay in F.load_bases(layouts_p).items():
                if k in roles:
                    roles[k] |= {_norm_role(r) for r in lay.roles}
        except F.FactsError:
            log.warning("bases.yaml unreadable; using template roles only")

    aliases = {_norm_role(k): _norm_role(v) for k, v in (cfg.get("family_aliases") or {}).items()}

    def match(token: str) -> str | None:
        t = _norm_role(token)
        for cand in (t, aliases.get(t)):
            for b in bases:
                if cand and cand in roles[b]:
                    return b
        return None

    title = str(job.get("title") or "")
    family = job.get("role_family")
    if not family and title:
        try:
            from jobcopilot.filters import role_match
            rm = role_match(title)
            family = rm[0] if rm else None
        except Exception:  # pragma: no cover - filters is optional here
            family = None
    if family:
        b = match(str(family))
        if b:
            return b
    low = f" {title.lower()} "
    for kw, role in sorted((cfg.get("title_keywords") or {}).items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"(?<![a-z0-9]){re.escape(str(kw).lower())}(?![a-z0-9])", low):
            b = match(str(role))
            if b:
                return b
    default = cfg.get("default_base")
    return default if default in bases else bases[0]


# --------------------------------------------------------------------------- jinja

class _Raw(str):
    """Already-escaped LaTeX (only produced by the filters below)."""


def _finalize(v: Any) -> str:
    return v if isinstance(v, _Raw) else L.latex_escape(v)


def _env(directory: Path) -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(directory)),
        block_start_string="((*", block_end_string="*))",
        variable_start_string="(((", variable_end_string=")))",
        comment_start_string="((=", comment_end_string="=))",
        line_comment_prefix=None, trim_blocks=True, lstrip_blocks=True,
        autoescape=False, undefined=jinja2.StrictUndefined, finalize=_finalize,
        keep_trailing_newline=True)
    env.filters["url"] = lambda v: _Raw(L.latex_url(v))
    # For the template's own macro output only (its values were escaped inside the macro).
    env.filters["raw"] = lambda v: _Raw(v)
    return env


def render_tex(content: ResumeContent, base: str, *, profile_dir: Path | str | None = None) -> str:
    p = template_path(base, profile_dir)
    if not p.exists():
        raise FileNotFoundError(f"no template for base '{base}' ({p.name})")
    tpl = _env(p.parent).get_template(p.name)
    return tpl.render(header=content.header, sections=content.sections, base=base)


# --------------------------------------------------------------------------- guard + checks

def _drop_candidates(content: ResumeContent) -> list[str]:
    """Fact ids the one-page guard may remove, most droppable first."""
    order: list[str] = []
    for key in ("hackathons", "projects", "experience"):
        sec = content.section(key)
        if sec is None:
            continue
        for it in reversed(sec.items):
            if len(it.bullets) > 1:
                order.append(it.bullets[-1].fact_id)
    if content.priority:
        rank = {fid: i for i, fid in enumerate(content.priority)}
        extra = []
        for key in ("hackathons", "projects", "research"):
            sec = content.section(key)
            if sec and len(sec.items) > 1:
                extra += [it.fact_id for it in reversed(sec.items) if it.fact_id in rank]
        for sec in content.sections:
            for it in sec.items:
                if len(it.bullets) > 1:
                    extra += [b.fact_id for b in it.bullets if b.fact_id in rank]
        order = list(dict.fromkeys(order + extra))
        inf = float("inf")
        order.sort(key=lambda fid: -rank.get(fid, inf))   # unlisted first, then least important
    return order


_DASHES = str.maketrans({"—": "-", "–": "-", "‒": "-", "’": "'", "‘": "'", "“": '"', "”": '"'})
LIGATURES = "ﬀﬁﬂﬃﬄﬅﬆ"


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s.translate(_DASHES)).lower()


def ats_check(content: ResumeContent, text: str) -> tuple[float, list[str]]:
    """Share of section headings + bullet / research openings (first 6 words)
    found in the extracted PDF text, and the probes that were missing."""
    hay = _squash(text)
    probes: list[str] = []
    for s in content.sections:
        probes.append(s.heading)
        for it in s.items:
            if it.kind == "research":
                probes.append(" ".join(it.text.split()[:6]))
            probes += [" ".join(b.text.split()[:6]) for b in it.bullets]
    probes = [p for p in probes if p]
    missing = [p for p in probes if _squash(p) not in hay]
    return (1 - len(missing) / len(probes)) if probes else 1.0, missing


def render(content: ResumeContent, base: str, out_dir: Path | str, *,
           profile_dir: Path | str | None = None, cfg: dict[str, Any] | None = None,
           stem: str = "resume") -> RenderResult:
    """Render + compile `content`; writes `<stem>.tex` / `<stem>.pdf` into out_dir.
    `content` is updated in place when the one-page guard drops facts."""
    cfg = cfg if cfg is not None else L.load_config()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    warnings = list(content.warnings)
    bad = sorted({c for ln in content.lines() for c in L.unsupported_chars(ln["text"])}
                 | {c for c in L.unsupported_chars(content.header.name)})
    if bad:
        warnings.append(f"characters with no LaTeX mapping were dropped: {[hex(ord(c)) for c in bad]}")

    tries = max(0, int(cfg.get("max_fit_tries", 6)))
    dropped: list[str] = []
    tex_p, pdf_p = out / f"{stem}.tex", out / f"{stem}.pdf"
    while True:
        tex = render_tex(content, base, profile_dir=profile_dir)
        compiled = L.compile_tex(tex, cfg=cfg, jobname=stem)
        tex_p.write_text(tex, encoding="utf-8")
        pdf_p.write_bytes(compiled.pdf)
        pages, text = L.pdf_pages_and_text(pdf_p)
        if pages <= 1 or len(dropped) >= tries:
            break
        cands = _drop_candidates(content)
        if not cands:
            break
        content.drop(cands[0])
        dropped.append(cands[0])
    content.dropped = dropped
    if dropped:
        warnings.append(f"one-page guard dropped {len(dropped)} line(s): {dropped}")
    if pages > 1:
        warnings.append(f"résumé is {pages} pages after {len(dropped)} drop(s); needs review")
    if any(c in text for c in LIGATURES):
        warnings.append("extracted text contains ligature glyphs (ATS-unsafe)")
    ratio, missing = ats_check(content, text)
    if ratio < float(cfg.get("ats_min_ratio", 0.9)):
        warnings.append(f"ATS round-trip: only {ratio:.0%} of headings/bullet openings found in "
                        f"the PDF text ({len(missing)} missing)")
    return RenderResult(tex_path=tex_p, pdf_path=pdf_p, pages=pages, text=text, warnings=warnings,
                        content=content, dropped=dropped, ats_ratio=ratio)


def find_pdflatex() -> str | None:
    return L.find_pdflatex()


__all__ = ["RenderResult", "ResumeContent", "build_content", "render", "render_tex", "pick_base",
           "available_bases", "template_meta", "templates_dir", "ats_check", "find_pdflatex"]
