"""Parse 3MF files and extract colored triangle data for rendering.

Supports ``<basematerials>`` (core spec), ``<m:colorgroup>`` (materials
extension), the slicer settings sidecar, and the slicer PAINTING channel —
the per-triangle ``paint_color`` (BambuStudio / OrcaSlicer) /
``slic3rpe:mmu_segmentation`` (PrusaSlicer) attributes every
MakerWorld-style painted model carries.  Returns per-face color data
suitable for mesh visualization.

Also home to the one walk of a 3MF's object graph (:class:`_ModelArchive`):
every build item and component, following the production extension's
``p:path`` into the ``3D/Objects/object_N.model`` parts a BambuStudio /
OrcaSlicer project keeps its meshes in.  The geometry readers in
:mod:`kiln.generation.validation` and :mod:`kiln.printers.bed_fit` walk
through it too, so a layout one reader understands the others cannot miss.
"""

from __future__ import annotations

import contextlib
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from array import array
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from kiln.untrusted_zip import UntrustedArchiveError, open_untrusted_zip

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_MATERIAL_NS = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"
_SLIC3RPE_NS = "http://schemas.slic3r.org/3mf/2017/06"
#: The 3MF production extension: build items and components may point at
#: an object in ANOTHER model part of the archive through ``p:path``.
#: BambuStudio / OrcaSlicer project files (every MakerWorld download) keep
#: each mesh in its own ``3D/Objects/object_N.model`` this way, so a
#: reader that only opens the root model sees an empty plate.
_PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
_PATH_ATTR = f"{{{_PRODUCTION_NS}}}path"

_HEX_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$")

_DEFAULT_COLOR: tuple[int, int, int] = (170, 170, 170)


# ---------------------------------------------------------------------------
# Streaming model parts — the structure kept, the mesh never materialized
# ---------------------------------------------------------------------------
#
# Opening a 3MF means opening a ZIP a stranger produced; :mod:`kiln.untrusted_zip`
# bounds what that open can cost (a read ceiling on every member, a shape
# pre-check that refuses a bomb).  What it cannot bound is a reader that holds
# too much of an HONEST file: parsed into an ElementTree, a model part costs
# ~16-20x its XML — an Element, an attribute dict and three strings per vertex
# and per triangle — which took a 126 MB model to ~2 GB (measured 2026-10-13).
# A model part is almost all mesh: the two real downloaded projects measured
# that day hold 1.5 M and 2.2 M <vertex>/<triangle> elements beside 231 and 36
# of everything else.
#
# So a part is read with ElementTree's parser-target API.  The STRUCTURE —
# objects, components, build items, palettes, everything a walk or a colour
# lookup reads — is built as ordinary Elements; every <vertex> and <triangle>
# is read from its attributes as it streams past and never becomes an Element;
# and no text is kept (no 3MF reader reads element text).  Each reader then
# chooses how much of the mesh to hold:
#
#   * KEEP — compact arrays per object (8 bytes a coordinate, 8 an index, 4 a
#     colour signature): the readers that hand the whole mesh on (the stage
#     preview, the validation geometry, the full colour parse);
#   * FOLD — the mesh handed to the reader a chunk at a time and dropped: the
#     readers whose answer is small (a bounding box, a colour set), which read a
#     part twice — first its STRUCTURE (counts only), then a fold — so their
#     memory does not grow with the mesh at all.
#
# The capture rules are the ones the tree readers used, so every reader sees
# the file it saw before: an object's FIRST <mesh>, that mesh's FIRST
# <vertices> and <triangles>, their DIRECT children; a vertex whose coordinates
# will not parse is (0, 0, 0); a triangle index that is missing or will not
# parse is -1, which every reader's range check already skips.

_OBJECT_TAG = f"{{{_CORE_NS}}}object"
_MESH_TAG = f"{{{_CORE_NS}}}mesh"
_VERTICES_TAG = f"{{{_CORE_NS}}}vertices"
_VERTEX_TAG = f"{{{_CORE_NS}}}vertex"
_TRIANGLES_TAG = f"{{{_CORE_NS}}}triangles"
_TRIANGLE_TAG = f"{{{_CORE_NS}}}triangle"
_RESOURCES_TAG = f"{{{_CORE_NS}}}resources"

#: Structure elements one archive walk may keep: objects, components, build
#: items, palettes, metadata — never the mesh, which is not kept as elements.
#: Measured 2026-10-13: the largest real project on hand (a 48-member
#: downloaded project plate) keeps 231 across all its parts, ~450 bytes each.  A file
#: of millions of tiny structure elements is a bomb no lean reader can absorb —
#: the structure is what a walk must keep — so past 100 000 (>400x reality,
#: ~45 MB) the archive is refused like any other bomb.
_MAX_RETAINED_ELEMENTS = 100_000

#: Mesh entries one KEEP walk may hold — vertices, triangles and distinct
#: triangle colour signatures together.  KEEP readers hand the whole mesh on,
#: and their callers turn it into Python objects at ~10x the compact arrays,
#: so an archive within the ZIP guard's caps could otherwise make them hold
#: gigabytes.  The largest real model measured 2026-10-13 holds 2.2 M
#: vertices and triangles; 6 M clears it ~3x.  FOLD readers (a bounding box,
#: a colour count) hold nothing per element and have no such limit, so a
#: model past it still gets its bed-fit check and colour count.
_MAX_KEPT_MESH_ENTRIES = 6_000_000

#: Bytes read from an archive member per parser feed.
_STREAM_CHUNK_BYTES = 1 << 16

#: Vertices (or triangles) handed to a FOLD reader at once.
_FOLD_CHUNK = 1 << 16

#: The byte run a model or sidecar refuses outright (see :func:`_stream_part`).
_ENTITY_DECL_RE = re.compile(rb"<!ENTITY", re.IGNORECASE)

_KEEP = "keep"
_FOLD = "fold"
_STRUCTURE = "structure"

#: A triangle's colour signature: its ``pid``, its ``p1``, and its painting
#: attribute — everything any colour reader looks at on a triangle.
_TriangleSig = tuple[str | None, str | None, str | None]


class _EntityDeclarationRefused(ValueError):
    """The member carries an ``<!ENTITY`` declaration the reader refuses."""


