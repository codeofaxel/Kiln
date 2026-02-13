"""Tests for the smart job routing engine.

Covers:
- Basic routing with single and multiple printers
- Priority weighting (quality vs speed vs cost)
- Material filtering and scoring
- Capability filtering
- Distance filtering
- Input validation for all fields
- Score breakdown correctness
- Learning engine integration
- Edge cases (offline printers, empty queues, zero costs)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kiln.job_router import (
    JobRouter,
    PrinterScore,
    RoutingCriteria,
    RoutingValidationError,
    get_job_router,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _printer(
    printer_id: str = "voron-1",
    printer_model: str = "Voron 2.4",
    *,
    status: str = "idle",
    queue_depth: int = 0,
    supported_materials: list[str] | None = None,
    capabilities: list[str] | None = None,
    success_rate: float | None = None,
    estimated_wait_s: float = 0.0,
    cost_per_hour: float | None = None,
    distance_km: float | None = None,
    print_speed_factor: float = 1.0,
    **extra: Any,
) -> dict[str, Any]:
    """Build a printer info dict with sensible defaults."""
    d: dict[str, Any] = {
        "printer_id": printer_id,
        "printer_model": printer_model,
        "status": status,
        "queue_depth": queue_depth,
        "estimated_wait_s": estimated_wait_s,
        "print_speed_factor": print_speed_factor,
    }
    if supported_materials is not None:
        d["supported_materials"] = supported_materials
    if capabilities is not None:
        d["capabilities"] = capabilities
    if success_rate is not None:
        d["success_rate"] = success_rate
    if cost_per_hour is not None:
        d["cost_per_hour"] = cost_per_hour
    if distance_km is not None:
        d["distance_km"] = distance_km
    d.update(extra)
    return d


def _criteria(
    material: str = "PLA",
    **kwargs: Any,
) -> RoutingCriteria:
    """Build routing criteria with sensible defaults."""
    return RoutingCriteria(material=material, **kwargs)


# ---------------------------------------------------------------------------
# Basic routing
# ---------------------------------------------------------------------------


class TestBasicRouting:
    """Single-printer and basic multi-printer scenarios."""

    def test_single_printer_returns_it_as_recommended(self) -> None:
        router = JobRouter()
        result = router.route_job(
            _criteria(),
            [_printer()],
        )
        assert result.recommended_printer.printer_id == "voron-1"
        assert result.alternatives == []
        assert result.routing_time_ms >= 0.0

    def test_single_printer_score_in_valid_range(self) -> None:
        router = JobRouter()
        result = router.route_job(_criteria(), [_printer()])
        assert 0.0 <= result.recommended_printer.score <= 100.0

    def test_multi_printer_returns_highest_score_first(self) -> None:
        router = JobRouter()
        printers = [
            _printer("slow", status="printing", queue_depth=3),
            _printer("fast", status="idle", queue_depth=0),
        ]
        result = router.route_job(_criteria(), printers)
        assert result.recommended_printer.printer_id == "fast"

    def test_alternatives_capped_at_four(self) -> None:
        router = JobRouter()
        printers = [_printer(f"p-{i}") for i in range(7)]
        result = router.route_job(_criteria(), printers)
        assert len(result.alternatives) == 4

    def test_two_printers_one_alternative(self) -> None:
        router = JobRouter()
        printers = [_printer("a"), _printer("b")]
        result = router.route_job(_criteria(), printers)
        assert len(result.alternatives) == 1

    def test_routing_result_to_dict_structure(self) -> None:
        router = JobRouter()
        result = router.route_job(_criteria(), [_printer()])
        d = result.to_dict()
        assert "recommended_printer" in d
        assert "alternatives" in d
        assert "criteria_used" in d
        assert "routing_time_ms" in d
        assert d["criteria_used"]["material"] == "PLA"


# ---------------------------------------------------------------------------
# Scoring and ranking
# ---------------------------------------------------------------------------


class TestScoringAndRanking:
    """Multi-printer scoring, breakdown correctness, and ranking order."""

    def test_idle_printer_scores_higher_than_busy(self) -> None:
        router = JobRouter()
        idle = _printer("idle-1", status="idle")
        busy = _printer("busy-1", status="printing")
        result = router.route_job(_criteria(), [idle, busy])
        assert result.recommended_printer.printer_id == "idle-1"

    def test_high_reliability_printer_preferred(self) -> None:
        router = JobRouter()
        reliable = _printer("rel", success_rate=0.99)
        unreliable = _printer("unrel", success_rate=0.30)
        result = router.route_job(_criteria(), [reliable, unreliable])
        assert result.recommended_printer.printer_id == "rel"

    def test_score_breakdown_has_all_categories(self) -> None:
        router = JobRouter()
        result = router.route_job(_criteria(), [_printer()])
        bd = result.recommended_printer.breakdown
        assert set(bd.keys()) == {"material", "availability", "reliability", "speed", "cost"}

    def test_score_breakdown_values_are_non_negative(self) -> None:
        router = JobRouter()
        result = router.route_job(_criteria(), [_printer()])
        for v in result.recommended_printer.breakdown.values():
            assert v >= 0.0

    def test_available_flag_true_when_idle(self) -> None:
        router = JobRouter()
        score = router.score_printer(_criteria(), _printer(status="idle"))
        assert score.available is True

    def test_available_flag_false_when_printing(self) -> None:
        router = JobRouter()
        score = router.score_printer(_criteria(), _printer(status="printing"))
        assert score.available is False

    def test_offline_printer_gets_zero_availability(self) -> None:
        router = JobRouter()
        score = router.score_printer(_criteria(), _printer(status="offline"))
        assert score.breakdown["availability"] == 0.0

    def test_error_printer_gets_zero_availability(self) -> None:
        router = JobRouter()
        score = router.score_printer(_criteria(), _printer(status="error"))
        assert score.breakdown["availability"] == 0.0

    def test_queue_depth_penalizes_availability(self) -> None:
        router = JobRouter()
        empty_q = router.score_printer(_criteria(), _printer(queue_depth=0))
        deep_q = router.score_printer(_criteria(), _printer(queue_depth=5))
        assert empty_q.breakdown["availability"] > deep_q.breakdown["availability"]

    def test_fast_speed_factor_scores_higher(self) -> None:
        router = JobRouter()
        fast = router.score_printer(_criteria(), _printer(print_speed_factor=2.0))
        slow = router.score_printer(_criteria(), _printer(print_speed_factor=0.5))
        assert fast.breakdown["speed"] > slow.breakdown["speed"]

    def test_low_cost_scores_higher(self) -> None:
        router = JobRouter()
        cheap = router.score_printer(_criteria(), _printer(cost_per_hour=1.0))
        expensive = router.score_printer(_criteria(), _printer(cost_per_hour=10.0))
        assert cheap.breakdown["cost"] > expensive.breakdown["cost"]

    def test_no_cost_data_returns_neutral_score(self) -> None:
        router = JobRouter()
        score = router.score_printer(_criteria(), _printer())
        assert score.breakdown["cost"] == 50.0

    def test_no_reliability_data_returns_neutral_score(self) -> None:
        router = JobRouter()
        score = router.score_printer(_criteria(), _printer())
        assert score.breakdown["reliability"] == 50.0

    def test_printer_score_to_dict(self) -> None:
        router = JobRouter()
        score = router.score_printer(_criteria(), _printer(distance_km=5.0))
        d = score.to_dict()
        assert d["printer_id"] == "voron-1"
        assert d["distance_km"] == 5.0
        assert isinstance(d["score"], float)
        assert isinstance(d["breakdown"], dict)

    def test_estimated_wait_passes_through(self) -> None:
        router = JobRouter()
        score = router.score_printer(
            _criteria(), _printer(estimated_wait_s=300.0)
        )
        assert score.estimated_wait_s == 300.0

    def test_zero_cost_returns_max_score(self) -> None:
        router = JobRouter()
        score = router.score_printer(_criteria(), _printer(cost_per_hour=0.0))
        assert score.breakdown["cost"] == 100.0


# ---------------------------------------------------------------------------
# Priority weighting
# ---------------------------------------------------------------------------


class TestPriorityWeighting:
    """Verify that quality/speed/cost priorities shift scoring weights."""

    def test_high_quality_priority_boosts_reliable_printer(self) -> None:
        router = JobRouter()
        criteria = _criteria(quality_priority=5, speed_priority=1, cost_priority=1)
        reliable = _printer("rel", success_rate=0.99)
        fast = _printer("fast", success_rate=0.50, print_speed_factor=2.0)
        result = router.route_job(criteria, [reliable, fast])
        assert result.recommended_printer.printer_id == "rel"

    def test_high_speed_priority_boosts_fast_printer(self) -> None:
        router = JobRouter()
        criteria = _criteria(quality_priority=1, speed_priority=5, cost_priority=1)
        slow_reliable = _printer("rel", success_rate=0.99, print_speed_factor=0.5)
        fast_ok = _printer("fast", success_rate=0.70, print_speed_factor=2.0)
        result = router.route_job(criteria, [slow_reliable, fast_ok])
        assert result.recommended_printer.printer_id == "fast"

    def test_high_cost_priority_boosts_cheap_printer(self) -> None:
        router = JobRouter()
        criteria = _criteria(quality_priority=1, speed_priority=1, cost_priority=5)
        cheap = _printer("cheap", cost_per_hour=0.5, success_rate=0.70)
        pricey = _printer("pricey", cost_per_hour=10.0, success_rate=0.95)
        result = router.route_job(criteria, [cheap, pricey])
        assert result.recommended_printer.printer_id == "cheap"

    def test_default_priorities_are_balanced(self) -> None:
        router = JobRouter()
        criteria = _criteria()  # all priorities at 3
        weights = router._compute_weights(criteria)
        # Material should have highest weight
        assert weights["material"] > weights["cost"]
        # Verify normalization
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-9

    def test_extreme_priority_doesnt_zero_out_other_weights(self) -> None:
        router = JobRouter()
        criteria = _criteria(quality_priority=5, speed_priority=1, cost_priority=1)
        weights = router._compute_weights(criteria)
        for w in weights.values():
            assert w >= 0.01


# ---------------------------------------------------------------------------
# Material filtering and scoring
# ---------------------------------------------------------------------------


class TestMaterialScoring:
    """Material compatibility scoring with and without explicit support lists."""

    def test_supported_material_scores_high(self) -> None:
        router = JobRouter()
        score = router.score_printer(
            _criteria(material="PLA"),
            _printer(supported_materials=["PLA", "PETG"]),
        )
        assert score.breakdown["material"] == 100.0

    def test_unsupported_material_scores_zero(self) -> None:
        router = JobRouter()
        score = router.score_printer(
            _criteria(material="ABS"),
            _printer(supported_materials=["PLA", "PETG"]),
        )
        assert score.breakdown["material"] == 0.0

    def test_no_material_list_assumes_compatible(self) -> None:
        router = JobRouter()
        score = router.score_printer(
            _criteria(material="PLA"),
            _printer(),
        )
        assert score.breakdown["material"] == 70.0

    def test_material_with_learning_engine_blends_rate(self) -> None:
        mock_engine = MagicMock()
        mock_insight = MagicMock()
        mock_insight.sample_count = 100
        mock_insight.success_rate = 0.95
        mock_engine.get_material_insights.return_value = mock_insight

        router = JobRouter(learning_engine=mock_engine)
        score = router.score_printer(
            _criteria(material="PLA"),
            _printer(supported_materials=["PLA"]),
        )
        # Blended: 100 * 0.4 + (0.95 * 100) * 0.6 = 40 + 57 = 97.0
        assert abs(score.breakdown["material"] - 97.0) < 0.01

    def test_learning_engine_with_no_data_falls_back(self) -> None:
        mock_engine = MagicMock()
        mock_insight = MagicMock()
        mock_insight.sample_count = 0
        mock_engine.get_material_insights.return_value = mock_insight

        router = JobRouter(learning_engine=mock_engine)
        score = router.score_printer(
            _criteria(material="PLA"),
            _printer(supported_materials=["PLA"]),
        )
        assert score.breakdown["material"] == 100.0

    def test_learning_engine_exception_handled_gracefully(self) -> None:
        mock_engine = MagicMock()
        mock_engine.get_material_insights.side_effect = RuntimeError("boom")

        router = JobRouter(learning_engine=mock_engine)
        score = router.score_printer(
            _criteria(material="PLA"),
            _printer(supported_materials=["PLA"]),
        )
        # Falls back to base score
        assert score.breakdown["material"] == 100.0

    def test_material_success_rate_from_printer_info(self) -> None:
        router = JobRouter()
        score = router.score_printer(
            _criteria(), _printer(success_rate=0.85)
        )
        assert score.material_success_rate == 0.85

    def test_material_success_rate_none_when_no_data(self) -> None:
        router = JobRouter()
        score = router.score_printer(_criteria(), _printer())
        assert score.material_success_rate is None


# ---------------------------------------------------------------------------
# Capability filtering
# ---------------------------------------------------------------------------


class TestCapabilityFiltering:
    """Printers that lack required capabilities are excluded."""

    def test_printer_with_required_caps_included(self) -> None:
        router = JobRouter()
        criteria = _criteria(required_capabilities=["enclosure"])
        result = router.route_job(
            criteria,
            [_printer(capabilities=["enclosure", "multi_material"])],
        )
        assert result.recommended_printer.printer_id == "voron-1"

    def test_printer_missing_cap_excluded(self) -> None:
        router = JobRouter()
        criteria = _criteria(required_capabilities=["enclosure"])
        with pytest.raises(RoutingValidationError, match="No printers match"):
            router.route_job(criteria, [_printer(capabilities=["multi_material"])])

    def test_multiple_required_caps_all_must_match(self) -> None:
        router = JobRouter()
        criteria = _criteria(required_capabilities=["enclosure", "multi_material"])
        # Only has one of two required
        with pytest.raises(RoutingValidationError, match="No printers match"):
            router.route_job(criteria, [_printer(capabilities=["enclosure"])])

    def test_no_required_caps_allows_any_printer(self) -> None:
        router = JobRouter()
        result = router.route_job(_criteria(), [_printer()])
        assert result.recommended_printer.printer_id == "voron-1"

    def test_cap_filtering_selects_capable_from_mixed(self) -> None:
        router = JobRouter()
        criteria = _criteria(required_capabilities=["enclosure"])
        printers = [
            _printer("no-enc", capabilities=[]),
            _printer("has-enc", capabilities=["enclosure"]),
        ]
        result = router.route_job(criteria, printers)
        assert result.recommended_printer.printer_id == "has-enc"
        assert len(result.alternatives) == 0

    def test_printer_with_no_caps_key_excluded_when_caps_required(self) -> None:
        router = JobRouter()
        criteria = _criteria(required_capabilities=["enclosure"])
        with pytest.raises(RoutingValidationError, match="No printers match"):
            router.route_job(criteria, [_printer()])


# ---------------------------------------------------------------------------
# Distance filtering
# ---------------------------------------------------------------------------


class TestDistanceFiltering:
    """Printers beyond max_distance_km are excluded."""

    def test_printer_within_range_included(self) -> None:
        router = JobRouter()
        criteria = _criteria(max_distance_km=10.0)
        result = router.route_job(criteria, [_printer(distance_km=5.0)])
        assert result.recommended_printer.printer_id == "voron-1"

    def test_printer_beyond_range_excluded(self) -> None:
        router = JobRouter()
        criteria = _criteria(max_distance_km=5.0)
        with pytest.raises(RoutingValidationError, match="No printers match"):
            router.route_job(criteria, [_printer(distance_km=10.0)])

    def test_printer_at_exact_boundary_included(self) -> None:
        router = JobRouter()
        criteria = _criteria(max_distance_km=5.0)
        result = router.route_job(criteria, [_printer(distance_km=5.0)])
        assert result.recommended_printer.printer_id == "voron-1"

    def test_printer_without_distance_excluded_when_max_set(self) -> None:
        router = JobRouter()
        criteria = _criteria(max_distance_km=10.0)
        with pytest.raises(RoutingValidationError, match="No printers match"):
            router.route_job(criteria, [_printer()])

    def test_no_max_distance_includes_all(self) -> None:
        router = JobRouter()
        printers = [_printer("near", distance_km=1.0), _printer("far", distance_km=1000.0)]
        result = router.route_job(_criteria(), printers)
        assert len(result.alternatives) == 1

    def test_distance_km_passed_through_to_score(self) -> None:
        router = JobRouter()
        score = router.score_printer(_criteria(), _printer(distance_km=7.5))
        assert score.distance_km == 7.5

    def test_distance_km_none_when_not_provided(self) -> None:
        router = JobRouter()
        score = router.score_printer(_criteria(), _printer())
        assert score.distance_km is None


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Validation of RoutingCriteria and available_printers."""

    def test_empty_material_raises(self) -> None:
        router = JobRouter()
        with pytest.raises(RoutingValidationError, match="material must be a non-empty"):
            router.route_job(
                RoutingCriteria(material=""),
                [_printer()],
            )

    def test_material_too_long_raises(self) -> None:
        router = JobRouter()
        with pytest.raises(RoutingValidationError, match="at most 50"):
            router.route_job(
                RoutingCriteria(material="X" * 51),
                [_printer()],
            )

    def test_quality_priority_below_range_raises(self) -> None:
        router = JobRouter()
        with pytest.raises(RoutingValidationError, match="quality_priority"):
            router.route_job(
                RoutingCriteria(material="PLA", quality_priority=0),
                [_printer()],
            )

    def test_quality_priority_above_range_raises(self) -> None:
        router = JobRouter()
        with pytest.raises(RoutingValidationError, match="quality_priority"):
            router.route_job(
                RoutingCriteria(material="PLA", quality_priority=6),
                [_printer()],
            )

    def test_speed_priority_below_range_raises(self) -> None:
        router = JobRouter()
        with pytest.raises(RoutingValidationError, match="speed_priority"):
            router.route_job(
                RoutingCriteria(material="PLA", speed_priority=0),
                [_printer()],
            )

    def test_cost_priority_above_range_raises(self) -> None:
        router = JobRouter()
        with pytest.raises(RoutingValidationError, match="cost_priority"):
            router.route_job(
                RoutingCriteria(material="PLA", cost_priority=6),
                [_printer()],
            )

    def test_max_distance_zero_raises(self) -> None:
        router = JobRouter()
        with pytest.raises(RoutingValidationError, match="max_distance_km must be > 0"):
            router.route_job(
                RoutingCriteria(material="PLA", max_distance_km=0.0),
                [_printer(distance_km=1.0)],
            )

    def test_max_distance_negative_raises(self) -> None:
        router = JobRouter()
        with pytest.raises(RoutingValidationError, match="max_distance_km must be > 0"):
            router.route_job(
                RoutingCriteria(material="PLA", max_distance_km=-5.0),
                [_printer(distance_km=1.0)],
            )

    def test_empty_printers_list_raises(self) -> None:
        router = JobRouter()
        with pytest.raises(RoutingValidationError, match="non-empty list"):
            router.route_job(_criteria(), [])

    def test_printer_missing_id_raises(self) -> None:
        router = JobRouter()
        with pytest.raises(RoutingValidationError, match="missing required key 'printer_id'"):
            router.route_job(_criteria(), [{"printer_model": "Ender 3"}])

    def test_valid_priorities_at_boundaries(self) -> None:
        router = JobRouter()
        # Priority 1 and 5 should both be valid
        result = router.route_job(
            RoutingCriteria(
                material="PLA",
                quality_priority=1,
                speed_priority=5,
                cost_priority=1,
            ),
            [_printer()],
        )
        assert result.recommended_printer is not None

    def test_valid_max_distance(self) -> None:
        router = JobRouter()
        result = router.route_job(
            RoutingCriteria(material="PLA", max_distance_km=0.1),
            [_printer(distance_km=0.05)],
        )
        assert result.recommended_printer is not None


