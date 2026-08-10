"""Printer adapter package.

Re-exports the public API from the base module so consumers can write::

    from kiln.printers import PrinterAdapter, PrinterState, ...
"""

from __future__ import annotations

from kiln.printers.base import (
    DeviceAdapter,
    DeviceType,
    FirmwareComponent,
    FirmwareStatus,
    FirmwareUpdateResult,
    IdentityConflict,
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterInfo,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)

try:
    from kiln.printers.bambu import BambuAdapter
except ImportError:
    BambuAdapter = None  # type: ignore[assignment,misc]

try:
    from kiln.printers.elegoo import ElegooAdapter
except ImportError:
    ElegooAdapter = None  # type: ignore[assignment,misc]

from kiln.printers.creality import CrealityAdapter
from kiln.printers.duet import DuetAdapter
from kiln.printers.moonraker import MoonrakerAdapter
from kiln.printers.octoprint import OctoPrintAdapter
from kiln.printers.prusalink import PrusaLinkAdapter
from kiln.printers.serial_adapter import SerialPrinterAdapter

__all__ = [
    "BambuAdapter",
    "CrealityAdapter",
    "DeviceAdapter",
    "DeviceType",
    "DuetAdapter",
    "ElegooAdapter",
    "FirmwareComponent",
    "FirmwareStatus",
    "FirmwareUpdateResult",
    "IdentityConflict",
    "JobProgress",
    "MoonrakerAdapter",
    "OctoPrintAdapter",
    "PrinterAdapter",
    "PrinterCapabilities",
    "PrinterError",
    "PrinterFile",
    "PrinterInfo",
    "PrinterState",
    "PrinterStatus",
    "PrintResult",
    "PrusaLinkAdapter",
    "SerialPrinterAdapter",
    "UploadResult",
]
