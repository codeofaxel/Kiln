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
- _get_adapter() creates correct adapter for octoprint and moonraker
- _validate_local_file() with valid, invalid extension, missing, and empty files
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from kiln.printers.bambu import BambuAdapter
from kiln.printers.octoprint import OctoPrintAdapter
from kiln.printers.moonraker import MoonrakerAdapter
from kiln.server import (
    _error_dict,
    _validate_local_file,
    cancel_print as server_cancel_print,
    delete_file as server_delete_file,
    pause_print as server_pause_print,
    preflight_check,
    printer_files,
    printer_status,
    resume_print as server_resume_print,
    send_gcode,
    set_temperature,
    start_print as server_start_print,
    upload_file as server_upload_file,
)


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

    @patch("kiln.server._get_adapter")
    def test_success(self, mock_get_adapter):
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

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_get_adapter):
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
            tool_temp_actual=270.0,
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
            bed_temp_actual=120.0,
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

from kiln.server import (
    validate_gcode as server_validate_gcode,
    fleet_status,
    register_printer,
    submit_job as server_submit_job,
    job_status as server_job_status,
    queue_summary,
    cancel_job as server_cancel_job,
    recent_events,
    search_models,
    model_details,
    model_files,
    download_model,
    browse_models,
    list_model_categories,
)
from kiln.registry import PrinterRegistry
from kiln.queue import PrintQueue, JobStatus
from kiln.events import EventBus, EventType
from kiln.thingiverse import (
    ThingiverseClient,
    ThingiverseError,
    ThingiverseNotFoundError,
    ThingSummary,
    ThingDetail,
    ThingFile as TvFile,
    Category as TvCategory,
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

    def test_with_mixed_statuses(self, monkeypatch):
        import kiln.server as mod

        fresh_queue = PrintQueue()
        monkeypatch.setattr(mod, "_queue", fresh_queue)

        id1 = fresh_queue.submit(file_name="printing.gcode", submitted_by="test")
        fresh_queue.mark_printing(id1)
        fresh_queue.submit(file_name="queued.gcode", submitted_by="test")

        result = queue_summary()
        assert result["success"] is True
        assert result["total"] == 2
        assert result["pending"] == 1
        assert result["active"] == 1
        assert "queued" in result["counts"]
        assert "printing" in result["counts"]


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
