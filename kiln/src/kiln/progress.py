"""Print progress estimation for FDM 3D printing operations.

Provides time-remaining estimates for active print jobs on FDM printers.
FDM printing has four distinct phases; the estimator calculates per-phase
durations and an overall completion timestamp.

Historical actuals are recorded to improve future estimates via a simple
moving average.

Example::

    estimator = get_estimator()
    est = estimator.estimate_print(
        layer_count=300,
        layer_height_mm=0.2,
        filament_length_mm=5000.0,
        print_speed_mm_s=60.0,
        current_layer=75,
    )
    print(est.overall_progress_pct, est.estimated_completion)
"""

from __future__ import annotations

import enum
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PrintPhase(enum.Enum):
    """Manufacturing phase within an FDM print job.

    FDM phase sequence: preparing -> printing -> cooling -> post_processing -> complete

    - **PREPARING**: Bed heating, nozzle heating, auto-leveling, homing.
    - **PRINTING**: Layer-by-layer filament deposition (the main phase).
    - **COOLING**: Part cooling on the bed, nozzle cooldown.
    - **POST_PROCESSING**: Part removal, bed cleanup, filament retraction.
    - **COMPLETE**: Job finished.
    """

    PREPARING = "preparing"
    PRINTING = "printing"
    COOLING = "cooling"
    POST_PROCESSING = "post_processing"
    COMPLETE = "complete"


# ---------------------------------------------------------------------------
# Phase sequence
# ---------------------------------------------------------------------------

_FDM_PHASES: list[PrintPhase] = [
    PrintPhase.PREPARING,
    PrintPhase.PRINTING,
    PrintPhase.COOLING,
    PrintPhase.POST_PROCESSING,
    PrintPhase.COMPLETE,
]


# ---------------------------------------------------------------------------
# Default phase-weight fractions (used when no layer/speed data available)
# ---------------------------------------------------------------------------
# PREPARING  ~4%   of total time (heating + leveling)
# PRINTING   ~92%  of total time (actual deposition)
# COOLING    ~2.5% of total time (bed/part cool-down)
# POST_PROC  ~1.5% of total time (part removal, cleanup)

_DEFAULT_WEIGHT_PREPARING = 0.04
_DEFAULT_WEIGHT_PRINTING = 0.92
_DEFAULT_WEIGHT_COOLING = 0.025
_DEFAULT_WEIGHT_POST_PROCESSING = 0.015


# ---------------------------------------------------------------------------
# FDM timing defaults (seconds)
# ---------------------------------------------------------------------------

# Preparing: bed heat (~45s) + nozzle heat (~20s) + auto-level (~30s) + homing (~5s)
_FDM_PREPARE_S = 100.0

# Per-layer overhead: travel moves, retraction, z-hop, wipe (~1.5s per layer)
_FDM_PER_LAYER_OVERHEAD_S = 1.5

# Cooling: part + bed cooldown to safe removal temperature
_FDM_COOL_S = 90.0

# Post-processing: part removal, bed scraping, cleanup
_FDM_POST_PROCESS_S = 60.0

# First-layer speed multiplier (first layer is typically printed at 50% speed)
_FIRST_LAYER_SPEED_FACTOR = 0.5

# Infill vs perimeter speed blend — most prints spend ~60% time on
# infill at higher speed and ~40% on perimeters at lower speed.
# This factor adjusts the average effective speed down from the nominal.
_EFFECTIVE_SPEED_FACTOR = 0.75


# ---------------------------------------------------------------------------
# Moving-average window
# ---------------------------------------------------------------------------

_HISTORY_WINDOW = 20


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PhaseEstimate:
    """Estimated timing for a single FDM print phase.

    :param phase: The print phase.
    :param estimated_duration_s: Expected wall-clock duration in seconds.
    :param elapsed_s: Seconds already spent in this phase.
    :param progress_pct: Completion percentage (0.0--100.0).
    """

    phase: PrintPhase
    estimated_duration_s: float
    elapsed_s: float
    progress_pct: float  # 0.0 -- 100.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        data["phase"] = self.phase.value
        return data


