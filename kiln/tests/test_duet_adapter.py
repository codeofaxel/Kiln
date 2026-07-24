"""Tests for the Duet / RepRapFirmware printer adapter.

Every public method of :class:`DuetAdapter` is exercised against a fake
RepRapFirmware board so the suite runs without any hardware.  The fake routes
on the ``rr_*`` endpoint name and records every request, which lets the tests
assert on the *wire* -- the exact G-code sent, the headers on an upload, the
pagination cursor -- rather than only on return values.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import Timeout

from kiln.printers.base import PrinterError, PrinterStatus
from kiln.printers.duet import (
    _RRF2_STATUS_MAP,
    _RRF3_STATUS_MAP,
    DuetAdapter,
    _escape_rrf_string,
    _qualify,
)

HOST = "http://duet.local"


# ---------------------------------------------------------------------------
# Fake RepRapFirmware board
# ---------------------------------------------------------------------------


class _Call:
    """One recorded HTTP request."""

    def __init__(self, method: str, path: str, params: dict[str, Any] | None, kwargs: dict[str, Any]):
        self.method = method
        self.path = path
        self.params = params or {}
        self.kwargs = kwargs

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.method} {self.path} {self.params}>"


class _FakeDuet:
    """A scriptable stand-in for a RepRapFirmware board.

    Handlers are keyed by endpoint (``"rr_model"``); each returns either a
    JSON-serialisable object or a ``(status_code, body)`` pair.
    """

    def __init__(self, **handlers: Any) -> None:
        self.calls: list[_Call] = []
        self.handlers: dict[str, Any] = handlers
        self.session_headers: dict[str, str] = {}

    # -- wiring --------------------------------------------------------
    def install(self, adapter: DuetAdapter) -> DuetAdapter:
        session = mock.MagicMock(spec=requests.Session)
        session.headers = self.session_headers

        def _get(url: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
            return self._handle("GET", url, params, kw)

        def _request(
            method: str, url: str, params: dict[str, Any] | None = None, **kw: Any
        ) -> Any:
            return self._handle(method, url, params, kw)

        session.get.side_effect = _get
        session.request.side_effect = _request
        adapter._session = session
        return adapter

    # -- dispatch ------------------------------------------------------
    def _handle(
        self, method: str, url: str, params: dict[str, Any] | None, kwargs: dict[str, Any]
    ) -> Any:
        path = url.rsplit("/", 1)[-1]
        self.calls.append(_Call(method, path, params, kwargs))

        handler = self.handlers.get(path)
        if handler is None:
            return _response(404, text="Not found")
        if callable(handler):
            handler = handler(params or {}, kwargs)
        if isinstance(handler, tuple):
            status, body = handler
            return _response(status, body)
        return _response(200, handler)

    # -- assertions ----------------------------------------------------
    def gcodes(self) -> list[str]:
        """Every G-code string sent through ``rr_gcode``, in order."""
        return [c.params.get("gcode", "") for c in self.calls if c.path == "rr_gcode"]

    def paths(self) -> list[str]:
        return [c.path for c in self.calls]


def _response(status_code: int, body: Any = None, text: str = "") -> mock.MagicMock:
    """Build a fake :class:`requests.Response`."""
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    if isinstance(body, str):
        resp.text = body
        resp.json.side_effect = ValueError("not json")
    else:
        resp.text = text or ""
        resp.json.return_value = body if body is not None else {}
    return resp


_CONNECT_OK = {"err": 0, "sessionTimeout": 8000, "boardType": "duet3mb6hc", "sessionKey": 4242}


def _adapter(**kwargs: Any) -> DuetAdapter:
    defaults: dict[str, Any] = {"host": HOST, "timeout": 5, "retries": 1}
    defaults.update(kwargs)
    return DuetAdapter(**defaults)


def _rrf3(**handlers: Any) -> _FakeDuet:
    """A fake board running RRF 3, with sensible defaults."""
    base: dict[str, Any] = {
        "rr_connect": _CONNECT_OK,
        "rr_reply": "",
        "rr_gcode": {"bufferSpace": 512},
    }
    base.update(handlers)
    if "rr_model" not in base:
        base["rr_model"] = lambda params, _kw: {
            "key": params.get("key"),
            "flags": params.get("flags"),
            "result": "idle",
        }
    return _FakeDuet(**base)


def _model_router(values: dict[str, Any]):
    """An ``rr_model`` handler serving *values* keyed by object-model key."""

    def _handler(params: dict[str, Any], _kw: dict[str, Any]) -> Any:
        key = params.get("key")
        if key not in values:
            return {"key": key, "flags": params.get("flags")}
        return {"key": key, "flags": params.get("flags"), "result": values[key]}

    return _handler


# ---------------------------------------------------------------------------
# Connection & session handling
# ---------------------------------------------------------------------------


def test_connect_sends_password_and_stores_session_key() -> None:
    fake = _rrf3()
    adapter = fake.install(_adapter(password="hunter2"))

    adapter.get_state()

    connect = next(c for c in fake.calls if c.path == "rr_connect")
    assert connect.params["password"] == "hunter2"
    # Boards from RRF 3.5-b4 hand back a key that must ride on later requests.
    assert fake.session_headers["X-Session-Key"] == "4242"


def test_connect_without_session_key_still_works() -> None:
    """Older boards key the session off client IP and return no sessionKey."""
    fake = _rrf3(rr_connect={"err": 0, "sessionTimeout": 8000, "boardType": "duetwifi"})
    adapter = fake.install(_adapter())

    adapter.get_state()

    assert "X-Session-Key" not in fake.session_headers


def test_connect_rejects_bad_password() -> None:
    fake = _rrf3(rr_connect={"err": 1})
    adapter = fake.install(_adapter(password="wrong"))

    with pytest.raises(PrinterError, match="rejected the password"):
        adapter.get_state()


def test_connect_reports_exhausted_sessions() -> None:
    fake = _rrf3(rr_connect={"err": 2})
    adapter = fake.install(_adapter())

    with pytest.raises(PrinterError, match="no free session slots"):
        adapter.get_state()


def test_connect_happens_once_and_is_reused() -> None:
    fake = _rrf3(rr_model=_model_router({"state.status": "idle", "heat": {}}))
    adapter = fake.install(_adapter())

    adapter.get_state()
    adapter.get_state()

    assert fake.paths().count("rr_connect") == 1


def test_unreachable_board_reports_offline_not_raise() -> None:
    fake = _rrf3()
    adapter = fake.install(_adapter())
    adapter._session.get.side_effect = ReqConnectionError("no route to host")

    state = adapter.get_state()

    assert state.connected is False
    assert state.state is PrinterStatus.OFFLINE


# ---------------------------------------------------------------------------
# 401 / expiry -- the behaviour that must never reach the user
# ---------------------------------------------------------------------------


def test_expired_session_reauthenticates_and_replays_transparently() -> None:
    """A 401 mid-session must reconnect and retry, not surface an error.

    RepRapFirmware answers 401 on every endpoint except rr_connect once the
    session times out, so a long-lived adapter meets this in normal use.
    """
    state = {"expired": True}

    def _model(params: dict[str, Any], _kw: dict[str, Any]) -> Any:
        if state["expired"]:
            state["expired"] = False
            return (401, "Unauthorized")
        return {"key": params.get("key"), "flags": "d99", "result": "idle"}

    fake = _rrf3(rr_model=_model)
    adapter = fake.install(_adapter())

    result = adapter.get_state()

    assert result.state is PrinterStatus.IDLE
    # Reconnected exactly once in response to the 401, then replayed the
    # request that hit it -- the caller never sees the expiry.
    assert fake.paths().count("rr_connect") == 2
    first_calls = fake.paths()[:4]
    assert first_calls == ["rr_connect", "rr_model", "rr_connect", "rr_model"]


def test_persistent_401_eventually_raises_rather_than_looping() -> None:
    fake = _rrf3(rr_model=(401, "Unauthorized"), rr_status=(401, "Unauthorized"))
    adapter = fake.install(_adapter())

    with pytest.raises(PrinterError, match="HTTP 401"):
        adapter.get_state()

    # One re-auth attempt per request, never an unbounded retry storm.
    assert fake.paths().count("rr_connect") <= 4


def test_retryable_status_is_retried() -> None:
    """RRF answers 503 when momentarily short of RAM; that is a retry."""
    attempts = {"n": 0}

    def _model(params: dict[str, Any], _kw: dict[str, Any]) -> Any:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return (503, "Insufficient RAM")
        return {"key": params.get("key"), "result": "idle"}

    fake = _rrf3(rr_model=_model)
    adapter = fake.install(_adapter(retries=3))

    with mock.patch("kiln.printers.duet.time.sleep"):
        assert adapter.get_state().state is PrinterStatus.IDLE
    # The 503 was retried rather than surfaced.
    assert attempts["n"] > 1


# ---------------------------------------------------------------------------
# Firmware generation detection (RRF2 vs RRF3)
# ---------------------------------------------------------------------------


def test_detects_rrf3_when_object_model_answers() -> None:
    fake = _rrf3(rr_model=_model_router({"state.status": "idle", "heat": {}}))
    adapter = fake.install(_adapter())

    assert adapter._generation() == 3


def test_detects_rrf2_when_object_model_is_absent() -> None:
    """RRF 2 has no object model; it must fall back to rr_status, not fail."""
    fake = _rrf3(
        rr_model=(404, "Not found"),
        rr_status={"status": "I", "temps": {}},
    )
    adapter = fake.install(_adapter())

    assert adapter._generation() == 2


def test_generation_detected_once_and_cached() -> None:
    fake = _rrf3(rr_model=_model_router({"state.status": "idle", "heat": {}}))
    adapter = fake.install(_adapter())

    adapter._generation()
    adapter._generation()
    adapter.get_state()

    # The probe query for state.status happens once; later calls reuse it.
    probes = [c for c in fake.calls if c.path == "rr_model"]
    assert len([c for c in probes if c.params.get("key") == "state.status"]) == 2


def test_non_duet_host_gets_an_actionable_error() -> None:
    fake = _rrf3(rr_model=(404, "nope"), rr_status=(404, "nope"))
    adapter = fake.install(_adapter())

    with pytest.raises(PrinterError, match="does not look like a Duet"):
        adapter.get_state()


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


def test_status_tables_cover_every_firmware_state() -> None:
    """Both tables must carry all 13 states the firmware can report.

    RepRapFirmware builds its status string and its status character from a
    single index (RepRap.cpp), so the two representations are the same set.
    A state missing here degrades to UNKNOWN silently.
    """
    assert len(_RRF3_STATUS_MAP) == 13
    assert len(_RRF2_STATUS_MAP) == 13
    assert set(_RRF3_STATUS_MAP.values()) == set(_RRF2_STATUS_MAP.values())
    assert "".join(_RRF2_STATUS_MAP) == "CFHODRSAMPTBI"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("idle", PrinterStatus.IDLE),
        ("processing", PrinterStatus.PRINTING),
        ("paused", PrinterStatus.PAUSED),
        ("cancelling", PrinterStatus.CANCELLING),
        ("halted", PrinterStatus.ERROR),
        ("off", PrinterStatus.OFFLINE),
        ("pausing", PrinterStatus.BUSY),
    ],
)
def test_rrf3_status_mapping(status: str, expected: PrinterStatus) -> None:
    fake = _rrf3(rr_model=_model_router({"state.status": status, "heat": {}}))
    adapter = fake.install(_adapter())

    assert adapter.get_state().state is expected


def test_unknown_status_string_degrades_to_unknown() -> None:
    fake = _rrf3(rr_model=_model_router({"state.status": "teleporting", "heat": {}}))
    adapter = fake.install(_adapter())

    assert adapter.get_state().state is PrinterStatus.UNKNOWN


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------


_HEAT_TYPICAL = {
    # heater 0 is the bed, heater 1 the hotend -- the usual Duet layout.
    "heaters": [
        {"current": 59.8, "active": 60.0, "standby": 0.0, "state": "active"},
        {"current": 244.6, "active": 245.0, "standby": 0.0, "state": "active"},
    ],
    "bedHeaters": [0],
    "chamberHeaters": [-1],
}


def test_get_state_rrf3_reads_temperatures() -> None:
    fake = _rrf3(
        rr_model=_model_router({"state.status": "processing", "heat": _HEAT_TYPICAL})
    )
    adapter = fake.install(_adapter())

    state = adapter.get_state()

    assert state.connected is True
    assert state.state is PrinterStatus.PRINTING
    assert state.tool_temp_actual == 244.6
    assert state.tool_temp_target == 245.0
    assert state.bed_temp_actual == 59.8
    assert state.bed_temp_target == 60.0
    assert state.chamber_temp_actual is None  # -1 means not configured


def test_get_state_rrf3_does_not_mistake_the_bed_for_the_tool() -> None:
    """The tool heater is derived by exclusion, so index order must not fool it."""
    heat = {
        "heaters": [
            {"current": 250.0, "active": 250.0},  # hotend first
            {"current": 60.0, "active": 60.0},  # bed second
            {"current": 40.0, "active": 45.0},  # chamber third
        ],
        "bedHeaters": [1],
        "chamberHeaters": [2],
    }
    fake = _rrf3(rr_model=_model_router({"state.status": "idle", "heat": heat}))
    adapter = fake.install(_adapter())

    state = adapter.get_state()

    assert state.tool_temp_actual == 250.0
    assert state.bed_temp_actual == 60.0
    assert state.chamber_temp_actual == 40.0


def test_get_state_rrf2_parses_legacy_status_response() -> None:
    status = {
        "status": "P",
        "temps": {
            "bed": {"current": 60.1, "active": 60.0, "heater": 0},
            "current": [60.1, 210.4],
            "tools": {"active": [[210.0]], "standby": [[0.0]]},
        },
    }
    fake = _rrf3(rr_model=(404, "no"), rr_status=status)
    adapter = fake.install(_adapter())

    state = adapter.get_state()

    assert state.state is PrinterStatus.PRINTING
    assert state.bed_temp_actual == 60.1
    assert state.tool_temp_actual == 210.4  # index 0 is the bed, so index 1
    assert state.tool_temp_target == 210.0


def test_get_state_survives_a_board_with_no_heaters() -> None:
    fake = _rrf3(rr_model=_model_router({"state.status": "idle", "heat": {}}))
    adapter = fake.install(_adapter())

    state = adapter.get_state()

    assert state.connected is True
    assert state.tool_temp_actual is None


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


def test_get_job_rrf3_computes_completion_from_byte_offset() -> None:
    job = {
        "file": {"fileName": "0:/gcodes/bracket.gcode", "size": 1000, "numLayers": 200},
        "filePosition": 250,
        "duration": 600,
        "layer": 50,
        "timesLeft": {"slicer": 1800, "file": 1900, "filament": 2000},
    }
    fake = _rrf3(rr_model=_model_router({"state.status": "processing", "job": job}))
    adapter = fake.install(_adapter())

    progress = adapter.get_job()

    assert progress.file_name == "0:/gcodes/bracket.gcode"
    assert progress.completion == 25.0
    assert progress.print_time_seconds == 600
    assert progress.print_time_left_seconds == 1800  # slicer estimate preferred
    assert progress.current_layer == 50
    assert progress.total_layers == 200


def test_get_job_rrf3_falls_back_through_time_estimates() -> None:
    job = {
        "file": {"fileName": "a.gcode", "size": 100},
        "filePosition": 50,
        "timesLeft": {"file": 900},
    }
    fake = _rrf3(rr_model=_model_router({"state.status": "processing", "job": job}))
    adapter = fake.install(_adapter())

    assert adapter.get_job().print_time_left_seconds == 900


def test_get_job_with_no_active_job_is_empty_not_an_error() -> None:
    fake = _rrf3(rr_model=_model_router({"state.status": "idle", "job": {}}))
    adapter = fake.install(_adapter())

    progress = adapter.get_job()

    assert progress.file_name is None
    assert progress.completion is None


def test_get_job_rrf2_uses_fileinfo_for_the_filename() -> None:
    """rr_status type 3 carries progress but not the filename."""
    fake = _rrf3(
        rr_model=(404, "no"),
        rr_status={
            "status": "P",
            "fractionPrinted": 42.5,
            "printDuration": 900,
            "currentLayer": 30,
            "timesLeft": {"file": 1200},
        },
        rr_fileinfo={"err": 0, "fileName": "0:/gcodes/part.gcode", "numLayers": 120},
    )
    adapter = fake.install(_adapter())

    progress = adapter.get_job()

    assert progress.completion == 42.5
    assert progress.print_time_seconds == 900
    assert progress.print_time_left_seconds == 1200
    assert progress.current_layer == 30
    assert progress.file_name == "0:/gcodes/part.gcode"
    assert progress.total_layers == 120


def test_get_job_rrf2_still_reports_progress_when_fileinfo_is_unavailable() -> None:
    fake = _rrf3(
        rr_model=(404, "no"),
        rr_status={"status": "P", "fractionPrinted": 10.0},
        rr_fileinfo=(404, "no"),
    )
    adapter = fake.install(_adapter())

    progress = adapter.get_job()

    assert progress.completion == 10.0
    assert progress.file_name is None


# ---------------------------------------------------------------------------
# list_files -- including the pagination that silently truncates if ignored
# ---------------------------------------------------------------------------


def test_list_files_follows_the_pagination_cursor() -> None:
    """rr_filelist returns partial listings; `next` is the resume index.

    Ignoring it silently truncates the listing on any board with a full card.
    """
    pages = {
        0: {
            "dir": "0:/gcodes",
            "first": 0,
            "files": [
                {"type": "f", "name": "a.gcode", "size": 100, "date": "2026-07-01T10:00:00"},
                {"type": "f", "name": "b.gcode", "size": 200},
            ],
            "next": 2,
            "err": 0,
        },
        2: {
            "dir": "0:/gcodes",
            "first": 2,
            "files": [{"type": "f", "name": "c.gcode", "size": 300}],
            "next": 0,  # 0 means the listing is complete
            "err": 0,
        },
    }
    fake = _rrf3(rr_filelist=lambda params, _kw: pages[int(params.get("first", 0))])
    adapter = fake.install(_adapter())

    files = adapter.list_files()

    assert [f.name for f in files] == ["a.gcode", "b.gcode", "c.gcode"]
    assert files[0].path == "0:/gcodes/a.gcode"
    assert files[0].size_bytes == 100
    assert files[0].date is not None
    assert files[1].date is None  # absent date stays absent, never invented
    # Two requests, and the second resumed from the reported cursor.
    filelists = [c for c in fake.calls if c.path == "rr_filelist"]
    assert [c.params["first"] for c in filelists] == [0, 2]


def test_list_files_skips_directories() -> None:
    fake = _rrf3(
        rr_filelist={
            "files": [
                {"type": "d", "name": "subfolder"},
                {"type": "f", "name": "real.gcode", "size": 10},
            ],
            "next": 0,
            "err": 0,
        }
    )
    adapter = fake.install(_adapter())

    assert [f.name for f in adapter.list_files()] == ["real.gcode"]


def test_list_files_reports_an_unmounted_card_clearly() -> None:
    fake = _rrf3(rr_filelist={"err": 1})
    adapter = fake.install(_adapter())

    with pytest.raises(PrinterError, match="SD card is not mounted"):
        adapter.list_files()


def test_list_files_reports_a_missing_directory_clearly() -> None:
    fake = _rrf3(rr_filelist={"err": 2})
    adapter = fake.install(_adapter())

    with pytest.raises(PrinterError, match="does not exist"):
        adapter.list_files()


def test_list_files_terminates_on_a_non_advancing_cursor() -> None:
    """A malformed `next` must not spin forever."""
    fake = _rrf3(
        rr_filelist={"files": [{"type": "f", "name": "x.gcode"}], "next": 0 or 1, "err": 0}
    )
    # `next` == 1 while first stays 0 would loop; the guard stops after a pass.
    adapter = fake.install(_adapter())

    files = adapter.list_files()

    assert len(files) >= 1
    assert len([c for c in fake.calls if c.path == "rr_filelist"]) <= 3


# ---------------------------------------------------------------------------
# upload / delete
# ---------------------------------------------------------------------------


def test_upload_sends_raw_body_with_content_length(tmp_path: Any) -> None:
    """rr_upload takes the file as a raw body with an explicit Content-Length.

    Multipart or chunked encoding is not accepted by the firmware.
    """
    src = tmp_path / "cube.gcode"
    src.write_bytes(b"G28\nG1 X10\n")

    fake = _rrf3(rr_upload={"err": 0})
    adapter = fake.install(_adapter())

    result = adapter.upload_file(str(src))

    assert result.success is True
    assert result.file_name == "0:/gcodes/cube.gcode"

    upload = next(c for c in fake.calls if c.path == "rr_upload")
    assert upload.method == "POST"
    assert upload.params["name"] == "0:/gcodes/cube.gcode"
    headers = upload.kwargs["headers"]
    assert headers["Content-Length"] == str(len(b"G28\nG1 X10\n"))
    assert headers["Content-Type"] == "application/octet-stream"
    # The body is the file itself -- no multipart wrapper.
    assert "files" not in upload.kwargs


def test_upload_sends_a_crc32_the_board_can_verify(tmp_path: Any) -> None:
    import zlib

    payload = b"G28\n"
    src = tmp_path / "x.gcode"
    src.write_bytes(payload)

    fake = _rrf3(rr_upload={"err": 0})
    adapter = fake.install(_adapter())
    adapter.upload_file(str(src))

    upload = next(c for c in fake.calls if c.path == "rr_upload")
    assert upload.params["crc32"] == format(zlib.crc32(payload) & 0xFFFFFFFF, "08x")


def test_upload_raises_when_the_board_rejects_it(tmp_path: Any) -> None:
    src = tmp_path / "x.gcode"
    src.write_bytes(b"G28\n")

    fake = _rrf3(rr_upload={"err": 1})
    adapter = fake.install(_adapter())

    with pytest.raises(PrinterError, match="rejected the upload"):
        adapter.upload_file(str(src))


def test_upload_missing_local_file_raises_filenotfound() -> None:
    fake = _rrf3()
    adapter = fake.install(_adapter())

    with pytest.raises(FileNotFoundError):
        adapter.upload_file("/nope/missing.gcode")


def test_delete_file_qualifies_a_bare_name() -> None:
    fake = _rrf3(rr_delete={"err": 0})
    adapter = fake.install(_adapter())

    assert adapter.delete_file("old.gcode") is True

    delete = next(c for c in fake.calls if c.path == "rr_delete")
    assert delete.params["name"] == "0:/gcodes/old.gcode"


def test_delete_file_failure_raises() -> None:
    fake = _rrf3(rr_delete={"err": 1})
    adapter = fake.install(_adapter())

    with pytest.raises(PrinterError, match="Could not delete"):
        adapter.delete_file("old.gcode")


# ---------------------------------------------------------------------------
# Print control
# ---------------------------------------------------------------------------


def test_start_print_sends_quoted_m32(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _rrf3()
    adapter = fake.install(_adapter())

    result = adapter._start_print_impl("bracket.gcode")

    assert result.success is True
    assert fake.gcodes() == ['M32 "0:/gcodes/bracket.gcode"']


def test_start_print_escapes_apostrophes_in_filenames() -> None:
    """An un-escaped apostrophe silently lower-cases the next character.

    RepRapFirmware's string parser treats ' before a letter as a
    force-to-lowercase escape, so "adam'sPart" would resolve to a different
    filename rather than failing loudly.
    """
    fake = _rrf3()
    adapter = fake.install(_adapter())

    adapter._start_print_impl("adam'sPart.gcode")

    assert fake.gcodes() == ["""M32 "0:/gcodes/adam''sPart.gcode\""""]


