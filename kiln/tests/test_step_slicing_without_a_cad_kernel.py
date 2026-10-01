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


# ---------------------------------------------------------------------------
# The doors that used to filter by extension before the engine
# ---------------------------------------------------------------------------
#
# Three doors kept a hand-copy of the engine's format list (".stl", ".obj",
# ".3mf") in front of it, so a STEP -- which the engine reads since
# 2026-09-30 -- skipped the analysis without a word: no brim decision in
# slice_and_print, no geometry for the retry's diagnosis, no supports at the
# CLI.  The lists are gone; a model the engine cannot read is said.


def _overhanging_kernel():
    """Kiln's mesh of a STEP, stood in: a post with a wide plate on top, so
    the support check has something to find."""

    def fake_mesh(path: str, *, output_dir: str | None = None, with_record: bool = False):
        out = Path(output_dir or Path(path).parent) / (Path(path).stem + ".stl")
        post = trimesh.creation.box(extents=(10.0, 10.0, 40.0))
        post.apply_translation([30.0, 30.0, 20.0])
        top = trimesh.creation.box(extents=(60.0, 60.0, 5.0))
        top.apply_translation([30.0, 30.0, 42.5])
        trimesh.util.concatenate([post, top]).export(str(out))
        note = f"Converted from STEP ({Path(path).name}) to mesh."
        return (str(out), note, None) if with_record else (str(out), note)

    return patch("kiln.step_import.ensure_mesh_path", side_effect=fake_mesh)


def _slice_and_print(tmp_path: Path, path: str, monkeypatch, *, material: str = "ABS") -> tuple[dict, list[dict]]:
    """``slice_and_print`` with the printer and the slicer stood in, and the
    settings the slicer was handed, one dict per slice."""
    from unittest.mock import MagicMock

    import kiln.server as srv
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin
    from kiln.printers.base import PrinterState, PrinterStatus, PrintResult, UploadResult
    from kiln.slicer_orca import ini_to_settings

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    tools: dict = {}

    class _FakeMcp:
        def tool(self, name=None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_FakeMcp())
    gcode = tmp_path / "out.gcode"
    gcode.write_text("G28\n;LAYER_CHANGE\n;TYPE:External perimeter\nG1 X60 Y60 F600\nG1 X120 Y60 E1\n")
    handed: list[dict] = []

    def fake_slice(_path, *, profile=None, **_kw):
        handed.append(ini_to_settings(profile) if profile else {})
        return SliceResult(success=True, output_path=str(gcode), slicer="PrusaSlicer", message="Sliced")

    adapter = MagicMock(spec=["get_state", "upload_file", "start_print"])
    adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.PRINTING)
    adapter.upload_file.return_value = UploadResult(success=True, file_name="out.gcode", message="ok")
    adapter.start_print.return_value = PrintResult(success=True, message="started")
    with patch.object(srv, "_check_auth", return_value=None), \
            patch.object(srv, "_resolve_adapter", return_value=adapter), \
            patch.object(srv, "_resolve_target_printer_type", return_value="octoprint"), \
            patch.object(srv, "_resolve_effective_printer_name", return_value="p1"), \
            patch.object(srv, "_emergency_latch_error", return_value=None), \
            patch.object(srv, "preflight_check", return_value={"ready": True}), \
            patch.object(srv, "_resolve_use_ams", return_value={"use_ams": False}), \
            patch.object(srv, "_note_print_started"), \
            patch.object(srv, "_audit"), \
            patch("kiln.slicer.slice_file", side_effect=fake_slice):
        resp = tools["slice_and_print"](input_path=path, printer_id="ender3", material=material, skip_validation=True)
    return resp, handed


class TestSliceAndPrintsBrimDecision:
    def test_a_step_gets_the_brim_its_mesh_twin_gets(self, tmp_path: Path, monkeypatch) -> None:
        twin, twin_handed = _slice_and_print(tmp_path, _stl(tmp_path, _PLATE), monkeypatch)
        assert twin["success"], twin
        assert twin_handed[0].get("brim_width") == "5", "the twin's brim, or this test proves nothing"
        size, mesh = _kernel(_PLATE, at=_FAR)
        with size, mesh:
            cad, handed = _slice_and_print(tmp_path, _step_named(tmp_path), monkeypatch)
        assert cad["success"], cad
        assert handed[0].get("brim_width") == "5" and cad["adhesion"] == twin["adhesion"]
        assert "Kiln's mesh of this STEP file" in cad["printability_note"]
        assert "printability_note" not in twin

    def test_a_step_that_cannot_be_read_prints_and_says_so(self, tmp_path: Path, monkeypatch) -> None:
        with patch("kiln.step_import.ensure_mesh_path", side_effect=step_import.NoBackendError()):
            resp, handed = _slice_and_print(tmp_path, _step_named(tmp_path), monkeypatch)
        assert resp["success"], resp
        assert handed[0].get("brim_width", "0") == "0", "the profile's own brim, nothing decided"
        assert resp["printability_note"].startswith("Printability and the brim decision were not checked")


