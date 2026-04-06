"""Universal multi-angle 3D model visualization.

Renders any STL, 3MF, or SCAD file from 6 standard camera angles to PNG
images. Works everywhere — CLI, Claude Desktop, MCP over stdio — because
the output is just PNG files that any multimodal client can display.

This is the universal "show me this model from all angles" capability
that agents and humans use before printing. The desktop app can offer
a richer interactive experience on top, but this is the baseline.

Usage::

    from kiln.model_visualizer import visualize_model

    result = visualize_model("/path/to/model.stl")
    for view in result["views"]:
        print(view["angle"], view["path"])
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kiln.emboss_generator import _openscad_version_year, get_openscad_version

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Camera angles — 6 standard views covering all faces
# ---------------------------------------------------------------------------

_CAMERA_ANGLES: list[tuple[str, str]] = [
    # (label, description) — distance is calculated from bounding box
    # Camera rotation: 7-param format rotX,rotY,rotZ
    # rotX,rotY,rotZ applied to default OpenSCAD view (looking down -Z)
    ("isometric", "3/4 overview showing overall shape"),
    ("front", "Front face — check symmetry and proportions"),
    ("right", "Right side — check profile and thickness"),
    ("top", "Top-down — check surface features and logo placement"),
    ("bottom", "Bottom-up — check bed adhesion surface and QR codes"),
    ("back", "Rear face — check for artifacts or missing geometry"),
]

# Rotation presets for each angle: (rotX, rotY, rotZ)
# Top/bottom use 15° tilt to cast shadows on debossed/raised features.
# Straight-down (0°) hides surface detail in mono-color rendering.
_ANGLE_ROTATIONS: dict[str, tuple[float, float, float]] = {
    "isometric": (55, 0, 25),
    "front": (90, 0, 0),
    "right": (90, 0, 90),
    "top": (15, 0, 10),
    "bottom": (170, 0, 15),
    "back": (90, 0, 180),
}

# Default distance when bounding box detection fails
_DEFAULT_DISTANCE = 250

# OpenSCAD binary search order
_OPENSCAD_PATHS = [
    "openscad",  # PATH
    "/opt/homebrew/bin/openscad",
    "/usr/local/bin/openscad",
    "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
    "/Applications/OpenSCAD-2021.01.app/Contents/MacOS/OpenSCAD",
]


def _find_openscad() -> str:
    """Find the OpenSCAD binary."""
    for path in _OPENSCAD_PATHS:
        if shutil.which(path):
            return path
    raise FileNotFoundError(
        "OpenSCAD not found. Install it via `brew install openscad` or "
        "download from https://openscad.org/downloads.html"
    )


def _get_bounding_box(scad_path: str) -> _BoundingBoxInfo:
    """Get model bounding box and return camera center + distance.

    Parses the STL binary header for an exact bounding box.  Falls back
    to default distance centered at origin if detection fails.
    """
    try:
        stl_path = None
        content = Path(scad_path).read_text(encoding="utf-8")
        if 'import("' in content:
            start = content.index('import("') + 8
            end = content.index('"', start)
            stl_path = content[start:end]
        elif scad_path.lower().endswith(".stl"):
            stl_path = scad_path

        if stl_path and os.path.isfile(stl_path) and stl_path.lower().endswith(".stl"):
            return _distance_from_stl(stl_path)

    except Exception:
        logger.debug("Bounding box detection failed", exc_info=True)

    return _BoundingBoxInfo()


@dataclass
class _BoundingBoxInfo:
    """Bounding box with center and optimal camera distance."""

    center_x: float = 0.0
    center_y: float = 0.0
    center_z: float = 0.0
    distance: float = _DEFAULT_DISTANCE


def _bbox_from_ascii_stl(data: bytes) -> _BoundingBoxInfo:
    """Parse ASCII STL vertex lines to compute bounding box."""
    import re

    text = data.decode("utf-8", errors="ignore")
    vertex_re = re.compile(r"vertex\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+"
                           r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+"
                           r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)")

    min_xyz = [float("inf")] * 3
    max_xyz = [float("-inf")] * 3
    count = 0

    for m in vertex_re.finditer(text):
        x, y, z = float(m.group(1)), float(m.group(2)), float(m.group(3))
        min_xyz[0] = min(min_xyz[0], x)
        min_xyz[1] = min(min_xyz[1], y)
        min_xyz[2] = min(min_xyz[2], z)
        max_xyz[0] = max(max_xyz[0], x)
        max_xyz[1] = max(max_xyz[1], y)
        max_xyz[2] = max(max_xyz[2], z)
        count += 1

    if count == 0:
        return _BoundingBoxInfo()

    dx = max_xyz[0] - min_xyz[0]
    dy = max_xyz[1] - min_xyz[1]
    dz = max_xyz[2] - min_xyz[2]

    import math

    diagonal = math.sqrt(dx * dx + dy * dy + dz * dz)
    distance = max(50.0, min(diagonal * 2.0, 5000.0))

    return _BoundingBoxInfo(
        center_x=(min_xyz[0] + max_xyz[0]) / 2.0,
        center_y=(min_xyz[1] + max_xyz[1]) / 2.0,
        center_z=(min_xyz[2] + max_xyz[2]) / 2.0,
        distance=distance,
    )


def _distance_from_stl(stl_path: str) -> _BoundingBoxInfo:
    """Calculate optimal camera distance and center from an STL bounding box."""
    import struct

    data = Path(stl_path).read_bytes()
    if len(data) < 84:
        return _BoundingBoxInfo()

    # Detect ASCII STL: starts with "solid" AND binary size check fails.
    num_triangles = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + num_triangles * 50
    if data[:5] == b"solid" and len(data) != expected:
        # ASCII STL — parse vertex lines for bounding box.
        return _bbox_from_ascii_stl(data)

    if len(data) < expected:
        return _BoundingBoxInfo()

    min_xyz = [float("inf")] * 3
    max_xyz = [float("-inf")] * 3

    for i in range(num_triangles):
        offset = 84 + i * 50 + 12  # skip normal (12 bytes)
        for v in range(3):
            voff = offset + v * 12
            x, y, z = struct.unpack_from("<fff", data, voff)
            min_xyz[0] = min(min_xyz[0], x)
            min_xyz[1] = min(min_xyz[1], y)
            min_xyz[2] = min(min_xyz[2], z)
            max_xyz[0] = max(max_xyz[0], x)
            max_xyz[1] = max(max_xyz[1], y)
            max_xyz[2] = max(max_xyz[2], z)

    dx = max_xyz[0] - min_xyz[0]
    dy = max_xyz[1] - min_xyz[1]
    dz = max_xyz[2] - min_xyz[2]

    # Center of the bounding box — camera target
    cx = (min_xyz[0] + max_xyz[0]) / 2.0
    cy = (min_xyz[1] + max_xyz[1]) / 2.0
    cz = (min_xyz[2] + max_xyz[2]) / 2.0

    # Use the 3D diagonal (not just max single axis) so elongated shapes
    # aren't clipped.  FOV ~22.5° → distance ≈ diagonal * 2.0 fills ~65%
    # with comfortable margin on all sides.
    import math

    diagonal = math.sqrt(dx * dx + dy * dy + dz * dz)
    distance = max(50.0, min(diagonal * 2.0, 5000.0))

    return _BoundingBoxInfo(
        center_x=cx, center_y=cy, center_z=cz,
        distance=distance,
    )


def _make_scad_wrapper(
    model_path: str,
    *,
    color: str = "#AAAAAA",
    bbox: _BoundingBoxInfo | None = None,
) -> str:
    """Create a temporary .scad file that imports the model centered at origin.

    For STL/OBJ: uses import() with color(), translated so the bounding
    box center sits at the origin.  This ensures ``--viewall`` and manual
    camera distances frame the model correctly regardless of where the
    original geometry was placed.

    For SCAD: returns the file path directly (no wrapper needed).

    :param color: Hex color string (e.g. "#F72323" for red) or named color.
    :param bbox: Bounding box info for centering.  When ``None`` the model
        is imported without translation (legacy behavior).
    """
    ext = Path(model_path).suffix.lower()

    if ext == ".scad":
        return model_path  # OpenSCAD can render directly

    escaped = model_path.replace("\\", "\\\\").replace('"', '\\"')
    safe_color = color.replace('"', '\\"')
    fd, scad_path = tempfile.mkstemp(suffix=".scad", prefix="kiln_viz_")

    # Center the model at origin so camera framing works for any geometry.
    if bbox and (bbox.center_x != 0 or bbox.center_y != 0 or bbox.center_z != 0):
        tx = -bbox.center_x
        ty = -bbox.center_y
        tz = -bbox.center_z
        with os.fdopen(fd, "w") as fh:
            fh.write(
                f'color("{safe_color}") '
                f'translate([{tx:.2f},{ty:.2f},{tz:.2f}]) '
                f'import("{escaped}");\n'
            )
    else:
        with os.fdopen(fd, "w") as fh:
            fh.write(f'color("{safe_color}") import("{escaped}");\n')

    return scad_path


def visualize_model(
    file_path: str,
    *,
    output_dir: str | None = None,
    width: int = 800,
    height: int = 600,
    angles: list[str] | None = None,
    color: str = "",
    timeout: int = 120,
) -> dict:
    """Render a 3D model from multiple camera angles.

    Args:
        file_path: Path to an STL, 3MF, OBJ, or SCAD file.
        output_dir: Directory for output PNGs. Defaults to a temp dir.
        width: Image width in pixels.
        height: Image height in pixels.
        angles: Subset of angle labels to render (e.g. ["top", "bottom"]).
            Defaults to all 6 standard angles.
        timeout: Max seconds per OpenSCAD render.

    Returns:
        Dict with ``success``, ``views`` list, ``output_dir``, and metadata.
    """
    file_path = os.path.abspath(file_path)
    if not os.path.isfile(file_path):
        return {
            "success": False,
            "error": f"File not found: {file_path}",
            "code": "FILE_NOT_FOUND",
        }

    ext = Path(file_path).suffix.lower()
    supported = {".stl", ".obj", ".scad", ".3mf"}
    if ext not in supported:
        return {
            "success": False,
            "error": f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(supported))}",
            "code": "UNSUPPORTED_FORMAT",
        }

    try:
        openscad = _find_openscad()
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc), "code": "OPENSCAD_NOT_FOUND"}

    # Detect version once for manifold flag — safe to fail
    _use_manifold = False
    try:
        _ver = get_openscad_version(openscad)
        _use_manifold = _openscad_version_year(_ver) >= 2024
    except Exception:  # noqa: BLE001
        pass

    # Select angles
    if angles:
        angle_set = {a.lower() for a in angles}
        selected = [a for a in _CAMERA_ANGLES if a[0] in angle_set]
        if not selected:
            return {
                "success": False,
                "error": f"No valid angles in {angles}. Valid: {[a[0] for a in _CAMERA_ANGLES]}",
                "code": "INVALID_ANGLES",
            }
    else:
        selected = _CAMERA_ANGLES

    # Output directory
    if output_dir is None:
        output_dir = os.path.join(tempfile.gettempdir(), "kiln_visualizations")
    os.makedirs(output_dir, mode=0o700, exist_ok=True)

    # Compute bounding box BEFORE creating the wrapper (needs the raw STL).
    # We use it both to center the model in the wrapper and to set camera distance.
    render_color = color if color else "#AAAAAA"

    # Pre-compute bbox from raw file for centering.
    _raw_scad = _make_scad_wrapper(file_path, color=render_color)
    bbox = _get_bounding_box(_raw_scad)
    if _raw_scad != file_path:
        with contextlib.suppress(OSError):
            os.unlink(_raw_scad)

    # Create the centered wrapper using the bbox.
    scad_path = _make_scad_wrapper(file_path, color=render_color, bbox=bbox)
    is_wrapper = scad_path != file_path

    logger.debug(
        "Camera: bbox_center=(%.1f,%.1f,%.1f) distance=%.1f (model centered at origin)",
        bbox.center_x, bbox.center_y, bbox.center_z, bbox.distance,
    )

    try:
        views: list[dict] = []
        stem = Path(file_path).stem

        for label, description in selected:
            rx, ry, rz = _ANGLE_ROTATIONS[label]
            # Model is now centered at origin via translate in the wrapper,
            # so camera targets 0,0,0 with the computed distance.
            camera = f"0,0,0,{rx},{ry},{rz},{bbox.distance:.0f}"
            png_path = os.path.join(output_dir, f"{stem}_{label}.png")

            cmd = [
                openscad,
                "--preview",
                "-o", png_path,
                f"--imgsize={width},{height}",
                f"--camera={camera}",
                "--viewall",
                "--colorscheme=DeepOcean",
            ]
            if _use_manifold:
                cmd.append("--backend=manifold")
            cmd.append(scad_path)

            logger.debug("Rendering %s view: %s", label, " ".join(cmd))

            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                views.append({
                    "angle": label,
                    "description": description,
                    "path": None,
                    "error": f"Render timed out after {timeout}s",
                })
                continue

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()[:200]
                views.append({
                    "angle": label,
                    "description": description,
                    "path": None,
                    "error": f"OpenSCAD failed (exit {result.returncode}): {stderr}",
                })
                continue

            if not os.path.isfile(png_path) or os.path.getsize(png_path) == 0:
                views.append({
                    "angle": label,
                    "description": description,
                    "path": None,
                    "error": "Render produced empty output",
                })
                continue

            views.append({
                "angle": label,
                "description": description,
                "path": png_path,
            })

        successful = [v for v in views if v.get("path")]
        failed = [v for v in views if not v.get("path")]

        return {
            "success": len(successful) > 0,
            "views": views,
            "output_dir": output_dir,
            "file_path": file_path,
            "file_type": ext,
            "rendered": len(successful),
            "failed": len(failed),
            "message": (
                f"Rendered {len(successful)}/{len(views)} angles for {Path(file_path).name}. "
                + (
                    "View ALL angles to check: shape, proportions, surface features, "
                    "bottom flatness, and any artifacts before printing."
                    if len(successful) == len(views)
                    else f"{len(failed)} angle(s) failed to render."
                )
            ),
        }

    finally:
        if is_wrapper:
            with contextlib.suppress(OSError):
                os.unlink(scad_path)
