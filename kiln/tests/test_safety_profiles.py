"""Tests for safety profiles and printer-specific G-code validation.

Covers:
    - SafetyProfile dataclass field verification
    - get_profile() for known printers (ender3, bambu_x1c)
    - get_profile() fallback to default for unknown printers
    - get_profile() normalization (case, hyphens, whitespace)
    - list_profiles() contents
    - get_all_profiles() dict structure
    - profile_to_dict() serialization roundtrip
    - JSON data file validity (all profiles have required fields)
    - validate_gcode_for_printer() with printer-specific temperature limits
    - validate_gcode_for_printer() with printer-specific feedrate limits
    - validate_gcode_for_printer() blocked commands are still blocked
    - validate_gcode_for_printer() with unknown printer (default fallback)
    - Error messages include printer display name
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln.safety_profiles import (
    SafetyProfile,
    get_profile,
    list_profiles,
    get_all_profiles,
    profile_to_dict,
    _DATA_FILE,
    _cache,
    _loaded,
)
from kiln.gcode import validate_gcode_for_printer, GCodeValidationResult


# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture(autouse=True)
def _reset_profile_cache():
    """Reset the singleton cache before each test to ensure isolation."""
    import kiln.safety_profiles as mod
    mod._cache.clear()
    mod._loaded = False
    yield
    mod._cache.clear()
    mod._loaded = False


# ===================================================================
# SafetyProfile dataclass
# ===================================================================

class TestSafetyProfileDataclass:
    """Verify SafetyProfile fields and immutability."""

    def test_all_fields_present(self) -> None:
        profile = SafetyProfile(
            id="test",
            display_name="Test Printer",
            max_hotend_temp=250.0,
            max_bed_temp=100.0,
            max_chamber_temp=50.0,
            max_feedrate=10000.0,
            min_safe_z=0.0,
            max_volumetric_flow=15.0,
            build_volume=[220, 220, 250],
            notes="Test notes",
        )
        assert profile.id == "test"
        assert profile.display_name == "Test Printer"
        assert profile.max_hotend_temp == 250.0
        assert profile.max_bed_temp == 100.0
        assert profile.max_chamber_temp == 50.0
        assert profile.max_feedrate == 10000.0
        assert profile.min_safe_z == 0.0
        assert profile.max_volumetric_flow == 15.0
        assert profile.build_volume == [220, 220, 250]
        assert profile.notes == "Test notes"

    def test_optional_fields_none(self) -> None:
        profile = SafetyProfile(
            id="basic",
            display_name="Basic",
            max_hotend_temp=260.0,
            max_bed_temp=110.0,
            max_chamber_temp=None,
            max_feedrate=7500.0,
            min_safe_z=0.0,
            max_volumetric_flow=None,
            build_volume=None,
            notes="",
        )
        assert profile.max_chamber_temp is None
        assert profile.max_volumetric_flow is None
        assert profile.build_volume is None

    def test_frozen_dataclass(self) -> None:
        profile = SafetyProfile(
            id="frozen",
            display_name="Frozen",
            max_hotend_temp=300.0,
            max_bed_temp=130.0,
            max_chamber_temp=None,
            max_feedrate=10000.0,
            min_safe_z=0.0,
            max_volumetric_flow=None,
            build_volume=None,
            notes="",
        )
        with pytest.raises(AttributeError):
            profile.max_hotend_temp = 999.0  # type: ignore[misc]


# ===================================================================
# get_profile()
# ===================================================================

class TestGetProfile:
    """Tests for get_profile() loading and lookup."""

    def test_ender3_limits(self) -> None:
        profile = get_profile("ender3")
        assert profile.id == "ender3"
        assert profile.max_hotend_temp == 260.0
        assert profile.max_bed_temp == 110.0
        assert profile.max_chamber_temp is None
        assert profile.max_feedrate == 7500.0
        assert profile.display_name == "Creality Ender 3 / Ender 3 Pro / Ender 3 V2"

    def test_bambu_x1c_limits(self) -> None:
        profile = get_profile("bambu_x1c")
        assert profile.id == "bambu_x1c"
        assert profile.max_hotend_temp == 300.0
        assert profile.max_bed_temp == 120.0
        assert profile.max_chamber_temp == 60.0
        assert profile.max_feedrate == 30000.0
        assert profile.display_name == "Bambu Lab X1 Carbon"

    def test_default_profile(self) -> None:
        profile = get_profile("default")
        assert profile.id == "default"
        assert profile.max_hotend_temp == 300.0
        assert profile.max_bed_temp == 130.0
        assert profile.max_chamber_temp == 80.0
        assert profile.max_feedrate == 10000.0
        assert profile.display_name == "Generic / Unknown Printer"

    def test_nonexistent_falls_back_to_default(self) -> None:
        profile = get_profile("nonexistent_printer_xyz")
        assert profile.id == "default"
        assert profile.display_name == "Generic / Unknown Printer"

    def test_normalization_case_insensitive(self) -> None:
        """'BAMBU_X1C' should match 'bambu_x1c'."""
        profile = get_profile("BAMBU_X1C")
        assert profile.id == "bambu_x1c"

    def test_normalization_hyphens_to_underscores(self) -> None:
        """'Ender-3' normalises to 'ender_3', which doesn't directly match
        'ender3', so it falls back to default.  Direct key 'ender3' works."""
        # The hyphen-to-underscore conversion yields "ender_3" which has no
        # exact or prefix match to "ender3", so fallback applies.
        profile = get_profile("Ender-3")
        assert profile.id == "default"

        # But the raw key "ender3" works without hyphens.
        profile2 = get_profile("ender3")
        assert profile2.id == "ender3"

    def test_normalization_mixed_case_and_hyphens(self) -> None:
        """'Bambu-X1C' normalises to 'bambu_x1c'."""
        profile = get_profile("Bambu-X1C")
        assert profile.id == "bambu_x1c"

    def test_normalization_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace should be stripped."""
        profile = get_profile("  ender3  ")
        assert profile.id == "ender3"

    def test_prusa_mk4(self) -> None:
        profile = get_profile("prusa_mk4")
        assert profile.max_hotend_temp == 300.0
        assert profile.max_bed_temp == 120.0
        assert profile.max_feedrate == 15000.0


