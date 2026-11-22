"""A live speed-up never asks the nozzle for more plastic than it can melt.

Raising a running print's speed scales every move of its file by the same
factor, and with it the plastic each second asks of the hotend.  A file
sliced close to the hotend's melt limit, sped up, asks for more than the
hotend can melt: the print under-extrudes.  This module is the one check
every live speed change meets, at the adapter template's command
chokepoints (:mod:`kiln.printers.base`), so a tool, a terminal command, a
speed schedule and a raw feedrate command are all judged by the same rule.

The rule, for a request above the sliced speed (100%):

* Read the file Kiln sent for the print running now
  (:func:`kiln.learned_print_time.sent_file_path`) -- for a sliced 3MF the
  plate its start recorded -- and the most plastic per
  second each of its layers commands at the sliced speed (filament length
  per millimetre of move, times the move's feed, times the filament's
  cross-section from the diameter the file states).
* The melt limit is the lowest of: the printer's own ceiling on its safety
  record (:func:`kiln.safety_profiles.get_profile`, ``max_volumetric_flow``),
  and the hotend and material limits the file says it was sliced with
  (``max_volumetric_speed``, ``filament_max_volumetric_speed``).
* Over the layers the change covers (a schedule segment's own layers;
  otherwise from the layer the printer is on to the end), the highest
  percent whose peak stays within the limit is allowed.  A request above it
  is lowered to it, never below the sliced speed; a printer that takes only
  presets gets the highest preset that fits.

A capped reply says what was set and what was asked, why, what going faster
does, and how to go past it.  A request made inside
:func:`past_the_melt_limit` (each door's ``past_the_melt_limit`` choice,
``--past-the-melt-limit`` in the terminal) is set as asked and the reply
says it was set past the limit at the person's choice; the choice lasts for
that one request and reaches no other check.  A printer that reports
neither its layer nor its progress has the whole file judged, and the reply
says so.

A slow-down (100% or less) is never judged and never changed.  Anything the
check cannot read -- no running print, a file Kiln did not send, a file
that does not state its filament diameter, a printer with no known limit,
a 3MF of several plates whose printed plate no start recorded (never one
plate taken for another) -- lets the request through unchanged, and the
reply says it was not checked.

Never raises into a command.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import math
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

#: The sliced speed, as a percentage.
SLICED = 100
#: Moves shorter than this never reach their commanded feed (the machine is
#: still accelerating), so they do not set a layer's peak.
MIN_MOVE_MM = 1.0
#: Files larger than this are not read for the check.
MAX_FILE_BYTES = 512 * 1024 * 1024

#: Said once beside any live speed change: next time the speed can be part of
#: the slice instead of an override.
SLICE_IT_NEXT_TIME = (
    "Next time, the speed can be put into the slice itself, so the slicer's "
    "time and its flow limit follow it."
)
#: Said beside a sport or ludicrous preset.
ACCELERATION_UNBOUNDED = (
    "Sport and ludicrous also raise the printer's acceleration, which this "
    "check does not bound."
)

# Every sentence a live speed door says about the melt limit lives here.
_CAPPED = (
    "Set to {allowed} instead of the {requested} you asked for. At {requested}, "
    "layer {layer} would need about {need:.0f} mm³ of melted plastic a second, and "
    "the nozzle can melt about {limit:g}, so it cannot keep up on these layers."
)
_KEPT = (
    "Kept at {allowed} instead of the {requested} you asked for. At the sliced "
    "speed, layer {layer} already needs about {peak:.0f} mm³ of melted plastic a "
    "second, about all the nozzle can melt ({limit:g}), so it cannot keep up with "
    "any speed-up on these layers."
)
#: What going faster than the cap does, said beside every cap.
FASTER_FAILS = "Printing faster than that gives thin or missing lines and a weaker part."
#: The two kinds of door, by how a person goes past the cap from each.
TOOL = "tool"
TERMINAL = "terminal"
#: How to go past the cap, said once beside every cap, by door.
GO_PAST = {
    TOOL: "If you want the faster speed anyway, ask again with past_the_melt_limit set to true.",
    TERMINAL: "If you want the faster speed anyway, run the same command again with --past-the-melt-limit.",
}
_PAST = (
    "Set to {requested}, past the melt limit, because you chose to: Kiln did not "
    "hold this speed back. At {requested}, layer {layer} needs about {need:.0f} mm³ "
    "of melted plastic a second and the nozzle can melt about {limit:g}, so expect "
    "thin or missing lines and a weaker part on those layers."
)
_UNCHECKED = (
    "{requested} was set without checking it against how much plastic the nozzle "
    "can melt: {why}."
)
#: Said when the printer gives neither its layer nor its progress.
WHOLE_FILE = (
    "The whole file was judged, because the printer did not report where the print is."
)

#: Why a request could not be checked.
NOTHING_PRINTING = "nothing is printing, so there is no file to check it against"
NOT_SENT_BY_KILN = "Kiln did not send this print's file, so it cannot read what the file asks for"
FILE_UNREADABLE = "the print's file could not be read"
NO_DIAMETER = "the print's file does not say its filament diameter"
NO_LIMIT = "Kiln does not know this printer's melt limit"
PLATE_UNKNOWN = (
    "the print's file holds several plates and Kiln does not know which one is printing"
)

#: Presets a printer that only takes presets offers, by percent.
_PRESETS = ((50, "silent"), (100, "standard"), (124, "sport"), (166, "ludicrous"))
_PRESET_PERCENT = {name: pct for pct, name in _PRESETS}
_FAST_PRESETS = frozenset({"sport", "ludicrous"})


# ---------------------------------------------------------------------------
# What a file asks for
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileFlow:
    """The plastic a sliced file asks for, layer by layer, at the sliced speed."""

    diameter: float | None
    #: ``(start_line, peak_mm3_s)`` for layer 1, 2, ...
    layers: tuple[tuple[int, float], ...]
    lines: int
    #: ``(value, what)`` limits the file says it was sliced with.
    stated_limits: tuple[tuple[float, str], ...] = ()


_MARKERS = (";LAYER_CHANGE", "; CHANGE_LAYER", ";LAYER:")
_SETTING = re.compile(r"^;\s*([a-z_]+)\s*[=:]\s*([0-9.]+)")
_STATED = {
    "max_volumetric_speed": "the hotend limit the file was sliced with",
    "filament_max_volumetric_speed": "the material limit the file was sliced with",
}


def _words(code: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for token in code.split()[1:]:
        if len(token) < 2:
            continue
        with contextlib.suppress(ValueError):
            out[token[0].upper()] = float(token[1:])
    return out


def _arc_length(x0: float, y0: float, x1: float, y1: float, i: float, j: float, clockwise: bool) -> float:
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
    sweep = (a0 - a1) if clockwise else (a1 - a0)
    sweep %= 2 * math.pi
    if sweep == 0:
        sweep = 2 * math.pi
    return r * sweep


def read_flow(lines: Any) -> FileFlow:
    """What *lines* (a sliced file, in order) ask of the hotend.  Moves before
    the first layer marker (the start sequence) are not counted."""
    x = y = 0.0
    e = 0.0
    feed = 0.0
    abs_xy = True
    e_explicit: bool | None = None  # True relative, False absolute, None follows G90/G91
    volumetric = False
    diameter: float | None = None
    stated: dict[str, float] = {}
    marker: str | None = None
    layers: list[list[float]] = []  # [start_line, peak]
    count = 0
    for count, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        if line[0] == ";":
            if marker is None:
                marker = next((m for m in _MARKERS if line.startswith(m)), None)
                if marker is not None:
                    layers.append([count, 0.0])
                    continue
            elif line.startswith(marker):
                layers.append([count, 0.0])
                continue
            setting = _SETTING.match(line)
            if setting:
                key, value = setting.group(1), float(setting.group(2))
                if key == "filament_diameter" and diameter is None and value > 0:
                    diameter = value
                elif key in _STATED and value > 0 and key not in stated:
                    stated[key] = value
            continue
        code = line.split(";", 1)[0].strip()
        if not code:
            continue
        head = code.split(None, 1)[0].upper()
        if head == "G90":
            abs_xy = True
            continue
        if head == "G91":
            abs_xy = False
            continue
        if head == "M82":
            e_explicit = False
            continue
        if head == "M83":
            e_explicit = True
            continue
        if head == "M200":
            volumetric = _words(code).get("D", 0.0) > 0
            continue
        if head == "G92":
            w = _words(code)
            if "E" in w:
                e = w["E"]
            continue
        if head not in ("G0", "G1", "G2", "G3"):
            continue
        w = _words(code)
        if "F" in w and w["F"] > 0:
            feed = w["F"]
        nx = (w["X"] if abs_xy else x + w["X"]) if "X" in w else x
        ny = (w["Y"] if abs_xy else y + w["Y"]) if "Y" in w else y
        e_relative = e_explicit if e_explicit is not None else not abs_xy
        de = 0.0
        if "E" in w:
            de = w["E"] if e_relative else w["E"] - e
            e = e + w["E"] if e_relative else w["E"]
        if head in ("G2", "G3") and ("I" in w or "J" in w):
            length = _arc_length(x, y, nx, ny, w.get("I", 0.0), w.get("J", 0.0), head == "G2")
        else:
            length = math.hypot(nx - x, ny - y)
        x, y = nx, ny
        if not layers or de <= 0 or length < MIN_MOVE_MM or feed <= 0:
            continue
        # Filament (or, volumetric, plastic) per second; the area is applied
        # once the file has said its diameter, which slicers write last.
        rate = de / length * feed / 60.0
        if rate > layers[-1][1]:
            layers[-1][1] = rate
    area = 1.0 if volumetric else (math.pi * (diameter / 2) ** 2 if diameter else 0.0)
    return FileFlow(
        diameter=1.0 if volumetric else diameter,
        layers=tuple((int(start), peak * area) for start, peak in layers),
        lines=count,
        stated_limits=tuple((v, _STATED[k]) for k, v in stated.items()),
    )


def _printed_member(zf: Any, plate: int | None) -> str | None:
    """The member of a sliced 3MF that prints plate *plate*: by the plate's
    own name when the archive names its plates (with no plate given, only an
    archive of one plate answers), else its one G-code."""
    from kiln.gcode_metadata import plate_gcode_member, plate_gcode_members, sliced_gcode_member

    if plate_gcode_members(zf):
        return plate_gcode_member(zf, plate)
    return sliced_gcode_member(zf)


def plates_in(path: str) -> int:
    """How many plates the sliced 3MF at *path* names (0 for any other file,
    or one that cannot be read).  Never raises."""
    if not str(path).lower().endswith(".3mf"):
        return 0
    try:
        from kiln.gcode_metadata import plate_gcode_members
        from kiln.untrusted_zip import UntrustedZipFile

        with UntrustedZipFile(path) as zf:
            return len(plate_gcode_members(zf))
    except Exception:  # noqa: BLE001 -- an unreadable file names no plates
        return 0


@lru_cache(maxsize=8)
def _flow_of(path: str, size: int, mtime_ns: int, plate: int | None) -> FileFlow | None:
    """:func:`read_flow` of the G-code *path* prints (plate *plate* of a
    sliced 3MF), cached by its bytes' stamp."""
    lower = path.lower()
    if lower.endswith((".gcode", ".gco", ".g")):
        with open(path, encoding="utf-8", errors="replace") as handle:
            return read_flow(handle)
    if lower.endswith(".3mf"):
        from kiln.gcode_metadata import read_member_text
        from kiln.untrusted_zip import UntrustedZipFile

        with UntrustedZipFile(path) as zf:
            member = _printed_member(zf, plate)
            if member is None:
                return None
            return read_flow(read_member_text(zf, member, MAX_FILE_BYTES).splitlines())
    return None