def test_start_print_routes_through_the_base_safety_gate() -> None:
    """The adapter must implement _start_print_impl and never override start_print."""
    assert "start_print" not in DuetAdapter.__dict__
    assert "resume_print" not in DuetAdapter.__dict__
    assert "_start_print_impl" in DuetAdapter.__dict__
    assert "_resume_print_impl" in DuetAdapter.__dict__


def test_pause_sends_m25() -> None:
    fake = _rrf3()
    adapter = fake.install(_adapter())

    adapter.pause_print()

    assert fake.gcodes() == ["M25"]


def test_resume_sends_m24() -> None:
    fake = _rrf3()
    adapter = fake.install(_adapter())

    adapter._resume_print_impl()

    assert fake.gcodes() == ["M24"]


def test_cancel_pauses_before_cancelling() -> None:
    """RepRapFirmware refuses M0 unless the job is already paused.

    GCodes2.cpp replies "Pause the print before attempting to cancel it" when
    M0 arrives over HTTP mid-print, so a bare M0 looks fine and does nothing.
    """
    states = iter(["processing", "pausing", "paused", "paused"])
    current = {"value": "processing"}

    def _model(params: dict[str, Any], _kw: dict[str, Any]) -> Any:
        key = params.get("key")
        if key == "state.status":
            current["value"] = next(states, "paused")
            return {"key": key, "result": current["value"]}
        return {"key": key, "result": {}}

    fake = _rrf3(rr_model=_model)
    adapter = fake.install(_adapter())

    with mock.patch("kiln.printers.duet.time.sleep"):
        result = adapter.cancel_print()

    assert result.success is True
    assert fake.gcodes() == ["M25", "M0"]


