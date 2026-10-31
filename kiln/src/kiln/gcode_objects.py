"""The objects a sliced file prints, as the file itself names them.

A slicer that labels objects writes a marker where each object's moves
begin and end, in its own dialect:

* Bambu Studio / OrcaSlicer: ``; start printing object, unique label id: 757``
  (older builds: ``; start printing object, id: 757``) and the matching
  ``; stop printing object`` line.  The id is the one a Bambu printer's own
  skip-object command takes.
* Klipper (PrusaSlicer, OrcaSlicer, SuperSlicer with object labelling):
  ``EXCLUDE_OBJECT_START NAME=part_1`` / ``EXCLUDE_OBJECT_END``.  The name
  is what Klipper's ``EXCLUDE_OBJECT`` takes.
* PrusaSlicer (Marlin flavour): ``; printing object cube.stl id:0 copy 1``
  and ``; stop printing object ...``; each copy is its own object.
* Marlin cancel-object: ``M486 S3`` starts object 3 and ``M486 S-1`` ends
  it; the index is what ``M486 P`` takes.

This module is the one reader of those markers in Kiln.  It says WHICH
objects the file prints, what the printer calls each one, and on which
layers each one has moves.  It judges nothing: whether an object is still
ahead of the nozzle, what to do about it, and how to route a material change
are the business of whoever asked.  :mod:`kiln.printers.safe_motion` reads
the same markers for each object's footprint, through the matchers here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

#: Lines read before a scan gives up: a sliced file is long, and a scan
#: that stopped early must say so rather than report fewer objects.
MAX_LINES = 5_000_000

_START_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("klipper", re.compile(r"^EXCLUDE_OBJECT_START\s+NAME=(?P<name>\S+)", re.IGNORECASE)),
    ("bambu", re.compile(
        r"^;\s*start printing object\s*(?:,\s*)?(?:unique\s+label\s+)?id:\s*(?P<name>\S+)", re.IGNORECASE,
    )),
    ("prusa", re.compile(r"^;\s*printing object\s+(?P<name>\S.*\S|\S)$", re.IGNORECASE)),
    ("marlin", re.compile(r"^M486\s+S(?P<name>\d+)\b", re.IGNORECASE)),
)
_END_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^EXCLUDE_OBJECT_END\b", re.IGNORECASE),
    re.compile(r"^;\s*stop printing object\b", re.IGNORECASE),
    re.compile(r"^M486\s+S-1\b", re.IGNORECASE),
)
#: A PrusaSlicer object line names the model and which copy of it this is:
#: ``cube.stl id:0 copy 1``, read from the right so the model name needs no
#: backtracking.
_PRUSA_COPY = re.compile(r"\s+id:(?P<id>\d+)\s+copy\s+(?P<copy>\d+)$", re.IGNORECASE)
#: A marker line is short; a longer line is not one, and is never matched.
MAX_MARKER_LINE = 512

# Layer markers, one per slicer family.  Bambu/Orca number theirs; Prusa
# and Cura count ``;LAYER_CHANGE`` lines; Cura and others may state
# ``;LAYER:N``; a few write ``; layer N, ...``.
_BAMBU_LAYER = re.compile(r"^;\s*layer\s+num\s*/\s*total_layer_count\s*:\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)
_COUNTING_LAYER = re.compile(r"^;\s*LAYER_CHANGE\s*$", re.IGNORECASE)
_EXPLICIT_LAYER = re.compile(r"^;\s*LAYER\s*:\s*(\d+)", re.IGNORECASE)
_GENERIC_LAYER = re.compile(r"^;\s*layer\s+(\d+)\s*,", re.IGNORECASE)


def match_object_start(line: str) -> tuple[str, str] | None:
    """``(dialect, name)`` when *line* (stripped) opens an object's block."""
    if len(line) > MAX_MARKER_LINE:
        return None
    for dialect, pattern in _START_PATTERNS:
        found = pattern.match(line)
        if found:
            name = found.group("name").strip()
            if name:
                return dialect, name
    return None


def is_object_end(line: str) -> bool:
    """Whether *line* (stripped) closes an object's block."""
    return len(line) <= MAX_MARKER_LINE and any(p.match(line) for p in _END_PATTERNS)


def layer_marker(line: str, counted: int) -> tuple[int | None, int]:
    """``(layer this line starts, counted layers so far)``; the layer is
    ``None`` for a line that starts none."""
    if len(line) > MAX_MARKER_LINE:
        return None, counted
    found = _BAMBU_LAYER.match(line)
    if found:
        return int(found.group(1)), counted
    if _COUNTING_LAYER.match(line):
        return counted + 1, counted + 1
    found = _EXPLICIT_LAYER.match(line) or _GENERIC_LAYER.match(line)
    if found:
        return max(1, int(found.group(1))), counted
    return None, counted


