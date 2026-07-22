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

import pytest

from kiln.printer_intelligence import (
    _DATA_FILE,
    FailureMode,
    MaterialProfile,
    diagnose_issue,
    get_material_settings,
    get_printer_intel,
    get_slicer_speed_overrides,
    intel_to_dict,
    list_intel_profiles,
)
from kiln.slicer_profiles import _DATA_FILE as _SLICER_DATA_FILE

from .conftest import requires_printer_intelligence_overlay

CREALITY_PROFILE_IDS = {
    "ender3",
    "ender3_s1",
    "ender5",
    "cr10",
    "ender3_v2",
    "sparkx_i7",
    "k1",
    "k1_max",
    "k1c",
    "k1_se",
    "k2",
    "k2_pro",
    "k2_plus",
    "k2_se",
    "creality_hi",
    "ender3_v4",
    "ender3_v3",
    "ender3_v3_ke",
    "ender3_v3_se",
    "ender3_v3_plus",
    "ender5_max",
    "cr10_se",
}

CREALITY_CAPABILITY_KEYS = {
    "has_camera",
    "camera",
    "camera_out_of_box",
    "camera_optional",
    "camera_options",
    "multicolor_system",
    "multicolor_out_of_box",
    "multicolor_optional",
    "multicolor_options",
    "multicolor_max_colors",
    "multicolor_max_colors_out_of_box",
    "cfs_compatible",
    "cfs_variant",
    "cfs_max_units",
    "hardened_nozzle_stock",
    "input_shaping",
    "filament_runout_sensor",
    "power_loss_recovery",
    "enclosure",
    # Sourcing rides the merged overlay under the underscore convention
    # (internal QA trail — stripped at the wire boundary, kept locally).
    "_source_notes",
}

EXTENDED_HARDWARE_FIELDS = {
    "ams_slots",
    "ams_type",
    "camera",
    "nozzle_options",
    "max_nozzle_temp",
    "max_speed_mm_s",
    "max_acceleration_mm_s2",
    "wifi",
}
NON_MODEL_PROFILES = {"default", "klipper_generic"}

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

    def test_creality_k1_max_intel(self) -> None:
        intel = get_printer_intel("k1_max")
        assert intel.id == "k1_max"
        assert intel.firmware == "klipper"
        assert intel.has_enclosure is True
        assert intel.materials["PLA"].bed == 55
        assert intel.capabilities["has_camera"] is True
        assert intel.capabilities["camera_out_of_box"] is True
        assert intel.capabilities["multicolor_system"] == "cfs_c"
        assert intel.capabilities["multicolor_out_of_box"] is False
        assert intel.capabilities["multicolor_optional"] is True
        assert intel.capabilities["multicolor_max_colors"] == 4

    def test_creality_brand_prefixed_alias(self) -> None:
        intel = get_printer_intel("creality_k1_max")
        assert intel.id == "k1_max"

    def test_ender3_v3_ke_volume_and_firmware(self) -> None:
        intel = get_printer_intel("ender3_v3_ke")
        assert intel.firmware == "klipper"
        assert intel.has_enclosure is False

    def test_sparkx_i7_multicolor_capabilities(self) -> None:
        intel = get_printer_intel("sparkx_i7")
        assert intel.capabilities["multicolor_system"] == "cfs"
        assert intel.capabilities["multicolor_out_of_box"] is True
        assert intel.capabilities["multicolor_max_colors"] == 4
        assert intel.capabilities["camera_out_of_box"] is True

    def test_ender3_v4_cfs_capability(self) -> None:
        intel = get_printer_intel("ender3_v4")
        assert intel.capabilities["cfs_compatible"] is True
        assert intel.capabilities["multicolor_out_of_box"] is False
        assert intel.capabilities["multicolor_optional"] is True
        assert intel.capabilities["camera_out_of_box"] is False

    def test_k1_series_cfs_c_optional_capabilities(self) -> None:
        for printer_id in ("k1", "k1_max", "k1c", "k1_se"):
            intel = get_printer_intel(printer_id)
            assert intel.capabilities["multicolor_system"] == "cfs_c"
            assert intel.capabilities["cfs_variant"] == "CFS-C"
            assert intel.capabilities["cfs_max_units"] == 1
            assert intel.capabilities["multicolor_max_colors"] == 4
            assert intel.capabilities["multicolor_out_of_box"] is False

    def test_k2_se_camera_is_optional(self) -> None:
        intel = get_printer_intel("k2_se")
        assert intel.capabilities["has_camera"] is False
        assert intel.capabilities["camera_out_of_box"] is False
        assert intel.capabilities["camera_optional"] is True
        assert intel.capabilities["multicolor_system"] == "cfs"
        assert intel.capabilities["multicolor_max_colors"] == 16

    def test_k2_family_hardened_nozzle_claims(self) -> None:
        for printer_id in ("k2", "k2_pro", "k2_plus"):
            intel = get_printer_intel(printer_id)
            assert intel.capabilities["hardened_nozzle_stock"] is True

    def test_ender5_max_optional_camera_no_cfs(self) -> None:
        intel = get_printer_intel("ender5_max")
        assert intel.capabilities["camera_optional"] is True
        assert intel.capabilities["cfs_compatible"] is False
        assert intel.capabilities["multicolor_system"] == "none"

    @requires_printer_intelligence_overlay
    def test_all_creality_profiles_have_capability_schema(self) -> None:
        for printer_id in CREALITY_PROFILE_IDS:
            intel = get_printer_intel(printer_id)
            assert set(intel.capabilities) >= CREALITY_CAPABILITY_KEYS, printer_id
            assert len(intel.quirks) >= 3, printer_id
            assert len(intel.failure_modes) >= 3, printer_id

    def test_creality_cfs_claims_align_with_slicer_tooling(self) -> None:
        slicer = json.loads(_SLICER_DATA_FILE.read_text())
        addon_compatible = set(
            slicer["_multi_material_addons"]["creality_cfs"]["compatible_printers"],
        )

        for printer_id in CREALITY_PROFILE_IDS:
            intel = get_printer_intel(printer_id)
            if not intel.capabilities["cfs_compatible"]:
                continue

            tool_change = slicer[printer_id]["tool_change"]
            covered_by_profile = tool_change["tool_changer"] == "cfs"
            covered_by_addon = printer_id in addon_compatible
            assert covered_by_profile or covered_by_addon, printer_id

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
        assert "qidi_x_plus3" in profiles

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

    def test_qidi_x_plus3_pc(self) -> None:
        mat = get_material_settings("qidi_x_plus3", "PC")
        assert mat is not None
        assert mat.hotend == 280
        assert mat.bed == 110

    def test_default_pla(self) -> None:
        mat = get_material_settings("default", "PLA")
        assert mat is not None
        assert mat.hotend == 210