def test_cancel_skips_the_pause_when_already_paused() -> None:
    fake = _rrf3(rr_model=_model_router({"state.status": "paused", "heat": {}}))
    adapter = fake.install(_adapter())

    adapter.cancel_print()

    assert fake.gcodes() == ["M0"]


def test_cancel_when_idle_is_a_no_op() -> None:
    fake = _rrf3(rr_model=_model_router({"state.status": "idle", "heat": {}}))
    adapter = fake.install(_adapter())

    result = adapter.cancel_print()

    assert result.success is True
    assert "No active print job" in result.message
    assert fake.gcodes() == []


def test_cancel_raises_rather_than_sending_a_doomed_m0() -> None:
    """If the pause never lands, M0 would be rejected -- say so instead."""
    fake = _rrf3(rr_model=_model_router({"state.status": "processing", "heat": {}}))
    adapter = fake.install(_adapter())

    with mock.patch("kiln.printers.duet.time.sleep"), mock.patch(
        "kiln.printers.duet.time.monotonic", side_effect=[0.0, 100.0, 200.0]
    ):
        with pytest.raises(PrinterError, match="did not reach a paused state"):
            adapter.cancel_print()

    assert "M0" not in fake.gcodes()


def test_emergency_stop_sends_m112_and_does_not_auto_reset() -> None:
    """M999 would un-latch the stop; clearing it is a separate deliberate act."""
    fake = _rrf3()
    adapter = fake.install(_adapter())

    result = adapter.emergency_stop()

    assert result.success is True
    assert fake.gcodes() == ["M112"]
    assert "M999" not in " ".join(fake.gcodes())


