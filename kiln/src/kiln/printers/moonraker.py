"""Klipper/Moonraker adapter for the Kiln printer abstraction layer.

Implements :class:`~kiln.printers.base.PrinterAdapter` by talking to the
`Moonraker HTTP API <https://moonraker.readthedocs.io/en/latest/web_api/>`_
via :mod:`requests`.  Moonraker is the API server that sits in front of
Klipper, providing a REST+WebSocket interface for printer control.

The adapter mirrors the retry and error-handling patterns established by
:class:`~kiln.printers.octoprint.OctoPrintAdapter`.
"""

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

from kiln.printers.base import (
    FirmwareComponent,
    FirmwareStatus,
    FirmwareUpdateResult,
    JobProgress,
    JobResult,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)

# websocket-client is an optional dependency; the adapter works without it
# but push monitoring requires it.
try:
    import websocket as _ws_mod  # websocket-client

    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ws_mod = None  # type: ignore[assignment]
    _WS_AVAILABLE = False

logger = logging.getLogger(__name__)

# HTTP status codes eligible for automatic retry.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({502, 503, 504})

# Mapping from Moonraker's ``klippy_state`` / ``state`` strings to the
# canonical :class:`PrinterStatus` enum.  Moonraker reports the Klipper
# state via ``GET /printer/info`` in the ``state`` field.
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

# How the last job ENDED, for the ``print_stats.state`` values that say so.
# Klipper is unusually explicit here — it distinguishes "complete" from
# "cancelled" natively — and both used to arrive as the same IDLE, which
# threw the distinction away at the one adapter best placed to keep it.
# ``standby`` is absent on purpose: it is a printer that has not run a job
# this session, which is a genuine absence of information rather than an
# ending.  ``error`` too — that is a Klipper fault state, not a verdict on
# a job, and the status it already maps to says so.
_JOB_RESULT_MAP: dict[str, JobResult] = {
    "complete": JobResult.COMPLETED,
    "cancelled": JobResult.CANCELLED,
}


def _map_moonraker_job_result(print_state: str | None) -> JobResult | None:
    """How the last job ended, per ``print_stats.state``, or ``None``.

    Only ``print_stats`` can answer this; the klippy connection state
    describes the host process, not a print.
    """
    if not print_state:
        return None
    return _JOB_RESULT_MAP.get(print_state)


# Klipper's two filament runout sensor modules.  Both are registered under
# "<type> <name>" (see get_filament_status), so these are section-name
# prefixes to match against, never queryable object names on their own.
_FILAMENT_SENSOR_TYPES: tuple[str, ...] = (
    "filament_switch_sensor",
    "filament_motion_sensor",
)


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


def _merge_status_into(cache: dict[str, Any], status: dict[str, Any]) -> None:
    """Merge a Moonraker status payload into *cache*, per printer object.

    Klipper's subscriptions push **deltas**, not snapshots: for each subscribed
    object it compares every requested field against the previous poll and sends
    only what changed.  During a live print that means a steady stream of
    ``{"print_stats": {"total_duration": 12.3}}`` -- ``state`` changed once, at
    the start, so it is absent from nearly every later frame.

    A flat ``cache.update(status)`` would replace the whole cached object with
    that one-key delta and discard every field that simply had not changed,
    leaving ``print_stats`` with no ``state`` and the printer reported idle
    mid-print.  Merging one level down keeps the unchanged fields.

    Each object is replaced with a *new* dict rather than mutated in place, so
    snapshots already handed out by ``get_cached_state()`` stay unchanged.
    """
    for obj_name, value in status.items():
        existing = cache.get(obj_name)
        if isinstance(value, dict) and isinstance(existing, dict):
            cache[obj_name] = {**existing, **value}
        else:
            cache[obj_name] = value