@dataclass
class PrintProgressEstimate:
    """Full progress estimate for an active FDM print job.

    :param job_id: Unique job identifier.
    :param printer_type: Printer category (``"fdm"``).
    :param current_phase: The phase the job is currently in.
    :param phases: Per-phase timing breakdown.
    :param total_estimated_s: Total estimated duration in seconds.
    :param total_elapsed_s: Total seconds elapsed so far.
    :param overall_progress_pct: Overall completion percentage (0.0--100.0).
    :param estimated_completion: Unix timestamp of expected completion.
    :param confidence: Estimate confidence (0.0--1.0); lower early in a job.
    :param current_layer: Current layer being printed (if available).
    :param total_layers: Total layer count (if available).
    """

    job_id: str
    printer_type: str
    current_phase: PrintPhase
    phases: list[PhaseEstimate]
    total_estimated_s: float
    total_elapsed_s: float
    overall_progress_pct: float  # 0.0 -- 100.0
    estimated_completion: float  # unix timestamp
    confidence: float  # 0.0 -- 1.0
    current_layer: Optional[int] = None
    total_layers: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        result: dict[str, Any] = {
            "job_id": self.job_id,
            "printer_type": self.printer_type,
            "current_phase": self.current_phase.value,
            "phases": [p.to_dict() for p in self.phases],
            "total_estimated_s": self.total_estimated_s,
            "total_elapsed_s": self.total_elapsed_s,
            "overall_progress_pct": self.overall_progress_pct,
            "estimated_completion": self.estimated_completion,
            "confidence": self.confidence,
        }
        if self.current_layer is not None:
            result["current_layer"] = self.current_layer
        if self.total_layers is not None:
            result["total_layers"] = self.total_layers
        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_phases(
    *,
    phase_durations: dict[PrintPhase, float],
    elapsed_s: float,
) -> tuple[list[PhaseEstimate], PrintPhase, float]:
    """Build phase estimates and determine current phase.

    Walks through the FDM phase sequence, allocating elapsed time to each
    phase in order.  The first phase not fully consumed by elapsed time is
    marked as the current phase.

    :returns: ``(phase_estimates, current_phase, overall_progress_pct)``
    """
    phase_estimates: list[PhaseEstimate] = []
    remaining_elapsed = elapsed_s
    current_phase = _FDM_PHASES[0]
    total_duration = sum(phase_durations.get(p, 0.0) for p in _FDM_PHASES)
    found_current = False

    for phase in _FDM_PHASES:
        duration = phase_durations.get(phase, 0.0)

        if found_current:
            # Past current phase — remaining phases not started
            phase_estimates.append(PhaseEstimate(
                phase=phase,
                estimated_duration_s=duration,
                elapsed_s=0.0,
                progress_pct=0.0,
            ))
            continue

        if duration > 0 and remaining_elapsed >= duration:
            # Phase fully complete
            phase_estimates.append(PhaseEstimate(
                phase=phase,
                estimated_duration_s=duration,
                elapsed_s=duration,
                progress_pct=100.0,
            ))
            remaining_elapsed -= duration
            current_phase = phase
        elif duration > 0 and remaining_elapsed < duration:
            # Currently in this phase (partially complete)
            pct = remaining_elapsed / duration * 100.0
            phase_estimates.append(PhaseEstimate(
                phase=phase,
                estimated_duration_s=duration,
                elapsed_s=remaining_elapsed,
                progress_pct=round(pct, 2),
            ))
            current_phase = phase
            remaining_elapsed = 0.0
            found_current = True
        else:
            # Zero-duration phase — mark complete if we haven't found current
            if not found_current and remaining_elapsed >= 0 and phase != _FDM_PHASES[-1]:
                phase_estimates.append(PhaseEstimate(
                    phase=phase,
                    estimated_duration_s=0.0,
                    elapsed_s=0.0,
                    progress_pct=100.0,
                ))
                current_phase = phase
            else:
                phase_estimates.append(PhaseEstimate(
                    phase=phase,
                    estimated_duration_s=0.0,
                    elapsed_s=0.0,
                    progress_pct=0.0,
                ))

    overall_pct = (elapsed_s / total_duration * 100.0) if total_duration > 0 else 0.0
    overall_pct = min(overall_pct, 100.0)

    return phase_estimates, current_phase, round(overall_pct, 2)


