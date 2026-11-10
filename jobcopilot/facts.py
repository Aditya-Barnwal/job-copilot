"""Résumé facts: the only material tailoring is allowed to use.

`profile/resume_facts.yaml` holds every line of the user's base résumés as an
addressable *fact* with a stable id. Tailoring (Phase 3, PLAN D4) selects,
reorders and rephrases facts by id instead of rewriting free text, so every
output line can cite the facts it came from and grounding becomes structural.
Nothing is trusted until the user confirms it: the file is pre-filled from the
PDFs with every line `unconfirmed`, and `usable_facts()` returns confirmed
facts only.

Schema (schema_version: 1)
--------------------------
    schema_version: 1
    person:      {name, email, phone, location, links: {linkedin, github, portfolio}}
    education:   [Education]   + institution, degree, start, end, grade, location
    experience:  [Role]        + company, title, location, start, end, link, bullets: [Fact]
    projects:    [Project]     + name, tech: [str], link, bullets: [Fact]
    research:    [Research]    + kind: patent | publication | research  (→ .research_kind)
    hackathons:  [Hackathon]   + name, event, tech: [str], bullets: [Fact]
    skills:      {category: [Skill]}   one fact per skill; layout per base is in bases.yaml

Every fact (each education row, role header, bullet, project header, research
item, hackathon header, skill) carries:

    id        stable dotted id: exp.cams, exp.cams.b1, proj.dume.b3, skill.java.
              A bullet's id starts with its header's id + ".".
    text      verbatim wording from the backend_sde PDF (or the only PDF that has
              it). Header facts join the PDF's header cells with " | " in
              reading order; their structured fields must each appear in `text`,
              so the two can't drift apart.
    variants  {base: text} when another base words the same line differently.
    sources   which base PDFs contain the line, e.g. [backend_sde, ml_ai].
    skills    optional skill tags; each must literally appear in the text, a
              variant or the tech line (no tagging what the line doesn't say).
    metrics   numbers as written ("500+", "94%", "2 hours"); each must appear in
              the text or a variant. Phase 3 locks these against rewording.
    status    unconfirmed | confirmed | pending
                unconfirmed  extracted from a PDF, not yet confirmed by the user
                confirmed    the user says it is true — the only usable status
                pending      known NOT to be true yet; becomes true when the
                             work in `pending_until` lands. Needs
                             `pending_until` and `pending_reason`.
    note      optional reviewer hint shown by `review` (never rendered).

Claims — partial truth inside one line
--------------------------------------
A line can be mostly true with one clause that isn't (yet). Instead of marking
the whole bullet pending and losing the rest, the clause becomes a `claim`:

    claims:
      - id: proj.job_copilot.b2.docker_ecs    # owner id + "."
        text: "; containerised with Docker and deployed on AWS ECS"
        variants: {ml_ai: "; deployed on AWS ECS"}
        status: pending
        pending_until: phase-3b
        pending_reason: "..."

`text` is an exact span of the owner's text (and `variants[base]`, defaulting to
`text`, a span of the owner's variant for that base). A claim on a header may
instead list `tech:` items of its tech line. `usable_facts()` cuts every claim
that isn't confirmed out of the line, so the owner stays usable without it.

CLI
---
    python -m jobcopilot.facts check  [path]   counts by status; exit 1 on errors
    python -m jobcopilot.facts review [path]   numbered list of lines to confirm

`review` prints résumé text to the user's own terminal; nothing here logs it.
"""
from __future__ import annotations

import argparse
import copy
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import yaml

log = logging.getLogger("jobcopilot.facts")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1
STATUSES = ("unconfirmed", "confirmed", "pending")
DEFAULT_BASES = ("backend_sde", "ml_ai")
SECTIONS = ("education", "skills", "experience", "projects", "research", "hackathons")
RESEARCH_KINDS = ("patent", "publication", "research")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")


class FactsError(Exception):
    """The file is missing or not parseable at all, so there is nothing to validate."""