def _map_moonraker_state(state_string: str, print_state: str | None = None) -> PrinterStatus:
    """Translate a Moonraker state string to a :class:`PrinterStatus`.

    Moonraker exposes two relevant state fields:
    * ``GET /printer/info`` -> ``state`` (klippy connection state)
    * ``GET /printer/objects/query?print_stats`` -> ``print_stats.state``

    The *print_state* (from ``print_stats``) is checked first when the
    klippy state is ``"ready"`` because the printer may be idle at the
    firmware level while actively printing.

    Args:
        state_string: The ``state`` field from ``GET /printer/info``.
        print_state: Optional ``print_stats.state`` field (e.g. ``"printing"``,
            ``"paused"``, ``"standby"``, ``"complete"``, ``"cancelled"``,
            ``"error"``).

    Returns:
        The corresponding :class:`PrinterStatus` value.
    """
    # When Klipper is ready, defer to the print_stats state for finer
    # granularity (printing, paused, standby, etc.).
    if state_string == "ready" and print_state:
        mapped = _STATE_MAP.get(print_state)
        if mapped is not None:
            return mapped

    return _STATE_MAP.get(state_string, PrinterStatus.UNKNOWN)


# Substrings (lowercased) that, when present in ``print_stats.message``
# alongside a ``state == "error"``, indicate the print stopped because
# of a flow / extrusion issue rather than a general firmware fault.
# Conservative on purpose: a false-positive flow-anomaly tag poisons the
# kiln-pro nozzle wear cross-check more than a missed positive does.
#
# Sources:
#   * Klipper ``filament_switch_sensor`` and ``filament_motion_sensor``
#     emit ``"Filament Sensor <name>: Runout"`` style messages via
#     ``pause_resume`` when the sensor trips
#     (klipper/klippy/extras/filament_switch_sensor.py,
#      klipper/klippy/extras/filament_motion_sensor.py).
#   * Klipper ``print_stats`` records the trigger string in
#     ``print_stats.message`` (klipper/klippy/extras/print_stats.py)
#     which Moonraker surfaces over the JSON-RPC subscription.
#
# Substrings are matched case-insensitively after ``.lower()``.  The
# "filament" + "runout"/"jam" combination is the conservative bar —
# a bare "error" state without one of these keywords is treated as
# unknown and DOES NOT fire the wear cross-check.
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
    """Return (event_type, severity) for a Moonraker/Klipper flow signal.

    Inspects ``print_stats.state`` together with ``print_stats.message``
    (both fields are exposed by Moonraker's
    ``GET /printer/objects/query?print_stats`` and over the JSON-RPC
    ``notify_status_update`` push channel).  Returns ``None`` when the
    state isn't an error OR the message doesn't carry a flow-related
    substring — a generic error without a filament hint is too broad
    to feed into the wear cross-check, so we drop it.

    Classification:
      * Filament runout / sensor trip / jam → ``("filament_jam", "high")``
      * Extruder shutdown / under-extrusion → ``("under_extrusion", "medium")``
      * Anything else → ``None``

    Args:
        print_state: The ``print_stats.state`` field, lowercase one of
            ``printing|paused|complete|error|cancelled|standby``.
        message: The ``print_stats.message`` field; Klipper writes the
            shutdown reason here when state transitions to ``error``.
    """
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
# WebSocket push monitor
# ---------------------------------------------------------------------------

def _push_monitoring_enabled() -> bool:
    """Check whether push monitoring is enabled (read at call time, not import time)."""
    return os.environ.get("KILN_PUSH_MONITORING", "0") == "1"

# Maximum reconnect backoff in seconds.
_MAX_RECONNECT_BACKOFF: float = 30.0


