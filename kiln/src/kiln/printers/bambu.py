"""Bambu Lab adapter for the Kiln printer abstraction layer.

Implements :class:`~kiln.printers.base.PrinterAdapter` by talking to a
Bambu Lab printer (X1C, P1S, P1P, A1, A1 Mini, etc.) over the local-LAN
MQTT protocol and FTPS for file management.

Bambu printers expose:
* **MQTT** on port 8883 (TLS) for status, commands, and G-code.
* **FTPS** on port 990 (implicit TLS) for file upload/download/delete.

Authentication uses the printer's **LAN Access Code** (found on the
printer's LCD under Network settings) as both the MQTT password and
the FTPS password.  The username is always ``"bblp"``.

The adapter mirrors the retry and error-handling patterns established by
the OctoPrint and Moonraker adapters.
"""

from __future__ import annotations

import contextlib
import ftplib
import hashlib
import hmac
import json
import logging
import os
import posixpath
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from kiln.printers.base import (
    STALE_STATE_WARN_AGE,
    JobProgress,
    JobResult,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterInfo,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
    _record_watched_duration,
    outcome_printer_name,
)
from kiln.printers.progress_motion import forget_job_start, job_elapsed_seconds

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

# Error message for single-client MQTT/FTPS connection rejection.
_SINGLE_CLIENT_MSG = (
    "MQTT connection rejected — another client (BambuStudio, Bambu Handy) "
    "may be connected. Bambu printers only allow one LAN MQTT client at a "
    "time. Close other Bambu software and retry."
)
_SINGLE_CLIENT_FTPS_MSG = (
    "FTPS TLS handshake failed — another client (BambuStudio, Bambu Handy) "
    "may be holding the connection. Bambu printers only allow one LAN client "
    "at a time. Close other Bambu software and retry."
)

# Backoff parameters for MQTT reconnection.
_BACKOFF_INITIAL_DELAY: float = 1.0  # seconds
_BACKOFF_MULTIPLIER: float = 2.0
_BACKOFF_MAX_DELAY: float = 30.0  # seconds
# Max age before cached state is "too old" to serve during a backoff cooldown.
# Read from base rather than restated, so this adapter's ceiling and the age at
# which every reporting surface calls a reading stale are one decision.
_STALE_STATE_MAX_AGE: float = STALE_STATE_WARN_AGE
_FTPS_MAX_RETRIES: int = 3  # retry count for transient FTPS connection failures


# ---------------------------------------------------------------------------
# Backoff tracking
# ---------------------------------------------------------------------------


@dataclass
class _BackoffState:
    """Tracks exponential backoff for MQTT reconnection attempts.

    :param attempt_count: Number of consecutive failed connection attempts.
    :param last_attempt_time: :func:`time.monotonic` timestamp of the last attempt.
    :param next_retry_time: Earliest :func:`time.monotonic` timestamp at which
        the next connection attempt is permitted.
    """

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


# Mapping from Bambu ``gcode_state`` strings to :class:`PrinterStatus`.
#
# ``finish`` maps to IDLE on purpose: a printer that has finished is doing
# nothing and is ready for the next job, which is what every pre-print gate
# reads this value to decide.  What it does NOT say — that a print just ran
# to completion — is carried by _JOB_RESULT_MAP below, so the two facts stop
# competing for one field.
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

# How the last job ENDED, for the ``gcode_state`` values that say so.
# ``idle`` is absent deliberately — on firmware that jumps straight to it
# after a print, it genuinely carries no ending, and inventing one here
# would be the same guess this field exists to stop making.  ``failed`` is
# resolved in :meth:`BambuAdapter._build_state_from_cache` rather than
# here, because its meaning depends on the ``print_error`` beside it.
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

# Bambu speed profile levels (MQTT print_speed command values).
_SPEED_PROFILES: dict[str, int] = {
    "silent": 1,
    "standard": 2,
    "sport": 3,
    "ludicrous": 4,
}
_SPEED_PROFILE_NAMES: dict[int, str] = {v: k for k, v in _SPEED_PROFILES.items()}

# Known Bambu firmware error codes with actionable messages.
# Error codes appear in the ``print_error`` field of MQTT push_status.
# Hex format: 0502-4007 → decimal 84033543.
_KNOWN_PRINT_ERRORS: dict[int, str] = {
    84033543: (
        "Printer rejected the command (error 0502-4007: authentication expired). "
        "This happens when the printer is restarted — the access code becomes stale. "
        "FIX: On the printer touchscreen, go to Settings → Network → "
        "turn LAN Only Mode OFF then ON, then toggle Developer Mode OFF and ON. "
        "Copy the NEW access code and update your Kiln config "
        "(kiln config set access_code <new_code>). "
        "The old access code will NOT work even if it looks the same — "
        "you must regenerate it."
    ),
}

# HMS error code prefixes that match nozzle clumping / blob detection.
# These are NOT the lidar first-layer inspection (which uses 0C00 prefix).
# The full HMS code is 0300-xxxx; the ``print_error`` decimal varies per
# firmware version, so we match on the descriptive prefix pattern.
# Error-code page: wiki.bambulab.com/en/a1-mini/troubleshooting/hmscode/0300_1A00_0002_0001
# Probe schedule + behaviour: wiki.bambulab.com/en "A1 Series Nozzle Clumping
# Detection" — the authoritative source for WHEN it probes.  A1 / A1 mini only.
_NOZZLE_CLUMP_ERROR_PREFIXES: tuple[str, ...] = (
    "03008014",   # Nozzle clumping detection by probing (A1 series)
    "03001A00",   # Nozzle wrapped in filament / plate placement
    "03001800",   # Nozzle clumping calibration failure
)

# The probe cadence is MASS-based, not fixed layers (the old "layers 4/11/20"
# was one print's 8 g-cadence mistaken for a rule).  Single-sourced here so the
# user message and the docstrings below stay in sync.  A1 / A1 mini only.
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
    "CLI: kiln print <file> --no-nozzle-check.  "
    "MCP: start_print(file, nozzle_clog_detect=False)."
)


def _is_nozzle_clump_error(error_code: int) -> bool:
    """Check if an error code matches a known nozzle clumping HMS code."""
    hex_code = f"{error_code:08X}"
    return any(hex_code.startswith(prefix) for prefix in _NOZZLE_CLUMP_ERROR_PREFIXES)


# HMS prefixes for flow / extrusion anomalies that should feed the
# kiln-pro nozzle wear cross-check.  Distinct from nozzle clumping
# (above) — clumping is a first-layer probe artifact, these are
# mid-print extrusion signals that correlate with bore widening /
# tip wear / filament-path friction.
#
# Source: Bambu Lab HMS wiki (wiki.bambulab.com/en/x1/troubleshooting/hms)
# plus cross-reference with the community-maintained code list at
# github.com/Doridian/BambuStudio/wiki/HMS-codes.  Conservatism is
# the right call here — false positives on flow-anomaly tagging
# poison the wear-rate signal more than missed positives.
_FLOW_ANOMALY_ERROR_PREFIXES: tuple[str, ...] = (
    "03008003",   # Filament feeding abnormal (P1/X1) — extruder can't pull
    "03008005",   # Filament broken at extruder
    "03001900",   # Tangled filament at extruder feed (cross-talks with 03001A00 nozzle wrap)
    "05000B00",   # Filament stuck/jam reported by AMS
    "05000900",   # Extrusion failure (P1 series) — broad bucket
    "03000900",   # Extruder motor overload / clog / filament stuck (P2S servo);
                  # a closed-loop servo faults here where a stepper skips
)

# Severity mapping for flow-anomaly codes.  Used when the wire
# fires record_extrusion_event so the wear cross-check weights
# strong signals (broken filament, stuck) over weak ones (general
# extrusion failure, which could be intermittent).
_FLOW_ANOMALY_SEVERITY: dict[str, str] = {
    "03008003": "medium",
    "03008005": "high",
    "03001900": "medium",
    "05000B00": "high",
    "05000900": "low",
    "03000900": "high",
}


def _classify_flow_anomaly(error_code: int) -> tuple[str, str] | None:
    """Return (event_type, severity) for a flow-anomaly HMS code.

    Returns ``None`` when the code doesn't match a known flow-anomaly
    prefix.  Caller uses this to decide whether to feed the kiln-pro
    nozzle wear cross-check via ``record_extrusion_event``.
    """
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

# Bambu LED node names.
_VALID_LED_NODES: frozenset[str] = frozenset({"chamber_light", "work_light"})
_VALID_LED_MODES: frozenset[str] = frozenset({"on", "off", "flashing"})

# Bambu fan node -> M106 P-index.  Bambu drives its fans with the standard
# Marlin ``M106 P<n> S<0-255>`` G-code (the same G-code path this adapter
# already uses for M104/M140 temperature control), where the P-index selects
# the fan:
#   P1 part-cooling / model fan   (reported as ``cooling_fan_speed``)
#   P2 auxiliary / big fan        (reported as ``big_fan1_speed``)
#   P3 chamber / exhaust fan      (reported as ``big_fan2_speed``)
# The index<->fan mapping is confirmed by Bambu's published machine G-code and
# matches the status fields ``get_state`` already reads.  A P-index for a fan a
# given model lacks (e.g. no chamber fan on the A1 / A1 mini) is a firmware
# no-op, not an error.
_FAN_NODE_TO_INDEX: dict[str, int] = {
    "part": 1,
    "part_cooling": 1,
    "cooling": 1,
    "aux": 2,
    "auxiliary": 2,
    "chamber": 3,
}

# Models whose firmware wants an ``ftp://`` job URL instead of the
# ``file:///sdcard/model/`` form every other Bambu reads.  See
# _build_print_url for the measurement this comes from.  Keyed on the
# CONFIG-DECLARED model (``printer_model`` in config.yaml), because that is
# the only identity permitted to drive behaviour — get_printer_info's probes
# are telemetry-only, per the safety boundary documented there.
_BAMBU_FTP_URL_MODELS: frozenset[str] = frozenset({"bambu_p2s", "p2s"})

# The directory that form points into.  Also the folder the P2S actually
# stores print jobs in, which is why the raw-G-code branch already accepts a
# "/cache/" path.
_BAMBU_FTP_URL_DIR = "cache"

# Mapping of printer model identifiers (from 3MF metadata, MQTT, and serial
# prefixes) to canonical family names.  Used by _check_printer_model_mismatch
# to detect when a 3MF was sliced for a different printer family.
_BAMBU_MODEL_FAMILIES: dict[str, str] = {
    # BambuStudio internal IDs
    "BBL-A1M": "a1_mini",
    "BBL-A1": "a1",
    "BL-A001": "a1",
    "BL-P002": "x1c",
    "BBL-X1C": "x1c",
    "BBL-X1E": "x1e",
    "BL-P001": "p1s",
    "BBL-P1S": "p1s",
    "BBL-P1P": "p1p",
    # model_id codes (BambuStudio resources/printers/N*.json or O*.json + MQTT report)
    "N9": "a2l",
    "N7": "p2s",
    "O1S": "h2s",
    # Human-readable names (from slicer config / XML metadata, and the
    # product_name field of MQTT get_version firmware modules)
    "Bambu Lab A1 mini": "a1_mini",
    "Bambu Lab A1": "a1",
    "Bambu Lab A2L": "a2l",
    "Bambu Lab X1 Carbon": "x1c",
    "Bambu Lab X1E": "x1e",
    "Bambu Lab X2D": "x2d",
    "Bambu Lab P1S": "p1s",
    "Bambu Lab P2S": "p2s",
    "Bambu Lab H2C": "h2c",
    "Bambu Lab H2D": "h2d",
    "Bambu Lab H2D Pro": "h2d_pro",
    "Bambu Lab H2S": "h2s",
    "Bambu Lab P1P": "p1p",
    # Serial number prefixes (first 3 chars of Bambu serial).
    # All verified against wiki.bambulab.com/en/general/find-sn
    # (2026-06; re-verified 2026-08-09, when the X2D/H2C/H2D/H2D Pro
    # rows were added from the same page).
    "030": "a1_mini",
    "039": "a1",
    "00M": "x1c",
    "03W": "x1e",
    "20P": "x2d",
    "01P": "p1s",
    "01S": "p1p",  # FIX: was wrongly mapped to "x1c". 01S is P1P; X1C is 00M.
    "22E": "p2s",
    "26A": "a2l",
    "31B": "h2c",
    "094": "h2d",
    "239": "h2d_pro",
    "093": "h2s",
}


