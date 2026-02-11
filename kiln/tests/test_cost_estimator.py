"""Tests for kiln.cost_estimator — print cost estimation from G-code."""

import math
import time

import pytest

from kiln.cost_estimator import (
    BUILTIN_MATERIALS,
    CostEstimate,
    CostEstimator,
    MaterialProfile,
    _parse_time_from_comments,
)


# -----------------------------------------------------------------------
# MaterialProfile dataclass
# -----------------------------------------------------------------------


class TestMaterialProfile:
    def test_creation_defaults(self):
        mp = MaterialProfile(name="PLA", density_g_per_cm3=1.24, cost_per_kg_usd=25.0)
        assert mp.name == "PLA"
        assert mp.density_g_per_cm3 == 1.24
        assert mp.cost_per_kg_usd == 25.0
        assert mp.filament_diameter_mm == 1.75
        assert mp.tool_temp_default == 200.0
        assert mp.bed_temp_default == 60.0

    def test_creation_custom_values(self):
        mp = MaterialProfile(
            name="CUSTOM",
            density_g_per_cm3=1.5,
            cost_per_kg_usd=50.0,
            filament_diameter_mm=2.85,
            tool_temp_default=300.0,
            bed_temp_default=120.0,
        )
        assert mp.filament_diameter_mm == 2.85
        assert mp.tool_temp_default == 300.0
        assert mp.bed_temp_default == 120.0

    def test_to_dict(self):
        mp = MaterialProfile(name="PLA", density_g_per_cm3=1.24, cost_per_kg_usd=25.0)
        d = mp.to_dict()
        assert isinstance(d, dict)
        assert d["name"] == "PLA"
        assert d["density_g_per_cm3"] == 1.24
        assert d["cost_per_kg_usd"] == 25.0
        assert d["filament_diameter_mm"] == 1.75
        assert "tool_temp_default" in d
        assert "bed_temp_default" in d


# -----------------------------------------------------------------------
# CostEstimate dataclass
# -----------------------------------------------------------------------


class TestCostEstimate:
    def test_creation_defaults(self):
        ce = CostEstimate(
            file_name="test.gcode",
            material="PLA",
            filament_length_meters=10.0,
            filament_weight_grams=30.0,
            filament_cost_usd=0.75,
        )
        assert ce.file_name == "test.gcode"
        assert ce.estimated_time_seconds is None
        assert ce.electricity_cost_usd == 0.0
        assert ce.total_cost_usd == 0.0
        assert ce.warnings == []

    def test_to_dict(self):
        ce = CostEstimate(
            file_name="test.gcode",
            material="PLA",
            filament_length_meters=10.0,
            filament_weight_grams=30.0,
            filament_cost_usd=0.75,
            estimated_time_seconds=3600,
            electricity_cost_usd=0.02,
            total_cost_usd=0.77,
            warnings=["some warning"],
        )
        d = ce.to_dict()
        assert isinstance(d, dict)
        assert d["file_name"] == "test.gcode"
        assert d["material"] == "PLA"
        assert d["estimated_time_seconds"] == 3600
        assert d["warnings"] == ["some warning"]
        assert d["total_cost_usd"] == 0.77

    def test_to_dict_contains_all_fields(self):
        ce = CostEstimate(
            file_name="x.gcode",
            material="ABS",
            filament_length_meters=1.0,
            filament_weight_grams=2.0,
            filament_cost_usd=0.05,
        )
        d = ce.to_dict()
        expected_keys = {
            "file_name", "material", "filament_length_meters",
            "filament_weight_grams", "filament_cost_usd",
            "estimated_time_seconds", "electricity_cost_usd",
            "electricity_rate_kwh", "printer_wattage",
            "total_cost_usd", "warnings",
        }
        assert set(d.keys()) == expected_keys


# -----------------------------------------------------------------------
# BUILTIN_MATERIALS
# -----------------------------------------------------------------------


class TestBuiltinMaterials:
    def test_has_all_seven_materials(self):
        expected = {"PLA", "PETG", "ABS", "TPU", "ASA", "NYLON", "PC"}
        assert set(BUILTIN_MATERIALS.keys()) == expected

    def test_all_are_material_profiles(self):
        for name, profile in BUILTIN_MATERIALS.items():
            assert isinstance(profile, MaterialProfile), f"{name} is not MaterialProfile"
            assert profile.name == name

    def test_all_have_positive_values(self):
        for name, profile in BUILTIN_MATERIALS.items():
            assert profile.density_g_per_cm3 > 0, f"{name} density"
            assert profile.cost_per_kg_usd > 0, f"{name} cost"
            assert profile.filament_diameter_mm > 0, f"{name} diameter"


