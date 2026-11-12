"""LaTeX résumé renderer (jobcopilot/resume): escaping, content, edit plans, bases, compile.

Runs on a temp copy of the FAKE profile.example/. Tests that compile need
pdflatex (TinyTeX locally) and are skipped without it, e.g. in CI.
"""
from __future__ import annotations

import importlib
import re
import shutil
from pathlib import Path

import pytest
import yaml

from jobcopilot import facts as F
from jobcopilot.resume import (
    ContentError, FactsNotConfirmed, LatexUnavailable, available_bases, build_content,
    latex_escape, latex_url, pick_base, render, template_meta,
)
from jobcopilot.resume import latex as L
import importlib

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "profile.example"
HAS_TEX = L.find_pdflatex() is not None
# the package re-exports the function `render`, so fetch the module explicitly
R = importlib.import_module("jobcopilot.resume.render")
needs_tex = pytest.mark.skipif(not HAS_TEX, reason="pdflatex not installed")


@pytest.fixture
def profile(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "profile"
    shutil.copytree(EXAMPLE, d)
    monkeypatch.setenv("JOBCOPILOT_PROFILE_DIR", str(d))
    return d


@pytest.fixture
def fx(profile) -> F.ResumeFacts:
    return F.load(profile / "resume_facts.yaml")


def edit_yaml(path: Path, fn) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s.replace("—", "-").replace("–", "-"))


# --------------------------------------------------------------------------- escaping

def test_latex_escape_specials():
    s = latex_escape(r"50% & $5 #1 a_b {x} ~ ^ \cmd")
    assert s == (r"50\% \& \$5 \#1 a\_b \{x\} \textasciitilde{} \textasciicircum{} "
                 r"\textbackslash{}cmd")


def test_latex_escape_dashes_arrows_ligature_sequences():
    assert latex_escape("a — b – c → d") == r"a --- b -- c $\rightarrow$ d"
    assert latex_escape("x--y") == "x-{}-y"          # two hyphens stay two hyphens
    assert latex_escape("<<a>>") == r"\textless{}\textless{}a\textgreater{}\textgreater{}"
    assert latex_escape("ﬁle") == "file"             # a ligature glyph in the source -> letters


def test_latex_escape_drops_unmapped_chars():
    assert L.unsupported_chars("ok ✓ 你") == ["✓", "你"]
    assert latex_escape("ok ✓") == "ok "
    assert latex_escape("café naïve") == "café naïve"


def test_latex_url():
    assert latex_url("https://example.com/a%20b#x") == r"https://example.com/a\%20b\#x"
    assert latex_url("mailto:alex@example.com") == "mailto:alex@example.com"
    for bad in ("javascript:alert(1)", "https://x/{y}", "https://x/a b", "file:///etc/passwd", ""):
        assert latex_url(bad) == ""


# --------------------------------------------------------------------------- content

def ids(content, key):
    s = content.section(key)
    return [it.fact_id for it in s.items] if s else None


def test_build_content_backend_uses_only_usable_facts(fx):
    c = build_content("backend_sde", fx)
    assert [s.key for s in c.sections] == ["education", "skills", "experience", "projects", "hackathons"]
    assert [s.heading for s in c.sections][2] == "Work Experience"
    acme = c.section("experience").items[0]
    assert acme.fact_id == "exp.acme" and acme.title == "Acme Widgets Inc."
    assert acme.detail == "Software Engineer" and acme.detail_right == "Jan 2024 – Present"
    assert [b.fact_id for b in acme.bullets] == ["exp.acme.b1", "exp.acme.b2"]   # b3 unconfirmed
    assert ids(c, "experience") == ["exp.acme"]                                 # globex unconfirmed
    proj = c.section("projects").items[0]
    assert proj.tech == ["Python", "FastAPI"]                                   # pending tech cut
    assert proj.bullets[0].text.endswith("widget question.")                    # pending clause cut
    assert [b.fact_id for b in proj.bullets] == ["proj.widgetbot.b1"]           # b2 pending
    groups = c.section("skills").groups
    assert [(g.heading, [x.text for x in g.skills]) for g in groups] == [
        ("Languages", ["Go", "Python"]), ("Cloud", ["AWS (Lambda)"])]           # docker pending
    assert c.section("research") is None                                        # unconfirmed
    hack = c.section("hackathons").items[0]
    assert (hack.title, hack.subtitle, hack.bullets) == ("QueueSnap — Example Queue Tracker",
                                                         "(HackExample 2022)", [])
    edu = c.section("education").items[0]
    assert (edu.title, edu.right, edu.detail, edu.detail_right) == (
        "Example State University", "2019 – 2023", "B.Sc. in Computer Science", "GPA: 3.7")


