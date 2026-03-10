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
    apply_reinforcements       — auto-apply fixes (thicken, fillet, base, gusset)
    infer_print_settings       — structural risks → optimal slicer settings
    StructuralRisk             — single risk finding dataclass
    ReinforcementRecommendation — single fix recommendation dataclass
    ReinforcementResult        — result of auto-applying reinforcements
    PrintSettingsRecommendation — recommended slicer settings dataclass
    DesignImprovementPlan      — complete plan dataclass
"""

from __future__ import annotations

import contextlib
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


# ---------------------------------------------------------------------------
# Auto-apply reinforcements
# ---------------------------------------------------------------------------


@dataclass
class ReinforcementResult:
    """Result of applying reinforcements to a mesh."""

    output_path: str
    original_path: str
    applied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    before_score: int = 0
    after_score: int = 0
    before_grade: str = ""
    after_grade: str = ""
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": self.output_path,
            "original_path": self.original_path,
            "applied": self.applied,
            "skipped": self.skipped,
            "before_score": self.before_score,
            "after_score": self.after_score,
            "before_grade": self.before_grade,
            "after_grade": self.after_grade,
            "summary": self.summary,
        }


@dataclass
class PrintSettingsRecommendation:
    """Recommended print settings based on structural analysis."""

    perimeters: int
    infill_percent: int
    infill_pattern: str
    layer_height_mm: float
    support_enabled: bool
    support_reason: str
    brim_enabled: bool
    brim_reason: str
    print_orientation: str
    orientation_reason: str
    special_notes: list[str] = field(default_factory=list)
    confidence: str = "high"  # "high", "medium", "low"

    def to_dict(self) -> dict[str, Any]:
        return {
            "perimeters": self.perimeters,
            "infill_percent": self.infill_percent,
            "infill_pattern": self.infill_pattern,
            "layer_height_mm": self.layer_height_mm,
            "support_enabled": self.support_enabled,
            "support_reason": self.support_reason,
            "brim_enabled": self.brim_enabled,
            "brim_reason": self.brim_reason,
            "print_orientation": self.print_orientation,
            "orientation_reason": self.orientation_reason,
            "special_notes": self.special_notes,
            "confidence": self.confidence,
        }


def apply_reinforcements(
    file_path: str,
    *,
    output_path: str | None = None,
    min_cross_section_mm2: float = _MIN_CROSS_SECTION_MM2,
    sharp_angle_threshold_deg: float = 60.0,
    fillet_radius_mm: float = 1.5,
    wall_thicken_mm: float = 0.6,
    base_height_mm: float = 2.0,
) -> ReinforcementResult:
    """Analyze a mesh, then auto-apply structural reinforcements.

    Runs the full improvement plan, then applies fixable reinforcements
    in sequence:

    1. **thicken_wall** → runs ``thicken_walls()`` on thin sections
    2. **fillet** → runs ``add_fillet()`` on sharp edges
    3. **add_base** → unions a wider base plate via OpenSCAD boolean
    4. **gusset** → unions triangular gusset ribs at cantilever bases

    Reinforcements that can't be auto-applied (like ``reorient``) are
    listed in ``skipped`` with guidance for the agent.

    :param file_path: Path to the input STL file.
    :param output_path: Output path (defaults to ``<name>_reinforced.stl``).
    :param min_cross_section_mm2: Minimum safe cross-section area.
    :param sharp_angle_threshold_deg: Angle for sharp edge detection.
    :param fillet_radius_mm: Fillet radius for sharp corners.
    :param wall_thicken_mm: Amount to thicken thin walls.
    :param base_height_mm: Height of stabilizing base plate.
    :returns: :class:`ReinforcementResult` with before/after scores.
    """
    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"File not found: {file_path}")

    # Step 1: Get the improvement plan (before state)
    plan = generate_improvement_plan(
        file_path,
        min_cross_section_mm2=min_cross_section_mm2,
        sharp_angle_threshold_deg=sharp_angle_threshold_deg,
    )

    if not plan.reinforcements:
        out = output_path or str(path)
        if output_path and output_path != file_path:
            import shutil
            shutil.copy2(file_path, output_path)
        return ReinforcementResult(
            output_path=out,
            original_path=file_path,
            before_score=plan.overall_structural_score,
            after_score=plan.overall_structural_score,
            before_grade=plan.structural_grade,
            after_grade=plan.structural_grade,
            summary="No reinforcements needed — design is structurally sound.",
        )

    # Step 2: Apply reinforcements in priority order
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    # Work on a temp copy so we can chain operations
    import shutil
    import tempfile

    work_dir = tempfile.mkdtemp(prefix="kiln_reinforce_")
    current_path = str(Path(work_dir) / "working.stl")
    shutil.copy2(file_path, current_path)

    # Sort by priority: high first
    priority_order = {"high": 0, "medium": 1, "low": 2}
    sorted_recs = sorted(
        plan.reinforcements,
        key=lambda r: priority_order.get(r.priority, 3),
    )

    for rec in sorted_recs:
        try:
            if rec.reinforcement_type == "thicken_wall":
                result = _apply_thicken(current_path, work_dir, wall_thicken_mm)
                if result:
                    current_path = result
                    applied.append({
                        "type": "thicken_wall",
                        "amount_mm": wall_thicken_mm,
                        "addresses": rec.addresses_risk,
                    })
                else:
                    skipped.append({
                        "type": "thicken_wall",
                        "reason": "No thin walls detected in mesh",
                        "addresses": rec.addresses_risk,
                    })

            elif rec.reinforcement_type == "fillet":
                result = _apply_fillet(
                    current_path, work_dir,
                    fillet_radius_mm, sharp_angle_threshold_deg,
                )
                if result:
                    current_path = result
                    applied.append({
                        "type": "fillet",
                        "radius_mm": fillet_radius_mm,
                        "addresses": rec.addresses_risk,
                    })
                else:
                    skipped.append({
                        "type": "fillet",
                        "reason": "No sharp edges found at threshold",
                        "addresses": rec.addresses_risk,
                    })

            elif rec.reinforcement_type == "add_base":
                result = _apply_base(
                    current_path, work_dir, base_height_mm,
                )
                if result:
                    current_path = result
                    applied.append({
                        "type": "add_base",
                        "height_mm": base_height_mm,
                        "addresses": rec.addresses_risk,
                    })
                else:
                    skipped.append({
                        "type": "add_base",
                        "reason": "Could not generate base (OpenSCAD not available)",
                        "addresses": rec.addresses_risk,
                    })

            elif rec.reinforcement_type == "gusset":
                result = _apply_gusset(
                    current_path, work_dir, rec.location_mm,
                )
                if result:
                    current_path = result
                    applied.append({
                        "type": "gusset",
                        "location_mm": list(rec.location_mm),
                        "addresses": rec.addresses_risk,
                    })
                else:
                    skipped.append({
                        "type": "gusset",
                        "reason": "Could not generate gusset (OpenSCAD not available)",
                        "addresses": rec.addresses_risk,
                    })

            elif rec.reinforcement_type == "reorient":
                # Can't auto-apply rotation — agent needs to decide
                skipped.append({
                    "type": "reorient",
                    "reason": (
                        "Print orientation must be changed in the slicer, not the mesh. "
                        "Recommended orientation: see load analysis."
                    ),
                    "addresses": rec.addresses_risk,
                    "guidance": rec.description,
                })

            elif rec.reinforcement_type == "chamfer":
                result = _apply_chamfer(
                    current_path, work_dir, sharp_angle_threshold_deg,
                )
                if result:
                    current_path = result
                    applied.append({
                        "type": "chamfer",
                        "addresses": rec.addresses_risk,
                    })
                else:
                    skipped.append({
                        "type": "chamfer",
                        "reason": "No sharp edges at threshold",
                        "addresses": rec.addresses_risk,
                    })

            else:
                skipped.append({
                    "type": rec.reinforcement_type,
                    "reason": f"Unknown reinforcement type: {rec.reinforcement_type}",
                    "addresses": rec.addresses_risk,
                })

        except Exception as exc:
            logger.warning(
                "Reinforcement %s failed: %s", rec.reinforcement_type, exc
            )
            skipped.append({
                "type": rec.reinforcement_type,
                "reason": f"Application failed: {exc}",
                "addresses": rec.addresses_risk,
            })

    # Step 3: Copy result to output path
    if output_path is None:
        stem = path.stem
        output_path = str(path.parent / f"{stem}_reinforced.stl")

    shutil.copy2(current_path, output_path)

    # Step 4: Re-score the reinforced mesh
    after_plan = generate_improvement_plan(
        output_path,
        min_cross_section_mm2=min_cross_section_mm2,
        sharp_angle_threshold_deg=sharp_angle_threshold_deg,
    )

    # Clean up temp dir
    with contextlib.suppress(Exception):
        shutil.rmtree(work_dir, ignore_errors=True)

    summary_parts = []
    if applied:
        summary_parts.append(
            f"Applied {len(applied)} reinforcement(s): "
            f"{', '.join(a['type'] for a in applied)}."
        )
    if skipped:
        summary_parts.append(
            f"Skipped {len(skipped)}: "
            f"{', '.join(s['type'] for s in skipped)}."
        )
    score_delta = after_plan.overall_structural_score - plan.overall_structural_score
    if score_delta > 0:
        summary_parts.append(
            f"Score improved {plan.overall_structural_score} → "
            f"{after_plan.overall_structural_score} "
            f"({plan.structural_grade} → {after_plan.structural_grade})."
        )
    elif score_delta == 0 and applied:
        summary_parts.append(
            f"Score unchanged at {plan.overall_structural_score} "
            f"({plan.structural_grade}) — improvements may be micro-level."
        )

    return ReinforcementResult(
        output_path=output_path,
        original_path=file_path,
        applied=applied,
        skipped=skipped,
        before_score=plan.overall_structural_score,
        after_score=after_plan.overall_structural_score,
        before_grade=plan.structural_grade,
        after_grade=after_plan.structural_grade,
        summary=" ".join(summary_parts),
    )


def _apply_thicken(
    stl_path: str,
    work_dir: str,
    amount_mm: float,
) -> str | None:
    """Apply wall thickening and return new path, or None on failure."""
    try:
        from kiln.generation.validation import thicken_walls

        out = str(Path(work_dir) / "thickened.stl")
        result = thicken_walls(stl_path, amount_mm=amount_mm, output_path=out)
        if result.get("vertices_modified", 0) > 0:
            return out
        return None
    except Exception:
        return None


def _apply_fillet(
    stl_path: str,
    work_dir: str,
    radius_mm: float,
    angle_deg: float,
) -> str | None:
    """Apply fillets to sharp edges and return new path, or None on failure."""
    try:
        from kiln.generation.validation import add_fillet

        out = str(Path(work_dir) / "filleted.stl")
        result = add_fillet(
            stl_path,
            radius_mm=radius_mm,
            angle_threshold_deg=angle_deg,
            output_path=out,
        )
        if result.get("sharp_edges_found", 0) > 0:
            return out
        return None
    except Exception:
        return None


def _apply_chamfer(
    stl_path: str,
    work_dir: str,
    angle_deg: float,
) -> str | None:
    """Apply chamfers to sharp edges and return new path, or None."""
    try:
        from kiln.generation.validation import add_chamfer

        out = str(Path(work_dir) / "chamfered.stl")
        result = add_chamfer(
            stl_path,
            distance_mm=0.5,
            angle_threshold_deg=angle_deg,
            output_path=out,
        )
        if result.get("sharp_edges_found", 0) > 0:
            return out
        return None
    except Exception:
        return None


def _apply_base(
    stl_path: str,
    work_dir: str,
    height_mm: float,
) -> str | None:
    """Add a stabilizing base plate via OpenSCAD boolean union."""
    try:
        from kiln.generation.openscad import _find_openscad, boolean_mesh_operation

        # Need OpenSCAD for boolean union
        _find_openscad()

        # Parse mesh to get bounding box
        _, vertices = _parse_stl_for_analysis(stl_path)
        if not vertices:
            return None
        bbox = _bounding_box(vertices)

        # Create a base plate wider than the part
        width_x = (bbox["max_x"] - bbox["min_x"]) * 1.3
        width_y = (bbox["max_y"] - bbox["min_y"]) * 1.3
        cx = (bbox["min_x"] + bbox["max_x"]) / 2
        cy = (bbox["min_y"] + bbox["max_y"]) / 2

        # Generate base plate as OpenSCAD → STL
        from kiln.generation.openscad import compose_from_primitives

        base_path = str(Path(work_dir) / "base_plate.stl")
        compose_from_primitives(
            [
                {
                    "type": "primitive",
                    "shape": "cube",
                    "params": {"size": [width_x, width_y, height_mm]},
                    "translate": [
                        cx - width_x / 2,
                        cy - width_y / 2,
                        bbox["min_z"] - height_mm,
                    ],
                }
            ],
            output_path=base_path,
        )

        # Union the base with the original
        out = str(Path(work_dir) / "with_base.stl")
        boolean_mesh_operation("union", [stl_path, base_path], output_path=out)
        return out

    except Exception as exc:
        logger.debug("Base plate application failed: %s", exc)
        return None


def _apply_gusset(
    stl_path: str,
    work_dir: str,
    location_mm: tuple[float, float, float],
) -> str | None:
    """Add a triangular gusset rib at a cantilever base via OpenSCAD."""
    try:
        from kiln.generation.openscad import _find_openscad, boolean_mesh_operation

        _find_openscad()

        # Parse mesh for context
        _, vertices = _parse_stl_for_analysis(stl_path)
        if not vertices:
            return None
        bbox = _bounding_box(vertices)

        # Gusset dimensions: proportional to part size
        gusset_thickness = 2.0  # mm
        gusset_size = min(
            (bbox["max_x"] - bbox["min_x"]) * 0.3,
            (bbox["max_z"] - bbox["min_z"]) * 0.3,
            15.0,  # cap at 15mm
        )
        gusset_size = max(gusset_size, 3.0)  # minimum 3mm

        # Create a triangular gusset using OpenSCAD polyhedron
        # The gusset sits at the identified location as a right-triangle rib
        x, y, z = location_mm

        # Generate gusset as a right-triangle prism via OpenSCAD code
        import subprocess

        scad_code = f"""
