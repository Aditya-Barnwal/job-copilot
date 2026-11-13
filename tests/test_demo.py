"""Public demo mode (JOBCOPILOT_DEMO=1) + the Cloud Run packaging around it.

  * the demo app boots from fixtures, serves jobs / queue, keeps the banner on;
  * it never reads the real profile/ or .env, never resolves or connects to a
    non-local host, never calls an LLM, never imports torch;
  * every third-party module it loads is installed by requirements-demo.txt;
  * Dockerfile / .dockerignore / the staging step keep personal files out.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
from importlib import metadata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobcopilot import answers, demo, facts, llm, store

ROOT = Path(__file__).resolve().parent.parent
REAL_PROFILE = ROOT / "profile"


def _under_real_profile(p) -> bool:
    try:
        return Path(p).resolve().is_relative_to(REAL_PROFILE.resolve())
    except (TypeError, ValueError):
        return False


@pytest.fixture
def demo_client(monkeypatch):
    """The real app in demo mode; facts/answers loaders refuse the real profile/."""
    real_facts_load, real_answers_load = facts.load, answers.load

    def facts_load(path=None):
        target = path or facts.default_facts_path()
        assert not _under_real_profile(target), f"demo read the real profile: {target}"
        return real_facts_load(path)

    def answers_load(path=None):
        target = path or answers.default_answers_path()
        assert not _under_real_profile(target), f"demo read the real profile: {target}"
        return real_answers_load(path)

    monkeypatch.setattr(facts, "load", facts_load)
    monkeypatch.setattr(answers, "load", answers_load)
    monkeypatch.setenv("JOBCOPILOT_DEMO", "1")
    monkeypatch.setenv("GROQ_API_KEY", "should-be-hidden")
    from server.app import app
    with TestClient(app) as client:
        yield client


def test_demo_serves_fixture_jobs_and_queue(demo_client):
    c = demo_client
    home = c.get("/")
    assert home.status_code == 200 and demo.BANNER in home.text
    assert 'class="demo-banner"' in home.text

    jobs = c.get("/api/jobs").json()
    rows = jobs if isinstance(jobs, list) else jobs.get("jobs") or jobs.get("items") or []
    assert len(rows) > 10
    q = c.get("/api/queue").json()
    assert q["count"] > 0 and q["items"]
    key = q["items"][0]["key"]
    assert c.get(f"/api/jobs/{key}").status_code == 200
    assert c.get(f"/api/jobs/{key}/fit").status_code == 200
    assert c.get("/api/tracker").status_code == 200
    assert c.get("/api/answers/review").status_code == 200     # profile.example copy

    # the demo DB lives in a temp dir, never the configured one
    db = store.resolve_db_path()
    assert "jobcopilot-demo-" in str(db) and db.exists()
    assert os.environ.get("JOBCOPILOT_PROFILE_DIR", "").endswith("profile")
    assert not _under_real_profile(facts.default_facts_path())
    assert "GROQ_API_KEY" not in os.environ


def test_demo_allows_status_change_only_in_demo_db(demo_client, isolated_data_dir):
    c = demo_client
    key = c.get("/api/queue").json()["items"][0]["key"]
    r = c.post(f"/api/jobs/{key}/status", json={"status": "submitted"})
    assert r.status_code == 200, r.text
    assert store.get_job(key)["status"] == "submitted"
    # the test's own (non-demo) DB is untouched
    assert store.get_job(key, db_path=isolated_data_dir / "jobcopilot.db") is None


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/jobs/K/tailor"),
    ("POST", "/api/jobs/K/tailor/v2"),
    ("GET", "/api/jobs/K/tailor/v2/keywords"),
    ("GET", "/api/jobs/K/tailor-suggestion"),
    ("POST", "/api/jobs/K/cover-letter"),
    ("POST", "/api/jobs/K/email"),
    ("POST", "/api/jobs/K/draft-email"),
    ("POST", "/api/answers/preview"),
    ("POST", "/api/ingest"),
    ("POST", "/api/score"),
    ("POST", "/api/funding/run"),
    ("POST", "/api/runs/daily"),
    ("POST", "/api/resume"),
    ("POST", "/api/profile"),
    ("POST", "/api/answer-pack"),
    ("DELETE", "/api/answer-pack"),
    ("POST", "/api/jobs/K/approve"),
])
def test_demo_refuses_llm_and_write_endpoints(demo_client, method, path):
    r = demo_client.request(method, path)
    assert r.status_code == 403
    assert r.json()["detail"].startswith("demo: disabled")
    assert r.headers.get("Content-Security-Policy")       # security headers still applied


def test_demo_llm_and_network_are_off(demo_client):
    health = demo_client.get("/api/llm/health").json()
    assert health["provider"] == "demo" and "demo: disabled" in health["hint"]
    with pytest.raises(demo.DemoDisabled):
        llm.complete("s", "u")
    with pytest.raises(demo.DemoDisabled):
        llm.complete_json("s", "u")
    with pytest.raises(demo.NetworkBlocked):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(demo.NetworkBlocked):
        socket.create_connection(("93.184.216.34", 443), timeout=1)
    import requests
    with pytest.raises(requests.RequestException):
        requests.get("https://example.com/", timeout=2)
    from jobcopilot.http import PoliteClient
    with pytest.raises(Exception) as exc:
        PoliteClient(max_retries=0).get("https://boards-api.greenhouse.io/v1/boards/x/jobs")
    assert "demo: disabled" in repr(exc.value) or isinstance(exc.value, (OSError, requests.RequestException))


def test_demo_session_restores_everything(monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "x")
    before = (socket.getaddrinfo, socket.socket.connect, llm.complete, store.DEFAULT_DB_PATH,
              os.environ.get("JOBCOPILOT_DB"))
    with demo.session(build=False) as st:
        assert st.root.exists() and "SOME_API_KEY" not in os.environ
        assert socket.getaddrinfo is not before[0]
    assert (socket.getaddrinfo, socket.socket.connect, llm.complete, store.DEFAULT_DB_PATH,
            os.environ.get("JOBCOPILOT_DB")) == before
    assert os.environ["SOME_API_KEY"] == "x"
    assert not st.root.exists()
    assert "connect" not in vars(socket.socket)


def test_non_demo_app_has_no_banner():
    from server.app import app
    with TestClient(app) as c:
        assert demo.BANNER not in c.get("/").text


@pytest.mark.parametrize("host,local", [
    ("127.0.0.1", True), ("::1", True), ("localhost", True), ("0.0.0.0", True), ("::", True),
    (None, True), ("127.8.0.1", True), ("[::1]", True),
    ("example.com", False), ("10.0.0.1", False), ("169.254.169.254", False), ("8.8.8.8", False),
    ("metadata.google.internal", False),
])
def test_is_local_host(host, local):
    assert demo.is_local_host(host) is local


def test_refusal_policy():
    assert demo.refusal("GET", "/api/jobs") is None
    assert demo.refusal("GET", "/api/queue") is None
    assert demo.refusal("POST", "/api/jobs/abc/status") is None
    assert demo.refusal("POST", "/api/meta-kv") is None
    assert demo.refusal("POST", "/api/jobs/abc/notes")
    assert demo.refusal("PUT", "/api/anything")
    assert demo.refusal("GET", "/api/jobs/abc/tailor/v2/keywords")


# --------------------------------------------------------------------------- subprocess: the slim image

_PROBE = textwrap.dedent(r"""
    import importlib.abc, ipaddress, json, os, sys
    ROOT = sys.argv[1]
    sys.path.insert(0, ROOT)
    os.environ["JOBCOPILOT_DEMO"] = "1"
    os.environ.pop("JOBCOPILOT_NO_DOTENV", None)     # demo mode must skip .env on its own
    REAL_PROFILE = os.path.join(ROOT, "profile")
    REAL_ENV = os.path.join(ROOT, ".env")
    HEAVY = {"torch", "sentence_transformers", "transformers", "playwright", "openai", "numpy"}
    events, attempted = [], []

    def local(h):
        if h in (None, "", "localhost"):
            return True
        if isinstance(h, bytes):
            h = h.decode()
        try:
            ip = ipaddress.ip_address(str(h).split("%")[0])
        except ValueError:
            return False
        return ip.is_loopback or ip.is_unspecified

    def hook(ev, args):
        if ev == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            p = os.path.abspath(os.fsdecode(args[0]))
            if p == REAL_ENV or p == REAL_PROFILE or p.startswith(REAL_PROFILE + os.sep):
                events.append(["open", p])
        elif ev == "socket.getaddrinfo" and not local(args[0]):
            events.append(["getaddrinfo", str(args[0])])
        elif ev == "socket.connect":
            addr = args[1]
            host = addr[0] if isinstance(addr, tuple) else None
            if isinstance(addr, tuple) and not local(host):
                events.append(["connect", str(host)])
    sys.addaudithook(hook)

    class BlockHeavy(importlib.abc.MetaPathFinder):     # what the slim image does not have
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in HEAVY:
                attempted.append(name)
                raise ImportError(f"{name} is not in the demo image")
            return None
    sys.meta_path.insert(0, BlockHeavy())

    from fastapi.testclient import TestClient
    from server.app import app
    out = {}
    with TestClient(app) as c:
        q = c.get("/api/queue").json()
        key = q["items"][0]["key"]
        gets = ["/", "/api/meta", "/api/jobs", "/api/matches", "/api/excluded",
                "/api/tracker", "/api/runs", "/api/llm/health", "/api/sources/health",
                "/api/export.csv", "/api/answers/review", "/api/profile", "/api/answer-pack",
                "/api/resume-versions", f"/api/jobs/{key}", f"/api/jobs/{key}/fit",
                f"/api/jobs/{key}/history", f"/api/jobs/{key}/apply-pack",
                f"/api/jobs/{key}/resume-options", f"/api/jobs/{key}/artifacts"]
        out["status"] = {p: c.get(p).status_code for p in gets}
        out["status"]["POST status"] = c.post(f"/api/jobs/{key}/status", json={"status": "oa"}).status_code
        out["status"]["POST tailor"] = c.post(f"/api/jobs/{key}/tailor/v2", json={}).status_code
        out["queue_count"] = q["count"]
        out["banner"] = "Demo" in c.get("/").text
    site = [p for p in sys.path if p.endswith("site-packages")]
    top = sorted({n.split(".")[0] for n, m in list(sys.modules.items())
                  if getattr(m, "__file__", None) and any(m.__file__.startswith(s) for s in site)})
    out.update(events=events, attempted=attempted, third_party=top,
               heavy_loaded=sorted(HEAVY & {n.split(".")[0] for n in sys.modules}))
    print("PROBE" + json.dumps(out))
