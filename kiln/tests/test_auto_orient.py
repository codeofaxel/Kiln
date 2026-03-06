"""Tests for kiln.auto_orient -- auto-orientation and support estimation."""

from __future__ import annotations

import os
import struct
import tempfile
import xml.etree.ElementTree as ET
import zipfile

import pytest

from kiln.auto_orient import (
    OrientationCandidate,
    OrientationResult,
    SupportEstimate,
    _apply_rotation,
    _build_rotation_matrix,
    _parse_3mf_transform,
    _rotate_triangles,
    _rotation_matrix_x,
    _rotation_matrix_y,
    _rotation_matrix_z,
    _translate_to_bed,
    apply_orientation,
    estimate_supports,
    find_optimal_orientation,
    rotate_3mf_file,
    rotate_stl_file,
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
        (0, 2, 3),
        (4, 6, 5),
        (4, 7, 6),
        (0, 4, 5),
        (0, 5, 1),
        (2, 6, 7),
        (2, 7, 3),
        (0, 3, 7),
        (0, 7, 4),
        (1, 5, 6),
        (1, 6, 2),
    ]
    return [(verts[a], verts[b], verts[c]) for a, b, c in faces]


def _write_stl(tmpdir: str, triangles: list[tuple]) -> str:
    """Write a binary STL file and return its path."""
    path = os.path.join(tmpdir, "test_model.stl")
    with open(path, "wb") as fh:
        fh.write(_make_binary_stl(triangles))
    return path


# ---------------------------------------------------------------------------
# TestRotationMatrices
# ---------------------------------------------------------------------------


class TestRotationMatrices:
    def test_rotation_x_zero(self):
        m = _rotation_matrix_x(0)
        # Identity for zero rotation.
        assert abs(m[0][0] - 1.0) < 1e-9
        assert abs(m[1][1] - 1.0) < 1e-9
        assert abs(m[2][2] - 1.0) < 1e-9

    def test_rotation_x_90(self):
        m = _rotation_matrix_x(90)
        # Y -> Z, Z -> -Y
        v = _apply_rotation((0, 1, 0), m)
        assert abs(v[0]) < 1e-6
        assert abs(v[1]) < 1e-6
        assert abs(v[2] - 1.0) < 1e-6

    def test_rotation_y_90(self):
        m = _rotation_matrix_y(90)
        # X -> Z, Z -> -X (but different convention)
        v = _apply_rotation((1, 0, 0), m)
        assert abs(v[0]) < 1e-6
        assert abs(v[1]) < 1e-6
        assert abs(v[2] + 1.0) < 1e-6

    def test_rotation_z_90(self):
        m = _rotation_matrix_z(90)
        v = _apply_rotation((1, 0, 0), m)
        assert abs(v[0]) < 1e-6
        assert abs(v[1] - 1.0) < 1e-6
        assert abs(v[2]) < 1e-6

    def test_rotation_360_returns_to_original(self):
        m = _build_rotation_matrix(360, 0, 0)
        v = _apply_rotation((3, 5, 7), m)
        assert abs(v[0] - 3.0) < 1e-6
        assert abs(v[1] - 5.0) < 1e-6
        assert abs(v[2] - 7.0) < 1e-6

    def test_combined_rotation(self):
        m = _build_rotation_matrix(90, 90, 0)
        v = _apply_rotation((1, 0, 0), m)
        # Should transform, not be identity.
        assert not (abs(v[0] - 1.0) < 1e-6 and abs(v[1]) < 1e-6 and abs(v[2]) < 1e-6)


# ---------------------------------------------------------------------------
# TestRotateTriangles
# ---------------------------------------------------------------------------


class TestRotateTriangles:
    def test_identity_rotation_preserves_triangles(self):
        tris = _cube_triangles()
        m = _build_rotation_matrix(0, 0, 0)
        rotated = _rotate_triangles(tris, m)
        assert len(rotated) == len(tris)
        for orig, rot in zip(tris, rotated, strict=True):
            for vo, vr in zip(orig, rot, strict=True):
                for co, cr in zip(vo, vr, strict=True):
                    assert abs(co - cr) < 1e-6

    def test_rotation_changes_vertices(self):
        tris = [((1, 0, 0), (0, 1, 0), (0, 0, 1))]
        m = _build_rotation_matrix(90, 0, 0)
        rotated = _rotate_triangles(tris, m)
        # At least one coordinate should differ.
        assert rotated[0] != tris[0]