class TestTheRetrysDiagnosis:
    def _retry(self, tmp_path: Path, model: str, monkeypatch) -> dict:
        from unittest.mock import MagicMock

        import kiln.server as srv
        from kiln.plugins.smart_print_tools import plugin
        from kiln.printers.base import PrinterState, PrinterStatus, PrintResult, UploadResult

        monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        tools: dict = {}

        class _Mcp:
            def tool(self, **_kwargs):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn

                return decorator

        plugin.register(_Mcp())
        gcode = tmp_path / "retry.gcode"
        gcode.write_text("G28\n;LAYER_CHANGE\n;TYPE:External perimeter\nG1 X60 Y60 F600\nG1 X120 Y60 E1\n")
        adapter = MagicMock(spec=["get_state", "upload_file", "start_print"])
        adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.PRINTING)
        adapter.upload_file.return_value = UploadResult(success=True, file_name="retry.gcode", message="ok")
        adapter.start_print.return_value = PrintResult(success=True, message="started")
        sliced = SliceResult(success=True, output_path=str(gcode), slicer="PrusaSlicer", message="Sliced")
        with patch.object(srv, "_check_auth", return_value=None), \
                patch.object(srv, "_resolve_adapter", return_value=adapter), \
                patch.object(srv, "_resolve_effective_printer_name", return_value="p1"), \
                patch.object(srv, "_emergency_latch_error", return_value=None), \
                patch.object(srv, "preflight_check", return_value={"ready": True}), \
                patch.object(srv, "_note_print_started"), \
                patch("kiln.slicer.slice_file", return_value=sliced):
            return tools["retry_print_with_fix"](
                model_path=model, printer_id="ender3", material="PLA", skip_validation=True,
            )

    def test_a_step_models_geometry_reaches_the_diagnosis(self, tmp_path: Path, monkeypatch) -> None:
        size, mesh = _kernel(_PLATE, at=_FAR)
        with size, mesh:
            result = self._retry(tmp_path, _step_named(tmp_path), monkeypatch)
        assert result["success"], result
        assert result["diagnosis"]["signals"]["contact_percentage"] is not None
        assert "printability_note" not in result

    def test_a_model_the_diagnosis_cannot_read_is_said(self, tmp_path: Path, monkeypatch) -> None:
        with patch("kiln.step_import.ensure_mesh_path", side_effect=step_import.NoBackendError()):
            result = self._retry(tmp_path, _step_named(tmp_path), monkeypatch)
        assert result["success"], result
        assert "contact_percentage" not in result["diagnosis"]["signals"]
        assert result["printability_note"].startswith("The diagnosis was made without the model's geometry")


class TestTheCliSupportCheck:
    def _slice(self, path: str) -> tuple[dict, list[str]]:
        import json
        from unittest.mock import MagicMock

        from click.testing import CliRunner

        from kiln.cli.main import cli

        sliced = MagicMock(message="Sliced", output_path=path + ".gcode")
        sliced.to_dict.return_value = {"output_path": sliced.output_path}
        with patch("kiln.cli.main._autodetect_printer_profile_id", return_value=None), \
                patch("kiln.slicer.slice_file", return_value=sliced) as slicer:
            out = CliRunner().invoke(cli, ["slice", path, "--support-mode", "auto", "--json"])
        assert out.exit_code == 0, out.output
        return json.loads(out.output)["data"], slicer.call_args.kwargs.get("extra_args") or []

    def test_a_step_that_needs_supports_gets_them(self, tmp_path: Path) -> None:
        with _overhanging_kernel():
            data, args = self._slice(_step_named(tmp_path))
        assert "--support-material" in args
        assert data["support_style"] == "minimal" and "overhangs=" in data["support_reason"]

    def test_a_model_the_check_cannot_read_says_it_was_not_checked(self, tmp_path: Path) -> None:
        with patch("kiln.step_import.ensure_mesh_path", side_effect=step_import.NoBackendError()):
            data, args = self._slice(_step_named(tmp_path))
        assert "--support-material" not in args
        assert data["support_reason"].startswith("not checked: ")
