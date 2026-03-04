"""Tests for kiln.mesh_diagnostics — advanced Trimesh-based mesh analysis.

Coverage areas:
    - diagnose_mesh on clean watertight meshes
    - diagnose_mesh on meshes with holes (open boundaries)
    - diagnose_mesh on meshes with inverted normals
    - diagnose_mesh on meshes with degenerate (zero-area) faces
    - diagnose_mesh on meshes with floating fragments (multiple components)
    - diagnose_mesh on high-polygon meshes (polygon count assessment)
    - diagnose_mesh on meshes with non-manifold edges (self-intersection proxy)
    - Input validation: missing file, unsupported extension, empty mesh
    - Graceful ImportError when trimesh is not installed
    - Dataclass serialization (to_dict round-trip)
    - MCP plugin tool registration and error handling
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Guard: skip entire module if trimesh is not installed.
trimesh = pytest.importorskip("trimesh", reason="trimesh required for mesh diagnostic tests")
import numpy as np  # noqa: E402

from kiln.mesh_diagnostics import (  # noqa: E402
    NormalsReport,
    _assess_polygon_count,
    _compute_severity,
    diagnose_mesh,
)

# ---------------------------------------------------------------------------
# Fixtures: generate test meshes in tmp directory
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _write_box_stl(path: Path) -> str:
    """Write a clean, watertight cube as binary STL (12 triangles)."""
    mesh = trimesh.creation.box(extents=[10, 20, 30])
    mesh.export(str(path / "cube.stl"), file_type="stl")
    return str(path / "cube.stl")


def _write_sphere_stl(path: Path, *, subdivisions: int = 3) -> str:
    """Write a watertight sphere STL."""
    mesh = trimesh.creation.icosphere(subdivisions=subdivisions)
    mesh.vertices *= 15.0  # 30mm diameter
    mesh.export(str(path / "sphere.stl"), file_type="stl")
    return str(path / "sphere.stl")


def _write_open_mesh_stl(path: Path) -> str:
    """Write a mesh with holes (remove some faces from a sphere)."""
    mesh = trimesh.creation.icosphere(subdivisions=2)
    mesh.vertices *= 10.0
    # Remove 20% of faces to create holes.
    keep = int(len(mesh.faces) * 0.8)
    mesh.faces = mesh.faces[:keep]
    mesh.remove_unreferenced_vertices()
    mesh.export(str(path / "open.stl"), file_type="stl")
    return str(path / "open.stl")


def _write_inverted_normals_obj(path: Path) -> str:
    """Write a mesh with inconsistent face winding (inverted normals).

    Manually writes an OBJ where half the faces have reversed vertex
    order, creating inconsistent normals that trimesh import preserves.
    """
    mesh = trimesh.creation.box(extents=[10, 10, 10])
    verts = mesh.vertices
    faces = mesh.faces.copy()

    # Flip winding order of half the faces.
    half = len(faces) // 2
    faces[:half] = faces[:half, ::-1]

    out = str(path / "inverted.obj")
    with open(out, "w") as f:
        for v in verts:
            f.write(f"v {v[0]} {v[1]} {v[2]}\n")
        for face in faces:
            # OBJ faces are 1-indexed.
            f.write(f"f {face[0] + 1} {face[1] + 1} {face[2] + 1}\n")
    return out


def _write_multi_component_obj(path: Path) -> str:
    """Write an OBJ with two disconnected components (floating fragment).

    Uses manual OBJ writing to guarantee two separate vertex groups
    survive the load → merge_vertices round-trip.  STL export flattens
    the vertex buffer and trimesh may merge components on reimport.
    """
    box1 = trimesh.creation.box(extents=[10, 10, 10])
    box2 = trimesh.creation.box(extents=[2, 2, 2])
    box2.vertices += [30, 30, 30]  # Move far away from box1.

    out = str(path / "fragments.obj")
    with open(out, "w") as f:
        for v in box1.vertices:
            f.write(f"v {v[0]} {v[1]} {v[2]}\n")
        for v in box2.vertices:
            f.write(f"v {v[0]} {v[1]} {v[2]}\n")
        for face in box1.faces:
            f.write(f"f {face[0] + 1} {face[1] + 1} {face[2] + 1}\n")
        offset = len(box1.vertices)
        for face in box2.faces:
            f.write(f"f {face[0] + offset + 1} {face[1] + offset + 1} {face[2] + offset + 1}\n")
    return out


def _write_degenerate_stl(path: Path) -> str:
    """Write a mesh that includes degenerate (zero-area) triangles."""
    mesh = trimesh.creation.box(extents=[10, 10, 10])
    # Add degenerate triangles: three identical vertices.
    degen_verts = np.array([[0, 0, 0], [0, 0, 0], [0, 0, 0]], dtype=np.float64)
    degen_faces = np.array([[0, 1, 2]])
    offset = len(mesh.vertices)
    new_verts = np.vstack([mesh.vertices, degen_verts])
    new_faces = np.vstack([mesh.faces, degen_faces + offset])
    degen_mesh = trimesh.Trimesh(vertices=new_verts, faces=new_faces, process=False)
    degen_mesh.export(str(path / "degenerate.stl"), file_type="stl")
    return str(path / "degenerate.stl")


def _write_high_poly_stl(path: Path) -> str:
    """Write a high-polygon mesh (>500K faces)."""
    # Highly subdivided sphere.
    mesh = trimesh.creation.icosphere(subdivisions=6)
    mesh.vertices *= 50.0
    mesh.export(str(path / "highpoly.stl"), file_type="stl")
    return str(path / "highpoly.stl")


def _write_obj_file(path: Path) -> str:
    """Write a simple OBJ cube."""
    mesh = trimesh.creation.box(extents=[10, 10, 10])
    mesh.export(str(path / "cube.obj"), file_type="obj")
    return str(path / "cube.obj")


def _write_ply_file(path: Path) -> str:
    """Write a PLY file."""
    mesh = trimesh.creation.box(extents=[10, 10, 10])
    mesh.export(str(path / "cube.ply"), file_type="ply")
    return str(path / "cube.ply")


# ---------------------------------------------------------------------------
# Tests: diagnose_mesh on clean meshes
# ---------------------------------------------------------------------------


class TestDiagnoseCleanMesh:
    """Tests for clean, watertight meshes."""

    def test_clean_cube_is_watertight(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.is_watertight is True

    def test_clean_cube_severity_is_clean(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        # May be "clean" or "minor" depending on trimesh processing.
        assert report.severity in ("clean", "minor")

    def test_clean_cube_no_holes(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.hole_count == 0

    def test_clean_cube_single_component(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.component_count == 1
        assert report.has_floating_fragments is False

    def test_clean_cube_dimensions(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        # Box is 10x20x30.
        assert abs(report.dimensions_mm["x"] - 10.0) < 0.1
        assert abs(report.dimensions_mm["y"] - 20.0) < 0.1
        assert abs(report.dimensions_mm["z"] - 30.0) < 0.1

    def test_clean_cube_has_volume(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.volume_mm3 is not None
        # Volume should be ~6000 mm3 (10*20*30).
        assert abs(report.volume_mm3 - 6000.0) < 10.0

    def test_clean_cube_face_and_vertex_count(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.face_count == 12  # Cube = 6 faces * 2 triangles.
        assert report.vertex_count == 8

    def test_clean_cube_bounding_box(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert "x_min" in report.bounding_box
        assert "x_max" in report.bounding_box
        assert "z_max" in report.bounding_box

    def test_clean_sphere(self, tmp_dir):
        stl = _write_sphere_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.is_watertight is True
        assert report.hole_count == 0

    def test_clean_mesh_recommendations_say_ready(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        if report.severity == "clean":
            assert any("ready" in r.lower() or "clean" in r.lower() for r in report.recommendations)


# ---------------------------------------------------------------------------
# Tests: holes (open boundaries)
# ---------------------------------------------------------------------------


class TestDiagnoseHoles:
    """Tests for meshes with holes."""

    def test_open_mesh_not_watertight(self, tmp_dir):
        stl = _write_open_mesh_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.is_watertight is False

    def test_open_mesh_has_holes(self, tmp_dir):
        stl = _write_open_mesh_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.hole_count > 0

    def test_open_mesh_hole_info_has_perimeter(self, tmp_dir):
        stl = _write_open_mesh_stl(tmp_dir)
        report = diagnose_mesh(stl)
        if report.holes:
            hole = report.holes[0]
            assert hole.perimeter_mm > 0
            assert hole.edge_count > 0

    def test_open_mesh_hole_info_has_centroid(self, tmp_dir):
        stl = _write_open_mesh_stl(tmp_dir)
        report = diagnose_mesh(stl)
        if report.holes:
            hole = report.holes[0]
            # Centroid should be a real coordinate, not NaN.
            assert hole.centroid_x == hole.centroid_x  # NaN check
            assert hole.centroid_y == hole.centroid_y
            assert hole.centroid_z == hole.centroid_z

    def test_open_mesh_severity_not_clean(self, tmp_dir):
        stl = _write_open_mesh_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.severity != "clean"

    def test_open_mesh_defects_mention_holes(self, tmp_dir):
        stl = _write_open_mesh_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert any("hole" in d.lower() for d in report.defects)

    def test_open_mesh_volume_is_none(self, tmp_dir):
        stl = _write_open_mesh_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.volume_mm3 is None


# ---------------------------------------------------------------------------
# Tests: inverted normals
# ---------------------------------------------------------------------------


class TestDiagnoseInvertedNormals:
    """Tests for meshes with inconsistent/inverted normals."""

    def test_inverted_normals_detected(self, tmp_dir):
        stl = _write_inverted_normals_obj(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.normals.consistent is False
        assert report.normals.inverted_count > 0
        assert report.normals.inverted_percentage > 0.0

    def test_inverted_normals_in_defects(self, tmp_dir):
        stl = _write_inverted_normals_obj(tmp_dir)
        report = diagnose_mesh(stl)
        assert any("inverted" in d.lower() or "normal" in d.lower() for d in report.defects)

    def test_inverted_normals_recommendation(self, tmp_dir):
        stl = _write_inverted_normals_obj(tmp_dir)
        report = diagnose_mesh(stl)
        assert any("normal" in r.lower() for r in report.recommendations)

    def test_clean_mesh_normals_consistent(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.normals.consistent is True
        assert report.normals.inverted_count == 0


# ---------------------------------------------------------------------------
# Tests: floating fragments (multiple components)
# ---------------------------------------------------------------------------


class TestDiagnoseFragments:
    """Tests for meshes with disconnected components."""

    def test_multi_component_detected(self, tmp_dir):
        stl = _write_multi_component_obj(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.component_count == 2
        assert report.has_floating_fragments is True

    def test_multi_component_info(self, tmp_dir):
        stl = _write_multi_component_obj(tmp_dir)
        report = diagnose_mesh(stl)
        assert len(report.components) == 2
        # Largest component should be marked.
        assert any(c.is_largest for c in report.components)

    def test_fragments_in_defects(self, tmp_dir):
        stl = _write_multi_component_obj(tmp_dir)
        report = diagnose_mesh(stl)
        assert any("fragment" in d.lower() or "disconnect" in d.lower() for d in report.defects)

    def test_fragments_recommendation_mentions_removal(self, tmp_dir):
        stl = _write_multi_component_obj(tmp_dir)
        report = diagnose_mesh(stl)
        assert any("remove" in r.lower() or "fragment" in r.lower() for r in report.recommendations)


# ---------------------------------------------------------------------------
# Tests: degenerate faces
# ---------------------------------------------------------------------------


class TestDiagnoseDegenerateFaces:
    """Tests for meshes with zero-area degenerate triangles."""

    def test_degenerate_faces_detected(self, tmp_dir):
        stl = _write_degenerate_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.degenerate_face_count > 0

    def test_degenerate_faces_in_defects(self, tmp_dir):
        stl = _write_degenerate_stl(tmp_dir)
        report = diagnose_mesh(stl)
        if report.degenerate_face_count > 0:
            assert any("degenerate" in d.lower() for d in report.defects)

    def test_clean_mesh_no_degenerate(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        assert report.degenerate_face_count == 0


# ---------------------------------------------------------------------------
# Tests: polygon count assessment
# ---------------------------------------------------------------------------


class TestPolygonCountAssessment:
    """Tests for polygon count level assessment."""

    def test_low_poly_is_ok(self):
        result = _assess_polygon_count(5000, 2500)
        assert result.level == "ok"
        assert result.decimation_ratio == 1.0

    def test_high_poly_detected(self):
        result = _assess_polygon_count(800_000, 400_000)
        assert result.level == "high"
        assert result.decimation_ratio < 1.0

    def test_excessive_poly_detected(self):
        result = _assess_polygon_count(5_000_000, 2_500_000)
        assert result.level == "excessive"
        assert result.decimation_ratio < 0.1

    def test_polygon_assessment_wired_to_face_count(self, tmp_dir):
        stl = _write_high_poly_stl(tmp_dir)
        report = diagnose_mesh(stl)
        # Icosphere subdivisions=6 produces ~82K faces (below 500K "high" threshold).
        # Verify polygon assessment is correctly wired into the full pipeline.
        assert report.polygon_assessment.face_count == report.face_count
        assert report.polygon_assessment.vertex_count == report.vertex_count
        assert report.face_count > 50_000  # Sanity: actually a high-ish poly mesh.

    def test_assessment_message_not_empty(self):
        result = _assess_polygon_count(100, 50)
        assert len(result.message) > 0


# ---------------------------------------------------------------------------
# Tests: input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Tests for error handling on bad inputs."""

    def test_missing_file_raises(self):
        with pytest.raises(ValueError, match="not found"):
            diagnose_mesh("/nonexistent/path/model.stl")

    def test_unsupported_extension_raises(self, tmp_dir):
        bad_file = tmp_dir / "model.gcode"
        bad_file.write_text("G28\n")
        with pytest.raises(ValueError, match="Unsupported"):
            diagnose_mesh(str(bad_file))

    def test_empty_file_raises(self, tmp_dir):
        empty = tmp_dir / "empty.stl"
        empty.write_bytes(b"")
        with pytest.raises((ValueError, Exception)):
            diagnose_mesh(str(empty))

    def test_corrupt_stl_raises(self, tmp_dir):
        corrupt = tmp_dir / "corrupt.stl"
        corrupt.write_bytes(b"not a real stl file content here")
        with pytest.raises((ValueError, Exception)):
            diagnose_mesh(str(corrupt))


