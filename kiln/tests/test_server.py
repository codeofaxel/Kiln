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
from kiln.printers.octoprint import OctoPrintAdapter
from kiln.printers.moonraker import MoonrakerAdapter
from kiln.server import (
    _error_dict,
    _validate_local_file,
    cancel_print as server_cancel_print,
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
        adapter._post.side_effect = PrinterError("printer busy")
        mock_get_adapter.return_value = adapter

        result = send_gcode("G28")
        assert result["success"] is False

    @patch("kiln.server._get_adapter")
    def test_moonraker_adapter(self, mock_get_adapter):
        """send_gcode works with MoonrakerAdapter via _send_gcode."""
        adapter = MagicMock(spec=MoonrakerAdapter)
        mock_get_adapter.return_value = adapter

        result = send_gcode("G28\nG1 Z10")
        assert result["success"] is True
        assert result["count"] == 2
        adapter._send_gcode.assert_called_once_with("G28\nG1 Z10")

    @patch("kiln.server._get_adapter")
    def test_unsupported_adapter(self, mock_get_adapter):
        """send_gcode returns UNSUPPORTED for unknown adapter types."""
        adapter = MagicMock()
        adapter.name = "other_printer"
        # Not spec'd to OctoPrintAdapter or MoonrakerAdapter
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
        monkeypatch.setattr(mod, "_PRINTER_TYPE", "bambu")

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
