"""Auto-orientation and support estimation for 3D models.

Determines the optimal print orientation to minimize supports, maximize
bed adhesion, and reduce print time. Pure Python implementation using
only stdlib math.
"""

from __future__ import annotations

import math
import os
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiln.generation.validation import _parse_obj, _parse_stl
from kiln.printability import (
    _analyze_bed_adhesion,
    _analyze_overhangs,
    _analyze_supports,
    _normalize,
    _triangle_normal,
)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class OrientationCandidate:
    """A candidate orientation with its printability metrics."""

    rotation_x: float  # degrees
    rotation_y: float  # degrees
    rotation_z: float  # degrees
    score: float  # 0-100, higher is better
    support_volume_mm3: float
    bed_contact_area_mm2: float
    print_height_mm: float
    overhang_percentage: float
    reasoning: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrientationResult:
    """Result of auto-orientation analysis."""

    best: OrientationCandidate
    alternatives: list[OrientationCandidate] = field(default_factory=list)
    original_score: float = 0.0
    improvement_percentage: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SupportEstimate:
    """Estimated support volume for a mesh in its current orientation."""

    estimated_support_volume_mm3: float
    support_percentage: float
    overhang_triangle_count: int
    overhang_percentage: float
    needs_supports: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Rotation math
# ---------------------------------------------------------------------------

_STL_HEADER_SIZE = 80
_STL_TRIANGLE_SIZE = 50


def _rotation_matrix_x(angle_deg: float) -> list[list[float]]:
    """3x3 rotation matrix around X axis."""
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return [
        [1.0, 0.0, 0.0],
        [0.0, c, -s],
        [0.0, s, c],
    ]


def _rotation_matrix_y(angle_deg: float) -> list[list[float]]:
    """3x3 rotation matrix around Y axis."""
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return [
        [c, 0.0, s],
        [0.0, 1.0, 0.0],
        [-s, 0.0, c],
    ]


def _rotation_matrix_z(angle_deg: float) -> list[list[float]]:
    """3x3 rotation matrix around Z axis."""
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return [
        [c, -s, 0.0],
        [s, c, 0.0],
        [0.0, 0.0, 1.0],
    ]


def _mat_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    """Multiply two 3x3 matrices."""
    result = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            for k in range(3):
                result[i][j] += a[i][k] * b[k][j]
    return result


def _apply_rotation(
    vertex: tuple[float, ...],
    matrix: list[list[float]],
) -> tuple[float, float, float]:
    """Apply a 3x3 rotation matrix to a 3D vertex."""
    x = matrix[0][0] * vertex[0] + matrix[0][1] * vertex[1] + matrix[0][2] * vertex[2]
    y = matrix[1][0] * vertex[0] + matrix[1][1] * vertex[1] + matrix[1][2] * vertex[2]
    z = matrix[2][0] * vertex[0] + matrix[2][1] * vertex[1] + matrix[2][2] * vertex[2]
    return (x, y, z)


def _build_rotation_matrix(
    rx: float,
    ry: float,
    rz: float,
) -> list[list[float]]:
    """Build a combined rotation matrix from Euler angles (degrees)."""
    mx = _rotation_matrix_x(rx)
    my = _rotation_matrix_y(ry)
    mz = _rotation_matrix_z(rz)
    return _mat_mul(mz, _mat_mul(my, mx))


def _rotate_triangles(
    triangles: list[tuple[tuple[float, ...], ...]],
    matrix: list[list[float]],
) -> list[tuple[tuple[float, ...], ...]]:
    """Rotate all triangles by a rotation matrix."""
    rotated = []
    for tri in triangles:
        rv = tuple(_apply_rotation(v, matrix) for v in tri)
        rotated.append(rv)
    return rotated


