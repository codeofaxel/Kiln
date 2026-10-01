"""The STEP slicing doors, tested on a machine with no CAD kernel and no slicer.

``test_step_fit_check.py`` and ``test_step_printability.py`` write real STEP
files with the CAD kernel and slice them with a real PrusaSlicer, and skip
wherever either is missing -- which is every CI runner.  A change that broke
the wiring would have gone green there and red only on a developer's machine.

So the same wiring is pinned here with the kernel's two answers stood in for:
how big the part is (:func:`kiln.step_import.read_exact_geometry`) and Kiln's
mesh of it (:func:`kiln.step_import.ensure_mesh_path`).  Everything between
those two answers and the result is the real code: the bed-fit gate and its
turn, the printability engine's CAD door, the estimate door.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiln import step_import
from kiln.printers import bed_fit
from kiln.slicer import SliceResult

trimesh = pytest.importorskip("trimesh")

# MK4 bed: 250 x 210 x 220.  245 deep as modelled is too deep; turned a
# quarter turn where it stands, it fits.
_TALL = (200.0, 245.0, 215.0)
_FAR = (500.0, -500.0, 40.0)


def _step_named(tmp_path: Path, name: str = "part.step") -> str:
    """A file with a STEP name and no kernel behind it."""
    path = tmp_path / name
    path.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")
    return str(path)


def _kernel(size: tuple[float, float, float], at: tuple[float, float, float] = (0.0, 0.0, 0.0)):
    """Stand in for the CAD kernel: the part's exact size, and Kiln's mesh of
    it -- a box of that size, written where the CAD file put it."""

    def fake_mesh(path: str, *, output_dir: str | None = None, with_record: bool = False):
        if not step_import.is_step_file(path):
            return (path, None, None) if with_record else (path, None)
        out = Path(output_dir or Path(path).parent) / (Path(path).stem + ".stl")
        out.parent.mkdir(parents=True, exist_ok=True)
        box = trimesh.creation.box(extents=size)
        box.apply_translation([a + s / 2.0 for a, s in zip(at, size, strict=True)])
        box.export(str(out))
        note = f"Converted from STEP ({Path(path).name}) to mesh."
        return (str(out), note, None) if with_record else (str(out), note)

    exact = step_import.ExactGeometry(available=True, size_mm=size)
    return (
        patch("kiln.step_import.read_exact_geometry", return_value=exact),
        patch("kiln.step_import.ensure_mesh_path", side_effect=fake_mesh),
    )


def _spans(bbox: dict) -> list[float]:
    return [bbox[f"{a}_max"] - bbox[f"{a}_min"] for a in "xyz"]


class TestTheSliceGate:
    def _gate(self, path: str, auto_center: bool = True):
        from kiln.plugins.slicer_tools import _apply_bed_fit_gate

        return _apply_bed_fit_gate(path, "prusa_mk4", auto_center)

    def test_a_step_that_fits_only_on_another_face_is_turned_as_kilns_mesh(self, tmp_path: Path) -> None:
        step = _step_named(tmp_path)
        size, mesh = _kernel(_TALL, at=_FAR)
        with size, mesh:
            sliced, refusal, fit = self._gate(step)
        assert refusal is None
        assert sliced.endswith(".stl") and sliced != step
        measured = bed_fit.compute_mesh_bbox(sliced)
        assert _spans(measured) == pytest.approx([245.0, 200.0, 215.0], abs=0.01)
        assert measured["x_min"] >= -0.5 and measured["x_max"] <= 250.5
        assert measured["y_min"] >= -0.5 and measured["y_max"] <= 210.5 and measured["z_min"] == pytest.approx(0.0)
        assert fit["auto_oriented"] and fit["turned_deg"] == [0.0, 0.0, 90.0]
        assert fit["approval_carries"] is False and fit["sliced_mesh"] == "kiln_step_mesh"
        assert "Kiln's own mesh" in fit["note"] and "not the slicer's reading" in fit["note"]

    def test_only_the_turn_that_fits_is_written(self, tmp_path: Path) -> None:
        """Turns that arithmetic rules out cost no rewrite of the mesh."""
        from kiln import auto_orient

        step = _step_named(tmp_path)
        size, mesh = _kernel((205.0, 215.0, 245.0))   # fits only laid on a side face and turned
        with size, mesh, patch.object(auto_orient, "apply_orientation", wraps=auto_orient.apply_orientation) as turn:
            _sliced, refusal, fit = self._gate(step)
        assert refusal is None and fit["turned_deg"] == [90.0, 0.0, 90.0]
        assert turn.call_count == 1

    def test_a_step_that_fits_as_modelled_goes_to_the_slicer_untouched(self, tmp_path: Path) -> None:
        step = _step_named(tmp_path)
        size, mesh = _kernel((30.0, 20.0, 10.0), at=_FAR)
        with size, mesh as converted:
            sliced, refusal, fit = self._gate(step)
        assert refusal is None and sliced == step and fit["approval_carries"] is True
        converted.assert_not_called()

    def test_a_step_no_face_of_which_fits_is_refused_in_kilns_words(self, tmp_path: Path) -> None:
        step = _step_named(tmp_path)
        size, mesh = _kernel((260.0, 100.0, 100.0))
        with size, mesh:
            _sliced, refusal, _fit = self._gate(step)
        assert refusal is not None and refusal["error_code"] == "EXCEEDS_BED"

    def test_a_step_is_left_as_modelled_when_it_cannot_be_turned(self, tmp_path: Path) -> None:
        """The caller forbade moving it, or this machine cannot convert it:
        the slicer gets the file as modelled and the block says why."""
        step = _step_named(tmp_path)
        size, mesh = _kernel(_TALL)
        with size, mesh:
            sliced, refusal, fit = self._gate(step, auto_center=False)
        assert refusal is None and sliced == step and "another face" in fit["note"]
        with size, patch("kiln.step_import.ensure_mesh_path", side_effect=step_import.NoBackendError()):
            sliced, refusal, fit = self._gate(step)
        assert refusal is None and sliced == step and "another face" in fit["note"]


def _stl(tmp_path: Path, size: tuple[float, float, float]) -> str:
    box = trimesh.creation.box(extents=size)
    box.apply_translation([s / 2.0 for s in size])
    path = tmp_path / "twin.stl"
    box.export(str(path))
    return str(path)


_PLATE = (60.0, 40.0, 12.0)


class TestThePrintabilityEngine:
    def test_a_step_is_analysed_as_kilns_mesh_of_it(self, tmp_path: Path) -> None:
        from kiln.printability import analyze_printability

        size, mesh = _kernel(_PLATE, at=_FAR)
        with size, mesh:
            cad = analyze_printability(_step_named(tmp_path), material="PLA")
        twin = analyze_printability(_stl(tmp_path, _PLATE), material="PLA")
        assert (cad.score, cad.grade, cad.printable) == (twin.score, twin.grade, twin.printable)
        assert cad.dimensions_mm == pytest.approx(twin.dimensions_mm, abs=0.01)

    def test_a_step_that_cannot_be_converted_raises_the_engines_own_error(self, tmp_path: Path) -> None:
        from kiln.printability import analyze_printability

        missing = step_import.NoBackendError()
        with patch("kiln.step_import.ensure_mesh_path", side_effect=missing), pytest.raises(ValueError) as caught:
            analyze_printability(_step_named(tmp_path), material="PLA")
        assert str(caught.value) == str(missing) and caught.value.__cause__ is missing
        broken = step_import.StepImportError("the kernel read no shapes")
        with patch("kiln.step_import.ensure_mesh_path", side_effect=broken), pytest.raises(
            ValueError, match="Could not read that CAD file: the kernel read no shapes"
        ):
            analyze_printability(_step_named(tmp_path), material="PLA")

    def test_a_step_path_that_is_not_a_file_reads_like_any_missing_file(self, tmp_path: Path) -> None:
        from kiln.printability import analyze_printability

        (tmp_path / "folder.step").mkdir()
        for path in (tmp_path / "nowhere.step", tmp_path / "folder.step", tmp_path / "nowhere.stl"):
            with pytest.raises(ValueError, match="File not found"):
                analyze_printability(str(path), material="PLA")


def _estimate(path: str, effective_input: str | None = None, bed_fit_block: dict | None = None) -> dict:
    from kiln.plugins.estimate_tools import _EstimateToolsPlugin

    tools: dict = {}

    class _FakeMcp:
        def tool(self, name=None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _EstimateToolsPlugin().register(_FakeMcp())
    info = {"placement": {"plate": "unknown"}, "bed_fit": bed_fit_block, "effective_input": effective_input or path}
    sliced = SliceResult(success=True, output_path=None, slicer="PrusaSlicer", message="sliced"), None, info
    with patch("kiln.server._check_auth", return_value=None), patch(
        "kiln.plugins.estimate_tools._estimate_slice", return_value=sliced
    ):
        return tools["slice_and_estimate"](input_path=path, printer_id="bambu_a1", material="PLA")


class TestTheEstimateDoor:
    def test_a_step_estimate_carries_printability_and_the_brim_decision(self, tmp_path: Path) -> None:
        size, mesh = _kernel(_PLATE, at=_FAR)
        with size, mesh:
            cad = _estimate(_step_named(tmp_path))
        twin = _estimate(_stl(tmp_path, _PLATE))
        assert cad["success"] and twin["printability"] is not None
        assert cad["printability"] is not None, cad
        assert cad["printability"]["score"] == twin["printability"]["score"]
        assert cad["adhesion"] == twin["adhesion"]
        assert "Kiln's mesh of this STEP file" in cad["printability_note"]
        assert "printability_note" not in twin

    def test_an_estimate_that_could_not_check_says_so(self, tmp_path: Path) -> None:
        with patch("kiln.step_import.ensure_mesh_path", side_effect=step_import.NoBackendError()):
            result = _estimate(_step_named(tmp_path))
        assert result["success"] and result["printability"] is None and result["adhesion"] is None
        assert "not checked" in result["printability_note"] and "not checked" in result["message"]

    def test_the_analysis_and_the_block_are_of_the_file_that_was_sliced(self, tmp_path: Path) -> None:
        """The gate turned Kiln's mesh of the STEP: the report is of the turned
        mesh, and the estimate carries the gate's sentence."""
        from kiln.plugins.slicer_tools import _apply_bed_fit_gate

        step = _step_named(tmp_path)
        size, mesh = _kernel(_TALL, at=_FAR)
        with size, mesh:
            turned, _refusal, fit = _apply_bed_fit_gate(step, "prusa_mk4", True)
            result = _estimate(step, effective_input=turned, bed_fit_block=fit)
        assert result["printability"]["dimensions_mm"]["width_mm"] == pytest.approx(245.0, abs=0.01)
        assert result["bed_fit"]["auto_oriented"] is True and "Kiln's own mesh" in result["bed_fit"]["note"]
        assert "printability_note" not in result   # the block already says whose mesh this is
