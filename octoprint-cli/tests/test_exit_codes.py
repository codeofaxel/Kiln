"""Tests for octoprint_cli.exit_codes."""

from __future__ import annotations

import pytest

from octoprint_cli.exit_codes import (
    ERROR_CODE_MAP,
    FILE_ERROR,
    OTHER_ERROR,
    PRINTER_BUSY,
    PRINTER_OFFLINE,
    SUCCESS,
    exit_code_for,
)


# ===================================================================
# Constant values
# ===================================================================


class TestExitCodeConstants:
    """Verify the exit code integer values are stable."""

    def test_success_is_zero(self) -> None:
        assert SUCCESS == 0

    def test_printer_offline_is_one(self) -> None:
        assert PRINTER_OFFLINE == 1

    def test_file_error_is_two(self) -> None:
        assert FILE_ERROR == 2

    def test_printer_busy_is_three(self) -> None:
        assert PRINTER_BUSY == 3

    def test_other_error_is_four(self) -> None:
        assert OTHER_ERROR == 4

    def test_all_codes_are_distinct(self) -> None:
        codes = [SUCCESS, PRINTER_OFFLINE, FILE_ERROR, PRINTER_BUSY, OTHER_ERROR]
        assert len(codes) == len(set(codes))


# ===================================================================
# ERROR_CODE_MAP
# ===================================================================


class TestErrorCodeMap:
    """Verify the mapping from error code strings to exit codes."""

    def test_connection_error_maps_to_printer_offline(self) -> None:
        assert ERROR_CODE_MAP["CONNECTION_ERROR"] == PRINTER_OFFLINE

    def test_timeout_maps_to_printer_offline(self) -> None:
        assert ERROR_CODE_MAP["TIMEOUT"] == PRINTER_OFFLINE

    def test_auth_error_maps_to_other(self) -> None:
        assert ERROR_CODE_MAP["AUTH_ERROR"] == OTHER_ERROR

    def test_not_found_maps_to_file_error(self) -> None:
        assert ERROR_CODE_MAP["NOT_FOUND"] == FILE_ERROR

    def test_conflict_maps_to_printer_busy(self) -> None:
        assert ERROR_CODE_MAP["CONFLICT"] == PRINTER_BUSY

    def test_unsupported_file_type_maps_to_file_error(self) -> None:
        assert ERROR_CODE_MAP["UNSUPPORTED_FILE_TYPE"] == FILE_ERROR

    def test_server_error_maps_to_other(self) -> None:
        assert ERROR_CODE_MAP["SERVER_ERROR"] == OTHER_ERROR

    def test_file_not_found_maps_to_file_error(self) -> None:
        assert ERROR_CODE_MAP["FILE_NOT_FOUND"] == FILE_ERROR

    def test_file_too_large_maps_to_file_error(self) -> None:
        assert ERROR_CODE_MAP["FILE_TOO_LARGE"] == FILE_ERROR

    def test_invalid_file_type_maps_to_file_error(self) -> None:
        assert ERROR_CODE_MAP["INVALID_FILE_TYPE"] == FILE_ERROR

    def test_printer_not_ready_maps_to_printer_offline(self) -> None:
        assert ERROR_CODE_MAP["PRINTER_NOT_READY"] == PRINTER_OFFLINE

    def test_printer_busy_maps_to_printer_busy(self) -> None:
        assert ERROR_CODE_MAP["PRINTER_BUSY"] == PRINTER_BUSY

    def test_validation_error_maps_to_other(self) -> None:
        assert ERROR_CODE_MAP["VALIDATION_ERROR"] == OTHER_ERROR


# ===================================================================
# exit_code_for
# ===================================================================


class TestExitCodeFor:
    """Tests for exit_code_for()."""

    @pytest.mark.parametrize(
        "error_code,expected_exit_code",
        [
            ("CONNECTION_ERROR", PRINTER_OFFLINE),
            ("TIMEOUT", PRINTER_OFFLINE),
            ("AUTH_ERROR", OTHER_ERROR),
            ("NOT_FOUND", FILE_ERROR),
            ("CONFLICT", PRINTER_BUSY),
            ("UNSUPPORTED_FILE_TYPE", FILE_ERROR),
            ("SERVER_ERROR", OTHER_ERROR),
            ("FILE_NOT_FOUND", FILE_ERROR),
            ("FILE_TOO_LARGE", FILE_ERROR),
            ("INVALID_FILE_TYPE", FILE_ERROR),
            ("PRINTER_NOT_READY", PRINTER_OFFLINE),
            ("PRINTER_BUSY", PRINTER_BUSY),
            ("VALIDATION_ERROR", OTHER_ERROR),
        ],
    )
    def test_known_error_codes(self, error_code: str, expected_exit_code: int) -> None:
        assert exit_code_for(error_code) == expected_exit_code

    def test_unknown_code_returns_other_error(self) -> None:
        assert exit_code_for("COMPLETELY_UNKNOWN_ERROR") == OTHER_ERROR

    def test_empty_string_returns_other_error(self) -> None:
        assert exit_code_for("") == OTHER_ERROR

    def test_case_sensitive(self) -> None:
        """Error codes are case-sensitive; lowercase should fall back to OTHER_ERROR."""
        assert exit_code_for("connection_error") == OTHER_ERROR
        assert exit_code_for("timeout") == OTHER_ERROR
