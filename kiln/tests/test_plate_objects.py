"""Tests for Bambu .gcode.3mf plate object listing and extraction."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from kiln.generation.validation import (
    extract_plate_object_gcode,
    list_plate_objects,
)

# ---------------------------------------------------------------------------
# Fixtures — build minimal .gcode.3mf files for testing
# ---------------------------------------------------------------------------

_PLATE_JSON = {
    "bbox_all": [100.0, 100.0, 200.0, 200.0],
    "bbox_objects": [
        {
            "area": 500.0,
            "bbox": [100.0, 100.0, 150.0, 150.0],
            "id": 10,
            "layer_height": 0.2,
            "name": "PartA - body.stl",
        },
        {
            "area": 200.0,
            "bbox": [160.0, 160.0, 200.0, 200.0],
            "id": 20,
            "layer_height": 0.16,
            "name": "PartA - lid.stl",
        },
    ],
    "bed_type": "textured_plate",
    "filament_colors": ["#FFFFFF"],
    "filament_ids": [0],
    "first_extruder": 0,
    "nozzle_diameter": 0.4,
    "is_seq_print": False,
    "version": 2,
}

_GCODE = """\
; HEADER_BLOCK_START
; BambuStudio 02.05.00.66
; model label id: 100,200
; HEADER_BLOCK_END

; CONFIG_BLOCK_START
; some_setting = value
; CONFIG_BLOCK_END

; EXECUTABLE_BLOCK_START
G28 ; home
M190 S60 ; heat bed
M109 S210 ; heat nozzle
M83 ; relative extrusion
; MACHINE_START_GCODE_END

; CHANGE_LAYER
G1 Z0.2 F1200
; start printing object, unique label id: 100
G1 X100 Y100 F6000
G1 X150 Y100 E1.5 F1500
G1 X150 Y150 E1.5
; TYPE: Outer wall
G1 X100 Y150 E1.5
G1 X100 Y100 E1.5
; stop printing object, unique label id: 100
; start printing object, unique label id: 200
G1 X160 Y160 F6000
G1 X200 Y160 E1.0 F1500
G1 X200 Y200 E1.0
G1 X160 Y200 E1.0
G1 X160 Y160 E1.0
; stop printing object, unique label id: 200
; object ids of layer 1 start: 100,200
G1 X0 Y0 F6000 ; wipe
; object ids of this layer1 end: 100,200

; CHANGE_LAYER
G1 Z0.4 F1200
; start printing object, unique label id: 100
G1 X100 Y100 F6000
G1 X150 Y100 E1.5 F1500
; stop printing object, unique label id: 100
; start printing object, unique label id: 200
G1 X160 Y160 F6000
G1 X200 Y160 E1.0 F1500
; stop printing object, unique label id: 200

