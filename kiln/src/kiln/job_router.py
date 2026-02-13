"""Smart job routing engine — picks the best printer for each job.

Scores available printers based on material compatibility, availability,
reliability, speed, and cost.  Priority weights shift dynamically based
on the caller's quality/speed/cost preferences.

Usage::

    from kiln.job_router import get_job_router, RoutingCriteria

    router = get_job_router()
    result = router.route_job(
        RoutingCriteria(material="PLA"),
        available_printers=[{"printer_id": "voron", "printer_model": "Voron 2.4", ...}],
    )
    print(result.recommended_printer.printer_id)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

_MAX_MATERIAL_LEN = 50
_MIN_PRIORITY = 1
_MAX_PRIORITY = 5
_MAX_ALTERNATIVES = 4
_MAX_SCORE = 100.0

# Base category weights (must sum to 1.0)
_BASE_WEIGHT_MATERIAL = 0.30
_BASE_WEIGHT_AVAILABILITY = 0.25
_BASE_WEIGHT_RELIABILITY = 0.20
_BASE_WEIGHT_SPEED = 0.15
_BASE_WEIGHT_COST = 0.10

# How much each priority point shifts weight (additive per point above 3)
_PRIORITY_SHIFT = 0.03


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RoutingValidationError(ValueError):
    """Raised when routing criteria fail input validation."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RoutingCriteria:
    """Criteria for selecting the best printer for a job.

    :param material: Filament material type (e.g. ``"PLA"``, ``"ABS"``).
    :param file_hash: SHA-256 hex digest of the G-code file, if available.
    :param estimated_print_time_s: Estimated duration in seconds.
    :param quality_priority: 1-5 scale; higher boosts reliability + material weight.
    :param speed_priority: 1-5 scale; higher boosts availability + speed weight.
    :param cost_priority: 1-5 scale; higher boosts cost weight.
    :param max_distance_km: Maximum acceptable distance, if location matters.
    :param required_capabilities: Capabilities the printer must have
        (e.g. ``["multi_material", "enclosure"]``).
    """

    material: str
    file_hash: str | None = None
    estimated_print_time_s: float | None = None
    quality_priority: int = 3
    speed_priority: int = 3
    cost_priority: int = 3
    max_distance_km: float | None = None
    required_capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "material": self.material,
            "file_hash": self.file_hash,
            "estimated_print_time_s": self.estimated_print_time_s,
            "quality_priority": self.quality_priority,
            "speed_priority": self.speed_priority,
            "cost_priority": self.cost_priority,
            "max_distance_km": self.max_distance_km,
            "required_capabilities": list(self.required_capabilities),
        }


@dataclass
class PrinterScore:
    """Scored candidate for a routing decision.

    :param printer_id: Unique printer identifier.
    :param printer_model: Printer model name.
    :param score: Overall score from 0 to 100.
    :param breakdown: Per-category scores keyed by category name.
    :param available: Whether the printer is currently idle.
    :param estimated_wait_s: Estimated seconds until the printer is free.
    :param material_success_rate: Historical success rate for this material.
    :param distance_km: Distance to the printer, if known.
    """

    printer_id: str
    printer_model: str
    score: float
    breakdown: dict[str, float]
    available: bool
    estimated_wait_s: float
    material_success_rate: float | None = None
    distance_km: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "printer_id": self.printer_id,
            "printer_model": self.printer_model,
            "score": round(self.score, 2),
            "breakdown": {k: round(v, 2) for k, v in self.breakdown.items()},
            "available": self.available,
            "estimated_wait_s": round(self.estimated_wait_s, 2),
            "material_success_rate": (
                round(self.material_success_rate, 4)
                if self.material_success_rate is not None
                else None
            ),
            "distance_km": (
                round(self.distance_km, 2)
                if self.distance_km is not None
                else None
            ),
        }


@dataclass
class RoutingResult:
    """Result of a routing decision.

    :param recommended_printer: Highest-scoring candidate.
    :param alternatives: Up to 4 next-best candidates, sorted by score.
    :param criteria_used: The criteria that produced this result.
    :param routing_time_ms: Wall-clock time to compute the result.
    """

    recommended_printer: PrinterScore
    alternatives: list[PrinterScore]
    criteria_used: RoutingCriteria
    routing_time_ms: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "recommended_printer": self.recommended_printer.to_dict(),
            "alternatives": [a.to_dict() for a in self.alternatives],
            "criteria_used": self.criteria_used.to_dict(),
            "routing_time_ms": round(self.routing_time_ms, 2),
        }


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------


