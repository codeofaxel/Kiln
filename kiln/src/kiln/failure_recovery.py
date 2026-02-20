"""Intelligent failure recovery for 3D printing.

Classifies print failures by type, suggests recovery actions, and
automates recovery when safe to do so. Uses print history, sensor data,
and failure pattern matching to make intelligent decisions.

Failure taxonomy:
- SPAGHETTI: Filament in air, no bed adhesion -> restart from scratch
- LAYER_SHIFT: Mechanical skip -> may be salvageable with Z-offset
- ADHESION_LOSS: Detached from bed mid-print -> clean bed, re-level, restart
- NOZZLE_CLOG: Under-extrusion or no extrusion -> cold pull, restart
- STRINGING: Excessive stringing between parts -> adjust retraction, restart
- THERMAL_RUNAWAY: Temperature exceeded limits -> safety shutdown, inspect
- POWER_LOSS: Unexpected power interruption -> resume if firmware supports
- FILAMENT_RUNOUT: Filament ran out mid-print -> reload and resume
- WARPING: Corners lifting from bed -> adjust bed temp, add brim, restart
"""

from __future__ import annotations

import contextlib
import enum
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FailureType(enum.Enum):
    """Classification of 3D print failure modes."""

    SPAGHETTI = "spaghetti"
    LAYER_SHIFT = "layer_shift"
    ADHESION_LOSS = "adhesion_loss"
    NOZZLE_CLOG = "nozzle_clog"
    STRINGING = "stringing"
    THERMAL_RUNAWAY = "thermal_runaway"
    POWER_LOSS = "power_loss"
    FILAMENT_RUNOUT = "filament_runout"
    WARPING = "warping"
    UNKNOWN = "unknown"


class RecoveryAction(enum.Enum):
    """Recovery actions that can be taken after a failure."""

    RESTART = "restart"
    RESUME = "resume"
    ADJUST_AND_RESTART = "adjust_and_restart"
    MAINTENANCE_REQUIRED = "maintenance_required"
    MANUAL_INTERVENTION = "manual_intervention"
    SAFETY_SHUTDOWN = "safety_shutdown"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FailureClassification:
    """Result of classifying a print failure."""

    failure_type: FailureType
    confidence: float  # 0.0 - 1.0
    evidence: list[str]  # what clues led to this classification
    progress_at_failure: float  # 0.0 - 1.0
    time_printing_seconds: int
    material_wasted_grams: float

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["failure_type"] = self.failure_type.value
        return data


@dataclass
class RecoveryPlan:
    """Suggested recovery steps for a classified failure."""

    action: RecoveryAction
    steps: list[str]  # human-readable steps
    automated: bool  # can Kiln execute this automatically?
    estimated_time_minutes: int
    risk_level: str  # "low", "medium", "high"
    settings_adjustments: dict[str, Any]  # suggested setting changes
    prevent_recurrence: list[str]  # tips to prevent this in the future

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["action"] = self.action.value
        return data


@dataclass
class FailureAnalysis:
    """Full failure analysis: classification + recovery + history."""

    classification: FailureClassification
    recovery_plan: RecoveryPlan
    similar_failures: list[dict[str, Any]]  # from history
    printer_health: dict[str, Any]  # overall printer health indicators

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.to_dict(),
            "recovery_plan": self.recovery_plan.to_dict(),
            "similar_failures": self.similar_failures,
            "printer_health": self.printer_health,
        }


# ---------------------------------------------------------------------------
# Keyword/pattern maps for heuristic classification
# ---------------------------------------------------------------------------

_ERROR_KEYWORDS: dict[FailureType, list[str]] = {
    FailureType.THERMAL_RUNAWAY: [
        "thermal runaway",
        "maxtemp",
        "mintemp",
        "temperature exceeded",
        "heating failed",
        "temp too high",
    ],
    FailureType.NOZZLE_CLOG: [
        "clog",
        "under-extrusion",
        "underextrusion",
        "no extrusion",
        "filament jam",
        "blocked nozzle",
    ],
    FailureType.POWER_LOSS: [
        "power loss",
        "power failure",
        "unexpected shutdown",
        "connection lost",
        "serial disconnect",
    ],
    FailureType.FILAMENT_RUNOUT: [
        "filament runout",
        "filament out",
        "no filament",
        "filament sensor",
        "spool empty",
    ],
    FailureType.LAYER_SHIFT: [
        "layer shift",
        "layer misalignment",
        "skip",
        "belt slip",
        "motor skip",
    ],
    FailureType.ADHESION_LOSS: [
        "adhesion",
        "detached",
        "came loose",
        "unstuck",
        "first layer",
        "bed adhesion",
    ],
    FailureType.SPAGHETTI: [
        "spaghetti",
        "blob",
        "filament in air",
        "printing in air",
    ],
    FailureType.STRINGING: [
        "stringing",
        "oozing",
        "strings",
        "whiskers",
    ],
    FailureType.WARPING: [
        "warping",
        "warp",
        "curling",
        "corner lift",
        "lifting",
    ],
}


