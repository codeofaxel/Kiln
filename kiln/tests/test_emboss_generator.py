"""Tests for kiln.emboss_generator module."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest


def _openscad_available() -> bool:
    try:
        subprocess.run(["openscad", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


needs_openscad = pytest.mark.skipif(
    not _openscad_available(), reason="OpenSCAD required for real text compiles"
)

# ---------------------------------------------------------------------------
# Tests: MATERIAL_DEPTHS and get_default_depth
# ---------------------------------------------------------------------------

class TestMaterialDepths:
    def test_expected_keys(self):
        from kiln.emboss_generator import MATERIAL_DEPTHS

        for key in ("PLA", "PETG", "ABS", "TPU", "Nylon", "Resin"):
            assert key in MATERIAL_DEPTHS, f"Missing material: {key}"

    def test_values_are_positive_floats(self):
        from kiln.emboss_generator import MATERIAL_DEPTHS

        for _mat, depth in MATERIAL_DEPTHS.items():
            assert isinstance(depth, float)
            assert depth > 0


class TestGetDefaultDepth:
    def test_known_materials(self):
        from kiln.emboss_generator import get_default_depth

        assert get_default_depth("PLA") == 0.6
        assert get_default_depth("TPU") == 1.2
        assert get_default_depth("Resin") == 0.3

    def test_unknown_material_fallback(self):
        from kiln.emboss_generator import get_default_depth

        assert get_default_depth("UnknownMaterial") == 0.8


# ---------------------------------------------------------------------------
# Tests: generate_emboss_scad
# ---------------------------------------------------------------------------

def _make_face(face_name: str = "top") -> dict:
    """Return a minimal face dict suitable for generate_emboss_scad."""
    return {
        "normal": [0.0, 0.0, 1.0],
        "center": [5.0, 5.0, 10.0],
        "width_mm": 10.0,
        "height_mm": 10.0,
        "face_name": face_name,
    }


class TestGenerateEmbossScad:
    def test_svg_content(self, tmp_path):
        from kiln.emboss_generator import generate_emboss_scad

        # Create a dummy SVG file for the path reference
        svg_file = tmp_path / "logo.svg"
        svg_file.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')

        content_info = {
            "type": "svg",
            "svg_path": str(svg_file),
            "width": 100,
            "height": 100,
            "aspect_ratio": 1.0,
        }

        result = generate_emboss_scad(
            model_path=str(tmp_path / "model.stl"),
            content_info=content_info,
            face=_make_face(),
            output_dir=str(tmp_path / "out"),
        )

        assert "scad_path" in result
        assert os.path.isfile(result["scad_path"])

        with open(result["scad_path"]) as f:
            scad_code = f.read()
        assert "difference()" in scad_code  # default mode is deboss
        assert "import(" in scad_code
        assert "linear_extrude" in scad_code

    @needs_openscad
    def test_text_content(self, tmp_path):
        from kiln.emboss_generator import generate_emboss_scad

        content_info = {
            "type": "openscad_text",
            "text": "KILN",
            "font_size": 12,
        }

        result = generate_emboss_scad(
            model_path=str(tmp_path / "model.stl"),
            content_info=content_info,
            face=_make_face(),
            output_dir=str(tmp_path / "out"),
            mode="emboss",
        )

        assert os.path.isfile(result["scad_path"])

        with open(result["scad_path"]) as f:
            scad_code = f.read()
        assert "union()" in scad_code  # emboss mode
        assert "text(" in scad_code
        assert "KILN" in scad_code

    def test_invalid_mode_raises(self, tmp_path):
        from kiln.emboss_generator import generate_emboss_scad

        with pytest.raises(ValueError, match="mode must be"):
            generate_emboss_scad(
                model_path=str(tmp_path / "model.stl"),
                content_info={"type": "svg", "svg_path": "/tmp/x.svg"},
                face=_make_face(),
                output_dir=str(tmp_path / "out"),
                mode="invalid",
            )

    def test_invalid_content_type_raises(self, tmp_path):
        from kiln.emboss_generator import generate_emboss_scad

        with pytest.raises(ValueError, match="content_info"):
            generate_emboss_scad(
                model_path=str(tmp_path / "model.stl"),
                content_info={"type": "unsupported"},
                face=_make_face(),
                output_dir=str(tmp_path / "out"),
            )

    @needs_openscad
    def test_output_paths(self, tmp_path):
        from kiln.emboss_generator import generate_emboss_scad

        content_info = {
            "type": "openscad_text",
            "text": "test",
        }

        result = generate_emboss_scad(
            model_path=str(tmp_path / "widget.stl"),
            content_info=content_info,
            face=_make_face(),
            output_dir=str(tmp_path / "out"),
        )

        assert result["scad_path"].endswith(".scad")
        assert result["output_stl_path"].endswith(".stl")
        assert "openscad" in result["openscad_command"]


class TestHeightmapDebossProportionalCut:
    """Pin: heightmap-deboss must produce a flat-topped, varying-bottom prism
    so the cut depth is proportional to the heightmap value at every pixel,
    not a step function that only cuts where hmap > ~0.92.

    Pre-fix, _heightmap_content_block emitted a positive Z scale and
    generate_emboss_scad translated to cz - depth_mm. The result was a
    flat-bottomed, varying-top prism whose top only reached the face surface
    when hmap >= depth/(depth+0.1) ≈ 0.92, producing sparse 1.2mm pinpoint
    pits and zero cut elsewhere. Visible on any product with a recessed face
    (jewelry tray, ashtray, divided tray) and on flat plates too.

    Fix: flip Z scale negative AND translate to cz so the flat top sits at
    the face surface and the varying bottom extends INTO the material by
    (depth+0.1)*hmap, giving a proportional cut at every column.
    """

    def _heightmap_content_info(self, tmp_path) -> dict:
        dat_file = tmp_path / "hmap.dat"
        dat_file.write_text("0.5 0.7\n0.3 1.0\n")
        return {
            "type": "heightmap",
            "dat_path": str(dat_file),
            "width_px": 2,
            "height_px": 2,
            "aspect_ratio": 1.0,
        }

    def test_deboss_emits_negative_z_scale_and_translates_to_cz(self, tmp_path):
        from kiln.emboss_generator import generate_emboss_scad

        result = generate_emboss_scad(
            model_path=str(tmp_path / "model.stl"),
            content_info=self._heightmap_content_info(tmp_path),
            face=_make_face(),  # cz = 10.0
            output_dir=str(tmp_path / "out"),
            depth_mm=1.2,
            mode="deboss",
        )
        with open(result["scad_path"]) as fh:
            scad_code = fh.read()

        # Negative Z scale — flat-topped, varying-bottomed prism.
        assert ", -1.300000])" in scad_code, (
            f"Expected negative Z scale on surface(); got:\n{scad_code}"
        )
        # Translate Z must equal cz, not cz - depth_mm. Pre-fix value 8.8
        # would put the prism's flat side below the face surface, producing
        # the step-function cut.
        assert "10.000000])" in scad_code, (
            f"Expected translate Z = cz = 10.0; got:\n{scad_code}"
        )
        assert "8.800000])" not in scad_code, (
            f"Found stale pre-fix translate Z = cz - depth_mm; got:\n{scad_code}"
        )
        # Sanity: deboss is a difference() boolean.
        assert "difference()" in scad_code

    def test_emboss_keeps_positive_z_scale(self, tmp_path):
        """Emboss path was always correct; pin so the deboss fix doesn't regress it."""
        from kiln.emboss_generator import generate_emboss_scad

        result = generate_emboss_scad(
            model_path=str(tmp_path / "model.stl"),
            content_info=self._heightmap_content_info(tmp_path),
            face=_make_face(),  # cz = 10.0
            output_dir=str(tmp_path / "out"),
            depth_mm=1.2,
            mode="emboss",
        )
        with open(result["scad_path"]) as fh:
            scad_code = fh.read()

        # Positive Z scale: prism extends upward from the face surface.
        assert ", 1.300000])" in scad_code, (
            f"Expected positive Z scale on surface(); got:\n{scad_code}"
        )
        assert ", -1.300000])" not in scad_code
        # Emboss is a union() boolean; translate Z stays at cz.
        assert "union()" in scad_code
        assert "10.000000])" in scad_code


