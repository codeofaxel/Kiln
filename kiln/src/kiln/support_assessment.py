"""Support feasibility assessment for FDM 3D printing.

Analyses mesh geometry to determine whether a model needs supports,
how difficult they would be to remove, and what type of support
strategy is optimal for the material.

Includes overhang detection, bridge estimation, sealed hollow detection
(from how the part's closed shells nest), orientation suggestions, and
material-specific support recommendations.

Usage::

    from kiln.support_assessment import assess_support_feasibility

    result = assess_support_feasibility(stl_path="/tmp/model.stl", material="PETG")
    print(result.needs_supports)
    print(result.removal_difficulty)
"""

from __future__ import annotations

import contextlib
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

# Asking whether a hollow lies inside a shell costs one look at every
# outward shell's bounding box and, for each box it fits in, one ray against
# every triangle of that shell.  Both are charged here, one unit per pair
# looked at and one per triangle tested, so a part made of thousands of
# pieces cannot turn the question into shells squared of work: past the
# budget the hollows not reached are left unchecked, and the assessment says
# so.  It is about 125 hollows inside a 400,000-triangle body, or 7,000
# inward shells against 7,000 outward ones that never nest.
_MAX_CONTAINMENT_TESTS = 50_000_000

# The direction of that ray.  Any direction answers "inside or outside",
# so this one leans off every axis: a ray along an axis can run straight
# down the edges of a box's or a cylinder's triangles and count one
# crossing twice, or not at all.
_PROBE_RAY = (0.2588, 0.1736, 0.9502)

# Thresholds
_TALL_SUPPORT_MM = 50.0  # supports taller than this are stability risks
_BRIDGE_NEAR_VERTICAL_DEG = 80.0  # normals > this are near-horizontal faces
_CLUSTER_PROXIMITY_MM = 5.0  # XY proximity for clustering overhang triangles
_ORIENTATION_IMPROVEMENT_THRESHOLD = 0.30  # 30% improvement required to suggest

