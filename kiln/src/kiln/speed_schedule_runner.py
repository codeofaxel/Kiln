"""Run a layer-based speed schedule on this computer, on a print that is
still the print the schedule was made for.

A schedule says which speed each range of layers prints at.  Deciding one
is a judgement -- the printer's own range, which layers a speed is unsafe
on, how a request maps onto a machine that only takes presets -- and that
judgement is made elsewhere and handed here checked.  What this module does
is the part only the computer attached to the printer can do: poll the
printer every few seconds, and when its layer crosses into a segment, set
that segment's speed, once.  It stops on its own when the print ends,
when it is asked to, or the moment the printer is found running a
different job from the one it started watching -- a schedule written for
one part must never pace another.

Nothing here decides a speed.  A segment's value is sent as it was given:
through the Bambu adapter's preset when the printer takes only presets,
otherwise as the standard feedrate command.  The adapter's own gates
(the engagement check, the command's own verdict) fire on every send.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: How often the printer is asked which layer it is on.
POLL_INTERVAL_S = 5.0
#: Presets a Bambu printer takes, by the percentage each stands for.
_BAMBU_PRESETS = ((50, "silent"), (100, "standard"), (124, "sport"), (166, "ludicrous"))
#: States in which there is no print to pace.
_ENDED = frozenset({"idle", "error", "offline", "cancelling"})


class ScheduleRefused(ValueError):
    """The schedule is not one this runner will run."""


@dataclass(frozen=True)
class Segment:
    from_layer: int
    to_layer: int
    speed_percent: int


@dataclass
class RunState:
    """What a running schedule has done so far, readable while it runs."""

    segments: tuple[Segment, ...]
    printer_name: str | None
    watching: Any = None  # the job identity the run was started on
    applied: list[tuple[int, int]] = field(default_factory=list)  # (layer, percent)
    active_segment: int | None = None
    error: str | None = None
    ended: str | None = None  # why the run stopped
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer_name": self.printer_name,
            "segments": [s.__dict__ for s in self.segments],
            "applied": [{"layer": layer, "speed_percent": pct} for layer, pct in self.applied],
            "active_segment": self.active_segment,
            "error": self.error,
            "ended": self.ended,
            "running": self.thread is not None and self.thread.is_alive(),
        }


def parse_segments(schedule: Any) -> tuple[Segment, ...]:
    """*schedule* as segments, or :class:`ScheduleRefused`: integers,
    non-negative, ordered, not overlapping, at most 64 of them, and every
    speed a percentage a printer can be asked for (10-300)."""
    if not isinstance(schedule, list) or not schedule or len(schedule) > 64:
        raise ScheduleRefused("a schedule is a list of 1 to 64 segments")
    out: list[Segment] = []
    for index, entry in enumerate(schedule):
        if not isinstance(entry, dict):
            raise ScheduleRefused(f"segment {index} is not a mapping")
        try:
            seg = Segment(int(entry["from_layer"]), int(entry["to_layer"]), int(entry["speed_percent"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ScheduleRefused(f"segment {index}: from_layer, to_layer and speed_percent must be integers ({exc})") from exc
        if isinstance(entry.get("speed_percent"), bool) or seg.from_layer < 0 or seg.to_layer < seg.from_layer:
            raise ScheduleRefused(f"segment {index}: layers must be non-negative and from_layer <= to_layer")
        if not 10 <= seg.speed_percent <= 300:
            raise ScheduleRefused(f"segment {index}: speed_percent {seg.speed_percent} is outside 10-300")
        out.append(seg)
    out.sort(key=lambda s: s.from_layer)
    for a, b in zip(out, out[1:]):
        if b.from_layer <= a.to_layer:
            raise ScheduleRefused(f"segments overlap at layer {b.from_layer}")
    return tuple(out)


def segment_for(segments: tuple[Segment, ...], layer: int) -> int | None:
    for index, seg in enumerate(segments):
        if seg.from_layer <= layer <= seg.to_layer:
            return index
    return None


def nearest_preset(percent: int) -> str:
    """The Bambu preset nearest *percent*."""
    return min(_BAMBU_PRESETS, key=lambda p: abs(p[0] - percent))[1]


def set_speed(adapter: Any, percent: int) -> Any:
    """Send *percent* to the printer the one way it takes a speed: a preset
    on a printer that only has presets, else the standard feedrate command.
    Returns the adapter's own verdict."""
    if hasattr(adapter, "set_speed_profile") and not getattr(
        getattr(adapter, "capabilities", None), "can_send_gcode", True,
    ):
        return adapter.set_speed_profile(nearest_preset(percent))
    if hasattr(adapter, "set_speed_profile") and type(adapter).__name__.lower().startswith("bambu"):
        return adapter.set_speed_profile(nearest_preset(percent))
    return adapter.send_gcode([f"M220 S{percent}"])


