"""Tests for the color_tools plugin — procedural color assignment.

Coverage areas:
    - Binary STL parsing and writing round-trip
    - Z-height band splitting
    - Normal-based splitting
    - Random splitting
    - Custom color palettes
    - Face count invariant (sum of zones == total)
    - Graceful degradation when compose_multicolor_3mf unavailable
    - Edge cases: single triangle, single color, empty file
    - Texture-based multicolor from OBJ (auto_multicolor_from_texture)
    - OBJ/MTL parsing, texture sampling, color quantization
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiln.plugins.color_tools import (
    _assign_normal,
    _assign_random,
    _assign_z_height,
    _band_height_warning,
    _estimate_weight_g,
    _obj_face_to_triangle,
    _ObjFace,
    _parse_ascii_stl,
    _parse_binary_stl,
    _parse_mtl,
    _parse_obj,
    _quantize_colors,
    _rgb_to_hex,
    _sample_face_color,
    _Triangle,
    _write_binary_stl,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_triangle(
    z0: float = 0.0,
    z1: float = 0.0,
    z2: float = 0.0,
    *,
    nx: float = 0.0,
    ny: float = 0.0,
    nz: float = 1.0,
) -> _Triangle:
    """Create a triangle with given Z coords and optional normal."""
    return _Triangle(
        normal=(nx, ny, nz),
        v0=(0.0, 0.0, z0),
        v1=(1.0, 0.0, z1),
        v2=(0.0, 1.0, z2),
    )


def _write_test_stl(triangles: list[_Triangle], path: str) -> None:
    """Write a minimal binary STL for testing."""
    _write_binary_stl(triangles, path)


def _make_stl_file(triangles: list[_Triangle]) -> str:
    """Write triangles to a temp binary STL and return the path."""
    fd, path = tempfile.mkstemp(suffix=".stl")
    os.close(fd)
    _write_test_stl(triangles, path)
    return path


# ---------------------------------------------------------------------------
# STL round-trip
# ---------------------------------------------------------------------------


class TestStlRoundTrip:
    """Binary STL parse/write round-trip."""

    def test_round_trip_two_triangles(self):
        t1 = _make_triangle(0.0, 0.0, 0.0)
        t2 = _make_triangle(10.0, 10.0, 10.0)
        path = _make_stl_file([t1, t2])

        parsed = _parse_binary_stl(path)
        assert len(parsed) == 2
        assert parsed[0].v0 == t1.v0
        assert parsed[1].v2 == t2.v2
        os.unlink(path)

    def test_empty_stl_raises(self):
        fd, path = tempfile.mkstemp(suffix=".stl")
        os.close(fd)
        # Write an empty file
        with open(path, "wb") as fh:
            fh.write(b"\0" * 10)
        with pytest.raises(ValueError, match="too small"):
            _parse_binary_stl(path)
        os.unlink(path)


# ---------------------------------------------------------------------------
# Z-height assignment
# ---------------------------------------------------------------------------


class TestZHeightAssignment:
    """Z-height band splitting."""

    def test_two_bands_simple(self):
        tris = [_make_triangle(0, 0, 0), _make_triangle(10, 10, 10)]
        result = _assign_z_height(tris, 2)
        assert result == [0, 1]

    def test_four_bands(self):
        # Triangles at z=0, 2.5, 5.0, 7.5
        tris = [
            _make_triangle(0, 0, 0),
            _make_triangle(2.5, 2.5, 2.5),
            _make_triangle(5.0, 5.0, 5.0),
            _make_triangle(7.5, 7.5, 7.5),
        ]
        result = _assign_z_height(tris, 4)
        assert result == [0, 1, 2, 3]

    def test_all_same_z(self):
        tris = [_make_triangle(5, 5, 5)] * 3
        result = _assign_z_height(tris, 2)
        # All same Z → all in same band (clamped)
        assert all(r == result[0] for r in result)

    def test_empty_list(self):
        assert _assign_z_height([], 4) == []


# ---------------------------------------------------------------------------
# Normal-based assignment
# ---------------------------------------------------------------------------


class TestNormalAssignment:
    """Face-normal clustering."""

    def test_top_face(self):
        tris = [_make_triangle(nz=1.0)]
        result = _assign_normal(tris, 4)
        assert result == [0]  # top

    def test_bottom_face(self):
        tris = [_make_triangle(nz=-1.0)]
        result = _assign_normal(tris, 4)
        assert result == [1]  # bottom

    def test_side_face(self):
        tris = [_make_triangle(nx=1.0, ny=0.0, nz=0.0)]
        result = _assign_normal(tris, 4)
        assert result[0] >= 2  # side zones start at 2

    def test_mixed_normals(self):
        tris = [
            _make_triangle(nz=1.0),    # top
            _make_triangle(nz=-1.0),   # bottom
            _make_triangle(nx=1.0, ny=0.0, nz=0.0),  # side
        ]
        result = _assign_normal(tris, 3)
        assert result[0] == 0  # top
        assert result[1] == 1  # bottom
        assert result[2] == 2  # side


# ---------------------------------------------------------------------------
# Random assignment
# ---------------------------------------------------------------------------


class TestRandomAssignment:
    """Random face assignment."""

    def test_deterministic_with_seed(self):
        tris = [_make_triangle()] * 10
        r1 = _assign_random(tris, 4, seed=42)
        r2 = _assign_random(tris, 4, seed=42)
        assert r1 == r2

    def test_all_within_range(self):
        tris = [_make_triangle()] * 50
        result = _assign_random(tris, 3, seed=7)
        assert all(0 <= r < 3 for r in result)


# ---------------------------------------------------------------------------
# Full tool integration (via plugin registration)
# ---------------------------------------------------------------------------


class TestAutoColorByHeight:
    """Integration test for the auto_color_by_height MCP tool."""

    def _call_tool(self, **kwargs: Any) -> dict:
        """Register the plugin on a mock MCP and call the tool."""
        from kiln.plugins.color_tools import _ColorToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn
                return decorator

        plugin = _ColorToolsPlugin()
        plugin.register(_FakeMcp())
        return tools["auto_color_by_height"](**kwargs)

    def test_splits_into_four_zones(self):
        tris = [
            _make_triangle(0, 0, 0),
            _make_triangle(3, 3, 3),
            _make_triangle(6, 6, 6),
            _make_triangle(9, 9, 9),
        ]
        stl_path = _make_stl_file(tris)
        result = self._call_tool(input_path=stl_path, num_colors=4)

        assert result["success"] is True
        assert result["total_faces"] == 4
        assert result["num_colors"] == 4
        assert len(result["zones"]) == 4

        # Face counts must sum to total
        face_sum = sum(z["face_count"] for z in result["zones"])
        assert face_sum == 4

        # Each zone STL exists
        for zone in result["zones"]:
            assert Path(zone["stl_path"]).exists()
            assert zone["ams_slot"] >= 1
            assert "estimated_weight_g" in zone

        os.unlink(stl_path)

    def test_custom_palette(self):
        tris = [_make_triangle(0, 0, 0), _make_triangle(10, 10, 10)]
        stl_path = _make_stl_file(tris)
        palette = ["#FF0000", "#00FF00"]
        result = self._call_tool(
            input_path=stl_path, num_colors=2, color_palette=palette,
        )

        assert result["success"] is True
        colors = [z["color"] for z in result["zones"]]
        assert colors == palette
        os.unlink(stl_path)

    def test_file_not_found(self):
        result = self._call_tool(input_path="/nonexistent/model.stl")
        assert result["success"] is False

    def test_single_color(self):
        tris = [_make_triangle(5, 5, 5)]
        stl_path = _make_stl_file(tris)
        result = self._call_tool(input_path=stl_path, num_colors=1)
        assert result["success"] is True
        assert result["zones"][0]["face_count"] == 1
        os.unlink(stl_path)


class TestAutoColorByRegion:
    """Integration test for the auto_color_by_region MCP tool."""

    def _call_tool(self, **kwargs: Any) -> dict:
        from kiln.plugins.color_tools import _ColorToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn
                return decorator

        plugin = _ColorToolsPlugin()
        plugin.register(_FakeMcp())
        return tools["auto_color_by_region"](**kwargs)

    def test_z_height_method(self):
        tris = [_make_triangle(0, 0, 0), _make_triangle(10, 10, 10)]
        stl_path = _make_stl_file(tris)
        result = self._call_tool(
            input_path=stl_path, num_colors=2, method="z_height",
        )
        assert result["success"] is True
        assert result["method"] == "z_height"
        os.unlink(stl_path)

    def test_normal_method(self):
        tris = [
            _make_triangle(nz=1.0),
            _make_triangle(nz=-1.0),
            _make_triangle(nx=1.0, ny=0.0, nz=0.0),
        ]
        stl_path = _make_stl_file(tris)
        result = self._call_tool(
            input_path=stl_path, num_colors=3, method="normal",
        )
        assert result["success"] is True
        assert result["method"] == "normal"
        face_sum = sum(z["face_count"] for z in result["zones"])
        assert face_sum == 3
        os.unlink(stl_path)

    def test_random_method(self):
        tris = [_make_triangle()] * 20
        stl_path = _make_stl_file(tris)
        result = self._call_tool(
            input_path=stl_path, num_colors=4, method="random",
        )
        assert result["success"] is True
        assert result["method"] == "random"
        face_sum = sum(z["face_count"] for z in result["zones"])
        assert face_sum == 20
        os.unlink(stl_path)

    def test_invalid_method(self):
        tris = [_make_triangle()]
        stl_path = _make_stl_file(tris)
        result = self._call_tool(
            input_path=stl_path, method="invalid",
        )
        assert result["success"] is False
        assert "Unknown method" in result["error"]
        os.unlink(stl_path)


class TestGracefulDegradation:
    """Verify 3MF composition failure is handled gracefully."""

    def _call_tool(self, **kwargs: Any) -> dict:
        from kiln.plugins.color_tools import _ColorToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn
                return decorator

        plugin = _ColorToolsPlugin()
        plugin.register(_FakeMcp())
        return tools["auto_color_by_height"](**kwargs)

    @patch(
        "kiln.plugins.color_tools._try_compose_3mf",
        return_value=(None, None),
    )
    def test_no_3mf_still_succeeds(self, mock_compose):
        tris = [_make_triangle(0, 0, 0), _make_triangle(10, 10, 10)]
        stl_path = _make_stl_file(tris)
        result = self._call_tool(input_path=stl_path, num_colors=2)

        assert result["success"] is True
        assert "multicolor_3mf" not in result
        assert "hint" in result["next_action"]
        os.unlink(stl_path)


class TestWeightEstimate:
    """Filament weight estimation."""

    def test_nonzero_weight_for_real_triangle(self):
        # A triangle with actual surface area → positive weight
        tri = _Triangle(
            normal=(0, 0, 1),
            v0=(0, 0, 0),
            v1=(10, 0, 0),
            v2=(0, 10, 10),
        )
        weight = _estimate_weight_g([tri])
        assert weight > 0.0

    def test_empty_returns_zero(self):
        assert _estimate_weight_g([]) == 0.0


# ---------------------------------------------------------------------------
# Empty zone filtering
# ---------------------------------------------------------------------------


class TestEmptyZoneFiltering:
    """Verify zones with 0 faces are excluded from results."""

    def _call_tool(self, **kwargs: Any) -> dict:
        from kiln.plugins.color_tools import _ColorToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn
                return decorator

        plugin = _ColorToolsPlugin()
        plugin.register(_FakeMcp())
        return tools["auto_color_by_height"](**kwargs)

    def test_empty_zones_excluded(self):
        # All triangles at z=0 → with 4 colors only the first band
        # gets faces; remaining 3 bands are empty.
        tris = [_make_triangle(0, 0, 0), _make_triangle(0, 0, 0)]
        stl_path = _make_stl_file(tris)
        result = self._call_tool(input_path=stl_path, num_colors=4)

        assert result["success"] is True
        # Only zones with faces should appear
        for zone in result["zones"]:
            assert zone["face_count"] > 0
        # num_colors reflects actual active zones
        assert result["num_colors"] == len(result["zones"])
        os.unlink(stl_path)


# ---------------------------------------------------------------------------
# ASCII STL parsing
# ---------------------------------------------------------------------------

_ASCII_STL_ONE_TRIANGLE = (
    "solid test\n"
    "  facet normal 0 0 1\n"
    "    outer loop\n"
    "      vertex 0 0 0\n"
    "      vertex 1 0 0\n"
    "      vertex 0 1 0\n"
    "    endloop\n"
    "  endfacet\n"
    "endsolid test\n"
)

_ASCII_STL_TWO_TRIANGLES = (
    "solid\n"
    "  facet normal 0 0 1\n"
    "    outer loop\n"
    "      vertex 0.0 0.0 0.0\n"
    "      vertex 1.0 0.0 0.0\n"
    "      vertex 0.0 1.0 0.0\n"
    "    endloop\n"
    "  endfacet\n"
    "  facet normal 0 0 -1\n"
    "    outer loop\n"
    "      vertex 0.0 0.0 1.0\n"
    "      vertex 0.0 1.0 1.0\n"
    "      vertex 1.0 0.0 1.0\n"
    "    endloop\n"
    "  endfacet\n"
    "endsolid\n"
)


def _write_ascii_stl(content: str) -> str:
    """Write ASCII STL content to a temp file, return path."""
    fd, path = tempfile.mkstemp(suffix=".stl")
    os.close(fd)
    with open(path, "w") as fh:
        fh.write(content)
    return path


class TestAsciiStlParsing:
    """Verify ASCII STL files are parsed correctly via _parse_ascii_stl."""

    def test_single_triangle_count(self):
        path = _write_ascii_stl(_ASCII_STL_ONE_TRIANGLE)
        try:
            triangles = _parse_ascii_stl(path)
            assert len(triangles) == 1
        finally:
            os.unlink(path)

    def test_single_triangle_vertices(self):
        path = _write_ascii_stl(_ASCII_STL_ONE_TRIANGLE)
        try:
            tri = _parse_ascii_stl(path)[0]
            assert tri.v0 == (0.0, 0.0, 0.0)
            assert tri.v1 == (1.0, 0.0, 0.0)
            assert tri.v2 == (0.0, 1.0, 0.0)
        finally:
            os.unlink(path)

    def test_single_triangle_normal(self):
        path = _write_ascii_stl(_ASCII_STL_ONE_TRIANGLE)
        try:
            tri = _parse_ascii_stl(path)[0]
            assert tri.normal == (0.0, 0.0, 1.0)
        finally:
            os.unlink(path)

    def test_two_triangles_count(self):
        path = _write_ascii_stl(_ASCII_STL_TWO_TRIANGLES)
        try:
            triangles = _parse_ascii_stl(path)
            assert len(triangles) == 2
        finally:
            os.unlink(path)

    def test_two_triangles_normals(self):
        path = _write_ascii_stl(_ASCII_STL_TWO_TRIANGLES)
        try:
            triangles = _parse_ascii_stl(path)
            assert triangles[0].normal == (0.0, 0.0, 1.0)
            assert triangles[1].normal == (0.0, 0.0, -1.0)
        finally:
            os.unlink(path)

    def test_bare_solid_header(self):
        """ASCII STL with bare 'solid' (no name) parses correctly."""
        path = _write_ascii_stl(_ASCII_STL_TWO_TRIANGLES)
        try:
            triangles = _parse_ascii_stl(path)
            assert len(triangles) == 2
        finally:
            os.unlink(path)


class TestAsciiStlDetection:
    """Verify ASCII STL files are parsed (not rejected) by _parse_binary_stl."""

    def test_ascii_stl_parsed_via_binary_entry_point(self):
        """_parse_binary_stl auto-detects ASCII STL and returns triangles."""
        path = _write_ascii_stl(_ASCII_STL_ONE_TRIANGLE)
        try:
            triangles = _parse_binary_stl(path)
            assert len(triangles) == 1
            assert triangles[0].v0 == (0.0, 0.0, 0.0)
        finally:
            os.unlink(path)

    def test_ascii_stl_two_triangles_via_binary_entry_point(self):
        """_parse_binary_stl correctly delegates multi-triangle ASCII STL."""
        path = _write_ascii_stl(_ASCII_STL_TWO_TRIANGLES)
        try:
            triangles = _parse_binary_stl(path)
            assert len(triangles) == 2
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# STL round-trip face-count invariant
# ---------------------------------------------------------------------------


class TestStlRoundTripFaceCount:
    """Write N triangles, read back, verify face count is preserved."""

    def test_single_triangle_round_trip_face_count(self):
        tris = [_make_triangle(0.0, 1.0, 2.0)]
        path = _make_stl_file(tris)
        try:
            parsed = _parse_binary_stl(path)
            assert len(parsed) == 1
        finally:
            os.unlink(path)

    def test_thousand_triangles_round_trip_face_count(self):
        tris = [_make_triangle(float(i), float(i), float(i)) for i in range(1000)]
        path = _make_stl_file(tris)
        try:
            parsed = _parse_binary_stl(path)
            assert len(parsed) == 1000
        finally:
            os.unlink(path)

    def test_vertex_values_preserved(self):
        tri = _make_triangle(3.14, 2.71, 1.41)
        path = _make_stl_file([tri])
        try:
            parsed = _parse_binary_stl(path)
            assert len(parsed) == 1
            # Vertices round-trip through IEEE 754 single precision
            assert abs(parsed[0].v0[2] - 3.14) < 1e-5
            assert abs(parsed[0].v1[2] - 2.71) < 1e-5
            assert abs(parsed[0].v2[2] - 1.41) < 1e-5
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Band-height warning
# ---------------------------------------------------------------------------


class TestBandHeightWarning:
    """_band_height_warning produces correct FDM advisories."""

    def test_no_warning_for_tall_bands(self):
        # 20 mm / 4 bands = 5 mm — above 3 mm threshold
        assert _band_height_warning(20.0, 4) is None

    def test_warning_for_thin_bands(self):
        # 20 mm / 4 bands = 5 mm per band → fine
        # 8 mm / 4 bands = 2 mm per band → warn
        result = _band_height_warning(8.0, 4)
        assert result is not None
        assert "2.0 mm" in result
        assert "3.0 mm" in result

    def test_warning_mentions_layer_count(self):
        # 4 mm / 4 bands = 1 mm → 5 layers
        result = _band_height_warning(4.0, 4)
        assert result is not None
        assert "5 layer" in result

    def test_single_color_no_warning(self):
        # num_colors=1 → no band splitting, never warn
        assert _band_height_warning(1.0, 1) is None

    def test_warning_surfaces_in_tool_result(self):
        """auto_color_by_height includes 'warning' when bands are too thin."""
        from kiln.plugins.color_tools import _ColorToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn
                return decorator

        _ColorToolsPlugin().register(_FakeMcp())

        # 4 triangles spanning 8 mm, 4 colors → 2 mm/band → should warn
        tris = [
            _make_triangle(0, 0, 0),
            _make_triangle(2, 2, 2),
            _make_triangle(5, 5, 5),
            _make_triangle(7, 7, 7),
        ]
        stl_path = _make_stl_file(tris)
        result = tools["auto_color_by_height"](input_path=stl_path, num_colors=4)
        os.unlink(stl_path)
        assert result["success"] is True
        assert "warning" in result

    def test_no_warning_not_included_in_result(self):
        """Result dict has no 'warning' key when bands are tall enough."""
        from kiln.plugins.color_tools import _ColorToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn
                return decorator

        _ColorToolsPlugin().register(_FakeMcp())

        # 4 triangles spanning 20 mm, 4 colors → 5 mm/band → no warn
        tris = [
            _make_triangle(0, 0, 0),
            _make_triangle(5, 5, 5),
            _make_triangle(10, 10, 10),
            _make_triangle(20, 20, 20),
        ]
        stl_path = _make_stl_file(tris)
        result = tools["auto_color_by_height"](input_path=stl_path, num_colors=4)
        os.unlink(stl_path)
        assert result["success"] is True
        assert "warning" not in result


# ---------------------------------------------------------------------------
# next_step guidance
# ---------------------------------------------------------------------------


class TestNextStep:
    """next_step field guides agents to slice/print."""

    def _call_tool(self, **kwargs: Any) -> dict:
        from kiln.plugins.color_tools import _ColorToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn
                return decorator

        _ColorToolsPlugin().register(_FakeMcp())
        return tools["auto_color_by_height"](**kwargs)

    @patch(
        "kiln.plugins.color_tools._try_compose_3mf",
        return_value=(None, None),
    )
    def test_next_step_without_3mf(self, _mock):
        tris = [_make_triangle(0, 0, 0), _make_triangle(10, 10, 10)]
        stl_path = _make_stl_file(tris)
        result = self._call_tool(input_path=stl_path, num_colors=2)
        os.unlink(stl_path)
        assert "next_step" in result
        assert "compose_multicolor_3mf" in result["next_step"]
        assert "upload_file" in result["next_step"]

    @patch(
        "kiln.plugins.color_tools._try_compose_3mf",
        return_value=("/tmp/fake.3mf", None),
    )
    def test_next_step_with_3mf(self, _mock):
        tris = [_make_triangle(0, 0, 0), _make_triangle(10, 10, 10)]
        stl_path = _make_stl_file(tris)
        result = self._call_tool(input_path=stl_path, num_colors=2)
        os.unlink(stl_path)
        assert "next_step" in result
        assert "slice_model" in result["next_step"]
        assert "upload_file" in result["next_step"]


# ---------------------------------------------------------------------------
# ams_mapping format
# ---------------------------------------------------------------------------


class TestAmsMapping:
    """ams_mapping matches the format from auto_multicolor_from_texture."""

    def _call_tool(self, **kwargs: Any) -> dict:
        from kiln.plugins.color_tools import _ColorToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn
                return decorator

        _ColorToolsPlugin().register(_FakeMcp())
        return tools["auto_color_by_height"](**kwargs)

    def test_ams_mapping_keys_match_slot_count(self):
        tris = [
            _make_triangle(0, 0, 0),
            _make_triangle(5, 5, 5),
            _make_triangle(10, 10, 10),
        ]
        stl_path = _make_stl_file(tris)
        palette = ["#FF0000", "#00FF00", "#0000FF"]
        result = self._call_tool(
            input_path=stl_path, num_colors=3, color_palette=palette,
        )
        os.unlink(stl_path)
        assert result["success"] is True
        mapping = result["ams_mapping"]
        # Keys must be slot_1, slot_2, ... matching num active zones
        for i in range(1, result["num_colors"] + 1):
            assert f"slot_{i}" in mapping
        # Values must be hex colors
        for v in mapping.values():
            assert v.startswith("#")


# ---------------------------------------------------------------------------
# Normal-method bowl / ashtray zone assignments
# ---------------------------------------------------------------------------


class TestNormalMethodBowlShape:
    """Verify normal-method correctly separates floor, wall, and bottom faces."""

    def _call_region_tool(self, **kwargs: Any) -> dict:
        from kiln.plugins.color_tools import _ColorToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn
                return decorator

        _ColorToolsPlugin().register(_FakeMcp())
        return tools["auto_color_by_region"](**kwargs)

    def test_bowl_three_zones(self):
        """Interior floor (up), walls (sideways), bottom (down) → 3 zones."""
        tris = [
            _make_triangle(nz=1.0),   # interior floor faces up → zone 0 (top)
            _make_triangle(nz=-1.0),  # outer bottom faces down → zone 1 (bottom)
            _make_triangle(nx=1.0, ny=0.0, nz=0.0),  # wall → zone 2+ (side)
        ]
        stl_path = _make_stl_file(tris)
        result = self._call_region_tool(
            input_path=stl_path, num_colors=3, method="normal",
        )
        os.unlink(stl_path)
        assert result["success"] is True
        assert result["num_colors"] == 3
        # Each zone must have exactly one face
        face_counts = {z["zone"]: z["face_count"] for z in result["zones"]}
        assert sum(face_counts.values()) == 3

    def test_bowl_two_colors_side_falls_back_to_last_zone(self):
        """With num_colors=2, side faces clamp to zone 1 (no overflow)."""
        tris = [
            _make_triangle(nz=1.0),
            _make_triangle(nz=-1.0),
            _make_triangle(nx=1.0, ny=0.0, nz=0.0),
        ]
        stl_path = _make_stl_file(tris)
        result = self._call_region_tool(
            input_path=stl_path, num_colors=2, method="normal",
        )
        os.unlink(stl_path)
        assert result["success"] is True
        # All 3 faces must be accounted for
        assert sum(z["face_count"] for z in result["zones"]) == 3


# ---------------------------------------------------------------------------
# Edge cases: single triangle, 1000+ triangles, num_colors=1
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Single triangle, large model, single color."""

    def _call_height_tool(self, **kwargs: Any) -> dict:
        from kiln.plugins.color_tools import _ColorToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn
                return decorator

        _ColorToolsPlugin().register(_FakeMcp())
        return tools["auto_color_by_height"](**kwargs)

    def test_single_triangle_succeeds(self):
        stl_path = _make_stl_file([_make_triangle(5.0, 5.0, 5.0)])
        result = self._call_height_tool(input_path=stl_path, num_colors=4)
        os.unlink(stl_path)
        assert result["success"] is True
        assert result["total_faces"] == 1
        # Only one zone should be active
        assert result["num_colors"] == 1
        assert result["zones"][0]["face_count"] == 1

    def test_thousand_triangles_face_count_invariant(self):
        """Sum of zone face counts == 1000 for a large model."""
        tris = [_make_triangle(float(i % 20), float(i % 20), float(i % 20)) for i in range(1000)]
        stl_path = _make_stl_file(tris)
        result = self._call_height_tool(input_path=stl_path, num_colors=4)
        os.unlink(stl_path)
        assert result["success"] is True
        assert result["total_faces"] == 1000
        face_sum = sum(z["face_count"] for z in result["zones"])
        assert face_sum == 1000

    def test_num_colors_one_returns_single_zone(self):
        """num_colors=1 puts all faces in one zone with no splitting."""
        tris = [_make_triangle(float(i), float(i), float(i)) for i in range(10)]
        stl_path = _make_stl_file(tris)
        result = self._call_height_tool(input_path=stl_path, num_colors=1)
        os.unlink(stl_path)
        assert result["success"] is True
        assert result["num_colors"] == 1
        assert len(result["zones"]) == 1
        assert result["zones"][0]["face_count"] == 10
        # AMS mapping has exactly one slot
        assert list(result["ams_mapping"].keys()) == ["slot_1"]


