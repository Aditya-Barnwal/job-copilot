"""Ashby adapter (jobs.ashbyhq.com/<org>/<id>/application), declarative:
specs/ashby.yaml. Fields are found by their labels (system fields
_systemfield_*, custom fields keyed by UUID); submission is Ashby's
non-user GraphQL ...Submit... operation, which the guard aborts."""
from __future__ import annotations

from .base import register
from .greenhouse import SpecAdapter


@register
class AshbyAdapter(SpecAdapter):
    spec_name = "ashby"


__all__ = ["AshbyAdapter"]
