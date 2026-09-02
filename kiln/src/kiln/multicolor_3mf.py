"""Multi-color / multi-material 3MF composer.

Creates a single .3mf file from multiple STL inputs, with per-part
extruder/filament assignments AND per-part plate positions. Compatible with
BambuStudio (Bambu A1/X1/P1 + AMS), PrusaSlicer (MMU / ERCF), Cura, and
any 3MF-capable slicer.

The .3mf format is a ZIP archive. Each part becomes a separate ``<object>``
in ``3D/3dmodel.model``. Extruder assignments live in three places for
maximum slicer compatibility:

* ``Metadata/model_settings.config`` — BambuStudio reads ``extruder`` here.
* ``Metadata/Slic3r_PE_model.config`` — PrusaSlicer reads ``extruder`` here
  (measured: with a 4-extruder profile it ignores the item attribute below).
* ``slic3rpe:extruder`` attribute on each ``<item>`` — informational for
  other 3MF consumers.

One limitation to know about, relevant only when a user hand-loads the
composed 3MF into Bambu Studio (Kiln's own slicing never goes through
that GUI). Measured 2026-08 on Bambu Studio 02.06 with an A1, and
corroborated against Bambu's own tracker:

* Bambu Studio imports third-party 3MFs "geometry and color data only"
  (its own dialog on load) — per-object extruder assignments from
  ``model_settings.config`` ARE honored (verified: 4 filaments used, 75
  filament changes), but print settings inside the file, including a
  prime-tower position written to ``project_settings.config`` or a
  ``<plate>`` block, are ignored. This is their documented design, not a
  quirk of our file: bambulab/BambuStudio#7775, #2491.
* Its own DEFAULT prime-tower placement can land outside the plate,
  producing "A G-code path goes beyond plate boundaries". This is not
  caused by anything we write and is not fixable from our side — it is
  tracked upstream as an open issue that also affects natively-created
  A1 projects (bambulab/BambuStudio#7375). Verified remedy: drag the
  prime tower onto the plate; the same project then slices clean.

Multi-extruder results carry this as ``slicer_note`` so agents can pass
it on at the moment they hand the file over — and stay silent otherwise.

**Two distinct use cases, one tool:**

1. **Multi-color single object** — parts share the same XY origin (they overlap
   geometrically, like a coaster body + QR pads). Leave ``x/y/z = 0``::

       compose_multicolor_3mf([
           ColorPart("/tmp/body.stl",    extruder=1),
           ColorPart("/tmp/qr_pads.stl", extruder=2),
       ])

2. **Multi-item plate** — separate objects arranged side by side, each with
   its own material. Set ``x/y`` to position each item on the plate, or call
   ``auto_arrange_parts()`` to get positions automatically::

       parts = [
           ColorPart("/tmp/coaster1_body.stl", extruder=1, name="coaster1_body"),
           ColorPart("/tmp/coaster1_qr.stl",   extruder=2, name="coaster1_qr"),
           ColorPart("/tmp/coaster2_body.stl",  extruder=1, name="coaster2_body",  x=100.0),
           ColorPart("/tmp/coaster2_qr.stl",    extruder=2, name="coaster2_qr",    x=100.0),
       ]
       result = compose_multicolor_3mf(parts)

   Or let Kiln arrange them::

       positioned = auto_arrange_parts([
           {"stl_path": "/tmp/coaster1_body.stl", "extruder": 1, "group": 0},
           {"stl_path": "/tmp/coaster1_qr.stl",   "extruder": 2, "group": 0},
           {"stl_path": "/tmp/coaster2_body.stl",  "extruder": 1, "group": 1},
           {"stl_path": "/tmp/coaster2_qr.stl",    "extruder": 2, "group": 1},
       ], plate_width=256, plate_depth=256, gap_mm=5)
       result = compose_multicolor_3mf(positioned)

Paywalls
--------
**Free tier (public Kiln):**

* Position-aware composition with per-part transforms
* Basic row auto-arrangement (left-to-right, wraps rows, gap control)
* Full material safety: incompatibility block, hardware warnings, pair-by-pair
  report, purge matrix, flush matrix embedded in the 3MF

**kiln-pro:**

* **MaxRects 2D bin-packing** — rotates parts, handles mixed sizes, returns
  plate utilization %, multi-plate fallback for large runs.  This is the actual
  revenue driver; table-lookup compatibility data is not.
* Auto-tuned exotic material settings (PA-CF, PC-ABS, ASA+interface, etc.)

Safety features are **never** paywalled.  Kiln will always block incompatible
pairings regardless of subscription tier.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import struct
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiln.preview_render import downscale_png, effective_supersample

logger = logging.getLogger(__name__)

# Attached to multi-extruder compose results (and relayed by the tools that
# emit these files) so the rare user who hand-loads the 3MF into Bambu
# Studio hears about the detour from us, not from a red error banner.
# Everything in this note is measured (2026-08, Bambu Studio 02.06 on an
# A1) or documented upstream — see the module docstring for the citations.
MULTI_EXTRUDER_SLICER_NOTE = (
    "This only matters if you hand-load the 3MF into Bambu Studio: it "
    "imports third-party files as geometry and color only (its stated "
    "policy), and its default prime-tower spot can sit off the plate — "
    "not a defect in this file. Drag the prime tower onto the plate "
    "there and it slices clean with all colors intact. Kiln's own print "
    "path slices this file as-is (verified in PrusaSlicer)."
)

# ---------------------------------------------------------------------------
# Thumbnail generation
# ---------------------------------------------------------------------------

_THUMBNAIL_SIZE = 512

#: Above this the pure-Python colored painter costs more than the OpenSCAD
#: subprocess it replaces; the grey fallback takes over.
_COLORED_THUMBNAIL_MAX_TRIANGLES = 200_000


def _part_rgb_hex(color: str | None) -> str | None:
    """Normalize a part's color hint to ``#RRGGBB``, or ``None``.

    Accepts bare and ``#``-prefixed hex: 6 or 8 digits (alpha dropped),
    and the CSS shorthands ``#RGB`` / ``#RGBA`` (each nibble doubled).
    Anything else is not a color claim.  Shorthand matters because a
    rejected hint here is SILENT downstream — the colorgroup is simply
    omitted while the composing tool still reports success, so a user
    who wrote ``#F00`` got a grey file with no hint why.
    """
    if not color:
        return None
    value = color.strip().lstrip("#")
    if len(value) == 4:
        value = value[:3]
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) == 8:
        value = value[:6]
    if len(value) != 6:
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return "#" + value.upper()


def _part_rgb(color: str | None) -> tuple[int, int, int] | None:
    """The part's color hint as an RGB tuple, or ``None``."""
    hex_color = _part_rgb_hex(color)
    if hex_color is None:
        return None
    return (
        int(hex_color[1:3], 16),
        int(hex_color[3:5], 16),
        int(hex_color[5:7], 16),
    )


def _generate_thumbnail(
    parsed: list[_ParsedPart],
    width: int = _THUMBNAIL_SIZE,
    height: int = _THUMBNAIL_SIZE,
) -> bytes | None:
    """Render the ``Metadata/plate_1.png`` thumbnail for the composed 3MF.

    First choice is Kiln's own colored renderer — pure Python, no OpenSCAD
    needed, and it paints the parts in their REAL colors: a multicolor 3MF
    whose thumbnail is grey undersells the print on every slicer LCD and
    file browser it lands in.  Falls back to the OpenSCAD grey render for
    meshes too big to paint in Python, and to ``None`` when neither path
    is available.  A thumbnail must never fail the compose.

    *width* and *height* default to the square plate thumbnail.  Callers
    filling a slicer's non-square slots pass those dimensions instead of
    stretching a square render into them, which turns a round part oval.
    """
    if not parsed:
        return None
    total_triangles = sum(len(t) for _, _, t in parsed)
    if total_triangles <= _COLORED_THUMBNAIL_MAX_TRIANGLES:
        try:
            data = _render_colored_thumbnail(parsed, width=width, height=height)
            if data:
                return data
        except Exception:  # noqa: BLE001 — enrichment, never a compose failure
            logger.debug(
                "colored thumbnail failed — falling back to OpenSCAD",
                exc_info=True,
            )
    return _generate_thumbnail_openscad(
        [p.stl_path for p, _, _ in parsed],
        [(p.x, p.y, p.z) for p, _, _ in parsed],
        width=width,
        height=height,
    )


