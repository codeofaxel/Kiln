"""Tests for kiln.auto_orient -- auto-orientation and support estimation."""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import types
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
    check_stability,
    duplicate_stl_on_plate,
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
            rotate_stl_file(input_path, output_path, rotation_x=30.0, rotation_y=45.0, rotation_z=60.0)
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
                f'<?xml version="1.0" encoding="UTF-8"?><model xmlns="{_3MF_NS}" unit="millimeter"><resources/></model>'
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




# ---------------------------------------------------------------------------
# Helpers — STL rectangular prism generator for stability tests
# ---------------------------------------------------------------------------


def _make_flat_cube_stl(tmp_path, width: float, depth: float, height: float) -> str:
    """Create a binary STL of a rectangular prism (0,0,0)→(width,depth,height).

    A rectangular prism has 6 faces × 2 triangles = 12 triangles total.
    Returns the path to the written STL file.
    """
    w, d, h = float(width), float(depth), float(height)
    # 8 vertices of the rectangular prism
    v = [
        (0, 0, 0),    # 0 - front bottom left
        (w, 0, 0),    # 1 - front bottom right
        (w, d, 0),    # 2 - back bottom right
        (0, d, 0),    # 3 - back bottom left
        (0, 0, h),    # 4 - front top left
        (w, 0, h),    # 5 - front top right
        (w, d, h),    # 6 - back top right
        (0, d, h),    # 7 - back top left
    ]
    # 12 triangles (2 per face), vertex indices
    faces = [
        (0, 2, 1), (0, 3, 2),  # bottom (z=0)
        (4, 5, 6), (4, 6, 7),  # top (z=h)
        (0, 1, 5), (0, 5, 4),  # front (y=0)
        (2, 3, 7), (2, 7, 6),  # back (y=d)
        (0, 4, 7), (0, 7, 3),  # left (x=0)
        (1, 2, 6), (1, 6, 5),  # right (x=w)
    ]

    path = os.path.join(str(tmp_path), f"prism_{width}x{depth}x{height}.stl")
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)  # header
        f.write(struct.pack("<I", len(faces)))  # triangle count
        for a, b, c in faces:
            f.write(struct.pack("<3f", 0.0, 0.0, 0.0))  # normal (auto-computed by parsers)
            f.write(struct.pack("<3f", *v[a]))  # vertex 1
            f.write(struct.pack("<3f", *v[b]))  # vertex 2
            f.write(struct.pack("<3f", *v[c]))  # vertex 3
            f.write(struct.pack("<H", 0))  # attribute byte count
    return path


# ---------------------------------------------------------------------------
# TestCheckStability
# ---------------------------------------------------------------------------


class TestCheckStability:
    """Tests for check_stability() — orientation stability analysis.

    Covers:
        - File not found raises ValueError
        - 3MF files raise ValueError (not supported)
        - Stable flat cube → low risk
        - Tall thin tower → high risk
        - Medium risk borderline geometry
        - Custom max_height_to_base_ratio threshold
        - to_dict() returns all expected fields
        - Center of gravity is reasonable for uniform geometry
    """

    def test_file_not_found_raises(self):

        with pytest.raises(ValueError, match="[Ff]ile"):
            check_stability("/nonexistent/model.stl")

    def test_3mf_raises_not_supported(self, tmp_path):

        p = tmp_path / "model.3mf"
        p.write_bytes(b"dummy")
        with pytest.raises(ValueError, match="(?i)3mf"):
            check_stability(str(p))

    def test_stable_cube_low_risk(self, tmp_path):

        # 20x20x5mm — wide base, short height → low risk
        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 5)
        result = check_stability(stl_path)
        assert result.stable is True
        assert result.risk_level == "low"
        assert result.height_to_base_ratio < 2.0
        assert result.suggested_rotation is None
        assert "stable" in result.recommendation.lower()

    def test_tall_tower_high_risk(self, tmp_path):

        # 5x5x100mm — tiny base, very tall → high risk
        stl_path = _make_flat_cube_stl(tmp_path, 5, 5, 100)
        result = check_stability(stl_path)
        assert result.stable is False
        assert result.risk_level == "high"
        assert result.height_to_base_ratio >= 3.0
        assert (
            "reorient" in result.recommendation.lower()
            or "wobble" in result.recommendation.lower()
        )

    def test_medium_risk_borderline(self, tmp_path):

        # 10x10x25mm — ratio ≈ 2.5 (between 2.0 and 3.0)
        stl_path = _make_flat_cube_stl(tmp_path, 10, 10, 25)
        result = check_stability(stl_path)
        assert result.risk_level == "medium"
        assert result.stable is False
        assert "brim" in result.recommendation.lower()

    def test_custom_threshold(self, tmp_path):

        stl_path = _make_flat_cube_stl(tmp_path, 10, 10, 25)
        # With a much higher threshold, the same part should be lower risk
        result = check_stability(stl_path, max_height_to_base_ratio=5.0)
        assert result.risk_level in ("low", "medium")

    def test_to_dict_returns_all_fields(self, tmp_path):

        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 5)
        result = check_stability(stl_path)
        d = result.to_dict()
        assert "stable" in d
        assert "risk_level" in d
        assert "height_mm" in d
        assert "base_footprint_mm2" in d
        assert "height_to_base_ratio" in d
        assert "center_of_gravity_z_mm" in d
        assert "recommendation" in d
        assert "suggested_rotation" in d

    def test_center_of_gravity_reasonable(self, tmp_path):

        # Uniform 20mm cube — CoG should be near z=10
        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 20)
        result = check_stability(stl_path)
        assert 5 < result.center_of_gravity_z_mm < 15

    def test_height_mm_matches_geometry(self, tmp_path):

        stl_path = _make_flat_cube_stl(tmp_path, 10, 10, 30)
        result = check_stability(stl_path)
        assert abs(result.height_mm - 30.0) < 1.0

    def test_base_footprint_reasonable(self, tmp_path):

        # 20x20 base → ~400 mm² footprint
        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 5)
        result = check_stability(stl_path)
        # The footprint should be close to 400 (the bottom face area)
        assert result.base_footprint_mm2 > 100  # at least a reasonable fraction


