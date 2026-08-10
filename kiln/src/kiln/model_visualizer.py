"""Universal multi-angle 3D model visualization.

Renders any STL, 3MF, or SCAD file from 6 standard camera angles to PNG
images. Works everywhere — CLI, Claude Desktop, MCP over stdio — because
the output is just PNG files that any multimodal client can display.

This is the universal "show me this model from all angles" capability
that agents and humans use before printing.

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
import zipfile
from dataclasses import dataclass
from pathlib import Path

from kiln.emboss_generator import _openscad_version_year, get_openscad_version
from kiln.preview_render import downscale_png, effective_supersample

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bambu-wrapped 3MF thumbnail fallback
# ---------------------------------------------------------------------------
#
# When Kiln wraps PrusaSlicer gcode in a Bambu-compatible 3MF for upload
# to a Bambu printer, the archive's ``3D/3dmodel.model`` is a minimal
# XML placeholder — the real print payload is ``Metadata/plate_1.gcode``.
# OpenSCAD renders such a 3MF as an empty black frame because there's
# no mesh to render, which breaks the preview gate for every Bambu
# print (the agent can't show the user what's about to print).
#
# The slicer already embeds high-quality thumbnails of the sliced plate
# in the archive.  They are the same images the Bambu LCD shows, so
# surfacing them through ``visualize_model`` gives the user a faithful
# preview of the physical output without needing a working mesh render.

_BAMBU_THUMBNAIL_MAP: list[tuple[str, str, str]] = [
    # (archive_member, angle_label, description)
    (
        "Auxiliaries/.thumbnails/thumbnail_middle.png",
        "isometric",
        "Slicer plate thumbnail (Bambu LCD preview) — 3/4 overview",
    ),
    (
        "Metadata/top_1.png",
        "top",
        "Slicer top-down plate render",
    ),
    (
        "Metadata/plate_1.png",
        "front",
        "Slicer plate render (primary)",
    ),
    (
        "Auxiliaries/.thumbnails/thumbnail_small.png",
        "bottom",
        "Slicer small plate thumbnail (fallback)",
    ),
]

# A real mesh embedded in 3D/3dmodel.model is usually tens to hundreds
# of KB of XML.  The Bambu wrapper's placeholder is ~1-2 KB.  Anything
# below this threshold triggers the embedded-thumbnail path.
_BAMBU_PLACEHOLDER_MODEL_MAX_BYTES = 4096


def _is_bambu_wrapped_3mf(file_path: str) -> bool:
    """Return True when the 3MF at *file_path* looks like a Bambu wrapper.

    Heuristic: archive contains ``Metadata/plate_1.gcode`` (the actual
    print payload for Bambu) AND the ``3D/3dmodel.model`` entry is
    below ``_BAMBU_PLACEHOLDER_MODEL_MAX_BYTES`` (just a format-
    compliance stub, not real geometry).  Both signals together
    distinguish a Kiln/BambuStudio slicer output from a geometry-
    carrying 3MF like those produced by Fusion or exported for
    PrusaSlicer input.
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            members = set(zf.namelist())
            if "Metadata/plate_1.gcode" not in members:
                return False
            if "3D/3dmodel.model" not in members:
                return False
            return zf.getinfo("3D/3dmodel.model").file_size <= _BAMBU_PLACEHOLDER_MODEL_MAX_BYTES
    except (zipfile.BadZipFile, KeyError, OSError):
        return False