def _estimate_printing_duration(
    *,
    layer_count: int,
    filament_length_mm: float,
    print_speed_mm_s: float,
) -> float:
    """Estimate the wall-clock duration of the PRINTING phase.

    Combines a speed-based extrusion time estimate with per-layer overhead
    for travel moves, retractions, and z-hops.

    :param layer_count: Total number of layers.
    :param filament_length_mm: Total filament to extrude in mm.
    :param print_speed_mm_s: Nominal print speed in mm/s.
    :returns: Estimated printing duration in seconds.
    """
    if print_speed_mm_s <= 0 or filament_length_mm <= 0:
        return 0.0

    # Effective speed accounts for perimeter/infill blend and first layer
    effective_speed = print_speed_mm_s * _EFFECTIVE_SPEED_FACTOR

    # Extrusion time
    extrusion_s = filament_length_mm / effective_speed

    # First layer penalty: extra time for the slower first layer
    if layer_count > 0:
        first_layer_extra = (extrusion_s / max(layer_count, 1)) * (
            (1.0 / _FIRST_LAYER_SPEED_FACTOR) - 1.0
        )
    else:
        first_layer_extra = 0.0

    # Per-layer overhead (travel, retraction, z-hop)
    layer_overhead = _FDM_PER_LAYER_OVERHEAD_S * max(layer_count, 0)

    return extrusion_s + first_layer_extra + layer_overhead


# ---------------------------------------------------------------------------
# ProgressEstimator
# ---------------------------------------------------------------------------

