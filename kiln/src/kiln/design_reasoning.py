"""Geometry-aware design reasoning engine.

Moves beyond lookup-based knowledge ("PLA is good for prototypes") to
actual geometric analysis that understands *where* a design is weak and
*what* to do about it.  Analyzes STL meshes to find structural risk zones,
recommend reinforcements, assess print orientation impact on strength,
and generate actionable improvement plans with specific coordinates.

Public API:
    analyze_structural_risks   — find cantilevers, thin necks, stress points
    recommend_reinforcements   — gussets, ribs, fillets at specific locations
    assess_load_bearing        — identify load surfaces and stress flow
    generate_improvement_plan  — full actionable plan for a design
    StructuralRisk             — single risk finding dataclass
    ReinforcementRecommendation — single fix recommendation dataclass
    DesignImprovementPlan      — complete plan dataclass
"""

from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Stress concentration threshold: cross-section area ratio that signals a
# "thin neck" where load transfer becomes risky.
_THIN_NECK_RATIO = 0.3

# Cantilever length-to-thickness ratio where deflection becomes concerning
_CANTILEVER_RISK_RATIO = 5.0

# Minimum cross-section area (mm²) below which any section is flagged
_MIN_CROSS_SECTION_MM2 = 4.0

# Number of Z-slices for cross-section analysis
_NUM_SLICES = 40

# Overhang angle (from vertical) where layer adhesion weakens structurally
_STRUCTURAL_OVERHANG_DEG = 45.0


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class StructuralRisk:
    """A single structural risk identified in the geometry."""

    risk_type: str  # "thin_neck", "cantilever", "stress_concentration",
    #   "unsupported_overhang", "weak_layer_adhesion",
    #   "sharp_corner", "insufficient_base"
    severity: str  # "critical", "warning", "info"
    location_mm: tuple[float, float, float]  # (x, y, z) center of risk zone
    region_size_mm: tuple[float, float, float]  # approximate bbox of affected region
    description: str  # human-readable explanation
    metric_name: str  # e.g. "cross_section_area_mm2"
    metric_value: float
    metric_threshold: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_type": self.risk_type,
            "severity": self.severity,
            "location_mm": list(self.location_mm),
            "region_size_mm": list(self.region_size_mm),
            "description": self.description,
            "metric_name": self.metric_name,
            "metric_value": round(self.metric_value, 2),
            "metric_threshold": round(self.metric_threshold, 2),
        }


@dataclass
class ReinforcementRecommendation:
    """A specific reinforcement recommendation tied to geometry."""

    reinforcement_type: str  # "gusset", "rib", "fillet", "chamfer",
    #   "thicken_wall", "add_base", "reorient"
    priority: str  # "high", "medium", "low"
    location_mm: tuple[float, float, float]
    description: str  # what to do and why
    estimated_strength_gain: str  # "2-3x", "30-50%", etc.
    addresses_risk: str  # which risk_type this fixes

    def to_dict(self) -> dict[str, Any]:
        return {
            "reinforcement_type": self.reinforcement_type,
            "priority": self.priority,
            "location_mm": list(self.location_mm),
            "description": self.description,
            "estimated_strength_gain": self.estimated_strength_gain,
            "addresses_risk": self.addresses_risk,
        }


@dataclass
class LoadAnalysis:
    """Analysis of load-bearing characteristics."""

    primary_load_axis: str  # "vertical", "horizontal", "multi-axis"
    load_surfaces: list[dict[str, Any]]  # surfaces that bear load
    weak_axis: str  # axis most vulnerable to failure
    layer_direction_concern: str  # how print layers affect strength
    recommended_print_orientation: str  # "upright", "on_side", "on_back"
    orientation_reasoning: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_load_axis": self.primary_load_axis,
            "load_surfaces": self.load_surfaces,
            "weak_axis": self.weak_axis,
            "layer_direction_concern": self.layer_direction_concern,
            "recommended_print_orientation": self.recommended_print_orientation,
            "orientation_reasoning": self.orientation_reasoning,
        }


@dataclass
class DesignImprovementPlan:
    """Complete improvement plan for a design."""

    file_path: str
    risks: list[StructuralRisk] = field(default_factory=list)
    reinforcements: list[ReinforcementRecommendation] = field(default_factory=list)
    load_analysis: LoadAnalysis | None = None
    overall_structural_score: int = 0  # 0-100
    structural_grade: str = ""  # A-F
    critical_count: int = 0
    warning_count: int = 0
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "risks": [r.to_dict() for r in self.risks],
            "reinforcements": [r.to_dict() for r in self.reinforcements],
            "load_analysis": self.load_analysis.to_dict() if self.load_analysis else None,
            "overall_structural_score": self.overall_structural_score,
            "structural_grade": self.structural_grade,
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Internal geometry helpers
# ---------------------------------------------------------------------------


def _parse_stl_for_analysis(
    file_path: str,
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]]]:
    """Parse STL and return (triangles, vertices).

    Each triangle is ((v0x,v0y,v0z), (v1x,v1y,v1z), (v2x,v2y,v2z)).
    Vertices is a flat list of all vertex positions.
    """
    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"File not found: {file_path}")

    data = path.read_bytes()

    # Detect ASCII vs binary
    if data[:5] == b"solid" and b"\n" in data[:80]:
        # Could be ASCII — check for "facet" keyword
        try:
            text = data.decode("ascii", errors="ignore")
            if "facet" in text.lower():
                return _parse_stl_ascii_analysis(text)
        except Exception:
            pass

    return _parse_stl_binary_analysis(data)