# ---------------------------------------------------------------------------
# Tests: _openscad_version_year
# ---------------------------------------------------------------------------

class TestOpenscadVersionYear:
    """Unit tests for _openscad_version_year() — pure string parsing, no I/O."""

    def test_normal_version(self):
        from kiln.emboss_generator import _openscad_version_year

        assert _openscad_version_year("2021.01") == 2021

    def test_three_part_version_2026(self):
        from kiln.emboss_generator import _openscad_version_year

        assert _openscad_version_year("2026.04.03") == 2026

    def test_three_part_version_2024(self):
        from kiln.emboss_generator import _openscad_version_year

        assert _openscad_version_year("2024.12.19") == 2024

    def test_empty_string_returns_zero(self):
        from kiln.emboss_generator import _openscad_version_year

        assert _openscad_version_year("") == 0

    def test_garbage_string_returns_zero(self):
        from kiln.emboss_generator import _openscad_version_year

        assert _openscad_version_year("garbage") == 0

    def test_none_returns_zero(self):
        from kiln.emboss_generator import _openscad_version_year

        # None is not a valid str but defensive code should handle it
        assert _openscad_version_year(None) == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests: get_openscad_version — caching behaviour
# ---------------------------------------------------------------------------

class TestGetOpenscadVersionCaching:
    """Verify get_openscad_version() caches the result and never calls subprocess twice."""

    def test_caching_prevents_second_subprocess_call(self):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import get_openscad_version

        # Reset the module-level cache so the test is deterministic
        original_cache = _mod._openscad_version_cache
        try:
            _mod._openscad_version_cache = None

            with patch.object(_mod, "_detect_openscad_version", return_value="2024.12.19") as mock_detect, \
                 patch.object(_mod, "_find_openscad", return_value="/fake/openscad"):
                first = get_openscad_version()
                second = get_openscad_version()

            assert first == "2024.12.19"
            assert second == "2024.12.19"
            # _detect_openscad_version must have been called only once — result cached
            assert mock_detect.call_count == 1
        finally:
            _mod._openscad_version_cache = original_cache


