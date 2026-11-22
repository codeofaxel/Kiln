"""A person's own "print this height range at this speed", put into the slice.

Contract
--------
* The wish is a list of height ranges, each measured in millimetres up from
  the plate, each with one speed: ``{"from_mm": 0, "to_mm": 6, "percent": 50}``
  or ``{"from_mm": 6, "speed_mm_s": 120}``.  ``to_mm`` left out means "to the
  top of the part".  ``percent`` is of the profile's own speeds, so every
  kind of line keeps its place beside the others; ``speed_mm_s`` asks every
  printing line in the range for that one speed.  ``max_mm_s`` asks that no
  line in the range print faster than that, and leaves a slower line as it
  is.  ``lines: "inside"`` keeps a ``percent`` or a ``speed_mm_s`` to the
  part's infill and inner walls.  ``max_acceleration_mm_s2`` asks that the
  printer speed up and stop no harder than that in the range, with a speed
  or alone; a slicer that cannot do so by height refuses it.
  :func:`checked` reads a wish and refuses one that cannot be printed as
  said (:class:`SpeedByHeightError`).
* :func:`sliced` makes the slice.  The person's slicer applies the ranges
  itself, so its time, its cooling and its flow limit all see them.  The
  part is sliced twice, once without the wish, and both files are read:
  the result's ``speed_by_height`` states, for each range, what was asked,
  what the file commands, and one outcome (:data:`APPLIED`,
  :data:`ALREADY_SLOWER`, :data:`CAPPED`, :data:`LIMITED`).  A range the
  slicer did not apply is :data:`NOT_APPLIED`: the slice is refused and no
  file is kept.
* What the reply says is measured from the G-code the slicer wrote, never
  taken from the settings it was handed.
* :func:`sliced` may be handed a *planner* in place of ranges: it is called
  once with the reading of the slice made without any wish and answers the
  ranges to put in, each with the sentence that says why, and what to say
  of the whole part.
* :func:`keep_wish` puts the wish of a sliced file on a design version's
  record, read from Kiln's own record of that slice
  (:func:`kiln.print_lineage.speed_by_height_of`), never from a caller.

Safety invariant
----------------
A speed asked for here is a slicer setting: the slicer bounds it by the
flow limit the profile states, exactly as it bounds the profile's own
speeds.  Nothing here writes a speed command into a file.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiln import plain_words as _plain_words
from kiln.print_lineage import SPEED_BY_HEIGHT_KEY
from kiln.slicer import SlicerError

#: Where a slice result, a slice's record and a version's record keep the wish.
RECORD_KEY = SPEED_BY_HEIGHT_KEY

#: The range prints at the speed asked.
APPLIED = "applied"
#: The range already printed this slowly or slower; nothing changed.
ALREADY_SLOWER = "already_slower"
#: The slicer held the range at what the nozzle can melt.
CAPPED = "capped"
#: The slicer held the range below what was asked for a reason of its own.
LIMITED = "limited"
#: The slicer did not apply the range.
NOT_APPLIED = "not_applied"

#: The slowest and fastest speed a range may ask for, in mm/s.
MIN_SPEED_MM_S = 1.0
MAX_SPEED_MM_S = 1000.0
#: The smallest and largest share of the profile's speeds a range may ask for.
MIN_PERCENT = 5.0
MAX_PERCENT = 500.0
MAX_RANGES = 20
#: The gentlest and the hardest acceleration a range may ask for, in mm/s².
MIN_ACCELERATION = 100.0
MAX_ACCELERATION = 50000.0
#: Which lines of a range its speed is for.
ALL_LINES = "all"
INSIDE_LINES = "inside"

PRUSA = "prusa"
BAMBU = "bambu"
_RANGE_MEMBER = {
    PRUSA: "Metadata/Prusa_Slicer_layer_config_ranges.xml",
    BAMBU: "Metadata/layer_config_ranges.xml",
}
#: The speeds a range sets, as each family's slicer spells them.
SPEED_KEYS: dict[str, tuple[str, ...]] = {
    PRUSA: (
        "perimeter_speed", "external_perimeter_speed", "small_perimeter_speed", "infill_speed",
        "solid_infill_speed", "top_solid_infill_speed", "gap_fill_speed", "bridge_speed",
    ),
    BAMBU: (
        "inner_wall_speed", "outer_wall_speed", "small_perimeter_speed", "sparse_infill_speed",
        "internal_solid_infill_speed", "top_surface_speed", "gap_infill_speed", "bridge_speed",
        "internal_bridge_speed",
    ),
}

#: The lines inside a part, its infill and its inner walls: the speeds each
#: family's slicer spells for them, and what a sliced file calls them.
INSIDE_KEYS: dict[str, tuple[str, ...]] = {
    PRUSA: ("perimeter_speed", "infill_speed", "solid_infill_speed"),
    BAMBU: ("inner_wall_speed", "sparse_infill_speed", "internal_solid_infill_speed"),
}
INSIDE_KINDS = frozenset({
    "perimeter", "internal infill", "solid infill", "inner wall", "sparse infill", "internal solid infill",
})

#: Two speeds closer than this are the same speed: a slicer writes whole
#: millimetres a minute.
_SAME_MM_S = 0.5
_SAME_SHARE = 0.02
#: A move shorter than this is not judged for its speed or its flow.
_JUDGED_MOVE_MM = 0.5
#: A range is applied when no more than this share of its printing, by
#: length, runs faster than asked.
_FASTER_SHARE_ALLOWED = 0.05
#: A range's acceleration is mostly held from this share of its printing,
#: by length; under it the range was not applied.
_ACCELERATION_MOSTLY = 0.5
#: A move is at the flow limit from this share of it.
_AT_FLOW_LIMIT = 0.95
#: The slack on the slicer's cooling time when a layer's time is read from
#: its commanded speeds alone.
_COOLING_SLACK = 1.15


class SpeedByHeightError(SlicerError):
    """A speed-by-height wish that cannot be sliced as said, or that the
    slicer did not apply.  ``code`` is the door's error code; ``ranges`` the
    outcome of every range when the slice was made and then refused."""

    def __init__(self, message: str, *, code: str = "SPEED_BY_HEIGHT_REFUSED", ranges: Sequence[dict] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.ranges = list(ranges)


@dataclass(frozen=True)
class SpeedRange:
    """One checked range: millimetres up from the plate, and its one speed."""

    from_mm: float
    to_mm: float | None
    speed_mm_s: float | None = None
    percent: float | None = None
    max_mm_s: float | None = None
    lines: str = ALL_LINES
    max_acceleration: float | None = None

    def asked(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.speed_mm_s is not None:
            out["speed_mm_s"] = self.speed_mm_s
        elif self.percent is not None:
            out["percent"] = self.percent
        elif self.max_mm_s is not None:
            out["max_mm_s"] = self.max_mm_s
        if self.lines != ALL_LINES:
            out["lines"] = self.lines
        if self.max_acceleration is not None:
            out["max_acceleration_mm_s2"] = self.max_acceleration
        return out

    def span(self) -> str:
        top = "the top" if self.to_mm is None else f"{self.to_mm:g} mm"
        return f"{self.from_mm:g} mm to {top}"

    def speed_asked(self) -> bool:
        return self.speed_mm_s is not None or self.percent is not None or self.max_mm_s is not None

    def wanted(self) -> str:
        parts = []
        inside = " on its infill and inner walls" if self.lines == INSIDE_LINES else ""
        if self.speed_mm_s is not None:
            parts.append(f"{self.speed_mm_s:g} mm/s{inside}")
        elif self.percent is not None:
            parts.append(f"{self.percent:g}% of the profile's speeds{inside}")
        elif self.max_mm_s is not None:
            parts.append(f"no line faster than {self.max_mm_s:g} mm/s")
        if self.max_acceleration is not None:
            parts.append(f"speeding up and stopping at no more than {self.max_acceleration:g} mm/s²")
        return ", ".join(parts)


_SHAPE = (
    'speed_by_height takes a list of height ranges, each like {"from_mm": 0, "to_mm": 6, "percent": 50}, '
    '{"from_mm": 6, "speed_mm_s": 120} or {"from_mm": 20, "max_mm_s": 60}: millimetres up from the plate, '
    "and one speed."
)
_RANGE_KEYS = frozenset({"from_mm", "to_mm", "speed_mm_s", "percent", "max_mm_s", "lines", "max_acceleration_mm_s2"})


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def checked(wish: Any) -> list[SpeedRange]:
    """*wish* as checked ranges, lowest first; ``[]`` for no wish.

    Refused, in plain words: a wish that is not a list of ranges, a range
    with no height or below the plate, a range with no speed or two, a speed
    outside what a printer runs, and ranges that overlap.
    """
    if wish is None or wish == "" or wish == []:
        return []
    if isinstance(wish, str):
        try:
            wish = json.loads(wish)
        except ValueError:
            raise SpeedByHeightError(f"The speed ranges could not be read. {_SHAPE}") from None
    if isinstance(wish, Mapping):
        wish = [wish]
    if not isinstance(wish, Sequence) or not all(isinstance(r, Mapping) for r in wish):
        raise SpeedByHeightError(_SHAPE)
    if len(wish) > MAX_RANGES:
        raise SpeedByHeightError(f"A slice takes up to {MAX_RANGES} speed ranges; this asks for {len(wish)}.")
    ranges: list[SpeedRange] = []
    for number, raw in enumerate(wish, 1):
        unknown = sorted(set(raw) - _RANGE_KEYS)
        if unknown:
            raise SpeedByHeightError(f"Range {number} says {', '.join(map(str, unknown))}, which is not read. {_SHAPE}")
        start = _number(raw.get("from_mm", 0))
        end = None if raw.get("to_mm") is None else _number(raw.get("to_mm"))
        if start is None or (raw.get("to_mm") is not None and end is None):
            raise SpeedByHeightError(f"Range {number}: `from_mm` and `to_mm` are heights in millimetres. {_SHAPE}")
        if start < 0:
            raise SpeedByHeightError(f"Range {number} starts {abs(start):g} mm below the plate; heights start at 0.")
        if end is not None and end <= start:
            raise SpeedByHeightError(
                f"Range {number} runs from {start:g} mm to {end:g} mm, so it has no height: "
                "`to_mm` must be above `from_mm`."
            )
        speed, percent, most = raw.get("speed_mm_s"), raw.get("percent"), raw.get("max_mm_s")
        hardest = raw.get("max_acceleration_mm_s2")
        given = sum(value is not None for value in (speed, percent, most))
        if given > 1 or (given == 0 and hardest is None):
            raise SpeedByHeightError(
                f"Range {number} needs one speed: `speed_mm_s`, `percent` or `max_mm_s`, not two and not none."
            )
        lines = raw.get("lines", ALL_LINES)
        if lines not in (ALL_LINES, INSIDE_LINES):
            raise SpeedByHeightError(f'Range {number}: `lines` is "{ALL_LINES}" or "{INSIDE_LINES}".')
        if lines == INSIDE_LINES and speed is None and percent is None:
            raise SpeedByHeightError(f'Range {number}: `lines` "{INSIDE_LINES}" goes with `speed_mm_s` or `percent`.')
        fields: dict[str, Any] = {"lines": lines}
        if hardest is not None:
            value = _number(hardest)
            if value is None or not MIN_ACCELERATION <= value <= MAX_ACCELERATION:
                raise SpeedByHeightError(
                    f"Range {number}: `max_acceleration_mm_s2` is from {MIN_ACCELERATION:g} to {MAX_ACCELERATION:g}."
                )
            fields["max_acceleration"] = value
        for key, raw_speed in (("speed_mm_s", speed), ("max_mm_s", most)):
            if raw_speed is not None:
                value = _number(raw_speed)
                if value is None or not MIN_SPEED_MM_S <= value <= MAX_SPEED_MM_S:
                    raise SpeedByHeightError(
                        f"Range {number}: `{key}` is from {MIN_SPEED_MM_S:g} to {MAX_SPEED_MM_S:g} mm/s."
                    )
                fields[key] = value
        if percent is not None:
            value = _number(percent)
            if value is None or not MIN_PERCENT <= value <= MAX_PERCENT:
                raise SpeedByHeightError(f"Range {number}: percent is from {MIN_PERCENT:g} to {MAX_PERCENT:g}.")
            fields["percent"] = value
        ranges.append(SpeedRange(start, end, **fields))
    ranges.sort(key=lambda r: r.from_mm)
    for lower, upper in zip(ranges, ranges[1:], strict=False):
        if lower.to_mm is None or lower.to_mm > upper.from_mm:
            top = "the top" if lower.to_mm is None else f"{lower.to_mm:g} mm"
            raise SpeedByHeightError(
                f"The ranges {lower.span()} and {upper.span()} overlap between {upper.from_mm:g} mm and {top}: "
                "each height can have one speed."
            )
    return ranges


# ---------------------------------------------------------------------------
# Reading a sliced file
# ---------------------------------------------------------------------------

_WORD = re.compile(r"([A-Za-z])\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")
_LAYER_Z = re.compile(r"^;\s*(?:Z|Z_HEIGHT)\s*:\s*(-?\d*\.?\d+)\s*$")
_LAYER_HEIGHT = re.compile(r"^;\s*(?:HEIGHT|LAYER_HEIGHT)\s*:\s*(\d*\.?\d+)\s*$")
_SETTING = re.compile(r"^;\s*([a-z_0-9]+)\s*=\s*(.*)$")
_SETTINGS_READ = frozenset({
    "filament_diameter", "filament_max_volumetric_speed", "max_volumetric_speed",
    "slowdown_below_layer_time", "slow_down_layer_time", "cooling", "slow_down_for_layer_cooling",
    "layer_height", "nozzle_diameter", "default_acceleration", "fill_density", "sparse_infill_density",
    *SPEED_KEYS[PRUSA], *SPEED_KEYS[BAMBU],
})
#: Lines that are not the part: what a slicer draws around or under it.
_AROUND_THE_PART = re.compile(r"skirt|brim|tower|custom|purge|prime", re.IGNORECASE)
_SUPPORT = re.compile(r"support", re.IGNORECASE)


@dataclass
class _Layer:
    z: float
    moves: list[tuple[float, float, float | None, str]]  # (feed mm/s, length mm, flow mm3/s, kind of line)
    support_moves: list[tuple[float, float]]
    seconds: float = 0.0
    height: float = 0.0
    #: The hardest acceleration the file commands for a line of the part in
    #: this layer, in mm/s², and whether any such line printed before the
    #: file had commanded one.
    hardest_acceleration: float | None = None
    acceleration_unstated: bool = False
    #: The length of the part's lines printed under each acceleration the
    #: file commands; ``None`` is before it commanded any.
    acceleration_lengths: dict[float | None, float] = field(default_factory=dict)

    @property
    def middle(self) -> float:
        return self.z - self.height / 2.0


@dataclass
class Reading:
    """What a sliced file commands, layer by layer, and what it states."""

    layers: list[_Layer]
    settings: dict[str, str]
    seconds: int | None

    def flow_limit(self) -> float | None:
        limits = [
            value for key in ("filament_max_volumetric_speed", "max_volumetric_speed")
            if (value := _first_positive(self.settings.get(key))) is not None
        ]
        return min(limits) if limits else None

    def cooling_seconds(self) -> float | None:
        """The layer time below which the slicer slows a layer, or ``None``
        when the file states none or states that it does not."""
        for switch in ("cooling", "slow_down_for_layer_cooling"):
            if _first_number(self.settings.get(switch)) == 0:
                return None
        for key in ("slowdown_below_layer_time", "slow_down_layer_time"):
            value = _first_positive(self.settings.get(key))
            if value is not None:
                return value
        return None


def _first_number(value: str | None) -> float | None:
    if value is None:
        return None
    return _number(re.split(r"[,;]", value.strip().strip('"'))[0].strip())


def _first_positive(value: str | None) -> float | None:
    number = _first_number(value)
    return number if number is not None and number > 0 else None


def read_gcode(path: str) -> Reading:
    """Every extruding move of the G-code at *path*, by the layer it prints
    in, with its commanded speed and flow, and the slicer's own time."""
    from kiln.gcode import feature_label, slicer_print_time

    layers: dict[float, _Layer] = {}
    settings: dict[str, str] = {}
    relative_e = False
    x = y = z = e = feed = 0.0
    acceleration: float | None = None
    feature = "custom"
    # The layer a move prints in is the one the slicer's own layer lines
    # name; a file without them is read by the height of each move.
    marked: float | None = None
    heights: dict[float, float] = {}
    pending: list[tuple[float, float, float, float, str]] = []  # (z, feed, length, e, kind of line)
    kept_text: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            if text.startswith(";"):
                label = feature_label(text)
                if label is not None:
                    feature = label
                    continue
                at = _LAYER_Z.match(text)
                if at:
                    marked = round(float(at.group(1)), 3)
                    continue
                tall = _LAYER_HEIGHT.match(text)
                if tall and marked is not None:
                    heights.setdefault(marked, float(tall.group(1)))
                    continue
                found = _SETTING.match(text)
                if found and found.group(1) in _SETTINGS_READ:
                    settings.setdefault(found.group(1), found.group(2).strip())
                if "ime" in text or "IME" in text or "generated" in text:
                    kept_text.append(text)
                continue
            code = text.split(";", 1)[0].strip()
            head = code.split(None, 1)[0].upper() if code else ""
            if head == "M83":
                relative_e = True
            elif head == "M82":
                relative_e = False
            elif head == "G92":
                words = {k.upper(): float(v) for k, v in _WORD.findall(code[3:])}
                e = words.get("E", e)
            elif head == "M204":
                words = {k.upper(): float(v) for k, v in _WORD.findall(code[4:])}
                acceleration = words.get("S", words.get("P", acceleration))
            if head not in ("G0", "G1", "G2", "G3"):
                continue
            words = {k.upper(): float(v) for k, v in _WORD.findall(code[len(head):])}
            feed = words.get("F", feed)
            nx, ny, nz = words.get("X", x), words.get("Y", y), words.get("Z", z)
            pushed = 0.0
            if "E" in words:
                pushed = words["E"] if relative_e else words["E"] - e
                if not relative_e:
                    e = words["E"]
            length = math.hypot(nx - x, ny - y)
            if head in ("G2", "G3") and ("I" in words or "J" in words) and length > 0:
                radius = math.hypot(words.get("I", 0.0), words.get("J", 0.0))
                if radius > length / 2.0:
                    length = 2.0 * radius * math.asin(min(1.0, length / (2.0 * radius)))
            x, y, z = nx, ny, nz
            if length <= 1e-4 or feed <= 0:
                continue
            seconds = length / (feed / 60.0)
            key = round(nz, 3) if marked is None else marked
            if pushed > 1e-6:
                layer = layers.setdefault(key, _Layer(z=key, moves=[], support_moves=[]))
                layer.seconds += seconds
                if _SUPPORT.search(feature):
                    layer.support_moves.append((feed / 60.0, length))
                elif not _AROUND_THE_PART.search(feature):
                    pending.append((key, feed / 60.0, length, pushed, feature))
                    layer.acceleration_lengths[acceleration] = (
                        layer.acceleration_lengths.get(acceleration, 0.0) + length
                    )
                    if length >= _JUDGED_MOVE_MM:
                        if acceleration is None:
                            layer.acceleration_unstated = True
                        elif layer.hardest_acceleration is None or acceleration > layer.hardest_acceleration:
                            layer.hardest_acceleration = acceleration
            elif key in layers:
                layers[key].seconds += seconds
    diameter = _first_positive(settings.get("filament_diameter"))
    area = None if diameter is None else math.pi * (diameter / 2.0) ** 2
    for key, speed, length, pushed, kind in pending:
        flow = None if area is None or length < _JUDGED_MOVE_MM else pushed / length * area * speed
        layers[key].moves.append((speed, length, flow, kind))
    ordered = [layers[key] for key in sorted(layers) if layers[key].moves or layers[key].support_moves]
    below = 0.0
    for layer in ordered:
        # A layer of the part is as tall as its rise above the part's last
        # layer; a layer that holds supports alone has its own height.
        if layer.moves:
            layer.height = layer.z - below
            below = layer.z
        else:
            layer.height = heights.get(layer.z) or layer.z - below
    printed = slicer_print_time("\n".join(kept_text))
    return Reading(
        layers=ordered, settings=settings,
        seconds=int(printed.seconds) if printed is not None and printed.seconds > 0 else None,
    )


