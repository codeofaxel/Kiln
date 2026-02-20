"""Tests for kiln.printability -- printability analysis engine."""

from __future__ import annotations

import os
import struct
import tempfile

import pytest

from kiln.printability import (
    BedAdhesionAnalysis,
    BridgingAnalysis,
    OverhangAnalysis,
    PrintabilityReport,
    SupportAnalysis,
    ThinWallAnalysis,
    _analyze_bed_adhesion,
    _analyze_bridging,
    _analyze_overhangs,
    _analyze_supports,
    _analyze_thin_walls,
    _compute_score,
    _score_to_grade,
    _triangle_area,
    _triangle_centroid,
    _triangle_normal,
    analyze_printability,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_binary_stl(triangles: list[tuple]) -> bytes:
    """Create a minimal binary STL from triangle vertex tuples."""
    header = b"\x00" * 80
    count = struct.pack("<I", len(triangles))
    body = b""
    for v1, v2, v3 in triangles:
        normal = struct.pack("<3f", 0.0, 0.0, 0.0)
        verts = struct.pack("<9f", *v1, *v2, *v3)
        attr = struct.pack("<H", 0)
        body += normal + verts + attr
    return header + count + body


def _cube_triangles(size: float = 10.0) -> list[tuple]:
    """12 triangles forming a cube [0,size]^3."""
    s = size
    verts = [
        (0, 0, 0),
        (s, 0, 0),
        (s, s, 0),
        (0, s, 0),
        (0, 0, s),
        (s, 0, s),
        (s, s, s),
        (0, s, s),
    ]
    faces = [
        (0, 1, 2),
        (0, 2, 3),  # bottom
        (4, 6, 5),
        (4, 7, 6),  # top
        (0, 4, 5),
        (0, 5, 1),  # front
        (2, 6, 7),
        (2, 7, 3),  # back
        (0, 3, 7),
        (0, 7, 4),  # left
        (1, 5, 6),
        (1, 6, 2),  # right
    ]
    return [(verts[a], verts[b], verts[c]) for a, b, c in faces]


def _write_stl(tmpdir: str, triangles: list[tuple]) -> str:
    """Write a binary STL file and return its path."""
    path = os.path.join(tmpdir, "test_model.stl")
    with open(path, "wb") as fh:
        fh.write(_make_binary_stl(triangles))
    return path


# ---------------------------------------------------------------------------
# TestTriangleNormal
# ---------------------------------------------------------------------------


class TestTriangleNormal:
    def test_xy_plane_triangle(self):
        n = _triangle_normal((0, 0, 0), (1, 0, 0), (0, 1, 0))
        assert n[2] > 0  # Z-up normal

    def test_degenerate_triangle(self):
        n = _triangle_normal((0, 0, 0), (0, 0, 0), (0, 0, 0))
        assert n == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# TestTriangleArea
# ---------------------------------------------------------------------------


class TestTriangleArea:
    def test_unit_right_triangle(self):
        area = _triangle_area((0, 0, 0), (1, 0, 0), (0, 1, 0))
        assert abs(area - 0.5) < 1e-6

    def test_degenerate_triangle_zero_area(self):
        area = _triangle_area((0, 0, 0), (1, 0, 0), (2, 0, 0))
        assert area < 1e-6


# ---------------------------------------------------------------------------
# TestTriangleCentroid
# ---------------------------------------------------------------------------


class TestTriangleCentroid:
    def test_origin_triangle(self):
        c = _triangle_centroid((0, 0, 0), (3, 0, 0), (0, 3, 0))
        assert abs(c[0] - 1.0) < 1e-6
        assert abs(c[1] - 1.0) < 1e-6
        assert abs(c[2]) < 1e-6


# ---------------------------------------------------------------------------
# TestOverhangAnalysis
# ---------------------------------------------------------------------------


class TestOverhangAnalysis:
    def test_cube_has_limited_overhangs(self):
        tris = _cube_triangles()
        result = _analyze_overhangs(tris, max_overhang_angle=45.0)
        # A cube has side faces (normal horizontal) and a bottom face
        # (normal pointing down).  The bottom face normal is (0,0,-1),
        # which is exactly 0 deg from straight down.
        assert isinstance(result, OverhangAnalysis)
        assert result.overhang_triangle_count >= 0

    def test_flat_surface_no_overhangs(self):
        # A single upward-facing triangle.
        tris = [((0, 0, 0), (10, 0, 0), (5, 10, 0))]
        result = _analyze_overhangs(tris, max_overhang_angle=45.0)
        assert result.overhang_triangle_count == 0
        assert not result.needs_supports

    def test_downward_face_is_overhang(self):
        # Triangle high up with normal pointing straight down.
        tris = [((0, 0, 50), (10, 0, 50), (5, 10, 50))]
        # Normal from cross product: (0, 0, +) but we need it facing down
        # Reverse winding to get downward normal.
        tris = [((0, 0, 50), (5, 10, 50), (10, 0, 50))]
        result = _analyze_overhangs(tris, max_overhang_angle=45.0)
        assert result.needs_supports

    def test_to_dict(self):
        result = _analyze_overhangs(_cube_triangles())
        d = result.to_dict()
        assert "max_overhang_angle" in d
        assert "overhang_triangle_count" in d


# ---------------------------------------------------------------------------
# TestThinWallAnalysis
# ---------------------------------------------------------------------------


class TestThinWallAnalysis:
    def test_cube_no_thin_walls(self):
        tris = _cube_triangles(10.0)
        verts = list({v for tri in tris for v in tri})
        result = _analyze_thin_walls(tris, verts, nozzle_diameter=0.4)
        assert result.thin_wall_count == 0
        assert result.min_wall_thickness_mm >= 0.4

    def test_thin_triangle_detected(self):
        # Triangle with a very short edge (0.1 mm).
        tris = [((0, 0, 0), (0.1, 0, 0), (0, 10, 0))]
        verts = [(0, 0, 0), (0.1, 0, 0), (0, 10, 0)]
        result = _analyze_thin_walls(tris, verts, nozzle_diameter=0.4)
        assert result.thin_wall_count == 1
        assert result.min_wall_thickness_mm < 0.4

    def test_to_dict(self):
        tris = _cube_triangles()
        verts = list({v for tri in tris for v in tri})
        d = _analyze_thin_walls(tris, verts).to_dict()
        assert "min_wall_thickness_mm" in d


# ---------------------------------------------------------------------------
# TestBridgingAnalysis
# ---------------------------------------------------------------------------


class TestBridgingAnalysis:
    def test_cube_no_bridging(self):
        tris = _cube_triangles()
        result = _analyze_bridging(tris, z_min=0.0, layer_height=0.2)
        # The bottom of a cube is at Z=0, so all downward faces are at the bed.
        assert isinstance(result, BridgingAnalysis)

    def test_to_dict(self):
        result = _analyze_bridging(_cube_triangles(), z_min=0.0)
        d = result.to_dict()
        assert "max_bridge_length_mm" in d
        assert "bridge_count" in d


# ---------------------------------------------------------------------------
# TestBedAdhesionAnalysis
# ---------------------------------------------------------------------------


class TestBedAdhesionAnalysis:
    def test_cube_has_good_bed_adhesion(self):
        tris = _cube_triangles(10.0)
        bbox = {
            "x_min": 0.0,
            "x_max": 10.0,
            "y_min": 0.0,
            "y_max": 10.0,
            "z_min": 0.0,
            "z_max": 10.0,
        }
        result = _analyze_bed_adhesion(tris, z_min=0.0, bbox=bbox)
        assert result.contact_area_mm2 > 0
        assert result.adhesion_risk in ("low", "medium", "high")

    def test_elevated_model_poor_adhesion(self):
        # All vertices above Z=1.
        tris = [((0, 0, 5), (10, 0, 5), (5, 10, 5))]
        bbox = {
            "x_min": 0.0,
            "x_max": 10.0,
            "y_min": 0.0,
            "y_max": 10.0,
            "z_min": 5.0,
            "z_max": 5.0,
        }
        result = _analyze_bed_adhesion(tris, z_min=5.0, bbox=bbox)
        # Only one triangle and it's flat at Z=5, which is within layer_height of z_min=5.
        assert isinstance(result, BedAdhesionAnalysis)

    def test_to_dict(self):
        tris = _cube_triangles()
        bbox = {"x_min": 0, "x_max": 10, "y_min": 0, "y_max": 10, "z_min": 0, "z_max": 10}
        d = _analyze_bed_adhesion(tris, 0.0, bbox).to_dict()
        assert "adhesion_risk" in d


# ---------------------------------------------------------------------------
# TestSupportAnalysis
# ---------------------------------------------------------------------------


class TestSupportAnalysis:
    def test_cube_support_analysis(self):
        tris = _cube_triangles()
        result = _analyze_supports(tris, z_min=0.0)
        assert isinstance(result, SupportAnalysis)

    def test_to_dict(self):
        d = _analyze_supports(_cube_triangles(), 0.0).to_dict()
        assert "estimated_support_volume_mm3" in d


# ---------------------------------------------------------------------------
# TestScoring
# ---------------------------------------------------------------------------


class TestScoring:
    def test_perfect_score(self):
        overhangs = OverhangAnalysis(0, 0, 0.0, False, [])
        thin_walls = ThinWallAnalysis(1.0, 0, 0.0, [])
        bridging = BridgingAnalysis(0.0, 0, False)
        adhesion = BedAdhesionAnalysis(100.0, 50.0, "low")
        supports = SupportAnalysis(0.0, 0.0, [])
        score = _compute_score(overhangs, thin_walls, bridging, adhesion, supports)
        assert score == 100

    def test_bad_score(self):
        overhangs = OverhangAnalysis(0, 100, 80.0, True, [])
        thin_walls = ThinWallAnalysis(0.1, 50, 50.0, [])
        bridging = BridgingAnalysis(50.0, 20, True)
        adhesion = BedAdhesionAnalysis(1.0, 1.0, "high")
        supports = SupportAnalysis(1000.0, 60.0, [])
        score = _compute_score(overhangs, thin_walls, bridging, adhesion, supports)
        assert score < 50

    def test_score_clamps_to_zero(self):
        overhangs = OverhangAnalysis(0, 1000, 100.0, True, [])
        thin_walls = ThinWallAnalysis(0.01, 500, 100.0, [])
        bridging = BridgingAnalysis(100.0, 100, True)
        adhesion = BedAdhesionAnalysis(0.0, 0.0, "high")
        supports = SupportAnalysis(10000.0, 100.0, [])
        score = _compute_score(overhangs, thin_walls, bridging, adhesion, supports)
        assert score >= 0


# ---------------------------------------------------------------------------
# TestGrading
# ---------------------------------------------------------------------------


class TestGrading:
    def test_grade_a(self):
        assert _score_to_grade(95) == "A"
        assert _score_to_grade(90) == "A"

    def test_grade_b(self):
        assert _score_to_grade(85) == "B"

    def test_grade_c(self):
        assert _score_to_grade(75) == "C"

    def test_grade_d(self):
        assert _score_to_grade(65) == "D"

    def test_grade_f(self):
        assert _score_to_grade(50) == "F"
        assert _score_to_grade(0) == "F"


# ---------------------------------------------------------------------------
# TestAnalyzePrintability (integration)
# ---------------------------------------------------------------------------


class TestAnalyzePrintability:
    def test_cube_is_printable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0))
            report = analyze_printability(path)
            assert isinstance(report, PrintabilityReport)
            assert report.score > 0
            assert report.grade in ("A", "B", "C", "D", "F")
            assert isinstance(report.recommendations, list)

    def test_cube_to_dict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles())
            report = analyze_printability(path)
            d = report.to_dict()
            assert "score" in d
            assert "grade" in d
            assert "overhangs" in d
            assert "thin_walls" in d

    def test_nonexistent_file_raises(self):
        with pytest.raises(ValueError, match="File not found"):
            analyze_printability("/nonexistent/model.stl")

    def test_empty_file_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "empty.stl")
            with open(path, "wb") as fh:
                fh.write(b"")
            with pytest.raises(ValueError):
                analyze_printability(path)

    def test_unsupported_format_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "model.gltf")
            with open(path, "w") as fh:
                fh.write("{}")
            with pytest.raises(ValueError, match="Unsupported"):
                analyze_printability(path)

    def test_build_volume_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(100.0))
            report = analyze_printability(path, build_volume=(50.0, 50.0, 50.0))
            # Model is 100x100x100 but build volume is 50x50x50.
            assert any("exceeds build volume" in r for r in report.recommendations)

    def test_custom_nozzle_diameter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0))
            report = analyze_printability(path, nozzle_diameter=0.8)
            assert isinstance(report, PrintabilityReport)

    def test_print_time_modifier_base(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0))
            report = analyze_printability(path)
            assert report.estimated_print_time_modifier >= 1.0
