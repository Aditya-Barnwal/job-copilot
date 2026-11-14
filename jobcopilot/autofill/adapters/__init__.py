"""Per-ATS adapters. Every module in this package is imported by load_all();
each registers its Adapter subclass(es) with @register. A module that fails
to import is skipped (and logged) so one broken adapter can't take the
others down; the generic adapter is always the fallback."""
from __future__ import annotations

import importlib
import logging
import pkgutil

from .base import REGISTRY, Adapter, PlanContext, adapter_for, get, register

log = logging.getLogger("jobcopilot.autofill.adapters")
_loaded = False


def load_all() -> list[type[Adapter]]:
    global _loaded
    if not _loaded:
        _loaded = True
        for mod in pkgutil.iter_modules(__path__):
            if mod.name in ("base",) or mod.name.startswith("_"):
                continue
            try:
                importlib.import_module(f"{__name__}.{mod.name}")
            except Exception as exc:  # noqa: BLE001
                log.warning("adapter module %s failed to import: %s", mod.name, type(exc).__name__)
    return list(REGISTRY)


__all__ = ["Adapter", "PlanContext", "REGISTRY", "register", "adapter_for", "get", "load_all"]
