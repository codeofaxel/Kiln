"""Kiln's bundled OpenSCAD libraries reach every compile, and a compile that
left part of the model out is a failure.

The failure this guards: a planetary gearbox written against BOSL2 compiles
to a sun knob, a floating flange and a carrier with no teeth, and Kiln
reports success.  OpenSCAD has no ``-L`` option on any version, so a library
folder passed that way (behind a probe of ``openscad --help``) is never on
the library path, locally or on the hosted server.  OpenSCAD then warns
"Can't find include file" and "Ignoring unknown module" for every gear,
leaves them out, exits 0 and writes the rest.

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


# --- a refusal names the missing module and nothing else ---
#
# The runner turns a run OpenSCAD called a success into a failure, and it
# keeps that success's report; a validator that counts every unclassified
# line as an error once the exit code is 1 then lists OpenSCAD's cache and
# render statistics as eight more "errors" after the missing module.  The
# fake runs above print warnings only, so they cannot see it.

#: The statistics OpenSCAD prints after such a run, verbatim.
_SUCCESS_REPORT = (
    "Geometries in cache: 3\n"
    "Geometry cache size in bytes: 2568\n"
    "CGAL Polyhedrons in cache: 0\n"
    "CGAL cache size in bytes: 0\n"
    "Total rendering time: 0:00:00.002\n"
    "Top level object is a 3D object (PolySet):\n"
    "Convex:       yes\n"
    "Facets:         6\n"
)
_UNKNOWN_MODULE = "WARNING: Ignoring unknown module 'not_a_real_part' in file a.scad, line 3\n"


class TestARefusalSaysOnlyWhatWentWrong:
    def test_the_runner_keeps_openscads_messages_and_drops_its_statistics(self, fake_openscad, tmp_path):
        out = tmp_path / "part.stl"
        fake_openscad(_FakeOpenSCAD(stderr=_UNKNOWN_MODULE + 'ECHO: "teeth", 12\n' + _SUCCESS_REPORT,
                                    writes=out))

        result = run_openscad(["/fake/openscad", "-o", str(out), "a.scad"], output_path=str(out))

        lines = result.stderr.splitlines()
        assert lines[0].startswith("OpenSCAD left part of this model out")
        assert lines[1:] == [_UNKNOWN_MODULE.strip(), 'ECHO: "teeth", 12']

    def test_validation_lists_the_refusal_alone_as_its_error(self, fake_openscad, monkeypatch):
        from kiln.generation import openscad as gen

        monkeypatch.setattr(gen, "_find_openscad", lambda p=None: "/fake/openscad")
        fake_openscad(_FakeOpenSCAD(stderr=_UNKNOWN_MODULE + _SUCCESS_REPORT))

        verdict = gen.OpenSCADProvider().validate_scad("cube(1);\nnot_a_real_part();\n")

        assert verdict["valid"] is False
        assert [e["message"] for e in verdict["errors"]] == [
            "OpenSCAD left part of this model out (missing module 'not_a_real_part') and still "
            "reported success, so Kiln deleted the partial file and counts the compile as failed. "
            "To fix: define the module or fix its name."
        ]
        assert [w["message"] for w in verdict["warnings"]] == [_UNKNOWN_MODULE.strip()]

    def test_the_compile_job_error_carries_no_statistics(self, fake_openscad, monkeypatch, tmp_path):
        from kiln.generation import openscad as gen

        monkeypatch.setattr(gen, "_find_openscad", lambda p=None: "/fake/openscad")
        fake_openscad(_FakeOpenSCAD(stderr=_UNKNOWN_MODULE + _SUCCESS_REPORT))

        job = gen.OpenSCADProvider().generate("cube(1);\nnot_a_real_part();\n", output_dir=str(tmp_path))

        assert job.status == gen.GenerationStatus.FAILED
        assert "not_a_real_part" in job.error
        assert "Geometries in cache" not in job.error and "Facets" not in job.error, job.error


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


class TestAnIgnoredSettingIsAFailure:
    """OpenSCAD ignores an argument a module has no parameter for, keeps the
    module's default and exits 0: the catalog's gear rack asked for
    ``height=10`` (BOSL2 calls that size ``width``) and came out 7.16 mm tall
    while every compile said it had succeeded."""

    _RACK = 'WARNING: variable "height" not specified as parameter in file rack.scad, line 3\n'

    def test_an_ignored_setting_fails_and_names_it(self, fake_openscad, tmp_path):
        out = tmp_path / "rack.stl"
        fake_openscad(_FakeOpenSCAD(stderr=self._RACK, writes=out))

        result = run_openscad(["/fake/openscad", "-o", str(out), "rack.scad"], output_path=str(out))

        assert result.returncode != 0
        assert not out.exists(), "the wrong-size part was left where a caller could pick it up"
        assert result.stderr.startswith("OpenSCAD ignored 'height' (line 3)"), result.stderr
        assert result.ignored_settings == (("height", 3),)

    def test_the_older_wording_is_read_too(self, fake_openscad, tmp_path):
        out = tmp_path / "a.stl"
        fake_openscad(_FakeOpenSCAD(
            stderr="WARNING: variable od not specified as parameter in file a.scad, line 2\n", writes=out,
        ))
        result = run_openscad(["/fake/openscad", "-o", str(out), "a.scad"], output_path=str(out))
        assert result.returncode != 0 and result.ignored_settings == (("od", 2),)

    def test_a_warning_inside_a_bundled_library_is_not_the_callers(self, fake_openscad, tmp_path):
        out = tmp_path / "a.stl"
        fake_openscad(_FakeOpenSCAD(
            stderr='WARNING: variable "foo" not specified as parameter in file /x/BOSL2/gears.scad, line 9\n',
            writes=out,
        ))
        result = run_openscad(["/fake/openscad", "-o", str(out), "a.scad"], output_path=str(out))
        assert result.returncode == 0 and out.exists()

    @pytest.mark.usefixtures("_isolate_openscad_env")
    def test_the_rack_as_it_used_to_be_listed_is_refused_and_width_builds(self, tmp_path):
        """The runner refuses the listed call (the compile layer above it reads
        height as width: tests/test_scad_settings.py); width builds 10 mm."""
        binary = _openscad_or_skip()
        from kiln.parametric import compile_scad_code

        head = "include <BOSL2/std.scad>\ninclude <BOSL2/gears.scad>\n"
        listed = tmp_path / "listed.scad"
        listed.write_text(head + "rack(circ_pitch=5, teeth=10, thickness=8, height=10);\n", encoding="utf-8")
        out = tmp_path / "listed.stl"
        refused = run_openscad([binary, "-o", str(out), str(listed)], output_path=str(out))
        assert refused.returncode != 0 and refused.ignored_settings == (("height", 3),)
        assert not out.exists()
        stl = compile_scad_code(head + "rack(circ_pitch=5, teeth=10, thickness=8, width=10);\n",
                                output_path=str(tmp_path / "width.stl"))
        data = Path(stl).read_bytes()
        (count,) = struct.unpack_from("<I", data, 80)
        zs = [struct.unpack_from("<f", data, 84 + 50 * i + 12 + 12 * v + 8)[0]
              for i in range(count) for v in range(3)]
        assert max(zs) - min(zs) == pytest.approx(10.0, abs=0.01)


@pytest.fixture
def _isolate_openscad_env(monkeypatch):
    """A developer's own OPENSCADPATH must not be what makes BOSL2 resolve."""
    monkeypatch.delenv("OPENSCADPATH", raising=False)
