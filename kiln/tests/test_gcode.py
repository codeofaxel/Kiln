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
    _extract_param,
    _parse_command_word,
    _strip_comment,
    check_missing_temperatures,
    scan_gcode_file,
    validate_gcode,
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


# ===================================================================
# scan_gcode_file() — file-level streaming validator
# ===================================================================

class TestScanGcodeFile:
    """Tests for the streaming file scanner."""

    def test_clean_file(self, tmp_path) -> None:
        """A file with only safe commands should pass."""
        gcode = tmp_path / "clean.gcode"
        gcode.write_text("G28\nG1 X10 Y10 Z0.2 F1200\nM104 S200\n")
        result = scan_gcode_file(str(gcode))
        assert result.valid is True
        assert result.errors == []

    def test_blocked_command_at_line_1(self, tmp_path) -> None:
        """A blocked command on the first line should be caught."""
        gcode = tmp_path / "bad_start.gcode"
        gcode.write_text("M112\nG28\n")
        result = scan_gcode_file(str(gcode))
        assert result.valid is False
        assert len(result.blocked_commands) >= 1
        assert "M112" in result.blocked_commands[0]

    def test_blocked_command_deep_in_file(self, tmp_path) -> None:
        """A blocked command buried deep in the file should still be caught."""
        gcode = tmp_path / "deep_bad.gcode"
        lines = ["G1 X10 Y10 F600\n"] * 5000
        lines.append("M500\n")  # blocked: save to EEPROM
        lines.append("G1 X20 Y20 F600\n")
        gcode.write_text("".join(lines))
        result = scan_gcode_file(str(gcode))
        assert result.valid is False
        assert any("M500" in e for e in result.errors)

    def test_temperature_over_limit(self, tmp_path) -> None:
        """Temperature exceeding generic max should fail."""
        gcode = tmp_path / "hot.gcode"
        gcode.write_text("G28\nM104 S400\n")
        result = scan_gcode_file(str(gcode))
        assert result.valid is False
        assert any("400" in e for e in result.errors)

    def test_empty_file(self, tmp_path) -> None:
        """An empty file should pass (no dangerous commands)."""
        gcode = tmp_path / "empty.gcode"
        gcode.write_text("")
        result = scan_gcode_file(str(gcode))
        assert result.valid is True

    def test_comments_only_file(self, tmp_path) -> None:
        """A file with only comments should pass."""
        gcode = tmp_path / "comments.gcode"
        gcode.write_text("; generated by slicer\n; layer 1\n")
        result = scan_gcode_file(str(gcode))
        assert result.valid is True

    def test_file_not_found(self) -> None:
        """Non-existent file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            scan_gcode_file("/nonexistent/path/test.gcode")

    def test_warnings_capped(self, tmp_path) -> None:
        """Warnings should be capped at the limit to prevent memory growth."""
        gcode = tmp_path / "many_warnings.gcode"
        # Z below bed plane generates a warning per line
        lines = [f"G1 Z-{i} F600\n" for i in range(1, 200)]
        gcode.write_text("".join(lines))
        result = scan_gcode_file(str(gcode))
        # Should have at most ~51 warnings (50 + cap message)
        assert len(result.warnings) <= 52

    def test_fail_fast_on_blocked(self, tmp_path) -> None:
        """Scanner should stop reading after first blocked command."""
        gcode = tmp_path / "fail_fast.gcode"
        # M112 at line 3, then 10000 more lines that should never be read
        lines = ["G28\n", "G1 X10 F600\n", "M112\n"]
        lines.extend(["G1 X20 F600\n"] * 10000)
        gcode.write_text("".join(lines))
        result = scan_gcode_file(str(gcode))
        assert result.valid is False
        # Should have stopped early — only a few commands parsed
        assert len(result.commands) <= 3

    def test_n_word_line_numbers(self, tmp_path) -> None:
        """G-code with N-word line numbers should still validate the actual command."""
        gcode = tmp_path / "nword.gcode"
        gcode.write_text("N10 G28\nN20 M104 S200\nN30 M112\n")
        result = scan_gcode_file(str(gcode))
        assert result.valid is False
        assert any("M112" in e for e in result.errors)


# ===================================================================
# Missing temperature detection
# ===================================================================


class TestCheckMissingTemperatures:
    """Tests for check_missing_temperatures() and scan_gcode_file integration.

    Covers:
    - Both hotend and bed temps present → no warnings
    - Missing hotend temp with extrusion → warning
    - Missing bed temp with extrusion → warning
    - Missing both temps with extrusion → two warnings
    - No extrusion commands → no warnings (movement-only script)
    - M104 S0 (zero temp) does not count as setting temp
    - Comments with temperature commands don't count
    - Integration with scan_gcode_file
    """

    def test_both_temps_present_no_warnings(self) -> None:
        gcode = "G28\nM104 S200\nM140 S60\nG1 X10 E1 F600"
        warnings = check_missing_temperatures(gcode)
        assert warnings == []

    def test_m109_and_m190_also_accepted(self) -> None:
        gcode = "G28\nM109 S210\nM190 S65\nG1 X10 E1 F600"
        warnings = check_missing_temperatures(gcode)
        assert warnings == []

    def test_missing_hotend_warns(self) -> None:
        gcode = "G28\nM140 S60\nG1 X10 E1 F600"
        warnings = check_missing_temperatures(gcode)
        assert len(warnings) == 1
        assert "hotend" in warnings[0].lower()
        assert "M104" in warnings[0] or "M109" in warnings[0]

    def test_missing_bed_warns(self) -> None:
        gcode = "G28\nM104 S200\nG1 X10 E1 F600"
        warnings = check_missing_temperatures(gcode)
        assert len(warnings) == 1
        assert "bed" in warnings[0].lower()
        assert "M140" in warnings[0] or "M190" in warnings[0]

    def test_missing_both_warns_twice(self) -> None:
        gcode = "G28\nG1 X10 E1 F600\nG1 X20 E2 F600"
        warnings = check_missing_temperatures(gcode)
        assert len(warnings) == 2
        messages = " ".join(warnings).lower()
        assert "hotend" in messages
        assert "bed" in messages

    def test_no_extrusion_no_warnings(self) -> None:
        """Movement-only gcode (no E parameter) should not trigger warnings."""
        gcode = "G28\nG1 X10 Y10 Z5 F600\nG1 X20 Y20 F1200"
        warnings = check_missing_temperatures(gcode)
        assert warnings == []

    def test_zero_temp_does_not_count(self) -> None:
        """M104 S0 turns off the hotend -- not a real temp setting."""
        gcode = "G28\nM104 S0\nM140 S0\nG1 X10 E1 F600"
        warnings = check_missing_temperatures(gcode)
        assert len(warnings) == 2

    def test_comment_temps_not_counted(self) -> None:
        """Temperature commands inside comments should be ignored."""
        gcode = "; M104 S200\n; M140 S60\nG28\nG1 X10 E1 F600"
        warnings = check_missing_temperatures(gcode)
        assert len(warnings) == 2

    def test_mixed_case_commands(self) -> None:
        gcode = "g28\nm104 s200\nm140 s60\ng1 x10 e1 f600"
        warnings = check_missing_temperatures(gcode)
        assert warnings == []

    def test_list_input(self) -> None:
        lines = ["G28", "M109 S215", "M190 S70", "G1 X10 E1 F600"]
        warnings = check_missing_temperatures(lines)
        assert warnings == []

    def test_empty_input(self) -> None:
        warnings = check_missing_temperatures("")
        assert warnings == []

    def test_negative_extrusion_not_counted(self) -> None:
        """Retraction (E < 0) should not count as extrusion."""
        gcode = "G28\nG1 E-2 F1800\nG1 X10 F600"
        warnings = check_missing_temperatures(gcode)
        assert warnings == []


class TestScanGcodeFileMissingTemps:
    """Integration: scan_gcode_file should include missing temp warnings."""

    def test_scan_warns_missing_temps(self, tmp_path) -> None:
        gcode = tmp_path / "no_temps.gcode"
        gcode.write_text("G28\nG1 X10 E1 F600\nG1 X20 E2 F600\n")
        result = scan_gcode_file(str(gcode))
        assert result.valid is True  # Warning only, not blocking
        assert len(result.warnings) >= 2
        warning_text = " ".join(result.warnings).lower()
        assert "hotend" in warning_text
        assert "bed" in warning_text

    def test_scan_no_warning_when_temps_present(self, tmp_path) -> None:
        gcode = tmp_path / "with_temps.gcode"
        gcode.write_text("G28\nM104 S200\nM140 S60\nG1 X10 E1 F600\n")
        result = scan_gcode_file(str(gcode))
        assert result.valid is True
        # Should have no missing-temp warnings
        temp_warns = [w for w in result.warnings if "hotend" in w.lower() or "bed temperature" in w.lower()]
        assert temp_warns == []

    def test_scan_no_warning_for_movement_only(self, tmp_path) -> None:
        gcode = tmp_path / "moves_only.gcode"
        gcode.write_text("G28\nG1 X10 Y10 Z5 F600\n")
        result = scan_gcode_file(str(gcode))
        assert result.valid is True
        temp_warns = [w for w in result.warnings if "hotend" in w.lower() or "bed temperature" in w.lower()]
        assert temp_warns == []


# ===================================================================
# Axis words, spelled the way slicers spell them
# ===================================================================


class TestAxisWords:
    """The one number spelling every G-code reader shares (``axis_value`` /
    ``has_axis_word``).  OrcaSlicer and Bambu Studio write no leading zero
    on most lines; a reader that wanted a digit first read a quarter of a
    plate."""

    @pytest.mark.parametrize(
        ("line", "letter", "expected"),
        [
            ("G1 X158.922 Y99.462 E.04805", "E", 0.04805),
            ("G1 E-.8 F1800", "E", -0.8),
            ("G1 E+.5", "E", 0.5),
            ("G1 X1. Y2", "X", 1.0),
            ("G1 Z.2 F24000", "Z", 0.2),
            ("G1 X10 Y20 E1.5", "E", 1.5),
            ("G1 E12", "E", 12.0),
            ("g1 x5 e.25", "E", 0.25),
            ("G1X10Y20E.5", "E", 0.5),
            ("G1 X 10", "X", 10.0),
        ],
    )
    def test_reads_every_spelling(self, line, letter, expected):
        from kiln.gcode import axis_value

        assert axis_value(line, letter) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("line", "letter"),
        [
            ("G1 X5 ; E.5 in a comment", "E"),
            ("M117 NAME=BEE.5", "E"),
            ("G28 X Y", "X"),
            ("G1 X5 F1800", "E"),
            ("", "E"),
        ],
    )
    def test_reads_nothing_that_is_not_a_word(self, line, letter):
        from kiln.gcode import axis_value

        assert axis_value(line, letter) is None

    def test_has_axis_word_sees_slicer_spelling(self):
        from kiln.gcode import has_axis_word

        assert has_axis_word("G1 Z.4 E.01", "XYE") is True
        assert has_axis_word("G1 Z.4", "XYE") is False
        assert has_axis_word("G1 Z.4 ; E.01", "XYE") is False

    def test_extract_param_takes_the_same_spelling(self):
        assert _extract_param("G1 X158.922 E.04805", "E") == pytest.approx(0.04805)
        assert _extract_param("G1 X1.", "X") == pytest.approx(1.0)


# ===================================================================
# Filament: the one counter and the one reader every door uses
# ===================================================================


class TestExtrudedMmPerTool:
    """Counted the way slicers count their own total: positive E on a move
    that travels, per selected tool; retract, unretract and prime are not
    plastic; Bambu's pseudo-tools keep the current filament."""

    def _count(self, text):
        from kiln.gcode import extruded_mm_per_tool

        return extruded_mm_per_tool(text)

    def test_orca_spelling_and_wipe_retract(self):
        text = (
            "M83\n"
            "G1 E.8 F1800\n"
            "G1 X158.922 Y99.462 E.04805\n"
            "G1 X160.875 Y99.53 E.06127\n"
            "G1 X161 Y99.6 E-.32 ; wipe\n"
            "G1 E-.48 F1800\n"
            "G1 E.8 F1800\n"
            "G1 X170 Y100 E2.03825\n"
        )
        assert self._count(text) == [pytest.approx(0.04805 + 0.06127 + 2.03825)]

    def test_absolute_e_with_g92_reset(self):
        assert self._count("M82\nG1 X1 E5\nG1 X2 E12\nG92 E0\nG1 X3 E3\n") == [pytest.approx(15.0)]

    def test_relative_moves_advance_the_absolute_position(self):
        # Firmware keeps counting through relative moves: after M83 +2 +3 the
        # extruder sits at 15, and an absolute E12 after M82 is a retract.
        text = "G1 X10 E5\nG1 X20 E10\nM83\nG1 X30 E2\nG1 X40 E3\nM82\nG1 X50 E12\n"
        assert self._count(text) == [pytest.approx(15.0)]

    def test_per_tool_and_pseudo_tools_keep_the_current_filament(self):
        # Bambu's start sequence purges under T1000 and unloads under T255;
        # the slicer counts those moves against the filament already
        # selected, and so does Kiln.  A reader that dropped them read a
        # Bambu Studio plate at 9% of its filament.
        text = (
            "M83\nT0\nG1 X1 Y1 E100\nT1000\nG1 X240 E15 F4800\nG1 E7\n"
            "T1\nG1 X2 Y2 E50\nT0\nG1 X3 Y3 E25\nT255\nG1 X65 F12000\n"
        )
        assert self._count(text) == [pytest.approx(140.0), pytest.approx(50.0)]

    def test_arcs_and_compact_spelling(self):
        assert self._count(["M83", "G1X10Y10E.5", "G01 X20 Y10 E.5", "G2 X0 Y0 I-5 J0 E1"]) == [pytest.approx(2.0)]

    def test_an_arc_mode_subcode_does_not_change_how_e_is_read(self):
        # G91.1 sets arc centres relative on firmware that has it; read as
        # G91 it would turn absolute E relative and count 6 as a 6 mm move.
        assert self._count("M82\nG1 X1 E5\nG91.1\nG1 X2 E6\n") == [pytest.approx(6.0)]

    def test_nothing_extruded_is_an_empty_list(self):
        assert self._count("G28\nG1 X10 Y10\nM83\nG1 E5\n") == []

    def test_the_cost_tool_and_the_printer_screen_count_the_same_way(self):
        from kiln.cost_estimator import CostEstimator
        from kiln.printers.bambu_3mf import filament_usage_from_gcode

        text = "M83\nT0\nG1 X1 Y1 E400\nG1 E-.8\nG1 E.8\nG1 X2 Y2 E600\nT1\nG1 X3 Y3 E50\n"
        usage = filament_usage_from_gcode(text)
        assert usage.source == "e_moves"
        assert sum(usage.mm) == pytest.approx(CostEstimator()._parse_extrusion(text.splitlines()))
        assert usage.mm == (pytest.approx(1000.0), pytest.approx(50.0))


