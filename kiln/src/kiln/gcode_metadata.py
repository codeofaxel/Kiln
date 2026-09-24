"""G-code metadata extraction for the Kiln project.

Parses G-code file headers to extract print metadata (material, time,
temperatures, slicer info) so the agent can reason about files even when
filenames are meaningless like ``test5112.gcode``.

Supports comment formats from:
    - PrusaSlicer / OrcaSlicer / BambuStudio
    - Cura / CuraEngine
    - Simplify3D

Usage::

    from kiln.gcode_metadata import extract_metadata, enrich_printer_file

    meta = extract_metadata("/path/to/file.gcode")
    print(meta.material, meta.estimated_time_seconds)

    enrich_printer_file(printer_file, file_content=gcode_string)
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any, BinaryIO

from kiln.gcode import slicer_filament_totals, slicer_print_time

logger = logging.getLogger(__name__)

# Bambu Studio writes its whole settings block at the TOP, and it runs to
# about line 560 (measured on its files for the A1 mini, P1P, P1S, P2S, X1C,
# X1E and H2S) — printer model at 371, nozzle size at 312.  A 200- or
# 300-line window read the material and missed the rest.
_MAX_HEADER_LINES: int = 1000
# PrusaSlicer writes its totals (estimated printing time, filament used) at
# the END of the file — a header-only scan answered "unknown" for time and
# filament on every PrusaSlicer gcode, which is Kiln's primary slicer.
# 1000, not a mirror of the header window: PrusaSlicer appends its whole
# config dump (~350+ lines) AFTER the totals, so a small tail window reads
# only config and misses the numbers it exists to find.  OrcaSlicer's
# totals and settings start about 745 lines (21 KB) from the end.
_MAX_FOOTER_LINES: int = 1000
_FOOTER_BYTES: int = 64 * 1024
#: The most of a file's top that is read, however few line breaks it has.
#: Bambu Studio's 560-line settings block is about 45 KB.  A budget in
#: bytes, not only lines, is what keeps one enormous line — or a package
#: member that unpacks to gigabytes — from being read whole.
_HEADER_BYTES: int = 1024 * 1024


def head_and_tail(lines: Iterable[str]) -> list[str]:
    """The lines a slicer writes its metadata in: the top and the end.

    The first :data:`_MAX_HEADER_LINES` lines, then the last
    :data:`_MAX_FOOTER_LINES`; a text that fits in the first window is
    returned whole.  The one window every reader of a G-code file's own
    metadata uses, so none of them sees less of the file than another.
    """
    head: list[str] = []
    tail: deque[str] = deque(maxlen=_MAX_FOOTER_LINES)
    for line in lines:
        if len(head) < _MAX_HEADER_LINES:
            head.append(line)
        else:
            tail.append(line)
    return head + list(tail)


def _decoded_lines(data: bytes) -> list[str]:
    return data.decode("utf-8", errors="replace").splitlines()


_LINE_BREAKS = (b"\n", b"\r")


def _read_window(fh: BinaryIO, size: int, *, tail: bool) -> list[str]:
    """The window of an open binary stream of *size* bytes, read in bounded
    pieces: at most :data:`_HEADER_BYTES` from the top and
    :data:`_FOOTER_BYTES` from the end.  A line cut by either edge is left
    out; a line that starts or ends exactly on an edge is kept — one byte
    past the top budget, and one before the end window, says which."""
    head = fh.read(_HEADER_BYTES + 1)
    if len(head) >= size:
        # The whole text is already in hand.
        return head_and_tail(_decoded_lines(head))
    after, head = head[_HEADER_BYTES:], head[:_HEADER_BYTES]
    lines = _decoded_lines(head)
    if lines and not head.endswith(_LINE_BREAKS) and after not in _LINE_BREAKS:
        lines.pop()  # the budget ended part-way through a line
    lines = lines[:_MAX_HEADER_LINES]
    if tail:
        start = max(0, size - _FOOTER_BYTES)
        fh.seek(max(0, start - 1))
        piece = fh.read(_FOOTER_BYTES + (1 if start else 0))
        before, piece = (piece[:1], piece[1:]) if start else (b"\n", piece)
        tail_lines = _decoded_lines(piece)
        if tail_lines and before not in _LINE_BREAKS:
            tail_lines = tail_lines[1:]  # the window began part-way through a line
        lines.extend(tail_lines[-_MAX_FOOTER_LINES:])
    return lines


def read_head_and_tail(file_path: str) -> list[str]:
    """:func:`head_and_tail` of a file on disk, reading only its two ends.

    The tail is taken from the final 64 KB, which holds the last
    :data:`_MAX_FOOTER_LINES` lines of every slicer's output measured
    (OrcaSlicer's totals and settings run to about 21 KB).

    :raises OSError: when the file cannot be read.
    """
    with open(file_path, "rb") as fh:
        return _read_window(fh, os.fstat(fh.fileno()).st_size, tail=True)


def read_head(fh: BinaryIO, size: int) -> list[str]:
    """The top window of an open binary stream of *size* bytes.

    For a G-code member of a package (a UFP): its end can only be reached
    by unpacking all of it, so at most its first 1 MB is read.  A member
    that small is read whole, end included.
    """
    return _read_window(fh, size, tail=False)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class GCodeMetadata:
    """Structured metadata extracted from a G-code file header."""

    material: str | None = None
    estimated_time_seconds: int | None = None
    tool_temp: float | None = None
    bed_temp: float | None = None
    slicer: str | None = None
    layer_height: float | None = None
    filament_used_mm: float | None = None
    printer_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict, omitting ``None`` values."""
        return {k: v for k, v in asdict(self).items() if v is not None}


# ---------------------------------------------------------------------------
# Comment-based metadata patterns
# ---------------------------------------------------------------------------

# PrusaSlicer / OrcaSlicer / BambuStudio patterns
_RE_PRUSA_MATERIAL = re.compile(
    r";\s*filament_type\s*=\s*(.+)",
    re.IGNORECASE,
)
_RE_PRUSA_TOOL_TEMP = re.compile(
    r";\s*(?:temperature|nozzle_temperature)\s*=\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_PRUSA_BED_TEMP = re.compile(
    r";\s*bed_temperature\s*=\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_PRUSA_LAYER_HEIGHT = re.compile(
    r";\s*layer_height\s*=\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_PRUSA_SLICER = re.compile(
    r";\s*generated by\s+(.+)",
    re.IGNORECASE,
)
_RE_PRUSA_PRINTER_MODEL = re.compile(
    r";\s*printer_model\s*=\s*(.+)",
    re.IGNORECASE,
)

# Cura patterns
_RE_CURA_MATERIAL = re.compile(
    r";\s*MATERIAL\s*[:=]\s*(.+)",
    re.IGNORECASE,
)
_RE_CURA_LAYER_HEIGHT = re.compile(
    r";\s*Layer height\s*[:=]\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_CURA_SLICER = re.compile(
    r";\s*Generated with\s+(.+)",
    re.IGNORECASE,
)
_RE_CURA_MACHINE = re.compile(
    r";\s*MACHINE_TYPE\s*[:=]\s*(.+)",
    re.IGNORECASE,
)

# Simplify3D patterns
_RE_S3D_TOOL_TEMP = re.compile(
    r";\s*extruder\d*Temp\s*,\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_S3D_BED_TEMP = re.compile(
    r";\s*platformTemp\s*,\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_S3D_LAYER_HEIGHT = re.compile(
    r";\s*layerHeight\s*,\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_S3D_SLICER = re.compile(
    r";\s*Simplify3D\(R\)\s*Version\s+(.+)",
    re.IGNORECASE,
)

# M-command temperature patterns (fallback)
_RE_M_COMMAND = re.compile(r"^[Mm](104|109|140|190)\s")
_RE_S_PARAM = re.compile(r"[Ss]\s*(\d+\.?\d*)")


# ---------------------------------------------------------------------------
# Material normalisation
# ---------------------------------------------------------------------------


def _normalize_material(raw: str) -> str:
    """Normalise a material name: uppercase, stripped, common aliases unified."""
    normalised = raw.strip().upper()
    # Unify common aliases
    _ALIASES: dict[str, str] = {
        "POLYLACTIC ACID": "PLA",
        "POLYETHYLENE TEREPHTHALATE": "PETG",
        "ACRYLONITRILE BUTADIENE STYRENE": "ABS",
        "THERMOPLASTIC POLYURETHANE": "TPU",
    }
    return _ALIASES.get(normalised, normalised)


# ---------------------------------------------------------------------------
# Core extraction from lines
# ---------------------------------------------------------------------------


def _extract_from_lines(lines: list[str]) -> GCodeMetadata:
    """Parse metadata from a list of G-code lines (first N header lines)."""
    meta = GCodeMetadata()

    # Filament used: every extruder's value, summed, in mm — read once by
    # the one reader for what a slicer says.
    text = "\n".join(lines)
    totals = slicer_filament_totals(text)
    if totals.mm:
        meta.filament_used_mm = totals.total_mm
    printed = slicer_print_time(text)
    if printed is not None:
        meta.estimated_time_seconds = printed.seconds

    # Track whether temps came from comments (preferred) vs M-commands (fallback)
    _tool_temp_from_comment = False
    _bed_temp_from_comment = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # --- Comment-based patterns (highest priority) ---
        if stripped.startswith(";"):
            # Material
            if meta.material is None:
                for pat in (_RE_PRUSA_MATERIAL, _RE_CURA_MATERIAL):
                    m = pat.match(stripped)
                    if m:
                        meta.material = _normalize_material(m.group(1))
                        break

            # Tool temperature (from comment)
            if meta.tool_temp is None or not _tool_temp_from_comment:
                for pat in (_RE_PRUSA_TOOL_TEMP, _RE_S3D_TOOL_TEMP):
                    m = pat.match(stripped)
                    if m:
                        try:
                            meta.tool_temp = float(m.group(1))
                            _tool_temp_from_comment = True
                        except ValueError:
                            pass
                        break

            # Bed temperature (from comment)
            if meta.bed_temp is None or not _bed_temp_from_comment:
                for pat in (_RE_PRUSA_BED_TEMP, _RE_S3D_BED_TEMP):
                    m = pat.match(stripped)
                    if m:
                        try:
                            meta.bed_temp = float(m.group(1))
                            _bed_temp_from_comment = True
                        except ValueError:
                            pass
                        break

            # Layer height
            if meta.layer_height is None:
                for pat in (_RE_PRUSA_LAYER_HEIGHT, _RE_CURA_LAYER_HEIGHT, _RE_S3D_LAYER_HEIGHT):
                    m = pat.match(stripped)
                    if m:
                        with contextlib.suppress(ValueError):
                            meta.layer_height = float(m.group(1))
                        break

            # Slicer identification
            if meta.slicer is None:
                for pat in (_RE_PRUSA_SLICER, _RE_CURA_SLICER, _RE_S3D_SLICER):
                    m = pat.match(stripped)
                    if m:
                        meta.slicer = m.group(1).strip()
                        break

            # Printer model
            if meta.printer_model is None:
                for pat in (_RE_PRUSA_PRINTER_MODEL, _RE_CURA_MACHINE):
                    m = pat.match(stripped)
                    if m:
                        meta.printer_model = m.group(1).strip()
                        break

            continue

        # --- M-command temperature fallback ---
        # Only use if no comment-based temp was found
        if not _tool_temp_from_comment and meta.tool_temp is None:
            cmd_match = re.match(r"^[Mm](104|109)\b", stripped)
            if cmd_match:
                s_match = _RE_S_PARAM.search(stripped)
                if s_match:
                    try:
                        temp = float(s_match.group(1))
                        if temp > 0:  # Ignore M104 S0 (heater off)
                            meta.tool_temp = temp
                    except ValueError:
                        pass

        if not _bed_temp_from_comment and meta.bed_temp is None:
            cmd_match = re.match(r"^[Mm](140|190)\b", stripped)
            if cmd_match:
                s_match = _RE_S_PARAM.search(stripped)
                if s_match:
                    try:
                        temp = float(s_match.group(1))
                        if temp > 0:  # Ignore M140 S0 (heater off)
                            meta.bed_temp = temp
                    except ValueError:
                        pass

    return meta


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_metadata(file_path: str) -> GCodeMetadata:
    """Extract metadata from a G-code file's own comments.

    Reads the top and the end of the file (:func:`read_head_and_tail`), where
    slicers write their metadata.  Also scans for the first temperature
    commands (M104/M109/M140/M190) as fallback for tool/bed temps.

    :param file_path: Path to a G-code file.
    :returns: A :class:`GCodeMetadata` with whatever fields could be parsed.
        Never raises on parse errors -- returns empty metadata on failure.
    """
    try:
        # Header lines come first, so a value written at the top still wins
        # over the same key at the end (_extract_from_lines keeps the first).
        return _extract_from_lines(read_head_and_tail(file_path))
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.warning("Could not read G-code file for metadata: %s", exc)
        return GCodeMetadata()
    except Exception as exc:
        logger.warning("Unexpected error extracting G-code metadata: %s", exc)
        return GCodeMetadata()


def extract_metadata_from_content(content: str) -> GCodeMetadata:
    """Extract metadata from G-code content string (for in-memory use).

    :param content: G-code file content as a string.
    :returns: A :class:`GCodeMetadata` with whatever fields could be parsed.
    """
    try:
        return _extract_from_lines(head_and_tail(content.splitlines()))
    except Exception as exc:
        logger.warning("Unexpected error extracting metadata from content: %s", exc)
        return GCodeMetadata()


def enrich_printer_file(
    printer_file: Any,
    file_content: str | None = None,
) -> None:
    """Enrich a PrinterFile with metadata from G-code content.

    Mutates *printer_file* in-place, filling in metadata fields.
    If *file_content* is ``None``, this is a no-op.

    :param printer_file: A :class:`~kiln.printers.base.PrinterFile` instance.
    :param file_content: Optional G-code file content string.
    """
    if file_content is None:
        return

    try:
        meta = extract_metadata_from_content(file_content)
        if meta.material is not None and printer_file.material is None:
            printer_file.material = meta.material
        if meta.estimated_time_seconds is not None and printer_file.estimated_time_seconds is None:
            printer_file.estimated_time_seconds = meta.estimated_time_seconds
        if meta.tool_temp is not None and printer_file.tool_temp is None:
            printer_file.tool_temp = meta.tool_temp
        if meta.bed_temp is not None and printer_file.bed_temp is None:
            printer_file.bed_temp = meta.bed_temp
        if meta.slicer is not None and printer_file.slicer is None:
            printer_file.slicer = meta.slicer
        if meta.layer_height is not None and printer_file.layer_height is None:
            printer_file.layer_height = meta.layer_height
        if meta.filament_used_mm is not None and printer_file.filament_used_mm is None:
            printer_file.filament_used_mm = meta.filament_used_mm
    except Exception as exc:
        logger.warning("Failed to enrich PrinterFile with metadata: %s", exc)