# ---------------------------------------------------------------------------
# TestDuplicateStlOnPlate
# ---------------------------------------------------------------------------


class TestDuplicateStlOnPlate:
    """Tests for duplicate_stl_on_plate() — multi-copy plate arrangement.

    Covers:
        - Basic 2-copy duplication produces valid STL
        - 4-copy grid arrangement
        - Count < 2 raises ValueError
        - Too many copies for bed raises ValueError
        - Model too large for bed raises ValueError
        - Custom spacing
        - Output triangle count is count × source triangles
        - All copies fit within bed bounds
        - Temp file created when output_path is None
    """

    def test_two_copies_produces_valid_stl(self, tmp_path):
        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 10)
        output = duplicate_stl_on_plate(
            stl_path, 2, bed_width_mm=256, bed_depth_mm=256,
            output_path=os.path.join(str(tmp_path), "out.stl"),
        )
        assert os.path.isfile(output)
        assert os.path.getsize(output) > 0

    def test_four_copies_grid(self, tmp_path):
        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 10)
        output = duplicate_stl_on_plate(
            stl_path, 4, bed_width_mm=256, bed_depth_mm=256,
            output_path=os.path.join(str(tmp_path), "out4.stl"),
        )
        # Read back and verify triangle count: 12 triangles per cube × 4 = 48
        with open(output, "rb") as f:
            f.read(80)  # header
            count = struct.unpack("<I", f.read(4))[0]
        assert count == 12 * 4

    def test_count_less_than_2_raises(self, tmp_path):
        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 10)
        with pytest.raises(ValueError, match="count must be >= 2"):
            duplicate_stl_on_plate(stl_path, 1)

    def test_too_many_copies_raises(self, tmp_path):
        # 100mm model + 10mm spacing = 110mm per cell on a 256mm bed
        # Only 2 columns × 2 rows = 4 max
        stl_path = _make_flat_cube_stl(tmp_path, 100, 100, 10)
        with pytest.raises(ValueError, match="Cannot fit"):
            duplicate_stl_on_plate(
                stl_path, 10, bed_width_mm=256, bed_depth_mm=256,
            )

    def test_printer_id_resolves_build_plate(self, tmp_path):
        stl_path = _make_flat_cube_stl(tmp_path, 140, 140, 10)
        output = duplicate_stl_on_plate(
            stl_path,
            4,
            printer_id="Kilntest Big 350",
            output_path=os.path.join(str(tmp_path), "k1max.stl"),
        )
        assert os.path.isfile(output)

    def test_unknown_printer_id_raises(self, tmp_path):
        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 10)
        with pytest.raises(ValueError, match="Unknown printer_id"):
            duplicate_stl_on_plate(stl_path, 2, printer_id="mystery_bot")

    def test_model_too_large_for_bed(self, tmp_path):
        stl_path = _make_flat_cube_stl(tmp_path, 300, 300, 10)
        with pytest.raises(ValueError, match="too large"):
            duplicate_stl_on_plate(
                stl_path, 2, bed_width_mm=256, bed_depth_mm=256,
            )

    def test_custom_spacing(self, tmp_path):
        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 10)
        output = duplicate_stl_on_plate(
            stl_path, 2, spacing_mm=50.0, bed_width_mm=256, bed_depth_mm=256,
            output_path=os.path.join(str(tmp_path), "spaced.stl"),
        )
        assert os.path.isfile(output)

    def test_triangle_count_is_multiplied(self, tmp_path):
        stl_path = _make_flat_cube_stl(tmp_path, 10, 10, 10)
        # Source has 12 triangles
        for n in (2, 3, 5):
            output = duplicate_stl_on_plate(
                stl_path, n, bed_width_mm=256, bed_depth_mm=256,
                output_path=os.path.join(str(tmp_path), f"out_{n}.stl"),
            )
            with open(output, "rb") as f:
                f.read(80)
                count = struct.unpack("<I", f.read(4))[0]
            assert count == 12 * n

    def test_copies_within_bed_bounds(self, tmp_path):
        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 10)
        output = duplicate_stl_on_plate(
            stl_path, 4, bed_width_mm=256, bed_depth_mm=256,
            output_path=os.path.join(str(tmp_path), "bounds.stl"),
        )
        # Read all vertices and verify within bed bounds
        with open(output, "rb") as f:
            f.read(80)
            tri_count = struct.unpack("<I", f.read(4))[0]
            for _ in range(tri_count):
                f.read(12)  # skip normal
                for _ in range(3):
                    x, y, z = struct.unpack("<3f", f.read(12))
                    assert 0 <= x <= 256, f"x={x} out of bounds"
                    assert 0 <= y <= 256, f"y={y} out of bounds"
                    assert z >= 0, f"z={z} below bed"
                f.read(2)  # attribute

    def test_temp_file_when_no_output_path(self, tmp_path):
        stl_path = _make_flat_cube_stl(tmp_path, 10, 10, 10)
        output = duplicate_stl_on_plate(
            stl_path, 2, bed_width_mm=256, bed_depth_mm=256,
        )
        assert os.path.isfile(output)
        assert output.endswith("_multi.stl")
        os.unlink(output)  # cleanup

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(ValueError, match="File not found"):
            duplicate_stl_on_plate("/nonexistent/model.stl", 2)

    def test_non_square_bed(self, tmp_path):
        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 10)
        output = duplicate_stl_on_plate(
            stl_path, 4, bed_width_mm=300, bed_depth_mm=200,
            output_path=os.path.join(str(tmp_path), "nonsquare.stl"),
        )
        assert os.path.isfile(output)
        # Verify all vertices within the non-square bed
        with open(output, "rb") as f:
            f.read(80)
            tri_count = struct.unpack("<I", f.read(4))[0]
            for _ in range(tri_count):
                f.read(12)
                for _ in range(3):
                    x, y, _z = struct.unpack("<3f", f.read(12))
                    assert 0 <= x <= 300, f"x={x} out of bounds"
                    assert 0 <= y <= 200, f"y={y} out of bounds"
                f.read(2)

    def test_zero_spacing(self, tmp_path):
        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 10)
        output = duplicate_stl_on_plate(
            stl_path, 2, spacing_mm=0.0, bed_width_mm=256, bed_depth_mm=256,
            output_path=os.path.join(str(tmp_path), "zero_space.stl"),
        )
        assert os.path.isfile(output)


