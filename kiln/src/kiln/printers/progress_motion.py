"""Is the machine actually MAKING PROGRESS?"""

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
# ---------------------------------------------------------------------------

_key_counter = itertools.count(1)


def observation_key(adapter: Any) -> str:
    """The key everything in this module is stored under: ONE PER PRINTER."""
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

_DEFAULT_STALL_MINUTES: float = 15.0

_ENV_STALL_MINUTES = "KILN_STALL_WARN_MINUTES"


def stall_threshold_seconds() -> float:
    """The configured freeze duration, in seconds."""
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


# The only status in which frozen progress is evidence of anything
_SHOULD_BE_MOVING: frozenset[str] = frozenset({"printing"})


class Motion(enum.Enum):
    """Whether the machine is making progress."""

    #: A primary axis advanced.  Only ever set from an OBSERVED advance.
    MOVING = "moving"
    STALLED = "stalled"
    #: Not enough evidence yet, or the question does not apply.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProgressSample:
    """One observation of the two axes that mean progress, plus context."""

    #: ``layer_num`` — primary axis.
    layer: int | None = None
    percent: float | None = None
    # : Printer-reported ETA in whole minutes — the SECONDARY axis
    remaining_minutes: int | None = None
    # : The job this sample belongs to
    job_label: str | None = None
    #: Normalised :class:`~kiln.printers.base.PrinterStatus` value.
    status: str = "unknown"
    job_result: str | None = None
    at: float = 0.0

    def primaries(self) -> tuple[int | None, float | None]:
        return (self.layer, self.percent)


@dataclass(frozen=True)
class MotionVerdict:
    """The answer, plus enough detail to say it in a sentence."""

    motion: Motion
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
        """One plain-English sentence, or ``None`` when there is nothing to say."""
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
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_anchors: dict[str, ProgressSample] = {}
_latest: dict[str, ProgressSample] = {}
_stalled_keys: set[str] = set()
_job_starts: dict[str, tuple[str | None, float]] = {}
_last_looks: dict[str, float] = {}


def reset_progress_observations(adapter: Any = None) -> None:
    """Forget observations for one printer, or all of them."""
    with _lock:
        if adapter is None:
            _anchors.clear()
            _latest.clear()
            _job_starts.clear()
            _last_looks.clear()
            _stalled_keys.clear()
        else:
            key = observation_key(adapter)
            _anchors.pop(key, None)
            _latest.pop(key, None)
            _job_starts.pop(key, None)
            _last_looks.pop(key, None)
            _stalled_keys.discard(key)


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
    """Build a sample from whatever a caller is already holding."""
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
    """Judge one pair of samples."""
    common = {
        "layer": current.layer,
        "percent": current.percent,
        "status": current.status,
    }

    # A different job, or a different machine state, is a different question
    if current.job_label != anchor.job_label:
        return MotionVerdict(Motion.UNKNOWN, None, "job changed", **common)
    if current.status != anchor.status:
        return MotionVerdict(Motion.UNKNOWN, None, "state changed", **common)

    if current.primaries() != anchor.primaries():
        # Any CHANGE is motion, including a decrease
        return MotionVerdict(Motion.MOVING, 0.0, "primary axis advanced", **common)

    frozen_for = max(0.0, current.at - anchor.at)

    if current.status not in _SHOULD_BE_MOVING:
        return MotionVerdict(
            Motion.UNKNOWN, frozen_for, f"{current.status} is not expected to advance",
            **common,
        )

    if current.layer is None and current.percent is None:
        # Nothing to be frozen
        return MotionVerdict(Motion.UNKNOWN, frozen_for, "no progress fields", **common)

    if frozen_for < stall_threshold_seconds():
        return MotionVerdict(Motion.UNKNOWN, frozen_for, "within threshold", **common)

    # The ETA guard
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
    """Record one observation and return the verdict it produces."""
    try:
        key = observation_key(adapter)
        current = sample_from(state, job, now=now)
        with _lock:
            anchor = _anchors.get(key)
            previous = _latest.get(key)
            _latest[key] = current
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
            if verdict.motion is Motion.MOVING or verdict.reason in (
                "job changed", "state changed",
            ):
                _anchors[key] = current
            edge = _stall_edge(key, verdict)
        if edge is not None:
            _announce_stall_edge(edge, adapter, verdict, current)
        return verdict
    except Exception:  # noqa: BLE001 — a detector must never break its caller
        logger.debug("progress-motion observation failed", exc_info=True)
        return MotionVerdict(Motion.UNKNOWN, None, "observation failed")


