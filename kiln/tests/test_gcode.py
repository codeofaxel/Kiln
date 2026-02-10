"""Tests for the G-code safety validator (kiln.gcode).

Covers every safety rule category:
    - Valid simple commands
    - Temperature limit enforcement (hotend, bed, chamber)
    - Blocked commands (emergency stop, EEPROM, network, firmware)
    - Warning-level commands (homing, stepper disable, movement safety)
    - Comment stripping and whitespace handling
    - Empty / blank input
    - Mixed valid and invalid commands in a single batch
    - Case insensitivity and flexible spacing
"""

from __future__ import annotations

import pytest

from kiln.gcode import (
    GCodeValidationResult,
    validate_gcode,
    _strip_comment,
    _parse_command_word,
    _extract_param,
)


# ===================================================================
# Helpers
# ===================================================================

class TestStripComment:
    """Unit tests for the internal comment-stripping helper."""

    def test_no_comment(self) -> None:
        assert _strip_comment("G28") == "G28"

    def test_inline_comment(self) -> None:
        assert _strip_comment("G28 ; home all axes") == "G28"

    def test_comment_only(self) -> None:
        assert _strip_comment("; this is just a comment") == ""

    def test_leading_whitespace(self) -> None:
        assert _strip_comment("  G1 X10  ; move") == "G1 X10"

    def test_semicolon_at_start(self) -> None:
        assert _strip_comment(";G28") == ""


class TestParseCommandWord:
    """Unit tests for command-word extraction."""

    def test_standard(self) -> None:
        assert _parse_command_word("G28") == "G28"

    def test_with_params(self) -> None:
        assert _parse_command_word("M104 S200") == "M104"

    def test_lowercase(self) -> None:
        assert _parse_command_word("g28") == "G28"

    def test_space_between_letter_and_number(self) -> None:
        assert _parse_command_word("G 28") == "G28"

    def test_no_match(self) -> None:
        assert _parse_command_word("") is None

    def test_no_match_text(self) -> None:
        assert _parse_command_word("hello world") is None

    def test_leading_zeros(self) -> None:
        assert _parse_command_word("G01 X10") == "G1"

    def test_decimal_command(self) -> None:
        # Some firmware uses G29.1, G38.2, etc.
        assert _parse_command_word("G29.1") == "G29.1"


class TestExtractParam:
    """Unit tests for parameter extraction."""

    def test_present(self) -> None:
        assert _extract_param("M104 S200", "S") == 200.0

    def test_absent(self) -> None:
        assert _extract_param("M104", "S") is None

    def test_case_insensitive(self) -> None:
        assert _extract_param("g1 x10 y20 z5", "Z") == 5.0

    def test_negative_value(self) -> None:
        assert _extract_param("G1 Z-0.5 F300", "Z") == -0.5

    def test_float_value(self) -> None:
        assert _extract_param("G1 X12.345", "X") == pytest.approx(12.345)

    def test_multiple_same_letter(self) -> None:
        # Returns the first occurrence.
        assert _extract_param("G1 X10 X20", "X") == 10.0


# ===================================================================
# Valid simple commands
# ===================================================================