class _RetainedBudget:
    """Counts what one archive walk keeps: structure elements, and (KEEP mode)
    mesh entries."""

    __slots__ = ("left", "limit", "mesh_left", "mesh_limit")

    def __init__(self, limit: int = _MAX_RETAINED_ELEMENTS) -> None:
        self.limit = limit
        self.left = limit
        self.mesh_limit = _MAX_KEPT_MESH_ENTRIES
        self.mesh_left = self.mesh_limit

    def take_mesh(self) -> None:
        self.mesh_left -= 1
        if self.mesh_left < 0:
            raise UntrustedArchiveError(
                f"3MF mesh is too large to hold whole (more than {self.mesh_limit} "
                "vertices and triangles)"
            )

    def take(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise UntrustedArchiveError(
                f"3MF model structure holds more than {self.limit} elements"
            )


@dataclass
class _MeshInfo:
    """The first ``<mesh>`` of one ``<object>``, as the stream found it.

    *ordinal* is the object's position among its part's objects, the same on
    every read of the part, so a FOLD pass can name the object a STRUCTURE
    pass found.  The arrays are filled only when the part was read to KEEP.
    """

    ordinal: int
    has_mesh: bool = False
    n_vertices: int = 0
    n_triangles: int = 0
    #: (N, 3) float64 vertex coordinates.
    vertices: Any = None
    #: (M, 3) int64 vertex indices; -1 where an index was missing or unparseable.
    tri_idx: Any = None
    #: (M,) int32 index into :attr:`signatures`, one per triangle.
    tri_sig: Any = None
    signatures: list[_TriangleSig] = field(default_factory=list)


def _numpy() -> Any:
    import numpy as np

    return np


def _parse_index(value: str | None) -> int:
    """A triangle's vertex index as the tree readers parsed it, -1 when it
    would not parse (or is missing) — which their range checks skip."""
    if value is None:
        return -1
    try:
        index = int(value)
    except ValueError:
        return -1
    return index if -(2**63) <= index < 2**63 else -1


def _parse_vertex(attrib: Any) -> tuple[float, float, float]:
    """A vertex the way the tree readers read one: (0, 0, 0) when any
    coordinate will not parse."""
    try:
        return (
            float(attrib.get("x", "0")),
            float(attrib.get("y", "0")),
            float(attrib.get("z", "0")),
        )
    except (ValueError, TypeError):
        return (0.0, 0.0, 0.0)


class _PartBuilder:
    """``XMLParser`` target for one model part (see the section comment)."""

    def __init__(
        self,
        budget: _RetainedBudget,
        mode: str,
        fold: Any = None,
        *,
        want_vertices: bool = True,
        want_triangles: bool = True,
    ) -> None:
        self._budget = budget
        self._mode = mode
        self._fold = fold
        self._want_v = mode == _KEEP or (mode == _FOLD and want_vertices)
        self._want_t = mode == _KEEP or (mode == _FOLD and want_triangles)
        # Open elements, innermost last; None marks a vertex / triangle,
        # whose contents nothing reads.
        self._stack: list[ET.Element | None] = []
        self.root: ET.Element | None = None
        self.meshes: dict[ET.Element, _MeshInfo] = {}
        self._resources: ET.Element | None = None
        self._ordinal = 0
        self._new_object(None)

    def _new_object(self, obj: ET.Element | None) -> None:
        self._obj = obj
        self._info: _MeshInfo | None = None
        self._mesh: ET.Element | None = None
        self._verts: ET.Element | None = None
        self._tris: ET.Element | None = None
        self._mesh_seen = self._verts_seen = self._tris_seen = False
        self._vbuf = array("d")
        self._tbuf = array("q")
        self._sig_idx = array("i")
        self._sig_list: list[_TriangleSig] = []
        self._sig_index: dict[_TriangleSig, int] = {}

    # -- XMLParser target interface -----------------------------------------

    def start(self, tag: str, attrib: dict[str, str]) -> None:
        stack = self._stack
        if stack and stack[-1] is None:
            stack.append(None)  # inside a vertex / triangle
            return
        parent = stack[-1] if stack else None
        if tag == _VERTEX_TAG:
            if parent is not None and parent is self._verts:
                self._vertex(attrib)
            stack.append(None)
            return
        if tag == _TRIANGLE_TAG:
            if parent is not None and parent is self._tris:
                self._triangle(attrib)
            stack.append(None)
            return
        self._budget.take()
        el = ET.Element(tag, attrib)
        if parent is None:
            self.root = el
        else:
            parent.append(el)
        stack.append(el)
        if parent is None:
            return
        if tag == _RESOURCES_TAG:
            if parent is self.root and self._resources is None:
                self._resources = el
        elif tag == _OBJECT_TAG:
            if parent is self._resources:
                self._new_object(el)
                self._info = self.meshes[el] = _MeshInfo(ordinal=self._ordinal)
                self._ordinal += 1
        elif tag == _MESH_TAG:
            if parent is self._obj and not self._mesh_seen:
                self._mesh = el
                self._mesh_seen = True
                self._info.has_mesh = True  # type: ignore[union-attr]
        elif tag == _VERTICES_TAG:
            if self._mesh is not None and parent is self._mesh and not self._verts_seen:
                self._verts = el
                self._verts_seen = True
        elif (
            tag == _TRIANGLES_TAG
            and self._mesh is not None
            and parent is self._mesh
            and not self._tris_seen
        ):
            self._tris = el
            self._tris_seen = True

    def end(self, tag: str) -> None:
        el = self._stack.pop()
        if el is None:
            return
        if el is self._verts:
            self._flush_vertices()
            self._verts = None
        elif el is self._tris:
            self._flush_triangles()
            self._tris = None
        elif el is self._mesh:
            self._mesh = None
        elif el is self._obj:
            self._end_object()

    def data(self, text: str) -> None:
        """Text is never read by a 3MF reader, so none is kept."""

    def close(self) -> ET.Element | None:
        return self.root

    # -- the mesh, as it streams past ---------------------------------------

    def _vertex(self, attrib: dict[str, str]) -> None:
        self._info.n_vertices += 1  # type: ignore[union-attr]
        if not self._want_v:
            return
        if self._mode == _KEEP:
            self._budget.take_mesh()
        x, y, z = _parse_vertex(attrib)
        buf = self._vbuf
        buf.append(x)
        buf.append(y)
        buf.append(z)
        if self._mode == _FOLD and len(buf) >= 3 * _FOLD_CHUNK:
            self._flush_vertices()

    def _triangle(self, attrib: dict[str, str]) -> None:
        info = self._info
        info.n_triangles += 1  # type: ignore[union-attr]
        if not self._want_t:
            return
        get = attrib.get
        buf = self._tbuf
        buf.append(_parse_index(get("v1")))
        buf.append(_parse_index(get("v2")))
        buf.append(_parse_index(get("v3")))
        sig = (get("pid"), get("p1"), _triangle_paint_attr(attrib))
        if self._mode == _KEEP:
            self._budget.take_mesh()
            idx = self._sig_index.get(sig)
            if idx is None:
                self._budget.take_mesh()
                idx = self._sig_index[sig] = len(info.signatures)  # type: ignore[union-attr]
                info.signatures.append(sig)  # type: ignore[union-attr]
            self._sig_idx.append(idx)
        else:
            self._sig_list.append(sig)
            if len(self._sig_list) >= _FOLD_CHUNK:
                self._flush_triangles()

    def _flush_vertices(self) -> None:
        if self._mode != _FOLD or not self._vbuf:
            return
        np = _numpy()
        chunk = np.frombuffer(self._vbuf, dtype=np.float64).reshape(-1, 3)
        self._fold(self._info.ordinal, "vertices", chunk)  # type: ignore[union-attr]
        self._vbuf = array("d")

    def _flush_triangles(self) -> None:
        if self._mode != _FOLD or not self._sig_list:
            return
        np = _numpy()
        idx = np.frombuffer(self._tbuf, dtype=np.int64).reshape(-1, 3)
        self._fold(self._info.ordinal, "triangles", (idx, self._sig_list))  # type: ignore[union-attr]
        self._tbuf = array("q")
        self._sig_list = []

    def _end_object(self) -> None:
        info = self._info
        if self._mode == _KEEP and info is not None:
            np = _numpy()
            info.vertices = np.frombuffer(self._vbuf, dtype=np.float64).reshape(-1, 3)
            info.tri_idx = np.frombuffer(self._tbuf, dtype=np.int64).reshape(-1, 3)
            info.tri_sig = np.frombuffer(self._sig_idx, dtype=np.int32)
        self._new_object(None)


def _stream_part(
    fh: Any,
    budget: _RetainedBudget,
    mode: str,
    fold: Any = None,
    *,
    want_vertices: bool = True,
    want_triangles: bool = True,
    refuse_entities: bool = False,
) -> tuple[ET.Element, dict[ET.Element, _MeshInfo]]:
    """Read one model part from the open member *fh* a chunk at a time.

    Returns the part's structure tree and, per object found in its first
    ``<resources>``, a :class:`_MeshInfo`.  *mode* is :data:`_KEEP`,
    :data:`_FOLD` (with *fold* called as ``fold(ordinal, kind, payload)`` —
    ``"vertices"`` with an (n, 3) float64 chunk, ``"triangles"`` with an
    ``(indices, signatures)`` chunk — which must not hold the chunk past the
    call) or :data:`_STRUCTURE`.  With *refuse_entities*, raises
    :class:`_EntityDeclarationRefused` on an ``<!ENTITY`` declaration, the
    refusal those readers made before parsing a whole member.  Raises
    ``ET.ParseError`` for malformed XML and
    :class:`~kiln.untrusted_zip.UntrustedArchiveError` past the structure
    budget.
    """
    builder = _PartBuilder(
        budget, mode, fold, want_vertices=want_vertices, want_triangles=want_triangles,
    )
    parser = ET.XMLParser(target=builder)
    carry = b""
    keep = len(b"<!ENTITY") - 1
    while True:
        chunk = fh.read(_STREAM_CHUNK_BYTES)
        if not chunk:
            break
        if refuse_entities:
            window = carry + chunk
            if _ENTITY_DECL_RE.search(window):
                raise _EntityDeclarationRefused("3MF XML declares entities")
            carry = window[-keep:]
        parser.feed(chunk)
    root = parser.close()
    return root, builder.meshes


def _member_tokens(zf: zipfile.ZipFile, member: Any, tokens: Sequence[bytes]) -> set[bytes]:
    """Which of *tokens* occur in archive *member*, read a chunk at a time.

    Always reads to the end, so a damaged member fails its CRC here exactly as
    a whole read would.
    """
    found: set[bytes] = set()
    keep = max(len(t) for t in tokens) - 1
    carry = b""
    with zf.open(member) as fh:
        while True:
            chunk = fh.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            window = carry + chunk
            for token in tokens:
                if token not in found and token in window:
                    found.add(token)
            carry = window[-keep:] if keep else b""
    return found


#: Per-triangle painting attributes.  BambuStudio and OrcaSlicer write
#: ``paint_color`` (un-namespaced); PrusaSlicer writes the namespaced
#: ``slic3rpe:mmu_segmentation`` — and each family's reader accepts the
#: other's spelling, so Kiln does too.
_PAINT_ATTRS = ("paint_color", f"{{{_SLIC3RPE_NS}}}mmu_segmentation")

#: Byte tokens whose absence proves the model XML carries no painting
#: attribute — what the pre-scan (:func:`_scan_color_constructs`) looks for
#: before spending an XML parse on a file.
_PAINT_BYTE_TOKENS = (b"paint_color", b"mmu_segmentation")


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

    Read from PrusaSlicer's own source; OrcaSlicer carries the identical
    encoding for states 0–16 (no 17–255 extended form) and a byte-identical
    string codec.

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


def _triangle_paint_attr(tri: Any) -> str | None:
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
        :func:`object_display_colors` keys by.  For an object reached
        through a ``p:path`` component, ``name`` is already the placing
        object's (see :class:`_PlacedObject`)."""
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


def _collect_object_triangles(
    obj_el: ET.Element,
    info: _MeshInfo | None,
    color_lookup: dict[int, list[tuple[int, int, int]]],
    *,
    default_color: tuple[int, int, int],
    fallback_color: tuple[int, int, int] | None = None,
) -> tuple[list[ColoredTriangle], set[int], int]:
    """Extract colored triangles from one ``<object>``'s kept mesh (*info*).

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
    if info is None or info.n_vertices == 0:
        return [], states_seen, split_faces

    vertices = [tuple(v) for v in info.vertices.tolist()]
    n = len(vertices)

    # Object-level color defaults: core spec first, then the sidecar
    obj_color = _resolve_color(
        color_lookup,
        obj_el.get("pid"),
        obj_el.get("pindex"),
        default=fallback_color if fallback_color is not None else default_color,
    )

    # Each distinct triangle signature resolves once; the per-triangle rule is
    # the tree reader's, applied to the attributes the stream kept.
    resolved = [
        _signature_color(sig, color_lookup, obj_color) for sig in info.signatures
    ]
    triangles: list[ColoredTriangle] = []
    for (a, b, c), s in zip(info.tri_idx.tolist(), info.tri_sig.tolist(), strict=True):
        if a < 0 or b < 0 or c < 0 or a >= n or b >= n or c >= n:
            continue
        color, paint_state, states, is_split = resolved[s]
        states_seen.update(states)
        if is_split:
            split_faces += 1
        triangles.append(ColoredTriangle(
            v0=vertices[a],
            v1=vertices[b],
            v2=vertices[c],
            color=color,
            paint_state=paint_state,
        ))

    return triangles, states_seen, split_faces


def _signature_color(
    sig: _TriangleSig,
    color_lookup: dict[int, list[tuple[int, int, int]]],
    obj_color: tuple[int, int, int] | None,
) -> tuple[tuple[int, int, int] | None, int | None, tuple[int, ...], bool]:
    """What a triangle with colour signature *sig* resolves to on an object
    whose own color is *obj_color*: ``(color, paint_state, painted_states,
    is_split)``.

    Strongest source first: a core-spec property reference (``p1`` used for
    the whole face), then the slicer painting attribute, then the object's
    own color.
    """
    tri_pid, tri_p1, paint = sig
    if tri_pid is not None:
        return (
            _resolve_color(color_lookup, tri_pid, tri_p1, default=obj_color),
            None, (), False,
        )
    decoded = _decode_paint_states(paint) if paint else None
    if decoded is None:
        return obj_color, None, (), False
    weights, is_split = decoded
    paint_state = _dominant_paint_state(weights)
    color = _paint_state_color(paint_state) if paint_state >= 1 else obj_color
    return color, paint_state, tuple(k for k in weights if k >= 1), is_split


def _find_model_xml(zf: zipfile.ZipFile) -> str:
    """Locate the 3D model XML inside the ZIP archive.

    Checks ``3D/3dmodel.model`` first, then falls back to scanning
    ``_rels/.rels`` for the primary model path, then to the first
    ``.model`` member in the archive.
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
                with zf.open(name) as fh:
                    rels_root, _ = _stream_part(fh, _RetainedBudget(), _STRUCTURE)
                rels_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
                for rel in rels_root.findall(f"{{{rels_ns}}}Relationship"):
                    target = rel.get("Target", "")
                    if target.lower().endswith(".model"):
                        # Strip leading '/' if present
                        return target.lstrip("/")
            except ET.ParseError:
                break

    # Last resort: an archive that names no root part but carries exactly
    # the model files — every reader used to keep this fallback on its own.
    for name in sorted(names):
        if name.lower().endswith(".model"):
            return name

    raise ValueError(
        f"No 3D model XML found in 3MF archive. "
        f"Expected '{standard_path}' or a .rels reference. "
        f"Archive contains: {names[:20]}"
    )


# ---------------------------------------------------------------------------
# Archive walk — every model part, every build placement
# ---------------------------------------------------------------------------
#
# The one reader of a 3MF's object graph.  Both the colour parser below and
# the geometry reader in ``kiln.generation.validation`` walk the archive
# through it, so a layout one of them understands the other cannot miss.

#: A 3MF ``transform`` attribute: twelve numbers, row-major ``m00 m01 m02
#: m10 m11 m12 m20 m21 m22`` followed by the translation ``m30 m31 m32``.
#: Points transform as row vectors, ``[x y z 1] * M``.
_Transform3mf = tuple[float, ...]
_IDENTITY_3MF: _Transform3mf = (
    1.0, 0.0, 0.0,
    0.0, 1.0, 0.0,
    0.0, 0.0, 1.0,
    0.0, 0.0, 0.0,
)

#: Millimetres per model unit — the ``unit`` attribute of ``<model>``.
_3MF_UNIT_TO_MM: dict[str, float] = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}


