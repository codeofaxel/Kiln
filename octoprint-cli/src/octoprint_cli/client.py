"""OctoPrint REST API client with retry logic and standardized responses."""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

import requests
from requests.exceptions import ConnectionError, RequestException, Timeout


class OctoPrintClient:
    """Client for interacting with the OctoPrint REST API.

    Wraps all OctoPrint REST API calls with automatic retry logic using
    exponential backoff for transient failures. All methods return a
    standardized response dictionary.

    Args:
        host: Base URL of the OctoPrint server (e.g. ``http://octopi.local``).
        api_key: OctoPrint API key used for authentication.
        timeout: Request timeout in seconds. Defaults to 30.
        retries: Maximum number of retry attempts for transient failures.
            Defaults to 3.

    Example::

        client = OctoPrintClient("http://octopi.local", "ABCDEF123456")
        result = client.get_printer_state()
        if result["success"]:
            print(result["data"]["state"])
    """

    # HTTP status codes considered transient and eligible for retry.
    _RETRYABLE_STATUS_CODES = {502, 503, 504}

    def __init__(
        self,
        host: str,
        api_key: str,
        timeout: int = 30,
        retries: int = 3,
    ) -> None:
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self._session = requests.Session()
        self._session.headers.update({"X-Api-Key": self.api_key})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        """Build a full URL from a relative API path."""
        return f"{self.host}{path}"

    @staticmethod
    def _success(data: Any = None) -> dict[str, Any]:
        """Return a standardized success response.

        Args:
            data: The parsed JSON payload from the server, or ``None`` when
                the endpoint returns no body (e.g. 204).
        """
        return {"success": True, "data": data, "error": None}

    @staticmethod
    def _error(
        code: str,
        message: str,
        http_status: int | None = None,
    ) -> dict[str, Any]:
        """Return a standardized error response.

        Args:
            code: Machine-readable error code (e.g. ``"AUTH_ERROR"``).
            message: Human-readable description of the error.
            http_status: HTTP status code, if the error originated from an
                HTTP response.
        """
        return {
            "success": False,
            "data": None,
            "error": {
                "code": code,
                "message": message,
                "http_status": http_status,
            },
        }

    def _classify_http_error(self, response: requests.Response) -> dict[str, Any]:
        """Map an HTTP error response to a standardized error dict.

        Args:
            response: The :class:`requests.Response` with a non-2xx status.

        Returns:
            A standardized error dictionary.
        """
        status = response.status_code

        # Try to extract a message from the JSON body if present.
        try:
            body = response.json()
            detail = body.get("error", response.text)
        except (ValueError, AttributeError):
            detail = response.text or response.reason

        mapping: dict[int, str] = {
            403: "AUTH_ERROR",
            404: "NOT_FOUND",
            409: "CONFLICT",
            415: "UNSUPPORTED_FILE_TYPE",
        }

        if status in mapping:
            code = mapping[status]
        elif 500 <= status < 600:
            code = "SERVER_ERROR"
        else:
            code = "HTTP_ERROR"

        human_messages: dict[str, str] = {
            "AUTH_ERROR": "Authentication failed. Check your API key.",
            "NOT_FOUND": "The requested resource was not found.",
            "CONFLICT": ("Conflict: the printer may not be in the correct state for this operation."),
            "UNSUPPORTED_FILE_TYPE": "The file type is not supported by OctoPrint.",
            "SERVER_ERROR": f"OctoPrint server error ({status}): {detail}",
        }

        message = human_messages.get(code, f"HTTP {status}: {detail}")

        return self._error(code, message, http_status=status)

    def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request with exponential-backoff retry logic.

        Retries are attempted for :class:`ConnectionError`,
        :class:`Timeout`, and HTTP 502/503/504 responses.

        Args:
            method: HTTP method (``"GET"``, ``"POST"``, etc.).
            path: Relative API path (e.g. ``"/api/printer"``).
            json: JSON body payload.
            params: URL query parameters.
            files: Multipart file upload mapping.
            data: Form-encoded body data (used alongside *files*).

        Returns:
            A standardized response dictionary.
        """
        url = self._url(path)
        last_error: dict[str, Any] | None = None

        for attempt in range(self.retries):
            try:
                response = self._session.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    files=files,
                    data=data,
                    timeout=self.timeout,
                )

                # On success, return immediately.
                if response.ok:
                    # Some endpoints (e.g. 204 No Content) return no body.
                    if response.status_code == 204 or not response.content:
                        return self._success(None)
                    try:
                        return self._success(response.json())
                    except ValueError:
                        return self._success(None)

                # Non-retryable HTTP error -- return immediately.
                if response.status_code not in self._RETRYABLE_STATUS_CODES:
                    return self._classify_http_error(response)

                # Retryable HTTP status -- fall through to backoff.
                last_error = self._classify_http_error(response)

            except Timeout:
                last_error = self._error(
                    "TIMEOUT",
                    (f"Request to {url} timed out after {self.timeout}s (attempt {attempt + 1}/{self.retries})."),
                )
            except ConnectionError:
                last_error = self._error(
                    "CONNECTION_ERROR",
                    (f"Could not connect to OctoPrint at {self.host} (attempt {attempt + 1}/{self.retries})."),
                )
            except RequestException as exc:
                # Catch-all for other request errors (InvalidURL, etc.)
                return self._error(
                    "CONNECTION_ERROR",
                    f"Request error: {exc}",
                )

            # Exponential backoff: 1s, 2s, 4s, ...
            if attempt < self.retries - 1:
                time.sleep(2**attempt)

        # All retries exhausted -- return the last captured error.
        assert last_error is not None
        return last_error

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def get_connection(self) -> dict[str, Any]:
        """Get the current connection state of the printer.

        Calls ``GET /api/connection``.

        Returns:
            Standardized response with connection state information
            including current port, baudrate, and printer profile.
        """
        return self._request("GET", "/api/connection")

    # ------------------------------------------------------------------
    # Printer
    # ------------------------------------------------------------------

    def get_printer_state(self) -> dict[str, Any]:
        """Get the full printer state including temperatures and flags.

        Calls ``GET /api/printer``.

        Returns:
            Standardized response with temperature data, SD state, and
            state flags (printing, paused, error, etc.).
        """
        return self._request("GET", "/api/printer")

    # ------------------------------------------------------------------
    # Job
    # ------------------------------------------------------------------

    def get_job(self) -> dict[str, Any]:
        """Get information about the current print job.

        Calls ``GET /api/job``.

        Returns:
            Standardized response with job information including file
            details, progress percentage, and estimated print time.
        """
        return self._request("GET", "/api/job")

    def start_job(self) -> dict[str, Any]:
        """Start the currently loaded print job.

        Calls ``POST /api/job`` with ``{"command": "start"}``.

        Returns:
            Standardized response. Success indicates the job was started.
        """
        return self._request("POST", "/api/job", json={"command": "start"})

    def cancel_job(self) -> dict[str, Any]:
        """Cancel the current print job.

        Calls ``POST /api/job`` with ``{"command": "cancel"}``.

        Returns:
            Standardized response. Success indicates the job was cancelled.
        """
        return self._request("POST", "/api/job", json={"command": "cancel"})

    def pause_job(self, action: str = "toggle") -> dict[str, Any]:
        """Pause, resume, or toggle the current print job.

        Calls ``POST /api/job`` with ``{"command": "pause", "action": ...}``.

        Args:
            action: One of ``"pause"``, ``"resume"``, or ``"toggle"``.
                Defaults to ``"toggle"``.

        Returns:
            Standardized response. Success indicates the action was applied.
        """
        return self._request(
            "POST",
            "/api/job",
            json={"command": "pause", "action": action},
        )

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def list_files(
        self,
        location: str = "local",
        recursive: bool = True,
    ) -> dict[str, Any]:
        """List files stored on the printer.

        Calls ``GET /api/files/{location}``.

        Args:
            location: Storage location -- ``"local"`` or ``"sdcard"``.
            recursive: Whether to list files recursively. Defaults to
                ``True``.

        Returns:
            Standardized response with a list of file and folder entries.
        """
        params: dict[str, Any] = {}
        if recursive:
            params["recursive"] = "true"
        return self._request(
            "GET",
            f"/api/files/{quote(location, safe='')}",
            params=params,
        )

    def get_file_info(self, location: str, path: str) -> dict[str, Any]:
        """Get detailed information about a specific file.

        Calls ``GET /api/files/{location}/{path}``.

        Args:
            location: Storage location (``"local"`` or ``"sdcard"``).
            path: Path to the file relative to the storage root.

        Returns:
            Standardized response with file metadata including size,
            date, hash, and analysis results.
        """
        return self._request(
            "GET",
            f"/api/files/{quote(location, safe='')}/{path}",
        )

    def upload_file(
        self,
        file_path: str,
        location: str = "local",
        select: bool = False,
        print_after: bool = False,
    ) -> dict[str, Any]:
        """Upload a file to OctoPrint.

        Calls ``POST /api/files/{location}`` with a multipart file upload.

        Args:
            file_path: Absolute or relative path to the local file to
                upload.
            location: Destination storage (``"local"`` or ``"sdcard"``).
            select: If ``True``, the file will be selected for printing
                after upload.
            print_after: If ``True``, printing will start immediately
                after upload.

        Returns:
            Standardized response with the uploaded file metadata.

        Raises:
            FileNotFoundError: If *file_path* does not exist.
        """
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            return self._error(
                "NOT_FOUND",
                f"Local file not found: {abs_path}",
            )

        filename = os.path.basename(abs_path)

        try:
            with open(abs_path, "rb") as fh:
                files = {"file": (filename, fh, "application/octet-stream")}
                form_data: dict[str, str] = {}
                if select:
                    form_data["select"] = "true"
                if print_after:
                    form_data["print"] = "true"

                return self._request(
                    "POST",
                    f"/api/files/{quote(location, safe='')}",
                    files=files,
                    data=form_data if form_data else None,
                )
        except PermissionError:
            return self._error(
                "CONNECTION_ERROR",
                f"Permission denied reading file: {abs_path}",
            )

    def select_file(
        self,
        location: str,
        path: str,
        print_after: bool = False,
    ) -> dict[str, Any]:
        """Select a file on the printer for printing.

        Calls ``POST /api/files/{location}/{path}`` with the ``select``
        command.

        Args:
            location: Storage location (``"local"`` or ``"sdcard"``).
            path: Path to the file relative to the storage root.
            print_after: If ``True``, printing starts immediately after
                selection.

        Returns:
            Standardized response. Success indicates the file was selected.
        """
        return self._request(
            "POST",
            f"/api/files/{quote(location, safe='')}/{path}",
            json={"command": "select", "print": print_after},
        )

    # ------------------------------------------------------------------
    # Printer tool / bed temperature
    # ------------------------------------------------------------------

    def set_tool_temp(self, targets: dict[str, int]) -> dict[str, Any]:
        """Set target temperatures for printer tool(s) (hotend).

        Calls ``POST /api/printer/tool``.

        Args:
            targets: Mapping of tool identifiers to target temperatures
                in Celsius, e.g. ``{"tool0": 210}``.

        Returns:
            Standardized response. Success indicates temperatures were set.
        """
        return self._request(
            "POST",
            "/api/printer/tool",
            json={"command": "target", "targets": targets},
        )

    def set_bed_temp(self, target: int) -> dict[str, Any]:
        """Set the target temperature for the heated bed.

        Calls ``POST /api/printer/bed``.

        Args:
            target: Target bed temperature in Celsius. Pass ``0`` to
                turn off the heater.

        Returns:
            Standardized response. Success indicates the temperature was set.
        """
        return self._request(
            "POST",
            "/api/printer/bed",
            json={"command": "target", "target": target},
        )

    # ------------------------------------------------------------------
    # G-code commands
    # ------------------------------------------------------------------

    def send_gcode(self, commands: str | list[str]) -> dict[str, Any]:
        """Send one or more G-code commands to the printer.

        Calls ``POST /api/printer/command``.

        Args:
            commands: A single G-code string or a list of G-code strings
                to send sequentially.

        Returns:
            Standardized response. Success indicates the commands were
            enqueued.
        """
        if isinstance(commands, str):
            commands = [commands]
        return self._request(
            "POST",
            "/api/printer/command",
            json={"commands": commands},
        )
