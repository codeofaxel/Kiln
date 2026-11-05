"""Kiln's bundled OpenSCAD libraries reach every compile, and a compile that
left part of the model out is a failure.

2026-11-04: a planetary gearbox written against BOSL2 compiled to a sun knob,
a floating flange and a carrier with no teeth, and Kiln reported success.
Kiln had passed its bundled library folder with ``-L``, behind a probe of
``openscad --help``; OpenSCAD has no ``-L`` option on any version, so the
probe always said no and BOSL2 was never on the library path, locally or on
the hosted server.  OpenSCAD then warned "Can't find include file" and
"Ignoring unknown module" for every gear, left them out, exited 0 and wrote
the rest.

Two halves, pinned separately:

- every launch through the shared runner carries the bundled folder on
  ``OPENSCADPATH`` (the one way to name a library folder on every version);
- a run that exited 0 after OpenSCAD said it left something out comes back
  failed, names what was missing, and leaves no partial file behind.

The fake-OpenSCAD tests run everywhere; the real-binary tests measure the
compiled part and skip where OpenSCAD is not installed.
"""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path

import pytest

import kiln
from kiln.openscad_runner import run_openscad

# Computed here rather than imported, so a runner that lost track of the
# folder cannot agree with itself.
_BUNDLED = str(Path(kiln.__file__).resolve().parent / "data" / "scad_libraries")


class _FakeOpenSCAD:
    """Stands in for :func:`subprocess.run` at the runner's seam."""

    def __init__(self, *, returncode=0, stderr="", writes=None):
        self.returncode = returncode
        self.stderr = stderr
        self.writes = writes
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        if self.writes:
            Path(self.writes).write_bytes(b"solid partial\nendsolid partial\n")
        text = kwargs.get("text", True)
        stderr = self.stderr if text else self.stderr.encode()
        return subprocess.CompletedProcess(list(argv), self.returncode, "" if text else b"", stderr)


@pytest.fixture
def fake_openscad(monkeypatch):
    def install(fake: _FakeOpenSCAD) -> _FakeOpenSCAD:
        monkeypatch.setattr("kiln.openscad_runner.subprocess.run", fake)
        return fake

    return install


_GEARBOX_STDERR = (
    "WARNING: Can't find include file 'BOSL2/std.scad'. in file gearbox.scad, line 1\n"
    "WARNING: Can't find include file 'BOSL2/gears.scad'. in file gearbox.scad, line 2\n"
    "WARNING: Ignoring unknown module 'spur_gear' in file gearbox.scad, line 9\n"
    "WARNING: Ignoring unknown module 'spur_gear' in file gearbox.scad, line 12\n"
    "WARNING: Ignoring unknown module 'ring_gear' in file gearbox.scad, line 15\n"
)