# -----------------------------------------------------------------------
# CostEstimator — materials & lookup
# -----------------------------------------------------------------------


class TestCostEstimatorMaterials:
    def test_materials_returns_copy(self):
        est = CostEstimator()
        m1 = est.materials
        m2 = est.materials
        assert m1 is not m2
        assert m1 == m2
        m1.pop("PLA")
        assert "PLA" in est.materials

    def test_get_material_case_insensitive(self):
        est = CostEstimator()
        assert est.get_material("pla") is not None
        assert est.get_material("Pla") is not None
        assert est.get_material("PLA") is not None
        assert est.get_material("pla").name == "PLA"

    def test_get_material_unknown_returns_none(self):
        est = CostEstimator()
        assert est.get_material("UNOBTANIUM") is None

    def test_custom_materials_added(self):
        custom = {
            "WOOD": MaterialProfile(
                name="WOOD", density_g_per_cm3=1.15, cost_per_kg_usd=35.0,
            )
        }
        est = CostEstimator(custom_materials=custom)
        assert est.get_material("WOOD") is not None
        assert est.get_material("WOOD").name == "WOOD"
        assert est.get_material("PLA") is not None

    def test_custom_materials_override_builtin(self):
        cheap_pla = MaterialProfile(
            name="PLA", density_g_per_cm3=1.24, cost_per_kg_usd=10.0,
        )
        est = CostEstimator(custom_materials={"PLA": cheap_pla})
        assert est.get_material("PLA").cost_per_kg_usd == 10.0


# -----------------------------------------------------------------------
# _parse_extrusion
# -----------------------------------------------------------------------


class TestParseExtrusion:
    def _parse(self, gcode_text):
        lines = gcode_text.strip().splitlines()
        return CostEstimator()._parse_extrusion(lines)

    def test_basic_absolute_extrusion(self):
        gcode = "G1 X10 Y10 E5.0\nG1 X20 Y20 E10.0\nG1 X30 Y30 E20.0"
        assert self._parse(gcode) == pytest.approx(20.0)

    def test_relative_mode_m83(self):
        gcode = "M83\nG1 X10 Y10 E3.0\nG1 X20 Y20 E4.0\nG1 X30 Y30 E5.0"
        assert self._parse(gcode) == pytest.approx(12.0)

    def test_mode_switching_m82_m83(self):
        gcode = "G1 X10 E5.0\nG1 X20 E10.0\nM83\nG1 X30 E2.0\nG1 X40 E3.0\nM82\nG1 X50 E12.0"
        # Absolute: (5-0)+(10-5)=10, M83 relative: 2+3=5, M82 resets last_e to 0: (12-0)=12 => 27
        assert self._parse(gcode) == pytest.approx(27.0)

    def test_g92_e0_reset(self):
        gcode = "G1 X10 E10.0\nG92 E0\nG1 X20 E5.0"
        assert self._parse(gcode) == pytest.approx(15.0)

    def test_retraction_filtered_absolute(self):
        gcode = "G1 X10 E10.0\nG1 E9.0\nG1 X20 E15.0"
        assert self._parse(gcode) == pytest.approx(16.0)

    def test_retraction_filtered_relative(self):
        gcode = "M83\nG1 X10 E5.0\nG1 E-1.0\nG1 X20 E5.0"
        assert self._parse(gcode) == pytest.approx(10.0)

    def test_empty_file(self):
        assert self._parse("") == pytest.approx(0.0)

    def test_no_extrusion_commands(self):
        gcode = "G28\nG1 X10 Y10 F3000\nG1 X20 Y20\nM104 S200"
        assert self._parse(gcode) == pytest.approx(0.0)

    def test_inline_comments_stripped(self):
        gcode = "G1 X10 E5.0 ; first move\nG1 X20 E10.0 ; second move"
        assert self._parse(gcode) == pytest.approx(10.0)

    def test_full_line_comments_skipped(self):
        gcode = "; this is a comment\n; E100.0 should be ignored\nG1 X10 E3.0"
        assert self._parse(gcode) == pytest.approx(3.0)

    def test_g0_moves_with_e(self):
        gcode = "G0 X10 E5.0\nG0 X20 E8.0"
        assert self._parse(gcode) == pytest.approx(8.0)

    def test_g92_with_nonzero_e(self):
        gcode = "G1 X10 E10.0\nG92 E5.0\nG1 X20 E8.0"
        assert self._parse(gcode) == pytest.approx(13.0)