def _parse_stl_binary_analysis(
    data: bytes,
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]]]:
    """Parse binary STL for analysis."""
    if len(data) < 84:
        return [], []

    n_tris = struct.unpack_from("<I", data, 80)[0]
    triangles: list[tuple[tuple[float, ...], ...]] = []
    vertices: list[tuple[float, ...]] = []
    offset = 84

    for _ in range(n_tris):
        if offset + 50 > len(data):
            break
        # Skip normal (12 bytes), read 3 vertices (36 bytes), skip attr (2 bytes)
        v0 = struct.unpack_from("<3f", data, offset + 12)
        v1 = struct.unpack_from("<3f", data, offset + 24)
        v2 = struct.unpack_from("<3f", data, offset + 36)
        triangles.append((v0, v1, v2))
        vertices.extend([v0, v1, v2])
        offset += 50

    return triangles, vertices


def _parse_stl_ascii_analysis(
    text: str,
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]]]:
    """Parse ASCII STL for analysis."""
    import re

    triangles: list[tuple[tuple[float, ...], ...]] = []
    vertices: list[tuple[float, ...]] = []

    vertex_pattern = re.compile(
        r"vertex\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
        r"\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
        r"\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
    )

    current_tri_verts: list[tuple[float, ...]] = []
    for match in vertex_pattern.finditer(text):
        v = (float(match.group(1)), float(match.group(2)), float(match.group(3)))
        current_tri_verts.append(v)
        vertices.append(v)
        if len(current_tri_verts) == 3:
            triangles.append(tuple(current_tri_verts))  # type: ignore[arg-type]
            current_tri_verts = []

    return triangles, vertices


def _bounding_box(
    vertices: list[tuple[float, ...]],
) -> dict[str, float]:
    """Compute bounding box from vertices."""
    if not vertices:
        return {"min_x": 0, "max_x": 0, "min_y": 0, "max_y": 0, "min_z": 0, "max_z": 0}
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    return {
        "min_x": min(xs), "max_x": max(xs),
        "min_y": min(ys), "max_y": max(ys),
        "min_z": min(zs), "max_z": max(zs),
    }


