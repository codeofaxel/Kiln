"""Klipper/Moonraker adapter for the Kiln printer abstraction layer.

Implements :class:`~kiln.printers.base.PrinterAdapter` by talking to the
`Moonraker HTTP API <https://moonraker.readthedocs.io/en/latest/web_api/>`_
via :mod:`requests`.  Moonraker is the API server that sits in front of
Klipper, providing a REST+WebSocket interface for printer control.

The adapter mirrors the retry and error-handling patterns established by
:class:`~kiln.printers.octoprint.OctoPrintAdapter`.
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

# Mapping from Moonraker's ``klippy_state`` / ``state`` strings to the
# canonical :class:`PrinterStatus` enum.  Moonraker reports the Klipper
# state via ``GET /printer/info`` in the ``state`` field.
_STATE_MAP: Dict[str, PrinterStatus] = {
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


def _map_moonraker_state(state_string: str, print_state: Optional[str] = None) -> PrinterStatus:
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

    def __init__(
        self,
        host: str,
        api_key: Optional[str] = None,
        timeout: int = 30,
        retries: int = 3,
        verify_ssl: bool = True,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")

        self._host: str = host.rstrip("/")
        self._api_key: Optional[str] = api_key or None
        self._timeout: int = timeout
        self._retries: int = max(retries, 1)

        self._session: requests.Session = requests.Session()
        if self._api_key:
            self._session.headers.update({"X-Api-Key": self._api_key})
        self._session.verify = verify_ssl
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return "moonraker"

    @property
    def capabilities(self) -> PrinterCapabilities:
        """Capabilities supported by the Moonraker/Klipper backend."""
        return PrinterCapabilities(
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
            can_pause=True,
            can_stream=True,
            can_probe_bed=True,
            can_update_firmware=True,
            can_snapshot=True,
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
                        f"Moonraker returned HTTP {response.status_code} "
                        f"for {method} {path}: {response.text[:300]}",
                    )

                # Retryable HTTP status -- fall through to backoff.
                last_exc = PrinterError(
                    f"Moonraker returned HTTP {response.status_code} "
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
                    f"Could not connect to Moonraker at {self._host} "
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
        params: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
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

        Issues two Moonraker requests:
        * ``GET /printer/info`` -- klippy state and connection info
        * ``GET /printer/objects/query?heater_bed&extruder&print_stats`` --
          temperatures and print state

        Returns an OFFLINE state when Moonraker is unreachable rather than
        raising, so callers always get a usable :class:`PrinterState`.

        Raises:
            PrinterError: On unexpected (non-connection) errors.
        """
        # -- klippy state --------------------------------------------------
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
        total_duration = print_stats.get("total_duration") if isinstance(print_stats, dict) else None

        # virtual_sdcard
        vsd = _safe_get(status, "virtual_sdcard", default={})
        progress = vsd.get("progress") if isinstance(vsd, dict) else None

        # Moonraker reports progress as 0.0--1.0; convert to 0.0--100.0 to
        # match the PrinterAdapter contract.
        completion: Optional[float] = None
        if progress is not None:
            completion = round(float(progress) * 100.0, 2)

        # Estimate time left based on progress and elapsed time.
        print_time_seconds: Optional[int] = None
        print_time_left_seconds: Optional[int] = None

        if print_duration is not None:
            print_time_seconds = int(print_duration)

        if (
            print_time_seconds is not None
            and completion is not None
            and completion > 0
        ):
            # total_estimated = elapsed / (completion / 100)
            total_estimated = print_time_seconds / (completion / 100.0)
            print_time_left_seconds = max(0, int(total_estimated - print_time_seconds))

        return JobProgress(
            file_name=file_name if file_name else None,
            completion=completion,
            print_time_seconds=print_time_seconds,
            print_time_left_seconds=print_time_left_seconds,
        )

    def list_files(self) -> List[PrinterFile]:
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

        results: List[PrinterFile] = []
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

    def start_print(self, file_name: str) -> PrintResult:
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

    def resume_print(self) -> PrintResult:
        """Resume a previously paused print job.

        Calls ``POST /printer/print/resume``.

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

    def send_gcode(self, commands: List[str]) -> bool:
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

    def get_snapshot(self) -> Optional[bytes]:
        """Capture a webcam snapshot from Moonraker.

        Discovers the webcam snapshot URL via
        ``GET /server/webcams/list`` and then fetches the image.

        Returns:
            Raw JPEG/PNG image bytes, or ``None`` if no webcam is configured.
        """
        try:
            payload = self._get_json("/server/webcams/list")
            webcams = _safe_get(payload, "result", "webcams", default=[])
            if not isinstance(webcams, list) or not webcams:
                return None

            # Use the first webcam's snapshot_url
            cam = webcams[0]
            snapshot_url = cam.get("snapshot_url") or cam.get("urlSnapshot")
            if not snapshot_url:
                # Fall back to stream_url if available
                stream_url = cam.get("stream_url") or cam.get("urlStream")
                if stream_url:
                    snapshot_url = stream_url.replace("/stream", "/?action=snapshot")
                else:
                    return None

            # If the URL is relative, prepend the host
            if snapshot_url.startswith("/"):
                snapshot_url = f"{self._host}{snapshot_url}"

            response = self._session.get(snapshot_url, timeout=10)
            if response.ok and response.content:
                return response.content
        except Exception:
            logger.debug("Webcam snapshot failed", exc_info=True)
        return None

    # ------------------------------------------------------------------
    # PrinterAdapter -- webcam streaming
    # ------------------------------------------------------------------

    def get_stream_url(self) -> Optional[str]:
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
    # PrinterAdapter -- bed mesh
    # ------------------------------------------------------------------

    def get_bed_mesh(self) -> Optional[Dict[str, Any]]:
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

    def get_firmware_status(self) -> Optional[FirmwareStatus]:
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
            elif current and remote and current != remote:
                has_update = True
            elif int(info.get("commits_behind_count", 0)) > 0:
                has_update = True

            if has_update:
                updates_available += 1

            components.append(FirmwareComponent(
                name=comp_name,
                current_version=str(current),
                remote_version=str(remote) if remote else None,
                update_available=has_update,
                rollback_version=str(rollback) if rollback else None,
                component_type=comp_type,
                channel=channel,
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
                raise PrinterError(
                    "Cannot update firmware while printing. "
                    "Wait for the current print to finish."
                )
        except PrinterError:
            raise
        except Exception:
            pass  # If we can't check state, let Moonraker decide

        payload = {}
        if component:
            payload["name"] = component

        try:
            self._post("/machine/update/upgrade", json=payload)
        except PrinterError:
            raise
        except Exception as exc:
            raise PrinterError(
                f"Firmware update failed: {exc}", cause=exc,
            ) from exc

        target = component or "all components"
        return FirmwareUpdateResult(
            success=True,
            message=f"Update started for {target}. "
                    "The printer services may restart.",
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
                f"Firmware rollback failed: {exc}", cause=exc,
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
