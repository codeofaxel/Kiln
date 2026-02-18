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
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


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

    All public methods are thread-safe.  The coordinator maintains three
    pieces of mutable state protected by a single lock:

    - **interlocks** -- ``{(printer_id, name): SafetyInterlock}``
    - **stop_history** -- ordered list of :class:`EmergencyRecord`
    - **stopped_printers** -- set of printer IDs currently in E-stop state
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._interlocks: dict[tuple[str, str], SafetyInterlock] = {}
        self._stop_history: list[EmergencyRecord] = []
        self._stopped_printers: set[str] = set()

    # -- stop operations ---------------------------------------------------

    def emergency_stop(
        self,
        printer_id: str,
        *,
        reason: EmergencyReason = EmergencyReason.USER_REQUEST,
    ) -> EmergencyRecord:
        """Execute an immediate emergency stop on a single printer.

        Sends FDM emergency G-code (M112, heater off, steppers off),
        records the event, and marks the printer as stopped.  Emits a
        :class:`~kiln.events.EventType` event if the events system is
        importable.

        :param printer_id: Identifier of the printer to stop.
        :param reason: Why the stop was triggered.
        :returns: :class:`EmergencyRecord` describing the outcome.
        """
        now = time.time()
        gcode_sent: list[str] = []
        actions: list[str] = []
        error: str | None = None

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
            self._stop_history.append(record)

        logger.warning(
            "EMERGENCY STOP: printer=%s reason=%s actions=%s",
            printer_id,
            reason.value,
            actions,
        )

        self._emit_event(printer_id, reason, actions)

        return record

    def emergency_stop_all(
        self,
        *,
        reason: EmergencyReason = EmergencyReason.USER_REQUEST,
    ) -> list[EmergencyRecord]:
        """Execute an immediate emergency stop on ALL known printers.

        "Known printers" includes any printer that has registered
        interlocks, has been previously stopped, or is present in the
        printer registry.  If no printers are known, returns an empty
        list.

        :param reason: Why the stop was triggered.
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
            results.append(self.emergency_stop(printer_id, reason=reason))
        return results

    # -- interlock management ----------------------------------------------

    def register_interlock(self, interlock: SafetyInterlock) -> None:
        """Register or update a safety interlock for a printer.

        :param interlock: The interlock to register.
        """
        key = (interlock.printer_id, interlock.name)
        with self._lock:
            self._interlocks[key] = interlock
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
    ) -> None:
        """Update the engaged state of a registered interlock.

        If a **critical** interlock transitions to disengaged, an
        emergency stop is automatically triggered with
        :attr:`EmergencyReason.INTERLOCK_BREACH`.

        :param printer_id: Printer the interlock belongs to.
        :param name: Interlock name as registered.
        :param is_engaged: New engaged state.
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

        if is_critical and not is_engaged:
            logger.warning(
                "CRITICAL interlock disengaged: printer=%s interlock=%s — triggering emergency stop",
                printer_id,
                name,
            )
            self.emergency_stop(
                printer_id,
                reason=EmergencyReason.INTERLOCK_BREACH,
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
            return printer_id in self._stopped_printers

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
        with self._lock:
            if printer_id not in self._stopped_printers:
                return False

            # Check all critical interlocks are engaged.
            for (pid, _), il in self._interlocks.items():
                if pid == printer_id and il.is_critical and not il.is_engaged:
                    logger.warning(
                        "Cannot clear E-stop for '%s': critical interlock '%s' is disengaged.",
                        printer_id,
                        il.name,
                    )
                    return False

            self._stopped_printers.discard(printer_id)

        logger.info("E-stop cleared for printer '%s'.", printer_id)
        return True

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
                    "event": "emergency_stop",
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
) -> EmergencyRecord:
    """Convenience wrapper: stop a single printer via the global coordinator.

    :param printer_id: Identifier of the printer to stop.
    :param reason: Why the stop was triggered.
    :returns: :class:`EmergencyRecord`.
    """
    return get_emergency_coordinator().emergency_stop(
        printer_id,
        reason=reason,
    )


def emergency_stop_all(
    *,
    reason: EmergencyReason = EmergencyReason.USER_REQUEST,
) -> list[EmergencyRecord]:
    """Convenience wrapper: stop ALL known printers via the global coordinator.

    :param reason: Why the stop was triggered.
    :returns: List of :class:`EmergencyRecord`.
    """
    return get_emergency_coordinator().emergency_stop_all(reason=reason)
