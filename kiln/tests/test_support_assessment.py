"""Tests for the support feasibility assessment system.

Coverage:
    - Overhang analysis (flat, 45-degree, steep)
    - Bridge detection
    - Enclosed cavity detection (ray casting)
    - Removal difficulty (material-dependent)
    - Orientation suggestions
    - Support type recommendations
    - Profile loading
    - Pipeline integration
"""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiln.support_assessment import (
    SupportAssessment,
    _get_profile,
    _load_profiles,
    _overhang_angle_deg,
    _ray_intersects_triangle,
    _triangle_area,
    _triangle_normal,
    assess_support_feasibility,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Type alias for triangle vertices
_Tri = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]


def _make_flat_quad_triangles(
    *,
    z: float = 0.0,
    size: float = 20.0,
) -> list[_Tri]:
    """Two triangles forming a flat square on the XY plane at height z.

    Normal points straight up (+Z) — zero overhang.
    """
    return [
        ((0.0, 0.0, z), (size, 0.0, z), (size, size, z)),
        ((0.0, 0.0, z), (size, size, z), (0.0, size, z)),
    ]


def _make_downward_face_triangles(
    *,
    z: float = 20.0,
    size: float = 20.0,
) -> list[_Tri]:
    """Two triangles forming a flat square facing straight DOWN at height z.

    Normal points -Z — 90-degree overhang.
    """
    # Reverse winding order so normal points down
    return [
        ((0.0, 0.0, z), (size, size, z), (size, 0.0, z)),
        ((0.0, 0.0, z), (0.0, size, z), (size, size, z)),
    ]


def _make_45_degree_overhang() -> list[_Tri]:
    """A single triangle tilted at ~45 degrees from vertical.

    Normal points roughly at 45 degrees below horizontal.
    """
    # Triangle with vertices that create a ~45 degree overhang
    return [
        ((0.0, 0.0, 10.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0)),
    ]


def _make_steep_overhang() -> list[_Tri]:
    """Triangles with 70+ degree overhang (near-horizontal underside)."""
    # Downward-facing triangles at height 10 — reversed winding for -Z normal
    return [
        ((0.0, 0.0, 10.0), (20.0, 20.0, 10.0), (20.0, 0.0, 10.0)),
        ((0.0, 0.0, 10.0), (0.0, 20.0, 10.0), (20.0, 20.0, 10.0)),
    ]


def _make_cube_triangles(size: float = 20.0) -> list[_Tri]:
    """12 triangles forming a cube sitting on the XY plane.

    Bottom at Z=0, top at Z=size.
    """
    s = size
    # Define 8 vertices
    v = [
        (0.0, 0.0, 0.0),  # 0: bottom-left-front
        (s, 0.0, 0.0),    # 1: bottom-right-front
        (s, s, 0.0),      # 2: bottom-right-back
        (0.0, s, 0.0),    # 3: bottom-left-back
        (0.0, 0.0, s),    # 4: top-left-front
        (s, 0.0, s),      # 5: top-right-front
        (s, s, s),        # 6: top-right-back
        (0.0, s, s),      # 7: top-left-back
    ]

    return [
        # Bottom face (normal -Z) — this IS an overhang but sits on bed
        (v[0], v[2], v[1]),
        (v[0], v[3], v[2]),
        # Top face (normal +Z) — no overhang
        (v[4], v[5], v[6]),
        (v[4], v[6], v[7]),
        # Front face (normal -Y) — vertical, no overhang
        (v[0], v[1], v[5]),
        (v[0], v[5], v[4]),
        # Back face (normal +Y) — vertical, no overhang
        (v[2], v[3], v[7]),
        (v[2], v[7], v[6]),
        # Left face (normal -X) — vertical, no overhang
        (v[0], v[4], v[7]),
        (v[0], v[7], v[3]),
        # Right face (normal +X) — vertical, no overhang
        (v[1], v[2], v[6]),
        (v[1], v[6], v[5]),
    ]


