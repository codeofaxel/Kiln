"""Tests for kiln.assembly -- multi-part assembly module."""
from __future__ import annotations

import json
import os
import struct

import pytest

from kiln.assembly import (
    _MAX_FREE_TIER_PARTS,
    Assembly,
    AssemblyPart,
    ClearanceCheck,
    JointValidation,
    MatingInterface,
    check_clearance,
    compose_assembly,
    create_assembly,
    get_clearance_recommendation,
    validate_assembly,
    validate_joint,
)

# ---------------------------------------------------------------------------
# STL helper
# ---------------------------------------------------------------------------

def _write_box_stl(
    path: str,
    x: float,
    y: float,
    z: float,
    *,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    offset_z: float = 0.0,
) -> None:
    """Write a minimal binary STL box at given dimensions with optional position offset."""
    x2, y2 = x / 2, y / 2
    x_lo, x_hi = offset_x - x2, offset_x + x2
    y_lo, y_hi = offset_y - y2, offset_y + y2
    z_lo, z_hi = offset_z, offset_z + z
    verts = [
        (x_lo, y_lo, z_lo), (x_hi, y_lo, z_lo), (x_hi, y_hi, z_lo), (x_lo, y_hi, z_lo),
        (x_lo, y_lo, z_hi), (x_hi, y_lo, z_hi), (x_hi, y_hi, z_hi), (x_lo, y_hi, z_hi),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3),
    ]
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(faces)))
        for face in faces:
            v0, v1, v2 = verts[face[0]], verts[face[1]], verts[face[2]]
            f.write(struct.pack("<fff", 0, 0, 0))
            for v in (v0, v1, v2):
                f.write(struct.pack("<fff", *v))
            f.write(struct.pack("<H", 0))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def box_stl(tmp_path):
    """Return a path to a 10x10x10 box STL."""
    p = str(tmp_path / "box.stl")
    _write_box_stl(p, 10, 10, 10)
    return p


@pytest.fixture()
def two_box_stls(tmp_path):
    """Return paths to two 10x10x10 box STLs."""
    a = str(tmp_path / "box_a.stl")
    b = str(tmp_path / "box_b.stl")
    _write_box_stl(a, 10, 10, 10)
    _write_box_stl(b, 10, 10, 10)
    return a, b


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateAssembly:
    def test_create_assembly(self):
        """create_assembly returns Assembly with empty parts, valid=True, non-empty id."""
        asm = create_assembly("test")
        assert isinstance(asm, Assembly)
        assert asm.name == "test"
        assert asm.parts == []
        assert asm.overall_valid is True
        assert asm.assembly_id  # non-empty


class TestAddPart:
    def test_add_part(self, box_stl):
        """Adding a part stores it with correct attributes."""
        asm = create_assembly("test")
        part = AssemblyPart(part_id="p1", file_path=box_stl)
        asm.add_part(part)
        assert len(asm.parts) == 1
        assert asm.parts[0].part_id == "p1"
        assert asm.parts[0].file_path == box_stl
        assert asm.parts[0].material == "PLA"
        assert asm.parts[0].role == "structural"

    def test_add_duplicate_part_raises(self, box_stl):
        """Adding two parts with the same part_id raises ValueError."""
        asm = create_assembly("test")
        part = AssemblyPart(part_id="p1", file_path=box_stl)
        asm.add_part(part)
        with pytest.raises(ValueError):
            asm.add_part(AssemblyPart(part_id="p1", file_path=box_stl))


class TestAddInterface:
    def test_add_interface(self, two_box_stls):
        """Adding an interface between two existing parts succeeds."""
        a_path, b_path = two_box_stls
        asm = create_assembly("test")
        asm.add_part(AssemblyPart(part_id="a", file_path=a_path))
        asm.add_part(AssemblyPart(part_id="b", file_path=b_path))
        iface = MatingInterface(part_a_id="a", part_b_id="b", joint_type="snap_fit")
        asm.add_interface(iface)
        assert len(asm.interfaces) == 1
        assert asm.interfaces[0].joint_type == "snap_fit"

    def test_add_interface_invalid_part_raises(self, box_stl):
        """Adding interface referencing a non-existent part_id raises ValueError."""
        asm = create_assembly("test")
        asm.add_part(AssemblyPart(part_id="a", file_path=box_stl))
        iface = MatingInterface(part_a_id="a", part_b_id="missing", joint_type="snap_fit")
        with pytest.raises(ValueError):
            asm.add_interface(iface)