# ---------------------------------------------------------------------------
# TestOrientationScoringSeam -- the bed-contact floor and the kiln-pro seam
# ---------------------------------------------------------------------------

_SCORER_MODULE = "kiln_pro.orientation_scoring"


def _hide_scorer(monkeypatch) -> None:
    """Make the kiln-pro scorer unimportable, as on a plain install on the
    free plan; Kiln's servers fail the test if asked."""
    import kiln.licensing as licensing
    import kiln.server as srv
    from kiln import _pro_orientation_bridge as bridge

    monkeypatch.setitem(sys.modules, _SCORER_MODULE, None)
    monkeypatch.setattr(licensing, "check_tier", lambda *a, **k: (False, "needs Kiln Pro"))
    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    monkeypatch.setattr(srv, "_pro_api_call", lambda *a, **k: pytest.fail("asked the servers"))


def _served_ranking(monkeypatch, reply) -> list[dict]:
    """A plain install (no kiln-pro) on a Kiln Pro plan, with Kiln's servers
    stood in by *reply* (a dict, or a function of what was sent); returns
    what was sent."""
    import kiln.licensing as licensing
    import kiln.server as srv
    from kiln import _pro_orientation_bridge as bridge

    monkeypatch.setitem(sys.modules, _SCORER_MODULE, None)
    monkeypatch.setattr(licensing, "check_tier", lambda *a, **k: (True, None))
    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    sent: list[dict] = []

    def wire(tool_name, /, _timeout=None, _asked_by_user=True, **kwargs):
        assert tool_name == bridge.WIRE_TOOL
        sent.append(kwargs)
        return reply(kwargs) if callable(reply) else reply

    monkeypatch.setattr(srv, "_pro_api_call", wire)
    return sent


