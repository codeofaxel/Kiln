"""Is the machine actually MAKING PROGRESS?

One question, one implementation, read by everything that needs the answer:
the status surfaces that report it and the resume gate that acts on it.

Why this is not :func:`kiln.printers.base.describe_stale_state`
---------------------------------------------------------------
That function asks *"is the last MESSAGE old?"* and is the right question
for a push cache that has gone quiet.  It is structurally incapable of
seeing the failure this module exists for, because in that failure the
messages were **current and wrong**.

Measured on a Bambu A1 (2026-08-11).  A print was paused for 13 minutes and
resumed.  ``resume_print`` returned ``{"success": true}``, ``gcode_state``
flipped to ``RUNNING``, and telemetry stayed two seconds fresh for the next
twenty minutes.  ``print_error`` was ``0``.  Every freshness check passed.
Meanwhile ``layer_num`` stayed ``2``, ``mc_percent`` stayed ``5``, and the
printer's own screen said "Paused at 5%".  The machine had not moved since
before the pause.  A second resume was then REFUSED — "the printer isn't
paused" — because Kiln believed the state word, so the lie disabled the
recovery path as well.  Only a cancel ended it.

The two signals are complements and both are needed: a stale cache freezes
the age while the state stays plausible; a lying state keeps the age fresh
while the progress freezes.  Neither can see the other's failure.

What counts as progress
-----------------------
A field-delta experiment during genuine printing (45-second window, same
machine, same session) sorted the MQTT payload into three groups:

* **real progress** — ``layer_num`` 6→7, ``mc_percent`` 17→20;
* **motion that means nothing** — ``sequence_id`` 2260→2279 and ``msg``
  0→1, which are MQTT message counters.  They keep incrementing while the
  printer is paused, which is precisely why every freshness check passed
  during the incident;
* **noise** — nozzle and bed temperature, ``fan_gear``, ``wifi_signal``.

So the primary axes are the two in the first group and nothing else.  A
counter that advances while the machine is standing still is worse than no
signal, because it is confidently wrong.
"""

from __future__ import annotations

import enum
import itertools
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Who is being observed
# ---------------------------------------------------------------------------

_key_counter = itertools.count(1)


def observation_key(adapter: Any) -> str:
    """The key everything in this module is stored under: ONE PER PRINTER.

    Every door passes the adapter OBJECT and gets the key from here, which is
    the only reason the doors cannot disagree about what to call a printer.
    They otherwise would: the status tools know a printer by its registry
    name (``"a1"``, ``"garage"``) while an adapter knows only its family
    (``adapter.name`` is ``"bambu"`` for every Bambu ever plugged in).  Key
    the recorder one way and the reader the other and the resume gate never
    sees the samples the status tool wrote — which reinstates the exact
    lockout this work exists to remove, invisibly.

    Keying on the INSTANCE also fixes the collision the other choice hides:
    ``adapter.name`` is shared by every printer of a brand, so on a two-Bambu
    bench one machine's progress would answer for the other's.  That is not
    hypothetical — ``monitor_print``'s elapsed history carries a comment
    about having had that bug already.

    A reconnect that rebuilds the adapter starts a fresh key and therefore a
    fresh history.  That is the honest reading: a new connection has not
    watched anything yet, so its answer is ``UNKNOWN`` rather than inherited.
    """
    key = getattr(adapter, "_kiln_motion_key", None)
    if isinstance(key, str):
        return key
    label = getattr(adapter, "name", None) or "printer"
    key = f"{label}#{next(_key_counter)}"
    try:
        adapter._kiln_motion_key = key
    except Exception:  # noqa: BLE001 — an adapter that refuses attributes
        # still gets a usable answer, just no cross-call history.
        logger.debug("could not tag adapter with an observation key", exc_info=True)
    return key


# ---------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------

