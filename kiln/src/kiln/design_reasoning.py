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
    describe_structural_risks  — what a person is told about the risks found
    apply_reinforcements       — auto-apply fixes (thicken, fillet, base plate)
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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiln.cost_estimator import BUILTIN_MATERIALS, DEFAULT_MATERIAL, resolve_material
from kiln.mesh_errors import UnreadableMeshError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — DEMOTE-NOT-DELETE overlay seam
# ---------------------------------------------------------------------------
#
# Detection always uses these public (stricter) values, so EVERY caller —
# overlay or not — surfaces the same features for a given mesh; a Pro caller
# is never silently denied a warning a free caller would get.  The kiln-pro
# ``structural_thresholds`` overlay (loaded by ``analyze_structural_risks``)
# supplies calibrated (looser) values that act as an ESCALATION line: a
# feature past the calibrated threshold stays a full warning, while one that
# only trips the stricter public floor is DEMOTED to an ``info`` note —
# quieter for the experienced user, nothing hidden.
#
# Stricter direction per metric (the public floor that surfaces a feature):
#   - thin_neck_ratio:           HIGHER value flags MORE thin sections
#   - tipping_ratio:             LOWER value flags MORE tall, narrow parts
#   - min_cross_section_mm2:     HIGHER value flags MORE small sections
#
# The kiln-pro overlay supplies calibrated values for those three; the
# public defaults below are the conservative floor.  The sharp-corner
# threshold is NOT a tier dial: one value, every caller (see
# ``_SHARP_ANGLE_THRESHOLD_DEG_PUBLIC``).  The tools leave every threshold
# unset so the engine resolves it; a caller who passes one pins that dial.

# Stress concentration threshold: cross-section area ratio that signals a
# "thin neck" where load transfer becomes risky.
_THIN_NECK_RATIO_PUBLIC = 0.35

# Height-to-base ratio past which a part may tip over on the plate.
_TIPPING_RATIO_PUBLIC = 4.0

# Minimum cross-section area (mm²) below which any section is flagged.
_MIN_CROSS_SECTION_MM2_PUBLIC = 5.0

# How far the surface turns inward across an inside edge (the angle between
# its two faces' normals, in degrees) before the corner counts as sharp.  At 65
# a corner counts when the material wraps 245 degrees or more around the edge:
# a square inside corner (270) counts, a pentagonal pocket's corners (252)
# count, a hexagonal pocket's corners (240, the nut trap) do not, and an
# inside fillet faceted in steps of 45 degrees or less does not.  One value at
# every tier and every door; a caller may pin another.  Only inside
# (re-entrant) edges are judged; see :func:`_find_sharp_corners`.  Pinned by
# ``tests/test_one_sharp_corner_dial.py``.
_SHARP_ANGLE_THRESHOLD_DEG_PUBLIC = 65.0

# Sharp inside edges that meet, or pass close by each other, are one corner:
# one place to round, one finding.  "Close" is read on a grid of cells this
# wide (:func:`_corners`): always under 1.25 mm, never over about 9.
_CORNER_CELL_MM = 2.5

# An inside corner whose edges total less than this (mm) is not counted: a
# printed corner is never sharper than the bead that forms it, so a crease
# this short is not in the print.  It is the width of a common line, and the
# creases two rounds leave where they meet are well under it.  Pinned by
# ``tests/test_tiny_creases_are_not_sharp_corners.py``.
_CORNER_FLOOR_MM = 0.4

# The most points the grouping lays along a part's inside edges, a point every
# 1.25 mm: over a kilometre of inside edge.  A file asking for more (one edge
# kilometres long, say) is grouped by its edges' ends alone, so the work is
# bounded by the file's size, never by the distances written in it.
_CORNER_POINT_BUDGET = 1_000_000

# A corner further than this from the origin (1,000 km) is not part of a
# printable part; a face reaching it is left out of the corner check, as a
# face with no position (NaN or infinite) is.
_FARTHEST_MM = 1e9

# Number of Z-slices for cross-section analysis (implementation detail).
_NUM_SLICES = 40

# Overhang angle (from vertical) where layer adhesion weakens structurally.
# 45° is textbook FDM; not tier-gated.
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
    estimated_strength_gain: str  # what the change does for the part, in words; no figure Kiln cannot source
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
    recommended_print_orientation: str  # "upright", "on_side", "on_back"; "" when not judged
    orientation_reasoning: str
    #: The units question the file's size raises; when set, nothing above
    #: was judged.
    units_question: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_load_axis": self.primary_load_axis,
            "load_surfaces": self.load_surfaces,
            "weak_axis": self.weak_axis,
            "layer_direction_concern": self.layer_direction_concern,
            "recommended_print_orientation": self.recommended_print_orientation,
            "orientation_reasoning": self.orientation_reasoning,
            **({"units_question": self.units_question} if self.units_question else {}),
        }


@dataclass
class DesignImprovementPlan:
    """Complete improvement plan for a design."""

    file_path: str
    risks: list[StructuralRisk] = field(default_factory=list)
    reinforcements: list[ReinforcementRecommendation] = field(default_factory=list)
    load_analysis: LoadAnalysis | None = None
    #: 0-100; the score, grade and counts are None when nothing was judged.
    overall_structural_score: int | None = 0
    structural_grade: str = ""  # A-F
    critical_count: int | None = 0
    warning_count: int | None = 0
    summary: str = ""
    #: The units question the file's size raises.  When set it is the
    #: summary, and no risk was judged: none is counted, scored or listed.
    units_question: str = ""

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
            **({"units_question": self.units_question} if self.units_question else {}),
        }

    @classmethod
    def not_judged(cls, file_path: str, units_question: str) -> DesignImprovementPlan:
        """The plan for a file whose size puts its units in question: that
        question, and nothing counted, scored or recommended."""
        return cls(
            file_path=file_path, overall_structural_score=None, critical_count=None,
            warning_count=None, summary=units_question, units_question=units_question,
        )


# ---------------------------------------------------------------------------
# Internal geometry helpers
# ---------------------------------------------------------------------------


def _parse_stl_for_analysis(
    file_path: str,
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]]]:
    """Parse STL and return (triangles, unique_vertices).

    Each triangle is ``((v0x,v0y,v0z), (v1x,v1y,v1z), (v2x,v2y,v2z))``.
    Vertices is a deduplicated list of all vertex positions.

    Delegates to the canonical STL parser in
    :mod:`kiln.generation.validation`.
    """
    from kiln.generation.validation import _parse_stl

    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"File not found: {file_path}")

    errors: list[str] = []
    triangles, vertices = _parse_stl(path, errors)
    if errors:
        return [], []
    return triangles, vertices


#: What the structural analysis adds to the units question: it judges nothing
#: until the size is settled, because every risk it finds is measured against
#: a part's real size.
_NOT_JUDGED = (
    "  Until its size is settled, the structural analysis judges nothing: a risk "
    "found at this size would describe a part nobody meant."
)


@dataclass(frozen=True)
class _Part:
    """A mesh as the structural analysis reads it, and what its size says.

    ``units_question`` is the units question the file's size raises, or
    ``""``: asked when the file reads smaller than anything a printer makes,
    in the words of the one judgement every door uses
    (:func:`kiln.generation.validation.unit_verdict`).  Nothing is rescaled.
    """

    triangles: list[tuple[tuple[float, ...], ...]]
    vertices: list[tuple[float, ...]]
    bbox: dict[str, float]
    units_question: str = ""


def _read_part(file_path: str) -> _Part:
    """Read *file_path* for the structural analysis: the one entry every
    structural door's reading passes through, so each reads a file's units
    the way every other mesh door does.

    A file whose size puts its units in question is answered with that
    question and no risk: the risks are judged against sizes a printer
    makes, so at a thousandth of the part's size each one describes a part
    nobody meant (the same scene exported in metres and in millimetres gave
    different risks).
    """
    from kiln.generation.validation import unit_verdict

    triangles, vertices = _parse_stl_for_analysis(file_path)
    bbox = _bounding_box(vertices)
    if not triangles:
        return _Part(triangles, vertices, bbox)
    largest = max(bbox["max_x"] - bbox["min_x"], bbox["max_y"] - bbox["min_y"], bbox["max_z"] - bbox["min_z"])
    verdict = unit_verdict(float(largest))
    question = verdict.describe_unchanged() + _NOT_JUDGED if verdict.below_printable else ""
    return _Part(triangles, vertices, bbox, question)


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
    min_area_mm2: float = _MIN_CROSS_SECTION_MM2_PUBLIC,
    ratio_threshold: float = _THIN_NECK_RATIO_PUBLIC,
) -> list[StructuralRisk]:
    """Find Z-heights where cross-section suddenly narrows (thin necks).

    ``ratio_threshold`` controls the stress-concentration check: a
    section is flagged when ``area / neighbor_avg < ratio_threshold``.
    Higher threshold → more thin sections flagged (public-stricter)."""
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

    # Flag each slice that is too thin, or much thinner than the slices beside
    # it.  Neighbouring slices flagged for the same reason are one neck: it is
    # reported once, at its narrowest slice, where that slice is.
    flagged: list[tuple[int, str, float]] = []  # (slice, kind, what it was flagged on)
    for i in range(1, len(areas) - 1):
        area = areas[i][1]
        neighbor_avg = (areas[i - 1][1] + areas[i + 1][1]) / 2.0
        if area < min_area_mm2:
            flagged.append((i, "thin_neck", area))
        elif neighbor_avg > 0 and area / neighbor_avg < ratio_threshold:
            flagged.append((i, "stress_concentration", area / neighbor_avg))
    runs: list[list[tuple[int, str, float]]] = []
    for item in flagged:
        if runs and runs[-1][-1][1] == item[1] and runs[-1][-1][0] == item[0] - 1:
            runs[-1].append(item)
        else:
            runs.append([item])

    for run in runs:
        middle = (run[0][0] + run[-1][0]) / 2.0  # a neck of even width is placed at its middle
        i, kind, measure = min(run, key=lambda item: (item[2], abs(item[0] - middle)))
        z, area = areas[i]
        low, high = areas[run[0][0]][0], areas[run[-1][0]][0]
        cx, cy = _section_centre(_cross_section_at_z(triangles, z), bbox)
        size = (bbox["max_x"] - bbox["min_x"], bbox["max_y"] - bbox["min_y"], high - low + 2 * step)
        along = f", and stays that thin from Z={low:.1f} to {high:.1f} mm" if len(run) > 1 else ""
        if kind == "thin_neck":
            risks.append(
                StructuralRisk(
                    risk_type="thin_neck",
                    severity="critical" if area < min_area_mm2 * 0.5 else "warning",
                    location_mm=(cx, cy, z),
                    region_size_mm=size,
                    description=(
                        f"Cross-section narrows to {area:.1f} mm² at Z={z:.1f} mm{along}. "
                        f"This creates a weak point where the part is likely to snap "
                        f"under load. Minimum safe cross-section is {min_area_mm2:.0f} mm²."
                    ),
                    metric_name="cross_section_area_mm2",
                    metric_value=area,
                    metric_threshold=min_area_mm2,
                )
            )
        else:
            risks.append(
                StructuralRisk(
                    risk_type="stress_concentration",
                    severity="warning",
                    location_mm=(cx, cy, z),
                    region_size_mm=size,
                    description=(
                        f"Cross-section drops to {measure:.0%} of adjacent "
                        f"sections at Z={z:.1f} mm{along}. This abrupt change concentrates "
                        f"stress and may cause cracking under load."
                    ),
                    metric_name="section_ratio",
                    metric_value=measure,
                    metric_threshold=ratio_threshold,
                )
            )

    return risks


def _section_centre(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    bbox: dict[str, float],
) -> tuple[float, float]:
    """Where a cross-section sits on the bed: the middle of its outline,
    weighted by length; the middle of the part when the slice has none."""
    total = cx = cy = 0.0
    for (x1, y1), (x2, y2) in segments:
        length = math.hypot(x2 - x1, y2 - y1)
        total += length
        cx += length * (x1 + x2) / 2.0
        cy += length * (y1 + y2) / 2.0
    if total <= 0.0:
        return (bbox["min_x"] + bbox["max_x"]) / 2, (bbox["min_y"] + bbox["max_y"]) / 2
    return cx / total, cy / total


