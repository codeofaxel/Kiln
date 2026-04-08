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


# ---------------------------------------------------------------------------
# Pure-math vector helpers (no numpy)
# ---------------------------------------------------------------------------

_Vec3 = tuple[float, float, float]


def _sub(a: _Vec3, b: _Vec3) -> _Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: _Vec3, b: _Vec3) -> _Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a: _Vec3, b: _Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(v: _Vec3) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _normalize(v: _Vec3) -> _Vec3:
    ln = _length(v)
    if ln < 1e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / ln, v[1] / ln, v[2] / ln)


def _face_normal(v0: _Vec3, v1: _Vec3, v2: _Vec3) -> _Vec3:
    """Compute the unit normal of a triangle."""
    edge1 = _sub(v1, v0)
    edge2 = _sub(v2, v0)
    return _normalize(_cross(edge1, edge2))


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

    Uses painter's algorithm with per-face colors, directional lighting,
    back-face culling, and optional supersampling for anti-aliasing.

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
    # triangle is a flat tile with uniform brightness).  Smooth normals
    # average each face's normal with its same-color neighbors, giving
    # a Gouraud-like gradual shading transition without per-pixel
    # interpolation.  We still use the raw normal for back-face culling
    # (must be exact) but use the smoothed normal for lighting only.

    # Pre-compute all camera-space face normals
    _raw_normals: list[_Vec3] = []
    for i in range(n):
        t0, t1, t2 = transformed[i]
        _raw_normals.append(_face_normal(t0, t1, t2))

    # Build neighbor sets (same-color faces sharing an edge)
    _same_color_neighbors: dict[int, list[int]] = {}
    for i, tri in enumerate(triangles):
        neighbors: list[int] = []
        verts = (tri.v0, tri.v1, tri.v2)
        for j in range(3):
            edge = tuple(sorted((verts[j], verts[(j + 1) % 3])))
            for ni in _edge_to_faces.get(edge, []):  # type: ignore[arg-type]
                if ni != i and triangles[ni].color == tri.color:
                    neighbors.append(ni)
        _same_color_neighbors[i] = neighbors

    # Smooth: average face normal with neighbors' normals
    _smooth_normals: list[_Vec3] = []
    for i in range(n):
        nx, ny, nz = _raw_normals[i]
        count = 1
        for ni in _same_color_neighbors.get(i, []):
            nn = _raw_normals[ni]
            nx += nn[0]
            ny += nn[1]
            nz += nn[2]
            count += 1
        _smooth_normals.append(_normalize((nx / count, ny / count, nz / count)))

    # --- Pass 3: Compute per-face render data ---

    face_data: list[
        tuple[
            float,  # depth (for sorting)
            list[tuple[int, int]],  # screen polygon (3 vertices)
            tuple[int, int, int],  # lit fill color
            tuple[int, int, int],  # outline color (darker)
            set[int],  # which edges (0,1,2) are color boundaries
        ]
    ] = []

    unique_colors: set[tuple[int, int, int]] = set()

    # Track which face indices survive culling for boundary detection
    _visible_faces: set[int] = set()

    for i in range(n):
        t0, t1, t2 = transformed[i]
        tri = triangles[i]

        raw_normal = _raw_normals[i]

        # Back-face culling uses RAW normal (must be exact)
        if raw_normal[1] > 0.1:
            continue

        _visible_faces.add(i)

        # Depth for painter's algorithm (mean Y of the three vertices)
        depth = (t0[1] + t1[1] + t2[1]) / 3.0

        # Orthographic projection: X -> screen X, Z -> screen Y (inverted)
        pts = [
            (
                int(half_rw + (v[0] - center_x) * sf),
                int(half_rh - (v[2] - center_z) * sf),
            )
            for v in (t0, t1, t2)
        ]

        # Lighting uses SMOOTH normal for gradual shading on curves
        smooth_normal = _smooth_normals[i]
        face_lum = (tri.color[0] * 299 + tri.color[1] * 587 + tri.color[2] * 114) / 1000
        brightness = _compute_brightness(smooth_normal, light_cam, face_luminance=face_lum)
        lit_color = _apply_brightness(tri.color, brightness)
        # Neutral dark outline for color boundaries — a fixed dark gray
        # prevents colored artifacts on curved surfaces where tinted
        # outlines (green*0.7) create visible colored lines along zone
        # transitions.  The color contrast between zones already
        # communicates the boundary — the line just separates cleanly.
        outline_color = (18, 18, 18)

        unique_colors.add(tri.color)
        face_data.append((depth, pts, lit_color, outline_color, set()))  # boundaries filled below

    # --- Pass 3b: Compute color boundary edges (post-culling) ---
    # Only mark an edge as a color boundary when BOTH adjacent faces
    # are visible and have different colors.  Silhouette edges (neighbor
    # culled or missing) are NOT boundaries — they get silhouette treatment.
    _face_boundary_edges: dict[int, set[int]] = {}
    for i in _visible_faces:
        tri = triangles[i]
        boundary: set[int] = set()
        verts = (tri.v0, tri.v1, tri.v2)
        for j in range(3):
            edge = tuple(sorted((verts[j], verts[(j + 1) % 3])))
            neighbors = _edge_to_faces.get(edge, [])  # type: ignore[arg-type]
            has_visible_same_color = False
            has_visible_diff_color = False
            for ni in neighbors:
                if ni == i:
                    continue
                if ni not in _visible_faces:
                    continue  # culled neighbor — skip, not a boundary
                if triangles[ni].color == tri.color:
                    has_visible_same_color = True
                else:
                    has_visible_diff_color = True
            # Mark as boundary ONLY if there's a visible neighbor with
            # different color.  Same-color or no visible neighbor → no outline.
            if has_visible_diff_color and not has_visible_same_color:
                boundary.add(j)
        _face_boundary_edges[i] = boundary

    # Patch boundary sets into face_data (indexed by draw order)
    _vis_list = sorted(_visible_faces)
    _vis_to_draw: dict[int, int] = {}
    draw_idx = 0
    for i in range(n):
        if i in _visible_faces:
            _vis_to_draw[i] = draw_idx
            draw_idx += 1
    for i, boundary in _face_boundary_edges.items():
        if boundary and i in _vis_to_draw:
            di = _vis_to_draw[i]
            old = face_data[di]
            face_data[di] = (old[0], old[1], old[2], old[3], boundary)

    # --- Pass 4: Sort back-to-front and draw ---

    # Sort by depth descending (farthest first = largest Y first)
    face_data.sort(key=lambda fd: fd[0], reverse=True)

    from PIL import Image, ImageDraw

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

    for _depth, pts, fill, outline, boundary_edges in face_data:
        # Fill the polygon without any outline — clean fill only
        draw.polygon(pts, fill=fill)

        # Draw outline ONLY on color-boundary edges (where neighbor
        # has a different color).  This eliminates same-color seams.
        for edge_idx in boundary_edges:
            p0 = pts[edge_idx]
            p1 = pts[(edge_idx + 1) % 3]
            draw.line([p0, p1], fill=outline, width=1)

    # --- Adaptive silhouette contour ---
    # Draw a subtle edge around the object outline so dark materials
    # remain visible against the background.  The edge is ADAPTIVE:
    # strong on dark faces (which need contrast), invisible on bright
    # faces (which already pop against the background).
    #
    # Detect silhouette edges: edges belonging to only ONE visible
    # face (boundary between drawn face and culled/missing face).
    _edge_info: dict[
        tuple[tuple[int, int], tuple[int, int]],
        list[tuple[int, int, int]],  # lit fill colors of adjacent visible faces
    ] = {}
    for _depth, pts, fill, _outline, _be in face_data:
        for ei in range(3):
            p0 = pts[ei]
            p1 = pts[(ei + 1) % 3]
            edge_key = (min(p0, p1), max(p0, p1))
            _edge_info.setdefault(edge_key, []).append(fill)

    for edge_key, fills in _edge_info.items():
        if len(fills) != 1:
            continue  # interior edge, skip
        # Silhouette edge — compute adaptive brightness.
        # Dark faces get a strong contour, bright faces get none.
        face_lum = (fills[0][0] * 299 + fills[0][1] * 587 + fills[0][2] * 114) / 1000
        # Only draw contour if face is darker than the background
        bg_lum = (bg_r * 299 + bg_g * 587 + bg_b * 114) / 1000
        if face_lum > bg_lum + 30:
            continue  # bright face — already has contrast, skip
        # Strength: max for very dark faces, fading as face approaches bg
        strength = max(0.0, min(1.0, 1.0 - (face_lum / max(1, bg_lum + 40))))
        lift = int(50 * strength)
        if lift < 5:
            continue
        sc = (
            min(255, bg_r + lift + 20),
            min(255, bg_g + lift + 20),
            min(255, bg_b + lift + 20),
        )
        # Width scales with supersample factor so the contour survives
        # LANCZOS downsampling (1px at 2x → invisible 0.5px otherwise)
        draw.line([edge_key[0], edge_key[1]], fill=sc, width=max(1, ss))

    # --- Downsample if supersampled ---

    if ss > 1:
        img = img.resize((width, height), Image.LANCZOS)

    img.save(output_path, "PNG")

    # Compute render quality score.  Rewards views that show more
    # distinct colors (informative) with decent contrast (readable).
    # color_diversity dominates so isometric (3+ colors visible) beats
    # a single-face view even if that face has extreme contrast.
    _pixel_lums = [
        (f[0] * 299 + f[1] * 587 + f[2] * 114) / 1000
        for _, _, f, _, _ in face_data
    ]
    contrast = (max(_pixel_lums) - min(_pixel_lums)) if _pixel_lums else 0.0
    quality = len(unique_colors) * 50.0 + contrast * 0.5

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
    _face_lums = sorted(
        (tri.color[0] * 299 + tri.color[1] * 587 + tri.color[2] * 114) / 1000
        for tri in triangles
    )
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