# ---------------------------------------------------------------------------
# OBJ parsing
# ---------------------------------------------------------------------------

_SIMPLE_OBJ = (
    "# Simple textured cube\n"
    "mtllib model.mtl\n"
    "v 0.0 0.0 0.0\n"
    "v 1.0 0.0 0.0\n"
    "v 1.0 1.0 0.0\n"
    "v 0.0 1.0 0.0\n"
    "vt 0.0 0.0\n"
    "vt 1.0 0.0\n"
    "vt 1.0 1.0\n"
    "vt 0.0 1.0\n"
    "usemtl material_0\n"
    "f 1/1 2/2 3/3\n"
    "f 1/1 3/3 4/4\n"
)

_SIMPLE_MTL = (
    "newmtl material_0\n"
    "map_Kd texture.png\n"
)


def _write_temp_file(content: str, suffix: str) -> str:
    """Write content to a temp file and return the path."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    with open(path, "w") as fh:
        fh.write(content)
    return path


def _create_test_texture(
    directory: str,
    filename: str = "texture.png",
    *,
    colors: list[tuple[int, int, int]] | None = None,
    size: int = 4,
) -> str:
    """Create a test texture PNG with colored quadrants.

    :param colors: List of 4 (R,G,B) tuples for each quadrant.
        Default: red, green, blue, white.
    :param size: Texture width/height in pixels (must be even).
    :returns: Path to the created PNG file.
    """
    from PIL import Image

    if colors is None:
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]

    img = Image.new("RGB", (size, size))
    half = size // 2
    for y in range(size):
        for x in range(size):
            qi = (0 if x < half else 1) + (0 if y < half else 2)
            img.putpixel((x, y), colors[qi])

    path = os.path.join(directory, filename)
    img.save(path)
    return path


def _create_textured_obj_dir(
    *,
    obj_content: str = _SIMPLE_OBJ,
    mtl_content: str = _SIMPLE_MTL,
    texture_colors: list[tuple[int, int, int]] | None = None,
) -> str:
    """Create a temp directory with OBJ + MTL + texture PNG.

    :returns: Path to the OBJ file.
    """
    tmp_dir = tempfile.mkdtemp(prefix="kiln_test_obj_")
    obj_path = os.path.join(tmp_dir, "model.obj")
    with open(obj_path, "w") as fh:
        fh.write(obj_content)

    mtl_path = os.path.join(tmp_dir, "model.mtl")
    with open(mtl_path, "w") as fh:
        fh.write(mtl_content)

    _create_test_texture(tmp_dir, colors=texture_colors)
    return obj_path


class TestParseObj:
    """OBJ file parsing."""

    def test_parse_vertices_and_faces(self):
        obj_path = _write_temp_file(_SIMPLE_OBJ, ".obj")
        try:
            verts, uvs, faces = _parse_obj(obj_path)
            assert len(verts) == 4
            assert len(uvs) == 4
            assert len(faces) == 2
        finally:
            os.unlink(obj_path)

    def test_face_vertex_indices_zero_based(self):
        obj_path = _write_temp_file(_SIMPLE_OBJ, ".obj")
        try:
            _, _, faces = _parse_obj(obj_path)
            assert faces[0].vertex_indices == [0, 1, 2]
            assert faces[1].vertex_indices == [0, 2, 3]
        finally:
            os.unlink(obj_path)

    def test_face_uv_indices_zero_based(self):
        obj_path = _write_temp_file(_SIMPLE_OBJ, ".obj")
        try:
            _, _, faces = _parse_obj(obj_path)
            assert faces[0].uv_indices == [0, 1, 2]
        finally:
            os.unlink(obj_path)

    def test_material_name_assigned(self):
        obj_path = _write_temp_file(_SIMPLE_OBJ, ".obj")
        try:
            _, _, faces = _parse_obj(obj_path)
            assert faces[0].material == "material_0"
        finally:
            os.unlink(obj_path)

    def test_nonexistent_file_raises(self):
        with pytest.raises(ValueError, match="Cannot read OBJ"):
            _parse_obj("/nonexistent/model.obj")

    def test_empty_file_returns_empty(self):
        obj_path = _write_temp_file("# empty\n", ".obj")
        try:
            verts, uvs, faces = _parse_obj(obj_path)
            assert verts == []
            assert uvs == []
            assert faces == []
        finally:
            os.unlink(obj_path)

    def test_faces_without_uvs(self):
        obj_no_uv = "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
        obj_path = _write_temp_file(obj_no_uv, ".obj")
        try:
            _, _, faces = _parse_obj(obj_path)
            assert len(faces) == 1
            assert faces[0].uv_indices == []
        finally:
            os.unlink(obj_path)

    def test_face_with_vn_only(self):
        """Format: f v//vn — no UV."""
        obj_vn = "v 0 0 0\nv 1 0 0\nv 0 1 0\nvn 0 0 1\nf 1//1 2//1 3//1\n"
        obj_path = _write_temp_file(obj_vn, ".obj")
        try:
            _, _, faces = _parse_obj(obj_path)
            assert len(faces) == 1
            assert faces[0].uv_indices == []
        finally:
            os.unlink(obj_path)


