"""The upgrade nudge: does the offer reach the agent, and did it convert?

Three things are pinned here, and the middle one is why this file exists.

1. The offer rides the FIRST tool result of a session, once, and never on
   an error or an empty result.
2. TWO features can wrap the lowlevel call-tool handler.  Before
   ``wrap_call_tool_result`` composed, the second caller was turned away
   with a ``False`` nobody checked — so whichever feature installed
   second was dead code that looked wired.  ``local_stage`` installs
   first in ``serve()``; the nudge installs after it.
3. The funnel counts every stage, because "nobody was offered an
   upgrade" and "everybody was offered one and pip failed" are the same
   zero without them, and they need opposite fixes.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from kiln import update_nudge
from kiln.mcp_compat import wrap_call_tool_result


class _Result:
    """Stands in for a CallToolResult: a mutable object with the fields
    the attach path reads."""

    def __init__(self, structured=None, is_error=False, text=None):
        self.structuredContent = structured
        self.isError = is_error
        self.content = [types.SimpleNamespace(type="text", text=text)] if text else []


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    update_nudge._reset_for_tests()
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.delenv("KILN_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("KILN_OFFLINE", raising=False)
    yield
    update_nudge._reset_for_tests()


def _offer_available(monkeypatch, available=True):
    info = {
        "available": True,
        "current": "1.1.9",
        "latest": "1.3.2",
        "command": "pip install --upgrade kiln3d",
        "summary": "Kiln 1.3.2 is available",
        "offer": "Want me to update Kiln for you now?",
        "action": "upgrade_kiln",
    }
    monkeypatch.setattr(
        "kiln.version_check.check_for_update",
        lambda *a, **k: (info if available else None),
    )


def _recorded(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr("kiln.daily_stats.record_update_nudge", seen.append)
    return seen


class TestAttach:
    def test_offer_rides_the_first_result(self, monkeypatch):
        _offer_available(monkeypatch)
        stages = _recorded(monkeypatch)
        r = _Result(structured={"success": True, "designs": []})
        update_nudge._attach(r, None, "list_designs")
        block = r.structuredContent[update_nudge.RESULT_KEY]
        assert block["latest"] == "1.3.2"
        assert block["action"] == "upgrade_kiln"
        assert "Offer to handle it" in block["note"]
        # The tool's own output survives — a host that renders only
        # structuredContent must not lose the result it rode in on.
        assert r.structuredContent["success"] is True
        assert stages == ["shown_tool_result"]

    def test_only_once_per_session(self, monkeypatch):
        _offer_available(monkeypatch)
        stages = _recorded(monkeypatch)
        first, second = (
            _Result(structured={"success": True}),
            _Result(structured={"success": True}),
        )
        update_nudge._attach(first, None, "a")
        update_nudge._attach(second, None, "b")
        assert update_nudge.RESULT_KEY in first.structuredContent
        assert update_nudge.RESULT_KEY not in second.structuredContent
        assert stages == ["shown_tool_result"]  # a nudge, not a nag

    def test_never_on_a_failure(self, monkeypatch):
        _offer_available(monkeypatch)
        stages = _recorded(monkeypatch)
        failed = _Result(structured={"success": False, "error": "nope"})
        errored = _Result(structured={"success": True}, is_error=True)
        update_nudge._attach(failed, None, "x")
        update_nudge._attach(errored, None, "y")
        assert update_nudge.RESULT_KEY not in failed.structuredContent
        assert update_nudge.RESULT_KEY not in errored.structuredContent
        assert stages == []
        # ...and the session is NOT spent: a later good result still gets it.
        ok = _Result(structured={"success": True})
        update_nudge._attach(ok, None, "z")
        assert update_nudge.RESULT_KEY in ok.structuredContent

    def test_no_offer_means_no_field(self, monkeypatch):
        _offer_available(monkeypatch, available=False)
        r = _Result(structured={"success": True})
        update_nudge._attach(r, None, "x")
        assert update_nudge.RESULT_KEY not in r.structuredContent

    def test_defers_when_there_is_nothing_to_seed_from(self, monkeypatch):
        # An unseedable result would be REPLACED by a bare nudge on a
        # structured-content host, hiding the tool's real output.
        _offer_available(monkeypatch)
        monkeypatch.setattr("kiln.local_stage._result_as_dict", lambda r: None)
        r = _Result(structured=None)
        update_nudge._attach(r, None, "x")
        assert r.structuredContent in (None, {})
        # The session is not spent — a seedable result later still gets it.
        good = _Result(structured={"success": True})
        update_nudge._attach(good, None, "y")
        assert update_nudge.RESULT_KEY in good.structuredContent

    def test_silent_on_the_hosted_server(self, monkeypatch):
        # The hosted package version is the SERVER's; telling a web user
        # to pip-upgrade is nonsense.
        _offer_available(monkeypatch)
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        r = _Result(structured={"success": True})
        update_nudge._attach(r, None, "x")
        assert update_nudge.RESULT_KEY not in r.structuredContent

    def test_yields_to_get_started(self, monkeypatch):
        # get_started/kiln_health own the "update" key; two offers in one
        # payload is noise.
        _offer_available(monkeypatch)
        r = _Result(structured={"success": True, "update": {"available": True}})
        update_nudge._attach(r, None, "get_started")
        assert update_nudge.RESULT_KEY not in r.structuredContent

    def test_a_broken_check_never_breaks_the_result(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("pypi is on fire")

        monkeypatch.setattr("kiln.version_check.check_for_update", boom)
        r = _Result(structured={"success": True})
        update_nudge._attach(r, None, "x")  # must not raise
        assert r.structuredContent == {"success": True}


class TestComposition:
    """The bug this feature would have died of, pinned.

    ``serve()`` installs ``local_stage`` first and the nudge second.  If
    the shared wrapper turns the second caller away, the nudge never
    runs — and nothing goes red, because a hook that was never installed
    simply does nothing.
    """

    def _fake_server(self):
        """A lowlevel server shaped like the INSTALLED SDK's, plus a way to call it.

        The two majors keep the ``tools/call`` handler in different places and
        invoke it with different arguments — that disagreement is the whole
        reason ``mcp_compat`` exists. This fake was 1.x-only (a dict keyed by
        request TYPE), so under SDK 2 these tests exercised a server shape the
        wrapper is right to reject, and went red for the harness rather than
        the code. ``pyproject`` pins ``mcp>=1.0``, so a fresh install resolves
        to 2.x and CI has been running that all along.

        Returns ``(mcp, invoke)``; ``invoke()`` runs whatever handler is
        registered now, so a test never has to know which major it is on.
        """
        from kiln.mcp_compat import MCP_SDK_MAJOR

        async def _handler(*_args):
            return types.SimpleNamespace(root=_Result(structured={"success": True}))

        if MCP_SDK_MAJOR >= 2:

            class _Srv:
                """Enough of SDK 2's registry for the wrapper to re-register into."""

                def __init__(self) -> None:
                    self._entries: dict = {}

                def get_request_handler(self, method):
                    return self._entries.get(method)

                def add_request_handler(self, method, params_type, handler):
                    self._entries[method] = types.SimpleNamespace(
                        handler=handler, params_type=params_type
                    )

            srv = _Srv()
            srv.add_request_handler("tools/call", object, _handler)

            def invoke():
                # 2.x calls handler(ctx, params).
                asyncio.run(srv.get_request_handler("tools/call").handler(None, object()))

            return types.SimpleNamespace(_lowlevel_server=srv), invoke

        from mcp.types import CallToolRequest  # 1.x keys the dict by request type

        srv = types.SimpleNamespace(request_handlers={CallToolRequest: _handler})

        def invoke():
            # 1.x calls handler(req).
            asyncio.run(srv.request_handlers[CallToolRequest](object()))

        return types.SimpleNamespace(_mcp_server=srv), invoke

    def test_a_second_mutator_composes_instead_of_being_dropped(self):
        mcp, invoke = self._fake_server()
        seen: list[str] = []

        def _stage_like(r, c, n):
            seen.append("first")

        def _nudge_like(r, c, n):
            seen.append("second")

        assert wrap_call_tool_result(mcp, _stage_like)
        assert wrap_call_tool_result(mcp, _nudge_like)

        invoke()
        assert seen == ["first", "second"], (
            "both mutators must run, in install order — a dropped second "
            "mutator is a feature that looks wired and does nothing"
        )

    def test_the_same_feature_installing_twice_still_attaches_once(self):
        """Composition must not cost idempotency: local_stage.install()
        running twice would otherwise attach the token twice and pay for
        the geometry twice."""
        mcp, invoke = self._fake_server()
        seen: list[str] = []

        def _one_feature(r, c, n):
            seen.append("x")

        assert wrap_call_tool_result(mcp, _one_feature) is True
        assert wrap_call_tool_result(mcp, _one_feature) is False

        invoke()
        assert seen == ["x"]

    def test_one_raising_mutator_cannot_cost_a_sibling_its_attach(self):
        mcp, invoke = self._fake_server()
        seen: list[str] = []

        def _boom(r, c, n):
            raise RuntimeError("bad mutator")

        def _survivor(r, c, n):
            seen.append("survived")

        assert wrap_call_tool_result(mcp, _boom)
        assert wrap_call_tool_result(mcp, _survivor)

        invoke()
        assert seen == ["survived"]