; MACHINE_END_GCODE_START
M400
M140 S0 ; bed off
M104 S0 ; hotend off
G1 Z10 F600
M18 X Y Z
; EXECUTABLE_BLOCK_END
"""


@pytest.fixture
def gcode_3mf(tmp_path):
    """Create a minimal .gcode.3mf with two objects."""
    out = tmp_path / "test_model.gcode.3mf"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("Metadata/plate_1.json", json.dumps(_PLATE_JSON))
        zf.writestr("Metadata/plate_1.gcode", _GCODE)
        # Empty 3D model (no mesh) — simulates Bambu export
        zf.writestr(
            "3D/3dmodel.model",
            '<?xml version="1.0"?><model><resources/><build/></model>',
        )
    return str(out)


@pytest.fixture
def no_plate_3mf(tmp_path):
    """Create a .3mf with no plate metadata."""
    out = tmp_path / "no_plate.3mf"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    return str(out)


# ---------------------------------------------------------------------------
# list_plate_objects tests
# ---------------------------------------------------------------------------


class TestListPlateObjects:
    def test_lists_all_objects(self, gcode_3mf):
        result = list_plate_objects(gcode_3mf)
        assert result["object_count"] == 2
        assert len(result["objects"]) == 2

    def test_object_names(self, gcode_3mf):
        result = list_plate_objects(gcode_3mf)
        names = [o["name"] for o in result["objects"]]
        assert names == ["PartA - body.stl", "PartA - lid.stl"]

    def test_label_id_mapping(self, gcode_3mf):
        result = list_plate_objects(gcode_3mf)
        ids = [o["label_id"] for o in result["objects"]]
        assert ids == [100, 200]

    def test_plate_metadata(self, gcode_3mf):
        result = list_plate_objects(gcode_3mf)
        assert result["bed_type"] == "textured_plate"
        assert result["nozzle_diameter_mm"] == 0.4
        assert result["filament_colors"] == ["#FFFFFF"]
        assert result["is_sequential_print"] is False

    def test_bbox_and_area(self, gcode_3mf):
        result = list_plate_objects(gcode_3mf)
        body = result["objects"][0]
        assert body["area_mm2"] == 500.0
        assert body["bbox"] == [100.0, 100.0, 150.0, 150.0]
        assert body["layer_height_mm"] == 0.2

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            list_plate_objects("/nonexistent/file.3mf")

    def test_no_plate_metadata(self, no_plate_3mf):
        with pytest.raises(ValueError, match="No plate metadata"):
            list_plate_objects(no_plate_3mf)


# ---------------------------------------------------------------------------
# extract_plate_object_gcode tests
# ---------------------------------------------------------------------------


class TestExtractPlateObjectGcode:
    def test_extracts_lid_by_exact_name(self, gcode_3mf, tmp_path):
        result = extract_plate_object_gcode(
            gcode_3mf,
            "PartA - lid.stl",
            output_path=str(tmp_path / "lid.gcode"),
        )
        assert os.path.isfile(result["output_path"])
        assert result["matched_object"]["name"] == "PartA - lid.stl"
        assert result["skipped_lines"] > 0

    def test_extracts_by_partial_name(self, gcode_3mf, tmp_path):
        result = extract_plate_object_gcode(
            gcode_3mf,
            "lid",
            output_path=str(tmp_path / "lid.gcode"),
        )
        assert result["matched_object"]["name"] == "PartA - lid.stl"

    def test_extracts_by_partial_name_no_ext(self, gcode_3mf, tmp_path):
        result = extract_plate_object_gcode(
            gcode_3mf,
            "body",
            output_path=str(tmp_path / "body.gcode"),
        )
        assert result["matched_object"]["name"] == "PartA - body.stl"

    def test_case_insensitive(self, gcode_3mf, tmp_path):
        result = extract_plate_object_gcode(
            gcode_3mf,
            "LID",
            output_path=str(tmp_path / "lid.gcode"),
        )
        assert result["matched_object"]["name"] == "PartA - lid.stl"

    def test_output_contains_start_gcode(self, gcode_3mf, tmp_path):
        result = extract_plate_object_gcode(
            gcode_3mf,
            "lid",
            output_path=str(tmp_path / "lid.gcode"),
        )
        gcode = Path(result["output_path"]).read_text()
        assert "G28 ; home" in gcode
        assert "M190 S60" in gcode
        assert "M109 S210" in gcode

    def test_output_contains_end_gcode(self, gcode_3mf, tmp_path):
        result = extract_plate_object_gcode(
            gcode_3mf,
            "lid",
            output_path=str(tmp_path / "lid.gcode"),
        )
        gcode = Path(result["output_path"]).read_text()
        assert "M140 S0 ; bed off" in gcode
        assert "M104 S0 ; hotend off" in gcode
        assert "M18 X Y Z" in gcode

    def test_output_excludes_other_object(self, gcode_3mf, tmp_path):
        result = extract_plate_object_gcode(
            gcode_3mf,
            "lid",
            output_path=str(tmp_path / "lid.gcode"),
        )
        gcode = Path(result["output_path"]).read_text()
        # Body object moves should NOT be present
        assert "G1 X150 Y100 E1.5 F1500" not in gcode
        assert "G1 X150 Y150 E1.5" not in gcode
        # Lid object moves SHOULD be present
        assert "G1 X200 Y160 E1.0 F1500" in gcode

    def test_output_keeps_layer_changes(self, gcode_3mf, tmp_path):
        result = extract_plate_object_gcode(
            gcode_3mf,
            "lid",
            output_path=str(tmp_path / "lid.gcode"),
        )
        gcode = Path(result["output_path"]).read_text()
        assert "; CHANGE_LAYER" in gcode
        assert "G1 Z0.2 F1200" in gcode
        assert "G1 Z0.4 F1200" in gcode

    def test_no_matching_object(self, gcode_3mf):
        with pytest.raises(ValueError, match="No object matching"):
            extract_plate_object_gcode(gcode_3mf, "nonexistent_part")

    def test_auto_output_path(self, gcode_3mf):
        result = extract_plate_object_gcode(gcode_3mf, "lid")
        assert os.path.isfile(result["output_path"])
        assert result["output_path"].endswith(".gcode")
        # Clean up
        os.unlink(result["output_path"])

    def test_all_objects_listed(self, gcode_3mf, tmp_path):
        result = extract_plate_object_gcode(
            gcode_3mf,
            "lid",
            output_path=str(tmp_path / "lid.gcode"),
        )
        assert result["all_objects"] == [
            "PartA - body.stl",
            "PartA - lid.stl",
        ]


# ---------------------------------------------------------------------------
# Edge case tests (audit findings)
# ---------------------------------------------------------------------------

_SINGLE_OBJECT_PLATE_JSON = {
    "bbox_all": [100.0, 100.0, 150.0, 150.0],
    "bbox_objects": [
        {
            "area": 500.0,
            "bbox": [100.0, 100.0, 150.0, 150.0],
            "id": 10,
            "layer_height": 0.2,
            "name": "solo_part.stl",
        },
    ],
    "bed_type": "textured_plate",
    "filament_colors": ["#000000"],
    "nozzle_diameter": 0.4,
    "is_seq_print": False,
}

_SINGLE_OBJECT_GCODE = """\
; HEADER_BLOCK_START
; model label id: 42
; HEADER_BLOCK_END
; EXECUTABLE_BLOCK_START
G28
M83
; MACHINE_START_GCODE_END
; CHANGE_LAYER
G1 Z0.2 F1200
; start printing object, unique label id: 42
G1 X100 Y100 E1.0 F1500
; stop printing object, unique label id: 42
; MACHINE_END_GCODE_START
M400
M18 X Y Z
; EXECUTABLE_BLOCK_END
"""

_THREE_OBJECT_PLATE_JSON = {
    "bbox_all": [0, 0, 300, 300],
    "bbox_objects": [
        {"area": 100, "bbox": [0, 0, 50, 50], "id": 1, "layer_height": 0.2, "name": "alpha.stl"},
        {"area": 100, "bbox": [100, 0, 150, 50], "id": 2, "layer_height": 0.2, "name": "beta.stl"},
        {"area": 100, "bbox": [200, 0, 250, 50], "id": 3, "layer_height": 0.2, "name": "gamma.stl"},
    ],
    "bed_type": "textured_plate",
    "filament_colors": ["#FF0000"],
    "nozzle_diameter": 0.4,
    "is_seq_print": False,
}

_THREE_OBJECT_GCODE = """\
; HEADER_BLOCK_START
; model label id: 10,20,30
; HEADER_BLOCK_END
; EXECUTABLE_BLOCK_START
G28
M83
; MACHINE_START_GCODE_END
; CHANGE_LAYER
G1 Z0.2 F1200
; start printing object, unique label id: 10
G1 X10 E1.0
; stop printing object, unique label id: 10
; start printing object, unique label id: 20
G1 X110 E1.0
; stop printing object, unique label id: 20
; start printing object, unique label id: 30
G1 X210 E1.0
; stop printing object, unique label id: 30
; MACHINE_END_GCODE_START
M400
M18 X Y Z
; EXECUTABLE_BLOCK_END
"""

_M82_GCODE = """\
; HEADER_BLOCK_START
; model label id: 1,2
; HEADER_BLOCK_END
; EXECUTABLE_BLOCK_START
G28
M82
; MACHINE_START_GCODE_END
; CHANGE_LAYER
G1 Z0.2 F1200
; start printing object, unique label id: 1
G1 X10 E10.0
; stop printing object, unique label id: 1
; MACHINE_END_GCODE_START
M400
; EXECUTABLE_BLOCK_END
"""


@pytest.fixture
def single_object_3mf(tmp_path):
    out = tmp_path / "single.gcode.3mf"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("Metadata/plate_1.json", json.dumps(_SINGLE_OBJECT_PLATE_JSON))
        zf.writestr("Metadata/plate_1.gcode", _SINGLE_OBJECT_GCODE)
    return str(out)


@pytest.fixture
def three_object_3mf(tmp_path):
    out = tmp_path / "three.gcode.3mf"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("Metadata/plate_1.json", json.dumps(_THREE_OBJECT_PLATE_JSON))
        zf.writestr("Metadata/plate_1.gcode", _THREE_OBJECT_GCODE)
    return str(out)


@pytest.fixture
def m82_3mf(tmp_path):
    out = tmp_path / "m82.gcode.3mf"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(
            "Metadata/plate_1.json",
            json.dumps({
                "bbox_objects": [
                    {"area": 100, "bbox": [0, 0, 50, 50], "id": 1, "layer_height": 0.2, "name": "a.stl"},
                    {"area": 100, "bbox": [100, 0, 150, 50], "id": 2, "layer_height": 0.2, "name": "b.stl"},
                ],
                "bed_type": "textured_plate",
                "filament_colors": ["#000"],
                "nozzle_diameter": 0.4,
                "is_seq_print": False,
            }),
        )
        zf.writestr("Metadata/plate_1.gcode", _M82_GCODE)
    return str(out)


class TestEdgeCases:
    def test_single_object_extraction(self, single_object_3mf, tmp_path):
        """Extracting the only object should produce valid output."""
        result = extract_plate_object_gcode(
            single_object_3mf,
            "solo_part",
            output_path=str(tmp_path / "solo.gcode"),
        )
        assert result["skipped_lines"] == 0
        gcode = Path(result["output_path"]).read_text()
        assert "G1 X100 Y100 E1.0 F1500" in gcode

    def test_three_objects_extract_middle(self, three_object_3mf, tmp_path):
        """Extracting beta from 3 objects should exclude alpha and gamma."""
        result = extract_plate_object_gcode(
            three_object_3mf,
            "beta",
            output_path=str(tmp_path / "beta.gcode"),
        )
        gcode = Path(result["output_path"]).read_text()
        assert "G1 X110 E1.0" in gcode  # beta's move
        assert "G1 X10 E1.0" not in gcode  # alpha excluded
        assert "G1 X210 E1.0" not in gcode  # gamma excluded

    def test_m82_absolute_extrusion_rejected(self, m82_3mf):
        """Files using M82 (absolute extrusion) should be rejected."""
        with pytest.raises(ValueError, match="absolute extrusion"):
            extract_plate_object_gcode(m82_3mf, "a")

    def test_ambiguous_match_raises(self, three_object_3mf):
        """Ambiguous substring match should raise, not silently pick first."""
        # "a" matches both "alpha.stl" and "gamma.stl"
        with pytest.raises(ValueError, match="Ambiguous match"):
            extract_plate_object_gcode(three_object_3mf, "a")

    def test_exact_match_preferred_over_substring(self, three_object_3mf, tmp_path):
        """Exact stem match should win over substring."""
        result = extract_plate_object_gcode(
            three_object_3mf,
            "beta",
            output_path=str(tmp_path / "beta.gcode"),
        )
        assert result["matched_object"]["name"] == "beta.stl"

    def test_not_a_zip(self, tmp_path):
        """Non-ZIP file should raise ValueError."""
        bad = tmp_path / "not_a_zip.3mf"
        bad.write_text("this is not a zip file")
        with pytest.raises(ValueError, match="Not a valid ZIP"):
            list_plate_objects(str(bad))


# ---------------------------------------------------------------------------
# Multi-plate support tests
# ---------------------------------------------------------------------------

_PLATE_2_JSON = {
    "bbox_all": [50.0, 50.0, 180.0, 180.0],
    "bbox_objects": [
        {
            "area": 300.0,
            "bbox": [50.0, 50.0, 120.0, 120.0],
            "id": 5,
            "layer_height": 0.12,
            "name": "bracket.stl",
        },
    ],
    "bed_type": "cool_plate",
    "filament_colors": ["#000000"],
    "filament_ids": [0],
    "first_extruder": 0,
    "nozzle_diameter": 0.4,
    "is_seq_print": False,
    "version": 2,
}

_PLATE_2_GCODE = """\
; HEADER_BLOCK_START
; BambuStudio 02.05.00.66
; model label id: 500
; HEADER_BLOCK_END