""")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _closure(reqs: list[str]) -> set[str]:
    """Distributions installed by `reqs` (names, extras honoured), transitively."""
    seen: set[str] = set()
    todo = list(reqs)
    while todo:
        spec = todo.pop()
        m = re.match(r"\s*([A-Za-z0-9_.\-]+)\s*(?:\[([^\]]*)\])?", spec)
        if not m:
            continue
        name, extras = _norm(m.group(1)), {e.strip() for e in (m.group(2) or "").split(",") if e.strip()}
        key = name + "[" + ",".join(sorted(extras)) + "]"
        if key in seen:
            continue
        seen.add(key)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for r in dist.requires or []:
            marker = r.split(";", 1)[1] if ";" in r else ""
            wanted = re.findall(r"extra\s*==\s*['\"]([^'\"]+)['\"]", marker)
            if wanted and not (set(wanted) & extras):
                continue
            todo.append(r.split(";", 1)[0])
    return {k.split("[", 1)[0] for k in seen}


def _req_names(path: Path) -> list[str]:
    return [ln.split("#", 1)[0].strip() for ln in path.read_text().splitlines()
            if ln.split("#", 1)[0].strip() and not ln.strip().startswith("-")]


def test_demo_subprocess_slim_image_offline_no_profile(tmp_path):
    """A clean interpreter in demo mode: no torch/openai/numpy, no profile/.env, no network."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("JOBCOPILOT_")}
    proc = subprocess.run([sys.executable, "-c", _PROBE, str(ROOT)], capture_output=True, text=True,
                          # cwd elsewhere: relative opens (rmtree of the temp demo profile
                          # uses dir_fd + 'profile') must not look like the repo's profile/
                          cwd=str(tmp_path), env=env, timeout=180)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE")), None)
    assert line, f"probe failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
    out = json.loads(line[len("PROBE"):])
    assert out["events"] == [], "demo touched the real profile/.env or the network"
    assert out["attempted"] == [] and out["heavy_loaded"] == []
    assert out["queue_count"] > 0 and out["banner"]
    bad = {p: s for p, s in out["status"].items() if p != "POST tailor" and s != 200}
    assert not bad, bad
    assert out["status"]["POST tailor"] == 403

    # every third-party module loaded is installed by requirements-demo.txt.
    # Exempt: httpx (+ rich/pygments it pulls via starlette.testclient) are the test
    # harness; sniffio is an optional try-import in anyio (absent -> None).
    allowed = _closure(_req_names(ROOT / "requirements-demo.txt") + ["httpx"])
    exempt = {"_distutils_hack", "rich", "pygments", "sniffio"}
    dists = metadata.packages_distributions()
    missing = {mod: dists.get(mod) for mod in out["third_party"]
               if mod not in exempt
               and not any(_norm(d) in allowed for d in dists.get(mod, []))}
    assert not missing, f"modules outside requirements-demo.txt: {missing}"