def _translate_to_bed(
    triangles: list[tuple[tuple[float, ...], ...]],
) -> list[tuple[tuple[float, ...], ...]]:
    """Translate triangles so the lowest Z is at Z=0."""
    z_min = float("inf")
    for tri in triangles:
        for v in tri:
            if v[2] < z_min:
                z_min = v[2]

    if abs(z_min) < 1e-9:
        return triangles

    translated = []
    for tri in triangles:
        tv = tuple((v[0], v[1], v[2] - z_min) for v in tri)
        translated.append(tv)
    return translated


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score_orientation(
    triangles: list[tuple[tuple[float, ...], ...]],
    *,
    nozzle_diameter: float = 0.4,
) -> tuple[float, float, float, float, float]:
    """Score a set of triangles in their current orientation.

    Returns (score, support_volume, bed_contact, print_height, overhang_pct).
    """
    # Ensure model sits on the bed.
    triangles = _translate_to_bed(triangles)

    # Collect vertices for bounding box.
    all_verts: list[tuple[float, ...]] = []
    for tri in triangles:
        all_verts.extend(tri)

    xs = [v[0] for v in all_verts]
    ys = [v[1] for v in all_verts]
    zs = [v[2] for v in all_verts]
    bbox = {
        "x_min": min(xs),
        "x_max": max(xs),
        "y_min": min(ys),
        "y_max": max(ys),
        "z_min": min(zs),
        "z_max": max(zs),
    }
    z_min = bbox["z_min"]
    print_height = bbox["z_max"] - z_min

    overhangs = _analyze_overhangs(triangles)
    bed_adhesion = _analyze_bed_adhesion(triangles, z_min, bbox)
    supports = _analyze_supports(triangles, z_min)

    # Normalize metrics to 0-100 scale.
    # Bed contact: higher is better (0-100).
    bed_score = min(100.0, bed_adhesion.contact_percentage * 2)

    # Supports: lower is better (invert).
    support_score = max(0.0, 100.0 - supports.support_percentage)

    # Height: lower is better (less print time).
    # Normalize against a reference (100mm).
    height_score = max(0.0, 100.0 - (print_height / 100.0) * 100.0)
    height_score = max(0.0, min(100.0, height_score))

    # Overhangs: fewer is better.
    overhang_score = max(0.0, 100.0 - overhangs.overhang_percentage)

    # Weighted combination.
    score = bed_score * 0.3 + support_score * 0.3 + height_score * 0.2 + overhang_score * 0.2

    return (
        round(score, 1),
        supports.estimated_support_volume_mm3,
        bed_adhesion.contact_area_mm2,
        round(print_height, 2),
        overhangs.overhang_percentage,
    )


# ---------------------------------------------------------------------------
# Parse helper
# ---------------------------------------------------------------------------


