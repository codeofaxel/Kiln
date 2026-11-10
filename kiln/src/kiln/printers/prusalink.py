"""Prusa Link adapter for the Kiln printer abstraction layer."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, ClassVar
from urllib.parse import quote

import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import RequestException, Timeout

from kiln.cli_command import kiln_command
from kiln.printer_backends import PRUSALINK_PI_KEY_WHERE, need_where
from kiln.printers.base import (
    CameraHandshake,
    FilamentHandlingUnsupported,
    FilamentOpPlan,
    FilamentOpResult,
    JobProgress,
    JobResult,
    MachineIdentity,
    NozzleSetting,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterInfo,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from kiln.printers.command_verdict import CommandVerdict

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({502, 503, 504})
_FILE_ROOTS: tuple[str, ...] = ("usb", "local")
_FILE_ROOT_FALLBACK_HTTP_CODES: tuple[int, ...] = (403, 404)

_BAD_API_KEY_MARKER = "Bad X-Api-Key"


class PrusaLinkAuthError(PrinterError):
    """The printer refused Kiln's API key."""


_PRUSA_TYPE_CODES: dict[str, str] = {
    "1.2.5": "prusa_mk2_5",
    "1.2.6": "prusa_mk2_5s",
    "1.3.0": "prusa_mk3",
    "1.3.1": "prusa_mk3s",
    "1.3.5": "prusa_mk3_5",
    "1.3.6": "prusa_mk3_5s",
    "1.3.9": "prusa_mk3_9",
    "1.3.10": "prusa_mk3_9s",
    "1.4.0": "prusa_mk4",
    "1.4.1": "prusa_mk4s",
    "2.1.0": "prusa_mini",
    "3.1.0": "prusa_xl",
    "4.1.0": "prusa_ix",
    "7.1.0": "prusa_core_one",
    "7.2.0": "prusa_core_one",  # COREONEOAK, a Core One variant
    "8.1.0": "prusa_core_one_l",
}

_PRUSA_ORIGINAL_NAMES: dict[str, str] = {
    "I3MK25": "prusa_mk2_5",
    "I3MK25S": "prusa_mk2_5s",
    "I3MK3": "prusa_mk3",
    "I3MK3S": "prusa_mk3s",
}

