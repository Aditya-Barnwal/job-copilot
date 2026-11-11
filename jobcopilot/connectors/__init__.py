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
from .smartrecruiters import SmartRecruitersConnector
from .gem import GemConnector
# Remote job boards (Phase 1): fetch(board_cfg) takes config.sources()['boards'][name].
from .himalayas import HimalayasConnector
from .workingnomads import WorkingNomadsConnector
from .jobspresso import JobspressoConnector
from .remoteok import RemoteOKConnector
from .weworkremotely import WeWorkRemotelyConnector

REGISTRY: dict[str, type[Connector]] = {
    GreenhouseConnector.source: GreenhouseConnector,
    LeverConnector.source: LeverConnector,
    AshbyConnector.source: AshbyConnector,
    WorkdayConnector.source: WorkdayConnector,
    AdzunaConnector.source: AdzunaConnector,
    JsearchConnector.source: JsearchConnector,
    SmartRecruitersConnector.source: SmartRecruitersConnector,
    GemConnector.source: GemConnector,
    HimalayasConnector.source: HimalayasConnector,
    WorkingNomadsConnector.source: WorkingNomadsConnector,
    JobspressoConnector.source: JobspressoConnector,
    RemoteOKConnector.source: RemoteOKConnector,
    WeWorkRemotelyConnector.source: WeWorkRemotelyConnector,
}

# Sources that are job boards (many employers) rather than one employer's ATS.
BOARD_SOURCES = frozenset({"himalayas", "workingnomads", "jobspresso", "remoteok", "weworkremotely"})

__all__ = [
    "Connector", "GreenhouseConnector", "LeverConnector",
    "AshbyConnector", "WorkdayConnector",
    "AdzunaConnector", "JsearchConnector", "REGISTRY", "BOARD_SOURCES", "SmartRecruitersConnector", "GemConnector",
    "HimalayasConnector", "WorkingNomadsConnector", "JobspressoConnector",
    "RemoteOKConnector", "WeWorkRemotelyConnector",
]
