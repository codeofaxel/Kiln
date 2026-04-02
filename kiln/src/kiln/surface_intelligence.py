"""Surface intelligence for STL mesh analysis.

Analyzes STL meshes to find the best face for embossing, engraving, or
decoration.  Used by the ``decorate_surface`` MCP tool.

Only uses Python stdlib (math) plus Kiln's existing STL parser from
``generation.validation`` — no numpy or trimesh.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------

def _vec_sub(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vec_add(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, float, float]:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _vec_scale(v: tuple[float, ...], s: float) -> tuple[float, float, float]:
    return (v[0] * s, v[1] * s, v[2] * s)


def _vec_dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vec_cross(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _vec_length(v: tuple[float, ...]) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _vec_normalize(v: tuple[float, ...]) -> tuple[float, float, float]:
    length = _vec_length(v)
    if length < 1e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / length, v[1] / length, v[2] / length)


# ---------------------------------------------------------------------------
# STL parsing — delegates to generation.validation (single source of truth)
# ---------------------------------------------------------------------------

def _parse_stl(path: str) -> list[dict[str, Any]]:
    """Parse an STL file and return a list of triangle dicts.

    Each dict has:
        normal: (nx, ny, nz)  — recomputed from vertices for reliability
        vertices: [(x,y,z), (x,y,z), (x,y,z)]

    Uses Kiln's canonical STL parser from generation.validation rather
    than maintaining a separate copy.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"STL file not found: {path}")

    from kiln.generation.validation import _parse_stl as _parse_stl_raw

    errors: list[str] = []
    raw_tris, _ = _parse_stl_raw(p, errors)
    if errors:
        raise ValueError(f"STL parse error: {'; '.join(errors)}")

    # Convert (v1, v2, v3) tuples to dicts with computed normals
    result: list[dict[str, Any]] = []
    for tri in raw_tris:
        v1, v2, v3 = tri
        normal = _compute_normal(v1, v2, v3)
        result.append({"normal": normal, "vertices": [v1, v2, v3]})
    return result


# ---------------------------------------------------------------------------
# Triangle area computation
# ---------------------------------------------------------------------------

def _triangle_area(v1: tuple[float, ...], v2: tuple[float, ...], v3: tuple[float, ...]) -> float:
    """Compute the area of a triangle given its three vertices."""
    edge1 = _vec_sub(v2, v1)
    edge2 = _vec_sub(v3, v1)
    cross = _vec_cross(edge1, edge2)
    return 0.5 * _vec_length(cross)


def _compute_normal(v1: tuple[float, ...], v2: tuple[float, ...], v3: tuple[float, ...]) -> tuple[float, float, float]:
    """Compute the unit normal of a triangle from its vertices."""
    edge1 = _vec_sub(v2, v1)
    edge2 = _vec_sub(v3, v1)
    cross = _vec_cross(edge1, edge2)
    return _vec_normalize(cross)


# ---------------------------------------------------------------------------
# Face naming from normal direction
# ---------------------------------------------------------------------------

_FACE_NAMES: list[tuple[str, tuple[float, float, float]]] = [
    ("top", (0.0, 0.0, 1.0)),
    ("bottom", (0.0, 0.0, -1.0)),
    ("front", (0.0, -1.0, 0.0)),
    ("back", (0.0, 1.0, 0.0)),
    ("right", (1.0, 0.0, 0.0)),
    ("left", (-1.0, 0.0, 0.0)),
]


def _name_from_normal(normal: tuple[float, ...]) -> str:
    """Return a human-readable face name based on the normal direction.

    Uses 0.9 threshold on the dominant axis component for cardinal faces.
    Falls back to closest cardinal direction otherwise.
    """
    nx, ny, nz = normal

    # Strict threshold for unambiguous cardinal faces
    if nz > 0.9:
        return "top"
    if nz < -0.9:
        return "bottom"
    if ny < -0.9:
        return "front"
    if ny > 0.9:
        return "back"
    if nx > 0.9:
        return "right"
    if nx < -0.9:
        return "left"

    # Fallback: closest cardinal direction by dot product
    best_name = "top"
    best_dot = -2.0
    for name, direction in _FACE_NAMES:
        d = _vec_dot(normal, direction)
        if d > best_dot:
            best_dot = d
            best_name = name
    return best_name


# ---------------------------------------------------------------------------
# Face grouping
# ---------------------------------------------------------------------------