# ===================================================================
# diagnose_issue
# ===================================================================

class TestDiagnoseIssue:
    """Tests for diagnose_issue() symptom matching."""

    @requires_printer_intelligence_overlay
    def test_ender3_under_extrusion(self) -> None:
        matches = diagnose_issue("ender3", "under-extrusion")
        assert len(matches) >= 1
        # Should find the PTFE tube / extruder arm failure mode
        symptoms = [m["symptom"] for m in matches]
        assert any("Under-extrusion" in s or "under-extrusion" in s.lower() for s in symptoms)

    @requires_printer_intelligence_overlay
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

    @requires_printer_intelligence_overlay
    def test_bambu_x1c_ams_issue(self) -> None:
        matches = diagnose_issue("bambu_x1c", "AMS")
        assert len(matches) >= 1

    @requires_printer_intelligence_overlay
    def test_qidi_x_plus3_firmware_issue(self) -> None:
        matches = diagnose_issue("qidi_x_plus3", "firmware")
        assert len(matches) >= 1
        assert any("Firmware update" in match["symptom"] for match in matches)


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
            "capabilities", "materials", "quirks", "calibration", "failure_modes",
        ]
        expected_keys.extend(sorted(EXTENDED_HARDWARE_FIELDS))
        for key in expected_keys:
            assert key in d, f"Missing key '{key}' in serialized dict"

    def test_materials_are_dicts(self) -> None:
        intel = get_printer_intel("ender3")
        d = intel_to_dict(intel)
        assert isinstance(d["materials"], dict)
        for _mat_name, mat_data in d["materials"].items():
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


class TestExtendedHardwareSpeedFallback:
    """Null hardware specs must not become integers in the speed fallback."""

    def test_builder_selected_speed_returns_no_override(self) -> None:
        assert get_slicer_speed_overrides("ratrig_vcore3") == {}

    def test_published_speed_with_unpublished_acceleration_uses_safe_default(self) -> None:
        overrides = get_slicer_speed_overrides("sovol_sv07")
        assert overrides["max_print_speed"] == "250"
        assert overrides["default_acceleration"] == "3500"


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

    def test_marketed_fleet_has_complete_extended_hardware_shape(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        marketed = {
            key: data
            for key, data in raw.items()
            if not key.startswith("_") and key not in NON_MODEL_PROFILES
        }
        assert len(marketed) == 55
        for printer_id, data in marketed.items():
            missing = EXTENDED_HARDWARE_FIELDS - set(data)
            assert not missing, f"{printer_id}: missing {sorted(missing)}"

    def test_extended_hardware_schema_pins_null_meaning_and_scope(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        schema = raw["_meta"]["extended_hardware_schema"]
        assert set(schema["fields"]) == EXTENDED_HARDWARE_FIELDS
        assert "manufacturer does not publish" in schema["null_semantics"]
        assert "default and klipper_generic" in schema["scope"]
        assert "not a recommended quality speed" in schema["max_speed_semantics"]

    def test_extended_hardware_types_and_thermal_ceiling_agree(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for printer_id, data in raw.items():
            if printer_id.startswith("_") or printer_id in NON_MODEL_PROFILES:
                continue
            assert data["ams_slots"] is None or isinstance(data["ams_slots"], int)
            assert data["ams_type"] is None or isinstance(data["ams_type"], str)
            assert data["camera"] is None or isinstance(data["camera"], str)
            assert data["nozzle_options"] is None or isinstance(
                data["nozzle_options"], list
            )
            assert data["max_nozzle_temp"] is None or (
                data["max_nozzle_temp"] == data["max_hotend_temp"]
            ), f"{printer_id}: extended and safety ceilings disagree"
            if data["ams_slots"] == 0:
                assert data["ams_type"] == "none"

    def test_x1c_records_ams_and_camera(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        x1c = raw["bambu_x1c"]
        assert x1c["ams_type"] == "ams"
        assert x1c["ams_slots"] == 4
        assert "1920x1080" in x1c["camera"]

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

    @requires_printer_intelligence_overlay
    def test_quirks_is_list(self) -> None:
        intel = get_printer_intel("ender3")
        assert isinstance(intel.quirks, list)
        assert len(intel.quirks) > 0

    def test_quirks_are_strings(self) -> None:
        intel = get_printer_intel("ender3")
        for q in intel.quirks:
            assert isinstance(q, str)

    @requires_printer_intelligence_overlay
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