# ===================================================================
# list_profiles()
# ===================================================================

class TestListProfiles:
    """Tests for list_profiles()."""

    def test_returns_list(self) -> None:
        result = list_profiles()
        assert isinstance(result, list)

    def test_includes_known_profiles(self) -> None:
        result = list_profiles()
        assert "default" in result
        assert "ender3" in result
        assert "bambu_x1c" in result

    def test_sorted_alphabetically(self) -> None:
        result = list_profiles()
        assert result == sorted(result)

    def test_not_empty(self) -> None:
        result = list_profiles()
        assert len(result) > 3


# ===================================================================
# get_all_profiles()
# ===================================================================

class TestGetAllProfiles:
    """Tests for get_all_profiles()."""

    def test_returns_dict(self) -> None:
        result = get_all_profiles()
        assert isinstance(result, dict)

    def test_values_are_safety_profiles(self) -> None:
        result = get_all_profiles()
        for key, profile in result.items():
            assert isinstance(profile, SafetyProfile)
            assert profile.id == key

    def test_contains_default(self) -> None:
        result = get_all_profiles()
        assert "default" in result

    def test_returns_copy(self) -> None:
        """Modifying the returned dict should not affect the cache."""
        result = get_all_profiles()
        result.pop("default", None)
        # Re-fetch; the cache should still have "default"
        result2 = get_all_profiles()
        assert "default" in result2


# ===================================================================
# profile_to_dict()
# ===================================================================

class TestProfileToDict:
    """Tests for profile_to_dict() serialization."""

    def test_roundtrip_fields(self) -> None:
        profile = get_profile("ender3")
        d = profile_to_dict(profile)
        assert d["id"] == "ender3"
        assert d["display_name"] == profile.display_name
        assert d["max_hotend_temp"] == 260.0
        assert d["max_bed_temp"] == 110.0
        assert d["max_chamber_temp"] is None
        assert d["max_feedrate"] == 7500.0
        assert d["min_safe_z"] == 0.0
        assert d["build_volume"] == [220, 220, 250]
        assert isinstance(d["notes"], str)

    def test_all_keys_present(self) -> None:
        profile = get_profile("bambu_x1c")
        d = profile_to_dict(profile)
        expected_keys = {
            "id", "display_name", "max_hotend_temp", "max_bed_temp",
            "max_chamber_temp", "max_feedrate", "min_safe_z",
            "max_volumetric_flow", "build_volume", "notes",
        }
        assert set(d.keys()) == expected_keys

    def test_json_serializable(self) -> None:
        profile = get_profile("ender3")
        d = profile_to_dict(profile)
        # Should not raise
        serialized = json.dumps(d)
        assert isinstance(serialized, str)

    def test_dict_values_match_profile(self) -> None:
        profile = get_profile("bambu_x1c")
        d = profile_to_dict(profile)
        assert d["max_hotend_temp"] == profile.max_hotend_temp
        assert d["max_bed_temp"] == profile.max_bed_temp
        assert d["max_chamber_temp"] == profile.max_chamber_temp
        assert d["max_feedrate"] == profile.max_feedrate
        assert d["max_volumetric_flow"] == profile.max_volumetric_flow


