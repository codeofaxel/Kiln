"""A slice OrcaSlicer refuses is refused, never kept as a crash.

The defect this pins (2026-10-23): OrcaSlicer's command line finishes a
slice, checks it, and exits non-zero WITH complete G-code on disk when it
refuses the result: 154 for G-code in the unprintable area (a skirt past the
bed edge), 155 for the paths of two objects colliding (measured on
OrcaSlicer 2.3.2; BambuStudio 02.06.00.51 says the first as 152).  Kiln's
crash salvage read any non-zero exit with complete G-code as a crash on
exit, so it kept the refused file and reported "the slicer crashed on exit
AFTER writing complete G-code — signal -154": a door that knew no bed handed
on a skirt printing 3 mm off the plate, and a collision arrived as the
slicer's raw log line.

Covered here: only a death by signal is salvaged; each refusal is said in
the person's terms with its fix and no file; and the skirt rule every slice
door shares answers the refusal itself: the part is sliced once more without
its skirt, and that second slice is judged on its own.  Against the real
OrcaSlicer where it is installed, and against a stand-in speaking its
measured statuses and words for the paths a real slicer cannot be made to
take on demand.
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf
from kiln.printers import bed_fit
from kiln.slicer import SlicerError, SlicerInfo, slice_file
from kiln.slicer_profiles import profile_with_overrides, resolve_slicer_profile

_ORCA = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
#: The Bambu Lab A1's bed, which the bundled ``bambu_a1`` profile states.
_A1_BED = (256.0, 256.0, 256.0)


def _installed(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _box(path: Path, lo: tuple[float, float, float], hi: tuple[float, float, float]) -> str:
    """A closed, outward-wound box as a binary STL."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


def _cube_in_the_corner(tmp_path: Path) -> str:
    """A 20 mm cube placed in the bed's front-left corner, as a 3MF: the part
    fits, and a skirt around it lands at negative X and Y."""
    cube = _box(tmp_path / "cube.stl", (0, 0, 0), (20, 20, 20))
    composed = compose_multicolor_3mf(
        [ColorPart(cube, extruder=1, name="cube")], output_path=str(tmp_path / "corner.3mf"),
    )
    assert composed["success"], composed
    return composed["output_path"]


def _gcode_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.gcode")) if directory.exists() else []


def _slice_model_tool():
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    tools: dict = {}

    class _FakeMcp:
        def tool(self, name=None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_FakeMcp())
    return tools["slice_model"]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    from kiln import monitor_twin

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")


def _slice_model(path: str, out: Path, *, slicer_path: str, profile: str | None = None) -> dict:
    """The slice_model tool with no printer named: a door that knows no bed,
    so nothing but the slicer itself can say a skirt left it."""
    with patch("kiln.server._check_auth", return_value=None):
        return _slice_model_tool()(
            input_path=path, output_dir=str(out), printer_id=None, profile=profile, slicer_path=slicer_path,
        )


