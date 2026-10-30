"""The objects a sliced file prints, read as the file names them.

Four slicer dialects write object markers four ways.  Before this reader,
Kiln's motion safety read three of them for footprints (and missed Bambu
Studio's current "unique label id" line), and the mid-print planner in
kiln-pro read only Bambu's for layer spans, so a Voron's or a Prusa's print
had no objects at all.  One reader now answers both, and the identity it
gives each object is the one that printer's own skip-object command takes.
"""

from __future__ import annotations

import io

import pytest

from kiln import gcode_objects

BAMBU = """\
; model label id: 757,758
; layer num/total_layer_count: 1/3
; start printing object, unique label id: 757
G1 X30 Y30 E1.0
; stop printing object, unique label id: 757
; layer num/total_layer_count: 2/3
; start printing object, unique label id: 757
G1 X31 Y31 E1.0
; stop printing object, unique label id: 757
; start printing object, unique label id: 758
G1 X90 Y90 E1.0
; stop printing object, unique label id: 758
; layer num/total_layer_count: 3/3
; start printing object, unique label id: 758
G1 X91 Y91 E1.0
; stop printing object, unique label id: 758
"""

KLIPPER = """\
EXCLUDE_OBJECT_DEFINE NAME=bracket CENTER=110,110 POLYGON=[[95,95],[125,95],[125,125],[95,125]]
EXCLUDE_OBJECT_DEFINE NAME=cap CENTER=40,40 POLYGON=[[30,30],[50,30],[50,50],[30,50]]
;LAYER_CHANGE
EXCLUDE_OBJECT_START NAME=bracket
G1 X100 Y100 E1
EXCLUDE_OBJECT_END NAME=bracket
;LAYER_CHANGE
EXCLUDE_OBJECT_START NAME=bracket
G1 X101 Y101 E1
EXCLUDE_OBJECT_END NAME=bracket
EXCLUDE_OBJECT_START NAME=cap
G1 X40 Y40 E1
EXCLUDE_OBJECT_END NAME=cap
"""

PRUSA = """\
;LAYER_CHANGE
; printing object cube.stl id:0 copy 0
G1 X20 Y20 E1
; stop printing object cube.stl id:0 copy 0
; printing object cube.stl id:0 copy 1
G1 X150 Y150 E1
; stop printing object cube.stl id:0 copy 1
"""

MARLIN = """\
;LAYER:0
M486 S0
G1 X10 Y10 E1
M486 S-1
;LAYER:1
M486 S1
G1 X80 Y80 E1
M486 S-1
"""


def _scan(text: str) -> gcode_objects.ObjectsScan:
    return gcode_objects.read_object_blocks(io.StringIO(text))


class TestEveryDialectIsRead:
    def test_bambu_studios_label_ids_and_their_layers(self):
        found = _scan(BAMBU).objects
        assert set(found) == {"757", "758"}
        assert (found["757"].first_layer, found["757"].last_layer) == (1, 2)
        assert (found["758"].first_layer, found["758"].last_layer) == (2, 3)
        assert found["757"].dialect == "bambu" and found["757"].name == "object 757"

    def test_an_older_bambu_line_without_the_word_unique(self):
        found = _scan("; start printing object, id: 190\nG1 X1 Y1 E1\n; stop printing object\n").objects
        assert set(found) == {"190"}

    def test_klippers_object_names(self):
        found = _scan(KLIPPER).objects
        assert set(found) == {"bracket", "cap"}, "the DEFINE lines name footprints, not blocks"
        assert (found["bracket"].first_layer, found["bracket"].last_layer) == (1, 2)
        assert (found["cap"].first_layer, found["cap"].last_layer) == (2, 2)
        assert found["cap"].dialect == "klipper"

    def test_prusaslicers_copies_are_distinct_objects(self):
        found = _scan(PRUSA).objects
        assert set(found) == {"cube.stl id:0 copy 0", "cube.stl id:0 copy 1"}

    def test_marlins_cancel_object_indices(self):
        found = _scan(MARLIN).objects
        assert set(found) == {"0", "1"}
        assert found["1"].first_layer == 1 and found["1"].dialect == "marlin"

    def test_a_block_open_across_a_layer_change_runs_on(self):
        found = _scan(";LAYER:0\nEXCLUDE_OBJECT_START NAME=a\n;LAYER:1\n;LAYER:2\nEXCLUDE_OBJECT_END\n").objects
        assert found["a"].segments == [[1, 2]]

    def test_a_scan_that_stopped_early_says_so(self):
        scan = gcode_objects.read_object_blocks(io.StringIO(KLIPPER), max_lines=3)
        assert scan.truncated is True


class TestFromAFile:
    def test_a_gcode_file_and_a_sliced_3mf_read_the_same(self, tmp_path):
        import zipfile

        plain = tmp_path / "plate.gcode"
        plain.write_text(KLIPPER)
        sliced = tmp_path / "plate.gcode.3mf"
        with zipfile.ZipFile(sliced, "w") as zf:
            zf.writestr("Metadata/plate_1.gcode", BAMBU)
        assert set(gcode_objects.read_object_blocks_from_path(str(plain)).objects) == {"bracket", "cap"}
        assert set(gcode_objects.read_object_blocks_from_path(str(sliced)).objects) == {"757", "758"}

    @pytest.mark.parametrize("name", ["nope.gcode", "model.stl"])
    def test_anything_else_is_none(self, tmp_path, name):
        path = tmp_path / name
        if name.endswith(".stl"):
            path.write_bytes(b"solid x\nendsolid x\n")
        assert gcode_objects.read_object_blocks_from_path(str(path)) is None or name.endswith(".stl")


class TestMotionSafetyReadsThroughTheSameReader:
    def test_bambu_studios_current_marker_now_gives_a_footprint(self, tmp_path):
        from kiln.printers.safe_motion import occupied_regions_for_job

        gcode = tmp_path / "plate.gcode"
        gcode.write_text(BAMBU)
        regions = occupied_regions_for_job(str(gcode))
        assert regions is not None
        assert {r.name for r in regions} == {"757", "758"}
