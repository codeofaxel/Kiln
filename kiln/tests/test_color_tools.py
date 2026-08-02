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
    - OBJ/MTL parsing, texture sampling, color quantization (helper unit tests)
    - Cut-loop capping: zones of a closed model are closed solids
      (edge-manifold zones, volume conservation, annulus and nested-ring
      caps, disjoint solids, open-input degradation, exact interfaces)
"""

from __future__ import annotations

import math
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiln.plugins.color_tools import (
    _assign_normal,
    _assign_random,
    _assign_z_height,
    _band_by_z_height,
    _band_height_warning,
    _boundary_edges,
    _chain_closed_loops,
    _estimate_weight_g,
    _is_edge_manifold,
    _obj_face_to_triangle,
    _ObjFace,
    _parse_ascii_stl,
    _parse_binary_stl,
    _parse_mtl,
    _parse_obj,
    _quantize_colors,
    _rgb_to_hex,
    _sample_face_color,
    _split_and_write,
    _split_triangle_at_plane,
    _Triangle,
    _write_binary_stl,
    plugin,
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
# Band-plane splitting — crisp boundaries for the z_height method
# ---------------------------------------------------------------------------


class TestSplitTriangleAtPlane:
    """The cutting primitive: pieces cover the input exactly, and a
    triangle that does not truly cross is returned untouched."""

    def _tall(self, z0=0.0, z1=10.0, z2=10.0):
        return _Triangle(
            normal=(1.0, 0.0, 0.0),
            v0=(0.0, 0.0, z0),
            v1=(0.0, 1.0, z1),
            v2=(0.0, 2.0, z2),
            attr=7,
        )

    def test_flat_triangle_is_untouched(self):
        tri = _make_triangle(5, 5, 5)
        assert _split_triangle_at_plane(tri, 5.0) == [tri]

    def test_touching_from_below_is_not_crossing(self):
        tri = self._tall(0.0, 5.0, 5.0)
        assert _split_triangle_at_plane(tri, 5.0) == [tri]

    def test_touching_from_above_is_not_crossing(self):
        tri = self._tall(5.0, 10.0, 10.0)
        assert _split_triangle_at_plane(tri, 5.0) == [tri]

    def test_a_crossing_triangle_splits_area_exactly(self):
        tri = self._tall()
        pieces = _split_triangle_at_plane(tri, 4.0)
        assert len(pieces) == 3  # triangle below the cut, quad above → 2
        assert abs(sum(p.area for p in pieces) - tri.area) < 1e-9
        for p in pieces:
            zs = [p.v0[2], p.v1[2], p.v2[2]]
            assert min(zs) >= -1e-9 and max(zs) <= 10.0 + 1e-9
            # no piece spans the plane
            assert max(zs) <= 4.0 + 1e-9 or min(zs) >= 4.0 - 1e-9

    def test_cut_vertices_land_exactly_on_the_plane(self):
        pieces = _split_triangle_at_plane(self._tall(), 4.0)
        on_plane = {
            v for p in pieces for v in (p.v0, p.v1, p.v2) if v[2] == 4.0
        }
        assert len(on_plane) == 2  # one shared pair, reused by both halves

    def test_normal_attr_and_winding_survive(self):
        tri = self._tall()
        for p in _split_triangle_at_plane(tri, 4.0):
            assert p.normal == tri.normal
            assert p.attr == tri.attr
            # winding: the geometric normal of every piece points the same
            # way as the parent's
            def geom_normal(t):
                ux, uy, uz = (
                    t.v1[0] - t.v0[0], t.v1[1] - t.v0[1], t.v1[2] - t.v0[2],
                )
                vx, vy, vz = (
                    t.v2[0] - t.v0[0], t.v2[1] - t.v0[1], t.v2[2] - t.v0[2],
                )
                return (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)

            gp, gt = geom_normal(p), geom_normal(tri)
            assert gp[0] * gt[0] + gp[1] * gt[1] + gp[2] * gt[2] > 0

    def test_vertex_within_epsilon_counts_as_on_the_plane(self):
        tri = self._tall(4.0 + 1e-12, 10.0, 10.0)
        assert _split_triangle_at_plane(tri, 4.0) == [tri]

    def test_adjacent_triangles_cut_watertight(self):
        """Two triangles sharing the crossing edge compute IDENTICAL cut
        points from that edge — the cut line cannot open a seam."""
        a = (0.0, 0.0, 0.0)
        b = (1.0, 0.0, 8.0)
        left = _Triangle(normal=(0, 1, 0), v0=a, v1=b, v2=(0.0, 0.0, 8.0))
        right = _Triangle(normal=(0, 1, 0), v0=b, v1=a, v2=(1.0, 0.0, 0.0))
        cuts_left = {
            v
            for p in _split_triangle_at_plane(left, 4.0)
            for v in (p.v0, p.v1, p.v2)
            if v[2] == 4.0
        }
        cuts_right = {
            v
            for p in _split_triangle_at_plane(right, 4.0)
            for v in (p.v0, p.v1, p.v2)
            if v[2] == 4.0
        }
        shared_edge_cut = (0.5, 0.0, 4.0)
        assert shared_edge_cut in cuts_left
        assert shared_edge_cut in cuts_right


class TestBandByZHeight:
    """The one door both tools route the z_height method through."""

    def _wall(self, height=40.0):
        """A rectangular wall of two triangles spanning the full height —
        the barber-pole pathology: faces TALLER than any band."""
        return [
            _Triangle(
                normal=(0.0, 1.0, 0.0),
                v0=(0.0, 0.0, 0.0),
                v1=(10.0, 0.0, 0.0),
                v2=(10.0, 0.0, height),
            ),
            _Triangle(
                normal=(0.0, 1.0, 0.0),
                v0=(0.0, 0.0, 0.0),
                v1=(10.0, 0.0, height),
                v2=(0.0, 0.0, height),
            ),
        ]

    def test_faces_taller_than_the_bands_fill_every_band(self):
        """The regression this exists for: centroid assignment on tall
        faces starved bands and striped walls; the split fills each band
        with exactly its share."""
        tris = self._wall()
        split, assigns, z_range, planes = _band_by_z_height(tris, 4)
        assert z_range == 40.0
        assert planes == [10.0, 20.0, 30.0]
        per_band_area = [0.0] * 4
        for t, a in zip(split, assigns, strict=True):
            per_band_area[a] += t.area
        total = sum(t.area for t in tris)
        for area in per_band_area:
            assert abs(area - total / 4) < 1e-6

    def test_every_piece_lies_wholly_inside_its_band(self):
        tris = self._wall()
        split, assigns, _, _ = _band_by_z_height(tris, 4)
        for t, a in zip(split, assigns, strict=True):
            lo, hi = a * 10.0, (a + 1) * 10.0
            for v in (t.v0, t.v1, t.v2):
                assert lo - 1e-9 <= v[2] <= hi + 1e-9

    def test_area_is_conserved(self):
        tris = self._wall()
        split, _, _, _ = _band_by_z_height(tris, 4)
        assert abs(sum(t.area for t in split) - sum(t.area for t in tris)) < 1e-9

    def test_band_edges_come_from_vertex_extent_not_centroids(self):
        """A leaning boundary face used to shrink the band range to the
        centroid span, shifting every edge inward; the bands divide the
        model's REAL height now."""
        tris = self._wall(height=30.0)
        split, assigns, z_range, _ = _band_by_z_height(tris, 3)
        assert z_range == 30.0
        cut_heights = sorted(
            {round(v[2], 9) for t in split for v in (t.v0, t.v1, t.v2)}
        )
        assert 10.0 in cut_heights and 20.0 in cut_heights

    def test_single_band_never_splits(self):
        tris = self._wall()
        split, assigns, _, planes = _band_by_z_height(tris, 1)
        assert split == tris
        assert assigns == [0, 0]
        assert planes == []

    def test_flat_model_never_splits(self):
        tris = [_make_triangle(5, 5, 5), _make_triangle(5, 5, 5)]
        split, assigns, z_range, planes = _band_by_z_height(tris, 4)
        assert split == tris
        assert assigns == [0, 0]
        assert z_range == 0.0
        assert planes == []

    def test_empty_input(self):
        assert _band_by_z_height([], 4) == ([], [], 0.0, [])