def _stand_in_scorer(monkeypatch, score_fn) -> list[dict]:
    """Install a stand-in scorer module; returns the facts it was handed."""
    seen: list[dict] = []

    def score_orientation(facts):
        seen.append(dict(facts))
        return score_fn(facts)

    module = types.ModuleType(_SCORER_MODULE)
    module.score_orientation = score_orientation
    monkeypatch.setitem(sys.modules, _SCORER_MODULE, module)
    return seen


class TestOrientationScoringSeam:
    """Pin the seam in ``_score_orientation``.

    - No kiln-pro scorer (a plain install): the score is the bed-contact
      percentage, nothing else.
    - A scorer that answers: its score replaces the floor.
    - A scorer that answers ``None`` or fails: the floor answers.

    The four geometry facts are identical either way; only ``score`` moves.
    How Kiln Pro ranks is judged in kiln-pro's own suite.
    """

    def test_plain_install_scores_by_bed_contact(self, tmp_path, monkeypatch):
        """With the scorer unimportable, a short and a tall prism on the same
        base score the same: height, support and overhang do not enter."""
        _hide_scorer(monkeypatch)
        from kiln import _pro_orientation_bridge

        assert _pro_orientation_bridge.rank([{"bed_contact_pct": 42.0}]) is None

        short = find_optimal_orientation(_make_flat_cube_stl(tmp_path, 20, 20, 5), candidates=1)
        tall = find_optimal_orientation(_make_flat_cube_stl(tmp_path, 20, 20, 60), candidates=1)
        assert short.original_score == tall.original_score
        assert short.original_score > 0

    def test_scorer_score_replaces_the_floor(self, tmp_path, monkeypatch):
        """A scorer that answers sets ``score``; the facts it is handed are
        the ones the candidate reports."""
        seen = _stand_in_scorer(monkeypatch, lambda facts: 12.5)
        result = find_optimal_orientation(_make_flat_cube_stl(tmp_path, 20, 20, 5), candidates=1)
        assert result.original_score == 12.5
        assert result.best.score == 12.5
        assert seen, "the scorer was never asked"
        assert {"bed_contact_pct", "support_pct", "print_height_mm", "overhang_pct"} <= set(seen[0])

    def test_scorer_answering_none_is_the_floor(self, tmp_path, monkeypatch):
        stl = _make_flat_cube_stl(tmp_path, 20, 20, 5)
        _hide_scorer(monkeypatch)
        floor = find_optimal_orientation(stl)
        _stand_in_scorer(monkeypatch, lambda facts: None)
        assert find_optimal_orientation(stl).to_dict() == floor.to_dict()

    def test_failing_scorer_falls_back_to_the_floor(self, tmp_path, monkeypatch):
        """A broken scorer never takes the tool down: the floor answers."""
        stl = _make_flat_cube_stl(tmp_path, 20, 20, 5)
        _hide_scorer(monkeypatch)
        floor = find_optimal_orientation(stl)

        def boom(facts):
            raise RuntimeError("scorer broke")

        _stand_in_scorer(monkeypatch, boom)
        assert find_optimal_orientation(stl).to_dict() == floor.to_dict()

    def test_facts_identical_across_the_seam(self, tmp_path, monkeypatch):
        """Crossing the seam changes the score, never the geometry facts."""
        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 5)

        _hide_scorer(monkeypatch)
        free = find_optimal_orientation(stl_path)

        # A stand-in that prefers short parts, so its ranking keeps the flat
        # pose best while every score differs from the floor's.
        _stand_in_scorer(monkeypatch, lambda facts: 100.0 - facts["print_height_mm"])
        pro = find_optimal_orientation(stl_path)

        assert free.best.score != pro.best.score
        assert free.best.rotation_x == pro.best.rotation_x
        assert free.best.rotation_y == pro.best.rotation_y
        assert free.best.rotation_z == pro.best.rotation_z
        assert free.best.bed_contact_area_mm2 == pro.best.bed_contact_area_mm2
        assert free.best.support_volume_mm3 == pro.best.support_volume_mm3
        assert free.best.print_height_mm == pro.best.print_height_mm
        assert free.best.overhang_percentage == pro.best.overhang_percentage

    def test_orientation_result_shape_identical_across_the_seam(self, tmp_path, monkeypatch):
        """The ``OrientationResult`` shape is the same either side of the
        seam; only the numeric ``score`` field varies."""
        import dataclasses

        stl_path = _make_flat_cube_stl(tmp_path, 20, 20, 5)

        _hide_scorer(monkeypatch)
        free = find_optimal_orientation(stl_path)

        _stand_in_scorer(monkeypatch, lambda facts: 50.0)
        pro = find_optimal_orientation(stl_path)

        assert dataclasses.fields(type(free)) == dataclasses.fields(type(pro))
        assert dataclasses.fields(type(free.best)) == dataclasses.fields(type(pro.best))