class ProgressEstimator:
    """Estimates print progress and time-remaining for FDM print jobs.

    Stores historical actual durations to improve future estimates using a
    simple moving average.  Each printer model can accumulate its own
    history bucket for more accurate per-printer calibration.
    """

    def __init__(self) -> None:
        # Keyed by printer model (or "fdm" as default): list of (estimated_s, actual_s)
        self._history: dict[str, list[tuple[float, float]]] = defaultdict(list)
        # Keyed by job_id: estimated total duration
        self._estimates: dict[str, float] = {}

    # -- FDM estimation -----------------------------------------------------

    def estimate_print(
        self,
        *,
        layer_count: int,
        layer_height_mm: float,
        filament_length_mm: float,
        print_speed_mm_s: float,
        current_layer: int = 0,
        printer_model: str = "fdm",
        job_id: Optional[str] = None,
    ) -> PrintProgressEstimate:
        """Estimate progress for an FDM print job.

        :param layer_count: Total number of layers in the print.
        :param layer_height_mm: Layer height in millimetres.
        :param filament_length_mm: Total filament length to extrude in mm.
        :param print_speed_mm_s: Nominal print speed in mm/s.
        :param current_layer: Layer currently being printed (0 = not started).
        :param printer_model: Printer model for per-printer history (default ``"fdm"``).
        :param job_id: Optional job identifier; auto-generated if not provided.
        :returns: Populated :class:`PrintProgressEstimate`.
        """
        jid = job_id or f"fdm-{id(self):x}-{int(time.time())}"

        printing_s = _estimate_printing_duration(
            layer_count=layer_count,
            filament_length_mm=filament_length_mm,
            print_speed_mm_s=print_speed_mm_s,
        )

        phase_durations: dict[PrintPhase, float] = {
            PrintPhase.PREPARING: _FDM_PREPARE_S,
            PrintPhase.PRINTING: printing_s,
            PrintPhase.COOLING: _FDM_COOL_S,
            PrintPhase.POST_PROCESSING: _FDM_POST_PROCESS_S,
            PrintPhase.COMPLETE: 0.0,
        }

        total_estimated = sum(phase_durations.values())

        # Elapsed: once printing has started, preparation is done;
        # progress is proportional to current_layer / layer_count
        if current_layer > 0 and layer_count > 0:
            layer_fraction = min(current_layer / layer_count, 1.0)
            elapsed = _FDM_PREPARE_S + (printing_s * layer_fraction)
        else:
            elapsed = 0.0

        # Apply historical correction factor
        history_key = printer_model.lower()
        correction = self._correction_factor(history_key)
        total_estimated *= correction

        phases, current_phase, overall_pct = _build_phases(
            phase_durations=phase_durations,
            elapsed_s=elapsed,
        )

        confidence = self._compute_confidence(
            elapsed_s=elapsed,
            total_s=total_estimated,
            history_key=history_key,
        )

        remaining_s = max(total_estimated - elapsed, 0.0)
        completion_ts = time.time() + remaining_s

        self._estimates[jid] = total_estimated

        return PrintProgressEstimate(
            job_id=jid,
            printer_type="fdm",
            current_phase=current_phase,
            phases=phases,
            total_estimated_s=round(total_estimated, 2),
            total_elapsed_s=round(elapsed, 2),
            overall_progress_pct=overall_pct,
            estimated_completion=round(completion_ts, 2),
            confidence=round(confidence, 4),
            current_layer=current_layer,
            total_layers=layer_count,
        )

    def estimate_from_progress(
        self,
        *,
        progress_pct: float,
        elapsed_s: float,
        printer_model: str = "fdm",
        job_id: Optional[str] = None,
    ) -> PrintProgressEstimate:
        """Estimate completion from a raw progress percentage and elapsed time.

        Useful when layer-level data is unavailable and the printer only
        reports overall progress (e.g., some Bambu Lab firmware builds).

        :param progress_pct: Reported progress (0.0--100.0).
        :param elapsed_s: Wall-clock seconds elapsed since job start.
        :param printer_model: Printer model for per-printer history.
        :param job_id: Optional job identifier.
        :returns: Populated :class:`PrintProgressEstimate`.
        """
        jid = job_id or f"fdm-{id(self):x}-{int(time.time())}"

        # Extrapolate total from elapsed + progress
        progress_clamped = max(min(progress_pct, 100.0), 0.0)
        if progress_clamped > 0:
            total_estimated = (elapsed_s / progress_clamped) * 100.0
        else:
            total_estimated = 0.0

        # Split into phases using default weight fractions
        printing_s = total_estimated * _DEFAULT_WEIGHT_PRINTING
        prepare_s = total_estimated * _DEFAULT_WEIGHT_PREPARING
        cool_s = total_estimated * _DEFAULT_WEIGHT_COOLING
        post_s = total_estimated * _DEFAULT_WEIGHT_POST_PROCESSING

        phase_durations: dict[PrintPhase, float] = {
            PrintPhase.PREPARING: prepare_s,
            PrintPhase.PRINTING: printing_s,
            PrintPhase.COOLING: cool_s,
            PrintPhase.POST_PROCESSING: post_s,
            PrintPhase.COMPLETE: 0.0,
        }

        # Apply historical correction
        history_key = printer_model.lower()
        correction = self._correction_factor(history_key)
        total_estimated *= correction

        phases, current_phase, overall_pct = _build_phases(
            phase_durations=phase_durations,
            elapsed_s=elapsed_s,
        )
        # Override overall_pct with the printer-reported value when available
        if progress_clamped > 0:
            overall_pct = round(progress_clamped, 2)

        confidence = self._compute_confidence(
            elapsed_s=elapsed_s,
            total_s=total_estimated,
            history_key=history_key,
        )

        remaining_s = max(total_estimated - elapsed_s, 0.0)
        completion_ts = time.time() + remaining_s

        self._estimates[jid] = total_estimated

        return PrintProgressEstimate(
            job_id=jid,
            printer_type="fdm",
            current_phase=current_phase,
            phases=phases,
            total_estimated_s=round(total_estimated, 2),
            total_elapsed_s=round(elapsed_s, 2),
            overall_progress_pct=overall_pct,
            estimated_completion=round(completion_ts, 2),
            confidence=round(confidence, 4),
        )

    # -- Accuracy tracking --------------------------------------------------

    def record_actual(
        self,
        job_id: str,
        actual_duration_s: float,
        *,
        printer_model: str = "fdm",
    ) -> None:
        """Record the actual duration of a completed print job.

        Used to calibrate future estimates via a simple moving average.

        :param job_id: The job's identifier.
        :param actual_duration_s: Actual wall-clock duration in seconds.
        :param printer_model: Printer model for per-printer history bucketing.
        """
        estimated = self._estimates.get(job_id)
        if estimated is None:
            # No prior estimate recorded; store with estimated = actual
            estimated = actual_duration_s

        history_key = printer_model.lower()
        history = self._history[history_key]
        history.append((estimated, actual_duration_s))

        # Trim to window size
        if len(history) > _HISTORY_WINDOW:
            self._history[history_key] = history[-_HISTORY_WINDOW:]

    def get_accuracy(self, *, printer_model: str = "fdm") -> float:
        """Return average estimation accuracy for a printer model.

        Accuracy is expressed as a value in 0.0--1.0, where 1.0 means
        estimates perfectly match actuals and lower values indicate
        larger estimation errors.

        :param printer_model: Printer model (or ``"fdm"`` for global).
        :returns: Average accuracy, or ``1.0`` if no history is available.
        """
        history_key = printer_model.lower()
        history = self._history.get(history_key, [])
        if not history:
            return 1.0

        accuracies: list[float] = []
        for estimated, actual in history:
            if actual == 0:
                continue
            ratio = min(estimated, actual) / max(estimated, actual)
            accuracies.append(ratio)

        if not accuracies:
            return 1.0

        return round(sum(accuracies) / len(accuracies), 4)

    def get_history_count(self, *, printer_model: str = "fdm") -> int:
        """Return the number of historical records for a printer model.

        :param printer_model: Printer model (or ``"fdm"`` for global).
        :returns: Number of recorded actuals.
        """
        return len(self._history.get(printer_model.lower(), []))

    # -- Internal -----------------------------------------------------------

    def _correction_factor(self, history_key: str) -> float:
        """Compute a correction factor from historical over/under estimates.

        Returns a multiplier to apply to raw time estimates.  A value > 1.0
        means jobs historically took longer than estimated.
        """
        history = self._history.get(history_key, [])
        if not history:
            return 1.0

        ratios: list[float] = []
        for estimated, actual in history:
            if estimated > 0:
                ratios.append(actual / estimated)

        if not ratios:
            return 1.0

        return sum(ratios) / len(ratios)

    def _compute_confidence(
        self,
        *,
        elapsed_s: float,
        total_s: float,
        history_key: str,
    ) -> float:
        """Compute estimate confidence.

        Confidence increases as the print progresses (more data available)
        and with more historical records for the printer model.

        :returns: Confidence value in 0.0--1.0.
        """
        # Base confidence from progress: starts at 0.3, rises to 1.0
        if total_s > 0:
            progress_fraction = min(elapsed_s / total_s, 1.0)
        else:
            progress_fraction = 0.0

        progress_confidence = 0.3 + (0.7 * progress_fraction)

        # Historical confidence: more past jobs = higher confidence
        history_count = len(self._history.get(history_key, []))
        if history_count >= 10:
            history_confidence = 1.0
        else:
            history_confidence = 0.5 + (0.5 * history_count / 10.0)

        return min(progress_confidence * history_confidence, 1.0)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_estimator: Optional[ProgressEstimator] = None