def _parse_3mf_transform(value: str | None, errors: list[str]) -> _Transform3mf:
    """Read a build-item / component ``transform`` attribute."""
    if not value or not value.strip():
        return _IDENTITY_3MF
    try:
        floats = tuple(float(p) for p in value.split())
    except ValueError:
        floats = ()
    if len(floats) != 12:
        errors.append(
            f"Malformed 3MF transform {value!r}: expected 12 numbers."
        )
        return _IDENTITY_3MF
    return floats


def _apply_3mf_transform(
    m: _Transform3mf, p: tuple[float, ...]
) -> tuple[float, float, float]:
    x, y, z = p
    return (
        x * m[0] + y * m[3] + z * m[6] + m[9],
        x * m[1] + y * m[4] + z * m[7] + m[10],
        x * m[2] + y * m[5] + z * m[8] + m[11],
    )


def _transform_columns(m: _Transform3mf, pts: Any) -> tuple[Any, Any, Any]:
    """:func:`_apply_3mf_transform` over an (n, 3) array, column by column.

    The same products and sums in the same order, one IEEE operation at a
    time, so every coordinate is bit-identical to the per-point function.
    """
    x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]
    return (
        x * m[0] + y * m[3] + z * m[6] + m[9],
        x * m[1] + y * m[4] + z * m[7] + m[10],
        x * m[2] + y * m[5] + z * m[8] + m[11],
    )