def test_build_content_ml_ai_uses_base_wording_and_layout(fx):
    c = build_content("ml_ai", fx)
    assert [s.heading for s in c.sections] == ["Education", "Skills", "Experience", "Projects"]
    b2 = c.section("experience").items[0].bullets[1]
    assert b2.text.startswith("Cut nightly feature-batch runtime")
    assert ids(c, "projects") == ["proj.widgetbot"]          # sketchml unconfirmed


def test_header_from_answers_personal_then_person(fx):
    c = build_content("backend_sde", fx)
    assert c.header.name == "Alex Example"
    assert [x.kind for x in c.header.contact] == ["email", "phone"]
    assert c.header.contact[0].url == "mailto:alex@example.com"
    assert [x.text for x in c.header.links] == ["LinkedIn", "GitHub", "Portfolio"]
    answers = {"personal": {"full_name": "Sam Sample", "email": "sam@example.org",
                            "links": {"github": "https://example.org/sam"}}}
    h = build_content("backend_sde", fx, answers=answers).header
    assert h.name == "Sam Sample" and h.contact[0].text == "sam@example.org"
    assert dict((x.kind, x.url) for x in h.links)["github"] == "https://example.org/sam"
    assert dict((x.kind, x.url) for x in h.links)["linkedin"].endswith("/linkedin/alex-example")


def test_nothing_confirmed_refuses_cleanly(profile):
    path = profile / "resume_facts.yaml"
    text = path.read_text(encoding="utf-8").replace("status: confirmed", "status: unconfirmed")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(FactsNotConfirmed, match="confirm your facts first"):
        build_content("backend_sde", F.load(path))


def test_unknown_base_is_an_error(fx):
    with pytest.raises(ContentError):
        build_content("no_such_base", fx)


def test_lines_cite_fact_ids(fx):
    lines = build_content("backend_sde", fx).lines()
    assert all(ln["fact_ids"] for ln in lines)
    skills = [ln for ln in lines if ln["kind"] == "skills"]
    assert skills[0] == {"section": "skills", "kind": "skills", "heading": "Languages",
                         "text": "Go, Python", "fact_ids": ["skill.go", "skill.python"],
                         "rephrased": False}


# --------------------------------------------------------------------------- edit plan

def test_edit_plan_reorder_exclude_rephrase(fx):
    plan = {"order": {"exp.acme": ["exp.acme.b2", "exp.acme.b1"],
                      "projects": ["proj.widgetbot"]},
            "exclude": ["hack.hackexample"],
            "rephrase": {"exp.acme.b1": "Built a Go order service handling 200+ requests per second.",
                         "exp.acme": "Chief Widget Officer"},
            "skills_order": {"Languages": ["skill.python", "skill.go"],
                             "categories": ["Cloud", "Languages"]}}
    c = build_content("backend_sde", fx, edit_plan=plan)
    acme = c.section("experience").items[0]
    assert [b.fact_id for b in acme.bullets] == ["exp.acme.b2", "exp.acme.b1"]
    b1 = acme.bullets[1]
    assert b1.rephrased and b1.text.endswith("per second.") and "REST API" in b1.original
    assert acme.title == "Acme Widgets Inc."                       # header not rephrasable
    assert any("only bullets may be rephrased" in w for w in c.warnings)
    assert c.section("hackathons") is None                         # excluded -> empty -> dropped
    sk = c.section("skills").groups
    assert [g.heading for g in sk] == ["Cloud", "Languages"]
    assert [x.fact_id for x in sk[1].skills] == ["skill.python", "skill.go"]


def test_edit_plan_include_only_usable_facts(fx):
    plan = {"include": ["skill.go", "exp.acme.b3", "proj.sketchml", "hack.hackexample", "nope.x"]}
    c = build_content("ml_ai", fx, edit_plan=plan)
    assert "skill.go" in c.fact_ids()                              # confirmed, other base
    assert "exp.acme.b3" not in c.fact_ids()                       # unconfirmed
    assert "proj.sketchml" not in c.fact_ids()
    assert "hack.hackexample" not in c.fact_ids()                  # ml_ai has no hackathons section
    joined = " ".join(c.warnings)
    for i in ("exp.acme.b3", "proj.sketchml", "nope.x"):
        assert f"'{i}' is not a usable" in joined
    assert "'hack.hackexample' has no place" in joined