# -----------------------------------------------------------------------
# _parse_time_from_comments
# -----------------------------------------------------------------------


class TestParseTimeFromComments:
    def _parse_time(self, text):
        return _parse_time_from_comments(text.strip().splitlines())

    def test_prusaslicer_format(self):
        text = "; estimated printing time (normal mode) = 1h 23m 45s"
        assert self._parse_time(text) == 5025

    def test_prusaslicer_minutes_only(self):
        text = "; estimated printing time (normal mode) = 45m 30s"
        assert self._parse_time(text) == 45 * 60 + 30

    def test_cura_time_format(self):
        text = ";TIME:5025"
        assert self._parse_time(text) == 5025

    def test_cura_time_with_spaces(self):
        text = "; TIME: 5025"
        assert self._parse_time(text) == 5025

    def test_orcaslicer_format(self):
        text = "; total estimated time: 2h 10m 5s"
        assert self._parse_time(text) == 2 * 3600 + 10 * 60 + 5

    def test_no_time_comment_returns_none(self):
        text = "; some comment\nG1 X10 Y10\n; another comment"
        assert self._parse_time(text) is None

    def test_hours_only(self):
        text = "; estimated printing time (normal mode) = 2h"
        assert self._parse_time(text) == 7200

    def test_ignores_non_comment_lines(self):
        text = "G1 X10 Y10\nTIME:9999"
        assert self._parse_time(text) is None


# -----------------------------------------------------------------------
# estimate_from_gcode
# -----------------------------------------------------------------------


class TestEstimateFromGcode:
    def _gcode_lines(self, text):
        return text.strip().splitlines()

    def test_pla_default(self):
        gcode = self._gcode_lines("G1 X10 E100.0")
        est = CostEstimator().estimate_from_gcode(gcode)
        assert est.material == "PLA"
        assert est.filament_length_meters > 0
        assert est.filament_weight_grams > 0
        assert est.filament_cost_usd > 0
        assert est.total_cost_usd > 0

    def test_petg_material(self):
        gcode = self._gcode_lines("G1 X10 E100.0")
        est = CostEstimator().estimate_from_gcode(gcode, material="PETG")
        assert est.material == "PETG"
        assert est.filament_cost_usd > 0

    def test_unknown_material_falls_back_to_pla(self):
        gcode = self._gcode_lines("G1 X10 E100.0")
        est = CostEstimator().estimate_from_gcode(gcode, material="UNOBTANIUM")
        assert est.material == "PLA"
        assert any("Unknown material" in w for w in est.warnings)

    def test_no_extrusion_produces_warning(self):
        gcode = self._gcode_lines("G28\nG1 X10 Y10")
        est = CostEstimator().estimate_from_gcode(gcode)
        assert any("No extrusion" in w for w in est.warnings)

    def test_electricity_cost_with_time(self):
        gcode = self._gcode_lines("G1 X10 E100.0\n;TIME:3600")
        est = CostEstimator().estimate_from_gcode(
            gcode, electricity_rate=0.15, printer_wattage=300.0,
        )
        assert est.estimated_time_seconds == 3600
        # 300W * 1h = 0.3 kWh * 0.15 = 0.045
        assert est.electricity_cost_usd == pytest.approx(0.04, abs=0.01)

    def test_no_time_means_no_electricity_cost(self):
        gcode = self._gcode_lines("G1 X10 E100.0")
        est = CostEstimator().estimate_from_gcode(gcode)
        assert est.estimated_time_seconds is None
        assert est.electricity_cost_usd == 0.0

    def test_total_cost_equals_filament_plus_electricity(self):
        gcode = self._gcode_lines("G1 X10 E100.0\n;TIME:7200")
        est = CostEstimator().estimate_from_gcode(
            gcode, electricity_rate=0.12, printer_wattage=200.0,
        )
        expected_total = est.filament_cost_usd + est.electricity_cost_usd
        assert est.total_cost_usd == pytest.approx(expected_total, abs=0.01)

    def test_zero_extrusion_produces_zero_cost(self):
        gcode = self._gcode_lines("G28\nG1 X10 Y10")
        est = CostEstimator().estimate_from_gcode(gcode)
        assert est.filament_length_meters == 0.0
        assert est.filament_weight_grams == 0.0
        assert est.filament_cost_usd == 0.0
        assert est.total_cost_usd == 0.0

    def test_file_name_passthrough(self):
        gcode = self._gcode_lines("G1 X10 E10.0")
        est = CostEstimator().estimate_from_gcode(gcode, file_name="benchy.gcode")
        assert est.file_name == "benchy.gcode"

    def test_default_file_name(self):
        gcode = self._gcode_lines("G1 X10 E10.0")
        est = CostEstimator().estimate_from_gcode(gcode)
        assert est.file_name == "<unknown>"