class TestEveryLaunchCarriesTheBundledLibraries:
    def test_openscadpath_starts_with_the_bundled_folder(self, fake_openscad):
        fake = fake_openscad(_FakeOpenSCAD())
        run_openscad(["/fake/openscad", "--version"])

        env = fake.calls[0].get("env")
        assert env is not None, "OpenSCAD was launched without Kiln's environment"
        assert env.get("OPENSCADPATH", "").split(os.pathsep)[0] == _BUNDLED
        assert (Path(_BUNDLED) / "BOSL2" / "gears.scad").is_file()
        assert env.get("PATH") == os.environ.get("PATH"), "the rest of the environment is kept"

    def test_folders_already_on_the_path_stay_after_it(self, fake_openscad, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENSCADPATH", os.pathsep.join([str(tmp_path), _BUNDLED]))
        fake = fake_openscad(_FakeOpenSCAD())
        run_openscad(["/fake/openscad", "--version"])

        assert fake.calls[0]["env"]["OPENSCADPATH"].split(os.pathsep) == [_BUNDLED, str(tmp_path)]


class TestALeftOutPartIsAFailure:
    def test_missing_library_fails_and_deletes_the_partial_file(self, fake_openscad, tmp_path):
        out = tmp_path / "gearbox.stl"
        fake_openscad(_FakeOpenSCAD(stderr=_GEARBOX_STDERR, writes=out))

        result = run_openscad(["/fake/openscad", "-o", str(out), "gearbox.scad"], output_path=str(out))

        assert result.returncode != 0
        assert not out.exists(), "the partial part was left where a caller could pick it up"
        assert "BOSL2/gears.scad" in result.stderr and "spur_gear" in result.stderr
        assert result.stderr.startswith("OpenSCAD left part of this model out")
        assert "Ignoring unknown module 'ring_gear'" in result.stderr, "OpenSCAD's own words are kept"

    @pytest.mark.parametrize(
        ("line", "named"),
        [
            ("WARNING: Can't open library 'BOSL2/std.scad'. in file a.scad, line 1", "BOSL2/std.scad"),
            ("WARNING: Ignoring unknown module 'cuboid' in file a.scad, line 3", "cuboid"),
            (
                "WARNING: Can't open import file '/tmp/kiln-callers/abc/parts/base.stl', import() at line 2",
                "base.stl",
            ),
            ("WARNING: The file '/tmp/kiln-callers/abc/relief.png' couldn't be opened.", "relief.png"),
        ],
    )
    def test_each_kind_of_left_out_part_fails(self, fake_openscad, tmp_path, line, named):
        out = tmp_path / "part.stl"
        fake_openscad(_FakeOpenSCAD(stderr=line + "\n", writes=out))

        result = run_openscad(["/fake/openscad", "-o", str(out), "a.scad"], output_path=str(out))

        assert result.returncode != 0
        assert not out.exists()
        first_line = result.stderr.splitlines()[0]
        assert f"'{named}'" in first_line
        assert "/tmp/kiln-callers" not in first_line, "a file is named by its base name only"

    def test_bytes_output_fails_the_same_way(self, fake_openscad, tmp_path):
        out = tmp_path / "part.png"
        fake_openscad(_FakeOpenSCAD(stderr=_GEARBOX_STDERR, writes=out))

        result = run_openscad(["/fake/openscad", "-o", str(out), "a.scad"], text=False, output_path=str(out))

        assert result.returncode != 0 and not out.exists()
        assert result.stderr.startswith(b"OpenSCAD left part of this model out")

    def test_the_same_words_in_an_echo_are_not_a_left_out_part(self, fake_openscad, tmp_path):
        out = tmp_path / "part.stl"
        fake_openscad(_FakeOpenSCAD(stderr='ECHO: "Ignoring unknown module"\n', writes=out))

        result = run_openscad(["/fake/openscad", "-o", str(out), "a.scad"], output_path=str(out))

        assert result.returncode == 0
        assert out.exists()

    def test_an_unknown_function_alone_is_not_refused(self, fake_openscad, tmp_path):
        out = tmp_path / "part.stl"
        stderr = "WARNING: Ignoring unknown function 'helper' in file a.scad, line 4\n"
        fake_openscad(_FakeOpenSCAD(stderr=stderr, writes=out))

        result = run_openscad(["/fake/openscad", "-o", str(out), "a.scad"], output_path=str(out))

        assert result.returncode == 0 and out.exists()


class TestEveryDoorSeesTheFailure:
    def test_the_compile_job_fails_and_says_what_was_missing(self, fake_openscad, monkeypatch, tmp_path):
        from kiln.generation import openscad as gen

        monkeypatch.setattr(gen, "_find_openscad", lambda p=None: "/fake/openscad")
        fake = _FakeOpenSCAD(stderr=_GEARBOX_STDERR)

        def run_and_write(argv, **kwargs):
            if "-o" in argv:
                fake.writes = argv[argv.index("-o") + 1]
            return fake(argv, **kwargs)

        fake_openscad(run_and_write)
        job = gen.OpenSCADProvider().generate(
            "include <BOSL2/std.scad>\ninclude <BOSL2/gears.scad>\nspur_gear(mod=2, teeth=12, thickness=5);\n",
            output_dir=str(tmp_path),
        )

        assert job.status == gen.GenerationStatus.FAILED
        assert job.error.startswith("OpenSCAD left part of this model out"), job.error
        assert "BOSL2/gears.scad" in job.error

    def test_validation_calls_it_invalid(self, fake_openscad, monkeypatch):
        from kiln.generation import openscad as gen

        monkeypatch.setattr(gen, "_find_openscad", lambda p=None: "/fake/openscad")
        fake_openscad(_FakeOpenSCAD(stderr=_GEARBOX_STDERR))

        verdict = gen.OpenSCADProvider().validate_scad(
            "include <BOSL2/std.scad>\ninclude <BOSL2/gears.scad>\nspur_gear(mod=2, teeth=12, thickness=5);\n"
        )

        assert verdict["valid"] is False


# ---------------------------------------------------------------------------
# The real binary: the part that comes out is the part the source describes
# ---------------------------------------------------------------------------


def _openscad_or_skip() -> str:
    from kiln.generation.openscad import GenerationError, _find_openscad

    try:
        return _find_openscad()
    except GenerationError:
        pytest.skip("OpenSCAD is not installed")


def _stl_x_range(path: str) -> tuple[float, float]:
    """The x extent of a binary STL, read straight from its triangles."""
    data = Path(path).read_bytes()
    (count,) = struct.unpack_from("<I", data, 80)
    xs = [
        struct.unpack_from("<f", data, 84 + 50 * i + 12 + 12 * v)[0]
        for i in range(count)
        for v in range(3)
    ]
    return min(xs), max(xs)


@pytest.mark.usefixtures("_isolate_openscad_env")
class TestTheRealCompile:
    def test_a_bosl2_gear_comes_out_with_the_part(self, tmp_path):
        """A 12-tooth module-2 gear is about 28 mm across, centred on the
        origin; the 4 mm cube beside it sits at x = 30..34.  Without BOSL2
        the file holds the cube alone and starts at x = 30."""
        _openscad_or_skip()
        from kiln.parametric import compile_scad_code

        stl = compile_scad_code(
            "include <BOSL2/std.scad>\n"
            "include <BOSL2/gears.scad>\n"
            "spur_gear(mod=2, teeth=12, thickness=5);\n"
            "translate([30, 0, 0]) cube(4);\n",
            output_path=str(tmp_path / "gear.stl"),
        )

        low, high = _stl_x_range(stl)
        assert low < -12.0, f"the gear is missing: the part starts at x={low:.2f}"
        assert high == pytest.approx(34.0, abs=0.01)

    @pytest.mark.parametrize(
        ("source", "named"),
        [
            ("include <BOSL2/no_such_file.scad>\ncube(4);\n", "BOSL2/no_such_file.scad"),
            ("use <BOSL2/no_such_file.scad>\ncube(4);\n", "BOSL2/no_such_file.scad"),
            ("cube(4);\ntranslate([10, 0, 0]) no_such_module(3);\n", "no_such_module"),
            ("cube(4);\nsurface(file=\"no_such_relief.png\");\n", "no_such_relief.png"),
        ],
    )
    def test_a_left_out_part_is_refused_not_compiled_without_it(self, tmp_path, source, named):
        _openscad_or_skip()
        from kiln.parametric import compile_scad_code

        out = tmp_path / "part.stl"
        with pytest.raises(ValueError, match="left part of this model out") as caught:
            compile_scad_code(source, output_path=str(out))

        assert named in str(caught.value)
        assert not out.exists()

    def test_the_runner_reads_the_real_import_warning(self, tmp_path):
        """compile_scad's policy refuses a missing import() before OpenSCAD
        runs; Kiln's own compiles (an emboss, a decoration) import files Kiln
        wrote, so the runner must read the real binary's words for one that
        is gone, not only the fake's."""
        binary = _openscad_or_skip()
        gone = tmp_path / "no_such_part.stl"
        source = tmp_path / "part.scad"
        source.write_text(f'cube(4);\nimport("{gone.as_posix()}");\n', encoding="utf-8")
        out = tmp_path / "part.stl"

        result = run_openscad([binary, "-o", str(out), str(source)], output_path=str(out))

        assert result.returncode != 0
        assert ("file", "no_such_part.stl") in result.left_out_parts
        assert not out.exists()


@pytest.fixture
def _isolate_openscad_env(monkeypatch):
    """A developer's own OPENSCADPATH must not be what makes BOSL2 resolve."""
    monkeypatch.delenv("OPENSCADPATH", raising=False)