def flow_of_file(path: str, plate: int | None = None) -> FileFlow | None:
    """The plastic the file at *path* asks for (plate *plate* of a sliced
    3MF; with no plate named, only a 3MF of one plate), or ``None`` when it
    prints no G-code Kiln can read.  Never raises."""
    try:
        st = os.stat(path)
        if st.st_size > MAX_FILE_BYTES:
            return None
        return _flow_of(path, st.st_size, st.st_mtime_ns, plate)
    except Exception:  # noqa: BLE001 -- an unreadable file is not checked
        logger.debug("flow not read from %s", os.path.basename(str(path)), exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The judgement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Judgement:
    """What the floor decided for one live speed request."""

    requested: int
    allowed: int
    checked: bool
    said: str | None = None
    limit: float | None = None
    peak: float | None = None
    layer: int | None = None
    preset: str | None = None
    requested_preset: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: Set as asked although it is past the melt limit, at the person's choice.
    past: bool = False

    @property
    def lowered(self) -> bool:
        return self.allowed < self.requested

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "requested_percent": self.requested,
            "percent": self.allowed,
            "checked": self.checked,
            "lowered": self.lowered,
        }
        if self.preset:
            out["preset"] = self.preset
        if self.past:
            out["past_the_melt_limit"] = True
        if self.limit is not None:
            out["melt_limit_mm3_s"] = self.limit
        if self.said:
            out["said"] = self.said
        return out