def profile_dir() -> Path:
    """`JOBCOPILOT_PROFILE_DIR` overrides the git-ignored `profile/` (tests, demos)."""
    return Path(os.environ.get("JOBCOPILOT_PROFILE_DIR") or (ROOT / "profile"))


def default_facts_path() -> Path:
    return profile_dir() / "resume_facts.yaml"


def bases_path_for(facts_path: Path | str) -> Path:
    return Path(facts_path).parent / "base_resumes" / "bases.yaml"


# --------------------------------------------------------------------------- model

@dataclass(kw_only=True)
class Claim:
    """A clause inside a fact that has its own truth status (see module doc)."""
    id: str
    status: str
    text: str = ""
    tech: list[str] = field(default_factory=list)
    variants: dict[str, str] = field(default_factory=dict)
    pending_until: str = ""
    pending_reason: str = ""
    note: str = ""


@dataclass(kw_only=True)
class Fact:
    id: str
    text: str
    status: str
    sources: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    variants: dict[str, str] = field(default_factory=dict)
    claims: list[Claim] = field(default_factory=list)
    pending_until: str = ""
    pending_reason: str = ""
    note: str = ""
    # Set by the loader, not read from YAML.
    section: str = ""
    kind: str = ""
    parent: str = ""
    where: str = field(default="", repr=False, compare=False)

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"

    def text_for(self, base: str | None = None) -> str:
        """The wording a given base uses for this line."""
        return self.variants.get(base, self.text) if base else self.text

    def all_texts(self) -> list[str]:
        return [self.text, *self.variants.values()]


@dataclass(kw_only=True)
class Education(Fact):
    institution: str = ""
    degree: str = ""
    start: str = ""
    end: str = ""
    grade: str = ""
    location: str = ""


@dataclass(kw_only=True)
class Role(Fact):
    company: str = ""
    title: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    link: str = ""
    bullets: list[Fact] = field(default_factory=list)


@dataclass(kw_only=True)
class Project(Fact):
    name: str = ""
    tech: list[str] = field(default_factory=list)
    link: str = ""
    bullets: list[Fact] = field(default_factory=list)


@dataclass(kw_only=True)
class Research(Fact):
    research_kind: str = ""


@dataclass(kw_only=True)
class Hackathon(Fact):
    name: str = ""
    event: str = ""
    tech: list[str] = field(default_factory=list)
    bullets: list[Fact] = field(default_factory=list)


@dataclass(kw_only=True)
class Skill(Fact):
    category: str = ""


@dataclass
class Person:
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: dict[str, str] = field(default_factory=dict)


@dataclass
class ResumeFacts:
    schema_version: Any
    person: Person
    education: list[Education] = field(default_factory=list)
    experience: list[Role] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    research: list[Research] = field(default_factory=list)
    hackathons: list[Hackathon] = field(default_factory=list)
    skills: dict[str, list[Skill]] = field(default_factory=dict)
    path: Path | None = None
    load_errors: list[str] = field(default_factory=list)

    def facts(self) -> Iterator[Fact]:
        """Every fact in document order; a header is followed by its bullets."""
        yield from self.education
        for group in (self.experience, self.projects):
            for header in group:
                yield header
                yield from header.bullets
        yield from self.research
        for hack in self.hackathons:
            yield hack
            yield from hack.bullets
        for items in self.skills.values():
            yield from items

    def by_id(self) -> dict[str, Fact]:
        return {f.id: f for f in self.facts() if f.id}


@dataclass
class SkillCategory:
    heading: str
    skills: list[str] = field(default_factory=list)


@dataclass
class Base:
    """Layout of one base résumé — what a LaTeX template needs to reproduce it."""
    key: str
    pdf: str = ""
    roles: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    headings: dict[str, str] = field(default_factory=dict)
    skills_categories: list[SkillCategory] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    tech_order: dict[str, list[str]] = field(default_factory=dict)
    hackathons: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- loading

