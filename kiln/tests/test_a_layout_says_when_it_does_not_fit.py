"""A door that lays out or composes a plate never claims a fit it does not have.

Measured 2026-10-17 with nine 110 x 105 mm parts for a 256 mm bed, before
this was written:

* ``auto_arrange_parts_on_plate`` kept adding rows past the plate (573 mm
  deep) and replied "Arranged 9 parts ... on a 256x256mm plate.  Pass
  'parts' directly to compose_multicolor_3mf()".
* ``compose_multicolor_3mf`` re-centred that 232 x 573 mm group and said
  "Next step: upload_file(...) then start_print()" -- advice no printer can
  follow even for a plate that fits, since the file is an unsliced model.
* ``multi_copy_print`` and the CLI's ``--copies`` hand PrusaSlicer
  ``--duplicate``, which sliced the four copies that fit, left the other
  five out without a word and exited 0.

Every one of these is now judged by :func:`kiln.printers.bed_fit.plate_fit`,
which is :func:`kiln.printers.bed_fit.check_bed_fit` -- the judgment the
slice and start gates make -- with the sizes and the way out in words.  The
free path keeps all of it: a fit is a fact about the bed, not a paid
feature.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

trimesh = pytest.importorskip("trimesh")

#: A synthetic printer from the test catalogue with a 256 x 256 x 256 mm bed.
BED_256 = "kilntest_enclosed"
REMEDY = "Use fewer parts or copies, split them across plates, or print on a printer with a bigger bed."


def _part(tmp_path: Path, name: str, size=(110.0, 105.0, 10.0)) -> str:
    mesh = trimesh.creation.box(extents=list(size))
    mesh.apply_translation([size[0] / 2, size[1] / 2, size[2] / 2])
    path = tmp_path / f"{name}.stl"
    mesh.export(str(path))
    return str(path)


def _specs(tmp_path: Path, count: int) -> list[dict]:
    return [
        {"stl_path": _part(tmp_path, f"part{i + 1}"), "extruder": i + 1, "name": f"part{i + 1}", "group": i}
        for i in range(count)
    ]


def _auto_arrange():
    from kiln.mcp_compat import FastMCP
    from kiln.plugins.design_reasoning_tools import plugin

    mcp = FastMCP("layout")
    plugin.register(mcp)
    return mcp._tool_manager._tools["auto_arrange_parts_on_plate"].fn  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# auto_arrange_parts_on_plate
# ---------------------------------------------------------------------------


class TestAutoArrange:
    def test_rows_past_the_bed_are_said_with_their_real_size(self, tmp_path):
        result = _auto_arrange()(part_specs=_specs(tmp_path, 9), printer_id=BED_256, gap_mm=12.0)
        assert result["fits_bed"] is False
        assert result["bed_fit"]["extent_mm"][:2] == [232.0, 573.0]
        assert "232 × 573 mm" in result["message"]
        assert "256 × 256 mm" in result["message"]
        assert REMEDY in result["message"]
        assert "compose_multicolor_3mf" not in result["message"]

    def test_parts_that_fit_keep_the_next_step(self, tmp_path):
        result = _auto_arrange()(part_specs=_specs(tmp_path, 4), printer_id=BED_256, gap_mm=12.0)
        assert result["fits_bed"] is True
        assert "Pass 'parts' directly to compose_multicolor_3mf()" in result["message"]


# ---------------------------------------------------------------------------
# compose_multicolor_3mf
# ---------------------------------------------------------------------------


def _composed(tmp_path: Path, count: int, printer_id: str | None = BED_256) -> dict:
    from kiln.multicolor_3mf import ColorPart, auto_arrange_parts, compose_multicolor_3mf

    laid_out = auto_arrange_parts(_specs(tmp_path, count), gap_mm=12.0, printer_id=printer_id)
    return compose_multicolor_3mf(
        [ColorPart(p.stl_path, extruder=p.extruder, name=p.name, x=p.x, y=p.y) for p in laid_out],
        output_path=str(tmp_path / f"plate{count}.3mf"),
        printer_id=printer_id,
    )


class TestCompose:
    def test_a_plate_bigger_than_its_bed_is_never_offered_for_printing(self, tmp_path):
        result = _composed(tmp_path, 9)
        assert result["success"] is True  # the file is made; it is the claim that changed
        assert result["fits_bed"] is False
        assert "232 × 573 mm" in result["message"] and REMEDY in result["message"]
        for offer in ("upload_file", "start_print", "slice_and_print", "Next step"):
            assert offer not in result["message"], offer

    def test_a_plate_that_fits_is_offered_for_slicing_not_for_upload(self, tmp_path):
        """A composed 3MF is a model, not a sliced plate: the upload door
        refuses it ("not a sliced plate") and no printer starts it."""
        result = _composed(tmp_path, 4)
        assert result["fits_bed"] is True
        assert "slice_and_print(" in result["message"]
        assert "upload_file" not in result["message"]
        assert "start_print" not in result["message"]

    def test_with_no_printer_named_it_is_judged_against_the_plate_it_was_given(self, tmp_path):
        result = _composed(tmp_path, 9, printer_id=None)
        assert result["fits_bed"] is False
        assert "the plate it was laid out for is 256 × 256 mm" in result["message"]


# ---------------------------------------------------------------------------
# The doors that compose and then print
# ---------------------------------------------------------------------------


class TestThePrintingDoorsStopAtTheLayout:
    @pytest.fixture(autouse=True)
    def _preview_gate_bypassed(self, monkeypatch):
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")

    def test_multi_material_print_refuses_before_slicing(self, tmp_path):
        from kiln import server

        objects = [{"file_path": _part(tmp_path, f"obj{i}"), "material_id": "pla"} for i in range(9)]
        with patch("kiln.server._pipeline_reslice_and_print") as pipeline:
            result = server.multi_material_print(objects_json=objects, printer_id=BED_256)
        assert pipeline.call_count == 0
        assert result["success"] is False
        assert result["error"]["code"] == "PLATE_EXCEEDS_BED"
        assert REMEDY in result["error"]["message"]
        assert os.path.isfile(result["multi_material_3mf"])

    def test_multi_color_copies_refuses_before_slicing(self, tmp_path):
        from kiln import server

        trays = {
            "success": True,
            "units": [{"unit_id": 0, "trays": [
                {"slot": s, "tray_id": s, "name": f"A{s + 1}", "tray_type": "PLA", "tray_color": colour}
                for s, colour in enumerate(["E53935FF", "1E88E5FF", "F2F2F2FF", "1A1A1AFF"])
            ]}],
        }
        model = _part(tmp_path, "tile", size=(130.0, 130.0, 5.0))
        with (
            patch("kiln.server.ams_status", return_value=trays),
            patch("kiln.server._pipeline_reslice_and_print") as pipeline,
        ):
            result = server.multi_color_copies(model_path=model, printer_id=BED_256)
        assert pipeline.call_count == 0
        assert result["success"] is False
        assert result["error"]["code"] == "PLATE_EXCEEDS_BED"
        assert REMEDY in result["error"]["message"]


# ---------------------------------------------------------------------------
# Copies the slicer lays out itself
# ---------------------------------------------------------------------------


def _real_prusaslicer() -> str | None:
    found = shutil.which("prusa-slicer") or shutil.which("PrusaSlicer")
    if found:
        return found
    mac = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


@pytest.mark.skipif(_real_prusaslicer() is None, reason="needs a real PrusaSlicer")
class TestCopiesTheSlicerLaysOut:
    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch, tmp_path):
        from kiln import monitor_twin

        monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
        twin = tmp_path / "twin"
        monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
        monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
        monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")

    def _profile(self, tmp_path: Path) -> str:
        path = tmp_path / "bed256.ini"
        path.write_text(
            "bed_shape = 0x0,256x0,256x256,0x256\nmax_print_height = 256\nnozzle_diameter = 0.4\n"
            "filament_diameter = 1.75\nlayer_height = 0.2\nfirst_layer_height = 0.2\n",
            encoding="utf-8",
        )
        return str(path)

    def _slice(self, tmp_path: Path, copies: int):
        from kiln.slicer import slice_file

        return slice_file(
            _part(tmp_path, "box"),
            profile=self._profile(tmp_path),
            slicer_path=_real_prusaslicer(),
            output_dir=str(tmp_path / "out"),
            extra_args=["--duplicate", str(copies), "--duplicate-distance", "12"],
        )

    def test_nine_copies_on_a_bed_that_holds_four_are_refused(self, tmp_path):
        from kiln.slicer import SlicerError

        with pytest.raises(SlicerError) as refused:
            self._slice(tmp_path, 9)
        assert "9 copies of box.stl do not fit on this 256 × 256 mm bed" in str(refused.value)
        assert REMEDY in str(refused.value)

    def test_four_copies_that_fit_are_all_sliced(self, tmp_path):
        """Measured on the G-code: four copies of a 110 x 105 mm part, two by
        two, so the extrusion spans more than two parts in each direction."""
        from kiln.printers.bed_fit import compute_gcode_bbox

        result = self._slice(tmp_path, 4)
        box = compute_gcode_bbox(result.output_path)
        assert box["x_max"] - box["x_min"] > 2 * 110.0
        assert box["y_max"] - box["y_min"] > 2 * 105.0
        text = Path(result.output_path).read_text(encoding="utf-8", errors="replace")
        used = float(re.search(r"; filament used \[mm\] = ([\d.]+)", text).group(1))
        single = self._single_copy_filament(tmp_path)
        assert used == pytest.approx(4 * single, rel=0.05)

    def _single_copy_filament(self, tmp_path: Path) -> float:
        from kiln.slicer import slice_file

        one = tmp_path / "one"
        one.mkdir()
        result = slice_file(
            _part(one, "box"), profile=self._profile(one), slicer_path=_real_prusaslicer(),
            output_dir=str(one / "out"),
        )
        text = Path(result.output_path).read_text(encoding="utf-8", errors="replace")
        return float(re.search(r"; filament used \[mm\] = ([\d.]+)", text).group(1))