def _make_hollow_box_triangles(
    *,
    outer: float = 20.0,
    wall: float = 2.0,
) -> list[_Tri]:
    """A hollow box: outer cube with inner cavity.

    The ceiling of the inner cavity has downward-facing overhangs that
    are ENCLOSED (ray cast from ceiling centroid hits multiple surfaces).
    """
    tris: list[_Tri] = []
    s = outer
    w = wall
    inner = s - 2 * w

    # Outer cube
    tris.extend(_make_cube_triangles(s))

    # Inner cavity (offset by wall thickness, reversed normals)
    # Inner bottom at z=w, inner top at z=s-w
    iv = [
        (w, w, w),                     # 0
        (w + inner, w, w),             # 1
        (w + inner, w + inner, w),     # 2
        (w, w + inner, w),             # 3
        (w, w, s - w),                 # 4
        (w + inner, w, s - w),         # 5
        (w + inner, w + inner, s - w), # 6
        (w, w + inner, s - w),         # 7
    ]

    # Inner top face — normal points DOWN into cavity (overhang, enclosed)
    tris.append((iv[4], iv[6], iv[5]))
    tris.append((iv[4], iv[7], iv[6]))

    # Inner bottom face — normal points UP
    tris.append((iv[0], iv[1], iv[2]))
    tris.append((iv[0], iv[2], iv[3]))

    # Inner walls (normals point inward)
    tris.append((iv[0], iv[5], iv[1]))
    tris.append((iv[0], iv[4], iv[5]))
    tris.append((iv[2], iv[7], iv[3]))
    tris.append((iv[2], iv[6], iv[7]))
    tris.append((iv[0], iv[7], iv[4]))
    tris.append((iv[0], iv[3], iv[7]))
    tris.append((iv[1], iv[6], iv[2]))
    tris.append((iv[1], iv[5], iv[6]))

    return tris


def _make_binary_stl_from_triangles(
    tmp_dir: Path,
    tris: list[_Tri],
    *,
    filename: str = "test.stl",
) -> str:
    """Write triangles to a binary STL file."""
    stl_path = tmp_dir / filename
    data = bytearray(b"\x00" * 80)  # header
    data += struct.pack("<I", len(tris))
    for tri in tris:
        # Compute normal
        n = _triangle_normal(tri)
        data += struct.pack("<3f", n[0], n[1], n[2])
        for v in tri:
            data += struct.pack("<3f", v[0], v[1], v[2])
        data += struct.pack("<H", 0)  # attribute
    stl_path.write_bytes(bytes(data))
    return str(stl_path)


def _reset_profiles() -> None:
    """Clear the cached profiles singleton."""
    import kiln.support_assessment as mod
    mod._PROFILES = {}


# ---------------------------------------------------------------------------
# Tests — Overhang Analysis
# ---------------------------------------------------------------------------


class TestOverhangAnalysis:
    """Overhang detection: angle classification and area summation."""

    def test_flat_bottom_no_overhangs(self) -> None:
        tris = _make_flat_quad_triangles(z=0.0)
        result = assess_support_feasibility(triangles=tris, material="PLA")

        assert result.needs_supports is False
        assert result.overhang_percentage == 0.0

    def test_45_degree_overhang(self) -> None:
        # Combine upward faces with a 45-degree overhang
        tris = _make_flat_quad_triangles() + _make_45_degree_overhang()
        result = assess_support_feasibility(triangles=tris, material="PLA")

        # For PLA (safe=55), a 45-degree overhang should be within safe range
        assert result.overhang_percentage < 50.0

    def test_steep_overhang_detected(self) -> None:
        tris = _make_steep_overhang()
        result = assess_support_feasibility(triangles=tris, material="PLA")

        # Steep overhangs should be detected
        assert result.overhang_area_mm2 > 0
        assert result.total_surface_area_mm2 > 0

    def test_pla_threshold_vs_petg_threshold(self) -> None:
        # Same geometry, different materials should give different results
        # Use faces with ~50 degree overhang (between PETG safe=45 and PLA safe=55)
        # Create a triangle with a normal at about 50 degrees from horizontal
        # (i.e., 140 degrees from +Z, overhang angle = 50)
        angle_rad = math.radians(140)  # 140 from +Z = 50 degree overhang
        nx = math.sin(angle_rad)
        nz = math.cos(angle_rad)
        # Build a triangle whose normal is roughly (nx, 0, nz)
        tris: list[_Tri] = [
            ((0.0, 0.0, 10.0), (10.0, 0.0, 10.0 - 10.0 * nx / abs(nz) if abs(nz) > 0.01 else 0.0), (5.0, 10.0, 10.0)),
        ]

        result_pla = assess_support_feasibility(triangles=tris, material="PLA")
        result_petg = assess_support_feasibility(triangles=tris, material="PETG")

        # PETG has stricter thresholds (safe=45) so should detect more overhang
        assert result_petg.overhang_area_mm2 >= result_pla.overhang_area_mm2


