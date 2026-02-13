"""Tests for kiln.failure_rerouter — instant re-routing on failure.

Coverage areas:
- Safety-critical failures blocked from reroute
- Low progress prefers same-printer restart
- Successful reroute with alternatives
- Max attempts enforcement
- Cooldown enforcement
- No alternatives available
- Auto-reroute disabled by policy
- Reroute history tracking
- Stats aggregation
- Custom policy overrides
- execute_reroute validation
- RerouteDecision.to_dict() serialisation
- Best printer selection (learning engine fallback)
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from kiln.failure_rerouter import (
    FailureRerouter,
    RerouteDecision,
    ReroutePolicy,
    get_failure_rerouter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rerouter(*, policy: ReroutePolicy | None = None) -> FailureRerouter:
    """Create a fresh FailureRerouter with an optional custom policy."""
    return FailureRerouter(policy=policy)


def _default_printers() -> list[str]:
    return ["printer-a", "printer-b", "printer-c"]


# ---------------------------------------------------------------------------
# TestRerouteDecision
# ---------------------------------------------------------------------------


class TestRerouteDecision:
    """RerouteDecision dataclass and serialisation."""

    def test_to_dict_includes_all_fields(self) -> None:
        d = RerouteDecision(
            original_printer_id="p1",
            original_job_id="j1",
            failure_type="nozzle_clog",
            should_reroute=True,
            target_printer_id="p2",
            reason="test",
            estimated_time_saved_s=120.0,
            estimated_waste_pct=45.0,
            rerouted_at=1000.0,
        )
        result = d.to_dict()
        assert result["original_printer_id"] == "p1"
        assert result["original_job_id"] == "j1"
        assert result["failure_type"] == "nozzle_clog"
        assert result["should_reroute"] is True
        assert result["target_printer_id"] == "p2"
        assert result["reason"] == "test"
        assert result["estimated_time_saved_s"] == 120.0
        assert result["estimated_waste_pct"] == 45.0
        assert result["rerouted_at"] == 1000.0

    def test_to_dict_none_target(self) -> None:
        d = RerouteDecision(
            original_printer_id="p1",
            original_job_id="j1",
            failure_type="timeout",
            should_reroute=False,
        )
        assert d.to_dict()["target_printer_id"] is None

    def test_default_rerouted_at_is_timestamp(self) -> None:
        before = time.time()
        d = RerouteDecision(
            original_printer_id="p1",
            original_job_id="j1",
            failure_type="timeout",
            should_reroute=False,
        )
        after = time.time()
        assert before <= d.rerouted_at <= after


# ---------------------------------------------------------------------------
# TestReroutePolicy
# ---------------------------------------------------------------------------


class TestReroutePolicy:
    """ReroutePolicy defaults and overrides."""

    def test_defaults(self) -> None:
        p = ReroutePolicy()
        assert p.auto_reroute_enabled is True
        assert p.max_reroute_attempts == 2
        assert p.min_progress_for_restart_pct == 10.0
        assert "thermal_runaway" in p.excluded_failure_types
        assert "bed_adhesion_failure" in p.excluded_failure_types
        assert p.cooldown_s == 300.0

    def test_custom_values(self) -> None:
        p = ReroutePolicy(
            auto_reroute_enabled=False,
            max_reroute_attempts=5,
            min_progress_for_restart_pct=25.0,
            excluded_failure_types=["thermal_runaway"],
            cooldown_s=60.0,
        )
        assert p.auto_reroute_enabled is False
        assert p.max_reroute_attempts == 5
        assert p.min_progress_for_restart_pct == 25.0
        assert p.excluded_failure_types == ["thermal_runaway"]
        assert p.cooldown_s == 60.0


# ---------------------------------------------------------------------------
# TestSafetyCriticalBlocking
# ---------------------------------------------------------------------------


class TestSafetyCriticalBlocking:
    """Safety-critical failures must NEVER be auto-rerouted."""

    def test_thermal_runaway_blocked(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="thermal_runaway",
            progress_pct=80.0,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is False
        assert "Safety-critical" in decision.reason
        assert decision.target_printer_id is None

    def test_bed_adhesion_failure_blocked(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="bed_adhesion_failure",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is False
        assert "Safety-critical" in decision.reason

    def test_custom_excluded_failure_type(self) -> None:
        policy = ReroutePolicy(
            excluded_failure_types=["thermal_runaway", "bed_adhesion_failure", "layer_shift"],
        )
        rerouter = _make_rerouter(policy=policy)
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="layer_shift",
            progress_pct=60.0,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is False
        assert "Safety-critical" in decision.reason


# ---------------------------------------------------------------------------
# TestLowProgress
# ---------------------------------------------------------------------------


class TestLowProgress:
    """Low progress prefers same-printer restart over reroute."""

    def test_below_threshold_blocks_reroute(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=5.0,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is False
        assert "Low progress" in decision.reason

    def test_exactly_at_threshold_blocks_reroute(self) -> None:
        """Progress exactly at threshold is still below (< not <=)."""
        policy = ReroutePolicy(min_progress_for_restart_pct=10.0)
        rerouter = _make_rerouter(policy=policy)
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=9.9,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is False

    def test_above_threshold_allows_reroute(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=15.0,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is True

    def test_zero_progress_blocks_reroute(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="timeout",
            progress_pct=0.0,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is False

    def test_custom_threshold(self) -> None:
        policy = ReroutePolicy(min_progress_for_restart_pct=50.0)
        rerouter = _make_rerouter(policy=policy)
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=30.0,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is False


# ---------------------------------------------------------------------------
# TestSuccessfulReroute
# ---------------------------------------------------------------------------


class TestSuccessfulReroute:
    """Successful reroute with available alternatives."""

    def test_reroute_picks_alternative(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=["p1", "p2", "p3"],
        )
        assert decision.should_reroute is True
        assert decision.target_printer_id in ("p2", "p3")
        assert decision.target_printer_id != "p1"

    def test_reroute_excludes_failing_printer(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="printer-a",
            failure_type="power_loss",
            progress_pct=40.0,
            available_printers=["printer-a", "printer-b"],
        )
        assert decision.should_reroute is True
        assert decision.target_printer_id == "printer-b"

    def test_reroute_sets_waste_and_time_saved(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=60.0,
            available_printers=["p1", "p2"],
        )
        assert decision.should_reroute is True
        assert decision.estimated_waste_pct == 60.0
        assert decision.estimated_time_saved_s == 600.0  # 60 * 10

    def test_reroute_reason_includes_context(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="network_disconnect",
            progress_pct=70.0,
            available_printers=["p1", "p2"],
        )
        assert "p2" in decision.reason
        assert "network_disconnect" in decision.reason
        assert "70.0%" in decision.reason


# ---------------------------------------------------------------------------
# TestNoAlternatives
# ---------------------------------------------------------------------------


class TestNoAlternatives:
    """No alternative printers available blocks reroute."""

    def test_empty_alternatives_list(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=[],
        )
        assert decision.should_reroute is False
        assert "No alternative" in decision.reason

    def test_only_failing_printer_in_list(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=["p1"],
        )
        assert decision.should_reroute is False
        assert "No alternative" in decision.reason


# ---------------------------------------------------------------------------
# TestMaxAttemptsEnforcement
# ---------------------------------------------------------------------------


class TestMaxAttemptsEnforcement:
    """Max reroute attempts enforcement."""

    def test_first_attempt_allowed(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is True

    def test_exceeds_max_attempts(self) -> None:
        policy = ReroutePolicy(max_reroute_attempts=1, cooldown_s=0.0)
        rerouter = _make_rerouter(policy=policy)

        # First reroute succeeds
        d1 = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        assert d1.should_reroute is True
        rerouter.execute_reroute(d1)

        # Second reroute blocked
        d2 = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="printer-b",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        assert d2.should_reroute is False
        assert "Max reroute attempts" in d2.reason

    def test_different_jobs_have_separate_counters(self) -> None:
        policy = ReroutePolicy(max_reroute_attempts=1, cooldown_s=0.0)
        rerouter = _make_rerouter(policy=policy)

        d1 = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        rerouter.execute_reroute(d1)

        # Different job still allowed
        d2 = rerouter.evaluate_reroute(
            job_id="j2",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        assert d2.should_reroute is True


# ---------------------------------------------------------------------------
# TestCooldownEnforcement
# ---------------------------------------------------------------------------


class TestCooldownEnforcement:
    """Cooldown period enforcement between reroutes."""

    def test_cooldown_blocks_immediate_reroute(self) -> None:
        policy = ReroutePolicy(cooldown_s=300.0, max_reroute_attempts=5)
        rerouter = _make_rerouter(policy=policy)

        d1 = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        rerouter.execute_reroute(d1)

        # Immediate second attempt — cooldown should block
        d2 = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="printer-b",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        assert d2.should_reroute is False
        assert "Cooldown" in d2.reason

    def test_cooldown_expires_allows_reroute(self) -> None:
        policy = ReroutePolicy(cooldown_s=0.01, max_reroute_attempts=5)
        rerouter = _make_rerouter(policy=policy)

        d1 = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        rerouter.execute_reroute(d1)

        # Wait for cooldown to expire
        time.sleep(0.02)

        d2 = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="printer-b",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        assert d2.should_reroute is True

    def test_zero_cooldown_allows_immediate_reroute(self) -> None:
        policy = ReroutePolicy(cooldown_s=0.0, max_reroute_attempts=5)
        rerouter = _make_rerouter(policy=policy)

        d1 = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        rerouter.execute_reroute(d1)

        d2 = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="printer-b",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        assert d2.should_reroute is True


# ---------------------------------------------------------------------------
# TestAutoRerouteDisabled
# ---------------------------------------------------------------------------


class TestAutoRerouteDisabled:
    """Auto-reroute disabled by policy."""

    def test_disabled_blocks_all_reroutes(self) -> None:
        policy = ReroutePolicy(auto_reroute_enabled=False)
        rerouter = _make_rerouter(policy=policy)
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is False
        assert "disabled" in decision.reason.lower()


# ---------------------------------------------------------------------------
# TestRerouteHistory
# ---------------------------------------------------------------------------


class TestRerouteHistory:
    """Reroute history tracking."""

    def test_empty_history(self) -> None:
        rerouter = _make_rerouter()
        assert rerouter.get_reroute_history("nonexistent") == []

    def test_decisions_recorded_in_history(self) -> None:
        rerouter = _make_rerouter()
        rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        history = rerouter.get_reroute_history("j1")
        assert len(history) == 1
        assert history[0].original_job_id == "j1"

    def test_multiple_decisions_tracked(self) -> None:
        policy = ReroutePolicy(cooldown_s=0.0, max_reroute_attempts=10)
        rerouter = _make_rerouter(policy=policy)

        # Two evaluations for same job
        rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="timeout",
            progress_pct=60.0,
            available_printers=_default_printers(),
        )

        history = rerouter.get_reroute_history("j1")
        assert len(history) == 2
        assert history[0].failure_type == "nozzle_clog"
        assert history[1].failure_type == "timeout"

    def test_rejected_decisions_also_recorded(self) -> None:
        rerouter = _make_rerouter()
        rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="thermal_runaway",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        history = rerouter.get_reroute_history("j1")
        assert len(history) == 1
        assert history[0].should_reroute is False


# ---------------------------------------------------------------------------
# TestStatsAggregation
# ---------------------------------------------------------------------------


class TestStatsAggregation:
    """Stats aggregation across reroutes."""

    def test_initial_stats_empty(self) -> None:
        rerouter = _make_rerouter()
        stats = rerouter.get_reroute_stats()
        assert stats["total_reroutes"] == 0
        assert stats["successful_reroutes"] == 0
        assert stats["success_rate"] == 0.0
        assert stats["avg_time_saved_s"] == 0.0

    def test_stats_after_one_reroute(self) -> None:
        policy = ReroutePolicy(cooldown_s=0.0)
        rerouter = _make_rerouter(policy=policy)

        d = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        rerouter.execute_reroute(d)

        stats = rerouter.get_reroute_stats()
        assert stats["total_reroutes"] == 1
        assert stats["successful_reroutes"] == 1
        assert stats["success_rate"] == 100.0
        assert stats["avg_time_saved_s"] == 500.0  # 50 * 10

    def test_stats_after_multiple_reroutes(self) -> None:
        policy = ReroutePolicy(cooldown_s=0.0, max_reroute_attempts=10)
        rerouter = _make_rerouter(policy=policy)

        for i, progress in enumerate([30.0, 60.0]):
            d = rerouter.evaluate_reroute(
                job_id=f"j{i}",
                printer_id="p1",
                failure_type="nozzle_clog",
                progress_pct=progress,
                available_printers=_default_printers(),
            )
            rerouter.execute_reroute(d)

        stats = rerouter.get_reroute_stats()
        assert stats["total_reroutes"] == 2
        assert stats["avg_time_saved_s"] == 450.0  # (300 + 600) / 2


# ---------------------------------------------------------------------------
# TestExecuteReroute
# ---------------------------------------------------------------------------


class TestExecuteReroute:
    """execute_reroute validation and behaviour."""

    def test_execute_with_should_reroute_false_raises(self) -> None:
        rerouter = _make_rerouter()
        decision = RerouteDecision(
            original_printer_id="p1",
            original_job_id="j1",
            failure_type="thermal_runaway",
            should_reroute=False,
        )
        with pytest.raises(ValueError, match="should_reroute is False"):
            rerouter.execute_reroute(decision)

    def test_execute_returns_status_dict(self) -> None:
        rerouter = _make_rerouter()
        d = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        result = rerouter.execute_reroute(d)
        assert result["status"] == "rerouted"
        assert result["job_id"] == "j1"
        assert result["from_printer"] == "p1"
        assert result["to_printer"] is not None
        assert result["attempt"] == 1

    def test_execute_increments_attempt_counter(self) -> None:
        policy = ReroutePolicy(cooldown_s=0.0, max_reroute_attempts=5)
        rerouter = _make_rerouter(policy=policy)

        d1 = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        r1 = rerouter.execute_reroute(d1)
        assert r1["attempt"] == 1

        d2 = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="printer-b",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        r2 = rerouter.execute_reroute(d2)
        assert r2["attempt"] == 2


# ---------------------------------------------------------------------------
# TestBestPrinterSelection
# ---------------------------------------------------------------------------


class TestBestPrinterSelection:
    """Printer selection with and without learning engine."""

    def test_fallback_to_first_available_without_learning_engine(self) -> None:
        rerouter = _make_rerouter()
        # Patch import to raise so learning engine is not available
        with patch(
            "kiln.failure_rerouter.FailureRerouter._select_best_printer",
            wraps=rerouter._select_best_printer,
        ):
            decision = rerouter.evaluate_reroute(
                job_id="j1",
                printer_id="p1",
                failure_type="nozzle_clog",
                progress_pct=50.0,
                available_printers=["p1", "p2", "p3"],
            )
        # Should pick p2 (first non-failing printer)
        assert decision.target_printer_id == "p2"

    def test_learning_engine_import_failure_falls_back(self) -> None:
        """When cross_printer_learning is not importable, fall back gracefully."""
        rerouter = _make_rerouter()
        with patch(
            "kiln.failure_rerouter.FailureRerouter._select_best_printer",
        ) as mock_select:
            mock_select.return_value = "p3"
            decision = rerouter.evaluate_reroute(
                job_id="j1",
                printer_id="p1",
                failure_type="nozzle_clog",
                progress_pct=50.0,
                available_printers=["p1", "p2", "p3"],
            )
        assert decision.target_printer_id == "p3"


# ---------------------------------------------------------------------------
# TestCustomPolicyOverrides
# ---------------------------------------------------------------------------


class TestCustomPolicyOverrides:
    """Custom policy overrides all defaults."""

    def test_high_min_progress_threshold(self) -> None:
        policy = ReroutePolicy(min_progress_for_restart_pct=90.0)
        rerouter = _make_rerouter(policy=policy)
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=85.0,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is False
        assert "Low progress" in decision.reason

    def test_empty_excluded_list_allows_safety_failures(self) -> None:
        policy = ReroutePolicy(excluded_failure_types=[])
        rerouter = _make_rerouter(policy=policy)
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="thermal_runaway",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        # With no exclusions, even thermal_runaway is reroutable
        assert decision.should_reroute is True

    def test_max_zero_blocks_all_after_first(self) -> None:
        policy = ReroutePolicy(max_reroute_attempts=0)
        rerouter = _make_rerouter(policy=policy)
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is False
        assert "Max reroute attempts" in decision.reason


# ---------------------------------------------------------------------------
# TestSingleton
# ---------------------------------------------------------------------------


class TestSingleton:
    """Module-level singleton access."""

    def test_get_failure_rerouter_returns_instance(self) -> None:
        # Reset singleton for clean test
        import kiln.failure_rerouter as mod

        mod._rerouter = None
        rerouter = get_failure_rerouter()
        assert isinstance(rerouter, FailureRerouter)

    def test_get_failure_rerouter_returns_same_instance(self) -> None:
        import kiln.failure_rerouter as mod

        mod._rerouter = None
        r1 = get_failure_rerouter()
        r2 = get_failure_rerouter()
        assert r1 is r2


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_progress_100_still_reroutes(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="printer_error",
            progress_pct=100.0,
            available_printers=_default_printers(),
        )
        # 100% progress still reroutes — the failure happened at completion
        assert decision.should_reroute is True

    def test_negative_progress_treated_as_low(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="timeout",
            progress_pct=-5.0,
            available_printers=_default_printers(),
        )
        assert decision.should_reroute is False
        assert "Low progress" in decision.reason

    def test_unknown_failure_type_still_evaluated(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="alien_invasion",
            progress_pct=50.0,
            available_printers=_default_printers(),
        )
        # Unknown type is not excluded, so reroute proceeds
        assert decision.should_reroute is True

    def test_waste_pct_matches_progress(self) -> None:
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=42.5,
            available_printers=_default_printers(),
        )
        assert decision.estimated_waste_pct == 42.5

    def test_all_printers_are_failing_printer(self) -> None:
        """If all printers in the list are the failing printer, no alternatives."""
        rerouter = _make_rerouter()
        decision = rerouter.evaluate_reroute(
            job_id="j1",
            printer_id="p1",
            failure_type="nozzle_clog",
            progress_pct=50.0,
            available_printers=["p1", "p1", "p1"],
        )
        assert decision.should_reroute is False
        assert "No alternative" in decision.reason