# ---------------------------------------------------------------------------
# Temperature & G-code
# ---------------------------------------------------------------------------


def test_set_tool_temp_sends_m104() -> None:
    fake = _rrf3()
    adapter = fake.install(_adapter())

    assert adapter.set_tool_temp(245) is True
    assert fake.gcodes() == ["M104 S245"]


def test_set_bed_temp_sends_m140() -> None:
    fake = _rrf3()
    adapter = fake.install(_adapter())

    assert adapter.set_bed_temp(60) is True
    assert fake.gcodes() == ["M140 S60"]


def test_high_temp_machines_are_not_capped_at_desktop_limits() -> None:
    """Duet drives 500 C hotends; a 300 C ceiling would break the class."""
    fake = _rrf3()
    adapter = fake.install(_adapter())

    assert adapter.set_tool_temp(450) is True
    assert fake.gcodes() == ["M104 S450"]


def test_temperature_above_the_ceiling_is_refused() -> None:
    fake = _rrf3()
    adapter = fake.install(_adapter())

    with pytest.raises(PrinterError, match="exceeds safety limit"):
        adapter.set_tool_temp(900)
    assert fake.gcodes() == []


def test_negative_temperature_is_refused() -> None:
    fake = _rrf3()
    adapter = fake.install(_adapter())

    with pytest.raises(PrinterError, match="negative"):
        adapter.set_bed_temp(-5)


