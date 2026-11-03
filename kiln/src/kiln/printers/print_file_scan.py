"""The G-code safety scan on the file a printer is about to receive.

Every file reaches a printer through two methods of its adapter: ``upload_file``
(wrapped for every backend in ``PrinterAdapter.__init_subclass__``) and
``start_print`` (the pre-print gate, ``kiln.printers.print_gate``).  Both read
the file through :func:`scan_print_file`, so a heater target above the
printer's ceiling, or a command the scanner refuses, is refused the same way
whichever door carried the file: the upload tool, a reslice, a pipeline, the
CLI, the bridge relay.

A bare G-code file is scanned as it is.  A sliced 3MF is scanned on the plate
G-code it carries (``Metadata/plate_N.gcode``, the bytes the printer runs);
a project 3MF with no plate has nothing to scan and :func:`scan_print_file`
says so by answering ``None``.  Until 2026-11-02 only bare G-code was
scanned, and only at the upload tool and the pipelines: a reslice wrapped as
a Bambu 3MF went to the printer with its temperatures unread.
"""

from __future__ import annotations

import os
from typing import Any

from kiln.gcode import GCodeDialect, GCodeValidationResult, scan_gcode_file

#: The code every refusal from this scan carries, at every door.
REFUSAL_CODE = "GCODE_BLOCKED"

#: How many of the scanner's findings a refusal sentence quotes.
_QUOTED_ERRORS = 3

_GCODE_EXTENSIONS = (".gcode", ".gco", ".g")


def is_gcode(path: str) -> bool:
    """Whether *path* is a bare G-code file by its name."""
    return os.path.splitext(path)[1].lower() in _GCODE_EXTENSIONS


def is_threemf(path: str) -> bool:
    """Whether *path* is a 3MF by its name (``.3mf`` and ``.gcode.3mf``)."""
    return path.lower().endswith(".3mf")


def dialect_for(printer_type: str | None, *, path: str | None = None) -> GCodeDialect:
    """The dialect a file for *printer_type* (an adapter's ``name``) is
    scanned in, so a command that is standard for that firmware is not
    refused by a rule written for another.  A 3MF plate for a backend with
    no dialect of its own is read as Bambu's: the plate layout is Bambu
    Studio's, and the Bambu adapter is the one that starts a 3MF."""
    kind = (printer_type or "").strip().lower()
    if kind == "bambu":
        return GCodeDialect.BAMBU
    if kind in ("moonraker", "creality"):
        return GCodeDialect.KLIPPER
    if path and is_threemf(path):
        return GCodeDialect.BAMBU
    return GCodeDialect.GENERIC


def scan_print_file(
    path: str,
    *,
    printer_id: str | None,
    dialect: GCodeDialect,
) -> GCodeValidationResult | None:
    """The safety scan (:func:`kiln.gcode.scan_gcode_file`) on what *path*
    would have the printer run: the file itself for G-code, the plate G-code
    for a sliced 3MF.  ``None`` when there is nothing to scan -- a mesh, or
    a 3MF with no plate -- so a caller can say the file was not scanned
    rather than call it clean.

    :raises FileNotFoundError: when *path* does not exist.
    """
    if is_gcode(path):
        return scan_gcode_file(path, printer_id=printer_id or None, dialect=dialect)
    if is_threemf(path):
        from kiln.printers.bed_fit import _plate_gcode_file

        with _plate_gcode_file(path) as plate:
            if plate is None:
                return None
            return scan_gcode_file(plate, printer_id=printer_id or None, dialect=dialect)
    return None


def refusal_sentence(path: str, scan: GCodeValidationResult, *, verb: str) -> str:
    """One sentence for a scan that refused *path*: the file, what did not
    happen to it (*verb*: ``"uploaded"`` / ``"started"``), and the scanner's
    own findings."""
    findings = "; ".join(scan.errors[:_QUOTED_ERRORS]) or "blocked G-code"
    return (
        f"{os.path.basename(path)} contains blocked G-code commands and was "
        f"not {verb}: {findings}."
    )


def refusal(path: str, scan: GCodeValidationResult, *, verb: str) -> dict[str, Any]:
    """The refusal block every door builds its answer from."""
    return {
        "code": REFUSAL_CODE,
        "reason": refusal_sentence(path, scan, verb=verb),
        "blocked_commands": scan.blocked_commands[:10],
        "errors": scan.errors[:10],
    }