_covering: contextvars.ContextVar[tuple[int, int] | None] = contextvars.ContextVar(
    "kiln_live_speed_covering", default=None,
)
_collected: contextvars.ContextVar[list[Judgement] | None] = contextvars.ContextVar(
    "kiln_live_speed_collected", default=None,
)


@contextlib.contextmanager
def judging_layers(from_layer: int, to_layer: int) -> Iterator[None]:
    """A speed sent inside this block lasts for layers *from_layer* to
    *to_layer* only (a speed schedule's segment), so only those are judged."""
    token = _covering.set((int(from_layer), int(to_layer)))
    try:
        yield
    finally:
        _covering.reset(token)


_past: contextvars.ContextVar[bool] = contextvars.ContextVar("kiln_live_speed_past", default=False)


@contextlib.contextmanager
def past_the_melt_limit(chosen: bool = True) -> Iterator[None]:
    """A speed sent inside this block is set as asked even past the melt
    limit, because the person chose to after being told what it costs.  It
    lasts for this block only and reaches no other check."""
    token = _past.set(bool(chosen))
    try:
        yield
    finally:
        _past.reset(token)


_inside: contextvars.ContextVar[bool] = contextvars.ContextVar("kiln_live_speed_inside", default=False)


@contextlib.contextmanager
def inside() -> Iterator[None]:
    """A command already judged is being sent: an override further down the
    adapter's own chain is not judged again."""
    token = _inside.set(True)
    try:
        yield
    finally:
        _inside.reset(token)


