"""Tests for kiln.printers.serial_adapter -- SerialPrinterAdapter with mocked pyserial.

Uses ``unittest.mock`` to mock all pyserial interactions so that tests are
fast, deterministic, and do not require a real serial port or printer.

Covers:
- Constructor validation and pyserial import guard
- connect() / disconnect() / is_connected / auto-reconnect
- _send_command() protocol: ok, error, timeout, write failures
- _send_and_parse() key:value parsing
- _parse_temps() M105 response parsing
- get_state() with temperatures and SD print status
- get_state() offline when serial port is closed
- get_job() SD progress parsing
- list_files() M20 file listing parsing
- upload_file() SD write protocol (M28/lines/M29)
- upload_file() FileNotFoundError and PermissionError
- delete_file() M30
- start_print() M23+M24 sequence
- cancel_print() M524 with M0 fallback
- pause_print() M25
- resume_print() M24
- emergency_stop() M112 without waiting for ok
- set_tool_temp() with M104 and validation
- set_bed_temp() with M140 and validation
- send_gcode() multiple commands
- get_firmware_status() M115 parsing
- firmware_resume_print() parameter validation and G-code sequence
- PrinterError wrapping for all serial error types
- capabilities and name properties
- repr
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import threading
import time
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from kiln.printers.base import (
    FirmwareStatus,
    JobProgress,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)


# ---------------------------------------------------------------------------
# Helpers for mocking pyserial
# ---------------------------------------------------------------------------

class _FakeSerialException(Exception):
    """Stand-in for serial.SerialException when pyserial is not installed."""


def _make_serial_module() -> ModuleType:
    """Create a fake ``serial`` module with a mockable ``Serial`` class."""
    mod = ModuleType("serial")
    mod.SerialException = _FakeSerialException  # type: ignore[attr-defined]
    mod.Serial = MagicMock  # type: ignore[attr-defined]  # replaced per-test
    return mod


# Ensure a usable serial module is in sys.modules for all tests in this file.
_fake_serial_mod = _make_serial_module()


@pytest.fixture(autouse=True)
def _ensure_serial_module():
    """Inject a fake serial module before every test so imports succeed."""
    old = sys.modules.get("serial")
    sys.modules["serial"] = _fake_serial_mod
    # Re-import the adapter module so it picks up the fake serial.
    import kiln.printers.serial_adapter as _mod
    importlib.reload(_mod)
    yield
    if old is not None:
        sys.modules["serial"] = old
    elif "serial" in sys.modules:
        del sys.modules["serial"]


def _make_mock_serial(
    *,
    readline_responses: list[bytes] | None = None,
    is_open: bool = True,
) -> MagicMock:
    """Create a mock serial.Serial instance with configurable responses.

    Args:
        readline_responses: Sequence of bytes that ``readline()`` returns
            on successive calls.  After exhaustion, returns ``b""``.
        is_open: Initial ``is_open`` property value.
    """
    mock_ser = MagicMock()
    mock_ser.is_open = is_open

    responses = list(readline_responses or [])
    response_iter = iter(responses)

    def _readline():
        try:
            return next(response_iter)
        except StopIteration:
            return b""

    mock_ser.readline.side_effect = _readline
    mock_ser.timeout = 10
    mock_ser.write.return_value = None
    mock_ser.flush.return_value = None
    mock_ser.reset_input_buffer.return_value = None
    mock_ser.close.return_value = None

    return mock_ser


def _build_adapter(
    mock_serial_instance: MagicMock,
    *,
    port: str = "/dev/ttyUSB0",
    baudrate: int = 115200,
    timeout: int = 10,
    printer_name: str = "test-serial",
):
    """Construct a SerialPrinterAdapter with a mocked serial port."""
    from kiln.printers.serial_adapter import SerialPrinterAdapter

    # Make serial.Serial() return our mock instance.
    _fake_serial_mod.Serial = MagicMock(return_value=mock_serial_instance)  # type: ignore[attr-defined]
    _fake_serial_mod.SerialException = _FakeSerialException  # type: ignore[attr-defined]

    with patch.object(SerialPrinterAdapter, "_wait_for_startup"):
        adapter = SerialPrinterAdapter(
            port=port,
            baudrate=baudrate,
            timeout=timeout,
            printer_name=printer_name,
        )
    return adapter


# ---------------------------------------------------------------------------
# Constructor tests
# ---------------------------------------------------------------------------

class TestSerialPrinterAdapterConstructor:
    """Tests for SerialPrinterAdapter.__init__."""

    def test_empty_port_raises(self):
        from kiln.printers.serial_adapter import SerialPrinterAdapter

        mock_ser = _make_mock_serial()
        _fake_serial_mod.Serial = MagicMock(return_value=mock_ser)  # type: ignore[attr-defined]

        with pytest.raises(ValueError, match="port must not be empty"):
            SerialPrinterAdapter(port="", printer_name="test")

    def test_valid_construction(self):
        mock_ser = _make_mock_serial(readline_responses=[b"start\n", b"ok\n"])
        adapter = _build_adapter(mock_ser)
        assert adapter.name == "test-serial"
        assert adapter._port == "/dev/ttyUSB0"
        assert adapter._baudrate == 115200
        assert adapter._timeout == 10

    def test_custom_baudrate_and_timeout(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser, baudrate=250000, timeout=5)
        assert adapter._baudrate == 250000
        assert adapter._timeout == 5

    def test_pyserial_import_error(self):
        """When pyserial is not installed, constructor raises PrinterError."""
        old = sys.modules.get("serial")
        sys.modules["serial"] = None  # type: ignore[assignment]
        try:
            import kiln.printers.serial_adapter as mod
            importlib.reload(mod)
            with pytest.raises(PrinterError, match="pyserial is required"):
                mod.SerialPrinterAdapter(port="/dev/ttyUSB0")
        finally:
            if old is not None:
                sys.modules["serial"] = old
            else:
                sys.modules["serial"] = _fake_serial_mod
            import kiln.printers.serial_adapter as mod2
            importlib.reload(mod2)

    def test_capabilities(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        caps = adapter.capabilities
        assert isinstance(caps, PrinterCapabilities)
        assert caps.can_upload is True
        assert caps.can_set_temp is True
        assert caps.can_send_gcode is True
        assert caps.can_pause is True
        assert caps.can_stream is False
        assert caps.can_snapshot is False
        assert caps.can_update_firmware is False

    def test_repr(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        assert "SerialPrinterAdapter" in repr(adapter)
        assert "/dev/ttyUSB0" in repr(adapter)


# ---------------------------------------------------------------------------
# Connection management tests
# ---------------------------------------------------------------------------

class TestConnectionManagement:
    """Tests for connect/disconnect/is_connected/reconnect."""

    def test_is_connected_when_port_open(self):
        mock_ser = _make_mock_serial(is_open=True)
        adapter = _build_adapter(mock_ser)
        assert adapter.is_connected is True

    def test_is_connected_false_when_port_closed(self):
        mock_ser = _make_mock_serial(is_open=False)
        adapter = _build_adapter(mock_ser)
        adapter._connected = True
        assert adapter.is_connected is False

    def test_disconnect(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        assert adapter.is_connected is True
        adapter.disconnect()
        assert adapter._connected is False
        assert adapter._serial is None

    def test_connect_already_connected_is_noop(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        # Calling connect again should not raise or change state.
        adapter.connect()
        assert adapter.is_connected is True

    def test_connect_permission_denied(self):
        """SerialException with 'permission' maps to helpful error message."""
        from kiln.printers.serial_adapter import SerialPrinterAdapter

        _fake_serial_mod.Serial = MagicMock(  # type: ignore[attr-defined]
            side_effect=_FakeSerialException("permission denied"),
        )

        with pytest.raises(PrinterError, match="Permission denied"):
            with patch.object(SerialPrinterAdapter, "_wait_for_startup"):
                SerialPrinterAdapter(port="/dev/ttyUSB0")

    def test_connect_port_not_found(self):
        """SerialException with 'no such file' maps to helpful error message."""
        from kiln.printers.serial_adapter import SerialPrinterAdapter

        _fake_serial_mod.Serial = MagicMock(  # type: ignore[attr-defined]
            side_effect=_FakeSerialException("no such file or directory"),
        )

        with pytest.raises(PrinterError, match="not found"):
            with patch.object(SerialPrinterAdapter, "_wait_for_startup"):
                SerialPrinterAdapter(port="/dev/ttyUSB0")

    def test_connect_generic_serial_error(self):
        """Generic SerialException wraps the error."""
        from kiln.printers.serial_adapter import SerialPrinterAdapter

        _fake_serial_mod.Serial = MagicMock(  # type: ignore[attr-defined]
            side_effect=_FakeSerialException("some other error"),
        )

        with pytest.raises(PrinterError, match="Failed to open serial port"):
            with patch.object(SerialPrinterAdapter, "_wait_for_startup"):
                SerialPrinterAdapter(port="/dev/ttyUSB0")

    def test_reconnect_on_connection_loss(self):
        """_ensure_connected attempts reconnection when disconnected."""
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        adapter._connected = False
        mock_ser.is_open = False

        with patch.object(adapter, "connect") as mock_connect:
            def _reconnect():
                adapter._connected = True
                mock_ser.is_open = True
            mock_connect.side_effect = _reconnect
            adapter._ensure_connected()
            mock_connect.assert_called_once()

    def test_reconnect_exhaustion_raises(self):
        """_ensure_connected raises after max reconnect attempts."""
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        adapter._connected = False
        mock_ser.is_open = False

        with patch.object(adapter, "connect", side_effect=PrinterError("fail")):
            with patch("kiln.printers.serial_adapter.time.sleep"):
                with pytest.raises(PrinterError, match="reconnect attempts"):
                    adapter._ensure_connected()


# ---------------------------------------------------------------------------
# G-code command/response protocol tests
# ---------------------------------------------------------------------------

class TestSendCommand:
    """Tests for _send_command protocol handling."""

    def test_send_receives_ok(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter._send_command("G28")
        assert "ok" in result.lower()

    def test_send_receives_multiline_then_ok(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"echo: some info\n",
            b"ok T:20.0 /0.0 B:20.0 /0.0\n",
        ])
        adapter = _build_adapter(mock_ser)
        result = adapter._send_command("M105")
        assert "T:20.0" in result
        assert "ok" in result.lower()

    def test_send_firmware_error_raises(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"Error:Printer halted. kill() called!\n",
        ])
        adapter = _build_adapter(mock_ser)
        with pytest.raises(PrinterError, match="Firmware error"):
            adapter._send_command("M104 S300")

    def test_send_error_colon_raises(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"error: some problem\n",
        ])
        adapter = _build_adapter(mock_ser)
        with pytest.raises(PrinterError, match="Firmware error"):
            adapter._send_command("G28")

    def test_send_timeout_raises(self):
        """When no ok is received within timeout, PrinterError is raised."""
        mock_ser = _make_mock_serial(readline_responses=[])
        adapter = _build_adapter(mock_ser)

        with patch("kiln.printers.serial_adapter.time.monotonic") as mock_time:
            mock_time.side_effect = [
                0.0,   # _ensure_connected check (is already connected)
                0.0,   # deadline calculation
                0.0,   # while check
                100.0, # while check (exceeds deadline)
            ]
            with pytest.raises(PrinterError, match="Timeout"):
                adapter._send_command("M105", timeout=1.0)

    def test_send_write_failure_marks_disconnected(self):
        mock_ser = _make_mock_serial()
        mock_ser.write.side_effect = OSError("write failed")
        adapter = _build_adapter(mock_ser)

        with pytest.raises(PrinterError, match="Failed to send"):
            adapter._send_command("G28")
        assert adapter._connected is False

    def test_send_read_failure_marks_disconnected(self):
        mock_ser = _make_mock_serial()
        mock_ser.readline.side_effect = OSError("read failed")
        adapter = _build_adapter(mock_ser)

        with pytest.raises(PrinterError, match="Serial read error"):
            adapter._send_command("G28")
        assert adapter._connected is False

    def test_send_without_wait_for_ok(self):
        """M112 style: send and don't wait for ok."""
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        result = adapter._send_command("M112", wait_for_ok=False)
        assert result == ""
        mock_ser.write.assert_called()


