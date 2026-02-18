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

try:
    from kiln.printers.bambu import BambuAdapter
except ImportError:
    BambuAdapter = None  # type: ignore[assignment,misc]

try:
    from kiln.printers.elegoo import ElegooAdapter
except ImportError:
    ElegooAdapter = None  # type: ignore[assignment,misc]

from kiln.printers.moonraker import MoonrakerAdapter
from kiln.printers.octoprint import OctoPrintAdapter
from kiln.printers.prusaconnect import PrusaConnectAdapter
from kiln.printers.serial_adapter import SerialPrinterAdapter

__all__ = [
    "BambuAdapter",
    "DeviceAdapter",
    "DeviceType",
    "ElegooAdapter",
    "FirmwareComponent",
    "FirmwareStatus",
    "FirmwareUpdateResult",
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
    "PrusaConnectAdapter",
    "SerialPrinterAdapter",
    "UploadResult",
]