; CONFIG_BLOCK_START
; some_setting = value
; CONFIG_BLOCK_END

; EXECUTABLE_BLOCK_START
G28 ; home
M190 S55 ; heat bed
M109 S200 ; heat nozzle
M83 ; relative extrusion
; MACHINE_START_GCODE_END

; CHANGE_LAYER
G1 Z0.12 F1200
; start printing object, unique label id: 500
G1 X50 Y50 F6000
G1 X120 Y50 E2.0 F1500
G1 X120 Y120 E2.0
G1 X50 Y120 E2.0
G1 X50 Y50 E2.0
; stop printing object, unique label id: 500

; MACHINE_END_GCODE_START
M400
M140 S0 ; bed off
M104 S0 ; hotend off
G1 Z10 F600
M18 X Y Z
; EXECUTABLE_BLOCK_END
"""


@pytest.fixture
def multi_plate_3mf(tmp_path):
    """Create a .gcode.3mf with plate_1 and plate_2."""
    out = tmp_path / "multi_plate.gcode.3mf"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("Metadata/plate_1.json", json.dumps(_PLATE_JSON))
        zf.writestr("Metadata/plate_1.gcode", _GCODE)
        zf.writestr("Metadata/plate_2.json", json.dumps(_PLATE_2_JSON))
        zf.writestr("Metadata/plate_2.gcode", _PLATE_2_GCODE)
        zf.writestr(
            "3D/3dmodel.model",
            '<?xml version="1.0"?><model><resources/><build/></model>',
        )
    return str(out)


class TestMultiPlate:
    def test_plates_available_single(self, gcode_3mf):
        """Single-plate archive should report plates_available=[1]."""
        result = list_plate_objects(gcode_3mf)
        assert result["plates_available"] == [1]
        assert result["plate_number"] == 1

    def test_plates_available_multi(self, multi_plate_3mf):
        """Multi-plate archive should report both plates."""
        result = list_plate_objects(multi_plate_3mf, plate_number=1)
        assert result["plates_available"] == [1, 2]

    def test_list_plate_2_objects(self, multi_plate_3mf):
        """plate_number=2 should return plate 2's objects."""
        result = list_plate_objects(multi_plate_3mf, plate_number=2)
        assert result["object_count"] == 1
        assert result["objects"][0]["name"] == "bracket.stl"
        assert result["objects"][0]["label_id"] == 500
        assert result["bed_type"] == "cool_plate"
        assert result["plate_number"] == 2
        assert result["plates_available"] == [1, 2]

    def test_list_plate_1_still_works(self, multi_plate_3mf):
        """Default plate_number=1 should return plate 1's objects."""
        result = list_plate_objects(multi_plate_3mf)
        assert result["object_count"] == 2
        names = [o["name"] for o in result["objects"]]
        assert "PartA - body.stl" in names
        assert "PartA - lid.stl" in names

    def test_nonexistent_plate_raises(self, multi_plate_3mf):
        """Requesting a plate that doesn't exist should raise ValueError."""
        with pytest.raises(ValueError, match="plate 3"):
            list_plate_objects(multi_plate_3mf, plate_number=3)

    def test_extract_from_plate_2(self, multi_plate_3mf, tmp_path):
        """extract_plate_object_gcode with plate_number=2 reads plate 2."""
        result = extract_plate_object_gcode(
            multi_plate_3mf,
            "bracket",
            output_path=str(tmp_path / "bracket.gcode"),
            plate_number=2,
        )
        assert result["matched_object"]["name"] == "bracket.stl"
        assert result["matched_object"]["label_id"] == 500
        gcode = Path(result["output_path"]).read_text()
        # Plate 2 gcode should be present
        assert "G1 X120 Y50 E2.0 F1500" in gcode
        assert "M190 S55" in gcode
        # Plate 1 gcode should NOT be present
        assert "G1 X150 Y100 E1.5 F1500" not in gcode

    def test_extract_plate_2_object_not_on_plate_1(self, multi_plate_3mf):
        """bracket.stl is only on plate 2; extracting from plate 1 should fail."""
        with pytest.raises(ValueError, match="No object matching"):
            extract_plate_object_gcode(
                multi_plate_3mf,
                "bracket",
                plate_number=1,
            )