def _group_triangles_by_normal(
    triangles: list[dict[str, Any]],
    tolerance_deg: float,
) -> list[dict[str, Any]]:
    """Group triangles by face normal within angular tolerance.

    Returns a list of face-group dicts, each with:
        normal: average unit normal of the group
        triangles: list of triangle dicts in this group
        area_mm2: total area of all triangles
    """
    cos_tol = math.cos(math.radians(tolerance_deg))
    groups: list[dict[str, Any]] = []

    for tri in triangles:
        # Recompute normal from vertices for reliability
        v1, v2, v3 = tri["vertices"]
        normal = _compute_normal(v1, v2, v3)
        if _vec_length(normal) < 1e-9:
            continue  # degenerate triangle

        area = _triangle_area(v1, v2, v3)
        if area < 1e-9:
            continue

        # Try to find an existing group with matching normal
        matched = False
        for group in groups:
            if _vec_dot(group["normal"], normal) >= cos_tol:
                group["triangles"].append(tri)
                group["area_mm2"] += area
                # Update running normal average (area-weighted)
                group["_weighted_normal"] = _vec_add(
                    group["_weighted_normal"], _vec_scale(normal, area)
                )
                group["normal"] = _vec_normalize(group["_weighted_normal"])
                matched = True
                break

        if not matched:
            groups.append({
                "normal": normal,
                "_weighted_normal": _vec_scale(normal, area),
                "triangles": [tri],
                "area_mm2": area,
            })

    return groups


# ---------------------------------------------------------------------------
# Face bounding box in local 2D coordinate system
# ---------------------------------------------------------------------------

