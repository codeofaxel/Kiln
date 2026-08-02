"""Parse 3MF files and extract colored triangle data for rendering.

Supports both ``<basematerials>`` (core spec) and ``<m:colorgroup>``
(materials extension) color definitions.  Returns per-face color data
suitable for mesh visualization.
"""

from __future__ import annotations

import contextlib
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_MATERIAL_NS = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"

_HEX_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$")

_DEFAULT_COLOR: tuple[int, int, int] = (170, 170, 170)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ColoredTriangle:
    """A triangle with vertex positions and an assigned face color."""

    v0: tuple[float, float, float]
    v1: tuple[float, float, float]
    v2: tuple[float, float, float]
    color: tuple[int, int, int]  # RGB 0-255

    def to_dict(self) -> dict[str, Any]:
        return {
            "v0": list(self.v0),
            "v1": list(self.v1),
            "v2": list(self.v2),
            "color": list(self.color),
        }


@dataclass
class ColoredMesh:
    """Parsed 3MF mesh with per-face colors."""

    triangles: list[ColoredTriangle] = field(default_factory=list)
    colors_found: bool = False
    color_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "triangles": [t.to_dict() for t in self.triangles],
            "triangle_count": len(self.triangles),
            "colors_found": self.colors_found,
            "color_count": self.color_count,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_hex_color(
    hex_str: str | None,
    *,
    fallback: tuple[int, int, int] | None = _DEFAULT_COLOR,
) -> tuple[int, int, int] | None:
    """Parse ``#RRGGBB`` or ``#RRGGBBAA`` to an ``(R, G, B)`` tuple.

    Returns *fallback* (``None`` only when a caller passes it explicitly)
    for ``None`` or malformed values.
    """
    if hex_str is None:
        return fallback
    m = _HEX_COLOR_RE.match(hex_str.strip())
    if m is None:
        return fallback
    rgb_hex = m.group(1)
    return (int(rgb_hex[0:2], 16), int(rgb_hex[2:4], 16), int(rgb_hex[4:6], 16))


def _build_color_lookup(
    resources_el: ET.Element,
) -> dict[int, list[tuple[int, int, int]]]:
    """Build a mapping of resource ID → ordered list of RGB colors.

    Scans both ``<basematerials>`` (core) and ``<m:colorgroup>``
    (materials extension) elements.
    """
    lookup: dict[int, list[tuple[int, int, int]]] = {}

    # Core spec: <basematerials id="N"> containing <base displaycolor="...">
    for bm in resources_el.findall(f"{{{_CORE_NS}}}basematerials"):
        rid = bm.get("id")
        if rid is None:
            continue
        colors: list[tuple[int, int, int]] = []
        for base in bm.findall(f"{{{_CORE_NS}}}base"):
            colors.append(_parse_hex_color(base.get("displaycolor")))
        lookup[int(rid)] = colors

    # Materials extension: <m:colorgroup id="N"> containing <m:color color="...">
    for cg in resources_el.findall(f"{{{_MATERIAL_NS}}}colorgroup"):
        rid = cg.get("id")
        if rid is None:
            continue
        colors = []
        for mc in cg.findall(f"{{{_MATERIAL_NS}}}color"):
            colors.append(_parse_hex_color(mc.get("color")))
        lookup[int(rid)] = colors

    return lookup


def _resolve_color(
    color_lookup: dict[int, list[tuple[int, int, int]]],
    pid: str | None,
    pindex: str | None,
    *,
    default: tuple[int, int, int] | None,
) -> tuple[int, int, int] | None:
    """Look up an RGB color from *pid* and *pindex* strings.

    Returns *default* (``None`` only when a caller passes it explicitly)
    when the reference is missing or out of range.
    """
    if pid is None or pindex is None:
        return default
    try:
        pid_int = int(pid)
        pindex_int = int(pindex)
    except (ValueError, TypeError):
        return default
    palette = color_lookup.get(pid_int)
    if palette is None or pindex_int < 0 or pindex_int >= len(palette):
        return default
    return palette[pindex_int]


