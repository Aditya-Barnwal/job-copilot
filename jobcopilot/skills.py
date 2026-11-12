"""Skill taxonomy + JD skill extraction (Phase 2, fit score).

One canonical name per skill ("Spring Boot") with its aliases ("springboot",
"spring"). Matching is whole-term: `java` never matches inside `javascript`,
`C` never matches `C++` / `C#` / `Objective-C` / `Series C`, `Node` never
matches inside `nodes`. Ambiguous English words are matched case-sensitively
and never at the start of a sentence (`Go`, `Swift`, `Express`, `Spring`,
`Rails`), so "Go above and beyond" or "Spring 2026 internship" don't count.

Alias prefixes in the table below:
    (none)   case-insensitive whole term
    cs:      case-sensitive whole term
    cs!:     case-sensitive whole term, not at the start of a sentence / bullet
    re:      a raw regex (case-sensitive unless it says otherwise)

`extract(text)` splits a job description into must-have vs nice-to-have skills
using section cues (Requirements / Qualifications / Must vs Preferred / Nice to
have / Bonus / Plus) and line-level cues ("... is a plus").

Deterministic, pure, no I/O, no LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Optional

# --------------------------------------------------------------------- taxonomy
# Curated, not exhaustive: biased to SDE / backend / full-stack / ML / AI / data.
SKILLS: dict[str, list[str]] = {
    # languages
    "Python": ["python", "python3"],
    "Java": ["java", "j2ee", "java ee", "jvm", "core java"],
    "JavaScript": ["javascript", "ecmascript", "es6", "cs:JS"],
    "TypeScript": ["typescript", "cs:TS"],
    "Go": ["golang", r"re:(?<![A-Za-z0-9_.#+/-])Go(?![A-Za-z0-9_#+-])(?!\s+(?:to|ahead|above|beyond|live|deep|out|back|through|after|get|far|for it|the extra)\b)"],
    "C": [r"re:(?<![A-Za-z0-9_.#+/-])(?<!Series )(?<!Type )(?<!Grade )C(?![A-Za-z0-9_#+-])(?!\s*(?:level|suite|corp)\b)", "c programming", "c language"],
    "C++": ["c++", "cpp", "c plus plus"],
    "C#": ["c#", "csharp", "c sharp"],
    "Kotlin": ["kotlin"],
    "Scala": ["scala"],
    "Rust": ["rust", "rustlang"],
    "Ruby": ["ruby"],
    "PHP": ["php"],
    "Swift": ["cs!:Swift", "swiftui"],
    "Objective-C": ["objective-c", "objective c", "objc"],
    "Dart": ["dart"],
    "R": ["r programming", "r language", "rstats", "cs:RStudio"],
    "SQL": ["sql", "t-sql", "pl/sql", "plsql"],
    "Bash": ["bash", "shell scripting", "shell script", "shell scripts"],
    "Elixir": ["elixir"],
    "Haskell": ["haskell"],
    "Solidity": ["solidity"],
    # backend frameworks
    "Spring Boot": ["spring boot", "springboot", "spring framework", "spring mvc", "spring cloud",
                    r"re:(?<![A-Za-z0-9_.#+-])Spring(?![A-Za-z0-9_#+-])(?!\s+(?:20\d\d|semester|term|intern|break|cohort|season)\b)(?!\s*['’]\d\d)"],
    "Hibernate": ["hibernate", "jpa"],
    "Django": ["django"],
    "Flask": ["flask"],
    "FastAPI": ["fastapi", "fast api"],
    "Node.js": ["node.js", "nodejs", "node js", "cs!:Node"],
    "Express": ["express.js", "expressjs", "cs!:Express"],
    "NestJS": ["nestjs", "nest.js"],
    ".NET": [".net", "dotnet", "asp.net", ".net core"],
    "Ruby on Rails": ["ruby on rails", "ror", "cs!:Rails"],
    "Laravel": ["laravel"],
    "Gin": ["cs:Gin", "gin-gonic"],
    "gRPC": ["grpc", "protobuf", "protocol buffers"],
    "GraphQL": ["graphql"],
    "REST APIs": ["rest api", "rest apis", "restful", "restful api", "restful apis", "restful services", "cs:REST"],
    "Microservices": ["microservices", "microservice", "micro-services", "micro services"],
    "Drools": ["drools", "drl", "rule engine", "rules engine", "business rules engine"],
    # frontend / mobile
    "React": ["cs:React", "react.js", "reactjs"],
    "React Native": ["react native"],
    "Next.js": ["next.js", "nextjs"],
    "Vue.js": ["vue", "vue.js", "vuejs", "nuxt"],
    "Angular": ["angular", "angularjs"],
    "Svelte": ["svelte", "sveltekit"],
    "Redux": ["redux"],
    "HTML/CSS": ["html", "html5", "css", "css3", "sass", "scss"],
    "Tailwind CSS": ["tailwind", "tailwindcss", "tailwind css"],
    "Flutter": ["flutter"],
    "Android": ["android"],
    "iOS": ["cs:iOS"],
    # databases / storage
    "PostgreSQL": ["postgresql", "postgres", "psql"],
    "MySQL": ["mysql", "mariadb"],
    "SQLite": ["sqlite"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"],
    "Cassandra": ["cassandra"],
    "DynamoDB": ["dynamodb"],
    "Elasticsearch": ["elasticsearch", "elastic search", "opensearch"],
    "NoSQL": ["nosql"],
    "Oracle DB": ["oracle db", "oracle database", "cs:Oracle"],
    "BigQuery": ["bigquery", "big query"],
    "Snowflake": ["snowflake"],
    "Redshift": ["redshift"],
    "ClickHouse": ["clickhouse"],
    "Vector databases": ["vector database", "vector databases", "vector db", "vector store",
                         "pinecone", "weaviate", "milvus", "qdrant", "pgvector", "faiss", "chromadb"],
    # data engineering
    "Kafka": ["kafka", "kafka streams"],
    "RabbitMQ": ["rabbitmq"],
    "Spark": ["cs!:Spark", "pyspark", "apache spark", "spark streaming"],
    "Hadoop": ["hadoop", "hdfs", "hive"],
    "Airflow": ["airflow"],
    "dbt": ["dbt"],
    "ETL": ["etl", "elt", "data pipeline", "data pipelines"],
    "Data warehousing": ["data warehouse", "data warehousing", "data modeling", "data modelling"],
    "Pandas": ["pandas"],
    "NumPy": ["numpy"],
    # ML / AI
    "Machine Learning": ["machine learning", "cs:ML"],
    "Deep Learning": ["deep learning", "neural network", "neural networks"],
    "NLP": ["nlp", "natural language processing"],
    "Computer Vision": ["computer vision", "image processing"],
    "OpenCV": ["opencv"],
    "PyTorch": ["pytorch"],
    "TensorFlow": ["tensorflow"],
    "Keras": ["keras"],
    "scikit-learn": ["scikit-learn", "sklearn", "scikit learn"],
    "XGBoost": ["xgboost", "lightgbm", "catboost"],
    "Hugging Face": ["hugging face", "huggingface", "transformers library"],
    "LLM": ["llm", "llms", "large language model", "large language models", "genai", "gen ai",
            "generative ai", "gpt", "openai api"],
    "RAG": ["rag", "retrieval augmented generation", "retrieval-augmented generation"],
    "LangChain": ["langchain", "langgraph", "llamaindex", "llama index"],
    "Prompt engineering": ["prompt engineering"],
    "AI agents": ["ai agents", "agentic", "llm agents", "multi-agent"],
    "Fine-tuning": ["fine-tuning", "fine tuning", "finetuning", "lora", "peft"],
    "Embeddings": ["embeddings", "embedding models", "sentence-transformers"],
    "MLOps": ["mlops", "mlflow", "kubeflow", "model serving", "model deployment"],
    "Statistics": ["statistics", "statistical modeling", "a/b testing", "hypothesis testing"],
    # cloud / devops
    "AWS": ["aws", "amazon web services", "ec2", "s3"],
    "AWS Lambda": ["aws lambda", "lambda functions", "cs:Lambda", "serverless"],
    "AWS ECS": ["aws ecs", "ecs", "fargate"],
    "GCP": ["gcp", "google cloud", "google cloud platform", "cloud run", "gke"],
    "Azure": ["azure", "microsoft azure"],
    "Docker": ["docker", "containerization", "containerisation"],
    "Kubernetes": ["kubernetes", "k8s", "helm charts", "cs:Helm", "eks"],
    "Terraform": ["terraform", "infrastructure as code", "iac", "cloudformation"],
    "CI/CD": ["ci/cd", "ci cd", "cicd", "continuous integration", "continuous delivery",
              "continuous deployment", "github actions", "gitlab ci", "jenkins", "circleci"],
    "Linux": ["linux", "unix"],
    "Git": ["git", "github", "gitlab", "bitbucket", "version control"],
    "Observability": ["prometheus", "grafana", "datadog", "observability", "opentelemetry", "splunk",
                      "new relic"],
    "Nginx": ["nginx"],
    # engineering practice / CS
    "Distributed systems": ["distributed systems", "distributed system", "distributed computing"],
    "System design": ["system design", "systems design", "scalable systems", "high-level design", "low-level design"],
    "Data structures & algorithms": ["data structures", "algorithms", "dsa", "problem solving skills"],
    "OOP": ["oop", "object-oriented", "object oriented", "design patterns", "solid principles"],
    "Testing": ["unit testing", "unit tests", "integration testing", "tdd", "test-driven", "junit",
                "pytest", "jest", "selenium", "cypress"],
    "Concurrency": ["concurrency", "multithreading", "multi-threading", "async programming", "asyncio"],
    "Event-driven architecture": ["event-driven", "event driven", "pub/sub", "message queues", "message queue"],
    "Caching": ["caching", "memcached"],
    "Security": ["oauth", "oauth2", "jwt", "authentication", "owasp"],
    "WebSockets": ["websocket", "websockets", "socket.io"],
    "Agile": ["agile", "scrum", "kanban"],
}

# Having the left skill implies the right ones (profile side only: someone who
# ships Spring Boot writes Java). Kept small and uncontroversial.
IMPLIES: dict[str, list[str]] = {
    "Spring Boot": ["Java"], "Hibernate": ["Java"], "Drools": ["Java"],
    "Django": ["Python"], "Flask": ["Python"], "FastAPI": ["Python", "REST APIs"],
    "Pandas": ["Python"], "NumPy": ["Python"], "PyTorch": ["Python", "Deep Learning", "Machine Learning"],
    "TensorFlow": ["Python", "Deep Learning", "Machine Learning"], "Keras": ["Deep Learning"],
    "scikit-learn": ["Machine Learning", "Python"], "XGBoost": ["Machine Learning"],
    "Deep Learning": ["Machine Learning"], "NLP": ["Machine Learning"],
    "Computer Vision": ["Machine Learning"], "OpenCV": ["Computer Vision"],
    "RAG": ["LLM"], "LangChain": ["LLM"], "Fine-tuning": ["Machine Learning"],
    "Express": ["Node.js"], "NestJS": ["Node.js", "TypeScript"], "Node.js": ["JavaScript"],
    "Next.js": ["React"], "React": ["JavaScript"], "React Native": ["React"], "Redux": ["React"],
    "TypeScript": ["JavaScript"], "Vue.js": ["JavaScript"], "Angular": ["TypeScript"],
    "PostgreSQL": ["SQL"], "MySQL": ["SQL"], "SQLite": ["SQL"], "BigQuery": ["SQL"],
    "Snowflake": ["SQL"], "Redshift": ["SQL"], "Oracle DB": ["SQL"],
    "MongoDB": ["NoSQL"], "DynamoDB": ["NoSQL", "AWS"], "Cassandra": ["NoSQL"],
    "AWS Lambda": ["AWS"], "AWS ECS": ["AWS", "Docker"], "Kubernetes": ["Docker"],
    "Gin": ["Go"], "Flutter": ["Dart"], "Ruby on Rails": ["Ruby"], "Laravel": ["PHP"],
    "Kafka": ["Event-driven architecture"], "RabbitMQ": ["Event-driven architecture"],
}

_LEFT = r"(?<![A-Za-z0-9_.#+-])"
_RIGHT = r"(?![A-Za-z0-9_#+])(?!\.[A-Za-z0-9])"
_SENTENCE_START_CHARS = set(".!?:;\n\r•*·–—-(\"'|>")


@dataclass(frozen=True)
class _Alias:
    regex: re.Pattern
    mid_only: bool  # skip matches at the start of a sentence / bullet


def _compile_alias(alias: str) -> _Alias:
    if alias.startswith("re:"):
        return _Alias(re.compile(alias[3:]), False)
    mid = alias.startswith("cs!:")
    if mid or alias.startswith("cs:"):
        term = alias.split(":", 1)[1]
        return _Alias(re.compile(_LEFT + re.escape(term) + _RIGHT), mid)
    body = re.escape(alias.lower()).replace(r"\ ", r"[\s-]+")
    return _Alias(re.compile(_LEFT + body + _RIGHT, re.I), False)


# Canonical names that are too ambiguous to match as text on their own: only
# their aliases count ("Testing" / "Security" are ordinary words in a JD).
_NO_CANON_MATCH = {"C", "Go", "R", "Swift", "Express", "Gin", "iOS", "React", "Spark",
                   "Testing", "Security", "AI agents", "Caching"}


@lru_cache(maxsize=1)
def _compiled() -> tuple[tuple[str, tuple[_Alias, ...]], ...]:
    out = []
    for canon, aliases in SKILLS.items():
        terms = list(aliases)
        plain = {a.split(":", 1)[-1].lower() for a in aliases if not a.startswith("re:")}
        if canon not in _NO_CANON_MATCH and canon.lower() not in plain:
            terms.append(canon)
        out.append((canon, tuple(_compile_alias(t) for t in terms)))
    return tuple(out)


@lru_cache(maxsize=1)
def _lookup() -> dict[str, str]:
    """lowercased canonical/plain alias -> canonical."""
    d: dict[str, str] = {}
    for canon, aliases in SKILLS.items():
        d[canon.lower()] = canon
        for a in aliases:
            if a.startswith("re:"):
                continue
            d[a.split(":", 1)[1].lower() if a.startswith(("cs:", "cs!:")) else a.lower()] = canon
    return d


def _at_sentence_start(text: str, pos: int) -> bool:
    j = pos - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    return j < 0 or text[j] in _SENTENCE_START_CHARS


def _matches(text: str) -> list[tuple[int, int, str]]:
    """(start, end, canonical) for every alias hit, with hits nested inside a
    longer hit of a different skill dropped ("React" inside "React Native")."""
    hits: list[tuple[int, int, str]] = []
    for canon, aliases in _compiled():
        for al in aliases:
            for m in al.regex.finditer(text):
                if m.end() == m.start():
                    continue
                if al.mid_only and _at_sentence_start(text, m.start()):
                    continue
                hits.append((m.start(), m.end(), canon))
    hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
    kept: list[tuple[int, int, str]] = []
    for s, e, c in hits:
        if any(ks <= s and e <= ke and kc != c for ks, ke, kc in kept):
            continue
        kept.append((s, e, c))
    kept.sort()
    return kept


def find_skills(text: Optional[str]) -> list[str]:
    """Canonical skills named in `text`, in order of first appearance."""
    if not text:
        return []
    return list(dict.fromkeys(c for _, _, c in _matches(text)))


def canonical(name: Optional[str]) -> Optional[str]:
    """Canonical name for a skill tag ("springboot" -> "Spring Boot"); None if unknown."""
    if not name:
        return None
    key = name.strip().lower()
    hit = _lookup().get(key)
    if hit:
        return hit
    found = find_skills(name)
    return found[0] if len(found) == 1 else None


def normalize(names: Iterable[str]) -> list[str]:
    """Canonicalise a list of tags/phrases (unknown ones dropped), order kept."""
    out: list[str] = []
    for n in names or []:
        c = canonical(n)
        if c:
            out.append(c)
        else:
            out.extend(find_skills(n))
    return list(dict.fromkeys(out))


def expand(skills: Iterable[str]) -> set[str]:
    """Close a skill set under IMPLIES."""
    have = set(skills)
    stack = list(have)
    while stack:
        for nxt in IMPLIES.get(stack.pop(), []):
            if nxt not in have:
                have.add(nxt)
                stack.append(nxt)
    return have


# ------------------------------------------------------------ must vs nice split
_IGNORE_HEAD = (r"benefits|perks|what we offer|what you(?:'ll| will)? get|compensation|why join|"
                r"why work|about (?:us|the company|the team|company)|who we are|our values|"
                r"equal (?:opportunity|employment)|eeo|diversity|life at")
_NICE_HEAD = (r"preferred|nice[- ]to[- ]haves?|good[- ]to[- ]haves?|bonus(?: points)?|pluses|plus points|"
              r"extra credit|even better|desired|desirable|would be (?:great|nice|a plus)|"
              r"added advantage|brownie points|optional|it'?s a plus|what sets you apart|"
              r"stand out|additional (?:skills|qualifications)")
_MUST_HEAD = (r"requirements?|required(?: skills)?|qualifications?|must[- ]haves?|minimum|basic|"
              r"what you(?:'ll| will)? (?:need|bring)|what we(?:'re| are) looking for|who you are|"
              r"you (?:have|bring|should have|will need)|(?:key |technical |core |required )?skills|"
              r"tech(?:nical)? stack|eligibility|about you|your profile|ideal candidate|competencies|"
              r"experience|expertise|you'?re a fit|mandatory")
_BODY_HEAD = (r"responsibilities|what you(?:'ll| will)? do|the role|about the role|your role|"
              r"day[- ]to[- ]day|job description|overview|role description|key responsibilities|"
              r"what you'?ll work on|the job|the opportunity|duties")

_HEAD_PARTS = {"ignore": _IGNORE_HEAD, "nice": _NICE_HEAD, "must": _MUST_HEAD, "body": _BODY_HEAD}
_HEAD_RES = {k: re.compile(r"^(?:[\W_]*)(?:\w+\s+){0,3}?(?:" + v + r")\b", re.I) for k, v in _HEAD_PARTS.items()}
_ALL_HEAD = "|".join(_HEAD_PARTS.values())
_INLINE_HEAD = re.compile(r"(?i)(?<=[\s.!?])((?:\w+\s+){0,2}(?:" + _ALL_HEAD + r"))\s*:")
_LINE_NICE = re.compile(
    r"(?i)\b(?:nice[- ]to[- ]have|good[- ]to[- ]have|(?:is|are|would be|will be)? ?an? (?:big |huge |strong )?plus|"
    r"bonus|preferred|preferably|desirable|ideally|added advantage|plus points|brownie|not required|"
    r"optional|even better)\b")
_BULLET = re.compile(r"\s*[•·▪●◦*]\s*|\s+[-–]\s+(?=[A-Z])")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])|;\s*")


@dataclass
class SkillHits:
    must: list[str] = field(default_factory=list)   # canonical, first-appearance order
    nice: list[str] = field(default_factory=list)
    sections_found: bool = False                     # an explicit must or nice section was seen

    @property
    def all(self) -> list[str]:
        return self.must + [s for s in self.nice if s not in self.must]


def _heading_kind(seg: str) -> tuple[Optional[str], str]:
    """(kind, rest) if `seg` is a heading (optionally with inline content after a colon)."""
    s = seg.strip().strip("#*_ ").strip()
    if not s:
        return None, seg
    head, colon, rest = s.partition(":")
    words = head.split()
    if not words or len(words) > 8:
        return None, seg
    is_heading = bool(colon) or (len(words) <= 6 and not re.search(r"[.!?]$", s)
                                 and not find_skills(head))
    if not is_heading:
        return None, seg
    for kind in ("ignore", "nice", "must", "body"):
        if _HEAD_RES[kind].search(head):
            # "Experience with Java" without a colon is a sentence, not a heading.
            if kind == "must" and not colon and re.match(r"(?i)\W*(experience|expertise)\b", head) and len(words) > 2:
                return None, seg
            return kind, rest if colon else ""
    return None, seg


def _segments(text: str) -> list[str]:
    text = _INLINE_HEAD.sub(lambda m: "\n" + m.group(1) + ":", text)
    out: list[str] = []
    for line in text.splitlines():
        for part in _BULLET.split(line):
            # Keep a heading's colon with the heading; split long prose into sentences.
            out.extend(p for p in _SENT.split(part) if p.strip())
    return out


def extract(text: Optional[str]) -> SkillHits:
    """Must-have vs nice-to-have skills in a job description.

    Sections: a heading switches the state (must / nice / body / ignore). A line
    with its own soft cue ("... is a plus", "preferred") is nice even inside a
    must section. Skills seen only in body text (responsibilities, intro) are
    must-haves when the JD has no explicit requirements section, else nice.
    Benefits / about-us sections are ignored. A skill that is a must anywhere is
    a must.
    """
    hits = SkillHits()
    if not text:
        return hits
    state = "body"
    must: dict[str, None] = {}
    nice: dict[str, None] = {}
    body: dict[str, None] = {}
    saw_must = False
    for seg in _segments(text):
        kind, rest = _heading_kind(seg)
        if kind:
            state = kind
            if kind in ("must", "nice"):
                hits.sections_found = True
                saw_must = saw_must or kind == "must"
            seg = rest
            if not seg.strip():
                continue
        if state == "ignore":
            continue
        found = find_skills(seg)
        if not found:
            continue
        soft = bool(_LINE_NICE.search(seg))
        target = nice if (state == "nice" or soft) else (must if state == "must" else body)
        for s in found:
            target.setdefault(s, None)
    body_to = nice if saw_must else must
    for s in body:
        body_to.setdefault(s, None)
    hits.must = list(must)
    hits.nice = [s for s in nice if s not in must]
    return hits
