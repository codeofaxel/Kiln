"""Tests for safety profiles and printer-specific G-code validation.

Covers:
    - SafetyProfile dataclass field verification
    - get_profile() for curated printers
    - get_profile() fallback to default for unknown printers
    - get_profile() normalization (case, hyphens, whitespace)
    - list_profiles() contents
    - get_all_profiles() dict structure
    - profile_to_dict() serialization roundtrip
    - JSON data file validity (the roster, and the rows with limits)
    - validate_gcode_for_printer() with printer-specific temperature limits
    - validate_gcode_for_printer() with printer-specific feedrate limits
    - validate_gcode_for_printer() blocked commands are still blocked
    - validate_gcode_for_printer() with unknown printer (default fallback)
    - Error messages include printer display name

Since 2026-10-12 the bundled file is a roster and a real printer's limits
are served per printer, so the curated printers here are the session
fixture's SYNTHETIC rows (``tests/fixtures/printer_catalogue_test.json``):
``kilntest_ptfe`` (260 C / 100 C / 8000 mm/min, PTFE-lined),
``kilntest_open`` (300 / 110 / 12000), ``kilntest_enclosed`` (320 / 120 /
60 C chamber / 30000), ``kilntest_big`` (350 x 350 x 400 mm),
``kilntest_variants`` (7500 mm/min).  The real printers' real numbers --
the Ender 3's 250-not-260, the X1C's 300 and its 30000 mm/min, the MK4's
290-not-300 -- are pinned beside the real rows, in kiln-pro's
``tests/test_printer_catalogue_numbers.py``.
"""

from __future__ import annotations

import json

import pytest

