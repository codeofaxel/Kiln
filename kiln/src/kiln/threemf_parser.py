"""Parse 3MF files and extract colored triangle data for rendering.

Supports ``<basematerials>`` (core spec), ``<m:colorgroup>`` (materials
extension), the slicer settings sidecar, and the slicer PAINTING channel —
the per-triangle ``paint_color`` (BambuStudio / OrcaSlicer) /
``slic3rpe:mmu_segmentation`` (PrusaSlicer) attributes every
MakerWorld-style painted model carries.  Returns per-face color data
suitable for mesh visualization.
"""

from __future__ import annotations

import contextlib
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_MATERIAL_NS = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"
_SLIC3RPE_NS = "http://schemas.slic3r.org/3mf/2017/06"

_HEX_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$")

_DEFAULT_COLOR: tuple[int, int, int] = (170, 170, 170)

#: Per-triangle painting attributes.  BambuStudio and OrcaSlicer write
#: ``paint_color`` (un-namespaced); PrusaSlicer writes the namespaced
#: ``slic3rpe:mmu_segmentation`` — and each family's reader accepts the
#: other's spelling, so Kiln does too.
_PAINT_ATTRS = ("paint_color", f"{{{_SLIC3RPE_NS}}}mmu_segmentation")

#: Byte tokens whose absence proves the model XML carries no painting
#: attribute — shared by every pre-scan gate that turns a file away
#: before spending an XML parse on it.
_PAINT_BYTE_TOKENS = (b"paint_color", b"mmu_segmentation")


def _bytes_have_paint(raw: bytes) -> bool:
    """Whether *raw* model XML could carry per-triangle painting attributes."""
    return any(token in raw for token in _PAINT_BYTE_TOKENS)


#: Colors assigned to painted filament states.  A slicer-painted 3MF
#: carries NO palette of its own: the painting attribute names a FILAMENT
#: SLOT (state k = filament k), and the real hex of each filament lives in
#: the slicer's printer profile, not the archive.  The truth these files
#: carry is WHICH triangles share a filament — not what hex that filament
#: is — so states map onto this fixed palette (state k → entry
#: ``(k − 1) % 16``; states above 16 wrap), which keeps distinct filaments
#: visually distinct and the mapping stable across runs.  A consumer that
#: learns the real filament colors elsewhere can re-map through
#: ``ColoredTriangle.paint_state`` / ``ColoredMesh.states_present``.
_PAINT_STATE_PALETTE: tuple[tuple[int, int, int], ...] = (
    (230, 57, 53),    # 1  red
    (30, 136, 229),   # 2  blue
    (67, 160, 71),    # 3  green
    (253, 216, 53),   # 4  yellow
    (142, 36, 170),   # 5  purple
    (251, 140, 0),    # 6  orange
    (0, 172, 193),    # 7  cyan
    (109, 76, 65),    # 8  brown
    (236, 64, 122),   # 9  pink
    (124, 179, 66),   # 10 light green
    (57, 73, 171),    # 11 indigo
    (255, 112, 67),   # 12 deep orange
    (0, 137, 123),    # 13 teal
    (192, 202, 51),   # 14 lime
    (84, 110, 122),   # 15 blue-grey
    (255, 179, 0),    # 16 amber
)


def _paint_state_color(state: int) -> tuple[int, int, int]:
    """Deterministic display color for painted filament *state* (k ≥ 1)."""
    return _PAINT_STATE_PALETTE[(state - 1) % len(_PAINT_STATE_PALETTE)]