class TestCrispBoundariesThroughTheTools:
    """Both doors — auto_color_by_height AND auto_color_by_region's
    default method — produce zones whose meeting line is exact."""

    def _register(self):
        tools = {}

        class _FakeMcp:
            def tool(self):
                def deco(fn):
                    tools[fn.__name__] = fn
                    return fn

                return deco

        plugin.register(_FakeMcp())
        return tools

    def _wall_stl(self, height=40.0):
        tris = [
            _Triangle(
                normal=(0.0, 1.0, 0.0),
                v0=(0.0, 0.0, 0.0),
                v1=(10.0, 0.0, 0.0),
                v2=(10.0, 0.0, height),
            ),
            _Triangle(
                normal=(0.0, 1.0, 0.0),
                v0=(0.0, 0.0, 0.0),
                v1=(10.0, 0.0, height),
                v2=(0.0, 0.0, height),
            ),
        ]
        return _make_stl_file(tris)

    def _assert_exact_bands(self, result, num_colors, height):
        assert result["success"] is True
        band = height / num_colors
        face_sum = 0
        for zone in result["zones"]:
            face_sum += zone["face_count"]
            assert zone["face_count"] > 0, (
                f"zone {zone} is empty — the tall-face pathology is back"
            )
            zs = [
                v[2]
                for t in _parse_binary_stl(zone["stl_path"])
                for v in (t.v0, t.v1, t.v2)
            ]
            lo = band * zone["zone"]
            hi = lo + band
            assert min(zs) >= lo - 1e-5 and max(zs) <= hi + 1e-5, (
                f"zone {zone['zone']} bleeds past its band edges"
            )
            # the boundary is REACHED, not just respected — a crisp line
            # exists exactly at the band edge
            assert abs(min(zs) - lo) < 1e-5 and abs(max(zs) - hi) < 1e-5
        assert face_sum == result["total_faces"]

    def test_height_tool_cuts_exact_bands(self):
        tools = self._register()
        stl = self._wall_stl()
        try:
            result = tools["auto_color_by_height"](input_path=stl, num_colors=4)
            self._assert_exact_bands(result, 4, 40.0)
        finally:
            os.unlink(stl)

    def test_region_tool_z_height_method_cuts_the_same_bands(self):
        tools = self._register()
        stl = self._wall_stl()
        try:
            result = tools["auto_color_by_region"](
                input_path=stl, num_colors=4, method="z_height",
            )
            self._assert_exact_bands(result, 4, 40.0)
        finally:
            os.unlink(stl)

    def test_normal_method_never_splits_faces(self):
        """Per-facet methods are exact by construction — no cutting."""
        tools = self._register()
        stl = self._wall_stl()
        try:
            result = tools["auto_color_by_region"](
                input_path=stl, num_colors=3, method="normal",
            )
            assert result["success"] is True
            assert result["total_faces"] == 2
        finally:
            os.unlink(stl)


