"""Tests for bundled slicer profiles — per-printer INI generation and lookup.

Covers:
    - get_slicer_profile() for known printers (ender3, bambu_x1c)
    - get_slicer_profile() fallback to default for unknown printers
    - get_slicer_profile() case/hyphen normalization
    - list_slicer_profiles() returns sorted list with expected entries
    - resolve_slicer_profile() writes a temp .ini file that exists
    - resolve_slicer_profile() cache — calling twice returns same path
    - resolve_slicer_profile() with overrides applied in the .ini
    - slicer_profile_to_dict() roundtrip serialization
    - JSON data file validity — all profiles have required settings keys
    - SlicerProfile dataclass frozen immutability
    - Profile settings contain expected keys across all profiles
"""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from kiln.slicer_profiles import (
    _DATA_FILE,
    get_slicer_profile,
    list_slicer_profiles,
    resolve_multiextruder_profile,
    resolve_slicer_profile,
    slicer_profile_to_dict,
)

# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture(autouse=True)
def _reset_slicer_profile_cache():
    """Reset the singleton cache before each test for isolation."""
    import kiln.slicer_profiles as mod
    mod._cache.clear()
    mod._loaded = False
    mod._temp_cache.clear()
    yield
    mod._cache.clear()
    mod._loaded = False
    mod._temp_cache.clear()


# ===================================================================
# get_slicer_profile
# ===================================================================

class TestGetSlicerProfile:
    """Tests for get_slicer_profile() lookup and fallback logic."""

    def test_ender3_profile_exists(self) -> None:
        profile = get_slicer_profile("ender3")
        assert profile.id == "ender3"
        assert profile.display_name == "Creality Ender 3 / Ender 3 Pro / Ender 3 V2"
        assert profile.slicer == "prusaslicer"

    def test_ender3_bowden_retraction(self) -> None:
        """Ender 3 has bowden setup — retraction should be longer than direct drive."""
        profile = get_slicer_profile("ender3")
        retract = float(profile.settings["retract_length"])
        assert retract >= 4.0, "Bowden retraction should be >= 4mm"

    def test_bambu_x1c_profile(self) -> None:
        profile = get_slicer_profile("bambu_x1c")
        assert profile.id == "bambu_x1c"
        assert profile.slicer == "prusaslicer"
        assert profile.display_name == "Bambu Lab X1 Carbon"

    def test_nonexistent_falls_back_to_default(self) -> None:
        profile = get_slicer_profile("nonexistent_printer_9999")
        assert profile.id == "default"
        assert profile.display_name == "Generic FDM Defaults"

    def test_case_normalization(self) -> None:
        """'Ender3' (mixed case) should resolve to 'ender3'."""
        profile = get_slicer_profile("Ender3")
        assert profile.id == "ender3"

    def test_hyphen_normalization(self) -> None:
        """'Ender-3' normalizes hyphens to underscores ('ender_3').

        Since 'ender_3' doesn't exactly match 'ender3' and the prefix
        fuzzy match also fails (different character at position 5),
        this falls back to default.
        """
        profile = get_slicer_profile("Ender-3")
        # "ender_3" doesn't match "ender3" exactly or by prefix
        assert profile.id == "default"

    def test_whitespace_stripping(self) -> None:
        profile = get_slicer_profile("  ender3  ")
        assert profile.id == "ender3"

    def test_default_profile_directly(self) -> None:
        profile = get_slicer_profile("default")
        assert profile.id == "default"
        assert profile.slicer == "prusaslicer"


# ===================================================================
# list_slicer_profiles
# ===================================================================

class TestListSlicerProfiles:
    """Tests for list_slicer_profiles() output."""

    def test_returns_sorted_list(self) -> None:
        profiles = list_slicer_profiles()
        assert profiles == sorted(profiles)

    def test_contains_expected_profiles(self) -> None:
        profiles = list_slicer_profiles()
        assert "default" in profiles
        assert "ender3" in profiles
        assert "bambu_x1c" in profiles

    def test_returns_list_of_strings(self) -> None:
        profiles = list_slicer_profiles()
        assert isinstance(profiles, list)
        assert all(isinstance(p, str) for p in profiles)

    def test_no_meta_key(self) -> None:
        """The _meta key from JSON should not appear in the profile list."""
        profiles = list_slicer_profiles()
        assert "_meta" not in profiles


# ===================================================================
# resolve_slicer_profile
# ===================================================================

