"""Reputation and verified operator profiles for the Kiln network.

Tracks operator performance metrics, collects order feedback, computes
reliability tiers, and provides leaderboard / filtering queries.  Agents
use this to surface trustworthy operators to customers.

Usage::

    from kiln.reputation import get_reputation_engine

    engine = get_reputation_engine()
    profile = engine.register_operator("op-42", "Acme Prints")
    engine.record_order_completion("op-42", success=True, print_time_s=3600.0)
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

_MAX_OPERATOR_ID_LEN = 100
_MAX_CUSTOMER_ID_LEN = 100
_MAX_DISPLAY_NAME_LEN = 200
_MAX_COMMENT_LEN = 500
_MAX_PRINT_TIME_S = 604800  # 7 days
_MIN_QUALITY_SCORE = 1
_MAX_QUALITY_SCORE = 5

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Control characters: U+0000..U+001F and U+007F..U+009F
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Reliability tier thresholds (rate, min_orders)
_TIER_PLATINUM = (0.98, 100)
_TIER_GOLD = (0.95, 50)
_TIER_SILVER = (0.90, 20)
_TIER_BRONZE = (0.80, 5)

_TIER_ORDER = ["platinum", "gold", "silver", "bronze", "new"]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ReputationValidationError(ValueError):
    """Raised when input validation fails for a reputation operation."""


class OperatorNotFoundError(KeyError):
    """Raised when an operator_id is not in the registry."""

    def __init__(self, operator_id: str) -> None:
        super().__init__(f"Operator not found: {operator_id!r}")
        self.operator_id = operator_id


class DuplicateOperatorError(ValueError):
    """Raised when registering an operator_id that already exists."""

    def __init__(self, operator_id: str) -> None:
        super().__init__(f"Operator already registered: {operator_id!r}")
        self.operator_id = operator_id


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class OperatorProfile:
    """A print operator's profile and aggregated performance metrics.

    :param operator_id: Unique operator identifier.
    :param display_name: Human-readable name.
    :param registered_at: Unix timestamp of registration.
    :param verified: Whether the operator has been admin-verified.
    :param printer_count: Number of printers the operator manages.
    :param total_orders: Total orders received.
    :param successful_orders: Orders completed successfully.
    :param failed_orders: Orders that failed.
    :param avg_print_time_s: Rolling average print time in seconds.
    :param avg_quality_score: Rolling average quality score (0-5).
    :param materials_supported: List of supported material types.
    :param response_time_avg_s: Average response time in seconds.
    :param last_active_at: Unix timestamp of last activity.
    """

    operator_id: str
    display_name: str
    registered_at: float = field(default_factory=time.time)
    verified: bool = False
    printer_count: int = 0
    total_orders: int = 0
    successful_orders: int = 0
    failed_orders: int = 0
    avg_print_time_s: float = 0.0
    avg_quality_score: float = 0.0
    materials_supported: list[str] = field(default_factory=list)
    response_time_avg_s: float = 0.0
    last_active_at: float = field(default_factory=time.time)

    @property
    def success_rate(self) -> float:
        """Fraction of successful orders (0.0 if no orders)."""
        if self.total_orders == 0:
            return 0.0
        return self.successful_orders / self.total_orders

    @property
    def reliability_tier(self) -> str:
        """Compute reliability tier from success rate and order count.

        Tiers:
        - ``"platinum"``: >= 98% success rate + >= 100 orders
        - ``"gold"``:     >= 95% success rate + >= 50 orders
        - ``"silver"``:   >= 90% success rate + >= 20 orders
        - ``"bronze"``:   >= 80% success rate + >= 5 orders
        - ``"new"``:      everything else
        """
        rate = self.success_rate
        total = self.total_orders

        if rate >= _TIER_PLATINUM[0] and total >= _TIER_PLATINUM[1]:
            return "platinum"
        if rate >= _TIER_GOLD[0] and total >= _TIER_GOLD[1]:
            return "gold"
        if rate >= _TIER_SILVER[0] and total >= _TIER_SILVER[1]:
            return "silver"
        if rate >= _TIER_BRONZE[0] and total >= _TIER_BRONZE[1]:
            return "bronze"
        return "new"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary with computed properties."""
        return {
            "operator_id": self.operator_id,
            "display_name": self.display_name,
            "registered_at": self.registered_at,
            "verified": self.verified,
            "printer_count": self.printer_count,
            "total_orders": self.total_orders,
            "successful_orders": self.successful_orders,
            "failed_orders": self.failed_orders,
            "avg_print_time_s": self.avg_print_time_s,
            "avg_quality_score": self.avg_quality_score,
            "materials_supported": list(self.materials_supported),
            "response_time_avg_s": self.response_time_avg_s,
            "last_active_at": self.last_active_at,
            "success_rate": self.success_rate,
            "reliability_tier": self.reliability_tier,
        }


