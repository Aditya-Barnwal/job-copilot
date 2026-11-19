"""The research section (patent, publication) is pinned: tailoring and the one-page guard never drop it."""
from jobcopilot import facts as F
from jobcopilot import tailor_v2 as T
import importlib
R = importlib.import_module("jobcopilot.resume.render")


def _fact(fid, section, kind="header", parent="", text="x"):
    f = F.Fact(id=fid, text=text, status="confirmed")
    f.section, f.kind, f.parent = section, kind, parent
    return f


USABLE = [_fact("res.patent", "research", text="Patent application"),
          _fact("res.paper", "research", text="Published paper"),
          _fact("hack.demo", "hackathons", text="Hackathon project"),
          _fact("hack.demo.b1", "hackathons", kind="bullet", parent="hack.demo", text="Built a demo")]


def test_pinned_ids_are_the_research_section_and_its_bullets():
    assert T.pinned_ids(USABLE) == {"res.patent", "res.paper"}


def test_validate_plan_never_excludes_research():
    plan, _ = T.validate_plan({"exclude": ["res.patent", "res.paper", "hack.demo"]}, USABLE)
    assert plan["exclude"] == ["hack.demo"]
    assert plan["pinned_kept"] == ["res.paper", "res.patent"]


def test_apply_plan_keeps_research_even_for_an_old_stored_plan():
    lines = T.apply_plan(USABLE, None, {"exclude": ["res.patent", "res.paper", "hack.demo"],
                                        "one_page_dropped": ["res.patent"]})
    printed = {fid for ln in lines for fid in ln["fact_ids"]}
    assert {"res.patent", "res.paper"} <= printed and "hack.demo" not in printed


def test_one_page_guard_never_offers_research():
    import inspect
    code = inspect.getsource(R._drop_candidates).split('"""', 2)[-1]     # body, without the docstring
    loops = [ln for ln in code.splitlines() if ln.strip().startswith("for key in")]
    assert loops and all('"research"' not in ln for ln in loops)