class JobRouter:
    """Scores and ranks printers for a given job based on weighted criteria.

    Optionally integrates with the cross-printer learning engine for
    material success-rate data.

    :param learning_engine: Optional :class:`CrossPrinterLearningEngine`.
        If ``None``, material scoring falls back to a binary
        supported/unsupported check.
    """

    def __init__(
        self,
        *,
        learning_engine: Any = None,
    ) -> None:
        self._learning_engine = learning_engine

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def route_job(
        self,
        criteria: RoutingCriteria,
        available_printers: list[dict[str, Any]],
    ) -> RoutingResult:
        """Score all printers and return the best match plus alternatives.

        :param criteria: Job requirements and priority weights.
        :param available_printers: List of printer info dicts.  Each dict
            must contain at least ``"printer_id"`` and ``"printer_model"``.
            Optional keys: ``"status"`` (str), ``"queue_depth"`` (int),
            ``"supported_materials"`` (list[str]),
            ``"capabilities"`` (list[str]), ``"success_rate"`` (float),
            ``"estimated_wait_s"`` (float), ``"cost_per_hour"`` (float),
            ``"distance_km"`` (float), ``"print_speed_factor"`` (float).
        :returns: :class:`RoutingResult` with the recommended printer.
        :raises RoutingValidationError: If inputs are invalid.
        """
        self._validate_criteria(criteria)
        self._validate_printers(available_printers)

        start = time.monotonic()

        # Filter printers that don't meet hard constraints
        candidates = self._filter_candidates(criteria, available_printers)

        if not candidates:
            raise RoutingValidationError(
                "No printers match the required capabilities and constraints"
            )

        # Score each candidate
        scores: list[PrinterScore] = []
        for printer_info in candidates:
            scores.append(self.score_printer(criteria, printer_info))

        # Sort descending by score, then by printer_id for determinism
        scores.sort(key=lambda s: (-s.score, s.printer_id))

        elapsed_ms = (time.monotonic() - start) * 1000.0

        return RoutingResult(
            recommended_printer=scores[0],
            alternatives=scores[1 : 1 + _MAX_ALTERNATIVES],
            criteria_used=criteria,
            routing_time_ms=elapsed_ms,
        )

    def score_printer(
        self,
        criteria: RoutingCriteria,
        printer_info: dict[str, Any],
    ) -> PrinterScore:
        """Compute the score for a single printer.

        Useful for debugging or presenting per-printer breakdowns.

        :param criteria: Job requirements and priority weights.
        :param printer_info: Printer info dict (see :meth:`route_job`).
        :returns: :class:`PrinterScore` with breakdown.
        """
        weights = self._compute_weights(criteria)

        material_score = self._score_material(criteria, printer_info)
        availability_score = self._score_availability(printer_info)
        reliability_score = self._score_reliability(printer_info)
        speed_score = self._score_speed(criteria, printer_info)
        cost_score = self._score_cost(printer_info)

        breakdown = {
            "material": material_score,
            "availability": availability_score,
            "reliability": reliability_score,
            "speed": speed_score,
            "cost": cost_score,
        }

        total = (
            weights["material"] * material_score
            + weights["availability"] * availability_score
            + weights["reliability"] * reliability_score
            + weights["speed"] * speed_score
            + weights["cost"] * cost_score
        )

        # Clamp to [0, 100]
        total = max(0.0, min(_MAX_SCORE, total))

        status = printer_info.get("status", "idle")
        available = status == "idle"
        estimated_wait = float(printer_info.get("estimated_wait_s", 0.0))

        # Pull material success rate from learning engine if available
        material_success_rate = self._get_material_success_rate(
            criteria.material, printer_info.get("printer_model", "")
        )
        # Fall back to printer-reported success_rate
        if material_success_rate is None:
            raw_rate = printer_info.get("success_rate")
            if raw_rate is not None:
                material_success_rate = float(raw_rate)

        return PrinterScore(
            printer_id=printer_info["printer_id"],
            printer_model=printer_info.get("printer_model", "unknown"),
            score=total,
            breakdown=breakdown,
            available=available,
            estimated_wait_s=estimated_wait,
            material_success_rate=material_success_rate,
            distance_km=printer_info.get("distance_km"),
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_criteria(criteria: RoutingCriteria) -> None:
        """Validate routing criteria fields.

        :raises RoutingValidationError: On invalid input.
        """
        if not criteria.material or not isinstance(criteria.material, str):
            raise RoutingValidationError("material must be a non-empty string")
        if len(criteria.material) > _MAX_MATERIAL_LEN:
            raise RoutingValidationError(
                f"material must be at most {_MAX_MATERIAL_LEN} characters"
            )

        for name, value in [
            ("quality_priority", criteria.quality_priority),
            ("speed_priority", criteria.speed_priority),
            ("cost_priority", criteria.cost_priority),
        ]:
            if not isinstance(value, int) or value < _MIN_PRIORITY or value > _MAX_PRIORITY:
                raise RoutingValidationError(
                    f"{name} must be an integer between "
                    f"{_MIN_PRIORITY} and {_MAX_PRIORITY}"
                )

        if criteria.max_distance_km is not None:
            if not isinstance(criteria.max_distance_km, (int, float)):
                raise RoutingValidationError("max_distance_km must be a number")
            if criteria.max_distance_km <= 0:
                raise RoutingValidationError("max_distance_km must be > 0")

    @staticmethod
    def _validate_printers(printers: list[dict[str, Any]]) -> None:
        """Validate the available printers list.

        :raises RoutingValidationError: On invalid input.
        """
        if not printers:
            raise RoutingValidationError("available_printers must be a non-empty list")
        for idx, p in enumerate(printers):
            if "printer_id" not in p:
                raise RoutingValidationError(
                    f"printer at index {idx} missing required key 'printer_id'"
                )

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _filter_candidates(
        self,
        criteria: RoutingCriteria,
        printers: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Remove printers that fail hard constraints."""
        candidates: list[dict[str, Any]] = []

        for p in printers:
            # Capability check
            if criteria.required_capabilities:
                printer_caps = set(p.get("capabilities", []))
                if not set(criteria.required_capabilities).issubset(printer_caps):
                    continue

            # Distance check
            if criteria.max_distance_km is not None:
                dist = p.get("distance_km")
                if dist is None or float(dist) > criteria.max_distance_km:
                    continue

            candidates.append(p)

        return candidates

    # ------------------------------------------------------------------
    # Weight computation
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_weights(criteria: RoutingCriteria) -> dict[str, float]:
        """Adjust base weights based on priority sliders.

        Each priority point above 3 adds ``_PRIORITY_SHIFT`` to the
        associated categories and subtracts proportionally from the rest.
        Points below 3 do the reverse.
        """
        w_material = _BASE_WEIGHT_MATERIAL
        w_availability = _BASE_WEIGHT_AVAILABILITY
        w_reliability = _BASE_WEIGHT_RELIABILITY
        w_speed = _BASE_WEIGHT_SPEED
        w_cost = _BASE_WEIGHT_COST

        # Quality priority shifts material + reliability
        q_delta = (criteria.quality_priority - 3) * _PRIORITY_SHIFT
        w_material += q_delta
        w_reliability += q_delta

        # Speed priority shifts availability + speed
        s_delta = (criteria.speed_priority - 3) * _PRIORITY_SHIFT
        w_availability += s_delta
        w_speed += s_delta

        # Cost priority shifts cost
        c_delta = (criteria.cost_priority - 3) * _PRIORITY_SHIFT
        w_cost += c_delta

        # Clamp all weights to a minimum of 0.01 to avoid zeroes
        weights = {
            "material": max(0.01, w_material),
            "availability": max(0.01, w_availability),
            "reliability": max(0.01, w_reliability),
            "speed": max(0.01, w_speed),
            "cost": max(0.01, w_cost),
        }

        # Normalize so they sum to 1.0
        total = sum(weights.values())
        return {k: v / total for k, v in weights.items()}

    # ------------------------------------------------------------------
    # Category scorers (each returns 0-100)
    # ------------------------------------------------------------------

    def _score_material(
        self,
        criteria: RoutingCriteria,
        printer_info: dict[str, Any],
    ) -> float:
        """Score based on material compatibility and success history.

        Returns 100 if the printer explicitly supports the material and has
        a high success rate, 0 if it doesn't support it at all.
        """
        supported = printer_info.get("supported_materials", [])

        # If no material list given, assume the printer supports everything
        if not supported:
            base = 70.0
        elif criteria.material in supported:
            base = 100.0
        else:
            return 0.0

        # Refine with learning engine data if available
        rate = self._get_material_success_rate(
            criteria.material, printer_info.get("printer_model", "")
        )
        if rate is not None:
            # Blend base compatibility with empirical success rate
            return base * 0.4 + (rate * 100.0) * 0.6

        return base

    def _get_material_success_rate(
        self,
        material: str,
        printer_model: str,
    ) -> float | None:
        """Query the learning engine for material success rate.

        Returns ``None`` if no learning engine or no data available.
        """
        if self._learning_engine is None or not printer_model:
            return None

        try:
            insight = self._learning_engine.get_material_insights(material)
            if insight.sample_count > 0:
                return insight.success_rate
        except Exception:
            logger.debug(
                "Learning engine query failed for %s/%s", material, printer_model
            )

        return None

    @staticmethod
    def _score_availability(printer_info: dict[str, Any]) -> float:
        """Score based on current status and queue depth.

        Idle printers with no queue get 100; busy printers with deep
        queues approach 0.
        """
        status = printer_info.get("status", "idle")
        queue_depth = int(printer_info.get("queue_depth", 0))

        if status == "idle":
            base = 100.0
        elif status == "printing":
            base = 50.0
        elif status == "busy":
            base = 30.0
        elif status == "error" or status == "offline":
            return 0.0
        else:
            base = 40.0

        # Penalize queue depth: -10 per queued job, floor at 0
        penalty = queue_depth * 10.0
        return max(0.0, base - penalty)

    @staticmethod
    def _score_reliability(printer_info: dict[str, Any]) -> float:
        """Score based on overall success rate.

        Falls back to 50 (neutral) when no data is available.
        """
        rate = printer_info.get("success_rate")
        if rate is None:
            return 50.0
        return float(rate) * 100.0

    @staticmethod
    def _score_speed(
        criteria: RoutingCriteria,
        printer_info: dict[str, Any],
    ) -> float:
        """Score based on estimated wait time and speed factor.

        Printers with lower wait times and higher speed factors score
        higher.
        """
        wait = float(printer_info.get("estimated_wait_s", 0.0))
        speed_factor = float(printer_info.get("print_speed_factor", 1.0))

        # Wait time penalty: lose 1 point per 60s of wait, cap at 50
        wait_penalty = min(50.0, wait / 60.0)

        # Speed bonus: speed_factor 1.0 = neutral (50), 2.0 = 100, 0.5 = 25
        speed_base = min(100.0, speed_factor * 50.0)

        return max(0.0, speed_base - wait_penalty)

    @staticmethod
    def _score_cost(printer_info: dict[str, Any]) -> float:
        """Score based on cost per hour.

        Lower cost = higher score.  Without data, returns a neutral 50.
        """
        cost = printer_info.get("cost_per_hour")
        if cost is None:
            return 50.0
        cost_f = float(cost)
        if cost_f <= 0:
            return 100.0
        # Inverse scoring: $1/hr = 100, $5/hr = 20, $10/hr = 10
        return min(100.0, max(0.0, 100.0 / cost_f))


# ---------------------------------------------------------------------------
# Module-level singleton (lazy, thread-safe)
# ---------------------------------------------------------------------------

_router: JobRouter | None = None
_router_lock = threading.Lock()


def get_job_router() -> JobRouter:
    """Return the module-level :class:`JobRouter` singleton.

    Thread-safe; the instance is created on first call.  Attempts to
    attach the cross-printer learning engine if available.
    """
    global _router
    if _router is not None:
        return _router
    with _router_lock:
        if _router is None:
            learning = _try_get_learning_engine()
            _router = JobRouter(learning_engine=learning)
        return _router


def _try_get_learning_engine() -> Any:
    """Attempt to import and return the learning engine singleton.

    Returns ``None`` if the module is unavailable or raises.
    """
    try:
        from kiln.cross_printer_learning import get_learning_engine

        return get_learning_engine()
    except Exception:
        logger.debug("Cross-printer learning engine not available")
        return None