// Triangular gusset rib
translate([{x - gusset_thickness / 2}, {y}, {bbox['min_z']}])
linear_extrude(height={gusset_thickness})
polygon(points=[
    [0, 0],
    [{gusset_size}, 0],
    [0, {gusset_size}]
]);
"""
        scad_path = str(Path(work_dir) / "gusset.scad")
        gusset_stl = str(Path(work_dir) / "gusset.stl")
        Path(scad_path).write_text(scad_code)

        from kiln.generation.openscad import _find_openscad as _find

        binary = _find()
        subprocess.run(
            [binary, "-o", gusset_stl, scad_path],
            capture_output=True,
            timeout=30,
            check=False,
        )

        if not Path(gusset_stl).is_file() or Path(gusset_stl).stat().st_size < 100:
            return None

        # Union the gusset with the part
        out = str(Path(work_dir) / "with_gusset.stl")
        boolean_mesh_operation("union", [stl_path, gusset_stl], output_path=out)
        return out

    except Exception as exc:
        logger.debug("Gusset application failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Slicer profile inference from structural analysis
# ---------------------------------------------------------------------------


def infer_print_settings(
    file_path: str,
    *,
    material: str = "PLA",
    min_cross_section_mm2: float = _MIN_CROSS_SECTION_MM2,
    sharp_angle_threshold_deg: float = 60.0,
) -> PrintSettingsRecommendation:
    """Infer optimal print settings from structural analysis.

    Bridges the gap between "this design has structural risks" and
    "here are the slicer settings that compensate."  Analyzes the mesh,
    considers the material, and returns concrete slicer parameters.

    :param file_path: Path to STL file.
    :param material: Filament material (PLA, PETG, ABS, Nylon, TPU).
    :param min_cross_section_mm2: Minimum safe cross-section area.
    :param sharp_angle_threshold_deg: Angle for sharp edge detection.
    :returns: :class:`PrintSettingsRecommendation`.
    """
    plan = generate_improvement_plan(
        file_path,
        min_cross_section_mm2=min_cross_section_mm2,
        sharp_angle_threshold_deg=sharp_angle_threshold_deg,
    )

    # Material-specific defaults
    mat = material.upper()
    defaults = _MATERIAL_DEFAULTS.get(mat, _MATERIAL_DEFAULTS["PLA"])

    perimeters = defaults["perimeters"]
    infill = defaults["infill"]
    infill_pattern = defaults["infill_pattern"]
    layer_height = defaults["layer_height"]
    support = False
    support_reason = "No overhangs requiring support detected."
    brim = False
    brim_reason = "Base contact area is adequate."
    notes: list[str] = []

    # Adjust based on structural risks
    has_thin_neck = any(r.risk_type == "thin_neck" for r in plan.risks)
    has_stress_conc = any(r.risk_type == "stress_concentration" for r in plan.risks)
    has_cantilever = any(r.risk_type == "cantilever" for r in plan.risks)
    has_weak_adhesion = any(r.risk_type == "weak_layer_adhesion" for r in plan.risks)
    has_insufficient_base = any(
        r.risk_type == "insufficient_base" for r in plan.risks
    )
    has_sharp_corners = any(r.risk_type == "sharp_corner" for r in plan.risks)

    # Thin necks → more perimeters for wall strength
    if has_thin_neck:
        perimeters = max(perimeters, 4)
        infill = max(infill, 40)
        notes.append(
            "Increased perimeters to 4+ for thin-neck zones. "
            "More perimeters add structural shells around narrow sections."
        )

    # Stress concentration → higher infill for load distribution
    if has_stress_conc:
        infill = max(infill, 50)
        infill_pattern = "gyroid"
        notes.append(
            "Switched to gyroid infill at 50%+ for uniform stress distribution "
            "at cross-section transitions."
        )

    # Cantilever → enable supports, increase perimeters
    if has_cantilever:
        support = True
        support_reason = (
            "Cantilever geometry detected — supports prevent drooping "
            "during printing and maintain dimensional accuracy."
        )
        perimeters = max(perimeters, 3)
        notes.append(
            "Supports enabled for cantilever overhangs. Consider tree supports "
            "for easier removal."
        )

    # Weak layer adhesion → slower speed, higher temp, more perimeters
    if has_weak_adhesion:
        perimeters = max(perimeters, 4)
        infill = max(infill, 35)
        notes.append(
            "Weak layer adhesion zones detected. Increase nozzle temperature "
            "by 5-10°C and reduce print speed by 20% in the slicer for better "
            "inter-layer bonding at overhang surfaces."
        )

    # Insufficient base → add brim
    if has_insufficient_base:
        brim = True
        brim_reason = (
            "Design has high height-to-base ratio. A brim provides additional "
            "bed adhesion to prevent tipping or warping during printing."
        )

    # Sharp corners → finer layers for detail
    if has_sharp_corners:
        layer_height = min(layer_height, 0.16)
        notes.append(
            "Fine layer height recommended for sharp corner detail. "
            "Thinner layers better approximate curved transitions."
        )

    # Determine print orientation from load analysis
    orientation = "upright"
    orientation_reason = "Default upright orientation."
    if plan.load_analysis:
        orientation = plan.load_analysis.recommended_print_orientation
        orientation_reason = plan.load_analysis.orientation_reasoning

    # Grade-based overall adjustments
    if plan.structural_grade in ("D", "F"):
        infill = max(infill, 50)
        perimeters = max(perimeters, 4)
        notes.append(
            f"Structural grade {plan.structural_grade} — using high infill "
            f"and perimeters to compensate for geometry weaknesses."
        )

    # Confidence assessment
    if plan.critical_count == 0 and plan.warning_count <= 2:
        confidence = "high"
    elif plan.critical_count <= 1:
        confidence = "medium"
    else:
        confidence = "low"

    return PrintSettingsRecommendation(
        perimeters=perimeters,
        infill_percent=infill,
        infill_pattern=infill_pattern,
        layer_height_mm=layer_height,
        support_enabled=support,
        support_reason=support_reason,
        brim_enabled=brim,
        brim_reason=brim_reason,
        print_orientation=orientation,
        orientation_reason=orientation_reason,
        special_notes=notes,
        confidence=confidence,
    )


# Material-specific slicer defaults
_MATERIAL_DEFAULTS: dict[str, dict[str, Any]] = {
    "PLA": {
        "perimeters": 3,
        "infill": 20,
        "infill_pattern": "grid",
        "layer_height": 0.2,
    },
    "PETG": {
        "perimeters": 3,
        "infill": 25,
        "infill_pattern": "grid",
        "layer_height": 0.2,
    },
    "ABS": {
        "perimeters": 3,
        "infill": 25,
        "infill_pattern": "gyroid",
        "layer_height": 0.2,
    },
    "NYLON": {
        "perimeters": 4,
        "infill": 30,
        "infill_pattern": "gyroid",
        "layer_height": 0.2,
    },
    "TPU": {
        "perimeters": 3,
        "infill": 15,
        "infill_pattern": "gyroid",
        "layer_height": 0.24,
    },
    "ASA": {
        "perimeters": 3,
        "infill": 25,
        "infill_pattern": "gyroid",
        "layer_height": 0.2,
    },
    "PC": {
        "perimeters": 4,
        "infill": 30,
        "infill_pattern": "gyroid",
        "layer_height": 0.2,
    },
}


# ---------------------------------------------------------------------------
# Parametric optimization
# ---------------------------------------------------------------------------


@dataclass
class OptimizationResult:
    """Result of parametric template optimization."""

    template_id: str
    best_params: dict[str, Any]
    best_score: int
    best_grade: str
    best_stl_path: str
    variants_tested: int
    all_scores: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "best_params": self.best_params,
            "best_score": self.best_score,
            "best_grade": self.best_grade,
            "best_stl_path": self.best_stl_path,
            "variants_tested": self.variants_tested,
            "all_scores": self.all_scores,
            "summary": self.summary,
        }


def optimize_template_params(
    template_id: str,
    *,
    templates_path: str | None = None,
    samples_per_param: int = 3,
    max_variants: int = 27,
    constraints: dict[str, Any] | None = None,
    output_dir: str | None = None,
) -> OptimizationResult:
    """Sweep template parameters to find the structurally strongest variant.

    Samples each parameter at ``samples_per_param`` evenly spaced points
    within its [min, max] range, generates each variant via OpenSCAD,
    runs structural analysis, and returns the highest-scoring configuration.

    :param template_id: Template ID from design_templates.json.
    :param templates_path: Path to templates JSON (auto-detected if None).
    :param samples_per_param: Number of sample points per parameter (default 3).
    :param max_variants: Maximum total variants to test (default 27).
    :param constraints: Optional constraints like ``{"max_width_mm": 100}``.
    :param output_dir: Directory for generated STLs (temp dir if None).
    :returns: :class:`OptimizationResult` with best params and STL path.
    """
    import itertools
    import json
    import tempfile
    from string import Template

    # Load templates
    if templates_path is None:
        tpl_path = Path(__file__).parent / "data" / "design_templates.json"
    else:
        tpl_path = Path(templates_path)

    if not tpl_path.is_file():
        raise ValueError(f"Templates file not found: {tpl_path}")

    with open(tpl_path) as fh:
        data = json.load(fh)

    tpl = data.get(template_id)
    if not tpl or template_id.startswith("_"):
        raise ValueError(f"Template {template_id!r} not found")

    params_spec = tpl.get("parameters", {})
    scad_template = tpl.get("scad_template", "")

    if not params_spec:
        raise ValueError(f"Template {template_id!r} has no parameters to optimize")

    # Generate parameter sample points
    param_names: list[str] = []
    param_values: list[list[float]] = []

    for name, spec in params_spec.items():
        pmin = spec.get("min", spec.get("default", 0))
        pmax = spec.get("max", spec.get("default", 100))
        default = spec.get("default", (pmin + pmax) / 2)

        # Sample points: min, evenly spaced midpoints, max
        if samples_per_param <= 1:
            points = [default]
        elif samples_per_param == 2:
            points = [pmin, pmax]
        else:
            step = (pmax - pmin) / (samples_per_param - 1)
            points = [pmin + i * step for i in range(samples_per_param)]

        # Round to reasonable precision
        unit = spec.get("unit", "mm")
        if unit == "degrees":
            points = [round(p, 1) for p in points]
        else:
            points = [round(p, 2) for p in points]

        param_names.append(name)
        param_values.append(points)

    # Generate parameter combinations (limited by max_variants)
    all_combos = list(itertools.product(*param_values))
    if len(all_combos) > max_variants:
        # Sample evenly
        step = len(all_combos) / max_variants
        indices = [int(i * step) for i in range(max_variants)]
        all_combos = [all_combos[i] for i in indices]

    # Set up working directory
    work_dir = output_dir or tempfile.mkdtemp(prefix="kiln_optimize_")
    Path(work_dir).mkdir(parents=True, exist_ok=True)

    # Try to find OpenSCAD
    try:
        from kiln.generation.openscad import _find_openscad

        openscad_binary = _find_openscad()
    except Exception as exc:
        raise ValueError(
            "OpenSCAD is required for template optimization but was not found. "
            "Install from https://openscad.org/"
        ) from exc

    best_score = -1
    best_grade = "F"
    best_params: dict[str, Any] = {}
    best_stl = ""
    all_scores: list[dict[str, Any]] = []

    import subprocess

    for idx, combo in enumerate(all_combos):
        params = dict(zip(param_names, combo, strict=False))

        # Apply constraints
        if constraints:
            skip = False
            max_width = constraints.get("max_width_mm")
            max_depth = constraints.get("max_depth_mm")
            max_height = constraints.get("max_height_mm")
            # Simple heuristic: check if common dimension params exceed constraints
            for pname, pval in params.items():
                if max_width and "width" in pname.lower() and pval > max_width:
                    skip = True
                if max_depth and "depth" in pname.lower() and pval > max_depth:
                    skip = True
                if max_height and "height" in pname.lower() and pval > max_height:
                    skip = True
            if skip:
                continue

        # Generate SCAD code
        scad_code = Template(scad_template).safe_substitute(params)
        scad_path = str(Path(work_dir) / f"variant_{idx}.scad")
        stl_path = str(Path(work_dir) / f"variant_{idx}.stl")

        Path(scad_path).write_text(scad_code)

        # Compile to STL
        try:
            proc = subprocess.run(
                [openscad_binary, "-o", stl_path, scad_path],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if proc.returncode != 0 or not Path(stl_path).is_file():
                continue
            if Path(stl_path).stat().st_size < 100:
                continue
        except Exception:
            continue

        # Score structurally
        try:
            plan = generate_improvement_plan(stl_path)
            score = plan.overall_structural_score
            grade = plan.structural_grade

            all_scores.append({
                "params": params,
                "score": score,
                "grade": grade,
                "risk_count": len(plan.risks),
                "stl_path": stl_path,
            })

            if score > best_score:
                best_score = score
                best_grade = grade
                best_params = params
                best_stl = stl_path
        except Exception:
            continue

    if not all_scores:
        raise ValueError(
            f"No valid variants could be generated for template {template_id!r}"
        )

    # Clean up non-best STLs if using temp dir
    if output_dir is None:
        for entry in all_scores:
            if entry["stl_path"] != best_stl:
                with contextlib.suppress(Exception):
                    Path(entry["stl_path"]).unlink()
                scad = entry["stl_path"].replace(".stl", ".scad")
                with contextlib.suppress(Exception):
                    Path(scad).unlink()

    summary = (
        f"Tested {len(all_scores)} variants of {tpl.get('display_name', template_id)}. "
        f"Best score: {best_score}/100 ({best_grade}). "
        f"Score range: {min(s['score'] for s in all_scores)}-{max(s['score'] for s in all_scores)}."
    )

    return OptimizationResult(
        template_id=template_id,
        best_params=best_params,
        best_score=best_score,
        best_grade=best_grade,
        best_stl_path=best_stl,
        variants_tested=len(all_scores),
        all_scores=all_scores,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Multi-part plate arrangement
# ---------------------------------------------------------------------------


@dataclass
class PlateArrangement:
    """Result of arranging parts on a build plate."""

    arranged_parts: list[dict[str, Any]] = field(default_factory=list)
    overflow_parts: list[str] = field(default_factory=list)
    plate_utilization: float = 0.0
    total_parts: int = 0
    fitted_parts: int = 0
    plate_width_mm: float = 256.0
    plate_depth_mm: float = 256.0
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "arranged_parts": self.arranged_parts,
            "overflow_parts": self.overflow_parts,
            "plate_utilization": round(self.plate_utilization, 4),
            "total_parts": self.total_parts,
            "fitted_parts": self.fitted_parts,
            "plate_width_mm": self.plate_width_mm,
            "plate_depth_mm": self.plate_depth_mm,
            "summary": self.summary,
        }


def arrange_on_plate(
    file_paths: list[str],
    *,
    plate_width_mm: float = 256.0,
    plate_depth_mm: float = 256.0,
    spacing_mm: float = 5.0,
    copies: dict[str, int] | None = None,
) -> PlateArrangement:
    """Pack multiple STL files onto a virtual build plate.

    Uses a greedy bottom-left bin-packing algorithm: parts are sorted by
    bounding-box area (largest first) and placed at the leftmost-bottommost
    position that fits within the plate dimensions.

    :param file_paths: Paths to STL files to arrange.
    :param plate_width_mm: Build plate width (X) in mm.
    :param plate_depth_mm: Build plate depth (Y) in mm.
    :param spacing_mm: Minimum gap between parts in mm.
    :param copies: Optional mapping of filename to copy count.
    :returns: :class:`PlateArrangement` with placement results.
    :raises ValueError: If any file path does not exist.
    """
    if not file_paths:
        return PlateArrangement(
            plate_width_mm=plate_width_mm,
            plate_depth_mm=plate_depth_mm,
            summary="No files provided.",
        )

    # Validate all paths exist
    for fp in file_paths:
        if not Path(fp).is_file():
            msg = f"File not found: {fp}"
            raise ValueError(msg)

    import trimesh  # lazy import — after validation

    # Build list of (path, width, depth, height) entries, expanding copies
    parts: list[tuple[str, float, float, float]] = []
    for fp in file_paths:
        mesh = trimesh.load(fp, force="mesh")
        extents = mesh.bounding_box.extents  # [x, y, z]
        w, d, h = float(extents[0]), float(extents[1]), float(extents[2])
        count = 1
        if copies:
            count = copies.get(Path(fp).name, copies.get(fp, 1))
        for _ in range(max(1, count)):
            parts.append((fp, w, d, h))

    # Sort by area descending (largest first for better packing)
    parts.sort(key=lambda p: p[1] * p[2], reverse=True)

    # Greedy bottom-left bin packing
    placed: list[dict[str, Any]] = []
    overflow: list[str] = []

    for path, pw, pd, ph in parts:
        best_x: float | None = None
        best_y: float | None = None

        # Scan grid positions (step by spacing for efficiency)
        step = max(1.0, spacing_mm)
        y = 0.0
        while y + pd <= plate_depth_mm:
            x = 0.0
            while x + pw <= plate_width_mm:
                # Check overlap with all placed parts
                fits = True
                for p in placed:
                    if (
                        x < p["x"] + p["width"] + spacing_mm
                        and x + pw + spacing_mm > p["x"]
                        and y < p["y"] + p["depth"] + spacing_mm
                        and y + pd + spacing_mm > p["y"]
                    ):
                        fits = False
                        break
                if fits:
                    best_x, best_y = x, y
                    break
                x += step
            if best_x is not None:
                break
            y += step

        if best_x is not None and best_y is not None:
            placed.append({
                "path": path,
                "x": round(best_x, 2),
                "y": round(best_y, 2),
                "width": round(pw, 2),
                "depth": round(pd, 2),
                "height": round(ph, 2),
            })
        else:
            overflow.append(path)

    plate_area = plate_width_mm * plate_depth_mm
    used_area = sum(p["width"] * p["depth"] for p in placed)
    utilization = used_area / plate_area if plate_area > 0 else 0.0

    summary = (
        f"Arranged {len(placed)}/{len(parts)} parts on "
        f"{plate_width_mm}×{plate_depth_mm}mm plate. "
        f"Utilization: {utilization:.1%}."
    )
    if overflow:
        summary += f" {len(overflow)} part(s) did not fit."

    return PlateArrangement(
        arranged_parts=placed,
        overflow_parts=overflow,
        plate_utilization=utilization,
        total_parts=len(parts),
        fitted_parts=len(placed),
        plate_width_mm=plate_width_mm,
        plate_depth_mm=plate_depth_mm,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Natural language → CSG composition plan
# ---------------------------------------------------------------------------

_SHAPE_KEYWORDS: dict[str, str] = {
    "box": "cube", "cube": "cube", "block": "cube", "rectangular": "cube",
    "square": "cube",
    "cylinder": "cylinder", "rod": "cylinder", "shaft": "cylinder",
    "pillar": "cylinder", "column": "cylinder",
    "tube": "pipe", "pipe": "pipe",
    "sphere": "sphere", "ball": "sphere", "dome": "sphere",
    "cone": "cone", "funnel": "cone", "taper": "cone", "pyramid": "cone",
    "torus": "torus", "donut": "torus", "ring": "torus",
    "wedge": "wedge", "ramp": "wedge", "slope": "wedge",
    "hexagon": "hex_prism", "hex": "hex_prism", "bolt": "hex_prism",
    "nut": "hex_prism",
    "text": "text", "label": "text", "letters": "text", "engraved": "text",
    "rounded": "rounded_cube", "fillet": "rounded_cube",
}

_OP_KEYWORDS: dict[str, str] = {
    "hole": "difference", "hollow": "difference", "cut": "difference",
    "subtract": "difference", "minus": "difference",
    "combine": "union", "join": "union", "merge": "union",
    "attach": "union", "and": "union", "with": "union",
    "overlap": "intersection", "common": "intersection",
    "intersect": "intersection",
}


@dataclass
class CompositionPlan:
    """A CSG composition plan generated from a text description."""

    description: str
    primitives: list[dict[str, Any]] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)
    estimated_dimensions_mm: dict[str, float] = field(default_factory=dict)
    complexity: str = "simple"
    notes: list[str] = field(default_factory=list)
    confidence: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "primitives": self.primitives,
            "operations": self.operations,
            "estimated_dimensions_mm": self.estimated_dimensions_mm,
            "complexity": self.complexity,
            "notes": self.notes,
            "confidence": self.confidence,
        }


def plan_composition_from_description(
    description: str,
    *,
    target_size_mm: float = 50.0,
    material: str = "PLA",
) -> CompositionPlan:
    """Parse a text description into a CSG primitive composition plan.

    Rule-based keyword parser — no LLM required. Scans the description
    for shape and operation keywords, then builds a CSG tree that can be
    passed directly to ``compose_part_from_primitives``.

    :param description: Natural language description (e.g. "a cube with a hole").
    :param target_size_mm: Target primary dimension in mm (default 50).
    :param material: Material hint (informational only).
    :returns: :class:`CompositionPlan` with primitives and operations.
    """
    import re

    tokens = re.findall(r"[a-z]+", description.lower())
    notes: list[str] = []

    # Detect shapes
    detected_shapes: list[str] = []
    seen_shapes: set[str] = set()
    for token in tokens:
        shape = _SHAPE_KEYWORDS.get(token)
        if shape and shape not in seen_shapes:
            detected_shapes.append(shape)
            seen_shapes.add(shape)

    # Detect operations — phrase-level patterns first (higher priority)
    detected_ops: list[str] = []
    seen_ops: set[str] = set()
    desc_lower = description.lower()

    if ("with hole" in desc_lower or "with a hole" in desc_lower) and "difference" not in seen_ops:
        detected_ops.append("difference")
        seen_ops.add("difference")
    if "hollow" in desc_lower and "difference" not in seen_ops:
        detected_ops.append("difference")
        seen_ops.add("difference")

    # Single-token operation keywords (lower priority)
    for token in tokens:
        op = _OP_KEYWORDS.get(token)
        if op and op not in seen_ops:
            detected_ops.append(op)
            seen_ops.add(op)

    # Default: if no shapes detected, use cube
    if not detected_shapes:
        detected_shapes = ["cube"]
        notes.append("No shape keywords detected; defaulting to cube.")

    # Build primitives
    s = target_size_mm
    primitives: list[dict[str, Any]] = []

    for i, shape in enumerate(detected_shapes):
        scale = 1.0 if i == 0 else 0.5
        dim = round(s * scale, 1)

        prim: dict[str, Any] = {"type": "primitive", "shape": shape, "params": {}}

        if shape == "cube":
            prim["params"] = {"size": [dim, dim, dim]}
        elif shape == "cylinder":
            prim["params"] = {"r": round(dim / 2, 1), "h": dim}
        elif shape == "sphere":
            prim["params"] = {"r": round(dim / 2, 1)}
        elif shape == "cone":
            prim["params"] = {"r1": round(dim / 2, 1), "r2": 1, "h": dim}
        elif shape == "torus":
            prim["params"] = {"major_r": round(dim / 2, 1), "minor_r": round(dim / 8, 1)}
        elif shape == "wedge":
            prim["params"] = {"width": dim, "depth": dim, "height": dim}
        elif shape == "hex_prism":
            prim["params"] = {"r": round(dim / 2, 1), "h": dim}
        elif shape == "text":
            # Try to extract text content from description
            text_content = "ABC"
            for token in tokens:
                if token not in _SHAPE_KEYWORDS and token not in _OP_KEYWORDS and len(token) > 2:
                    text_content = token.capitalize()
                    break
            prim["params"] = {"text": text_content, "size": round(dim / 4, 1), "depth": round(dim / 10, 1)}
        elif shape == "rounded_cube":
            prim["params"] = {"size": [dim, dim, dim], "radius": round(dim / 10, 1)}
        elif shape == "pipe":
            prim["params"] = {"h": dim, "outer_r": round(dim / 2, 1), "inner_r": round(dim / 3, 1)}

        # Position secondary shapes centered on primary
        if i > 0:
            prim["translate"] = [0, 0, round(s / 4, 1)]

        primitives.append(prim)

    # If "difference" detected, add a hole cylinder for it
    if "difference" in seen_ops and len(detected_shapes) == 1:
        hole_r = round(s * 0.15, 1)
        primitives.append({
            "type": "primitive",
            "shape": "cylinder",
            "params": {"r": hole_r, "h": round(s + 2, 1)},
            "translate": [0, 0, -1],
        })

    # Build operations
    operations: list[dict[str, Any]] = []
    if detected_ops:
        operations = [{"type": "operation", "op": detected_ops[0]}]
    elif len(primitives) > 1:
        operations = [{"type": "operation", "op": "union"}]

    # Classify complexity
    n = len(primitives)
    complexity = "simple" if n <= 1 else ("moderate" if n <= 3 else "complex")

    # Confidence based on keyword match rate
    total_tokens = max(len(tokens), 1)
    matched = sum(1 for t in tokens if t in _SHAPE_KEYWORDS or t in _OP_KEYWORDS)
    ratio = matched / total_tokens
    confidence = "high" if ratio > 0.3 else ("medium" if ratio > 0.1 else "low")

    notes.append(f"Material hint: {material} (informational).")

    primary_dim = s
    return CompositionPlan(
        description=description,
        primitives=primitives,
        operations=operations,
        estimated_dimensions_mm={
            "width": primary_dim,
            "depth": primary_dim,
            "height": primary_dim,
        },
        complexity=complexity,
        notes=notes,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Loop 14: Template search by description
# ---------------------------------------------------------------------------

@dataclass
class TemplateSearchResult:
    """Result of a fuzzy template search."""

    query: str = ""
    matches: list[dict[str, Any]] = field(default_factory=list)
    total_templates: int = 0
    search_method: str = "keyword"

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "matches": list(self.matches),
            "total_templates": self.total_templates,
            "search_method": self.search_method,
        }


def search_templates(
    query: str,
    *,
    max_results: int = 10,
    category_filter: str = "",
) -> TemplateSearchResult:
    """Search templates by natural-language description.

    Uses token-overlap scoring: each query token is checked against the
    template's ID, description, category, and tags.  Templates are ranked
    by match ratio and returned in descending score order.

    :param query: Natural-language search string.
    :param max_results: Cap on returned matches (default 10).
    :param category_filter: If set, only return templates in this category.
    :returns: ``TemplateSearchResult`` with scored matches.
    """
    import json as _json

    if not query or not query.strip():
        return TemplateSearchResult(query=query, total_templates=0)

    templates_path = Path(__file__).parent / "data" / "design_templates.json"
    if not templates_path.exists():
        return TemplateSearchResult(query=query, total_templates=0)

    with open(templates_path) as fh:
        data: dict[str, Any] = _json.load(fh)

    # Normalised query tokens
    q_tokens = query.lower().split()
    if not q_tokens:
        return TemplateSearchResult(query=query, total_templates=0)

    scored: list[tuple[float, str, dict[str, Any]]] = []
    template_keys = [k for k in data if not k.startswith("_")]

    for tid in template_keys:
        tmpl = data[tid]
        cat = tmpl.get("category", "")
        if category_filter and cat != category_filter:
            continue

        # Build searchable text from template metadata
        desc = tmpl.get("description", "")
        tags = tmpl.get("tags", [])
        search_text = " ".join([
            tid.replace("_", " "),
            desc,
            cat.replace("_", " "),
            " ".join(tags) if isinstance(tags, list) else "",
        ]).lower()

        # Score = fraction of query tokens found in search text
        hits = sum(1 for t in q_tokens if t in search_text)
        score = hits / len(q_tokens)

        if score > 0:
            scored.append((score, tid, {
                "template_id": tid,
                "score": round(score, 3),
                "description": desc,
                "category": cat,
            }))

    # Sort by score descending, then alphabetically for ties
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:max_results]

    return TemplateSearchResult(
        query=query,
        matches=[entry[2] for entry in top],
        total_templates=len(template_keys),
        search_method="keyword",
    )


# ---------------------------------------------------------------------------
# Loop 15: Mesh weight estimation
# ---------------------------------------------------------------------------

# Material densities in g/cm³
_MATERIAL_DENSITIES: dict[str, float] = {
    "pla": 1.24,
    "abs": 1.04,
    "petg": 1.27,
    "tpu": 1.21,
    "nylon": 1.14,
    "asa": 1.07,
    "pc": 1.20,
    "pva": 1.23,
    "hips": 1.05,
    "wood": 1.15,
    "carbon_fiber": 1.30,
    "resin": 1.10,
}


@dataclass
class WeightEstimate:
    """Estimated weight of a 3D-printed part."""

    file_path: str = ""
    volume_mm3: float = 0.0
    volume_cm3: float = 0.0
    material: str = "pla"
    density_g_cm3: float = 1.24
    infill_percent: float = 20.0
    wall_thickness_mm: float = 1.2
    solid_weight_g: float = 0.0
    estimated_weight_g: float = 0.0
    bounding_box_mm: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "volume_mm3": round(self.volume_mm3, 2),
            "volume_cm3": round(self.volume_cm3, 4),
            "material": self.material,
            "density_g_cm3": self.density_g_cm3,
            "infill_percent": self.infill_percent,
            "wall_thickness_mm": self.wall_thickness_mm,
            "solid_weight_g": round(self.solid_weight_g, 2),
            "estimated_weight_g": round(self.estimated_weight_g, 2),
            "bounding_box_mm": self.bounding_box_mm,
            "notes": list(self.notes),
        }


def _signed_triangle_volume(
    v0: tuple[float, ...],
    v1: tuple[float, ...],
    v2: tuple[float, ...],
) -> float:
    """Signed volume of the tetrahedron formed by a triangle and the origin."""
    return (
        v0[0] * (v1[1] * v2[2] - v2[1] * v1[2])
        - v1[0] * (v0[1] * v2[2] - v2[1] * v0[2])
        + v2[0] * (v0[1] * v1[2] - v1[1] * v0[2])
    ) / 6.0


def estimate_weight(
    file_path: str,
    *,
    material: str = "pla",
    infill_percent: float = 20.0,
    wall_thickness_mm: float = 1.2,
) -> WeightEstimate:
    """Estimate the printed weight of an STL file.

    Uses the divergence theorem to calculate mesh volume from triangles,
    then applies material density and infill ratio to estimate weight.

    :param file_path: Path to an STL file.
    :param material: Material name (must be in ``_MATERIAL_DENSITIES``).
    :param infill_percent: Infill percentage (0-100).
    :param wall_thickness_mm: Perimeter wall thickness in mm.
    :returns: ``WeightEstimate`` dataclass.
    """
    fp = Path(file_path)
    if not fp.exists():
        raise FileNotFoundError(f"STL file not found: {file_path}")

    mat_key = material.lower().strip()
    density = _MATERIAL_DENSITIES.get(mat_key, 1.24)
    notes: list[str] = []
    if mat_key not in _MATERIAL_DENSITIES:
        notes.append(f"Unknown material '{material}', using PLA density (1.24 g/cm³).")

    # Parse triangles
    triangles, _verts = _parse_stl_for_analysis(file_path)
    if not triangles:
        raise ValueError(f"Could not parse triangles from {file_path}")

    # Compute signed volume via divergence theorem
    total_volume = 0.0
    for tri in triangles:
        total_volume += _signed_triangle_volume(tri[0], tri[1], tri[2])
    volume_mm3 = abs(total_volume)
    volume_cm3 = volume_mm3 / 1000.0

    # Bounding box
    bbox = _bounding_box(_verts)
    bbox_dims = {
        "width": round(bbox["max_x"] - bbox["min_x"], 2),
        "depth": round(bbox["max_y"] - bbox["min_y"], 2),
        "height": round(bbox["max_z"] - bbox["min_z"], 2),
    }

    # Weight estimation model:
    # Outer shell is solid, interior uses infill ratio
    # Approximate shell fraction based on wall thickness vs bounding box dims
    min_dim = min(bbox_dims["width"], bbox_dims["depth"], bbox_dims["height"])
    if min_dim > 0:
        shell_fraction = min(1.0, (2 * wall_thickness_mm) / min_dim)
    else:
        shell_fraction = 1.0

    infill_ratio = max(0.0, min(100.0, infill_percent)) / 100.0
    effective_fill = shell_fraction + (1 - shell_fraction) * infill_ratio
    effective_fill = min(1.0, effective_fill)

    solid_weight = volume_cm3 * density
    estimated_weight = solid_weight * effective_fill

    notes.append(
        f"Shell fraction: {shell_fraction:.1%}, "
        f"effective fill: {effective_fill:.1%}."
    )

    return WeightEstimate(
        file_path=file_path,
        volume_mm3=volume_mm3,
        volume_cm3=volume_cm3,
        material=mat_key,
        density_g_cm3=density,
        infill_percent=infill_percent,
        wall_thickness_mm=wall_thickness_mm,
        solid_weight_g=solid_weight,
        estimated_weight_g=estimated_weight,
        bounding_box_mm=bbox_dims,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Loop 16: Design-to-GCode end-to-end pipeline
# ---------------------------------------------------------------------------

@dataclass
class DesignToGCodeResult:
    """Result of the full design-to-gcode pipeline."""

    description: str = ""
    template_id: str = ""
    scad_file: str = ""
    stl_file: str = ""
    gcode_file: str = ""
    steps_completed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    weight_estimate_g: float = 0.0
    structural_risks: int = 0
    success: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "template_id": self.template_id,
            "scad_file": self.scad_file,
            "stl_file": self.stl_file,
            "gcode_file": self.gcode_file,
            "steps_completed": list(self.steps_completed),
            "errors": list(self.errors),
            "weight_estimate_g": round(self.weight_estimate_g, 2),
            "structural_risks": self.structural_risks,
            "success": self.success,
        }


def design_to_gcode(
    description: str,
    *,
    output_dir: str = "",
    material: str = "PLA",
    printer_model: str = "",
    infill_percent: float = 20.0,
) -> DesignToGCodeResult:
    """End-to-end pipeline: description → template → STL → structural check → GCode.

    Steps:
        1. Search templates for best match
        2. Generate STL via OpenSCAD
        3. Run structural risk analysis
        4. Estimate weight
        5. Slice to G-code (if slicer available)

    :param description: Natural-language design description.
    :param output_dir: Directory for output files (uses tempdir if empty).
    :param material: Material for weight estimation and slicing.
    :param printer_model: Printer model for slicer profile lookup.
    :param infill_percent: Infill percentage for weight estimation.
    :returns: ``DesignToGCodeResult`` with paths and metadata.
    """
    import json as _json
    import tempfile

    result = DesignToGCodeResult(description=description)

    if not description or not description.strip():
        result.errors.append("Empty description provided.")
        return result

    out_dir = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix="kiln_d2g_"))
    out_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Find best matching template
    search = search_templates(description, max_results=1)
    if not search.matches:
        result.errors.append("No matching template found for description.")
        result.steps_completed.append("template_search (no match)")
        return result

    template_id = search.matches[0]["template_id"]
    result.template_id = template_id
    result.steps_completed.append("template_search")

    # Step 2: Load template and generate SCAD → STL
    templates_path = Path(__file__).parent / "data" / "design_templates.json"
    with open(templates_path) as fh:
        templates = _json.load(fh)

    tmpl = templates.get(template_id)
    if not tmpl or "scad_template" not in tmpl:
        result.errors.append(f"Template '{template_id}' has no scad_template.")
        return result

    # Write SCAD file
    scad_path = out_dir / f"{template_id}.scad"
    scad_code = tmpl["scad_template"]
    # Apply default params
    params = tmpl.get("default_params", {})
    for k, v in params.items():
        if isinstance(v, (int, float)):
            scad_code = f"{k} = {v};\n" + scad_code
    scad_path.write_text(scad_code)
    result.scad_file = str(scad_path)
    result.steps_completed.append("scad_generation")

    # Try to render STL via OpenSCAD
    stl_path = out_dir / f"{template_id}.stl"
    try:
        from kiln.generation.openscad import OpenSCADProvider

        provider = OpenSCADProvider()
        provider.render(str(scad_path), str(stl_path))
        result.stl_file = str(stl_path)
        result.steps_completed.append("stl_rendering")
    except Exception as exc:
        result.errors.append(f"STL rendering failed: {exc}")
        return result

    # Step 3: Structural risk analysis
    try:
        risks = analyze_structural_risks(str(stl_path))
        result.structural_risks = len(risks)
        result.steps_completed.append("structural_analysis")
    except Exception as exc:
        result.errors.append(f"Structural analysis failed: {exc}")
        # Non-fatal — continue

    # Step 4: Weight estimation
    try:
        weight = estimate_weight(
            str(stl_path),
            material=material,
            infill_percent=infill_percent,
        )
        result.weight_estimate_g = weight.estimated_weight_g
        result.steps_completed.append("weight_estimation")
    except Exception as exc:
        result.errors.append(f"Weight estimation failed: {exc}")

    # Step 5: Slice to G-code
    gcode_path = out_dir / f"{template_id}.gcode"
    try:
        from kiln.slicer import slice_stl

        slice_stl(
            str(stl_path),
            str(gcode_path),
            printer_model=printer_model,
        )
        result.gcode_file = str(gcode_path)
        result.steps_completed.append("slicing")
    except ImportError:
        result.errors.append("Slicer module not available.")
    except Exception as exc:
        result.errors.append(f"Slicing failed: {exc}")

    result.success = bool(result.stl_file)
    return result


# ---------------------------------------------------------------------------
# Loop 17: STL merge / assembly
# ---------------------------------------------------------------------------

@dataclass
class MergedMeshResult:
    """Result of merging multiple STL files."""

    output_path: str = ""
    input_files: list[str] = field(default_factory=list)
    total_triangles: int = 0
    bounding_box_mm: dict[str, float] = field(default_factory=dict)
    success: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": self.output_path,
            "input_files": list(self.input_files),
            "total_triangles": self.total_triangles,
            "bounding_box_mm": self.bounding_box_mm,
            "success": self.success,
            "errors": list(self.errors),
        }


def merge_stl_files(
    file_paths: list[str],
    output_path: str,
    *,
    positions: list[dict[str, float]] | None = None,
) -> MergedMeshResult:
    """Merge multiple STL files into a single mesh.

    Optionally repositions each part via translate offsets before merging.

    :param file_paths: List of STL file paths to merge.
    :param output_path: Where to write the combined STL.
    :param positions: Optional list of ``{"x": ..., "y": ..., "z": ...}``
        translate offsets for each file. Must match length of *file_paths*.
    :returns: ``MergedMeshResult`` with combined mesh info.
    """
    result = MergedMeshResult(input_files=list(file_paths))

    if not file_paths:
        result.errors.append("No input files provided.")
        return result

    if positions and len(positions) != len(file_paths):
        result.errors.append(
            f"positions length ({len(positions)}) must match "
            f"file_paths length ({len(file_paths)})."
        )
        return result

    # Validate all files exist
    for fp in file_paths:
        if not Path(fp).exists():
            result.errors.append(f"File not found: {fp}")
    if result.errors:
        return result

    # Collect all triangles with optional translation
    all_triangles: list[tuple[tuple[float, ...], ...]] = []
    for idx, fp in enumerate(file_paths):
        tris, _verts = _parse_stl_for_analysis(fp)
        if not tris:
            result.errors.append(f"Could not parse triangles from {fp}")
            continue

        if positions:
            offset = positions[idx]
            dx = offset.get("x", 0.0)
            dy = offset.get("y", 0.0)
            dz = offset.get("z", 0.0)
            translated: list[tuple[tuple[float, ...], ...]] = []
            for tri in tris:
                new_tri = tuple(
                    (v[0] + dx, v[1] + dy, v[2] + dz) for v in tri
                )
                translated.append(new_tri)
            all_triangles.extend(translated)
        else:
            all_triangles.extend(tris)

    if not all_triangles:
        result.errors.append("No triangles parsed from any input file.")
        return result

    # Write merged binary STL
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with open(out, "wb") as fh:
        # 80-byte header
        fh.write(b"Kiln merged STL" + b"\x00" * 65)
        # Triangle count (uint32)
        fh.write(struct.pack("<I", len(all_triangles)))
        for tri in all_triangles:
            # Compute normal
            n = _triangle_normal(tri)
            fh.write(struct.pack("<3f", *n))
            for v in tri:
                fh.write(struct.pack("<3f", *v[:3]))
            # Attribute byte count
            fh.write(struct.pack("<H", 0))

    all_verts = [v for tri in all_triangles for v in tri]
    bbox = _bounding_box(all_verts)
    result.output_path = str(out)
    result.total_triangles = len(all_triangles)
    result.bounding_box_mm = {
        "width": round(bbox["max_x"] - bbox["min_x"], 2),
        "depth": round(bbox["max_y"] - bbox["min_y"], 2),
        "height": round(bbox["max_z"] - bbox["min_z"], 2),
    }
    result.success = True
    return result


# ---------------------------------------------------------------------------
# Loop 18: Cross-section / cutaway view
# ---------------------------------------------------------------------------

@dataclass
class CrossSectionResult:
    """Result of slicing a mesh at a plane to reveal internal structure."""

    file_path: str = ""
    plane: str = "z"
    plane_offset_mm: float = 0.0
    contour_count: int = 0
    contour_points: list[list[tuple[float, float]]] = field(default_factory=list)
    bounding_box_mm: dict[str, float] = field(default_factory=dict)
    cross_section_area_mm2: float = 0.0
    success: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "plane": self.plane,
            "plane_offset_mm": round(self.plane_offset_mm, 3),
            "contour_count": self.contour_count,
            "contour_points": [
                [(round(x, 3), round(y, 3)) for x, y in contour]
                for contour in self.contour_points
            ],
            "bounding_box_mm": self.bounding_box_mm,
            "cross_section_area_mm2": round(self.cross_section_area_mm2, 3),
            "success": self.success,
        }


def cross_section_at_plane(
    file_path: str,
    *,
    plane: str = "z",
    offset_ratio: float = 0.5,
    offset_mm: float | None = None,
) -> CrossSectionResult:
    """Compute the 2D cross-section of a mesh at a given plane.

    Slices the mesh perpendicular to the chosen axis and returns the
    contour polygons and cross-sectional area.

    :param file_path: Path to STL file.
    :param plane: Axis perpendicular to the cut plane — "x", "y", or "z".
    :param offset_ratio: Fractional position along the axis (0.0=min, 1.0=max).
        Ignored if *offset_mm* is provided.
    :param offset_mm: Absolute position along the axis in mm.
    :returns: ``CrossSectionResult`` with contour data.
    """
    fp = Path(file_path)
    if not fp.exists():
        raise FileNotFoundError(f"STL file not found: {file_path}")

    plane = plane.lower().strip()
    if plane not in ("x", "y", "z"):
        raise ValueError(f"plane must be 'x', 'y', or 'z', got '{plane}'")

    triangles, _verts = _parse_stl_for_analysis(file_path)
    if not triangles:
        raise ValueError(f"Could not parse triangles from {file_path}")

    bbox = _bounding_box(_verts)

    # Determine axis index and compute cut position
    axis_idx = {"x": 0, "y": 1, "z": 2}[plane]
    axis_keys = {0: ("min_x", "max_x"), 1: ("min_y", "max_y"), 2: ("min_z", "max_z")}
    axis_min = bbox[axis_keys[axis_idx][0]]
    axis_max = bbox[axis_keys[axis_idx][1]]

    if offset_mm is not None:
        cut_pos = offset_mm
    else:
        ratio = max(0.0, min(1.0, offset_ratio))
        cut_pos = axis_min + ratio * (axis_max - axis_min)

    # Collect intersection segments
    # For each triangle, find the intersection with the cut plane
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    # Map 3D coords to 2D by dropping the cut axis
    keep_axes = [i for i in range(3) if i != axis_idx]

    for tri in triangles:
        pts_on_plane: list[tuple[float, float]] = []
        for i in range(3):
            v0 = tri[i]
            v1 = tri[(i + 1) % 3]
            a_val = v0[axis_idx]
            b_val = v1[axis_idx]
            # Check if edge crosses the cut plane
            if (a_val <= cut_pos <= b_val) or (b_val <= cut_pos <= a_val):
                denom = b_val - a_val
                if abs(denom) < 1e-12:
                    # Edge lies on the plane
                    pts_on_plane.append(
                        (v0[keep_axes[0]], v0[keep_axes[1]])
                    )
                else:
                    t = (cut_pos - a_val) / denom
                    ix = v0[keep_axes[0]] + t * (v1[keep_axes[0]] - v0[keep_axes[0]])
                    iy = v0[keep_axes[1]] + t * (v1[keep_axes[1]] - v0[keep_axes[1]])
                    pts_on_plane.append((ix, iy))

        if len(pts_on_plane) >= 2:
            segments.append((pts_on_plane[0], pts_on_plane[1]))

    # Build contours from segments by chaining endpoints
    contours = _chain_segments(segments)

    # Compute area of each contour using the shoelace formula
    total_area = 0.0
    for contour in contours:
        total_area += abs(_shoelace_area(contour))

    bbox_dims = {
        "width": round(bbox["max_x"] - bbox["min_x"], 2),
        "depth": round(bbox["max_y"] - bbox["min_y"], 2),
        "height": round(bbox["max_z"] - bbox["min_z"], 2),
    }

    return CrossSectionResult(
        file_path=file_path,
        plane=plane,
        plane_offset_mm=cut_pos,
        contour_count=len(contours),
        contour_points=contours,
        bounding_box_mm=bbox_dims,
        cross_section_area_mm2=total_area,
        success=len(contours) > 0,
    )


def _chain_segments(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    """Chain line segments into contour polylines.

    Greedy nearest-endpoint chaining with tolerance for floating-point gaps.
    """
    if not segments:
        return []

    eps = 1e-4
    remaining = list(segments)
    contours: list[list[tuple[float, float]]] = []

    while remaining:
        seg = remaining.pop(0)
        chain = [seg[0], seg[1]]

        changed = True
        while changed:
            changed = False
            for i, s in enumerate(remaining):
                # Try to attach to the end of the chain
                if _pt_dist(chain[-1], s[0]) < eps:
                    chain.append(s[1])
                    remaining.pop(i)
                    changed = True
                    break
                if _pt_dist(chain[-1], s[1]) < eps:
                    chain.append(s[0])
                    remaining.pop(i)
                    changed = True
                    break
                # Try to attach to the start
                if _pt_dist(chain[0], s[1]) < eps:
                    chain.insert(0, s[0])
                    remaining.pop(i)
                    changed = True
                    break
                if _pt_dist(chain[0], s[0]) < eps:
                    chain.insert(0, s[1])
                    remaining.pop(i)
                    changed = True
                    break

        if len(chain) >= 3:
            contours.append(chain)

    return contours


def _pt_dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Euclidean distance between two 2D points."""
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