# ---------------------------------------------------------------------------
# Tests: compile_embossed_model — --backend=manifold flag
# ---------------------------------------------------------------------------

class TestManifoldBackendFlag:
    """Verify compile_embossed_model passes --backend=manifold on 2024+ OpenSCAD."""

    def _write_plain_scad(self, tmp_path):
        """Write a .scad file that does NOT contain SVG import()."""
        scad = tmp_path / "test.scad"
        scad.write_text("cube([10,10,10]);")
        return scad

    def test_manifold_flag_present_for_2024(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import compile_embossed_model

        scad = self._write_plain_scad(tmp_path)
        out_stl = str(tmp_path / "out.stl")

        original_cache = _mod._openscad_version_cache
        try:
            _mod._openscad_version_cache = "2024.12.19"

            captured_cmd: list = []

            def fake_run(cmd, **kwargs):
                captured_cmd.extend(cmd)
                # Simulate success; create the output file
                import subprocess
                result = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                tmp_path.joinpath("out.stl").write_bytes(b"solid\nendsolid")
                return result

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=fake_run):
                compile_embossed_model(str(scad), out_stl)

            assert "--backend=manifold" in captured_cmd
        finally:
            _mod._openscad_version_cache = original_cache

    def test_manifold_flag_absent_for_2021(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import compile_embossed_model

        scad = self._write_plain_scad(tmp_path)
        out_stl = str(tmp_path / "out.stl")

        original_cache = _mod._openscad_version_cache
        try:
            _mod._openscad_version_cache = "2021.01"

            captured_cmd: list = []

            def fake_run(cmd, **kwargs):
                captured_cmd.extend(cmd)
                import subprocess
                result = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                tmp_path.joinpath("out.stl").write_bytes(b"solid\nendsolid")
                return result

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=fake_run):
                compile_embossed_model(str(scad), out_stl)

            assert "--backend=manifold" not in captured_cmd
        finally:
            _mod._openscad_version_cache = original_cache


# ---------------------------------------------------------------------------
# Tests: KILN_OPENSCAD_BACKEND=cgal suppresses manifold flag
# ---------------------------------------------------------------------------

class TestCgalBackendEnvVar:
    """KILN_OPENSCAD_BACKEND=cgal must suppress --backend=manifold even on 2024+."""

    def test_cgal_env_suppresses_manifold(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import compile_embossed_model

        scad = tmp_path / "test.scad"
        scad.write_text("cube([10,10,10]);")
        out_stl = str(tmp_path / "out.stl")

        original_cache = _mod._openscad_version_cache
        try:
            _mod._openscad_version_cache = "2024.12.19"

            captured_cmd: list = []

            def fake_run(cmd, **kwargs):
                captured_cmd.extend(cmd)
                import subprocess
                result = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                tmp_path.joinpath("out.stl").write_bytes(b"solid\nendsolid")
                return result

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=fake_run), \
                 patch.dict(os.environ, {"KILN_OPENSCAD_BACKEND": "cgal"}):
                compile_embossed_model(str(scad), out_stl)

            assert "--backend=manifold" not in captured_cmd
        finally:
            _mod._openscad_version_cache = original_cache


# ---------------------------------------------------------------------------
# Tests: _find_openscad probes executable compatibility before returning
# ---------------------------------------------------------------------------

