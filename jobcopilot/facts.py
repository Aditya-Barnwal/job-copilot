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

`text` is a non-empty span that occurs exactly once in the owner's text (and
`variants[base]`, defaulting to `text`, exactly once in the owner's variant for
that base). Claims on one fact must not overlap. A claim on a header may
instead list `tech:` items of its tech line; if the header's text also names
such an item, a text claim must cover it there too. `usable_facts()` cuts
every claim that isn't confirmed out of the text, every variant, the header's
structured fields and its tech line, so the owner stays usable without it.

`usable_facts()` fails closed: a fact whose open claim can't be cut exactly
(span missing or ambiguous, a cut across a header field, a required field
left empty, a pending tech item still in the text) is left out entirely, and
`validate()` reports why. Duplicate YAML keys make the file unloadable.

CLI
---
    python -m jobcopilot.facts check  [path]   counts by status; exit 1 on errors
    python -m jobcopilot.facts review [path]   numbered list of lines to confirm

`review` prints résumé text to the user's own terminal; nothing here logs it.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
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
    # True when the loader hit a type error in this fact's own fields (e.g. a
    # `claims:` that isn't a list of mappings): it is never usable.
    malformed: bool = field(default=False, repr=False, compare=False)

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

    def mark(self, fact: Fact, start: int) -> Fact:
        """Flag `fact` if reading its own fields added errors since `start`."""
        fact.malformed = len(self.errors) > start
        return fact

    def bullets(self, raw: dict, where: str, section: str, parent: str) -> list[Fact]:
        out = []
        for w, b in self.mapping_list(raw.get("bullets"), f"{where}.bullets"):
            start = len(self.errors)
            kw = self.common(b, w, section, "bullet")
            out.append(self.mark(Fact(parent=parent, **kw), start))
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

    education = []
    for w, e in r.mapping_list(data.get("education"), "education"):
        start = len(errors)
        education.append(r.mark(
            Education(**r.common(e, w, "education", "education"),
                      institution=_s(e.get("institution")), degree=_s(e.get("degree")),
                      start=_s(e.get("start")), end=_s(e.get("end")),
                      grade=_s(e.get("grade")), location=_s(e.get("location"))), start))
    experience = []
    for w, e in r.mapping_list(data.get("experience"), "experience"):
        start = len(errors)
        role = r.mark(Role(**r.common(e, w, "experience", "role"),
                           company=_s(e.get("company")), title=_s(e.get("title")),
                           location=_s(e.get("location")), start=_s(e.get("start")),
                           end=_s(e.get("end")), link=_s(e.get("link"))), start)
        role.bullets = r.bullets(e, w, "experience", role.id)
        experience.append(role)
    projects = []
    for w, e in r.mapping_list(data.get("projects"), "projects"):
        start = len(errors)
        proj = r.mark(Project(**r.common(e, w, "projects", "project"), name=_s(e.get("name")),
                              tech=r.strlist(e, "tech", w), link=_s(e.get("link"))), start)
        proj.bullets = r.bullets(e, w, "projects", proj.id)
        projects.append(proj)
    research = []
    for w, e in r.mapping_list(data.get("research"), "research"):
        start = len(errors)
        research.append(r.mark(Research(**r.common(e, w, "research", "research"),
                                        research_kind=_s(e.get("kind")).lower()), start))
    hackathons = []
    for w, e in r.mapping_list(data.get("hackathons"), "hackathons"):
        start = len(errors)
        hack = r.mark(Hackathon(**r.common(e, w, "hackathons", "hackathon"),
                                name=_s(e.get("name")), event=_s(e.get("event")),
                                tech=r.strlist(e, "tech", w)), start)
        hack.bullets = r.bullets(e, w, "hackathons", hack.id)
        hackathons.append(hack)

    skills: dict[str, list[Skill]] = {}
    raw_skills = data.get("skills") or {}
    if not isinstance(raw_skills, dict):
        errors.append("skills: must be a mapping of category -> list of skills")
        raw_skills = {}
    for cat, items in raw_skills.items():
        cat = _s(cat)
        skills[cat] = []
        for w, s in r.mapping_list(items, f"skills.{cat}"):
            start = len(errors)
            skills[cat].append(r.mark(Skill(**r.common(s, w, "skills", "skill"), category=cat),
                                      start))

    facts = ResumeFacts(schema_version=data.get("schema_version"), person=person,
                        education=education, experience=experience, projects=projects,
                        research=research, hackathons=hackathons, skills=skills,
                        path=path, load_errors=errors)
    log.debug("parsed %d facts from %s", sum(1 for _ in facts.facts()), path or "<data>")
    return facts