def _s(v: Any) -> str:
    """Scalar → str. YAML turns `2021` / `8.65` into numbers; facts are text."""
    return "" if v is None else str(v).strip()


class _Reader:
    """Lenient field access that records type problems instead of raising, so
    `validate()` can report every problem in one pass."""

    def __init__(self, errors: list[str]):
        self.errors = errors

    def strlist(self, raw: dict, key: str, where: str) -> list[str]:
        v = raw.get(key)
        if v is None:
            return []
        if not isinstance(v, list):
            self.errors.append(f"{where}: `{key}` must be a list")
            return []
        return [_s(x) for x in v]

    def strdict(self, raw: dict, key: str, where: str) -> dict[str, str]:
        v = raw.get(key)
        if v is None:
            return {}
        if not isinstance(v, dict):
            self.errors.append(f"{where}: `{key}` must be a mapping")
            return {}
        return {_s(k): _s(x) for k, x in v.items()}

    def mapping_list(self, raw: Any, where: str) -> list[tuple[str, dict]]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            self.errors.append(f"{where}: must be a list")
            return []
        out = []
        for i, item in enumerate(raw):
            w = f"{where}[{i}]"
            if isinstance(item, dict):
                out.append((w, item))
            else:
                self.errors.append(f"{w}: must be a mapping")
        return out

    def claims(self, raw: dict, where: str) -> list[Claim]:
        out = []
        for w, c in self.mapping_list(raw.get("claims"), f"{where}.claims"):
            out.append(Claim(
                id=_s(c.get("id")), status=_s(c.get("status")).lower(),
                text=_s(c.get("text")), tech=self.strlist(c, "tech", w),
                variants=self.strdict(c, "variants", w),
                pending_until=_s(c.get("pending_until")),
                pending_reason=_s(c.get("pending_reason")), note=_s(c.get("note")),
            ))
        return out

    def common(self, raw: dict, where: str, section: str, kind: str) -> dict[str, Any]:
        return dict(
            id=_s(raw.get("id")), text=_s(raw.get("text")),
            status=_s(raw.get("status")).lower(),
            sources=self.strlist(raw, "sources", where),
            skills=self.strlist(raw, "skills", where),
            metrics=self.strlist(raw, "metrics", where),
            variants=self.strdict(raw, "variants", where),
            claims=self.claims(raw, where),
            pending_until=_s(raw.get("pending_until")),
            pending_reason=_s(raw.get("pending_reason")),
            note=_s(raw.get("note")),
            section=section, kind=kind, where=where,
        )

    def bullets(self, raw: dict, where: str, section: str, parent: str) -> list[Fact]:
        out = []
        for w, b in self.mapping_list(raw.get("bullets"), f"{where}.bullets"):
            kw = self.common(b, w, section, "bullet")
            out.append(Fact(parent=parent, **kw))
        return out