def _decode_paint_states(encoded: str) -> tuple[dict[int, float], bool] | None:
    """Decode a TriangleSelector state string into leaf-state area weights.

    The string is the hex form PrusaSlicer's
    ``FacetsAnnotation::get_triangle_as_string`` (``src/libslic3r/Model.cpp``)
    writes into the 3MF painting attribute and
    ``FacetsAnnotation::set_triangle_from_string`` reads back: the
    triangle's bitstream is cut into nibbles (LSB-first within each
    nibble), each nibble becomes one hex digit, and digits are PREPENDED —
    so the string reads right-to-left in stream order.  The bitstream
    itself is ``TriangleSelector::serialize``
    (``src/libslic3r/TriangleSelector.cpp``): each node's first nibble is
    ``xxyy`` where ``yy`` = number of split sides (0 = leaf).  For a leaf,
    ``xx`` is the state for 0–2, or ``0b11`` marking a second nibble that
    carries state − 3 (states 3–16), which in turn reserves ``0b1110`` to
    mark two further nibbles carrying state − 17 (states 17–255,
    PrusaSlicer's ``decode_leaf_state``).  For a split node ``xx`` is the
    special side and its ``yy + 1`` children follow depth-first, each
    encoded the same way.

    Sources (read 2026-08-02): github.com/prusa3d/PrusaSlicer master —
    ``TriangleSelector::serialize`` / ``decode_leaf_state`` and
    ``FacetsAnnotation::get_triangle_as_string`` /
    ``set_triangle_from_string``; github.com/SoftFever/OrcaSlicer main
    carries the identical encoding for states 0–16 (no 17–255 extended
    form), and its ``FacetsAnnotation`` string codec is byte-identical.

    Returns ``(weights, is_split)``: *weights* maps each leaf state to its
    approximate area fraction — every split divides the parent's weight
    equally among its children, exact for 2- and 4-child splits, a
    1/3-each approximation for 3-child splits (true areas 1/2, 1/4, 1/4) —
    and *is_split* says whether any sub-triangle split was present.
    Returns ``None`` for a malformed string: the painting channel is
    enrichment, so a bad attribute must read as "no paint", never raise.
    """
    text = encoded.strip()
    if not text:
        return None
    try:
        nibbles = [int(ch, 16) for ch in reversed(text)]
    except ValueError:
        return None
    stream = iter(nibbles)
    weights: dict[int, float] = {}

    def _walk(weight: float, depth: int) -> bool:
        code = next(stream)
        split_sides = code & 0b11
        if split_sides:
            if depth > 16:  # hostile-input guard; real paint trees are shallow
                raise ValueError("paint tree too deep")
            n_children = split_sides + 1
            for _ in range(n_children):
                _walk(weight / n_children, depth + 1)
            return True
        if (code & 0b1100) != 0b1100:
            state = code >> 2
        elif (second := next(stream)) != 0b1110:
            state = second + 3
        else:
            lo, hi = next(stream), next(stream)
            state = (lo | (hi << 4)) + 17
        weights[state] = weights.get(state, 0.0) + weight
        return False

    try:
        is_split = _walk(1.0, 0)
    except (StopIteration, ValueError):
        return None
    if next(stream, None) is not None:
        return None  # unread nibbles — a legitimate string is consumed exactly
    return weights, is_split


def _dominant_paint_state(weights: dict[int, float]) -> int:
    """The approximately area-dominant leaf state of a decoded paint tree.

    Ties break toward the LOWER state so the answer is deterministic.
    """
    return min(weights, key=lambda state: (-weights[state], state))


def _triangle_paint_attr(tri: ET.Element) -> str | None:
    """The triangle's painting attribute value, in either spelling.

    An empty string means "unpainted" upstream (PrusaSlicer's
    ``set_triangle_from_string`` treats it as state NONE), so it reads as
    absent here too.
    """
    for attr in _PAINT_ATTRS:
        value = tri.get(attr)
        if value:
            return value
    return None

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

    #: Dominant painted filament state when ``color`` came from the slicer
    #: painting channel (0 = the paint attribute said "unpainted"; k ≥ 1 =
    #: filament k, displayed via the deterministic state palette).  ``None``
    #: when the triangle carried no decodable paint attribute — a consumer
    #: that knows the real filament colors re-maps through this field.
    paint_state: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "v0": list(self.v0),
            "v1": list(self.v1),
            "v2": list(self.v2),
            "color": list(self.color),
            "paint_state": self.paint_state,
        }


