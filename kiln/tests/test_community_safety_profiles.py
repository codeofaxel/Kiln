"""Tests for community safety profile contribution mechanism.

Covers:
- validate_safety_profile() — valid profile, missing fields, out-of-range values,
  type errors, optional field validation
- add_community_profile() — save and retrieve, persistence to disk, source tagging
- export_profile() — export bundled profile, export community profile
- Community profiles override bundled profiles in get_profile()
- list_community_profiles() — empty state, after adding profiles
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

from kiln.safety_profiles import (
    SafetyProfile,
    add_community_profile,
    export_profile,
    get_profile,
    list_community_profiles,
    list_profiles,
    profile_to_dict,
    validate_safety_profile,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _valid_profile() -> Dict[str, Any]:
    """Return a minimal valid community profile dict."""
    return {
        "display_name": "Test Printer XL",
        "max_hotend_temp": 280.0,
        "max_bed_temp": 100.0,
        "max_feedrate": 500.0,
        "build_volume": [300, 300, 350],
    }


@pytest.fixture(autouse=True)
def _reset_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reset module-level caches and redirect community file to tmp_path."""
    import kiln.safety_profiles as sp

    # Reset singleton caches so each test starts clean.
    sp._cache.clear()
    sp._community_cache.clear()
    sp._loaded = False
    sp._community_loaded = False

    # Redirect community file to a temp directory.
    community_dir = tmp_path / ".kiln"
    community_file = community_dir / "community_profiles.json"
    monkeypatch.setattr(sp, "_COMMUNITY_DIR", community_dir)
    monkeypatch.setattr(sp, "_COMMUNITY_FILE", community_file)

    yield

    # Cleanup caches after test.
    sp._cache.clear()
    sp._community_cache.clear()
    sp._loaded = False
    sp._community_loaded = False


# ---------------------------------------------------------------------------
# TestValidateSafetyProfile
# ---------------------------------------------------------------------------

class TestValidateSafetyProfile:
    """validate_safety_profile() — schema validation for candidate profiles."""

    def test_valid_profile_returns_no_errors(self):
        errors = validate_safety_profile(_valid_profile())
        assert errors == []

    def test_valid_profile_with_all_optional_fields(self):
        profile = _valid_profile()
        profile["max_chamber_temp"] = 60.0
        profile["min_safe_z"] = 0.0
        profile["max_volumetric_flow"] = 20.0
        profile["notes"] = "All options set."
        errors = validate_safety_profile(profile)
        assert errors == []

    def test_missing_max_hotend_temp(self):
        profile = _valid_profile()
        del profile["max_hotend_temp"]
        errors = validate_safety_profile(profile)
        assert any("max_hotend_temp" in e for e in errors)

    def test_missing_max_bed_temp(self):
        profile = _valid_profile()
        del profile["max_bed_temp"]
        errors = validate_safety_profile(profile)
        assert any("max_bed_temp" in e for e in errors)

    def test_missing_max_feedrate(self):
        profile = _valid_profile()
        del profile["max_feedrate"]
        errors = validate_safety_profile(profile)
        assert any("max_feedrate" in e for e in errors)

    def test_missing_build_volume(self):
        profile = _valid_profile()
        del profile["build_volume"]
        errors = validate_safety_profile(profile)
        assert any("build_volume" in e for e in errors)

    def test_missing_multiple_fields(self):
        errors = validate_safety_profile({})
        assert len(errors) == 4  # All 4 required fields missing.

    def test_hotend_temp_too_high(self):
        profile = _valid_profile()
        profile["max_hotend_temp"] = 501.0
        errors = validate_safety_profile(profile)
        assert any("max_hotend_temp" in e and "500" in e for e in errors)

    def test_hotend_temp_negative(self):
        profile = _valid_profile()
        profile["max_hotend_temp"] = -10.0
        errors = validate_safety_profile(profile)
        assert any("max_hotend_temp" in e for e in errors)

    def test_bed_temp_too_high(self):
        profile = _valid_profile()
        profile["max_bed_temp"] = 600.0
        errors = validate_safety_profile(profile)
        assert any("max_bed_temp" in e for e in errors)

    def test_feedrate_too_high(self):
        profile = _valid_profile()
        profile["max_feedrate"] = 50001.0
        errors = validate_safety_profile(profile)
        assert any("max_feedrate" in e and "50000" in e for e in errors)

    def test_feedrate_negative(self):
        profile = _valid_profile()
        profile["max_feedrate"] = -1.0
        errors = validate_safety_profile(profile)
        assert any("max_feedrate" in e for e in errors)

    def test_build_volume_wrong_length(self):
        profile = _valid_profile()
        profile["build_volume"] = [100, 100]
        errors = validate_safety_profile(profile)
        assert any("build_volume" in e and "3" in e for e in errors)

    def test_build_volume_not_list(self):
        profile = _valid_profile()
        profile["build_volume"] = "big"
        errors = validate_safety_profile(profile)
        assert any("build_volume" in e for e in errors)

    def test_build_volume_zero_dimension(self):
        profile = _valid_profile()
        profile["build_volume"] = [0, 200, 200]
        errors = validate_safety_profile(profile)
        assert any("build_volume[0]" in e and "positive" in e for e in errors)

    def test_build_volume_negative_dimension(self):
        profile = _valid_profile()
        profile["build_volume"] = [200, -5, 200]
        errors = validate_safety_profile(profile)
        assert any("build_volume[1]" in e for e in errors)

    def test_build_volume_non_numeric(self):
        profile = _valid_profile()
        profile["build_volume"] = [200, "big", 200]
        errors = validate_safety_profile(profile)
        assert any("build_volume[1]" in e and "number" in e for e in errors)

    def test_temperature_type_string(self):
        profile = _valid_profile()
        profile["max_hotend_temp"] = "hot"
        errors = validate_safety_profile(profile)
        assert any("max_hotend_temp" in e and "number" in e for e in errors)

    def test_feedrate_type_string(self):
        profile = _valid_profile()
        profile["max_feedrate"] = "fast"
        errors = validate_safety_profile(profile)
        assert any("max_feedrate" in e and "number" in e for e in errors)

    def test_chamber_temp_out_of_range(self):
        profile = _valid_profile()
        profile["max_chamber_temp"] = 501.0
        errors = validate_safety_profile(profile)
        assert any("max_chamber_temp" in e for e in errors)

    def test_chamber_temp_none_is_valid(self):
        profile = _valid_profile()
        profile["max_chamber_temp"] = None
        errors = validate_safety_profile(profile)
        assert errors == []

    def test_boundary_temps_valid(self):
        profile = _valid_profile()
        profile["max_hotend_temp"] = 0
        profile["max_bed_temp"] = 500
        profile["max_feedrate"] = 0
        errors = validate_safety_profile(profile)
        assert errors == []

    def test_integer_values_accepted(self):
        profile = _valid_profile()
        profile["max_hotend_temp"] = 260
        profile["max_bed_temp"] = 100
        profile["max_feedrate"] = 500
        errors = validate_safety_profile(profile)
        assert errors == []