def _identity(job: Any) -> Any:
    """What the printer is printing, as a comparable identity."""
    from kiln.printers import job_identity

    try:
        return job_identity.resolve(job)
    except Exception:  # noqa: BLE001 — a job that answers nothing
        return None


def _state_word(adapter: Any) -> str:
    state = adapter.get_state()
    status = getattr(state, "effective_state", None) or getattr(state, "state", None)
    return str(getattr(status, "value", status) or "unknown")


def _tick(run: RunState, adapter: Any) -> bool:
    """One poll -- one state read, one job read: ``False`` when the run is over."""
    from kiln.printers import job_identity

    if _state_word(adapter) in _ENDED:
        run.ended = "the print ended"
        return False
    job = adapter.get_job()
    # A DIFFERENT verdict is positive evidence of another print; an
    # UNKNOWN one (a backend that names no job) is not a reason to stop.
    if job_identity.compare(run.watching, _identity(job)) == job_identity.DIFFERENT:
        run.ended = "the printer is running a different print from the one this schedule was made for"
        return False
    layer = getattr(job, "current_layer", None)
    if layer is None:
        return True
    index = segment_for(run.segments, int(layer))
    if index is None or index == run.active_segment:
        return True
    percent = run.segments[index].speed_percent
    try:
        verdict = set_speed(adapter, percent)
    except Exception as exc:  # noqa: BLE001 — said, and the next poll tries the next segment
        run.error = f"could not set {percent}% at layer {layer}: {type(exc).__name__}: {exc}"
        return True
    if getattr(verdict, "ok", True) is False:
        run.error = f"the printer refused {percent}% at layer {layer}"
        return True
    run.active_segment = index
    run.applied.append((int(layer), percent))
    return True


def _loop(run: RunState, resolve: Callable[[], Any], poll_s: float) -> None:
    while not run.stop.is_set():
        try:
            adapter = resolve()
            if not _tick(run, adapter):
                break
        except Exception as exc:  # noqa: BLE001 — the printer is asked again next time
            run.error = f"{type(exc).__name__}: {exc}"
        run.stop.wait(poll_s)
    if run.ended is None:
        run.ended = "stopped" if run.stop.is_set() else run.error or "stopped"


def start(
    schedule: Any,
    resolve: Callable[[], Any],
    *,
    printer_name: str | None = None,
    poll_s: float = POLL_INTERVAL_S,
) -> RunState:
    """Start running *schedule* against the printer *resolve* returns, in a
    daemon thread.  Refuses (``ScheduleRefused``) a schedule that is not
    well formed or a printer that is not printing now; the run is pinned to
    the job the printer reports at this moment."""
    segments = parse_segments(schedule)
    adapter = resolve()
    if _state_word(adapter) in _ENDED:
        raise ScheduleRefused("the printer is not printing, so there is nothing to pace")
    run = RunState(segments=segments, printer_name=printer_name, watching=_identity(adapter.get_job()))
    run.thread = threading.Thread(
        target=_loop, args=(run, resolve, poll_s), daemon=True, name="kiln-speed-schedule",
    )
    run.thread.start()
    return run


def stop(run: RunState, *, timeout_s: float = 10.0) -> None:
    """Stop *run* and wait for its thread.  The printer stays at the last
    speed set; nothing is sent."""
    run.stop.set()
    if run.thread is not None and run.thread.is_alive():
        run.thread.join(timeout=timeout_s)


__all__ = [
    "POLL_INTERVAL_S",
    "RunState",
    "ScheduleRefused",
    "Segment",
    "nearest_preset",
    "parse_segments",
    "segment_for",
    "set_speed",
    "start",
    "stop",
]