def _layers_in(reading: Reading, band: SpeedRange) -> list[_Layer]:
    """The layers *band* covers: a slicer gives a layer the range its middle
    falls in."""
    top = math.inf if band.to_mm is None else band.to_mm
    return [layer for layer in reading.layers if band.from_mm <= layer.middle < top]


def _fastest(layers: Sequence[_Layer]) -> float | None:
    speeds = [speed for layer in layers for speed, length, _flow, _kind in layer.moves if length >= _JUDGED_MOVE_MM]
    return max(speeds) if speeds else None


def _slowest(layers: Sequence[_Layer]) -> float | None:
    speeds = [speed for layer in layers for speed, length, _flow, _kind in layer.moves if length >= _JUDGED_MOVE_MM]
    return min(speeds) if speeds else None


def _typical(layers: Sequence[_Layer]) -> float | None:
    length = sum(m[1] for layer in layers for m in layer.moves)
    seconds = sum(m[1] / m[0] for layer in layers for m in layer.moves)
    return length / seconds if seconds > 0 else None


def _peak_flow(layers: Sequence[_Layer]) -> float | None:
    flows = [flow for layer in layers for _s, _l, flow, _kind in layer.moves if flow is not None]
    return max(flows) if flows else None


def _print(layers: Sequence[_Layer]) -> list[tuple[float, int, int]]:
    """What *layers* command, to compare two slices of the same part."""
    return [
        (layer.z, round(speed * 60.0), round(length * 100.0))
        for layer in layers for speed, length, _flow, _kind in layer.moves
    ]