def _parse_vertices(mesh_el: ET.Element) -> list[tuple[float, float, float]]:
    """Extract vertex positions from a ``<vertices>`` element."""
    verts: list[tuple[float, float, float]] = []
    vertices_el = mesh_el.find(f"{{{_CORE_NS}}}vertices")
    if vertices_el is None:
        return verts
    for v in vertices_el.findall(f"{{{_CORE_NS}}}vertex"):
        try:
            verts.append((
                float(v.get("x", "0")),
                float(v.get("y", "0")),
                float(v.get("z", "0")),
            ))
        except (ValueError, TypeError):
            verts.append((0.0, 0.0, 0.0))
    return verts


def _collect_object_triangles(
    obj_el: ET.Element,
    color_lookup: dict[int, list[tuple[int, int, int]]],
    *,
    default_color: tuple[int, int, int],
    fallback_color: tuple[int, int, int] | None = None,
) -> list[ColoredTriangle]:
    """Extract colored triangles from a single ``<object>`` element.

    *fallback_color* is the object's slicer-sidecar color, consulted only
    when the object carries no core-spec ``pid``/``pindex`` of its own.
    """
    mesh_el = obj_el.find(f"{{{_CORE_NS}}}mesh")
    if mesh_el is None:
        return []

    vertices = _parse_vertices(mesh_el)
    if not vertices:
        return []

    # Object-level color defaults: core spec first, then the sidecar
    obj_pid = obj_el.get("pid")
    obj_pindex = obj_el.get("pindex")
    obj_color = _resolve_color(
        color_lookup,
        obj_pid,
        obj_pindex,
        default=fallback_color if fallback_color is not None else default_color,
    )

    triangles: list[ColoredTriangle] = []
    triangles_el = mesh_el.find(f"{{{_CORE_NS}}}triangles")
    if triangles_el is None:
        return triangles

    for tri in triangles_el.findall(f"{{{_CORE_NS}}}triangle"):
        try:
            v1_idx = int(tri.get("v1", "-1"))
            v2_idx = int(tri.get("v2", "-1"))
            v3_idx = int(tri.get("v3", "-1"))
        except (ValueError, TypeError):
            continue

        if (
            v1_idx < 0
            or v2_idx < 0
            or v3_idx < 0
            or v1_idx >= len(vertices)
            or v2_idx >= len(vertices)
            or v3_idx >= len(vertices)
        ):
            continue

        # Per-triangle color override (p1 used for whole-face color)
        tri_pid = tri.get("pid")
        tri_p1 = tri.get("p1")
        if tri_pid is not None:
            color = _resolve_color(
                color_lookup, tri_pid, tri_p1, default=obj_color,
            )
        else:
            color = obj_color

        triangles.append(ColoredTriangle(
            v0=vertices[v1_idx],
            v1=vertices[v2_idx],
            v2=vertices[v3_idx],
            color=color,
        ))

    return triangles


def _find_model_xml(zf: zipfile.ZipFile) -> str:
    """Locate the 3D model XML inside the ZIP archive.

    Checks ``3D/3dmodel.model`` first, then falls back to scanning
    ``_rels/.rels`` for the primary model path.
    """
    standard_path = "3D/3dmodel.model"
    names = zf.namelist()

    # Direct check (case-insensitive)
    for name in names:
        if name.lower() == standard_path.lower():
            return name

    # Fallback: parse _rels/.rels for the StartPart relationship
    rels_path = "_rels/.rels"
    for name in names:
        if name.lower() == rels_path.lower():
            try:
                rels_xml = zf.read(name)
                rels_root = ET.fromstring(rels_xml)
                rels_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
                for rel in rels_root.findall(f"{{{rels_ns}}}Relationship"):
                    target = rel.get("Target", "")
                    if target.lower().endswith(".model"):
                        # Strip leading '/' if present
                        return target.lstrip("/")
            except ET.ParseError:
                break

    raise ValueError(
        f"No 3D model XML found in 3MF archive. "
        f"Expected '{standard_path}' or a .rels reference. "
        f"Archive contains: {names[:20]}"
    )