def _first_extreme(col: Any, want_max: bool) -> float | None:
    """What ``min()`` (or ``max()``) over *col* returns once its first item is
    a number: the first occurrence of the extreme of the non-NaN values (so
    the sign of a zero is the first one met).  ``None`` when there are none."""
    np = _numpy()
    valid = col[~np.isnan(col)]
    if not len(valid):
        return None
    extreme = valid.max() if want_max else valid.min()
    return float(col[int(np.argmax(col == extreme))])


def _placed_vertex_bounds(
    archive: _ModelArchive, root_transform: _Transform3mf,
) -> tuple[list[float], list[float]] | None:
    """``([x_min, y_min, z_min], [x_max, y_max, z_max])`` of every vertex the
    build places, transformed — exactly what ``min()`` / ``max()`` over the
    list of every placed, transformed vertex returned, without the list.

    Two passes: the structure says which object each placement shows and
    how; then each part's vertices stream past once, folded through every
    placement of their object, so memory is one chunk and a few numbers per
    placement, whatever the mesh.  Exact, not approximate: the transform is
    applied per vertex with the per-point arithmetic, and the per-placement
    results combine in build order with ``min()``'s own semantics — a NaN as
    the very first coordinate wins, a NaN anywhere else is passed over, and
    of equal extremes the first met is kept.  ``None`` when nothing is placed.
    """
    placements: list[tuple[str, int, _Transform3mf, int]] = []
    for placed in archive.placements(root_transform=root_transform):
        info = archive.mesh(placed)
        if info is not None:
            placements.append((placed.part, info.ordinal, placed.transform, info.n_vertices))
    if not any(n for _part, _ordinal, _t, n in placements):
        return None

    # Per placement: its first transformed vertex, and per axis the running
    # first-occurrence min and max of its non-NaN coordinates.
    first: list[tuple[float, float, float] | None] = [None] * len(placements)
    lows: list[list[float | None]] = [[None, None, None] for _ in placements]
    highs: list[list[float | None]] = [[None, None, None] for _ in placements]
    by_part: dict[str, dict[int, list[int]]] = {}
    for i, (part, ordinal, _t, _n) in enumerate(placements):
        by_part.setdefault(part, {}).setdefault(ordinal, []).append(i)

    def fold_into(indices: dict[int, list[int]]) -> Any:
        def fold(ordinal: int, kind: str, chunk: Any) -> None:
            if kind != "vertices":
                return
            for i in indices.get(ordinal, ()):
                cols = _transform_columns(placements[i][2], chunk)
                if first[i] is None:
                    first[i] = (float(cols[0][0]), float(cols[1][0]), float(cols[2][0]))
                for axis, col in enumerate(cols):
                    low = _first_extreme(col, want_max=False)
                    high = _first_extreme(col, want_max=True)
                    if low is not None and (lows[i][axis] is None or low < lows[i][axis]):
                        lows[i][axis] = low
                    if high is not None and (highs[i][axis] is None or high > highs[i][axis]):
                        highs[i][axis] = high
        return fold

    for part, indices in by_part.items():
        archive.fold(part, fold_into(indices), want_vertices=True)

    start = next(i for i, (_p, _o, _t, n) in enumerate(placements) if n)
    mins: list[float] = []
    maxs: list[float] = []
    for axis in range(3):
        head = first[start][axis]  # type: ignore[index]
        if math.isnan(head):
            mins.append(head)
            maxs.append(head)
            continue
        low = high = None
        for i in range(len(placements)):
            lo, hi = lows[i][axis], highs[i][axis]
            if lo is not None and (low is None or lo < low):
                low = lo
            if hi is not None and (high is None or hi > high):
                high = hi
        mins.append(low)  # type: ignore[arg-type]
        maxs.append(high)  # type: ignore[arg-type]
    return mins, maxs


