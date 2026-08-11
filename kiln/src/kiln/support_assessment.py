"""Support feasibility assessment for FDM 3D printing.

Analyses mesh geometry to determine whether a model needs supports,
how difficult they would be to remove, and what type of support
strategy is optimal for the material.

Includes overhang detection, bridge estimation, enclosed cavity
detection (ray-cast), orientation suggestions, and material-specific
support recommendations.

Usage::

    from kiln.support_assessment import assess_support_feasibility

    result = assess_support_feasibility(stl_path="/tmp/model.stl", material="PETG")
    print(result.needs_supports)
    print(result.removal_difficulty)
"""

from __future__ import annotations

import json
import logging
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiln.generation.validation import _is_bed_supported_triangle, _mesh_bed_z

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent / "data" / "support_profiles.json"

# Module-level cached profiles
_PROFILES: dict[str, Any] = {}

# Ray-cast budget — cap total intersection tests for performance
_MAX_RAY_TESTS = 10_000

# Thresholds
_TALL_SUPPORT_MM = 50.0  # supports taller than this are stability risks
_BRIDGE_NEAR_VERTICAL_DEG = 80.0  # normals > this are near-horizontal faces
_CLUSTER_PROXIMITY_MM = 5.0  # XY proximity for clustering overhang triangles
_ORIENTATION_IMPROVEMENT_THRESHOLD = 0.30  # 30% improvement required to suggest

# Default profile for unknown materials (PLA-like, conservative)
_DEFAULT_PROFILE: dict[str, Any] = {
    "overhang_safe_deg": 50,
    "overhang_max_deg": 60,
    "bridge_safe_mm": 30,
    "bridge_max_mm": 100,
    "z_gap_multiplier": 1.0,
    "removal_difficulty": 3,
    "self_support_fuse_risk": "medium",
    "support_notes": "Unknown material — using conservative defaults.",
    "soluble_pairing": None,
    "cross_material_pairing": None,
}

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_profiles() -> dict[str, Any]:
    """Load support profiles from JSON, cache in module singleton."""
    global _PROFILES  # noqa: PLW0603
    if _PROFILES:
        return _PROFILES
    try:
        with open(_DATA_FILE) as fh:
            raw = json.load(fh)
        # Skip _meta key
        _PROFILES = {k: v for k, v in raw.items() if not k.startswith("_")}
    except Exception:
        logger.debug("Could not load support profiles from %s", _DATA_FILE, exc_info=True)
        _PROFILES = {}
    return _PROFILES


MATERIAL_ALIASES: dict[str, str] = {
    "pla+": "pla",
    "pla-cf": "pla",
    "petg-cf": "petg",
    "asa": "abs",
    "tpe": "tpu",
    "nylon": "pa",
    "pa-cf": "pa",
    "polycarbonate": "pc",
}
"""Canonical material alias mapping shared across Kiln modules."""


def _get_profile(material: str) -> dict[str, Any]:
    """Get the support profile for a material, falling back to defaults."""
    profiles = _load_profiles()
    key = material.lower().strip()
    key = MATERIAL_ALIASES.get(key, key)
    return profiles.get(key, _DEFAULT_PROFILE)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class OverhangRegion:
    """A connected region of overhang triangles."""

    centroid: tuple[float, float, float]
    area_mm2: float
    max_angle_deg: float
    estimated_bridge_mm: float
    support_height_mm: float
    accessible: bool  # ray-cast result
    triangle_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "centroid": list(self.centroid),
            "area_mm2": round(self.area_mm2, 2),
            "max_angle_deg": round(self.max_angle_deg, 1),
            "estimated_bridge_mm": round(self.estimated_bridge_mm, 1),
            "support_height_mm": round(self.support_height_mm, 1),
            "accessible": self.accessible,
            "triangle_count": self.triangle_count,
        }


@dataclass
class OrientationCandidate:
    """A candidate print orientation with overhang analysis."""

    rotation_axis: str  # "none", "X90", "X180", "Y90", "Y180", "Z90"
    overhang_area_mm2: float
    overhang_percentage: float
    support_volume_estimate_cm3: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "rotation_axis": self.rotation_axis,
            "overhang_area_mm2": round(self.overhang_area_mm2, 2),
            "overhang_percentage": round(self.overhang_percentage, 1),
            "support_volume_estimate_cm3": round(self.support_volume_estimate_cm3, 3),
        }