class TestResolveSlicerProfile:
    """Tests for resolve_slicer_profile() temp file generation."""

    def test_writes_ini_file_that_exists(self) -> None:
        path = resolve_slicer_profile("ender3")
        assert os.path.isfile(path)
        assert path.endswith(".ini")

    def test_ini_contains_settings(self) -> None:
        path = resolve_slicer_profile("ender3")
        content = Path(path).read_text(encoding="utf-8")
        assert "layer_height" in content
        assert "temperature" in content
        assert "retract_length" in content

    def test_ini_contains_header_comment(self) -> None:
        path = resolve_slicer_profile("ender3")
        content = Path(path).read_text(encoding="utf-8")
        assert content.startswith("# Kiln auto-generated profile:")

    def test_cache_returns_same_path(self) -> None:
        """Calling resolve twice with same args should return the same cached path."""
        path1 = resolve_slicer_profile("ender3")
        path2 = resolve_slicer_profile("ender3")
        assert path1 == path2

    def test_overrides_applied(self) -> None:
        path = resolve_slicer_profile("ender3", overrides={"layer_height": "0.3"})
        content = Path(path).read_text(encoding="utf-8")
        assert "layer_height = 0.3" in content

    def test_overrides_produce_different_path(self) -> None:
        """Overrides should produce a different cached file than the base profile."""
        path_base = resolve_slicer_profile("ender3")
        path_override = resolve_slicer_profile("ender3", overrides={"layer_height": "0.3"})
        assert path_base != path_override

    def test_override_does_not_mutate_profile(self) -> None:
        """Original profile settings should be unchanged after an override call."""
        profile_before = get_slicer_profile("ender3")
        original_lh = profile_before.settings["layer_height"]
        resolve_slicer_profile("ender3", overrides={"layer_height": "0.3"})
        profile_after = get_slicer_profile("ender3")
        assert profile_after.settings["layer_height"] == original_lh


# ===================================================================
# slicer_profile_to_dict
# ===================================================================

class TestSlicerProfileToDict:
    """Tests for slicer_profile_to_dict() serialization."""

    def test_roundtrip_contains_all_fields(self) -> None:
        profile = get_slicer_profile("ender3")
        d = slicer_profile_to_dict(profile)
        assert d["id"] == "ender3"
        assert d["display_name"] == profile.display_name
        assert d["slicer"] == profile.slicer
        assert d["notes"] == profile.notes
        assert isinstance(d["settings"], dict)

    def test_settings_dict_matches_profile(self) -> None:
        profile = get_slicer_profile("ender3")
        d = slicer_profile_to_dict(profile)
        for key, val in profile.settings.items():
            assert d["settings"][key] == val

    def test_dict_is_json_serializable(self) -> None:
        profile = get_slicer_profile("bambu_x1c")
        d = slicer_profile_to_dict(profile)
        serialized = json.dumps(d)
        assert isinstance(serialized, str)


# ===================================================================
# JSON data file validity
# ===================================================================