def test_safety_profile_narrows_the_ceiling() -> None:
    """A bound per-printer profile must be able to lower the adapter ceiling."""
    fake = _rrf3()
    adapter = fake.install(_adapter())

    profile = mock.MagicMock(max_hotend_temp=260.0, max_bed_temp=100.0)
    with mock.patch("kiln.safety_profiles.get_profile", return_value=profile):
        adapter.set_safety_profile("some_printer")
        with pytest.raises(PrinterError, match="exceeds safety limit"):
            adapter.set_tool_temp(400)


def test_send_gcode_batches_commands_into_one_request() -> None:
    fake = _rrf3()
    adapter = fake.install(_adapter())

    assert adapter.send_gcode(["G28", "G1 Z10"]) is True
    assert fake.gcodes() == ["G28\nG1 Z10"]


def test_send_gcode_surfaces_a_firmware_rejection() -> None:
    """rr_gcode returns 200 even for a rejected command; the reply carries it."""
    fake = _rrf3(rr_reply="Error: bad command")
    adapter = fake.install(_adapter())

    with pytest.raises(PrinterError, match="Error: bad command"):
        adapter.send_gcode(["M9999"])


def test_empty_gcode_batch_is_a_no_op() -> None:
    fake = _rrf3()
    adapter = fake.install(_adapter())

    assert adapter.send_gcode([]) is True
    assert fake.gcodes() == []


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain.gcode", "plain.gcode"),
        ("adam's.gcode", "adam''s.gcode"),
        ('quote".gcode', 'quote"".gcode'),
    ],
)
def test_escape_rrf_string(raw: str, expected: str) -> None:
    assert _escape_rrf_string(raw) == expected