def _prints_alike(layers: Sequence[_Layer], others: Sequence[_Layer]) -> bool:
    """Whether two slices print the same layers with the same amount of line
    at the same speeds.  A slicer re-times a short layer for cooling from
    one slice to the next, so speeds within :func:`_same` are the same."""
    if [layer.z for layer in layers] != [layer.z for layer in others]:
        return False
    for layer, other in zip(layers, others, strict=True):
        length, other_length = (sum(m[1] for m in one.moves) for one in (layer, other))
        if abs(length - other_length) > max(1.0, _SAME_SHARE * other_length):
            return False
        for read in (_fastest, _typical):
            speed, other_speed = read([layer]), read([other])
            if (speed is None) != (other_speed is None) or (speed is not None and not _same(speed, other_speed)):
                return False
    return True


def _usual_height(reading: Reading) -> float:
    heights = sorted(round(layer.height, 4) for layer in reading.layers)
    return heights[len(heights) // 2]


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= max(_SAME_MM_S, _SAME_SHARE * max(a, b))


def _mm_s(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _measured(layers: Sequence[_Layer]) -> dict[str, Any]:
    block: dict[str, Any] = {
        "layers": len(layers),
        "first_layer_z_mm": layers[0].z if layers else None,
        "last_layer_z_mm": layers[-1].z if layers else None,
        "slowest_mm_s": _mm_s(_slowest(layers)),
        "fastest_mm_s": _mm_s(_fastest(layers)),
        "typical_mm_s": _mm_s(_typical(layers)),
    }
    flow = _peak_flow(layers)
    if flow is not None:
        block["peak_flow_mm3_s"] = round(flow, 2)
    supports = [speed for layer in layers for speed, length in layer.support_moves if length >= _JUDGED_MOVE_MM]
    if supports:
        block["supports_fastest_mm_s"] = _mm_s(max(supports))
    return block


# ---------------------------------------------------------------------------
# What a range asks the slicer for
# ---------------------------------------------------------------------------


def _stated_speeds(plain: Reading, family: str) -> dict[str, float]:
    """The speed the slice without the wish states for each kind of line, in
    mm/s, for the kinds it states as a speed of their own."""
    speeds: dict[str, float] = {}
    for key in SPEED_KEYS[family]:
        text = str(plain.settings.get(key, "")).strip()
        if text and not text.endswith("%") and (speed := _first_positive(text)) is not None:
            speeds[key] = speed
    return speeds


def _asked_speeds(band: SpeedRange, stated: Mapping[str, float], family: str) -> dict[str, float]:
    """The speed *band* asks of each kind of line, in *family*'s spelling.
    A kind the slicer derives from another (a share of it) is left to
    follow it under ``percent`` and ``max_mm_s``; under ``max_mm_s`` a kind
    already at or under the most asked is left as it is."""
    keys = INSIDE_KEYS[family] if band.lines == INSIDE_LINES else SPEED_KEYS[family]
    if band.speed_mm_s is not None:
        return dict.fromkeys(keys, band.speed_mm_s)
    if not band.speed_asked():
        return {}
    if not stated:
        raise SpeedByHeightError(
            f"The range {band.span()} asks for {band.wanted()}, and this slice states no speeds to read that "
            "against. Ask for `speed_mm_s` instead."
        )
    if band.max_mm_s is not None:
        return {key: band.max_mm_s for key, speed in stated.items() if speed > band.max_mm_s}
    return {key: speed * band.percent / 100.0 for key, speed in stated.items() if key in keys}


def _options(asked: Mapping[str, float]) -> dict[str, str]:
    return {key: f"{round(speed, 2):g}" for key, speed in asked.items()}


def _ranges_xml(plan: Sequence[tuple[SpeedRange, dict[str, str]]], objects: int, top_mm: float) -> str:
    rows = ['<?xml version="1.0" encoding="utf-8"?>', "<objects>"]
    # A range names the object by its position on the plate, counted from 1.
    for position in range(1, objects + 1):
        rows.append(f' <object id="{position}">')
        for band, options in plan:
            top = top_mm if band.to_mm is None else band.to_mm
            rows.append(f'  <range min_z="{band.from_mm:g}" max_z="{top:g}">')
            rows.extend(f'   <option opt_key="{key}">{value}</option>' for key, value in options.items())
            rows.append("  </range>")
        rows.append(" </object>")
    rows.append("</objects>")
    return "\n".join(rows) + "\n"


_BUILD_ITEM = re.compile(r"<(?:\w+:)?item\b")


def plate_3mf(input_path: str, out_dir: str, *, bed_centre: tuple[float, float] | None) -> str:
    """*input_path* as a 3MF the slicer takes where it stands: a 3MF is a
    copy of itself, and a mesh is centred on *bed_centre* (left where the
    file has it when that is ``None``) and set on the plate."""
    from kiln.slicer_supports import _CONTENT_TYPES, _ROOT_RELS

    path = os.path.join(out_dir, f"{Path(input_path).stem}.3mf")
    if input_path.lower().endswith(".3mf"):
        shutil.copyfile(input_path, path)
        return path
    from kiln.printability import _parse_mesh
    from kiln.step_import import resolve_mesh_input

    # A CAD file is sliced as the mesh every mesh tool makes of it.
    mesh_path, _conversion, refusal = resolve_mesh_input(input_path)
    if refusal:
        raise SpeedByHeightError(
            f"{Path(input_path).name} could not be made into a mesh, so the speed ranges were not put into its "
            f"slice: {refusal.get('error') or refusal.get('message') or 'no converter is available'}"
        )
    try:
        facets, _vertices = _parse_mesh(mesh_path)
    except (OSError, ValueError) as exc:
        raise SpeedByHeightError(
            f"{Path(input_path).name} could not be read as a mesh, so the speed ranges were not put into its "
            f"slice: {_plain_words.said(exc)}"
        ) from exc
    corner_ids: dict[tuple[float, ...], int] = {}
    rows = [
        '<triangle v1="{}" v2="{}" v3="{}"/>'.format(*(corner_ids.setdefault(tuple(v), len(corner_ids)) for v in tri))
        for tri in facets
    ]
    if not rows:
        raise SpeedByHeightError(f"{Path(input_path).name} holds no triangles, so there is nothing to slice.")
    xs, ys, zs = zip(*corner_ids, strict=True)
    tx = 0.0 if bed_centre is None else bed_centre[0] - (min(xs) + max(xs)) / 2.0
    ty = 0.0 if bed_centre is None else bed_centre[1] - (min(ys) + max(ys)) / 2.0
    model = "".join([
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<model unit="millimeter" xml:lang="en-US" ',
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" ',
        'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">\n',
        '<metadata name="Application">Kiln</metadata>\n',
        '<metadata name="slic3rpe:Version3mf">1</metadata>\n',
        '<metadata name="BambuStudio:3mfVersion">1</metadata>\n',
        '<resources><object id="1" type="model"><mesh><vertices>\n',
        "\n".join(f'<vertex x="{vx:.9g}" y="{vy:.9g}" z="{vz:.9g}"/>' for vx, vy, vz in corner_ids),
        "\n</vertices><triangles>\n",
        "\n".join(rows),
        "\n</triangles></mesh></object></resources>\n",
        f'<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 {tx:.9g} {ty:.9g} {-min(zs):.9g}"/></build>\n',
        "</model>\n",
    ])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("3D/3dmodel.model", model)
    return path


def with_ranges(
    plate_path: str, out_dir: str, plan: Sequence[tuple[SpeedRange, dict[str, str]]], family: str, *, top_mm: float,
) -> str:
    """A copy of the 3MF at *plate_path*, in *out_dir*, carrying *plan* as
    the height ranges *family*'s slicer reads, for every object on it."""
    from kiln.untrusted_zip import UntrustedZipFile

    member = _RANGE_MEMBER[family]
    path = os.path.join(out_dir, os.path.basename(plate_path))
    with UntrustedZipFile(plate_path) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        names = zin.namelist()
        root = next((n for n in names if n.lower().lstrip("/") == "3d/3dmodel.model"), None)
        if root is None:
            raise SpeedByHeightError("This 3MF has no model Kiln can read, so the speed ranges were not put into its slice.")
        if any(n.lstrip("/") in _RANGE_MEMBER.values() for n in names):
            raise SpeedByHeightError(
                "This 3MF already carries height ranges of its own. Slice it without `speed_by_height`, or take "
                "its ranges out in the slicer that made it."
            )
        objects = max(1, len(_BUILD_ITEM.findall(zin.read(root).decode("utf-8", errors="replace"))))
        for info in zin.infolist():
            zout.writestr(info, zin.read(info.filename))
        zout.writestr(member, _ranges_xml(plan, objects, top_mm))
    return path


# ---------------------------------------------------------------------------
# The slice
# ---------------------------------------------------------------------------


def _judge(
    band: SpeedRange,
    asked: Mapping[str, float],
    profile_speeds: Mapping[str, float],
    made: Reading,
    plain: Reading,
    *,
    first_layer_kept: bool,
) -> dict[str, Any]:
    """One range's outcome, from the slice with the wish (*made*) and the
    slice without it (*plain*)."""
    with_wish, without = _layers_in(made, band), _layers_in(plain, band)
    every = [layer for layer in with_wish if layer.moves]
    first_z = next((layer.z for layer in plain.layers if layer.moves), None)
    kept_first: _Layer | None = None
    if first_layer_kept and with_wish and with_wish[0].z == first_z:
        # The first layer prints at the profile's own first-layer speed.
        kept_first = with_wish[0]
        with_wish, without = with_wish[1:], [layer for layer in without if layer.z != first_z]
    if band.lines == INSIDE_LINES:
        with_wish, without = _inside(with_wish), _inside(without)
    block: dict[str, Any] = {
        "from_mm": band.from_mm, "to_mm": band.to_mm, "asked": band.asked(),
        "measured": _measured(with_wish), "without": {
            "fastest_mm_s": _mm_s(_fastest(without)), "typical_mm_s": _mm_s(_typical(without)),
        },
    }
    span = f"{band.span()}, asked {band.wanted()}"
    if kept_first is not None:
        block["first_layer"] = {"kept_own_speed": True, "fastest_mm_s": _mm_s(_fastest([kept_first]))}
    if band.speed_asked():
        outcome = _judged(block, span, band, asked, profile_speeds, made, with_wish, without)
    else:
        outcome = block
        outcome["outcome"], outcome["said"] = APPLIED, f"{span}: applied."
    if band.max_acceleration is not None and outcome["outcome"] != NOT_APPLIED:
        most = band.max_acceleration * (1.0 + _SAME_SHARE)
        total = held = 0.0
        hardest: float | None = None
        for layer in every:
            for commanded, length in layer.acceleration_lengths.items():
                total += length
                if commanded is not None and commanded <= most:
                    held += length
                if commanded is not None and (hardest is None or commanded > hardest):
                    hardest = commanded
        share = held / total if total > 0 else 0.0
        outcome["acceleration"] = {
            "asked_mm_s2": band.max_acceleration, "hardest_mm_s2": hardest, "held_share": round(share, 3),
        }
        if share >= 1.0 - _FASTER_SHARE_ALLOWED:
            outcome["said"] += f" It speeds up and stops at no more than {band.max_acceleration:g} mm/s² here."
        elif share >= _ACCELERATION_MOSTLY:
            outcome["said"] += (
                f" It speeds up and stops at no more than {band.max_acceleration:g} mm/s² for {share:.0%} of the "
                f"printing here; the rest runs at up to {hardest:g} mm/s², where the printer's own layer-change "
                "routine sets it."
            )
        else:
            reached = f"up to {hardest:g} mm/s²" if hardest is not None else "at the printer's own setting"
            outcome["outcome"] = NOT_APPLIED
            outcome["said"] = (
                f"{span}: not applied. The file still speeds up and stops harder than asked here ({reached}) "
                f"for {1.0 - share:.0%} of its printing."
            )
    if kept_first is not None:
        outcome["said"] += (
            f" The first layer keeps the profile's own first-layer speed (up to {_fastest([kept_first]) or 0:.1f} mm/s)."
        )
    # A slicer takes no support speed for a height range, so supports here
    # print at the profile's own; said when that is faster than was asked.
    supports = block["measured"].get("supports_fastest_mm_s")
    target = band.max_mm_s if band.max_mm_s is not None else max(asked.values(), default=None)
    if band.lines == ALL_LINES and target is not None and supports is not None and supports > target and not _same(
        supports, target
    ):
        outcome["supports"] = {"kept_own_speed": True, "fastest_mm_s": supports}
        outcome["said"] += f" Supports in this range keep the profile's own support speed (up to {supports:.1f} mm/s)."
    return outcome


def _judged(
    block: dict[str, Any],
    span: str,
    band: SpeedRange,
    asked: Mapping[str, float],
    profile_speeds: Mapping[str, float],
    made: Reading,
    with_wish: Sequence[_Layer],
    without: Sequence[_Layer],
) -> dict[str, Any]:
    target = band.max_mm_s if band.max_mm_s is not None else max(asked.values())
    fastest, fastest_before = _fastest(with_wish), _fastest(without)
    if fastest is None or fastest_before is None:
        if "first_layer" in block:
            block["outcome"] = ALREADY_SLOWER
            block["said"] = f"{span}: the only layer here is the first. Nothing changed."
            return block
        block["outcome"] = NOT_APPLIED
        block["said"] = f"{span}: no part of the print could be measured in this range."
        return block
    changed = _print(with_wish) != _print(without)
    reached = f"{fastest:.1f} mm/s"
    if band.max_mm_s is not None and (fastest_before <= target or _same(fastest_before, target)):
        block["outcome"] = ALREADY_SLOWER
        block["said"] = (
            f"{span}: nothing here prints faster than that already (up to {fastest_before:.1f} mm/s), so "
            "nothing changed."
        )
        return block
    if target < fastest_before - _SAME_MM_S and not _same(target, fastest_before):
        # Asked slower than what prints here.
        length = sum(m[1] for layer in with_wish for m in layer.moves)
        faster = sum(
            m[1] for layer in with_wish for m in layer.moves if m[0] > target and not _same(m[0], target)
        )
        if length > 0 and faster / length <= _FASTER_SHARE_ALLOWED:
            block["outcome"] = APPLIED
            block["said"] = f"{span}: applied. It prints at up to {reached} (it was up to {fastest_before:.1f})."
        else:
            kinds = sorted({
                m[3] for layer in with_wish for m in layer.moves if m[0] > target and not _same(m[0], target)
            })
            block["outcome"] = NOT_APPLIED
            block["said"] = (
                f"{span}: not applied. {faster / length:.0%} of the printing here still runs faster than asked, "
                f"up to {reached} ({', '.join(kinds)})."
            )
        return block
    if fastest > target or _same(fastest, target):
        block["outcome"] = APPLIED
        block["said"] = f"{span}: applied. It prints at up to {reached} (it was up to {fastest_before:.1f})."
        return block
    limit = made.flow_limit()
    flows = [(m[1], m[2]) for layer in with_wish for m in layer.moves if m[2] is not None]
    at_limit = sum(length for length, flow in flows if limit is not None and flow >= _AT_FLOW_LIMIT * limit)
    cooling = made.cooling_seconds()
    cooled = cooling is not None and all(layer.seconds <= cooling * _COOLING_SLACK for layer in with_wish)
    pace = max(profile_speeds.values()) if profile_speeds else None
    if changed:
        if at_limit > 0:
            block["outcome"] = CAPPED
            block["said"] = (
                f"{span}: capped. The slicer held it at what the nozzle can melt ({limit:g} mm³/s); it reached "
                f"{reached} (it was up to {fastest_before:.1f})."
            )
        else:
            block["outcome"] = LIMITED
            why = "so these short layers can cool" if cooled else "by its own limits"
            block["said"] = f"{span}: the slicer held it {why}; it reached {reached} (it was up to {fastest_before:.1f})."
        return block
    if cooled or (pace is not None and target < pace and not _same(target, pace)):
        block["outcome"] = ALREADY_SLOWER
        block["said"] = (
            f"{span}: already this slow or slower here (the slicer slows thin sections on its own), nothing "
            f"changed. It prints at up to {reached}."
        )
        return block
    judged = sum(length for length, _flow in flows)
    if judged > 0 and at_limit / judged >= 1.0 - _FASTER_SHARE_ALLOWED * 2:
        block["outcome"] = CAPPED
        block["said"] = (
            f"{span}: capped. The slicer already holds this range at what the nozzle can melt ({limit:g} mm³/s), "
            f"at up to {reached}; nothing changed."
        )
        return block
    block["outcome"] = NOT_APPLIED
    block["said"] = f"{span}: not applied. The sliced file prints this range exactly as it does without the wish."
    return block


def _inside(layers: Sequence[_Layer]) -> list[_Layer]:
    """*layers* with only their infill and inner-wall lines, and only the
    layers that have any."""
    kept = []
    for layer in layers:
        moves = [move for move in layer.moves if move[3].strip().lower() in INSIDE_KINDS]
        if moves:
            kept.append(_Layer(z=layer.z, moves=moves, support_moves=[], seconds=layer.seconds, height=layer.height))
    return kept


def _first_layer_speed(
    ranges: Sequence[SpeedRange], asked: Sequence[Mapping[str, float]], plain: Reading,
) -> float | None:
    """The one speed the first layer prints at when a range that covers it
    asks for less than it prints at now, or ``None`` when the profile's own
    first-layer speed is kept.  Never faster than the slowest line the
    first layer has now."""
    first = next((layer for layer in plain.layers if layer.moves), None)
    if first is None:
        return None
    fastest, slowest = _fastest([first]), _slowest([first])
    if fastest is None or slowest is None:
        return None
    for band, speeds in zip(ranges, asked, strict=True):
        if band.lines != ALL_LINES or not band.speed_asked():
            continue
        if any(layer is first for layer in _layers_in(plain, band)):
            if band.percent is not None:
                wanted = slowest * band.percent / 100.0
            else:
                wanted = band.max_mm_s if band.max_mm_s is not None else max(speeds.values())
            target = min(wanted, slowest)
            if target < fastest and not _same(target, fastest):
                return target
    return None


#: Who chose a slice's ranges when the person did not state them.
PICKED_BY_KILN = "kiln"


def _acceleration_options(most: float) -> dict[str, str]:
    """The settings that hold a range's acceleration at *most*, as Bambu
    Studio spells them: its height slowdown, level across the range, with
    its speed left free."""
    return {
        "enable_height_slowdown": "1", "slowdown_start_height": "0", "slowdown_end_height": "10000",
        "slowdown_start_speed": f"{MAX_SPEED_MM_S:g}", "slowdown_end_speed": f"{MAX_SPEED_MM_S:g}",
        "slowdown_start_acc": f"{most:g}", "slowdown_end_acc": f"{most:g}",
    }


def sliced(
    ranges: Sequence[SpeedRange],
    *,
    family: str,
    out_file: str,
    run: Callable[..., Any],
    planner: Callable[[Reading], Mapping[str, Any]] | None = None,
    eases_acceleration: bool = False,
) -> Any:
    """The slice of a part with *ranges* in it, written to *out_file*.

    *run* makes one slice: ``run(out_path, prepare=..., first_layer_speed=...)``
    returns a slice result, having handed the slicer whatever ``prepare(plate
    input, scratch dir, bed centre)`` returned in place of the input; a run
    told ``record=False`` leaves no record of itself.  The part is sliced
    without the wish first, to a file that is not kept, then with it.

    *planner*, when given, chooses the ranges: it is handed the reading of
    the slice without any wish and answers ``{"ranges": [...], "said":
    [...], "ask": ...}``, the ranges in the wish's own form, each with a
    ``why``.  With no ranges the part is sliced as it was and the result
    says so.  *eases_acceleration* is whether this slicer can hold a range's
    acceleration; a range that asks it of one that cannot is refused.

    Raises :class:`SpeedByHeightError` for a range above the part and for a
    range the slicer did not apply; the file is then removed.
    """
    scratch = tempfile.mkdtemp(prefix="kiln_speed_by_height_")
    try:
        plain_file = os.path.join(scratch, "without.gcode")
        run(plain_file, prepare=lambda path, out_dir, centre: plate_3mf(path, out_dir, bed_centre=centre), record=False)
        plain = read_gcode(plain_file)
        if not plain.layers:
            raise SpeedByHeightError("The slice could not be read back, so the speed ranges were not put into it.")
        top = plain.layers[-1].z
        notes: Mapping[str, Any] = {}
        whys: dict[tuple[float, float | None], str] = {}
        if planner is not None:
            notes = planner(plain)
            # A chosen range starts and ends where a layer does, so every
            # layer is wholly in one range or wholly out of it.
            tops = [layer.z for layer in plain.layers if layer.moves]
            picked = []
            for one in notes.get("ranges") or []:
                if not isinstance(one, Mapping):
                    continue
                one = dict(one)
                for edge in ("from_mm", "to_mm"):
                    value = _number(one.get(edge))
                    if value is not None and value > 0:
                        one[edge] = min(tops, key=lambda z: abs(z - value))
                if one.get("to_mm") is None or one["to_mm"] > (_number(one.get("from_mm")) or 0.0):
                    picked.append(one)
            ranges = checked([{k: v for k, v in one.items() if k != "why"} for one in picked])
            whys = {
                (float(one.get("from_mm", 0)), None if one.get("to_mm") is None else float(one["to_mm"])): str(one["why"])
                for one in picked if one.get("why")
            }
            if not ranges:
                result = run(
                    out_file, prepare=lambda path, out_dir, centre: plate_3mf(path, out_dir, bed_centre=centre),
                    record=True,
                )
                unchanged: dict[str, Any] = {"ranges": [], "outside_ranges": "unchanged"}
                if plain.seconds is not None:
                    unchanged["estimate_seconds"] = unchanged["estimate_without_seconds"] = plain.seconds
                return _noted(result, _planned(unchanged, notes, []), out_file)
        for band in ranges:
            if band.max_acceleration is not None and not eases_acceleration:
                raise SpeedByHeightError(
                    f"The range {band.span()} asks for gentler acceleration, and this slicer cannot hold "
                    "acceleration by height. Bambu Studio can. Take max_acceleration_mm_s2 out, or slice with "
                    "Bambu Studio.",
                    code="SPEED_BY_HEIGHT_NO_ACCELERATION",
                )
        profile_speeds = _stated_speeds(plain, family)
        asked = [_asked_speeds(band, profile_speeds, family) for band in ranges]
        for band in ranges:
            if not _layers_in(plain, band):
                raise SpeedByHeightError(
                    f"The range {band.span()} holds no layer of this part: as sliced it prints from "
                    f"{plain.layers[0].z:g} mm to {top:g} mm, in layers of about {plain.layers[-1].height:g} mm. "
                    "Give heights inside the part, each range at least a layer tall.",
                    code="SPEED_BY_HEIGHT_OUTSIDE_PART",
                )
        # A range states its layer height too, and it is the slice's own.
        layer_mm = _first_positive(plain.settings.get("layer_height")) or _usual_height(plain)
        plan = [
            (band, {
                "layer_height": f"{layer_mm:g}", **_options(speeds),
                **({} if band.max_acceleration is None else _acceleration_options(band.max_acceleration)),
            })
            for band, speeds in zip(ranges, asked, strict=True)
        ]
        first_speed = _first_layer_speed(ranges, asked, plain)

        def _prepare(path: str, out_dir: str, centre: tuple[float, float] | None) -> str:
            plate_dir = tempfile.mkdtemp(prefix="plate_", dir=scratch)
            return with_ranges(
                plate_3mf(path, plate_dir, bed_centre=centre), out_dir, plan, family, top_mm=math.ceil(top) + 1.0,
            )

        result = run(out_file, prepare=_prepare, first_layer_speed=first_speed, record=True)
        made = read_gcode(out_file)
        outcomes = [
            _judge(band, speeds, profile_speeds, made, plain, first_layer_kept=first_speed is None)
            for band, speeds in zip(ranges, asked, strict=True)
        ]
        refused = [o for o in outcomes if o["outcome"] == NOT_APPLIED]
        if refused:
            for leftover in (out_file, out_file + ".made.3mf"):
                if os.path.isfile(leftover):
                    os.unlink(leftover)
            raise SpeedByHeightError(
                "The slicer did not apply every speed range, so no file was kept. "
                + " ".join(o["said"] for o in refused)
                + " Slice again without `speed_by_height`, or with other ranges.",
                code="SPEED_BY_HEIGHT_NOT_APPLIED", ranges=outcomes,
            )
        inside = {id(layer) for band in ranges for layer in _layers_in(made, band)}
        inside_plain = {id(layer) for band in ranges for layer in _layers_in(plain, band)}
        outside = [layer for layer in made.layers if id(layer) not in inside and layer.moves]
        outside_plain = [layer for layer in plain.layers if id(layer) not in inside_plain and layer.moves]
        block: dict[str, Any] = {
            "ranges": outcomes,
            "outside_ranges": "unchanged" if _prints_alike(outside, outside_plain) else "changed",
        }
        if made.seconds is not None:
            block["estimate_seconds"] = made.seconds
        if plain.seconds is not None:
            block["estimate_without_seconds"] = plain.seconds
        for outcome in outcomes:
            why = whys.get((outcome["from_mm"], outcome["to_mm"]))
            if why:
                outcome["why"] = why
        spans = {(band.from_mm, band.to_mm): band.span() for band in ranges}
        said = " ".join(
            f"{spans[(o['from_mm'], o['to_mm'])]}: {o['why']} In the sliced file: {o['said'].split(': ', 1)[-1]}"
            if o.get("why") else o["said"]
            for o in outcomes
        )
        if block["outside_ranges"] == "changed":
            said += " Outside the ranges the slicer also changed some speeds."
        if made.seconds is not None and plain.seconds is not None:
            from kiln.generation.validation import _human_duration

            said += (
                f" The slicer's time is about {_human_duration(float(made.seconds))} with these ranges and about "
                f"{_human_duration(float(plain.seconds))} without."
            )
        block["said"] = said
        if planner is not None:
            block = _planned(block, notes, [said])
        return _noted(result, block, out_file)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _planned(block: dict[str, Any], notes: Mapping[str, Any], said: list[str]) -> dict[str, Any]:
    """*block* as a slice whose ranges a planner chose: who chose them, what
    the planner says of the whole part, and its one question."""
    block["picked_by"] = PICKED_BY_KILN
    block["headline"] = _headline(block)
    said = [block["headline"], *said]
    lines = [str(line) for line in notes.get("said") or [] if line]
    if lines:
        block["notes"] = lines
    ask = notes.get("ask")
    if isinstance(ask, Mapping) and ask.get("question"):
        block["ask"] = dict(ask)
        lines = [*lines, str(ask["question"])]
    block["said"] = " ".join([*said, *lines]).strip()
    return block


def _headline(block: Mapping[str, Any]) -> str:
    """One line a person reads first: how many heights Kiln changed, which
    way, and what it did to the slicer's time."""
    ranges = [one for one in block.get("ranges") or [] if isinstance(one, Mapping)]
    asked = [one.get("asked") or {} for one in ranges]
    gentle = sum(1 for one in asked if "max_mm_s" in one or ("max_acceleration_mm_s2" in one and "percent" not in one))
    quicker = sum(1 for one in asked if one.get("lines") == INSIDE_LINES)
    if not ranges:
        return "Kiln changed no speeds for this part."
    parts = []
    if gentle:
        parts.append(f"{gentle} {'stretch' if gentle == 1 else 'stretches'} gently")
    if quicker:
        parts.append(f"{quicker} {'stretch' if quicker == 1 else 'stretches'} faster")
    head = "Kiln prints " + " and ".join(parts) if parts else f"Kiln set speeds for {len(ranges)} heights"
    with_wish, without = block.get("estimate_seconds"), block.get("estimate_without_seconds")
    if isinstance(with_wish, int) and isinstance(without, int) and with_wish != without:
        from kiln.generation.validation import _human_duration

        change = "longer" if with_wish > without else "shorter"
        head += f"; the print is about {_human_duration(float(abs(with_wish - without)))} {change}"
    return head + "."


def _noted(result: Any, block: dict[str, Any], out_file: str) -> Any:
    """*result* carrying *block*, with the slice's own record told of it."""
    result.speed_by_height = block
    result.message = f"{result.message} Speed by height: {block['said']}".strip()
    from kiln.print_lineage import note_speed_by_height

    note_speed_by_height(out_file, kept_form(block))
    return result


# ---------------------------------------------------------------------------
# The wish on a record
# ---------------------------------------------------------------------------


def kept_form(block: Any) -> dict[str, Any] | None:
    """The small form of a slice's ``speed_by_height`` a record keeps: each
    range's heights, what was asked, its outcome and the fastest speed
    measured, and the slicer's two times.  ``None`` for anything else."""
    if not isinstance(block, Mapping) or not isinstance(block.get("ranges"), Sequence):
        return None
    ranges = []
    for one in block["ranges"]:
        if not isinstance(one, Mapping) or not isinstance(one.get("asked"), Mapping):
            return None
        measured = one.get("measured") if isinstance(one.get("measured"), Mapping) else {}
        ranges.append({
            "from_mm": one.get("from_mm"), "to_mm": one.get("to_mm"), "asked": dict(one["asked"]),
            "outcome": one.get("outcome"),
            "fastest_mm_s": measured.get("fastest_mm_s", one.get("fastest_mm_s")),
        })
    kept: dict[str, Any] = {"ranges": ranges}
    if block.get("picked_by") == PICKED_BY_KILN:
        kept["picked_by"] = PICKED_BY_KILN
    for key in ("estimate_seconds", "estimate_without_seconds"):
        if isinstance(block.get(key), int) and not isinstance(block.get(key), bool):
            kept[key] = block[key]
    return kept


def keep_wish(provenance: dict[str, Any] | None, sliced_path: str | None) -> dict[str, Any] | None:
    """*provenance* (a copy) holding the speed ranges the sliced file at
    *sliced_path* was made with, as Kiln's own record of that slice states
    them, and no other: a value already there, or handed in by a caller, is
    replaced by Kiln's record or dropped when it has none.  ``None`` when
    nothing is left."""
    from kiln.print_lineage import speed_by_height_of

    kept = {k: v for k, v in (provenance or {}).items() if k != RECORD_KEY}
    wish = kept_form(speed_by_height_of(sliced_path))
    if wish is not None:
        kept[RECORD_KEY] = wish
    return kept or None


def wish_sentence(provenance: Any) -> str | None:
    """``"Sliced with its own speeds by height: 0 mm to 6 mm at 50% ..."`` for
    a version's *provenance*, or ``None`` when it keeps no wish."""
    wish = kept_form(provenance.get(RECORD_KEY)) if isinstance(provenance, Mapping) else None
    if not wish or not wish["ranges"]:
        return None
    parts = []
    for one in wish["ranges"]:
        asked = one["asked"]
        try:
            band = SpeedRange(
                float(one["from_mm"]), None if one["to_mm"] is None else float(one["to_mm"]),
                speed_mm_s=asked.get("speed_mm_s"), percent=asked.get("percent"), max_mm_s=asked.get("max_mm_s"),
                lines=asked.get("lines", ALL_LINES), max_acceleration=asked.get("max_acceleration_mm_s2"),
            )
        except (TypeError, ValueError):
            return None
        joined = " at " if band.speed_mm_s is not None or band.percent is not None else ", "
        parts.append(f"{band.span()}{joined}{band.wanted()} ({str(one.get('outcome') or '').replace('_', ' ')})")
    chosen = "speeds Kiln chose by height" if wish.get("picked_by") == PICKED_BY_KILN else "its own speeds by height"
    return f"Sliced with {chosen}: " + "; ".join(parts) + "."


__all__ = [
    "ALREADY_SLOWER",
    "APPLIED",
    "CAPPED",
    "INSIDE_KINDS",
    "LIMITED",
    "NOT_APPLIED",
    "PICKED_BY_KILN",
    "RECORD_KEY",
    "SpeedByHeightError",
    "SpeedRange",
    "checked",
    "keep_wish",
    "kept_form",
    "read_gcode",
    "sliced",
    "wish_sentence",
]