def _compose_3mf_transforms(
    inner: _Transform3mf, outer: _Transform3mf
) -> _Transform3mf:
    """The transform that applies *inner* first, then *outer*.

    A component's own transform is *inner*; the transform of the object
    (or build item) that placed it is *outer*.
    """
    if inner is _IDENTITY_3MF:
        return outer
    if outer is _IDENTITY_3MF:
        return inner
    rotation = tuple(
        inner[3 * i + 0] * outer[3 * 0 + j]
        + inner[3 * i + 1] * outer[3 * 1 + j]
        + inner[3 * i + 2] * outer[3 * 2 + j]
        for i in range(3)
        for j in range(3)
    )
    translation = _apply_3mf_transform(outer, (inner[9], inner[10], inner[11]))
    return rotation + translation


@dataclass
class _ModelPart:
    """One parsed model part of the archive, its objects indexed by id.

    Object ids — and the ``pid`` palette ids the objects reference — are
    scoped to their part: two parts may both hold an object 1.
    """

    name: str
    model_el: ET.Element
    objects: dict[int, ET.Element]
    #: Each object's first mesh as the stream found it (see :class:`_MeshInfo`).
    meshes: dict[ET.Element, _MeshInfo] = field(default_factory=dict)

    @property
    def resources_el(self) -> ET.Element | None:
        return self.model_el.find(f"{{{_CORE_NS}}}resources")


@dataclass
class _PlacedObject:
    """An object the build reaches, with everything the walk learned on
    the way down to it."""

    part: str
    object_id: int
    element: ET.Element
    #: The name trimesh gives this object's Scene geometry: an object
    #: reached through a ``p:path`` component is named after the object
    #: holding the component (trimesh never reads the part's own object
    #: attributes); any other object carries its own ``name``.  ``None``
    #: when neither names it — the key then falls back to the id.
    name: str | None
    #: Build-item and component transforms composed down to this object,
    #: on top of whatever root transform the caller started the walk with.
    transform: _Transform3mf
    #: ``(part, object id)`` of every object that placed this one, the
    #: build item's object first and the immediate parent last.
    ancestry: tuple[tuple[str, int], ...]

    @property
    def key(self) -> str:
        return self.name or str(self.object_id)


class _ModelArchive:
    """The model parts of one open 3MF archive, loaded on first use.

    Trouble short of a corrupt root part — a part missing from the
    archive, a malformed transform — is reported through *errors* and the
    walk continues, so one bad reference costs one object, not the file.
    A part that failed to load is remembered so it is reported once.

    Parts are streamed (see "Streaming model parts"): with *keep_mesh* each
    object's mesh is kept as compact arrays (:meth:`mesh`); without it only
    the structure and the mesh's counts are kept, and a reader that needs the
    mesh folds it in a second pass (:meth:`fold`).  Every part one walk loads
    shares one structure budget.

    :raises ValueError: when the archive names no root model at all.
    """

    def __init__(
        self, zf: zipfile.ZipFile, errors: list[str], *, keep_mesh: bool = False,
    ) -> None:
        self._zf = zf
        self.errors = errors
        self._mode = _KEEP if keep_mesh else _STRUCTURE
        self._budget = _RetainedBudget()
        self.root_part = _find_model_xml(zf)
        self._names = {name.lower(): name for name in zf.namelist()}
        self._parts: dict[str, _ModelPart | None] = {}

    def part(self, name: str) -> _ModelPart | None:
        if name in self._parts:
            return self._parts[name]
        self._parts[name] = None
        try:
            fh = self._zf.open(name)
        except KeyError:
            self.errors.append(
                f"3MF references a model part missing from the archive: {name!r}"
            )
            return None
        try:
            with fh:
                model_el, meshes = _stream_part(fh, self._budget, self._mode)
        except ET.ParseError as exc:
            self.errors.append(f"Corrupt 3MF model XML in {name!r}: {exc}")
            return None
        objects: dict[int, ET.Element] = {}
        resources_el = model_el.find(f"{{{_CORE_NS}}}resources")
        if resources_el is not None:
            for obj_el in resources_el.findall(f"{{{_CORE_NS}}}object"):
                with contextlib.suppress(ValueError, TypeError):
                    objects[int(obj_el.get("id", ""))] = obj_el
        loaded = _ModelPart(name=name, model_el=model_el, objects=objects, meshes=meshes)
        self._parts[name] = loaded
        return loaded

    def mesh(self, placed: _PlacedObject) -> _MeshInfo | None:
        """The first mesh of *placed*'s object, or ``None`` if it has none."""
        part = self._parts.get(placed.part)
        info = None if part is None else part.meshes.get(placed.element)
        return info if info is not None and info.has_mesh else None

    def fold(
        self,
        name: str,
        fold: Any,
        *,
        want_vertices: bool = False,
        want_triangles: bool = False,
    ) -> None:
        """Stream the already-loaded part *name* again, handing its mesh to
        ``fold(ordinal, kind, payload)`` a chunk at a time (see
        :func:`_stream_part`) — the second pass of a reader whose memory must
        not grow with the mesh.  The same bytes parsed cleanly on the first
        pass, so this pass cannot fail on them differently.
        """
        with self._zf.open(name) as fh:
            _stream_part(
                fh, _RetainedBudget(), _FOLD, fold,
                want_vertices=want_vertices, want_triangles=want_triangles,
            )

    @property
    def root(self) -> _ModelPart | None:
        return self.part(self.root_part)

    def root_transform_mm(self) -> _Transform3mf | None:
        """The scale that takes the root model's ``unit`` to millimetres,
        as a transform to start :meth:`placements` from.

        ``None`` — reported through *errors* — for a unit the spec does
        not name, or when the root part failed to load.
        """
        root = self.root
        if root is None:
            return None
        unit = (root.model_el.get("unit") or "millimeter").strip().lower()
        scale = _3MF_UNIT_TO_MM.get(unit)
        if scale is None:
            self.errors.append(f"Unknown 3MF model unit {unit!r}.")
            return None
        if scale == 1.0:
            return _IDENTITY_3MF
        return (scale, 0.0, 0.0, 0.0, scale, 0.0, 0.0, 0.0, scale, 0.0, 0.0, 0.0)

    def resolve_part(self, el: ET.Element, current: str) -> str:
        """The model part *el* points at through ``p:path``, else *current*."""
        ref = el.get(_PATH_ATTR)
        if not ref:
            return current
        stripped = ref.lstrip("/")
        return self._names.get(stripped.lower(), stripped)

    def placements(
        self, *, root_transform: _Transform3mf = _IDENTITY_3MF
    ) -> Iterator[_PlacedObject]:
        """Every object the build reaches, depth-first in build order.

        An object placed twice is yielded twice — this is the plate as a
        slicer sees it.  A reader that describes objects rather than
        placements dedupes on ``(part, object_id)``.  Components that
        loop back onto an ancestor are cut there.
        """
        root = self.root
        if root is None:
            return
        build_el = root.model_el.find(f"{{{_CORE_NS}}}build")
        if build_el is not None:
            items = [
                (
                    self.resolve_part(item, self.root_part),
                    item.get("objectid", ""),
                    item.get("transform"),
                )
                for item in build_el.findall(f"{{{_CORE_NS}}}item")
            ]
        else:
            items = [(self.root_part, str(oid), None) for oid in root.objects]

        for part, object_id, transform_attr in items:
            try:
                root_id = int(object_id)
            except ValueError:
                continue
            placement = _parse_3mf_transform(transform_attr, self.errors)
            yield from self._walk(
                part,
                root_id,
                None,
                _compose_3mf_transforms(placement, root_transform),
                (),
            )

    def _walk(
        self,
        part: str,
        object_id: int,
        name: str | None,
        transform: _Transform3mf,
        ancestry: tuple[tuple[str, int], ...],
    ) -> Iterator[_PlacedObject]:
        loaded = self.part(part)
        if loaded is None:
            return
        obj_el = loaded.objects.get(object_id)
        if obj_el is None or (part, object_id) in ancestry:
            return
        if name is None:
            name = obj_el.get("name") or None

        yield _PlacedObject(
            part=part,
            object_id=object_id,
            element=obj_el,
            name=name,
            transform=transform,
            ancestry=ancestry,
        )

        components_el = obj_el.find(f"{{{_CORE_NS}}}components")
        if components_el is None:
            return
        child_ancestry = ancestry + ((part, object_id),)
        for comp in components_el.findall(f"{{{_CORE_NS}}}component"):
            try:
                child_id = int(comp.get("objectid", ""))
            except ValueError:
                continue
            child_part = self.resolve_part(comp, part)
            local = _parse_3mf_transform(comp.get("transform"), self.errors)
            yield from self._walk(
                child_part,
                child_id,
                # trimesh names a geometry behind ``p:path`` after the
                # object holding the component, not the part's object.
                (obj_el.get("name") or None) if child_part != part else None,
                _compose_3mf_transforms(local, transform),
                child_ancestry,
            )


