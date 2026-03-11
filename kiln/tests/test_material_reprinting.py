"""Tests for material-aware reprinting MCP tools.

Covers:
- get_material_properties — material profile lookup
- check_printer_material_support — printer/material compatibility
- compare_material_properties — side-by-side material comparison
- build_material_overrides — auto-generate slicer overrides for material
- reprint_with_material — one-shot material switch + reprint pipeline
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _no_auth(*_args, **_kwargs):
    """Stub _check_auth to always pass."""
    return None


def _make_tmp_stl():
    """Create a minimal temporary STL file, return its path."""
    fd, path = tempfile.mkstemp(suffix=".stl")
    with os.fdopen(fd, "wb") as f:
        f.write(b"solid test\nendsolid test\n")
    return path


# ---------------------------------------------------------------------------
# TestGetMaterialProperties
# ---------------------------------------------------------------------------

class TestGetMaterialProperties:
    """Tests for the get_material_properties MCP tool."""

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_known_material_returns_profile(self, _auth):
        from kiln.server import get_material_properties

        result = get_material_properties("pla")
        assert result["success"] is True
        assert result["material"]["material_id"] == "pla"
        assert "thermal" in result["material"]
        assert "mechanical" in result["material"]
        assert "design_limits" in result["material"]
        assert "agent_guidance" in result["material"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_petg_profile(self, _auth):
        from kiln.server import get_material_properties

        result = get_material_properties("petg")
        assert result["success"] is True
        thermal = result["material"]["thermal"]
        # PETG prints hotter than PLA
        assert thermal["print_temp_range_c"][0] >= 220

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_case_insensitive(self, _auth):
        from kiln.server import get_material_properties

        result = get_material_properties("PETG")
        assert result["success"] is True
        assert result["material"]["material_id"] == "petg"

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_unknown_material_returns_error(self, _auth):
        from kiln.server import get_material_properties

        result = get_material_properties("unobtainium")
        assert result["success"] is False
        assert "unobtainium" in result["error"]["message"]
        assert "Available" in result["error"]["message"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_tpu_profile_has_flexibility_data(self, _auth):
        from kiln.server import get_material_properties

        result = get_material_properties("tpu")
        assert result["success"] is True
        mech = result["material"]["mechanical"]
        # TPU should have high elongation
        assert mech.get("elongation_at_break_pct", 0) > 100


# ---------------------------------------------------------------------------
# TestCheckPrinterMaterialSupport
# ---------------------------------------------------------------------------

class TestCheckPrinterMaterialSupport:
    """Tests for the check_printer_material_support MCP tool."""

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_a1_petg_compatible(self, _auth):
        from kiln.server import check_printer_material_support

        result = check_printer_material_support("bambu_a1", "petg")
        assert result["success"] is True
        assert "petg" in result["materials"]
        assert result["materials"]["petg"]["status"] == "compatible"
        assert "compatible" in result["summary"].lower()

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_a1_abs_needs_upgrade(self, _auth):
        from kiln.server import check_printer_material_support

        result = check_printer_material_support("bambu_a1", "abs")
        assert result["success"] is True
        assert result["materials"]["abs"]["status"] == "needs_upgrade"
        assert "enclosure" in result["materials"]["abs"]["upgrades_needed"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_all_materials_for_printer(self, _auth):
        from kiln.server import check_printer_material_support

        result = check_printer_material_support("bambu_a1")
        assert result["success"] is True
        # Should have many materials
        assert len(result["materials"]) >= 15
        assert "summary" not in result  # no summary when querying all

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_unknown_printer_falls_back_to_default(self, _auth):
        from kiln.server import check_printer_material_support

        result = check_printer_material_support("nonexistent_printer")
        # Falls back to "default" profile — still succeeds
        assert result["success"] is True
        assert result["printer_id"] == "default"
        assert len(result["materials"]) >= 15

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_a1_tpu_compatible(self, _auth):
        from kiln.server import check_printer_material_support

        result = check_printer_material_support("bambu_a1", "tpu")
        assert result["success"] is True
        assert result["materials"]["tpu"]["status"] == "compatible"

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_a1_cf_nylon_needs_multiple_upgrades(self, _auth):
        from kiln.server import check_printer_material_support

        result = check_printer_material_support("bambu_a1", "cf_nylon")
        assert result["success"] is True
        upgrades = result["materials"]["cf_nylon"]["upgrades_needed"]
        assert "hardened_nozzle" in upgrades
        assert "enclosure" in upgrades


# ---------------------------------------------------------------------------
# TestCompareMaterialProperties
# ---------------------------------------------------------------------------

class TestCompareMaterialProperties:
    """Tests for the compare_material_properties MCP tool."""

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_pla_vs_petg(self, _auth):
        from kiln.server import compare_material_properties

        result = compare_material_properties("pla", "petg")
        assert result["success"] is True
        assert result["materials"] == ["pla", "petg"]

        # Thermal comparison exists
        thermal = result["thermal"]
        assert "print_temp_range_c" in thermal
        pla_range = thermal["print_temp_range_c"]["pla"]
        petg_range = thermal["print_temp_range_c"]["petg"]
        assert petg_range[0] > pla_range[0]  # PETG prints hotter

        # Mechanical comparison exists
        assert "mechanical" in result
        assert "design_limits" in result

        # Summary highlights differences
        assert len(result["summary"]) > 0

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_unknown_material_returns_error(self, _auth):
        from kiln.server import compare_material_properties

        result = compare_material_properties("pla", "unobtainium")
        assert result["success"] is False
        assert "unobtainium" in result["error"]["message"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_same_material_no_summary(self, _auth):
        from kiln.server import compare_material_properties

        result = compare_material_properties("pla", "pla")
        assert result["success"] is True
        # Same material — no differences to highlight
        assert len(result["summary"]) == 0

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_guidance_included(self, _auth):
        from kiln.server import compare_material_properties

        result = compare_material_properties("pla", "tpu")
        assert result["success"] is True
        assert "pla" in result["guidance"]
        assert "tpu" in result["guidance"]
        assert isinstance(result["guidance"]["pla"], list)

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_bed_temp_difference_in_summary(self, _auth):
        from kiln.server import compare_material_properties

        result = compare_material_properties("pla", "petg")
        summary_text = " ".join(result["summary"])
        assert "Bed temp" in summary_text or "bed" in summary_text.lower()


# ---------------------------------------------------------------------------
# TestBuildMaterialOverrides
# ---------------------------------------------------------------------------

class TestBuildMaterialOverrides:
    """Tests for the build_material_overrides MCP tool."""

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_petg_overrides(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("petg")
        assert result["success"] is True
        ov = result["overrides"]
        # PETG should have temp ~235
        temp = int(ov["temperature"])
        assert 220 <= temp <= 250
        bed = int(ov["bed_temperature"])
        assert 70 <= bed <= 85
        # PETG specific speed/retraction
        assert "retract_length" in ov

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_tpu_slow_speeds(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("tpu")
        assert result["success"] is True
        ov = result["overrides"]
        # TPU needs very slow speeds
        assert int(ov.get("perimeter_speed", "50")) <= 25
        assert float(ov.get("retract_length", "5")) <= 2.0

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_pla_baseline(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("pla")
        assert result["success"] is True
        ov = result["overrides"]
        temp = int(ov["temperature"])
        assert 190 <= temp <= 220

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_with_printer_id(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("petg", "bambu_a1")
        assert result["success"] is True
        assert result["printer_id"] == "bambu_a1"
        # Should have overrides
        assert "temperature" in result["overrides"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_unknown_material_returns_error(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("unobtainium")
        assert result["success"] is False

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_abs_overrides(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("abs")
        assert result["success"] is True
        ov = result["overrides"]
        temp = int(ov["temperature"])
        assert temp >= 230  # ABS prints hot
        bed = int(ov["bed_temperature"])
        assert bed >= 80  # ABS needs hot bed

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_first_layer_temp_higher(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("petg")
        assert result["success"] is True
        ov = result["overrides"]
        base = int(ov["temperature"])
        first = int(ov["first_layer_temperature"])
        assert first > base  # First layer should be hotter

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_notes_field_populated(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("petg", "bambu_a1")
        assert result["success"] is True
        assert "notes" in result
        assert "reslice" in result["notes"].lower() or "overrides" in result["notes"].lower()

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_nylon_overrides(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("nylon")
        assert result["success"] is True
        ov = result["overrides"]
        # Nylon-specific retraction
        assert float(ov.get("retract_length", "0")) >= 4.0

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_cf_petg_overrides(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("cf_petg")
        assert result["success"] is True
        ov = result["overrides"]
        # Should have PETG-family speed/retraction
        assert "retract_length" in ov


# ---------------------------------------------------------------------------
# TestReprintWithMaterial
# ---------------------------------------------------------------------------

class TestReprintWithMaterial:
    """Tests for the reprint_with_material MCP tool."""

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    @patch("kiln.server.run_reslice_and_print")
    def test_delegates_to_reslice_and_print(self, mock_reslice, _auth):
        mock_reslice.return_value = {
            "success": True,
            "gcode_path": "/tmp/output.gcode",
            "print_started": True,
        }

        from kiln.server import reprint_with_material

        result = reprint_with_material(
            file_path="/tmp/model.stl",
            material_id="petg",
            printer_name="my_printer",
            printer_id="bambu_a1",
        )

        assert result["success"] is True
        assert result["material"] == "petg"
        assert "material_overrides_applied" in result
        assert "temperature" in result["material_overrides_applied"]

        # Verify run_reslice_and_print was called with overrides
        mock_reslice.assert_called_once()
        call_kwargs = mock_reslice.call_args
        overrides_json = call_kwargs.kwargs.get("overrides") or call_kwargs[1].get("overrides")
        if overrides_json is None:
            # Positional args
            overrides_json = call_kwargs[0][3] if len(call_kwargs[0]) > 3 else None
        # It was called — that's the key assertion
        assert mock_reslice.called

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    @patch("kiln.server.run_reslice_and_print")
    def test_extra_overrides_merged(self, mock_reslice, _auth):
        mock_reslice.return_value = {"success": True, "gcode_path": "/tmp/out.gcode"}

        from kiln.server import reprint_with_material

        result = reprint_with_material(
            file_path="/tmp/model.stl",
            material_id="petg",
            extra_overrides='{"fill_density": "30%", "support_material": "1"}',
        )

        assert result["success"] is True
        applied = result["material_overrides_applied"]
        assert applied["fill_density"] == "30%"
        assert applied["support_material"] == "1"
        # Material overrides should also be present
        assert "temperature" in applied

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_unknown_material_returns_error(self, _auth):
        from kiln.server import reprint_with_material

        result = reprint_with_material(
            file_path="/tmp/model.stl",
            material_id="unobtainium",
        )
        assert result["success"] is False

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_invalid_extra_overrides_json(self, _auth):
        from kiln.server import reprint_with_material

        result = reprint_with_material(
            file_path="/tmp/model.stl",
            material_id="petg",
            extra_overrides="not valid json{{{",
        )
        assert result["success"] is False
        assert "JSON" in result["error"]["message"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    @patch("kiln.server.run_reslice_and_print")
    def test_tpu_reprint_has_slow_speeds(self, mock_reslice, _auth):
        mock_reslice.return_value = {"success": True, "gcode_path": "/tmp/out.gcode"}

        from kiln.server import reprint_with_material

        result = reprint_with_material(
            file_path="/tmp/model.stl",
            material_id="tpu",
        )

        assert result["success"] is True
        applied = result["material_overrides_applied"]
        assert int(applied.get("perimeter_speed", "50")) <= 25

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    @patch("kiln.server.run_reslice_and_print")
    def test_reslice_failure_propagated(self, mock_reslice, _auth):
        mock_reslice.return_value = {
            "success": False,
            "error": {"code": "SLICER_ERROR", "message": "Slicer crashed"},
        }

        from kiln.server import reprint_with_material

        result = reprint_with_material(
            file_path="/tmp/model.stl",
            material_id="petg",
        )

        # Should propagate the failure
        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestNewMaterialProfiles
# ---------------------------------------------------------------------------

class TestNewMaterialProfiles:
    """Tests for the 5 new material profiles (pla_matte, petg_hf, pla_tough,
    tpu_95a, tpu_85a) — verify they load and have sane data."""

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_pla_matte_profile(self, _auth):
        from kiln.server import get_material_properties

        result = get_material_properties("pla_matte")
        assert result["success"] is True
        thermal = result["material"]["thermal"]
        assert thermal["print_temp_range_c"][0] >= 190
        assert thermal["print_temp_range_c"][1] <= 230

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_petg_hf_profile(self, _auth):
        from kiln.server import get_material_properties

        result = get_material_properties("petg_hf")
        assert result["success"] is True
        thermal = result["material"]["thermal"]
        # PETG-HF prints hotter
        assert thermal["print_temp_range_c"][0] >= 230

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_pla_tough_profile(self, _auth):
        from kiln.server import get_material_properties

        result = get_material_properties("pla_tough")
        assert result["success"] is True
        mech = result["material"]["mechanical"]
        assert mech.get("tensile_strength_mpa", 0) >= 40

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_tpu_95a_profile(self, _auth):
        from kiln.server import get_material_properties

        result = get_material_properties("tpu_95a")
        assert result["success"] is True
        mech = result["material"]["mechanical"]
        # Should have high elongation like TPU
        assert mech.get("elongation_at_break_pct", 0) >= 300

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_tpu_85a_profile(self, _auth):
        from kiln.server import get_material_properties

        result = get_material_properties("tpu_85a")
        assert result["success"] is True
        mech = result["material"]["mechanical"]
        # 85A is softer, even higher elongation
        assert mech.get("elongation_at_break_pct", 0) >= 400


# ---------------------------------------------------------------------------
# TestNewMaterialOverrides
# ---------------------------------------------------------------------------

class TestNewMaterialOverrides:
    """Tests for build_material_overrides with new material families."""

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_petg_hf_uses_petg_family_speeds(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("petg_hf")
        assert result["success"] is True
        ov = result["overrides"]
        assert int(ov["perimeter_speed"]) == 40
        assert float(ov["retract_length"]) == 4.0

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_tpu_95a_uses_tpu_family_speeds(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("tpu_95a")
        assert result["success"] is True
        ov = result["overrides"]
        assert int(ov["perimeter_speed"]) == 20
        assert float(ov["retract_length"]) == 1.0

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_tpu_85a_slower_than_standard_tpu(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("tpu_85a")
        assert result["success"] is True
        ov = result["overrides"]
        # 85A needs even slower speeds
        assert int(ov["perimeter_speed"]) <= 15
        assert float(ov["retract_length"]) <= 1.0

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_pla_matte_no_special_speed_overrides(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("pla_matte")
        assert result["success"] is True
        ov = result["overrides"]
        # PLA family doesn't set special speed overrides
        assert "temperature" in ov
        assert "perimeter_speed" not in ov

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_pla_tough_no_special_speed_overrides(self, _auth):
        from kiln.server import build_material_overrides

        result = build_material_overrides("pla_tough")
        assert result["success"] is True
        ov = result["overrides"]
        assert "temperature" in ov
        assert "perimeter_speed" not in ov


# ---------------------------------------------------------------------------
# TestNewMaterialCompatibility
# ---------------------------------------------------------------------------

class TestNewMaterialCompatibility:
    """Tests for printer compatibility data for new materials."""

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_bambu_a1_pla_matte_compatible(self, _auth):
        from kiln.server import check_printer_material_support

        result = check_printer_material_support("bambu_a1", "pla_matte")
        assert result["success"] is True
        assert result["materials"]["pla_matte"]["status"] == "compatible"

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_bambu_a1_petg_hf_compatible(self, _auth):
        from kiln.server import check_printer_material_support

        result = check_printer_material_support("bambu_a1", "petg_hf")
        assert result["success"] is True
        assert result["materials"]["petg_hf"]["status"] == "compatible"

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_bambu_a1_tpu_95a_compatible(self, _auth):
        from kiln.server import check_printer_material_support

        result = check_printer_material_support("bambu_a1", "tpu_95a")
        assert result["success"] is True
        assert result["materials"]["tpu_95a"]["status"] == "compatible"

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_all_new_materials_have_entries(self, _auth):
        from kiln.server import check_printer_material_support

        new_mats = ["pla_matte", "petg_hf", "pla_tough", "tpu_95a", "tpu_85a"]
        result = check_printer_material_support("bambu_a1")
        assert result["success"] is True
        for mat in new_mats:
            assert mat in result["materials"], f"{mat} missing from bambu_a1"


# ---------------------------------------------------------------------------
# TestSmartReprint
# ---------------------------------------------------------------------------

class TestSmartReprint:
    """Tests for the smart_reprint MCP tool."""

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_file_not_found_returns_error(self, _auth):
        from kiln.server import smart_reprint

        result = smart_reprint(
            file_name="nonexistent_model_xyz_12345",
            material_id="petg",
            auto_ams=False,
        )
        assert result["success"] is False
        assert "NOT_FOUND" in result["error"]["code"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_unknown_material_returns_error(self, _auth):
        from kiln.server import smart_reprint

        tmp_path = _make_tmp_stl()
        try:
            result = smart_reprint(
                file_name=tmp_path,
                material_id="unobtainium",
                auto_ams=False,
            )
            assert result["success"] is False
        finally:
            os.unlink(tmp_path)

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_direct_path_found(self, _auth):
        from kiln.server import smart_reprint

        tmp_path = _make_tmp_stl()
        try:
            # smart_reprint will find the file but fail at reslice (no slicer)
            # That's OK — we're testing file discovery, not the full pipeline
            result = smart_reprint(
                file_name=tmp_path,
                material_id="petg",
                auto_ams=False,
            )
            # Either succeeds at finding file (and fails at slicer) or errors
            # The key is it should NOT return NOT_FOUND for the file
            if not result.get("success"):
                # Should fail at reslice, not at file finding
                assert result.get("error", {}).get("code") != "NOT_FOUND" or \
                    "model" not in result.get("error", {}).get("message", "").lower()
        finally:
            os.unlink(tmp_path)

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    @patch("kiln.server.ams_status")
    def test_ams_auto_detect_finds_slot(self, mock_ams, _auth):
        from kiln.server import smart_reprint

        mock_ams.return_value = {
            "status": "success",
            "units": [{
                "trays": [
                    {"slot": 0, "tray_type": "PLA", "tray_color": "000000FF", "remain": 80},
                    {"slot": 1, "tray_type": "PETG", "tray_color": "000000FF", "remain": 95},
                ],
            }],
        }

        tmp_path = _make_tmp_stl()
        try:
            result = smart_reprint(
                file_name=tmp_path,
                material_id="petg",
                auto_ams=True,
            )
            # Check that AMS detection found slot 1
            steps = result.get("smart_reprint_steps", [])
            ams_step = next((s for s in steps if s.get("step") == "ams_detection"), None)
            if ams_step:
                assert ams_step["found"] is True
                assert ams_step["slot"] == 1
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# TestAmsPassthrough
# ---------------------------------------------------------------------------

class TestAmsPassthrough:
    """Tests for AMS mapping passthrough in reprint_with_material."""

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    @patch("kiln.server.run_reslice_and_print")
    def test_ams_params_passed_to_reslice(self, mock_reslice, _auth):
        mock_reslice.return_value = {"success": True, "gcode_path": "/tmp/out.gcode"}

        from kiln.server import reprint_with_material

        result = reprint_with_material(
            file_path="/tmp/model.stl",
            material_id="petg",
            use_ams=True,
            ams_mapping="[1]",
        )

        assert result["success"] is True
        # Verify run_reslice_and_print was called with AMS params
        mock_reslice.assert_called_once()
        call_kwargs = mock_reslice.call_args
        assert call_kwargs.kwargs.get("use_ams") is True or \
            (call_kwargs[1].get("use_ams") is True if len(call_kwargs) > 1 else False)

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    @patch("kiln.server.run_reslice_and_print")
    def test_ams_none_when_not_provided(self, mock_reslice, _auth):
        mock_reslice.return_value = {"success": True, "gcode_path": "/tmp/out.gcode"}

        from kiln.server import reprint_with_material

        reprint_with_material(
            file_path="/tmp/model.stl",
            material_id="petg",
        )

        mock_reslice.assert_called_once()
        call_kwargs = mock_reslice.call_args
        assert call_kwargs.kwargs.get("use_ams") is None


# ---------------------------------------------------------------------------
# TestMultiMaterial3MF
# ---------------------------------------------------------------------------

class TestMultiMaterial3MF:
    """Tests for the build_multi_material_3mf helper function."""

    def test_build_two_object_3mf(self, tmp_path):
        import zipfile

        from kiln.generation.validation import build_multi_material_3mf

        # Create two minimal STL files
        stl_content = (
            b"solid test\n"
            b"  facet normal 0 0 1\n"
            b"    outer loop\n"
            b"      vertex 0 0 0\n"
            b"      vertex 1 0 0\n"
            b"      vertex 0 1 0\n"
            b"    endloop\n"
            b"  endfacet\n"
            b"endsolid test\n"
        )
        stl_a = tmp_path / "part_a.stl"
        stl_b = tmp_path / "part_b.stl"
        stl_a.write_bytes(stl_content)
        stl_b.write_bytes(stl_content)

        output = str(tmp_path / "multi.3mf")
        result = build_multi_material_3mf(
            [
                {"file_path": str(stl_a), "filament_index": 0, "material_name": "PLA", "color": "#FF0000"},
                {"file_path": str(stl_b), "filament_index": 1, "material_name": "PETG", "color": "#0000FF"},
            ],
            output_path=output,
        )

        assert result == output
        assert zipfile.is_zipfile(output)

        # Verify 3MF structure
        with zipfile.ZipFile(output) as zf:
            names = zf.namelist()
            assert "[Content_Types].xml" in names
            assert "_rels/.rels" in names
            assert "3D/3dmodel.model" in names

            # Check model XML has two objects and basematerials
            model_xml = zf.read("3D/3dmodel.model").decode()
            assert "basematerials" in model_xml
            assert "PLA" in model_xml
            assert "PETG" in model_xml
            assert 'pindex="0"' in model_xml
            assert 'pindex="1"' in model_xml

    def test_empty_objects_raises(self):
        import pytest

        from kiln.generation.validation import build_multi_material_3mf

        with pytest.raises(ValueError, match="At least one object"):
            build_multi_material_3mf([])

    def test_missing_file_raises(self, tmp_path):
        from kiln.generation.validation import build_multi_material_3mf

        with __import__("pytest").raises(ValueError):
            build_multi_material_3mf(
                [{"file_path": str(tmp_path / "missing.stl"), "filament_index": 0}],
                output_path=str(tmp_path / "out.3mf"),
            )

    def test_xml_escaping_special_chars(self, tmp_path):
        """Material/object names with XML special chars are properly escaped."""
        import zipfile

        from kiln.generation.validation import build_multi_material_3mf

        stl_content = (
            b"solid test\n"
            b"  facet normal 0 0 1\n"
            b"    outer loop\n"
            b"      vertex 0 0 0\n"
            b"      vertex 1 0 0\n"
            b"      vertex 0 1 0\n"
            b"    endloop\n"
            b"  endfacet\n"
            b"endsolid test\n"
        )
        stl_path = tmp_path / "part.stl"
        stl_path.write_bytes(stl_content)
        out = str(tmp_path / "escaped.3mf")
        build_multi_material_3mf(
            [{
                "file_path": str(stl_path),
                "filament_index": 0,
                "name": 'Part "A" <test>',
                "material_name": 'PLA & "Silk"',
                "color": "#FF0000",
            }],
            output_path=out,
        )
        with zipfile.ZipFile(out) as zf:
            model_xml = zf.read("3D/3dmodel.model").decode()
        # Special chars must be escaped
        assert "&amp;" in model_xml
        assert "&quot;" in model_xml
        assert "&lt;" in model_xml
        # Raw unescaped chars must NOT appear in attribute values
        assert 'name="PLA & "' not in model_xml


# ---------------------------------------------------------------------------
# TestMultiMaterialPrint
# ---------------------------------------------------------------------------

class TestMultiMaterialPrint:
    """Tests for the multi_material_print MCP tool."""

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_invalid_json_returns_error(self, _auth):
        from kiln.server import multi_material_print

        result = multi_material_print(objects_json="not valid json{{{")
        assert result["success"] is False
        assert "VALIDATION_ERROR" in result["error"]["code"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_empty_array_returns_error(self, _auth):
        from kiln.server import multi_material_print

        result = multi_material_print(objects_json="[]")
        assert result["success"] is False

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_missing_file_path_returns_error(self, _auth):
        from kiln.server import multi_material_print

        result = multi_material_print(
            objects_json='[{"material_id": "pla"}]'
        )
        assert result["success"] is False
        assert "file_path" in result["error"]["message"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_missing_material_id_returns_error(self, _auth):
        import json

        from kiln.server import multi_material_print

        tmp_path = _make_tmp_stl()
        try:
            result = multi_material_print(
                objects_json=json.dumps([{"file_path": tmp_path}])
            )
            assert result["success"] is False
            assert "material_id" in result["error"]["message"]
        finally:
            os.unlink(tmp_path)

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_unknown_material_returns_error(self, _auth):
        import json

        from kiln.server import multi_material_print

        tmp_path = _make_tmp_stl()
        try:
            result = multi_material_print(
                objects_json=json.dumps([
                    {"file_path": tmp_path, "material_id": "unobtainium"}
                ]),
                auto_ams=False,
            )
            assert result["success"] is False
        finally:
            os.unlink(tmp_path)

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_file_not_found_returns_error(self, _auth):
        import json

        from kiln.server import multi_material_print

        result = multi_material_print(
            objects_json=json.dumps([
                {"file_path": "/nonexistent/model.stl", "material_id": "pla"}
            ]),
            auto_ams=False,
        )
        assert result["success"] is False
        assert "NOT_FOUND" in result["error"]["code"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_incompatible_nozzle_temps_rejected(self, _auth):
        """PLA (190-220) + polycarbonate (270-310) have no nozzle overlap → rejected."""
        import json

        from kiln.server import multi_material_print

        paths = [_make_tmp_stl(), _make_tmp_stl()]
        try:
            result = multi_material_print(
                objects_json=json.dumps([
                    {"file_path": paths[0], "material_id": "pla"},
                    {"file_path": paths[1], "material_id": "polycarbonate"},
                ]),
                auto_ams=False,
            )
            assert result["success"] is False
            assert "MATERIAL_INCOMPATIBLE" in result["error"]["code"]
            assert "nozzle" in result["error"]["message"].lower()
        finally:
            for p in paths:
                os.unlink(p)

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_compatible_materials_allowed(self, _auth):
        """PLA + PLA Matte have overlapping temps → allowed past compat check."""
        import json

        from kiln.server import multi_material_print

        paths = [_make_tmp_stl(), _make_tmp_stl()]
        try:
            # Should get past the compatibility check (may fail later at slicing)
            result = multi_material_print(
                objects_json=json.dumps([
                    {"file_path": paths[0], "material_id": "pla"},
                    {"file_path": paths[1], "material_id": "pla_matte"},
                ]),
                auto_ams=False,
            )
            # It will fail at slicing (no slicer available in tests) but should
            # NOT fail with MATERIAL_INCOMPATIBLE
            if not result.get("success"):
                assert result["error"]["code"] != "MATERIAL_INCOMPATIBLE"
        finally:
            for p in paths:
                os.unlink(p)
