"""OctoPrint adapter for the Kiln printer abstraction layer."""

from __future__ import annotations

import json as _json
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any, ClassVar
from urllib.parse import quote

import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import RequestException, Timeout

from kiln.printer_backends import REQUEST_OFFERS, backend_for
from kiln.printers.base import (
    DEFAULT_LOAD_LENGTH_MM,
    DEFAULT_PURGE_LENGTH_MM,
    DEFAULT_UNLOAD_LENGTH_MM,
    FilamentOpPlan,
    FilamentOpResult,
    FirmwareComponent,
    FirmwareStatus,
    FirmwareUpdateResult,
    JobProgress,
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
from kiln.printers.safe_motion import build_firmware_resume_positioning

try:
    import websocket as _ws_mod  # websocket-client

    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ws_mod = None  # type: ignore[assignment]
    _WS_AVAILABLE = False

logger = logging.getLogger(__name__)

_KEY_NEED = next(need for need in backend_for("octoprint").needs if need.key == "api_key")
_KEY_WHERE = _KEY_NEED.where
_KEY_OFFER = REQUEST_OFFERS.get(_KEY_NEED.request, "")

# HTTP status codes eligible for automatic retry.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({502, 503, 504})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_get(data: Any, *keys: str, default: Any = None) -> Any:
    """Walk nested dicts safely, returning *default* on any miss or type error."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
    return current


def _map_flags_to_status(flags: dict[str, Any]) -> PrinterStatus:
    """Translate OctoPrint state-flag booleans to a :class:`PrinterStatus`."""
    if flags.get("cancelling"):
        return PrinterStatus.CANCELLING
    if flags.get("printing"):
        return PrinterStatus.PRINTING
    if flags.get("paused") or flags.get("pausing"):
        return PrinterStatus.PAUSED
    if flags.get("error") or flags.get("closedOrError"):
        return PrinterStatus.ERROR
    if flags.get("ready") and flags.get("operational"):
        return PrinterStatus.IDLE
    if flags.get("operational"):
        return PrinterStatus.BUSY
    return PrinterStatus.UNKNOWN


def _flatten_files(entries: list[dict[str, Any]], prefix: str = "") -> list[dict[str, Any]]:
    """Recursively flatten OctoPrint's nested file/folder listing."""
    flat: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("type") == "folder":
            children = entry.get("children", [])
            folder_path = f"{prefix}{entry.get('name', '')}/"
            flat.extend(_flatten_files(children, prefix=folder_path))
        else:
            flat.append(entry)
    return flat


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

_FLOW_ANOMALY_EVENT_MAP: dict[str, tuple[str, str]] = {
    "FilamentChange": ("filament_jam", "high"),
}

_FLOW_ANOMALY_FLAG_MAP: dict[str, tuple[str, str]] = {
    "filament_change": ("filament_jam", "high"),
}

_FLOW_ANOMALY_ERROR_KEYWORDS: tuple[str, ...] = (
    "filament",
    "extruder",
    "extrusion",
    "feed",
    "jam",
    "runout",
)

# Plugin event-name substrings (lowercased) that map to flow anomalies
_FLOW_ANOMALY_PLUGIN_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # (substring, event_type, severity)
    ("filament_not_present", "filament_jam", "high"),
    ("filament_runout", "filament_jam", "high"),
    ("filamentsensor", "filament_jam", "high"),
    ("under_extrusion", "under_extrusion", "medium"),
    ("underextrusion", "under_extrusion", "medium"),
)


