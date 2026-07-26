"""Connector registry.

The orchestrator looks a company's `connector` field up in REGISTRY to pick the
right class. Adding a new ATS later (Workable, Recruitee, Adzuna, ...) means
writing one connector and adding one line here.
"""
from .base import Connector
from .greenhouse import GreenhouseConnector
from .lever import LeverConnector
from .ashby import AshbyConnector
from .workday import WorkdayConnector
from .adzuna import AdzunaConnector
from .jsearch import JsearchConnector

REGISTRY: dict[str, type[Connector]] = {
    GreenhouseConnector.source: GreenhouseConnector,
    LeverConnector.source: LeverConnector,
    AshbyConnector.source: AshbyConnector,
    WorkdayConnector.source: WorkdayConnector,
    AdzunaConnector.source: AdzunaConnector,
    JsearchConnector.source: JsearchConnector,
}

__all__ = [
    "Connector", "GreenhouseConnector", "LeverConnector",
    "AshbyConnector", "WorkdayConnector",
    "AdzunaConnector", "JsearchConnector", "REGISTRY",
]