class TestClearance:
    def test_clearance_no_overlap(self, tmp_path):
        """Two separated boxes have no overlap and positive min_clearance."""
        stl_a = str(tmp_path / "a.stl")
        stl_b = str(tmp_path / "b.stl")
        _write_box_stl(stl_a, 10, 10, 10)
        _write_box_stl(stl_b, 10, 10, 10)

        asm = create_assembly("test")
        asm.add_part(AssemblyPart(part_id="a", file_path=stl_a, position_mm=(0, 0, 0)))
        asm.add_part(AssemblyPart(part_id="b", file_path=stl_b, position_mm=(50, 0, 0)))

        result = check_clearance(asm, "a", "b", required_clearance_mm=0.2)
        assert isinstance(result, ClearanceCheck)
        assert result.overlaps is False
        assert result.min_clearance_mm > 0
        assert result.clearance_adequate is True

    def test_clearance_overlap(self, tmp_path):
        """Two boxes at the same position overlap."""
        stl_a = str(tmp_path / "a.stl")
        stl_b = str(tmp_path / "b.stl")
        _write_box_stl(stl_a, 10, 10, 10)
        _write_box_stl(stl_b, 10, 10, 10)

        asm = create_assembly("test")
        asm.add_part(AssemblyPart(part_id="a", file_path=stl_a, position_mm=(0, 0, 0)))
        asm.add_part(AssemblyPart(part_id="b", file_path=stl_b, position_mm=(0, 0, 0)))

        result = check_clearance(asm, "a", "b")
        assert result.overlaps is True
        assert result.clearance_adequate is False

    def test_clearance_adjacent(self, tmp_path):
        """Two 20mm boxes touching faces should have ~0mm clearance."""
        stl_a = str(tmp_path / "a.stl")
        stl_b = str(tmp_path / "b.stl")
        _write_box_stl(stl_a, 20, 20, 20)
        _write_box_stl(stl_b, 20, 20, 20)

        asm = create_assembly("test")
        asm.add_part(AssemblyPart(part_id="a", file_path=stl_a, position_mm=(0, 0, 0)))
        asm.add_part(AssemblyPart(part_id="b", file_path=stl_b, position_mm=(20, 0, 0)))

        result = check_clearance(asm, "a", "b")
        # Touching faces: clearance should be approximately 0
        assert result.min_clearance_mm == pytest.approx(0.0, abs=1.0)


class TestJointValidation:
    def test_validate_snap_fit_joint(self, two_box_stls):
        """snap_fit with 0.2mm clearance and PLA should be valid."""
        a_path, b_path = two_box_stls
        iface = MatingInterface(
            part_a_id="a",
            part_b_id="b",
            joint_type="snap_fit",
            clearance_mm=0.2,
            tolerance_mm=0.1,
        )
        parts_dict = {
            "a": AssemblyPart(part_id="a", file_path=a_path, material="PLA"),
            "b": AssemblyPart(part_id="b", file_path=b_path, material="PLA"),
        }
        result = validate_joint(iface, parts_dict)
        assert isinstance(result, JointValidation)
        assert result.valid is True
        assert result.joint_type == "snap_fit"

    def test_validate_press_fit_joint(self, two_box_stls):
        """press_fit with -0.1mm clearance (interference) should be valid."""
        a_path, b_path = two_box_stls
        iface = MatingInterface(
            part_a_id="a",
            part_b_id="b",
            joint_type="press_fit",
            clearance_mm=-0.1,
            tolerance_mm=0.1,
        )
        parts_dict = {
            "a": AssemblyPart(part_id="a", file_path=a_path, material="PLA"),
            "b": AssemblyPart(part_id="b", file_path=b_path, material="PLA"),
        }
        result = validate_joint(iface, parts_dict)
        assert isinstance(result, JointValidation)
        assert result.valid is True
        assert result.joint_type == "press_fit"


class TestValidateAssembly:
    def test_validate_assembly_all_clear(self, tmp_path):
        """Well-separated parts with valid interface -> overall_valid True."""
        stl_a = str(tmp_path / "a.stl")
        stl_b = str(tmp_path / "b.stl")
        _write_box_stl(stl_a, 10, 10, 10)
        _write_box_stl(stl_b, 10, 10, 10)

        asm = create_assembly("test")
        asm.add_part(AssemblyPart(part_id="a", file_path=stl_a, position_mm=(0, 0, 0)))
        asm.add_part(AssemblyPart(part_id="b", file_path=stl_b, position_mm=(50, 0, 0)))
        asm.add_interface(MatingInterface(
            part_a_id="a", part_b_id="b", joint_type="snap_fit",
            clearance_mm=0.2, tolerance_mm=0.1,
        ))

        result = validate_assembly(asm)
        assert isinstance(result, Assembly)
        assert result.overall_valid is True

    def test_validate_assembly_with_issues(self, tmp_path):
        """Overlapping parts -> overall_valid False."""
        stl_a = str(tmp_path / "a.stl")
        stl_b = str(tmp_path / "b.stl")
        _write_box_stl(stl_a, 10, 10, 10)
        _write_box_stl(stl_b, 10, 10, 10)

        asm = create_assembly("test")
        asm.add_part(AssemblyPart(part_id="a", file_path=stl_a, position_mm=(0, 0, 0)))
        asm.add_part(AssemblyPart(part_id="b", file_path=stl_b, position_mm=(0, 0, 0)))

        result = validate_assembly(asm)
        assert result.overall_valid is False