def _extract_bambu_thumbnails(
    file_path: str,
    output_dir: str,
    angles: list[str] | None = None,
) -> list[dict]:
    """Extract the slicer-embedded thumbnails from a Bambu-wrapped 3MF.

    Returns a list of view dicts compatible with the ``visualize_model``
    response shape.  Only members that exist in the archive contribute
    views — a missing thumbnail is skipped rather than faked.  When
    *angles* is supplied, thumbnails are filtered to that subset.
    """
    os.makedirs(output_dir, mode=0o700, exist_ok=True)
    stem = Path(file_path).stem
    wanted_angles = {a.lower() for a in angles} if angles else None

    out: list[dict] = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            members = set(zf.namelist())
            for member, angle_label, description in _BAMBU_THUMBNAIL_MAP:
                if member not in members:
                    continue
                if wanted_angles is not None and angle_label not in wanted_angles:
                    continue
                # Materialise to a predictable path under output_dir so
                # the caller can Read the image directly.
                png_path = os.path.join(output_dir, f"{stem}_{angle_label}.png")
                with zf.open(member) as src, open(png_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                out.append({
                    "angle": angle_label,
                    "description": description,
                    "path": png_path,
                    "source": "bambu_3mf_thumbnail",
                    "archive_member": member,
                })
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("Bambu 3MF thumbnail extract failed for %s: %s", file_path, exc)
    return out

# ---------------------------------------------------------------------------
# Camera angles — 6 standard views covering all faces
# ---------------------------------------------------------------------------

_CAMERA_ANGLES: list[tuple[str, str]] = [
    # (label, description) — distance is calculated from bounding box
    # Camera rotation: 7-param format rotX,rotY,rotZ
    # rotX,rotY,rotZ applied to default OpenSCAD view (looking down -Z)
    ("isometric", "3/4 overview showing overall shape"),
    ("wedge_iso", "Pitched-up 3/4 view — optimised for nameplates and angled-canvas products"),
    ("front", "Front face — check symmetry and proportions"),
    ("right", "Right side — check profile and thickness"),
    ("top", "Top-down — check surface features and logo placement"),
    ("bottom", "Bottom-up — check bed adhesion surface and QR codes"),
    ("back", "Rear face — check for artifacts or missing geometry"),
]

# Rotation presets for each angle: (rotX, rotY, rotZ)
# Top/bottom use 15° tilt to cast shadows on debossed/raised features.
# Straight-down (0°) hides surface detail in mono-color rendering.
# wedge_iso: pitched-up 3/4 view (rz=35) optimised for tilted-canvas
# products (nameplates, awards, desk signs) — shows the angled face,
# the slope, AND the triangular side so the user reads "this is 3D"
# at a glance.  Default isometric (rz=25) flattens nameplates because
# the camera is too straight-on relative to the wedge orientation.
_ANGLE_ROTATIONS: dict[str, tuple[float, float, float]] = {
    "isometric": (55, 0, 25),
    "wedge_iso": (55, 0, 35),
    "front": (90, 0, 0),
    "right": (90, 0, 90),
    "top": (15, 0, 10),
    "bottom": (170, 0, 15),
    "back": (90, 0, 180),
}

# Default distance when bounding box detection fails
_DEFAULT_DISTANCE = 250


# Aspect-ratio threshold below which a model is "flat" — for these the
# pure-horizontal front/right/back views degenerate to a thin strip and
# hide all top-surface decoration.  We tilt them up by FLAT_TILT_DEGREES
# so the top face is visible from the side angle.
_FLAT_ASPECT_RATIO = 0.3
_FLAT_TILT_DEGREES = 35

# Aspect-ratio threshold above which a model is "tall" — we steepen the
# top/bottom views so flat tops don't look like circles.
_TALL_ASPECT_RATIO = 1.6


def _adapt_angles_to_bbox(
    bbox: _BoundingBoxInfo,
) -> dict[str, tuple[float, float, float]]:
    """Return angle rotations adjusted for the model's aspect ratio.

    - **Flat models** (z < 0.3 × max(x,y)): pure-horizontal front/right/
      back views show only a thin strip and hide top decoration.  Tilt
      them ``_FLAT_TILT_DEGREES`` toward the top so the decorated face is
      visible from every angle.  Previously these rendered as
      uninformative slabs of background color — exactly the failure the
      jewelry-tray decoration debug session surfaced.
    - **Tall models** (z > 1.6 × max(x,y)): steepen top/bottom to 30°
      tilt so the cylinder caps don't appear as flat circles.
    - **Cubic-ish models**: keep the defaults.

    Isometric is always a 3/4 overview regardless of aspect ratio.
    """
    dx = max(1e-6, bbox.dx)
    dy = max(1e-6, bbox.dy)
    dz = max(1e-6, bbox.dz)
    planar_max = max(dx, dy)
    aspect = dz / planar_max

    angles = dict(_ANGLE_ROTATIONS)

    if aspect < _FLAT_ASPECT_RATIO:
        # Flat — shift horizontal views to a near-top oblique.
        tilt = 90 - _FLAT_TILT_DEGREES  # e.g. 55° from vertical
        angles["front"] = (tilt, 0, 0)
        angles["right"] = (tilt, 0, 90)
        angles["back"] = (tilt, 0, 180)
    elif aspect > _TALL_ASPECT_RATIO:
        # Tall — pull top/bottom further off-axis to catch detail.
        angles["top"] = (30, 0, 10)
        angles["bottom"] = (150, 0, 15)

    return angles

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
        "OpenSCAD not found. Install the current build: "
        "`brew install --cask openscad@snapshot` (macOS) "
        "or https://openscad.org/downloads#snapshots"
    )