@contextlib.contextmanager
def collect() -> Iterator[list[Judgement]]:
    """Gather what the chokepoints decided for commands sent inside this
    block, for the reply of the door that sent them."""
    found: list[Judgement] = []
    token = _collected.set(found)
    try:
        yield found
    finally:
        _collected.reset(token)


def _keep(judgement: Judgement) -> Judgement:
    found = _collected.get()
    if found is not None:
        found.append(judgement)
    return judgement


def _label(percent: int, preset: str | None) -> str:
    return f"{preset} ({percent}%)" if preset else f"{percent}%"


def _running_file(adapter: Any) -> tuple[str | None, str | None, Any, int | None]:
    """``(path, why_not, job, plate)`` for the print running now on
    *adapter*: the file Kiln sent for it and the plate its start recorded
    (``None`` when no start of this file recorded one)."""
    from kiln.printers.base import PrinterStatus

    state = adapter.get_state()
    status = getattr(state, "effective_state", None) or getattr(state, "state", None)
    if status not in (PrinterStatus.PRINTING, PrinterStatus.PAUSED):
        return None, NOTHING_PRINTING, None, None
    job = None
    with contextlib.suppress(Exception):
        job = adapter.get_job()
    from kiln.learned_print_time import sent_file_path

    names = [getattr(job, "file_name", None)]
    running: dict[str, Any] | None = None
    with contextlib.suppress(Exception):
        from kiln.learned_print_time import _machine
        from kiln.persistence import get_db
        from kiln.printers.base import outcome_printer_name

        printer = outcome_printer_name(adapter)
        if printer:
            running = get_db().running_print(*_machine(printer))
            if running:
                names.append(running.get("file_name"))
    recorded = sent_file_path(running.get("file_name")) if running and running.get("file_name") else None
    for name in names:
        path = sent_file_path(name) if name else None
        if path and os.path.isfile(path):
            plate = running.get("plate_number") if running and recorded == path else None
            return path, None, job, plate if isinstance(plate, int) else None
    return None, NOT_SENT_BY_KILN, job, None


def _melt_limit(adapter: Any, flow: FileFlow) -> tuple[float, str] | None:
    """The lowest melt limit known for this printer and file, with what it is."""
    known: list[tuple[float, str]] = list(flow.stated_limits)
    with contextlib.suppress(Exception):
        from kiln.printers.base import upload_scan_target
        from kiln.safety_profiles import get_profile

        printer_id = upload_scan_target(adapter)
        if printer_id:
            profile = get_profile(str(printer_id))
            ceiling = profile.max_volumetric_flow
            if profile.id != "default" and ceiling:
                known.append((float(ceiling), "the printer's own limit"))
    return min(known, key=lambda k: k[0]) if known else None


def _first_layer(flow: FileFlow, job: Any) -> int | None:
    """The layer the printer is on, as the file counts them, or ``None``."""
    layer = getattr(job, "current_layer", None)
    if isinstance(layer, int) and not isinstance(layer, bool) and layer >= 0:
        return max(1, layer)
    completion = getattr(job, "completion", None)
    if isinstance(completion, (int, float)) and not isinstance(completion, bool) and completion >= 0 and flow.lines:
        fraction = completion / 100.0 if completion > 1 else float(completion)
        line = fraction * flow.lines
        at = 1
        for index, (start, _peak) in enumerate(flow.layers, start=1):
            if start <= line:
                at = index
        return at
    return None


