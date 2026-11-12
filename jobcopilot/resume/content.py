"""ResumeContent: what a base résumé template prints, built from usable facts.

`build_content(base, facts, edit_plan=None)` takes only `facts.usable_facts()`
(confirmed facts, unconfirmed claims cut out — fail closed) laid out per
`base_resumes/bases.yaml`. An edit plan may only reorder, include, exclude or
rephrase facts *by id*; it never adds free text except a bullet's rephrasing,
which keeps its `fact_id` so grounding (tailor_v2 / grounding v3) can check it.
Locked fields (employer, title, dates, degree, grade, project names) come from
the facts' structured fields and are never editable here. The header comes
from answers.yaml `personal` (falling back to resume_facts `person`); nothing
is invented — an empty value is simply not printed.
"""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobcopilot import facts as F

log = logging.getLogger("jobcopilot.resume")

LINK_LABELS = {"linkedin": "LinkedIn", "github": "GitHub", "portfolio": "Portfolio"}


class ContentError(Exception):
    """The résumé can't be built (unknown base, missing layout, bad edit plan)."""


class FactsNotConfirmed(ContentError):
    """No usable (confirmed) facts for this base: tailoring must refuse."""


# --------------------------------------------------------------------------- model

@dataclass
class Line:
    fact_id: str
    text: str
    original: str = ""          # the base wording when `text` is a rephrasing
    rephrased: bool = False


@dataclass
class Item:
    """One entry: an education row, a role, a project, a research item or a hackathon."""
    fact_id: str
    kind: str
    text: str                   # the usable fact text (base wording)
    title: str = ""             # bold, row 1 left
    subtitle: str = ""          # plain after the title (hackathon event)
    right: str = ""             # row 1 right (dates / location)
    detail: str = ""            # italic, row 2 left (degree / job title)
    detail_right: str = ""      # italic, row 2 right (grade / dates)
    tech: list[str] = field(default_factory=list)
    link: str = ""
    lead: str = ""              # research: bold lead, italic emph, plain rest
    emph: str = ""
    rest: str = ""
    bullets: list[Line] = field(default_factory=list)


@dataclass
class SkillGroup:
    heading: str
    skills: list[Line] = field(default_factory=list)


@dataclass
class Section:
    key: str
    heading: str
    items: list[Item] = field(default_factory=list)
    groups: list[SkillGroup] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.items and not any(g.skills for g in self.groups)


@dataclass
class Contact:
    kind: str                   # email | phone | linkedin | github | portfolio | <other>
    text: str
    url: str = ""


@dataclass
class Header:
    name: str = ""
    contact: list[Contact] = field(default_factory=list)   # email, phone
    links: list[Contact] = field(default_factory=list)     # LinkedIn | GitHub | Portfolio


@dataclass
class ResumeContent:
    base: str
    header: Header
    sections: list[Section] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    priority: list[str] = field(default_factory=list)     # most important first (edit plan)
    dropped: list[str] = field(default_factory=list)      # removed by the one-page guard

    def section(self, key: str) -> Section | None:
        return next((s for s in self.sections if s.key == key), None)

    def fact_ids(self) -> list[str]:
        out: list[str] = []
        for s in self.sections:
            for it in s.items:
                out.append(it.fact_id)
                out.extend(b.fact_id for b in it.bullets)
            for g in s.groups:
                out.extend(x.fact_id for x in g.skills)
        return out

    def lines(self) -> list[dict[str, Any]]:
        """Every printed line with the fact ids it comes from (grounding v3 input)."""
        out: list[dict[str, Any]] = []
        for s in self.sections:
            for it in s.items:
                out.append({"section": s.key, "kind": it.kind, "text": it.text,
                            "fact_ids": [it.fact_id], "rephrased": False})
                for b in it.bullets:
                    out.append({"section": s.key, "kind": "bullet", "text": b.text,
                                "fact_ids": [b.fact_id], "rephrased": b.rephrased,
                                "original": b.original or b.text})
            for g in s.groups:
                if g.skills:
                    out.append({"section": s.key, "kind": "skills", "heading": g.heading,
                                "text": ", ".join(x.text for x in g.skills),
                                "fact_ids": [x.fact_id for x in g.skills], "rephrased": False})
        return out

    def drop(self, fact_id: str) -> bool:
        """Remove an item, bullet or skill by id (one-page guard / exclude)."""
        hit = False
        for s in self.sections:
            n = len(s.items)
            s.items = [it for it in s.items if it.fact_id != fact_id]
            hit |= len(s.items) != n
            for it in s.items:
                n = len(it.bullets)
                it.bullets = [b for b in it.bullets if b.fact_id != fact_id]
                hit |= len(it.bullets) != n
            for g in s.groups:
                n = len(g.skills)
                g.skills = [x for x in g.skills if x.fact_id != fact_id]
                hit |= len(g.skills) != n
        return hit

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- inputs

