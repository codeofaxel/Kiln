"""Coverage for the ``plan_failure_recovery`` wear-hypothesis hook.

The tool lives in :mod:`kiln.plugins.recovery_tools`.  With kiln-pro
installed it merges whatever ``wear_hypothesis_fields`` hands back into
its success-path response: the ``nozzle_wear_hypothesis`` block, the
``nozzle_wear_hypothesis_gated`` line for a caller below the tier, or
nothing.  kiln-pro decides which, by the caller's tier; this door
decides nothing, and these tests pin that it merges and stays out of
the way.

This file pins:

- A plain install (``ImportError`` on the kiln-pro import) returns the
  base ``{"success": True, "plan": ...}`` response unchanged.
- Whatever kiln-pro returns is merged as given -- the block or the line.
- An empty answer leaves the response unchanged -- no empty key, no
  ``None`` value.
- An unexpected exception inside the kiln-pro pipeline is
  swallowed so the recovery plan still ships.
- The hook is on the SUCCESS return path only — a missing failure
  report or an engine-level exception still returns an error dict
  without a hypothesis key.

Which tier gets which answer is kiln-pro's to pin
(``tests/test_recovery_paid_blocks_follow_caller_tier.py`` there, through
these registered tools).
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
        def tool(self, **_kwargs):
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
    def test_plain_install_falls_through_silently(
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
    @pytest.mark.parametrize(
        "fields",
        [
            {"nozzle_wear_hypothesis": {"cause": "nozzle_wear", "wear_attribution_pct": 0.42}},
            {"nozzle_wear_hypothesis_gated": {"required_tier": "pro", "headline": "h"}},
        ],
        ids=["block", "gated-line"],
    )
    def test_kiln_pro_answer_is_merged_as_given(
        self, _mock_auth, recovery_tools, monkeypatch, fields,
    ) -> None:
        """Whatever kiln-pro hands back -- the block or the line -- is merged as given."""
        fake_module = ModuleType("kiln_pro.recovery.nozzle_wear_hypothesis")
        fake_module.wear_hypothesis_fields = MagicMock(  # type: ignore[attr-defined]
            return_value=fields,
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
        for key, value in fields.items():
            assert result[key] == value
        # Called with the failure's printer and the classified drift_kind.
        # The default fixture's failure report carries no flow/dimensional
        # keywords, so the classifier resolves to "unknown".
        fake_module.wear_hypothesis_fields.assert_called_once_with(
            printer_id="bambu_a1",
            drift_kind="unknown",
        )

    @patch("kiln.server._check_auth", return_value=None)
    def test_empty_answer_adds_nothing(
        self, _mock_auth, recovery_tools, monkeypatch,
    ) -> None:
        """An empty answer (wear not implicated, or nothing to say) adds no key."""
        fake_module = ModuleType("kiln_pro.recovery.nozzle_wear_hypothesis")
        fake_module.wear_hypothesis_fields = MagicMock(return_value={})  # type: ignore[attr-defined]
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
        assert not any(k.startswith("nozzle_wear_hypothesis") for k in result)

    @patch("kiln.server._check_auth", return_value=None)
    def test_internal_exception_is_swallowed(
        self, _mock_auth, recovery_tools, monkeypatch,
    ) -> None:
        """A raised exception inside the kiln-pro pipeline does not break the plan."""
        fake_module = ModuleType("kiln_pro.recovery.nozzle_wear_hypothesis")
        fake_module.wear_hypothesis_fields = MagicMock(  # type: ignore[attr-defined]
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
        sentinel = MagicMock(return_value={"nozzle_wear_hypothesis": {"cause": "nozzle_wear"}})
        fake_module.wear_hypothesis_fields = sentinel  # type: ignore[attr-defined]
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