def judge(adapter: Any, percent: int, *, preset: str | None = None) -> Judgement:
    """The floor's decision for setting *adapter*'s running print to
    *percent* of its sliced speed (*preset* names it on a printer that takes
    only presets).  Never raises."""
    requested_label = _label(percent, preset)
    if percent <= SLICED:
        return Judgement(percent, percent, True, preset=preset, requested_preset=preset)

    def unchecked(why: str) -> Judgement:
        return Judgement(
            percent, percent, False, preset=preset, requested_preset=preset,
            said=_UNCHECKED.format(requested=requested_label, why=why),
        )

    try:
        path, why_not, job, plate = _running_file(adapter)
    except Exception:  # noqa: BLE001 -- a printer that cannot be read is not checked
        logger.debug("running print not read for the speed floor", exc_info=True)
        return unchecked(FILE_UNREADABLE)
    if path is None:
        return unchecked(why_not or NOT_SENT_BY_KILN)
    if plate is None and plates_in(path) > 1:
        return unchecked(PLATE_UNKNOWN)
    flow = flow_of_file(path, plate)
    if flow is None or not flow.layers:
        return unchecked(FILE_UNREADABLE)
    if not flow.diameter:
        return unchecked(NO_DIAMETER)
    limit = _melt_limit(adapter, flow)
    if limit is None:
        return unchecked(NO_LIMIT)
    span = _covering.get()
    notes: tuple[str, ...] = ()
    if span is not None:
        first, last = span
    else:
        first = _first_layer(flow, job)
        last = len(flow.layers)
        if first is None:
            first, notes = 1, (WHOLE_FILE,)
    covered = [
        (index, peak)
        for index, (_start, peak) in enumerate(flow.layers, start=1)
        if first <= index <= last and peak > 0
    ]
    if not covered:
        return Judgement(percent, percent, True, preset=preset, requested_preset=preset, limit=limit[0])
    layer, peak = max(covered, key=lambda item: item[1])
    ceiling = limit[0]
    if peak * percent / SLICED <= ceiling:
        return Judgement(
            percent, percent, True, preset=preset, requested_preset=preset,
            limit=ceiling, peak=peak, layer=layer, notes=notes,
        )
    if _past.get():
        said = _PAST.format(requested=requested_label, layer=layer, need=peak * percent / SLICED, limit=ceiling)
        return Judgement(
            percent, percent, True, said=said, limit=ceiling, peak=peak, layer=layer,
            preset=preset, requested_preset=preset, notes=notes, past=True,
        )
    fits = max(SLICED, math.floor(SLICED * ceiling / peak))
    allowed, allowed_preset = fits, None
    if preset is not None:
        allowed_preset = max((p for p in _PRESETS if p[0] <= fits), default=(SLICED, "standard"))[1]
        allowed = _PRESET_PERCENT[allowed_preset]
    template = _KEPT if allowed <= SLICED else _CAPPED
    said = template.format(
        allowed=_label(allowed, allowed_preset), requested=requested_label, layer=layer,
        need=peak * percent / SLICED, peak=peak, limit=ceiling,
    ) + " " + FASTER_FAILS
    return Judgement(
        percent, allowed, True, said=said, limit=ceiling, peak=peak, layer=layer,
        preset=allowed_preset, requested_preset=preset, notes=notes,
    )


# ---------------------------------------------------------------------------
# The chokepoints (called from the adapter template)
# ---------------------------------------------------------------------------

_M220 = re.compile(r"^(\s*M220\b[^;]*?\bS)(\d+(?:\.\d+)?)(.*)$", re.I | re.S)


def _hosted() -> bool:
    with contextlib.suppress(Exception):
        from kiln.learned_print_time import _hosted as hosted

        return hosted()
    return False


def _rewrite(commands: Any, decide: Any) -> list[Any]:
    """*commands* with each ``M220 S<n>`` above the sliced speed replaced by
    ``decide(n)`` (a :class:`Judgement`, or ``None`` to leave it)."""
    out: list[Any] = []
    for raw in commands:
        match = _M220.match(raw) if isinstance(raw, str) else None
        percent = int(float(match.group(2))) if match else 0
        verdict = decide(percent) if match and percent > SLICED else None
        if verdict is not None and verdict.lowered:
            out.append(f"{match.group(1)}{verdict.allowed}{match.group(3)}")
        else:
            out.append(raw)
    return out