def _render_colored_thumbnail(
    parsed: list[_ParsedPart],
    width: int = _THUMBNAIL_SIZE,
    height: int = _THUMBNAIL_SIZE,
) -> bytes | None:
    """PNG bytes from the colored renderer, honoring per-part placement."""
    from kiln.colored_renderer import render_colored_mesh
    from kiln.threemf_parser import _DEFAULT_COLOR, ColoredTriangle

    triangles: list[ColoredTriangle] = []
    for part, vertices, faces in parsed:
        rgb = _part_rgb(part.color) or _DEFAULT_COLOR
        dx, dy, dz = part.x, part.y, part.z
        for a, b, c in faces:
            triangles.append(
                ColoredTriangle(
                    v0=(vertices[a][0] + dx, vertices[a][1] + dy, vertices[a][2] + dz),
                    v1=(vertices[b][0] + dx, vertices[b][1] + dy, vertices[b][2] + dz),
                    v2=(vertices[c][0] + dx, vertices[c][1] + dy, vertices[c][2] + dz),
                    color=rgb,
                )
            )
    if not triangles:
        return None
    result = render_colored_mesh(triangles, width=width, height=height)
    path = result.path
    try:
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def _generate_thumbnail_openscad(
    stl_paths: list[str],
    offsets: list[tuple[float, float, float]] | None = None,
    width: int = _THUMBNAIL_SIZE,
    height: int = _THUMBNAIL_SIZE,
) -> bytes | None:
    """Render a plate thumbnail PNG from STL files via OpenSCAD.

    Imports all STL parts into a single scene so the thumbnail shows
    the complete model as it will be printed.  Uses preview mode (not
    full render) so non-manifold meshes work, and applies a neutral
    grey color with the DeepOcean colorscheme for high contrast on
    printer LCDs.

    Args:
        stl_paths: Mesh files to render.
        offsets: Optional per-part ``(x, y, z)`` plate translations,
            parallel to *stl_paths*.  Without them, N spaced copies of
            one mesh render stacked — a thumbnail showing one object
            for a four-object plate.
        width: Output width in pixels.
        height: Output height in pixels.

    Returns PNG bytes suitable for embedding as ``Metadata/plate_1.png``
    in a 3MF archive, or ``None`` if OpenSCAD is unavailable.
    """
    if not stl_paths:
        return None
    try:
        from kiln.generation.openscad import OpenSCADProvider
        from kiln.openscad_runner import run_openscad

        provider = OpenSCADProvider()
        binary = provider._binary
        if not binary:
            return None

        # Build a SCAD file that imports all parts (at their plate
        # positions when given) with a neutral colour so the model is
        # visible against any colorscheme background.
        if offsets is None:
            offsets = [(0.0, 0.0, 0.0)] * len(stl_paths)
        imports = "\n".join(
            f'  translate([{ox:.4f}, {oy:.4f}, {oz:.4f}]) '
            f'import("{Path(p).resolve()}");'
            for p, (ox, oy, oz) in zip(stl_paths, offsets, strict=True)
            if os.path.isfile(p)
        )
        if not imports:
            return None

        scad_code = f"color([0.75, 0.75, 0.80]) {{\n{imports}\n}}\n"
        fd, scad_path = tempfile.mkstemp(suffix=".scad", prefix="kiln_thumb_")
        fd2, png_path = tempfile.mkstemp(suffix=".png", prefix="kiln_thumb_")
        os.close(fd2)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(scad_code)

            # Preview mode (no --render) avoids CGAL failures on
            # non-manifold STLs.  DeepOcean gives a dark background
            # with good contrast for printer LCD thumbnails.
            # Supersample: render oversized then Lanczos-downscale for a
            # crisp LCD thumbnail — same shared knob as every preview.
            ss = effective_supersample()
            cmd = [
                binary,
                "-o", png_path,
                f"--imgsize={width * ss},{height * ss}",
                "--autocenter",
                "--viewall",
                "--colorscheme", "DeepOcean",
                scad_path,
            ]
            run_openscad(cmd, timeout=30, output_path=png_path)
            if os.path.isfile(png_path) and os.path.getsize(png_path) > 0:
                if ss > 1:
                    downscale_png(png_path, width, height)
                return Path(png_path).read_bytes()
            return None
        finally:
            for p in (scad_path, png_path):
                with contextlib.suppress(OSError):
                    os.unlink(p)
    except Exception:
        logger.debug("Thumbnail generation skipped (OpenSCAD unavailable or render failed)")
        return None


def _colors_for_parts(
    stl_paths: list[str],
    declared: list[str] | None,
) -> list[str | None]:
    """Map a file's DECLARED filament colors onto its parts, or refuse to.

    The preview exists to tell the truth about the print, so a color may
    only be painted when the mapping from declaration to geometry is
    unambiguous.  Two cases are:

    * one distinct declared color — every part prints in it, whatever the
      part count;
    * one color per part — the assignment the declaration already makes.

    Anything else is a guess.  Four declared filaments against a single
    undifferentiated mesh cannot say WHICH of them that mesh prints in,
    and picking the first would state something the file never claimed —
    so those render neutral.  Neutral is honest; invented is not.

    :returns: A per-part list, entries ``None`` where no color is claimed.
    """
    usable = [c for c in (declared or []) if c]
    if not usable:
        return [None] * len(stl_paths)
    distinct = set(usable)
    if len(distinct) == 1:
        return [usable[0]] * len(stl_paths)
    if len(usable) == len(stl_paths):
        return list(usable)
    logger.info(
        "Thumbnail rendered neutral: %d declared filament colors cannot be "
        "mapped onto %d part(s) without guessing which prints in which.",
        len(usable), len(stl_paths),
    )
    return [None] * len(stl_paths)


def render_plate_thumbnail(
    stl_paths: list[str],
    colors: list[str] | None = None,
    width: int = _THUMBNAIL_SIZE,
    height: int = _THUMBNAIL_SIZE,
) -> bytes | None:
    """Render a plate thumbnail from bare mesh PATHS, in its declared colors.

    :func:`_generate_thumbnail` wants parsed geometry, because that is what
    the colored renderer paints.  A caller holding only file paths — every
    gcode-wrapping path does — cannot reach it without doing that parse
    first, and handing it the paths instead yields a mesh made of
    characters.  This is that parse, so a wrap gets the SAME colored
    preview :func:`compose_multicolor_3mf` embeds rather than nothing.

    :param stl_paths: Mesh files making up the plate.
    :param colors: The ``#RRGGBB`` colors the 3MF ITSELF declares, so the
        preview and the file agree on what comes off the printer.  Never
        a house default: ``None`` renders neutral, because a preview that
        shows a color the file never claimed misrepresents the print, and
        that is worse than showing no color at all.  See
        :func:`_colors_for_parts` for how a declaration maps onto parts.
    :param width: Output width in pixels.
    :param height: Output height in pixels.
    :returns: PNG bytes, or ``None`` when no part yielded geometry.
    """
    part_colors = _colors_for_parts(stl_paths, colors)
    parsed: list[_ParsedPart] = []
    for index, path in enumerate(stl_paths):
        try:
            vertices, triangles = _parse_mesh_file(path)
        except Exception:  # noqa: BLE001 — one bad part must not blank the plate
            logger.warning("Could not parse %s for thumbnail", path, exc_info=True)
            continue
        # Same degenerate-triangle guard compose_multicolor_3mf applies, so
        # the preview shows exactly the triangles a composed file would.
        kept = [t for t in triangles if len(set(t)) == 3]
        if not kept:
            logger.warning("No usable triangles in %s for thumbnail", path)
            continue
        parsed.append(
            (
                ColorPart(
                    stl_path=path,
                    extruder=index + 1,
                    name=Path(path).stem,
                    color=part_colors[index],
                ),
                vertices,
                kept,
            )
        )
    if not parsed:
        return None
    return _generate_thumbnail(parsed, width=width, height=height)