def _get_bounding_box(scad_path: str) -> _BoundingBoxInfo:
    """Get model bounding box and return camera center + distance.

    For STL files: parses the binary/ASCII STL header directly.
    For SCAD files with ``import("...")``: reads the embedded STL path.
    For pure parametric SCAD: compiles to a temp STL via OpenSCAD to get
    the real bounding box.  Falls back to default distance if all else fails.
    """
    try:
        stl_path = None
        if scad_path.lower().endswith(".stl"):
            stl_path = scad_path
        elif scad_path.lower().endswith(".scad"):
            content = Path(scad_path).read_text(encoding="utf-8")
            if 'import("' in content:
                start = content.index('import("') + 8
                end = content.index('"', start)
                stl_path = content[start:end]
            else:
                # Pure parametric SCAD — compile to temp STL to measure bbox.
                stl_path = _compile_scad_for_bbox(scad_path)

        if stl_path and os.path.isfile(stl_path) and stl_path.lower().endswith(".stl"):
            return _distance_from_stl(stl_path)

    except Exception:
        logger.debug("Bounding box detection failed", exc_info=True)

    return _BoundingBoxInfo()


def _compile_scad_for_bbox(scad_path: str) -> str | None:
    """Compile a SCAD file to a temporary STL for bounding-box measurement.

    Returns the temp STL path on success, None on failure.  The caller is
    responsible for cleanup — the temp file is created with ``delete=False``
    so it survives the subprocess boundary.
    """
    import tempfile

    try:
        openscad = _find_openscad()
    except FileNotFoundError:
        return None

    fd, tmp_stl = tempfile.mkstemp(suffix=".stl", prefix="kiln_bbox_")
    os.close(fd)
    try:
        result = subprocess.run(
            [openscad, "--export-format", "binstl", "-o", tmp_stl, scad_path],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0 and os.path.getsize(tmp_stl) > 84:
            return tmp_stl
    except Exception:
        logger.debug("SCAD bbox compile failed", exc_info=True)
    finally:
        # Clean up temp file if compilation failed or produced empty output.
        if not (os.path.exists(tmp_stl) and os.path.getsize(tmp_stl) > 84):
            with contextlib.suppress(OSError):
                os.unlink(tmp_stl)
    return None


@dataclass
class _BoundingBoxInfo:
    """Bounding box with center, optimal camera distance, and raw extents.

    ``dx``/``dy``/``dz`` are the per-axis extents (max − min).  They drive
    aspect-ratio-adaptive angle selection in :func:`_adapt_angles_to_bbox`
    so flat models (like a jewelry tray) don't render pure-horizontal
    views as uninformative strips.
    """

    center_x: float = 0.0
    center_y: float = 0.0
    center_z: float = 0.0
    distance: float = _DEFAULT_DISTANCE
    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0


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
        dx=dx, dy=dy, dz=dz,
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
        dx=dx, dy=dy, dz=dz,
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
    """Primary 3D preview tool — renders high-quality PNGs via OpenSCAD.

    Preferred over ``render_multi_view_preview()`` (which produces
    lightweight SVGs). Use this for all user-facing model previews.

    Render a 3D model from multiple camera angles.

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

    # ------------------------------------------------------------------
    # Colored 3MF fast path — use PIL-based renderer when per-face
    # colors are present (OpenSCAD cannot render per-face colors).
    # ------------------------------------------------------------------
    if ext == ".3mf":
        try:
            from kiln.colored_renderer import render_colored_mesh_multi_angle
            from kiln.threemf_parser import parse_colored_3mf

            mesh = parse_colored_3mf(file_path)
            if mesh.colors_found:
                logger.debug(
                    "3MF has per-face colors (%d unique) — using colored renderer",
                    mesh.color_count,
                )
                colored_views = render_colored_mesh_multi_angle(
                    mesh.triangles,
                    output_dir=output_dir,
                    width=width,
                    height=height,
                    angles=angles,
                )
                successful = [v for v in colored_views if v.get("path")]
                failed = [v for v in colored_views if not v.get("path")]
                return {
                    "success": len(successful) > 0,
                    "views": colored_views,
                    "output_dir": output_dir or os.path.join(
                        tempfile.gettempdir(), "kiln_visualizations",
                    ),
                    "file_path": file_path,
                    "file_type": ext,
                    # Every success envelope names its engine, or a caller
                    # that branches on this key crashes on whichever path
                    # forgot it.  This one is the PIL painter's-algorithm
                    # renderer, not OpenSCAD and not the stage.
                    "renderer": "colored_mesh",
                    "rendered": len(successful),
                    "failed": len(failed),
                    "message": (
                        f"Rendered {len(successful)}/{len(colored_views)} angles "
                        f"for {Path(file_path).name} with per-face colors. "
                        + (
                            "View ALL angles to check: shape, proportions, surface "
                            "features, bottom flatness, and color placement."
                            if len(successful) == len(colored_views)
                            else f"{len(failed)} angle(s) failed to render."
                        )
                    ),
                }
            # No colors — fall through to OpenSCAD for uniform gray render
            logger.debug("3MF has no per-face colors — falling through to OpenSCAD")
        except ImportError:
            logger.debug("Colored renderer not available — falling through to OpenSCAD")
        except Exception:  # noqa: BLE001
            logger.debug("Colored 3MF parse/render failed — falling through to OpenSCAD", exc_info=True)

        # Bambu-wrapped 3MF path: the archive's 3D/3dmodel.model is a
        # format-compliance placeholder; the real payload is gcode plus
        # embedded thumbnails.  OpenSCAD would render a black frame, so
        # we short-circuit to the slicer-rendered plate thumbnails —
        # these are the same images the Bambu LCD shows the user, which
        # is exactly what the preview gate is asking to confirm.
        if _is_bambu_wrapped_3mf(file_path):
            logger.debug("3MF is Bambu-wrapped — extracting slicer thumbnails")
            thumb_out_dir = output_dir or os.path.join(
                tempfile.gettempdir(), "kiln_visualizations",
            )
            bambu_views = _extract_bambu_thumbnails(
                file_path, thumb_out_dir, angles=angles,
            )
            if bambu_views:
                return {
                    "success": True,
                    "views": bambu_views,
                    "output_dir": thumb_out_dir,
                    "file_path": file_path,
                    "file_type": ext,
                    # Not a render at all — these are the slicer's own
                    # embedded plate images, so say that rather than
                    # claiming an engine drew them.
                    "renderer": "slicer_thumbnails",
                    "rendered": len(bambu_views),
                    "failed": 0,
                    "source": "bambu_3mf_thumbnails",
                    "message": (
                        f"Surfaced {len(bambu_views)} slicer-embedded "
                        f"thumbnail(s) for {Path(file_path).name}.  This 3MF "
                        "wraps gcode for Bambu firmware; its 3D/3dmodel.model "
                        "entry is a placeholder, so the slicer's own plate "
                        "renders are the faithful preview.  Same images the "
                        "Bambu LCD shows."
                    ),
                }
            # Fall through — no thumbnails extractable.  OpenSCAD will
            # produce an empty frame, but that's better than returning
            # nothing; the caller still gets a structured response.
            logger.warning(
                "Bambu 3MF has no usable embedded thumbnails: %s",
                file_path,
            )

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

    # Aspect-ratio-adaptive angle selection: flat models tilt side views
    # up so the top decoration is visible; tall models steepen top/bottom.
    angle_rotations = _adapt_angles_to_bbox(bbox)

    logger.debug(
        "Camera: bbox_center=(%.1f,%.1f,%.1f) dims=(%.1fx%.1fx%.1f) "
        "distance=%.1f aspect_z=%.2f",
        bbox.center_x, bbox.center_y, bbox.center_z,
        bbox.dx, bbox.dy, bbox.dz,
        bbox.distance,
        bbox.dz / max(1e-6, max(bbox.dx, bbox.dy)),
    )

    # Supersample: render oversized, then Lanczos-downscale to the
    # requested size for crisp, anti-aliased edges (shared helper so one
    # knob governs every OpenSCAD preview surface). effective_supersample
    # degrades to 1 without Pillow, so output is always the requested size.
    ss = effective_supersample()
    img_w, img_h = width * ss, height * ss

    try:
        views: list[dict] = []
        stem = Path(file_path).stem

        # Stage-look backend: photograph the same three.js stage the web
        # viewer and inline conversation viewer render, when this machine
        # can (a chromium-family browser + a still-capable cached stage
        # document — see kiln.stage_still).  Any miss falls through to
        # the OpenSCAD loop below unchanged, which also remains the only
        # renderer for caller-specified colors (the stage ignores them)
        # and for machines with no browser.  All-or-nothing per result:
        # one preview never mixes two looks.
        used_stage = False
        from kiln.stage_still import try_render_stage_views

        stage_views = try_render_stage_views(
            file_path,
            selected,
            angle_rotations,
            output_dir=output_dir,
            width=width,
            height=height,
            color=color,
        )
        if stage_views:
            views = stage_views
            used_stage = True

        # Nothing left to draw when the stage already produced every view.
        openscad_views = [] if used_stage else selected
        for label, description in openscad_views:
            rx, ry, rz = angle_rotations[label]
            # Model is now centered at origin via translate in the wrapper,
            # so camera targets 0,0,0 with the computed distance.
            camera = f"0,0,0,{rx},{ry},{rz},{bbox.distance:.0f}"
            png_path = os.path.join(output_dir, f"{stem}_{label}.png")

            cmd = [
                openscad,
                "--preview",
                "-o", png_path,
                f"--imgsize={img_w},{img_h}",
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
                # Logged, not just returned: the returned dict dies with
                # the tool call, and this failure class is exactly what a
                # later bug report needs a durable record of.
                logger.error(
                    "OpenSCAD render timed out after %ss (view %s, %s)",
                    timeout, label, scad_path,
                )
                views.append({
                    "angle": label,
                    "description": description,
                    "path": None,
                    "error": f"Render timed out after {timeout}s",
                })
                continue

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()[:200]
                logger.error(
                    "OpenSCAD render failed (exit %s, view %s, %s): %s",
                    result.returncode, label, scad_path, stderr,
                )
                views.append({
                    "angle": label,
                    "description": description,
                    "path": None,
                    "error": f"OpenSCAD failed (exit {result.returncode}): {stderr}",
                })
                continue

            if not os.path.isfile(png_path) or os.path.getsize(png_path) == 0:
                logger.error(
                    "OpenSCAD render produced empty output (view %s, %s)",
                    label, scad_path,
                )
                views.append({
                    "angle": label,
                    "description": description,
                    "path": None,
                    "error": "Render produced empty output",
                })
                continue

            # Downscale the oversized render to the requested size for
            # crisp edges. On the rare downscale failure (I/O fault) the
            # oversized-but-valid image is kept rather than lost.
            if ss > 1:
                downscale_png(png_path, width, height)

            views.append({
                "angle": label,
                "description": description,
                "path": png_path,
            })

        successful = [v for v in views if v.get("path")]
        failed = [v for v in views if not v.get("path")]

        # A preview is a picture of a 3D thing; the stage is the thing.  One
        # wire here gives every caller that hands this result back the
        # turn-it-over link instead of each tool remembering to ask.  Cost is
        # bounded by the content-addressed cache in stage_link: a sixteen-pose
        # inspection sheet of one mesh uploads once, and re-rendering an
        # unchanged design uploads not at all.
        #
        # The link rides the ENVELOPE, not the view dicts inside it.  So a
        # caller that keeps one entry out of ``views`` and drops the rest of
        # this result does NOT inherit the link, and neither does one that
        # serves a render from its own cache without calling here at all.
        # Those attach their own by mesh path — the link is a fact about the
        # bytes, not about any particular render of them.
        result = {
            "success": len(successful) > 0,
            "views": views,
            "output_dir": output_dir,
            "file_path": file_path,
            "file_type": ext,
            # Which engine produced the pixels — "stage" is the browser
            # photograph of the shared three.js stage, "openscad" the
            # canonical fallback.  Agents and tests branch on this.
            "renderer": "stage" if used_stage else "openscad",
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
        from kiln.stage_link import attach_stage_link

        return attach_stage_link(result, file_path)

    finally:
        if is_wrapper:
            with contextlib.suppress(OSError):
                os.unlink(scad_path)


# ---------------------------------------------------------------------------
# Side-by-side comparison rendering
# ---------------------------------------------------------------------------

_LABEL_HEIGHT = 30
_LABEL_BG = (51, 51, 51)  # #333333
_LABEL_FG = (255, 255, 255)
_DEFAULT_LABELS = ["A", "B", "C", "D"]


def compare_renders(
    paths: list[str],
    *,
    labels: list[str] | None = None,
    angle: str = "isometric",
    width: int = 800,
    height: int = 600,
    colors: list[str] | None = None,
    output_path: str | None = None,
    timeout: int = 120,
) -> dict:
    """Render 2-4 models side by side in a single comparison image.

    Each model is rendered at the same camera angle using
    :func:`visualize_model`, then stitched into a single PNG with text
    labels beneath each render.  Useful for comparing texture variants,
    design iterations, before/after decoration, or material options.

    :param paths: 2-4 file paths (STL, 3MF, OBJ, or SCAD).
    :param labels: Custom labels for each model.  Defaults to A, B, C, D.
    :param angle: Camera angle for all renders.  One of ``isometric``,
        ``front``, ``right``, ``top``, ``bottom``, ``back``.
    :param width: Per-model image width in pixels.
    :param height: Per-model image height in pixels.
    :param colors: Optional hex color per model (e.g. ``["#F72323", "#2323F7"]``).
    :param output_path: Path for the final comparison PNG.  Defaults to a
        temp file.
    :param timeout: Max seconds per individual OpenSCAD render.
    :returns: Dict with ``success``, ``comparison_path``, ``models`` list,
        and metadata.
    """
    # --- Validate inputs ---------------------------------------------------
    if not isinstance(paths, list) or len(paths) < 2:
        return {
            "success": False,
            "error": "compare_renders requires 2-4 file paths.",
            "code": "INVALID_COUNT",
        }
    if len(paths) > 4:
        return {
            "success": False,
            "error": f"Too many paths ({len(paths)}). Maximum is 4.",
            "code": "INVALID_COUNT",
        }

    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        return {
            "success": False,
            "error": f"File(s) not found: {', '.join(missing)}",
            "code": "FILE_NOT_FOUND",
        }

    valid_angles = {a[0] for a in _CAMERA_ANGLES}
    if angle.lower() not in valid_angles:
        return {
            "success": False,
            "error": f"Invalid angle '{angle}'. Valid: {sorted(valid_angles)}",
            "code": "INVALID_ANGLE",
        }

    use_labels = list(labels) if labels else _DEFAULT_LABELS[: len(paths)]
    if len(use_labels) < len(paths):
        # Pad with defaults if caller provided fewer labels than paths
        use_labels.extend(_DEFAULT_LABELS[len(use_labels) : len(paths)])

    use_colors = list(colors) if colors else [""] * len(paths)
    if len(use_colors) < len(paths):
        use_colors.extend([""] * (len(paths) - len(use_colors)))

    # --- Render each model individually ------------------------------------
    models: list[dict] = []
    render_paths: list[str | None] = []

    for idx, fpath in enumerate(paths):
        color_kwarg: dict[str, str] = {}
        if use_colors[idx]:
            color_kwarg["color"] = use_colors[idx]

        result = visualize_model(
            fpath,
            angles=[angle.lower()],
            width=width,
            height=height,
            timeout=timeout,
            **color_kwarg,
        )

        model_info: dict = {
            "path": fpath,
            "label": use_labels[idx],
            "render_path": None,
            "error": None,
        }

        if result.get("success") and result.get("views"):
            view = result["views"][0]
            model_info["render_path"] = view.get("path")
            if not view.get("path"):
                model_info["error"] = view.get("error", "Render failed")
        else:
            model_info["error"] = result.get("error", "Render failed")

        models.append(model_info)
        render_paths.append(model_info["render_path"])

    successful_renders = [p for p in render_paths if p is not None]
    if not successful_renders:
        return {
            "success": False,
            "error": "All renders failed. Check that OpenSCAD is installed.",
            "code": "RENDER_ERROR",
            "models": models,
        }

    # --- Stitch into a single comparison image -----------------------------
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: I001
    except ImportError:
        # PIL not available — return individual paths as fallback
        return {
            "success": True,
            "comparison_path": None,
            "models": models,
            "angle": angle.lower(),
            "width": width,
            "height": height,
            "stitched": False,
            "note": (
                "Pillow is not installed — returning individual render paths. "
                "Install with: pip install Pillow"
            ),
            "message": (
                f"Rendered {len(successful_renders)}/{len(paths)} models at "
                f"'{angle}' angle. Install Pillow to get a single comparison image."
            ),
        }

    n = len(paths)
    # 2x2 grid for 4 models, otherwise single row
    if n == 4:
        cols, rows = 2, 2
    else:
        cols, rows = n, 1

    canvas_w = cols * width
    canvas_h = rows * (height + _LABEL_HEIGHT)
    canvas = Image.new("RGB", (canvas_w, canvas_h), color=(0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # Load a default font for labels — try platform-appropriate paths
    font = None
    for font_name in (
        "/System/Library/Fonts/Helvetica.ttc",  # macOS
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux
        "Arial",  # Windows / fallback
    ):
        try:
            font = ImageFont.truetype(font_name, 16)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    for idx in range(n):
        if n == 4:
            col = idx % 2
            row = idx // 2
        else:
            col = idx
            row = 0

        x_offset = col * width
        y_offset = row * (height + _LABEL_HEIGHT)

        # Paste render or draw placeholder
        rpath = render_paths[idx]
        if rpath and os.path.isfile(rpath):
            with Image.open(rpath) as img:
                img = img.convert("RGB")
                if img.size != (width, height):
                    resample = getattr(Image, "Resampling", Image).LANCZOS
                    img = img.resize((width, height), resample)
                canvas.paste(img, (x_offset, y_offset))
        else:
            # Dark placeholder for failed renders
            draw.rectangle(
                [x_offset, y_offset, x_offset + width, y_offset + height],
                fill=(30, 30, 30),
            )
            draw.text(
                (x_offset + width // 2, y_offset + height // 2),
                "render failed",
                fill=(100, 100, 100),
                font=font,
                anchor="mm",
            )

        # Draw label strip
        label_y = y_offset + height
        draw.rectangle(
            [x_offset, label_y, x_offset + width, label_y + _LABEL_HEIGHT],
            fill=_LABEL_BG,
        )
        label_text = use_labels[idx] if idx < len(use_labels) else _DEFAULT_LABELS[idx]
        draw.text(
            (x_offset + width // 2, label_y + _LABEL_HEIGHT // 2),
            label_text,
            fill=_LABEL_FG,
            font=font,
            anchor="mm",
        )

    # Save the comparison image
    if output_path is None:
        fd, output_path = tempfile.mkstemp(
            suffix=".png", prefix="kiln_compare_",
        )
        os.close(fd)

    canvas.save(output_path, "PNG")

    return {
        "success": True,
        "comparison_path": output_path,
        "models": models,
        "angle": angle.lower(),
        "width": canvas_w,
        "height": canvas_h,
        "stitched": True,
        "layout": f"{cols}x{rows}",
        "message": (
            f"Compared {len(paths)} models side by side at '{angle}' angle. "
            f"{len(successful_renders)}/{len(paths)} rendered successfully."
        ),
    }
