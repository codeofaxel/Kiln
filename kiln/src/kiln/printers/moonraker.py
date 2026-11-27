"""Klipper/Moonraker adapter for the Kiln printer abstraction layer."""

from __future__ import annotations

import contextlib
import json as _json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any, ClassVar
from urllib.parse import quote

import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import RequestException, Timeout

from kiln import plain_words as _plain_words
from kiln.printers import read_back as _read_back
from kiln.printers.base import (
    DEFAULT_LOAD_LENGTH_MM,
    DEFAULT_PURGE_LENGTH_MM,
    DEFAULT_UNLOAD_LENGTH_MM,
    ActiveSlotReading,
    FilamentHandlingUnsupported,
    FilamentOpPlan,
    FilamentOpResult,
    FirmwareComponent,
    FirmwareStatus,
    FirmwareUpdateResult,
    JobProgress,
    JobResult,
    MachineIdentity,
    NozzleSetting,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from kiln.printers.command_verdict import CommandVerdict
from kiln.quoted_text import fence

try:
    import websocket as _ws_mod  # websocket-client

    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ws_mod = None  # type: ignore[assignment]
    _WS_AVAILABLE = False

logger = logging.getLogger(__name__)

# HTTP status codes eligible for automatic retry.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({502, 503, 504})

_STATE_MAP: dict[str, PrinterStatus] = {
    "ready": PrinterStatus.IDLE,
    "printing": PrinterStatus.PRINTING,
    "paused": PrinterStatus.PAUSED,
    "error": PrinterStatus.ERROR,
    "shutdown": PrinterStatus.OFFLINE,
    "startup": PrinterStatus.BUSY,
    "standby": PrinterStatus.IDLE,
    "complete": PrinterStatus.IDLE,
    "cancelled": PrinterStatus.IDLE,
}

# How the last job ENDED, for the ``print_stats.state`` values that say so
_JOB_RESULT_MAP: dict[str, JobResult] = {
    "complete": JobResult.COMPLETED,
    "cancelled": JobResult.CANCELLED,
}


def _map_moonraker_job_result(print_state: str | None) -> JobResult | None:
    """How the last job ended, per ``print_stats.state``, or ``None``."""
    if not print_state:
        return None
    return _JOB_RESULT_MAP.get(print_state)


# Klipper's two filament runout sensor modules
_FILAMENT_SENSOR_TYPES: tuple[str, ...] = (
    "filament_switch_sensor",
    "filament_motion_sensor",
)

#: The extensions Klipper's ``SDCARD_PRINT_FILE`` opens, compared the way
#: Klipper compares them: the text after the name's last dot, capitals and all.
_KLIPPER_PRINT_EXTS: frozenset[str] = frozenset({"gcode", "g", "gco"})

#: Bytes taken from a download at a time when a file is read back.
_READ_BACK_CHUNK_BYTES: int = 1024 * 1024

#: A name Klipper's quoted start command could not hold as one name.
_UNSAFE_START_NAME = re.compile(r'["\\\x00-\x1f\x7f]')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _printer_words(response: Any) -> str:
    """Klipper's own reason for refusing a request (``{"error": {"message": ...}}``),
    as a sentence, or ``""`` when the reply carries none."""
    try:
        message = _safe_get(response.json(), "error", "message", default="")
    except ValueError:
        return ""
    message = message.strip()[:300] if isinstance(message, str) else ""
    return message if not message or message.endswith((".", "!", "?")) else message + "."


def _klipper_prints(path: str) -> bool:
    """Whether Klipper counts *path* as a print file it can start."""
    name = path.rsplit("/", 1)[-1]
    return name[name.rfind(".") + 1 :] in _KLIPPER_PRINT_EXTS


def _download_refused(status_code: int) -> str:
    """Why Moonraker refused a download, as a clause of a sentence."""
    if status_code == 401:
        return "Moonraker didn't accept the API key"
    if status_code == 403:
        return "Moonraker doesn't allow Kiln to download that file"
    if status_code == 404:
        return "the printer has no file by that name"
    if status_code >= 500:
        return "Moonraker ran into a problem of its own"
    return "Moonraker refused the download"


def _safe_get(data: Any, *keys: str, default: Any = None) -> Any:
    """Walk nested dicts safely, returning *default* on any miss or type error."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
    return current


def _merge_status_into(cache: dict[str, Any], status: dict[str, Any]) -> None:
    """Merge a Moonraker status payload into *cache*, per printer object."""
    for obj_name, value in status.items():
        existing = cache.get(obj_name)
        if isinstance(value, dict) and isinstance(existing, dict):
            cache[obj_name] = {**existing, **value}
        else:
            cache[obj_name] = value


def _map_moonraker_state(state_string: str, print_state: str | None = None) -> PrinterStatus:
    """Translate a Moonraker state string to a :class:`PrinterStatus`."""
    if state_string == "ready" and print_state:
        mapped = _STATE_MAP.get(print_state)
        if mapped is not None:
            return mapped

    return _STATE_MAP.get(state_string, PrinterStatus.UNKNOWN)


_FLOW_ANOMALY_JAM_SUBSTRINGS: tuple[str, ...] = (
    "filament sensor",  # Klipper runout / motion sensor trip
    "filament runout",  # Some configs spell it explicitly
    "runout detected",
    "filament jam",
    "filament stuck",
)

_FLOW_ANOMALY_UNDER_EXTRUSION_SUBSTRINGS: tuple[str, ...] = (
    "under extrusion",
    "under-extrusion",
    "extruder shutdown",  # Klipper extruder failure
    "extruder not",       # "Extruder not ready" / "Extruder not heating"
    "no trigger on extruder",  # filament_motion_sensor wording
)


def _classify_flow_anomaly(
    print_state: str | None,
    message: str | None,
) -> tuple[str, str] | None:
    """Return (event_type, severity) for a Moonraker/Klipper flow signal."""
    if print_state != "error":
        return None
    if not message or not isinstance(message, str):
        return None
    msg_lower = message.lower()
    for substring in _FLOW_ANOMALY_JAM_SUBSTRINGS:
        if substring in msg_lower:
            return "filament_jam", "high"
    for substring in _FLOW_ANOMALY_UNDER_EXTRUSION_SUBSTRINGS:
        if substring in msg_lower:
            return "under_extrusion", "medium"
    return None


# ---------------------------------------------------------------------------
# Feeding slot: which slot a multi-material unit reports it is on
# ---------------------------------------------------------------------------

_ACTIVE_SLOT_UNIT_KEYWORDS: frozenset[str] = frozenset(
    {"mmu", "ercf", "afc", "box", "ams", "ace", "cfs", "filament_box"}
)

_ACTIVE_SLOT_NOT_A_UNIT: frozenset[str] = frozenset(
    {"filament_switch_sensor", "filament_motion_sensor"}
)

_ACTIVE_SLOT_GENERIC_FIELDS: tuple[str, ...] = (
    "gate",
    "lane",
    "slot",
    "active_slot",
    "current_slot",
    "current_lane",
    "tool",
)


def _looks_like_unit_object(name: str) -> bool:
    """Whether a Klipper object name's type reads as a multi-material unit."""
    kind = name.split(" ", 1)[0].casefold()
    if not kind or kind in _ACTIVE_SLOT_NOT_A_UNIT:
        return False
    tokens = set(kind.split("_")) | {kind}
    return not tokens.isdisjoint(_ACTIVE_SLOT_UNIT_KEYWORDS)


def _active_slot_unit_objects(names: list[str]) -> list[str]:
    """The Klipper objects worth querying for a feeding slot, from the object list."""
    if "mmu" in names:
        return ["mmu"]
    if "AFC" in names:
        return ["AFC"]
    matched = [name for name in names if _looks_like_unit_object(name)]
    bare = [name for name in matched if " " not in name]
    instanced = [name for name in matched if " " in name]
    return bare + instanced


def _slot_id(value: Any) -> tuple[bool, str | None]:
    """``(is_an_id, slot)`` for a unit's slot value."""
    if isinstance(value, bool):
        return False, None
    if isinstance(value, float):
        if not value.is_integer():
            return False, None
        value = int(value)
    if isinstance(value, int):
        return True, (None if value < 0 else str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return True, None
        try:
            number = int(text)
        except ValueError:
            return True, text
        return True, (None if number < 0 else str(number))
    return False, None


def _happy_hare_reading(mmu: dict[str, Any]) -> ActiveSlotReading | None:
    """Happy Hare's ``mmu`` object."""
    raw = mmu.get("gate")
    if raw is None:
        raw = mmu.get("tool")
    if raw is None:
        return None
    is_id, slot = _slot_id(raw)
    if not is_id:
        return None
    return ActiveSlotReading(slot=slot, source="moonraker:mmu", verified=False)


def _afc_reading(afc: dict[str, Any]) -> ActiveSlotReading | None:
    """AFC's ``AFC`` object: ``current_load`` is the lane name loaded to the toolhead and ``None`` when nothing is."""
    if "current_load" not in afc:
        return None
    raw = afc.get("current_load")
    if raw is None:
        return ActiveSlotReading(slot=None, source="moonraker:AFC", verified=False)
    is_id, slot = _slot_id(raw)
    if not is_id:
        return None
    return ActiveSlotReading(slot=slot, source="moonraker:AFC", verified=False)


def _active_slot_from_klipper_objects(
    status: dict[str, Any], names: list[str]
) -> ActiveSlotReading | None:
    """One :class:`ActiveSlotReading` from a ``printer.objects.query`` status."""
    mmu = status.get("mmu")
    if isinstance(mmu, dict):
        return _happy_hare_reading(mmu)
    afc = status.get("AFC")
    if isinstance(afc, dict):
        return _afc_reading(afc)
    for name in names:
        unit = status.get(name)
        if not isinstance(unit, dict):
            continue
        for field in _ACTIVE_SLOT_GENERIC_FIELDS:
            raw = unit.get(field)
            if raw is None:
                continue
            is_id, slot = _slot_id(raw)
            if not is_id:
                continue
            return ActiveSlotReading(slot=slot, source=f"moonraker:{name}", verified=False)
    return None


# ---------------------------------------------------------------------------
# WebSocket push monitor
# ---------------------------------------------------------------------------

def _push_monitoring_enabled() -> bool:
    """Check whether push monitoring is enabled (read at call time, not import time)."""
    return os.environ.get("KILN_PUSH_MONITORING", "0") == "1"

# Maximum reconnect backoff in seconds.
_MAX_RECONNECT_BACKOFF: float = 30.0


class MoonrakerWebSocketMonitor:
    """Push-based status monitor for Moonraker via its native WebSocket API."""

    _SUBSCRIBE_OBJECTS: dict[str, list[str] | None] = {
        "print_stats": None,
        "heater_bed": None,
        "extruder": None,
        "display_status": None,
    }

    def __init__(
        self,
        host: str,
        *,
        on_state_update: Callable[[dict[str, Any]], None] | None = None,
        printer_name: str = "moonraker",
    ) -> None:
        self._host: str = host.rstrip("/")
        self._on_state_update = on_state_update
        self._printer_name: str = printer_name

        self._last_flow_anomaly_key: tuple[str, str] | None = None

        # Shared state cache -- written by the WS thread, read by the adapter.
        self._cache: dict[str, Any] = {}
        self._cache_lock: threading.Lock = threading.Lock()
        self._connected: bool = False

        self._print_state_time: float | None = None

        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()
        self._ws: Any | None = None  # websocket.WebSocketApp instance
        self._rpc_id: int = 0

    # -- public API --------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the WebSocket connection is currently alive."""
        return self._connected

    def get_cached_state(self) -> dict[str, Any] | None:
        """Return the latest cached status dict, or ``None`` if empty."""
        with self._cache_lock:
            return dict(self._cache) if self._cache else None

    def _stamp_print_state_locked(self, status: dict[str, Any]) -> None:
        """Note that this push carried the print state."""
        if isinstance(status.get("print_stats"), dict):
            self._print_state_time = time.time()

    def get_print_state_age(self) -> float | None:
        """Seconds since a push last carried the print state, or ``None``."""
        with self._cache_lock:
            if self._print_state_time is None:
                return None
            return max(0.0, time.time() - self._print_state_time)

    def start(self) -> None:
        """Start the background listener thread."""
        if not _WS_AVAILABLE:
            logger.warning(
                "websocket-client not installed -- push monitoring unavailable. "
                "Install with: pip install websocket-client"
            )
            return

        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="moonraker-ws-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("Moonraker WebSocket monitor started for %s", self._host)

    def stop(self) -> None:
        """Signal the background thread to shut down and wait for it."""
        self._stop_event.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception as exc:
                logger.debug("Failed to close Moonraker WebSocket: %s", exc)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._connected = False
        logger.info("Moonraker WebSocket monitor stopped")

    # -- internal ----------------------------------------------------------

    def _next_rpc_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _ws_url(self) -> str:
        """Convert the HTTP base URL to a WebSocket URL."""
        url = self._host
        if url.startswith("https://"):
            url = "wss://" + url[len("https://") :]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://") :]
        return f"{url}/websocket"

    def _run_loop(self) -> None:
        """Reconnecting event loop -- runs in the daemon thread."""
        backoff: float = 1.0

        while not self._stop_event.is_set():
            try:
                self._connect_and_listen()
            except Exception:
                logger.debug("Moonraker WS error", exc_info=True)

            self._connected = False

            if self._stop_event.is_set():
                break

            logger.debug(
                "Moonraker WS reconnecting in %.1fs",
                backoff,
            )
            self._stop_event.wait(timeout=backoff)
            backoff = min(backoff * 2, _MAX_RECONNECT_BACKOFF)

    def _connect_and_listen(self) -> None:
        """Open the WebSocket, subscribe, and block until it closes."""
        ws_url = self._ws_url()
        logger.debug("Connecting to Moonraker WS at %s", ws_url)

        ws = _ws_mod.WebSocketApp(  # type: ignore[union-attr]
            ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws
        ws.run_forever(ping_interval=20, ping_timeout=10)

    def _on_open(self, ws: Any) -> None:
        self._connected = True
        logger.info("Moonraker WS connected")

        # Subscribe to printer objects for push updates.
        subscribe_msg = _json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "printer.objects.subscribe",
                "params": {"objects": self._SUBSCRIBE_OBJECTS},
                "id": self._next_rpc_id(),
            }
        )
        ws.send(subscribe_msg)

    def _on_message(self, ws: Any, message: str) -> None:
        try:
            data = _json.loads(message)
        except (ValueError, TypeError):
            return

        method = data.get("method")
        if method == "notify_status_update":
            params = data.get("params", [])
            if params and isinstance(params, list) and isinstance(params[0], dict):
                status = params[0]
                with self._cache_lock:
                    _merge_status_into(self._cache, status)
                    self._stamp_print_state_locked(status)
                self._maybe_record_flow_anomaly(status)
                if self._on_state_update:
                    try:
                        self._on_state_update(status)
                    except Exception:
                        logger.debug("on_state_update callback error", exc_info=True)
            return

        # The initial subscribe response also contains current state.
        result = data.get("result")
        if isinstance(result, dict) and "status" in result:
            status = result["status"]
            if isinstance(status, dict):
                with self._cache_lock:
                    _merge_status_into(self._cache, status)
                    self._stamp_print_state_locked(status)

    def _maybe_record_flow_anomaly(self, status: dict[str, Any]) -> None:
        """Feed flow / extrusion signals into the kiln-pro wear cross-check."""
        print_stats = status.get("print_stats")
        if not isinstance(print_stats, dict):
            return
        state = print_stats.get("state")
        message = print_stats.get("message")
        if not isinstance(state, str):
            return

        classification = _classify_flow_anomaly(state, message)
        if classification is None:
            if state != "error":
                self._last_flow_anomaly_key = None
            return

        # De-dupe by (state, message) — same payload, same printer, skip.
        msg_key = message if isinstance(message, str) else ""
        key = (state, msg_key)
        if self._last_flow_anomaly_key == key:
            return
        self._last_flow_anomaly_key = key

        event_type, severity = classification
        from kiln._pro_nozzle_bridge import report_flow_anomaly

        report_flow_anomaly(self._printer_name, event_type, severity)

    def _on_error(self, ws: Any, error: Any) -> None:
        logger.debug("Moonraker WS error: %s", error)

    def _on_close(self, ws: Any, close_status_code: Any, close_msg: Any) -> None:
        self._connected = False
        logger.info("Moonraker WS closed (code=%s)", close_status_code)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MoonrakerAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by the Moonraker HTTP API."""

    _DURATION_SEMANTICS: ClassVar[str] = "frozen"

    #: Klipper says where the head is, and the extruder, while it moves.
    head_position = "head_and_extruder"

    def __init__(
        self,
        host: str,
        api_key: str | None = None,
        timeout: int = 30,
        retries: int = 3,
        verify_ssl: bool = True,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")

        self._host: str = host.rstrip("/")
        self._api_key: str | None = api_key or None
        self._timeout: int = timeout
        self._retries: int = max(retries, 1)

        self._session: requests.Session = requests.Session()
        if self._api_key:
            self._session.headers.update({"X-Api-Key": self._api_key})
        self._session.verify = verify_ssl
        if not verify_ssl:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # Configure HTTP proxy from environment variables.
        _http_proxy = os.environ.get("HTTP_PROXY")
        _https_proxy = os.environ.get("HTTPS_PROXY")
        if _http_proxy or _https_proxy:
            self._session.proxies = {
                "http": _http_proxy,
                "https": _https_proxy,
            }

        self._history_backfilled: bool = False
        self._history_backfill_thread: threading.Thread | None = None

        # Unit-shaped Klipper objects, found once by read_active_slot.
        self._active_slot_objects: list[str] | None = None

        # Push monitoring (WebSocket) -- disabled by default.
        self._ws_monitor: MoonrakerWebSocketMonitor | None = None
        if _push_monitoring_enabled():
            self.enable_push_monitoring()

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return "moonraker"

    @property
    def capabilities(self) -> PrinterCapabilities:
        """Capabilities supported by the Moonraker/Klipper backend."""
        return PrinterCapabilities(
            can_clear_error=True,
            can_report_multi_material=True,
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
            can_pause=True,
            can_handle_filament=True,
            can_stream=True,
            can_probe_bed=True,
            can_update_firmware=True,
            can_snapshot=True,
            can_detect_filament=True,
            supported_extensions=(".gcode", ".gco", ".g"),
        )

    # ------------------------------------------------------------------
    # Push monitoring
    # ------------------------------------------------------------------

    def enable_push_monitoring(self) -> None:
        """Start the WebSocket push monitor for real-time status updates."""
        if self._ws_monitor is not None:
            return
        self._ws_monitor = MoonrakerWebSocketMonitor(
            self._host,
            printer_name=self.name,
        )
        self._ws_monitor.start()

    def disable_push_monitoring(self) -> None:
        """Stop the WebSocket push monitor and fall back to HTTP polling."""
        if self._ws_monitor is not None:
            self._ws_monitor.stop()
            self._ws_monitor = None

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        """Build a fully-qualified URL from a relative API path."""
        return f"{self._host}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> requests.Response:
        """Execute an HTTP request with exponential-backoff retry logic."""
        url = self._url(path)
        last_exc: Exception | None = None

        for attempt in range(self._retries):
            try:
                response = self._session.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    files=files,
                    data=data,
                    timeout=self._timeout,
                )

                if response.ok:
                    return response

                # Non-retryable HTTP error -- raise immediately.
                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    body = response.text[:300]
                    if len(response.text) > 300:
                        body += " (truncated)"
                    sc = response.status_code
                    if sc == 401:
                        said = "Moonraker didn't accept the API key. Check `KILN_PRINTER_API_KEY`, or Moonraker's sign-in settings."
                    elif sc == 403:
                        said = "Moonraker doesn't allow this request. Check Moonraker's permission settings."
                    elif sc == 404:
                        said = "Moonraker doesn't have what Kiln asked for; the file may not be there. Check with `printer_files`."
                    elif sc == 409:
                        said = "The printer is busy with something else. Wait a moment and try again."
                    elif printer_said := _printer_words(response):
                        said = (f"Moonraker ran into a problem of its own: {printer_said}" if sc >= 500
                                else f"The printer refused it: {printer_said}")
                    else:
                        said = "Moonraker didn't complete the request. Check `printer_status`, then try again."
                    raise PrinterError(said, detail=f"HTTP {sc} on {method} {path}: {body}")

                # Retryable HTTP status -- fall through to backoff.
                last_exc = PrinterError(
                    "Moonraker isn't answering right now. Check that it's running, then try again.",
                    detail=f"HTTP {response.status_code} on {method} {path} (try {attempt + 1} of {self._retries})",
                )

            except Timeout as exc:
                last_exc = PrinterError(
                    f"The printer took longer than {self._timeout:g}s to answer. It may be off or busy.",
                    cause=exc,
                    detail=f"{method} {path} (try {attempt + 1} of {self._retries})",
                )
            except ReqConnectionError as exc:
                last_exc = PrinterError(
                    f"Kiln couldn't reach Moonraker at {self._host}.",
                    cause=exc,
                    detail=f"try {attempt + 1} of {self._retries}",
                )
            except RequestException as exc:
                # Non-transient request errors -- raise immediately.
                raise PrinterError(
                    f"Kiln couldn't reach Moonraker at {self._host}: {_plain_words.said(exc)}. "
                    "Check the network, and that Moonraker is running.",
                    cause=exc,
                    detail=f"{method} {path}",
                ) from exc

            # Exponential backoff: 1 s, 2 s, 4 s, ...
            if attempt < self._retries - 1:
                backoff = 2**attempt
                logger.debug(
                    "Retrying %s %s in %ds (attempt %d/%d)",
                    method,
                    path,
                    backoff,
                    attempt + 1,
                    self._retries,
                )
                time.sleep(backoff)

        # All retries exhausted.
        assert last_exc is not None
        raise last_exc

    def _get_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Shorthand: GET *path* and return the parsed JSON body."""
        response = self._request("GET", path, **kwargs)
        try:
            return response.json()  # type: ignore[no-any-return]
        except ValueError as exc:
            raise PrinterError(
                "The printer sent back a reply Kiln couldn't read.",
                cause=exc,
                detail=f"invalid JSON from GET {path}",
            ) from exc

    def _post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> requests.Response:
        """Shorthand for POST requests."""
        return self._request("POST", path, json=json, params=params, files=files, data=data)

    def _send_gcode(self, script: str) -> requests.Response:
        """Send a G-code script to Klipper via Moonraker."""
        return self._post("/printer/gcode/script", params={"script": script})

    # ------------------------------------------------------------------
    # PrinterAdapter -- state queries
    # ------------------------------------------------------------------

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures."""
        state = self._read_state()
        if getattr(state, "connected", False):
            self._maybe_backfill_history()
        return state

    def _maybe_backfill_history(self) -> None:
        """Adopt the server's own job history — once per adapter instance."""
        if self._history_backfilled:
            return
        self._history_backfilled = True
        try:
            thread = threading.Thread(
                target=self._run_history_backfill,
                name="kiln-moonraker-history",
                daemon=True,
            )
            thread.start()
            self._history_backfill_thread = thread
        except Exception as exc:  # noqa: BLE001 — never break get_state
            logger.debug("Could not start Moonraker history backfill: %s", exc)

    def _run_history_backfill(self) -> None:
        """Thread body for the one-shot history import."""
        try:
            from kiln.printers.moonraker_history import backfill_history

            backfill_history(self)
        except Exception as exc:  # noqa: BLE001 — courtesy import, non-fatal
            logger.debug("Moonraker history backfill failed: %s", exc)

    def _read_state(self) -> PrinterState:
        """Fetch the current printer state and temperatures."""
        # -- try push-based cache first ------------------------------------
        cached = self._state_from_push_cache()
        if cached is not None:
            return cached

        # -- HTTP fallback: klippy state -----------------------------------
        try:
            info = self._get_json("/printer/info")
        except PrinterError as exc:
            if exc.cause and isinstance(exc.cause, (ReqConnectionError, Timeout)):
                return PrinterState(
                    connected=False,
                    state=PrinterStatus.OFFLINE,
                )
            raise

        klippy_state = _safe_get(info, "result", "state", default="unknown")
        if not isinstance(klippy_state, str):
            klippy_state = "unknown"

        if klippy_state != "ready":
            return PrinterState(
                connected=True,
                state=_map_moonraker_state(klippy_state),
            )

        # -- temperatures and print stats ----------------------------------
        try:
            objects = self._get_json(
                "/printer/objects/query",
                params={
                    "heater_bed": "",
                    "extruder": "",
                    "print_stats": "",
                    "temperature_sensor chamber": "",
                },
            )
        except PrinterError:
            return PrinterState(
                connected=True,
                state=_map_moonraker_state(klippy_state),
            )

        status = _safe_get(objects, "result", "status", default={})

        # Extruder
        extruder = _safe_get(status, "extruder", default={})
        tool_actual = extruder.get("temperature") if isinstance(extruder, dict) else None
        tool_target = extruder.get("target") if isinstance(extruder, dict) else None

        # Bed
        bed = _safe_get(status, "heater_bed", default={})
        bed_actual = bed.get("temperature") if isinstance(bed, dict) else None
        bed_target = bed.get("target") if isinstance(bed, dict) else None

        print_stats = _safe_get(status, "print_stats", default={})
        print_state = print_stats.get("state") if isinstance(print_stats, dict) else None

        mapped_status = _map_moonraker_state(klippy_state, print_state)

        chamber = _safe_get(status, "temperature_sensor chamber", default={})
        chamber_actual = chamber.get("temperature") if isinstance(chamber, dict) else None

        return PrinterState(
            connected=True,
            state=mapped_status,
            last_job_result=_map_moonraker_job_result(print_state),
            tool_temp_actual=tool_actual,
            tool_temp_target=tool_target,
            bed_temp_actual=bed_actual,
            bed_temp_target=bed_target,
            chamber_temp_actual=chamber_actual,
        )

    def _state_from_push_cache(self) -> PrinterState | None:
        """Build a :class:`PrinterState` from the WebSocket cache."""
        if self._ws_monitor is None or not self._ws_monitor.connected:
            return None

        cached = self._ws_monitor.get_cached_state()
        if not cached:
            return None

        # Extruder
        extruder = cached.get("extruder", {})
        tool_actual = extruder.get("temperature") if isinstance(extruder, dict) else None
        tool_target = extruder.get("target") if isinstance(extruder, dict) else None

        # Bed
        bed = cached.get("heater_bed", {})
        bed_actual = bed.get("temperature") if isinstance(bed, dict) else None
        bed_target = bed.get("target") if isinstance(bed, dict) else None

        # Print stats
        print_stats = cached.get("print_stats", {})
        print_state = print_stats.get("state") if isinstance(print_stats, dict) else None

        mapped_status = _map_moonraker_state("ready", print_state)

        # Chamber
        chamber = cached.get("temperature_sensor chamber", {})
        chamber_actual = chamber.get("temperature") if isinstance(chamber, dict) else None

        # How old the STATE is — not how old the cache is
        age = self._ws_monitor.get_print_state_age()

        return PrinterState(
            connected=True,
            state=mapped_status,
            state_age_seconds=round(age, 1) if age is not None else None,
            last_job_result=_map_moonraker_job_result(print_state),
            tool_temp_actual=tool_actual,
            tool_temp_target=tool_target,
            bed_temp_actual=bed_actual,
            bed_temp_target=bed_target,
            chamber_temp_actual=chamber_actual,
        )

    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active (or last) print job."""
        payload = self._get_json(
            "/printer/objects/query",
            params={
                "print_stats": "",
                "virtual_sdcard": "",
            },
        )

        status = _safe_get(payload, "result", "status", default={})

        # print_stats
        print_stats = _safe_get(status, "print_stats", default={})
        file_name = print_stats.get("filename") if isinstance(print_stats, dict) else None
        print_duration = print_stats.get("print_duration") if isinstance(print_stats, dict) else None

        vsd = _safe_get(status, "virtual_sdcard", default={})
        progress = vsd.get("progress") if isinstance(vsd, dict) else None

        completion: float | None = None
        if progress is not None:
            completion = round(float(progress) * 100.0, 2)

        # Estimate time left based on progress and elapsed time.
        print_time_seconds: int | None = None
        print_time_left_seconds: int | None = None

        if print_duration is not None:
            print_time_seconds = round(float(print_duration))

        if print_time_seconds is not None and completion is not None and completion > 0:
            total_estimated = print_time_seconds / (completion / 100.0)
            print_time_left_seconds = max(0, int(total_estimated - print_time_seconds))

        return JobProgress(
            file_name=file_name if file_name else None,
            completion=completion,
            print_time_seconds=print_time_seconds,
            print_time_left_seconds=print_time_left_seconds,
        )

    def list_files(self) -> list[PrinterFile]:
        """Return a list of G-code files stored on the Klipper host."""
        payload = self._get_json(
            "/server/files/list",
            params={"root": "gcodes"},
        )

        raw_files = _safe_get(payload, "result", default=[])
        if not isinstance(raw_files, list):
            raw_files = []

        results: list[PrinterFile] = []
        for entry in raw_files:
            if not isinstance(entry, dict):
                continue

            path = entry.get("path", "")
            name = path.rsplit("/", 1)[-1] if "/" in path else path

            results.append(
                PrinterFile(
                    name=name,
                    path=path,
                    size_bytes=entry.get("size"),
                    date=int(entry["modified"]) if entry.get("modified") is not None else None,
                )
            )
        return results

    # ------------------------------------------------------------------
    # PrinterAdapter -- file management
    # ------------------------------------------------------------------

    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a local G-code file to the Klipper host via Moonraker."""
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Local file not found: {abs_path}")

        filename = os.path.basename(abs_path)

        try:
            with open(abs_path, "rb") as fh:
                files_payload = {
                    "file": (filename, fh, "application/octet-stream"),
                }
                data_payload = {
                    "root": "gcodes",
                }
                response = self._post(
                    "/server/files/upload",
                    files=files_payload,
                    data=data_payload,
                )
        except PermissionError as exc:
            raise PrinterError(
                f"Permission denied reading file: {abs_path}",
                cause=exc,
            ) from exc

        # Parse the response to confirm the upload.
        try:
            body = response.json()
        except ValueError:
            body = {}

        result_item = _safe_get(body, "result", default={})
        uploaded_name = result_item.get("item", {}).get("path", filename) if isinstance(result_item, dict) else filename

        return UploadResult(
            success=True,
            file_name=uploaded_name,
            message=f"Uploaded {uploaded_name} to Moonraker.",
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- print control
    # ------------------------------------------------------------------

    def _start_print_impl(self, file_name: str, **_kwargs: Any) -> PrintResult:
        """Begin printing a file that already exists on the Klipper host.

        The name is sent inside a quoted G-code command, so a name a quote,
        backslash or control character could break out of is refused, and the
        file started is the one :meth:`_printed_entry` finds, read back."""
        if _UNSAFE_START_NAME.search(str(file_name or "")):
            return PrintResult(
                success=False,
                message=(
                    f"{fence(file_name)} was not started: its name has a quotation mark, a backslash or a "
                    "line break, which Klipper would read as part of the start command. Nothing was sent. "
                    "Rename the file on the printer, then start it again."
                ),
                refused_before_send=True,
            )
        started = file_name
        with contextlib.suppress(PrinterError):
            started = self._printed_entry(file_name)["path"]
        self._post(
            "/printer/print/start",
            params={"filename": started},
        )
        return PrintResult(
            success=True,
            message=f"Started printing {fence(file_name)}.",
        )

    def cancel_print(self) -> PrintResult:
        """Cancel the currently running print job."""
        self._post("/printer/print/cancel")
        return PrintResult(success=True, message="Print cancelled.")

    def emergency_stop(self) -> PrintResult:
        """Perform emergency stop via Moonraker's dedicated endpoint."""
        self._post("/printer/emergency_stop")
        return PrintResult(
            success=True,
            message="Emergency stop triggered.",
        )

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job."""
        self._post("/printer/print/pause")
        return PrintResult(success=True, message="Print paused.")

    def _resume_print_impl(self) -> PrintResult:
        """Resume a previously paused print job."""
        self._post("/printer/print/resume")
        return PrintResult(success=True, message="Print resumed.")

    # ------------------------------------------------------------------
    # PrinterAdapter -- temperature control
    # ------------------------------------------------------------------

    def set_tool_temp(self, target: float) -> CommandVerdict:
        """Set the hotend (extruder) target temperature in degrees Celsius."""
        self._validate_temp(target, 300.0, "Hotend")
        self._send_gcode(f"M104 S{int(target)}")
        return self._script_accepted(f"Hotend target {int(target)}°C")

    def set_bed_temp(self, target: float) -> CommandVerdict:
        """Set the heated-bed target temperature in degrees Celsius."""
        self._validate_temp(target, 130.0, "Bed")
        self._send_gcode(f"M140 S{int(target)}")
        return self._script_accepted(f"Bed target {int(target)}°C")

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def clear_error(self) -> PrintResult:
        """Clear a Klipper shutdown/error state with ``FIRMWARE_RESTART``."""
        self._post("/printer/firmware_restart")
        return PrintResult(
            success=True,
            message=(
                "Sent FIRMWARE_RESTART. Re-read `printer_status` to confirm "
                "Klipper came back ready."
            ),
        )

    #: Where Klipper states which axes are homed.
    _homed_axes_field = "toolhead.homed_axes"

    def _query_homed_axes(self) -> set[str] | None:
        """Klipper's own word: ``toolhead.homed_axes`` (``"xyz"``, or fewer)."""
        payload = self._get_json("/printer/objects/query", params={"toolhead": "homed_axes"})
        raw = payload.get("result", {}).get("status", {}).get("toolhead", {}).get("homed_axes")
        if not isinstance(raw, str):
            return None
        return {c.upper() for c in raw if c.upper() in "XYZ"}

    def _read_homed_axes(self) -> set[str] | None:
        """The confirm-after-homing read: a failed query reads as "not confirmed"."""
        try:
            return self._query_homed_axes()
        except PrinterError:
            return None

    def homed_axes_now(self) -> set[str] | None:
        """A fresh ``toolhead.homed_axes`` read, lowercase; raises on a failed read."""
        axes = self._query_homed_axes()
        return None if axes is None else {c.lower() for c in axes}

    def _z_lifts_before_home(self) -> bool | None:
        """Whether the owner configured ``[safe_z_home]`` -- read off the printer."""
        try:
            payload = self._get_json("/printer/objects/query", params={"configfile": "settings"})
        except PrinterError:
            return None
        settings = payload.get("result", {}).get("status", {}).get("configfile", {}).get("settings")
        if not isinstance(settings, dict):
            return None
        return "safe_z_home" in settings

    def send_gcode(self, commands: list[str]) -> CommandVerdict:
        """Send G-code commands to Klipper via Moonraker."""
        script = "\n".join(self._gcode_lines(commands))
        self._send_gcode(script)
        return self._script_accepted(f"{len(commands)} G-code line(s)")

    @staticmethod
    def _script_accepted(what: str) -> CommandVerdict:
        """The verdict for a script Moonraker answered ``ok`` to."""
        return CommandVerdict.accepted_only(
            f"{what}: Moonraker accepted the script and Klipper did not "
            "report an error. The resulting state is not read back on this "
            "call — read printer_status to confirm.",
            corroboration="moonraker_ok",
        )

    # ------------------------------------------------------------------
    # Fan control
    # ------------------------------------------------------------------

    def set_fan(self, node: str, percent: int) -> CommandVerdict:
        """Set the part-cooling fan speed via ``M106``/``M107`` G-code."""
        speed = self._validate_part_fan(node, percent)
        self._send_gcode(f"M106 S{speed}" if speed else "M107")
        return self._script_accepted(f"part fan {int(percent)}%")

    def skip_objects(self, object_names: list[str]) -> CommandVerdict:
        """Abandon named objects on a live Klipper multi-object print."""
        names = [str(n).strip() for n in (object_names or []) if str(n).strip()]
        if not names:
            raise PrinterError("`skip_objects` requires at least one object name.")
        return self.send_gcode([f"EXCLUDE_OBJECT NAME={n}" for n in names])

    # ------------------------------------------------------------------
    # PrinterAdapter -- calibration
    # ------------------------------------------------------------------

    _CALIBRATION_MACROS: dict[str, str] = {
        "bed_leveling": "BED_MESH_CALIBRATE",
        "vibration": "SHAPER_CALIBRATE",
    }


    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    # : Macro names a Klipper config commonly defines for filament work
    _FILAMENT_MACROS: dict[str, tuple[str, ...]] = {
        "load": ("LOAD_FILAMENT", "M701"),
        "unload": ("UNLOAD_FILAMENT", "M702"),
    }

    def _refuse_if_multi_material(self, action: str) -> None:
        """Refuse when a unit owns the filament path, or when Kiln cannot tell."""
        from kiln.multi_material import KIND_UNKNOWN, multi_material_status

        status = multi_material_status(self)
        if status.detected:
            raise FilamentHandlingUnsupported(
                f"{status.label} owns the filament path on this printer, so an "
                f"extruder-driven {action} from Kiln would fight it. Use the "
                "unit's own commands (Happy-Hare: MMU_LOAD / MMU_UNLOAD, "
                "MMU_EJECT to eject fully, MMU_SELECT_TOOL / MMU_CHANGE_TOOL) "
                "from the printer's console for now; Kiln's MMU-aware routing "
                "is a separate change."
            )
        if status.kind == KIND_UNKNOWN:
            raise FilamentHandlingUnsupported(
                "Kiln could not read whether a multi-material unit owns the "
                f"filament path on this printer, so it is refusing the {action} "
                "rather than risk driving the extruder against a unit mid-move. "
                "Check that Moonraker is reachable and try again."
            )

    def _klipper_commands(self) -> set[str]:
        """Every command / macro name Klipper reports it knows."""
        try:
            data = self._get_json("/printer/gcode/help")
        except PrinterError:
            return set()
        result = data.get("result", data)
        return {str(k).upper() for k in result} if isinstance(result, dict) else set()

    def read_nozzle_setting(self) -> NozzleSetting | None:
        """Klipper's own nozzle setting, from the configuration it is running."""
        try:
            payload = self._get_json("/printer/objects/query", params={"configfile": "settings"})
        except PrinterError:
            return None
        settings = _safe_get(payload, "result", "status", "configfile", "settings", default={}) or {}
        extruder = settings.get("extruder") if isinstance(settings, dict) else None
        diameter: float | None = None
        if isinstance(extruder, dict):
            try:
                diameter = float(extruder.get("nozzle_diameter"))
            except (TypeError, ValueError):
                diameter = None
        material: str | None = None
        for section, body in settings.items():
            if str(section).casefold() == "gcode_macro kiln_nozzle" and isinstance(body, dict):
                word = str(body.get("variable_material") or "").strip().strip("'\"")
                material = word or None
                break
        if diameter is None and material is None:
            return None
        return NozzleSetting(
            material=material,
            diameter_mm=diameter,
            source="klipper_configfile",
            firmware_version=self._software_version(),
        )

    def read_machine_identity(self) -> MachineIdentity | None:
        """The serial of the computer running Klipper, from Moonraker's host info."""
        try:
            payload = self._get_json("/machine/system_info")
        except PrinterError:
            return None
        raw = _safe_get(payload, "result", "system_info", "cpu_info", "serial_number", default="")
        value = str(raw or "").strip()
        if not value:
            return None
        return MachineIdentity(
            value=value,
            source="moonraker_system_info.cpu_info.serial_number",
            kind="host_board_serial",
        )

    def _software_version(self) -> str | None:
        """Klipper's ``software_version`` from ``/printer/info``, or ``None``."""
        try:
            info = self._get_json("/printer/info")
        except PrinterError:
            return None
        version = str(_safe_get(info, "result", "software_version", default="") or "").strip()
        return version or None

    def reported_firmware_version(self) -> str | None:
        """Klipper's own version line (``/printer/info`` software_version): a fork's build stamp, which says which macros a unit likely ships."""
        return self._software_version()

    def get_tool_position(self) -> dict[str, float] | None:
        """Where the head is now."""
        try:
            data = self._get_json("/printer/objects/query", params={"motion_report": "live_position"})
        except PrinterError:
            return None
        raw = _safe_get(data, "result", "status", "motion_report", "live_position", default=None)
        if not isinstance(raw, (list, tuple)) or len(raw) < 3:
            return None
        try:
            out = {"x": float(raw[0]), "y": float(raw[1]), "z": float(raw[2])}
            if len(raw) >= 4 and raw[3] is not None:
                out["e"] = float(raw[3])
        except (TypeError, ValueError):
            return None
        return out

    def _klipper_can_extrude(self) -> tuple[str, str] | None:
        """Klipper's own ``extruder.can_extrude`` — a real signal, read after heating."""
        try:
            data = self._get_json("/printer/objects/query", params={"extruder": ""})
        except PrinterError as exc:
            cause = f"{exc} {getattr(exc, 'detail', '') or ''}"
            if "404" in cause:
                return None
            return f"could not read the extruder object ({_plain_words.said(exc)})", "moonraker_query_failed"
        ext = _safe_get(data, "result", "status", "extruder", default={}) or {}
        if ext.get("can_extrude") is False:
            temp = ext.get("temperature")
            return (
                "Klipper reports extruder.can_extrude=false"
                + (f" at {temp:g}°C" if isinstance(temp, (int, float)) else "")
                + " — below its min_extrude_temp, so it would drop the move.",
                "klipper_can_extrude",
            )
        return None

    def _macro_filament_op(self, plan: FilamentOpPlan, macro: str) -> FilamentOpResult:
        """Heat, wait for the thermistor, run the config's own macro."""
        try:
            self.set_tool_temp(plan.temperature)
        except PrinterError as exc:
            return FilamentOpResult(
                success=False, action=plan.action,
                message=f"Kiln couldn't set the hotend to {plan.temperature:g}°C: {_plain_words.said(exc)}.",
                extrusion_verified=False, verification_source="heater_command_rejected",
                error_hint=str(exc), slot=plan.slot, material=plan.material,
                temperature=plan.temperature,
            )
        reached, reading = self._wait_for_hotend(plan.temperature)
        if not reached:
            return FilamentOpResult(
                success=False, action=plan.action,
                message=(
                    f"The hotend did not reach {plan.temperature:g}°C before the "
                    f"{macro} macro would have run (last reading {reading!r}). "
                    "Nothing was moved."
                ),
                extrusion_verified=False, verification_source="thermistor",
                slot=plan.slot, material=plan.material, temperature=plan.temperature,
                details={"last_hotend_reading": reading},
            )
        try:
            self._send_gcode(macro)
        except PrinterError as exc:
            return FilamentOpResult(
                success=False, action=plan.action,
                message=f"Klipper rejected {macro}: {_plain_words.said(exc)}.",
                extrusion_verified=False, verification_source="firmware_rejected_move",
                error_hint=str(exc), slot=plan.slot, material=plan.material,
                temperature=plan.temperature, details={"macro": macro},
            )
        return FilamentOpResult(
            success=True, action=plan.action,
            message=(
                f"Hotend at {reading:g}°C; Klipper accepted the {macro} macro from "
                "this printer's own config. Klipper reports no flow sensor, so "
                "whether filament actually moved is not something Kiln can "
                "confirm — check the extruder."
            ),
            extrusion_verified=None, verification_source="command_accepted_only",
            slot=plan.slot, material=plan.material, temperature=plan.temperature,
            details={"macro": macro, "hotend_reading": reading, "mechanism": "klipper_macro"},
        )

    def _load_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        self._refuse_if_multi_material("load")
        known = self._klipper_commands()
        for macro in self._FILAMENT_MACROS["load"]:
            if macro in known:
                return self._macro_filament_op(plan, macro)
        return self._gcode_filament_move(
            plan,
            signed_length_mm=float(plan.length_mm or DEFAULT_LOAD_LENGTH_MM),
            mechanism="moonraker_gcode_script",
            pre_move_check=self._klipper_can_extrude,
        )

    def _unload_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        self._refuse_if_multi_material("unload")
        known = self._klipper_commands()
        for macro in self._FILAMENT_MACROS["unload"]:
            if macro in known:
                return self._macro_filament_op(plan, macro)
        return self._gcode_filament_move(
            plan,
            signed_length_mm=-float(plan.length_mm or DEFAULT_UNLOAD_LENGTH_MM),
            mechanism="moonraker_gcode_script",
            pre_move_check=self._klipper_can_extrude,
        )

    def _purge_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        return self._gcode_filament_move(
            plan,
            signed_length_mm=float(plan.length_mm or DEFAULT_PURGE_LENGTH_MM),
            mechanism="moonraker_gcode_script",
            pre_move_check=self._klipper_can_extrude,
        )

    def run_calibration(self, *, options: list[str] | None = None) -> PrintResult:
        """Run calibration routines via Klipper G-code macros."""
        if options is None:
            options = ["bed_leveling"]

        # Resolve "all" shortcut.
        if "all" in options:
            macros = list(self._CALIBRATION_MACROS.values())
            description = "full calibration (bed leveling + vibration)"
        else:
            macros = []
            parts: list[str] = []
            for opt in options:
                macro = self._CALIBRATION_MACROS.get(opt)
                if macro is None:
                    valid = ", ".join(sorted(self._CALIBRATION_MACROS))
                    return PrintResult(
                        success=False,
                        message=(
                            f"Unknown calibration option {opt!r}. "
                            f"Valid options: {valid}, all"
                        ),
                    )
                macros.append(macro)
                parts.append(opt)
            description = " + ".join(parts) + " calibration"

        script = "\n".join(macros)
        self._send_gcode(script)
        return PrintResult(
            success=True,
            message=f"Started {description}. The printer will calibrate and return to idle.",
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- file deletion
    # ------------------------------------------------------------------

    def delete_file(self, file_path: str) -> bool:
        """Delete a G-code file from the Klipper host via Moonraker."""
        encoded = quote(file_path, safe="")
        self._request("DELETE", f"/server/files/gcodes/{encoded}")
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- reading a print file back
    # ------------------------------------------------------------------

    def _printed_entry(self, file_name: str) -> dict[str, Any]:
        """The ``gcodes`` listing entry a start of *file_name* prints, found
        as Klipper finds it: two leading slashes dropped, then the exact
        path, else the one match apart from capital letters.  Raises
        :class:`PrinterError` when the listing fails or no single file
        matches."""
        wanted = str(file_name or "")
        for _ in range(2):
            if wanted.startswith("/"):
                wanted = wanted[1:]
        try:
            payload = self._get_json("/server/files/list", params={"root": "gcodes"})
        except PrinterError as exc:
            raise _read_back.read_failed(file_name, exc) from exc
        raw = _safe_get(payload, "result", default=[])
        printable = [
            entry
            for entry in (raw if isinstance(raw, list) else [])
            if isinstance(entry, dict) and isinstance(entry.get("path"), str) and _klipper_prints(entry["path"])
        ]
        for entry in printable:
            if entry["path"] == wanted:
                return entry
        folded = [entry for entry in printable if entry["path"].lower() == wanted.lower()]
        if len(folded) == 1:
            return folded[0]
        if folded:
            raise _read_back.read_failed(
                file_name,
                "more than one file on the printer has that name in different capital letters, "
                "so Kiln can't tell which one would print",
            )
        raise _read_back.read_failed(file_name, "the printer has no print file by that name")

    def read_print_file(self, file_name: str) -> bytes | None:
        """The bytes a start of *file_name* prints, downloaded from
        Moonraker's ``gcodes`` root with this session's API key.  Moonraker
        serves every file in that root, so this never answers ``None``."""
        path = self._printed_entry(file_name)["path"]
        cap = _read_back.READ_BACK_CAP_BYTES
        url = self._url(f"/server/files/gcodes/{quote(path, safe='/')}")
        try:
            response = self._session.get(url, stream=True, timeout=self._timeout)
        except RequestException as exc:
            raise _read_back.read_failed(file_name, exc) from exc
        try:
            if not response.ok:
                raise _read_back.read_failed(file_name, _download_refused(response.status_code))
            declared = str(response.headers.get("Content-Length") or "")
            if declared.isdigit() and int(declared) > cap:
                raise _read_back.too_big(file_name, cap)
            return _read_back.collect_capped(
                response.iter_content(chunk_size=_READ_BACK_CHUNK_BYTES), file_name, cap,
            )
        except RequestException as exc:
            raise _read_back.read_failed(file_name, exc) from exc
        finally:
            response.close()

    def print_file_identity(self, file_name: str) -> tuple[int | None, int | None] | None:
        """``(size, time stamp)`` of the file a start of *file_name* prints,
        from Moonraker's ``gcodes`` listing, which reads them off the disk at
        each call (the per-file metadata is the record of the last scan, and
        has none for a file not scanned yet).  ``None`` when the listing does
        not come through or no single file matches."""
        try:
            entry = self._printed_entry(file_name)
        except Exception:  # noqa: BLE001 -- an identity the printer did not give is none
            return None
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            size = None
        stamp = _read_back.unix_time(entry.get("modified"))
        if size is None and stamp is None:
            return None
        return size, stamp

    # ------------------------------------------------------------------
    # PrinterAdapter -- webcam snapshot
    # ------------------------------------------------------------------

    def get_snapshot(self) -> bytes | None:
        """Capture a webcam snapshot from Moonraker."""
        try:
            payload = self._get_json("/server/webcams/list")
            webcams = _safe_get(payload, "result", "webcams", default=[])
            if not isinstance(webcams, list) or not webcams:
                raise PrinterError(
                    "No webcams configured in Moonraker. Add a webcam via Moonraker's webcam configuration."
                )

            # Use the first webcam's snapshot_url
            cam = webcams[0]
            snapshot_url = cam.get("snapshot_url") or cam.get("urlSnapshot")
            if not snapshot_url:
                stream_url = cam.get("stream_url") or cam.get("urlStream")
                if stream_url:
                    snapshot_url = stream_url.replace("/stream", "/?action=snapshot")
                else:
                    raise PrinterError(
                        "Webcam found in Moonraker but no snapshot URL configured. Check your webcam configuration."
                    )

            if snapshot_url.startswith("/"):
                snapshot_url = f"{self._host}{snapshot_url}"

            response = self._session.get(snapshot_url, timeout=10)
            if response.ok and response.content:
                return response.content
            if not response.ok:
                raise PrinterError(
                    f"Webcam snapshot failed (HTTP {response.status_code}). Check that the webcam service is running."
                )
            return None
        except PrinterError:
            raise
        except Timeout as exc:
            raise PrinterError(
                "Webcam snapshot timed out after 10s. Check that the webcam service is running and accessible."
            ) from exc
        except ReqConnectionError as exc:
            raise PrinterError(
                "Kiln couldn't get a picture from the webcam: it couldn't connect. Check that the "
                "webcam is set up and the printer is online."
            ) from exc
        except RequestException as exc:
            raise PrinterError(
                f"Kiln couldn't get a picture from the webcam: {_plain_words.said(exc)}. "
                "Check the webcam's setup in Moonraker.",
            ) from exc
        except Exception as exc:
            raise PrinterError(
                f"Kiln couldn't get a picture from the webcam: {_plain_words.said(exc)}.",
            ) from exc

    # ------------------------------------------------------------------
    # PrinterAdapter -- webcam streaming
    # ------------------------------------------------------------------

    def get_stream_url(self) -> str | None:
        """Discover and return the MJPEG stream URL from Moonraker."""
        try:
            payload = self._get_json("/server/webcams/list")
            webcams = _safe_get(payload, "result", "webcams", default=[])
            if not isinstance(webcams, list) or not webcams:
                return None

            cam = webcams[0]
            stream_url = cam.get("stream_url") or cam.get("urlStream")
            if not stream_url:
                return None

            if stream_url.startswith("/"):
                stream_url = f"{self._host}{stream_url}"

            return stream_url
        except Exception:
            logger.debug("Webcam stream URL discovery failed", exc_info=True)
            return None

    camera_check_ids: ClassVar[tuple[str, ...]] = ("moonraker_webcam_list",)

    def camera_probes(self) -> list[Any]:
        """The stream address Moonraker's own webcam list names, to check."""
        from kiln.camera_check import CameraProbe

        ask = type(self).get_stream_url
        while getattr(ask, "_kiln_camera_wrapped", False) and hasattr(ask, "__wrapped__"):
            ask = ask.__wrapped__
        url = ask(self)
        if not url:
            return []
        return [
            CameraProbe(
                "moonraker_webcam_list",
                url,
                "The stream address the printer's own Moonraker webcam list reports "
                "(GET /server/webcams/list, stream_url), as configured on the printer. "
                "Source: the printer's own API.",
            )
        ]

    # ------------------------------------------------------------------
    # PrinterAdapter -- filament sensor
    # ------------------------------------------------------------------

    def get_filament_status(self) -> dict[str, Any] | None:
        """Query Klipper for filament runout sensor status via Moonraker."""
        try:
            listing = self._get_json("/printer/objects/list")
            objects = _safe_get(listing, "result", "objects", default=[])
            if not isinstance(objects, list):
                return None

            names = [
                obj
                for obj in objects
                if isinstance(obj, str)
                and any(obj == t or obj.startswith(f"{t} ") for t in _FILAMENT_SENSOR_TYPES)
            ]
            if not names:
                return None

            payload = self._get_json(
                "/printer/objects/query",
                params=dict.fromkeys(names, ""),
            )
            status = _safe_get(payload, "result", "status", default={})
            if not isinstance(status, dict):
                return None

            # Klipper reports per sensor: enabled (bool), filament_detected (bool)
            sensors: list[dict[str, Any]] = []
            for name in names:
                data = status.get(name)
                if isinstance(data, dict) and data:
                    sensors.append(
                        {
                            "name": name,
                            "detected": bool(data.get("filament_detected", False)),
                            "enabled": bool(data.get("enabled", False)),
                        }
                    )
            if not sensors:
                return None

            considered = [s for s in sensors if s["enabled"]] or sensors

            detected = all(s["detected"] for s in considered)
            primary = next((s for s in considered if not s["detected"]), considered[0])

            return {
                "detected": detected,
                "sensor_enabled": any(s["enabled"] for s in sensors),
                "sensor_name": primary["name"],
                "source": f"klipper_{primary['name'].split()[0]}",
                "sensors": sensors,
            }
        except Exception:
            logger.debug("Filament sensor query failed", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # PrinterAdapter -- multi-material (read-only)
    # ------------------------------------------------------------------

    _MULTI_MATERIAL_OBJECTS: ClassVar[tuple[str, ...]] = ("mmu", "AFC")

    def get_multi_material_status(self) -> Any:
        """Read the MMU this Klipper carries, if any — a read, never a route."""
        from kiln.multi_material import from_afc, from_happy_hare, none_status

        try:
            listing = self._get_json("/printer/objects/list")
        except PrinterError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise PrinterError(f"objects/list failed: {_plain_words.said(exc)}.") from exc
        objects = _safe_get(listing, "result", "objects", default=None)
        if not isinstance(objects, list):
            # Moonraker answered in a shape this probe does not understand
            raise PrinterError(
                "Klipper's object list came back in an unexpected shape, so "
                "Kiln cannot tell what this printer has"
            )
        names = {o for o in objects if isinstance(o, str)}

        if "mmu" in names:
            payload = self._get_json(
                "/printer/objects/query",
                params={"mmu": "", "mmu_machine": ""},
            )
            status = _safe_get(payload, "result", "status", default={}) or {}
            mmu = status.get("mmu") if isinstance(status, dict) else None
            if not isinstance(mmu, dict):
                raise PrinterError("Klipper lists an mmu object but returned no status for it")
            machine = status.get("mmu_machine") if isinstance(status, dict) else None
            return from_happy_hare(mmu, machine if isinstance(machine, dict) else None)

        if "AFC" in names:
            payload = self._get_json("/printer/objects/query", params={"AFC": ""})
            status = _safe_get(payload, "result", "status", default={}) or {}
            afc = status.get("AFC") if isinstance(status, dict) else None
            if not isinstance(afc, dict):
                raise PrinterError("Klipper lists an AFC object but returned no status for it")
            return from_afc(afc)

        return none_status("moonraker:no_mmu_object")

    def _active_slot_object_names_cached(self) -> list[str]:
        """Unit objects worth querying for a feeding slot, found once per adapter."""
        cached = getattr(self, "_active_slot_objects", None)
        if cached is not None:
            return cached
        listing = self._get_json("/printer/objects/list")
        objects = _safe_get(listing, "result", "objects", default=None)
        if not isinstance(objects, list):
            raise PrinterError("Klipper's object list came back in an unexpected shape")
        names = _active_slot_unit_objects([o for o in objects if isinstance(o, str)])
        self._active_slot_objects = names
        return names

    def read_active_slot(self) -> ActiveSlotReading | None:
        """Which slot this Klipper machine's multi-material unit reports, or ``None``."""
        try:
            names = self._active_slot_object_names_cached()
            if not names:
                return None
            payload = self._get_json("/printer/objects/query", params=dict.fromkeys(names, ""))
            status = _safe_get(payload, "result", "status", default=None)
            if not isinstance(status, dict):
                return None
            return _active_slot_from_klipper_objects(status, names)
        except Exception:  # noqa: BLE001 - PrinterError included; a reading never raises
            return None

    # ------------------------------------------------------------------
    # Klipper configuration
    # ------------------------------------------------------------------

    def get_printer_config(self) -> dict[str, Any] | None:
        """Return the printer's parsed Klipper configuration sections."""
        payload = self._get_json(
            "/printer/objects/query",
            params={"configfile": ""},
        )
        config = _safe_get(payload, "result", "status", "configfile", "config", default=None)
        if not isinstance(config, dict):
            return None
        return config

    # ------------------------------------------------------------------
    # PrinterAdapter -- bed mesh
    # ------------------------------------------------------------------

    def get_bed_mesh(self) -> dict[str, Any] | None:
        """Query Moonraker for the current bed mesh data."""
        try:
            payload = self._get_json(
                "/printer/objects/query",
                params={"bed_mesh": ""},
            )
            mesh = _safe_get(payload, "result", "status", "bed_mesh", default=None)
            if not mesh or not isinstance(mesh, dict):
                return None
            return mesh
        except Exception:
            logger.debug("Bed mesh query failed", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    def get_firmware_status(self) -> FirmwareStatus | None:
        """Check Moonraker update manager for available updates."""
        try:
            data = self._get_json("/machine/update/status")
        except Exception:
            logger.debug("Firmware status query failed", exc_info=True)
            return None

        result = data.get("result", data)
        version_info = result.get("version_info", {})
        busy = bool(result.get("busy", False))

        components: list[FirmwareComponent] = []
        updates_available = 0

        for comp_name, info in version_info.items():
            if not isinstance(info, dict):
                continue

            current = info.get("version", info.get("full_version_string", ""))
            remote = info.get("remote_version", "")
            rollback = info.get("rollback_version")
            comp_type = info.get("configured_type", "")
            channel = info.get("channel", "")

            has_update = False
            if comp_name == "system":
                has_update = int(info.get("package_count", 0)) > 0
            elif current and remote and current != remote or int(info.get("commits_behind_count", 0)) > 0:
                has_update = True

            if has_update:
                updates_available += 1

            components.append(
                FirmwareComponent(
                    name=comp_name,
                    current_version=str(current),
                    remote_version=str(remote) if remote else None,
                    update_available=has_update,
                    rollback_version=str(rollback) if rollback else None,
                    component_type=comp_type,
                    channel=channel,
                )
            )

        return FirmwareStatus(
            busy=busy,
            components=components,
            updates_available=updates_available,
        )

    def update_firmware(
        self,
        component: str | None = None,
    ) -> FirmwareUpdateResult:
        """Trigger an update via Moonraker's update manager."""
        try:
            state = self.get_state()
            if state.effective_state == PrinterStatus.PRINTING:
                raise PrinterError("Kiln can't update the firmware while printing. Wait for the current print to finish.")
        except PrinterError:
            raise
        except Exception as exc:
            logger.debug(
                "Failed to check printer state before firmware update: %s", exc
            )

        payload = {}
        if component:
            payload["name"] = component

        try:
            self._post("/machine/update/upgrade", json=payload)
        except PrinterError:
            raise
        except Exception as exc:
            raise PrinterError(
                f"The firmware update didn't finish: {_plain_words.said(exc)}. "
                "Check Moonraker logs for details. Retry with `update_machine_firmware`.",
                cause=exc,
            ) from exc

        target = component or "all components"
        return FirmwareUpdateResult(
            success=True,
            message=f"Update started for {target}. The printer services may restart.",
            component=component,
        )

    def rollback_firmware(self, component: str) -> FirmwareUpdateResult:
        """Roll back a component to its previous version."""
        if not component:
            raise PrinterError("Component name is required for rollback.")

        try:
            self._post(
                "/machine/update/rollback",
                json={"name": component},
            )
        except PrinterError:
            raise
        except Exception as exc:
            raise PrinterError(
                f"The firmware rollback didn't finish: {_plain_words.said(exc)}. "
                "Check Moonraker logs for details. Retry with `roll_back_machine_firmware`.",
                cause=exc,
            ) from exc

        return FirmwareUpdateResult(
            success=True,
            message=f"Rollback started for {component}.",
            component=component,
        )

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<MoonrakerAdapter host={self._host!r}>"
