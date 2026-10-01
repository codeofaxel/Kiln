"""A STEP file handed straight to a slicing tool is drawn on the stage.

The slicer reads STEP itself, so slicing one never made a mesh -- and every
reader of a slice draws triangles: the 3D stage, the print gate's look at the
print file, the Monitor's retained copy.  Until 2026-09-30 each was handed the
STEP, the stage could draw none of it, and the panel after a STEP slice
opened on an empty-stage card, the day before a demo.  ``import_step_file``
had always shown the same part, because it converted first.

Now the slice runner draws a STEP as Kiln's mesh of it, made by the one
conversion the import uses (:func:`kiln.slicer._drawn_mesh_for`), and the
slice ledger keeps the STEP beside it.  Pinned here, at the files and the
ledger: the stage finds a mesh; the slicer's additions and the print file
join to it; a yes or a look given for the STEP still covers its print; a
STEP nothing can convert is sliced and drawn as before.  The slicer is
still handed the STEP: what prints does not change.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiln import monitor_twin, preview_evidence
from kiln.print_signoff import Clearance
from kiln.slicer import SlicerInfo, slice_file
from kiln.stage_link import find_mesh_path

pytest.importorskip("OCP", reason="a real STEP file needs the CAD kernel to write and convert")

_FAKE_SLICER = "/usr/bin/prusa-slicer"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    preview_evidence._reset_for_tests()
    yield
    preview_evidence._reset_for_tests()


def _step(path: Path) -> str:
    """A 30 x 20 x 10 mm block, written as STEP by the CAD kernel."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    writer = STEPControl_Writer()
    writer.Transfer(BRepPrimAPI_MakeBox(30.0, 20.0, 10.0).Shape(), STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(path))
    return str(path)


def _slice(model: str, out_dir: Path):
    """``slice_file`` with only the slicer binary stood in: the conversion,
    the ledger and every reader are real."""
    real_run = subprocess.run
    done = MagicMock(returncode=0, stdout="Done", stderr="")

    def _run(cmd, *args, **kwargs):
        if cmd and cmd[0] == _FAKE_SLICER:
            out = Path(cmd[cmd.index("--output") + 1])
            out.write_text("; gcode\nG1 X10 Y10 E1\n")
            return done
        return real_run(cmd, *args, **kwargs)

    out_dir.mkdir(exist_ok=True)
    with patch("kiln.slicer.find_slicer", return_value=SlicerInfo(path=_FAKE_SLICER, name="prusa-slicer", version="2.9.4")), \
            patch("subprocess.run", side_effect=_run):
        return slice_file(model, output_dir=str(out_dir))


class TestTheStageDrawsTheStep:
    def test_the_slice_names_a_mesh_the_stage_can_draw(self, tmp_path: Path) -> None:
        step = _step(tmp_path / "bracket.step")
        result = _slice(step, tmp_path / "out")

        drawn = find_mesh_path(result.to_dict())
        assert drawn, "the stage found nothing to draw for a STEP slice"
        assert drawn == result.stage_mesh_path
        assert Path(drawn).suffix == ".stl" and os.path.isfile(drawn)
        trimesh = pytest.importorskip("trimesh")
        assert trimesh.load(drawn).extents.tolist() == pytest.approx([30.0, 20.0, 10.0], abs=0.01)

    def test_the_slicer_is_still_handed_the_step(self, tmp_path: Path) -> None:
        step = _step(tmp_path / "bracket.step")
        seen: list[list[str]] = []
        real_run = subprocess.run

        def _run(cmd, *args, **kwargs):
            if cmd and cmd[0] == _FAKE_SLICER:
                seen.append(list(cmd))
                Path(cmd[cmd.index("--output") + 1]).write_text("; gcode\n")
                return MagicMock(returncode=0, stdout="", stderr="")
            return real_run(cmd, *args, **kwargs)

        (tmp_path / "out").mkdir()
        with patch("kiln.slicer.find_slicer", return_value=SlicerInfo(path=_FAKE_SLICER, name="prusa-slicer", version="2.9.4")), \
                patch("subprocess.run", side_effect=_run):
            slice_file(step, output_dir=str(tmp_path / "out"))
        assert seen and os.path.abspath(step) in seen[0]
        assert not [arg for arg in seen[0] if arg.lower().endswith(".stl")], seen[0]

    def test_a_mesh_is_drawn_as_itself(self, tmp_path: Path) -> None:
        trimesh = pytest.importorskip("trimesh")
        stl = tmp_path / "block.stl"
        trimesh.creation.box(extents=(30.0, 20.0, 10.0)).export(str(stl))
        result = _slice(str(stl), tmp_path / "out")
        assert result.stage_mesh_path == str(stl.resolve())
        entry = monitor_twin.sliced_entry_for(Path(result.output_path).name)
        assert entry["input"] == str(stl.resolve())
        assert "source" not in entry

    def test_a_step_nothing_can_convert_slices_as_before(self, tmp_path: Path, monkeypatch) -> None:
        from kiln.step_import import NoBackendError

        def _no_backend(*_a, **_k):
            raise NoBackendError("no converter on this machine")

        monkeypatch.setattr("kiln.step_import.ensure_mesh_path", _no_backend)
        step = _step(tmp_path / "bracket.step")
        result = _slice(step, tmp_path / "out")
        assert result.success
        assert result.stage_mesh_path == os.path.abspath(step)
        assert find_mesh_path(result.to_dict()) is None
        entry = monitor_twin.sliced_entry_for(Path(result.output_path).name)
        assert entry["input"] == os.path.abspath(step)
        assert "source" not in entry