def render_plate_preview(
    stl_paths: list[str],
    colors: list[str] | None = None,
    width: int = _THUMBNAIL_SIZE,
    height: int = _THUMBNAIL_SIZE,
) -> bytes | None:
    """The canonical picture of a plate, for embedding in an emitted file.

    Goes through :func:`~kiln.model_visualizer.visualize_model`, the
    renderer behind every other preview Kiln shows, so an embedded
    thumbnail carries the same picture of the part the user already saw
    rather than a lesser one drawn just for that slot.  Its framing and
    lighting are the ones Kiln has tuned; a thumbnail is the last place
    to be re-deriving them.

    The stage backend is declined here.  It draws the plate grid the web
    viewer draws, which reads as scenery around a model on screen and as
    part of the model on a 2cm printer tile.  OpenSCAD renders the same
    angles and takes the filament colour.

    The share link is declined too, and that is the half that actually
    keeps this local: attaching one uploads the mesh, so without it every
    slice would ship a copy of the user's model to Kiln's API and wait on
    the reply — to fill in a URL that gets embedded nowhere and read by
    nobody.  Emitting a file must not depend on a network.

    Falls back to :func:`render_plate_thumbnail` for a multi-part plate,
    which ``visualize_model`` cannot draw as one scene, and returns
    ``None`` when nothing renders at all.  Colour rules are unchanged:
    the declared colour or neutral, never an invented one.
    """
    color = ""
    if colors and len(set(colors)) == 1:
        color = colors[0]

    if len(stl_paths) == 1:
        tmp_dir = tempfile.mkdtemp(prefix="kiln_thumb_")
        try:
            from kiln.model_visualizer import visualize_model

            result = visualize_model(
                stl_paths[0],
                output_dir=tmp_dir,
                width=width,
                height=height,
                angles=["isometric"],
                color=color,
                allow_stage=False,
                share_link=False,
            )
            for view in result.get("views", []):
                path = view.get("path")
                if path and os.path.isfile(path):
                    return Path(path).read_bytes()
            logger.warning(
                "visualize_model returned no image for %s — falling back.",
                stl_paths[0],
            )
        except Exception:  # noqa: BLE001 — a preview never blocks the emit
            logger.warning(
                "visualize_model failed for %s — falling back.",
                stl_paths[0],
                exc_info=True,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return render_plate_thumbnail(
        stl_paths, colors=colors, width=width, height=height,
    )


# ---------------------------------------------------------------------------
# Public data class
# ---------------------------------------------------------------------------


@dataclass
class ColorPart:
    """One part (color/material/position) in a multi-color or multi-item print.

    Attributes:
        stl_path: Absolute path to the STL file for this part.
        extruder: 1-indexed AMS/extruder slot number.  Maps directly to
            Bambu AMS trays (1 = tray 1).  Must be >= 1.
        name: Human-readable label shown in the slicer object list.
        color: Optional hex color hint ``"#RRGGBB"`` for slicer preview.
            Does not affect the physical print — display only.
        material: Optional filament label, e.g. ``"PLA Grey"`` (display only).
        x: X translation on the print plate in mm.  Default 0 (origin).
            Set this when placing multiple separate objects side-by-side.
        y: Y translation on the print plate in mm.  Default 0 (origin).
        z: Z translation in mm.  Usually 0; set for elevated/stacked parts.

    **Single-object multi-color** (parts overlap geometrically):
        Leave x/y/z at 0 — parts share the same coordinate space.

    **Multi-item plate** (separate objects):
        Set x/y to position each object.  Use ``auto_arrange_parts()`` to
        calculate non-overlapping positions automatically.
    """

    stl_path: str
    extruder: int = 1
    name: str = ""
    color: str | None = None
    material: str | None = None
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


# ---------------------------------------------------------------------------
# STL parsing
# ---------------------------------------------------------------------------


def _parse_stl(
    stl_path: str,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse binary or ASCII STL → (vertices, triangles) with deduplication.

    Vertices are deduplicated so the 3MF mesh is as compact as possible.
    Triangle winding order is preserved.

    Returns:
        vertices: unique ``(x, y, z)`` float tuples
        triangles: ``(v1_idx, v2_idx, v3_idx)`` index tuples into *vertices*
    """
    data = Path(stl_path).read_bytes()

    # Heuristic: binary STLs rarely start with "solid" and contain valid UTF-8.
    # We check for the ASCII keyword AND the presence of "facet normal" to be sure.
    is_ascii = False
    if data[:5].lower() == b"solid":
        try:
            text = data.decode("utf-8", errors="strict")
            if "facet normal" in text:
                is_ascii = True
        except UnicodeDecodeError:
            pass

    if is_ascii:
        return _parse_ascii_stl(data.decode("utf-8", errors="replace"))
    return _parse_binary_stl(data)


def _parse_binary_stl(
    data: bytes,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse a binary STL byte string."""
    if len(data) < 84:
        raise ValueError(f"Binary STL too small: {len(data)} bytes (need ≥ 84)")

    (num_triangles,) = struct.unpack_from("<I", data, 80)
    expected = 84 + num_triangles * 50
    if len(data) < expected:
        raise ValueError(
            f"Binary STL truncated: {len(data)} bytes, need {expected} "
            f"for {num_triangles:,} triangles"
        )

    vertex_map: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []

    for i in range(num_triangles):
        base = 84 + i * 50 + 12  # skip 12-byte normal vector
        tri: list[int] = []
        for v in range(3):
            vbase = base + v * 12
            x, y, z = struct.unpack_from("<fff", data, vbase)
            pt = (x, y, z)
            if pt not in vertex_map:
                vertex_map[pt] = len(vertices)
                vertices.append(pt)
            tri.append(vertex_map[pt])
        triangles.append((tri[0], tri[1], tri[2]))

    return vertices, triangles


def _parse_ascii_stl(
    text: str,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse an ASCII STL string."""
    vertex_map: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    pending: list[int] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("vertex "):
            toks = line.split()
            try:
                pt = (float(toks[1]), float(toks[2]), float(toks[3]))
            except (IndexError, ValueError):
                continue
            if pt not in vertex_map:
                vertex_map[pt] = len(vertices)
                vertices.append(pt)
            pending.append(vertex_map[pt])
        elif line == "endfacet":
            if len(pending) == 3:
                triangles.append((pending[0], pending[1], pending[2]))
            pending = []

    return vertices, triangles


def _parse_mesh_file(
    mesh_path: str,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse an STL, OBJ, or GLB mesh → deduplicated (vertices, triangles).

    STL is parsed natively; OBJ and GLB reuse the generation-pipeline
    parsers and are re-indexed into the same compact form.
    """
    ext = Path(mesh_path).suffix.lower()
    if ext in ("", ".stl"):
        return _parse_stl(mesh_path)
    if ext not in (".obj", ".glb"):
        raise ValueError(
            f"Unsupported mesh format {ext!r} for {mesh_path} "
            "(need .stl, .obj, or .glb)"
        )

    from kiln.generation import validation as _validation

    errors: list[str] = []
    if ext == ".obj":
        raw_tris, _ = _validation._parse_obj(Path(mesh_path), errors)
    else:
        raw_tris, _ = _validation._parse_glb(Path(mesh_path), errors)
    if errors:
        raise ValueError(f"Failed to parse {mesh_path}: {'; '.join(errors)}")

    vertex_map: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    for tri in raw_tris:
        indices: list[int] = []
        for v in tri:
            pt = (float(v[0]), float(v[1]), float(v[2]))
            if pt not in vertex_map:
                vertex_map[pt] = len(vertices)
                vertices.append(pt)
            indices.append(vertex_map[pt])
        triangles.append((indices[0], indices[1], indices[2]))
    return vertices, triangles


# ---------------------------------------------------------------------------
# 3MF XML / ZIP builders
# ---------------------------------------------------------------------------


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


_CONTENT_TYPES = """\
<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels"   ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model"  ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
  <Default Extension="config" ContentType="text/xml"/>
  <Default Extension="png"    ContentType="image/png"/>
</Types>"""

_RELS = """\
<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel-1"
    Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""


# Type alias for parsed part tuple
_ParsedPart = tuple[ColorPart, list[tuple[float, float, float]], list[tuple[int, int, int]]]


def _display_names(parsed: list[_ParsedPart]) -> list[str]:
    """The object names, XML-escaped, in part order.

    Computed ONCE for the three documents that must agree about them — the
    core model, BambuStudio's ``model_settings.config``, and the PrusaSlicer
    family's ``Slic3r_PE_model.config`` — because a slicer's object list and
    the model it names have to be the same list.

    Names are made unique first
    (:func:`~kiln.threemf_parser.unique_object_names`): two parts called
    "body" is an ordinary thing for a caller to ask for, and colour is read
    back per object BY NAME, so leaving the duplicate would cost the file
    every colour it carries.  That helper also supplies ``Part N`` for a part
    whose name is blank — one place names the nameless, so a plate composed
    here and a CAD file imported elsewhere label them the same way.
    """
    from kiln.threemf_parser import unique_object_names

    return [
        _xml_escape(name)
        for name in unique_object_names([part.name for part, _, _ in parsed])
    ]


def _build_model_xml(parsed: list[_ParsedPart]) -> str:
    """Build ``3D/3dmodel.model`` XML containing all mesh objects.

    Part colors are written where SPEC-COMPLIANT readers look, not only in
    the slicer sidecar: one ``<m:colorgroup>`` entry per distinct part
    color, referenced from every triangle of a colored object
    (``pid``/``p1``).  That exact shape — colorgroup plus per-triangle
    references, and deliberately NO object-level ``pid`` — is the one the
    web viewer's color-preserve tests drive through three.js' real
    3MFLoader, which bakes it to vertex colors; kiln.threemf_parser
    resolves it too (a single effective color per object).  The sidecar
    alone kept the colors invisible to every reader but the slicers that
    wrote the convention.
    """
    palette: list[str] = []
    part_pindex: dict[int, int] = {}
    for obj_id, (part, _, _) in enumerate(parsed, start=1):
        rgb_hex = _part_rgb_hex(part.color)
        if rgb_hex is None:
            continue
        if rgb_hex not in palette:
            palette.append(rgb_hex)
        part_pindex[obj_id] = palette.index(rgb_hex)
    # Object ids stay 1..N (model_settings.config references them); the
    # color group takes the next free resource id.
    colorgroup_id = len(parsed) + 1

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<model unit="millimeter" xml:lang="en-US"',
        '  xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"',
        '  xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02"',
        '  xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06"',
        '  xmlns:bambu="http://bambulab.com/model/2021"',
        '  xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">',
        '  <metadata name="Application">Kiln</metadata>',
        # The Bambu-family project-version stamp.  Without it OrcaSlicer
        # classifies a file carrying slicer sidecars as "generated by an old
        # OrcaSlicer version" and warns it is loading geometry only.  Safe by
        # both forks' readers (verified in bbs_3mf.cpp, Orca and Bambu): the
        # key only sets an integer version — a file is treated as a
        # BambuStudio/OrcaSlicer PROJECT solely when the Application metadata
        # starts with their names, which ours never does.
        '  <metadata name="BambuStudio:3mfVersion">1</metadata>',
        "  <resources>",
    ]
    if palette:
        lines.append(f'    <m:colorgroup id="{colorgroup_id}">')
        lines += [f'      <m:color color="{c}"/>' for c in palette]
        lines.append("    </m:colorgroup>")

    names = _display_names(parsed)
    for obj_id, (_part, vertices, triangles) in enumerate(parsed, start=1):
        name = names[obj_id - 1]
        pindex = part_pindex.get(obj_id)
        tri_ref = (
            f' pid="{colorgroup_id}" p1="{pindex}"' if pindex is not None else ""
        )
        lines += [
            f'    <object id="{obj_id}" type="model" name="{name}">',
            "      <mesh>",
            "        <vertices>",
        ]
        for x, y, z in vertices:
            lines.append(f'          <vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>')
        lines += [
            "        </vertices>",
            "        <triangles>",
        ]
        for v1, v2, v3 in triangles:
            lines.append(f'          <triangle v1="{v1}" v2="{v2}" v3="{v3}"{tri_ref}/>')
        lines += [
            "        </triangles>",
            "      </mesh>",
            "    </object>",
        ]

    lines.append("  </resources>")
    lines.append("  <build>")
    for obj_id, (part, _, _) in enumerate(parsed, start=1):
        # 3MF transform is a 4x3 column-major matrix: r00..r22 tx ty tz
        # Identity rotation + translation by (part.x, part.y, part.z)
        tx, ty, tz = part.x, part.y, part.z
        transform = f"1 0 0 0 1 0 0 0 1 {tx:.6f} {ty:.6f} {tz:.6f}"
        lines.append(
            f'    <item objectid="{obj_id}"'
            f' transform="{transform}"'
            f' slic3rpe:extruder="{part.extruder}"/>'
        )
    lines += ["  </build>", "</model>"]

    return "\n".join(lines)


def _build_prusa_model_config(parsed: list[_ParsedPart]) -> str:
    """Build ``Metadata/Slic3r_PE_model.config`` — the PrusaSlicer channel.

    PrusaSlicer reads per-object settings ONLY from this file: each object
    carries a ``<volume firstid lastid>`` spanning its triangles plus an
    ``extruder`` config entry (verified against the reader in
    ``src/libslic3r/Format/3mf.cpp`` — the ``slic3rpe:extruder`` build-item
    attribute this composer also writes appears nowhere in it, and a
    4-extruder profile confirmed it end to end: everything printed with
    extruder 1 until this file was present).  Without this file a
    multicolor 3MF sliced in the PrusaSlicer family prints entirely with
    extruder 1 — no tool change, colors silently gone.
    """
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<config>",
    ]
    names = _display_names(parsed)
    for obj_id, (part, _, triangles) in enumerate(parsed, start=1):
        if not triangles:
            continue  # a rangeless volume would make the reader reject the file
        name = names[obj_id - 1]
        lines += [
            f' <object id="{obj_id}" instances_count="1">',
            f'  <volume firstid="0" lastid="{len(triangles) - 1}">',
            f'   <metadata type="volume" key="name" value="{name}"/>',
            f'   <metadata type="volume" key="extruder" value="{part.extruder}"/>',
            "  </volume>",
            f'  <metadata type="object" key="name" value="{name}"/>',
            f'  <metadata type="object" key="extruder" value="{part.extruder}"/>',
            " </object>",
        ]
    lines.append("</config>")
    return "\n".join(lines)


def _build_model_settings(parsed: list[_ParsedPart]) -> str:
    """Build ``Metadata/model_settings.config`` for BambuStudio.

    BambuStudio reads extruder assignments from this file, not from the
    ``slic3rpe:extruder`` attribute.  Both are written for cross-slicer
    compatibility.
    """
    lines: list[str] = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<config>",
    ]
    names = _display_names(parsed)
    for obj_id, (part, _, _) in enumerate(parsed, start=1):
        name = names[obj_id - 1]
        lines.append(f'  <object id="{obj_id}">')
        lines.append(f'    <metadata key="name"     value="{name}"/>')
        lines.append(f'    <metadata key="extruder" value="{part.extruder}"/>')
        if part.color:
            color_hex = _xml_escape(part.color.lstrip("#"))
            lines.append(f'    <metadata key="color"    value="{color_hex}"/>')
        if part.material:
            mat = _xml_escape(part.material)
            lines.append(f'    <metadata key="material" value="{mat}"/>')
        lines.append("  </object>")
    lines.append("</config>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# BambuStudio project settings (flush matrix)
# ---------------------------------------------------------------------------


def _build_project_settings(flush_matrix_str: str) -> str:
    """Build ``Metadata/project_settings.config`` for BambuStudio.

    BambuStudio reads the flush (purge) volume matrix from this file to size
    the purge tower between material changes.  The ``flush_volumes_matrix``
    key is a flat space-separated string of N×N integer mm³ volumes.

    Args:
        flush_matrix_str: Space-separated flush volumes from
            :func:`~kiln.material_safety.build_bambu_flush_matrix`.

    Returns:
        JSON string ready to embed in the 3MF ZIP.
    """
    import json

    # Parse back to int list for JSON embedding
    values = [int(v) for v in flush_matrix_str.split()] if flush_matrix_str.strip() else []
    return json.dumps(
        {
            "flush_volumes_matrix": values,
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Bed placement (shared by both composers)
# ---------------------------------------------------------------------------

#: Matches kiln.printers.bed_fit._FIT_EPSILON_MM — floating-point noise on
#: the plate edge must not trigger a re-placement.
_BED_EPSILON_MM = 0.5


def _resolve_plate(
    plate_width: float, plate_depth: float, printer_id: str | None,
) -> tuple[float, float]:
    """Plate XY dimensions, from the printer when it resolves.

    An unresolvable ``printer_id`` keeps the caller's dimensions rather
    than failing: bed placement is a correction, never a reason to
    refuse a compose.
    """
    if printer_id:
        try:
            from kiln.printers.bed_fit import resolve_build_volume

            resolved = resolve_build_volume(printer_id)
        except Exception:  # noqa: BLE001 — placement is advisory, never fatal
            resolved = None
            logger.debug("build volume lookup failed for %r", printer_id,
                         exc_info=True)
        if resolved is not None:
            _model_id, build_volume = resolved
            return build_volume[0], build_volume[1]
    return plate_width, plate_depth


def _plate_translation(
    min_x: float, max_x: float,
    min_y: float, max_y: float,
    min_z: float,
    plate_width: float, plate_depth: float,
) -> tuple[float, float, float]:
    """The translation that puts a bbox on the plate — zeros when it fits.

    Both composers write 3MF build transforms that slicers honour
    literally: PrusaSlicer auto-centres a loose STL but takes a 3MF at
    its word, so geometry off a corner-origin bed is silently "outside
    of the print volume" (exit 0, no gcode).  A bbox already inside the
    plate returns ``(0, 0, 0)`` so deliberate placement is respected;
    otherwise the bbox is centred on the plate and floored to z=0.
    """
    if (
        min_x >= -_BED_EPSILON_MM and max_x <= plate_width + _BED_EPSILON_MM
        and min_y >= -_BED_EPSILON_MM and max_y <= plate_depth + _BED_EPSILON_MM
        and min_z >= -_BED_EPSILON_MM
    ):
        return (0.0, 0.0, 0.0)
    tx = plate_width / 2.0 - (min_x + max_x) / 2.0
    ty = plate_depth / 2.0 - (min_y + max_y) / 2.0
    tz = -min_z
    # 0.0 + x normalises -0.0 (e.g. -min(0.0)) out of the emitted XML
    return (0.0 + tx, 0.0 + ty, 0.0 + tz)


# ---------------------------------------------------------------------------
# Bounding box helper
# ---------------------------------------------------------------------------


def _stl_bounding_box(stl_path: str) -> tuple[float, float, float, float, float, float]:
    """Return (min_x, min_y, min_z, max_x, max_y, max_z) for a mesh file."""
    vertices, _ = _parse_mesh_file(stl_path)
    if not vertices:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


# ---------------------------------------------------------------------------
# Auto-arrangement (free tier — basic row layout)
# ---------------------------------------------------------------------------


def auto_arrange_parts(
    part_specs: list[dict[str, Any]],
    *,
    plate_width: float = 256.0,
    plate_depth: float = 256.0,
    gap_mm: float = 5.0,
    printer_id: str | None = None,
) -> list[ColorPart]:
    """Arrange multiple parts on a print plate without overlapping.

    **Free tier** — uses a simple left-to-right row layout.  Parts that share
    the same ``group`` index are treated as a multi-color unit and placed at
    the same XY position (they overlap intentionally, like body + QR pads).
    Different groups are spaced apart.

    For smart 2D bin-packing that maximises plate density, use Kiln Pro's
    ``auto_arrange_parts_on_plate``.

    Args:
        part_specs: List of dicts, each with:

            * ``stl_path`` (str) — path to the STL
            * ``extruder`` (int) — AMS slot, 1-indexed
            * ``group`` (int, optional) — parts sharing a group are placed at
              the same position (multi-color unit).  Default: each part is its
              own group.
            * ``name`` (str, optional)
            * ``color`` (str, optional)
            * ``material`` (str, optional)

        plate_width: Print plate X dimension in mm (default 256 for legacy
          callers without a printer id).
        plate_depth: Print plate Y dimension in mm (default 256 for legacy
          callers without a printer id).
        gap_mm: Minimum gap between groups in mm.
        printer_id: Optional supported printer model id.  When provided,
          bundled printer-intelligence build volume overrides ``plate_width``
          and ``plate_depth``.

    Returns:
        List of :class:`ColorPart` with ``x/y`` positions set, ready to pass
        directly to :func:`compose_multicolor_3mf`.

    Example — two coasters on one plate::

        parts = auto_arrange_parts([
            {"stl_path": "/tmp/c1_body.stl", "extruder": 1, "group": 0, "name": "c1_body"},
            {"stl_path": "/tmp/c1_qr.stl",   "extruder": 2, "group": 0, "name": "c1_qr"},
            {"stl_path": "/tmp/c2_body.stl",  "extruder": 1, "group": 1, "name": "c2_body"},
            {"stl_path": "/tmp/c2_qr.stl",    "extruder": 2, "group": 1, "name": "c2_qr"},
        ], printer_id="bambu_a1", gap_mm=5)
        result = compose_multicolor_3mf(parts, printer_id="bambu_a1")
        # Same printer on both calls: the composer re-centres a group it
        # judges off ITS plate, so arranging for one bed and composing
        # against another undoes the arrangement.
    """
    if printer_id:
        from kiln.printers.bed_fit import resolve_build_volume

        resolved = resolve_build_volume(printer_id)
        if resolved is None:
            raise ValueError(
                f"Unknown printer_id {printer_id!r}; omit printer_id and "
                "pass plate_width/plate_depth explicitly, or use a "
                "supported printer model id."
            )
        _model_id, build_volume = resolved
        plate_width = build_volume[0]
        plate_depth = build_volume[1]

    # Assign default groups (each spec is its own group if not specified).
    # Track group per spec in a parallel list so the result-build pass never
    # calls list.index() — which would give wrong results for identical dicts.
    groups: dict[int, list[dict[str, Any]]] = {}
    spec_groups: list[int] = []          # parallel to part_specs
    for i, spec in enumerate(part_specs):
        g = int(spec.get("group", i))
        groups.setdefault(g, []).append(spec)
        spec_groups.append(g)

    # For each group, take the union bounding box of all its parts.  The
    # union MIN matters as much as the size: meshes are frequently centered
    # on the origin (negative min), and placing one at a cursor position
    # without subtracting its min leaves it hanging off the plate corner.
    group_order = sorted(groups.keys())
    # group → (min_x, min_y, width, depth) of the union bbox
    group_bboxes: dict[int, tuple[float, float, float, float]] = {}
    for g in group_order:
        mn_x = mn_y = float("inf")
        mx_x = mx_y = float("-inf")
        for spec in groups[g]:
            try:
                b_mn_x, b_mn_y, _, b_mx_x, b_mx_y, _ = _stl_bounding_box(spec["stl_path"])
            except Exception:
                b_mn_x, b_mn_y = 0.0, 0.0   # fallback if mesh unreadable
                b_mx_x, b_mx_y = 50.0, 50.0
            mn_x, mn_y = min(mn_x, b_mn_x), min(mn_y, b_mn_y)
            mx_x, mx_y = max(mx_x, b_mx_x), max(mx_y, b_mx_y)
        group_bboxes[g] = (mn_x, mn_y, mx_x - mn_x, mx_y - mn_y)

    # Simple row layout: place groups left-to-right, wrap to next row when
    # the plate width would be exceeded.  Each group is translated so its
    # union bbox min lands on the cursor; parts within a group share one
    # translation, preserving their relative positions.
    group_positions: dict[int, tuple[float, float]] = {}  # group → translation
    cursor_x, cursor_y, row_depth = 0.0, 0.0, 0.0
    used_w, used_d = 0.0, 0.0
    for g in group_order:
        mn_x, mn_y, w, d = group_bboxes[g]
        if cursor_x > 0 and cursor_x + w > plate_width:
            # Wrap to next row
            cursor_x = 0.0
            cursor_y += row_depth + gap_mm
            row_depth = 0.0
        group_positions[g] = (cursor_x - mn_x, cursor_y - mn_y)
        used_w = max(used_w, cursor_x + w)
        used_d = max(used_d, cursor_y + d)
        cursor_x += w + gap_mm
        row_depth = max(row_depth, d)

    # Center the whole arrangement on the plate when it fits.
    shift_x = (plate_width - used_w) / 2.0 if 0.0 < used_w <= plate_width else 0.0
    shift_y = (plate_depth - used_d) / 2.0 if 0.0 < used_d <= plate_depth else 0.0
    if shift_x or shift_y:
        group_positions = {
            g: (tx + shift_x, ty + shift_y) for g, (tx, ty) in group_positions.items()
        }

    # Build final ColorPart list with positions
    result: list[ColorPart] = []
    for i, spec in enumerate(part_specs):
        g = spec_groups[i]
        px, py = group_positions.get(g, (0.0, 0.0))
        result.append(ColorPart(
            stl_path=str(spec["stl_path"]),
            extruder=int(spec.get("extruder", 1)),
            name=str(spec.get("name", "")),
            color=spec.get("color"),
            material=spec.get("material"),
            x=px,
            y=py,
            z=float(spec.get("z", 0.0)),
        ))
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compose_multicolor_3mf(
    parts: list[ColorPart],
    output_path: str | None = None,
    *,
    plate_width: float = 256.0,
    plate_depth: float = 256.0,
    printer_id: str | None = None,
) -> dict[str, Any]:
    """Compose a multi-color / multi-material .3mf from multiple STL files.

    Creates a single print-ready .3mf containing all parts with per-part
    AMS/extruder assignments.  All parts must share the same coordinate
    origin — their relative layout is preserved exactly as positioned in
    the STL files (when the group as a whole sits off the plate, one
    common translation moves it onto the bed; see ``plate_width``).

    Compatible slicers:
        * **BambuStudio** — reads ``Metadata/model_settings.config``
        * **PrusaSlicer** — reads ``slic3rpe:extruder`` on ``<item>``
        * **Cura** — reads standard 3MF objects (manual extruder assignment)
        * Any slicer that supports 3MF Core + multiple objects

    Args:
        parts: List of :class:`ColorPart`.  Each part needs a mesh path
            (.stl, .obj, or .glb) and extruder number.  Extruder numbers map
            directly to Bambu AMS trays (1-indexed).  Each part is placed at
            its ``x/y/z`` translation on top of its source coordinates.
        output_path: Where to write the .3mf.  Defaults to a system temp
            file (path returned in the result dict).
        plate_width: Print plate X dimension in mm (default 256, same
            legacy default as :func:`auto_arrange_parts`).  When the
            union bbox of every placed part misses the plate, ONE common
            translation is added to every build item so the whole group
            lands on the bed with its relative layout intact — slicers
            honour 3MF placement literally (PrusaSlicer auto-centres a
            loose STL but never a 3MF), so an off-plate group silently
            slices to nothing.  Already-on-plate groups are untouched.
        plate_depth: Print plate Y dimension in mm (default 256).
        printer_id: Optional supported printer model id; when it
            resolves, its build volume overrides ``plate_width`` /
            ``plate_depth``.  Unresolvable ids keep the defaults —
            placement is a correction, never a reason to refuse.

    Returns:
        Dict with keys:

        * ``success`` (bool)
        * ``output_path`` (str) — path to the created .3mf file
        * ``parts`` (int) — number of color parts
        * ``total_vertices`` (int)
        * ``total_triangles`` (int) — triangles actually emitted
        * ``bed_translation`` (``[tx, ty, tz]``) — only present when the
          whole group had to be moved onto the plate
        * ``degenerate_skipped`` (int) — only present when > 0: input
          triangles dropped because their vertices collapse under exact
          dedup (the 3MF spec forbids repeated indices)
        * ``message`` (str) — human summary
        * ``slicer_note`` (str) — multi-extruder plates only: what to tell
          the user if they open the file in Bambu Studio themselves (it
          re-derives print settings and may need its prime tower moved
          onto the plate). Relay this to the user.
        * ``error`` (str) — only present on failure

    Example::

        result = compose_multicolor_3mf([
            ColorPart("/tmp/coaster_body.stl", extruder=1, name="body",
                      color="#AAAAAA", material="PLA Grey"),
            ColorPart("/tmp/coaster_qr.stl",   extruder=2, name="qr_code",
                      color="#111111", material="PLA Black"),
        ])
        # → result["output_path"] is a ready-to-upload .3mf
    """
    if not parts:
        return {"success": False, "error": "No parts provided — need at least one ColorPart."}

    # -----------------------------------------------------------------------
    # Validate inputs
    # -----------------------------------------------------------------------
    seen_placements: dict[tuple[str, float, float, float], int] = {}
    for i, part in enumerate(parts):
        if not os.path.isfile(part.stl_path):
            return {
                "success": False,
                "error": f"Part {i + 1} STL not found: {part.stl_path}",
            }
        if part.extruder < 1:
            return {
                "success": False,
                "error": (
                    f"Part {i + 1} extruder must be ≥ 1 (got {part.extruder}). "
                    "Extruders are 1-indexed on Bambu AMS."
                ),
            }
        # The same mesh twice at the same position is always a mistake: the
        # copies would print stacked into one footprint (double-extruded).
        # Distinct meshes at one position are legitimate (body + inlay).
        placement = (
            os.path.abspath(part.stl_path),
            round(part.x, 3),
            round(part.y, 3),
            round(part.z, 3),
        )
        if placement in seen_placements:
            return {
                "success": False,
                "error": (
                    f"Parts {seen_placements[placement] + 1} and {i + 1} are the "
                    f"same mesh ({Path(part.stl_path).name}) at the same position "
                    f"({part.x:.1f}, {part.y:.1f}, {part.z:.1f}) — they would print "
                    "stacked on top of each other. Run the parts through "
                    "auto_arrange_parts() or give each copy distinct x/y."
                ),
            }
        seen_placements[placement] = i

    # -----------------------------------------------------------------------
    # Material safety check (always free — full report, hardware warnings,
    # purge matrix, and flush matrix embedding are all free tier).
    #
    # Safety is never paywalled.  The revenue driver is smart plate packing
    # (MaxRects, kiln-pro), not table-lookup compatibility warnings.
    # -----------------------------------------------------------------------
    safety_result: dict[str, Any] | None = None
    flush_matrix_str: str | None = None
    specified_materials = [p.material for p in parts if p.material and p.material.strip()]
    if specified_materials:
        try:
            from kiln.material_safety import (  # lazy import keeps startup lean
                build_bambu_flush_matrix,
                check_material_compatibility,
            )

            safety_result = check_material_compatibility(specified_materials)
            if not safety_result["safe"]:
                return {
                    "success": False,
                    "error": (
                        "⛔ Incompatible materials — do not print. "
                        + safety_result["message"]
                    ),
                    "hardware_warnings": safety_result.get("hardware_warnings", []),
                    "safety": safety_result,
                }

            # Build flush matrix for BambuStudio purge tower (padded to 4 slots)
            flush_matrix_str = build_bambu_flush_matrix(specified_materials, n_slots=4)
            logger.debug("Flush matrix: %s", flush_matrix_str)

        except ImportError:
            logger.debug("kiln.material_safety not available — skipping safety check")

    # -----------------------------------------------------------------------
    # Parse all STL files
    # -----------------------------------------------------------------------
    parsed: list[_ParsedPart] = []
    degenerate_skipped = 0
    for part in parts:
        try:
            vertices, triangles = _parse_mesh_file(part.stl_path)
            if not triangles:
                return {
                    "success": False,
                    "error": f"STL for part '{part.name or part.stl_path}' contains no triangles.",
                }
            # Same guard as compose_painted_3mf: a triangle whose vertices
            # collapse under exact dedup (common in real-world scans) would
            # emit spec-forbidden repeated indices a strict reader may
            # reject.  Skipped at construction so every downstream consumer
            # — model XML, sidecar volume ranges, counts, thumbnail — sees
            # only what is actually emitted.  Counted, never silent.
            kept = [t for t in triangles if len(set(t)) == 3]
            part_degenerate = len(triangles) - len(kept)
            if part_degenerate:
                degenerate_skipped += part_degenerate
                logger.debug(
                    "Skipped %d degenerate triangle(s) in %s",
                    part_degenerate,
                    Path(part.stl_path).name,
                )
            if not kept:
                return {
                    "success": False,
                    "error": (
                        f"STL for part '{part.name or part.stl_path}' "
                        "contains only degenerate triangles."
                    ),
                }
            parsed.append((part, vertices, kept))
            logger.debug(
                "Parsed %s: %d vertices, %d triangles (extruder %d)",
                Path(part.stl_path).name,
                len(vertices),
                len(kept),
                part.extruder,
            )
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to parse STL for part '{part.name or part.stl_path}': {exc}",
            }

    # -----------------------------------------------------------------------
    # Place the GROUP on the plate.  One common translation added to every
    # build item, so relative layout (overlapping color parts, arranged
    # copies) is preserved exactly — see _plate_translation for why an
    # off-plate 3MF silently slices to nothing.
    # -----------------------------------------------------------------------
    plate_w, plate_d = _resolve_plate(plate_width, plate_depth, printer_id)
    g_min_x = g_min_y = g_min_z = float("inf")
    g_max_x = g_max_y = float("-inf")
    for part, vertices, _ in parsed:
        g_min_x = min(g_min_x, min(v[0] for v in vertices) + part.x)
        g_max_x = max(g_max_x, max(v[0] for v in vertices) + part.x)
        g_min_y = min(g_min_y, min(v[1] for v in vertices) + part.y)
        g_max_y = max(g_max_y, max(v[1] for v in vertices) + part.y)
        g_min_z = min(g_min_z, min(v[2] for v in vertices) + part.z)
    bed_tx, bed_ty, bed_tz = _plate_translation(
        g_min_x, g_max_x, g_min_y, g_max_y, g_min_z, plate_w, plate_d,
    )
    if (bed_tx, bed_ty, bed_tz) != (0.0, 0.0, 0.0):
        from dataclasses import replace as _dc_replace

        parsed = [
            (
                _dc_replace(
                    part,
                    x=part.x + bed_tx, y=part.y + bed_ty, z=part.z + bed_tz,
                ),
                vertices,
                triangles,
            )
            for part, vertices, triangles in parsed
        ]

    # -----------------------------------------------------------------------
    # Resolve output path
    # -----------------------------------------------------------------------
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".3mf", prefix="kiln_multicolor_")
        os.close(fd)

    # -----------------------------------------------------------------------
    # Generate plate thumbnail (best-effort, non-blocking)
    # -----------------------------------------------------------------------
    thumbnail_data = _generate_thumbnail(parsed)

    # -----------------------------------------------------------------------
    # Build and write the 3MF ZIP archive
    # -----------------------------------------------------------------------
    try:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml",          _CONTENT_TYPES)
            zf.writestr("_rels/.rels",                  _RELS)
            zf.writestr("3D/3dmodel.model",             _build_model_xml(parsed))
            zf.writestr("Metadata/model_settings.config", _build_model_settings(parsed))
            zf.writestr(
                "Metadata/Slic3r_PE_model.config", _build_prusa_model_config(parsed),
            )
            if flush_matrix_str:
                zf.writestr(
                    "Metadata/project_settings.config",
                    _build_project_settings(flush_matrix_str),
                )
            if thumbnail_data:
                zf.writestr("Metadata/plate_1.png", thumbnail_data)
    except Exception as exc:
        return {"success": False, "error": f"Failed to write 3MF archive: {exc}"}

    total_v = sum(len(v) for _, v, _ in parsed)
    total_t = sum(len(t) for _, _, t in parsed)
    extruder_summary = ", ".join(
        f"extruder {p.extruder} → {p.name or Path(p.stl_path).stem}"
        for p, _, _ in parsed
    )

    logger.info(
        "compose_multicolor_3mf: wrote %s (%d parts, %d triangles)",
        output_path,
        len(parsed),
        total_t,
    )

    result: dict[str, Any] = {
        "success": True,
        "output_path": output_path,
        "parts": len(parsed),
        "total_vertices": total_v,
        "total_triangles": total_t,
        "extruder_map": extruder_summary,
    }
    if (bed_tx, bed_ty, bed_tz) != (0.0, 0.0, 0.0):
        result["bed_translation"] = [
            round(bed_tx, 6), round(bed_ty, 6), round(bed_tz, 6),
        ]
    if degenerate_skipped:
        result["degenerate_skipped"] = degenerate_skipped
    if len({p.extruder for p, _, _ in parsed}) > 1:
        result["slicer_note"] = MULTI_EXTRUDER_SLICER_NOTE

    # Attach full safety report (all free)
    if safety_result is not None:
        result["safety_level"] = safety_result["level"]
        result["safety_message"] = safety_result["message"]
        if safety_result.get("hardware_warnings"):
            result["hardware_warnings"] = safety_result["hardware_warnings"]
        if safety_result.get("pairs"):
            result["material_pairs"] = safety_result["pairs"]
        if flush_matrix_str:
            result["flush_matrix_embedded"] = True

    # Compose human summary
    safety_note = ""
    if safety_result and safety_result["level"] in ("caution", "conditional"):
            safety_note = f" ⚠️  {safety_result['message']}"

    result["message"] = (
        f"Created {len(parsed)}-color 3MF with {total_t:,} triangles. "
        f"Extruder assignments: {extruder_summary}. "
        f"Compatible with BambuStudio (AMS), PrusaSlicer (MMU), and Cura."
        + (" Flush matrix embedded for purge tower sizing." if flush_matrix_str else "")
        + safety_note
        + f" Next step: upload_file('{output_path}') then start_print()."
    )

    return result


# ---------------------------------------------------------------------------
# Painted single-object composer
# ---------------------------------------------------------------------------

#: Highest paint state a single serialized string can carry and still be
#: decoded identically by BOTH slicer families — see painted_state_string.
#: PrusaSlicer master alone could encode up to 255 (an 0b1110-escaped 8-bit
#: extension), but the Bambu/Orca fork's TriangleSelector::deserialize
#: predates that extension and reads only the two-nibble form, and the
#: escape string itself ("EC") would make PrusaSlicer's decoder read past
#: the end of the bitstream.  16 is the shared ceiling.
PAINTED_STATE_MAX = 16


def painted_state_string(state: int) -> str:
    """The slicers' native serialized paint state for a WHOLE triangle.

    All three slicers store per-triangle painting as one hex string per
    ``<triangle>``: PrusaSlicer under ``slic3rpe:mmu_segmentation``, and
    BambuStudio/OrcaSlicer under ``paint_color`` — both consumed by
    ``FacetsAnnotation::set_triangle_from_string``.

    Derivation, from the readers' own sources (not from documentation):

    * ``TriangleSelector::serialize`` (prusa3d/PrusaSlicer,
      ``src/libslic3r/TriangleSelector.cpp``) encodes each triangle as a
      bitstream of nibbles, bits appended LSB-first.  An UNSPLIT triangle
      painted state ``n`` is::

          nibble 0 bits: [split&1, split&2, ...]   split = 0 for unsplit
          n in 1..2:   nibble 0 = n << 2           -> 0x4 / 0x8   (1 nibble)
          n in 3..16:  nibble 0 = 0b1100 = 0xC,
                       nibble 1 = n - 3            -> 2 nibbles

    * ``FacetsAnnotation::get_triangle_as_string`` / ``…from_string``
      (``src/libslic3r/Model.cpp``, both repos, byte-identical) hex-encode
      one char per nibble with the string REVERSED: the first bitstream
      nibble is the LAST character.  So state 3 is ``"0C"``, not ``"C0"``.

    * The Bambu/Orca fork (SoftFever/OrcaSlicer,
      ``src/libslic3r/TriangleSelector.cpp``) asserts ``n <= 16`` in its
      serializer and decodes leaf states only as
      ``(code & 0b1100) == 0b1100 ? next_nibble() + 3 : code >> 2`` — no
      8-bit escape, hence :data:`PAINTED_STATE_MAX`.  Orca's own
      ``CONST_FILAMENTS`` table (``Model.cpp``) pins the identical canon:
      ``{"", "4", "8", "0C", "1C", …, "DC"}`` for filaments 0..16.

    State ``k`` means "painted with filament/extruder ``k``" (1-based);
    unpainted triangles carry NO attribute rather than a state-0 string.

    :raises ValueError: on ``state < 1`` or ``state > PAINTED_STATE_MAX``.
    """
    if not 1 <= state <= PAINTED_STATE_MAX:
        raise ValueError(
            f"paint state must be 1..{PAINTED_STATE_MAX} (got {state}): "
            "state 0 is 'unpainted' (omit the attribute), and both slicer "
            "families agree on the wire format only up to state 16"
        )
    if state <= 2:
        return format(state << 2, "X")
    return format(state - 3, "X") + "C"


def compose_painted_3mf(
    triangles: list[tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]],
    triangle_colors: list[str | None],
    *,
    output_path: str | None = None,
    name: str = "painted",
    plate_width: float = 256.0,
    plate_depth: float = 256.0,
    printer_id: str | None = None,
) -> dict[str, Any]:
    """Compose a 3MF of ONE object whose colors vary per triangle.

    The other shape multicolor takes.  ``compose_multicolor_3mf`` writes one
    solid object per color — right when every color region can stand as a
    closed body (stacked Z-bands, a boss on a plate).  A coloring that
    follows the SURFACE (faces grouped by orientation, random speckle) has
    no such bodies: splitting the shell along it yields zero-thickness
    sheets no slicer accepts (measured: "unable to create convex hull",
    exit 206).  Here the mesh stays whole — one watertight object — and the
    colors ride as core-spec per-triangle references, the painted-model
    form slicers' color-to-filament import flows exist for.  Each colored
    triangle ALSO carries the slicers' native painting state (see
    :func:`painted_state_string`) under both attribute spellings, so
    PrusaSlicer imports the painting as real per-triangle MMU segmentation
    (measured: tool changes and both filaments in the gcode, where the
    colorgroup alone produced neither) and the Bambu family opens the file
    as a painted model.

    :param triangles: The full mesh, one ``(v0, v1, v2)`` tuple per
        triangle, each vertex an ``(x, y, z)``.  Vertices are deduplicated
        by exact coordinates, so a watertight input stays watertight.
    :param triangle_colors: One ``#RRGGBB`` hint per triangle (``None`` =
        uncolored; such faces carry no reference and render neutral).
    :param output_path: Where to write.  Defaults to a temp file.
    :param name: The object name shown in slicers.
    :param plate_width: Print plate X dimension in mm (default 256, same
        legacy default as :func:`auto_arrange_parts`).  Used to place the
        object ON the plate: a 3MF carries an explicit build transform
        that slicers honour literally — PrusaSlicer does NOT auto-centre
        it the way it auto-centres a loose STL, and a mesh with negative
        coordinates is silently "outside of the print volume" (exit 0,
        no gcode).  When the mesh bbox already sits inside the plate the
        transform stays identity, preserving deliberate placement.
    :param plate_depth: Print plate Y dimension in mm (default 256).
    :param printer_id: Optional supported printer model id.  When it
        resolves, its build volume overrides ``plate_width`` /
        ``plate_depth``; when it does not, the defaults stand — bed
        placement is a correction, never a reason to refuse a paint.
    :returns: Dict with ``output_path``, ``colors`` (distinct palette
        actually referenced), and counts.  ``bed_translation`` (``[tx,
        ty, tz]``) appears when the build transform had to move the
        object onto the plate.  ``native_paint_truncated`` (int)
        appears only when the palette exceeds :data:`PAINTED_STATE_MAX`
        colors: triangles past the limit keep their spec colorgroup
        reference but carry no native paint state.  ``{"success": False,
        ...}`` on empty input or write failure — never raises.
    """
    if not triangles:
        return {"success": False, "error": "No triangles to compose"}
    if len(triangle_colors) != len(triangles):
        return {
            "success": False,
            "error": (
                f"triangle_colors length {len(triangle_colors)} != "
                f"triangle count {len(triangles)}"
            ),
        }

    # Exact-coordinate vertex dedup — the same discipline _parse_stl uses,
    # so the emitted object is as watertight as the input mesh.  A triangle
    # whose vertices collapse to fewer than three is dropped WITH its color
    # (the spec forbids repeated indices, and a slicer meeting one may
    # reject the whole file) — counted, never silent.
    vert_index: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    kept_colors: list[str | None] = []
    degenerate_skipped = 0
    for tri, color in zip(triangles, triangle_colors, strict=True):
        idx = []
        for v in tri:
            key = (float(v[0]), float(v[1]), float(v[2]))
            i = vert_index.get(key)
            if i is None:
                i = len(vertices)
                vert_index[key] = i
                vertices.append(key)
            idx.append(i)
        if len(set(idx)) < 3:
            degenerate_skipped += 1
            continue
        faces.append((idx[0], idx[1], idx[2]))
        kept_colors.append(color)
    if not faces:
        return {"success": False, "error": "Every triangle was degenerate"}

    palette: list[str] = []
    tri_pindex: list[int | None] = []
    for color in kept_colors:
        rgb_hex = _part_rgb_hex(color)
        if rgb_hex is None:
            tri_pindex.append(None)
            continue
        if rgb_hex not in palette:
            palette.append(rgb_hex)
        tri_pindex.append(palette.index(rgb_hex))

    # Place the object ON the plate.  A single translation of the whole
    # object preserves every relative layout inside it (a jar and its
    # lid stay a jar-width apart) — see _plate_translation for why the
    # transform must not stay identity for an off-plate mesh.
    plate_width, plate_depth = _resolve_plate(plate_width, plate_depth, printer_id)
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    tx, ty, tz = _plate_translation(
        min(xs), max(xs), min(ys), max(ys), min(zs), plate_width, plate_depth,
    )

    colorgroup_id = 2  # the single object is id 1
    obj_name = _xml_escape(name)
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<model unit="millimeter" xml:lang="en-US"',
        '  xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"',
        '  xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02"',
        '  xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06"',
        '  xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">',
        '  <metadata name="Application">Kiln</metadata>',
        '  <metadata name="BambuStudio:3mfVersion">1</metadata>',
        "  <resources>",
    ]
    if palette:
        lines.append(f'    <m:colorgroup id="{colorgroup_id}">')
        lines += [f'      <m:color color="{c}"/>' for c in palette]
        lines.append("    </m:colorgroup>")
    lines += [
        f'    <object id="1" type="model" name="{obj_name}">',
        "      <mesh>",
        "        <vertices>",
    ]
    for x, y, z in vertices:
        lines.append(f'          <vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>')
    lines += [
        "        </vertices>",
        "        <triangles>",
    ]
    # Each colored triangle carries THREE channels: the core-spec
    # colorgroup reference (generic readers + kiln.threemf_parser), and the
    # slicers' native painting state under both spellings —
    # slic3rpe:mmu_segmentation (PrusaSlicer; its reader has no colorgroup
    # handling at all) and paint_color (BambuStudio/OrcaSlicer).  Same
    # serialized value, one per spelling; palette index i maps to paint
    # state i+1.  A palette past PAINTED_STATE_MAX keeps its colorgroup
    # reference but gets no native state (a wrong state would repaint the
    # triangle with someone else's filament) — counted, never silent.
    native_paint_truncated = 0
    for (a, b, c), pindex in zip(faces, tri_pindex, strict=True):
        ref = ""
        if pindex is not None:
            ref = f' pid="{colorgroup_id}" p1="{pindex}"'
            if pindex < PAINTED_STATE_MAX:
                state = painted_state_string(pindex + 1)
                ref += (
                    f' slic3rpe:mmu_segmentation="{state}"'
                    f' paint_color="{state}"'
                )
            else:
                native_paint_truncated += 1
        lines.append(f'          <triangle v1="{a}" v2="{b}" v3="{c}"{ref}/>')
    lines += [
        "        </triangles>",
        "      </mesh>",
        "    </object>",
        "  </resources>",
        "  <build>",
        f'    <item objectid="1" transform="1 0 0 0 1 0 0 0 1 '
        f'{tx:.6f} {ty:.6f} {tz:.6f}"/>',
        "  </build>",
        "</model>",
    ]
    model_xml = "\n".join(lines)

    settings = "\n".join([
        '<?xml version="1.0" encoding="utf-8"?>',
        "<config>",
        '  <object id="1">',
        f'    <metadata key="name"     value="{obj_name}"/>',
        "  </object>",
        "</config>",
    ])
    prusa_config = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<config>",
        ' <object id="1" instances_count="1">',
        f'  <volume firstid="0" lastid="{len(faces) - 1}">',
        f'   <metadata type="volume" key="name" value="{obj_name}"/>',
        "  </volume>",
        f'  <metadata type="object" key="name" value="{obj_name}"/>',
        " </object>",
        "</config>",
    ])

    thumbnail = None
    if len(faces) <= _COLORED_THUMBNAIL_MAX_TRIANGLES:
        try:
            from kiln.threemf_parser import _DEFAULT_COLOR, ColoredTriangle

            colored = [
                ColoredTriangle(
                    v0=tri[0], v1=tri[1], v2=tri[2],
                    color=(
                        _part_rgb(triangle_colors[i]) or _DEFAULT_COLOR
                    ),
                )
                for i, tri in enumerate(triangles)
            ]
            from kiln.colored_renderer import render_colored_mesh

            result = render_colored_mesh(
                colored, width=_THUMBNAIL_SIZE, height=_THUMBNAIL_SIZE,
            )
            with open(result.path, "rb") as fh:
                thumbnail = fh.read()
            with contextlib.suppress(OSError):
                os.remove(result.path)
        except Exception:  # noqa: BLE001 — enrichment, never a compose failure
            logger.debug("painted thumbnail failed", exc_info=True)

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".3mf", prefix="kiln_painted_")
        os.close(fd)
    try:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
            zf.writestr("_rels/.rels", _RELS)
            zf.writestr("3D/3dmodel.model", model_xml)
            zf.writestr("Metadata/model_settings.config", settings)
            zf.writestr("Metadata/Slic3r_PE_model.config", prusa_config)
            if thumbnail:
                zf.writestr("Metadata/plate_1.png", thumbnail)
    except Exception as exc:  # noqa: BLE001 — mirror compose_multicolor_3mf
        return {"success": False, "error": f"Failed to write 3MF archive: {exc}"}

    logger.info(
        "compose_painted_3mf: wrote %s (1 object, %d triangles, %d colors)",
        output_path, len(faces), len(palette),
    )
    result: dict[str, Any] = {
        "success": True,
        "output_path": output_path,
        "form": "painted_single_object",
        "colors": palette,
        "total_vertices": len(vertices),
        "total_triangles": len(faces),
        "message": (
            f"Created painted multicolor 3MF: one watertight object, "
            f"{len(faces):,} triangles across {len(palette)} colors.  "
            "Slicers that support color import (BambuStudio, OrcaSlicer) "
            "will offer to map each color to a filament on open."
            if palette
            else (
                f"Created single-object 3MF with {len(faces):,} triangles "
                "and no color hints."
            )
        ),
    }
    if (tx, ty, tz) != (0.0, 0.0, 0.0):
        result["bed_translation"] = [round(tx, 6), round(ty, 6), round(tz, 6)]
    if degenerate_skipped:
        result["degenerate_skipped"] = degenerate_skipped
    if native_paint_truncated:
        result["native_paint_truncated"] = native_paint_truncated
        result["message"] += (
            f"  {native_paint_truncated:,} triangle(s) use colors beyond "
            f"the {PAINTED_STATE_MAX}-filament native painting limit; they "
            "keep their spec colors but slicers will not auto-paint them."
        )
    return result


# ---------------------------------------------------------------------------
# Multicolor detection
#
# Lives here — with the writers whose output it recognises — so both the
# slicing engine (kiln.slicer) and the tool surface (kiln.plugins.
# slicer_tools) can ask "does this 3MF want more than one filament?"
# without either importing from the other.  It used to be private to the
# tool layer, which meant the engine could WARN about flattened colors
# but never act on them.
# ---------------------------------------------------------------------------

#: Slicer-native painted-model attributes: BambuStudio/OrcaSlicer write
#: ``paint_color`` and PrusaSlicer writes ``slic3rpe:mmu_segmentation``
#: on <triangle> elements.
_PAINT_ATTRIBUTE_MARKERS = (b"paint_color", b"mmu_segmentation")

#: Per-object extruder assignment in the slicer sidecars
#: (Metadata/model_settings.config for BambuStudio,
#: Metadata/Slic3r_PE_model.config for the PrusaSlicer family).
_SIDECAR_EXTRUDER_RE = re.compile(rb'key="extruder"\s+value="(\d+)"')

#: Per-build-item extruder attribute in the model XML (PrusaSlicer also
#: reads/writes this form).
_ITEM_EXTRUDER_RE = re.compile(rb'slic3rpe:extruder="(\d+)"')

#: One palette entry inside <m:colorgroup>.  The trailing whitespace class
#: keeps ``<m:colorgroup`` itself from matching.  Deliberately loose: this
#: COUNTS entries, and a palette entry spelled in a way the value pattern
#: below cannot read is still a palette entry — narrowing the count would
#: silently un-detect a multicolor file (measured: a bare ``FF0000`` or a
#: single-quoted value drops from 2 to 0).
_COLORGROUP_COLOR_RE = re.compile(rb"<m:color\s")

#: The same entries with their hex VALUE captured, for putting real colors
#: on real filament slots.  Strictly a bonus channel over the count above:
#: display metadata only, so an unreadable spelling costs a nice-to-have
#: rather than the detection itself.
_COLORGROUP_VALUE_RE = re.compile(
    rb"""<m:color\s[^>]*?color=["'](\#?[0-9A-Fa-f]{6,8})["']"""
)

_SIDECAR_CONFIG_NAMES = frozenset({
    "metadata/model_settings.config",
    "metadata/slic3r_pe_model.config",
})

#: Most filament slots a detected file is reported as needing.  Sixteen for
#: two independent reasons that happen to agree: it is the painting wire
#: format's ceiling (:data:`PAINTED_STATE_MAX`), and it is as many filament
#: presets as any slicer Kiln drives will accept.  Named separately because
#: a multi-OBJECT file's extruder numbers are not paint states — they share
#: a limit, not a meaning.
_MAX_FILAMENT_SLOTS = PAINTED_STATE_MAX


def detect_3mf_multicolor(input_path: str) -> dict[str, Any] | None:
    """Cheap structural scan: does this 3MF ask for more than one filament?

    Detects both multicolor forms without a full model parse (mirrors the
    byte-scan idiom in ``threemf_parser.object_display_colors``):

    * **multi-object** — two-plus DISTINCT per-object extruder values in
      the slicer sidecars or on ``slic3rpe:extruder`` build items;
    * **painted single object** — two-plus distinct FILAMENTS referenced
      by the painting channel (``paint_color`` / ``mmu_segmentation``
      values decoded to their leaf states: a painting that references one
      filament everywhere loses nothing when flattened, and state 0 — the
      unpainted portion of a partially painted triangle — is the object's
      base filament, so it counts) or a two-plus-color ``<m:colorgroup>``
      palette in the model XML.  Distinct attribute STRINGS are not the
      metric: sub-triangle painting multiplies split shapes without
      touching a new filament, which made every correctly sliced painted
      3MF warn that colors were lost, including on a profile that could
      print them.

    Returns an evidence dict when multicolor, else ``None``.  Evidence
    keys (each present only when its channel produced evidence):

    * ``extruders`` — sorted distinct per-object extruder values;
    * ``paint_attribute`` / ``paint_filaments`` — which painting channel
      fired, and how many distinct filaments it references;
    * ``palette_colors`` — ``<m:colorgroup>`` entry count;
    * ``palette`` — the palette's hex values in model-XML order.  For
      files this module writes, palette index ``i`` is filament ``i+1``
      (:func:`compose_painted_3mf` maps palette index i to paint state
      i+1), so this is the color-per-slot list a multi-filament slice
      wants;
    * ``filament_slots_needed`` — how many filament SLOTS a slicer must
      offer so no reference dangles: the max over the highest extruder
      value, the highest paint state (state k is "filament k"), and the
      palette length, capped at :data:`PAINTED_STATE_MAX` (the shared
      painting ceiling).  A max, not a count: paint states {1, 3} need
      three slots even though only two are used.

    NEVER raises: this feeds an advisory and a best-effort preset
    expansion, so a corrupt archive, odd layout, or any other trouble
    reads as "not multicolor" and slicing proceeds untouched.
    """
    try:
        from kiln.threemf_parser import _decode_paint_states

        extruders: set[int] = set()
        paint_attribute: str | None = None
        paint_states: set[int] = set()
        palette: list[str] = []
        palette_colors = 0
        with zipfile.ZipFile(input_path) as zf:
            for member in zf.namelist():
                low = member.lower()
                if low in _SIDECAR_CONFIG_NAMES:
                    raw = zf.read(member)
                    extruders.update(
                        int(m) for m in _SIDECAR_EXTRUDER_RE.findall(raw)
                    )
                elif low.endswith(".model"):
                    raw = zf.read(member)
                    extruders.update(
                        int(m) for m in _ITEM_EXTRUDER_RE.findall(raw)
                    )
                    for marker in _PAINT_ATTRIBUTE_MARKERS:
                        values = set(
                            re.findall(marker + b'="([^"]*)"', raw)
                        )
                        values.discard(b"")
                        if not values:
                            continue
                        for value in values:
                            decoded = _decode_paint_states(
                                value.decode("ascii", errors="replace")
                            )
                            if decoded is None:
                                continue  # malformed string = no evidence
                            paint_states.update(decoded[0])
                        if paint_states:
                            paint_attribute = marker.decode()
                        break
                    # Both taken as a MAX across model members: a 3MF may
                    # carry several, and the richest palette is the one
                    # the file is asking for.
                    palette_colors = max(
                        palette_colors, len(_COLORGROUP_COLOR_RE.findall(raw)),
                    )
                    palette_values = [
                        "#" + m.decode("ascii").lstrip("#")[:6].upper()
                        for m in _COLORGROUP_VALUE_RE.findall(raw)
                    ]
                    if len(palette_values) > len(palette):
                        palette = palette_values

        # Distinct FILAMENTS the painting references: state k ≥ 1 is
        # filament k, and state 0 (unpainted) is the object's base
        # filament, distinct from every painted one.
        paint_filaments = len(paint_states)

        evidence: dict[str, Any] = {}
        if len(extruders) >= 2:
            evidence["extruders"] = sorted(extruders)
        if paint_attribute is not None and paint_filaments >= 2:
            evidence["paint_attribute"] = paint_attribute
            evidence["paint_filaments"] = paint_filaments
        if palette_colors >= 2:
            evidence["palette_colors"] = palette_colors
        if not evidence:
            return None

        if palette:
            evidence["palette"] = palette
        slots = max(
            # Extruder numbers are 1-based slot NAMES, so the highest one
            # is the slot count — parts on extruders 1 and 3 need three.
            max(extruders, default=0),
            # Painted state k is "filament k", so likewise; state 0 means
            # unpainted, which prints from the base filament in slot 1.
            max(paint_states, default=0),
            1 if 0 in paint_states else 0,
            palette_colors,
        )
        evidence["filament_slots_needed"] = min(slots, _MAX_FILAMENT_SLOTS)
        return evidence
    except Exception:  # noqa: BLE001 — advisory only, never break slicing
        return None


def multicolor_filament_colors(
    evidence: dict[str, Any], slots: int,
) -> list[str]:
    """One display hex per filament slot, from *evidence*'s palette.

    Slot ``i`` (0-based) takes palette entry ``i`` — the mapping
    :func:`compose_painted_3mf` writes (palette index i ↔ paint state
    i+1 ↔ filament i+1).  Slots past the palette fall back to the
    deterministic painted-state palette ``kiln.threemf_parser`` renders
    with, so every slot stays visually distinct.  Display metadata only:
    a wrong hex never changes a toolpath.
    """
    from kiln.threemf_parser import _PAINT_STATE_PALETTE

    palette = [c for c in (evidence.get("palette") or []) if c]
    colors: list[str] = []
    for i in range(max(slots, 0)):
        if i < len(palette):
            colors.append(palette[i])
            continue
        r, g, b = _PAINT_STATE_PALETTE[i % len(_PAINT_STATE_PALETTE)]
        colors.append(f"#{r:02X}{g:02X}{b:02X}")
    return colors