# ---------------------------------------------------------------------------
# TestTranslateToBed
# ---------------------------------------------------------------------------


class TestTranslateToBed:
    def test_already_on_bed(self):
        tris = _cube_triangles()
        translated = _translate_to_bed(tris)
        z_min = min(v[2] for tri in translated for v in tri)
        assert abs(z_min) < 1e-9

    def test_elevated_model(self):
        tris = [((0, 0, 5), (10, 0, 5), (5, 10, 10))]
        translated = _translate_to_bed(tris)
        z_min = min(v[2] for tri in translated for v in tri)
        assert abs(z_min) < 1e-9

    def test_below_bed(self):
        tris = [((0, 0, -3), (10, 0, -3), (5, 10, 2))]
        translated = _translate_to_bed(tris)
        z_min = min(v[2] for tri in translated for v in tri)
        assert abs(z_min) < 1e-9


# ---------------------------------------------------------------------------
# TestFindOptimalOrientation
# ---------------------------------------------------------------------------


class TestFindOptimalOrientation:
    def test_cube_returns_valid_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles())
            result = find_optimal_orientation(path)
            assert isinstance(result, OrientationResult)
            assert isinstance(result.best, OrientationCandidate)
            assert result.best.score >= 0
            assert len(result.alternatives) > 0

    def test_cube_score_is_reasonable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles())
            result = find_optimal_orientation(path)
            assert result.best.score > 0

    def test_result_to_dict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles())
            result = find_optimal_orientation(path)
            d = result.to_dict()
            assert "best" in d
            assert "alternatives" in d
            assert "original_score" in d
            assert "improvement_percentage" in d

    def test_candidate_has_reasoning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles())
            result = find_optimal_orientation(path)
            assert result.best.reasoning != ""

    def test_limited_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles())
            result = find_optimal_orientation(path, candidates=4)
            assert isinstance(result, OrientationResult)

    def test_nonexistent_file_raises(self):
        with pytest.raises(ValueError, match="File not found"):
            find_optimal_orientation("/nonexistent/model.stl")


# ---------------------------------------------------------------------------
# TestApplyOrientation
# ---------------------------------------------------------------------------


class TestApplyOrientation:
    def test_apply_writes_valid_stl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = _write_stl(tmpdir, _cube_triangles())
            output_path = os.path.join(tmpdir, "oriented.stl")
            result = apply_orientation(input_path, 90, 0, 0, output_path=output_path)
            assert result == output_path
            assert os.path.isfile(output_path)
            assert os.path.getsize(output_path) > 0

    def test_apply_default_output_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = _write_stl(tmpdir, _cube_triangles())
            result = apply_orientation(input_path, 0, 90, 0)
            assert result.endswith("_oriented.stl")
            assert os.path.isfile(result)

    def test_apply_zero_rotation_preserves_geometry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = _write_stl(tmpdir, _cube_triangles())
            output_path = os.path.join(tmpdir, "same.stl")
            apply_orientation(input_path, 0, 0, 0, output_path=output_path)
            # File sizes should match (same number of triangles).
            assert os.path.getsize(input_path) == os.path.getsize(output_path)

    def test_apply_nonexistent_file_raises(self):
        with pytest.raises(ValueError, match="File not found"):
            apply_orientation("/nonexistent/model.stl", 90, 0, 0)


# ---------------------------------------------------------------------------
# TestEstimateSupports
# ---------------------------------------------------------------------------


class TestEstimateSupports:
    def test_cube_support_estimate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles())
            result = estimate_supports(path)
            assert isinstance(result, SupportEstimate)
            assert isinstance(result.needs_supports, bool)

    def test_support_estimate_to_dict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles())
            d = estimate_supports(path).to_dict()
            assert "estimated_support_volume_mm3" in d
            assert "needs_supports" in d

    def test_nonexistent_file_raises(self):
        with pytest.raises(ValueError, match="File not found"):
            estimate_supports("/nonexistent/model.stl")


# ---------------------------------------------------------------------------
# TestOrientationDataclasses
# ---------------------------------------------------------------------------


