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
from typing import Any, ClassVar
from urllib.parse import quote

import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import RequestException, Timeout

from kiln.printers.base import (
    FilamentHandlingUnsupported,
    FilamentOpPlan,
    FilamentOpResult,
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
)

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({502, 503, 504})
_FILE_ROOTS: tuple[str, ...] = ("usb", "local")
_FILE_ROOT_FALLBACK_HTTP_CODES: tuple[int, ...] = (403, 404)

# Printer type codes reported in the ``printer`` field of GET /api/version
# by PrusaLink on Buddy firmware (MINI / MK3.5 / MK3.9 / MK4 / XL / iX /
# Core One).  Verified against Prusa-Firmware-Buddy
# include/common/printer_model_data.hpp (master, checked 2026-08-09):
# the field is PrinterVersion{type, version, subversion} formatted
# "%i.%i.%i" by get_version() in lib/WUI/link_content/basic_gets.cpp.
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
    # Deliberately absent: "5.1.0" — Buddy assigns it to the XL dev kit
    # while the Prusa Connect SDK assigns it to the SL1 resin printer,
    # so the code alone is ambiguous.  Also absent: 7.10.0 / 8.10.0
    # (Core One iNdx industrial variants) — too new to trust the codes
    # as settled.  Unknown codes report nothing (family grain).
    # 1.2.5 / 1.2.6 (MK2.5 / MK2.5S) come from the Connect SDK
    # PrinterType enum — the Python PrusaLink serves those too.
}

# Printer type names reported by the Python PrusaLink (MK3-era printers
# with a Pi) in the ``original`` field of GET /api/version, formatted
# "PrusaLink <TYPE>".  Verified against prusa3d/Prusa-Link
# prusa/link/web/main.py api_version() and the PrinterType enum in
# prusa3d/Prusa-Connect-SDK-Printer prusa/connect/printer/const.py
# (master, checked 2026-08-09).  FDM members only — the enum's resin
# entries (SL1, SL1S, M1) don't run PrusaLink and are left out.
_PRUSA_ORIGINAL_NAMES: dict[str, str] = {
    "I3MK25": "prusa_mk2_5",
    "I3MK25S": "prusa_mk2_5s",
    "I3MK3": "prusa_mk3",
    "I3MK3S": "prusa_mk3s",
}

# Prusa Link printer states → PrinterStatus
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

# How the last job ENDED, for the Prusa Link states that say so.  Both
# used to arrive as the same IDLE as a printer sitting untouched — so a
# print that ran to completion and one a user stopped at layer 3 were
# reported identically, and so was neither happening at all.
#
# "STOPPED" is Prusa Link's word for a print the user ended early; the
# firmware has no separate cancelled state.  "ERROR" and "ATTENTION" are
# deliberately absent: those describe a condition the PRINTER is in
# (a thermal fault, a filament-change prompt), not a verdict on a job,
# and they already map to an ERROR status that says so.
_JOB_RESULT_MAP: dict[str, JobResult] = {
    "FINISHED": JobResult.COMPLETED,
    "STOPPED": JobResult.CANCELLED,
}


# ---------------------------------------------------------------------------
# Flow-anomaly classification
# ---------------------------------------------------------------------------

# Prusa Link state strings that indicate the printer is in a
# user-intervention or fault posture.  Used by the flow-anomaly
# cross-check to recognize transitions INTO an anomaly window that the
# kiln-pro nozzle wear cross-check consumes.
#
# Source: Prusa Link Web API (`/api/v1/status`).  The `printer.state`
# enum carries values like "IDLE", "PRINTING", "PAUSED", "FINISHED",
# "ATTENTION", "ERROR".  ATTENTION on MK4 / XL / MMU3 commonly
# correlates with filament-feeding issues (jam, runout, MMU error);
# ERROR is broader and only counts when the accompanying message
# carries a filament hint.
_ANOMALY_STATES: frozenset[str] = frozenset({"ATTENTION", "ERROR"})

# Substrings inside the printer's status / error message that point at
# the filament path.  Conservatism is the right call — false positives
# on flow-anomaly tagging poison the wear-rate signal more than missed
# positives.  We only fire for messages whose text clearly implicates
# filament feeding; bed-leveling / first-layer-calibration / user-pause
# messages are deliberately omitted.
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

