"""Tests for the Elegoo SDCP printer adapter.

Every public method of :class:`ElegooAdapter` is exercised with mocked
WebSocket and HTTP responses so the test suite runs without a real printer.

Coverage areas:
- Constructor validation
- State mapping from SDCP status codes
- Job progress parsing
- File listing
- File upload (HTTP pull mechanism)
- Print control (start, pause, resume, cancel, emergency stop)
- Temperature control via G-code
- G-code command sending
- File deletion
- WebSocket connection management and backoff
- Discovery (UDP broadcast)
- Stream URL retrieval
- Helper functions (_safe_float, _safe_int)
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from typing import Any, Dict
from unittest import mock

import pytest

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
from kiln.printers.elegoo import (
    ElegooAdapter,
    _BackoffState,
    _CMD_CANCEL_PRINT,
    _CMD_DELETE_FILE,
    _CMD_GET_ATTRIBUTES,
    _CMD_LIST_FILES,
    _CMD_PAUSE_PRINT,
    _CMD_RESUME_PRINT,
    _CMD_START_PRINT,
    _CMD_STATUS_REQUEST,
    _CMD_UPLOAD_FILE,
    _PRINT_STATUS_MAP,
    _safe_float,
    _safe_int,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

HOST = "192.168.1.50"
MAINBOARD_ID = "ABCD1234ABCD1234"


def _adapter(**kwargs: Any) -> ElegooAdapter:
    """Create an :class:`ElegooAdapter` with sensible test defaults."""
    defaults: Dict[str, Any] = {
        "host": HOST,
        "mainboard_id": MAINBOARD_ID,
        "timeout": 2,
    }
    defaults.update(kwargs)
    return ElegooAdapter(**defaults)


@pytest.fixture
def adapter_with_ws() -> ElegooAdapter:
    """Create an adapter with pre-mocked WebSocket connection state.

    This simulates a successfully connected WebSocket client so that tests
    can exercise adapter methods without triggering real network calls.
    """
    adapter = _adapter()
    # Pre-populate the WebSocket state to avoid actual connection.
    mock_ws = mock.MagicMock()
    mock_ws.send = mock.MagicMock()
    mock_ws.recv = mock.MagicMock(return_value="")
    mock_ws.close = mock.MagicMock()
    adapter._ws = mock_ws
    adapter._connected = True
    # Pre-set a status so state queries work.
    adapter._last_status = {"CurrentStatus": 0}
    adapter._last_state_time = time.monotonic()
    return adapter


# ---------------------------------------------------------------------------
# Constructor tests
# ---------------------------------------------------------------------------


class TestElegooAdapterInit:
    """Tests for adapter construction and validation."""

    def test_empty_host_raises(self) -> None:
        with pytest.raises(ValueError, match="host must not be empty"):
            ElegooAdapter(host="")

    def test_valid_construction(self) -> None:
        adapter = _adapter()
        assert adapter._host == HOST
        assert adapter._mainboard_id == MAINBOARD_ID

    def test_timeout_stored(self) -> None:
        adapter = _adapter(timeout=15)
        assert adapter._timeout == 15

    def test_default_mainboard_id_empty(self) -> None:
        adapter = ElegooAdapter(host=HOST)
        assert adapter._mainboard_id == ""

    def test_host_stripped(self) -> None:
        adapter = ElegooAdapter(host="  192.168.1.50  ")
        assert adapter._host == "192.168.1.50"


# ---------------------------------------------------------------------------
# Identity and capabilities
# ---------------------------------------------------------------------------


class TestElegooAdapterIdentity:
    """Tests for name and capabilities properties."""

    def test_name(self) -> None:
        adapter = _adapter()
        assert adapter.name == "elegoo"

    def test_capabilities_type(self) -> None:
        adapter = _adapter()
        caps = adapter.capabilities
        assert isinstance(caps, PrinterCapabilities)

    def test_can_upload(self) -> None:
        adapter = _adapter()
        assert adapter.capabilities.can_upload is True

    def test_can_pause(self) -> None:
        adapter = _adapter()
        assert adapter.capabilities.can_pause is True

    def test_can_set_temp(self) -> None:
        adapter = _adapter()
        assert adapter.capabilities.can_set_temp is True

    def test_supported_extensions(self) -> None:
        adapter = _adapter()
        exts = adapter.capabilities.supported_extensions
        assert ".gcode" in exts
        assert ".ctb" in exts
        assert ".3mf" in exts

    def test_is_printer_adapter(self) -> None:
        adapter = _adapter()
        assert isinstance(adapter, PrinterAdapter)

    def test_repr(self) -> None:
        adapter = _adapter()
        r = repr(adapter)
        assert "ElegooAdapter" in r
        assert HOST in r
        assert MAINBOARD_ID in r


# ---------------------------------------------------------------------------
# State mapping
# ---------------------------------------------------------------------------


class TestStateMapping:
    """Tests for SDCP print status → PrinterStatus mapping."""

    def test_idle_maps_correctly(self) -> None:
        assert _PRINT_STATUS_MAP[0] == PrinterStatus.IDLE

    def test_printing_maps_correctly(self) -> None:
        assert _PRINT_STATUS_MAP[13] == PrinterStatus.PRINTING

    def test_paused_maps_correctly(self) -> None:
        assert _PRINT_STATUS_MAP[10] == PrinterStatus.PAUSED

    def test_busy_states(self) -> None:
        for code in (5, 8, 9, 20):
            assert _PRINT_STATUS_MAP[code] == PrinterStatus.BUSY

    def test_build_state_from_cache_idle(self, adapter_with_ws: ElegooAdapter) -> None:
        state = adapter_with_ws._build_state_from_cache({"CurrentStatus": 0})
        assert state.state == PrinterStatus.IDLE
        assert state.connected is True

    def test_build_state_from_cache_printing(self, adapter_with_ws: ElegooAdapter) -> None:
        state = adapter_with_ws._build_state_from_cache({"CurrentStatus": 13})
        assert state.state == PrinterStatus.PRINTING

    def test_build_state_from_cache_unknown_code(self, adapter_with_ws: ElegooAdapter) -> None:
        state = adapter_with_ws._build_state_from_cache({"CurrentStatus": 999})
        assert state.state == PrinterStatus.UNKNOWN

    def test_build_state_temperatures(self, adapter_with_ws: ElegooAdapter) -> None:
        state = adapter_with_ws._build_state_from_cache({
            "CurrentStatus": 0,
            "TempOfNozzle": 210.5,
            "TempOfNozzleTarget": 220.0,
            "TempOfHotbed": 60.0,
            "TempOfHotbedTarget": 65.0,
            "TempOfBox": 35.0,
        })
        assert state.tool_temp_actual == 210.5
        assert state.tool_temp_target == 220.0
        assert state.bed_temp_actual == 60.0
        assert state.bed_temp_target == 65.0
        assert state.chamber_temp_actual == 35.0

    def test_build_state_alternate_temp_keys(self, adapter_with_ws: ElegooAdapter) -> None:
        state = adapter_with_ws._build_state_from_cache({
            "CurrentStatus": 0,
            "NozzleTemp": 200.0,
            "BedTemp": 55.0,
            "ChamberTemp": 30.0,
        })
        assert state.tool_temp_actual == 200.0
        assert state.bed_temp_actual == 55.0
        assert state.chamber_temp_actual == 30.0

    def test_build_state_string_status(self, adapter_with_ws: ElegooAdapter) -> None:
        state = adapter_with_ws._build_state_from_cache({"CurrentStatus": "13"})
        assert state.state == PrinterStatus.PRINTING

    def test_build_state_invalid_string_status(self, adapter_with_ws: ElegooAdapter) -> None:
        state = adapter_with_ws._build_state_from_cache({"CurrentStatus": "invalid"})
        assert state.state == PrinterStatus.IDLE  # defaults to 0 → IDLE


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------


class TestGetState:
    """Tests for the get_state method."""

    def test_returns_offline_when_connection_fails(self) -> None:
        adapter = _adapter()
        with mock.patch.object(adapter, "_ensure_ws", side_effect=PrinterError("fail")):
            state = adapter.get_state()
        assert state.state == PrinterStatus.OFFLINE
        assert state.connected is False

    def test_returns_idle_when_no_status(self) -> None:
        adapter = _adapter()
        with mock.patch.object(adapter, "_ensure_ws"):
            with mock.patch.object(adapter, "_send_command"):
                adapter._connected = True
                state = adapter.get_state()
        assert state.state == PrinterStatus.IDLE

    def test_returns_cached_state_during_backoff(self) -> None:
        adapter = _adapter()
        adapter._backoff.record_failure()
        adapter._backoff.next_retry_time = time.monotonic() + 100  # far future
        adapter._last_status = {"CurrentStatus": 13, "TempOfNozzle": 200.0}
        adapter._last_state_time = time.monotonic()
        state = adapter.get_state()
        assert state.state == PrinterStatus.PRINTING
        assert state.tool_temp_actual == 200.0

    def test_returns_offline_during_backoff_without_cache(self) -> None:
        adapter = _adapter()
        adapter._backoff.record_failure()
        adapter._backoff.next_retry_time = time.monotonic() + 100
        adapter._last_status = {}
        state = adapter.get_state()
        assert state.state == PrinterStatus.OFFLINE


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


class TestGetJob:
    """Tests for the get_job method."""

    def test_empty_status_returns_empty_progress(self, adapter_with_ws: ElegooAdapter) -> None:
        adapter_with_ws._last_status = {}
        job = adapter_with_ws.get_job()
        assert job.file_name is None
        assert job.completion is None

    def test_job_with_progress(self, adapter_with_ws: ElegooAdapter) -> None:
        adapter_with_ws._last_status = {
            "Filename": "test.gcode",
            "Progress": 45.0,
            "CurrentTicks": 3600,
            "TotalTicks": 7200,
        }
        job = adapter_with_ws.get_job()
        assert job.file_name == "test.gcode"
        assert job.completion == 45.0
        assert job.print_time_seconds == 3600
        assert job.print_time_left_seconds == 3600

    def test_job_clamps_completion(self, adapter_with_ws: ElegooAdapter) -> None:
        adapter_with_ws._last_status = {"Progress": 150.0}
        job = adapter_with_ws.get_job()
        assert job.completion == 100.0

    def test_job_alternate_keys(self, adapter_with_ws: ElegooAdapter) -> None:
        adapter_with_ws._last_status = {
            "PrintFilename": "alt.gcode",
            "PrintProgress": 50.0,
            "PrintTime": 1800,
            "PrintTimeTotal": 3600,
        }
        job = adapter_with_ws.get_job()
        assert job.file_name == "alt.gcode"
        assert job.completion == 50.0


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


class TestListFiles:
    """Tests for the list_files method."""

    def test_list_files_success(self, adapter_with_ws: ElegooAdapter) -> None:
        response = {
            "Data": {
                "Ack": 0,
                "FileList": [
                    {"name": "test.gcode", "path": "/local/test.gcode", "size": 1024, "date": 1700000000},
                    {"name": "model.ctb", "path": "/local/model.ctb", "size": 2048},
                ],
            },
        }
        with mock.patch.object(adapter_with_ws, "_send_command_checked", return_value=response):
            files = adapter_with_ws.list_files()
        assert len(files) == 2
        assert files[0].name == "test.gcode"
        assert files[0].size_bytes == 1024
        assert files[1].name == "model.ctb"

    def test_list_files_empty(self, adapter_with_ws: ElegooAdapter) -> None:
        response = {"Data": {"Ack": 0, "FileList": []}}
        with mock.patch.object(adapter_with_ws, "_send_command_checked", return_value=response):
            files = adapter_with_ws.list_files()
        assert files == []

    def test_list_files_error(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(
            adapter_with_ws,
            "_send_command_checked",
            side_effect=PrinterError("fail"),
        ):
            with pytest.raises(PrinterError):
                adapter_with_ws.list_files()


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------


class TestUploadFile:
    """Tests for the upload_file method."""

    def test_file_not_found(self, adapter_with_ws: ElegooAdapter) -> None:
        with pytest.raises(FileNotFoundError, match="Local file not found"):
            adapter_with_ws.upload_file("/nonexistent/file.gcode")

    def test_upload_success(self, adapter_with_ws: ElegooAdapter) -> None:
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\nG1 X10 Y10\n")
            tmp_path = f.name

        try:
            from kiln.printers.elegoo import _UploadHTTPHandler

            def _fake_send_command_checked(cmd: int, data: Any = None, *, timeout: float = 10.0) -> dict:
                # Simulate the printer downloading the file after the command.
                _UploadHTTPHandler._served = True
                return {"Data": {"Ack": 0}}

            with mock.patch.object(
                adapter_with_ws,
                "_send_command_checked",
                side_effect=_fake_send_command_checked,
            ):
                result = adapter_with_ws.upload_file(tmp_path)
            assert result.success is True
            assert os.path.basename(tmp_path) in result.file_name
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Print control
# ---------------------------------------------------------------------------


class TestPrintControl:
    """Tests for start/pause/resume/cancel/emergency_stop."""

    def test_start_print(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "_send_command_checked") as m:
            result = adapter_with_ws.start_print("test.gcode")
        assert result.success is True
        assert "test.gcode" in result.message
        # Verify the right command was sent.
        call_args = m.call_args
        assert call_args[0][0] == _CMD_START_PRINT

    def test_cancel_print(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "_send_command_checked"):
            result = adapter_with_ws.cancel_print()
        assert result.success is True
        assert "cancelled" in result.message.lower()

    def test_pause_print(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "_send_command_checked"):
            result = adapter_with_ws.pause_print()
        assert result.success is True
        assert "paused" in result.message.lower()

    def test_resume_print(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "_send_command_checked"):
            result = adapter_with_ws.resume_print()
        assert result.success is True
        assert "resumed" in result.message.lower()

    def test_emergency_stop(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "_send_command"):
            with mock.patch.object(adapter_with_ws, "send_gcode"):
                result = adapter_with_ws.emergency_stop()
        assert result.success is True
        assert "emergency" in result.message.lower()

    def test_start_print_strips_path(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "_send_command_checked") as m:
            adapter_with_ws.start_print("/local/subdir/test.gcode")
        data = m.call_args[0][1]
        assert data["Filename"] == "test.gcode"

    def test_start_print_error(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(
            adapter_with_ws,
            "_send_command_checked",
            side_effect=PrinterError("fail"),
        ):
            with pytest.raises(PrinterError):
                adapter_with_ws.start_print("test.gcode")


# ---------------------------------------------------------------------------
# Temperature control
# ---------------------------------------------------------------------------


class TestTemperatureControl:
    """Tests for set_tool_temp and set_bed_temp."""

    def test_set_tool_temp(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "send_gcode") as m:
            result = adapter_with_ws.set_tool_temp(210.0)
        assert result is True
        m.assert_called_once_with(["M104 S210"])

    def test_set_bed_temp(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "send_gcode") as m:
            result = adapter_with_ws.set_bed_temp(60.0)
        assert result is True
        m.assert_called_once_with(["M140 S60"])

    def test_set_tool_temp_zero(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "send_gcode"):
            result = adapter_with_ws.set_tool_temp(0.0)
        assert result is True

    def test_set_bed_temp_zero(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "send_gcode"):
            result = adapter_with_ws.set_bed_temp(0.0)
        assert result is True


# ---------------------------------------------------------------------------
# G-code
# ---------------------------------------------------------------------------


class TestSendGcode:
    """Tests for send_gcode."""

    def test_send_single_command(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "_send_command") as m:
            result = adapter_with_ws.send_gcode(["G28"])
        assert result is True
        assert m.call_count == 1

    def test_send_multiple_commands(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "_send_command") as m:
            result = adapter_with_ws.send_gcode(["G28", "G1 X10", "G1 Y10"])
        assert result is True
        assert m.call_count == 3

    def test_send_gcode_error(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(
            adapter_with_ws,
            "_send_command",
            side_effect=PrinterError("fail"),
        ):
            with pytest.raises(PrinterError):
                adapter_with_ws.send_gcode(["G28"])


# ---------------------------------------------------------------------------
# File deletion
# ---------------------------------------------------------------------------


class TestDeleteFile:
    """Tests for delete_file."""

    def test_delete_file_success(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "_send_command_checked"):
            result = adapter_with_ws.delete_file("/local/test.gcode")
        assert result is True

    def test_delete_file_strips_path(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "_send_command_checked") as m:
            adapter_with_ws.delete_file("/local/subdir/test.gcode")
        data = m.call_args[0][1]
        assert data["Filename"] == "test.gcode"

    def test_delete_file_error(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(
            adapter_with_ws,
            "_send_command_checked",
            side_effect=PrinterError("fail"),
        ):
            with pytest.raises(PrinterError):
                adapter_with_ws.delete_file("test.gcode")


# ---------------------------------------------------------------------------
# WebSocket connection management
# ---------------------------------------------------------------------------


class TestWebSocketConnection:
    """Tests for WebSocket connection and backoff."""

    def test_ensure_ws_raises_during_backoff(self) -> None:
        adapter = _adapter()
        adapter._backoff.record_failure()
        adapter._backoff.next_retry_time = time.monotonic() + 100
        with pytest.raises(PrinterError, match="backoff cooldown"):
            adapter._ensure_ws()

    def test_ensure_ws_import_error(self) -> None:
        adapter = _adapter()
        with mock.patch.dict("sys.modules", {"websocket": None}):
            with mock.patch("builtins.__import__", side_effect=ImportError("no module")):
                with pytest.raises(PrinterError, match="websocket-client"):
                    adapter._ensure_ws()

    def test_disconnect(self, adapter_with_ws: ElegooAdapter) -> None:
        adapter_with_ws.disconnect()
        assert adapter_with_ws._ws is None
        assert adapter_with_ws._connected is False


# ---------------------------------------------------------------------------
# Backoff state
# ---------------------------------------------------------------------------


class TestBackoffState:
    """Tests for _BackoffState dataclass."""

    def test_initial_state(self) -> None:
        bs = _BackoffState()
        assert bs.attempt_count == 0
        assert bs.in_cooldown() is False

    def test_record_failure(self) -> None:
        bs = _BackoffState()
        bs.record_failure()
        assert bs.attempt_count == 1
        assert bs.in_cooldown() is True

    def test_record_success_resets(self) -> None:
        bs = _BackoffState()
        bs.record_failure()
        bs.record_failure()
        bs.record_success()
        assert bs.attempt_count == 0
        assert bs.in_cooldown() is False

    def test_exponential_backoff(self) -> None:
        bs = _BackoffState()
        bs.record_failure()  # delay = 1s
        first_retry = bs.next_retry_time
        bs.record_failure()  # delay = 2s
        second_retry = bs.next_retry_time
        # Second retry should be further in the future.
        assert second_retry > first_retry


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------


class TestMessageHandling:
    """Tests for _handle_message."""

    def test_status_update(self, adapter_with_ws: ElegooAdapter) -> None:
        adapter_with_ws._handle_message({
            "Data": {
                "Cmd": 0,
                "Data": {"CurrentStatus": 13, "TempOfNozzle": 210.0},
                "RequestID": "",
                "MainboardID": MAINBOARD_ID,
            },
        })
        assert adapter_with_ws._last_status.get("CurrentStatus") == 13
        assert adapter_with_ws._last_status.get("TempOfNozzle") == 210.0

    def test_mainboard_id_discovery(self) -> None:
        adapter = ElegooAdapter(host=HOST)
        assert adapter._mainboard_id == ""
        adapter._handle_message({
            "Data": {
                "MainboardID": "DISCOVERED123",
                "Data": {},
            },
        })
        assert adapter._mainboard_id == "DISCOVERED123"

    def test_pending_response(self, adapter_with_ws: ElegooAdapter) -> None:
        import threading
        event = threading.Event()
        request_id = "test-req-123"
        adapter_with_ws._pending[request_id] = event

        adapter_with_ws._handle_message({
            "Data": {
                "Cmd": 258,
                "Data": {"FileList": []},
                "RequestID": request_id,
                "MainboardID": MAINBOARD_ID,
            },
        })
        assert event.is_set()
        assert request_id in adapter_with_ws._responses

    def test_invalid_data_ignored(self, adapter_with_ws: ElegooAdapter) -> None:
        # Should not raise.
        adapter_with_ws._handle_message({"Data": "not a dict"})
        adapter_with_ws._handle_message({})


# ---------------------------------------------------------------------------
# Send command
# ---------------------------------------------------------------------------


class TestSendCommand:
    """Tests for _send_command and _send_command_checked."""

    def test_send_command_fire_and_forget(self, adapter_with_ws: ElegooAdapter) -> None:
        result = adapter_with_ws._send_command(_CMD_STATUS_REQUEST)
        assert result is None
        adapter_with_ws._ws.send.assert_called_once()

    def test_send_command_with_timeout(self, adapter_with_ws: ElegooAdapter) -> None:
        # The command will time out because no response handler fires.
        result = adapter_with_ws._send_command(_CMD_STATUS_REQUEST, timeout=0.1)
        assert result is None  # timed out

    def test_send_command_raises_on_ws_error(self, adapter_with_ws: ElegooAdapter) -> None:
        adapter_with_ws._ws.send.side_effect = Exception("ws broken")
        with pytest.raises(PrinterError, match="Failed to send"):
            adapter_with_ws._send_command(_CMD_STATUS_REQUEST)

    def test_send_command_checked_no_response(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "_send_command", return_value=None):
            with pytest.raises(PrinterError, match="No response"):
                adapter_with_ws._send_command_checked(_CMD_START_PRINT)

    def test_send_command_checked_ack_failure(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(
            adapter_with_ws,
            "_send_command",
            return_value={"Data": {"Ack": 1}},
        ):
            with pytest.raises(PrinterError, match="failed with ack code"):
                adapter_with_ws._send_command_checked(_CMD_START_PRINT)

    def test_send_command_checked_success(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(
            adapter_with_ws,
            "_send_command",
            return_value={"Data": {"Ack": 0, "FileList": []}},
        ):
            resp = adapter_with_ws._send_command_checked(_CMD_LIST_FILES)
        assert resp is not None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    """Tests for UDP discovery."""

    def test_discover_no_printers(self) -> None:
        with mock.patch("socket.socket") as mock_socket_cls:
            mock_sock = mock.MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.recvfrom.side_effect = socket.timeout()
            results = ElegooAdapter.discover(timeout=0.1)
        assert results == []

    def test_discover_returns_printer(self) -> None:
        response_data = json.dumps({
            "MainboardID": MAINBOARD_ID,
            "Name": "Centauri Carbon",
            "MachineName": "Centauri Carbon",
            "FirmwareVersion": "1.2.3",
        }).encode("utf-8")

        with mock.patch("socket.socket") as mock_socket_cls:
            mock_sock = mock.MagicMock()
            mock_socket_cls.return_value = mock_sock
            # First recvfrom returns data, second times out.
            mock_sock.recvfrom.side_effect = [
                (response_data, ("192.168.1.50", 3000)),
                socket.timeout(),
            ]
            results = ElegooAdapter.discover(timeout=0.1)

        assert len(results) == 1
        assert results[0]["host"] == "192.168.1.50"
        assert results[0]["mainboard_id"] == MAINBOARD_ID
        assert results[0]["type"] == "elegoo"


# ---------------------------------------------------------------------------
# Stream URL
# ---------------------------------------------------------------------------


class TestStreamUrl:
    """Tests for get_stream_url."""

    def test_fallback_stream_url(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(adapter_with_ws, "_send_command", return_value=None):
            url = adapter_with_ws.get_stream_url()
        assert url is not None
        assert HOST in url

    def test_stream_url_from_response(self, adapter_with_ws: ElegooAdapter) -> None:
        with mock.patch.object(
            adapter_with_ws,
            "_send_command",
            return_value={"Data": {"StreamUrl": "http://192.168.1.50:8080/stream"}},
        ):
            url = adapter_with_ws.get_stream_url()
        assert url == "http://192.168.1.50:8080/stream"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    """Tests for _safe_float and _safe_int."""

    def test_safe_float_valid(self) -> None:
        assert _safe_float(42.5) == 42.5
        assert _safe_float("42.5") == 42.5
        assert _safe_float(0) == 0.0

    def test_safe_float_none(self) -> None:
        assert _safe_float(None) is None

    def test_safe_float_invalid(self) -> None:
        assert _safe_float("not a number") is None
        assert _safe_float([]) is None

    def test_safe_int_valid(self) -> None:
        assert _safe_int(42) == 42
        assert _safe_int("42") == 42
        assert _safe_int(0) == 0

    def test_safe_int_none(self) -> None:
        assert _safe_int(None) is None

    def test_safe_int_invalid(self) -> None:
        assert _safe_int("not a number") is None
        assert _safe_int([]) is None

    def test_safe_int_float(self) -> None:
        assert _safe_int(42.9) == 42