class TestOrientationDataclasses:
    def test_orientation_candidate_to_dict(self):
        c = OrientationCandidate(
            rotation_x=90,
            rotation_y=0,
            rotation_z=0,
            score=85.0,
            support_volume_mm3=100.0,
            bed_contact_area_mm2=50.0,
            print_height_mm=20.0,
            overhang_percentage=5.0,
            reasoning="test",
        )
        d = c.to_dict()
        assert d["rotation_x"] == 90
        assert d["score"] == 85.0

    def test_support_estimate_to_dict_fields(self):
        s = SupportEstimate(
            estimated_support_volume_mm3=500.0,
            support_percentage=10.0,
            overhang_triangle_count=50,
            overhang_percentage=5.0,
            needs_supports=True,
        )
        d = s.to_dict()
        assert d["estimated_support_volume_mm3"] == 500.0
        assert d["needs_supports"] is True


# ---------------------------------------------------------------------------
# Helpers — 3MF fixtures
# ---------------------------------------------------------------------------

_3MF_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def _make_minimal_3mf(path: str, *, transform: str | None = None) -> str:
    """Create a minimal valid 3MF file with one object and one build item."""
    item_attr = ' objectid="1"'
    if transform:
        item_attr += f' transform="{transform}"'
    model_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<model xmlns="{_3MF_NS}" unit="millimeter">'
        "<resources>"
        '<object id="1" type="model">'
        "<mesh>"
        "<vertices>"
        '<vertex x="0" y="0" z="0"/>'
        '<vertex x="1" y="0" z="0"/>'
        '<vertex x="0" y="1" z="0"/>'
        "</vertices>"
        "<triangles>"
        '<triangle v1="0" v2="1" v3="2"/>'
        "</triangles>"
        "</mesh>"
        "</object>"
        "</resources>"
        f"<build><item{item_attr}/></build>"
        "</model>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", model_xml)
    return path


# ---------------------------------------------------------------------------
# TestRotateStlFile
# ---------------------------------------------------------------------------