def _profile_dir_for(facts: F.ResumeFacts) -> Path:
    return Path(facts.path).parent if facts.path else F.profile_dir()


def load_layouts(facts: F.ResumeFacts) -> dict[str, F.Base]:
    p = F.bases_path_for(facts.path) if facts.path else F.profile_dir() / "base_resumes" / "bases.yaml"
    if not Path(p).exists():
        raise ContentError(f"no base layout file ({Path(p).name}) next to the facts file")
    return F.load_bases(p)


def load_answers(facts: F.ResumeFacts) -> dict[str, Any]:
    p = _profile_dir_for(facts) / "answers.yaml"
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        log.warning("answers.yaml is not valid YAML; header falls back to resume_facts person")
        return {}
    return data if isinstance(data, dict) else {}


def _str(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, False) else ""


def build_header(person: F.Person, answers: dict[str, Any] | None) -> Header:
    personal = (answers or {}).get("personal") or {}
    if not isinstance(personal, dict):
        personal = {}
    first_last = " ".join(x for x in (_str(personal.get("first_name")), _str(personal.get("last_name"))) if x)
    name = _str(personal.get("full_name")) or _str(personal.get("name")) or first_last or _str(person.name)
    email = _str(personal.get("email")) or _str(person.email)
    phone = _str(personal.get("phone")) or _str(person.phone)
    links_a = personal.get("links") if isinstance(personal.get("links"), dict) else {}
    links: dict[str, str] = {}
    for k in [*LINK_LABELS, *links_a, *person.links]:
        if k in links:
            continue
        url = _str(links_a.get(k)) or _str(person.links.get(k))
        if url:
            links[k] = url
    h = Header(name=name)
    if email:
        h.contact.append(Contact("email", email, f"mailto:{email}"))
    if phone:
        h.contact.append(Contact("phone", phone, ""))
    for k, url in links.items():
        h.links.append(Contact(k, LINK_LABELS.get(k, k.replace("_", " ").title()), url))
    return h


# --------------------------------------------------------------------------- items

def _dates(start: str, end: str) -> str:
    return " – ".join(x for x in (start, end) if x)


def _item(f: F.Fact, layout: F.Base | None) -> Item:
    text = f.text
    if isinstance(f, F.Education):
        return Item(f.id, "education", text, title=f.institution, right=_dates(f.start, f.end),
                    detail=f.degree, detail_right=f.grade)
    if isinstance(f, F.Role):
        return Item(f.id, "role", text, title=f.company, link=f.link, right=f.location,
                    detail=f.title, detail_right=_dates(f.start, f.end))
    if isinstance(f, F.Project):
        return Item(f.id, "project", text, title=text, link=f.link, tech=F.ordered_tech(f, layout))
    if isinstance(f, F.Hackathon):
        title, sub = text, ""
        tail = f"({f.event})" if f.event else ""
        if tail and text.endswith(tail):
            title, sub = text[: -len(tail)].rstrip(), tail
        return Item(f.id, "hackathon", text, title=title, subtitle=sub, tech=F.ordered_tech(f, layout))
    if isinstance(f, F.Research):
        # "Lead — rest": bold lead; a patent's title (up to the first ". ") is italic.
        lead, emph, rest = "", "", text
        if " — " in text:
            lead, body = text.split(" — ", 1)
            cut = body.find(". ") if f.research_kind == "patent" else -1
            emph, rest = (body[:cut], body[cut:]) if cut > 0 else ("", body)
        return Item(f.id, "research", text, lead=lead, emph=emph, rest=rest)
    raise ContentError(f"{f.id}: not a section item")


def _line(f: F.Fact) -> Line:
    return Line(f.id, f.text)


# --------------------------------------------------------------------------- build

def _layout_for(base: str, layouts: dict[str, F.Base], facts_base: str | None) -> tuple[F.Base, str]:
    """(layout, facts base key). A dropped-in template may borrow another base's
    layout and wording via its `% facts_base:` header."""
    if base in layouts:
        return layouts[base], base
    if facts_base and facts_base in layouts:
        return layouts[facts_base], facts_base
    raise ContentError(f"base '{base}' has no layout in bases.yaml (and no `% facts_base:` header)")


