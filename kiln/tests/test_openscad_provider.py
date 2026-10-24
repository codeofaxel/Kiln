"""Tests for OpenSCAD provider -- bundled library whitelist."""

from __future__ import annotations

import os

from kiln.generation.openscad import _get_bundled_library_path, _has_only_safe_includes


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