def _shoelace_area(points: list[tuple[float, float]]) -> float:
    """Signed area of a polygon via the shoelace formula."""
    n = len(points)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += points[i][0] * points[j][1]
        area -= points[j][0] * points[i][1]
    return area / 2.0


# ---------------------------------------------------------------------------
# Loop 19: Parametric constraint solver
# ---------------------------------------------------------------------------

@dataclass
class ConstraintSolution:
    """Result of solving parametric constraints."""

    template_id: str = ""
    solved_params: dict[str, float] = field(default_factory=dict)
    constraints_satisfied: list[str] = field(default_factory=list)
    constraints_violated: list[str] = field(default_factory=list)
    iterations: int = 0
    success: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "solved_params": dict(self.solved_params),
            "constraints_satisfied": list(self.constraints_satisfied),
            "constraints_violated": list(self.constraints_violated),
            "iterations": self.iterations,
            "success": self.success,
            "notes": list(self.notes),
        }


def solve_constraints(
    template_id: str,
    constraints: dict[str, Any],
) -> ConstraintSolution:
    """Solve parametric constraints to find valid template parameters.

    Given a template ID and constraints like ``{"width": {"min": 10, "max": 50},
    "height": {"equals": 20}}``, finds parameter values that satisfy all
    constraints while staying within the template's declared ranges.

    :param template_id: Template identifier in design_templates.json.
    :param constraints: Dict mapping param names to constraint dicts.
        Supported constraint keys: ``min``, ``max``, ``equals``, ``ratio``
        (``ratio`` specifies a ratio to another param, e.g. ``{"ratio": ["width", 0.5]}``).
    :returns: ``ConstraintSolution`` with solved parameters.
    """
    import json as _json

    result = ConstraintSolution(template_id=template_id)

    templates_path = Path(__file__).parent / "data" / "design_templates.json"
    if not templates_path.exists():
        result.notes.append("Templates file not found.")
        return result

    with open(templates_path) as fh:
        data = _json.load(fh)

    if template_id not in data or template_id.startswith("_"):
        result.notes.append(f"Unknown template: {template_id}")
        return result

    tmpl = data[template_id]
    raw_params = tmpl.get("parameters", {})

    if not raw_params:
        result.notes.append("Template has no parameters.")
        return result

    # Build flat param dict from template's parameter definitions
    solved: dict[str, Any] = {}
    range_bounds: dict[str, dict[str, float]] = {}
    for pname, pdef in raw_params.items():
        if isinstance(pdef, dict):
            solved[pname] = pdef.get("default", 0)
            range_bounds[pname] = {
                "min": pdef.get("min", float("-inf")),
                "max": pdef.get("max", float("inf")),
            }
        else:
            solved[pname] = pdef

    # Apply constraints iteratively
    max_iter = 10
    for iteration in range(max_iter):
        result.iterations = iteration + 1
        all_satisfied = True

        for param_name, constraint in constraints.items():
            if param_name not in solved:
                result.constraints_violated.append(
                    f"Unknown parameter: {param_name}"
                )
                continue

            val = solved[param_name]
            if not isinstance(val, (int, float)):
                continue

            # Get range bounds
            p_range = range_bounds.get(param_name, {})
            range_min = p_range.get("min", float("-inf"))
            range_max = p_range.get("max", float("inf"))

            # Apply "equals" constraint
            if "equals" in constraint:
                target = float(constraint["equals"])
                target = max(range_min, min(range_max, target))
                solved[param_name] = target
                if abs(val - target) > 0.01:
                    all_satisfied = False
                continue

            # Apply "min" constraint
            if "min" in constraint:
                c_min = float(constraint["min"])
                if val < c_min:
                    solved[param_name] = max(range_min, c_min)
                    all_satisfied = False

            # Apply "max" constraint
            if "max" in constraint:
                c_max = float(constraint["max"])
                if val > c_max:
                    solved[param_name] = min(range_max, c_max)
                    all_satisfied = False

            # Apply "ratio" constraint: [other_param, ratio_value]
            if "ratio" in constraint:
                ratio_spec = constraint["ratio"]
                if isinstance(ratio_spec, list) and len(ratio_spec) == 2:
                    other_param, ratio_val = ratio_spec[0], float(ratio_spec[1])
                    if other_param in solved:
                        other_val = solved[other_param]
                        if isinstance(other_val, (int, float)):
                            target = other_val * ratio_val
                            target = max(range_min, min(range_max, target))
                            if abs(val - target) > 0.01:
                                solved[param_name] = target
                                all_satisfied = False

            # Clamp to range
            val = solved[param_name]
            if isinstance(val, (int, float)):
                solved[param_name] = max(range_min, min(range_max, float(val)))

        if all_satisfied:
            break

    # Validate final solution
    satisfied: list[str] = []
    violated: list[str] = []
    for param_name, constraint in constraints.items():
        if param_name not in solved:
            violated.append(f"{param_name}: not in template")
            continue

        val = solved[param_name]
        if not isinstance(val, (int, float)):
            continue

        ok = True
        if "equals" in constraint and abs(val - float(constraint["equals"])) > 0.01:
            ok = False
        if "min" in constraint and val < float(constraint["min"]) - 0.01:
            ok = False
        if "max" in constraint and val > float(constraint["max"]) + 0.01:
            ok = False

        if ok:
            satisfied.append(f"{param_name}={val}")
        else:
            violated.append(f"{param_name}={val} (constraint: {constraint})")

    result.solved_params = {
        k: round(v, 3) if isinstance(v, float) else v
        for k, v in solved.items()
    }
    result.constraints_satisfied = satisfied
    result.constraints_violated = violated
    result.success = len(violated) == 0

    return result