def build_content(base: str, facts: F.ResumeFacts, *, edit_plan: dict | None = None,
                  answers: dict[str, Any] | None = None,
                  layouts: dict[str, F.Base] | None = None,
                  facts_base: str | None = None) -> ResumeContent:
    """Résumé content for `base` from usable facts + the base layout, with an
    optional edit plan applied (reorder / include / exclude / rephrase by id).

    Raises FactsNotConfirmed when the base has no usable fact (e.g. the real
    profile before the user has confirmed anything)."""
    if facts.load_errors:
        raise ContentError("resume_facts.yaml has load errors; run `python -m jobcopilot.facts check`")
    layouts = layouts if layouts is not None else load_layouts(facts)
    if facts_base is None:
        from jobcopilot.resume.render import template_meta
        facts_base = template_meta(base, _profile_dir_for(facts)).get("facts_base")
    layout, fbase = _layout_for(base, layouts, facts_base)

    usable = F.usable_facts(facts, base=fbase)
    if not usable:
        raise FactsNotConfirmed(
            f"No confirmed résumé facts for base '{base}' — confirm your facts first "
            f"(python -m jobcopilot.facts review), then tailor again.")
    by_id = {f.id: f for f in usable}
    answers = answers if answers is not None else load_answers(facts)
    content = ResumeContent(base=base, header=build_header(facts.person, answers))
    if not content.header.name:
        content.warnings.append("header: no name in answers.yaml personal or resume_facts person")

    for key in layout.sections:
        heading = layout.headings.get(key) or key.title()
        sec = Section(key, heading)
        if key == "skills":
            for cat in layout.skills_categories:
                g = SkillGroup(cat.heading, [_line(by_id[i]) for i in cat.skills if i in by_id])
                sec.groups.append(g)
        else:
            if key == "projects":
                pool = [by_id[i] for i in layout.projects if i in by_id]
            elif key == "hackathons":
                pool = [by_id[i] for i in layout.hackathons if i in by_id]
            else:
                cls = {"education": F.Education, "experience": F.Role, "research": F.Research}[key]
                pool = [f for f in usable if isinstance(f, cls)]
            for f in pool:
                it = _item(f, layout)
                it.bullets = [_line(b) for b in getattr(f, "bullets", [])]
                sec.items.append(it)
        content.sections.append(sec)

    if edit_plan:
        apply_edit_plan(content, edit_plan, facts=facts, base=fbase, layout=layout)
    content.sections = [s for s in content.sections if not s.empty]
    for s in content.sections:
        s.groups = [g for g in s.groups if g.skills]
    if not content.sections:
        raise FactsNotConfirmed(f"Nothing left to print for base '{base}' — confirm your facts first.")
    return content


# --------------------------------------------------------------------------- edit plan

PLAN_KEYS = ("order", "include", "exclude", "rephrase", "skills_order", "priority")


def _ids(v: Any, what: str, warnings: list[str]) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) for x in v):
        warnings.append(f"edit_plan.{what}: expected a list of fact ids — ignored")
        return []
    return list(dict.fromkeys(v))


def _reorder(seq: list, ids: list[str], key, where: str, warnings: list[str]) -> list:
    have = {key(x): x for x in seq}
    for i in ids:
        if i not in have:
            warnings.append(f"edit_plan.{where}: '{i}' is not in that list — ignored")
    first = [have[i] for i in ids if i in have]
    return first + [x for x in seq if key(x) not in ids]