class TestSlicerFilamentTotals:
    def _read(self, text):
        from kiln.gcode import slicer_filament_totals

        return slicer_filament_totals(text)

    def test_orca_lists_one_value_per_extruder(self):
        t = self._read(
            "; filament used [mm] = 11040.26, 584.30\n"
            "; filament used [cm3] = 26.55, 1.41\n"
            "; filament used [g] = 32.93, 1.74\n"
            "; total filament used [g] = 34.67\n"
        )
        assert t.mm == (11040.26, 584.30)
        assert t.grams == (32.93, 1.74)
        assert t.cm3 == (26.55, 1.41)
        assert t.total_g == 34.67
        assert t.total_mm == pytest.approx(11624.56)
        assert t.weight_g == pytest.approx(34.67)

    def test_bambu_studio_writes_colons(self):
        t = self._read("; total filament length [mm] : 1227.58\n; total filament weight [g] : 3.66\n")
        assert t.mm == (1227.58,) and t.grams == (3.66,)

    def test_cura_and_simplify3d_write_metres(self):
        assert self._read(";Filament used: 4.523m\n").mm == (4523.0,)
        assert self._read("; Filament length: 4523.4 mm (4.52 m)\n").mm == (4523.4,)
        assert self._read("; Filament length: 4.523 m\n").mm == (4523.0,)

    def test_a_zero_weight_falls_back_to_the_one_number_total(self):
        t = self._read("; filament used [g] = 0\n; total filament used [g] = 34.67\n")
        assert t.grams == (0.0,) and t.weight_g == pytest.approx(34.67)
        assert self._read("; filament used [g] = 0\n; total filament used [g] = 0.00\n").weight_g is None

    def test_slic3r_and_a_written_unit_decide_the_kind(self):
        t = self._read("; filament used = 1034.5mm (7.4cm3)\n")
        assert t.mm == (1034.5,) and t.grams == ()
        assert self._read("; filament_used = 123\n").mm == (123.0,)
        grams = self._read("; filament used: 12.3g\n")
        assert grams.grams == (12.3,) and grams.mm == ()
        assert self._read("; filament used [mm] = 12g\n").mm == ()
        assert self._read("; filament used = 1.2m, 5g\n").mm == ()

    def test_an_empty_value_never_reads_the_next_line(self):
        assert self._read("; filament used [mm] = \n; filament used [g] = 3\n").mm == ()

    def test_trailing_text_and_move_lines_are_ignored(self):
        assert self._read("; filament used [mm] = 1234.56 (model only)\n").mm == (1234.56,)
        assert self._read("G1 X1 E.5 ; filament used [mm] = 9\n").mm == ()
        assert self._read("").mm == ()


