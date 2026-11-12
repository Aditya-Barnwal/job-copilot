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
# More ATS types (Phase 1b): company-config connectors, like greenhouse/ashby.
from .recruitee import RecruiteeConnector
from .workable import WorkableConnector
from .rippling import RipplingConnector
from .teamtailor import TeamtailorConnector
from .pinpoint import PinpointConnector
from .personio import PersonioConnector
from .bamboohr import BambooHRConnector
from .keka import KekaConnector
# Remote job boards (Phase 1): fetch(board_cfg) takes config.sources()['boards'][name].
from .himalayas import HimalayasConnector
from .workingnomads import WorkingNomadsConnector
from .jobspresso import JobspressoConnector
from .remoteok import RemoteOKConnector
from .weworkremotely import WeWorkRemotelyConnector
from .jobicy import JobicyConnector
from .realworkfromanywhere import RealWorkFromAnywhereConnector
from .fourdayweek import FourDayWeekConnector
from .cryptojobslist import CryptoJobsListConnector
from .hasjob import HasjobConnector
from .remotecom import RemoteComConnector
from .arbeitnow import ArbeitnowConnector
from .nodesk import NoDeskConnector
from .hn_hiring import HNHiringConnector
from .speedyapply import SpeedyApplyConnector
from .simplify import SimplifyConnector

REGISTRY: dict[str, type[Connector]] = {
    GreenhouseConnector.source: GreenhouseConnector,
    LeverConnector.source: LeverConnector,
    AshbyConnector.source: AshbyConnector,
    WorkdayConnector.source: WorkdayConnector,
    AdzunaConnector.source: AdzunaConnector,
    JsearchConnector.source: JsearchConnector,
    SmartRecruitersConnector.source: SmartRecruitersConnector,
    GemConnector.source: GemConnector,
    RecruiteeConnector.source: RecruiteeConnector,
    WorkableConnector.source: WorkableConnector,
    RipplingConnector.source: RipplingConnector,
    TeamtailorConnector.source: TeamtailorConnector,
    PinpointConnector.source: PinpointConnector,
    PersonioConnector.source: PersonioConnector,
    BambooHRConnector.source: BambooHRConnector,
    KekaConnector.source: KekaConnector,
    HimalayasConnector.source: HimalayasConnector,
    WorkingNomadsConnector.source: WorkingNomadsConnector,
    JobspressoConnector.source: JobspressoConnector,
    RemoteOKConnector.source: RemoteOKConnector,
    WeWorkRemotelyConnector.source: WeWorkRemotelyConnector,
    JobicyConnector.source: JobicyConnector,
    RealWorkFromAnywhereConnector.source: RealWorkFromAnywhereConnector,
    FourDayWeekConnector.source: FourDayWeekConnector,
    CryptoJobsListConnector.source: CryptoJobsListConnector,
    HasjobConnector.source: HasjobConnector,
    RemoteComConnector.source: RemoteComConnector,
    ArbeitnowConnector.source: ArbeitnowConnector,
    NoDeskConnector.source: NoDeskConnector,
    HNHiringConnector.source: HNHiringConnector,
    SpeedyApplyConnector.source: SpeedyApplyConnector,
    SimplifyConnector.source: SimplifyConnector,
}

# Sources that are job boards (many employers) rather than one employer's ATS.
BOARD_SOURCES = frozenset({"himalayas", "workingnomads", "jobspresso", "remoteok", "weworkremotely",
    "jobicy", "realworkfromanywhere", "fourdayweek", "cryptojobslist", "hasjob", "remotecom", "arbeitnow", "nodesk",
    "hn_hiring", "speedyapply", "simplify"})

__all__ = [
    "Connector", "GreenhouseConnector", "LeverConnector",
    "AshbyConnector", "WorkdayConnector",
    "AdzunaConnector", "JsearchConnector", "REGISTRY", "BOARD_SOURCES", "SmartRecruitersConnector", "GemConnector", "RecruiteeConnector", "WorkableConnector", "RipplingConnector", "TeamtailorConnector", "PinpointConnector", "PersonioConnector", "BambooHRConnector", "KekaConnector",
    "HimalayasConnector", "WorkingNomadsConnector", "JobspressoConnector",
    "RemoteOKConnector", "WeWorkRemotelyConnector",
    "JobicyConnector", "RealWorkFromAnywhereConnector", "FourDayWeekConnector", "CryptoJobsListConnector", "HasjobConnector", "RemoteComConnector", "ArbeitnowConnector", "NoDeskConnector", "HNHiringConnector", "SpeedyApplyConnector", "SimplifyConnector",
]