def parse(data: Any, path: Path | None = None) -> ResumeFacts:
    """Build typed facts from already-parsed YAML (tests feed dicts directly)."""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise FactsError("top level must be a mapping")
    r = _Reader(errors)

    p = data.get("person") or {}
    if not isinstance(p, dict):
        errors.append("person: must be a mapping")
        p = {}
    person = Person(name=_s(p.get("name")), email=_s(p.get("email")),
                    phone=_s(p.get("phone")), location=_s(p.get("location")),
                    links=r.strdict(p, "links", "person"))

    education = [
        Education(**r.common(e, w, "education", "education"),
                  institution=_s(e.get("institution")), degree=_s(e.get("degree")),
                  start=_s(e.get("start")), end=_s(e.get("end")),
                  grade=_s(e.get("grade")), location=_s(e.get("location")))
        for w, e in r.mapping_list(data.get("education"), "education")
    ]
    experience = []
    for w, e in r.mapping_list(data.get("experience"), "experience"):
        role = Role(**r.common(e, w, "experience", "role"),
                    company=_s(e.get("company")), title=_s(e.get("title")),
                    location=_s(e.get("location")), start=_s(e.get("start")),
                    end=_s(e.get("end")), link=_s(e.get("link")))
        role.bullets = r.bullets(e, w, "experience", role.id)
        experience.append(role)
    projects = []
    for w, e in r.mapping_list(data.get("projects"), "projects"):
        proj = Project(**r.common(e, w, "projects", "project"), name=_s(e.get("name")),
                       tech=r.strlist(e, "tech", w), link=_s(e.get("link")))
        proj.bullets = r.bullets(e, w, "projects", proj.id)
        projects.append(proj)
    research = [
        Research(**r.common(e, w, "research", "research"),
                 research_kind=_s(e.get("kind")).lower())
        for w, e in r.mapping_list(data.get("research"), "research")
    ]
    hackathons = []
    for w, e in r.mapping_list(data.get("hackathons"), "hackathons"):
        hack = Hackathon(**r.common(e, w, "hackathons", "hackathon"), name=_s(e.get("name")),
                         event=_s(e.get("event")), tech=r.strlist(e, "tech", w))
        hack.bullets = r.bullets(e, w, "hackathons", hack.id)
        hackathons.append(hack)

    skills: dict[str, list[Skill]] = {}
    raw_skills = data.get("skills") or {}
    if not isinstance(raw_skills, dict):
        errors.append("skills: must be a mapping of category -> list of skills")
        raw_skills = {}
    for cat, items in raw_skills.items():
        cat = _s(cat)
        skills[cat] = [Skill(**r.common(s, w, "skills", "skill"), category=cat)
                       for w, s in r.mapping_list(items, f"skills.{cat}")]

    facts = ResumeFacts(schema_version=data.get("schema_version"), person=person,
                        education=education, experience=experience, projects=projects,
                        research=research, hackathons=hackathons, skills=skills,
                        path=path, load_errors=errors)
    log.debug("parsed %d facts from %s", sum(1 for _ in facts.facts()), path or "<data>")
    return facts


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise FactsError(f"{path}: not found")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as e:
        # The YAML error message quotes the offending line, which can be résumé
        # text — report the position only.
        mark = getattr(e, "problem_mark", None)
        pos = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise FactsError(f"{path}: invalid YAML{pos}") from None


def load(path: Path | str | None = None) -> ResumeFacts:
    """Load resume_facts.yaml into typed objects. Raises FactsError only when the
    file is missing or unparseable; content problems are left for validate()."""
    p = Path(path) if path else default_facts_path()
    return parse(_read_yaml(p), path=p)


def load_bases(path: Path | str) -> dict[str, Base]:
    """Load base_resumes/bases.yaml. A malformed layout file raises FactsError:
    it is small and hand-written, so a structural slip should stop the run."""
    p = Path(path)
    data = _read_yaml(p)
    if not isinstance(data, dict) or not isinstance(data.get("bases"), dict):
        raise FactsError(f"{p}: expected a mapping with a `bases:` mapping")
    out: dict[str, Base] = {}
    for key, b in data["bases"].items():
        if not isinstance(b, dict):
            raise FactsError(f"{p}: bases.{key} must be a mapping")
        cats = []
        for c in b.get("skills_categories") or []:
            if not isinstance(c, dict):
                raise FactsError(f"{p}: bases.{key}.skills_categories entries must be mappings")
            cats.append(SkillCategory(heading=_s(c.get("heading")),
                                      skills=[_s(x) for x in c.get("skills") or []]))
        out[_s(key)] = Base(
            key=_s(key), pdf=_s(b.get("pdf")),
            roles=[_s(x) for x in b.get("roles") or []],
            sections=[_s(x) for x in b.get("sections") or []],
            headings={_s(k): _s(v) for k, v in (b.get("headings") or {}).items()},
            skills_categories=cats,
            projects=[_s(x) for x in b.get("projects") or []],
            tech_order={_s(k): [_s(x) for x in v or []]
                        for k, v in (b.get("tech_order") or {}).items()},
            hackathons=[_s(x) for x in b.get("hackathons") or []],
        )
    return out