# ---------------------------------------------------------------------------
# The real OrcaSlicer
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _installed(_ORCA), reason="OrcaSlicer not installed")
class TestTheRealOrcaSlicer:
    def test_a_skirt_off_the_bed_is_refused_not_kept_as_a_crash(self, tmp_path):
        with pytest.raises(SlicerError) as refused:
            slice_file(
                _cube_in_the_corner(tmp_path), output_dir=str(tmp_path / "out"),
                profile=resolve_slicer_profile("bambu_a1"), slicer_path=_ORCA, timeout=600,
            )
        said = str(refused.value)
        assert "off the printable area of the bed" in said and "Move the part in from the bed edge" in said
        assert "crashed" not in said and "exited with code" not in said
        # The refused file is not handed on.
        assert _gcode_files(tmp_path / "out") == []
        # And a door that can fix the cause is told which refusal it is.
        from kiln.slicer import REFUSED_OFF_THE_BED

        assert refused.value.kind == REFUSED_OFF_THE_BED
        assert refused.value.code == "PRINTS_PAST_BED"

    def test_two_objects_whose_paths_collide_are_refused_in_plain_words(self, tmp_path, monkeypatch):
        from kiln import multicolor_3mf

        # Every part its own build object, as the compose wrote overlapping
        # parts before it joined them: the shape OrcaSlicer refuses.
        monkeypatch.setattr(multicolor_3mf, "_touching_groups", lambda parsed: [[i] for i in range(len(parsed))])
        small = _box(tmp_path / "small.stl", (115, 100, 0), (125, 120, 10))
        big = _box(tmp_path / "big.stl", (100, 100, 0), (120, 120, 20))
        composed = compose_multicolor_3mf(
            [ColorPart(small, extruder=2, color="#0000FF", name="inlay"),
             ColorPart(big, extruder=1, color="#FF0000", name="body")],
            output_path=str(tmp_path / "pair.3mf"), printer_id="bambu_a1",
        )
        assert composed["objects"] == 2, composed
        with pytest.raises(SlicerError) as refused:
            slice_file(
                composed["output_path"], output_dir=str(tmp_path / "out"),
                profile=resolve_slicer_profile("bambu_a1"), slicer_path=_ORCA, timeout=600,
            )
        said = str(refused.value)
        assert '"body"' in said and '"inlay"' in said and "Move them apart" in said
        assert "[error]" not in said and "exited with code" not in said
        assert _gcode_files(tmp_path / "out") == []
        from kiln.slicer import REFUSED_PATHS_COLLIDE

        assert refused.value.kind == REFUSED_PATHS_COLLIDE
        assert refused.value.code == "OBJECTS_COLLIDE"
        assert set(refused.value.objects) == {"body", "inlay"}

    def test_slice_model_slices_again_without_the_skirt_the_slicer_refused(self, tmp_path):
        result = _slice_model(_cube_in_the_corner(tmp_path), tmp_path / "out", slicer_path=_ORCA)
        assert result.get("success"), result
        # The file handed on is the second slice, and every print move in it
        # lands on the bed: the check before a print takes it.
        printed = bed_fit.compute_gcode_bbox(result["output_path"])
        assert bed_fit.check_bed_fit(printed, _A1_BED, source="gcode")["ok"], printed
        assert "crashed" not in result["message"]
        assert "The skirt would have printed past the edge of the bed" in result["message"]
        # OrcaSlicer refused the first file without saying how far past the
        # edge it reached, and Kiln did not measure a file it was refused.
        assert not re.search(r"\d mm past the edge", result["message"]), result["message"]


# ---------------------------------------------------------------------------
# A stand-in that speaks OrcaSlicer's measured exits
# ---------------------------------------------------------------------------

#: OrcaSlicer 2.3.2's own words for the skirt-past-the-edge refusal, as it
#: printed them on 2026-10-23.
_ORCA_OFF_THE_BED = (
    "[2026-10-23 09:24:55.913212] [0x00000001f490dd80] [error]   plate 1: found gcode in unprintable area "
    "of the printers! gcode_result->gcode_check_result.error_code = 4\n\nrun found error, exit\n"
)


def _part(x0: float, y0: float, x1: float, y1: float) -> str:
    return (
        f"G1 X{x0} Y{y0} F6000\nG1 X{x1} Y{y0} E0.5\nG1 X{x1} Y{y1} E0.5\n"
        f"G1 X{x0} Y{y1} E0.5\nG1 X{x0} Y{y0} E0.5\n"
    )


def _gcode(skirt: bool) -> str:
    """A complete first layer the way OrcaSlicer labels one: the cube in the
    corner, and when asked a skirt at X/Y -3.2."""
    body = "; generated by OrcaSlicer 2.3.2\nG90\nM83\nG28\nG1 Z0.2 F600\n;LAYER_CHANGE\n;Z:0.2\n"
    if skirt:
        body += ";TYPE:Skirt\n" + _part(-3.2, -3.2, 23.2, 23.2)
    body += ";TYPE:Outer wall\n" + _part(0.2, 0.2, 19.8, 19.8)
    return body + "; filament used [mm] = 123.4\n"


