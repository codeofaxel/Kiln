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
import tempfile
import time
from pathlib import Path
from typing import Any

from kiln import _vec

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

#: Per-process OpenSCAD executable probe cache. Values are ``(ok, reason)``.
_openscad_probe_cache: dict[str, tuple[bool, str | None]] = {}

_OPENSCAD_MIN_VERSION_YEAR = 2024
_OPENSCAD_UPGRADE_INSTRUCTIONS = (
    "  macOS: brew install --cask openscad@snapshot\n"
    "  Linux: sudo snap install openscad --edge\n"
    "  Windows: Download from https://openscad.org/downloads#snapshots"
)
_OPENSCAD_UPGRADE_MSG = (
    "This OpenSCAD build is outdated: it is far slower (no Manifold backend) and "
    "silently fails SVG booleans (an SVG logo in difference() produces no "
    "geometry). Upgrade to a current build: "
    "brew install --cask openscad@snapshot  (macOS) "
    "or https://openscad.org/downloads#snapshots"
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


def _probe_openscad_runs(path: str) -> tuple[bool, str | None]:
    """Return whether an OpenSCAD binary can execute on this host."""
    cached = _openscad_probe_cache.get(path)
    if cached is not None:
        return cached

    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        probed = (False, "openscad --version timed out after 5s")
        _openscad_probe_cache[path] = probed
        return probed
    except OSError as exc:
        probed = (False, str(exc) or exc.__class__.__name__)
        _openscad_probe_cache[path] = probed
        return probed
    except subprocess.SubprocessError as exc:
        probed = (False, str(exc) or exc.__class__.__name__)
        _openscad_probe_cache[path] = probed
        return probed

    output = (result.stderr or "").strip() or (result.stdout or "").strip()
    if result.returncode != 0:
        reason = output or f"openscad --version exited with status {result.returncode}"
        probed = (False, reason)
        _openscad_probe_cache[path] = probed
        return probed
    if not output:
        probed = (False, "openscad --version produced no output")
        _openscad_probe_cache[path] = probed
        return probed
    if not re.search(r"\bOpenSCAD\b", output, re.IGNORECASE):
        probed = (False, f"unexpected openscad --version output: {output[:200]}")
        _openscad_probe_cache[path] = probed
        return probed

    probed = (True, None)
    _openscad_probe_cache[path] = probed
    return probed


def _openscad_sandbox_hint(reasons: list[str]) -> str:
    """Return a Codex/sandbox hint for Qt CPU-feature probe failures."""
    joined = "\n".join(reasons).lower()
    cpu_probe_markers = [
        "neon",
        "bad cpu type",
        "incompatible processor",
        "qdetectcpufeatures",
        "qt build requires",
    ]
    if not any(marker in joined for marker in cpu_probe_markers):
        return ""
    return (
        "OpenSCAD is installed but Qt could not verify required CPU features "
        "from this launch environment. In Codex, approve running the Kiln "
        "generation command outside the sandbox, or run the generation step "
        "from a normal terminal.\n"
    )


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

_vec_length = _vec.length
_normalize = _vec.normalize  # type: ignore[assignment]
_dot = _vec.dot  # type: ignore[assignment]
_cross = _vec.cross  # type: ignore[assignment]


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
        #
        # EXCEPT for mark_geometry output: fill() erases holes, and that
        # geometry carries real even-odd holes (letter counters, outline
        # bands).  Such content_info sets openscad_polygons_fill_safe=False.
        fill_safe = content_info.get("openscad_polygons_fill_safe", True)
        try:
            use_fill = (
                fill_safe
                and _openscad_version_year(get_openscad_version()) >= 2024
            )
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
    mode: str = "emboss",
) -> str:
    """Return the OpenSCAD fragment for a PGM heightmap surface.

    OpenSCAD's ``surface()`` builds a flat-bottomed, varying-top prism.
    For *emboss*, we want that prism to sit ON the face and protrude
    upward — positive Z scale, flat bottom flush with the face.  For
    *deboss*, we want a flat-TOPPED, varying-BOTTOM prism so the flat
    top sits flush with the face and the varying bottom extends INTO
    the material by (depth + 0.1) * hmap — this gives a proportional
    cut depth across all heightmap values, not a step function.  Flip
    achieved with a negative Z scale.
    """
    dat_path = content_info.get("dat_path") or content_info.get("pgm_path", "")
    # DAT files use 0.0–1.0 range, so z_scale = full depth (+0.1 for overlap)
    z_scale = depth + 0.1
    signed_z = -z_scale if mode == "deboss" else z_scale
    return (
        f'scale([{x_scale:.6f}, {y_scale:.6f}, {signed_z:.6f}])\n'
        f'                surface(file="{_escape_scad_string(dat_path)}", center=true, convexity=5);'
    )


