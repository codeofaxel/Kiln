"""Auto-orientation and support estimation for 3D models.

Determines the optimal print orientation to minimize supports, maximize
bed adhesion, and reduce print time. Pure Python implementation using
only stdlib math.
"""

from __future__ import annotations

import math
import os
import struct
import xml.etree.ElementTree as ET
import zipfile
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


@dataclass
class StabilityResult:
    """Stability assessment for a model in its current orientation."""

    stable: bool  # True if orientation is safe to print
    risk_level: str  # "low", "medium", "high"
    height_mm: float
    base_footprint_mm2: float  # approximate bed contact area
    height_to_base_ratio: float  # key metric — high = unstable
    center_of_gravity_z_mm: float  # higher CoG = more tippy
    recommendation: str  # human-readable advice
    suggested_rotation: dict[str, float] | None  # {"x": 90, "y": 0, "z": 0} if reorientation would help

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


def check_stability(
    file_path: str,
    *,
    max_height_to_base_ratio: float = 3.0,
) -> StabilityResult:
    """Analyze whether a model's current orientation is stable for printing.

    Computes the height-to-base ratio (height divided by the square root of
    the bed contact footprint area) to detect tall, narrow orientations that
    are prone to wobble or topple mid-print.

    :param file_path: Path to an STL or OBJ file.
    :param max_height_to_base_ratio: Ratio at or above which the model is
        considered high-risk.  Defaults to ``3.0``.
    :returns: A :class:`StabilityResult`.
    :raises ValueError: If the file cannot be parsed or is an unsupported
        format (e.g. 3MF).
    """
    ext = Path(file_path).suffix.lower()
    if ext == ".3mf":
        raise ValueError(
            "3MF stability analysis is not yet supported. "
            "Export the model as STL or OBJ first."
        )

    triangles, vertices = _parse_mesh(file_path)

    # Translate so the lowest Z sits at Z=0.
    triangles = _translate_to_bed(triangles)

    # Recompute vertices after translation.
    all_verts: list[tuple[float, ...]] = []
    for tri in triangles:
        all_verts.extend(tri)

    # ---- Bounding box (Z extent only) ----
    zs = [v[2] for v in all_verts]
    z_min = min(zs)
    z_max = max(zs)
    height_mm = z_max - z_min

    # ---- Center of gravity (vertex average approximation) ----
    center_of_gravity_z_mm = sum(zs) / len(zs) if zs else 0.0

    # ---- Base footprint area ----
    # Sum the XY-projected area of triangles whose lowest vertex is within
    # 0.5mm of the bed (Z=0).
    bed_threshold = z_min + 0.5
    base_footprint_mm2 = 0.0
    for tri in triangles:
        tri_z_min = min(v[2] for v in tri)
        if tri_z_min <= bed_threshold:
            # Projected area onto XY plane using the cross product method.
            ax, ay = tri[0][0], tri[0][1]
            bx, by = tri[1][0], tri[1][1]
            cx, cy = tri[2][0], tri[2][1]
            projected_area = abs(
                (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
            ) / 2.0
            base_footprint_mm2 += projected_area

    # Guard against degenerate models with zero footprint.
    if base_footprint_mm2 < 1e-6:
        base_footprint_mm2 = 1e-6

    # ---- Height-to-base ratio ----
    # sqrt converts area to a linear dimension for a fair comparison with
    # height.
    height_to_base_ratio = height_mm / math.sqrt(base_footprint_mm2)

    # ---- Risk level ----
    if height_to_base_ratio < 2.0:
        risk_level = "low"
    elif height_to_base_ratio < max_height_to_base_ratio:
        risk_level = "medium"
    else:
        risk_level = "high"

    stable = risk_level == "low"

    # ---- Suggested rotation for high-risk models ----
    suggested_rotation: dict[str, float] | None = None
    if risk_level == "high":
        try:
            orientation = find_optimal_orientation(file_path)
            best = orientation.best
            # Only suggest if the best orientation is meaningfully shorter.
            if best.print_height_mm < height_mm * 0.75:
                suggested_rotation = {
                    "x": best.rotation_x,
                    "y": best.rotation_y,
                    "z": best.rotation_z,
                }
        except (ValueError, OSError):
            # If orientation analysis fails, skip the suggestion.
            pass

    # ---- Recommendation text ----
    if risk_level == "low":
        recommendation = (
            "Orientation looks stable. Good bed contact relative to height."
        )
    elif risk_level == "medium":
        recommendation = (
            "Borderline stability. Consider adding a brim (5-8mm) for extra adhesion."
        )
    else:
        recommendation = (
            "High wobble risk — tall part on small base. "
            "Reorienting to lay flat is strongly recommended."
        )
        if suggested_rotation is not None:
            recommendation += (
                f" Try rotating X={suggested_rotation['x']:.0f}°, "
                f"Y={suggested_rotation['y']:.0f}°, "
                f"Z={suggested_rotation['z']:.0f}°."
            )

    return StabilityResult(
        stable=stable,
        risk_level=risk_level,
        height_mm=round(height_mm, 2),
        base_footprint_mm2=round(base_footprint_mm2, 2),
        height_to_base_ratio=round(height_to_base_ratio, 2),
        center_of_gravity_z_mm=round(center_of_gravity_z_mm, 2),
        recommendation=recommendation,
        suggested_rotation=suggested_rotation,
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


# ---------------------------------------------------------------------------
# Multi-copy plate duplication
# ---------------------------------------------------------------------------


def duplicate_stl_on_plate(
    file_path: str,
    count: int,
    *,
    spacing_mm: float = 10.0,
    bed_width_mm: float = 256.0,
    bed_depth_mm: float = 256.0,
    output_path: str | None = None,
) -> str:
    """Duplicate an STL model into *count* copies arranged in a grid on the build plate.

    Parses the source STL, computes its bounding box, arranges copies in a
    grid with *spacing_mm* between them, validates they fit within the build
    volume, and writes a single merged STL with all copies.

    :param file_path: Path to the source STL file.
    :param count: Number of copies (must be >= 2).
    :param spacing_mm: Minimum gap between copies in mm.
    :param bed_width_mm: Build plate width (X) in mm.
    :param bed_depth_mm: Build plate depth (Y) in mm.
    :param output_path: Where to write the output STL.  If ``None``, a
        temp file is created.
    :returns: Path to the output STL with all copies.
    :raises ValueError: If copies don't fit on the bed or file is invalid.
    """
    if count < 2:
        raise ValueError(f"count must be >= 2, got {count}")

    triangles, _vertices = _parse_mesh(file_path)
    triangles = _translate_to_bed(triangles)

    # --- Compute bounding box ---
    all_verts: list[tuple[float, ...]] = []
    for tri in triangles:
        all_verts.extend(tri)

    xs = [v[0] for v in all_verts]
    ys = [v[1] for v in all_verts]

    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    model_width = x_max - x_min
    model_depth = y_max - y_min

    if model_width <= 0 or model_depth <= 0:
        raise ValueError("Model has zero width or depth.")

    # --- Center model at origin ---
    center_x = (x_min + x_max) / 2.0
    center_y = (y_min + y_max) / 2.0

    centered: list[tuple[tuple[float, ...], ...]] = []
    for tri in triangles:
        tv = tuple((v[0] - center_x, v[1] - center_y, v[2]) for v in tri)
        centered.append(tv)

    # --- Calculate grid layout ---
    cell_w = model_width + spacing_mm
    cell_d = model_depth + spacing_mm

    cols = int(bed_width_mm // cell_w)
    rows = int(bed_depth_mm // cell_d)

    if cols < 1 or rows < 1:
        raise ValueError(
            f"Model ({model_width:.1f} x {model_depth:.1f} mm) is too large "
            f"for the build plate ({bed_width_mm} x {bed_depth_mm} mm)."
        )

    if count > cols * rows:
        raise ValueError(
            f"Cannot fit {count} copies on {bed_width_mm}x{bed_depth_mm} mm bed. "
            f"Max {cols * rows} copies ({cols} cols x {rows} rows) with "
            f"{spacing_mm} mm spacing."
        )

    # --- Generate copy positions (centered on bed) ---
    # Determine how many columns/rows we actually need.
    actual_cols = min(count, cols)
    actual_rows = math.ceil(count / actual_cols)

    grid_width = actual_cols * cell_w - spacing_mm
    grid_depth = actual_rows * cell_d - spacing_mm

    start_x = (bed_width_mm - grid_width) / 2.0 + model_width / 2.0
    start_y = (bed_depth_mm - grid_depth) / 2.0 + model_depth / 2.0

    # --- Build merged triangles ---
    merged: list[tuple[tuple[float, ...], ...]] = []
    placed = 0
    for row in range(actual_rows):
        for col in range(actual_cols):
            if placed >= count:
                break
            offset_x = start_x + col * cell_w
            offset_y = start_y + row * cell_d
            for tri in centered:
                tv = tuple(
                    (v[0] + offset_x, v[1] + offset_y, v[2]) for v in tri
                )
                merged.append(tv)
            placed += 1

    # --- Write output ---
    if output_path is None:
        import tempfile as _tempfile

        fd, output_path = _tempfile.mkstemp(suffix="_multi.stl")
        os.close(fd)

    _write_binary_stl(merged, output_path)
    return output_path


# ---------------------------------------------------------------------------
# File-level rotation — STL
# ---------------------------------------------------------------------------


def rotate_stl_file(
    input_path: str,
    output_path: str,
    *,
    rotation_z: float = 0.0,
    rotation_x: float = 0.0,
    rotation_y: float = 0.0,
) -> str:
    """Rotate an STL file by Euler angles (degrees) and save to *output_path*.

    Reuses the existing mesh parser and binary STL writer.  The model is
    translated so its lowest Z sits on the bed after rotation.

    :param input_path: Path to the source STL file.
    :param output_path: Where to write the rotated STL.
    :param rotation_z: Rotation around Z axis in degrees.
    :param rotation_x: Rotation around X axis in degrees.
    :param rotation_y: Rotation around Y axis in degrees.
    :returns: *output_path*.
    :raises ValueError: If the file cannot be parsed.
    """
    triangles, _vertices = _parse_mesh(input_path)

    matrix = _build_rotation_matrix(rotation_x, rotation_y, rotation_z)
    rotated = _rotate_triangles(triangles, matrix)
    rotated = _translate_to_bed(rotated)

    _write_binary_stl(rotated, output_path)
    return output_path


# ---------------------------------------------------------------------------
# File-level rotation — 3MF
# ---------------------------------------------------------------------------

_3MF_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_3MF_MODEL_PATH = "3D/3dmodel.model"


def _parse_3mf_transform(attr: str) -> list[list[float]]:
    """Parse a 3MF ``transform`` attribute into a 4x3 matrix.

    The attribute contains 12 floats in row-major order:
    ``m00 m01 m02 m10 m11 m12 m20 m21 m22 tx ty tz``.

    Returns a list of 4 rows of 3 values each:
    ``[[m00,m01,m02],[m10,m11,m12],[m20,m21,m22],[tx,ty,tz]]``.
    """
    vals = [float(v) for v in attr.strip().split()]
    if len(vals) != 12:
        raise ValueError(f"Expected 12 values in transform, got {len(vals)}")
    return [
        vals[0:3],
        vals[3:6],
        vals[6:9],
        vals[9:12],
    ]


def _identity_3mf_transform() -> list[list[float]]:
    """Return the identity 4x3 transform."""
    return [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
        [0.0, 0.0, 0.0],
    ]


def _compose_3mf_transform(
    rot: list[list[float]],
    existing: list[list[float]],
) -> list[list[float]]:
    """Compose a 3x3 rotation matrix with an existing 4x3 3MF transform.

    The 3x3 rotation is applied to the top-left 3x3 block and to the
    translation row, producing a new 4x3 transform.
    """
    rot3 = existing[:3]  # 3x3 rotation part
    t = existing[3]      # translation row

    new_rot = _mat_mul(rot, rot3)
    # Rotate the translation vector too.
    new_t = _apply_rotation(tuple(t), rot)

    return [
        new_rot[0],
        new_rot[1],
        new_rot[2],
        list(new_t),
    ]


def _format_3mf_transform(mat: list[list[float]]) -> str:
    """Format a 4x3 matrix back into a 3MF transform attribute string."""
    vals = mat[0] + mat[1] + mat[2] + mat[3]
    return " ".join(f"{v:.10g}" for v in vals)


def rotate_3mf_file(
    input_path: str,
    output_path: str,
    *,
    rotation_z: float = 0.0,
    rotation_x: float = 0.0,
    rotation_y: float = 0.0,
) -> str:
    """Rotate all objects in a 3MF file by updating item transforms.

    Copies the 3MF archive to *output_path*, then modifies the
    ``3D/3dmodel.model`` XML to compose the requested rotation with each
    ``<item>`` element's existing ``transform`` attribute.

    :param input_path: Path to the source 3MF file.
    :param output_path: Where to write the rotated 3MF.
    :param rotation_z: Rotation around Z axis in degrees.
    :param rotation_x: Rotation around X axis in degrees.
    :param rotation_y: Rotation around Y axis in degrees.
    :returns: *output_path*.
    :raises ValueError: If the 3MF is missing a model file or build section.
    :raises FileNotFoundError: If *input_path* does not exist.
    """
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"File not found: {input_path}")

    # Register the namespace so ET doesn't mangle it on write.
    ET.register_namespace("", _3MF_NS)

    # Read the model XML from the source archive.
    with zipfile.ZipFile(input_path, "r") as zin:
        if _3MF_MODEL_PATH not in zin.namelist():
            raise ValueError(
                f"3MF archive missing {_3MF_MODEL_PATH}: not a valid 3MF file"
            )
        model_xml = zin.read(_3MF_MODEL_PATH)

    root = ET.fromstring(model_xml)
    ns = {"m": _3MF_NS}

    build = root.find(".//m:build", ns)
    if build is None:
        raise ValueError("3MF model XML missing <build> section")

    items = build.findall("m:item", ns)
    rot_matrix = _build_rotation_matrix(rotation_x, rotation_y, rotation_z)

    for item in items:
        existing_attr = item.get("transform")
        if existing_attr:
            existing = _parse_3mf_transform(existing_attr)
        else:
            existing = _identity_3mf_transform()

        new_transform = _compose_3mf_transform(rot_matrix, existing)
        item.set("transform", _format_3mf_transform(new_transform))

    updated_xml = ET.tostring(root, encoding="unicode", xml_declaration=True)

    # Build the output 3MF: copy every entry except the model XML from
    # the source archive, then write the updated model XML.
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with zipfile.ZipFile(input_path, "r") as zin, zipfile.ZipFile(
        output_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as zout:
        for entry in zin.infolist():
            if entry.filename == _3MF_MODEL_PATH:
                continue
            zout.writestr(entry, zin.read(entry.filename))
        zout.writestr(_3MF_MODEL_PATH, updated_xml)

    return output_path
