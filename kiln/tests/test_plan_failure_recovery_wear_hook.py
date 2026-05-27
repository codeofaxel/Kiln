"""Coverage for the ``plan_failure_recovery`` wear-hypothesis hook.

The tool lives in :mod:`kiln.plugins.recovery_tools` and, when
kiln-pro is installed, attaches a structured
``nozzle_wear_hypothesis`` block to its success-path response so
the user sees nozzle wear as a likely co-contributor to the
failure being planned for.

This file pins:

- Free tier (``ImportError`` on the kiln-pro import) returns the
  base ``{"success": True, "plan": ...}`` response unchanged.
- Pro+ with a non-``None`` hypothesis attaches it under
  ``nozzle_wear_hypothesis``.
- Pro+ with a ``None`` hypothesis (e.g. nozzle still in window,
  drift attribution below threshold) leaves the response
  unchanged — no empty key, no ``None`` value.
- An unexpected exception inside the kiln-pro pipeline is
  swallowed so the recovery plan still ships.
- The hook is on the SUCCESS return path only — a missing failure
  report or an engine-level exception still returns an error dict
  without a hypothesis key.

Sibling wire to the ``analyze_print_failure_smart`` hook on
``feat/nozzle-recovery-hook`` (commit ``1cb0969``).
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_mcp():
    """Mock MCP server that captures registered tools."""
    tools: dict[str, callable] = {}

    class MockMCP:
        def tool(self):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn
            return decorator

    return MockMCP(), tools


@pytest.fixture()
def recovery_tools(mock_mcp):
    """Register the recovery plugin and return the captured tool table."""
    mcp, tools = mock_mcp
    from kiln.plugins.recovery_tools import plugin
    plugin.register(mcp)
    return tools


def _make_failure_report(printer_name: str = "test_printer"):
    """Build a minimal FailureReport with the fields plan_failure_recovery reads."""
    from kiln.print_recovery import FailureReport, FailureType

    return FailureReport(
        failure_id="fail-abc-123",
        failure_type=FailureType.SPAGHETTI,
        detected_at="2026-05-27T10:00:00Z",
        printer_name=printer_name,
    )


def _make_recovery_plan():
    """Build a minimal RecoveryPlan stub with a ``to_dict`` method."""
    plan = MagicMock()
    plan.to_dict.return_value = {
        "plan_id": "plan-xyz-987",
        "failure_id": "fail-abc-123",
        "strategy": "resume_from_checkpoint",
    }
    return plan


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPlanFailureRecoveryWearHook:
    """Pin the wear-hypothesis hook on ``plan_failure_recovery``."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_free_tier_falls_through_silently(
        self, _mock_auth, recovery_tools, monkeypatch,
    ) -> None:
        """ImportError on the kiln-pro module leaves the response unchanged."""
        # Simulate "kiln-pro not installed" by stubbing the import target
        # to ``None`` in ``sys.modules`` — Python raises ImportError when
        # the hook tries the ``from ... import`` statement.
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.recovery.nozzle_wear_hypothesis",
            None,
        )

        engine = MagicMock()
        engine.get_failure_history.return_value = [_make_failure_report()]
        engine.plan_recovery.return_value = _make_recovery_plan()

        with patch("kiln.print_recovery.get_recovery_engine", return_value=engine):
            result = recovery_tools["plan_failure_recovery"](
                failure_id="fail-abc-123",
            )

        assert result["success"] is True
        assert "plan" in result
        assert "nozzle_wear_hypothesis" not in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_pro_tier_with_hypothesis_attaches_block(
        self, _mock_auth, recovery_tools, monkeypatch,
    ) -> None:
        """When kiln-pro returns a hypothesis dict, attach it to the response."""
        hypothesis_payload = {
            "status": "WARNING",
            "wear_attribution_pct": 0.42,
            "message": "Nozzle past planning window.",
        }
        fake_module = ModuleType("kiln_pro.recovery.nozzle_wear_hypothesis")
        fake_module.build_wear_hypothesis = MagicMock(  # type: ignore[attr-defined]
            return_value=hypothesis_payload,
        )
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.recovery.nozzle_wear_hypothesis",
            fake_module,
        )

        engine = MagicMock()
        engine.get_failure_history.return_value = [
            _make_failure_report(printer_name="bambu_a1"),
        ]
        engine.plan_recovery.return_value = _make_recovery_plan()

        with patch("kiln.print_recovery.get_recovery_engine", return_value=engine):
            result = recovery_tools["plan_failure_recovery"](
                failure_id="fail-abc-123",
            )

        assert result["success"] is True
        assert result["nozzle_wear_hypothesis"] == hypothesis_payload
        fake_module.build_wear_hypothesis.assert_called_once_with(
            printer_id="bambu_a1",
        )

    @patch("kiln.server._check_auth", return_value=None)
    def test_pro_tier_with_none_hypothesis_omits_block(
        self, _mock_auth, recovery_tools, monkeypatch,
    ) -> None:
        """A ``None`` hypothesis (wear not a credible co-contributor) is omitted."""
        fake_module = ModuleType("kiln_pro.recovery.nozzle_wear_hypothesis")
        fake_module.build_wear_hypothesis = MagicMock(return_value=None)  # type: ignore[attr-defined]
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.recovery.nozzle_wear_hypothesis",
            fake_module,
        )

        engine = MagicMock()
        engine.get_failure_history.return_value = [_make_failure_report()]
        engine.plan_recovery.return_value = _make_recovery_plan()

        with patch("kiln.print_recovery.get_recovery_engine", return_value=engine):
            result = recovery_tools["plan_failure_recovery"](
                failure_id="fail-abc-123",
            )

        assert result["success"] is True
        assert "nozzle_wear_hypothesis" not in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_pro_tier_internal_exception_is_swallowed(
        self, _mock_auth, recovery_tools, monkeypatch,
    ) -> None:
        """A raised exception inside the kiln-pro pipeline does not break the plan."""
        fake_module = ModuleType("kiln_pro.recovery.nozzle_wear_hypothesis")
        fake_module.build_wear_hypothesis = MagicMock(  # type: ignore[attr-defined]
            side_effect=RuntimeError("backend exploded"),
        )
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.recovery.nozzle_wear_hypothesis",
            fake_module,
        )

        engine = MagicMock()
        engine.get_failure_history.return_value = [_make_failure_report()]
        engine.plan_recovery.return_value = _make_recovery_plan()

        with patch("kiln.print_recovery.get_recovery_engine", return_value=engine):
            result = recovery_tools["plan_failure_recovery"](
                failure_id="fail-abc-123",
            )

        # Plan still ships; hook is best-effort.
        assert result["success"] is True
        assert "plan" in result
        assert "nozzle_wear_hypothesis" not in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_failure_not_found_does_not_attach_hypothesis(
        self, _mock_auth, recovery_tools, monkeypatch,
    ) -> None:
        """The hook lives on the SUCCESS return path only."""
        fake_module = ModuleType("kiln_pro.recovery.nozzle_wear_hypothesis")
        sentinel = MagicMock(return_value={"status": "WARNING"})
        fake_module.build_wear_hypothesis = sentinel  # type: ignore[attr-defined]
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.recovery.nozzle_wear_hypothesis",
            fake_module,
        )

        engine = MagicMock()
        engine.get_failure_history.return_value = []  # no matching failure
        with patch("kiln.print_recovery.get_recovery_engine", return_value=engine):
            result = recovery_tools["plan_failure_recovery"](
                failure_id="fail-missing",
            )

        # Failure-not-found returns an error dict; no hypothesis attaches.
        assert result.get("success") is False or "error" in result
        assert "nozzle_wear_hypothesis" not in result
        sentinel.assert_not_called()