@dataclass
class ObjectSegment:
    """A contiguous run of ``ColoredMesh.triangles`` from ONE build object.

    ``parse_colored_3mf`` flattens every object into one triangle list;
    the segment records where each object's triangles landed, so a
    consumer holding per-object geometry from another reader (trimesh's
    Scene) can line the two up instead of guessing at ordering.
    """

    object_id: int
    name: str | None
    start: int
    count: int

    @property
    def key(self) -> str:
        """The name trimesh gives this object's Scene geometry — the
        object's ``name``, else its id as a string; the same convention
        :func:`object_display_colors` keys by."""
        return self.name or str(self.object_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "name": self.name,
            "start": self.start,
            "count": self.count,
            "key": self.key,
        }


@dataclass
class ColoredMesh:
    """Parsed 3MF mesh with per-face colors."""

    triangles: list[ColoredTriangle] = field(default_factory=list)
    colors_found: bool = False
    color_count: int = 0

    #: Every painted filament state (k ≥ 1) seen anywhere in the file,
    #: minority states inside split triangles included — the full filament
    #: set a re-mapping consumer would need.  Sorted, empty when the file
    #: carries no painting.
    states_present: list[int] = field(default_factory=list)

    #: Triangles whose paint attribute encoded SUB-triangle painting.
    #: Their color is the approximately area-dominant state — an honest
    #: simplification, counted here so it is never a silent one.
    split_faces: int = 0

    #: Per-object triangle runs, in the order the objects were collected.
    segments: list[ObjectSegment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "triangles": [t.to_dict() for t in self.triangles],
            "triangle_count": len(self.triangles),
            "colors_found": self.colors_found,
            "color_count": self.color_count,
            "states_present": list(self.states_present),
            "split_faces": self.split_faces,
            "segments": [s.to_dict() for s in self.segments],
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
) -> tuple[list[ColoredTriangle], set[int], int]:
    """Extract colored triangles from a single ``<object>`` element.

    Per-triangle priority: core-spec ``pid``/``p1`` property references
    first, then the slicer painting attribute (a whole-triangle state
    k ≥ 1 maps to the deterministic state palette; state 0 and malformed
    strings fall through), then the object's own color — core-spec
    ``pid``/``pindex``, else *fallback_color* (the slicer-sidecar color),
    else *default_color*.

    Returns ``(triangles, painted_states_seen, split_face_count)`` —
    *painted_states_seen* is every k ≥ 1 leaf state decoded (minority
    states of split triangles included), *split_face_count* the number of
    triangles whose painting was sub-triangle and therefore reduced to
    the dominant state.
    """
    states_seen: set[int] = set()
    split_faces = 0

    mesh_el = obj_el.find(f"{{{_CORE_NS}}}mesh")
    if mesh_el is None:
        return [], states_seen, split_faces

    vertices = _parse_vertices(mesh_el)
    if not vertices:
        return [], states_seen, split_faces

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
        return triangles, states_seen, split_faces

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

        # Per-triangle color, strongest source first: core-spec property
        # reference (p1 used for whole-face color), then the slicer
        # painting attribute, then the object's own color.
        tri_pid = tri.get("pid")
        tri_p1 = tri.get("p1")
        paint_state: int | None = None
        if tri_pid is not None:
            color = _resolve_color(
                color_lookup, tri_pid, tri_p1, default=obj_color,
            )
        else:
            paint = _triangle_paint_attr(tri)
            decoded = _decode_paint_states(paint) if paint else None
            if decoded is not None:
                weights, is_split = decoded
                states_seen.update(k for k in weights if k >= 1)
                if is_split:
                    split_faces += 1
                paint_state = _dominant_paint_state(weights)
                color = (
                    _paint_state_color(paint_state)
                    if paint_state >= 1
                    else obj_color
                )
            else:
                color = obj_color

        triangles.append(ColoredTriangle(
            v0=vertices[v1_idx],
            v1=vertices[v2_idx],
            v2=vertices[v3_idx],
            color=color,
            paint_state=paint_state,
        ))

    return triangles, states_seen, split_faces


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

    Color sources, strongest first per triangle: core-spec property
    references (``<basematerials>`` and ``<m:colorgroup>``), then the
    slicer PAINTING channel — the per-triangle ``paint_color``
    (BambuStudio / OrcaSlicer) / ``slic3rpe:mmu_segmentation``
    (PrusaSlicer) attribute every MakerWorld-style painted model carries —
    then per-object fallbacks: the object's own ``pid``/``pindex``, else
    the slicer settings sidecar (``Metadata/model_settings.config``),
    which is where Kiln's own
    :func:`kiln.multicolor_3mf.compose_multicolor_3mf` records part
    colors.  Falls back to *default_color* when no color data is present.

    Painted files carry no palette — the paint attribute names a filament
    SLOT, whose real color lives in the slicer's printer profile, not the
    archive — so painted states display through the deterministic
    ``_PAINT_STATE_PALETTE`` and the state indices ride along
    (``ColoredTriangle.paint_state``, ``ColoredMesh.states_present``) for
    any consumer that can re-map them.  Sub-triangle painting reduces to
    the dominant state, counted in ``ColoredMesh.split_faces``.

    :param file_path: Path to a ``.3mf`` ZIP file.
    :param default_color: RGB tuple used when a triangle has no color.
    :returns: A :class:`ColoredMesh` with all triangles, color metadata,
        and per-object :class:`ObjectSegment` boundaries.
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
    segments: list[ObjectSegment] = []
    states_seen: set[int] = set()
    split_count = 0
    visited: set[int] = set()

    def _collect_from_object(obj_id: int) -> None:
        nonlocal split_count
        if obj_id in visited:
            return
        visited.add(obj_id)
        obj_el = objects.get(obj_id)
        if obj_el is None:
            return

        # Collect direct mesh triangles
        tris, states, splits = _collect_object_triangles(
            obj_el,
            color_lookup,
            default_color=default_color,
            fallback_color=sidecar_colors.get(obj_id),
        )
        if tris:
            segments.append(ObjectSegment(
                object_id=obj_id,
                name=obj_el.get("name"),
                start=len(all_triangles),
                count=len(tris),
            ))
        all_triangles.extend(tris)
        states_seen.update(states)
        split_count += splits

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
    colors_found = (
        bool(color_lookup) or bool(sidecar_colors) or bool(states_seen)
    ) and has_non_default

    return ColoredMesh(
        triangles=all_triangles,
        colors_found=colors_found,
        color_count=len(distinct_colors),
        states_present=sorted(states_seen),
        split_faces=split_count,
        segments=segments,
    )


