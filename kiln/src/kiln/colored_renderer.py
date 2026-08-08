"""PIL-based colored mesh renderer using painter's algorithm.

Renders colored triangles (with per-face colors) to PNG images.
Uses pure Python math with PIL as the only external dependency.
Supports supersampling anti-aliasing, directional lighting,
back-face culling, and multiple camera angle presets.
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kiln._vec import Vec3 as _Vec3
from kiln._vec import cross as _cross
from kiln._vec import dot as _dot
from kiln._vec import face_normal as _face_normal
from kiln._vec import normalize as _normalize
from kiln._vec import sub as _sub

if TYPE_CHECKING:
    from kiln.threemf_parser import ColoredTriangle

# ---------------------------------------------------------------------------
# Camera angle presets — converted from OpenSCAD (rotX, rotY, rotZ) to
# our renderer's (elevation, azimuth) system.
#
# Mapping: elevation = 90 - rotX, azimuth = rotZ
# ---------------------------------------------------------------------------

_CAMERA_ANGLES: dict[str, dict[str, Any]] = {
    "isometric": {
        "elevation": 35.0,
        "azimuth": 25.0,
        "description": "Isometric 3/4 view",
    },
    "front": {
        "elevation": 0.0,
        "azimuth": 0.0,
        "description": "Front view",
    },
    "right": {
        "elevation": 0.0,
        "azimuth": 90.0,
        "description": "Right side view",
    },
    "top": {
        "elevation": 75.0,
        "azimuth": 10.0,
        "description": "Top-down view (15 deg tilt for detail)",
    },
    "bottom": {
        "elevation": -80.0,
        "azimuth": 15.0,
        "description": "Bottom view (near upside-down + tilt)",
    },
    "back": {
        "elevation": 0.0,
        "azimuth": 180.0,
        "description": "Back view",
    },
}

_DEFAULT_RENDER_DIR = "/tmp/kiln_colored_renders"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class RenderResult:
    """Result of rendering a colored mesh to PNG."""

    path: str
    width: int
    height: int
    triangle_count: int
    face_colors_used: int
    quality_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "triangle_count": self.triangle_count,
            "face_colors_used": self.face_colors_used,
            "quality_score": round(self.quality_score, 1),
        }



def _luminance(color: tuple[int, ...]) -> float:
    """Perceived luminance (ITU-R BT.601) from an RGB tuple."""
    return (color[0] * 299 + color[1] * 587 + color[2] * 114) / 1000


# ---------------------------------------------------------------------------
# Camera transform
# ---------------------------------------------------------------------------


def _rotate_point(
    p: _Vec3,
    *,
    ce: float,
    se: float,
    ca: float,
    sa: float,
) -> _Vec3:
    """Rotate a point: first azimuth around Z, then elevation around X."""
    # Azimuth (rotation around Z axis)
    x = p[0] * ca - p[1] * sa
    y = p[0] * sa + p[1] * ca
    z = p[2]
    # Elevation (rotation around X axis)
    y2 = y * ce - z * se
    z2 = y * se + z * ce
    return (x, y2, z2)


def _rotate_vec(
    v: _Vec3,
    *,
    ce: float,
    se: float,
    ca: float,
    sa: float,
) -> _Vec3:
    """Rotate a direction vector (same transform as points)."""
    return _rotate_point(v, ce=ce, se=se, ca=ca, sa=sa)


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------

_LIGHT_DIR: _Vec3 = _normalize((0.3, -0.6, 0.7))
_AMBIENT = 0.45
_DIFFUSE = 0.55

# Rim light: edge glow so dark objects don't vanish against the
# background.  Kicks in when the face normal is nearly perpendicular
# to the camera direction (silhouette edges).  Fresnel-inspired:
# dark materials get stronger rim (physically: dark surfaces show
# stronger relative edge reflection because there's less diffuse to
# compete with).
_RIM_BASE = 0.08
_RIM_DARK_BOOST = 0.30  # extra rim for very dark faces


def _compute_brightness(
    normal_cam: _Vec3,
    light_cam: _Vec3,
    *,
    face_luminance: float = 128.0,
) -> float:
    """Compute brightness with ambient + directional diffuse + adaptive rim.

    Rim strength scales inversely with face luminance — dark materials
    get a strong Fresnel-like edge glow while bright materials get
    minimal rim (they already have contrast against the background).

    :param face_luminance: Perceptual luminance of the face color (0-255).
    """
    ndl = _dot(normal_cam, light_cam)
    diffuse = max(0.0, ndl)

    # Rim: brighten faces whose normal is nearly perpendicular to
    # camera (view direction ≈ -Y in camera space → normal.y ≈ 0).
    rim_factor = (1.0 - abs(normal_cam[1])) ** 2.5
    # Adaptive strength: dark faces (lum < 60) get much stronger rim
    darkness = max(0.0, min(1.0, 1.0 - face_luminance / 120.0))
    rim_strength = _RIM_BASE + _RIM_DARK_BOOST * darkness
    rim = rim_factor * rim_strength

    return min(1.0, _AMBIENT + _DIFFUSE * diffuse + rim)


def _apply_brightness(
    color: tuple[int, int, int],
    brightness: float,
) -> tuple[int, int, int]:
    """Apply brightness while preserving color saturation in shadow.

    Uses a blend toward a tinted dark rather than pure black so that
    e.g. red stays reddish-dark instead of becoming brown.
    """
    # Floor at 30% of channel max prevents full crush to black/brown
    floor = 0.30
    effective = floor + (1.0 - floor) * brightness
    return (
        max(0, min(255, int(color[0] * effective))),
        max(0, min(255, int(color[1] * effective))),
        max(0, min(255, int(color[2] * effective))),
    )


def _darken(color: tuple[int, int, int], *, factor: float = 0.75) -> tuple[int, int, int]:
    """Produce a slightly darker version of a color for edge outlines."""
    return (
        max(0, int(color[0] * factor)),
        max(0, int(color[1] * factor)),
        max(0, int(color[2] * factor)),
    )


# ---------------------------------------------------------------------------
# Smooth shading (crease-aware vertex-normal smoothing)
# ---------------------------------------------------------------------------

# Dihedral angles BELOW this threshold shade smoothly; at or above it the
# edge stays hard.  35 degrees sits in the 30-40 degree band that CAD and
# DCC tools conventionally default to for auto-smoothing: a cylinder wall
# tessellated at 12+ sections has facet angles of at most 30 degrees (64
# sections: ~5.6), so curved surfaces smooth, while cube edges, wall-to-cap
# rims, and deliberate chamfers of 35+ degrees keep a crisp lighting break.
_CREASE_ANGLE_DEG = 35.0
_CREASE_COS = math.cos(math.radians(_CREASE_ANGLE_DEG))


def _smooth_face_normals(
    tri_verts: list[tuple[_Vec3, _Vec3, _Vec3]],
) -> list[_Vec3]:
    """Per-face lighting normals via crease-aware vertex-normal smoothing.

    Flat facet normals make adjacent near-coplanar triangles — the tall
    split-quad pairs of a tessellated cylinder wall — alternate slightly
    in brightness, rendering a smooth wall as vertical zigzag striping
    that is not in the geometry.  The classic fix: for each vertex of
    each face, average the area-weighted normals of every face sharing
    that exact vertex position whose dihedral angle to this face is
    below ``_CREASE_ANGLE_DEG`` (so hard edges keep a hard lighting
    break), then take the face's lighting normal as the renormalized
    mean of its three vertex normals.

    Vertices are matched by exact coordinate tuple: triangle soups
    duplicate shared vertices bit-exactly (same source value, same
    deterministic transform), and fuzzy merging could weld genuinely
    distinct nearby vertices.

    Area weighting falls out of the raw cross product (its magnitude is
    twice the triangle area), so big faces dominate slivers at a shared
    vertex.  Purely geometric: face colors never enter — paint is not
    geometry, so brightness smooths across color boundaries while the
    colors themselves stay exact.

    :param tri_verts: One ``(v0, v1, v2)`` triple per face, any single
        consistent space (the rotation to camera space is rigid, so
        smoothing commutes with it).
    :returns: One unit lighting normal per face; degenerate faces fall
        back to their flat normal.
    """
    weighted: list[_Vec3] = []  # raw cross products (area-weighted normals)
    unit: list[_Vec3] = []  # flat unit normals (crease tests + fallback)
    for v0, v1, v2 in tri_verts:
        w = _cross(_sub(v1, v0), _sub(v2, v0))
        weighted.append(w)
        unit.append(_normalize(w))

    faces_at_vertex: dict[_Vec3, list[int]] = {}
    for i, verts in enumerate(tri_verts):
        for v in verts:
            faces_at_vertex.setdefault(v, []).append(i)

    # Fast path: when every normal in a vertex's fan lies within HALF
    # the crease angle of the fan's mean direction, the triangle
    # inequality guarantees every pair is within the crease — so the
    # per-face filter passes the whole fan for every querying face and
    # the vertex normal is the same for all of them.  Compute it once
    # (identical sum, identical order — bit-exact with the slow path).
    # This is the common case everywhere but crease rings and corners.
    half_crease_cos = math.cos(math.radians(_CREASE_ANGLE_DEG / 2.0))
    zero = (0.0, 0.0, 0.0)
    fan_normal: dict[_Vec3, _Vec3 | None] = {}  # None → filter per face
    for v, fan in faces_at_vertex.items():
        mx = my = mz = 0.0
        for j in fan:
            uj = unit[j]
            mx += uj[0]
            my += uj[1]
            mz += uj[2]
        m = _normalize((mx, my, mz))
        if m != zero and all(_dot(unit[j], m) >= half_crease_cos for j in fan):
            ax = ay = az = 0.0
            for j in fan:
                wj = weighted[j]
                ax += wj[0]
                ay += wj[1]
                az += wj[2]
            fan_normal[v] = _normalize((ax, ay, az))
        else:
            fan_normal[v] = None

    smoothed: list[_Vec3] = []
    for i, verts in enumerate(tri_verts):
        ui = unit[i]
        sx = sy = sz = 0.0
        for v in verts:
            vn = fan_normal[v]
            if vn is None:  # crease vertex — filter the fan per face
                ax = ay = az = 0.0
                for j in faces_at_vertex[v]:
                    if _dot(unit[j], ui) >= _CREASE_COS:
                        wj = weighted[j]
                        ax += wj[0]
                        ay += wj[1]
                        az += wj[2]
                vn = _normalize((ax, ay, az))
            sx += vn[0]
            sy += vn[1]
            sz += vn[2]
        sn = _normalize((sx, sy, sz))
        smoothed.append(ui if sn == zero else sn)
    return smoothed


# ---------------------------------------------------------------------------
# Core renderer
# ---------------------------------------------------------------------------


def render_colored_mesh(
    triangles: list[ColoredTriangle],
    *,
    output_path: str | None = None,
    width: int = 800,
    height: int = 600,
    elevation: float = 35.0,
    azimuth: float = 45.0,
    background: tuple[int, int, int] = (30, 30, 30),
    supersample: int = 2,
) -> RenderResult:
    """Render colored triangles to a PNG image.

    Uses a per-pixel depth buffer with per-face colors, directional
    lighting, back-face culling, and optional supersampling for
    anti-aliasing.

    :param triangles: List of ColoredTriangle from threemf_parser.
    :param output_path: Where to write the PNG. Defaults to a temp file.
    :param width: Output image width in pixels.
    :param height: Output image height in pixels.
    :param elevation: Camera elevation in degrees (0 = horizon, 90 = top-down).
    :param azimuth: Camera azimuth in degrees (rotation around Z axis).
    :param background: RGB background color.
    :param supersample: Supersampling factor (2 = render at 2x then downscale).
    :returns: RenderResult with output path and metadata.
    """
    if not triangles:
        raise ValueError("No triangles to render")

    # Resolve output path
    if output_path is None:
        os.makedirs(_DEFAULT_RENDER_DIR, mode=0o700, exist_ok=True)
        fd, output_path = tempfile.mkstemp(
            suffix=".png",
            dir=_DEFAULT_RENDER_DIR,
        )
        os.close(fd)

    # Internal render size (supersampled)
    ss = max(1, supersample)
    rw = width * ss
    rh = height * ss

    # Precompute camera trig
    elev_rad = math.radians(elevation)
    azim_rad = math.radians(azimuth)
    ce = math.cos(elev_rad)
    se = math.sin(elev_rad)
    ca = math.cos(azim_rad)
    sa = math.sin(azim_rad)

    cam_kwargs = {"ce": ce, "se": se, "ca": ca, "sa": sa}

    # Transform light direction into camera space
    light_cam = _normalize(_rotate_vec(_LIGHT_DIR, **cam_kwargs))

    # --- Pass 1: Transform all vertices & compute bounding box -----------

    # Pre-extract vertex data for speed
    n = len(triangles)
    transformed: list[tuple[_Vec3, _Vec3, _Vec3]] = [
        (
            _rotate_point(tri.v0, **cam_kwargs),
            _rotate_point(tri.v1, **cam_kwargs),
            _rotate_point(tri.v2, **cam_kwargs),
        )
        for tri in triangles
    ]

    # Find bounding box of all transformed vertices for projection scaling
    all_x: list[float] = []
    all_z2: list[float] = []
    for t0, t1, t2 in transformed:
        all_x.extend((t0[0], t1[0], t2[0]))
        all_z2.extend((t0[2], t1[2], t2[2]))

    min_x = min(all_x)
    max_x = max(all_x)
    min_z = min(all_z2)
    max_z = max(all_z2)

    span_x = max_x - min_x
    span_z = max_z - min_z
    center_x = (min_x + max_x) / 2.0
    center_z = (min_z + max_z) / 2.0

    # Scale to fit with margin
    margin = 0.85
    if span_x < 1e-9 and span_z < 1e-9:
        sf = 1.0
    elif span_x / rw > span_z / rh:
        sf = rw * margin / span_x
    else:
        sf = rh * margin / span_z

    half_rw = rw / 2.0
    half_rh = rh / 2.0

    # --- Pass 2: Build adjacency for same-color outline suppression --------
    #
    # Index triangles by their edges (as sorted vertex-index pairs from the
    # original mesh) so we know which faces share an edge.  Only draw an
    # outline segment on edges where the neighbor has a DIFFERENT source
    # color — this eliminates the ugly diagonal seam across same-colored
    # faces (e.g. the two triangles that make up one cube face).

    # Map: edge (sorted pair of vertex coords) → list of face indices
    _edge_to_faces: dict[tuple[_Vec3, _Vec3], list[int]] = {}
    for i, tri in enumerate(triangles):
        verts = (tri.v0, tri.v1, tri.v2)
        for j in range(3):
            edge = tuple(sorted((verts[j], verts[(j + 1) % 3])))
            _edge_to_faces.setdefault(edge, []).append(i)  # type: ignore[arg-type]

    # Boundary edges are computed AFTER back-face culling (Pass 3)
    # so that silhouette edges (neighbor culled) are NOT marked as
    # color boundaries — they get the silhouette treatment instead.

    # --- Pass 2b: Compute smooth normals for lighting --------------------
    #
    # Raw face normals cause visible faceting on curved surfaces (each
    # triangle is a flat tile with uniform brightness).  Lighting instead
    # uses crease-aware vertex-normal smoothing (see _smooth_face_normals)
    # for a Gouraud-like gradual transition without per-pixel
    # interpolation.  The raw normal is kept for back-face culling
    # (must be exact); the smoothed normal is for lighting only.

    # Pre-compute all camera-space face normals
    _raw_normals: list[_Vec3] = []
    for i in range(n):
        t0, t1, t2 = transformed[i]
        _raw_normals.append(_face_normal(t0, t1, t2))

    _smooth_normals = _smooth_face_normals(transformed)

    # --- Pass 3: Compute per-face render data ---

    face_data: list[
        tuple[
            list[tuple[int, int]],  # screen polygon (3 vertices)
            tuple[int, int, int],  # lit fill color
        ]
    ] = []
    # Rasterization inputs aligned with face_data: float screen coordinates
    # (no int truncation) and per-vertex camera-space depth for the z-buffer.
    raster_data: list[
        tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
    ] = []

    unique_colors: set[tuple[int, int, int]] = set()

    # Track which face indices survive culling for boundary detection
    _visible_faces: set[int] = set()
    _vis_to_draw: dict[int, int] = {}

    for i in range(n):
        t0, t1, t2 = transformed[i]
        tri = triangles[i]

        raw_normal = _raw_normals[i]

        # Back-face culling uses RAW normal (must be exact)
        if raw_normal[1] > 0.1:
            continue

        _vis_to_draw[i] = len(face_data)
        _visible_faces.add(i)

        # Orthographic projection: X -> screen X, Z -> screen Y (inverted)
        fxs = tuple(half_rw + (v[0] - center_x) * sf for v in (t0, t1, t2))
        fys = tuple(half_rh - (v[2] - center_z) * sf for v in (t0, t1, t2))
        pts = [(int(x), int(y)) for x, y in zip(fxs, fys, strict=True)]

        # Lighting uses SMOOTH normal for gradual shading on curves
        smooth_normal = _smooth_normals[i]
        face_lum = _luminance(tri.color)
        brightness = _compute_brightness(smooth_normal, light_cam, face_luminance=face_lum)
        lit_color = _apply_brightness(tri.color, brightness)

        unique_colors.add(tri.color)
        face_data.append((pts, lit_color))
        raster_data.append((fxs, fys, (t0[1], t1[1], t2[1])))

    # --- Pass 3b: Compute color boundary edges (post-culling) ---
    # Only mark an edge as a color boundary when BOTH adjacent faces
    # are visible and have different colors.  Silhouette edges (neighbor
    # culled or missing) are NOT boundaries — they get silhouette treatment.
    # Each line remembers which faces legitimately own its pixels (the two
    # sides of the edge), so the draw pass can keep it off geometry that
    # occludes the edge.
    _boundary_lines: list[tuple[int, int, tuple[int, ...]]] = []
    for i in _visible_faces:
        tri = triangles[i]
        verts = (tri.v0, tri.v1, tri.v2)
        for j in range(3):
            edge = tuple(sorted((verts[j], verts[(j + 1) % 3])))
            neighbors = _edge_to_faces.get(edge, [])  # type: ignore[arg-type]
            has_visible_same_color = False
            visible_diff: list[int] = []
            for ni in neighbors:
                if ni == i:
                    continue
                if ni not in _visible_faces:
                    continue  # culled neighbor — skip, not a boundary
                if triangles[ni].color == tri.color:
                    has_visible_same_color = True
                else:
                    visible_diff.append(ni)
            # Mark as boundary ONLY if there's a visible neighbor with
            # different color.  Same-color or no visible neighbor → no outline.
            if visible_diff and not has_visible_same_color:
                owners = (
                    _vis_to_draw[i],
                    *(_vis_to_draw[ni] for ni in visible_diff),
                )
                _boundary_lines.append((_vis_to_draw[i], j, owners))

    # --- Pass 4: Depth-buffered rasterization ---
    #
    # One depth per PIXEL, not one per face: the previous pass sorted whole
    # faces by centroid depth and painted back-to-front, and a single sort
    # key cannot order faces whose screen overlap spans crossing depth
    # ranges.  On a single watertight mesh that cost ~25 stray pixels; on
    # composed multi-part plates — where long thin parts from different
    # objects interleave — it measured 3.9-10.8% wrong-colour pixels, a
    # solid wedge of one part's colour across another, in a feature whose
    # whole claim is which colour goes where.
    import numpy as np
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    img = Image.new("RGB", (rw, rh), background)
    draw = ImageDraw.Draw(img)

    # --- Gradient background: vertical gradient for spatial context.
    # Top is slightly darker, bottom lighter — simulates ambient
    # environment light and a ground plane so objects don't float
    # in a featureless void.
    bg_r, bg_g, bg_b = background
    for row in range(rh):
        t = row / max(1, rh - 1)  # 0=top, 1=bottom
        # Full gradient: top darkened by -8, bottom lifted by +30
        shift = int(-8 + 38 * t)
        lr = max(0, min(255, bg_r + shift))
        lg = max(0, min(255, bg_g + shift))
        lb = max(0, min(255, bg_b + shift))
        draw.line([(0, row), (rw, row)], fill=(lr, lg, lb))

    # The camera looks along +Y, so smaller camera-space Y is nearer;
    # the winner at each pixel is the face with the minimum interpolated
    # depth among those covering the pixel's center.
    zbuf = np.full((rh, rw), np.inf, dtype=np.float32)
    owner = np.full((rh, rw), -1, dtype=np.int32)
    for di, (fxs, fys, deps) in enumerate(raster_data):
        x0 = max(int(min(fxs)), 0)
        y0 = max(int(min(fys)), 0)
        x1 = min(int(max(fxs)) + 1, rw)
        y1 = min(int(max(fys)) + 1, rh)
        if x0 >= x1 or y0 >= y1:
            continue
        ax, ay = fxs[0], fys[0]
        bx, by = fxs[1], fys[1]
        cx, cy = fxs[2], fys[2]
        denom = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(denom) < 1e-12:
            continue  # edge-on: zero screen area
        ys_grid, xs_grid = np.mgrid[y0:y1, x0:x1]
        px = xs_grid + 0.5  # pixel centers
        py = ys_grid + 0.5
        w0 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / denom
        w1 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / denom
        w2 = 1.0 - w0 - w1
        # The epsilon keeps shared-edge pixels covered by at least one of
        # the two faces regardless of which side of the edge the center
        # falls on — without it, watertight surfaces show background
        # pinholes along triangle seams.
        inside = (w0 >= -1e-7) & (w1 >= -1e-7) & (w2 >= -1e-7)
        if not inside.any():
            continue
        depth = w0 * deps[0] + w1 * deps[1] + w2 * deps[2]
        tile_z = zbuf[y0:y1, x0:x1]
        tile_owner = owner[y0:y1, x0:x1]
        win = inside & (depth < tile_z)
        tile_z[win] = depth[win].astype(np.float32)
        tile_owner[win] = di

    arr = np.asarray(img, dtype=np.uint8).copy()
    if face_data:
        colors = np.array([fill for _, fill in face_data], dtype=np.uint8)
        drawn = owner >= 0
        arr[drawn] = colors[owner[drawn]]

    # Boundary lines: a neutral dark outline where different-colored faces
    # meet — a fixed dark gray prevents colored artifacts on curved
    # surfaces where tinted outlines (color*0.7) create visible colored
    # lines along zone transitions.  Drawn only where one of the edge's own
    # faces won the pixel, so an occluding part in front is never striped.
    outline_rgb = np.array((18, 18, 18), dtype=np.uint8)
    for di, edge_idx, owners in _boundary_lines:
        fxs, fys, _deps = raster_data[di]
        ex0, ey0 = fxs[edge_idx], fys[edge_idx]
        ex1, ey1 = fxs[(edge_idx + 1) % 3], fys[(edge_idx + 1) % 3]
        steps = int(max(abs(ex1 - ex0), abs(ey1 - ey0))) + 1
        lx = np.rint(np.linspace(ex0, ex1, steps)).astype(np.int64)
        ly = np.rint(np.linspace(ey0, ey1, steps)).astype(np.int64)
        inb = (lx >= 0) & (lx < rw) & (ly >= 0) & (ly < rh)
        lx, ly = lx[inb], ly[inb]
        vis = np.isin(owner[ly, lx], owners)
        arr[ly[vis], lx[vis]] = outline_rgb

    img = Image.fromarray(arr)

    # --- Adaptive silhouette contour ---
    # Draw a subtle edge around the object outline so dark materials
    # remain visible against the background.  The edge is ADAPTIVE:
    # strong on dark faces (which need contrast), invisible on bright
    # faces (which already pop against the background).
    #
    # The outline is WHERE THE OBJECT MEETS THE BACKGROUND, and that is
    # measured directly rather than inferred from adjacency.  The previous
    # test — "an edge carried by exactly ONE visible face" — reads as a
    # silhouette for plenty of interior edges too, because back-face
    # culling strips the neighbour off any edge whose far side turns away:
    # the seam where a boolean union re-triangulates a wall, and the
    # terminator of a curved surface.  Both sit in the MIDDLE of an
    # unbroken surface, so the contour was drawn as hairlines across the
    # body of the model (measured on a mug with a handle: a full-height
    # line down the wall plus two arcs at the handle junction).  It hid in
    # single-colour previews only because bright fills skip the contour,
    # so a grey control render looked clean while every painted one did
    # not.  Silhouette-by-background-adjacency cannot draw inside the
    # object at all: the ring is derived from the drawn mask's own border.
    _silhouette = Image.new("L", (rw, rh), 0)
    _sil_draw = ImageDraw.Draw(_silhouette)
    for pts, _fill in face_data:
        _sil_draw.polygon(pts, fill=255)

    # Inner border = mask minus its erosion.  Width tracks the supersample
    # factor so the contour survives LANCZOS downsampling (a 1px ring at
    # 2x would land at an invisible half-pixel).
    _eroded = _silhouette.filter(ImageFilter.MinFilter(2 * ss + 1))
    _ring = ImageChops.difference(_silhouette, _eroded)

    # Adaptive, exactly as before, but evaluated per PIXEL against what was
    # actually drawn there: bright pixels already contrast with the
    # background and get nothing; dark ones get a lift toward it.
    bg_lum = _luminance((bg_r, bg_g, bg_b))
    _cutoff = bg_lum + 30
    _denom = max(1, bg_lum + 40)
    _lum = img.convert("L")

    def _lift_of(value: int) -> int:
        if value > _cutoff:
            return 0  # bright pixel — already has contrast
        lift = int(50 * max(0.0, min(1.0, 1.0 - (value / _denom))))
        return lift if lift >= 5 else 0

    _lift = _lum.point(_lift_of)
    _ring = ImageChops.multiply(_ring, _lift.point(lambda v: 255 if v else 0))
    _contour = Image.merge(
        "RGB",
        (
            _lift.point(lambda v: min(255, bg_r + v + 20)),
            _lift.point(lambda v: min(255, bg_g + v + 20)),
            _lift.point(lambda v: min(255, bg_b + v + 20)),
        ),
    )
    img.paste(_contour, (0, 0), _ring)

    # --- Downsample if supersampled ---

    if ss > 1:
        img = img.resize((width, height), Image.LANCZOS)

    img.save(output_path, "PNG")

    # Compute render quality score.  Rewards views that show more
    # distinct colors (informative) with decent contrast (readable).
    # color_diversity dominates so isometric (3+ colors visible) beats
    # a single-face view even if that face has extreme contrast.
    _pixel_lums = [_luminance(f) for _, f in face_data]
    contrast = (max(_pixel_lums) - min(_pixel_lums)) if _pixel_lums else 0.0
    # Quality = color diversity (dominant) + contrast (tiebreaker).
    # A view showing 3 colors is always better than one showing 1 color
    # with high contrast, because multicolor preview exists to show
    # color placement.  The 100:1 weighting ensures color count drives
    # ranking while contrast only breaks ties within the same count.
    _COLOR_WEIGHT = 100.0
    _CONTRAST_WEIGHT = 1.0
    quality = len(unique_colors) * _COLOR_WEIGHT + contrast * _CONTRAST_WEIGHT

    return RenderResult(
        path=output_path,
        width=width,
        height=height,
        triangle_count=len(triangles),
        face_colors_used=len(unique_colors),
        quality_score=quality,
    )


# ---------------------------------------------------------------------------
# Multi-angle renderer
# ---------------------------------------------------------------------------


def render_colored_mesh_multi_angle(
    triangles: list[ColoredTriangle],
    *,
    output_dir: str | None = None,
    width: int = 800,
    height: int = 600,
    angles: list[str] | None = None,
    background: tuple[int, int, int] = (30, 30, 30),
    supersample: int = 2,
) -> list[dict[str, Any]]:
    """Render colored triangles from multiple standard camera angles.

    Matches the angle labels used by Kiln's existing ``visualize_model``:
    isometric, front, right, top, bottom, back.

    :param triangles: List of ColoredTriangle from threemf_parser.
    :param output_dir: Directory for output PNGs. Defaults to temp dir.
    :param width: Output image width in pixels.
    :param height: Output image height in pixels.
    :param angles: List of angle names to render. Defaults to all presets.
    :param background: RGB background color.
    :param supersample: Supersampling factor.
    :returns: List of dicts with 'angle', 'path', 'description' keys
        (same structure as visualize_model's views list).
    """
    if not triangles:
        raise ValueError("No triangles to render")

    if angles is None:
        angles = list(_CAMERA_ANGLES.keys())

    # Validate angle names
    unknown = [a for a in angles if a not in _CAMERA_ANGLES]
    if unknown:
        raise ValueError(
            f"Unknown camera angles: {unknown}. "
            f"Valid angles: {list(_CAMERA_ANGLES.keys())}"
        )

    if output_dir is None:
        output_dir = _DEFAULT_RENDER_DIR
    os.makedirs(output_dir, mode=0o700, exist_ok=True)

    # --- Adaptive background: detect mesh luminance ONCE, apply to
    # all angles.  Triggers when ≥25% of faces are very dark (lum < 40),
    # not just the average — so a gold+obsidian pyramid still triggers
    # while a mostly-bright model with one dark face doesn't.
    _face_lums = sorted(_luminance(tri.color) for tri in triangles)
    _q25_idx = max(0, len(_face_lums) // 4)  # 25th percentile
    is_dark_material = _face_lums[_q25_idx] < 40 if _face_lums else False

    if is_dark_material:
        # Shift background lighter for dark meshes
        bg = (
            min(255, background[0] + 30),
            min(255, background[1] + 30),
            min(255, background[2] + 30),
        )
    else:
        bg = background

    results: list[dict[str, Any]] = []

    for angle_name in angles:
        preset = _CAMERA_ANGLES[angle_name]
        out_path = os.path.join(output_dir, f"colored_{angle_name}.png")

        result = render_colored_mesh(
            triangles,
            output_path=out_path,
            width=width,
            height=height,
            elevation=preset["elevation"],
            azimuth=preset["azimuth"],
            background=bg,
            supersample=supersample,
        )

        results.append({
            "angle": angle_name,
            "path": result.path,
            "description": preset["description"],
            "quality_score": round(result.quality_score, 1),
        })

    # Quality scores are metadata for the agent — it can mention the
    # best angle or reorder if it wants.  Default order stays canonical
    # (isometric first) because it's almost always the best overview.

    # --- Dark material metadata flag for agent context
    if is_dark_material:
        for r in results:
            r["dark_material"] = True

    return results