#: How far a member must reach out from what holds it up, with nothing under
#: it, to be read as a cantilever (mm) -- and it must reach at least as far as
#: it is thick where it leaves its support.  A shorter overhang is a lip or a
#: ledge, which the printability check's support verdict already covers; one
#: thicker than it is long is a boss or a bracket, which a load on its end
#: cannot bend.  Pinned by ``tests/test_cantilever_reaches_further_than_it_is_thick.py``.
_CANTILEVER_MIN_REACH_MM = 5.0

#: The cantilever reading looks down vertical columns this far apart (mm), or
#: further apart on a part too large to read this finely within the budgets
#: below.
_COLUMN_MM = 0.5

#: The most columns one reading lays out, and the most (face, column) pairs it
#: tests for where a column crosses a face: the work is bounded by these and
#: by the file's size, never by the distances written in it.
_COLUMN_BUDGET = 250_000
_COLUMN_TEST_BUDGET = 1_000_000

#: The (face, column) tests made at once, for memory.
_COLUMN_CHUNK = 50_000

#: Where each column runs inside its cell, as fractions of the cell: off the
#: middle, so the edges a CAD part draws along round numbers and diagonals
#: pass beside a column rather than through it.
_COLUMN_OFFSET = (0.5731, 0.5377)

#: Material closer than this (mm) along a column is one piece: two bodies
#: that touch, written a rounding error apart, are one.
_COLUMN_JOIN_MM = 0.01

#: A member's reach is measured from at most this many (point, support)
#: pairs across the whole part, points spread evenly over each member.
_REACH_PAIR_BUDGET = 16_000_000

#: A point of an overhang with support on its far side too -- support further
#: round than this from the nearest, seen from the point (degrees) -- is held
#: at both ends: a bridge, not a cantilever.
_BRIDGE_TURN_DEG = 120.0


def _find_cantilevers(
    triangles: list[tuple[tuple[float, ...], ...]],
) -> list[StructuralRisk]:
    """Find cantilevers: members that reach out sideways from what holds
    them up, with nothing under them -- a shelf arm, a flange printed in the
    air, a hook's reach, the rim of a cap on a stem.  One finding each,
    however finely it was tessellated, the longest first.

    The part is judged as it sits in the file.  Material is held up where it
    rests on the bed, or where the material beside it comes down from it at
    least as steeply as 45 degrees and is held up itself: a wall, a post, a
    brace.  Material with air under it and neither is overhanging, and an
    overhang reaching at least ``_CANTILEVER_MIN_REACH_MM`` from the nearest
    support, and at least as far as it is thick where it leaves that support,
    with no support on its far side, is a cantilever.

    Never a cantilever: a peg, or a bracket's upright, held from below its
    whole height as it prints (a column; a thin neck or a tall slender post
    is another check's); an overhang held at both ends (a bridge: the roof of
    a slot, the top of a window); a rounded underside curving down to the bed
    (a sphere, a donut), held by nothing beside it, whose overhang the
    printability check judges; a boss or a bracket thicker than it is long,
    which a load on its end cannot bend.  Pieces that only touch are read as
    one part.

    On a part too large to read at :data:`_COLUMN_MM` within the budgets,
    the columns stand further apart, and a member narrower than they are
    apart can go unseen.
    """
    import numpy as np

    from kiln.generation.validation import _bed_threshold_z

    tri = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
    with np.errstate(invalid="ignore"):
        tri = tri[(np.abs(tri) <= _FARTHEST_MM).all(axis=(1, 2))]  # NaN fails too
    if not len(tri):
        return []
    cols = _material_columns(tri)
    pieces = len(cols.bottom)
    if not pieces:
        return []
    on_bed = cols.bottom <= _bed_threshold_z(float(tri[:, :, 2].min()))

    # What holds what: a piece beside this one, starting at least a column's
    # width lower (45 degrees) and running up to this one's underside.
    beside = cols.beside()
    holder = np.full((pieces, 4), -1, dtype=np.int64)
    for d, column in enumerate(beside):
        h = cols.piece_from(column, cols.bottom - cols.cell * 0.999)  # a 45-degree chamfer holds
        reaches = (h >= 0) & (cols.top[np.maximum(h, 0)] >= cols.bottom - _COLUMN_JOIN_MM)
        holder[:, d] = np.where(reaches, h, -1)
    held_up = _held_up(on_bed, holder)

    overhang = ~on_bed  # air under it
    if not overhang.any():
        return []
    support = overhang & held_up
    # Undersides beside each other, no steeper than 45 degrees, are one surface.
    index = np.full(pieces, -1, dtype=np.int64)
    index[overhang] = np.arange(int(overhang.sum()))
    pairs = []
    for column in beside[0], beside[2]:  # each pair of neighbours once
        other = cols.piece_from(column, cols.bottom + cols.cell)
        safe = np.maximum(other, 0)
        joined = (overhang & (other >= 0) & overhang[safe]
                  & (np.abs(cols.bottom[safe] - cols.bottom) <= cols.cell))
        pairs.append(np.stack([index[joined], index[safe[joined]]]))
    surfaces = _joined(int(overhang.sum()), pairs)

    members = []
    smallest = _CANTILEVER_MIN_REACH_MM / (cols.cell * math.sqrt(2.0))  # columns a reach that long spans
    overhang_at = np.flatnonzero(overhang)
    for surface in surfaces:
        if len(surface) < smallest:
            continue
        surface = overhang_at[surface]
        holds, hangs = surface[support[surface]], surface[~held_up[surface]]
        if len(holds) and len(hangs):
            members.append((holds, hangs))
    demand = sum(len(h) * len(f) for h, f in members)
    share = min(1.0, math.sqrt(_REACH_PAIR_BUDGET / demand)) if demand else 1.0

    risks: list[StructuralRisk] = []
    for holds, hangs in members:
        found = _cantilever_reach(cols, _spread(holds, share), _spread(hangs, share))
        if found is None:
            continue
        reach, base, spread_xy = found
        thick = float(cols.top[base] - cols.bottom[base])
        if reach < max(_CANTILEVER_MIN_REACH_MM, thick):
            continue
        (x, y), z = cols.centres(np.array([base]))[0], float(cols.bottom[base])
        risks.append(
            StructuralRisk(
                risk_type="cantilever",
                severity="warning",
                location_mm=(float(x), float(y), z),
                region_size_mm=(float(spread_xy[0]), float(spread_xy[1]), thick),
                description=(
                    f"Cantilever at ({x:.1f}, {y:.1f}, {z:.1f}): it reaches {reach:.0f} mm "
                    f"out from what holds it up, {thick:.1f} mm thick ({reach / thick:.1f} times "
                    f"its thickness), with nothing under it. A load on its end bends it hardest "
                    f"where it meets its support, so that is where it is most likely to break."
                ),
                metric_name="cantilever_reach_mm",
                metric_value=reach,
                metric_threshold=_CANTILEVER_MIN_REACH_MM,
            )
        )
    risks.sort(key=lambda r: -r.metric_value)
    return risks


def _spread(items: Any, share: float) -> Any:
    """``share`` of ``items``, at least one, spread evenly through them."""
    import numpy as np

    keep = max(1, math.ceil(len(items) * share))
    if keep >= len(items):
        return items
    return items[np.linspace(0, len(items) - 1, keep).astype(np.int64)]


