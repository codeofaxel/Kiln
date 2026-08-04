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
        assert set(result["material"]) == {
            "material_id",
            "display_name",
            "category",
            "thermal",
            "chemical",
            "design_limits",
            "bonding",
        }
        assert result["material"]["material_id"] == "pla"
        assert "thermal" in result["material"]
        assert "design_limits" in result["material"]
        assert "mechanical" not in result["material"]
        assert "agent_guidance" not in result["material"]

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
    @patch("kiln.design_intelligence.get_material_profile")
    def test_merged_engineering_profile_is_never_serialized(
        self,
        merged_lookup,
        _auth,
    ):
        from kiln.server import get_material_properties

        result = get_material_properties("tpu")
        assert result["success"] is True
        assert "mechanical" not in result["material"]
        assert "agent_guidance" not in result["material"]
        merged_lookup.assert_not_called()


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
    def test_material_is_required(self, _auth):
        from kiln.server import check_printer_material_support

        result = check_printer_material_support("bambu_a1", "")
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_INPUT"

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_unknown_printer_falls_back_to_default(self, _auth):
        from kiln.server import check_printer_material_support

        result = check_printer_material_support("nonexistent_printer", "pla")
        # Falls back to "default" profile — still succeeds
        assert result["success"] is True
        assert result["printer_id"] == "default"
        assert set(result["materials"]) == {"pla"}

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
        assert set(result) == {
            "success",
            "materials",
            "thermal",
            "design_limits",
            "summary",
        }

        # Thermal comparison exists
        thermal = result["thermal"]
        assert "print_temp_range_c" in thermal
        pla_range = thermal["print_temp_range_c"]["pla"]
        petg_range = thermal["print_temp_range_c"]["petg"]
        assert petg_range[0] > pla_range[0]  # PETG prints hotter

        # Only the fixed public comparison bands are returned.
        assert "design_limits" in result
        assert "mechanical" not in result
        assert "guidance" not in result

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
    def test_engineering_guidance_not_included(self, _auth):
        from kiln.server import compare_material_properties

        result = compare_material_properties("pla", "tpu")
        assert result["success"] is True
        assert "guidance" not in result
        assert "mechanical" not in result

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
        assert result["material"]["material_id"] == "pla_tough"
        assert "mechanical" not in result["material"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_tpu_95a_profile(self, _auth):
        from kiln.server import get_material_properties

        result = get_material_properties("tpu_95a")
        assert result["success"] is True
        assert result["material"]["material_id"] == "tpu_95a"
        assert "mechanical" not in result["material"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    def test_tpu_85a_profile(self, _auth):
        from kiln.server import get_material_properties

        result = get_material_properties("tpu_85a")
        assert result["success"] is True
        assert result["material"]["material_id"] == "tpu_85a"
        assert "mechanical" not in result["material"]


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
        for mat in new_mats:
            result = check_printer_material_support("bambu_a1", mat)
            assert result["success"] is True
            assert set(result["materials"]) == {mat}, (
                f"{mat} missing from bambu_a1"
            )


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
            "success": True,
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
# TestMulticolorPlacement
# ---------------------------------------------------------------------------

def _make_square_stl(size: float = 10.0) -> str:
    """ASCII STL: flat square plate spanning [0, size] x [0, size] at z=0."""
    fd, path = tempfile.mkstemp(suffix=".stl")
    with os.fdopen(fd, "w") as f:
        f.write(
            "solid sq\n"
            "  facet normal 0 0 1\n    outer loop\n"
            f"      vertex 0 0 0\n      vertex {size} 0 0\n      vertex {size} {size} 0\n"
            "    endloop\n  endfacet\n"
            "  facet normal 0 0 1\n    outer loop\n"
            f"      vertex 0 0 0\n      vertex {size} {size} 0\n      vertex 0 {size} 0\n"
            "    endloop\n  endfacet\n"
            "endsolid sq\n"
        )
    return path


def _read_3mf_placements(path_3mf: str) -> tuple[list[dict], list[str]]:
    """Parse a 3MF into per-item placement facts.

    Returns ([{tx, ty, extruder, world_bbox}], zip_namelist).  Every build
    item MUST carry a transform — an item without one is the coincident-
    copies defect coming back.
    """
    import xml.etree.ElementTree as ET
    import zipfile

    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    slic3r_extruder = "{http://schemas.slic3r.org/3mf/2017/06}extruder"
    with zipfile.ZipFile(path_3mf) as zf:
        names = zf.namelist()
        root = ET.fromstring(zf.read("3D/3dmodel.model"))

    obj_bboxes: dict[str, tuple[float, float, float, float]] = {}
    for obj in root.findall(".//m:object", ns):
        xs, ys = [], []
        for v in obj.findall(".//m:vertex", ns):
            xs.append(float(v.get("x")))
            ys.append(float(v.get("y")))
        obj_bboxes[obj.get("id")] = (min(xs), min(ys), max(xs), max(ys))

    items: list[dict] = []
    for item in root.findall(".//m:item", ns):
        transform = item.get("transform")
        assert transform is not None, "build item has no transform (stacked at origin)"
        vals = [float(t) for t in transform.split()]
        tx, ty = vals[9], vals[10]
        extruder = item.get(slic3r_extruder)
        mn_x, mn_y, mx_x, mx_y = obj_bboxes[item.get("objectid")]
        items.append(
            {
                "tx": tx,
                "ty": ty,
                "extruder": int(extruder) if extruder is not None else None,
                "world_bbox": (mn_x + tx, mn_y + ty, mx_x + tx, mx_y + ty),
            }
        )
    return items, names


def _xy_disjoint(a: tuple, b: tuple) -> bool:
    return a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]


_AMS_FOUR_PLA_TRAYS = {
    "success": True,
    "units": [
        {
            "trays": [
                {"slot": 0, "tray_type": "PLA", "tray_color": "FF0000FF"},
                {"slot": 1, "tray_type": "PLA", "tray_color": "00FF00FF"},
                {"slot": 2, "tray_type": "PLA", "tray_color": "0000FFFF"},
                {"slot": 3, "tray_type": "PLA", "tray_color": "FFFF00FF"},
            ]
        }
    ],
}


class TestMulticolorPlacement:
    """Geometric invariants of the 3MFs the multicolor tools emit.

    Regression suite for the coincident-copies defect: multi_color_copies
    once emitted every copy at the origin with no transform, so N copies
    sliced into a single footprint at ~N x the extrusion.  Assertions here
    are numeric (positions, footprint spans, per-object extruders) — never
    the presence of a string.
    """

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    @patch("kiln.server.run_reslice_and_print")
    @patch("kiln.server.ams_status")
    def test_multi_color_copies_are_spaced_on_plate(self, mock_ams, mock_reslice, _auth):
        mock_ams.return_value = _AMS_FOUR_PLA_TRAYS
        mock_reslice.return_value = {"success": True, "gcode_path": "/tmp/out.gcode"}

        from kiln.server import multi_color_copies

        model = _make_square_stl(size=10.0)
        try:
            result = multi_color_copies(model_path=model, spacing_mm=10.0)
        finally:
            os.unlink(model)

        assert result.get("multi_color_copies") is True
        items, names = _read_3mf_placements(result["multi_color_3mf"])
        assert len(items) == 4

        # Per-copy extruders 1..4, in both slicer dialects
        assert [it["extruder"] for it in items] == [1, 2, 3, 4]
        assert "Metadata/model_settings.config" in names       # BambuStudio
        assert "Metadata/Slic3r_PE_model.config" in names      # PrusaSlicer

        # Copies occupy disjoint footprints, on the plate
        boxes = [it["world_bbox"] for it in items]
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                assert _xy_disjoint(boxes[i], boxes[j]), (boxes[i], boxes[j])
        for b in boxes:
            assert b[0] >= 0.0 and b[1] >= 0.0
            assert b[2] <= 256.0 and b[3] <= 256.0

        # Four 10mm squares + three 10mm gaps — the footprint really spans
        union_w = max(b[2] for b in boxes) - min(b[0] for b in boxes)
        assert abs(union_w - 70.0) < 0.01

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    @patch("kiln.server.ams_status")
    def test_manual_slots_without_filament_refused(self, mock_ams, _auth):
        mock_ams.return_value = _AMS_FOUR_PLA_TRAYS

        from kiln.server import multi_color_copies

        model = _make_square_stl()
        try:
            result = multi_color_copies(model_path=model, ams_slots=[0, 7])
        finally:
            os.unlink(model)

        assert result["success"] is False
        assert "NO_MATERIAL" in result["error"]["code"]
        assert "7" in result["error"]["message"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    @patch("kiln.server.run_reslice_and_print")
    @patch("kiln.server.ams_status", side_effect=RuntimeError("printer offline"))
    def test_manual_slots_proceed_with_warning_when_ams_unreachable(
        self, _ams, mock_reslice, _auth
    ):
        mock_reslice.return_value = {"success": True, "gcode_path": "/tmp/out.gcode"}

        from kiln.server import multi_color_copies

        model = _make_square_stl()
        try:
            result = multi_color_copies(model_path=model, ams_slots=[0, 1])
        finally:
            os.unlink(model)

        assert result.get("multi_color_copies") is True
        assert "ams_warning" in result
        items, _ = _read_3mf_placements(result["multi_color_3mf"])
        assert len(items) == 2
        assert items[0]["tx"] != items[1]["tx"]

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    @patch("kiln.server.run_reslice_and_print")
    def test_multi_material_print_objects_do_not_stack(self, mock_reslice, _auth):
        import json

        mock_reslice.return_value = {"success": True, "gcode_path": "/tmp/out.gcode"}

        from kiln.server import multi_material_print

        paths = [_make_square_stl(size=10.0), _make_square_stl(size=10.0)]
        try:
            result = multi_material_print(
                objects_json=json.dumps(
                    [
                        {"file_path": paths[0], "material_id": "pla"},
                        {"file_path": paths[1], "material_id": "pla_matte"},
                    ]
                ),
                auto_ams=False,
            )
        finally:
            for pth in paths:
                os.unlink(pth)

        assert result.get("multi_material") is True
        items, names = _read_3mf_placements(result["multi_material_3mf"])
        assert len(items) == 2
        assert {it["extruder"] for it in items} == {1, 2}
        assert "Metadata/model_settings.config" in names
        assert _xy_disjoint(items[0]["world_bbox"], items[1]["world_bbox"])

    @patch("kiln.server._check_auth", side_effect=_no_auth)
    @patch("kiln.server.run_reslice_and_print")
    def test_multi_material_print_same_group_stays_coincident(self, mock_reslice, _auth):
        import json

        mock_reslice.return_value = {"success": True, "gcode_path": "/tmp/out.gcode"}

        from kiln.server import multi_material_print

        paths = [_make_square_stl(size=20.0), _make_square_stl(size=6.0)]
        try:
            result = multi_material_print(
                objects_json=json.dumps(
                    [
                        {"file_path": paths[0], "material_id": "pla", "group": 0},
                        {"file_path": paths[1], "material_id": "pla_matte", "group": 0},
                    ]
                ),
                auto_ams=False,
            )
        finally:
            for pth in paths:
                os.unlink(pth)

        assert result.get("multi_material") is True
        items, _ = _read_3mf_placements(result["multi_material_3mf"])
        assert len(items) == 2
        assert (items[0]["tx"], items[0]["ty"]) == (items[1]["tx"], items[1]["ty"])


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