def _text_content_block(content_info: dict) -> str:
    """Return the OpenSCAD fragment for a text() shape.

    Centering: when the caller measured the rendered text
    (``_measured_center`` — see :func:`measure_text_block_mm`), translate
    by the MEASURED bbox center.  That is exact for any font and any
    glyphs, and works on every OpenSCAD build.  ``textmetrics()`` is NOT
    used: it is an experimental builtin that ships feature-flagged (e.g.
    2026.04 builds return ``undef`` unless ``--enable=textmetrics``), and
    an undef metric silently broke the centering translate — text drew
    left-aligned from the face center and ran off the edge.  Without a
    measurement, ``halign/valign="center"`` is the safe fallback.
    """
    text_str = content_info.get("text", "KILN")
    font_size = content_info.get("font_size", 10)
    font = content_info.get("font", "Liberation Sans:style=Bold")

    escaped_text = _escape_scad_string(text_str)
    escaped_font = _escape_scad_string(font)

    measured = content_info.get("_measured_center")
    if measured is not None:
        tx, ty = measured
        return (
            f'translate([{tx:.4f}, {ty:.4f}, 0])\n'
            f'    text("{escaped_text}", size={font_size}, '
            f'font="{escaped_font}");'
        )
    return (
        f'text("{escaped_text}", '
        f'size={font_size}, '
        f'halign="center", valign="center", '
        f'font="{escaped_font}");'
    )


# ---------------------------------------------------------------------------
# Measured text metrics — the exact rendered size of a text() block
# ---------------------------------------------------------------------------

# Normalized (per-unit-of-font-size) metrics per (text, font): one tiny probe
# compile EVER per string+font — metrics scale linearly with font size.
_TEXT_METRICS_CACHE: dict[tuple[str, str], tuple[float, float, float, float]] = {}


class TextMeasureError(RuntimeError):
    """The text probe compile failed — measured fitting unavailable."""