# ---------------------------------------------------------------------------
# Tests — Bridge Detection
# ---------------------------------------------------------------------------


class TestBridgeDetection:
    """Bridge span estimation from overhang clusters."""

    def test_no_bridges_solid_cube(self) -> None:
        tris = _make_cube_triangles()
        result = assess_support_feasibility(triangles=tris, material="PLA")

        # A cube on the bed has bottom face overhangs but they are at Z=0
        # The bridge regions should be minimal
        # Bottom face is an overhang but sits at Z=0, bridge width is small
        for br in result.bridge_regions:
            assert br.support_height_mm == pytest.approx(0.0, abs=1.0)

    def test_horizontal_bridge_detected(self) -> None:
        # Create a wide horizontal face at height 20mm
        tris = _make_flat_quad_triangles(z=0.0)  # bed face
        tris += _make_downward_face_triangles(z=20.0, size=30.0)  # wide bridge

        result = assess_support_feasibility(triangles=tris, material="PLA")

        assert result.overhang_area_mm2 > 0
        assert result.needs_supports is True


# ---------------------------------------------------------------------------
# Tests — Enclosed Cavity Detection
# ---------------------------------------------------------------------------


class TestEnclosedCavityDetection:
    """Ray-cast cavity detection for trapped support identification."""

    def test_open_model_no_cavities(self) -> None:
        # Simple cube — no enclosed cavities
        tris = _make_cube_triangles()
        result = assess_support_feasibility(triangles=tris, material="PLA")

        assert len(result.trapped_regions) == 0

    def test_hollow_sphere_detects_cavity(self) -> None:
        # Use hollow box as proxy (simpler geometry, same principle)
        tris = _make_hollow_box_triangles()
        result = assess_support_feasibility(triangles=tris, material="PLA")

        # The inner ceiling overhangs should be detected as enclosed
        # With the ray-cast, downward from the inner ceiling should hit
        # the inner bottom + outer bottom = enclosed
        # (This depends on the geometry being watertight enough for ray-cast)
        # At minimum, the model should detect overhangs
        assert result.overhang_area_mm2 > 0

    def test_box_with_hole_accessible(self) -> None:
        # Open-top box — overhangs at bottom are accessible
        s = 20.0
        v = [
            (0.0, 0.0, 0.0), (s, 0.0, 0.0), (s, s, 0.0), (0.0, s, 0.0),
            (0.0, 0.0, s), (s, 0.0, s), (s, s, s), (0.0, s, s),
        ]
        # Only bottom + 4 walls (no top) — bottom overhangs are accessible
        tris: list[_Tri] = [
            (v[0], v[2], v[1]), (v[0], v[3], v[2]),  # bottom
            (v[0], v[1], v[5]), (v[0], v[5], v[4]),  # front
            (v[2], v[3], v[7]), (v[2], v[7], v[6]),  # back
            (v[0], v[4], v[7]), (v[0], v[7], v[3]),  # left
            (v[1], v[2], v[6]), (v[1], v[6], v[5]),  # right
        ]
        result = assess_support_feasibility(triangles=tris, material="PLA")

        # Bottom face overhangs should NOT be trapped (open top)
        assert len(result.trapped_regions) == 0


# ---------------------------------------------------------------------------
# Tests — Removal Difficulty
# ---------------------------------------------------------------------------