def _stall_edge(key: str, verdict: MotionVerdict) -> str | None:
    """Which edge of a stall episode this verdict crosses, if any."""
    if verdict.stalled:
        if key in _stalled_keys:
            return None
        _stalled_keys.add(key)
        return "stalled"
    if key not in _stalled_keys:
        return None
    if verdict.motion is Motion.MOVING:
        _stalled_keys.discard(key)
        return "cleared"
    if verdict.reason in ("job changed", "state changed"):
        _stalled_keys.discard(key)
    return None


def _announce_stall_edge(edge: str, adapter: Any, verdict: MotionVerdict, sample: ProgressSample) -> None:
    """Say it once, on the event bus and in the log."""
    try:
        from kiln.printers.base import outcome_printer_name

        printer_name = outcome_printer_name(adapter)
    except Exception:  # noqa: BLE001
        printer_name = str(getattr(adapter, "name", None) or "printer")
    data: dict[str, Any] = {
        "printer_name": printer_name,
        "job": sample.job_label,
        "layer": verdict.layer,
        "percent": verdict.percent,
        "frozen_for_seconds": round(verdict.frozen_for_seconds or 0.0),
    }
    if edge == "stalled":
        data["note"] = verdict.note()
        logger.warning("%s: %s", printer_name, data["note"])
    else:
        logger.info("%s is moving again (layer %s, %s%%)", printer_name, verdict.layer, verdict.percent)
    try:
        import kiln.server as _srv
        from kiln.events import Event, EventType

        _srv._get_event_bus().publish(
            Event(
                type=EventType.PRINT_STALLED if edge == "stalled" else EventType.PRINT_STALL_CLEARED,
                data=data,
                source="progress_motion",
            )
        )
    except Exception:  # noqa: BLE001 — an announcement must never break the detector
        logger.debug("stall edge not published", exc_info=True)


def progress_stall_note(
    adapter: Any,
    state: Any,
    job: Any,
    *,
    now: float | None = None,
) -> str | None:
    """Observe, and return the sentence to show a user — or ``None``."""
    try:
        return observe_progress(adapter, state, job, now=now).note()
    except Exception:  # noqa: BLE001
        logger.debug("progress-motion note failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def normalize_job_label(label: Any) -> str | None:
    """Compare-able form of a job name: basename, no extension, lower case."""
    if not label:
        return None
    text = str(label).strip().replace("\\", "/")
    text = text.rsplit("/", 1)[-1]
    while "." in text[1:]:
        text = text.rsplit(".", 1)[0]
    return text.lower() or None


_normalize_job_label = normalize_job_label


def note_job_start(adapter: Any, job_label: Any, *, at: float | None = None) -> None:
    """Record that a print Kiln STARTED began now."""
    try:
        key = observation_key(adapter)
        with _lock:
            _job_starts[key] = (
                _normalize_job_label(job_label),
                time.monotonic() if at is None else at,
            )
    except Exception:  # noqa: BLE001
        logger.debug("job-start stamp failed", exc_info=True)


# : How long a gap between two status reads still counts as WATCHING
WATCHED_ENDING_MAX_GAP_S: float = 120.0


def note_status_read(adapter: Any, *, now: float | None = None) -> float | None:
    """Record that a status read just happened; return the gap since the last."""
    try:
        key = observation_key(adapter)
        stamp = time.monotonic() if now is None else now
        with _lock:
            previous = _last_looks.get(key)
            _last_looks[key] = stamp
        return None if previous is None else max(0.0, stamp - previous)
    except Exception:  # noqa: BLE001 — bookkeeping never breaks a status read
        logger.debug("status-read stamp failed", exc_info=True)
        return None


def forget_job_start(adapter: Any) -> None:
    """Drop the start stamp — the job it described is over."""
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
    """Measured seconds since this job started, or ``None`` if not known."""
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
    """Re-judge the stored samples without recording a new observation."""
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