def _cantilever_reach(cols: _Columns, holds: Any, hangs: Any) -> tuple[float, int, Any] | None:
    """How far an overhang reaches from its supports ``holds``, measured from
    the points of it hanging in the air, ``hangs`` (pieces of ``cols``):
    ``(reach, the support it reaches from, its spread in x and y)``, or None
    when every point of it is held at both ends.

    A point's reach is its distance to the nearest support, one column added
    for the half column at each end the centres leave out.  A point with
    support on its far side as well (:data:`_BRIDGE_TURN_DEG`) is a bridge's,
    and reaches nothing.
    """
    import numpy as np

    at = cols.centres(holds)
    points = cols.centres(hangs)
    nearest = np.empty(len(hangs), dtype=np.int64)
    gap = np.empty(len(hangs))
    bridged = np.empty(len(hangs), dtype=bool)
    far_side = math.cos(math.radians(_BRIDGE_TURN_DEG))
    rows = max(1, 1_000_000 // max(1, len(holds)))  # pairs per pass, for memory
    for s in range(0, len(hangs), rows):
        to = at[None, :, :] - points[s:s + rows, None, :]
        dist = np.linalg.norm(to, axis=2)
        k = dist.argmin(axis=1)
        near = dist[np.arange(len(k)), k]
        towards = to[np.arange(len(k)), k] / np.maximum(near, 1e-12)[:, None]
        turn = np.einsum("ijk,ik->ij", to, towards) / np.maximum(dist, 1e-12)
        nearest[s:s + rows], gap[s:s + rows] = k, near
        bridged[s:s + rows] = (turn < far_side).any(axis=1)
    if bridged.all():
        return None
    reach = np.where(bridged, -1.0, gap)
    # Of the points reaching furthest (to within a column), the middle one:
    # the far end of an arm, not one of its corners.
    far = np.flatnonzero(reach >= reach.max() - cols.cell)
    tip = int(far[np.linalg.norm(points[far] - points[far].mean(axis=0), axis=1).argmin()])
    free = points[~bridged]
    spread_xy = free.max(axis=0) - free.min(axis=0) + cols.cell
    return float(reach.max()) + cols.cell, int(holds[nearest[tip]]), spread_xy


def _held_up(on_bed: Any, holder: Any) -> Any:
    """Which pieces are held up: those resting on the bed, then every piece
    a held-up piece holds (``holder[p]`` names the pieces beside ``p`` that
    hold it, -1 for none), and so on up.  Each piece is visited once."""
    import numpy as np

    held = on_bed.copy()
    who = holder.reshape(-1)
    whom = np.repeat(np.arange(len(holder)), holder.shape[1])
    has = who >= 0
    order = np.argsort(who[has], kind="stable")
    who, whom = who[has][order], whom[has][order]
    first = np.searchsorted(who, np.arange(len(holder)))
    count = np.searchsorted(who, np.arange(len(holder)), side="right") - first
    frontier = np.flatnonzero(held)
    while len(frontier):
        n = count[frontier]
        step = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
        lifted = np.unique(whom[np.repeat(first[frontier], n) + step])
        frontier = lifted[~held[lifted]]
        held[frontier] = True
    return held


@dataclass(frozen=True)
class _Columns:
    """Where a part is solid, read down vertical columns ``cell`` mm apart
    laid over it from ``origin``, ``shape`` columns along x and along y.

    Each piece is one stretch of solid in one column, ``bottom`` to ``top``,
    sorted by column and then upward; ``key`` orders them the same way, for
    finding the piece of a column at a height.
    """

    origin: Any
    cell: float
    shape: tuple[int, int]
    column: Any
    bottom: Any
    top: Any
    key: Any
    z_from: float
    z_span: float

    def piece_from(self, column: Any, z: Any) -> Any:
        """For each ``column`` (an index, or -1), its highest piece starting
        at or below ``z``; -1 where there is none."""
        import numpy as np

        at = np.searchsorted(self.key, _column_key(column, z, self.z_from, self.z_span), side="right") - 1
        safe = np.maximum(at, 0)
        found = (at >= 0) & (column >= 0) & (self.column[safe] == column)
        return np.where(found, at, -1)

    def beside(self) -> list[Any]:
        """For every piece, the column next to its own in each of four
        directions (+x, -x, +y, -y), or -1 past the edge of the grid."""
        import numpy as np

        nx, ny = self.shape
        ix, iy = self.column % nx, self.column // nx
        out = []
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            jx, jy = ix + dx, iy + dy
            inside = (jx >= 0) & (jx < nx) & (jy >= 0) & (jy < ny)
            out.append(np.where(inside, jy * nx + jx, -1))
        return out

    def centres(self, pieces: Any) -> Any:
        """The (x, y) of the columns ``pieces`` stand in."""
        import numpy as np

        nx = self.shape[0]
        ix, iy = self.column[pieces] % nx, self.column[pieces] // nx
        return np.stack([self.origin[0] + (ix + _COLUMN_OFFSET[0]) * self.cell,
                         self.origin[1] + (iy + _COLUMN_OFFSET[1]) * self.cell], axis=1)


def _column_key(column: Any, z: Any, z_from: float, z_span: float) -> Any:
    """A key ordering (column, height) pairs as :class:`_Columns` sorts its
    pieces: the column, then the height as a fraction of half a unit, so no
    height reaches the next column's keys."""
    import numpy as np

    return column + np.clip((z - z_from) / z_span, 0.0, 1.0) * 0.5


def _column_grid(tri: Any) -> tuple[Any, float, Any, Any, Any]:
    """The columns to read ``tri`` down: ``(origin, cell, shape, first,
    count)``, where ``first`` and ``count`` say which columns (along x and
    along y) each face's outline spans.  Cells start at :data:`_COLUMN_MM`
    and widen until the grid and the tests fit their budgets, or until one
    column covers the part."""
    import numpy as np

    flat = tri[:, :, :2]
    origin, far = flat.min(axis=(0, 1)), flat.max(axis=(0, 1))
    low, high = flat.min(axis=1), flat.max(axis=1)
    offset = np.asarray(_COLUMN_OFFSET)
    cell = _COLUMN_MM
    while True:
        shape = np.maximum(np.floor((far - origin) / cell - offset).astype(np.int64) + 1, 1)
        first = np.clip(np.ceil((low - origin) / cell - offset), 0, shape - 1).astype(np.int64)
        last = np.clip(np.floor((high - origin) / cell - offset), -1, shape - 1).astype(np.int64)
        count = np.maximum(last - first + 1, 0)
        tests = int((count[:, 0] * count[:, 1]).sum())
        fits = int(shape[0]) * int(shape[1]) <= _COLUMN_BUDGET and tests <= _COLUMN_TEST_BUDGET
        if fits or (shape == 1).all():
            return origin, cell, shape, first, count
        cell *= 1.5


def _material_columns(tri: Any) -> _Columns:
    """Read a part down vertical columns (:func:`_column_grid`): where each
    column crosses a face, and from those crossings where it is solid.

    Going up a column, a face looking down is where solid starts and one
    looking up is where it ends; counting them, a column is solid wherever
    more have started than ended, so bodies that overlap or touch are read as
    the solid they make together.  An inside-out mesh has every face turned
    over, and is read turned back.  A column whose faces do not close (a hole
    in the surface) is left out, and so is a face standing on its edge, which
    no column crosses.
    """
    import numpy as np

    origin, cell, shape, first, count = _column_grid(tri)
    offset = np.asarray(_COLUMN_OFFSET)
    (x0, y0), (x1, y1), (x2, y2) = (tri[:, k, :2].T for k in range(3))
    twice_area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)  # seen from above; below zero it faces down
    centred = tri - tri.reshape(-1, 3).mean(axis=0)
    outward = 1.0 if np.einsum("ij,ij->", centred[:, 0], np.cross(centred[:, 1], centred[:, 2])) >= 0 else -1.0
    tests = count[:, 0] * count[:, 1]
    faces = np.flatnonzero((np.abs(twice_area) > 1e-12) & (tests > 0))

    column, height, step = [np.empty(0, dtype=np.int64)], [np.empty(0)], [np.empty(0, dtype=np.int8)]
    done = np.cumsum(tests[faces])
    total = int(done[-1]) if len(done) else 0
    for part in np.split(faces, np.searchsorted(done, np.arange(_COLUMN_CHUNK, total, _COLUMN_CHUNK))):
        across, per = count[part, 0], tests[part]
        face = np.repeat(part, per)
        k = np.arange(per.sum()) - np.repeat(np.cumsum(per) - per, per)
        ix = first[face, 0] + k % np.repeat(across, per)
        iy = first[face, 1] + k // np.repeat(across, per)
        px, py = origin[0] + (ix + offset[0]) * cell, origin[1] + (iy + offset[1]) * cell
        t, a = tri[face], twice_area[face]
        u = ((px - t[:, 0, 0]) * (t[:, 2, 1] - t[:, 0, 1]) - (t[:, 2, 0] - t[:, 0, 0]) * (py - t[:, 0, 1])) / a
        v = ((t[:, 1, 0] - t[:, 0, 0]) * (py - t[:, 0, 1]) - (px - t[:, 0, 0]) * (t[:, 1, 1] - t[:, 0, 1])) / a
        w = 1.0 - u - v
        hit = (u >= 0) & (v >= 0) & (w >= 0)
        column.append((iy * shape[0] + ix)[hit])
        height.append((w * t[:, 0, 2] + u * t[:, 1, 2] + v * t[:, 2, 2])[hit])
        step.append(np.where(a[hit] * outward < 0, 1, -1).astype(np.int8))
    c, z, s = np.concatenate(column), np.concatenate(height), np.concatenate(step)
    if len(c):
        order = np.lexsort((-s, z, c))  # where two faces meet at one height, solid starts first
        c, z, s = c[order], z[order], s[order]
        starts = np.flatnonzero(np.r_[True, c[1:] != c[:-1]])
        ends = np.r_[starts[1:], len(c)] - 1
        wound = np.cumsum(s, dtype=np.int64)
        depth = wound - np.repeat(np.r_[0, wound[starts[1:] - 1]], ends - starts + 1)  # solid above each crossing
        closed = ~np.isin(c, c[ends[depth[ends] != 0]])
        enter = closed & (depth > 0) & (depth - s <= 0)
        leave = closed & (depth <= 0) & (depth - s > 0)
        c, z, top = c[enter], z[enter], z[leave]
    else:
        top = z
    if len(c):
        # Solid closer than _COLUMN_JOIN_MM along a column is one piece.
        apart = np.r_[True, (c[1:] != c[:-1]) | (z[1:] - top[:-1] > _COLUMN_JOIN_MM)]
        c, z, top = c[apart], z[apart], top[np.r_[apart[1:], True]]
    # Every height a reading asks about lies within two cells of a piece.
    z_from = float(z.min()) - 2 * cell - 1.0 if len(z) else 0.0
    z_span = float(top.max()) + 2 * cell + 1.0 - z_from if len(top) else 1.0
    return _Columns(origin, cell, (int(shape[0]), int(shape[1])), c, z, top,
                    _column_key(c, z, z_from, z_span), z_from, z_span)


def _find_sharp_corners(
    triangles: list[tuple[tuple[float, ...], ...]],
    *,
    angle_threshold_deg: float = _SHARP_ANGLE_THRESHOLD_DEG_PUBLIC,
) -> list[StructuralRisk]:
    """Find sharp inside corners, where load concentrates and cracks start.

    An edge counts when the surface turns INWARD across it -- the material
    wraps more than 180 degrees around it, as at the root of a peg, the inside
    of an L-bracket or the base of a rib -- by at least ``angle_threshold_deg``.
    An outside edge (the rim of a box or a cylinder, the facets of a convex
    curve) concentrates no stress and never counts.  Sharp inside edges that
    meet, or pass close by each other (:func:`_corners`), are one corner and
    one finding, however finely the surface was tessellated; and a finding is
    a warning to round it, never more, since how many facets a corner has
    says nothing about how bad it is.  A corner whose edges total less than
    :data:`_CORNER_FLOOR_MM` is not in the print and is not counted.

    Every edge of the mesh is read.  An edge whose two faces disagree about
    which side is outside is left out, since its corner cannot be told from
    its edge; and pieces that only touch (a peg set on a plate without being
    joined to it) share no edge, so the corner between them is not seen.
    """
    import numpy as np

    if not triangles:
        return []
    edges, verts, turns = _sharp_inside_edges(triangles, angle_threshold_deg)
    corners = []
    for group in _corners(edges, verts):
        a, b = verts[edges[group, 0]], verts[edges[group, 1]]
        lengths = np.linalg.norm(b - a, axis=1)
        if lengths.sum() < _CORNER_FLOOR_MM:
            continue
        middle = ((a + b) / 2 * lengths[:, None]).sum(axis=0) / lengths.sum()
        ends = np.vstack([a, b])
        corners.append((float(lengths.sum()), middle, ends.max(axis=0) - ends.min(axis=0), float(turns[group].max())))
    # The longest corner first: the one a fillet does the most for.
    corners.sort(key=lambda c: -c[0])

    risks: list[StructuralRisk] = []
    for length, middle, size, turn in corners:
        cx, cy, cz = (float(c) for c in middle)
        risks.append(
            StructuralRisk(
                risk_type="sharp_corner",
                severity="warning",
                location_mm=(cx, cy, cz),
                region_size_mm=(float(size[0]), float(size[1]), float(size[2])),
                description=(
                    f"Sharp inside corner near ({cx:.1f}, {cy:.1f}, {cz:.1f}), "
                    f"{length:.1f} mm of edge. Load concentrates at a sharp "
                    f"inside corner and cracks start there; a fillet spreads it."
                ),
                metric_name="inside_corner_turn_deg",
                metric_value=turn,
                metric_threshold=angle_threshold_deg,
            )
        )
    return risks


def _sharp_inside_edges(
    triangles: list[tuple[tuple[float, ...], ...]],
    angle_threshold_deg: float,
) -> tuple[Any, Any, Any]:
    """The edges of a mesh where the surface turns inward by more than
    ``angle_threshold_deg``: ``(edges, vertices, turns)``, each edge a pair of
    indices into ``vertices`` and its turn in degrees.

    Faces are joined where they share corners exactly, the way the mesh was
    written.  An edge shared by two faces is written once each way when the
    faces agree which side is outside; the turn from the first face's normal
    to the second's then points along the edge as the first face writes it
    when the edge is convex, and against it when the edge is an inside one.
    Faces with no area or no position (:data:`_FARTHEST_MM`), and edges
    shared by more than two faces, are left out.
    """
    import numpy as np

    tri = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
    with np.errstate(invalid="ignore"):
        tri = tri[(np.abs(tri) <= _FARTHEST_MM).all(axis=(1, 2))]  # NaN fails too
    if not len(tri):
        return np.empty((0, 2), dtype=np.int64), np.empty((0, 3)), np.empty(0)
    # Corners at the same point are one vertex.
    flat = tri.reshape(-1, 3)
    order = np.lexsort(flat.T[::-1])
    in_order = flat[order]
    new = np.r_[True, (in_order[1:] != in_order[:-1]).any(axis=1)]
    verts = in_order[new]
    faces = np.empty(len(flat), dtype=np.int64)
    faces[order] = np.cumsum(new) - 1
    faces = faces.reshape(-1, 3)
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    twice_area = np.linalg.norm(cross, axis=1)
    whole = twice_area > 1e-12
    normals = np.zeros_like(cross)
    normals[whole] = cross[whole] / twice_area[whole, None]
    # An inside-out mesh turns every corner around.  Measured from the part's
    # middle, so a surface that is not closed is judged the same wherever it sits.
    centred = tri - verts.mean(axis=0)
    outward = 1.0 if np.einsum("ij,ij->", centred[:, 0], np.cross(centred[:, 1], centred[:, 2])) >= 0 else -1.0

    halves = faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    face_of = np.repeat(np.arange(len(faces)), 3)
    key = halves.min(axis=1).astype(np.int64) * len(verts) + halves.max(axis=1)
    order = np.argsort(key, kind="stable")
    in_order = key[order]
    first = np.flatnonzero(np.r_[True, in_order[1:] != in_order[:-1]])
    count = np.diff(np.r_[first, len(key)])
    shared = first[count == 2]  # an edge three faces share is not one corner
    h0, h1 = order[shared], order[shared + 1]
    f0, f1 = face_of[h0], face_of[h1]
    keep = (halves[h0, 0] == halves[h1, 1]) & whole[f0] & whole[f1]
    h0, f0, f1 = h0[keep], f0[keep], f1[keep]

    n0, n1 = normals[f0], normals[f1]
    along = verts[halves[h0, 1]] - verts[halves[h0, 0]]
    along /= np.linalg.norm(along, axis=1)[:, None]
    # The sine of the turn, signed: below zero, the surface turns inward.  Two
    # faces folded flat onto each other turn neither way and are left out.
    side = outward * np.einsum("ij,ij->i", np.cross(n0, n1), along)
    inward = side < -1e-9
    cos_turn = np.einsum("ij,ij->i", n0, n1)
    sharp = inward & (cos_turn < math.cos(math.radians(angle_threshold_deg)))
    turns = np.degrees(np.arccos(np.clip(cos_turn[sharp], -1.0, 1.0)))
    return halves[h0[sharp]], verts, turns