class TestValidSimpleCommands:
    """Commands that should pass validation cleanly."""

    def test_home(self) -> None:
        r = validate_gcode("G28")
        assert r.valid is True
        assert r.commands == ["G28"]
        assert r.errors == []
        assert r.blocked_commands == []
        # G28 does generate a warning, which is expected.
        assert len(r.warnings) == 1

    def test_linear_move(self) -> None:
        r = validate_gcode("G1 X10 Y10")
        assert r.valid is True
        assert r.commands == ["G1 X10 Y10"]
        assert r.errors == []
        assert r.warnings == []

    def test_hotend_temp_within_limit(self) -> None:
        r = validate_gcode("M104 S200")
        assert r.valid is True
        assert r.commands == ["M104 S200"]
        assert r.errors == []

    def test_bed_temp_within_limit(self) -> None:
        r = validate_gcode("M140 S60")
        assert r.valid is True
        assert r.commands == ["M140 S60"]
        assert r.errors == []

    def test_chamber_temp_within_limit(self) -> None:
        r = validate_gcode("M141 S50")
        assert r.valid is True
        assert r.commands == ["M141 S50"]

    def test_multiple_valid_commands(self) -> None:
        r = validate_gcode("G28\nG1 X10 Y10 Z0.2 F1200\nM104 S200")
        assert r.valid is True
        assert len(r.commands) == 3
        assert r.errors == []
        assert r.blocked_commands == []

    def test_hotend_at_exact_limit(self) -> None:
        r = validate_gcode("M104 S300")
        assert r.valid is True
        assert "M104 S300" in r.commands

    def test_bed_at_exact_limit(self) -> None:
        r = validate_gcode("M140 S130")
        assert r.valid is True

    def test_chamber_at_exact_limit(self) -> None:
        r = validate_gcode("M141 S80")
        assert r.valid is True

    def test_temp_zero_off(self) -> None:
        """Setting temperature to 0 (turn heater off) is always valid."""
        r = validate_gcode("M104 S0\nM140 S0\nM141 S0")
        assert r.valid is True
        assert len(r.commands) == 3

    def test_rapid_move(self) -> None:
        r = validate_gcode("G0 X50 Y50 Z10 F3000")
        assert r.valid is True

    def test_list_input(self) -> None:
        """validate_gcode accepts a list of strings."""
        r = validate_gcode(["G28", "M104 S200"])
        assert r.valid is True
        assert len(r.commands) == 2

    def test_m109_wait_within_limit(self) -> None:
        r = validate_gcode("M109 S250")
        assert r.valid is True

    def test_m190_wait_within_limit(self) -> None:
        r = validate_gcode("M190 S100")
        assert r.valid is True


# ===================================================================
# Temperature over limits (BLOCKING)
# ===================================================================

class TestTemperatureOverLimits:
    """Commands that exceed temperature safety limits must be blocked."""

    def test_hotend_over_limit(self) -> None:
        r = validate_gcode("M104 S350")
        assert r.valid is False
        assert len(r.errors) == 1
        assert "hotend" in r.errors[0].lower()
        assert "M104 S350" in r.blocked_commands

    def test_hotend_wait_over_limit(self) -> None:
        r = validate_gcode("M109 S301")
        assert r.valid is False
        assert len(r.errors) == 1
        assert "M109 S301" in r.blocked_commands

    def test_bed_over_limit(self) -> None:
        r = validate_gcode("M140 S150")
        assert r.valid is False
        assert "bed" in r.errors[0].lower()
        assert "M140 S150" in r.blocked_commands

    def test_bed_wait_over_limit(self) -> None:
        r = validate_gcode("M190 S131")
        assert r.valid is False
        assert "M190 S131" in r.blocked_commands

    def test_chamber_over_limit(self) -> None:
        r = validate_gcode("M141 S100")
        assert r.valid is False
        assert "chamber" in r.errors[0].lower()
        assert "M141 S100" in r.blocked_commands

    def test_hotend_just_over(self) -> None:
        r = validate_gcode("M104 S300.1")
        assert r.valid is False

    def test_bed_just_over(self) -> None:
        r = validate_gcode("M140 S130.1")
        assert r.valid is False

    def test_chamber_just_over(self) -> None:
        r = validate_gcode("M141 S80.1")
        assert r.valid is False

    def test_no_temp_param_is_ok(self) -> None:
        """M104 without an S parameter should not block (firmware default)."""
        r = validate_gcode("M104")
        assert r.valid is True


# ===================================================================
# Blocked commands
# ===================================================================

class TestBlockedCommands:
    """Commands that are unconditionally blocked."""

    def test_emergency_stop(self) -> None:
        r = validate_gcode("M112")
        assert r.valid is False
        assert "cancel_print" in r.errors[0]
        assert "M112" in r.blocked_commands

    def test_factory_reset(self) -> None:
        r = validate_gcode("M502")
        assert r.valid is False
        assert "M502" in r.blocked_commands

    def test_save_eeprom(self) -> None:
        r = validate_gcode("M500")
        assert r.valid is False
        assert "M500" in r.blocked_commands

    def test_load_eeprom(self) -> None:
        r = validate_gcode("M501")
        assert r.valid is False
        assert "M501" in r.blocked_commands

    def test_network_m552(self) -> None:
        r = validate_gcode("M552")
        assert r.valid is False
        assert "M552" in r.blocked_commands

    def test_network_m553(self) -> None:
        r = validate_gcode("M553")
        assert r.valid is False

    def test_network_m554(self) -> None:
        r = validate_gcode("M554")
        assert r.valid is False

    def test_firmware_update(self) -> None:
        r = validate_gcode("M997")
        assert r.valid is False
        assert "firmware" in r.errors[0].lower()

    def test_blocked_with_params(self) -> None:
        """Blocked commands are caught even when they carry parameters."""
        r = validate_gcode("M500 S1")
        assert r.valid is False
        assert "M500 S1" in r.blocked_commands