# -----------------------------------------------------------------------
# estimate_from_file
# -----------------------------------------------------------------------


class TestEstimateFromFile:
    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            CostEstimator().estimate_from_file("/nonexistent/path/to/file.gcode")

    def test_reads_real_temp_file(self, tmp_path):
        gcode_content = (
            "; estimated printing time (normal mode) = 0h 30m 0s\n"
            "G28\n"
            "G1 X10 Y10 E50.0\n"
            "G1 X20 Y20 E100.0\n"
        )
        gcode_file = tmp_path / "test_print.gcode"
        gcode_file.write_text(gcode_content)

        est = CostEstimator().estimate_from_file(str(gcode_file))
        assert est.file_name == "test_print.gcode"
        assert est.filament_length_meters > 0
        assert est.estimated_time_seconds == 1800
        assert est.electricity_cost_usd > 0
        assert est.total_cost_usd > 0

    def test_passes_material_and_rates(self, tmp_path):
        gcode_content = "G1 X10 E50.0\n;TIME:3600\n"
        gcode_file = tmp_path / "rates.gcode"
        gcode_file.write_text(gcode_content)

        est = CostEstimator().estimate_from_file(
            str(gcode_file),
            material="PETG",
            electricity_rate=0.20,
            printer_wattage=400.0,
        )
        assert est.material == "PETG"
        assert est.electricity_rate_kwh == 0.20
        assert est.printer_wattage == 400.0


# -----------------------------------------------------------------------
# Weight / volume calculation accuracy
# -----------------------------------------------------------------------


class TestCalculationAccuracy:
    def test_known_pla_values(self):
        """Verify weight/volume math with hand-calculated values."""
        gcode = ["G1 X10 E100.0"]
        est = CostEstimator().estimate_from_gcode(gcode)

        radius = 1.75 / 2.0
        cross_section = math.pi * radius * radius
        volume_cm3 = (100.0 * cross_section) / 1000.0
        weight_g = volume_cm3 * 1.24
        cost = (weight_g / 1000.0) * 25.0

        assert est.filament_length_meters == pytest.approx(0.1, abs=0.001)
        assert est.filament_weight_grams == pytest.approx(weight_g, abs=0.01)
        assert est.filament_cost_usd == pytest.approx(cost, abs=0.01)

    def test_petg_different_from_pla(self):
        gcode = ["G1 X10 E500.0"]
        est_pla = CostEstimator().estimate_from_gcode(gcode, material="PLA")
        est_petg = CostEstimator().estimate_from_gcode(gcode, material="PETG")

        assert est_pla.filament_length_meters == est_petg.filament_length_meters
        assert est_petg.filament_weight_grams > est_pla.filament_weight_grams
        assert est_petg.filament_cost_usd > est_pla.filament_cost_usd


# -----------------------------------------------------------------------
# Performance
# -----------------------------------------------------------------------


class TestPerformance:
    def test_large_file_not_slow(self):
        lines = []
        for i in range(10000):
            e = i * 0.5
            lines.append(f"G1 X{i % 200} Y{i % 200} E{e}")
        lines.append(";TIME:7200")

        start = time.monotonic()
        est = CostEstimator().estimate_from_gcode(lines)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"Parsing took {elapsed:.2f}s, expected < 1s"
        assert est.filament_length_meters > 0
        assert est.estimated_time_seconds == 7200
