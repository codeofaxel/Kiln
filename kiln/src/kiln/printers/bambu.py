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
_STALE_STATE_MAX_AGE: float = 60.0  # seconds — max age before cached state is "too old"


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
# See: wiki.bambulab.com/en/a1-mini/troubleshooting/hmscode/0300_1A00_0002_0001
_NOZZLE_CLUMP_ERROR_PREFIXES: tuple[str, ...] = (
    "03008014",   # Nozzle clumping detection by probing (A1 series)
    "03001A00",   # Nozzle wrapped in filament / plate placement
    "03001800",   # Nozzle clumping calibration failure
)

_NOZZLE_CLUMP_MESSAGE = (
    "Nozzle clumping / blob detection triggered (HMS 0300-xxxx). "
    "This is often a false positive on models with thin first-layer geometry "
    "(grips, cases, bezels). FIX: Retry with nozzle_clog_detect=False to "
    "bypass the eddy-current probe at layers 4/11/20. "
    "CLI: kiln print <file> --no-nozzle-check. "
    "MCP: start_print(file, nozzle_clog_detect=False)."
)


def _is_nozzle_clump_error(error_code: int) -> bool:
    """Check if an error code matches a known nozzle clumping HMS code."""
    hex_code = f"{error_code:08X}"
    return any(hex_code.startswith(prefix) for prefix in _NOZZLE_CLUMP_ERROR_PREFIXES)