@dataclass
class SupportAssessment:
    """Complete support feasibility report."""

    needs_supports: bool
    overhang_percentage: float
    overhang_area_mm2: float
    total_surface_area_mm2: float
    bridge_regions: list[OverhangRegion] = field(default_factory=list)
    trapped_regions: list[OverhangRegion] = field(default_factory=list)
    removal_difficulty: str = "easy"  # "easy", "medium", "hard", "impossible"
    removal_notes: str = ""
    recommended_support_type: str = "none"
    recommended_z_gap_mm: float = 0.2
    recommended_interface_layers: int = 2
    soluble_support_option: str | None = None
    cross_material_option: str | None = None
    orientation_suggestions: list[OrientationCandidate] = field(default_factory=list)
    current_orientation: OrientationCandidate | None = None
    warnings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "needs_supports": self.needs_supports,
            "overhang_percentage": round(self.overhang_percentage, 1),
            "overhang_area_mm2": round(self.overhang_area_mm2, 2),
            "total_surface_area_mm2": round(self.total_surface_area_mm2, 2),
            "bridge_regions": [r.to_dict() for r in self.bridge_regions],
            "trapped_regions": [r.to_dict() for r in self.trapped_regions],
            "removal_difficulty": self.removal_difficulty,
            "removal_notes": self.removal_notes,
            "recommended_support_type": self.recommended_support_type,
            "recommended_z_gap_mm": round(self.recommended_z_gap_mm, 2),
            "recommended_interface_layers": self.recommended_interface_layers,
            "soluble_support_option": self.soluble_support_option,
            "cross_material_option": self.cross_material_option,
            "orientation_suggestions": [o.to_dict() for o in self.orientation_suggestions],
            "current_orientation": self.current_orientation.to_dict() if self.current_orientation else None,
            "warnings": self.warnings,
            "recommendations": self.recommendations,
        }


# ---------------------------------------------------------------------------
# Triangle / geometry helpers
# ---------------------------------------------------------------------------

_Triangle = tuple[
    tuple[float, float, float],  # v0
    tuple[float, float, float],  # v1
    tuple[float, float, float],  # v2
]
_Vec3 = tuple[float, float, float]


def _cross(a: _Vec3, b: _Vec3) -> _Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _sub(a: _Vec3, b: _Vec3) -> _Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: _Vec3, b: _Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(v: _Vec3) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _triangle_normal(tri: _Triangle) -> _Vec3:
    """Compute the unit normal of a triangle from its vertices."""
    v0, v1, v2 = tri
    edge1 = _sub(v1, v0)
    edge2 = _sub(v2, v0)
    n = _cross(edge1, edge2)
    ln = _length(n)
    if ln < 1e-12:
        return (0.0, 0.0, 0.0)
    return (n[0] / ln, n[1] / ln, n[2] / ln)


def _triangle_area(tri: _Triangle) -> float:
    """Compute the area of a triangle."""
    v0, v1, v2 = tri
    edge1 = _sub(v1, v0)
    edge2 = _sub(v2, v0)
    c = _cross(edge1, edge2)
    return _length(c) * 0.5


def _triangle_centroid(tri: _Triangle) -> _Vec3:
    v0, v1, v2 = tri
    return (
        (v0[0] + v1[0] + v2[0]) / 3.0,
        (v0[1] + v1[1] + v2[1]) / 3.0,
        (v0[2] + v1[2] + v2[2]) / 3.0,
    )


def _overhang_angle_deg(normal: _Vec3) -> float:
    """Compute the overhang angle in degrees.

    0 = face points straight up (no overhang).
    90 = face points straight down (full overhang).
    """
    # Angle between normal and +Z axis
    nz = normal[2]
    # Clamp for numerical safety
    nz = max(-1.0, min(1.0, nz))
    angle_from_up = math.degrees(math.acos(nz))
    # Overhang angle: 0 when pointing up, 90 when pointing down
    # A face pointing straight down has angle_from_up = 180, overhang = 90
    # We want the angle from the horizontal plane for overhang classification
    if angle_from_up <= 90:
        return 0.0  # Face points upward — no overhang
    return angle_from_up - 90.0