@pytest.mark.use_real_openscad_probe
class TestFindOpenscadProbe:
    """_find_openscad skips binaries that exist but cannot execute."""

    @staticmethod
    def _reset_caches(_mod) -> tuple[str | None, bool, dict[str, tuple[bool, str | None]]]:
        original_version = _mod._openscad_version_cache
        original_warned = _mod._upgrade_warned
        original_probe = dict(_mod._openscad_probe_cache)
        _mod._openscad_version_cache = None
        _mod._upgrade_warned = False
        _mod._openscad_probe_cache.clear()
        return original_version, original_warned, original_probe

    @staticmethod
    def _restore_caches(_mod, state) -> None:
        version, warned, probe = state
        _mod._openscad_version_cache = version
        _mod._upgrade_warned = warned
        _mod._openscad_probe_cache.clear()
        _mod._openscad_probe_cache.update(probe)

    def test_nonzero_probe_is_skipped(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import _find_openscad

        bad = str(tmp_path / "bad-openscad")
        good = str(tmp_path / "good-openscad")
        for path in (bad, good):
            tmp_path.joinpath(os.path.basename(path)).write_text("#!/bin/sh\n")
        executable = {bad, good}

        def fake_run(cmd, **kwargs):
            if cmd[0] == bad:
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    stdout="",
                    stderr="Bad CPU type in executable",
                )
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="",
                stderr="OpenSCAD version 2024.12.19",
            )

        state = self._reset_caches(_mod)
        try:
            with patch.dict(os.environ, {"KILN_OPENSCAD_PATH": bad}), \
                 patch("kiln.emboss_generator.platform.system", return_value="Linux"), \
                 patch("kiln.emboss_generator.shutil.which", return_value=good), \
                 patch("kiln.emboss_generator.os.path.isfile", side_effect=lambda p: p in executable), \
                 patch("kiln.emboss_generator.os.access", side_effect=lambda p, _m: p in executable), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=fake_run):
                assert _find_openscad() == good
        finally:
            self._restore_caches(_mod, state)

    def test_oserror_probe_is_skipped(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import _find_openscad

        bad = str(tmp_path / "bad-openscad")
        good = str(tmp_path / "good-openscad")
        for path in (bad, good):
            tmp_path.joinpath(os.path.basename(path)).write_text("#!/bin/sh\n")
        executable = {bad, good}

        def fake_run(cmd, **kwargs):
            if cmd[0] == bad:
                raise OSError("exec format error: Bad CPU type in executable")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="OpenSCAD version 2024.12.19",
                stderr="",
            )

        state = self._reset_caches(_mod)
        try:
            with patch.dict(os.environ, {"KILN_OPENSCAD_PATH": bad}), \
                 patch("kiln.emboss_generator.platform.system", return_value="Linux"), \
                 patch("kiln.emboss_generator.shutil.which", return_value=good), \
                 patch("kiln.emboss_generator.os.path.isfile", side_effect=lambda p: p in executable), \
                 patch("kiln.emboss_generator.os.access", side_effect=lambda p, _m: p in executable), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=fake_run):
                assert _find_openscad() == good
        finally:
            self._restore_caches(_mod, state)

    def test_only_path_with_passing_probe_is_returned(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import _find_openscad

        good = str(tmp_path / "good-openscad")
        tmp_path.joinpath("good-openscad").write_text("#!/bin/sh\n")

        state = self._reset_caches(_mod)
        try:
            with patch.dict(os.environ, {"KILN_OPENSCAD_PATH": good}), \
                 patch("kiln.emboss_generator.platform.system", return_value="Linux"), \
                 patch("kiln.emboss_generator.shutil.which", return_value=None), \
                 patch("kiln.emboss_generator.os.path.isfile", side_effect=lambda p: p == good), \
                 patch("kiln.emboss_generator.os.access", side_effect=lambda p, _m: p == good), \
                 patch(
                     "kiln.emboss_generator.subprocess.run",
                     return_value=subprocess.CompletedProcess(
                         [good, "--version"],
                         0,
                         stdout="OpenSCAD version 2024.12.19",
                         stderr="",
                     ),
                 ):
                assert _find_openscad() == good
        finally:
            self._restore_caches(_mod, state)

    def test_zero_passing_paths_reports_all_attempts(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import _find_openscad

        env_path = str(tmp_path / "env-openscad")
        path_path = str(tmp_path / "path-openscad")
        for path in (env_path, path_path):
            tmp_path.joinpath(os.path.basename(path)).write_text("#!/bin/sh\n")
        executable = {env_path, path_path}

        def fake_run(cmd, **kwargs):
            if cmd[0] == env_path:
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    stdout="",
                    stderr="Incompatible processor. This Qt build requires neon",
                )
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="Bad CPU type in executable",
            )

        state = self._reset_caches(_mod)
        try:
            with patch.dict(os.environ, {"KILN_OPENSCAD_PATH": env_path}), \
                 patch("kiln.emboss_generator.platform.system", return_value="Linux"), \
                 patch("kiln.emboss_generator.shutil.which", return_value=path_path), \
                 patch("kiln.emboss_generator.os.path.isfile", side_effect=lambda p: p in executable), \
                 patch("kiln.emboss_generator.os.access", side_effect=lambda p, _m: p in executable), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=fake_run), \
                 pytest.raises(FileNotFoundError) as exc_info:
                _find_openscad()

            message = str(exc_info.value)
            assert env_path in message
            assert path_path in message
            assert "neon" in message
            assert "Bad CPU type" in message
            assert "approve running the Kiln generation command outside the sandbox" in message
        finally:
            self._restore_caches(_mod, state)


# ---------------------------------------------------------------------------
# Tests: _find_openscad_for_svg raises RuntimeError on version < 2024
# ---------------------------------------------------------------------------