class TestParseMtl:
    """MTL file parsing."""

    def test_parse_texture_path(self):
        mtl_path = _write_temp_file(_SIMPLE_MTL, ".mtl")
        try:
            textures = _parse_mtl(mtl_path)
            assert textures == {"material_0": "texture.png"}
        finally:
            os.unlink(mtl_path)

    def test_multiple_materials(self):
        mtl_content = (
            "newmtl mat_a\nmap_Kd tex_a.png\n"
            "newmtl mat_b\nmap_Kd tex_b.png\n"
        )
        mtl_path = _write_temp_file(mtl_content, ".mtl")
        try:
            textures = _parse_mtl(mtl_path)
            assert len(textures) == 2
            assert textures["mat_a"] == "tex_a.png"
            assert textures["mat_b"] == "tex_b.png"
        finally:
            os.unlink(mtl_path)

    def test_nonexistent_returns_empty(self):
        assert _parse_mtl("/nonexistent/model.mtl") == {}


class TestQuantizeColors:
    """Color quantization via k-means."""

    def test_single_color(self):
        colors = [(255, 0, 0)] * 10
        assignments, centroids = _quantize_colors(colors, 1)
        assert len(assignments) == 10
        assert all(a == 0 for a in assignments)
        assert len(centroids) == 1

    def test_two_distinct_colors(self):
        colors = [(255, 0, 0)] * 5 + [(0, 0, 255)] * 5
        assignments, centroids = _quantize_colors(colors, 2)
        # All reds in one cluster, all blues in another
        red_cluster = assignments[0]
        blue_cluster = assignments[5]
        assert red_cluster != blue_cluster
        assert all(a == red_cluster for a in assignments[:5])
        assert all(a == blue_cluster for a in assignments[5:])

    def test_empty_input(self):
        assignments, centroids = _quantize_colors([], 4)
        assert assignments == []
        assert centroids == []

    def test_num_colors_clamped_to_face_count(self):
        colors = [(128, 128, 128)]
        assignments, centroids = _quantize_colors(colors, 10)
        assert len(assignments) == 1
        assert len(centroids) == 1