# ---------------------------------------------------------------------------
# STL parsing
# ---------------------------------------------------------------------------

_STL_HEADER_SIZE = 80
_STL_TRIANGLE_SIZE = 50


def _parse_stl_triangles(stl_path: str) -> list[_Triangle]:
    """Parse a binary STL file and return a list of triangles (vertex tuples)."""
    path = Path(stl_path)
    size = path.stat().st_size
    triangles: list[_Triangle] = []

    with open(path, "rb") as fh:
        header = fh.read(_STL_HEADER_SIZE)
        if size < _STL_HEADER_SIZE + 4:
            return triangles

        # Detect ASCII STL
        if header[:5] == b"solid":
            count_bytes = fh.read(4)
            tri_count_candidate = struct.unpack("<I", count_bytes)[0]
            expected = _STL_HEADER_SIZE + 4 + tri_count_candidate * _STL_TRIANGLE_SIZE
            if expected != size:
                # ASCII STL — not supported for triangle extraction
                return triangles
            tri_count = tri_count_candidate
        else:
            count_bytes = fh.read(4)
            tri_count = struct.unpack("<I", count_bytes)[0]

        expected = _STL_HEADER_SIZE + 4 + tri_count * _STL_TRIANGLE_SIZE
        if size < expected:
            tri_count = 0

        for _ in range(min(tri_count, 500_000)):
            data = fh.read(_STL_TRIANGLE_SIZE)
            if len(data) < _STL_TRIANGLE_SIZE:
                break
            # Skip normal (3 floats), read 3 vertices (9 floats)
            verts = struct.unpack_from("<9f", data, 12)
            v0 = (verts[0], verts[1], verts[2])
            v1 = (verts[3], verts[4], verts[5])
            v2 = (verts[6], verts[7], verts[8])
            triangles.append((v0, v1, v2))

    return triangles


# ---------------------------------------------------------------------------
# Ray-triangle intersection (Moller-Trumbore)
# ---------------------------------------------------------------------------

_EPSILON = 1e-7


def _ray_intersects_triangle(
    origin: _Vec3,
    direction: _Vec3,
    tri: _Triangle,
) -> bool:
    """Moller-Trumbore ray-triangle intersection test.

    :param origin: Ray origin point.
    :param direction: Ray direction (does not need to be normalized).
    :param tri: Triangle as (v0, v1, v2).
    :returns: True if ray intersects triangle in the positive direction.
    """
    v0, v1, v2 = tri
    edge1 = _sub(v1, v0)
    edge2 = _sub(v2, v0)
    h = _cross(direction, edge2)
    a = _dot(edge1, h)

    if -_EPSILON < a < _EPSILON:
        return False  # Ray parallel to triangle

    f = 1.0 / a
    s = _sub(origin, v0)
    u = f * _dot(s, h)
    if u < 0.0 or u > 1.0:
        return False

    q = _cross(s, edge1)
    v = f * _dot(direction, q)
    if v < 0.0 or u + v > 1.0:
        return False

    t = f * _dot(edge2, q)
    return t > _EPSILON


def _count_ray_intersections(
    origin: _Vec3,
    direction: _Vec3,
    all_triangles: list[_Triangle],
) -> int:
    """Count how many triangles a ray from origin intersects."""
    count = 0
    for tri in all_triangles:
        if _ray_intersects_triangle(origin, direction, tri):
            count += 1
    return count