# ---------------------------------------------------------------------------
# Response parsing tests
# ---------------------------------------------------------------------------

class TestSendAndParse:
    """Tests for _send_and_parse and _parse_response."""

    def test_parse_temperature_response(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"ok T:210.5 /210.0 B:60.3 /60.0 @:127 B@:64\n",
        ])
        adapter = _build_adapter(mock_ser)
        result = adapter._send_and_parse("M105")
        assert "T" in result
        assert result["T"] == 210.5

    def test_parse_response_static(self):
        from kiln.printers.serial_adapter import SerialPrinterAdapter
        result = SerialPrinterAdapter._parse_response("ok T:200.0 B:55.5 @:127")
        assert result["T"] == 200.0
        assert result["B"] == 55.5

    def test_parse_response_non_numeric_values(self):
        from kiln.printers.serial_adapter import SerialPrinterAdapter
        result = SerialPrinterAdapter._parse_response("FIRMWARE_NAME:Marlin ok")
        assert result["FIRMWARE_NAME"] == "Marlin"


# ---------------------------------------------------------------------------
# Temperature parsing tests
# ---------------------------------------------------------------------------

class TestParseTemps:
    """Tests for _parse_temps M105 response parsing."""

    def test_standard_m105(self):
        from kiln.printers.serial_adapter import SerialPrinterAdapter
        temps = SerialPrinterAdapter._parse_temps(
            "ok T:210.0 /210.0 B:60.0 /60.0 @:127 B@:127"
        )
        assert temps["tool_actual"] == 210.0
        assert temps["tool_target"] == 210.0
        assert temps["bed_actual"] == 60.0
        assert temps["bed_target"] == 60.0

    def test_cold_printer(self):
        from kiln.printers.serial_adapter import SerialPrinterAdapter
        temps = SerialPrinterAdapter._parse_temps(
            "ok T:22.5 /0.0 B:21.3 /0.0"
        )
        assert temps["tool_actual"] == 22.5
        assert temps["tool_target"] == 0.0
        assert temps["bed_actual"] == 21.3
        assert temps["bed_target"] == 0.0

    def test_no_bed(self):
        from kiln.printers.serial_adapter import SerialPrinterAdapter
        temps = SerialPrinterAdapter._parse_temps("ok T:200.0 /200.0")
        assert temps["tool_actual"] == 200.0
        assert temps["tool_target"] == 200.0
        assert temps["bed_actual"] is None
        assert temps["bed_target"] is None

    def test_empty_response(self):
        from kiln.printers.serial_adapter import SerialPrinterAdapter
        temps = SerialPrinterAdapter._parse_temps("")
        assert temps["tool_actual"] is None
        assert temps["tool_target"] is None
        assert temps["bed_actual"] is None
        assert temps["bed_target"] is None

    def test_garbage_response(self):
        from kiln.printers.serial_adapter import SerialPrinterAdapter
        temps = SerialPrinterAdapter._parse_temps("echo: garbage line\nok")
        assert temps["tool_actual"] is None


