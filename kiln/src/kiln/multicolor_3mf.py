"""Multi-color / multi-material 3MF composer."""

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
from kiln.untrusted_zip import UntrustedZipFile

logger = logging.getLogger(__name__)

MULTI_EXTRUDER_SLICER_NOTE = (
    "This only matters if you hand-load the 3MF into Bambu Studio: it "
    "imports third-party files as geometry and color only (its stated "
    "policy), and its default prime-tower spot can sit off the plate — "
    "not a defect in this file. Drag the prime tower onto the plate "
    "there and it slices clean with all colors intact."
)

JOINED_PARTS_PRUSA_NOTE = (
    "Opened by hand in PrusaSlicer, the touching parts arrive as separate "
    "objects; merge them into one object there before slicing. Kiln's own "
    "slice keeps them one object."
)

# ---------------------------------------------------------------------------
# Thumbnail generation
# ---------------------------------------------------------------------------

_THUMBNAIL_SIZE = 512

_COLORED_THUMBNAIL_MAX_TRIANGLES = 200_000


def _part_rgb_hex(color: str | None) -> str | None:
    """Normalize a part's color hint to ``#RRGGBB``, or ``None``."""
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
    """Render the ``Metadata/plate_1.png`` thumbnail for the composed 3MF."""
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
    """Render a plate thumbnail PNG from STL files via OpenSCAD."""
    if not stl_paths:
        return None
    try:
        from kiln.generation.openscad import OpenSCADProvider
        from kiln.openscad_runner import run_openscad

        provider = OpenSCADProvider()
        binary = provider._binary
        if not binary:
            return None

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

            # Export in preview mode (no --render)
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
    """Map a file's DECLARED filament colors onto its parts, or refuse to."""
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
    """Render a plate thumbnail from bare mesh PATHS, in its declared colors."""
    part_colors = _colors_for_parts(stl_paths, colors)
    parsed: list[_ParsedPart] = []
    for index, path in enumerate(stl_paths):
        try:
            vertices, triangles = _parse_mesh_file(path)
        except Exception:  # noqa: BLE001 — one bad part must not blank the plate
            logger.warning("Could not parse %s for thumbnail", path, exc_info=True)
            continue
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
    """The canonical picture of a plate, for embedding in an emitted file."""
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
    """One part (color/material/position) in a multi-color or multi-item print."""

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
    """Parse binary or ASCII STL → (vertices, triangles) with deduplication."""
    data = Path(stl_path).read_bytes()

    # Heuristic: binary STLs rarely start with "solid" and contain valid UTF-8
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
        base = 84 + i * 50 + 12
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
    """Parse an STL, OBJ, or GLB mesh → deduplicated (vertices, triangles)."""
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
    """The object names, XML-escaped, in part order."""
    from kiln.threemf_parser import unique_object_names

    return [
        _xml_escape(name)
        for name in unique_object_names([part.name for part, _, _ in parsed])
    ]


# ---------------------------------------------------------------------------
# Joining touching parts into one object
# ---------------------------------------------------------------------------

_JOIN_GAP_MM = 0.5


def _placed_bounds(
    part: ColorPart, vertices: list[tuple[float, float, float]],
) -> tuple[float, float, float, float, float, float]:
    """``(min_x, min_y, min_z, max_x, max_y, max_z)`` of a part where it is placed: its mesh bounds moved by its ``x/y/z``."""
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    return (
        min(xs) + part.x, min(ys) + part.y, min(zs) + part.z,
        max(xs) + part.x, max(ys) + part.y, max(zs) + part.z,
    )


def _enclosed_volume(
    vertices: list[tuple[float, float, float]],
    triangles: list[tuple[int, int, int]],
) -> float:
    """The volume a closed mesh encloses (divergence theorem), in mm³."""
    total = 0.0
    for a, b, c in triangles:
        x1, y1, z1 = vertices[a]
        x2, y2, z2 = vertices[b]
        x3, y3, z3 = vertices[c]
        total += (
            x1 * (y2 * z3 - y3 * z2)
            - x2 * (y1 * z3 - y3 * z1)
            + x3 * (y1 * z2 - y2 * z1)
        )
    return abs(total) / 6.0