class TestTheFloorKeepsAPartSteady:
    """On the bed-contact floor every flat face of a block scores the same, so
    a tall pillar's upright pose tied with lying down and, listed first, won:
    auto_orient_model kept it standing and check_stability's high wobble risk
    came with no turn to lay it flat.  Between equal scores the lower part
    now ranks first."""

    def test_a_tall_pillar_is_laid_down(self, tmp_path, monkeypatch):
        _hide_scorer(monkeypatch)
        result = find_optimal_orientation(_make_flat_cube_stl(tmp_path, 10, 10, 80))
        assert result.best.print_height_mm == pytest.approx(10.0, abs=0.01)
        assert result.best.score == result.original_score  # the floor's scores are unchanged

    def test_the_wobble_warning_comes_with_the_turn_that_fixes_it(self, tmp_path, monkeypatch):
        _hide_scorer(monkeypatch)
        stability = check_stability(_make_flat_cube_stl(tmp_path, 10, 10, 80))
        assert stability.risk_level == "high"
        assert stability.suggested_rotation is not None
        assert "Try rotating" in stability.recommendation

    def test_a_part_already_lying_flat_stays(self, tmp_path, monkeypatch):
        _hide_scorer(monkeypatch)
        result = find_optimal_orientation(_make_flat_cube_stl(tmp_path, 20, 20, 5))
        assert (result.best.rotation_x, result.best.rotation_y, result.best.rotation_z) == (0.0, 0.0, 0.0)

    def test_a_scorer_s_ranking_still_decides_between_unequal_scores(self, tmp_path, monkeypatch):
        """The lower-part order only splits equal scores: a scorer that
        prefers the tall pose keeps it."""
        _stand_in_scorer(monkeypatch, lambda facts: facts["print_height_mm"])
        result = find_optimal_orientation(_make_flat_cube_stl(tmp_path, 10, 10, 80))
        assert result.best.print_height_mm == pytest.approx(80.0, abs=0.01)


def _auto_orient_tool():
    from kiln.plugins.printability_tools import plugin

    tools: dict = {}

    class _Mcp:
        def tool(self, *_a, **_kw):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    return tools["auto_orient_model"]


def _stand_in_note(monkeypatch, note_fn) -> None:
    module = types.ModuleType(_SCORER_MODULE)
    module.score_orientation = lambda facts: None
    module.ranking_withheld_note = note_fn
    monkeypatch.setitem(sys.modules, _SCORER_MODULE, module)


