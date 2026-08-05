"""In-process background watchdog for active prints.

Agent-driven polling (~60s between MCP calls) is not fast enough to catch
a clog, a thermal runaway, or a nozzle crash before damage occurs.  The
:class:`PrintWatchdog` runs inside the Kiln MCP server as a daemon thread,
polls the printer every few seconds, and triggers an immediate
:meth:`emergency_stop` when any of the configured red flags fire.

Design notes:

* Sync threading — the MCP server is sync-threaded, so we use
  ``threading.Thread(daemon=True)`` with a ``threading.Event`` for stop
  signalling.  No asyncio.
* Testable — all time reads go through an injectable clock
  (:attr:`time_fn`), and :meth:`step` performs one poll cycle
  synchronously so unit tests never spawn real threads.
* Idempotent trip — once a red flag fires, e-stop is called exactly once
  and the watchdog puts itself to sleep.  Subsequent :meth:`status`
  calls still report the trip.

Red flags (any triggers e-stop):

* ``state.print_error`` non-zero
* ``state.hms_code`` (or equivalent) matches a blocklist entry
* Tool temperature drops > 30°C below setpoint, after it first reaches it
* Bed temperature drops > 15°C below setpoint, after it first reaches it
* A heater stops climbing while still below its setpoint
* No layer progress for > 90 seconds while printing and not heating
* Printer reports any configured HMS blocklist code

Yellow flags (logged, no e-stop):

* WiFi signal weaker than -80 dBm
* Chamber fan stalled (speed reported as 0 while printing)
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Tunable thresholds.  Named constants — no magic numbers inline.
# --------------------------------------------------------------------------

#: Tool-temp drop (°C below setpoint) that triggers e-stop.  30°C is well
#: outside normal PID wobble but well inside "nozzle clogged, heat creep,
#: or thermistor disconnected" territory.
DEFAULT_TOOL_DROP_C: float = 30.0

#: Bed-temp drop (°C below setpoint) that triggers e-stop.  Beds have
#: more thermal mass so the threshold is tighter.
DEFAULT_BED_DROP_C: float = 15.0

#: Seconds without layer or completion progress before we trip.
DEFAULT_STALL_SECONDS: float = 90.0

#: How often the watchdog polls the printer, in seconds.
DEFAULT_POLL_INTERVAL: float = 2.5

#: WiFi threshold for yellow-flag logging (dBm, more negative == weaker).
WIFI_WARN_DBM: int = -80

#: Minimum target temperature considered "actually heating" — below this
#: the heater is off or cooling, and drops below setpoint are expected.
MIN_ACTIVE_TARGET_C: float = 30.0

#: How close to setpoint a heater must come to count as having reached it.
#: Comfortably wider than steady-state PID wobble.
REACHED_MARGIN_C: float = 5.0

#: Temperature rise that counts as a heater still climbing.  Wider than
#: sensor noise and than the resolution printers report in.
HEATING_RISE_C: float = 1.0

#: How long a heater may show no such rise, while further below setpoint
#: than the drop threshold, before the gap counts as a fault.  A heater
#: climbing slowly is fine at any speed; one that has stopped is not.
DEFAULT_NO_RISE_TIMEOUT_S: float = 120.0


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------


@dataclass
class Flag:
    """A single red or yellow flag event observed by the watchdog."""

    kind: str  #: "red" or "yellow"
    rule: str  #: short identifier, e.g. "tool_drop", "stalled_layer"
    message: str
    timestamp: float
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "rule": self.rule,
            "message": self.message,
            "timestamp": self.timestamp,
            "context": dict(self.context),
        }


# --------------------------------------------------------------------------
# Watchdog
# --------------------------------------------------------------------------


class PrintWatchdog:
    """Background thread that polls a printer and triggers e-stop on anomalies.

    Args:
        adapter: Any object with ``get_state()``, ``get_job()``, and
            ``emergency_stop()`` methods (see :class:`PrinterAdapter`).
        poll_interval_sec: Seconds between polls when running as a thread.
        on_anomaly: Optional callback invoked with the triggering
            :class:`Flag` when a red flag fires.  Exceptions in the
            callback are logged and swallowed.
        hms_blocklist: HMS codes that trigger e-stop when the printer
            reports them.  Compared case-insensitively against
            ``state.hms_code`` and ``state.print_error`` (formatted hex).
        tool_drop_c: Override for tool-temp drop threshold.
        bed_drop_c: Override for bed-temp drop threshold.
        stall_seconds: Override for layer-stall timeout.
        no_rise_timeout_s: Override for how long a warming heater may
            show no temperature rise before the gap counts as a failure.
        time_fn: Injectable clock for deterministic testing.  Defaults
            to :func:`time.monotonic`.
    """

    def __init__(
        self,
        adapter: Any,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL,
        on_anomaly: Callable[[Flag], None] | None = None,
        hms_blocklist: list[str] | None = None,
        *,
        tool_drop_c: float = DEFAULT_TOOL_DROP_C,
        bed_drop_c: float = DEFAULT_BED_DROP_C,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        no_rise_timeout_s: float = DEFAULT_NO_RISE_TIMEOUT_S,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._adapter = adapter
        self._poll_interval = max(0.1, float(poll_interval_sec))
        self._on_anomaly = on_anomaly
        self._hms_blocklist = {c.strip().upper() for c in (hms_blocklist or []) if c}
        self._tool_drop_c = float(tool_drop_c)
        self._bed_drop_c = float(bed_drop_c)
        self._stall_seconds = float(stall_seconds)
        self._no_rise_timeout_s = float(no_rise_timeout_s)
        self._time = time_fn

        # Threading primitives.
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Observed state.
        self.anomaly_triggered: bool = False
        self._last_completion: float | None = None
        self._last_layer: int | None = None
        self._last_progress_time: float | None = None
        self._last_state: Any = None
        self._last_job: Any = None
        self._flags: list[Flag] = []

        # Warmup tracking: a drop only counts once its heater has arrived.
        self._tool_reached = False
        self._tool_target_prev: float | None = None
        self._tool_rise_ref: tuple[float, float] | None = None
        self._bed_reached = False
        self._bed_target_prev: float | None = None
        self._bed_rise_ref: tuple[float, float] | None = None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the background polling thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.debug("PrintWatchdog already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="kiln-print-watchdog",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "PrintWatchdog started (poll=%.1fs, tool_drop=%.0f°C, "
            "bed_drop=%.0f°C, stall=%.0fs, hms_blocklist=%d)",
            self._poll_interval,
            self._tool_drop_c,
            self._bed_drop_c,
            self._stall_seconds,
            len(self._hms_blocklist),
        )

    def stop(self, timeout: float | None = None) -> None:
        """Signal the thread to exit and join it.

        Safe to call even if :meth:`start` was never invoked.
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout if timeout is not None else self._poll_interval * 2.0)
        self._thread = None
        logger.info("PrintWatchdog stopped")

    def status(self) -> dict[str, Any]:
        """Return the latest poll snapshot and a history of flags seen."""
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "anomaly_triggered": self.anomaly_triggered,
                "last_completion": self._last_completion,
                "last_layer": self._last_layer,
                "last_progress_time": self._last_progress_time,
                "flags": [f.to_dict() for f in self._flags],
                "red_flags": [f.to_dict() for f in self._flags if f.kind == "red"],
                "yellow_flags": [f.to_dict() for f in self._flags if f.kind == "yellow"],
            }

    # ------------------------------------------------------------------
    # Single-step entry point — the core of the watchdog.
    # ------------------------------------------------------------------

    def step(self) -> Flag | None:
        """Perform one poll cycle.  Returns the red flag that fired, if any.

        Factored out of :meth:`_run_loop` so tests can drive the watchdog
        deterministically without spawning threads.
        """
        # Once tripped, do nothing — don't spam e-stop.
        if self.anomaly_triggered:
            return None

        try:
            state = self._adapter.get_state()
        except Exception:
            logger.exception("PrintWatchdog: get_state() failed; skipping tick")
            return None

        try:
            job = self._adapter.get_job()
        except Exception:
            # Job info is optional — a stall check just won't fire without it.
            job = None

        with self._lock:
            self._last_state = state
            self._last_job = job

        # --- Red flags ------------------------------------------------
        red = self._evaluate_red_flags(state, job)
        if red is not None:
            self._trip(red)
            return red

        # --- Yellow flags ---------------------------------------------
        for yellow in self._evaluate_yellow_flags(state):
            self._record_flag(yellow)
            logger.warning("PrintWatchdog yellow: %s", yellow.message)

        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Daemon-thread entry point."""
        while not self._stop_event.is_set():
            try:
                self.step()
            except Exception:
                logger.exception("PrintWatchdog: unexpected error in step()")
            # If tripped, idle out until stop() is called — don't spam.
            interval = self._poll_interval if not self.anomaly_triggered else max(
                self._poll_interval, 5.0
            )
            self._stop_event.wait(timeout=interval)

    def _evaluate_red_flags(self, state: Any, job: Any) -> Flag | None:
        """Return the first red flag that fires this tick, or ``None``."""
        now = self._time()

        # --- print_error (HMS numeric) -------------------------------
        print_error = _getattr(state, "print_error")
        if print_error:
            return Flag(
                kind="red",
                rule="print_error",
                message=f"Printer reported print_error={print_error} (hex: {int(print_error):08X})",
                timestamp=now,
                context={"print_error": int(print_error)},
            )

        # --- HMS blocklist match -------------------------------------
        hms_code = _getattr(state, "hms_code")
        if hms_code and self._hms_blocklist:
            as_str = str(hms_code).strip().upper()
            if as_str in self._hms_blocklist:
                return Flag(
                    kind="red",
                    rule="hms_blocklist",
                    message=f"Printer reported HMS code {as_str} (on blocklist)",
                    timestamp=now,
                    context={"hms_code": as_str},
                )
        # Also match numeric print_error formatted as hex (Bambu style).
        if print_error is not None and self._hms_blocklist:
            as_hex = f"{int(print_error):08X}"
            if as_hex in self._hms_blocklist:
                return Flag(
                    kind="red",
                    rule="hms_blocklist",
                    message=f"Printer print_error hex {as_hex} is on HMS blocklist",
                    timestamp=now,
                    context={"hms_code": as_hex},
                )

        # Only run the remaining checks if the printer is actively printing.
        if not _is_printing(state):
            # Reset progress tracking so a stall timer doesn't accumulate
            # while paused / idle.
            self._last_completion = None
            self._last_layer = None
            self._last_progress_time = now
            # Clear warmup tracking so the next print checks afresh.
            self._tool_reached = False
            self._tool_target_prev = None
            self._tool_rise_ref = None
            self._bed_reached = False
            self._bed_target_prev = None
            self._bed_rise_ref = None
            return None

        # --- Tool temperature drop -----------------------------------
        tool_actual = _getattr(state, "tool_temp_actual")
        tool_target = _getattr(state, "tool_temp_target")
        tool_warming = False
        if (
            tool_actual is not None
            and tool_target is not None
            and tool_target >= MIN_ACTIVE_TARGET_C
        ):
            if self._tool_target_prev != tool_target:
                # New setpoint: the heater has to climb to it again.
                self._tool_target_prev = tool_target
                self._tool_reached = False
            if tool_actual >= tool_target - REACHED_MARGIN_C:
                self._tool_reached = True
                self._tool_rise_ref = None
            tool_warming = not self._tool_reached
            drop = tool_target - tool_actual
            if drop >= self._tool_drop_c and self._tool_reached:
                return Flag(
                    kind="red",
                    rule="tool_drop",
                    message=(
                        f"Hotend dropped {drop:.1f}°C below setpoint "
                        f"({tool_actual:.1f}°C vs {tool_target:.0f}°C target) "
                        f"— likely clog or heater failure"
                    ),
                    timestamp=now,
                    context={
                        "tool_temp_actual": float(tool_actual),
                        "tool_temp_target": float(tool_target),
                        "drop_c": float(drop),
                    },
                )
            if tool_warming:
                if self._tool_rise_ref is None:
                    self._tool_rise_ref = (tool_actual, now)
                ref_c, ref_at = self._tool_rise_ref
                if tool_actual >= ref_c + HEATING_RISE_C:
                    self._tool_rise_ref = (tool_actual, now)
                elif now - ref_at >= self._no_rise_timeout_s:
                    return Flag(
                        kind="red",
                        rule="tool_not_heating",
                        message=(
                            f"Hotend stopped climbing {drop:.1f}°C below setpoint "
                            f"({tool_actual:.1f}°C vs {tool_target:.0f}°C target) "
                            f"— heater failure or thermistor fault"
                        ),
                        timestamp=now,
                        context={
                            "tool_temp_actual": float(tool_actual),
                            "tool_temp_target": float(tool_target),
                            "no_rise_seconds": float(now - ref_at),
                        },
                    )
        elif tool_target is not None and tool_target < MIN_ACTIVE_TARGET_C:
            # Heater switched off, as filament-change macros do with M104 S0.
            # The rise reference stays: a target toggling through zero must not
            # restart the timer, or a dead heater is never reported.
            self._tool_reached = False
            self._tool_target_prev = None

        # --- Bed temperature drop ------------------------------------
        bed_actual = _getattr(state, "bed_temp_actual")
        bed_target = _getattr(state, "bed_temp_target")
        bed_warming = False
        if (
            bed_actual is not None
            and bed_target is not None
            and bed_target >= MIN_ACTIVE_TARGET_C
        ):
            if self._bed_target_prev != bed_target:
                self._bed_target_prev = bed_target
                self._bed_reached = False
            if bed_actual >= bed_target - REACHED_MARGIN_C:
                self._bed_reached = True
                self._bed_rise_ref = None
            bed_warming = not self._bed_reached
            drop = bed_target - bed_actual
            if drop >= self._bed_drop_c and self._bed_reached:
                return Flag(
                    kind="red",
                    rule="bed_drop",
                    message=(
                        f"Bed dropped {drop:.1f}°C below setpoint "
                        f"({bed_actual:.1f}°C vs {bed_target:.0f}°C target)"
                    ),
                    timestamp=now,
                    context={
                        "bed_temp_actual": float(bed_actual),
                        "bed_temp_target": float(bed_target),
                        "drop_c": float(drop),
                    },
                )
            if bed_warming:
                if self._bed_rise_ref is None:
                    self._bed_rise_ref = (bed_actual, now)
                ref_c, ref_at = self._bed_rise_ref
                if bed_actual >= ref_c + HEATING_RISE_C:
                    self._bed_rise_ref = (bed_actual, now)
                elif now - ref_at >= self._no_rise_timeout_s:
                    return Flag(
                        kind="red",
                        rule="bed_not_heating",
                        message=(
                            f"Bed stopped climbing {drop:.1f}°C below setpoint "
                            f"({bed_actual:.1f}°C vs {bed_target:.0f}°C target) "
                            f"— heater failure or thermistor fault"
                        ),
                        timestamp=now,
                        context={
                            "bed_temp_actual": float(bed_actual),
                            "bed_temp_target": float(bed_target),
                            "no_rise_seconds": float(now - ref_at),
                        },
                    )
        elif bed_target is not None and bed_target < MIN_ACTIVE_TARGET_C:
            self._bed_reached = False
            self._bed_target_prev = None

        # --- Layer / completion stall --------------------------------
        if tool_warming or bed_warming:
            # Heating blocks the G-code stream, so no progress is expected.
            self._last_progress_time = now
        progressed = self._update_progress(job, now)
        if not progressed and self._last_progress_time is not None:
            stalled = now - self._last_progress_time
            if stalled >= self._stall_seconds:
                return Flag(
                    kind="red",
                    rule="stalled_layer",
                    message=(
                        f"No layer/completion progress for {stalled:.0f}s "
                        f"while state=printing (last layer={self._last_layer}, "
                        f"last completion={self._last_completion})"
                    ),
                    timestamp=now,
                    context={
                        "stalled_seconds": float(stalled),
                        "last_layer": self._last_layer,
                        "last_completion": self._last_completion,
                    },
                )

        return None

    def _evaluate_yellow_flags(self, state: Any) -> list[Flag]:
        """Return all yellow flags firing this tick."""
        now = self._time()
        flags: list[Flag] = []

        # --- WiFi signal ----------------------------------------------
        wifi = _getattr(state, "wifi_signal")
        if wifi is not None:
            dbm = _parse_dbm(wifi)
            if dbm is not None and dbm < WIFI_WARN_DBM:
                flags.append(
                    Flag(
                        kind="yellow",
                        rule="wifi_weak",
                        message=f"WiFi signal weak: {wifi} (< {WIFI_WARN_DBM} dBm)",
                        timestamp=now,
                        context={"wifi_signal": str(wifi), "dbm": dbm},
                    )
                )

        # --- Chamber fan stalled -------------------------------------
        # Only flag if the printer is printing — a 0-speed chamber fan on
        # an idle printer is normal.
        if _is_printing(state):
            chamber_fan = _getattr(state, "chamber_fan_speed")
            if chamber_fan is not None and int(chamber_fan) == 0:
                flags.append(
                    Flag(
                        kind="yellow",
                        rule="chamber_fan_stalled",
                        message="Chamber fan reported 0 while printing",
                        timestamp=now,
                        context={"chamber_fan_speed": 0},
                    )
                )

        return flags

    def _update_progress(self, job: Any, now: float) -> bool:
        """Record progress; return True if layer or completion advanced."""
        if job is None:
            return False

        layer = _getattr(job, "current_layer")
        completion = _getattr(job, "completion")

        advanced = False
        if layer is not None and layer != self._last_layer:
            self._last_layer = int(layer)
            advanced = True
        if completion is not None and (
            self._last_completion is None or completion > self._last_completion
        ):
            self._last_completion = float(completion)
            advanced = True

        if advanced or self._last_progress_time is None:
            self._last_progress_time = now
        return advanced

    def _trip(self, flag: Flag) -> None:
        """Handle a red-flag trip: log, e-stop, callback, and latch."""
        logger.error(
            "PrintWatchdog RED FLAG [%s]: %s | context=%s",
            flag.rule,
            flag.message,
            flag.context,
        )
        self._record_flag(flag)
        with self._lock:
            self.anomaly_triggered = True

        # Emergency stop — isolated try/except so a failed e-stop still
        # fires the callback and sets the latch.
        try:
            self._adapter.emergency_stop()
            logger.error("PrintWatchdog: emergency_stop() dispatched")
        except Exception:
            logger.exception("PrintWatchdog: emergency_stop() FAILED")

        if self._on_anomaly is not None:
            try:
                self._on_anomaly(flag)
            except Exception:
                logger.exception("PrintWatchdog: on_anomaly callback raised")

    def _record_flag(self, flag: Flag) -> None:
        with self._lock:
            self._flags.append(flag)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _getattr(obj: Any, name: str) -> Any:
    """Attribute-or-key lookup — tolerant of dataclasses, objects, dicts."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _is_printing(state: Any) -> bool:
    """Return True if the printer is actively printing."""
    s = _getattr(state, "state")
    if s is None:
        return False
    # PrinterStatus enum has .value == "printing"; strings or enum-likes work.
    value = getattr(s, "value", s)
    return str(value).lower() == "printing"


def _parse_dbm(signal: Any) -> int | None:
    """Parse a wifi signal strength like ``'-72dBm'`` or ``-72`` into an int."""
    if isinstance(signal, (int, float)):
        return int(signal)
    try:
        return int(str(signal).lower().replace("dbm", "").strip())
    except (ValueError, AttributeError):
        return None