def load_bases_for(facts_path: Path | str) -> dict[str, Base] | None:
    p = bases_path_for(facts_path)
    return load_bases(p) if p.exists() else None


# --------------------------------------------------------------------------- validation

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _has_term(haystack: str, term: str) -> bool:
    """Whole-term, case-insensitive match ('C' must not match 'CI/CD')."""
    pat = r"(?<![A-Za-z0-9])" + re.escape(_norm(term)) + r"(?![A-Za-z0-9])"
    return re.search(pat, _norm(haystack), re.IGNORECASE) is not None


def _check_status(label: str, obj: Fact | Claim, errors: list[str]) -> None:
    if obj.status not in STATUSES:
        errors.append(f"{label}: status '{obj.status or '(missing)'}' must be one of "
                      f"{', '.join(STATUSES)}")
    if obj.status == "pending":
        if not obj.pending_until:
            errors.append(f"{label}: pending needs `pending_until`")
        if not obj.pending_reason:
            errors.append(f"{label}: pending needs `pending_reason`")


def _check_id(label: str, ident: str, seen: dict[str, str], errors: list[str],
              prefix: str = "") -> None:
    if not ident:
        errors.append(f"{label}: `id` is required")
        return
    if not _ID_RE.match(ident):
        errors.append(f"{label}: id '{ident}' must be dotted lowercase, e.g. exp.acme.b1")
    if prefix and not ident.startswith(prefix + "."):
        errors.append(f"{label}: id must start with '{prefix}.'")
    if ident in seen:
        errors.append(f"{label}: duplicate id (also at {seen[ident]})")
    else:
        seen[ident] = label


def _check_claims(f: Fact, label: str, seen: dict[str, str], errors: list[str]) -> None:
    tech = getattr(f, "tech", None)
    for i, c in enumerate(f.claims):
        cl = c.id or f"{label}.claims[{i}]"
        _check_id(cl, c.id, seen, errors, prefix=f.id)
        _check_status(cl, c, errors)
        if bool(c.text) == bool(c.tech):
            errors.append(f"{cl}: a claim needs exactly one of `text` or `tech`")
        if c.text:
            if _norm(c.text) not in _norm(f.text):
                errors.append(f"{cl}: claim text is not an exact span of {f.id or label}'s text")
            for base in c.variants:
                if base not in f.variants:
                    errors.append(f"{cl}: variant '{base}' but {f.id or label} has no "
                                  f"'{base}' variant")
            for base, vtext in f.variants.items():
                span = c.variants.get(base, c.text)
                if _norm(span) not in _norm(vtext):
                    errors.append(f"{cl}: claim is not an exact span of the '{base}' variant "
                                  f"(add claims.variants.{base})")
        for t in c.tech:
            if tech is None:
                errors.append(f"{cl}: `tech` claim on a fact without a tech line")
                break
            if t not in tech:
                errors.append(f"{cl}: tech '{t}' is not in {f.id or label}'s tech line")


def _check_fact(f: Fact, known: tuple[str, ...], seen: dict[str, str],
                errors: list[str]) -> None:
    label = f.id or f.where
    _check_id(f.where if not f.id else f.id, f.id, seen, errors,
              prefix=f.parent if f.kind == "bullet" else "")
    if not f.text:
        errors.append(f"{label}: `text` is required")
    _check_status(label, f, errors)

    if not f.sources:
        errors.append(f"{label}: `sources` must name at least one base")
    for s in f.sources:
        if s not in known:
            errors.append(f"{label}: unknown source '{s}' (known: {', '.join(known)})")
    if len(set(f.sources)) != len(f.sources):
        errors.append(f"{label}: duplicate entries in `sources`")
    for base, vtext in f.variants.items():
        if base not in f.sources:
            errors.append(f"{label}: variant '{base}' is not one of its sources")
        if not vtext:
            errors.append(f"{label}: variant '{base}' is empty")

    texts = " \n ".join(f.all_texts())
    for m in f.metrics:
        if not re.search(r"\d", m):
            errors.append(f"{label}: metric '{m}' has no number in it")
        elif _norm(m) not in _norm(texts):
            errors.append(f"{label}: metric '{m}' does not appear in its text")
    tech_line = " , ".join(getattr(f, "tech", []))
    for tag in f.skills:
        if not _has_term(texts + " , " + tech_line, tag):
            errors.append(f"{label}: skill tag '{tag}' does not appear in its text")

    _check_claims(f, label, seen, errors)
    _check_header_fields(f, label, errors)