# Default profile for unknown materials (PLA-like, conservative)
_DEFAULT_PROFILE: dict[str, Any] = {
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
        with open(_DATA_FILE, encoding="utf-8") as fh:
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
    accessible: bool  # False when the region lies on a sealed hollow's shell
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
    """Complete support feasibility report.

    ``trapped_regions`` holds the support regions whose triangles lie on the
    shell of a sealed hollow: a closed shell facing inward, inside a closed
    shell facing outward.  Supports printed there can never be removed.
    Until 2026-10-24 it held the regions where a point 0.1 mm under the
    overhang read odd/odd under an up and a down ray, which is the test for
    a point inside plastic: it missed real hollows and flagged open parts
    with a few triangles wound the wrong way.  The key and its shape are
    unchanged; ``removal_difficulty`` is still ``"impossible"`` when it is
    not empty.
    """

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


def _shared_verdict(
    tris: list[_Triangle],
    stl_path: str | None,
    *,
    material: str,
    nozzle_diameter_mm: float,
    layer_height_mm: float,
) -> tuple[bool, str | None, str | None]:
    """``(needs_supports, reason, not_judged)`` read off the printability
    engine -- the same verdict every other door gives
    (:func:`kiln.printability.support_verdict`), with its reason -- and,
    for faces that are not a solid (flat, degenerate, nothing to print),
    ``False`` with the engine's own sentence to put in the warnings.

    Until 2026-10-19 this module answered from its own rule (more than 5 %
    of the surface area overhanging), which called a desk stand's 70 mm
    cantilevered arm "no supports" while the report beside it said the
    opposite.  Triangles handed in without a file are written to one for
    the engine, and the file is removed after.
    """
    import os
    import tempfile

    from kiln.printability import judge_supports, support_verdict

    path = stl_path
    scratch: str | None = None
    if path is None:
        from kiln.auto_orient import _write_binary_stl

        fd, scratch = tempfile.mkstemp(suffix=".stl", prefix="kiln_support_")
        os.close(fd)
        _write_binary_stl(tris, scratch)
        path = scratch
    try:
        judged = judge_supports(
            path,
            material=material.lower(),
            nozzle_diameter=nozzle_diameter_mm,
            layer_height=layer_height_mm,
        )
    except ValueError as exc:
        return False, None, f"Whether supports are needed was not judged: {exc}"
    except MemoryError:
        # The analysis runner's cap: more memory than this machine gives a
        # single analysis.  Not judged, said -- never a quiet "no".
        return False, None, "Whether supports are needed was not judged: the part is too large for this machine's analysis"
    finally:
        if scratch is not None:
            with contextlib.suppress(OSError):
                os.unlink(scratch)
    reason = support_verdict(judged)
    return reason is not None, reason, None


def _bridge_part(reason: str | None) -> str | None:
    """The bridge clause of a verdict's reason (``"bridges=52.6mm"``), if any."""
    if not reason:
        return None
    parts = [part.strip() for part in reason.split(",") if part.strip().startswith("bridges=")]
    return parts[0] if parts else None


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
# Sealed hollows: how the part's closed shells nest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Shells:
    """What the part's closed shells say about sealed hollows.

    ``on_hollow[i]`` is True when triangle *i* lies on the shell of a sealed
    hollow.  ``misfaced`` counts triangles wound the opposite way to the
    surface around them.  ``unchecked`` counts closed shells that face
    inward but were never asked whether they lie inside the part, because
    the containment budget ran out.
    """

    on_hollow: Any  # numpy bool array, one entry per triangle
    misfaced: int
    unchecked: int


def _crossings(point: Any, triangles: Any) -> int:
    """How many of *triangles* (an ``(m, 3, 3)`` array) a ray from *point*
    crosses, cast along :data:`_PROBE_RAY`.  Odd means the point is inside
    the closed surface those triangles make, whichever way they are wound.
    """
    import numpy as np

    ray = np.asarray(_PROBE_RAY) / np.linalg.norm(_PROBE_RAY)
    origin = triangles[:, 0]
    edge1 = triangles[:, 1] - origin
    edge2 = triangles[:, 2] - origin
    h = np.cross(ray, edge2)
    det = np.einsum("ij,ij->i", edge1, h)
    usable = np.abs(det) > 1e-12  # a triangle edge-on to the ray is never crossed
    inv = np.divide(1.0, det, out=np.zeros_like(det), where=usable)
    s = point - origin
    u = inv * np.einsum("ij,ij->i", s, h)
    q = np.cross(s, edge1)
    v = inv * (q @ ray)
    t = inv * np.einsum("ij,ij->i", edge2, q)
    hit = usable & (u >= 0.0) & (u <= 1.0) & (v >= 0.0) & (u + v <= 1.0) & (t > 1e-9)
    return int(np.count_nonzero(hit))


def _read_shells(tris: list[_Triangle]) -> _Shells:
    """Split the part into closed shells and find the sealed hollows.

    A shell is the triangles joined edge to edge.  Its winding is read the
    way most of its surface area faces, so a few triangles wound backwards
    cannot turn the whole shell inside out, and they are counted as a mesh
    fault instead.  A closed shell that then faces inward (its signed volume
    is negative) and lies inside a closed shell that faces outward is a
    sealed hollow: the plastic wraps all the way round it.  An open part,
    however it is wound, has nothing around it, so it is never one.

    Everything here is whole-array numpy work; nothing walks the triangles
    one at a time.
    """
    import itertools

    import numpy as np

    count = len(tris)
    flat = itertools.chain.from_iterable(itertools.chain.from_iterable(tris))
    corners = np.fromiter(flat, dtype=np.float64, count=9 * count).reshape(count, 3, 3)

    # Weld corners that are the same point.  An STL stores each corner in
    # every triangle that uses it, as the same 32-bit floats, so their bit
    # patterns are the key (adding 0.0 turns -0.0 into 0.0 first).
    bits = (corners.reshape(-1, 3).astype(np.float32) + np.float32(0.0)).view(np.uint32).astype(np.uint64)
    xy = (bits[:, 0] << np.uint64(32)) | bits[:, 1]
    order = np.lexsort((bits[:, 2], xy))
    new_point = np.ones(len(order), dtype=bool)
    new_point[1:] = (xy[order][1:] != xy[order][:-1]) | (bits[order, 2][1:] != bits[order, 2][:-1])
    point_id = np.empty(len(order), dtype=np.int64)
    point_id[order] = np.cumsum(new_point) - 1
    faces = point_id.reshape(count, 3)
    real = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])

    # Edges two triangles share.  An edge used once is an opening; one used
    # three or more times joins nothing here.
    face_of_edge = np.repeat(np.nonzero(real)[0], 3)
    edges = faces[real][:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    key = np.minimum(edges[:, 0], edges[:, 1]) * (int(point_id.max()) + 1) + np.maximum(edges[:, 0], edges[:, 1])
    by_key = np.argsort(key)
    sorted_key = key[by_key]
    run_start = np.ones(len(sorted_key), dtype=bool)
    run_start[1:] = sorted_key[1:] != sorted_key[:-1]
    starts = np.nonzero(run_start)[0]
    run_length = np.diff(np.append(starts, len(sorted_key)))
    twins = starts[run_length == 2]
    first, second = by_key[twins], by_key[twins + 1]
    face_a, face_b = face_of_edge[first], face_of_edge[second]
    # Two neighbours wound alike traverse their shared edge in opposite
    # directions; the same direction means one of them is the wrong way round.
    unlike = edges[first, 0] == edges[second, 0]
    shared = np.zeros(len(edges), dtype=bool)
    shared[first] = shared[second] = True
    closed_face = np.zeros(count, dtype=bool)
    closed_face[real] = shared.reshape(-1, 3).all(axis=1)

    # Join neighbours into shells, keeping for each triangle whether it is
    # wound like its shell's root ("flip" False) or opposite to it.  Each
    # pass hangs every root that touches a lower root under it, then follows
    # parents to the root, so a shell of any size settles in a few passes.
    parent = np.arange(count)
    flip = np.zeros(count, dtype=bool)
    while True:
        while True:
            grand = parent[parent]
            if np.array_equal(grand, parent):
                break
            flip = flip ^ flip[parent]
            parent = grand
        root_a, root_b = parent[face_a], parent[face_b]
        apart = root_a != root_b
        if not apart.any():
            break
        relation = (flip[face_a] ^ flip[face_b] ^ unlike)[apart]
        high = np.maximum(root_a[apart], root_b[apart])
        low = np.minimum(root_a[apart], root_b[apart])
        hung, pick = np.unique(high, return_index=True)
        parent[hung] = low[pick]
        flip[hung] = relation[pick]
    shell = parent
    # A shell whose windings cannot all be made to agree (a one-sided
    # surface, or edges glued crosswise) has no inside to speak of.
    clash = (flip[face_a] ^ flip[face_b]) != unlike
    tangled = np.zeros(count, dtype=bool)
    tangled[shell[face_a[clash]]] = True

    v0, v1, v2 = corners[:, 0], corners[:, 1], corners[:, 2]
    area = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1) * real
    flipped_area = np.bincount(shell, weights=area * flip, minlength=count)
    shell_area = np.bincount(shell, weights=area, minlength=count)
    # The winding most of the shell's surface has is the one it means.
    meant = flipped_area > shell_area - flipped_area
    against = (flip != meant[shell]) & real & ~tangled[shell]
    signed = np.einsum("ij,ij->i", v0, np.cross(v1, v2)) / 6.0
    volume = np.bincount(shell, weights=np.where(against, -signed, signed) * real, minlength=count)

    open_faces = np.bincount(shell, weights=(~closed_face).astype(np.float64), minlength=count)
    size = np.bincount(shell, minlength=count)
    closed = (size >= 4) & (open_faces == 0) & ~tangled
    hollows = np.nonzero(closed & (volume < 0))[0]
    bodies = np.nonzero(closed & (volume > 0))[0]

    on_hollow = np.zeros(count, dtype=bool)
    unchecked = 0
    if len(hollows) and len(bodies):
        members_order = np.argsort(shell, kind="stable")
        first_member = np.searchsorted(shell[members_order], np.arange(count))
        low_corner = np.full((count, 3), np.inf)
        high_corner = np.full((count, 3), -np.inf)
        np.minimum.at(low_corner, shell, corners.min(axis=1))
        np.maximum.at(high_corner, shell, corners.max(axis=1))

        def members(root: int) -> Any:
            return members_order[first_member[root]:first_member[root] + size[root]]

        sealed: list[int] = []
        budget = _MAX_CONTAINMENT_TESTS
        body_low, body_high = low_corner[bodies], high_corner[bodies]
        # The largest hollows first, so a budget that runs out spends itself
        # on the voids that matter most to a print.
        for hollow in hollows[np.argsort(volume[hollows], kind="stable")]:
            if budget <= 0:
                unchecked += 1
                continue
            # Every outward shell is looked at once, as one array: only one
            # whose box holds the hollow's box can hold the hollow.
            budget -= len(bodies)
            fits = (body_low <= low_corner[hollow]).all(axis=1) & (body_high >= high_corner[hollow]).all(axis=1)
            if not fits.any():
                continue
            own = members(hollow)
            # A point on the hollow's largest triangle, which lies on no
            # other shell unless the two touch.
            probe = corners[own[np.argmax(area[own])]].mean(axis=0)
            for body in bodies[fits]:
                around = members(body)
                budget -= len(around)
                if _crossings(probe, corners[around]) % 2 == 1:
                    sealed.append(int(hollow))
                    break
        on_hollow = np.isin(shell, sealed) & real

    return _Shells(on_hollow=on_hollow, misfaced=int(against.sum()), unchecked=unchecked)


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
    # Supports inside a sealed hollow can never come out.
    if trapped_regions:
        return (
            "impossible",
            f"{len(trapped_regions)} support region(s) inside a sealed hollow: supports "
            f"printed there can't be removed and stay inside the part. "
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


def _region(cluster: list[tuple[_Triangle, float, float]], *, accessible: bool = True) -> OverhangRegion:
    """One support region from a cluster of (triangle, angle, area) overhangs."""
    centroids = [_triangle_centroid(tri) for tri, _, _ in cluster]
    xs = [c[0] for c in centroids]
    ys = [c[1] for c in centroids]
    return OverhangRegion(
        centroid=(
            sum(xs) / len(centroids),
            sum(ys) / len(centroids),
            sum(c[2] for c in centroids) / len(centroids),
        ),
        area_mm2=sum(ar for _, _, ar in cluster),
        max_angle_deg=max(ang for _, ang, _ in cluster),
        # Bridge width: the cluster's extent in XY.
        estimated_bridge_mm=max(max(xs) - min(xs), max(ys) - min(ys)),
        # Support height: the lowest point of the cluster.
        support_height_mm=max(0.0, min(c[2] for c in centroids)),
        accessible=accessible,
        triangle_count=len(cluster),
    )


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
    needs_supports: bool | None = None,
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
    :param needs_supports: The verdict already made from the part's
        printability report (:func:`kiln.printability.support_verdict`),
        when the caller has one; read off that same engine here otherwise.
        This module measures the overhang AREA and what removing supports
        would take; whether the part needs them is one answer Kiln gives
        everywhere, never a second threshold here.
    :returns: Complete support assessment report.
    """
    profile = _get_profile(material)

    # The one overhang angle Kiln judges this material against -- the
    # printability report's, the slice's (:func:`kiln.printability.overhang_threshold_deg`).
    # This table carried its own copy (PLA 55, PETG 45, ...) beside the
    # engine's 45 and the overlay's per-material angle, so the area it
    # reported as overhanging and the verdict it was handed could disagree
    # about which faces counted.
    from kiln.printability import overhang_threshold_deg

    safe_deg = overhang_threshold_deg(material)

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

    warnings: list[str] = []

    # Which triangles lie on a sealed hollow, read once from the shells.
    on_hollow: Any = None
    try:
        shells = _read_shells(tris)
    except Exception as exc:  # noqa: BLE001 -- the rest of the assessment stands without it
        logger.debug("Sealed hollow check failed", exc_info=True)
        warnings.append(f"Sealed hollows were not looked for: {exc}")
    else:
        on_hollow = shells.on_hollow
        if shells.misfaced:
            noun = "triangle" if shells.misfaced == 1 else "triangles"
            warnings.append(
                f"Mesh defect: {shells.misfaced} {noun} face the opposite way to the "
                f"surface around them (inconsistent winding). They do not make the part "
                f"a sealed hollow; repairing the mesh fixes the file itself."
            )
        if shells.unchecked:
            warnings.append(
                f"{shells.unchecked} inward-facing shell(s) were not checked for a sealed "
                f"hollow: the part has too many for one assessment."
            )

    # ---------------------------------------------------------------
    # a) Overhang analysis
    # ---------------------------------------------------------------
    total_area = 0.0
    safe_area = 0.0
    overhang_area = 0.0

    # (triangle, angle_deg, area_mm2) for overhangs
    overhang_tris: list[tuple[_Triangle, float, float]] = []
    # The same overhangs, split by whether they lie on a sealed hollow, so
    # a hollow's ceiling and an outside overhang above or below it are never
    # one region.
    open_overhangs: list[tuple[_Triangle, float, float]] = []
    sealed_overhangs: list[tuple[_Triangle, float, float]] = []

    # The plate the model rests on — its own lowest point, not z=0.
    bed_z = _mesh_bed_z(tris)

    for index, tri in enumerate(tris):
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
        else:
            overhang_area += area
            overhang_tris.append((tri, angle, area))
            if on_hollow is not None and on_hollow[index]:
                sealed_overhangs.append((tri, angle, area))
            else:
                open_overhangs.append((tri, angle, area))

    total_overhang_area = overhang_area
    overhang_pct = (total_overhang_area / total_area * 100.0) if total_area > 0 else 0.0

    # ---------------------------------------------------------------
    # b) Bridge detection — cluster near-horizontal overhangs
    # ---------------------------------------------------------------
    bridge_tris = [
        (tri, ang, ar)
        for tri, ang, ar in overhang_tris
        if ang > _BRIDGE_NEAR_VERTICAL_DEG
    ]
    bridge_regions = [_region(cluster) for cluster in _cluster_overhang_triangles(bridge_tris) if cluster]

    # ---------------------------------------------------------------
    # c) Support regions, and the ones sealed inside a hollow
    # ---------------------------------------------------------------
    all_regions = [_region(cluster) for cluster in _cluster_overhang_triangles(open_overhangs) if cluster]
    trapped_regions = [
        _region(cluster, accessible=False)
        for cluster in _cluster_overhang_triangles(sealed_overhangs)
        if cluster
    ]
    all_regions.extend(trapped_regions)

    tall_regions = [r for r in all_regions if r.support_height_mm > _TALL_SUPPORT_MM]
    if tall_regions:
        warnings.append(
            f"{len(tall_regions)} overhang region(s) need supports taller than "
            f"{_TALL_SUPPORT_MM:.0f}mm — stability risk"
        )

    # ---------------------------------------------------------------
    # d) Removal difficulty
    # ---------------------------------------------------------------
    difficulty, removal_notes = _assess_removal_difficulty(
        profile,
        total_overhang_area,
        total_area,
        trapped_regions,
        all_regions,
    )

    # ---------------------------------------------------------------
    # e) Orientation suggestions
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
    # f) Support type recommendation
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
    bridge_reason: str | None = None
    if needs_supports is None:
        needs_supports, verdict_reason, not_judged = _shared_verdict(
            tris, stl_path, material=material,
            nozzle_diameter_mm=nozzle_diameter_mm, layer_height_mm=layer_height_mm,
        )
        if not_judged:
            warnings.append(not_judged)
        bridge_reason = _bridge_part(verdict_reason)

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
        if bridge_reason:
            # The verdict's own bridge limit (kiln.printability), never a
            # second one kept here: this table said PLA bridges 50 mm while
            # the engine refused 30.
            recommendations.append(f"Supports required under the bridge: {bridge_reason}")
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