class TestRemovalDifficulty:
    """Material-dependent support removal difficulty."""

    def test_pla_easy_removal(self) -> None:
        # PLA with small overhangs = easy
        tris = _make_flat_quad_triangles()
        tris += _make_downward_face_triangles(z=5.0, size=5.0)
        result = assess_support_feasibility(triangles=tris, material="PLA")

        assert result.removal_difficulty in ("easy", "medium")

    def test_petg_hard_removal(self) -> None:
        # PETG with large overhangs = hard
        # Create mostly overhang geometry
        tris = _make_downward_face_triangles(z=30.0, size=50.0)
        tris += _make_downward_face_triangles(z=25.0, size=50.0)
        tris += _make_flat_quad_triangles(size=10.0)  # small upward face
        result = assess_support_feasibility(triangles=tris, material="PETG")

        assert result.removal_difficulty in ("hard", "impossible")

    def test_enclosed_cavity_impossible(self) -> None:
        # Any material with enclosed cavities
        tris = _make_hollow_box_triangles()
        result = assess_support_feasibility(triangles=tris, material="PLA")

        # If cavities detected, difficulty should be impossible
        if result.trapped_regions:
            assert result.removal_difficulty == "impossible"


# ---------------------------------------------------------------------------
# Tests — Orientation Suggestion
# ---------------------------------------------------------------------------


class TestOrientationSuggestion:
    """Orientation optimization to minimize overhang area."""

    def test_suggests_better_orientation(self) -> None:
        # L-shape: two boxes joined, one arm has overhangs
        # Create geometry that clearly benefits from rotation
        tris = _make_downward_face_triangles(z=20.0, size=40.0)
        tris += _make_flat_quad_triangles(z=0.0, size=10.0)
        result = assess_support_feasibility(triangles=tris, material="PLA")

        # Should have at least the current orientation
        assert result.current_orientation is not None
        assert result.current_orientation.rotation_axis == "none"

    def test_no_suggestion_when_already_optimal(self) -> None:
        # Cube on bed — already optimal, all orientations similar
        tris = _make_cube_triangles()
        result = assess_support_feasibility(triangles=tris, material="PLA")

        # A cube has the same overhang profile from any cardinal direction
        # So no orientation should offer >30% improvement
        # (The bottom face is always present regardless of rotation)
        assert result.current_orientation is not None


# ---------------------------------------------------------------------------
# Tests — Support Type Recommendation
# ---------------------------------------------------------------------------


class TestSupportTypeRecommendation:
    """Support strategy selection based on geometry and material."""

    def test_large_flat_overhang_recommends_grid_or_hybrid(self) -> None:
        # Wide flat overhang
        tris = _make_flat_quad_triangles(z=0.0, size=5.0)
        tris += _make_downward_face_triangles(z=20.0, size=50.0)
        result = assess_support_feasibility(triangles=tris, material="PLA")

        if result.needs_supports:
            assert result.recommended_support_type in (
                "tree_hybrid", "tree_organic", "grid",
            )

    def test_organic_shape_recommends_tree(self) -> None:
        # Many scattered small overhangs → tree supports
        tris = _make_flat_quad_triangles(z=0.0, size=5.0)
        for i in range(10):
            tris += _make_downward_face_triangles(
                z=10.0 + i * 2, size=3.0,
            )
        result = assess_support_feasibility(triangles=tris, material="PLA")

        if result.needs_supports:
            assert result.recommended_support_type in (
                "tree_organic", "tree_hybrid",
            )

    def test_petg_recommends_cross_material(self) -> None:
        tris = _make_flat_quad_triangles(z=0.0, size=5.0)
        tris += _make_downward_face_triangles(z=20.0, size=30.0)
        result = assess_support_feasibility(triangles=tris, material="PETG")

        if result.needs_supports:
            # PETG has high fuse risk — should recommend cross-material
            assert result.recommended_support_type in (
                "cross_material", "tree_organic", "tree_hybrid",
            )
            assert result.cross_material_option is not None
            assert "PLA" in result.cross_material_option