def get_estimator() -> ProgressEstimator:
    """Return the module-level :class:`ProgressEstimator` singleton.

    Lazily creates the instance on first call.
    """
    global _estimator
    if _estimator is None:
        _estimator = ProgressEstimator()
    return _estimator


def estimate_print_progress(
    *,
    layer_count: int,
    layer_height_mm: float,
    filament_length_mm: float,
    print_speed_mm_s: float,
    current_layer: int = 0,
    printer_model: str = "fdm",
    job_id: Optional[str] = None,
) -> PrintProgressEstimate:
    """Convenience wrapper delegating to the singleton estimator.

    See :meth:`ProgressEstimator.estimate_print` for parameter details.
    """
    return get_estimator().estimate_print(
        layer_count=layer_count,
        layer_height_mm=layer_height_mm,
        filament_length_mm=filament_length_mm,
        print_speed_mm_s=print_speed_mm_s,
        current_layer=current_layer,
        printer_model=printer_model,
        job_id=job_id,
    )


def estimate_progress_from_pct(
    *,
    progress_pct: float,
    elapsed_s: float,
    printer_model: str = "fdm",
    job_id: Optional[str] = None,
) -> PrintProgressEstimate:
    """Convenience wrapper for percentage-based estimation.

    See :meth:`ProgressEstimator.estimate_from_progress` for parameter details.
    """
    return get_estimator().estimate_from_progress(
        progress_pct=progress_pct,
        elapsed_s=elapsed_s,
        printer_model=printer_model,
        job_id=job_id,
    )