# ---------------------------------------------------------------------------
# Recovery plan templates
# ---------------------------------------------------------------------------


def _build_recovery(failure_type: FailureType, *, printer_capabilities: dict[str, Any] | None = None) -> RecoveryPlan:
    """Build a recovery plan for a given failure type."""
    caps = printer_capabilities or {}

    plans: dict[FailureType, RecoveryPlan] = {
        FailureType.SPAGHETTI: RecoveryPlan(
            action=RecoveryAction.RESTART,
            steps=[
                "Cancel the current print",
                "Remove the spaghetti mess from the bed and nozzle",
                "Clean the build plate with IPA",
                "Re-level the bed if needed",
                "Restart the print from scratch",
            ],
            automated=False,
            estimated_time_minutes=15,
            risk_level="low",
            settings_adjustments={
                "first_layer_speed": "reduce by 20%",
                "bed_temp": "increase by 5C for better adhesion",
            },
            prevent_recurrence=[
                "Ensure bed is properly leveled",
                "Use adhesion helpers (brim, raft) for small-footprint parts",
                "Check first layer calibration",
            ],
        ),
        FailureType.LAYER_SHIFT: RecoveryPlan(
            action=RecoveryAction.ADJUST_AND_RESTART,
            steps=[
                "Cancel the current print",
                "Check belt tension on X and Y axes",
                "Inspect pulleys for set screw tightness",
                "Clear any obstructions from the motion path",
                "Restart the print with reduced acceleration",
            ],
            automated=False,
            estimated_time_minutes=20,
            risk_level="medium",
            settings_adjustments={
                "acceleration": "reduce by 30%",
                "jerk": "reduce by 30%",
                "print_speed": "reduce by 20%",
            },
            prevent_recurrence=[
                "Tighten belts regularly",
                "Reduce print speed and acceleration for tall prints",
                "Check for loose pulleys and set screws",
            ],
        ),
        FailureType.ADHESION_LOSS: RecoveryPlan(
            action=RecoveryAction.ADJUST_AND_RESTART,
            steps=[
                "Cancel the current print",
                "Remove the detached part from the build plate",
                "Clean the build plate thoroughly with IPA",
                "Re-level the bed",
                "Apply adhesion helper (glue stick, hairspray, or brim)",
                "Restart the print",
            ],
            automated=False,
            estimated_time_minutes=15,
            risk_level="low",
            settings_adjustments={
                "bed_temp": "increase by 5-10C",
                "first_layer_height": "increase slightly",
                "brim_width": "add 5mm brim",
            },
            prevent_recurrence=[
                "Clean bed before every print",
                "Use a brim for parts with small footprints",
                "Calibrate first layer height (live Z adjust)",
                "Check bed temperature is correct for material",
            ],
        ),
        FailureType.NOZZLE_CLOG: RecoveryPlan(
            action=RecoveryAction.MAINTENANCE_REQUIRED,
            steps=[
                "Cancel the current print",
                "Heat the nozzle to printing temperature",
                "Perform a cold pull (heat to 250C, cool to 90C, pull filament)",
                "If clog persists, remove and clean or replace the nozzle",
                "Reload filament and test extrusion",
                "Restart the print",
            ],
            automated=False,
            estimated_time_minutes=30,
            risk_level="medium",
            settings_adjustments={
                "print_temp": "increase by 5-10C",
                "retraction_distance": "reduce slightly",
            },
            prevent_recurrence=[
                "Use high-quality filament",
                "Avoid excessive retraction (causes heat creep)",
                "Ensure PTFE tube is seated properly",
                "Clean nozzle periodically with cold pulls",
            ],
        ),
        FailureType.STRINGING: RecoveryPlan(
            action=RecoveryAction.ADJUST_AND_RESTART,
            steps=[
                "Cancel the current print",
                "Remove the stringy print from the bed",
                "Adjust retraction settings",
                "Restart the print",
            ],
            automated=False,
            estimated_time_minutes=10,
            risk_level="low",
            settings_adjustments={
                "retraction_distance": "increase by 1-2mm",
                "retraction_speed": "increase by 10mm/s",
                "travel_speed": "increase by 20%",
                "print_temp": "decrease by 5-10C",
            },
            prevent_recurrence=[
                "Run a retraction test (temperature tower)",
                "Ensure filament is dry",
                "Reduce nozzle temperature to lower end of range",
            ],
        ),
        FailureType.THERMAL_RUNAWAY: RecoveryPlan(
            action=RecoveryAction.SAFETY_SHUTDOWN,
            steps=[
                "The printer should have automatically shut down",
                "Allow the printer to cool completely before touching",
                "Inspect the thermistor and heater connections",
                "Check for loose wires or damaged connectors",
                "Run a PID auto-tune before resuming printing",
                "Test with a temperature hold before printing",
            ],
            automated=False,
            estimated_time_minutes=60,
            risk_level="high",
            settings_adjustments={
                "pid_tune": "run PID auto-tune for hotend and bed",
            },
            prevent_recurrence=[
                "Run PID auto-tune after any hardware changes",
                "Inspect wiring periodically",
                "Ensure thermistor is properly seated",
                "Never leave printer unattended during first prints after maintenance",
            ],
        ),
        FailureType.POWER_LOSS: RecoveryPlan(
            action=RecoveryAction.RESUME if caps.get("power_loss_recovery") else RecoveryAction.RESTART,
            steps=(
                [
                    "Power on the printer",
                    "Use the firmware power-loss recovery feature to resume",
                    "Monitor the first few layers after resume for quality",
                ]
                if caps.get("power_loss_recovery")
                else [
                    "Power on the printer",
                    "Remove the partial print from the bed",
                    "Restart the print from scratch",
                ]
            ),
            automated=bool(caps.get("power_loss_recovery")),
            estimated_time_minutes=5 if caps.get("power_loss_recovery") else 10,
            risk_level="low",
            settings_adjustments={},
            prevent_recurrence=[
                "Use a UPS (uninterruptible power supply) for the printer",
                "Enable power-loss recovery in firmware if available",
            ],
        ),
        FailureType.FILAMENT_RUNOUT: RecoveryPlan(
            action=RecoveryAction.RESUME if caps.get("filament_sensor") else RecoveryAction.MANUAL_INTERVENTION,
            steps=(
                [
                    "Load a new spool of the same filament",
                    "Use the printer's filament change procedure to resume",
                    "Monitor the first few layers after resume for consistency",
                ]
                if caps.get("filament_sensor")
                else [
                    "The print has likely failed without a filament sensor",
                    "Remove the partial print",
                    "Load a new spool",
                    "Restart the print from scratch",
                ]
            ),
            automated=bool(caps.get("filament_sensor")),
            estimated_time_minutes=5 if caps.get("filament_sensor") else 15,
            risk_level="low",
            settings_adjustments={},
            prevent_recurrence=[
                "Check spool weight before starting long prints",
                "Install a filament runout sensor if not present",
                "Use spool tracking to estimate remaining filament",
            ],
        ),
        FailureType.WARPING: RecoveryPlan(
            action=RecoveryAction.ADJUST_AND_RESTART,
            steps=[
                "Cancel the current print",
                "Remove the warped part from the bed",
                "Clean the build plate",
                "Increase bed temperature",
                "Add a brim or use an enclosure",
                "Restart the print",
            ],
            automated=False,
            estimated_time_minutes=15,
            risk_level="low",
            settings_adjustments={
                "bed_temp": "increase by 5-10C",
                "brim_width": "add 8mm brim",
                "enclosure": "use an enclosure if available",
                "fan_speed": "reduce first layer fan speed to 0%",
            },
            prevent_recurrence=[
                "Use an enclosure for ABS/ASA/Nylon",
                "Increase bed temperature",
                "Add a brim for large flat parts",
                "Reduce part cooling fan for the first 3-5 layers",
                "Ensure the room is free of drafts",
            ],
        ),
        FailureType.UNKNOWN: RecoveryPlan(
            action=RecoveryAction.MANUAL_INTERVENTION,
            steps=[
                "Cancel the current print if still running",
                "Inspect the printer and print for obvious issues",
                "Check printer logs for error messages",
                "Take a photo for future reference",
                "Clean the build plate and restart if no hardware issues found",
            ],
            automated=False,
            estimated_time_minutes=20,
            risk_level="medium",
            settings_adjustments={},
            prevent_recurrence=[
                "Monitor prints more closely",
                "Enable print monitoring tools",
                "Record detailed failure information for better future classification",
            ],
        ),
    }
    return plans[failure_type]


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def classify_failure(
    *,
    job_id: str | None = None,
    printer_name: str | None = None,
    progress: float = 0.0,
    error_message: str | None = None,
    temperature_history: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> FailureClassification:
    """Classify a print failure using heuristics.

    Uses error messages, progress percentage, temperature data, and event
    history to determine the most likely failure type.

    :param job_id: Optional job ID for context.
    :param printer_name: Optional printer name for context.
    :param progress: Print progress at failure (0.0 - 1.0).
    :param error_message: Error message from the printer or system.
    :param temperature_history: List of temperature readings over time.
    :param events: List of event dicts from the event bus.
    :returns: A :class:`FailureClassification` with the best match.
    """
    evidence: list[str] = []
    scores: dict[FailureType, float] = {ft: 0.0 for ft in FailureType}

    # --- Heuristic 1: Error message keyword matching ---
    if error_message:
        msg_lower = error_message.lower()
        for ftype, keywords in _ERROR_KEYWORDS.items():
            for kw in keywords:
                if kw in msg_lower:
                    scores[ftype] += 0.6
                    evidence.append(f"Error message contains '{kw}'")

    # --- Heuristic 2: Progress-based inference ---
    # Only apply progress heuristics when we have some other signal that
    # a failure actually occurred (error message, events, temp data), or
    # when progress is explicitly non-zero.
    has_signal = bool(error_message or temperature_history or events)
    if progress < 0.10 and (progress > 0 or has_signal):
        scores[FailureType.ADHESION_LOSS] += 0.3
        evidence.append(f"Failed early (progress={progress:.0%}) — likely first-layer issue")
    elif progress > 0.80:
        scores[FailureType.THERMAL_RUNAWAY] += 0.1
        scores[FailureType.LAYER_SHIFT] += 0.1
        evidence.append(f"Failed late (progress={progress:.0%}) — may be mechanical or thermal")

    # --- Heuristic 3: Temperature anomaly detection ---
    if temperature_history:
        temps = [t.get("tool_actual", 0) for t in temperature_history if "tool_actual" in t]
        if temps:
            max_temp = max(temps)
            min_temp = min(temps)
            avg_temp = sum(temps) / len(temps)
            temp_range = max_temp - min_temp

            if max_temp > 280:
                scores[FailureType.THERMAL_RUNAWAY] += 0.5
                evidence.append(f"Temperature spike detected: {max_temp}C")
            if temp_range > 30:
                scores[FailureType.THERMAL_RUNAWAY] += 0.3
                evidence.append(f"Large temperature variation: {temp_range:.1f}C range")
            if avg_temp < 170 and avg_temp > 0:
                scores[FailureType.NOZZLE_CLOG] += 0.2
                evidence.append(f"Low average temperature ({avg_temp:.0f}C) may indicate clog")

    # --- Heuristic 4: Event-based inference ---
    if events:
        for evt in events:
            evt_type = str(evt.get("type", "")).lower()
            evt_data = evt.get("data", {})
            if isinstance(evt_data, str):
                evt_data = {}

            if "power" in evt_type or "disconnect" in evt_type:
                scores[FailureType.POWER_LOSS] += 0.5
                evidence.append(f"Power/disconnect event: {evt_type}")
            if "filament" in evt_type and ("runout" in evt_type or "out" in evt_type):
                scores[FailureType.FILAMENT_RUNOUT] += 0.5
                evidence.append(f"Filament event: {evt_type}")

    # --- Heuristic 5: No error message + sudden stop ---
    if not error_message and progress > 0:
        scores[FailureType.POWER_LOSS] += 0.3
        evidence.append("No error message with partial progress — possible power loss")

    # Pick the highest-scoring failure type
    best_type = max(scores, key=lambda ft: scores[ft])
    best_score = scores[best_type]

    # If nothing matched well, fall back to UNKNOWN
    if best_score < 0.1:
        best_type = FailureType.UNKNOWN
        best_score = 0.1
        evidence.append("No strong signal — failure type unknown")

    # Normalise confidence to 0.0 - 1.0
    confidence = min(best_score, 1.0)

    # Estimate material wasted (rough: assume 10g/hour of printing)
    time_seconds = 0
    if events:
        timestamps = [e.get("timestamp", 0) for e in events if e.get("timestamp")]
        if len(timestamps) >= 2:
            time_seconds = int(max(timestamps) - min(timestamps))

    material_grams = (time_seconds / 3600.0) * 10.0  # ~10g/hour rough estimate

    return FailureClassification(
        failure_type=best_type,
        confidence=confidence,
        evidence=evidence,
        progress_at_failure=progress,
        time_printing_seconds=time_seconds,
        material_wasted_grams=round(material_grams, 1),
    )


def plan_recovery(
    classification: FailureClassification,
    *,
    printer_name: str | None = None,
    printer_capabilities: dict[str, Any] | None = None,
) -> RecoveryPlan:
    """Generate a recovery plan for a classified failure.

    :param classification: The failure classification to plan recovery for.
    :param printer_name: Optional printer name for context.
    :param printer_capabilities: Optional dict of printer capabilities
        (e.g. ``{"power_loss_recovery": True, "filament_sensor": True}``).
    :returns: A :class:`RecoveryPlan` with actionable steps.
    """
    return _build_recovery(classification.failure_type, printer_capabilities=printer_capabilities)


def analyze_failure(
    *,
    job_id: str | None = None,
    printer_name: str | None = None,
    progress: float = 0.0,
    error_message: str | None = None,
) -> FailureAnalysis:
    """Full failure analysis: classify, plan, and find similar failures.

    :param job_id: Optional job ID for context.
    :param printer_name: Optional printer name for context.
    :param progress: Print progress at failure (0.0 - 1.0).
    :param error_message: Error message from the printer or system.
    :returns: A complete :class:`FailureAnalysis`.
    """
    classification = classify_failure(
        job_id=job_id,
        printer_name=printer_name,
        progress=progress,
        error_message=error_message,
    )
    recovery = plan_recovery(classification, printer_name=printer_name)

    # Look up similar failures from history
    similar: list[dict[str, Any]] = []
    try:
        similar = get_failure_history(
            printer_name=printer_name,
            failure_type=classification.failure_type.value,
            limit=5,
        )
    except Exception:
        logger.debug("Failed to fetch failure history (non-fatal)")

    # Build printer health summary
    health: dict[str, Any] = {"status": "unknown"}
    if printer_name:
        try:
            all_failures = get_failure_history(printer_name=printer_name, limit=50)
            total = len(all_failures)
            if total > 0:
                type_counts: dict[str, int] = {}
                for f in all_failures:
                    ft = f.get("failure_type", "unknown")
                    type_counts[ft] = type_counts.get(ft, 0) + 1
                most_common = max(type_counts, key=lambda k: type_counts[k])
                health = {
                    "status": "degraded" if total > 10 else "fair" if total > 3 else "good",
                    "total_failures": total,
                    "most_common_failure": most_common,
                    "failure_breakdown": type_counts,
                }
            else:
                health = {"status": "good", "total_failures": 0}
        except Exception:
            logger.debug("Failed to compute printer health (non-fatal)")

    return FailureAnalysis(
        classification=classification,
        recovery_plan=recovery,
        similar_failures=similar,
        printer_health=health,
    )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def get_failure_history(
    *,
    printer_name: str | None = None,
    failure_type: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Query failure records from the database.

    :param printer_name: Optional filter by printer name.
    :param failure_type: Optional filter by failure type string.
    :param limit: Maximum records to return (default 20).
    :returns: List of failure record dicts.
    """
    from kiln.persistence import get_db

    db = get_db()
    query = "SELECT * FROM failure_records"
    conditions: list[str] = []
    params: list[Any] = []

    if printer_name:
        conditions.append("printer_name = ?")
        params.append(printer_name)
    if failure_type:
        conditions.append("failure_type = ?")
        params.append(failure_type)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    try:
        rows = db._conn.execute(query, params).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            # Parse JSON fields
            for json_field in ("evidence", "settings_adjustments"):
                if record.get(json_field):
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        record[json_field] = json.loads(record[json_field])
            results.append(record)
        return results
    except Exception:
        logger.debug("Failed to query failure_records table", exc_info=True)
        return []


def record_failure(
    classification: FailureClassification,
    recovery_plan: RecoveryPlan,
    *,
    printer_name: str | None = None,
    job_id: str | None = None,
) -> None:
    """Save a failure record to the database for learning.

    :param classification: The failure classification.
    :param recovery_plan: The recovery plan generated.
    :param printer_name: Optional printer name.
    :param job_id: Optional job ID.
    """
    from kiln.persistence import get_db

    db = get_db()
    try:
        db._conn.execute(
            """INSERT INTO failure_records
               (job_id, printer_name, failure_type, confidence,
                progress_at_failure, recovery_action, settings_adjustments,
                evidence, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                printer_name,
                classification.failure_type.value,
                classification.confidence,
                classification.progress_at_failure,
                recovery_plan.action.value,
                json.dumps(recovery_plan.settings_adjustments),
                json.dumps(classification.evidence),
                time.time(),
            ),
        )
        db._conn.commit()
    except Exception:
        logger.exception("Failed to record failure (non-fatal)")