def test_edit_plan_must_be_a_mapping(fx):
    with pytest.raises(ContentError):
        build_content("backend_sde", fx, edit_plan=["nope"])


def test_drop_candidates_respect_priority(fx):
    c = build_content("backend_sde", fx)
    assert R._drop_candidates(c) == ["exp.acme.b2"]                 # last bullet, multi-bullet role
    c = build_content("backend_sde", fx, edit_plan={"priority": ["exp.acme.b2", "exp.acme.b1"]})
    assert R._drop_candidates(c) == ["exp.acme.b1", "exp.acme.b2"]  # least important first
    c = build_content("backend_sde", fx, edit_plan={"priority": {"exp.acme.b1": 1, "exp.acme.b2": 5}})
    assert R._drop_candidates(c)[0] == "exp.acme.b1"


# --------------------------------------------------------------------------- bases

def test_available_bases_and_pick_base(profile):
    assert available_bases() == ["backend_sde", "ml_ai"]
    assert template_meta("ml_ai")["roles"] == ["ml", "ai", "genai", "data"]
    cases = {"Senior Backend Engineer": "backend_sde", "Machine Learning Engineer": "ml_ai",
             "Data Scientist": "ml_ai", "GenAI Engineer": "ml_ai", "Full Stack Developer": "backend_sde",
             "Software Engineer": "backend_sde", "Gardener": "backend_sde"}
    for title, base in cases.items():
        assert pick_base({"title": title}) == base, title
    assert pick_base({"title": "Engineer", "role_family": "nlp"}) == "ml_ai"
    assert pick_base({"title": "ML Engineer", "role_family": "backend"}) == "backend_sde"


def test_new_base_by_dropping_a_file(profile, fx):
    d = profile / "base_resumes"
    src = (d / "backend_sde.tex.j2").read_text(encoding="utf-8")
    body = src.split("\n", 1)[1]
    (d / "platform.tex.j2").write_text("% roles: platform, sre, devops\n% facts_base: backend_sde\n"
                                       + body, encoding="utf-8")
    (d / "notes.tex.j2").write_text("% just a draft, no roles header\n", encoding="utf-8")
    assert available_bases() == ["backend_sde", "ml_ai", "platform"]
    assert pick_base({"title": "Site Reliability Engineer", "role_family": "sre"}) == "platform"
    c = build_content("platform", fx)
    assert c.base == "platform" and c.section("experience") is not None
    assert "\\begin{document}" in R.render_tex(c, "platform")


def test_template_escapes_every_value(fx):
    evil = r"Shipped \input{/etc/passwd} at 100% $x$ #1 {}"
    c = build_content("backend_sde", fx, edit_plan={"rephrase": {"exp.acme.b1": evil}})
    tex = R.render_tex(c, "backend_sde")
    assert r"\input{/etc/passwd}" not in tex
    assert r"\textbackslash{}input\{/etc/passwd\} at 100\% \$x\$ \#1 \{\}" in tex


def test_legacy_extract_text_still_importable():
    from jobcopilot.resume import extract_text
    assert extract_text(b"") == ""


def test_render_without_pdflatex_raises(fx, tmp_path, monkeypatch):
    monkeypatch.setattr(L, "find_pdflatex", lambda cfg=None: None)
    with pytest.raises(LatexUnavailable):
        render(build_content("backend_sde", fx), "backend_sde", tmp_path / "out")


def test_ats_check_counts_missing_probes(fx):
    c = build_content("backend_sde", fx)
    ratio, missing = R.ats_check(c, "Education Skills")
    assert ratio < 0.9 and "Work Experience" in missing


# --------------------------------------------------------------------------- patent and research

PATENT = ("Patent Application (Exampleland) No. 123456789 — Example Widget Platform with "
          "Real-Time Guidance. Filed through ESU, Nov 2025; published Dec 2025; under examination.")
PUBLICATION = ("WidgetCrypt: Example Steganography with AES-RSA Encryption — Image tool using "
               "AES-256 and RSA-4096 encryption, achieving 48 dB PSNR and 0.98 SSIM while "
               "preserving image quality.")


