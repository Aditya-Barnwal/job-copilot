"""Every RSS/Atom reader refuses DTD / entity tricks and odd encodings (reviewer S5 follow-up)."""
from __future__ import annotations

import pytest

from jobcopilot import funding
from jobcopilot.connectors import jobspresso, weworkremotely
from jobcopilot.connectors.realworkfromanywhere import UnsafeFeed

BOMB = ('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "AAAA"><!ENTITY b "&a;&a;&a;&a;">]>'
        '<rss><channel><item><title>&b;</title></item></channel></rss>')


def _bodies():
    return [BOMB, BOMB.encode("utf-8"), BOMB.encode("utf-16"), ("<!-- x -->" + BOMB).encode()]


@pytest.mark.parametrize("body", _bodies())
def test_jobspresso_and_wwr_refuse_dtd_and_utf16(body):
    with pytest.raises(UnsafeFeed):
        jobspresso.JobspressoConnector().parse(body, {"attribution": {"name": "J", "url": "https://jobspresso.co"}})
    with pytest.raises(UnsafeFeed):
        weworkremotely.WeWorkRemotelyConnector().parse(body, {"attribution": {"name": "W", "url": "https://weworkremotely.com"}})


def test_funding_feed_refuses_dtd(monkeypatch):
    class R:
        content = BOMB.encode()

    class C:
        def get(self, *a, **k):
            return R()

    monkeypatch.setattr("jobcopilot.http.default_client", lambda: C())
    with pytest.raises(UnsafeFeed):
        funding._parse_rss("https://example.com/feed")
