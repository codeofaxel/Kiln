"""Tests for OpenSCAD provider -- bundled library whitelist."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from kiln.generation.base import GenerationStatus
from kiln.generation.openscad import (
    OpenSCADProvider,
    _get_bundled_library_path,
    _has_only_safe_includes,
)

_needs_openscad = pytest.mark.skipif(
    shutil.which("openscad") is None,
    reason="OpenSCAD binary not installed",
)

#: Stands in for OpenSCAD where a real render needs a display: it reads its
#: input and writes its ``-o`` file relative to its own working folder, as
#: the real binary does.
_STAND_IN_OPENSCAD = """#!/bin/sh
out=""
src=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) shift; out="$1" ;;
    -*) ;;
    *) src="$1" ;;
  esac
  shift
done
[ -f "$src" ] || { echo "ERROR: Can't open input file '$src'!" >&2; exit 1; }
printf 'stand-in' > "$out"
"""


class TestGetBundledLibraryPath:
    """_get_bundled_library_path returns a valid directory."""

    def test_returns_string(self):
        result = _get_bundled_library_path()
        assert isinstance(result, str)

    def test_path_ends_with_scad_libraries(self):
        result = _get_bundled_library_path()
        assert result.endswith("scad_libraries")

    def test_directory_exists(self):
        result = _get_bundled_library_path()
        assert os.path.isdir(result)


class TestHasOnlySafeIncludes:
    """_has_only_safe_includes validates include/use statements."""

    def test_no_includes_is_safe(self):
        assert _has_only_safe_includes("cube([10, 10, 10]);") is True

    def test_bosl2_include_is_safe(self):
        assert _has_only_safe_includes("include <BOSL2/std.scad>\ncube(10);") is True

    def test_mcad_use_is_safe(self):
        assert _has_only_safe_includes("use <MCAD/boxes.scad>\ncube(10);") is True

    def test_multiple_safe_includes(self):
        code = (
            "include <BOSL2/std.scad>\n"
            "use <BOSL2/gears.scad>\n"
            "use <MCAD/boxes.scad>\n"
            "cube(10);"
        )
        assert _has_only_safe_includes(code) is True

    def test_unsafe_include_rejected(self):
        assert _has_only_safe_includes("include </etc/passwd>") is False

    def test_relative_path_rejected(self):
        assert _has_only_safe_includes("include <../secrets.scad>") is False

    def test_unknown_library_rejected(self):
        assert _has_only_safe_includes("use <SomeLib/module.scad>") is False

    def test_mixed_safe_and_unsafe_rejected(self):
        code = (
            "include <BOSL2/std.scad>\n"
            "include <evil/hack.scad>\n"
        )
        assert _has_only_safe_includes(code) is False

    def test_path_traversal_rejected(self):
        assert _has_only_safe_includes("include <BOSL2/../../etc/passwd>") is False

    def test_deep_path_traversal_rejected(self):
        assert _has_only_safe_includes("include <BOSL2/../../../sensitive.scad>") is False

    def test_mixed_safe_and_traversal_rejected(self):
        code = (
            "use <BOSL2/foo.scad>\n"
            "include <BOSL2/../../etc/passwd>\n"
        )
        assert _has_only_safe_includes(code) is False

    def test_absolute_path_rejected(self):
        assert _has_only_safe_includes("include </etc/passwd>") is False

    def test_null_byte_rejected(self):
        assert _has_only_safe_includes("include <BOSL2/std.scad\x00/../../etc/passwd>") is False

    def test_non_ascii_rejected(self):
        # Unicode tricks to bypass path checks
        assert _has_only_safe_includes("include <BOSL2/\u2025/etc/passwd>") is False


class TestBackendChoice:
    """OpenSCAD 2024+ runs Manifold unless told otherwise, so the CGAL opt-out
    names CGAL; leaving the flag off opted out of nothing.  Fails at ac3e1cc0."""

    def _compile(self, monkeypatch, tmp_path, backend: str) -> list[str]:
        from types import SimpleNamespace
        from unittest.mock import patch

        from kiln.parametric import compile_scad_code

        monkeypatch.setenv("KILN_OPENSCAD_BACKEND", backend)
        seen: list[str] = []

        def fake_run(cmd, *_args, **_kwargs):
            seen.extend(cmd)
            with open(cmd[cmd.index("-o") + 1], "wb") as fh:
                fh.write(b"solid kiln\nendsolid kiln\n")
            return SimpleNamespace(returncode=0, stderr="")

        with (
            patch("kiln.generation.openscad._find_openscad", return_value="openscad"),
            patch("kiln.emboss_generator.get_openscad_version", return_value="2026.04.26"),
            patch("kiln.generation.openscad.subprocess.run", side_effect=fake_run),
        ):
            compile_scad_code("cube(1);", output_path=str(tmp_path / "out.stl"))
        return seen

    def test_the_opt_out_names_cgal(self, monkeypatch, tmp_path):
        cmd = self._compile(monkeypatch, tmp_path, "cgal")
        assert "--backend=cgal" in cmd
        assert "--backend=manifold" not in cmd

    def test_the_default_names_manifold(self, monkeypatch, tmp_path):
        assert "--backend=manifold" in self._compile(monkeypatch, tmp_path, "manifold")


class TestARelativePathIsTheCallers:
    """A relative path handed to the provider names a place in the caller's
    working folder, though OpenSCAD runs in a scratch folder of its own:
    the compile's folder, a boolean's output, a preview's input and image."""

    @_needs_openscad
    def test_generate_writes_into_a_relative_folder(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        provider = OpenSCADProvider()

        job = provider.generate("cube(4);", output_dir="parts")

        assert job.status == GenerationStatus.SUCCEEDED, job.error
        stl = Path(provider.download_result(job.id).local_path)
        assert stl.is_absolute()
        assert stl.parent.resolve() == (tmp_path / "parts").resolve()
        assert stl.stat().st_size > 84

    @_needs_openscad
    def test_a_boolean_writes_a_relative_output(self, tmp_path, monkeypatch):
        from kiln.parametric import compile_scad_code

        compile_scad_code("cube(10);", output_path=str(tmp_path / "a.stl"))
        compile_scad_code("translate([5, 5, 5]) cube(10);", output_path=str(tmp_path / "b.stl"))
        monkeypatch.chdir(tmp_path)

        out = OpenSCADProvider().boolean_operation("union", ["a.stl", "b.stl"], output_path="joined.stl")

        assert Path(out).resolve() == (tmp_path / "joined.stl").resolve()
        assert (tmp_path / "joined.stl").stat().st_size > 84

    @pytest.mark.skipif(os.name == "nt", reason="the stand-in OpenSCAD is a POSIX shell script")
    def test_a_preview_reads_and_writes_relative_paths(self, tmp_path, monkeypatch):
        stand_in = tmp_path / "bin" / "openscad"
        stand_in.parent.mkdir()
        stand_in.write_text(_STAND_IN_OPENSCAD, encoding="utf-8")
        stand_in.chmod(0o755)
        monkeypatch.setenv("KILN_PREVIEW_SUPERSAMPLE", "1")
        work = tmp_path / "work"
        work.mkdir()
        (work / "part.scad").write_text("cube(5);", encoding="utf-8")
        monkeypatch.chdir(work)

        png = OpenSCADProvider(binary_path=str(stand_in)).render_preview("part.scad", output_path="part.png")

        assert Path(png).resolve() == (work / "part.png").resolve()
        assert (work / "part.png").read_bytes() == b"stand-in"
