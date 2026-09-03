"""Printer adapter package.

Re-exports the public API from the base module so consumers can write::

    from kiln.printers import PrinterAdapter, PrinterState, ...
"""

from __future__ import annotations

from kiln.printers.base import (
    BUSY_STATES,
    CAUSE_CONNECTION_LIMIT,
    CAUSE_POWERED_OFF,
    CAUSE_SILENT,
    CAUSE_WRONG_ACCESS_CODE,
    INDETERMINATE_STATES,
    READY_STATES,
    STALE_CADENCE_MULTIPLIER,
    STALE_STATE_MAX_AGE,
    STALE_STATE_WARN_AGE,
    UNREACHABLE_STATES,
    DeviceAdapter,
    DeviceType,
    FilamentHandlingUnsupported,
    FilamentOpPlan,
    FilamentOpResult,
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
    ReadDiagnosis,
    TelemetryCadence,
    UploadResult,
    as_status,
    describe_stale_state,
    diagnose_read_failure,
    diagnosed_state,
    format_error_code,
    probe_tcp,
    read_status,
    reconcile_job_with_state,
    status_is_occupied,
    status_is_unreachable,
    stuck_job_note,
)
from kiln.printers.progress_motion import (
    Motion,
    MotionVerdict,
    ProgressSample,
    latest_verdict,
    observe_progress,
    progress_stall_note,
    reset_progress_observations,
    stall_threshold_seconds,
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
    "BUSY_STATES",
    "CAUSE_CONNECTION_LIMIT",
    "CAUSE_POWERED_OFF",
    "CAUSE_SILENT",
    "CAUSE_WRONG_ACCESS_CODE",
    "INDETERMINATE_STATES",
    "READY_STATES",
    "STALE_CADENCE_MULTIPLIER",
    "STALE_STATE_MAX_AGE",
    "STALE_STATE_WARN_AGE",
    "UNREACHABLE_STATES",
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
    "Motion",
    "MotionVerdict",
    "OctoPrintAdapter",
    "PrinterAdapter",
    "PrinterCapabilities",
    "PrinterError",
    "PrinterFile",
    "PrinterInfo",
    "PrinterState",
    "PrinterStatus",
    "FilamentHandlingUnsupported",
    "FilamentOpPlan",
    "FilamentOpResult",
    "PrintResult",
    "ProgressSample",
    "PrusaLinkAdapter",
    "SerialPrinterAdapter",
    "TelemetryCadence",
    "ReadDiagnosis",
    "UploadResult",
    "as_status",
    "describe_stale_state",
    "diagnose_read_failure",
    "format_error_code",
    "latest_verdict",
    "observe_progress",
    "probe_tcp",
    "progress_stall_note",
    "read_status",
    "reconcile_job_with_state",
    "reset_progress_observations",
    "stall_threshold_seconds",
    "status_is_occupied",
    "status_is_unreachable",
    "stuck_job_note",
    "diagnosed_state",
]