class TestFunnelCounters:
    def test_every_stage_is_a_valid_counter_key(self):
        from kiln.daily_stats import _TOOL_NAME_RE, _UPDATE_NUDGE_STAGES

        for stage in _UPDATE_NUDGE_STAGES:
            assert _TOOL_NAME_RE.match(stage), stage

    def test_unknown_stages_are_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        from kiln import daily_stats

        daily_stats.record_update_nudge("not_a_real_stage")
        assert not daily_stats.get_daily_stats().get("update_nudge")

    def test_the_funnel_reaches_the_heartbeat_payload(self, tmp_path, monkeypatch):
        # The counter, the projection, and the heartbeat field must move
        # together — a counter the heartbeat never ships is a wire that
        # ends in a wall.
        monkeypatch.setenv("HOME", str(tmp_path))
        from kiln import daily_stats

        daily_stats.record_update_nudge("shown_tool_result")
        daily_stats.record_update_nudge("upgrade_ok")
        stats = daily_stats.get_daily_stats()
        assert stats["update_nudge"] == {
            "shown_tool_result": 1,
            "upgrade_ok": 1,
        }
        assert "update_nudge" in daily_stats._ROLLOVER_MAPS

    def test_upgrade_tool_records_attempt_and_outcome(self, monkeypatch):
        """The offer being SHOWN and the upgrade WORKING are different
        questions; the tool owns the second half."""
        import kiln.plugins.utility_tools as ut

        src = ut.__loader__.get_source("kiln.plugins.utility_tools")
        for stage in (
            "upgrade_attempted",
            "upgrade_ok",
            "upgrade_failed",
            "upgrade_deferred",
        ):
            assert f'_stage("{stage}")' in src, (
                f"{stage} is never recorded — the funnel has a blind step"
            )
