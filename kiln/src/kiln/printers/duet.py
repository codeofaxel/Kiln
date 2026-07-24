"""Duet / RepRapFirmware adapter for the Kiln printer abstraction layer.

Implements :class:`~kiln.printers.base.PrinterAdapter` by talking to the
RepRapFirmware HTTP interface -- the same API that Duet Web Control uses --
via :mod:`requests`.  This is the interface exposed by Duet 2 / Duet 3
controller boards, and by the machines built around them.

The adapter mirrors the retry and error-handling patterns established by
:class:`~kiln.printers.moonraker.MoonrakerAdapter`.

Both firmware generations are supported, and the difference is explicit:

* **RRF 3** (``rr_model``) -- the object model.  This is the primary path.
  ``rr_status`` is deprecated in RRF 3.0 and slated for removal in RRF 3.6,
  so a modern board must not be driven through it.
* **RRF 2** (``rr_status``) -- the legacy polled status response.  RRF 2 has
  no object model, so this is the only option there.

The generation is detected once per adapter instance (see
:meth:`DuetAdapter._generation`) and cached; every other difference between
the two is confined to :meth:`get_state` and :meth:`get_job`.  Print control,
file transfer and G-code are identical on both.

Sources consulted when writing this adapter (all opened directly):

* ``Developer-documentation/OpenAPI.yaml`` in Duet3D/RepRapFirmware at
  ``3.6-dev`` -- the machine-readable definition of every ``rr_*`` endpoint,
  its parameters and its response fields.
* https://github.com/Duet3D/RepRapFirmware/wiki/HTTP-requests -- session
  handling, the 401 contract, and the ``X-Session-Key`` header.
* ``src/Platform/RepRap.cpp`` -- the authoritative status tables (see
  :data:`_RRF3_STATUS_MAP` / :data:`_RRF2_STATUS_MAP`).
* ``src/GCodes/GCodes2.cpp`` -- ``M0`` semantics (see :meth:`cancel_print`).
* ``src/GCodes/GCodeBuffer/StringParser.cpp`` -- quoted-string escaping
  (see :func:`_escape_rrf_string`).
* ``src/PrintMonitor/PrintMonitor.cpp`` and ``src/Heating/Heat.cpp`` --
  the ``job`` and ``heat`` object-model key names.
"""

from __future__ import annotations

import logging
import os
import time
import zlib
from datetime import datetime, timezone
from typing import Any

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

# HTTP status codes eligible for automatic retry.  RepRapFirmware returns 503
# when it is temporarily short on RAM to build a response -- the OpenAPI
# definition documents this on rr_status, rr_filelist and rr_model -- so 503
# is a retry, never a hard failure.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({502, 503, 504})

# Maximum safe temperatures (fallback used only when no safety profile is
# bound).  These are deliberately higher than the desktop-class adapters:
# Duet boards are the controller of choice for high-temperature industrial
# machines, whose hotends run well past the 300 C that a desktop backend can
# assume.  :meth:`PrinterAdapter._validate_temp` takes ``min()`` of this value
# and the bound safety profile, so a per-printer profile always narrows the
# window -- it can never widen it.  Setting this ceiling too low would make
# the adapter structurally unable to drive the machines it exists for.
_MAX_HOTEND_TEMP: float = 500.0
_MAX_BED_TEMP: float = 200.0

# RepRapFirmware's virtual SD volume 0.  Job files live here; this is the
# directory Duet Web Control lists and the one M32 resolves against.
_GCODE_DIR: str = "0:/gcodes"

# How long cancel_print waits for a pause to take effect before giving up.
# See cancel_print for why the pause is mandatory.
_PAUSE_SETTLE_TIMEOUT_S: float = 15.0
_PAUSE_POLL_INTERVAL_S: float = 0.5

# Object-model status strings, from RepRap::GetStatusString() in
# src/Platform/RepRap.cpp.  The firmware builds both representations from one
# index, so the two maps below are the same 13 states in the same order.
_RRF3_STATUS_MAP: dict[str, PrinterStatus] = {
    "starting": PrinterStatus.BUSY,  # reading config.g
    "updating": PrinterStatus.BUSY,  # flashing firmware
    "halted": PrinterStatus.ERROR,  # after an emergency stop (M112)
    "off": PrinterStatus.OFFLINE,
    "pausing": PrinterStatus.BUSY,  # decelerating; not yet stopped
    "resuming": PrinterStatus.BUSY,
    "paused": PrinterStatus.PAUSED,
    "cancelling": PrinterStatus.CANCELLING,
    # A simulated job runs the file without moving or extruding.  Reporting it
    # as PRINTING would tell a watching user a part is being made when none is.
    "simulating": PrinterStatus.BUSY,
    "processing": PrinterStatus.PRINTING,
    "changingTool": PrinterStatus.BUSY,
    "busy": PrinterStatus.BUSY,
    "idle": PrinterStatus.IDLE,
}