class TestComposeAssembly:
    def test_compose_assembly(self, tmp_path):
        """Composing two boxes produces output file with 24 total triangles."""
        stl_a = str(tmp_path / "a.stl")
        stl_b = str(tmp_path / "b.stl")
        output = str(tmp_path / "composed.stl")
        _write_box_stl(stl_a, 10, 10, 10)
        _write_box_stl(stl_b, 10, 10, 10)

        asm = create_assembly("test")
        asm.add_part(AssemblyPart(part_id="a", file_path=stl_a, position_mm=(0, 0, 0)))
        asm.add_part(AssemblyPart(part_id="b", file_path=stl_b, position_mm=(30, 0, 0)))

        result = compose_assembly(asm, output)
        assert isinstance(result, dict)
        assert os.path.exists(output)
        assert result["total_triangles"] == 24  # 12 per box


class TestClearanceRecommendation:
    def test_get_clearance_recommendation(self):
        """snap_fit + PLA returns dict with positive recommended_clearance_mm."""
        rec = get_clearance_recommendation("snap_fit", material_a="PLA", material_b="PLA")
        assert isinstance(rec, dict)
        assert rec["recommended_clearance_mm"] > 0


class TestSerialization:
    def test_assembly_to_dict_roundtrip(self, two_box_stls):
        """to_dict -> from_dict preserves assembly structure."""
        a_path, b_path = two_box_stls
        asm = create_assembly("roundtrip_test")
        asm.add_part(AssemblyPart(part_id="a", file_path=a_path, material="PLA"))
        asm.add_part(AssemblyPart(part_id="b", file_path=b_path, material="PETG"))
        asm.add_interface(MatingInterface(
            part_a_id="a", part_b_id="b", joint_type="snap_fit",
            clearance_mm=0.2, tolerance_mm=0.1,
        ))

        data = asm.to_dict()
        assert isinstance(data, dict)
        # Ensure it's JSON-serializable
        json_str = json.dumps(data)
        assert json_str

        restored = Assembly.from_dict(data)
        assert restored.name == asm.name
        assert restored.assembly_id == asm.assembly_id
        assert len(restored.parts) == len(asm.parts)
        assert len(restored.interfaces) == len(asm.interfaces)
        assert restored.parts[0].part_id == "a"
        assert restored.parts[1].material == "PETG"
        assert restored.interfaces[0].joint_type == "snap_fit"

    def test_assembly_from_dict(self):
        """from_dict correctly reconstructs Assembly with all fields."""
        data = {
            "assembly_id": "asm-123",
            "name": "from_dict_test",
            "parts": [
                {
                    "part_id": "p1",
                    "file_path": "/tmp/fake.stl",
                    "position_mm": (0, 0, 0),
                    "rotation_deg": (0, 0, 0),
                    "material": "ABS",
                    "role": "cosmetic",
                },
            ],
            "interfaces": [
            ],
            "clearance_checks": [],
            "joint_validations": [],
            "overall_valid": True,
            "recommendations": ["Check tolerances"],
        }
        asm = Assembly.from_dict(data)
        assert asm.assembly_id == "asm-123"
        assert asm.name == "from_dict_test"
        assert len(asm.parts) == 1
        assert asm.parts[0].material == "ABS"
        assert asm.parts[0].role == "cosmetic"
        assert asm.overall_valid is True
        assert "Check tolerances" in asm.recommendations


class TestMaterialCompatibility:
    def test_material_compatibility_check(self, two_box_stls):
        """snap_fit with TPU material should produce warnings about flexibility."""
        a_path, b_path = two_box_stls
        iface = MatingInterface(
            part_a_id="a",
            part_b_id="b",
            joint_type="snap_fit",
            clearance_mm=0.2,
            tolerance_mm=0.1,
        )
        parts_dict = {
            "a": AssemblyPart(part_id="a", file_path=a_path, material="TPU"),
            "b": AssemblyPart(part_id="b", file_path=b_path, material="TPU"),
        }
        result = validate_joint(iface, parts_dict)
        assert isinstance(result, JointValidation)
        # TPU is flexible; snap_fit joints should produce warnings/recommendations
        has_flex_warning = any(
            "flex" in r.lower() or "tpu" in r.lower() or "elastic" in r.lower()
            for r in result.recommendations + result.issues
        )
        assert has_flex_warning, (
            f"Expected flexibility warning for TPU snap_fit, got: "
            f"issues={result.issues}, recommendations={result.recommendations}"
        )


class TestMaxPartsLimit:
    def test_max_parts_limit(self, tmp_path):
        """Adding more than _MAX_FREE_TIER_PARTS parts raises ValueError."""
        asm = create_assembly("limit_test")
        for i in range(_MAX_FREE_TIER_PARTS):
            stl = str(tmp_path / f"box_{i}.stl")
            _write_box_stl(stl, 5, 5, 5)
            asm.add_part(AssemblyPart(part_id=f"p{i}", file_path=stl))

        # The next one should exceed the limit
        extra_stl = str(tmp_path / "extra.stl")
        _write_box_stl(extra_stl, 5, 5, 5)
        with pytest.raises(ValueError):
            asm.add_part(AssemblyPart(part_id="overflow", file_path=extra_stl))
