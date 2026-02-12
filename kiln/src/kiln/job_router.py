"""Fleet-aware job router for FDM multi-printer environments.

Routes print jobs to the optimal printer in a fleet based on capabilities,
current availability, material compatibility, and user preference for
quality vs speed.  This is the compound play for Kiln -- the ability to
automatically dispatch jobs across a heterogeneous printer fleet without
the agent (or human) needing to manually pick a machine.

The router integrates with the :mod:`kiln.registry` to query live printer
state and with printer capability metadata to make informed decisions.

Example::

    router = JobRouter(registry)
    req = JobRequirement(
        file_path="benchy.gcode",
        material="pla",
        build_volume_needed=(120.0, 120.0, 120.0),
    )
    result = router.route_job(req)
    if result.feasible:
        print(f"Best printer: {result.best_printer}")
"""

from __future__ import annotations

import enum
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class QualityPreference(enum.Enum):
    """User preference that biases printer selection."""

    SPEED = "speed"
    QUALITY = "quality"
    BALANCED = "balanced"


class RoutingOutcome(enum.Enum):
    """Classification of why a printer was selected or rejected."""

    SELECTED = "selected"
    REJECTED_NO_MATERIAL = "rejected_no_material"
    REJECTED_BUILD_VOLUME = "rejected_build_volume"
    REJECTED_OFFLINE = "rejected_offline"
    REJECTED_BUSY = "rejected_busy"
    REJECTED_NOZZLE = "rejected_nozzle"
    NO_PRINTERS_AVAILABLE = "no_printers_available"


# ---------------------------------------------------------------------------
# Scoring constants
# ---------------------------------------------------------------------------

# Points awarded for each criterion.  Higher total = better candidate.
_SCORE_MATERIAL_MATCH = 40
_SCORE_MATERIAL_COMPATIBLE = 15
_SCORE_IDLE = 25
_SCORE_QUALITY_PRINTER_QUALITY_JOB = 20
_SCORE_SPEED_PRINTER_SPEED_JOB = 20
_SCORE_BALANCED_BONUS = 10
_SCORE_NOZZLE_EXACT = 10
_SCORE_NOZZLE_COMPATIBLE = 5
_SCORE_VOLUME_HEADROOM = 5  # build volume > 2x needed

# Penalty scores (subtracted).
_PENALTY_MATERIAL_SWAP = 10  # printer can use the material but isn't loaded
_PENALTY_BUSY_QUEUE = 15  # printer has queued jobs


# ---------------------------------------------------------------------------
# Printer metadata
# ---------------------------------------------------------------------------

@dataclass
class PrinterProfile:
    """Static capability metadata for a registered printer.

    This is configuration data -- it describes what a printer *can* do,
    not what it is doing right now.  Live state (idle/busy, temperatures)
    comes from the :class:`~kiln.registry.PrinterRegistry`.

    :param name: Registry name matching the key in
        :class:`~kiln.registry.PrinterRegistry`.
    :param build_volume: Usable build volume as ``(X, Y, Z)`` in mm.
    :param supported_materials: Materials this printer can handle
        (e.g. ``["pla", "petg", "tpu"]``).
    :param loaded_material: Material currently loaded, if known.
    :param nozzle_diameter: Installed nozzle size in mm.
    :param quality_tier: ``"high"`` for fine-detail printers (e.g. Prusa,
        Bambu X1C), ``"standard"`` for workhorses (e.g. Ender).
    :param speed_tier: ``"fast"`` for high-speed printers (e.g. Bambu,
        Voron), ``"standard"`` for typical Cartesian machines.
    :param max_print_speed_mm_s: Advertised max speed for time estimates.
    :param queued_jobs: Number of jobs already queued for this printer.
    """

    name: str
    build_volume: Tuple[float, float, float] = (220.0, 220.0, 250.0)
    supported_materials: List[str] = field(default_factory=lambda: ["pla", "petg"])
    loaded_material: Optional[str] = None
    nozzle_diameter: float = 0.4
    quality_tier: str = "standard"
    speed_tier: str = "standard"
    max_print_speed_mm_s: float = 60.0
    queued_jobs: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        return data


# ---------------------------------------------------------------------------
# Job requirements
# ---------------------------------------------------------------------------