class TestFindOpenscadForSvg:
    """_find_openscad_for_svg must raise RuntimeError (not just log) on 2021."""

    def test_raises_runtime_error_for_old_version(self):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import _find_openscad_for_svg

        original_cache = _mod._openscad_version_cache
        try:
            _mod._openscad_version_cache = "2021.01"

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"), \
                 pytest.raises(RuntimeError, match="OpenSCAD 2021"):
                _find_openscad_for_svg()
        finally:
            _mod._openscad_version_cache = original_cache

    def test_does_not_raise_for_2024(self):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import _find_openscad_for_svg

        original_cache = _mod._openscad_version_cache
        try:
            _mod._openscad_version_cache = "2024.12.19"

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"):
                result = _find_openscad_for_svg()

            assert result == "/fake/openscad"
        finally:
            _mod._openscad_version_cache = original_cache

    def test_does_not_raise_for_2026(self):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import _find_openscad_for_svg

        original_cache = _mod._openscad_version_cache
        try:
            _mod._openscad_version_cache = "2026.04.03"

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"):
                result = _find_openscad_for_svg()

            assert result == "/fake/openscad"
        finally:
            _mod._openscad_version_cache = original_cache

    def test_does_not_raise_when_version_unknown(self):
        """Unknown version (empty string) should pass through — fail open, not hard."""
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import _find_openscad_for_svg

        original_cache = _mod._openscad_version_cache
        try:
            _mod._openscad_version_cache = ""

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"):
                result = _find_openscad_for_svg()

            assert result == "/fake/openscad"
        finally:
            _mod._openscad_version_cache = original_cache


# ---------------------------------------------------------------------------
# Tests: Manifold benchmark logs once per session
# ---------------------------------------------------------------------------

class TestManifoldBenchmarkLogging:
    """Manifold benchmark INFO message must log only on the first successful compile."""

    def _write_plain_scad(self, tmp_path):
        scad = tmp_path / "test.scad"
        scad.write_text("cube([10,10,10]);")
        return scad

    def _make_fake_run(self, tmp_path):
        def fake_run(cmd, **kwargs):
            import subprocess as _sp
            tmp_path.joinpath("out.stl").write_bytes(b"solid\nendsolid")
            return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")
        return fake_run

    def test_benchmark_logged_on_first_compile(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import compile_embossed_model

        scad = self._write_plain_scad(tmp_path)
        out_stl = str(tmp_path / "out.stl")

        original_cache = _mod._openscad_version_cache
        original_benchmarked = _mod._manifold_benchmarked
        try:
            _mod._openscad_version_cache = "2024.12.19"
            _mod._manifold_benchmarked = False

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=self._make_fake_run(tmp_path)), \
                 patch.object(_mod._logger, "info") as mock_info:
                compile_embossed_model(str(scad), out_stl)

            assert mock_info.called
            logged_msg = mock_info.call_args[0][0]
            assert "Manifold backend" in logged_msg
            assert "20-100x" in logged_msg
        finally:
            _mod._openscad_version_cache = original_cache
            _mod._manifold_benchmarked = original_benchmarked

    def test_benchmark_not_logged_on_second_compile(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import compile_embossed_model

        scad = self._write_plain_scad(tmp_path)
        out_stl = str(tmp_path / "out.stl")

        original_cache = _mod._openscad_version_cache
        original_benchmarked = _mod._manifold_benchmarked
        try:
            _mod._openscad_version_cache = "2024.12.19"
            _mod._manifold_benchmarked = True  # already benchmarked

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=self._make_fake_run(tmp_path)), \
                 patch.object(_mod._logger, "info") as mock_info:
                compile_embossed_model(str(scad), out_stl)

            assert not mock_info.called
        finally:
            _mod._openscad_version_cache = original_cache
            _mod._manifold_benchmarked = original_benchmarked


# ---------------------------------------------------------------------------
# Tests: Auto-fallback from Manifold to CGAL
# ---------------------------------------------------------------------------

class TestManifoldCgalFallback:
    """When Manifold compile fails, compile_embossed_model retries with CGAL."""

    def test_cgal_fallback_on_manifold_failure(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import compile_embossed_model

        scad = tmp_path / "test.scad"
        scad.write_text("cube([10,10,10]);")
        out_stl = str(tmp_path / "out.stl")

        original_cache = _mod._openscad_version_cache
        original_benchmarked = _mod._manifold_benchmarked
        try:
            _mod._openscad_version_cache = "2024.12.19"
            _mod._manifold_benchmarked = False

            call_count = {"n": 0}

            import subprocess as _sp

            def fake_run(cmd, **kwargs):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    # First call (Manifold) — fail
                    return _sp.CompletedProcess(cmd, 1, stdout="", stderr="manifold geometry error")
                # Second call (CGAL) — succeed
                tmp_path.joinpath("out.stl").write_bytes(b"solid\nendsolid")
                return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=fake_run), \
                 patch.object(_mod._logger, "warning") as mock_warn:
                result = compile_embossed_model(str(scad), out_stl)

            assert result["success"] is True
            assert call_count["n"] == 2
            assert mock_warn.called
            assert "CGAL" in mock_warn.call_args[0][0]
        finally:
            _mod._openscad_version_cache = original_cache
            _mod._manifold_benchmarked = original_benchmarked

    def test_no_fallback_when_cgal_env_set(self, tmp_path):
        """When KILN_OPENSCAD_BACKEND=cgal, no manifold attempt so no retry."""
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import compile_embossed_model

        scad = tmp_path / "test.scad"
        scad.write_text("cube([10,10,10]);")
        out_stl = str(tmp_path / "out.stl")

        original_cache = _mod._openscad_version_cache
        try:
            _mod._openscad_version_cache = "2024.12.19"

            import subprocess as _sp

            call_count = {"n": 0}

            def fake_run(cmd, **kwargs):
                call_count["n"] += 1
                tmp_path.joinpath("out.stl").write_bytes(b"solid\nendsolid")
                return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=fake_run), \
                 patch.dict(os.environ, {"KILN_OPENSCAD_BACKEND": "cgal"}):
                result = compile_embossed_model(str(scad), out_stl)

            assert result["success"] is True
            assert call_count["n"] == 1
        finally:
            _mod._openscad_version_cache = original_cache


