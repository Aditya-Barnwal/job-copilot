"""Base connector contract.

Every ATS connector splits its work into two halves on purpose:

  * fetch_raw(company) -> the untouched payload from the network
  * parse(raw, company) -> list[Job]

Keeping them separate means `parse` is a pure function of its input, so it can
be unit-tested against saved fixture JSON with no network at all. That is how
the test suite (and the --fixtures run mode) exercise the whole pipeline
offline.

All network access goes through `self.client`, a shared `PoliteClient`
(jobcopilot/http.py): honest User-Agent, per-host rate limits, robots.txt,
Retry-After. Connectors never build their own requests.Session.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

import requests

from ..http import PoliteClient, default_client
from ..models import Job

# Content negotiation only. The User-Agent is set by PoliteClient from
# config/sources.yaml and can't be overridden here (v1 spoofed Chrome).
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


class Connector(ABC):
    source: str = "base"

    def __init__(
        self,
        client: Optional[PoliteClient] = None,
        timeout: Optional[float] = None,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        """`client` defaults to the process-wide default_client(), resolved lazily so
        parse-only use (tests, --fixtures) never reads config or opens a session.

        `session` is the v1 argument, still accepted (also positionally): it is
        wrapped in a PoliteClient so the politeness rules still apply.
        `timeout` overrides the configured per-request timeout.
        """
        if isinstance(client, requests.Session):
            client, session = None, client
        if client is None and session is not None:
            client = PoliteClient.from_config(session=session)
        self._client = client
        self.timeout = timeout

    @property
    def client(self) -> PoliteClient:
        if self._client is None:
            self._client = default_client()
        return self._client

    @abstractmethod
    def fetch_raw(self, company: dict[str, Any]) -> Any:
        """Return the raw payload (dict/list) from the source."""

    @abstractmethod
    def parse(self, raw: Any, company: dict[str, Any]) -> list[Job]:
        """Turn a raw payload into normalized Job objects."""

    def fetch(self, company: dict[str, Any]) -> list[Job]:
        return self.parse(self.fetch_raw(company), company)