def _corners(edges: Any, verts: Any) -> list[Any]:
    """Sharp inside edges grouped into corners, each an array of indices into
    ``edges``: edges that meet, or pass through the same or neighbouring
    ``_CORNER_CELL_MM`` cells, are one corner.  Edges closer than half a cell
    are always joined, and edges further apart than about three and a half
    cells never are.

    Past :data:`_CORNER_POINT_BUDGET` only each edge's two ends are laid down,
    and on a part too large to number its cells, only edges that meet are
    joined.  Either way every corner is still found; nearby ones may be told
    apart that would otherwise be one.
    """
    import itertools

    import numpy as np

    if not len(edges):
        return []
    cell = _CORNER_CELL_MM
    # Points along every edge, its two ends among them, no more than half a
    # cell apart; the cells they fall in, counted from the corner nearest the
    # origin so a part is grouped the same wherever it sits.
    a, b = verts[edges[:, 0]], verts[edges[:, 1]]
    steps = np.ceil(np.linalg.norm(b - a, axis=1) / (cell / 2)) + 1
    if steps.sum() > _CORNER_POINT_BUDGET:
        logger.info("Inside corners grouped by their edges' ends: %d edges ask for %.3g points", len(edges), steps.sum())
        steps = np.full(len(edges), 2.0)
    steps = steps.astype(np.int64)
    owner = np.repeat(np.arange(len(edges)), steps)
    nth = np.arange(len(owner)) - np.repeat(np.cumsum(steps) - steps, steps)
    points = a[owner] + (b - a)[owner] * (nth / np.repeat(steps - 1, steps))[:, None]
    grid = np.floor((points - points.min(axis=0)) / cell).astype(np.int64) + 1
    span = grid.max(axis=0) + 2
    if float(span[0]) * float(span[1]) * float(span[2]) >= 2.0**62:
        # Too large to number every cell: join the edges that meet.
        ends = edges.reshape(-1)
        _, first_end, which_end = np.unique(ends, return_index=True, return_inverse=True)
        end_owner = np.repeat(np.arange(len(edges)), 2)
        return _joined(len(edges), [np.stack([end_owner, end_owner[first_end][which_end.reshape(-1)]])])

    def number(g: Any) -> Any:
        return (g[..., 0] * span[1] + g[..., 1]) * span[2] + g[..., 2]

    cells, first, which = np.unique(number(grid), return_index=True, return_inverse=True)
    one = owner[first]  # an edge in each cell
    pairs = [np.stack([owner, one[which.reshape(-1)]])]
    for step in itertools.product((-1, 0, 1), repeat=3):
        if step > (0, 0, 0):  # each pair of neighbouring cells once
            there = number(grid[first] + np.asarray(step))
            at = np.minimum(np.searchsorted(cells, there), len(cells) - 1)
            hit = cells[at] == there
            pairs.append(np.stack([one[hit], one[at[hit]]]))
    return _joined(len(edges), pairs)


def _joined(count: int, pairs: list[Any]) -> list[Any]:
    """``count`` items in the groups ``pairs`` (arrays of two rows) join them into."""
    import numpy as np

    parent = list(range(count))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for u, v in np.unique(np.concatenate(pairs, axis=1), axis=1).T.tolist():
        parent[root(u)] = root(v)
    groups: dict[int, list[int]] = {}
    for i in range(count):
        groups.setdefault(root(i), []).append(i)
    return [np.asarray(g) for g in groups.values()]


def _check_base_adequacy(
    triangles: list[tuple[tuple[float, ...], ...]],
    bbox: dict[str, float],
    *,
    tipping_ratio: float = _TIPPING_RATIO_PUBLIC,
) -> list[StructuralRisk]:
    """Check if the base (bottom layer) is adequate for the part's height.

    ``tipping_ratio`` is the height-to-base ratio above which
    the part is flagged as toppling-prone.  Lower → more parts flagged
    (public-stricter)."""
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
    if base_min_dim > 0 and z_range / base_min_dim > tipping_ratio:
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
                    f"(threshold: {tipping_ratio:.1f}:1). Part may tip or "
                    f"warp during printing. Add a wider base, use a brim, or re-orient."
                ),
                metric_name="height_to_base_ratio",
                metric_value=z_range / base_min_dim,
                metric_threshold=tipping_ratio,
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


# ---------------------------------------------------------------------------
# Reinforcement recommendations
# ---------------------------------------------------------------------------


def _generate_reinforcements(
    risks: list[StructuralRisk],
    bbox: dict[str, float],
) -> list[ReinforcementRecommendation]:
    """Generate specific reinforcement recommendations for identified risks.

    A number the advice quotes ("redesign with a minimum X mm dimension",
    "below X:1") is the threshold its risk was flagged against
    (``risk.metric_threshold``), so the advice and the risk always agree,
    and wording stays identical across tiers."""
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
                        f"with a minimum {math.sqrt(risk.metric_threshold):.1f} mm dimension."
                    ),
                    estimated_strength_gain="More material where the part is thinnest",
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
                    estimated_strength_gain="Spreads the load where the section changes",
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
                    estimated_strength_gain="Braces the overhang at its root",
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
                        f"Round the sharp inside corner near ({risk.location_mm[0]:.0f}, "
                        f"{risk.location_mm[1]:.0f}, {risk.location_mm[2]:.0f}) with a "
                        f"1-2 mm fillet: a rounded inside corner spreads the load a "
                        f"sharp one concentrates. Use add_mesh_fillet()."
                    ),
                    estimated_strength_gain="Rounds the corner so load isn't focused there",
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
                            f"{risk.metric_threshold:.1f}:1. Add a brim in the slicer "
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
                        "to the print layers, not perpendicular. FDM parts are "
                        "weaker across layers than along them. Use optimize_print_orientation() "
                        "for structural strength, not just overhang minimization."
                    ),
                    estimated_strength_gain="Puts the load along the layers, not across them",
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
        "FDM layers create anisotropic strength: parts are weaker across "
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
    min_cross_section_mm2: float | None = None,
    sharp_angle_threshold_deg: float | None = None,
) -> list[StructuralRisk]:
    """Analyze an STL for structural risks.

    Threshold tiering — DEMOTE, NOT DELETE:

    - Detection always surfaces at the public (stricter) thresholds,
      so EVERY caller — overlay or not — sees the same features.  A Pro
      caller is never silently denied a warning a free caller would get.
    - With the ``structural_thresholds`` overlay (Pro): the calibrated
      (looser) values become an ESCALATION line, not a detection line.
      A feature past the calibrated threshold stays a full warning; one
      that only trips the stricter public floor is DEMOTED to ``info``
      (e.g. a 4.5:1 cantilever is a warning for free but an info note
      for Pro, whose calibrated bar is 5.0:1).  Quieter for the
      experienced user — nothing hidden.

    Explicit kwargs pin a dial (surface == escalate), disabling its
    demotion — power-user escape hatch.  ``None`` means "surface at the
    public floor; demote with the overlay if loaded."  The sharp-corner
    threshold is one value at every tier (``_SHARP_ANGLE_THRESHOLD_DEG_PUBLIC``):
    the overlay never moves it, and an inside corner that counts is a
    warning for every caller.

    Performs geometric analysis to find:
    - Thin necks (narrow cross-sections that will snap)
    - Stress concentrations (abrupt section changes)
    - Cantilevers (unsupported overhanging geometry)
    - Sharp corners (stress concentration at concave edges)
    - Insufficient base (topple risk)
    - Weak layer adhesion zones (overhangs in structural areas)

    :param file_path: Path to STL file.
    :param min_cross_section_mm2: Override the resolved threshold.
        ``None`` (default) = use the overlay if loaded, else the
        stricter public default (5.0 mm²).
    :param sharp_angle_threshold_deg: Same semantics; public default
        65.0°.
    :returns: List of :class:`StructuralRisk` findings; empty for a file
        whose size puts its units in question, where no risk is judged
        (:func:`generate_improvement_plan` carries the question).
    """
    from kiln.design_intelligence import load_pro_overlay_or_empty

    part = _read_part(file_path)
    if not part.triangles or part.units_question:
        return []

    triangles, bbox = part.triangles, part.bbox

    # --- Demote-not-delete tiering.
    # SURFACE thresholds are always the public (stricter) values, so a Pro
    # caller sees exactly the same features a free caller would — nothing a
    # free user is warned about ever silently disappears for someone who paid.
    # When the overlay is present, its calibrated (looser) values become the
    # ESCALATION line: a feature that still trips the calibrated threshold
    # stays a full warning; one that only trips the stricter public floor is
    # DEMOTED to "info" — quieted for the experienced user, never hidden.
    # An explicit kwarg pins a dial (surface == escalate), disabling demotion
    # for that dial (power-user escape hatch).
    overlay = load_pro_overlay_or_empty("structural_thresholds")
    surf_thin = _THIN_NECK_RATIO_PUBLIC
    surf_cant = _TIPPING_RATIO_PUBLIC
    surf_area = (min_cross_section_mm2 if min_cross_section_mm2 is not None
                 else _MIN_CROSS_SECTION_MM2_PUBLIC)
    surf_sharp = (sharp_angle_threshold_deg if sharp_angle_threshold_deg is not None
                  else _SHARP_ANGLE_THRESHOLD_DEG_PUBLIC)

    def _dial_risks(thin: float, cant: float, area: float, sharp: float) -> list[StructuralRisk]:
        out: list[StructuralRisk] = []
        out.extend(_find_thin_necks(
            triangles, bbox, min_area_mm2=area, ratio_threshold=thin,
        ))
        out.extend(_find_sharp_corners(triangles, angle_threshold_deg=sharp))
        out.extend(_check_base_adequacy(triangles, bbox, tipping_ratio=cant))
        return out

    def _key(r: StructuralRisk) -> tuple:
        # A geometric feature has a stable (type, metric, location) across runs.
        return (r.risk_type, r.metric_name,
                round(r.location_mm[0], 1), round(r.location_mm[1], 1),
                round(r.location_mm[2], 1))

    # Dial-gated detectors surface at the public (strict) thresholds.
    dial_risks = _dial_risks(surf_thin, surf_cant, surf_area, surf_sharp)

    if overlay:
        esc_thin = overlay.get("thin_neck_ratio", surf_thin)
        esc_cant = overlay.get("tipping_ratio", surf_cant)
        esc_area = surf_area if min_cross_section_mm2 is not None else overlay.get(
            "min_cross_section_mm2", surf_area)
        esc_sharp = surf_sharp  # one value at every tier; never the overlay's to move
        # Features that still trip the calibrated thresholds are genuine
        # warnings; those that only trip the stricter public floor are
        # borderline → demote to "info" rather than drop them.
        survivors = {_key(r) for r in _dial_risks(esc_thin, esc_cant, esc_area, esc_sharp)}
        for r in dial_risks:
            if r.severity != "info" and _key(r) not in survivors:
                r.severity = "info"
                r.description += (
                    " (Borderline: Kiln Pro's calibrated thresholds quiet this "
                    "to a note rather than a warning — review it yourself if "
                    "this feature is load-bearing.)"
                )

    # Dial-free detectors are unaffected by the overlay; run once.
    risks: list[StructuralRisk] = dial_risks
    risks.extend(_find_cantilevers(triangles))
    risks.extend(_find_weak_layer_adhesion_zones(triangles, bbox))

    # Sort by severity: critical first, then warning, then info
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    risks.sort(key=lambda r: severity_order.get(r.severity, 3))

    return risks