@dataclass
class ObjectBlock:
    """One object the file prints."""

    #: What the printer's own skip takes: a Bambu label id, a Klipper
    #: object name, a Marlin M486 index.
    identity: str
    #: What a person would call it: the Klipper or Prusa name, or for a
    #: Bambu file (whose names live in the 3MF beside it) ``object <id>``.
    name: str
    dialect: str
    first_layer: int | None = None
    last_layer: int | None = None
    #: Runs of layers with moves, as ``[start, end]`` pairs.
    segments: list[list[int]] = field(default_factory=list)

    def touch(self, layer: int, *, opening: bool) -> None:
        layer = max(1, layer)
        if self.first_layer is None:
            self.first_layer = layer
        self.last_layer = layer
        if opening or not self.segments:
            self.segments.append([layer, layer])
        else:
            self.segments[-1][1] = layer


@dataclass
class ObjectsScan:
    objects: dict[str, ObjectBlock]
    #: The scan stopped at :data:`MAX_LINES`; what it found is a part.
    truncated: bool = False
    layers_seen: int = 0


def _named(dialect: str, raw: str) -> tuple[str, str]:
    """``(identity, name)`` for a start marker's text."""
    if dialect == "prusa":
        found = _PRUSA_COPY.search(raw)
        if found:
            model = raw[: found.start()].strip()
            ident = f"{model} id:{found.group('id')} copy {found.group('copy')}"
            return ident, ident
        return raw, raw
    if dialect == "bambu":
        return raw, f"object {raw}"
    return raw, raw


def read_object_blocks(lines: Iterable[str], *, max_lines: int = MAX_LINES) -> ObjectsScan:
    """Every object *lines* print, with the layers each has moves on.

    Layers come from the file's own layer markers; before the first one
    everything is layer 1.  A block left open at a layer change keeps
    running on the new layer.  The scan reads at most *max_lines* and says
    when it stopped early.
    """
    scan = ObjectsScan(objects={})
    open_now: dict[str, ObjectBlock] = {}
    layer = 0
    counted = 0
    for i, raw in enumerate(lines):
        if i >= max_lines:
            scan.truncated = True
            break
        line = raw.strip()
        if not line or line[0] not in ";EeMm":
            continue
        started, counted = layer_marker(line, counted)
        if started is not None:
            layer = started
            scan.layers_seen = max(scan.layers_seen, layer)
            for block in open_now.values():
                block.touch(layer, opening=False)
            continue
        opened = match_object_start(line)
        if opened is not None:
            dialect, raw_name = opened
            identity, name = _named(dialect, raw_name)
            block = scan.objects.get(identity)
            if block is None:
                block = ObjectBlock(identity=identity, name=name, dialect=dialect)
                scan.objects[identity] = block
            block.touch(layer, opening=True)
            open_now[identity] = block
            continue
        if is_object_end(line):
            for block in open_now.values():
                block.touch(layer, opening=False)
            open_now.clear()
    return scan


def read_object_blocks_from_path(path: str, *, plate_number: int = 1) -> ObjectsScan | None:
    """The objects a ``.gcode`` file, or the named plate of a sliced
    ``.3mf``, prints; ``None`` for a file that is neither."""
    import io
    import zipfile
    from pathlib import Path

    from kiln.untrusted_zip import UntrustedZipFile

    file = Path(path)
    if not file.is_file():
        return None
    if zipfile.is_zipfile(file):
        from kiln.gcode import _MAX_SCAN_BYTES
        from kiln.gcode_metadata import read_member_text, sliced_gcode_member

        try:
            with UntrustedZipFile(file) as zf:
                names = set(zf.namelist())
                member = next(
                    (c for c in (f"Metadata/plate_{plate_number}.gcode", f"metadata/plate_{plate_number}.gcode")
                     if c in names),
                    None,
                ) or sliced_gcode_member(zf)
                if member is None:
                    return None
                text = read_member_text(zf, member, _MAX_SCAN_BYTES)
        except (zipfile.BadZipFile, KeyError, OSError, ValueError):
            return None
        return read_object_blocks(io.StringIO(text))
    try:
        with open(file, encoding="utf-8", errors="replace") as handle:
            return read_object_blocks(handle)
    except OSError:
        return None


__all__ = [
    "MAX_LINES",
    "ObjectBlock",
    "ObjectsScan",
    "is_object_end",
    "layer_marker",
    "match_object_start",
    "read_object_blocks",
    "read_object_blocks_from_path",
]