# How long both primary progress axes must sit frozen, while the printer
# insists it is printing, before Kiln will say so out loud.
#
# THE MEASUREMENTS THIS SITS BETWEEN (same machine, same night):
#
#   ~5 minutes   a LEGITIMATE first layer on a 70 mm solid disc held
#                layer=1, pct=2 while genuinely printing.
#   20+ minutes  the real stall held layer=2, pct=5 having printed nothing.
#
# Fifteen minutes is three times the longest legitimate freeze actually
# measured, and still leaves five minutes of margin inside the real one.
#
# It takes the QUIET end of the 10-15 minute range for two reasons.  The
# first is the cost asymmetry: a false stall warning on a healthy print is
# how a user learns to ignore the warning that matters, and the failure this
# catches is one the user can also see by looking at the machine, so a late
# warning still helps while a wrong one does lasting damage.
#
# The second is that the inputs are COARSE and a coarse input needs a long
# window to be readable at all.  ``mc_percent`` is a whole number, so on a
# 24-hour print one percent legitimately takes ~14 minutes; the ETA that
# guards this detector (see below) is whole MINUTES, so a ten-minute window
# gets at most ten ticks to judge by and a two-minute window is blind.
#
# Larger first layers than the one measured plainly exist — a full 256 mm
# bed is ~17x the area of a 70 mm disc — and on a long enough print such a
# layer could freeze both primary axes past any threshold in this range.
# That case is not held off by the timer; it is held off by the ETA guard.
_DEFAULT_STALL_MINUTES: float = 15.0

_ENV_STALL_MINUTES = "KILN_STALL_WARN_MINUTES"


def stall_threshold_seconds() -> float:
    """The configured freeze duration, in seconds.

    Read live rather than frozen at import so an operator can retune it
    without a restart, and so tests can set it without reloading modules.
    A malformed or non-positive value falls back to the default: a broken
    setting must not silently disable the detector, and must not turn it
    into a hair trigger either.
    """
    raw = os.environ.get(_ENV_STALL_MINUTES)
    if raw:
        try:
            minutes = float(raw)
            if minutes > 0:
                return minutes * 60.0
        except (TypeError, ValueError):
            pass
        logger.debug(
            "%s=%r is not a positive number; using the %s-minute default.",
            _ENV_STALL_MINUTES, raw, _DEFAULT_STALL_MINUTES,
        )
    return _DEFAULT_STALL_MINUTES * 60.0


# The only status in which frozen progress is evidence of anything.  A
# PAUSED printer is *supposed* to be frozen, and a BUSY one (Bambu's
# prepare / slicing / init) is heating, homing and calibrating — which the
# adapter's own comments record as taking 5-8 minutes with no layer to show
# for it.  Firing there would be crying wolf at the one moment the machine
# is behaving exactly as designed.
_SHOULD_BE_MOVING: frozenset[str] = frozenset({"printing"})


class Motion(enum.Enum):
    """Whether the machine is making progress.

    ``UNKNOWN`` is a first-class answer, not a failure of the other two.
    Kiln has documented history of loops that guessed when they could not
    see (``lessons_learned.md``, "The loop that only learned when watched"),
    so absence of evidence is reported as absence of evidence — never
    rounded to "fine".
    """

    #: A primary axis advanced.  Only ever set from an OBSERVED advance.
    MOVING = "moving"
    #: Both primary axes frozen past the threshold, in a state that should
    #: be moving, with nothing else to explain it.
    STALLED = "stalled"
    #: Not enough evidence yet, or the question does not apply.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProgressSample:
    """One observation of the two axes that mean progress, plus context."""

    #: ``layer_num`` — primary axis.
    layer: int | None = None
    #: ``mc_percent`` 0-100, rounded to 0.1 so a fine-grained estimator's
    #: jitter is not mistaken for progress.  0.1 % is coarse enough to be
    #: stable and fine enough that freezing it for 15 minutes would imply a
    #: 250-hour print, so the rounding cannot manufacture a stall.
    percent: float | None = None
    #: Printer-reported ETA in whole minutes — the SECONDARY axis.  Never
    #: promoted to evidence of motion (see :func:`_verdict`); used only to
    #: hold the detector quiet.
    remaining_minutes: int | None = None
    #: The job this sample belongs to.  A change means a different print,
    #: not a stalled one.
    job_label: str | None = None
    #: Normalised :class:`~kiln.printers.base.PrinterStatus` value.
    status: str = "unknown"
    #: Normalised :class:`~kiln.printers.base.JobResult` value, when the
    #: printer is reporting one.  Not part of the motion verdict; it is how
    #: the elapsed ledger below learns that a job ENDED.
    job_result: str | None = None
    #: ``time.monotonic()`` when observed.
    at: float = 0.0

    def primaries(self) -> tuple[int | None, float | None]:
        return (self.layer, self.percent)