def unique_object_names(names: Sequence[str | None]) -> list[str]:
    """Object names made unique, so a colored 3MF stays readable per part.

    The write-side counterpart to :func:`object_display_colors`' duplicate
    refusal, and the reason that refusal never has to fire on a file Kiln
    wrote.  Colour in a 3MF is carried per OBJECT, but every consumer that
    reads geometry through trimesh addresses objects by NAME — so two
    objects sharing one name make the whole file's colour unattributable,
    and the reader honestly declines all of it.  Duplicates are not an edge
    case: a CAD assembly legitimately holds four bolts all called "M3x8",
    and an unnamed STEP body degrades to its shape type, so a two-body file
    arrives as ``["SOLID", "SOLID"]``.

    Uniqueness is guaranteed against the key the reader actually builds
    (``name`` if non-empty, else the object id), by never returning a blank
    name: a name that is empty or all whitespace becomes ``part_N`` at its
    1-based position, which also keeps a nameless part legible in a slicer's
    object list.  Repeats then take a ``" (2)"``, ``" (3)"`` … suffix, the
    disambiguation every file browser and CAD tree already uses.

    The first use of a name always keeps it verbatim, and a suffix never
    lands on a name spoken for elsewhere in the list — ``["A", "A (2)",
    "A"]`` yields ``["A", "A (2)", "A (3)"]``, not a second ``"A (2)"``.
    Order-preserving, deterministic, and idempotent: names that are already
    unique come back untouched, so a caller may apply it twice without
    growing suffixes.
    """
    filled: list[str] = []
    for i, raw in enumerate(names):
        name = raw or ""
        filled.append(name if name.strip() else f"part_{i + 1}")

    # Every name in the list is spoken for from the start, so a suffix can
    # never collide with a name that appears LATER (the "A (2)" case above).
    taken = set(filled)
    seen: set[str] = set()
    out: list[str] = []
    for name in filled:
        if name not in seen:
            seen.add(name)
            out.append(name)
            continue
        n = 2
        while f"{name} ({n})" in taken or f"{name} ({n})" in seen:
            n += 1
        unique = f"{name} ({n})"
        seen.add(unique)
        out.append(unique)
    return out


