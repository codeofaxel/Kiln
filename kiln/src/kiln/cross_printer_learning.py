"""Cross-printer learning engine — aggregate print outcomes across a fleet
and surface material/printer insights to agents.

Collects validated :class:`PrintOutcome` records from every printer on the
network, detects statistical outliers, enforces rate limits, and computes
:class:`MaterialInsight` and :class:`PrinterModelInsight` summaries that
agents use to recommend optimal settings.

Usage::

    from kiln.cross_printer_learning import get_learning_engine

    engine = get_learning_engine()
    engine.record_outcome(PrintOutcome(...))
    insight = engine.get_material_insights("PLA")
"""

from __future__ import annotations

import collections
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

_MAX_PRINTER_MODEL_LEN = 100
_MAX_MATERIAL_LEN = 50
_MAX_FAILURE_MODE_LEN = 200
_MAX_HOTEND_TEMP = 500
_MAX_BED_TEMP = 200
_MAX_PRINT_TIME_S = 604800  # 7 days
_MAX_LAYER_COUNT = 100000
_SHA256_HEX_LEN = 64

_PRINTER_MODEL_RE = re.compile(r"^[A-Za-z0-9 _-]+$")
_MATERIAL_RE = re.compile(r"^[A-Za-z0-9 -]+$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# Control characters: U+0000..U+001F and U+007F..U+009F
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Rate limiting
_RATE_LIMIT_WINDOW_S = 60
_RATE_LIMIT_MAX = 100

# Outlier threshold (standard deviations)
_OUTLIER_STD_DEVS = 3.0

# Default max stored outcomes
_DEFAULT_MAX_OUTCOMES = 10000


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LearningValidationError(ValueError):
    """Raised when a :class:`PrintOutcome` fails input validation."""


class LearningRateLimitError(Exception):
    """Raised when outcome recording exceeds the per-model rate limit."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PrintOutcome:
    """A single print outcome record.

    :param printer_model: Printer model identifier.
    :param material: Filament material type (e.g. ``"PLA"``, ``"ABS"``).
    :param hotend_temp: Hotend temperature in Celsius.
    :param bed_temp: Bed temperature in Celsius.
    :param success: Whether the print completed successfully.
    :param failure_mode: Description of the failure, if any.
    :param print_time_s: Total print time in seconds.
    :param layer_count: Number of layers printed.
    :param file_hash: SHA-256 hex digest of the G-code file.
    :param is_outlier: Automatically set by the engine when temps are
        statistical outliers.  Callers should leave this as ``False``.
    """

    printer_model: str
    material: str
    hotend_temp: float
    bed_temp: float
    success: bool
    failure_mode: Optional[str]
    print_time_s: float
    layer_count: int
    file_hash: str
    is_outlier: bool = False
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "printer_model": self.printer_model,
            "material": self.material,
            "hotend_temp": self.hotend_temp,
            "bed_temp": self.bed_temp,
            "success": self.success,
            "failure_mode": self.failure_mode,
            "print_time_s": self.print_time_s,
            "layer_count": self.layer_count,
            "file_hash": self.file_hash,
            "is_outlier": self.is_outlier,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class MaterialInsight:
    """Aggregated insight for a specific material across all printers.

    :param material: Material identifier.
    :param recommended_hotend_temp_range: ``(min, max)`` hotend temp range.
    :param recommended_bed_temp_range: ``(min, max)`` bed temp range.
    :param success_rate: Fraction of successful prints (0.0 -- 1.0).
    :param sample_count: Number of non-outlier outcomes used.
    :param common_failures: Failure modes with occurrence counts, sorted
        descending by count.
    """

    material: str
    recommended_hotend_temp_range: Tuple[float, float]
    recommended_bed_temp_range: Tuple[float, float]
    success_rate: float
    sample_count: int
    common_failures: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "material": self.material,
            "recommended_hotend_temp_range": list(self.recommended_hotend_temp_range),
            "recommended_bed_temp_range": list(self.recommended_bed_temp_range),
            "success_rate": self.success_rate,
            "sample_count": self.sample_count,
            "common_failures": self.common_failures,
        }


@dataclass(frozen=True)
class PrinterModelInsight:
    """Aggregated insight for a specific printer model.

    :param printer_model: Printer model identifier.
    :param best_materials: Materials with the highest success rates.
    :param worst_materials: Materials with the lowest success rates.
    :param common_failures: Failure modes with occurrence counts.
    :param avg_success_rate: Average success rate across all materials.
    :param sample_count: Number of non-outlier outcomes used.
    """

    printer_model: str
    best_materials: List[str]
    worst_materials: List[str]
    common_failures: List[Dict[str, Any]]
    avg_success_rate: float
    sample_count: int

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "printer_model": self.printer_model,
            "best_materials": self.best_materials,
            "worst_materials": self.worst_materials,
            "common_failures": self.common_failures,
            "avg_success_rate": self.avg_success_rate,
            "sample_count": self.sample_count,
        }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_outcome(outcome: PrintOutcome) -> None:
    """Validate every field of a :class:`PrintOutcome`.

    :raises LearningValidationError: If any field is invalid.
    """
    # -- printer_model --
    if not isinstance(outcome.printer_model, str) or not outcome.printer_model.strip():
        raise LearningValidationError("printer_model must be a non-empty string")
    if len(outcome.printer_model) > _MAX_PRINTER_MODEL_LEN:
        raise LearningValidationError(
            "printer_model exceeds max length of "
            f"{_MAX_PRINTER_MODEL_LEN} characters"
        )
    if not _PRINTER_MODEL_RE.match(outcome.printer_model):
        raise LearningValidationError(
            "printer_model contains invalid characters; "
            "only alphanumeric, spaces, hyphens, and underscores are allowed"
        )

    # -- material --
    if not isinstance(outcome.material, str) or not outcome.material.strip():
        raise LearningValidationError("material must be a non-empty string")
    if len(outcome.material) > _MAX_MATERIAL_LEN:
        raise LearningValidationError(
            f"material exceeds max length of {_MAX_MATERIAL_LEN} characters"
        )
    if not _MATERIAL_RE.match(outcome.material):
        raise LearningValidationError(
            "material contains invalid characters; "
            "only alphanumeric, spaces, and hyphens are allowed"
        )

    # -- hotend_temp --
    if not isinstance(outcome.hotend_temp, (int, float)):
        raise LearningValidationError("hotend_temp must be a number")
    if outcome.hotend_temp < 0 or outcome.hotend_temp > _MAX_HOTEND_TEMP:
        raise LearningValidationError(
            f"hotend_temp must be between 0 and {_MAX_HOTEND_TEMP}"
        )

    # -- bed_temp --
    if not isinstance(outcome.bed_temp, (int, float)):
        raise LearningValidationError("bed_temp must be a number")
    if outcome.bed_temp < 0 or outcome.bed_temp > _MAX_BED_TEMP:
        raise LearningValidationError(
            f"bed_temp must be between 0 and {_MAX_BED_TEMP}"
        )

    # -- success --
    if not isinstance(outcome.success, bool):
        raise LearningValidationError("success must be a boolean")

    # -- failure_mode --
    if outcome.failure_mode is not None:
        if not isinstance(outcome.failure_mode, str):
            raise LearningValidationError("failure_mode must be a string or None")
        if len(outcome.failure_mode) > _MAX_FAILURE_MODE_LEN:
            raise LearningValidationError(
                f"failure_mode exceeds max length of {_MAX_FAILURE_MODE_LEN} characters"
            )
        if _CONTROL_CHAR_RE.search(outcome.failure_mode):
            raise LearningValidationError(
                "failure_mode contains control characters"
            )

    # -- print_time_s --
    if not isinstance(outcome.print_time_s, (int, float)):
        raise LearningValidationError("print_time_s must be a number")
    if outcome.print_time_s < 0:
        raise LearningValidationError("print_time_s must be >= 0")
    if outcome.print_time_s > _MAX_PRINT_TIME_S:
        raise LearningValidationError(
            f"print_time_s exceeds maximum of {_MAX_PRINT_TIME_S} seconds (7 days)"
        )

    # -- layer_count --
    if not isinstance(outcome.layer_count, int):
        raise LearningValidationError("layer_count must be an integer")
    if outcome.layer_count < 0:
        raise LearningValidationError("layer_count must be >= 0")
    if outcome.layer_count > _MAX_LAYER_COUNT:
        raise LearningValidationError(
            f"layer_count exceeds maximum of {_MAX_LAYER_COUNT}"
        )

    # -- file_hash --
    if not isinstance(outcome.file_hash, str):
        raise LearningValidationError("file_hash must be a string")
    if not _HEX_RE.match(outcome.file_hash):
        raise LearningValidationError(
            "file_hash must be exactly 64 hexadecimal characters (SHA-256)"
        )


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


def _mean(values: List[float]) -> float:
    """Return the arithmetic mean of *values*, or 0.0 if empty."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def _std_dev(values: List[float]) -> float:
    """Return the population standard deviation of *values*."""
    if len(values) < 2:
        return 0.0
    avg = _mean(values)
    variance = sum((v - avg) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------


class CrossPrinterLearningEngine:
    """Aggregates print outcomes across a printer fleet and surfaces insights.

    Thread-safe.  All public methods acquire the internal lock.

    :param max_outcomes: Maximum number of stored outcomes.  When exceeded
        the oldest records are evicted.  Defaults to ``10000`` or the value
        of the ``KILN_LEARNING_MAX_OUTCOMES`` environment variable.
    """

    def __init__(self, *, max_outcomes: Optional[int] = None) -> None:
        env_max = os.environ.get("KILN_LEARNING_MAX_OUTCOMES")
        if max_outcomes is not None:
            self._max_outcomes = max_outcomes
        elif env_max is not None:
            try:
                self._max_outcomes = int(env_max)
            except ValueError:
                logger.warning(
                    "Invalid KILN_LEARNING_MAX_OUTCOMES=%r, using default %d",
                    env_max,
                    _DEFAULT_MAX_OUTCOMES,
                )
                self._max_outcomes = _DEFAULT_MAX_OUTCOMES
        else:
            self._max_outcomes = _DEFAULT_MAX_OUTCOMES

        self._outcomes: List[PrintOutcome] = []
        self._lock = threading.Lock()

        # Rate limiting: printer_model -> list of timestamps
        self._rate_buckets: Dict[str, List[float]] = {}

    # -- Recording ---------------------------------------------------------

    def record_outcome(self, outcome: PrintOutcome) -> None:
        """Validate and store a print outcome.

        :param outcome: The outcome to record.
        :raises LearningValidationError: If any field is invalid.
        :raises LearningRateLimitError: If the per-model rate limit is
            exceeded (100 outcomes/minute).
        """
        _validate_outcome(outcome)

        with self._lock:
            self._check_rate_limit(outcome.printer_model)
            self._mark_outlier_if_needed(outcome)

            self._outcomes.append(outcome)

            # Evict oldest if over capacity
            if len(self._outcomes) > self._max_outcomes:
                overage = len(self._outcomes) - self._max_outcomes
                self._outcomes = self._outcomes[overage:]

            self._record_rate_event(outcome.printer_model)

    def _check_rate_limit(self, printer_model: str) -> None:
        """Enforce rate limiting.  Must be called with *_lock* held."""
        now = time.time()
        bucket = self._rate_buckets.get(printer_model, [])

        # Prune timestamps outside the window
        cutoff = now - _RATE_LIMIT_WINDOW_S
        bucket = [ts for ts in bucket if ts > cutoff]
        self._rate_buckets[printer_model] = bucket

        if len(bucket) >= _RATE_LIMIT_MAX:
            raise LearningRateLimitError(
                f"Rate limit exceeded for printer_model '{printer_model}': "
                f"max {_RATE_LIMIT_MAX} outcomes per {_RATE_LIMIT_WINDOW_S}s"
            )

    def _record_rate_event(self, printer_model: str) -> None:
        """Record a timestamp for rate limiting.  Must be called with *_lock* held."""
        now = time.time()
        if printer_model not in self._rate_buckets:
            self._rate_buckets[printer_model] = []
        self._rate_buckets[printer_model].append(now)

    def _mark_outlier_if_needed(self, outcome: PrintOutcome) -> None:
        """Flag outcome as outlier if temps deviate 3+ stddevs.

        Must be called with *_lock* held.
        """
        material_outcomes = [
            o for o in self._outcomes
            if o.material == outcome.material and not o.is_outlier
        ]

        if len(material_outcomes) < 5:
            # Not enough data to compute meaningful statistics
            return

        hotend_temps = [o.hotend_temp for o in material_outcomes]
        bed_temps = [o.bed_temp for o in material_outcomes]

        hotend_mean = _mean(hotend_temps)
        hotend_sd = _std_dev(hotend_temps)
        bed_mean = _mean(bed_temps)
        bed_sd = _std_dev(bed_temps)

        is_outlier = False
        # When stddev is 0, all existing values are identical.  Use a
        # minimum absolute deviation threshold to avoid false positives
        # from tiny jitter while still catching extreme values.
        _MIN_HOTEND_DEVIATION = 20.0  # degrees C
        _MIN_BED_DEVIATION = 15.0

        if hotend_sd > 0:
            if abs(outcome.hotend_temp - hotend_mean) > _OUTLIER_STD_DEVS * hotend_sd:
                is_outlier = True
        elif abs(outcome.hotend_temp - hotend_mean) > _MIN_HOTEND_DEVIATION:
            is_outlier = True

        if bed_sd > 0:
            if abs(outcome.bed_temp - bed_mean) > _OUTLIER_STD_DEVS * bed_sd:
                is_outlier = True
        elif abs(outcome.bed_temp - bed_mean) > _MIN_BED_DEVIATION:
            is_outlier = True

        if is_outlier:
            # Mutate before appending to the list
            object.__setattr__(outcome, "is_outlier", True)
            logger.info(
                "Outlier detected: %s/%s hotend=%.1f bed=%.1f",
                outcome.printer_model,
                outcome.material,
                outcome.hotend_temp,
                outcome.bed_temp,
            )

    # -- Querying ----------------------------------------------------------

    def _non_outlier_outcomes(self) -> List[PrintOutcome]:
        """Return all stored outcomes that are not outliers.

        Must be called with *_lock* held.
        """
        return [o for o in self._outcomes if not o.is_outlier]

    def get_material_insights(self, material: str) -> MaterialInsight:
        """Aggregate insights for *material* across all printers.

        :param material: Material identifier (case-sensitive).
        :returns: Aggregated :class:`MaterialInsight`.
        :raises LearningValidationError: If *material* is empty.
        """
        if not material or not isinstance(material, str):
            raise LearningValidationError("material must be a non-empty string")

        with self._lock:
            relevant = [
                o for o in self._non_outlier_outcomes()
                if o.material == material
            ]

        if not relevant:
            return MaterialInsight(
                material=material,
                recommended_hotend_temp_range=(0.0, 0.0),
                recommended_bed_temp_range=(0.0, 0.0),
                success_rate=0.0,
                sample_count=0,
                common_failures=[],
            )

        hotend_temps = [o.hotend_temp for o in relevant if o.success]
        bed_temps = [o.bed_temp for o in relevant if o.success]

        if hotend_temps:
            hotend_range = (min(hotend_temps), max(hotend_temps))
        else:
            hotend_range = (0.0, 0.0)

        if bed_temps:
            bed_range = (min(bed_temps), max(bed_temps))
        else:
            bed_range = (0.0, 0.0)

        successes = sum(1 for o in relevant if o.success)
        success_rate = successes / len(relevant)

        failure_counter: collections.Counter[str] = collections.Counter()
        for o in relevant:
            if not o.success and o.failure_mode:
                failure_counter[o.failure_mode] += 1

        common_failures = [
            {"failure_mode": mode, "count": count}
            for mode, count in failure_counter.most_common()
        ]

        return MaterialInsight(
            material=material,
            recommended_hotend_temp_range=hotend_range,
            recommended_bed_temp_range=bed_range,
            success_rate=round(success_rate, 4),
            sample_count=len(relevant),
            common_failures=common_failures,
        )

    def get_printer_insights(self, printer_model: str) -> PrinterModelInsight:
        """Aggregate insights for *printer_model*.

        :param printer_model: Printer model identifier (case-sensitive).
        :returns: Aggregated :class:`PrinterModelInsight`.
        :raises LearningValidationError: If *printer_model* is empty.
        """
        if not printer_model or not isinstance(printer_model, str):
            raise LearningValidationError("printer_model must be a non-empty string")

        with self._lock:
            relevant = [
                o for o in self._non_outlier_outcomes()
                if o.printer_model == printer_model
            ]

        if not relevant:
            return PrinterModelInsight(
                printer_model=printer_model,
                best_materials=[],
                worst_materials=[],
                common_failures=[],
                avg_success_rate=0.0,
                sample_count=0,
            )

        # Per-material success rates
        material_stats: Dict[str, Dict[str, int]] = {}
        for o in relevant:
            if o.material not in material_stats:
                material_stats[o.material] = {"success": 0, "total": 0}
            material_stats[o.material]["total"] += 1
            if o.success:
                material_stats[o.material]["success"] += 1

        material_rates = {
            mat: stats["success"] / stats["total"]
            for mat, stats in material_stats.items()
            if stats["total"] > 0
        }

        sorted_materials = sorted(
            material_rates.items(), key=lambda x: x[1], reverse=True
        )

        best_materials = [mat for mat, _ in sorted_materials[:3]]
        worst_materials = [mat for mat, _ in sorted_materials[-3:] if material_rates[mat] < 1.0]

        # Overall success rate
        successes = sum(1 for o in relevant if o.success)
        avg_success_rate = successes / len(relevant)

        # Common failures
        failure_counter: collections.Counter[str] = collections.Counter()
        for o in relevant:
            if not o.success and o.failure_mode:
                failure_counter[o.failure_mode] += 1

        common_failures = [
            {"failure_mode": mode, "count": count}
            for mode, count in failure_counter.most_common()
        ]

        return PrinterModelInsight(
            printer_model=printer_model,
            best_materials=best_materials,
            worst_materials=worst_materials,
            common_failures=common_failures,
            avg_success_rate=round(avg_success_rate, 4),
            sample_count=len(relevant),
        )

    def get_recommendation(
        self,
        printer_model: str,
        material: str,
    ) -> Dict[str, Any]:
        """Recommend print settings for a printer/material combination.

        Returns recommended temperatures based on successful non-outlier
        prints, plus a confidence level based on sample size.

        :param printer_model: Printer model identifier.
        :param material: Filament material type.
        :returns: Dict with ``recommended_hotend_temp``,
            ``recommended_bed_temp``, ``confidence``, ``sample_count``,
            and ``success_rate``.
        """
        if not printer_model or not isinstance(printer_model, str):
            raise LearningValidationError("printer_model must be a non-empty string")
        if not material or not isinstance(material, str):
            raise LearningValidationError("material must be a non-empty string")

        with self._lock:
            # Prefer outcomes for this specific printer+material combo
            specific = [
                o for o in self._non_outlier_outcomes()
                if o.printer_model == printer_model
                and o.material == material
                and o.success
            ]

            # Fall back to all printers for this material
            all_material = [
                o for o in self._non_outlier_outcomes()
                if o.material == material
                and o.success
            ]

            # For success rate, include failures too (non-outlier)
            all_for_rate = [
                o for o in self._non_outlier_outcomes()
                if o.printer_model == printer_model
                and o.material == material
            ]

        # Use specific data if we have enough, otherwise fall back
        if len(specific) >= 3:
            source = specific
            confidence = "high" if len(specific) >= 10 else "medium"
        elif all_material:
            source = all_material
            confidence = "low"
        else:
            return {
                "recommended_hotend_temp": None,
                "recommended_bed_temp": None,
                "confidence": "none",
                "sample_count": 0,
                "success_rate": 0.0,
            }

        hotend_temps = [o.hotend_temp for o in source]
        bed_temps = [o.bed_temp for o in source]

        # Use median for robustness
        recommended_hotend = _median(hotend_temps)
        recommended_bed = _median(bed_temps)

        success_rate = 0.0
        if all_for_rate:
            successes = sum(1 for o in all_for_rate if o.success)
            success_rate = round(successes / len(all_for_rate), 4)

        return {
            "recommended_hotend_temp": round(recommended_hotend, 1),
            "recommended_bed_temp": round(recommended_bed, 1),
            "confidence": confidence,
            "sample_count": len(source),
            "success_rate": success_rate,
        }

    def get_network_stats(self) -> Dict[str, Any]:
        """Return aggregate statistics for the entire printer network.

        :returns: Dict with ``total_outcomes``, ``unique_printers``,
            ``unique_materials``, and ``overall_success_rate``.
        """
        with self._lock:
            total = len(self._outcomes)
            if total == 0:
                return {
                    "total_outcomes": 0,
                    "unique_printers": 0,
                    "unique_materials": 0,
                    "overall_success_rate": 0.0,
                }

            printers = {o.printer_model for o in self._outcomes}
            materials = {o.material for o in self._outcomes}
            successes = sum(1 for o in self._outcomes if o.success)

            return {
                "total_outcomes": total,
                "unique_printers": len(printers),
                "unique_materials": len(materials),
                "overall_success_rate": round(successes / total, 4),
            }


# ---------------------------------------------------------------------------
# Additional statistics helpers
# ---------------------------------------------------------------------------


def _median(values: List[float]) -> float:
    """Return the median of *values*.  Returns 0.0 if empty."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 0:
        return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0
    return sorted_vals[mid]


# ---------------------------------------------------------------------------
# Module-level singleton (lazy, thread-safe)
# ---------------------------------------------------------------------------

_engine: Optional[CrossPrinterLearningEngine] = None
_engine_lock = threading.Lock()


def get_learning_engine() -> CrossPrinterLearningEngine:
    """Return the module-level :class:`CrossPrinterLearningEngine` singleton.

    Thread-safe; the instance is created on first call.
    """
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is None:
            _engine = CrossPrinterLearningEngine()
        return _engine
