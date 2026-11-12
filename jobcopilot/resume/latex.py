"""LaTeX plumbing for the résumé renderer: strict escaping, config, pdflatex.

Every value that reaches a template goes through `latex_escape` (text) or
`latex_url` (an \\href target): résumé facts, JD-derived rephrasings and
answers are all untrusted as far as TeX is concerned, so no value may ever
open a command, a group, a math shift or a comment. pdflatex runs without a
shell (`shell=False`), with `-no-shell-escape`, a timeout, paranoid
`openin_any`/`openout_any`, in a throwaway directory.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("jobcopilot.resume")

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "latex.yaml"
TINYTEX_DEFAULT = Path("~/Library/TinyTeX/bin/universal-darwin/pdflatex").expanduser()

DEFAULTS: dict[str, Any] = {
    "pdflatex": "", "timeout_s": 60, "runs": 2, "max_fit_tries": 6,
    "ats_min_ratio": 0.9, "default_base": "backend_sde",
    "family_aliases": {}, "title_keywords": {},
}


class LatexError(Exception):
    """pdflatex failed; `log_tail` holds the end of its log (no résumé text is logged)."""

    def __init__(self, msg: str, log_tail: str = ""):
        super().__init__(msg)
        self.log_tail = log_tail


class LatexUnavailable(LatexError):
    """No pdflatex binary could be found."""


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path) if path else CONFIG_PATH
    cfg = dict(DEFAULTS)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if v is not None})
    return cfg


def find_pdflatex(cfg: dict[str, Any] | None = None) -> str | None:
    cfg = cfg if cfg is not None else load_config()
    for cand in (os.environ.get("JOBCOPILOT_PDFLATEX"), cfg.get("pdflatex")):
        if cand:
            p = Path(str(cand)).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    if TINYTEX_DEFAULT.is_file():
        return str(TINYTEX_DEFAULT)
    return shutil.which("pdflatex")


# --------------------------------------------------------------------------- escaping

_ASCII = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}", "<": r"\textless{}", ">": r"\textgreater{}",
    "|": r"\textbar{}", "`": r"\textasciigrave{}", '"': r"\textquotedbl{}",
}
_UNICODE = {
    "—": "---", "–": "--", "‒": "--", "―": "---", "−": "-",
    "‐": "-", "‑": "-",
    "→": r"$\rightarrow$", "←": r"$\leftarrow$", "↔": r"$\leftrightarrow$",
    "⇒": r"$\Rightarrow$", "⇐": r"$\Leftarrow$", "⇔": r"$\Leftrightarrow$",
    "↑": r"$\uparrow$", "↓": r"$\downarrow$",
    "•": r"\textbullet{}", "·": r"\textperiodcentered{}", "…": r"\ldots{}",
    "‘": "`", "’": "'", "“": "``", "”": "''",
    "×": r"$\times$", "≤": r"$\leq$", "≥": r"$\geq$", "±": r"$\pm$",
    "≈": r"$\approx$", "≠": r"$\neq$", "°": r"\textdegree{}",
    "₹": "Rs.", "€": r"\texteuro{}", "£": r"\pounds{}",
    "©": r"\textcopyright{}", "®": r"\textregistered{}", "™": r"\texttrademark{}",
    " ": "~", " ": r"\,", " ": r"\,", "​": "",
}
_LIGATURE_FORMS = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
                   "ﬅ": "st", "ﬆ": "st"}


# Latin-1 Supplement + Latin Extended-A letters / numbers that T1 + textcomp +
# utf8 inputenc define (each verified to compile: tests/test_resume_render.py).
# Not in it, on purpose: U+0126 U+0127 U+0138 U+013F U+0140 U+0149 U+0166 U+0167
# U+017F, which have no T1 definition and would abort the compile.
_NO_T1 = frozenset("\u0126\u0127\u0138\u013f\u0140\u0149\u0166\u0167\u017f")
LATIN_PASSTHROUGH = frozenset(
    chr(o) for o in range(0xA1, 0x180)
    if unicodedata.category(chr(o)).startswith(("L", "N")) and chr(o) not in _NO_T1)


def unsupported_chars(text: str) -> list[str]:
    """Characters `latex_escape` has to drop (no T1/TS1 glyph or mapping)."""
    s = unicodedata.normalize("NFC", str(text or ""))
    return sorted({c for c in s if _rendered(c) is None})


def _rendered(c: str) -> str | None:
    if c in _ASCII:
        return _ASCII[c]
    if c in _UNICODE:
        return _UNICODE[c]
    if c in _LIGATURE_FORMS:
        return _LIGATURE_FORMS[c]
    if c in "\n\t\r":
        return " "
    o = ord(c)
    if 0x20 <= o < 0x7F:
        return c
    if c in LATIN_PASSTHROUGH:
        return c
    return None


# TeX input ligatures (T1): pairs of emitted characters that would fuse into
# another glyph: -- ---, `` '', ,, (low quote), << >>, !` (inverted !), ?` (inverted ?).
_LIG_PAIRS = {("-", "-"), ("`", "`"), ("'", "'"), (",", ","), ("<", "<"), (">", ">"),
              ("!", "`"), ("?", "`")}


def latex_escape(value: Any) -> str:
    """Strict text escape. Every TeX special is neutralised, TeX ligature
    sequences that would change the text (--, ``, '', ,,, <<, !`, ?`) are broken
    (judged on the characters actually emitted, so a dropped character in
    between can't create one), dashes / arrows / quotes become proper LaTeX, and
    characters with no mapping are dropped (see `unsupported_chars`) instead of
    breaking the run."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFC", str(value))
    out: list[str] = []
    last = ""                           # last character emitted so far
    for c in s:
        r = _rendered(c)
        if not r:
            continue
        if (last, r[0]) in _LIG_PAIRS:
            out.append("{}")
        out.append(r)
        last = r[-1]
    return "".join(out)