# ---------------------------------------------------------------------------
# get_state() tests
# ---------------------------------------------------------------------------

class TestGetState:
    """Tests for get_state() state/temperature reporting."""

    def test_idle_state(self):
        mock_ser = _make_mock_serial(readline_responses=[
            # M105 response
            b"ok T:22.0 /0.0 B:21.0 /0.0\n",
            # M27 response
            b"Not SD printing\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        state = adapter.get_state()
        assert isinstance(state, PrinterState)
        assert state.connected is True
        assert state.state == PrinterStatus.IDLE
        assert state.tool_temp_actual == 22.0
        assert state.tool_temp_target == 0.0
        assert state.bed_temp_actual == 21.0
        assert state.bed_temp_target == 0.0

    def test_printing_state(self):
        mock_ser = _make_mock_serial(readline_responses=[
            # M105 response
            b"ok T:210.0 /210.0 B:60.0 /60.0\n",
            # M27 response
            b"SD printing byte 5000/10000\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        state = adapter.get_state()
        assert state.connected is True
        assert state.state == PrinterStatus.PRINTING
        assert state.tool_temp_actual == 210.0

    def test_offline_when_not_connected(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        adapter._connected = False
        mock_ser.is_open = False
        state = adapter.get_state()
        assert state.connected is False
        assert state.state == PrinterStatus.OFFLINE

    def test_offline_on_serial_error(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        with patch.object(adapter, "_send_command", side_effect=PrinterError("dead")):
            state = adapter.get_state()
            assert state.connected is False
            assert state.state == PrinterStatus.OFFLINE

    def test_print_complete_maps_to_idle(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"ok T:22.0 /0.0 B:21.0 /0.0\n",
            b"SD printing byte 10000/10000\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        state = adapter.get_state()
        assert state.state == PrinterStatus.IDLE

    def test_m27_failure_defaults_to_idle(self):
        """If M27 fails, get_state still returns IDLE (not crash)."""
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        call_count = [0]

        def _selective_send(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return "ok T:22.0 /0.0 B:21.0 /0.0"
            raise PrinterError("M27 failed")

        with patch.object(adapter, "_send_command", side_effect=_selective_send):
            state = adapter.get_state()
            assert state.state == PrinterStatus.IDLE

    def test_to_dict_serializes_state(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"ok T:22.0 /0.0 B:21.0 /0.0\n",
            b"Not SD printing\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        state = adapter.get_state()
        d = state.to_dict()
        assert d["state"] == "idle"
        assert d["connected"] is True


# ---------------------------------------------------------------------------
# get_job() tests
# ---------------------------------------------------------------------------

class TestGetJob:
    """Tests for get_job() SD progress parsing."""

    def test_active_print_progress(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"SD printing byte 2500/10000\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        adapter._current_file = "BENCHY.GCO"
        job = adapter.get_job()
        assert isinstance(job, JobProgress)
        assert job.file_name == "BENCHY.GCO"
        assert job.completion == 25.0

    def test_no_active_print(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"Not SD printing\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        job = adapter.get_job()
        assert job.completion is None

    def test_job_when_disconnected(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        adapter._connected = False
        mock_ser.is_open = False
        job = adapter.get_job()
        assert isinstance(job, JobProgress)
        assert job.completion is None

    def test_job_on_serial_error(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        with patch.object(adapter, "_send_command", side_effect=PrinterError("fail")):
            job = adapter.get_job()
            assert job.completion is None

    def test_job_progress_100_percent(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"SD printing byte 10000/10000\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        job = adapter.get_job()
        assert job.completion == 100.0

    def test_job_progress_zero_total(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"SD printing byte 0/0\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        job = adapter.get_job()
        assert job.completion == 0.0


# ---------------------------------------------------------------------------
# list_files() tests
# ---------------------------------------------------------------------------

class TestListFiles:
    """Tests for list_files() SD card file listing."""

    def test_standard_file_listing(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"Begin file list\n",
            b"BENCHY.GCO\n",
            b"CALIBRA~1.GCO\n",
            b"VASE.GCO 54321\n",
            b"End file list\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        files = adapter.list_files()
        assert len(files) == 3
        assert all(isinstance(f, PrinterFile) for f in files)
        assert files[0].name == "BENCHY.GCO"
        assert files[0].path == "BENCHY.GCO"
        assert files[1].name == "CALIBRA~1.GCO"
        assert files[2].name == "VASE.GCO"
        assert files[2].size_bytes == 54321

    def test_empty_file_listing(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"Begin file list\n",
            b"End file list\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        files = adapter.list_files()
        assert files == []

    def test_no_sd_card_error(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        with patch.object(
            adapter, "_send_command",
            side_effect=PrinterError("error: no SD card"),
        ):
            with pytest.raises(PrinterError, match="No SD card"):
                adapter.list_files()

    def test_parse_file_list_static(self):
        from kiln.printers.serial_adapter import SerialPrinterAdapter
        response = (
            "Begin file list\n"
            "FILE1.GCO\n"
            "FILE2.GCO 12345\n"
            "End file list\n"
            "ok"
        )
        files = SerialPrinterAdapter._parse_file_list(response)
        assert len(files) == 2
        assert files[0].name == "FILE1.GCO"
        assert files[0].size_bytes is None
        assert files[1].size_bytes == 12345

    def test_generic_list_files_error_propagates(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        with patch.object(
            adapter, "_send_command",
            side_effect=PrinterError("generic error"),
        ):
            with pytest.raises(PrinterError, match="generic error"):
                adapter.list_files()


# ---------------------------------------------------------------------------
# upload_file() tests
# ---------------------------------------------------------------------------

class TestUploadFile:
    """Tests for upload_file() SD write protocol."""

    def test_successful_upload(self):
        mock_ser = _make_mock_serial(readline_responses=[
            # M28 response
            b"Writing to file: TEST.GCO\n",
            b"ok\n",
            # M29 response
            b"Done saving file.\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".gcode", delete=False,
        ) as f:
            f.write("G28\n")
            f.write("; comment line\n")
            f.write("G1 X10 Y10 Z5 F1200\n")
            f.write("\n")
            f.write("M104 S200\n")
            temp_path = f.name

        try:
            result = adapter.upload_file(temp_path)
            assert isinstance(result, UploadResult)
            assert result.success is True
        finally:
            os.unlink(temp_path)

    def test_upload_file_not_found(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        with pytest.raises(FileNotFoundError, match="not found"):
            adapter.upload_file("/nonexistent/path/test.gcode")

    def test_upload_truncates_long_filename(self):
        """Filenames longer than 8.3 are truncated for SD card compatibility."""
        mock_ser = _make_mock_serial(readline_responses=[
            b"Writing to file\n",
            b"ok\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)

        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix="very_long_filename_here_",
            suffix=".gcode",
            delete=False,
        ) as f:
            f.write("G28\n")
            temp_path = f.name

        try:
            result = adapter.upload_file(temp_path)
            assert result.success is True
            assert len(result.file_name) <= 12
        finally:
            os.unlink(temp_path)


# ---------------------------------------------------------------------------
# delete_file() tests
# ---------------------------------------------------------------------------

class TestDeleteFile:
    """Tests for delete_file() M30 command."""

    def test_successful_delete(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.delete_file("BENCHY.GCO")
        assert result is True

    def test_delete_failure_raises(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        with patch.object(
            adapter, "_send_command",
            side_effect=PrinterError("M30 failed"),
        ):
            with pytest.raises(PrinterError, match="M30 failed"):
                adapter.delete_file("BENCHY.GCO")


# ---------------------------------------------------------------------------
# start_print() tests
# ---------------------------------------------------------------------------

class TestStartPrint:
    """Tests for start_print() M23+M24 sequence."""

    def test_successful_start(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"File opened: BENCHY.GCO Size: 54321\n",
            b"ok\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        result = adapter.start_print("BENCHY.GCO")
        assert isinstance(result, PrintResult)
        assert result.success is True
        assert "BENCHY.GCO" in result.message
        assert adapter._current_file == "BENCHY.GCO"

    def test_start_print_failure_raises(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        with patch.object(
            adapter, "_send_command",
            side_effect=PrinterError("File not found"),
        ):
            with pytest.raises(PrinterError, match="File not found"):
                adapter.start_print("NOFILE.GCO")


# ---------------------------------------------------------------------------
# cancel_print() tests
# ---------------------------------------------------------------------------

class TestCancelPrint:
    """Tests for cancel_print() with M524 and M0 fallback."""

    def test_cancel_via_m524(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        adapter._current_file = "BENCHY.GCO"
        result = adapter.cancel_print()
        assert result.success is True
        assert adapter._current_file is None

    def test_cancel_fallback_to_m0(self):
        """When M524 fails, cancel_print falls back to M0."""
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        adapter._current_file = "BENCHY.GCO"

        call_count = [0]

        def _selective_send(cmd, **kwargs):
            call_count[0] += 1
            if "M524" in cmd:
                raise PrinterError("Unknown command")
            return "ok"

        with patch.object(adapter, "_send_command", side_effect=_selective_send):
            result = adapter.cancel_print()
            assert result.success is True
            assert call_count[0] == 2  # M524 tried, then M0


# ---------------------------------------------------------------------------
# pause_print() / resume_print() tests
# ---------------------------------------------------------------------------

class TestPausePrint:
    """Tests for pause_print() M25 command."""

    def test_successful_pause(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.pause_print()
        assert isinstance(result, PrintResult)
        assert result.success is True
        assert "paused" in result.message.lower()


class TestResumePrint:
    """Tests for resume_print() M24 command."""

    def test_successful_resume(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.resume_print()
        assert isinstance(result, PrintResult)
        assert result.success is True
        assert "resumed" in result.message.lower()


# ---------------------------------------------------------------------------
# emergency_stop() tests
# ---------------------------------------------------------------------------

class TestEmergencyStop:
    """Tests for emergency_stop() M112 command."""

    def test_emergency_stop_sends_m112(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        adapter._current_file = "BENCHY.GCO"
        result = adapter.emergency_stop()
        assert result.success is True
        assert "M112" in result.message
        assert adapter._current_file is None
        assert adapter._connected is False

    def test_emergency_stop_succeeds_even_on_read_error(self):
        """M112 is fire-and-forget; even serial errors don't prevent success."""
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        with patch.object(
            adapter, "_send_command",
            side_effect=PrinterError("read fail"),
        ):
            result = adapter.emergency_stop()
            assert result.success is True


# ---------------------------------------------------------------------------
# set_tool_temp() / set_bed_temp() tests
# ---------------------------------------------------------------------------

class TestSetToolTemp:
    """Tests for set_tool_temp() M104 with validation."""

    def test_set_valid_temp(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.set_tool_temp(200.0)
        assert result is True

    def test_set_zero_temp(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.set_tool_temp(0.0)
        assert result is True

    def test_negative_temp_raises(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        with pytest.raises(PrinterError, match="negative"):
            adapter.set_tool_temp(-10.0)

    def test_over_max_temp_raises(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        with pytest.raises(PrinterError, match="exceeds safety limit"):
            adapter.set_tool_temp(350.0)


class TestSetBedTemp:
    """Tests for set_bed_temp() M140 with validation."""

    def test_set_valid_temp(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.set_bed_temp(60.0)
        assert result is True

    def test_set_zero_temp(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.set_bed_temp(0.0)
        assert result is True

    def test_negative_temp_raises(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        with pytest.raises(PrinterError, match="negative"):
            adapter.set_bed_temp(-5.0)

    def test_over_max_temp_raises(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        with pytest.raises(PrinterError, match="exceeds safety limit"):
            adapter.set_bed_temp(200.0)


# ---------------------------------------------------------------------------
# send_gcode() tests
# ---------------------------------------------------------------------------

class TestSendGcode:
    """Tests for send_gcode() multiple command dispatch."""

    def test_send_multiple_commands(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"ok\n",
            b"ok\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        result = adapter.send_gcode(["G28", "G1 X10 Y10 Z5 F1200", "M104 S200"])
        assert result is True

    def test_send_empty_list(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        result = adapter.send_gcode([])
        assert result is True

    def test_send_gcode_failure_on_second_command(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        call_count = [0]

        def _send_side_effect(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise PrinterError("Command failed")
            return "ok"

        with patch.object(adapter, "_send_command", side_effect=_send_side_effect):
            with pytest.raises(PrinterError, match="Command failed"):
                adapter.send_gcode(["G28", "G1 X999 Y999", "M104 S200"])


# ---------------------------------------------------------------------------
# get_firmware_status() tests
# ---------------------------------------------------------------------------

class TestGetFirmwareStatus:
    """Tests for get_firmware_status() M115 parsing."""

    def test_standard_m115_response(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"FIRMWARE_NAME:Marlin FIRMWARE_VERSION:2.1.2 SOURCE_CODE_URL:github.com\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        status = adapter.get_firmware_status()
        assert isinstance(status, FirmwareStatus)
        assert len(status.components) == 1
        assert status.components[0].name == "Marlin"
        assert status.components[0].current_version == "2.1.2"

    def test_m115_failure_returns_none(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        with patch.object(adapter, "_send_command", side_effect=PrinterError("fail")):
            status = adapter.get_firmware_status()
            assert status is None

    def test_m115_unknown_firmware(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        status = adapter.get_firmware_status()
        assert status is not None
        assert status.components[0].name == "Unknown"
        assert status.components[0].current_version == "Unknown"


# ---------------------------------------------------------------------------
# firmware_resume_print() tests
# ---------------------------------------------------------------------------

class TestFirmwareResumePrint:
    """Tests for firmware_resume_print() Marlin M413 recovery."""

    def _make_resume_adapter(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"] * 20)
        return _build_adapter(mock_ser)

    def test_successful_resume(self):
        adapter = self._make_resume_adapter()
        result = adapter.firmware_resume_print(
            z_height_mm=5.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="BENCHY.GCO",
            layer_number=42,
        )
        assert isinstance(result, PrintResult)
        assert result.success is True
        assert "BENCHY.GCO" in result.message
        assert "layer 42" in result.message

    def test_z_height_zero_raises(self):
        adapter = self._make_resume_adapter()
        with pytest.raises(PrinterError, match="z_height_mm must be > 0"):
            adapter.firmware_resume_print(
                z_height_mm=0.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gco",
            )

    def test_negative_z_height_raises(self):
        adapter = self._make_resume_adapter()
        with pytest.raises(PrinterError, match="z_height_mm must be > 0"):
            adapter.firmware_resume_print(
                z_height_mm=-1.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gco",
            )

    def test_zero_hotend_temp_raises(self):
        adapter = self._make_resume_adapter()
        with pytest.raises(PrinterError, match="Hotend temperature must be > 0"):
            adapter.firmware_resume_print(
                z_height_mm=5.0,
                hotend_temp_c=0.0,
                bed_temp_c=60.0,
                file_name="test.gco",
            )

    def test_hotend_over_max_raises(self):
        adapter = self._make_resume_adapter()
        with pytest.raises(PrinterError, match="exceeds safety limit"):
            adapter.firmware_resume_print(
                z_height_mm=5.0,
                hotend_temp_c=350.0,
                bed_temp_c=60.0,
                file_name="test.gco",
            )

    def test_bed_over_max_raises(self):
        adapter = self._make_resume_adapter()
        with pytest.raises(PrinterError, match="exceeds safety limit"):
            adapter.firmware_resume_print(
                z_height_mm=5.0,
                hotend_temp_c=200.0,
                bed_temp_c=200.0,
                file_name="test.gco",
            )

    def test_negative_prime_length_raises(self):
        adapter = self._make_resume_adapter()
        with pytest.raises(PrinterError, match="prime_length_mm must be >= 0"):
            adapter.firmware_resume_print(
                z_height_mm=5.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gco",
                prime_length_mm=-1.0,
            )

    def test_z_clearance_out_of_range_raises(self):
        adapter = self._make_resume_adapter()
        with pytest.raises(PrinterError, match="z_clearance_mm must be > 0 and <= 10"):
            adapter.firmware_resume_print(
                z_height_mm=5.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gco",
                z_clearance_mm=15.0,
            )

    def test_z_clearance_zero_raises(self):
        adapter = self._make_resume_adapter()
        with pytest.raises(PrinterError, match="z_clearance_mm must be > 0 and <= 10"):
            adapter.firmware_resume_print(
                z_height_mm=5.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gco",
                z_clearance_mm=0.0,
            )

    def test_no_layer_number_in_message(self):
        adapter = self._make_resume_adapter()
        result = adapter.firmware_resume_print(
            z_height_mm=5.0,
            hotend_temp_c=200.0,
            bed_temp_c=60.0,
            file_name="test.gco",
        )
        assert "layer" not in result.message.lower()

    def test_gcode_commands_sent(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"] * 20)
        adapter = _build_adapter(mock_ser)

        commands_sent: list[str] = []

        def _capture_gcode(commands):
            commands_sent.extend(commands)
            return True

        with patch.object(adapter, "send_gcode", side_effect=_capture_gcode):
            adapter.firmware_resume_print(
                z_height_mm=10.0,
                hotend_temp_c=200.0,
                bed_temp_c=60.0,
                file_name="test.gco",
            )

        assert "M413 S0" in commands_sent
        assert "G28 X Y" in commands_sent
        assert "M140 S60.0" in commands_sent
        assert "M104 S200.0" in commands_sent
        assert "G92 Z10.0" in commands_sent


# ---------------------------------------------------------------------------
# PrinterError wrapping tests
# ---------------------------------------------------------------------------

class TestPrinterErrorWrapping:
    """Tests that all serial errors are wrapped in PrinterError."""

    def test_serial_write_error_wrapped(self):
        mock_ser = _make_mock_serial()
        mock_ser.write.side_effect = OSError("broken pipe")
        adapter = _build_adapter(mock_ser)
        with pytest.raises(PrinterError, match="Failed to send"):
            adapter._send_command("G28")

    def test_serial_read_error_wrapped(self):
        mock_ser = _make_mock_serial()
        mock_ser.readline.side_effect = OSError("device disconnected")
        adapter = _build_adapter(mock_ser)
        with pytest.raises(PrinterError, match="Serial read error"):
            adapter._send_command("G28")

    def test_connect_os_error_wrapped(self):
        """OSError during port open is wrapped in PrinterError."""
        from kiln.printers.serial_adapter import SerialPrinterAdapter

        _fake_serial_mod.Serial = MagicMock(  # type: ignore[attr-defined]
            side_effect=OSError("generic OS error"),
        )

        with pytest.raises(PrinterError, match="OS error"):
            with patch.object(SerialPrinterAdapter, "_wait_for_startup"):
                SerialPrinterAdapter(port="/dev/ttyUSB0")


# ---------------------------------------------------------------------------
# Optional method defaults tests
# ---------------------------------------------------------------------------

class TestOptionalMethods:
    """Tests for optional methods that return None or default values."""

    def test_get_snapshot_returns_none(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        assert adapter.get_snapshot() is None

    def test_get_stream_url_returns_none(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        assert adapter.get_stream_url() is None

    def test_get_bed_mesh_returns_none(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        assert adapter.get_bed_mesh() is None

    def test_get_filament_status_returns_none(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        assert adapter.get_filament_status() is None

    def test_update_firmware_raises(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        with pytest.raises(PrinterError, match="does not support firmware updates"):
            adapter.update_firmware()

    def test_rollback_firmware_raises(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        with pytest.raises(PrinterError, match="does not support firmware rollback"):
            adapter.rollback_firmware("firmware")

    def test_set_spindle_speed_raises(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        with pytest.raises(PrinterError, match="does not support spindle"):
            adapter.set_spindle_speed(1000)

    def test_set_laser_power_raises(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        with pytest.raises(PrinterError, match="does not support laser"):
            adapter.set_laser_power(50.0)


# ---------------------------------------------------------------------------
# Thread safety test
# ---------------------------------------------------------------------------

class TestThreadSafety:
    """Verify that _send_command uses a lock for serial access."""

    def test_lock_exists(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        assert isinstance(adapter._lock, type(threading.Lock()))

    def test_command_completes_without_deadlock(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        # If the lock is held improperly, this will deadlock.
        result = adapter._send_command("G28")
        assert "ok" in result.lower()


# ---------------------------------------------------------------------------
# Wait for startup tests
# ---------------------------------------------------------------------------

class TestWaitForStartup:
    """Tests for _wait_for_startup() behavior."""

    def test_startup_receives_start(self):
        from kiln.printers.serial_adapter import SerialPrinterAdapter

        mock_ser = _make_mock_serial(readline_responses=[
            b"Marlin 2.1.2\n",
            b"echo: Last Updated: 2024-01-01\n",
            b"start\n",
        ])
        _fake_serial_mod.Serial = MagicMock(return_value=mock_ser)  # type: ignore[attr-defined]

        adapter = SerialPrinterAdapter(
            port="/dev/ttyUSB0",
            printer_name="test",
        )
        assert adapter._connected is True

    def test_startup_receives_ok(self):
        from kiln.printers.serial_adapter import SerialPrinterAdapter

        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        _fake_serial_mod.Serial = MagicMock(return_value=mock_ser)  # type: ignore[attr-defined]

        adapter = SerialPrinterAdapter(
            port="/dev/ttyUSB0",
            printer_name="test",
        )
        assert adapter._connected is True

    def test_startup_timeout_still_connects(self):
        """If no startup message is received, we still proceed."""
        from kiln.printers.serial_adapter import SerialPrinterAdapter

        mock_ser = _make_mock_serial(readline_responses=[])
        _fake_serial_mod.Serial = MagicMock(return_value=mock_ser)  # type: ignore[attr-defined]

        with patch("kiln.printers.serial_adapter.time.monotonic") as mock_time:
            mock_time.side_effect = [0.0, 100.0]
            adapter = SerialPrinterAdapter(
                port="/dev/ttyUSB0",
                printer_name="test",
                timeout=1,
            )
        assert adapter._connected is True


# ---------------------------------------------------------------------------
# Safety profile integration test
# ---------------------------------------------------------------------------

class TestSafetyProfile:
    """Tests for safety profile temperature override."""

    def test_set_safety_profile(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        adapter.set_safety_profile("ender3")
        assert adapter._safety_profile_id == "ender3"


# ---------------------------------------------------------------------------
# Import guard test
# ---------------------------------------------------------------------------

class TestImportGuard:
    """Test that missing pyserial gives a clear error."""

    def test_import_error_message(self):
        old = sys.modules.get("serial")
        sys.modules["serial"] = None  # type: ignore[assignment]
        try:
            import kiln.printers.serial_adapter as mod
            importlib.reload(mod)
            with pytest.raises(PrinterError, match="pyserial is required"):
                mod.SerialPrinterAdapter(port="/dev/ttyUSB0")
        finally:
            if old is not None:
                sys.modules["serial"] = old
            else:
                sys.modules["serial"] = _fake_serial_mod
            import kiln.printers.serial_adapter as mod2
            importlib.reload(mod2)


# ===========================================================================
# ROUND 2 GAP ANALYSIS TESTS
# ===========================================================================


# ---------------------------------------------------------------------------
# Pause state tracking tests
# ---------------------------------------------------------------------------

class TestPauseStateTracking:
    """Tests for internal _paused flag that distinguishes paused from printing."""

    def test_pause_sets_flag(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        assert adapter._paused is False
        adapter.pause_print()
        assert adapter._paused is True

    def test_resume_clears_flag(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n", b"ok\n"])
        adapter = _build_adapter(mock_ser)
        adapter._paused = True
        adapter.resume_print()
        assert adapter._paused is False

    def test_cancel_clears_flag(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        adapter._paused = True
        adapter.cancel_print()
        assert adapter._paused is False

    def test_emergency_stop_clears_flag(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        adapter._paused = True
        adapter.emergency_stop()
        assert adapter._paused is False

    def test_get_state_reports_paused_when_flag_set(self):
        """When SD is printing but _paused is True, status should be PAUSED."""
        mock_ser = _make_mock_serial(readline_responses=[
            b"ok T:210.0 /210.0 B:60.0 /60.0\n",
            b"SD printing byte 5000/10000\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        adapter._paused = True
        state = adapter.get_state()
        assert state.state == PrinterStatus.PAUSED

    def test_get_state_reports_printing_when_not_paused(self):
        """When SD is printing and _paused is False, status should be PRINTING."""
        mock_ser = _make_mock_serial(readline_responses=[
            b"ok T:210.0 /210.0 B:60.0 /60.0\n",
            b"SD printing byte 5000/10000\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        adapter._paused = False
        state = adapter.get_state()
        assert state.state == PrinterStatus.PRINTING

    def test_get_state_clears_paused_on_print_complete(self):
        """When SD print completes, _paused flag should be cleared."""
        mock_ser = _make_mock_serial(readline_responses=[
            b"ok T:22.0 /0.0 B:21.0 /0.0\n",
            b"SD printing byte 10000/10000\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        adapter._paused = True
        state = adapter.get_state()
        assert state.state == PrinterStatus.IDLE
        assert adapter._paused is False

    def test_get_state_clears_paused_on_not_sd_printing(self):
        """When not SD printing, _paused flag should be cleared."""
        mock_ser = _make_mock_serial(readline_responses=[
            b"ok T:22.0 /0.0 B:21.0 /0.0\n",
            b"Not SD printing\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        adapter._paused = True
        state = adapter.get_state()
        assert state.state == PrinterStatus.IDLE
        assert adapter._paused is False


# ---------------------------------------------------------------------------
# get_tool_position() M114 tests
# ---------------------------------------------------------------------------

class TestGetToolPosition:
    """Tests for get_tool_position() M114 parsing."""

    def test_standard_m114_response(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"X:10.00 Y:20.00 Z:5.00 E:0.00 Count X:800 Y:1600 Z:4000\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        pos = adapter.get_tool_position()
        assert pos is not None
        assert pos["x"] == 10.0
        assert pos["y"] == 20.0
        assert pos["z"] == 5.0
        assert pos["e"] == 0.0

    def test_negative_coordinates(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"X:-5.50 Y:0.00 Z:100.00 E:50.00\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        pos = adapter.get_tool_position()
        assert pos is not None
        assert pos["x"] == -5.5
        assert pos["z"] == 100.0

    def test_m114_failure_returns_none(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        with patch.object(adapter, "_send_command", side_effect=PrinterError("fail")):
            pos = adapter.get_tool_position()
            assert pos is None

    def test_m114_unparseable_returns_none(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"garbage response\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        pos = adapter.get_tool_position()
        assert pos is None

    def test_m114_partial_axes(self):
        """Only some axes present in response."""
        mock_ser = _make_mock_serial(readline_responses=[
            b"X:10.00 Z:5.00\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        pos = adapter.get_tool_position()
        assert pos is not None
        assert pos["x"] == 10.0
        assert pos["z"] == 5.0
        assert "y" not in pos
        assert "e" not in pos


# ---------------------------------------------------------------------------
# Upload edge case tests (Round 2)
# ---------------------------------------------------------------------------

class TestUploadEdgeCases:
    """Additional upload_file tests for edge cases found in gap analysis."""

    def test_upload_permission_error_on_read(self):
        """PermissionError reading the local file is wrapped in PrinterError."""
        mock_ser = _make_mock_serial(readline_responses=[
            b"Writing to file\n",
            b"ok\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".gcode", delete=False,
        ) as f:
            f.write("G28\n")
            temp_path = f.name

        try:
            # Mock open to raise PermissionError.
            with patch("builtins.open", side_effect=PermissionError("denied")):
                with pytest.raises(PrinterError, match="Permission denied"):
                    adapter.upload_file(temp_path)
        finally:
            os.unlink(temp_path)

    def test_upload_m28_failure_raises(self):
        """Failure on M28 (start SD write) raises PrinterError."""
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)

        call_count = [0]

        def _fail_m28(cmd, timeout_val, wait):
            call_count[0] += 1
            raise PrinterError("M28 failed")

        adapter._ensure_connected()
        with patch.object(adapter, "_send_command_locked", side_effect=_fail_m28):
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".gcode", delete=False,
            ) as f:
                f.write("G28\n")
                temp_path = f.name

            try:
                with pytest.raises(PrinterError, match="Failed to start SD write"):
                    adapter.upload_file(temp_path)
            finally:
                os.unlink(temp_path)


# ---------------------------------------------------------------------------
# Return type verification tests (Round 2)
# ---------------------------------------------------------------------------

class TestReturnTypes:
    """Verify that all methods return the exact types specified in base.py."""

    def test_get_state_returns_printer_state(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"ok T:22.0 /0.0 B:21.0 /0.0\n",
            b"Not SD printing\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        result = adapter.get_state()
        assert isinstance(result, PrinterState)
        assert isinstance(result.state, PrinterStatus)

    def test_get_job_returns_job_progress(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"Not SD printing\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        result = adapter.get_job()
        assert isinstance(result, JobProgress)

    def test_list_files_returns_list_of_printer_file(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"Begin file list\n",
            b"FILE.GCO\n",
            b"End file list\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        result = adapter.list_files()
        assert isinstance(result, list)
        assert all(isinstance(f, PrinterFile) for f in result)

    def test_upload_file_returns_upload_result(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"ok\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".gcode", delete=False,
        ) as f:
            f.write("G28\n")
            temp_path = f.name

        try:
            result = adapter.upload_file(temp_path)
            assert isinstance(result, UploadResult)
        finally:
            os.unlink(temp_path)

    def test_start_print_returns_print_result(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n", b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.start_print("FILE.GCO")
        assert isinstance(result, PrintResult)

    def test_cancel_print_returns_print_result(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.cancel_print()
        assert isinstance(result, PrintResult)

    def test_pause_print_returns_print_result(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.pause_print()
        assert isinstance(result, PrintResult)

    def test_resume_print_returns_print_result(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.resume_print()
        assert isinstance(result, PrintResult)

    def test_emergency_stop_returns_print_result(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        result = adapter.emergency_stop()
        assert isinstance(result, PrintResult)

    def test_delete_file_returns_bool(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.delete_file("FILE.GCO")
        assert result is True

    def test_set_tool_temp_returns_bool(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.set_tool_temp(200.0)
        assert result is True

    def test_set_bed_temp_returns_bool(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.set_bed_temp(60.0)
        assert result is True

    def test_send_gcode_returns_bool(self):
        mock_ser = _make_mock_serial(readline_responses=[b"ok\n"])
        adapter = _build_adapter(mock_ser)
        result = adapter.send_gcode(["G28"])
        assert result is True

    def test_capabilities_returns_printer_capabilities(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        result = adapter.capabilities
        assert isinstance(result, PrinterCapabilities)

    def test_firmware_status_returns_firmware_status(self):
        mock_ser = _make_mock_serial(readline_responses=[
            b"FIRMWARE_NAME:Marlin FIRMWARE_VERSION:2.1.2\n",
            b"ok\n",
        ])
        adapter = _build_adapter(mock_ser)
        result = adapter.get_firmware_status()
        assert isinstance(result, FirmwareStatus)


# ---------------------------------------------------------------------------
# to_dict() serialisation tests (Round 2)
# ---------------------------------------------------------------------------

class TestSerialisation:
    """Verify that returned dataclasses serialise cleanly."""

    def test_printer_state_to_dict(self):
        state = PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            tool_temp_actual=210.0,
            tool_temp_target=210.0,
            bed_temp_actual=60.0,
            bed_temp_target=60.0,
        )
        d = state.to_dict()
        assert d["state"] == "printing"
        assert d["tool_temp_actual"] == 210.0

    def test_upload_result_to_dict(self):
        result = UploadResult(success=True, file_name="TEST.GCO", message="ok")
        d = result.to_dict()
        assert d["success"] is True
        assert d["file_name"] == "TEST.GCO"

    def test_print_result_to_dict(self):
        result = PrintResult(success=True, message="done")
        d = result.to_dict()
        assert d["success"] is True
        assert d["message"] == "done"

    def test_capabilities_to_dict(self):
        mock_ser = _make_mock_serial()
        adapter = _build_adapter(mock_ser)
        d = adapter.capabilities.to_dict()
        assert d["can_upload"] is True
        assert isinstance(d["supported_extensions"], list)
