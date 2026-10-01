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
refused; and a machine that cannot read STEP passes it unmeasured, as
before.

A part that fits only lying on another face is not refused by the check
either, and the slice gate turns it: Kiln cannot turn a STEP file, so it
turns its own mesh of it with the machinery that turns an STL, slices that,
and says so.  That machinery tried only the two side-face turns and never
put a turned part back on the bed, so until 2026-09-30 a part that needed
turning where it stood -- the MK4 case below -- was refused as an STL as
well, and a STEP of it reached PrusaSlicer to be refused there.
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


def _light_step(path: Path, x: float, y: float, z: float, at: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> str:
    """A part with the envelope ``x`` x ``y`` x ``z`` and very little in it --
    a 2 mm floor with a 5 mm post -- so a real slicer is done in seconds."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    floor = BRepPrimAPI_MakeBox(gp_Pnt(*at), x, y, 2.0).Shape()
    post = BRepPrimAPI_MakeBox(gp_Pnt(*at), 5.0, 5.0, z).Shape()
    writer = STEPControl_Writer()
    writer.Transfer(BRepAlgoAPI_Fuse(floor, post).Shape(), STEPControl_StepModelType.STEPControl_AsIs)
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

    def test_a_step_that_fits_only_lying_on_another_face_is_not_refused_by_the_check(self, tmp_path: Path) -> None:
        """The check measures and says so; the slice gate does the turning."""
        # MK4: 250 x 210 x 220.  245 deep as modelled is too deep; turned a quarter turn it fits.
        step = _step(tmp_path / "tall.step", 200.0, 245.0, 215.0)
        fit = bed_fit.validate_mesh_for_printer(step, "prusa_mk4")
        assert fit["ok"] and fit["error_code"] is None
        assert fit["fits_on_another_face"] is True
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


def _spans(bbox: dict) -> list[float]:
    return [bbox[f"{a}_max"] - bbox[f"{a}_min"] for a in "xyz"]


def _on_the_mk4_bed(bbox: dict) -> bool:
    return (
        bbox["x_min"] >= -0.5 and bbox["x_max"] <= 250.5
        and bbox["y_min"] >= -0.5 and bbox["y_max"] <= 210.5
        and bbox["z_min"] >= -0.5 and bbox["z_max"] <= 220.5
    )


class TestTheSliceGate:
    """The gate every slice door shares, measured on the file it hands the slicer."""

    def _gate(self, path: str, auto_center: bool = True):
        from kiln.plugins.slicer_tools import _apply_bed_fit_gate

        return _apply_bed_fit_gate(path, "prusa_mk4", auto_center)

    def test_a_step_that_fits_only_on_another_face_is_turned_as_kilns_mesh(self, tmp_path: Path) -> None:
        # Modelled far from the origin on purpose: the mesh Kiln makes of a
        # STEP keeps the CAD position, and the turn must be judged by size.
        step = _step(tmp_path / "tall.step", 200.0, 245.0, 215.0, at=(500.0, -500.0, 40.0))
        sliced, refusal, fit = self._gate(step)
        assert refusal is None
        assert sliced.endswith(".stl") and sliced != step
        measured = bed_fit.compute_mesh_bbox(sliced)
        assert _spans(measured) == pytest.approx([245.0, 200.0, 215.0], abs=0.01)
        assert _on_the_mk4_bed(measured)
        assert fit["auto_oriented"] and fit["turned_deg"] == [0.0, 0.0, 90.0]
        assert fit["approval_carries"] is False and "rotated to fit" in fit["approval_note"]
        assert fit["sliced_mesh"] == "kiln_step_mesh"
        assert "Kiln's own mesh" in fit["note"] and "not the slicer's reading" in fit["note"]

    def test_a_step_is_left_as_modelled_when_the_caller_forbids_moving_it(self, tmp_path: Path) -> None:
        step = _step(tmp_path / "tall.step", 200.0, 245.0, 215.0)
        sliced, refusal, fit = self._gate(step, auto_center=False)
        assert refusal is None and sliced == step
        assert "another face" in fit["note"] and not fit.get("auto_oriented")

    def test_a_step_this_machine_cannot_convert_is_left_as_modelled(self, tmp_path: Path) -> None:
        step = _step(tmp_path / "tall.step", 200.0, 245.0, 215.0)
        with patch(
            "kiln.step_import.ensure_mesh_path",
            side_effect=bed_fit.step_import.NoBackendError(),
        ):
            sliced, refusal, fit = self._gate(step)
        assert refusal is None and sliced == step
        assert "another face" in fit["note"] and not fit.get("auto_oriented")

    def test_a_step_no_face_of_which_fits_is_still_refused(self, tmp_path: Path) -> None:
        _sliced, refusal, _fit = self._gate(_step(tmp_path / "huge.step", 260.0, 100.0, 100.0))
        assert refusal is not None and refusal["error_code"] == "EXCEEDS_BED"

    @pytest.mark.parametrize(
        ("extents", "turn", "turned"),
        [
            # Turned where it stands: the face the designer put down stays down.
            ((200.0, 245.0, 215.0), [0.0, 0.0, 90.0], [245.0, 200.0, 215.0]),
            # Laid on a side face -- a turn that swings the part to negative Y.
            ((240.0, 215.0, 100.0), [90.0, 0.0, 0.0], [240.0, 100.0, 215.0]),
            ((100.0, 100.0, 240.0), [0.0, 90.0, 0.0], [240.0, 100.0, 100.0]),
            # Laid on a side face and turned.
            ((205.0, 215.0, 245.0), [90.0, 0.0, 90.0], [245.0, 205.0, 215.0]),
        ],
    )
    def test_an_stl_is_turned_whichever_quarter_turn_it_needs(self, tmp_path: Path, extents, turn, turned) -> None:
        trimesh = pytest.importorskip("trimesh")
        stl = tmp_path / "part.stl"
        box = trimesh.creation.box(extents=extents)
        box.apply_translation([e / 2.0 for e in extents])
        box.export(str(stl))
        sliced, refusal, fit = self._gate(str(stl))
        assert refusal is None, refusal
        measured = bed_fit.compute_mesh_bbox(sliced)
        assert _spans(measured) == pytest.approx(turned, abs=0.01)
        assert _on_the_mk4_bed(measured)
        assert fit["turned_deg"] == turn and fit["approval_carries"] is False
        assert "sliced_mesh" not in fit  # an STL turned is still the person's own mesh


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

    def _slice_model(self, path: str, out: Path, printer_id: str = "bambu_a1") -> dict:
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
                input_path=path, output_dir=str(out), printer_id=printer_id, slicer_path=_real_prusaslicer(),
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

    def test_a_step_that_fits_only_on_another_face_slices_turned(self, tmp_path: Path) -> None:
        """MK4 bed 250 x 210 x 220; the part is 245 deep as modelled.  The
        slicer used to get it as modelled and refuse it in its own words."""
        step = _light_step(tmp_path / "tall.step", 200.0, 245.0, 215.0, at=(500.0, -500.0, 40.0))
        result = self._slice_model(step, tmp_path / "out", printer_id="prusa_mk4")
        assert result.get("success"), result
        fit = result["bed_fit"]
        assert fit["auto_oriented"] and fit["approval_carries"] is False
        assert "Kiln's own mesh" in fit["note"]
        # The stage draws the turned mesh: the plate as it will print.
        assert result["stage_mesh_path"] == fit["oriented_input_path"]
        assert os.path.isfile(result["stage_mesh_path"])
        # Measured on the sliced file's own toolpaths, the part's and not its
        # skirt's: the 245 side now lies along the 250 bed, the 200 side
        # along the 210, and the part is on the plate.
        from kiln.slicer_geometry import parse_slicer_features

        sliced = parse_slicer_features(result["output_path"])
        assert sliced.labelled and sliced.model_footprint is not None
        x0, y0, x1, y1 = sliced.model_footprint
        assert x1 - x0 == pytest.approx(245.0, abs=1.5)
        assert y1 - y0 == pytest.approx(200.0, abs=1.5)
        assert x0 >= 0.0 and x1 <= 250.0 and y0 >= 0.0 and y1 <= 210.0
        assert "outside of the print volume" not in str(result)