class _StandInOrca:
    """``subprocess.run`` for an OrcaSlicer at ``/x/OrcaSlicer``: writes a
    complete ``plate_1.gcode``, then exits as the real one does.

    *refuses* says when it refuses the file: ``"with_skirt"`` (a skirt past
    the bed's edge, the measured case), ``"always"`` (something the skirt is
    not, such as a prime tower), or a status to exit with whatever was asked.
    Every other command runs for real.
    """

    path = "/x/OrcaSlicer"

    def __init__(self, refuses: str | int = "with_skirt") -> None:
        self.refuses = refuses
        self.skirt_loops: list[int] = []
        self._real = subprocess.run

    def __call__(self, cmd, **kwargs):
        if not cmd or cmd[0] != self.path:
            return self._real(cmd, **kwargs)
        work = cmd[cmd.index("--outputdir") + 1]
        process = cmd[cmd.index("--load-settings") + 1].split(";")[1]
        with open(process) as fh:
            loops = int(float(json.load(fh).get("skirt_loops", "1")))
        self.skirt_loops.append(loops)
        with open(os.path.join(work, "plate_1.gcode"), "w") as fh:
            fh.write(_gcode(skirt=loops > 0))
        if isinstance(self.refuses, int):
            return SimpleNamespace(returncode=self.refuses, stdout="", stderr="")
        if self.refuses == "always" or loops > 0:
            return SimpleNamespace(returncode=154, stdout=_ORCA_OFF_THE_BED, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _with_stand_in(orca: _StandInOrca):
    info = SlicerInfo(path=orca.path, name="orcaslicer", version="OrcaSlicer-2.3.2:")
    return (
        patch("kiln.slicer.find_slicer", return_value=info),
        patch("kiln.slicer.subprocess.run", side_effect=orca),
    )


class TestOnlyACrashIsSalvaged:
    @pytest.mark.parametrize("status", [154, 155, 3])
    def test_a_status_the_slicer_chose_never_keeps_its_gcode(self, tmp_path, status):
        """Complete G-code on disk and a positive exit: a verdict, so nothing
        is kept, whatever the status."""
        orca = _StandInOrca(refuses=status)
        finder, runner = _with_stand_in(orca)
        with finder, runner, pytest.raises(SlicerError) as failed:
            slice_file(_cube_in_the_corner(tmp_path), output_dir=str(tmp_path / "out"))
        assert "crashed" not in str(failed.value)
        assert _gcode_files(tmp_path / "out") == []

    def test_a_crash_a_shell_reports_names_its_signal(self, tmp_path):
        """139 is SIGSEGV as a shell spells it (128 + 11): a crash, kept, and
        said as signal 11."""
        orca = _StandInOrca(refuses=139)
        finder, runner = _with_stand_in(orca)
        with finder, runner:
            result = slice_file(_cube_in_the_corner(tmp_path), output_dir=str(tmp_path / "out"))
        assert result.success and os.path.isfile(result.output_path)
        assert "signal 11;" in result.message and "-139" not in result.message


class TestTheSkirtRuleAnswersTheRefusal:
    def test_refused_again_without_the_skirt_is_said_in_the_persons_terms(self, tmp_path):
        orca = _StandInOrca(refuses="always")
        finder, runner = _with_stand_in(orca)
        with finder, runner:
            result = _slice_model(_cube_in_the_corner(tmp_path), tmp_path / "out", slicer_path=orca.path)
        assert result.get("success") is False, result
        assert result["error"]["code"] == "PRINTS_PAST_BED"
        said = result["error"]["message"]
        assert "without its skirt, OrcaSlicer still refuses it" in said
        assert "prime tower" in said and "Move the part in from the bed edge" in said
        # Sliced once as asked, once without the skirt, and no more.
        assert orca.skirt_loops[0] > 0 and orca.skirt_loops[1:] == [0]
        assert _gcode_files(tmp_path / "out") == []

    def test_with_no_skirt_to_drop_it_is_refused_without_another_slice(self, tmp_path):
        orca = _StandInOrca(refuses="always")
        finder, runner = _with_stand_in(orca)
        no_skirt = profile_with_overrides(resolve_slicer_profile("bambu_a1"), {"skirts": "0"})
        with finder, runner:
            result = _slice_model(
                _cube_in_the_corner(tmp_path), tmp_path / "out", slicer_path=orca.path, profile=no_skirt,
            )
        assert result.get("success") is False, result
        assert result["error"]["code"] == "PRINTS_PAST_BED"
        said = result["error"]["message"]
        assert "OrcaSlicer refuses it" in said and "without its skirt" not in said
        assert orca.skirt_loops == [0]
        assert _gcode_files(tmp_path / "out") == []
