"""Tests for kiln.reputation — operator profiles, feedback, and reputation engine.

Coverage areas:
- OperatorProfile dataclass, computed properties, to_dict
- OrderFeedback dataclass, validation
- ReputationEvent dataclass
- Registration and duplicate prevention
- Order completion tracking (success/failure)
- Feedback submission and quality score aggregation
- Reliability tier calculations (all 5 tiers)
- Verification flow
- Leaderboard sorting and filtering
- list_operators filtering
- get_operator_stats
- Input validation (all fields, edge cases)
- Thread safety
- Singleton accessor
"""

from __future__ import annotations

import threading
import time

import pytest

from kiln.reputation import (
    DuplicateOperatorError,
    OperatorNotFoundError,
    OperatorProfile,
    OrderFeedback,
    ReputationEngine,
    ReputationEvent,
    ReputationValidationError,
    _TIER_ORDER,
    get_reputation_engine,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine() -> ReputationEngine:
    """Return a fresh ReputationEngine for testing."""
    return ReputationEngine()


def _make_feedback(
    *,
    order_id: str = "order-1",
    operator_id: str = "op-1",
    customer_id: str = "cust-1",
    quality_score: int = 4,
    on_time: bool = True,
    communication_score: int = 4,
    would_recommend: bool = True,
    comment: str | None = None,
) -> OrderFeedback:
    return OrderFeedback(
        order_id=order_id,
        operator_id=operator_id,
        customer_id=customer_id,
        quality_score=quality_score,
        on_time=on_time,
        communication_score=communication_score,
        would_recommend=would_recommend,
        comment=comment,
    )


# ===========================================================================
# OperatorProfile dataclass
# ===========================================================================


class TestOperatorProfile:
    """OperatorProfile computed properties and serialisation."""

    def test_success_rate_no_orders(self):
        profile = OperatorProfile(operator_id="op-1", display_name="Test")
        assert profile.success_rate == 0.0

    def test_success_rate_all_successful(self):
        profile = OperatorProfile(
            operator_id="op-1",
            display_name="Test",
            total_orders=10,
            successful_orders=10,
            failed_orders=0,
        )
        assert profile.success_rate == 1.0

    def test_success_rate_mixed(self):
        profile = OperatorProfile(
            operator_id="op-1",
            display_name="Test",
            total_orders=10,
            successful_orders=7,
            failed_orders=3,
        )
        assert profile.success_rate == pytest.approx(0.7)

    def test_reliability_tier_platinum(self):
        profile = OperatorProfile(
            operator_id="op-1",
            display_name="Test",
            total_orders=100,
            successful_orders=98,
            failed_orders=2,
        )
        assert profile.reliability_tier == "platinum"

    def test_reliability_tier_gold(self):
        profile = OperatorProfile(
            operator_id="op-1",
            display_name="Test",
            total_orders=50,
            successful_orders=48,
            failed_orders=2,
        )
        assert profile.reliability_tier == "gold"

    def test_reliability_tier_silver(self):
        profile = OperatorProfile(
            operator_id="op-1",
            display_name="Test",
            total_orders=20,
            successful_orders=18,
            failed_orders=2,
        )
        assert profile.reliability_tier == "silver"

    def test_reliability_tier_bronze(self):
        profile = OperatorProfile(
            operator_id="op-1",
            display_name="Test",
            total_orders=5,
            successful_orders=4,
            failed_orders=1,
        )
        assert profile.reliability_tier == "bronze"

    def test_reliability_tier_new_not_enough_orders(self):
        profile = OperatorProfile(
            operator_id="op-1",
            display_name="Test",
            total_orders=3,
            successful_orders=3,
            failed_orders=0,
        )
        assert profile.reliability_tier == "new"

    def test_reliability_tier_new_low_rate(self):
        profile = OperatorProfile(
            operator_id="op-1",
            display_name="Test",
            total_orders=100,
            successful_orders=70,
            failed_orders=30,
        )
        assert profile.reliability_tier == "new"

    def test_reliability_tier_new_zero_orders(self):
        profile = OperatorProfile(operator_id="op-1", display_name="Test")
        assert profile.reliability_tier == "new"

    def test_to_dict_includes_computed_properties(self):
        profile = OperatorProfile(
            operator_id="op-1",
            display_name="Test Op",
            total_orders=50,
            successful_orders=48,
            failed_orders=2,
        )
        d = profile.to_dict()
        assert d["operator_id"] == "op-1"
        assert d["display_name"] == "Test Op"
        assert d["success_rate"] == pytest.approx(0.96)
        assert d["reliability_tier"] == "gold"
        assert "registered_at" in d
        assert "last_active_at" in d
        assert isinstance(d["materials_supported"], list)

    def test_to_dict_materials_is_copy(self):
        profile = OperatorProfile(
            operator_id="op-1",
            display_name="Test",
            materials_supported=["PLA"],
        )
        d = profile.to_dict()
        d["materials_supported"].append("ABS")
        assert "ABS" not in profile.materials_supported


# ===========================================================================
# OrderFeedback dataclass
# ===========================================================================


class TestOrderFeedback:
    """OrderFeedback serialisation."""

    def test_to_dict(self):
        fb = _make_feedback(comment="Great!")
        d = fb.to_dict()
        assert d["order_id"] == "order-1"
        assert d["quality_score"] == 4
        assert d["comment"] == "Great!"
        assert "created_at" in d


# ===========================================================================
# ReputationEvent dataclass
# ===========================================================================


class TestReputationEvent:
    """ReputationEvent serialisation and valid types."""

    def test_to_dict(self):
        event = ReputationEvent(
            event_type="order_completed",
            operator_id="op-1",
            metadata={"print_time_s": 3600},
        )
        d = event.to_dict()
        assert d["event_type"] == "order_completed"
        assert d["operator_id"] == "op-1"
        assert d["metadata"]["print_time_s"] == 3600

    def test_valid_event_types(self):
        expected = {
            "order_completed",
            "order_failed",
            "feedback_received",
            "verification_granted",
            "verification_revoked",
        }
        assert ReputationEvent._VALID_TYPES == expected


# ===========================================================================
# Registration
# ===========================================================================


class TestRegistration:
    """Operator registration and duplicate prevention."""

    def test_register_operator(self):
        engine = _make_engine()
        profile = engine.register_operator("op-1", "Test Operator")
        assert profile.operator_id == "op-1"
        assert profile.display_name == "Test Operator"
        assert profile.verified is False
        assert profile.total_orders == 0
        assert profile.reliability_tier == "new"

    def test_register_returns_profile(self):
        engine = _make_engine()
        profile = engine.register_operator("op-1", "Test")
        assert isinstance(profile, OperatorProfile)

    def test_duplicate_registration_raises(self):
        engine = _make_engine()
        engine.register_operator("op-1", "First")
        with pytest.raises(DuplicateOperatorError, match="op-1"):
            engine.register_operator("op-1", "Second")

    def test_get_operator_exists(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        profile = engine.get_operator("op-1")
        assert profile is not None
        assert profile.operator_id == "op-1"

    def test_get_operator_missing_returns_none(self):
        engine = _make_engine()
        assert engine.get_operator("nonexistent") is None


# ===========================================================================
# Order tracking
# ===========================================================================


class TestOrderTracking:
    """Order completion recording and stat updates."""

    def test_successful_order_increments_counts(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        engine.record_order_completion("op-1", success=True, print_time_s=3600.0)

        profile = engine.get_operator("op-1")
        assert profile.total_orders == 1
        assert profile.successful_orders == 1
        assert profile.failed_orders == 0

    def test_failed_order_increments_counts(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        engine.record_order_completion("op-1", success=False, print_time_s=1800.0)

        profile = engine.get_operator("op-1")
        assert profile.total_orders == 1
        assert profile.successful_orders == 0
        assert profile.failed_orders == 1

    def test_mixed_orders(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        engine.record_order_completion("op-1", success=True, print_time_s=1000.0)
        engine.record_order_completion("op-1", success=True, print_time_s=2000.0)
        engine.record_order_completion("op-1", success=False, print_time_s=500.0)

        profile = engine.get_operator("op-1")
        assert profile.total_orders == 3
        assert profile.successful_orders == 2
        assert profile.failed_orders == 1
        assert profile.success_rate == pytest.approx(2 / 3)

    def test_avg_print_time_rolling(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        engine.record_order_completion("op-1", success=True, print_time_s=1000.0)
        assert engine.get_operator("op-1").avg_print_time_s == pytest.approx(1000.0)

        engine.record_order_completion("op-1", success=True, print_time_s=3000.0)
        assert engine.get_operator("op-1").avg_print_time_s == pytest.approx(2000.0)

    def test_order_updates_last_active_at(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        before = time.time()
        engine.record_order_completion("op-1", success=True, print_time_s=100.0)
        after = time.time()

        profile = engine.get_operator("op-1")
        assert before <= profile.last_active_at <= after

    def test_order_for_unknown_operator_raises(self):
        engine = _make_engine()
        with pytest.raises(OperatorNotFoundError, match="nonexistent"):
            engine.record_order_completion(
                "nonexistent", success=True, print_time_s=100.0
            )

    def test_negative_print_time_raises(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        with pytest.raises(ReputationValidationError, match="print_time_s"):
            engine.record_order_completion("op-1", success=True, print_time_s=-1.0)

    def test_zero_print_time_allowed(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        engine.record_order_completion("op-1", success=True, print_time_s=0.0)
        assert engine.get_operator("op-1").total_orders == 1


# ===========================================================================
# Feedback
# ===========================================================================


class TestFeedback:
    """Feedback submission, validation, and quality score aggregation."""

    def test_submit_feedback_updates_quality_score(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        engine.submit_feedback(_make_feedback(quality_score=5))

        profile = engine.get_operator("op-1")
        assert profile.avg_quality_score == pytest.approx(5.0)

    def test_multiple_feedback_averages(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        engine.submit_feedback(_make_feedback(order_id="o1", quality_score=3))
        engine.submit_feedback(_make_feedback(order_id="o2", quality_score=5))

        profile = engine.get_operator("op-1")
        assert profile.avg_quality_score == pytest.approx(4.0)

    def test_feedback_for_unknown_operator_raises(self):
        engine = _make_engine()
        with pytest.raises(OperatorNotFoundError, match="op-999"):
            engine.submit_feedback(_make_feedback(operator_id="op-999"))

    def test_feedback_updates_last_active_at(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        before = time.time()
        engine.submit_feedback(_make_feedback())
        after = time.time()

        profile = engine.get_operator("op-1")
        assert before <= profile.last_active_at <= after

    def test_feedback_invalid_quality_score_low(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        with pytest.raises(ReputationValidationError, match="quality_score"):
            engine.submit_feedback(_make_feedback(quality_score=0))

    def test_feedback_invalid_quality_score_high(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        with pytest.raises(ReputationValidationError, match="quality_score"):
            engine.submit_feedback(_make_feedback(quality_score=6))

    def test_feedback_invalid_communication_score(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        with pytest.raises(ReputationValidationError, match="communication_score"):
            engine.submit_feedback(_make_feedback(communication_score=0))

    def test_feedback_comment_too_long(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        with pytest.raises(ReputationValidationError, match="comment"):
            engine.submit_feedback(_make_feedback(comment="x" * 501))

    def test_feedback_comment_control_chars(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        with pytest.raises(ReputationValidationError, match="control characters"):
            engine.submit_feedback(_make_feedback(comment="bad\x00char"))

    def test_feedback_comment_none_is_valid(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        engine.submit_feedback(_make_feedback(comment=None))
        # Should not raise

    def test_feedback_comment_max_length_exactly(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        engine.submit_feedback(_make_feedback(comment="x" * 500))
        # Should not raise


# ===========================================================================
# Reliability tiers (all 5 via the engine)
# ===========================================================================


class TestReliabilityTiers:
    """Verify all 5 reliability tiers are reachable through the engine."""

    def _build_operator(
        self,
        engine: ReputationEngine,
        operator_id: str,
        *,
        success_count: int,
        fail_count: int,
    ) -> OperatorProfile:
        engine.register_operator(operator_id, f"Op {operator_id}")
        for _ in range(success_count):
            engine.record_order_completion(
                operator_id, success=True, print_time_s=100.0
            )
        for _ in range(fail_count):
            engine.record_order_completion(
                operator_id, success=False, print_time_s=100.0
            )
        return engine.get_operator(operator_id)

    def test_tier_platinum(self):
        engine = _make_engine()
        profile = self._build_operator(
            engine, "plat", success_count=98, fail_count=2
        )
        assert profile.reliability_tier == "platinum"

    def test_tier_gold(self):
        engine = _make_engine()
        profile = self._build_operator(
            engine, "gold", success_count=48, fail_count=2
        )
        assert profile.reliability_tier == "gold"

    def test_tier_silver(self):
        engine = _make_engine()
        profile = self._build_operator(
            engine, "silver", success_count=18, fail_count=2
        )
        assert profile.reliability_tier == "silver"

    def test_tier_bronze(self):
        engine = _make_engine()
        profile = self._build_operator(
            engine, "bronze", success_count=4, fail_count=1
        )
        assert profile.reliability_tier == "bronze"

    def test_tier_new(self):
        engine = _make_engine()
        profile = self._build_operator(
            engine, "newbie", success_count=2, fail_count=0
        )
        assert profile.reliability_tier == "new"

    def test_tier_boundary_platinum_exact(self):
        """Exactly 98% with exactly 100 orders = platinum."""
        engine = _make_engine()
        profile = self._build_operator(
            engine, "exact-plat", success_count=98, fail_count=2
        )
        assert profile.total_orders == 100
        assert profile.success_rate == pytest.approx(0.98)
        assert profile.reliability_tier == "platinum"

    def test_tier_boundary_gold_not_enough_orders(self):
        """96% success but only 49 orders = not gold."""
        engine = _make_engine()
        profile = self._build_operator(
            engine, "almost-gold", success_count=47, fail_count=2
        )
        assert profile.total_orders == 49
        assert profile.success_rate > 0.95
        assert profile.reliability_tier != "gold"


# ===========================================================================
# Verification
# ===========================================================================


class TestVerification:
    """Operator verification flow."""

    def test_verify_operator(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        assert engine.get_operator("op-1").verified is False

        engine.verify_operator("op-1")
        assert engine.get_operator("op-1").verified is True

    def test_verify_unknown_operator_raises(self):
        engine = _make_engine()
        with pytest.raises(OperatorNotFoundError, match="unknown"):
            engine.verify_operator("unknown")

    def test_verify_idempotent(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        engine.verify_operator("op-1")
        engine.verify_operator("op-1")  # Should not raise
        assert engine.get_operator("op-1").verified is True


# ===========================================================================
# Leaderboard
# ===========================================================================


class TestLeaderboard:
    """Leaderboard sorting and filtering."""

    def test_leaderboard_sorts_by_tier_then_rate(self):
        engine = _make_engine()

        # Platinum operator
        engine.register_operator("plat", "Platinum Op")
        for _ in range(100):
            engine.record_order_completion("plat", success=True, print_time_s=100.0)

        # Gold operator
        engine.register_operator("gold", "Gold Op")
        for _ in range(48):
            engine.record_order_completion("gold", success=True, print_time_s=100.0)
        for _ in range(2):
            engine.record_order_completion("gold", success=False, print_time_s=100.0)

        # New operator
        engine.register_operator("new", "New Op")

        board = engine.get_leaderboard()
        assert board[0].operator_id == "plat"
        assert board[1].operator_id == "gold"
        assert board[2].operator_id == "new"

    def test_leaderboard_limit(self):
        engine = _make_engine()
        for i in range(20):
            engine.register_operator(f"op-{i}", f"Op {i}")

        board = engine.get_leaderboard(limit=5)
        assert len(board) == 5

    def test_leaderboard_material_filter(self):
        engine = _make_engine()
        engine.register_operator("pla-op", "PLA Op")
        engine.get_operator("pla-op").materials_supported.append("PLA")

        engine.register_operator("abs-op", "ABS Op")
        engine.get_operator("abs-op").materials_supported.append("ABS")

        board = engine.get_leaderboard(material="PLA")
        assert len(board) == 1
        assert board[0].operator_id == "pla-op"

    def test_leaderboard_empty(self):
        engine = _make_engine()
        assert engine.get_leaderboard() == []

    def test_leaderboard_same_tier_sorted_by_rate(self):
        engine = _make_engine()

        # Two "new" operators with different success rates
        engine.register_operator("better", "Better")
        engine.record_order_completion("better", success=True, print_time_s=100.0)
        engine.record_order_completion("better", success=True, print_time_s=100.0)

        engine.register_operator("worse", "Worse")
        engine.record_order_completion("worse", success=True, print_time_s=100.0)
        engine.record_order_completion("worse", success=False, print_time_s=100.0)

        board = engine.get_leaderboard()
        assert board[0].operator_id == "better"
        assert board[1].operator_id == "worse"


# ===========================================================================
# list_operators
# ===========================================================================


class TestListOperators:
    """Operator listing and filtering."""

    def test_list_all(self):
        engine = _make_engine()
        engine.register_operator("op-1", "One")
        engine.register_operator("op-2", "Two")
        result = engine.list_operators()
        assert len(result) == 2

    def test_list_verified_only(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Verified")
        engine.register_operator("op-2", "Not Verified")
        engine.verify_operator("op-1")

        result = engine.list_operators(verified_only=True)
        assert len(result) == 1
        assert result[0].operator_id == "op-1"

    def test_list_min_tier_filter(self):
        engine = _make_engine()

        # Gold-tier operator
        engine.register_operator("gold-op", "Gold")
        for _ in range(48):
            engine.record_order_completion("gold-op", success=True, print_time_s=100.0)
        for _ in range(2):
            engine.record_order_completion("gold-op", success=False, print_time_s=100.0)

        # New operator
        engine.register_operator("new-op", "New")

        # min_tier="gold" should only return gold or better
        result = engine.list_operators(min_tier="gold")
        assert len(result) == 1
        assert result[0].operator_id == "gold-op"

    def test_list_material_filter(self):
        engine = _make_engine()
        engine.register_operator("pla-op", "PLA Op")
        engine.get_operator("pla-op").materials_supported.append("PLA")

        engine.register_operator("abs-op", "ABS Op")
        engine.get_operator("abs-op").materials_supported.append("ABS")

        result = engine.list_operators(material="PLA")
        assert len(result) == 1
        assert result[0].operator_id == "pla-op"

    def test_list_invalid_tier_raises(self):
        engine = _make_engine()
        with pytest.raises(ReputationValidationError, match="Invalid tier"):
            engine.list_operators(min_tier="diamond")

    def test_list_empty(self):
        engine = _make_engine()
        assert engine.list_operators() == []


# ===========================================================================
# get_operator_stats
# ===========================================================================


class TestGetOperatorStats:
    """Full stats retrieval with feedback summary."""

    def test_stats_no_feedback(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        stats = engine.get_operator_stats("op-1")
        assert stats["operator_id"] == "op-1"
        assert stats["feedback_summary"]["feedback_count"] == 0
        assert stats["feedback_summary"]["avg_communication_score"] == 0.0

    def test_stats_with_feedback(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        engine.submit_feedback(_make_feedback(
            order_id="o1", quality_score=4, communication_score=5,
            on_time=True, would_recommend=True,
        ))
        engine.submit_feedback(_make_feedback(
            order_id="o2", quality_score=2, communication_score=3,
            on_time=False, would_recommend=False,
        ))

        stats = engine.get_operator_stats("op-1")
        summary = stats["feedback_summary"]
        assert summary["feedback_count"] == 2
        assert summary["avg_communication_score"] == pytest.approx(4.0)
        assert summary["recommend_rate"] == pytest.approx(0.5)
        assert summary["on_time_rate"] == pytest.approx(0.5)

    def test_stats_unknown_operator_raises(self):
        engine = _make_engine()
        with pytest.raises(OperatorNotFoundError, match="unknown"):
            engine.get_operator_stats("unknown")

    def test_stats_includes_computed_properties(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        engine.record_order_completion("op-1", success=True, print_time_s=1000.0)
        stats = engine.get_operator_stats("op-1")
        assert "success_rate" in stats
        assert "reliability_tier" in stats
        assert stats["success_rate"] == pytest.approx(1.0)


# ===========================================================================
# Input validation
# ===========================================================================


class TestInputValidation:
    """Boundary validation for all input fields."""

    def test_empty_operator_id_raises(self):
        engine = _make_engine()
        with pytest.raises(ReputationValidationError, match="operator_id"):
            engine.register_operator("", "Test")

    def test_whitespace_only_operator_id_raises(self):
        engine = _make_engine()
        with pytest.raises(ReputationValidationError, match="operator_id"):
            engine.register_operator("   ", "Test")

    def test_operator_id_too_long_raises(self):
        engine = _make_engine()
        with pytest.raises(ReputationValidationError, match="operator_id"):
            engine.register_operator("x" * 101, "Test")

    def test_operator_id_invalid_chars_raises(self):
        engine = _make_engine()
        with pytest.raises(ReputationValidationError, match="operator_id"):
            engine.register_operator("op@1", "Test")

    def test_operator_id_special_chars_rejected(self):
        engine = _make_engine()
        with pytest.raises(ReputationValidationError, match="operator_id"):
            engine.register_operator("op 1", "Test")

    def test_operator_id_valid_chars(self):
        engine = _make_engine()
        profile = engine.register_operator("op-1_test", "Test")
        assert profile.operator_id == "op-1_test"

    def test_operator_id_max_length_exactly(self):
        engine = _make_engine()
        profile = engine.register_operator("x" * 100, "Test")
        assert len(profile.operator_id) == 100

    def test_empty_display_name_raises(self):
        engine = _make_engine()
        with pytest.raises(ReputationValidationError, match="display_name"):
            engine.register_operator("op-1", "")

    def test_display_name_too_long_raises(self):
        engine = _make_engine()
        with pytest.raises(ReputationValidationError, match="display_name"):
            engine.register_operator("op-1", "x" * 201)

    def test_display_name_control_chars_raises(self):
        engine = _make_engine()
        with pytest.raises(ReputationValidationError, match="control characters"):
            engine.register_operator("op-1", "bad\x00name")

    def test_display_name_max_length_exactly(self):
        engine = _make_engine()
        profile = engine.register_operator("op-1", "x" * 200)
        assert len(profile.display_name) == 200

    def test_feedback_empty_order_id_raises(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        with pytest.raises(ReputationValidationError, match="order_id"):
            engine.submit_feedback(_make_feedback(order_id=""))

    def test_feedback_invalid_customer_id_raises(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        with pytest.raises(ReputationValidationError, match="customer_id"):
            engine.submit_feedback(_make_feedback(customer_id="cust@bad"))

    def test_print_time_not_a_number_raises(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        with pytest.raises(ReputationValidationError, match="print_time_s"):
            engine.record_order_completion(
                "op-1", success=True, print_time_s="not_a_number"
            )


# ===========================================================================
# Thread safety
# ===========================================================================


class TestThreadSafety:
    """Verify concurrent access doesn't corrupt state."""

    def test_concurrent_registrations(self):
        engine = _make_engine()
        errors: list[Exception] = []

        def register(op_id: str) -> None:
            try:
                engine.register_operator(op_id, f"Op {op_id}")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=register, args=(f"op-{i}",))
            for i in range(50)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(engine.list_operators()) == 50

    def test_concurrent_order_completions(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        errors: list[Exception] = []

        def record_order() -> None:
            try:
                engine.record_order_completion(
                    "op-1", success=True, print_time_s=100.0
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=record_order) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        profile = engine.get_operator("op-1")
        assert profile.total_orders == 100
        assert profile.successful_orders == 100

    def test_concurrent_feedback_submissions(self):
        engine = _make_engine()
        engine.register_operator("op-1", "Test")
        errors: list[Exception] = []

        def submit(idx: int) -> None:
            try:
                engine.submit_feedback(_make_feedback(order_id=f"order-{idx}"))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=submit, args=(i,))
            for i in range(50)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # avg_quality_score should be 4.0 (all feedback has quality_score=4)
        profile = engine.get_operator("op-1")
        assert profile.avg_quality_score == pytest.approx(4.0)

    def test_concurrent_duplicate_registration_one_succeeds(self):
        engine = _make_engine()
        results: list[str] = []
        errors: list[Exception] = []

        def register() -> None:
            try:
                engine.register_operator("op-dup", "Dup")
                results.append("success")
            except DuplicateOperatorError:
                results.append("duplicate")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=register) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert results.count("success") == 1
        assert results.count("duplicate") == 9


# ===========================================================================
# Singleton
# ===========================================================================


class TestSingleton:
    """Module-level singleton accessor."""

    def test_get_reputation_engine_returns_same_instance(self):
        # Reset the module-level singleton for a clean test
        import kiln.reputation as mod
        mod._engine = None

        engine1 = get_reputation_engine()
        engine2 = get_reputation_engine()
        assert engine1 is engine2

    def test_get_reputation_engine_is_reputation_engine(self):
        engine = get_reputation_engine()
        assert isinstance(engine, ReputationEngine)


# ===========================================================================
# Tier order constant
# ===========================================================================


class TestTierOrder:
    """Verify the tier ordering constant."""

    def test_tier_order(self):
        assert _TIER_ORDER == ["platinum", "gold", "silver", "bronze", "new"]
