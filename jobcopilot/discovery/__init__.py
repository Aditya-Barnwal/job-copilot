"""Company discovery importers (Phase 1): they feed the `companies` table only.

Jobs never come from these sources — they come from each company's own ATS API
(docs/DESIGN.md §4). Each importer is polite (one conditional GET via PoliteClient),
diff-based (source_state), and returns a counts dict; a failure is logged and
reported in the counts, never raised into the caller's run.
"""
from __future__ import annotations

from .bsm import run as run_bsm
from .detect import run as run_detect
from .yc import run as run_yc

__all__ = ["run_bsm", "run_detect", "run_yc"]