def measure_text_block_mm(
    text: str,
    font: str = "Liberation Sans:style=Bold",
    font_size: float = 48.0,
) -> tuple[float, float, float, float]:
    """Measure the EXACT rendered mm bbox of an OpenSCAD ``text()`` block.

    Compiles the text alone (a paper-thin extrude — sub-second under
    Manifold) and reads the geometry's true bounding box, so the answer
    is right for any font, any glyphs, any kerning — no ``char_aspect``
    heuristics, no ``textmetrics()`` (an experimental builtin that ships
    feature-flagged and returns ``undef`` on stock builds).  The text is
    rendered in its DEFAULT frame (halign left, valign baseline), which
    is the same frame the final decoration uses, so the returned offsets
    center it exactly.

    :returns: ``(width_mm, height_mm, min_x_mm, min_y_mm)`` at *font_size*.
    :raises TextMeasureError: when OpenSCAD is unavailable or the probe
        produces no geometry (caller falls back to heuristic centering).
    """
    key = (text, font)
    if key not in _TEXT_METRICS_CACHE:
        probe_size = 48.0
        escaped_text = _escape_scad_string(text)
        escaped_font = _escape_scad_string(font)
        with tempfile.TemporaryDirectory(prefix="kiln_text_probe_") as d:
            scad = os.path.join(d, "probe.scad")
            with open(scad, "w") as f:
                f.write(
                    f'linear_extrude(height=0.5) text("{escaped_text}", '
                    f'size={probe_size}, font="{escaped_font}");\n'
                )
            stl = os.path.join(d, "probe.stl")
            openscad = _find_openscad()
            if not openscad:
                raise TextMeasureError("OpenSCAD not found for text probe")
            proc = subprocess.run(
                [openscad, "-o", stl, scad],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0 or not os.path.exists(stl):
                raise TextMeasureError(
                    f"text probe compile failed: {proc.stderr[-200:] if proc.stderr else 'no output'}"
                )
            from kiln.surface_intelligence import _parse_stl

            triangles = _parse_stl(stl)
            if not triangles:
                raise TextMeasureError("text probe produced no geometry")
            xs = [v[0] for t in triangles for v in t["vertices"]]
            ys = [v[1] for t in triangles for v in t["vertices"]]
        _TEXT_METRICS_CACHE[key] = (
            (max(xs) - min(xs)) / probe_size,
            (max(ys) - min(ys)) / probe_size,
            min(xs) / probe_size,
            min(ys) / probe_size,
        )
    nw, nh, nx, ny = _TEXT_METRICS_CACHE[key]
    return nw * font_size, nh * font_size, nx * font_size, ny * font_size


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

    Presets resolve in the FACE-LOCAL frame (like the offsets they
    feed): "top" means toward the content's top on whichever face is
    being decorated, not world +y.
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
    min_edge_margin_mm: float = 4.0,
    additional_pre_text_transform: str = "",
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
        Placement offset from the face centre along the face's own
        WIDTH axis, in mm — positive slides the content toward the
        content's right.  Face-local on every face (cardinal or
        arbitrary): the offset rides inside the face-aligning
        rotation, so it always moves the art in the face plane.
    offset_y_mm:
        Same, along the face's HEIGHT axis — positive slides the
        content toward the content's top.  Measured world-axis
        mapping per face: top +y, bottom −y, front +z, back −z.

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

    # Enforce an absolute minimum edge margin so text doesn't kiss the
    # sides of the face on either small or large products.  The
    # proportional ``scale`` alone produces inconsistent visual padding
    # — a 100mm face at scale=0.85 has 7.5mm/side, which looks tight,
    # while a 30mm pet tag at the same scale has 2.25mm/side, which
    # looks broken.  Clamping ``target_w`` to ``face_w - 2 ×
    # min_edge_margin_mm`` guarantees a comfortable margin regardless
    # of face size.  Same for the vertical dimension.  Callers who
    # explicitly want hug-the-wall text override
    # ``min_edge_margin_mm=0`` (e.g. for license-plate frame band text
    # where the design intent is edge-to-edge).
    margin_clamped_w = max(face_w - 2.0 * min_edge_margin_mm, 1.0)
    margin_clamped_h = max(face_h - 2.0 * min_edge_margin_mm, 1.0)
    if target_w > margin_clamped_w:
        warnings.append(
            f"text width clamped from {target_w:.1f}mm to "
            f"{margin_clamped_w:.1f}mm to preserve "
            f"{min_edge_margin_mm:.1f}mm edge margin on a {face_w:.0f}mm-wide face"
        )
        target_w = margin_clamped_w
    if target_h > margin_clamped_h:
        target_h = margin_clamped_h

    # Compute the translation along the face normal for positioning
    # For deboss: start slightly above the surface, extrude inward
    # For emboss: start at the surface, extrude outward
    _normalize(normal)  # validate normal is non-zero
    # Resolve placement preset + manual offsets
    final_offset_x, final_offset_y = _resolve_placement_offsets(
        placement, face, scale, offset_x_mm, offset_y_mm,
    )

    # Offsets are FACE-LOCAL on every face: they ride an INNER translate
    # (post-rotation), so the face-aligning rotation itself defines the
    # in-plane axes and +offset_x/+offset_y always slide the content
    # along its own width/height — never along the face normal.
    # Measured through the real pipeline (image → SCAD → OpenSCAD →
    # STL readback) the world-axis mapping per cardinal face is:
    #
    #   top    → +x moves world +x, +y moves world +y
    #   bottom → +x moves world +x, +y moves world −y
    #   front  → +x moves world +x, +y moves world +z
    #   back   → +x moves world +x, +y moves world −z
    #
    # The old cardinal path added offsets to the OUTER translate in
    # world x/y instead — right for top/bottom (both world axes lie in
    # the face plane), silently wrong for front/back (world-y is the
    # face NORMAL there: offset_y_mm changed carve depth instead of
    # sliding the art) and left/right (world-x is the normal).
    #
    # Cardinal vs non-cardinal still differ in DEPTH handling: cardinal
    # faces keep the tuned world-Z z_offset logic below; arbitrary
    # normals (tilted nameplate canvas, sloped trophy face) shift along
    # the face normal via the inner translate's z component — the
    # 2026-05-03 nameplate "no visible text" bug was a world-Z shift on
    # a tilted face leaving the prism entirely inside the body, a
    # silent no-op cut.
    is_cardinal = (
        abs(normal[2]) > 0.9
        or abs(normal[1]) > 0.9
        or abs(normal[0]) > 0.9
    )

    if mode == "deboss":
        boolean_op = "difference"
    else:
        boolean_op = "union"

    # Outer translate always goes to the bare face centre; placement
    # offsets are applied after the rotation (see above).
    tx = cx
    ty = cy
    tz = cz

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
            heightmap_info, x_scale, y_scale, depth_mm, mode=mode,
        )
    else:
        # openscad_text — caller can pre-set ``font_size`` in
        # content_info to bypass auto-sizing.  Multi-line helpers use
        # this to enforce typography hierarchy (primary > secondary >
        # tertiary) when the auto-sizer's width/height coupling would
        # otherwise produce inverted sizing — e.g. "Josh Beckham"
        # width-limited at 11mm while "CEO" height-limited at 17mm
        # on a 200×78 wedge face.
        #
        # Sizing + centering are MEASURED, not estimated: a probe compile
        # gives the text's exact rendered mm bbox (see
        # ``measure_text_block_mm``), so the font is scaled to truly fit
        # the target box and the block is centered by its real bounds.
        # An explicit caller size is honoured but clamped DOWN if its
        # measured bbox would overflow the face — overflow is never OK.
        text_str = content_info.get("text", "")
        text_font = content_info.get("font", "Liberation Sans:style=Bold")
        explicit_font_size = content_info.get("font_size", 0)
        try:
            chosen = float(explicit_font_size) if explicit_font_size else 48.0
            t_w, t_h, _, _ = measure_text_block_mm(text_str, text_font, chosen)
            fit_ratio = min(target_w / t_w, target_h / t_h) if t_w > 0 and t_h > 0 else 1.0
            if not explicit_font_size:
                # Auto: use the target box exactly.
                chosen *= fit_ratio
            elif fit_ratio < 1.0:
                # Explicit but overflowing: shrink to fit, and say so.
                warnings.append(
                    f"text font_size={explicit_font_size} would render "
                    f"{t_w:.0f}mm wide on a {target_w:.0f}mm target — "
                    f"clamped to {chosen * fit_ratio:.1f} to keep the text "
                    f"on the face"
                )
                chosen *= fit_ratio
            f_w, f_h, f_minx, f_miny = measure_text_block_mm(text_str, text_font, chosen)
            content_info = {
                **content_info,
                "font_size": round(chosen, 2),
                # Center by the MEASURED bbox — exact for any glyphs, on
                # any OpenSCAD build (textmetrics ships feature-flagged
                # and returns undef on stock builds, which silently broke
                # the old centering translate).
                "_measured_center": (-(f_minx + f_w / 2), -(f_miny + f_h / 2)),
            }
        except TextMeasureError as exc:
            _logger.warning("text probe unavailable (%s) — heuristic fit", exc)
            if not explicit_font_size:
                # Fallback: the legacy char-aspect estimate.
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

    # Compute translate Z component depending on mode and face orientation.
    #
    # The content prism is created as linear_extrude(height=depth_mm + 0.1)
    # pointing in +Z in the content's local frame.  _rotation_for_normal()
    # then aligns the prism's +Z with the face normal direction.  This means
    # for a FLIPPED face (normal has a strong -Z component), the prism ends
    # up pointing in -Z in world coordinates — the OPPOSITE side of the
    # material it should be cutting.  Previously `z_offset = -depth_mm`
    # shifted the prism even further away from material for bottom faces,
    # leaving it entirely outside the model (Z=[-1.7, -0.8] vs tray Z=[0,18])
    # and producing a silent no-op subtraction.
    #
    # Fix: when the face normal points downward, the prism (post-rotate) lives
    # at Z=[-h, 0] relative to the face center.  Shift it by +(depth_mm + 0.1)
    # so the prism's far end rests at cz and its near end extends INTO the
    # material at cz + depth_mm.  This mirrors the top-face behavior where
    # we shift -depth_mm so the prism penetrates inward.
    extrude_height = depth_mm + 0.1
    if is_cardinal:
        # Cardinal-face path (top/bottom/front/back/left/right): use the
        # original world-Z shift logic.  The face's local Z aligns with
        # ±world-Z (after rotation) so this still works correctly.
        if mode == "deboss":
            if content_type == "heightmap":
                # Heightmap deboss uses a negative Z scale (see
                # _heightmap_content_block) so the prism is flat-TOPPED and
                # varying-BOTTOMED.  Translating to cz puts the flat top
                # exactly on the face surface; the varying bottom extends
                # INTO the material by (depth + 0.1) * hmap.  Cut depth is
                # proportional across all hmap values, not a step function.
                z_offset = 0.0
            elif normal[2] < -0.9:
                # SVG/text deboss on a bottom-like face: prism was flipped by
                # rotate([180,0,0]).  Shift up by full extrude_height so it
                # penetrates upward into the body sitting above the face.
                z_offset = extrude_height
            else:
                # SVG/text deboss on a top-like or side face: prism already
                # points toward the body after rotation; shift by -depth_mm
                # so far end penetrates depth.
                z_offset = -depth_mm
        else:
            # Emboss — protrude outward from the surface.  Rotation already
            # orients the prism outward; no additional Z shift needed for top
            # faces.  For bottom faces the prism (post-flip) sits at Z=[-h, 0]
            # in world frame, which is already OUTSIDE the material above —
            # correct for a raised emboss on the tray underside.
            z_offset = 0.0

        translate_line = f"translate([{tx:.6f}, {ty:.6f}, {tz + z_offset:.6f}])"
        # Placement offsets compose post-rotation so they stay in the
        # face plane on every cardinal face (see the mapping above).
        # Depth stays on the outer world-Z shift, so the inner z is 0.
        if final_offset_x or final_offset_y:
            inner_translate_line = (
                f"translate([{final_offset_x:.6f}, "
                f"{final_offset_y:.6f}, 0])\n        "
            )
        else:
            inner_translate_line = ""
    else:
        # Non-cardinal-face path: outer translate goes to bare face
        # center; offsets and deboss-shift are applied AFTER rotation in
        # face-local space so they compose along the face's u/v/normal
        # axes regardless of world orientation.
        if mode == "deboss":
            if content_type == "heightmap":
                local_z_shift = 0.0
                local_extrude_override = None
            else:
                # Shift the prism INTO the body by depth_mm along the
                # face normal (which is local +Z post-rotation), then
                # extrude back out by (depth_mm + 1.0) so the prism
                # robustly straddles the face surface — 1mm OUTSIDE
                # the body, full depth_mm INSIDE.  The 1mm outward
                # overlap (vs the cardinal-face 0.1mm) absorbs face-
                # centroid jitter from chained deboss operations:
                # each deboss carving subtly shifts the centroid off
                # the actual face plane (deboss-floor pulls it
                # inward), and a chained CEO-after-name deboss with
                # only 0.1mm overlap silently no-ops because the
                # prism never crosses the surface.  See the 2026-05-03
                # nameplate "CEO disappears on second pass" bug.
                local_z_shift = -depth_mm
                local_extrude_override = depth_mm + 1.0
        else:
            local_z_shift = 0.0
            local_extrude_override = None

        translate_line = f"translate([{tx:.6f}, {ty:.6f}, {tz:.6f}])"
        inner_translate_line = (
            f"translate([{final_offset_x:.6f}, "
            f"{final_offset_y:.6f}, {local_z_shift:.6f}])\n        "
        )

        # If the local-frame deboss path needs a different extrude
        # height to straddle the surface robustly, rebuild the
        # content_block with the override.
        if local_extrude_override is not None and content_type == "openscad_text":
            inner_text = _text_content_block(content_info)
            content_block = (
                f"linear_extrude(height={local_extrude_override:.4f})\n"
                f"            {inner_text}"
            )
        elif local_extrude_override is not None and content_type == "svg":
            # Same idea for SVG content — rebuild with override height.
            # _svg_content_block was already called above; we need the
            # raw inner.  Recompute it here for the override case.
            svg_w_o = content_info.get("content_width") or content_info.get("width", 100)
            svg_h_o = content_info.get("content_height") or content_info.get("height", 100)
            content_cx_o = content_info.get("content_x_min", 0) + svg_w_o / 2
            content_cy_o = content_info.get("content_y_min", 0) + svg_h_o / 2
            scale_x_o = target_w / svg_w_o if svg_w_o else 1.0
            scale_y_o = target_h / svg_h_o if svg_h_o else 1.0
            uniform_scale_o = min(scale_x_o, scale_y_o)
            inner_svg = _svg_content_block(
                content_info, uniform_scale_o, uniform_scale_o,
                content_cx_o, content_cy_o,
                svg_id=svg_id, svg_layer=svg_layer,
            )
            content_block = (
                f"linear_extrude(height={local_extrude_override:.4f})\n"
                f"            {inner_svg}"
            )

    # ``additional_pre_text_transform`` is the smart-flip injection point.
    # Callers (decoration_helpers.emboss_text_on_face) consult
    # ``select_bottom_face_flip`` for bottom faces; if the helper picks
    # the ``mirror`` alternative for a tall-narrow face, the caller
    # passes ``additional_pre_text_transform='mirror([1, 0, 0])\n
    # '`` so the SCAD chain becomes ``rotate-align → mirror → text``.
    # Wide-shallow faces (and non-bottom faces) leave this empty so
    # only the standard rotation runs.
    pre_text_clause = additional_pre_text_transform
    if pre_text_clause and not pre_text_clause.endswith(("\n", "\n        ")):
        # Normalise indentation so the injected clause aligns with
        # rotation_clause / inner_translate_line.
        pre_text_clause = pre_text_clause.rstrip() + "\n            "

    scad_code = (
        f'// Generated by Kiln emboss_generator\n'
        f'// Mode: {mode} | Depth: {depth_mm} mm | Face: {face.get("face_name", "unknown")}\n'
        f'// Content type: {content_type}\n'
        f'\n'
        f'{boolean_op}() {{\n'
        f'    import("{_escape_scad_string(str(model_path))}");\n'
        f'    {translate_line}\n'
        f'        {rotation_clause}{inner_translate_line}{pre_text_clause}{content_block}\n'
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
    attempted: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _try_candidate(candidate: str) -> str | None:
        if not candidate or candidate in seen:
            return None
        seen.add(candidate)
        if not os.path.isfile(candidate):
            attempted.append((candidate, "not found"))
            return None
        if not os.access(candidate, os.X_OK):
            attempted.append((candidate, "not executable"))
            return None

        ok, reason = _probe_openscad_runs(candidate)
        if not ok:
            attempted.append((candidate, reason or "openscad --version failed"))
            return None

        _detect_and_cache_version(candidate)
        _warn_if_outdated()
        return candidate

    # Check env var fast path first (CLAUDE.md rule: env vars > config > defaults)
    if not openscad_path:
        openscad_path = os.environ.get("KILN_OPENSCAD_PATH")

    if openscad_path:
        found = _try_candidate(openscad_path)
        if found:
            return found

    # macOS bundle — handle versioned app names (e.g. OpenSCAD-2021.01.app)
    if platform.system() == "Darwin":
        import glob as _glob

        for pattern in [
            "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
            "/Applications/OpenSCAD-*.app/Contents/MacOS/OpenSCAD",
        ]:
            for mac_path in sorted(_glob.glob(pattern)):
                found = _try_candidate(mac_path)
                if found:
                    return found

    # $PATH
    on_path = shutil.which("openscad")
    if on_path:
        found = _try_candidate(on_path)
        if found:
            return found

    # Homebrew fallback (MCP servers may not inherit full $PATH)
    for brew_path in ["/opt/homebrew/bin/openscad", "/usr/local/bin/openscad"]:
        found = _try_candidate(brew_path)
        if found:
            return found

    attempted_msg = "\n".join(
        f"  - {path} -> {reason}"
        for path, reason in attempted
    )
    if attempted_msg:
        attempted_msg = "Tried OpenSCAD candidates:\n" + attempted_msg + "\n"
    hint = _openscad_sandbox_hint([reason for _path, reason in attempted])

    raise FileNotFoundError(
        "OpenSCAD not found or not usable on this machine.\n"
        + attempted_msg
        + hint
        + "Install it for 3D model generation:\n"
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


def _openscad_install_command() -> str:
    """The platform-appropriate command to install the modern OpenSCAD build."""
    system = platform.system()
    if system == "Darwin":
        return "brew install --cask openscad@snapshot"
    if system == "Linux":
        return "sudo snap install openscad --edge"
    return "Download the latest from https://openscad.org/downloads#snapshots"


def openscad_version_warning() -> dict | None:
    """A user-facing upgrade notice when the installed OpenSCAD is older than the
    minimum recommended year, else ``None``.

    An old build is ~20-100x slower (no Manifold backend) and silently fails SVG
    booleans, so a maker deserves to know — the buried ``_warn_if_outdated`` log
    isn't enough.  Callers (e.g. ``compile_scad``) attach this to their result so
    the warning shows up at the moment someone actually makes something, not only
    at the ``get_started`` front door.  Reuses the cached version detection, so it
    costs no extra subprocess.  A *missing* OpenSCAD returns ``None`` here — that
    case is handled prominently by ``get_started``.
    """
    version = get_openscad_version()
    if not version:
        return None
    year = _openscad_version_year(version)
    if not year or year >= _OPENSCAD_MIN_VERSION_YEAR:
        return None
    return {
        "version": version,
        "status": "outdated",
        "message": _OPENSCAD_UPGRADE_MSG,
        "install_command": _openscad_install_command(),
    }


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