# ---------------------------------------------------------------------------
# TestAddCommunityProfile
# ---------------------------------------------------------------------------

class TestAddCommunityProfile:
    """add_community_profile() — save, retrieve, and persistence."""

    def test_add_and_retrieve(self):
        add_community_profile("test_printer", _valid_profile())
        profile = get_profile("test_printer")
        assert profile.id == "test_printer"
        assert profile.max_hotend_temp == 280.0
        assert profile.display_name == "Test Printer XL"

    def test_normalises_model_name(self):
        add_community_profile("My-Custom-Printer", _valid_profile())
        profile = get_profile("my_custom_printer")
        assert profile.id == "my_custom_printer"

    def test_persists_to_disk(self, tmp_path: Path):
        import kiln.safety_profiles as sp

        add_community_profile("disk_test", _valid_profile())
        assert sp._COMMUNITY_FILE.exists()

        raw = json.loads(sp._COMMUNITY_FILE.read_text(encoding="utf-8"))
        assert "disk_test" in raw
        assert raw["disk_test"]["max_hotend_temp"] == 280.0

    def test_invalid_profile_raises_value_error(self):
        bad = {"max_hotend_temp": 999}
        with pytest.raises(ValueError, match="Invalid safety profile"):
            add_community_profile("bad_printer", bad)

    def test_source_tagging_in_notes(self):
        add_community_profile("tagged", _valid_profile(), source="john_bass")
        profile = get_profile("tagged")
        assert "[source: john_bass]" in profile.notes

    def test_default_source_does_not_add_tag(self):
        add_community_profile("no_tag", _valid_profile())
        profile = get_profile("no_tag")
        assert "[source:" not in profile.notes

    def test_overwrite_existing_community_profile(self):
        add_community_profile("overwrite_me", _valid_profile())
        updated = _valid_profile()
        updated["max_hotend_temp"] = 300.0
        add_community_profile("overwrite_me", updated)
        profile = get_profile("overwrite_me")
        assert profile.max_hotend_temp == 300.0

    def test_optional_fields_default(self):
        add_community_profile("minimal", _valid_profile())
        profile = get_profile("minimal")
        assert profile.max_chamber_temp is None
        assert profile.max_volumetric_flow is None
        assert profile.min_safe_z == 0.0

    def test_optional_fields_provided(self):
        p = _valid_profile()
        p["max_chamber_temp"] = 55.0
        p["max_volumetric_flow"] = 18.0
        p["min_safe_z"] = 0.2
        add_community_profile("full_opts", p)
        profile = get_profile("full_opts")
        assert profile.max_chamber_temp == 55.0
        assert profile.max_volumetric_flow == 18.0
        assert profile.min_safe_z == 0.2


# ---------------------------------------------------------------------------
# TestExportProfile
# ---------------------------------------------------------------------------

