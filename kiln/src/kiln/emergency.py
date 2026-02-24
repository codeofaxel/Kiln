"""Emergency stop and safety interlock system for multi-printer FDM farms.

Provides immediate printer shutdown capability across all registered
printers.  The :class:`EmergencyCoordinator` is the central coordinator
-- it tracks interlock states, records stop events, and enforces the
invariant that a printer cannot resume until all critical interlocks are
re-engaged.

For FDM printers the emergency sequence is:

1. **M112** — firmware-level emergency stop (immediate halt).
2. **M104 S0** — turn off hotend heater.
3. **M140 S0** — turn off heated bed.
4. **M84** — disable stepper motors.

Thread safety is guaranteed via :class:`threading.Lock` on all mutable
state.  The module-level :func:`emergency_stop` and
:func:`emergency_stop_all` convenience functions delegate to a lazy
singleton coordinator.

Usage::

    from kiln.emergency import emergency_stop, emergency_stop_all

    # Stop a single printer
    result = emergency_stop("voron-350", reason=EmergencyReason.THERMAL_RUNAWAY)

    # Stop ALL printers
    results = emergency_stop_all(reason=EmergencyReason.POWER_ANOMALY)
"""

from __future__ import annotations

import enum
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_PERSIST_SETTING_KEY = "emergency_latch_state_v1"
_PERSIST_ENABLED_ENV = "KILN_EMERGENCY_PERSIST"
_DEFAULT_DEBOUNCE_SECONDS = 2.0


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# G-code sequences for FDM emergency shutdown
# ---------------------------------------------------------------------------

_FDM_EMERGENCY_GCODE: list[str] = [
    "M112",  # Emergency stop — immediate firmware halt
    "M104 S0",  # Hotend heater off
    "M140 S0",  # Bed heater off
    "M84",  # Disable stepper motors
]

_FDM_EMERGENCY_ACTIONS: list[str] = [
    "emergency_stop_m112",
    "hotend_heater_off",
    "bed_heater_off",
    "steppers_disabled",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EmergencyReason(str, enum.Enum):
    """Reason codes for emergency stop events.

    Uses string values for JSON serialisation.
    """

    USER_REQUEST = "user_request"
    THERMAL_RUNAWAY = "thermal_runaway"
    COLLISION_DETECTED = "collision_detected"
    MATERIAL_JAM = "material_jam"
    POWER_ANOMALY = "power_anomaly"
    SOFTWARE_FAULT = "software_fault"
    INTERLOCK_BREACH = "interlock_breach"
    AGENT_REQUEST = "agent_request"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class EmergencyRecord:
    """Outcome of an emergency stop command on a single printer.

    :param printer_id: Identifier of the stopped printer.
    :param success: ``True`` if the stop was executed (or printer was
        already stopped).
    :param reason: Why the stop was triggered.
    :param timestamp: Unix timestamp when the stop was recorded.
    :param actions_taken: Printer-specific shutdown actions performed
        (e.g. ``["emergency_stop_m112", "hotend_heater_off"]``).
    :param gcode_sent: G-code commands that were sent to the printer.
    :param error: Error message if the stop could not be executed.
    """

    printer_id: str
    success: bool
    reason: EmergencyReason
    timestamp: float
    actions_taken: list[str] = field(default_factory=list)
    gcode_sent: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "printer_id": self.printer_id,
            "success": self.success,
            "reason": self.reason.value,
            "timestamp": self.timestamp,
            "actions_taken": self.actions_taken,
            "gcode_sent": self.gcode_sent,
            "error": self.error,
        }


@dataclass
class EmergencyLatch:
    """Persistent latch state for a printer emergency stop."""

    printer_id: str
    latched: bool = False
    reason: str | None = None
    source: str | None = None
    triggered_at: float | None = None
    cleared_at: float | None = None
    cleared_by: str | None = None
    ack_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer_id": self.printer_id,
            "latched": self.latched,
            "reason": self.reason,
            "source": self.source,
            "triggered_at": self.triggered_at,
            "cleared_at": self.cleared_at,
            "cleared_by": self.cleared_by,
            "ack_note": self.ack_note,
        }