#: The BambuStudio / PrusaSlicer-family per-object settings sidecar — and
#: where Kiln's own :func:`kiln.multicolor_3mf.compose_multicolor_3mf`
#: records each part's color.  The core 3MF spec never sees these values.
_SLICER_SETTINGS_PATH = "Metadata/model_settings.config"


def _slicer_config_colors(zf: zipfile.ZipFile) -> dict[int, tuple[int, int, int]]:
    """Object-id → RGB from the slicer settings sidecar, ``{}`` if absent.

    Never raises: the sidecar is optional metadata, so a missing or
    malformed one simply contributes no color information.
    """
    name = next(
        (n for n in zf.namelist() if n.lower() == _SLICER_SETTINGS_PATH.lower()),
        None,
    )
    if name is None:
        return {}
    try:
        raw = zf.read(name)
    except KeyError:
        return {}
    # Same XML hardening as the SVG path in mark_geometry: stdlib
    # ElementTree refuses external entities, and billion-laughs needs
    # <!ENTITY> declarations no legitimate settings file carries.
    if re.search(rb"<!ENTITY", raw, re.IGNORECASE):
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    out: dict[int, tuple[int, int, int]] = {}
    for obj in root.iter("object"):
        oid = obj.get("id")
        if oid is None:
            continue
        value = None
        for md in obj.findall("metadata"):
            if md.get("key") == "color":
                value = (md.get("value") or "").strip()
        if not value:
            continue
        # Written both bare (Kiln) and #-prefixed (BambuStudio).
        parsed = _parse_hex_color(
            value if value.startswith("#") else f"#{value}", fallback=None,
        )
        if parsed is None:
            continue
        with contextlib.suppress(ValueError, TypeError):
            out[int(oid)] = parsed
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_colored_3mf(
    file_path: str,
    *,
    default_color: tuple[int, int, int] = _DEFAULT_COLOR,
) -> ColoredMesh:
    """Parse a 3MF file and extract triangles with per-face colors.

    Supports ``<basematerials>`` (core spec), ``<m:colorgroup>`` (materials
    extension), and — as a per-object fallback when the core spec is silent —
    the slicer settings sidecar (``Metadata/model_settings.config``), which is
    the only place Kiln's own :func:`kiln.multicolor_3mf.compose_multicolor_3mf`
    records part colors.  Falls back to *default_color* when no color data is
    present.

    :param file_path: Path to a ``.3mf`` ZIP file.
    :param default_color: RGB tuple used when a triangle has no color.
    :returns: A :class:`ColoredMesh` with all triangles and color metadata.
    :raises ValueError: If the archive has no model XML or the XML is corrupt.
    :raises zipfile.BadZipFile: If *file_path* is not a valid ZIP.
    :raises FileNotFoundError: If *file_path* does not exist.
    """
    with zipfile.ZipFile(file_path, "r") as zf:
        model_path = _find_model_xml(zf)
        try:
            xml_bytes = zf.read(model_path)
        except KeyError as exc:
            raise ValueError(
                f"Model XML path '{model_path}' found in rels but missing from archive"
            ) from exc
        sidecar_colors = _slicer_config_colors(zf)

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(
            f"Failed to parse 3MF model XML in '{file_path}': {exc}"
        ) from exc

    resources_el = root.find(f"{{{_CORE_NS}}}resources")
    if resources_el is None:
        return ColoredMesh(triangles=[], colors_found=False, color_count=0)

    color_lookup = _build_color_lookup(resources_el)

    # Index all objects by id for component assembly resolution
    objects: dict[int, ET.Element] = {}
    for obj in resources_el.findall(f"{{{_CORE_NS}}}object"):
        obj_id = obj.get("id")
        if obj_id is not None:
            with contextlib.suppress(ValueError, TypeError):
                objects[int(obj_id)] = obj

    # Determine root objects: objects referenced by <build> items,
    # or all objects if no <build> section exists.
    build_el = root.find(f"{{{_CORE_NS}}}build")
    if build_el is not None:
        root_ids: list[int] = []
        for item in build_el.findall(f"{{{_CORE_NS}}}item"):
            oid = item.get("objectid")
            if oid is not None:
                with contextlib.suppress(ValueError, TypeError):
                    root_ids.append(int(oid))
    else:
        root_ids = list(objects.keys())

    # Collect triangles, walking component assemblies
    all_triangles: list[ColoredTriangle] = []
    visited: set[int] = set()

    def _collect_from_object(obj_id: int) -> None:
        if obj_id in visited:
            return
        visited.add(obj_id)
        obj_el = objects.get(obj_id)
        if obj_el is None:
            return

        # Collect direct mesh triangles
        tris = _collect_object_triangles(
            obj_el,
            color_lookup,
            default_color=default_color,
            fallback_color=sidecar_colors.get(obj_id),
        )
        all_triangles.extend(tris)

        # Walk <components> for assemblies
        components_el = obj_el.find(f"{{{_CORE_NS}}}components")
        if components_el is not None:
            for comp in components_el.findall(f"{{{_CORE_NS}}}component"):
                comp_oid = comp.get("objectid")
                if comp_oid is not None:
                    with contextlib.suppress(ValueError, TypeError):
                        _collect_from_object(int(comp_oid))

    for rid in root_ids:
        _collect_from_object(rid)

    # Compute color metadata
    distinct_colors = {t.color for t in all_triangles}
    has_non_default = any(c != default_color for c in distinct_colors)
    colors_found = (bool(color_lookup) or bool(sidecar_colors)) and has_non_default

    return ColoredMesh(
        triangles=all_triangles,
        colors_found=colors_found,
        color_count=len(distinct_colors),
    )