# What an escaped value reads as in the PDF text layer (for the ATS check).
_PLAIN = {"₹": "Rs.", "…": "...", "\u00a0": " ", "\u2009": " ", "\u202f": " ", "\u200b": "",
          **_LIGATURE_FORMS}


def plain_text(value: Any) -> str:
    """`value` as the PDF text layer should show it: the escaper's own
    substitutions (₹ -> Rs., … -> ..., ligature glyphs -> letters), dropped
    characters removed. Quotes and dashes are left for the caller to fold."""
    s = unicodedata.normalize("NFC", str(value or ""))
    out = []
    for c in s:
        if c in _PLAIN:
            out.append(_PLAIN[c])
        elif c in "\n\t\r":
            out.append(" ")
        elif _rendered(c) is not None:
            out.append(c)
    return "".join(out)


_URL_OK = re.compile(r"^(https?://|mailto:|tel:)[^\s\\{}]+$", re.I)
# Inside \href only % and # need a backslash; characters TeX could read as
# something else (^ starts ^^xx char codes, ~ is active, plus the ones that are
# never valid raw in a URL) are percent-encoded, which is the same URL.
_URL_PCT = {"^": "%5E", "~": "%7E", '"': "%22", "<": "%3C", ">": "%3E", "|": "%7C", "`": "%60"}


def latex_url(value: Any) -> str:
    """Escape a URL for \\href{...}. Only http(s), mailto and tel; anything else
    (or a URL with spaces / braces / backslashes / control or non-ASCII
    characters) yields "" so no link is made."""
    u = str(value or "").strip()
    if not u or any(ord(c) < 0x20 or ord(c) > 0x7E for c in u) or not _URL_OK.match(u):
        return ""
    u = "".join(_URL_PCT.get(c, c) for c in u)
    return u.replace("%", r"\%").replace("#", r"\#")


# --------------------------------------------------------------------------- compile

@dataclass
class Compiled:
    pdf: bytes
    log_tail: str


def compile_tex(tex: str, *, cfg: dict[str, Any] | None = None, jobname: str = "resume") -> Compiled:
    """Compile `tex` with pdflatex (`runs` passes) in a temp dir; returns the PDF bytes."""
    cfg = cfg if cfg is not None else load_config()
    exe = find_pdflatex(cfg)
    if not exe:
        raise LatexUnavailable("pdflatex not found: install TinyTeX or set `pdflatex` in "
                               "config/latex.yaml (or $JOBCOPILOT_PDFLATEX)")
    timeout = float(cfg.get("timeout_s") or 60)
    runs = max(1, int(cfg.get("runs") or 2))
    env = dict(os.environ, openin_any="p", openout_any="p", shell_escape="f")
    with tempfile.TemporaryDirectory(prefix="jc-tex-") as tmp:
        d = Path(tmp)
        (d / f"{jobname}.tex").write_text(tex, encoding="utf-8")
        cmd = [exe, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape",
               "-file-line-error", f"-jobname={jobname}", f"{jobname}.tex"]
        for _ in range(runs):
            try:
                proc = subprocess.run(cmd, cwd=d, env=env, capture_output=True,
                                      timeout=timeout, shell=False, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired as e:
                raise LatexError(f"pdflatex timed out after {timeout:.0f}s") from e
            logf = d / f"{jobname}.log"
            tail = _log_tail(logf.read_text(encoding="latin-1") if logf.exists() else "")
            if proc.returncode != 0:
                raise LatexError(f"pdflatex failed (exit {proc.returncode})", tail)
        pdf = d / f"{jobname}.pdf"
        if not pdf.exists():
            raise LatexError("pdflatex produced no PDF", tail)
        return Compiled(pdf=pdf.read_bytes(), log_tail=tail)


def _log_tail(text: str) -> str:
    """Error lines only (`! ...` and file:line: messages) — never the typeset text."""
    keep = [ln for ln in text.splitlines() if ln.startswith("!") or re.match(r"^\S+\.tex:\d+:", ln)
            or "Overfull" in ln or "Undefined control" in ln]
    return "\n".join(keep[-20:])


def pdf_pages_and_text(pdf_path: Path) -> tuple[int, str]:
    from pypdf import PdfReader
    r = PdfReader(str(pdf_path))
    return len(r.pages), "\n".join((p.extract_text() or "") for p in r.pages)