def apply_edit_plan(content: ResumeContent, plan: dict, *, facts: F.ResumeFacts, base: str,
                    layout: F.Base) -> None:
    """Apply only id-based operations. Anything referring to a fact that isn't
    usable is ignored with a warning (never printed)."""
    if not isinstance(plan, dict):
        raise ContentError("edit_plan must be a mapping")
    w = content.warnings
    exclude = set(_ids(plan.get("exclude"), "exclude", w))

    # include: usable facts the base layout doesn't place (any base's confirmed facts)
    include = [i for i in _ids(plan.get("include"), "include", w) if i not in exclude]
    if include:
        present = set(content.fact_ids())
        any_usable = {f.id: f for f in F.usable_facts(facts)}
        for i in include:
            if i in present:
                continue
            f = any_usable.get(i)
            if f is None:
                w.append(f"edit_plan.include: '{i}' is not a usable (confirmed) fact — ignored")
                continue
            g = copy.copy(f)
            g.text = f.variants.get(base, f.text)
            if not _include(content, g, any_usable, base, layout, exclude):
                w.append(f"edit_plan.include: '{i}' has no place in this base — ignored")
            present = set(content.fact_ids())

    for i in exclude:
        if not content.drop(i):
            w.append(f"edit_plan.exclude: '{i}' is not in this résumé — ignored")

    rephrase = plan.get("rephrase") or {}
    if not isinstance(rephrase, dict):
        w.append("edit_plan.rephrase: expected {fact_id: text} — ignored")
        rephrase = {}
    bullets = {b.fact_id: b for s in content.sections for it in s.items for b in it.bullets}
    for fid, text in rephrase.items():
        b = bullets.get(fid)
        if b is None:
            w.append(f"edit_plan.rephrase: '{fid}' is not a bullet in this résumé — ignored "
                     f"(only bullets may be rephrased)")
            continue
        t = _str(text) if isinstance(text, str) else ""
        if not t:
            w.append(f"edit_plan.rephrase: '{fid}' has no text — ignored")
            continue
        if t != b.text:
            b.original, b.text, b.rephrased = b.text, t, True

    order = plan.get("order") or {}
    if not isinstance(order, dict):
        w.append("edit_plan.order: expected {section or item id: [fact ids]} — ignored")
        order = {}
    items = {it.fact_id: it for s in content.sections for it in s.items}
    for k, ids in order.items():
        ids = _ids(ids, f"order.{k}", w)
        sec = content.section(k)
        if sec is not None and k != "skills":
            sec.items = _reorder(sec.items, ids, lambda x: x.fact_id, f"order.{k}", w)
        elif k in items:
            it = items[k]
            it.bullets = _reorder(it.bullets, ids, lambda x: x.fact_id, f"order.{k}", w)
        else:
            w.append(f"edit_plan.order: '{k}' is not a section or item of this résumé — ignored")

    so = plan.get("skills_order") or {}
    sk = content.section("skills")
    if not isinstance(so, dict):
        w.append("edit_plan.skills_order: expected {heading: [skill ids]} — ignored")
    elif sk is not None:
        headings = {g.heading for g in sk.groups}
        for k, ids in so.items():
            if k == "categories" and k not in headings:
                if isinstance(ids, list) and all(isinstance(x, str) for x in ids):
                    sk.groups = _reorder(sk.groups, list(dict.fromkeys(ids)), lambda g: g.heading,
                                         "skills_order.categories", w)
                else:
                    w.append("edit_plan.skills_order.categories: expected a list of headings — ignored")
                continue
            g = next((g for g in sk.groups if g.heading == k), None)
            if g is None:
                w.append(f"edit_plan.skills_order: no skills heading '{k}' — ignored")
                continue
            g.skills = _reorder(g.skills, _ids(ids, f"skills_order.{k}", w), lambda x: x.fact_id,
                                f"skills_order.{k}", w)

    pr = plan.get("priority")
    if isinstance(pr, dict):
        try:
            content.priority = [k for k, _ in sorted(pr.items(), key=lambda kv: -float(kv[1]))]
        except (TypeError, ValueError):
            w.append("edit_plan.priority: scores must be numbers — ignored")
    elif pr is not None:
        content.priority = _ids(pr, "priority", w)


def _include(content: ResumeContent, f: F.Fact, any_usable: dict[str, F.Fact], base: str,
             layout: F.Base, exclude: set[str]) -> bool:
    if f.kind == "bullet":
        for s in content.sections:
            for it in s.items:
                if it.fact_id == f.parent:
                    it.bullets.append(_line(f))
                    return True
        return False
    if isinstance(f, F.Skill):
        sk = content.section("skills")
        if sk is None or not sk.groups:
            return False
        cat_of = {x.id: x.category for x in any_usable.values() if isinstance(x, F.Skill)}
        best = max(sk.groups, key=lambda g: sum(cat_of.get(x.fact_id) == f.category for x in g.skills))
        best.skills.append(_line(f))
        return True
    key = {F.Education: "education", F.Role: "experience", F.Project: "projects",
           F.Research: "research", F.Hackathon: "hackathons"}.get(type(f))
    sec = content.section(key) if key else None
    if sec is None:
        return False
    it = _item(f, layout)
    it.bullets = [Line(b.id, b.variants.get(base, b.text)) for b in getattr(f, "bullets", [])
                  if b.id not in exclude]
    sec.items.append(it)
    return True
