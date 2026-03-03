"""Sanity checks for FDM material and design pattern data files.

Permanent guardrail — catches bad data on every future edit.
Validates value ranges, schema completeness, and cross-references.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent.parent / "src" / "kiln" / "data" / "design_knowledge"
PRINTER_INTEL_DIR = Path(__file__).parent.parent / "src" / "kiln" / "data"
MATERIALS_FILE = DATA_DIR / "materials.json"
PATTERNS_FILE = DATA_DIR / "design_patterns.json"
COMPAT_FILE = DATA_DIR / "printer_material_compatibility.json"
TROUBLESHOOT_FILE = DATA_DIR / "material_troubleshooting.json"
MULTI_MAT_FILE = DATA_DIR / "multi_material_pairing.json"
POST_PROC_FILE = DATA_DIR / "post_processing.json"
PRINTER_INTEL_FILE = PRINTER_INTEL_DIR / "printer_intelligence.json"

# --- Fixtures ---


@pytest.fixture(scope="module")
def materials_data() -> dict:
    with open(MATERIALS_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def materials(materials_data: dict) -> dict:
    """Return only material entries (skip _meta and any _ prefixed keys)."""
    return {k: v for k, v in materials_data.items() if not k.startswith("_")}


@pytest.fixture(scope="module")
def patterns_data() -> dict:
    with open(PATTERNS_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def patterns(patterns_data: dict) -> dict:
    """Return only pattern entries (skip _meta and any _ prefixed keys)."""
    return {k: v for k, v in patterns_data.items() if not k.startswith("_")}


# --- Material Schema Tests ---


class TestMaterialSchema:
    """Every material must have all required top-level keys."""

    REQUIRED_KEYS = {
        "display_name",
        "category",
        "mechanical",
        "thermal",
        "chemical",
        "design_limits",
        "use_case_ratings",
        "agent_guidance",
    }

    MECHANICAL_KEYS = {
        "tensile_strength_mpa",
        "flexural_strength_mpa",
        "compressive_strength_mpa",
        "elongation_at_break_pct",
        "youngs_modulus_gpa",
        "impact_resistance",
        "layer_adhesion",
        "creep_resistance",
        "fatigue_resistance",
    }

    THERMAL_KEYS = {
        "glass_transition_c",
        "heat_deflection_c",
        "max_service_temp_c",
        "print_temp_range_c",
        "bed_temp_range_c",
        "warping_tendency",
    }

    CHEMICAL_KEYS = {
        "uv_resistance",
        "moisture_absorption",
        "chemical_resistance",
        "food_safe",
        "biodegradable",
        "outgassing",
    }

    DESIGN_LIMIT_KEYS = {
        "min_wall_thickness_mm",
        "recommended_wall_thickness_mm",
        "max_unsupported_overhang_deg",
        "max_bridge_length_mm",
        "min_hole_diameter_mm",
        "min_embossed_text_height_mm",
        "min_engraved_text_depth_mm",
        "snap_fit_tolerance_mm",
        "press_fit_interference_mm",
        "thread_min_pitch_mm",
        "living_hinge_viable",
        "max_cantilever_length_mm",
        "min_pin_diameter_mm",
    }

    USE_CASE_KEYS = {
        "structural_load_bearing",
        "outdoor_use",
        "food_contact",
        "high_temp_environment",
        "impact_resistance",
        "repeated_flexing",
        "cosmetic_finish",
        "dimensional_accuracy",
        "prototyping",
        "snap_fits",
        "gears_and_bearings",
        "water_tight",
    }

    def test_all_materials_have_required_keys(self, materials):
        for mat_id, mat in materials.items():
            missing = self.REQUIRED_KEYS - set(mat.keys())
            assert not missing, f"{mat_id} missing top-level keys: {missing}"

    def test_mechanical_keys_complete(self, materials):
        for mat_id, mat in materials.items():
            missing = self.MECHANICAL_KEYS - set(mat["mechanical"].keys())
            assert not missing, f"{mat_id}.mechanical missing: {missing}"

    def test_thermal_keys_complete(self, materials):
        for mat_id, mat in materials.items():
            missing = self.THERMAL_KEYS - set(mat["thermal"].keys())
            assert not missing, f"{mat_id}.thermal missing: {missing}"

    def test_chemical_keys_complete(self, materials):
        for mat_id, mat in materials.items():
            missing = self.CHEMICAL_KEYS - set(mat["chemical"].keys())
            assert not missing, f"{mat_id}.chemical missing: {missing}"

    def test_design_limits_keys_complete(self, materials):
        for mat_id, mat in materials.items():
            missing = self.DESIGN_LIMIT_KEYS - set(mat["design_limits"].keys())
            assert not missing, f"{mat_id}.design_limits missing: {missing}"

    def test_use_case_ratings_keys_complete(self, materials):
        for mat_id, mat in materials.items():
            missing = self.USE_CASE_KEYS - set(mat["use_case_ratings"].keys())
            assert not missing, f"{mat_id}.use_case_ratings missing: {missing}"


class TestMaterialValueRanges:
    """Catch obviously wrong values that would produce bad print advice."""

    def test_print_temp_range_valid(self, materials):
        for mat_id, mat in materials.items():
            low, high = mat["thermal"]["print_temp_range_c"]
            assert 150 <= low <= 350, (
                f"{mat_id} print_temp low {low}C out of range [150, 350]"
            )
            assert 170 <= high <= 400, (
                f"{mat_id} print_temp high {high}C out of range [170, 400]"
            )
            assert low < high, (
                f"{mat_id} print_temp_range_c low >= high: [{low}, {high}]"
            )

    def test_bed_temp_range_valid(self, materials):
        for mat_id, mat in materials.items():
            low, high = mat["thermal"]["bed_temp_range_c"]
            assert 0 <= low <= 130, (
                f"{mat_id} bed_temp low {low}C out of range [0, 130]"
            )
            assert 0 <= high <= 130, (
                f"{mat_id} bed_temp high {high}C out of range [0, 130]"
            )
            assert low < high, (
                f"{mat_id} bed_temp_range_c low >= high: [{low}, {high}]"
            )

    def test_tensile_strength_plausible(self, materials):
        for mat_id, mat in materials.items():
            val = mat["mechanical"]["tensile_strength_mpa"]
            assert 10 <= val <= 200, (
                f"{mat_id} tensile_strength_mpa {val} out of plausible range [10, 200]"
            )

    def test_flexural_strength_plausible(self, materials):
        for mat_id, mat in materials.items():
            val = mat["mechanical"]["flexural_strength_mpa"]
            assert 10 <= val <= 250, (
                f"{mat_id} flexural_strength_mpa {val} out of plausible range [10, 250]"
            )

    def test_youngs_modulus_plausible(self, materials):
        for mat_id, mat in materials.items():
            val = mat["mechanical"]["youngs_modulus_gpa"]
            assert 0.01 <= val <= 15, (
                f"{mat_id} youngs_modulus_gpa {val} out of plausible range [0.01, 15]"
            )

    def test_min_wall_thickness_plausible(self, materials):
        for mat_id, mat in materials.items():
            val = mat["design_limits"]["min_wall_thickness_mm"]
            assert 0.4 <= val <= 3.0, (
                f"{mat_id} min_wall_thickness_mm {val} out of range [0.4, 3.0]"
            )

    def test_recommended_wall_gte_min(self, materials):
        for mat_id, mat in materials.items():
            min_wall = mat["design_limits"]["min_wall_thickness_mm"]
            rec_wall = mat["design_limits"]["recommended_wall_thickness_mm"]
            assert rec_wall >= min_wall, (
                f"{mat_id} recommended_wall ({rec_wall}) < min_wall ({min_wall})"
            )

    def test_glass_transition_below_print_temp(self, materials):
        for mat_id, mat in materials.items():
            tg = mat["thermal"]["glass_transition_c"]
            print_low = mat["thermal"]["print_temp_range_c"][0]
            # Glass transition must be well below print temp (material must flow)
            # Exception: TPU has negative Tg
            if tg > 0:
                assert tg < print_low, (
                    f"{mat_id} glass_transition {tg}C >= print_temp_low {print_low}C"
                )

    def test_max_service_temp_below_glass_transition_or_hdt(self, materials):
        for mat_id, mat in materials.items():
            max_svc = mat["thermal"]["max_service_temp_c"]
            hdt = mat["thermal"]["heat_deflection_c"]
            # Max service temp should not exceed HDT (parts deform above HDT)
            # Allow equality and small margin for composites where fiber reinforcement
            # allows service above unreinforced HDT
            assert max_svc <= hdt + 20, (
                f"{mat_id} max_service_temp {max_svc}C too far above "
                f"heat_deflection {hdt}C"
            )


class TestMaterialGuidance:
    """Agent guidance must be substantive."""

    def test_minimum_guidance_entries(self, materials):
        for mat_id, mat in materials.items():
            count = len(mat["agent_guidance"])
            assert count >= 4, (
                f"{mat_id} has only {count} agent_guidance entries (need >= 4)"
            )

    def test_guidance_entries_are_nonempty(self, materials):
        for mat_id, mat in materials.items():
            for i, entry in enumerate(mat["agent_guidance"]):
                assert len(entry.strip()) > 10, (
                    f"{mat_id} agent_guidance[{i}] is too short"
                )


class TestNoDuplicateMaterialIDs:
    """Material IDs must be unique (JSON keys are unique by spec, but
    a linting pass catches copy-paste errors where a key is repeated)."""

    def test_no_duplicate_ids(self):
        with open(MATERIALS_FILE) as f:
            raw = f.read()
        # Parse manually to detect duplicate keys
        import re

        # Find all top-level keys (2-space indented strings)
        keys = re.findall(r'^  "([^"]+)":', raw, re.MULTILINE)
        seen = set()
        dupes = []
        for k in keys:
            if k in seen:
                dupes.append(k)
            seen.add(k)
        assert not dupes, f"Duplicate material/top-level keys: {dupes}"


class TestWarpingTendencyValues:
    """warping_tendency must use the controlled vocabulary."""

    VALID = {"none", "low", "moderate", "high", "very_high"}

    def test_valid_warping_values(self, materials):
        for mat_id, mat in materials.items():
            val = mat["thermal"]["warping_tendency"]
            assert val in self.VALID, (
                f"{mat_id} warping_tendency '{val}' not in {self.VALID}"
            )


class TestRatingValues:
    """use_case_ratings and qualitative fields must use controlled vocabulary."""

    VALID_RATINGS = {
        "no", "poor", "conditional", "moderate", "good", "excellent", "outstanding",
    }
    VALID_QUALITATIVE = {
        "poor", "low", "moderate", "good", "excellent", "outstanding",
    }

    def test_use_case_ratings_valid(self, materials):
        for mat_id, mat in materials.items():
            for key, val in mat["use_case_ratings"].items():
                assert val in self.VALID_RATINGS, (
                    f"{mat_id}.use_case_ratings.{key} = '{val}' not in "
                    f"{self.VALID_RATINGS}"
                )

    def test_mechanical_qualitative_valid(self, materials):
        qual_keys = [
            "impact_resistance", "layer_adhesion",
            "creep_resistance", "fatigue_resistance",
        ]
        for mat_id, mat in materials.items():
            for key in qual_keys:
                val = mat["mechanical"][key]
                assert val in self.VALID_QUALITATIVE, (
                    f"{mat_id}.mechanical.{key} = '{val}' not in "
                    f"{self.VALID_QUALITATIVE}"
                )


# --- Design Pattern Tests ---


class TestPatternSchema:
    """Every design pattern must have all required top-level keys."""

    REQUIRED_KEYS = {
        "display_name",
        "description",
        "use_cases",
        "design_rules",
        "material_compatibility",
        "print_orientation",
        "print_orientation_reason",
        "agent_guidance",
    }

    COMPAT_KEYS = {"excellent", "good", "poor", "avoid"}

    def test_all_patterns_have_required_keys(self, patterns):
        for pat_id, pat in patterns.items():
            missing = self.REQUIRED_KEYS - set(pat.keys())
            assert not missing, f"{pat_id} missing keys: {missing}"

    def test_material_compatibility_keys(self, patterns):
        for pat_id, pat in patterns.items():
            compat = pat["material_compatibility"]
            missing = self.COMPAT_KEYS - set(compat.keys())
            assert not missing, (
                f"{pat_id}.material_compatibility missing: {missing}"
            )

    def test_pattern_guidance_minimum(self, patterns):
        for pat_id, pat in patterns.items():
            count = len(pat["agent_guidance"])
            assert count >= 4, (
                f"{pat_id} has only {count} agent_guidance entries (need >= 4)"
            )


class TestPatternMaterialReferences:
    """Material IDs referenced in patterns must exist in materials.json."""

    def test_referenced_materials_exist(self, materials, patterns):
        material_ids = set(materials.keys())
        for pat_id, pat in patterns.items():
            compat = pat["material_compatibility"]
            for tier in ("excellent", "good", "poor", "avoid"):
                for mat_ref in compat.get(tier, []):
                    assert mat_ref in material_ids, (
                        f"{pat_id}.material_compatibility.{tier} references "
                        f"unknown material '{mat_ref}'"
                    )


# --- Cross-file Consistency ---


class TestCrossFileConsistency:
    """Materials and patterns files must be internally consistent."""

    def test_materials_json_valid(self):
        with open(MATERIALS_FILE) as f:
            data = json.load(f)
        assert "_meta" in data

    def test_patterns_json_valid(self):
        with open(PATTERNS_FILE) as f:
            data = json.load(f)
        assert "_meta" in data

    def test_material_count_minimum(self, materials):
        assert len(materials) >= 20, (
            f"Expected >= 20 materials, got {len(materials)}"
        )

    def test_pattern_count_minimum(self, patterns):
        assert len(patterns) >= 20, (
            f"Expected >= 20 patterns, got {len(patterns)}"
        )


# --- Printer-Material Compatibility Matrix Tests ---


@pytest.fixture(scope="module")
def compat_data() -> dict:
    with open(COMPAT_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def printer_intel() -> dict:
    with open(PRINTER_INTEL_FILE) as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


class TestCompatibilityMatrix:
    """Printer-material compatibility matrix cross-references and schema."""

    VALID_STATUS = {"compatible", "needs_upgrade", "not_compatible"}
    VALID_UPGRADES = {
        "hardened_nozzle", "all_metal_hotend", "enclosure",
        "heated_bed", "direct_drive", "dry_box",
    }

    def test_every_printer_exists_in_printer_intelligence(
        self, compat_data, printer_intel,
    ):
        compat_printers = {k for k in compat_data if not k.startswith("_")}
        intel_printers = set(printer_intel.keys())
        unknown = compat_printers - intel_printers
        assert not unknown, (
            f"Compatibility matrix has printers not in "
            f"printer_intelligence.json: {unknown}"
        )

    def test_every_material_exists_in_materials(self, compat_data, materials):
        material_ids = set(materials.keys())
        for printer_id, mat_map in compat_data.items():
            if printer_id.startswith("_"):
                continue
            unknown = set(mat_map.keys()) - material_ids
            assert not unknown, (
                f"{printer_id} references unknown materials: {unknown}"
            )

    def test_every_printer_has_every_material(
        self, compat_data, materials, printer_intel,
    ):
        material_ids = set(materials.keys())
        for printer_id in printer_intel:
            assert printer_id in compat_data, (
                f"Printer {printer_id} missing from compatibility matrix"
            )
            missing = material_ids - set(compat_data[printer_id].keys())
            assert not missing, (
                f"{printer_id} missing materials: {missing}"
            )

    def test_valid_status_values(self, compat_data):
        for printer_id, mat_map in compat_data.items():
            if printer_id.startswith("_"):
                continue
            for mat_id, entry in mat_map.items():
                assert entry["status"] in self.VALID_STATUS, (
                    f"{printer_id}.{mat_id} status "
                    f"'{entry['status']}' not valid"
                )

    def test_compatible_has_empty_upgrades(self, compat_data):
        for printer_id, mat_map in compat_data.items():
            if printer_id.startswith("_"):
                continue
            for mat_id, entry in mat_map.items():
                if entry["status"] == "compatible":
                    assert entry["upgrades_needed"] == [], (
                        f"{printer_id}.{mat_id} is 'compatible' but "
                        f"has upgrades: {entry['upgrades_needed']}"
                    )

    def test_needs_upgrade_has_upgrades(self, compat_data):
        for printer_id, mat_map in compat_data.items():
            if printer_id.startswith("_"):
                continue
            for mat_id, entry in mat_map.items():
                if entry["status"] == "needs_upgrade":
                    assert len(entry["upgrades_needed"]) >= 1, (
                        f"{printer_id}.{mat_id} is 'needs_upgrade' but "
                        f"upgrades_needed is empty"
                    )

    def test_upgrade_values_are_valid(self, compat_data):
        for printer_id, mat_map in compat_data.items():
            if printer_id.startswith("_"):
                continue
            for mat_id, entry in mat_map.items():
                for upg in entry["upgrades_needed"]:
                    assert upg in self.VALID_UPGRADES, (
                        f"{printer_id}.{mat_id} has unknown upgrade "
                        f"'{upg}', valid: {self.VALID_UPGRADES}"
                    )

    def test_every_entry_has_notes(self, compat_data):
        for printer_id, mat_map in compat_data.items():
            if printer_id.startswith("_"):
                continue
            for mat_id, entry in mat_map.items():
                assert "notes" in entry and len(entry["notes"]) > 0, (
                    f"{printer_id}.{mat_id} missing or empty notes"
                )


# --- Material Troubleshooting Tests ---


@pytest.fixture(scope="module")
def troubleshoot_data() -> dict:
    with open(TROUBLESHOOT_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def troubleshoot(troubleshoot_data: dict) -> dict:
    return {k: v for k, v in troubleshoot_data.items() if not k.startswith("_")}


class TestTroubleshootingSchema:
    """Material troubleshooting guide schema and cross-references."""

    VALID_SEVERITY = {"minor", "moderate", "major"}
    STORAGE_KEYS = {
        "humidity_sensitive", "max_humidity_pct", "storage_method",
        "drying_temp_c", "drying_time_hours",
    }

    def test_every_material_has_troubleshooting(self, troubleshoot, materials):
        missing = set(materials.keys()) - set(troubleshoot.keys())
        assert not missing, (
            f"Materials missing from troubleshooting: {missing}"
        )

    def test_no_extra_materials(self, troubleshoot, materials):
        extra = set(troubleshoot.keys()) - set(materials.keys())
        assert not extra, (
            f"Troubleshooting has unknown materials: {extra}"
        )

    def test_minimum_common_issues(self, troubleshoot):
        for mat_id, entry in troubleshoot.items():
            count = len(entry["common_issues"])
            assert count >= 5, (
                f"{mat_id} has only {count} common_issues (need >= 5)"
            )

    def test_issue_has_required_fields(self, troubleshoot):
        required = {"symptom", "severity", "root_cause", "fixes", "prevention"}
        for mat_id, entry in troubleshoot.items():
            for i, issue in enumerate(entry["common_issues"]):
                missing = required - set(issue.keys())
                assert not missing, (
                    f"{mat_id}.common_issues[{i}] missing: {missing}"
                )

    def test_severity_values_valid(self, troubleshoot):
        for mat_id, entry in troubleshoot.items():
            for i, issue in enumerate(entry["common_issues"]):
                assert issue["severity"] in self.VALID_SEVERITY, (
                    f"{mat_id}.common_issues[{i}] severity "
                    f"'{issue['severity']}' not valid"
                )

    def test_fixes_have_sequential_priorities(self, troubleshoot):
        for mat_id, entry in troubleshoot.items():
            for i, issue in enumerate(entry["common_issues"]):
                fixes = issue["fixes"]
                assert len(fixes) >= 1, (
                    f"{mat_id}.common_issues[{i}] has no fixes"
                )
                priorities = [f["priority"] for f in fixes]
                expected = list(range(1, len(fixes) + 1))
                assert priorities == expected, (
                    f"{mat_id}.common_issues[{i}] fix priorities "
                    f"{priorities} != expected {expected}"
                )

    def test_storage_requirements_complete(self, troubleshoot):
        for mat_id, entry in troubleshoot.items():
            assert "storage_requirements" in entry, (
                f"{mat_id} missing storage_requirements"
            )
            missing = self.STORAGE_KEYS - set(entry["storage_requirements"].keys())
            assert not missing, (
                f"{mat_id}.storage_requirements missing: {missing}"
            )

    def test_break_in_tips_present(self, troubleshoot):
        for mat_id, entry in troubleshoot.items():
            tips = entry.get("break_in_tips", [])
            assert len(tips) >= 3, (
                f"{mat_id} has only {len(tips)} break_in_tips (need >= 3)"
            )


# --- Multi-Material Pairing Tests ---


@pytest.fixture(scope="module")
def multi_mat_data() -> dict:
    with open(MULTI_MAT_FILE) as f:
        return json.load(f)


class TestMultiMaterialPairing:
    """Multi-material pairing rules cross-references and schema."""

    VALID_ADHESION = {"excellent", "good", "moderate", "poor", "none"}

    def test_support_pairs_reference_valid_materials(
        self, multi_mat_data, materials,
    ):
        material_ids = set(materials.keys())
        for pair in multi_mat_data["support_pairs"]:
            assert pair["model_material"] in material_ids, (
                f"support_pair model_material "
                f"'{pair['model_material']}' not in materials.json"
            )
            assert pair["support_material"] in material_ids, (
                f"support_pair support_material "
                f"'{pair['support_material']}' not in materials.json"
            )

    def test_support_pairs_have_required_fields(self, multi_mat_data):
        required = {
            "model_material", "support_material",
            "dissolution_method", "interface_adhesion", "notes",
        }
        for i, pair in enumerate(multi_mat_data["support_pairs"]):
            missing = required - set(pair.keys())
            assert not missing, (
                f"support_pairs[{i}] missing: {missing}"
            )

    def test_support_pair_adhesion_valid(self, multi_mat_data):
        for i, pair in enumerate(multi_mat_data["support_pairs"]):
            assert pair["interface_adhesion"] in self.VALID_ADHESION, (
                f"support_pairs[{i}] adhesion "
                f"'{pair['interface_adhesion']}' not valid"
            )

    def test_co_print_materials_reference_valid(
        self, multi_mat_data, materials,
    ):
        material_ids = set(materials.keys())
        co = multi_mat_data.get("co_print_compatibility", {})
        for mat_a, pairings in co.items():
            assert mat_a in material_ids, (
                f"co_print_compatibility key '{mat_a}' not in materials.json"
            )
            for mat_b in pairings:
                assert mat_b in material_ids, (
                    f"co_print_compatibility.{mat_a}.{mat_b} "
                    f"not in materials.json"
                )

    def test_co_print_adhesion_valid(self, multi_mat_data):
        co = multi_mat_data.get("co_print_compatibility", {})
        for mat_a, pairings in co.items():
            for mat_b, entry in pairings.items():
                assert entry["interface_adhesion"] in self.VALID_ADHESION, (
                    f"co_print.{mat_a}.{mat_b} adhesion "
                    f"'{entry['interface_adhesion']}' not valid"
                )

    def test_general_rules_present(self, multi_mat_data):
        rules = multi_mat_data.get("general_rules", [])
        assert len(rules) >= 5, (
            f"Only {len(rules)} general_rules (need >= 5)"
        )


# --- Post-Processing Guide Tests ---


@pytest.fixture(scope="module")
def post_proc_data() -> dict:
    with open(POST_PROC_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def post_proc(post_proc_data: dict) -> dict:
    return {k: v for k, v in post_proc_data.items() if not k.startswith("_")}


class TestPostProcessingGuide:
    """Post-processing guide schema and cross-references."""

    VALID_DIFFICULTY = {"easy", "moderate", "advanced"}

    def test_every_material_has_post_processing(self, post_proc, materials):
        missing = set(materials.keys()) - set(post_proc.keys())
        assert not missing, (
            f"Materials missing from post_processing: {missing}"
        )

    def test_no_extra_materials(self, post_proc, materials):
        extra = set(post_proc.keys()) - set(materials.keys())
        assert not extra, (
            f"Post-processing has unknown materials: {extra}"
        )

    def test_minimum_techniques(self, post_proc):
        for mat_id, entry in post_proc.items():
            count = len(entry.get("techniques", []))
            assert count >= 2, (
                f"{mat_id} has only {count} techniques (need >= 2)"
            )

    def test_technique_has_required_fields(self, post_proc):
        required = {
            "name", "difficulty", "tools_needed",
            "procedure", "result", "safety_notes",
        }
        for mat_id, entry in post_proc.items():
            for i, tech in enumerate(entry.get("techniques", [])):
                missing = required - set(tech.keys())
                assert not missing, (
                    f"{mat_id}.techniques[{i}] missing: {missing}"
                )

    def test_difficulty_values_valid(self, post_proc):
        for mat_id, entry in post_proc.items():
            for i, tech in enumerate(entry.get("techniques", [])):
                assert tech["difficulty"] in self.VALID_DIFFICULTY, (
                    f"{mat_id}.techniques[{i}] difficulty "
                    f"'{tech['difficulty']}' not valid"
                )

    def test_paintability_present(self, post_proc):
        for mat_id, entry in post_proc.items():
            assert "paintability" in entry, (
                f"{mat_id} missing paintability section"
            )

    def test_strengthening_present(self, post_proc):
        for mat_id, entry in post_proc.items():
            assert "strengthening" in entry, (
                f"{mat_id} missing strengthening section"
            )