def recommend_reinforcements(
    file_path: str,
    *,
    min_cross_section_mm2: float | None = None,
) -> list[ReinforcementRecommendation]:
    """Analyze an STL and recommend specific reinforcements.

    :param file_path: Path to STL file.
    :param min_cross_section_mm2: Minimum safe cross-section area; ``None``
        defers to ``analyze_structural_risks`` (overlay-resolved or
        stricter public default).
    :returns: List of :class:`ReinforcementRecommendation`; empty for a
        file whose size puts its units in question.
    """
    part = _read_part(file_path)
    if not part.triangles or part.units_question:
        return []

    bbox = part.bbox
    risks = analyze_structural_risks(
        file_path, min_cross_section_mm2=min_cross_section_mm2
    )
    return _generate_reinforcements(risks, bbox)


def assess_load_bearing(file_path: str) -> LoadAnalysis:
    """Analyze load-bearing characteristics from mesh geometry.

    :param file_path: Path to STL file.
    :returns: :class:`LoadAnalysis` with load surface info and orientation
        advice; for a file whose size puts its units in question, that
        question and nothing judged.
    """
    part = _read_part(file_path)
    if part.units_question:
        return LoadAnalysis(
            primary_load_axis="unknown",
            load_surfaces=[],
            weak_axis="unknown",
            layer_direction_concern="",
            recommended_print_orientation="",
            orientation_reasoning="",
            units_question=part.units_question,
        )
    triangles, bbox = part.triangles, part.bbox
    if not triangles:
        return LoadAnalysis(
            primary_load_axis="unknown",
            load_surfaces=[],
            weak_axis="unknown",
            layer_direction_concern="Could not parse mesh.",
            recommended_print_orientation="upright",
            orientation_reasoning="Default — mesh could not be analyzed.",
        )

    return _analyze_load_bearing(triangles, bbox)


# ---------------------------------------------------------------------------
# What a person is told about the risks
# ---------------------------------------------------------------------------
#
# A sentence names the risks the analysis found and the first thing to do.
# The structural score and grade rank versions of a part against each other
# and stay in the report, the same rule the printability score follows: a
# person told "Score: 62/100 (D)" learns nothing to act on.

#: Each kind of structural risk in plain words (one of it, several) and the
#: first thing to do about it.
_RISK_WORDS: dict[str, tuple[str, str, str]] = {
    "thin_neck": ("thin neck", "thin necks", "thicken the narrow section"),
    "stress_concentration": (
        "abrupt change in cross-section", "abrupt changes in cross-section",
        "smooth the change in section with a fillet",
    ),
    "cantilever": ("cantilever", "cantilevers", "add a gusset under the overhang"),
    "sharp_corner": ("sharp inside corner", "sharp inside corners", "round the inside corners with fillets"),
    "insufficient_base": ("tipping risk", "tipping risks", "widen the base or add a brim"),
    "weak_layer_adhesion": (
        "overhang area with weak layer bonds", "overhang areas with weak layer bonds",
        "orient the part so the load runs along the layers",
    ),
}

#: The severities a sentence counts; an ``info`` finding is a borderline note.
_COUNTED = ("critical", "warning")


def _risk_words(risk_type: str) -> tuple[str, str, str]:
    plain = risk_type.replace("_", " ")
    return _RISK_WORDS.get(risk_type, (plain, f"{plain}s", "see the reinforcements in the plan"))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def structural_risk_counts(risks: Sequence[StructuralRisk]) -> str:
    """How many structural risks a part has, in words: ``"1 critical
    structural risk and 3 warnings"``, or ``"no structural risks"``."""
    critical = sum(1 for r in risks if r.severity == "critical")
    warnings = sum(1 for r in risks if r.severity == "warning")
    if critical and warnings:
        return f"{_plural(critical, 'critical structural risk')} and {_plural(warnings, 'warning')}"
    if critical:
        return _plural(critical, "critical structural risk")
    if warnings:
        return _plural(warnings, "structural warning")
    return "no structural risks"


def _named_risks(
    risks: Sequence[StructuralRisk], done: frozenset[str] = frozenset(),
) -> tuple[str, str | None]:
    """Each kind of counted risk named with how many, the worst first, and
    the first thing to do about the worst kind an edit has not already
    addressed (*done*); ``None`` when every kind left was addressed."""
    kinds: dict[str, list[StructuralRisk]] = {}
    for risk in risks:
        if risk.severity in _COUNTED:
            kinds.setdefault(risk.risk_type, []).append(risk)

    def critical(group: list[StructuralRisk]) -> int:
        return sum(1 for r in group if r.severity == "critical")

    # Critical kinds first, then the most frequent; a tie keeps the
    # analysis's own order.
    order = sorted(kinds.items(), key=lambda kv: (-critical(kv[1]), -len(kv[1])))
    named = []
    for kind, group in order:
        one, many, _ = _risk_words(kind)
        n, c = len(group), critical(group)
        said = f"{'an' if one[0] in 'aeiou' else 'a'} {one}" if n == 1 else f"{n} {many}"
        if c:
            said += " (critical)" if c == n else f" ({c} critical)"
        named.append(said)
    first = next((_risk_words(kind)[2] for kind, _ in order if kind not in done), None)
    return ", ".join(named), first


def _borderline_note(risks: Sequence[StructuralRisk]) -> str:
    notes = sum(1 for r in risks if r.severity not in _COUNTED)
    if not notes:
        return ""
    return f" {_plural(notes, 'borderline feature')} {'is' if notes == 1 else 'are'} noted in the report."


def describe_structural_risks(risks: Sequence[StructuralRisk]) -> str:
    """What a person is told about a part's structural risks: how many, each
    kind named with the worst first, and the first thing to do.

    The one wording every door uses (the improvement plan, the reinforcement
    result, the slicer-settings note, the template sweep, the design audit's
    structural gate).  It states no score or grade.
    """
    if not any(r.severity in _COUNTED for r in risks):
        return "No structural risks found." + _borderline_note(risks)
    named, first = _named_risks(risks)
    return f"{structural_risk_counts(risks)}: {named}. First, {first}." + _borderline_note(risks)


def _carries_more_risk(before: DesignImprovementPlan, after: DesignImprovementPlan) -> bool:
    """Whether *after* carries more structural risk than *before*: more
    critical risks, or as many and more warnings.

    Read from the risks, not the scores, because the score stops at 0: a
    part that went from one critical risk to three scores 0 both times.  The
    one test both the reinforcement's own check of each step and the
    sentence it ends with (:func:`describe_structural_change`) use.
    """
    return (after.critical_count, after.warning_count) > (before.critical_count, before.warning_count)


def describe_structural_change(
    before: DesignImprovementPlan,
    after: DesignImprovementPlan,
    applied: Sequence[dict[str, Any]] = (),
) -> str:
    """What reinforcing a part did to its structural risks, in words: what
    it had and what is left, and whether it now carries more
    (:func:`_carries_more_risk`).

    The next step is never an edit *applied* already addressed: risks of a
    kind it addressed are ones it did not reach, and are said as that, with
    the edges it left sharp on purpose counted.
    """
    had = structural_risk_counts(before.risks)
    if not any(r.severity in _COUNTED for r in after.risks):
        if not any(r.severity in _COUNTED for r in before.risks):
            return "No structural risks before or after."
        return f"It had {had}; none remain." + _borderline_note(after.risks)
    done = frozenset(
        kind for entry in applied for kind in [entry.get("addresses"), *entry.get("also_addresses", [])] if kind
    )
    named, first = _named_risks(after.risks, done)
    if first:
        now = f"it now has {structural_risk_counts(after.risks)}: {named}. First, {first}."
    else:
        left = [
            edge for entry in applied for edge in ((entry.get("edges") or {}).get("left_sharp") or ())
            if edge.get("corner") != "smooth"
        ]
        failed = sum(1 for edge in left if edge.get("failed"))
        now = f"it now has {structural_risk_counts(after.risks)}: {named}, which the reinforcement did not reach."
        if len(left) > failed:
            now += f" It left {_plural(len(left) - failed, 'edge')} sharp on purpose."
        if failed:
            now += f" The CAD kernel could not round {_plural(failed, 'edge')}."
    if _carries_more_risk(before, after):
        said = f"The reinforced part carries more structural risk than the original: it had {had}; {now}"
    else:
        said = f"It had {had}; {now}"
    return said + _borderline_note(after.risks)


def generate_improvement_plan(
    file_path: str,
    *,
    min_cross_section_mm2: float | None = None,
    sharp_angle_threshold_deg: float | None = None,
) -> DesignImprovementPlan:
    """Generate a complete structural improvement plan for a design.

    Combines risk analysis, reinforcement recommendations, and load analysis
    into a single actionable report with an overall structural score.

    :param file_path: Path to STL file.
    :param min_cross_section_mm2: Minimum safe cross-section area.
    :param sharp_angle_threshold_deg: Angle threshold for sharp edge detection.
    :returns: :class:`DesignImprovementPlan`.  For a file whose size puts
        its units in question, the plan is that question and nothing else:
        no risk, count, score or reinforcement.
    """
    part = _read_part(file_path)
    if not part.triangles:
        return DesignImprovementPlan(
            file_path=file_path,
            summary="Could not parse mesh file.",
        )
    if part.units_question:
        return DesignImprovementPlan.not_judged(file_path, part.units_question)

    triangles, bbox = part.triangles, part.bbox

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

    # Start at 100 and subtract once per KIND of risk, at its worst severity:
    # six identical peg roots are one thing to round, and one narrow neck
    # read at twenty heights is one neck.  Different kinds add up, so a part
    # that gains a new kind of risk never scores higher.  The counts above
    # still count every risk.
    critical_kinds = {r.risk_type for r in risks if r.severity == "critical"}
    warning_kinds = {r.risk_type for r in risks if r.severity == "warning"} - critical_kinds
    score = 100
    score -= len(critical_kinds) * 25
    score -= len(warning_kinds) * 10
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

    # The summary names the risks; the score and grade above stay in the
    # plan for comparing versions of the part.
    summary = describe_structural_risks(risks)
    if reinforcements:
        summary += f" {_plural(len(reinforcements), 'reinforcement')} recommended."

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
    #: None when nothing was judged (``units_question``).
    before_score: int | None = 0
    after_score: int | None = 0
    before_grade: str = ""
    after_grade: str = ""
    summary: str = ""
    #: The reinforced part as CAD, beside ``output_path``, when every edit
    #: applied was made on the part's CAD file.
    step_path: str | None = None
    #: What the edge finishing did, or would do, about how the part prints.
    print_aware: str = ""
    #: The units question the file's size raises: when set, nothing was
    #: judged, reinforced or written, and ``output_path`` is the file as given.
    units_question: str = ""

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
            **({"step_path": self.step_path} if self.step_path else {}),
            **({"print_aware": self.print_aware} if self.print_aware else {}),
            **({"units_question": self.units_question} if self.units_question else {}),
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
    #: The units question the file's size raises: when set, the settings
    #: are the material's defaults and nothing about the part was judged.
    units_question: str = ""

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
            **({"units_question": self.units_question} if self.units_question else {}),
        }


