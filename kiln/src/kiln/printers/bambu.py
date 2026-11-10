"""Bambu Lab adapter for the Kiln printer abstraction layer."""

from __future__ import annotations

import contextlib
import ftplib
import hashlib
import hmac
import json
import logging
import os
import posixpath
import socket
import ssl
import struct
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import paho.mqtt.client as mqtt

from kiln.bambu_trays import EXTERNAL_SPOOL_TRAY as _EXTERNAL_SPOOL_TRAY
from kiln.bambu_trays import NO_TRAY as _NO_TRAY
from kiln.bambu_trays import TrayRef as _TrayRef
from kiln.bambu_trays import describe_tray_id as _describe_tray_id
from kiln.bambu_trays import feeding_record as _feeding_record
from kiln.bambu_trays import read_extruder_slot as _read_extruder_slot
from kiln.bambu_trays import read_tray_id as _read_tray_id
from kiln.bambu_trays import tray_id as _bambu_tray_id
from kiln.bambu_trays import tray_name as _bambu_tray_name
from kiln.bambu_trays import unit_name as _bambu_unit_name
from kiln.cli_command import kiln_command
from kiln.printer_backends import need_where
from kiln.printer_intelligence import chamber_sensor_for_model
from kiln.printers.bambu_hms_text import (
    device_type_from_serial,
    kick_background_refresh,
    lookup_screen_text,
)
from kiln.printers.base import (
    DEFAULT_PURGE_LENGTH_MM,
    FILAMENT_FEED_RATE_MM_MIN,
    NOT_A_PICTURE,
    STALE_STATE_WARN_AGE,
    CameraHandshake,
    CameraStreamError,
    FilamentHandlingUnsupported,
    FilamentOpPlan,
    FilamentOpResult,
    HomeResult,
    HomingUnsupported,
    JobProgress,
    JobResult,
    NozzleClumpingDetection,
    NozzleSetting,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterInfo,
    PrinterState,
    PrinterStatus,
    PrintResult,
    StreamCapability,
    TelemetryCadence,
    UploadResult,
    _record_print_duration,
    capture_rtsp_frame,
    describe_fault_remedy,
    describe_unacknowledged_fault,
    diagnose_read_failure,
    diagnosed_state,
    find_ffmpeg,
    format_error_code,
    is_resume_mode_3mf,
    outcome_printer_name,
)
from kiln.printers.command_verdict import CommandVerdict
from kiln.printers.progress_motion import forget_job_start, job_elapsed_seconds
from kiln.snapshot_analysis import picture_format
from kiln.untrusted_zip import UntrustedZipFile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MQTT_PORT = 8883
_FTPS_PORT = 990
_MQTT_USERNAME = "bblp"
_FTPS_USERNAME = "bblp"
_TLS_MODE_PIN = "pin"
_TLS_MODE_CA = "ca"
_TLS_MODE_INSECURE = "insecure"
_VALID_TLS_MODES = {_TLS_MODE_PIN, _TLS_MODE_CA, _TLS_MODE_INSECURE}
_DEFAULT_TLS_MODE = _TLS_MODE_PIN
_DEFAULT_BAMBU_PIN_FILE = os.path.join(str(Path.home()), ".kiln", "bambu_tls_pins.json")
_TLS_PIN_FILE_ENV = "KILN_BAMBU_TLS_PIN_FILE"
_TLS_MODE_ENV = "KILN_BAMBU_TLS_MODE"
_TLS_FINGERPRINT_ENV = "KILN_BAMBU_TLS_FINGERPRINT"

_CONNACK_CREDENTIAL_REFUSALS: frozenset[int] = frozenset({4, 5, 134, 135})


def _connack_code(reason_code: Any) -> int:
    """The number in a CONNACK answer, whichever paho callback style sent it."""
    if reason_code is None:
        return 0
    value = getattr(reason_code, "value", None)
    if isinstance(value, int):
        return value
    try:
        return int(reason_code)
    except (TypeError, ValueError):
        return 0


_SINGLE_CLIENT_MSG = (
    "MQTT connection rejected — something else is already connected. "
    "Bambu printers allow only a few LAN clients at once."
)
_SINGLE_CLIENT_FTPS_MSG = (
    "FTPS TLS handshake failed — something else may be holding the "
    "connection. Bambu printers allow only a few LAN clients at once."
)

_DEFAULT_IDLE_DISCONNECT_S: float = 120.0
_IDLE_DISCONNECT_ENV = "KILN_BAMBU_IDLE_DISCONNECT_S"

_BACKOFF_INITIAL_DELAY: float = 1.0  # seconds
_BACKOFF_MULTIPLIER: float = 2.0
_BACKOFF_MAX_DELAY: float = 30.0  # seconds
_STALE_STATE_MAX_AGE: float = STALE_STATE_WARN_AGE
_FTPS_MAX_RETRIES: int = 3


# ---------------------------------------------------------------------------
# Backoff tracking
# ---------------------------------------------------------------------------


@dataclass
class _BackoffState:
    """Tracks exponential backoff for MQTT reconnection attempts."""

    attempt_count: int = 0
    last_attempt_time: float = 0.0
    next_retry_time: float = 0.0

    def record_failure(self) -> None:
        """Record a failed connection attempt and advance the backoff window."""
        now = time.monotonic()
        self.attempt_count += 1
        self.last_attempt_time = now
        delay = min(
            _BACKOFF_INITIAL_DELAY * (_BACKOFF_MULTIPLIER ** (self.attempt_count - 1)),
            _BACKOFF_MAX_DELAY,
        )
        self.next_retry_time = now + delay
        logger.debug(
            "MQTT backoff: attempt #%d, next retry in %.1fs",
            self.attempt_count,
            delay,
        )

    def record_success(self) -> None:
        """Reset backoff state after a successful connection."""
        self.attempt_count = 0
        self.last_attempt_time = time.monotonic()
        self.next_retry_time = 0.0

    def in_cooldown(self) -> bool:
        """Return ``True`` if the backoff cooldown period has not yet elapsed."""
        return time.monotonic() < self.next_retry_time


# Mapping from Bambu ``gcode_state`` strings to :class:`PrinterStatus`
_STATE_MAP: dict[str, PrinterStatus] = {
    "idle": PrinterStatus.IDLE,
    "finish": PrinterStatus.IDLE,
    "running": PrinterStatus.PRINTING,
    "prepare": PrinterStatus.BUSY,
    "slicing": PrinterStatus.BUSY,
    "init": PrinterStatus.BUSY,
    "pause": PrinterStatus.PAUSED,
    "failed": PrinterStatus.ERROR,
    "cancelling": PrinterStatus.CANCELLING,
    "offline": PrinterStatus.OFFLINE,
    "unknown": PrinterStatus.UNKNOWN,
}

# How the last job ENDED, for the ``gcode_state`` values that say so
_JOB_RESULT_MAP: dict[str, JobResult] = {
    "finish": JobResult.COMPLETED,
}

# States that indicate a print job is active or starting.
_PRINT_ACTIVE_STATES: frozenset[str] = frozenset(
    {
        "running",
        "prepare",
        "slicing",
        "init",
    }
)

_SPEED_PROFILES: dict[str, int] = {
    "silent": 1,
    "standard": 2,
    "sport": 3,
    "ludicrous": 4,
}
_SPEED_PROFILE_NAMES: dict[int, str] = {v: k for k, v in _SPEED_PROFILES.items()}

_ACCESS_CODE_WHERE = need_where("bambu", "access_code")

_KNOWN_PRINT_ERRORS: dict[int, str] = {
    84033543: (
        "Printer rejected the command (error 0502-4007: authentication expired). "
        "This happens when the printer is restarted — the access code becomes stale. "
        f"FIX: Find the access code {_ACCESS_CODE_WHERE}, turn LAN Only Mode "
        "OFF then ON, then toggle Developer Mode OFF and ON. "
        "Copy the NEW access code and update your Kiln config "
        "(kiln config set access_code <new_code>). "
        "The old access code will NOT work even if it looks the same — "
        "you must regenerate it."
    ),
}

_NOZZLE_CLUMP_ERROR_PREFIXES: tuple[str, ...] = (
    "03008014",   # Nozzle clumping detection by probing (A1 series)
    "03001A00",   # Nozzle wrapped in filament / plate placement
    "03001800",   # Nozzle clumping calibration failure
)

_NOZZLE_CLUMP_SCHEDULE = (
    "first probe after the first object's walls on layer 3, then once per "
    "~8 g of filament consumed"
)

_NOZZLE_CLUMP_MESSAGE = (
    "Nozzle clumping / blob detection paused the print (HMS 0300-xxxx). "
    "On the A1 / A1 mini the printer taps the nozzle just off the bed to feel "
    "for a melted blob (" + _NOZZLE_CLUMP_SCHEDULE + ").  This is often a false "
    "positive on thin or flat first-layer geometry (grips, cases, bezels) or an "
    "unseated build plate.  FIX: clear any gunk on the nozzle tip and resume, or "
    "retry with nozzle_clog_detect=False to skip the probe.  "
    "CLI: " + kiln_command("print <file> --no-nozzle-check") + ".  "
    "MCP: start_print(file, nozzle_clog_detect=False)."
)


_HOMED_AXIS_BITS: dict[str, int] = {"x": 0, "y": 1, "z": 2}
HOMED_AXES_FIELD = "print.home_flag[bits 0-2]"

_CLUMP_HOME_FLAG_ON_BIT = 24
_CLUMP_HOME_FLAG_SUPPORTED_BIT = 25
_CLUMP_CFG_ON_BIT = 24
_CLUMP_FUN_SUPPORTED_BIT = 13
_CLUMP_FUN2_THREE_WAY_BIT = 15
_CLUMP_CFG_THREE_WAY_SHIFT = 43
_CLUMP_THREE_WAY_MODES: dict[int, str] = {0: "off", 1: "on", 2: "auto"}
#: What ``nozzle_blob_detect_v2`` takes for each three-way mode.
_CLUMP_THREE_WAY_VALUES: dict[str, int] = {"off": 0, "on": 1, "auto": 2}
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

CLUMP_SOURCE_HOME_FLAG = "bambu_mqtt_home_flag"
CLUMP_SOURCE_CFG = "bambu_mqtt_cfg"
CLUMP_SOURCE_THREE_WAY = "bambu_mqtt_cfg_smart"