@dataclass(frozen=True)
class MotionVerdict:
    """The answer, plus enough detail to say it in a sentence."""

    motion: Motion
    #: How long the primary axes have been frozen, when that is known.
    frozen_for_seconds: float | None = None
    #: Short machine-readable reason — for logs and tests, not for users.
    reason: str = ""
    layer: int | None = None
    percent: float | None = None
    status: str = "unknown"

    @property
    def stalled(self) -> bool:
        return self.motion is Motion.STALLED

    def note(self) -> str | None:
        """One plain-English sentence, or ``None`` when there is nothing to say.

        Only a :attr:`Motion.STALLED` verdict produces text.  ``UNKNOWN`` is
        deliberately silent on the reporting surfaces: a status read that
        appended "Kiln cannot tell whether this is moving" to every poll
        would be noise on the overwhelming majority of healthy prints, and
        noise is how the sentence that matters gets skipped.  Callers that
        need the uncertainty itself read :attr:`motion`.
        """
        if self.motion is not Motion.STALLED:
            return None
        minutes = int((self.frozen_for_seconds or 0) // 60)
        where = []
        if self.layer is not None:
            where.append(f"layer {self.layer}")
        if self.percent is not None:
            where.append(f"{self.percent:.0f}%")
        stuck_at = " and ".join(where) if where else "the same place"
        return (
            f"The printer says {self.status.upper()} and its telemetry is "
            f"current, but it has not actually moved in {minutes} minutes — "
            f"still {stuck_at}. Fresh readings are not proof of motion; a "
            f"paused machine keeps sending them. Look at the printer's own "
            f"screen. If it is paused or stopped, resume_print(force=True) "
            f"sends a resume even though Kiln has been told the printer is "
            f"running, and cancel_print ends the job."
        )


# ---------------------------------------------------------------------------
# Observation store
# ---------------------------------------------------------------------------
#
# Two samples per printer: the ANCHOR (the oldest reading in the current
# unbroken run of no-motion) and the LATEST.  Bounded by printer count, so
# there is nothing here that grows with time or with poll rate.
#
# This is a PASSIVE observer.  It records only what a caller was already
# asking for, and starts no thread and no poll of its own — normal printing
# costs exactly what it cost before.  The price of that is honest: with
# nobody looking there are no samples, and the answer is UNKNOWN.

_lock = threading.Lock()
_anchors: dict[str, ProgressSample] = {}
_latest: dict[str, ProgressSample] = {}
#: printer name → (normalised job label, ``time.monotonic()`` at start).
_job_starts: dict[str, tuple[str | None, float]] = {}


def reset_progress_observations(adapter: Any = None) -> None:
    """Forget observations for one printer, or all of them.

    Exists for tests and for the rare caller that knows the history is no
    longer about the same physical situation.
    """
    with _lock:
        if adapter is None:
            _anchors.clear()
            _latest.clear()
            _job_starts.clear()
        else:
            key = observation_key(adapter)
            _anchors.pop(key, None)
            _latest.pop(key, None)
            _job_starts.pop(key, None)


def _round_percent(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _whole_minutes(seconds: Any) -> int | None:
    try:
        if seconds is None:
            return None
        return int(float(seconds) // 60)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def sample_from(state: Any, job: Any, *, now: float | None = None) -> ProgressSample:
    """Build a sample from whatever a caller is already holding.

    Takes the objects rather than an adapter so that a caller with a
    :class:`~kiln.printers.base.PrinterState` / :class:`JobProgress` pair, a
    duck-typed shim, or a relayed ``to_dict()`` payload all produce the same
    sample instead of each growing its own field-reading rules.  Every read
    is defensive: an object missing a field yields ``None`` for that axis,
    which the verdict treats as "no evidence", never as "frozen".
    """
    def _get(obj: Any, key: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    status = _get(state, "state")
    status_value = getattr(status, "value", status)
    result = _get(state, "last_job_result")
    result_value = getattr(result, "value", result)

    return ProgressSample(
        layer=_as_int(_get(job, "current_layer")),
        percent=_round_percent(_get(job, "completion")),
        remaining_minutes=_whole_minutes(_get(job, "print_time_left_seconds")),
        job_label=(str(_get(job, "file_name")) if _get(job, "file_name") else None),
        status=str(status_value).lower() if status_value else "unknown",
        job_result=str(result_value).lower() if result_value else None,
        at=time.monotonic() if now is None else now,
    )


def _verdict(anchor: ProgressSample, current: ProgressSample) -> MotionVerdict:
    """Judge one pair of samples.  Pure — no state, no clock, no I/O."""
    common = {
        "layer": current.layer,
        "percent": current.percent,
        "status": current.status,
    }

    # A different job, or a different machine state, is a different
    # question.  Both reset the clock rather than answering with it: a
    # pause→resume is exactly when the "should be moving by now" window
    # ought to start fresh, and that is the transition the incident began
    # with.
    if current.job_label != anchor.job_label:
        return MotionVerdict(Motion.UNKNOWN, None, "job changed", **common)
    if current.status != anchor.status:
        return MotionVerdict(Motion.UNKNOWN, None, "state changed", **common)

    if current.primaries() != anchor.primaries():
        # Any CHANGE is motion, including a decrease.  A layer counter that
        # went backwards means something happened; it does not mean the
        # machine is standing still, and reading it as a stall would be the
        # detector inventing an incident.
        return MotionVerdict(Motion.MOVING, 0.0, "primary axis advanced", **common)

    frozen_for = max(0.0, current.at - anchor.at)

    if current.status not in _SHOULD_BE_MOVING:
        return MotionVerdict(
            Motion.UNKNOWN, frozen_for, f"{current.status} is not expected to advance",
            **common,
        )

    if current.layer is None and current.percent is None:
        # Nothing to be frozen.  An adapter that reports neither axis
        # cannot be judged, and saying "stalled" on the strength of two
        # missing fields would be the loudest possible guess.
        return MotionVerdict(Motion.UNKNOWN, frozen_for, "no progress fields", **common)

    if frozen_for < stall_threshold_seconds():
        return MotionVerdict(Motion.UNKNOWN, frozen_for, "within threshold", **common)

    # The ETA guard.  The printer's own countdown is a THIRD axis, coarser
    # than the other two and used in one direction only: it can hold the
    # detector quiet, never make it speak.
    #
    # That asymmetry is deliberate, because the evidence for the two axes is
    # not the same.  It is MEASURED that layer and percent both advance
    # during genuine printing and both froze during the incident, and
    # measured that the ETA also froze during the incident.  It is NOT
    # measured that the ETA keeps counting down through a legitimately long
    # layer — that is a reasonable belief about the firmware, and it is
    # exactly the kind of belief that should not be load-bearing.
    #
    # Used this way it does not have to be: if the belief is right, a huge
    # first layer on a long print is spared a false alarm; if it is wrong,
    # the ETA is frozen too and the detector fires as designed.  Either way
    # the guard can only subtract alarms, never add one.
    if (
        current.remaining_minutes is not None
        and anchor.remaining_minutes is not None
        and current.remaining_minutes != anchor.remaining_minutes
    ):
        return MotionVerdict(
            Motion.UNKNOWN, frozen_for, "eta still moving", **common,
        )

    return MotionVerdict(Motion.STALLED, frozen_for, "frozen past threshold", **common)


def observe_progress(
    adapter: Any,
    state: Any,
    job: Any,
    *,
    now: float | None = None,
) -> MotionVerdict:
    """Record one observation and return the verdict it produces.

    NEVER RAISES.  Telemetry that throws is worse than telemetry that is
    absent: this runs inside status reads and a resume gate, and neither
    may be turned into an error by a detector.  Anything unexpected becomes
    an ``UNKNOWN`` verdict and a debug log.
    """
    try:
        key = observation_key(adapter)
        current = sample_from(state, job, now=now)
        with _lock:
            anchor = _anchors.get(key)
            previous = _latest.get(key)
            _latest[key] = current
            # A job that just ENDED invalidates its start stamp, so the next
            # print does not inherit it.  Keyed on the TRANSITION into an
            # ended state rather than on merely seeing one, because a push
            # cache keeps reporting the last ending until the next print
            # replaces it — and reading that steady value as news would wipe
            # the stamp Kiln had just written for the print about to begin.
            # Reads the field the adapters already populate rather than
            # inventing a second way to tell that a print is over.
            if (
                current.job_result is not None
                and previous is not None
                and previous.job_result is None
            ):
                _job_starts.pop(key, None)
            if anchor is None:
                # One reading is a photograph, not a film.
                _anchors[key] = current
                return MotionVerdict(
                    Motion.UNKNOWN, None, "first observation",
                    layer=current.layer, percent=current.percent,
                    status=current.status,
                )
            verdict = _verdict(anchor, current)
            # The anchor advances only when the run of no-motion is broken.
            # While frozen it stays put, so the reported duration is measured
            # from the last real movement rather than from the last poll —
            # which is what makes a detector work for a caller that checks
            # twice, twenty minutes apart.
            if verdict.motion is Motion.MOVING or verdict.reason in (
                "job changed", "state changed",
            ):
                _anchors[key] = current
            return verdict
    except Exception:  # noqa: BLE001 — a detector must never break its caller
        logger.debug("progress-motion observation failed", exc_info=True)
        return MotionVerdict(Motion.UNKNOWN, None, "observation failed")


def progress_stall_note(
    adapter: Any,
    state: Any,
    job: Any,
    *,
    now: float | None = None,
) -> str | None:
    """Observe, and return the sentence to show a user — or ``None``.

    The one line every reporting surface calls, so that a stall reads the
    same on every one of them instead of each writing its own wording.
    Never raises.
    """
    try:
        return observe_progress(adapter, state, job, now=now).note()
    except Exception:  # noqa: BLE001
        logger.debug("progress-motion note failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Elapsed: measured, or absent
# ---------------------------------------------------------------------------
#
# The same "what is actually true of this job over time" question as the
# motion verdict, so it shares this module rather than growing a second
# notion of when a print began.
#
# It replaces an EXTRAPOLATION.  The Bambu adapter used to derive elapsed
# from the two coarse numbers beside it:
#
#     total_est = remaining / (1 - completion/100)
#     elapsed   = total_est - remaining
#
# which is not a measurement of anything.  Verified on hardware 2026-08-11:
# at 99 % with 1 minute remaining it produced 5940 s, and the web showed
# "1h 39m" for a print that had run about 31 minutes.  The arithmetic makes
# that inevitable — whenever remaining is one minute the elapsed in minutes
# equals the completion percentage exactly, so the 1h39m WAS the 99 %.
# ``mc_remaining_time`` is whole MINUTES, so near the end the divisor
# approaches 0.01 and the coarseness is multiplied by a hundred.
#
# The replacement measures: stamp the start Kiln witnessed, subtract.  When
# there is no witnessed start — Kiln attached to a print already running, or
# the process restarted — the answer is None, and the field is simply not
# reported.  An absent number is a fact; a fabricated one is a lie that
# also reached the community learning corpus, which records
# ``print_time_seconds`` for every print it ingests.


def _normalize_job_label(label: Any) -> str | None:
    """Compare-able form of a job name: basename, no extension, lower case.

    The two sides never spell it identically — a print is started as
    ``/sdcard/model/bracket.gcode.3mf`` and reported back as ``bracket`` or
    a plate/subtask name — so the comparison has to be loose enough to match
    the same job and strict enough to notice a different one.
    """
    if not label:
        return None
    text = str(label).strip().replace("\\", "/")
    text = text.rsplit("/", 1)[-1]
    while "." in text[1:]:
        text = text.rsplit(".", 1)[0]
    return text.lower() or None


def note_job_start(adapter: Any, job_label: Any, *, at: float | None = None) -> None:
    """Record that a print Kiln STARTED began now.

    Called from :meth:`~kiln.printers.base.PrinterAdapter.start_print`, the
    one event Kiln is guaranteed to witness because it is the one Kiln
    causes.  Never raises.
    """
    try:
        key = observation_key(adapter)
        with _lock:
            _job_starts[key] = (
                _normalize_job_label(job_label),
                time.monotonic() if at is None else at,
            )
    except Exception:  # noqa: BLE001
        logger.debug("job-start stamp failed", exc_info=True)


def forget_job_start(adapter: Any) -> None:
    """Drop the start stamp — the job it described is over.  Never raises."""
    try:
        key = observation_key(adapter)
        with _lock:
            _job_starts.pop(key, None)
    except Exception:  # noqa: BLE001
        logger.debug("job-start forget failed", exc_info=True)


def job_elapsed_seconds(
    adapter: Any,
    job_label: Any = None,
    *,
    now: float | None = None,
) -> int | None:
    """Measured seconds since this job started, or ``None`` if not known.

    ``None`` is returned — and must be reported as "not known" rather than
    filled in — whenever Kiln did not witness the start, or the printer is
    now running a job whose name does not match the one that was stamped.
    A label the printer does not report cannot contradict the stamp, so it
    does not invalidate it; a label that clearly names a different job does.

    Never raises.
    """
    try:
        key = observation_key(adapter)
        with _lock:
            stamp = _job_starts.get(key)
        if stamp is None:
            return None
        stamped_label, started_at = stamp
        current_label = _normalize_job_label(job_label)
        if (
            stamped_label is not None
            and current_label is not None
            and stamped_label != current_label
        ):
            return None
        elapsed = (time.monotonic() if now is None else now) - started_at
        return max(0, int(elapsed))
    except Exception:  # noqa: BLE001
        logger.debug("job-elapsed read failed", exc_info=True)
        return None


def latest_verdict(adapter: Any, *, now: float | None = None) -> MotionVerdict:
    """Re-judge the stored samples without recording a new observation.

    For a caller that wants the current answer but holds no fresh reading
    and must not perturb the window.  Returns ``UNKNOWN`` when the printer
    has never been observed.  Never raises.
    """
    try:
        key = observation_key(adapter)
        with _lock:
            anchor = _anchors.get(key)
            current = _latest.get(key)
        if anchor is None or current is None:
            return MotionVerdict(Motion.UNKNOWN, None, "never observed")
        return _verdict(anchor, current)
    except Exception:  # noqa: BLE001
        logger.debug("progress-motion read failed", exc_info=True)
        return MotionVerdict(Motion.UNKNOWN, None, "read failed")