# Bambu LED node names.
_VALID_LED_NODES: frozenset[str] = frozenset({"chamber_light", "work_light"})
_VALID_LED_MODES: frozenset[str] = frozenset({"on", "off", "flashing"})

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
    # Human-readable names (from slicer config / XML metadata)
    "Bambu Lab A1 mini": "a1_mini",
    "Bambu Lab A1": "a1",
    "Bambu Lab X1 Carbon": "x1c",
    "Bambu Lab X1E": "x1e",
    "Bambu Lab P1S": "p1s",
    "Bambu Lab P1P": "p1p",
    # Serial number prefixes (first 3 chars of Bambu serial)
    "030": "a1_mini",
    "039": "a1",
    "01S": "x1c",
    "01P": "p1s",
}


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

        # State cache -- updated by MQTT messages.
        self._state_lock = threading.Lock()
        self._last_status: dict[str, Any] = {}
        self._last_state_time: float = 0.0  # monotonic time of last accepted update
        self._connected = False
        self._sequence_id = 0

        # MQTT client.
        self._mqtt_client: mqtt.Client | None = None
        self._mqtt_connected = threading.Event()

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
            logger.warning(
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
            can_snapshot=_find_ffmpeg() is not None,
            can_stream=True,
            supported_extensions=(".3mf", ".gcode", ".gco"),
        )

    # ------------------------------------------------------------------
    # Internal: MQTT
    # ------------------------------------------------------------------

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
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
            except Exception as exc:
                logger.debug("Failed to tear down stale MQTT client: %s", exc)
            self._mqtt_client = None

        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"kiln-{self._serial[:8]}",
                protocol=mqtt.MQTTv311,
            )
            client.username_pw_set(_MQTT_USERNAME, self._access_code)

            tls_context = self._build_tls_context()
            client.tls_set_context(tls_context)

            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.on_disconnect = self._on_disconnect

            self._mqtt_connected.clear()
            client.connect(self._host, _MQTT_PORT, keepalive=60)
            client.loop_start()

            # Wait for the connection to be established.
            if not self._mqtt_connected.wait(timeout=self._timeout):
                client.loop_stop()
                self._backoff.record_failure()
                raise PrinterError(
                    f"MQTT connection to {self._host} timed out after "
                    f"{self._timeout}s. Check network connectivity and access code.\n"
                    "  Checklist:\n"
                    "  1) Printer is powered on and on the same network\n"
                    "  2) LAN Access Code is correct (printer → Settings → Network)\n"
                    "  3) LAN Mode is enabled on the printer\n"
                    "  4) Port 8883 is not blocked by a firewall\n"
                    "  Try: kiln verify"
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
                try:
                    client.loop_stop()
                    client.disconnect()
                except Exception:
                    pass
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
        client.subscribe(self._topic_report)
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

        # Merge print status fields into our cache.
        # A1/A1 mini may send command as "push_status" or "PUSH_STATUS".
        print_data = payload.get("print", {})
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
                    self._last_status.update(print_data)
                    self._last_state_time = time.monotonic()

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
            result = c.publish(
                self._topic_request,
                json.dumps(payload),
                qos=1,
            )
            result.wait_for_publish(timeout=self._timeout)
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
           disables the eddy-current probing at layers 4/11/20 and
           prevents ``print_halt`` on detection.

        These commands must be sent **before** the ``project_file``
        command to take effect for the upcoming print.

        Note: The A1's nozzle clumping detection is also hardcoded into
        the timelapse G-code section at layer 3.  For complete bypass,
        users should also edit the slicer's machine G-code to remove
        or skip the timelapse probing (change ``{if layer_num == 2}``
        to ``{if layer_num == 20000}``).
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
        """Open an FTPS connection to the printer.

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

    def _build_state_from_cache(self, status: dict[str, Any]) -> PrinterState:
        """Convert a cached status dict into a :class:`PrinterState`."""
        gcode_state = status.get("gcode_state", "unknown")
        if not isinstance(gcode_state, str):
            gcode_state = "unknown"
        # A1/A1 mini sends uppercase state values (e.g. "RUNNING", "IDLE").
        gcode_state = gcode_state.lower()

        mapped = _STATE_MAP.get(gcode_state, PrinterStatus.UNKNOWN)

        # After a cancelled print the MQTT cache can get stuck with
        # gcode_state="failed" even though the printer is actually idle.
        # When print_error is explicitly present and equals 0 (no real error),
        # this is a stale post-cancel state — treat it as IDLE so preflight
        # checks pass.  If print_error is absent we conservatively keep ERROR.
        if mapped == PrinterStatus.ERROR:
            raw_error = status.get("print_error")
            if raw_error is not None:
                error_val: int = -1
                with contextlib.suppress(TypeError, ValueError):
                    error_val = int(raw_error)
                if error_val == 0:
                    mapped = PrinterStatus.IDLE

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
        )

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures.

        Uses the MQTT status cache, which is updated by periodic pushes
        from the printer and explicit ``pushall`` requests.

        During a backoff cooldown period, returns the last known state if
        it is recent enough (< :data:`_STALE_STATE_MAX_AGE` seconds old),
        otherwise returns OFFLINE without attempting reconnection.
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
                    return self._build_state_from_cache(dict(self._last_status))
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

        return self._build_state_from_cache(status)

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

        # Estimate elapsed time from completion and remaining.
        print_time_seconds: int | None = None
        print_time_left_seconds: int | None = None

        if mc_remaining is not None:
            print_time_left_seconds = int(mc_remaining) * 60

        if completion is not None and completion > 0 and print_time_left_seconds is not None:
            # total_est = remaining / (1 - completion/100)
            fraction_left = 1.0 - (completion / 100.0)
            if fraction_left > 0:
                total_est = print_time_left_seconds / fraction_left
                print_time_seconds = max(0, int(total_est - print_time_left_seconds))

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

        A1 series printers store files at ``/model/`` while X1/P1 series
        use ``/sdcard/``.  Tries ``/model/`` first (A1), falls back to
        ``/sdcard/`` if CWD fails.

        Returns:
            The storage path (e.g. ``"/model"`` or ``"/sdcard"``).
        """
        for path in ("/model", "/sdcard"):
            try:
                ftp.cwd(path)
                logger.debug("Detected Bambu storage path: %s", path)
                return path
            except ftplib.error_perm:
                continue
        # Default fallback.
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
    ) -> str:
        """Wrap PrusaSlicer gcode in a Bambu-compatible 3MF.

        The Bambu A1 requires BambuStudio's proprietary start/end gcode
        (including ``M620 M`` motor enable, AMS load, extrusion calibration)
        for the extruder to function.  This method wraps raw PrusaSlicer
        output with those sequences and packages everything as a 3MF.

        :param gcode_path: Path to PrusaSlicer ``.gcode`` output (must be
            sliced with ``--use-relative-e-distances`` and empty start/end).
        :param hotend_temp: Hotend temperature in °C (default 220 for PLA).
        :param bed_temp: Bed temperature in °C (default 65 for PLA).
        :param filament_type: Filament type string (PLA, PETG, ABS, etc.).
        :param source_3mf_path: Optional source 3MF for thumbnails/geometry.
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
        )

        result = build_bambu_3mf(
            gcode_body,
            output_path,
            settings=settings,
            source_3mf_path=source_3mf_path,
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
        """Build the correct ``file:///`` URL for the MQTT print command.

        Bambu firmware reads files from the filesystem, not FTP.  The URL
        must always use ``file:///sdcard/model/`` (which maps to the FTPS
        ``/model/`` path on A1 series, or ``/sdcard/`` on X1/P1 which also
        works with this path).

        Using ``ftp:///`` URLs causes HMS error 0500-C010-010800
        ("MicroSD Card read/write exception") on A1 printers.
        """
        return f"file:///sdcard/model/{basename}"

    def start_print(self, file_name: str, **kwargs: Any) -> PrintResult:
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
                  Default ``[0]``.  Use ``[-1]`` for unused slots.
                * ``timelapse`` (bool): Record timelapse.  Default ``False``.
                * ``bed_leveling`` (bool): Run bed leveling.  Default ``True``.
                * ``flow_cali`` (bool): Run flow calibration.  Default ``False``.
                * ``vibration_cali`` (bool): Run vibration calibration.
                  Default ``False``.
                * ``layer_inspect`` (bool): Enable first-layer inspection
                  (lidar visual scan).  Default ``False``.
                * ``nozzle_clog_detect`` (bool): Enable nozzle clumping /
                  blob detection by probing (eddy current sensor check at
                  layers 4, 11, 20).  Default ``True``.  Set to ``False``
                  to bypass HMS 0300-8014 errors that trigger on models
                  with thin first-layer geometry.  This sends both a
                  ``print_option`` and ``xcam_control_set`` command to
                  disable the check before starting the print.
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

            # Fall back to single-filament defaults.
            if ams_mapping is None:
                ams_mapping = [0]
            if not isinstance(ams_mapping, list):
                ams_mapping = [0]

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
                path = os.path.normpath(file_name)
                if not (path.startswith("/sdcard/") or path.startswith("/cache/")):
                    raise PrinterError(f"File path must be under /sdcard/ or /cache/, got: {file_name!r}")
            else:
                if self._last_storage_path == "/sdcard":
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
            if result_state in ("timeout", "failed"):
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
                        f"Print command sent for {basename} but printer did not "
                        f"transition to an active state within timeout."
                        f"{err_detail}"
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

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job."""
        self._send_print_command("pause")
        return PrintResult(success=True, message="Print paused.")

    def resume_print(self) -> PrintResult:
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

    def set_tool_temp(self, target: float) -> bool:
        """Set the hotend target temperature via G-code over MQTT."""
        self._validate_temp(target, 300.0, "Hotend")
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
    # AMS (Automatic Material System)
    # ------------------------------------------------------------------

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

        if not isinstance(ams_data, list):
            return result

        for unit in ams_data:
            if not isinstance(unit, dict):
                continue
            unit_id = unit.get("id", 0)
            humidity = unit.get("humidity")
            humidity_int: int | None = None
            if humidity is not None:
                with contextlib.suppress(TypeError, ValueError):
                    humidity_int = int(humidity)

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
                    trays.append({
                        "slot": slot_id,
                        "tray_type": tray.get("tray_type", ""),
                        "tray_color": tray.get("tray_color", ""),
                        "remain": remain_int,
                        "tag_uid": tray.get("tag_uid", ""),
                        "nozzle_temp_min": nozzle_min,
                        "nozzle_temp_max": nozzle_max,
                        "bed_temp": bed_t,
                    })

            result["units"].append({
                "unit_id": unit_id,
                "humidity": humidity_int,
                "trays": trays,
            })

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

        # Sanitise path — only allow files under /sdcard/ or /cache/
        safe_path = os.path.normpath(file_path)
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

    def _capture_jpeg_frame(self, *, timeout: float = 15.0) -> bytes | None:
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

        stream_url = self.get_stream_url()
        if not stream_url:
            return None

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
                timeout=10,
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
                "Camera RTSPS stream timed out after 10s. Check camera and network."
            ) from exc
        except Exception as exc:
            raise PrinterError(
                f"Camera snapshot failed: {exc}\n"
                "Camera may be disabled or in use. Check printer camera settings. "
                "Retry with `get_snapshot()`.",
            ) from exc

    def get_stream_url(self) -> str | None:
        """Return the RTSPS stream URL for X1 series printers.

        X1C/X1 printers expose an RTSP stream at port 322.  A1/P1
        printers use port 6000 with a proprietary JPEG protocol instead
        (handled by :meth:`_capture_jpeg_frame`).
        """
        return f"rtsps://bblp:{self._access_code}@{self._host}:322/streaming/live/1"

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """Disconnect the MQTT client and release resources."""
        if self._mqtt_client is not None:
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
            except Exception as exc:
                logger.debug("Failed to disconnect MQTT client: %s", exc)
            self._mqtt_client = None
            self._mqtt_connected.clear()
            with self._state_lock:
                self._connected = False

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<BambuAdapter host={self._host!r} serial={self._serial!r}>"

    def __del__(self) -> None:
        if hasattr(self, "_mqtt_client"):
            self.disconnect()