class TestTheRankingNote:
    """A caller ranked on the floor because Kiln Pro's ranking is not in
    their plan is told so once, in ``upgrade_hint``; kiln-pro words it and
    decides when.  A plain install says nothing."""

    def test_the_note_is_said_once(self, tmp_path, monkeypatch):
        note = "Ranked by bed contact. Kiln Pro ranks them its own way: https://kiln3d.com/pricing"
        _stand_in_note(monkeypatch, lambda: note)
        out = _auto_orient_tool()(file_path=_make_flat_cube_stl(tmp_path, 10, 10, 80))
        assert out["success"] is True and out["upgrade_hint"] == note
        assert str(out).count("kiln3d.com/pricing") == 1

    @pytest.mark.parametrize("answer", [None, "", "boom"])
    def test_no_note_when_kiln_pro_has_none_or_fails(self, tmp_path, monkeypatch, answer):
        def note_fn():
            if answer == "boom":
                raise RuntimeError("note broke")
            return answer

        _stand_in_note(monkeypatch, note_fn)
        out = _auto_orient_tool()(file_path=_make_flat_cube_stl(tmp_path, 10, 10, 80))
        assert out["success"] is True and "upgrade_hint" not in out

    def test_a_plain_install_says_nothing(self, tmp_path, monkeypatch):
        _hide_scorer(monkeypatch)
        out = _auto_orient_tool()(file_path=_make_flat_cube_stl(tmp_path, 10, 10, 80))
        assert out["success"] is True and "upgrade_hint" not in out


class TestKilnProRankingOnAPlainInstall:
    """A plain ``pip install kiln3d`` has no kiln-pro.  Until 2026-10-18 it
    ranked every caller's poses by bed contact, paying or not: Kiln's own
    ranking was reached only by importing kiln-pro.  Now a Kiln Pro plan asks
    Kiln's servers ONCE per part for the poses' order, best first -- never a
    score (``kiln._pro_orientation_bridge``); a free plan never waits on the
    network; any miss is the floor."""

    def test_a_pro_plan_gets_the_served_order_in_one_ask(self, tmp_path, monkeypatch):
        """The servers' order decides the best pose; the scores shown stay
        the floor's.  Here the servers prefer the LAST candidate."""
        stl = _make_flat_cube_stl(tmp_path, 20, 20, 5)
        _hide_scorer(monkeypatch)
        floor = find_optimal_orientation(stl, candidates=6)

        sent = _served_ranking(
            monkeypatch,
            lambda kw: {"success": True, "status": "ok", "known": True,
                        "order": list(reversed(range(len(kw["candidates"]))))},
        )
        pro = find_optimal_orientation(stl, candidates=6)
        assert len(sent) == 1, "one ask per part, never one per pose"
        poses = sent[0]["candidates"]
        assert len(poses) == 7  # the original pose and the six candidates
        assert all(set(p) == {"bed_contact_pct", "support_pct", "print_height_mm", "overhang_pct"} for p in poses)
        # Candidates are generated X-major over 0, 45, ... degrees, so the
        # sixth (the last one sent) is X=0, Y=225; the floor would not pick it.
        assert (pro.best.rotation_x, pro.best.rotation_y) == (0.0, 225.0)
        assert (floor.best.rotation_x, floor.best.rotation_y) != (0.0, 225.0)
        # No score crosses the wire: every score shown is the floor's own.
        assert pro.original_score == floor.original_score
        assert pro.best.score <= floor.best.score

    def test_a_free_plan_never_asks(self, tmp_path, monkeypatch):
        stl = _make_flat_cube_stl(tmp_path, 20, 20, 5)
        _hide_scorer(monkeypatch)  # fails the test if the servers are asked
        assert find_optimal_orientation(stl).best.score > 0

    @pytest.mark.parametrize(
        "reply",
        [
            {"success": False, "status": "error", "code": "TIER_REQUIRED", "error": "needs Kiln Pro"},
            {"success": True, "status": "ok", "known": False, "order": None},
            {"success": True, "status": "ok", "known": True, "order": [0, 0, 1]},
            {"success": True, "status": "ok", "known": True, "order": "best first"},
            "not a dict",
        ],
    )
    def test_any_miss_is_the_floor(self, tmp_path, monkeypatch, reply):
        stl = _make_flat_cube_stl(tmp_path, 20, 20, 5)
        _hide_scorer(monkeypatch)
        floor = find_optimal_orientation(stl, candidates=6)
        _served_ranking(monkeypatch, reply)
        assert find_optimal_orientation(stl, candidates=6).to_dict() == floor.to_dict()

    def test_servers_that_do_not_answer_are_left_alone_for_a_while(self, tmp_path, monkeypatch):
        stl = _make_flat_cube_stl(tmp_path, 20, 20, 5)

        def offline(kwargs):
            raise ConnectionError("no route to host")

        sent = _served_ranking(monkeypatch, offline)
        for _ in range(3):
            find_optimal_orientation(stl, candidates=4)
        assert len(sent) == 1

