"""Tests for the Bambu Lab printer adapter.

Every public method of :class:`BambuAdapter` is exercised with mocked
MQTT and FTPS responses so the test suite runs without a real Bambu printer.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
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
    _ImplicitFTP_TLS,
    _PRINT_ACTIVE_STATES,
    _STATE_MAP,
    _find_ffmpeg,
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
    # Pre-set a running state so start_print skips confirmation wait.
    adapter._last_status = {"gcode_state": "running"}
    return adapter


@pytest.fixture(autouse=True)
def _pin_store_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use a writable temporary pin-store path for every test."""
    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "bambu_tls_pins.json"))


@pytest.fixture
def mock_ftp_class() -> mock.MagicMock:
    """Create a mock _ImplicitFTP_TLS class that returns a configured mock instance."""
    mock_ftp = mock.MagicMock()
    mock_ftp.connect = mock.MagicMock()
    mock_ftp.login = mock.MagicMock()
    mock_ftp.prot_p = mock.MagicMock()
    mock_ftp.mlsd = mock.MagicMock(return_value=[])
    mock_ftp.storbinary = mock.MagicMock()
    mock_ftp.delete = mock.MagicMock()
    mock_ftp.quit = mock.MagicMock()
    mock_ftp.close = mock.MagicMock()
    mock_sock = mock.MagicMock()
    mock_sock.getpeercert.return_value = b"fake-cert-der"
    mock_ftp.sock = mock_sock
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

    def test_default_tls_mode_is_pin(self) -> None:
        adapter = _adapter()
        assert adapter._tls_mode == "pin"

    def test_invalid_tls_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="tls_mode must be one of"):
            BambuAdapter(
                host=HOST,
                access_code=ACCESS_CODE,
                serial=SERIAL,
                tls_mode="bogus",
            )

    def test_invalid_tls_fingerprint_raises(self) -> None:
        with pytest.raises(ValueError, match="tls_fingerprint must be a SHA-256 fingerprint"):
            BambuAdapter(
                host=HOST,
                access_code=ACCESS_CODE,
                serial=SERIAL,
                tls_fingerprint="short",
            )

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

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
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

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            files = adapter_with_mqtt.list_files()

        assert files == []

    def test_directories_skipped(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.mlsd.return_value = [
            (".", {"type": "dir"}),
            ("..", {"type": "dir"}),
            ("subfolder", {"type": "dir"}),
            ("actual_file.3mf", {"type": "file", "size": "1000"}),
        ]

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            files = adapter_with_mqtt.list_files()

        assert len(files) == 1
        assert files[0].name == "actual_file.3mf"

    def test_ftp_connection_error(self, adapter_with_mqtt: BambuAdapter) -> None:
        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS") as mock_ftp_cls:
            mock_ftp_cls.return_value.connect.side_effect = Exception("Connection refused")
            with pytest.raises(PrinterError, match="FTPS connection"):
                adapter_with_mqtt.list_files()

    def test_ftp_mlsd_error(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.mlsd.side_effect = Exception("MLSD failed")

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            with pytest.raises(PrinterError, match="Failed to list files"):
                adapter_with_mqtt.list_files()

    def test_mlsd_502_falls_back_to_nlst(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        import ftplib

        mock_ftp_class.mlsd.side_effect = ftplib.error_perm("502 Command not implemented")
        mock_ftp_class.nlst.return_value = ["/sdcard/test.3mf", "/sdcard/print.gcode"]

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            files = adapter_with_mqtt.list_files()

        assert len(files) == 2
        assert files[0].name == "test.3mf"
        assert files[0].path == "/sdcard/test.3mf"
        assert files[0].size_bytes is None
        assert files[0].date is None
        assert files[1].name == "print.gcode"

    def test_mlsd_502_nlst_fails_falls_back_to_list(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        import ftplib

        mock_ftp_class.mlsd.side_effect = ftplib.error_perm("502 Command not implemented")
        mock_ftp_class.nlst.side_effect = ftplib.error_perm("502 Command not implemented")

        def fake_retrlines(cmd: str, callback: Any) -> str:
            callback("-rw-r--r-- 1 user group 12345 Jan  1 12:00 model.3mf")
            callback("drw-r--r-- 1 user group  4096 Jan  1 12:00 subdir")
            return "226 Transfer complete"

        mock_ftp_class.retrlines = fake_retrlines

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            files = adapter_with_mqtt.list_files()

        assert len(files) == 1
        assert files[0].name == "model.3mf"
        assert files[0].path == "/sdcard/model.3mf"
        assert files[0].size_bytes == 12345

    def test_mlsd_non_502_error_still_raises(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        import ftplib

        mock_ftp_class.mlsd.side_effect = ftplib.error_perm("550 Permission denied")

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            with pytest.raises(PrinterError, match="Failed to list files"):
                adapter_with_mqtt.list_files()

    def test_nlst_skips_dot_entries(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        import ftplib

        mock_ftp_class.mlsd.side_effect = ftplib.error_perm("502 Command not implemented")
        mock_ftp_class.nlst.return_value = [".", "..", "/sdcard/file.3mf"]

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            files = adapter_with_mqtt.list_files()

        assert len(files) == 1
        assert files[0].name == "file.3mf"

    def test_file_without_size(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.mlsd.return_value = [
            ("nosize.3mf", {"type": "file", "modify": "20241201120000"}),
        ]

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            files = adapter_with_mqtt.list_files()

        assert len(files) == 1
        assert files[0].size_bytes is None

    def test_file_without_modify(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.mlsd.return_value = [
            ("nodate.3mf", {"type": "file", "size": "1000"}),
        ]

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            files = adapter_with_mqtt.list_files()

        assert len(files) == 1
        assert files[0].date is None

    def test_ftp_quit_called(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.mlsd.return_value = []

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
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

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
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

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS") as mock_ftp_cls:
            mock_ftp_cls.return_value.connect.side_effect = Exception("Connection refused")
            with pytest.raises(PrinterError, match="FTPS connection"):
                adapter_with_mqtt.upload_file(str(test_file))

    def test_ftp_upload_error(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock, tmp_path: Any) -> None:
        test_file = tmp_path / "test.3mf"
        test_file.write_text("content")
        mock_ftp_class.storbinary.side_effect = Exception("Upload failed")

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            with pytest.raises(PrinterError, match="FTPS upload failed"):
                adapter_with_mqtt.upload_file(str(test_file))

    def test_permission_error(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock, tmp_path: Any) -> None:
        test_file = tmp_path / "locked.3mf"
        test_file.write_text("content")
        real_open = open

        def selective_open(path, *args, **kwargs):
            candidate = os.path.abspath(os.fspath(path))
            if candidate == os.path.abspath(str(test_file)):
                raise PermissionError("no read")
            return real_open(path, *args, **kwargs)

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            with mock.patch("builtins.open", side_effect=selective_open):
                with pytest.raises(PrinterError, match="Permission denied"):
                    adapter_with_mqtt.upload_file(str(test_file))

    def test_ftp_quit_called_on_success(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock, tmp_path: Any) -> None:
        test_file = tmp_path / "test.3mf"
        test_file.write_text("content")

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
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
        assert payload["print"]["subtask_name"] == "model"

    def test_start_gcode_file(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.start_print("test.gcode")

        assert result.success is True

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["command"] == "gcode_file"
        assert "/sdcard/model/test.gcode" in payload["print"]["param"]

    def test_start_print_strips_path(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.start_print("/sdcard/subdir/model.3mf")

        assert result.success is True

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["print"]["subtask_name"] == "model"

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

    def test_emergency_stop(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.emergency_stop()

        assert isinstance(result, PrintResult)
        assert result.success is True
        assert "emergency" in result.message.lower() or "m112" in result.message.lower()

        call_args = adapter_with_mqtt._mqtt_client.publish.call_args
        payload = json.loads(call_args[0][1])
        assert "gcode_line" in str(payload) or "M112" in str(payload)


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
        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            result = adapter_with_mqtt.delete_file("/sdcard/old_model.3mf")

        assert result is True
        mock_ftp_class.delete.assert_called_once_with("/sdcard/old_model.3mf")

    def test_ftp_connection_error(self, adapter_with_mqtt: BambuAdapter) -> None:
        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS") as mock_ftp_cls:
            mock_ftp_cls.return_value.connect.side_effect = Exception("Connection refused")
            with pytest.raises(PrinterError, match="FTPS connection"):
                adapter_with_mqtt.delete_file("/sdcard/file.3mf")

    def test_ftp_delete_error(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        mock_ftp_class.delete.side_effect = Exception("File not found")

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            with pytest.raises(PrinterError, match="Failed to delete"):
                adapter_with_mqtt.delete_file("/sdcard/nonexistent.3mf")

    def test_ftp_quit_called(self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock) -> None:
        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
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

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            ftp = adapter._ftp_connect()

        mock_ftp_class.connect.assert_called_once_with(HOST, 990, timeout=2)
        mock_ftp_class.login.assert_called_once_with("bblp", ACCESS_CODE)
        mock_ftp_class.prot_p.assert_called_once()
        assert ftp is mock_ftp_class

    def test_ftp_connect_raises_on_failure(self) -> None:
        adapter = _adapter()

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS") as mock_ftp_cls:
            mock_ftp_cls.return_value.connect.side_effect = Exception("Connection failed")
            with pytest.raises(PrinterError, match="FTPS connection"):
                adapter._ftp_connect()

    def test_ftp_connect_pins_cert_on_first_use(
        self, mock_ftp_class: mock.MagicMock, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pin_file = tmp_path / "pins.json"
        monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(pin_file))
        adapter = _adapter()
        mock_ftp_class.sock.getpeercert.return_value = b"cert-a"

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            adapter._ftp_connect()

        assert pin_file.exists()
        pins = json.loads(pin_file.read_text(encoding="utf-8"))
        assert pins.get(HOST.lower()) == hashlib.sha256(b"cert-a").hexdigest()

    def test_ftp_connect_rejects_pin_mismatch(
        self, mock_ftp_class: mock.MagicMock, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pin_file = tmp_path / "pins.json"
        pins = {HOST.lower(): hashlib.sha256(b"cert-a").hexdigest()}
        pin_file.write_text(json.dumps(pins), encoding="utf-8")
        monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(pin_file))
        adapter = _adapter()
        mock_ftp_class.sock.getpeercert.return_value = b"cert-b"

        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            with pytest.raises(PrinterError, match="pin mismatch"):
                adapter._ftp_connect()

    def test_ftp_connect_insecure_mode_skips_cert_checks(
        self, mock_ftp_class: mock.MagicMock,
    ) -> None:
        adapter = _adapter(tls_mode="insecure")
        mock_ftp_class.sock.getpeercert.return_value = None
        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            ftp = adapter._ftp_connect()
        assert ftp is mock_ftp_class


# ---------------------------------------------------------------------------
# PrinterState serialization tests
# ---------------------------------------------------------------------------

class TestBambuAdapterSnapshot:
    """Tests for get_snapshot and get_stream_url."""

    def test_get_stream_url(self) -> None:
        adapter = _adapter()
        url = adapter.get_stream_url()
        assert url == f"rtsps://bblp:{ACCESS_CODE}@{HOST}:322/streaming/live/1"

    @mock.patch("kiln.printers.bambu._find_ffmpeg", return_value="/usr/bin/ffmpeg")
    @mock.patch("kiln.printers.bambu.subprocess.run")
    @mock.patch.object(BambuAdapter, "_capture_jpeg_frame", return_value=None)
    def test_get_snapshot_rtsps_fallback(self, mock_jpeg, mock_run, mock_ffmpeg) -> None:
        adapter = _adapter()
        fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        mock_run.return_value = mock.MagicMock(
            returncode=0,
            stdout=fake_jpeg,
        )
        result = adapter.get_snapshot()
        assert result is not None
        assert result == fake_jpeg
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0][0] == "/usr/bin/ffmpeg"
        assert "pipe:1" in call_args[0][0]

    @mock.patch("kiln.printers.bambu._find_ffmpeg", return_value=None)
    @mock.patch.object(BambuAdapter, "_capture_jpeg_frame", return_value=None)
    def test_get_snapshot_no_camera(self, mock_jpeg, mock_ffmpeg) -> None:
        adapter = _adapter()
        with pytest.raises(PrinterError, match="Neither is available"):
            adapter.get_snapshot()

    @mock.patch("kiln.printers.bambu._find_ffmpeg", return_value="/usr/bin/ffmpeg")
    @mock.patch("kiln.printers.bambu.subprocess.run")
    @mock.patch.object(BambuAdapter, "_capture_jpeg_frame", return_value=None)
    def test_get_snapshot_ffmpeg_timeout(self, mock_jpeg, mock_run, mock_ffmpeg) -> None:
        adapter = _adapter()
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10)
        with pytest.raises(PrinterError, match="RTSPS stream timed out"):
            adapter.get_snapshot()

    @mock.patch("kiln.printers.bambu._find_ffmpeg", return_value="/usr/bin/ffmpeg")
    def test_capabilities_with_ffmpeg(self, mock_ffmpeg) -> None:
        adapter = _adapter()
        caps = adapter.capabilities
        assert caps.can_snapshot is True
        assert caps.can_stream is True

    @mock.patch("kiln.printers.bambu._find_ffmpeg", return_value=None)
    def test_capabilities_without_ffmpeg(self, mock_ffmpeg) -> None:
        adapter = _adapter()
        caps = adapter.capabilities
        assert caps.can_snapshot is False
        assert caps.can_stream is True


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


# ---------------------------------------------------------------------------
# A1/A1 mini: uppercase state parsing
# ---------------------------------------------------------------------------


class TestBambuAdapterUppercaseState:
    """Tests for A1/A1 mini uppercase gcode_state values."""

    @pytest.mark.parametrize("state_str,expected", [
        ("IDLE", PrinterStatus.IDLE),
        ("RUNNING", PrinterStatus.PRINTING),
        ("PREPARE", PrinterStatus.BUSY),
        ("PAUSE", PrinterStatus.PAUSED),
        ("FAILED", PrinterStatus.ERROR),
        ("Idle", PrinterStatus.IDLE),
        ("Running", PrinterStatus.PRINTING),
        ("idle", PrinterStatus.IDLE),
        ("running", PrinterStatus.PRINTING),
    ])
    def test_case_insensitive_state_mapping(
        self, adapter_with_mqtt: BambuAdapter, state_str: str, expected: PrinterStatus,
    ) -> None:
        adapter_with_mqtt._last_status = {"gcode_state": state_str}
        state = adapter_with_mqtt.get_state()
        assert state.state == expected

    def test_on_message_uppercase_push_status(
        self, adapter_with_mqtt: BambuAdapter,
    ) -> None:
        """push_status command matching should be case-insensitive."""
        adapter_with_mqtt._last_status = {}
        msg = mock.MagicMock()
        msg.payload = json.dumps({
            "print": {
                "command": "PUSH_STATUS",
                "gcode_state": "RUNNING",
                "nozzle_temper": 210,
            }
        }).encode()
        adapter_with_mqtt._on_message(None, None, msg)
        assert adapter_with_mqtt._last_status.get("gcode_state") == "RUNNING"
        # get_state() should still normalise to lowercase.
        state = adapter_with_mqtt.get_state()
        assert state.state == PrinterStatus.PRINTING


# ---------------------------------------------------------------------------
# Print start confirmation
# ---------------------------------------------------------------------------


class TestBambuAdapterPrintConfirmation:
    """Tests for the print-start confirmation polling."""

    def test_confirmed_on_running(self, adapter_with_mqtt: BambuAdapter) -> None:
        # Pre-set idle so it enters the wait path.
        adapter_with_mqtt._last_status = {"gcode_state": "idle"}
        # Simulate state transition after a brief delay.
        original_sleep = time.sleep

        call_count = 0

        def fake_sleep(secs: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                adapter_with_mqtt._last_status["gcode_state"] = "running"

        with mock.patch("kiln.printers.bambu.time.sleep", side_effect=fake_sleep):
            result = adapter_with_mqtt.start_print("test.3mf")
        assert result.success is True

    def test_timeout_returns_failure(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"gcode_state": "idle"}
        # Mock time.monotonic to simulate timeout.
        with mock.patch("kiln.printers.bambu.time.monotonic", side_effect=[0.0, 0.0, 100.0]):
            with mock.patch("kiln.printers.bambu.time.sleep"):
                result = adapter_with_mqtt.start_print("test.3mf")
        assert result.success is False
        assert "did not transition" in result.message

    def test_skips_wait_when_already_active(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"gcode_state": "running"}
        result = adapter_with_mqtt.start_print("test.3mf")
        assert result.success is True

    def test_failure_on_error_state(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"gcode_state": "idle"}

        def fake_sleep(secs: float) -> None:
            adapter_with_mqtt._last_status["gcode_state"] = "failed"

        with mock.patch("kiln.printers.bambu.time.sleep", side_effect=fake_sleep):
            result = adapter_with_mqtt.start_print("test.3mf")
        assert result.success is False


# ---------------------------------------------------------------------------
# Implicit FTPS
# ---------------------------------------------------------------------------


class TestImplicitFTPTLS:
    """Tests for the _ImplicitFTP_TLS subclass."""

    def test_is_ftp_tls_subclass(self) -> None:
        import ftplib
        assert issubclass(_ImplicitFTP_TLS, ftplib.FTP_TLS)

    def test_ftp_connect_uses_implicit_class(
        self, adapter_with_mqtt: BambuAdapter, mock_ftp_class: mock.MagicMock,
    ) -> None:
        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=mock_ftp_class):
            ftp = adapter_with_mqtt._ftp_connect()
        assert ftp is mock_ftp_class
        mock_ftp_class.connect.assert_called_once()
        mock_ftp_class.login.assert_called_once()
        mock_ftp_class.prot_p.assert_called_once()


# ---------------------------------------------------------------------------
# Speed profile control
# ---------------------------------------------------------------------------


class TestBambuAdapterSpeedProfile:
    """Tests for speed profile get/set."""

    def test_get_speed_profile_standard(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"spd_lvl": 2, "spd_mag": 100}
        result = adapter_with_mqtt.get_speed_profile()
        assert result["level"] == 2
        assert result["name"] == "standard"
        assert result["speed_magnitude"] == 100

    def test_get_speed_profile_ludicrous(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"spd_lvl": 4, "spd_mag": 166}
        result = adapter_with_mqtt.get_speed_profile()
        assert result["level"] == 4
        assert result["name"] == "ludicrous"

    def test_get_speed_profile_missing(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {}
        result = adapter_with_mqtt.get_speed_profile()
        assert result["level"] is None
        assert result["name"] == "unknown"

    def test_get_speed_profile_mqtt_failure(self) -> None:
        adapter = _adapter()
        result = adapter.get_speed_profile()
        assert result["name"] == "unknown"

    def test_set_speed_profile_silent(self, adapter_with_mqtt: BambuAdapter) -> None:
        ok = adapter_with_mqtt.set_speed_profile("silent")
        assert ok is True
        adapter_with_mqtt._mqtt_client.publish.assert_called_once()
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["command"] == "print_speed"
        assert payload["print"]["param"] == "1"

    def test_set_speed_profile_sport(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.set_speed_profile("sport")
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["param"] == "3"

    def test_set_speed_profile_case_insensitive(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.set_speed_profile("LUDICROUS")
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["param"] == "4"

    def test_set_speed_profile_invalid(self, adapter_with_mqtt: BambuAdapter) -> None:
        with pytest.raises(PrinterError, match="Unknown speed profile"):
            adapter_with_mqtt.set_speed_profile("turbo")

    @pytest.mark.parametrize("profile,expected_param", [
        ("silent", "1"),
        ("standard", "2"),
        ("sport", "3"),
        ("ludicrous", "4"),
    ])
    def test_set_all_profiles(
        self, adapter_with_mqtt: BambuAdapter, profile: str, expected_param: str,
    ) -> None:
        adapter_with_mqtt.set_speed_profile(profile)
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["param"] == expected_param


# ---------------------------------------------------------------------------
# LED / light control
# ---------------------------------------------------------------------------


class TestBambuAdapterLightControl:
    """Tests for set_light."""

    def test_chamber_light_on(self, adapter_with_mqtt: BambuAdapter) -> None:
        ok = adapter_with_mqtt.set_light("chamber_light", "on")
        assert ok is True
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["system"]["command"] == "ledctrl"
        assert payload["system"]["led_node"] == "chamber_light"
        assert payload["system"]["led_mode"] == "on"

    def test_work_light_off(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.set_light("work_light", "off")
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["system"]["led_node"] == "work_light"
        assert payload["system"]["led_mode"] == "off"

    def test_flashing_mode(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.set_light("chamber_light", "flashing")
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["system"]["led_mode"] == "flashing"

    def test_invalid_node(self, adapter_with_mqtt: BambuAdapter) -> None:
        with pytest.raises(PrinterError, match="Unknown LED node"):
            adapter_with_mqtt.set_light("disco_ball", "on")

    def test_invalid_mode(self, adapter_with_mqtt: BambuAdapter) -> None:
        with pytest.raises(PrinterError, match="Unknown LED mode"):
            adapter_with_mqtt.set_light("chamber_light", "strobe")

    def test_case_insensitive(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.set_light("CHAMBER_LIGHT", "ON")
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["system"]["led_node"] == "chamber_light"
        assert payload["system"]["led_mode"] == "on"


# ---------------------------------------------------------------------------
# Rich monitoring (extended state + layer tracking)
# ---------------------------------------------------------------------------


class TestBambuAdapterRichMonitoring:
    """Tests for extended PrinterState and JobProgress fields."""

    def test_state_includes_fan_speeds(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "running",
            "cooling_fan_speed": 255,
            "big_fan1_speed": 128,
            "big_fan2_speed": 0,
            "heatbreak_fan_speed": 200,
        }
        state = adapter_with_mqtt.get_state()
        assert state.cooling_fan_speed == 255
        assert state.aux_fan_speed == 128
        assert state.chamber_fan_speed == 0
        assert state.heatbreak_fan_speed == 200

    def test_state_includes_speed_profile(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "running",
            "spd_lvl": 3,
            "spd_mag": 124,
        }
        state = adapter_with_mqtt.get_state()
        assert state.speed_profile == "sport"
        assert state.speed_magnitude == 124

    def test_state_includes_wifi_signal(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "idle",
            "wifi_signal": "-45dBm",
        }
        state = adapter_with_mqtt.get_state()
        assert state.wifi_signal == "-45dBm"

    def test_state_includes_nozzle_info(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "idle",
            "nozzle_diameter": "0.4",
            "nozzle_type": "stainless_steel",
        }
        state = adapter_with_mqtt.get_state()
        assert state.nozzle_diameter == "0.4"
        assert state.nozzle_type == "stainless_steel"

    def test_state_includes_print_error(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "failed",
            "print_error": 318734337,
        }
        state = adapter_with_mqtt.get_state()
        assert state.state == PrinterStatus.ERROR
        assert state.print_error == 318734337

    def test_state_to_dict_omits_none_extended(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {"gcode_state": "idle"}
        state = adapter_with_mqtt.get_state()
        d = state.to_dict()
        assert "cooling_fan_speed" not in d
        assert "wifi_signal" not in d
        assert "print_error" not in d
        assert "speed_profile" not in d

    def test_state_to_dict_includes_present_extended(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "running",
            "spd_lvl": 2,
            "spd_mag": 100,
            "cooling_fan_speed": 255,
        }
        state = adapter_with_mqtt.get_state()
        d = state.to_dict()
        assert d["speed_profile"] == "standard"
        assert d["speed_magnitude"] == 100
        assert d["cooling_fan_speed"] == 255

    def test_job_includes_layer_info(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "running",
            "gcode_file": "test.gcode",
            "mc_percent": 50,
            "mc_remaining_time": 30,
            "layer_num": 50,
            "total_layer_num": 200,
        }
        job = adapter_with_mqtt.get_job()
        assert job.current_layer == 50
        assert job.total_layers == 200

    def test_job_layer_info_missing(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "running",
            "gcode_file": "test.gcode",
            "mc_percent": 10,
        }
        job = adapter_with_mqtt.get_job()
        assert job.current_layer is None
        assert job.total_layers is None

    def test_job_to_dict_omits_none_layers(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "running",
            "gcode_file": "test.gcode",
            "mc_percent": 10,
        }
        job = adapter_with_mqtt.get_job()
        d = job.to_dict()
        assert "current_layer" not in d
        assert "total_layers" not in d

    def test_job_to_dict_includes_layers_when_present(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt._last_status = {
            "gcode_state": "running",
            "layer_num": 10,
            "total_layer_num": 100,
        }
        job = adapter_with_mqtt.get_job()
        d = job.to_dict()
        assert d["current_layer"] == 10
        assert d["total_layers"] == 100


# ---------------------------------------------------------------------------
# RTSP camera auth
# ---------------------------------------------------------------------------


class TestBambuAdapterRTSPAuth:
    """Tests for RTSP camera URL authentication."""

    def test_stream_url_includes_credentials(self) -> None:
        adapter = _adapter()
        url = adapter.get_stream_url()
        assert f"bblp:{ACCESS_CODE}@" in url
        assert url.startswith("rtsps://")
        assert url.endswith("/streaming/live/1")

    @mock.patch("kiln.printers.bambu._find_ffmpeg", return_value="/usr/bin/ffmpeg")
    @mock.patch("kiln.printers.bambu.subprocess.run")
    def test_snapshot_uses_authenticated_url(self, mock_run, mock_ffmpeg) -> None:
        adapter = _adapter()
        fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        mock_run.return_value = mock.MagicMock(returncode=0, stdout=fake_jpeg)
        adapter.get_snapshot()
        call_args = mock_run.call_args[0][0]
        # Find the -i argument
        i_index = call_args.index("-i")
        rtsp_url = call_args[i_index + 1]
        assert f"bblp:{ACCESS_CODE}@" in rtsp_url


# ---------------------------------------------------------------------------
# AMS Intelligence tests
# ---------------------------------------------------------------------------


class TestBambuAdapterAMSStatus:
    """Tests for the get_ams_status method: tray queries, edge cases."""

    def _adapter_with_ams(self, ams_data: list[dict[str, Any]]) -> BambuAdapter:
        """Create an adapter with pre-populated AMS data in the MQTT cache."""
        adapter = _adapter()
        adapter._mqtt_connected.set()
        adapter._connected = True
        adapter._mqtt_client = mock.MagicMock()
        publish_result = mock.MagicMock()
        publish_result.wait_for_publish = mock.MagicMock()
        adapter._mqtt_client.publish.return_value = publish_result
        adapter._last_status = {
            "gcode_state": "IDLE",
            "ams": ams_data,
            "ams_exist_bits": "1",
            "tray_exist_bits": "f",
            "tray_now": "0",
        }
        adapter._last_state_time = time.monotonic()
        return adapter

    def test_ams_status_single_unit_four_trays(self) -> None:
        adapter = self._adapter_with_ams([
            {
                "id": 0,
                "humidity": 3,
                "tray": [
                    {"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 85, "tag_uid": "abc123", "nozzle_temp_min": 190, "nozzle_temp_max": 230, "bed_temp": 60},
                    {"id": 1, "tray_type": "PETG", "tray_color": "00FF00FF", "remain": 42, "tag_uid": "def456", "nozzle_temp_min": 220, "nozzle_temp_max": 260, "bed_temp": 80},
                    {"id": 2, "tray_type": "PLA", "tray_color": "0000FFFF", "remain": 100, "tag_uid": "", "nozzle_temp_min": 190, "nozzle_temp_max": 230, "bed_temp": 60},
                    {"id": 3, "tray_type": "", "tray_color": "", "remain": None, "tag_uid": ""},
                ],
            }
        ])
        result = adapter.get_ams_status()
        assert result["ams_exist_bits"] == "1"
        assert result["tray_exist_bits"] == "f"
        assert result["tray_now"] == "0"
        assert len(result["units"]) == 1
        unit = result["units"][0]
        assert unit["unit_id"] == 0
        assert unit["humidity"] == 3
        assert len(unit["trays"]) == 4
        # Verify first tray
        t0 = unit["trays"][0]
        assert t0["slot"] == 0
        assert t0["tray_type"] == "PLA"
        assert t0["tray_color"] == "FF0000FF"
        assert t0["remain"] == 85
        assert t0["tag_uid"] == "abc123"
        assert t0["nozzle_temp_min"] == 190
        assert t0["nozzle_temp_max"] == 230
        assert t0["bed_temp"] == 60
        # Empty slot
        t3 = unit["trays"][3]
        assert t3["tray_type"] == ""
        assert t3["remain"] is None

    def test_ams_status_no_ams_attached(self) -> None:
        adapter = _adapter()
        adapter._mqtt_connected.set()
        adapter._connected = True
        adapter._mqtt_client = mock.MagicMock()
        publish_result = mock.MagicMock()
        publish_result.wait_for_publish = mock.MagicMock()
        adapter._mqtt_client.publish.return_value = publish_result
        adapter._last_status = {"gcode_state": "IDLE"}
        adapter._last_state_time = time.monotonic()
        result = adapter.get_ams_status()
        assert result["units"] == []
        assert result["ams_exist_bits"] == "0"

    def test_ams_status_ams_data_not_list(self) -> None:
        adapter = self._adapter_with_ams([])
        adapter._last_status["ams"] = "invalid"
        result = adapter.get_ams_status()
        assert result["units"] == []

    def test_ams_status_multiple_units(self) -> None:
        adapter = self._adapter_with_ams([
            {"id": 0, "humidity": 2, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 50, "tag_uid": ""}]},
            {"id": 1, "humidity": 5, "tray": [{"id": 0, "tray_type": "ABS", "tray_color": "000000FF", "remain": 30, "tag_uid": "xyz"}]},
        ])
        result = adapter.get_ams_status()
        assert len(result["units"]) == 2
        assert result["units"][0]["unit_id"] == 0
        assert result["units"][1]["unit_id"] == 1
        assert result["units"][1]["humidity"] == 5

    def test_ams_status_tray_now_255_means_external(self) -> None:
        adapter = self._adapter_with_ams([
            {"id": 0, "humidity": 3, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 85, "tag_uid": ""}]},
        ])
        adapter._last_status["tray_now"] = "255"
        result = adapter.get_ams_status()
        assert result["tray_now"] == "255"

    def test_ams_status_remain_as_string(self) -> None:
        """Some firmware versions report remain as a string."""
        adapter = self._adapter_with_ams([
            {"id": 0, "humidity": "3", "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "remain": "72", "tag_uid": ""}]},
        ])
        result = adapter.get_ams_status()
        assert result["units"][0]["humidity"] == 3
        assert result["units"][0]["trays"][0]["remain"] == 72

    def test_ams_status_malformed_tray_entry_skipped(self) -> None:
        adapter = self._adapter_with_ams([
            {"id": 0, "humidity": 3, "tray": ["not_a_dict", {"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 50, "tag_uid": ""}]},
        ])
        result = adapter.get_ams_status()
        assert len(result["units"][0]["trays"]) == 1

    def test_ams_status_malformed_unit_skipped(self) -> None:
        adapter = self._adapter_with_ams(["not_a_dict"])
        result = adapter.get_ams_status()
        assert result["units"] == []

    def test_ams_status_missing_tray_key(self) -> None:
        adapter = self._adapter_with_ams([{"id": 0, "humidity": 3}])
        result = adapter.get_ams_status()
        assert len(result["units"]) == 1
        assert result["units"][0]["trays"] == []


class TestBambuAdapterStartPrintConfigurable:
    """Tests for configurable start_print parameters (AMS, calibration, etc.)."""

    def test_start_print_with_ams_enabled(self, adapter_with_mqtt: BambuAdapter) -> None:
        result = adapter_with_mqtt.start_print("model.3mf", use_ams=True, ams_mapping=[0, 1, 2, 3])
        assert result.success is True
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["use_ams"] is True
        assert payload["print"]["ams_mapping"] == [0, 1, 2, 3]

    def test_start_print_defaults_ams_off(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.start_print("model.3mf")
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["use_ams"] is False
        assert payload["print"]["ams_mapping"] == [0]

    def test_start_print_timelapse_enabled(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.start_print("model.3mf", timelapse=True)
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["timelapse"] is True

    def test_start_print_skip_calibrations(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.start_print(
            "model.3mf", bed_leveling=False, flow_cali=False, vibration_cali=False,
        )
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["bed_leveling"] is False
        assert payload["print"]["flow_cali"] is False
        assert payload["print"]["vibration_cali"] is False

    def test_start_print_layer_inspect(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.start_print("model.3mf", layer_inspect=True)
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["layer_inspect"] is True

    def test_start_print_custom_bed_type(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.start_print("model.3mf", bed_type="textured_plate")
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["bed_type"] == "textured_plate"

    def test_start_print_plate_number(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.start_print("model.3mf", plate_number=3)
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["param"] == "Metadata/plate_3.gcode"

    def test_start_print_ams_mapping_negative_one(self, adapter_with_mqtt: BambuAdapter) -> None:
        """ams_mapping with -1 means unused slot."""
        adapter_with_mqtt.start_print("model.3mf", use_ams=True, ams_mapping=[0, -1, -1, -1])
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["ams_mapping"] == [0, -1, -1, -1]

    def test_start_print_invalid_ams_mapping_type(self, adapter_with_mqtt: BambuAdapter) -> None:
        """Non-list ams_mapping falls back to default."""
        adapter_with_mqtt.start_print("model.3mf", ams_mapping="invalid")
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["ams_mapping"] == [0]

    def test_start_print_gcode_ignores_ams_params(self, adapter_with_mqtt: BambuAdapter) -> None:
        """G-code files use gcode_file command, not project_file — kwargs are ignored."""
        adapter_with_mqtt.start_print("test.gcode", use_ams=True, timelapse=True)
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["command"] == "gcode_file"
        assert "use_ams" not in payload["print"]

    def test_start_print_all_params_combined(self, adapter_with_mqtt: BambuAdapter) -> None:
        adapter_with_mqtt.start_print(
            "multi.3mf",
            use_ams=True,
            ams_mapping=[2, 0, 1, 3],
            timelapse=True,
            bed_leveling=False,
            flow_cali=False,
            vibration_cali=False,
            layer_inspect=True,
            bed_type="engineering_plate",
            plate_number=2,
        )
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        p = payload["print"]
        assert p["use_ams"] is True
        assert p["ams_mapping"] == [2, 0, 1, 3]
        assert p["timelapse"] is True
        assert p["bed_leveling"] is False
        assert p["flow_cali"] is False
        assert p["vibration_cali"] is False
        assert p["layer_inspect"] is True
        assert p["bed_type"] == "engineering_plate"
        assert p["param"] == "Metadata/plate_2.gcode"


# ---------------------------------------------------------------------------
# 3MF filament detection tests
# ---------------------------------------------------------------------------


class TestDetect3mfFilaments:
    """Tests for _detect_3mf_filaments static method."""

    def test_single_plate_two_colors(self, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr(
                "Metadata/plate_1.json",
                json.dumps({"filament_colors": ["#FFFFFF", "#808080"]}),
            )
        result = BambuAdapter._detect_3mf_filaments(str(threemf), 1)
        assert result == ["#FFFFFF", "#808080"]

    def test_plate_2_metadata(self, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr(
                "Metadata/plate_1.json",
                json.dumps({"filament_colors": ["#FF0000"]}),
            )
            zf.writestr(
                "Metadata/plate_2.json",
                json.dumps({"filament_colors": ["#00FF00", "#0000FF"]}),
            )
        result = BambuAdapter._detect_3mf_filaments(str(threemf), 2)
        assert result == ["#00FF00", "#0000FF"]

    def test_missing_metadata_returns_none(self, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr("3D/model.model", "<model/>")
        result = BambuAdapter._detect_3mf_filaments(str(threemf), 1)
        assert result is None

    def test_empty_colors_returns_none(self, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr(
                "Metadata/plate_1.json",
                json.dumps({"filament_colors": []}),
            )
        result = BambuAdapter._detect_3mf_filaments(str(threemf), 1)
        assert result is None

    def test_invalid_zip_returns_none(self, tmp_path: Any) -> None:
        bad_file = tmp_path / "notazip.3mf"
        bad_file.write_text("not a zip")
        result = BambuAdapter._detect_3mf_filaments(str(bad_file), 1)
        assert result is None

    def test_nonexistent_file_returns_none(self) -> None:
        result = BambuAdapter._detect_3mf_filaments("/nonexistent/path.3mf", 1)
        assert result is None

    def test_missing_filament_colors_key(self, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr(
                "Metadata/plate_1.json",
                json.dumps({"other_key": "value"}),
            )
        result = BambuAdapter._detect_3mf_filaments(str(threemf), 1)
        assert result is None


# ---------------------------------------------------------------------------
# AMS color mismatch check tests
# ---------------------------------------------------------------------------


class TestCheckAmsColorMismatch:
    """Tests for _check_ams_color_mismatch method."""

    def test_matching_colors_no_warnings(self, adapter_with_mqtt: BambuAdapter, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr(
                "Metadata/plate_1.json",
                json.dumps({"filament_colors": ["#FFFFFF", "#000000"]}),
            )
        # Mock AMS status with matching colors.
        adapter_with_mqtt.get_ams_status = mock.MagicMock(return_value={
            "units": [{
                "trays": [
                    {"slot": 0, "tray_color": "FFFFFFFF"},
                    {"slot": 1, "tray_color": "000000FF"},
                ],
            }],
        })
        warnings = adapter_with_mqtt._check_ams_color_mismatch(str(threemf), 1, [0, 1])
        assert warnings == []

    def test_mismatched_color_returns_warning(self, adapter_with_mqtt: BambuAdapter, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr(
                "Metadata/plate_1.json",
                json.dumps({"filament_colors": ["#FFFFFF", "#808080"]}),
            )
        # AMS slot 1 has black instead of gray.
        adapter_with_mqtt.get_ams_status = mock.MagicMock(return_value={
            "units": [{
                "trays": [
                    {"slot": 0, "tray_color": "FFFFFFFF"},
                    {"slot": 1, "tray_color": "000000FF"},
                ],
            }],
        })
        warnings = adapter_with_mqtt._check_ams_color_mismatch(str(threemf), 1, [0, 1])
        assert len(warnings) == 1
        assert "mismatch" in warnings[0].lower()
        assert "808080" in warnings[0]
        assert "000000" in warnings[0]

    def test_no_3mf_metadata_returns_empty(self, adapter_with_mqtt: BambuAdapter, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr("3D/model.model", "<model/>")
        warnings = adapter_with_mqtt._check_ams_color_mismatch(str(threemf), 1, [0])
        assert warnings == []

    def test_ams_error_returns_empty(self, adapter_with_mqtt: BambuAdapter, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr(
                "Metadata/plate_1.json",
                json.dumps({"filament_colors": ["#FFFFFF"]}),
            )
        # Simulate AMS status failure.
        adapter_with_mqtt.get_ams_status = mock.MagicMock(side_effect=Exception("AMS offline"))
        warnings = adapter_with_mqtt._check_ams_color_mismatch(str(threemf), 1, [0])
        assert warnings == []


# ---------------------------------------------------------------------------
# Auto-detect filament + AMS integration in start_print
# ---------------------------------------------------------------------------


class TestStartPrintAutoDetect:
    """Tests for auto-detection of filament count and AMS color warnings in start_print."""

    def test_auto_detect_enables_ams_for_multi_filament(self, adapter_with_mqtt: BambuAdapter, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr(
                "Metadata/plate_1.json",
                json.dumps({"filament_colors": ["#FFFFFF", "#808080"]}),
            )
        result = adapter_with_mqtt.start_print(
            "model.3mf", local_file_path=str(threemf),
        )
        assert result.success is True
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["use_ams"] is True
        assert payload["print"]["ams_mapping"] == [0, 1]

    def test_explicit_ams_mapping_overrides_auto_detect(self, adapter_with_mqtt: BambuAdapter, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr(
                "Metadata/plate_1.json",
                json.dumps({"filament_colors": ["#FFFFFF", "#808080"]}),
            )
        result = adapter_with_mqtt.start_print(
            "model.3mf", ams_mapping=[2, 3], use_ams=True, local_file_path=str(threemf),
        )
        assert result.success is True
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["ams_mapping"] == [2, 3]

    def test_single_filament_no_auto_ams(self, adapter_with_mqtt: BambuAdapter, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr(
                "Metadata/plate_1.json",
                json.dumps({"filament_colors": ["#FFFFFF"]}),
            )
        result = adapter_with_mqtt.start_print(
            "model.3mf", local_file_path=str(threemf),
        )
        assert result.success is True
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        # Single filament: no AMS needed.
        assert payload["print"]["use_ams"] is False
        assert payload["print"]["ams_mapping"] == [0]

    def test_color_warning_in_result_message(self, adapter_with_mqtt: BambuAdapter, tmp_path: Any) -> None:
        import zipfile

        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr(
                "Metadata/plate_1.json",
                json.dumps({"filament_colors": ["#FFFFFF", "#808080"]}),
            )
        adapter_with_mqtt.get_ams_status = mock.MagicMock(return_value={
            "units": [{
                "trays": [
                    {"slot": 0, "tray_color": "FFFFFFFF"},
                    {"slot": 1, "tray_color": "000000FF"},
                ],
            }],
        })
        result = adapter_with_mqtt.start_print(
            "model.3mf", local_file_path=str(threemf),
        )
        assert result.success is True
        assert "WARNING" in result.message
        assert "mismatch" in result.message.lower()


# ---------------------------------------------------------------------------
# Gcode path based on storage detection
# ---------------------------------------------------------------------------


class TestStartPrintGcodePath:
    """Tests for gcode file path construction based on detected storage path."""

    def test_default_path_a1_series(self, adapter_with_mqtt: BambuAdapter) -> None:
        """Default (no cached storage path) uses /sdcard/model/ (A1 series)."""
        adapter_with_mqtt.start_print("test.gcode")
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["param"] == "/sdcard/model/test.gcode"

    def test_cached_model_path(self, adapter_with_mqtt: BambuAdapter) -> None:
        """When upload detected /model (A1), gcode uses /sdcard/model/."""
        adapter_with_mqtt._last_storage_path = "/model"
        adapter_with_mqtt.start_print("test.gcode")
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["param"] == "/sdcard/model/test.gcode"

    def test_cached_sdcard_path_x1_p1(self, adapter_with_mqtt: BambuAdapter) -> None:
        """When upload detected /sdcard (X1/P1), gcode uses /sdcard/."""
        adapter_with_mqtt._last_storage_path = "/sdcard"
        adapter_with_mqtt.start_print("test.gcode")
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["param"] == "/sdcard/test.gcode"

    def test_full_path_preserved(self, adapter_with_mqtt: BambuAdapter) -> None:
        """Full path (starts with /) is preserved regardless of cache."""
        adapter_with_mqtt._last_storage_path = "/model"
        adapter_with_mqtt.start_print("/sdcard/custom/test.gcode")
        payload = json.loads(adapter_with_mqtt._mqtt_client.publish.call_args[0][1])
        assert payload["print"]["param"] == "/sdcard/custom/test.gcode"
