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
        assert profile.display_name == "Creality Ender 3 / Ender 3 Pro"
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

    def test_creality_k1_max_profile(self) -> None:
        profile = get_slicer_profile("k1_max")
        assert profile.id == "k1_max"
        assert profile.slicer == "orcaslicer"
        assert profile.settings["bed_shape"] == "0x0,300x0,300x300,0x300"
        assert profile.settings["max_print_height"] == "300"

    def test_creality_brand_prefixed_alias(self) -> None:
        profile = get_slicer_profile("creality_k1_max")
        assert profile.id == "k1_max"

    def test_sparkx_i7_profile(self) -> None:
        profile = get_slicer_profile("sparkx_i7")
        assert profile.settings["bed_shape"] == "0x0,260x0,260x260,0x260"
        assert "four-color" in profile.notes

    def test_ender3_v4_profile(self) -> None:
        profile = get_slicer_profile("ender3_v4")
        assert profile.settings["max_print_height"] == "235"
        assert "CFS" in profile.notes

    def test_qidi_x_plus3_profile(self) -> None:
        profile = get_slicer_profile("qidi_x_plus3")
        assert profile.id == "qidi_x_plus3"
        assert profile.display_name == "QIDI X-Plus 3"
        assert profile.slicer == "orcaslicer"
        assert profile.settings["gcode_flavor"] == "klipper"
        assert profile.settings["bed_shape"] == "0x0,280x0,280x280,0x280"
        assert profile.settings["max_print_height"] == "270"

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

    def test_cfs_tool_changes_are_hardware_unverified(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in raw.items():
            if key.startswith("_"):
                continue
            tool_change = data.get("tool_change", {})
            if tool_change.get("tool_changer") == "cfs":
                assert tool_change.get("hardware_unverified") is True, (
                    f"Profile '{key}' must mark CFS slot control hardware-unverified"
                )
                assert tool_change.get("control_mode") == "firmware_gcode_or_creality_print"


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


# ===================================================================
# Relative-E profiles must reset E each layer
# ===================================================================

class TestRelativeExtrusionNeedsLayerReset:
    """A relative-E profile with no per-layer E reset does not slice.

    PrusaSlicer refuses such a profile on a Marlin flavour: it writes the
    reason to stderr, produces no gcode, and **exits 0**.  Kiln saw a clean
    exit and a missing file, so seven bundled Bambu profiles — the P2S, P1S,
    P1P, X1C, X1E, H2S and A1 mini — reported "Slicer completed but output
    file was not created" for every single-material job.  Only the A1 and A2L
    had declared a ``layer_gcode`` of their own.

    Measured against PrusaSlicer 2.9.4: the check is a whitespace- and
    case-insensitive search for ``G92 E0`` anywhere in ``layer_gcode``.
    """

    def _ini(self, path: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.lstrip().startswith("#"):
                key, val = raw.split("=", 1)
                out[key.strip()] = val.strip()
        return out

    def test_every_bundled_relative_e_profile_resets_e(self) -> None:
        """The whole bundle, so a tenth Bambu profile cannot reintroduce it."""
        offenders = []
        for pid in list_slicer_profiles():
            settings = get_slicer_profile(pid).settings
            if str(settings.get("use_relative_e_distances", "0")).strip() != "1":
                continue
            emitted = self._ini(resolve_slicer_profile(pid))
            flat = "".join(emitted.get("layer_gcode", "").split()).lower()
            if "g92e0" not in flat:
                offenders.append(pid)
        assert not offenders, (
            f"relative-E profiles that PrusaSlicer will silently refuse: {offenders}"
        )

    def test_bambu_p2s_specifically(self) -> None:
        """The machine the field report came from."""
        emitted = self._ini(resolve_slicer_profile("bambu_p2s"))
        assert emitted["use_relative_e_distances"] == "1"
        assert "G92 E0" in emitted["layer_gcode"]

    def test_absolute_e_profile_gets_no_reset(self) -> None:
        """The guard that keeps this fix from becoming a worse bug.

        A per-layer ``G92 E0`` under absolute extrusion resets the extruder
        counter mid-print; the next absolute E value would push a whole
        layer's filament in one move.
        """
        for pid in ("prusa_mk4", "ender3"):
            settings = get_slicer_profile(pid).settings
            assert str(settings.get("use_relative_e_distances", "0")) != "1"
            assert "layer_gcode" not in self._ini(resolve_slicer_profile(pid))

    def test_declared_layer_gcode_is_not_overwritten(self) -> None:
        """bambu_a1 ships its own value; it must survive untouched."""
        emitted = self._ini(resolve_slicer_profile("bambu_a1"))
        assert emitted["layer_gcode"] == "G92 E0"

    def test_caller_layer_gcode_is_kept_and_extended(self) -> None:
        """An override without an E reset gets one, and keeps what it said."""
        emitted = self._ini(
            resolve_slicer_profile("bambu_p2s", overrides={"layer_gcode": "M117 layer"})
        )
        assert "M117 layer" in emitted["layer_gcode"]
        assert "G92 E0" in emitted["layer_gcode"]

    def test_alternate_spellings_are_recognised(self) -> None:
        """Match PrusaSlicer's own normalisation, so we never double the reset."""
        for spelling in ("G92E0", "g92 e0", "  G92  E0  "):
            emitted = self._ini(
                resolve_slicer_profile(
                    "bambu_p2s", overrides={"layer_gcode": spelling}
                )
            )
            assert emitted["layer_gcode"].count("92") == 1, spelling

    def test_override_switching_relative_e_on_is_covered(self) -> None:
        """The invariant runs after the merge, not before it."""
        emitted = self._ini(
            resolve_slicer_profile("ender3", overrides={"use_relative_e_distances": "1"})
        )
        assert "G92 E0" in emitted["layer_gcode"]

    def test_profile_with_overrides_door_is_covered(self) -> None:
        """The door a Bambu with an unmappable model actually goes through.

        ``slice_and_print`` pushes ``use_relative_e_distances=1`` into
        :func:`profile_with_overrides` for a printer whose TYPE is bambu and
        whose model is unset — that path writes an .ini too.
        """
        from kiln.slicer_profiles import profile_with_overrides

        path = profile_with_overrides(
            None, {"use_relative_e_distances": "1", "start_gcode": "", "end_gcode": ""}
        )
        assert path is not None
        assert "G92 E0" in self._ini(path)["layer_gcode"]

    def test_multiextruder_door_is_covered(self) -> None:
        """The AMS path kept its own copy of this rule; it now shares one."""
        emitted = self._ini(resolve_multiextruder_profile("bambu_p2s", 2))
        assert "G92 E0" in emitted["layer_gcode"]

    def test_multiextruder_absolute_e_gets_no_reset(self) -> None:
        """The AMS builder used to inject the reset unconditionally."""
        emitted = self._ini(resolve_multiextruder_profile("prusa_mk4", 2))
        assert "layer_gcode" not in emitted
