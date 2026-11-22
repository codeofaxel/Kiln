"""``license_status`` answers for the caller a server is serving."""

from __future__ import annotations

import contextvars
import pytest


class _Info:
    def to_dict(self) -> dict:
        return {"tier": "enterprise", "is_valid": True, "is_expired": False, "source": "default"}


class _Manager:
    def get_info(self) -> _Info:
        return _Info()


@pytest.fixture
def served_licensing(monkeypatch):
    """The module as a server has it: the machine's own plan (enterprise) and
    the per-request slot a verified caller's plan is put in."""
    import kiln.licensing as licensing
    import kiln.server  # noqa: F401 -- loaded before the names below are swapped

    slot: contextvars.ContextVar = contextvars.ContextVar("caller_plan", default=None)
    monkeypatch.setattr(licensing, "get_license_manager", lambda: _Manager(), raising=False)
    monkeypatch.setattr(licensing, "_caller_tier_override", slot, raising=False)
    monkeypatch.setattr(kiln.server, "_annotate_session_liveness", lambda payload: None)
    return slot


def _ask(slot, plan):
    from kiln.server import license_status

    token = slot.set(plan)
    try:
        return license_status()
    finally:
        slot.reset(token)


def test_a_served_caller_is_told_their_own_plan(served_licensing):
    answer = _ask(served_licensing, "pro")
    assert answer["success"] is True
    assert answer["tier"] == "pro"
    assert "Pro" in answer["agent_summary"]


def test_a_free_caller_is_not_told_the_servers_plan(served_licensing):
    assert _ask(served_licensing, "free")["tier"] == "free"


def test_with_no_caller_set_the_machines_own_plan_is_reported(served_licensing):
    from kiln.server import license_status

    assert license_status()["tier"] == "enterprise"