def _is_enclosed_cavity(
    origin: _Vec3,
    all_triangles: list[_Triangle],
) -> bool:
    """Determine if a point is inside an enclosed cavity using ray casting.

    A point is enclosed if rays cast in BOTH +Z and -Z directions each
    pass through at least one surface.  A simple overhang above an open
    floor will only have hits going down, not up.

    The origin should be slightly offset from the overhang surface to
    avoid self-intersection.
    """
    # Offset slightly inward (downward from an overhang surface)
    test_point = (origin[0], origin[1], origin[2] - 0.1)

    down_hits = _count_ray_intersections(test_point, (0.0, 0.0, -1.0), all_triangles)
    if down_hits == 0:
        return False

    up_hits = _count_ray_intersections(test_point, (0.0, 0.0, 1.0), all_triangles)
    if up_hits == 0:
        return False

    # Both directions have hits — point is enclosed
    # Check parity: if both are odd, point is inside a volume
    return down_hits % 2 == 1 and up_hits % 2 == 1


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _cluster_overhang_triangles(
    overhang_tris: list[tuple[_Triangle, float, float]],
    *,
    proximity_mm: float = _CLUSTER_PROXIMITY_MM,
) -> list[list[tuple[_Triangle, float, float]]]:
    """Cluster overhang triangles by XY proximity.

    Each element of overhang_tris is (triangle, angle_deg, area_mm2).
    Returns list of clusters.
    """
    if not overhang_tris:
        return []

    # Simple greedy clustering by centroid XY distance
    assigned = [False] * len(overhang_tris)
    centroids = [_triangle_centroid(t[0]) for t in overhang_tris]
    clusters: list[list[tuple[_Triangle, float, float]]] = []

    for i in range(len(overhang_tris)):
        if assigned[i]:
            continue
        cluster = [overhang_tris[i]]
        assigned[i] = True
        # BFS from this triangle
        queue = [i]
        while queue:
            ci = queue.pop(0)
            cx, cy = centroids[ci][0], centroids[ci][1]
            for j in range(len(overhang_tris)):
                if assigned[j]:
                    continue
                dx = centroids[j][0] - cx
                dy = centroids[j][1] - cy
                if math.sqrt(dx * dx + dy * dy) <= proximity_mm:
                    assigned[j] = True
                    cluster.append(overhang_tris[j])
                    queue.append(j)
        clusters.append(cluster)

    return clusters


# ---------------------------------------------------------------------------
# Orientation analysis
# ---------------------------------------------------------------------------

_ROTATIONS: dict[str, tuple[tuple[int, int, int], ...]] = {
    "none": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    "X90": ((1, 0, 0), (0, 0, -1), (0, 1, 0)),
    "X180": ((1, 0, 0), (0, -1, 0), (0, 0, -1)),
    "Y90": ((0, 0, 1), (0, 1, 0), (-1, 0, 0)),
    "Y180": ((-1, 0, 0), (0, 1, 0), (0, 0, -1)),
    "Z90": ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
}


def _rotate_point(p: _Vec3, matrix: tuple[tuple[int, int, int], ...]) -> _Vec3:
    """Apply a 3x3 rotation matrix to a point."""
    return (
        matrix[0][0] * p[0] + matrix[0][1] * p[1] + matrix[0][2] * p[2],
        matrix[1][0] * p[0] + matrix[1][1] * p[1] + matrix[1][2] * p[2],
        matrix[2][0] * p[0] + matrix[2][1] * p[1] + matrix[2][2] * p[2],
    )


def _analyze_orientation(
    triangles: list[_Triangle],
    rotation_name: str,
    matrix: tuple[tuple[int, int, int], ...],
    safe_deg: float,
    layer_height_mm: float = 0.2,
) -> OrientationCandidate:
    """Analyze overhang characteristics for a given orientation."""
    total_area = 0.0
    overhang_area = 0.0
    support_volume = 0.0

    rotated_tris = [
        (
            _rotate_point(tri[0], matrix),
            _rotate_point(tri[1], matrix),
            _rotate_point(tri[2], matrix),
        )
        for tri in triangles
    ]
    # Each candidate orientation lands the model on the plate, so the
    # plate sits under whatever this rotation makes the lowest point.
    bed_z = _mesh_bed_z(rotated_tris)

    for rotated in rotated_tris:
        area = _triangle_area(rotated)
        total_area += area

        # The face this orientation stands on rests on the bed.
        if _is_bed_supported_triangle(rotated, bed_z, layer_height_mm):
            continue

        normal = _triangle_normal(rotated)
        angle = _overhang_angle_deg(normal)
        if angle > safe_deg:
            overhang_area += area
            # Rough support volume: area * height above the plate
            cz = _triangle_centroid(rotated)[2] - bed_z
            if cz > 0:
                support_volume += area * cz

    pct = (overhang_area / total_area * 100.0) if total_area > 0 else 0.0
    vol_cm3 = support_volume / 1000.0  # mm^3 to cm^3

    return OrientationCandidate(
        rotation_axis=rotation_name,
        overhang_area_mm2=overhang_area,
        overhang_percentage=pct,
        support_volume_estimate_cm3=vol_cm3,
    )


