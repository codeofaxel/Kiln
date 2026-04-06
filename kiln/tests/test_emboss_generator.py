"""Tests for kiln.emboss_generator module."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

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
