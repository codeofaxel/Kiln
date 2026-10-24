"""Print failure classification, and what to do about each failure.

Classifies print failures by type from error messages, progress, sensor data
and event history, and answers each with a recovery plan.  Every install
gets the free floor (:mod:`kiln.recovery_floor`: what happened, the safety
lines, the safe thing to do now); the steps to fix it, the settings to
change and how to keep it from happening again are Kiln Pro's fix plan
(https://kiln3d.com/pricing), asked for one failure at a time through
:mod:`kiln._pro_recovery_bridge`.

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
from dataclasses import asdict, dataclass, field
from typing import Any

from kiln import recovery_floor

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
    """Suggested recovery steps for a classified failure.

    ``floor_note`` marks the free floor and says once why it is not Kiln
    Pro's fix plan (:func:`kiln._pro_recovery_bridge.floor_note`); it is
    never set on Kiln Pro's plan, whose answer is unchanged by it.
    """

    action: RecoveryAction
    steps: list[str]  # human-readable steps
    automated: bool  # can Kiln execute this automatically?
    estimated_time_minutes: int | None  # None on the floor, which makes no estimate
    risk_level: str  # "low", "medium", "high"
    settings_adjustments: dict[str, Any]  # suggested setting changes
    prevent_recurrence: list[str]  # tips to prevent this in the future
    floor_note: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("floor_note")
        data["action"] = self.action.value
        if self.floor_note:
            data.update(self.floor_note)
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
# The free floor, and Kiln Pro's fix plan as it arrives
# ---------------------------------------------------------------------------

#: How serious each failure is, as the floor says it.
_RISK: dict[FailureType, str] = {
    FailureType.THERMAL_RUNAWAY: "high",
    FailureType.LAYER_SHIFT: "medium",
    FailureType.NOZZLE_CLOG: "medium",
    FailureType.UNKNOWN: "medium",
}
#: The printer abilities a fix plan reads.
_ABILITIES = ("power_loss_recovery", "filament_sensor")


def _floor_plan(failure_type: FailureType, caps: dict[str, bool]) -> RecoveryPlan:
    """The free floor for a classified failure: what happened (in its note),
    the safety lines, and the safe thing to do now.  The printer's own
    power-loss recovery and filament-change screen are the printer's, and
    the floor says when to use them."""
    steps = recovery_floor.steps(failure_type.value)
    action = RecoveryAction.RESTART
    if failure_type is FailureType.THERMAL_RUNAWAY:
        action = RecoveryAction.SAFETY_SHUTDOWN
        steps = [
            "The printer should have shut its heater off by itself; if it is still heating, "
            "stop it now with an emergency stop or switch it off at the wall",
            *recovery_floor.THERMAL_RUNAWAY_STEPS,
        ]
    elif failure_type is FailureType.NOZZLE_CLOG:
        action = RecoveryAction.MAINTENANCE_REQUIRED
    elif failure_type in (FailureType.STRINGING, FailureType.UNKNOWN):
        action = RecoveryAction.MANUAL_INTERVENTION
        if failure_type is FailureType.UNKNOWN:
            steps = [
                "Stop the print if it is still running and look at the printer and the part",
                "Clear the bed and reprint once nothing looks wrong",
            ]
    elif failure_type is FailureType.POWER_LOSS:
        if caps.get("power_loss_recovery"):
            action = RecoveryAction.RESUME
            steps = ["Power the printer back on", recovery_floor.PRINTERS_OWN_RESUME,
                     "If it does not offer to continue, clear the bed and reprint"]
        else:
            steps = ["Power the printer back on", recovery_floor.REPRINT]
    elif failure_type is FailureType.FILAMENT_RUNOUT:
        if caps.get("filament_sensor"):
            action = RecoveryAction.RESUME
            steps = ["Load a new spool of the same filament",
                     "Continue from the printer's own filament-change screen"]
        else:
            action = RecoveryAction.MANUAL_INTERVENTION
            steps = ["The print has likely failed without a filament sensor",
                     "Clear the bed, load a new spool and reprint"]
    return RecoveryPlan(
        action=action,
        steps=steps,
        # A resume here is the printer's own (its power-loss recovery, its
        # filament-change screen), which carries on without Kiln.
        automated=action is RecoveryAction.RESUME,
        estimated_time_minutes=None,
        risk_level=_RISK.get(failure_type, "low"),
        settings_adjustments={},
        prevent_recurrence=[],
    )


def _plan_from_wire(answer: dict[str, Any]) -> RecoveryPlan | None:
    """Kiln Pro's fix plan as this install's :class:`RecoveryPlan`, or
    ``None`` when the answer is not one."""
    said = answer.get("plan")
    if not isinstance(said, dict):
        return None

    def _strings(value: Any) -> list[str]:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    try:
        return RecoveryPlan(
            action=RecoveryAction(said.get("action")),
            steps=_strings(said.get("steps")),
            automated=bool(said.get("automated")),
            estimated_time_minutes=said.get("estimated_time_minutes"),
            risk_level=str(said.get("risk_level") or "medium"),
            settings_adjustments=dict(said.get("settings_adjustments") or {}),
            prevent_recurrence=_strings(said.get("prevent_recurrence")),
        )
    except (TypeError, ValueError):
        return None


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

    Kiln Pro's fix plan when the caller's plan includes it (asked through
    :mod:`kiln._pro_recovery_bridge`); otherwise the free floor, whose
    ``floor_note`` says once why.

    :param classification: The failure classification to plan recovery for.
    :param printer_name: Optional printer name for context (never sent).
    :param printer_capabilities: Optional dict of printer capabilities
        (e.g. ``{"power_loss_recovery": True, "filament_sensor": True}``).
    :returns: A :class:`RecoveryPlan` with actionable steps.
    """
    from kiln import _pro_recovery_bridge as bridge

    given = printer_capabilities or {}
    caps = {name: bool(given[name]) for name in _ABILITIES if name in given}
    failure_type = classification.failure_type
    asked = bridge.ask("fix", {"failure_type": failure_type.value, "printer_capabilities": caps})
    if asked.answer is not None:
        plan = _plan_from_wire(asked.answer)
        if plan is not None:
            return plan
        from kiln.served_answer import Miss

        asked = bridge.Asked(included=True, miss=Miss("unanswered", detail="a plan this install could not read"))
    plan = _floor_plan(failure_type, caps)
    _, happened = recovery_floor.HAPPENED.get(failure_type.value, recovery_floor.HAPPENED["unknown"])
    plan.floor_note = {
        "what_happened": happened,
        **bridge.floor_note(asked, recovery_floor.FIX_PRO_ADDS.get(failure_type.value, "")),
    }
    return plan


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


def count_failure_records(
    *,
    printer_name: str | None = None,
    failure_type: str | None = None,
) -> int:
    """Count failure records matching the same filters as the listing.

    Lets a limited listing say "20 of 400" instead of presenting its
    page as the whole story.
    """
    from kiln.persistence import get_db

    query = "SELECT COUNT(*) FROM failure_records"
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
    row = get_db()._conn.execute(query, params).fetchone()
    return int(row[0]) if row else 0


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