# ---------------------------------------------------------------------------
# Tests: --enable=textmetrics flag on OpenSCAD >= 2024
# ---------------------------------------------------------------------------

class TestTextmetricsFlag:
    """--enable=textmetrics must be in the compile command on OpenSCAD >= 2024."""

    def _make_capture_run(self, tmp_path, captured_cmds: list) -> object:
        import subprocess as _sp

        def fake_run(cmd, **kwargs):
            captured_cmds.append(list(cmd))
            tmp_path.joinpath("out.stl").write_bytes(b"solid\nendsolid")
            return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

        return fake_run

    def test_textmetrics_flag_present_for_2024(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import compile_embossed_model

        scad = tmp_path / "test.scad"
        scad.write_text("cube([10,10,10]);")
        out_stl = str(tmp_path / "out.stl")

        original_cache = _mod._openscad_version_cache
        try:
            _mod._openscad_version_cache = "2024.12.19"
            captured: list = []

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=self._make_capture_run(tmp_path, captured)):
                compile_embossed_model(str(scad), out_stl)

            assert len(captured) >= 1
            assert "--enable=textmetrics" in captured[0]
        finally:
            _mod._openscad_version_cache = original_cache

    def test_textmetrics_flag_absent_for_2021(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import compile_embossed_model

        scad = tmp_path / "test.scad"
        scad.write_text("cube([10,10,10]);")
        out_stl = str(tmp_path / "out.stl")

        original_cache = _mod._openscad_version_cache
        try:
            _mod._openscad_version_cache = "2021.01"
            captured: list = []

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=self._make_capture_run(tmp_path, captured)):
                compile_embossed_model(str(scad), out_stl)

            assert len(captured) >= 1
            assert "--enable=textmetrics" not in captured[0]
        finally:
            _mod._openscad_version_cache = original_cache

    def test_textmetrics_flag_present_for_2026(self, tmp_path):
        import kiln.emboss_generator as _mod
        from kiln.emboss_generator import compile_embossed_model

        scad = tmp_path / "test.scad"
        scad.write_text("cube([10,10,10]);")
        out_stl = str(tmp_path / "out.stl")

        original_cache = _mod._openscad_version_cache
        try:
            _mod._openscad_version_cache = "2026.04.03"
            captured: list = []

            with patch.object(_mod, "_find_openscad", return_value="/fake/openscad"), \
                 patch("kiln.emboss_generator.subprocess.run", side_effect=self._make_capture_run(tmp_path, captured)):
                compile_embossed_model(str(scad), out_stl)

            assert len(captured) >= 1
            assert "--enable=textmetrics" in captured[0]
        finally:
            _mod._openscad_version_cache = original_cache


class TestOpenscadVersionWarning:
    """The make-time outdated-OpenSCAD notice (surfaced in compile_scad's result)."""

    def test_install_command_per_platform(self):
        from kiln.emboss_generator import _openscad_install_command

        with patch("kiln.emboss_generator.platform.system", return_value="Darwin"):
            assert "openscad@snapshot" in _openscad_install_command()
        with patch("kiln.emboss_generator.platform.system", return_value="Linux"):
            assert "snap install openscad --edge" in _openscad_install_command()
        with patch("kiln.emboss_generator.platform.system", return_value="Windows"):
            assert "openscad.org" in _openscad_install_command()

    def test_modern_version_no_warning(self):
        from kiln.emboss_generator import openscad_version_warning

        with patch("kiln.emboss_generator.get_openscad_version", return_value="2026.04.26"):
            assert openscad_version_warning() is None

    def test_missing_openscad_no_warning(self):
        # A missing OpenSCAD is handled prominently at the get_started front door,
        # not here — so this helper stays quiet rather than double-reporting.
        from kiln.emboss_generator import openscad_version_warning

        with patch("kiln.emboss_generator.get_openscad_version", return_value=""):
            assert openscad_version_warning() is None

    def test_outdated_version_warns_accurately(self):
        from kiln.emboss_generator import openscad_version_warning

        # install_command is platform-dependent (see test_install_command_per_platform);
        # pin the platform so this assertion is deterministic regardless of which
        # OS actually runs the test (Linux CI vs a macOS dev machine).
        with (
            patch("kiln.emboss_generator.get_openscad_version", return_value="2021.01"),
            patch("kiln.emboss_generator.platform.system", return_value="Darwin"),
        ):
            w = openscad_version_warning()
        assert w is not None
        assert w["version"] == "2021.01"
        assert w["status"] == "outdated"
        assert "snapshot" in w["install_command"]
        # The message must NOT hardcode a year — a user on 2022/2023 (also < 2024)
        # must never be told they are on "2021".  The accurate version is its own field.
        assert "2021" not in w["message"]
        assert "2023" not in w["message"]