_STATE_MAP: dict[str, PrinterStatus] = {
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

# How the last job ENDED, for the Prusa Link states that say so
_JOB_RESULT_MAP: dict[str, JobResult] = {
    "FINISHED": JobResult.COMPLETED,
    "STOPPED": JobResult.CANCELLED,
}


# ---------------------------------------------------------------------------
# Flow-anomaly classification
# ---------------------------------------------------------------------------

_ANOMALY_STATES: frozenset[str] = frozenset({"ATTENTION", "ERROR"})

_FILAMENT_MESSAGE_HINTS: tuple[str, ...] = (
    "filament",       # generic — covers "filament jam", "no filament", etc.
    "runout",         # "runout sensor triggered", "filament runout"
    "no_filament",    # MK3-era state flag string
    "no filament",
    "mmu",            # MMU3 error states are virtually always filament-path
    "extruder",       # "blocked extruder", "extruder fault"
    "jam",            # explicit jam wording
    "feed",           # "feeding abnormal", "feed error"
    "clog",
)

_FILAMENT_HINT_SUPPRESSORS: tuple[str, ...] = (
    "bed leveling",
    "first layer",
    "calibration",
    "user pause",
    "user-paused",
    "front panel",
)


def _classify_flow_anomaly(
    prusalink_state: str,
    message: str = "",
) -> tuple[str, str] | None:
    """Return (event_type, severity) for a Prusa Link state transition."""
    if not prusalink_state:
        return None

    state_upper = prusalink_state.strip().upper()
    if state_upper not in _ANOMALY_STATES:
        return None

    message_lower = (message or "").lower()

    for suppressor in _FILAMENT_HINT_SUPPRESSORS:
        if suppressor in message_lower:
            return None

    if any(
        hint in message_lower
        for hint in ("runout", "no filament", "no_filament", "jam", "clog", "mmu")
    ):
        return "filament_jam", "high"

    # Generic ATTENTION + filament-path hint — under-extrusion bucket
    if state_upper == "ATTENTION":
        if any(hint in message_lower for hint in _FILAMENT_MESSAGE_HINTS):
            return "under_extrusion", "medium"
        return None

    # ERROR with a filament hint — treat as under_extrusion medium
    if any(hint in message_lower for hint in _FILAMENT_MESSAGE_HINTS):
        return "under_extrusion", "medium"

    return None


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


class PrusaLinkAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by the Prusa Link HTTP API."""

    _DURATION_SEMANTICS: ClassVar[str] = "frozen"

    #: How Kiln learns this printer's own pause and cancel moves.
    head_position = ("PrusaLink reports X and Y only while the head is still, and no extruder, so a "
                     "pause's path cannot be read; its owner teaches Kiln the moves through the guided "
                     "session's questions")

    def __init__(
        self,
        host: str,
        api_key: str | None = None,
        timeout: int = 30,
        retries: int = 3,
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

        self._prior_state: str | None = None

        self._printer_info: PrinterInfo | None = None
        self._printer_info_retry_at: float = 0.0
        self._identity_channels: dict[str, str] = {}

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return "prusalink"

    @property
    def capabilities(self) -> PrinterCapabilities:
        """Capabilities supported by the Prusa Link backend."""
        return PrinterCapabilities(
            can_upload=True,
            can_set_temp=False,
            can_send_gcode=False,
            can_pause=True,
            supported_extensions=(".gcode", ".gco", ".g", ".bgcode"),
        )

    def get_printer_info(self) -> PrinterInfo | None:
        """The printer's self-reported model, for telemetry and display."""
        if self._printer_info is not None:
            return self._printer_info
        if time.monotonic() < self._printer_info_retry_at:
            return None
        try:
            data = self._get_json("/api/version")
        except PrinterError:
            self._printer_info_retry_at = time.monotonic() + 300.0
            return None
        if not isinstance(data, dict):
            return None

        code = str(data.get("printer") or "").strip()
        original = str(data.get("original") or "").strip()
        type_name = original.removeprefix("PrusaLink").strip().upper()
        by_code = _PRUSA_TYPE_CODES.get(code)
        by_name = _PRUSA_ORIGINAL_NAMES.get(type_name)

        channels: dict[str, str] = {}
        if by_code:
            channels["api_version_type_code"] = by_code
        if by_name:
            channels["api_version_original"] = by_name
        self._identity_channels = channels

        if by_code and by_name and by_code != by_name:
            logger.warning(
                "PrusaLink identity channels disagree: type code %r says %r, "
                "original %r says %r. Reporting no model — set `printer_model` "
                "in ~/.kiln/config.yaml to settle it.",
                code, by_code, original, by_name,
            )
            return None
        if by_code:
            self._printer_info = PrinterInfo(
                model=by_code, raw_model=code, source="http"
            )
            return self._printer_info
        if by_name:
            self._printer_info = PrinterInfo(
                model=by_name, raw_model=original, source="http"
            )
            return self._printer_info
        return None

    def get_identity_channels(self) -> dict[str, str]:
        """Both /api/version identity fields, each with its claim."""
        self.get_printer_info()
        return dict(self._identity_channels)

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
        headers: dict[str, str] | None = None,
        data: Any | None = None,
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
                            f"Your API key is invalid or missing. The key is the PrusaLink "
                            f"password, {need_where('prusalink', 'api_key')}. Then update "
                            f"with: {kiln_command('auth --name <name> --host', self._host)} "
                            f"--type prusalink --api-key <YOUR_KEY>",
                        )
                    if response.status_code == 403 and _BAD_API_KEY_MARKER in (response.text or ""):
                        raise PrusaLinkAuthError(
                            f"Prusa Link at {self._host} refused the API key (HTTP 403, "
                            f"'{_BAD_API_KEY_MARKER}'). That is how PrusaLink on a Raspberry Pi "
                            f"(MK3S/MK2.5) answers a wrong or empty key. Its API key is not the "
                            f"login the setup wizard made and is empty until you make one, "
                            f"{PRUSALINK_PI_KEY_WHERE}, not on the printer's screen. Then update "
                            f"with: {kiln_command('auth --name <name> --host', self._host)} "
                            f"--type prusalink --api-key <YOUR_KEY>",
                        )
                    if response.status_code == 403:
                        endpoint_hint = (
                            " This endpoint is under /api/v1/files; if status/cancel work but "
                            "files/print fail, verify the storage root/path and use the API "
                            f"filename (often 8.3) shown by `{kiln_command('files')}`."
                            if path.startswith("/api/v1/files/")
                            else ""
                        )
                        raise PrinterError(
                            f"Access forbidden (HTTP 403) for Prusa Link at {self._host} "
                            f"on {method} {path}. Your API key may lack required permissions, "
                            f"or this firmware may reject the requested operation/path. "
                            f"Check the key: it is the PrusaLink password, "
                            f"{need_where('prusalink', 'api_key')}.{endpoint_hint}",
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
                        f"Prusa Link returned HTTP {response.status_code} for {method} {path}: {response.text[:300]}",
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
                    method,
                    path,
                    backoff,
                    attempt + 1,
                    self._retries,
                )
                time.sleep(backoff)

        assert last_exc is not None
        raise last_exc

    def _get_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
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

    def _get_active_job_id(self) -> int | None:
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
        if isinstance(exc, PrusaLinkAuthError):
            # A refused key is the same on every storage root.
            return False
        return any(cls._is_http_error(exc, code) for code in _FILE_ROOT_FALLBACK_HTTP_CODES)

    def _iter_file_roots(self, preferred: str | None = None) -> list[str]:
        roots = list(_FILE_ROOTS)
        if preferred and preferred in roots:
            return [preferred, *[r for r in roots if r != preferred]]
        return roots

    def _split_storage_root(self, file_path: str) -> tuple[str | None, str]:
        clean = file_path.strip().lstrip("/")
        for root in _FILE_ROOTS:
            prefix = f"{root}/"
            if clean.lower().startswith(prefix):
                return root, clean[len(prefix) :]
        return None, clean

    def _resolve_print_path(self, requested: str) -> str:
        """Resolve a user-provided file identifier to a Prusa API path."""
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
            if candidate.name.lower() == basename or candidate.path.rsplit("/", 1)[-1].lower() == basename
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

    def read_machine_identity(self) -> MachineIdentity | None:
        """The printer's own serial number, from its info endpoint."""
        try:
            info = self._get_json("/api/v1/info")
        except PrinterError:
            return None
        raw = info.get("serial") if isinstance(info, dict) else None
        value = str(raw or "").strip()
        if not value:
            return None
        return MachineIdentity(value=value, source="prusalink_info.serial", kind="printer_serial")

    def read_nozzle_setting(self) -> NozzleSetting | None:
        """The nozzle diameter this printer holds in its own settings."""
        try:
            info = self._get_json("/api/v1/info")
        except PrinterError:
            return None
        raw = info.get("nozzle_diameter") if isinstance(info, dict) else None
        try:
            diameter = float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            diameter = None
        if diameter is None:
            return None
        return NozzleSetting(material=None, diameter_mm=diameter, source="prusalink_info")

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures."""
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
        job_result = _JOB_RESULT_MAP.get(state_str)
        job = _safe_get(data, "job", default={})
        if state_str == "ATTENTION" and isinstance(job, dict) and job.get("id") is not None:
            mapped_status = PrinterStatus.PAUSED

        tool_actual = printer.get("temp_nozzle") if isinstance(printer, dict) else None
        tool_target = printer.get("target_nozzle") if isinstance(printer, dict) else None
        bed_actual = printer.get("temp_bed") if isinstance(printer, dict) else None
        bed_target = printer.get("target_bed") if isinstance(printer, dict) else None
        chamber_actual = printer.get("temp_chamber") if isinstance(printer, dict) else None
        chamber_target = printer.get("target_chamber") if isinstance(printer, dict) else None

        self._maybe_fire_flow_anomaly(prior=self._prior_state, current=state_str, printer=printer)
        self._prior_state = state_str

        return PrinterState(
            connected=True,
            state=mapped_status,
            last_job_result=job_result,
            tool_temp_actual=tool_actual,
            tool_temp_target=tool_target,
            bed_temp_actual=bed_actual,
            bed_temp_target=bed_target,
            chamber_temp_actual=chamber_actual,
            chamber_temp_target=chamber_target,
        )

    # ------------------------------------------------------------------
    # Internal -- flow anomaly cross-check
    # ------------------------------------------------------------------

    def _maybe_fire_flow_anomaly(
        self,
        *,
        prior: str | None,
        current: str,
        printer: Any,
    ) -> None:
        """Feed a transition-into-anomaly state to the kiln-pro wear cross-check."""
        current_upper = (current or "").strip().upper()
        prior_upper = (prior or "").strip().upper()

        # Suppress steady-state and non-transition cases.
        if current_upper not in _ANOMALY_STATES:
            return
        if prior_upper in _ANOMALY_STATES:
            return

        message_parts: list[str] = []
        if isinstance(printer, dict):
            for key in ("message", "status_printer", "error", "warning"):
                raw = printer.get(key)
                if isinstance(raw, str):
                    message_parts.append(raw)
                elif isinstance(raw, dict):
                    for sub_key in ("message", "text", "description"):
                        sub = raw.get(sub_key)
                        if isinstance(sub, str):
                            message_parts.append(sub)
        message = " | ".join(part for part in message_parts if part)

        classification = _classify_flow_anomaly(current_upper, message)
        if classification is None:
            return

        event_type, severity = classification
        from kiln._pro_nozzle_bridge import report_flow_anomaly

        report_flow_anomaly(self.name, event_type, severity)

    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active print job."""
        try:
            data = self._get_json("/api/v1/status")
        except PrinterError:
            return JobProgress()

        job = _safe_get(data, "job", default={})
        if not isinstance(job, dict):
            return JobProgress()

        progress = job.get("progress")
        completion: float | None = None
        if progress is not None:
            completion = round(float(progress), 2)

        time_printing = job.get("time_printing")
        time_remaining = job.get("time_remaining")

        print_time_seconds: int | None = None
        print_time_left_seconds: int | None = None

        if time_printing is not None:
            print_time_seconds = int(time_printing)
        if time_remaining is not None:
            print_time_left_seconds = int(time_remaining)

        job_id = job.get("id")

        return JobProgress(
            file_name=None,  # Prusa Link doesn't include filename in status
            job_id=str(job_id) if job_id is not None else None,
            completion=completion,
            print_time_seconds=print_time_seconds,
            print_time_left_seconds=print_time_left_seconds,
        )

    def list_files(self) -> list[PrinterFile]:
        """Return a list of G-code files across supported Prusa storage roots."""
        results: list[PrinterFile] = []
        successful_roots = 0
        fallback_errors: list[tuple[str, PrinterError]] = []

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

        deduped: list[PrinterFile] = []
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
        entries: list[Any],
        results: list[PrinterFile],
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
        """Upload a local G-code file to the printer via Prusa Link."""
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

        last_fallback_error: PrinterError | None = None

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

    def _start_print_impl(self, file_name: str, **_kwargs: Any) -> PrintResult:
        """Begin printing a file on the printer."""
        preferred_root, _ = self._split_storage_root(file_name)
        resolved_path = self._resolve_print_path(file_name)
        encoded = quote(resolved_path, safe="/")

        roots = self._iter_file_roots(preferred=preferred_root)
        last_fallback_error: PrinterError | None = None
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
        """Cancel the currently running print job."""
        job_id = self._get_active_job_id()
        if job_id is None:
            raise PrinterError("No active job to cancel.")

        self._request("DELETE", f"/api/v1/job/{job_id}")
        return PrintResult(success=True, message="Print cancelled.")

    def emergency_stop(self) -> PrintResult:
        """Perform emergency stop by cancelling the active job."""
        try:
            return self.cancel_print()
        except PrinterError:
            raise PrinterError(
                "Emergency stop failed — Prusa Link does not support "
                "raw G-code commands.  Power off the printer manually."
            ) from None

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job."""
        job_id = self._get_active_job_id()
        if job_id is None:
            raise PrinterError("No active job to pause.")

        self._request("PUT", f"/api/v1/job/{job_id}/pause")
        return PrintResult(success=True, message="Print paused.")

    def _resume_print_impl(self) -> PrintResult:
        """Resume a previously paused print job."""
        job_id = self._get_active_job_id()
        if job_id is None:
            raise PrinterError("No active job to resume.")

        self._request("PUT", f"/api/v1/job/{job_id}/resume")
        return PrintResult(success=True, message="Print resumed.")

    # ------------------------------------------------------------------
    # PrinterAdapter -- temperature control
    # ------------------------------------------------------------------

    def set_tool_temp(self, target: float) -> CommandVerdict:
        """Not natively supported by Prusa Link."""
        raise PrinterError(
            "Prusa Link does not support direct temperature control. "
            "Temperature is managed through G-code in print files."
        )

    def set_bed_temp(self, target: float) -> CommandVerdict:
        """Not natively supported by Prusa Link."""
        raise PrinterError(
            "Prusa Link does not support direct temperature control. "
            "Temperature is managed through G-code in print files."
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def send_gcode(self, commands: list[str]) -> CommandVerdict:
        """Not supported by Prusa Link."""
        raise PrinterError(
            "Prusa Link does not support sending raw G-code commands. "
            "See: https://github.com/prusa3d/Prusa-Link/issues/832"
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- file deletion
    # ------------------------------------------------------------------

    def delete_file(self, file_path: str) -> bool:
        """Delete a G-code file from the printer's local storage."""
        preferred_root, normalized_path = self._split_storage_root(file_path)
        if not normalized_path:
            raise PrinterError("File path must not be empty.")

        encoded = quote(normalized_path, safe="/")
        roots = self._iter_file_roots(preferred=preferred_root)
        last_fallback_error: PrinterError | None = None
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

    def get_snapshot(self) -> bytes | None:
        """Capture a webcam snapshot from Prusa Link."""
        try:
            response = self._request("GET", "/api/v1/cameras/snap")
            if response.ok and response.content:
                return response.content
        except Exception:
            logger.debug("Webcam snapshot failed", exc_info=True)
        return None

    _CAMERA_SNAP_PATH: ClassVar[str] = "/api/v1/cameras/snap"

    def _own_camera_handshake(self) -> CameraHandshake:
        """Headers only, through the authenticated session: no picture is read."""
        from kiln.camera_check import handshake_address
        from kiln.printers.base import CAMERA_HANDSHAKE_ADDRESS_TIMEOUT

        answered, what = handshake_address(
            self._url(self._CAMERA_SNAP_PATH),
            timeout=CAMERA_HANDSHAKE_ADDRESS_TIMEOUT,
            session=self._session,
        )
        if answered:
            return CameraHandshake(True, "prusalink_cameras_snap", f"The printer's camera answered with {what}.")
        return CameraHandshake(
            False, "prusalink_cameras_snap", f"The printer's camera endpoint gave {what}."
        )

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Filament handling — not available on this backend, said plainly
    # ------------------------------------------------------------------

    _FILAMENT_UNSUPPORTED_REASON = (
        "Prusa Link exposes no raw G-code endpoint, so there is no way to "
        "heat and drive the extruder (github.com/prusa3d/Prusa-Link/issues/832). "
        "Use the printer's own screen: Filament → Load / Unload / Purge."
    )

    def _load_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        raise FilamentHandlingUnsupported(
            f"{self.name} cannot load filament through Kiln: " + self._FILAMENT_UNSUPPORTED_REASON
        )

    def _unload_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        raise FilamentHandlingUnsupported(
            f"{self.name} cannot unload filament through Kiln: " + self._FILAMENT_UNSUPPORTED_REASON
        )

    def _purge_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        raise FilamentHandlingUnsupported(
            f"{self.name} cannot purge filament through Kiln: " + self._FILAMENT_UNSUPPORTED_REASON
        )

    def __repr__(self) -> str:
        return f"<PrusaLinkAdapter host={self._host!r}>"
