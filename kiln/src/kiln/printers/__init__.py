"""Printer adapter package.

Re-exports the public API from the base module so consumers can write::

    from kiln.printers import PrinterAdapter, PrinterState, ...
"""

from __future__ import annotations

from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from kiln.printers.bambu import BambuAdapter
from kiln.printers.moonraker import MoonrakerAdapter
from kiln.printers.octoprint import OctoPrintAdapter

__all__ = [
    "BambuAdapter",
    "JobProgress",
    "MoonrakerAdapter",
    "OctoPrintAdapter",
    "PrinterAdapter",
    "PrinterCapabilities",
    "PrinterError",
    "PrinterFile",
    "PrinterState",
    "PrinterStatus",
    "PrintResult",
    "UploadResult",
]