from kiln.gcode import GCodeValidationResult, validate_gcode_for_printer
from kiln.safety_profiles import (
    _DATA_FILE,
    SafetyProfile,
    get_all_profiles,
    get_profile,
    list_profiles,
    profile_to_dict,
)

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

    def test_a_lined_printers_limits(self) -> None:
        profile = get_profile("kilntest_ptfe")
        assert profile.id == "kilntest_ptfe"
        assert profile.max_hotend_temp == 260.0
        assert profile.max_bed_temp == 100.0
        assert profile.max_chamber_temp is None
        assert profile.max_feedrate == 8000.0
        assert profile.display_name == "Kilntest Lined 235"

    def test_an_enclosed_printers_limits(self) -> None:
        profile = get_profile("kilntest_enclosed")
        assert profile.id == "kilntest_enclosed"
        assert profile.max_hotend_temp == 320.0
        assert profile.max_bed_temp == 120.0
        assert profile.max_chamber_temp == 60.0
        assert profile.max_feedrate == 30000.0
        assert profile.display_name == "Kilntest Enclosed 256"

    def test_a_large_printers_limits(self) -> None:
        profile = get_profile("kilntest_big")
        assert profile.id == "kilntest_big"
        assert profile.max_hotend_temp == 300.0
        assert profile.max_bed_temp == 110.0
        assert profile.build_volume == [350, 350, 400]

    def test_creality_brand_prefixed_alias(self) -> None:
        # ``creality_`` in front of any id is tolerated and stripped.
        profile = get_profile("creality_kilntest_big")
        assert profile.id == "kilntest_big"

    def test_the_printers_own_name_finds_its_row(self) -> None:
        profile = get_profile("Kilntest Enclosed 256")
        assert profile.id == "kilntest_enclosed"

    def test_a_roster_printer_whose_record_is_not_held_is_the_floor(self) -> None:
        """A catalogue name with no record here (the session fixture seals
        the served door as a signed-out install) is held to the generic
        floor, never to a number nobody served.  The served path itself is
        proved in test_pro_catalogue_bridge."""
        profile = get_profile("kilntest_served")
        assert profile.id == "default"
        assert "kilntest_served" in list_profiles()

    def test_default_profile(self) -> None:
        """The generic profile describes the LEAST capable machine, not the most.

        These were 300/130 until 2026-07-20 — the loosest ceiling in the whole
        file, applied to a printer we cannot identify and which may well have a
        PTFE-lined hotend. An unknown machine is now treated as the weakest one
        the registry describes, matching printer_intelligence -- and since
        2026-10-12, when the rows moved to kiln-pro's catalogue, the hotend
        floor is the PTFE-safe 240 C: an install that could not fetch a
        printer's record cannot know whether its hotend is lined.
        """
        profile = get_profile("default")
        assert profile.id == "default"
        assert profile.max_hotend_temp == 240.0
        assert profile.max_bed_temp == 80.0
        assert profile.max_chamber_temp == 80.0
        assert profile.max_feedrate == 10000.0
        assert profile.display_name == "Generic / Unknown Printer"

    def test_nonexistent_falls_back_to_default(self) -> None:
        profile = get_profile("nonexistent_printer_xyz")
        assert profile.id == "default"
        assert profile.display_name == "Generic / Unknown Printer"

    def test_normalization_case_insensitive(self) -> None:
        """'KILNTEST_ENCLOSED' should match 'kilntest_enclosed'."""
        profile = get_profile("KILNTEST_ENCLOSED")
        assert profile.id == "kilntest_enclosed"

    def test_normalization_hyphens_to_underscores(self) -> None:
        """'Kilntest-Enclosed' normalises to 'kilntest_enclosed'; a spelling
        whose normalised form is no key and no prefix of one falls back to
        default."""
        profile = get_profile("Kilntest-Enclosed")
        assert profile.id == "kilntest_enclosed"
        assert get_profile("Kiln-Test-Enclosed").id == "default"

    def test_normalization_mixed_case_and_hyphens(self) -> None:
        """'Kilntest-Big' normalises to 'kilntest_big'."""
        profile = get_profile("Kilntest-Big")
        assert profile.id == "kilntest_big"

    def test_normalization_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace should be stripped."""
        profile = get_profile("  kilntest_ptfe  ")
        assert profile.id == "kilntest_ptfe"


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
        # The roster: a real printer is listed by name whether or not its
        # record is held here ...
        assert "ender3" in result
        assert "bambu_x1c" in result
        # ... and so is a row with limits in it.
        assert "kilntest_open" in result

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
        profile = get_profile("kilntest_ptfe")
        d = profile_to_dict(profile)
        assert d["id"] == "kilntest_ptfe"
        assert d["display_name"] == profile.display_name
        assert d["max_hotend_temp"] == 260.0
        assert d["max_bed_temp"] == 100.0
        assert d["max_chamber_temp"] is None
        assert d["max_feedrate"] == 8000.0
        assert d["min_safe_z"] == 0.0
        assert d["build_volume"] == [235, 235, 250]
        assert isinstance(d["notes"], str)

    def test_all_keys_present(self) -> None:
        profile = get_profile("kilntest_enclosed")
        d = profile_to_dict(profile)
        expected_keys = {
            "id", "display_name", "max_hotend_temp", "max_bed_temp",
            "max_chamber_temp", "max_feedrate", "min_safe_z",
            "max_volumetric_flow", "build_volume", "notes",
            "variant", "available_variants",
            "owner_supplied", "curated_base", "limits_provenance",
        }
        assert set(d.keys()) == expected_keys

    def test_json_serializable(self) -> None:
        profile = get_profile("kilntest_ptfe")
        d = profile_to_dict(profile)
        # Should not raise
        serialized = json.dumps(d)
        assert isinstance(serialized, str)

    def test_dict_values_match_profile(self) -> None:
        profile = get_profile("kilntest_enclosed")
        d = profile_to_dict(profile)
        assert d["max_hotend_temp"] == profile.max_hotend_temp
        assert d["max_bed_temp"] == profile.max_bed_temp
        assert d["max_chamber_temp"] == profile.max_chamber_temp
        assert d["max_feedrate"] == profile.max_feedrate
        assert d["max_volumetric_flow"] == profile.max_volumetric_flow


# ===================================================================
# JSON file validity
# ===================================================================

def _rows_with_limits(raw: dict) -> dict[str, dict]:
    """The rows the loader builds a profile from: the generic row and, in
    the session copy this suite reads, the synthetic fixture rows."""
    return {k: v for k, v in raw.items() if not k.startswith("_") and "max_hotend_temp" in v}


class TestJSONFileValidity:
    """Validate the bundled safety_profiles.json data file (the session's
    copy, with the synthetic rows flattened in)."""

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
        assert "max_hotend_temp" in raw["default"]

    def test_every_row_has_a_name(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in raw.items():
            if key == "_meta":
                continue
            assert isinstance(data.get("display_name"), str) and data["display_name"], key

    def test_a_roster_row_is_a_name_and_nothing_else(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        roster = {k: v for k, v in raw.items() if not k.startswith("_") and k not in _rows_with_limits(raw)}
        assert roster, "expected roster rows"
        for key, data in roster.items():
            assert set(data) == {"display_name"}, key

    def test_rows_with_limits_have_the_required_fields(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        required_fields = {"display_name", "max_hotend_temp", "max_bed_temp"}
        for key, data in _rows_with_limits(raw).items():
            for field in required_fields:
                assert field in data, (
                    f"Profile '{key}' is missing required field '{field}'"
                )

    def test_temperature_values_are_numeric(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in _rows_with_limits(raw).items():
            assert isinstance(data["max_hotend_temp"], (int, float)), (
                f"Profile '{key}': max_hotend_temp is not numeric"
            )
            assert isinstance(data["max_bed_temp"], (int, float)), (
                f"Profile '{key}': max_bed_temp is not numeric"
            )

    def test_temperature_values_are_positive(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in _rows_with_limits(raw).items():
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

    def test_lined_printer_hotend_270_blocked(self) -> None:
        """kilntest_ptfe max hotend is 260C; 270C should be BLOCKED."""
        r = validate_gcode_for_printer("M104 S270", "kilntest_ptfe")
        assert r.valid is False
        assert len(r.errors) == 1
        assert "M104 S270" in r.blocked_commands

    def test_open_printer_hotend_270_passes(self) -> None:
        """kilntest_open max hotend is 300C; 270C should PASS."""
        r = validate_gcode_for_printer("M104 S270", "kilntest_open")
        assert r.valid is True
        assert "M104 S270" in r.commands
        assert r.errors == []

    def test_lined_printer_bed_120_blocked(self) -> None:
        """kilntest_ptfe max bed is 100C; 120C should be BLOCKED."""
        r = validate_gcode_for_printer("M140 S120", "kilntest_ptfe")
        assert r.valid is False
        assert len(r.errors) == 1
        assert "M140 S120" in r.blocked_commands

    def test_enclosed_printer_bed_120_passes(self) -> None:
        """kilntest_enclosed max bed is 120C; 120C (at limit) should PASS."""
        r = validate_gcode_for_printer("M140 S120", "kilntest_enclosed")
        assert r.valid is True
        assert "M140 S120" in r.commands
        assert r.errors == []

    def test_hotend_at_limit_passes(self) -> None:
        """kilntest_ptfe max hotend is 260C; exactly 260C should PASS."""
        r = validate_gcode_for_printer("M104 S260", "kilntest_ptfe")
        assert r.valid is True
        assert "M104 S260" in r.commands

    def test_hotend_just_over_blocked(self) -> None:
        """kilntest_ptfe max hotend is 260C; 260.1C should be BLOCKED."""
        r = validate_gcode_for_printer("M104 S260.1", "kilntest_ptfe")
        assert r.valid is False

    def test_open_printer_hotend_at_limit_passes(self) -> None:
        """kilntest_open max hotend is 300C; exactly 300C should PASS."""
        r = validate_gcode_for_printer("M104 S300", "kilntest_open")
        assert r.valid is True

    def test_open_printer_hotend_over_blocked(self) -> None:
        """kilntest_open max hotend is 300C; 301C should be BLOCKED."""
        r = validate_gcode_for_printer("M104 S301", "kilntest_open")
        assert r.valid is False

    def test_m109_wait_command_also_checked(self) -> None:
        """M109 (set and wait) should use the same limits as M104."""
        r = validate_gcode_for_printer("M109 S270", "kilntest_ptfe")
        assert r.valid is False
        assert len(r.errors) == 1

    def test_m190_wait_command_also_checked(self) -> None:
        """M190 (set and wait) should use the same limits as M140."""
        r = validate_gcode_for_printer("M190 S120", "kilntest_ptfe")
        assert r.valid is False

    def test_temp_zero_always_passes(self) -> None:
        """Setting temperature to 0 (off) is always valid."""
        r = validate_gcode_for_printer("M104 S0\nM140 S0", "kilntest_ptfe")
        assert r.valid is True
        assert len(r.commands) == 2


# ===================================================================
# validate_gcode_for_printer() -- feedrate limits
# ===================================================================

class TestValidateGcodeForPrinterFeedrate:
    """Printer-specific feedrate validation."""

    def test_slow_printer_high_feedrate_warns(self) -> None:
        """kilntest_variants max feedrate is 7500 mm/min; 35000 should WARN."""
        r = validate_gcode_for_printer("G1 X100 F35000", "kilntest_variants")
        assert r.valid is True  # warnings don't block
        assert any("feedrate" in w.lower() for w in r.warnings)

    def test_fast_printer_high_feedrate_warns(self) -> None:
        """kilntest_enclosed max feedrate is 30000 mm/min; 35000 should WARN (over 30000)."""
        r = validate_gcode_for_printer("G1 X100 F35000", "kilntest_enclosed")
        assert r.valid is True
        assert any("feedrate" in w.lower() for w in r.warnings)

    def test_fast_printer_feedrate_at_limit_no_warn(self) -> None:
        """kilntest_enclosed max feedrate is 30000; exactly 30000 should NOT warn."""
        r = validate_gcode_for_printer("G1 X100 F30000", "kilntest_enclosed")
        assert r.valid is True
        assert not any("feedrate" in w.lower() for w in r.warnings)

    def test_slow_printer_feedrate_at_limit_no_warn(self) -> None:
        """kilntest_variants max feedrate is 7500; exactly 7500 should NOT warn."""
        r = validate_gcode_for_printer("G1 X100 F7500", "kilntest_variants")
        assert r.valid is True
        assert not any("feedrate" in w.lower() for w in r.warnings)

    def test_slow_printer_feedrate_just_over_warns(self) -> None:
        """kilntest_variants max feedrate is 7500; 7501 should warn."""
        r = validate_gcode_for_printer("G1 X100 F7501", "kilntest_variants")
        assert r.valid is True
        assert any("feedrate" in w.lower() for w in r.warnings)

    def test_g0_feedrate_also_checked(self) -> None:
        """G0 (rapid move) feedrate should also be checked."""
        r = validate_gcode_for_printer("G0 X100 F35000", "kilntest_variants")
        assert r.valid is True
        assert any("feedrate" in w.lower() for w in r.warnings)


# ===================================================================
# validate_gcode_for_printer() -- blocked commands
# ===================================================================

class TestValidateGcodeForPrinterBlockedCommands:
    """Blocked commands are blocked regardless of printer."""

    def test_m112_blocked_lined_printer(self) -> None:
        r = validate_gcode_for_printer("M112", "kilntest_ptfe")
        assert r.valid is False
        assert "M112" in r.blocked_commands

    def test_m112_blocked_enclosed_printer(self) -> None:
        r = validate_gcode_for_printer("M112", "kilntest_enclosed")
        assert r.valid is False
        assert "M112" in r.blocked_commands

    def test_m112_blocked_default(self) -> None:
        r = validate_gcode_for_printer("M112", "default")
        assert r.valid is False
        assert "M112" in r.blocked_commands

    def test_m500_blocked_any_printer(self) -> None:
        r = validate_gcode_for_printer("M500", "kilntest_big")
        assert r.valid is False
        assert "M500" in r.blocked_commands

    def test_m502_blocked_any_printer(self) -> None:
        r = validate_gcode_for_printer("M502", "kilntest_enclosed")
        assert r.valid is False
        assert "M502" in r.blocked_commands

    def test_m997_blocked_any_printer(self) -> None:
        r = validate_gcode_for_printer("M997", "kilntest_ptfe")
        assert r.valid is False
        assert "M997" in r.blocked_commands


# ===================================================================
# validate_gcode_for_printer() -- unknown printer fallback
# ===================================================================

class TestValidateGcodeForPrinterUnknown:
    """Unknown printer should fall back to default profile validation."""

    def test_unknown_printer_uses_default_limits(self) -> None:
        """Unknown printer falls back to the generic floor (240C).

        Previously the default was 300C, so 290C passed for a printer we know
        nothing about. That is the wrong direction for an unidentified machine:
        240C now passes and 290C does not.
        """
        r = validate_gcode_for_printer("M104 S240", "totally_unknown_printer")
        assert r.valid is True
        over = validate_gcode_for_printer("M104 S290", "totally_unknown_printer")
        assert over.valid is False

    def test_unknown_printer_still_blocks_over_default(self) -> None:
        """Even with unknown printer, going well over the floor should block."""
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

    def test_hotend_error_includes_display_name(self) -> None:
        r = validate_gcode_for_printer("M104 S270", "kilntest_ptfe")
        assert r.valid is False
        assert any(
            "Kilntest Lined 235" in e for e in r.errors
        ), f"Expected printer display name in error, got: {r.errors}"

    def test_enclosed_printer_hotend_error_includes_display_name(self) -> None:
        r = validate_gcode_for_printer("M104 S321", "kilntest_enclosed")
        assert r.valid is False
        assert any(
            "Kilntest Enclosed 256" in e for e in r.errors
        ), f"Expected printer display name in error, got: {r.errors}"

    def test_bed_error_includes_display_name(self) -> None:
        r = validate_gcode_for_printer("M140 S120", "kilntest_ptfe")
        assert r.valid is False
        assert any(
            "Kilntest Lined 235" in e for e in r.errors
        ), f"Expected printer display name in error, got: {r.errors}"

    def test_feedrate_warning_includes_display_name(self) -> None:
        r = validate_gcode_for_printer("G1 X100 F50000", "kilntest_ptfe")
        assert r.valid is True
        assert any(
            "Kilntest Lined 235" in w
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
            "kilntest_ptfe",
        )
        assert r.valid is False
        assert "G28" in r.commands
        assert "G1 X10 Y10" in r.commands
        assert "M104 S270" in r.blocked_commands
        assert len(r.errors) == 1

    def test_all_commands_valid(self) -> None:
        r = validate_gcode_for_printer(
            "G28\nM104 S200\nM140 S60\nG1 X10 Y10 Z0.2 F1200",
            "kilntest_ptfe",
        )
        assert r.valid is True
        assert len(r.commands) == 4
        assert r.errors == []

    def test_list_input(self) -> None:
        r = validate_gcode_for_printer(
            ["G28", "M104 S200"],
            "kilntest_ptfe",
        )
        assert r.valid is True
        assert len(r.commands) == 2

    def test_list_input_with_embedded_newlines(self) -> None:
        r = validate_gcode_for_printer(
            ["G28\nM104 S200", "G1 X10"],
            "kilntest_enclosed",
        )
        assert r.valid is True
        assert len(r.commands) == 3