# Firmware-module name heads that belong to ACCESSORIES, not the
# printer: "ams_f1/N" = AMS Lite unit N, "n3f/N" = AMS 2 Pro,
# "n3s/N" = AMS HT, "ams/N" = AMS.  These carry their own product_name
# ("Bambu Lab AMS 2 Pro"), so identity reads must skip them or an A1
# with AMS Lite can be reported as whatever its accessory says.
_BAMBU_ACCESSORY_MODULE_HEADS: frozenset[str] = frozenset(
    {"ams_f1", "n3f", "n3s", "ams"}
)


def _is_accessory_module(name: str) -> bool:
    """True when a firmware module belongs to an AMS unit, not the printer."""
    head, sep, idx = str(name).partition("/")
    return bool(sep) and idx.isdigit() and head in _BAMBU_ACCESSORY_MODULE_HEADS


def _normalize_fingerprint(value: str) -> str:
    """Normalize a SHA-256 fingerprint string to lowercase hex."""
    return "".join(ch for ch in value.lower() if ch in "0123456789abcdef")


def _find_ffmpeg() -> str | None:
    """Find ffmpeg binary on PATH or common install locations."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    for candidate in (
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Implicit FTPS helper (Bambu printers use port-990 implicit TLS)
# ---------------------------------------------------------------------------


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS subclass for implicit TLS (port 990).

    Standard :class:`ftplib.FTP_TLS` only supports explicit STARTTLS.
    Bambu Lab printers require the socket to be wrapped in TLS immediately
    upon connection (implicit mode), and data channels must reuse the
    control-channel TLS session to satisfy the printer's session-reuse
    requirement.

    Also handles Python 3.14+ changes to TLS handling in ``ftplib`` and
    the ``conn.unwrap()`` timeout that Bambu printers frequently cause
    (the upload succeeds before unwrap completes).
    """

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
        """Override to handle passive mode data connections with TLS wrapping.

        Manually implements passive mode to avoid Python 3.14 issues where
        ``ftplib.FTP.ntransfercmd`` may try to wrap an already-wrapped socket.
        Reuses the control-channel TLS session, which Bambu printers require.
        """
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
        """Override to handle ``conn.unwrap()`` timeout on Bambu printers.

        Bambu printers frequently cause ``TimeoutError`` on ``conn.unwrap()``
        after the upload data has already been fully sent.  The upload itself
        succeeds; only the TLS shutdown handshake times out.
        """
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


class BambuAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by Bambu Lab MQTT + FTPS.

    Args:
        host: IP address or hostname of the Bambu printer on the LAN.
        access_code: LAN Access Code from the printer's LCD.
        serial: Printer serial number (used in MQTT topics).  Found on the
            printer's LCD under Device Info.
        timeout: Timeout in seconds for MQTT operations and FTP connections.
        tls_mode: TLS verification mode: ``"pin"`` (default, TOFU pinning),
            ``"ca"`` (strict CA/hostname validation), or ``"insecure"``
            (legacy behavior, disables certificate validation).
        tls_fingerprint: Optional SHA-256 fingerprint to pin explicitly
            (hex, with or without ``:`` separators).

    Raises:
        ValueError: If *host*, *access_code*, or *serial* are empty.

    Example::

        adapter = BambuAdapter(
            host="192.168.1.100",
            access_code="12345678",
            serial="01P00A000000001",
        )
        state = adapter.get_state()
        print(state.state, state.tool_temp_actual)
    """

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
        # Configured printer model (e.g. "bambu_a1"), lower-cased.  Used
        # to tell AMS Lite (A1 / A1 mini) from the full AMS — they differ
        # in which fields carry real readings.  Empty when unset.
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

        # MQTT topic names.
        self._topic_report = f"device/{serial}/report"
        self._topic_request = f"device/{serial}/request"
        # Firmware module list from the get_version reply (info.module).
        # Static per session; cached on first sight.  Lets get_ams_status
        # resolve AMS unit type (e.g. "ams_f1/0") which print.ams.ams[] omits.
        self._fw_modules: list[Any] = []
        self._fw_modules_requested = False  # one get_version request per session

        # State cache -- updated by MQTT messages.
        self._state_lock = threading.Lock()
        self._last_status: dict[str, Any] = {}
        self._last_state_time: float = 0.0  # monotonic time of last accepted update
        # Monotonic time of the last accepted update that actually CARRIED
        # gcode_state.  Separate from _last_state_time because the cache is a
        # merge (see _on_message): a push carrying only temperatures advances
        # the dict's age while gcode_state stays whatever it was, so the dict's
        # age is not the age of the state a caller is told about.  0.0 means no
        # push has ever carried it.
        self._gcode_state_time: float = 0.0
        self._connected = False
        self._sequence_id = 0
        # One-shot per process: on the first full status after connecting,
        # settle any outcome rows left pending by prints that ended while
        # no Kiln process was watching (see auto_record_hook).
        self._pending_outcomes_reconciled = False

        # MQTT client.
        self._mqtt_client: mqtt.Client | None = None
        self._mqtt_connected = threading.Event()
        self._connect_lock = threading.Lock()

        # Exponential backoff for reconnection attempts.
        self._backoff = _BackoffState()
        self._pin_lock = threading.Lock()

        # Cached FTPS storage path — set by upload_file() to avoid
        # re-probing during start_print().  Values: "/model" (A1) or
        # "/sdcard" (X1/P1).
        self._last_storage_path: str | None = None

    @staticmethod
    def _host_key(host: str) -> str:
        """Return canonical key for pin-store lookups."""
        return host.strip().lower()

    def _build_tls_context(self) -> ssl.SSLContext:
        """Build SSL context according to configured TLS mode."""
        ctx = ssl.create_default_context()
        # Floor the protocol at TLS 1.2.  Bambu firmware negotiates
        # TLS 1.2+, and modern Python/OpenSSL builds already refuse
        # anything older by default — this makes the floor explicit on
        # older builds too.  Certificate trust is a separate axis: the
        # printers use self-signed certs, so identity is verified via
        # fingerprint pinning (pin mode) or a user-supplied CA (ca
        # mode), never by the system trust store.
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
        """Capabilities supported by the Bambu backend.

        Bambu printers use 3MF files (which contain G-code inside) and
        can also accept raw G-code commands via MQTT.  File management
        is done via FTPS, not the MQTT channel.
        """
        return PrinterCapabilities(
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
            can_pause=True,
            can_clear_error=True,
            cancel_during_calibration_faults=True,
            # Port 6000 TLS+JPEG works on A1 / A1 Mini / P1P / P1S
            # without ffmpeg.  get_snapshot() tries port 6000 first
            # and falls back to RTSPS (X1 series, port 322) only if
            # the port-6000 path fails — RTSPS does need ffmpeg, but
            # by that point we're already in a Bambu X1-specific
            # error case that surfaces as a clear PrinterError.
            # Reporting True here lets every Bambu user opt into
            # snapshot-based workflows; runtime failures bubble up
            # from get_snapshot() with model-specific guidance.
            can_snapshot=True,
            can_stream=True,
            supported_extensions=(".3mf", ".gcode", ".gco"),
        )

    def _mqtt_reported_family(self) -> tuple[str | None, str]:
        """``(family, raw_product_name)`` from the cached ``get_version``
        module list, skipping accessories.

        AMS units appear in the same module list and carry their own
        ``product_name`` ("Bambu Lab AMS 2 Pro"), so an identity read
        that trusted module order could report an A1's accessory as the
        printer.  Only non-accessory modules are consulted, and an
        unmapped name is never returned verbatim.
        """
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

    def _identity_families(self) -> tuple[str | None, str | None, str]:
        """``(serial_family, mqtt_family, product_name)`` — the single
        computation behind both :meth:`get_printer_info` and
        :meth:`get_identity_channels`, so the model this adapter
        reports and the channels a diagnostic shows can never drift.
        """
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
        """The printer's self-reported model, for telemetry and display.

        Two INDEPENDENT identity channels, no new I/O, and they must
        agree:

        1. The serial-number prefix, mapped through
           :data:`_BAMBU_MODEL_FAMILIES` — Bambu's own documented
           scheme (wiki.bambulab.com/en/general/find-sn), deterministic
           and available even when the printer is powered off.  This is
           the primary channel.
        2. ``product_name`` from the cached ``get_version`` firmware
           modules — the printer's exact model string, e.g. ``"Bambu
           Lab P1S"``.  Corroborates the serial, and covers a model too
           new for the prefix table.  X1-series firmware never reports
           this field, and the legacy hw_ver/project_name matching that
           once covered it is unreliable on newer firmware (hardware
           revisions re-use hw_ver codes), so it is deliberately unused.

        When both resolve and DISAGREE, this reports nothing and logs a
        warning.  A disagreement means one of the two tables is wrong,
        and shipping either answer would repeat the 2026-04 failure
        (commit a19e665b): a mapping table that was wrong in five of
        six rows, confidently naming the wrong printer.  Silence is the
        honest answer; the fleet view falls back to family grain, and
        :meth:`get_identity_channels` keeps the disagreement itself
        visible to diagnostics rather than losing it in this ``None``.

        SAFETY BOUNDARY: telemetry/display only.  The config-declared
        model (``printer_model`` in config.yaml) owns every safety and
        behavior decision — including this adapter's own
        ``_printer_model`` attribute, which selects AMS-Lite vs full
        AMS interpretation.  This probe never writes to that attribute,
        and where the owner has declared a model the declaration wins
        even for display (see ``community_autofire.resolve_adapter_model``).
        """
        serial_family, mqtt_family, product_name = self._identity_families()
        if serial_family and mqtt_family and serial_family != mqtt_family:
            logger.warning(
                "Bambu identity channels disagree: serial prefix %r says %r, "
                "firmware product_name %r says %r. Reporting no model — set "
                "`printer_model` in ~/.kiln/config.yaml to settle it.",
                self._serial[:3],
                serial_family,
                product_name,
                mqtt_family,
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
    # Internal: MQTT
    # ------------------------------------------------------------------

    def _safe_stop_client(self, client: mqtt.Client) -> None:
        """Stop an MQTT client with a timeout to prevent hangs.

        ``loop_stop()`` calls ``threading.join()`` with no timeout,
        which can block forever if the network thread is stuck.  This
        helper wraps the stop in a daemon thread with a deadline.
        """
        try:
            client.disconnect()
        except Exception:
            logger.debug("MQTT client disconnect call failed", exc_info=True)
        def _quiet_loop_stop() -> None:
            # Catch inside the daemon thread: an exception escaping the thread
            # target becomes an unraisable thread exception (noisy in prod, and
            # it trips pytest's threadexception plugin).  A failed/abandoned
            # stop is already best-effort here, so log and move on.
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

    def _ensure_mqtt(self) -> mqtt.Client:
        """Ensure the MQTT client is connected, creating it if needed.

        Respects the exponential backoff schedule.  If the backoff cooldown
        has not yet elapsed, raises :class:`PrinterError` immediately
        instead of hammering the printer with connection attempts.

        Returns:
            The connected MQTT client.

        Raises:
            PrinterError: If connection fails within the timeout or the
                adapter is in a backoff cooldown period.
        """
        # Fast path — no lock needed if already connected.
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
                    # Unique per process: the MQTT broker drops any existing
                    # session when a new client connects with a client_id that
                    # is already in use. A fixed id meant two Kiln instances on
                    # one machine (for example a long-running `kiln serve` plus
                    # a Claude Code MCP session) evicted each other from the
                    # printer, so each saw it flap offline. The pid suffix gives
                    # every instance its own session; it stays stable across
                    # that instance's own reconnects.
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
                # Use connect_async so the TCP handshake happens in the
                # background network thread instead of blocking the caller.
                # Prevents scheduler TimeoutError on slow/flaky networks.
                client.connect_async(self._host, _MQTT_PORT, keepalive=60)
                client.loop_start()

                # Wait for the connection to be established.
                if not self._mqtt_connected.wait(timeout=self._timeout):
                    self._safe_stop_client(client)
                    self._backoff.record_failure()
                    raise PrinterError(
                        f"Couldn't reach the printer at {self._host} — "
                        f"no response within {self._timeout}s.\n"
                        "  Most likely something else is already connected: "
                        "Bambu printers allow only a few connections at "
                        "once.  Close Bambu Studio, the Handy app, or "
                        "another machine using the printer, then try "
                        "again.\n"
                        "  If that's not it: check the printer is powered "
                        "on and on this network, LAN Mode is on, and the "
                        "access code is current (printer screen → Settings "
                        "→ Network)."
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
                return client

            except PrinterError:
                raise
            except Exception as exc:
                self._backoff.record_failure()
                # Detect single-client rejection: Bambu printers only allow one
                # LAN MQTT connection at a time.  When BambuStudio or Bambu Handy
                # holds the slot, the TLS handshake is reset or times out.
                exc_str = str(exc).lower()
                is_single_client = (
                    isinstance(exc, (ConnectionResetError, ssl.SSLError))
                    or "connection reset by peer" in exc_str
                    or "errno 54" in exc_str
                    or "tls" in exc_str and "handshake" in exc_str
                )
                if is_single_client:
                    raise PrinterError(
                        _SINGLE_CLIENT_MSG,
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
        try:
            rc = int(reason_code) if reason_code is not None else 0
        except (TypeError, ValueError):
            # paho-mqtt v2 passes a ReasonCode object
            rc = reason_code.value if hasattr(reason_code, "value") else 0
        if rc != 0:
            logger.warning(
                "MQTT connection rejected by %s (reason_code=%s)",
                self._host,
                reason_code,
            )
            # On auth failure (rc=5 "Not authorized", rc=4 "Bad credentials"),
            # stop the client to prevent infinite reconnect spam that floods
            # the printer's MQTT broker and can destabilize other connections.
            if rc in (4, 5):
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
        """MQTT on_message callback -- update cached state.

        Applies stale-update rejection: if the incoming message carries a
        ``msg_timestamp`` (epoch seconds) that is older than the timestamp
        of the last accepted update, the message is silently discarded.
        """
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        # Cache the firmware module list from a get_version reply.  Static
        # per session; used by get_ams_status to resolve AMS unit type.
        info_data = payload.get("info")
        if isinstance(info_data, dict) and isinstance(info_data.get("module"), list) and info_data["module"]:
            with self._state_lock:
                self._fw_modules = info_data["module"]

        # Merge print status fields into our cache.
        # A1/A1 mini may send command as "push_status" or "PUSH_STATUS".
        print_data = payload.get("print", {})
        # Initialize hook vars to None — they're only set when cmd ==
        # "push_status" and the merge actually happens.  The hook-call
        # block below short-circuits when any of these is falsy.
        prev_gcode_state: str | None = None
        new_gcode_state: str | None = None
        job_id_for_hook: Any = None
        file_name_for_hook: Any = None
        print_error_for_hook: int = 0
        # Seconds since we last KNEW this printer's run state, read across the
        # merge below.  ``None`` when we never have — the first frame of a
        # process has nothing behind it to measure from.
        push_gap_seconds: float | None = None
        if isinstance(print_data, dict):
            cmd = str(print_data.get("command", "")).lower()
            if cmd == "push_status":
                # Stale-update rejection: discard messages with an older
                # timestamp than the most recently accepted update.
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
                    # Bug #10: capture previous gcode_state BEFORE the
                    # merge so the terminal-transition hook can detect
                    # a printing→finish/failed/idle edge and auto-fire
                    # record_print_outcome.
                    prev_gcode_state = str(
                        self._last_status.get("gcode_state", "")
                    ).lower().strip()
                    # How long since we last KNEW what this printer was doing.
                    # Read BEFORE the merge refreshes it: it is the only bound
                    # on how late an ending carried by this frame might be.
                    #
                    # Deliberately the STATE's age and not the cache's.  A
                    # partial frame — a temperature, a fan step — advances the
                    # cache while saying nothing about whether the print is
                    # still running, so measuring "when did this printer last
                    # speak" would let one such frame, landing between a
                    # reconnect and the full dump, present an hour-old ending
                    # as a one-second-old one.  This is also exactly the
                    # quantity the polled door guards on as state_age_seconds.
                    push_gap_seconds = self._gcode_state_age_locked()
                    self._last_status.update(print_data)
                    self._last_state_time = time.monotonic()
                    # Stamp the vintage of the one key that decides the
                    # reported state.  ``update`` is a merge, so without this
                    # a partial push would reset the age of a gcode_state it
                    # never contained — printing a small, confident freshness
                    # number beside a stale state, which is worse than saying
                    # nothing.  Reported as PrinterState.state_age_seconds.
                    if "gcode_state" in print_data:
                        self._gcode_state_time = self._last_state_time

                    # Fire the auto-record hook outside the state lock
                    # to avoid a deadlock if record_print_outcome ever
                    # needs to call back into the adapter.  Capture the
                    # post-merge values we need to pass and defer the
                    # hook call until after we've released _state_lock.
                    new_gcode_state = str(
                        self._last_status.get("gcode_state", "")
                    ).lower().strip()
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
            # _state_lock has been released here (outside the `with`).
            # The name its owner registered, not "bambu" — that family name is
            # shared by every Bambu on the bench.  Resolved once here because
            # BOTH blocks below key on it, and the reconcile runs on the first
            # frame of a process, which is exactly when the hook block is
            # skipped for want of a previous state.
            lifecycle_name = outcome_printer_name(self)
            # _state_lock has been released here (outside the `with`).
            # Fire the hook — it's idempotent per (printer, job_id)
            # and cheap when no terminal transition occurred.
            if prev_gcode_state and new_gcode_state and job_id_for_hook:
                try:
                    from kiln.auto_record_hook import (
                        fire_terminal_state_hook,
                        is_terminal_transition,
                        observe_state,
                    )

                    # observe_state records the printer's current gcode_state
                    # for the NEXT hook fire; it returns the previous
                    # one it had recorded, which may be staler than
                    # prev_gcode_state (e.g. on the first message after
                    # process start).  Either works for terminal-transition
                    # detection, but prev_gcode_state from this merge is
                    # strictly fresher.
                    observe_state(lifecycle_name, new_gcode_state)

                    # This frame is where a connected Bambu print ENDS, as far
                    # as the rest of Kiln is concerned.  The line above writes
                    # the same shared table the adapter-generic get_state wrap
                    # reads, so the wrap that banks every other backend's
                    # duration sees prev == terminal on its next poll and finds
                    # no edge at all.  Bambu therefore has to do here what that
                    # wrap does there — with the same helper, the same rule and
                    # the same job id, not a second set of them.
                    ended = is_terminal_transition(
                        prev_gcode_state, new_gcode_state
                    )
                    # Read the stopwatch BEFORE the hook's database write: it
                    # is still running, so anything that takes time between
                    # the ending and this read is added to the print.
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
                        # Stop the stopwatch — the job it was measuring is
                        # over.  The polled door does this on its own terminal
                        # edge and, per the paragraph above, never reaches one
                        # on a connected Bambu.  Without it the stamp outlives
                        # its print, and the next print started from the
                        # touchscreen — which never passes ``start_print`` and
                        # so never restamps — inherits it and reports the age
                        # of a job that is already finished.
                        #
                        # Before the banking, not after: the elapsed was read
                        # above, so nothing here still needs the stamp, and a
                        # ledger that failed must not also leave a stopwatch
                        # running on the next print.
                        forget_job_start(self)
                        # Under the id we just gave the hook, so the hours row
                        # and the outcome row name one job and the two dedupe
                        # against each other.
                        #
                        # This frame IS the state, so what it reports has no
                        # age — what bounds the ending's lateness is how long
                        # we had gone without knowing the run state.  A live
                        # stream measures seconds and banks; the full dump that
                        # arrives on RECONNECT measures the whole outage, and
                        # its prev_gcode_state predates that outage, so it
                        # reports a print that ended at 31 minutes as however
                        # long ago we happened to notice.  That reading is
                        # monotonic and plausible and nothing downstream could
                        # ever flag it, which is exactly why it is refused.
                        _record_watched_duration(
                            job_label=str(job_id_for_hook),
                            elapsed_seconds=elapsed_seconds,
                            state_age_seconds=0.0,
                            observation_gap_seconds=push_gap_seconds,
                        )
                except Exception as exc:  # pragma: no cover
                    logger.debug(
                        "auto-record hook raised (non-fatal): %s", exc,
                    )

            # First full status after (re)connecting: settle outcome rows
            # left pending by prints that ended while nothing was
            # watching.  The reconcile only trusts what this status can
            # honestly say — a terminal state still naming the pending job
            # resolves it; a merely-idle printer resolves it to "unknown",
            # never to success; an actively-printing state leaves rows
            # for the live hook above.
            if not self._pending_outcomes_reconciled and new_gcode_state:
                self._pending_outcomes_reconciled = True
                try:
                    from kiln.auto_record_hook import (
                        reconcile_pending_outcomes,
                    )

                    reconcile_pending_outcomes(
                        printer_name=lifecycle_name,
                        gcode_state=new_gcode_state,
                        print_error_code=print_error_for_hook,
                        current_job_label=(
                            str(job_id_for_hook) if job_id_for_hook else None
                        ),
                        # Rows opened before the identity fix live under the
                        # family name; when this adapter is unregistered the
                        # two names coincide and the sweep no-ops.
                        legacy_printer_name=self.name,
                    )
                except Exception as exc:  # pragma: no cover
                    logger.debug(
                        "pending-outcome reconcile raised (non-fatal): %s", exc,
                    )

            # Flow-anomaly cross-check — when the merged push_status
            # carries an HMS code that the firmware classifies as a
            # flow / extrusion issue, feed the signal into the
            # kiln-pro nozzle wear cross-check.
            #
            # The wire is intentionally idempotent: every status
            # message that carries the same HMS code re-fires the
            # event.  The kiln-pro recorder de-dupes by (printer, code,
            # time-bucket) so a steady-state error doesn't flood the
            # event log.  Free-tier installs without kiln-pro silently
            # skip via try/except ImportError.
            if print_error_for_hook:
                _flow = _classify_flow_anomaly(print_error_for_hook)
                if _flow is not None:
                    _event_type, _severity = _flow
                    try:
                        from kiln_pro.nozzle_intelligence.sensor_signal import (
                            record_extrusion_event_for_printer,
                        )
                        record_extrusion_event_for_printer(
                            printer_id=self.name,
                            event_type=_event_type,
                            severity=_severity,
                        )
                    except ImportError:
                        # Free tier — kiln-pro nozzle module not
                        # installed.  Drop the signal silently.
                        pass
                    except Exception as exc:  # pragma: no cover
                        logger.debug(
                            "Flow-anomaly cross-check raised (non-fatal): %s",
                            exc,
                        )

    def _publish_command(
        self,
        payload: dict[str, Any],
        *,
        client: mqtt.Client | None = None,
    ) -> None:
        """Publish an MQTT command to the printer.

        Args:
            payload: The JSON command dict.
            client: Optional pre-connected client (used during on_connect).

        Raises:
            PrinterError: If publishing fails.
        """
        c = client or self._ensure_mqtt()
        try:
            c.publish(
                self._topic_request,
                json.dumps(payload),
                qos=0,
            )
            # QoS 0 is fire-and-forget — no PUBACK to wait for.
            # Bambu LAN MQTT broker does not support QoS 1 and
            # disconnects immediately on receiving a QoS 1 PUBLISH.
        except Exception as exc:
            raise PrinterError(
                f"Failed to publish MQTT command: {exc}\n"
                "MQTT session may have dropped. "
                "Retry with `get_state()` to re-establish the connection.",
                cause=exc,
            ) from exc

    def _disable_nozzle_detection(self) -> None:
        """Disable nozzle clumping / blob detection via MQTT.

        Sends two commands:
        1. ``print_option`` with ``nozzle_blob_detect: false`` — disables
           the general nozzle blob detection.
        2. ``xcam_control_set`` with ``module_name: "clump_detector"`` —
           disables the eddy-current clump probe and prevents
           ``print_halt`` on detection.

        On the A1 / A1 mini the probe runs the first probe after the first
        object's walls on layer 3, then once per ~8 g of filament consumed
        (mass-cadenced, not fixed layers).  It auto-disables under the
        slicer's Print-by-object and Spiral-vase modes and needs firmware
        >= 01.02.00.00.  A1 / A1 mini only — the X1/P1 use LiDAR/AI instead.

        These commands must be sent **before** the ``project_file``
        command to take effect for the upcoming print.

        Note: the layer-3 seed probe is also hardcoded into the timelapse
        G-code section.  For complete bypass, users should also edit the
        slicer's machine G-code to skip the timelapse probing (change
        ``{if layer_num == 2}`` to ``{if layer_num == 20000}``).
        """
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

    def _send_print_command(self, command: str) -> None:
        """Send a print-category command (pause/resume/stop).

        Raises:
            PrinterError: If the command fails.
        """
        self._publish_command(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": command,
                }
            }
        )

    def _get_cached_status(self) -> dict[str, Any]:
        """Get the latest status from the cache, requesting a refresh if stale.

        Returns a copy of the cached status dict.
        """
        self._ensure_mqtt()

        # If cache is empty, request a full dump and wait briefly.
        with self._state_lock:
            if not self._last_status:
                need_refresh = True
            else:
                need_refresh = False

        if need_refresh:
            self._publish_command(
                {
                    "pushing": {
                        "sequence_id": self._next_seq(),
                        "command": "pushall",
                    }
                }
            )
            # Give the printer a moment to respond.
            time.sleep(min(2.0, self._timeout / 2))

        with self._state_lock:
            return dict(self._last_status)

    # ------------------------------------------------------------------
    # Internal: FTPS
    # ------------------------------------------------------------------

    def _ftp_connect(self) -> ftplib.FTP_TLS:
        """Open an FTPS connection with retry and exponential backoff.

        Transient errors (connection refused, timeout, no route) are retried
        up to :data:`_FTPS_MAX_RETRIES` times.  Auth failures and
        single-client locks are raised immediately.
        """
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
        """Open a single FTPS connection attempt (no retry).

        Returns:
            A connected and authenticated :class:`ftplib.FTP_TLS` instance.

        Raises:
            PrinterError: If connection fails.
        """
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
            # Detect single-client TLS rejection on FTPS.
            exc_str = str(exc).lower()
            is_single_client = (
                isinstance(exc, (ConnectionResetError, ssl.SSLError))
                or "connection reset by peer" in exc_str
                or "tls" in exc_str and "handshake" in exc_str
            )
            if is_single_client:
                raise PrinterError(
                    _SINGLE_CLIENT_FTPS_MSG,
                    cause=exc,
                ) from exc
            exc_lower = str(exc).lower()
            if "530" in exc_lower or "login" in exc_lower or "auth" in exc_lower:
                detail = (
                    f"FTPS authentication to {self._host}:{_FTPS_PORT} failed. "
                    "Access code may be wrong or stale.\n"
                    "  1) Check printer -> Settings -> LAN for the current access code\n"
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
        """Seconds since a push last carried ``gcode_state``, or ``None``.

        ``None`` when no push ever has, so a never-populated cache reports no
        age rather than an age measured from process start.

        The caller must hold :attr:`_state_lock`; it is a plain
        :class:`threading.Lock`, and :meth:`get_state`'s cooldown branch
        already holds it where the age is needed.
        """
        if not self._gcode_state_time:
            return None
        return max(0.0, time.monotonic() - self._gcode_state_time)

    def _build_state_from_cache(
        self,
        status: dict[str, Any],
        *,
        age: float | None = None,
    ) -> PrinterState:
        """Convert a cached status dict into a :class:`PrinterState`.

        *age* is the vintage of the ``gcode_state`` this status carries, from
        :meth:`_gcode_state_age_locked`.  It is passed in rather than read here
        because both callers already hold — or deliberately do not hold —
        :attr:`_state_lock`, which is not reentrant.
        """
        gcode_state = status.get("gcode_state", "unknown")
        if not isinstance(gcode_state, str):
            gcode_state = "unknown"
        # A1/A1 mini sends uppercase state values (e.g. "RUNNING", "IDLE").
        gcode_state = gcode_state.lower()

        mapped = _STATE_MAP.get(gcode_state, PrinterStatus.UNKNOWN)
        job_result = _JOB_RESULT_MAP.get(gcode_state)

        # After a cancelled print the MQTT cache can get stuck with
        # gcode_state="failed" even though the printer is actually idle.
        # When print_error is explicitly present and equals 0 (no real error),
        # this is a stale post-cancel state — treat it as IDLE so preflight
        # checks pass.  If print_error is absent we conservatively keep ERROR.
        #
        # The IDLE downgrade stays exactly as it was: the machine really is
        # ready, and demoting it would block the next print.  What changes is
        # that the downgrade no longer DESTROYS the fact underneath it.  A job
        # the firmware calls "failed" while naming no error code is a job that
        # ended without completing — on this firmware, what a cancel looks
        # like — so it is reported as CANCELLED rather than silently becoming
        # indistinguishable from a printer nobody has touched.  Even if such a
        # state were some unreported failure rather than a cancel, "did not
        # complete" is the honest half of both, and it errs away from the
        # false "success" the flattened value used to produce.
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

        # Print error code (populated when gcode_state == "failed").
        print_error = status.get("print_error")
        print_error_int: int | None = None
        if print_error is not None:
            with contextlib.suppress(TypeError, ValueError):
                print_error_int = int(print_error)

        return PrinterState(
            connected=True,
            state=mapped,
            last_job_result=job_result,
            tool_temp_actual=status.get("nozzle_temper"),
            tool_temp_target=status.get("nozzle_target_temper"),
            bed_temp_actual=status.get("bed_temper"),
            bed_temp_target=status.get("bed_target_temper"),
            chamber_temp_actual=status.get("chamber_temper"),
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
            state_age_seconds=round(age, 1) if age is not None else None,
        )

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures.

        Uses the MQTT status cache, which is updated by periodic pushes
        from the printer and explicit ``pushall`` requests.

        During a backoff cooldown period, returns the last known state if
        it is recent enough (< :data:`_STALE_STATE_MAX_AGE` seconds old),
        otherwise returns OFFLINE without attempting reconnection.

        The returned state carries ``state_age_seconds``: how long ago a push
        last told us this.  Nothing here rewrites a state on account of its
        age — a stale PRINTING stays PRINTING, because the concurrency gate
        and the pre-flight checks read the enum, and demoting it would let a
        second print start on a machine that is already busy.  It is reported
        with its age instead, so a caller can tell "printing" from "was
        printing when the socket last delivered".
        """
        # If we are in backoff cooldown, avoid the reconnect attempt.
        if self._backoff.in_cooldown():
            with self._state_lock:
                age = time.monotonic() - self._last_state_time
                if self._last_status and age < _STALE_STATE_MAX_AGE:
                    logger.debug(
                        "In backoff cooldown; returning cached state (%.1fs old)",
                        age,
                    )
                    # The serve/refuse decision stays on the DICT's age (a
                    # cache still receiving pushes is a live connection), while
                    # the age reported to the caller is the gcode_state's own —
                    # which can be older, and is the number that describes the
                    # state being returned.
                    return self._build_state_from_cache(
                        dict(self._last_status),
                        age=self._gcode_state_age_locked(),
                    )
            logger.debug("In backoff cooldown with no recent cached state; returning OFFLINE")
            return PrinterState(
                connected=False,
                state=PrinterStatus.OFFLINE,
            )

        try:
            status = self._get_cached_status()
        except PrinterError:
            return PrinterState(
                connected=False,
                state=PrinterStatus.OFFLINE,
            )

        with self._state_lock:
            state_age = self._gcode_state_age_locked()
        return self._build_state_from_cache(status, age=state_age)

    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active (or last) print job.

        Uses the MQTT status cache.
        """
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

        # Elapsed is MEASURED from the start Kiln witnessed, or not reported.
        #
        # It used to be extrapolated from the two numbers above —
        # ``remaining / (1 - completion/100) - remaining`` — which is a
        # restatement of the percentage, not a measurement of time.  Verified
        # on an A1 (2026-08-11): at 99 % with one minute left it produced
        # 5940 s and the web rendered "1h 39m" for a print that had run about
        # 31 minutes.  Whenever remaining is one minute the elapsed in minutes
        # equals the completion percentage exactly; the "1h39m" WAS the
        # "99 %".  ``mc_remaining_time`` arrives in whole MINUTES, so as the
        # divisor approaches 0.01 that coarseness is multiplied by a hundred.
        #
        # Bambu's push payload carries no start timestamp to read instead, so
        # when Kiln did not start the print — it attached to one already
        # running, or the process restarted — the honest answer is that
        # elapsed is unknown, and the field is left unset.
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

        return JobProgress(
            file_name=file_name if file_name else None,
            completion=completion,
            print_time_seconds=print_time_seconds,
            print_time_left_seconds=print_time_left_seconds,
            current_layer=current_layer,
            total_layers=total_layers,
        )

    def list_files(self) -> list[PrinterFile]:
        """Return a list of files stored on the printer's storage.

        Uses FTPS to list the storage directory.  Automatically detects
        the correct path (``/model/`` for A1 series, ``/sdcard/`` for
        X1/P1 series).  Tries MLSD first for rich metadata, falling back
        to NLST then LIST.  If LIST returns a 550 error (common on A1
        printers), falls back to NLST which the A1 FTP server supports.
        """
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

            # Last resort: LIST (raw text parsing).  A1 printers return
            # 550 for LIST; fall back to NLST if that happens.
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
        """Detect the correct FTPS storage path for this printer.

        A1 series store files at ``/model/``, X1/P1 at ``/sdcard/``, and the
        P2S keeps print jobs in ``cache/`` — which this probe never tried, so
        a P2S upload landed in a directory the firmware does not read.

        A declared P2S is offered ``/cache`` first, following the same
        identity rule :meth:`_build_print_url` documents: the config-declared
        model is the only identity allowed to drive behaviour, and a probe is
        evidence rather than a guess.  An undeclared printer keeps ``/model``
        first, so no A1/X1/P1 ordering changes.

        The directory name is spelled from :data:`_BAMBU_FTP_URL_DIR` so it
        cannot drift away from the URL the print command is given.

        Returns:
            The storage path (e.g. ``"/model"``, ``"/sdcard"``, ``"/cache"``).
        """
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
        # Nothing answered.  Previously this returned the same value as a
        # successful /sdcard probe, so "found it" and "found nothing" were
        # indistinguishable in the logs.
        logger.warning(
            "No Bambu storage directory answered CWD (tried %s); falling "
            "back to /sdcard. Uploads may land where the firmware does not "
            "look.",
            ", ".join(candidates),
        )
        return "/sdcard"

    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a file to the printer via FTPS.

        Automatically detects the correct storage path (``/model/`` for A1
        series, ``/sdcard/`` for X1/P1 series).

        Args:
            file_path: Absolute or relative path to the local file.

        Raises:
            PrinterError: On FTP errors.
            FileNotFoundError: If *file_path* does not exist locally.
        """
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Local file not found: {abs_path}")

        filename = os.path.basename(abs_path)

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
    # 3MF wrapping for PrusaSlicer output
    # ------------------------------------------------------------------

    def wrap_gcode_as_3mf(
        self,
        gcode_path: str,
        *,
        hotend_temp: int = 220,
        bed_temp: int = 65,
        filament_type: str = "PLA",
        source_3mf_path: str | None = None,
        num_filaments: int = 1,
        filament_colors: list[str] | None = None,
        filament_types: list[str] | None = None,
        stl_paths: list[str] | None = None,
        resume_mode: bool = False,
    ) -> str:
        """Wrap PrusaSlicer gcode in a Bambu-compatible 3MF.

        The Bambu A1 requires BambuStudio's proprietary start/end gcode
        (including ``M620 M`` motor enable, AMS load, extrusion calibration)
        for the extruder to function.  This method wraps raw PrusaSlicer
        output with those sequences and packages everything as a 3MF.

        For multi-color prints, set ``num_filaments`` > 1 and provide
        ``filament_colors`` / ``filament_types`` lists.  Tool change
        ``T`` commands in the gcode will be wrapped in M620/M621 AMS
        load sequences automatically.

        :param gcode_path: Path to PrusaSlicer ``.gcode`` output (must be
            sliced with ``--use-relative-e-distances`` and empty start/end).
        :param hotend_temp: Hotend temperature in °C (default 220 for PLA).
        :param bed_temp: Bed temperature in °C (default 65 for PLA).
        :param filament_type: Filament type string (PLA, PETG, ABS, etc.).
        :param source_3mf_path: Optional source 3MF for thumbnails/geometry.
        :param num_filaments: Number of filaments (>1 for multi-color).
        :param filament_colors: List of hex color strings per filament.
        :param filament_types: List of filament type strings per filament.
        :param stl_paths: Optional STL paths for auto-generating thumbnails
            when no source_3mf_path is provided.
        :returns: Path to the output 3MF file.
        :raises FileNotFoundError: If the gcode file doesn't exist.
        :raises ValueError: If the gcode has no layer changes.
        """
        from kiln.printers.bambu_3mf import BambuPrintSettings, build_bambu_3mf

        abs_path = os.path.abspath(gcode_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Gcode file not found: {abs_path}")

        gcode_body = Path(abs_path).read_text(encoding="utf-8")
        stem = Path(abs_path).stem
        output_path = os.path.join(os.path.dirname(abs_path), f"{stem}.3mf")

        settings = BambuPrintSettings(
            hotend_temp=hotend_temp,
            bed_temp=bed_temp,
            filament_type=filament_type,
            model_name=stem,
            num_filaments=num_filaments,
            filament_colors=filament_colors,
            filament_types=filament_types,
        )

        result = build_bambu_3mf(
            gcode_body,
            output_path,
            settings=settings,
            source_3mf_path=source_3mf_path,
            stl_paths=stl_paths,
            resume_mode=resume_mode,
            # The declared model, per the identity rule _build_print_url
            # documents: the config declaration drives behaviour and the
            # serial/firmware probes stay telemetry.  Empty when the owner
            # never declared one, which keeps the historical A1 templates.
            printer_model=self._printer_model,
        )
        return result.output_path

    # ------------------------------------------------------------------
    # PrinterAdapter -- print control
    # ------------------------------------------------------------------

    def _wait_for_print_start(
        self,
        timeout: float = 15.0,
        poll_interval: float = 1.0,
    ) -> tuple[str, int | None]:
        """Poll MQTT cache until printer enters a print-active state.

        Returns a tuple of ``(state, error_code)`` where *state* is the
        string that triggered the return (e.g. ``"running"``,
        ``"prepare"``), ``"failed"`` on error state, or ``"timeout"``
        if no transition occurred.  *error_code* is the ``print_error``
        value if the printer reported one (often non-zero even before
        ``gcode_state`` flips to ``"failed"``), or ``None``.
        """
        deadline = time.monotonic() + timeout
        last_error: int | None = None
        while time.monotonic() < deadline:
            with self._state_lock:
                state = str(self._last_status.get("gcode_state", "")).lower()
                raw_err = self._last_status.get("print_error")
            if raw_err is not None:
                with contextlib.suppress(TypeError, ValueError):
                    err_val = int(raw_err)
                    if err_val != 0:
                        last_error = err_val
            if state in _PRINT_ACTIVE_STATES:
                return state, last_error
            if state == "failed":
                return "failed", last_error
            # If the printer set a non-zero error code while still IDLE,
            # the command was rejected — no point waiting further.
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
        """Check if 3MF expected filament colors match what's in the AMS.

        Compares the 3MF plate's ``filament_colors`` against what's
        actually loaded in the AMS trays (via MQTT status).  Non-blocking
        — logs warnings and returns them as strings, never raises.

        Args:
            file_path: Local path to the 3MF file.
            plate_number: Which plate is being printed.
            ams_mapping: The AMS slot mapping being used.

        Returns:
            List of human-readable warning strings (empty if no mismatches).
        """
        warnings: list[str] = []
        try:
            expected_colors = self._detect_3mf_filaments(file_path, plate_number)
            if not expected_colors:
                return warnings

            ams_info = self.get_ams_status()
            loaded_trays: dict[int, str] = {}
            for unit in ams_info.get("units", []):
                for tray in unit.get("trays", []):
                    tray_idx = tray.get("slot")
                    tray_color = tray.get("tray_color", "")
                    if tray_idx is not None and tray_color:
                        # tray_color is hex like "FF0000FF" (RRGGBBAA).
                        loaded_trays[int(tray_idx)] = tray_color[:6].upper()

            for i, slot in enumerate(ams_mapping):
                if i >= len(expected_colors):
                    break
                expected_hex = expected_colors[i].lstrip("#").upper()[:6]
                loaded_hex = loaded_trays.get(slot, "")
                if loaded_hex and expected_hex and expected_hex != loaded_hex:
                    msg = (
                        f"AMS color mismatch: plate {plate_number} filament {i} "
                        f"expects #{expected_hex} but AMS slot {slot} has "
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
        """Read plate metadata from a 3MF archive.

        Returns the parsed JSON dict for ``Metadata/plate_N.json``,
        or ``None`` if the file cannot be read.
        """
        import json
        import zipfile

        try:
            with zipfile.ZipFile(file_path, "r") as zf:
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
        """Extract filament color list from a 3MF's plate metadata.

        Reads ``Metadata/plate_N.json`` inside the 3MF archive and returns
        the ``filament_colors`` list (e.g. ``["#FFFFFF", "#808080"]``).
        Returns ``None`` if the metadata cannot be read.

        Args:
            file_path: Local path to the 3MF file.
            plate_number: Which plate's metadata to inspect.
        """
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
        """Build an ``ams_mapping`` array from 3MF plate metadata.

        BambuStudio/OrcaSlicer write ``filament_ids`` as the slicer-internal
        profile indices used by the plate.  The ``ams_mapping`` sent to the
        printer is a positional array where
        ``ams_mapping[filament_id] = tray_index``.

        When ``filament_ids`` has gaps (e.g. ``[0, 2]`` — filament profiles 0
        and 2 but not 1), the mapping must include placeholder entries (``-1``)
        for unused positions so the printer routes each filament to the
        correct AMS tray.

        Without this, a 2-color model sliced with filament IDs ``[0, 2]``
        would get a mapping of ``[0, 1]`` which only covers IDs 0 and 1,
        leaving ID 2 unmapped and defaulting to the wrong tray.

        Returns a positional mapping list (e.g. ``[0, -1, 1]``), or ``None``
        if the metadata cannot be read or has < 2 filaments.
        """
        meta = BambuAdapter._read_3mf_plate_meta(file_path, plate_number)
        if meta is None:
            return None

        filament_ids = meta.get("filament_ids")
        colors = meta.get("filament_colors")

        # Need at least 2 filaments for multi-material.
        if not isinstance(colors, list) or len(colors) < 2:
            return None

        # If filament_ids is missing or malformed, fall back to sequential.
        if not isinstance(filament_ids, list) or len(filament_ids) != len(colors):
            return list(range(len(colors)))

        # Build a positional mapping: ams_mapping[filament_id] = tray_index.
        # Filament IDs may have gaps (e.g. [0, 2]) — fill gaps with -1.
        max_id = max(filament_ids)
        mapping = [-1] * (max_id + 1)
        for tray_idx, fid in enumerate(filament_ids):
            mapping[fid] = tray_idx
        return mapping

    @staticmethod
    def filament_count_3mf(file_path: str, plate_number: int = 1) -> int | None:
        """Number of filaments a 3MF plate declares, or ``None`` if the
        plate metadata can't be read.

        Used to decide single- vs multi-material print routing.  Callers
        MUST treat ``None`` as "unknown — assume multi-material" so an
        unreadable plate is never mis-routed through a single-tray path
        (which would override a real multi-color mapping).
        """
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
        """Extract the printer model from a 3MF's metadata.

        Inspects two locations inside the 3MF archive:

        1. ``Metadata/plate_N.json`` — ``printer_model`` field.
        2. ``Metadata/model_settings.config`` or
           ``Metadata/slice_info.config`` — XML files that may contain
           ``<machine>`` or ``<printer_model>`` tags written by
           BambuStudio/OrcaSlicer.

        Returns the model identifier string (e.g. ``"BBL-X1C"``) if
        found, or ``None`` if detection fails.

        Args:
            file_path: Local path to the 3MF file.
            plate_number: Which plate's metadata to inspect.
        """
        import xml.etree.ElementTree as ET
        import zipfile

        try:
            with zipfile.ZipFile(file_path, "r") as zf:
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
        """Check if a 3MF was sliced for a different printer model.

        Compares the printer model embedded in the 3MF metadata against
        the connected printer (identified by serial number prefix).
        Non-blocking — logs warnings and returns them as strings, never
        raises.

        Args:
            file_path: Local path to the 3MF file.
            plate_number: Which plate is being printed.

        Returns:
            List of human-readable warning strings (empty if no mismatch
            or if detection fails).
        """
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
        """Check if a 3MF references filament slots that exceed AMS capacity.

        Reads ``filament_ids`` from the 3MF plate metadata and compares
        against the number of AMS tray slots actually available.  Returns
        a list of warning/error strings (empty if everything is fine).

        BambuStudio writes ``filament_ids`` as slicer-internal profile
        indices (e.g. ``[7]`` means the 8th filament profile in the
        project, NOT physical AMS slot 7).  When mapped to AMS, the
        physical slot is determined by ``ams_mapping``.  However, if
        ``filament_ids`` contains values >= total AMS slots AND no
        explicit ``ams_mapping`` is provided, the print will likely
        fail because the slicer expected more filament positions than
        the AMS supports.

        Args:
            file_path: Local path to the 3MF file.
            plate_number: Which plate to inspect.
        """
        import json
        import zipfile

        issues: list[str] = []
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                meta_name = f"Metadata/plate_{plate_number}.json"
                if meta_name not in zf.namelist():
                    return issues
                with zf.open(meta_name) as mf:
                    meta = json.loads(mf.read())

            filament_ids = meta.get("filament_ids")
            if not isinstance(filament_ids, list) or not filament_ids:
                return issues

            # Count available AMS tray slots.
            ams_info = self.get_ams_status()
            total_slots = 0
            for unit in ams_info.get("units", []):
                total_slots += len(unit.get("trays", []))

            if total_slots == 0:
                return issues  # No AMS info — can't validate.

            max_id = max(filament_ids)
            if max_id >= total_slots:
                issues.append(
                    f"3MF plate {plate_number} references filament profile "
                    f"index {max_id} but your AMS only has {total_slots} "
                    f"slot(s) (indices 0-{total_slots - 1}). This file was "
                    f"likely sliced with a multi-filament project that "
                    f"doesn't match your AMS setup. Re-slice the model in "
                    f"BambuStudio/OrcaSlicer with only your installed "
                    f"filaments, or provide an explicit --ams-mapping to "
                    f"remap the extruder indices to valid AMS slots."
                )
        except PrinterError:
            logger.debug("Could not query AMS for filament_ids validation", exc_info=True)
        except Exception:
            logger.debug("Could not validate 3MF filament_ids from %s", file_path, exc_info=True)
        return issues

    def _build_print_url(self, basename: str) -> str:
        """Build the job URL for the MQTT print command.

        The form is NOT the same on every model, which this function
        assumed for a long time.

        A1 / X1 / P1 read the job off the filesystem and want
        ``file:///sdcard/model/``.  Handing those models an ``ftp://``
        URL raises HMS 0500-C010-010800 ("MicroSD Card read/write
        exception") on A1, so that default stays exactly as it was.

        P2S firmware rejects the ``file:///`` form outright.  Measured on
        a P2S (2026-08-10), same file, same card, MD5 verified by
        round-trip, three URL forms tried in sequence:

        ==================================  ==========================
        ``file:///mnt/sdcard/cache/<name>``  ERROR STATE, 0500_4002
        ``file:///sdcard/cache/<name>``      ERROR STATE, 0500_4002
        ``ftp://cache/<name>``               started, print_error 0
        ==================================  ==========================

        0500_4002 is the firmware's "failed to load the print job" error.
        It also has to be cleared before a retry, because a set error
        code blocks new print commands.

        WHICH MODEL AM I? — read carefully before extending this.
        ``self._printer_model`` (declared in ``~/.kiln/config.yaml``) is
        the only identity allowed to drive behaviour.  The
        serial-prefix / firmware probes behind :meth:`get_printer_info`
        are TELEMETRY ONLY by the safety boundary documented there: a
        model table that guessed wrong in five of six rows once named
        the wrong printer confidently (a19e665b), so a wrong guess here
        would send the wrong URL and fail the print.  Declared model
        first; then the storage path we actually OBSERVED the upload land
        in, which is evidence rather than a guess and is the same signal
        the raw-G-code branch already trusts; then the historical
        default, so a printer nobody has declared behaves as it did
        before this change.

        H2 series is an open question, deliberately NOT included: Bambu's
        own wiki groups "H2 Series/P2S" together for Developer Mode,
        which hints at a shared firmware generation, but hinting is not
        measuring and nobody has run the three-form test on an H2.
        """
        if self._printer_model in _BAMBU_FTP_URL_MODELS:
            return f"ftp://{_BAMBU_FTP_URL_DIR}/{basename}"
        if self._last_storage_path == f"/{_BAMBU_FTP_URL_DIR}":
            return f"ftp://{_BAMBU_FTP_URL_DIR}/{basename}"
        return f"file:///sdcard/model/{basename}"

    def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
        """Begin printing a file on the Bambu printer.

        The file must already exist on the printer's SD card (uploaded
        via ``upload_file``).  For 3MF files, this sends the
        ``project_file`` command; for raw G-code, ``gcode_file``.

        After sending the command, polls MQTT for an actual state
        transition to confirm the printer accepted the job.

        Args:
            file_name: Name or path of the file on the printer.
            **kwargs: Optional overrides for 3MF print parameters:

                * ``use_ams`` (bool): Enable AMS filament feeding.
                  Default ``False``.
                * ``ams_mapping`` (list[int]): Slot mapping per extruder.
                  Defaults to ``[0]`` when AMS feeding is on and ``[]``
                  (external spool) when it is off.  Use ``-1`` for unused
                  positions.
                  Default ``[0]``.  Use ``[-1]`` for unused slots.
                * ``timelapse`` (bool): Record timelapse.  Default ``False``.
                * ``bed_leveling`` (bool): Run bed leveling.  Default ``True``.
                * ``flow_cali`` (bool): Run flow calibration.  Default ``False``.
                * ``vibration_cali`` (bool): Run vibration calibration.
                  Default ``False``.
                * ``layer_inspect`` (bool): Enable first-layer inspection
                  (lidar visual scan).  Default ``False``.
                * ``nozzle_clog_detect`` (bool): Enable nozzle clumping /
                  blob detection — the A1 / A1 mini eddy-current probe that
                  taps the nozzle just off the bed (first after the layer-3
                  walls, then once per ~8 g of filament; A1 series only).
                  Default ``True``.  Set to ``False`` to bypass HMS
                  0300-8014 false positives on thin first-layer geometry.
                  This sends both a ``print_option`` and ``xcam_control_set``
                  command to disable the check before starting the print.
                * ``bed_type`` (str): Bed surface type.  Default ``"auto"``.
                * ``plate_number`` (int): Plate index in multi-plate 3MF.
                  Default ``1``.
                * ``local_file_path`` (str): Local path to the 3MF file for
                  MD5 calculation.  If not provided, MD5 is omitted.
        """
        # Normalise: strip leading path components if user passes full path.
        basename = os.path.basename(file_name)

        # Check if already in a print-active state (skip wait).
        with self._state_lock:
            already_active = str(self._last_status.get("gcode_state", "")).lower() in _PRINT_ACTIVE_STATES

        # Collect warnings (e.g. AMS color mismatches, printer model
        # mismatches) to surface in the result message.
        warnings: list[str] = []

        if basename.lower().endswith(".3mf"):
            plate_num = kwargs.get("plate_number", 1)
            ams_mapping = kwargs.get("ams_mapping")
            use_ams = kwargs.get("use_ams", False)

            # Compute MD5 of the local 3MF file if path is provided.
            local_path = kwargs.get("local_file_path")
            file_md5 = ""
            if local_path and os.path.isfile(local_path):
                file_md5 = self._compute_file_md5(local_path)

            # Auto-detect filament count from 3MF plate metadata when
            # the caller didn't specify ams_mapping explicitly.
            if ams_mapping is None and local_path and os.path.isfile(local_path):
                auto_mapping = self._build_ams_mapping_from_3mf(
                    local_path, plate_num,
                )
                if auto_mapping is not None:
                    ams_mapping = auto_mapping
                    use_ams = True
                    logger.info(
                        "Auto-detected multi-material in plate %d — "
                        "setting use_ams=True, ams_mapping=%s",
                        plate_num,
                        ams_mapping,
                    )

            # Single-filament AMS auto-routing (defense in depth).
            #
            # If the caller didn't pass ams_mapping or use_ams and the AMS
            # has loaded trays, route to the first loaded tray rather than
            # falling through to external spool.  Silent external-spool
            # fallthrough caused production failures (error 0300-8015
            # "filament on external spool has run out") when users had
            # AMS trays loaded but nothing on the direct feeder.
            #
            # Uses the cached MQTT status only — no extra round-trip — so
            # we don't add latency to every start_print call.  Callers that
            # want the freshest state should poll ``get_ams_status`` first.
            #
            # ``use_ams`` must be checked against the original kwargs (not
            # the local default of ``False``) so callers can still opt out
            # of AMS routing by passing ``use_ams=False`` explicitly.
            if (
                ams_mapping is None
                and "use_ams" not in kwargs
            ):
                loaded_trays = self._peek_loaded_ams_trays()
                if loaded_trays:
                    slot_idx = int(loaded_trays[0].get("slot", 0))
                    ams_mapping = [slot_idx]
                    use_ams = True
                    logger.info(
                        "Single-filament AMS auto-routing: tray %d (%s)",
                        slot_idx,
                        loaded_trays[0].get("tray_type", "unknown"),
                    )
                # If we have no cached AMS data at all, stay silent — the
                # caller may not have an AMS attached.  Only warn when we
                # DO have AMS data and it shows zero loaded trays.
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

            # Fall back to single-filament defaults.
            #
            # [0] names AMS unit 0, slot 0.  Sending it when AMS feeding is
            # OFF points the firmware at a tray that need not exist, and on a
            # machine with no AMS at all it halts at the filament-mapping
            # dialog waiting for a human to reconcile a mapping against
            # hardware that is not there — a no-AMS owner being asked to fix
            # their AMS.
            #
            # The external-spool wire shape is an EMPTY array, not a magic
            # slot number.  Bambu's own networking plugin sends exactly
            # ``use_ams ? "[0]" : "[]"`` (open-bamboo-networking
            # src/print_job.cpp:184), and its comment there records that
            # firmware treats [] the same as the field not being provided.
            # Genuine BambuStudio traffic also carries one -1 per 3MF
            # filament (SelectMachine.cpp:1414); both are accepted, and [] is
            # the smaller change with the plugin-parity citation.
            #
            # 254 and 255 do NOT belong in this field.  255 is the virtual
            # external tray in ``ams_mapping2`` ({"ams_id": 255, "slot_id":
            # 0}) and in the ``tray_now`` status field; putting either here
            # conflates a tray identifier with a slot index.
            #
            # An explicitly passed [] is preserved, not rewritten — callers
            # already send one (kiln_pro material routing normalises with
            # ``list(mapping or [])``).
            if ams_mapping is None or not isinstance(ams_mapping, list):
                ams_mapping = [0] if use_ams else []

            # Validate ams_mapping length covers all filament_ids in the 3MF.
            # If the mapping is too short, filament IDs beyond the mapping
            # length will silently default to the wrong AMS tray.
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

            # Validate filament_ids against AMS capacity.
            if local_path and os.path.isfile(local_path):
                filament_issues = self._validate_3mf_filament_ids(local_path, plate_num)
                if filament_issues:
                    return PrintResult(
                        success=False,
                        message=" ".join(filament_issues),
                    )

            # Check for AMS color mismatches and surface warnings.
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

            # Disable nozzle clumping / blob detection if requested.
            # This must be sent BEFORE the project_file command.
            # Prevents HMS 0300-8014 false positives on models with
            # thin first-layer geometry.
            if not kwargs.get("nozzle_clog_detect", True):
                self._disable_nozzle_detection()

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
            # Raw G-code file.
            # A1 series stores files at FTPS /model/ → filesystem
            # /sdcard/model/, while X1/P1 uses FTPS /sdcard/ → filesystem
            # /sdcard/.  Use the cached storage path from upload_file()
            # when available; otherwise default to /sdcard/model/ (A1,
            # the more common model) so the common upload→print flow
            # works correctly on all series.
            if file_name.startswith("/"):
                # This is a path on the printer's SD card — always
                # POSIX-style.  Use posixpath so a Windows host does
                # not rewrite the separators to backslashes.
                path = posixpath.normpath(file_name)
                if not (path.startswith("/sdcard/") or path.startswith("/cache/")):
                    raise PrinterError(f"File path must be under /sdcard/ or /cache/, got: {file_name!r}")
            else:
                if self._last_storage_path == f"/{_BAMBU_FTP_URL_DIR}":
                    # P2S — jobs live in cache/, which is where the upload
                    # just went.  Without this case a declared P2S uploads a
                    # .gcode to cache/ and is then told to print
                    # /sdcard/model/<name>, which is not where it landed.
                    path = f"/{_BAMBU_FTP_URL_DIR}/{basename}"
                elif self._last_storage_path == "/sdcard":
                    # X1/P1 series — files live directly under /sdcard/.
                    path = f"/sdcard/{basename}"
                else:
                    # A1 series (or unknown) — files under /sdcard/model/.
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

        # Wait for MQTT confirmation unless already active.
        if not already_active:
            result_state, error_code = self._wait_for_print_start()
            if result_state == "failed":
                # Build a specific error message if we recognise the code.
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
                # The printer hasn't transitioned to an active state yet,
                # but the command was sent successfully.  Bambu printers
                # can take 5-8+ minutes for their startup sequence
                # (homing, AMS load, calibration) before gcode_state
                # flips to "running" or "prepare".  This is normal.
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
        self._send_print_command("stop")
        return PrintResult(success=True, message="Print cancelled.")

    def emergency_stop(self) -> PrintResult:
        """Perform emergency stop via M112 G-code over MQTT."""
        self.send_gcode(["M112"])
        return PrintResult(
            success=True,
            message="Emergency stop triggered (M112 sent).",
        )

    def clear_error(self) -> PrintResult:
        """Acknowledge a latched ``print_error`` so the next print can start.

        Bambu firmware holds ``gcode_state`` at ``failed`` with a non-zero
        ``print_error`` after a job ends badly, and keeps reporting it until
        something acknowledges it.  Dismissing the message on the printer's
        own touchscreen clears the NOTIFICATION, not the reported state —
        measured on an A1 (2026-08-13), where the screen read "ready" while
        the push payload still carried ``print_error=50348032`` and every
        pre-flight check refused.

        The payload mirrors BambuStudio's own ``command_clean_print_error``,
        field for field and type for type: a string ``sequence_id``, the
        string ``subtask_id`` of the job being acknowledged, and the INTEGER
        ``print_error`` naming which error is being cleared.  That last field
        is the one that matters and the one an earlier attempt here omitted —
        acknowledging an error without saying which error changed nothing at
        all, which is what the printer did with it.

        This reports only that the acknowledgement was SENT.  Firmware takes
        a moment, so the caller re-reads state to learn whether it took.
        """
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
                "confirm the printer has left its error state; if it has not, "
                "this firmware needs a power cycle."
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
        """Run calibration routines on the Bambu printer via MQTT.

        Bambu printers accept a ``calibration`` command with a bitmask
        ``option`` field:

        * 1 = vibration compensation (input shaper)
        * 2 = bed leveling + Z offset
        * 4 = first-layer inspection (xcam)
        * 7 = all of the above

        The printer must be idle — calibration will fail if a print is
        running.  Calibration typically takes 2-5 minutes.  The printer
        will home, probe the bed, and return to idle when complete.

        Args:
            options: Which routines to run.  Accepts ``"bed_leveling"``,
                ``"vibration"``, ``"flow"``, or ``"all"``.
                Defaults to ``["bed_leveling"]``.
        """
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

    #: Coarse adapter-side hotend ceiling. A bound per-model safety profile
    #: TIGHTENS this to the specific machine's rating (PrinterAdapter.
    #: _validate_temp takes the min), so this is only the fallback for a
    #: Bambu registered with no model. Set to the hottest current Bambu
    #: hotend -- the H2S at 350C -- so the net never sits BELOW a real
    #: machine's rating and clamps it: at 300 it silently capped the X1E
    #: (rated 320) and the H2S (rated 350) below what their own firmware
    #: allows. The firmware is the real backstop; Kiln should not be
    #: stricter than the printer.
    _MAX_HOTEND_C: float = 350.0

    def set_tool_temp(self, target: float) -> bool:
        """Set the hotend target temperature via G-code over MQTT."""
        self._validate_temp(target, self._MAX_HOTEND_C, "Hotend")
        self.send_gcode([f"M104 S{int(target)}"])
        return True

    def set_bed_temp(self, target: float) -> bool:
        """Set the heated-bed target temperature via G-code over MQTT."""
        self._validate_temp(target, 130.0, "Bed")
        self.send_gcode([f"M140 S{int(target)}"])
        return True

    # ------------------------------------------------------------------
    # Bambu-specific: speed profiles
    # ------------------------------------------------------------------

    def get_speed_profile(self) -> dict[str, Any]:
        """Return the current speed profile level and name.

        Reads ``spd_lvl`` and ``spd_mag`` from the MQTT status cache.

        Returns:
            Dict with ``level`` (1-4), ``name`` (silent/standard/sport/ludicrous),
            and ``speed_magnitude`` (actual multiplier percentage).
        """
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

    def set_speed_profile(self, profile: str) -> bool:
        """Set the printer speed profile.

        Args:
            profile: One of ``"silent"``, ``"standard"``, ``"sport"``,
                or ``"ludicrous"`` (case-insensitive).

        Returns:
            ``True`` if the command was accepted.

        Raises:
            PrinterError: If *profile* is not a valid speed profile name.
        """
        key = profile.strip().lower()
        if key not in _SPEED_PROFILES:
            raise PrinterError(
                f"Unknown speed profile {profile!r}. "
                f"Valid profiles: {', '.join(sorted(_SPEED_PROFILES))}"
            )
        self._publish_command(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "print_speed",
                    "param": str(_SPEED_PROFILES[key]),
                }
            }
        )
        return True

    def publish_print_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> bool:
        """Publish a ``print``-category MQTT command to the printer.

        A thin escape hatch for ``print`` commands this adapter does not
        wrap with a dedicated method.  Builds the standard
        ``{"print": {"sequence_id", "command", ...}}`` envelope and
        publishes it fire-and-forget (QoS 0), like every other command.

        Args:
            command: The ``print`` command name (e.g. ``"print_option"``).
            params: Extra command fields merged into the envelope.

        Returns:
            ``True`` once the command is published.
        """
        inner: dict[str, Any] = dict(params or {})
        # command + sequence_id are authoritative: set them last so a
        # caller-supplied params dict can never override them.
        inner["sequence_id"] = self._next_seq()
        inner["command"] = str(command)
        self._publish_command({"print": inner})
        return True

    # ------------------------------------------------------------------
    # Bambu-specific: skip objects mid-print
    # ------------------------------------------------------------------

    def skip_objects(self, object_ids: list[int]) -> bool:
        """Abandon one or more plate objects during a live multi-object print.

        Publishes Bambu's ``skip_objects`` print command.  The printer stops
        laying down the named objects and finishes the rest of the plate — so
        one failed part on a full plate no longer forces you to scrap the whole
        run.

        The ids are the per-object label ids Bambu assigns at slice time: the
        same numbers ``list_plate_objects`` returns as ``label_id`` and that the
        printer reports as already-skipped in its status ``s_obj`` list.  They
        are cumulative on the firmware side — each call adds to the skip set;
        an object already skipped stays skipped.

        This is irreversible for the objects named, and only meaningful while a
        multi-object plate is actively printing.

        Args:
            object_ids: Label ids of the objects to abandon (non-empty).

        Returns:
            ``True`` once the command is published.

        Raises:
            PrinterError: If *object_ids* is empty or holds a non-integer id.
        """
        if not object_ids:
            raise PrinterError("skip_objects requires at least one object id.")
        try:
            ids = [int(x) for x in object_ids]
        except (TypeError, ValueError) as exc:
            raise PrinterError(f"skip_objects: object ids must be integers ({exc}).") from exc
        self._publish_command(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "skip_objects",
                    "obj_list": ids,
                }
            }
        )
        return True

    # ------------------------------------------------------------------
    # Bambu-specific: LED control
    # ------------------------------------------------------------------

    def set_light(self, node: str, mode: str) -> bool:
        """Control the printer's LED lights.

        Args:
            node: Light to control — ``"chamber_light"`` or ``"work_light"``.
            mode: ``"on"``, ``"off"``, or ``"flashing"``.

        Returns:
            ``True`` if the command was accepted.

        Raises:
            PrinterError: If *node* or *mode* is invalid.
        """
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
        return True

    # ------------------------------------------------------------------
    # Bambu-specific: fan control
    # ------------------------------------------------------------------

    def set_fan(self, node: str, percent: int) -> bool:
        """Set the speed of one of the printer's fans.

        Drives the fan with Bambu's standard ``M106 P<n> S<0-255>`` G-code
        over the same MQTT ``gcode_line`` path this adapter already uses for
        temperature control, so no live-print state is required beyond an
        active MQTT connection.

        Args:
            node: Which fan to set — ``"part"`` (part-cooling / model fan),
                ``"aux"`` (auxiliary / big fan), or ``"chamber"`` (chamber /
                exhaust fan).  ``"part_cooling"``, ``"cooling"``, and
                ``"auxiliary"`` are accepted aliases.
            percent: Fan speed 0-100 (0 turns the fan off, 100 is full speed).

        Returns:
            ``True`` once the command is published.

        Raises:
            PrinterError: If *node* is not a known fan or *percent* is outside
                0-100.

        Note:
            The chamber fan only exists on enclosed models — per Kiln's own
            printer intelligence data (``has_enclosure``): X1 Carbon, X1E, P1S,
            P2S, and H2S.  Open-frame models — A1, A1 Mini, A2L, and P1P — have
            no chamber fan, and a chamber command there is a firmware no-op.
            The printer's own thermal management may override a manual fan
            speed during a print.
        """
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
        self.send_gcode([f"M106 P{index} S{speed}"])
        return True

    # ------------------------------------------------------------------
    # AMS (Automatic Material System)
    # ------------------------------------------------------------------

    def _peek_loaded_ams_trays(self) -> list[dict[str, Any]] | None:
        """Return loaded AMS trays from cached MQTT status, without any I/O.

        Unlike ``get_ams_status``, this does not trigger a pushall request
        when the cache is empty — it returns ``None`` instead.  Intended
        for auto-routing decisions where an extra MQTT round-trip per
        ``start_print`` call would be wasteful.

        :returns: List of loaded-tray dicts (``tray_type`` non-empty), or
            ``None`` if no AMS data is cached yet.  Empty list means AMS
            is attached but no trays have filament.
        """
        try:
            status = self._get_cached_status()
        except Exception:
            return None

        ams_data = status.get("ams")
        if isinstance(ams_data, dict):
            ams_data = ams_data.get("ams")
        if not isinstance(ams_data, list):
            return None

        loaded: list[dict[str, Any]] = []
        for unit in ams_data:
            if not isinstance(unit, dict):
                continue
            raw_trays = unit.get("tray")
            if not isinstance(raw_trays, list):
                continue
            for tray in raw_trays:
                if not isinstance(tray, dict):
                    continue
                if tray.get("tray_type"):
                    loaded.append({
                        "slot": tray.get("id", 0),
                        "tray_type": tray.get("tray_type", ""),
                        "tray_color": tray.get("tray_color", ""),
                    })
        return loaded

    def get_ams_status(self) -> dict[str, Any]:
        """Query AMS status: what's loaded in each tray.

        Returns a dict with structure::

            {
                "ams_exist_bits": "1",
                "tray_exist_bits": "f",
                "tray_now": "0",
                "units": [
                    {
                        "unit_id": 0,
                        "humidity": 3,
                        "trays": [
                            {
                                "slot": 0,
                                "tray_type": "PLA",
                                "tray_color": "FF0000FF",
                                "remain": 85,
                                "tag_uid": "...",
                                "nozzle_temp_min": 190,
                                "nozzle_temp_max": 230,
                                "bed_temp": 60,
                            },
                            ...
                        ]
                    }
                ]
            }

        Returns an empty ``units`` list if no AMS data is available
        (e.g. printer not connected or no AMS attached).

        Raises:
            PrinterError: If the MQTT connection is not available.
        """
        status = self._get_cached_status()
        ams_data = status.get("ams")

        # A1/AMS Lite printers don't push AMS data as frequently as X1/P1.
        # If the cache has no AMS data yet, request a full status update
        # and give the printer a moment to respond.
        if not ams_data:
            self._publish_command(
                {"pushing": {"sequence_id": self._next_seq(), "command": "pushall"}}
            )
            time.sleep(min(2.0, self._timeout / 2))
            status = self._get_cached_status()
            ams_data = status.get("ams")

        # Bambu printers may nest AMS data as a dict wrapper containing an
        # inner "ams" list alongside top-level fields like ams_exist_bits.
        # Unwrap the dict to get the actual unit list.
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
        # A1 / AMS Lite firmware may keep ``tray_now`` at 255 even when
        # the AMS Lite is loaded.  Preserve the adjacent selection fields
        # so higher-level tools can avoid falsely reporting external-spool
        # feed when the wrapper names a selected or target tray.
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

        if not isinstance(ams_data, list):
            return result

        # AMS Lite (A1 / A1 mini) has no humidity sensor — the firmware
        # still reports a `humidity` field, but it's a fixed placeholder,
        # not a measurement.  Flag it so callers don't present it as real.
        humidity_known = self._printer_model not in ("bambu_a1", "bambu_a1_mini")

        # AMS unit TYPE isn't in print.ams.ams[]; it's encoded in the firmware
        # module name ("ams_f1/0" = AMS Lite unit 0, "n3f/N" = AMS 2 Pro,
        # "n3s/N" = AMS HT, "ams/N" = AMS).  Fetch the module list once
        # (cached); prefix interpretation lives downstream, not here.
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
            # AMS 2 Pro / AMS HT active-drying telemetry (absent on AMS /
            # AMS Lite, which have no dryer).  Raw passthrough only — the
            # interpretation (true-% vs legacy index, the duration:0 sentinel)
            # lives downstream, not here.
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
                    # Bambu's AMS has no scale: a `remain` % is only real
                    # when it comes from a spool's RFID tag.  AMS Lite and
                    # non-RFID spools report tag_uid as all zeros — there
                    # `remain` is a placeholder, so flag it as not known so
                    # callers don't render it as "0% / empty".
                    tag_uid = str(tray.get("tag_uid") or "").strip()
                    remaining_known = bool(tag_uid) and set(tag_uid) != {"0"}
                    trays.append({
                        "slot": slot_id,
                        "tray_type": tray.get("tray_type", ""),
                        "tray_color": tray.get("tray_color", ""),
                        "remain": remain_int,
                        "remaining_known": remaining_known,
                        "tag_uid": tag_uid,
                        "nozzle_temp_min": nozzle_min,
                        "nozzle_temp_max": nozzle_max,
                        "bed_temp": bed_t,
                    })

            unit_out: dict[str, Any] = {
                "unit_id": unit_id,
                "humidity": humidity_int,
                "humidity_known": humidity_known,
                "trays": trays,
            }
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

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def send_gcode(self, commands: list[str]) -> bool:
        """Send G-code commands to the Bambu printer via MQTT.

        Joins commands with newlines and sends as a ``gcode_line`` command.

        Args:
            commands: List of G-code command strings.

        Returns:
            ``True`` if the commands were accepted.

        Raises:
            PrinterError: If sending fails.
        """
        script = "\n".join(commands)
        self._publish_command(
            {
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "gcode_line",
                    "param": script,
                }
            }
        )
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- file deletion
    # ------------------------------------------------------------------

    def delete_file(self, file_path: str) -> bool:
        """Delete a file from the printer's SD card via FTPS.

        Args:
            file_path: Path of the file on the printer (e.g.
                ``"/sdcard/model.3mf"``).

        Returns:
            ``True`` if the file was deleted.

        Raises:
            PrinterError: If deletion fails.
        """
        try:
            ftp = self._ftp_connect()
        except PrinterError:
            raise

        # Sanitise path — only allow files under /sdcard/ or /cache/.
        # The printer's SD card uses POSIX paths, so normalize with
        # posixpath; os.path.normpath would mangle the separators to
        # backslashes on a Windows host.
        safe_path = posixpath.normpath(file_path)
        if not safe_path.startswith("/sdcard/") and not safe_path.startswith("/cache/"):
            raise PrinterError(f"File path must be under /sdcard/ or /cache/, got: {file_path!r}")

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
        """Capture a webcam snapshot from the printer's camera.

        Bambu printers use two different camera protocols:

        * **A1 / A1 Mini / P1P / P1S**: TLS + JPEG streaming on port 6000.
          A custom 80-byte auth packet is sent, then JPEG frames are read
          from the socket.  No ffmpeg required.

        * **X1C / X1 / P2S**: RTSPS on port 322 via ffmpeg.

        This method tries port 6000 first (works for A1/P1 series), and
        falls back to RTSPS if port 6000 is not available.

        Raises:
            PrinterError: If both camera protocols fail.
        """
        # Try the TLS+JPEG protocol first (A1/P1 series, port 6000).
        try:
            frame = self._capture_jpeg_frame()
            if frame:
                return frame
        except Exception:
            logger.debug("Port 6000 JPEG capture failed, trying RTSPS fallback", exc_info=True)

        # Fallback to RTSPS (X1 series, port 322) via ffmpeg.
        return self._capture_rtsps_frame()

    def _capture_jpeg_frame(self, *, timeout: float = 5.0) -> bytes | None:
        """Capture a JPEG frame via the TLS+JPEG protocol on port 6000.

        The A1/P1 series printers stream sequential JPEG frames over a
        TLS socket.  Authentication uses an 80-byte binary packet with
        the username and LAN access code.

        :param timeout: Maximum time in seconds to wait for a complete frame.
        :returns: JPEG bytes, or ``None`` if capture fails.
        """
        import struct
        import time

        _CAMERA_PORT = 6000
        _JPEG_SOI = b"\xff\xd8\xff"  # JPEG Start of Image
        _JPEG_EOI = b"\xff\xd9"      # JPEG End of Image

        # Build 80-byte auth packet.
        auth_data = struct.pack("<II", 0x40, 0x3000)
        auth_data += struct.pack("<II", 0, 0)
        auth_data += _MQTT_USERNAME.encode("ascii").ljust(32, b"\x00")
        auth_data += self._access_code.encode("ascii").ljust(32, b"\x00")

        ctx = self._build_tls_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        sock = socket.create_connection((self._host, _CAMERA_PORT), timeout=timeout)
        try:
            ssock = ctx.wrap_socket(sock, server_hostname=self._host)
        except ssl.SSLError as exc:
            sock.close()
            logger.debug("Camera TLS handshake failed: %s", exc)
            return None

        try:
            ssock.sendall(auth_data)

            buf = b""
            start_time = time.monotonic()
            while time.monotonic() - start_time < timeout:
                chunk = ssock.recv(8192)
                if not chunk:
                    break
                buf += chunk

                start_idx = buf.find(_JPEG_SOI)
                if start_idx == -1:
                    continue

                end_idx = buf.find(_JPEG_EOI, start_idx + 3)
                if end_idx != -1:
                    return buf[start_idx : end_idx + 2]
        except (TimeoutError, OSError) as exc:
            logger.debug("Camera JPEG read failed: %s", exc)
        finally:
            ssock.close()

        return None

    def _capture_rtsps_frame(self) -> bytes | None:
        """Capture a frame via RTSPS on port 322 using ffmpeg (X1 series)."""
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            raise PrinterError(
                "Camera snapshot requires either a port-6000 JPEG stream "
                "(A1/P1 series) or ffmpeg for RTSPS (X1 series). "
                "Neither is available. Install ffmpeg if using an X1 printer."
            )

        stream_url = self._raw_stream_url()

        try:
            result = subprocess.run(
                [
                    ffmpeg, "-y",
                    "-rtsp_transport", "tcp",
                    "-i", stream_url,
                    "-frames:v", "1",
                    "-f", "image2",
                    "-vcodec", "mjpeg",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout and len(result.stdout) > 100:
                return result.stdout
            raise PrinterError(
                f"Camera RTSPS snapshot failed (ffmpeg exit {result.returncode}). "
                f"Check that the printer camera is enabled."
            )
        except PrinterError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise PrinterError(
                "Camera RTSPS stream timed out after 5s. Check camera and network."
            ) from exc
        except Exception as exc:
            raise PrinterError(
                f"Camera snapshot failed: {exc}\n"
                "Camera may be disabled or in use. Check printer camera settings. "
                "Retry with `get_snapshot()`.",
            ) from exc

    def _raw_stream_url(self) -> str:
        """Return the real RTSPS URL with embedded credentials (internal use only)."""
        return f"rtsps://bblp:{self._access_code}@{self._host}:322/streaming/live/1"

    def get_stream_url(self) -> str | None:
        """Return the RTSPS stream URL for X1 series printers.

        X1C/X1 printers expose an RTSP stream at port 322.  A1/P1
        printers use port 6000 with a proprietary JPEG protocol instead
        (handled by :meth:`_capture_jpeg_frame`).

        The access code is masked so the URL is safe for logging and
        tool responses.  Internal methods that need the real credential
        call :meth:`_raw_stream_url` directly.
        """
        return f"rtsps://bblp:****@{self._host}:322/streaming/live/1"

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """Disconnect the MQTT client and release resources."""
        if self._mqtt_client is not None:
            client = self._mqtt_client
            self._mqtt_client = None
            self._safe_stop_client(client)
            self._mqtt_connected.clear()
            with self._state_lock:
                self._connected = False

    def update_credentials(self, access_code: str) -> None:
        """Update the access code and force MQTT reconnection.

        Bambu printers rotate their LAN access code periodically.  Call
        this method to update the stored credential without recreating
        the entire adapter instance.

        :param access_code: New LAN access code from the printer's screen.
        """
        self._access_code = access_code
        # Force MQTT reconnection with new credentials on next operation.
        if self._mqtt_client is not None:
            self._safe_stop_client(self._mqtt_client)
            self._mqtt_client = None
            self._mqtt_connected.clear()
            with self._state_lock:
                self._connected = False
        logger.info("Access code updated; MQTT will reconnect on next operation")

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<BambuAdapter host={self._host!r} serial={self._serial!r}>"

    def __del__(self) -> None:
        # Don't call full disconnect() from GC — loop_stop() can deadlock
        # during interpreter shutdown.  Just send DISCONNECT and drop ref.
        if hasattr(self, "_mqtt_client") and self._mqtt_client is not None:
            with contextlib.suppress(Exception):
                self._mqtt_client.disconnect()
            self._mqtt_client = None