def _check_header_fields(f: Fact, label: str, errors: list[str]) -> None:
    """Structured fields are what templates render; they must say what `text`
    says, or confirming the text would not confirm what gets printed."""
    required: tuple[str, ...] = ()
    in_text: tuple[str, ...] = ()
    if isinstance(f, Education):
        required, in_text = ("institution", "degree"), ("institution", "degree", "start",
                                                         "end", "grade", "location")
    elif isinstance(f, Role):
        required, in_text = ("company", "title", "start"), ("company", "title", "location",
                                                            "start", "end")
    elif isinstance(f, (Project, Hackathon)):
        required, in_text = ("name",), ("name", "event")
    elif isinstance(f, Research):
        if f.research_kind not in RESEARCH_KINDS:
            errors.append(f"{label}: kind '{f.research_kind or '(missing)'}' must be one of "
                          f"{', '.join(RESEARCH_KINDS)}")
    for name in required:
        if not getattr(f, name, ""):
            errors.append(f"{label}: `{name}` is required")
    for name in in_text:
        v = getattr(f, name, "")
        if v and _norm(v) not in _norm(f.text):
            errors.append(f"{label}: `{name}` does not appear in its text")
    tech = getattr(f, "tech", None)
    if tech is not None:
        if any(not t for t in tech):
            errors.append(f"{label}: empty entry in `tech`")
        if len(set(tech)) != len(tech):
            errors.append(f"{label}: duplicate entries in `tech`")
    for b in getattr(f, "bullets", []):
        extra = set(b.sources) - set(f.sources)
        if extra:
            errors.append(f"{b.id or b.where}: source(s) {', '.join(sorted(extra))} not in "
                          f"its header's sources")


def validate(facts: ResumeFacts, bases: dict[str, Base] | None = None) -> list[str]:
    """Every problem as a human-readable line, naming fact ids — never résumé
    text, so the messages are safe in CI logs."""
    errors = list(facts.load_errors)
    known = tuple(bases) if bases else DEFAULT_BASES
    if facts.schema_version != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION} "
                      f"(got {facts.schema_version!r})")
    if not facts.person.name:
        errors.append("person.name is required")
    seen: dict[str, str] = {}
    for f in facts.facts():
        _check_fact(f, known, seen, errors)
    if bases:
        errors.extend(validate_bases(bases, facts))
    return errors