# ---------------------------------------------------------------------------
# Support type recommendation
# ---------------------------------------------------------------------------


def _recommend_support_type(
    bridge_regions: list[OverhangRegion],
    trapped_regions: list[OverhangRegion],
    overhang_area_mm2: float,
    total_area_mm2: float,
    profile: dict[str, Any],
) -> str:
    """Choose the best support type based on geometry and material."""
    if overhang_area_mm2 <= 0:
        return "none"

    fuse_risk = profile.get("self_support_fuse_risk", "medium")

    # If cavities exist, we need soluble supports or tree supports to reach them
    if trapped_regions:
        if profile.get("soluble_pairing"):
            return "soluble_dual"
        return "tree_organic"

    # Cross-material recommended for high-fuse-risk materials
    if fuse_risk == "high" and profile.get("cross_material_pairing"):
        return "cross_material"

    # Large flat overhangs → grid/tree hybrid
    large_flat = any(
        r.estimated_bridge_mm > 20 and r.max_angle_deg < 85
        for r in bridge_regions
    )
    if large_flat:
        return "tree_hybrid"

    # Organic complex shapes → tree organic
    overhang_pct = (overhang_area_mm2 / total_area_mm2 * 100) if total_area_mm2 > 0 else 0
    if overhang_pct > 30 or len(bridge_regions) > 5:
        return "tree_organic"

    # Default for moderate overhangs
    return "tree_hybrid"


# ---------------------------------------------------------------------------
# Removal difficulty assessment
# ---------------------------------------------------------------------------


def _assess_removal_difficulty(
    profile: dict[str, Any],
    overhang_area_mm2: float,
    total_area_mm2: float,
    trapped_regions: list[OverhangRegion],
    bridge_regions: list[OverhangRegion],
) -> tuple[str, str]:
    """Determine how hard supports will be to remove.

    :returns: (difficulty, notes) where difficulty is one of
        "easy", "medium", "hard", "impossible".
    """
    # Enclosed cavities → impossible to remove
    if trapped_regions:
        return (
            "impossible",
            f"{len(trapped_regions)} enclosed cavity region(s) with trapped supports. "
            f"{profile.get('support_notes', '')}",
        )

    base_difficulty = profile.get("removal_difficulty", 3)
    notes = profile.get("support_notes", "")

    # Adjust based on contact area ratio
    contact_ratio = (overhang_area_mm2 / total_area_mm2) if total_area_mm2 > 0 else 0
    if contact_ratio > 0.4:
        base_difficulty += 1
    if contact_ratio > 0.6:
        base_difficulty += 1

    # High supports are harder to remove (stability + surface marks)
    tall_supports = any(r.support_height_mm > _TALL_SUPPORT_MM for r in bridge_regions)
    if tall_supports:
        base_difficulty += 1

    # Map numeric difficulty to string
    if base_difficulty <= 2:
        return "easy", notes
    if base_difficulty <= 3:
        return "medium", notes
    if base_difficulty <= 4:
        return "hard", notes
    return "hard", notes


# ---------------------------------------------------------------------------
# Main assessment function
# ---------------------------------------------------------------------------