def apply_reinforcements(
    file_path: str,
    *,
    output_path: str | None = None,
    min_cross_section_mm2: float | None = None,
    sharp_angle_threshold_deg: float | None = None,
    fillet_radius_mm: float = 1.5,
    wall_thicken_mm: float = 0.6,
    base_height_mm: float = 2.0,
    printer_id: str | None = None,
    nozzle_mm: float | None = None,
    layer_height_mm: float | None = None,
    material: str | None = None,
    fastener: str | None = None,
) -> ReinforcementResult:
    """Analyze a part, then auto-apply structural reinforcements.

    Runs the full improvement plan, then applies fixable reinforcements
    in sequence:

    1. **thicken_wall** → :func:`kiln.wall_thicken.thicken_part` (the CAD file when
       there is one, a measured mesh offset otherwise)
    2. **fillet** / **chamfer** → :func:`kiln.edge_finish.fillet_part` /
       ``chamfer_part``, on the part's CAD file, on its sharp inside edges at
       the angle the plan found them at, sized for the printer it prints on
    3. **add_base** → unions a base plate under the part via OpenSCAD
       boolean, wide enough that the part no longer reads as a tipping risk
       at the ratio the risk was found at

    The edits that work on the part's CAD file run first and hand the CAD
    on to each other; the base plate is a union on the mesh, so it runs
    after.  When every edit applied was a CAD one, the result carries
    ``step_path``: the reinforced part as CAD.

    Thickening, rounding, bevelling and the base plate act on the whole
    part, so each runs once: its one entry names the first risk it
    ``addresses`` and lists the rest of its kind under ``also_addresses``.
    Each edit is kept only if the part it makes carries no more structural
    risk than the part before it (:func:`_carries_more_risk`); one that does
    is left out, listed in ``skipped`` with what it would have added, so the
    part handed back is never worse than the part given.  Reinforcements
    that can't be auto-applied are listed in ``skipped`` with guidance for
    the agent: ``reorient``, and ``gusset``, since a cantilever finding says
    where an overhang is but not which wall it hangs from, and a rib placed
    without that braces nothing (``locations_mm`` lists where to add one).

    Edge finishing is literal by default -- the radius asked, on every sharp
    inside edge; outside edges are left as they are -- and its entry lists
    what will print badly.  kiln-pro
    (https://kiln3d.com) may choose the finish edge by edge for the printer
    and material instead; ``print_aware`` says which happened.

    :param file_path: Path to the part: an STL, or its STEP file.
    :param output_path: Output path (defaults to ``<name>_reinforced.stl``).
    :param min_cross_section_mm2: Minimum safe cross-section area.
    :param sharp_angle_threshold_deg: How far the surface must turn inward
        across an edge, in degrees, for an inside corner to count as sharp.
    :param fillet_radius_mm: Fillet radius for sharp corners.
    :param wall_thicken_mm: Amount to thicken thin walls.
    :param base_height_mm: Height of stabilizing base plate.
    :param printer_id: The printer the part is for; sizes the edge finishing.
    :param nozzle_mm: Its nozzle, when it differs from the one on record.
    :param layer_height_mm: The layer height it will print at.
    :param material: The material it will print in.
    :param fastener: The fastener the part's holes take, when the user named
        one; nothing reads the geometry to guess it.  With kiln-pro's edge
        choice, each hole that threads onto it is prepared for it as the
        corners are rounded; without, the holes are left as drawn and
        ``print_aware`` says so once.
    :returns: :class:`ReinforcementResult` with before/after scores.
    """
    from kiln.step_import import ensure_mesh_path, is_step_file

    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"File not found: {file_path}")
    # The analysis reads a mesh; a STEP is read as Kiln's mesh of it, and
    # stays the file the CAD edits start from.
    mesh_path = ensure_mesh_path(file_path)[0] if is_step_file(file_path) else file_path

    # Step 1: Get the improvement plan (before state)
    plan = generate_improvement_plan(
        mesh_path,
        min_cross_section_mm2=min_cross_section_mm2,
        sharp_angle_threshold_deg=sharp_angle_threshold_deg,
    )

    if plan.units_question:
        # Nothing judged, so nothing to reinforce, and no file is written: a
        # copy of the file as given would read as the reinforced part.
        return ReinforcementResult(
            output_path=file_path,
            original_path=file_path,
            before_score=None,
            after_score=None,
            summary=f"{plan.units_question}  Nothing was reinforced and no file was written.",
            units_question=plan.units_question,
        )

    if not plan.reinforcements:
        out = output_path or str(path)
        if output_path and output_path != file_path:
            import shutil
            shutil.copy2(mesh_path, output_path)
        return ReinforcementResult(
            output_path=out,
            original_path=file_path,
            before_score=plan.overall_structural_score,
            after_score=plan.overall_structural_score,
            before_grade=plan.structural_grade,
            after_grade=plan.structural_grade,
            summary="No reinforcements needed — design is structurally sound.",
        )

    # The edges are rounded or bevelled at the threshold the plan found them
    # at -- None means the public default there, and must here too.
    edge_angle_deg = (sharp_angle_threshold_deg if sharp_angle_threshold_deg is not None
                      else _SHARP_ANGLE_THRESHOLD_DEG_PUBLIC)

    # Step 2: Apply reinforcements in priority order
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    import shutil
    import tempfile

    work_dir = tempfile.mkdtemp(prefix="kiln_reinforce_")
    # The CAD edits start from the file as given, so its CAD is found: a
    # STEP, or a mesh Kiln converted with its STEP still beside it.  Nothing
    # below writes to it.
    current_path = file_path
    current_step: str | None = None

    # CAD-capable edits first, then by priority: a union on the mesh ahead
    # of them would leave no CAD to round.
    priority_order = {"high": 0, "medium": 1, "low": 2}
    cad_capable = ("thicken_wall", "fillet", "chamfer")
    sorted_recs = sorted(
        plan.reinforcements,
        key=lambda r: (r.reinforcement_type not in cad_capable, priority_order.get(r.priority, 3)),
    )

    printer = {"printer_id": printer_id, "nozzle_mm": nozzle_mm, "layer_height_mm": layer_height_mm, "material": material}
    policy = _edge_policy(material, fastener)
    edge_replies: list[dict[str, Any]] = []

    # Thickening, rounding, bevelling and the base plate act on the WHOLE
    # part, not on the spot a recommendation names: each runs once, and every
    # later recommendation of its kind is answered by that one edit.  (Run once per
    # recommendation, fourteen sharp corners filleted the part fourteen
    # times over -- or refused it fourteen times, identically.)
    whole_part = {
        "thicken_wall": (
            lambda p: _apply_thicken(p, work_dir, wall_thicken_mm), {"amount_mm": wall_thicken_mm},
        ),
        "fillet": (
            lambda p: _apply_edge_finish(p, work_dir, "fillet", fillet_radius_mm, edge_angle_deg, printer, policy),
            {"radius_mm": fillet_radius_mm},
        ),
        "chamfer": (
            lambda p: _apply_edge_finish(p, work_dir, "chamfer", _CHAMFER_MM, edge_angle_deg, printer, policy),
            {"distance_mm": _CHAMFER_MM},
        ),
    }
    # The base plate is sized from the tipping risk it answers, read off the
    # risk itself: the ratio it was found at.
    tipping = [r.metric_threshold for r in plan.risks if r.metric_name == "height_to_base_ratio"]
    whole_part["add_base"] = (
        lambda p: _base_reply(_apply_base(as_mesh(p), work_dir, base_height_mm, min(tipping, default=None))),
        {"height_mm": base_height_mm},
    )
    answered: dict[str, dict[str, Any]] = {}

    def as_mesh(p: str) -> str:
        return ensure_mesh_path(p)[0] if is_step_file(p) else p

    # Every edit is measured as it lands, read the way the original was, and
    # kept only if the part it makes carries no more structural risk than the
    # part before it, in no more solids and as sealed.  One reading per edit,
    # and at most one edit of each kind, so the work stays a few passes over
    # the file whatever it holds.
    measured = (plan, _part_shape(mesh_path))

    def kept(reply: dict[str, Any]) -> dict[str, Any]:
        """*reply*, or the same edit left out with the reason."""
        nonlocal measured
        made_path = as_mesh(reply["path"])
        made = generate_improvement_plan(
            made_path, min_cross_section_mm2=min_cross_section_mm2,
            sharp_angle_threshold_deg=sharp_angle_threshold_deg,
        )
        before, (solids, sealed) = measured
        shape = _part_shape(made_path)
        if made.load_analysis is None:
            why = "Kiln could not read back the part it made"
        elif _carries_more_risk(before, made):
            why = (f"the part it made carried more structural risk: {structural_risk_counts(made.risks)}, "
                   f"where the part before it had {structural_risk_counts(before.risks)}")
        elif shape[0] > solids:
            why = f"the part it made printed as {_plural(shape[0], 'separate solid')} where the part before it was {solids}"
        elif sealed and not shape[1]:
            why = "the part it made had holes in its surface where the part before it was sealed"
        else:
            measured = (made, shape)
            return reply
        return {**reply, "success": False, "message": f"Left out: {why}."}

    for rec in sorted_recs:
        try:
            kind = rec.reinforcement_type
            if kind in answered:
                entry = answered[kind]
                if rec.addresses_risk != entry["addresses"] and rec.addresses_risk not in entry.get("also_addresses", []):
                    entry.setdefault("also_addresses", []).append(rec.addresses_risk)
                if "locations_mm" in entry:
                    entry["locations_mm"].append([round(c, 1) for c in rec.location_mm])

            elif kind in whole_part:
                edit, settings = whole_part[kind]
                reply = edit(current_path)
                if reply.get("success"):
                    reply = kept(reply)
                if reply.get("success"):
                    current_path, current_step = reply["path"], reply.get("step_path")
                    answered[kind] = {"type": kind, **settings, "addresses": rec.addresses_risk}
                    if reply.get("edges"):
                        answered[kind]["edges"] = reply["edges"]
                        edge_replies.append(reply)
                    applied.append(answered[kind])
                else:
                    answered[kind] = {"type": kind, "reason": reply["message"], "addresses": rec.addresses_risk}
                    if reply.get("edges"):
                        answered[kind]["edges"] = reply["edges"]
                    skipped.append(answered[kind])

            elif kind == "gusset":
                # Listed, never placed: a rib braces an overhang only from the
                # wall it hangs from, and the finding does not say which wall
                # that is.  (Placed at the finding, it was a flat triangle on
                # the bed: loose in a donut's hole, loose under a post's arm.)
                answered[kind] = {
                    "type": "gusset", "reason": _GUSSET_NOT_PLACED, "addresses": rec.addresses_risk,
                    "locations_mm": [[round(c, 1) for c in rec.location_mm]], "guidance": _GUSSET_GUIDANCE,
                }
                skipped.append(answered[kind])

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

    shutil.copy2(as_mesh(current_path), output_path)
    step_path = None
    if current_step and Path(output_path).suffix.lower() == ".stl":
        from kiln.step_import import keep_step_beside

        scratch_step = str(Path(work_dir) / "reinforced_out.step")
        shutil.copy2(current_step, scratch_step)
        step_path, _ = keep_step_beside(scratch_step, output_path, file_path)

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
    # What changed, in risks named; before_score / after_score below compare
    # the two versions.
    if applied:
        summary_parts.append(describe_structural_change(plan, after_plan, applied))
    else:
        summary_parts.append(f"The part is as it was. {describe_structural_risks(plan.risks)}")

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
        step_path=step_path,
        print_aware=_print_aware_note(edge_replies, policy is not None, fastener),
    )


#: The bevel a ``chamfer`` recommendation gets, mm: the door's own default.
_CHAMFER_MM = 0.5


def _edge_policy(material: str | None, fastener: str | None = None) -> Any:
    """kiln-pro's choice of finish for each edge, when the caller has it; else ``None``.

    The contract: ``choose_finishes(chains, frame, material=..., fastener=...)``
    answers a :data:`kiln.edge_plan.Choice` per chain.  See https://kiln3d.com.
    """
    try:
        from kiln_pro.bridge import pro_features
    except ImportError:
        return None
    try:
        if not pro_features.is_available("edge_intelligence"):
            return None
        choose = pro_features.edge_intelligence.choose_finishes
    except Exception:  # noqa: BLE001 -- no policy is the literal finish, never a failed reinforcement
        return None
    return lambda chains, frame: choose(chains, frame, material=material, fastener=fastener)