def validate_bases(bases: dict[str, Base], facts: ResumeFacts) -> list[str]:
    """Layout ↔ facts consistency: a base lists exactly the facts that claim it."""
    errors: list[str] = []
    by_id = facts.by_id()
    all_skills = [s for items in facts.skills.values() for s in items]

    def placed(base: str, ids: list[str], cls: type, what: str, pool: list[Fact]) -> None:
        for i in ids:
            f = by_id.get(i)
            if not isinstance(f, cls):
                errors.append(f"bases.{base}.{what}: '{i}' is not a known {cls.__name__} id")
            elif base not in f.sources:
                errors.append(f"bases.{base}.{what}: '{i}' does not list {base} in sources")
        counts = Counter(ids)
        for f in pool:
            if base in f.sources and counts[f.id] != 1:
                errors.append(f"bases.{base}.{what}: '{f.id}' must appear exactly once "
                              f"(found {counts[f.id]})")

    for key, b in bases.items():
        if not b.roles:
            errors.append(f"bases.{key}: `roles` must list the role families it suits")
        for s in b.sections:
            if s not in SECTIONS:
                errors.append(f"bases.{key}.sections: unknown section '{s}'")
        if len(set(b.sections)) != len(b.sections):
            errors.append(f"bases.{key}.sections: duplicate section")
        for s in b.headings:
            if s not in b.sections:
                errors.append(f"bases.{key}.headings: '{s}' is not in sections")
        for c in b.skills_categories:
            if not c.heading:
                errors.append(f"bases.{key}.skills_categories: heading is required")
        skill_ids = [i for c in b.skills_categories for i in c.skills]
        placed(key, skill_ids, Skill, "skills_categories", all_skills)
        placed(key, b.projects, Project, "projects", list(facts.projects))
        placed(key, b.hackathons, Hackathon, "hackathons", list(facts.hackathons))
        for pid, order in b.tech_order.items():
            proj = by_id.get(pid)
            if pid not in b.projects or not isinstance(proj, Project):
                errors.append(f"bases.{key}.tech_order: '{pid}' is not a project of this base")
            elif sorted(order) != sorted(proj.tech):
                errors.append(f"bases.{key}.tech_order.{pid}: must reorder the project's "
                              f"tech line exactly")
    return errors


# --------------------------------------------------------------------------- usable view

def _cut(text: str, span: str) -> str:
    """Remove `span` from `text` and tidy the punctuation it leaves behind."""
    text, span = _norm(text), _norm(span)
    i = text.find(span)
    if not span or i < 0:
        return text
    out = text[:i] + " " + text[i + len(span):]
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+([,.;:)])", r"\1", out)
    out = re.sub(r"\(\s+", "(", out)
    out = re.sub(r"[,;:]+([,.;)])", r"\1", out)
    return out.strip()


def _usable_copy(f: Fact, base: str | None) -> Fact | None:
    g = copy.deepcopy(f)
    dropped = [c for c in g.claims if c.status != "confirmed"]
    for c in dropped:
        if c.text:
            g.text = _cut(g.text, c.text)
            g.variants = {b: _cut(v, c.variants.get(b, c.text)) for b, v in g.variants.items()}
        if c.tech and hasattr(g, "tech"):
            g.tech = [t for t in g.tech if t not in c.tech]
    g.claims = [c for c in g.claims if c.status == "confirmed"]
    if base:
        g.text = g.text_for(base)
        g.variants = {}
    if not g.text:
        return None
    texts = " \n ".join(g.all_texts())
    tech_line = " , ".join(getattr(g, "tech", []))
    g.metrics = [m for m in g.metrics if _norm(m) in _norm(texts)]
    g.skills = [t for t in g.skills if _has_term(texts + " , " + tech_line, t)]
    return g


def usable_facts(facts: ResumeFacts, base: str | None = None) -> list[Fact]:
    """Confirmed facts only — never pending or unconfirmed — as copies with every
    non-confirmed claim cut out of the text and tech line.

    A bullet is usable only if its header is confirmed too (an unconfirmed
    employer or title would otherwise be printed above it). With `base`, keeps
    only facts that base contains and uses that base's wording as `text`.
    Headers' `bullets` are filtered the same way.
    """
    by_id = facts.by_id()
    out: list[Fact] = []
    usable: dict[str, Fact] = {}
    for f in facts.facts():
        if not f.confirmed:
            continue
        if f.parent and not (by_id.get(f.parent) and by_id[f.parent].confirmed):
            continue
        if base and base not in f.sources:
            continue
        g = _usable_copy(f, base)
        if g is None:
            continue
        usable[g.id] = g
        out.append(g)
    for g in out:
        if hasattr(g, "bullets"):
            g.bullets = [usable[b.id] for b in g.bullets if b.id in usable]
    return out


# --------------------------------------------------------------------------- CLI