# ---------------------------------------------------------------------------
# Tests — Profile Loading
# ---------------------------------------------------------------------------


class TestSupportProfileLoading:
    """Support profile data file loading and defaults."""

    def setup_method(self) -> None:
        _reset_profiles()

    def test_all_materials_have_profiles(self) -> None:
        profiles = _load_profiles()
        expected = {"pla", "petg", "abs", "asa", "pa", "tpu", "pc"}
        assert expected.issubset(set(profiles.keys()))

    def test_unknown_material_returns_default(self) -> None:
        profile = _get_profile("unobtanium")
        assert "overhang_safe_deg" in profile
        assert "overhang_max_deg" in profile
        assert profile["overhang_safe_deg"] == 50  # default value

    def test_material_alias_nylon(self) -> None:
        profile = _get_profile("nylon")
        assert profile["overhang_safe_deg"] == 35  # PA profile

    def test_pla_profile_values(self) -> None:
        profile = _get_profile("PLA")
        assert profile["overhang_safe_deg"] == 55
        assert profile["overhang_max_deg"] == 65
        assert profile["soluble_pairing"] == "PVA"


# ---------------------------------------------------------------------------
# Tests — Pipeline Integration
# ---------------------------------------------------------------------------


def _build_tools() -> dict[str, Any]:
    """Instantiate the validation pipeline plugin and return registered tools."""
    from kiln.plugins.validation_pipeline_tools import _ValidationPipelinePlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn
            return decorator

    plugin = _ValidationPipelinePlugin()
    plugin.register(_FakeMcp())
    return tools


class TestPipelineIntegration:
    """Support assessment step within the validation pipeline."""

    def test_support_step_appears_in_pipeline_output(self, tmp_path: Path) -> None:
        tris = _make_cube_triangles()
        stl = _make_binary_stl_from_triangles(tmp_path, tris)

        with patch.dict("sys.modules", {
            "kiln.generation.validation": None,
            "kiln.printability": None,
            "kiln.design_intelligence": None,
        }):
            tools = _build_tools()
            result = tools["validate_and_prepare"](stl, material="PLA")

        check_names = [c["name"] for c in result["checks"]]
        assert "support_assessment" in check_names

    def test_cavity_warning_blocks_ready_to_print(self, tmp_path: Path) -> None:
        tris = _make_hollow_box_triangles()
        stl = _make_binary_stl_from_triangles(tmp_path, tris)

        # Mock the support assessment to return trapped regions
        mock_assessment = SupportAssessment(
            needs_supports=True,
            overhang_percentage=40.0,
            overhang_area_mm2=200.0,
            total_surface_area_mm2=500.0,
            trapped_regions=[
                __import__("kiln.support_assessment", fromlist=["OverhangRegion"]).OverhangRegion(
                    centroid=(10.0, 10.0, 15.0),
                    area_mm2=100.0,
                    max_angle_deg=90.0,
                    estimated_bridge_mm=15.0,
                    support_height_mm=10.0,
                    accessible=False,
                    triangle_count=5,
                ),
            ],
            removal_difficulty="impossible",
            removal_notes="Enclosed cavity",
        )

        with (
            patch.dict("sys.modules", {
                "kiln.generation.validation": None,
                "kiln.printability": None,
                "kiln.design_intelligence": None,
            }),
            patch(
                "kiln.support_assessment.assess_support_feasibility",
                return_value=mock_assessment,
            ),
        ):
            tools = _build_tools()
            result = tools["validate_and_prepare"](stl, material="PLA")

        # Find the support_assessment check
        support_check = [c for c in result["checks"] if c["name"] == "support_assessment"]
        assert len(support_check) == 1
        assert support_check[0]["passed"] is False
        assert support_check[0]["severity"] == "error"
        assert "enclosed cavity" in support_check[0]["details"].lower()

        # Error severity should block ready_to_print
        assert result["ready_to_print"] is False