class TestEveryReaderOfTheSliceJoinsToIt:
    def test_the_slicers_additions_join_the_drawn_mesh_and_the_step(self, tmp_path: Path) -> None:
        step = _step(tmp_path / "bracket.step")
        result = _slice(step, tmp_path / "out")
        # Made before the slice, so the G-code reads as the newer file.
        assert monitor_twin.sliced_output_for(result.stage_mesh_path) == result.output_path
        assert monitor_twin.sliced_output_for(step) == result.output_path

    def test_the_print_file_is_drawn_as_the_steps_mesh_and_says_so(self, tmp_path: Path) -> None:
        step = _step(tmp_path / "bracket.step")
        result = _slice(step, tmp_path / "out")
        staged, said = preview_evidence.stage_file_for(result.output_path)
        assert staged == result.stage_mesh_path
        assert "Kiln's mesh of bracket.step, the CAD file this machine sliced bracket.gcode from" in said

    def test_a_look_at_either_credits_the_print_file(self, tmp_path: Path) -> None:
        step = _step(tmp_path / "bracket.step")
        result = _slice(step, tmp_path / "out")
        assert preview_evidence.evidence_for(result.output_path)[preview_evidence.DOOR_STAGE] is None
        preview_evidence.record(preview_evidence.DOOR_STAGE, result.stage_mesh_path)
        assert preview_evidence.evidence_for(result.output_path)[preview_evidence.DOOR_STAGE]
        preview_evidence.record(preview_evidence.DOOR_PNG, step)
        assert preview_evidence.evidence_for(result.output_path)[preview_evidence.DOOR_PNG]

    @pytest.mark.parametrize("approved", ["bracket.step", "drawn"])
    def test_a_yes_for_either_covers_the_print(self, tmp_path: Path, approved: str) -> None:
        step = _step(tmp_path / "bracket.step")
        result = _slice(step, tmp_path / "out")
        file_name = step if approved == "bracket.step" else result.stage_mesh_path
        clearance = Clearance(tool="slice_and_print", file_name=file_name, printer_name=None, source="test")
        assert clearance.covers(file_name=Path(result.output_path).name, printer_name=None)


class TestTheExtractRefusalNamesTheStep:
    def test_a_placeholder_archive_of_a_step_slice_names_the_cad_file(self, tmp_path: Path) -> None:
        """A wrap of a STEP slice carries no model; the refusal points the
        person at their own CAD file, not at a mesh in a temp folder."""
        from kiln.generation.validation import extract_model_from_3mf
        from kiln.printers.bambu_3mf import repackage_gcode_as_bambu_3mf

        step = _step(tmp_path / "bracket.step")
        result = _slice(step, tmp_path / "out")
        archive = str(tmp_path / "bracket.gcode.3mf")
        repackage_gcode_as_bambu_3mf(result.output_path, archive)
        monitor_twin.note_wrapped(result.output_path, archive)
        with pytest.raises(ValueError, match=r"The CAD file it was sliced from is .*bracket\.step"):
            extract_model_from_3mf(archive, output_path=str(tmp_path / "part.stl"))


def _real_prusaslicer() -> str | None:
    found = shutil.which("prusa-slicer") or shutil.which("PrusaSlicer")
    if found:
        return found
    mac = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


@pytest.mark.skipif(_real_prusaslicer() is None, reason="needs a real PrusaSlicer")
def test_a_real_slice_of_a_real_step_reaches_the_stage(tmp_path: Path) -> None:
    """End to end on the real binary: the drawn mesh predates the G-code by
    real wall-clock time, which is what the ledger's freshness check reads."""
    from kiln.slicer_profiles import resolve_slicer_profile

    step = _step(tmp_path / "bracket.step")
    result = slice_file(
        step,
        output_dir=str(tmp_path / "out"),
        profile=resolve_slicer_profile("bambu_a1"),
        slicer_path=_real_prusaslicer(),
    )
    assert "G1 " in Path(result.output_path).read_text(errors="replace")
    assert find_mesh_path(result.to_dict()) == result.stage_mesh_path
    assert monitor_twin.sliced_output_for(result.stage_mesh_path) == result.output_path


@pytest.mark.skipif(_real_prusaslicer() is None, reason="needs a real PrusaSlicer")
def test_the_slice_model_tool_opens_the_stage_on_the_step(tmp_path: Path, monkeypatch) -> None:
    """Through the registered tool and the stage's own result reader --
    the door a person meets -- not just the engine underneath."""
    import json

    from kiln import local_stage
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    monkeypatch.delenv(local_stage._OPT_OUT_ENV, raising=False)
    local_stage._reset_for_tests()
    tools: dict = {}

    class _FakeMcp:
        def tool(self, name=None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_FakeMcp())
    step = _step(tmp_path / "bracket.step")
    with patch("kiln.server._check_auth", return_value=None):
        result = tools["slice_model"](
            input_path=step,
            output_dir=str(tmp_path / "out"),
            printer_id="bambu_a1",
            slicer_path=_real_prusaslicer(),
        )
    assert result.get("success"), result
    call_result = type("R", (), {"content": [type("T", (), {"text": json.dumps(result)})()], "isError": False})()
    token = local_stage.token_for_call_result(call_result)
    assert token, "the stage minted nothing for a STEP slice"
    drawn = local_stage.resolve(token)
    assert drawn and Path(drawn).suffix == ".stl"
    local_stage._reset_for_tests()
