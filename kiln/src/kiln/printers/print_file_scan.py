"""The G-code safety scan on the file a printer is about to receive.

A sliced 3MF reaches a printer through two methods of its adapter:
``upload_file`` (wrapped for every backend in
``PrinterAdapter.__init_subclass__``) and ``start_print`` (the pre-print
gate, ``kiln.printers.print_gate``).  Both read its plate through
:func:`scan_print_file`, so a heater target above the printer's ceiling is
refused the same way whichever door carried the 3MF.  The upload tool
scans bare G-code through it too; bare G-code sent by another door (the
CLI's upload, a plugin) is not scanned at the adapter, because those doors
take files this scan has never been measured against.

A sliced 3MF is scanned on the plate G-code it carries
(``Metadata/plate_N.gcode``, the bytes the printer runs): at start, the
plate that will print; at upload, where no plate is chosen yet, every
plate, refused only when none of them could start.  A project 3MF with no
plate has nothing to scan, and :func:`scan_print_file` says so by
answering ``None``.

A print file is judged as a file, not as a command Kiln is asked to send:
a firmware macro a slicer wrote (``PRINT_START``, ``SET_PRINT_STATS_INFO``)
passes with only the heat it asks for judged, and a printer whose own
limits Kiln does not hold (signed out, offline, a model it has no record
of) is judged against the generic limits rather than the least capable
machine Kiln describes, and the answer says which.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import zipfile
from collections.abc import Iterator
from typing import Any

from kiln.gcode import _MAX_SCAN_BYTES, GCodeDialect, GCodeValidationResult, scan_gcode_file

#: The code every refusal from this scan carries, at every door.
REFUSAL_CODE = "GCODE_BLOCKED"

#: How many of the scanner's findings a refusal sentence quotes.
_QUOTED_ERRORS = 3

_GCODE_EXTENSIONS = (".gcode", ".gco", ".g")

#: A sliced 3MF's plate G-code, numbered.
_PLATE_MEMBER_RE = re.compile(r"^Metadata/plate_(\d+)\.gcode$")

_COPY_CHUNK_BYTES = 1024 * 1024


def is_gcode(path: str) -> bool:
    """Whether *path* is a bare G-code file by its name."""
    return os.path.splitext(path)[1].lower() in _GCODE_EXTENSIONS


def is_threemf(path: str) -> bool:
    """Whether *path* is a 3MF by its name (``.3mf`` and ``.gcode.3mf``)."""
    return path.lower().endswith(".3mf")


def dialect_for(printer_type: str | None, *, path: str | None = None) -> GCodeDialect:
    """The dialect a file for *printer_type* (an adapter's ``name``) is
    scanned in, so a command that is standard for that firmware is not
    refused by a rule written for another.  Elegoo's network printers run
    Klipper-family firmware and take a 3MF, so their plates are read as
    Klipper's (their slicer templates pause with ``M600``, which Bambu's
    rules refuse).  A 3MF plate for a backend with no dialect of its own is
    read as Bambu's: the plate layout is Bambu Studio's."""
    kind = (printer_type or "").strip().lower()
    if kind == "bambu":
        return GCodeDialect.BAMBU
    if kind in ("moonraker", "creality", "elegoo"):
        return GCodeDialect.KLIPPER
    if path and is_threemf(path):
        return GCodeDialect.BAMBU
    return GCodeDialect.GENERIC


def _plate_members(zf: zipfile.ZipFile, plate_number: int | None) -> list[tuple[int, str]]:
    """The plates to scan, as ``(number, member)``: the one *plate_number*
    names (none when the archive lacks it), else every plate in order.  A
    3MF with G-code but no numbered plate is read as one plate."""
    names = [n for n in zf.namelist() if n.lower().endswith(".gcode")]
    plates = sorted(
        (int(m.group(1)), n) for n in names if (m := _PLATE_MEMBER_RE.match(n))
    )
    if not plates:
        return [(1, names[0])] if names else []
    if plate_number is not None:
        return [(number, name) for number, name in plates if number == plate_number]
    return plates


def _unreadable(sentence: str) -> GCodeValidationResult:
    """A refusal for a plate Kiln could not read, as a bare file over the
    size cap is refused: unscanned is never sent as if it were clean."""
    result = GCodeValidationResult(valid=False)
    result.errors.append(sentence)
    return result


@contextlib.contextmanager
def _plate_on_disk(zf: zipfile.ZipFile, member: str) -> Iterator[str | GCodeValidationResult]:
    """The plate *member* copied to a temp G-code file a chunk at a time --
    never held whole in memory -- or the refusal when it is too large to
    scan.  (How it is packed was judged when the archive opened.)"""
    info = zf.getinfo(member)
    fd, tmp_path = tempfile.mkstemp(suffix=".gcode")
    try:
        copied = 0
        with os.fdopen(fd, "wb") as dst, zf.open(info) as src:
            while chunk := src.read(_COPY_CHUNK_BYTES):
                copied += len(chunk)
                if copied > _MAX_SCAN_BYTES:
                    break
                dst.write(chunk)
        if copied > _MAX_SCAN_BYTES:
            yield _unreadable(
                f"The plate inside this file is too large to scan (over "
                f"{_MAX_SCAN_BYTES / 1024 / 1024:.0f} MB), so it was not checked"
            )
        else:
            yield tmp_path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _scan(path: str, printer_id: str | None, dialect: GCodeDialect) -> GCodeValidationResult:
    """One G-code file, judged as a print file."""
    return scan_gcode_file(
        path,
        printer_id=printer_id or None,
        dialect=dialect,
        generic_when_unread=True,
        macros=True,
        keep_commands=False,
    )


def scan_print_file(
    path: str,
    *,
    printer_id: str | None,
    dialect: GCodeDialect,
    plate_number: int | None = None,
) -> GCodeValidationResult | None:
    """The safety scan (:func:`kiln.gcode.scan_gcode_file`) on what *path*
    would have the printer run: the file itself for G-code, the plate G-code
    for a sliced 3MF -- the plate *plate_number* names, or, with none named
    (an upload), every plate.  An upload whose plates are not all refused
    passes, with the refused plates named in its warnings: the start gate
    refuses one of them if it is chosen.

    ``None`` when there is nothing to scan -- a mesh, a 3MF with no plate,
    or no plate by that number -- so a caller can say the file was not
    scanned rather than call it clean.

    :raises FileNotFoundError: when *path* does not exist.
    """
    if is_gcode(path):
        return _with_limits_note(_scan(path, printer_id, dialect), printer_id)
    if not is_threemf(path):
        return None
    from kiln.untrusted_zip import UntrustedArchiveError, UntrustedZipFile

    if not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {os.path.abspath(path)}")
    try:
        zf = UntrustedZipFile(path)
    except UntrustedArchiveError as exc:
        return _unreadable(f"Kiln could not read the plate inside this file ({exc}), so it was not checked")
    except (zipfile.BadZipFile, ValueError):
        return None  # not an archive at all: no plate to scan
    scanned: list[tuple[int, GCodeValidationResult]] = []
    with zf:
        for number, member in _plate_members(zf, plate_number):
            with _plate_on_disk(zf, member) as plate:
                result = plate if isinstance(plate, GCodeValidationResult) else _scan(plate, printer_id, dialect)
            scanned.append((number, result))
    if not scanned:
        return None
    refused = [(number, result) for number, result in scanned if not result.valid]
    if len(refused) == len(scanned):
        return _with_limits_note(refused[0][1], printer_id)
    passed = next(result for _, result in scanned if result.valid)
    for number, result in refused:
        passed.warnings.insert(
            0,
            f"Plate {number} would be refused if it were started: "
            + "; ".join(result.errors[:_QUOTED_ERRORS]),
        )
    return _with_limits_note(passed, printer_id)


def _limits_clause(printer_id: str | None) -> str:
    """Why the generic limits judged this file, as a clause."""
    if not printer_id:
        return "Kiln could not tell which printer this file is for"
    from kiln._pro_catalogue_bridge import why_unanswered

    spelling = str(printer_id).strip()
    cause = why_unanswered(spelling) or why_unanswered(spelling.lower().replace("-", "_"))
    if cause == "signed_out":
        return (
            "this install is signed out, so Kiln could not read this printer's own "
            "limits; sign in free and Kiln checks files against them"
        )
    if cause == "offline":
        return "this computer is offline, so Kiln could not read this printer's own limits"
    if cause == "unanswered":
        return "Kiln's servers did not answer, so Kiln could not read this printer's own limits"
    if cause == "refused":
        return "Kiln's servers refused the request for this printer's own limits"
    return "Kiln has no record of this printer's own limits"


def _with_limits_note(result: GCodeValidationResult, printer_id: str | None) -> GCodeValidationResult:
    """*result*, saying on a clean file that its temperatures were judged
    against the generic limits, and why."""
    if result.valid and result.generic_limits:
        result.warnings.append(
            "Kiln checked this file's temperatures against its general limits, "
            f"not this printer's own: {_limits_clause(printer_id)}."
        )
    return result


def refusal_sentence(
    path: str, scan: GCodeValidationResult, *, verb: str, printer_id: str | None = None,
) -> str:
    """One sentence for a scan that refused *path*: the file, what did not
    happen to it (*verb*: ``"uploaded"`` / ``"started"``), the line, and
    what to do."""
    errors = scan.errors[:_QUOTED_ERRORS]
    findings = "; ".join(errors) or "a line Kiln refuses"
    hot = any(" exceeds " in e for e in errors)
    if hot:
        what = f"it heats past what the printer allows ({findings})"
        todo = "Re-slice it with every temperature at or below that limit."
        if scan.generic_limits:
            todo += f" That limit is Kiln's general one, because {_limits_clause(printer_id)}."
    elif any("no readable number" in e for e in errors):
        what = f"Kiln could not read a temperature in it ({findings})"
        todo = "Re-slice it, or write that temperature as a plain number."
    elif errors and errors[0].startswith(("Kiln could not read the plate", "The plate inside", "File is too large")):
        what, todo = findings, ""
    else:
        what = f"it contains a command Kiln does not send to a printer ({findings})"
        todo = "Remove that line, or re-slice without it."
    return f"{os.path.basename(path)} was not {verb}: {what}. {todo}".rstrip()


def refusal(
    path: str, scan: GCodeValidationResult, *, verb: str, printer_id: str | None = None,
) -> dict[str, Any]:
    """The refusal block every door builds its answer from."""
    return {
        "code": REFUSAL_CODE,
        "reason": refusal_sentence(path, scan, verb=verb, printer_id=printer_id),
        "blocked_commands": scan.blocked_commands[:10],
        "errors": scan.errors[:10],
    }
