"""Duet / RepRapFirmware adapter for the Kiln printer abstraction layer."""

from __future__ import annotations

import logging
import os
import time
import zlib
from datetime import datetime, timezone
from typing import Any, ClassVar

import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import RequestException, Timeout

from kiln.printers.base import (
    DEFAULT_LOAD_LENGTH_MM,
    DEFAULT_PURGE_LENGTH_MM,
    DEFAULT_UNLOAD_LENGTH_MM,
    FilamentOpPlan,
    FilamentOpResult,
    JobProgress,
    MachineIdentity,
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

logger = logging.getLogger(__name__)

# HTTP status codes eligible for automatic retry
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({502, 503, 504})

_MAX_HOTEND_TEMP: float = 500.0
_MAX_BED_TEMP: float = 200.0

_GCODE_DIR: str = "0:/gcodes"

# How long cancel_print waits for a pause to take effect before giving up
_PAUSE_SETTLE_TIMEOUT_S: float = 15.0
_PAUSE_POLL_INTERVAL_S: float = 0.5

_RRF3_STATUS_MAP: dict[str, PrinterStatus] = {
    "starting": PrinterStatus.BUSY,  # reading config.g
    "updating": PrinterStatus.BUSY,
    "halted": PrinterStatus.ERROR,
    "off": PrinterStatus.OFFLINE,
    "pausing": PrinterStatus.BUSY,  # decelerating; not yet stopped
    "resuming": PrinterStatus.BUSY,
    "paused": PrinterStatus.PAUSED,
    "cancelling": PrinterStatus.CANCELLING,
    "simulating": PrinterStatus.BUSY,
    "processing": PrinterStatus.PRINTING,
    "changingTool": PrinterStatus.BUSY,
    "busy": PrinterStatus.BUSY,
    "idle": PrinterStatus.IDLE,
}

_RRF2_STATUS_MAP: dict[str, PrinterStatus] = {
    "C": PrinterStatus.BUSY,  # starting / reading configuration
    "F": PrinterStatus.BUSY,
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
    """Escape *value* for use inside a double-quoted RepRapFirmware string."""
    if any(ch < " " for ch in value):
        raise PrinterError(
            f"Filename {value!r} contains a control character, which "
            "RepRapFirmware's G-code parser rejects. Rename the file."
        )
    return value.replace("'", "''").replace('"', '""')


def _qualify(file_name: str) -> str:
    """Return *file_name* as an absolute path on the printer's SD volume."""
    name = file_name.strip()
    if name.startswith("/") or ":" in name.split("/", 1)[0]:
        return name
    return f"{_GCODE_DIR}/{name}"


def _parse_rrf_datetime(value: Any) -> int | None:
    """Convert an RRF ISO8601-like timestamp to a Unix timestamp."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


class DuetAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by the RepRapFirmware HTTP API."""

    _DURATION_SEMANTICS: ClassVar[str] = "frozen"

    head_position = "head_and_extruder"

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

        # Session state
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
            can_clear_error=True,
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
            can_handle_filament=True,
            can_pause=True,
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
        """Establish an HTTP session with the board via ``rr_connect``."""
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
        """Execute an HTTP request with retry and transparent re-authentication."""
        self._ensure_session()

        url = self._url(path)
        last_exc: Exception | None = None
        reauthed = False
        attempt = 0

        while attempt < self._retries:
            try:
                if hasattr(data, "seek"):
                    data.seek(0)  # type: ignore[union-attr]

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
                    # Session expired -- re-authenticate and replay
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
                    error.status_code = sc  # type: ignore[attr-defined]
                    raise error

                # Retryable HTTP status -- fall through to backoff
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
        """Shorthand: GET *path* and return the parsed JSON body."""
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
    # ------------------------------------------------------------------

    @staticmethod
    def _is_access_failure(exc: PrinterError) -> bool:
        """True when *exc* means "could not reach or authenticate", not "absent"."""
        if isinstance(exc.cause, (ReqConnectionError, Timeout)):
            return True
        return getattr(exc, "status_code", None) == 401

    def _generation(self) -> int:
        """Return the firmware generation (``3`` or ``2``), detecting it once."""
        if self._rrf_generation is not None:
            return self._rrf_generation

        # Authenticate up front
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

    def _model(self, key: str, *, flags: str = "d99") -> Any:
        """Query one object-model *key* (RRF 3 only) and return its ``result``."""
        body = self._get_json("/rr_model", params={"key": key, "flags": flags})
        return body.get("result")

    def get_tool_position(self) -> dict[str, float] | None:
        """Where the head is and the first extruder's position, from the RRF 3 object model."""
        try:
            if self._generation() != 3:
                return None
            move = self._model("move")
        except PrinterError:
            return None
        if not isinstance(move, dict):
            return None
        out: dict[str, float] = {}
        for axis in move.get("axes") or []:
            if not isinstance(axis, dict):
                continue
            letter = str(axis.get("letter") or "").lower()
            value = axis.get("machinePosition")
            if letter in ("x", "y", "z") and isinstance(value, (int, float)) and not isinstance(value, bool):
                out[letter] = float(value)
        extruders = move.get("extruders") or []
        first = extruders[0] if extruders and isinstance(extruders[0], dict) else {}
        e = first.get("position")
        if isinstance(e, (int, float)) and not isinstance(e, bool):
            out["e"] = float(e)
        return out if all(k in out for k in ("x", "y", "z")) else None

    def read_machine_identity(self) -> MachineIdentity | None:
        """The main board's unique id, from the RRF 3 object model."""
        try:
            if self._generation() != 3:
                return None
            raw = self._model("boards[0].uniqueId", flags="d99v")
        except PrinterError:
            return None
        value = str(raw or "").strip() if isinstance(raw, (str, int)) else ""
        if not value:
            return None
        return MachineIdentity(
            value=value, source="rrf_object_model.boards[0].uniqueId", kind="board_id"
        )

    def _current_status(self) -> PrinterStatus:
        """Fetch just the machine status, without temperatures."""
        if self._generation() == 3:
            raw = self._model("state.status")
            return _RRF3_STATUS_MAP.get(
                raw if isinstance(raw, str) else "", PrinterStatus.UNKNOWN
            )

        payload = self._get_json("/rr_status", params={"type": 1})
        raw = payload.get("status")
        return _RRF2_STATUS_MAP.get(
            raw if isinstance(raw, str) else "", PrinterStatus.UNKNOWN
        )

    def disconnect(self) -> None:
        """Release this adapter's session on the board (``rr_disconnect``)."""
        if not self._connected:
            return
        try:
            self._request("GET", "/rr_disconnect")
        except PrinterError:
            logger.debug("rr_disconnect failed; the session will time out", exc_info=True)
        finally:
            self._connected = False
            self._session.headers.pop("X-Session-Key", None)

    # ------------------------------------------------------------------
    # G-code transport
    # ------------------------------------------------------------------

    def _run_gcode(self, gcode: str) -> str:
        """Execute *gcode* and return whatever the firmware replied."""
        self._request("GET", "/rr_gcode", params={"gcode": gcode})

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
        """Retrieve the current printer state and temperatures."""
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
        """Return the first valid heater index listed under *key*."""
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
        """Retrieve progress info for the active (or last) print job."""
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

        # timesLeft carries three independent estimates
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
        """Read job progress from the legacy ``rr_status?type=3`` response."""
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
        """Return the G-code files stored on the printer's SD volume."""
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
        """Upload a local G-code file to the printer's SD card."""
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Local file not found: {abs_path}")

        filename = os.path.basename(abs_path)
        size = os.path.getsize(abs_path)

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
        """Delete a G-code file from the printer's SD card."""
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
        """Begin printing a file that already exists on the printer."""
        target = _qualify(file_name)
        self._run_gcode(f'M32 "{_escape_rrf_string(target)}"')
        return PrintResult(success=True, message=f"Started printing {target}.")

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job (``M25``)."""
        self._run_gcode("M25")
        return PrintResult(success=True, message="Print paused.")

    def _resume_print_impl(self) -> PrintResult:
        """Resume a previously paused print job (``M24``)."""
        self._run_gcode("M24")
        return PrintResult(success=True, message="Print resumed.")

    def cancel_print(self) -> PrintResult:
        """Cancel the currently running print job."""
        status = self._current_status()

        if status in (PrinterStatus.IDLE, PrinterStatus.OFFLINE):
            return PrintResult(success=True, message="No active print job to cancel.")

        if status != PrinterStatus.PAUSED:
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
            status = self._current_status()
            if status == PrinterStatus.PAUSED:
                return True
            if status in (PrinterStatus.IDLE, PrinterStatus.OFFLINE, PrinterStatus.ERROR):
                return False
        return False

    def emergency_stop(self) -> PrintResult:
        """Perform an immediate emergency stop (``M112``)."""
        self._request("GET", "/rr_gcode", params={"gcode": "M112"})
        return PrintResult(
            success=True,
            message="Emergency stop triggered. The board stays halted until it is reset.",
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- temperature control
    # ------------------------------------------------------------------

    def set_tool_temp(self, target: float) -> CommandVerdict:
        """Set the hot-end (tool) target temperature in degrees Celsius."""
        self._validate_temp(target, _MAX_HOTEND_TEMP, "Hotend")
        self._run_gcode(f"M104 S{int(target)}")
        return CommandVerdict.accepted_only(
            f"Hotend target {int(target)}°C: RepRapFirmware took the command and did not reject it. "
            "The resulting state is not read back on this call — read "
            "printer_status to confirm.",
            corroboration="rr_gcode_ok",
        )

    def set_bed_temp(self, target: float) -> CommandVerdict:
        """Set the heated-bed target temperature in degrees Celsius."""
        self._validate_temp(target, _MAX_BED_TEMP, "Bed")
        self._run_gcode(f"M140 S{int(target)}")
        return CommandVerdict.accepted_only(
            f"Bed target {int(target)}°C: RepRapFirmware took the command and did not reject it. "
            "The resulting state is not read back on this call — read "
            "printer_status to confirm.",
            corroboration="rr_gcode_ok",
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def clear_error(self) -> PrintResult:
        """Reset the board after a halt, with ``M999``."""
        self.send_gcode(["M999"])
        return PrintResult(
            success=True,
            message=(
                "Sent M999 to reset the board. Re-read printer_status to "
                "confirm it came back ready."
            ),
        )

    def _query_homed_axes(self) -> set[str] | None:
        """RepRapFirmware's own word: ``move.axes[].homed`` (RRF 3), or the legacy ``coords.axesHomed`` flags (RRF 2)."""
        if self._generation() == 3:
            self._homed_axes_field = "move.axes[].homed"
            axes = self._model("move.axes")
            if not isinstance(axes, list):
                return None
            return {
                str(a.get("letter", "")).upper()
                for a in axes
                if isinstance(a, dict) and a.get("homed") is True and str(a.get("letter", "")).upper() in "XYZ"
            }
        self._homed_axes_field = "coords.axesHomed"
        payload = self._get_json("/rr_status", params={"type": 1})
        flags = payload.get("coords", {}).get("axesHomed")
        if not isinstance(flags, list):
            return None
        return {axis for axis, flag in zip("XYZ", flags, strict=False) if flag}

    def _read_homed_axes(self) -> set[str] | None:
        """The confirm-after-homing read: a failed query reads as "not confirmed"."""
        try:
            return self._query_homed_axes()
        except PrinterError:
            return None

    def homed_axes_now(self) -> set[str] | None:
        """A fresh object-model read, lowercase; raises on a failed read."""
        axes = self._query_homed_axes()
        return None if axes is None else {c.lower() for c in axes}

    def send_gcode(self, commands: list[str]) -> CommandVerdict:
        """Send one or more G-code commands to the printer."""
        script = "\n".join(self._gcode_lines(commands))
        if not script.strip():
            return CommandVerdict.accepted_only(
                "Nothing to send: every line was blank.", corroboration="none"
            )
        self._run_gcode(script)
        return CommandVerdict.accepted_only(
            f"{len(commands)} G-code line(s): RepRapFirmware took the command and did not reject it. "
            "The resulting state is not read back on this call — read "
            "printer_status to confirm.",
            corroboration="rr_gcode_ok",
        )


    # ------------------------------------------------------------------
    # Filament handling — the shared G-code sequence
    # ------------------------------------------------------------------

    def _load_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Heat, then feed ``length_mm`` — the manual-load the firmware's own Extrude button performs."""
        return self._gcode_filament_move(
            plan,
            signed_length_mm=float(plan.length_mm or DEFAULT_LOAD_LENGTH_MM),
            mechanism="duet_rr_gcode",
        )

    def _unload_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Heat, then retract ``length_mm`` to free the melt zone."""
        return self._gcode_filament_move(
            plan,
            signed_length_mm=-float(plan.length_mm or DEFAULT_UNLOAD_LENGTH_MM),
            mechanism="duet_rr_gcode",
        )

    def _purge_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Heat, then extrude ``length_mm`` — the clog test."""
        return self._gcode_filament_move(
            plan,
            signed_length_mm=float(plan.length_mm or DEFAULT_PURGE_LENGTH_MM),
            mechanism="duet_rr_gcode",
        )

    def __repr__(self) -> str:
        """Return a debug representation naming the host."""
        return f"DuetAdapter(host={self._host!r})"