class TestTextProbeContract:
    """Every way the text probe fails to run is TextMeasureError.

    ``_find_openscad`` RAISES ``FileNotFoundError`` when the binary is
    missing — it never returns falsy.  Before this contract was pinned,
    that exception escaped ``measure_text_block_mm`` untranslated, blew
    through the caller's ``except TextMeasureError`` fallback, and a
    missing binary crashed the whole generation instead of degrading to
    heuristic fitting.
    """

    def test_missing_binary_raises_measure_error(self, monkeypatch):
        from kiln import emboss_generator as eg

        monkeypatch.setattr(eg, "_TEXT_METRICS_CACHE", {})

        def missing_binary():
            raise FileNotFoundError("OpenSCAD not found or not usable")

        monkeypatch.setattr(eg, "_find_openscad", missing_binary)
        with pytest.raises(eg.TextMeasureError):
            eg.measure_text_block_mm("probe contract text", "Liberation Sans")


# ---------------------------------------------------------------------------
# Tests: inscribed-width fitting for elliptical faces (the rim guard)
#
# The text-sizing seam fix, 2026-08-08.  A face dict carries only bbox +
# area, so a coaster top and a square plate were indistinguishable to the
# fitter — a monogram "W" auto-fit to a 72mm box shipped with its corners
# 4.58mm past an 80mm disc's rim, silently.
# ---------------------------------------------------------------------------


class TestFaceInscribedProfile:
    def test_disc_face_matches_the_ellipse_signature(self):
        import math

        from kiln.emboss_generator import face_inscribed_profile

        face = {
            "width_mm": 80.0,
            "height_mm": 80.0,
            "area_mm2": math.pi / 4.0 * 80.0 * 80.0,
        }
        assert face_inscribed_profile(face) == (40.0, 40.0)

    def test_oval_face_matches_too(self):
        import math

        from kiln.emboss_generator import face_inscribed_profile

        face = {
            "width_mm": 100.0,
            "height_mm": 60.0,
            "area_mm2": math.pi / 4.0 * 100.0 * 60.0 * 0.99,  # tessellated
        }
        assert face_inscribed_profile(face) == (50.0, 30.0)

    def test_rectangular_face_is_not_elliptical(self):
        from kiln.emboss_generator import face_inscribed_profile

        face = {"width_mm": 70.0, "height_mm": 70.0, "area_mm2": 4900.0}
        assert face_inscribed_profile(face) is None

    def test_ring_face_is_deliberately_not_modelled(self):
        # An annulus (ashtray rim) has LESS material than the ellipse
        # model assumes — pretending it is a disc would lie in the
        # unsafe direction, so it keeps bbox fitting.
        import math

        from kiln.emboss_generator import face_inscribed_profile

        outer, inner = 40.0, 30.0
        face = {
            "width_mm": 80.0,
            "height_mm": 80.0,
            "area_mm2": math.pi * (outer**2 - inner**2),
        }
        assert face_inscribed_profile(face) is None

    def test_degenerate_faces_return_none(self):
        from kiln.emboss_generator import face_inscribed_profile

        assert face_inscribed_profile({}) is None
        assert face_inscribed_profile(
            {"width_mm": 0.0, "height_mm": 80.0, "area_mm2": 100.0}
        ) is None


class TestEllipseFitScale:
    def test_comfortably_inside_never_grows(self):
        from kiln.emboss_generator import ellipse_fit_scale

        # Fitting only shrinks: a small rect reports 1.0, not >1.
        assert ellipse_fit_scale(40.0, 40.0, 5.0, 5.0) == 1.0

    def test_centered_oversize_shrinks_corner_onto_the_rim(self):
        import math

        from kiln.emboss_generator import ellipse_fit_scale

        # The measured monogram case: 72.0 x 52.59mm "W" on an 80mm disc.
        k = ellipse_fit_scale(40.0, 40.0, 36.0, 26.3)
        assert 0.0 < k < 1.0
        # The scaled corner must land exactly on the rim.
        r = math.hypot(36.0 * k, 26.3 * k)
        assert r == pytest.approx(40.0, abs=1e-9)

    def test_offset_band_gets_less_width(self):
        from kiln.emboss_generator import ellipse_fit_scale

        centered = ellipse_fit_scale(40.0, 40.0, 30.0, 5.0, 0.0, 0.0)
        near_rim = ellipse_fit_scale(40.0, 40.0, 30.0, 5.0, 0.0, 24.0)
        assert near_rim < centered

    def test_offset_outside_the_face_fits_nothing(self):
        from kiln.emboss_generator import ellipse_fit_scale

        assert ellipse_fit_scale(40.0, 40.0, 10.0, 5.0, 0.0, 41.0) == 0.0

    def test_degenerate_axes_are_a_no_op(self):
        from kiln.emboss_generator import ellipse_fit_scale

        assert ellipse_fit_scale(0.0, 40.0, 10.0, 5.0) == 1.0