def _build_face_axes(normal: tuple[float, ...]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Build orthonormal x_axis and y_axis on the plane defined by *normal*.

    x_axis is roughly aligned with the world X or Y axis (whichever is less
    parallel to the normal), and y_axis = normal x x_axis.
    """
    nx, ny, nz = normal
    # Pick a reference vector not parallel to the normal
    if abs(nx) < 0.9:
        ref = (1.0, 0.0, 0.0)
    else:
        ref = (0.0, 1.0, 0.0)

    # x_axis = normalize(ref - (ref . normal) * normal)  (Gram-Schmidt)
    proj = _vec_dot(ref, normal)
    x_raw = _vec_sub(ref, _vec_scale(normal, proj))
    x_axis = _vec_normalize(x_raw)

    # y_axis = normal x x_axis
    y_axis = _vec_normalize(_vec_cross(normal, x_axis))
    return x_axis, y_axis  # type: ignore[return-value]


def _face_bounds(
    group: dict[str, Any],
    x_axis: tuple[float, ...],
    y_axis: tuple[float, ...],
) -> tuple[float, float, float, float]:
    """Project all vertices in *group* onto the 2D face plane.

    Returns (u_min, u_max, v_min, v_max) in millimetres.
    """
    u_min = float("inf")
    u_max = float("-inf")
    v_min = float("inf")
    v_max = float("-inf")

    for tri in group["triangles"]:
        for vert in tri["vertices"]:
            u = _vec_dot(vert, x_axis)
            v = _vec_dot(vert, y_axis)
            if u < u_min:
                u_min = u
            if u > u_max:
                u_max = u
            if v < v_min:
                v_min = v
            if v > v_max:
                v_max = v

    return u_min, u_max, v_min, v_max


def _face_centroid(group: dict[str, Any]) -> tuple[float, float, float]:
    """Compute the area-weighted centroid of all triangles in the group."""
    cx, cy, cz = 0.0, 0.0, 0.0
    total_area = 0.0

    for tri in group["triangles"]:
        v1, v2, v3 = tri["vertices"]
        area = _triangle_area(v1, v2, v3)
        mid = (
            (v1[0] + v2[0] + v3[0]) / 3.0,
            (v1[1] + v2[1] + v3[1]) / 3.0,
            (v1[2] + v2[2] + v3[2]) / 3.0,
        )
        cx += mid[0] * area
        cy += mid[1] * area
        cz += mid[2] * area
        total_area += area

    if total_area < 1e-12:
        return (0.0, 0.0, 0.0)
    return (cx / total_area, cy / total_area, cz / total_area)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _build_face_dict(group: dict[str, Any]) -> dict[str, Any]:
    """Convert an internal face-group into the public return dict."""
    normal = group["normal"]
    x_axis, y_axis = _build_face_axes(normal)
    u_min, u_max, v_min, v_max = _face_bounds(group, x_axis, y_axis)
    center = _face_centroid(group)

    return {
        "normal": (round(normal[0], 6), round(normal[1], 6), round(normal[2], 6)),
        "center": (round(center[0], 4), round(center[1], 4), round(center[2], 4)),
        "width_mm": round(u_max - u_min, 4),
        "height_mm": round(v_max - v_min, 4),
        "area_mm2": round(group["area_mm2"], 4),
        "face_name": _name_from_normal(normal),
        "triangles": len(group["triangles"]),
    }


def find_largest_flat_face(stl_path: str, tolerance_deg: float = 10.0) -> dict[str, Any]:
    """Find the largest coplanar face group in an STL mesh.

    Reads a binary or ASCII STL file, groups triangles whose normals are
    within *tolerance_deg* of each other, and returns information about the
    largest group (by total area).

    Args:
        stl_path: Path to the STL file.
        tolerance_deg: Maximum angular deviation (degrees) for two normals
            to be considered part of the same face.  Default 10.0.

    Returns:
        A dict with keys: ``normal``, ``center``, ``width_mm``,
        ``height_mm``, ``area_mm2``, ``face_name``, ``triangles``.

    Raises:
        FileNotFoundError: If the STL file does not exist.
        ValueError: If the mesh contains no valid triangles.
    """
    tris = _parse_stl(stl_path)
    if not tris:
        raise ValueError(f"No valid triangles found in {stl_path}")

    groups = _group_triangles_by_normal(tris, tolerance_deg)
    if not groups:
        raise ValueError(f"No valid face groups found in {stl_path}")

    # Among faces of similar area (within 5%), prefer top > front > sides
    # because users almost always want to decorate the top/visible face.
    _FACE_PREFERENCE = {"top": 0, "front": 1, "right": 2, "left": 3, "back": 4, "bottom": 5}
    max_area = max(g["area_mm2"] for g in groups)
    candidates = [g for g in groups if g["area_mm2"] >= max_area * 0.95]
    if len(candidates) > 1:
        candidates.sort(key=lambda g: _FACE_PREFERENCE.get(
            _name_from_normal(g["normal"]), 6
        ))
    return _build_face_dict(candidates[0])


def find_named_face(stl_path: str, face_name: str) -> dict[str, Any]:
    """Find a specific face by name in an STL mesh.

    Groups triangles by normal direction and returns the largest group
    whose cardinal name matches *face_name*.

    Args:
        stl_path: Path to the STL file.
        face_name: One of ``"top"``, ``"bottom"``, ``"front"``, ``"back"``,
            ``"left"``, ``"right"``.

    Returns:
        A dict with the same keys as :func:`find_largest_flat_face`.

    Raises:
        FileNotFoundError: If the STL file does not exist.
        ValueError: If no face matching *face_name* is found.
    """
    valid_names = {"top", "bottom", "front", "back", "left", "right"}
    face_name_lower = face_name.lower().strip()
    if face_name_lower not in valid_names:
        raise ValueError(
            f"Invalid face name {face_name!r}. Must be one of: {', '.join(sorted(valid_names))}"
        )

    tris = _parse_stl(stl_path)
    if not tris:
        raise ValueError(f"No valid triangles found in {stl_path}")

    groups = _group_triangles_by_normal(tris, tolerance_deg=10.0)

    # Find groups matching the requested face name, pick the largest
    matching = [g for g in groups if _name_from_normal(g["normal"]) == face_name_lower]
    if not matching:
        available = sorted({_name_from_normal(g["normal"]) for g in groups})
        raise ValueError(
            f"No {face_name!r} face found. Available faces: {', '.join(available)}"
        )

    largest = max(matching, key=lambda g: g["area_mm2"])
    return _build_face_dict(largest)


def compute_face_transform(face: dict[str, Any]) -> dict[str, Any]:
    """Compute a 2D coordinate system for placing content on a face.

    Given a face dict (as returned by :func:`find_largest_flat_face` or
    :func:`find_named_face`), returns the 3D origin, axis vectors, and
    dimensions needed to map 2D coordinates onto the face surface.

    Args:
        face: A face dict with at least ``normal``, ``center``,
            ``width_mm``, and ``height_mm``.

    Returns:
        A dict with keys:

        - ``origin``: 3D point at the bottom-left corner of the face bbox.
        - ``x_axis``: unit vector along the face width.
        - ``y_axis``: unit vector along the face height.
        - ``normal``: the face normal (extrusion direction).
        - ``width_mm``: face width.
        - ``height_mm``: face height.
    """
    normal = face["normal"]
    center = face["center"]
    width = face["width_mm"]
    height = face["height_mm"]

    x_axis, y_axis = _build_face_axes(normal)

    # Origin = center - (width/2)*x_axis - (height/2)*y_axis
    origin = (
        center[0] - (width / 2.0) * x_axis[0] - (height / 2.0) * y_axis[0],
        center[1] - (width / 2.0) * x_axis[1] - (height / 2.0) * y_axis[1],
        center[2] - (width / 2.0) * x_axis[2] - (height / 2.0) * y_axis[2],
    )

    return {
        "origin": (round(origin[0], 4), round(origin[1], 4), round(origin[2], 4)),
        "x_axis": x_axis,
        "y_axis": y_axis,
        "normal": normal,
        "width_mm": width,
        "height_mm": height,
    }
