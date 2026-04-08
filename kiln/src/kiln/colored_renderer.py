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
from typing import Any

from PIL import Image, ImageDraw

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "triangle_count": self.triangle_count,
            "face_colors_used": self.face_colors_used,
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
_AMBIENT = 0.35
_DIFFUSE = 0.65


def _compute_brightness(
    normal_cam: _Vec3,
    light_cam: _Vec3,
) -> float:
    """Compute brightness with ambient + directional diffuse lighting.

    Uses signed dot product clamped to [0, 1] for proper directional shading.
    """
    ndl = _dot(normal_cam, light_cam)
    diffuse = max(0.0, ndl)
    return min(1.0, _AMBIENT + _DIFFUSE * diffuse)


def _apply_brightness(
    color: tuple[int, int, int],
    brightness: float,
) -> tuple[int, int, int]:
    return (
        max(0, min(255, int(color[0] * brightness))),
        max(0, min(255, int(color[1] * brightness))),
        max(0, min(255, int(color[2] * brightness))),
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

    # --- Pass 2: Compute per-face data (normals, depth, screen coords) ---

    face_data: list[
        tuple[
            float,  # depth (for sorting)
            list[tuple[int, int]],  # screen polygon
            tuple[int, int, int],  # lit fill color
            tuple[int, int, int],  # outline color (darker)
        ]
    ] = []

    unique_colors: set[tuple[int, int, int]] = set()

    for i in range(n):
        t0, t1, t2 = transformed[i]
        tri = triangles[i]

        # Face normal in camera space
        normal = _face_normal(t0, t1, t2)

        # Back-face culling: skip faces whose normal points away from camera.
        # Camera looks along -Y in our coordinate system (after transforms),
        # so faces with normal Y > threshold are facing away.
        if normal[1] > 0.1:
            continue

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

        # Lighting
        brightness = _compute_brightness(normal, light_cam)
        lit_color = _apply_brightness(tri.color, brightness)
        outline_color = _darken(lit_color, factor=0.75)

        unique_colors.add(tri.color)
        face_data.append((depth, pts, lit_color, outline_color))

    # --- Pass 3: Sort back-to-front and draw ---

    # Sort by depth descending (farthest first = largest Y first)
    face_data.sort(key=lambda fd: fd[0], reverse=True)

    img = Image.new("RGB", (rw, rh), background)
    draw = ImageDraw.Draw(img)

    for _depth, pts, fill, outline in face_data:
        draw.polygon(pts, fill=fill, outline=outline)

    # --- Downsample if supersampled ---

    if ss > 1:
        img = img.resize((width, height), Image.LANCZOS)

    img.save(output_path, "PNG")

    return RenderResult(
        path=output_path,
        width=width,
        height=height,
        triangle_count=len(triangles),
        face_colors_used=len(unique_colors),
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
            background=background,
            supersample=supersample,
        )

        results.append({
            "angle": angle_name,
            "path": result.path,
            "description": preset["description"],
        })

    return results
