"""Load the git-ignored .env (LLM provider, API keys) for command-line entry points.

The web app loads .env in server/app.py. Command-line runs (the nightly
`python -m jobcopilot.daily` LaunchAgent above all) must load it the same way,
or jobcopilot.llm silently falls back to its default provider. That happened:
every nightly tailoring used a local model that wasn't running and stored
unchanged base copies. Same rules as the server: tests set JOBCOPILOT_NO_DOTENV,
the public demo (JOBCOPILOT_DEMO=1) never reads it, and variables already set in
the environment win over the file. Call this before anything imports jobcopilot.llm.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load() -> bool:
    """True if a .env file was read."""
    from . import demo                     # stdlib only, safe before anything else
    if os.environ.get("JOBCOPILOT_NO_DOTENV") or demo.enabled():
        return False
    path = ROOT / ".env"
    if not path.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except Exception:  # noqa: BLE001 - python-dotenv missing: nothing to load with
        return False
    return bool(load_dotenv(path, override=False))
