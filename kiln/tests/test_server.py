"""Tests for kiln.server -- MCP tool functions with mocked adapter.

All tests patch ``kiln.server._get_adapter`` (or the module-level globals)
so that no real HTTP requests are made and no real OctoPrint instance is
needed.

Covers:
- printer_status() returns structured response
- printer_files() returns file list
- upload_file() success and file not found
- start_print(), cancel_print(), pause_print(), resume_print()
- set_temperature() with tool, bed, both, neither (error)
- preflight_check() all-pass and various failure scenarios
- send_gcode() success, empty commands, and non-supported adapter
- _get_adapter() missing env vars raises RuntimeError
- _get_adapter() unsupported printer type raises RuntimeError
import json
- _get_adapter() creates correct adapter for octoprint and moonraker
- _validate_local_file() with valid, invalid extension, missing, and empty files
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import tempfile
import zipfile
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from kiln.printers.bambu import BambuAdapter
from kiln.printers.base import (
    PrinterError,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from kiln.printers.moonraker import MoonrakerAdapter
from kiln.printers.octoprint import OctoPrintAdapter
from kiln.server import (
    _error_dict,
    _format_duration,
    _generate_print_comment,
    _tool_limiter,
    _validate_local_file,
    check_orientation,
    get_bed_mesh,
    get_filament_status,
    get_speed_profile,
    get_tool_position,
    monitor_print,
    multi_copy_print,
    preflight_check,
    printer_files,
    printer_status,
    reslice_with_overrides,
    rotate_model,
    send_gcode,
    set_temperature,
    wrap_gcode_as_3mf,
)
from kiln.server import (
    cancel_print as server_cancel_print,
)
from kiln.server import (
    clear_emergency_stop as server_clear_emergency_stop,
)
from kiln.server import (
    delete_file as server_delete_file,
)
from kiln.server import (
    emergency_status as server_emergency_status,
)
from kiln.server import (
    emergency_trip_input as server_emergency_trip_input,
)
from kiln.server import (
    pause_print as server_pause_print,
)
from kiln.server import (
    resume_print as server_resume_print,
)
from kiln.server import (
    start_print as server_start_print,
)
from kiln.server import (
    upload_file as server_upload_file,
)


@pytest.fixture(autouse=True)
def _disable_rate_limiter(monkeypatch):
    """Disable rate limiting in tests to prevent timing-dependent failures."""
    monkeypatch.setattr(_tool_limiter, "check", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _reset_emergency_state(monkeypatch):
    """Keep server tests isolated from any local persisted E-stop state."""
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    import kiln.emergency as _emergency_mod

    _emergency_mod._coordinator = None
    yield
    _emergency_mod._coordinator = None



# ---------------------------------------------------------------------------
# _error_dict helper
# ---------------------------------------------------------------------------

class TestErrorDict:
    """Tests for the _error_dict helper function."""

    def test_default_code(self):
        d = _error_dict("something broke")
        assert d["success"] is False
        assert d["error"]["code"] == "ERROR"
        assert d["error"]["message"] == "something broke"

    def test_custom_code(self):
        d = _error_dict("not found", code="FILE_NOT_FOUND")
        assert d["error"]["code"] == "FILE_NOT_FOUND"

    def test_retryable_inferred_true_for_generic_error(self):
        d = _error_dict("timeout")
        assert d["error"]["retryable"] is True

    def test_retryable_inferred_true_for_internal_error(self):
        d = _error_dict("oops", code="INTERNAL_ERROR")
        assert d["error"]["retryable"] is True

    def test_retryable_inferred_false_for_not_found(self):
        d = _error_dict("missing", code="NOT_FOUND")
        assert d["error"]["retryable"] is False

    def test_retryable_inferred_false_for_auth(self):
        d = _error_dict("denied", code="AUTH_ERROR")
        assert d["error"]["retryable"] is False

    def test_retryable_inferred_false_for_unsupported(self):
        d = _error_dict("nope", code="UNSUPPORTED")
        assert d["error"]["retryable"] is False

    def test_retryable_explicit_override(self):
        d = _error_dict("network flake", code="NOT_FOUND", retryable=True)
        assert d["error"]["retryable"] is True


# ---------------------------------------------------------------------------
# printer_status()
# ---------------------------------------------------------------------------

class TestPrinterStatus:
    """Tests for the printer_status MCP tool."""

    @patch("kiln.server._get_adapter")
    def test_success(self, mock_get_adapter, mock_printer_state_idle, mock_job_progress, mock_capabilities):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = mock_printer_state_idle
        adapter.get_job.return_value = mock_job_progress
        type(adapter).capabilities = PropertyMock(return_value=mock_capabilities)
        mock_get_adapter.return_value = adapter

        result = printer_status()
        assert result["success"] is True
        assert result["printer"]["state"] == "idle"
        assert result["printer"]["connected"] is True
        assert result["job"]["file_name"] == "benchy.gcode"
        assert "capabilities" in result

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter):
        mock_get_adapter.side_effect = PrinterError("connection lost")
        result = printer_status()
        assert result["success"] is False
        assert "connection lost" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_runtime_error(self, mock_get_adapter):
        mock_get_adapter.side_effect = RuntimeError("env not set")
        result = printer_status()
        assert result["success"] is False
        assert "env not set" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_unexpected_error(self, mock_get_adapter):
        mock_get_adapter.side_effect = ValueError("weird")
        result = printer_status()
        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# printer_files()
# ---------------------------------------------------------------------------

class TestPrinterFiles:
    """Tests for the printer_files MCP tool."""

    @patch("kiln.server._get_adapter")
    def test_success(self, mock_get_adapter, mock_file_list):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.list_files.return_value = mock_file_list
        mock_get_adapter.return_value = adapter

        result = printer_files()
        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["files"]) == 2
        assert result["files"][0]["name"] == "benchy.gcode"

    @patch("kiln.server._get_adapter")
    def test_empty_list(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.list_files.return_value = []
        mock_get_adapter.return_value = adapter

        result = printer_files()
        assert result["success"] is True
        assert result["count"] == 0
        assert result["files"] == []

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.list_files.side_effect = PrinterError("timeout")
        mock_get_adapter.return_value = adapter

        result = printer_files()
        assert result["success"] is False


# ---------------------------------------------------------------------------
# upload_file()
# ---------------------------------------------------------------------------

class TestUploadFile:
    """Tests for the upload_file MCP tool."""

    @patch("kiln.server.os.path.getsize", return_value=1024)
    @patch("kiln.server.os.path.isfile", return_value=True)
    @patch("kiln.server._get_adapter")
    def test_success(self, mock_get_adapter, mock_isfile, mock_getsize):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.upload_file.return_value = UploadResult(
            success=True, file_name="test.gcode", message="Uploaded test.gcode"
        )
        mock_get_adapter.return_value = adapter

        result = server_upload_file("/some/path/test.gcode")
        assert result["success"] is True
        assert result["file_name"] == "test.gcode"

    @patch("kiln.server._get_adapter")
    def test_file_not_found(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.upload_file.side_effect = FileNotFoundError("not found: /bad/path")
        mock_get_adapter.return_value = adapter

        result = server_upload_file("/bad/path")
        assert result["success"] is False
        assert result["error"]["code"] == "FILE_NOT_FOUND"

    @patch("kiln.server.os.path.getsize", return_value=1024)
    @patch("kiln.server.os.path.isfile", return_value=True)
    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter, mock_isfile, mock_getsize):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.upload_file.side_effect = PrinterError("upload failed")
        mock_get_adapter.return_value = adapter

        result = server_upload_file("/some/file.gcode")
        assert result["success"] is False
        assert "upload failed" in result["error"]["message"]


# ---------------------------------------------------------------------------
# delete_file()
# ---------------------------------------------------------------------------

class TestDeleteFile:
    """Tests for the delete_file MCP tool."""

    @patch("kiln.server._get_adapter")
    def test_success(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.delete_file.return_value = True
        mock_get_adapter.return_value = adapter

        result = server_delete_file("benchy.gcode")
        assert result["success"] is True
        assert "Deleted" in result["message"]
        adapter.delete_file.assert_called_once_with("benchy.gcode")

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.delete_file.side_effect = PrinterError("file not found")
        mock_get_adapter.return_value = adapter

        result = server_delete_file("missing.gcode")
        assert result["success"] is False

    @patch("kiln.server._get_adapter")
    def test_runtime_error(self, mock_get_adapter):
        mock_get_adapter.side_effect = RuntimeError("no adapter")
        result = server_delete_file("test.gcode")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# start_print()
# ---------------------------------------------------------------------------

class TestStartPrint:
    """Tests for the start_print MCP tool."""

    @patch("kiln.server._get_adapter")
    def test_success(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.start_print.return_value = PrintResult(
            success=True, message="Started printing benchy.gcode."
        )
        # Provide a proper PrinterState so the auto-preflight check passes
        adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.IDLE,
            connected=True,
            tool_temp_actual=22.0,
            tool_temp_target=0.0,
            bed_temp_actual=21.0,
            bed_temp_target=0.0,
        )
        adapter.get_job.return_value = MagicMock(file_name=None)
        # Mock list_files so the file_on_printer preflight check passes
        adapter.list_files.return_value = [
            PrinterFile(name="benchy.gcode", path="benchy.gcode", size_bytes=1234),
        ]
        mock_get_adapter.return_value = adapter

        result = server_start_print("benchy.gcode")
        assert result["success"] is True
        assert "benchy" in result["message"]

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.start_print.side_effect = PrinterError("file not found on printer")
        mock_get_adapter.return_value = adapter

        result = server_start_print("missing.gcode")
        assert result["success"] is False

    @patch("kiln.server._emergency_latch_error")
    def test_blocked_when_emergency_latched(self, mock_latch_error):
        mock_latch_error.return_value = _error_dict("latched", code="E_STOP_LATCHED", retryable=False)
        result = server_start_print("benchy.gcode")
        assert result["success"] is False
        assert result["error"]["code"] == "E_STOP_LATCHED"


# ---------------------------------------------------------------------------
# cancel_print()
# ---------------------------------------------------------------------------

class TestCancelPrint:
    """Tests for the cancel_print MCP tool."""

    @patch("kiln.server._get_adapter")
    def test_success(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.cancel_print.return_value = PrintResult(success=True, message="Print cancelled.")
        mock_get_adapter.return_value = adapter

        result = server_cancel_print()
        assert result["success"] is True

    @patch("kiln.server._get_adapter")
    def test_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.cancel_print.side_effect = PrinterError("no active job")
        mock_get_adapter.return_value = adapter

        result = server_cancel_print()
        assert result["success"] is False


# ---------------------------------------------------------------------------
# pause_print()
# ---------------------------------------------------------------------------

class TestPausePrint:
    """Tests for the pause_print MCP tool."""

    @patch("kiln.server._get_adapter")
    def test_success(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.pause_print.return_value = PrintResult(success=True, message="Print paused.")
        mock_get_adapter.return_value = adapter

        result = server_pause_print()
        assert result["success"] is True

    @patch("kiln.server._get_adapter")
    def test_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.pause_print.side_effect = PrinterError("cannot pause")
        mock_get_adapter.return_value = adapter

        result = server_pause_print()
        assert result["success"] is False


# ---------------------------------------------------------------------------
# resume_print()
# ---------------------------------------------------------------------------

class TestResumePrint:
    """Tests for the resume_print MCP tool."""

    @patch("kiln.server._get_adapter")
    def test_success(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.resume_print.return_value = PrintResult(success=True, message="Print resumed.")
        mock_get_adapter.return_value = adapter

        result = server_resume_print()
        assert result["success"] is True

    @patch("kiln.server._get_adapter")
    def test_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.resume_print.side_effect = PrinterError("not paused")
        mock_get_adapter.return_value = adapter

        result = server_resume_print()
        assert result["success"] is False

    @patch("kiln.server._emergency_latch_error")
    def test_blocked_when_emergency_latched(self, mock_latch_error):
        mock_latch_error.return_value = _error_dict("latched", code="E_STOP_LATCHED", retryable=False)
        result = server_resume_print()
        assert result["success"] is False
        assert result["error"]["code"] == "E_STOP_LATCHED"


class TestEmergencyTools:
    @patch("kiln.emergency.get_emergency_coordinator")
    def test_emergency_status_single(self, mock_get_coord):
        coord = MagicMock()
        coord.get_latch_status.return_value = {"printer_id": "default", "latched": False}
        mock_get_coord.return_value = coord

        result = server_emergency_status("default")
        assert result["success"] is True
        assert result["emergency_status"]["printer_id"] == "default"

    @patch("kiln.emergency.get_emergency_coordinator")
    def test_emergency_clear_success(self, mock_get_coord):
        coord = MagicMock()
        coord.clear_stop_with_ack.return_value = {
            "success": True,
            "status": {"printer_id": "default", "latched": False},
            "message": "cleared",
        }
        mock_get_coord.return_value = coord

        result = server_clear_emergency_stop("default", "operator confirmed", acknowledged_by="adam")
        assert result["success"] is True
        assert result["cleared"] is True

    @patch("kiln.server._ESTOP_INPUT_TOKEN", "abc123")
    def test_emergency_trip_input_requires_token(self):
        result = server_emergency_trip_input("default", token="wrong")
        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# set_temperature()
# ---------------------------------------------------------------------------

class TestSetTemperature:
    """Tests for the set_temperature MCP tool."""

    @patch("kiln.server._get_adapter")
    def test_tool_only(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.set_tool_temp.return_value = True
        mock_get_adapter.return_value = adapter

        result = set_temperature(tool_temp=210.0)
        assert result["success"] is True
        assert result["tool"]["target"] == 210.0
        assert result["tool"]["accepted"] is True
        assert "bed" not in result

    @patch("kiln.server._get_adapter")
    def test_bed_only(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.set_bed_temp.return_value = True
        mock_get_adapter.return_value = adapter

        result = set_temperature(bed_temp=60.0)
        assert result["success"] is True
        assert result["bed"]["target"] == 60.0
        assert result["bed"]["accepted"] is True
        assert "tool" not in result

    @patch("kiln.server._get_adapter")
    def test_both(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.set_tool_temp.return_value = True
        adapter.set_bed_temp.return_value = True
        mock_get_adapter.return_value = adapter

        result = set_temperature(tool_temp=200.0, bed_temp=60.0)
        assert result["success"] is True
        assert "tool" in result
        assert "bed" in result

    def test_neither_returns_error(self):
        result = set_temperature(tool_temp=None, bed_temp=None)
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.set_tool_temp.side_effect = PrinterError("printer busy")
        mock_get_adapter.return_value = adapter

        result = set_temperature(tool_temp=200.0)
        assert result["success"] is False


# ---------------------------------------------------------------------------
# preflight_check()
# ---------------------------------------------------------------------------

class TestPreflightCheck:
    """Tests for the preflight_check MCP tool."""

    @patch("kiln.server._get_adapter")
    def test_all_pass_no_file(self, mock_get_adapter, mock_printer_state_idle):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = mock_printer_state_idle
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert result["success"] is True
        assert result["ready"] is True
        assert len(result["errors"]) == 0
        check_names = [c["name"] for c in result["checks"]]
        assert "printer_connected" in check_names
        assert "printer_idle" in check_names
        assert "no_errors" in check_names
        assert "temperatures_safe" in check_names

    @patch("kiln.server._get_adapter")
    def test_offline_printer_fails(self, mock_get_adapter, mock_printer_state_offline):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = mock_printer_state_offline
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert result["success"] is True
        assert result["ready"] is False
        assert len(result["errors"]) > 0

    @patch("kiln.server._get_adapter")
    def test_printing_state_fails(self, mock_get_adapter, mock_printer_state_printing):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = mock_printer_state_printing
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert result["ready"] is False
        errors_text = " ".join(result["errors"])
        assert "not idle" in errors_text.lower()

    @patch("kiln.server._get_adapter")
    def test_error_state_fails(self, mock_get_adapter, mock_printer_state_error):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = mock_printer_state_error
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert result["ready"] is False
        errors_text = " ".join(result["errors"])
        assert "error" in errors_text.lower()

    @patch("kiln.server._get_adapter")
    def test_high_tool_temp_warning(self, mock_get_adapter):
        state = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            tool_temp_actual=310.0,
            tool_temp_target=0.0,
            bed_temp_actual=23.0,
            bed_temp_target=0.0,
        )
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = state
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert result["ready"] is False
        temp_check = next(c for c in result["checks"] if c["name"] == "temperatures_safe")
        assert temp_check["passed"] is False
        assert "Tool temp" in temp_check["message"]

    @patch("kiln.server._get_adapter")
    def test_high_bed_temp_warning(self, mock_get_adapter):
        state = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            tool_temp_actual=24.0,
            tool_temp_target=0.0,
            bed_temp_actual=140.0,
            bed_temp_target=0.0,
        )
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = state
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert result["ready"] is False
        temp_check = next(c for c in result["checks"] if c["name"] == "temperatures_safe")
        assert temp_check["passed"] is False
        assert "Bed temp" in temp_check["message"]

    @patch("kiln.server._get_adapter")
    def test_with_valid_file(self, mock_get_adapter, mock_printer_state_idle):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = mock_printer_state_idle
        mock_get_adapter.return_value = adapter

        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\nG1 X10\n")
            tmp_path = f.name

        try:
            result = preflight_check(file_path=tmp_path)
            assert result["ready"] is True
            assert "file" in result
            assert result["file"]["valid"] is True
            file_check = next(c for c in result["checks"] if c["name"] == "file_valid")
            assert file_check["passed"] is True
        finally:
            os.unlink(tmp_path)

    @patch("kiln.server._get_adapter")
    def test_with_missing_file(self, mock_get_adapter, mock_printer_state_idle):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = mock_printer_state_idle
        mock_get_adapter.return_value = adapter

        result = preflight_check(file_path="/nonexistent/file.gcode")
        assert result["ready"] is False
        assert "file" in result
        assert result["file"]["valid"] is False

    @patch("kiln.server._get_adapter")
    def test_with_bad_extension(self, mock_get_adapter, mock_printer_state_idle):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = mock_printer_state_idle
        mock_get_adapter.return_value = adapter

        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
            f.write(b"solid model\n")
            tmp_path = f.name

        try:
            result = preflight_check(file_path=tmp_path)
            assert result["ready"] is False
            assert result["file"]["valid"] is False
            assert any("extension" in e.lower() for e in result["file"]["errors"])
        finally:
            os.unlink(tmp_path)

    @patch("kiln.server._get_adapter")
    def test_printer_error_in_preflight(self, mock_get_adapter):
        mock_get_adapter.side_effect = PrinterError("cannot reach printer")
        result = preflight_check()
        assert result["success"] is False

    @patch("kiln.server._get_adapter")
    def test_summary_message_all_pass(self, mock_get_adapter, mock_printer_state_idle):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = mock_printer_state_idle
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert "Ready to print" in result["summary"]

    @patch("kiln.server._get_adapter")
    def test_summary_message_failure(self, mock_get_adapter, mock_printer_state_offline):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = mock_printer_state_offline
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert "failed" in result["summary"].lower()

    @patch("kiln.server._get_adapter")
    def test_temperatures_included(self, mock_get_adapter, mock_printer_state_idle):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.return_value = mock_printer_state_idle
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert "temperatures" in result
        assert result["temperatures"]["tool_actual"] == 24.5


# ---------------------------------------------------------------------------
# send_gcode()
# ---------------------------------------------------------------------------

class TestSendGcode:
    """Tests for the send_gcode MCP tool."""

    @patch("kiln.server._get_adapter")
    def test_single_command_octoprint(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        mock_get_adapter.return_value = adapter

        result = send_gcode("G28")
        assert result["success"] is True
        assert result["count"] == 1
        assert result["commands_sent"] == ["G28"]

    @patch("kiln.server._get_adapter")
    def test_multiple_commands_newline(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        mock_get_adapter.return_value = adapter

        result = send_gcode("G28\nG1 X10 Y10\nM104 S200")
        assert result["success"] is True
        assert result["count"] == 3
        assert result["commands_sent"] == ["G28", "G1 X10 Y10", "M104 S200"]

    @patch("kiln.server._get_adapter")
    def test_empty_commands(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        mock_get_adapter.return_value = adapter

        result = send_gcode("")
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"

    @patch("kiln.server._get_adapter")
    def test_whitespace_only(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        mock_get_adapter.return_value = adapter

        result = send_gcode("   \n  \n  ")
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"

    @patch("kiln.server._get_adapter")
    def test_commands_with_extra_whitespace(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        mock_get_adapter.return_value = adapter

        result = send_gcode("  G28  \n\n  G1 Z10  \n")
        assert result["success"] is True
        assert result["commands_sent"] == ["G28", "G1 Z10"]

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.capabilities.can_send_gcode = True
        adapter.send_gcode.side_effect = PrinterError("printer busy")
        mock_get_adapter.return_value = adapter

        result = send_gcode("G28")
        assert result["success"] is False

    @patch("kiln.server._get_adapter")
    def test_moonraker_adapter(self, mock_get_adapter):
        """send_gcode works with MoonrakerAdapter via adapter.send_gcode."""
        adapter = MagicMock(spec=MoonrakerAdapter)
        adapter.capabilities.can_send_gcode = True
        mock_get_adapter.return_value = adapter

        result = send_gcode("G28\nG1 Z10")
        assert result["success"] is True
        assert result["count"] == 2
        adapter.send_gcode.assert_called_once_with(["G28", "G1 Z10"])

    @patch("kiln.server._get_adapter")
    def test_unsupported_adapter(self, mock_get_adapter):
        """send_gcode returns UNSUPPORTED when capabilities.can_send_gcode is False."""
        adapter = MagicMock()
        adapter.name = "other_printer"
        adapter.capabilities.can_send_gcode = False
        mock_get_adapter.return_value = adapter

        result = send_gcode("G28")
        assert result["success"] is False
        assert result["error"]["code"] == "UNSUPPORTED"


# ---------------------------------------------------------------------------
# _get_adapter() tests
# ---------------------------------------------------------------------------

class TestGetAdapter:
    """Tests for the _get_adapter lazy-initialisation function."""

    def test_missing_host(self, monkeypatch):
        """Missing KILN_PRINTER_HOST raises RuntimeError."""
        import kiln.server as mod

        monkeypatch.setattr(mod, "_adapter", None)
        monkeypatch.setattr(mod, "_PRINTER_HOST", "")
        monkeypatch.setattr(mod, "_PRINTER_API_KEY", "somekey")
        monkeypatch.setattr(mod, "_PRINTER_TYPE", "octoprint")

        with pytest.raises(RuntimeError, match="KILN_PRINTER_HOST"):
            mod._get_adapter()

    def test_missing_api_key_for_octoprint(self, monkeypatch):
        """Missing KILN_PRINTER_API_KEY raises RuntimeError for octoprint."""
        import kiln.server as mod

        monkeypatch.setattr(mod, "_adapter", None)
        monkeypatch.setattr(mod, "_PRINTER_HOST", "http://localhost")
        monkeypatch.setattr(mod, "_PRINTER_API_KEY", "")
        monkeypatch.setattr(mod, "_PRINTER_TYPE", "octoprint")

        with pytest.raises(RuntimeError, match="KILN_PRINTER_API_KEY"):
            mod._get_adapter()

    def test_unsupported_printer_type(self, monkeypatch):
        """Unsupported printer type raises RuntimeError."""
        import kiln.server as mod

        monkeypatch.setattr(mod, "_adapter", None)
        monkeypatch.setattr(mod, "_PRINTER_HOST", "http://localhost")
        monkeypatch.setattr(mod, "_PRINTER_API_KEY", "key123")
        monkeypatch.setattr(mod, "_PRINTER_TYPE", "unknown_brand")

        with pytest.raises(RuntimeError, match="Unsupported printer type"):
            mod._get_adapter()

    def test_octoprint_type_creates_adapter(self, monkeypatch):
        """Valid config with type='octoprint' creates an OctoPrintAdapter."""
        import kiln.server as mod

        monkeypatch.setattr(mod, "_adapter", None)
        monkeypatch.setattr(mod, "_PRINTER_HOST", "http://localhost:5000")
        monkeypatch.setattr(mod, "_PRINTER_API_KEY", "key123")
        monkeypatch.setattr(mod, "_PRINTER_TYPE", "octoprint")

        adapter = mod._get_adapter()
        assert isinstance(adapter, OctoPrintAdapter)

        # Clean up the global singleton
        monkeypatch.setattr(mod, "_adapter", None)

    def test_moonraker_type_creates_adapter(self, monkeypatch):
        """Valid config with type='moonraker' creates a MoonrakerAdapter."""
        import kiln.server as mod

        monkeypatch.setattr(mod, "_adapter", None)
        monkeypatch.setattr(mod, "_PRINTER_HOST", "http://localhost:7125")
        monkeypatch.setattr(mod, "_PRINTER_API_KEY", "")
        monkeypatch.setattr(mod, "_PRINTER_TYPE", "moonraker")

        adapter = mod._get_adapter()
        assert isinstance(adapter, MoonrakerAdapter)

        # Clean up the global singleton
        monkeypatch.setattr(mod, "_adapter", None)

    def test_bambu_type_creates_adapter(self, monkeypatch):
        """Valid config with type='bambu' creates a BambuAdapter."""
        import kiln.server as mod

        monkeypatch.setattr(mod, "_adapter", None)
        monkeypatch.setattr(mod, "_PRINTER_HOST", "192.168.1.100")
        monkeypatch.setattr(mod, "_PRINTER_API_KEY", "12345678")
        monkeypatch.setattr(mod, "_PRINTER_TYPE", "bambu")
        monkeypatch.setattr(mod, "_PRINTER_SERIAL", "01P00A000000001")

        adapter = mod._get_adapter()
        assert isinstance(adapter, BambuAdapter)

        # Clean up the global singleton
        monkeypatch.setattr(mod, "_adapter", None)

    def test_bambu_missing_api_key(self, monkeypatch):
        """Bambu requires access code (via api_key env var)."""
        import kiln.server as mod

        monkeypatch.setattr(mod, "_adapter", None)
        monkeypatch.setattr(mod, "_PRINTER_HOST", "192.168.1.100")
        monkeypatch.setattr(mod, "_PRINTER_API_KEY", "")
        monkeypatch.setattr(mod, "_PRINTER_TYPE", "bambu")
        monkeypatch.setattr(mod, "_PRINTER_SERIAL", "01P00A000000001")

        with pytest.raises(RuntimeError, match="KILN_PRINTER_API_KEY"):
            mod._get_adapter()

    def test_bambu_missing_serial(self, monkeypatch):
        """Bambu requires serial number."""
        import kiln.server as mod

        monkeypatch.setattr(mod, "_adapter", None)
        monkeypatch.setattr(mod, "_PRINTER_HOST", "192.168.1.100")
        monkeypatch.setattr(mod, "_PRINTER_API_KEY", "12345678")
        monkeypatch.setattr(mod, "_PRINTER_TYPE", "bambu")
        monkeypatch.setattr(mod, "_PRINTER_SERIAL", "")

        with pytest.raises(RuntimeError, match="KILN_PRINTER_SERIAL"):
            mod._get_adapter()

    def test_returns_cached_adapter(self, monkeypatch):
        """Second call returns the same cached adapter."""
        import kiln.server as mod

        mock_adapter = MagicMock(spec=OctoPrintAdapter)
        monkeypatch.setattr(mod, "_adapter", mock_adapter)

        result = mod._get_adapter()
        assert result is mock_adapter


# ---------------------------------------------------------------------------
# _validate_local_file() tests
# ---------------------------------------------------------------------------

class TestValidateLocalFile:
    """Tests for the _validate_local_file helper function."""

    def test_valid_gcode_file(self):
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\nG1 X10 Y10 Z5 F1500\n")
            tmp_path = f.name

        try:
            result = _validate_local_file(tmp_path)
            assert result["valid"] is True
            assert len(result["errors"]) == 0
            assert result["info"]["extension"] == ".gcode"
            assert result["info"]["size_bytes"] > 0
        finally:
            os.unlink(tmp_path)

    def test_valid_gco_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".gco", delete=False) as f:
            f.write(b"G28\n")
            tmp_path = f.name

        try:
            result = _validate_local_file(tmp_path)
            assert result["valid"] is True
        finally:
            os.unlink(tmp_path)

    def test_valid_g_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".g", delete=False) as f:
            f.write(b"G28\n")
            tmp_path = f.name

        try:
            result = _validate_local_file(tmp_path)
            assert result["valid"] is True
        finally:
            os.unlink(tmp_path)

    def test_missing_file(self):
        result = _validate_local_file("/nonexistent/path/missing.gcode")
        assert result["valid"] is False
        assert any("not found" in e.lower() for e in result["errors"])

    def test_bad_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
            f.write(b"solid model\n")
            tmp_path = f.name

        try:
            result = _validate_local_file(tmp_path)
            assert result["valid"] is False
            assert any("extension" in e.lower() for e in result["errors"])
            assert result["info"]["extension"] == ".stl"
        finally:
            os.unlink(tmp_path)

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            tmp_path = f.name
            # Write nothing -- 0 bytes

        try:
            result = _validate_local_file(tmp_path)
            assert result["valid"] is False
            assert any("empty" in e.lower() for e in result["errors"])
        finally:
            os.unlink(tmp_path)

    def test_directory_path(self):
        with tempfile.TemporaryDirectory() as d:
            result = _validate_local_file(d)
            assert result["valid"] is False
            assert any("not a regular file" in e.lower() for e in result["errors"])

    def test_info_contains_size_and_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\nG1 X50 Y50\n")
            tmp_path = f.name

        try:
            result = _validate_local_file(tmp_path)
            assert "info" in result
            assert result["info"]["size_bytes"] > 0
            assert result["info"]["extension"] == ".gcode"
        finally:
            os.unlink(tmp_path)

    def test_warnings_and_errors_are_lists(self):
        result = _validate_local_file("/no/such/file.gcode")
        assert isinstance(result["errors"], list)
        assert isinstance(result["warnings"], list)

    def test_uppercase_extension_accepted(self):
        """Extensions are lowercased during validation; .GCODE should still work."""
        with tempfile.NamedTemporaryFile(suffix=".GCODE", delete=False) as f:
            f.write(b"G28\n")
            tmp_path = f.name

        try:
            result = _validate_local_file(tmp_path)
            # .GCODE -> .gcode after lower(), which is valid
            assert result["valid"] is True
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# New tool imports
# ---------------------------------------------------------------------------

from kiln.events import EventBus, EventType
from kiln.plugins.queue_tools import (
    cancel_job as server_cancel_job,
)
from kiln.plugins.queue_tools import (
    job_status as server_job_status,
)
from kiln.plugins.queue_tools import (
    queue_summary,
)
from kiln.plugins.queue_tools import (
    submit_job as server_submit_job,
)
from kiln.queue import JobStatus, PrintQueue
from kiln.registry import PrinterRegistry
from kiln.server import (
    browse_models,
    download_model,
    fleet_status,
    list_model_categories,
    model_details,
    model_files,
    recent_events,
    register_printer,
    search_models,
)
from kiln.server import (
    validate_gcode as server_validate_gcode,
)
from kiln.thingiverse import (
    Category as TvCategory,
)
from kiln.thingiverse import (
    ThingDetail,
    ThingiverseClient,
    ThingiverseError,
    ThingiverseNotFoundError,
    ThingSummary,
)
from kiln.thingiverse import (
    ThingFile as TvFile,
)

# ---------------------------------------------------------------------------
# validate_gcode()
# ---------------------------------------------------------------------------

class TestValidateGcode:
    """Tests for the validate_gcode MCP tool."""

    def test_valid_commands(self):
        result = server_validate_gcode("G28\nG1 X10 Y10 Z5 F1200")
        assert result["success"] is True
        assert result["valid"] is True
        assert result["commands"] == ["G28", "G1 X10 Y10 Z5 F1200"]
        assert result["errors"] == []
        assert result["blocked_commands"] == []

    def test_blocked_command_m112(self):
        result = server_validate_gcode("M112")
        assert result["success"] is True
        assert result["valid"] is False
        assert len(result["errors"]) > 0
        assert "M112" in result["blocked_commands"][0]

    def test_blocked_command_m502(self):
        result = server_validate_gcode("M502")
        assert result["success"] is True
        assert result["valid"] is False
        assert len(result["blocked_commands"]) == 1

    def test_blocked_high_temp(self):
        result = server_validate_gcode("M104 S999")
        assert result["success"] is True
        assert result["valid"] is False
        assert any("temperature" in e.lower() for e in result["errors"])
        assert "M104 S999" in result["blocked_commands"]

    def test_blocked_high_bed_temp(self):
        result = server_validate_gcode("M140 S200")
        assert result["success"] is True
        assert result["valid"] is False
        assert any("bed" in e.lower() for e in result["errors"])

    def test_empty_input(self):
        result = server_validate_gcode("")
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"

    def test_whitespace_only_input(self):
        result = server_validate_gcode("  \n  \n  ")
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"

    def test_warnings_included(self):
        """G28 should produce a warning but still be valid."""
        result = server_validate_gcode("G28")
        assert result["success"] is True
        assert result["valid"] is True
        assert len(result["warnings"]) > 0
        assert any("home" in w.lower() or "G28" in w for w in result["warnings"])

    def test_mixed_valid_and_blocked(self):
        """When one command is blocked, the whole set is invalid."""
        result = server_validate_gcode("G28\nM112\nG1 X10")
        assert result["success"] is True
        assert result["valid"] is False
        assert len(result["blocked_commands"]) == 1

    def test_safe_temperature(self):
        result = server_validate_gcode("M104 S200")
        assert result["success"] is True
        assert result["valid"] is True

    def test_comments_stripped(self):
        result = server_validate_gcode("G28 ; home all axes")
        assert result["success"] is True
        assert result["valid"] is True
        assert result["commands"] == ["G28"]


# ---------------------------------------------------------------------------
# send_gcode() with G-code safety validation
# ---------------------------------------------------------------------------

class TestSendGcodeWithValidation:
    """Tests for send_gcode with integrated G-code safety validation."""

    @patch("kiln.server._get_adapter")
    def test_blocked_command_returns_gcode_blocked(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        mock_get_adapter.return_value = adapter

        result = send_gcode("M112")
        assert result["success"] is False
        assert result["error"]["code"] == "GCODE_BLOCKED"
        assert "blocked_commands" in result
        assert len(result["blocked_commands"]) > 0
        # Verify adapter was NOT called
        adapter._post.assert_not_called()

    @patch("kiln.server._get_adapter")
    def test_blocked_high_hotend_temp(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        mock_get_adapter.return_value = adapter

        result = send_gcode("M104 S999")
        assert result["success"] is False
        assert result["error"]["code"] == "GCODE_BLOCKED"
        assert any("temperature" in e.lower() for e in result["errors"])
        adapter._post.assert_not_called()

    @patch("kiln.server._get_adapter")
    def test_blocked_eeprom_save(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        mock_get_adapter.return_value = adapter

        result = send_gcode("M500")
        assert result["success"] is False
        assert result["error"]["code"] == "GCODE_BLOCKED"
        adapter._post.assert_not_called()

    @patch("kiln.server._get_adapter")
    def test_warnings_included_in_success(self, mock_get_adapter):
        """Commands that trigger warnings (e.g. G28) should succeed but include warnings."""
        adapter = MagicMock(spec=OctoPrintAdapter)
        mock_get_adapter.return_value = adapter

        result = send_gcode("G28")
        assert result["success"] is True
        assert "warnings" in result
        assert len(result["warnings"]) > 0

    @patch("kiln.server._get_adapter")
    def test_no_warnings_field_when_clean(self, mock_get_adapter):
        """Safe command with no warnings should not have a warnings key."""
        adapter = MagicMock(spec=OctoPrintAdapter)
        mock_get_adapter.return_value = adapter

        result = send_gcode("G1 X10 Y10 Z5 F1200")
        assert result["success"] is True
        # When there are no warnings the key should be absent
        assert "warnings" not in result

    @patch("kiln.server._get_adapter")
    def test_partial_block_blocks_all(self, mock_get_adapter):
        """If any command in a batch is blocked, nothing is sent."""
        adapter = MagicMock(spec=OctoPrintAdapter)
        mock_get_adapter.return_value = adapter

        result = send_gcode("G28\nM112\nG1 X10")
        assert result["success"] is False
        assert result["error"]["code"] == "GCODE_BLOCKED"
        adapter._post.assert_not_called()

    @patch("kiln.server._get_adapter")
    def test_firmware_settings_blocked(self, mock_get_adapter):
        """M500, M501, M502 should all be blocked."""
        adapter = MagicMock(spec=OctoPrintAdapter)
        mock_get_adapter.return_value = adapter

        for cmd in ["M500", "M501", "M502"]:
            result = send_gcode(cmd)
            assert result["success"] is False, f"{cmd} should be blocked"
            assert result["error"]["code"] == "GCODE_BLOCKED"


# ---------------------------------------------------------------------------
# fleet_status()
# ---------------------------------------------------------------------------

class TestFleetStatus:
    """Tests for the fleet_status MCP tool."""

    @pytest.fixture(autouse=True)
    def _bypass_license(self, monkeypatch):
        """Bypass PRO tier check so fleet tests can exercise the tool logic."""
        monkeypatch.setattr("kiln.licensing.check_tier", lambda _tier: (True, None))

    def test_empty_registry_no_env(self, monkeypatch):
        """Empty registry with no env adapter returns empty list."""
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_registry", fresh_registry)
        monkeypatch.setattr(mod, "_adapter", None)
        monkeypatch.setattr(mod, "_PRINTER_HOST", "")
        monkeypatch.setattr(mod, "_PRINTER_TYPE", "octoprint")

        result = fleet_status()
        assert result["success"] is True
        assert result["printers"] == []
        assert result["count"] == 0

    def test_auto_register_from_env(self, monkeypatch):
        """When the registry is empty but env is configured, default printer is auto-registered."""
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        mock_adapter = MagicMock(spec=OctoPrintAdapter)
        mock_adapter.name = "OctoPrint"
        mock_adapter.get_state.return_value = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            tool_temp_actual=24.0,
            tool_temp_target=0.0,
            bed_temp_actual=22.0,
            bed_temp_target=0.0,
        )

        monkeypatch.setattr(mod, "_registry", fresh_registry)
        monkeypatch.setattr(mod, "_adapter", mock_adapter)

        result = fleet_status()
        assert result["success"] is True
        assert result["count"] == 1
        assert result["printers"][0]["name"] == "default"
        assert "idle_printers" in result

    def test_with_pre_registered_printers(self, monkeypatch):
        """Registry with existing printers returns their status."""
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        adapter1 = MagicMock(spec=OctoPrintAdapter)
        adapter1.name = "OctoPrint"
        adapter1.get_state.return_value = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            tool_temp_actual=24.0,
            tool_temp_target=0.0,
            bed_temp_actual=22.0,
            bed_temp_target=0.0,
        )

        adapter2 = MagicMock(spec=MoonrakerAdapter)
        adapter2.name = "Moonraker"
        adapter2.get_state.return_value = PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            tool_temp_actual=210.0,
            tool_temp_target=210.0,
            bed_temp_actual=60.0,
            bed_temp_target=60.0,
        )

        fresh_registry.register("printer-1", adapter1)
        fresh_registry.register("printer-2", adapter2)
        monkeypatch.setattr(mod, "_registry", fresh_registry)

        result = fleet_status()
        assert result["success"] is True
        assert result["count"] == 2
        names = [p["name"] for p in result["printers"]]
        assert "printer-1" in names
        assert "printer-2" in names
        assert result["connected_count"] == 2
        assert result["disconnected_count"] == 0
        assert result["state_counts"]["idle"] == 1
        assert result["state_counts"]["printing"] == 1
        assert "printer-2" in result["busy_printers"]


# ---------------------------------------------------------------------------
# register_printer()
# ---------------------------------------------------------------------------

class TestRegisterPrinter:
    """Tests for the register_printer MCP tool."""

    def test_octoprint_success(self, monkeypatch):
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_registry", fresh_registry)

        result = register_printer(
            name="my-octoprint",
            printer_type="octoprint",
            host="http://octo.local",
            api_key="KEY123",
        )
        assert result["success"] is True
        assert result["name"] == "my-octoprint"
        assert "my-octoprint" in fresh_registry

    def test_moonraker_success(self, monkeypatch):
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_registry", fresh_registry)

        result = register_printer(
            name="my-moonraker",
            printer_type="moonraker",
            host="http://moonraker.local",
        )
        assert result["success"] is True
        assert result["name"] == "my-moonraker"
        assert "my-moonraker" in fresh_registry

    def test_moonraker_no_api_key(self, monkeypatch):
        """Moonraker does not require an api_key."""
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_registry", fresh_registry)

        result = register_printer(
            name="klipper-box",
            printer_type="moonraker",
            host="http://klipper.local",
            api_key=None,
        )
        assert result["success"] is True

    def test_bambu_success(self, monkeypatch):
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_registry", fresh_registry)

        result = register_printer(
            name="my-bambu",
            printer_type="bambu",
            host="192.168.1.100",
            api_key="12345678",
            serial="01P00A000000001",
        )
        assert result["success"] is True
        assert result["name"] == "my-bambu"
        assert "my-bambu" in fresh_registry

    def test_bambu_missing_api_key(self, monkeypatch):
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_registry", fresh_registry)

        result = register_printer(
            name="bambu-nokey",
            printer_type="bambu",
            host="192.168.1.100",
            serial="01P00A000000001",
        )
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"

    def test_bambu_missing_serial(self, monkeypatch):
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_registry", fresh_registry)

        result = register_printer(
            name="bambu-noserial",
            printer_type="bambu",
            host="192.168.1.100",
            api_key="12345678",
        )
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"

    def test_bambu_verify_ssl_flag_maps_to_tls_mode(self, monkeypatch):
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_registry", fresh_registry)
        calls = []

        class _DummyBambu:
            name = "bambu"

        def _fake_bambu_ctor(*, host, access_code, serial, tls_mode="pin"):
            calls.append(
                {
                    "host": host,
                    "access_code": access_code,
                    "serial": serial,
                    "tls_mode": tls_mode,
                }
            )
            return _DummyBambu()

        monkeypatch.setattr(mod, "BambuAdapter", _fake_bambu_ctor)

        result_pin = register_printer(
            name="bambu-pin",
            printer_type="bambu",
            host="192.168.1.100",
            api_key="12345678",
            serial="01P00A000000001",
            verify_ssl=True,
        )
        result_insecure = register_printer(
            name="bambu-insecure",
            printer_type="bambu",
            host="192.168.1.100",
            api_key="12345678",
            serial="01P00A000000001",
            verify_ssl=False,
        )

        assert result_pin["success"] is True
        assert result_insecure["success"] is True
        assert calls[0]["tls_mode"] == "pin"
        assert calls[1]["tls_mode"] == "insecure"

    def test_unsupported_type(self, monkeypatch):
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_registry", fresh_registry)

        result = register_printer(
            name="unknown",
            printer_type="prusa_connect",
            host="http://prusa.local",
        )
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"

    def test_octoprint_missing_api_key(self, monkeypatch):
        """OctoPrint requires an api_key."""
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_registry", fresh_registry)

        result = register_printer(
            name="no-key",
            printer_type="octoprint",
            host="http://octo.local",
            api_key=None,
        )
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"
        assert "api_key" in result["error"]["message"].lower()

    def test_octoprint_empty_api_key(self, monkeypatch):
        """Empty string api_key is treated as missing for OctoPrint."""
        import kiln.server as mod

        fresh_registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_registry", fresh_registry)

        result = register_printer(
            name="empty-key",
            printer_type="octoprint",
            host="http://octo.local",
            api_key="",
        )
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"


# ---------------------------------------------------------------------------
# submit_job()
# ---------------------------------------------------------------------------

class TestSubmitJob:
    """Tests for the submit_job MCP tool."""

    def test_basic_submission(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_queue", fresh_queue)
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        result = server_submit_job("benchy.gcode", printer_name="voron-350", priority=5)
        assert result["success"] is True
        assert "job_id" in result
        assert result["message"].startswith("Job")

    def test_job_appears_in_queue(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_queue", fresh_queue)
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        result = server_submit_job("cube.gcode")
        assert result["success"] is True
        job_id = result["job_id"]

        job = fresh_queue.get_job(job_id)
        assert job.file_name == "cube.gcode"
        assert job.status == JobStatus.QUEUED
        assert job.submitted_by == "mcp-agent"

    def test_submission_publishes_event(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_queue", fresh_queue)
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        server_submit_job("test.gcode")
        events = fresh_bus.recent_events()
        assert len(events) == 1
        assert events[0].type == EventType.JOB_QUEUED

    def test_priority_ordering(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_queue", fresh_queue)
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        server_submit_job("low.gcode", priority=0)
        server_submit_job("high.gcode", priority=10)

        next_job = fresh_queue.next_job()
        assert next_job is not None
        assert next_job.file_name == "high.gcode"

    def test_submit_with_no_printer_name(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_queue", fresh_queue)
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        result = server_submit_job("any_printer.gcode")
        assert result["success"] is True
        job_id = result["job_id"]
        job = fresh_queue.get_job(job_id)
        assert job.printer_name is None


# ---------------------------------------------------------------------------
# job_status()
# ---------------------------------------------------------------------------

class TestJobStatus:
    """Tests for the job_status MCP tool."""

    def test_found_job(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        monkeypatch.setattr(mod, "_queue", fresh_queue)

        job_id = fresh_queue.submit(
            file_name="benchy.gcode",
            printer_name="voron",
            submitted_by="test",
        )
        result = server_job_status(job_id)
        assert result["success"] is True
        assert result["job"]["id"] == job_id
        assert result["job"]["file_name"] == "benchy.gcode"
        assert result["job"]["status"] == "queued"

    def test_not_found(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        monkeypatch.setattr(mod, "_queue", fresh_queue)

        result = server_job_status("nonexistent-id")
        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_completed_job(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        monkeypatch.setattr(mod, "_queue", fresh_queue)

        job_id = fresh_queue.submit(file_name="done.gcode", submitted_by="test")
        fresh_queue.mark_starting(job_id)
        fresh_queue.mark_printing(job_id)
        fresh_queue.mark_completed(job_id)

        result = server_job_status(job_id)
        assert result["success"] is True
        assert result["job"]["status"] == "completed"


# ---------------------------------------------------------------------------
# queue_summary()
# ---------------------------------------------------------------------------

class TestQueueSummary:
    """Tests for the queue_summary MCP tool."""

    def test_empty_queue(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        monkeypatch.setattr(mod, "_queue", fresh_queue)

        result = queue_summary()
        assert result["success"] is True
        assert result["total"] == 0
        assert result["pending"] == 0
        assert result["active"] == 0
        assert result["next_job"] is None
        assert result["recent_jobs"] == []
        assert "registered_printers" in result
        assert result["dispatch_blocked"] is False

    def test_with_queued_jobs(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        monkeypatch.setattr(mod, "_queue", fresh_queue)

        fresh_queue.submit(file_name="a.gcode", submitted_by="test", priority=0)
        fresh_queue.submit(file_name="b.gcode", submitted_by="test", priority=5)

        result = queue_summary()
        assert result["success"] is True
        assert result["total"] == 2
        assert result["pending"] == 2
        assert result["active"] == 0
        assert result["next_job"] is not None
        assert result["next_job"]["file_name"] == "b.gcode"
        assert len(result["recent_jobs"]) == 2
        assert result["dispatch_blocked"] in (True, False)

    def test_with_mixed_statuses(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        monkeypatch.setattr(mod, "_queue", fresh_queue)

        id1 = fresh_queue.submit(file_name="printing.gcode", submitted_by="test")
        fresh_queue.mark_starting(id1)
        fresh_queue.mark_printing(id1)
        fresh_queue.submit(file_name="queued.gcode", submitted_by="test")

        result = queue_summary()
        assert result["success"] is True
        assert result["total"] == 2
        assert result["pending"] == 1
        assert result["active"] == 1
        assert "queued" in result["counts"]
        assert "printing" in result["counts"]

    def test_dispatch_blocked_when_jobs_queued_and_no_printers(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        fresh_registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_queue", fresh_queue)
        monkeypatch.setattr(mod, "_registry", fresh_registry)

        fresh_queue.submit(file_name="queued.gcode", submitted_by="test")
        result = queue_summary()
        assert result["success"] is True
        assert result["dispatch_blocked"] is True
        assert "no printers are registered" in (result["dispatch_block_reason"] or "").lower()


# ---------------------------------------------------------------------------
# cancel_job()
# ---------------------------------------------------------------------------

class TestCancelJob:
    """Tests for the cancel_job MCP tool."""

    def test_cancel_queued_job(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_queue", fresh_queue)
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        job_id = fresh_queue.submit(file_name="cancel_me.gcode", submitted_by="test")
        result = server_cancel_job(job_id)
        assert result["success"] is True
        assert result["job"]["status"] == "cancelled"

        # Verify event was published
        events = fresh_bus.recent_events()
        assert any(e.type == EventType.JOB_CANCELLED for e in events)

    def test_cancel_printing_job(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_queue", fresh_queue)
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        job_id = fresh_queue.submit(file_name="active.gcode", submitted_by="test")
        fresh_queue.mark_starting(job_id)
        fresh_queue.mark_printing(job_id)

        result = server_cancel_job(job_id)
        assert result["success"] is True
        assert result["job"]["status"] == "cancelled"

    def test_cancel_not_found(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_queue", fresh_queue)
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        result = server_cancel_job("nonexistent-id")
        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_cancel_already_completed(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_queue", fresh_queue)
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        job_id = fresh_queue.submit(file_name="done.gcode", submitted_by="test")
        fresh_queue.mark_starting(job_id)
        fresh_queue.mark_printing(job_id)
        fresh_queue.mark_completed(job_id)

        result = server_cancel_job(job_id)
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_STATE"

    def test_cancel_already_cancelled(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_queue", fresh_queue)
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        job_id = fresh_queue.submit(file_name="twice.gcode", submitted_by="test")
        fresh_queue.cancel(job_id)

        result = server_cancel_job(job_id)
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_STATE"


# ---------------------------------------------------------------------------
# recent_events()
# ---------------------------------------------------------------------------

class TestRecentEvents:
    """Tests for the recent_events MCP tool."""

    def test_empty_bus(self, monkeypatch):
        import kiln.server as mod

        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        result = recent_events()
        assert result["success"] is True
        assert result["events"] == []
        assert result["count"] == 0

    def test_after_publishing_events(self, monkeypatch):
        import kiln.server as mod
        from kiln.events import Event

        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        fresh_bus.publish(Event(
            type=EventType.PRINT_STARTED,
            data={"file": "benchy.gcode"},
            source="test",
        ))
        fresh_bus.publish(Event(
            type=EventType.PRINT_COMPLETED,
            data={"file": "benchy.gcode"},
            source="test",
        ))

        result = recent_events()
        assert result["success"] is True
        assert result["count"] == 2
        # Events are returned newest first
        assert result["events"][0]["type"] == "print.completed"
        assert result["events"][1]["type"] == "print.started"

    def test_limit_parameter(self, monkeypatch):
        import kiln.server as mod
        from kiln.events import Event

        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        for i in range(10):
            fresh_bus.publish(Event(
                type=EventType.PRINT_PROGRESS,
                data={"progress": i * 10},
                source="test",
            ))

        result = recent_events(limit=3)
        assert result["success"] is True
        assert result["count"] == 3

    def test_limit_capped_at_100(self, monkeypatch):
        """Limit is capped at 100 even if larger value is requested."""
        import kiln.server as mod

        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        # Just verify the function doesn't error with a high limit
        result = recent_events(limit=200)
        assert result["success"] is True
        # With empty bus the count should be 0
        assert result["count"] == 0

    def test_limit_capped_at_1_minimum(self, monkeypatch):
        """Limit is at least 1 even if 0 or negative is requested."""
        import kiln.server as mod
        from kiln.events import Event

        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        fresh_bus.publish(Event(
            type=EventType.PRINT_STARTED,
            data={},
            source="test",
        ))

        result = recent_events(limit=0)
        assert result["success"] is True
        assert result["count"] == 1

    def test_event_data_structure(self, monkeypatch):
        """Each event in the response has the expected keys."""
        import kiln.server as mod
        from kiln.events import Event

        fresh_bus = EventBus()
        monkeypatch.setattr(mod, "_event_bus", fresh_bus)

        fresh_bus.publish(Event(
            type=EventType.JOB_CANCELLED,
            data={"job_id": "abc123"},
            source="mcp",
        ))

        result = recent_events()
        assert result["count"] == 1
        event = result["events"][0]
        assert "type" in event
        assert "data" in event
        assert "timestamp" in event
        assert "source" in event
        assert event["data"]["job_id"] == "abc123"


# ---------------------------------------------------------------------------
# _get_thingiverse() tests
# ---------------------------------------------------------------------------

class TestGetThingiverse:
    """Tests for the _get_thingiverse lazy-initialisation function."""

    def test_missing_token(self, monkeypatch):
        import kiln.server as mod

        monkeypatch.setattr(mod, "_thingiverse", None)
        monkeypatch.setattr(mod, "_THINGIVERSE_TOKEN", "")

        with pytest.raises(RuntimeError, match="KILN_THINGIVERSE_TOKEN"):
            mod._get_thingiverse()

    def test_creates_client_with_token(self, monkeypatch):
        import kiln.server as mod

        monkeypatch.setattr(mod, "_thingiverse", None)
        monkeypatch.setattr(mod, "_THINGIVERSE_TOKEN", "test-token-xyz")

        client = mod._get_thingiverse()
        assert isinstance(client, ThingiverseClient)
        assert client._token == "test-token-xyz"
        # Clean up
        monkeypatch.setattr(mod, "_thingiverse", None)

    def test_returns_cached_client(self, monkeypatch):
        import kiln.server as mod

        mock_client = MagicMock(spec=ThingiverseClient)
        monkeypatch.setattr(mod, "_thingiverse", mock_client)

        result = mod._get_thingiverse()
        assert result is mock_client


# ---------------------------------------------------------------------------
# search_models()
# ---------------------------------------------------------------------------

class TestSearchModels:
    """Tests for the search_models MCP tool."""

    @patch("kiln.server._get_thingiverse")
    def test_success(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.search.return_value = [
            ThingSummary(id=1, name="Benchy", url="/t/1", creator="u"),
            ThingSummary(id=2, name="Cube", url="/t/2", creator="v"),
        ]
        mock_get_tv.return_value = client

        result = search_models("benchy")
        assert result["success"] is True
        assert result["count"] == 2
        assert result["query"] == "benchy"
        assert result["models"][0]["name"] == "Benchy"
        client.search.assert_called_once_with(
            "benchy", page=1, per_page=10, sort="relevant",
        )

    @patch("kiln.server._get_thingiverse")
    def test_empty_results(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.search.return_value = []
        mock_get_tv.return_value = client

        result = search_models("noresults")
        assert result["success"] is True
        assert result["count"] == 0

    @patch("kiln.server._get_thingiverse")
    def test_api_error(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.search.side_effect = ThingiverseError("rate limit")
        mock_get_tv.return_value = client

        result = search_models("test")
        assert result["success"] is False

    def test_missing_token(self, monkeypatch):
        import kiln.server as mod
        monkeypatch.setattr(mod, "_thingiverse", None)
        monkeypatch.setattr(mod, "_THINGIVERSE_TOKEN", "")

        result = search_models("test")
        assert result["success"] is False
        assert "KILN_THINGIVERSE_TOKEN" in result["error"]["message"]


# ---------------------------------------------------------------------------
# model_details()
# ---------------------------------------------------------------------------

class TestModelDetails:
    """Tests for the model_details MCP tool."""

    @patch("kiln.server._get_thingiverse")
    def test_success(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.get_thing.return_value = ThingDetail(
            id=123, name="Benchy", url="/t/123", creator="user",
            description="A boat", tags=["benchy"],
        )
        mock_get_tv.return_value = client

        result = model_details(123)
        assert result["success"] is True
        assert result["model"]["name"] == "Benchy"
        assert result["model"]["description"] == "A boat"

    @patch("kiln.server._get_thingiverse")
    def test_not_found(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.get_thing.side_effect = ThingiverseNotFoundError("not found")
        mock_get_tv.return_value = client

        result = model_details(999)
        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# model_files()
# ---------------------------------------------------------------------------

class TestModelFiles:
    """Tests for the model_files MCP tool."""

    @patch("kiln.server._get_thingiverse")
    def test_success(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.get_files.return_value = [
            TvFile(id=10, name="model.stl", size_bytes=5000, download_url="/dl"),
        ]
        mock_get_tv.return_value = client

        result = model_files(123)
        assert result["success"] is True
        assert result["count"] == 1
        assert result["files"][0]["name"] == "model.stl"
        assert result["thing_id"] == 123

    @patch("kiln.server._get_thingiverse")
    def test_not_found(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.get_files.side_effect = ThingiverseNotFoundError("not found")
        mock_get_tv.return_value = client

        result = model_files(999)
        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# download_model()
# ---------------------------------------------------------------------------

class TestDownloadModel:
    """Tests for the download_model MCP tool."""

    @patch("kiln.server._get_thingiverse")
    def test_success(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.download_file.return_value = "/tmp/kiln_downloads/model.stl"
        mock_get_tv.return_value = client

        result = download_model(10)
        assert result["success"] is True
        assert result["local_path"] == "/tmp/kiln_downloads/model.stl"
        assert result["file_id"] == 10

    @patch("kiln.server._get_thingiverse")
    def test_custom_dest_and_name(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.download_file.return_value = "/custom/dir/custom.stl"
        mock_get_tv.return_value = client

        result = download_model(10, dest_dir="/custom/dir", file_name="custom.stl")
        assert result["success"] is True
        client.download_file.assert_called_once_with(
            10, "/custom/dir", file_name="custom.stl",
        )

    @patch("kiln.server._get_thingiverse")
    def test_not_found(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.download_file.side_effect = ThingiverseNotFoundError("not found")
        mock_get_tv.return_value = client

        result = download_model(999)
        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# browse_models()
# ---------------------------------------------------------------------------

class TestBrowseModels:
    """Tests for the browse_models MCP tool."""

    @patch("kiln.server._get_thingiverse")
    def test_popular(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.popular.return_value = [
            ThingSummary(id=1, name="Hot", url="/t/1", creator="u"),
        ]
        mock_get_tv.return_value = client

        result = browse_models("popular")
        assert result["success"] is True
        assert result["browse_type"] == "popular"
        assert result["count"] == 1

    @patch("kiln.server._get_thingiverse")
    def test_newest(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.newest.return_value = []
        mock_get_tv.return_value = client

        result = browse_models("newest")
        assert result["success"] is True
        assert result["browse_type"] == "newest"

    @patch("kiln.server._get_thingiverse")
    def test_featured(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.featured.return_value = []
        mock_get_tv.return_value = client

        result = browse_models("featured")
        assert result["success"] is True
        assert result["browse_type"] == "featured"

    @patch("kiln.server._get_thingiverse")
    def test_by_category(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.category_things.return_value = [
            ThingSummary(id=5, name="Art", url="/t/5", creator="u"),
        ]
        mock_get_tv.return_value = client

        result = browse_models(category="art")
        assert result["success"] is True
        assert result["browse_type"] == "category:art"
        client.category_things.assert_called_once_with(
            "art", page=1, per_page=10,
        )

    def test_invalid_browse_type(self, monkeypatch):
        import kiln.server as mod
        mock_client = MagicMock(spec=ThingiverseClient)
        monkeypatch.setattr(mod, "_thingiverse", mock_client)

        result = browse_models("invalid")
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_ARGS"


# ---------------------------------------------------------------------------
# list_model_categories()
# ---------------------------------------------------------------------------

class TestListModelCategories:
    """Tests for the list_model_categories MCP tool."""

    @patch("kiln.server._get_thingiverse")
    def test_success(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.list_categories.return_value = [
            TvCategory(name="Art", slug="art", url="/cat/art", count=100),
            TvCategory(name="Tools", slug="tools", url="/cat/tools", count=50),
        ]
        mock_get_tv.return_value = client

        result = list_model_categories()
        assert result["success"] is True
        assert result["count"] == 2
        assert result["categories"][0]["name"] == "Art"

    @patch("kiln.server._get_thingiverse")
    def test_api_error(self, mock_get_tv):
        client = MagicMock(spec=ThingiverseClient)
        client.list_categories.side_effect = ThingiverseError("timeout")
        mock_get_tv.return_value = client

        result = list_model_categories()
        assert result["success"] is False


# ---------------------------------------------------------------------------
# get_speed_profile()
# ---------------------------------------------------------------------------


class TestGetSpeedProfile:
    """Tests for the get_speed_profile MCP tool (Bambu-only)."""

    @patch("kiln.server._get_adapter")
    def test_returns_current_profile(self, mock_get_adapter):
        adapter = MagicMock(spec=BambuAdapter)
        adapter.get_speed_profile.return_value = {
            "level": 2,
            "name": "standard",
            "speed_magnitude": 100,
        }
        mock_get_adapter.return_value = adapter

        result = get_speed_profile()
        assert result["status"] == "success"
        assert result["level"] == 2
        assert result["name"] == "standard"
        assert result["speed_magnitude"] == 100

    @patch("kiln.server._get_adapter")
    def test_unsupported_printer_returns_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        # OctoPrintAdapter does not have get_speed_profile
        del adapter.get_speed_profile
        mock_get_adapter.return_value = adapter

        result = get_speed_profile()
        assert result["success"] is False
        assert result["error"]["code"] == "UNSUPPORTED"
        assert "Bambu" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter):
        adapter = MagicMock(spec=BambuAdapter)
        adapter.get_speed_profile.side_effect = PrinterError("MQTT timeout")
        mock_get_adapter.return_value = adapter

        result = get_speed_profile()
        assert result["success"] is False
        assert "MQTT timeout" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_runtime_error(self, mock_get_adapter):
        mock_get_adapter.side_effect = RuntimeError("not configured")

        result = get_speed_profile()
        assert result["success"] is False
        assert "not configured" in result["error"]["message"]


# ---------------------------------------------------------------------------
# get_bed_mesh()
# ---------------------------------------------------------------------------


class TestGetBedMesh:
    """Tests for the get_bed_mesh MCP tool (OctoPrint / Moonraker)."""

    @patch("kiln.server._get_adapter")
    def test_returns_mesh_data(self, mock_get_adapter):
        adapter = MagicMock(spec=MoonrakerAdapter)
        adapter.get_bed_mesh.return_value = {
            "probed_matrix": [[0.1, 0.2], [0.0, -0.1]],
            "mesh_min": [10.0, 10.0],
            "mesh_max": [200.0, 200.0],
            "variance": 0.05,
        }
        mock_get_adapter.return_value = adapter

        result = get_bed_mesh()
        assert result["status"] == "success"
        assert result["probed_matrix"] == [[0.1, 0.2], [0.0, -0.1]]
        assert result["variance"] == 0.05

    @patch("kiln.server._get_adapter")
    def test_unsupported_returns_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_bed_mesh.return_value = None
        mock_get_adapter.return_value = adapter

        result = get_bed_mesh()
        assert result["success"] is False
        assert result["error"]["code"] == "UNSUPPORTED"
        assert "G29" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter):
        adapter = MagicMock(spec=MoonrakerAdapter)
        adapter.get_bed_mesh.side_effect = PrinterError("connection refused")
        mock_get_adapter.return_value = adapter

        result = get_bed_mesh()
        assert result["success"] is False
        assert "connection refused" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_runtime_error(self, mock_get_adapter):
        mock_get_adapter.side_effect = RuntimeError("env not set")

        result = get_bed_mesh()
        assert result["success"] is False
        assert "env not set" in result["error"]["message"]


# ---------------------------------------------------------------------------
# get_filament_status()
# ---------------------------------------------------------------------------


class TestGetFilamentStatus:
    """Tests for the get_filament_status MCP tool (OctoPrint / Moonraker)."""

    @patch("kiln.server._get_adapter")
    def test_returns_filament_status(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_filament_status.return_value = {
            "detected": True,
            "sensor_enabled": True,
        }
        mock_get_adapter.return_value = adapter

        result = get_filament_status()
        assert result["status"] == "success"
        assert result["detected"] is True
        assert result["sensor_enabled"] is True

    @patch("kiln.server._get_adapter")
    def test_unsupported_suggests_ams_status(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_filament_status.return_value = None
        mock_get_adapter.return_value = adapter

        result = get_filament_status()
        assert result["success"] is False
        assert result["error"]["code"] == "UNSUPPORTED"
        assert "ams_status" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_filament_status.side_effect = PrinterError("sensor fault")
        mock_get_adapter.return_value = adapter

        result = get_filament_status()
        assert result["success"] is False
        assert "sensor fault" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_runtime_error(self, mock_get_adapter):
        mock_get_adapter.side_effect = RuntimeError("missing config")

        result = get_filament_status()
        assert result["success"] is False
        assert "missing config" in result["error"]["message"]


# ---------------------------------------------------------------------------
# get_tool_position()
# ---------------------------------------------------------------------------


class TestGetToolPosition:
    """Tests for the get_tool_position MCP tool (Moonraker / Serial)."""

    @patch("kiln.server._get_adapter")
    def test_returns_xyz_position(self, mock_get_adapter):
        adapter = MagicMock(spec=MoonrakerAdapter)
        adapter.get_tool_position.return_value = {
            "x": 120.5,
            "y": 80.3,
            "z": 0.2,
        }
        mock_get_adapter.return_value = adapter

        result = get_tool_position()
        assert result["status"] == "success"
        assert result["position"]["x"] == 120.5
        assert result["position"]["y"] == 80.3
        assert result["position"]["z"] == 0.2

    @patch("kiln.server._get_adapter")
    def test_unsupported_returns_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_tool_position.return_value = None
        mock_get_adapter.return_value = adapter

        result = get_tool_position()
        assert result["success"] is False
        assert result["error"]["code"] == "UNSUPPORTED"
        assert "printer_status" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_includes_extruder_position(self, mock_get_adapter):
        adapter = MagicMock(spec=MoonrakerAdapter)
        adapter.get_tool_position.return_value = {
            "x": 0.0,
            "y": 0.0,
            "z": 10.0,
            "e": 5.2,
        }
        mock_get_adapter.return_value = adapter

        result = get_tool_position()
        assert result["status"] == "success"
        assert result["position"]["e"] == 5.2

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter):
        adapter = MagicMock(spec=MoonrakerAdapter)
        adapter.get_tool_position.side_effect = PrinterError("homing required")
        mock_get_adapter.return_value = adapter

        result = get_tool_position()
        assert result["success"] is False
        assert "homing required" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_runtime_error(self, mock_get_adapter):
        mock_get_adapter.side_effect = RuntimeError("env not set")

        result = get_tool_position()
        assert result["success"] is False
        assert "env not set" in result["error"]["message"]


# ---------------------------------------------------------------------------
# wrap_gcode_as_3mf()
# ---------------------------------------------------------------------------


class TestWrapGcodeAs3mf:
    """Tests for the wrap_gcode_as_3mf MCP tool (Bambu-only)."""

    @patch("kiln.server._get_adapter")
    def test_wraps_gcode_successfully(self, mock_get_adapter):
        adapter = MagicMock(spec=BambuAdapter)
        adapter.wrap_gcode_as_3mf.return_value = "/tmp/output.3mf"
        mock_get_adapter.return_value = adapter

        result = wrap_gcode_as_3mf(
            gcode_path="/tmp/test.gcode",
            hotend_temp=210,
            bed_temp=60,
            filament_type="PLA",
        )
        assert result["status"] == "success"
        assert result["output_path"] == "/tmp/output.3mf"
        assert result["gcode_path"] == "/tmp/test.gcode"
        assert result["filament_type"] == "PLA"

        adapter.wrap_gcode_as_3mf.assert_called_once_with(
            "/tmp/test.gcode",
            hotend_temp=210,
            bed_temp=60,
            filament_type="PLA",
            source_3mf_path=None,
        )

    @patch("kiln.server._get_adapter")
    def test_unsupported_printer_returns_error(self, mock_get_adapter):
        adapter = MagicMock(spec=OctoPrintAdapter)
        # OctoPrintAdapter does not have wrap_gcode_as_3mf
        del adapter.wrap_gcode_as_3mf
        mock_get_adapter.return_value = adapter

        result = wrap_gcode_as_3mf(gcode_path="/tmp/test.gcode")
        assert result["success"] is False
        assert result["error"]["code"] == "UNSUPPORTED"
        assert "upload_file" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_file_not_found(self, mock_get_adapter):
        adapter = MagicMock(spec=BambuAdapter)
        adapter.wrap_gcode_as_3mf.side_effect = FileNotFoundError(
            "/tmp/missing.gcode"
        )
        mock_get_adapter.return_value = adapter

        result = wrap_gcode_as_3mf(gcode_path="/tmp/missing.gcode")
        assert result["success"] is False
        assert "not found" in result["error"]["message"].lower()

    @patch("kiln.server._get_adapter")
    def test_invalid_gcode(self, mock_get_adapter):
        adapter = MagicMock(spec=BambuAdapter)
        adapter.wrap_gcode_as_3mf.side_effect = ValueError(
            "Missing relative E distances"
        )
        mock_get_adapter.return_value = adapter

        result = wrap_gcode_as_3mf(gcode_path="/tmp/bad.gcode")
        assert result["success"] is False
        assert "Invalid G-code" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter):
        adapter = MagicMock(spec=BambuAdapter)
        adapter.wrap_gcode_as_3mf.side_effect = PrinterError("3MF generation failed")
        mock_get_adapter.return_value = adapter

        result = wrap_gcode_as_3mf(gcode_path="/tmp/test.gcode")
        assert result["success"] is False
        assert "3MF generation failed" in result["error"]["message"]

    @patch("kiln.server._get_adapter")
    def test_with_source_3mf(self, mock_get_adapter):
        adapter = MagicMock(spec=BambuAdapter)
        adapter.wrap_gcode_as_3mf.return_value = "/tmp/output.3mf"
        mock_get_adapter.return_value = adapter

        result = wrap_gcode_as_3mf(
            gcode_path="/tmp/test.gcode",
            source_3mf_path="/tmp/source.3mf",
        )
        assert result["status"] == "success"
        adapter.wrap_gcode_as_3mf.assert_called_once_with(
            "/tmp/test.gcode",
            hotend_temp=220,
            bed_temp=65,
            filament_type="PLA",
            source_3mf_path="/tmp/source.3mf",
        )

    @patch("kiln.server._get_adapter")
    def test_runtime_error(self, mock_get_adapter):
        mock_get_adapter.side_effect = RuntimeError("not configured")

        result = wrap_gcode_as_3mf(gcode_path="/tmp/test.gcode")
        assert result["success"] is False
        assert "not configured" in result["error"]["message"]


# ---------------------------------------------------------------------------
# reslice_with_overrides
# ---------------------------------------------------------------------------


class TestResliceWithOverrides:
    """Tests for the reslice_with_overrides MCP tool.

    Covers: basic overrides, no overrides, invalid JSON, missing file,
    unsupported format, slicer not found, temperature safety validation.
    """

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server.resolve_slicer_profile")
    @patch("kiln.server._map_printer_hint_to_profile_id", return_value="prusa_mini")
    def test_reslice_basic_overrides(
        self, mock_map, mock_resolve, mock_auth, tmp_path
    ):
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")
        mock_resolve.return_value = "/tmp/merged_profile.ini"

        from kiln.slicer import SliceResult

        with patch("kiln.slicer.slice_file") as mock_slice:
            mock_slice.return_value = SliceResult(
                success=True,
                output_path=str(tmp_path / "model.gcode"),
                slicer="prusa-slicer",
                message="Sliced",
            )
            result = reslice_with_overrides(
                input_path=str(stl),
                printer_id="prusa_mini",
                overrides=json.dumps({"brim_width": "8", "fill_density": "25%"}),
            )

        assert result["success"] is True
        assert result["applied_overrides"] == {"brim_width": "8", "fill_density": "25%"}
        assert result["printer_id"] == "prusa_mini"
        mock_resolve.assert_called_once_with(
            "prusa_mini",
            overrides={"brim_width": "8", "fill_density": "25%"},
        )

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server.resolve_slicer_profile")
    @patch("kiln.server._map_printer_hint_to_profile_id", return_value="prusa_mini")
    def test_reslice_no_overrides(
        self, mock_map, mock_resolve, mock_auth, tmp_path
    ):
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")
        mock_resolve.return_value = "/tmp/profile.ini"

        from kiln.slicer import SliceResult

        with patch("kiln.slicer.slice_file") as mock_slice:
            mock_slice.return_value = SliceResult(
                success=True,
                output_path=str(tmp_path / "model.gcode"),
                slicer="prusa-slicer",
                message="Sliced",
            )
            result = reslice_with_overrides(
                input_path=str(stl),
                printer_id="prusa_mini",
            )

        assert result["success"] is True
        assert "applied_overrides" not in result
        # With no overrides, resolve_slicer_profile is called with overrides=None
        mock_resolve.assert_called_once_with("prusa_mini", overrides=None)

    @patch("kiln.server._check_auth", return_value=None)
    def test_reslice_invalid_json_overrides(self, mock_auth, tmp_path):
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")

        result = reslice_with_overrides(
            input_path=str(stl),
            overrides="not valid json{{{",
        )
        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "Invalid overrides JSON" in result["error"]["message"]

    @patch("kiln.server._check_auth", return_value=None)
    def test_reslice_invalid_input_path(self, mock_auth):
        result = reslice_with_overrides(
            input_path="/nonexistent/model.stl",
        )
        assert result["success"] is False
        assert result["error"]["code"] == "FILE_NOT_FOUND"

    @patch("kiln.server._check_auth", return_value=None)
    def test_reslice_unsupported_format(self, mock_auth, tmp_path):
        txt = tmp_path / "readme.txt"
        txt.write_text("not a model")

        result = reslice_with_overrides(input_path=str(txt))
        assert result["success"] is False
        assert result["error"]["code"] == "UNSUPPORTED_FORMAT"
        assert ".txt" in result["error"]["message"]

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._map_printer_hint_to_profile_id", return_value=None)
    def test_reslice_slicer_not_found(self, mock_map, mock_auth, tmp_path):
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")

        from kiln.slicer import SlicerNotFoundError

        with patch("kiln.slicer.slice_file") as mock_slice:
            mock_slice.side_effect = SlicerNotFoundError("No slicer found")
            result = reslice_with_overrides(input_path=str(stl))

        assert result["success"] is False
        assert result["error"]["code"] == "SLICER_NOT_FOUND"
        assert "PrusaSlicer or OrcaSlicer" in result["error"]["message"]

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server.validate_profile_for_printer")
    @patch("kiln.server.resolve_slicer_profile")
    @patch("kiln.server._map_printer_hint_to_profile_id", return_value="bambu_a1")
    @patch("kiln.server._PRINTER_MODEL", "bambu_a1")
    def test_reslice_with_temperature_validation(
        self, mock_map, mock_resolve, mock_validate, mock_auth, tmp_path
    ):
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")
        mock_resolve.return_value = "/tmp/merged.ini"
        mock_validate.return_value = {
            "compatible": False,
            "warnings": [],
            "errors": ["Profile hotend temp temperature=350°C exceeds max 300°C."],
        }

        from kiln.slicer import SliceResult

        with patch("kiln.slicer.slice_file") as mock_slice:
            mock_slice.return_value = SliceResult(
                success=True,
                output_path=str(tmp_path / "model.gcode"),
                slicer="prusa-slicer",
                message="Sliced",
            )
            result = reslice_with_overrides(
                input_path=str(stl),
                printer_id="bambu_a1",
                overrides=json.dumps({"temperature": "350"}),
            )

        assert result["success"] is True
        assert "profile_validation" in result
        assert "profile_validation_warning" in result
        assert "unsafe" in result["profile_validation_warning"].lower()
        mock_validate.assert_called_once_with("bambu_a1", "bambu_a1")


# ---------------------------------------------------------------------------
# TestRotateModel — MCP tool tests
# ---------------------------------------------------------------------------


class TestRotateModel:
    """Tests for the rotate_model MCP tool.

    Covers STL rotation, 3MF rotation, error cases, and default paths.
    """

    @staticmethod
    def _make_stl(path: str) -> str:
        """Create a minimal binary STL with one triangle."""
        with open(path, "wb") as f:
            f.write(b"\x00" * 80)  # header
            f.write(struct.pack("<I", 1))  # 1 triangle
            f.write(struct.pack("<fff", 0, 0, 1))  # normal
            f.write(struct.pack("<fff", 0, 0, 0))  # v1
            f.write(struct.pack("<fff", 1, 0, 0))  # v2
            f.write(struct.pack("<fff", 0, 1, 0))  # v3
            f.write(struct.pack("<H", 0))  # attribute
        return path

    @staticmethod
    def _make_3mf(path: str) -> str:
        """Create a minimal valid 3MF with one build item."""
        ns = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
        model_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<model xmlns="{ns}" unit="millimeter">'
            "<resources>"
            '<object id="1" type="model">'
            "<mesh>"
            "<vertices>"
            '<vertex x="0" y="0" z="0"/>'
            '<vertex x="1" y="0" z="0"/>'
            '<vertex x="0" y="1" z="0"/>'
            "</vertices>"
            "<triangles>"
            '<triangle v1="0" v2="1" v3="2"/>'
            "</triangles>"
            "</mesh>"
            "</object>"
            "</resources>"
            '<build><item objectid="1"/></build>'
            "</model>"
        )
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("3D/3dmodel.model", model_xml)
        return path

    def test_rotate_stl_z_axis(self, tmp_path):
        stl = self._make_stl(str(tmp_path / "model.stl"))
        result = rotate_model(input_path=stl, rotation_z=45.0)
        assert result["status"] == "success"
        assert os.path.isfile(result["output_path"])
        assert result["rotations_applied"]["z"] == 45.0

    def test_rotate_3mf_z_axis(self, tmp_path):
        threemf = self._make_3mf(str(tmp_path / "model.3mf"))
        result = rotate_model(input_path=threemf, rotation_z=90.0)
        assert result["status"] == "success"
        assert os.path.isfile(result["output_path"])
        assert result["rotations_applied"]["z"] == 90.0

    def test_rotate_nonexistent_file(self):
        result = rotate_model(input_path="/nonexistent/model.stl", rotation_z=45.0)
        assert result["success"] is False
        assert result["error"]["code"] == "FILE_NOT_FOUND"

    def test_rotate_unsupported_format(self, tmp_path):
        obj_path = str(tmp_path / "model.gcode")
        (tmp_path / "model.gcode").write_text("G28")
        result = rotate_model(input_path=obj_path, rotation_z=45.0)
        assert result["success"] is False
        assert result["error"]["code"] == "UNSUPPORTED"

    def test_rotate_default_output_path(self, tmp_path):
        stl = self._make_stl(str(tmp_path / "my_part.stl"))
        result = rotate_model(input_path=stl, rotation_z=10.0)
        assert result["status"] == "success"
        assert "_rotated" in result["output_path"]
        assert result["output_path"].endswith(".stl")

    def test_rotate_3mf_default_output_path(self, tmp_path):
        threemf = self._make_3mf(str(tmp_path / "my_part.3mf"))
        result = rotate_model(input_path=threemf, rotation_z=10.0)
        assert result["status"] == "success"
        assert "_rotated" in result["output_path"]
        assert result["output_path"].endswith(".3mf")

    def test_rotate_zero_degrees(self, tmp_path):
        stl = self._make_stl(str(tmp_path / "model.stl"))
        result = rotate_model(input_path=stl)
        assert result["status"] == "success"
        assert os.path.isfile(result["output_path"])
        assert result["rotations_applied"] == {"x": 0.0, "y": 0.0, "z": 0.0}

    def test_rotate_custom_output_path(self, tmp_path):
        stl = self._make_stl(str(tmp_path / "model.stl"))
        out = str(tmp_path / "custom_out.stl")
        result = rotate_model(input_path=stl, rotation_z=90.0, output_path=out)
        assert result["status"] == "success"
        assert result["output_path"] == out
        assert os.path.isfile(out)


# ---------------------------------------------------------------------------
# reslice_with_overrides
# ---------------------------------------------------------------------------


class TestCheckOrientation:
    """Tests for the check_orientation MCP tool.

    Covers: successful stability result, error JSON on failure,
    high-risk result with recommendation and suggested rotation.
    """

    def test_returns_stability_result(self):
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "stable": True,
            "risk_level": "low",
            "height_mm": 10.0,
            "base_footprint_mm2": 400.0,
            "height_to_base_ratio": 0.5,
            "center_of_gravity_z_mm": 5.0,
            "recommendation": "Orientation looks stable.",
            "suggested_rotation": None,
        }
        with patch("kiln.auto_orient.check_stability", create=True, return_value=mock_result):
            result = asyncio.run(check_orientation(model_path="/tmp/test.stl"))
        data = json.loads(result)
        assert data["stable"] is True
        assert data["risk_level"] == "low"

    def test_error_returns_json_error(self):
        with patch("kiln.auto_orient.check_stability", create=True, side_effect=ValueError("bad file")):
            result = asyncio.run(check_orientation(model_path="/tmp/bad.stl"))
        data = json.loads(result)
        assert "error" in data
        assert "bad file" in data["error"]

    def test_high_risk_includes_recommendation(self):
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "stable": False,
            "risk_level": "high",
            "height_mm": 100.0,
            "base_footprint_mm2": 25.0,
            "height_to_base_ratio": 20.0,
            "center_of_gravity_z_mm": 50.0,
            "recommendation": "High wobble risk. Reorienting recommended.",
            "suggested_rotation": {"x": 90, "y": 0, "z": 0},
        }
        with patch("kiln.auto_orient.check_stability", create=True, return_value=mock_result):
            result = asyncio.run(check_orientation(model_path="/tmp/tall.stl"))
        data = json.loads(result)
        assert data["stable"] is False
        assert data["suggested_rotation"] is not None
        assert data["suggested_rotation"]["x"] == 90

    def test_runtime_error_returns_error_json(self):
        with patch(
            "kiln.auto_orient.check_stability",
            create=True,
            side_effect=RuntimeError("mesh parse failed"),
        ):
            result = asyncio.run(check_orientation(model_path="/tmp/broken.stl"))
        data = json.loads(result)
        assert "error" in data
        assert "mesh parse failed" in data["error"]

    def test_result_is_valid_json(self):
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "stable": True,
            "risk_level": "low",
            "height_mm": 5.0,
            "base_footprint_mm2": 100.0,
            "height_to_base_ratio": 0.5,
            "center_of_gravity_z_mm": 2.5,
            "recommendation": "Stable",
            "suggested_rotation": None,
        }
        with patch("kiln.auto_orient.check_stability", create=True, return_value=mock_result):
            result = asyncio.run(check_orientation(model_path="/tmp/cube.stl"))
        # Verify it's parseable JSON with expected keys
        data = json.loads(result)
        expected_keys = {
            "stable", "risk_level", "height_mm", "base_footprint_mm2",
            "height_to_base_ratio", "center_of_gravity_z_mm",
            "recommendation", "suggested_rotation",
        }
        assert expected_keys.issubset(data.keys())


# ---------------------------------------------------------------------------
# monitor_print
# ---------------------------------------------------------------------------


class TestFormatDuration:
    """Tests for _format_duration helper."""

    def test_none_returns_na(self):
        assert _format_duration(None) == "N/A"

    def test_negative_returns_na(self):
        assert _format_duration(-5) == "N/A"

    def test_zero_seconds(self):
        assert _format_duration(0) == "~0s"

    def test_minutes_only(self):
        assert _format_duration(300) == "~5 min"

    def test_hours_and_minutes(self):
        assert _format_duration(5580) == "~1h 33min"

    def test_seconds_only(self):
        assert _format_duration(45) == "~45s"

    def test_exactly_one_hour(self):
        assert _format_duration(3600) == "~1h 0min"

    def test_large_value_24h(self):
        assert _format_duration(86400) == "~24h 0min"

    def test_float_seconds(self):
        assert _format_duration(90.7) == "~1 min"


class TestGeneratePrintComment:
    """Tests for _generate_print_comment helper."""

    def test_normal_printing(self):
        result = _generate_print_comment(
            "printing",
            completion=50.0,
            tool_actual=210.0,
            tool_target=210.0,
            bed_actual=60.0,
            bed_target=60.0,
            print_error=0,
        )
        assert result == "Print progressing normally."

    def test_error_detected(self):
        result = _generate_print_comment(
            "printing",
            completion=50.0,
            tool_actual=210.0,
            tool_target=210.0,
            bed_actual=60.0,
            bed_target=60.0,
            print_error=84033543,
        )
        assert "Error detected" in result

    def test_paused(self):
        result = _generate_print_comment(
            "paused",
            completion=50.0,
            tool_actual=210.0,
            tool_target=210.0,
            bed_actual=60.0,
            bed_target=60.0,
            print_error=0,
        )
        assert result == "Print is paused."

    def test_nozzle_heating(self):
        result = _generate_print_comment(
            "printing",
            completion=50.0,
            tool_actual=150.0,
            tool_target=210.0,
            bed_actual=60.0,
            bed_target=60.0,
            print_error=0,
        )
        assert "Nozzle still heating" in result

    def test_almost_done(self):
        result = _generate_print_comment(
            "printing",
            completion=95.0,
            tool_actual=210.0,
            tool_target=210.0,
            bed_actual=60.0,
            bed_target=60.0,
            print_error=0,
        )
        assert "Almost done" in result

    def test_just_started(self):
        result = _generate_print_comment(
            "printing",
            completion=2.0,
            tool_actual=210.0,
            tool_target=210.0,
            bed_actual=60.0,
            bed_target=60.0,
            print_error=0,
        )
        assert "just started" in result

    def test_idle_state(self):
        result = _generate_print_comment(
            "idle",
            completion=None,
            tool_actual=25.0,
            tool_target=0.0,
            bed_actual=25.0,
            bed_target=0.0,
            print_error=0,
        )
        assert "idle" in result

    def test_bed_heating(self):
        result = _generate_print_comment(
            "printing",
            completion=3.0,
            tool_actual=210.0,
            tool_target=210.0,
            bed_actual=30.0,
            bed_target=60.0,
            print_error=0,
        )
        assert "Bed still heating" in result

    def test_nozzle_overtemp(self):
        result = _generate_print_comment(
            "printing",
            completion=50.0,
            tool_actual=230.0,
            tool_target=210.0,
            bed_actual=60.0,
            bed_target=60.0,
            print_error=0,
        )
        assert "Nozzle temperature deviation" in result

    def test_all_none_temps(self):
        result = _generate_print_comment(
            "printing",
            completion=50.0,
            tool_actual=None,
            tool_target=None,
            bed_actual=None,
            bed_target=None,
            print_error=0,
        )
        assert result == "Print progressing normally."


class TestMonitorPrint:
    """Tests for the monitor_print MCP tool.

    Covers: standard output format, snapshot handling, error states,
    printer not found, all report sections present.
    """

    def _mock_state(self, **kwargs):
        defaults = {
            "connected": True,
            "state": "printing",
            "tool_temp_actual": 210.0,
            "tool_temp_target": 210.0,
            "bed_temp_actual": 60.0,
            "bed_temp_target": 60.0,
            "speed_profile": "Normal",
            "speed_magnitude": 100,
            "print_error": 0,
        }
        defaults.update(kwargs)
        state = MagicMock()
        state.to_dict.return_value = defaults
        return state

    def _mock_job(self, **kwargs):
        defaults = {
            "file_name": "test_model.gcode",
            "completion": 45.0,
            "print_time_seconds": 1800,
            "print_time_left_seconds": 2200,
            "current_layer": 50,
            "total_layers": 120,
        }
        defaults.update(kwargs)
        job = MagicMock()
        job.to_dict.return_value = defaults
        return job

    def test_standard_format_contains_all_sections(self):
        adapter = MagicMock()
        adapter.get_state.return_value = self._mock_state()
        adapter.get_job.return_value = self._mock_job()
        adapter.get_snapshot.return_value = b"\xff\xd8\xff"

        with patch("kiln.server._get_adapter", return_value=adapter):
            result = monitor_print(include_snapshot=False)

        assert "Print Status" in result
        assert "45% complete" in result
        assert "File: test_model.gcode" in result
        assert "Layer: 50 / 120" in result
        assert "Time elapsed:" in result
        assert "Remaining:" in result
        assert "Nozzle: 210°C" in result
        assert "Bed: 60°C" in result
        assert "Speed: Normal (100%)" in result
        assert "Errors: None" in result
        assert "Camera:" in result
        assert "Comments:" in result

    def test_snapshot_saved_when_enabled(self):
        adapter = MagicMock()
        adapter.get_state.return_value = self._mock_state()
        adapter.get_job.return_value = self._mock_job()
        adapter.get_snapshot.return_value = b"\xff\xd8\xff\xe0"

        with patch("kiln.server._get_adapter", return_value=adapter):
            result = monitor_print(include_snapshot=True)

        assert "Camera:" in result
        assert "kiln_monitor_" in result
        assert ".jpg" in result
        adapter.get_snapshot.assert_called_once()

    def test_no_snapshot_when_disabled(self):
        adapter = MagicMock()
        adapter.get_state.return_value = self._mock_state()
        adapter.get_job.return_value = self._mock_job()

        with patch("kiln.server._get_adapter", return_value=adapter):
            result = monitor_print(include_snapshot=False)

        assert "Camera: No camera available" in result
        adapter.get_snapshot.assert_not_called()

    def test_snapshot_failure_graceful(self):
        adapter = MagicMock()
        adapter.get_state.return_value = self._mock_state()
        adapter.get_job.return_value = self._mock_job()
        adapter.get_snapshot.side_effect = RuntimeError("camera offline")

        with patch("kiln.server._get_adapter", return_value=adapter):
            result = monitor_print(include_snapshot=True)

        assert "Snapshot capture failed" in result

    def test_error_code_shown(self):
        adapter = MagicMock()
        adapter.get_state.return_value = self._mock_state(print_error=84033543)
        adapter.get_job.return_value = self._mock_job()

        with patch("kiln.server._get_adapter", return_value=adapter):
            result = monitor_print(include_snapshot=False)

        assert "Errors: Code 84033543" in result
        assert "Error detected" in result

    def test_printer_not_found(self):
        from kiln.registry import PrinterNotFoundError

        with patch("kiln.server._registry") as mock_reg:
            mock_reg.get.side_effect = PrinterNotFoundError("ghost")
            result = monitor_print(printer_name="ghost", include_snapshot=False)

        assert "not found" in result

    def test_printer_error(self):
        with patch("kiln.server._get_adapter", side_effect=PrinterError("offline")):
            result = monitor_print(include_snapshot=False)

        assert "Error:" in result
        assert "offline" in result

    def test_na_when_fields_missing(self):
        adapter = MagicMock()
        adapter.get_state.return_value = self._mock_state(
            tool_temp_actual=None,
            tool_temp_target=None,
            bed_temp_actual=None,
            bed_temp_target=None,
        )
        adapter.get_job.return_value = self._mock_job(
            completion=None,
            current_layer=None,
            total_layers=None,
            print_time_seconds=None,
            print_time_left_seconds=None,
            file_name=None,
        )

        with patch("kiln.server._get_adapter", return_value=adapter):
            result = monitor_print(include_snapshot=False)

        assert "N/A% complete" in result
        assert "File: N/A" in result
        assert "Layer: N/A" in result
        assert "Nozzle: N/A" in result
        assert "Bed: N/A" in result

    def test_uses_named_printer(self):
        adapter = MagicMock()
        adapter.get_state.return_value = self._mock_state()
        adapter.get_job.return_value = self._mock_job()

        with patch("kiln.server._registry") as mock_reg:
            mock_reg.get.return_value = adapter
            result = monitor_print(printer_name="bambu-a1", include_snapshot=False)

        mock_reg.get.assert_called_once_with("bambu-a1")
        assert "Print Status" in result

    def test_chamber_temp_shown_when_available(self):
        adapter = MagicMock()
        adapter.get_state.return_value = self._mock_state(chamber_temp_actual=45.0)
        adapter.get_job.return_value = self._mock_job()

        with patch("kiln.server._get_adapter", return_value=adapter):
            result = monitor_print(include_snapshot=False)

        assert "Chamber: 45°C" in result

    def test_chamber_temp_hidden_when_none(self):
        adapter = MagicMock()
        adapter.get_state.return_value = self._mock_state()
        adapter.get_job.return_value = self._mock_job()

        with patch("kiln.server._get_adapter", return_value=adapter):
            result = monitor_print(include_snapshot=False)

        assert "Chamber:" not in result

    def test_speed_na_when_both_fields_none(self):
        adapter = MagicMock()
        adapter.get_state.return_value = self._mock_state(
            speed_profile=None, speed_magnitude=None,
        )
        adapter.get_job.return_value = self._mock_job()

        with patch("kiln.server._get_adapter", return_value=adapter):
            result = monitor_print(include_snapshot=False)

        assert "Speed: N/A" in result
        # Must NOT show "N/A (N/A%)"
        assert "N/A%" not in result

    def test_snapshot_adapter_no_get_snapshot(self):
        adapter = MagicMock(spec=[])  # empty spec — no get_snapshot method
        adapter.get_state = MagicMock(return_value=self._mock_state())
        adapter.get_job = MagicMock(return_value=self._mock_job())

        with patch("kiln.server._get_adapter", return_value=adapter):
            result = monitor_print(include_snapshot=True)

        # Should degrade gracefully (snapshot capture failed or no camera)
        assert "Camera:" in result


# ---------------------------------------------------------------------------
# multi_copy_print
# ---------------------------------------------------------------------------


class TestMultiCopyPrint:
    """Tests for the multi_copy_print MCP tool.

    Covers: validation errors, PrusaSlicer strategy, OrcaSlicer fallback,
    file not found, invalid overrides, auth check.
    """

    def _mock_pipeline_result(self, success=True):
        result = MagicMock()
        result.success = success
        result.to_dict.return_value = {
            "pipeline": "reslice_and_print",
            "success": success,
            "steps": [],
        }
        return result

    def test_copies_less_than_2_rejected(self, tmp_path):
        stl = os.path.join(str(tmp_path), "model.stl")
        with open(stl, "wb") as f:
            f.write(b"\x00" * 84)
        result = multi_copy_print(model_path=stl, copies=1)
        assert result["success"] is False
        assert "copies must be >= 2" in result["error"]["message"]

    def test_copies_more_than_20_rejected(self, tmp_path):
        stl = os.path.join(str(tmp_path), "model.stl")
        with open(stl, "wb") as f:
            f.write(b"\x00" * 84)
        result = multi_copy_print(model_path=stl, copies=21)
        assert result["success"] is False
        assert "copies must be <= 20" in result["error"]["message"]

    def test_non_stl_file_rejected(self, tmp_path):
        gcode = os.path.join(str(tmp_path), "model.gcode")
        with open(gcode, "w") as f:
            f.write("G28\n")
        result = multi_copy_print(model_path=gcode, copies=2)
        assert result["success"] is False
        assert "STL or OBJ" in result["error"]["message"]

    def test_file_not_found(self):
        result = multi_copy_print(model_path="/nonexistent/model.stl", copies=2)
        assert result["success"] is False
        assert "not found" in result["error"]["message"].lower()

    def test_invalid_overrides_json(self, tmp_path):
        stl = os.path.join(str(tmp_path), "model.stl")
        with open(stl, "wb") as f:
            f.write(b"\x00" * 84)
        result = multi_copy_print(
            model_path=stl, copies=2, overrides="not json",
        )
        assert result["success"] is False
        assert "Invalid JSON" in result["error"]["message"]

    def test_overrides_not_dict_rejected(self, tmp_path):
        stl = os.path.join(str(tmp_path), "model.stl")
        with open(stl, "wb") as f:
            f.write(b"\x00" * 84)
        result = multi_copy_print(
            model_path=stl, copies=2, overrides="[1, 2, 3]",
        )
        assert result["success"] is False
        assert "JSON object" in result["error"]["message"]

    def test_prusaslicer_uses_duplicate_flag(self, tmp_path):
        stl = os.path.join(str(tmp_path), "model.stl")
        with open(stl, "wb") as f:
            f.write(b"\x00" * 84)

        mock_slicer = MagicMock()
        mock_slicer.name = "PrusaSlicer"

        pipeline_result = self._mock_pipeline_result()

        with (
            patch("kiln.slicer.find_slicer", return_value=mock_slicer),
            patch("kiln.server._pipeline_reslice_and_print", return_value=pipeline_result) as mock_pipeline,
        ):
            result = multi_copy_print(model_path=stl, copies=3, spacing_mm=15.0)

        assert result["success"] is True
        assert result["strategy"] == "prusaslicer_duplicate"
        assert result["copies"] == 3
        # Verify extra_args passed to pipeline
        call_kwargs = mock_pipeline.call_args[1]
        assert "--duplicate" in call_kwargs["extra_args"]
        assert "3" in call_kwargs["extra_args"]
        assert "--duplicate-distance" in call_kwargs["extra_args"]
        assert "15.0" in call_kwargs["extra_args"]

    def test_orcaslicer_uses_stl_duplication(self, tmp_path):
        # Create a valid STL with geometry (need parseable triangles)
        stl = _make_test_stl(tmp_path, 20, 20, 10)

        mock_slicer = MagicMock()
        mock_slicer.name = "OrcaSlicer"

        pipeline_result = self._mock_pipeline_result()

        with (
            patch("kiln.slicer.find_slicer", return_value=mock_slicer),
            patch("kiln.server._pipeline_reslice_and_print", return_value=pipeline_result) as mock_pipeline,
        ):
            result = multi_copy_print(model_path=stl, copies=2)

        assert result["success"] is True
        assert result["strategy"] == "stl_mesh_duplication"
        assert result["copies"] == 2
        # The model_path passed to the pipeline should be the merged STL, not the original
        call_kwargs = mock_pipeline.call_args[1]
        assert call_kwargs["model_path"] != stl

    def test_negative_spacing_rejected(self, tmp_path):
        stl = os.path.join(str(tmp_path), "model.stl")
        with open(stl, "wb") as f:
            f.write(b"\x00" * 84)
        result = multi_copy_print(model_path=stl, copies=2, spacing_mm=-5)
        assert result["success"] is False
        assert "spacing" in result["error"]["message"].lower()

    def test_auth_check_applied(self):
        with patch("kiln.server._check_auth", return_value={"error": "denied"}):
            result = multi_copy_print(model_path="/tmp/model.stl", copies=2)
        assert result == {"error": "denied"}

    def test_slicer_not_found(self, tmp_path):
        from kiln.slicer import SlicerNotFoundError

        stl = os.path.join(str(tmp_path), "model.stl")
        with open(stl, "wb") as f:
            f.write(b"\x00" * 84)
        with patch("kiln.slicer.find_slicer", side_effect=SlicerNotFoundError("No slicer installed")):
            result = multi_copy_print(model_path=stl, copies=2)
        assert result["success"] is False
        assert "No slicer" in result["error"]["message"]

    def test_valid_overrides_passed_to_pipeline(self, tmp_path):
        stl = os.path.join(str(tmp_path), "model.stl")
        with open(stl, "wb") as f:
            f.write(b"\x00" * 84)

        mock_slicer = MagicMock()
        mock_slicer.name = "PrusaSlicer"
        pipeline_result = self._mock_pipeline_result()

        with (
            patch("kiln.slicer.find_slicer", return_value=mock_slicer),
            patch("kiln.server._pipeline_reslice_and_print", return_value=pipeline_result) as mock_pipeline,
        ):
            result = multi_copy_print(
                model_path=stl, copies=2,
                overrides='{"fill_density": "20%"}',
            )

        assert result["success"] is True
        call_kwargs = mock_pipeline.call_args[1]
        assert call_kwargs["overrides"] == {"fill_density": "20%"}

    def test_obj_file_accepted(self, tmp_path):
        obj = os.path.join(str(tmp_path), "model.obj")
        with open(obj, "w") as f:
            f.write("v 0 0 0\n")

        mock_slicer = MagicMock()
        mock_slicer.name = "PrusaSlicer"
        pipeline_result = self._mock_pipeline_result()

        with (
            patch("kiln.slicer.find_slicer", return_value=mock_slicer),
            patch("kiln.server._pipeline_reslice_and_print", return_value=pipeline_result),
        ):
            result = multi_copy_print(model_path=obj, copies=2)

        assert result["success"] is True

    def test_zero_spacing_accepted(self, tmp_path):
        stl = os.path.join(str(tmp_path), "model.stl")
        with open(stl, "wb") as f:
            f.write(b"\x00" * 84)

        mock_slicer = MagicMock()
        mock_slicer.name = "PrusaSlicer"
        pipeline_result = self._mock_pipeline_result()

        with (
            patch("kiln.slicer.find_slicer", return_value=mock_slicer),
            patch("kiln.server._pipeline_reslice_and_print", return_value=pipeline_result) as mock_pipeline,
        ):
            result = multi_copy_print(model_path=stl, copies=2, spacing_mm=0)

        assert result["success"] is True
        call_kwargs = mock_pipeline.call_args[1]
        assert "0" in call_kwargs["extra_args"]


def _make_test_stl(tmp_path, width: float, depth: float, height: float) -> str:
    """Create a binary STL rectangular prism for testing."""
    w, d, h = float(width), float(depth), float(height)
    v = [
        (0, 0, 0), (w, 0, 0), (w, d, 0), (0, d, 0),
        (0, 0, h), (w, 0, h), (w, d, h), (0, d, h),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6),
        (0, 4, 7), (0, 7, 3), (1, 2, 6), (1, 6, 5),
    ]
    path = os.path.join(str(tmp_path), f"test_{width}x{depth}x{height}.stl")
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            f.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            f.write(struct.pack("<3f", *v[a]))
            f.write(struct.pack("<3f", *v[b]))
            f.write(struct.pack("<3f", *v[c]))
            f.write(struct.pack("<H", 0))
    return path


# ---------------------------------------------------------------------------
# AMS auto-detect (_resolve_use_ams)
# ---------------------------------------------------------------------------


class TestResolveUseAms:
    """Tests for the tri-state use_ams auto-detection logic."""

    def test_bool_true_passes_through(self):
        from kiln.server import _resolve_use_ams

        adapter = MagicMock()
        result = _resolve_use_ams(True, None, adapter)
        assert result["use_ams"] is True
        adapter.get_ams_status.assert_not_called()

    def test_bool_false_passes_through(self):
        from kiln.server import _resolve_use_ams

        adapter = MagicMock()
        result = _resolve_use_ams(False, None, adapter)
        assert result["use_ams"] is False
        adapter.get_ams_status.assert_not_called()

    def test_string_true_enables(self):
        from kiln.server import _resolve_use_ams

        adapter = MagicMock()
        result = _resolve_use_ams("true", None, adapter)
        assert result["use_ams"] is True

    def test_string_false_disables(self):
        from kiln.server import _resolve_use_ams

        adapter = MagicMock()
        result = _resolve_use_ams("false", None, adapter)
        assert result["use_ams"] is False

    def test_auto_with_loaded_ams_enables(self):
        from kiln.server import _resolve_use_ams

        adapter = MagicMock()
        adapter.get_ams_status.return_value = {
            "units": [
                {
                    "unit_id": 0,
                    "trays": [
                        {"slot": 0, "tray_type": "PLA", "remain": 85},
                        {"slot": 1, "tray_type": "PLA", "remain": 50},
                    ],
                }
            ]
        }
        result = _resolve_use_ams("auto", None, adapter)
        assert result["use_ams"] is True
        assert result["ams_mapping"] == [0]

    def test_auto_with_empty_ams_disables(self):
        from kiln.server import _resolve_use_ams

        adapter = MagicMock()
        adapter.get_ams_status.return_value = {"units": []}
        result = _resolve_use_ams("auto", None, adapter)
        assert result["use_ams"] is False

    def test_auto_with_no_loaded_trays_disables(self):
        from kiln.server import _resolve_use_ams

        adapter = MagicMock()
        adapter.get_ams_status.return_value = {
            "units": [
                {
                    "unit_id": 0,
                    "trays": [
                        {"slot": 0, "tray_type": "", "remain": 0},
                    ],
                }
            ]
        }
        result = _resolve_use_ams("auto", None, adapter)
        assert result["use_ams"] is False

    def test_auto_no_get_ams_status_method(self):
        from kiln.server import _resolve_use_ams

        adapter = MagicMock(spec=[])  # no methods at all
        result = _resolve_use_ams("auto", None, adapter)
        assert result["use_ams"] is False

    def test_auto_probe_exception_falls_back(self):
        from kiln.server import _resolve_use_ams

        adapter = MagicMock()
        adapter.get_ams_status.side_effect = RuntimeError("MQTT timeout")
        result = _resolve_use_ams("auto", None, adapter)
        assert result["use_ams"] is False

    def test_auto_respects_explicit_mapping(self):
        """When caller provides ams_mapping, auto should not override it."""
        from kiln.server import _resolve_use_ams

        adapter = MagicMock()
        adapter.get_ams_status.return_value = {
            "units": [
                {
                    "unit_id": 0,
                    "trays": [
                        {"slot": 0, "tray_type": "PLA", "remain": 85},
                        {"slot": 3, "tray_type": "PETG", "remain": 60},
                    ],
                }
            ]
        }
        result = _resolve_use_ams("auto", [3, 1], adapter)
        assert result["use_ams"] is True
        # Should NOT override the caller's mapping
        assert result["ams_mapping"] is None

    def test_auto_selects_first_loaded_slot(self):
        """Auto-mapping should pick the first slot with filament."""
        from kiln.server import _resolve_use_ams

        adapter = MagicMock()
        adapter.get_ams_status.return_value = {
            "units": [
                {
                    "unit_id": 0,
                    "trays": [
                        {"slot": 0, "tray_type": "", "remain": 0},  # empty
                        {"slot": 1, "tray_type": "", "remain": 0},  # empty
                        {"slot": 2, "tray_type": "ABS", "remain": 40},  # loaded
                        {"slot": 3, "tray_type": "PLA", "remain": 90},  # loaded
                    ],
                }
            ]
        }
        result = _resolve_use_ams("auto", None, adapter)
        assert result["use_ams"] is True
        assert result["ams_mapping"] == [2]