@dataclass
class OrderFeedback:
    """Customer feedback for a completed order.

    :param order_id: Unique order identifier.
    :param operator_id: Operator who fulfilled the order.
    :param customer_id: Customer who placed the order.
    :param quality_score: Quality rating (1-5).
    :param on_time: Whether the order was delivered on time.
    :param communication_score: Communication rating (1-5).
    :param would_recommend: Whether the customer would recommend.
    :param comment: Optional free-text comment (max 500 chars).
    :param created_at: Unix timestamp when feedback was submitted.
    """

    order_id: str
    operator_id: str
    customer_id: str
    quality_score: int
    on_time: bool
    communication_score: int
    would_recommend: bool
    comment: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "order_id": self.order_id,
            "operator_id": self.operator_id,
            "customer_id": self.customer_id,
            "quality_score": self.quality_score,
            "on_time": self.on_time,
            "communication_score": self.communication_score,
            "would_recommend": self.would_recommend,
            "comment": self.comment,
            "created_at": self.created_at,
        }


@dataclass
class ReputationEvent:
    """An auditable event in the reputation system.

    :param event_type: One of ``"order_completed"``, ``"order_failed"``,
        ``"feedback_received"``, ``"verification_granted"``,
        ``"verification_revoked"``.
    :param operator_id: Operator this event relates to.
    :param timestamp: Unix timestamp of the event.
    :param metadata: Arbitrary additional data.
    """

    event_type: str
    operator_id: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    _VALID_TYPES = frozenset(
        {
            "order_completed",
            "order_failed",
            "feedback_received",
            "verification_granted",
            "verification_revoked",
        }
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "event_type": self.event_type,
            "operator_id": self.operator_id,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_id(value: str, field_name: str, *, max_len: int) -> None:
    """Validate an identifier string (operator_id, customer_id, order_id).

    :raises ReputationValidationError: If validation fails.
    """
    if not isinstance(value, str) or not value.strip():
        raise ReputationValidationError(f"{field_name} must be a non-empty string")
    if len(value) > max_len:
        raise ReputationValidationError(f"{field_name} exceeds max length of {max_len} characters")
    if not _ID_RE.match(value):
        raise ReputationValidationError(
            f"{field_name} contains invalid characters; only alphanumeric, hyphens, and underscores are allowed"
        )


def _validate_display_name(value: str) -> None:
    """Validate a display name.

    :raises ReputationValidationError: If validation fails.
    """
    if not isinstance(value, str) or not value.strip():
        raise ReputationValidationError("display_name must be a non-empty string")
    if len(value) > _MAX_DISPLAY_NAME_LEN:
        raise ReputationValidationError(f"display_name exceeds max length of {_MAX_DISPLAY_NAME_LEN} characters")
    if _CONTROL_CHAR_RE.search(value):
        raise ReputationValidationError("display_name contains control characters")


def _validate_score(value: int, field_name: str) -> None:
    """Validate a 1-5 integer score.

    :raises ReputationValidationError: If validation fails.
    """
    if not isinstance(value, int):
        raise ReputationValidationError(f"{field_name} must be an integer")
    if value < _MIN_QUALITY_SCORE or value > _MAX_QUALITY_SCORE:
        raise ReputationValidationError(f"{field_name} must be between {_MIN_QUALITY_SCORE} and {_MAX_QUALITY_SCORE}")


def _validate_feedback(feedback: OrderFeedback) -> None:
    """Validate all fields of an :class:`OrderFeedback`.

    :raises ReputationValidationError: If any field is invalid.
    """
    _validate_id(feedback.order_id, "order_id", max_len=_MAX_OPERATOR_ID_LEN)
    _validate_id(feedback.operator_id, "operator_id", max_len=_MAX_OPERATOR_ID_LEN)
    _validate_id(feedback.customer_id, "customer_id", max_len=_MAX_CUSTOMER_ID_LEN)
    _validate_score(feedback.quality_score, "quality_score")
    _validate_score(feedback.communication_score, "communication_score")

    if not isinstance(feedback.on_time, bool):
        raise ReputationValidationError("on_time must be a boolean")
    if not isinstance(feedback.would_recommend, bool):
        raise ReputationValidationError("would_recommend must be a boolean")

    if feedback.comment is not None:
        if not isinstance(feedback.comment, str):
            raise ReputationValidationError("comment must be a string or None")
        if len(feedback.comment) > _MAX_COMMENT_LEN:
            raise ReputationValidationError(f"comment exceeds max length of {_MAX_COMMENT_LEN} characters")
        if _CONTROL_CHAR_RE.search(feedback.comment):
            raise ReputationValidationError("comment contains control characters")


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------


class ReputationEngine:
    """Manages operator profiles, order tracking, and feedback aggregation.

    Thread-safe.  All public methods acquire the internal lock.
    """

    def __init__(self) -> None:
        self._operators: dict[str, OperatorProfile] = {}
        self._feedback: list[OrderFeedback] = []
        self._events: list[ReputationEvent] = []
        self._lock = threading.Lock()

    # -- Registration ------------------------------------------------------

    def register_operator(
        self,
        operator_id: str,
        display_name: str,
    ) -> OperatorProfile:
        """Register a new operator.

        :param operator_id: Unique identifier for the operator.
        :param display_name: Human-readable name.
        :returns: The newly created :class:`OperatorProfile`.
        :raises ReputationValidationError: If inputs are invalid.
        :raises DuplicateOperatorError: If operator_id already exists.
        """
        _validate_id(operator_id, "operator_id", max_len=_MAX_OPERATOR_ID_LEN)
        _validate_display_name(display_name)

        with self._lock:
            if operator_id in self._operators:
                raise DuplicateOperatorError(operator_id)

            profile = OperatorProfile(
                operator_id=operator_id,
                display_name=display_name,
            )
            self._operators[operator_id] = profile
            logger.info("Registered operator %r (%s)", operator_id, display_name)
            return profile

    # -- Querying ----------------------------------------------------------

    def get_operator(self, operator_id: str) -> OperatorProfile | None:
        """Return the profile for *operator_id*, or ``None`` if not found."""
        with self._lock:
            return self._operators.get(operator_id)

    # -- Order tracking ----------------------------------------------------

    def record_order_completion(
        self,
        operator_id: str,
        *,
        success: bool,
        print_time_s: float,
    ) -> None:
        """Record a completed (or failed) order for an operator.

        :param operator_id: The operator who fulfilled the order.
        :param success: Whether the order completed successfully.
        :param print_time_s: Total print time in seconds.
        :raises OperatorNotFoundError: If the operator is not registered.
        :raises ReputationValidationError: If print_time_s is invalid.
        """
        if not isinstance(print_time_s, (int, float)):
            raise ReputationValidationError("print_time_s must be a number")
        if print_time_s < 0:
            raise ReputationValidationError("print_time_s must be >= 0")
        if not isinstance(success, bool):
            raise ReputationValidationError("success must be a boolean")

        with self._lock:
            profile = self._operators.get(operator_id)
            if profile is None:
                raise OperatorNotFoundError(operator_id)

            profile.total_orders += 1
            if success:
                profile.successful_orders += 1
            else:
                profile.failed_orders += 1

            # Rolling average for print time
            prev_total = profile.total_orders - 1
            if prev_total == 0:
                profile.avg_print_time_s = float(print_time_s)
            else:
                profile.avg_print_time_s = (profile.avg_print_time_s * prev_total + print_time_s) / profile.total_orders

            now = time.time()
            profile.last_active_at = now

            event_type = "order_completed" if success else "order_failed"
            self._events.append(
                ReputationEvent(
                    event_type=event_type,
                    operator_id=operator_id,
                    timestamp=now,
                    metadata={"print_time_s": print_time_s, "success": success},
                )
            )

    # -- Feedback ----------------------------------------------------------

    def submit_feedback(self, feedback: OrderFeedback) -> None:
        """Submit customer feedback for an order.

        :param feedback: The feedback to record.
        :raises ReputationValidationError: If any field is invalid.
        :raises OperatorNotFoundError: If the operator is not registered.
        """
        _validate_feedback(feedback)

        with self._lock:
            profile = self._operators.get(feedback.operator_id)
            if profile is None:
                raise OperatorNotFoundError(feedback.operator_id)

            self._feedback.append(feedback)

            # Update rolling average quality score
            op_feedback = [f for f in self._feedback if f.operator_id == feedback.operator_id]
            total_quality = sum(f.quality_score for f in op_feedback)
            profile.avg_quality_score = total_quality / len(op_feedback)

            now = time.time()
            profile.last_active_at = now

            self._events.append(
                ReputationEvent(
                    event_type="feedback_received",
                    operator_id=feedback.operator_id,
                    timestamp=now,
                    metadata={
                        "order_id": feedback.order_id,
                        "quality_score": feedback.quality_score,
                    },
                )
            )

    # -- Verification ------------------------------------------------------

    def verify_operator(self, operator_id: str) -> None:
        """Mark an operator as verified (admin action).

        :param operator_id: The operator to verify.
        :raises OperatorNotFoundError: If the operator is not registered.
        """
        with self._lock:
            profile = self._operators.get(operator_id)
            if profile is None:
                raise OperatorNotFoundError(operator_id)

            profile.verified = True

            self._events.append(
                ReputationEvent(
                    event_type="verification_granted",
                    operator_id=operator_id,
                    timestamp=time.time(),
                    metadata={},
                )
            )
            logger.info("Operator %r verified", operator_id)

    # -- Leaderboard & filtering -------------------------------------------

    def get_leaderboard(
        self,
        *,
        limit: int = 10,
        material: str | None = None,
    ) -> list[OperatorProfile]:
        """Return top operators sorted by reliability tier then success rate.

        :param limit: Maximum number of results.
        :param material: If provided, only include operators supporting
            this material.
        :returns: Sorted list of :class:`OperatorProfile`.
        """
        with self._lock:
            operators = list(self._operators.values())

        if material is not None:
            operators = [op for op in operators if material in op.materials_supported]

        # Sort by tier rank (lower index = better), then by success rate desc
        operators.sort(
            key=lambda op: (
                _TIER_ORDER.index(op.reliability_tier),
                -op.success_rate,
            ),
        )

        return operators[:limit]

    def get_operator_stats(self, operator_id: str) -> dict[str, Any]:
        """Return full stats and feedback summary for an operator.

        :param operator_id: The operator to query.
        :returns: Dict with profile data and feedback summary.
        :raises OperatorNotFoundError: If the operator is not registered.
        """
        with self._lock:
            profile = self._operators.get(operator_id)
            if profile is None:
                raise OperatorNotFoundError(operator_id)

            op_feedback = [f for f in self._feedback if f.operator_id == operator_id]

        feedback_count = len(op_feedback)
        avg_communication = 0.0
        recommend_rate = 0.0
        on_time_rate = 0.0

        if feedback_count > 0:
            avg_communication = sum(f.communication_score for f in op_feedback) / feedback_count
            recommend_rate = sum(1 for f in op_feedback if f.would_recommend) / feedback_count
            on_time_rate = sum(1 for f in op_feedback if f.on_time) / feedback_count

        return {
            **profile.to_dict(),
            "feedback_summary": {
                "feedback_count": feedback_count,
                "avg_communication_score": round(avg_communication, 2),
                "recommend_rate": round(recommend_rate, 4),
                "on_time_rate": round(on_time_rate, 4),
            },
        }

    def list_operators(
        self,
        *,
        verified_only: bool = False,
        min_tier: str = "new",
        material: str | None = None,
    ) -> list[OperatorProfile]:
        """Return a filtered list of operators.

        :param verified_only: If ``True``, only return verified operators.
        :param min_tier: Minimum reliability tier (inclusive).
        :param material: If provided, only include operators supporting
            this material.
        :returns: Filtered list of :class:`OperatorProfile`.
        """
        if min_tier not in _TIER_ORDER:
            raise ReputationValidationError(f"Invalid tier: {min_tier!r}; must be one of {_TIER_ORDER}")

        min_tier_index = _TIER_ORDER.index(min_tier)

        with self._lock:
            operators = list(self._operators.values())

        result: list[OperatorProfile] = []
        for op in operators:
            if verified_only and not op.verified:
                continue
            tier_index = _TIER_ORDER.index(op.reliability_tier)
            if tier_index > min_tier_index:
                continue
            if material is not None and material not in op.materials_supported:
                continue
            result.append(op)

        return result


# ---------------------------------------------------------------------------
# Module-level singleton (lazy, thread-safe)
# ---------------------------------------------------------------------------

_engine: ReputationEngine | None = None
_engine_lock = threading.Lock()


def get_reputation_engine() -> ReputationEngine:
    """Return the module-level :class:`ReputationEngine` singleton.

    Thread-safe; the instance is created on first call.
    """
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is None:
            _engine = ReputationEngine()
        return _engine
