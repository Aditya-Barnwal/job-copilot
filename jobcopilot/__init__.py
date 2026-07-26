"""jobcopilot — sanctioned, ban-safe job ingestion from official ATS feeds."""
from .models import Job, Compensation
from .filters import FilterConfig, apply_filters

__all__ = ["Job", "Compensation", "FilterConfig", "apply_filters"]
__version__ = "0.1.0"
