"""The skipped-onboarding pointer: once, honestly, and never in the way.

Companion to test_update_nudge.py — same surface (the lowlevel result
hook), same seed-don't-replace rules, different message.  The failure it
exists for: agents are told at connect to call get_started() first,
measurably skip it, and then cannot rediscover mid-session what only
onboarding would have shown them.
"""

from __future__ import annotations

import types

import pytest

from kiln import onboarding_nudge


class _Result:
    """Stands in for a CallToolResult: a mutable object with the fields
    the attach path reads."""

    def __init__(self, structured=None, is_error=False, text=None):
        self.structuredContent = structured
        self.isError = is_error
        self.content = [types.SimpleNamespace(type="text", text=text)] if text else []


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    onboarding_nudge._reset_for_tests()
    monkeypatch.delenv("KILN_NO_ONBOARDING_NUDGE", raising=False)
    yield
    onboarding_nudge._reset_for_tests()


def _recorded(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        "kiln.daily_stats.record_event",
        lambda event_type, **kw: seen.append(event_type),
    )
    return seen


class TestAttach:
    def test_the_pointer_rides_the_first_result(self, monkeypatch):
        seen = _recorded(monkeypatch)
        r = _Result(structured={"success": True, "state": "idle"})
        onboarding_nudge._attach(r, None, "printer_status")

        block = r.structuredContent[onboarding_nudge.RESULT_KEY]
        assert "get_started" in block["note"]
        # The tools this exists to surface are named, not alluded to.
        assert "restart_server" in block["note"]
        # The result it rode in on is intact.
        assert r.structuredContent["success"] is True
        assert r.structuredContent["state"] == "idle"
        assert seen == ["onboarding_nudge_shown"]

    def test_only_once_per_session(self, monkeypatch):
        _recorded(monkeypatch)
        first = _Result(structured={"success": True})
        second = _Result(structured={"success": True})
        onboarding_nudge._attach(first, None, "printer_status")
        onboarding_nudge._attach(second, None, "slice_model")
        assert onboarding_nudge.RESULT_KEY in first.structuredContent
        assert onboarding_nudge.RESULT_KEY not in second.structuredContent

    @pytest.mark.parametrize("tool", sorted(onboarding_nudge.ONBOARDING_TOOLS))
    def test_calling_onboarding_first_means_no_nudge_ever(self, monkeypatch, tool):
        """A session that onboarded needs no pointer to onboarding."""
        _recorded(monkeypatch)
        onboarded = _Result(structured={"success": True})
        later = _Result(structured={"success": True})
        onboarding_nudge._attach(onboarded, None, tool)
        onboarding_nudge._attach(later, None, "printer_status")
        assert onboarding_nudge.RESULT_KEY not in (onboarded.structuredContent or {})
        assert onboarding_nudge.RESULT_KEY not in later.structuredContent

    def test_never_on_a_failure(self, monkeypatch):
        """An error should be relayed undistracted — and the session
        still gets its pointer on the next healthy result."""
        _recorded(monkeypatch)
        failed = _Result(structured={"success": False, "error": {"m": "x"}})
        errored = _Result(structured={"success": True}, is_error=True)
        ok = _Result(structured={"success": True})
        onboarding_nudge._attach(failed, None, "start_print")
        onboarding_nudge._attach(errored, None, "start_print")
        onboarding_nudge._attach(ok, None, "printer_status")
        assert onboarding_nudge.RESULT_KEY not in failed.structuredContent
        assert onboarding_nudge.RESULT_KEY not in errored.structuredContent
        assert onboarding_nudge.RESULT_KEY in ok.structuredContent

    def test_defers_when_there_is_nothing_to_seed_from(self, monkeypatch):
        """Attaching to an empty result would REPLACE the visible output
        on structured-content hosts; wait for one that can carry it."""
        _recorded(monkeypatch)
        empty = _Result(structured=None)
        carrier = _Result(structured={"success": True})
        onboarding_nudge._attach(empty, None, "printer_status")
        assert empty.structuredContent in (None, {})
        onboarding_nudge._attach(carrier, None, "printer_status")
        assert onboarding_nudge.RESULT_KEY in carrier.structuredContent

    def test_env_opt_out(self, monkeypatch):
        _recorded(monkeypatch)
        monkeypatch.setenv("KILN_NO_ONBOARDING_NUDGE", "1")
        r = _Result(structured={"success": True})
        onboarding_nudge._attach(r, None, "printer_status")
        assert onboarding_nudge.RESULT_KEY not in r.structuredContent

    def test_a_broken_stats_recorder_never_breaks_the_result(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("stats db locked")

        monkeypatch.setattr("kiln.daily_stats.record_event", _boom)
        r = _Result(structured={"success": True})
        onboarding_nudge._attach(r, None, "printer_status")
        # The nudge is best-effort end to end: the result survives intact
        # whether or not the attach round-trip completed.
        assert r.structuredContent["success"] is True