# --------------------------------------------------------------------------- packaging lint

def _dockerignore() -> list[str]:
    return [ln.strip() for ln in (ROOT / ".dockerignore").read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def test_dockerignore_keeps_personal_data_out():
    pats = _dockerignore()
    for needed in (".env", ".env.*", "profile/", "data/", "jobs.jsonl", "**/*.db", "**/*.sqlite",
                   "**/*.pdf", "**/*.docx", ".browser-profile/", ".git/", ".venv/"):
        assert needed in pats, f".dockerignore must exclude {needed}"
    assert "tests/*" in pats and "!tests/fixtures/" in pats
    negations = [p for p in pats if p.startswith("!")]
    assert set(negations) <= {"!.env.example", "!tests/fixtures/"}, negations


def test_dockerfile_shape():
    text = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^FROM python:3\.13-slim\b", text, re.M)
    assert re.search(r"^ARG REQS=requirements\.txt$", text, re.M)      # full build by default (compose)
    assert re.search(r"^ARG DEMO=0$", text, re.M)
    assert re.search(r"^USER (?!root)\w+", text, re.M)
    copies = re.findall(r"^COPY\s+(.+)$", text, re.M)
    for c in copies:
        src = c.split()[:-1]
        assert not any(s in (".", "./") or s.startswith(("profile/", "profile ", "data", ".env"))
                       or s == "profile" for s in src), c
    cmd = re.search(r"^CMD (.+)$", text, re.M).group(1)
    assert "${PORT" in cmd and "0.0.0.0" in cmd and "--workers 1" in cmd
    assert "--reload" not in cmd


def test_demo_requirements_are_slim_and_in_sync():
    demo_reqs = {_norm(re.split(r"[<>=!~\[ ;]", r)[0]): r for r in _req_names(ROOT / "requirements-demo.txt")}
    full = {_norm(re.split(r"[<>=!~\[ ;]", r)[0]): r for r in _req_names(ROOT / "requirements.txt")}
    for heavy in ("torch", "sentence-transformers", "transformers", "playwright"):
        assert heavy not in demo_reqs
    for name, spec in demo_reqs.items():
        assert full.get(name) == spec, f"{name}: keep requirements-demo.txt in step with requirements.txt"


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs git")
def test_deploy_stage_uploads_only_demo_files(tmp_path):
    dest = tmp_path / "src"
    proc = subprocess.run(["bash", str(ROOT / "scripts" / "deploy_cloudrun.sh"), "stage", str(dest)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    files = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    assert "requirements-demo.txt" in files and "jobcopilot/demo.py" in files
    assert any(f.startswith("tests/fixtures/") for f in files)
    assert any(f.startswith("profile.example/") for f in files)
    for f in files:
        assert not f.startswith(("profile/", "data/", ".git/", "docs/", "scripts/")), f
        assert not re.search(r"(^|/)\.env|\.(db|sqlite3?|pdf|docx?)$|^tests/[^/]+\.py$", f), f
    df = (dest / "Dockerfile").read_text()
    assert re.search(r"^ARG REQS=requirements-demo\.txt$", df, re.M)
    assert re.search(r"^ARG DEMO=1$", df, re.M)
    assert re.search(r"^ARG TORCH_INDEX=$", df, re.M)


def test_deploy_script_commands():
    text = (ROOT / "scripts" / "deploy_cloudrun.sh").read_text()
    assert '"$GCLOUD" services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com' in text
    for flag in ("--source", "--allow-unauthenticated", "--max-instances 1", "--min-instances 0",
                 "JOBCOPILOT_DEMO=1"):
        assert flag in text
    assert "GOOGLE_CLOUD_PROJECT" in text and "\"$GCLOUD\" run services delete" in text
    assert "profile/" not in [ln.split("#")[0] for ln in text.splitlines() if "ALLOW=" in ln][0]


def test_deploy_script_resolves_gcloud_without_path(tmp_path):
    """$GCLOUD, else PATH, else ~/google-cloud-sdk/bin/gcloud, else a clear install hint."""
    script = str(ROOT / "scripts" / "deploy_cloudrun.sh")
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    r = subprocess.run(["bash", script, "url", "p"], env=base, capture_output=True, text=True)
    assert r.returncode != 0 and "gcloud not found" in r.stderr and "GCLOUD=" in r.stderr
    fake = tmp_path / "google-cloud-sdk" / "bin" / "gcloud"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\necho \"fake-gcloud $*\" >&2\nexit 0\n")
    fake.chmod(0o755)
    r = subprocess.run(["bash", script, "url", "p"], env=base, capture_output=True, text=True)
    assert f"using {fake}" in r.stderr and "not logged in" in r.stderr
    other = tmp_path / "custom-gcloud"
    shutil.copy(fake, other)
    r = subprocess.run(["bash", script, "url", "p"], env={**base, "GCLOUD": str(other)},
                       capture_output=True, text=True)
    assert f"using {other}" in r.stderr