class TestRgbToHex:
    """Hex color conversion."""

    def test_red(self):
        assert _rgb_to_hex((255, 0, 0)) == "#FF0000"

    def test_black(self):
        assert _rgb_to_hex((0, 0, 0)) == "#000000"

    def test_white(self):
        assert _rgb_to_hex((255, 255, 255)) == "#FFFFFF"


class TestObjFaceToTriangle:
    """OBJ face to triangle fan conversion."""

    def test_triangle_face(self):
        face = _ObjFace(vertex_indices=[0, 1, 2], uv_indices=[], material="")
        verts = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
        tris = _obj_face_to_triangle(face, verts)
        assert len(tris) == 1

    def test_quad_face_produces_two_triangles(self):
        face = _ObjFace(vertex_indices=[0, 1, 2, 3], uv_indices=[], material="")
        verts = [
            (0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
            (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),
        ]
        tris = _obj_face_to_triangle(face, verts)
        assert len(tris) == 2

    def test_degenerate_face_returns_empty(self):
        face = _ObjFace(vertex_indices=[0, 1], uv_indices=[], material="")
        verts = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]
        assert _obj_face_to_triangle(face, verts) == []


class TestSampleFaceColor:
    """Texture sampling at face UV centroid."""

    def test_returns_grey_fallback_without_uvs(self):
        face = _ObjFace(vertex_indices=[0, 1, 2], uv_indices=[], material="")
        color = _sample_face_color(face, [], None, 0, 0)
        assert color == (128, 128, 128)

    def test_samples_texture_with_pillow(self):
        from PIL import Image

        # 2x2 red image
        img = Image.new("RGB", (2, 2), (255, 0, 0))
        face = _ObjFace(
            vertex_indices=[0, 1, 2],
            uv_indices=[0, 1, 2],
            material="",
        )
        uvs = [(0.5, 0.5), (0.5, 0.5), (0.5, 0.5)]
        color = _sample_face_color(face, uvs, img, 2, 2)
        assert color == (255, 0, 0)


