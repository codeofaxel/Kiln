"""Tests for the Bambu Lab printer adapter.

Every public method of :class:`BambuAdapter` is exercised with mocked
MQTT and FTPS responses so the test suite runs without a real Bambu printer.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional
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
from kiln.printers.bambu import (
    BambuAdapter,
    _STATE_MAP,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

HOST = "192.168.1.100"
ACCESS_CODE = "12345678"
SERIAL = "01P00A000000001"


def _adapter(**kwargs: Any) -> BambuAdapter:
    """Create a :class:`BambuAdapter` with sensible test defaults."""
    defaults: Dict[str, Any] = {
        "host": HOST,
        "access_code": ACCESS_CODE,
        "serial": SERIAL,
        "timeout": 2,
    }
    defaults.update(kwargs)
    return BambuAdapter(**defaults)


@pytest.fixture
def adapter_with_mqtt() -> BambuAdapter:
    """Create an adapter with pre-mocked MQTT connection state.

    This simulates a successfully connected MQTT client so that tests
    can exercise adapter methods without triggering real network calls.
    """
    adapter = _adapter()
    # Pre-populate the MQTT state to avoid actual connection.
    adapter._mqtt_connected.set()
    adapter._connected = True
    adapter._mqtt_client = mock.MagicMock()
    # Mock publish result.
    publish_result = mock.MagicMock()
    publish_result.wait_for_publish = mock.MagicMock()
    adapter._mqtt_client.publish.return_value = publish_result
    return adapter


@pytest.fixture
def mock_ftp_class() -> mock.MagicMock:
    """Create a mock FTP_TLS class that returns a configured mock instance."""
    mock_ftp = mock.MagicMock()
    mock_ftp.connect = mock.MagicMock()
    mock_ftp.login = mock.MagicMock()
    mock_ftp.prot_p = mock.MagicMock()
    mock_ftp.mlsd = mock.MagicMock(return_value=[])
    mock_ftp.storbinary = mock.MagicMock()
    mock_ftp.delete = mock.MagicMock()
    mock_ftp.quit = mock.MagicMock()
    return mock_ftp


# ---------------------------------------------------------------------------
# Constructor tests
# ---------------------------------------------------------------------------

class TestBambuAdapterInit:
    """Tests for adapter construction and validation."""

    def test_empty_host_raises(self) -> None:
        with pytest.raises(ValueError, match="host must not be empty"):
            BambuAdapter(host="", access_code=ACCESS_CODE, serial=SERIAL)

    def test_empty_access_code_raises(self) -> None:
        with pytest.raises(ValueError, match="access_code must not be empty"):
            BambuAdapter(host=HOST, access_code="", serial=SERIAL)

    def test_empty_serial_raises(self) -> None:
        with pytest.raises(ValueError, match="serial must not be empty"):
            BambuAdapter(host=HOST, access_code=ACCESS_CODE, serial="")

    def test_valid_construction(self) -> None:
        adapter = _adapter()
        assert adapter._host == HOST
        assert adapter._access_code == ACCESS_CODE
        assert adapter._serial == SERIAL

    def test_timeout_stored(self) -> None:
        adapter = _adapter(timeout=15)
        assert adapter._timeout == 15

    def test_topics_constructed_from_serial(self) -> None:
        adapter = _adapter()
        assert adapter._topic_report == f"device/{SERIAL}/report"
        assert adapter._topic_request == f"device/{SERIAL}/request"

    def test_initial_state_disconnected(self) -> None:
        adapter = _adapter()
        assert adapter._connected is False
        assert adapter._mqtt_client is None
        assert not adapter._mqtt_connected.is_set()

    def test_initial_status_empty(self) -> None:
        adapter = _adapter()
        assert adapter._last_status == {}

    def test_repr(self) -> None:
        adapter = _adapter()
        repr_str = repr(adapter)
        assert "BambuAdapter" in repr_str
        assert HOST in repr_str
        assert SERIAL in repr_str


# ---------------------------------------------------------------------------
# Properties tests
# ---------------------------------------------------------------------------

class TestBambuAdapterProperties:
    """Tests for name and capabilities properties."""

    def test_name_property(self) -> None:
        adapter = _adapter()
        assert adapter.name == "bambu"

    def test_capabilities_type(self) -> None:
        caps = _adapter().capabilities
        assert isinstance(caps, PrinterCapabilities)

    def test_capabilities_can_upload(self) -> None:
        caps = _adapter().capabilities
        assert caps.can_upload is True

    def test_capabilities_can_set_temp(self) -> None:
        caps = _adapter().capabilities
        assert caps.can_set_temp is True

    def test_capabilities_can_send_gcode(self) -> None:
        caps = _adapter().capabilities
        assert caps.can_send_gcode is True

    def test_capabilities_can_pause(self) -> None:
        caps = _adapter().capabilities
        assert caps.can_pause is True

    def test_capabilities_supported_extensions(self) -> None:
        caps = _adapter().capabilities
        assert ".3mf" in caps.supported_extensions
        assert ".gcode" in caps.supported_extensions
        assert ".gco" in caps.supported_extensions


# ---------------------------------------------------------------------------
# State map tests
# ---------------------------------------------------------------------------

class TestStateMap:
    """Tests for the gcode_state to PrinterStatus mapping."""

    @pytest.mark.parametrize(
        "gcode_state, expected",
        [
            ("idle", PrinterStatus.IDLE),
            ("finish", PrinterStatus.IDLE),
            ("running", PrinterStatus.PRINTING),
            ("prepare", PrinterStatus.BUSY),
            ("slicing", PrinterStatus.BUSY),
            ("init", PrinterStatus.BUSY),
            ("pause", PrinterStatus.PAUSED),
            ("failed", PrinterStatus.ERROR),
            ("offline", PrinterStatus.OFFLINE),
            ("unknown", PrinterStatus.UNKNOWN),
        ],
    )
    def test_state_mapping(self, gcode_state: str, expected: PrinterStatus) -> None:
        assert _STATE_MAP[gcode_state] == expected

    def test_unknown_state_defaults_to_unknown(self) -> None:
        # Any state not in _STATE_MAP should default to UNKNOWN when used
        # via .get() in get_state()
        assert _STATE_MAP.get("nonexistent_state", PrinterStatus.UNKNOWN) == PrinterStatus.UNKNOWN


# ---------------------------------------------------------------------------
# get_state tests
# ---------------------------------------------------------------------------

class TestBambuAdapterGetState:
    """Tests for the get_state method."""

    def test_idle_state(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "idle",
            "nozzle_temper": 25.0,
            "nozzle_target_temper": 0.0,
            "bed_temper": 22.0,
            "bed_target_temper": 0.0,
        }

        state = adapter_with_mqtt.get_state()

        assert state.connected is True
        assert state.state == PrinterStatus.IDLE
        assert state.tool_temp_actual == 25.0
        assert state.tool_temp_target == 0.0
        assert state.bed_temp_actual == 22.0
        assert state.bed_temp_target == 0.0

    def test_printing_state(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "running",
            "nozzle_temper": 210.0,
            "nozzle_target_temper": 210.0,
            "bed_temper": 60.0,
            "bed_target_temper": 60.0,
        }

        state = adapter_with_mqtt.get_state()

        assert state.connected is True
        assert state.state == PrinterStatus.PRINTING
        assert state.tool_temp_actual == 210.0

    def test_paused_state(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"gcode_state": "pause"}

        state = adapter_with_mqtt.get_state()

        assert state.state == PrinterStatus.PAUSED

    def test_error_state(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"gcode_state": "failed"}

        state = adapter_with_mqtt.get_state()

        assert state.state == PrinterStatus.ERROR

    def test_busy_prepare_state(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"gcode_state": "prepare"}

        state = adapter_with_mqtt.get_state()

        assert state.state == PrinterStatus.BUSY

    def test_busy_slicing_state(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"gcode_state": "slicing"}

        state = adapter_with_mqtt.get_state()

        assert state.state == PrinterStatus.BUSY

    def test_unknown_state(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"gcode_state": "garbage_state"}

        state = adapter_with_mqtt.get_state()

        assert state.state == PrinterStatus.UNKNOWN

    def test_missing_gcode_state_defaults_to_unknown(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "nozzle_temper": 25.0,
        }

        state = adapter_with_mqtt.get_state()

        assert state.state == PrinterStatus.UNKNOWN

    def test_non_string_gcode_state_handled(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"gcode_state": 12345}

        state = adapter_with_mqtt.get_state()

        assert state.state == PrinterStatus.UNKNOWN

    def test_offline_on_mqtt_failure(self) -> None:
        adapter = _adapter()
        # Don't set up MQTT - it will fail to connect.
        with mock.patch.object(adapter, "_ensure_mqtt", side_effect=PrinterError("Connection failed")):
            state = adapter.get_state()

        assert state.connected is False
        assert state.state == PrinterStatus.OFFLINE

    def test_state_with_temperatures(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "running",
            "nozzle_temper": 215.5,
            "nozzle_target_temper": 220.0,
            "bed_temper": 65.3,
            "bed_target_temper": 70.0,
        }

        state = adapter_with_mqtt.get_state()

        assert state.tool_temp_actual == 215.5
        assert state.tool_temp_target == 220.0
        assert state.bed_temp_actual == 65.3
        assert state.bed_temp_target == 70.0

    def test_state_missing_temperatures(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"gcode_state": "idle"}

        state = adapter_with_mqtt.get_state()

        assert state.tool_temp_actual is None
        assert state.tool_temp_target is None
        assert state.bed_temp_actual is None
        assert state.bed_temp_target is None


# ---------------------------------------------------------------------------
# get_job tests
# ---------------------------------------------------------------------------

class TestBambuAdapterGetJob:
    """Tests for the get_job method."""

    def test_active_job(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_file": "model.3mf",
            "mc_percent": 50,
            "mc_remaining_time": 60,  # 60 minutes remaining
        }

        job = adapter_with_mqtt.get_job()

        assert isinstance(job, JobProgress)
        assert job.file_name == "model.3mf"
        assert job.completion == 50.0
        assert job.print_time_left_seconds == 3600  # 60 * 60

    def test_job_with_subtask_name(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "subtask_name": "test_print.3mf",
            "mc_percent": 25,
            "mc_remaining_time": 120,
        }

        job = adapter_with_mqtt.get_job()

        assert job.file_name == "test_print.3mf"

    def test_gcode_file_takes_precedence(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_file": "primary.3mf",
            "subtask_name": "fallback.3mf",
            "mc_percent": 10,
        }

        job = adapter_with_mqtt.get_job()

        assert job.file_name == "primary.3mf"

    def test_no_active_job(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {}

        job = adapter_with_mqtt.get_job()

        assert job.file_name is None
        assert job.completion is None

    def test_completed_job(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_file": "finished.3mf",
            "mc_percent": 100,
            "mc_remaining_time": 0,
        }

        job = adapter_with_mqtt.get_job()

        assert job.completion == 100.0
        assert job.print_time_left_seconds == 0

    def test_job_elapsed_time_calculated(self, adapter_with_mqtt: BambuAdapter) -> None:
        # 50% complete, 30 minutes remaining -> elapsed should be ~30 minutes
        adapter_with_mqtt._last_status = {
            "gcode_file": "test.3mf",
            "mc_percent": 50,
            "mc_remaining_time": 30,  # 30 minutes
        }

        job = adapter_with_mqtt.get_job()

        # elapsed = total - remaining = (remaining / 0.5) - remaining = remaining
        assert job.print_time_seconds == 1800  # 30 * 60

    def test_job_zero_percent_no_elapsed(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_file": "test.3mf",
            "mc_percent": 0,
            "mc_remaining_time": 120,
        }

        job = adapter_with_mqtt.get_job()

        # Can't calculate elapsed time when 0% complete
        assert job.print_time_seconds is None

    def test_job_on_mqtt_failure(self) -> None:
        adapter = _adapter()
        with mock.patch.object(adapter, "_get_cached_status", side_effect=PrinterError("Failed")):
            job = adapter.get_job()

        # Should return empty JobProgress on failure
        assert job.file_name is None
        assert job.completion is None


# ---------------------------------------------------------------------------
# list_files tests
# ---------------------------------------------------------------------------

class TestBambuAdapterListFiles:
    """Tests for the list_files method."""

    def test_normal_listing(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.mlsd.return_value = [
            ("test.3mf", {"type": "file", "size": "12345", "modify": "20241201120000"}),
            ("print.gcode", {"type": "file", "size": "67890", "modify": "20241202150000"}),
        ]

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            files = adapter_with_mqtt.list_files()

        assert len(files) == 2
        assert all(isinstance(f, PrinterFile) for f in files)
        assert files[0].name == "test.3mf"
        assert files[0].path == "/sdcard/test.3mf"
        assert files[0].size_bytes == 12345
        assert files[1].name == "print.gcode"

    def test_empty_listing(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.mlsd.return_value = [
            (".", {"type": "dir"}),
            ("..", {"type": "dir"}),
        ]

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            files = adapter_with_mqtt.list_files()

        assert files == []

    def test_directories_skipped(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.mlsd.return_value = [
            (".", {"type": "dir"}),
            ("..", {"type": "dir"}),
            ("subfolder", {"type": "dir"}),
            ("actual_file.3mf", {"type": "file", "size": "1000"}),
        ]

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            files = adapter_with_mqtt.list_files()

        assert len(files) == 1
        assert files[0].name == "actual_file.3mf"

    def test_ftp_connection_error(self, adapter_with_mqtt: BambuAdapter) -> None:
        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS") as mock_ftp_cls:
            mock_ftp_cls.return_value.connect.side_effect = Exception("Connection refused")
            with pytest.raises(PrinterError, match="FTPS connection"):
                adapter_with_mqtt.list_files()

    def test_ftp_mlsd_error(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.mlsd.side_effect = Exception("MLSD failed")

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            with pytest.raises(PrinterError, match="Failed to list files"):
                adapter_with_mqtt.list_files()

    def test_file_without_size(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.mlsd.return_value = [
            ("nosize.3mf", {"type": "file", "modify": "20241201120000"}),
        ]

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            files = adapter_with_mqtt.list_files()

        assert len(files) == 1
        assert files[0].size_bytes is None

    def test_file_without_modify(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.mlsd.return_value = [
            ("nodate.3mf", {"type": "file", "size": "1000"}),
        ]

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            files = adapter_with_mqtt.list_files()

        assert len(files) == 1
        assert files[0].date is None

    def test_ftp_quit_called(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.mlsd.return_value = []

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            adapter_with_mqtt.list_files()

        mock_ftp_class.quit.assert_called_once()


# ---------------------------------------------------------------------------
# upload_file tests
# ---------------------------------------------------------------------------

class TestBambuAdapterUploadFile:
    """Tests for the upload_file method."""

    def test_successful_upload(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock, tmp_path: Any) -> None:
        test_file = tmp_path / "test.3mf"
        test_file.write_text("fake 3mf content")

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            result = adapter_with_mqtt.upload_file(str(test_file))

        assert isinstance(result, UploadResult)
        assert result.success is True
        assert result.file_name == "test.3mf"
        assert "Uploaded" in result.message
        mock_ftp_class.storbinary.assert_called_once()

    def test_file_not_found(self, adapter_with_mqtt: BambuAdapter) -> None:
        with pytest.raises(FileNotFoundError, match="Local file not found"):
            adapter_with_mqtt.upload_file("/nonexistent/path/file.3mf")

    def test_ftp_connection_error(self, adapter_with_mqtt: BambuAdapter, tmp_path: Any) -> None:
        test_file = tmp_path / "test.3mf"
        test_file.write_text("content")

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS") as mock_ftp_cls:
            mock_ftp_cls.return_value.connect.side_effect = Exception("Connection refused")
            with pytest.raises(PrinterError, match="FTPS connection"):
                adapter_with_mqtt.upload_file(str(test_file))

    def test_ftp_upload_error(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock, tmp_path: Any) -> None:
        test_file = tmp_path / "test.3mf"
        test_file.write_text("content")
        mock_ftp_class.storbinary.side_effect = Exception("Upload failed")

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            with pytest.raises(PrinterError, match="FTPS upload failed"):
                adapter_with_mqtt.upload_file(str(test_file))

    def test_permission_error(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock, tmp_path: Any) -> None:
        test_file = tmp_path / "locked.3mf"
        test_file.write_text("content")

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            with mock.patch("builtins.open", side_effect=PermissionError("no read")):
                with pytest.raises(PrinterError, match="Permission denied"):
                    adapter_with_mqtt.upload_file(str(test_file))

    def test_ftp_quit_called_on_success(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock, tmp_path: Any) -> None:
        test_file = tmp_path / "test.3mf"
        test_file.write_text("content")

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            adapter_with_mqtt.upload_file(str(test_file))

        mock_ftp_class.quit.assert_called_once()


# ---------------------------------------------------------------------------
# start_print tests
# ---------------------------------------------------------------------------

class TestBambuAdapterStartPrint:
    """Tests for the start_print method."""

    def test_start_3mf_file(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.start_print("model.3mf")

        assert isinstance(result, PrintResult)
        assert result.success is True
        assert "model.3mf" in result.message

        # Verify MQTT publish was called with project_file command
        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["command"] == "project_file"
        assert payload["print"]["subtask_name"] == "model.3mf"

    def test_start_gcode_file(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.start_print("test.gcode")

        assert result.success is True

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["command"] == "gcode_file"
        assert "/sdcard/test.gcode" in payload["print"]["param"]

    def test_start_print_strips_path(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.start_print("/sdcard/subdir/model.3mf")

        assert result.success is True

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["subtask_name"] == "model.3mf"

    def test_start_print_gcode_with_full_path(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.start_print("/sdcard/test.gcode")

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        # Full path should be preserved for gcode files
        assert payload["print"]["param"] == "/sdcard/test.gcode"

    def test_start_print_3mf_uppercase(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.start_print("MODEL.3MF")

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["command"] == "project_file"


# ---------------------------------------------------------------------------
# Print control tests
# ---------------------------------------------------------------------------

class TestBambuAdapterPrintControl:
    """Tests for cancel, pause, resume operations."""

    def test_cancel_print(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.cancel_print()

        assert isinstance(result, PrintResult)
        assert result.success is True
        assert "cancelled" in result.message.lower()

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["command"] == "stop"

    def test_pause_print(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.pause_print()

        assert result.success is True
        assert "paused" in result.message.lower()

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["command"] == "pause"

    def test_resume_print(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.resume_print()

        assert result.success is True
        assert "resumed" in result.message.lower()

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["command"] == "resume"


# ---------------------------------------------------------------------------
# Temperature tests
# ---------------------------------------------------------------------------

class TestBambuAdapterTemperature:
    """Tests for temperature control methods."""

    def test_set_tool_temp(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.set_tool_temp(210.0)

        assert result is True

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["command"] == "gcode_line"
        assert "M104 S210" in payload["print"]["param"]

    def test_set_tool_temp_off(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.set_tool_temp(0)

        assert result is True

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert "M104 S0" in payload["print"]["param"]

    def test_set_bed_temp(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.set_bed_temp(60.0)

        assert result is True

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["command"] == "gcode_line"
        assert "M140 S60" in payload["print"]["param"]

    def test_set_bed_temp_off(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.set_bed_temp(0)

        assert result is True

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert "M140 S0" in payload["print"]["param"]

    def test_temperature_truncates_float(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.set_tool_temp(210.7)

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        # Should truncate to int
        assert "M104 S210" in payload["print"]["param"]


# ---------------------------------------------------------------------------
# send_gcode tests
# ---------------------------------------------------------------------------

class TestBambuAdapterSendGcode:
    """Tests for the send_gcode method."""

    def test_single_command(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.send_gcode(["G28"])

        assert result is True

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["command"] == "gcode_line"
        assert payload["print"]["param"] == "G28"

    def test_multiple_commands(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.send_gcode(["G28", "G1 X10 Y10 Z5 F1200", "M104 S200"])

        assert result is True

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        expected = "G28\nG1 X10 Y10 Z5 F1200\nM104 S200"
        assert payload["print"]["param"] == expected

    def test_empty_command_list(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.send_gcode([])

        assert result is True

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["param"] == ""


# ---------------------------------------------------------------------------
# delete_file tests
# ---------------------------------------------------------------------------

class TestBambuAdapterDeleteFile:
    """Tests for the delete_file method."""

    def test_successful_delete(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            result = adapter_with_mqtt.delete_file("/sdcard/old_model.3mf")

        assert result is True
        mock_ftp_class.delete.assert_called_once_with("/sdcard/old_model.3mf")

    def test_ftp_connection_error(self, adapter_with_mqtt: BambuAdapter) -> None:
        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS") as mock_ftp_cls:
            mock_ftp_cls.return_value.connect.side_effect = Exception("Connection refused")
            with pytest.raises(PrinterError, match="FTPS connection"):
                adapter_with_mqtt.delete_file("/sdcard/file.3mf")

    def test_ftp_delete_error(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.delete.side_effect = Exception("File not found")

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            with pytest.raises(PrinterError, match="Failed to delete"):
                adapter_with_mqtt.delete_file("/sdcard/nonexistent.3mf")

    def test_ftp_quit_called(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            adapter_with_mqtt.delete_file("/sdcard/file.3mf")

        mock_ftp_class.quit.assert_called_once()


# ---------------------------------------------------------------------------
# disconnect tests
# ---------------------------------------------------------------------------

class TestBambuAdapterDisconnect:
    """Tests for the disconnect method."""

    def test_disconnect_stops_mqtt_loop(self, adapter_with_mqtt: BambuAdapter) -> None:
        client = adapter_with_mqtt._mqtt_client
        adapter_with_mqtt.disconnect()

        client.loop_stop.assert_called_once()
        client.disconnect.assert_called_once()

    def test_disconnect_clears_state(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.disconnect()

        assert adapter_with_mqtt._mqtt_client is None
        assert not adapter_with_mqtt._mqtt_connected.is_set()
        assert adapter_with_mqtt._connected is False

    def test_disconnect_when_not_connected(self) -> None:
        adapter = _adapter()
        # Should not raise even when not connected
        adapter.disconnect()

        assert adapter._mqtt_client is None

    def test_disconnect_handles_exception(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._mqtt_client.loop_stop.side_effect = Exception("Loop stop failed")

        # Should not raise
        adapter_with_mqtt.disconnect()

        assert adapter_with_mqtt._mqtt_client is None


# ---------------------------------------------------------------------------
# Interface compliance tests
# ---------------------------------------------------------------------------

class TestBambuAdapterInterfaceCompliance:
    """Verify that BambuAdapter correctly implements the abstract interface."""

    def test_is_printer_adapter_subclass(self) -> None:
        assert issubclass(BambuAdapter, PrinterAdapter)

    def test_instance_check(self) -> None:
        adapter = _adapter()
        assert isinstance(adapter, PrinterAdapter)

    def test_all_abstract_methods_implemented(self) -> None:
        # Verify all abstract methods exist and are callable
        adapter = _adapter()
        abstract_methods = [
            "name",
            "capabilities",
            "get_state",
            "get_job",
            "list_files",
            "upload_file",
            "start_print",
            "cancel_print",
            "pause_print",
            "resume_print",
            "set_tool_temp",
            "set_bed_temp",
            "send_gcode",
            "delete_file",
        ]
        for method_name in abstract_methods:
            assert hasattr(adapter, method_name), f"Missing method: {method_name}"

    def test_importable_from_package(self) -> None:
        from kiln.printers import BambuAdapter as Imported

        assert Imported is BambuAdapter


# ---------------------------------------------------------------------------
# MQTT internal tests
# ---------------------------------------------------------------------------

class TestBambuAdapterMQTTInternals:
    """Tests for internal MQTT methods."""

    def test_next_seq_increments(self, adapter_with_mqtt: BambuAdapter) -> None:
        seq1 = adapter_with_mqtt._next_seq()
        seq2 = adapter_with_mqtt._next_seq()
        seq3 = adapter_with_mqtt._next_seq()

        assert seq1 == "1"
        assert seq2 == "2"
        assert seq3 == "3"

    def test_publish_command_calls_mqtt_publish(self, adapter_with_mqtt: BambuAdapter) -> None:
        payload = {"test": "command"}
        adapter_with_mqtt._publish_command(payload)

        adapter_with_mqtt._mqtt_client.publish.assert_called_once()
        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        assert call_args[0][0] == adapter_with_mqtt._topic_request
        assert json.loads(call_args[0][1]) == payload

    def test_publish_command_waits_for_publish(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._publish_command({"test": "data"})

        publish_result = adapter_with_mqtt._mqtt_client.publish.return_value
        publish_result.wait_for_publish.assert_called_once()

    def test_publish_command_raises_on_failure(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._mqtt_client.publish.side_effect = Exception("Publish failed")

        with pytest.raises(PrinterError, match="Failed to publish"):
            adapter_with_mqtt._publish_command({"test": "data"})

    def test_on_message_updates_status(self) -> None:
        adapter = _adapter()
        adapter._mqtt_client = mock.MagicMock()

        # Simulate incoming MQTT message
        msg = mock.MagicMock()
        msg.payload = json.dumps({
            "print": {
                "command": "push_status",
                "gcode_state": "running",
                "nozzle_temper": 200,
            }
        }).encode()

        adapter._on_message(adapter._mqtt_client, None, msg)

        assert adapter._last_status.get("gcode_state") == "running"
        assert adapter._last_status.get("nozzle_temper") == 200

    def test_on_message_ignores_non_push_status(self) -> None:
        adapter = _adapter()
        adapter._mqtt_client = mock.MagicMock()

        msg = mock.MagicMock()
        msg.payload = json.dumps({
            "print": {
                "command": "other_command",
                "data": "value",
            }
        }).encode()

        adapter._on_message(adapter._mqtt_client, None, msg)

        assert adapter._last_status == {}

    def test_on_message_handles_invalid_json(self) -> None:
        adapter = _adapter()
        adapter._mqtt_client = mock.MagicMock()

        msg = mock.MagicMock()
        msg.payload = b"not valid json"

        # Should not raise
        adapter._on_message(adapter._mqtt_client, None, msg)

        assert adapter._last_status == {}

    def test_on_connect_subscribes_and_requests_status(self) -> None:
        adapter = _adapter()
        mock_client = mock.MagicMock()
        publish_result = mock.MagicMock()
        mock_client.publish.return_value = publish_result

        adapter._on_connect(mock_client, None, None, None)

        mock_client.subscribe.assert_called_once_with(adapter._topic_report)
        assert adapter._mqtt_connected.is_set()
        assert adapter._connected is True
        mock_client.publish.assert_called_once()

    def test_on_disconnect_clears_state(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._on_disconnect(adapter_with_mqtt._mqtt_client, None)

        assert not adapter_with_mqtt._mqtt_connected.is_set()
        assert adapter_with_mqtt._connected is False


# ---------------------------------------------------------------------------
# FTPS internal tests
# ---------------------------------------------------------------------------

class TestBambuAdapterFTPSInternals:
    """Tests for internal FTPS connection handling."""

    def test_ftp_connect_calls_all_setup_methods(self, mock_ftp_class: mock.MagicMock) -> None:
        adapter = _adapter()

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS", return_value=mock_ftp_class):
            ftp = adapter._ftp_connect()

        mock_ftp_class.connect.assert_called_once_with(HOST, 990, timeout=2)
        mock_ftp_class.login.assert_called_once_with("bblp", ACCESS_CODE)
        mock_ftp_class.prot_p.assert_called_once()
        assert ftp is mock_ftp_class

    def test_ftp_connect_raises_on_failure(self) -> None:
        adapter = _adapter()

        with mock.patch("kiln.printers.bambu.ftplib.FTP_TLS") as mock_ftp_cls:
            mock_ftp_cls.return_value.connect.side_effect = Exception("Connection failed")
            with pytest.raises(PrinterError, match="FTPS connection"):
                adapter._ftp_connect()


# ---------------------------------------------------------------------------
# PrinterState serialization tests
# ---------------------------------------------------------------------------

class TestBambuAdapterSnapshot:
    """Tests for get_snapshot and get_stream_url."""

    def test_get_stream_url(self) -> None:
        adapter = _adapter()
        url = adapter.get_stream_url()
        assert url == f"rtsps://{HOST}:322/streaming/live/1"

    @mock.patch("urllib.request.urlopen")
    def test_get_snapshot_https_success(self, mock_urlopen) -> None:
        adapter = _adapter()
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = b"\x89PNG" + b"\x00" * 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = adapter.get_snapshot()
        assert result is not None
        assert result[:4] == b"\x89PNG"

    @mock.patch("urllib.request.urlopen")
    def test_get_snapshot_falls_back_to_http(self, mock_urlopen) -> None:
        adapter = _adapter()
        # HTTPS fails, HTTP succeeds
        good_resp = mock.MagicMock()
        good_resp.read.return_value = b"\x89PNG" + b"\x00" * 200
        good_resp.__enter__ = mock.MagicMock(return_value=good_resp)
        good_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.side_effect = [Exception("SSL error"), good_resp]
        result = adapter.get_snapshot()
        assert result is not None
        assert mock_urlopen.call_count == 2

    @mock.patch("urllib.request.urlopen")
    def test_get_snapshot_returns_none_on_failure(self, mock_urlopen) -> None:
        adapter = _adapter()
        mock_urlopen.side_effect = Exception("connection refused")
        result = adapter.get_snapshot()
        assert result is None


class TestPrinterStateSerialization:
    """Tests for PrinterState serialization."""

    def test_to_dict_roundtrip(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "running",
            "nozzle_temper": 200.0,
            "nozzle_target_temper": 210.0,
            "bed_temper": 58.5,
            "bed_target_temper": 60.0,
        }

        state = adapter_with_mqtt.get_state()
        d = state.to_dict()

        assert d["connected"] is True
        assert d["state"] == "printing"
        assert d["tool_temp_actual"] == 200.0
        assert d["bed_temp_target"] == 60.0