# ---------------------------------------------------------------------------
# Dataclass serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """to_dict() methods produce correct JSON-serialisable output."""

    def test_routing_criteria_to_dict(self) -> None:
        c = _criteria(
            material="ABS",
            file_hash="abc123",
            estimated_print_time_s=3600.0,
            quality_priority=4,
            speed_priority=2,
            cost_priority=5,
            max_distance_km=10.0,
            required_capabilities=["enclosure"],
        )
        d = c.to_dict()
        assert d["material"] == "ABS"
        assert d["file_hash"] == "abc123"
        assert d["estimated_print_time_s"] == 3600.0
        assert d["quality_priority"] == 4
        assert d["speed_priority"] == 2
        assert d["cost_priority"] == 5
        assert d["max_distance_km"] == 10.0
        assert d["required_capabilities"] == ["enclosure"]

    def test_printer_score_to_dict_rounds_values(self) -> None:
        ps = PrinterScore(
            printer_id="v1",
            printer_model="Voron",
            score=87.12345,
            breakdown={"material": 99.999, "availability": 50.001},
            available=True,
            estimated_wait_s=123.456,
            material_success_rate=0.98765,
            distance_km=3.14159,
        )
        d = ps.to_dict()
        assert d["score"] == 87.12
        assert d["breakdown"]["material"] == 100.0
        assert d["breakdown"]["availability"] == 50.0
        assert d["estimated_wait_s"] == 123.46
        assert d["material_success_rate"] == 0.9877
        assert d["distance_km"] == 3.14

    def test_printer_score_to_dict_none_values(self) -> None:
        ps = PrinterScore(
            printer_id="v1",
            printer_model="Voron",
            score=50.0,
            breakdown={},
            available=True,
            estimated_wait_s=0.0,
        )
        d = ps.to_dict()
        assert d["material_success_rate"] is None
        assert d["distance_km"] is None

    def test_routing_result_to_dict_nested(self) -> None:
        router = JobRouter()
        result = router.route_job(_criteria(), [_printer("a"), _printer("b")])
        d = result.to_dict()
        assert isinstance(d["recommended_printer"], dict)
        assert isinstance(d["alternatives"], list)
        assert len(d["alternatives"]) == 1
        assert isinstance(d["criteria_used"], dict)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    """get_job_router() returns a consistent singleton."""

    def test_get_job_router_returns_job_router_instance(self) -> None:
        # Reset singleton for test isolation
        import kiln.job_router as mod

        mod._router = None
        router = get_job_router()
        assert isinstance(router, JobRouter)

    def test_get_job_router_returns_same_instance(self) -> None:
        import kiln.job_router as mod

        mod._router = None
        r1 = get_job_router()
        r2 = get_job_router()
        assert r1 is r2