def guard_gcode(adapter: Any, commands: Any) -> Any:
    """*commands* with each feedrate override (``M220 S<n>``) above the
    sliced speed lowered to what the floor allows.  Anything else, and
    anything that cannot be read, is returned as given.  Never raises."""
    try:
        if _inside.get() or _hosted() or not isinstance(commands, (list, tuple)):
            return commands
        out = _rewrite(commands, lambda percent: _keep(judge(adapter, percent)))
        if out == list(commands):
            return commands
        return tuple(out) if isinstance(commands, tuple) else out
    except Exception:  # noqa: BLE001 -- the floor never breaks a command
        logger.debug("speed floor skipped for raw commands", exc_info=True)
        return commands


def guarded_lines(commands: list[Any], judgements: list[Judgement]) -> list[Any]:
    """*commands* as :func:`guard_gcode` sent them, given what it decided
    (*judgements*, in order)."""
    pending = iter(judgements)
    return _rewrite(commands, lambda _percent: next(pending, None))


def changes_speed(commands: Any) -> bool:
    """Whether *commands* set a speed other than the sliced speed."""
    for raw in commands or ():
        match = _M220.match(raw) if isinstance(raw, str) else None
        if match and int(float(match.group(2))) != SLICED:
            return True
    return False


def guard_preset(adapter: Any, profile: Any) -> Any:
    """*profile* (a preset name), or the highest preset the floor allows when
    *profile* asks for more.  Never raises."""
    try:
        if _inside.get() or _hosted() or not isinstance(profile, str):
            return profile
        name = profile.strip().lower()
        percent = _PRESET_PERCENT.get(name)
        if percent is None or percent <= SLICED:
            return profile
        verdict = _keep(judge(adapter, percent, preset=name))
        return verdict.preset if verdict.lowered and verdict.preset else profile
    except Exception:  # noqa: BLE001 -- the floor never breaks a command
        logger.debug("speed floor skipped for a preset", exc_info=True)
        return profile


# ---------------------------------------------------------------------------
# What a door says
# ---------------------------------------------------------------------------


def sentences(
    judgements: list[Judgement] | tuple[Judgement, ...], *, changed: bool, door: str = TOOL,
) -> list[str]:
    """The sentences a live speed door adds to its reply, each once: what the
    floor did (or that it could not check), how to go past a cap from this
    *door* (:data:`TOOL` or :data:`TERMINAL`), the acceleration a fast preset
    adds, and -- for a change away from the sliced speed -- that the print's
    time no longer follows the slicer, and how next time it can."""
    out: list[str] = []
    for judgement in judgements:
        for line in (judgement.said, *judgement.notes):
            if line and line not in out:
                out.append(line)
    if any(j.lowered for j in judgements):
        out.append(GO_PAST.get(door, GO_PAST[TOOL]))
    for judgement in judgements:
        if judgement.preset in _FAST_PRESETS and ACCELERATION_UNBOUNDED not in out:
            out.append(ACCELERATION_UNBOUNDED)
    if changed:
        from kiln.ran_as_sliced import NOT_AT_SLICED_SPEED

        out.extend([NOT_AT_SLICED_SPEED, SLICE_IT_NEXT_TIME])
    return out


def reply(
    out: dict[str, Any], judgements: list[Judgement], *, changed: bool, door: str = TOOL,
) -> dict[str, Any]:
    """Put the floor's word on a door's reply *out*: ``speed_floor`` (each
    judgement) and the :func:`sentences` appended to ``message``."""
    said = sentences(judgements, changed=changed, door=door)
    if judgements:
        out["speed_floor"] = [j.to_dict() for j in judgements]
    if said:
        lead = str(out.get("message") or "").strip()
        out["message"] = " ".join([lead, *said] if lead else said)
    return out


__all__ = [
    "ACCELERATION_UNBOUNDED", "FASTER_FAILS", "FileFlow", "GO_PAST", "Judgement", "MIN_MOVE_MM", "SLICED",
    "SLICE_IT_NEXT_TIME", "TERMINAL", "TOOL", "WHOLE_FILE", "changes_speed", "past_the_melt_limit", "collect", "judging_layers", "guarded_lines", "inside", "flow_of_file", "guard_gcode", "guard_preset", "judge", "read_flow",
    "reply", "sentences",
]