def _model_part_members(zf: zipfile.ZipFile) -> list[str]:
    """Every ``.model`` member of the archive, the root part first."""
    try:
        root = _find_model_xml(zf)
    except ValueError:
        return []
    rest = sorted(
        n for n in zf.namelist() if n.lower().endswith(".model") and n != root
    )
    return [root] + rest


def _scan_color_constructs(zf: zipfile.ZipFile) -> tuple[bool, bool]:
    """Byte scan: ``(has_palette_or_sidecar_color, has_paint)``.

    Every 3MF headed for the stage passes through the colour readers, and
    an XML parse is real money on a large mesh — this turns away the files
    that carry no colour construct at all before one is spent.  It looks in
    every model part, not just the root: a painted MakerWorld model keeps
    its ``paint_color`` attributes in ``3D/Objects/object_N.model``.
    """
    has_palette = False
    has_paint = False
    for member in _model_part_members(zf):
        # A chunk at a time (:func:`_member_tokens`): a whole read of a large
        # part held ~2-3x its XML just to look for a word.
        try:
            found = _member_tokens(zf, member, _SCAN_TOKENS)
        except KeyError:
            continue
        if b"colorgroup" in found or b"basematerials" in found:
            has_palette = True
        if any(token in found for token in _PAINT_BYTE_TOKENS):
            has_paint = True
        if has_palette and has_paint:
            break
    if not has_palette:
        for member in zf.namelist():
            if member.lower() == _SLICER_SETTINGS_PATH.lower():
                has_palette = b'key="color"' in _member_tokens(zf, member, (b'key="color"',))
                break
    return has_palette, has_paint


#: What :func:`_scan_color_constructs` looks for in a model part.
_SCAN_TOKENS = (b"colorgroup", b"basematerials", *_PAINT_BYTE_TOKENS)


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
    # Streamed under its own structure budget, refusing <!ENTITY> declarations
    # (the same hardening as the SVG path in mark_geometry: stdlib ElementTree
    # refuses external entities, and billion-laughs needs <!ENTITY>
    # declarations no legitimate settings file carries).  Any of those, or a
    # settings file too large in structure to be real, contributes no color.
    try:
        with zf.open(name) as fh:
            root, _ = _stream_part(fh, _RetainedBudget(), _STRUCTURE, refuse_entities=True)
    except (KeyError, ET.ParseError, _EntityDeclarationRefused, UntrustedArchiveError):
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