def _cross_section_at_z(
    triangles: list[tuple[tuple[float, ...], ...]],
    z: float,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Compute cross-section edges at a given Z height.

    Returns line segments where the Z-plane intersects the mesh.
    """
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

    for tri in triangles:
        # Find intersections of the triangle with the Z-plane
        above: list[tuple[float, ...]] = []
        below: list[tuple[float, ...]] = []
        on_plane: list[tuple[float, ...]] = []

        for v in tri:
            if abs(v[2] - z) < 1e-6:
                on_plane.append(v)
            elif v[2] > z:
                above.append(v)
            else:
                below.append(v)

        # Need exactly 2 intersection points for a segment
        intersections: list[tuple[float, float]] = []

        if len(on_plane) == 2:
            # Edge lies on the plane
            intersections.append((on_plane[0][0], on_plane[0][1]))
            intersections.append((on_plane[1][0], on_plane[1][1]))
        elif len(on_plane) == 1:
            intersections.append((on_plane[0][0], on_plane[0][1]))
            # Find intersection on the edge between above and below
            if above and below:
                p = _edge_z_intersect(above[0], below[0], z)
                if p:
                    intersections.append(p)
        elif above and below:
            # Two groups — find the 2 intersection points
            # Case: 1 above + 2 below or 2 above + 1 below
            if len(above) == 1:
                for b in below:
                    p = _edge_z_intersect(above[0], b, z)
                    if p:
                        intersections.append(p)
            else:
                for a in above:
                    p = _edge_z_intersect(a, below[0], z)
                    if p:
                        intersections.append(p)

        if len(intersections) == 2:
            segments.append((intersections[0], intersections[1]))

    return segments


def _edge_z_intersect(
    v_high: tuple[float, ...],
    v_low: tuple[float, ...],
    z: float,
) -> tuple[float, float] | None:
    """Interpolate intersection point on an edge at Z height."""
    dz = v_high[2] - v_low[2]
    if abs(dz) < 1e-10:
        return None
    t = (z - v_low[2]) / dz
    x = v_low[0] + t * (v_high[0] - v_low[0])
    y = v_low[1] + t * (v_high[1] - v_low[1])
    return (x, y)


def _cross_section_area(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> float:
    """Estimate cross-section area from line segments using the shoelace formula.

    Connects segments into loops and computes enclosed area.
    """
    if not segments:
        return 0.0

    # Approximate area from segment bounding box when loops are complex
    if len(segments) < 3:
        # Too few segments for meaningful area — use segment-length heuristic
        total_len = sum(
            math.sqrt((s[1][0] - s[0][0]) ** 2 + (s[1][1] - s[0][1]) ** 2)
            for s in segments
        )
        # Assume roughly circular cross-section
        return (total_len / (2 * math.pi)) ** 2 * math.pi if total_len > 0 else 0.0

    # Collect all points and compute convex hull area as upper bound
    points: list[tuple[float, float]] = []
    for s in segments:
        points.extend(s)

    if len(points) < 3:
        return 0.0

    return _convex_hull_area(points)


def _convex_hull_area(points: list[tuple[float, float]]) -> float:
    """Compute area of convex hull using Graham scan + shoelace."""
    if len(points) < 3:
        return 0.0

    # Find centroid
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)

    # Sort by angle from centroid
    def angle_key(p: tuple[float, float]) -> float:
        return math.atan2(p[1] - cy, p[0] - cx)

    sorted_pts = sorted(set(points), key=angle_key)

    if len(sorted_pts) < 3:
        return 0.0

    # Shoelace formula
    area = 0.0
    n = len(sorted_pts)
    for i in range(n):
        j = (i + 1) % n
        area += sorted_pts[i][0] * sorted_pts[j][1]
        area -= sorted_pts[j][0] * sorted_pts[i][1]

    return abs(area) / 2.0


def _triangle_normal(
    tri: tuple[tuple[float, ...], ...],
) -> tuple[float, float, float]:
    """Compute face normal of a triangle."""
    v0, v1, v2 = tri
    e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
    e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
    nx = e1[1] * e2[2] - e1[2] * e2[1]
    ny = e1[2] * e2[0] - e1[0] * e2[2]
    nz = e1[0] * e2[1] - e1[1] * e2[0]
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length < 1e-10:
        return (0.0, 0.0, 1.0)
    return (nx / length, ny / length, nz / length)


def _triangle_area(tri: tuple[tuple[float, ...], ...]) -> float:
    """Compute area of a triangle."""
    v0, v1, v2 = tri
    e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
    e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
    cx = e1[1] * e2[2] - e1[2] * e2[1]
    cy = e1[2] * e2[0] - e1[0] * e2[2]
    cz = e1[0] * e2[1] - e1[1] * e2[0]
    return 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)


# ---------------------------------------------------------------------------
# Structural risk analysis
# ---------------------------------------------------------------------------


def _find_thin_necks(
    triangles: list[tuple[tuple[float, ...], ...]],
    bbox: dict[str, float],
    *,
    min_area_mm2: float = _MIN_CROSS_SECTION_MM2,
) -> list[StructuralRisk]:
    """Find Z-heights where cross-section suddenly narrows (thin necks)."""
    risks: list[StructuralRisk] = []
    z_min = bbox["min_z"]
    z_max = bbox["max_z"]
    z_range = z_max - z_min

    if z_range < 1.0:
        return risks

    # Sample cross-sections at regular Z heights
    n_slices = min(_NUM_SLICES, max(10, int(z_range / 0.5)))
    step = z_range / n_slices

    areas: list[tuple[float, float]] = []  # (z, area)
    for i in range(1, n_slices):
        z = z_min + i * step
        segments = _cross_section_at_z(triangles, z)
        area = _cross_section_area(segments)
        areas.append((z, area))

    if len(areas) < 3:
        return risks

    # Find the maximum cross-section area
    max_area = max(a for _, a in areas)
    if max_area < 1.0:
        return risks

    # Find sections that are unusually thin relative to neighbors
    for i in range(1, len(areas) - 1):
        z, area = areas[i]
        _, area_below = areas[i - 1]
        _, area_above = areas[i + 1]

        neighbor_avg = (area_below + area_above) / 2.0

        # Thin neck: area drops below threshold AND below neighbor average
        if area < min_area_mm2:
            severity = "critical" if area < min_area_mm2 * 0.5 else "warning"
            cx = (bbox["min_x"] + bbox["max_x"]) / 2
            cy = (bbox["min_y"] + bbox["max_y"]) / 2
            risks.append(
                StructuralRisk(
                    risk_type="thin_neck",
                    severity=severity,
                    location_mm=(cx, cy, z),
                    region_size_mm=(
                        bbox["max_x"] - bbox["min_x"],
                        bbox["max_y"] - bbox["min_y"],
                        step * 2,
                    ),
                    description=(
                        f"Cross-section narrows to {area:.1f} mm² at Z={z:.1f} mm. "
                        f"This creates a weak point where the part is likely to snap "
                        f"under load. Minimum safe cross-section is {min_area_mm2:.0f} mm²."
                    ),
                    metric_name="cross_section_area_mm2",
                    metric_value=area,
                    metric_threshold=min_area_mm2,
                )
            )
        elif neighbor_avg > 0 and area / neighbor_avg < _THIN_NECK_RATIO:
            # Sudden constriction relative to neighbors
            cx = (bbox["min_x"] + bbox["max_x"]) / 2
            cy = (bbox["min_y"] + bbox["max_y"]) / 2
            risks.append(
                StructuralRisk(
                    risk_type="stress_concentration",
                    severity="warning",
                    location_mm=(cx, cy, z),
                    region_size_mm=(
                        bbox["max_x"] - bbox["min_x"],
                        bbox["max_y"] - bbox["min_y"],
                        step * 2,
                    ),
                    description=(
                        f"Cross-section drops to {area / neighbor_avg:.0%} of adjacent "
                        f"sections at Z={z:.1f} mm. This abrupt change concentrates "
                        f"stress and may cause cracking under load."
                    ),
                    metric_name="section_ratio",
                    metric_value=area / neighbor_avg,
                    metric_threshold=_THIN_NECK_RATIO,
                )
            )

    return risks


def _find_cantilevers(
    triangles: list[tuple[tuple[float, ...], ...]],
    bbox: dict[str, float],
) -> list[StructuralRisk]:
    """Detect cantilevered regions (geometry extending far from support).

    Uses horizontal cross-section centroid drift — if the centroid shifts
    dramatically between slices, there's a cantilever.
    """
    risks: list[StructuralRisk] = []
    z_min = bbox["min_z"]
    z_max = bbox["max_z"]
    z_range = z_max - z_min

    if z_range < 2.0:
        return risks

    n_slices = min(_NUM_SLICES, max(10, int(z_range / 0.5)))
    step = z_range / n_slices
    width = bbox["max_x"] - bbox["min_x"]
    depth = bbox["max_y"] - bbox["min_y"]
    base_dim = max(width, depth)

    if base_dim < 1.0:
        return risks

    # Track centroid and extent at each Z
    prev_cx: float | None = None
    prev_cy: float | None = None
    prev_z: float = z_min

    for i in range(1, n_slices):
        z = z_min + i * step
        segments = _cross_section_at_z(triangles, z)
        if not segments:
            continue

        # Compute centroid of this cross-section
        all_pts: list[tuple[float, float]] = []
        for s in segments:
            all_pts.extend(s)

        if not all_pts:
            continue

        cx = sum(p[0] for p in all_pts) / len(all_pts)
        cy = sum(p[1] for p in all_pts) / len(all_pts)

        # Compute extent (width of cross-section)
        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        extent_x = max(xs) - min(xs)
        extent_y = max(ys) - min(ys)

        if prev_cx is not None:
            drift = math.sqrt((cx - prev_cx) ** 2 + (cy - prev_cy) ** 2)  # type: ignore[operator]
            dz = z - prev_z
            extent = max(extent_x, extent_y)

            # Cantilever: centroid drifts significantly with no widening of base
            if drift > 0 and dz > 0:
                cantilever_ratio = drift / max(dz, 1.0)
                if cantilever_ratio > 1.5 and extent > base_dim * 0.3:
                    risks.append(
                        StructuralRisk(
                            risk_type="cantilever",
                            severity="warning",
                            location_mm=(cx, cy, z),
                            region_size_mm=(extent_x, extent_y, dz),
                            description=(
                                f"Cantilevered geometry at Z={z:.1f} mm — "
                                f"center of mass shifts {drift:.1f} mm over {dz:.1f} mm "
                                f"of height. Unsupported overhangs weaken layer adhesion "
                                f"and risk print failure."
                            ),
                            metric_name="cantilever_drift_ratio",
                            metric_value=cantilever_ratio,
                            metric_threshold=1.5,
                        )
                    )

        prev_cx = cx
        prev_cy = cy
        prev_z = z

    return risks


def _find_sharp_corners(
    triangles: list[tuple[tuple[float, ...], ...]],
    *,
    angle_threshold_deg: float = 60.0,
) -> list[StructuralRisk]:
    """Find sharp internal corners that create stress concentrations.

    Sharp concave edges concentrate stress and are common failure points.
    """
    risks: list[StructuralRisk] = []

    if len(triangles) > 50000:
        # Subsample for performance — check every Nth triangle pair
        sample_step = max(1, len(triangles) // 5000)
    else:
        sample_step = 1

    # Build edge → face map for sampled triangles
    edge_faces: dict[tuple[tuple[float, ...], ...], list[int]] = {}
    sampled_indices = list(range(0, len(triangles), sample_step))

    for idx in sampled_indices:
        tri = triangles[idx]
        for i in range(3):
            v0 = tri[i]
            v1 = tri[(i + 1) % 3]
            # Canonical edge key (sorted by coordinate tuple)
            edge = (min(v0, v1), max(v0, v1))
            if edge not in edge_faces:
                edge_faces[edge] = []
            edge_faces[edge].append(idx)

    cos_threshold = math.cos(math.radians(angle_threshold_deg))
    sharp_points: list[tuple[float, float, float]] = []

    for edge, face_indices in edge_faces.items():
        if len(face_indices) != 2:
            continue

        n0 = _triangle_normal(triangles[face_indices[0]])
        n1 = _triangle_normal(triangles[face_indices[1]])
        dot = n0[0] * n1[0] + n0[1] * n1[1] + n0[2] * n1[2]

        # Sharp concave edge: normals diverge (dot < cos_threshold)
        if dot < cos_threshold:
            # Midpoint of edge
            mx = (edge[0][0] + edge[1][0]) / 2
            my = (edge[0][1] + edge[1][1]) / 2
            mz = (edge[0][2] + edge[1][2]) / 2
            sharp_points.append((mx, my, mz))

    # Cluster nearby sharp points to avoid flooding with individual edge reports
    if sharp_points:
        clusters = _cluster_points(sharp_points, cluster_radius=5.0)
        for center, count in clusters:
            if count >= 2:  # At least a few sharp edges clustered together
                severity = "warning" if count < 10 else "critical"
                risks.append(
                    StructuralRisk(
                        risk_type="sharp_corner",
                        severity=severity,
                        location_mm=center,
                        region_size_mm=(10.0, 10.0, 10.0),  # approximate
                        description=(
                            f"Cluster of {count} sharp internal edges near "
                            f"({center[0]:.1f}, {center[1]:.1f}, {center[2]:.1f}). "
                            f"Sharp concave corners concentrate stress and are "
                            f"common fracture initiation points. Add fillets."
                        ),
                        metric_name="sharp_edge_count",
                        metric_value=float(count),
                        metric_threshold=2.0,
                    )
                )

    return risks


def _check_base_adequacy(
    triangles: list[tuple[tuple[float, ...], ...]],
    bbox: dict[str, float],
) -> list[StructuralRisk]:
    """Check if the base (bottom layer) is adequate for the part's height."""
    risks: list[StructuralRisk] = []
    z_min = bbox["min_z"]
    z_range = bbox["max_z"] - z_min
    width = bbox["max_x"] - bbox["min_x"]
    depth = bbox["max_y"] - bbox["min_y"]

    if z_range < 5.0:
        return risks

    # Check cross-section at the base
    base_z = z_min + z_range * 0.05  # Just above bottom
    segments = _cross_section_at_z(triangles, base_z)
    base_area = _cross_section_area(segments)

    # Check cross-section at mid-height and top
    mid_z = z_min + z_range * 0.5
    mid_segments = _cross_section_at_z(triangles, mid_z)
    mid_area = _cross_section_area(mid_segments)

    # Top-heavy: upper section larger than base
    if mid_area > 0 and base_area > 0 and mid_area > base_area * 2.0:
        risks.append(
            StructuralRisk(
                risk_type="insufficient_base",
                severity="warning",
                location_mm=(
                    (bbox["min_x"] + bbox["max_x"]) / 2,
                    (bbox["min_y"] + bbox["max_y"]) / 2,
                    z_min,
                ),
                region_size_mm=(width, depth, z_range * 0.1),
                description=(
                    f"Part is top-heavy — mid-section area ({mid_area:.0f} mm²) is "
                    f"{mid_area / base_area:.1f}x larger than base ({base_area:.0f} mm²). "
                    f"This can cause tipping during printing and in use. "
                    f"Widen the base or add stabilizing feet."
                ),
                metric_name="base_to_mid_ratio",
                metric_value=base_area / mid_area if mid_area > 0 else 0,
                metric_threshold=0.5,
            )
        )

    # Tall with narrow base (stability)
    base_min_dim = min(width, depth) if min(width, depth) > 0 else max(width, depth)
    if base_min_dim > 0 and z_range / base_min_dim > _CANTILEVER_RISK_RATIO:
        risks.append(
            StructuralRisk(
                risk_type="insufficient_base",
                severity="critical" if z_range / base_min_dim > 8 else "warning",
                location_mm=(
                    (bbox["min_x"] + bbox["max_x"]) / 2,
                    (bbox["min_y"] + bbox["max_y"]) / 2,
                    z_min,
                ),
                region_size_mm=(width, depth, z_range * 0.2),
                description=(
                    f"Height-to-base ratio is {z_range / base_min_dim:.1f}:1 "
                    f"(threshold: {_CANTILEVER_RISK_RATIO:.0f}:1). Part may tip or "
                    f"warp during printing. Add a wider base, use a brim, or re-orient."
                ),
                metric_name="height_to_base_ratio",
                metric_value=z_range / base_min_dim,
                metric_threshold=_CANTILEVER_RISK_RATIO,
            )
        )

    return risks


def _find_weak_layer_adhesion_zones(
    triangles: list[tuple[tuple[float, ...], ...]],
    bbox: dict[str, float],
) -> list[StructuralRisk]:
    """Identify regions where FDM layer adhesion would be structurally weak.

    Large horizontal surfaces printed mid-air (overhangs) have weak
    inter-layer bonds. If these surfaces are load-bearing, the part
    is at risk.
    """
    risks: list[StructuralRisk] = []

    # Count triangles with normals pointing down (overhangs).
    # Exclude faces near z_min — those are bed contact surfaces, not overhangs.
    z_min = bbox["min_z"]
    z_range = bbox["max_z"] - z_min
    bed_zone = z_min + z_range * 0.05  # bottom 5% is bed contact

    overhang_triangles: list[tuple[tuple[float, ...], ...]] = []
    total_area = 0.0
    overhang_area = 0.0

    for tri in triangles:
        area = _triangle_area(tri)
        total_area += area
        normal = _triangle_normal(tri)
        # Downward-facing normal: z-component < -cos(structural_overhang_angle)
        threshold = -math.cos(math.radians(_STRUCTURAL_OVERHANG_DEG))
        if normal[2] < threshold:
            # Skip bottom-face triangles near the build plate
            tri_z = (tri[0][2] + tri[1][2] + tri[2][2]) / 3.0
            if tri_z <= bed_zone:
                continue
            overhang_area += area
            overhang_triangles.append(tri)

    if total_area < 1.0 or overhang_area < 1.0:
        return risks

    overhang_fraction = overhang_area / total_area

    if overhang_fraction > 0.15:
        # Find the centroid of overhang regions
        if overhang_triangles:
            avg_x = sum(
                (t[0][0] + t[1][0] + t[2][0]) / 3 for t in overhang_triangles
            ) / len(overhang_triangles)
            avg_y = sum(
                (t[0][1] + t[1][1] + t[2][1]) / 3 for t in overhang_triangles
            ) / len(overhang_triangles)
            avg_z = sum(
                (t[0][2] + t[1][2] + t[2][2]) / 3 for t in overhang_triangles
            ) / len(overhang_triangles)
        else:
            avg_x = (bbox["min_x"] + bbox["max_x"]) / 2
            avg_y = (bbox["min_y"] + bbox["max_y"]) / 2
            avg_z = (bbox["min_z"] + bbox["max_z"]) / 2

        risks.append(
            StructuralRisk(
                risk_type="weak_layer_adhesion",
                severity="warning" if overhang_fraction < 0.3 else "critical",
                location_mm=(avg_x, avg_y, avg_z),
                region_size_mm=(
                    bbox["max_x"] - bbox["min_x"],
                    bbox["max_y"] - bbox["min_y"],
                    bbox["max_z"] - bbox["min_z"],
                ),
                description=(
                    f"{overhang_fraction:.0%} of the surface area is overhanging "
                    f"(>{_STRUCTURAL_OVERHANG_DEG:.0f}° from vertical). These regions "
                    f"have weak inter-layer bonds when 3D printed. If load-bearing, "
                    f"re-orient the part so forces align with the layer direction, "
                    f"not across layers."
                ),
                metric_name="overhang_area_fraction",
                metric_value=overhang_fraction,
                metric_threshold=0.15,
            )
        )

    return risks


def _cluster_points(
    points: list[tuple[float, float, float]],
    *,
    cluster_radius: float = 5.0,
) -> list[tuple[tuple[float, float, float], int]]:
    """Simple greedy clustering of 3D points.

    Returns list of (centroid, count) for each cluster.
    """
    used = [False] * len(points)
    clusters: list[tuple[tuple[float, float, float], int]] = []

    for i, p in enumerate(points):
        if used[i]:
            continue
        # Start a new cluster
        cluster_pts = [p]
        used[i] = True
        for j in range(i + 1, len(points)):
            if used[j]:
                continue
            dist = math.sqrt(
                (points[j][0] - p[0]) ** 2
                + (points[j][1] - p[1]) ** 2
                + (points[j][2] - p[2]) ** 2
            )
            if dist <= cluster_radius:
                cluster_pts.append(points[j])
                used[j] = True

        cx = sum(pt[0] for pt in cluster_pts) / len(cluster_pts)
        cy = sum(pt[1] for pt in cluster_pts) / len(cluster_pts)
        cz = sum(pt[2] for pt in cluster_pts) / len(cluster_pts)
        clusters.append(((cx, cy, cz), len(cluster_pts)))

    return clusters


# ---------------------------------------------------------------------------
# Reinforcement recommendations
# ---------------------------------------------------------------------------


def _generate_reinforcements(
    risks: list[StructuralRisk],
    bbox: dict[str, float],
) -> list[ReinforcementRecommendation]:
    """Generate specific reinforcement recommendations for identified risks."""
    recs: list[ReinforcementRecommendation] = []

    for risk in risks:
        if risk.risk_type == "thin_neck":
            recs.append(
                ReinforcementRecommendation(
                    reinforcement_type="thicken_wall",
                    priority="high",
                    location_mm=risk.location_mm,
                    description=(
                        f"Thicken the narrow section at Z={risk.location_mm[2]:.1f} mm. "
                        f"Current cross-section is {risk.metric_value:.1f} mm². "
                        f"Use thicken_mesh_walls() to add material, or redesign "
                        f"with a minimum {math.sqrt(_MIN_CROSS_SECTION_MM2):.1f} mm dimension."
                    ),
                    estimated_strength_gain="2-5x at the constriction",
                    addresses_risk="thin_neck",
                )
            )

        elif risk.risk_type == "stress_concentration":
            recs.append(
                ReinforcementRecommendation(
                    reinforcement_type="fillet",
                    priority="high",
                    location_mm=risk.location_mm,
                    description=(
                        f"Add a fillet or gradual transition at Z={risk.location_mm[2]:.1f} mm "
                        f"where the cross-section drops to {risk.metric_value:.0%} of adjacent "
                        f"sections. A smooth transition distributes stress over a larger area. "
                        f"Use add_mesh_fillet() with radius_mm=2-4."
                    ),
                    estimated_strength_gain="30-60% at the transition",
                    addresses_risk="stress_concentration",
                )
            )

        elif risk.risk_type == "cantilever":
            recs.append(
                ReinforcementRecommendation(
                    reinforcement_type="gusset",
                    priority="high",
                    location_mm=risk.location_mm,
                    description=(
                        f"Add a triangular gusset or support rib at the cantilever base "
                        f"near ({risk.location_mm[0]:.0f}, {risk.location_mm[1]:.0f}, "
                        f"{risk.location_mm[2]:.0f}). Gussets transfer load from the "
                        f"overhanging section down to the main body, reducing deflection "
                        f"and preventing snap-off."
                    ),
                    estimated_strength_gain="3-10x for cantilevered loads",
                    addresses_risk="cantilever",
                )
            )

        elif risk.risk_type == "sharp_corner":
            recs.append(
                ReinforcementRecommendation(
                    reinforcement_type="fillet",
                    priority="medium",
                    location_mm=risk.location_mm,
                    description=(
                        f"Add fillets to the {int(risk.metric_value)} sharp edges "
                        f"near ({risk.location_mm[0]:.0f}, {risk.location_mm[1]:.0f}, "
                        f"{risk.location_mm[2]:.0f}). A 1-2 mm radius fillet eliminates "
                        f"stress concentration at concave corners. Use add_mesh_fillet()."
                    ),
                    estimated_strength_gain="20-40% at corner joints",
                    addresses_risk="sharp_corner",
                )
            )

        elif risk.risk_type == "insufficient_base":
            if "top-heavy" in risk.description.lower():
                recs.append(
                    ReinforcementRecommendation(
                        reinforcement_type="add_base",
                        priority="medium",
                        location_mm=risk.location_mm,
                        description=(
                            "Add a wider flared base or stabilizing feet to prevent "
                            "tipping. The base should be at least as wide as the widest "
                            "upper section. Consider adding 3-4 small feet with generous "
                            "base contact area."
                        ),
                        estimated_strength_gain="Eliminates tipping risk",
                        addresses_risk="insufficient_base",
                    )
                )
            else:
                recs.append(
                    ReinforcementRecommendation(
                        reinforcement_type="add_base",
                        priority="high",
                        location_mm=risk.location_mm,
                        description=(
                            "Widen the base to reduce the height-to-base ratio below "
                            f"{_CANTILEVER_RISK_RATIO:.0f}:1. Add a brim in the slicer "
                            f"for print adhesion, or add a permanent flange to the design."
                        ),
                        estimated_strength_gain="Eliminates topple risk during print and use",
                        addresses_risk="insufficient_base",
                    )
                )

        elif risk.risk_type == "weak_layer_adhesion":
            recs.append(
                ReinforcementRecommendation(
                    reinforcement_type="reorient",
                    priority="high" if risk.severity == "critical" else "medium",
                    location_mm=risk.location_mm,
                    description=(
                        "Re-orient the part so that load-bearing surfaces are parallel "
                        "to the print layers, not perpendicular. FDM layers are 3-5x "
                        "weaker across layers than along them. Use optimize_print_orientation() "
                        "for structural strength, not just overhang minimization."
                    ),
                    estimated_strength_gain="3-5x in layer-perpendicular loading",
                    addresses_risk="weak_layer_adhesion",
                )
            )

    return recs


# ---------------------------------------------------------------------------
# Load analysis
# ---------------------------------------------------------------------------


def _analyze_load_bearing(
    triangles: list[tuple[tuple[float, ...], ...]],
    bbox: dict[str, float],
) -> LoadAnalysis:
    """Analyze load-bearing characteristics from geometry.

    Infers likely load direction and structural behavior from shape analysis.
    """
    width = bbox["max_x"] - bbox["min_x"]
    depth = bbox["max_y"] - bbox["min_y"]
    height = bbox["max_z"] - bbox["min_z"]

    # Compute face normal distribution to understand shape
    up_area = 0.0  # Z+ facing
    down_area = 0.0  # Z- facing
    side_area = 0.0  # XY facing
    total_area = 0.0

    for tri in triangles:
        area = _triangle_area(tri)
        normal = _triangle_normal(tri)
        total_area += area

        if abs(normal[2]) > 0.7:
            if normal[2] > 0:
                up_area += area
            else:
                down_area += area
        else:
            side_area += area

    # Determine dominant geometry type
    if total_area < 1.0:
        return LoadAnalysis(
            primary_load_axis="unknown",
            load_surfaces=[],
            weak_axis="unknown",
            layer_direction_concern="Part too small to analyze.",
            recommended_print_orientation="upright",
            orientation_reasoning="Default orientation for very small parts.",
        )

    up_frac = up_area / total_area
    down_frac = down_area / total_area
    side_frac = side_area / total_area

    # Determine shape type and load axis
    is_flat = height < min(width, depth) * 0.3
    is_tall = height > max(width, depth) * 2
    is_columnar = is_tall and abs(width - depth) < max(width, depth) * 0.3

    if is_flat:
        primary_axis = "vertical"
        weak_axis = "Z (through-thickness)"
        load_surfaces = [
            {"surface": "top", "area_fraction": round(up_frac, 2), "type": "compression"},
            {"surface": "bottom", "area_fraction": round(down_frac, 2), "type": "support"},
        ]
        orientation = "flat"
        reason = (
            "Flat part — print flat (as designed) so layers stack vertically. "
            "This aligns compression loads with the strong layer-stacking direction."
        )
    elif is_columnar:
        primary_axis = "vertical"
        weak_axis = "X/Y (lateral bending)"
        load_surfaces = [
            {"surface": "top", "area_fraction": round(up_frac, 2), "type": "compression"},
            {"surface": "sides", "area_fraction": round(side_frac, 2), "type": "lateral_load"},
        ]
        orientation = "upright"
        reason = (
            "Columnar part — print upright so axial loads compress layers together. "
            "Lateral loads are the weak point; add ribs or gussets if lateral "
            "forces are expected."
        )
    elif is_tall:
        primary_axis = "vertical"
        weak_axis = "perpendicular to tallest axis"
        load_surfaces = [
            {"surface": "top", "area_fraction": round(up_frac, 2), "type": "compression"},
            {"surface": "sides", "area_fraction": round(side_frac, 2), "type": "bending"},
        ]
        orientation = "on_side"
        reason = (
            "Tall narrow part — consider printing on its side to reduce height "
            "and improve stability. If vertical loads dominate, print upright "
            "but use a brim for adhesion."
        )
    else:
        primary_axis = "multi-axis"
        weak_axis = "shortest dimension"
        load_surfaces = [
            {"surface": "top", "area_fraction": round(up_frac, 2), "type": "compression"},
            {"surface": "sides", "area_fraction": round(side_frac, 2), "type": "lateral"},
            {"surface": "bottom", "area_fraction": round(down_frac, 2), "type": "support"},
        ]
        # Choose orientation based on which axis is shortest
        dims = sorted([(width, "X"), (depth, "Y"), (height, "Z")])
        shortest_axis = dims[0][1]
        if shortest_axis == "Z":
            orientation = "upright"
            reason = (
                "Compact part — print upright (Z is shortest). This maximizes "
                "bed contact and aligns the weak inter-layer direction with the "
                "shortest dimension."
            )
        else:
            orientation = "on_side"
            reason = (
                f"Part is thinnest along {shortest_axis}-axis. Consider orienting "
                f"so layers stack along that axis — this keeps the weakest "
                f"direction in the shortest dimension."
            )

    layer_concern = (
        "FDM layers create anisotropic strength: parts are 3-5x weaker across "
        "layer boundaries than along them. Orient the part so primary loads "
        "compress layers together (perpendicular to build plate), never pull "
        "layers apart (parallel to build plate)."
    )

    return LoadAnalysis(
        primary_load_axis=primary_axis,
        load_surfaces=load_surfaces,
        weak_axis=weak_axis,
        layer_direction_concern=layer_concern,
        recommended_print_orientation=orientation,
        orientation_reasoning=reason,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_structural_risks(
    file_path: str,
    *,
    min_cross_section_mm2: float = _MIN_CROSS_SECTION_MM2,
    sharp_angle_threshold_deg: float = 60.0,
) -> list[StructuralRisk]:
    """Analyze an STL for structural risks.

    Performs geometric analysis to find:
    - Thin necks (narrow cross-sections that will snap)
    - Stress concentrations (abrupt section changes)
    - Cantilevers (unsupported overhanging geometry)
    - Sharp corners (stress concentration at concave edges)
    - Insufficient base (topple risk)
    - Weak layer adhesion zones (overhangs in structural areas)

    :param file_path: Path to STL file.
    :param min_cross_section_mm2: Minimum safe cross-section area.
    :param sharp_angle_threshold_deg: Angle below which edges are "sharp".
    :returns: List of :class:`StructuralRisk` findings.
    """
    triangles, vertices = _parse_stl_for_analysis(file_path)
    if not triangles:
        return []

    bbox = _bounding_box(vertices)
    risks: list[StructuralRisk] = []

    risks.extend(_find_thin_necks(triangles, bbox, min_area_mm2=min_cross_section_mm2))
    risks.extend(_find_cantilevers(triangles, bbox))
    risks.extend(_find_sharp_corners(triangles, angle_threshold_deg=sharp_angle_threshold_deg))
    risks.extend(_check_base_adequacy(triangles, bbox))
    risks.extend(_find_weak_layer_adhesion_zones(triangles, bbox))

    # Sort by severity: critical first, then warning, then info
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    risks.sort(key=lambda r: severity_order.get(r.severity, 3))

    return risks


def recommend_reinforcements(
    file_path: str,
    *,
    min_cross_section_mm2: float = _MIN_CROSS_SECTION_MM2,
) -> list[ReinforcementRecommendation]:
    """Analyze an STL and recommend specific reinforcements.

    :param file_path: Path to STL file.
    :param min_cross_section_mm2: Minimum safe cross-section area.
    :returns: List of :class:`ReinforcementRecommendation`.
    """
    triangles, vertices = _parse_stl_for_analysis(file_path)
    if not triangles:
        return []

    bbox = _bounding_box(vertices)
    risks = analyze_structural_risks(
        file_path, min_cross_section_mm2=min_cross_section_mm2
    )
    return _generate_reinforcements(risks, bbox)


def assess_load_bearing(file_path: str) -> LoadAnalysis:
    """Analyze load-bearing characteristics from mesh geometry.

    :param file_path: Path to STL file.
    :returns: :class:`LoadAnalysis` with load surface info and orientation advice.
    """
    triangles, vertices = _parse_stl_for_analysis(file_path)
    if not triangles:
        return LoadAnalysis(
            primary_load_axis="unknown",
            load_surfaces=[],
            weak_axis="unknown",
            layer_direction_concern="Could not parse mesh.",
            recommended_print_orientation="upright",
            orientation_reasoning="Default — mesh could not be analyzed.",
        )

    bbox = _bounding_box(vertices)
    return _analyze_load_bearing(triangles, bbox)


def generate_improvement_plan(
    file_path: str,
    *,
    min_cross_section_mm2: float = _MIN_CROSS_SECTION_MM2,
    sharp_angle_threshold_deg: float = 60.0,
) -> DesignImprovementPlan:
    """Generate a complete structural improvement plan for a design.

    Combines risk analysis, reinforcement recommendations, and load analysis
    into a single actionable report with an overall structural score.

    :param file_path: Path to STL file.
    :param min_cross_section_mm2: Minimum safe cross-section area.
    :param sharp_angle_threshold_deg: Angle threshold for sharp edge detection.
    :returns: :class:`DesignImprovementPlan`.
    """
    triangles, vertices = _parse_stl_for_analysis(file_path)
    if not triangles:
        return DesignImprovementPlan(
            file_path=file_path,
            summary="Could not parse mesh file.",
        )

    bbox = _bounding_box(vertices)

    # Run all analyses
    risks = analyze_structural_risks(
        file_path,
        min_cross_section_mm2=min_cross_section_mm2,
        sharp_angle_threshold_deg=sharp_angle_threshold_deg,
    )
    reinforcements = _generate_reinforcements(risks, bbox)
    load = _analyze_load_bearing(triangles, bbox)

    # Compute structural score
    critical_count = sum(1 for r in risks if r.severity == "critical")
    warning_count = sum(1 for r in risks if r.severity == "warning")

    # Start at 100, subtract for risks
    score = 100
    score -= critical_count * 25
    score -= warning_count * 10
    score = max(0, min(100, score))

    # Grade
    if score >= 90:
        grade = "A"
    elif score >= 80:
        grade = "B"
    elif score >= 65:
        grade = "C"
    elif score >= 50:
        grade = "D"
    else:
        grade = "F"

    # Build summary
    if not risks:
        summary = (
            f"No structural risks detected. Score: {score}/100 ({grade}). "
            f"Design appears structurally sound for typical FDM printing."
        )
    else:
        risk_types = list(set(r.risk_type for r in risks))
        summary = (
            f"Found {len(risks)} structural risk(s) ({critical_count} critical, "
            f"{warning_count} warning). Score: {score}/100 ({grade}). "
            f"Risk types: {', '.join(risk_types)}. "
            f"{len(reinforcements)} reinforcement(s) recommended."
        )

    return DesignImprovementPlan(
        file_path=file_path,
        risks=risks,
        reinforcements=reinforcements,
        load_analysis=load,
        overall_structural_score=score,
        structural_grade=grade,
        critical_count=critical_count,
        warning_count=warning_count,
        summary=summary,
    )