# Legacy single-character status codes, from RepRap::GetStatusCharacter() in
# the same file: the literal is "CFHODRSAMPTBI", indexed identically to the
# strings above.  Note that 'A' (cancelling) and 'I' (idle) are absent from
# the rendered wiki table but present in the firmware -- the source is the
# source of truth here.
_RRF2_STATUS_MAP: dict[str, PrinterStatus] = {
    "C": PrinterStatus.BUSY,  # starting / reading configuration
    "F": PrinterStatus.BUSY,  # flashing firmware
    "H": PrinterStatus.ERROR,  # halted
    "O": PrinterStatus.OFFLINE,  # off
    "D": PrinterStatus.BUSY,  # pausing (decelerating)
    "R": PrinterStatus.BUSY,  # resuming
    "S": PrinterStatus.PAUSED,  # stopped / paused
    "A": PrinterStatus.CANCELLING,
    "M": PrinterStatus.BUSY,  # simulating
    "P": PrinterStatus.PRINTING,
    "T": PrinterStatus.BUSY,  # changing tool
    "B": PrinterStatus.BUSY,
    "I": PrinterStatus.IDLE,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    """Coerce *value* to ``float``, returning ``None`` when it isn't numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _escape_rrf_string(value: str) -> str:
    """Escape *value* for use inside a double-quoted RepRapFirmware string.

    ``StringParser::InternalGetQuotedString`` in the firmware defines exactly
    two escapes inside a quoted string, and both matter here:

    * A ``"`` ends the string unless it is doubled.
    * A ``'`` immediately before an alphabetic character *forces that
      character to lower case*; doubling the ``'`` yields a literal one.

    The second rule is the subtle one -- an un-escaped apostrophe in a
    filename does not fail loudly, it silently lower-cases the next character
    and the print then fails on a file that "exists".  Duet Web Control's own
    ``escapeFilename`` doubles the apostrophe for this reason; we additionally
    double the double-quote so a filename containing one cannot truncate the
    command.

    Raises:
        PrinterError: If *value* contains a control character, which the
            firmware's parser rejects outright.
    """
    if any(ch < " " for ch in value):
        raise PrinterError(
            f"Filename {value!r} contains a control character, which "
            "RepRapFirmware's G-code parser rejects. Rename the file."
        )
    return value.replace("'", "''").replace('"', '""')


def _qualify(file_name: str) -> str:
    """Return *file_name* as an absolute path on the printer's SD volume.

    Bare names are resolved against :data:`_GCODE_DIR`, matching where Duet
    Web Control stores and lists job files.  Anything already carrying a
    volume prefix (``0:/``) or a leading slash is passed through untouched.
    """
    name = file_name.strip()
    if name.startswith("/") or ":" in name.split("/", 1)[0]:
        return name
    return f"{_GCODE_DIR}/{name}"


def _parse_rrf_datetime(value: Any) -> int | None:
    """Convert an RRF ISO8601-like timestamp to a Unix timestamp.

    RepRapFirmware emits local time with no zone suffix (e.g.
    ``"2026-07-24T14:03:11"``).  There is no way to recover the board's
    offset from the string alone, so it is read as UTC; the value is used
    only for display ordering, never for control decisions.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


class DuetAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by the RepRapFirmware HTTP API.

    Args:
        host: Base URL of the board's web interface, e.g.
            ``"http://duet.local"`` or ``"http://192.168.1.60"``.  RRF serves
            HTTP on port 80, so a port is usually unnecessary.
        password: Machine password set by ``M551``.  RepRapFirmware's own
            default is ``"reprap"``, which is what a board with no password
            configured accepts, so it is the default here too.
        timeout: Per-request timeout in seconds.
        retries: Maximum number of attempts for transient failures
            (connection errors and HTTP 502/503/504).

    Raises:
        ValueError: If *host* is empty.

    Example::

        adapter = DuetAdapter("http://duet.local")
        state = adapter.get_state()
        print(state.state, state.tool_temp_actual)
    """

    def __init__(
        self,
        host: str,
        password: str = "reprap",
        timeout: int = 30,
        retries: int = 3,
        verify_ssl: bool = True,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")

        self._host: str = host.rstrip("/")
        self._password: str = password
        self._timeout: int = timeout
        self._retries: int = max(retries, 1)

        self._session: requests.Session = requests.Session()
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

        # Session state.  ``_connected`` tracks whether rr_connect has
        # succeeded since the last 401; ``_rrf_generation`` caches firmware
        # detection (2 or 3) so it costs one probe per adapter, not per call.
        self._connected: bool = False
        self._rrf_generation: int | None = None

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return "duet"

    @property
    def capabilities(self) -> PrinterCapabilities:
        """Capabilities supported by the Duet / RepRapFirmware backend."""
        return PrinterCapabilities(
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
            can_pause=True,
            # RRF has no built-in camera, and no bed-mesh, filament-monitor or
            # firmware-update reader is implemented here -- claiming these
            # would advertise behaviour this adapter does not have.
            can_stream=False,
            can_probe_bed=False,
            can_update_firmware=False,
            can_snapshot=False,
            can_detect_filament=False,
            supported_extensions=(".gcode", ".gco", ".g"),
        )

    # ------------------------------------------------------------------
    # Session handling
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        """Build a fully-qualified URL from a relative API path."""
        return f"{self._host}{path}"

    def _connect(self) -> None:
        """Establish an HTTP session with the board via ``rr_connect``.

        ``rr_connect`` is the only endpoint that does not require a session,
        so it is issued directly rather than through :meth:`_request` -- which
        would otherwise recurse when it tried to authenticate a 401.

        Boards from RRF 3.5-beta4 onward return a ``sessionKey`` that must be
        echoed in the ``X-Session-Key`` header on every subsequent request.
        Older boards key the session off the client IP instead and return no
        such field; sending the request parameter to them is harmless, so one
        code path serves both.

        Raises:
            PrinterError: If the password is rejected, the board has no free
                session slots, or it cannot be reached.
        """
        try:
            response = self._session.get(
                self._url("/rr_connect"),
                params={
                    "password": self._password,
                    "sessionKey": "yes",
                    "time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                },
                timeout=self._timeout,
            )
        except (Timeout, ReqConnectionError) as exc:
            raise PrinterError(
                f"Could not connect to the Duet board at {self._host}. "
                "Check the address and that the board is powered and on the network.",
                cause=exc,
            ) from exc
        except RequestException as exc:
            raise PrinterError(
                f"Request error connecting to {self._host}: {exc}",
                cause=exc,
            ) from exc

        try:
            body = response.json()
        except ValueError:
            body = {}

        err = body.get("err")
        if err == 1:
            raise PrinterError(
                "The Duet board rejected the password. Set the correct machine "
                "password (configured with M551 in config.g), or clear it to use "
                "the default."
            )
        if err == 2:
            raise PrinterError(
                "The Duet board has no free session slots. Close an open Duet Web "
                "Control tab, or wait for an idle session to time out, then retry."
            )

        session_key = body.get("sessionKey")
        if session_key is not None:
            self._session.headers["X-Session-Key"] = str(session_key)

        self._connected = True
        logger.debug(
            "Connected to Duet at %s (board=%s, session key=%s)",
            self._host,
            body.get("boardType"),
            "yes" if session_key is not None else "no",
        )

    def _ensure_session(self) -> None:
        """Connect if this adapter has no live session."""
        if not self._connected:
            self._connect()

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        """Execute an HTTP request with retry and transparent re-authentication.

        Every endpoint except ``rr_connect`` answers 401 when the session has
        expired.  Sessions time out on their own (the board reports
        ``sessionTimeout`` at connect time), so a long-lived adapter *will*
        meet a 401 in normal operation.  That is recoverable without the user
        doing anything, so a 401 reconnects and replays the request once
        rather than surfacing an error.

        Returns the :class:`requests.Response` on success (2xx).

        Raises:
            PrinterError: On non-retryable HTTP errors, connection failures,
                timeouts, or when all retry attempts are exhausted.
        """
        self._ensure_session()

        url = self._url(path)
        last_exc: Exception | None = None
        reauthed = False
        attempt = 0

        while attempt < self._retries:
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=self._timeout,
                )

                if response.ok:
                    return response

                if response.status_code == 401 and not reauthed:
                    # Session expired -- re-authenticate and replay.  This
                    # deliberately does not consume a retry attempt: expiry is
                    # routine, not a failure, and an adapter configured with
                    # retries=1 must still recover from it.  ``reauthed`` caps
                    # this at one replay, so it cannot loop.
                    logger.debug("Duet session expired on %s; reconnecting", path)
                    self._connected = False
                    reauthed = True
                    self._connect()
                    continue

                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    body = response.text[:300]
                    if len(response.text) > 300:
                        body += " (truncated)"
                    sc = response.status_code
                    if sc == 401:
                        hint = (
                            " Re-authentication did not restore the session. Check the "
                            "machine password (M551 in config.g). Retry with `get_state()`."
                        )
                    elif sc == 404:
                        hint = (
                            " Endpoint or file not found. Verify with `list_files()`;"
                            " very old RepRapFirmware builds may not serve this request."
                        )
                    else:
                        hint = " Retry with `get_state()` to check printer status."
                    error = PrinterError(
                        f"Duet returned HTTP {sc} for {method} {path}: {body}.{hint}",
                    )
                    # Carried so callers can tell an access refusal apart from
                    # an endpoint that genuinely is not served -- see
                    # _generation, where confusing the two would blame the
                    # wrong thing.
                    error.status_code = sc  # type: ignore[attr-defined]
                    raise error

                # Retryable HTTP status -- fall through to backoff.  RRF answers
                # 503 when it is momentarily short of RAM to build a response.
                last_exc = PrinterError(
                    f"Duet returned HTTP {response.status_code} "
                    f"for {method} {path} "
                    f"(attempt {attempt + 1}/{self._retries}). "
                    f"The board may be busy or low on memory. "
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
                    f"Could not connect to the Duet board at {self._host} "
                    f"(attempt {attempt + 1}/{self._retries})",
                    cause=exc,
                )
            except RequestException as exc:
                raise PrinterError(
                    f"Request error for {method} {path}: {exc}. "
                    f"Cannot reach the Duet board at {self._host}. "
                    f"Check network connectivity. Retry with `get_state()`.",
                    cause=exc,
                ) from exc

            attempt += 1

            # Exponential backoff: 1 s, 2 s, 4 s, ...
            if attempt < self._retries:
                backoff = 2 ** (attempt - 1)
                logger.debug(
                    "Retrying %s %s in %ds (attempt %d/%d)",
                    method,
                    path,
                    backoff,
                    attempt,
                    self._retries,
                )
                time.sleep(backoff)

        if last_exc is None:  # pragma: no cover - defensive
            last_exc = PrinterError(f"{method} {path} failed with no recorded cause.")
        raise last_exc

    def _get_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Shorthand: GET *path* and return the parsed JSON body.

        Raises :class:`PrinterError` if the response body is not valid JSON.
        """
        response = self._request("GET", path, **kwargs)
        try:
            body = response.json()
        except ValueError as exc:
            raise PrinterError(
                f"Invalid JSON in response from GET {path}",
                cause=exc,
            ) from exc
        if not isinstance(body, dict):
            raise PrinterError(f"Unexpected non-object JSON in response from GET {path}")
        return body

    # ------------------------------------------------------------------
    # Firmware generation
    # ------------------------------------------------------------------

    @staticmethod
    def _is_access_failure(exc: PrinterError) -> bool:
        """True when *exc* means "could not reach or authenticate", not "absent".

        Firmware detection infers RRF 2 from rr_model being unavailable, so it
        must not draw that inference from a network drop or a refused
        session -- both would otherwise be reported to the user as "this is
        not a Duet board", pointing them at the wrong problem entirely.
        """
        if isinstance(exc.cause, (ReqConnectionError, Timeout)):
            return True
        return getattr(exc, "status_code", None) == 401

    def _generation(self) -> int:
        """Return the firmware generation (``3`` or ``2``), detecting it once.

        ``rr_model`` exists only in RRF 3 and later, so a successful object
        model query is the discriminator.  RRF 2 boards do not serve the
        endpoint; they answer ``rr_status`` instead.

        Raises:
            PrinterError: If neither interface answers, which means the host
                is reachable but is not running RepRapFirmware.
        """
        if self._rrf_generation is not None:
            return self._rrf_generation

        # Authenticate up front.  Without this, a rejected password or an
        # unreachable board surfaces from the probe below and gets reported as
        # "this is not a Duet" -- which sends the user to check the wrong
        # thing entirely.  Connection and auth errors must be reported as
        # themselves; only a genuine "endpoint absent" answer means RRF 2.
        self._ensure_session()

        try:
            body = self._get_json(
                "/rr_model", params={"key": "state.status", "flags": "d99"}
            )
            if "result" in body:
                self._rrf_generation = 3
                logger.debug("Detected RepRapFirmware 3 (object model) at %s", self._host)
                return 3
        except PrinterError as exc:
            if self._is_access_failure(exc):
                raise
            logger.debug("rr_model unavailable at %s; probing rr_status", self._host)

        try:
            self._get_json("/rr_status", params={"type": 1})
        except PrinterError as exc:
            if self._is_access_failure(exc):
                raise
            raise PrinterError(
                f"{self._host} answered, but neither the RepRapFirmware object model "
                "(rr_model) nor the legacy status endpoint (rr_status) responded. "
                "This does not look like a Duet / RepRapFirmware controller -- check "
                "the address, and that you have not pointed Kiln at a different "
                "printer's web interface.",
                cause=exc,
            ) from exc

        self._rrf_generation = 2
        logger.debug("Detected RepRapFirmware 2 (legacy status) at %s", self._host)
        return 2

    def _model(self, key: str) -> Any:
        """Query one object-model *key* (RRF 3 only) and return its ``result``."""
        body = self._get_json("/rr_model", params={"key": key, "flags": "d99"})
        return body.get("result")

    # ------------------------------------------------------------------
    # G-code transport
    # ------------------------------------------------------------------

    def _run_gcode(self, gcode: str) -> str:
        """Execute *gcode* and return whatever the firmware replied.

        ``rr_gcode`` reports only remaining buffer space; a command that the
        firmware *rejects* still returns HTTP 200 there, and the reason is
        left in the reply buffer for ``rr_reply`` to collect.  Reading that
        reply is what turns a silent no-op into a real error -- see
        :meth:`cancel_print` for the case that makes this indispensable.

        Raises:
            PrinterError: If the firmware answered with an error.
        """
        self._request("GET", "/rr_gcode", params={"gcode": gcode})

        # The reply is best-effort: it is buffered per client and discarded
        # once every client has read it, so an empty reply is normal and is
        # never treated as a failure.
        try:
            reply = self._request("GET", "/rr_reply").text.strip()
        except PrinterError:
            logger.debug("Could not read rr_reply after %r", gcode, exc_info=True)
            return ""

        if reply.lower().startswith("error"):
            raise PrinterError(f"The printer rejected {gcode!r}: {reply}")
        if reply.lower().startswith("warning"):
            logger.warning("Duet reported a warning for %r: %s", gcode, reply)
        return reply

    # ------------------------------------------------------------------
    # PrinterAdapter -- state queries
    # ------------------------------------------------------------------

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures.

        On RRF 3 this issues two object-model queries (``state.status`` and
        ``heat``); on RRF 2 a single ``rr_status?type=1``.

        Returns an OFFLINE state when the board is unreachable rather than
        raising, so callers always get a usable :class:`PrinterState`.

        Raises:
            PrinterError: On unexpected (non-connection) errors.
        """
        try:
            generation = self._generation()
        except PrinterError as exc:
            if exc.cause is not None and isinstance(exc.cause, (ReqConnectionError, Timeout)):
                return PrinterState(connected=False, state=PrinterStatus.OFFLINE)
            raise

        try:
            if generation == 3:
                return self._state_rrf3()
            return self._state_rrf2()
        except PrinterError as exc:
            if exc.cause is not None and isinstance(exc.cause, (ReqConnectionError, Timeout)):
                return PrinterState(connected=False, state=PrinterStatus.OFFLINE)
            raise

    def _state_rrf3(self) -> PrinterState:
        """Build a :class:`PrinterState` from the RRF 3 object model."""
        status_raw = self._model("state.status")
        state = _RRF3_STATUS_MAP.get(
            status_raw if isinstance(status_raw, str) else "", PrinterStatus.UNKNOWN
        )

        heat = self._model("heat")
        heaters = heat.get("heaters") if isinstance(heat, dict) else None
        if not isinstance(heaters, list):
            return PrinterState(connected=True, state=state)

        bed_idx = self._first_index(heat, "bedHeaters", len(heaters))
        chamber_idx = self._first_index(heat, "chamberHeaters", len(heaters))

        # The tool heater is not named in the `heat` key, and asking the
        # `tools` key would cost another round-trip.  Every heater that is not
        # a bed or a chamber is a tool heater, so the first such entry is the
        # active hotend -- derived from the response we already have.
        tool_idx: int | None = None
        for index in range(len(heaters)):
            if index not in (bed_idx, chamber_idx):
                tool_idx = index
                break

        def _heater(index: int | None, field: str) -> float | None:
            if index is None or not isinstance(heaters[index], dict):
                return None
            return _as_float(heaters[index].get(field))

        return PrinterState(
            connected=True,
            state=state,
            tool_temp_actual=_heater(tool_idx, "current"),
            tool_temp_target=_heater(tool_idx, "active"),
            bed_temp_actual=_heater(bed_idx, "current"),
            bed_temp_target=_heater(bed_idx, "active"),
            chamber_temp_actual=_heater(chamber_idx, "current"),
            chamber_temp_target=_heater(chamber_idx, "active"),
        )

    @staticmethod
    def _first_index(heat: Any, key: str, heater_count: int) -> int | None:
        """Return the first valid heater index listed under *key*.

        ``heat.bedHeaters`` / ``heat.chamberHeaters`` are arrays of indices
        into ``heat.heaters``, and use ``-1`` for "not configured".
        """
        values = heat.get(key) if isinstance(heat, dict) else None
        if not isinstance(values, list):
            return None
        for value in values:
            if isinstance(value, int) and 0 <= value < heater_count:
                return value
        return None

    def _state_rrf2(self) -> PrinterState:
        """Build a :class:`PrinterState` from the legacy ``rr_status`` response."""
        payload = self._get_json("/rr_status", params={"type": 1})

        status_raw = payload.get("status")
        state = _RRF2_STATUS_MAP.get(
            status_raw if isinstance(status_raw, str) else "", PrinterStatus.UNKNOWN
        )

        temps = payload.get("temps")
        if not isinstance(temps, dict):
            return PrinterState(connected=True, state=state)

        bed = temps.get("bed") if isinstance(temps.get("bed"), dict) else {}
        chamber = temps.get("chamber") if isinstance(temps.get("chamber"), dict) else {}

        # As on RRF 3, the tool heater is the first heater that is neither the
        # bed nor the chamber.  Here the bed/chamber blocks carry their own
        # `heater` index into the `current` array.
        current = temps.get("current")
        reserved = {
            idx
            for idx in (bed.get("heater"), chamber.get("heater"))
            if isinstance(idx, int)
        }
        tool_actual: float | None = None
        if isinstance(current, list):
            for index, value in enumerate(current):
                if index not in reserved:
                    tool_actual = _as_float(value)
                    break

        # Tool targets are a per-tool array of per-heater arrays.
        tool_target: float | None = None
        tools = temps.get("tools")
        active = tools.get("active") if isinstance(tools, dict) else None
        if isinstance(active, list) and active and isinstance(active[0], list) and active[0]:
            tool_target = _as_float(active[0][0])

        return PrinterState(
            connected=True,
            state=state,
            tool_temp_actual=tool_actual,
            tool_temp_target=tool_target,
            bed_temp_actual=_as_float(bed.get("current")),
            bed_temp_target=_as_float(bed.get("active")),
            chamber_temp_actual=_as_float(chamber.get("current")),
            chamber_temp_target=_as_float(chamber.get("active")),
        )

    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active (or last) print job.

        Raises:
            PrinterError: On communication or parsing errors.
        """
        if self._generation() == 3:
            return self._job_rrf3()
        return self._job_rrf2()

    def _job_rrf3(self) -> JobProgress:
        """Read job progress from the RRF 3 ``job`` object-model key."""
        job = self._model("job")
        if not isinstance(job, dict):
            return JobProgress()

        file_info = job.get("file") if isinstance(job.get("file"), dict) else {}
        file_name = file_info.get("fileName") or job.get("lastFileName")

        # RRF reports the byte offset into the file, not a percentage.
        completion: float | None = None
        position = _as_float(job.get("filePosition"))
        size = _as_float(file_info.get("size"))
        if position is not None and size is not None and size > 0:
            completion = round(min(position / size * 100.0, 100.0), 2)

        duration = _as_float(job.get("duration"))

        # timesLeft carries three independent estimates.  The slicer's own
        # estimate is the most accurate when the file provides one; the
        # file-progress estimate is the fallback that always exists.
        times_left = job.get("timesLeft") if isinstance(job.get("timesLeft"), dict) else {}
        remaining: float | None = None
        for source in ("slicer", "file", "filament"):
            remaining = _as_float(times_left.get(source))
            if remaining is not None:
                break

        layer = job.get("layer")
        num_layers = file_info.get("numLayers")

        return JobProgress(
            file_name=str(file_name) if file_name else None,
            completion=completion,
            print_time_seconds=int(duration) if duration is not None else None,
            print_time_left_seconds=int(remaining) if remaining is not None else None,
            current_layer=layer if isinstance(layer, int) else None,
            total_layers=num_layers if isinstance(num_layers, int) else None,
        )

    def _job_rrf2(self) -> JobProgress:
        """Read job progress from the legacy ``rr_status?type=3`` response.

        The print status response carries progress but not the filename, so
        this also calls ``rr_fileinfo`` with no ``name`` -- which the firmware
        answers with information about the file currently being printed.
        """
        payload = self._get_json("/rr_status", params={"type": 3})

        completion = _as_float(payload.get("fractionPrinted"))
        duration = _as_float(payload.get("printDuration"))

        times_left = (
            payload.get("timesLeft") if isinstance(payload.get("timesLeft"), dict) else {}
        )
        remaining: float | None = None
        for source in ("file", "filament", "layer"):
            remaining = _as_float(times_left.get(source))
            if remaining is not None:
                break

        layer = payload.get("currentLayer")

        file_name: str | None = None
        total_layers: int | None = None
        try:
            info = self._get_json("/rr_fileinfo")
            if info.get("err") == 0:
                raw_name = info.get("fileName")
                file_name = str(raw_name) if raw_name else None
                if isinstance(info.get("numLayers"), int):
                    total_layers = info["numLayers"]
        except PrinterError:
            # No job loaded, or an older build without rr_fileinfo: progress
            # is still worth returning without the filename.
            logger.debug("rr_fileinfo unavailable", exc_info=True)

        return JobProgress(
            file_name=file_name,
            completion=round(completion, 2) if completion is not None else None,
            print_time_seconds=int(duration) if duration is not None else None,
            print_time_left_seconds=int(remaining) if remaining is not None else None,
            current_layer=layer if isinstance(layer, int) else None,
            total_layers=total_layers,
        )

    def list_files(self) -> list[PrinterFile]:
        """Return the G-code files stored on the printer's SD volume.

        ``rr_filelist`` is paginated: it returns as many entries as fit in the
        response buffer and reports the index to resume from in ``next``,
        which is ``0`` once the listing is complete.  A single unpaginated
        request silently truncates on any board with a full SD card, so this
        follows the cursor to the end.

        Raises:
            PrinterError: On communication errors, or if the directory is
                missing or the SD card is not mounted.
        """
        results: list[PrinterFile] = []
        first = 0

        while True:
            body = self._get_json(
                "/rr_filelist", params={"dir": _GCODE_DIR, "first": first}
            )

            err = body.get("err")
            if err == 1:
                raise PrinterError(
                    "The printer's SD card is not mounted, so its files cannot be "
                    "listed. Check the card is seated, then retry."
                )
            if err == 2:
                raise PrinterError(
                    f"The directory {_GCODE_DIR} does not exist on the printer. "
                    "Upload a file first, or check the SD card layout."
                )

            entries = body.get("files")
            if not isinstance(entries, list):
                break

            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "f":
                    continue  # skip directories
                name = entry.get("name")
                if not name:
                    continue
                size = entry.get("size")
                results.append(
                    PrinterFile(
                        name=str(name),
                        path=f"{_GCODE_DIR}/{name}",
                        size_bytes=size if isinstance(size, int) else None,
                        date=_parse_rrf_datetime(entry.get("date")),
                    )
                )

            next_index = body.get("next")
            if not isinstance(next_index, int) or next_index == 0:
                break  # 0 means the listing is complete
            if next_index <= first:
                # A cursor that does not advance would loop forever; stop and
                # return what we have rather than hang.
                logger.warning(
                    "rr_filelist cursor did not advance (first=%d, next=%d); "
                    "returning %d entries",
                    first,
                    next_index,
                    len(results),
                )
                break
            first = next_index

        return results

    # ------------------------------------------------------------------
    # PrinterAdapter -- file management
    # ------------------------------------------------------------------

    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a local G-code file to the printer's SD card.

        ``rr_upload`` is the only POST the firmware serves, and it takes the
        file as a raw body -- no multipart, no encapsulation -- with the
        destination in the query string.  ``Content-Length`` must be set
        explicitly: it both tells the firmware how much to expect and stops
        :mod:`requests` from falling back to chunked transfer encoding, which
        RRF does not accept.

        A CRC32 of the content is sent alongside so the board can verify what
        it received; the firmware reports a mismatch as an upload error rather
        than leaving a corrupt file to fail mid-print.

        Args:
            file_path: Absolute or relative path to the local file.

        Raises:
            PrinterError: On communication errors or a rejected upload.
            FileNotFoundError: If *file_path* does not exist locally.
        """
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Local file not found: {abs_path}")

        filename = os.path.basename(abs_path)
        size = os.path.getsize(abs_path)

        # Stream the checksum so an arbitrarily large job file never has to be
        # held in memory in full.
        try:
            checksum = 0
            with open(abs_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    checksum = zlib.crc32(chunk, checksum)
        except PermissionError as exc:
            raise PrinterError(
                f"Permission denied reading file: {abs_path}",
                cause=exc,
            ) from exc

        target = f"{_GCODE_DIR}/{filename}"
        modified = datetime.fromtimestamp(os.path.getmtime(abs_path)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )

        with open(abs_path, "rb") as fh:
            response = self._request(
                "POST",
                "/rr_upload",
                params={
                    "name": target,
                    "time": modified,
                    "crc32": format(checksum & 0xFFFFFFFF, "08x"),
                },
                data=fh,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                },
            )

        try:
            body = response.json()
        except ValueError:
            body = {}

        if body.get("err") != 0:
            raise PrinterError(
                f"The printer rejected the upload of {filename}. This is usually a "
                "checksum mismatch or a full SD card. Check free space and retry."
            )

        return UploadResult(
            success=True,
            file_name=target,
            message=f"Uploaded {filename} to {target}.",
        )

    def delete_file(self, file_path: str) -> bool:
        """Delete a G-code file from the printer's SD card.

        Args:
            file_path: Filename, or a full path on the printer's volume.

        Returns:
            ``True`` if the file was deleted.

        Raises:
            PrinterError: If the deletion fails.
        """
        target = _qualify(file_path)
        body = self._get_json("/rr_delete", params={"name": target})
        if body.get("err") != 0:
            raise PrinterError(
                f"Could not delete {target} from the printer. It may not exist, or "
                "may be the file currently being printed. Check `list_files()`."
            )
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- print control
    # ------------------------------------------------------------------

    def _start_print_impl(self, file_name: str, **_kwargs: Any) -> PrintResult:
        """Begin printing a file that already exists on the printer.

        Sends ``M32 "<path>"``, which is what Duet Web Control issues to start
        a job.  The universal pre-print gate runs in the base
        :meth:`~kiln.printers.base.PrinterAdapter.start_print` template before
        this is reached.

        Args:
            file_name: Name (or path) of the file as known by the printer.

        Raises:
            PrinterError: If the printer cannot start the job.
        """
        target = _qualify(file_name)
        self._run_gcode(f'M32 "{_escape_rrf_string(target)}"')
        return PrintResult(success=True, message=f"Started printing {target}.")

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job (``M25``).

        Raises:
            PrinterError: If the printer cannot pause.
        """
        self._run_gcode("M25")
        return PrintResult(success=True, message="Print paused.")

    def _resume_print_impl(self) -> PrintResult:
        """Resume a previously paused print job (``M24``).

        The not-paused gate runs in the base :meth:`resume_print` template, so
        the job is already known to be paused (or the state was uncertain and
        the gate failed open) by the time this runs.

        Raises:
            PrinterError: If the printer cannot resume.
        """
        self._run_gcode("M24")
        return PrintResult(success=True, message="Print resumed.")

    def cancel_print(self) -> PrintResult:
        """Cancel the currently running print job.

        RepRapFirmware will only cancel a job that is **already paused**.  In
        ``GCodes2.cpp``, ``M0`` arriving on a non-file channel -- which is what
        an HTTP request is -- cancels only when ``pauseState`` is ``paused``,
        and otherwise replies ``"Pause the print before attempting to cancel
        it"``.  Sending a bare ``M0`` mid-print therefore looks like it worked
        and does nothing; Duet Web Control hides its own cancel button until
        the job is paused for exactly this reason.

        So this pauses first, waits for the pause to actually take effect --
        ``M25`` starts a deceleration and a ``pause.g`` macro, and the state
        passes through ``pausing`` before reaching ``paused`` -- and only then
        cancels.

        Raises:
            PrinterError: If the printer cannot be paused within
                :data:`_PAUSE_SETTLE_TIMEOUT_S`, or the cancel is refused.
        """
        state = self.get_state()

        if state.state in (PrinterStatus.IDLE, PrinterStatus.OFFLINE):
            return PrintResult(success=True, message="No active print job to cancel.")

        if state.state != PrinterStatus.PAUSED:
            self._run_gcode("M25")
            if not self._wait_for_paused():
                raise PrinterError(
                    "The print did not reach a paused state within "
                    f"{int(_PAUSE_SETTLE_TIMEOUT_S)}s, so it was not cancelled. "
                    "RepRapFirmware only cancels a paused job. The printer may be "
                    "running a long pause macro -- check `get_state()` and retry."
                )

        self._run_gcode("M0")
        return PrintResult(success=True, message="Print cancelled.")

    def _wait_for_paused(self) -> bool:
        """Poll until the job is paused, or the settle timeout expires."""
        deadline = time.monotonic() + _PAUSE_SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(_PAUSE_POLL_INTERVAL_S)
            status = self.get_state().state
            if status == PrinterStatus.PAUSED:
                return True
            if status in (PrinterStatus.IDLE, PrinterStatus.OFFLINE, PrinterStatus.ERROR):
                # The job ended on its own, or the board faulted -- either way
                # there is nothing left to pause.
                return False
        return False

    def emergency_stop(self) -> PrintResult:
        """Perform an immediate emergency stop (``M112``).

        ``M112`` halts motion and heaters at the firmware level; the board
        stays halted until it is reset.  Duet Web Control's emergency button
        sends ``M112`` and ``M999`` together so its one button both stops and
        recovers the board -- this adapter deliberately sends only ``M112``,
        because Kiln exposes clearing an emergency stop as its own separate,
        deliberate action.  Auto-resetting here would un-latch a safety stop
        the instant it was triggered.
        """
        # A halted board may not answer rr_reply, so this bypasses the reply
        # check that _run_gcode performs: the stop must go out regardless.
        self._request("GET", "/rr_gcode", params={"gcode": "M112"})
        return PrintResult(
            success=True,
            message="Emergency stop triggered. The board stays halted until it is reset.",
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- temperature control
    # ------------------------------------------------------------------

    def set_tool_temp(self, target: float) -> bool:
        """Set the hot-end (tool) target temperature in degrees Celsius.

        Args:
            target: Target temperature.  Pass ``0`` to turn the heater off.

        Returns:
            ``True`` if the command was accepted.

        Raises:
            PrinterError: If the command fails or the temperature is out of
                the safe range for this printer.
        """
        self._validate_temp(target, _MAX_HOTEND_TEMP, "Hotend")
        self._run_gcode(f"M104 S{int(target)}")
        return True

    def set_bed_temp(self, target: float) -> bool:
        """Set the heated-bed target temperature in degrees Celsius.

        Args:
            target: Target temperature.  Pass ``0`` to turn the heater off.

        Returns:
            ``True`` if the command was accepted.

        Raises:
            PrinterError: If the command fails or the temperature is out of
                the safe range for this printer.
        """
        self._validate_temp(target, _MAX_BED_TEMP, "Bed")
        self._run_gcode(f"M140 S{int(target)}")
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def send_gcode(self, commands: list[str]) -> bool:
        """Send one or more G-code commands to the printer.

        ``rr_gcode`` accepts several newline-separated codes in one request,
        so the batch costs a single round-trip.

        Args:
            commands: List of G-code command strings.

        Returns:
            ``True`` if the commands were accepted.

        Raises:
            PrinterError: If sending fails or the firmware rejects a command.
        """
        script = "\n".join(commands)
        if not script.strip():
            return True
        self._run_gcode(script)
        return True

    def __repr__(self) -> str:
        """Return a debug representation naming the host."""
        return f"DuetAdapter(host={self._host!r})"
