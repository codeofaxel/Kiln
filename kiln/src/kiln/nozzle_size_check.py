"""Is the nozzle a print file was sliced for the nozzle the printer says it has?

Two numbers: the size the file states (:func:`kiln.gcode.slicer_nozzle_diameters`)
and the size the printer reports as its setting
(:meth:`kiln.printers.base.PrinterAdapter.read_nozzle_setting`).  When both
can be read and they differ, one of them is wrong.  When either cannot be
read, nothing is claimed: ``unchecked`` is never ``match``.

One helper for every door -- the start gate, the pre-flight -- so no door
grows its own idea of what "differs" means.  Sends nothing to the printer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The code a refused start carries.
MISMATCH_CODE = "NOZZLE_SIZE_MISMATCH"

#: Two sizes closer than this are the same size.
_SAME_SIZE_MM = 0.005


@dataclass(frozen=True)
class NozzleSizeCheck:
    """One comparison.

    :param status: ``"match"``, ``"differs"``, or ``"unchecked"`` (one side
        could not say; *why* names which).
    :param file_mm: The one size the file states, when it states one.
    :param printer_mm: The size the printer reports as its setting.
    :param held_by: Whose record the printer's figure is
        (:attr:`kiln.printers.base.NozzleSetting.held_by`).
    """

    status: str
    file_mm: float | None = None
    printer_mm: float | None = None
    held_by: str | None = None
    why: str | None = None

    @property
    def refuses(self) -> bool:
        """Whether a start is refused: the sizes differ, and the printer's
        figure is the machine's own setting."""
        return self.status == "differs" and self.held_by == "machine"

    def sentence(self, file_name: str = "") -> str:
        """The comparison in words, for a refusal or a pre-flight row."""
        name = file_name or "This file"
        if self.status == "match":
            return f"{name} was sliced for the {self.file_mm:g} mm nozzle the printer says it has."
        if self.status != "differs":
            return (
                f"Kiln could not compare the nozzle {name} was sliced for with the printer's: "
                f"{self.why or 'one of the two could not be read'}."
            )
        if not self.refuses:
            return (
                f"{name} was sliced for a {self.file_mm:g} mm nozzle, and the printer profile kept by "
                f"the software driving this printer says {self.printer_mm:g} mm. That profile is not "
                "the printer's own setting, so Kiln does not refuse the start; check which is right."
            )
        return (
            f"{name} was sliced for a {self.file_mm:g} mm nozzle, and this printer's own setting says a "
            f"{self.printer_mm:g} mm nozzle is fitted. One of the two is wrong, and Kiln does not start a "
            "print while they disagree: plastic laid out for one nozzle size comes out the wrong width "
            f"through another. If a {self.printer_mm:g} mm nozzle is fitted, slice the part again for "
            f"{self.printer_mm:g} mm; if a {self.file_mm:g} mm one is, correct the nozzle setting on the printer."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "file_mm": self.file_mm,
            "printer_mm": self.printer_mm,
            "held_by": self.held_by,
            "why": self.why,
        }


def _sizes_in(lines: list[str] | None) -> set[float]:
    from kiln.gcode import slicer_nozzle_diameters

    if not lines:
        return set()
    return {round(size, 3) for statement in slicer_nozzle_diameters("\n".join(lines)) for size in statement}


def _plate_sizes(job_path: str, plate_number: int | None) -> list[set[float]]:
    """The nozzle sizes each plate that may print states, one set per plate.

    With *plate_number* named, that plate alone.  With none named, or one
    the file does not hold, every plate of a sliced 3MF: which one prints is
    not known, so each is checked."""
    from kiln.file_metadata import every_plate_gcode_lines, plate_of_print, printed_gcode_lines

    if plate_number is not None and plate_of_print(job_path, plate_number) is not None:
        return [_sizes_in(printed_gcode_lines(job_path, plate_number))]
    # No plate named, or the one named is not one the file holds by the
    # name the printer asks for: every plate is checked, never none.
    return [_sizes_in(lines) for lines in every_plate_gcode_lines(job_path) or []]


def _stated_sizes(job_path: str, plate_number: int | None = None) -> set[float]:
    """Every distinct nozzle size the plates of *job_path* that may print state."""
    return set().union(*_plate_sizes(job_path, plate_number))


def file_nozzle_mm(job_path: str, plate_number: int | None = None) -> float | None:
    """The one nozzle size the file at *job_path* was sliced for, or ``None``
    when it states none (a mesh, a slicer that writes no such line) or more
    than one (a multi-nozzle slice, or plates sliced for different sizes)."""
    sizes = _stated_sizes(job_path, plate_number)
    return next(iter(sizes)) if len(sizes) == 1 else None


def check_nozzle_size(adapter: Any, job_path: str, plate_number: int | None = None) -> NozzleSizeCheck:
    """Compare the plate of *job_path* that prints with *adapter*'s own
    nozzle setting.

    *plate_number* names the plate printed.  With none named, every plate
    of a sliced 3MF is compared and the first that differs answers, so a
    plate that would be refused is never passed because it was not the one
    read.

    ``unchecked`` whenever either side cannot say -- the plate states no one
    size, the backend has no nozzle setting, the read failed or ran past its
    deadline, or the printer's last report is older than its own freshness
    budget.  Never raises.
    """
    from kiln.printers.base import NozzleSetting

    try:
        plates = _plate_sizes(job_path, plate_number)
    except Exception:  # noqa: BLE001 -- a file that cannot be read states nothing
        logger.debug("nozzle size: file unreadable", exc_info=True)
        plates = []
    singles = [next(iter(sizes)) for sizes in plates if len(sizes) == 1]
    if not plates or len(singles) != len(plates):
        several = any(len(sizes) > 1 for sizes in plates)
        return NozzleSizeCheck(
            "unchecked",
            why="the file states more than one nozzle size" if several else "the file states no nozzle size",
        )
    file_mm = singles[0]

    try:
        from kiln.printer_nozzle_reading import read_setting

        setting = read_setting(adapter)
    except Exception:  # noqa: BLE001 -- a read that fails is a read that did not happen
        logger.debug("nozzle size: printer read failed", exc_info=True)
        return NozzleSizeCheck("unchecked", file_mm=file_mm, why="the printer could not be asked for its nozzle setting")
    if not isinstance(setting, NozzleSetting) or setting.diameter_mm is None:
        return NozzleSizeCheck("unchecked", file_mm=file_mm, why="this printer reports no nozzle size")
    if (
        setting.age_seconds is not None
        and setting.stale_after_seconds is not None
        and setting.age_seconds > setting.stale_after_seconds
    ):
        return NozzleSizeCheck(
            "unchecked", file_mm=file_mm, why="the printer's last report is too old to rely on",
        )
    printer_mm = float(setting.diameter_mm)
    differing = next((size for size in singles if abs(printer_mm - size) >= _SAME_SIZE_MM), None)
    if differing is None:
        return NozzleSizeCheck("match", file_mm=file_mm, printer_mm=printer_mm, held_by=setting.held_by)
    return NozzleSizeCheck("differs", file_mm=differing, printer_mm=printer_mm, held_by=setting.held_by)


__all__ = ["MISMATCH_CODE", "NozzleSizeCheck", "check_nozzle_size", "file_nozzle_mm"]
