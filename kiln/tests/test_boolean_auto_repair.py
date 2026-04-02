"""Tests for the boolean_mesh_operation auto-repair pipeline.

Covers:
- _python_boolean_fallback planar triangle clipping
- boolean_mesh_operation 3-stage pipeline (fast path → auto-repair → fallback)
- Input validation (bad operation, too few files, missing files)
- Temp file cleanup after auto-repair
"""

from __future__ import annotations

import os
import struct
from unittest.mock import patch

import pytest

from kiln.generation.base import GenerationError
from kiln.generation.openscad import (
    OpenSCADProvider,
    _python_boolean_fallback,
    boolean_mesh_operation,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _write_test_cube_stl(path: str, center: tuple[float, float, float], size: float) -> None:
    """Write a minimal binary STL cube (12 triangles, 2 per face)."""
    cx, cy, cz = center
    h = size / 2.0

    # 8 vertices of the cube
    v = [
        (cx - h, cy - h, cz - h),  # 0: left  bottom back
        (cx + h, cy - h, cz - h),  # 1: right bottom back
        (cx + h, cy + h, cz - h),  # 2: right top    back
        (cx - h, cy + h, cz - h),  # 3: left  top    back
        (cx - h, cy - h, cz + h),  # 4: left  bottom front
        (cx + h, cy - h, cz + h),  # 5: right bottom front
        (cx + h, cy + h, cz + h),  # 6: right top    front
        (cx - h, cy + h, cz + h),  # 7: left  top    front
    ]

    # 12 triangles (2 per face), each as (normal, v0, v1, v2)
    # Normals are approximate — sufficient for centroid-based clipping tests
    triangles = [
        # Back face (z = cz - h)
        ((0, 0, -1), v[0], v[2], v[1]),
        ((0, 0, -1), v[0], v[3], v[2]),
        # Front face (z = cz + h)
        ((0, 0, 1), v[4], v[5], v[6]),
        ((0, 0, 1), v[4], v[6], v[7]),
        # Bottom face (y = cy - h)
        ((0, -1, 0), v[0], v[1], v[5]),
        ((0, -1, 0), v[0], v[5], v[4]),
        # Top face (y = cy + h)
        ((0, 1, 0), v[3], v[7], v[6]),
        ((0, 1, 0), v[3], v[6], v[2]),
        # Left face (x = cx - h)
        ((-1, 0, 0), v[0], v[4], v[7]),
        ((-1, 0, 0), v[0], v[7], v[3]),
        # Right face (x = cx + h)
        ((1, 0, 0), v[1], v[2], v[6]),
        ((1, 0, 0), v[1], v[6], v[5]),
    ]

    with open(path, "wb") as fh:
        # 80-byte header
        fh.write(b"\x00" * 80)
        # Triangle count
        fh.write(struct.pack("<I", len(triangles)))
        for normal, p0, p1, p2 in triangles:
            fh.write(struct.pack("<3f", *normal))
            fh.write(struct.pack("<3f", *p0))
            fh.write(struct.pack("<3f", *p1))
            fh.write(struct.pack("<3f", *p2))
            fh.write(struct.pack("<H", 0))  # attribute byte count


# ---------------------------------------------------------------------------
# TestPythonBooleanFallback
# ---------------------------------------------------------------------------


class TestPythonBooleanFallback:
    """Planar triangle clipping via _python_boolean_fallback."""

    def test_difference_removes_triangles_inside_cutter_bbox(self, tmp_path):
        body = str(tmp_path / "body.stl")
        cutter = str(tmp_path / "cutter.stl")
        output = str(tmp_path / "result.stl")

        # Body: 10x10x10 cube at origin → vertices at +/-5, 12 triangles.
        # Face triangle centroids sit on the face planes (e.g. z=-5 for back face).
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        # Cutter: 12x12x12 cube at (0, 0, -10) → bbox x[-6,6] y[-6,6] z[-16,-4].
        # Encloses back-face centroids (z=-5) and bottom-face centroids
        # but NOT front-face (z=+5) or top-face (y=+5).
        _write_test_cube_stl(cutter, (0, 0, -10), 12.0)

        _python_boolean_fallback("difference", [body, cutter], output)

        with open(output, "rb") as fh:
            fh.seek(80)
            tri_count = struct.unpack("<I", fh.read(4))[0]

        # Some triangles whose centroid falls inside the cutter bbox are removed.
        assert tri_count < 12
        assert tri_count > 0

    def test_union_raises_unsupported(self, tmp_path):
        body = str(tmp_path / "a.stl")
        cutter = str(tmp_path / "b.stl")
        output = str(tmp_path / "out.stl")
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        _write_test_cube_stl(cutter, (5, 0, 0), 10.0)

        with pytest.raises(GenerationError, match="only supports 'difference'"):
            _python_boolean_fallback("union", [body, cutter], output)

    def test_intersection_raises_unsupported(self, tmp_path):
        body = str(tmp_path / "a.stl")
        cutter = str(tmp_path / "b.stl")
        output = str(tmp_path / "out.stl")
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        _write_test_cube_stl(cutter, (5, 0, 0), 10.0)

        with pytest.raises(GenerationError, match="only supports 'difference'"):
            _python_boolean_fallback("intersection", [body, cutter], output)

    def test_empty_result_raises(self, tmp_path):
        body = str(tmp_path / "body.stl")
        cutter = str(tmp_path / "cutter.stl")
        output = str(tmp_path / "result.stl")

        # Body: small 2x2x2 cube at origin
        _write_test_cube_stl(body, (0, 0, 0), 2.0)
        # Cutter: huge 20x20x20 cube at origin — fully encloses body
        _write_test_cube_stl(cutter, (0, 0, 0), 20.0)

        with pytest.raises(GenerationError, match="removed all triangles"):
            _python_boolean_fallback("difference", [body, cutter], output)

    def test_preserves_triangles_outside_cutter(self, tmp_path):
        body = str(tmp_path / "body.stl")
        cutter = str(tmp_path / "cutter.stl")
        output = str(tmp_path / "result.stl")

        # Body: 10x10x10 cube at origin
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        # Cutter: 2x2x2 cube far away — no overlap with body
        _write_test_cube_stl(cutter, (50, 50, 50), 2.0)

        _python_boolean_fallback("difference", [body, cutter], output)

        with open(output, "rb") as fh:
            fh.seek(80)
            tri_count = struct.unpack("<I", fh.read(4))[0]

        # No triangles removed — all 12 should survive
        assert tri_count == 12


# ---------------------------------------------------------------------------
# TestBooleanMeshOperationPipeline
# ---------------------------------------------------------------------------


class TestBooleanMeshOperationPipeline:
    """3-stage pipeline: fast path → auto-repair → Python fallback."""

    def test_fast_path_succeeds_no_repair(self, tmp_path):
        body = str(tmp_path / "a.stl")
        cutter = str(tmp_path / "b.stl")
        output = str(tmp_path / "out.stl")
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        _write_test_cube_stl(cutter, (0, 0, 0), 4.0)

        with (
            patch("kiln.generation.openscad._find_openscad", return_value="/fake/openscad"),
            patch.object(
                OpenSCADProvider,
                "boolean_operation",
                return_value=output,
            ),
        ):
            # Write a valid STL to output so triangle count can be read
            _write_test_cube_stl(output, (0, 0, 0), 10.0)
            result = boolean_mesh_operation("difference", [body, cutter], output_path=output)

        assert result["auto_repaired"] is False
        assert result["fallback_used"] is False
        assert result["path"] == output

    def test_auto_repair_on_bool_failed(self, tmp_path):
        body = str(tmp_path / "a.stl")
        cutter = str(tmp_path / "b.stl")
        output = str(tmp_path / "out.stl")
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        _write_test_cube_stl(cutter, (0, 0, 0), 4.0)

        call_count = 0

        def _mock_boolean(self_prov, op, fps, *, output_path=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GenerationError("Boolean failed", code="BOOL_FAILED")
            # Second call succeeds — write a result STL
            out = output_path or output
            _write_test_cube_stl(out, (0, 0, 0), 10.0)
            return out

        with (
            patch("kiln.generation.openscad._find_openscad", return_value="/fake/openscad"),
            patch.object(OpenSCADProvider, "boolean_operation", _mock_boolean),
            patch("kiln.generation.validation.repair_stl_advanced"),
        ):
            result = boolean_mesh_operation("difference", [body, cutter], output_path=output)

        assert result["auto_repaired"] is True
        assert result["fallback_used"] is False
        assert call_count == 2

    def test_auto_repair_on_bool_empty(self, tmp_path):
        body = str(tmp_path / "a.stl")
        cutter = str(tmp_path / "b.stl")
        output = str(tmp_path / "out.stl")
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        _write_test_cube_stl(cutter, (0, 0, 0), 4.0)

        call_count = 0

        def _mock_boolean(self_prov, op, fps, *, output_path=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GenerationError("Empty result", code="BOOL_EMPTY")
            out = output_path or output
            _write_test_cube_stl(out, (0, 0, 0), 10.0)
            return out

        with (
            patch("kiln.generation.openscad._find_openscad", return_value="/fake/openscad"),
            patch.object(OpenSCADProvider, "boolean_operation", _mock_boolean),
            patch("kiln.generation.validation.repair_stl_advanced"),
        ):
            result = boolean_mesh_operation("difference", [body, cutter], output_path=output)

        assert result["auto_repaired"] is True
        assert result["fallback_used"] is False

    def test_fallback_on_double_failure(self, tmp_path):
        body = str(tmp_path / "a.stl")
        cutter = str(tmp_path / "b.stl")
        output = str(tmp_path / "out.stl")
        # Body large, cutter small and far away so fallback keeps all triangles
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        _write_test_cube_stl(cutter, (50, 50, 50), 2.0)

        def _always_fail(self_prov, op, fps, *, output_path=None):
            raise GenerationError("Nope", code="BOOL_FAILED")

        with (
            patch("kiln.generation.openscad._find_openscad", return_value="/fake/openscad"),
            patch.object(OpenSCADProvider, "boolean_operation", _always_fail),
            patch("kiln.generation.validation.repair_stl_advanced"),
        ):
            result = boolean_mesh_operation("difference", [body, cutter], output_path=output)

        assert result["auto_repaired"] is True
        assert result["fallback_used"] is True
        assert os.path.isfile(result["path"])

    def test_timeout_not_retried(self, tmp_path):
        body = str(tmp_path / "a.stl")
        cutter = str(tmp_path / "b.stl")
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        _write_test_cube_stl(cutter, (0, 0, 0), 4.0)

        def _timeout(self_prov, op, fps, *, output_path=None):
            raise GenerationError("Timed out", code="BOOL_TIMEOUT")

        with (
            patch("kiln.generation.openscad._find_openscad", return_value="/fake/openscad"),
            patch.object(OpenSCADProvider, "boolean_operation", _timeout),
            pytest.raises(GenerationError, match="Timed out"),
        ):
            boolean_mesh_operation("difference", [body, cutter])

    def test_openscad_not_found_not_retried(self, tmp_path):
        body = str(tmp_path / "a.stl")
        cutter = str(tmp_path / "b.stl")
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        _write_test_cube_stl(cutter, (0, 0, 0), 4.0)

        def _exec_error(self_prov, op, fps, *, output_path=None):
            raise GenerationError("OpenSCAD crashed", code="OPENSCAD_EXEC_ERROR")

        with (
            patch("kiln.generation.openscad._find_openscad", return_value="/fake/openscad"),
            patch.object(OpenSCADProvider, "boolean_operation", _exec_error),
            pytest.raises(GenerationError, match="OpenSCAD crashed"),
        ):
            boolean_mesh_operation("difference", [body, cutter])

    def test_repaired_temp_files_cleaned_up(self, tmp_path):
        body = str(tmp_path / "a.stl")
        cutter = str(tmp_path / "b.stl")
        output = str(tmp_path / "out.stl")
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        _write_test_cube_stl(cutter, (50, 50, 50), 2.0)

        created_temps: list[str] = []
        original_mkstemp = __import__("tempfile").mkstemp

        def _tracking_mkstemp(**kwargs):
            fd, path = original_mkstemp(**kwargs)
            created_temps.append(path)
            return fd, path

        def _always_fail(self_prov, op, fps, *, output_path=None):
            raise GenerationError("Nope", code="BOOL_FAILED")

        with (
            patch("kiln.generation.openscad._find_openscad", return_value="/fake/openscad"),
            patch.object(OpenSCADProvider, "boolean_operation", _always_fail),
            patch("kiln.generation.validation.repair_stl_advanced"),
            patch("kiln.generation.openscad.tempfile.mkstemp", side_effect=_tracking_mkstemp),
        ):
            result = boolean_mesh_operation("difference", [body, cutter], output_path=output)

        # All temp files created for repair should be cleaned up
        for temp in created_temps:
            # The output path from fallback may still exist — skip it
            if temp == result["path"]:
                continue
            assert not os.path.isfile(temp), f"Temp file not cleaned up: {temp}"

    def test_return_dict_has_expected_keys(self, tmp_path):
        body = str(tmp_path / "a.stl")
        cutter = str(tmp_path / "b.stl")
        output = str(tmp_path / "out.stl")
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        _write_test_cube_stl(cutter, (0, 0, 0), 4.0)

        with (
            patch("kiln.generation.openscad._find_openscad", return_value="/fake/openscad"),
            patch.object(OpenSCADProvider, "boolean_operation", return_value=output),
        ):
            _write_test_cube_stl(output, (0, 0, 0), 10.0)
            result = boolean_mesh_operation("difference", [body, cutter], output_path=output)

        expected_keys = {
            "path", "operation", "input_files",
            "triangle_count", "auto_repaired", "fallback_used",
        }
        assert set(result.keys()) == expected_keys
        assert result["operation"] == "difference"
        assert result["input_files"] == [body, cutter]
        assert isinstance(result["triangle_count"], int)
        assert result["triangle_count"] == 12

    def test_invalid_operation_raises_valueerror(self, tmp_path):
        body = str(tmp_path / "a.stl")
        cutter = str(tmp_path / "b.stl")
        _write_test_cube_stl(body, (0, 0, 0), 10.0)
        _write_test_cube_stl(cutter, (0, 0, 0), 4.0)

        with pytest.raises(ValueError, match="Unknown operation 'foo'"):
            boolean_mesh_operation("foo", [body, cutter])

    def test_single_file_raises_valueerror(self, tmp_path):
        body = str(tmp_path / "a.stl")
        _write_test_cube_stl(body, (0, 0, 0), 10.0)

        with pytest.raises(ValueError, match="at least 2 file paths"):
            boolean_mesh_operation("difference", [body])

    def test_missing_file_raises_filenotfounderror(self, tmp_path):
        existing = str(tmp_path / "a.stl")
        missing = str(tmp_path / "nonexistent.stl")
        _write_test_cube_stl(existing, (0, 0, 0), 10.0)

        with pytest.raises(FileNotFoundError, match="nonexistent.stl"):
            boolean_mesh_operation("difference", [existing, missing])