def _hex_flag(raw: Any) -> int | None:
    """A hex flag field of the report as an integer, or ``None`` when absent or holding no hex digits."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text[:2].lower() == "0x":
        text = text[2:]
    digits = "".join(c for c in text if c in _HEX_DIGITS)
    return int(digits, 16) if digits else None


def decode_nozzle_clumping_switch(status: dict[str, Any]) -> tuple[str, bool, str | None] | None:
    """``(source, supported, mode)`` from a ``print`` report, or ``None`` when the report states nothing about the setting."""
    cfg = _hex_flag(status.get("cfg"))
    fun = _hex_flag(status.get("fun"))
    fun2 = _hex_flag(status.get("fun2"))
    raw_home = status.get("home_flag")
    home_flag = raw_home if isinstance(raw_home, int) and not isinstance(raw_home, bool) else None

    def on_off(flags: int, bit: int) -> str:
        return "on" if (flags >> bit) & 1 else "off"

    if fun2 is not None and (fun2 >> _CLUMP_FUN2_THREE_WAY_BIT) & 1:
        mode = None if cfg is None else _CLUMP_THREE_WAY_MODES.get((cfg >> _CLUMP_CFG_THREE_WAY_SHIFT) & 0b11)
        return CLUMP_SOURCE_THREE_WAY, True, mode
    if fun is not None:
        if not (fun >> _CLUMP_FUN_SUPPORTED_BIT) & 1:
            return CLUMP_SOURCE_CFG, False, None
        if cfg is not None:
            return CLUMP_SOURCE_CFG, True, on_off(cfg, _CLUMP_CFG_ON_BIT)
        if home_flag is not None:
            return CLUMP_SOURCE_HOME_FLAG, True, on_off(home_flag, _CLUMP_HOME_FLAG_ON_BIT)
        return CLUMP_SOURCE_CFG, True, None
    if home_flag is not None:
        if not (home_flag >> _CLUMP_HOME_FLAG_SUPPORTED_BIT) & 1:
            return CLUMP_SOURCE_HOME_FLAG, False, None
        return CLUMP_SOURCE_HOME_FLAG, True, on_off(home_flag, _CLUMP_HOME_FLAG_ON_BIT)
    return None


def _is_nozzle_clump_error(error_code: int) -> bool:
    """Check if an error code matches a known nozzle clumping HMS code."""
    hex_code = f"{error_code:08X}"
    return any(hex_code.startswith(prefix) for prefix in _NOZZLE_CLUMP_ERROR_PREFIXES)


_FLOW_ANOMALY_ERROR_PREFIXES: tuple[str, ...] = (
    "03008003",   # Filament feeding abnormal (P1/X1) — extruder can't pull
    "03008005",   # Filament broken at extruder
    "03001900",
    "05000B00",
    "05000900",   # Extrusion failure (P1 series) — broad bucket
    "03000900",   # Extruder motor overload / clog / filament stuck (P2S servo);
                  # a closed-loop servo faults here where a stepper skips
)

# Severity mapping for flow-anomaly codes
_FLOW_ANOMALY_SEVERITY: dict[str, str] = {
    "03008003": "medium",
    "03008005": "high",
    "03001900": "medium",
    "05000B00": "high",
    "05000900": "low",
    "03000900": "high",
}


def _classify_flow_anomaly(error_code: int) -> tuple[str, str] | None:
    """Return (event_type, severity) for a flow-anomaly HMS code."""
    if not error_code:
        return None
    hex_code = f"{error_code:08X}"
    for prefix in _FLOW_ANOMALY_ERROR_PREFIXES:
        if hex_code.startswith(prefix):
            event_type = (
                "filament_jam" if prefix in ("03008005", "05000B00", "03000900")
                else "under_extrusion"
            )
            severity = _FLOW_ANOMALY_SEVERITY.get(prefix, "medium")
            return event_type, severity
    return None

_BAMBU_HMS_WIKI_URL = (
    "https://wiki.bambulab.com/en/{model}/troubleshooting/hmscode/{code}"
)
_BAMBU_HMS_INDEX_URL = "https://wiki.bambulab.com/en/hms/home"


_AMS_UNIT_FAMILIES: tuple[str, ...] = ("0700", "1200", "1800")
_AMS_SLOT_FAMILIES: dict[str, str] = {"7": "7000", "2": "2000"}


def _hms_group(hex_digits: str) -> str:
    """``"12008007"`` -> ``"1200_8007"`` (4-hex groups, the screen's form)."""
    h = hex_digits.upper()
    return "_".join(h[i : i + 4] for i in range(0, len(h), 4))


def normalize_bambu_hms(code: str) -> tuple[str, int | None, int | None]:
    """``(canonical 16-hex code, ams_unit_index, slot_index)``."""
    hex_only = "".join(c for c in str(code).upper() if c in "0123456789ABCDEF")
    if len(hex_only) < 8:
        return "", None, None
    hex_only = hex_only[:16].ljust(16, "0")
    g = [hex_only[i : i + 4] for i in range(0, 16, 4)]
    unit: int | None = None
    slot: int | None = None
    base = g[0][:3] + "0"
    if base in _AMS_UNIT_FAMILIES and g[0][3] in "0123":
        unit = int(g[0][3])
        g[0] = base
    slot_base = _AMS_SLOT_FAMILIES.get(g[1][0])
    if slot_base and g[1][1] in "0123" and g[1][2:] == "00":
        slot = int(g[1][1])
        g[1] = slot_base
    return "_".join(g), unit, slot


_BAMBU_HMS_FILAMENT_FAULTS: dict[str, tuple[str, str]] = {
    "0700_7000_0002_0001": (
        "Failed to pull the filament out of the extruder. The extruder may be clogged, or the filament broken inside it.",
        "x1",
    ),
    "0700_7000_0002_0002": (
        "Failed to feed the filament into the toolhead. The filament or the spool may be stuck.",
        "x1",
    ),
    "0700_7000_0002_0003": (
        "Failed to extrude the filament. The extruder or nozzle may be clogged.",
        "x1",
    ),
    "0700_7000_0002_0004": (
        "Failed to pull the filament back from the toolhead to the AMS. The filament or the spool may be stuck.",
        "x1",
    ),
    "0700_7000_0002_0005": (
        "Failed to feed the filament outside the AMS. Clip the end of the filament flat and check whether the spool is stuck.",
        "x1",
    ),
    "0700_7000_0002_0006": (
        "Timed out purging the old filament. The filament may be stuck, or the extruder / nozzle clogged.",
        "x1",
    ),
    "0700_7000_0002_0007": (
        "The AMS filament ran out. Load new filament into the same slot and resume.",
        "x1",
    ),
    "0700_2000_0002_0003": (
        "The filament may be broken inside the AMS.",
        "x1",
    ),
    "0700_2000_0002_0004": (
        "The filament may be broken in the toolhead.",
        "x1",
    ),
    "0700_2000_0002_0005": (
        "The filament ran out and purging the old filament went abnormally. Check whether filament is stuck in the toolhead.",
        "x1",
    ),
    "0700_2000_0002_0009": (
        "Failed to extrude the filament. The extruder may be clogged, or the filament too thin, letting the extruder slip.",
        "p2s",
    ),
    "1200_2000_0002_0003": (
        "The AMS lite filament may be broken in the PTFE tube.",
        "a1-mini",
    ),
    "1200_2000_0002_0004": (
        "The AMS lite filament may be broken in the toolhead.",
        "a1",
    ),
    "1200_2000_0002_0005": (
        "The AMS lite filament ran out and purging the old filament went abnormally. Check whether filament is stuck in the toolhead.",
        "a2l",
    ),
    "1200_2000_0002_0006": (
        "Failed to extrude the AMS lite filament. The extruder may be clogged, or the filament too thin, letting the extruder slip.",
        "a1-mini",
    ),
    "1200_2000_0002_0009": (
        "Failed to extrude the AMS lite filament. The extruder may be clogged, or the filament too thin, letting the extruder slip.",
        "a2l",
    ),
    "1200_8000_0002_0001": (
        "The AMS lite filament may be tangled or stuck.",
        "a1-mini",
    ),
    "0300_0900_0002_0001": (
        "The extrusion motor is overloaded. The extruder may be clogged, or filament stuck in the toolhead.",
        "p2s",
    ),
    "0300_0900_0002_0002": (
        "The extrusion resistance is abnormal. The extruder may be clogged, or filament stuck in the toolhead.",
        "p2s",
    ),
    "0300_0900_0002_0003": (
        "The extruder is extruding abnormally. It may be clogged, or the filament too thin, letting the extruder slip.",
        "p2s",
    ),
    "0300_1A00_0002_0001": (
        "The nozzle is covered with filament, or the build plate is crooked.",
        "a1-mini",
    ),
    "0300_1A00_0002_0002": (
        "The extrusion force sensor detects that the nozzle is clogged.",
        "a1-mini",
    ),
}

_BAMBU_HMS_FAMILIES: dict[str, str] = {
    "0700_7000": "a fault on the AMS filament load / unload path",
    "0700_2000": "an AMS slot / filament fault",
    "1200_2000": "an AMS lite slot / filament fault",
    "1200_8000": "an AMS lite filament feed fault",
    "0300_0900": "an extruder motor fault",
    "0300_1A00": "a nozzle clumping / clog detection fault",
}

# print_error readings

# : Where the reading is, for a code whose public line is only its kind
_PRIVATE_READING_POINTER = (
    "Check the printer's own screen for its message; Kiln's reading of this "
    "code -- what to check and what fixes it -- is free with a Kiln sign-in"
)

_BAMBU_PRINT_ERROR_FAULTS: dict[str, str] = {
    "12008001": (
        f"This is an AMS lite filament-cut fault. {_PRIVATE_READING_POINTER}"
    ),
    "12008007": (
        f"This is an AMS lite filament feed fault. {_PRIVATE_READING_POINTER}"
    ),
    "12008010": (
        f"This is an AMS lite filament feed fault. {_PRIVATE_READING_POINTER}"
    ),
    "03004000": (
        f"This is a Z-axis homing fault. {_PRIVATE_READING_POINTER}"
    ),
    "12008003": "The AMS could not pull the filament back out of the extruder",
    "12008014": "The AMS could not find where the filament is in the path",
    "12008016": "The extruder is not extruding normally",
    "03008003": "Filament feeding is abnormal -- the extruder cannot pull filament",
    "03008005": "The filament broke at the extruder",
    "05000B00": "The AMS reports the filament stuck or jammed",
    "03000900": "The extruder motor is overloaded -- a clog or stuck filament",
    "05000900": "The printer reports an extrusion failure",
    # The nozzle-clumping family of fault codes
    "03008014": (
        "The A1's nozzle-clumping probe tapped the nozzle and thinks it found "
        "a blob; often a false positive on thin or flat first-layer shapes"
    ),
    "03001A00": "The nozzle looks wrapped or clogged with filament",
    "03001800": (
        "The nozzle-clumping probe could not calibrate its baseline, usually "
        "residue on the tip or an obstructed probing spot"
    ),
    "03001900": (
        "The filament may be tangled at the extruder feed -- Kiln could not "
        "confirm this reading against a primary source, so treat it as a hint"
    ),
    "0500C010": (
        "The printer's microSD card failed a read or write, so the file "
        "could not be stored and the print did not start. Reinsert the card, "
        "or replace it if this repeats; nothing heated"
    ),
}

# Prefixes that name a module in BOTH namespaces
_HMS_NAMESPACE_COLLISIONS: dict[str, str] = {
    "03001A00": "HMS 0300-1A00 is nozzle-covered / nozzle-clogged detection",
    "03001800": "HMS 0300-1800 is the extruder eddy current sensor",
    "03000900": "HMS 0300-0900 is the extrusion motor",
    "03001900": (
        "HMS 0300-1900 is the A1 mini's Y-axis eddy current sensor -- "
        "unrelated to filament, and the reading above disagrees with it"
    ),
    "03004000": (
        "HMS 0300-4000 is serial-port / G-code data transmission between "
        "the toolhead and the mainboard -- unrelated to homing"
    ),
}

_BAMBU_PRINT_ERROR_FAMILIES: dict[str, str] = {
    "1200": "an AMS lite filament load / unload fault",
    "0700": "an AMS filament path fault",
    "0300": "a toolhead / extruder fault",
    "0500": "a main-board (0500-family) fault",
}


@dataclass(frozen=True)
class BambuFaultReading:
    """One fault code, read once, for every door that shows it."""

    reading: str
    remedy: str | None = None
    url: str | None = None
    decoded: dict[str, Any] | None = None
    safety_alert: dict[str, Any] | None = None
    note: str | None = None

    @property
    def private(self) -> bool:
        """Did kiln-pro supply this reading?"""
        return self.decoded is not None


def read_bambu_fault(code: str, *, kind: str = "hms", wait: bool = True) -> BambuFaultReading:
    """Read a Bambu fault code, once, for every door that shows it."""
    from kiln import _pro_fault_bridge

    decoded = _pro_fault_bridge.decode_fault(code, kind=kind, wait=wait)
    alert = _pro_fault_bridge.safety_alert(code, kind=kind, wait=wait)
    public_reading, url = describe_bambu_filament_fault_public(code, kind=kind)
    if decoded is None:
        return BambuFaultReading(
            reading=public_reading, url=url, safety_alert=alert,
            note=_pro_fault_bridge.note_for(code, kind=kind),
        )
    reading = str(decoded["cause"]).strip()
    fix = decoded.get("fix")
    remedy = fix.strip() if isinstance(fix, str) and fix.strip() else None
    return BambuFaultReading(reading=reading, remedy=remedy, url=url, decoded=decoded, safety_alert=alert)


def describe_bambu_filament_fault(
    code: str, *, kind: str = "hms"
) -> tuple[str, str | None]:
    """``(plain-language reading, wiki URL or None)`` for a Bambu fault."""
    fault = read_bambu_fault(code, kind=kind)
    return fault.reading, fault.url


def describe_bambu_filament_fault_public(
    code: str, *, kind: str = "hms"
) -> tuple[str, str | None]:
    """``(reading, wiki URL or None)`` from public Kiln's own tables only."""
    if kind == "print_error":
        hex_only = "".join(c for c in str(code).upper() if c in "0123456789ABCDEF")
        prefix = hex_only[:8]
        reading = _BAMBU_PRINT_ERROR_FAULTS.get(prefix)
        if reading:
            return f"{reading}.", None
        # These two say what the code MEANS and stop
        family = _BAMBU_PRINT_ERROR_FAMILIES.get(prefix[:4])
        if family:
            return (
                f"This is {family}, and Bambu publishes no page for it.",
                None,
            )
        return (
            "Kiln has no reading for this code, and Bambu publishes none "
            "either.",
            None,
        )

    canonical, unit, slot = normalize_bambu_hms(code)
    if not canonical:
        return f"The printer reported {code}, which is not a readable HMS code.", None
    where = ""
    if unit is not None and slot is not None:
        with contextlib.suppress(ValueError):
            where = f" (slot {_bambu_tray_name(unit, slot)})"
    elif unit is not None:
        with contextlib.suppress(ValueError):
            where = f" (AMS {_bambu_unit_name(unit)})"
    entry = _BAMBU_HMS_FILAMENT_FAULTS.get(canonical)
    if entry is not None:
        reading, model = entry
        return f"{reading}{where}", _BAMBU_HMS_WIKI_URL.format(
            model=model, code=canonical
        )
    shown = _hms_group(canonical.replace("_", ""))
    family = _BAMBU_HMS_FAMILIES.get(canonical[:9])
    if family:
        return (
            f"The printer reported {shown}{where}, {family}.",
            _BAMBU_HMS_INDEX_URL,
        )
    return (
        f"The printer reported {shown}{where}, a fault Kiln has no reading for.",
        _BAMBU_HMS_INDEX_URL,
    )


def format_bambu_hms_code(attr: int, code: int) -> str:
    """An ``hms`` array entry in the four-group form the vendor files it under."""
    return _hms_group(f"{attr:08X}{code:08X}").replace("_", "-")


def compose_bambu_faults(
    status: dict[str, Any], *, device_type: str = ""
) -> list[dict[str, Any]]:
    """Every fault *status* carries, each as its own screen would show it."""
    faults: list[dict[str, Any]] = []
    seen: set[str] = set()

    raw_error = status.get("print_error")
    error_int: int | None = None
    if raw_error is not None:
        with contextlib.suppress(TypeError, ValueError):
            error_int = int(raw_error)
    pretty = format_error_code(error_int)
    if pretty is not None and error_int is not None:
        entry: dict[str, Any] = {
            "code": pretty,
            "kind": "print_error",
            "raw": {"print_error": error_int},
        }
        found = lookup_screen_text(pretty, device_type=device_type, kind="print_error")
        if found is not None:
            entry["screen_text"], entry["source"] = found
        _attach_reading(entry)
        faults.append(entry)
        seen.add(pretty)

    hms = status.get("hms")
    if isinstance(hms, list):
        for item in hms:
            if not isinstance(item, dict):
                continue
            try:
                attr = int(item.get("attr") or 0)
                code = int(item.get("code") or 0)
            except (TypeError, ValueError):
                continue
            if attr < 0 or code < 0 or not (attr or code):
                continue
            shown = format_bambu_hms_code(attr, code)
            if shown in seen:
                continue
            seen.add(shown)
            entry = {
                "code": shown,
                "kind": "hms",
                "raw": {"attr": attr, "code": code},
            }
            found = lookup_screen_text(shown, device_type=device_type, kind="hms")
            if found is not None:
                entry["screen_text"], entry["source"] = found
            _attach_reading(entry)
            faults.append(entry)
    return faults


def _attach_reading(entry: dict[str, Any]) -> None:
    """Put Kiln's reading of *entry*'s code on the entry, in the entry's own namespace."""
    fault = read_bambu_fault(entry["code"], kind=entry["kind"])
    entry["reading"] = fault.reading
    if fault.remedy:
        entry["remedy"] = fault.remedy
    if fault.safety_alert:
        entry["safety"] = fault.safety_alert["means"]


# Bambu LED node names.
_VALID_LED_NODES: frozenset[str] = frozenset({"chamber_light", "work_light"})
_VALID_LED_MODES: frozenset[str] = frozenset({"on", "off", "flashing"})

_FAN_NODE_TO_INDEX: dict[str, int] = {
    "part": 1,
    "part_cooling": 1,
    "cooling": 1,
    "aux": 2,
    "auxiliary": 2,
    "chamber": 3,
}

_FAN_INDEX_TO_FIELD: dict[int, str] = {
    1: "cooling_fan_speed",
    2: "big_fan1_speed",
    3: "big_fan2_speed",
}

_COMMAND_CONFIRM_WINDOW_S: float = 3.0

_FAN_CONFIRM_WINDOW_S: float = 10.0


@dataclass(frozen=True)
class _SendStamp:
    """When a command went out, and what its field said just before it."""

    at: float
    prior: Any = None
    prior_seen: float = 0.0

_BAMBU_FTP_URL_MODELS: frozenset[str] = frozenset({"bambu_p2s", "p2s"})

# The directory that form points into
_BAMBU_FTP_URL_DIR = "cache"

_BAMBU_MODEL_FAMILIES: dict[str, str] = {
    "BBL-A1M": "a1_mini",
    "BBL-A1": "a1",
    "BL-A001": "a1",
    "BL-P001": "x1c",
    "BL-P002": "x1c",
    "BBL-X1C": "x1c",
    "BBL-X1E": "x1e",
    "BBL-P1S": "p1s",
    "BBL-P1P": "p1p",
    "C11": "p1p",
    "C12": "p1s",
    "C13": "x1e",
    "N1": "a1_mini",
    "N2S": "a1",
    "N6": "x2d",
    "N9": "a2l",
    "N7": "p2s",
    "N10": "h3c",
    "O1C2": "h2c",
    "O1D": "h2d",
    "O1E": "h2d_pro",
    "O1S": "h2s",
    "Bambu Lab A1 mini": "a1_mini",
    "Bambu Lab A1": "a1",
    "Bambu Lab A2L": "a2l",
    "Bambu Lab X1 Carbon": "x1c",
    "Bambu Lab X1E": "x1e",
    "Bambu Lab X2D": "x2d",
    "Bambu Lab P1S": "p1s",
    "Bambu Lab P2S": "p2s",
    "Bambu Lab H2C": "h2c",
    "Bambu Lab H3C": "h3c",
    "Bambu Lab H2D": "h2d",
    "Bambu Lab H2D Pro": "h2d_pro",
    "Bambu Lab H2S": "h2s",
    "Bambu Lab P1P": "p1p",
    "030": "a1_mini",
    "039": "a1",
    "00M": "x1c",
    "03W": "x1e",
    "20P": "x2d",
    "01P": "p1s",
    "01S": "p1p",
    "22E": "p2s",
    "26A": "a2l",
    "31B": "h2c",
    "32X": "h3c",
    "094": "h2d",
    "239": "h2d_pro",
    "093": "h2s",
}


_BAMBU_ACCESSORY_MODULE_HEADS: frozenset[str] = frozenset(
    {"ams_f1", "n3f", "n3s", "ams"}
)


def _merge_push_status(cache: dict[str, Any], delta: dict[str, Any]) -> None:
    """Fold a ``push_status`` frame into the cache, section by section."""
    for key, value in delta.items():
        current = cache.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            _merge_push_status(current, value)
        else:
            cache[key] = value


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _feeding_ref(status: dict[str, Any]) -> tuple[_TrayRef | None, str]:
    """``(what feeds the nozzle, which field said so)`` from a raw status."""
    device = status.get("device")
    extruder = device.get("extruder") if isinstance(device, dict) else None
    if isinstance(extruder, dict) and isinstance(extruder.get("info"), list):
        info = [entry for entry in extruder["info"] if isinstance(entry, dict)]
        current = 0
        state = _int_or_none(extruder.get("state"))
        if state is not None:
            current = (state >> 4) & 0xF
        chosen = next((e for e in info if _int_or_none(e.get("id")) == current), info[0] if info else None)
        if chosen is not None and chosen.get("snow") is not None:
            ref = _read_extruder_slot(chosen.get("snow"))
            if ref is not None:
                return ref, "extruder"
    ams = status.get("ams")
    if isinstance(ams, dict) and ams.get("tray_now") is not None:
        return _read_tray_id(ams.get("tray_now")), "tray_now"
    if status.get("tray_now") is not None:
        return _read_tray_id(status.get("tray_now")), "tray_now"
    return None, ""


def _tray_now_of(status: dict[str, Any]) -> str | None:
    """The tray feeding the nozzle as the cache holds it, as the printer's own tray id, or ``None`` when the AMS section has never said."""
    ref, source = _feeding_ref(status)
    if ref is not None:
        return str(ref.tray_id)
    if source == "tray_now":
        ams = status.get("ams")
        value = ams.get("tray_now") if isinstance(ams, dict) and ams.get("tray_now") is not None else status.get("tray_now")
        return None if value is None else str(value)
    return None


def _raw_ams_trays(ams_data: Any) -> list[tuple[int, int, int, str, dict[str, Any]]]:
    """``(unit, slot, tray_id, name, tray)`` for every tray in the raw ``ams`` array."""
    out: list[tuple[int, int, int, str, dict[str, Any]]] = []
    if not isinstance(ams_data, list):
        return out
    for position, unit in enumerate(ams_data):
        if not isinstance(unit, dict):
            continue
        raw_trays = unit.get("tray")
        if not isinstance(raw_trays, list):
            continue
        try:
            unit_id = int(unit.get("id", position))
        except (TypeError, ValueError):
            continue
        for tray in raw_trays:
            if not isinstance(tray, dict):
                continue
            try:
                slot = int(tray.get("id", 0))
                out.append((unit_id, slot, _bambu_tray_id(unit_id, slot), _bambu_tray_name(unit_id, slot), tray))
            except (TypeError, ValueError):
                continue
    return out


def _is_accessory_module(name: str) -> bool:
    """True when a firmware module belongs to an AMS unit, not the printer."""
    head, sep, idx = str(name).partition("/")
    return bool(sep) and idx.isdigit() and head in _BAMBU_ACCESSORY_MODULE_HEADS


def _normalize_fingerprint(value: str) -> str:
    """Normalize a SHA-256 fingerprint string to lowercase hex."""
    return "".join(ch for ch in value.lower() if ch in "0123456789abcdef")


def _find_ffmpeg() -> str | None:
    """Find ffmpeg binary on PATH or common install locations."""
    return find_ffmpeg()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

#: The printer-side port for the TLS + JPEG camera feed (A1 / A2 / P1 series).
_CAMERA_PORT = 6000

#: The printer-side port for the RTSPS camera stream (X1 / X2 / H2 / P2 series).
_RTSPS_PORT = 322

_CAMERA_FRAME_HEADER = struct.Struct("<IIII")

_CAMERA_AUTH_HEAD = struct.pack("<IIII", 0x40, 0x3000, 0, 0)

_CAMERA_MAX_FRAME = 10 * 1024 * 1024

_CAMERA_IDLE_SECONDS = 30.0

#: Models whose camera is RTSPS, used when the printer has not said which.
_RTSPS_FAMILIES: frozenset[str] = frozenset(
    {"x1c", "x1e", "x2d", "p2s", "h2c", "h2d", "h2d_pro", "h2s", "h3c"}
)

#: Models whose camera is the port-6000 JPEG feed.
_PORT6000_FAMILIES: frozenset[str] = frozenset({"a1", "a1_mini", "a2l", "p1p", "p1s"})

_LIVEVIEW_OFF = (
    "This printer reports that LAN Only Liveview is off, so its camera cannot "
    "be read over the network. Turn on LAN Only Liveview on the printer's "
    "screen, then try again."
)

_RTSPS_NEEDS_FFMPEG = (
    "This printer's camera streams over RTSPS (port 322), and cutting a frame "
    "from it needs ffmpeg, which is not installed here. Install ffmpeg, then "
    "try again."
)


def _announced_camera_transport(ipcam: Any) -> str:
    """``"rtsps"``, ``"port6000"``, ``"off"`` or ``""`` from a status report's ``ipcam`` block."""
    if not isinstance(ipcam, dict):
        return ""
    url = ipcam.get("rtsp_url")
    if isinstance(url, str) and url:
        if url == "disable":
            return "off"
        return "rtsps" if url.lower().startswith("rtsp") else ""
    liveview = ipcam.get("liveview")
    local = liveview.get("local") if isinstance(liveview, dict) else None
    return {"local": "port6000", "rtsps": "rtsps", "rtsp": "rtsps", "disabled": "off"}.get(
        str(local or ""), ""
    )


def _camera_auth_packet(access_code: str) -> bytes:
    """The 80-byte packet the camera port expects after the TLS handshake."""
    return (
        _CAMERA_AUTH_HEAD
        + _MQTT_USERNAME.encode("ascii").ljust(32, b"\x00")
        + access_code.encode("ascii").ljust(32, b"\x00")
    )


def _read_port6000_frames(
    ssock: ssl.SSLSocket,
    stop: threading.Event,
    *,
    first_frame_timeout: float,
    idle_timeout: float = _CAMERA_IDLE_SECONDS,
) -> Iterator[bytes]:
    """Yield whole JPEGs from an authenticated camera socket."""
    buf = bytearray()
    payload_size: int | None = None
    frames = 0
    ssock.settimeout(first_frame_timeout)
    while not stop.is_set():
        try:
            chunk = ssock.recv(16384)
        except TimeoutError:
            if frames == 0:
                raise CameraStreamError(
                    "The printer accepted the camera connection but sent no "
                    f"video within {first_frame_timeout:g}s. On models with a "
                    "LAN-mode liveview switch, check it is on.",
                    code="CAMERA_UNREACHABLE",
                ) from None
            raise CameraStreamError(
                f"No camera frame for {idle_timeout:g}s; reconnecting.",
                code="CAMERA_UNREACHABLE",
            ) from None
        if not chunk:
            if frames == 0:
                raise CameraStreamError(
                    "The printer closed the camera connection without sending "
                    "a frame. Check the LAN access code; on models with a "
                    "LAN-mode liveview switch, check it is on (the video "
                    "toggle on an A1's screen does not gate this feed).",
                    code="CAMERA_REFUSED",
                )
            return
        buf += chunk
        while True:
            if payload_size is None:
                if len(buf) < _CAMERA_FRAME_HEADER.size:
                    break
                size, _itrack, _flags, _zero = _CAMERA_FRAME_HEADER.unpack_from(buf)
                del buf[: _CAMERA_FRAME_HEADER.size]
                if not 0 < size <= _CAMERA_MAX_FRAME:
                    raise CameraStreamError(
                        f"The camera port sent a frame header of {size} bytes, "
                        "which is not a picture; the connection is not the "
                        "camera feed Kiln expects.",
                        code="CAMERA_UNREACHABLE",
                    )
                payload_size = size
            if len(buf) < payload_size:
                break
            frame = bytes(buf[:payload_size])
            del buf[:payload_size]
            payload_size = None
            if picture_format(frame) is None:
                if frames == 0:
                    raise CameraStreamError(
                        f"The camera port answered with {len(frame)} bytes that are not a "
                        "picture, so it is not this printer's camera feed; some models "
                        "stream over RTSPS instead.",
                        code=NOT_A_PICTURE,
                    )
                # A feed already proven by a picture drops a stray frame, not its viewers.
                continue
            frames += 1
            if frames == 1:
                ssock.settimeout(idle_timeout)
            yield frame


def _connect_camera_tls(
    host: str, port: int, ctx: ssl.SSLContext, *, timeout: float, name_address: bool = True
) -> ssl.SSLSocket:
    """Open the camera port and complete its TLS handshake, nothing more."""
    shown = f" ({host}:{port})" if name_address else ""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise CameraStreamError(
            f"Nothing answered on the camera port{shown}: "
            "the printer may be off, a LAN-mode liveview switch may be off "
            "on models that have one, or this model streams over RTSPS "
            "instead. "
            f"({exc.__class__.__name__})",
            code="CAMERA_UNREACHABLE",
        ) from exc
    try:
        return ctx.wrap_socket(sock, server_hostname=host)
    except (ssl.SSLError, OSError) as exc:
        sock.close()
        raise CameraStreamError(
            f"The camera port{shown} answered but did "
            "not complete a TLS handshake: a LAN-mode liveview switch may "
            "be off on models that have one, or this model streams over "
            f"RTSPS instead. ({exc.__class__.__name__})",
            code="CAMERA_UNREACHABLE",
        ) from exc


class BambuPort6000Source:
    """The relay's frame source for an A1 / P1 camera: ONE authenticated connection, every frame the printer pushes, until stopped."""

    kind = "bambu_port6000"

    def __init__(
        self,
        host: str,
        auth_packet: bytes,
        tls_context: ssl.SSLContext,
        *,
        port: int = _CAMERA_PORT,
        timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._auth = auth_packet
        self._ctx = tls_context
        self._timeout = timeout
        self.label = f"bambu-port6000://{host}:{port}"

    def frames(self, stop: threading.Event) -> Iterator[bytes]:
        ssock = _connect_camera_tls(self._host, self._port, self._ctx, timeout=self._timeout)
        try:
            ssock.sendall(self._auth)
            yield from _read_port6000_frames(ssock, stop, first_frame_timeout=self._timeout)
        except (TimeoutError, OSError) as exc:
            raise CameraStreamError(
                f"The camera connection dropped: {exc.__class__.__name__}.",
                code="CAMERA_UNREACHABLE",
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                ssock.close()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS subclass for implicit TLS (port 990)."""

    def connect(
        self,
        host: str = "",
        port: int = 0,
        timeout: float = -999,
        source_address: Any = None,
    ) -> str:
        """Connect and immediately wrap socket in TLS."""
        if host:
            self.host = host
        if port:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address

        self.sock = socket.create_connection(
            (self.host, self.port),
            self.timeout,
            source_address=self.source_address,
        )
        self.af = self.sock.family
        # Wrap in TLS immediately (implicit mode).
        self.sock = self.context.wrap_socket(
            self.sock,
            server_hostname=self.host,
        )
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd: str, rest: Any = None) -> Any:
        """Override to handle passive mode data connections with TLS wrapping."""
        import re as _re

        size = None
        if self.passiveserver:
            host, port = self.makepasv()
            conn = socket.create_connection(
                (host, port), self.timeout, self.source_address
            )
            try:
                if self._prot_p:  # type: ignore[attr-defined]
                    conn = self.context.wrap_socket(
                        conn,
                        server_hostname=self.host,
                        session=self.sock.session,  # type: ignore[union-attr]
                    )
            except Exception:
                conn.close()
                raise
            if rest is not None:
                self.sendcmd(f"REST {rest}")
            resp = self.sendcmd(cmd)
            if resp[0] == "2":
                resp = self.getresp()
            if resp[0] != "1":
                raise ftplib.error_reply(resp)
        else:
            raise ftplib.error_reply("Active mode not supported for Bambu FTPS")
        if resp[:3] == "150":
            m = _re.search(r"\((\d+) bytes\)", resp)
            if m:
                size = int(m.group(1))
        return conn, size

    def storbinary(
        self,
        cmd: str,
        fp: Any,
        blocksize: int = 8192,
        callback: Any = None,
        rest: Any = None,
    ) -> str:
        """Override to handle ``conn.unwrap()`` timeout on Bambu printers."""
        self.voidcmd("TYPE I")
        conn, _ = self.ntransfercmd(cmd, rest)
        try:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
        finally:
            try:
                if hasattr(conn, "unwrap"):
                    conn.unwrap()
            except (TimeoutError, OSError, AttributeError):
                pass
            conn.close()
        return self.voidresp()

    def retrbinary(
        self,
        cmd: str,
        callback: Any,
        blocksize: int = 8192,
        rest: Any = None,
    ) -> str:
        """Override to handle ``conn.unwrap()`` timeout on Bambu printers."""
        self.voidcmd("TYPE I")
        conn, _ = self.ntransfercmd(cmd, rest)
        try:
            while True:
                data = conn.recv(blocksize)
                if not data:
                    break
                callback(data)
        finally:
            try:
                if hasattr(conn, "unwrap"):
                    conn.unwrap()
            except (TimeoutError, OSError, AttributeError):
                pass
            conn.close()
        return self.voidresp()


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


def _first_real_job_id(*candidates: Any) -> str | None:
    """First candidate that is an actual id, or None if they are all placeholders."""
    from kiln.printers.job_identity import clean_native_id

    for candidate in candidates:
        cleaned = clean_native_id(candidate)
        if cleaned is not None:
            return cleaned
    return None


class BambuAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by Bambu Lab MQTT + FTPS."""

    _DURATION_SEMANTICS: ClassVar[str] = "stopwatch"

    @property
    def camera_fitted_at_factory(self) -> bool | None:
        """Whether this machine left the factory with a camera of its own."""
        prefix = str(getattr(self, "_serial", "") or "")[:3].upper()
        if _BAMBU_MODEL_FAMILIES.get(prefix) is not None:
            return True
        declared = str(getattr(self, "_printer_model", "") or "").lower()
        if not declared or ("x1" in declared and "x1e" not in declared):
            return None
        return True

    def camera_catalogue_model(self) -> str:
        """The declared model -- except for a machine that could be the X1 (see :attr:`camera_fitted_at_factory`)."""
        if self.camera_fitted_at_factory is None:
            return ""
        return self.declared_printer_model()

    _IDLE_RELEASE_ENV = _IDLE_DISCONNECT_ENV
    _IDLE_RELEASE_DEFAULT_S = _DEFAULT_IDLE_DISCONNECT_S

    #: How Kiln learns this printer's own pause and cancel moves.
    head_position = ("a Bambu printer reports no head position over MQTT, so its owner teaches Kiln "
                     "its moves through the guided session's questions")

    def __init__(
        self,
        host: str,
        access_code: str,
        serial: str,
        timeout: int = 10,
        tls_mode: str | None = None,
        tls_fingerprint: str | None = None,
        printer_model: str | None = None,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")
        if not access_code:
            raise ValueError("access_code must not be empty")
        if not serial:
            raise ValueError("serial must not be empty")

        self._host = host
        self._access_code = access_code
        self._serial = serial
        self._printer_model = (printer_model or "").strip().lower()
        self._timeout = timeout
        configured_tls_mode = (tls_mode or os.environ.get(_TLS_MODE_ENV, _DEFAULT_TLS_MODE)).strip().lower()
        if configured_tls_mode not in _VALID_TLS_MODES:
            raise ValueError(f"tls_mode must be one of {sorted(_VALID_TLS_MODES)}, got {configured_tls_mode!r}")
        self._tls_mode = configured_tls_mode
        configured_fp = tls_fingerprint or os.environ.get(_TLS_FINGERPRINT_ENV, "")
        self._tls_fingerprint = _normalize_fingerprint(configured_fp)
        if configured_fp and not self._tls_fingerprint:
            raise ValueError(f"tls_fingerprint must be a SHA-256 fingerprint (64 hex chars), got {configured_fp!r}")
        if self._tls_fingerprint and len(self._tls_fingerprint) != 64:
            raise ValueError(f"tls_fingerprint must be a SHA-256 fingerprint (64 hex chars), got {configured_fp!r}")
        self._pin_store_path = os.environ.get(_TLS_PIN_FILE_ENV, _DEFAULT_BAMBU_PIN_FILE)

        self._topic_report = f"device/{serial}/report"
        self._topic_request = f"device/{serial}/request"
        self._fw_modules: list[Any] = []
        self._restore_nozzle_detection: dict[str, Any] | None = None
        self._connack_refusal: tuple[int, str] | None = None
        self._fw_modules_requested = False  # one get_version request per session

        self._state_lock = threading.Lock()
        self._last_status: dict[str, Any] = {}
        self._last_state_time: float = 0.0  # monotonic time of last accepted update
        # Monotonic time each status key was last CARRIED by an accepted push
        self._field_seen_at: dict[str, float] = {}
        self._confirm_window_s: float | None = None
        self._gcode_state_time: float = 0.0
        self._stop_sent_at: float = 0.0
        self._cadence = TelemetryCadence()
        self._last_forced_refresh: float = 0.0
        self._connected = False
        self._sequence_id = 0
        self._pending_outcomes_reconciled = False
        self._reconcile_lock = threading.Lock()

        self._mqtt_client: mqtt.Client | None = None
        self._mqtt_connected = threading.Event()
        self._connect_lock = threading.Lock()

        # Idle-release bookkeeping (PrinterAdapter)
        self._init_idle_release()

        # Exponential backoff for reconnection attempts.
        self._backoff = _BackoffState()
        self._pin_lock = threading.Lock()

        self._last_storage_path: str | None = None

    @staticmethod
    def _host_key(host: str) -> str:
        """Return canonical key for pin-store lookups."""
        return host.strip().lower()

    def _build_tls_context(self) -> ssl.SSLContext:
        """Build SSL context according to configured TLS mode."""
        ctx = ssl.create_default_context()
        # Floor the protocol at TLS 1.2
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if self._tls_mode == _TLS_MODE_CA:
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
        else:
            # Pin mode verifies identity via fingerprint; insecure disables checks.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _load_pins(self) -> dict[str, str]:
        """Load persisted host->fingerprint pins from disk."""
        path = self._pin_store_path
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception as exc:
            logger.warning("Failed to read Bambu TLS pin store %s: %s", path, exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        pins: dict[str, str] = {}
        for host, fp in raw.items():
            host_key = self._host_key(str(host))
            normalized = _normalize_fingerprint(str(fp))
            if len(normalized) == 64:
                pins[host_key] = normalized
        return pins

    def _save_pins(self, pins: dict[str, str]) -> None:
        """Persist host->fingerprint pins to disk with restrictive perms."""
        path = self._pin_store_path
        pin_dir = os.path.dirname(path)
        if pin_dir:
            os.makedirs(pin_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(pins, fh, indent=2, sort_keys=True)
        if sys.platform != "win32":
            try:
                if pin_dir:
                    os.chmod(pin_dir, 0o700)
            except OSError:
                pass
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)

    @staticmethod
    def _extract_socket_cert(sock_obj: Any) -> bytes | None:
        """Return peer certificate in DER format from an SSL socket-like object."""
        if sock_obj is None or not hasattr(sock_obj, "getpeercert"):
            return None
        try:
            cert = sock_obj.getpeercert(binary_form=True)
            if isinstance(cert, (bytes, bytearray)):
                return bytes(cert)
        except Exception:
            return None
        return None

    def _enforce_pin_policy(self, actual_fp: str, *, transport: str) -> None:
        """Validate certificate fingerprint against explicit or TOFU pin."""
        if self._tls_fingerprint:
            if not hmac.compare_digest(actual_fp, self._tls_fingerprint):
                raise PrinterError(
                    f"{transport} TLS fingerprint mismatch for {self._host}. "
                    "Set KILN_BAMBU_TLS_FINGERPRINT to the correct value or "
                    "temporarily set KILN_BAMBU_TLS_MODE=insecure to bypass."
                )
            return

        if self._tls_mode != _TLS_MODE_PIN:
            return

        host_key = self._host_key(self._host)
        with self._pin_lock:
            pins = self._load_pins()
            expected = pins.get(host_key)
            if expected:
                if not hmac.compare_digest(actual_fp, expected):
                    raise PrinterError(
                        f"{transport} TLS pin mismatch for {self._host}. "
                        "The presented certificate changed from the pinned value. "
                        "If this is expected, remove or update the pin in "
                        f"{self._pin_store_path}."
                    )
                return

            pins[host_key] = actual_fp
            self._save_pins(pins)
            logger.debug(
                "Pinned Bambu TLS certificate for %s (SHA256=%s..., mode=pin).",
                self._host,
                actual_fp[:12],
            )

    def _validate_peer_certificate(self, cert_bytes: bytes | None, *, transport: str) -> None:
        """Validate peer certificate according to TLS mode and pin policy."""
        if self._tls_mode == _TLS_MODE_INSECURE:
            return
        if not cert_bytes:
            raise PrinterError(
                f"{transport} TLS handshake for {self._host} did not expose a peer certificate. "
                "This can happen if a firewall or proxy is intercepting TLS traffic.\n"
                "  1) Check that no network proxy is between Kiln and the printer\n"
                "  2) Try setting KILN_BAMBU_TLS_MODE=insecure temporarily to confirm\n"
                "Retry with `get_state()`."
            )
        actual_fp = hashlib.sha256(cert_bytes).hexdigest()
        self._enforce_pin_policy(actual_fp, transport=transport)

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return "bambu"

    @property
    def capabilities(self) -> PrinterCapabilities:
        """Capabilities supported by the Bambu backend."""
        return PrinterCapabilities(
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
            can_pause=True,
            can_clear_error=True,
            can_report_multi_material=True,
            cancel_during_calibration_faults=True,
            can_handle_filament=True,
            can_snapshot=True,
            can_stream=True,
            supported_extensions=(".3mf", ".gcode", ".gco"),
        )

    def _mqtt_reported_family(self) -> tuple[str | None, str]:
        """``(family, raw_product_name)`` from the cached ``get_version`` module list, skipping accessories."""
        with self._state_lock:
            modules = list(self._fw_modules)
        for mod in modules:
            if not isinstance(mod, dict):
                continue
            if _is_accessory_module(mod.get("name", "")):
                continue
            product_name = str(mod.get("product_name") or "").strip()
            if not product_name:
                continue
            family = _BAMBU_MODEL_FAMILIES.get(product_name)
            if family:
                return family, product_name
        return None, ""

    def _chamber_lookup_model(self) -> str | None:
        """The catalogue id the chamber-sensor fact is looked up by."""
        declared = getattr(self, "_printer_model", "")
        if declared:
            return declared
        family = _BAMBU_MODEL_FAMILIES.get(getattr(self, "_serial", "")[:3].upper())
        return f"bambu_{family}" if family else None

    def _identity_families(self) -> tuple[str | None, str | None, str]:
        """``(serial_family, mqtt_family, product_name)``."""
        return (
            _BAMBU_MODEL_FAMILIES.get(self._serial[:3].upper()),
            *self._mqtt_reported_family(),
        )

    def get_identity_channels(self) -> dict[str, str]:
        """The serial-prefix and firmware channels, each with its claim."""
        serial_family, mqtt_family, _ = self._identity_families()
        channels: dict[str, str] = {}
        if serial_family:
            channels["serial_prefix"] = f"bambu_{serial_family}"
        if mqtt_family:
            channels["firmware_product_name"] = f"bambu_{mqtt_family}"
        return channels

    def get_printer_info(self) -> PrinterInfo | None:
        """The printer's self-reported model, for telemetry and display."""
        serial_family, mqtt_family, product_name = self._identity_families()
        if serial_family and mqtt_family and serial_family != mqtt_family:
            logger.warning(
                "Bambu identity channels disagree: serial prefix %r says %r, "
                "firmware product_name %r says %r. Reporting no model — settle "
                "it with `%s` or the `set_printer_model` tool.",
                self._serial[:3],
                serial_family,
                product_name,
                mqtt_family,
                kiln_command("set-model"),
            )
            return None
        if serial_family:
            return PrinterInfo(
                model=f"bambu_{serial_family}",
                raw_model=self._serial[:3].upper(),
                source="serial_prefix",
            )
        if mqtt_family:
            return PrinterInfo(
                model=f"bambu_{mqtt_family}", raw_model=product_name, source="mqtt"
            )
        return None

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    def _safe_stop_client(self, client: mqtt.Client) -> None:
        """Stop an MQTT client with a timeout to prevent hangs."""
        try:
            client.disconnect()
        except Exception:
            logger.debug("MQTT client disconnect call failed", exc_info=True)
        def _quiet_loop_stop() -> None:
            try:
                client.loop_stop()
            except Exception:
                logger.debug("MQTT loop_stop raised in stopper thread", exc_info=True)

        try:
            stopper = threading.Thread(target=_quiet_loop_stop, daemon=True)
            stopper.start()
            stopper.join(timeout=self._timeout)
            if stopper.is_alive():
                logger.debug(
                    "MQTT loop_stop did not complete within %ss — abandoning",
                    self._timeout,
                )
        except Exception:
            logger.debug("MQTT loop_stop wrapper failed", exc_info=True)

    def _next_seq(self) -> str:
        """Return the next sequence ID as a string."""
        with self._state_lock:
            self._sequence_id += 1
            return str(self._sequence_id)

    def _other_clients_hint(self) -> str:
        """Name the likely holder of the printer's LAN slots, measured."""
        try:
            from kiln.serve_siblings import (
                check_serve_siblings,
                printer_connection_holders,
            )

            held = printer_connection_holders(self._host)
            if held["supported"] and held["kiln_count"] >= 1:
                n = held["kiln_count"]
                return (
                    f"\n  Measured just now: {n} other "
                    f"{'copy' if n == 1 else 'copies'} of Kiln's own server "
                    f"{'is' if n == 1 else 'are'} holding a connection to "
                    f"{self._host}. That is the cause, and it is not a printer "
                    f"fault. Close the leftovers with `{kiln_command('trim')}` (or ask Kiln "
                    f"to trim them) and retry — power-cycling will not help."
                )
            if held["supported"]:
                return ""
            count = check_serve_siblings().get("count")
        except Exception:
            return ""
        if not isinstance(count, int) or count < 2:
            return ""
        return (
            f"\n  Kiln itself has {count} servers running on this machine, "
            f"each able to hold one of those slots. That is the most likely "
            f"cause, and it is not a printer fault. Close the leftovers with "
            f"`{kiln_command('trim')}` (or ask Kiln to trim them) and retry."
        )

    def _connection_is_live(self) -> bool:
        """True while the MQTT client is connected (idle-reaper hook)."""
        return self._mqtt_client is not None and self._mqtt_connected.is_set()

    def _print_in_flight(self) -> bool:
        """True while the printer is mid-job, as of the last status seen."""
        with self._state_lock:
            state = str(self._last_status.get("gcode_state", "")).lower().strip()
        return state in _PRINT_ACTIVE_STATES

    def _ensure_mqtt(self) -> mqtt.Client:
        """Ensure the MQTT client is connected, creating it if needed."""
        self._note_activity()
        if self._mqtt_client is not None and self._mqtt_connected.is_set():
            return self._mqtt_client

        with self._connect_lock:
            # Double-check inside the lock to avoid duplicate connections.
            if self._mqtt_client is not None and self._mqtt_connected.is_set():
                return self._mqtt_client

            # Respect backoff cooldown — don't spam reconnection attempts.
            if self._backoff.in_cooldown():
                raise PrinterError(
                    f"MQTT reconnection to {self._host} is in backoff cooldown "
                    f"(attempt #{self._backoff.attempt_count}, "
                    f"retry in {self._backoff.next_retry_time - time.monotonic():.1f}s)"
                )

            # Tear down stale client that lost its connection.
            if self._mqtt_client is not None:
                logger.debug("MQTT client exists but disconnected; tearing down stale client")
                self._safe_stop_client(self._mqtt_client)
                self._mqtt_client = None

            try:
                client = mqtt.Client(
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"kiln-{self._serial[:8]}-{os.getpid()}",
                    protocol=mqtt.MQTTv311,
                )
                client.username_pw_set(_MQTT_USERNAME, self._access_code)

                tls_context = self._build_tls_context()
                client.tls_set_context(tls_context)

                client.on_connect = self._on_connect
                client.on_message = self._on_message
                client.on_disconnect = self._on_disconnect

                self._mqtt_connected.clear()
                self._connack_refusal = None
                client.connect_async(self._host, _MQTT_PORT, keepalive=60)
                client.loop_start()

                # Wait for the printer's answer: connected, refused, or none.
                if not self._await_connack(self._timeout):
                    refusal = getattr(self, "_connack_refusal", None)
                    self._safe_stop_client(client)
                    self._backoff.record_failure()
                    if refusal is not None:
                        raise PrinterError(self._refusal_message(*refusal))
                    # No answer to the login at all
                    raise PrinterError(
                        f"Couldn't reach the printer at {self._host} — "
                        f"no response within {self._timeout}s.\n"
                        "  Most likely something else is already connected: "
                        "Bambu printers allow only a few connections at "
                        "once."
                        + self._other_clients_hint()
                        + "\n  Otherwise, close Bambu Studio, the Handy app, "
                        "or another machine using the printer, then try "
                        "again.\n"
                        "  If that's not it: check the printer is powered "
                        "on and on this network, and that LAN Only Mode is "
                        f"on -- the switch is {_ACCESS_CODE_WHERE}.  A refused "
                        "login is reported separately, not as this."
                        + self._untried_model_note()
                    )

                # Certificate policy check (pin/explicit fingerprint) after TLS handshake.
                mqtt_sock = None
                try:
                    mqtt_sock = client.socket()
                except Exception:
                    mqtt_sock = None
                try:
                    self._validate_peer_certificate(
                        self._extract_socket_cert(mqtt_sock),
                        transport="MQTT",
                    )
                except PrinterError:
                    self._safe_stop_client(client)
                    self._backoff.record_failure()
                    raise

                self._mqtt_client = client
                self._backoff.record_success()
                self._start_idle_reaper()
                return client

            except PrinterError:
                raise
            except Exception as exc:
                self._backoff.record_failure()
                exc_str = str(exc).lower()
                is_single_client = (
                    isinstance(exc, (ConnectionResetError, ssl.SSLError))
                    or "connection reset by peer" in exc_str
                    or "errno 54" in exc_str
                    or "tls" in exc_str and "handshake" in exc_str
                )
                if is_single_client:
                    raise PrinterError(
                        _SINGLE_CLIENT_MSG + self._other_clients_hint(),
                        cause=exc,
                    ) from exc
                exc_lower = str(exc).lower()
                if isinstance(exc, ConnectionRefusedError) or "connection refused" in exc_lower:
                    detail = (
                        f"MQTT connection to {self._host}:{_MQTT_PORT} refused. "
                        "Printer may be powered off or MQTT port 8883 is blocked.\n"
                        "  1) Check that the printer is powered on\n"
                        "  2) Check that no firewall is blocking port 8883\n"
                    )
                elif isinstance(exc, OSError) or "errno" in exc_lower:
                    detail = (
                        f"Network error connecting MQTT to {self._host}:{_MQTT_PORT}: {exc}\n"
                        "  1) Check that the printer is on the same network\n"
                        "  2) Check router/firewall settings\n"
                    )
                else:
                    detail = f"Failed to connect MQTT to {self._host}:{_MQTT_PORT}: {exc}\n"
                raise PrinterError(
                    detail + "Retry with `get_state()` to check printer reachability.",
                    cause=exc,
                ) from exc

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        """MQTT on_connect callback."""
        # Check for auth failure or rejected connection before proceeding.
        rc = _connack_code(reason_code)
        if rc != 0:
            logger.warning(
                "MQTT connection rejected by %s (reason_code=%s)",
                self._host,
                reason_code,
            )
            # Recorded first, so the connect wait sees the answer at once.
            self._connack_refusal = (rc, str(reason_code))
            if rc in _CONNACK_CREDENTIAL_REFUSALS:
                logger.warning(
                    "Stopping MQTT client for %s due to auth failure — "
                    "check access code and re-register the printer",
                    self._host,
                )
                self._safe_stop_client(client)
                self._mqtt_client = None
            return

        client.subscribe(self._topic_report, qos=0)
        self._mqtt_connected.set()
        with self._state_lock:
            self._connected = True
        self._start_idle_reaper()

        # Request a full status dump.
        self._publish_command(
            {
                "pushing": {
                    "sequence_id": "0",
                    "command": "pushall",
                }
            },
            client=client,
        )

    def _await_connack(self, timeout: float) -> bool:
        """Wait for the printer to answer the login."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if self._mqtt_connected.is_set():
                return True
            if getattr(self, "_connack_refusal", None) is not None:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._mqtt_connected.wait(timeout=min(0.05, remaining))

    def _refusal_message(self, code: int, reason: str) -> str:
        """The error for a login the printer answered and refused."""
        if code in _CONNACK_CREDENTIAL_REFUSALS:
            return (
                f"The printer at {self._host} answered and refused Kiln's "
                f"access code ({reason}).  Copy the current access code "
                f"({_ACCESS_CODE_WHERE}) and update "
                "it in Kiln's config -- a restarted printer can issue a new one "
                "that looks the same." + self._untried_model_note()
            )
        return (
            f"The printer at {self._host} answered and turned the connection "
            f"away ({reason}).  Bambu printers allow only a few connections at "
            "once." + self._other_clients_hint() + self._untried_model_note()
        )

    def _untried_model_note(self) -> str:
        """One more sentence for a failed connection on a model Kiln has not tried on real hardware."""
        try:
            from kiln.cli.printer_model_prompt import suggest_bambu_model
            from kiln.printer_support import failed_connection_note

            model = getattr(self, "_safety_profile_id", None) or suggest_bambu_model(getattr(self, "_serial", None))
            note = failed_connection_note(model)
            return f"\n  {note}" if note else ""
        except Exception:  # noqa: BLE001 -- the failure is still reported without it
            return ""

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any = None,
        reason_code: Any = None,
        properties: Any = None,
    ) -> None:
        """MQTT on_disconnect callback."""
        self._mqtt_connected.clear()
        with self._state_lock:
            self._connected = False

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        """MQTT on_message callback -- update cached state."""
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        info_data = payload.get("info")
        if isinstance(info_data, dict) and isinstance(info_data.get("module"), list) and info_data["module"]:
            with self._state_lock:
                self._fw_modules = info_data["module"]

        # Merge print status fields into our cache
        print_data = payload.get("print", {})
        prev_gcode_state: str | None = None
        new_gcode_state: str | None = None
        job_id_for_hook: Any = None
        file_name_for_hook: Any = None
        print_error_for_hook: int = 0
        prev_print_error: int = 0
        prev_tray_now: str | None = None
        new_tray_now: str | None = None
        push_gap_seconds: float | None = None
        if isinstance(print_data, dict):
            cmd = str(print_data.get("command", "")).lower()
            if cmd == "push_status":
                msg_ts = print_data.get("msg_timestamp")
                with self._state_lock:
                    if msg_ts is not None:
                        try:
                            msg_ts_float = float(msg_ts)
                        except (TypeError, ValueError):
                            msg_ts_float = None
                        if msg_ts_float is not None:
                            last_ts = self._last_status.get("msg_timestamp")
                            if last_ts is not None:
                                try:
                                    last_ts_float = float(last_ts)
                                except (TypeError, ValueError):
                                    last_ts_float = None
                                if last_ts_float is not None and msg_ts_float < last_ts_float:
                                    logger.debug(
                                        "Discarding stale MQTT update (msg_ts=%.0f < last_ts=%.0f)",
                                        msg_ts_float,
                                        last_ts_float,
                                    )
                                    return
                    prev_gcode_state = str(
                        self._last_status.get("gcode_state", "")
                    ).lower().strip()
                    with contextlib.suppress(TypeError, ValueError):
                        prev_print_error = int(
                            self._last_status.get("print_error") or 0
                        )
                    prev_tray_now = _tray_now_of(self._last_status)
                    # How long since we last KNEW what this printer was doing
                    push_gap_seconds = self._gcode_state_age_locked()
                    _merge_push_status(self._last_status, print_data)
                    self._last_state_time = time.monotonic()
                    for key in print_data:
                        self._field_seen_at[key] = self._last_state_time
                    # Stamp the vintage of the one key that decides the reported state
                    if "gcode_state" in print_data:
                        self._gcode_state_time = self._last_state_time
                        self._cadence.record(self._last_state_time)

                    new_gcode_state = str(
                        self._last_status.get("gcode_state", "")
                    ).lower().strip()
                    new_tray_now = _tray_now_of(self._last_status)
                    job_id_for_hook = (
                        self._last_status.get("subtask_name")
                        or self._last_status.get("task_id")
                        or self._last_status.get("subtask_id")
                        or ""
                    )
                    file_name_for_hook = (
                        self._last_status.get("gcode_file")
                        or self._last_status.get("subtask_name")
                        or None
                    )
                    print_error_for_hook = int(
                        self._last_status.get("print_error") or 0
                    )
            # _state_lock has been released here (outside the `with`)
            lifecycle_name = outcome_printer_name(self)
            if getattr(self, "_restore_nozzle_detection", None) and prev_gcode_state:
                with self._state_lock:
                    _ended_state = str(self._last_status.get("gcode_state") or "")
                self._maybe_restore_nozzle_detection(prev_gcode_state, _ended_state)
            # _state_lock has been released here (outside the `with`)
            if (
                prev_tray_now is not None
                and new_tray_now is not None
                and prev_tray_now != new_tray_now
            ):
                if self._kiln_is_driving():
                    held = getattr(self, "_cutter_print", None)
                    if isinstance(held, dict):
                        held["observed"] = int(held.get("observed") or 0) + 1
                else:
                    self._notice_tray_change(prev_tray_now, new_tray_now)
            if prev_gcode_state and new_gcode_state and job_id_for_hook:
                try:
                    from kiln.auto_record_hook import (
                        ending_is_a_finish,
                        fire_terminal_state_hook,
                        is_terminal_transition,
                        observe_state,
                    )

                    observe_state(lifecycle_name, new_gcode_state)

                    ended = is_terminal_transition(
                        prev_gcode_state, new_gcode_state
                    )
                    if ended:
                        self._reconcile_cutter_print(str(file_name_for_hook or job_id_for_hook))
                    elapsed_seconds = (
                        job_elapsed_seconds(self, file_name_for_hook)
                        if ended
                        else None
                    )

                    fire_terminal_state_hook(
                        prev_state=prev_gcode_state,
                        new_state=new_gcode_state,
                        print_error_code=print_error_for_hook,
                        printer_name=lifecycle_name,
                        job_id=str(job_id_for_hook),
                        file_name=str(file_name_for_hook) if file_name_for_hook else None,
                    )

                    if ended:
                        # Stop the stopwatch — the job it was measuring is over
                        forget_job_start(self)
                        _record_print_duration(
                            job_label=str(job_id_for_hook),
                            elapsed_seconds=elapsed_seconds,
                            state_age_seconds=0.0,
                            observation_gap_seconds=push_gap_seconds,
                            duration_semantics=self._DURATION_SEMANTICS,
                            printer_name=lifecycle_name,
                            finished=ending_is_a_finish(new_gcode_state),
                        )
                except Exception as exc:  # pragma: no cover
                    logger.debug(
                        "auto-record hook raised (non-fatal): %s", exc,
                    )

            if not self._pending_outcomes_reconciled and new_gcode_state:
                self._pending_outcomes_reconciled = True
                threading.Thread(
                    target=self._reconcile_pending_outcomes,
                    kwargs={
                        "printer_name": lifecycle_name,
                        "gcode_state": new_gcode_state,
                        "print_error_code": print_error_for_hook,
                        "current_job_label": (
                            str(job_id_for_hook) if job_id_for_hook else None
                        ),
                        "legacy_printer_name": self.name,
                    },
                    name="kiln-outcome-reconcile",
                    daemon=True,
                ).start()

            if print_error_for_hook:
                _flow = _classify_flow_anomaly(print_error_for_hook)
                if _flow is not None:
                    _event_type, _severity = _flow
                    from kiln._pro_nozzle_bridge import report_flow_anomaly

                    report_flow_anomaly(self.name, _event_type, _severity)

            # A fault Kiln can plainly see, on a printer nobody asked it to watch
            if (
                print_error_for_hook
                and print_error_for_hook != prev_print_error
                and not self._own_stop_settling()
            ):
                self._notice_fault(print_error_for_hook, lifecycle_name)

    _STOP_SETTLE_SECONDS: float = 15.0

    _slot_observer_native: bool = True

    def _notice_tray_change(self, before: str, after: str) -> None:
        """Report one observed tray change."""
        try:
            from kiln._pro_cutter_bridge import record_observed_switch

            record_observed_switch(
                self.name,
                printer_model=self.declared_printer_model() or None,
                from_tray=before,
                to_tray=after,
            )
        except Exception:  # noqa: BLE001
            logger.debug("observed tray change not reported", exc_info=True)

    def _reconcile_pending_outcomes(self, **kwargs: Any) -> None:
        """Settle outcome rows left pending while nothing watched."""
        with self._reconcile_lock:
            try:
                from kiln.auto_record_hook import reconcile_pending_outcomes

                reconcile_pending_outcomes(**kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.debug("pending-outcome reconcile raised (non-fatal): %s", exc)

    def _own_stop_settling(self) -> bool:
        """Is this fault the tail of a stop Kiln itself just sent?"""
        if not self._stop_sent_at:
            return False
        return (time.monotonic() - self._stop_sent_at) < self._STOP_SETTLE_SECONDS

    def _notice_fault(self, code: int, printer_name: str) -> None:
        """Say, once, that this printer has raised a fault."""
        pretty = format_error_code(code)
        fault = read_bambu_fault(pretty or str(code), kind="print_error", wait=False)
        reading = fault.reading
        logger.warning(
            "Printer %s reported fault %s: %s",
            printer_name or self.name, pretty or code, reading,
        )
        # Off the network thread, deliberately
        threading.Thread(
            target=self._publish_fault_event,
            args=(code, pretty, reading, printer_name or self.name, fault.remedy),
            name="kiln-fault-notice",
            daemon=True,
        ).start()

    def _publish_fault_event(
        self,
        code: int,
        pretty: str | None,
        reading: str,
        printer_name: str,
        remedy: str | None = None,
    ) -> None:
        """Announce the fault on the event bus."""
        try:
            import kiln.server as _srv
            from kiln.events import Event, EventType

            fresh = read_bambu_fault(pretty or str(code), kind="print_error")
            if fresh.decoded is not None:
                reading, remedy = fresh.reading, fresh.remedy
            data: dict[str, Any] = {
                "printer_name": printer_name,
                "print_error": int(code),
                "print_error_code": pretty,
                "reading": reading,
                "noticed_by": "connection",
            }
            if remedy:
                data["remedy"] = remedy
            faults = compose_bambu_faults(
                {"print_error": int(code)},
                device_type=device_type_from_serial(self._serial),
            )
            if faults:
                data["faults"] = faults
                if "screen_text" in faults[0]:
                    data["screen_text"] = faults[0]["screen_text"]
                    data["source"] = faults[0]["source"]
            _srv._get_event_bus().publish(
                Event(
                    type=EventType.PRINTER_ERROR,
                    data=data,
                    source=f"printer:{printer_name}",
                )
            )
        except Exception as exc:  # noqa: BLE001 — never break the push path
            logger.debug("fault notice not published: %s", exc)

    def _publish_command(
        self,
        payload: dict[str, Any],
        *,
        client: mqtt.Client | None = None,
    ) -> None:
        """Publish an MQTT command to the printer."""
        c = client or self._ensure_mqtt()
        if client is None and (c is None or not c.is_connected()):
            self._mark_mqtt_dropped()
            raise PrinterError(
                "MQTT command not sent: the client is not connected to "
                f"{self._host}. The command was NOT delivered. "
                "Retry with `get_state()` to re-establish the connection."
            )
        try:
            info = c.publish(
                self._topic_request,
                json.dumps(payload),
                qos=0,
            )
            # QoS 0 is fire-and-forget — no PUBACK to wait for
        except Exception as exc:
            raise PrinterError(
                f"Failed to publish MQTT command: {exc}\n"
                "MQTT session may have dropped. "
                "Retry with `get_state()` to re-establish the connection.",
                cause=exc,
            ) from exc
        rc = getattr(info, "rc", mqtt.MQTT_ERR_SUCCESS)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            if rc == mqtt.MQTT_ERR_NO_CONN:
                self._mark_mqtt_dropped()
            raise PrinterError(
                f"MQTT publish refused: {mqtt.error_string(rc)} (rc={int(rc)}). "
                "The command was NOT delivered to the printer. "
                "Retry with `get_state()` to re-establish the connection."
            )

    def _mark_mqtt_dropped(self) -> None:
        """Record that the MQTT session is gone so the next call reconnects."""
        self._mqtt_connected.clear()
        with self._state_lock:
            self._connected = False

    # ------------------------------------------------------------------
    # Internal: write verification by read-back
    # ------------------------------------------------------------------

    def _stamp_before_send(self, field_name: str | None = None) -> _SendStamp:
        """The instant a command is about to go out, with the link already up."""
        self._ensure_mqtt()
        prior: Any = None
        prior_seen = 0.0
        if field_name is not None:
            with self._state_lock:
                prior = self._last_status.get(field_name)
                prior_seen = self._field_seen_at.get(field_name, 0.0)
        return _SendStamp(at=time.monotonic(), prior=prior, prior_seen=prior_seen)

    def _await_readback(
        self,
        field_name: str,
        matches: Any,
        *,
        sent_at: float,
        window_s: float | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Wait for a status frame AFTER *sent_at* that carries *field_name* with a value *matches* accepts."""
        want_window = _COMMAND_CONFIRM_WINDOW_S if window_s is None else window_s
        if self._confirm_window_s is not None:
            want_window = self._confirm_window_s  # an explicit override wins outright
        window = max(0.0, min(want_window, float(self._timeout)))
        deadline = time.monotonic() + window
        observed: Any = None
        seen_at = 0.0
        while True:
            with self._state_lock:
                seen_at = self._field_seen_at.get(field_name, 0.0)
                observed = self._last_status.get(field_name)
                last_frame = self._last_state_time
            if seen_at > sent_at:
                try:
                    hit = bool(matches(observed))
                except (TypeError, ValueError):
                    hit = False
                if hit:
                    return True, {
                        "corroboration": "read_back",
                        "field": field_name,
                        "observed": observed,
                        "confirm_latency_seconds": round(seen_at - sent_at, 2),
                    }
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)

        evidence: dict[str, Any] = {
            "field": field_name,
            "window_seconds": window,
        }
        if seen_at > sent_at:
            evidence["corroboration"] = "read_back_mismatch"
            evidence["observed"] = observed
        elif last_frame > sent_at:
            evidence["corroboration"] = "report_without_field"
        else:
            evidence["corroboration"] = "no_report_since_command"
        with self._state_lock:
            status = dict(self._last_status)
        faults = sorted(code for code, _kind in self._bambu_fault_codes(status))
        if faults:
            evidence["printer_faults"] = faults
        return False, evidence

    def _readback_verdict(
        self,
        what: str,
        field_name: str,
        matches: Any,
        *,
        sent: _SendStamp,
        wire: str,
        window_s: float | None = None,
    ) -> CommandVerdict:
        """One verdict for a write whose effect the status report shows."""
        confirmed, evidence = self._await_readback(
            field_name, matches, sent_at=sent.at, window_s=window_s
        )
        evidence["sent"] = wire
        if confirmed:
            return CommandVerdict.confirmed_by(
                f"{what}: the printer's next report shows it took effect.",
                **evidence,
            )
        why = {
            "no_report_since_command": (
                "the printer has not reported anything since the command"
            ),
            "report_without_field": (
                "the printer has reported since the command but has not "
                f"mentioned {field_name}"
            ),
            "read_back_mismatch": (
                f"the printer's report since the command shows {field_name}="
                f"{evidence.get('observed')!r}, not the requested value"
            ),
        }.get(
            str(evidence.get("corroboration")),
            f"the printer has not shown {field_name} at the requested value",
        )
        prior_value, prior_seen = sent.prior, sent.prior_seen
        if prior_seen > 0.0 and evidence["corroboration"] != "read_back_mismatch":
            try:
                already = bool(matches(prior_value))
            except (TypeError, ValueError):
                already = False
            if already:
                evidence["already_at_requested_value"] = True
                evidence["reported_before_command"] = prior_value
                return CommandVerdict.accepted_only(
                    f"{what}: sent over MQTT. The printer already reported "
                    f"{field_name}={prior_value!r} before the command, and it "
                    "reports this field when it CHANGES — so there was nothing "
                    "for it to report, and the command itself is unconfirmed. "
                    "The printer is in the requested state either way.",
                    **evidence,
                )
        tail = ""
        if evidence.get("printer_faults"):
            tail = (
                " The printer is reporting fault code(s) "
                f"{', '.join(evidence['printer_faults'])}; a fault showing on "
                "the screen is a reason a command may not have been acted "
                "on — check the printer before retrying."
            )
        return CommandVerdict.accepted_only(
            f"{what}: sent over MQTT, but NOT confirmed — {why} "
            f"(waited {evidence['window_seconds']:g}s). Re-read printer_status "
            f"and check {field_name} before building on this.{tail}",
            **evidence,
        )

    def _disable_nozzle_detection(self) -> None:
        """Disable nozzle clumping / blob detection via MQTT."""
        logger.info("Disabling nozzle clumping / blob detection for this print")
        self._publish_command(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "print_option",
                    "nozzle_blob_detect": False,
                }
            }
        )
        self._publish_command(
            {
                "xcam": {
                    "sequence_id": self._next_seq(),
                    "command": "xcam_control_set",
                    "module_name": "clump_detector",
                    "control": False,
                    "print_halt": False,
                }
            }
        )

    def _clumping_setting_is_three_way(self) -> bool:
        """Whether this printer's report offers the three-way setting."""
        try:
            with self._state_lock:
                status = dict(self._last_status)
        except AttributeError:
            return False
        decoded = decode_nozzle_clumping_switch(status)
        return decoded is not None and decoded[0] == CLUMP_SOURCE_THREE_WAY

    def _skip_nozzle_detection_for_print(self, kwargs: dict[str, Any]) -> str:
        """Turn the probe off for this print, remembering what to put back."""
        restore = bool(kwargs.get("restore_nozzle_detection", True))
        before = None
        if restore:
            try:
                before = self.read_nozzle_clumping_detection()
            except Exception:  # noqa: BLE001 -- an unreadable setting is "cannot say"
                before = None
        three_way = self._clumping_setting_is_three_way()
        if three_way:
            self._publish_command(
                {
                    "print": {
                        "sequence_id": self._next_seq(),
                        "command": "print_option",
                        "nozzle_blob_detect_v2": _CLUMP_THREE_WAY_VALUES["off"],
                    }
                }
            )
        else:
            self._disable_nozzle_detection()
        self._restore_nozzle_detection = None
        if not restore:
            return (
                "Kiln turned the printer's nozzle clumping detection off for this "
                "print and will leave it off (restore_nozzle_detection=False)."
            )
        if before is not None and before.enabled is True:
            prior = before.mode or "on"
            self._restore_nozzle_detection = {"three_way": three_way, "mode": prior}
            back = "set it back to automatic" if prior == "auto" else "turn it back on"
            return (
                "Kiln turned the printer's nozzle clumping detection off for this "
                f"print and will {back} when the print ends. If Kiln is not "
                "running when it ends, set it again in the printer's own settings."
            )
        if before is not None and (before.enabled is False or before.supported is False):
            return ""
        return (
            "Kiln asked the printer to skip the nozzle clumping probe. On this "
            "printer Kiln could not read whether the switch was on beforehand, "
            "so it will not turn it back on afterwards: that may leave the "
            "printer's own setting off -- check it after the print."
        )

    def _restore_nozzle_detection_now(self, reason: str) -> bool:
        """Put the setting back if Kiln turned it off and still owes that."""
        memo = getattr(self, "_restore_nozzle_detection", None)
        if not memo:
            return False
        self._restore_nozzle_detection = None
        logger.info("Restoring the printer's nozzle clumping detection (%s)", reason)
        payload: dict[str, Any] = {"sequence_id": self._next_seq(), "command": "print_option"}
        if memo.get("three_way"):
            payload["nozzle_blob_detect_v2"] = _CLUMP_THREE_WAY_VALUES.get(str(memo.get("mode")), 1)
        else:
            payload["nozzle_blob_detect"] = True
        try:
            self._publish_command({"print": payload})
        except Exception:  # noqa: BLE001 -- best effort; the note told the user how to do it by hand
            logger.debug("nozzle clumping detection restore failed", exc_info=True)
            return False
        return True

    def _maybe_restore_nozzle_detection(self, prev_state: str | None, new_state: str | None) -> None:
        """On the print's terminal transition, put the setting back."""
        if not getattr(self, "_restore_nozzle_detection", None):
            return
        try:
            from kiln.auto_record_hook import is_terminal_transition
        except ImportError:
            return
        if is_terminal_transition(prev_state, new_state):
            self._restore_nozzle_detection_now("the print ended")

    def _send_print_command(self, command: str) -> None:
        """Send a print-category command (pause/resume/stop)."""
        self._publish_command(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": command,
                }
            }
        )

    def _get_cached_status(self) -> dict[str, Any]:
        """Get the latest status from the cache, asking again when it is stale."""
        self._ensure_mqtt()

        with self._state_lock:
            if not self._last_status:
                need_refresh = True
            else:
                age = self._gcode_state_age_locked()
                budget = self._cadence.stale_after_seconds()
                since_asked = time.monotonic() - self._last_forced_refresh
                need_refresh = (
                    age is not None
                    and age > budget
                    and since_asked > budget
                )
            if need_refresh:
                self._last_forced_refresh = time.monotonic()

        if need_refresh:
            with self._state_lock:
                asked_at = self._gcode_state_time
            self._publish_command(
                {
                    "pushing": {
                        "sequence_id": self._next_seq(),
                        "command": "pushall",
                    }
                }
            )
            # Wait for the ANSWER, not for a fixed interval
            deadline = time.monotonic() + min(2.0, self._timeout / 2)
            while time.monotonic() < deadline:
                with self._state_lock:
                    if self._gcode_state_time != asked_at:
                        break
                time.sleep(0.05)

        with self._state_lock:
            return dict(self._last_status)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    def _ftp_connect(self) -> ftplib.FTP_TLS:
        """Open an FTPS connection with retry and exponential backoff."""
        last_exc: Exception | None = None
        for attempt in range(_FTPS_MAX_RETRIES):
            try:
                return self._ftp_connect_once()
            except PrinterError as exc:
                err_msg = str(exc).lower()
                # Don't retry auth failures or single-client locks.
                if any(
                    k in err_msg
                    for k in (
                        "authentication",
                        "login",
                        "530",
                        "already connected",
                        "another client",
                        "only allow one",
                    )
                ):
                    raise
                last_exc = exc
                if attempt < _FTPS_MAX_RETRIES - 1:
                    delay = 2**attempt  # 1s, 2s
                    logger.info(
                        "FTPS connection failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1,
                        _FTPS_MAX_RETRIES,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
        raise last_exc  # type: ignore[misc]

    def _ftp_connect_once(self) -> ftplib.FTP_TLS:
        """Open a single FTPS connection attempt (no retry)."""
        ftp: ftplib.FTP_TLS | None = None
        try:
            ctx = self._build_tls_context()

            ftp = _ImplicitFTP_TLS(context=ctx)
            ftp.connect(self._host, _FTPS_PORT, timeout=self._timeout)
            ftp.login(_FTPS_USERNAME, self._access_code)
            ftp.prot_p()  # Enable data channel encryption.
            self._validate_peer_certificate(
                self._extract_socket_cert(getattr(ftp, "sock", None)),
                transport="FTPS",
            )
            return ftp
        except Exception as exc:
            if ftp is not None:
                with contextlib.suppress(Exception):
                    ftp.close()
            exc_str = str(exc).lower()
            is_single_client = (
                isinstance(exc, (ConnectionResetError, ssl.SSLError))
                or "connection reset by peer" in exc_str
                or "tls" in exc_str and "handshake" in exc_str
            )
            if is_single_client:
                raise PrinterError(
                    _SINGLE_CLIENT_FTPS_MSG + self._other_clients_hint(),
                    cause=exc,
                ) from exc
            exc_lower = str(exc).lower()
            if "530" in exc_lower or "login" in exc_lower or "auth" in exc_lower:
                detail = (
                    f"FTPS authentication to {self._host}:{_FTPS_PORT} failed. "
                    "Access code may be wrong or stale.\n"
                    f"  1) Find the current access code {_ACCESS_CODE_WHERE}\n"
                    "  2) Toggle LAN Only Mode off/on to regenerate the code\n"
                )
            elif isinstance(exc, ConnectionRefusedError) or "connection refused" in exc_lower:
                detail = (
                    f"FTPS connection to {self._host}:{_FTPS_PORT} refused. "
                    "Printer may be powered off or port 990 is blocked.\n"
                )
            else:
                detail = f"FTPS connection to {self._host}:{_FTPS_PORT} failed: {exc}\n"
            raise PrinterError(
                detail + "Retry with `upload_file()` or check reachability with `get_state()`.",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # PrinterAdapter -- state queries
    # ------------------------------------------------------------------

    def _gcode_state_age_locked(self) -> float | None:
        """Seconds since a push last carried ``gcode_state``, or ``None``."""
        if not self._gcode_state_time:
            return None
        return max(0.0, time.monotonic() - self._gcode_state_time)

    def _build_state_from_cache(
        self,
        status: dict[str, Any],
        *,
        age: float | None = None,
        stale_after: float | None = None,
    ) -> PrinterState:
        """Convert a cached status dict into a :class:`PrinterState`."""
        gcode_state = status.get("gcode_state", "unknown")
        if not isinstance(gcode_state, str):
            gcode_state = "unknown"
        # A1/A1 mini sends uppercase state values (e.g. "RUNNING", "IDLE").
        gcode_state = gcode_state.lower()

        mapped = _STATE_MAP.get(gcode_state, PrinterStatus.UNKNOWN)
        job_result = _JOB_RESULT_MAP.get(gcode_state)

        if mapped == PrinterStatus.ERROR:
            raw_error = status.get("print_error")
            error_val: int = -1
            if raw_error is not None:
                with contextlib.suppress(TypeError, ValueError):
                    error_val = int(raw_error)
                if error_val == 0:
                    mapped = PrinterStatus.IDLE
            job_result = (
                JobResult.CANCELLED if error_val == 0 else JobResult.FAILED
            )

        # Speed profile.
        spd_lvl = status.get("spd_lvl")
        spd_lvl_int: int | None = None
        if spd_lvl is not None:
            with contextlib.suppress(TypeError, ValueError):
                spd_lvl_int = int(spd_lvl)
        speed_name = _SPEED_PROFILE_NAMES.get(spd_lvl_int) if spd_lvl_int else None
        spd_mag = status.get("spd_mag")
        spd_mag_int: int | None = None
        if spd_mag is not None:
            with contextlib.suppress(TypeError, ValueError):
                spd_mag_int = int(spd_mag)

        print_error = status.get("print_error")
        print_error_int: int | None = None
        if print_error is not None:
            with contextlib.suppress(TypeError, ValueError):
                print_error_int = int(print_error)

        budget = stale_after if stale_after is not None else STALE_STATE_WARN_AGE

        device_type = device_type_from_serial(getattr(self, "_serial", ""))
        kick_background_refresh(device_type)
        faults = compose_bambu_faults(status, device_type=device_type) or None

        fault_note: str | None = None
        fault_remedy: str | None = None
        if print_error_int:
            pretty = format_error_code(print_error_int)
            lead = next(
                (f for f in faults or () if f.get("kind") == "print_error"), None
            )
            if lead is None:
                fault = read_bambu_fault(
                    pretty or str(print_error_int), kind="print_error"
                )
                reading, remedy = fault.reading, fault.remedy
            else:
                reading, remedy = lead["reading"], lead.get("remedy")
            fault_note = describe_unacknowledged_fault(pretty, reading)
            if remedy:
                fault_remedy = describe_fault_remedy(remedy)

        chamber_sensor = chamber_sensor_for_model(self._chamber_lookup_model())
        chamber_temper = status.get("chamber_temper") if chamber_sensor else None

        return PrinterState(
            connected=True,
            state=mapped,
            state_stale_after_seconds=round(budget, 1),
            last_job_result=job_result,
            tool_temp_actual=status.get("nozzle_temper"),
            tool_temp_target=status.get("nozzle_target_temper"),
            bed_temp_actual=status.get("bed_temper"),
            bed_temp_target=status.get("bed_target_temper"),
            chamber_temp_actual=chamber_temper,
            chamber_sensor=chamber_sensor,
            cooling_fan_speed=status.get("cooling_fan_speed"),
            aux_fan_speed=status.get("big_fan1_speed"),
            chamber_fan_speed=status.get("big_fan2_speed"),
            heatbreak_fan_speed=status.get("heatbreak_fan_speed"),
            wifi_signal=status.get("wifi_signal"),
            nozzle_diameter=status.get("nozzle_diameter"),
            nozzle_type=status.get("nozzle_type"),
            speed_profile=speed_name,
            speed_magnitude=spd_mag_int,
            print_error=print_error_int,
            fault_note=fault_note,
            fault_remedy=fault_remedy,
            faults=faults,
            state_age_seconds=round(age, 1) if age is not None else None,
        )

    def read_nozzle_setting(self) -> NozzleSetting | None:
        """The nozzle this printer was told it has, from its own status report."""
        state = self.get_state()
        material = str(state.nozzle_type or "").strip() or None
        diameter: float | None = None
        try:
            diameter = float(state.nozzle_diameter) if state.nozzle_diameter not in (None, "") else None
        except (TypeError, ValueError):
            diameter = None
        if material is None and diameter is None:
            return None
        return NozzleSetting(
            material=material,
            diameter_mm=diameter,
            source="bambu_mqtt_report",
            age_seconds=state.state_age_seconds,
            stale_after_seconds=state.state_stale_after_seconds,
            firmware_version=self._printer_firmware_version(),
        )

    def read_nozzle_clumping_detection(self) -> NozzleClumpingDetection | None:
        """The printer's own nozzle clumping detection setting, from its status report (:func:`decode_nozzle_clumping_switch`)."""
        state = self.get_state()
        if not state.connected:
            return None
        with self._state_lock:
            status = dict(self._last_status)
        decoded = decode_nozzle_clumping_switch(status)
        if decoded is None:
            return None
        source, supported, mode = decoded
        stamps = {
            "source": source,
            "age_seconds": state.state_age_seconds,
            "stale_after_seconds": state.state_stale_after_seconds,
            "firmware_version": self._printer_firmware_version(),
            "supported": supported,
        }
        if not supported:
            return NozzleClumpingDetection(enabled=None, **stamps)
        if mode is None:
            return NozzleClumpingDetection(
                enabled=None,
                unverified_reason=(
                    "the printer reported a nozzle clumping detection value "
                    "Kiln does not recognise"
                ),
                **stamps,
            )
        return NozzleClumpingDetection(enabled=mode != "off", mode=mode, **stamps)

    def _printer_firmware_version(self) -> str | None:
        """The printer firmware from the cached module list, when it holds one."""
        with self._state_lock:
            modules = list(self._fw_modules)
        for mod in modules:
            if isinstance(mod, dict) and str(mod.get("name") or "").casefold() == "ota":
                version = str(mod.get("sw_ver") or "").strip()
                return version or None
        return None

    def reported_firmware_version(self) -> str | None:
        """The OTA firmware version from the cached module list; no request."""
        return self._printer_firmware_version()

    def homed_axes_now(self) -> set[str] | None:
        """Which axes the printer says are homed, from a push it answers NOW."""
        self._ensure_mqtt()
        with self._state_lock:
            asked_at = self._gcode_state_time
        self._publish_command(
            {"pushing": {"sequence_id": self._next_seq(), "command": "pushall"}}
        )
        deadline = time.monotonic() + min(2.0, self._timeout / 2)
        while time.monotonic() < deadline:
            with self._state_lock:
                if self._gcode_state_time and self._gcode_state_time != asked_at:
                    flag = self._last_status.get("home_flag")
                    break
            time.sleep(0.05)
        else:
            return None
        if not isinstance(flag, int):
            return None
        return {axis for axis, bit in _HOMED_AXIS_BITS.items() if flag >> bit & 1}

    def homed_axes_field(self) -> str | None:
        return HOMED_AXES_FIELD

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures."""
        if self._backoff.in_cooldown():
            with self._state_lock:
                age = time.monotonic() - self._last_state_time
                if self._last_status and age < self._cadence.stale_after_seconds():
                    logger.debug(
                        "In backoff cooldown; returning cached state (%.1fs old)",
                        age,
                    )
                    return self._build_state_from_cache(
                        dict(self._last_status),
                        age=self._gcode_state_age_locked(),
                        stale_after=self._cadence.stale_after_seconds(),
                    )
            logger.debug(
                "In backoff cooldown with no recent cached state; diagnosing"
            )
            return self._unreachable("MQTT reconnection is in backoff cooldown")

        try:
            status = self._get_cached_status()
        except PrinterError as exc:
            return self._unreachable(str(exc))

        with self._state_lock:
            state_age = self._gcode_state_age_locked()
            stale_after = self._cadence.stale_after_seconds()
        return self._build_state_from_cache(
            status, age=state_age, stale_after=stale_after
        )

    def _unreachable(self, message: str) -> PrinterState:
        """Why this printer cannot be seen, in place of a bare OFFLINE."""
        holders: int | None = None
        try:
            from kiln.serve_siblings import printer_connection_holders

            held = printer_connection_holders(self._host)
            if held.get("supported"):
                holders = int(held.get("kiln_count") or 0)
        except Exception:  # noqa: BLE001 — a diagnosis never breaks a read
            logger.debug("connection-holder scan unavailable", exc_info=True)
        return diagnosed_state(
            diagnose_read_failure(
                message,
                host=self._host,
                port=_MQTT_PORT,
                kiln_slot_holders=holders,
            )
        )

    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active (or last) print job."""
        try:
            status = self._get_cached_status()
        except PrinterError:
            return JobProgress()

        file_name = status.get("gcode_file") or status.get("subtask_name")
        mc_percent = status.get("mc_percent")
        mc_remaining = status.get("mc_remaining_time")  # minutes

        completion: float | None = None
        if mc_percent is not None:
            completion = float(mc_percent)

        print_time_left_seconds: int | None = None
        if mc_remaining is not None:
            print_time_left_seconds = int(mc_remaining) * 60

        print_time_seconds: int | None = job_elapsed_seconds(
            self, file_name or None
        )

        # Layer tracking.
        current_layer: int | None = None
        total_layers: int | None = None
        layer_num = status.get("layer_num")
        total_layer_num = status.get("total_layer_num")
        if layer_num is not None:
            with contextlib.suppress(TypeError, ValueError):
                current_layer = int(layer_num)
        if total_layer_num is not None:
            with contextlib.suppress(TypeError, ValueError):
                total_layers = int(total_layer_num)

        native_job_id = _first_real_job_id(
            status.get("task_id"), status.get("subtask_id"),
        )

        # Has this job already ENDED?
        ended_as = self._job_ending(status)
        # And is it the job the machine is running now?
        raw_state = status.get("gcode_state")
        running: bool | None = None
        if isinstance(raw_state, str):
            lowered = raw_state.lower()
            running = lowered in _PRINT_ACTIVE_STATES or lowered == "pause"

        return JobProgress(
            file_name=file_name if file_name else None,
            completion=completion,
            print_time_seconds=print_time_seconds,
            print_time_left_seconds=print_time_left_seconds,
            current_layer=current_layer,
            total_layers=total_layers,
            job_id=native_job_id,
            ended_as=ended_as,
            active=running,
        )

    @staticmethod
    def _job_ending(status: dict[str, Any]) -> JobResult | None:
        """How the job in *status* ended, or ``None`` while it is running."""
        gcode_state = status.get("gcode_state")
        if not isinstance(gcode_state, str):
            return None
        gcode_state = gcode_state.lower()
        if gcode_state in _PRINT_ACTIVE_STATES or gcode_state == "pause":
            return None
        ended = _JOB_RESULT_MAP.get(gcode_state)
        if ended is not None:
            return ended
        if _STATE_MAP.get(gcode_state) is not PrinterStatus.ERROR:
            return None
        raw_error = status.get("print_error")
        if raw_error is None:
            return JobResult.FAILED
        try:
            return (
                JobResult.CANCELLED if int(raw_error) == 0 else JobResult.FAILED
            )
        except (TypeError, ValueError):
            return JobResult.FAILED

    def list_files(self) -> list[PrinterFile]:
        """Return a list of files stored on the printer's storage."""
        try:
            ftp = self._ftp_connect()
        except PrinterError:
            raise

        try:
            storage_path = self._detect_storage_path(ftp)

            # Try MLSD first (rich metadata: name, size, modify time).
            try:
                return self._list_via_mlsd(ftp, storage_path)
            except ftplib.error_perm as exc:
                if not str(exc).startswith("502"):
                    raise
                logger.info("MLSD not supported (502), falling back to NLST")

            # Fallback: NLST (filenames only).
            try:
                return self._list_via_nlst(ftp, storage_path)
            except Exception:
                logger.info("NLST failed, falling back to LIST")

            # Last resort: LIST (raw text parsing)
            try:
                return self._list_via_list(ftp, storage_path)
            except ftplib.error_perm as exc:
                if not str(exc).startswith("550"):
                    raise
                logger.info(
                    "LIST returned 550 (not supported), falling back to NLST"
                )
                return self._list_via_nlst(ftp, storage_path)
        except PrinterError:
            raise
        except Exception as exc:
            raise PrinterError(
                f"Failed to list files via FTPS: {exc}\n"
                "If you just formatted the SD card, the /model/ directory may need to be recreated.\n"
                "Retry with `list_files()`. If persistent, check FTPS connectivity with `get_state()`.",
                cause=exc,
            ) from exc
        finally:
            try:
                ftp.quit()
            except Exception as exc:
                logger.debug("Failed to quit FTP session after listing files: %s", exc)

    def _list_via_mlsd(
        self, ftp: ftplib.FTP_TLS, storage_path: str,
    ) -> list[PrinterFile]:
        """List files using MLSD (rich metadata: name, size, modify time)."""
        entries: list[PrinterFile] = []
        for name, facts in ftp.mlsd(f"{storage_path}/"):
            if name in (".", ".."):
                continue
            if facts.get("type") == "dir":
                continue

            size_str = facts.get("size")
            size = int(size_str) if size_str else None

            modify = facts.get("modify")
            date_ts: int | None = None
            if modify:
                try:
                    import datetime

                    dt = datetime.datetime.strptime(modify, "%Y%m%d%H%M%S")
                    date_ts = int(dt.timestamp())
                except (ValueError, OSError):
                    pass

            entries.append(
                PrinterFile(
                    name=name,
                    path=f"{storage_path}/{name}",
                    size_bytes=size,
                    date=date_ts,
                )
            )
        return entries

    def _list_via_nlst(
        self, ftp: ftplib.FTP_TLS, storage_path: str,
    ) -> list[PrinterFile]:
        """List files using NLST (filenames only, no metadata)."""
        names = ftp.nlst(f"{storage_path}/")
        entries: list[PrinterFile] = []
        for raw_name in names:
            name = raw_name.rsplit("/", 1)[-1] if "/" in raw_name else raw_name
            if name in (".", ".."):
                continue
            entries.append(
                PrinterFile(
                    name=name,
                    path=f"{storage_path}/{name}",
                    size_bytes=None,
                    date=None,
                )
            )
        return entries

    def _list_via_list(
        self, ftp: ftplib.FTP_TLS, storage_path: str,
    ) -> list[PrinterFile]:
        """List files using LIST (raw text, parse filenames from output)."""
        lines: list[str] = []
        ftp.retrlines(f"LIST {storage_path}/", lines.append)
        entries: list[PrinterFile] = []
        for line in lines:
            parts = line.split()
            if not parts:
                continue
            name = parts[-1]
            if name in (".", ".."):
                continue
            # Skip directories (Unix-style listing: first char is 'd').
            if line.startswith("d"):
                continue
            size: int | None = None
            if len(parts) >= 5:
                with contextlib.suppress(ValueError):
                    size = int(parts[4])
            entries.append(
                PrinterFile(
                    name=name,
                    path=f"{storage_path}/{name}",
                    size_bytes=size,
                    date=None,
                )
            )
        return entries

    # ------------------------------------------------------------------
    # PrinterAdapter -- file management
    # ------------------------------------------------------------------

    def _detect_storage_path(self, ftp: ftplib.FTP_TLS) -> str:
        """Detect the correct FTPS storage path for this printer."""
        cache = f"/{_BAMBU_FTP_URL_DIR}"
        if self._printer_model in _BAMBU_FTP_URL_MODELS:
            candidates = (cache, "/model", "/sdcard")
        else:
            candidates = ("/model", "/sdcard", cache)
        for path in candidates:
            try:
                ftp.cwd(path)
            except ftplib.error_perm:
                continue
            logger.debug("Detected Bambu storage path: %s", path)
            return path
        # Nothing answered
        logger.warning(
            "No Bambu storage directory answered CWD (tried %s); falling "
            "back to /sdcard. Uploads may land where the firmware does not "
            "look.",
            ", ".join(candidates),
        )
        return "/sdcard"

    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a file to the printer via FTPS."""
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Local file not found: {abs_path}")

        filename = os.path.basename(abs_path)

        from kiln.printers.bambu_3mf import bambu_archive_problems

        problems = bambu_archive_problems(abs_path)
        if problems:
            raise PrinterError(f"Refused to upload {filename}: " + "; ".join(problems) + ".")

        try:
            ftp = self._ftp_connect()
        except PrinterError:
            raise

        try:
            storage_path = self._detect_storage_path(ftp)
            self._last_storage_path = storage_path
            with open(abs_path, "rb") as fh:
                ftp.storbinary(f"STOR {storage_path}/{filename}", fh)
            return UploadResult(
                success=True,
                file_name=filename,
                message=f"Uploaded {filename} to {storage_path}/ on Bambu printer via FTPS.",
            )
        except PermissionError as exc:
            raise PrinterError(
                f"Permission denied reading file: {abs_path}",
                cause=exc,
            ) from exc
        except Exception as exc:
            exc_lower = str(exc).lower()
            if "550" in exc_lower or "no such file" in exc_lower:
                detail = (
                    f"FTPS upload failed — storage path may not exist: {exc}\n"
                    "Try reformatting the SD card on the printer touchscreen.\n"
                )
            elif "timed out" in exc_lower or isinstance(exc, TimeoutError):
                detail = (
                    f"FTPS upload timed out: {exc}\n"
                    "Connection dropped during upload — check network stability.\n"
                )
            else:
                detail = f"FTPS upload failed: {exc}\n"
            raise PrinterError(
                detail + "Retry with `upload_file()`.",
                cause=exc,
            ) from exc
        finally:
            try:
                ftp.quit()
            except Exception as exc:
                logger.debug("Failed to quit FTP session after upload: %s", exc)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    def wrap_gcode_as_3mf(
        self,
        gcode_path: str,
        *,
        hotend_temp: int | None = None,
        bed_temp: int | None = None,
        filament_type: str | None = None,
        source_3mf_path: str | None = None,
        num_filaments: int = 1,
        filament_colors: list[str] | None = None,
        filament_types: list[str] | None = None,
        stl_paths: list[str] | None = None,
        resume_mode: bool = False,
        quiet_start: dict[str, Any] | None = None,
        lift_floor_mm: float | None = None,
        bed_type: str | None = None,
    ) -> str:
        """Wrap PrusaSlicer gcode in a Bambu-compatible 3MF."""
        from kiln.printers.bambu_3mf import TEXTURED_PLATE, BambuPrintSettings, build_bambu_3mf

        abs_path = os.path.abspath(gcode_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Gcode file not found: {abs_path}")

        gcode_body = Path(abs_path).read_text(encoding="utf-8")
        stem = Path(abs_path).stem
        if quiet_start is not None and not stem.endswith("_quiet"):
            stem = f"{stem}_quiet"
        output_path = os.path.join(os.path.dirname(abs_path), f"{stem}.3mf")

        if not filament_colors and num_filaments == 1:
            loaded = self.active_filament_color()
            if loaded:
                filament_colors = [loaded]

        settings = BambuPrintSettings(
            hotend_temp=hotend_temp,
            bed_temp=bed_temp,
            filament_type=filament_type,
            model_name=stem,
            num_filaments=num_filaments,
            filament_colors=filament_colors,
            filament_types=filament_types,
            bed_type=bed_type or TEXTURED_PLATE,
        )

        result = build_bambu_3mf(
            gcode_body,
            output_path,
            settings=settings,
            source_3mf_path=source_3mf_path,
            stl_paths=stl_paths,
            resume_mode=resume_mode,
            printer_model=self._printer_model,
            quiet_start=quiet_start,
            lift_floor_mm=lift_floor_mm,
        )
        try:
            from kiln.monitor_twin import note_wrapped

            note_wrapped(abs_path, result.output_path)
        except Exception:  # noqa: BLE001 — bookkeeping never blocks a wrap
            logger.debug("monitor-twin wrap note failed", exc_info=True)
        return result.output_path

    # ------------------------------------------------------------------
    # PrinterAdapter -- print control
    # ------------------------------------------------------------------

    def _wait_for_print_start(
        self,
        timeout: float = 15.0,
        poll_interval: float = 1.0,
        sent_at: float | None = None,
    ) -> tuple[str, int | None]:
        """Poll MQTT cache until printer enters a print-active state."""
        deadline = time.monotonic() + timeout
        last_error: int | None = None
        while time.monotonic() < deadline:
            with self._state_lock:
                stale = (
                    sent_at is not None and self._last_state_time < sent_at
                )
                state = "" if stale else str(
                    self._last_status.get("gcode_state", "")
                ).lower()
                raw_err = None if stale else self._last_status.get("print_error")
            if raw_err is not None:
                with contextlib.suppress(TypeError, ValueError):
                    err_val = int(raw_err)
                    if err_val != 0:
                        last_error = err_val
            if state in _PRINT_ACTIVE_STATES:
                return state, last_error
            if state == "failed" and (last_error or raw_err is None):
                return "failed", last_error
            if last_error is not None and state in ("idle", "finish"):
                return "failed", last_error
            time.sleep(poll_interval)
        return "timeout", last_error

    @staticmethod
    def _compute_file_md5(file_path: str) -> str:
        """Compute the MD5 hex digest of a local file."""
        md5 = hashlib.md5()
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(8192), b""):
                md5.update(chunk)
        return md5.hexdigest()

    def _check_ams_color_mismatch(
        self,
        file_path: str,
        plate_number: int,
        ams_mapping: list[int],
    ) -> list[str]:
        """Check if 3MF expected filament colors match what's in the AMS."""
        warnings: list[str] = []
        try:
            expected_colors = self._detect_3mf_filaments(file_path, plate_number)
            if not expected_colors:
                return warnings

            ams_info = self.get_ams_status()
            loaded_trays: dict[int, str] = {}
            for unit_pos, unit in enumerate(ams_info.get("units", [])):
                try:
                    unit_id = int(unit.get("unit_id", unit_pos))
                except (TypeError, ValueError):
                    unit_id = unit_pos
                for tray in unit.get("trays", []):
                    tray_idx = tray.get("slot")
                    tray_color = tray.get("tray_color", "")
                    if tray_idx is not None and tray_color:
                        try:
                            key = _bambu_tray_id(unit_id, int(tray_idx))
                        except (TypeError, ValueError):
                            continue
                        loaded_trays[key] = tray_color[:6].upper()

            for i, slot in enumerate(ams_mapping):
                if i >= len(expected_colors):
                    break
                expected_hex = expected_colors[i].lstrip("#").upper()[:6]
                loaded_hex = loaded_trays.get(slot, "")
                if loaded_hex and expected_hex and expected_hex != loaded_hex:
                    msg = (
                        f"AMS color mismatch: plate {plate_number} filament {i} "
                        f"expects #{expected_hex} but {_describe_tray_id(slot)} has "
                        f"#{loaded_hex} loaded."
                    )
                    logger.warning("%s", msg)
                    warnings.append(msg)
        except Exception:
            logger.debug("AMS color mismatch check failed", exc_info=True)
        return warnings

    @staticmethod
    def _read_3mf_plate_meta(
        file_path: str,
        plate_number: int = 1,
    ) -> dict[str, Any] | None:
        """Read plate metadata from a 3MF archive."""
        import json

        try:
            with UntrustedZipFile(file_path, "r") as zf:
                meta_name = f"Metadata/plate_{plate_number}.json"
                if meta_name not in zf.namelist():
                    return None
                with zf.open(meta_name) as mf:
                    return json.loads(mf.read())
        except Exception:
            logger.debug("Could not read 3MF plate metadata from %s", file_path, exc_info=True)
        return None

    @staticmethod
    def _detect_3mf_filaments(
        file_path: str,
        plate_number: int = 1,
    ) -> list[str] | None:
        """Extract filament color list from a 3MF's plate metadata."""
        meta = BambuAdapter._read_3mf_plate_meta(file_path, plate_number)
        if meta is None:
            return None
        colors = meta.get("filament_colors")
        if isinstance(colors, list) and len(colors) >= 1:
            return colors
        return None

    @staticmethod
    def _build_ams_mapping_from_3mf(
        file_path: str,
        plate_number: int = 1,
    ) -> list[int] | None:
        """Build an ``ams_mapping`` array from 3MF plate metadata."""
        meta = BambuAdapter._read_3mf_plate_meta(file_path, plate_number)
        if meta is None:
            return None

        filament_ids = meta.get("filament_ids")
        colors = meta.get("filament_colors")

        # Need at least 2 filaments for multi-material.
        if not isinstance(colors, list) or len(colors) < 2:
            return None

        if not isinstance(filament_ids, list) or len(filament_ids) != len(colors):
            return list(range(len(colors)))

        max_id = max(filament_ids)
        mapping = [-1] * (max_id + 1)
        for tray_idx, fid in enumerate(filament_ids):
            mapping[fid] = tray_idx
        return mapping

    @staticmethod
    def filament_count_3mf(file_path: str, plate_number: int = 1) -> int | None:
        """Number of filaments a 3MF plate declares, or ``None`` if the plate metadata can't be read."""
        meta = BambuAdapter._read_3mf_plate_meta(file_path, plate_number)
        if meta is None:
            return None
        colors = meta.get("filament_colors")
        if isinstance(colors, list):
            return len(colors)
        ids = meta.get("filament_ids")
        if isinstance(ids, list):
            return len(ids)
        return None

    @staticmethod
    def _detect_3mf_printer_model(
        file_path: str,
        *,
        plate_number: int = 1,
    ) -> str | None:
        """Extract the printer model from a 3MF's metadata."""
        import xml.etree.ElementTree as ET

        try:
            with UntrustedZipFile(file_path, "r") as zf:
                # 1. Check plate JSON metadata.
                meta_name = f"Metadata/plate_{plate_number}.json"
                if meta_name in zf.namelist():
                    with zf.open(meta_name) as mf:
                        meta = json.loads(mf.read())
                    model = meta.get("printer_model")
                    if isinstance(model, str) and model.strip():
                        return model.strip()

                # 2. Check XML config files for printer model info.
                for config_name in (
                    "Metadata/model_settings.config",
                    "Metadata/slice_info.config",
                ):
                    if config_name not in zf.namelist():
                        continue
                    with zf.open(config_name) as cf:
                        try:
                            tree = ET.parse(cf)
                        except ET.ParseError:
                            continue
                        root = tree.getroot()
                        # Look for <machine> or <printer_model> text.
                        for tag in ("machine", "printer_model"):
                            elem = root.find(f".//{tag}")
                            if elem is not None and elem.text and elem.text.strip():
                                return elem.text.strip()
                        # Also check attributes on the root or config elements.
                        for elem in root.iter():
                            for attr in ("printer_model", "machine"):
                                val = elem.get(attr, "").strip()
                                if val:
                                    return val
        except Exception:
            logger.debug(
                "Could not read 3MF printer model from %s",
                file_path,
                exc_info=True,
            )
        return None

    def _check_printer_model_mismatch(
        self,
        file_path: str,
        *,
        plate_number: int = 1,
    ) -> list[str]:
        """Check if a 3MF was sliced for a different printer model."""
        warnings: list[str] = []
        try:
            sliced_model = self._detect_3mf_printer_model(
                file_path, plate_number=plate_number,
            )
            if not sliced_model:
                return warnings

            sliced_family = _BAMBU_MODEL_FAMILIES.get(sliced_model)
            if not sliced_family:
                logger.debug(
                    "Unknown 3MF printer model %r — skipping mismatch check",
                    sliced_model,
                )
                return warnings

            # Identify the connected printer family from the serial prefix.
            serial_prefix = self._serial[:3] if len(self._serial) >= 3 else ""
            connected_family = _BAMBU_MODEL_FAMILIES.get(serial_prefix)
            if not connected_family:
                logger.debug(
                    "Unknown serial prefix %r — skipping mismatch check",
                    serial_prefix,
                )
                return warnings

            if sliced_family != connected_family:
                msg = (
                    f"Printer profile mismatch: 3MF was sliced for "
                    f"{sliced_model} ({sliced_family}) but the connected "
                    f"printer is {connected_family} (serial {self._serial}). "
                    f"Wrong printer profile means wrong speeds, accelerations, "
                    f"and firmware-specific gcode — this may cause print "
                    f"failures. Re-slice with the correct printer profile."
                )
                logger.warning("%s", msg)
                warnings.append(msg)
        except Exception:
            logger.debug("Printer model mismatch check failed", exc_info=True)
        return warnings

    def _validate_3mf_filament_ids(
        self,
        file_path: str,
        plate_number: int = 1,
    ) -> list[str]:
        """Check if a 3MF references filament slots that exceed AMS capacity."""
        import json

        issues: list[str] = []
        try:
            with UntrustedZipFile(file_path, "r") as zf:
                meta_name = f"Metadata/plate_{plate_number}.json"
                if meta_name not in zf.namelist():
                    return issues
                with zf.open(meta_name) as mf:
                    meta = json.loads(mf.read())

            filament_ids = meta.get("filament_ids")
            if not isinstance(filament_ids, list) or not filament_ids:
                return issues

            ams_info = self.get_ams_status()
            names: list[str] = []
            for position, unit in enumerate(ams_info.get("units", [])):
                for tray in unit.get("trays", []):
                    with contextlib.suppress(TypeError, ValueError):
                        names.append(_bambu_tray_name(int(unit.get("unit_id", position)), int(tray.get("slot", 0))))
            total_slots = len(names)

            if total_slots == 0:
                return issues

            max_id = max(filament_ids)
            if max_id >= total_slots:
                issues.append(
                    f"3MF plate {plate_number} references filament profile "
                    f"index {max_id} but your AMS only has {total_slots} "
                    f"tray(s) ({', '.join(names)}). This file was "
                    f"likely sliced with a multi-filament project that "
                    f"doesn't match your AMS setup. Re-slice the model in "
                    f"BambuStudio/OrcaSlicer with only your installed "
                    f"filaments, or provide an explicit --ams-mapping naming "
                    f"the printer's tray id for each filament (see ams_status)."
                )
        except PrinterError:
            logger.debug("Could not query AMS for filament_ids validation", exc_info=True)
        except Exception:
            logger.debug("Could not validate 3MF filament_ids from %s", file_path, exc_info=True)
        return issues

    def _build_print_url(self, basename: str) -> str:
        """Build the job URL for the MQTT print command."""
        if self._printer_model in _BAMBU_FTP_URL_MODELS:
            return f"ftp://{_BAMBU_FTP_URL_DIR}/{basename}"
        if self._last_storage_path == f"/{_BAMBU_FTP_URL_DIR}":
            return f"ftp://{_BAMBU_FTP_URL_DIR}/{basename}"
        return f"file:///sdcard/model/{basename}"

    def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
        """Begin printing a file on the Bambu printer."""
        basename = os.path.basename(file_name)

        command_sent_at = time.monotonic()

        with self._state_lock:
            already_active = str(self._last_status.get("gcode_state", "")).lower() in _PRINT_ACTIVE_STATES

        warnings: list[str] = []

        if basename.lower().endswith(".3mf"):
            plate_num = kwargs.get("plate_number", 1)
            ams_mapping = kwargs.get("ams_mapping")
            use_ams = kwargs.get("use_ams", False)

            local_path = kwargs.get("local_file_path")
            file_md5 = ""
            if local_path and os.path.isfile(local_path):
                file_md5 = self._compute_file_md5(local_path)

            declared: int | None = None
            if local_path and os.path.isfile(local_path):
                declared = self.filament_count_3mf(local_path, plate_num)
                too_short = (
                    ams_mapping is not None
                    and declared is not None
                    and len(ams_mapping) < declared
                )
                if ams_mapping is None or too_short:
                    auto_mapping = self._build_ams_mapping_from_3mf(
                        local_path, plate_num,
                    )
                    if auto_mapping is not None:
                        if too_short:
                            logger.warning(
                                "Plate %d declares %d filaments but the "
                                "supplied ams_mapping %s covers %d — using "
                                "the plate's own mapping %s so every color "
                                "reaches its tray.",
                                plate_num, declared, ams_mapping,
                                len(ams_mapping), auto_mapping,
                            )
                            warnings.append(
                                f"This plate uses {declared} filaments; the "
                                f"requested AMS mapping covered only "
                                f"{len(ams_mapping)}. Routed with the file's "
                                f"own mapping {auto_mapping} — pass an "
                                f"ams_mapping with {declared} entries to "
                                f"choose the trays yourself."
                            )
                        ams_mapping = auto_mapping
                        use_ams = True
                        logger.info(
                            "Auto-detected multi-material in plate %d — "
                            "setting use_ams=True, ams_mapping=%s",
                            plate_num,
                            ams_mapping,
                        )

            if (
                ams_mapping is None
                and "use_ams" not in kwargs
            ):
                loaded_trays = self._peek_loaded_ams_trays()
                if loaded_trays:
                    slot_idx = int(loaded_trays[0]["tray_id"])
                    ams_mapping = [slot_idx]
                    use_ams = True
                    logger.info(
                        "Single-filament AMS auto-routing: tray %d (slot %s, %s)",
                        slot_idx,
                        loaded_trays[0].get("name", "?"),
                        loaded_trays[0].get("tray_type", "unknown"),
                    )
                elif loaded_trays is not None:
                    warnings.append(
                        "AMS is attached but no trays report loaded "
                        "filament. Print will use the external-spool "
                        "feed path — if nothing is loaded there the "
                        "print will pause with error 0300-8015."
                    )
                    logger.warning(
                        "Bambu AMS has no loaded trays — routing to external spool"
                    )

            # Fall back to single-filament defaults
            if ams_mapping is None or not isinstance(ams_mapping, list):
                ams_mapping = [0] if use_ams else []

            if (
                use_ams
                and declared is None
                and len(ams_mapping) <= 1
                and not kwargs.get("resume_from_paused")
                and not is_resume_mode_3mf(basename)
            ):
                return PrintResult(
                    success=False,
                    message=(
                        f"Kiln could not read which filaments {basename} uses (plate {plate_num}'s "
                        "record could not be read), so it cannot tell which AMS tray should feed "
                        "each one, and it did not start the print on a guess. Start it from the "
                        "printer's own screen, where you choose the trays, or slice it again "
                        "through Kiln and start the new file."
                    ),
                    refused_before_send=True,
                )

            if use_ams and local_path and os.path.isfile(local_path):
                meta = self._read_3mf_plate_meta(local_path, plate_num)
                if meta is not None:
                    filament_ids = meta.get("filament_ids")
                    if isinstance(filament_ids, list) and filament_ids:
                        max_id = max(filament_ids)
                        if max_id >= len(ams_mapping):
                            msg = (
                                f"ams_mapping has {len(ams_mapping)} "
                                f"entries but the 3MF uses filament ID "
                                f"{max_id} (filament_ids={filament_ids}). "
                                f"Entries beyond the mapping length will "
                                f"default to unexpected AMS trays. The "
                                f"mapping needs at least {max_id + 1} "
                                f"entries (use -1 for unused positions)."
                            )
                            logger.warning("%s", msg)
                            warnings.append(msg)

            if local_path and os.path.isfile(local_path):
                filament_issues = self._validate_3mf_filament_ids(local_path, plate_num)
                if filament_issues:
                    return PrintResult(
                        success=False,
                        message=" ".join(filament_issues),
                    )

            if use_ams and local_path and os.path.isfile(local_path):
                warnings.extend(
                    self._check_ams_color_mismatch(local_path, plate_num, ams_mapping)
                )

            # Check for printer model mismatch (sliced for wrong printer).
            if local_path and os.path.isfile(local_path):
                warnings.extend(
                    self._check_printer_model_mismatch(
                        local_path, plate_number=plate_num,
                    )
                )

            subtask_name = os.path.splitext(basename)[0]

            if not kwargs.get("nozzle_clog_detect", True):
                note = self._skip_nozzle_detection_for_print(kwargs)
                if note:
                    warnings.append(note)

            self._publish_command(
                {
                    "print": {
                        "sequence_id": self._next_seq(),
                        "command": "project_file",
                        "param": f"Metadata/plate_{plate_num}.gcode",
                        "subtask_name": subtask_name,
                        "file": "",
                        "url": self._build_print_url(basename),
                        "md5": file_md5,
                        "bed_type": str(kwargs.get("bed_type", "auto")),
                        "timelapse": bool(kwargs.get("timelapse", False)),
                        "bed_leveling": bool(kwargs.get("bed_leveling", True)),
                        "flow_cali": bool(kwargs.get("flow_cali", False)),
                        "vibration_cali": bool(kwargs.get("vibration_cali", False)),
                        "layer_inspect": bool(kwargs.get("layer_inspect", False)),
                        "use_ams": bool(use_ams),
                        "ams_mapping": ams_mapping,
                        "profile_id": "0",
                        "project_id": "0",
                        "subtask_id": "0",
                        "task_id": "0",
                    }
                }
            )
        else:
            # Raw G-code file
            if file_name.startswith("/"):
                # This is a path on the printer's SD card — always POSIX-style
                path = posixpath.normpath(file_name)
                if not (path.startswith("/sdcard/") or path.startswith("/cache/")):
                    raise PrinterError(f"File path must be under /sdcard/ or /cache/, got: {file_name!r}")
            else:
                if self._last_storage_path == f"/{_BAMBU_FTP_URL_DIR}":
                    # P2S — jobs live in cache/, which is where the upload just went
                    path = f"/{_BAMBU_FTP_URL_DIR}/{basename}"
                elif self._last_storage_path == "/sdcard":
                    path = f"/sdcard/{basename}"
                else:
                    path = f"/sdcard/model/{basename}"
            self._publish_command(
                {
                    "print": {
                        "sequence_id": self._next_seq(),
                        "command": "gcode_file",
                        "param": path,
                    }
                }
            )

        # Build optional warning suffix from pre-print checks.
        warn_suffix = ""
        if warnings:
            warn_suffix = " WARNING: " + "; ".join(warnings)

        if not already_active:
            result_state, error_code = self._wait_for_print_start(
                sent_at=command_sent_at,
            )
            if result_state == "failed":
                self._restore_nozzle_detection_now("the print never started")
                err_detail = ""
                if error_code is not None:
                    known = _KNOWN_PRINT_ERRORS.get(error_code)
                    if known:
                        err_detail = f" {known}"
                    elif _is_nozzle_clump_error(error_code):
                        err_detail = f" {_NOZZLE_CLUMP_MESSAGE}"
                    else:
                        err_detail = (
                            f" Printer reported error code {error_code} "
                            f"(hex {error_code:08X}). Check the Bambu Wiki "
                            f"or printer LCD for details."
                        )
                return PrintResult(
                    success=False,
                    message=(
                        f"Print command sent for {basename} but printer "
                        f"reported a failure.{err_detail}"
                    ),
                )
            if result_state == "timeout":
                with self._state_lock:
                    current_state = str(self._last_status.get("gcode_state", "unknown")).lower()
                return PrintResult(
                    success=True,
                    message=(
                        f"Print command accepted for {basename}. Printer is "
                        f"preparing (state: {current_state}). Use printer_status() "
                        f"to monitor — print has not yet confirmed running.{warn_suffix}"
                    ),
                )
            if result_state == "running":
                return PrintResult(
                    success=True,
                    message=f"Started printing {basename}. Printer confirmed running.{warn_suffix}",
                )
            # prepare / slicing / init — accepted but not yet running
            return PrintResult(
                success=True,
                message=(
                    f"Print command accepted for {basename}. Printer is "
                    f"preparing (state: {result_state}). Use printer_status() "
                    f"to monitor — print has not yet confirmed running.{warn_suffix}"
                ),
            )

        return PrintResult(
            success=True,
            message=f"Started printing {basename}.{warn_suffix}",
        )

    def cancel_print(self) -> PrintResult:
        """Cancel the currently running print job."""
        self._stop_sent_at = time.monotonic()
        self._send_print_command("stop")
        try:
            from kiln._pro_cutter_bridge import record_command_cut

            record_command_cut(self.name, "cancel", printer_model=self.declared_printer_model() or None)
        except Exception:  # noqa: BLE001 -- cut bookkeeping never touches a stop
            logger.debug("cutter count recording failed", exc_info=True)
        return PrintResult(success=True, message="Print cancelled.")

    _ESTOP_CONFIRM_TIMEOUT_S: float = 5.0
    _ESTOP_CONFIRM_INTERVAL_S: float = 0.5

    _ESTOP_LANDED: frozenset[PrinterStatus] = frozenset(
        {PrinterStatus.ERROR, PrinterStatus.IDLE, PrinterStatus.CANCELLING}
    )

    def emergency_stop(self) -> PrintResult:
        """Stop the job, cut both heaters, send M112 -- then read back whether it landed."""
        self._stop_sent_at = time.monotonic()
        try:
            sent = self._stamp_before_send().at
        except Exception as exc:  # noqa: BLE001 — every command below is still attempted
            logger.error("Bambu emergency stop: no MQTT session before sending: %s", exc)
            sent = time.monotonic()

        commands = (
            ("stop", lambda: self._send_print_command("stop")),
            ("M104 S0 + M140 S0", lambda: self.send_gcode(["M104 S0", "M140 S0"])),
            ("M112", lambda: self.send_gcode(["M112"])),
        )
        not_sent: list[str] = []
        for label, send in commands:
            try:
                send()
            except Exception as exc:  # noqa: BLE001 — one failed publish never blocks the next
                logger.error("Bambu emergency stop: %s was NOT sent: %s", label, exc)
                not_sent.append(label)

        if len(not_sent) == len(commands):
            # Nothing left this process, so there is nothing to wait for.
            return PrintResult(
                success=False,
                message=(
                    "Emergency stop NOT sent: none of the stop commands reached the "
                    "printer's connection. Stop it at the machine now, with its own "
                    "screen or its power switch."
                ),
            )

        # Filed the moment the commands are out, before the wait below
        from kiln.auto_record_hook import note_cancel_requested

        note_cancel_requested(self)
        self._request_report_after_stop()
        return self._confirm_emergency_stop(sent, not_sent)

    def _request_report_after_stop(self) -> None:
        """Ask for one full status report, straight after the stop commands."""
        try:
            self._publish_command(
                {"pushing": {"sequence_id": self._next_seq(), "command": "pushall"}}
            )
        except Exception as exc:  # noqa: BLE001 — the read-back decides, not this request
            logger.debug("Bambu emergency stop: status request not sent: %s", exc)
            return
        with self._state_lock:
            self._last_forced_refresh = time.monotonic()

    def _estop_snapshot(self) -> tuple[float, Any, tuple[float, Any], tuple[float, Any]]:
        """When gcode_state was last carried and what it says, plus each heater target as ``(when last carried, value)``."""
        with self._state_lock:
            return (
                self._gcode_state_time,
                self._last_status.get("gcode_state"),
                (
                    self._field_seen_at.get("nozzle_target_temper", 0.0),
                    self._last_status.get("nozzle_target_temper"),
                ),
                (
                    self._field_seen_at.get("bed_target_temper", 0.0),
                    self._last_status.get("bed_target_temper"),
                ),
            )

    @staticmethod
    def _heater_cut_word(reading: tuple[float, Any], sent: float) -> str:
        """``confirmed`` only when a report after *sent* shows the target at 0."""
        seen_at, value = reading
        try:
            cut = seen_at > sent and int(float(value)) == 0
        except (TypeError, ValueError):
            cut = False
        return "confirmed" if cut else "not yet confirmed"

    def _confirm_emergency_stop(self, sent: float, not_sent: list[str]) -> PrintResult:
        """Watch the reports for the stop to land."""
        window = (
            self._ESTOP_CONFIRM_TIMEOUT_S
            if self._confirm_window_s is None
            else max(0.0, self._confirm_window_s)
        )
        unsent = f" Not sent: {', '.join(not_sent)}." if not_sent else ""
        state_at = 0.0
        status = PrinterStatus.UNKNOWN
        try:
            deadline = time.monotonic() + window
            while True:
                state_at, raw_state, nozzle, bed = self._estop_snapshot()
                word = raw_state.lower() if isinstance(raw_state, str) else "unknown"
                status = _STATE_MAP.get(word, PrinterStatus.UNKNOWN)
                if state_at > sent and status in self._ESTOP_LANDED:
                    return PrintResult(
                        success=True,
                        message=(
                            f"Emergency stop confirmed: the printer reports {status.value} "
                            f"{state_at - sent:.1f}s after the stop command. Heater targets: "
                            f"hotend 0 ({self._heater_cut_word(nozzle, sent)}), "
                            f"bed 0 ({self._heater_cut_word(bed, sent)}).{unsent}"
                        ),
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(self._ESTOP_CONFIRM_INTERVAL_S, remaining))
        except Exception:  # noqa: BLE001 — an unreadable report is an unconfirmed stop
            logger.exception("Bambu emergency stop: read-back failed")
            return PrintResult(
                success=False,
                message=(
                    "Emergency stop SENT but NOT confirmed: Kiln could not read the "
                    "printer's state back. Stop it at the machine now, with its own "
                    f"screen or its power switch.{unsent}"
                ),
            )
        if state_at > sent:
            reported = f"the printer still reports {status.value} {window:g}s later"
        else:
            reported = (
                f"the printer has not reported its state in the {window:g}s since "
                f"(its last report said {status.value})"
            )
        return PrintResult(
            success=False,
            message=(
                f"Emergency stop SENT but NOT confirmed: {reported}. Stop it at the "
                f"machine now, with its own screen or its power switch.{unsent}"
            ),
        )

    def clear_error(self) -> PrintResult:
        """Acknowledge a latched ``print_error`` so the next print can start."""
        with self._state_lock:
            subtask_id = str(self._last_status.get("subtask_id") or "0")
            try:
                print_error = int(self._last_status.get("print_error") or 0)
            except (TypeError, ValueError):
                print_error = 0

        self._publish_command(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "clean_print_error",
                    "subtask_id": subtask_id,
                    "print_error": print_error,
                }
            }
        )
        return PrintResult(
            success=True,
            message=(
                "Sent the error acknowledgement. Re-read printer_status to "
                "confirm. If the printer is still refusing prints, give it a "
                "minute — a fault from cancelling during bed levelling often "
                "clears on its own — and power-cycle only if it persists."
            ),
        )

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job."""
        self._send_print_command("pause")
        return PrintResult(success=True, message="Print paused.")

    def _resume_print_impl(self) -> PrintResult:
        """Resume a previously paused print job."""
        self._send_print_command("resume")
        return PrintResult(success=True, message="Print resumed.")

    # ------------------------------------------------------------------
    # PrinterAdapter -- calibration
    # ------------------------------------------------------------------

    # Bambu calibration option bitmask values.
    _CALIBRATION_OPTIONS: dict[str, int] = {
        "bed_leveling": 2,
        "vibration": 1,
        "flow": 4,  # xcam / first-layer inspection calibration
    }

    def run_calibration(self, *, options: list[str] | None = None) -> PrintResult:
        """Run calibration routines on the Bambu printer via MQTT."""
        if options is None:
            options = ["bed_leveling"]

        # Resolve "all" shortcut.
        if "all" in options:
            bitmask = 7
            description = "full calibration (bed leveling + vibration + flow)"
        else:
            bitmask = 0
            parts: list[str] = []
            for opt in options:
                val = self._CALIBRATION_OPTIONS.get(opt)
                if val is None:
                    valid = ", ".join(sorted(self._CALIBRATION_OPTIONS))
                    return PrintResult(
                        success=False,
                        message=(
                            f"Unknown calibration option {opt!r}. "
                            f"Valid options: {valid}, all"
                        ),
                    )
                bitmask |= val
                parts.append(opt)
            description = " + ".join(parts) + " calibration"

        self._publish_command(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "calibration",
                    "option": bitmask,
                }
            }
        )
        return PrintResult(
            success=True,
            message=(
                f"Started {description} on Bambu printer. "
                f"This takes 2-5 minutes. Use printer_status() to monitor — "
                f"printer will return to idle when complete."
            ),
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- temperature control
    # ------------------------------------------------------------------

    # : Coarse adapter-side hotend ceiling
    _MAX_HOTEND_C: float = 350.0

    def set_tool_temp(self, target: float) -> CommandVerdict:
        """Set the hotend target temperature via G-code over MQTT."""
        self._validate_temp(target, self._MAX_HOTEND_C, "Hotend")
        want = int(target)
        wire = f"M104 S{want}"
        sent = self._stamp_before_send("nozzle_target_temper")
        self.send_gcode([wire])
        return self._readback_verdict(
            f"Hotend target {want}°C",
            "nozzle_target_temper",
            lambda v: int(float(v)) == want,
            sent=sent,
            wire=wire,
        )

    def set_bed_temp(self, target: float) -> CommandVerdict:
        """Set the heated-bed target temperature via G-code over MQTT."""
        self._validate_temp(target, 130.0, "Bed")
        want = int(target)
        wire = f"M140 S{want}"
        sent = self._stamp_before_send("bed_target_temper")
        self.send_gcode([wire])
        return self._readback_verdict(
            f"Bed target {want}°C",
            "bed_target_temper",
            lambda v: int(float(v)) == want,
            sent=sent,
            wire=wire,
        )

    # ------------------------------------------------------------------
    # Bambu-specific: speed profiles
    # ------------------------------------------------------------------

    def get_speed_profile(self) -> dict[str, Any]:
        """Return the current speed profile level and name."""
        try:
            status = self._get_cached_status()
        except PrinterError:
            return {"level": None, "name": "unknown", "speed_magnitude": None}

        spd_lvl = status.get("spd_lvl")
        spd_mag = status.get("spd_mag")
        level: int | None = None
        if spd_lvl is not None:
            with contextlib.suppress(TypeError, ValueError):
                level = int(spd_lvl)
        name = _SPEED_PROFILE_NAMES.get(level, "unknown") if level else "unknown"
        return {"level": level, "name": name, "speed_magnitude": spd_mag}

    def set_speed_profile(self, profile: str) -> CommandVerdict:
        """Set the printer speed profile."""
        key = profile.strip().lower()
        if key not in _SPEED_PROFILES:
            raise PrinterError(
                f"Unknown speed profile {profile!r}. "
                f"Valid profiles: {', '.join(sorted(_SPEED_PROFILES))}"
            )
        level = _SPEED_PROFILES[key]
        sent = self._stamp_before_send("spd_lvl")
        self._publish_command(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "print_speed",
                    "param": str(level),
                }
            }
        )
        return self._readback_verdict(
            f"Speed profile {key}",
            "spd_lvl",
            lambda v: int(v) == level,
            sent=sent,
            wire=f"print_speed {level}",
        )

    def publish_print_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> CommandVerdict:
        """Publish a ``print``-category MQTT command to the printer."""
        inner: dict[str, Any] = dict(params or {})
        inner["sequence_id"] = self._next_seq()
        inner["command"] = str(command)
        self._publish_command({"print": inner})
        return CommandVerdict.accepted_only(
            f"Published print command {command!r} over MQTT. Its effect is "
            "not read back here, so it is not confirmed — read printer_status "
            "to check.",
            corroboration="none",
            sent=str(command),
        )

    # ------------------------------------------------------------------
    # Bambu-specific: skip objects mid-print
    # ------------------------------------------------------------------

    def skip_objects(self, object_ids: list[int]) -> CommandVerdict:
        """Abandon one or more plate objects during a live multi-object print."""
        if not object_ids:
            raise PrinterError("skip_objects requires at least one object id.")
        try:
            ids = [int(x) for x in object_ids]
        except (TypeError, ValueError) as exc:
            raise PrinterError(f"skip_objects: object ids must be integers ({exc}).") from exc
        sent = self._stamp_before_send("s_obj")
        self._publish_command(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "skip_objects",
                    "obj_list": ids,
                }
            }
        )

        def _all_skipped(reported: Any) -> bool:
            if not isinstance(reported, list):
                return False
            seen = {int(x) for x in reported if isinstance(x, (int, str)) and str(x).isdigit()}
            return set(ids) <= seen

        return self._readback_verdict(
            f"Skip object(s) {', '.join(str(i) for i in ids)}",
            "s_obj",
            _all_skipped,
            sent=sent,
            wire=f"skip_objects {ids}",
        )

    # ------------------------------------------------------------------
    # Bambu-specific: LED control
    # ------------------------------------------------------------------

    def set_light(self, node: str, mode: str) -> CommandVerdict:
        """Control the printer's LED lights."""
        node_lower = node.strip().lower()
        mode_lower = mode.strip().lower()
        if node_lower not in _VALID_LED_NODES:
            raise PrinterError(
                f"Unknown LED node {node!r}. Valid nodes: {', '.join(sorted(_VALID_LED_NODES))}"
            )
        if mode_lower not in _VALID_LED_MODES:
            raise PrinterError(
                f"Unknown LED mode {mode!r}. Valid modes: {', '.join(sorted(_VALID_LED_MODES))}"
            )
        sent = self._stamp_before_send("lights_report")
        self._publish_command(
            {
                "system": {
                    "sequence_id": self._next_seq(),
                    "command": "ledctrl",
                    "led_node": node_lower,
                    "led_mode": mode_lower,
                }
            }
        )

        def _light_matches(report: Any) -> bool:
            if not isinstance(report, list):
                return False
            return any(
                isinstance(e, dict)
                and str(e.get("node", "")).lower() == node_lower
                and str(e.get("mode", "")).lower() == mode_lower
                for e in report
            )

        return self._readback_verdict(
            f"{node_lower} {mode_lower}",
            "lights_report",
            _light_matches,
            sent=sent,
            wire=f"ledctrl {node_lower}={mode_lower}",
        )

    # ------------------------------------------------------------------
    # Bambu-specific: fan control
    # ------------------------------------------------------------------

    def set_fan(self, node: str, percent: int) -> CommandVerdict:
        """Set the speed of one of the printer's fans."""
        key = node.strip().lower()
        index = _FAN_NODE_TO_INDEX.get(key)
        if index is None:
            valid = ", ".join(sorted(set(_FAN_NODE_TO_INDEX)))
            raise PrinterError(f"Unknown fan node {node!r}. Valid nodes: {valid}")
        try:
            pct = int(percent)
        except (TypeError, ValueError) as exc:
            raise PrinterError(f"set_fan: percent must be an integer 0-100 ({exc}).") from exc
        if not 0 <= pct <= 100:
            raise PrinterError(f"set_fan: percent must be 0-100, got {pct}.")
        speed = round(pct / 100 * 255)
        wire = f"M106 P{index} S{speed}"
        sent = self._stamp_before_send(_FAN_INDEX_TO_FIELD[index])
        self.send_gcode([wire])
        want_level = round(speed / 255 * 15)
        return self._readback_verdict(
            f"{node.strip().lower()} fan {pct}%",
            _FAN_INDEX_TO_FIELD[index],
            lambda v: abs(int(float(v)) - want_level) <= 1,
            sent=sent,
            wire=wire,
            window_s=_FAN_CONFIRM_WINDOW_S,
        )

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    def _peek_loaded_ams_trays(self) -> list[dict[str, Any]] | None:
        """Return loaded AMS trays from cached MQTT status."""
        try:
            status = self._get_cached_status()
        except Exception:
            return None

        ams_data = status.get("ams")
        if isinstance(ams_data, dict):
            ams_data = ams_data.get("ams")
        if not isinstance(ams_data, list):
            return None

        return [
            {"slot": slot, "unit": unit_id, "tray_id": tid, "name": name,
             "tray_type": tray.get("tray_type", ""), "tray_color": tray.get("tray_color", "")}
            for unit_id, slot, tid, name, tray in _raw_ams_trays(ams_data)
            if tray.get("tray_type")
        ]

    def active_filament_color(self) -> str | None:
        """The ``#RRGGBB`` of the filament actually loaded, or ``None``."""
        try:
            with self._state_lock:
                status = dict(self._last_status or {})
        except Exception:  # noqa: BLE001 — telemetry never blocks a wrap
            logger.debug("AMS colour unavailable", exc_info=True)
            return None
        if not status:
            return None

        ams_data = status.get("ams")
        if isinstance(ams_data, dict):
            ams_data = ams_data.get("ams")
        if not isinstance(ams_data, list):
            return None

        trays: list[dict[str, Any]] = [
            {"slot": tid, "tray_color": tray.get("tray_color", "")}
            for _unit, _slot, tid, _name, tray in _raw_ams_trays(ams_data)
            if tray.get("tray_type")
        ]
        if not trays:
            return None

        active: int | None = None
        ref, _source = _feeding_ref(status)
        if ref is not None:
            if not ref.loaded_tray:
                return None
            active = ref.tray_id

        chosen: dict[str, Any] | None = None
        if active is not None:
            chosen = next(
                (t for t in trays if t.get("slot") == active), None
            )
        if chosen is None:
            # No active tray named
            if len(trays) != 1:
                return None
            chosen = trays[0]

        # Bambu reports RRGGBBAA; the alpha is not ours to carry.
        value = str(chosen.get("tray_color") or "").strip().lstrip("#")
        if len(value) < 6:
            return None
        try:
            int(value[:6], 16)
        except ValueError:
            return None
        return "#" + value[:6].upper()

    def get_ams_status(self) -> dict[str, Any]:
        """Query AMS status: what's loaded in each tray."""
        status = self._get_cached_status()
        ams_data = status.get("ams")

        if not ams_data:
            self._publish_command(
                {"pushing": {"sequence_id": self._next_seq(), "command": "pushall"}}
            )
            time.sleep(min(2.0, self._timeout / 2))
            status = self._get_cached_status()
            ams_data = status.get("ams")

        ams_wrapper: dict[str, Any] = {}
        if isinstance(ams_data, dict):
            ams_wrapper = ams_data
            ams_data = ams_data.get("ams")

        result: dict[str, Any] = {
            "ams_exist_bits": (
                ams_wrapper.get("ams_exist_bits")
                or status.get("ams_exist_bits", "0")
            ),
            "tray_exist_bits": (
                ams_wrapper.get("tray_exist_bits")
                or status.get("tray_exist_bits", "0")
            ),
            "tray_now": (
                ams_wrapper.get("tray_now")
                or status.get("tray_now", "255")
            ),
            "units": [],
        }
        for key in (
            "tray_pre",
            "tray_tar",
            "tray_read_done_bits",
            "tray_reading_bits",
            "tray_is_bbl_bits",
            "version",
        ):
            if key in ams_wrapper:
                result[key] = ams_wrapper[key]
            elif key in status:
                result[key] = status[key]
        ref, source = _feeding_ref(status)
        result["feeding"] = _feeding_record(ref, source)
        result["feeding_source"] = source

        if not isinstance(ams_data, list):
            return result

        humidity_known = self._printer_model not in ("bambu_a1", "bambu_a1_mini")

        if not self._fw_modules and not self._fw_modules_requested:
            self._fw_modules_requested = True
            with contextlib.suppress(Exception):
                self._publish_command(
                    {"info": {"sequence_id": self._next_seq(), "command": "get_version"}}
                )
                time.sleep(min(1.5, self._timeout / 2))
        module_by_unit: dict[int, str] = {}
        for mod in self._fw_modules or []:
            if not isinstance(mod, dict):
                continue
            name = str(mod.get("name", ""))
            if _is_accessory_module(name):
                module_by_unit[int(name.partition("/")[2])] = name

        for unit in ams_data:
            if not isinstance(unit, dict):
                continue
            unit_id = unit.get("id", 0)
            humidity = unit.get("humidity")
            humidity_int: int | None = None
            if humidity is not None:
                with contextlib.suppress(TypeError, ValueError):
                    humidity_int = int(humidity)
            humidity_raw_int: int | None = None
            raw_h = unit.get("humidity_raw")
            if raw_h is not None:
                with contextlib.suppress(TypeError, ValueError):
                    humidity_raw_int = int(raw_h)
            dry_time_int: int | None = None
            raw_dt = unit.get("dry_time")
            if raw_dt is not None:
                with contextlib.suppress(TypeError, ValueError):
                    dry_time_int = int(raw_dt)
            dry_setting = unit.get("dry_setting")

            trays: list[dict[str, Any]] = []
            raw_trays = unit.get("tray")
            if isinstance(raw_trays, list):
                for tray in raw_trays:
                    if not isinstance(tray, dict):
                        continue
                    slot_id = tray.get("id", 0)
                    remain = tray.get("remain")
                    remain_int: int | None = None
                    if remain is not None:
                        with contextlib.suppress(TypeError, ValueError):
                            remain_int = int(remain)
                    nozzle_min: int | None = None
                    nozzle_max: int | None = None
                    bed_t: int | None = None
                    raw_min = tray.get("nozzle_temp_min")
                    raw_max = tray.get("nozzle_temp_max")
                    raw_bed = tray.get("bed_temp")
                    if raw_min is not None:
                        with contextlib.suppress(TypeError, ValueError):
                            nozzle_min = int(raw_min)
                    if raw_max is not None:
                        with contextlib.suppress(TypeError, ValueError):
                            nozzle_max = int(raw_max)
                    if raw_bed is not None:
                        with contextlib.suppress(TypeError, ValueError):
                            bed_t = int(raw_bed)
                    tag_uid = str(tray.get("tag_uid") or "").strip()
                    remaining_known = bool(tag_uid) and set(tag_uid) != {"0"}
                    entry: dict[str, Any] = {
                        "slot": slot_id,
                        "tray_type": tray.get("tray_type", ""),
                        "tray_color": tray.get("tray_color", ""),
                        "remain": remain_int,
                        "remaining_known": remaining_known,
                        "tag_uid": tag_uid,
                        "nozzle_temp_min": nozzle_min,
                        "nozzle_temp_max": nozzle_max,
                        "bed_temp": bed_t,
                    }
                    with contextlib.suppress(TypeError, ValueError):
                        entry["tray_id"] = _bambu_tray_id(int(unit_id), int(slot_id))
                        entry["name"] = _bambu_tray_name(int(unit_id), int(slot_id))
                    trays.append(entry)

            unit_out: dict[str, Any] = {
                "unit_id": unit_id,
                "humidity": humidity_int,
                "humidity_known": humidity_known,
                "trays": trays,
            }
            with contextlib.suppress(TypeError, ValueError):
                unit_out["name"] = _bambu_unit_name(int(unit_id))
            if humidity_raw_int is not None:
                unit_out["humidity_raw"] = humidity_raw_int
            if dry_time_int is not None:
                unit_out["dry_time"] = dry_time_int
            if isinstance(dry_setting, dict):
                unit_out["dry_setting"] = dry_setting
            if module_by_unit:
                with contextlib.suppress(TypeError, ValueError):
                    mn = module_by_unit.get(int(unit_id))
                    if mn:
                        unit_out["module_name"] = mn
            result["units"].append(unit_out)

        return result

    def get_multi_material_status(self) -> Any:
        """The AMS as a :class:`~kiln.multi_material.MultiMaterialStatus`."""
        from kiln.multi_material import from_bambu_ams

        return from_bambu_ams(self.get_ams_status(), printer_model=self._printer_model)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    _BAMBU_EXTERNAL_SPOOL_TRAY: int = _EXTERNAL_SPOOL_TRAY
    _BAMBU_NO_TRAY: int = _NO_TRAY
    _FILAMENT_LOAD_WAIT_S: float = 120.0
    _FILAMENT_UNLOAD_WAIT_S: float = 90.0
    _FILAMENT_PURGE_WATCH_S: float = 10.0
    _FILAMENT_WIPE_WATCH_S: float = 45.0


    _SERVED_LINE = (
        "The motion sequence for {model} is served one plan at a time to a signed-in Kiln, "
        "and no plan answered on this install. Sign in, or reconnect to the internet, and try again."
    )
    _JOG_REMEDY = (
        "Use the printer's own screen's jog controls instead, Z UP first, then X and Y, with your "
        "eyes on the plate (the screen's Home button descends the nozzle to the bed and is the "
        "wrong tool with a part on the plate)"
    )
    _WIPE_REMEDY = "Wipe from the printer's own screen, or start a print (its start sequence wipes on the pad)"
    _MOTION_WORDS = {
        "home": ("Homing moves {model}'s head and presses the nozzle onto the plate",
                 "get {model}'s own homing sequence", "won't home {model}"),
        "park": ("Parking moves {model}'s head across the plate to its chute",
                 "get {model}'s own park sequence", "won't park {model}"),
        "wipe": ("A wipe runs {model}'s hot nozzle across its wipe pad",
                 "get {model}'s own wipe sequence", "won't wipe {model}"),
        "purge": ("A purge runs where {model}'s head is unless it parks over the chute first",
                  "get {model}'s own park sequence", "purges in place"),
    }
    _PLAN_MEMO_S: float = 120.0

    def _plan_for(self, verb: str, *, axes: str = "XYZ", on_plate_ok: bool = False) -> dict[str, Any] | None:
        """The plan document for *verb* on this machine, or ``None`` -- memoised briefly."""
        from kiln import _pro_motion_bridge as _bridge

        memo = getattr(self, "_plan_memo", None)
        if memo is None:
            memo = self._plan_memo = {}
        misses = getattr(self, "_plan_misses", None)
        if misses is None:
            misses = self._plan_misses = {}
        key = (verb, axes, bool(on_plate_ok), str(getattr(self, "_printer_model", "") or ""))
        hit = memo.get(key)
        now = time.monotonic()
        if hit is not None:
            ttl = self._PLAN_MEMO_S if hit[1] is not None else 5.0
            if now - hit[0] < ttl:
                return hit[1]
        doc = _bridge.plan_for(self, verb, axes=axes, on_plate_ok=on_plate_ok)
        memo[key] = (now, doc)
        if doc is None:
            try:
                misses[verb] = _bridge.miss_for(self, verb, axes=axes, on_plate_ok=on_plate_ok)
            except Exception:  # noqa: BLE001 -- the why is a courtesy; the refusal stands without it
                misses[verb] = None
        else:
            misses.pop(verb, None)
        return doc

    def _plan_miss(self, verb: str) -> Any | None:
        """Why the last ask for *verb* had no plan, or ``None``."""
        return (getattr(self, "_plan_misses", None) or {}).get(verb)

    def _model_name(self) -> str:
        return self._printer_model or "this printer (no printer_model declared in config.yaml)"

    def _no_plan_sentence(self, verb: str, miss: Any, *, safe_remedy: str = "") -> str:
        """The one served-door sentence for *verb* with no plan: what is on the line, why Kiln can't plan it right now, what it won't do, and the safe way plus the fix."""
        from kiln.served_answer import sentence

        model = self._model_name()
        on_the_line, cannot, wont = self._MOTION_WORDS[verb]
        return sentence(
            miss, feature="servers",
            on_the_line=on_the_line.format(model=model), cannot=cannot.format(model=model),
            wont=wont.format(model=model), safe_remedy=safe_remedy,
        )

    def _no_plan_clause(self, verb: str, miss: Any, *, then: str = "try again") -> str:
        """The same why as a clause after "because", for an answer that has its own sentence (the purge's placement line)."""
        from kiln.served_answer import clause

        model = self._model_name()
        _on_the_line, cannot, _wont = self._MOTION_WORDS[verb]
        return clause(miss, feature="servers", cannot=cannot.format(model=model), then=then)

    def _served_refusal(self, verb: str, doc: dict[str, Any] | None, *, safe_remedy: str | None = None) -> str:
        """The refusal for *verb* when no plan, or a plan that says no, answered."""
        model = self._model_name()
        remedy = self._JOG_REMEDY if safe_remedy is None else safe_remedy
        refusal = (doc or {}).get("refusal") if isinstance(doc, dict) else None
        reason = (refusal or {}).get("message") if isinstance(refusal, dict) else None
        if reason:
            return f"Kiln will not {verb} {model}: {reason.rstrip('. ')}." + (f" {remedy}." if remedy else "")
        miss = self._plan_miss(verb)
        if miss is None:
            return f"Kiln will not {verb} {model}: {self._SERVED_LINE.format(model=model)}" + (f" {remedy}." if remedy else "")
        return self._no_plan_sentence(verb, miss, safe_remedy=remedy)

    def _unsupported(self, exc_cls: type, verb: str, doc: dict[str, Any] | None, *,
                     safe_remedy: str | None = None, tail: str = "") -> Exception:
        """*exc_cls* carrying the refusal sentence, with why beside it for the door's envelope (``why_fields``)."""
        from kiln.served_answer import fields

        exc = exc_cls(self._served_refusal(verb, doc, safe_remedy=safe_remedy) + tail)
        exc.why_fields = fields(self._plan_miss(verb)) if doc is None else {}
        return exc

    def _station_supports(self, station: dict[str, Any] | None, capability: str) -> tuple[bool, str]:
        """Whether this install may drive *capability* on this model, and why not."""
        from kiln import _pro_motion_bridge as _bridge

        answer = _bridge.station_supports(self, station, capability)
        if answer is not None:
            return bool(answer[0]), str(answer[1])
        model = str(getattr(self, "_printer_model", "") or "").strip().lower()
        if not model:
            return super()._station_supports(None, capability)
        verb, axes, consent = {
            "purge": ("purge", "XY", False),
            "wipe": ("wipe", "XY", False),
            "park": ("park", "XY", False),
            "home_z": ("home", "XYZ", False),
            "home_z_on_plate": ("home", "XYZ", True),
        }.get(capability, ("home", "XYZ", False))
        doc = self._plan_for(verb, axes=axes, on_plate_ok=consent)
        if doc is None:
            miss = self._plan_miss(verb)
            if miss is None:
                return False, self._SERVED_LINE.format(model=model)
            if verb == "purge":
                return False, self._no_plan_clause("purge", miss, then="the next purge parks first")
            return False, self._no_plan_sentence(verb, miss, safe_remedy=self._WIPE_REMEDY if verb == "wipe" else self._JOG_REMEDY)
        refusal = doc.get("refusal") if isinstance(doc.get("refusal"), dict) else {}
        if capability == "home_z_on_plate":
            return (bool(doc.get("ok")) and bool(doc.get("z_home_on_plate"))), (
                "" if doc.get("ok") else str(refusal.get("message") or self._SERVED_LINE.format(model=model)))
        if doc.get("ok"):
            return True, ""
        if capability == "home_z" and refusal.get("code") == "PLATE_CLEAR_REQUIRED":
            return False, (f"{model} homes Z by pressing the nozzle onto the plate; that runs only "
                           "with plate_clear on the call, after a person has looked")
        return False, str(refusal.get("message") or self._SERVED_LINE.format(model=model))

    def _purge_placement(self, plan: FilamentOpPlan) -> dict[str, Any]:
        """Where a purge is about to go, from the plan rather than a local record."""
        if plan.printer_paused:
            return super()._purge_placement(plan)
        model = str(getattr(self, "_printer_model", "") or "").strip().lower()
        doc = self._plan_for("purge", axes="XY")
        base = {"status": "in_place", "printer_id": model or None, "position": self._reported_position(), "wiped": None}
        if doc is None:
            miss = self._plan_miss("purge") if model else None
            if miss is not None:
                from kiln.served_answer import fields

                return {**base, "reason": self._no_plan_clause("purge", miss, then="the next purge parks first"),
                        **fields(miss)}
            return {**base, "reason": (self._SERVED_LINE.format(model=model) if model else
                                       "no printer_model is declared in config.yaml, so Kiln cannot ask for a plan").rstrip(". ")}
        if not doc.get("ok"):
            refusal = doc.get("refusal") if isinstance(doc.get("refusal"), dict) else {}
            return {**base, "reason": str(refusal.get("message") or self._SERVED_LINE.format(model=model)).rstrip(". ")}
        plate, clearance, blocked = self._plate_raise_block(doc.get("raise_clearance_mm"))
        if blocked:
            assert clearance is not None
            return {**base, "plate": plate.to_dict(), "reason": (
                f"{plate.describe()}, taller than the {clearance:g} mm raise the travel to "
                "the chute starts with, so the head stayed where it is; clear the plate and "
                f"run `{kiln_command('plate clear')}` to park over the chute again")}
        placement = dict(doc.get("placement") or {})
        placement.setdefault("status", "parked")
        placement.setdefault("printer_id", model)
        return placement

    def _park_for_firmware_routine(self, plan: FilamentOpPlan) -> dict[str, Any]:
        """Park over the chute before the firmware's own change-filament routine."""
        placement = self._purge_placement(plan)
        if placement.get("status") != "parked":
            return placement
        doc = self._plan_for("purge", axes="XY") or {}
        script = doc.get("park_gcode")
        if not isinstance(script, list) or not script:
            return {**placement, "status": "in_place", "wiped": None,
                    "reason": "the served plan carries no park for the firmware's own routine"}
        self.send_gcode([str(line) for line in script])
        placement["after"] = (
            "the firmware's own routine then ran with the head there, so its "
            "purge fell into the chute"
        )
        placement["pre_gcode"] = list(script)
        if isinstance(doc.get("finish"), dict):
            placement["finish"] = dict(doc["finish"])  # the routine leaves the nozzle hot; cool it the machine's way
        return placement

    @staticmethod
    def _tray_now_of(status: dict[str, Any]) -> Any:
        """The feeding tray as the printer's own id, read the one way."""
        return _tray_now_of(status)

    def _ams_tray(self, slot: int) -> dict[str, Any] | None:
        """The tray dict ``get_ams_status`` reports for the printer's tray id *slot*."""
        ref = _read_tray_id(slot)
        if ref is None or not ref.loaded_tray:
            return None
        try:
            status = self.get_ams_status()
        except PrinterError:
            return None
        for unit in status.get("units", []):
            if int(unit.get("unit_id", 0)) != ref.unit:
                continue
            for tray in unit.get("trays", []):
                if int(tray.get("slot", -1)) == ref.slot:
                    return tray
        return None

    def _filament_material_window(
        self, material: str | None, slot: int | None
    ) -> tuple[float, float, str] | None:
        """The AMS tray's own ``nozzle_temp_min`` / ``max`` when it has one."""
        tray_slot = slot
        if tray_slot is None:
            with self._state_lock:
                raw_now = self._tray_now_of(self._last_status)
            with contextlib.suppress(TypeError, ValueError):
                now = int(raw_now)
                if now not in (self._BAMBU_EXTERNAL_SPOOL_TRAY, self._BAMBU_NO_TRAY):
                    tray_slot = now
        if tray_slot is not None:
            tray = self._ams_tray(tray_slot)
            if tray is not None:
                lo, hi = tray.get("nozzle_temp_min"), tray.get("nozzle_temp_max")
                if lo is not None and hi is not None and float(hi) >= float(lo) > 0:
                    return (
                        float(lo),
                        float(hi),
                        f"the AMS {_describe_tray_id(tray_slot)} report ({tray.get('tray_type') or 'unknown type'})",
                    )
                if not material and tray.get("tray_type"):
                    material = str(tray["tray_type"])
        return super()._filament_material_window(material, slot)

    def _bambu_fault_codes(self, status: dict[str, Any]) -> set[tuple[str, str]]:
        """Every fault the cached status carries, as ``(code, kind)`` pairs."""
        codes: set[tuple[str, str]] = set()
        raw = status.get("print_error")
        with contextlib.suppress(TypeError, ValueError):
            val = int(raw or 0)
            if val:
                codes.add((_hms_group(f"{val:08X}"), "print_error"))
        hms = status.get("hms")
        if isinstance(hms, list):
            for entry in hms:
                if not isinstance(entry, dict):
                    continue
                with contextlib.suppress(TypeError, ValueError):
                    attr = int(entry.get("attr") or 0)
                    code = int(entry.get("code") or 0)
                    if attr or code:
                        codes.add((_hms_group(f"{attr:08X}{code:08X}"), "hms"))
        return codes

    def _snapshot_faults(self) -> set[tuple[str, str]]:
        with self._state_lock:
            return self._bambu_fault_codes(dict(self._last_status))

    def _fault_result(
        self, plan: FilamentOpPlan, codes: set[tuple[str, str]], *, stage: str
    ) -> FilamentOpResult:
        """Report the fault the printer raised, in its own words."""
        ordered = sorted(codes, key=lambda pair: (pair[1] != "hms", pair[0]))
        code, kind = ordered[0]
        fault = read_bambu_fault(code, kind=kind)
        hint = fault.reading
        details: dict[str, Any] = {
            "all_new_faults": [
                {"code": c, "kind": k} for c, k in ordered
            ],
            "code_kind": kind,
            "screen_code": code.replace("_", "-"),
        }
        message = f"The printer raised {code} during the {stage}: {hint}"
        if fault.remedy:
            details["remedy"] = fault.remedy
            message = f"{message} {fault.remedy}"
        found = lookup_screen_text(
            code, device_type=device_type_from_serial(self._serial), kind=kind
        )
        if found is not None:
            details["screen_text"], details["source"] = found
        return FilamentOpResult(
            success=False,
            action=plan.action,
            message=message,
            extrusion_verified=False,
            verification_source="bambu_fault_code",
            error_code=code,
            error_hint=hint,
            slot=plan.slot,
            material=plan.material,
            temperature=plan.temperature,
            details=details,
        )

    def _watch_tray_change(
        self,
        plan: FilamentOpPlan,
        *,
        expect_tray: int,
        faults_before: set[str],
        wait_seconds: float,
        stage: str,
    ) -> FilamentOpResult:
        """Watch the push cache until the AMS reports the change or a fault."""
        deadline = time.monotonic() + wait_seconds
        last_now: Any = None
        firmware_target: float | None = None
        while True:
            with self._state_lock:
                status = dict(self._last_status)
            with contextlib.suppress(TypeError, ValueError):
                seen = float(status.get("nozzle_target_temper"))
                if seen > 0 and (firmware_target is None or seen > firmware_target):
                    firmware_target = seen
            override = (
                f" The firmware ran its routine at {firmware_target:g}°C — its own "
                f"flush temperature — not the {plan.temperature:g}°C Kiln validated; "
                "the temperature gate binds what Kiln sends, and the firmware's "
                "change-filament routine overrides it."
                if firmware_target is not None and firmware_target > plan.temperature
                else ""
            )
            extra = {"firmware_hotend_target_c": firmware_target}
            new_faults = self._bambu_fault_codes(status) - faults_before
            if new_faults:
                result = self._fault_result(plan, new_faults, stage=stage)
                result.details.update(extra)
                return result
            raw_now = self._tray_now_of(status)
            with contextlib.suppress(TypeError, ValueError):
                last_now = int(raw_now)
                if last_now == expect_tray:
                    what = (
                        "no tray is feeding the nozzle"
                        if expect_tray == self._BAMBU_NO_TRAY
                        else f"{_describe_tray_id(expect_tray)} is feeding the nozzle"
                    )
                    return FilamentOpResult(
                        success=True,
                        action=plan.action,
                        message=(
                            f"{plan.action.capitalize()} complete: the AMS reports "
                            f"{what} and no fault was raised.{override}"
                        ),
                        extrusion_verified=True,
                        verification_source="ams_tray_now",
                        slot=plan.slot,
                        material=plan.material,
                        temperature=plan.temperature,
                        details={"tray_now": last_now, **extra},
                    )
            if time.monotonic() >= deadline:
                next_read = (
                    f"ams_status: tray_now == {expect_tray} means the "
                    f"{plan.action} finished; printer_status: a non-zero "
                    "print_error is the fault it raised."
                )
                return FilamentOpResult(
                    success=False,
                    action=plan.action,
                    message=(
                        f"The {plan.action} command was sent, but within "
                        f"{wait_seconds:g}s the AMS never reported "
                        f"tray_now={expect_tray} (last seen {last_now!r}) and "
                        "raised no fault. The printer is most likely still "
                        "working through its routine, or it is waiting on the "
                        f"screen. Do not send the {plan.action} again: read "
                        f"{next_read}{override}"
                    ),
                    extrusion_verified=None,
                    verification_source="timeout_no_signal",
                    slot=plan.slot,
                    material=plan.material,
                    temperature=plan.temperature,
                    details={
                        "tray_now": last_now,
                        "waited_seconds": wait_seconds,
                        "next_read": next_read,
                        **extra,
                    },
                )
            time.sleep(1.0)

    def _ams_change_filament(self, target: int, plan: FilamentOpPlan) -> None:
        self._filament_command_sent_at = time.monotonic()
        with self._state_lock:
            raw_cur = self._last_status.get("nozzle_target_temper")
        curr_temp = int(plan.temperature)
        with contextlib.suppress(TypeError, ValueError):
            if raw_cur is not None and int(float(raw_cur)) > 0:
                curr_temp = int(float(raw_cur))
        self._publish_command(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "ams_change_filament",
                    "target": int(target),
                    "curr_temp": curr_temp,
                    "tar_temp": int(plan.temperature),
                }
            }
        )

    def _load_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Feed an AMS tray (or the external spool) to the nozzle."""
        ref = _read_tray_id(plan.slot) if plan.slot is not None else None
        if plan.slot is None or (ref is not None and ref.external):
            target = self._BAMBU_EXTERNAL_SPOOL_TRAY
        elif ref is None or not ref.loaded_tray:
            raise PrinterError(
                f"{_describe_tray_id(plan.slot)} is not a tray this printer can load: "
                "a chained unit's trays are 0-15 (unit B starts at 4), an AMS HT is "
                "128-135, and 254 is the external spool. Use ams_status to see which "
                "trays exist, or unload_filament to unload."
            )
        else:
            target = ref.tray_id
            tray = self._ams_tray(target)
            if tray is None:
                raise PrinterError(
                    f"AMS {_describe_tray_id(target)} is not present in the AMS report. "
                    "Use ams_status to see which trays exist."
                )
            if not tray.get("tray_type"):
                raise PrinterError(
                    f"AMS {_describe_tray_id(target)} reports no filament. Put a spool in it "
                    "(or pick a tray that has one) and try again."
                )
        faults_before = self._snapshot_faults()
        placement = self._park_for_firmware_routine(plan)
        self._ams_change_filament(target, plan)
        wait = plan.wait_seconds(self._FILAMENT_LOAD_WAIT_S)
        result = self._watch_tray_change(
            plan,
            expect_tray=target,
            faults_before=faults_before,
            wait_seconds=wait,
            stage="load (the firmware's own feed-and-purge routine)",
        )
        return self._say_where(result, placement)

    def _unload_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        faults_before = self._snapshot_faults()
        placement = self._park_for_firmware_routine(plan)
        self._ams_change_filament(self._BAMBU_NO_TRAY, plan)
        wait = plan.wait_seconds(self._FILAMENT_UNLOAD_WAIT_S)
        result = self._watch_tray_change(
            plan,
            expect_tray=self._BAMBU_NO_TRAY,
            faults_before=faults_before,
            wait_seconds=wait,
            stage="unload",
        )
        return self._say_where(result, placement)

    def _say_where(self, result: FilamentOpResult, placement: dict[str, Any]) -> FilamentOpResult:
        """Every answer says where the head was when the plastic moved."""
        placement = dict(placement)
        finish = placement.pop("finish", None)
        if isinstance(finish, dict) and finish:
            result.details["finish"] = finish
        result.details["purge_station"] = placement
        result.message = f"{result.message} {self._placement_sentence(placement)}"
        return result

    def _watch_for_purge_fault(
        self, plan: FilamentOpPlan, faults_before: set[tuple[str, str]], *, watch: float, stage: str
    ) -> FilamentOpResult | None:
        """A fault raised inside *watch* seconds, as a result, or ``None``."""
        deadline = time.monotonic() + watch
        while True:
            new_faults = self._snapshot_faults() - faults_before
            if new_faults:
                return self._fault_result(plan, new_faults, stage=stage)
            if time.monotonic() >= deadline:
                return None
            time.sleep(1.0)

    def _purge_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Clear the nozzle over the chute, watching for a fault."""
        faults_before = self._snapshot_faults()
        placement = self._purge_placement(plan)
        pre: list[str] | None = None
        post: list[str] | None = None
        finish: dict[str, Any] | None = None
        if placement.get("status") == "parked":
            doc = self._plan_for("purge", axes="XY") or {}
            pre = [str(x) for x in (doc.get("pre_gcode") or [])] or None
            post = [str(x) for x in (doc.get("post_gcode") or [])] or None
            finish = doc.get("finish") if isinstance(doc.get("finish"), dict) else None
            if doc.get("after"):
                placement["after"] = str(doc["after"])
            if pre is None:
                placement = {**placement, "status": "in_place", "wiped": None,
                             "reason": "the served plan answered no script for the chute"}
                post = None
        result = self._gcode_filament_move(
            plan,
            signed_length_mm=float(plan.length_mm or DEFAULT_PURGE_LENGTH_MM),
            mechanism="bambu_mqtt_gcode_line",
            pre_gcode=pre,
            post_gcode=post,
            placement=placement,
        )
        from kiln.printers.motion_plan import note_motion_outcome

        ran_plan = pre is not None and placement.get("status") == "parked"
        if not result.success:
            if ran_plan and result.verification_source == "firmware_rejected_move":
                note_motion_outcome(self, "purge", "fault", "rejected")
            return result
        if finish:
            result.details["finish"] = dict(finish)
        watch = plan.wait_seconds(self._FILAMENT_PURGE_WATCH_S)
        fault = self._watch_for_purge_fault(plan, faults_before, watch=watch, stage="purge")
        if fault is not None:
            fault.details["purge_station"] = placement
            if ran_plan:
                note_motion_outcome(self, "purge", "fault", fault.error_code)
            return fault
        if ran_plan:
            note_motion_outcome(self, "purge", "full_run")
        result.verification_source = "no_fault_within_window"
        result.message = (
            f"Hotend at {result.details.get('hotend_reading')}°C; the printer "
            f"accepted a {plan.length_mm:g} mm purge and raised no extrusion "
            f"fault in the {watch:g}s after it. Bambu reports no flow sensor, "
            "so 'no fault' is the strongest thing the printer can say — look "
            f"at the nozzle to confirm a clean stream. {self._placement_sentence(placement)}"
        )
        result.details["fault_watch_seconds"] = watch
        return result

    def _wipe_nozzle_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Run the model's own wipe-pad pass from its plan, or refuse with the reason."""
        if plan.printer_paused:
            return FilamentOpResult(
                success=False,
                action=plan.action,
                message=(
                    "Not wiping: the print is paused and the head is parked over "
                    "the part. Kiln does not travel mid-print; finish or cancel "
                    "the print first, or wipe from the printer's own screen."
                ),
                extrusion_verified=False,
                verification_source="refused_paused",
                slot=plan.slot,
                material=plan.material,
                temperature=plan.temperature,
                details={"purge_station": self._purge_placement(plan)},
            )
        from kiln.printers import motion_plan as _exec

        doc = self._plan_for("wipe", axes="XY")
        if doc is None or not doc.get("ok"):
            _exec.note_motion_outcome(self, "wipe", "refused", "no_plan")
            raise self._unsupported(FilamentHandlingUnsupported, "wipe", doc, safe_remedy=self._WIPE_REMEDY)
        step = plan.options.get("step")
        plan_only = bool(plan.options.get("plan_only"))
        steps = _exec.wipe_steps_of(doc)
        if (step is not None or plan_only) and not steps:
            raise PrinterError(
                f"The wipe plan for {self._model_name()} describes no steps, so it runs whole or not at all; "
                "call again without step / plan_only."
            )
        only = doc.get("step_mode_only") if isinstance(doc.get("step_mode_only"), dict) else None
        if only and step is None and not plan_only:
            _exec.note_motion_outcome(self, "wipe", "refused", "step_mode_only")
            raise FilamentHandlingUnsupported(
                f"Kiln will not run the wipe on {self._model_name()} in one go: "
                f"{str(only.get('reason') or 'the plan runs in step mode only').rstrip('. ')}. "
                "Until then, wipe from the printer's own screen -- its wizard parks first -- or start a print; "
                "its start sequence wipes on the pad."
            )
        contact = (doc.get("details") or {}).get("needs_plate_clear")
        touches = bool(doc.get("z_home_on_plate")) or any(s_.get("touches_plate") for s_ in steps)
        self._plate_gate(
            plan.options, clearance_mm=doc.get("raise_clearance_mm"), action="wipe", allow_plan=False,
            touches_plate=touches, contact=contact if isinstance(contact, str) else None,
            fallback=("Until then, wipe from the printer's own screen -- its wizard parks first -- "
                      "or start a print; its start sequence wipes on the pad."),
        )
        if plan_only:
            return _exec.wipe_plan_only(doc, plan, steps)
        if step is not None:
            result = _exec.run_wipe_step(self, doc, plan, steps, int(step))
            if result.success and result.next_step is None:
                result.details["plate_clear_given"] = touches and plan.options.get("plate_clear") is True
            return result
        faults_before = self._snapshot_faults()
        placement = dict(doc.get("placement") or {"status": "parked", "printer_id": self._printer_model})
        try:
            retract_mm = float(doc.get("end_retract_mm") or 0.0)
            retract_feed = float(doc.get("retract_feed_mm_min") or FILAMENT_FEED_RATE_MM_MIN)
        except (TypeError, ValueError):
            retract_mm, retract_feed = 0.0, FILAMENT_FEED_RATE_MM_MIN
        pre = [str(x) for x in (doc.get("pre_gcode") or [])]
        post = [str(x) for x in (doc.get("post_gcode") or [])]
        if not pre or not post or retract_mm <= 0:
            raise FilamentHandlingUnsupported(
                f"Kiln will not wipe {self._model_name()}: the plan that answered carries no pad pass. "
                "Wipe from the printer's own screen, or start a print -- its start sequence wipes on the pad."
            )
        result = self._gcode_filament_move(
            plan,
            signed_length_mm=-retract_mm,
            feed_mm_min=retract_feed,
            mechanism="bambu_mqtt_gcode_line",
            pre_gcode=pre,
            post_gcode=post,
            placement=placement,
        )
        if not result.success:
            if result.verification_source == "firmware_rejected_move":
                _exec.note_motion_outcome(self, "wipe", "fault", "rejected")
            return result
        watch = plan.wait_seconds(float(doc.get("watch_seconds") or self._FILAMENT_WIPE_WATCH_S))
        fault = self._watch_for_purge_fault(plan, faults_before, watch=watch, stage="wipe")
        if fault is not None:
            fault.details["purge_station"] = placement
            _exec.note_motion_outcome(self, "wipe", "fault", fault.error_code)
            return fault
        _exec.note_motion_outcome(self, "wipe", "full_run")
        result.verification_source = "no_fault_within_window"
        result.details["end_retract_mm"] = retract_mm
        for key, value in (doc.get("details") or {}).items():
            result.details[key] = value
        if isinstance(doc.get("finish"), dict):
            result.details["finish"] = dict(doc["finish"])
        if steps:
            result.steps = steps
        result.details["plate_clear_given"] = touches and plan.options.get("plate_clear") is True
        summary = str(doc.get("summary") or f"Wiped the nozzle on {self._model_name()}'s pad.")
        result.message = (
            f"{summary} Bambu acknowledges none of this and raised no fault in {watch:g}s -- look at the tip."
        )
        result.details["fault_watch_seconds"] = watch
        return result

    def _home_axes_impl(self, axes: str, options: dict[str, Any]) -> HomeResult:
        """Home the way the model's own start sequence does, from its plan; else refuse."""
        from kiln.printers.motion_plan import note_motion_outcome, run_home_plan

        action = str(options.get("_action") or "home")
        consent = options.get("plate_clear") is True
        doc = self._plan_for("home", axes=axes, on_plate_ok=consent)
        if doc is None:
            miss = self._plan_miss("home")
            note_motion_outcome(self, "home", "refused", f"no_plan_{miss.cause}" if miss is not None else "no_plan")
            raise self._unsupported(
                HomingUnsupported, "home", None,
                tail=(" On this family the vendor's own sequence raises the head before it homes, and a "
                      "bare G28 from an unknown height is exactly the move it avoids."),
            )
        if not doc.get("ok"):
            refusal = doc.get("refusal") if isinstance(doc.get("refusal"), dict) else {}
            if refusal.get("code") == "PLATE_CLEAR_REQUIRED":
                self._plate_gate(options, clearance_mm=doc.get("raise_clearance_mm"), action=action, touches_plate=True)
            note_motion_outcome(self, "home", "refused", "no_plan")
            raise self._unsupported(HomingUnsupported, action, doc)
        touches = bool(doc.get("z_home_on_plate")) and "Z" in axes
        detour = self._plate_gate(options, clearance_mm=doc.get("raise_clearance_mm"), action=action, touches_plate=touches)
        return run_home_plan(self, doc, axes=axes, options=options, action=action, steps=detour)

    def _park_head_impl(self, options: dict[str, Any]) -> HomeResult:
        """Raise, home X, travel off the plate edge to the chute -- from the park plan; else refuse."""
        from kiln.printers.motion_plan import note_motion_outcome, run_home_plan

        doc = self._plan_for("park", axes="XY")
        if doc is None or not doc.get("ok"):
            miss = self._plan_miss("park") if doc is None else None
            note_motion_outcome(self, "park", "refused", f"no_plan_{miss.cause}" if miss is not None else "no_plan")
            raise self._unsupported(HomingUnsupported, "park", doc)
        detour = self._plate_gate(options, clearance_mm=doc.get("raise_clearance_mm"), action="park")
        result = run_home_plan(self, doc, axes="XY", options=options, action="park", steps=detour)
        result.action = "park"
        return result

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def send_gcode(self, commands: list[str]) -> CommandVerdict:
        """Send G-code commands to the Bambu printer via MQTT."""
        script = "\n".join(self._gcode_lines(commands))
        self._publish_command(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "gcode_line",
                    "param": script,
                }
            }
        )
        return CommandVerdict.accepted_only(
            f"Sent {len(commands)} G-code line(s) over MQTT. Bambu firmware "
            "does not acknowledge gcode_line and there is no general "
            "read-back, so execution is NOT confirmed — observe the printer "
            "(printer_status, or the machine itself) before building on it.",
            corroboration="none",
            sent=script,
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- file deletion
    # ------------------------------------------------------------------

    _STORAGE_DIRS: tuple[str, ...] = ("/model", "/sdcard", f"/{_BAMBU_FTP_URL_DIR}")

    _READ_BACK_CAP_BYTES: int = 256 * 1024 * 1024

    def _storage_file_path(self, ftp: ftplib.FTP_TLS, file_name: str) -> str:
        """*file_name* as a full path on the printer's storage."""
        name = str(file_name or "").strip()
        if not name.startswith("/"):
            name = f"{self._detect_storage_path(ftp)}/{posixpath.basename(name)}"
        safe_path = posixpath.normpath(name)
        if not any(safe_path.startswith(d + "/") for d in self._STORAGE_DIRS):
            raise PrinterError(
                f"File path must be inside the printer storage ({', '.join(self._STORAGE_DIRS)}), "
                f"got: {file_name!r}"
            )
        return safe_path

    def read_print_file(self, file_name: str) -> bytes | None:
        """The printer's own copy of *file_name*, read back over FTPS."""
        ftp = self._ftp_connect()
        try:
            path = self._storage_file_path(ftp, file_name)
            chunks: list[bytes] = []
            size = 0
            too_big = False

            def _take(chunk: bytes) -> None:
                nonlocal size, too_big
                if too_big:
                    return
                size += len(chunk)
                if size > self._READ_BACK_CAP_BYTES:
                    too_big = True
                    chunks.clear()
                    return
                chunks.append(chunk)

            try:
                ftp.retrbinary(f"RETR {path}", _take)
            except Exception as exc:
                raise PrinterError(
                    f"Could not read {file_name} back from the printer over FTPS: {exc}. "
                    "Use `list_files()` to check the file is there.",
                    cause=exc,
                ) from exc
            if too_big:
                raise PrinterError(
                    f"{file_name} is larger than the {self._READ_BACK_CAP_BYTES // (1024 * 1024)} MB "
                    "read-back cap, so Kiln could not inspect it; start it from the printer's "
                    "own screen or re-slice through Kiln."
                )
            return b"".join(chunks)
        finally:
            try:
                ftp.quit()
            except Exception as exc:
                logger.debug("Failed to quit FTP session after read-back: %s", exc)

    def delete_file(self, file_path: str) -> bool:
        """Delete a file from the printer's storage via FTPS."""
        try:
            ftp = self._ftp_connect()
        except PrinterError:
            raise

        try:
            safe_path = self._storage_file_path(ftp, file_path)
        except PrinterError:
            with contextlib.suppress(Exception):
                ftp.quit()
            raise

        try:
            ftp.delete(safe_path)
            return True
        except Exception as exc:
            raise PrinterError(
                f"Failed to delete {file_path} via FTPS: {exc}\n"
                "File may not exist or the path may be wrong. "
                "Use `list_files()` to verify the file exists before retrying `delete_file()`.",
                cause=exc,
            ) from exc
        finally:
            try:
                ftp.quit()
            except Exception as exc:
                logger.debug("Failed to quit FTP session after delete: %s", exc)

    # ------------------------------------------------------------------
    # Webcam (optional)
    # ------------------------------------------------------------------

    def get_snapshot(self) -> bytes | None:
        """Capture a webcam snapshot over the camera transport this printer uses."""
        transport = self._camera_transport()
        if transport == "off":
            raise PrinterError(_LIVEVIEW_OFF)
        if transport == "rtsps":
            return self._rtsps_still()
        port_error: Exception | None = None
        try:
            frame = self._capture_jpeg_frame(raise_errors=True)
            if frame:
                return frame
        except Exception as exc:
            logger.debug("Port 6000 JPEG capture failed, trying RTSPS fallback", exc_info=True)
            port_error = exc
        try:
            return self._capture_rtsps_frame()
        except PrinterError as exc:
            if port_error is None:
                raise
            raise PrinterError(
                f"Neither camera feed gave a picture. Port 6000: {port_error} RTSPS: {exc}"
            ) from exc

    def _camera_family(self) -> str:
        """The model the camera transport falls back to: declared, else what the printer says it is."""
        declared = self._family()
        if declared:
            return declared
        serial_family, mqtt_family, _ = self._identity_families()
        if serial_family and mqtt_family and serial_family != mqtt_family:
            return ""
        return serial_family or mqtt_family or ""

    def _camera_transport(self) -> str:
        """``"rtsps"``, ``"port6000"``, ``"off"`` or ``""``: the printer's own status first, then its model."""
        with self._state_lock:
            ipcam = self._last_status.get("ipcam")
            ipcam = dict(ipcam) if isinstance(ipcam, dict) else None
        announced = _announced_camera_transport(ipcam)
        family = self._camera_family()
        # The liveview switch does not gate the port-6000 feed, so "off" only
        # stops a model whose camera is RTSPS.
        if announced == "off" and family not in _RTSPS_FAMILIES:
            announced = ""
        if announced:
            return announced
        if family in _RTSPS_FAMILIES:
            return "rtsps"
        return "port6000" if family in _PORT6000_FAMILIES else ""

    def _rtsps_still(self) -> bytes:
        """One frame from a printer whose camera is RTSPS."""
        if not _find_ffmpeg():
            raise PrinterError(_RTSPS_NEEDS_FFMPEG)
        try:
            return self._capture_rtsps_frame()
        except PrinterError as exc:
            raise PrinterError(
                f"{exc} If LAN Only Liveview is off on the printer's screen, turn it on."
            ) from exc

    def _camera_auth_packet(self) -> bytes:
        """The 80-byte camera auth packet for this printer's access code."""
        return _camera_auth_packet(self._access_code)

    def _camera_tls_context(self) -> ssl.SSLContext:
        """The TLS context the camera port is opened with."""
        ctx = self._build_tls_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def camera_frame_source(
        self, *, port: int | None = None, timeout: float | None = None
    ) -> BambuPort6000Source:
        """The relay's continuous port-6000 source for this printer."""
        return BambuPort6000Source(
            self._host,
            self._camera_auth_packet(),
            self._camera_tls_context(),
            port=_CAMERA_PORT if port is None else port,
            timeout=float(self._timeout if timeout is None else timeout),
        )

    def _capture_jpeg_frame(self, *, timeout: float = 5.0, raise_errors: bool = False) -> bytes | None:
        """Capture ONE JPEG frame via the TLS+JPEG protocol on port 6000."""
        source = self.camera_frame_source(timeout=timeout)
        stop = threading.Event()
        try:
            for frame in source.frames(stop):
                stop.set()
                return frame
        except CameraStreamError as exc:
            if raise_errors:
                raise
            logger.debug("Port 6000 JPEG capture failed: %s", exc)
        return None

    def _family(self) -> str:
        """The config-declared family (``"a1"`` for ``bambu_a1``), or ``""``."""
        model = self._printer_model
        return model[len("bambu_") :] if model.startswith("bambu_") else model

    def _own_camera_handshake(self) -> CameraHandshake:
        """Open the camera port, complete TLS, hang up: no access code, no video."""
        from kiln.printers.base import CAMERA_HANDSHAKE_ADDRESS_TIMEOUT

        if self._camera_transport() in ("rtsps", "off"):
            port, where, proven = (
                _RTSPS_PORT,
                "bambu_rtsps_322",
                "The RTSPS camera port completed a TLS handshake. Kiln has no RTSP client "
                "that asks without opening the stream, so nothing past the handshake was "
                "checked.",
            )
        else:
            port, where, proven = (
                _CAMERA_PORT,
                "bambu_port6000",
                "The camera port completed a TLS handshake. Kiln sent no access code and "
                "read no video, so whether the code is accepted is learned when video "
                "first starts.",
            )
        try:
            ssock = _connect_camera_tls(
                self._host,
                port,
                self._camera_tls_context(),
                timeout=CAMERA_HANDSHAKE_ADDRESS_TIMEOUT,
                name_address=False,
            )
        except CameraStreamError as exc:
            return CameraHandshake(False, where, str(exc))
        with contextlib.suppress(OSError):
            ssock.close()
        return CameraHandshake(True, where, proven)

    def stream_capability(self) -> StreamCapability:
        """Whether Kiln's local relay can carry this printer's live video."""
        if self._external_camera is not None:
            return super().stream_capability()
        transport = self._camera_transport()
        if transport == "off":
            return StreamCapability(False, "rtsp", _LIVEVIEW_OFF)
        if transport == "rtsps":
            return StreamCapability(
                False,
                "rtsp",
                "This model's camera streams over RTSPS (port 322), which "
                "needs ffmpeg and which Kiln's local relay does not carry. "
                "Snapshots still work with ffmpeg installed.",
                requires=("ffmpeg",),
            )
        return StreamCapability(
            True,
            "bambu_port6000",
            # The screen's camera toggle is not a requirement for this feed
            requires=("LAN Only mode with the printer's LAN access code",),
        )

    def frame_source(self) -> Any | None:
        if self._external_camera is not None:
            return super().frame_source()
        if not self.stream_capability().available:
            return None
        return self.camera_frame_source()

    def _capture_rtsps_frame(self) -> bytes | None:
        """Capture a frame via RTSPS on port 322 using ffmpeg."""
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            raise PrinterError(
                "Camera snapshot requires either a port-6000 JPEG stream "
                "(A1 / A2 / P1 series) or ffmpeg for RTSPS (X1 / X2 / H2 / P2 series). "
                "Neither is available. Install ffmpeg if using an RTSPS printer."
            )

        return capture_rtsp_frame(
            self._raw_stream_url(), ffmpeg=ffmpeg, label="Camera RTSPS"
        )

    def _raw_stream_url(self) -> str:
        """Return the real RTSPS URL with embedded credentials (internal use only)."""
        return f"rtsps://bblp:{self._access_code}@{self._host}:{_RTSPS_PORT}/streaming/live/1"

    def get_stream_url(self) -> str | None:
        """Return the RTSPS stream URL for X1 series printers."""
        return f"rtsps://bblp:****@{self._host}:{_RTSPS_PORT}/streaming/live/1"

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """Disconnect the MQTT client and release the printer's LAN slot."""
        self._stop_idle_reaper()
        with self._connect_lock:
            if self._mqtt_client is not None:
                client = self._mqtt_client
                self._mqtt_client = None
                self._safe_stop_client(client)
                self._mqtt_connected.clear()
                with self._state_lock:
                    self._connected = False
            self._pending_outcomes_reconciled = False

    def update_credentials(self, access_code: str) -> None:
        """Update the access code and force MQTT reconnection."""
        self._access_code = access_code
        self.disconnect()
        logger.info("Access code updated; MQTT will reconnect on next operation")

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<BambuAdapter host={self._host!r} serial={self._serial!r}>"

    def __del__(self) -> None:
        if hasattr(self, "_mqtt_client") and self._mqtt_client is not None:
            with contextlib.suppress(Exception):
                self._mqtt_client.disconnect()
            self._mqtt_client = None