def object_display_colors(file_path: str) -> dict[str, tuple[int, int, int]]:
    """Uniform display color per build object, keyed the way trimesh keys a
    Scene's geometry: the object's ``name`` attribute, else its id as a string.

    This is the color half of the 3MF story for consumers that read GEOMETRY
    through trimesh — trimesh 4.x drops every color a 3MF carries, core-spec
    basematerials and slicer sidecar alike (measured 2026-08-01), so the
    ``kiln.mesh.v1`` encoder asks this module, the one place that knows where
    3MF colors live.

    Strongest source first per object: core-spec object-level
    ``pid``/``pindex``, then the slicer sidecar.  Per-triangle property
    references resolve against the object's own color, slicer painting
    attributes contribute each decoded state's palette color, and ONE
    effective color is a uniform part (the shape Kiln's own composer
    writes so spec readers can bake vertex colors).  Omitted rather than
    guessed:

    * objects whose triangles resolve to MORE than one color — no single
      color can honestly stand for a painted object;
    * everything, when two build objects share a name — trimesh renames
      duplicates with suffixes that can collide with real sibling names, so a
      name-keyed map could color the wrong part.  That refusal is for files
      Kiln did not write: every composer here runs its names through
      :func:`unique_object_names` first, so a duplicate arriving at this
      point means the file came from elsewhere.

    Never raises: color is enrichment (the caller keeps its mesh either way),
    so any archive trouble reads as ``{}``.
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            xml_bytes = zf.read(_find_model_xml(zf))
            # Every 3MF headed for the stage passes through here, and an XML
            # parse is real money on a large mesh — a byte scan turns away
            # the files that carry no color construct at all.  Painting
            # attributes count (a painted object must be SEEN to be
            # refused, and a single-filament paint is honestly uniform);
            # sidecar-only colors still need the parse, so the sidecar is
            # scanned too.
            if (
                b"colorgroup" not in xml_bytes
                and b"basematerials" not in xml_bytes
                and not _bytes_have_paint(xml_bytes)
            ):
                sidecar_raw = b""
                for member in zf.namelist():
                    if member.lower() == _SLICER_SETTINGS_PATH.lower():
                        sidecar_raw = zf.read(member)
                        break
                if b'key="color"' not in sidecar_raw:
                    return {}
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

        base = _resolve_color(
            color_lookup, obj_el.get("pid"), obj_el.get("pindex"), default=None,
        )
        if base is None:
            with contextlib.suppress(ValueError, TypeError):
                base = sidecar_colors.get(int(oid))

        # Per-triangle property references resolve against the object's own
        # color; painting attributes contribute every leaf state's palette
        # color (state 0 = the base).  One effective color is a uniform
        # part; more than one is a painted object no single color can
        # honestly stand for.
        effective: set[tuple[int, int, int] | None] = set()
        triangles_el = mesh_el.find(f"{{{_CORE_NS}}}triangles")
        triangle_els = (
            [] if triangles_el is None
            else triangles_el.findall(f"{{{_CORE_NS}}}triangle")
        )
        for tri in triangle_els:
            tri_pid, tri_p1 = tri.get("pid"), tri.get("p1")
            if tri_pid is not None or tri_p1 is not None:
                effective.add(_resolve_color(
                    color_lookup,
                    tri_pid if tri_pid is not None else obj_el.get("pid"),
                    tri_p1,
                    default=base,
                ))
                continue
            paint = _triangle_paint_attr(tri)
            decoded = _decode_paint_states(paint) if paint else None
            if decoded is None:
                effective.add(base)
                continue
            weights, _is_split = decoded
            effective.update(
                base if state == 0 else _paint_state_color(state)
                for state in weights
            )
        if len(effective) > 1:
            continue
        color = effective.pop() if effective else base
        if color is not None:
            out[key] = color
    return out