@dataclass
class JobRequirement:
    """Everything the router needs to know to pick a printer for a job.

    :param file_path: Path to the G-code file (used for logging/tracking).
    :param material: Required filament type (e.g. ``"pla"``, ``"abs"``,
        ``"tpu"``).  Case-insensitive matching.
    :param build_volume_needed: Minimum ``(X, Y, Z)`` bounding box in mm
        that the print requires.  ``None`` skips volume checks.
    :param nozzle_diameter: Required nozzle size in mm, or ``None`` for
        any nozzle.
    :param priority: Job priority (higher = more urgent).  Ties are broken
        by score.
    :param quality_preference: Bias the selection toward speed, quality,
        or a balanced pick.
    :param preferred_printer: Explicit printer name to prefer (soft
        preference, not a hard constraint).
    :param notes: Free-form notes for logging.
    """

    file_path: str
    material: str = "pla"
    build_volume_needed: Optional[Tuple[float, float, float]] = None
    nozzle_diameter: Optional[float] = None
    priority: int = 0
    quality_preference: QualityPreference = QualityPreference.BALANCED
    preferred_printer: Optional[str] = None
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        data["quality_preference"] = self.quality_preference.value
        return data


# ---------------------------------------------------------------------------
# Scoring result
# ---------------------------------------------------------------------------

@dataclass
class PrinterScore:
    """Score breakdown for a single printer evaluated against a job.

    :param printer_name: Registry name of the printer.
    :param score: Total numeric score (higher = better fit).
    :param reasons: Human-readable list of scoring rationale.
    :param estimated_time_minutes: Rough print time estimate for this
        printer, or ``None`` if indeterminate.
    :param outcome: Why this printer was selected or rejected.
    """

    printer_name: str
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    estimated_time_minutes: Optional[float] = None
    outcome: RoutingOutcome = RoutingOutcome.SELECTED

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        data["outcome"] = self.outcome.value
        return data


# ---------------------------------------------------------------------------
# Routing result
# ---------------------------------------------------------------------------

@dataclass
class RoutingResult:
    """Complete result of routing a job across the fleet.

    :param best_printer: Name of the recommended printer, or ``None`` if
        no suitable printer was found.
    :param scores: Per-printer score breakdown, sorted best-first.
    :param feasible: ``True`` if at least one printer can handle the job.
    :param warnings: Non-fatal advisory messages.
    :param errors: Fatal issues that prevent routing.
    """

    best_printer: Optional[str] = None
    scores: List[PrinterScore] = field(default_factory=list)
    feasible: bool = True
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "best_printer": self.best_printer,
            "scores": [s.to_dict() for s in self.scores],
            "feasible": self.feasible,
            "warnings": self.warnings,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_router_instance: Optional[JobRouter] = None


def get_router() -> JobRouter:
    """Return the lazily-initialised global :class:`JobRouter` singleton.

    The router is created on first access.  It requires a
    :class:`~kiln.registry.PrinterRegistry` which is obtained from the
    global registry singleton.

    :returns: The shared :class:`JobRouter` instance.
    """
    global _router_instance
    if _router_instance is None:
        from kiln.registry import PrinterRegistry

        _router_instance = JobRouter(registry=PrinterRegistry())
    return _router_instance


# ---------------------------------------------------------------------------
# Core router
# ---------------------------------------------------------------------------

