"""Multi-color / multi-material 3MF composer.

Creates a single .3mf file from multiple STL inputs, with per-part
extruder/filament assignments AND per-part plate positions. Compatible with
BambuStudio (Bambu A1/X1/P1 + AMS), PrusaSlicer (MMU / ERCF), Cura, and
any 3MF-capable slicer.

The .3mf format is a ZIP archive. Each part becomes a separate ``<object>``
in ``3D/3dmodel.model``. Extruder assignments live in two places for maximum
slicer compatibility:

* ``Metadata/model_settings.config`` — BambuStudio reads ``extruder`` here.
* ``slic3rpe:extruder`` attribute on each ``<item>`` — PrusaSlicer reads this.

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
import struct
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thumbnail generation
# ---------------------------------------------------------------------------

_THUMBNAIL_SIZE = 512


def _generate_thumbnail(stl_paths: list[str]) -> bytes | None:
    """Render a plate thumbnail PNG from STL files via OpenSCAD.

    Imports all STL parts into a single scene so the thumbnail shows
    the complete model as it will be printed.  Uses preview mode (not
    full render) so non-manifold meshes work, and applies a neutral
    grey color with the DeepOcean colorscheme for high contrast on
    printer LCDs.

    Returns PNG bytes suitable for embedding as ``Metadata/plate_1.png``
    in a 3MF archive, or ``None`` if OpenSCAD is unavailable.
    """
    if not stl_paths:
        return None
    try:
        import subprocess

        from kiln.generation.openscad import OpenSCADProvider

        provider = OpenSCADProvider()
        binary = provider._binary
        if not binary:
            return None

        # Build a SCAD file that imports all parts with a neutral colour
        # so the model is visible against any colorscheme background.
        imports = "\n".join(
            f'  import("{Path(p).resolve()}");'
            for p in stl_paths
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
            cmd = [
                binary,
                "-o", png_path,
                f"--imgsize={_THUMBNAIL_SIZE},{_THUMBNAIL_SIZE}",
                "--autocenter",
                "--viewall",
                "--colorscheme", "DeepOcean",
                scad_path,
            ]
            subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
            if os.path.isfile(png_path) and os.path.getsize(png_path) > 0:
                return Path(png_path).read_bytes()
            return None
        finally:
            for p in (scad_path, png_path):
                with contextlib.suppress(OSError):
                    os.unlink(p)
    except Exception:
        logger.debug("Thumbnail generation skipped (OpenSCAD unavailable or render failed)")
        return None

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


def _build_model_xml(parsed: list[_ParsedPart]) -> str:
    """Build ``3D/3dmodel.model`` XML containing all mesh objects."""
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<model unit="millimeter" xml:lang="en-US"',
        '  xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"',
        '  xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06"',
        '  xmlns:bambu="http://bambulab.com/model/2021"',
        '  xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">',
        '  <metadata name="Application">Kiln</metadata>',
        "  <resources>",
    ]

    for obj_id, (part, vertices, triangles) in enumerate(parsed, start=1):
        name = _xml_escape(part.name or f"part_{obj_id}")
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
            lines.append(f'          <triangle v1="{v1}" v2="{v2}" v3="{v3}"/>')
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
    for obj_id, (part, _, _) in enumerate(parsed, start=1):
        name = _xml_escape(part.name or f"part_{obj_id}")
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
# Bounding box helper
# ---------------------------------------------------------------------------


def _stl_bounding_box(stl_path: str) -> tuple[float, float, float, float, float, float]:
    """Return (min_x, min_y, min_z, max_x, max_y, max_z) for an STL file."""
    vertices, _ = _parse_stl(stl_path)
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

    For smart 2D bin-packing that maximises plate density, upgrade to
    kiln-pro (``from kiln_pro.plate_optimizer import smart_arrange``).

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
        ], plate_width=256, plate_depth=256, gap_mm=5)
        result = compose_multicolor_3mf(parts)
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
        plate_width, plate_depth = build_volume[0], build_volume[1]

    # Assign default groups (each spec is its own group if not specified).
    # Track group per spec in a parallel list so the result-build pass never
    # calls list.index() — which would give wrong results for identical dicts.
    groups: dict[int, list[dict[str, Any]]] = {}
    spec_groups: list[int] = []          # parallel to part_specs
    for i, spec in enumerate(part_specs):
        g = int(spec.get("group", i))
        groups.setdefault(g, []).append(spec)
        spec_groups.append(g)

    # For each group, determine the bounding box by taking the union of all parts
    group_order = sorted(groups.keys())
    group_bboxes: dict[int, tuple[float, float]] = {}  # group → (width, depth)
    for g in group_order:
        max_w, max_d = 0.0, 0.0
        for spec in groups[g]:
            try:
                mn_x, mn_y, _, mx_x, mx_y, _ = _stl_bounding_box(spec["stl_path"])
                max_w = max(max_w, mx_x - mn_x)
                max_d = max(max_d, mx_y - mn_y)
            except Exception:
                max_w = max(max_w, 50.0)   # fallback if STL unreadable
                max_d = max(max_d, 50.0)
        group_bboxes[g] = (max_w, max_d)

    # Simple row layout: place groups left-to-right, wrap to next row when
    # the plate width would be exceeded.
    group_positions: dict[int, tuple[float, float]] = {}
    cursor_x, cursor_y, row_depth = 0.0, 0.0, 0.0
    for g in group_order:
        w, d = group_bboxes[g]
        if cursor_x > 0 and cursor_x + w > plate_width:
            # Wrap to next row
            cursor_x = 0.0
            cursor_y += row_depth + gap_mm
            row_depth = 0.0
        group_positions[g] = (cursor_x, cursor_y)
        cursor_x += w + gap_mm
        row_depth = max(row_depth, d)

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
) -> dict[str, Any]:
    """Compose a multi-color / multi-material .3mf from multiple STL files.

    Creates a single print-ready .3mf containing all parts with per-part
    AMS/extruder assignments.  All parts must share the same coordinate
    origin — they are overlaid in the slicer exactly as positioned in the
    STL files.

    Compatible slicers:
        * **BambuStudio** — reads ``Metadata/model_settings.config``
        * **PrusaSlicer** — reads ``slic3rpe:extruder`` on ``<item>``
        * **Cura** — reads standard 3MF objects (manual extruder assignment)
        * Any slicer that supports 3MF Core + multiple objects

    Args:
        parts: List of :class:`ColorPart`.  Each part needs an STL path and
            extruder number.  Extruder numbers map directly to Bambu AMS
            trays (1-indexed).  Parts are placed in the same world space as
            their source STLs — no transforms applied.
        output_path: Where to write the .3mf.  Defaults to a system temp
            file (path returned in the result dict).

    Returns:
        Dict with keys:

        * ``success`` (bool)
        * ``output_path`` (str) — path to the created .3mf file
        * ``parts`` (int) — number of color parts
        * ``total_vertices`` (int)
        * ``total_triangles`` (int)
        * ``message`` (str) — human summary
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
    for part in parts:
        try:
            vertices, triangles = _parse_stl(part.stl_path)
            if not triangles:
                return {
                    "success": False,
                    "error": f"STL for part '{part.name or part.stl_path}' contains no triangles.",
                }
            parsed.append((part, vertices, triangles))
            logger.debug(
                "Parsed %s: %d vertices, %d triangles (extruder %d)",
                Path(part.stl_path).name,
                len(vertices),
                len(triangles),
                part.extruder,
            )
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to parse STL for part '{part.name or part.stl_path}': {exc}",
            }

    # -----------------------------------------------------------------------
    # Resolve output path
    # -----------------------------------------------------------------------
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".3mf", prefix="kiln_multicolor_")
        os.close(fd)

    # -----------------------------------------------------------------------
    # Generate plate thumbnail (best-effort, non-blocking)
    # -----------------------------------------------------------------------
    thumbnail_data = _generate_thumbnail([p.stl_path for p in parts])

    # -----------------------------------------------------------------------
    # Build and write the 3MF ZIP archive
    # -----------------------------------------------------------------------
    try:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml",          _CONTENT_TYPES)
            zf.writestr("_rels/.rels",                  _RELS)
            zf.writestr("3D/3dmodel.model",             _build_model_xml(parsed))
            zf.writestr("Metadata/model_settings.config", _build_model_settings(parsed))
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
