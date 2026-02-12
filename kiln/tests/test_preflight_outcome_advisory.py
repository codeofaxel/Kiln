"""Tests for outcome-aware advisory checks in preflight_check."""

from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest


def _make_state(connected=True, idle=True):
    """Build a mock printer state."""
    from kiln.printers.base import PrinterStatus
    state = MagicMock()
    state.connected = connected
    state.state = PrinterStatus.IDLE if idle else PrinterStatus.PRINTING
    state.tool_temp_actual = 25.0
    state.tool_temp_target = 0.0
    state.bed_temp_actual = 25.0
    state.bed_temp_target = 0.0
    return state


class TestPreflightOutcomeAdvisory:
    """Verify outcome-history advisory checks in preflight_check."""

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_advisory_warning_for_low_material_success_rate(
        self, mock_registry, mock_get_db, mock_limits, mock_adapter
    ):
        """When material success rate < 30% with 3+ prints, add advisory warning."""
        mock_adapter.return_value.get_state.return_value = _make_state()
        mock_registry.count = 1
        mock_registry.list_names.return_value = ["voron"]

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.get_printer_learning_insights.return_value = {
            "total_outcomes": 10,
            "success_rate": 0.6,
            "failure_breakdown": {"warping": 3},
            "material_stats": {
                "PLA": {"count": 5, "success_rate": 0.2},  # 20% — triggers warning
            },
        }

        from kiln.server import preflight_check
        result = preflight_check(expected_material="PLA")

        # Find the outcome_history check
        history_checks = [c for c in result["checks"] if c["name"] == "outcome_history"]
        assert len(history_checks) == 1
        assert history_checks[0]["passed"] is True  # Advisory — always passes
        assert "20% success rate" in history_checks[0]["message"]
        assert history_checks[0].get("advisory") is True
        assert result["ready"] is True  # Overall still ready

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_advisory_warning_for_low_overall_success_rate(
        self, mock_registry, mock_get_db, mock_limits, mock_adapter
    ):
        """When overall success rate < 50% with 5+ prints, add advisory."""
        mock_adapter.return_value.get_state.return_value = _make_state()
        mock_registry.count = 1
        mock_registry.list_names.return_value = ["voron"]

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.get_printer_learning_insights.return_value = {
            "total_outcomes": 8,
            "success_rate": 0.25,
            "failure_breakdown": {"spaghetti": 4, "warping": 2},
            "material_stats": {},
        }

        from kiln.server import preflight_check
        result = preflight_check()

        history_checks = [c for c in result["checks"] if c["name"] == "outcome_history"]
        assert len(history_checks) == 1
        assert history_checks[0]["passed"] is True
        assert "25% overall success rate" in history_checks[0]["message"]
        assert history_checks[0].get("advisory") is True

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_no_advisory_with_insufficient_data(
        self, mock_registry, mock_get_db, mock_limits, mock_adapter
    ):
        """No advisory check when < 3 outcomes recorded."""
        mock_adapter.return_value.get_state.return_value = _make_state()
        mock_registry.count = 1
        mock_registry.list_names.return_value = ["voron"]

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.get_printer_learning_insights.return_value = {
            "total_outcomes": 2,
            "success_rate": 0.5,
            "failure_breakdown": {},
            "material_stats": {},
        }

        from kiln.server import preflight_check
        result = preflight_check()

        history_checks = [c for c in result["checks"] if c["name"] == "outcome_history"]
        assert len(history_checks) == 0  # Not enough data — no check added

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_advisory_never_blocks_print(
        self, mock_registry, mock_get_db, mock_limits, mock_adapter
    ):
        """Even with terrible success rate, ready should still be True."""
        mock_adapter.return_value.get_state.return_value = _make_state()
        mock_registry.count = 1
        mock_registry.list_names.return_value = ["voron"]

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.get_printer_learning_insights.return_value = {
            "total_outcomes": 100,
            "success_rate": 0.0,  # 0% — terrible
            "failure_breakdown": {"spaghetti": 100},
            "material_stats": {"PLA": {"count": 100, "success_rate": 0.0}},
        }

        from kiln.server import preflight_check
        result = preflight_check(expected_material="PLA")

        assert result["ready"] is True  # MUST still be ready

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db", side_effect=Exception("DB not available"))
    @patch("kiln.server._registry")
    def test_db_error_silently_skipped(
        self, mock_registry, mock_get_db, mock_limits, mock_adapter
    ):
        """DB errors should not break preflight."""
        mock_adapter.return_value.get_state.return_value = _make_state()
        mock_registry.count = 1
        mock_registry.list_names.return_value = ["voron"]

        from kiln.server import preflight_check
        result = preflight_check()

        assert result["success"] is True
        assert result["ready"] is True

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_positive_learning_data_shown_when_rate_is_good(
        self, mock_registry, mock_get_db, mock_limits, mock_adapter
    ):
        """When success rate is healthy, show positive learning data."""
        mock_adapter.return_value.get_state.return_value = _make_state()
        mock_registry.count = 1
        mock_registry.list_names.return_value = ["voron"]

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.get_printer_learning_insights.return_value = {
            "total_outcomes": 20,
            "success_rate": 0.9,
            "failure_breakdown": {},
            "material_stats": {},
        }

        from kiln.server import preflight_check
        result = preflight_check()

        history_checks = [c for c in result["checks"] if c["name"] == "outcome_history"]
        assert len(history_checks) == 1
        assert history_checks[0]["passed"] is True
        assert "90% success rate" in history_checks[0]["message"]
        # Positive data should NOT have advisory flag
        assert "advisory" not in history_checks[0]

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server._registry")
    def test_no_registry_skips_outcome_check(
        self, mock_registry, mock_limits, mock_adapter
    ):
        """When no printers registered, outcome check is skipped entirely."""
        mock_adapter.return_value.get_state.return_value = _make_state()
        mock_registry.count = 0

        from kiln.server import preflight_check
        result = preflight_check()

        history_checks = [c for c in result["checks"] if c["name"] == "outcome_history"]
        assert len(history_checks) == 0
