"""OpenSCAD code generation for embossing/debossing images onto 3D models.

Core engine for the ``decorate_surface`` MCP tool.  Generates ``.scad``
files that apply SVG, heightmap (PGM), or OpenSCAD ``text()`` content to
a specified face of an STL/OBJ model, then optionally compiles the result
to STL via the OpenSCAD CLI.

Only Python stdlib is used.
"""

from __future__ import annotations

import logging
import math
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenSCAD version cache — checked once per session
# ---------------------------------------------------------------------------

#: Cached detected OpenSCAD version string, e.g. ``"2024.12.19"``.
#: ``None`` means not yet checked; ``""`` means check failed / not found.
_openscad_version_cache: str | None = None

#: Set to True after the first successful Manifold compile so the benchmark
#: message is only logged once per process lifetime.
_manifold_benchmarked: bool = False

#: Set to True after the first outdated-version warning so it only fires once
#: per process lifetime.
_upgrade_warned: bool = False

_OPENSCAD_MIN_VERSION_YEAR = 2024
_OPENSCAD_UPGRADE_INSTRUCTIONS = (
    "  macOS: brew install --cask openscad@snapshot\n"
    "  Linux: sudo snap install openscad --edge\n"
    "  Windows: Download from https://openscad.org/downloads#snapshots"
)
_OPENSCAD_UPGRADE_MSG = (
    "OpenSCAD 2021 is outdated and has critical SVG bugs "
    "(SVG import() in difference() silently fails). "
    "Please upgrade: "
    "brew install --cask openscad@snapshot  (macOS) "
    "or download from https://openscad.org/downloads"
)


def _detect_openscad_version(binary: str) -> str:
    """Run ``openscad --version`` and return the version string.

    Returns the raw version token (e.g. ``"2024.12.19"``) or ``""`` on
    failure.  Result is not cached here — callers manage the cache.
    """
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # OpenSCAD prints e.g. "OpenSCAD version 2024.12.19" to stderr
        output = result.stderr.strip() or result.stdout.strip()
        match = re.search(r"(\d{4}\.\d+(?:\.\d+)?)", output)
        if match:
            return match.group(1)
    except Exception:  # noqa: BLE001
        pass
    return ""


def get_openscad_version(binary: str | None = None) -> str:
    """Return the cached OpenSCAD version string, detecting it if needed.

    Checks the version once per process lifetime and caches the result.
    Returns ``""`` if detection fails.
    """
    global _openscad_version_cache  # noqa: PLW0603
    if _openscad_version_cache is not None:
        return _openscad_version_cache
    if binary is None:
        try:
            binary = _find_openscad()
        except FileNotFoundError:
            _openscad_version_cache = ""
            return ""
    _openscad_version_cache = _detect_openscad_version(binary)
    return _openscad_version_cache


def _openscad_version_year(version: str) -> int:
    """Extract the year component from a version string like ``"2024.12.19"``."""
    if not version:
        return 0
    try:
        return int(version.split(".")[0])
    except (ValueError, IndexError):
        return 0


# ---------------------------------------------------------------------------
# Material-aware depth defaults (mm)
# ---------------------------------------------------------------------------

MATERIAL_DEPTHS: dict[str, float] = {
    "PLA": 0.6,
    "PETG": 0.8,
    "ABS": 0.7,
    "TPU": 1.2,
    "Nylon": 0.8,
    "Resin": 0.3,
}

_DEFAULT_DEPTH_MM = 0.8


def get_default_depth(material: str) -> float:
    """Return the recommended emboss/deboss depth for *material*.

    Falls back to 0.8 mm if the material is not in :data:`MATERIAL_DEPTHS`.
    """
    return MATERIAL_DEPTHS.get(material, _DEFAULT_DEPTH_MM)


# ---------------------------------------------------------------------------
# Internal helpers — face orientation
# ---------------------------------------------------------------------------

def _vec_length(v: list[float]) -> float:
    return math.sqrt(sum(c * c for c in v))


def _normalize(v: list[float]) -> list[float]:
    length = _vec_length(v)
    if length < 1e-12:
        return [0.0, 0.0, 1.0]
    return [c / length for c in v]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(ai * bi for ai, bi in zip(a, b, strict=True))