# ---------------------------------------------------------------------------
# Combined scenario tests
# ---------------------------------------------------------------------------


class TestCombinedScenarios:
    """End-to-end routing scenarios combining multiple criteria."""

    def test_material_and_capability_filter_combined(self) -> None:
        router = JobRouter()
        criteria = _criteria(
            material="ABS",
            required_capabilities=["enclosure"],
        )
        printers = [
            _printer("open-pla", supported_materials=["PLA"], capabilities=[]),
            _printer("enclosed-abs", supported_materials=["ABS"], capabilities=["enclosure"]),
            _printer("open-abs", supported_materials=["ABS"], capabilities=[]),
        ]
        result = router.route_job(criteria, printers)
        # Only enclosed-abs has both ABS support and enclosure
        assert result.recommended_printer.printer_id == "enclosed-abs"
        assert len(result.alternatives) == 0

    def test_all_printers_filtered_raises(self) -> None:
        router = JobRouter()
        criteria = _criteria(
            material="TPU",
            required_capabilities=["direct_drive"],
            max_distance_km=5.0,
        )
        printers = [
            _printer("far", capabilities=["direct_drive"], distance_km=100.0),
            _printer("close-no-cap", capabilities=[], distance_km=1.0),
        ]
        with pytest.raises(RoutingValidationError, match="No printers match"):
            router.route_job(criteria, printers)

    def test_ranking_with_diverse_fleet(self) -> None:
        router = JobRouter()
        criteria = _criteria(material="PETG")
        printers = [
            _printer(
                "workhorse",
                supported_materials=["PLA", "PETG"],
                status="idle",
                success_rate=0.95,
                cost_per_hour=2.0,
            ),
            _printer(
                "premium",
                supported_materials=["PLA", "PETG", "ABS", "TPU"],
                status="idle",
                success_rate=0.99,
                cost_per_hour=5.0,
                print_speed_factor=1.5,
            ),
            _printer(
                "budget",
                supported_materials=["PLA", "PETG"],
                status="idle",
                success_rate=0.80,
                cost_per_hour=0.5,
            ),
        ]
        result = router.route_job(criteria, printers)
        # All should be scored; top pick should be one of the idle ones
        assert result.recommended_printer.printer_id in {"workhorse", "premium", "budget"}
        assert len(result.alternatives) == 2

    def test_wait_time_affects_speed_score(self) -> None:
        router = JobRouter()
        no_wait = router.score_printer(_criteria(), _printer(estimated_wait_s=0.0))
        long_wait = router.score_printer(_criteria(), _printer(estimated_wait_s=3000.0))
        assert no_wait.breakdown["speed"] > long_wait.breakdown["speed"]