def object_display_colors(file_path: str) -> dict[str, tuple[int, int, int]]:
    """Uniform display color per build object, keyed the way trimesh keys a
    Scene's geometry: the object's ``name`` attribute, else its id as a string.

    This is the color half of the 3MF story for consumers that read GEOMETRY
    through trimesh — trimesh 4.x drops every color a 3MF carries, core-spec
    basematerials and slicer sidecar alike (measured 2026-08-01), so the
    ``kiln.mesh.v1`` encoder asks this module, the one place that knows where
    3MF colors live.

    Strongest source first per object: core-spec object-level
    ``pid``/``pindex``, then the slicer sidecar.  Omitted rather than guessed:

    * objects whose triangles carry their own color overrides — a uniform
      bake of a painted object would render a solid color the file never
      claimed;
    * everything, when two build objects share a name — trimesh renames
      duplicates with suffixes that can collide with real sibling names, so a
      name-keyed map could color the wrong part.

    Never raises: color is enrichment (the caller keeps its mesh either way),
    so any archive trouble reads as ``{}``.
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            xml_bytes = zf.read(_find_model_xml(zf))
            sidecar_colors = _slicer_config_colors(zf)
        root = ET.fromstring(xml_bytes)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return {}

    resources_el = root.find(f"{{{_CORE_NS}}}resources")
    if resources_el is None:
        return {}
    color_lookup = _build_color_lookup(resources_el)

    out: dict[str, tuple[int, int, int]] = {}
    for obj_el in resources_el.findall(f"{{{_CORE_NS}}}object"):
        oid = obj_el.get("id")
        mesh_el = obj_el.find(f"{{{_CORE_NS}}}mesh")
        if oid is None or mesh_el is None:
            continue
        key = obj_el.get("name") or oid
        if key in out:
            return {}  # duplicate names — refuse to guess which part is which

        # A per-triangle pid or p1 means the color varies WITHIN the object.
        triangles_el = mesh_el.find(f"{{{_CORE_NS}}}triangles")
        painted = triangles_el is not None and any(
            tri.get("pid") is not None or tri.get("p1") is not None
            for tri in triangles_el.findall(f"{{{_CORE_NS}}}triangle")
        )
        if painted:
            continue

        color = _resolve_color(
            color_lookup, obj_el.get("pid"), obj_el.get("pindex"), default=None,
        )
        if color is None:
            with contextlib.suppress(ValueError, TypeError):
                color = sidecar_colors.get(int(oid))
        if color is not None:
            out[key] = color
    return out