def _sidecar_color_for(
    placed: _PlacedObject, sidecar_colors: dict[int, tuple[int, int, int]]
) -> tuple[int, int, int] | None:
    """The sidecar colour that applies to *placed*: its own id first, then
    the nearest assembly that placed it.

    The BambuStudio sidecar keys by the ROOT object — the assembly whose
    component points into the part — so a mesh object inherits the colour
    written against the object that placed it.
    """
    own = sidecar_colors.get(placed.object_id)
    if own is not None:
        return own
    for _part, ancestor_id in reversed(placed.ancestry):
        color = sidecar_colors.get(ancestor_id)
        if color is not None:
            return color
    return None


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

    Every object the build reaches is read, including those in other
    model parts of the archive (the production extension's ``p:path``,
    which is where a BambuStudio / OrcaSlicer project keeps each mesh).
    Object ids and palettes resolve inside the part that made the
    reference.  Triangles stay in file coordinates — build-item and
    component transforms are not applied — and an object placed twice
    contributes one segment.

    :param file_path: Path to a ``.3mf`` ZIP file.
    :param default_color: RGB tuple used when a triangle has no color.
    :returns: A :class:`ColoredMesh` with all triangles, color metadata,
        and per-object :class:`ObjectSegment` boundaries.
    :raises ValueError: If the archive has no model XML or the root model
        XML is corrupt.  A missing or corrupt ``p:path`` part costs its
        objects, not the file.  An archive :mod:`kiln.untrusted_zip` refuses
        (or one whose model structure exceeds the walk's budget) raises
        :class:`~kiln.untrusted_zip.UntrustedArchiveError`, which is both a
        ``ValueError`` and a ``zipfile.BadZipFile``.
    :raises zipfile.BadZipFile: If *file_path* is not a valid ZIP.
    :raises FileNotFoundError: If *file_path* does not exist.
    """
    all_triangles: list[ColoredTriangle] = []
    segments: list[ObjectSegment] = []
    states_seen: set[int] = set()
    split_count = 0
    #: Palette lookups per model part — ``pid`` ids are scoped to the part.
    lookups: dict[str, dict[int, list[tuple[int, int, int]]]] = {}
    seen: set[tuple[str, int]] = set()

    with open_untrusted_zip(file_path) as zf:
        errors: list[str] = []
        archive = _ModelArchive(zf, errors, keep_mesh=True)
        sidecar_colors = _slicer_config_colors(zf)
        if archive.root is None:
            raise ValueError(
                f"Failed to parse 3MF model XML in '{file_path}': "
                + "; ".join(errors)
            )

        for placed in archive.placements():
            # An object placed twice is ONE object — one segment, in file
            # coordinates.  Build transforms are the geometry reader's
            # business; a consumer lining this soup up with trimesh's
            # Scene applies the node transform itself.
            if (placed.part, placed.object_id) in seen:
                continue
            seen.add((placed.part, placed.object_id))

            lookup = lookups.get(placed.part)
            if lookup is None:
                part = archive.part(placed.part)
                resources_el = part.resources_el if part is not None else None
                lookup = (
                    _build_color_lookup(resources_el)
                    if resources_el is not None else {}
                )
                lookups[placed.part] = lookup

            tris, states, splits = _collect_object_triangles(
                placed.element,
                archive.mesh(placed),
                lookup,
                default_color=default_color,
                fallback_color=_sidecar_color_for(placed, sidecar_colors),
            )
            if tris:
                segments.append(ObjectSegment(
                    object_id=placed.object_id,
                    name=placed.name,
                    start=len(all_triangles),
                    count=len(tris),
                ))
            all_triangles.extend(tris)
            states_seen.update(states)
            split_count += splits

    # Compute color metadata
    distinct_colors = {t.color for t in all_triangles}
    has_non_default = any(c != default_color for c in distinct_colors)
    colors_found = (
        any(lookups.values()) or bool(sidecar_colors) or bool(states_seen)
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
    case: a CAD assembly legitimately holds four bolts all called "M3x8", and
    a file can arrive with two bodies nobody named at all.

    Uniqueness is guaranteed against the key the reader actually builds
    (``name`` if non-empty, else the object id), by never returning a blank
    name: a name that is empty or all whitespace becomes ``Part N`` at its
    1-based position.  This is the ONE place an unnamed part gets named — the
    label is read by a person in a slicer's object list, so it is written the
    way a person writes one rather than as an identifier.  Repeats then take a
    ``" (2)"``, ``" (3)"`` … suffix, the disambiguation every file browser and
    CAD tree already uses.

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
        filled.append(name if name.strip() else f"Part {i + 1}")

    # Every name in the list is spoken for from the start, so a suffix can
    # never collide with a name that appears LATER (the "A (2)" case above).
    taken = set(filled)
    seen: set[str] = set()
    # Where to resume the search per name.  A suffix already handed out is in
    # `seen` forever, so restarting the scan at 2 each time would re-walk the
    # whole run — quadratic on exactly the input this exists for, a thousand
    # identical fasteners in one assembly.
    resume: dict[str, int] = {}
    out: list[str] = []
    for name in filled:
        if name not in seen:
            seen.add(name)
            out.append(name)
            continue
        n = resume.get(name, 2)
        while f"{name} ({n})" in taken or f"{name} ({n})" in seen:
            n += 1
        resume[name] = n + 1
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

    Objects in other model parts (``p:path``) are read like any other, keyed
    the way trimesh keys them — after the object holding the component.

    Never raises: color is enrichment (the caller keeps its mesh either way),
    so any archive trouble reads as ``{}``.
    """
    try:
        with open_untrusted_zip(file_path) as zf:
            # A byte scan over every model part turns away the files that
            # carry no color construct at all before an XML parse is spent
            # on a large mesh.  Painting attributes count (a painted object
            # must be SEEN to be refused, and a single-filament paint is
            # honestly uniform); sidecar-only colors are scanned for too.
            has_palette, has_paint = _scan_color_constructs(zf)
            if not has_palette and not has_paint:
                return {}
            sidecar_colors = _slicer_config_colors(zf)
            archive = _ModelArchive(zf, [])
            # The plate's structure first — which objects are placed, each
            # one's base color and palette — then each part's triangles folded
            # into a per-object color set that stops growing once a second
            # color proves the object painted: memory follows the number of
            # objects, never the number of triangles.
            plan = _placed_color_plan(archive, sidecar_colors, base_default=None)
            effective = _fold_display_colors(archive, plan)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return {}
    out: dict[str, tuple[int, int, int]] = {}
    for entry in plan:
        if entry.placed.key in out:
            return {}  # duplicate names — refuse to guess which part is which
        colors = effective[(entry.placed.part, entry.info.ordinal)]
        if len(colors) > 1:
            continue  # painted: no single color can honestly stand for it
        color = next(iter(colors)) if colors else entry.base
        if color is not None:
            out[entry.placed.key] = color
    return out


#: Distinct triangle signatures one object remembers how it resolved;
#: past this a signature is resolved again rather than remembered, so a
#: file of unique signatures cannot grow the memo.
_SIG_MEMO_LIMIT = 4096


@dataclass
class _PlanEntry:
    """One object the build places, deduplicated, with what its colors need."""

    placed: _PlacedObject
    info: _MeshInfo
    lookup: dict[int, list[tuple[int, int, int]]]
    #: The object's own color: core-spec ``pid``/``pindex``, else the slicer
    #: sidecar, else *base_default* (see :func:`_placed_color_plan`).
    base: tuple[int, int, int] | None


def _placed_color_plan(
    archive: _ModelArchive,
    sidecar_colors: dict[int, tuple[int, int, int]],
    *,
    base_default: tuple[int, int, int] | None,
    lookups: dict[str, dict[int, list[tuple[int, int, int]]]] | None = None,
) -> list[_PlanEntry]:
    """Every object the build places, once each, in build order, that has a
    mesh — with its part's palette and its own color.

    The walk, the deduplication (an object placed twice is ONE object, colored
    by its first placement), the palette per part and the base color are the
    full colour parse's; *lookups*, when given, receives the palette of every
    part a placed object (meshed or not) lives in, as that parse builds them.
    """
    if lookups is None:
        lookups = {}
    plan: list[_PlanEntry] = []
    seen: set[tuple[str, int]] = set()
    for placed in archive.placements():
        if (placed.part, placed.object_id) in seen:
            continue
        seen.add((placed.part, placed.object_id))
        lookup = lookups.get(placed.part)
        if lookup is None:
            part = archive.part(placed.part)
            resources_el = part.resources_el if part is not None else None
            lookup = (
                _build_color_lookup(resources_el)
                if resources_el is not None else {}
            )
            lookups[placed.part] = lookup
        info = archive.mesh(placed)
        if info is None:
            continue
        sidecar = _sidecar_color_for(placed, sidecar_colors)
        obj_el = placed.element
        if base_default is None:
            # Display colors: the object's own color, else the sidecar.
            base = _resolve_color(
                lookup, obj_el.get("pid"), obj_el.get("pindex"), default=None,
            )
            if base is None:
                base = sidecar
        else:
            # The colour parse: own color, else the sidecar, else the default.
            base = _resolve_color(
                lookup, obj_el.get("pid"), obj_el.get("pindex"),
                default=sidecar if sidecar is not None else base_default,
            )
        plan.append(_PlanEntry(placed=placed, info=info, lookup=lookup, base=base))
    return plan


def _display_contribution(
    sig: _TriangleSig,
    lookup: dict[int, list[tuple[int, int, int]]],
    obj_pid: str | None,
    base: tuple[int, int, int] | None,
) -> tuple[tuple[int, int, int] | None, ...]:
    """The colors one triangle adds to its object's display set.

    Per-triangle property references resolve against the object's own
    color; painting attributes contribute every leaf state's palette color
    (state 0 = the base); anything else is the base.
    """
    tri_pid, tri_p1, paint = sig
    if tri_pid is not None or tri_p1 is not None:
        return (_resolve_color(
            lookup, tri_pid if tri_pid is not None else obj_pid, tri_p1, default=base,
        ),)
    decoded = _decode_paint_states(paint) if paint else None
    if decoded is None:
        return (base,)
    weights, _is_split = decoded
    return tuple(
        base if state == 0 else _paint_state_color(state) for state in weights
    )


def _fold_display_colors(
    archive: _ModelArchive, plan: list[_PlanEntry],
) -> dict[tuple[str, int], set[tuple[int, int, int] | None]]:
    """Each planned object's display color set, folded from its triangles.

    Every triangle of the first triangle block counts (the display reader
    never range-checked indices).  A set stops growing at two colors — the
    object is then painted, which is all :func:`object_display_colors` needs
    to know.
    """
    sets: dict[tuple[str, int], set[tuple[int, int, int] | None]] = {}
    by_part: dict[str, dict[int, _PlanEntry]] = {}
    for entry in plan:
        sets[(entry.placed.part, entry.info.ordinal)] = set()
        by_part.setdefault(entry.placed.part, {})[entry.info.ordinal] = entry
    for part_name, entries in by_part.items():
        memos: dict[int, dict[_TriangleSig, tuple[tuple[int, int, int] | None, ...]]] = {}

        def fold(ordinal: int, kind: str, payload: Any, entries=entries, part_name=part_name, memos=memos) -> None:
            entry = entries.get(ordinal)
            if entry is None or kind != "triangles":
                return
            colors = sets[(part_name, ordinal)]
            if len(colors) > 1:
                return
            memo = memos.setdefault(ordinal, {})
            obj_pid = entry.placed.element.get("pid")
            for sig in payload[1]:
                contribution = memo.get(sig)
                if contribution is None:
                    contribution = _display_contribution(sig, entry.lookup, obj_pid, entry.base)
                    if len(memo) < _SIG_MEMO_LIMIT:
                        memo[sig] = contribution
                colors.update(contribution)
                if len(colors) > 1:
                    return

        archive.fold(part_name, fold, want_triangles=True)
    return sets


def summarize_3mf_colors(
    file_path: str,
    *,
    default_color: tuple[int, int, int] = _DEFAULT_COLOR,
) -> tuple[bool, int]:
    """``(colors_found, color_count)`` exactly as :func:`parse_colored_3mf`
    reports them, read without holding the mesh.

    For a caller that needs only the count (a quote saying how many colors a
    file carries): the plate's structure is read first, then each part's
    triangles are folded into a set of the colors they resolve to, so memory
    grows with the number of distinct colors, never with the number of
    triangles.  Raises what :func:`parse_colored_3mf` raises for an archive it
    cannot read.
    """
    with open_untrusted_zip(file_path) as zf:
        errors: list[str] = []
        archive = _ModelArchive(zf, errors)
        sidecar_colors = _slicer_config_colors(zf)
        if archive.root is None:
            raise ValueError(
                f"Failed to parse 3MF model XML in '{file_path}': "
                + "; ".join(errors)
            )
        lookups: dict[str, dict[int, list[tuple[int, int, int]]]] = {}
        plan = _placed_color_plan(
            archive, sidecar_colors, base_default=default_color, lookups=lookups,
        )
        colors: set[tuple[int, int, int] | None] = set()
        states_seen: set[int] = set()
        by_part: dict[str, dict[int, _PlanEntry]] = {}
        for entry in plan:
            if entry.info.n_vertices:
                by_part.setdefault(entry.placed.part, {})[entry.info.ordinal] = entry
        for part_name, entries in by_part.items():
            memos: dict[int, dict[_TriangleSig, Any]] = {}

            def fold(ordinal: int, kind: str, payload: Any, entries=entries, memos=memos) -> None:
                entry = entries.get(ordinal)
                if entry is None or kind != "triangles":
                    return
                n = entry.info.n_vertices
                memo = memos.setdefault(ordinal, {})
                idx, sigs = payload
                for (a, b, c), sig in zip(idx.tolist(), sigs, strict=True):
                    if a < 0 or b < 0 or c < 0 or a >= n or b >= n or c >= n:
                        continue
                    resolved = memo.get(sig)
                    if resolved is None:
                        resolved = _signature_color(sig, entry.lookup, entry.base)
                        if len(memo) < _SIG_MEMO_LIMIT:
                            memo[sig] = resolved
                    colors.add(resolved[0])
                    states_seen.update(resolved[2])

            archive.fold(part_name, fold, want_triangles=True)
    has_non_default = any(c != default_color for c in colors)
    found = (
        any(lookups.values()) or bool(sidecar_colors) or bool(states_seen)
    ) and has_non_default
    return found, len(colors)
