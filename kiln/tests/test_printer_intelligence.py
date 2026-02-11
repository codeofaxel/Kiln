"""Tests for printer intelligence database — firmware quirks, material
compatibility, calibration guidance, and known failure modes.

Covers:
    - get_printer_intel() for known printers (ender3, bambu_x1c)
    - get_printer_intel() fallback to default for unknown printers
    - get_printer_intel() case normalization
    - list_intel_profiles() returns sorted list
    - get_material_settings() for known materials (PLA, PA-CF)
    - get_material_settings() returns None for unknown material
    - diagnose_issue() matches known symptoms
    - diagnose_issue() returns empty list for unknown symptoms
    - intel_to_dict() serialization includes all fields
    - JSON data file validity — required fields present
    - MaterialProfile dataclass fields and immutability
    - FailureMode dataclass fields and immutability
    - PrinterIntel quirks is a list
    - PrinterIntel calibration is a dict
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from kiln.printer_intelligence import (
    MaterialProfile,
    FailureMode,
    PrinterIntel,
    get_printer_intel,
    list_intel_profiles,
    get_material_settings,
    diagnose_issue,
    intel_to_dict,
    _DATA_FILE,
)


# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture(autouse=True)
def _reset_intel_cache():
    """Reset the singleton cache before each test for isolation."""
    import kiln.printer_intelligence as mod
    mod._cache.clear()
    mod._loaded = False
    yield
    mod._cache.clear()
    mod._loaded = False


# ===================================================================
# get_printer_intel
# ===================================================================

class TestGetPrinterIntel:
    """Tests for get_printer_intel() lookup and fallback logic."""

    def test_ender3_intel_exists(self) -> None:
        intel = get_printer_intel("ender3")
        assert intel.id == "ender3"
        assert intel.display_name == "Creality Ender 3 / Ender 3 Pro / Ender 3 V2"

    def test_ender3_firmware(self) -> None:
        intel = get_printer_intel("ender3")
        assert intel.firmware == "marlin"

    def test_ender3_extruder_type(self) -> None:
        intel = get_printer_intel("ender3")
        assert intel.extruder_type == "bowden"

    def test_ender3_hotend_type(self) -> None:
        intel = get_printer_intel("ender3")
        assert intel.hotend_type == "ptfe_lined"

    def test_bambu_x1c_intel(self) -> None:
        intel = get_printer_intel("bambu_x1c")
        assert intel.id == "bambu_x1c"
        assert intel.firmware == "bambu"
        assert intel.hotend_type == "all_metal"
        assert intel.has_enclosure is True
        assert intel.has_abl is True

    def test_bambu_x1c_extruder(self) -> None:
        intel = get_printer_intel("bambu_x1c")
        assert intel.extruder_type == "direct_drive"

    def test_nonexistent_falls_back_to_default(self) -> None:
        intel = get_printer_intel("nonexistent_printer_xyz")
        assert intel.id == "default"
        assert intel.display_name == "Generic FDM Printer"

    def test_case_normalization(self) -> None:
        intel = get_printer_intel("Ender3")
        assert intel.id == "ender3"

    def test_hyphen_normalization(self) -> None:
        """'Ender-3' normalizes hyphens to underscores ('ender_3').

        Since 'ender_3' doesn't exactly match 'ender3' and the prefix
        fuzzy match also fails, this falls back to default.
        """
        intel = get_printer_intel("Ender-3")
        assert intel.id == "default"

    def test_whitespace_stripping(self) -> None:
        intel = get_printer_intel("  bambu_x1c  ")
        assert intel.id == "bambu_x1c"


# ===================================================================
# list_intel_profiles
# ===================================================================

class TestListIntelProfiles:
    """Tests for list_intel_profiles() output."""

    def test_returns_sorted_list(self) -> None:
        profiles = list_intel_profiles()
        assert profiles == sorted(profiles)

    def test_contains_expected_profiles(self) -> None:
        profiles = list_intel_profiles()
        assert "default" in profiles
        assert "ender3" in profiles
        assert "bambu_x1c" in profiles

    def test_returns_list_of_strings(self) -> None:
        profiles = list_intel_profiles()
        assert isinstance(profiles, list)
        assert all(isinstance(p, str) for p in profiles)

    def test_no_meta_key(self) -> None:
        profiles = list_intel_profiles()
        assert "_meta" not in profiles


# ===================================================================
# get_material_settings
# ===================================================================

class TestGetMaterialSettings:
    """Tests for get_material_settings() lookup."""

    def test_ender3_pla(self) -> None:
        mat = get_material_settings("ender3", "PLA")
        assert mat is not None
        assert isinstance(mat, MaterialProfile)
        assert mat.hotend == 200
        assert mat.bed == 60
        assert mat.fan == 100

    def test_ender3_pla_case_insensitive(self) -> None:
        """Material lookup normalizes to upper case."""
        mat = get_material_settings("ender3", "pla")
        assert mat is not None
        assert mat.hotend == 200

    def test_ender3_unknown_material(self) -> None:
        mat = get_material_settings("ender3", "UNKNOWN_MATERIAL")
        assert mat is None

    def test_bambu_x1c_pa_cf(self) -> None:
        """Bambu X1C supports high-temp materials like PA-CF."""
        mat = get_material_settings("bambu_x1c", "PA-CF")
        assert mat is not None
        assert mat.hotend == 280
        assert mat.bed == 100

    def test_bambu_x1c_pc(self) -> None:
        mat = get_material_settings("bambu_x1c", "PC")
        assert mat is not None
        assert mat.hotend == 270

    def test_default_pla(self) -> None:
        mat = get_material_settings("default", "PLA")
        assert mat is not None
        assert mat.hotend == 210


# ===================================================================
# diagnose_issue
# ===================================================================

class TestDiagnoseIssue:
    """Tests for diagnose_issue() symptom matching."""

    def test_ender3_under_extrusion(self) -> None:
        matches = diagnose_issue("ender3", "under-extrusion")
        assert len(matches) >= 1
        # Should find the PTFE tube / extruder arm failure mode
        symptoms = [m["symptom"] for m in matches]
        assert any("Under-extrusion" in s or "under-extrusion" in s.lower() for s in symptoms)

    def test_ender3_stringing(self) -> None:
        matches = diagnose_issue("ender3", "stringing")
        assert len(matches) >= 1

    def test_ender3_nonexistent_symptom(self) -> None:
        matches = diagnose_issue("ender3", "nonexistent symptom xyz123")
        assert matches == []

    def test_match_contains_required_keys(self) -> None:
        matches = diagnose_issue("ender3", "under-extrusion")
        for m in matches:
            assert "symptom" in m
            assert "cause" in m
            assert "fix" in m

    def test_default_has_no_failure_modes(self) -> None:
        """Default profile has empty failure_modes."""
        matches = diagnose_issue("default", "anything")
        assert matches == []

    def test_bambu_x1c_ams_issue(self) -> None:
        matches = diagnose_issue("bambu_x1c", "AMS")
        assert len(matches) >= 1


# ===================================================================
# intel_to_dict
# ===================================================================

class TestIntelToDict:
    """Tests for intel_to_dict() serialization."""

    def test_contains_all_fields(self) -> None:
        intel = get_printer_intel("ender3")
        d = intel_to_dict(intel)
        expected_keys = [
            "id", "display_name", "firmware", "extruder_type",
            "hotend_type", "has_enclosure", "has_abl",
            "materials", "quirks", "calibration", "failure_modes",
        ]
        for key in expected_keys:
            assert key in d, f"Missing key '{key}' in serialized dict"

    def test_materials_are_dicts(self) -> None:
        intel = get_printer_intel("ender3")
        d = intel_to_dict(intel)
        assert isinstance(d["materials"], dict)
        for mat_name, mat_data in d["materials"].items():
            assert "hotend" in mat_data
            assert "bed" in mat_data
            assert "fan" in mat_data
            assert "notes" in mat_data

    def test_failure_modes_are_dicts(self) -> None:
        intel = get_printer_intel("ender3")
        d = intel_to_dict(intel)
        assert isinstance(d["failure_modes"], list)
        for fm in d["failure_modes"]:
            assert "symptom" in fm
            assert "cause" in fm
            assert "fix" in fm

    def test_is_json_serializable(self) -> None:
        intel = get_printer_intel("bambu_x1c")
        d = intel_to_dict(intel)
        serialized = json.dumps(d)
        assert isinstance(serialized, str)

    def test_roundtrip_id_matches(self) -> None:
        intel = get_printer_intel("ender3")
        d = intel_to_dict(intel)
        assert d["id"] == intel.id
        assert d["firmware"] == intel.firmware


# ===================================================================
# JSON data file validity
# ===================================================================

class TestPrinterIntelligenceJSON:
    """Tests for the bundled printer_intelligence.json data file."""

    REQUIRED_PROFILE_FIELDS = [
        "display_name", "firmware", "extruder_type", "hotend_type",
    ]

    def test_json_file_exists_and_parses(self) -> None:
        assert _DATA_FILE.exists()
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)

    def test_all_profiles_have_required_fields(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in raw.items():
            if key == "_meta":
                continue
            for req_field in self.REQUIRED_PROFILE_FIELDS:
                assert req_field in data, (
                    f"Profile '{key}' missing required field '{req_field}'"
                )

    def test_all_profiles_have_materials_dict(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in raw.items():
            if key == "_meta":
                continue
            assert "materials" in data, f"Profile '{key}' missing 'materials'"
            assert isinstance(data["materials"], dict)

    def test_all_material_entries_have_required_keys(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in raw.items():
            if key == "_meta":
                continue
            for mat_name, mat_data in data.get("materials", {}).items():
                assert "hotend" in mat_data, (
                    f"Profile '{key}' material '{mat_name}' missing 'hotend'"
                )
                assert "bed" in mat_data, (
                    f"Profile '{key}' material '{mat_name}' missing 'bed'"
                )
                assert "fan" in mat_data, (
                    f"Profile '{key}' material '{mat_name}' missing 'fan'"
                )


# ===================================================================
# MaterialProfile dataclass
# ===================================================================

class TestMaterialProfile:
    """Tests for the MaterialProfile dataclass."""

    def test_fields(self) -> None:
        mp = MaterialProfile(hotend=200, bed=60, fan=100, notes="Test")
        assert mp.hotend == 200
        assert mp.bed == 60
        assert mp.fan == 100
        assert mp.notes == "Test"

    def test_default_notes(self) -> None:
        mp = MaterialProfile(hotend=200, bed=60, fan=100)
        assert mp.notes == ""

    def test_frozen(self) -> None:
        mp = MaterialProfile(hotend=200, bed=60, fan=100)
        with pytest.raises(FrozenInstanceError):
            mp.hotend = 999  # type: ignore[misc]


# ===================================================================
# FailureMode dataclass
# ===================================================================

class TestFailureMode:
    """Tests for the FailureMode dataclass."""

    def test_fields(self) -> None:
        fm = FailureMode(symptom="Jam", cause="Dirt", fix="Clean")
        assert fm.symptom == "Jam"
        assert fm.cause == "Dirt"
        assert fm.fix == "Clean"

    def test_frozen(self) -> None:
        fm = FailureMode(symptom="Jam", cause="Dirt", fix="Clean")
        with pytest.raises(FrozenInstanceError):
            fm.symptom = "Other"  # type: ignore[misc]


# ===================================================================
# PrinterIntel structure
# ===================================================================

class TestPrinterIntelStructure:
    """Tests for PrinterIntel field types and structure."""

    def test_quirks_is_list(self) -> None:
        intel = get_printer_intel("ender3")
        assert isinstance(intel.quirks, list)
        assert len(intel.quirks) > 0

    def test_quirks_are_strings(self) -> None:
        intel = get_printer_intel("ender3")
        for q in intel.quirks:
            assert isinstance(q, str)

    def test_calibration_is_dict(self) -> None:
        intel = get_printer_intel("ender3")
        assert isinstance(intel.calibration, dict)
        assert len(intel.calibration) > 0

    def test_calibration_values_are_strings(self) -> None:
        intel = get_printer_intel("ender3")
        for key, val in intel.calibration.items():
            assert isinstance(key, str)
            assert isinstance(val, str)

    def test_failure_modes_is_list_of_failure_mode(self) -> None:
        intel = get_printer_intel("ender3")
        assert isinstance(intel.failure_modes, list)
        for fm in intel.failure_modes:
            assert isinstance(fm, FailureMode)

    def test_materials_is_dict_of_material_profile(self) -> None:
        intel = get_printer_intel("ender3")
        assert isinstance(intel.materials, dict)
        for mat_name, mat in intel.materials.items():
            assert isinstance(mat_name, str)
            assert isinstance(mat, MaterialProfile)

    def test_default_quirks_is_empty(self) -> None:
        intel = get_printer_intel("default")
        assert intel.quirks == []

    def test_frozen(self) -> None:
        intel = get_printer_intel("ender3")
        with pytest.raises(FrozenInstanceError):
            intel.firmware = "klipper"  # type: ignore[misc]