# ===================================================================
# The slicer's own print time and layer count
# ===================================================================


class TestParseDuration:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [
            ("1h 42m 30s", 6150), ("2h 30m", 9000), ("42m 30s", 2550), ("30s", 30), ("2h", 7200),
            ("1d 2h 30m 15s", 95415), ("6150", 6150), ("0", 0), ("1 hours 42 minutes", 6120),
            ("1 hour 1 minute", 3660), ("100h", 360000), ("  1h 30m  ", 5400),
            ("1h50m32s", 6632), ("12 mins", 720),
        ],
    )
    def test_every_way_slicers_write_one(self, text, seconds):
        from kiln.gcode import parse_duration

        assert parse_duration(text) == seconds

    @pytest.mark.parametrize("text", ["", "   ", "not a time"])
    def test_nothing_readable_is_none(self, text):
        from kiln.gcode import parse_duration

        assert parse_duration(text) is None


class TestSlicerPrintTime:
    def _seconds(self, text):
        from kiln.gcode import slicer_print_time

        printed = slicer_print_time(text)
        return printed.seconds if printed else None

    def test_bambu_studios_total_not_its_model_time(self):
        assert self._seconds("; model printing time: 14m 49s; total estimated time: 21m 5s\n") == 1265

    def test_the_normal_mode_time_wins_over_a_slower_silent_one_after_it(self):
        text = (
            "; estimated printing time (normal mode) = 16m 32s\n"
            "; estimated printing time (silent mode) = 17m 33s\n"
        )
        assert self._seconds(text) == 992

    def test_a_first_layer_time_is_never_the_prints_time(self):
        text = "; estimated first layer printing time (normal mode) = 29s\n; estimated printing time (normal mode) = 1h 50m 32s\n"
        assert self._seconds(text) == 6632

    def test_curas_spellings_and_not_its_per_layer_time(self):
        assert self._seconds(";TIME_ELAPSED:12.5\n;TIME:6632\n") == 6632
        assert self._seconds(";PRINT.TIME:6632\n") == 6632

    def test_simplify3d_and_a_day_long_print(self):
        assert self._seconds(";   Build time: 1 hours 42 minutes\n") == 6120
        assert self._seconds("; estimated printing time (normal mode) = 1d 2h 3m 4s\n") == 93784

    def test_the_words_come_back_as_written(self):
        from kiln.gcode import slicer_print_time

        assert slicer_print_time("; total estimated time: 21m 5s\n").as_written == "21m 5s"

    def test_an_empty_value_never_reads_the_next_line(self):
        text = "; estimated printing time (normal mode) = \n; estimated first layer printing time (normal mode) = 29s\n"
        assert self._seconds(text) is None

    def test_a_move_line_or_nothing_is_none(self):
        assert self._seconds("G1 X10 Y10\nTIME:9999\n") is None
        assert self._seconds("") is None


class TestSlicerLayerCount:
    @pytest.mark.parametrize(
        ("text", "layers"),
        [
            ("; total layer number: 225\n", 225),
            ("; total layers count = 225\n", 225),
            (";LAYER_COUNT:150\n;LAYER:0\n", 150),
        ],
    )
    def test_every_slicer_that_writes_one(self, text, layers):
        from kiln.gcode import slicer_layer_count

        assert slicer_layer_count(text) == layers

    def test_a_setting_or_a_layer_marker_is_not_the_total(self):
        from kiln.gcode import slicer_layer_count

        assert slicer_layer_count("; interlocking_beam_layer_count = 2\n;LAYER:5\n") is None