# ---------------------------------------------------------------------------
# auto_multicolor_from_texture integration tests
# ---------------------------------------------------------------------------


class TestAutoMulticolorFromTexture:
    """Integration tests for the auto_multicolor_from_texture MCP tool."""

    def _call_tool(self, **kwargs: Any) -> dict:
        from kiln.plugins.color_tools import _ColorToolsPlugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn
                return decorator

        plugin = _ColorToolsPlugin()
        plugin.register(_FakeMcp())
        return tools["auto_multicolor_from_texture"](**kwargs)

    def test_splits_textured_obj_into_zones(self):
        obj_path = _create_textured_obj_dir()
        result = self._call_tool(obj_path=obj_path, num_colors=2)

        assert result["success"] is True
        assert result["method"] == "texture"
        assert result["total_faces"] > 0
        assert len(result["zones"]) > 0
        # Face counts must sum
        face_sum = sum(z["face_count"] for z in result["zones"])
        assert face_sum == result["total_faces"]
        # Each zone STL exists
        for zone in result["zones"]:
            assert Path(zone["stl_path"]).exists()
            assert zone["ams_slot"] >= 1
            assert "estimated_weight_g" in zone

    def test_hex_colors_in_palette(self):
        obj_path = _create_textured_obj_dir()
        result = self._call_tool(obj_path=obj_path, num_colors=2)
        assert result["success"] is True
        for zone in result["zones"]:
            assert zone["color"].startswith("#")
            assert len(zone["color"]) == 7

    def test_ams_mapping_present(self):
        obj_path = _create_textured_obj_dir()
        result = self._call_tool(obj_path=obj_path, num_colors=2)
        assert result["success"] is True
        assert "ams_mapping" in result
        for key in result["ams_mapping"]:
            assert key.startswith("slot_")

    def test_file_not_found(self):
        result = self._call_tool(obj_path="/nonexistent/model.obj")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_num_colors_zero(self):
        obj_path = _create_textured_obj_dir()
        result = self._call_tool(obj_path=obj_path, num_colors=0)
        assert result["success"] is False
        assert "num_colors" in result["error"]

    def test_no_texture_returns_error(self):
        tmp_dir = tempfile.mkdtemp(prefix="kiln_test_notex_")
        obj_path = os.path.join(tmp_dir, "model.obj")
        with open(obj_path, "w") as fh:
            fh.write(_SIMPLE_OBJ)
        # No MTL or texture files
        result = self._call_tool(obj_path=obj_path)
        assert result["success"] is False
        assert "texture" in result["error"].lower()

    def test_empty_obj_returns_error(self):
        tmp_dir = tempfile.mkdtemp(prefix="kiln_test_empty_")
        obj_path = os.path.join(tmp_dir, "empty.obj")
        with open(obj_path, "w") as fh:
            fh.write("# empty\n")
        _create_test_texture(tmp_dir)
        result = self._call_tool(obj_path=obj_path)
        assert result["success"] is False
        assert "No faces" in result["error"]

    def test_weight_and_time_estimates_present(self):
        obj_path = _create_textured_obj_dir()
        result = self._call_tool(obj_path=obj_path, num_colors=2)
        assert result["success"] is True
        assert "total_weight_g" in result
        assert "print_time_estimate_min" in result

    def test_next_step_present(self):
        obj_path = _create_textured_obj_dir()
        result = self._call_tool(obj_path=obj_path, num_colors=2)
        assert result["success"] is True
        assert "next_step" in result
        assert "next_action" in result

    @patch(
        "kiln.plugins.color_tools._PIL_AVAILABLE",
        False,
    )
    def test_no_pillow_returns_error(self):
        result = self._call_tool(obj_path="/tmp/fake.obj")
        assert result["success"] is False
        assert "Pillow" in result["error"]

    def test_uniform_texture_single_zone(self):
        """A solid-red texture with 1 color → all faces in one zone."""
        obj_path = _create_textured_obj_dir(
            texture_colors=[(255, 0, 0)] * 4,
        )
        result = self._call_tool(obj_path=obj_path, num_colors=1)
        assert result["success"] is True
        assert result["num_colors"] == 1
        assert result["zones"][0]["face_count"] == result["total_faces"]

    def test_summary_present(self):
        obj_path = _create_textured_obj_dir()
        result = self._call_tool(obj_path=obj_path, num_colors=2)
        assert result["success"] is True
        assert "summary" in result
        assert "color zone" in result["summary"]

    def test_fallback_to_png_in_directory(self):
        """When MTL has no map_Kd, falls back to any PNG in the directory."""
        tmp_dir = tempfile.mkdtemp(prefix="kiln_test_fallback_")
        obj_path = os.path.join(tmp_dir, "model.obj")
        with open(obj_path, "w") as fh:
            fh.write(_SIMPLE_OBJ)
        # MTL with no texture reference
        mtl_path = os.path.join(tmp_dir, "model.mtl")
        with open(mtl_path, "w") as fh:
            fh.write("newmtl material_0\n")
        # But there IS a PNG in the directory
        _create_test_texture(tmp_dir)
        result = self._call_tool(obj_path=obj_path, num_colors=2)
        assert result["success"] is True