class TestSlicerProfilesJSON:
    """Tests for the bundled slicer_profiles.json data file."""

    REQUIRED_SETTINGS_KEYS = [
        "layer_height",
        "temperature",
        "nozzle_diameter",
        "retract_length",
    ]

    def test_json_file_exists_and_parses(self) -> None:
        assert _DATA_FILE.exists()
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)

    def test_all_profiles_have_settings_dict(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in raw.items():
            if key.startswith("_"):
                continue
            assert "settings" in data, f"Profile '{key}' missing 'settings'"
            assert isinstance(data["settings"], dict)

    def test_all_profiles_have_required_settings_keys(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in raw.items():
            if key.startswith("_"):
                continue
            settings = data["settings"]
            for req_key in self.REQUIRED_SETTINGS_KEYS:
                assert req_key in settings, (
                    f"Profile '{key}' missing required setting '{req_key}'"
                )

    def test_all_profiles_have_display_name(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in raw.items():
            if key.startswith("_"):
                continue
            assert "display_name" in data, f"Profile '{key}' missing 'display_name'"


# ===================================================================
# SlicerProfile dataclass immutability
# ===================================================================

class TestSlicerProfileImmutability:
    """Verify SlicerProfile is frozen (immutable)."""

    def test_cannot_modify_id(self) -> None:
        profile = get_slicer_profile("ender3")
        with pytest.raises(FrozenInstanceError):
            profile.id = "changed"  # type: ignore[misc]

    def test_cannot_modify_slicer(self) -> None:
        profile = get_slicer_profile("ender3")
        with pytest.raises(FrozenInstanceError):
            profile.slicer = "changed"  # type: ignore[misc]

    def test_cannot_modify_display_name(self) -> None:
        profile = get_slicer_profile("ender3")
        with pytest.raises(FrozenInstanceError):
            profile.display_name = "changed"  # type: ignore[misc]


# ===================================================================
# Profile settings consistency
# ===================================================================

class TestProfileSettingsConsistency:
    """Verify key settings are present across all loaded profiles."""

    EXPECTED_KEYS = ["nozzle_diameter", "retract_length", "layer_height", "temperature"]

    def test_all_profiles_have_expected_settings(self) -> None:
        profile_ids = list_slicer_profiles()
        for pid in profile_ids:
            profile = get_slicer_profile(pid)
            for key in self.EXPECTED_KEYS:
                assert key in profile.settings, (
                    f"Profile '{pid}' missing expected setting '{key}'"
                )

    def test_nozzle_diameter_is_numeric(self) -> None:
        profile_ids = list_slicer_profiles()
        for pid in profile_ids:
            profile = get_slicer_profile(pid)
            val = float(profile.settings["nozzle_diameter"])
            assert 0.1 <= val <= 1.5, f"Profile '{pid}' nozzle_diameter {val} out of range"

    def test_retract_length_is_positive(self) -> None:
        profile_ids = list_slicer_profiles()
        for pid in profile_ids:
            profile = get_slicer_profile(pid)
            val = float(profile.settings["retract_length"])
            assert val > 0, f"Profile '{pid}' retract_length should be positive"


# ===================================================================
# resolve_multiextruder_profile
# ===================================================================

class TestResolveMultiextruderProfile:
    """Tests for resolve_multiextruder_profile() — AMS/MMU INI generation."""

    def test_writes_ini_file(self) -> None:
        path = resolve_multiextruder_profile("bambu_a1", 2)
        assert os.path.isfile(path)
        assert path.endswith(".ini")

    def test_extruder_count_in_ini(self) -> None:
        path = resolve_multiextruder_profile("bambu_a1", 2)
        content = Path(path).read_text(encoding="utf-8")
        assert "extruder_count = 2" in content

    def test_single_extruder_multi_material_not_set(self) -> None:
        """single_extruder_multi_material must NOT be 1 — PS 2.9 CLI silently
        produces no output when this flag is enabled."""
        path = resolve_multiextruder_profile("bambu_a1", 2)
        content = Path(path).read_text(encoding="utf-8")
        assert "single_extruder_multi_material = 1" not in content

    def test_layer_gcode_set(self) -> None:
        """layer_gcode = G92 E0 resets relative extruder counter each layer."""
        path = resolve_multiextruder_profile("bambu_a1", 2)
        content = Path(path).read_text(encoding="utf-8")
        assert "layer_gcode = G92 E0" in content

    def test_nozzle_diameter_expanded(self) -> None:
        """nozzle_diameter should be repeated N times, semicolon-joined."""
        path = resolve_multiextruder_profile("bambu_a1", 2)
        content = Path(path).read_text(encoding="utf-8")
        assert "nozzle_diameter = 0.4;0.4" in content

    def test_temperature_expanded(self) -> None:
        path = resolve_multiextruder_profile("bambu_a1", 2)
        content = Path(path).read_text(encoding="utf-8")
        # bambu_a1 temp is 220; should be 220;220 for 2-extruder
        assert "temperature = 220;220" in content

    def test_four_extruder_semicolons(self) -> None:
        path = resolve_multiextruder_profile("bambu_a1", 4)
        content = Path(path).read_text(encoding="utf-8")
        assert "extruder_count = 4" in content
        assert "nozzle_diameter = 0.4;0.4;0.4;0.4" in content

    def test_base_bambu_settings_preserved(self) -> None:
        """Bambu-critical settings (empty gcode, relative E) must survive."""
        path = resolve_multiextruder_profile("bambu_a1", 2)
        content = Path(path).read_text(encoding="utf-8")
        assert "use_relative_e_distances = 1" in content
        assert "start_gcode = " in content
        assert "end_gcode = " in content

    def test_cache_returns_same_path(self) -> None:
        path1 = resolve_multiextruder_profile("bambu_a1", 2)
        path2 = resolve_multiextruder_profile("bambu_a1", 2)
        assert path1 == path2

    def test_different_num_extruders_different_path(self) -> None:
        path2 = resolve_multiextruder_profile("bambu_a1", 2)
        path4 = resolve_multiextruder_profile("bambu_a1", 4)
        assert path2 != path4

    def test_overrides_applied(self) -> None:
        path = resolve_multiextruder_profile(
            "bambu_a1", 2, overrides={"layer_height": "0.1"}
        )
        content = Path(path).read_text(encoding="utf-8")
        assert "layer_height = 0.1" in content

    def test_invalid_num_extruders_raises(self) -> None:
        with pytest.raises(ValueError, match="num_extruders must be"):
            resolve_multiextruder_profile("bambu_a1", 0)

    def test_num_extruders_too_large_raises(self) -> None:
        with pytest.raises(ValueError, match="num_extruders must be"):
            resolve_multiextruder_profile("bambu_a1", 17)

    def test_does_not_mutate_base_profile(self) -> None:
        """Base profile settings must not be affected by multi-extruder expansion."""
        base_before = get_slicer_profile("bambu_a1").settings["nozzle_diameter"]
        resolve_multiextruder_profile("bambu_a1", 2)
        base_after = get_slicer_profile("bambu_a1").settings["nozzle_diameter"]
        assert base_before == base_after  # still single value, not expanded
