"""Kiln measures a STEP file before it slices one.

From 2026-04-15 to 2026-09-30 the bed-fit reader handed a STEP file to a
mesh library that needs an add-on Kiln does not install, got nothing back,
and passed the file unmeasured -- by design, since a fit check must never
block on a file it cannot read.  So a STEP too big for the bed reached the
slicer, which refused it with "All objects are outside of the print volume"
where an STL got Kiln's own answer: how big, how big the bed is, and what to
do.  The fleet survey and plate planning could not size a STEP job either.
Nothing errored and no test sent a STEP file through the check.

Now the reader measures a STEP with Kiln's own CAD reader.  Pinned here:
the size is exact; the position is left to the slicer (both slicers lay a
STEP onto the bed), so a part modelled far from the origin is never
refused; a part that fits only lying on another face is left to the slicer
too, since Kiln cannot turn a STEP file; and a machine that cannot read STEP
passes it unmeasured, as before.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from kiln.printers import bed_fit

pytest.importorskip("OCP", reason="a real STEP file needs the CAD kernel to write and measure")


def _step(path: Path, x: float, y: float, z: float, at: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> str:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    writer = STEPControl_Writer()
    writer.Transfer(BRepPrimAPI_MakeBox(gp_Pnt(*at), x, y, z).Shape(), STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(path))
    return str(path)


class TestTheReader:
    def test_a_step_is_measured_exactly_and_rests_at_the_origin(self, tmp_path: Path) -> None:
        step = _step(tmp_path / "bracket.step", 30.0, 20.0, 10.0, at=(500.0, 500.0, 40.0))
        assert bed_fit.compute_mesh_bbox(step) == pytest.approx(
            {"x_min": 0.0, "x_max": 30.0, "y_min": 0.0, "y_max": 20.0, "z_min": 0.0, "z_max": 10.0}, abs=1e-6,
        )

    def test_a_machine_that_cannot_read_step_passes_it_unmeasured(self, tmp_path: Path) -> None:
        step = _step(tmp_path / "bracket.step", 300.0, 300.0, 10.0)
        unavailable = bed_fit.step_import._exact_unavailable("no CAD kernel on this machine")
        with patch("kiln.step_import.read_exact_geometry", return_value=unavailable):
            fit = bed_fit.validate_mesh_for_printer(step, "bambu_a1")
        assert fit["ok"] and fit["error_code"] == "BBOX_UNKNOWN"


class TestTheFitCheck:
    def test_a_step_too_big_for_the_bed_is_refused_in_kilns_words(self, tmp_path: Path) -> None:
        fit = bed_fit.validate_mesh_for_printer(_step(tmp_path / "big.step", 300.0, 300.0, 10.0), "bambu_a1")
        assert not fit["ok"] and fit["error_code"] == "EXCEEDS_BED"
        assert "300.0×300.0×10.0mm" in fit["error_message"] and "256×256×256mm" in fit["error_message"]

    def test_a_step_modelled_far_from_the_origin_is_never_refused(self, tmp_path: Path) -> None:
        """The slicer centres it; refusing it would block a slice that works."""
        step = _step(tmp_path / "far.step", 30.0, 20.0, 10.0, at=(500.0, -500.0, 0.0))
        fit = bed_fit.validate_mesh_for_printer(step, "bambu_a1")
        assert fit["ok"] and fit["error_code"] is None

    def test_a_step_that_fits_only_lying_on_another_face_is_left_to_the_slicer(self, tmp_path: Path) -> None:
        # MK4: 250 x 210 x 220.  245 deep as modelled is too deep; on its side it fits.
        step = _step(tmp_path / "tall.step", 200.0, 245.0, 215.0)
        fit = bed_fit.validate_mesh_for_printer(step, "prusa_mk4")
        assert fit["ok"] and fit["error_code"] is None
        assert "another face" in fit["note"]

    def test_the_same_part_as_an_stl_is_still_refused_here(self, tmp_path: Path) -> None:
        """An STL is turned to fit by the slice gate itself, so the check
        keeps refusing it as modelled -- the leniency is STEP's alone."""
        trimesh = pytest.importorskip("trimesh")
        stl = tmp_path / "tall.stl"
        box = trimesh.creation.box(extents=(200.0, 245.0, 215.0))
        box.apply_translation([100.0, 122.5, 107.5])
        box.export(str(stl))
        fit = bed_fit.validate_mesh_for_printer(str(stl), "prusa_mk4")
        assert not fit["ok"] and fit["error_code"] == "EXCEEDS_BED"

    def test_no_face_fits(self, tmp_path: Path) -> None:
        fit = bed_fit.validate_mesh_for_printer(_step(tmp_path / "huge.step", 260.0, 100.0, 100.0), "prusa_mk4")
        assert not fit["ok"] and fit["error_code"] == "EXCEEDS_BED"


def test_the_fleet_survey_sizes_a_step_job(tmp_path: Path) -> None:
    from kiln._pro_placement_bridge import job_envelope

    envelope = job_envelope(_step(tmp_path / "bracket.step", 30.0, 20.0, 10.0, at=(500.0, 500.0, 0.0)))
    assert envelope["part"]["size_mm"] == pytest.approx([30.0, 20.0, 10.0])


def _real_prusaslicer() -> str | None:
    found = shutil.which("prusa-slicer") or shutil.which("PrusaSlicer")
    if found:
        return found
    mac = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


@pytest.mark.skipif(_real_prusaslicer() is None, reason="needs a real PrusaSlicer")
class TestTheSliceModelTool:
    """The door a person meets, with the real slicer behind it."""

    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch, tmp_path):
        from kiln import monitor_twin

        monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
        twin = tmp_path / "twin"
        monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
        monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
        monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")

    def _slice_model(self, path: str, out: Path) -> dict:
        from kiln.plugins.slicer_tools import _SlicerToolsPlugin

        tools: dict = {}

        class _FakeMcp:
            def tool(self, name=None, **_kwargs):
                def decorator(fn):
                    tools[name or fn.__name__] = fn
                    return fn

                return decorator

        _SlicerToolsPlugin().register(_FakeMcp())
        with patch("kiln.server._check_auth", return_value=None):
            return tools["slice_model"](
                input_path=path, output_dir=str(out), printer_id="bambu_a1", slicer_path=_real_prusaslicer(),
            )

    def test_a_step_too_big_gets_kilns_answer_not_the_slicers(self, tmp_path: Path) -> None:
        result = self._slice_model(_step(tmp_path / "big.step", 300.0, 300.0, 10.0), tmp_path / "out")
        assert result.get("success") is False
        assert "EXCEEDS_BED" in str(result), result
        assert "outside of the print volume" not in str(result)

    def test_a_step_far_from_the_origin_still_slices(self, tmp_path: Path) -> None:
        step = _step(tmp_path / "far.step", 30.0, 20.0, 10.0, at=(500.0, 500.0, 0.0))
        result = self._slice_model(step, tmp_path / "out")
        assert result.get("success"), result