def _claim_rows(facts: ResumeFacts) -> Iterator[tuple[Fact, Claim]]:
    for f in facts.facts():
        for c in f.claims:
            yield f, c


def _summary(facts: ResumeFacts) -> list[str]:
    all_facts = list(facts.facts())
    by_status = Counter(f.status for f in all_facts)
    claims = Counter(c.status for _, c in _claim_rows(facts))
    by_section = Counter(f.section for f in all_facts)

    def fmt(c: Counter, keys: tuple[str, ...]) -> str:
        return ", ".join(f"{k} {c[k]}" for k in keys if c[k]) or "none"

    other = tuple(sorted(set(by_status) - set(STATUSES)))
    return [
        f"{len(all_facts)} facts, {sum(claims.values())} claims (schema v{facts.schema_version})",
        f"  facts by status:   {fmt(by_status, STATUSES + other)}",
        f"  claims by status:  {fmt(claims, STATUSES + tuple(sorted(set(claims) - set(STATUSES))))}",
        f"  facts by section:  {fmt(by_section, SECTIONS)}",
        f"  usable for tailoring (confirmed): {len(usable_facts(facts))}",
    ]


def _cmd_check(path: Path, facts: ResumeFacts, bases: dict[str, Base] | None) -> int:
    errors = validate(facts, bases)
    print(f"{path}:")
    for line in _summary(facts):
        print(line)
    print(f"  layout: {'bases.yaml ' + ', '.join(bases) if bases else 'no bases.yaml (default bases)'}")
    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK")
    return 0


def _pending_tag(obj: Fact | Claim) -> str:
    if obj.status == "pending":
        return f"[pending until {obj.pending_until}: {obj.pending_reason}]"
    return f"[{obj.status}]"


def _cmd_review(path: Path, facts: ResumeFacts, bases: dict[str, Base] | None) -> int:
    n = 0
    for f in facts.facts():
        open_claims = [c for c in f.claims if c.status != "confirmed"]
        if f.confirmed and not open_claims:
            continue
        if not f.confirmed:
            n += 1
            print(f"{n:>3}. {f.id} — {f.text}")
            print(f"       {_pending_tag(f)}  sources: {', '.join(f.sources)}")
            for base, v in f.variants.items():
                print(f"       {base} wording: {v}")
            if getattr(f, "tech", None):
                print(f"       tech: {', '.join(f.tech)}")
            if f.note:
                print(f"       note: {f.note}")
        for c in open_claims:
            n += 1
            span = c.text or ", ".join(c.tech)
            print(f"{n:>3}. {c.id} — \"{span}\"  (part of {f.id})")
            print(f"       {_pending_tag(c)}")
            if c.note:
                print(f"       note: {c.note}")
    if n:
        print(f"\n{n} line(s) to confirm. For each true line set `status: confirmed` in "
              f"{path}; fix the wording first if it isn't exactly right. Then run "
              f"`python -m jobcopilot.facts check`.")
    else:
        print("Every fact is confirmed.")
    errors = validate(facts, bases)
    if errors:
        print(f"({len(errors)} validation error(s) — run `check` for details)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobcopilot.facts",
                                 description="Validate and review profile/resume_facts.yaml.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("check", "validate; print counts by status; exit 1 on errors"),
                        ("review", "list every line that still needs your confirmation")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("path", nargs="?", type=Path, default=None,
                        help="facts file (default: profile/resume_facts.yaml)")
        sp.add_argument("--bases", type=Path, default=None,
                        help="layout file (default: <facts dir>/base_resumes/bases.yaml)")
    args = ap.parse_args(argv)
    path: Path = args.path or default_facts_path()
    try:
        facts = load(path)
        bases = load_bases(args.bases) if args.bases else load_bases_for(path)
    except FactsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.cmd == "check":
        return _cmd_check(path, facts, bases)
    return _cmd_review(path, facts, bases)


if __name__ == "__main__":
    raise SystemExit(main())
