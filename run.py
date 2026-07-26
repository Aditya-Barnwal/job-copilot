#!/usr/bin/env python3
"""Convenience entrypoint so you can run `python run.py ...` from the repo root."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jobcopilot.ingest import main

if __name__ == "__main__":
    raise SystemExit(main())