@dataclass
class SafetyInterlock:
    """State of a single safety interlock on a printer.

    :param name: Human-readable interlock name (e.g. ``"enclosure_closed"``).
    :param printer_id: Printer this interlock belongs to.
    :param is_engaged: ``True`` when the interlock condition is satisfied.
    :param is_critical: If ``True``, the printer MUST stop immediately
        when this interlock disengages.
    :param last_checked: Unix timestamp of the most recent state update.
    """

    name: str
    printer_id: str
    is_engaged: bool
    is_critical: bool
    last_checked: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "name": self.name,
            "printer_id": self.printer_id,
            "is_engaged": self.is_engaged,
            "is_critical": self.is_critical,
            "last_checked": self.last_checked,
        }


# ---------------------------------------------------------------------------
# Emergency coordinator
# ---------------------------------------------------------------------------


class EmergencyCoordinator:
    """Central coordinator for emergency stops and safety interlocks.

    All public methods are thread-safe.  The coordinator maintains four
    pieces of mutable state protected by a single lock:

    - **interlocks** -- ``{(printer_id, name): SafetyInterlock}``
    - **stop_history** -- ordered list of :class:`EmergencyRecord`
    - **stopped_printers** -- set of printer IDs currently in E-stop state
    - **latches** -- per-printer persistent latch metadata
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._interlocks: dict[tuple[str, str], SafetyInterlock] = {}
        self._stop_history: list[EmergencyRecord] = []
        self._stopped_printers: set[str] = set()
        self._latches: dict[str, EmergencyLatch] = {}
        self._debounce_seconds = self._resolve_debounce_seconds()
        self._persist_enabled = os.environ.get(_PERSIST_ENABLED_ENV, "1").strip().lower() not in ("0", "false", "no")
        self._load_persisted_state()

    def _resolve_debounce_seconds(self) -> float:
        raw = os.environ.get("KILN_EMERGENCY_DEBOUNCE_SECONDS", "").strip()
        if not raw:
            return _DEFAULT_DEBOUNCE_SECONDS
        try:
            value = float(raw)
        except ValueError:
            logger.warning("Invalid KILN_EMERGENCY_DEBOUNCE_SECONDS=%r, using %.1fs", raw, _DEFAULT_DEBOUNCE_SECONDS)
            return _DEFAULT_DEBOUNCE_SECONDS
        return max(0.0, value)

    def _load_persisted_state(self) -> None:
        """Restore latched state from persistence (best effort)."""
        if not self._persist_enabled:
            return
        try:
            from kiln.persistence import get_db

            raw = get_db().get_setting(_PERSIST_SETTING_KEY, "")
            if not raw:
                return
            payload = json.loads(raw)
        except Exception as exc:
            logger.debug("Emergency state load skipped: %s", exc)
            return

        if not isinstance(payload, dict):
            return

        with self._lock:
            stopped = payload.get("stopped_printers", [])
            if isinstance(stopped, list):
                self._stopped_printers = {str(x) for x in stopped if str(x).strip()}

            raw_latches = payload.get("latches", {})
            if isinstance(raw_latches, dict):
                for printer_id, item in raw_latches.items():
                    if not isinstance(item, dict):
                        continue
                    pid = str(printer_id).strip()
                    if not pid:
                        continue
                    self._latches[pid] = EmergencyLatch(
                        printer_id=pid,
                        latched=bool(item.get("latched", False)),
                        reason=str(item.get("reason")) if item.get("reason") is not None else None,
                        source=str(item.get("source")) if item.get("source") is not None else None,
                        triggered_at=_as_float(item.get("triggered_at")),
                        cleared_at=_as_float(item.get("cleared_at")),
                        cleared_by=str(item.get("cleared_by")) if item.get("cleared_by") is not None else None,
                        ack_note=str(item.get("ack_note")) if item.get("ack_note") is not None else None,
                    )
            # Keep stopped set and latch states consistent.
            for printer_id in list(self._stopped_printers):
                latch = self._latches.get(printer_id)
                if latch is None:
                    self._latches[printer_id] = EmergencyLatch(printer_id=printer_id, latched=True)
                else:
                    latch.latched = True
            for printer_id, latch in self._latches.items():
                if latch.latched:
                    self._stopped_printers.add(printer_id)

    def _persist_state_locked(self) -> None:
        """Persist stopped/latch state (requires self._lock)."""
        if not self._persist_enabled:
            return
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "stopped_printers": sorted(self._stopped_printers),
            "latches": {pid: latch.to_dict() for pid, latch in self._latches.items()},
        }
        try:
            from kiln.persistence import get_db

            get_db().set_setting(_PERSIST_SETTING_KEY, json.dumps(payload, sort_keys=True))
        except Exception as exc:
            logger.debug("Emergency state persist skipped: %s", exc)

    def _ensure_latch_locked(self, printer_id: str) -> EmergencyLatch:
        latch = self._latches.get(printer_id)
        if latch is None:
            latch = EmergencyLatch(printer_id=printer_id)
            self._latches[printer_id] = latch
        return latch

    def _critical_interlock_blockers_locked(self, printer_id: str) -> list[str]:
        blockers: list[str] = []
        for (pid, _), interlock in self._interlocks.items():
            if pid == printer_id and interlock.is_critical and not interlock.is_engaged:
                blockers.append(interlock.name)
        return sorted(blockers)

    def _latch_status_locked(self, printer_id: str) -> dict[str, Any]:
        latch = self._ensure_latch_locked(printer_id)
        blockers = self._critical_interlock_blockers_locked(printer_id)
        return {
            **latch.to_dict(),
            "latched": printer_id in self._stopped_printers or latch.latched,
            "critical_interlocks_pending": blockers,
            "all_critical_interlocks_engaged": len(blockers) == 0,
        }

    # -- stop operations ---------------------------------------------------

    def emergency_stop(
        self,
        printer_id: str,
        *,
        reason: EmergencyReason = EmergencyReason.USER_REQUEST,
        source: str = "unknown",
        note: str | None = None,
    ) -> EmergencyRecord:
        """Execute an immediate emergency stop on a single printer.

        Sends FDM emergency G-code (M112, heater off, steppers off),
        records the event, and marks the printer as stopped.  Emits a
        :class:`~kiln.events.EventType` event if the events system is
        importable.

        :param printer_id: Identifier of the printer to stop.
        :param reason: Why the stop was triggered.
        :param source: Human/agent/system source label for audit context.
        :param note: Optional short note for the latch record.
        :returns: :class:`EmergencyRecord` describing the outcome.
        """
        printer_id = printer_id.strip()
        if not printer_id:
            raise ValueError("printer_id is required")

        now = time.time()
        gcode_sent: list[str] = []
        actions: list[str] = []
        error: str | None = None

        with self._lock:
            existing = self._latches.get(printer_id)
            already_latched = printer_id in self._stopped_printers
            last_trigger = existing.triggered_at if existing is not None else None

        if already_latched and last_trigger is not None and (now - last_trigger) <= self._debounce_seconds:
            # Debounce repeated stop requests from digital buttons or retries.
            record = EmergencyRecord(
                printer_id=printer_id,
                success=True,
                reason=reason,
                timestamp=now,
                actions_taken=["already_stopped"],
                gcode_sent=[],
                error=None,
            )
            with self._lock:
                latch = self._ensure_latch_locked(printer_id)
                latch.latched = True
                latch.reason = reason.value
                latch.source = source
                if note:
                    latch.ack_note = note
                self._stop_history.append(record)
                self._persist_state_locked()
            logger.warning(
                "EMERGENCY STOP (debounced): printer=%s reason=%s source=%s",
                printer_id,
                reason.value,
                source,
            )
            if source and source != "unknown":
                self._emit_event(printer_id, reason, ["already_stopped"], source=source)
            else:
                self._emit_event(printer_id, reason, ["already_stopped"])
            return record

        # Attempt to send emergency G-code via the printer adapter.
        try:
            gcode_sent, actions = self._send_emergency_gcode(printer_id)
        except Exception as exc:
            # Even if G-code delivery fails, we still record the stop
            # and mark the printer as stopped — the physical state is
            # indeterminate and must be treated as halted.
            error = f"G-code delivery failed: {exc}"
            actions = list(_FDM_EMERGENCY_ACTIONS)
            logger.error(
                "Failed to deliver emergency G-code to %s: %s",
                printer_id,
                exc,
            )

        record = EmergencyRecord(
            printer_id=printer_id,
            success=error is None,
            reason=reason,
            timestamp=now,
            actions_taken=actions,
            gcode_sent=gcode_sent,
            error=error,
        )

        with self._lock:
            self._stopped_printers.add(printer_id)
            latch = self._ensure_latch_locked(printer_id)
            latch.latched = True
            latch.reason = reason.value
            latch.source = source
            latch.triggered_at = now
            latch.cleared_at = None
            latch.cleared_by = None
            if note:
                latch.ack_note = note
            self._stop_history.append(record)
            self._persist_state_locked()

        logger.warning(
            "EMERGENCY STOP: printer=%s reason=%s source=%s actions=%s",
            printer_id,
            reason.value,
            source,
            actions,
        )

        if source and source != "unknown":
            self._emit_event(printer_id, reason, actions, source=source)
        else:
            self._emit_event(printer_id, reason, actions)

        return record

    def emergency_stop_all(
        self,
        *,
        reason: EmergencyReason = EmergencyReason.USER_REQUEST,
        source: str = "unknown",
        note: str | None = None,
    ) -> list[EmergencyRecord]:
        """Execute an immediate emergency stop on ALL known printers.

        "Known printers" includes any printer that has registered
        interlocks, has been previously stopped, or is present in the
        printer registry.  If no printers are known, returns an empty
        list.

        :param reason: Why the stop was triggered.
        :param source: Human/agent/system source label for audit context.
        :param note: Optional short note for latch records.
        :returns: List of :class:`EmergencyRecord` for each printer.
        """
        printer_ids: set[str] = set()

        with self._lock:
            printer_ids.update(self._stopped_printers)
            for key in self._interlocks:
                printer_ids.add(key[0])

        # Also pull from the registry if available.
        try:
            from kiln.server import _registry as registry

            printer_ids.update(registry.list_names())
        except ImportError:
            pass

        results: list[EmergencyRecord] = []
        for printer_id in sorted(printer_ids):
            results.append(self.emergency_stop(printer_id, reason=reason, source=source, note=note))
        return results

    # -- interlock management ----------------------------------------------

    def register_interlock(self, interlock: SafetyInterlock) -> None:
        """Register or update a safety interlock for a printer.

        :param interlock: The interlock to register.
        """
        key = (interlock.printer_id, interlock.name)
        with self._lock:
            self._interlocks[key] = interlock
            self._persist_state_locked()
        logger.info(
            "Interlock registered: printer=%s name=%s critical=%s engaged=%s",
            interlock.printer_id,
            interlock.name,
            interlock.is_critical,
            interlock.is_engaged,
        )

    def update_interlock(
        self,
        printer_id: str,
        name: str,
        *,
        is_engaged: bool,
        source: str = "interlock",
    ) -> None:
        """Update the engaged state of a registered interlock.

        If a **critical** interlock transitions to disengaged, an
        emergency stop is automatically triggered with
        :attr:`EmergencyReason.INTERLOCK_BREACH`.

        :param printer_id: Printer the interlock belongs to.
        :param name: Interlock name as registered.
        :param is_engaged: New engaged state.
        :param source: Source label for auto-triggered emergency stop.
        :raises KeyError: If the interlock is not registered.
        """
        key = (printer_id, name)
        with self._lock:
            if key not in self._interlocks:
                raise KeyError(f"Interlock '{name}' not registered for printer '{printer_id}'.")
            interlock = self._interlocks[key]
            interlock.is_engaged = is_engaged
            interlock.last_checked = time.time()
            is_critical = interlock.is_critical
            self._persist_state_locked()

        if is_critical and not is_engaged:
            logger.warning(
                "CRITICAL interlock disengaged: printer=%s interlock=%s — triggering emergency stop",
                printer_id,
                name,
            )
            self.emergency_stop(
                printer_id,
                reason=EmergencyReason.INTERLOCK_BREACH,
                source=source,
                note=f"critical interlock disengaged: {name}",
            )

    def check_interlocks(self, printer_id: str) -> list[SafetyInterlock]:
        """Return all registered interlocks for a printer.

        :param printer_id: Printer to query.
        :returns: List of :class:`SafetyInterlock` entries (may be empty).
        """
        with self._lock:
            return [il for (pid, _), il in self._interlocks.items() if pid == printer_id]

    # -- stop state queries ------------------------------------------------

    def is_stopped(self, printer_id: str) -> bool:
        """Check whether a printer is currently in emergency stop state.

        :param printer_id: Printer to check.
        :returns: ``True`` if the printer has been stopped and not yet
            cleared.
        """
        with self._lock:
            latch = self._latches.get(printer_id)
            return printer_id in self._stopped_printers or bool(latch and latch.latched)

    def get_latch_status(self, printer_id: str) -> dict[str, Any]:
        """Return emergency latch + critical interlock status for a printer."""
        with self._lock:
            return self._latch_status_locked(printer_id)

    def list_latch_statuses(
        self,
        *,
        include_unlatched: bool = False,
    ) -> list[dict[str, Any]]:
        """Return latch status for known printers."""
        with self._lock:
            known: set[str] = set(self._stopped_printers)
            known.update(self._latches.keys())
            known.update(pid for (pid, _) in self._interlocks.keys())
            rows = [self._latch_status_locked(pid) for pid in sorted(known)]
        if include_unlatched:
            return rows
        return [row for row in rows if bool(row.get("latched"))]

    def clear_stop_with_ack(
        self,
        printer_id: str,
        *,
        acknowledged_by: str | None = None,
        ack_note: str | None = None,
    ) -> dict[str, Any]:
        """Acknowledge and clear an emergency stop for a printer.

        The stop can only be cleared if ALL critical interlocks for the
        printer are currently engaged.  Returns a structured result for
        API/CLI usage.
        """
        printer_id = (printer_id or "").strip()
        if not printer_id:
            return {
                "success": False,
                "cleared": False,
                "reason": "invalid_printer_id",
                "status": None,
                "message": "printer_id is required",
            }

        actor = (acknowledged_by or "").strip() or "operator"
        note = (ack_note or "").strip() or None
        now = time.time()

        with self._lock:
            status_before = self._latch_status_locked(printer_id)
            if not bool(status_before.get("latched")):
                return {
                    "success": False,
                    "cleared": False,
                    "reason": "not_stopped",
                    "status": status_before,
                    "message": f"Printer '{printer_id}' is not latched.",
                }

            blockers = self._critical_interlock_blockers_locked(printer_id)
            if blockers:
                return {
                    "success": False,
                    "cleared": False,
                    "reason": "critical_interlocks_pending",
                    "status": status_before,
                    "message": (
                        f"Cannot clear E-stop for '{printer_id}' until critical interlocks are engaged: "
                        + ", ".join(blockers)
                    ),
                }

            self._stopped_printers.discard(printer_id)
            latch = self._ensure_latch_locked(printer_id)
            latch.latched = False
            latch.cleared_at = now
            latch.cleared_by = actor
            if note:
                latch.ack_note = note
            self._persist_state_locked()
            status_after = self._latch_status_locked(printer_id)

        logger.info(
            "E-stop cleared for printer '%s' by %s.",
            printer_id,
            actor,
        )
        self._emit_event(
            printer_id,
            EmergencyReason.USER_REQUEST,
            ["clear_emergency_stop"],
            source=f"clear:{actor}",
            event_name="emergency_clear",
        )
        return {
            "success": True,
            "cleared": True,
            "reason": "cleared",
            "status": status_after,
            "message": f"E-stop cleared for printer '{printer_id}'.",
        }

    def clear_stop(self, printer_id: str) -> bool:
        """Acknowledge and clear an emergency stop for a printer.

        The stop can only be cleared if ALL critical interlocks for the
        printer are currently engaged.  Non-critical interlocks do not
        block clearing.

        :param printer_id: Printer to clear.
        :returns: ``True`` if the stop was successfully cleared,
            ``False`` if a critical interlock is still disengaged or
            the printer was not in a stopped state.
        """
        result = self.clear_stop_with_ack(
            printer_id,
            acknowledged_by="legacy_clear_stop",
            ack_note="legacy clear_stop()",
        )
        return bool(result.get("cleared"))

    # -- history -----------------------------------------------------------

    def get_stop_history(
        self,
        *,
        printer_id: str | None = None,
        limit: int = 50,
    ) -> list[EmergencyRecord]:
        """Retrieve recent emergency stop events.

        :param printer_id: If provided, filter to this printer only.
        :param limit: Maximum number of results to return.
        :returns: List of :class:`EmergencyRecord`, most recent first.
        """
        with self._lock:
            history = list(self._stop_history)

        if printer_id is not None:
            history = [r for r in history if r.printer_id == printer_id]

        # Most recent first, capped at limit.
        return list(reversed(history))[:limit]

    # -- internal helpers --------------------------------------------------

    def _send_emergency_gcode(
        self,
        printer_id: str,
    ) -> tuple[list[str], list[str]]:
        """Send FDM emergency G-code to a printer via its adapter.

        Tries the adapter's hardware-level ``emergency_stop()`` first.
        If that fails, falls back to sending G-code commands individually
        via ``send_gcode()``.

        :returns: Tuple of (gcode_sent, actions_taken).
        :raises RuntimeError: If the printer is not found in the registry.
        :raises kiln.printers.base.PrinterError: If all delivery methods fail.
        """
        # Lazy imports — the registry may not be initialised yet at
        # module load time.
        from kiln.printers.base import PrinterError

        # Get the server's registry singleton.  If unavailable, we
        # cannot reach the printer at all.
        try:
            from kiln.server import _registry as registry
        except ImportError:
            from kiln.registry import PrinterRegistry

            registry = PrinterRegistry()

        adapter = registry.get(printer_id)

        gcode = list(_FDM_EMERGENCY_GCODE)
        actions = list(_FDM_EMERGENCY_ACTIONS)

        # Prefer the adapter's hardware-level emergency stop (M112 or
        # firmware equivalent).  This is the fastest path to halt.
        try:
            result = adapter.emergency_stop()
            if result.success:
                logger.info(
                    "Hardware emergency_stop() succeeded for %s",
                    printer_id,
                )
                return gcode, actions
            # If the adapter reports failure, fall through to G-code.
            logger.warning(
                "Hardware emergency_stop() reported failure for %s: %s — falling back to G-code",
                printer_id,
                result.message,
            )
        except Exception as exc:
            logger.warning(
                "Hardware emergency_stop() raised for %s: %s — falling back to G-code",
                printer_id,
                exc,
            )

        # Fallback: send G-code commands individually so partial
        # delivery still disables heaters even if a later command fails.
        last_error: Exception | None = None
        for cmd in gcode:
            try:
                adapter.send_gcode([cmd])
            except Exception as exc:
                logger.error(
                    "Failed to send G-code %r to %s: %s",
                    cmd,
                    printer_id,
                    exc,
                )
                last_error = exc

        if last_error is not None:
            raise PrinterError(
                f"Partial G-code delivery failure for {printer_id}",
                cause=last_error,
            )

        return gcode, actions

    def _emit_event(
        self,
        printer_id: str,
        reason: EmergencyReason,
        actions: list[str],
        *,
        source: str = "unknown",
        event_name: str = "emergency_stop",
    ) -> None:
        """Best-effort event emission.  Never raises."""
        try:
            from kiln.events import Event, EventBus, EventType

            event = Event(
                type=EventType.SAFETY_ESCALATED,
                data={
                    "printer_id": printer_id,
                    "reason": reason.value,
                    "actions": actions,
                    "event": event_name,
                    "source": source,
                },
                source=f"emergency:{printer_id}",
            )

            # Try to use the server's shared event bus so subscribers
            # (webhooks, scheduler, persistence) receive the event.
            bus: EventBus | None = None
            try:
                from kiln.server import _event_bus as server_bus

                bus = server_bus
            except ImportError:
                pass

            if bus is not None:
                bus.publish(event)
                logger.debug(
                    "Emergency event published for %s",
                    printer_id,
                )
            else:
                logger.debug(
                    "No event bus available — emergency event for %s not published",
                    printer_id,
                )
        except Exception:
            # Best-effort: never let event emission break the stop flow.
            logger.debug(
                "Failed to emit emergency event for %s",
                printer_id,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Module-level singleton and convenience functions
# ---------------------------------------------------------------------------

_coordinator: EmergencyCoordinator | None = None
_coordinator_lock = threading.Lock()


def get_emergency_coordinator() -> EmergencyCoordinator:
    """Return the module-level :class:`EmergencyCoordinator` singleton.

    The coordinator is created lazily on first access.
    """
    global _coordinator
    if _coordinator is None:
        with _coordinator_lock:
            if _coordinator is None:
                _coordinator = EmergencyCoordinator()
    return _coordinator


def emergency_stop(
    printer_id: str,
    *,
    reason: EmergencyReason = EmergencyReason.USER_REQUEST,
    source: str = "unknown",
    note: str | None = None,
) -> EmergencyRecord:
    """Convenience wrapper: stop a single printer via the global coordinator.

    :param printer_id: Identifier of the printer to stop.
    :param reason: Why the stop was triggered.
    :returns: :class:`EmergencyRecord`.
    """
    if source == "unknown" and note is None:
        return get_emergency_coordinator().emergency_stop(
            printer_id,
            reason=reason,
        )
    return get_emergency_coordinator().emergency_stop(
        printer_id,
        reason=reason,
        source=source,
        note=note,
    )


def emergency_stop_all(
    *,
    reason: EmergencyReason = EmergencyReason.USER_REQUEST,
    source: str = "unknown",
    note: str | None = None,
) -> list[EmergencyRecord]:
    """Convenience wrapper: stop ALL known printers via the global coordinator.

    :param reason: Why the stop was triggered.
    :returns: List of :class:`EmergencyRecord`.
    """
    if source == "unknown" and note is None:
        return get_emergency_coordinator().emergency_stop_all(reason=reason)
    return get_emergency_coordinator().emergency_stop_all(reason=reason, source=source, note=note)


def emergency_status(printer_id: str) -> dict[str, Any]:
    """Return emergency latch status for a single printer."""
    return get_emergency_coordinator().get_latch_status(printer_id)


def clear_emergency_stop(
    printer_id: str,
    *,
    acknowledged_by: str | None = None,
    ack_note: str | None = None,
) -> dict[str, Any]:
    """Acknowledge and clear emergency stop state for a printer."""
    return get_emergency_coordinator().clear_stop_with_ack(
        printer_id,
        acknowledged_by=acknowledged_by,
        ack_note=ack_note,
    )