@pytest.fixture
def research_profile(profile) -> F.ResumeFacts:
    def facts_fn(d):
        d["research"] = [
            {"id": "res.patent", "kind": "patent", "text": PATENT, "metrics": ["123456789"],
             "sources": ["backend_sde", "ml_ai"], "status": "confirmed"},
            {"id": "res.crypt", "kind": "publication", "text": PUBLICATION,
             "metrics": ["AES-256", "48 dB"], "sources": ["backend_sde", "ml_ai"], "status": "confirmed"},
        ]

    def bases_fn(d):
        for b in d["bases"].values():
            b["headings"]["research"] = "Patent and Research"
    edit_yaml(profile / "resume_facts.yaml", facts_fn)
    edit_yaml(profile / "base_resumes" / "bases.yaml", bases_fn)
    return F.load(profile / "resume_facts.yaml")


@pytest.mark.parametrize("base", ["backend_sde", "ml_ai"])
def test_patent_and_research_section_unchanged(research_profile, base, tmp_path):
    c = build_content(base, research_profile)
    sec = c.section("research")
    assert sec.heading == "Patent and Research"
    assert [it.fact_id for it in sec.items] == ["res.patent", "res.crypt"]
    assert [it.text for it in sec.items] == [PATENT, PUBLICATION]
    pat, pub = sec.items
    assert pat.lead + " — " + pat.emph + pat.rest == PATENT and pat.emph   # title italic
    assert pub.lead + " — " + pub.rest == PUBLICATION and not pub.emph     # publication: as research
    tex = R.render_tex(c, base)
    assert r"\textit{Example Widget Platform with Real-Time Guidance}" in tex
    assert r"\textbf{WidgetCrypt: Example Steganography with AES-RSA Encryption} --- Image" in tex
    if HAS_TEX:
        r = render(c, base, tmp_path / base)
        text = squash(r.text)
        i, j, k = (text.find(squash(x)) for x in ("Patent and Research", PATENT, PUBLICATION))
        assert -1 < i < j < k, (i, j, k)


# --------------------------------------------------------------------------- compile

@needs_tex
@pytest.mark.parametrize("base", ["backend_sde", "ml_ai"])
def test_render_compiles_one_page_ats_safe(fx, base, tmp_path):
    plan = {"rephrase": {"exp.acme.b1": "Built an efficient offline-first Go service with "
                                        "fine-grained traffic shaping."}}
    c = build_content(base, fx, edit_plan=plan)
    r = render(c, base, tmp_path / "out")
    assert r.pages == 1 and r.warnings == [] and r.ats_ratio == 1.0
    assert r.tex_path.read_text(encoding="utf-8").startswith("% roles:")
    assert not any(ch in r.text for ch in R.LIGATURES)
    for word in ("efficient", "offline-first", "fine-grained", "traffic"):
        assert word in r.text
    for heading in (s.heading for s in c.sections):
        assert heading in r.text
    from pypdf import PdfReader
    page = PdfReader(str(r.pdf_path)).pages[0]
    fonts = {str(f.get_object()["/BaseFont"]).split("+")[-1] for f in page["/Resources"]["/Font"].values()}
    assert {"NimbusRomNo9L-Medi", "NimbusRomNo9L-Regu", "NimbusRomNo9L-ReguItal"} <= fonts
    uris = [a.get_object()["/A"]["/URI"] for a in page["/Annots"]]
    assert "mailto:alex@example.com" in uris and len(uris) == 4    # email + 3 profile links
    assert [float(x) for x in page.mediabox[2:]] == [612.0, 792.0]  # US letter


@needs_tex
def test_one_page_guard_drops_lowest_priority(fx, tmp_path):
    long = "Cut nightly batch runtime by 30% " + "while keeping every widget price correct " * 400
    c = build_content("backend_sde", fx, edit_plan={"rephrase": {"exp.acme.b2": long.strip() + "."}})
    r = render(c, "backend_sde", tmp_path / "out")
    assert r.pages == 1 and r.dropped == ["exp.acme.b2"]
    assert "exp.acme.b2" not in c.fact_ids()
    assert any("one-page guard dropped 1" in w for w in r.warnings)


@needs_tex
def test_latex_error_is_reported(tmp_path):
    with pytest.raises(L.LatexError) as e:
        L.compile_tex(r"\documentclass{article}\begin{document}\undefinedmacro\end{document}")
    assert "Undefined control sequence" in e.value.log_tail