def _print_aware_note(
    replies: dict[str, Any] | Sequence[dict[str, Any]], chosen_per_edge: bool, fastener: str | None = None,
) -> str:
    """What the edge finishing did about how the part prints, and what it did
    about the holes when a fastener was named -- one note, and the tier that
    chooses named once, with the link, only when there was something to choose.

    *replies* is the edge step's reply, or every edge step's reply; a step
    that finished nothing contributes no caution.
    """
    found = [replies] if isinstance(replies, dict) else list(replies)
    cautions = sum(len(reply["edges"]["cautions"]) for reply in found)
    if chosen_per_edge:
        said = "Each edge's finish was chosen for how it prints on this printer"
        return said + (f", and each hole for the {fastener} you named." if fastener else ".")
    if not cautions and not fastener:
        return ""
    from kiln.tiers_and_terms import upgrade_link

    parts = []
    if cautions:
        parts.append(f"{cautions} of the finished edges will print badly as asked; each entry's edges.cautions says how.")
    if fastener:
        parts.append(f"The holes are left as drawn for the {fastener} you named.")
    does = ["chooses the finish edge by edge for your printer and material"] if cautions else []
    if fastener:
        does.append(f"prepares each hole that takes the {fastener} as it rounds the corners")
    parts.append(f"Kiln Pro {' and '.join(does)}: {upgrade_link('apply_design_reinforcements')}")
    return " ".join(parts)


def _apply_thicken(
    part_path: str,
    work_dir: str,
    amount_mm: float,
) -> dict[str, Any]:
    """Thicken through the one measured door; its reply, success or not."""
    try:
        from kiln.wall_thicken import thicken_part

        return thicken_part(part_path, amount_mm=amount_mm, output_path=str(Path(work_dir) / "thickened.stl"))
    except Exception as exc:  # noqa: BLE001 -- a reinforcement that cannot run is skipped, and says why
        return {"success": False, "message": f"Wall thickening could not run: {exc}"}


def _apply_edge_finish(
    part_path: str,
    work_dir: str,
    kind: str,
    size_mm: float,
    angle_deg: float,
    printer: dict[str, Any],
    choose: Any,
) -> dict[str, Any]:
    """Round or bevel the part's sharp INSIDE edges through the one door; its reply, success or not.

    Only inside edges, the ones the structural analysis counts
    (:func:`_find_sharp_corners`): a rounded inside corner spreads the load a
    sharp one concentrates, while rounding an outside edge changes how the
    part looks and fits for no strength.  They are selected by kind, at the
    plan's threshold, rather than one by one, so the door's own rules still
    hold: the floor of a blind hole stays as it is unless named.
    """
    try:
        from kiln.edge_finish import NOTHING_FINISHED, finish_part

        reply = finish_part(
            part_path, kind=kind, size_mm=size_mm, edges="inside", angle_threshold_deg=angle_deg,
            output_path=str(Path(work_dir) / f"{kind}ed.stl"), choose=choose, **printer,
        )
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "message": f"Edge finishing could not run: {exc}"}
    if reply.get("code") == NOTHING_FINISHED and not reply.get("edges", {}).get("left_sharp"):
        verb = "round" if kind == "fillet" else "bevel"
        reply["message"] = (
            f"Kiln did not {verb} any edge: the part has no sharp inside corner. Its outside edges are left as they are."
        )
    return reply


#: How much wider than the part, on each side, a base plate reaches when no
#: tipping ratio sets its width.
_BASE_SPREAD = 1.3

#: How far inside the tipping ratio a base plate keeps the part: a tenth
#: wider than the ratio asks, so the part stands clear of the line, not on it.
_BASE_CLEARANCE = 1.1

#: Why a cantilever's gusset is listed and not placed.
_GUSSET_NOT_PLACED = (
    "Kiln does not place gussets: a rib braces an overhang only where it joins the wall the "
    "overhang hangs from, and Kiln's cantilever finding says where the overhang is, not which "
    "wall carries it."
)
_GUSSET_GUIDANCE = (
    "Add a triangular rib under each overhang in locations_mm, joined to the wall it hangs from, "
    "in the part's design; or print it with supports under the overhang."
)


def _part_shape(stl_path: str) -> tuple[int, bool]:
    """How many separate solids the mesh at *stl_path* prints as, and whether
    every piece of it is sealed: the one piece reading every door uses
    (:func:`kiln.mesh_bodies.read_triangles`)."""
    from kiln.mesh_bodies import read_triangles

    triangles, _ = _parse_stl_for_analysis(stl_path)
    if not triangles:
        return 0, False
    reading = read_triangles(triangles)
    return reading.solid_count, reading.sealed


def _base_reply(path: str | None) -> dict[str, Any]:
    """:func:`_apply_base`'s result in the shape every whole-part edit replies in."""
    if path:
        return {"success": True, "path": path}
    return {"success": False, "message": "Could not generate base (OpenSCAD not available)"}


def _apply_base(
    stl_path: str,
    work_dir: str,
    height_mm: float,
    tipping_ratio: float | None = None,
) -> str | None:
    """Add a stabilizing base plate via OpenSCAD boolean union.

    The plate reaches :data:`_BASE_SPREAD` times the part's width on each
    axis, and when the part was found to tip at *tipping_ratio* (height over
    narrowest width), wide enough that the part with its plate stands
    :data:`_BASE_CLEARANCE` inside that ratio.  (A plate a third wider than
    a 5 mm post 60 mm tall left it a tipping risk at 9.5 to 1, and added a
    sharp inside corner where the post meets the plate.)
    """
    try:
        from kiln.generation.openscad import _find_openscad, boolean_mesh_operation

        # Need OpenSCAD for boolean union
        _find_openscad()

        # Parse mesh to get bounding box
        _, vertices = _parse_stl_for_analysis(stl_path)
        if not vertices:
            return None
        bbox = _bounding_box(vertices)

        width_x = (bbox["max_x"] - bbox["min_x"]) * _BASE_SPREAD
        width_y = (bbox["max_y"] - bbox["min_y"]) * _BASE_SPREAD
        if tipping_ratio:
            standing = (bbox["max_z"] - bbox["min_z"] + height_mm) / tipping_ratio * _BASE_CLEARANCE
            width_x, width_y = max(width_x, standing), max(width_y, standing)
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


# ---------------------------------------------------------------------------
# Slicer profile inference from structural analysis
# ---------------------------------------------------------------------------


def _support_call(file_path: str, material: str) -> tuple[bool, str]:
    """Whether the part needs supports, and why, in the words every other
    door uses: :func:`kiln.printability.support_verdict`, the one answer the
    report, the pre-print check and the slice itself give."""
    from kiln.printability import judge_supports, support_verdict

    try:
        reason = support_verdict(judge_supports(file_path, material=material.lower()))
    except Exception as exc:  # noqa: BLE001 -- an unread part claims nothing
        logger.debug("support check skipped for %s: %s", file_path, exc)
        return False, "Kiln could not run its support check on this file, so supports are left off."
    if reason:
        return True, f"Prints with supports ({reason})."
    return False, "Kiln's support check finds no overhang or bridge that needs support."


def infer_print_settings(
    file_path: str,
    *,
    material: str = "PLA",
    min_cross_section_mm2: float | None = None,
    sharp_angle_threshold_deg: float | None = None,
) -> PrintSettingsRecommendation:
    """Infer optimal print settings from structural analysis.

    Bridges the gap between "this design has structural risks" and
    "here are the slicer settings that compensate."  Analyzes the mesh,
    considers the material, and returns concrete slicer parameters.

    :param file_path: Path to STL file.
    :param material: Filament material (PLA, PETG, ABS, Nylon, TPU).
    :param min_cross_section_mm2: Minimum safe cross-section area.
    :param sharp_angle_threshold_deg: How far the surface must turn inward
        across an edge, in degrees, for an inside corner to count as sharp.
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

    if plan.units_question:
        # Nothing about the part was judged, so no setting is tuned to it and
        # no reason claims anything was or was not found.
        return PrintSettingsRecommendation(
            perimeters=defaults["perimeters"],
            infill_percent=defaults["infill"],
            infill_pattern=defaults["infill_pattern"],
            layer_height_mm=defaults["layer_height"],
            support_enabled=False,
            support_reason="",
            brim_enabled=False,
            brim_reason="",
            print_orientation="",
            orientation_reason="",
            special_notes=["These are the material's default settings; nothing about the part was judged."],
            confidence="low",
            units_question=plan.units_question,
        )

    perimeters = defaults["perimeters"]
    infill = defaults["infill"]
    infill_pattern = defaults["infill_pattern"]
    layer_height = defaults["layer_height"]
    support, support_reason = _support_call(file_path, material)
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

    # Cantilever → more perimeters where it meets its support.  Whether it
    # needs supports is the one support verdict's call, made above.
    if has_cantilever:
        perimeters = max(perimeters, 3)
        if support:
            notes.append("Supports are on for the overhangs. Consider tree supports for easier removal.")

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

    # Grade-based overall adjustments; the note names what was found.
    if plan.structural_grade in ("D", "F"):
        infill = max(infill, 50)
        perimeters = max(perimeters, 4)
        notes.append(
            f"With {structural_risk_counts(plan.risks)}, using high infill "
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

    from kiln.parametric import render_template_scad

    # Load templates
    if templates_path is None:
        tpl_path = Path(__file__).parent / "data" / "design_templates.json"
    else:
        tpl_path = Path(templates_path)

    if not tpl_path.is_file():
        raise ValueError(f"Templates file not found: {tpl_path}")

    with open(tpl_path, encoding="utf-8") as fh:
        data = json.load(fh)

    tpl = data.get(template_id)
    if not tpl or template_id.startswith("_"):
        raise ValueError(f"Template {template_id!r} not found")

    params_spec = tpl.get("parameters", {})

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
        from kiln.openscad_runner import run_openscad

        openscad_binary = _find_openscad()
    except Exception as exc:
        raise ValueError(
            "OpenSCAD is required for template optimization but was not found. "
            "Install the current build from https://openscad.org/downloads#snapshots"
        ) from exc

    best_score = -1
    best_grade = "F"
    best_params: dict[str, Any] = {}
    best_stl = ""
    best_risks: list[StructuralRisk] = []
    all_scores: list[dict[str, Any]] = []

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
        scad_code = render_template_scad(tpl, params)
        scad_path = str(Path(work_dir) / f"variant_{idx}.scad")
        stl_path = str(Path(work_dir) / f"variant_{idx}.stl")

        Path(scad_path).write_text(scad_code, encoding="utf-8")

        # Compile to STL
        try:
            proc = run_openscad(
                [openscad_binary, "-o", stl_path, scad_path],
                timeout=30,
                text=False,
                output_path=stl_path,
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
            if score is None:
                # Nothing judged: Kiln drew it in millimetres, so a variant
                # this small is too small to print, and no candidate.
                continue
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
                best_risks = plan.risks
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

    # The scores pick the strongest variant (all_scores keeps them for
    # comparing); the summary says what the analysis found in it.
    summary = (
        f"Tested {len(all_scores)} variants of {tpl.get('display_name', template_id)}; "
        f"best_params is the strongest. {describe_structural_risks(best_risks)}"
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

    from kiln.mesh_frame import load_mesh  # lazy import — after validation

    # Build list of (path, width, depth, height) entries, expanding copies
    parts: list[tuple[str, float, float, float]] = []
    for fp in file_paths:
        mesh = load_mesh(fp, force="mesh")
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

    A thin shim over :func:`kiln.design_intelligence.
    score_generatable_design_templates` — the one scorer behind every
    template-search surface.  This module used to carry its own
    substring scorer over the same file, and two rankings of one
    library meant a template could match here and miss on the MCP
    discovery tools, or the reverse, with nothing to say which was
    right.  Only the envelope (scores, category filter, result cap)
    lives here.

    :param query: Natural-language search string.
    :param max_results: Cap on returned matches (default 10).
    :param category_filter: If set, only return templates in this category.
    :returns: ``TemplateSearchResult`` with scored matches.
    """
    from kiln.design_intelligence import (
        list_generatable_design_templates,
        score_generatable_design_templates,
    )

    total = len(list_generatable_design_templates())
    if not query or not query.strip():
        return TemplateSearchResult(query=query, total_templates=total)

    matches = [
        {
            "template_id": summary["template_id"],
            "score": round(score, 3),
            "description": summary["description"],
            "category": summary["category"],
        }
        for score, summary in score_generatable_design_templates(query)
        if not category_filter or summary["category"] == category_filter
    ]

    return TemplateSearchResult(
        query=query,
        matches=matches[:max_results],
        total_templates=total,
        search_method="keyword",
    )