def _parse_mesh(
    file_path: str,
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]]]:
    """Parse an STL or OBJ file."""
    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"File not found: {file_path}")

    ext = path.suffix.lower()
    errors: list[str] = []

    if ext == ".stl":
        triangles, vertices = _parse_stl(path, errors)
    elif ext == ".obj":
        triangles, vertices = _parse_obj(path, errors)
    else:
        raise ValueError(f"Unsupported file type: {ext!r}.")

    if errors:
        raise ValueError(f"Failed to parse mesh: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("Mesh contains no geometry.")

    return triangles, vertices


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_optimal_orientation(
    file_path: str,
    *,
    candidates: int = 24,
    nozzle_diameter: float = 0.4,
) -> OrientationResult:
    """Find the optimal print orientation for a mesh.

    Generates candidate orientations by rotating around X and Y axes
    (0, 45, 90, 135, 180, 225, 270, 315 degrees), scores each, and
    returns the best orientation with alternatives.

    :param file_path: Path to an STL or OBJ file.
    :param candidates: Number of candidate orientations to evaluate.
    :param nozzle_diameter: Printer nozzle diameter in mm.
    :returns: An :class:`OrientationResult`.
    :raises ValueError: If the file cannot be parsed.
    """
    triangles, vertices = _parse_mesh(file_path)

    # Generate candidate rotations.
    angles = [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0]
    candidate_rotations: list[tuple[float, float, float]] = []

    for rx in angles:
        for ry in angles:
            candidate_rotations.append((rx, ry, 0.0))
            if len(candidate_rotations) >= candidates:
                break
        if len(candidate_rotations) >= candidates:
            break

    # Always include the original orientation.
    if (0.0, 0.0, 0.0) not in candidate_rotations:
        candidate_rotations.insert(0, (0.0, 0.0, 0.0))

    # Score the original orientation.
    orig_score, _, _, _, _ = _score_orientation(triangles, nozzle_diameter=nozzle_diameter)

    # Score each candidate.
    scored: list[OrientationCandidate] = []

    for rx, ry, rz in candidate_rotations:
        matrix = _build_rotation_matrix(rx, ry, rz)
        rotated = _rotate_triangles(triangles, matrix)

        score, support_vol, bed_contact, height, overhang_pct = _score_orientation(
            rotated, nozzle_diameter=nozzle_diameter
        )

        reasoning_parts = []
        if bed_contact > 0:
            reasoning_parts.append(f"bed contact: {bed_contact:.1f} mm2")
        reasoning_parts.append(f"height: {height:.1f} mm")
        if overhang_pct > 0:
            reasoning_parts.append(f"overhangs: {overhang_pct:.1f}%")

        scored.append(
            OrientationCandidate(
                rotation_x=rx,
                rotation_y=ry,
                rotation_z=rz,
                score=score,
                support_volume_mm3=support_vol,
                bed_contact_area_mm2=bed_contact,
                print_height_mm=height,
                overhang_percentage=overhang_pct,
                reasoning=", ".join(reasoning_parts),
            )
        )

    # Sort by score descending.
    scored.sort(key=lambda c: c.score, reverse=True)

    best = scored[0]
    alternatives = scored[1:4]  # Top 3 alternatives

    improvement = ((best.score - orig_score) / orig_score * 100.0) if orig_score > 0 else 0.0

    return OrientationResult(
        best=best,
        alternatives=alternatives,
        original_score=orig_score,
        improvement_percentage=round(improvement, 1),
    )


def apply_orientation(
    file_path: str,
    rotation_x: float,
    rotation_y: float,
    rotation_z: float,
    *,
    output_path: str | None = None,
) -> str:
    """Apply a rotation to a mesh and write a new STL file.

    :param file_path: Path to the input STL or OBJ file.
    :param rotation_x: Rotation around X axis in degrees.
    :param rotation_y: Rotation around Y axis in degrees.
    :param rotation_z: Rotation around Z axis in degrees.
    :param output_path: Output file path.  Defaults to
        ``<input>_oriented.stl``.
    :returns: Path to the reoriented STL file.
    :raises ValueError: If the file cannot be parsed.
    """
    triangles, vertices = _parse_mesh(file_path)

    matrix = _build_rotation_matrix(rotation_x, rotation_y, rotation_z)
    rotated = _rotate_triangles(triangles, matrix)

    # Translate so Z_min = 0 (sit on the bed).
    rotated = _translate_to_bed(rotated)

    if output_path is None:
        p = Path(file_path)
        output_path = str(p.with_stem(p.stem + "_oriented").with_suffix(".stl"))

    _write_binary_stl(rotated, output_path)
    return output_path


def estimate_supports(
    file_path: str,
    *,
    max_overhang_angle: float = 45.0,
) -> SupportEstimate:
    """Estimate support volume for a mesh in its current orientation.

    :param file_path: Path to an STL or OBJ file.
    :param max_overhang_angle: Max overhang angle (degrees) before
        supports are needed.
    :returns: A :class:`SupportEstimate`.
    :raises ValueError: If the file cannot be parsed.
    """
    triangles, vertices = _parse_mesh(file_path)

    z_min = min(v[2] for v in vertices)

    overhangs = _analyze_overhangs(triangles, max_overhang_angle=max_overhang_angle)
    supports = _analyze_supports(triangles, z_min, max_overhang_angle=max_overhang_angle)

    return SupportEstimate(
        estimated_support_volume_mm3=supports.estimated_support_volume_mm3,
        support_percentage=supports.support_percentage,
        overhang_triangle_count=overhangs.overhang_triangle_count,
        overhang_percentage=overhangs.overhang_percentage,
        needs_supports=overhangs.needs_supports,
    )


# ---------------------------------------------------------------------------
# STL writing
# ---------------------------------------------------------------------------


def _write_binary_stl(
    triangles: list[tuple[tuple[float, ...], ...]],
    output_path: str,
) -> None:
    """Write triangles to a binary STL file."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as fh:
        # 80-byte header.
        fh.write(b"\x00" * _STL_HEADER_SIZE)
        # Triangle count as uint32 LE.
        fh.write(struct.pack("<I", len(triangles)))

        for tri in triangles:
            # Compute normal.
            n = _triangle_normal(tri[0], tri[1], tri[2])
            nn = _normalize(n)
            fh.write(struct.pack("<3f", nn[0], nn[1], nn[2]))
            # Three vertices.
            for v in tri:
                fh.write(struct.pack("<3f", v[0], v[1], v[2]))
            # Attribute byte count.
            fh.write(struct.pack("<H", 0))