def _touching_groups(parsed: list[_ParsedPart]) -> list[list[int]]:
    """Indices into *parsed*, one list per build object."""
    bounds = [_placed_bounds(part, vertices) for part, vertices, _ in parsed]
    parent = list(range(len(parsed)))

    def root_of(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Sweep along X: only parts whose X spans meet can touch.
    by_min_x = sorted(range(len(parsed)), key=lambda i: bounds[i][0])
    active: list[int] = []
    for i in by_min_x:
        bi = bounds[i]
        active = [j for j in active if bounds[j][3] + _JOIN_GAP_MM >= bi[0]]
        for j in active:
            bj = bounds[j]
            if all(
                bi[axis] <= bj[axis + 3] + _JOIN_GAP_MM
                and bj[axis] <= bi[axis + 3] + _JOIN_GAP_MM
                for axis in (1, 2)
            ):
                parent[root_of(i)] = root_of(j)
        active.append(i)

    members: dict[int, list[int]] = {}
    for i in range(len(parsed)):
        members.setdefault(root_of(i), []).append(i)
    groups = sorted(members.values(), key=lambda g: g[0])
    joined = [i for g in groups if len(g) > 1 for i in g]
    volume = {i: _enclosed_volume(parsed[i][1], parsed[i][2]) for i in joined}
    return [
        g if len(g) == 1 else sorted(g, key=lambda i: (-volume[i], i))
        for g in groups
    ]


def _part_palette(parsed: list[_ParsedPart]) -> list[str]:
    """The distinct part colors, ``#RRGGBB``, in part order."""
    palette: list[str] = []
    for part, _, _ in parsed:
        rgb_hex = _part_rgb_hex(part.color)
        if rgb_hex is not None and rgb_hex not in palette:
            palette.append(rgb_hex)
    return palette


def _translation(x: float, y: float, z: float) -> str:
    """A 3MF transform (4x3, column-major) that only translates."""
    return f"1 0 0 0 1 0 0 0 1 {x:.6f} {y:.6f} {z:.6f}"


def _build_model_xml(parsed: list[_ParsedPart], groups: list[list[int]]) -> str:
    """Build ``3D/3dmodel.model``: a mesh object per part, plus one object per joined group whose ``<components>`` place its parts in :func:`_touching_groups` order."""
    palette = _part_palette(parsed)
    part_pindex: dict[int, int] = {}
    for obj_id, (part, _, _) in enumerate(parsed, start=1):
        rgb_hex = _part_rgb_hex(part.color)
        if rgb_hex is not None:
            part_pindex[obj_id] = palette.index(rgb_hex)
    joined = [g for g in groups if len(g) > 1]
    colorgroup_id = len(parsed) + len(joined) + 1

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<model unit="millimeter" xml:lang="en-US"',
        '  xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"',
        '  xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02"',
        '  xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06"',
        '  xmlns:bambu="http://bambulab.com/model/2021"',
        '  xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">',
        '  <metadata name="Application">Kiln</metadata>',
        # The Bambu-family project-version stamp
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

    # Joined objects follow the meshes they reference
    for asm_id, group in enumerate(joined, start=len(parsed) + 1):
        lines.append(
            f'    <object id="{asm_id}" type="model" name="{names[group[0]]}">'
        )
        lines.append("      <components>")
        for i in group:
            part = parsed[i][0]
            lines.append(
                f'        <component objectid="{i + 1}"'
                f' transform="{_translation(part.x, part.y, part.z)}"/>'
            )
        lines += ["      </components>", "    </object>"]

    lines.append("  </resources>")
    lines.append("  <build>")
    asm_ids = iter(range(len(parsed) + 1, len(parsed) + len(joined) + 1))
    for group in groups:
        first = parsed[group[0]][0]
        if len(group) == 1:
            obj_id = group[0] + 1
            transform = _translation(first.x, first.y, first.z)
        else:
            obj_id = next(asm_ids)
            transform = _translation(0.0, 0.0, 0.0)
        lines.append(
            f'    <item objectid="{obj_id}"'
            f' transform="{transform}"'
            f' slic3rpe:extruder="{first.extruder}"/>'
        )
    lines += ["  </build>", "</model>"]

    return "\n".join(lines)


def _build_prusa_model_config(parsed: list[_ParsedPart]) -> str:
    """Build ``Metadata/Slic3r_PE_model.config`` — the PrusaSlicer channel."""
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


def _build_model_settings(parsed: list[_ParsedPart], groups: list[list[int]]) -> str:
    """Build ``Metadata/model_settings.config`` for BambuStudio / OrcaSlicer."""
    lines: list[str] = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<config>",
    ]
    names = _display_names(parsed)
    asm_ids = iter(range(len(parsed) + 1, len(parsed) + len(groups) + 1))
    for group in groups:
        if len(group) > 1:
            first = parsed[group[0]][0]
            lines.append(f'  <object id="{next(asm_ids)}">')
            lines.append(f'    <metadata key="name"     value="{names[group[0]]}"/>')
            lines.append(f'    <metadata key="extruder" value="{first.extruder}"/>')
            for i in group:
                part = parsed[i][0]
                lines.append(f'    <part id="{i + 1}" subtype="normal_part">')
                lines.append(f'      <metadata key="name"     value="{names[i]}"/>')
                lines.append(f'      <metadata key="extruder" value="{part.extruder}"/>')
                lines.append("    </part>")
            lines.append("  </object>")
            continue
        obj_id = group[0] + 1
        part = parsed[group[0]][0]
        lines.append(f'  <object id="{obj_id}">')
        lines.append(f'    <metadata key="name"     value="{names[group[0]]}"/>')
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
# Project settings (flush matrix)
# ---------------------------------------------------------------------------