def _cross(a: list[float], b: list[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _rotation_for_normal(normal: list[float]) -> str:
    """Return the OpenSCAD ``rotate(...)`` clause that maps [0,0,1] to *normal*.

    For cardinal faces (top, bottom, front, back, left, right) this returns
    clean degree rotations.  For arbitrary normals it falls back to an
    axis-angle rotation.
    """
    n = _normalize(normal)

    # Cardinal-direction shortcuts (tolerance 0.9)
    if n[2] > 0.9:
        return ""  # TOP — no rotation needed
    if n[2] < -0.9:
        return "rotate([180, 0, 0])\n        "
    if n[1] < -0.9:
        return "rotate([90, 0, 0])\n        "  # FRONT
    if n[1] > 0.9:
        return "rotate([-90, 0, 0])\n        "  # BACK
    if n[0] < -0.9:
        return "rotate([0, 90, 0])\n        "  # LEFT
    if n[0] > 0.9:
        return "rotate([0, -90, 0])\n        "  # RIGHT

    # General axis-angle: rotate [0,0,1] → n
    z_axis = [0.0, 0.0, 1.0]
    axis = _cross(z_axis, n)
    axis_len = _vec_length(axis)
    if axis_len < 1e-12:
        # Vectors are (anti-)parallel — already handled above
        return ""
    axis = [c / axis_len for c in axis]
    angle_deg = math.degrees(math.acos(max(-1.0, min(1.0, _dot(z_axis, n)))))
    return f"rotate(a={angle_deg:.4f}, v=[{axis[0]:.6f}, {axis[1]:.6f}, {axis[2]:.6f}])\n        "


def _escape_scad_string(s: str) -> str:
    """Escape a string for embedding inside an OpenSCAD double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# OpenSCAD code templates
# ---------------------------------------------------------------------------

def _invert_dat_heightmap(content_info: dict, output_dir: Path) -> dict:
    """Create an inverted copy of a DAT heightmap for emboss/deboss.

    In the raw heightmap, dark pixels (the pattern) have low values (near 0)
    and background has high values (near 1).  Both ``difference()`` (deboss)
    and ``union()`` (emboss) need the *pattern* to be tall so it either gets
    subtracted into or protrudes from the surface.  This writes a new DAT
    file with values flipped (1-v).

    Returns a new content_info dict pointing to the inverted DAT file.
    """
    dat_path = content_info.get("dat_path", "")
    if not dat_path or not os.path.isfile(dat_path):
        return content_info

    inv_path = output_dir / (Path(dat_path).stem + "_inv.dat")
    with open(dat_path) as f_in, open(inv_path, "w") as f_out:
        for line in f_in:
            vals = line.strip().split()
            inv_vals = [f"{1.0 - float(v):.4f}" for v in vals]
            f_out.write(" ".join(inv_vals) + "\n")

    result = dict(content_info)
    result["dat_path"] = str(inv_path)
    return result


def _svg_content_block(
    content_info: dict,
    scale_x: float,
    scale_y: float,
    content_cx: float | None = None,
    content_cy: float | None = None,
    *,
    svg_id: str = "",
    svg_layer: str = "",
) -> str:
    """Return the OpenSCAD fragment that produces the SVG extrusion shape.

    Uses native OpenSCAD polygon() geometry extracted from the SVG
    (via ``openscad_polygons`` in content_info).  This is the primary
    path because OpenSCAD 2021's SVG ``import()`` silently fails in
    ``difference()`` operations — the boolean produces no geometry
    change against both inline and imported STL meshes.  Native
    polygon() calls work reliably in all OpenSCAD versions.

    Falls back to SVG ``import()`` only when no extractable polygon
    geometry was found in the SVG.

    When *content_cx*/*content_cy* are provided (from content-bounds
    analysis), the translate centers the actual geometry instead of the
    viewBox, so logos with whitespace padding scale correctly.
    """
    # Fall back to viewBox center when content center is not provided
    if content_cx is None:
        content_cx = content_info.get("width", 100) / 2
    if content_cy is None:
        content_cy = content_info.get("height", 100) / 2

    # Use native OpenSCAD polygons when available (reliable boolean path)
    native_code = content_info.get("openscad_polygons", "")
    if native_code:
        # On OpenSCAD 2024+, wrap the union() in fill() so that tiny gaps
        # between adjacent hull() endpoints are closed automatically.
        # fill() is a stable module (added in 2021.01) — no --enable flag needed.
        try:
            use_fill = _openscad_version_year(get_openscad_version()) >= 2024
        except Exception:  # noqa: BLE001
            use_fill = False

        if use_fill:
            inner = (
                f'fill()\n'
                f'                        {native_code}'
            )
        else:
            inner = native_code

        return (
            f'scale([{scale_x:.6f}, {scale_y:.6f}, 1])\n'
            f'                translate([-{content_cx:.6f}, -{content_cy:.6f}, 0])\n'
            f'                    {inner}'
        )

    # Fallback: SVG import (unreliable on complex meshes)
    svg_path = content_info["svg_path"]
    extra_args = ""
    if svg_id:
        extra_args += f', id="{_escape_scad_string(svg_id)}"'
    if svg_layer:
        extra_args += f', layer="{_escape_scad_string(svg_layer)}"'
    return (
        f'scale([{scale_x:.6f}, {scale_y:.6f}, 1])\n'
        f'                translate([-{content_cx:.6f}, -{content_cy:.6f}, 0])\n'
        f'                    import("{_escape_scad_string(svg_path)}"{extra_args});'
    )


def _heightmap_content_block(
    content_info: dict,
    x_scale: float,
    y_scale: float,
    depth: float,
) -> str:
    """Return the OpenSCAD fragment for a PGM heightmap surface.

    OpenSCAD's ``surface()`` interprets pixel values 0-255 as heights.
    We scale so that black pixels (0) produce zero height (no emboss)
    and white pixels (255) produce full depth.  For debossing, the
    caller inverts the image so dark areas = emboss.

    The depth scale factor is ``(depth + 0.1) / 255`` so the surface
    geometry fully penetrates the model (the +0.1 ensures overlap).
    """
    dat_path = content_info.get("dat_path") or content_info.get("pgm_path", "")
    # DAT files use 0.0–1.0 range, so z_scale = full depth (+0.1 for overlap)
    z_scale = depth + 0.1
    return (
        f'scale([{x_scale:.6f}, {y_scale:.6f}, {z_scale:.6f}])\n'
        f'                surface(file="{_escape_scad_string(dat_path)}", center=true, convexity=5);'
    )


def _text_content_block(content_info: dict) -> str:
    """Return the OpenSCAD fragment for a text() shape.

    On OpenSCAD 2024+ uses ``textmetrics()`` for pixel-perfect centering.
    On older versions falls back to ``halign="center"`` which is close but
    relies on font-specific heuristics.
    """
    text_str = content_info.get("text", "KILN")
    font_size = content_info.get("font_size", 10)
    font = content_info.get("font", "Liberation Sans:style=Bold")

    # Use textmetrics() on 2024+ for exact centering; fall back safely.
    try:
        version_year = _openscad_version_year(get_openscad_version())
        use_textmetrics = version_year >= 2024
    except Exception:  # noqa: BLE001
        use_textmetrics = False

    escaped_text = _escape_scad_string(text_str)
    escaped_font = _escape_scad_string(font)

    if use_textmetrics:
        return (
            f'let(tm = textmetrics(text="{escaped_text}", size={font_size}, font="{escaped_font}"))\n'
            f'  translate([-tm.size[0]/2, -tm.size[1]/2, 0])\n'
            f'    text("{escaped_text}", size={font_size}, '
            f'font="{escaped_font}", valign="baseline");'
        )
    return (
        f'text("{escaped_text}", '
        f'size={font_size}, '
        f'halign="center", valign="center", '
        f'font="{escaped_font}");'
    )


# ---------------------------------------------------------------------------
# Main generation function
# ---------------------------------------------------------------------------

def _resolve_placement_offsets(
    placement: str,
    face: dict,
    scale: float,
    offset_x_mm: float,
    offset_y_mm: float,
) -> tuple[float, float]:
    """Convert a named placement preset into concrete X/Y offsets.

    Presets position content relative to the face dimensions so callers
    don't have to guess offsets manually.

    Supported presets:
        - ``"center"`` — no offset (default)
        - ``"top"`` — upper third of the face
        - ``"bottom"`` — lower third of the face
        - ``"top-rim"`` — near the top edge (10% from edge)
        - ``"bottom-rim"`` — near the bottom edge (10% from edge)

    Any explicit ``offset_x_mm`` / ``offset_y_mm`` are added on top of
    the preset, allowing fine-tuning.
    """
    face_h = face.get("height_mm", 0)
    usable_h = face_h * scale

    preset_offsets: dict[str, tuple[float, float]] = {
        "center": (0.0, 0.0),
        "top": (0.0, usable_h * 0.30),
        "bottom": (0.0, -usable_h * 0.30),
        "top-rim": (0.0, usable_h * 0.40),
        "bottom-rim": (0.0, -usable_h * 0.40),
    }
    px, py = preset_offsets.get(placement, (0.0, 0.0))
    return (offset_x_mm + px, offset_y_mm + py)


def generate_emboss_scad(
    *,
    model_path: str,
    content_info: dict,
    face: dict,
    output_dir: str,
    depth_mm: float = 0.8,
    mode: str = "deboss",
    scale: float = 0.7,
    absolute_size_mm: float = 0.0,
    offset_x_mm: float = 0.0,
    offset_y_mm: float = 0.0,
    placement: str = "center",
    svg_id: str = "",
    svg_layer: str = "",
) -> dict[str, Any]:
    """Generate an OpenSCAD ``.scad`` file for an emboss/deboss operation.

    Parameters
    ----------
    model_path:
        Path to the base STL or OBJ model file.
    content_info:
        Dict describing the content to apply.  Must contain a ``type`` key
        with one of ``"svg"``, ``"heightmap"``, or ``"openscad_text"``.

        * ``svg`` — requires ``svg_path``, ``width``, ``height``,
          ``aspect_ratio``.
        * ``heightmap`` — requires ``pgm_path``, ``width_px``,
          ``height_px``, ``aspect_ratio``.
        * ``openscad_text`` — requires ``text``; optional ``font_size``,
          ``font``.
    face:
        Dict from the surface-intelligence module.  Must contain
        ``normal`` (3-element list), ``center`` (3-element list),
        ``width_mm``, ``height_mm``, and ``face_name``.
    output_dir:
        Directory in which to write the generated ``.scad`` file.
    depth_mm:
        Depth of the emboss/deboss in millimetres (positive = into surface).
    mode:
        ``"deboss"`` (cut into surface) or ``"emboss"`` (raised above).
    scale:
        Fraction of the face to cover (0.0 – 1.0).  Ignored when
        *absolute_size_mm* is provided.
    absolute_size_mm:
        Exact width of the decoration in millimetres.  When > 0, overrides
        *scale* so the decoration is always this width regardless of the
        product size.  Use for brand specs that require a fixed logo size
        across different products.  Clamped to 95% of face width if too
        large; warns if below 5mm (near FDM detail limits).
    offset_x_mm:
        Horizontal offset from face centre in mm.
    offset_y_mm:
        Vertical offset from face centre in mm.

    Returns
    -------
    dict
        ``scad_path`` — absolute path to the generated ``.scad`` file.
        ``output_stl_path`` — where the compiled STL will be written.
        ``openscad_command`` — the full shell command to compile it.
    """
    if mode not in ("deboss", "emboss"):
        raise ValueError(f"mode must be 'deboss' or 'emboss', got {mode!r}")

    content_type = content_info.get("type")
    if content_type not in ("svg", "heightmap", "openscad_text"):
        raise ValueError(
            f"content_info['type'] must be 'svg', 'heightmap', or "
            f"'openscad_text', got {content_type!r}"
        )

    # Ensure output directory exists
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    model_name = Path(model_path).stem
    scad_filename = f"{model_name}_{mode}.scad"
    scad_path = out / scad_filename
    output_stl_path = out / f"{model_name}_{mode}.stl"

    # Face geometry
    cx, cy, cz = face["center"]
    face_w = face["width_mm"]
    face_h = face["height_mm"]
    normal = face["normal"]

    # Scale content to fit within the target area.
    # absolute_size_mm overrides scale — exact width in mm.
    warnings: list[str] = []
    if absolute_size_mm > 0:
        max_allowed = face_w * 0.95
        if absolute_size_mm > max_allowed:
            warnings.append(
                f"absolute_size_mm={absolute_size_mm:.1f} exceeds 95% of "
                f"face width ({face_w:.1f}mm). Clamped to {max_allowed:.1f}mm."
            )
            absolute_size_mm = max_allowed
        if absolute_size_mm < 5.0:
            warnings.append(
                f"absolute_size_mm={absolute_size_mm:.1f}mm is very small — "
                f"near FDM detail limits. Features may not be visible."
            )
        # Compute effective scale from absolute size
        scale = absolute_size_mm / face_w if face_w > 0 else 0.5
        target_w = absolute_size_mm
        target_h = absolute_size_mm * (face_h / face_w) if face_w > 0 else absolute_size_mm
    else:
        target_w = face_w * scale
        target_h = face_h * scale

    # Compute the translation along the face normal for positioning
    # For deboss: start slightly above the surface, extrude inward
    # For emboss: start at the surface, extrude outward
    _normalize(normal)  # validate normal is non-zero
    # Resolve placement preset + manual offsets
    final_offset_x, final_offset_y = _resolve_placement_offsets(
        placement, face, scale, offset_x_mm, offset_y_mm,
    )

    if mode == "deboss":
        tx = cx + final_offset_x
        ty = cy + final_offset_y
        tz = cz
        boolean_op = "difference"
    else:
        tx = cx + final_offset_x
        ty = cy + final_offset_y
        tz = cz
        boolean_op = "union"

    rotation_clause = _rotation_for_normal(normal)

    # Build the content-specific extrusion block
    if content_type == "svg":
        # Use content bounds if available, otherwise fall back to viewBox
        svg_w = content_info.get("content_width") or content_info.get("width", 100)
        svg_h = content_info.get("content_height") or content_info.get("height", 100)
        content_cx = content_info.get("content_x_min", 0) + svg_w / 2
        content_cy = content_info.get("content_y_min", 0) + svg_h / 2

        scale_x = target_w / svg_w if svg_w else 1.0
        scale_y = target_h / svg_h if svg_h else 1.0
        # Use uniform scale to preserve aspect ratio
        uniform_scale = min(scale_x, scale_y)
        inner = _svg_content_block(
            content_info, uniform_scale, uniform_scale, content_cx, content_cy,
            svg_id=svg_id, svg_layer=svg_layer,
        )
        extrude_height = depth_mm + 0.1
        content_block = (
            f"linear_extrude(height={extrude_height:.4f})\n"
            f"            {inner}"
        )
    elif content_type == "heightmap":
        x_scale = target_w / content_info.get("width_px", 100)
        y_scale = target_h / content_info.get("height_px", 100)
        # The raw heightmap has dark=0 / light=1.  For both deboss
        # (difference — pattern must be tall to get subtracted) and
        # emboss (union — pattern must be tall to protrude), we need
        # dark areas to be tall.  Invert the DAT so pattern=1, bg=0.
        heightmap_info = _invert_dat_heightmap(content_info, out)
        content_block = _heightmap_content_block(
            heightmap_info, x_scale, y_scale, depth_mm,
        )
    else:
        # openscad_text — scale font_size to fit the target width
        text_str = content_info.get("text", "")
        # Approximate: each character is ~0.6× font_size wide
        char_count = max(1, len(text_str))
        max_font_from_w = target_w / (char_count * 0.6)
        max_font_from_h = target_h * 0.8  # leave vertical margin
        auto_font_size = min(max_font_from_w, max_font_from_h)
        content_info = {**content_info, "font_size": round(auto_font_size, 1)}
        inner = _text_content_block(content_info)
        extrude_height = depth_mm + 0.1
        content_block = (
            f"linear_extrude(height={extrude_height:.4f})\n"
            f"            {inner}"
        )

    # Compute translate Z component depending on mode and face orientation
    if mode == "deboss":
        # Position so the extrusion cuts into the surface
        z_offset = -depth_mm
    else:
        # Position so the extrusion protrudes from the surface
        z_offset = 0.0

    translate_line = f"translate([{tx:.6f}, {ty:.6f}, {tz + z_offset:.6f}])"

    scad_code = (
        f'// Generated by Kiln emboss_generator\n'
        f'// Mode: {mode} | Depth: {depth_mm} mm | Face: {face.get("face_name", "unknown")}\n'
        f'// Content type: {content_type}\n'
        f'\n'
        f'{boolean_op}() {{\n'
        f'    import("{_escape_scad_string(str(model_path))}");\n'
        f'    {translate_line}\n'
        f'        {rotation_clause}{content_block}\n'
        f'}}\n'
    )

    scad_path.write_text(scad_code, encoding="utf-8")

    openscad_cmd = f'openscad -o "{output_stl_path}" "{scad_path}"'

    result = {
        "scad_path": str(scad_path),
        "output_stl_path": str(output_stl_path),
        "openscad_command": openscad_cmd,
    }
    if warnings:
        result["warnings"] = warnings
    return result


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------

def _find_openscad(openscad_path: str | None = None) -> str:
    """Locate the OpenSCAD binary.

    Search order:
    1. *openscad_path* if provided.
    2. macOS application bundle path.
    3. ``openscad`` on ``$PATH``.
    4. Homebrew fallback paths.

    Raises :class:`FileNotFoundError` if no executable is found.

    .. note::
        This function also caches the detected OpenSCAD version so callers
        can warn about outdated builds without paying the subprocess cost
        twice.  SVG-based operations should call
        :func:`_find_openscad_for_svg` instead, which hard-fails on
        OpenSCAD < 2024.
    """
    # Check env var fast path first (CLAUDE.md rule: env vars > config > defaults)
    if not openscad_path:
        openscad_path = os.environ.get("KILN_OPENSCAD_PATH")

    if openscad_path:
        if os.path.isfile(openscad_path) and os.access(openscad_path, os.X_OK):
            _detect_and_cache_version(openscad_path)
            _warn_if_outdated()
            return openscad_path
        raise FileNotFoundError(
            f"Provided OpenSCAD path does not exist or is not executable: {openscad_path}"
        )

    # macOS bundle — handle versioned app names (e.g. OpenSCAD-2021.01.app)
    if platform.system() == "Darwin":
        import glob as _glob

        for pattern in [
            "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
            "/Applications/OpenSCAD-*.app/Contents/MacOS/OpenSCAD",
        ]:
            for mac_path in _glob.glob(pattern):
                if os.path.isfile(mac_path) and os.access(mac_path, os.X_OK):
                    _detect_and_cache_version(mac_path)
                    _warn_if_outdated()
                    return mac_path

    # $PATH
    on_path = shutil.which("openscad")
    if on_path:
        _detect_and_cache_version(on_path)
        _warn_if_outdated()
        return on_path

    # Homebrew fallback (MCP servers may not inherit full $PATH)
    for brew_path in ["/opt/homebrew/bin/openscad", "/usr/local/bin/openscad"]:
        if os.path.isfile(brew_path) and os.access(brew_path, os.X_OK):
            _detect_and_cache_version(brew_path)
            _warn_if_outdated()
            return brew_path

    raise FileNotFoundError(
        "OpenSCAD not found. Install it for 3D model generation:\n"
        + _OPENSCAD_UPGRADE_INSTRUCTIONS
    )


def _detect_and_cache_version(binary: str) -> None:
    """Detect and cache the OpenSCAD version if not already cached."""
    global _openscad_version_cache  # noqa: PLW0603
    if _openscad_version_cache is None:
        _openscad_version_cache = _detect_openscad_version(binary)


def _warn_if_outdated() -> None:
    """Log a one-time WARNING when the cached OpenSCAD version is < 2024."""
    global _upgrade_warned  # noqa: PLW0603
    if _upgrade_warned:
        return
    version = _openscad_version_cache or ""
    year = _openscad_version_year(version)
    if year and year < _OPENSCAD_MIN_VERSION_YEAR:
        _upgrade_warned = True
        _logger.warning(
            "OpenSCAD %s is outdated. Upgrade for 20-100x faster compiles "
            "and reliable SVG support:\n%s",
            version,
            _OPENSCAD_UPGRADE_INSTRUCTIONS,
        )


def _find_openscad_for_svg(openscad_path: str | None = None) -> str:
    """Locate the OpenSCAD binary and hard-fail if version < 2024.

    SVG ``import()`` inside ``difference()`` silently fails on OpenSCAD
    2021, producing no geometry change.  This function rejects outdated
    builds with an actionable upgrade message so users don't waste time
    debugging phantom failures.

    For operations that do not use SVG import (pure geometry, text,
    heightmaps), use :func:`_find_openscad` instead.

    Raises :class:`FileNotFoundError` if binary is not found.
    Raises :class:`RuntimeError` if binary is OpenSCAD < 2024.
    """
    binary = _find_openscad(openscad_path)
    version = _openscad_version_cache or ""
    year = _openscad_version_year(version)
    if year and year < _OPENSCAD_MIN_VERSION_YEAR:
        raise RuntimeError(
            f"OpenSCAD {version} detected. {_OPENSCAD_UPGRADE_MSG}"
        )
    return binary


def compile_embossed_model(
    scad_path: str,
    output_stl_path: str,
    *,
    openscad_path: str | None = None,
    timeout: int = 120,
    export_format: str = "stl",
) -> dict[str, Any]:
    """Compile a ``.scad`` file to STL or 3MF using the OpenSCAD CLI.

    Parameters
    ----------
    scad_path:
        Path to the ``.scad`` file (as generated by
        :func:`generate_emboss_scad`).
    output_stl_path:
        Destination path for the compiled output.  When *export_format* is
        ``"3mf"`` the caller should supply a ``.3mf`` path here; if a
        ``.stl`` path is given it is rewritten to ``.3mf`` automatically.
    openscad_path:
        Explicit path to the OpenSCAD binary.  If ``None``, the function
        searches common locations.
    timeout:
        Maximum compilation time in seconds.
    export_format:
        Output format — ``"stl"`` (default, backwards-compatible) or
        ``"3mf"`` (preserves ``color()`` information, requires OpenSCAD
        2024+).  Falls back to STL silently if the installed OpenSCAD
        version is older than 2024.

    Returns
    -------
    dict
        ``stl_path`` — path to the output file (kept for backwards
        compatibility; identical to *output_path*).
        ``output_path`` — path to the output file (STL or 3MF).
        ``export_format`` — the format actually used (``"stl"`` or
        ``"3mf"``).
        ``file_size`` — size of the output file in bytes.
        ``compile_time_seconds`` — wall-clock compilation time.
        ``success`` — boolean indicating whether compilation succeeded.
        ``error`` — error message string (only present when *success* is
        ``False``).
    """
    # Detect whether the .scad file uses SVG import() — if so we need
    # OpenSCAD 2024+ because 2021 silently fails SVG booleans.
    _uses_svg = False
    try:
        _scad_text = Path(scad_path).read_text(encoding="utf-8")
        _uses_svg = ".svg" in _scad_text and "import(" in _scad_text
    except OSError:
        pass

    try:
        exe = _find_openscad_for_svg(openscad_path) if _uses_svg else _find_openscad(openscad_path)
    except (FileNotFoundError, RuntimeError) as exc:
        return {
            "stl_path": output_stl_path,
            "output_path": output_stl_path,
            "export_format": export_format,
            "file_size": 0,
            "compile_time_seconds": 0.0,
            "success": False,
            "error": str(exc),
        }

    import os as _os

    backend = _os.environ.get("KILN_OPENSCAD_BACKEND", "manifold")
    version = get_openscad_version(exe)
    version_year = _openscad_version_year(version)
    use_manifold = version_year >= 2024 and backend == "manifold"

    # 3MF export requires OpenSCAD 2024+; fall back to STL on older versions.
    use_3mf = export_format == "3mf" and version_year >= 2024
    if export_format == "3mf" and not use_3mf:
        _logger.warning(
            "OpenSCAD %s does not support 3MF export (need 2024+); falling back to STL",
            version or "unknown",
        )

    # Resolve the actual output path — rewrite extension when needed.
    if use_3mf:
        output_path = str(Path(output_stl_path).with_suffix(".3mf"))
    else:
        output_path = str(Path(output_stl_path).with_suffix(".stl"))
    actual_format = "3mf" if use_3mf else "stl"

    def _build_cmd(*, with_manifold: bool) -> list[str]:
        c = [exe, "-o", output_path]
        # Use Manifold backend on OpenSCAD 2024+ for 20-100x faster boolean ops.
        # Manifold uses multithreaded double-precision FP instead of CGAL's
        # exact arithmetic. Opt-out via KILN_OPENSCAD_BACKEND=cgal env var.
        if with_manifold:
            c.append("--backend=manifold")
        # Enable textmetrics() on 2024+ for improved font metrics in text().
        if version_year >= 2024:
            c.append("--enable=textmetrics")
        c.append(scad_path)
        return c

    cmd = _build_cmd(with_manifold=use_manifold)
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - start
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return {
            "stl_path": output_path,
            "output_path": output_path,
            "export_format": actual_format,
            "file_size": 0,
            "compile_time_seconds": round(elapsed, 2),
            "success": False,
            "error": f"OpenSCAD compilation timed out after {timeout}s",
        }

    # Auto-fallback: if Manifold compile failed, retry with CGAL backend.
    if result.returncode != 0 and use_manifold:
        global _manifold_benchmarked  # noqa: PLW0603
        _logger.warning("Manifold compile failed, retrying with CGAL backend")
        cgal_cmd = _build_cmd(with_manifold=False)
        start = time.monotonic()
        try:
            result = subprocess.run(
                cgal_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            elapsed = time.monotonic() - start
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            return {
                "stl_path": output_path,
                "output_path": output_path,
                "export_format": actual_format,
                "file_size": 0,
                "compile_time_seconds": round(elapsed, 2),
                "success": False,
                "error": f"OpenSCAD CGAL compilation timed out after {timeout}s",
            }

    if result.returncode != 0:
        return {
            "stl_path": output_path,
            "output_path": output_path,
            "export_format": actual_format,
            "file_size": 0,
            "compile_time_seconds": round(elapsed, 2),
            "success": False,
            "error": result.stderr.strip() or result.stdout.strip(),
        }

    # Log Manifold benchmark once per session on first successful compile.
    if use_manifold:
        global _manifold_benchmarked  # noqa: PLW0603
        if not _manifold_benchmarked:
            _manifold_benchmarked = True
            _logger.info(
                "Manifold backend: %.2fs compile. "
                "OpenSCAD 2026 with Manifold enables 20-100x faster boolean operations.",
                elapsed,
            )

    out = Path(output_path)
    file_size = out.stat().st_size if out.exists() else 0

    return {
        "stl_path": output_path,
        "output_path": output_path,
        "export_format": actual_format,
        "file_size": file_size,
        "compile_time_seconds": round(elapsed, 2),
        "success": True,
    }


# ---------------------------------------------------------------------------
# Boolean success heuristic
# ---------------------------------------------------------------------------

def check_boolean_success(input_stl: str, output_stl: str, *, tolerance: float = 0.05) -> bool:
    """Check if a boolean operation produced meaningful geometry change.

    Compares the file sizes of *input_stl* (the original model) and
    *output_stl* (the boolean result).  If the output is within
    *tolerance* (default 5%) of the input, the boolean likely failed —
    for example, OpenSCAD's ``import()`` of thin SVG polygons can
    produce degenerate geometry that ``difference()`` silently ignores.

    Returns ``True`` when the boolean appears to have succeeded (i.e.
    the output differs meaningfully from the input).
    """
    try:
        input_size = os.path.getsize(input_stl)
        output_size = os.path.getsize(output_stl)
    except OSError:
        # If we can't stat either file, assume success (don't block).
        return True
    if input_size == 0:
        return True
    return abs(output_size - input_size) > input_size * tolerance


# ---------------------------------------------------------------------------
# QR code OpenSCAD module — Pro feature, lives in kiln-pro
# ---------------------------------------------------------------------------

def generate_qr_openscad_module(
    url: str,
    module_name: str = "qr_code",
    target_size_mm: float = 38.0,
    border: int = 1,
) -> tuple[str, dict]:
    """Generate an OpenSCAD module for a QR code (Pro feature).

    QR code generation is a paid feature. The implementation lives in
    the kiln-pro package.

    Raises :class:`ImportError` if kiln-pro is not installed.
    """
    try:
        from kiln_pro.decoration.qr_openscad import generate_qr_openscad_module as _impl
    except ImportError:
        raise ImportError(
            "QR code generation is a Pro feature. "
            "Upgrade at https://kiln3d.com/pricing"
        ) from None
    return _impl(url, module_name=module_name, target_size_mm=target_size_mm, border=border)
