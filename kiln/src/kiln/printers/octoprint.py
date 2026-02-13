"""OctoPrint adapter for the Kiln printer abstraction layer.

Implements :class:`~kiln.printers.base.PrinterAdapter` by talking directly to
the `OctoPrint REST API <https://docs.octoprint.org/en/master/api/>`_ via
:mod:`requests`.  The adapter is self-contained and does **not** depend on the
sibling ``octoprint-cli`` package, though its design mirrors the same retry
and error-handling patterns.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import RequestException, Timeout

from kiln.printers.base import (
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

logger = logging.getLogger(__name__)

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


def _map_flags_to_status(flags: Dict[str, Any]) -> PrinterStatus:
    """Translate OctoPrint state-flag booleans to a :class:`PrinterStatus`.

    OctoPrint exposes a ``flags`` dictionary on ``GET /api/printer`` with keys
    such as ``printing``, ``paused``, ``cancelling``, ``error``, ``ready``,
    ``operational``, ``closedOrError``, etc.  This function evaluates them in
    priority order and returns the single most relevant status enum.
    """
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


def _flatten_files(entries: List[Dict[str, Any]], prefix: str = "") -> List[Dict[str, Any]]:
    """Recursively flatten OctoPrint's nested file/folder listing."""
    flat: List[Dict[str, Any]] = []
    for entry in entries:
        if entry.get("type") == "folder":
            children = entry.get("children", [])
            folder_path = f"{prefix}{entry.get('name', '')}/"
            flat.extend(_flatten_files(children, prefix=folder_path))
        else:
            flat.append(entry)
    return flat


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class OctoPrintAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by the OctoPrint REST API.

    Args:
        host: Base URL of the OctoPrint instance, e.g.
            ``"http://octopi.local"`` or ``"http://192.168.1.50:5000"``.
        api_key: OctoPrint API key for authentication.
        timeout: Per-request timeout in seconds.
        retries: Maximum number of attempts for transient failures
            (connection errors and HTTP 502/503/504).

    Raises:
        ValueError: If *host* or *api_key* are empty.

    Example::

        adapter = OctoPrintAdapter("http://octopi.local", "ABCDEF123456")
        state = adapter.get_state()
        print(state.state, state.tool_temp_actual)
    """

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

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return "octoprint"

    @property
    def capabilities(self) -> PrinterCapabilities:
        """Capabilities supported by the OctoPrint backend."""
        return PrinterCapabilities(
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
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        """Execute an HTTP request with exponential-backoff retry logic.

        Returns the :class:`requests.Response` on success (2xx).

        Raises:
            PrinterError: On non-retryable HTTP errors, connection failures,
                timeouts, or when all retry attempts are exhausted.
        """
        url = self._url(path)
        last_exc: Optional[Exception] = None

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
                    raise PrinterError(
                        f"OctoPrint returned HTTP {response.status_code} "
                        f"for {method} {path}: {response.text[:300]}",
                    )

                # Retryable HTTP status -- fall through to backoff.
                last_exc = PrinterError(
                    f"OctoPrint returned HTTP {response.status_code} "
                    f"for {method} {path} "
                    f"(attempt {attempt + 1}/{self._retries})"
                )

            except Timeout as exc:
                last_exc = PrinterError(
                    f"Request to {url} timed out after {self._timeout}s "
                    f"(attempt {attempt + 1}/{self._retries})",
                    cause=exc,
                )
            except ReqConnectionError as exc:
                last_exc = PrinterError(
                    f"Could not connect to OctoPrint at {self._host} "
                    f"(attempt {attempt + 1}/{self._retries})",
                    cause=exc,
                )
            except RequestException as exc:
                # Non-transient request errors -- raise immediately.
                raise PrinterError(
                    f"Request error for {method} {path}: {exc}",
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

    def _get_json(self, path: str, **kwargs: Any) -> Dict[str, Any]:
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
        json: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        """Shorthand for POST requests."""
        return self._request("POST", path, json=json, files=files, data=data)

    # ------------------------------------------------------------------
    # PrinterAdapter -- state queries
    # ------------------------------------------------------------------

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures.

        Calls ``GET /api/printer`` and maps the OctoPrint response to a
        :class:`PrinterState`.

        Raises:
            PrinterError: On communication or parsing errors.
        """
        try:
            payload = self._get_json("/api/printer")
        except PrinterError as exc:
            # If we cannot talk to OctoPrint at all, report OFFLINE.
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

        # Chamber (optional — requires OctoPrint plugin or firmware support).
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

    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active (or last) print job.

        Calls ``GET /api/job``.

        Raises:
            PrinterError: On communication or parsing errors.
        """
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

    def list_files(self) -> List[PrinterFile]:
        """Return a list of files stored on OctoPrint's local storage.

        Calls ``GET /api/files/local?recursive=true`` and flattens the
        response into a simple list of :class:`PrinterFile` objects.

        Raises:
            PrinterError: On communication or parsing errors.
        """
        payload = self._get_json(
            "/api/files/local",
            params={"recursive": "true"},
        )

        raw_files = payload.get("files", [])
        if not isinstance(raw_files, list):
            raw_files = []

        flat = _flatten_files(raw_files)

        results: List[PrinterFile] = []
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
        """Upload a local G-code file to OctoPrint.

        Calls ``POST /api/files/local`` with a multipart file upload.

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

    def start_print(self, file_name: str) -> PrintResult:
        """Begin printing a file that already exists on OctoPrint.

        Calls ``POST /api/files/local/{file_name}`` with the ``select``
        command and ``"print": true``.

        Args:
            file_name: Name (or path) of the file as known by OctoPrint.

        Raises:
            PrinterError: If the printer cannot start the job.
        """
        self._post(
            f"/api/files/local/{quote(file_name, safe='')}",
            json={"command": "select", "print": True},
        )
        return PrintResult(
            success=True,
            message=f"Started printing {file_name}.",
        )

    def cancel_print(self) -> PrintResult:
        """Cancel the currently running print job.

        Calls ``POST /api/job`` with ``{"command": "cancel"}``.

        Raises:
            PrinterError: If the cancellation fails.
        """
        self._post("/api/job", json={"command": "cancel"})
        return PrintResult(success=True, message="Print cancelled.")

    def emergency_stop(self) -> PrintResult:
        """Perform emergency stop via M112 firmware halt.

        Calls ``POST /api/printer/command`` with the M112 command which
        immediately kills heaters and stepper motors at the firmware level.
        """
        self._post("/api/printer/command", json={"commands": ["M112"]})
        return PrintResult(
            success=True,
            message="Emergency stop triggered (M112 sent).",
        )

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job.

        Calls ``POST /api/job`` with ``{"command": "pause", "action": "pause"}``.

        Raises:
            PrinterError: If the printer cannot pause.
        """
        self._post(
            "/api/job",
            json={"command": "pause", "action": "pause"},
        )
        return PrintResult(success=True, message="Print paused.")

    def resume_print(self) -> PrintResult:
        """Resume a previously paused print job.

        Calls ``POST /api/job`` with ``{"command": "pause", "action": "resume"}``.

        Raises:
            PrinterError: If the printer cannot resume.
        """
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
        layer_number: Optional[int] = None,
        fan_speed_pct: float = 100.0,
        flow_rate_pct: float = 100.0,
        prime_length_mm: float = 30.0,
        z_clearance_mm: float = 2.0,
    ) -> PrintResult:
        """Send Marlin M413 power-loss recovery positioning G-code.

        Generates and sends a G-code sequence that disables Marlin's built-in
        power-loss recovery, homes X/Y (never Z -- the nozzle would crash into
        the part), heats the bed and hotend, sets Z position without moving,
        raises the nozzle by *z_clearance_mm*, primes the nozzle, and
        configures fan speed and flow rate.

        After this method returns, the printer is positioned and ready.  The
        caller is responsible for starting the actual print file (e.g. a
        re-sliced file starting at the target layer) via :meth:`start_print`.

        :param z_height_mm: Z height to resume from, in millimetres (> 0).
        :param hotend_temp_c: Hotend target temperature in Celsius (> 0, <= 300).
        :param bed_temp_c: Bed target temperature in Celsius (>= 0, <= 130).
        :param file_name: Original file name (included in the result message
            for traceability; not sent to the printer).
        :param layer_number: Optional layer number for the result message.
        :param fan_speed_pct: Part-cooling fan speed as 0--100 % (default 100).
        :param flow_rate_pct: Flow rate multiplier as a percentage (default 100).
        :param prime_length_mm: Extrusion length to prime the nozzle (>= 0).
        :param z_clearance_mm: Distance to raise nozzle above the part (> 0, <= 10).

        :returns: :class:`PrintResult` indicating success.

        :raises PrinterError: If any parameter is out of range or if G-code
            delivery fails.
        """
        # -- parameter validation ------------------------------------------
        if z_height_mm <= 0:
            raise PrinterError(
                f"z_height_mm must be > 0, got {z_height_mm}."
            )
        if hotend_temp_c <= 0:
            raise PrinterError(
                f"Hotend temperature must be > 0°C, got {hotend_temp_c}°C."
            )
        self._validate_temp(hotend_temp_c, 300.0, "Hotend")
        self._validate_temp(bed_temp_c, 130.0, "Bed")
        if prime_length_mm < 0:
            raise PrinterError(
                f"prime_length_mm must be >= 0, got {prime_length_mm}."
            )
        if z_clearance_mm <= 0 or z_clearance_mm > 10:
            raise PrinterError(
                f"z_clearance_mm must be > 0 and <= 10, got {z_clearance_mm}."
            )

        # -- build G-code sequence -----------------------------------------
        fan_pwm = int(fan_speed_pct * 2.55)
        commands = [
            "M413 S0",                          # Disable Marlin power-loss recovery
            "G28 X Y",                           # Home X/Y only (NEVER Z)
            f"M140 S{bed_temp_c}",               # Start heating bed (non-blocking)
            f"M104 S{hotend_temp_c}",            # Start heating hotend (non-blocking)
            f"M190 S{bed_temp_c}",               # Wait for bed temp
            f"M109 S{hotend_temp_c}",            # Wait for hotend temp
            "G92 E0",                            # Reset extruder position
            f"G92 Z{z_height_mm}",               # Set Z position without moving
            "G91",                               # Relative positioning
            f"G1 Z{z_clearance_mm} F300",        # Raise nozzle above part
            "G90",                               # Absolute positioning
            f"G1 E{prime_length_mm} F200",       # Prime nozzle
            "G92 E0",                            # Reset extruder again
            f"M106 S{fan_pwm}",                  # Set fan speed (0-255)
            f"M221 S{int(flow_rate_pct)}",       # Set flow rate multiplier
        ]

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

    def set_tool_temp(self, target: float) -> bool:
        """Set the hotend (tool0) target temperature in degrees Celsius.

        Calls ``POST /api/printer/tool``.

        Args:
            target: Target temperature.  Pass ``0`` to turn the heater off.

        Returns:
            ``True`` if the command was accepted.

        Raises:
            PrinterError: If the command fails.
        """
        self._validate_temp(target, 300.0, "Hotend")
        self._post(
            "/api/printer/tool",
            json={"command": "target", "targets": {"tool0": int(target)}},
        )
        return True

    def set_bed_temp(self, target: float) -> bool:
        """Set the heated-bed target temperature in degrees Celsius.

        Calls ``POST /api/printer/bed``.

        Args:
            target: Target temperature.  Pass ``0`` to turn the heater off.

        Returns:
            ``True`` if the command was accepted.

        Raises:
            PrinterError: If the command fails.
        """
        self._validate_temp(target, 130.0, "Bed")
        self._post(
            "/api/printer/bed",
            json={"command": "target", "target": int(target)},
        )
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def send_gcode(self, commands: List[str]) -> bool:
        """Send G-code commands to OctoPrint.

        Calls ``POST /api/printer/command`` with a JSON body containing
        the list of commands.

        Args:
            commands: List of G-code command strings.

        Returns:
            ``True`` if the commands were accepted.

        Raises:
            PrinterError: If sending fails.
        """
        self._post(
            "/api/printer/command",
            json={"commands": commands},
        )
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- file deletion
    # ------------------------------------------------------------------

    def delete_file(self, file_path: str) -> bool:
        """Delete a G-code file from OctoPrint's local storage.

        Calls ``DELETE /api/files/local/{file_path}``.

        Args:
            file_path: Path of the file as returned by ``list_files()``.

        Returns:
            ``True`` if the file was deleted.

        Raises:
            PrinterError: If deletion fails.
        """
        self._request("DELETE", f"/api/files/local/{quote(file_path, safe='')}")
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- webcam snapshot
    # ------------------------------------------------------------------

    def get_snapshot(self) -> Optional[bytes]:
        """Capture a webcam snapshot from OctoPrint.

        Attempts ``GET /webcam/?action=snapshot`` which is the standard
        mjpg-streamer endpoint exposed by OctoPrint.

        Returns:
            Raw JPEG image bytes, or ``None`` if the webcam is not available.
        """
        try:
            response = self._session.get(
                self._url("/webcam/?action=snapshot"),
                timeout=10,
            )
            if response.ok and response.content:
                return response.content
        except Exception:
            logger.debug("Webcam snapshot failed", exc_info=True)
        return None

    # ------------------------------------------------------------------
    # PrinterAdapter -- webcam streaming
    # ------------------------------------------------------------------

    def get_stream_url(self) -> Optional[str]:
        """Return the MJPEG stream URL for OctoPrint's webcam.

        The standard mjpg-streamer endpoint is
        ``/webcam/?action=stream``.
        """
        return f"{self._host}/webcam/?action=stream"

    # -- filament sensor (optional) ----------------------------------------

    def get_filament_status(self) -> Optional[Dict[str, Any]]:
        """Query OctoPrint for filament sensor status via the Filament Manager plugin.

        Uses ``GET /api/plugin/filamentmanager`` to check whether filament
        is loaded and detected.  Returns ``None`` if the plugin is not
        installed.
        """
        try:
            payload = self._get_json("/api/plugin/filamentmanager")
            # The plugin returns spool/selection info; if we get a response
            # at all, the plugin is installed.
            selections = payload.get("selections", [])
            detected = len(selections) > 0 and any(
                s.get("spool") is not None for s in selections
            )
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

    def get_bed_mesh(self) -> Optional[Dict[str, Any]]:
        """Query OctoPrint for bed mesh data via the Bed Level Visualizer plugin.

        Uses ``GET /api/plugin/bedlevelvisualizer`` to retrieve probe data.
        Returns ``None`` if the plugin is not installed or no mesh is available.
        """
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
    # Firmware updates
    # ------------------------------------------------------------------

    def get_firmware_status(self) -> Optional[FirmwareStatus]:
        """Check OctoPrint's Software Update plugin for available updates.

        Calls ``GET /plugin/softwareupdate/check`` to get version info
        for OctoPrint and its installed plugins.
        """
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
            current = info.get("information", {}).get(
                "local", {}).get("value", "")
            remote = info.get("information", {}).get(
                "remote", {}).get("value", "")
            has_update = bool(info.get("updateAvailable", False))

            if has_update:
                updates_available += 1

            components.append(FirmwareComponent(
                name=display,
                current_version=str(current),
                remote_version=str(remote) if remote else None,
                update_available=has_update,
                component_type="octoprint_plugin",
            ))

        return FirmwareStatus(
            busy=busy,
            components=components,
            updates_available=updates_available,
        )

    def update_firmware(
        self,
        component: Optional[str] = None,
    ) -> FirmwareUpdateResult:
        """Trigger an update via OctoPrint's Software Update plugin.

        Calls ``POST /plugin/softwareupdate/update``.  OctoPrint will
        refuse if a print is in progress.

        Args:
            component: Specific plugin/component to update.  If ``None``,
                updates all components with available updates.
        """
        # Safety: refuse if printer is actively printing
        try:
            state = self.get_state()
            if state.state == PrinterStatus.PRINTING:
                raise PrinterError(
                    "Cannot update firmware while printing. "
                    "Wait for the current print to finish."
                )
        except PrinterError:
            raise
        except Exception:
            pass

        # Build the update targets
        if component:
            targets = [component]
        else:
            # Get all components with updates available
            status = self.get_firmware_status()
            if status is None:
                raise PrinterError(
                    "Software Update plugin not available on this OctoPrint instance."
                )
            targets = [
                c.name for c in status.components if c.update_available
            ]
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
                f"Firmware update failed: {exc}", cause=exc,
            ) from exc

        target_str = component or "all components"
        return FirmwareUpdateResult(
            success=True,
            message=f"Update started for {target_str}. "
                    "OctoPrint may restart.",
            component=component,
        )

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<OctoPrintAdapter host={self._host!r}>"