class _DuplicateKeyError(yaml.constructor.ConstructorError):
    def __init__(self, key: Any, mark: Any):
        super().__init__(None, None, "found duplicate key", mark)
        self.key = key


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys. Plain YAML keeps the last
    one silently, so a second `claims:` would drop a pending claim unnoticed."""

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        if isinstance(node, yaml.MappingNode):
            seen: set = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue            # `<<: *anchor` then an override is legitimate
                key = self.construct_object(key_node, deep=deep)
                try:
                    dup = key in seen
                except TypeError:       # unhashable; SafeLoader reports it itself
                    continue
                if dup:
                    raise _DuplicateKeyError(key, key_node.start_mark)
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _yaml_pos(e: yaml.YAMLError) -> str:
    mark = getattr(e, "problem_mark", None)
    return f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise FactsError(f"{path}: not found")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.load(fh, Loader=_StrictLoader)  # noqa: S506 — SafeLoader subclass
    except _DuplicateKeyError as e:
        # Name the key only when it looks like a schema key, never résumé text.
        k = e.key if isinstance(e.key, str) and re.fullmatch(r"[A-Za-z_][\w.-]{0,40}", e.key) else ""
        raise FactsError(f"{path}: duplicate key{f' `{k}`' if k else ''}{_yaml_pos(e)}; "
                         f"YAML would keep only the last one") from None
    except yaml.YAMLError as e:
        # The YAML error message quotes the offending line, which can be résumé
        # text — report the position only.
        raise FactsError(f"{path}: invalid YAML{_yaml_pos(e)}") from None


def load(path: Path | str | None = None) -> ResumeFacts:
    """Load resume_facts.yaml into typed objects. Raises FactsError only when the
    file is missing, unparseable or has a duplicate mapping key (ambiguous: there
    is no telling which value was meant); content problems are left for validate()."""
    p = Path(path) if path else default_facts_path()
    return parse(_read_yaml(p), path=p)


# --------------------------------------------------------------------------- confirm (UI)

_ID_LINE_RE = r"^(\s*)(-\s+)?id:\s*[\"']?{ident}[\"']?\s*(#.*)?$"
_STATUS_LINE_RE = re.compile(r"^(\s*)status:\s*[\"']?([A-Za-z_]+)[\"']?(\s*#.*)?$")


def _status_map(facts: ResumeFacts) -> dict[str, tuple[str, str]]:
    """{fact or claim id: (status, text)} for every fact and claim."""
    out: dict[str, tuple[str, str]] = {}
    for f in facts.facts():
        out[f.id] = (f.status, f.text)
        for c in f.claims:
            out[c.id] = (c.status, c.text)
    return out


def confirm(ident: str, path: Path | str | None = None) -> dict:
    """Mark one `unconfirmed` fact or claim as `confirmed` (an explicit user action
    in the app). Edits only that line's `status:` value, so every comment and the
    layout stay as they are; the result is re-parsed and must differ from the old
    file in that one status only, else nothing is written. Pending lines (claims
    that are not true yet) can't be confirmed here. Returns {id, status, changed}."""
    import tempfile
    p = Path(path) if path else default_facts_path()
    if not isinstance(ident, str) or not _ID_RE.match(ident):
        raise FactsError("not a fact id")
    before = load(p)
    old = _status_map(before)
    if ident not in old:
        raise FactsError(f"unknown fact id {ident}")
    status = old[ident][0]
    if status == "confirmed":
        return {"id": ident, "status": "confirmed", "changed": False}
    if status != "unconfirmed":
        raise FactsError(f"{ident} is {status}; only unconfirmed lines can be confirmed here")
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    rx = re.compile(_ID_LINE_RE.format(ident=re.escape(ident)))
    hits = [i for i, ln in enumerate(lines) if rx.match(ln.rstrip("\r\n"))]
    if len(hits) != 1:
        raise FactsError(f"{ident}: can't find its line in {p.name} safely; confirm it by hand")
    m = rx.match(lines[hits[0]].rstrip("\r\n"))
    indent = len(m.group(1)) + len(m.group(2) or "")
    target = None
    for k in range(hits[0] + 1, len(lines)):
        ln = lines[k].rstrip("\r\n")
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        ind = len(ln) - len(ln.lstrip(" "))
        if ind < indent:
            break
        sm = _STATUS_LINE_RE.match(ln)
        if ind == indent and sm:
            target = (k, sm)
            break
    if target is None:
        raise FactsError(f"{ident}: no status line found; confirm it by hand")
    k, sm = target
    eol = lines[k][len(lines[k].rstrip("\r\n")):]
    lines[k] = f"{sm.group(1)}status: confirmed{sm.group(3) or ''}{eol}"
    new_text = "".join(lines)
    try:
        after = parse(yaml.load(new_text, Loader=_StrictLoader), path=p)  # noqa: S506 — SafeLoader subclass
    except yaml.YAMLError:
        raise FactsError("the edit would break the file; nothing was changed") from None
    new = _status_map(after)
    want = dict(old)
    want[ident] = ("confirmed", old[ident][1])
    if new != want:
        raise FactsError(f"{p.name} has a layout this editor can't change safely; confirm it by hand")
    fd, tmp = tempfile.mkstemp(prefix=".facts-", suffix=".yaml", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        os.chmod(tmp, p.stat().st_mode & 0o777)
        os.replace(tmp, p)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise FactsError(f"could not write {p.name}: {type(exc).__name__}") from None
    log.info("confirmed fact %s", ident)
    return {"id": ident, "status": "confirmed", "changed": True}


def review_rows(facts: ResumeFacts) -> list[dict]:
    """Every fact (and its claims) for the app's Facts screen, document order."""
    rows = []
    for f in facts.facts():
        rows.append({
            "id": f.id, "section": f.section, "kind": f.kind, "parent": f.parent or None,
            "text": f.text, "status": f.status, "sources": list(f.sources),
            "variants": dict(f.variants), "tech": list(getattr(f, "tech", []) or []),
            "pending_until": f.pending_until or None, "pending_reason": f.pending_reason or None,
            "note": f.note or None,
            "claims": [{"id": c.id, "text": c.text, "tech": list(c.tech), "status": c.status,
                        "pending_until": c.pending_until or None,
                        "pending_reason": c.pending_reason or None} for c in f.claims],
        })
    return rows


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


def _has_term(haystack: str, term: str, *, ignore_case: bool = True) -> bool:
    """Whole-term match. Letters, digits, `+ # %` and a `.` inside a token are
    term characters, so 'C' matches neither 'CI/CD', 'C++' nor 'C#', 'NET' not
    'ASP.NET', and '50%' not '150%'; a sentence-final '.' still ends a term."""
    t = _norm(term)
    if not t:
        return False
    pat = (r"(?<![A-Za-z0-9+#%.])" + re.escape(t)
           + r"(?![A-Za-z0-9+#%])(?!\.[A-Za-z0-9])")
    return re.search(pat, _norm(haystack), re.IGNORECASE if ignore_case else 0) is not None


# Claim spans are matched on normalised whitespace but cut from the original
# text, so nothing outside a cut is rewritten.

Span = tuple[int, int]


def _find_all(text: str, span: str) -> list[Span]:
    """Every (start, end) where `span` occurs in `text`, overlapping ones too;
    any run of whitespace in `span` matches any run in `text`."""
    s = _norm(span)
    if not s:
        return []
    pat = r"\s+".join(re.escape(tok) for tok in s.split(" "))
    return [(m.start(), m.start() + len(m.group(1)))
            for m in re.finditer(f"(?=({pat}))", text)]


def _merge(spans: list[Span]) -> list[Span]:
    """Union of spans: overlapping or touching ones become one."""
    out: list[Span] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _seam(left: str, right: str) -> str:
    """Join the text either side of a cut, tidying only the characters at the
    seam: whitespace, a separator left dangling, an emptied '()'."""
    lt, rt = left.rstrip(), right.lstrip()
    spaced = lt != left or rt != right
    while lt.endswith("(") and rt.startswith(")"):
        nl, nr = lt[:-1], rt[1:]
        spaced = spaced or nl != nl.rstrip() or nr != nr.lstrip()
        lt, rt = nl.rstrip(), nr.lstrip()
    if not lt:
        return re.sub(r"^[,;:]+\s*", "", rt)
    if not rt:
        return re.sub(r"\s*[,;:]+$", "", lt)
    if rt[0] in ",.;:!?)]":
        return re.sub(r"\s*[,;:]+$", "", lt) + rt
    if lt[-1] in "([":
        return lt + rt
    return lt + (" " if spaced else "") + rt


def _cut_spans(text: str, spans: list[Span]) -> str:
    """Remove the union of `spans` from `text`; text away from the seams is
    returned exactly as it was."""
    merged = _merge(spans)
    if not merged:
        return text
    pieces, prev = [], 0
    for s, e in merged:
        pieces.append(text[prev:s])
        prev = e
    pieces.append(text[prev:])
    out = pieces[0]
    for p in pieces[1:]:
        out = _seam(out, p)
    return out


def _cut(text: str, span: str) -> str | None:
    """`text` without `span`, or None unless `span` occurs exactly once."""
    found = _find_all(text, span)
    return _cut_spans(text, found) if len(found) == 1 else None


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
    owner = f.id or label
    # Located spans per wording (None = `text`, else a variant base), for the
    # overlap check below.
    located: dict[str | None, list[tuple[Span, str]]] = {}

    def locate(cl: str, where: str | None, text: str, span: str, missing: str,
               what: str) -> None:
        found = _find_all(text, span)
        if not found:
            errors.append(f"{cl}: {missing}")
        elif len(found) > 1:
            errors.append(f"{cl}: claim occurs {len(found)} times in {what}; it must occur "
                          f"exactly once (include more context)")
        else:
            located.setdefault(where, []).append((found[0], cl))

    for i, c in enumerate(f.claims):
        cl = c.id or f"{label}.claims[{i}]"
        _check_id(cl, c.id, seen, errors, prefix=f.id)
        _check_status(cl, c, errors)
        if bool(c.text) == bool(c.tech):
            errors.append(f"{cl}: a claim needs exactly one of `text` or `tech`")
        if c.variants and not c.text:
            errors.append(f"{cl}: `variants` needs a `text` span (a tech claim can't have them)")
        for base, span in c.variants.items():
            if base not in f.variants:
                errors.append(f"{cl}: variant '{base}' but {owner} has no '{base}' variant")
            if not span:
                errors.append(f"{cl}: claim variant '{base}' is empty; give the exact span "
                              f"of the '{base}' wording")
        if c.text:
            locate(cl, None, f.text, c.text,
                   f"claim text is not an exact span of {owner}'s text", f"{owner}'s text")
            for base, vtext in f.variants.items():
                span = c.variants.get(base, c.text)
                if span:
                    locate(cl, base, vtext, span,
                           f"claim is not an exact span of the '{base}' variant "
                           f"(add claims.variants.{base})", f"the '{base}' variant")
        for t in c.tech:
            if tech is None:
                errors.append(f"{cl}: `tech` claim on a fact without a tech line")
                break
            if t not in tech:
                errors.append(f"{cl}: tech '{t}' is not in {owner}'s tech line")

    for where, items in located.items():
        what = f"{owner}'s text" if where is None else f"the '{where}' variant"
        reach: tuple[int, str] | None = None        # furthest end so far, and its claim
        for (s, e), cl in sorted(items):
            if reach and s < reach[0]:
                errors.append(f"{cl}: overlaps claim {reach[1]} in {what}; claims must not "
                              f"overlap (merge them into one)")
            if not reach or e > reach[0]:
                reach = (e, cl)


def _check_fact(f: Fact, known: tuple[str, ...], seen: dict[str, str],
                errors: list[str]) -> None:
    start = len(errors)
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

    for m in f.metrics:
        if not re.search(r"\d", m):
            errors.append(f"{label}: metric '{m}' has no number in it")
        elif not any(_has_term(t, m, ignore_case=False) for t in f.all_texts()):
            errors.append(f"{label}: metric '{m}' does not appear in its text")
    for tag in f.skills:
        if not _mentions(f, tag):
            errors.append(f"{label}: skill tag '{tag}' does not appear in its text")

    _check_claims(f, label, seen, errors)
    _check_header_fields(f, label, errors)
    if len(errors) == start:
        # Statically fine; now make sure the usable view can be built, so a
        # fact usable_facts() would leave out is never a silent omission.
        try:
            _build_usable(f, None)
        except _Unusable as e:
            errors.append(f"{label}: not usable for tailoring: {e}")


def _mentions(f: Fact, term: str) -> bool:
    """`term` appears as a whole term in the text, a variant or the tech line."""
    return any(_has_term(t, term) for t in [*f.all_texts(), *getattr(f, "tech", [])])


def _header_fields(f: Fact) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(required fields, fields that must appear in `text`) for a header fact."""
    if isinstance(f, Education):
        return ("institution", "degree"), ("institution", "degree", "start", "end", "grade",
                                           "location")
    if isinstance(f, Role):
        return ("company", "title", "start"), ("company", "title", "location", "start", "end")
    if isinstance(f, (Project, Hackathon)):
        return ("name",), ("name", "event")
    return (), ()


def _check_header_fields(f: Fact, label: str, errors: list[str]) -> None:
    """Structured fields are what templates render; they must say what `text`
    says, or confirming the text would not confirm what gets printed."""
    required, in_text = _header_fields(f)
    if isinstance(f, Research) and f.research_kind not in RESEARCH_KINDS:
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

class _Unusable(Exception):
    """An open claim can't be cut out exactly, so the fact must not be used.
    Messages name claim ids and field names only, never résumé text."""


def _locate_once(text: str, span: str, what: str) -> Span:
    found = _find_all(text, span)
    if len(found) != 1:
        raise _Unusable(f"{what} occurs {len(found)} times; it must occur exactly once")
    return found[0]


def _cut_header_fields(g: Fact, text: str, cuts: list[Span]) -> None:
    """Apply the cuts made to `text` to the structured fields templates print.
    A cut inside a field is applied to it; a field inside a cut is emptied; a
    cut across a field boundary, or into a field that `text` repeats, is
    ambiguous and raises. Every field must still appear in the cut `g.text`."""
    required, in_text = _header_fields(g)
    merged = _merge(cuts)
    for name in in_text:
        value = getattr(g, name, "")
        if not value:
            continue
        found = _find_all(text, value)
        if not found:
            raise _Unusable(f"`{name}` does not appear in its text")
        hit = [(s, e) for s, e in merged if any(s < b and a < e for a, b in found)]
        if hit:
            if len(found) > 1:
                raise _Unusable(f"a claim that isn't confirmed cuts into `{name}`, which "
                                f"occurs more than once in its text")
            a, b = found[0]
            if any(s <= a and b <= e for s, e in hit):
                value = ""
            elif all(a <= s and e <= b for s, e in hit):
                value = _norm(_cut_spans(text[a:b], [(s - a, e - a) for s, e in hit]))
            else:
                raise _Unusable(f"a claim that isn't confirmed cuts across `{name}`; make the "
                                f"claim lie inside the field or cover it whole")
            setattr(g, name, value)
        if value and not _find_all(g.text, value):
            raise _Unusable(f"`{name}` no longer appears in its text once claims are cut")
    for name in required:
        if not getattr(g, name, ""):
            raise _Unusable(f"`{name}` would be empty once claims that aren't confirmed are cut")


def _build_usable(f: Fact, base: str | None) -> Fact | None:
    """A copy of `f` with every claim that isn't confirmed cut out of its text,
    variants, structured fields and tech line; None if no text is left.
    Raises _Unusable when that can't be done exactly (fail closed)."""
    g = copy.deepcopy(dataclasses.replace(f, bullets=[]) if hasattr(f, "bullets") else f)
    open_claims = [c for c in g.claims if c.status != "confirmed"]
    text_cuts: list[Span] = []
    variant_cuts: dict[str, list[Span]] = {b: [] for b in g.variants}
    pending_tech: list[str] = []
    for c in open_claims:
        cid = c.id or "(no id)"
        if not c.text and not c.tech:
            raise _Unusable(f"claim {cid} has neither `text` nor `tech`")
        if any(not v for v in c.variants.values()) or (c.variants and not c.text):
            raise _Unusable(f"claim {cid} has a variant span that can't be cut")
        if c.text:
            text_cuts.append(_locate_once(g.text, c.text, f"claim {cid}"))
            for b, v in g.variants.items():
                variant_cuts[b].append(_locate_once(v, c.variants.get(b, c.text),
                                                    f"claim {cid} in the '{b}' variant"))
        for t in c.tech:
            if t not in getattr(g, "tech", ()):
                raise _Unusable(f"tech '{t}' of claim {cid} is not in the tech line")
            pending_tech.append(t)

    original = g.text
    g.text = _cut_spans(original, text_cuts)
    g.variants = {b: _cut_spans(v, variant_cuts[b]) for b, v in g.variants.items()}
    for c in open_claims:          # a seam must not have rebuilt a cut span
        if c.text and (_find_all(g.text, c.text) or any(
                _find_all(v, c.variants.get(b, c.text)) for b, v in g.variants.items())):
            raise _Unusable(f"claim {c.id or '(no id)'} still occurs once cut")
    _cut_header_fields(g, original, text_cuts)
    if pending_tech:
        g.tech = [t for t in g.tech if t not in pending_tech]
        for t in dict.fromkeys(pending_tech):
            if any(_has_term(x, t) for x in g.all_texts()):
                raise _Unusable(f"tech '{t}' isn't confirmed but its text still names it; "
                                f"add a text claim with the same status that covers it")
    g.claims = [c for c in g.claims if c.status == "confirmed"]

    if base:
        g.text = g.text_for(base)
        g.variants = {}
    if not g.text:
        return None
    g.metrics = [m for m in g.metrics
                 if any(_has_term(t, m, ignore_case=False) for t in g.all_texts())]
    g.skills = [t for t in g.skills if _mentions(g, t)]
    return g


def usable_facts(facts: ResumeFacts, base: str | None = None) -> list[Fact]:
    """Confirmed facts only — never pending or unconfirmed — as copies with every
    claim that isn't confirmed cut out of the text, variants, header fields and
    tech line.

    Fails closed: a fact is left out (and `validate()` says why) when it is
    malformed, its id is missing or shared, or an open claim can't be cut
    exactly. A bullet is usable only if its header is usable for the same
    `base` (an unconfirmed employer or title would otherwise be printed above
    it). With `base`, keeps only facts that base contains and uses that base's
    wording as `text`. Headers' `bullets` are filtered the same way. Render a
    tech line with `ordered_tech()`, which drops tech that isn't confirmed
    from a base's `tech_order`.
    """
    ids = Counter(f.id for f in facts.facts())
    out: list[Fact] = []
    usable: dict[str, Fact] = {}
    originals: dict[str, Fact] = {}
    for f in facts.facts():
        if not f.confirmed or f.malformed or not f.id or ids[f.id] > 1:
            continue
        if f.kind == "bullet" and f.parent not in usable:
            continue
        if base and base not in f.sources:
            continue
        try:
            g = _build_usable(f, base)
        except _Unusable as e:
            log.debug("fact %s left out: %s", f.id, e)
            continue
        if g is None:
            continue
        usable[g.id] = g
        originals[g.id] = f
        out.append(g)
    for g in out:
        if hasattr(g, "bullets"):
            g.bullets = [usable[b.id] for b in originals[g.id].bullets
                         if b.id in usable and usable[b.id].parent == g.id]
    return out


def ordered_tech(fact: Fact, base: Base | None = None) -> list[str]:
    """The tech line to print: `base.tech_order` for this fact, keeping only
    items still in `fact.tech` (a usable copy has unconfirmed items removed)."""
    tech = list(getattr(fact, "tech", []))
    order = base.tech_order.get(fact.id) if base else None
    if not order:
        return tech
    out = [t for t in order if t in tech]
    return out + [t for t in tech if t not in out]


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