class JobRouter:
    """Routes print jobs to the optimal printer in a fleet.

    The router evaluates every registered printer against the job
    requirements, assigns a numeric score, and returns a ranked list.
    Hard constraints (build volume, material support) are gate checks
    that disqualify a printer entirely; soft criteria (loaded material,
    speed/quality tier, availability) adjust the score.

    Usage::

        from kiln.registry import PrinterRegistry
        from kiln.job_router import JobRouter, JobRequirement

        registry = PrinterRegistry()
        # ... register printers ...

        router = JobRouter(registry=registry)
        req = JobRequirement(
            file_path="bracket.gcode",
            material="petg",
            build_volume_needed=(150.0, 150.0, 80.0),
            quality_preference=QualityPreference.QUALITY,
        )
        result = router.route_job(req, profiles)
        print(result.best_printer)  # e.g. "prusa-mk4"

    :param registry: The printer registry to query for live state.
    """

    def __init__(self, *, registry: Any = None) -> None:
        self._registry = registry

    # -- public API --------------------------------------------------------

    def route_job(
        self,
        requirement: JobRequirement,
        profiles: List[PrinterProfile],
    ) -> RoutingResult:
        """Score every printer and return the best match for a job.

        :param requirement: The job's requirements.
        :param profiles: Static capability profiles for each candidate
            printer.  Only printers present in both the profiles list and
            the registry are evaluated.
        :returns: A :class:`RoutingResult` with ranked scores and the
            recommended printer.
        """
        result = RoutingResult()

        if not profiles:
            result.feasible = False
            result.errors.append("No printer profiles provided.")
            return result

        scores: List[PrinterScore] = []
        for profile in profiles:
            ps = self.score_printer(requirement, profile)
            scores.append(ps)

        # Sort by score descending; ties broken by printer name for
        # deterministic ordering.
        scores.sort(key=lambda s: (-s.score, s.printer_name))
        result.scores = scores

        # Find the best *eligible* printer (outcome == SELECTED).
        best = self.find_best_printer(scores)
        if best is not None:
            result.best_printer = best.printer_name
        else:
            result.feasible = False
            rejection_reasons = set()
            for s in scores:
                if s.outcome != RoutingOutcome.SELECTED:
                    rejection_reasons.add(s.outcome.value)
            result.errors.append(
                f"No printer can fulfil this job. Rejection reasons: "
                f"{', '.join(sorted(rejection_reasons)) or 'unknown'}."
            )

        # Advisory warnings.
        selected_scores = [s for s in scores if s.outcome == RoutingOutcome.SELECTED]
        if len(selected_scores) == 1 and result.feasible:
            result.warnings.append(
                f"Only one printer ({selected_scores[0].printer_name}) is "
                f"eligible. No fallback available."
            )

        if result.best_printer and requirement.preferred_printer:
            if result.best_printer != requirement.preferred_printer:
                result.warnings.append(
                    f"Preferred printer {requirement.preferred_printer!r} was "
                    f"not the best match; routing to "
                    f"{result.best_printer!r} instead."
                )

        return result

    def score_printer(
        self,
        requirement: JobRequirement,
        profile: PrinterProfile,
    ) -> PrinterScore:
        """Evaluate a single printer against a job requirement.

        Gate checks (hard fails) are evaluated first.  If the printer
        passes all gates, soft scoring criteria are applied additively.

        :param requirement: The job's requirements.
        :param profile: Static capability profile for the printer.
        :returns: A :class:`PrinterScore` with breakdown.
        """
        ps = PrinterScore(printer_name=profile.name)
        req_material = requirement.material.lower()

        # ---- gate: live state (offline / busy) ---------------------------
        live_state = self._get_live_state(profile.name)
        if live_state == "offline":
            ps.outcome = RoutingOutcome.REJECTED_OFFLINE
            ps.reasons.append("Printer is offline.")
            ps.score = -1.0
            return ps

        # ---- gate: build volume ------------------------------------------
        if requirement.build_volume_needed is not None:
            if not self._volume_fits(requirement.build_volume_needed, profile.build_volume):
                ps.outcome = RoutingOutcome.REJECTED_BUILD_VOLUME
                ps.reasons.append(
                    f"Build volume {profile.build_volume} is too small "
                    f"for required {requirement.build_volume_needed}."
                )
                ps.score = -1.0
                return ps

        # ---- gate: material support --------------------------------------
        supported_lower = [m.lower() for m in profile.supported_materials]
        if req_material not in supported_lower:
            ps.outcome = RoutingOutcome.REJECTED_NO_MATERIAL
            ps.reasons.append(
                f"Printer does not support material {req_material!r}. "
                f"Supported: {profile.supported_materials}."
            )
            ps.score = -1.0
            return ps

        # ---- gate: nozzle diameter ---------------------------------------
        if requirement.nozzle_diameter is not None:
            if abs(profile.nozzle_diameter - requirement.nozzle_diameter) > 0.05:
                ps.outcome = RoutingOutcome.REJECTED_NOZZLE
                ps.reasons.append(
                    f"Nozzle {profile.nozzle_diameter} mm does not match "
                    f"required {requirement.nozzle_diameter} mm."
                )
                ps.score = -1.0
                return ps
            elif abs(profile.nozzle_diameter - requirement.nozzle_diameter) < 0.001:
                ps.score += _SCORE_NOZZLE_EXACT
                ps.reasons.append(f"+{_SCORE_NOZZLE_EXACT}: exact nozzle match.")
            else:
                ps.score += _SCORE_NOZZLE_COMPATIBLE
                ps.reasons.append(f"+{_SCORE_NOZZLE_COMPATIBLE}: nozzle within tolerance.")

        # ---- soft: material loaded vs swap required ----------------------
        loaded_lower = (profile.loaded_material or "").lower()
        if loaded_lower == req_material:
            ps.score += _SCORE_MATERIAL_MATCH
            ps.reasons.append(
                f"+{_SCORE_MATERIAL_MATCH}: material {req_material!r} already loaded."
            )
        else:
            ps.score += _SCORE_MATERIAL_COMPATIBLE
            ps.score -= _PENALTY_MATERIAL_SWAP
            ps.reasons.append(
                f"+{_SCORE_MATERIAL_COMPATIBLE} -{_PENALTY_MATERIAL_SWAP}: "
                f"material supported but swap from "
                f"{loaded_lower or 'unknown'!r} required."
            )

        # ---- soft: printer availability ----------------------------------
        if live_state == "idle":
            ps.score += _SCORE_IDLE
            ps.reasons.append(f"+{_SCORE_IDLE}: printer is idle and ready.")
        elif live_state == "busy":
            # Not a hard rejection -- printer may finish soon.
            ps.score -= _PENALTY_BUSY_QUEUE
            ps.reasons.append(
                f"-{_PENALTY_BUSY_QUEUE}: printer is currently busy."
            )

        # ---- soft: queued jobs backlog -----------------------------------
        if profile.queued_jobs > 0:
            penalty = profile.queued_jobs * _PENALTY_BUSY_QUEUE
            ps.score -= penalty
            ps.reasons.append(
                f"-{penalty}: {profile.queued_jobs} job(s) already queued."
            )

        # ---- soft: quality / speed preference ----------------------------
        pref_bonus = self._preference_bonus(
            requirement.quality_preference, profile,
        )
        if pref_bonus > 0:
            ps.score += pref_bonus
            ps.reasons.append(
                f"+{pref_bonus:.0f}: "
                f"{requirement.quality_preference.value} preference "
                f"aligns with printer capabilities."
            )

        # ---- soft: build volume headroom ---------------------------------
        if requirement.build_volume_needed is not None:
            ratio = self._volume_ratio(
                requirement.build_volume_needed, profile.build_volume,
            )
            if ratio is not None and ratio >= 2.0:
                ps.score += _SCORE_VOLUME_HEADROOM
                ps.reasons.append(
                    f"+{_SCORE_VOLUME_HEADROOM}: ample build volume headroom "
                    f"({ratio:.1f}x)."
                )

        # ---- soft: preferred printer bonus --------------------------------
        if (
            requirement.preferred_printer
            and requirement.preferred_printer == profile.name
        ):
            bonus = 15
            ps.score += bonus
            ps.reasons.append(f"+{bonus}: user-preferred printer.")

        # ---- time estimate -----------------------------------------------
        ps.estimated_time_minutes = self._estimate_print_time(
            requirement, profile,
        )

        ps.outcome = RoutingOutcome.SELECTED
        return ps

    def find_best_printer(
        self,
        scores: List[PrinterScore],
    ) -> Optional[PrinterScore]:
        """Return the highest-scoring eligible printer, or ``None``.

        :param scores: Pre-computed scores (need not be sorted).
        :returns: The best :class:`PrinterScore`, or ``None`` if all
            printers were rejected.
        """
        eligible = [
            s for s in scores
            if s.outcome == RoutingOutcome.SELECTED and s.score >= 0
        ]
        if not eligible:
            return None
        eligible.sort(key=lambda s: (-s.score, s.printer_name))
        return eligible[0]

    # -- internal helpers --------------------------------------------------

    def _get_live_state(self, printer_name: str) -> str:
        """Query the registry for the printer's live status.

        Returns ``"idle"``, ``"busy"``, or ``"offline"``.  Swallows
        exceptions and returns ``"offline"`` if the printer cannot be
        reached.
        """
        if self._registry is None:
            # No registry -- assume idle (useful for unit testing without
            # a full registry).
            return "idle"

        try:
            adapter = self._registry.get(printer_name)
            state = adapter.get_state()
        except Exception:
            return "offline"

        from kiln.printers.base import PrinterStatus  # lazy import

        if not state.connected:
            return "offline"
        if state.state == PrinterStatus.IDLE:
            return "idle"
        if state.state in (
            PrinterStatus.PRINTING,
            PrinterStatus.PAUSED,
            PrinterStatus.BUSY,
            PrinterStatus.CANCELLING,
        ):
            return "busy"
        if state.state == PrinterStatus.ERROR:
            return "offline"
        return "idle"

    @staticmethod
    def _volume_fits(
        needed: Tuple[float, float, float],
        available: Tuple[float, float, float],
    ) -> bool:
        """Return ``True`` if the printer's build volume can contain the part.

        Each axis of *needed* must be <= the corresponding axis of
        *available*.
        """
        return (
            needed[0] <= available[0]
            and needed[1] <= available[1]
            and needed[2] <= available[2]
        )

    @staticmethod
    def _volume_ratio(
        needed: Tuple[float, float, float],
        available: Tuple[float, float, float],
    ) -> Optional[float]:
        """Return the ratio of available volume to needed volume.

        Returns ``None`` if the needed volume is zero (avoids division by
        zero).
        """
        needed_vol = needed[0] * needed[1] * needed[2]
        if needed_vol <= 0:
            return None
        available_vol = available[0] * available[1] * available[2]
        return available_vol / needed_vol

    @staticmethod
    def _preference_bonus(
        preference: QualityPreference,
        profile: PrinterProfile,
    ) -> float:
        """Score bonus/penalty based on quality preference vs printer tier.

        :param preference: The job's quality preference.
        :param profile: The printer's capability profile.
        :returns: Points to add (can be negative).
        """
        bonus = 0.0
        reasons: List[str] = []

        if preference == QualityPreference.QUALITY:
            if profile.quality_tier == "high":
                bonus += _SCORE_QUALITY_PRINTER_QUALITY_JOB
            elif profile.speed_tier == "fast" and profile.quality_tier != "high":
                # Fast printers are *capable* but not ideal for quality.
                bonus += _SCORE_BALANCED_BONUS * 0.5

        elif preference == QualityPreference.SPEED:
            if profile.speed_tier == "fast":
                bonus += _SCORE_SPEED_PRINTER_SPEED_JOB
            elif profile.quality_tier == "high" and profile.speed_tier != "fast":
                bonus += _SCORE_BALANCED_BONUS * 0.5

        elif preference == QualityPreference.BALANCED:
            # Balanced jobs get a small bonus for printers that are good
            # at either, with a larger bonus if they're good at both.
            if profile.quality_tier == "high" and profile.speed_tier == "fast":
                bonus += _SCORE_BALANCED_BONUS * 1.5
            elif profile.quality_tier == "high" or profile.speed_tier == "fast":
                bonus += _SCORE_BALANCED_BONUS

        return bonus

    @staticmethod
    def _estimate_print_time(
        requirement: JobRequirement,
        profile: PrinterProfile,
    ) -> Optional[float]:
        """Rough print time estimate in minutes.

        This is a heuristic -- real estimates come from slicer output or
        G-code analysis.  Here we use build volume as a proxy for part
        size and scale by the printer's advertised speed.

        :param requirement: The job's requirements.
        :param profile: The printer's capability profile.
        :returns: Estimated minutes, or ``None`` if volume data is missing.
        """
        if requirement.build_volume_needed is None:
            return None

        vol = requirement.build_volume_needed
        part_volume_cm3 = (vol[0] * vol[1] * vol[2]) / 1_000.0  # mm^3 -> cm^3

        # Heuristic: ~2 minutes per cm^3 at 60 mm/s baseline.
        baseline_speed = 60.0
        speed_factor = baseline_speed / max(profile.max_print_speed_mm_s, 1.0)

        # Base time scaled by volume and speed.
        base_minutes = part_volume_cm3 * 2.0 * speed_factor

        # Clamp to a reasonable range.
        return round(max(5.0, min(base_minutes, 4320.0)), 1)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def route_job(
    requirement: JobRequirement,
    profiles: List[PrinterProfile],
) -> RoutingResult:
    """Module-level convenience that uses the global router singleton.

    :param requirement: The job's requirements.
    :param profiles: Static capability profiles for each candidate printer.
    :returns: A :class:`RoutingResult` with ranked scores and the
        recommended printer.
    """
    return get_router().route_job(requirement, profiles)