# ===================================================================
# Warning-level commands
# ===================================================================

class TestWarningCommands:
    """Commands that are allowed but produce warnings."""

    def test_home_warning(self) -> None:
        r = validate_gcode("G28")
        assert r.valid is True
        assert any("home" in w.lower() for w in r.warnings)

    def test_disable_steppers_m18(self) -> None:
        r = validate_gcode("M18")
        assert r.valid is True
        assert any("stepper" in w.lower() for w in r.warnings)
        assert any("shift" in w.lower() for w in r.warnings)

    def test_disable_steppers_m84(self) -> None:
        r = validate_gcode("M84")
        assert r.valid is True
        assert any("stepper" in w.lower() for w in r.warnings)

    def test_stepper_current(self) -> None:
        r = validate_gcode("M906 X800 Y800")
        assert r.valid is True
        assert any("stepper" in w.lower() or "current" in w.lower() for w in r.warnings)

    def test_z_below_bed(self) -> None:
        r = validate_gcode("G1 Z-1 F300")
        assert r.valid is True
        assert any("below" in w.lower() or "z" in w.lower() for w in r.warnings)

    def test_z_below_bed_g0(self) -> None:
        r = validate_gcode("G0 Z-0.5")
        assert r.valid is True
        assert any("below" in w.lower() for w in r.warnings)

    def test_high_feedrate(self) -> None:
        r = validate_gcode("G1 X100 F15000")
        assert r.valid is True
        assert any("feedrate" in w.lower() for w in r.warnings)

    def test_high_feedrate_g0(self) -> None:
        r = validate_gcode("G0 X100 Y100 F20000")
        assert r.valid is True
        assert any("feedrate" in w.lower() for w in r.warnings)

    def test_feedrate_at_limit_no_warning(self) -> None:
        """Feedrate exactly at 10000 should NOT trigger a warning."""
        r = validate_gcode("G1 X10 F10000")
        assert r.valid is True
        assert not any("feedrate" in w.lower() for w in r.warnings)

    def test_z_at_zero_no_warning(self) -> None:
        """Z exactly at 0 should NOT trigger a warning."""
        r = validate_gcode("G1 Z0 F300")
        assert r.valid is True
        assert not any("below" in w.lower() for w in r.warnings)

    def test_multiple_warnings(self) -> None:
        """A single line can generate multiple warnings."""
        r = validate_gcode("G1 Z-1 F15000")
        assert r.valid is True
        assert len(r.warnings) >= 2
        assert any("below" in w.lower() or "z" in w.lower() for w in r.warnings)
        assert any("feedrate" in w.lower() for w in r.warnings)


# ===================================================================
# Comment stripping
# ===================================================================

class TestCommentStripping:
    """Inline comments must be stripped before validation."""

    def test_inline_comment(self) -> None:
        r = validate_gcode("G28 ; home all")
        assert r.valid is True
        assert r.commands == ["G28"]

    def test_comment_only_line(self) -> None:
        r = validate_gcode("; just a comment")
        assert r.valid is True
        assert r.commands == []

    def test_mixed_comments_and_commands(self) -> None:
        r = validate_gcode("; header\nG28 ; home\n; footer")
        assert r.valid is True
        assert r.commands == ["G28"]


# ===================================================================
# Empty / blank input
# ===================================================================