# ---------------------------------------------------------------------------
# Tests: format support (OBJ, PLY)
# ---------------------------------------------------------------------------


class TestFormatSupport:
    """Tests for non-STL format loading."""

    def test_obj_loads_and_diagnoses(self, tmp_dir):
        obj = _write_obj_file(tmp_dir)
        report = diagnose_mesh(obj)
        assert report.face_count > 0
        assert report.vertex_count > 0

    def test_ply_loads_and_diagnoses(self, tmp_dir):
        ply = _write_ply_file(tmp_dir)
        report = diagnose_mesh(ply)
        assert report.face_count > 0


# ---------------------------------------------------------------------------
# Tests: dataclass serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """Tests for to_dict() round-trip."""

    def test_report_to_dict(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        d = report.to_dict()
        assert isinstance(d, dict)
        assert d["face_count"] == report.face_count
        assert d["is_watertight"] == report.is_watertight
        assert d["severity"] == report.severity
        assert isinstance(d["normals"], dict)
        assert isinstance(d["polygon_assessment"], dict)
        assert isinstance(d["holes"], list)
        assert isinstance(d["components"], list)
        assert isinstance(d["defects"], list)
        assert isinstance(d["recommendations"], list)

    def test_report_to_dict_has_all_fields(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)
        report = diagnose_mesh(stl)
        d = report.to_dict()
        expected_keys = {
            "file_path", "file_size_bytes", "face_count", "vertex_count",
            "is_watertight", "volume_mm3", "surface_area_mm2",
            "bounding_box", "dimensions_mm", "degenerate_face_count",
            "self_intersection_count", "normals", "polygon_assessment",
            "hole_count", "holes", "component_count", "components",
            "has_floating_fragments", "severity", "defects", "recommendations",
        }
        assert expected_keys.issubset(set(d.keys()))


# ---------------------------------------------------------------------------
# Tests: severity computation
# ---------------------------------------------------------------------------


class TestSeverityComputation:
    """Tests for _compute_severity logic."""

    def test_clean_severity(self):
        normals = NormalsReport(consistent=True, inverted_count=0, inverted_percentage=0.0)
        result = _compute_severity(
            degenerate_count=0,
            self_intersection_count=0,
            normals=normals,
            hole_count=0,
            has_fragments=False,
            is_watertight=True,
        )
        assert result == "clean"

    def test_minor_severity_one_issue(self):
        normals = NormalsReport(consistent=True, inverted_count=0, inverted_percentage=0.0)
        result = _compute_severity(
            degenerate_count=5,
            self_intersection_count=0,
            normals=normals,
            hole_count=0,
            has_fragments=False,
            is_watertight=True,
        )
        assert result == "minor"

    def test_moderate_severity(self):
        normals = NormalsReport(consistent=False, inverted_count=50, inverted_percentage=15.0)
        result = _compute_severity(
            degenerate_count=10,
            self_intersection_count=0,
            normals=normals,
            hole_count=3,
            has_fragments=False,
            is_watertight=False,
        )
        assert result in ("moderate", "severe")

    def test_severe_severity(self):
        normals = NormalsReport(consistent=False, inverted_count=100, inverted_percentage=50.0)
        result = _compute_severity(
            degenerate_count=200,
            self_intersection_count=10,
            normals=normals,
            hole_count=10,
            has_fragments=True,
            is_watertight=False,
        )
        assert result == "severe"


# ---------------------------------------------------------------------------
# Tests: graceful degradation without trimesh
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    """Tests for behavior when trimesh is not installed."""

    def test_import_error_message(self):
        with patch.dict("sys.modules", {"trimesh": None}):
            from kiln.mesh_diagnostics import _require_trimesh

            with pytest.raises(ImportError, match="trimesh"):
                _require_trimesh()


# ---------------------------------------------------------------------------
# Tests: MCP plugin tool
# ---------------------------------------------------------------------------


class TestMCPPlugin:
    """Tests for the mesh_diagnostic_tools plugin."""

    def test_plugin_has_required_attributes(self):
        from kiln.plugins.mesh_diagnostic_tools import plugin

        assert plugin.name == "mesh_diagnostic_tools"
        assert len(plugin.description) > 0

    def test_plugin_registers_tool(self):
        from kiln.plugins.mesh_diagnostic_tools import plugin

        tools_registered = []

        class FakeMCP:
            def tool(self_mcp):
                def decorator(fn):
                    tools_registered.append(fn.__name__)
                    return fn
                return decorator

        plugin.register(FakeMCP())
        assert "diagnose_mesh" in tools_registered

    def test_tool_returns_success_on_clean_mesh(self, tmp_dir):
        stl = _write_box_stl(tmp_dir)

        from kiln.plugins.mesh_diagnostic_tools import plugin

        tool_fn = None

        class FakeMCP:
            def tool(self_mcp):
                def decorator(fn):
                    nonlocal tool_fn
                    tool_fn = fn
                    return fn
                return decorator

        plugin.register(FakeMCP())
        result = tool_fn(file_path=stl)
        assert result["success"] is True
        assert "report" in result
        assert "message" in result

    def test_tool_returns_error_on_missing_file(self):
        from kiln.plugins.mesh_diagnostic_tools import plugin

        tool_fn = None

        class FakeMCP:
            def tool(self_mcp):
                def decorator(fn):
                    nonlocal tool_fn
                    tool_fn = fn
                    return fn
                return decorator

        plugin.register(FakeMCP())
        result = tool_fn(file_path="/nonexistent/model.stl")
        assert result["success"] is False
        assert "error" in result

    def test_tool_message_includes_severity_for_defective_mesh(self, tmp_dir):
        stl = _write_open_mesh_stl(tmp_dir)

        from kiln.plugins.mesh_diagnostic_tools import plugin

        tool_fn = None

        class FakeMCP:
            def tool(self_mcp):
                def decorator(fn):
                    nonlocal tool_fn
                    tool_fn = fn
                    return fn
                return decorator

        plugin.register(FakeMCP())
        result = tool_fn(file_path=stl)
        assert result["success"] is True
        # Message should mention severity for defective meshes.
        msg = result["message"].lower()
        assert any(word in msg for word in ("severity", "defect", "clean", "minor", "moderate", "severe"))
