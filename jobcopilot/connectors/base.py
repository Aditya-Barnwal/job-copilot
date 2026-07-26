"""Base connector contract.

Every ATS connector splits its work into two halves on purpose:

  * fetch_raw(company) -> the untouched payload from the network
  * parse(raw, company) -> list[Job]

Keeping them separate means `parse` is a pure function of its input, so it can
be unit-tested against saved fixture JSON with no network at all. That is how
the test suite (and the --fixtures run mode) exercise the whole pipeline
offline.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import requests

from ..models import Job

# A plain, honest browser-ish UA. We hit public, sanctioned endpoints; this just
# avoids the occasional default-python-UA block.
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


class Connector(ABC):
    source: str = "base"

    def __init__(self, session: requests.Session | None = None, timeout: int = 20):
        self.session = session or requests.Session()
        self.timeout = timeout

    @abstractmethod
    def fetch_raw(self, company: dict[str, Any]) -> Any:
        """Return the raw payload (dict/list) from the source."""

    @abstractmethod
    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        """Turn a raw payload into normalized Job objects."""

    def fetch(self, company: dict[str, Any]) -> list[Job]:
        return self.parse(self.fetch_raw(company), company)