# Strings that look filament-adjacent but are NOT flow anomalies — used
# to suppress false positives when the message text happens to mention
# one of the hints above in an unrelated context.
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
    """Return (event_type, severity) for a Prusa Link state transition.

    Args:
        prusalink_state: The raw ``printer.state`` enum value reported
            by ``/api/v1/status`` (e.g. ``"ATTENTION"``, ``"ERROR"``,
            ``"IDLE"``).  Case-sensitive — Prusa Link emits upper-case.
        message: Optional human-readable status / error text from
            ``printer.status_printer.message``, ``printer.error.text``,
            or equivalent surface.  Used to disambiguate generic
            ATTENTION / ERROR states; without filament-related text,
            an ATTENTION state is treated as "not a flow anomaly" and
            ``None`` is returned.

    Returns:
        ``None`` when the state isn't a flow anomaly (IDLE, BUSY,
        PRINTING, PAUSED, FINISHED, READY, STOPPED), when the state is
        ATTENTION / ERROR but the message doesn't mention the filament
        path, or when a suppressor substring (bed leveling, first
        layer, calibration, user pause) is present.

        Otherwise:
          * ``("filament_jam", "high")`` — explicit runout / jam /
            no-filament message text, or any MMU error.  Mirrors the
            Bambu wire's "high" severity bucket for hard stoppages.
          * ``("under_extrusion", "medium")`` — ATTENTION state with a
            generic filament / extruder / feed hint that doesn't
            escalate to a hard jam.

        Generic ERROR states without a filament hint never fire.
    """
    if not prusalink_state:
        return None

    state_upper = prusalink_state.strip().upper()
    if state_upper not in _ANOMALY_STATES:
        return None

    message_lower = (message or "").lower()

    # Suppress false positives where the message text mentions a
    # filament hint as part of an unrelated workflow (e.g. "first
    # layer calibration: insert filament").
    for suppressor in _FILAMENT_HINT_SUPPRESSORS:
        if suppressor in message_lower:
            return None

    # Hard-jam signals — runout sensor fire, explicit "no filament" or
    # "jam" text, MMU error window.  All escalate to high severity.
    if any(
        hint in message_lower
        for hint in ("runout", "no filament", "no_filament", "jam", "clog", "mmu")
    ):
        return "filament_jam", "high"

    # Generic ATTENTION + filament-path hint — under-extrusion bucket.
    # ERROR without a filament hint already returned None above; ERROR
    # with a clear filament word also falls through to here.
    if state_upper == "ATTENTION":
        if any(hint in message_lower for hint in _FILAMENT_MESSAGE_HINTS):
            return "under_extrusion", "medium"
        # ATTENTION without context is too generic to attribute to
        # flow — could be user-pause, door open, filament-load prompt.
        return None

    # ERROR with a filament hint — treat as under_extrusion medium.
    # ERROR without is already None.
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

    # The job's time_printing is the printer's own clock, frozen at the
    # ending — a late reading is merely late and still correct.
    _DURATION_SEMANTICS: ClassVar[str] = "frozen"

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

        # Last raw Prusa Link state string observed by get_state().  The
        # flow-anomaly cross-check fires only on a TRANSITION into an
        # anomaly state, not on every poll while the printer sits in
        # ATTENTION; a steady-state error would otherwise flood the
        # kiln-pro wear-signal log every poll cycle.
        self._prior_state: str | None = None

        # Successful get_printer_info() result, cached for the session —
        # the hardware behind a host:port doesn't change mid-process.
        # After a failed probe, _printer_info_retry_at holds the
        # monotonic time before which we won't probe again: the fleet
        # view resolves models at poll frequency, and an offline
        # printer must cost one bounded probe per cooldown window, not
        # one per poll.
        self._printer_info: PrinterInfo | None = None
        self._printer_info_retry_at: float = 0.0
        # What each /api/version identity field resolved to, for
        # diagnostics (see get_identity_channels).
        self._identity_channels: dict[str, str] = {}

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return "prusalink"

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

    def get_printer_info(self) -> PrinterInfo | None:
        """The printer's self-reported model, for telemetry and display.

        One GET /api/version, parsed against the two real PrusaLink
        implementations:

        * Buddy firmware (MINI / MK3.5 / MK3.9 / MK4 / XL / iX / Core
          One) reports a ``printer`` type code like ``"1.4.1"``, mapped
          through :data:`_PRUSA_TYPE_CODES`.
        * The Python PrusaLink (MK3-era printers with a Pi) reports an
          ``original`` string like ``"PrusaLink I3MK3S"``, mapped
          through :data:`_PRUSA_ORIGINAL_NAMES`.

        A successful result is cached for the adapter's lifetime; an
        unreachable printer or unknown code returns ``None`` and the
        caller keeps its family-grain fallback.  A failed probe backs
        off for five minutes so poll-frequency callers (the registry
        fleet view) never stack timeouts against an offline printer.

        SAFETY BOUNDARY: telemetry/display only.  The config-declared
        model (``printer_model`` in config.yaml) owns every safety and
        behavior decision; this self-report must never override it
        where the two disagree — it fills in only where config is
        silent (see ``PrinterAdapter.get_printer_info`` and commit
        a19e665b).
        """
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
            # A proxy or captive portal answering with a JSON list/string
            # is not a printer talking — treat it as no answer.
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
            # The two firmware families never both answer in practice —
            # Buddy fills `printer`, the Python PrusaLink fills
            # `original`.  If both answer and disagree, one of our
            # tables is wrong; report nothing rather than pick.
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
        self.get_printer_info()  # populates the cache (no-op when cached)
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
                            f"Your API key is invalid or missing. Find the correct key in "
                            f"Settings > Network > PrusaLink on your printer's LCD, then update "
                            f"with: kiln auth --name <name> --host {self._host} "
                            f"--type prusalink --api-key <YOUR_KEY>",
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
        job_result = _JOB_RESULT_MAP.get(state_str)

        tool_actual = printer.get("temp_nozzle") if isinstance(printer, dict) else None
        tool_target = printer.get("target_nozzle") if isinstance(printer, dict) else None
        bed_actual = printer.get("temp_bed") if isinstance(printer, dict) else None
        bed_target = printer.get("target_bed") if isinstance(printer, dict) else None
        chamber_actual = printer.get("temp_chamber") if isinstance(printer, dict) else None
        chamber_target = printer.get("target_chamber") if isinstance(printer, dict) else None

        # Flow-anomaly cross-check — fire on TRANSITION INTO ATTENTION /
        # ERROR (with a filament-path message hint) and feed the signal
        # into the kiln-pro nozzle wear cross-check.
        #
        # Transition-only firing — _prior_state guards against
        # steady-state ATTENTION flooding the wear-signal log every
        # poll cycle.  Free-tier installs without kiln-pro silently
        # skip via try/except ImportError.
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
        """Feed a transition-into-anomaly state to the kiln-pro wear cross-check.

        Only fires when *current* is a flow-anomaly state, *prior* was
        not (or was unknown), and the printer's status / error message
        text implicates the filament path.  Steady-state anomalies
        (same anomaly state across two consecutive polls) are skipped
        so the wear-signal log isn't flooded.

        Silent on missing kiln-pro (free tier); silent on any
        unexpected error inside the recorder so a flow-anomaly signal
        never breaks the status-poll happy path.
        """
        current_upper = (current or "").strip().upper()
        prior_upper = (prior or "").strip().upper()

        # Suppress steady-state and non-transition cases.
        if current_upper not in _ANOMALY_STATES:
            return
        if prior_upper in _ANOMALY_STATES:
            return

        # Pull the message text from whichever surface the firmware
        # populated.  Prusa Link doesn't standardize this across MK4 /
        # XL / MMU3 firmwares — try the common locations and join
        # whatever we find so the classifier sees the union.
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
        try:
            from kiln_pro.nozzle_intelligence.sensor_signal import (
                record_extrusion_event_for_printer,
            )

            record_extrusion_event_for_printer(
                printer_id=self.name,
                event_type=event_type,
                severity=severity,
            )
        except ImportError:
            # Free tier — kiln-pro nozzle module not installed.  Drop
            # the signal silently.
            pass
        except Exception as exc:  # pragma: no cover
            logger.debug(
                "Flow-anomaly cross-check raised (non-fatal): %s",
                exc,
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

        # ``job.id`` is the server-assigned handle DELETE/pause/resume already
        # take -- it is in this very payload, so surfacing it costs no request.
        job_id = job.get("id")

        return JobProgress(
            file_name=None,  # Prusa Link doesn't include filename in status
            job_id=str(job_id) if job_id is not None else None,
            completion=completion,
            print_time_seconds=print_time_seconds,
            print_time_left_seconds=print_time_left_seconds,
        )

    def list_files(self) -> list[PrinterFile]:
        """Return a list of G-code files across supported Prusa storage roots.

        Tries ``/api/v1/files/usb`` first, then falls back to ``/api/v1/files/local``.
        """
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
        """Begin printing a file on the printer.

        Resolves display names to API-safe file paths, then attempts
        ``POST /api/v1/files/usb/<file_path>`` first, falling back to ``local``.
        """
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
            ) from None

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job.

        Calls ``PUT /api/v1/job/<id>/pause``.
        """
        job_id = self._get_active_job_id()
        if job_id is None:
            raise PrinterError("No active job to pause.")

        self._request("PUT", f"/api/v1/job/{job_id}/pause")
        return PrintResult(success=True, message="Print paused.")

    def _resume_print_impl(self) -> PrintResult:
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

    def send_gcode(self, commands: list[str]) -> bool:
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
