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

#: How long a heater may warm toward a target it has never reached before
#: the gap is judged on its own merits.
#:
#: "Still climbing" alone leaves one state with no detector: a heater that
#: keeps rising, arbitrarily slowly, toward a target it never arrives at.
#: A rise of 1°C per 119s satisfies the rule above forever, and the layer
#: stall timer is paused while warming, so nothing reports it.
#:
#: This ends the AMBIGUITY rather than delivering a verdict.  "Below
#: setpoint early in a print" is genuinely ambiguous; thirty minutes in it
#: is not, so the already-calibrated drop threshold takes over.  A heater
#: within that threshold of its target is still never flagged — that is
#: the asymptotic final approach, where patience is correct.  Getting this
#: number wrong by a factor of two therefore shifts WHEN ambiguity ends,
#: never WHAT counts as broken.  It is a policy choice about how long to
#: tolerate not knowing; it is not a physical constant.
DEFAULT_WARMUP_TIMEOUT_S: float = 1800.0

#: Fraction of that ceiling at which a warning is raised.  Logged and sent
#: to ``on_anomaly``; it stops nothing.
WARMUP_WARN_FRACTION: float = 0.5


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


@dataclass
class _Verdict:
    """What one heater has to say this poll."""

    red: Flag | None = None
    warning: Flag | None = None
    #: True while the heater is climbing toward a target it has not reached
    #: AND the ceiling has not expired.  The layer-stall timer is held for
    #: exactly that long: heating blocks the G-code stream, so no layer can
    #: finish, but once the ceiling ends the grace the stall rule resumes.
    warming: bool = False