# ---------------------------------------------------------------------------
# Capping — zones of a closed model are themselves closed solids
# ---------------------------------------------------------------------------
#
# Closed fixtures are hand-built from _Triangle lists with consistent
# outward winding, and each test first asserts the fixture itself is
# edge-manifold — a broken fixture must fail as the fixture, not as the
# feature.


def _fixture_tri(
    p: tuple[float, float, float],
    q: tuple[float, float, float],
    r: tuple[float, float, float],
) -> _Triangle:
    """Triangle p-q-r with its geometric unit normal."""
    ux, uy, uz = q[0] - p[0], q[1] - p[1], q[2] - p[2]
    vx, vy, vz = r[0] - p[0], r[1] - p[1], r[2] - p[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return _Triangle(normal=(nx / ln, ny / ln, nz / ln), v0=p, v1=q, v2=r)


def _fixture_quad(a, b, c, d) -> list[_Triangle]:
    """Two triangles covering quad a-b-c-d (wound as given)."""
    return [_fixture_tri(a, b, c), _fixture_tri(a, c, d)]


def _closed_box(x0, y0, z0, x1, y1, z1) -> list[_Triangle]:
    """A closed box with outward winding."""
    def p(x, y, z):
        return (float(x), float(y), float(z))

    tris: list[_Triangle] = []
    tris += _fixture_quad(p(x0, y0, z0), p(x0, y1, z0), p(x1, y1, z0), p(x1, y0, z0))
    tris += _fixture_quad(p(x0, y0, z1), p(x1, y0, z1), p(x1, y1, z1), p(x0, y1, z1))
    tris += _fixture_quad(p(x0, y0, z0), p(x1, y0, z0), p(x1, y0, z1), p(x0, y0, z1))
    tris += _fixture_quad(p(x1, y0, z0), p(x1, y1, z0), p(x1, y1, z1), p(x1, y0, z1))
    tris += _fixture_quad(p(x1, y1, z0), p(x0, y1, z0), p(x0, y1, z1), p(x1, y1, z1))
    tris += _fixture_quad(p(x0, y1, z0), p(x0, y0, z0), p(x0, y0, z1), p(x0, y1, z1))
    return tris


def _hollow_tube(o0=0.0, o1=10.0, i0=3.0, i1=7.0, z0=0.0, z1=10.0) -> list[_Triangle]:
    """A closed square tube: a vertical hole all the way through."""
    def p(x, y, z):
        return (float(x), float(y), float(z))

    tris: list[_Triangle] = []
    outer_walls = (
        (p(o0, o0, z0), p(o1, o0, z0), p(o1, o0, z1), p(o0, o0, z1)),
        (p(o1, o0, z0), p(o1, o1, z0), p(o1, o1, z1), p(o1, o0, z1)),
        (p(o1, o1, z0), p(o0, o1, z0), p(o0, o1, z1), p(o1, o1, z1)),
        (p(o0, o1, z0), p(o0, o0, z0), p(o0, o0, z1), p(o0, o1, z1)),
    )
    for a, b, c, d in outer_walls:
        tris += _fixture_quad(a, b, c, d)
    inner_walls = (
        (p(i0, i0, z0), p(i1, i0, z0), p(i1, i0, z1), p(i0, i0, z1)),
        (p(i1, i0, z0), p(i1, i1, z0), p(i1, i1, z1), p(i1, i0, z1)),
        (p(i1, i1, z0), p(i0, i1, z0), p(i0, i1, z1), p(i1, i1, z1)),
        (p(i0, i1, z0), p(i0, i0, z0), p(i0, i0, z1), p(i0, i1, z1)),
    )
    for a, b, c, d in inner_walls:
        tris += _fixture_quad(d, c, b, a)  # reversed: normals face the hole
    # Flat annular rings close the top (+z) and bottom (-z).
    tris += _fixture_quad(p(o0, o0, z1), p(o1, o0, z1), p(i1, i0, z1), p(i0, i0, z1))
    tris += _fixture_quad(p(o1, o0, z1), p(o1, o1, z1), p(i1, i1, z1), p(i1, i0, z1))
    tris += _fixture_quad(p(o1, o1, z1), p(o0, o1, z1), p(i0, i1, z1), p(i1, i1, z1))
    tris += _fixture_quad(p(o0, o1, z1), p(o0, o0, z1), p(i0, i0, z1), p(i0, i1, z1))
    tris += _fixture_quad(p(i0, i0, z0), p(i1, i0, z0), p(o1, o0, z0), p(o0, o0, z0))
    tris += _fixture_quad(p(i1, i0, z0), p(i1, i1, z0), p(o1, o1, z0), p(o1, o0, z0))
    tris += _fixture_quad(p(i1, i1, z0), p(i0, i1, z0), p(o0, o1, z0), p(o1, o1, z0))
    tris += _fixture_quad(p(i0, i1, z0), p(i0, i0, z0), p(o0, o0, z0), p(o0, o1, z0))
    return tris


def _capsule_like(radius=5.0, n=8) -> list[_Triangle]:
    """An octagonal prism with pyramidal end caps — every band plane
    crosses slanted, non-axis-aligned faces."""
    ring = [
        (radius * math.cos(2 * math.pi * k / n),
         radius * math.sin(2 * math.pi * k / n))
        for k in range(n)
    ]
    z_lo, z_hi, apex_lo, apex_hi = 8.0, 22.0, 0.0, 30.0
    bot, top = (0.0, 0.0, apex_lo), (0.0, 0.0, apex_hi)
    tris: list[_Triangle] = []
    for k in range(n):
        x0, y0 = ring[k]
        x1, y1 = ring[(k + 1) % n]
        lo0, lo1 = (x0, y0, z_lo), (x1, y1, z_lo)
        hi0, hi1 = (x0, y0, z_hi), (x1, y1, z_hi)
        tris += _fixture_quad(lo0, lo1, hi1, hi0)
        tris.append(_fixture_tri(bot, lo1, lo0))
        tris.append(_fixture_tri(top, hi0, hi1))
    return tris


def _signed_volume(tris: list[_Triangle]) -> float:
    """Signed volume via the divergence theorem over signed tetrahedra."""
    v = 0.0
    for t in tris:
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = t.v0, t.v1, t.v2
        v += (
            x0 * (y1 * z2 - z1 * y2)
            - y0 * (x1 * z2 - z1 * x2)
            + z0 * (x1 * y2 - y1 * x2)
        )
    return v / 6.0


def _assert_edge_manifold(tris: list[_Triangle], label: str) -> None:
    """Every directed edge appears exactly twice — once in each direction."""
    counts: Counter = Counter()
    for t in tris:
        for a, b in ((t.v0, t.v1), (t.v1, t.v2), (t.v2, t.v0)):
            counts[(a, b)] += 1
    bad = [
        e for e, cnt in counts.items()
        if cnt != 1 or counts.get((e[1], e[0]), 0) != 1
    ]
    assert not bad, (
        f"{label}: {len(bad)} unbalanced directed edges, e.g. {bad[:3]}"
    )


def _capped_zones(tris, num_colors):
    """Route a fixture through the real z_height door: split, bucket, cap."""
    split, assigns, _, planes = _band_by_z_height(tris, num_colors)
    with tempfile.TemporaryDirectory() as td:
        zones = _split_and_write(
            split, assigns, num_colors,
            ["#FFFFFF", "#F72323", "#161616", "#898989"],
            td, "fixture", cap_planes=planes,
        )
    return zones, planes


def _cap_triangles_at(zone_tris, plane):
    """The zone's cap at one plane: triangles lying wholly at that height."""
    return [
        t for t in zone_tris
        if all(abs(v[2] - plane) <= 1e-9 for v in (t.v0, t.v1, t.v2))
    ]


class TestCutPointDirectionIndependence:
    """Adjacent faces traverse a shared edge in opposite directions; the
    cut point must come out bit-identical from both, or the cut line is
    a hairline crack no loop can close over."""

    def test_shared_edge_cut_identical_from_both_directions(self):
        # Height 9 cut at 3 → t = 1/3: naive interpolation rounds
        # differently from each end of the edge.
        a, c = (0.0, 0.0, 0.0), (10.0, 0.0, 9.0)
        left = _Triangle(normal=(0, 1, 0), v0=a, v1=c, v2=(0.0, 0.0, 9.0))
        right = _Triangle(normal=(0, 1, 0), v0=c, v1=a, v2=(10.0, 0.0, 0.0))

        def shared_edge_cuts(tri):
            return {
                v
                for p in _split_triangle_at_plane(tri, 3.0)
                for v in (p.v0, p.v1, p.v2)
                if v[2] == 3.0 and 0.0 < v[0] < 10.0
            }

        cuts_left = shared_edge_cuts(left)
        cuts_right = shared_edge_cuts(right)
        assert len(cuts_left) == 1 and cuts_left == cuts_right


class TestCapPrimitives:
    """The boundary-edge and loop-chaining building blocks."""

    def test_closed_box_has_no_boundary_edges(self):
        assert _boundary_edges(_closed_box(0, 0, 0, 4, 4, 4)) == []

    def test_missing_face_leaves_its_boundary(self):
        tris = _closed_box(0, 0, 0, 4, 4, 4)[:-1]
        assert len(_boundary_edges(tris)) == 3

    def test_open_chains_produce_no_loops(self):
        edges = [
            ((0.0, 0.0, 5.0), (1.0, 0.0, 5.0)),
            ((1.0, 0.0, 5.0), (2.0, 0.0, 5.0)),
        ]
        assert _chain_closed_loops(edges) == []

    def test_closed_ring_chains_into_one_loop(self):
        square = [
            (0.0, 0.0, 5.0), (1.0, 0.0, 5.0), (1.0, 1.0, 5.0), (0.0, 1.0, 5.0),
        ]
        edges = [
            (square[i], square[(i + 1) % 4]) for i in range(4)
        ]
        loops = _chain_closed_loops(edges)
        assert len(loops) == 1 and len(loops[0]) == 4

    def test_is_edge_manifold_verdicts(self):
        assert _is_edge_manifold(_closed_box(0, 0, 0, 4, 4, 4)) is True
        assert _is_edge_manifold(_closed_box(0, 0, 0, 4, 4, 4)[:-1]) is False
        assert _is_edge_manifold([]) is False


class TestCappedZonesAreClosedSolids:
    """Every zone of a split closed solid is itself edge-manifold, and
    the zone volumes sum to the input's — which catches orientation and
    hole errors at once."""

    def _check(self, tris, num_colors, label):
        _assert_edge_manifold(tris, f"{label} fixture")
        vol_in = _signed_volume(tris)
        zones, _ = _capped_zones(tris, num_colors)
        vol_sum = 0.0
        for zone in zones:
            if not zone.triangles:
                continue
            _assert_edge_manifold(zone.triangles, f"{label} zone {zone.index}")
            assert zone.watertight is True
            vol_sum += _signed_volume(zone.triangles)
        assert abs(vol_sum - vol_in) <= 1e-6 * abs(vol_in)

    def test_box_zones_closed_and_volume_conserved(self):
        # Height 9 over 3 bands cuts at t = 1/3 and 2/3 — the rounding-
        # hostile case, plus collinear mid-edge cut points in every loop.
        self._check(_closed_box(0, 0, 0, 10, 10, 9), 3, "box")

    def test_capsule_zones_closed_and_volume_conserved(self):
        self._check(_capsule_like(), 4, "capsule")

    def test_single_band_keeps_closed_input_closed(self):
        zones, planes = _capped_zones(_closed_box(0, 0, 0, 6, 6, 6), 1)
        assert planes == []
        assert zones[0].watertight is True


class TestAnnulusCap:
    """A hollow tube's cap is an annulus — the hole stays a hole."""

    def test_tube_zones_closed_and_volume_conserved(self):
        tube = _hollow_tube()
        _assert_edge_manifold(tube, "tube fixture")
        vol_in = _signed_volume(tube)  # 10*10*10 - 4*4*10 = 840
        assert abs(vol_in - 840.0) < 1e-9
        zones, _ = _capped_zones(tube, 2)
        vol_sum = 0.0
        for zone in zones:
            _assert_edge_manifold(zone.triangles, f"tube zone {zone.index}")
            assert zone.watertight is True
            vol_sum += _signed_volume(zone.triangles)
        assert abs(vol_sum - vol_in) <= 1e-6 * vol_in

    def test_caps_do_not_fill_the_hole(self):
        zones, planes = _capped_zones(_hollow_tube(), 2)
        assert planes == [5.0]
        for zone in zones:
            caps = _cap_triangles_at(zone.triangles, 5.0)
            assert caps, f"zone {zone.index} has no cap at the plane"
            for t in caps:
                cx, cy, _ = t.centroid
                assert not (3.0 < cx < 7.0 and 3.0 < cy < 7.0), (
                    "a cap triangle sits inside the hole — the annulus "
                    "was filled like a disk"
                )


class TestTwoTowersCap:
    """Two disjoint solids cut by one plane get two separate caps."""

    def test_towers_zones_closed_and_volume_conserved(self):
        towers = _closed_box(0, 0, 0, 4, 10, 10) + _closed_box(6, 0, 0, 10, 10, 10)
        _assert_edge_manifold(towers, "towers fixture")
        vol_in = _signed_volume(towers)
        zones, planes = _capped_zones(towers, 2)
        assert planes == [5.0]
        vol_sum = 0.0
        for zone in zones:
            _assert_edge_manifold(zone.triangles, f"towers zone {zone.index}")
            assert zone.watertight is True
            vol_sum += _signed_volume(zone.triangles)
            caps = _cap_triangles_at(zone.triangles, 5.0)
            assert caps
            for t in caps:
                cx = t.centroid[0]
                assert cx < 4.0 or cx > 6.0, (
                    "a cap triangle bridges the gap between the towers"
                )
        assert abs(vol_sum - vol_in) <= 1e-6 * vol_in


class TestNestedRingCaps:
    """Ring-in-ring-in-ring: two concentric tubes cut by one plane —
    four nested loops resolve into two annular caps per zone side."""

    def test_concentric_tubes_closed_and_volume_conserved(self):
        nested = (
            _hollow_tube(0.0, 20.0, 2.0, 18.0)
            + _hollow_tube(6.0, 14.0, 9.0, 11.0)
        )
        _assert_edge_manifold(nested, "nested fixture")
        vol_in = _signed_volume(nested)
        zones, _ = _capped_zones(nested, 2)
        vol_sum = 0.0
        for zone in zones:
            _assert_edge_manifold(zone.triangles, f"nested zone {zone.index}")
            assert zone.watertight is True
            vol_sum += _signed_volume(zone.triangles)
        assert abs(vol_sum - vol_in) <= 1e-6 * vol_in


class TestOpenInputDegradesGracefully:
    """An input that was never closed splits fine, stays open, and no
    cap is invented at boundaries the input itself had."""

    def _wall(self, height=40.0):
        return [
            _Triangle(
                normal=(0.0, 1.0, 0.0),
                v0=(0.0, 0.0, 0.0),
                v1=(10.0, 0.0, 0.0),
                v2=(10.0, 0.0, height),
            ),
            _Triangle(
                normal=(0.0, 1.0, 0.0),
                v0=(0.0, 0.0, 0.0),
                v1=(10.0, 0.0, height),
                v2=(0.0, 0.0, height),
            ),
        ]

    def test_wall_zones_stay_open_with_no_caps(self):
        split, assigns, _, planes = _band_by_z_height(self._wall(), 4)
        with tempfile.TemporaryDirectory() as td:
            zones = _split_and_write(
                split, assigns, 4, ["#FFFFFF"], td, "wall",
                cap_planes=planes,
            )
        bucket_counts = [0] * 4
        for a in assigns:
            bucket_counts[a] += 1
        for zone in zones:
            # No caps appended: face counts match the raw buckets.
            assert zone.face_count == bucket_counts[zone.index]
            assert zone.watertight is False
            for t in zone.triangles:
                assert t.v0[1] == 0.0 and t.v1[1] == 0.0 and t.v2[1] == 0.0


class TestCoincidentInterface:
    """Zone i's top cap and zone i+1's bottom cap are built from the
    same loop coordinates — the two solids meet exactly."""

    def test_cap_vertex_sets_match_across_each_plane(self):
        zones, planes = _capped_zones(_closed_box(0, 0, 0, 10, 10, 9), 3)
        assert planes == [3.0, 6.0]
        for i, plane in enumerate(planes):
            below = _cap_triangles_at(zones[i].triangles, plane)
            above = _cap_triangles_at(zones[i + 1].triangles, plane)
            assert below and above
            verts_below = {v for t in below for v in (t.v0, t.v1, t.v2)}
            verts_above = {v for t in above for v in (t.v0, t.v1, t.v2)}
            assert verts_below == verts_above

    def test_facing_caps_are_wound_opposite(self):
        def winding_z(t):
            ux, uy = t.v1[0] - t.v0[0], t.v1[1] - t.v0[1]
            vx, vy = t.v2[0] - t.v0[0], t.v2[1] - t.v0[1]
            return ux * vy - uy * vx

        zones, planes = _capped_zones(_closed_box(0, 0, 0, 10, 10, 9), 3)
        for i, plane in enumerate(planes):
            for t in _cap_triangles_at(zones[i].triangles, plane):
                assert t.normal == (0.0, 0.0, 1.0)
                assert winding_z(t) > 0  # right-hand rule: +z winding
            for t in _cap_triangles_at(zones[i + 1].triangles, plane):
                assert t.normal == (0.0, 0.0, -1.0)
                assert winding_z(t) < 0


class TestCappedThroughTheTools:
    """The tool doors deliver closed zones: STLs on disk are manifold
    and the response reports each zone watertight."""

    def _register(self):
        tools = {}

        class _FakeMcp:
            def tool(self):
                def deco(fn):
                    tools[fn.__name__] = fn
                    return fn

                return deco

        plugin.register(_FakeMcp())
        return tools

    def _box_stl(self):
        return _make_stl_file(_closed_box(0, 0, 0, 8, 8, 8))

    def test_height_tool_zone_stls_are_manifold(self):
        tools = self._register()
        stl = self._box_stl()
        try:
            result = tools["auto_color_by_height"](input_path=stl, num_colors=4)
            assert result["success"] is True
            vol_sum = 0.0
            for zone in result["zones"]:
                assert zone["watertight"] is True
                parsed = _parse_binary_stl(zone["stl_path"])
                _assert_edge_manifold(parsed, f"zone {zone['zone']} STL")
                vol_sum += _signed_volume(parsed)
            vol_in = _signed_volume(_parse_binary_stl(stl))
            assert abs(vol_sum - vol_in) <= 1e-6 * vol_in
        finally:
            os.unlink(stl)

    def test_region_tool_z_height_reports_watertight(self):
        tools = self._register()
        stl = self._box_stl()
        try:
            result = tools["auto_color_by_region"](
                input_path=stl, num_colors=2, method="z_height",
            )
            assert result["success"] is True
            assert all(z["watertight"] is True for z in result["zones"])
        finally:
            os.unlink(stl)

    def test_per_facet_methods_report_no_watertight_verdict(self):
        tools = self._register()
        stl = self._box_stl()
        try:
            result = tools["auto_color_by_region"](
                input_path=stl, num_colors=3, method="normal",
            )
            assert result["success"] is True
            assert all("watertight" not in z for z in result["zones"])
        finally:
            os.unlink(stl)
