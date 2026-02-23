"""Mesh preview rendering for generated models.

Renders lightweight multi-view SVG previews (isometric, dimetric, trimetric)
without external dependencies.
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiln.generation.validation import _parse_obj, _parse_stl

Triangle = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]

_VIEW_CONFIGS: tuple[tuple[str, float, float, float], ...] = (
    ("Isometric", 45.0, 35.264, 0.0),
    ("Dimetric", 45.0, 20.0, 0.0),
    ("Trimetric", 35.0, 50.0, 15.0),
)


@dataclass(frozen=True)
class PreviewResult:
    """Metadata describing a rendered preview artifact."""

    path: str
    format: str
    views: list[str]
    triangle_count: int
    downsampled: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "format": self.format,
            "views": list(self.views),
            "triangle_count": self.triangle_count,
            "downsampled": self.downsampled,
        }


def render_multi_view_preview(
    mesh_path: str,
    *,
    output_path: str | None = None,
    max_triangles: int = 7000,
) -> PreviewResult:
    """Render a 3-view mesh preview as an SVG image.

    Args:
        mesh_path: Path to STL/OBJ mesh.
        output_path: Optional output file path. Defaults to temp dir.
        max_triangles: Maximum triangles to draw before deterministic downsampling.

    Returns:
        :class:`PreviewResult` describing the generated file.
    """
    triangles = _load_triangles(mesh_path)
    triangle_count = len(triangles)
    if triangle_count == 0:
        raise ValueError("Mesh contains no triangles.")

    downsampled = False
    if triangle_count > max_triangles:
        step = max(1, triangle_count // max_triangles)
        triangles = triangles[::step]
        downsampled = len(triangles) < triangle_count

    centered = _centered_triangles(triangles)
    svg = _build_svg(centered, original_triangle_count=triangle_count, downsampled=downsampled)

    if output_path is None:
        out_dir = os.path.join(tempfile.gettempdir(), "kiln_previews")
        os.makedirs(out_dir, mode=0o700, exist_ok=True)
        stem = Path(mesh_path).stem or "model"
        output_path = os.path.join(out_dir, f"{stem}_preview.svg")
    else:
        output_path = os.path.abspath(output_path)
        out_dir = os.path.dirname(output_path) or "."
        os.makedirs(out_dir, mode=0o700, exist_ok=True)

    Path(output_path).write_text(svg, encoding="utf-8")
    return PreviewResult(
        path=output_path,
        format="svg",
        views=[cfg[0].lower() for cfg in _VIEW_CONFIGS],
        triangle_count=triangle_count,
        downsampled=downsampled,
    )


def _load_triangles(mesh_path: str) -> list[Triangle]:
    path = Path(mesh_path)
    if not path.is_file():
        raise FileNotFoundError(f"Mesh file not found: {mesh_path}")

    ext = path.suffix.lower()
    errors: list[str] = []
    if ext == ".stl":
        raw, _ = _parse_stl(path, errors)
    elif ext == ".obj":
        raw, _ = _parse_obj(path, errors)
    else:
        raise ValueError(f"Unsupported mesh extension {ext!r} for preview. Expected .stl or .obj.")

    if errors:
        raise ValueError(f"Failed to parse mesh for preview: {'; '.join(errors)}")

    triangles: list[Triangle] = []
    for tri in raw:
        triangles.append(
            (
                (float(tri[0][0]), float(tri[0][1]), float(tri[0][2])),
                (float(tri[1][0]), float(tri[1][1]), float(tri[1][2])),
                (float(tri[2][0]), float(tri[2][1]), float(tri[2][2])),
            )
        )
    return triangles


def _centered_triangles(triangles: list[Triangle]) -> list[Triangle]:
    xs = [v[0] for tri in triangles for v in tri]
    ys = [v[1] for tri in triangles for v in tri]
    zs = [v[2] for tri in triangles for v in tri]
    cx = (min(xs) + max(xs)) * 0.5
    cy = (min(ys) + max(ys)) * 0.5
    cz = (min(zs) + max(zs)) * 0.5

    centered: list[Triangle] = []
    for tri in triangles:
        centered.append(
            (
                (tri[0][0] - cx, tri[0][1] - cy, tri[0][2] - cz),
                (tri[1][0] - cx, tri[1][1] - cy, tri[1][2] - cz),
                (tri[2][0] - cx, tri[2][1] - cy, tri[2][2] - cz),
            )
        )
    return centered


def _build_svg(triangles: list[Triangle], *, original_triangle_count: int, downsampled: bool) -> str:
    width = 1380
    height = 520
    margin = 24
    gutter = 20
    panel_w = (width - (margin * 2) - (gutter * 2)) / 3.0
    panel_h = 440.0
    panel_y = 58.0

    chunks: list[str] = []
    chunks.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    chunks.append('<rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>')
    chunks.append(
        '<text x="690" y="32" text-anchor="middle" fill="#222222" '
        'font-family="Helvetica, Arial, sans-serif" font-size="26" font-weight="700">'
        "Model Preview"
        "</text>"
    )
    subtitle = f"{original_triangle_count:,} triangles"
    if downsampled:
        subtitle += " (downsampled for preview)"
    chunks.append(
        '<text x="690" y="50" text-anchor="middle" fill="#666666" '
        'font-family="Helvetica, Arial, sans-serif" font-size="13">'
        + subtitle
        + "</text>"
    )

    for idx, (title, yaw, pitch, roll) in enumerate(_VIEW_CONFIGS):
        x0 = margin + idx * (panel_w + gutter)
        chunks.extend(_render_panel(triangles, title, yaw, pitch, roll, x0, panel_y, panel_w, panel_h))

    chunks.append("</svg>")
    return "\n".join(chunks)


def _render_panel(
    triangles: list[Triangle],
    title: str,
    yaw_deg: float,
    pitch_deg: float,
    roll_deg: float,
    x0: float,
    y0: float,
    w: float,
    h: float,
) -> list[str]:
    pad = 20.0
    lines: list[str] = []
    lines.append(f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{w:.2f}" height="{h:.2f}" fill="#fbfbfc" stroke="#dddddf"/>')
    lines.append(
        f'<text x="{x0 + w / 2:.2f}" y="{y0 + 24:.2f}" text-anchor="middle" '
        'fill="#333333" font-family="Helvetica, Arial, sans-serif" font-size="18" font-weight="600">'
        f"{title}</text>"
    )

    proj: list[dict[str, Any]] = []
    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")

    for tri in triangles:
        rv1 = _rotate(tri[0], yaw_deg, pitch_deg, roll_deg)
        rv2 = _rotate(tri[1], yaw_deg, pitch_deg, roll_deg)
        rv3 = _rotate(tri[2], yaw_deg, pitch_deg, roll_deg)
        p1 = (rv1[0], rv1[1])
        p2 = (rv2[0], rv2[1])
        p3 = (rv3[0], rv3[1])
        min_x = min(min_x, p1[0], p2[0], p3[0])
        max_x = max(max_x, p1[0], p2[0], p3[0])
        min_y = min(min_y, p1[1], p2[1], p3[1])
        max_y = max(max_y, p1[1], p2[1], p3[1])
        proj.append(
            {
                "pts": (p1, p2, p3),
                "depth": (rv1[2] + rv2[2] + rv3[2]) / 3.0,
                "intensity": _face_intensity(rv1, rv2, rv3),
            }
        )

    dx = max(1e-6, max_x - min_x)
    dy = max(1e-6, max_y - min_y)
    drawable_w = max(10.0, w - (pad * 2))
    drawable_h = max(10.0, h - 56.0 - pad)
    scale = min(drawable_w / dx, drawable_h / dy)
    origin_x = x0 + (w - dx * scale) / 2.0
    origin_y = y0 + 48.0 + (drawable_h - dy * scale) / 2.0

    # Light grid for visual depth cues.
    for i in range(1, 6):
        gx = origin_x + (i / 6.0) * dx * scale
        gy = origin_y + (i / 6.0) * dy * scale
        lines.append(
            f'<line x1="{gx:.2f}" y1="{origin_y:.2f}" x2="{gx:.2f}" y2="{origin_y + dy * scale:.2f}" '
            'stroke="#ececef" stroke-width="0.8"/>'
        )
        lines.append(
            f'<line x1="{origin_x:.2f}" y1="{gy:.2f}" x2="{origin_x + dx * scale:.2f}" y2="{gy:.2f}" '
            'stroke="#ececef" stroke-width="0.8"/>'
        )

    for item in sorted(proj, key=lambda d: d["depth"]):
        points: list[str] = []
        for px, py in item["pts"]:
            sx = origin_x + (px - min_x) * scale
            sy = origin_y + (max_y - py) * scale
            points.append(f"{sx:.2f},{sy:.2f}")
        fill = _shade_hex(item["intensity"])
        lines.append(
            f'<polygon points="{" ".join(points)}" fill="{fill}" stroke="#b66342" stroke-width="0.35" fill-opacity="0.96"/>'
        )

    return lines


def _rotate(v: tuple[float, float, float], yaw_deg: float, pitch_deg: float, roll_deg: float) -> tuple[float, float, float]:
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    roll = math.radians(roll_deg)

    x, y, z = v

    # Yaw around Z axis.
    cy, sy = math.cos(yaw), math.sin(yaw)
    x, y = x * cy - y * sy, x * sy + y * cy

    # Pitch around X axis.
    cp, sp = math.cos(pitch), math.sin(pitch)
    y, z = y * cp - z * sp, y * sp + z * cp

    # Roll around Y axis.
    cr, sr = math.cos(roll), math.sin(roll)
    x, z = x * cr + z * sr, -x * sr + z * cr

    return (x, y, z)


def _face_intensity(a: tuple[float, float, float], b: tuple[float, float, float], c: tuple[float, float, float]) -> float:
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    n_len = math.sqrt(nx * nx + ny * ny + nz * nz)
    if n_len <= 1e-9:
        return 0.55
    nx, ny, nz = nx / n_len, ny / n_len, nz / n_len

    # Slightly frontal light source.
    lx, ly, lz = (0.28, -0.25, 0.93)
    dot = nx * lx + ny * ly + nz * lz
    return max(0.2, min(1.0, 0.25 + 0.75 * abs(dot)))


def _shade_hex(intensity: float) -> str:
    base = (234, 122, 79)
    r = int(base[0] * (0.55 + 0.45 * intensity))
    g = int(base[1] * (0.55 + 0.45 * intensity))
    b = int(base[2] * (0.55 + 0.45 * intensity))
    return f"#{r:02x}{g:02x}{b:02x}"
