"""Tests for octoprint_cli.safety."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from octoprint_cli.safety import (
    _GCODE_EXTENSIONS,
    _LARGE_FILE_THRESHOLD,
    _MAX_FILE_SIZE,
    check_can_cancel,
    check_printer_ready,
    check_temperatures,
    estimate_resources,
    preflight_check,
    validate_file,
)


# ---------------------------------------------------------------------------
# Helpers: build mock clients that return canned API responses
# ---------------------------------------------------------------------------


def _mock_client(
    connection_response: Dict[str, Any] | None = None,
    printer_response: Dict[str, Any] | None = None,
    job_response: Dict[str, Any] | None = None,
    file_info_response: Dict[str, Any] | None = None,
) -> MagicMock:
    """Build a mock OctoPrintClient returning the given canned responses."""
    client = MagicMock()
    if connection_response is not None:
        client.get_connection.return_value = connection_response
    if printer_response is not None:
        client.get_printer_state.return_value = printer_response
    if job_response is not None:
        client.get_job.return_value = job_response
    if file_info_response is not None:
        client.get_file_info.return_value = file_info_response
    return client


def _success(data: Any) -> Dict[str, Any]:
    return {"success": True, "data": data, "error": None}


def _error(code: str = "CONNECTION_ERROR", message: str = "fail") -> Dict[str, Any]:
    return {
        "success": False,
        "data": None,
        "error": {"code": code, "message": message},
    }


# ===================================================================
# check_printer_ready
# ===================================================================


class TestCheckPrinterReady:
    """Tests for check_printer_ready()."""

    def test_fully_ready(
        self,
        connection_state_operational: Dict[str, Any],
        printer_state_operational: Dict[str, Any],
    ) -> None:
        client = _mock_client(
            connection_response=_success(connection_state_operational),
            printer_response=_success(printer_state_operational),
        )
        result = check_printer_ready(client)
        assert result["ready"] is True
        assert len(result["errors"]) == 0
        assert all(c["passed"] for c in result["checks"])

    def test_not_ready_when_disconnected(
        self,
        connection_state_closed: Dict[str, Any],
        printer_state_operational: Dict[str, Any],
    ) -> None:
        client = _mock_client(
            connection_response=_success(connection_state_closed),
            printer_response=_success(printer_state_operational),
        )
        result = check_printer_ready(client)
        assert result["ready"] is False
        assert any("not connected" in e.lower() for e in result["errors"])

    def test_not_ready_when_printing(
        self,
        connection_state_operational: Dict[str, Any],
        printer_state_printing: Dict[str, Any],
    ) -> None:
        client = _mock_client(
            connection_response=_success(connection_state_operational),
            printer_response=_success(printer_state_printing),
        )
        result = check_printer_ready(client)
        assert result["ready"] is False
        assert any("busy" in e.lower() for e in result["errors"])

    def test_not_ready_when_error_state(
        self,
        connection_state_operational: Dict[str, Any],
        printer_state_error: Dict[str, Any],
    ) -> None:
        client = _mock_client(
            connection_response=_success(connection_state_operational),
            printer_response=_success(printer_state_error),
        )
        result = check_printer_ready(client)
        assert result["ready"] is False
        assert any("error" in e.lower() for e in result["errors"])

    def test_connection_api_failure(
        self,
        printer_state_operational: Dict[str, Any],
    ) -> None:
        """If get_connection fails, the printer should not be considered ready."""
        client = _mock_client(
            connection_response=_error("CONNECTION_ERROR", "Host unreachable"),
            printer_response=_success(printer_state_operational),
        )
        result = check_printer_ready(client)
        assert result["ready"] is False
        assert any("failed to query connection" in e.lower() for e in result["errors"])

    def test_printer_state_api_failure(
        self,
        connection_state_operational: Dict[str, Any],
    ) -> None:
        """If get_printer_state fails, the printer should not be considered ready."""
        client = _mock_client(
            connection_response=_success(connection_state_operational),
            printer_response=_error("TIMEOUT", "Timed out"),
        )
        result = check_printer_ready(client)
        assert result["ready"] is False
        assert any("failed to query printer state" in e.lower() for e in result["errors"])

    def test_checks_list_structure(
        self,
        connection_state_operational: Dict[str, Any],
        printer_state_operational: Dict[str, Any],
    ) -> None:
        """Verify the expected check names are present."""
        client = _mock_client(
            connection_response=_success(connection_state_operational),
            printer_response=_success(printer_state_operational),
        )
        result = check_printer_ready(client)
        check_names = [c["name"] for c in result["checks"]]
        assert "printer_connected" in check_names
        assert "printer_operational" in check_names
        assert "not_printing" in check_names
        assert "no_errors" in check_names


# ===================================================================
# check_temperatures
# ===================================================================


class TestCheckTemperatures:
    """Tests for check_temperatures()."""

    def test_safe_temperatures(
        self, printer_state_operational: Dict[str, Any]
    ) -> None:
        client = _mock_client(printer_response=_success(printer_state_operational))
        result = check_temperatures(client)
        assert result["safe"] is True
        assert len(result["warnings"]) == 0
        assert result["tool"]["actual"] == 24.3
        assert result["bed"]["actual"] == 23.1

    def test_tool_over_limit(self) -> None:
        data = {
            "temperature": {
                "tool0": {"actual": 280.0, "target": 210.0},
                "bed": {"actual": 60.0, "target": 60.0},
            }
        }
        client = _mock_client(printer_response=_success(data))
        result = check_temperatures(client, max_tool_temp=260)
        assert result["safe"] is False
        assert any("tool temperature" in w.lower() and "exceeds" in w.lower() for w in result["warnings"])

    def test_tool_target_over_limit(self) -> None:
        data = {
            "temperature": {
                "tool0": {"actual": 200.0, "target": 300.0},
                "bed": {"actual": 60.0, "target": 60.0},
            }
        }
        client = _mock_client(printer_response=_success(data))
        result = check_temperatures(client, max_tool_temp=260)
        assert result["safe"] is False
        assert any("tool target" in w.lower() for w in result["warnings"])

    def test_bed_over_limit(self) -> None:
        data = {
            "temperature": {
                "tool0": {"actual": 200.0, "target": 210.0},
                "bed": {"actual": 120.0, "target": 60.0},
            }
        }
        client = _mock_client(printer_response=_success(data))
        result = check_temperatures(client, max_bed_temp=110)
        assert result["safe"] is False
        assert any("bed temperature" in w.lower() and "exceeds" in w.lower() for w in result["warnings"])

    def test_bed_target_over_limit(self) -> None:
        data = {
            "temperature": {
                "tool0": {"actual": 200.0, "target": 210.0},
                "bed": {"actual": 60.0, "target": 150.0},
            }
        }
        client = _mock_client(printer_response=_success(data))
        result = check_temperatures(client, max_bed_temp=110)
        assert result["safe"] is False
        assert any("bed target" in w.lower() for w in result["warnings"])

    def test_warning_tool_set_bed_not(self) -> None:
        """Warn if tool is heated but bed is not."""
        data = {
            "temperature": {
                "tool0": {"actual": 50.0, "target": 210.0},
                "bed": {"actual": 23.0, "target": 0.0},
            }
        }
        client = _mock_client(printer_response=_success(data))
        result = check_temperatures(client)
        assert result["safe"] is True  # It's still safe, just a warning
        assert any("tool temperature is set but bed" in w.lower() for w in result["warnings"])

    def test_warning_bed_set_tool_not(self) -> None:
        """Warn if bed is heated but tool is not."""
        data = {
            "temperature": {
                "tool0": {"actual": 24.0, "target": 0.0},
                "bed": {"actual": 50.0, "target": 60.0},
            }
        }
        client = _mock_client(printer_response=_success(data))
        result = check_temperatures(client)
        assert result["safe"] is True
        assert any("bed temperature is set but tool" in w.lower() for w in result["warnings"])

    def test_api_failure(self) -> None:
        client = _mock_client(
            printer_response=_error("TIMEOUT", "Connection timed out")
        )
        result = check_temperatures(client)
        assert result["safe"] is False
        assert len(result["warnings"]) > 0
        assert any("could not read" in w.lower() for w in result["warnings"])

    def test_custom_limits(self) -> None:
        """Check that custom max_tool_temp and max_bed_temp are respected."""
        data = {
            "temperature": {
                "tool0": {"actual": 200.0, "target": 200.0},
                "bed": {"actual": 80.0, "target": 80.0},
            }
        }
        client = _mock_client(printer_response=_success(data))
        # Within custom limits
        result = check_temperatures(client, max_tool_temp=250, max_bed_temp=100)
        assert result["safe"] is True

        # Outside custom limits
        result = check_temperatures(client, max_tool_temp=190, max_bed_temp=70)
        assert result["safe"] is False

    def test_none_temperature_values(self) -> None:
        """None temperature values should be treated as 0.0."""
        data = {
            "temperature": {
                "tool0": {"actual": None, "target": None},
                "bed": {"actual": None, "target": None},
            }
        }
        client = _mock_client(printer_response=_success(data))
        result = check_temperatures(client)
        assert result["safe"] is True
        assert result["tool"]["actual"] == 0.0
        assert result["bed"]["actual"] == 0.0


# ===================================================================
# validate_file
# ===================================================================


class TestValidateFile:
    """Tests for validate_file()."""

    def test_valid_gcode_file(self, gcode_file: Path) -> None:
        result = validate_file(str(gcode_file))
        assert result["valid"] is True
        assert len(result["errors"]) == 0
        assert result["info"]["extension"] == ".gcode"
        assert result["info"]["size_bytes"] > 0

    def test_nonexistent_file(self) -> None:
        result = validate_file("/nonexistent/path/model.gcode")
        assert result["valid"] is False
        assert any("not found" in e.lower() for e in result["errors"])

    def test_wrong_extension(self, non_gcode_file: Path) -> None:
        result = validate_file(str(non_gcode_file))
        assert result["valid"] is False
        assert any("unsupported" in e.lower() for e in result["errors"])

    def test_empty_file(self, empty_file: Path) -> None:
        result = validate_file(str(empty_file))
        assert result["valid"] is False
        assert any("empty" in e.lower() for e in result["errors"])

    def test_directory_instead_of_file(self, tmp_path: Path) -> None:
        result = validate_file(str(tmp_path))
        assert result["valid"] is False
        assert any("not a regular file" in e.lower() for e in result["errors"])

    def test_gco_extension(self, tmp_path: Path) -> None:
        """Files with .gco extension should be valid."""
        p = tmp_path / "model.gco"
        p.write_text("G28\n")
        result = validate_file(str(p))
        assert result["valid"] is True
        assert result["info"]["extension"] == ".gco"

    def test_g_extension(self, tmp_path: Path) -> None:
        """Files with .g extension should be valid."""
        p = tmp_path / "model.g"
        p.write_text("G28\n")
        result = validate_file(str(p))
        assert result["valid"] is True
        assert result["info"]["extension"] == ".g"

    def test_info_size_human_populated(self, gcode_file: Path) -> None:
        result = validate_file(str(gcode_file))
        assert result["info"]["size_human"] != "0 B"

    def test_all_gcode_extensions_recognized(self) -> None:
        """Verify the known extension set."""
        assert ".gcode" in _GCODE_EXTENSIONS
        assert ".gco" in _GCODE_EXTENSIONS
        assert ".g" in _GCODE_EXTENSIONS


# ===================================================================
# check_can_cancel
# ===================================================================


class TestCheckCanCancel:
    """Tests for check_can_cancel()."""

    def test_can_cancel_when_printing(
        self, printer_state_printing: Dict[str, Any]
    ) -> None:
        client = _mock_client(printer_response=_success(printer_state_printing))
        result = check_can_cancel(client)
        assert result["can_cancel"] is True
        assert "active job" in result["message"].lower()

    def test_cannot_cancel_when_idle(
        self, printer_state_operational: Dict[str, Any]
    ) -> None:
        client = _mock_client(printer_response=_success(printer_state_operational))
        result = check_can_cancel(client)
        assert result["can_cancel"] is False
        assert "no active job" in result["message"].lower()

    def test_can_cancel_when_paused(self) -> None:
        data = {
            "state": {
                "text": "Paused",
                "flags": {
                    "operational": True,
                    "printing": False,
                    "paused": True,
                    "pausing": False,
                    "error": False,
                    "closedOrError": False,
                },
            }
        }
        client = _mock_client(printer_response=_success(data))
        result = check_can_cancel(client)
        assert result["can_cancel"] is True

    def test_api_failure(self) -> None:
        client = _mock_client(
            printer_response=_error("TIMEOUT", "Timed out")
        )
        result = check_can_cancel(client)
        assert result["can_cancel"] is False
        assert "could not query" in result["message"].lower()

    def test_current_state_text(
        self, printer_state_printing: Dict[str, Any]
    ) -> None:
        client = _mock_client(printer_response=_success(printer_state_printing))
        result = check_can_cancel(client)
        assert result["current_state"] == "Printing"


# ===================================================================
# estimate_resources
# ===================================================================


class TestEstimateResources:
    """Tests for estimate_resources()."""

    def test_with_analysis_data(self) -> None:
        file_data = {
            "name": "benchy.gcode",
            "gcodeAnalysis": {
                "estimatedPrintTime": 7200,
                "filament": {
                    "tool0": {"length": 12000.0, "volume": 28.5},
                },
            },
        }
        client = _mock_client(file_info_response=_success(file_data))
        result = estimate_resources(client, "benchy.gcode")
        assert result["available"] is True
        assert result["estimated_print_time_seconds"] == 7200
        assert result["filament"]["length_mm"] == 12000.0
        assert result["filament"]["volume_cm3"] == 28.5

    def test_without_analysis(self) -> None:
        file_data = {"name": "new_file.gcode"}
        client = _mock_client(file_info_response=_success(file_data))
        result = estimate_resources(client, "new_file.gcode")
        assert result["available"] is False
        assert "not yet analyzed" in result["estimated_print_time"].lower()

    def test_api_failure(self) -> None:
        client = _mock_client(
            file_info_response=_error("NOT_FOUND", "File not found")
        )
        result = estimate_resources(client, "missing.gcode")
        assert result["available"] is False
        assert "error" in result["estimated_print_time"].lower()

    def test_no_filament_data(self) -> None:
        file_data = {
            "name": "benchy.gcode",
            "gcodeAnalysis": {
                "estimatedPrintTime": 3600,
                "filament": {},
            },
        }
        client = _mock_client(file_info_response=_success(file_data))
        result = estimate_resources(client, "benchy.gcode")
        assert result["available"] is True
        assert result["filament"]["length_mm"] is None
        assert result["filament"]["volume_cm3"] is None


# ===================================================================
# preflight_check (combined)
# ===================================================================


class TestPreflightCheck:
    """Tests for the combined preflight_check()."""

    def test_all_pass(
        self,
        connection_state_operational: Dict[str, Any],
        printer_state_operational: Dict[str, Any],
        gcode_file: Path,
    ) -> None:
        client = _mock_client(
            connection_response=_success(connection_state_operational),
            printer_response=_success(printer_state_operational),
        )
        result = preflight_check(client, file_path=str(gcode_file))
        assert result["ready"] is True
        assert "passed" in result["summary"].lower()

    def test_fails_when_printer_not_ready(
        self,
        connection_state_closed: Dict[str, Any],
        printer_state_operational: Dict[str, Any],
    ) -> None:
        client = _mock_client(
            connection_response=_success(connection_state_closed),
            printer_response=_success(printer_state_operational),
        )
        result = preflight_check(client)
        assert result["ready"] is False
        assert "printer not ready" in result["summary"].lower()

    def test_fails_when_file_invalid(
        self,
        connection_state_operational: Dict[str, Any],
        printer_state_operational: Dict[str, Any],
        non_gcode_file: Path,
    ) -> None:
        client = _mock_client(
            connection_response=_success(connection_state_operational),
            printer_response=_success(printer_state_operational),
        )
        result = preflight_check(client, file_path=str(non_gcode_file))
        assert result["ready"] is False
        assert "file validation failed" in result["summary"].lower()

    def test_fails_with_unsafe_temperatures(
        self,
        connection_state_operational: Dict[str, Any],
    ) -> None:
        hot_printer = {
            "state": {
                "text": "Operational",
                "flags": {
                    "operational": True,
                    "printing": False,
                    "pausing": False,
                    "paused": False,
                    "error": False,
                    "closedOrError": False,
                },
            },
            "temperature": {
                "tool0": {"actual": 300.0, "target": 300.0},
                "bed": {"actual": 60.0, "target": 60.0},
            },
        }
        client = _mock_client(
            connection_response=_success(connection_state_operational),
            printer_response=_success(hot_printer),
        )
        result = preflight_check(client)
        assert result["ready"] is False
        assert "temperature" in result["summary"].lower()

    def test_no_file_path_skips_file_check(
        self,
        connection_state_operational: Dict[str, Any],
        printer_state_operational: Dict[str, Any],
    ) -> None:
        client = _mock_client(
            connection_response=_success(connection_state_operational),
            printer_response=_success(printer_state_operational),
        )
        result = preflight_check(client)
        assert result["ready"] is True
        assert "file" not in result

    def test_includes_file_result_when_provided(
        self,
        connection_state_operational: Dict[str, Any],
        printer_state_operational: Dict[str, Any],
        gcode_file: Path,
    ) -> None:
        client = _mock_client(
            connection_response=_success(connection_state_operational),
            printer_response=_success(printer_state_operational),
        )
        result = preflight_check(client, file_path=str(gcode_file))
        assert "file" in result
        assert result["file"]["valid"] is True

    def test_includes_resources_when_server_file_given(
        self,
        connection_state_operational: Dict[str, Any],
        printer_state_operational: Dict[str, Any],
    ) -> None:
        file_data = {
            "name": "benchy.gcode",
            "gcodeAnalysis": {
                "estimatedPrintTime": 3600,
                "filament": {"tool0": {"length": 5000.0, "volume": 12.5}},
            },
        }
        client = _mock_client(
            connection_response=_success(connection_state_operational),
            printer_response=_success(printer_state_operational),
            file_info_response=_success(file_data),
        )
        result = preflight_check(client, file_on_server="benchy.gcode")
        assert "resources" in result
        assert result["resources"]["available"] is True

    def test_result_keys(
        self,
        connection_state_operational: Dict[str, Any],
        printer_state_operational: Dict[str, Any],
    ) -> None:
        client = _mock_client(
            connection_response=_success(connection_state_operational),
            printer_response=_success(printer_state_operational),
        )
        result = preflight_check(client)
        assert "ready" in result
        assert "printer" in result
        assert "temperatures" in result
        assert "summary" in result