class MoonrakerWebSocketMonitor:
    """Push-based status monitor for Moonraker via its native WebSocket API.

    Subscribes to ``print_stats``, ``heater_bed``, ``extruder``, and
    ``display_status`` objects.  Incoming status updates are written to a
    shared cache dict that :meth:`MoonrakerAdapter.get_state` can consult
    to avoid an HTTP round-trip.

    The monitor runs in a daemon thread and reconnects automatically with
    exponential backoff (1 s -> 2 s -> 4 s -> ... -> 30 s max).

    Args:
        host: Moonraker base URL (``http://...`` or ``https://...``).
        on_state_update: Optional callback fired on every status message.
            Receives the full ``status`` dict from the Moonraker notification.
    """

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

        # Last-fired flow-anomaly key (state, message) so we don't
        # re-fire the kiln-pro wear cross-check on every push for a
        # stuck error state.  The recorder de-dupes by time bucket too,
        # but skipping the import + cross-process call on every status
        # tick is cheaper.
        self._last_flow_anomaly_key: tuple[str, str] | None = None

        # Shared state cache -- written by the WS thread, read by the adapter.
        self._cache: dict[str, Any] = {}
        self._cache_lock: threading.Lock = threading.Lock()
        self._connected: bool = False

        # When a push last carried the PRINT STATE, as distinct from when
        # the cache was last written at all.  Klipper sends deltas, so a
        # single cache-wide clock would answer "when did any field
        # arrive" — a temperature tick every second would report a fresh
        # age beside a print_stats.state that stopped updating minutes
        # ago, which is the exact reassuring-lie this age exists to
        # prevent.  ``None`` until a push actually carries the state.
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
        """Note that this push carried the print state.  Holds the lock.

        Only a payload containing ``print_stats`` counts.  Everything else
        Klipper subscribes us to — temperatures, fans, the toolhead — says
        nothing about whether the machine is still printing.
        """
        if isinstance(status.get("print_stats"), dict):
            self._print_state_time = time.time()

    def get_print_state_age(self) -> float | None:
        """Seconds since a push last carried the print state, or ``None``.

        ``None`` means no push ever has — which is not a claim of
        freshness, and the caller must pass it through as-is rather than
        substituting a zero.
        """
        with self._cache_lock:
            if self._print_state_time is None:
                return None
            return max(0.0, time.time() - self._print_state_time)

    def start(self) -> None:
        """Start the background listener thread.

        No-op if the thread is already running or if ``websocket-client``
        is not installed.
        """
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

        # Moonraker sends subscription updates as JSON-RPC notifications
        # with method "notify_status_update".
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
        """Feed flow / extrusion signals into the kiln-pro wear cross-check.

        When Klipper's ``print_stats`` reports an ``error`` state with a
        message that names a filament / extruder issue (filament-sensor
        runout, extruder shutdown, under-extrusion), call kiln-pro's
        ``record_extrusion_event`` so the nozzle wear estimator learns
        from real-print flow failures alongside gram counts.

        Free-tier installs (no kiln-pro) silently skip via the
        ``ImportError`` guard — the adapter remains free-tier-functional.

        De-duplication: each (state, message) pair fires once per
        WebSocket session.  Klipper repeats the same status in every
        subsequent ``notify_status_update`` while the printer is stuck
        on the error; without the de-dupe the cross-check would see N
        repeated events per stuck print.
        """
        print_stats = status.get("print_stats")
        if not isinstance(print_stats, dict):
            return
        state = print_stats.get("state")
        message = print_stats.get("message")
        if not isinstance(state, str):
            return

        classification = _classify_flow_anomaly(state, message)
        if classification is None:
            # State recovered or never matched — clear the dedupe key
            # so the next genuine anomaly fires.
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
        try:
            from kiln_pro.nozzle_intelligence.sensor_signal import (
                record_extrusion_event_for_printer,
            )
            record_extrusion_event_for_printer(
                printer_id=self._printer_name,
                event_type=event_type,
                severity=severity,
            )
        except ImportError:
            # Free tier — kiln-pro nozzle module not installed.  Drop
            # the signal silently so the public adapter stays
            # free-tier-functional.
            pass
        except Exception as exc:  # pragma: no cover
            logger.debug(
                "Flow-anomaly cross-check raised (non-fatal): %s",
                exc,
            )

    def _on_error(self, ws: Any, error: Any) -> None:
        logger.debug("Moonraker WS error: %s", error)

    def _on_close(self, ws: Any, close_status_code: Any, close_msg: Any) -> None:
        self._connected = False
        logger.info("Moonraker WS closed (code=%s)", close_status_code)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MoonrakerAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by the Moonraker HTTP API.

    Args:
        host: Base URL of the Moonraker instance, e.g.
            ``"http://klipper.local"`` or ``"http://192.168.1.50:7125"``.
        api_key: Optional API key.  Moonraker typically does not require
            authentication, but an API key can be provided for setups that
            use a trusted-client or API-key authentication.  When provided
            the key is sent as the ``X-Api-Key`` header on every request.
        timeout: Per-request timeout in seconds.
        retries: Maximum number of attempts for transient failures
            (connection errors and HTTP 502/503/504).

    Raises:
        ValueError: If *host* is empty.

    Example::

        adapter = MoonrakerAdapter("http://klipper.local:7125")
        state = adapter.get_state()
        print(state.state, state.tool_temp_actual)
    """

    # print_stats.print_duration is Klipper's own job clock, frozen at the
    # ending — a late reading is merely late and still correct.
    _DURATION_SEMANTICS: ClassVar[str] = "frozen"

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

        # Print-history backfill: fired once per adapter instance, the
        # first time a request confirms the server is actually there.
        self._history_backfilled: bool = False
        self._history_backfill_thread: threading.Thread | None = None

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
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
            can_pause=True,
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
        """Start the WebSocket push monitor for real-time status updates.

        Falls back gracefully if ``websocket-client`` is not installed.
        """
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
        """Execute an HTTP request with exponential-backoff retry logic.

        Returns the :class:`requests.Response` on success (2xx).

        Raises:
            PrinterError: On non-retryable HTTP errors, connection failures,
                timeouts, or when all retry attempts are exhausted.
        """
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
                        hint = " API key may be invalid. Check KILN_PRINTER_API_KEY or Moonraker auth config. Retry with `get_state()`."
                    elif sc == 403:
                        hint = " Insufficient permissions. Check Moonraker's authorization config. Retry with `get_state()`."
                    elif sc == 404:
                        hint = " Resource not found — the endpoint or file may not exist. Verify with `list_files()`."
                    elif sc == 409:
                        hint = " Conflict — printer may be busy with another operation. Check `get_state()` first."
                    else:
                        hint = " Retry with `get_state()` to check printer status."
                    raise PrinterError(
                        f"Moonraker returned HTTP {sc} for {method} {path}: {body}.{hint}",
                    )

                # Retryable HTTP status -- fall through to backoff.
                last_exc = PrinterError(
                    f"Moonraker returned HTTP {response.status_code} "
                    f"for {method} {path} "
                    f"(attempt {attempt + 1}/{self._retries}). "
                    f"Moonraker backend may be unavailable — check that the service is running. "
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
                    f"Could not connect to Moonraker at {self._host} (attempt {attempt + 1}/{self._retries})",
                    cause=exc,
                )
            except RequestException as exc:
                # Non-transient request errors -- raise immediately.
                raise PrinterError(
                    f"Request error for {method} {path}: {exc}. "
                    f"Cannot reach Moonraker at {self._host}. Check network and that Moonraker is running. "
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
        """Shorthand: GET *path* and return the parsed JSON body.

        Raises :class:`PrinterError` if the response body is not valid JSON.
        """
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
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> requests.Response:
        """Shorthand for POST requests."""
        return self._request("POST", path, json=json, params=params, files=files, data=data)

    def _send_gcode(self, script: str) -> requests.Response:
        """Send a G-code script to Klipper via Moonraker.

        Calls ``POST /printer/gcode/script?script=<command>``.

        Args:
            script: One or more G-code commands separated by newlines.

        Returns:
            The HTTP response from Moonraker.

        Raises:
            PrinterError: If the command fails.
        """
        return self._post("/printer/gcode/script", params={"script": script})

    # ------------------------------------------------------------------
    # PrinterAdapter -- state queries
    # ------------------------------------------------------------------

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures.

        This is also Moonraker's connect path: the adapter has no
        explicit ``connect()``, so the first status that comes back
        connected IS the confirmation that the server is reachable.  That
        is where the one-shot print-history backfill fires.

        See :meth:`_read_state` for the status protocol itself.
        """
        state = self._read_state()
        if getattr(state, "connected", False):
            self._maybe_backfill_history()
        return state

    def _maybe_backfill_history(self) -> None:
        """Adopt the server's own job history — once per adapter instance.

        Moonraker keeps a complete record of every job it ran, including
        the years before Kiln was installed; :mod:`kiln.printers.moonraker_history`
        turns that into real print outcomes.  Wired HERE, at the engine's
        connect path, rather than behind a new tool: no user should have
        to know an import exists to get their own history.

        Three things keep it from ever costing a connection: the flag is
        set BEFORE any work (a failure never re-arms a retry storm), the
        work runs on a daemon thread off the status path (an HTTP fetch
        plus N row writes must not be paid by a caller asking for a
        temperature), and every layer swallows its own exceptions.
        """
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
        """Thread body for the one-shot history import.  Never raises."""
        try:
            from kiln.printers.moonraker_history import backfill_history

            backfill_history(self)
        except Exception as exc:  # noqa: BLE001 — courtesy import, non-fatal
            logger.debug("Moonraker history backfill failed: %s", exc)

    def _read_state(self) -> PrinterState:
        """Fetch the current printer state and temperatures.

        When push monitoring is active and the WebSocket is connected,
        builds the state from the cached data to avoid an HTTP round-trip.
        Falls back to HTTP polling if the cache is empty or the WebSocket
        is disconnected.

        Issues two Moonraker requests (HTTP fallback path):
        * ``GET /printer/info`` -- klippy state and connection info
        * ``GET /printer/objects/query?heater_bed&extruder&print_stats`` --
          temperatures and print state

        Returns an OFFLINE state when Moonraker is unreachable rather than
        raising, so callers always get a usable :class:`PrinterState`.

        Raises:
            PrinterError: On unexpected (non-connection) errors.
        """
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

        # If Klipper itself is not ready, we can still report the high-level
        # state without querying objects (which would likely fail).
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
            # If the objects query fails we still know the printer is
            # connected, just cannot read temps.
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

        # Print stats -- used to refine the status when Klipper is "ready"
        print_stats = _safe_get(status, "print_stats", default={})
        print_state = print_stats.get("state") if isinstance(print_stats, dict) else None

        mapped_status = _map_moonraker_state(klippy_state, print_state)

        # Chamber (optional — only present if Klipper has a
        # [temperature_sensor chamber] section in printer.cfg).
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
        """Build a :class:`PrinterState` from the WebSocket cache.

        Returns ``None`` if push monitoring is inactive, disconnected,
        or the cache is empty -- signalling the caller to fall back to HTTP.
        """
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

        # When using push cache, klippy must be "ready" (otherwise the
        # subscription would not be active), so we default to "ready".
        mapped_status = _map_moonraker_state("ready", print_state)

        # Chamber
        chamber = cached.get("temperature_sensor chamber", {})
        chamber_actual = chamber.get("temperature") if isinstance(chamber, dict) else None

        # How old the STATE is — not how old the cache is.  This is the
        # push path, so the answer is only as current as the last frame
        # that carried print_stats.  Without it the identical get_state
        # returns a cache-backed reading with no age, and the "these
        # readings may be stale" warning can never fire on this backend.
        # The HTTP fallback below deliberately has no age: it asks the
        # printer on every call, so its reading is current by construction.
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
        """Retrieve progress info for the active (or last) print job.

        Queries ``GET /printer/objects/query?print_stats&virtual_sdcard``.

        Raises:
            PrinterError: On communication or parsing errors.
        """
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

        # virtual_sdcard
        vsd = _safe_get(status, "virtual_sdcard", default={})
        progress = vsd.get("progress") if isinstance(vsd, dict) else None

        # Moonraker reports progress as 0.0--1.0; convert to 0.0--100.0 to
        # match the PrinterAdapter contract.
        completion: float | None = None
        if progress is not None:
            completion = round(float(progress) * 100.0, 2)

        # Estimate time left based on progress and elapsed time.
        print_time_seconds: int | None = None
        print_time_left_seconds: int | None = None

        if print_duration is not None:
            # Round rather than truncate: a print a fraction of a second in
            # otherwise reports 0 s elapsed, which reads as "not started".
            print_time_seconds = round(float(print_duration))

        if print_time_seconds is not None and completion is not None and completion > 0:
            # total_estimated = elapsed / (completion / 100)
            total_estimated = print_time_seconds / (completion / 100.0)
            print_time_left_seconds = max(0, int(total_estimated - print_time_seconds))

        return JobProgress(
            file_name=file_name if file_name else None,
            completion=completion,
            print_time_seconds=print_time_seconds,
            print_time_left_seconds=print_time_left_seconds,
        )

    def list_files(self) -> list[PrinterFile]:
        """Return a list of G-code files stored on the Klipper host.

        Calls ``GET /server/files/list?root=gcodes``.

        Raises:
            PrinterError: On communication or parsing errors.
        """
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
        """Upload a local G-code file to the Klipper host via Moonraker.

        Calls ``POST /server/files/upload`` with a multipart file upload.

        Args:
            file_path: Absolute or relative path to the local file.

        Raises:
            PrinterError: On communication errors.
            FileNotFoundError: If *file_path* does not exist locally.
        """
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

        Calls ``POST /printer/print/start?filename=<file_name>``.

        Args:
            file_name: Name (or path) of the file as known by Moonraker.

        Raises:
            PrinterError: If the printer cannot start the job.
        """
        self._post(
            "/printer/print/start",
            params={"filename": file_name},
        )
        return PrintResult(
            success=True,
            message=f"Started printing {file_name}.",
        )

    def cancel_print(self) -> PrintResult:
        """Cancel the currently running print job.

        Calls ``POST /printer/print/cancel``.

        Raises:
            PrinterError: If the cancellation fails.
        """
        self._post("/printer/print/cancel")
        return PrintResult(success=True, message="Print cancelled.")

    def emergency_stop(self) -> PrintResult:
        """Perform emergency stop via Moonraker's dedicated endpoint.

        Calls ``POST /printer/emergency_stop`` which immediately halts
        all motion and cuts power to heaters at the firmware level.
        """
        self._post("/printer/emergency_stop")
        return PrintResult(
            success=True,
            message="Emergency stop triggered.",
        )

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job.

        Calls ``POST /printer/print/pause``.

        Raises:
            PrinterError: If the printer cannot pause.
        """
        self._post("/printer/print/pause")
        return PrintResult(success=True, message="Print paused.")

    def _resume_print_impl(self) -> PrintResult:
        """Resume a previously paused print job.

        Calls ``POST /printer/print/resume``.  The not-paused gate runs in the
        base :meth:`resume_print` template, so by the time we reach here the
        print is paused (or the state was uncertain and we fail open).

        Raises:
            PrinterError: If the printer cannot resume.
        """
        self._post("/printer/print/resume")
        return PrintResult(success=True, message="Print resumed.")

    # ------------------------------------------------------------------
    # PrinterAdapter -- temperature control
    # ------------------------------------------------------------------

    def set_tool_temp(self, target: float) -> bool:
        """Set the hotend (extruder) target temperature in degrees Celsius.

        Moonraker does not have a dedicated temperature-set endpoint.
        Instead we send the ``M104`` G-code command via
        ``POST /printer/gcode/script``.

        Args:
            target: Target temperature.  Pass ``0`` to turn the heater off.

        Returns:
            ``True`` if the command was accepted.

        Raises:
            PrinterError: If the command fails.
        """
        self._validate_temp(target, 300.0, "Hotend")
        self._send_gcode(f"M104 S{int(target)}")
        return True

    def set_bed_temp(self, target: float) -> bool:
        """Set the heated-bed target temperature in degrees Celsius.

        Sends the ``M140`` G-code command via Moonraker's gcode script
        endpoint.

        Args:
            target: Target temperature.  Pass ``0`` to turn the heater off.

        Returns:
            ``True`` if the command was accepted.

        Raises:
            PrinterError: If the command fails.
        """
        self._validate_temp(target, 130.0, "Bed")
        self._send_gcode(f"M140 S{int(target)}")
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def clear_error(self) -> PrintResult:
        """Clear a Klipper shutdown/error state with ``FIRMWARE_RESTART``.

        A Klipper host that has shut down — after an ``M112``, a failed
        homing move, a thermal fault — refuses every subsequent command until
        the firmware is restarted, which is exactly the dead end this method
        exists to open.  Moonraker exposes it as its own endpoint rather than
        as a G-code line, because a shut-down Klipper will not accept G-code.

        The restart re-initialises the MCU; it does not clear the CAUSE.  A
        printer that shut down for a real fault will shut down again, which is
        the correct outcome — this reconciles Kiln with the machine, it does
        not overrule the machine.
        """
        self._post("/printer/firmware_restart")
        return PrintResult(
            success=True,
            message=(
                "Sent FIRMWARE_RESTART. Re-read printer_status to confirm "
                "Klipper came back ready."
            ),
        )

    def send_gcode(self, commands: list[str]) -> bool:
        """Send G-code commands to Klipper via Moonraker.

        Joins all commands into a single newline-separated script and
        sends them via ``POST /printer/gcode/script``.

        Args:
            commands: List of G-code command strings.

        Returns:
            ``True`` if the commands were accepted.

        Raises:
            PrinterError: If sending fails.
        """
        script = "\n".join(commands)
        self._send_gcode(script)
        return True

    # ------------------------------------------------------------------
    # Fan control
    # ------------------------------------------------------------------

    def set_fan(self, node: str, percent: int) -> bool:
        """Set the part-cooling fan speed via ``M106``/``M107`` G-code.

        Only the single default part-cooling fan is supported — see
        :meth:`PrinterAdapter._validate_part_fan` for why auxiliary/chamber
        fan names are rejected here. A Klipper machine that names a chamber
        or auxiliary fan in its own ``printer.cfg`` (e.g. via
        ``SET_FAN_SPEED FAN=chamber_fan``) isn't reachable through this
        generic path — Kiln has no way to discover that machine-specific name.

        Args:
            node: Must be ``"part"`` (or the aliases ``"part_cooling"`` /
                ``"cooling"``) — the part-cooling fan.
            percent: Fan speed 0-100 (0 turns the fan off, 100 is full speed).

        Returns:
            ``True`` once the command is sent.

        Raises:
            PrinterError: If *node* is not the part-cooling fan, or *percent*
                is outside 0-100.
        """
        speed = self._validate_part_fan(node, percent)
        self._send_gcode(f"M106 S{speed}" if speed else "M107")
        return True

    def skip_objects(self, object_names: list[str]) -> bool:
        """Abandon named objects on a live Klipper multi-object print.

        Uses Klipper's ``EXCLUDE_OBJECT NAME=<name>`` — the print keeps going
        for every other object.  This only works when the file was sliced with
        object labelling on (the slicer emits ``EXCLUDE_OBJECT_DEFINE`` per
        object); the *object_names* are those labels.  Klipper has no live API
        to list them mid-print, so the caller supplies the names (from the
        sliced file or the slicer's object list).

        Irreversible for the objects named; only meaningful while a
        multi-object print is active.

        Args:
            object_names: Klipper object names to exclude (non-empty).

        Returns:
            ``True`` once the exclude commands are sent.

        Raises:
            PrinterError: If *object_names* is empty or all blank.
        """
        names = [str(n).strip() for n in (object_names or []) if str(n).strip()]
        if not names:
            raise PrinterError("skip_objects requires at least one object name.")
        return self.send_gcode([f"EXCLUDE_OBJECT NAME={n}" for n in names])

    # ------------------------------------------------------------------
    # PrinterAdapter -- calibration
    # ------------------------------------------------------------------

    _CALIBRATION_MACROS: dict[str, str] = {
        "bed_leveling": "BED_MESH_CALIBRATE",
        "vibration": "SHAPER_CALIBRATE",
    }

    def run_calibration(self, *, options: list[str] | None = None) -> PrintResult:
        """Run calibration routines via Klipper G-code macros.

        Moonraker/Klipper supports calibration through built-in macros:

        * ``"bed_leveling"`` — runs ``BED_MESH_CALIBRATE`` to probe and
          generate a bed mesh compensation map.
        * ``"vibration"`` — runs ``SHAPER_CALIBRATE`` to measure and
          compensate for vibration (input shaper).
        * ``"all"`` — runs both of the above sequentially.

        Other calibration types (e.g. ``"flow"``) are not natively
        available and require printer-specific macros.

        Args:
            options: Which routines to run.  Accepts ``"bed_leveling"``,
                ``"vibration"``, or ``"all"``.
                Defaults to ``["bed_leveling"]``.
        """
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
        """Delete a G-code file from the Klipper host via Moonraker.

        Calls ``DELETE /server/files/gcodes/{file_path}``.

        Args:
            file_path: Path of the file as returned by ``list_files()``.

        Returns:
            ``True`` if the file was deleted.

        Raises:
            PrinterError: If deletion fails.
        """
        encoded = quote(file_path, safe="")
        self._request("DELETE", f"/server/files/gcodes/{encoded}")
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- webcam snapshot
    # ------------------------------------------------------------------

    def get_snapshot(self) -> bytes | None:
        """Capture a webcam snapshot from Moonraker.

        Discovers the webcam snapshot URL via
        ``GET /server/webcams/list`` and then fetches the image.

        Raises:
            PrinterError: With a diagnostic message if no webcams are
                configured or the snapshot request fails.
        """
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
                # Fall back to stream_url if available
                stream_url = cam.get("stream_url") or cam.get("urlStream")
                if stream_url:
                    snapshot_url = stream_url.replace("/stream", "/?action=snapshot")
                else:
                    raise PrinterError(
                        "Webcam found in Moonraker but no snapshot URL configured. Check your webcam configuration."
                    )

            # If the URL is relative, prepend the host
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
                "Webcam snapshot failed: could not connect. Check that the "
                "webcam is configured and the printer is online."
            ) from exc
        except RequestException as exc:
            raise PrinterError(
                f"Webcam snapshot failed: {exc}. "
                "Check webcam configuration in Moonraker. Retry with `get_snapshot()`.",
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
        """Discover and return the MJPEG stream URL from Moonraker.

        Queries ``GET /server/webcams/list`` and returns the first
        webcam's ``stream_url`` (or ``urlStream``).
        """
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

    # ------------------------------------------------------------------
    # PrinterAdapter -- filament sensor
    # ------------------------------------------------------------------

    def get_filament_status(self) -> dict[str, Any] | None:
        """Query Klipper for filament runout sensor status via Moonraker.

        Klipper registers each sensor under its **full config section name** --
        ``"<type> <name>"``, e.g. ``"filament_switch_sensor runout"`` -- because
        both sensor modules are ``load_config_prefix``-only, so a bare
        ``[filament_switch_sensor]`` section cannot exist.  Querying the bare
        type therefore never matches on a real printer; the sensor names have to
        be discovered from ``GET /printer/objects/list`` first, then queried.

        Both Klipper sensor types are covered: ``filament_switch_sensor``
        (mechanical switch) and ``filament_motion_sensor`` (encoder).

        Returns ``None`` if no sensor is configured.
        """
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

            # Prefer the sensors actually armed; fall back to all of them so a
            # disabled sensor still reports its reading rather than a bare False.
            considered = [s for s in sensors if s["enabled"]] or sensors

            # Any armed sensor reporting no filament means runout: a spurious
            # "check your filament" is cheap, a missed runout is not.
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
    # PrinterAdapter -- bed mesh
    # ------------------------------------------------------------------

    def get_bed_mesh(self) -> dict[str, Any] | None:
        """Query Moonraker for the current bed mesh data.

        Uses ``GET /printer/objects/query?bed_mesh`` to retrieve the
        probed mesh point data from Klipper.
        """
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
    # Firmware updates
    # ------------------------------------------------------------------

    def get_firmware_status(self) -> FirmwareStatus | None:
        """Check Moonraker update manager for available updates.

        Calls ``GET /machine/update/status`` to get version info for all
        managed components (Klipper, Moonraker, system packages, web
        frontends, etc.).
        """
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

            # Determine if an update is available
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
        """Trigger an update via Moonraker's update manager.

        Calls ``POST /machine/update/upgrade``.  Moonraker will refuse
        if a print is in progress or another update is already running.

        Args:
            component: Specific component to update (e.g. ``"klipper"``,
                ``"moonraker"``, ``"system"``).  If ``None``, updates all.
        """
        # Safety: refuse if printer is actively printing
        try:
            state = self.get_state()
            if state.state == PrinterStatus.PRINTING:
                raise PrinterError("Cannot update firmware while printing. Wait for the current print to finish.")
        except PrinterError:
            raise
        except Exception as exc:
            logger.debug(
                "Failed to check printer state before firmware update: %s", exc
            )  # If we can't check state, let Moonraker decide

        payload = {}
        if component:
            payload["name"] = component

        try:
            self._post("/machine/update/upgrade", json=payload)
        except PrinterError:
            raise
        except Exception as exc:
            raise PrinterError(
                f"Firmware update failed: {exc}. "
                "Check Moonraker logs for details. Retry with `update_firmware()`.",
                cause=exc,
            ) from exc

        target = component or "all components"
        return FirmwareUpdateResult(
            success=True,
            message=f"Update started for {target}. The printer services may restart.",
            component=component,
        )

    def rollback_firmware(self, component: str) -> FirmwareUpdateResult:
        """Roll back a component to its previous version.

        Calls ``POST /machine/update/rollback`` with the component name.
        """
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
                f"Firmware rollback failed: {exc}. "
                "Check Moonraker logs for details. Retry with `rollback_firmware()`.",
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