class TestRotateStlFile:
    """Tests for rotate_stl_file — STL file rotation."""

    def test_rotate_z_axis(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = _write_stl(tmpdir, _cube_triangles())
            output_path = os.path.join(tmpdir, "rotated.stl")
            result = rotate_stl_file(input_path, output_path, rotation_z=45.0)
            assert result == output_path
            assert os.path.isfile(output_path)
            assert os.path.getsize(output_path) > 0

    def test_rotate_x_axis(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = _write_stl(tmpdir, _cube_triangles())
            output_path = os.path.join(tmpdir, "rotated.stl")
            rotate_stl_file(input_path, output_path, rotation_x=90.0)
            assert os.path.isfile(output_path)

    def test_rotate_combined(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = _write_stl(tmpdir, _cube_triangles())
            output_path = os.path.join(tmpdir, "rotated.stl")
            rotate_stl_file(
                input_path, output_path, rotation_x=30.0, rotation_y=45.0, rotation_z=60.0
            )
            assert os.path.isfile(output_path)

    def test_zero_rotation_preserves_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = _write_stl(tmpdir, _cube_triangles())
            output_path = os.path.join(tmpdir, "rotated.stl")
            rotate_stl_file(input_path, output_path)
            # Same number of triangles → same file size.
            assert os.path.getsize(input_path) == os.path.getsize(output_path)

    def test_nonexistent_file_raises(self):
        with pytest.raises(ValueError, match="File not found"):
            rotate_stl_file("/nonexistent/model.stl", "/tmp/out.stl", rotation_z=45.0)


# ---------------------------------------------------------------------------
# TestRotate3mfFile
# ---------------------------------------------------------------------------


class TestRotate3mfFile:
    """Tests for rotate_3mf_file — 3MF file rotation."""

    def test_rotate_z_axis(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = _make_minimal_3mf(os.path.join(tmpdir, "model.3mf"))
            output_path = os.path.join(tmpdir, "rotated.3mf")
            result = rotate_3mf_file(input_path, output_path, rotation_z=90.0)
            assert result == output_path
            assert os.path.isfile(output_path)

            # Verify the transform was written into the XML.
            with zipfile.ZipFile(output_path) as zf:
                xml_bytes = zf.read("3D/3dmodel.model")
            root = ET.fromstring(xml_bytes)
            ns = {"m": _3MF_NS}
            items = root.findall(".//m:build/m:item", ns)
            assert len(items) == 1
            transform = items[0].get("transform")
            assert transform is not None
            vals = [float(v) for v in transform.split()]
            assert len(vals) == 12

    def test_rotate_preserves_other_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "model.3mf")
            # Add an extra file into the 3MF.
            _make_minimal_3mf(input_path)
            with zipfile.ZipFile(input_path, "a") as zf:
                zf.writestr("Metadata/extra.txt", "hello")

            output_path = os.path.join(tmpdir, "rotated.3mf")
            rotate_3mf_file(input_path, output_path, rotation_z=45.0)

            with zipfile.ZipFile(output_path) as zf:
                assert "Metadata/extra.txt" in zf.namelist()
                assert zf.read("Metadata/extra.txt") == b"hello"

    def test_rotate_composes_with_existing_transform(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Start with an existing identity-ish transform.
            input_path = _make_minimal_3mf(
                os.path.join(tmpdir, "model.3mf"),
                transform="1 0 0 0 1 0 0 0 1 10 20 30",
            )
            output_path = os.path.join(tmpdir, "rotated.3mf")
            rotate_3mf_file(input_path, output_path, rotation_z=90.0)

            with zipfile.ZipFile(output_path) as zf:
                xml_bytes = zf.read("3D/3dmodel.model")
            root = ET.fromstring(xml_bytes)
            ns = {"m": _3MF_NS}
            item = root.find(".//m:build/m:item", ns)
            vals = [float(v) for v in item.get("transform").split()]
            # Translation should have been rotated too (10,20,30 rotated 90° Z).
            # X' = -20, Y' = 10 (approximately).
            tx, ty, tz = vals[9], vals[10], vals[11]
            assert abs(tx - (-20.0)) < 1e-6
            assert abs(ty - 10.0) < 1e-6
            assert abs(tz - 30.0) < 1e-6

    def test_zero_rotation_sets_identity_transform(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = _make_minimal_3mf(os.path.join(tmpdir, "model.3mf"))
            output_path = os.path.join(tmpdir, "rotated.3mf")
            rotate_3mf_file(input_path, output_path)

            with zipfile.ZipFile(output_path) as zf:
                xml_bytes = zf.read("3D/3dmodel.model")
            root = ET.fromstring(xml_bytes)
            ns = {"m": _3MF_NS}
            item = root.find(".//m:build/m:item", ns)
            vals = [float(v) for v in item.get("transform").split()]
            # Should be identity.
            expected = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
            for v, e in zip(vals, expected, strict=True):
                assert abs(v - e) < 1e-6

    def test_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError, match="File not found"):
            rotate_3mf_file("/nonexistent/model.3mf", "/tmp/out.3mf", rotation_z=45.0)

    def test_missing_build_section_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "bad.3mf")
            bad_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<model xmlns="{_3MF_NS}" unit="millimeter">'
                "<resources/>"
                "</model>"
            )
            with zipfile.ZipFile(input_path, "w") as zf:
                zf.writestr("3D/3dmodel.model", bad_xml)

            with pytest.raises(ValueError, match="missing <build> section"):
                rotate_3mf_file(input_path, os.path.join(tmpdir, "out.3mf"), rotation_z=45.0)

    def test_missing_model_file_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "bad.3mf")
            with zipfile.ZipFile(input_path, "w") as zf:
                zf.writestr("dummy.txt", "not a 3MF")

            with pytest.raises(ValueError, match="not a valid 3MF"):
                rotate_3mf_file(input_path, os.path.join(tmpdir, "out.3mf"), rotation_z=45.0)


# ---------------------------------------------------------------------------
# TestParse3mfTransform
# ---------------------------------------------------------------------------


class TestParse3mfTransform:
    """Tests for _parse_3mf_transform helper."""

    def test_identity(self):
        m = _parse_3mf_transform("1 0 0 0 1 0 0 0 1 0 0 0")
        assert len(m) == 4
        assert m[0] == [1.0, 0.0, 0.0]
        assert m[3] == [0.0, 0.0, 0.0]

    def test_wrong_count_raises(self):
        with pytest.raises(ValueError, match="Expected 12"):
            _parse_3mf_transform("1 0 0")