def _build_project_settings(flush_matrix_str: str) -> str:
    """Build ``Metadata/project_settings.config`` — the purge volumes."""
    import json

    values = [str(int(float(v))) for v in flush_matrix_str.split()]
    return json.dumps(
        {
            "flush_volumes_matrix": values,
            "flush_multiplier": ["1"],
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Bed placement (shared by both composers)
# ---------------------------------------------------------------------------

_BED_EPSILON_MM = 0.5


def _resolve_plate(
    plate_width: float, plate_depth: float, printer_id: str | None,
) -> tuple[float, float]:
    """Plate XY dimensions, from the printer when it resolves."""
    width, depth, _height, _words = plate_bed(plate_width, plate_depth, printer_id)
    return width, depth


def plate_bed(
    plate_width: float, plate_depth: float, printer_id: str | None,
) -> tuple[float, float, float | None, str]:
    """``(width, depth, height, words)`` of the bed a plate is laid out for."""
    if printer_id:
        try:
            from kiln.printers.bed_fit import get_printer_display_name, printer_bed

            bed = printer_bed(printer_id)
        except Exception:  # noqa: BLE001 — placement is advisory, never fatal
            bed = None
            logger.debug("build volume lookup failed for %r", printer_id,
                         exc_info=True)
        if bed is not None:
            name = get_printer_display_name(bed.printer_id) or bed.printer_id
            return bed.volume[0], bed.volume[1], bed.volume[2], f"the {name}'s bed"
    return plate_width, plate_depth, None, "the plate it was laid out for"


def placed_bbox(parts: list[ColorPart]) -> dict[str, float] | None:
    """The bounding box of *parts* where they are placed: each mesh's own bounds moved by its ``x/y/z``."""
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for part in parts:
        try:
            box = _stl_bounding_box(part.stl_path)
        except Exception:  # noqa: BLE001 — an unreadable mesh is not measured
            continue
        offset = (part.x, part.y, part.z)
        for axis in range(3):
            lo[axis] = min(lo[axis], box[axis] + offset[axis])
            hi[axis] = max(hi[axis], box[axis + 3] + offset[axis])
    if lo[0] == float("inf"):
        return None
    return {
        "x_min": lo[0], "x_max": hi[0],
        "y_min": lo[1], "y_max": hi[1],
        "z_min": lo[2], "z_max": hi[2],
    }


def _plate_translation(
    min_x: float, max_x: float,
    min_y: float, max_y: float,
    min_z: float,
    plate_width: float, plate_depth: float,
) -> tuple[float, float, float]:
    """The translation that puts a bbox on the plate — zeros when it fits."""
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
    """Arrange multiple parts on a print plate without overlapping."""
    if printer_id:
        from kiln.printers.bed_fit import printer_bed_volume

        build_volume = printer_bed_volume(printer_id)
        if build_volume is None:
            from kiln.printers.bed_fit import no_bed_reason

            raise ValueError(
                no_bed_reason(printer_id, instead="omit printer_id and pass plate_width/plate_depth explicitly")
            )
        plate_width = build_volume[0]
        plate_depth = build_volume[1]

    groups: dict[int, list[dict[str, Any]]] = {}
    spec_groups: list[int] = []          # parallel to part_specs
    for i, spec in enumerate(part_specs):
        g = int(spec.get("group", i))
        groups.setdefault(g, []).append(spec)
        spec_groups.append(g)

    # For each group, take the union bounding box of all its parts
    group_order = sorted(groups.keys())
    group_bboxes: dict[int, tuple[float, float, float, float]] = {}
    for g in group_order:
        mn_x = mn_y = float("inf")
        mx_x = mx_y = float("-inf")
        for spec in groups[g]:
            try:
                b_mn_x, b_mn_y, _, b_mx_x, b_mx_y, _ = _stl_bounding_box(spec["stl_path"])
            except Exception:
                b_mn_x, b_mn_y = 0.0, 0.0
                b_mx_x, b_mx_y = 50.0, 50.0
            mn_x, mn_y = min(mn_x, b_mn_x), min(mn_y, b_mn_y)
            mx_x, mx_y = max(mx_x, b_mx_x), max(mx_y, b_mx_y)
        group_bboxes[g] = (mn_x, mn_y, mx_x - mn_x, mx_y - mn_y)

    group_positions: dict[int, tuple[float, float]] = {}
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
    """Compose a multi-color / multi-material .3mf from multiple STL files."""
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
    # -----------------------------------------------------------------------
    safety_result: dict[str, Any] | None = None
    flush_matrix_str: str | None = None
    specified_materials = [p.material for p in parts if p.material and p.material.strip()]
    if specified_materials:
        try:
            from kiln.material_safety import (  # lazy import keeps startup lean
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
    # Place the GROUP on the plate
    # -----------------------------------------------------------------------
    plate_w, plate_d, plate_h, bed_words = plate_bed(plate_width, plate_depth, printer_id)
    g_min_x = g_min_y = g_min_z = float("inf")
    g_max_x = g_max_y = g_max_z = float("-inf")
    for part, vertices, _ in parsed:
        g_min_x = min(g_min_x, min(v[0] for v in vertices) + part.x)
        g_max_x = max(g_max_x, max(v[0] for v in vertices) + part.x)
        g_min_y = min(g_min_y, min(v[1] for v in vertices) + part.y)
        g_max_y = max(g_max_y, max(v[1] for v in vertices) + part.y)
        g_min_z = min(g_min_z, min(v[2] for v in vertices) + part.z)
        g_max_z = max(g_max_z, max(v[2] for v in vertices) + part.z)
    bed_tx, bed_ty, bed_tz = _plate_translation(
        g_min_x, g_max_x, g_min_y, g_max_y, g_min_z, plate_w, plate_d,
    )
    from kiln.printers.bed_fit import plate_fit

    plate_verdict = plate_fit(
        {
            "x_min": g_min_x + bed_tx, "x_max": g_max_x + bed_tx,
            "y_min": g_min_y + bed_ty, "y_max": g_max_y + bed_ty,
            "z_min": g_min_z + bed_tz, "z_max": g_max_z + bed_tz,
        },
        (plate_w, plate_d, plate_h),
        bed=bed_words,
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
    groups = _touching_groups(parsed)
    members: list[tuple[str, str | bytes]] = [
        ("[Content_Types].xml",             _CONTENT_TYPES),
        ("_rels/.rels",                     _RELS),
        ("3D/3dmodel.model",                _build_model_xml(parsed, groups)),
        ("Metadata/model_settings.config",  _build_model_settings(parsed, groups)),
        ("Metadata/Slic3r_PE_model.config", _build_prusa_model_config(parsed)),
    ]
    if thumbnail_data:
        members.append(("Metadata/plate_1.png", thumbnail_data))
    try:
        _write_archive(output_path, members)
        if specified_materials:
            flush_matrix_str = _flush_matrix_for(output_path, parsed)
            if flush_matrix_str:
                members.append((
                    "Metadata/project_settings.config",
                    _build_project_settings(flush_matrix_str),
                ))
                _write_archive(output_path, members)
    except Exception as exc:
        return {"success": False, "error": f"Failed to write 3MF archive: {exc}"}

    total_v = sum(len(v) for _, v, _ in parsed)
    total_t = sum(len(t) for _, _, t in parsed)
    slot_lines = _slot_summary(parsed)
    extruder_summary = ", ".join(slot_lines)
    joined = [g for g in groups if len(g) > 1]

    logger.info(
        "compose_multicolor_3mf: wrote %s (%d parts in %d objects, %d triangles)",
        output_path,
        len(parsed),
        len(groups),
        total_t,
    )

    result: dict[str, Any] = {
        "success": True,
        "output_path": output_path,
        "parts": len(parsed),
        "objects": len(groups),
        "colours": len(slot_lines),
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
    if len(slot_lines) > 1:
        result["slicer_note"] = MULTI_EXTRUDER_SLICER_NOTE + (
            " " + JOINED_PARTS_PRUSA_NOTE if joined else ""
        )

    # Attach full safety report (all free)
    if safety_result is not None:
        result["safety_level"] = safety_result["level"]
        result["safety_message"] = safety_result["message"]
        if safety_result.get("hardware_warnings"):
            result["hardware_warnings"] = safety_result["hardware_warnings"]
        if safety_result.get("pairs"):
            result["material_pairs"] = safety_result["pairs"]
        result["flush_matrix_embedded"] = bool(flush_matrix_str)

    result["fits_bed"] = plate_verdict["fits"]
    result["bed_fit"] = plate_verdict

    # Compose human summary
    safety_note = ""
    if safety_result and safety_result["level"] in ("caution", "conditional"):
        warning = str(safety_result["message"])
        safety_note = f" {warning}" if warning.startswith("⚠") else f" ⚠️  {warning}"

    if plate_verdict["fits"] is False:
        next_step = f" {plate_verdict['message']} It is not ready to print as laid out."
    else:
        next_step = (
            f" Next step: slice_and_print('{output_path}') slices it for the printer and "
            f"prints it, or slice_model('{output_path}') to look at the slice first."
        )
    joined_note = ""
    if joined:
        pieces = sum(len(g) for g in joined)
        objects = "one object" if len(joined) == 1 else f"{len(joined)} objects"
        joined_note = (
            f" {pieces} parts touch, so they print as {objects}; where two "
            "overlap, the smaller one is printed in the space they share."
        )
    colours = len(slot_lines)
    result["message"] = (
        f"Created a {colours}-colour 3MF ({total_t:,} triangles): "
        f"{extruder_summary}.{joined_note} "
        f"Compatible with BambuStudio (AMS), PrusaSlicer (MMU), and Cura."
        + (" Flush matrix embedded for purge tower sizing." if flush_matrix_str else "")
        + safety_note
        + next_step
    )

    return result


_NAME_NUMBERING_RE = re.compile(r"(?:[ ._-]*\d+|\s*\(\d+\))$")


def _slot_label(parts: list[ColorPart], names: list[str]) -> str:
    """How one extruder slot's parts are named to a person."""
    materials = {(p.material or "").strip() for p in parts}
    if len(materials) == 1 and "" not in materials:
        label = materials.pop()
    elif len(names) == 1:
        return names[0]
    else:
        stems = {_NAME_NUMBERING_RE.sub("", n).strip() for n in names}
        if len(stems) != 1 or not next(iter(stems)):
            return f"{names[0]} and {len(names) - 1} more"
        label = stems.pop()
    return label if len(parts) == 1 else f"{label} ({len(parts)} pieces)"


def _slot_summary(parsed: list[_ParsedPart]) -> list[str]:
    """One phrase per extruder slot, in slot order: "Icing pink in slot 2"."""
    from kiln.threemf_parser import unique_object_names

    names = unique_object_names([part.name for part, _, _ in parsed])
    by_slot: dict[int, list[int]] = {}
    for i, (part, _, _) in enumerate(parsed):
        by_slot.setdefault(part.extruder, []).append(i)
    return [
        f"{_slot_label([parsed[i][0] for i in idx], [names[i] for i in idx])}"
        f" in slot {slot}"
        for slot, idx in sorted(by_slot.items())
    ]


def _write_archive(output_path: str, members: list[tuple[str, str | bytes]]) -> None:
    """Write *members* to a new 3MF at *output_path*, in order, replacing it."""
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)


def _flush_matrix_for(output_path: str, parsed: list[_ParsedPart]) -> str | None:
    """The purge matrix for *output_path*, one row per filament slot a slice of it loads, or ``None`` when that slice loads a single filament."""
    evidence = detect_3mf_multicolor(output_path)
    slots = int((evidence or {}).get("filament_slots_needed", 0))
    if slots < 2:
        return None
    try:
        from kiln.material_safety import build_bambu_flush_matrix
    except ImportError:
        return None
    per_slot: dict[int, str] = {}
    for part, _, _ in parsed:
        material = (part.material or "").strip()
        if material:
            per_slot.setdefault(part.extruder, material)
    materials = [per_slot.get(slot, "") for slot in range(1, slots + 1)]
    return build_bambu_flush_matrix(materials, n_slots=slots)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

#: Sidecar config naming each part's type and extruder (Bambu family).
_BAMBU_CONFIG = "metadata/model_settings.config"
_PRUSA_CONFIG = "metadata/slic3r_pe_model.config"


#: A sidecar the hardened reader turned away: present, but not to be read.
_REFUSED = object()


def _sidecar_root(zf: zipfile.ZipFile, lowered: str) -> Any:
    """The sidecar config named *lowered*."""
    import xml.etree.ElementTree as ET

    from kiln.threemf_parser import (
        _STRUCTURE,
        _EntityDeclarationRefused,
        _RetainedBudget,
        _stream_part,
    )
    from kiln.untrusted_zip import UntrustedArchiveError

    name = next((n for n in zf.namelist() if n.lower() == lowered), None)
    if name is None:
        return None
    try:
        with zf.open(name) as fh:
            root, _ = _stream_part(fh, _RetainedBudget(), _STRUCTURE, refuse_entities=True)
    except (KeyError, ET.ParseError, _EntityDeclarationRefused, UntrustedArchiveError):
        return _REFUSED
    return root if root is not None else _REFUSED


def _int_value(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def prusa_multipart_copy(input_path: str, out_path: str) -> bool:
    """Write *out_path*: *input_path* with every build object as ONE PrusaSlicer object holding one volume per part."""
    try:
        return _write_prusa_multipart_copy(input_path, out_path)
    except Exception:  # noqa: BLE001 — the caller slices the original instead
        logger.debug("PrusaSlicer multi-part copy skipped", exc_info=True)
        with contextlib.suppress(OSError):
            if os.path.exists(out_path):
                os.remove(out_path)
        return False


@dataclass
class _CopyVolume:
    """One part of one build object in the PrusaSlicer copy."""

    placed: Any
    info: Any
    extruder: int
    flip: bool
    name: str


def _write_prusa_multipart_copy(input_path: str, out_path: str) -> bool:
    import io

    from kiln import threemf_parser as tp

    with UntrustedZipFile(input_path) as zf:
        archive = tp._ModelArchive(zf, [], keep_mesh=True)
        root_transform = archive.root_transform_mm()
        if archive.root is None or root_transform is None:
            return False

        bambu = _sidecar_root(zf, _BAMBU_CONFIG)
        prusa = _sidecar_root(zf, _PRUSA_CONFIG)
        if bambu is _REFUSED or prusa is _REFUSED:
            logger.info("PrusaSlicer multi-part copy refused: a sidecar config was refused")
            return False
        object_extruder: dict[int, int] = {}
        part_config: dict[tuple[int, int], tuple[str, int | None]] = {}
        if bambu is not None:
            for obj in bambu.iter("object"):
                oid = _int_value(obj.get("id"))
                if oid is None:
                    continue
                for md in obj.findall("metadata"):
                    if md.get("key") == "extruder":
                        ext = _int_value(md.get("value"))
                        if ext is not None:
                            object_extruder[oid] = ext
                for part_el in obj.findall("part"):
                    pid = _int_value(part_el.get("id"))
                    if pid is None:
                        continue
                    ext = None
                    for md in part_el.findall("metadata"):
                        if md.get("key") == "extruder":
                            ext = _int_value(md.get("value"))
                    part_config[(oid, pid)] = (
                        part_el.get("subtype") or "normal_part", ext,
                    )
        prusa_extruder: dict[int, int] = {}
        if prusa is not None:
            for obj in prusa.iter("object"):
                if len(obj.findall("volume")) > 1:
                    return False
                oid = _int_value(obj.get("id"))
                for md in obj.iter("metadata"):
                    if md.get("key") == "extruder" and oid is not None:
                        ext = _int_value(md.get("value"))
                        if ext is not None:
                            prusa_extruder.setdefault(oid, ext)

        placement_budget = tp._MAX_RETAINED_ELEMENTS
        entry_budget = tp._MAX_KEPT_MESH_ENTRIES
        placements = entries = 0
        items: list[tuple[Any, list[_CopyVolume]]] = []
        for placed in archive.placements(root_transform=root_transform):
            placements += 1
            if placements > placement_budget:
                logger.info(
                    "PrusaSlicer multi-part copy refused: more than %d placements",
                    placement_budget,
                )
                return False
            if not placed.ancestry:
                items.append((placed, []))
            info = archive.mesh(placed)
            if info is None or not items:
                continue
            root = items[-1][0]
            n_vertices = len(info.vertices)
            tri = info.tri_idx
            entries += n_vertices + len(tri)
            if entries > entry_budget:
                logger.info(
                    "PrusaSlicer multi-part copy refused: more than %d vertices "
                    "and triangles across placements", entry_budget,
                )
                return False
            if len(tri) == 0 or (tri < 0).any() or (tri >= n_vertices).any():
                return False
            subtype, part_ext = part_config.get(
                (root.object_id, placed.object_id), ("normal_part", None),
            )
            if subtype != "normal_part":
                return False
            m = placed.transform
            determinant = (
                m[0] * (m[4] * m[8] - m[5] * m[7])
                - m[1] * (m[3] * m[8] - m[5] * m[6])
                + m[2] * (m[3] * m[7] - m[4] * m[6])
            )
            items[-1][1].append(_CopyVolume(
                placed=placed,
                info=info,
                extruder=(
                    part_ext
                    or prusa_extruder.get(placed.object_id)
                    or object_extruder.get(root.object_id)
                    or object_extruder.get(placed.object_id)
                    or 1
                ),
                # A mirror turns every triangle inside out.
                flip=determinant < 0,
                name=placed.name or "",
            ))
        if not any(len(volumes) > 1 for _, volumes in items):
            return False
        roots = [(root.part, root.object_id) for root, _ in items]
        if len(set(roots)) != len(roots):
            return False

        config: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
        dropped = {_BAMBU_CONFIG, _PRUSA_CONFIG, "[content_types].xml", "_rels/.rels"}
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as out:
            out.writestr("[Content_Types].xml", _CONTENT_TYPES)
            out.writestr("_rels/.rels", _RELS)
            with out.open("3D/3dmodel.model", "w") as raw, io.TextIOWrapper(
                raw, encoding="utf-8", newline="\n",
            ) as model:
                model.write(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<model unit="millimeter" xml:lang="en-US"'
                    ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
                    ' xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">\n'
                    ' <metadata name="Application">Kiln</metadata>\n'
                    " <resources>\n"
                )
                for k, (root, volumes) in enumerate(items, start=1):
                    obj_name = _xml_escape(root.name or f"Object {k}")
                    model.write(
                        f' <object id="{k}" type="model" name="{obj_name}">\n'
                        "  <mesh>\n   <vertices>\n"
                    )
                    for volume in volumes:
                        xs, ys, zs = tp._transform_columns(
                            volume.placed.transform, volume.info.vertices,
                        )
                        for x, y, z in zip(xs.tolist(), ys.tolist(), zs.tolist(), strict=True):
                            model.write(f'    <vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>\n')
                    model.write("   </vertices>\n   <triangles>\n")
                    config += [
                        f' <object id="{k}" instances_count="1">',
                        f'  <metadata type="object" key="name" value="{obj_name}"/>',
                    ]
                    base = first = 0
                    for volume in volumes:
                        info = volume.info
                        tri = info.tri_idx[:, [0, 2, 1]] if volume.flip else info.tri_idx
                        paint = [info.signatures[i][2] for i in info.tri_sig.tolist()]
                        for (a, b, c), state in zip(
                            (tri + base).tolist(), paint, strict=True,
                        ):
                            mmu = (
                                f' slic3rpe:mmu_segmentation="{_xml_escape(state)}"'
                                if state else ""
                            )
                            model.write(f'    <triangle v1="{a}" v2="{b}" v3="{c}"{mmu}/>\n')
                        last = first + len(tri) - 1
                        volume_name = _xml_escape(volume.name) or obj_name
                        config += [
                            f'  <volume firstid="{first}" lastid="{last}">',
                            f'   <metadata type="volume" key="name" value="{volume_name}"/>',
                            '   <metadata type="volume" key="volume_type" value="ModelPart"/>',
                            f'   <metadata type="volume" key="extruder" value="{volume.extruder}"/>',
                            "  </volume>",
                        ]
                        base += len(info.vertices)
                        first = last + 1
                    model.write("   </triangles>\n  </mesh>\n </object>\n")
                    config.append(" </object>")
                model.write(" </resources>\n <build>\n")
                for k in range(1, len(items) + 1):
                    model.write(
                        f'  <item objectid="{k}" transform="{_translation(0.0, 0.0, 0.0)}"/>\n'
                    )
                model.write(" </build>\n</model>\n")
            config.append("</config>")
            out.writestr("Metadata/Slic3r_PE_model.config", "\n".join(config))
            for member in zf.namelist():
                low = member.lower()
                if (
                    low in dropped
                    or low.endswith((".model", ".model.rels"))
                    or member.endswith("/")
                ):
                    continue
                out.writestr(member, zf.read(member))
    return True


# ---------------------------------------------------------------------------
# Painted single-object composer
# ---------------------------------------------------------------------------

PAINTED_STATE_MAX = 16


def painted_state_string(state: int) -> str:
    """The slicers' native serialized paint state for a WHOLE triangle."""
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
    """Compose a 3MF of ONE object whose colors vary per triangle."""
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

    # Place the object ON the plate
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
# ---------------------------------------------------------------------------

_PAINT_ATTRIBUTE_MARKERS = (b"paint_color", b"mmu_segmentation")

_SIDECAR_EXTRUDER_RE = re.compile(rb'key="extruder"\s+value="(\d+)"')

_ITEM_EXTRUDER_RE = re.compile(rb'slic3rpe:extruder="(\d+)"')

# : One palette entry inside <m:colorgroup>
_COLORGROUP_COLOR_RE = re.compile(rb"<m:color\s")

_COLORGROUP_VALUE_RE = re.compile(
    rb"""<m:color\s[^>]*?color=["'](\#?[0-9A-Fa-f]{6,8})["']"""
)

_SIDECAR_CONFIG_NAMES = frozenset({
    "metadata/model_settings.config",
    "metadata/slic3r_pe_model.config",
})

# : Most filament slots a detected file is reported as needing
_MAX_FILAMENT_SLOTS = PAINTED_STATE_MAX


def detect_3mf_multicolor(input_path: str) -> dict[str, Any] | None:
    """Cheap structural scan: does this 3MF ask for more than one filament?"""
    try:
        from kiln.threemf_parser import _decode_paint_states

        extruders: set[int] = set()
        paint_attribute: str | None = None
        paint_states: set[int] = set()
        palette: list[str] = []
        palette_colors = 0
        with UntrustedZipFile(input_path) as zf:
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
                    palette_colors = max(
                        palette_colors, len(_COLORGROUP_COLOR_RE.findall(raw)),
                    )
                    palette_values = [
                        "#" + m.decode("ascii").lstrip("#")[:6].upper()
                        for m in _COLORGROUP_VALUE_RE.findall(raw)
                    ]
                    if len(palette_values) > len(palette):
                        palette = palette_values

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
            max(extruders, default=0),
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
    """One display hex per filament slot, from *evidence*'s palette."""
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
