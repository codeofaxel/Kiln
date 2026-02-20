"""Printability analysis engine for 3D models.

Analyzes STL/OBJ meshes for FDM printing readiness: overhang detection,
thin wall analysis, bridging assessment, bed adhesion surface estimation,
and support volume estimation. Uses only stdlib (struct, math) -- no
external mesh libraries.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiln.generation.validation import _parse_obj, _parse_stl

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class OverhangAnalysis:
    """Results of overhang detection."""

    max_overhang_angle: float  # degrees
    overhang_triangle_count: int
    overhang_percentage: float  # % of total triangles
    needs_supports: bool
    worst_regions: list[dict[str, float]]  # [{x, y, z, angle}]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ThinWallAnalysis:
    """Results of thin wall detection."""

    min_wall_thickness_mm: float
    thin_wall_count: int  # walls below nozzle diameter
    thin_wall_percentage: float
    problematic_regions: list[dict[str, float]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BridgingAnalysis:
    """Results of bridging assessment."""

    max_bridge_length_mm: float
    bridge_count: int
    needs_supports_for_bridges: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BedAdhesionAnalysis:
    """Results of bed adhesion surface estimation."""

    contact_area_mm2: float
    contact_percentage: float  # % of bounding box footprint
    adhesion_risk: str  # "low", "medium", "high"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SupportAnalysis:
    """Results of support volume estimation."""

    estimated_support_volume_mm3: float
    support_percentage: float  # % of model volume
    support_regions: list[dict[str, float]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrintabilityReport:
    """Full printability analysis report."""

    printable: bool
    score: int  # 0-100
    grade: str  # A/B/C/D/F
    overhangs: OverhangAnalysis
    thin_walls: ThinWallAnalysis
    bridging: BridgingAnalysis
    bed_adhesion: BedAdhesionAnalysis
    supports: SupportAnalysis
    recommendations: list[str] = field(default_factory=list)
    estimated_print_time_modifier: float = 1.0  # 1.0 = normal

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Internal geometry helpers
# ---------------------------------------------------------------------------


def _triangle_normal(
    v1: tuple[float, ...],
    v2: tuple[float, ...],
    v3: tuple[float, ...],
) -> tuple[float, float, float]:
    """Compute the normal vector of a triangle via cross product."""
    # Edge vectors
    e1 = (v2[0] - v1[0], v2[1] - v1[1], v2[2] - v1[2])
    e2 = (v3[0] - v1[0], v3[1] - v1[1], v3[2] - v1[2])
    # Cross product
    nx = e1[1] * e2[2] - e1[2] * e2[1]
    ny = e1[2] * e2[0] - e1[0] * e2[2]
    nz = e1[0] * e2[1] - e1[1] * e2[0]
    return (nx, ny, nz)


def _normalize(v: tuple[float, float, float]) -> tuple[float, float, float]:
    """Normalize a 3D vector to unit length."""
    length = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    if length < 1e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / length, v[1] / length, v[2] / length)


def _triangle_area(
    v1: tuple[float, ...],
    v2: tuple[float, ...],
    v3: tuple[float, ...],
) -> float:
    """Compute the area of a triangle from its vertices."""
    n = _triangle_normal(v1, v2, v3)
    return 0.5 * math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)


def _triangle_centroid(
    v1: tuple[float, ...],
    v2: tuple[float, ...],
    v3: tuple[float, ...],
) -> tuple[float, float, float]:
    """Compute the centroid of a triangle."""
    return (
        (v1[0] + v2[0] + v3[0]) / 3.0,
        (v1[1] + v2[1] + v3[1]) / 3.0,
        (v1[2] + v2[2] + v3[2]) / 3.0,
    )


def _signed_volume_of_triangle(
    v1: tuple[float, ...],
    v2: tuple[float, ...],
    v3: tuple[float, ...],
) -> float:
    """Compute the signed volume contribution of a triangle to a mesh volume.

    Uses the divergence theorem: V = (1/6) * sum(dot(v1, cross(v2, v3)))
    for each triangle.
    """
    return (
        v1[0] * (v2[1] * v3[2] - v2[2] * v3[1])
        + v1[1] * (v2[2] * v3[0] - v2[0] * v3[2])
        + v1[2] * (v2[0] * v3[1] - v2[1] * v3[0])
    ) / 6.0


def _vertex_distance(
    a: tuple[float, ...],
    b: tuple[float, ...],
) -> float:
    """Euclidean distance between two 3D points."""
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _parse_mesh(
    file_path: str,
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]]]:
    """Parse an STL or OBJ file, returning (triangles, vertices).

    :raises ValueError: If the file is not a supported format or cannot
        be parsed.
    """
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
        raise ValueError(f"Unsupported file type: {ext!r}.  Expected .stl or .obj.")

    if errors:
        raise ValueError(f"Failed to parse mesh: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("Mesh contains no geometry.")

    return triangles, vertices


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------


def _analyze_overhangs(
    triangles: list[tuple[tuple[float, ...], ...]],
    *,
    max_overhang_angle: float = 45.0,
) -> OverhangAnalysis:
    """Detect overhanging triangles.

    A triangle is an overhang if its normal points downward (negative Z
    component) at an angle greater than ``max_overhang_angle`` from
    vertical.
    """
    total = len(triangles)
    overhang_count = 0
    max_angle = 0.0
    worst_regions: list[dict[str, float]] = []

    # The build direction is +Z.  A triangle overhangs when the angle
    # between its outward normal and the downward direction (-Z) is
    # small, which is equivalent to the angle between the normal and +Z
    # being large (> 90 + threshold).  We measure the angle from
    # vertical (angle between normal and +Z), and flag triangles where
    # the normal has a downward Z component and the angle from the
    # horizontal plane exceeds the threshold.
    for tri in triangles:
        n = _triangle_normal(tri[0], tri[1], tri[2])
        nn = _normalize(n)
        nz = nn[2]

        # Only consider downward-facing normals (nz < 0 means the
        # face points downward).
        if nz >= 0:
            continue

        # Angle from the horizontal plane (0 = horizontal, 90 = straight down).
        # We want the overhang angle measured from vertical.
        # cos(angle_from_down) = -nz (dot product with (0,0,-1))
        # overhang_angle_from_vertical = 180 - angle_from_vertical
        # Simpler: the angle the normal makes with the downward
        # vector is acos(-nz).  If acos(-nz) < max_overhang_angle
        # from horizontal, it needs supports.
        # We define overhang angle as the angle between the face and
        # the horizontal.  A flat face pointing down has 0 deg
        # overhang (from horizontal), fully supported bottom face at
        # 0 deg too, but a 45 deg overhang means the face is 45 deg
        # from vertical.
        #
        # Standard definition: overhang angle = angle between
        # downward normal and the vertical (build direction).
        # If that angle < threshold, it needs supports.
        # angle = acos(dot(normal, (0,0,-1))) = acos(-nz)
        angle_from_down = math.degrees(math.acos(max(-1.0, min(1.0, -nz))))

        # If the angle from the straight-down direction is less than
        # (90 - max_overhang_angle), the overhang is too steep.
        # Equivalently, if the face normal is within max_overhang_angle
        # of straight down, it needs supports.
        overhang_angle = 90.0 - angle_from_down
        if overhang_angle < 0:
            overhang_angle = 0.0

        effective_angle = angle_from_down  # angle from straight down
        if effective_angle <= max_overhang_angle:
            overhang_count += 1
            if effective_angle > max_angle:
                max_angle = effective_angle
            centroid = _triangle_centroid(tri[0], tri[1], tri[2])
            if len(worst_regions) < 10:
                worst_regions.append(
                    {
                        "x": round(centroid[0], 2),
                        "y": round(centroid[1], 2),
                        "z": round(centroid[2], 2),
                        "angle": round(effective_angle, 1),
                    }
                )

    # Sort worst regions by angle (smallest = most overhung).
    worst_regions.sort(key=lambda r: r["angle"])

    overhang_pct = (overhang_count / total * 100.0) if total > 0 else 0.0

    return OverhangAnalysis(
        max_overhang_angle=round(max_angle, 1),
        overhang_triangle_count=overhang_count,
        overhang_percentage=round(overhang_pct, 1),
        needs_supports=overhang_count > 0,
        worst_regions=worst_regions[:5],
    )


def _analyze_thin_walls(
    triangles: list[tuple[tuple[float, ...], ...]],
    vertices: list[tuple[float, ...]],
    *,
    nozzle_diameter: float = 0.4,
) -> ThinWallAnalysis:
    """Detect thin walls using edge-length approximation.

    Analyzes the shortest edge in each triangle as a proxy for wall
    thickness.  True ray-casting would require a spatial index, so we
    use edge lengths as an approximation that works well for common
    FDM geometries.
    """
    thin_count = 0
    min_thickness = float("inf")
    problematic: list[dict[str, float]] = []
    total = len(triangles)

    for tri in triangles:
        # Compute the three edge lengths.
        e1 = _vertex_distance(tri[0], tri[1])
        e2 = _vertex_distance(tri[1], tri[2])
        e3 = _vertex_distance(tri[2], tri[0])
        shortest = min(e1, e2, e3)

        if shortest < nozzle_diameter:
            thin_count += 1
            if shortest < min_thickness:
                min_thickness = shortest
            centroid = _triangle_centroid(tri[0], tri[1], tri[2])
            if len(problematic) < 10:
                problematic.append(
                    {
                        "x": round(centroid[0], 2),
                        "y": round(centroid[1], 2),
                        "z": round(centroid[2], 2),
                        "thickness_mm": round(shortest, 3),
                    }
                )

    if min_thickness == float("inf"):
        min_thickness = nozzle_diameter  # No thin walls found

    thin_pct = (thin_count / total * 100.0) if total > 0 else 0.0

    return ThinWallAnalysis(
        min_wall_thickness_mm=round(min_thickness, 3),
        thin_wall_count=thin_count,
        thin_wall_percentage=round(thin_pct, 1),
        problematic_regions=problematic[:5],
    )


def _analyze_bridging(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    *,
    layer_height: float = 0.2,
) -> BridgingAnalysis:
    """Detect unsupported horizontal spans (bridges).

    Identifies triangles with normals pointing nearly straight down
    that are above the first layer (not bed-touching).  Measures the
    longest edge of such triangles as the bridge length.
    """
    bridge_count = 0
    max_bridge_len = 0.0

    bed_threshold = z_min + layer_height * 2

    for tri in triangles:
        # Skip triangles near the bed (they're supported).
        centroid = _triangle_centroid(tri[0], tri[1], tri[2])
        if centroid[2] <= bed_threshold:
            continue

        n = _triangle_normal(tri[0], tri[1], tri[2])
        nn = _normalize(n)

        # Bridge: normal points nearly straight down (nz < -0.9).
        if nn[2] > -0.9:
            continue

        # Measure the longest edge as the bridge span.
        e1 = _vertex_distance(tri[0], tri[1])
        e2 = _vertex_distance(tri[1], tri[2])
        e3 = _vertex_distance(tri[2], tri[0])
        longest = max(e1, e2, e3)

        bridge_count += 1
        if longest > max_bridge_len:
            max_bridge_len = longest

    # Bridges > 10mm typically need supports.
    needs_supports = max_bridge_len > 10.0

    return BridgingAnalysis(
        max_bridge_length_mm=round(max_bridge_len, 2),
        bridge_count=bridge_count,
        needs_supports_for_bridges=needs_supports,
    )


def _analyze_bed_adhesion(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    bbox: dict[str, float],
    *,
    layer_height: float = 0.2,
) -> BedAdhesionAnalysis:
    """Estimate bed contact area.

    Sums the area of triangles whose vertices are all within one layer
    height of the bottom of the mesh.
    """
    contact_threshold = z_min + layer_height
    contact_area = 0.0

    for tri in triangles:
        # All three vertices must be near Z_min.
        if tri[0][2] <= contact_threshold and tri[1][2] <= contact_threshold and tri[2][2] <= contact_threshold:
            contact_area += _triangle_area(tri[0], tri[1], tri[2])

    # Bounding box footprint (XY projection).
    footprint = (bbox["x_max"] - bbox["x_min"]) * (bbox["y_max"] - bbox["y_min"])
    contact_pct = (contact_area / footprint * 100.0) if footprint > 0 else 0.0

    if contact_pct > 30.0:
        risk = "low"
    elif contact_pct > 10.0:
        risk = "medium"
    else:
        risk = "high"

    return BedAdhesionAnalysis(
        contact_area_mm2=round(contact_area, 2),
        contact_percentage=round(contact_pct, 1),
        adhesion_risk=risk,
    )


def _analyze_supports(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    *,
    max_overhang_angle: float = 45.0,
) -> SupportAnalysis:
    """Estimate support volume.

    For each overhang triangle, projects it downward to the build plate
    and estimates the support column volume as area x height.
    """
    support_volume = 0.0
    support_regions: list[dict[str, float]] = []

    for tri in triangles:
        n = _triangle_normal(tri[0], tri[1], tri[2])
        nn = _normalize(n)
        nz = nn[2]

        if nz >= 0:
            continue

        angle_from_down = math.degrees(math.acos(max(-1.0, min(1.0, -nz))))
        if angle_from_down > max_overhang_angle:
            continue

        centroid = _triangle_centroid(tri[0], tri[1], tri[2])
        height = centroid[2] - z_min
        if height <= 0:
            continue

        area = _triangle_area(tri[0], tri[1], tri[2])
        volume = area * height
        support_volume += volume

        if len(support_regions) < 10:
            support_regions.append(
                {
                    "x": round(centroid[0], 2),
                    "y": round(centroid[1], 2),
                    "z": round(centroid[2], 2),
                    "volume_mm3": round(volume, 2),
                }
            )

    # Sort by volume descending.
    support_regions.sort(key=lambda r: r["volume_mm3"], reverse=True)

    # Estimate model volume for percentage calculation.
    model_volume = abs(sum(_signed_volume_of_triangle(tri[0], tri[1], tri[2]) for tri in triangles))

    support_pct = (support_volume / model_volume * 100.0) if model_volume > 0 else 0.0

    return SupportAnalysis(
        estimated_support_volume_mm3=round(support_volume, 2),
        support_percentage=round(support_pct, 1),
        support_regions=support_regions[:5],
    )


def _compute_score(
    overhangs: OverhangAnalysis,
    thin_walls: ThinWallAnalysis,
    bridging: BridgingAnalysis,
    bed_adhesion: BedAdhesionAnalysis,
    supports: SupportAnalysis,
) -> int:
    """Compute a printability score from 0-100.

    Starts at 100 and deducts points for each issue found.
    """
    score = 100

    # Overhang deductions (max -30)
    if overhangs.needs_supports:
        score -= min(30, int(overhangs.overhang_percentage * 0.5))

    # Thin wall deductions (max -25)
    if thin_walls.thin_wall_count > 0:
        score -= min(25, int(thin_walls.thin_wall_percentage * 0.5))

    # Bridging deductions (max -15)
    if bridging.bridge_count > 0:
        score -= min(15, 5 + bridging.bridge_count)

    # Bed adhesion deductions (max -15)
    if bed_adhesion.adhesion_risk == "high":
        score -= 15
    elif bed_adhesion.adhesion_risk == "medium":
        score -= 7

    # Support volume deductions (max -15)
    if supports.support_percentage > 50:
        score -= 15
    elif supports.support_percentage > 20:
        score -= 10
    elif supports.support_percentage > 5:
        score -= 5

    return max(0, min(100, score))


def _score_to_grade(score: int) -> str:
    """Convert a 0-100 score to a letter grade."""
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def _build_recommendations(
    overhangs: OverhangAnalysis,
    thin_walls: ThinWallAnalysis,
    bridging: BridgingAnalysis,
    bed_adhesion: BedAdhesionAnalysis,
    supports: SupportAnalysis,
) -> list[str]:
    """Generate actionable recommendations based on analysis results."""
    recs: list[str] = []

    if overhangs.needs_supports:
        recs.append(
            f"Enable supports: {overhangs.overhang_percentage:.0f}% of triangles "
            f"are overhangs.  Consider re-orienting the model to reduce supports."
        )

    if thin_walls.thin_wall_count > 0:
        recs.append(
            f"Thin walls detected ({thin_walls.min_wall_thickness_mm:.2f} mm min).  "
            f"Use a smaller nozzle or increase wall thickness."
        )

    if bridging.needs_supports_for_bridges:
        recs.append(
            f"Long bridges detected ({bridging.max_bridge_length_mm:.1f} mm).  Enable supports or reduce bridge spans."
        )

    if bed_adhesion.adhesion_risk == "high":
        recs.append(
            "Low bed contact area.  Use a brim or raft, or re-orient the model to increase the contact surface."
        )
    elif bed_adhesion.adhesion_risk == "medium":
        recs.append("Moderate bed contact area.  Consider adding a brim for better adhesion.")

    if supports.support_percentage > 20:
        recs.append(
            f"High support volume ({supports.support_percentage:.0f}% of model).  "
            f"Re-orienting the model may reduce material waste."
        )

    if not recs:
        recs.append("Model looks good for printing.  No issues detected.")

    return recs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_printability(
    file_path: str,
    *,
    nozzle_diameter: float = 0.4,
    layer_height: float = 0.2,
    max_overhang_angle: float = 45.0,
    build_volume: tuple[float, float, float] | None = None,
) -> PrintabilityReport:
    """Run a full printability analysis on a mesh file.

    :param file_path: Path to an STL or OBJ file.
    :param nozzle_diameter: Printer nozzle diameter in mm.
    :param layer_height: Print layer height in mm.
    :param max_overhang_angle: Max overhang angle (degrees) before
        supports are needed.
    :param build_volume: Optional (X, Y, Z) build volume in mm.  If
        provided, the report will warn if the model exceeds it.
    :returns: A :class:`PrintabilityReport` with scores, grades, and
        recommendations.
    :raises ValueError: If the file cannot be parsed.
    """
    triangles, vertices = _parse_mesh(file_path)

    # Bounding box.
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    bbox = {
        "x_min": min(xs),
        "x_max": max(xs),
        "y_min": min(ys),
        "y_max": max(ys),
        "z_min": min(zs),
        "z_max": max(zs),
    }
    z_min = bbox["z_min"]

    overhangs = _analyze_overhangs(triangles, max_overhang_angle=max_overhang_angle)
    thin_walls = _analyze_thin_walls(triangles, vertices, nozzle_diameter=nozzle_diameter)
    bridging = _analyze_bridging(triangles, z_min, layer_height=layer_height)
    bed_adhesion = _analyze_bed_adhesion(triangles, z_min, bbox, layer_height=layer_height)
    supports = _analyze_supports(triangles, z_min, max_overhang_angle=max_overhang_angle)

    score = _compute_score(overhangs, thin_walls, bridging, bed_adhesion, supports)
    grade = _score_to_grade(score)
    recommendations = _build_recommendations(overhangs, thin_walls, bridging, bed_adhesion, supports)

    # Estimate print time modifier: supports and bridges add time.
    time_mod = 1.0
    if supports.support_percentage > 0:
        time_mod += supports.support_percentage / 100.0 * 0.5
    if bridging.bridge_count > 0:
        time_mod += 0.05

    # Build volume check.
    if build_volume is not None:
        bx, by, bz = build_volume
        dx = bbox["x_max"] - bbox["x_min"]
        dy = bbox["y_max"] - bbox["y_min"]
        dz = bbox["z_max"] - bbox["z_min"]
        if dx > bx or dy > by or dz > bz:
            recommendations.insert(
                0,
                f"Model ({dx:.1f} x {dy:.1f} x {dz:.1f} mm) exceeds build volume ({bx:.0f} x {by:.0f} x {bz:.0f} mm).",
            )
            score = max(0, score - 20)

    printable = score >= 50

    return PrintabilityReport(
        printable=printable,
        score=score,
        grade=grade,
        overhangs=overhangs,
        thin_walls=thin_walls,
        bridging=bridging,
        bed_adhesion=bed_adhesion,
        supports=supports,
        recommendations=recommendations,
        estimated_print_time_modifier=round(time_mod, 2),
    )