class TestExportProfile:
    """export_profile() — exporting profiles for sharing."""

    def test_export_community_profile(self):
        add_community_profile("exportable", _valid_profile())
        exported = export_profile("exportable")
        assert exported["max_hotend_temp"] == 280.0
        assert exported["max_bed_temp"] == 100.0
        assert exported["build_volume"] == [300, 300, 350]
        assert "id" not in exported  # ID is stripped for sharing.

    def test_export_bundled_profile(self):
        # The bundled data should load from the real JSON file.
        exported = export_profile("ender3")
        assert exported["max_hotend_temp"] == 260.0
        assert "id" not in exported

    def test_export_missing_falls_back_to_default(self):
        # get_profile() falls back to "default" when no match is found,
        # so export should return the default profile rather than raise.
        exported = export_profile("nonexistent_printer_xyz")
        assert exported["display_name"] == "Generic / Unknown Printer"

    def test_export_raises_when_no_default(self):
        import kiln.safety_profiles as sp

        # Clear the bundled cache so there's no default fallback.
        sp._loaded = True  # Prevent re-loading.
        sp._cache.clear()
        with pytest.raises(KeyError):
            export_profile("nonexistent_printer_xyz")

    def test_export_contains_all_fields(self):
        add_community_profile("full_export", _valid_profile())
        exported = export_profile("full_export")
        expected_keys = {
            "display_name", "max_hotend_temp", "max_bed_temp",
            "max_chamber_temp", "max_feedrate", "min_safe_z",
            "max_volumetric_flow", "build_volume", "notes",
        }
        assert set(exported.keys()) == expected_keys


# ---------------------------------------------------------------------------
# TestCommunityOverridesBundled
# ---------------------------------------------------------------------------

class TestCommunityOverridesBundled:
    """Community profiles take precedence over bundled profiles in get_profile()."""

    def test_community_overrides_bundled(self):
        # Bundled ender3 has max_hotend_temp=260.0
        bundled = get_profile("ender3")
        assert bundled.max_hotend_temp == 260.0

        # Add a community override with a higher limit.
        override = _valid_profile()
        override["max_hotend_temp"] = 300.0
        override["display_name"] = "Ender 3 (All-Metal Upgrade)"
        add_community_profile("ender3", override)

        overridden = get_profile("ender3")
        assert overridden.max_hotend_temp == 300.0
        assert overridden.display_name == "Ender 3 (All-Metal Upgrade)"

    def test_bundled_still_available_for_non_overridden(self):
        add_community_profile("custom_only", _valid_profile())
        # Bundled profiles should still be accessible.
        prusa = get_profile("prusa_mk4")
        assert prusa.max_hotend_temp == 300.0

    def test_list_profiles_includes_both(self):
        add_community_profile("community_only", _valid_profile())
        all_ids = list_profiles()
        assert "community_only" in all_ids
        assert "ender3" in all_ids  # Bundled profile.
        assert "default" in all_ids


# ---------------------------------------------------------------------------
# TestListCommunityProfiles
# ---------------------------------------------------------------------------

class TestListCommunityProfiles:
    """list_community_profiles() — listing user-local profiles."""

    def test_empty_when_no_community_profiles(self):
        result = list_community_profiles()
        assert result == []

    def test_returns_added_profiles(self):
        add_community_profile("alpha", _valid_profile())
        add_community_profile("beta", _valid_profile())
        result = list_community_profiles()
        assert result == ["alpha", "beta"]

    def test_sorted_alphabetically(self):
        add_community_profile("zulu", _valid_profile())
        add_community_profile("alpha", _valid_profile())
        add_community_profile("mike", _valid_profile())
        result = list_community_profiles()
        assert result == ["alpha", "mike", "zulu"]


# ---------------------------------------------------------------------------
# TestCommunityFileReload
# ---------------------------------------------------------------------------

class TestCommunityFileReload:
    """Community profiles loaded from disk on first access."""

    def test_profiles_loaded_from_existing_file(self, tmp_path: Path):
        import kiln.safety_profiles as sp

        # Pre-populate the community file.
        sp._COMMUNITY_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "preloaded": {
                "display_name": "Preloaded Printer",
                "max_hotend_temp": 250.0,
                "max_bed_temp": 90.0,
                "max_feedrate": 400.0,
                "build_volume": [200, 200, 200],
                "notes": "From disk.",
            }
        }
        sp._COMMUNITY_FILE.write_text(json.dumps(data), encoding="utf-8")

        # Force a fresh load.
        sp._community_loaded = False
        sp._community_cache.clear()

        result = list_community_profiles()
        assert "preloaded" in result

        profile = get_profile("preloaded")
        assert profile.max_hotend_temp == 250.0

    def test_corrupt_community_file_handled_gracefully(self, tmp_path: Path):
        import kiln.safety_profiles as sp

        sp._COMMUNITY_DIR.mkdir(parents=True, exist_ok=True)
        sp._COMMUNITY_FILE.write_text("not valid json {{{", encoding="utf-8")

        sp._community_loaded = False
        sp._community_cache.clear()

        # Should not raise — just logs an error and returns empty.
        result = list_community_profiles()
        assert result == []