# ===================================================================
# JSON file validity
# ===================================================================

class TestJSONFileValidity:
    """Validate the bundled safety_profiles.json data file."""

    def test_file_exists(self) -> None:
        assert _DATA_FILE.exists(), f"Safety profiles JSON not found at {_DATA_FILE}"

    def test_valid_json(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)

    def test_has_meta_key(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        assert "_meta" in raw

    def test_has_default_profile(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        assert "default" in raw

    def test_all_profiles_have_required_fields(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        required_fields = {"display_name", "max_hotend_temp", "max_bed_temp"}
        for key, data in raw.items():
            if key == "_meta":
                continue
            for field in required_fields:
                assert field in data, (
                    f"Profile '{key}' is missing required field '{field}'"
                )

    def test_temperature_values_are_numeric(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in raw.items():
            if key == "_meta":
                continue
            assert isinstance(data["max_hotend_temp"], (int, float)), (
                f"Profile '{key}': max_hotend_temp is not numeric"
            )
            assert isinstance(data["max_bed_temp"], (int, float)), (
                f"Profile '{key}': max_bed_temp is not numeric"
            )

    def test_temperature_values_are_positive(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in raw.items():
            if key == "_meta":
                continue
            assert data["max_hotend_temp"] > 0, (
                f"Profile '{key}': max_hotend_temp must be positive"
            )
            assert data["max_bed_temp"] > 0, (
                f"Profile '{key}': max_bed_temp must be positive"
            )


# ===================================================================
# validate_gcode_for_printer() -- temperature limits
# ===================================================================

class TestValidateGcodeForPrinterTemperature:
    """Printer-specific temperature validation via validate_gcode_for_printer()."""

    def test_ender3_hotend_270_blocked(self) -> None:
        """Ender 3 max hotend is 260C; 270C should be BLOCKED."""
        r = validate_gcode_for_printer("M104 S270", "ender3")
        assert r.valid is False
        assert len(r.errors) == 1
        assert "M104 S270" in r.blocked_commands

    def test_bambu_x1c_hotend_270_passes(self) -> None:
        """Bambu X1C max hotend is 300C; 270C should PASS."""
        r = validate_gcode_for_printer("M104 S270", "bambu_x1c")
        assert r.valid is True
        assert "M104 S270" in r.commands
        assert r.errors == []

    def test_ender3_bed_120_blocked(self) -> None:
        """Ender 3 max bed is 110C; 120C should be BLOCKED."""
        r = validate_gcode_for_printer("M140 S120", "ender3")
        assert r.valid is False
        assert len(r.errors) == 1
        assert "M140 S120" in r.blocked_commands

    def test_prusa_mk4_bed_120_passes(self) -> None:
        """Prusa MK4 max bed is 120C; 120C (at limit) should PASS."""
        r = validate_gcode_for_printer("M140 S120", "prusa_mk4")
        assert r.valid is True
        assert "M140 S120" in r.commands
        assert r.errors == []

    def test_ender3_hotend_at_limit_passes(self) -> None:
        """Ender 3 max hotend is 260C; exactly 260C should PASS."""
        r = validate_gcode_for_printer("M104 S260", "ender3")
        assert r.valid is True
        assert "M104 S260" in r.commands

    def test_ender3_hotend_just_over_blocked(self) -> None:
        """Ender 3 max hotend is 260C; 260.1C should be BLOCKED."""
        r = validate_gcode_for_printer("M104 S260.1", "ender3")
        assert r.valid is False

    def test_bambu_x1c_hotend_at_limit_passes(self) -> None:
        """Bambu X1C max hotend is 300C; exactly 300C should PASS."""
        r = validate_gcode_for_printer("M104 S300", "bambu_x1c")
        assert r.valid is True

    def test_bambu_x1c_hotend_over_blocked(self) -> None:
        """Bambu X1C max hotend is 300C; 301C should be BLOCKED."""
        r = validate_gcode_for_printer("M104 S301", "bambu_x1c")
        assert r.valid is False

    def test_m109_wait_command_also_checked(self) -> None:
        """M109 (set and wait) should use the same limits as M104."""
        r = validate_gcode_for_printer("M109 S270", "ender3")
        assert r.valid is False
        assert len(r.errors) == 1

    def test_m190_wait_command_also_checked(self) -> None:
        """M190 (set and wait) should use the same limits as M140."""
        r = validate_gcode_for_printer("M190 S120", "ender3")
        assert r.valid is False

    def test_temp_zero_always_passes(self) -> None:
        """Setting temperature to 0 (off) is always valid."""
        r = validate_gcode_for_printer("M104 S0\nM140 S0", "ender3")
        assert r.valid is True
        assert len(r.commands) == 2


# ===================================================================
# validate_gcode_for_printer() -- feedrate limits
# ===================================================================

class TestValidateGcodeForPrinterFeedrate:
    """Printer-specific feedrate validation."""

    def test_ender3_high_feedrate_warns(self) -> None:
        """Ender 3 max feedrate is 7500 mm/min; 35000 should WARN."""
        r = validate_gcode_for_printer("G1 X100 F35000", "ender3")
        assert r.valid is True  # warnings don't block
        assert any("feedrate" in w.lower() for w in r.warnings)

    def test_bambu_x1c_high_feedrate_passes(self) -> None:
        """Bambu X1C max feedrate is 30000 mm/min; 35000 should WARN (over 30000)."""
        r = validate_gcode_for_printer("G1 X100 F35000", "bambu_x1c")
        assert r.valid is True
        # F35000 exceeds bambu_x1c max of 30000, so it should warn
        assert any("feedrate" in w.lower() for w in r.warnings)

    def test_bambu_x1c_feedrate_at_limit_no_warn(self) -> None:
        """Bambu X1C max feedrate is 30000; exactly 30000 should NOT warn."""
        r = validate_gcode_for_printer("G1 X100 F30000", "bambu_x1c")
        assert r.valid is True
        assert not any("feedrate" in w.lower() for w in r.warnings)

    def test_ender3_feedrate_at_limit_no_warn(self) -> None:
        """Ender 3 max feedrate is 7500; exactly 7500 should NOT warn."""
        r = validate_gcode_for_printer("G1 X100 F7500", "ender3")
        assert r.valid is True
        assert not any("feedrate" in w.lower() for w in r.warnings)

    def test_ender3_feedrate_just_over_warns(self) -> None:
        """Ender 3 max feedrate is 7500; 7501 should warn."""
        r = validate_gcode_for_printer("G1 X100 F7501", "ender3")
        assert r.valid is True
        assert any("feedrate" in w.lower() for w in r.warnings)

    def test_g0_feedrate_also_checked(self) -> None:
        """G0 (rapid move) feedrate should also be checked."""
        r = validate_gcode_for_printer("G0 X100 F35000", "ender3")
        assert r.valid is True
        assert any("feedrate" in w.lower() for w in r.warnings)


# ===================================================================
# validate_gcode_for_printer() -- blocked commands
# ===================================================================

class TestValidateGcodeForPrinterBlockedCommands:
    """Blocked commands are blocked regardless of printer."""

    def test_m112_blocked_ender3(self) -> None:
        r = validate_gcode_for_printer("M112", "ender3")
        assert r.valid is False
        assert "M112" in r.blocked_commands

    def test_m112_blocked_bambu_x1c(self) -> None:
        r = validate_gcode_for_printer("M112", "bambu_x1c")
        assert r.valid is False
        assert "M112" in r.blocked_commands

    def test_m112_blocked_default(self) -> None:
        r = validate_gcode_for_printer("M112", "default")
        assert r.valid is False
        assert "M112" in r.blocked_commands

    def test_m500_blocked_any_printer(self) -> None:
        r = validate_gcode_for_printer("M500", "prusa_mk4")
        assert r.valid is False
        assert "M500" in r.blocked_commands

    def test_m502_blocked_any_printer(self) -> None:
        r = validate_gcode_for_printer("M502", "bambu_x1c")
        assert r.valid is False
        assert "M502" in r.blocked_commands

    def test_m997_blocked_any_printer(self) -> None:
        r = validate_gcode_for_printer("M997", "ender3")
        assert r.valid is False
        assert "M997" in r.blocked_commands


# ===================================================================
# validate_gcode_for_printer() -- unknown printer fallback
# ===================================================================

class TestValidateGcodeForPrinterUnknown:
    """Unknown printer should fall back to default profile validation."""

    def test_unknown_printer_uses_default_limits(self) -> None:
        """Unknown printer should use default max hotend of 300C."""
        # 290C should pass with default limits (300C max)
        r = validate_gcode_for_printer("M104 S290", "totally_unknown_printer")
        assert r.valid is True

    def test_unknown_printer_still_blocks_over_default(self) -> None:
        """Even with unknown printer, going over default 300C should block."""
        r = validate_gcode_for_printer("M104 S350", "totally_unknown_printer")
        assert r.valid is False

    def test_unknown_printer_still_blocks_dangerous_commands(self) -> None:
        """M112 should be blocked even for unknown printers."""
        r = validate_gcode_for_printer("M112", "totally_unknown_printer")
        assert r.valid is False
        assert "M112" in r.blocked_commands

    def test_unknown_printer_result_type(self) -> None:
        r = validate_gcode_for_printer("G28", "unknown_xyz")
        assert isinstance(r, GCodeValidationResult)


# ===================================================================
# Error messages include printer display name
# ===================================================================

class TestErrorMessagesIncludeDisplayName:
    """Error messages should reference the printer's display name for clarity."""

    def test_ender3_hotend_error_includes_display_name(self) -> None:
        r = validate_gcode_for_printer("M104 S270", "ender3")
        assert r.valid is False
        assert any(
            "Ender 3" in e or "ender" in e.lower()
            for e in r.errors
        ), f"Expected printer display name in error, got: {r.errors}"

    def test_bambu_x1c_hotend_error_includes_display_name(self) -> None:
        r = validate_gcode_for_printer("M104 S301", "bambu_x1c")
        assert r.valid is False
        assert any(
            "Bambu" in e or "X1" in e
            for e in r.errors
        ), f"Expected printer display name in error, got: {r.errors}"

    def test_ender3_bed_error_includes_display_name(self) -> None:
        r = validate_gcode_for_printer("M140 S120", "ender3")
        assert r.valid is False
        assert any(
            "Ender 3" in e or "ender" in e.lower()
            for e in r.errors
        ), f"Expected printer display name in error, got: {r.errors}"

    def test_feedrate_warning_includes_display_name(self) -> None:
        r = validate_gcode_for_printer("G1 X100 F50000", "ender3")
        assert r.valid is True
        assert any(
            "Ender 3" in w or "ender" in w.lower()
            for w in r.warnings
            if "feedrate" in w.lower()
        ), f"Expected printer display name in feedrate warning, got: {r.warnings}"


# ===================================================================
# validate_gcode_for_printer() -- multiple commands
# ===================================================================

class TestValidateGcodeForPrinterMultipleCommands:
    """Batch validation with printer-specific limits."""

    def test_mixed_valid_and_blocked(self) -> None:
        """One bad temp among good commands should invalidate the batch."""
        r = validate_gcode_for_printer(
            "G28\nM104 S270\nG1 X10 Y10",
            "ender3",
        )
        assert r.valid is False
        assert "G28" in r.commands
        assert "G1 X10 Y10" in r.commands
        assert "M104 S270" in r.blocked_commands
        assert len(r.errors) == 1

    def test_all_commands_valid(self) -> None:
        r = validate_gcode_for_printer(
            "G28\nM104 S200\nM140 S60\nG1 X10 Y10 Z0.2 F1200",
            "ender3",
        )
        assert r.valid is True
        assert len(r.commands) == 4
        assert r.errors == []

    def test_list_input(self) -> None:
        r = validate_gcode_for_printer(
            ["G28", "M104 S200"],
            "ender3",
        )
        assert r.valid is True
        assert len(r.commands) == 2

    def test_list_input_with_embedded_newlines(self) -> None:
        r = validate_gcode_for_printer(
            ["G28\nM104 S200", "G1 X10"],
            "bambu_x1c",
        )
        assert r.valid is True
        assert len(r.commands) == 3
