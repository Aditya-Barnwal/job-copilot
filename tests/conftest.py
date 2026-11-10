"""Shared test setup: every test gets its own throwaway data directory.

A test once wrote into the real data/jobcopilot.db. The autouse fixture below
makes that impossible by construction: before each test the DB path and the
résumé-PDF directory point at tmp_path (via both the env vars and the module
attributes), and the schema is created there.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set before any test module imports server.app: keeps the real .env (API keys,
# provider choice) out of the test process so nothing can reach a live LLM.
os.environ["JOBCOPILOT_NO_DOTENV"] = "1"

from jobcopilot import store, versions  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    db = data / "jobcopilot.db"
    resumes = data / "resumes"
    monkeypatch.setenv("JOBCOPILOT_DB", str(db))
    monkeypatch.setenv("JOBCOPILOT_RESUMES_DIR", str(resumes))
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(store, "RESUMES_DIR", resumes)
    # versions.py resolves the directory through store at call time; patch a
    # module copy too if one ever reappears, so this guard can't go stale.
    if hasattr(versions, "RESUMES_DIR"):
        monkeypatch.setattr(versions, "RESUMES_DIR", resumes)
    store.init_db()
    return data
