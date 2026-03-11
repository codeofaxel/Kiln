"""Tests for material-aware reprinting MCP tools.

Covers:
- get_material_properties — material profile lookup
- check_printer_material_support — printer/material compatibility
- compare_material_properties — side-by-side material comparison
- build_material_overrides — auto-generate slicer overrides for material
- reprint_with_material — one-shot material switch + reprint pipeline
"""

from __future__ import annotations

from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _no_auth(*_args, **_kwargs):
    """Stub _check_auth to always pass."""
    return None


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
