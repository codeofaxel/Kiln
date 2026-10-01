"""A STEP file gets the printability check, and an estimate that could not
make it says so.

Until 2026-09-30 ``analyze_printability`` refused a STEP file ("Unsupported
file type: '.step'"), and ``slice_and_estimate`` -- which slices a STEP
happily -- ran the analysis only for the formats the engine read.  So a STEP
estimate came back with a time, a weight, and no printability score and no
brim decision, and the only trace of the skip was a debug log line.  A person
reading the result could not tell "nothing to say" from "nobody looked".

Now the engine takes the shared CAD door: a STEP is analysed as Kiln's mesh
of it (:func:`kiln.step_import.ensure_mesh_path`, cached by content), for
every caller.  Pinned here: the STEP's report equals the same part's as an
STL; a STEP nothing can convert raises rather than reading as a clean mesh;
the estimate door carries the report and the brim decision, analyses the
file that was actually sliced, and when the check could not be made says so
in the result and in the sentence people read.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from kiln import step_import
from kiln.printability import analyze_printability
from kiln.slicer import SliceResult

pytest.importorskip("OCP", reason="a real STEP file needs the CAD kernel to write and convert")
trimesh = pytest.importorskip("trimesh")

# A plate with a post: enough contact and height for a real report.
_SIZE = (60.0, 40.0, 12.0)


def _step(path: Path, at: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> str:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    writer = STEPControl_Writer()
    writer.Transfer(BRepPrimAPI_MakeBox(gp_Pnt(*at), *_SIZE).Shape(), STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(path))
    return str(path)


def _stl(path: Path) -> str:
    box = trimesh.creation.box(extents=_SIZE)
    box.apply_translation([v / 2.0 for v in _SIZE])
    box.export(str(path))
    return str(path)


class TestTheEngine:
    def test_a_step_gets_the_report_the_same_part_gets_as_an_stl(self, tmp_path: Path) -> None:
        cad = analyze_printability(_step(tmp_path / "plate.step", at=(500.0, -500.0, 40.0)), material="PLA")
        mesh = analyze_printability(_stl(tmp_path / "plate.stl"), material="PLA")
        assert (cad.score, cad.grade, cad.printable) == (mesh.score, mesh.grade, mesh.printable)
        assert cad.dimensions_mm == pytest.approx(mesh.dimensions_mm, abs=0.01)
        assert cad.model_height_mm == pytest.approx(_SIZE[2], abs=0.01)
        assert (cad.adhesion is None) == (mesh.adhesion is None)
        if mesh.adhesion is not None:
            assert cad.adhesion.to_dict() == mesh.adhesion.to_dict()

    def test_a_step_nothing_can_convert_raises_and_never_reads_as_a_mesh(self, tmp_path: Path) -> None:
        """In the engine's own error type, so every caller that handles an
        unreadable mesh handles this -- with the remedy still attached."""
        step = _step(tmp_path / "plate.step")
        missing = step_import.NoBackendError()
        with patch("kiln.step_import.ensure_mesh_path", side_effect=missing), pytest.raises(ValueError) as caught:
            analyze_printability(step, material="PLA")
        assert str(caught.value) == str(missing)
        assert caught.value.__cause__ is missing and missing.remedy

    def test_a_step_that_is_not_cad_at_all_is_an_unreadable_file(self, tmp_path: Path) -> None:
        garbage = tmp_path / "garbage.step"
        garbage.write_bytes(b"this is not a CAD file at all " * 40)
        with pytest.raises(ValueError, match="Could not read that CAD file"):
            analyze_printability(str(garbage), material="PLA")


def _estimate_tool():
    from kiln.plugins.estimate_tools import _EstimateToolsPlugin

    tools: dict = {}

    class _FakeMcp:
        def tool(self, name=None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _EstimateToolsPlugin().register(_FakeMcp())
    return tools["slice_and_estimate"]


def _sliced(effective_input: str, bed_fit: dict | None = None):
    """What the shared slice step hands the estimate door, without a slicer."""
    info = {"placement": {"plate": "unknown"}, "bed_fit": bed_fit, "effective_input": effective_input}
    return SliceResult(success=True, output_path=None, slicer="PrusaSlicer", message="sliced"), None, info


class TestTheEstimateDoor:
    """``slice_and_estimate`` as registered, the slicer stood in for: what is
    under test is what the door does with the file it sliced."""

    def _estimate(self, path: str, sliced) -> dict:
        with patch("kiln.server._check_auth", return_value=None), patch(
            "kiln.plugins.estimate_tools._estimate_slice", return_value=sliced
        ):
            return _estimate_tool()(input_path=path, printer_id="bambu_a1", material="PLA")

    def test_a_step_estimate_carries_printability_and_the_brim_decision(self, tmp_path: Path) -> None:
        step = _step(tmp_path / "plate.step", at=(500.0, -500.0, 40.0))
        stl = _stl(tmp_path / "plate.stl")
        cad = self._estimate(step, _sliced(step))
        mesh = self._estimate(stl, _sliced(stl))
        assert cad["success"] and mesh["printability"] is not None
        assert cad["printability"] is not None, cad
        assert cad["printability"]["score"] == mesh["printability"]["score"]
        assert cad["adhesion"] == mesh["adhesion"]
        assert "Printability:" in cad["message"]
        # What the advice rests on, in one sentence -- and only for a CAD file.
        assert "Kiln's mesh of this STEP file" in cad["printability_note"]
        assert "printability_note" not in mesh

    def test_a_step_that_cannot_be_converted_says_the_check_was_not_made(self, tmp_path: Path) -> None:
        step = _step(tmp_path / "plate.step")
        with patch("kiln.step_import.ensure_mesh_path", side_effect=step_import.NoBackendError()):
            result = self._estimate(step, _sliced(step))
        assert result["success"]
        assert result["printability"] is None and result["adhesion"] is None
        assert "not checked" in result["printability_note"]
        assert "not checked" in result["message"]

    def test_a_format_the_engine_cannot_read_says_so_too(self, tmp_path: Path) -> None:
        amf = tmp_path / "part.amf"
        amf.write_text("<amf/>")
        result = self._estimate(str(amf), _sliced(str(amf)))
        assert result["printability"] is None
        assert "not checked" in result["printability_note"] and ".amf" in result["printability_note"]

    def test_the_analysis_is_of_the_file_that_was_sliced(self, tmp_path: Path) -> None:
        """The bed-fit gate may turn a part to fit; the brim decision has to
        be for the face that is actually on the bed."""
        stl = _stl(tmp_path / "plate.stl")
        stood = trimesh.load(stl)
        stood.apply_transform(trimesh.transformations.rotation_matrix(1.5707963267948966, [1.0, 0.0, 0.0]))
        stood.apply_translation(-stood.bounds[0])
        turned = str(tmp_path / "plate_oriented.stl")
        stood.export(turned)
        fit = {"ok": True, "auto_oriented": True, "approval_carries": False}
        result = self._estimate(stl, _sliced(turned, bed_fit=fit))
        assert result["printability"]["model_height_mm"] == pytest.approx(_SIZE[1], abs=0.01)
        assert result["bed_fit"]["auto_oriented"] is True


def _real_prusaslicer() -> str | None:
    found = shutil.which("prusa-slicer") or shutil.which("PrusaSlicer")
    if found:
        return found
    mac = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


@pytest.mark.skipif(_real_prusaslicer() is None, reason="needs a real PrusaSlicer")
def test_the_real_door_estimates_a_step_with_its_printability(tmp_path: Path, monkeypatch) -> None:
    from kiln import monitor_twin

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    step = _step(tmp_path / "plate.step", at=(500.0, -500.0, 40.0))
    with patch("kiln.server._check_auth", return_value=None):
        result = _estimate_tool()(input_path=step, printer_id="bambu_a1", material="PLA")
    assert result.get("success"), result
    assert result["estimate"]["estimated_time_seconds"]
    assert result["printability"]["score"] is not None
    assert result["printability"]["model_height_mm"] == pytest.approx(_SIZE[2], abs=0.01)
    assert "Kiln's mesh of this STEP file" in result["printability_note"]
