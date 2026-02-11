"""Heater watchdog — auto-cools idle printers after a configurable timeout.

Prevents heaters from being left on indefinitely when no print is active.
Runs as a daemon thread alongside the scheduler and webhook manager.

Configure via environment variable:
    KILN_HEATER_TIMEOUT  — minutes before auto-cooldown (default 30, 0=disabled)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Default idle timeout: 30 minutes.
_DEFAULT_TIMEOUT_MINUTES = 30


class HeaterWatchdog:
    """Background watchdog that turns off heaters when the printer is idle.

    The watchdog tracks when heaters were last intentionally activated.
    If the printer has active heaters (target > 0) but no print is running,
    and the timeout has elapsed, it sends cooldown commands.

    Lifecycle::

        watchdog = HeaterWatchdog(get_adapter, timeout_minutes=30)
        watchdog.start()
        ...
        watchdog.notify_heater_set()   # call when set_temperature is used
        watchdog.notify_print_started() # call when a print begins
        ...
        watchdog.stop()

    Args:
        get_adapter: Callable returning the active PrinterAdapter.
        timeout_minutes: Minutes of idle heater time before auto-cooldown.
            0 disables the watchdog.
        poll_interval: Seconds between checks.
        event_bus: Optional EventBus for emitting cooldown events.
    """

    def __init__(
        self,
        get_adapter,
        timeout_minutes: float = _DEFAULT_TIMEOUT_MINUTES,
        poll_interval: float = 30.0,
        event_bus=None,
    ) -> None:
        self._get_adapter = get_adapter
        self._timeout_seconds = timeout_minutes * 60.0
        self._poll_interval = poll_interval
        self._event_bus = event_bus
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Timestamp of last intentional heater activation (set_temperature call
        # or print start).  None means no heater activity tracked yet.
        self._last_heater_activity: Optional[float] = None

        # When a print is active, the watchdog should not intervene.
        self._print_active = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def timeout_minutes(self) -> float:
        return self._timeout_seconds / 60.0

    def notify_heater_set(self) -> None:
        """Call when set_temperature is invoked with a non-zero target."""
        with self._lock:
            self._last_heater_activity = time.monotonic()

    def notify_print_started(self) -> None:
        """Call when a print job begins."""
        with self._lock:
            self._print_active = True
            self._last_heater_activity = time.monotonic()

    def notify_print_ended(self) -> None:
        """Call when a print job completes, fails, or is cancelled."""
        with self._lock:
            self._print_active = False
            # Reset the timer — give the user time to inspect the result
            # before we cool down.
            self._last_heater_activity = time.monotonic()

    def start(self) -> None:
        """Start the watchdog background thread."""
        if self._timeout_seconds <= 0:
            logger.info("Heater watchdog disabled (timeout=0)")
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="kiln-heater-watchdog",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Heater watchdog started (timeout=%.0f min, poll=%.0fs)",
            self._timeout_seconds / 60.0,
            self._poll_interval,
        )

    def stop(self) -> None:
        """Stop the watchdog gracefully."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval * 2)
            self._thread = None
        logger.info("Heater watchdog stopped")

    def _run_loop(self) -> None:
        """Main loop — runs in daemon thread."""
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("Heater watchdog tick error")
            time.sleep(self._poll_interval)

    def _tick(self) -> None:
        """Single watchdog check."""
        with self._lock:
            # If a print is active, don't touch heaters.
            if self._print_active:
                return
            # If no heater activity has been tracked, nothing to watch.
            if self._last_heater_activity is None:
                return
            elapsed = time.monotonic() - self._last_heater_activity
            if elapsed < self._timeout_seconds:
                return
            # Copy values under lock, then release before I/O.
            last_activity = self._last_heater_activity

        # Timeout elapsed, print not active — check if heaters are actually on.
        try:
            adapter = self._get_adapter()
            state = adapter.get_state()
        except Exception:
            # Printer offline or not configured — nothing to cool down.
            return

        tool_target = getattr(state, "tool_temp_target", None) or 0
        bed_target = getattr(state, "bed_temp_target", None) or 0

        if tool_target <= 0 and bed_target <= 0:
            # Heaters already off — clear the tracker.
            with self._lock:
                # Only clear if no new activity happened while we were checking.
                if self._last_heater_activity == last_activity:
                    self._last_heater_activity = None
            return

        # Heaters are on and idle timeout exceeded — cool down.
        logger.warning(
            "Heater watchdog: auto-cooldown after %.0f min idle "
            "(tool_target=%.0f°C, bed_target=%.0f°C)",
            elapsed / 60.0,
            tool_target,
            bed_target,
        )

        cooled = []
        try:
            if tool_target > 0:
                adapter.set_tool_temp(0)
                cooled.append(f"hotend ({tool_target:.0f}°C -> 0°C)")
            if bed_target > 0:
                adapter.set_bed_temp(0)
                cooled.append(f"bed ({bed_target:.0f}°C -> 0°C)")
        except Exception:
            logger.exception("Heater watchdog: failed to send cooldown commands")
            return

        # Clear tracker after successful cooldown.
        with self._lock:
            if self._last_heater_activity == last_activity:
                self._last_heater_activity = None

        # Emit event if event bus is available.
        if self._event_bus is not None:
            try:
                from kiln.events import EventType
                self._event_bus.publish(
                    EventType.TEMPERATURE_WARNING,
                    {
                        "source": "heater_watchdog",
                        "action": "auto_cooldown",
                        "reason": f"Heaters idle for {elapsed / 60.0:.0f} min",
                        "cooled": cooled,
                    },
                )
            except Exception:
                logger.debug("Failed to emit heater watchdog event", exc_info=True)

        logger.info("Heater watchdog: cooled down %s", ", ".join(cooled))