# ---------------------------------------------------------------------------
# Loop 15: Mesh weight estimation
# ---------------------------------------------------------------------------

@dataclass
class WeightEstimate:
    """Estimated weight of a 3D-printed part."""

    file_path: str = ""
    volume_mm3: float = 0.0
    volume_cm3: float = 0.0
    material: str = "pla"
    density_g_cm3: float = field(
        default_factory=lambda: BUILTIN_MATERIALS[DEFAULT_MATERIAL].density_g_per_cm3
    )
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
    :param material: Material name, looked up in the one material table
        (:func:`kiln.cost_estimator.resolve_material`).
    :param infill_percent: Infill percentage (0-100).
    :param wall_thickness_mm: Perimeter wall thickness in mm.
    :returns: ``WeightEstimate`` dataclass.
    """
    fp = Path(file_path)
    if not fp.exists():
        raise FileNotFoundError(f"STL file not found: {file_path}")

    # Density from the one material table, PLA's when it has no row.
    row = resolve_material(material)
    notes: list[str] = []
    if row is None:
        row = BUILTIN_MATERIALS[DEFAULT_MATERIAL]
        notes.append(
            f"Unknown material '{material}', using {row.name} density ({row.density_g_per_cm3} g/cm³)."
        )
    density = row.density_g_per_cm3

    # Parse triangles
    triangles, _verts = _parse_stl_for_analysis(file_path)
    if not triangles:
        raise UnreadableMeshError(f"Could not parse triangles from {file_path}")

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
        material=material.lower().strip(),
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
    #: The plate-clearance verdict when the plate still held the last print
    #: (see ``kiln._pro_placement_bridge``); ``None`` on a clear plate.
    placement: dict[str, Any] | None = None

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
            "placement": self.placement,
        }


def design_to_gcode(
    description: str,
    *,
    output_dir: str = "",
    material: str = "PLA",
    printer_model: str = "",
    infill_percent: float = 20.0,
    placement: Any = "auto",
) -> DesignToGCodeResult:
    """End-to-end pipeline: description → template → STL → structural check → GCode.

    ``placement`` is where the part goes when the plate still holds the last
    print (``[x, y]`` in mm, a named region, or ``"keep"``); ``"auto"``, the
    default, refuses the slice step on an occupied plate -- the same gate
    ``slice_model`` runs, so the G-code this pipeline hands back is never
    sliced onto a part.

    Steps:
        1. Search templates for best match
        2. Generate STL via OpenSCAD
        3. Run structural risk analysis
        4. Estimate weight
        5. Slice to G-code (if slicer available)

    :param description: Natural-language design description.
    :param output_dir: Directory for output files (uses tempdir if empty).
    :param material: Material the slice is set for -- temperatures, melt
        rate and cooling -- and weighed as.
    :param printer_model: Printer model for slicer profile lookup.
    :param infill_percent: Infill percentage for weight estimation.
    :returns: ``DesignToGCodeResult`` with paths and metadata.
    """
    import json as _json
    import tempfile

    from kiln.parametric import render_template_scad

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
    with open(templates_path, encoding="utf-8") as fh:
        templates = _json.load(fh)

    tmpl = templates.get(template_id)
    if not tmpl or "scad_template" not in tmpl:
        result.errors.append(f"Template '{template_id}' has no scad_template.")
        return result

    # Write SCAD file.
    #
    # Templates are ``string.Template`` sources — ``pegs = ${pegs};`` —
    # so the defaults must be SUBSTITUTED, through the renderer every
    # template door shares (:func:`kiln.parametric.render_template_scad`).
    # This step used to prepend ``k = v;`` assignments instead, leaving
    # every ``${...}`` in place, and the resulting file failed OpenSCAD's
    # parser on line one of the first placeholder — no template ever
    # compiled through this pipeline.
    scad_path = out_dir / f"{template_id}.scad"
    # The schema is ``parameters: {name: {default, min, max, ...}}`` —
    # the ``default_params`` key the old code read has never existed in
    # design_templates.json, so no default ever reached a template.
    params = {
        name: spec.get("default")
        for name, spec in tmpl.get("parameters", {}).items()
        if isinstance(spec, dict) and spec.get("default") is not None
    }
    scad_code = render_template_scad(tmpl, params)
    scad_path.write_text(scad_code, encoding="utf-8")
    result.scad_file = str(scad_path)
    result.steps_completed.append("scad_generation")

    # Try to render STL via OpenSCAD.
    #
    # This step called an ``OpenSCADProvider.render`` that has never
    # existed (the provider's API is ``generate``); the AttributeError
    # was caught below and every run of this pipeline ended right here
    # with "STL rendering failed" — so the dead slicer import in Step 5
    # went unnoticed for the same reason it went unreached.
    # :func:`kiln.parametric.compile_scad_code` is the engine every
    # other SCAD door compiles through; it takes the source (which is
    # already in hand), honours the bundled libraries, and returns the
    # STL path.
    stl_path = out_dir / f"{template_id}.stl"
    try:
        from kiln.parametric import compile_scad_code

        compile_scad_code(scad_code, output_path=str(stl_path))
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
    #
    # This step imported a ``slice_stl`` that has never existed in
    # ``kiln.slicer`` (the entry point is ``slice_file``), so the
    # ImportError below fired on every run and the pipeline reported
    # "Slicer module not available" on machines with a working slicer —
    # and ``printer_model`` was never consulted at all.  The import is
    # now module-level-shaped and exercised by a test, so a renamed
    # slicer entry point fails the suite instead of a runtime fallback.
    try:
        from kiln.plugins.slicer_tools import _placed_slice
        from kiln.slicer import SlicerNotFoundError
        from kiln.slicer_profiles import resolve_slicer_profile

        profile = resolve_slicer_profile(printer_model) if printer_model else None
        # The plate may still hold the last print: the same shared step as
        # slice_model (plate gate, slice, second verdict).
        slice_result, place_err, sinfo = _placed_slice(
            str(stl_path), effective_printer_id=printer_model or None, printer_name=None,
            placement=placement, profile_path=profile,
            output_dir=str(out_dir), output_name=f"{template_id}.gcode", material=material,
        )
        if place_err is not None:
            result.errors.append(str(place_err["error"]["message"]))
            result.placement = place_err.get("placement")
        else:
            result.gcode_file = slice_result.output_path
            result.steps_completed.append("slicing")
            if sinfo["placement"].get("plate") == "occupied":
                result.placement = sinfo["placement"].get("placement")
    except SlicerNotFoundError:
        result.errors.append(
            "No slicer installed — install PrusaSlicer or OrcaSlicer to get G-code."
        )
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
        raise UnreadableMeshError(f"Could not parse triangles from {file_path}")

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
    # Degrees of freedom: numeric template parameters no constraint pinned.
    # ``equals`` pins a parameter and ``ratio`` pins the dependent one;
    # ``min``/``max`` alone bound a range without choosing a value.
    dof: int = 0
    free: list[str] = field(default_factory=list)
    defaults_applied: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "solved_params": dict(self.solved_params),
            "constraints_satisfied": list(self.constraints_satisfied),
            "constraints_violated": list(self.constraints_violated),
            "iterations": self.iterations,
            "success": self.success,
            "notes": list(self.notes),
            "dof": self.dof,
            "free": list(self.free),
            "defaults_applied": dict(self.defaults_applied),
        }


def _constraint_pins(constraint: Any, known_params: dict[str, Any]) -> bool:
    """Whether a constraint dict chooses a value rather than bounding one.

    ``equals`` pins.  ``ratio`` pins when it names a known template
    parameter (``[other_param, factor]``); a ratio to nothing applies
    nothing.  ``min``/``max`` alone do not pin — a range is not a value.
    """
    if not isinstance(constraint, dict):
        return False
    if "equals" in constraint:
        return True
    ratio_spec = constraint.get("ratio")
    if isinstance(ratio_spec, list) and len(ratio_spec) == 2:
        other = known_params.get(ratio_spec[0])
        return isinstance(other, (int, float)) and not isinstance(other, bool)
    return False


def _describe_bounds(constraint: dict[str, Any]) -> str:
    """Plain-English ``min``/``max`` summary for a bounded-but-unpinned param."""
    parts = []
    if "min" in constraint:
        parts.append(f"min {constraint['min']}")
    if "max" in constraint:
        parts.append(f"max {constraint['max']}")
    return ", ".join(parts)


def _describe_idle_ratio(constraint: dict[str, Any], known_params: dict[str, Any]) -> str:
    """Why a ``ratio`` constraint chose nothing: its other parameter is unknown or not a number."""
    ratio_spec = constraint.get("ratio")
    if not (isinstance(ratio_spec, list) and len(ratio_spec) == 2):
        return ""
    other = ratio_spec[0]
    kind = "unknown" if other not in known_params else "non-numeric"
    return f"ratio to {kind} parameter {other!r} applied nothing"


def _plain_number(val: float) -> str:
    """Render a value the way a person says it: ``15.0`` is ``15``, ``2.5`` stays."""
    return str(int(val)) if float(val).is_integer() else str(val)


def _free_sentence(
    pname: str,
    val: float,
    default: Any,
    constraint: Any,
    known_params: dict[str, Any],
) -> str:
    """One plain sentence for a numeric parameter no constraint chose a value for."""
    shown = _plain_number(val)
    bounds = _describe_bounds(constraint) if isinstance(constraint, dict) else ""
    idle_ratio = (
        _describe_idle_ratio(constraint, known_params)
        if isinstance(constraint, dict) else ""
    )
    if bounds:
        what = f"bounded ({bounds}) but no value chosen"
    elif idle_ratio:
        what = idle_ratio
    else:
        return f"{pname}: not constrained, left at the template default {shown}"
    moved = (
        isinstance(default, (int, float))
        and not isinstance(default, bool)
        and abs(float(default) - float(val)) > 0.01
    )
    if moved:
        return (
            f"{pname}: {what}; the bound moved it from the template default "
            f"{_plain_number(default)} to {shown}"
        )
    return f"{pname}: {what}, left at the template default {shown}"


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
    :returns: ``ConstraintSolution`` with solved parameters.  ``dof`` counts
        the numeric template parameters no constraint pinned, ``free``
        names each one with the value it was left at, and
        ``defaults_applied`` maps those names to the values used.
    """
    import json as _json

    result = ConstraintSolution(template_id=template_id)

    templates_path = Path(__file__).parent / "data" / "design_templates.json"
    if not templates_path.exists():
        result.notes.append("Templates file not found.")
        return result

    with open(templates_path, encoding="utf-8") as fh:
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
    defaults: dict[str, Any] = dict(solved)

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

    # Degrees of freedom: which numeric parameters did nothing pin?
    for pname, val in result.solved_params.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        constraint = constraints.get(pname)
        if _constraint_pins(constraint, solved):
            continue
        result.defaults_applied[pname] = val
        result.free.append(
            _free_sentence(pname, val, defaults.get(pname), constraint, solved)
        )
    result.dof = len(result.defaults_applied)

    return result