@needs_openscad
class TestRimGuardOnRoundFaces:
    """The engine-level half of the rim guard, on real compiled geometry."""

    def _disc(self, dirpath, d_mm=80.0):
        scad = os.path.join(dirpath, "disc.scad")
        with open(scad, "w") as f:
            f.write(f"cylinder(h=6, d={d_mm}, $fn=160);")
        stl = os.path.join(dirpath, "disc.stl")
        subprocess.run(["openscad", "-o", stl, scad], check=True, capture_output=True)
        return stl

    def _final_font_size(self, scad_path):
        import re

        with open(scad_path) as f:
            m = re.search(r'text\("[^"]*",\s*size=([0-9.]+)', f.read())
        assert m, "no text() in generated scad"
        return float(m.group(1))

    def test_explicit_oversize_on_disc_warns_and_stays_inside(self, tmp_path):
        from kiln.emboss_generator import (
            generate_emboss_scad,
            measure_text_block_mm,
        )
        from kiln.surface_intelligence import find_named_face

        disc = self._disc(str(tmp_path))
        face = find_named_face(disc, "top")
        result = generate_emboss_scad(
            model_path=disc,
            content_info={"type": "openscad_text", "text": "W", "font_size": 55.0},
            face=face,
            output_dir=str(tmp_path),
            depth_mm=1.2,
            mode="emboss",
            scale=0.9,
            min_edge_margin_mm=0.0,
        )
        assert any("rim" in w for w in result.get("warnings", [])), result
        size = self._final_font_size(result["scad_path"])
        assert size < 55.0
        t_w, t_h, _, _ = measure_text_block_mm("W", font_size=size)
        # Worst corner of the centered run sits inside the 40mm rim.
        import math

        assert math.hypot(t_w / 2.0, t_h / 2.0) <= 40.0 + 0.05

    def test_auto_fit_respects_the_offset_band(self, tmp_path):
        import math

        from kiln.emboss_generator import (
            generate_emboss_scad,
            measure_text_block_mm,
        )
        from kiln.surface_intelligence import find_named_face

        disc = self._disc(str(tmp_path))
        face = find_named_face(disc, "top")
        result = generate_emboss_scad(
            model_path=disc,
            content_info={"type": "openscad_text", "text": "WWWWWW"},
            face=face,
            output_dir=str(tmp_path),
            depth_mm=1.2,
            mode="emboss",
            scale=0.9,
            offset_y_mm=24.0,
            min_edge_margin_mm=0.0,
        )
        with open(result["scad_path"]) as f:
            scad = f.read()
        # The offset survives (box-based clamping used to yank 24 -> 4)…
        assert "24.000000" in scad
        # …and the run still clears the rim at that band.
        size = self._final_font_size(result["scad_path"])
        t_w, t_h, _, _ = measure_text_block_mm("WWWWWW", font_size=size)
        corner = math.hypot(t_w / 2.0, 24.0 + t_h / 2.0)
        assert corner <= 40.0 + 0.05

    def test_square_plate_keeps_plain_box_fitting(self, tmp_path):
        # A rectangular face must not be ellipse-fitted: auto mode fills
        # the target box exactly, the documented span contract.
        from kiln.emboss_generator import (
            generate_emboss_scad,
            measure_text_block_mm,
        )
        from kiln.surface_intelligence import find_named_face

        scad = os.path.join(str(tmp_path), "plate.scad")
        with open(scad, "w") as f:
            f.write("translate([0, 0, 2]) cube([70, 70, 4], center=true);")
        stl = os.path.join(str(tmp_path), "plate.stl")
        subprocess.run(["openscad", "-o", stl, scad], check=True, capture_output=True)

        face = find_named_face(stl, "top")
        result = generate_emboss_scad(
            model_path=stl,
            content_info={"type": "openscad_text", "text": "KILN"},
            face=face,
            output_dir=str(tmp_path),
            depth_mm=1.2,
            mode="emboss",
            scale=0.7,
        )
        size = self._final_font_size(result["scad_path"])
        t_w, _, _, _ = measure_text_block_mm("KILN", font_size=size)
        assert t_w == pytest.approx(70.0 * 0.7, abs=0.2)