class _HeaterWatch:
    """One heater's warmup state, and every verdict that depends on it.

    The hotend and the bed run the SAME rules; only the labels, the context
    keys and the drop threshold differ.  Two copies is how a fix to one
    silently misses the other, and it is why adding the ceiling to a
    copy-pasted pair would have tripled forty lines instead of adding ten.

    Honest bound, because it is easy to read more into this than it does:
    every rule here reads the temperature the PRINTER REPORTS.  If that
    number is wrong the watchdog is wrong with it, so this is not thermal
    runaway protection and cannot be — a thermistor that under-reports
    fools the firmware's protection in exactly the same way.  What the
    ceiling adds is that a reported temperature which never arrives is
    eventually treated as a fault instead of tolerated forever.
    """

    def __init__(
        self,
        *,
        prefix: str,
        label: str,
        drop_c: float,
        no_rise_timeout_s: float,
        warmup_timeout_s: float,
        drop_tail: str = "",
    ) -> None:
        self._prefix = prefix  # "tool" / "bed" — rule names and context keys
        self._label = label  # "Hotend" / "Bed" — user-facing prose
        self._drop_c = drop_c
        self._no_rise_timeout_s = no_rise_timeout_s
        self._warmup_timeout_s = warmup_timeout_s
        self._drop_tail = drop_tail
        self.reset()

    def reset(self) -> None:
        """Forget everything: a new print judges its heaters afresh."""
        self._reached = False
        self._target_prev: float | None = None
        self._rise_ref: tuple[float, float] | None = None
        self._warming_since: float | None = None
        self._warned = False
        self._grace_expired = False

    def _flag(
        self, rule: str, message: str, now: float, *, kind: str = "red", **context: float
    ) -> Flag:
        # kind is passed, never inferred from the rule name: inferring it read
        # the UNPREFIXED name and quietly minted the warning as a red flag,
        # which would have filed an incident for every slow warmup.
        return Flag(
            kind=kind,
            rule=f"{self._prefix}_{rule}",
            message=message,
            timestamp=now,
            context={
                f"{self._prefix}_temp_actual": context["actual"],
                f"{self._prefix}_temp_target": context["target"],
                **{k: v for k, v in context.items() if k not in ("actual", "target")},
            },
        )

    def evaluate(self, actual: Any, target: Any, now: float) -> _Verdict:
        if target is None:
            return _Verdict()

        if target < MIN_ACTIVE_TARGET_C:
            # Heater switched off, as filament-change macros do with M104 S0.
            # The rise reference and the warmup clock both STAY: a target
            # toggling through zero must not restart either, or a dead heater
            # is never reported and the ceiling never arrives.
            self._reached = False
            self._target_prev = None
            return _Verdict()

        if actual is None:
            # A missing reading neither arms nor disarms anything.
            return _Verdict()

        if self._target_prev != target:
            # New setpoint: the heater has to climb to it again.  The clock
            # keeps running for the same anti-evasion reason as above.
            self._target_prev = target
            self._reached = False

        if actual >= target - REACHED_MARGIN_C:
            self._reached = True
            self._rise_ref = None
            self._warming_since = None
            self._warned = False
            self._grace_expired = False

        drop = target - actual

        # The drop rule is armed once the heater has ARRIVED — or once the
        # ceiling has decided the question is no longer ambiguous.
        if drop >= self._drop_c and (self._reached or self._grace_expired):
            return _Verdict(
                red=self._flag(
                    "drop",
                    f"{self._label} dropped {drop:.1f}°C below setpoint "
                    f"({actual:.1f}°C vs {target:.0f}°C target){self._drop_tail}",
                    now,
                    actual=float(actual),
                    target=float(target),
                    drop_c=float(drop),
                )
            )

        if self._reached or self._grace_expired:
            return _Verdict()

        # --- still warming -------------------------------------------
        if self._warming_since is None:
            self._warming_since = now
        warmed_for = now - self._warming_since

        if self._rise_ref is None:
            self._rise_ref = (actual, now)
        ref_c, ref_at = self._rise_ref
        if actual >= ref_c + HEATING_RISE_C:
            self._rise_ref = (actual, now)
        elif now - ref_at >= self._no_rise_timeout_s:
            return _Verdict(
                red=self._flag(
                    "not_heating",
                    f"{self._label} stopped climbing {drop:.1f}°C below setpoint "
                    f"({actual:.1f}°C vs {target:.0f}°C target) "
                    f"— heater failure or thermistor fault",
                    now,
                    actual=float(actual),
                    target=float(target),
                    no_rise_seconds=float(now - ref_at),
                )
            )

        if warmed_for >= self._warmup_timeout_s:
            self._grace_expired = True
            if drop >= self._drop_c:
                # Its own rule and its own words: a heater that never started
                # is a different fault from one that died mid-print, and the
                # user's next move differs.
                return _Verdict(
                    red=self._flag(
                        "warmup_timeout",
                        f"{self._label} never reached setpoint: {actual:.1f}°C vs "
                        f"{target:.0f}°C target after {warmed_for / 60:.1f} min "
                        f"— heater, thermistor, or a fan cooling it faster "
                        f"than it heats",
                        now,
                        actual=float(actual),
                        target=float(target),
                        warming_seconds=float(warmed_for),
                    )
                )
            # Close enough that the ordinary drop rule can take it from here.
            return _Verdict()

        warning = None
        if not self._warned and warmed_for >= self._warmup_timeout_s * WARMUP_WARN_FRACTION:
            self._warned = True
            warning = self._flag(
                "warmup_slow",
                f"{self._label} has been warming for {warmed_for / 60:.0f} min "
                f"and is still {drop:.1f}°C below its {target:.0f}°C target",
                now,
                kind="yellow",
                actual=float(actual),
                target=float(target),
                warming_seconds=float(warmed_for),
            )
        return _Verdict(warning=warning, warming=True)


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
        warmup_timeout_s: Override for how long a heater may warm toward a
            target it has never reached before the gap is judged.  Raise it
            for a large enclosed machine in a cold room, which legitimately
            takes longer than a desktop printer.
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
        warmup_timeout_s: float = DEFAULT_WARMUP_TIMEOUT_S,
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
        # Yellow rules already reported for the current print, so a condition
        # that holds for hours is reported once rather than every poll.
        self._yellow_seen: set[str] = set()

        # One object per heater, same rules in both — a drop only counts once
        # its heater has arrived, and a heater that never arrives is judged
        # when the ceiling says the question has stopped being ambiguous.
        self._tool = _HeaterWatch(
            prefix="tool",
            label="Hotend",
            drop_c=self._tool_drop_c,
            no_rise_timeout_s=self._no_rise_timeout_s,
            warmup_timeout_s=float(warmup_timeout_s),
            drop_tail=" — likely clog or heater failure",
        )
        self._bed = _HeaterWatch(
            prefix="bed",
            label="Bed",
            drop_c=self._bed_drop_c,
            no_rise_timeout_s=self._no_rise_timeout_s,
            warmup_timeout_s=float(warmup_timeout_s),
        )

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
        # Once per rule per print.  A weak-WiFi condition holds for as long as
        # it holds, so notifying every poll would mean thousands of callbacks
        # (and thousands of recorded flags) for one fact the caller already
        # knows.  The condition is reported when it appears; it is not
        # re-reported until the next print.
        for yellow in self._evaluate_yellow_flags(state):
            if yellow.rule in self._yellow_seen:
                continue
            self._yellow_seen.add(yellow.rule)
            logger.warning("PrintWatchdog yellow: %s", yellow.message)
            self._notify(yellow)

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
            # A new print gets to hear about a condition again — "the WiFi was
            # weak on your last print" is not a useful thing to withhold.
            self._yellow_seen.clear()
            # Clear warmup tracking so the next print checks afresh.
            self._tool.reset()
            self._bed.reset()
            return None

        # --- Heater temperature and warmup ---------------------------
        # Both heaters, same rules, one implementation.  A red flag returns
        # immediately; a warning is recorded and passed on without stopping
        # anything, which it can only do because reporting and stopping are
        # separate acts.
        warming = False
        for watch, actual_key, target_key in (
            (self._tool, "tool_temp_actual", "tool_temp_target"),
            (self._bed, "bed_temp_actual", "bed_temp_target"),
        ):
            verdict = watch.evaluate(
                _getattr(state, actual_key), _getattr(state, target_key), now
            )
            if verdict.red is not None:
                return verdict.red
            if verdict.warning is not None and verdict.warning.rule not in self._yellow_seen:
                self._yellow_seen.add(verdict.warning.rule)
                logger.warning("PrintWatchdog yellow: %s", verdict.warning.message)
                self._notify(verdict.warning)
            warming = warming or verdict.warming

        # --- Layer / completion stall --------------------------------
        if warming:
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

    def _notify(self, flag: Flag) -> None:
        """Record a flag and hand it to the caller.  Stops nothing.

        Reporting and stopping are separate acts.  A yellow flag needs the
        first without the second: before this split the only route to
        ``on_anomaly`` was :meth:`_trip`, which also fires the e-stop, so
        every yellow flag went to an in-memory list that nothing reads.  A
        warning nobody receives is not a warning.
        """
        self._record_flag(flag)
        self._dispatch(flag)

    def _dispatch(self, flag: Flag) -> None:
        """Hand a flag to ``on_anomaly``; a raising callback never propagates."""
        if self._on_anomaly is not None:
            try:
                self._on_anomaly(flag)
            except Exception:
                logger.exception("PrintWatchdog: on_anomaly callback raised")

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

        # Recording stays above the e-stop and dispatch stays below it, so a
        # red flag's ordering is exactly what it has always been.
        self._dispatch(flag)

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