def _classify_flow_anomaly(
    event_name: str,
    payload: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    """Return ``(event_type, severity)`` for an OctoPrint anomaly event."""
    if not event_name:
        return None

    if event_name in _FLOW_ANOMALY_EVENT_MAP:
        return _FLOW_ANOMALY_EVENT_MAP[event_name]

    # Synthetic ``flag:<name>`` from the REST polling path.
    if event_name.startswith("flag:"):
        flag_name = event_name[len("flag:") :]
        return _FLOW_ANOMALY_FLAG_MAP.get(flag_name)

    if event_name == "Error":
        if not isinstance(payload, dict):
            return None
        error_text = str(payload.get("error", "")).lower()
        if any(kw in error_text for kw in _FLOW_ANOMALY_ERROR_KEYWORDS):
            return ("under_extrusion", "medium")
        return None

    # Plugin-emitted events: ``plugin_<id>_<event>`` is the convention.
    if event_name.startswith("plugin_"):
        lowered = event_name.lower()
        for substr, event_type, severity in _FLOW_ANOMALY_PLUGIN_PATTERNS:
            if substr in lowered:
                return (event_type, severity)
        return None

    return None


# ---------------------------------------------------------------------------
# SockJS push monitor
# ---------------------------------------------------------------------------

def _push_monitoring_enabled() -> bool:
    """Check whether push monitoring is enabled (read at call time, not import time)."""
    return os.environ.get("KILN_PUSH_MONITORING", "0") == "1"

# Maximum reconnect backoff in seconds.
_MAX_RECONNECT_BACKOFF: float = 30.0


class OctoPrintSockJSMonitor:
    """Push-based status monitor for OctoPrint via its SockJS WebSocket."""

    def __init__(
        self,
        host: str,
        api_key: str,
        *,
        on_state_update: Callable[[dict[str, Any]], None] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._host: str = host.rstrip("/")
        self._api_key: str = api_key
        self._on_state_update = on_state_update
        self._on_event = on_event

        # Shared state cache -- written by the WS thread, read by the adapter.
        self._cache: dict[str, Any] = {}
        self._cache_lock: threading.Lock = threading.Lock()

        self._print_state_time: float | None = None
        self._connected: bool = False

        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()
        self._ws: Any | None = None  # websocket.WebSocketApp instance

    # -- public API --------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the WebSocket connection is currently alive."""
        return self._connected

    def get_cached_state(self) -> dict[str, Any] | None:
        """Return the latest cached ``current`` payload, or ``None``."""
        with self._cache_lock:
            return dict(self._cache) if self._cache else None

    def get_print_state_age(self) -> float | None:
        """Seconds since a push last carried the printer state, or ``None``."""
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
            name="octoprint-sockjs-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("OctoPrint SockJS monitor started for %s", self._host)

    def stop(self) -> None:
        """Signal the background thread to shut down and wait for it."""
        self._stop_event.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception as exc:
                logger.debug("Failed to close OctoPrint WebSocket: %s", exc)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._connected = False
        logger.info("OctoPrint SockJS monitor stopped")

    # -- internal ----------------------------------------------------------

    def _ws_url(self) -> str:
        """Convert the HTTP base URL to a SockJS WebSocket URL."""
        url = self._host
        if url.startswith("https://"):
            url = "wss://" + url[len("https://") :]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://") :]
        return f"{url}/sockjs/websocket"

    def _run_loop(self) -> None:
        """Reconnecting event loop -- runs in the daemon thread."""
        backoff: float = 1.0

        while not self._stop_event.is_set():
            try:
                self._connect_and_listen()
            except Exception:
                logger.debug("OctoPrint SockJS error", exc_info=True)

            self._connected = False

            if self._stop_event.is_set():
                break

            logger.debug(
                "OctoPrint SockJS reconnecting in %.1fs",
                backoff,
            )
            self._stop_event.wait(timeout=backoff)
            backoff = min(backoff * 2, _MAX_RECONNECT_BACKOFF)

    def _connect_and_listen(self) -> None:
        """Open the SockJS WebSocket, authenticate, and block until close."""
        ws_url = self._ws_url()
        logger.debug("Connecting to OctoPrint SockJS at %s", ws_url)

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
        logger.info("OctoPrint SockJS connected")

        # OctoPrint SockJS requires an auth message after connect.
        auth_msg = _json.dumps({"auth": f"{self._api_key}"})
        ws.send(auth_msg)

    def _on_message(self, ws: Any, message: str) -> None:
        try:
            data = _json.loads(message)
        except (ValueError, TypeError):
            return

        if not isinstance(data, dict):
            return

        if "current" in data:
            current = data["current"]
            if isinstance(current, dict):
                with self._cache_lock:
                    self._cache.update(current)
                    if isinstance(current.get("state"), dict):
                        self._print_state_time = time.time()
                if self._on_state_update:
                    try:
                        self._on_state_update(current)
                    except Exception:
                        logger.debug("on_state_update callback error", exc_info=True)

        if "event" in data and self._on_event is not None:
            event_obj = data["event"]
            if isinstance(event_obj, dict):
                event_name = event_obj.get("type") or event_obj.get("event") or ""
                payload = event_obj.get("payload") or {}
                if not isinstance(payload, dict):
                    payload = {}
                if isinstance(event_name, str) and event_name:
                    try:
                        self._on_event(event_name, payload)
                    except Exception:
                        logger.debug("on_event callback error", exc_info=True)

    def _on_error(self, ws: Any, error: Any) -> None:
        logger.debug("OctoPrint SockJS error: %s", error)

    def _on_close(self, ws: Any, close_status_code: Any, close_msg: Any) -> None:
        self._connected = False
        logger.info("OctoPrint SockJS closed (code=%s)", close_status_code)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OctoPrintAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by the OctoPrint REST API."""

    _DURATION_SEMANTICS: ClassVar[str] = "frozen"

    #: How Kiln learns this printer's own pause and cancel moves.
    head_position = ("OctoPrint's API reports no head position, so its owner teaches Kiln the "
                     "printer's moves through the guided session's questions")

    def __init__(
        self,
        host: str,
        api_key: str,
        timeout: int = 30,
        retries: int = 3,
        verify_ssl: bool = True,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")
        if not api_key:
            raise ValueError("api_key must not be empty")

        self._host: str = host.rstrip("/")
        self._api_key: str = api_key
        self._timeout: int = timeout
        self._retries: int = max(retries, 1)

        self._session: requests.Session = requests.Session()
        self._session.headers.update({"X-Api-Key": self._api_key})
        self._session.verify = verify_ssl
        if not verify_ssl:
            # Suppress noisy InsecureRequestWarning for self-signed certs
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

        # Push monitoring (SockJS) -- disabled by default.
        self._sockjs_monitor: OctoPrintSockJSMonitor | None = None

        self._prev_flow_flags: dict[str, bool] = {}

        if _push_monitoring_enabled():
            self.enable_push_monitoring()

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return "octoprint"

    @property
    def capabilities(self) -> PrinterCapabilities:
        """Capabilities supported by the OctoPrint backend."""
        return PrinterCapabilities(
            can_clear_error=True,
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
        """Start the SockJS push monitor for real-time status updates."""
        if self._sockjs_monitor is not None:
            return
        self._sockjs_monitor = OctoPrintSockJSMonitor(
            self._host,
            self._api_key,
            on_state_update=self._handle_push_state,
            on_event=self._handle_push_event,
        )
        self._sockjs_monitor.start()

    def disable_push_monitoring(self) -> None:
        """Stop the SockJS push monitor and fall back to HTTP polling."""
        if self._sockjs_monitor is not None:
            self._sockjs_monitor.stop()
            self._sockjs_monitor = None

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
                    if sc in (401, 403):
                        hint = (
                            " OctoPrint refused the API key: it is missing, wrong, or its user lacks "
                            f"permission for this. Make a new one {_KEY_WHERE}"
                            + (f"; {_KEY_OFFER}." if _KEY_OFFER else ".")
                        )
                    elif sc == 404:
                        hint = " Resource not found — the file or endpoint may not exist. Verify with `list_files()`."
                    elif sc == 409:
                        hint = " Conflict — printer may be busy with another operation. Check `get_state()` first."
                    else:
                        hint = " Retry with `get_state()` to check printer status."
                    raise PrinterError(
                        f"OctoPrint returned HTTP {sc} for {method} {path}: {body}.{hint}",
                    )

                # Retryable HTTP status -- fall through to backoff.
                last_exc = PrinterError(
                    f"OctoPrint returned HTTP {response.status_code} "
                    f"for {method} {path} "
                    f"(attempt {attempt + 1}/{self._retries}). "
                    f"OctoPrint backend may be unavailable — check that the service is running. "
                    f"Retry with `get_state()`."
                )

            except Timeout as exc:
                last_exc = PrinterError(
                    f"{method} {path} timed out after {self._timeout}s. "
                    f"Printer may be offline or overloaded. "
                    f"(attempt {attempt + 1}/{self._retries})",
                    cause=exc,
                )
            except ReqConnectionError as exc:
                last_exc = PrinterError(
                    f"Could not connect to OctoPrint at {self._host} (attempt {attempt + 1}/{self._retries})",
                    cause=exc,
                )
            except RequestException as exc:
                # Non-transient request errors -- raise immediately.
                raise PrinterError(
                    f"Request error for {method} {path}: {exc}. "
                    f"Cannot reach OctoPrint at {self._host}. Check network and that OctoPrint is running. "
                    f"Retry with `get_state()`.",
                    cause=exc,
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
                f"Invalid JSON in response from GET {path}",
                cause=exc,
            ) from exc

    def _post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> requests.Response:
        """Shorthand for POST requests."""
        return self._request("POST", path, json=json, files=files, data=data)

    # ------------------------------------------------------------------
    # PrinterAdapter -- state queries
    # ------------------------------------------------------------------

    def read_nozzle_setting(self) -> NozzleSetting | None:
        """The nozzle diameter in OctoPrint's current printer profile."""
        try:
            payload = self._get_json("/api/printerprofiles")
        except PrinterError:
            return None
        profiles = payload.get("profiles") if isinstance(payload, dict) else None
        if not isinstance(profiles, dict):
            return None
        current = next(
            (p for p in profiles.values() if isinstance(p, dict) and p.get("current")),
            None,
        )
        extruder = current.get("extruder") if isinstance(current, dict) else None
        raw = extruder.get("nozzleDiameter") if isinstance(extruder, dict) else None
        try:
            diameter = float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            diameter = None
        if diameter is None:
            return None
        return NozzleSetting(
            material=None, diameter_mm=diameter, source="octoprint_printer_profile", held_by="host",
        )

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures."""
        # -- try push-based cache first ------------------------------------
        cached = self._state_from_push_cache()
        if cached is not None:
            return cached

        # -- HTTP fallback -------------------------------------------------
        try:
            payload = self._get_json("/api/printer")
        except PrinterError as exc:
            if exc.cause and isinstance(exc.cause, (ReqConnectionError, Timeout)):
                return PrinterState(
                    connected=False,
                    state=PrinterStatus.OFFLINE,
                )
            raise

        # --- temperatures -------------------------------------------------
        temps = _safe_get(payload, "temperature", default={})
        tool = temps.get("tool0", {}) if isinstance(temps, dict) else {}
        bed = temps.get("bed", {}) if isinstance(temps, dict) else {}

        # --- state flags --------------------------------------------------
        flags = _safe_get(payload, "state", "flags", default={})
        if not isinstance(flags, dict):
            flags = {}

        status = _map_flags_to_status(flags)

        # Flow-anomaly cross-check (REST polling path)
        self._check_flow_flag_transitions(flags)

        chamber = temps.get("chamber", {}) if isinstance(temps, dict) else {}

        return PrinterState(
            connected=True,
            state=status,
            tool_temp_actual=tool.get("actual"),
            tool_temp_target=tool.get("target"),
            bed_temp_actual=bed.get("actual"),
            bed_temp_target=bed.get("target"),
            chamber_temp_actual=chamber.get("actual") if isinstance(chamber, dict) else None,
            chamber_temp_target=chamber.get("target") if isinstance(chamber, dict) else None,
        )

    def _state_from_push_cache(self) -> PrinterState | None:
        """Build a :class:`PrinterState` from the SockJS push cache."""
        if self._sockjs_monitor is None or not self._sockjs_monitor.connected:
            return None

        cached = self._sockjs_monitor.get_cached_state()
        if not cached:
            return None

        temps_list = cached.get("temps", [])
        temps: dict[str, Any] = {}
        if isinstance(temps_list, list) and temps_list:
            temps = temps_list[-1] if isinstance(temps_list[-1], dict) else {}

        tool = temps.get("tool0", {}) if isinstance(temps, dict) else {}
        bed = temps.get("bed", {}) if isinstance(temps, dict) else {}
        chamber = temps.get("chamber", {}) if isinstance(temps, dict) else {}

        state_obj = cached.get("state", {})
        flags = state_obj.get("flags", {}) if isinstance(state_obj, dict) else {}
        if not isinstance(flags, dict):
            flags = {}

        status = _map_flags_to_status(flags)

        # Flow-anomaly cross-check (push path)
        self._check_flow_flag_transitions(flags)

        # How old the STATE is — not how old the cache is
        age = self._sockjs_monitor.get_print_state_age()

        return PrinterState(
            connected=True,
            state=status,
            state_age_seconds=round(age, 1) if age is not None else None,
            tool_temp_actual=tool.get("actual") if isinstance(tool, dict) else None,
            tool_temp_target=tool.get("target") if isinstance(tool, dict) else None,
            bed_temp_actual=bed.get("actual") if isinstance(bed, dict) else None,
            bed_temp_target=bed.get("target") if isinstance(bed, dict) else None,
            chamber_temp_actual=chamber.get("actual") if isinstance(chamber, dict) else None,
            chamber_temp_target=chamber.get("target") if isinstance(chamber, dict) else None,
        )

    # ------------------------------------------------------------------
    # Flow-anomaly cross-check (kiln-pro nozzle wear)
    # ------------------------------------------------------------------

    def _fire_extrusion_event(self, event_type: str, severity: str) -> None:
        """Feed a flow-anomaly signal into kiln-pro's nozzle wear cross-check."""
        from kiln._pro_nozzle_bridge import report_flow_anomaly

        report_flow_anomaly(self.name, event_type, severity)

    def _check_flow_flag_transitions(self, flags: dict[str, Any]) -> None:
        """Detect ``state.flags`` transitions that should fire a flow-anomaly."""
        for flag_name in _FLOW_ANOMALY_FLAG_MAP:
            current = bool(flags.get(flag_name, False))
            previous = bool(self._prev_flow_flags.get(flag_name, False))
            if current and not previous:
                classification = _classify_flow_anomaly(f"flag:{flag_name}")
                if classification is not None:
                    event_type, severity = classification
                    self._fire_extrusion_event(event_type, severity)
            self._prev_flow_flags[flag_name] = current

    def _handle_push_state(self, current: dict[str, Any]) -> None:
        """SockJS ``current``-message callback."""
        try:
            state_obj = current.get("state", {})
            flags = state_obj.get("flags", {}) if isinstance(state_obj, dict) else {}
            if isinstance(flags, dict):
                self._check_flow_flag_transitions(flags)
        except Exception:
            logger.debug("flow-flag transition check failed (non-fatal)", exc_info=True)

    def _handle_push_event(self, event_name: str, payload: dict[str, Any]) -> None:
        """SockJS ``event``-message callback."""
        classification = _classify_flow_anomaly(event_name, payload)
        if classification is None:
            return
        event_type, severity = classification
        self._fire_extrusion_event(event_type, severity)

    # ------------------------------------------------------------------
    # PrinterAdapter -- job query
    # ------------------------------------------------------------------

    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active (or last) print job."""
        payload = self._get_json("/api/job")

        file_name = _safe_get(payload, "job", "file", "name")
        progress = _safe_get(payload, "progress", "completion")
        print_time = _safe_get(payload, "progress", "printTime")
        print_time_left = _safe_get(payload, "progress", "printTimeLeft")

        return JobProgress(
            file_name=file_name,
            completion=round(progress, 2) if progress is not None else None,
            print_time_seconds=int(print_time) if print_time is not None else None,
            print_time_left_seconds=int(print_time_left) if print_time_left is not None else None,
        )

    def list_files(self) -> list[PrinterFile]:
        """Return a list of files stored on OctoPrint's local storage."""
        payload = self._get_json(
            "/api/files/local",
            params={"recursive": "true"},
        )

        raw_files = payload.get("files", [])
        if not isinstance(raw_files, list):
            raw_files = []

        flat = _flatten_files(raw_files)

        results: list[PrinterFile] = []
        for entry in flat:
            results.append(
                PrinterFile(
                    name=entry.get("name", ""),
                    path=entry.get("path", entry.get("name", "")),
                    size_bytes=entry.get("size"),
                    date=entry.get("date"),
                )
            )
        return results

    # ------------------------------------------------------------------
    # PrinterAdapter -- file management
    # ------------------------------------------------------------------

    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a local G-code file to OctoPrint."""
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Local file not found: {abs_path}")

        filename = os.path.basename(abs_path)

        try:
            with open(abs_path, "rb") as fh:
                files_payload = {
                    "file": (filename, fh, "application/octet-stream"),
                }
                response = self._post(
                    "/api/files/local",
                    files=files_payload,
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

        uploaded_name = _safe_get(body, "files", "local", "name", default=filename)

        return UploadResult(
            success=True,
            file_name=uploaded_name,
            message=f"Uploaded {uploaded_name} to OctoPrint.",
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- print control
    # ------------------------------------------------------------------

    def _start_print_impl(self, file_name: str, **_kwargs: Any) -> PrintResult:
        """Begin printing a file that already exists on OctoPrint."""
        self._post(
            f"/api/files/local/{quote(file_name, safe='')}",
            json={"command": "select", "print": True},
        )
        return PrintResult(
            success=True,
            message=f"Started printing {file_name}.",
        )

    def cancel_print(self) -> PrintResult:
        """Cancel the currently running print job."""
        self._post("/api/job", json={"command": "cancel"})
        return PrintResult(success=True, message="Print cancelled.")

    def skip_objects(self, object_ids: list[int]) -> CommandVerdict:
        """Abandon objects on a live multi-object print via ``M486``."""
        if not object_ids:
            raise PrinterError("skip_objects requires at least one object id.")
        try:
            ids = [int(x) for x in object_ids]
        except (TypeError, ValueError) as exc:
            raise PrinterError(f"skip_objects: object ids must be integers ({exc}).") from exc
        self._post("/api/printer/command", json={"commands": [f"M486 P{i}" for i in ids]})
        return self._http_accepted(f"Skip object(s) {', '.join(str(i) for i in ids)}")

    def emergency_stop(self) -> PrintResult:
        """Perform emergency stop via M112 firmware halt."""
        self._post("/api/printer/command", json={"commands": ["M112"]})
        return PrintResult(
            success=True,
            message="Emergency stop triggered (M112 sent).",
        )

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job."""
        self._post(
            "/api/job",
            json={"command": "pause", "action": "pause"},
        )
        return PrintResult(success=True, message="Print paused.")

    def _resume_print_impl(self) -> PrintResult:
        """Resume a previously paused print job."""
        self._post(
            "/api/job",
            json={"command": "pause", "action": "resume"},
        )
        return PrintResult(success=True, message="Print resumed.")

    def firmware_resume_print(
        self,
        *,
        z_height_mm: float,
        hotend_temp_c: float,
        bed_temp_c: float,
        file_name: str,
        layer_number: int | None = None,
        fan_speed_pct: float = 100.0,
        flow_rate_pct: float = 100.0,
        prime_length_mm: float = 30.0,
        z_clearance_mm: float = 2.0,
    ) -> PrintResult:
        """Send Marlin M413 power-loss recovery positioning G-code."""
        # -- parameter validation ------------------------------------------
        if z_height_mm <= 0:
            raise PrinterError(f"z_height_mm must be > 0, got {z_height_mm}.")
        if hotend_temp_c <= 0:
            raise PrinterError(f"Hotend temperature must be > 0°C, got {hotend_temp_c}°C.")
        self._validate_temp(hotend_temp_c, 300.0, "Hotend")
        self._validate_temp(bed_temp_c, 130.0, "Bed")
        if prime_length_mm < 0:
            raise PrinterError(f"prime_length_mm must be >= 0, got {prime_length_mm}.")
        if z_clearance_mm <= 0 or z_clearance_mm > 10:
            raise PrinterError(f"z_clearance_mm must be > 0 and <= 10, got {z_clearance_mm}.")

        fan_pwm = int(fan_speed_pct * 2.55)
        commands = build_firmware_resume_positioning(
            z_height_mm=z_height_mm,
            hotend_temp_c=hotend_temp_c,
            bed_temp_c=bed_temp_c,
            fan_pwm=fan_pwm,
            flow_rate_pct=flow_rate_pct,
            prime_length_mm=prime_length_mm,
            z_clearance_mm=z_clearance_mm,
        )

        self.send_gcode(commands)

        layer_info = f" at layer {layer_number}" if layer_number is not None else ""
        return PrintResult(
            success=True,
            message=(
                f"Firmware resume positioning complete for {file_name}{layer_info}. "
                f"Z={z_height_mm}mm, hotend={hotend_temp_c}°C, bed={bed_temp_c}°C. "
                f"Printer is positioned and ready — start the resume file via start_print."
            ),
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- temperature control
    # ------------------------------------------------------------------

    def set_tool_temp(self, target: float) -> CommandVerdict:
        """Set the hotend (tool0) target temperature in degrees Celsius."""
        self._validate_temp(target, 300.0, "Hotend")
        self._post(
            "/api/printer/tool",
            json={"command": "target", "targets": {"tool0": int(target)}},
        )
        return self._http_accepted(f"Hotend target {int(target)}°C")

    def set_bed_temp(self, target: float) -> CommandVerdict:
        """Set the heated-bed target temperature in degrees Celsius."""
        self._validate_temp(target, 130.0, "Bed")
        self._post(
            "/api/printer/bed",
            json={"command": "target", "target": int(target)},
        )
        return self._http_accepted(f"Bed target {int(target)}°C")

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def clear_error(self) -> PrintResult:
        """Recover OctoPrint's link to a printer that faulted."""
        self._post("/api/connection", json={"command": "connect"})
        return PrintResult(
            success=True,
            message=(
                "Asked OctoPrint to reconnect to the printer. Re-read "
                "printer_status to confirm it came back ready."
            ),
        )

    def send_gcode(self, commands: list[str]) -> CommandVerdict:
        """Send G-code commands to OctoPrint."""
        self._post(
            "/api/printer/command",
            json={"commands": self._gcode_lines(commands)},
        )
        return self._http_accepted(f"{len(commands)} G-code line(s)")

    @staticmethod
    def _http_accepted(what: str) -> CommandVerdict:
        """The verdict for a write OctoPrint answered 2xx to."""
        return CommandVerdict.accepted_only(
            f"{what}: OctoPrint accepted the command and queued it for the "
            "printer. Execution is not reported back on this call — read "
            "printer_status to confirm.",
            corroboration="http_2xx",
        )

    # ------------------------------------------------------------------
    # Fan control
    # ------------------------------------------------------------------

    def set_fan(self, node: str, percent: int) -> CommandVerdict:
        """Set the part-cooling fan speed via ``M106``/``M107`` G-code."""
        speed = self._validate_part_fan(node, percent)
        return self.send_gcode([f"M106 S{speed}" if speed else "M107"])

    # ------------------------------------------------------------------
    # PrinterAdapter -- calibration
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Filament handling — the shared G-code sequence
    # ------------------------------------------------------------------

    def _load_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Heat, then feed ``length_mm`` — the manual-load the firmware's own Extrude button performs."""
        return self._gcode_filament_move(
            plan,
            signed_length_mm=float(plan.length_mm or DEFAULT_LOAD_LENGTH_MM),
            mechanism="octoprint_api_printer_command",
        )

    def _unload_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Heat, then retract ``length_mm`` to free the melt zone."""
        return self._gcode_filament_move(
            plan,
            signed_length_mm=-float(plan.length_mm or DEFAULT_UNLOAD_LENGTH_MM),
            mechanism="octoprint_api_printer_command",
        )

    def _purge_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Heat, then extrude ``length_mm`` — the clog test."""
        return self._gcode_filament_move(
            plan,
            signed_length_mm=float(plan.length_mm or DEFAULT_PURGE_LENGTH_MM),
            mechanism="octoprint_api_printer_command",
        )

    def run_calibration(self, *, options: list[str] | None = None) -> PrintResult:
        """Run calibration routines via G-code commands."""
        if options is None:
            options = ["bed_leveling"]

        supported = {"bed_leveling", "all"}
        unsupported = [opt for opt in options if opt not in supported]
        if unsupported:
            return PrintResult(
                success=False,
                message=(
                    f"Calibration option(s) {', '.join(repr(o) for o in unsupported)} "
                    "not supported on OctoPrint. Only bed_leveling is available."
                ),
            )

        self._post(
            "/api/printer/command",
            json={"commands": ["G28", "G29"]},
        )
        return PrintResult(
            success=True,
            message=(
                "Bed leveling started (G28 + G29 sent). "
                "The printer will home and probe the bed."
            ),
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- file deletion
    # ------------------------------------------------------------------

    def delete_file(self, file_path: str) -> bool:
        """Delete a G-code file from OctoPrint's local storage."""
        self._request("DELETE", f"/api/files/local/{quote(file_path, safe='')}")
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- webcam snapshot
    # ------------------------------------------------------------------

    def get_snapshot(self) -> bytes | None:
        """Capture a webcam snapshot from OctoPrint."""
        try:
            response = self._session.get(
                self._url("/webcam/?action=snapshot"),
                timeout=10,
            )
            if response.ok and response.content:
                return response.content
            if not response.ok:
                raise PrinterError(
                    f"Webcam snapshot failed (HTTP {response.status_code}). "
                    f"Check that mjpg-streamer is running and the webcam is "
                    f"configured in OctoPrint."
                )
            return None
        except PrinterError:
            raise
        except Timeout as exc:
            raise PrinterError(
                "Webcam snapshot timed out after 10s. Check that the webcam is connected and mjpg-streamer is running."
            ) from exc
        except ReqConnectionError as exc:
            raise PrinterError(
                "Webcam snapshot failed: could not connect. Check that the "
                "webcam is configured in OctoPrint and the printer is online."
            ) from exc
        except RequestException as exc:
            raise PrinterError(
                f"Webcam snapshot failed: {exc}. "
                "Check webcam configuration in OctoPrint. Retry with `get_snapshot()`.",
            ) from exc
        except Exception as exc:
            raise PrinterError(
                f"Webcam snapshot failed unexpectedly: {exc}. "
                "Retry with `get_snapshot()`.",
            ) from exc

    # ------------------------------------------------------------------
    # PrinterAdapter -- webcam streaming
    # ------------------------------------------------------------------

    def get_stream_url(self) -> str | None:
        """Return the MJPEG stream URL for OctoPrint's webcam."""
        return f"{self._host}/webcam/?action=stream"

    camera_check_ids: ClassVar[tuple[str, ...]] = ("octoprint_webcam_stream",)

    def camera_probes(self) -> list[Any]:
        """The one address this adapter opens for OctoPrint's webcam."""
        from kiln.camera_check import CameraProbe

        return [
            CameraProbe(
                "octoprint_webcam_stream",
                f"{self._host}/webcam/?action=stream",
                "OctoPrint's own configuration reference (config.yaml, webcam section) gives "
                "the stream as the MJPG-Streamer path ?action=stream on the stream host; this "
                "adapter has always opened it as /webcam/?action=stream on the printer's own "
                "host, the same address its snapshots and live video use. Source: the maker's "
                "documentation for the path, this adapter for the host.",
            )
        ]

    # -- filament sensor (optional) ----------------------------------------

    def get_filament_status(self) -> dict[str, Any] | None:
        """Query OctoPrint for filament sensor status via the Filament Manager plugin."""
        try:
            payload = self._get_json("/api/plugin/filamentmanager")
            selections = payload.get("selections", [])
            detected = len(selections) > 0 and any(s.get("spool") is not None for s in selections)
            return {
                "detected": detected,
                "sensor_enabled": True,
                "source": "filamentmanager_plugin",
                "selections": selections,
            }
        except Exception:
            logger.debug(
                "Filament sensor query failed (plugin may not be installed)",
                exc_info=True,
            )
            return None

    # -- bed mesh (optional) -----------------------------------------------

    def get_bed_mesh(self) -> dict[str, Any] | None:
        """Query OctoPrint for bed mesh data via the Bed Level Visualizer plugin."""
        try:
            payload = self._get_json("/api/plugin/bedlevelvisualizer")
            mesh_data = payload.get("mesh", payload.get("bed_level_visualizer"))
            if not mesh_data:
                return None
            return payload
        except Exception:
            logger.debug("Bed mesh query failed (plugin may not be installed)", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    def get_firmware_status(self) -> FirmwareStatus | None:
        """Check OctoPrint's Software Update plugin for available updates."""
        try:
            data = self._get_json("/plugin/softwareupdate/check")
        except Exception:
            logger.debug(
                "Software update check failed (plugin may not be installed)",
                exc_info=True,
            )
            return None

        information = data.get("information", {})
        busy = bool(data.get("busy", False))

        components: list[FirmwareComponent] = []
        updates_available = 0

        for comp_name, info in information.items():
            if not isinstance(info, dict):
                continue

            display = info.get("displayName", comp_name)
            current = info.get("information", {}).get("local", {}).get("value", "")
            remote = info.get("information", {}).get("remote", {}).get("value", "")
            has_update = bool(info.get("updateAvailable", False))

            if has_update:
                updates_available += 1

            components.append(
                FirmwareComponent(
                    name=display,
                    current_version=str(current),
                    remote_version=str(remote) if remote else None,
                    update_available=has_update,
                    component_type="octoprint_plugin",
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
        """Trigger an update via OctoPrint's Software Update plugin."""
        try:
            state = self.get_state()
            if state.effective_state == PrinterStatus.PRINTING:
                raise PrinterError("Cannot update firmware while printing. Wait for the current print to finish.")
        except PrinterError:
            raise
        except Exception as exc:
            raise PrinterError(
                f"Cannot verify printer is idle before firmware update: {exc}. Aborting for safety.",
                cause=exc,
            ) from exc

        # Build the update targets
        if component:
            targets = [component]
        else:
            # Get all components with updates available
            status = self.get_firmware_status()
            if status is None:
                raise PrinterError("Software Update plugin not available on this OctoPrint instance.")
            targets = [c.name for c in status.components if c.update_available]
            if not targets:
                return FirmwareUpdateResult(
                    success=True,
                    message="All components are already up to date.",
                    component=None,
                )

        try:
            self._post(
                "/plugin/softwareupdate/update",
                json={"targets": targets, "force": False},
            )
        except PrinterError:
            raise
        except Exception as exc:
            raise PrinterError(
                f"Firmware update failed: {exc}. "
                "Check OctoPrint logs for details. Retry with `update_machine_firmware`.",
                cause=exc,
            ) from exc

        target_str = component or "all components"
        return FirmwareUpdateResult(
            success=True,
            message=f"Update started for {target_str}. OctoPrint may restart.",
            component=component,
        )

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<OctoPrintAdapter host={self._host!r}>"
