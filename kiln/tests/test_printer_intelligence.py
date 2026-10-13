"""Tests for printer intelligence database — firmware quirks, material
compatibility, calibration guidance, and known failure modes.

Since 2026-10-12 the bundled file is a roster and a real printer's spec
sheet is served per printer, so the curated printers here are the session
fixture's SYNTHETIC rows (``tests/fixtures/printer_catalogue_test.json``);
the real printers' real spec sheets and recipes are pinned beside the real
rows, in kiln-pro's ``tests/test_printer_catalogue_numbers.py``.  The tests
that need kiln-pro's curated overlay keep their real ids: the overlay is
keyed by them, and an install with the overlay has the catalogue too.

Covers:
    - get_printer_intel() for curated printers
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
import sys
from dataclasses import FrozenInstanceError
from unittest import mock

import pytest

from kiln.data_manifest import PUBLIC_PROFILE_FIELDS
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

from .conftest import requires_printer_intelligence_overlay

# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture(autouse=True)
def _reset_intel_cache():
    """Reset the profile caches before each test for isolation."""
    import kiln.printer_intelligence as mod
    mod._reset_caches()
    yield
    mod._reset_caches()


# ===================================================================
# get_printer_intel
# ===================================================================

class TestGetPrinterIntel:
    """Tests for get_printer_intel() lookup and fallback logic."""

    def test_lined_printer_intel_exists(self) -> None:
        intel = get_printer_intel("kilntest_ptfe")
        assert intel.id == "kilntest_ptfe"
        assert intel.display_name == "Kilntest Lined 235"

    def test_lined_printer_firmware(self) -> None:
        intel = get_printer_intel("kilntest_ptfe")
        assert intel.firmware == "marlin"

    def test_lined_printer_extruder_type(self) -> None:
        intel = get_printer_intel("kilntest_ptfe")
        assert intel.extruder_type == "bowden"

    def test_lined_printer_hotend_type(self) -> None:
        intel = get_printer_intel("kilntest_ptfe")
        assert intel.hotend_type == "ptfe_lined"

    def test_enclosed_printer_intel(self) -> None:
        intel = get_printer_intel("kilntest_enclosed")
        assert intel.id == "kilntest_enclosed"
        assert intel.firmware == "klipper"
        assert intel.hotend_type == "all_metal"
        assert intel.has_enclosure is True
        assert intel.has_abl is True
        assert intel.materials["PLA"].bed == 55

    def test_enclosed_printer_extruder(self) -> None:
        intel = get_printer_intel("kilntest_enclosed")
        assert intel.extruder_type == "direct_drive"

    def test_creality_brand_prefixed_alias(self) -> None:
        intel = get_printer_intel("creality_kilntest_enclosed")
        assert intel.id == "kilntest_enclosed"

    def test_open_klipper_printer_firmware_and_enclosure(self) -> None:
        intel = get_printer_intel("kilntest_big")
        assert intel.firmware == "klipper"
        assert intel.has_enclosure is False

    def test_a_roster_printer_whose_record_is_not_held_is_generic(self, signed_out_install) -> None:
        """A catalogue name with no record here (a signed-out install, on
        purpose) answers as the generic row; the served path is proved in
        test_pro_catalogue_bridge."""
        intel = get_printer_intel("kilntest_served")
        assert intel.id == "default"
        assert "kilntest_served" in list_intel_profiles()

    def test_nonexistent_falls_back_to_default(self) -> None:
        intel = get_printer_intel("nonexistent_printer_xyz")
        assert intel.id == "default"
        assert intel.display_name == "Generic FDM Printer"

    def test_case_normalization(self) -> None:
        intel = get_printer_intel("Kilntest_Ptfe")
        assert intel.id == "kilntest_ptfe"

    def test_hyphen_normalization(self) -> None:
        """'Kilntest-Ptfe' normalizes hyphens to underscores and finds its
        row; a spelling whose normalised form is no key and no prefix of
        one falls back to default."""
        intel = get_printer_intel("Kilntest-Ptfe")
        assert intel.id == "kilntest_ptfe"
        assert get_printer_intel("Kiln-Test-Ptfe").id == "default"

    def test_whitespace_stripping(self) -> None:
        intel = get_printer_intel("  kilntest_enclosed  ")
        assert intel.id == "kilntest_enclosed"

    @requires_printer_intelligence_overlay
    def test_cached_lookup_still_touches_canonical_overlay_loader(self) -> None:
        """A process cache must not bypass hosted private-read tracing."""
        get_printer_intel("kilntest_enclosed")
        with mock.patch(
            "kiln_pro.data_overlays.load_overlay",
            return_value={},
        ) as load_overlay:
            get_printer_intel("kilntest_enclosed")
        load_overlay.assert_called_once_with("printer_intelligence")


# ===================================================================
# Per-caller entitlement
# ===================================================================

class _FakeOverlayLoader:
    """Stand-in for kiln-pro's caller-aware ``load_overlay``.

    The real one refuses a caller who has not earned an overlay by raising;
    this reproduces that contract without needing kiln-pro installed, and
    counts calls so a test can tell "asked again" from "merged again".
    """

    def __init__(self, overlay: dict) -> None:
        self.overlay = overlay
        self.tier = "free"
        self.calls: list[str] = []

    def __call__(self, kind: str) -> dict:
        self.calls.append(kind)
        if self.tier == "free":
            raise RuntimeError(f"Caller tier is not entitled to '{kind}'.")
        return self.overlay


PAID_DEPTH = {
    "kilntest_ptfe": {
        "quirks": ["PTFE tube degrades above 240C."],
        "calibration": {"esteps": "Extrude 100mm, measure, rescale."},
        "failure_modes": [
            {
                "symptom": "under-extrusion",
                "cause": "clogged nozzle",
                "fix": "cold pull",
            }
        ],
        "materials": {"PLA": {"notes": "Curated per-printer note."}},
    }
}


@pytest.fixture
def overlay_loader(monkeypatch):
    """Install a fake kiln-pro whose overlay is gated on a switchable tier."""
    import types

    loader = _FakeOverlayLoader(PAID_DEPTH)
    data_overlays = types.ModuleType("kiln_pro.data_overlays")
    data_overlays.load_overlay = loader
    package = types.ModuleType("kiln_pro")
    package.data_overlays = data_overlays
    monkeypatch.setitem(sys.modules, "kiln_pro", package)
    monkeypatch.setitem(sys.modules, "kiln_pro.data_overlays", data_overlays)
    return loader


def _depth(printer_id: str = "kilntest_ptfe") -> tuple[int, int, int]:
    intel = get_printer_intel(printer_id)
    return len(intel.quirks), len(intel.calibration), len(intel.failure_modes)


def _read_as(loader: _FakeOverlayLoader, tier: str) -> tuple[int, int, int]:
    loader.tier = tier
    return _depth()


class TestPerCallerEntitlement:
    """The overlay merge is process-wide; the entitlement decision is not.

    Covers:
        - a paid caller keeps curated depth behind a free caller
        - a free caller keeps the public floor behind a paid caller
        - the answer does not depend on who warmed the process (the one
          that would have caught the frozen ``_loaded`` seam)
        - the public safety floor survives a refused overlay
        - the entitlement question is re-asked without re-merging
        - no kiln-pro at all still degrades to the public floor
    """

    def test_paid_caller_keeps_depth_after_a_free_caller_warms_the_process(
        self, overlay_loader
    ) -> None:
        assert _read_as(overlay_loader, "free") == (0, 0, 0)
        assert _read_as(overlay_loader, "pro") == (1, 1, 1)

    def test_free_caller_keeps_the_floor_after_a_paid_caller_warms_the_process(
        self, overlay_loader
    ) -> None:
        assert _read_as(overlay_loader, "pro") == (1, 1, 1)
        assert _read_as(overlay_loader, "free") == (0, 0, 0)

    def test_answer_does_not_depend_on_warm_up_order(self, overlay_loader) -> None:
        import kiln.printer_intelligence as mod

        mod._reset_caches()
        free_alone = _read_as(overlay_loader, "free")
        mod._reset_caches()
        paid_alone = _read_as(overlay_loader, "pro")
        # If these matched, the rest of the test would prove nothing.
        assert free_alone != paid_alone

        mod._reset_caches()
        free_first = _read_as(overlay_loader, "free")
        paid_second = _read_as(overlay_loader, "pro")

        mod._reset_caches()
        paid_first = _read_as(overlay_loader, "pro")
        free_second = _read_as(overlay_loader, "free")

        assert free_first == free_second == free_alone
        assert paid_first == paid_second == paid_alone

    def test_refused_overlay_still_serves_the_public_safety_floor(
        self, overlay_loader
    ) -> None:
        overlay_loader.tier = "free"
        intel = get_printer_intel("kilntest_ptfe")
        assert intel.display_name == "Kilntest Lined 235"
        assert intel.firmware == "marlin"
        assert intel.hotend_type == "ptfe_lined"
        # The temperatures a caller could hurt a printer with are the floor.
        assert intel.materials["PLA"].hotend == 200
        assert intel.materials["PLA"].bed == 60
        # Losing the overlay costs depth, never the catalogue.
        assert "kilntest_ptfe" in list_intel_profiles()

    def test_entitlement_is_re_asked_but_the_merge_is_not_repeated(
        self, overlay_loader
    ) -> None:
        overlay_loader.tier = "pro"
        first = get_printer_intel("kilntest_ptfe")
        second = get_printer_intel("kilntest_ptfe")
        # Asked every read — the entitlement answer is never cached...
        assert overlay_loader.calls == ["printer_intelligence"] * 2
        # ...but the decoded profile is, so no re-merge on the hot path.
        assert first is second

    def test_no_kiln_pro_installed_serves_the_public_floor(self, monkeypatch) -> None:
        # ``None`` in sys.modules is the documented "this import fails" marker.
        # The submodule needs its own: an already-imported ``kiln_pro.x`` is
        # served straight out of sys.modules without consulting its parent.
        monkeypatch.setitem(sys.modules, "kiln_pro", None)
        monkeypatch.setitem(sys.modules, "kiln_pro.data_overlays", None)
        assert _depth() == (0, 0, 0)
        assert get_printer_intel("kilntest_ptfe").materials["PLA"].hotend == 200


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
        # The roster lists a real printer by name whether or not its record
        # is held here, and the synthetic rows ride along in the session copy.
        assert "ender3" in profiles
        assert "bambu_x1c" in profiles
        assert "kilntest_ptfe" in profiles

    def test_returns_list_of_strings(self) -> None:
        profiles = list_intel_profiles()
        assert isinstance(profiles, list)
        assert all(isinstance(p, str) for p in profiles)

    def test_no_meta_key(self) -> None:
        profiles = list_intel_profiles()
        assert "_meta" not in profiles

    def test_a2l_speed_behavior_survives_public_field_removal(self) -> None:
        overrides = get_slicer_speed_overrides("bambu_a2l")
        assert overrides["max_print_speed"] == "187"
        assert overrides["default_acceleration"] == "7000"


# ===================================================================
# get_material_settings
# ===================================================================

class TestGetMaterialSettings:
    """Tests for get_material_settings() lookup."""

    def test_lined_printer_pla(self) -> None:
        mat = get_material_settings("kilntest_ptfe", "PLA")
        assert mat is not None
        assert isinstance(mat, MaterialProfile)
        assert mat.hotend == 200
        assert mat.bed == 60
        assert mat.fan == 100

    def test_pla_case_insensitive(self) -> None:
        """Material lookup normalizes to upper case."""
        mat = get_material_settings("kilntest_ptfe", "pla")
        assert mat is not None
        assert mat.hotend == 200

    def test_unknown_material(self) -> None:
        mat = get_material_settings("kilntest_ptfe", "UNKNOWN_MATERIAL")
        assert mat is None

    def test_enclosed_printer_pa_cf(self) -> None:
        """An enclosed machine carries high-temp recipes like PA-CF."""
        mat = get_material_settings("kilntest_enclosed", "PA-CF")
        assert mat is not None
        assert mat.hotend == 290
        assert mat.bed == 100

    def test_enclosed_printer_asa(self) -> None:
        mat = get_material_settings("kilntest_enclosed", "ASA")
        assert mat is not None
        assert mat.hotend == 260

    def test_a_recipe_the_row_does_not_carry_is_none(self) -> None:
        mat = get_material_settings("kilntest_ptfe", "PA-CF")
        assert mat is None

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

    def test_nonexistent_symptom(self) -> None:
        matches = diagnose_issue("kilntest_ptfe", "nonexistent symptom xyz123")
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
        intel = get_printer_intel("kilntest_ptfe")
        d = intel_to_dict(intel)
        expected_keys = [
            "id", "display_name", "firmware", "extruder_type",
            "hotend_type", "has_enclosure", "has_abl",
            "capabilities", "materials", "quirks", "calibration", "failure_modes",
        ]
        for key in expected_keys:
            assert key in d, f"Missing key '{key}' in serialized dict"

    def test_materials_are_dicts(self) -> None:
        intel = get_printer_intel("kilntest_ptfe")
        d = intel_to_dict(intel)
        assert isinstance(d["materials"], dict)
        for _mat_name, mat_data in d["materials"].items():
            assert "hotend" in mat_data
            assert "bed" in mat_data
            assert "fan" in mat_data
            assert "notes" in mat_data

    def test_failure_modes_are_dicts(self) -> None:
        intel = get_printer_intel("kilntest_ptfe")
        d = intel_to_dict(intel)
        assert isinstance(d["failure_modes"], list)
        for fm in d["failure_modes"]:
            assert "symptom" in fm
            assert "cause" in fm
            assert "fix" in fm

    def test_is_json_serializable(self) -> None:
        intel = get_printer_intel("kilntest_enclosed")
        d = intel_to_dict(intel)
        serialized = json.dumps(d)
        assert isinstance(serialized, str)

    def test_roundtrip_id_matches(self) -> None:
        intel = get_printer_intel("kilntest_ptfe")
        d = intel_to_dict(intel)
        assert d["id"] == intel.id
        assert d["firmware"] == intel.firmware


# ===================================================================
# JSON data file validity
# ===================================================================

def _full_rows(raw: dict) -> dict[str, dict]:
    """The rows the loader builds a profile from: the generic row and, in
    the session copy this suite reads, the synthetic fixture rows."""
    return {k: v for k, v in raw.items() if not k.startswith("_") and "max_hotend_temp" in v}


class TestPrinterIntelligenceJSON:
    """Tests for the bundled printer_intelligence.json data file (the
    session's copy, with the synthetic rows flattened in)."""

    #: What a row with a spec sheet carries.  A roster row carries only
    #: ``PUBLIC_PROFILE_FIELDS``; its spec sheet is served.
    REQUIRED_PROFILE_FIELDS = [
        "display_name", "firmware", "extruder_type", "hotend_type",
    ]

    def test_json_file_exists_and_parses(self) -> None:
        assert _DATA_FILE.exists()
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)

    def test_rows_with_a_spec_sheet_have_required_fields(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        assert "default" in _full_rows(raw)
        for key, data in _full_rows(raw).items():
            for req_field in self.REQUIRED_PROFILE_FIELDS:
                assert req_field in data, (
                    f"Profile '{key}' missing required field '{req_field}'"
                )

    def test_every_roster_row_has_the_exact_public_floor(self) -> None:
        """A roster row is a name and the motion block, nothing served."""
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        roster = {k: v for k, v in raw.items() if not k.startswith("_") and k not in _full_rows(raw)}
        assert roster, "expected roster rows"
        for key, data in roster.items():
            assert set(data) == PUBLIC_PROFILE_FIELDS, key

    def test_the_generic_row_carries_the_floor_whole(self) -> None:
        """The one bundled row every install holds with a spec sheet in it."""
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        default = raw["default"]
        assert set(default) >= PUBLIC_PROFILE_FIELDS
        for field in ("max_hotend_temp", "max_bed_temp", "build_volume_mm", "materials", "hotend_type"):
            assert field in default, field

    def test_private_hardware_shape_and_schema_ids_are_absent(self) -> None:
        raw_text = _DATA_FILE.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
        for private_field in (
            "ams_slots",
            "ams_type",
            "camera",
            "nozzle_options",
            "max_nozzle_temp",
            "max_speed_mm_s",
            "max_acceleration_mm_s2",
            "wifi",
            "capabilities",
        ):
            assert f'"{private_field}"' not in raw_text
        assert "fdm_hardware_capabilities_v1" not in raw_text
        assert "fdm_operational_capabilities_v1" not in raw_text
        assert not {
            key
            for key in raw.get("_meta", {})
            if "schema" in key.lower()
            and ("hardware" in key.lower() or "capability" in key.lower())
        }

    def test_rows_with_a_spec_sheet_have_materials_dict(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in _full_rows(raw).items():
            assert "materials" in data, f"Profile '{key}' missing 'materials'"
            assert isinstance(data["materials"], dict)

    def test_all_material_entries_have_required_keys(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for key, data in _full_rows(raw).items():
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
        intel = get_printer_intel("kilntest_ptfe")
        for q in intel.quirks:
            assert isinstance(q, str)

    @requires_printer_intelligence_overlay
    def test_calibration_is_dict(self) -> None:
        intel = get_printer_intel("ender3")
        assert isinstance(intel.calibration, dict)
        assert len(intel.calibration) > 0

    def test_calibration_values_are_strings(self) -> None:
        intel = get_printer_intel("kilntest_ptfe")
        for key, val in intel.calibration.items():
            assert isinstance(key, str)
            assert isinstance(val, str)

    def test_failure_modes_is_list_of_failure_mode(self) -> None:
        intel = get_printer_intel("kilntest_ptfe")
        assert isinstance(intel.failure_modes, list)
        for fm in intel.failure_modes:
            assert isinstance(fm, FailureMode)

    def test_materials_is_dict_of_material_profile(self) -> None:
        intel = get_printer_intel("kilntest_ptfe")
        assert isinstance(intel.materials, dict)
        for mat_name, mat in intel.materials.items():
            assert isinstance(mat_name, str)
            assert isinstance(mat, MaterialProfile)

    def test_default_quirks_is_empty(self) -> None:
        intel = get_printer_intel("default")
        assert intel.quirks == []

    def test_frozen(self) -> None:
        intel = get_printer_intel("kilntest_ptfe")
        with pytest.raises(FrozenInstanceError):
            intel.firmware = "klipper"  # type: ignore[misc]