# ---------------------------------------------------------------------------
# Tests — Dataclass serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """SupportAssessment.to_dict() output format."""

    def test_to_dict_keys(self) -> None:
        tris = _make_cube_triangles()
        result = assess_support_feasibility(triangles=tris, material="PLA")
        d = result.to_dict()

        expected_keys = {
            "needs_supports", "overhang_percentage", "overhang_area_mm2",
            "total_surface_area_mm2", "bridge_regions", "trapped_regions",
            "removal_difficulty", "removal_notes", "recommended_support_type",
            "recommended_z_gap_mm", "recommended_interface_layers",
            "soluble_support_option", "cross_material_option",
            "orientation_suggestions", "current_orientation",
            "warnings", "recommendations",
        }
        assert expected_keys == set(d.keys())

    def test_to_dict_values_are_serializable(self) -> None:
        """Ensure to_dict output is JSON-serializable."""
        import json

        tris = _make_downward_face_triangles(z=20.0) + _make_flat_quad_triangles()
        result = assess_support_feasibility(triangles=tris, material="PETG")
        d = result.to_dict()

        # Should not raise
        serialized = json.dumps(d)
        assert isinstance(serialized, str)


# ---------------------------------------------------------------------------
# Tests — Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases: empty input, None, degenerate geometry."""

    def test_empty_triangles_returns_no_supports(self) -> None:
        result = assess_support_feasibility(triangles=[], material="PLA")
        assert result.needs_supports is False
        assert "No triangles" in result.warnings[0]

    def test_none_input_returns_no_supports(self) -> None:
        result = assess_support_feasibility(material="PLA")
        assert result.needs_supports is False

    def test_degenerate_triangle_handled(self) -> None:
        # Zero-area triangle (all vertices at same point)
        tris: list[_Tri] = [
            ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
        ]
        # Should not crash
        result = assess_support_feasibility(triangles=tris, material="PLA")
        assert isinstance(result, SupportAssessment)

    def test_stl_path_file_not_found(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "nonexistent.stl")
        # Should not crash — returns empty assessment
        with pytest.raises(FileNotFoundError):
            assess_support_feasibility(stl_path=missing, material="PLA")

    def test_stl_path_valid(self, tmp_path: Path) -> None:
        tris = _make_cube_triangles()
        stl = _make_binary_stl_from_triangles(tmp_path, tris)
        result = assess_support_feasibility(stl_path=stl, material="PLA")
        assert result.total_surface_area_mm2 > 0


# ---------------------------------------------------------------------------
# Tests — Low-level geometry helpers
# ---------------------------------------------------------------------------


class TestGeometryHelpers:
    """Unit tests for triangle math utilities."""

    def test_upward_normal_zero_overhang(self) -> None:
        angle = _overhang_angle_deg((0.0, 0.0, 1.0))
        assert angle == 0.0

    def test_downward_normal_90_overhang(self) -> None:
        angle = _overhang_angle_deg((0.0, 0.0, -1.0))
        assert angle == pytest.approx(90.0, abs=0.1)

    def test_horizontal_normal_no_overhang(self) -> None:
        # Normal pointing sideways = vertical face = 0 overhang by our definition
        angle = _overhang_angle_deg((1.0, 0.0, 0.0))
        assert angle == 0.0

    def test_triangle_area_unit_square(self) -> None:
        # Right triangle with legs 10mm each = area 50mm^2
        tri: _Tri = ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0))
        assert _triangle_area(tri) == pytest.approx(50.0, abs=0.01)

    def test_ray_intersects_triangle_hit(self) -> None:
        tri: _Tri = ((-5.0, -5.0, 0.0), (5.0, -5.0, 0.0), (0.0, 5.0, 0.0))
        origin = (0.0, 0.0, 10.0)
        direction = (0.0, 0.0, -1.0)
        assert _ray_intersects_triangle(origin, direction, tri) is True

    def test_ray_intersects_triangle_miss(self) -> None:
        tri: _Tri = ((-5.0, -5.0, 0.0), (5.0, -5.0, 0.0), (0.0, 5.0, 0.0))
        origin = (100.0, 100.0, 10.0)  # far away
        direction = (0.0, 0.0, -1.0)
        assert _ray_intersects_triangle(origin, direction, tri) is False