class TestEmptyInput:
    """Empty or blank input should produce a valid, empty result."""

    def test_empty_string(self) -> None:
        r = validate_gcode("")
        assert r.valid is True
        assert r.commands == []
        assert r.warnings == []
        assert r.errors == []

    def test_whitespace_only(self) -> None:
        r = validate_gcode("   \n\n   \n")
        assert r.valid is True
        assert r.commands == []

    def test_empty_list(self) -> None:
        r = validate_gcode([])
        assert r.valid is True
        assert r.commands == []

    def test_list_of_empty_strings(self) -> None:
        r = validate_gcode(["", "  ", "\n"])
        assert r.valid is True
        assert r.commands == []


# ===================================================================
# Mixed valid and invalid
# ===================================================================

class TestMixedValidAndInvalid:
    """Batches containing both safe and unsafe commands."""

    def test_one_blocked_invalidates_batch(self) -> None:
        r = validate_gcode("G28\nM112\nG1 X10")
        assert r.valid is False
        # The valid commands should still be in .commands
        assert "G28" in r.commands
        assert "G1 X10" in r.commands
        # The blocked command should be recorded
        assert "M112" in r.blocked_commands
        assert len(r.errors) == 1

    def test_multiple_errors(self) -> None:
        r = validate_gcode("M104 S999\nM140 S999\nM112")
        assert r.valid is False
        assert len(r.errors) == 3
        assert len(r.blocked_commands) == 3
        assert r.commands == []  # all were blocked

    def test_warnings_dont_invalidate(self) -> None:
        r = validate_gcode("G28\nG1 Z-1 F15000\nM18")
        assert r.valid is True
        assert len(r.commands) == 3
        assert len(r.warnings) >= 4  # home + Z below bed + feedrate + stepper

    def test_mixed_block_and_warning(self) -> None:
        r = validate_gcode("G28\nM500\nG1 Z-1")
        assert r.valid is False
        assert "G28" in r.commands
        assert "G1 Z-1" in r.commands
        assert len(r.blocked_commands) == 1
        # Still has warnings from G28 and Z < 0
        assert len(r.warnings) >= 2


# ===================================================================
# Case insensitivity and flexible spacing
# ===================================================================

class TestCaseAndSpacing:
    """The parser should handle case and spacing variations."""

    def test_lowercase_command(self) -> None:
        r = validate_gcode("g28")
        assert r.valid is True
        assert len(r.commands) == 1

    def test_lowercase_blocked(self) -> None:
        r = validate_gcode("m112")
        assert r.valid is False

    def test_lowercase_temp(self) -> None:
        r = validate_gcode("m104 s350")
        assert r.valid is False
        assert len(r.errors) == 1

    def test_space_between_letter_and_number(self) -> None:
        r = validate_gcode("G 28")
        assert r.valid is True
        assert len(r.commands) == 1

    def test_mixed_case_params(self) -> None:
        r = validate_gcode("g1 x10 Y20 z0.2 f1200")
        assert r.valid is True
        assert len(r.commands) == 1

    def test_leading_trailing_whitespace(self) -> None:
        r = validate_gcode("  G28  \n  M104 S200  ")
        assert r.valid is True
        assert len(r.commands) == 2


# ===================================================================
# List input with embedded newlines
# ===================================================================

class TestListInput:
    """The list input path should split elements that contain newlines."""

    def test_list_with_newlines(self) -> None:
        r = validate_gcode(["G28\nG1 X10", "M104 S200"])
        assert r.valid is True
        assert len(r.commands) == 3

    def test_list_mixed_valid_invalid(self) -> None:
        r = validate_gcode(["G28", "M112", "M104 S200"])
        assert r.valid is False
        assert "G28" in r.commands
        assert "M104 S200" in r.commands
        assert "M112" in r.blocked_commands


# ===================================================================
# GCodeValidationResult dataclass
# ===================================================================

class TestGCodeValidationResult:
    """Verify the result dataclass defaults and structure."""

    def test_defaults(self) -> None:
        r = GCodeValidationResult()
        assert r.valid is True
        assert r.commands == []
        assert r.warnings == []
        assert r.errors == []
        assert r.blocked_commands == []

    def test_independent_instances(self) -> None:
        """Ensure mutable defaults don't leak between instances."""
        r1 = GCodeValidationResult()
        r2 = GCodeValidationResult()
        r1.commands.append("G28")
        assert r2.commands == []