def assess_support_feasibility(
    *,
    triangles: list[tuple[tuple[float, float, float], ...]] | None = None,
    stl_path: str | None = None,
    material: str = "PLA",
    printer_id: str = "",
    layer_height_mm: float = 0.2,
    nozzle_diameter_mm: float = 0.4,
    has_dual_extrusion: bool = False,
) -> SupportAssessment:
    """Assess support feasibility for a 3D model.

    Analyses mesh geometry to determine overhang severity, bridge spans,
    enclosed cavities, and recommends support strategy.

    :param triangles: List of triangles as ((v0x,v0y,v0z), (v1x,v1y,v1z), (v2x,v2y,v2z)).
    :param stl_path: Path to a binary STL file (alternative to triangles).
    :param material: Material name for material-specific thresholds.
    :param printer_id: Printer ID (reserved for future per-printer adjustments).
    :param layer_height_mm: Layer height for Z-gap calculation.
    :param nozzle_diameter_mm: Nozzle diameter for bridge threshold scaling.
    :param has_dual_extrusion: Whether printer has AMS/MMU for cross-material supports.
    :returns: Complete support assessment report.
    """
    profile = _get_profile(material)

    safe_deg = float(profile["overhang_safe_deg"])
    max_deg = float(profile["overhang_max_deg"])

    # Scale bridge thresholds by nozzle size (wider nozzle = better bridging)
    nozzle_factor = nozzle_diameter_mm / 0.4 if nozzle_diameter_mm > 0 else 1.0

    # Load triangles
    tris: list[_Triangle] = []
    if triangles is not None:
        tris = [(t[0], t[1], t[2]) for t in triangles]  # type: ignore[index]
    elif stl_path is not None:
        tris = _parse_stl_triangles(stl_path)

    if not tris:
        return SupportAssessment(
            needs_supports=False,
            overhang_percentage=0.0,
            overhang_area_mm2=0.0,
            total_surface_area_mm2=0.0,
            warnings=["No triangles available for analysis"],
        )

    # ---------------------------------------------------------------
    # a) Overhang analysis
    # ---------------------------------------------------------------
    total_area = 0.0
    safe_area = 0.0
    marginal_area = 0.0
    overhang_area = 0.0

    # (triangle, angle_deg, area_mm2) for overhangs
    overhang_tris: list[tuple[_Triangle, float, float]] = []

    # The plate the model rests on — its own lowest point, not z=0.
    bed_z = _mesh_bed_z(tris)

    for tri in tris:
        area = _triangle_area(tri)
        total_area += area

        # The face the model stands on is carried by the bed: safe, and
        # never a candidate for supports or a bridge.
        if _is_bed_supported_triangle(tri, bed_z, layer_height_mm):
            safe_area += area
            continue

        normal = _triangle_normal(tri)
        angle = _overhang_angle_deg(normal)

        if angle <= safe_deg:
            safe_area += area
        elif angle <= max_deg:
            marginal_area += area
            overhang_tris.append((tri, angle, area))
        else:
            overhang_area += area
            overhang_tris.append((tri, angle, area))

    total_overhang_area = marginal_area + overhang_area
    overhang_pct = (total_overhang_area / total_area * 100.0) if total_area > 0 else 0.0

    # ---------------------------------------------------------------
    # b) Bridge detection — cluster near-horizontal overhangs
    # ---------------------------------------------------------------
    bridge_tris = [
        (tri, ang, ar)
        for tri, ang, ar in overhang_tris
        if ang > _BRIDGE_NEAR_VERTICAL_DEG
    ]
    clusters = _cluster_overhang_triangles(bridge_tris)

    bridge_regions: list[OverhangRegion] = []
    for cluster in clusters:
        if not cluster:
            continue
        cluster_area = sum(ar for _, _, ar in cluster)
        max_angle = max(ang for _, ang, _ in cluster)
        centroids = [_triangle_centroid(tri) for tri, _, _ in cluster]
        cx = sum(c[0] for c in centroids) / len(centroids)
        cy = sum(c[1] for c in centroids) / len(centroids)
        cz = sum(c[2] for c in centroids) / len(centroids)

        # Bridge width: bounding box of cluster in XY
        xs = [c[0] for c in centroids]
        ys = [c[1] for c in centroids]
        bridge_width = max(max(xs) - min(xs), max(ys) - min(ys))

        # Support height: min Z of cluster centroids
        min_z = min(c[2] for c in centroids)

        bridge_regions.append(OverhangRegion(
            centroid=(cx, cy, cz),
            area_mm2=cluster_area,
            max_angle_deg=max_angle,
            estimated_bridge_mm=bridge_width,
            support_height_mm=max(0.0, min_z),
            accessible=True,  # updated by ray-cast below
            triangle_count=len(cluster),
        ))

    # ---------------------------------------------------------------
    # c) Support height — also build regions from all overhang clusters
    # ---------------------------------------------------------------
    all_overhang_clusters = _cluster_overhang_triangles(overhang_tris)
    all_regions: list[OverhangRegion] = []
    for cluster in all_overhang_clusters:
        if not cluster:
            continue
        cluster_area = sum(ar for _, _, ar in cluster)
        max_angle = max(ang for _, ang, _ in cluster)
        centroids = [_triangle_centroid(tri) for tri, _, _ in cluster]
        cx = sum(c[0] for c in centroids) / len(centroids)
        cy = sum(c[1] for c in centroids) / len(centroids)
        cz = sum(c[2] for c in centroids) / len(centroids)

        xs = [c[0] for c in centroids]
        ys = [c[1] for c in centroids]
        bridge_width = max(max(xs) - min(xs), max(ys) - min(ys))

        min_z = min(c[2] for c in centroids)

        all_regions.append(OverhangRegion(
            centroid=(cx, cy, cz),
            area_mm2=cluster_area,
            max_angle_deg=max_angle,
            estimated_bridge_mm=bridge_width,
            support_height_mm=max(0.0, min_z),
            accessible=True,
            triangle_count=len(cluster),
        ))

    warnings: list[str] = []
    tall_regions = [r for r in all_regions if r.support_height_mm > _TALL_SUPPORT_MM]
    if tall_regions:
        warnings.append(
            f"{len(tall_regions)} overhang region(s) need supports taller than "
            f"{_TALL_SUPPORT_MM:.0f}mm — stability risk"
        )

    # ---------------------------------------------------------------
    # d) Enclosed cavity detection (ray casting)
    # ---------------------------------------------------------------
    trapped_regions: list[OverhangRegion] = []
    ray_tests_done = 0

    for region in all_regions:
        if ray_tests_done >= _MAX_RAY_TESTS:
            break
        centroid = region.centroid
        enclosed = _is_enclosed_cavity(centroid, tris)
        ray_tests_done += 2 * len(tris)  # two rays per test

        if enclosed:
            region.accessible = False
            trapped_regions.append(region)

    # ---------------------------------------------------------------
    # e) Removal difficulty
    # ---------------------------------------------------------------
    difficulty, removal_notes = _assess_removal_difficulty(
        profile,
        total_overhang_area,
        total_area,
        trapped_regions,
        all_regions,
    )

    # ---------------------------------------------------------------
    # f) Orientation suggestions
    # ---------------------------------------------------------------
    current_orient = _analyze_orientation(
        tris, "none", _ROTATIONS["none"], safe_deg, layer_height_mm
    )
    orientation_suggestions: list[OrientationCandidate] = []

    if total_overhang_area > 0:
        for name, matrix in _ROTATIONS.items():
            if name == "none":
                continue
            candidate = _analyze_orientation(
                tris, name, matrix, safe_deg, layer_height_mm
            )
            # Only suggest if >30% improvement
            if current_orient.overhang_area_mm2 > 0:
                improvement = (
                    1.0 - candidate.overhang_area_mm2 / current_orient.overhang_area_mm2
                )
                if improvement >= _ORIENTATION_IMPROVEMENT_THRESHOLD:
                    orientation_suggestions.append(candidate)

        # Sort by lowest overhang area
        orientation_suggestions.sort(key=lambda c: c.overhang_area_mm2)

    # ---------------------------------------------------------------
    # g) Support type recommendation
    # ---------------------------------------------------------------
    support_type = _recommend_support_type(
        bridge_regions, trapped_regions,
        total_overhang_area, total_area, profile,
    )

    # Z-gap calculation
    z_gap_mm = round(layer_height_mm * profile["z_gap_multiplier"], 2)

    # Interface layers
    interface_layers = 2
    if profile.get("self_support_fuse_risk") == "high":
        interface_layers = 3

    # Build recommendations
    recommendations: list[str] = []
    needs_supports = total_overhang_area > 0 and overhang_pct > 5.0

    if needs_supports:
        recommendations.append(
            f"Supports recommended: {overhang_pct:.0f}% overhang area detected"
        )
        if support_type == "cross_material":
            pairing = profile.get("cross_material_pairing", "")
            recommendations.append(
                f"Use {pairing} supports for {material.upper()} — "
                f"reduces fusing risk"
            )
        elif support_type == "soluble_dual":
            pairing = profile.get("soluble_pairing", "")
            recommendations.append(
                f"Use {pairing} soluble supports — enclosed cavities require "
                f"dissolvable supports"
            )
        if bridge_regions:
            max_bridge = max(r.estimated_bridge_mm for r in bridge_regions)
            bridge_max = profile.get("bridge_max_mm", 100) * nozzle_factor
            if max_bridge > bridge_max:
                recommendations.append(
                    f"Bridge span {max_bridge:.0f}mm exceeds material limit "
                    f"({bridge_max:.0f}mm @ {nozzle_diameter_mm}mm nozzle) — "
                    f"supports required under bridge"
                )
        if orientation_suggestions:
            best = orientation_suggestions[0]
            recommendations.append(
                f"Rotate {best.rotation_axis} to reduce overhang from "
                f"{current_orient.overhang_percentage:.0f}% to "
                f"{best.overhang_percentage:.0f}%"
            )

    # AMS/dual extrusion awareness
    if has_dual_extrusion and needs_supports:
        fuse_risk = profile.get("self_support_fuse_risk", "medium")
        cross_pairing = profile.get("cross_material_pairing")
        soluble = profile.get("soluble_pairing")
        if fuse_risk == "high" and cross_pairing:
            recommendations.append(
                f"AMS detected — use {cross_pairing} in a second slot for "
                f"support material. {material.upper()}/{cross_pairing} don't bond, "
                f"supports peel off cleanly."
            )
        elif trapped_regions and soluble:
            recommendations.append(
                f"AMS detected — use {soluble} soluble supports for "
                f"enclosed cavities. Dissolves in water, no manual removal needed."
            )

    # Thin wall crack risk — supports on walls < 3× nozzle may crack on removal
    min_wall_threshold = nozzle_diameter_mm * 3
    for region in all_regions:
        # Check if the overhang is on a thin wall by looking at nearby geometry
        # Heuristic: if the overhang region's area is very small and height is low,
        # it's likely on a thin feature
        if region.area_mm2 < min_wall_threshold * 10 and region.support_height_mm < 20:
            warnings.append(
                f"Support contact on small feature ({region.area_mm2:.0f}mm²) — "
                f"wall may crack during support removal. "
                f"Consider increasing wall thickness or using soluble supports."
            )
            break  # one warning is enough

    # Surface finish warning — all supported faces will have witness marks
    if needs_supports and overhang_pct > 15:
        warnings.append(
            "Supported faces will have surface witness marks regardless of "
            "support settings. If cosmetic finish is required on overhanging "
            "surfaces, consider reorientation or post-processing (sanding, vapor smoothing)."
        )

    # Bed adhesion warning for tall supports
    if tall_regions:
        warnings.append(
            "Tall supports (>50mm) need strong bed adhesion — use brim "
            "on supports to prevent detachment. Textured PEI recommended."
        )

    # Soluble/cross-material options
    soluble_option = profile.get("soluble_pairing")
    cross_option = None
    if profile.get("cross_material_pairing"):
        cross_option = (
            f"{profile['cross_material_pairing']} supports for "
            f"{material.upper()}"
        )

    return SupportAssessment(
        needs_supports=needs_supports,
        overhang_percentage=overhang_pct,
        overhang_area_mm2=total_overhang_area,
        total_surface_area_mm2=total_area,
        bridge_regions=bridge_regions,
        trapped_regions=trapped_regions,
        removal_difficulty=difficulty,
        removal_notes=removal_notes,
        recommended_support_type=support_type if needs_supports else "none",
        recommended_z_gap_mm=z_gap_mm,
        recommended_interface_layers=interface_layers,
        soluble_support_option=soluble_option,
        cross_material_option=cross_option,
        orientation_suggestions=orientation_suggestions,
        current_orientation=current_orient,
        warnings=warnings,
        recommendations=recommendations,
    )