def test_escape_rrf_string_rejects_control_characters() -> None:
    with pytest.raises(PrinterError, match="control character"):
        _escape_rrf_string("bad\nname.gcode")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("part.gcode", "0:/gcodes/part.gcode"),
        ("0:/gcodes/part.gcode", "0:/gcodes/part.gcode"),
        ("/sys/config.g", "/sys/config.g"),
    ],
)
def test_qualify_paths(raw: str, expected: str) -> None:
    assert _qualify(raw) == expected


def test_capabilities_are_honest_about_what_is_missing() -> None:
    caps = _adapter().capabilities

    assert caps.can_upload and caps.can_set_temp and caps.can_send_gcode and caps.can_pause
    # RRF has no built-in camera, and no bed-mesh or firmware-update reader is
    # implemented here -- these must not advertise behaviour that is absent.
    assert not caps.can_snapshot
    assert not caps.can_stream
    assert not caps.can_update_firmware


def test_adapter_is_free_tier_and_needs_no_pro_package() -> None:
    """A control adapter is plain orchestration; it must not import kiln_pro."""
    import inspect

    import kiln.printers.duet as duet_module

    source = inspect.getsource(duet_module)
    assert "kiln_pro" not in source


def test_adapter_writes_no_local_state() -> None:
    """No ~/.kiln store means no caller-scoped state to gate on hosted deploys."""
    import inspect

    import kiln.printers.duet as duet_module

    source = inspect.getsource(duet_module)
    assert "~/.kiln" not in source
    assert "Path.home" not in source
