"""Prusa Link adapter for the Kiln printer abstraction layer.

Implements :class:`~kiln.printers.base.PrinterAdapter` by talking to the
`Prusa Link HTTP API <https://github.com/prusa3d/Prusa-Link-Web>`_
via :mod:`requests`.  Prusa Link is the local API running on Prusa
printers (MK4, XL, Mini+), providing REST endpoints for printer control.

Limitations compared to OctoPrint/Moonraker:
- No direct temperature control endpoints (uses G-code workaround
  only if the printer firmware supports it via file execution)
- No raw G-code endpoint (Prusa Link does not expose one)
- Job pause/resume/cancel require the active job ID

The adapter uses ``X-Api-Key`` header authentication by default.
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

_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({502, 503, 504})
_FILE_ROOTS: tuple[str, ...] = ("usb", "local")
_FILE_ROOT_FALLBACK_HTTP_CODES: tuple[int, ...] = (403, 404)

# Prusa Link printer states → PrinterStatus
_STATE_MAP: Dict[str, PrinterStatus] = {
    "IDLE": PrinterStatus.IDLE,
    "BUSY": PrinterStatus.BUSY,
    "PRINTING": PrinterStatus.PRINTING,
    "PAUSED": PrinterStatus.PAUSED,
    "FINISHED": PrinterStatus.IDLE,
    "STOPPED": PrinterStatus.IDLE,
    "ERROR": PrinterStatus.ERROR,
    "ATTENTION": PrinterStatus.ERROR,
    "READY": PrinterStatus.IDLE,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_get(data: Any, *keys: str, default: Any = None) -> Any:
    """Walk nested dicts safely, returning *default* on any miss."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
    return current


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class PrusaConnectAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by the Prusa Link HTTP API.

    Args:
        host: Base URL of the Prusa Link instance, e.g.
            ``"http://192.168.1.100"`` or ``"http://prusa.local"``.
        api_key: API key shown in printer settings under
            Settings > Network > PrusaLink.
        timeout: Per-request timeout in seconds.
        retries: Maximum number of attempts for transient failures.

    Raises:
        ValueError: If *host* is empty.
    """

    def __init__(
        self,
        host: str,
        api_key: Optional[str] = None,
        timeout: int = 30,
        retries: int = 3,
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

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return "prusaconnect"

    @property
    def capabilities(self) -> PrinterCapabilities:
        """Capabilities supported by the Prusa Link backend.

        Note: Temperature control and raw G-code are not natively
        supported by Prusa Link's API.
        """
        return PrinterCapabilities(
            can_upload=True,
            can_set_temp=False,
            can_send_gcode=False,
            can_pause=True,
            supported_extensions=(".gcode", ".gco", ".g", ".bgcode"),
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
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Any] = None,
    ) -> requests.Response:
        """Execute an HTTP request with exponential-backoff retry logic."""
        url = self._url(path)
        last_exc: Optional[Exception] = None

        for attempt in range(self._retries):
            try:
                response = self._session.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=headers,
                    data=data,
                    timeout=self._timeout,
                )

                if response.ok:
                    return response

                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    if response.status_code == 401:
                        raise PrinterError(
                            f"Authentication failed (HTTP 401) for Prusa Link at {self._host}. "
                            f"Your API key is invalid or missing. Find the correct key in "
                            f"Settings > Network > PrusaLink on your printer's LCD, then update "
                            f"with: kiln auth --name <name> --host {self._host} "
                            f"--type prusaconnect --api-key <YOUR_KEY>",
                        )
                    if response.status_code == 403:
                        endpoint_hint = (
                            " This endpoint is under /api/v1/files; if status/cancel work but "
                            "files/print fail, verify the storage root/path and use the API "
                            "filename (often 8.3) shown by 'kiln files'."
                            if path.startswith("/api/v1/files/")
                            else ""
                        )
                        raise PrinterError(
                            f"Access forbidden (HTTP 403) for Prusa Link at {self._host} "
                            f"on {method} {path}. Your API key may lack required permissions, "
                            f"or this firmware may reject the requested operation/path. "
                            f"Check the key in Settings > Network > PrusaLink on your printer's "
                            f"LCD.{endpoint_hint}",
                        )
                    if response.status_code == 404:
                        raise PrinterError(
                            f"Endpoint not found (HTTP 404) for {method} {path} on {self._host}. "
                            f"This may indicate an unsupported Prusa Link firmware version. "
                            f"Ensure your printer firmware is up to date.",
                        )
                    if response.status_code == 409:
                        raise PrinterError(
                            f"Conflict (HTTP 409) for {method} {path} — the printer may be busy "
                            f"with another operation. Wait a moment and try again.",
                        )
                    raise PrinterError(
                        f"Prusa Link returned HTTP {response.status_code} "
                        f"for {method} {path}: {response.text[:300]}",
                    )

                last_exc = PrinterError(
                    f"Prusa Link returned HTTP {response.status_code} "
                    f"for {method} {path} "
                    f"(attempt {attempt + 1}/{self._retries})"
                )

            except Timeout as exc:
                last_exc = PrinterError(
                    f"Request to Prusa Link at {self._host} timed out after {self._timeout}s "
                    f"(attempt {attempt + 1}/{self._retries}). "
                    f"The printer may be busy, overloaded, or on a slow network. "
                    f"Try: (1) check the printer's LCD for errors, "
                    f"(2) restart the printer, (3) verify the IP is correct.",
                    cause=exc,
                )
            except ReqConnectionError as exc:
                last_exc = PrinterError(
                    f"Could not connect to Prusa Link at {self._host} "
                    f"(attempt {attempt + 1}/{self._retries}). "
                    f"Check: (1) printer is powered on and connected to LAN (Ethernet or Wi-Fi), "
                    f"(2) IP address is correct (find it on the printer's LCD under "
                    f"Settings > Network), (3) Prusa Link is enabled.",
                    cause=exc,
                )
            except RequestException as exc:
                raise PrinterError(
                    f"Request error for {method} {path}: {exc}",
                    cause=exc,
                ) from exc

            if attempt < self._retries - 1:
                backoff = 2**attempt
                logger.debug(
                    "Retrying %s %s in %ds (attempt %d/%d)",
                    method, path, backoff, attempt + 1, self._retries,
                )
                time.sleep(backoff)

        assert last_exc is not None
        raise last_exc

    def _get_json(self, path: str, **kwargs: Any) -> Dict[str, Any]:
        """GET *path* and return the parsed JSON body."""
        response = self._request("GET", path, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise PrinterError(
                f"Invalid JSON in response from GET {path}",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_active_job_id(self) -> Optional[int]:
        """Return the active job ID, or None if no job is running."""
        try:
            data = self._get_json("/api/v1/status")
            return _safe_get(data, "job", "id")
        except PrinterError:
            return None

    @staticmethod
    def _is_http_error(exc: PrinterError, status_code: int) -> bool:
        return f"HTTP {status_code}" in str(exc)

    @classmethod
    def _is_storage_fallback_error(cls, exc: PrinterError) -> bool:
        return any(
            cls._is_http_error(exc, code)
            for code in _FILE_ROOT_FALLBACK_HTTP_CODES
        )

    def _iter_file_roots(self, preferred: Optional[str] = None) -> List[str]:
        roots = list(_FILE_ROOTS)
        if preferred and preferred in roots:
            return [preferred, *[r for r in roots if r != preferred]]
        return roots

    def _split_storage_root(self, file_path: str) -> tuple[Optional[str], str]:
        clean = file_path.strip().lstrip("/")
        for root in _FILE_ROOTS:
            prefix = f"{root}/"
            if clean.lower().startswith(prefix):
                return root, clean[len(prefix):]
        return None, clean

    def _resolve_print_path(self, requested: str) -> str:
        """Resolve a user-provided file identifier to a Prusa API path.

        Prusa Link can expose user-facing long display names (``display_name``)
        while some firmware paths are 8.3 short names (``name``). This resolver
        maps display names to API-safe paths when possible.
        """
        _, normalized = self._split_storage_root(requested)
        if not normalized:
            raise PrinterError("File name must not be empty.")

        try:
            files = self.list_files()
        except PrinterError as exc:
            logger.debug(
                "Could not resolve file path via list_files(); using raw input %r: %s",
                requested,
                exc,
            )
            return normalized

        lookup = normalized.lower()
        for candidate in files:
            if candidate.path.lower() == lookup or candidate.name.lower() == lookup:
                return candidate.path

        basename = lookup.rsplit("/", 1)[-1]
        basename_matches = [
            candidate
            for candidate in files
            if candidate.name.lower() == basename
            or candidate.path.rsplit("/", 1)[-1].lower() == basename
        ]
        if len(basename_matches) == 1:
            return basename_matches[0].path
        if len(basename_matches) > 1:
            options = ", ".join(sorted({c.path for c in basename_matches})[:5])
            raise PrinterError(
                f"Multiple files match '{requested}'. Use one of: {options}",
            )

        return normalized

    # ------------------------------------------------------------------
    # PrinterAdapter -- state queries
    # ------------------------------------------------------------------

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures.

        Calls ``GET /api/v1/status`` which returns printer state,
        temperatures, and job progress in a single response.
        """
        try:
            data = self._get_json("/api/v1/status")
        except PrinterError as exc:
            if exc.cause and isinstance(exc.cause, (ReqConnectionError, Timeout)):
                return PrinterState(
                    connected=False,
                    state=PrinterStatus.OFFLINE,
                )
            raise

        printer = _safe_get(data, "printer", default={})
        state_str = printer.get("state", "IDLE") if isinstance(printer, dict) else "IDLE"
        mapped_status = _STATE_MAP.get(state_str, PrinterStatus.UNKNOWN)

        tool_actual = printer.get("temp_nozzle") if isinstance(printer, dict) else None
        tool_target = printer.get("target_nozzle") if isinstance(printer, dict) else None
        bed_actual = printer.get("temp_bed") if isinstance(printer, dict) else None
        bed_target = printer.get("target_bed") if isinstance(printer, dict) else None
        chamber_actual = printer.get("temp_chamber") if isinstance(printer, dict) else None
        chamber_target = printer.get("target_chamber") if isinstance(printer, dict) else None

        return PrinterState(
            connected=True,
            state=mapped_status,
            tool_temp_actual=tool_actual,
            tool_temp_target=tool_target,
            bed_temp_actual=bed_actual,
            bed_temp_target=bed_target,
            chamber_temp_actual=chamber_actual,
            chamber_temp_target=chamber_target,
        )

    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active print job.

        Calls ``GET /api/v1/status`` and extracts job info.
        """
        try:
            data = self._get_json("/api/v1/status")
        except PrinterError:
            return JobProgress()

        job = _safe_get(data, "job", default={})
        if not isinstance(job, dict):
            return JobProgress()

        progress = job.get("progress")
        completion: Optional[float] = None
        if progress is not None:
            completion = round(float(progress), 2)

        time_printing = job.get("time_printing")
        time_remaining = job.get("time_remaining")

        print_time_seconds: Optional[int] = None
        print_time_left_seconds: Optional[int] = None

        if time_printing is not None:
            print_time_seconds = int(time_printing)
        if time_remaining is not None:
            print_time_left_seconds = int(time_remaining)

        return JobProgress(
            file_name=None,  # Prusa Link doesn't include filename in status
            completion=completion,
            print_time_seconds=print_time_seconds,
            print_time_left_seconds=print_time_left_seconds,
        )

    def list_files(self) -> List[PrinterFile]:
        """Return a list of G-code files across supported Prusa storage roots.

        Tries ``/api/v1/files/usb`` first, then falls back to ``/api/v1/files/local``.
        """
        results: List[PrinterFile] = []
        successful_roots = 0
        fallback_errors: List[tuple[str, PrinterError]] = []

        for root in _FILE_ROOTS:
            try:
                data = self._get_json(f"/api/v1/files/{root}")
            except PrinterError as exc:
                if self._is_storage_fallback_error(exc):
                    fallback_errors.append((root, exc))
                    logger.debug(
                        "Skipping unavailable Prusa storage root '%s': %s",
                        root,
                        exc,
                    )
                    continue
                raise

            successful_roots += 1
            children = data.get("children", [])
            if isinstance(children, list):
                self._collect_files(children, results, prefix="")

        if successful_roots == 0 and fallback_errors:
            roots = ", ".join(root for root, _ in fallback_errors)
            detail = "; ".join(str(exc) for _, exc in fallback_errors)
            raise PrinterError(
                f"Unable to list files from Prusa Link storage roots ({roots}). {detail}",
            )

        deduped: List[PrinterFile] = []
        seen_paths: set[str] = set()
        for entry in results:
            key = entry.path.lower()
            if key in seen_paths:
                continue
            seen_paths.add(key)
            deduped.append(entry)

        return deduped

    def _collect_files(
        self,
        entries: List[Any],
        results: List[PrinterFile],
        prefix: str,
    ) -> None:
        """Recursively collect files from a directory listing."""
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            display_name = str(entry.get("display_name") or entry.get("name") or "")
            api_name = str(entry.get("name") or display_name)
            entry_type = entry.get("type", "")
            raw_path = entry.get("path")
            if isinstance(raw_path, str) and raw_path.strip():
                api_path = raw_path.strip("/")
            else:
                api_path = f"{prefix}{api_name}" if prefix else api_name

            if entry_type == "FOLDER":
                children = entry.get("children", [])
                if isinstance(children, list):
                    folder_prefix = f"{api_path}/" if api_path else prefix
                    self._collect_files(children, results, prefix=folder_prefix)
            else:
                if not api_path:
                    continue
                results.append(
                    PrinterFile(
                        name=display_name or api_name,
                        path=api_path,
                        size_bytes=entry.get("size"),
                        date=entry.get("m_timestamp"),
                    )
                )

    # ------------------------------------------------------------------
    # PrinterAdapter -- file management
    # ------------------------------------------------------------------

    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a local G-code file to the printer via Prusa Link.

        Attempts ``PUT /api/v1/files/usb/<filename>`` first, then ``local``.

        Args:
            file_path: Absolute or relative path to the local file.
        """
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Local file not found: {abs_path}")

        filename = os.path.basename(abs_path)
        file_size = os.path.getsize(abs_path)
        encoded_name = quote(filename, safe="")
        upload_headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(file_size),
            "Print-After-Upload": "?0",
            "Overwrite": "?1",
        }

        last_fallback_error: Optional[PrinterError] = None

        for root in _FILE_ROOTS:
            try:
                with open(abs_path, "rb") as fh:
                    self._request(
                        "PUT",
                        f"/api/v1/files/{root}/{encoded_name}",
                        data=fh,
                        headers=upload_headers,
                    )
                break
            except PermissionError as exc:
                raise PrinterError(
                    f"Permission denied reading file: {abs_path}",
                    cause=exc,
                ) from exc
            except PrinterError as exc:
                if self._is_storage_fallback_error(exc):
                    last_fallback_error = exc
                    continue
                raise
        else:
            root_list = ", ".join(_FILE_ROOTS)
            detail = f" Last error: {last_fallback_error}" if last_fallback_error else ""
            raise PrinterError(
                f"Failed to upload {filename} to Prusa storage roots ({root_list}).{detail}",
            )

        return UploadResult(
            success=True,
            file_name=filename,
            message=f"Uploaded {filename} to Prusa Link.",
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- print control
    # ------------------------------------------------------------------

    def start_print(self, file_name: str) -> PrintResult:
        """Begin printing a file on the printer.

        Resolves display names to API-safe file paths, then attempts
        ``POST /api/v1/files/usb/<file_path>`` first, falling back to ``local``.
        """
        preferred_root, _ = self._split_storage_root(file_name)
        resolved_path = self._resolve_print_path(file_name)
        encoded = quote(resolved_path, safe="/")

        roots = self._iter_file_roots(preferred=preferred_root)
        last_fallback_error: Optional[PrinterError] = None
        for root in roots:
            try:
                self._request("POST", f"/api/v1/files/{root}/{encoded}")
                break
            except PrinterError as exc:
                if self._is_storage_fallback_error(exc):
                    last_fallback_error = exc
                    continue
                raise
        else:
            root_list = ", ".join(roots)
            detail = f" Last error: {last_fallback_error}" if last_fallback_error else ""
            raise PrinterError(
                f"Failed to start print for '{file_name}' from Prusa storage roots "
                f"({root_list}). The file may require its API path/8.3 name.{detail}",
            )

        return PrintResult(
            success=True,
            message=f"Started printing {resolved_path}.",
        )

    def cancel_print(self) -> PrintResult:
        """Cancel the currently running print job.

        Calls ``DELETE /api/v1/job/<id>``.
        """
        job_id = self._get_active_job_id()
        if job_id is None:
            raise PrinterError("No active job to cancel.")

        self._request("DELETE", f"/api/v1/job/{job_id}")
        return PrintResult(success=True, message="Print cancelled.")

    def emergency_stop(self) -> PrintResult:
        """Perform emergency stop by cancelling the active job.

        Prusa Link does not expose a raw G-code or M112 endpoint.
        The closest available action is a job cancellation.
        """
        try:
            return self.cancel_print()
        except PrinterError:
            raise PrinterError(
                "Emergency stop failed — Prusa Link does not support "
                "raw G-code commands.  Power off the printer manually."
            )

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job.

        Calls ``PUT /api/v1/job/<id>/pause``.
        """
        job_id = self._get_active_job_id()
        if job_id is None:
            raise PrinterError("No active job to pause.")

        self._request("PUT", f"/api/v1/job/{job_id}/pause")
        return PrintResult(success=True, message="Print paused.")

    def resume_print(self) -> PrintResult:
        """Resume a previously paused print job.

        Calls ``PUT /api/v1/job/<id>/resume``.
        """
        job_id = self._get_active_job_id()
        if job_id is None:
            raise PrinterError("No active job to resume.")

        self._request("PUT", f"/api/v1/job/{job_id}/resume")
        return PrintResult(success=True, message="Print resumed.")

    # ------------------------------------------------------------------
    # PrinterAdapter -- temperature control
    # ------------------------------------------------------------------

    def set_tool_temp(self, target: float) -> bool:
        """Not natively supported by Prusa Link.

        Prusa Link does not expose a temperature control endpoint.
        """
        raise PrinterError(
            "Prusa Link does not support direct temperature control. "
            "Temperature is managed through G-code in print files."
        )

    def set_bed_temp(self, target: float) -> bool:
        """Not natively supported by Prusa Link.

        Prusa Link does not expose a temperature control endpoint.
        """
        raise PrinterError(
            "Prusa Link does not support direct temperature control. "
            "Temperature is managed through G-code in print files."
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def send_gcode(self, commands: List[str]) -> bool:
        """Not supported by Prusa Link.

        Prusa Link does not expose a raw G-code endpoint.
        """
        raise PrinterError(
            "Prusa Link does not support sending raw G-code commands. "
            "See: https://github.com/prusa3d/Prusa-Link/issues/832"
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- file deletion
    # ------------------------------------------------------------------

    def delete_file(self, file_path: str) -> bool:
        """Delete a G-code file from the printer's local storage.

        Attempts deletion on ``usb`` first (or requested root), then ``local``.
        """
        preferred_root, normalized_path = self._split_storage_root(file_path)
        if not normalized_path:
            raise PrinterError("File path must not be empty.")

        encoded = quote(normalized_path, safe="/")
        roots = self._iter_file_roots(preferred=preferred_root)
        last_fallback_error: Optional[PrinterError] = None
        for root in roots:
            try:
                self._request("DELETE", f"/api/v1/files/{root}/{encoded}")
                return True
            except PrinterError as exc:
                if self._is_storage_fallback_error(exc):
                    last_fallback_error = exc
                    continue
                raise

        root_list = ", ".join(roots)
        detail = f" Last error: {last_fallback_error}" if last_fallback_error else ""
        raise PrinterError(
            f"Failed to delete '{file_path}' from Prusa storage roots ({root_list}).{detail}",
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- webcam snapshot
    # ------------------------------------------------------------------

    def get_snapshot(self) -> Optional[bytes]:
        """Capture a webcam snapshot from Prusa Link.

        Calls ``GET /api/v1/cameras/snap`` for the default camera.
        """
        try:
            response = self._request("GET", "/api/v1/cameras/snap")
            if response.ok and response.content:
                return response.content
        except Exception:
            logger.debug("Webcam snapshot failed", exc_info=True)
        return None

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<PrusaConnectAdapter host={self._host!r}>"
