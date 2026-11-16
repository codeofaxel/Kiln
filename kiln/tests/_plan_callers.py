"""Callers as the plan check reads them.

The suite answers every plan check yes, so a door that asks the plan check
before it names a plan would otherwise meet a caller who holds every plan.
A test about what a door says to someone whose plan does not include what it
holds back asks for :func:`free_caller`.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def free_caller(monkeypatch):
    """A machine never signed in: the plan check refuses every paid plan and
    says where it is described."""
    from kiln import account_plan, licensing

    monkeypatch.setattr(licensing, "check_tier", account_plan.check_tier)
