"""Mesh validation pipeline for generated 3D models.

Validates STL, OBJ, and GLB files for 3D-printing readiness: parseable
geometry, reasonable dimensions, manifold checks, and polygon counts.
Uses only the Python standard library (``struct`` + ``json`` for binary
STL/GLB parsing) — no external mesh libraries required.
"""

from __future__ import annotations

import contextlib
import json as _json
import logging
import math
import re
import struct
import zipfile
from pathlib import Path
from typing import Any

from kiln.generation.base import MeshAnalysis, MeshValidationResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_TRIANGLES = 10_000_000
_WARN_TRIANGLES = 2_000_000
_MAX_DIMENSION_MM = 1000.0
_MIN_DIMENSION_MM = 0.1
_STL_HEADER_SIZE = 80
_STL_COUNT_SIZE = 4
_STL_TRIANGLE_SIZE = 50  # 12 floats (normal + 3 vertices) + 2 byte attr

# GLB (binary glTF 2.0) constants
_GLB_MAGIC = 0x46546C67  # "glTF" in little-endian
_GLB_JSON_CHUNK = 0x4E4F534A  # "JSON"
_GLB_BIN_CHUNK = 0x004E4942  # "BIN\0"
# componentType → (struct format, byte size)
_COMPONENT_FMT: dict[int, tuple[str, int]] = {
    5120: ("b", 1),  # BYTE
    5121: ("B", 1),  # UNSIGNED_BYTE
    5122: ("h", 2),  # SHORT
    5123: ("H", 2),  # UNSIGNED_SHORT
    5125: ("I", 4),  # UNSIGNED_INT
    5126: ("f", 4),  # FLOAT
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def convert_to_stl(input_path: str, output_path: str | None = None) -> str:
    """Convert an OBJ or GLB file to binary STL.

    Parses the input geometry and writes a binary STL with the same
    triangles.  Quads and higher polygons are triangulated.  Only
    geometry is preserved — textures, normals, and materials are
    discarded (not needed for 3D printing).

    Args:
        input_path: Path to the input file (``.obj`` or ``.glb``).
        output_path: Path for the output STL file.  Defaults to
            replacing the input extension with ``.stl``.

    Returns:
        The path to the written STL file.

    Raises:
        ValueError: If the input format is unsupported or has no geometry.
    """
    path = Path(input_path)
    ext = path.suffix.lower()

    if ext == ".glb":
        return _convert_glb_to_stl(path, output_path)
    if ext == ".obj":
        errors: list[str] = []
        triangles, vertices = _parse_obj(path, errors)
        if errors:
            raise ValueError(f"Failed to parse OBJ: {'; '.join(errors)}")
        if not triangles:
            raise ValueError("OBJ file contains no geometry to convert.")
        if output_path is None:
            output_path = str(path.with_suffix(".stl"))
        _write_binary_stl(triangles, output_path)
        return output_path

    raise ValueError(
        f"convert_to_stl expects .obj or .glb input, got {ext!r}"
    )


def validate_mesh(file_path: str) -> MeshValidationResult:
    """Validate an STL or OBJ file for 3D printing readiness.

    Checks performed:

    1. File exists and is non-empty.
    2. Extension is ``.stl`` or ``.obj``.
    3. Geometry is parseable (binary/ASCII STL or OBJ).
    4. Triangle count is within printable range.
    5. Bounding box dimensions are reasonable.
    6. Basic manifold (watertight) check via edge analysis.

    Args:
        file_path: Path to the mesh file.

    Returns:
        :class:`MeshValidationResult` with errors, warnings, and metrics.
    """
    errors: list[str] = []
    warnings: list[str] = []

    path = Path(file_path)

    # --- existence / size ---
    if not path.is_file():
        return MeshValidationResult(
            valid=False,
            errors=[f"File not found: {file_path}"],
        )

    size = path.stat().st_size
    if size == 0:
        return MeshValidationResult(
            valid=False,
            errors=["File is empty (0 bytes)."],
        )

    ext = path.suffix.lower()
    if ext not in (".stl", ".obj", ".glb"):
        return MeshValidationResult(
            valid=False,
            errors=[f"Unsupported file type: {ext!r}.  Expected .stl, .obj, or .glb."],
        )

    # --- parse geometry ---
    try:
        if ext == ".stl":
            triangles, vertices = _parse_stl(path, errors)
        elif ext == ".glb":
            triangles, vertices = _parse_glb(path, errors)
        else:
            triangles, vertices = _parse_obj(path, errors)
    except Exception as exc:
        return MeshValidationResult(
            valid=False,
            errors=[f"Failed to parse mesh: {exc}"],
        )

    if errors:
        return MeshValidationResult(valid=False, errors=errors)

    tri_count = len(triangles)
    vert_count = len(vertices)

    # --- triangle count ---
    if tri_count == 0:
        errors.append("Mesh contains zero triangles.")
        return MeshValidationResult(valid=False, errors=errors)

    if tri_count > _MAX_TRIANGLES:
        errors.append(
            f"Triangle count ({tri_count:,}) exceeds maximum ({_MAX_TRIANGLES:,}).  Model is too complex for slicing."
        )

    if tri_count > _WARN_TRIANGLES and tri_count <= _MAX_TRIANGLES:
        warnings.append(f"High triangle count ({tri_count:,}).  Slicing may be slow.")

    # --- bounding box ---
    bbox = _bounding_box(vertices)
    dims = {
        "x": bbox["x_max"] - bbox["x_min"],
        "y": bbox["y_max"] - bbox["y_min"],
        "z": bbox["z_max"] - bbox["z_min"],
    }

    for axis, size_mm in dims.items():
        if size_mm > _MAX_DIMENSION_MM:
            warnings.append(
                f"{axis.upper()}-axis dimension ({size_mm:.1f} mm) exceeds "
                f"{_MAX_DIMENSION_MM} mm.  Model may be too large to print."
            )
        if size_mm < _MIN_DIMENSION_MM:
            warnings.append(
                f"{axis.upper()}-axis dimension ({size_mm:.4f} mm) is below "
                f"{_MIN_DIMENSION_MM} mm.  Model may be too small to print."
            )

    # --- manifold check ---
    is_manifold = _check_manifold(triangles, warnings)

    valid = len(errors) == 0

    return MeshValidationResult(
        valid=valid,
        errors=errors,
        warnings=warnings,
        triangle_count=tri_count,
        vertex_count=vert_count,
        is_manifold=is_manifold,
        bounding_box=bbox,
    )


# ---------------------------------------------------------------------------
# STL parsing
# ---------------------------------------------------------------------------


def _parse_stl(
    path: Path,
    errors: list[str],
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]]]:
    """Parse a binary or ASCII STL file.

    Returns:
        (triangles, unique_vertices) where each triangle is a tuple of
        three (x, y, z) vertex tuples.
    """
    with open(path, "rb") as fh:
        header = fh.read(_STL_HEADER_SIZE)

    # Heuristic: ASCII STL starts with "solid" followed by a name.
    # Binary STL has an 80-byte header that *may* also start with "solid".
    # Check if file size matches the binary formula.
    file_size = path.stat().st_size

    is_ascii = False
    if header[:5] == b"solid":
        # Check binary formula: 80 + 4 + 50*n
        with open(path, "rb") as fh:
            fh.seek(_STL_HEADER_SIZE)
            count_bytes = fh.read(_STL_COUNT_SIZE)
            if len(count_bytes) == _STL_COUNT_SIZE:
                tri_count = struct.unpack("<I", count_bytes)[0]
                expected = _STL_HEADER_SIZE + _STL_COUNT_SIZE + _STL_TRIANGLE_SIZE * tri_count
                if file_size != expected:
                    is_ascii = True
            else:
                is_ascii = True

    if is_ascii:
        return _parse_stl_ascii(path, errors)
    return _parse_stl_binary(path, errors)


def _parse_stl_binary(
    path: Path,
    errors: list[str],
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]]]:
    """Parse a binary STL file."""
    with open(path, "rb") as fh:
        fh.read(_STL_HEADER_SIZE)  # skip header
        count_bytes = fh.read(_STL_COUNT_SIZE)
        if len(count_bytes) < _STL_COUNT_SIZE:
            errors.append("Binary STL file is truncated (missing triangle count).")
            return [], []

        tri_count = struct.unpack("<I", count_bytes)[0]
        expected_size = _STL_HEADER_SIZE + _STL_COUNT_SIZE + _STL_TRIANGLE_SIZE * tri_count
        actual_size = path.stat().st_size
        if actual_size < expected_size:
            errors.append(
                f"Binary STL truncated: header says {tri_count} triangles "
                f"({expected_size} bytes) but file is {actual_size} bytes."
            )
            return [], []

        triangles = []
        vertex_set: set[tuple[float, ...]] = set()

        for _ in range(tri_count):
            data = fh.read(_STL_TRIANGLE_SIZE)
            if len(data) < _STL_TRIANGLE_SIZE:
                break
            floats = struct.unpack("<12f", data[:48])
            # Skip normal (first 3 floats), take 3 vertices (9 floats).
            v1 = (floats[3], floats[4], floats[5])
            v2 = (floats[6], floats[7], floats[8])
            v3 = (floats[9], floats[10], floats[11])
            triangles.append((v1, v2, v3))
            vertex_set.add(v1)
            vertex_set.add(v2)
            vertex_set.add(v3)

    return triangles, list(vertex_set)


def _parse_stl_ascii(
    path: Path,
    errors: list[str],
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]]]:
    """Parse an ASCII STL file."""
    triangles = []
    vertex_set: set[tuple[float, ...]] = set()
    current_verts: list[tuple[float, ...]] = []

    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("vertex"):
                    parts = stripped.split()
                    if len(parts) >= 4:
                        try:
                            v = (float(parts[1]), float(parts[2]), float(parts[3]))
                            current_verts.append(v)
                            vertex_set.add(v)
                        except ValueError:
                            pass
                elif stripped.startswith("endfacet"):
                    if len(current_verts) == 3:
                        triangles.append(tuple(current_verts))
                    current_verts = []
    except Exception as exc:
        errors.append(f"Could not read ASCII STL: {exc}")

    return triangles, list(vertex_set)


def _compute_bounding_box(
    vertices: list[tuple[float, ...]],
) -> dict[str, Any] | None:
    """Compute axis-aligned bounding box from a list of vertex tuples.

    Returns a dict with min/max per axis and dimension sizes, or ``None``
    if *vertices* is empty.
    """
    if not vertices:
        return None
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    z_min, z_max = min(zs), max(zs)
    return {
        "x_min": round(x_min, 3),
        "x_max": round(x_max, 3),
        "y_min": round(y_min, 3),
        "y_max": round(y_max, 3),
        "z_min": round(z_min, 3),
        "z_max": round(z_max, 3),
        "dimensions_mm": {
            "x": round(x_max - x_min, 2),
            "y": round(y_max - y_min, 2),
            "z": round(z_max - z_min, 2),
        },
    }


# ---------------------------------------------------------------------------
# OBJ parsing
# ---------------------------------------------------------------------------


def _parse_obj(
    path: Path,
    errors: list[str],
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]]]:
    """Parse a Wavefront OBJ file (vertices and faces only)."""
    vertices: list[tuple[float, ...]] = []
    triangles: list[tuple[tuple[float, ...], ...]] = []

    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("v "):
                    parts = stripped.split()
                    if len(parts) >= 4:
                        with contextlib.suppress(ValueError):
                            vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                elif stripped.startswith("f "):
                    parts = stripped.split()[1:]
                    # OBJ face indices are 1-based, may contain v/vt/vn.
                    indices = []
                    for p in parts:
                        try:
                            raw_idx = int(p.split("/")[0])
                            # OBJ supports negative indices as back-references
                            if raw_idx < 0:
                                idx = len(vertices) + raw_idx
                            else:
                                idx = raw_idx - 1
                            indices.append(idx)
                        except (ValueError, IndexError):
                            pass
                    # Triangulate quads and higher polygons.
                    if len(indices) >= 3:
                        for i in range(1, len(indices) - 1):
                            i0, i1, i2 = indices[0], indices[i], indices[i + 1]
                            if 0 <= i0 < len(vertices) and 0 <= i1 < len(vertices) and 0 <= i2 < len(vertices):
                                triangles.append((vertices[i0], vertices[i1], vertices[i2]))
    except Exception as exc:
        errors.append(f"Could not read OBJ file: {exc}")

    return triangles, vertices


# ---------------------------------------------------------------------------
# Geometry analysis
# ---------------------------------------------------------------------------


def _bounding_box(vertices: list[tuple[float, ...]]) -> dict[str, float]:
    """Compute axis-aligned bounding box from vertex list."""
    if not vertices:
        return {
            "x_min": 0.0,
            "x_max": 0.0,
            "y_min": 0.0,
            "y_max": 0.0,
            "z_min": 0.0,
            "z_max": 0.0,
        }

    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]

    return {
        "x_min": min(xs),
        "x_max": max(xs),
        "y_min": min(ys),
        "y_max": max(ys),
        "z_min": min(zs),
        "z_max": max(zs),
    }


def _edge_census(
    triangles: list[tuple[tuple[float, ...], ...]],
    *,
    weld_tolerance: float = 0.0,
) -> dict[str, int]:
    """Count how many triangles meet along each edge, split by defect class.

    A closed surface has every edge shared by exactly two triangles.  The two
    ways that fails are not the same problem and do not have the same fix:

    * **boundary** — an edge on exactly one triangle.  The surface is open
      there: a hole.  ``repair_stl_advanced`` can sew small ones shut.
    * **pinch** — an edge on three or more triangles.  Two sheets of surface
      meet along a line instead of enclosing a volume, so there is no hole to
      sew and no repair pass here handles it; the geometry has to be built
      differently upstream.

    *weld_tolerance* snaps coordinates to a grid of that size before counting,
    so a mesh whose shared vertices differ only in the last float bit is not
    reported as riddled with holes.  Zero (the default) compares exactly,
    which is what every caller has always done.

    Returns:
        Counts keyed ``total``, ``manifold``, ``boundary``, ``pinch``.
    """
    if weld_tolerance > 0.0:
        def key(v: tuple[float, ...]) -> tuple[float, ...]:
            return tuple(round(c / weld_tolerance) * weld_tolerance for c in v[:3])
    else:
        def key(v: tuple[float, ...]) -> tuple[float, ...]:
            return tuple(v[:3])

    edge_count: dict[tuple[tuple[float, ...], tuple[float, ...]], int] = {}
    for tri in triangles:
        welded = [key(v) for v in tri]
        for i in range(3):
            v_a, v_b = welded[i], welded[(i + 1) % 3]
            if v_a == v_b:
                continue  # Collapsed edge of a degenerate triangle.
            # Canonical edge order for undirected comparison.
            edge = (min(v_a, v_b), max(v_a, v_b))
            edge_count[edge] = edge_count.get(edge, 0) + 1

    counts = edge_count.values()
    return {
        "total": len(edge_count),
        "manifold": sum(1 for c in counts if c == 2),
        "boundary": sum(1 for c in counts if c == 1),
        "pinch": sum(1 for c in counts if c >= 3),
    }


def _check_manifold(
    triangles: list[tuple[tuple[float, ...], ...]],
    warnings: list[str],
) -> bool:
    """Check if the mesh is manifold (watertight).

    A manifold mesh has every edge shared by exactly two triangles.  Counts
    edges through :func:`_edge_census` in O(n) time and names which of the two
    defect classes it found, because a hole and a pinch call for different
    fixes.

    Returns:
        True if manifold, False otherwise (with a warning appended).
    """
    if not triangles:
        return False

    census = _edge_census(triangles)
    if census["boundary"] == 0 and census["pinch"] == 0:
        return True

    parts: list[str] = []
    if census["boundary"]:
        parts.append(
            f"{census['boundary']:,} open edges (holes in the surface)"
        )
    if census["pinch"]:
        parts.append(
            f"{census['pinch']:,} pinched edges (3+ triangles meet along one "
            f"line)"
        )
    warnings.append(
        f"Mesh is not manifold (watertight): {' and '.join(parts)}.  Most "
        f"slicers can handle this, but print quality may be affected."
    )
    return False


# ---------------------------------------------------------------------------
# STL writing
# ---------------------------------------------------------------------------


def _write_binary_stl(
    triangles: list[tuple[tuple[float, ...], ...]],
    output_path: str,
) -> None:
    """Write triangles to a binary STL file.

    Each triangle is a tuple of three ``(x, y, z)`` vertex tuples.
    A real unit facet normal (computed from vertex winding) is written for
    every facet.  PrusaSlicer's binary-STL loader rejects an all-zero-normal
    mesh ("Loading of a model file failed"), so we cannot rely on the old
    "slicers recompute from winding" assumption.
    """
    with open(output_path, "wb") as fh:
        # 80-byte header — Kiln attribution stamp.
        header = b"Created by Kiln | kiln3d.com"
        fh.write(header.ljust(_STL_HEADER_SIZE, b"\x00"))
        # Triangle count as uint32 LE.
        fh.write(struct.pack("<I", len(triangles)))

        for tri in triangles:
            # Facet normal from vertex winding (right-hand rule): n = (b-a) x (c-a).
            # We write a real unit normal instead of (0, 0, 0) because
            # PrusaSlicer's binary-STL loader rejects an all-zero-normal mesh
            # ("Loading of a model file failed") — even though many slicers
            # would recompute it from winding.
            ax, ay, az = tri[0][0], tri[0][1], tri[0][2]
            bx, by, bz = tri[1][0], tri[1][1], tri[1][2]
            cx, cy, cz = tri[2][0], tri[2][1], tri[2][2]
            nx = (by - ay) * (cz - az) - (bz - az) * (cy - ay)
            ny = (bz - az) * (cx - ax) - (bx - ax) * (cz - az)
            nz = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            length = (nx * nx + ny * ny + nz * nz) ** 0.5
            if length > 0.0:
                nx, ny, nz = nx / length, ny / length, nz / length
            fh.write(struct.pack("<3f", nx, ny, nz))
            # Three vertices.
            for v in tri:
                fh.write(struct.pack("<3f", v[0], v[1], v[2]))
            # Attribute byte count (unused, must be 0).
            fh.write(struct.pack("<H", 0))


# ---------------------------------------------------------------------------
# GLB (binary glTF 2.0) parsing
# ---------------------------------------------------------------------------



def _parse_glb(
    path: Path,
    errors: list[str],
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]]]:
    """Parse a binary glTF 2.0 (.glb) file.

    Extracts vertex positions and triangle indices from all mesh
    primitives.  Only ``TRIANGLES`` mode (4) is supported — strips
    and fans are skipped.

    Returns:
        (triangles, unique_vertices).
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except Exception as exc:
        errors.append(f"Could not read GLB file: {exc}")
        return [], []

    if len(data) < 12:
        errors.append("GLB file too small (< 12 bytes).")
        return [], []

    magic, version, total_length = struct.unpack_from("<III", data, 0)
    if magic != _GLB_MAGIC:
        errors.append(f"Not a valid GLB file (magic: {magic:#010x}).")
        return [], []

    # Parse chunks.
    json_data: dict | None = None
    bin_data: bytes = b""
    offset = 12

    while offset + 8 <= len(data):
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_length

        if chunk_type == _GLB_JSON_CHUNK:
            try:
                json_data = _json.loads(data[chunk_start:chunk_end])
            except Exception as exc:
                errors.append(f"Failed to parse GLB JSON chunk: {exc}")
                return [], []
        elif chunk_type == _GLB_BIN_CHUNK:
            bin_data = data[chunk_start:chunk_end]

        offset = chunk_end
        # Chunks are padded to 4-byte boundaries.
        if offset % 4 != 0:
            offset += 4 - (offset % 4)

    if json_data is None:
        errors.append("GLB file has no JSON chunk.")
        return [], []

    accessors = json_data.get("accessors", [])
    buffer_views = json_data.get("bufferViews", [])
    meshes = json_data.get("meshes", [])

    all_triangles: list[tuple[tuple[float, ...], ...]] = []
    vertex_set: set[tuple[float, ...]] = set()

    for mesh in meshes:
        for primitive in mesh.get("primitives", []):
            # Only handle TRIANGLES mode (4, the default).
            mode = primitive.get("mode", 4)
            if mode != 4:
                continue

            pos_idx = primitive.get("attributes", {}).get("POSITION")
            if pos_idx is None:
                continue

            # Read vertex positions.
            positions = _read_glb_accessor(
                accessors, buffer_views, bin_data, pos_idx, errors,
            )
            if not positions:
                continue

            # Read triangle indices (if present).
            idx_accessor = primitive.get("indices")
            if idx_accessor is not None:
                indices = _read_glb_accessor_scalar(
                    accessors, buffer_views, bin_data, idx_accessor, errors,
                )
                if not indices:
                    continue
                # Build triangles from indexed geometry.
                for i in range(0, len(indices) - 2, 3):
                    i0, i1, i2 = indices[i], indices[i + 1], indices[i + 2]
                    if i0 < len(positions) and i1 < len(positions) and i2 < len(positions):
                        v0 = positions[i0]
                        v1 = positions[i1]
                        v2 = positions[i2]
                        all_triangles.append((v0, v1, v2))
                        vertex_set.update((v0, v1, v2))
            else:
                # Non-indexed: every 3 vertices form a triangle.
                for i in range(0, len(positions) - 2, 3):
                    v0 = positions[i]
                    v1 = positions[i + 1]
                    v2 = positions[i + 2]
                    all_triangles.append((v0, v1, v2))
                    vertex_set.update((v0, v1, v2))

    return all_triangles, list(vertex_set)


def _read_glb_accessor(
    accessors: list[dict],
    buffer_views: list[dict],
    bin_data: bytes,
    accessor_idx: int,
    errors: list[str],
) -> list[tuple[float, ...]]:
    """Read a VEC3 accessor from the GLB binary buffer.

    Returns a list of ``(x, y, z)`` tuples.
    """
    if accessor_idx >= len(accessors):
        errors.append(f"Accessor index {accessor_idx} out of range.")
        return []

    acc = accessors[accessor_idx]
    component_type = acc.get("componentType", 5126)
    acc_type = acc.get("type", "")
    count = acc.get("count", 0)

    if acc_type != "VEC3":
        errors.append(f"Expected VEC3 accessor, got {acc_type!r}.")
        return []

    fmt_info = _COMPONENT_FMT.get(component_type)
    if not fmt_info:
        errors.append(f"Unsupported component type: {component_type}.")
        return []

    fmt_char, comp_size = fmt_info
    bv_idx = acc.get("bufferView")
    if bv_idx is None or bv_idx >= len(buffer_views):
        errors.append("Missing or invalid bufferView for accessor.")
        return []

    bv = buffer_views[bv_idx]
    bv_offset = bv.get("byteOffset", 0)
    bv_stride = bv.get("byteStride", 0)
    acc_offset = acc.get("byteOffset", 0)

    start = bv_offset + acc_offset
    stride = bv_stride if bv_stride > 0 else comp_size * 3

    result: list[tuple[float, ...]] = []
    for i in range(count):
        pos = start + i * stride
        if pos + comp_size * 3 > len(bin_data):
            break
        x, y, z = struct.unpack_from(f"<3{fmt_char}", bin_data, pos)
        result.append((float(x), float(y), float(z)))

    return result


def _read_glb_accessor_scalar(
    accessors: list[dict],
    buffer_views: list[dict],
    bin_data: bytes,
    accessor_idx: int,
    errors: list[str],
) -> list[int]:
    """Read a SCALAR accessor from the GLB binary buffer.

    Returns a flat list of integer index values.
    """
    if accessor_idx >= len(accessors):
        errors.append(f"Accessor index {accessor_idx} out of range.")
        return []

    acc = accessors[accessor_idx]
    component_type = acc.get("componentType", 5123)
    count = acc.get("count", 0)

    fmt_info = _COMPONENT_FMT.get(component_type)
    if not fmt_info:
        errors.append(f"Unsupported index component type: {component_type}.")
        return []

    fmt_char, comp_size = fmt_info
    bv_idx = acc.get("bufferView")
    if bv_idx is None or bv_idx >= len(buffer_views):
        errors.append("Missing or invalid bufferView for index accessor.")
        return []

    bv = buffer_views[bv_idx]
    bv_offset = bv.get("byteOffset", 0)
    acc_offset = acc.get("byteOffset", 0)

    start = bv_offset + acc_offset
    result: list[int] = []
    for i in range(count):
        pos = start + i * comp_size
        if pos + comp_size > len(bin_data):
            break
        val = struct.unpack_from(f"<{fmt_char}", bin_data, pos)[0]
        result.append(int(val))

    return result


def _convert_glb_to_stl(path: Path, output_path: str | None = None) -> str:
    """Convert a GLB file to binary STL.

    Args:
        path: Path to the input GLB file.
        output_path: Optional output path.

    Returns:
        The path to the written STL file.

    Raises:
        ValueError: If the GLB has no geometry or cannot be parsed.
    """
    errors: list[str] = []
    triangles, vertices = _parse_glb(path, errors)
    if errors:
        raise ValueError(f"Failed to parse GLB: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("GLB file contains no geometry to convert.")

    if output_path is None:
        output_path = str(path.with_suffix(".stl"))

    _write_binary_stl(triangles, output_path)
    return output_path


# ---------------------------------------------------------------------------
# Mesh rescaling
# ---------------------------------------------------------------------------


def analyze_mesh(file_path: str) -> MeshAnalysis:
    """Perform detailed geometric and printability analysis of a mesh.

    Computes volume, surface area, center of mass, overhang detection,
    connected components, and a composite printability score.

    Args:
        file_path: Path to .stl, .obj, or .glb file.

    Returns:
        :class:`MeshAnalysis` with full metrics.
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    if not path.is_file():
        return MeshAnalysis(printability_issues=["File not found"])

    errors: list[str] = []
    if ext == ".stl":
        triangles, vertices = _parse_stl(path, errors)
    elif ext == ".obj":
        triangles, vertices = _parse_obj(path, errors)
    elif ext == ".glb":
        triangles, vertices = _parse_glb(path, errors)
    else:
        return MeshAnalysis(printability_issues=[f"Unsupported format: {ext}"])

    if errors or not triangles:
        return MeshAnalysis(printability_issues=errors or ["No geometry found"])

    bbox = _bounding_box(vertices)
    dims = {
        "width_mm": round(bbox["x_max"] - bbox["x_min"], 2),
        "depth_mm": round(bbox["y_max"] - bbox["y_min"], 2),
        "height_mm": round(bbox["z_max"] - bbox["z_min"], 2),
    }

    # Volume via signed tetrahedron method
    volume = 0.0
    total_area = 0.0
    cx, cy, cz = 0.0, 0.0, 0.0
    overhang_count = 0
    max_overhang = 0.0
    degenerate_count = 0

    for tri in triangles:
        v0, v1, v2 = tri
        # Cross product of edges
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        cross = (
            e1[1] * e2[2] - e1[2] * e2[1],
            e1[2] * e2[0] - e1[0] * e2[2],
            e1[0] * e2[1] - e1[1] * e2[0],
        )
        area_2 = math.sqrt(cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2)
        tri_area = area_2 / 2.0

        if tri_area < 1e-10:
            degenerate_count += 1
            continue

        total_area += tri_area

        # Signed volume contribution
        volume += (
            v0[0] * (v1[1] * v2[2] - v2[1] * v1[2])
            - v1[0] * (v0[1] * v2[2] - v2[1] * v0[2])
            + v2[0] * (v0[1] * v1[2] - v1[1] * v0[2])
        ) / 6.0

        # Centroid contribution (area-weighted)
        centroid = (
            (v0[0] + v1[0] + v2[0]) / 3.0,
            (v0[1] + v1[1] + v2[1]) / 3.0,
            (v0[2] + v1[2] + v2[2]) / 3.0,
        )
        cx += centroid[0] * tri_area
        cy += centroid[1] * tri_area
        cz += centroid[2] * tri_area

        # Overhang detection: angle between face normal and -Z
        nz = cross[2] / area_2  # normalized Z component of normal
        if nz < 0:  # face points downward
            angle = math.degrees(math.acos(max(-1.0, min(1.0, -nz))))
            overhang_angle = 90.0 - angle  # angle from vertical
            if overhang_angle > max_overhang:
                max_overhang = overhang_angle
            if overhang_angle > 45:
                overhang_count += 1

    volume = abs(volume)
    if total_area > 0:
        cx /= total_area
        cy /= total_area
        cz /= total_area

    # Connected components via union-find on shared edges
    components = _count_components(triangles)

    # Manifold check
    is_manifold = _check_manifold(triangles, [])

    # Overhang percentage
    valid_tris = len(triangles) - degenerate_count
    overhang_pct = (overhang_count / valid_tris * 100) if valid_tris > 0 else 0.0

    # Printability score (0-100)
    issues: list[str] = []
    score = 100

    if not is_manifold:
        score -= 15
        issues.append("Non-manifold geometry (not watertight)")
    if components > 1:
        score -= min(20, (components - 1) * 10)
        issues.append(f"{components} disconnected components (floating parts)")
    if max_overhang > 60:
        score -= 20
        issues.append(f"Severe overhangs ({max_overhang:.0f} degrees)")
    elif max_overhang > 45:
        score -= 10
        issues.append(f"Moderate overhangs ({max_overhang:.0f} degrees)")
    if overhang_pct > 30:
        score -= 10
        issues.append(f"High overhang percentage ({overhang_pct:.0f}%)")
    if degenerate_count > 0:
        pct = degenerate_count / len(triangles) * 100
        if pct > 5:
            score -= 10
            issues.append(f"Degenerate triangles ({degenerate_count})")
        else:
            score -= 5
    max_dim = max(dims["width_mm"], dims["depth_mm"], dims["height_mm"])
    if max_dim < 1:
        score -= 15
        issues.append("Model is very small (< 1mm)")
    if volume < 1:
        score -= 10
        issues.append("Negligible volume")

    score = max(0, score)

    return MeshAnalysis(
        triangle_count=len(triangles),
        vertex_count=len(vertices),
        is_manifold=is_manifold,
        bounding_box=bbox,
        dimensions_mm=dims,
        volume_mm3=round(volume, 2),
        surface_area_mm2=round(total_area, 2),
        center_of_mass={"x": round(cx, 2), "y": round(cy, 2), "z": round(cz, 2)},
        connected_components=components,
        degenerate_triangles=degenerate_count,
        overhang_triangle_count=overhang_count,
        overhang_percentage=round(overhang_pct, 1),
        max_overhang_angle_deg=round(max_overhang, 1),
        printability_score=score,
        printability_issues=issues,
    )


def repair_stl(
    file_path: str,
    *,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Repair common STL issues: degenerate triangles, inconsistent normals.

    Removes zero-area triangles and recomputes face normals from vertex
    winding order.  Does not attempt topology repair (hole closing).

    Args:
        file_path: Path to the STL file.
        output_path: Output path.  Defaults to overwriting the input.

    Returns:
        Dict with repair statistics.
    """
    path = Path(file_path)
    errors: list[str] = []
    triangles, vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    cleaned: list[tuple[tuple[float, ...], ...]] = []
    degenerate_removed = 0
    normals_fixed = 0

    for tri in triangles:
        v0, v1, v2 = tri
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        cross = (
            e1[1] * e2[2] - e1[2] * e2[1],
            e1[2] * e2[0] - e1[0] * e2[2],
            e1[0] * e2[1] - e1[1] * e2[0],
        )
        mag = math.sqrt(cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2)
        if mag < 1e-10:
            degenerate_removed += 1
            continue
        cleaned.append(tri)

    # Recompute normals is handled by _write_binary_stl (writes zero normals,
    # slicers recompute from winding order — this is standard practice)
    normals_fixed = len(cleaned)  # all normals refreshed

    out = output_path or file_path
    _write_binary_stl(cleaned, out)

    return {
        "path": out,
        "original_triangles": len(triangles),
        "cleaned_triangles": len(cleaned),
        "degenerate_removed": degenerate_removed,
        "normals_recomputed": normals_fixed,
        **_residual_defect_report(cleaned, hole_pass_ran=False),
    }


def _residual_defect_report(
    triangles: list[tuple[tuple[float, ...], ...]],
    *,
    hole_pass_ran: bool,
) -> dict[str, Any]:
    """Describe what is still wrong with a mesh a repair pass just wrote.

    A repair result that only lists what was removed lets "success" stand in
    for "fixed": a caller whose mesh went in non-manifold and came out
    unchanged reads zeros next to a happy path and assumes the mesh is now
    clean.  This tail states the output's actual condition — watertight or
    not — and, when defects remain, says which classes this pass repairs and
    which it has no repair for, so the caller is never left inferring.
    """
    census = _edge_census(triangles)
    report: dict[str, Any] = {
        "is_watertight": census["boundary"] == 0 and census["pinch"] == 0,
        "remaining_boundary_edges": census["boundary"],
        "remaining_pinch_edges": census["pinch"],
    }

    unrepaired: list[str] = []
    if census["boundary"]:
        if hole_pass_ran:
            unrepaired.append(
                f"{census['boundary']:,} open edges remain: the hole-closing "
                f"pass sews boundary loops of 3-50 edges, so larger or "
                f"tangled holes were left as-is."
            )
        else:
            unrepaired.append(
                f"{census['boundary']:,} open edges remain: this pass only "
                f"removes degenerate triangles and refreshes normals.  Run "
                f"again with close_holes=True to sew small holes."
            )
    if census["pinch"]:
        unrepaired.append(
            f"{census['pinch']:,} pinched edges remain (3+ triangles meet "
            f"along one line).  No repair pass here handles this class — it "
            f"is not a hole, so hole closing cannot touch it.  The geometry "
            f"has to be rebuilt where it was made (e.g. regenerate the "
            f"model, or re-run the boolean/decoration step that produced "
            f"it)."
        )
    if unrepaired:
        report["unrepaired"] = unrepaired
    return report


def splice_mesh_at_z(
    top_path: str,
    bottom_path: str,
    z_plane: float,
    output_path: str,
) -> dict[str, Any]:
    """Splice two meshes at a z-plane: keep top from one, bottom from another.

    Takes the top portion (above *z_plane*) from *top_path* and the bottom
    portion (below *z_plane*) from *bottom_path*, clipping triangles that
    cross the boundary.  Triangles that straddle the z-plane are split into
    smaller triangles so the geometry is clean at the seam.

    Use case: combine a body with the correct top surface (e.g. logo) with
    a body that has the correct bottom geometry (e.g. bigger pocket).

    Args:
        top_path: STL providing geometry ABOVE z_plane.
        bottom_path: STL providing geometry BELOW z_plane.
        z_plane: Z height where the splice happens (mm).
        output_path: Where to write the combined STL.

    Returns:
        Dict with triangle counts and splice stats.
    """
    top_tris, _ = _parse_stl(Path(top_path), [])
    bot_tris, _ = _parse_stl(Path(bottom_path), [])

    def _lerp(
        a: tuple[float, ...], b: tuple[float, ...], t: float,
    ) -> tuple[float, ...]:
        return (
            a[0] + t * (b[0] - a[0]),
            a[1] + t * (b[1] - a[1]),
            a[2] + t * (b[2] - a[2]),
        )

    def _clip_above(
        tri: tuple[tuple[float, ...], ...], z: float,
    ) -> list[tuple[tuple[float, ...], ...]]:
        above = [v for v in tri if v[2] >= z]
        below = [v for v in tri if v[2] < z]
        if len(above) == 3:
            return [tri]
        if len(above) == 0:
            return []
        if len(above) == 1:
            a = above[0]
            b, c = below
            t_ab = (z - a[2]) / (b[2] - a[2]) if b[2] != a[2] else 0.0
            t_ac = (z - a[2]) / (c[2] - a[2]) if c[2] != a[2] else 0.0
            return [(a, _lerp(a, b, t_ab), _lerp(a, c, t_ac))]
        a, b = above
        c = below[0]
        t_ac = (z - a[2]) / (c[2] - a[2]) if c[2] != a[2] else 0.0
        t_bc = (z - b[2]) / (c[2] - b[2]) if c[2] != b[2] else 0.0
        p_ac, p_bc = _lerp(a, c, t_ac), _lerp(b, c, t_bc)
        return [(a, b, p_bc), (a, p_bc, p_ac)]

    def _clip_below(
        tri: tuple[tuple[float, ...], ...], z: float,
    ) -> list[tuple[tuple[float, ...], ...]]:
        below = [v for v in tri if v[2] <= z]
        above = [v for v in tri if v[2] > z]
        if len(below) == 3:
            return [tri]
        if len(below) == 0:
            return []
        if len(below) == 1:
            a = below[0]
            b, c = above
            t_ab = (z - a[2]) / (b[2] - a[2]) if b[2] != a[2] else 0.0
            t_ac = (z - a[2]) / (c[2] - a[2]) if c[2] != a[2] else 0.0
            return [(a, _lerp(a, b, t_ab), _lerp(a, c, t_ac))]
        a, b = below
        c = above[0]
        t_ac = (z - a[2]) / (c[2] - a[2]) if c[2] != a[2] else 0.0
        t_bc = (z - b[2]) / (c[2] - b[2]) if c[2] != b[2] else 0.0
        p_ac, p_bc = _lerp(a, c, t_ac), _lerp(b, c, t_bc)
        return [(a, b, p_bc), (a, p_bc, p_ac)]

    result: list[tuple[tuple[float, ...], ...]] = []
    top_kept = top_clipped = 0
    for tri in top_tris:
        clipped = _clip_above(tri, z_plane)
        if clipped:
            if len(clipped) != 1 or clipped[0] != tri:
                top_clipped += 1
            top_kept += len(clipped)
            result.extend(clipped)

    bot_kept = bot_clipped = 0
    for tri in bot_tris:
        clipped = _clip_below(tri, z_plane)
        if clipped:
            if len(clipped) != 1 or clipped[0] != tri:
                bot_clipped += 1
            bot_kept += len(clipped)
            result.extend(clipped)

    _write_binary_stl(result, output_path)

    return {
        "path": output_path,
        "total_triangles": len(result),
        "top_source": top_path,
        "top_triangles_kept": top_kept,
        "top_triangles_clipped": top_clipped,
        "bottom_source": bottom_path,
        "bottom_triangles_kept": bot_kept,
        "bottom_triangles_clipped": bot_clipped,
        "z_plane": z_plane,
    }


def compose_stls(
    file_paths: list[str],
    output_path: str,
) -> dict[str, Any]:
    """Merge multiple STL files into a single combined mesh.

    Concatenates all triangle geometry.  No boolean operations —
    simply combines all bodies into one file.

    Args:
        file_paths: List of STL file paths to merge.
        output_path: Path for the combined STL output.

    Returns:
        Dict with merge statistics.
    """
    if not file_paths:
        raise ValueError("No files to compose.")

    all_triangles: list[tuple[tuple[float, ...], ...]] = []
    file_stats: list[dict[str, Any]] = []

    for fp in file_paths:
        path = Path(fp)
        errors: list[str] = []
        ext = path.suffix.lower()
        if ext == ".stl":
            triangles, _ = _parse_stl(path, errors)
        elif ext == ".obj":
            triangles, _ = _parse_obj(path, errors)
        elif ext == ".glb":
            triangles, _ = _parse_glb(path, errors)
        else:
            raise ValueError(f"Unsupported format for composition: {ext}")

        if errors:
            raise ValueError(f"Failed to parse {fp}: {'; '.join(errors)}")

        file_stats.append({"file": fp, "triangles": len(triangles)})
        all_triangles.extend(triangles)

    _write_binary_stl(all_triangles, output_path)

    return {
        "path": output_path,
        "total_triangles": len(all_triangles),
        "files_merged": len(file_paths),
        "per_file": file_stats,
    }


def export_3mf(
    file_path: str,
    *,
    output_path: str | None = None,
) -> str:
    """Export an STL/OBJ/GLB file as 3MF (3D Manufacturing Format).

    3MF is a ZIP-based XML format preferred by modern slicers
    (PrusaSlicer, OrcaSlicer, Bambu Studio).

    Args:
        file_path: Path to the input mesh file.
        output_path: Output 3MF path.  Auto-generated if omitted.

    Returns:
        Path to the written 3MF file.
    """
    path = Path(file_path)
    ext = path.suffix.lower()
    errors: list[str] = []

    if ext == ".stl":
        triangles, vertices = _parse_stl(path, errors)
    elif ext == ".obj":
        triangles, vertices = _parse_obj(path, errors)
    elif ext == ".glb":
        triangles, vertices = _parse_glb(path, errors)
    else:
        raise ValueError(f"Unsupported format for 3MF export: {ext}")

    if errors:
        raise ValueError(f"Failed to parse {file_path}: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("File contains no geometry.")

    if output_path is None:
        output_path = str(path.with_suffix(".3mf"))

    # Build unique vertex list and index map
    vert_map: dict[tuple[float, ...], int] = {}
    indexed_verts: list[tuple[float, ...]] = []
    indexed_tris: list[tuple[int, int, int]] = []

    for tri in triangles:
        indices = []
        for v in tri:
            if v not in vert_map:
                vert_map[v] = len(indexed_verts)
                indexed_verts.append(v)
            indices.append(vert_map[v])
        indexed_tris.append((indices[0], indices[1], indices[2]))

    # Build 3MF XML content
    vert_lines = "\n".join(
        f'        <vertex x="{v[0]}" y="{v[1]}" z="{v[2]}" />'
        for v in indexed_verts
    )
    tri_lines = "\n".join(
        f'        <triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}" />'
        for t in indexed_tris
    )

    model_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
{vert_lines}
        </vertices>
        <triangles>
{tri_lines}
        </triangles>
      </mesh>
    </object>
  </resources>
  <build>
    <item objectid="1" />
  </build>
</model>"""

    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml" />
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml" />
  <Default Extension="png" ContentType="image/png" />
</Types>"""

    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0"
                 Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" />
</Relationships>"""

    # Generate thumbnail (best-effort).
    thumbnail_data: bytes | None = None
    try:
        from kiln.multicolor_3mf import _generate_thumbnail
        thumbnail_data = _generate_thumbnail([file_path])
    except Exception:
        pass

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("3D/3dmodel.model", model_xml)
        if thumbnail_data:
            zf.writestr("Metadata/plate_1.png", thumbnail_data)

    return output_path


def build_multi_material_3mf(
    objects: list[dict[str, Any]],
    *,
    output_path: str | None = None,
) -> str:
    """Build a 3MF file with multiple objects and per-object filament assignments.

    Each object dict has:
        ``file_path`` (str): Path to STL/OBJ/GLB mesh file.
        ``filament_index`` (int): 0-based filament/material index.
        ``name`` (str, optional): Display name for the object.
        ``color`` (str, optional): Hex color for the material (e.g. ``"#FF0000"``).
        ``material_name`` (str, optional): Material name (e.g. ``"PETG"``).

    The resulting 3MF assigns each object to its filament_index via the 3MF
    ``pid``/``pindex`` mechanism, which PrusaSlicer, OrcaSlicer, and Bambu
    Studio all understand for multi-material prints.

    Args:
        objects: List of object dicts with mesh paths and filament assignments.
        output_path: Output 3MF path. Auto-generated if omitted.

    Returns:
        Path to the written multi-material 3MF file.
    """
    if not objects:
        raise ValueError("At least one object is required.")

    if output_path is None:
        output_path = str(
            Path(objects[0]["file_path"]).parent / "multi_material_print.3mf"
        )

    # Collect unique filament indices and build material list
    filament_set: dict[int, dict[str, str]] = {}
    for obj in objects:
        fidx = obj.get("filament_index", 0)
        if fidx not in filament_set:
            filament_set[fidx] = {
                "name": obj.get("material_name", f"Material_{fidx}"),
                "color": obj.get("color", _DEFAULT_COLORS[fidx % len(_DEFAULT_COLORS)]),
            }

    # Sort by index for deterministic output
    sorted_filaments = sorted(filament_set.items())

    # Build basematerials XML
    from xml.sax.saxutils import escape as _xml_escape

    base_mat_lines = []
    for _idx, mat in sorted_filaments:
        color_hex = mat["color"].lstrip("#")
        if len(color_hex) == 6:
            color_hex += "FF"
        safe_name = _xml_escape(mat["name"], {'"': "&quot;"})
        base_mat_lines.append(
            f'      <base name="{safe_name}" displaycolor="#{color_hex}" />'
        )

    # Build per-object mesh XML blocks
    object_blocks: list[str] = []
    build_items: list[str] = []
    obj_id = 1

    for obj in objects:
        fp = obj["file_path"]
        if not Path(fp).is_file():
            raise ValueError(f"Mesh file not found: {fp}")
        ext = Path(fp).suffix.lower()
        errors: list[str] = []

        if ext == ".stl":
            triangles, _verts = _parse_stl(Path(fp), errors)
        elif ext == ".obj":
            triangles, _verts = _parse_obj(Path(fp), errors)
        elif ext == ".glb":
            triangles, _verts = _parse_glb(Path(fp), errors)
        else:
            raise ValueError(f"Unsupported format for multi-material: {ext}")

        if errors:
            raise ValueError(f"Failed to parse {fp}: {'; '.join(errors)}")
        if not triangles:
            raise ValueError(f"File {fp} contains no geometry.")

        # Build indexed vertex list
        vert_map: dict[tuple[float, ...], int] = {}
        indexed_verts: list[tuple[float, ...]] = []
        indexed_tris: list[tuple[int, int, int]] = []

        for tri in triangles:
            indices = []
            for v in tri:
                if v not in vert_map:
                    vert_map[v] = len(indexed_verts)
                    indexed_verts.append(v)
                indices.append(vert_map[v])
            indexed_tris.append((indices[0], indices[1], indices[2]))

        raw_name = obj.get("name", Path(fp).stem)
        name = _xml_escape(raw_name, {'"': "&quot;"})
        fidx = obj.get("filament_index", 0)

        # Find the position of this filament_index in sorted_filaments
        pindex = next(
            i for i, (idx, _) in enumerate(sorted_filaments) if idx == fidx
        )

        vert_xml = "\n".join(
            f'          <vertex x="{v[0]}" y="{v[1]}" z="{v[2]}" />'
            for v in indexed_verts
        )
        tri_xml = "\n".join(
            f'          <triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}" />'
            for t in indexed_tris
        )

        object_blocks.append(
            f'    <object id="{obj_id}" type="model" name="{name}" pid="1" pindex="{pindex}">\n'
            f"      <mesh>\n"
            f"        <vertices>\n{vert_xml}\n        </vertices>\n"
            f"        <triangles>\n{tri_xml}\n        </triangles>\n"
            f"      </mesh>\n"
            f"    </object>"
        )
        build_items.append(f'    <item objectid="{obj_id}" />')
        obj_id += 1

    # Assemble full model XML
    model_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US"\n'
        '       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        "  <resources>\n"
        '    <basematerials id="1">\n'
        + "\n".join(base_mat_lines)
        + "\n"
        "    </basematerials>\n"
        + "\n".join(object_blocks)
        + "\n"
        "  </resources>\n"
        "  <build>\n"
        + "\n".join(build_items)
        + "\n"
        "  </build>\n"
        "</model>"
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="model" '
        'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml" />\n'
        '  <Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml" />\n'
        '  <Default Extension="png" ContentType="image/png" />\n'
        "</Types>"
    )

    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Target="/3D/3dmodel.model" Id="rel0"\n'
        '                 Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" />\n'
        "</Relationships>"
    )

    # Generate thumbnail from all STL parts (best-effort).
    thumbnail_data: bytes | None = None
    try:
        from kiln.multicolor_3mf import _generate_thumbnail
        stl_paths = [obj["file_path"] for obj in objects if obj.get("file_path")]
        thumbnail_data = _generate_thumbnail(stl_paths)
    except Exception:
        pass

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("3D/3dmodel.model", model_xml)
        if thumbnail_data:
            zf.writestr("Metadata/plate_1.png", thumbnail_data)

    return output_path


# Default colors for filament indices when no color is specified
_DEFAULT_COLORS = [
    "#FFFFFFFF",  # White
    "#FF0000FF",  # Red
    "#0000FFFF",  # Blue
    "#00FF00FF",  # Green
    "#FFFF00FF",  # Yellow
    "#FF00FFFF",  # Magenta
    "#00FFFFFF",  # Cyan
    "#000000FF",  # Black
]


# ---------------------------------------------------------------------------
# Connected component analysis
# ---------------------------------------------------------------------------


def _count_components(
    triangles: list[tuple[tuple[float, ...], ...]],
) -> int:
    """Count connected components via union-find on shared edges."""
    if not triangles:
        return 0

    n = len(triangles)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Map each edge to the first triangle that uses it
    edge_to_tri: dict[tuple[tuple[float, ...], tuple[float, ...]], int] = {}
    for i, tri in enumerate(triangles):
        for j in range(3):
            v_a = tri[j]
            v_b = tri[(j + 1) % 3]
            edge = (min(v_a, v_b), max(v_a, v_b))
            if edge in edge_to_tri:
                union(i, edge_to_tri[edge])
            else:
                edge_to_tri[edge] = i

    roots = {find(i) for i in range(n)}
    return len(roots)


def rescale_stl(
    file_path: str,
    *,
    target_height_mm: float | None = None,
    scale_factor: float | None = None,
    max_dimension_mm: float | None = None,
    scale_x: float | None = None,
    scale_y: float | None = None,
    scale_z: float | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Rescale an STL file to meet dimensional targets.

    **Uniform scaling** — provide exactly one of ``target_height_mm``,
    ``scale_factor``, or ``max_dimension_mm``.

    **Per-axis scaling** — provide ``scale_x``, ``scale_y``, and/or
    ``scale_z``.  Omitted axes default to 1.0.

    Cannot combine uniform and per-axis options.

    Args:
        file_path: Path to the input STL file.
        target_height_mm: Desired Z-axis height in mm.
        scale_factor: Uniform scale multiplier (e.g., 2.0 = double).
        max_dimension_mm: Scale down so the largest axis fits this limit.
        scale_x: X-axis scale multiplier (default 1.0).
        scale_y: Y-axis scale multiplier (default 1.0).
        scale_z: Z-axis scale multiplier (default 1.0).
        output_path: Output file path.  Defaults to overwriting input.

    Returns:
        Dict with ``path``, ``scale_applied``, ``original_dimensions``,
        and ``new_dimensions``.
    """
    uniform_opts = sum(x is not None for x in (target_height_mm, scale_factor, max_dimension_mm))
    per_axis = any(x is not None for x in (scale_x, scale_y, scale_z))

    if uniform_opts and per_axis:
        raise ValueError("Cannot combine uniform scaling with per-axis scaling.")
    if not uniform_opts and not per_axis:
        raise ValueError("Provide uniform (target_height_mm/scale_factor/max_dimension_mm) or per-axis (scale_x/y/z).")
    if uniform_opts > 1:
        raise ValueError("Exactly one of target_height_mm, scale_factor, or max_dimension_mm required.")

    path = Path(file_path)
    errors: list[str] = []
    triangles, vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    bbox = _bounding_box(vertices)
    orig_dims = {
        "width_mm": round(bbox["x_max"] - bbox["x_min"], 2),
        "depth_mm": round(bbox["y_max"] - bbox["y_min"], 2),
        "height_mm": round(bbox["z_max"] - bbox["z_min"], 2),
    }

    # Compute scale factors (sx, sy, sz)
    if per_axis:
        sx = scale_x if scale_x is not None else 1.0
        sy = scale_y if scale_y is not None else 1.0
        sz = scale_z if scale_z is not None else 1.0
    elif target_height_mm is not None:
        current_h = bbox["z_max"] - bbox["z_min"]
        if current_h < 0.001:
            raise ValueError("Model has near-zero height, cannot scale to target.")
        sx = sy = sz = target_height_mm / current_h
    elif max_dimension_mm is not None:
        largest = max(
            bbox["x_max"] - bbox["x_min"],
            bbox["y_max"] - bbox["y_min"],
            bbox["z_max"] - bbox["z_min"],
        )
        if largest < 0.001:
            raise ValueError("Model has near-zero dimensions, cannot scale.")
        sx = sy = sz = max_dimension_mm / largest if largest > max_dimension_mm else 1.0
    else:
        sx = sy = sz = scale_factor  # type: ignore[assignment]

    # Apply scale to all vertices
    scaled_triangles: list[tuple[tuple[float, ...], ...]] = []
    for tri in triangles:
        scaled_tri = tuple((v[0] * sx, v[1] * sy, v[2] * sz) for v in tri)
        scaled_triangles.append(scaled_tri)

    out = output_path or file_path
    _write_binary_stl(scaled_triangles, out)

    new_dims = {
        "width_mm": round(orig_dims["width_mm"] * sx, 2),
        "depth_mm": round(orig_dims["depth_mm"] * sy, 2),
        "height_mm": round(orig_dims["height_mm"] * sz, 2),
    }

    scale_applied = {"x": round(sx, 4), "y": round(sy, 4), "z": round(sz, 4)} if per_axis else round(sx, 4)

    return {
        "path": out,
        "scale_applied": scale_applied,
        "original_dimensions": orig_dims,
        "new_dimensions": new_dims,
    }


# ---------------------------------------------------------------------------
# Print orientation optimization
# ---------------------------------------------------------------------------


def optimize_orientation(
    file_path: str,
    *,
    output_path: str | None = None,
    candidates: int = 6,
) -> dict[str, Any]:
    """Find the print orientation that minimizes overhangs.

    Tests the mesh in several candidate rotations (around X and Y axes)
    and picks the orientation with the fewest overhang triangles and
    largest bed contact area.

    Only operates on STL files (binary read/write).

    Args:
        file_path: Path to the STL file.
        output_path: Where to write the re-oriented STL.  Defaults to
            overwriting the input.
        candidates: Number of candidate rotations per axis (default 6,
            tests 0/30/60/90/120/150 degrees around X and Y = 36 combos).

    Returns:
        Dict with best rotation angles, overhang stats, and output path.
    """
    path = Path(file_path)
    errors: list[str] = []
    triangles, vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    step = 180.0 / candidates
    angles = [i * step for i in range(candidates)]

    best_score = -1.0
    best_rx = 0.0
    best_ry = 0.0
    best_tris: list[tuple[tuple[float, ...], ...]] = triangles

    for rx in angles:
        for ry in angles:
            if rx == 0 and ry == 0:
                rotated = triangles
            else:
                rotated = _rotate_triangles(triangles, rx, ry)

            # Score: minimize overhangs, maximize bed contact
            overhang_count = 0
            bed_contact = 0.0
            for tri in rotated:
                v0, v1, v2 = tri
                e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
                e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
                # Full cross product for area and normal
                cx = e1[1] * e2[2] - e1[2] * e2[1]
                cy = e1[2] * e2[0] - e1[0] * e2[2]
                cz = e1[0] * e2[1] - e1[1] * e2[0]
                area_2 = math.sqrt(cx * cx + cy * cy + cz * cz)
                if area_2 < 1e-10:
                    continue

                nz_norm = cz / area_2
                if nz_norm < -0.7:  # face points strongly downward
                    overhang_count += 1
                # Bottom face contributes to bed contact
                min_z = min(v0[2], v1[2], v2[2])
                if min_z < 0.5 and nz_norm < -0.9:
                    bed_contact += area_2 / 2.0

            # Score: less overhangs is better, more bed contact is better
            score = bed_contact * 10.0 - overhang_count
            if score > best_score:
                best_score = score
                best_rx = rx
                best_ry = ry
                best_tris = rotated

    # Center the best orientation on the build plate (z_min = 0)
    all_z = [v[2] for tri in best_tris for v in tri]
    z_shift = -min(all_z) if all_z else 0.0
    if abs(z_shift) > 1e-6:
        best_tris = [
            tuple((v[0], v[1], v[2] + z_shift) for v in tri)
            for tri in best_tris
        ]

    out = output_path or file_path
    _write_binary_stl(best_tris, out)

    # Analyze the result
    analysis = analyze_mesh(out)

    return {
        "path": out,
        "rotation_x_deg": round(best_rx, 1),
        "rotation_y_deg": round(best_ry, 1),
        "overhang_percentage": analysis.overhang_percentage,
        "max_overhang_angle": analysis.max_overhang_angle_deg,
        "printability_score": analysis.printability_score,
        "dimensions_mm": analysis.dimensions_mm,
    }


def _rotate_triangles(
    triangles: list[tuple[tuple[float, ...], ...]],
    rx_deg: float,
    ry_deg: float,
) -> list[tuple[tuple[float, ...], ...]]:
    """Rotate all triangles around X then Y axis (degrees)."""
    rx = math.radians(rx_deg)
    ry = math.radians(ry_deg)
    cos_x, sin_x = math.cos(rx), math.sin(rx)
    cos_y, sin_y = math.cos(ry), math.sin(ry)

    def rot(v: tuple[float, ...]) -> tuple[float, ...]:
        x, y, z = v[0], v[1], v[2]
        # Rotate around X
        y2 = y * cos_x - z * sin_x
        z2 = y * sin_x + z * cos_x
        # Rotate around Y
        x3 = x * cos_y + z2 * sin_y
        z3 = -x * sin_y + z2 * cos_y
        return (x3, y2, z3)

    return [tuple(rot(v) for v in tri) for tri in triangles]


# ---------------------------------------------------------------------------
# Support volume estimation
# ---------------------------------------------------------------------------


def estimate_support_volume(file_path: str) -> dict[str, Any]:
    """Estimate the volume of support material needed for printing.

    Projects each overhang triangle downward to the build plate (z=0)
    and sums the prism volumes.  This is a rough estimate — real slicer
    support generation is more sophisticated.

    Args:
        file_path: Path to .stl, .obj, or .glb file.

    Returns:
        Dict with support volume estimate and overhang statistics.
    """
    path = Path(file_path)
    ext = path.suffix.lower()
    errors: list[str] = []

    if ext == ".stl":
        triangles, _ = _parse_stl(path, errors)
    elif ext == ".obj":
        triangles, _ = _parse_obj(path, errors)
    elif ext == ".glb":
        triangles, _ = _parse_glb(path, errors)
    else:
        raise ValueError(f"Unsupported format: {ext}")

    if errors:
        raise ValueError(f"Failed to parse: {'; '.join(errors)}")

    support_volume = 0.0
    overhang_area = 0.0
    overhang_count = 0
    total_count = 0

    for tri in triangles:
        v0, v1, v2 = tri
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        cross = (
            e1[1] * e2[2] - e1[2] * e2[1],
            e1[2] * e2[0] - e1[0] * e2[2],
            e1[0] * e2[1] - e1[1] * e2[0],
        )
        area_2 = math.sqrt(cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2)
        if area_2 < 1e-10:
            continue

        total_count += 1
        nz = cross[2] / area_2  # normalized Z of face normal

        # Overhang: face points downward past 45 degrees
        if nz < -0.707:  # cos(45°) ≈ 0.707
            tri_area = area_2 / 2.0
            overhang_area += tri_area
            overhang_count += 1

            # Approximate support volume: project triangle down to z=0
            avg_z = (v0[2] + v1[2] + v2[2]) / 3.0
            if avg_z > 0:
                # Prism volume = projected area × height
                # Projected XY area ≈ tri_area × |nz| (projection onto XY)
                proj_area = tri_area * abs(nz)
                support_volume += proj_area * avg_z

    # Estimate support weight (typical PLA density ~1.24 g/cm³)
    support_volume_cm3 = support_volume / 1000.0
    support_weight_g = support_volume_cm3 * 1.24

    return {
        "support_volume_mm3": round(support_volume, 1),
        "support_volume_cm3": round(support_volume_cm3, 2),
        "support_weight_g": round(support_weight_g, 1),
        "overhang_area_mm2": round(overhang_area, 1),
        "overhang_triangle_count": overhang_count,
        "total_triangles": total_count,
        "overhang_percentage": round(overhang_count / total_count * 100, 1) if total_count else 0.0,
        "needs_supports": overhang_count > 0,
    }


# ---------------------------------------------------------------------------
# Enhanced mesh repair: hole closing
# ---------------------------------------------------------------------------


def repair_stl_advanced(
    file_path: str,
    *,
    output_path: str | None = None,
    close_holes: bool = True,
) -> dict[str, Any]:
    """Enhanced STL repair: degenerate removal, normal recompute, hole closing.

    Finds boundary edges (edges shared by only one triangle) and attempts
    to close small holes by fan-triangulating the boundary loop.

    Args:
        file_path: Path to the STL file.
        output_path: Output path.  Defaults to overwriting input.
        close_holes: Whether to attempt hole closing (default True).

    Returns:
        Dict with repair statistics.
    """
    path = Path(file_path)
    errors: list[str] = []
    triangles, _ = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    # Phase 1: Remove degenerate triangles
    cleaned: list[tuple[tuple[float, ...], ...]] = []
    degenerate_removed = 0
    for tri in triangles:
        v0, v1, v2 = tri
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        mag = math.sqrt(
            (e1[1] * e2[2] - e1[2] * e2[1]) ** 2
            + (e1[2] * e2[0] - e1[0] * e2[2]) ** 2
            + (e1[0] * e2[1] - e1[1] * e2[0]) ** 2
        )
        if mag < 1e-10:
            degenerate_removed += 1
            continue
        cleaned.append(tri)

    # Phase 2: Find and close boundary holes
    holes_closed = 0
    new_triangles: list[tuple[tuple[float, ...], ...]] = []

    if close_holes and cleaned:
        # Find boundary edges (edges with exactly 1 adjacent triangle)
        edge_count: dict[tuple[tuple[float, ...], tuple[float, ...]], int] = {}
        # Track directed edges for winding order
        directed: dict[tuple[tuple[float, ...], tuple[float, ...]], bool] = {}

        for tri in cleaned:
            for j in range(3):
                va, vb = tri[j], tri[(j + 1) % 3]
                edge = (min(va, vb), max(va, vb))
                edge_count[edge] = edge_count.get(edge, 0) + 1
                directed[(va, vb)] = True

        boundary_edges: list[tuple[tuple[float, ...], tuple[float, ...]]] = []
        for edge, count in edge_count.items():
            if count == 1:
                # Determine correct direction from the directed edge map
                if (edge[0], edge[1]) in directed:
                    # Reverse to close the hole (opposite winding)
                    boundary_edges.append((edge[1], edge[0]))
                else:
                    boundary_edges.append((edge[0], edge[1]))

        # Try to form loops from boundary edges
        if boundary_edges:
            loops = _find_boundary_loops(boundary_edges)
            for loop in loops:
                if len(loop) < 3 or len(loop) > 50:
                    continue  # Skip trivially small or huge holes
                # Fan triangulate from first vertex
                center = loop[0]
                for i in range(1, len(loop) - 1):
                    new_triangles.append((center, loop[i], loop[i + 1]))
                holes_closed += 1

    all_tris = cleaned + new_triangles
    out = output_path or file_path
    _write_binary_stl(all_tris, out)

    return {
        "path": out,
        "original_triangles": len(triangles),
        "cleaned_triangles": len(cleaned),
        "degenerate_removed": degenerate_removed,
        "holes_closed": holes_closed,
        "triangles_added": len(new_triangles),
        "final_triangles": len(all_tris),
        **_residual_defect_report(all_tris, hole_pass_ran=close_holes),
    }


def _find_boundary_loops(
    edges: list[tuple[tuple[float, ...], tuple[float, ...]]],
) -> list[list[tuple[float, ...]]]:
    """Find closed loops from a set of directed boundary edges.

    Returns a list of vertex loops (each a list of vertices forming
    a closed boundary).
    """
    # Build adjacency: vertex → next vertex
    adj: dict[tuple[float, ...], tuple[float, ...]] = {}
    for a, b in edges:
        adj[a] = b

    visited: set[tuple[float, ...]] = set()
    loops: list[list[tuple[float, ...]]] = []

    for start in adj:
        if start in visited:
            continue
        loop: list[tuple[float, ...]] = []
        current = start
        for _ in range(len(adj) + 1):  # safety limit
            if current in visited and current != start:
                break
            if current == start and len(loop) > 0:
                break
            visited.add(current)
            loop.append(current)
            nxt = adj.get(current)
            if nxt is None:
                break
            current = nxt

        if len(loop) >= 3:
            loops.append(loop)

    return loops


# ---------------------------------------------------------------------------
# Mesh comparison / diff
# ---------------------------------------------------------------------------


def compare_meshes(
    file_a: str,
    file_b: str,
) -> dict[str, Any]:
    """Compare two mesh files and report geometric differences.

    Computes bounding box deltas, volume change, surface area change,
    triangle count change, center-of-mass shift, and a sampled
    Hausdorff-like distance (how far the meshes differ spatially).

    Works with STL, OBJ, and GLB files.

    Args:
        file_a: Path to the first (reference) mesh.
        file_b: Path to the second (modified) mesh.

    Returns:
        Dict with comparison metrics.
    """
    a = analyze_mesh(file_a)
    b = analyze_mesh(file_b)

    if a.printability_issues and not a.triangle_count:
        raise ValueError(f"Cannot parse reference mesh: {a.printability_issues}")
    if b.printability_issues and not b.triangle_count:
        raise ValueError(f"Cannot parse comparison mesh: {b.printability_issues}")

    result: dict[str, Any] = {
        "triangle_count_a": a.triangle_count,
        "triangle_count_b": b.triangle_count,
        "triangle_count_delta": b.triangle_count - a.triangle_count,
        "volume_a_mm3": a.volume_mm3,
        "volume_b_mm3": b.volume_mm3,
        "volume_delta_mm3": round(b.volume_mm3 - a.volume_mm3, 2),
        "volume_change_pct": round(
            (b.volume_mm3 - a.volume_mm3) / a.volume_mm3 * 100, 1
        )
        if a.volume_mm3 > 0
        else 0.0,
        "surface_area_a_mm2": a.surface_area_mm2,
        "surface_area_b_mm2": b.surface_area_mm2,
        "surface_area_delta_mm2": round(b.surface_area_mm2 - a.surface_area_mm2, 2),
    }

    # Dimension deltas
    if a.dimensions_mm and b.dimensions_mm:
        result["dimensions_delta_mm"] = {
            k: round(b.dimensions_mm[k] - a.dimensions_mm[k], 2)
            for k in a.dimensions_mm
        }

    # Center of mass shift
    if a.center_of_mass and b.center_of_mass:
        dx = b.center_of_mass["x"] - a.center_of_mass["x"]
        dy = b.center_of_mass["y"] - a.center_of_mass["y"]
        dz = b.center_of_mass["z"] - a.center_of_mass["z"]
        result["center_of_mass_shift_mm"] = round(
            math.sqrt(dx * dx + dy * dy + dz * dz), 2
        )

    # Printability comparison
    result["printability_score_a"] = a.printability_score
    result["printability_score_b"] = b.printability_score
    result["printability_delta"] = b.printability_score - a.printability_score
    result["overhang_pct_a"] = a.overhang_percentage
    result["overhang_pct_b"] = b.overhang_percentage

    # Sampled Hausdorff-like distance: sample centroids from each mesh
    # and find the max nearest-centroid distance
    hausdorff = _sampled_hausdorff(file_a, file_b)
    if hausdorff is not None:
        result["hausdorff_distance_mm"] = hausdorff

    # Meshes are identical only if tri count, volume, surface area match AND
    # the geometric distance is negligible (catches mirrors, translations, etc.)
    hausdorff_ok = hausdorff is not None and hausdorff < 0.01
    result["meshes_identical"] = (
        a.triangle_count == b.triangle_count
        and abs(a.volume_mm3 - b.volume_mm3) < 0.01
        and abs(a.surface_area_mm2 - b.surface_area_mm2) < 0.01
        and hausdorff_ok
    )

    return result


def _sampled_hausdorff(file_a: str, file_b: str, *, max_samples: int = 500) -> float | None:
    """Approximate one-directional Hausdorff distance via triangle centroids."""
    path_a, path_b = Path(file_a), Path(file_b)
    errors: list[str] = []

    tris_a = _load_triangles(path_a, errors)
    if errors or not tris_a:
        return None
    errors.clear()
    tris_b = _load_triangles(path_b, errors)
    if errors or not tris_b:
        return None

    # Compute centroids
    def centroids(tris: list[tuple[tuple[float, ...], ...]]) -> list[tuple[float, float, float]]:
        return [
            (
                (t[0][0] + t[1][0] + t[2][0]) / 3.0,
                (t[0][1] + t[1][1] + t[2][1]) / 3.0,
                (t[0][2] + t[1][2] + t[2][2]) / 3.0,
            )
            for t in tris
        ]

    ca = centroids(tris_a)
    cb = centroids(tris_b)

    # Subsample if too large
    step_a = max(1, len(ca) // max_samples)
    step_b = max(1, len(cb) // max_samples)
    ca_s = ca[::step_a]
    cb_s = cb[::step_b]

    # For each centroid in A, find nearest in B
    max_dist = 0.0
    for pa in ca_s:
        best = float("inf")
        for pb in cb_s:
            d = (pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2 + (pa[2] - pb[2]) ** 2
            if d < best:
                best = d
        dist = math.sqrt(best)
        if dist > max_dist:
            max_dist = dist

    return round(max_dist, 3)


def _load_triangles(
    path: Path, errors: list[str]
) -> list[tuple[tuple[float, ...], ...]]:
    """Load triangles from any supported format."""
    ext = path.suffix.lower()
    if ext == ".stl":
        tris, _ = _parse_stl(path, errors)
    elif ext == ".obj":
        tris, _ = _parse_obj(path, errors)
    elif ext == ".glb":
        tris, _ = _parse_glb(path, errors)
    else:
        errors.append(f"Unsupported format: {ext}")
        return []
    return tris


# ---------------------------------------------------------------------------
# Print failure prediction
# ---------------------------------------------------------------------------


def predict_print_failures(
    file_path: str,
    *,
    min_wall_mm: float = 0.8,
    max_bridge_mm: float = 15.0,
    max_overhang_deg: float = 55.0,
) -> dict[str, Any]:
    """Predict common 3D printing failure modes from mesh geometry.

    Detects:
    - Thin walls (below minimum printable thickness)
    - Long unsupported bridges
    - Severe overhangs
    - Sharp internal corners (stress concentrators)
    - Small features that may not resolve
    - Top-heavy geometry (tip-over risk)

    Args:
        file_path: Path to mesh file.
        min_wall_mm: Minimum printable wall thickness.
        max_bridge_mm: Maximum unsupported bridge length.
        max_overhang_deg: Maximum overhang angle before failure.

    Returns:
        Dict with failure predictions and risk scores.
    """
    path = Path(file_path)
    errors: list[str] = []
    tris = _load_triangles(path, errors)
    if errors or not tris:
        raise ValueError(f"Cannot parse mesh: {errors or ['No geometry']}")

    # Gather all vertices for bounding box
    all_verts: list[tuple[float, ...]] = []
    for tri in tris:
        all_verts.extend(tri)
    bbox = _bounding_box(all_verts)

    dims = {
        "width": bbox["x_max"] - bbox["x_min"],
        "depth": bbox["y_max"] - bbox["y_min"],
        "height": bbox["z_max"] - bbox["z_min"],
    }

    failures: list[dict[str, Any]] = []
    risk_score = 0  # 0=safe, 100=will fail

    # 1. Thin wall detection via edge length analysis
    edge_lengths: list[float] = []
    for tri in tris:
        for j in range(3):
            va, vb = tri[j], tri[(j + 1) % 3]
            dx = vb[0] - va[0]
            dy = vb[1] - va[1]
            dz = vb[2] - va[2]
            edge_lengths.append(math.sqrt(dx * dx + dy * dy + dz * dz))

    if edge_lengths:
        min_edge = min(edge_lengths)
        # Very short edges suggest thin geometry
        thin_edges = sum(1 for e in edge_lengths if e < min_wall_mm)
        thin_pct = thin_edges / len(edge_lengths) * 100
        if thin_pct > 5:
            failures.append({
                "type": "thin_walls",
                "severity": "high" if thin_pct > 20 else "medium",
                "detail": f"{thin_pct:.0f}% of edges below {min_wall_mm}mm (min edge: {min_edge:.2f}mm)",
                "suggestion": f"Increase wall thickness to at least {min_wall_mm}mm",
            })
            risk_score += 20 if thin_pct > 20 else 10

    # 2. Overhang analysis
    overhang_count = 0
    severe_count = 0
    max_angle = 0.0
    for tri in tris:
        v0, v1, v2 = tri
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        cz = e1[0] * e2[1] - e1[1] * e2[0]
        area_2 = math.sqrt(
            (e1[1] * e2[2] - e1[2] * e2[1]) ** 2
            + (e1[2] * e2[0] - e1[0] * e2[2]) ** 2
            + cz ** 2
        )
        if area_2 < 1e-10:
            continue
        nz = (e1[0] * e2[1] - e1[1] * e2[0]) / area_2
        if nz < 0:
            angle = math.degrees(math.acos(max(-1.0, min(1.0, -nz))))
            overhang_angle = 90.0 - angle
            if overhang_angle > max_angle:
                max_angle = overhang_angle
            if overhang_angle > max_overhang_deg:
                severe_count += 1
            if overhang_angle > 45:
                overhang_count += 1

    if severe_count > 0:
        failures.append({
            "type": "severe_overhangs",
            "severity": "high",
            "detail": f"{severe_count} faces exceed {max_overhang_deg}° (max: {max_angle:.0f}°)",
            "suggestion": "Add supports or redesign to reduce overhangs",
        })
        risk_score += 20
    elif overhang_count > len(tris) * 0.1:
        failures.append({
            "type": "moderate_overhangs",
            "severity": "medium",
            "detail": f"{overhang_count} overhang faces (max: {max_angle:.0f}°)",
            "suggestion": "Consider supports or orientation optimization",
        })
        risk_score += 10

    # 3. Bridging detection (long horizontal spans on interior ceiling faces)
    # True bridges are flat downward-facing faces in the model interior.
    # The topmost face at z_max is always supported by layers below and
    # should not be flagged.
    z_max = max(v[2] for tri in tris for v in tri) if tris else 0.0
    long_bridges = 0
    max_bridge = 0.0
    for tri in tris:
        v0, v1, v2 = tri
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        nz = e1[0] * e2[1] - e1[1] * e2[0]  # z-component of cross product
        if nz >= -0.1:
            continue  # face isn't downward-facing — not a bridge candidate

        # Skip faces at/near the top of the model — they're supported by
        # the layer stack below them.
        face_z = (v0[2] + v1[2] + v2[2]) / 3.0
        if face_z >= z_max - 0.5:
            continue

        for j in range(3):
            va, vb = tri[j], tri[(j + 1) % 3]
            z_diff = abs(va[2] - vb[2])
            avg_z = (va[2] + vb[2]) / 2.0
            if z_diff < 0.5 and avg_z > 1.0:
                span = math.sqrt(
                    (vb[0] - va[0]) ** 2 + (vb[1] - va[1]) ** 2
                )
                if span > max_bridge:
                    max_bridge = span
                if span > max_bridge_mm:
                    long_bridges += 1

    if long_bridges > 0:
        failures.append({
            "type": "long_bridges",
            "severity": "high" if max_bridge > max_bridge_mm * 2 else "medium",
            "detail": f"{long_bridges} bridges exceed {max_bridge_mm}mm (max: {max_bridge:.1f}mm)",
            "suggestion": "Add supports under bridges or split into multiple parts",
        })
        risk_score += 15 if max_bridge > max_bridge_mm * 2 else 8

    # 4. Top-heavy / tip-over risk
    # Compare center of mass height to footprint size
    analysis = analyze_mesh(file_path)
    if analysis.center_of_mass and analysis.dimensions_mm:
        com_z = analysis.center_of_mass["z"]
        footprint = min(
            analysis.dimensions_mm["width_mm"],
            analysis.dimensions_mm["depth_mm"],
        )
        height = analysis.dimensions_mm["height_mm"]
        if height > 0 and footprint > 0:
            stability_ratio = footprint / height
            if stability_ratio < 0.3 and com_z > height * 0.6:
                failures.append({
                    "type": "top_heavy",
                    "severity": "medium",
                    "detail": (
                        f"Narrow base ({footprint:.1f}mm) with high center of mass "
                        f"({com_z:.1f}mm / {height:.1f}mm height)"
                    ),
                    "suggestion": "Widen the base or add a brim for stability",
                })
                risk_score += 10

    # 5. Very small features
    min_dim = min(dims.values())
    if min_dim < 1.0:
        failures.append({
            "type": "small_features",
            "severity": "high" if min_dim < 0.4 else "medium",
            "detail": f"Minimum dimension {min_dim:.2f}mm may not resolve",
            "suggestion": "Scale up or increase feature size for reliable printing",
        })
        risk_score += 15 if min_dim < 0.4 else 5

    # 6. Non-manifold / disconnected components
    if not analysis.is_manifold:
        failures.append({
            "type": "non_manifold",
            "severity": "medium",
            "detail": "Mesh is not watertight — slicers may produce artifacts",
            "suggestion": "Run repair_mesh_advanced() to fix topology",
        })
        risk_score += 10

    if analysis.connected_components > 1:
        failures.append({
            "type": "disconnected_parts",
            "severity": "low",
            "detail": f"{analysis.connected_components} separate components will print independently",
            "suggestion": "Verify this is intentional or merge components",
        })
        risk_score += 5

    risk_score = min(100, risk_score)

    # Overall verdict
    if risk_score >= 50:
        verdict = "high_risk"
    elif risk_score >= 25:
        verdict = "moderate_risk"
    elif risk_score > 0:
        verdict = "low_risk"
    else:
        verdict = "likely_success"

    return {
        "verdict": verdict,
        "risk_score": risk_score,
        "failure_count": len(failures),
        "failures": failures,
        "dimensions_mm": dims,
        "triangle_count": len(tris),
        "printability_score": analysis.printability_score,
    }


# ---------------------------------------------------------------------------
# Mesh simplification (vertex decimation)
# ---------------------------------------------------------------------------


def simplify_mesh(
    file_path: str,
    *,
    target_ratio: float = 0.5,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Reduce triangle count via edge-collapse decimation.

    A simple vertex-clustering approach: divides the bounding box into
    a grid and merges vertices that fall into the same cell.  Fast and
    deterministic but produces lower quality than quadric-based methods.

    Useful for generating quick previews or reducing file size before
    upload.

    Args:
        file_path: Path to the STL file.
        target_ratio: Target triangle count as fraction of original
            (0.5 = keep ~50%).  Clamped to [0.01, 1.0].
        output_path: Output path.  Defaults to ``<name>_simplified.stl``.

    Returns:
        Dict with simplification statistics.
    """
    target_ratio = max(0.01, min(1.0, target_ratio))

    path = Path(file_path)
    errors: list[str] = []
    triangles, vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    original_count = len(triangles)

    if target_ratio >= 0.99:
        # No simplification needed
        if output_path:
            _write_binary_stl(triangles, output_path)
        return {
            "path": output_path or file_path,
            "original_triangles": original_count,
            "simplified_triangles": original_count,
            "reduction_pct": 0.0,
        }

    bbox = _bounding_box(vertices)
    dx = bbox["x_max"] - bbox["x_min"]
    dy = bbox["y_max"] - bbox["y_min"]
    dz = bbox["z_max"] - bbox["z_min"]
    max_dim = max(dx, dy, dz, 0.001)

    # Grid resolution: more cells = less simplification
    # Rough heuristic: cells ≈ cube root of target vertex count
    target_verts = int(len(vertices) * target_ratio)
    grid_res = max(4, int(target_verts ** (1.0 / 3.0)))
    cell_size = max_dim / grid_res

    # Cluster vertices into grid cells
    def cell_key(v: tuple[float, ...]) -> tuple[int, int, int]:
        return (
            int((v[0] - bbox["x_min"]) / cell_size),
            int((v[1] - bbox["y_min"]) / cell_size),
            int((v[2] - bbox["z_min"]) / cell_size),
        )

    # Build cell → representative vertex mapping
    cell_verts: dict[tuple[int, int, int], list[tuple[float, ...]]] = {}
    for v in vertices:
        ck = cell_key(v)
        if ck not in cell_verts:
            cell_verts[ck] = []
        cell_verts[ck].append(v)

    # Representative = centroid of vertices in each cell
    cell_rep: dict[tuple[int, int, int], tuple[float, ...]] = {}
    for ck, vlist in cell_verts.items():
        n = len(vlist)
        cell_rep[ck] = (
            sum(v[0] for v in vlist) / n,
            sum(v[1] for v in vlist) / n,
            sum(v[2] for v in vlist) / n,
        )

    # Rebuild triangles with representative vertices, skip degenerate
    simplified: list[tuple[tuple[float, ...], ...]] = []
    for tri in triangles:
        new_tri = tuple(cell_rep[cell_key(v)] for v in tri)
        # Skip if vertices collapsed to same point
        if new_tri[0] == new_tri[1] or new_tri[1] == new_tri[2] or new_tri[0] == new_tri[2]:
            continue
        simplified.append(new_tri)

    if output_path is None:
        output_path = str(path.with_name(f"{path.stem}_simplified.stl"))

    _write_binary_stl(simplified, output_path)

    return {
        "path": output_path,
        "original_triangles": original_count,
        "simplified_triangles": len(simplified),
        "reduction_pct": round(
            (1.0 - len(simplified) / original_count) * 100, 1
        )
        if original_count > 0
        else 0.0,
        "original_vertices": len(vertices),
        "grid_cells": len(cell_rep),
    }


# ---------------------------------------------------------------------------
# Multi-factor design scorecard
# ---------------------------------------------------------------------------
#
# Tiering: the SCALE is not a tier feature.  Every tier combines the
# four factors with the same weights and reads the same grade ladder,
# because a letter grade is a claim about the part — "B" has to mean
# the same thing to everyone or it means nothing.  Two earlier ladders
# (a 25/25/25/25 blend and an A=80 ladder here, an A=90 ladder in the
# overlay) graded the same 11:1 narrow-base part "A" without a licence
# and "B" with one, off identical per-factor scores and identical
# notes.  The scale was the only difference, and it read generous in
# exactly the direction that hurts: the lighter printability weight
# discounted the factor that catches overhangs and non-manifold walls.
#
# What the ``scorecard_weights`` overlay legitimately still supplies is
# DEPTH: curated per-factor deduction rules and their notes, which
# change what gets flagged rather than what a letter is worth.  An
# overlay may override any value below; these are the floor everyone
# gets.  The weights here match the ones the ``mesh_quality_scorecard``
# tool docstring publishes to every caller.


_OVERALL_WEIGHTS_PUBLIC: dict[str, float] = {
    "printability": 0.35,
    "structural": 0.25,
    "efficiency": 0.20,
    "quality": 0.20,
}

_GRADE_THRESHOLDS_PUBLIC: dict[str, int] = {
    "A": 90,
    "B": 80,
    "C": 65,
    "D": 50,
}

# Rule order = severity order.  First matching rule per metric wins,
# so put more-severe thresholds first.
_STRUCTURAL_DEDUCTIONS_PUBLIC: list[dict[str, Any]] = [
    {"metric": "aspect_ratio",       "operator": ">",  "threshold": 10,    "deduction": -15, "note_template": "Extreme aspect ratio ({value:.0f}:1)"},
    {"metric": "aspect_ratio",       "operator": ">",  "threshold": 5,     "deduction": -5,  "note_template": "High aspect ratio ({value:.1f}:1)"},
    {"metric": "min_base_to_height", "operator": "<",  "threshold": 0.2,   "deduction": -10, "note_template": "Narrow base relative to height"},
    {"metric": "components",         "operator": ">",  "threshold": 3,     "deduction": -10, "note_template": "{value} disconnected parts"},
    {"metric": "components",         "operator": ">",  "threshold": 1,     "deduction": -5,  "note_template": None},
    {"metric": "is_manifold",        "operator": "==", "threshold": False, "deduction": -5,  "note_template": "Non-manifold mesh"},
]

_EFFICIENCY_DEDUCTIONS_PUBLIC: list[dict[str, Any]] = [
    {"metric": "fill_ratio",   "operator": "<", "threshold": 0.05, "deduction": -10, "note_template": "Very low fill ratio ({value:.1%})"},
    {"metric": "fill_ratio",   "operator": "<", "threshold": 0.15, "deduction": -5,  "note_template": "Low fill ratio ({value:.1%})"},
    {"metric": "overhang_pct", "operator": ">", "threshold": 30,   "deduction": -10, "note_template": "High overhangs ({value:.0f}%)"},
    {"metric": "overhang_pct", "operator": ">", "threshold": 15,   "deduction": -5,  "note_template": None},
]

_QUALITY_DEDUCTIONS_PUBLIC: list[dict[str, Any]] = [
    {"metric": "avg_tri_area_mm2", "operator": ">", "threshold": 50, "deduction": -10, "note_template": "Low mesh resolution (large triangles)"},
    {"metric": "avg_tri_area_mm2", "operator": ">", "threshold": 20, "deduction": -5,  "note_template": "Moderate mesh resolution"},
    {"metric": "degenerate_pct",   "operator": ">", "threshold": 5,  "deduction": -10, "note_template": "Degenerate triangles ({value:.1f}%)"},
    {"metric": "degenerate_pct",   "operator": ">", "threshold": 0,  "deduction": -5,  "note_template": None},
]


def _check_scorecard_op(op: str, value: Any, threshold: Any) -> bool:
    """Compare ``value`` to ``threshold`` per ``op``.  Centralised so
    any future rule type maps to one tested function."""
    if op == ">":
        return value > threshold
    if op == "<":
        return value < threshold
    if op == ">=":
        return value >= threshold
    if op == "<=":
        return value <= threshold
    if op == "==":
        return value == threshold
    return False


def _extract_scorecard_metric(
    name: str, analysis: Any,
) -> float | int | bool | None:
    """Pull a metric value out of the ``MeshAnalysis`` dataclass.

    Returns ``None`` when the metric isn't applicable to the analysis
    (e.g. ``dimensions_mm`` missing for an aspect-ratio metric) so the
    rule iterator skips it cleanly rather than firing on a bogus value.
    """
    needs_dims = name in (
        "aspect_ratio", "min_base_to_height", "fill_ratio", "overhang_pct",
    )
    if needs_dims and not analysis.dimensions_mm:
        return None
    if analysis.dimensions_mm:
        w = analysis.dimensions_mm["width_mm"]
        d = analysis.dimensions_mm["depth_mm"]
        h = analysis.dimensions_mm["height_mm"]
    else:
        w = d = h = 0.0

    if name == "aspect_ratio":
        return max(w, d, h) / max(min(w, d, h), 0.01)
    if name == "min_base_to_height":
        return min(w, d) / max(h, 0.01)
    if name == "components":
        return analysis.connected_components
    if name == "is_manifold":
        return analysis.is_manifold
    if name == "fill_ratio":
        bbox_vol = w * d * h
        if bbox_vol <= 0 or analysis.volume_mm3 <= 0:
            return None
        return analysis.volume_mm3 / bbox_vol
    if name == "overhang_pct":
        return analysis.overhang_percentage
    if name == "avg_tri_area_mm2":
        if analysis.triangle_count > 0 and analysis.surface_area_mm2 > 0:
            return analysis.surface_area_mm2 / analysis.triangle_count
        return None
    if name == "degenerate_pct":
        if analysis.triangle_count > 0:
            return analysis.degenerate_triangles / analysis.triangle_count * 100
        return None
    return None


def _score_factor_from_rules(
    rules: list[dict[str, Any]], analysis: Any,
) -> tuple[int, list[str]]:
    """Apply deduction rules to one factor, return (score, notes).

    Rules are evaluated in order; the FIRST matching rule per metric
    wins (so severity-first ordering = max one rule per metric).  An
    unknown metric or operator is silently skipped — adding a new rule
    type in the Pro overlay does not break public.
    """
    score = 100
    notes: list[str] = []
    fired: set[str] = set()

    for rule in rules:
        metric = rule.get("metric")
        if metric in fired:
            continue
        value = _extract_scorecard_metric(metric, analysis)
        if value is None:
            continue
        if not _check_scorecard_op(
            rule.get("operator", ">"), value, rule.get("threshold"),
        ):
            continue
        score += rule.get("deduction", 0)
        fired.add(metric)
        tpl = rule.get("note_template")
        if tpl:
            try:
                notes.append(tpl.format(value=value))
            except (KeyError, IndexError, ValueError):
                notes.append(tpl)

    return max(0, score), notes


def design_scorecard(file_path: str) -> dict[str, Any]:
    """Generate a multi-factor quality scorecard for a mesh.

    Evaluates four factors (each 0-100):

    - **Printability**: overhangs, manifold, supports needed
    - **Structural**: aspect ratio, base stability, component count
    - **Efficiency**: fill ratio, overhang waste
    - **Quality**: triangle density, degenerate count

    The score and the letter mean the same thing at every tier: the
    35/25/20/20 weighting and the A>=90 / B>=80 / C>=65 / D>=50 ladder
    are the floor everyone gets.  What the ``scorecard_weights``
    overlay adds for Pro+ is depth — curated per-factor deduction
    rules and the notes that come with them, which change what gets
    flagged, not what a grade is worth.

    Args:
        file_path: Path to mesh file.

    Returns:
        Dict with per-factor scores, overall score, and grade.  Shape
        is identical between tiers; only values differ.
    """
    from kiln.design_intelligence import load_pro_overlay_or_empty

    analysis = analyze_mesh(file_path)
    if analysis.printability_issues and not analysis.triangle_count:
        raise ValueError(f"Cannot analyze mesh: {analysis.printability_issues}")

    overlay = load_pro_overlay_or_empty("scorecard_weights")

    # --- Printability (already a 0-100 score from the upstream analysis) ---
    printability = analysis.printability_score

    # --- Structural / Efficiency / Quality (overlay-driven rules) -------
    structural, structural_notes = _score_factor_from_rules(
        rules=overlay.get("structural_deductions") or _STRUCTURAL_DEDUCTIONS_PUBLIC,
        analysis=analysis,
    )
    efficiency, efficiency_notes = _score_factor_from_rules(
        rules=overlay.get("efficiency_deductions") or _EFFICIENCY_DEDUCTIONS_PUBLIC,
        analysis=analysis,
    )
    quality, quality_notes = _score_factor_from_rules(
        rules=overlay.get("quality_deductions") or _QUALITY_DEDUCTIONS_PUBLIC,
        analysis=analysis,
    )

    # --- Overall (weighted combination) ---------------------------------
    weights = overlay.get("overall_weights") or _OVERALL_WEIGHTS_PUBLIC
    overall = round(
        printability * weights["printability"]
        + structural * weights["structural"]
        + efficiency * weights["efficiency"]
        + quality * weights["quality"]
    )

    # --- Grade ladder ---------------------------------------------------
    thresholds = overlay.get("grade_thresholds") or _GRADE_THRESHOLDS_PUBLIC
    if overall >= thresholds["A"]:
        grade = "A"
    elif overall >= thresholds["B"]:
        grade = "B"
    elif overall >= thresholds["C"]:
        grade = "C"
    elif overall >= thresholds["D"]:
        grade = "D"
    else:
        grade = "F"

    return {
        "overall_score": overall,
        "grade": grade,
        "printability": {"score": printability, "notes": analysis.printability_issues},
        "structural": {"score": structural, "notes": structural_notes},
        "efficiency": {"score": efficiency, "notes": efficiency_notes},
        "quality": {"score": quality, "notes": quality_notes},
        "triangle_count": analysis.triangle_count,
        "volume_mm3": analysis.volume_mm3,
        "dimensions_mm": analysis.dimensions_mm,
    }


# ---------------------------------------------------------------------------
# Material cost estimation
# ---------------------------------------------------------------------------

# Common FDM material densities (g/cm³) and approximate costs ($/kg)
_MATERIAL_DB: dict[str, dict[str, float]] = {
    "pla": {"density": 1.24, "cost_per_kg": 20.0},
    "petg": {"density": 1.27, "cost_per_kg": 22.0},
    "abs": {"density": 1.04, "cost_per_kg": 18.0},
    "tpu": {"density": 1.21, "cost_per_kg": 30.0},
    "asa": {"density": 1.07, "cost_per_kg": 25.0},
    "nylon": {"density": 1.14, "cost_per_kg": 35.0},
    "pc": {"density": 1.20, "cost_per_kg": 40.0},
    "pla+": {"density": 1.24, "cost_per_kg": 22.0},
    "carbon_fiber_pla": {"density": 1.30, "cost_per_kg": 45.0},
}


def estimate_material_cost(
    file_path: str,
    *,
    material: str = "pla",
    infill_pct: float = 20.0,
    wall_layers: int = 3,
    layer_height_mm: float = 0.2,
    nozzle_mm: float = 0.4,
    cost_per_kg: float | None = None,
) -> dict[str, Any]:
    """Estimate material usage and cost for printing a mesh.

    Uses mesh volume + infill percentage to approximate filament
    consumption.  Accounts for wall shells and infill separately.

    Args:
        file_path: Path to mesh file.
        material: Material type (pla, petg, abs, tpu, etc.).
        infill_pct: Interior fill percentage (0-100).
        wall_layers: Number of perimeter shells.
        layer_height_mm: Layer height.
        nozzle_mm: Nozzle diameter.
        cost_per_kg: Override material cost ($/kg).

    Returns:
        Dict with weight, filament length, and cost estimates.
    """
    analysis = analyze_mesh(file_path)
    if analysis.volume_mm3 <= 0:
        raise ValueError("Cannot estimate cost: mesh has no volume")

    mat = _MATERIAL_DB.get(material.lower(), _MATERIAL_DB["pla"])
    density = mat["density"]
    price = cost_per_kg if cost_per_kg is not None else mat["cost_per_kg"]

    # Approximate solid shell volume
    # Shell thickness ≈ wall_layers × nozzle_mm
    shell_thickness = wall_layers * nozzle_mm

    # For a rough estimate: shell volume ≈ surface_area × shell_thickness
    # Interior volume ≈ total_volume - shell_volume
    shell_vol_mm3 = analysis.surface_area_mm2 * shell_thickness
    interior_vol_mm3 = max(0, analysis.volume_mm3 - shell_vol_mm3)

    # Actual plastic used
    shell_plastic = shell_vol_mm3  # shells are solid
    infill_plastic = interior_vol_mm3 * (infill_pct / 100.0)
    total_plastic_mm3 = shell_plastic + infill_plastic

    # Convert to real units
    total_plastic_cm3 = total_plastic_mm3 / 1000.0
    weight_g = total_plastic_cm3 * density

    # Filament length: volume / cross-section area of filament (1.75mm dia)
    filament_diameter = 1.75  # mm
    filament_cross_section = math.pi * (filament_diameter / 2) ** 2  # mm²
    filament_length_mm = total_plastic_mm3 / filament_cross_section
    filament_length_m = filament_length_mm / 1000.0

    cost = weight_g / 1000.0 * price

    return {
        "material": material.lower(),
        "volume_mm3": round(analysis.volume_mm3, 1),
        "plastic_volume_mm3": round(total_plastic_mm3, 1),
        "shell_volume_mm3": round(shell_vol_mm3, 1),
        "infill_volume_mm3": round(infill_plastic, 1),
        "weight_g": round(weight_g, 1),
        "filament_length_m": round(filament_length_m, 2),
        "estimated_cost_usd": round(cost, 2),
        "infill_pct": infill_pct,
        "density_g_cm3": density,
        "cost_per_kg_usd": price,
    }


# ---------------------------------------------------------------------------
# Floating region removal
# ---------------------------------------------------------------------------


def remove_floating_regions(
    file_path: str,
    *,
    output_path: str | None = None,
    keep_largest: bool = True,
    min_triangle_pct: float = 1.0,
) -> dict[str, Any]:
    """Remove small disconnected components (floating geometry).

    Uses union-find to identify connected components, then keeps only
    the largest (or all components above a minimum triangle threshold).

    Args:
        file_path: Path to the STL file.
        output_path: Output path.  Defaults to overwriting input.
        keep_largest: If True, keep only the single largest component.
            If False, keep all components with >= ``min_triangle_pct``
            percent of total triangles.
        min_triangle_pct: Minimum triangle percentage to keep a
            component (only used when ``keep_largest=False``).

    Returns:
        Dict with removal statistics.
    """
    path = Path(file_path)
    errors: list[str] = []
    triangles, _ = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    n = len(triangles)

    # Union-find
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    edge_to_tri: dict[tuple[tuple[float, ...], tuple[float, ...]], int] = {}
    for i, tri in enumerate(triangles):
        for j in range(3):
            va, vb = tri[j], tri[(j + 1) % 3]
            edge = (min(va, vb), max(va, vb))
            if edge in edge_to_tri:
                union(i, edge_to_tri[edge])
            else:
                edge_to_tri[edge] = i

    # Group triangles by component
    components: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        if root not in components:
            components[root] = []
        components[root].append(i)

    total_components = len(components)

    if total_components <= 1:
        # Nothing to remove
        out = output_path or file_path
        if output_path and output_path != file_path:
            _write_binary_stl(triangles, out)
        return {
            "path": out,
            "original_triangles": n,
            "kept_triangles": n,
            "removed_triangles": 0,
            "original_components": 1,
            "kept_components": 1,
            "removed_components": 0,
        }

    # Sort components by size (largest first)
    sorted_comps = sorted(components.values(), key=len, reverse=True)

    if keep_largest:
        keep_indices = set(sorted_comps[0])
    else:
        threshold = n * (min_triangle_pct / 100.0)
        keep_indices: set[int] = set()
        for comp in sorted_comps:
            if len(comp) >= threshold:
                keep_indices.update(comp)

    kept = [triangles[i] for i in range(n) if i in keep_indices]
    removed = n - len(kept)

    kept_comp_count = sum(
        1 for comp in sorted_comps
        if any(i in keep_indices for i in comp)
    )

    out = output_path or file_path
    _write_binary_stl(kept, out)

    return {
        "path": out,
        "original_triangles": n,
        "kept_triangles": len(kept),
        "removed_triangles": removed,
        "original_components": total_components,
        "kept_components": kept_comp_count,
        "removed_components": total_components - kept_comp_count,
    }


# ---------------------------------------------------------------------------
# Unified print-readiness gate
# ---------------------------------------------------------------------------


def can_print_now(
    file_path: str,
    *,
    auto_fix: bool = False,
    output_path: str | None = None,
    printer_bed_mm: tuple[float, float, float] | None = None,
    printer_id: str | None = None,
) -> dict[str, Any]:
    """Single-call print readiness check with optional auto-repair.

    Runs the full validation battery:
    1. Mesh parseable and non-empty
    2. Manifold (watertight)
    3. No floating regions
    4. Overhangs within limits
    5. Fits on build plate
    6. No degenerate triangles

    When ``auto_fix=True``, attempts to fix issues in-place:
    - Removes degenerate triangles
    - Closes small holes
    - Removes floating regions

    Args:
        file_path: Path to mesh file.
        auto_fix: Whether to attempt automatic repairs.
        output_path: Where to write the fixed file (only used with auto_fix).
        printer_bed_mm: Build volume as (x, y, z) in mm.
            Defaults to a legacy 256mm cube only when no printer_id or
            explicit bed is provided.
        printer_id: Optional supported printer model id.  When provided and
            ``printer_bed_mm`` is omitted, printer intelligence supplies the
            build volume.

    Returns:
        Dict with pass/fail verdict, issues found, and actions taken.
    """
    if printer_bed_mm is None and printer_id:
        from kiln.printers.bed_fit import get_build_volume

        printer_bed_mm = get_build_volume(printer_id)
        if printer_bed_mm is None:
            return {
                "can_print": False,
                "verdict": "unknown_printer_bed",
                "issues": [{
                    "type": "unknown_printer_bed",
                    "detail": (
                        f"Unknown printer_id {printer_id!r}; pass "
                        "printer_bed_mm explicitly or use a supported "
                        "printer model id."
                    ),
                }],
                "actions_taken": [],
            }
    if printer_bed_mm is None:
        printer_bed_mm = (256.0, 256.0, 256.0)

    issues: list[dict[str, str]] = []
    actions_taken: list[str] = []
    working_path = file_path

    # Step 1: Basic parse check
    analysis = analyze_mesh(file_path)
    if analysis.printability_issues and not analysis.triangle_count:
        return {
            "can_print": False,
            "verdict": "unprintable",
            "issues": [{"type": "parse_failure", "detail": str(analysis.printability_issues)}],
            "actions_taken": [],
        }

    # Step 2: Auto-fix pass (if requested)
    if auto_fix:
        out = output_path or file_path
        # Advanced repair: degenerate removal + hole closing
        try:
            repair_result = repair_stl_advanced(working_path, output_path=out)
            if repair_result["degenerate_removed"] > 0:
                actions_taken.append(
                    f"Removed {repair_result['degenerate_removed']} degenerate triangles"
                )
            if repair_result["holes_closed"] > 0:
                actions_taken.append(
                    f"Closed {repair_result['holes_closed']} holes"
                )
            working_path = out
        except (ValueError, FileNotFoundError):
            pass

        # Remove floating regions
        try:
            region_result = remove_floating_regions(working_path, output_path=out)
            if region_result["removed_components"] > 0:
                actions_taken.append(
                    f"Removed {region_result['removed_components']} floating regions "
                    f"({region_result['removed_triangles']} triangles)"
                )
            working_path = out
        except (ValueError, FileNotFoundError):
            pass

        # Re-analyze after fixes
        analysis = analyze_mesh(working_path)

    # Step 3: Check all criteria
    if not analysis.is_manifold:
        issues.append({
            "type": "non_manifold",
            "detail": "Mesh is not watertight — slicers may produce artifacts",
            "fix": "Run with auto_fix=True or use repair_mesh_advanced()",
        })

    if analysis.connected_components > 1:
        issues.append({
            "type": "floating_regions",
            "detail": f"{analysis.connected_components} disconnected components",
            "fix": "Run with auto_fix=True or use remove_floating_regions()",
        })

    if analysis.max_overhang_angle_deg > 60:
        issues.append({
            "type": "severe_overhangs",
            "detail": f"Max overhang {analysis.max_overhang_angle_deg}° (limit: 60°)",
            "fix": "Use optimize_print_orientation() or enable supports in slicer",
        })

    if analysis.degenerate_triangles > 0:
        issues.append({
            "type": "degenerate_triangles",
            "detail": f"{analysis.degenerate_triangles} zero-area triangles",
            "fix": "Run with auto_fix=True or use repair_mesh()",
        })

    # Check bed fit
    if analysis.dimensions_mm:
        w = analysis.dimensions_mm["width_mm"]
        d = analysis.dimensions_mm["depth_mm"]
        h = analysis.dimensions_mm["height_mm"]
        bed_x, bed_y, bed_z = printer_bed_mm
        if w > bed_x or d > bed_y or h > bed_z:
            issues.append({
                "type": "too_large",
                "detail": (
                    f"Model ({w:.0f}×{d:.0f}×{h:.0f}mm) exceeds "
                    f"build volume ({bed_x:.0f}×{bed_y:.0f}×{bed_z:.0f}mm)"
                ),
                "fix": "Use rescale_model() to fit the build plate",
            })

    if len(issues) == 0:
        verdict = "ready_to_print"
        can_print = True
    elif all(i["type"] in ("severe_overhangs",) for i in issues):
        verdict = "printable_with_supports"
        can_print = True  # printable — just needs support enabled in slicer
    else:
        verdict = "needs_fixes"
        can_print = False

    result: dict[str, Any] = {
        "can_print": can_print,
        "verdict": verdict,
        "issues": issues,
        "issue_count": len(issues),
        "actions_taken": actions_taken,
        "printability_score": analysis.printability_score,
        "triangle_count": analysis.triangle_count,
        "dimensions_mm": analysis.dimensions_mm,
    }

    if auto_fix and working_path != file_path:
        result["fixed_file"] = working_path

    return result


# ---------------------------------------------------------------------------
# Mesh mirroring
# ---------------------------------------------------------------------------


def mirror_mesh(
    file_path: str,
    *,
    axis: str = "x",
    output_path: str | None = None,
) -> dict[str, Any]:
    """Mirror (reflect) a mesh along an axis.

    Useful for creating left/right symmetric pairs or fixing
    mirrored exports from CAD tools.

    Args:
        file_path: Path to the STL file.
        axis: Axis to mirror across ("x", "y", or "z").
        output_path: Output path.  Defaults to overwriting input.

    Returns:
        Dict with mirror statistics.
    """
    axis = axis.lower()
    if axis not in ("x", "y", "z"):
        raise ValueError(f"axis must be 'x', 'y', or 'z', got {axis!r}")

    path = Path(file_path)
    errors: list[str] = []
    triangles, _ = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    axis_idx = {"x": 0, "y": 1, "z": 2}[axis]

    mirrored: list[tuple[tuple[float, ...], ...]] = []
    for tri in triangles:
        new_tri = []
        for v in tri:
            vl = list(v)
            vl[axis_idx] = -vl[axis_idx]
            new_tri.append(tuple(vl))
        # Reverse winding order to flip normals after mirror
        mirrored.append((new_tri[0], new_tri[2], new_tri[1]))

    out = output_path or file_path
    _write_binary_stl(mirrored, out)

    return {
        "path": out,
        "axis": axis,
        "triangle_count": len(mirrored),
    }


# ---------------------------------------------------------------------------
# Hollow shell (for resin printing or material savings)
# ---------------------------------------------------------------------------


def hollow_mesh(
    file_path: str,
    *,
    wall_thickness_mm: float = 2.0,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Create a hollow version of a mesh by generating an inner offset shell.

    Approximates hollowing by scaling a copy of the mesh inward from its
    center of mass and combining both shells.  This is a rough approach
    that works well for convex/simple shapes but may self-intersect on
    complex geometry.

    Args:
        file_path: Path to the STL file.
        wall_thickness_mm: Wall thickness in mm (default 2.0).
        output_path: Output path.  Defaults to ``<name>_hollow.stl``.

    Returns:
        Dict with hollowing statistics.
    """
    path = Path(file_path)
    errors: list[str] = []
    triangles, vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    if wall_thickness_mm <= 0:
        raise ValueError("wall_thickness_mm must be positive")

    bbox = _bounding_box(vertices)
    dims = [
        bbox["x_max"] - bbox["x_min"],
        bbox["y_max"] - bbox["y_min"],
        bbox["z_max"] - bbox["z_min"],
    ]
    max_dim = max(dims)
    if max_dim < wall_thickness_mm * 2:
        raise ValueError(
            f"Model too small ({max_dim:.1f}mm) for {wall_thickness_mm}mm wall thickness"
        )

    # Compute center of mass
    cx = (bbox["x_min"] + bbox["x_max"]) / 2.0
    cy = (bbox["y_min"] + bbox["y_max"]) / 2.0
    cz = (bbox["z_min"] + bbox["z_max"]) / 2.0

    # Scale factor for inner shell: shrink by wall_thickness from each side
    # Approximate: scale = 1 - (2 * wall_thickness / max_dim)
    scale = 1.0 - (2.0 * wall_thickness_mm / max_dim)
    if scale <= 0.05:
        raise ValueError("Wall thickness too large relative to model size")

    # Create inner shell (scaled + reversed winding)
    inner: list[tuple[tuple[float, ...], ...]] = []
    for tri in triangles:
        new_tri = []
        for v in tri:
            # Scale toward center
            nx = cx + (v[0] - cx) * scale
            ny = cy + (v[1] - cy) * scale
            nz = cz + (v[2] - cz) * scale
            new_tri.append((nx, ny, nz))
        # Reverse winding for inner shell (normals face inward)
        inner.append((new_tri[0], new_tri[2], new_tri[1]))

    # Combine outer + inner shells
    combined = list(triangles) + inner

    if output_path is None:
        output_path = str(path.with_name(f"{path.stem}_hollow.stl"))

    _write_binary_stl(combined, output_path)

    original_vol = 0.0
    for tri in triangles:
        v0, v1, v2 = tri
        original_vol += abs(
            v0[0] * (v1[1] * v2[2] - v2[1] * v1[2])
            - v1[0] * (v0[1] * v2[2] - v2[1] * v0[2])
            + v2[0] * (v0[1] * v1[2] - v1[1] * v0[2])
        ) / 6.0

    inner_vol = original_vol * (scale ** 3)
    # Material saved = the hollow void (inner volume that's now empty)
    saved_vol = inner_vol

    return {
        "path": output_path,
        "wall_thickness_mm": wall_thickness_mm,
        "original_triangles": len(triangles),
        "total_triangles": len(combined),
        "scale_factor": round(scale, 4),
        "estimated_volume_saved_mm3": round(saved_vol, 1),
        "estimated_material_saved_pct": round(
            saved_vol / original_vol * 100, 1
        ) if original_vol > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Center on build plate
# ---------------------------------------------------------------------------


def center_on_bed(
    file_path: str,
    *,
    bed_x_mm: float = 256.0,
    bed_y_mm: float = 256.0,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Center a mesh on the build plate and place z_min at z=0.

    Args:
        file_path: Path to the STL file.
        bed_x_mm: Build plate X dimension.
        bed_y_mm: Build plate Y dimension.
        output_path: Output path.  Defaults to overwriting input.

    Returns:
        Dict with new position info.
    """
    path = Path(file_path)
    errors: list[str] = []
    triangles, vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    bbox = _bounding_box(vertices)

    # Current center
    cur_cx = (bbox["x_min"] + bbox["x_max"]) / 2.0
    cur_cy = (bbox["y_min"] + bbox["y_max"]) / 2.0
    cur_zmin = bbox["z_min"]

    # Target center
    target_cx = bed_x_mm / 2.0
    target_cy = bed_y_mm / 2.0

    dx = target_cx - cur_cx
    dy = target_cy - cur_cy
    dz = -cur_zmin  # place z_min at 0

    if abs(dx) < 0.001 and abs(dy) < 0.001 and abs(dz) < 0.001:
        out = output_path or file_path
        if output_path and output_path != file_path:
            _write_binary_stl(triangles, out)
        return {
            "path": out,
            "already_centered": True,
            "translation_mm": {"x": 0.0, "y": 0.0, "z": 0.0},
        }

    moved: list[tuple[tuple[float, ...], ...]] = [
        tuple((v[0] + dx, v[1] + dy, v[2] + dz) for v in tri)
        for tri in triangles
    ]

    out = output_path or file_path
    _write_binary_stl(moved, out)

    return {
        "path": out,
        "already_centered": False,
        "translation_mm": {
            "x": round(dx, 2),
            "y": round(dy, 2),
            "z": round(dz, 2),
        },
        "new_center_mm": {
            "x": round(target_cx, 2),
            "y": round(target_cy, 2),
        },
    }


# ---------------------------------------------------------------------------
# Non-manifold edge analysis
# ---------------------------------------------------------------------------


def count_non_manifold_edges(file_path: str) -> dict[str, Any]:
    """Count and classify non-manifold edges in a mesh.

    A manifold mesh has every edge shared by exactly 2 triangles.
    Non-manifold edges are shared by 1 (boundary) or 3+ (T-junction)
    triangles.

    Args:
        file_path: Path to mesh file.

    Returns:
        Dict with edge counts broken down by type.
    """
    path = Path(file_path)
    errors: list[str] = []
    tris = _load_triangles(path, errors)
    if errors or not tris:
        raise ValueError(f"Cannot parse mesh: {errors or ['No geometry']}")

    census = _edge_census(tris)
    total_edges = census["total"]
    non_manifold = census["boundary"] + census["pinch"]

    return {
        "total_edges": total_edges,
        "manifold_edges": census["manifold"],
        "boundary_edges": census["boundary"],
        "t_junction_edges": census["pinch"],
        "non_manifold_edges": non_manifold,
        "is_watertight": non_manifold == 0,
        "manifold_pct": round(census["manifold"] / total_edges * 100, 1) if total_edges > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Scale to fit build volume
# ---------------------------------------------------------------------------


def scale_to_fit(
    file_path: str,
    *,
    max_x_mm: float = 256.0,
    max_y_mm: float = 256.0,
    max_z_mm: float = 256.0,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Auto-scale a mesh to fit within a build volume.

    Applies uniform scaling so the mesh fits inside the given
    bounding box while maintaining aspect ratio.  If the mesh
    already fits, no scaling is applied.

    Args:
        file_path: Path to the STL file.
        max_x_mm: Maximum X dimension of the build volume.
        max_y_mm: Maximum Y dimension of the build volume.
        max_z_mm: Maximum Z dimension of the build volume.
        output_path: Output path.  Defaults to overwriting input.

    Returns:
        Dict with original/new dimensions and scale factor.
    """
    if max_x_mm <= 0 or max_y_mm <= 0 or max_z_mm <= 0:
        raise ValueError("Build volume dimensions must be positive.")

    path = Path(file_path)
    errors: list[str] = []
    triangles, vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    bbox = _bounding_box(vertices)
    dim_x = bbox["x_max"] - bbox["x_min"]
    dim_y = bbox["y_max"] - bbox["y_min"]
    dim_z = bbox["z_max"] - bbox["z_min"]

    original_dimensions = {
        "x": round(dim_x, 3),
        "y": round(dim_y, 3),
        "z": round(dim_z, 3),
    }

    # Compute uniform scale factor (smallest ratio wins).
    ratios: list[float] = []
    if dim_x > 0:
        ratios.append(max_x_mm / dim_x)
    if dim_y > 0:
        ratios.append(max_y_mm / dim_y)
    if dim_z > 0:
        ratios.append(max_z_mm / dim_z)

    if not ratios:
        raise ValueError("Mesh has zero extent on all axes.")

    scale = min(ratios)

    out = output_path or file_path
    if scale >= 1.0:
        # Already fits — write copy if separate output requested.
        if output_path and output_path != file_path:
            _write_binary_stl(triangles, out)
        return {
            "path": out,
            "original_dimensions": original_dimensions,
            "new_dimensions": original_dimensions,
            "scale_factor": 1.0,
            "already_fits": True,
        }

    # Scale around bounding-box center so it stays centered.
    cx = (bbox["x_min"] + bbox["x_max"]) / 2.0
    cy = (bbox["y_min"] + bbox["y_max"]) / 2.0
    cz = (bbox["z_min"] + bbox["z_max"]) / 2.0

    scaled: list[tuple[tuple[float, ...], ...]] = []
    for tri in triangles:
        new_tri = tuple(
            (
                (v[0] - cx) * scale + cx,
                (v[1] - cy) * scale + cy,
                (v[2] - cz) * scale + cz,
            )
            for v in tri
        )
        scaled.append(new_tri)

    _write_binary_stl(scaled, out)

    new_dimensions = {
        "x": round(dim_x * scale, 3),
        "y": round(dim_y * scale, 3),
        "z": round(dim_z * scale, 3),
    }

    return {
        "path": out,
        "original_dimensions": original_dimensions,
        "new_dimensions": new_dimensions,
        "scale_factor": round(scale, 6),
        "already_fits": False,
    }


# ---------------------------------------------------------------------------
# Merge multiple STL files
# ---------------------------------------------------------------------------


def merge_stl_files(
    file_paths: list[str],
    *,
    output_path: str,
) -> dict[str, Any]:
    """Combine multiple STL files into a single file.

    Reads triangles from each input file and writes a single
    combined binary STL.

    Args:
        file_paths: List of paths to STL files.
        output_path: Destination path for the merged file.

    Returns:
        Dict with merge statistics.
    """
    if not file_paths:
        raise ValueError("file_paths must not be empty.")
    if not output_path:
        raise ValueError("output_path is required.")

    all_triangles: list[tuple[tuple[float, ...], ...]] = []

    for fp in file_paths:
        path = Path(fp)
        if not path.exists():
            raise ValueError(f"File not found: {fp}")
        errors: list[str] = []
        tris, _ = _parse_stl(path, errors)
        if errors:
            raise ValueError(f"Failed to parse {fp}: {'; '.join(errors)}")
        all_triangles.extend(tris)

    if not all_triangles:
        raise ValueError("No triangles found across input files.")

    _write_binary_stl(all_triangles, output_path)

    return {
        "path": output_path,
        "file_count": len(file_paths),
        "total_triangles": len(all_triangles),
    }


# ---------------------------------------------------------------------------
# Split mesh by connected component
# ---------------------------------------------------------------------------


def split_by_component(
    file_path: str,
    *,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Split a multi-component mesh into separate STL files.

    Uses union-find on shared edges to identify connected
    components, then writes each component as a separate file.

    Args:
        file_path: Path to the STL file.
        output_dir: Directory for output files.  Defaults to
            the same directory as the input file.

    Returns:
        Dict with component count and file paths.
    """
    path = Path(file_path)
    errors: list[str] = []
    triangles, _ = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    n = len(triangles)

    # Union-find
    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    edge_to_tri: dict[tuple[tuple[float, ...], tuple[float, ...]], int] = {}
    for i, tri in enumerate(triangles):
        for j in range(3):
            va, vb = tri[j], tri[(j + 1) % 3]
            edge = (min(va, vb), max(va, vb))
            if edge in edge_to_tri:
                _union(i, edge_to_tri[edge])
            else:
                edge_to_tri[edge] = i

    # Group triangles by component root
    components: dict[int, list[int]] = {}
    for i in range(n):
        root = _find(i)
        if root not in components:
            components[root] = []
        components[root].append(i)

    # Sort components by size (largest first) for deterministic ordering
    sorted_comps = sorted(components.values(), key=len, reverse=True)

    out_dir = Path(output_dir) if output_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem

    written_paths: list[str] = []
    for idx, comp_indices in enumerate(sorted_comps):
        comp_tris = [triangles[i] for i in comp_indices]
        out_file = str(out_dir / f"{stem}_component_{idx}.stl")
        _write_binary_stl(comp_tris, out_file)
        written_paths.append(out_file)

    return {
        "component_count": len(sorted_comps),
        "file_paths": written_paths,
        "triangles_per_component": [len(c) for c in sorted_comps],
    }


# ---------------------------------------------------------------------------
# Rough print time estimation from mesh geometry
# ---------------------------------------------------------------------------


def estimate_print_time_from_mesh(
    file_path: str,
    *,
    layer_height_mm: float = 0.2,
    print_speed_mm_s: float = 60.0,
    material: str = "pla",
) -> dict[str, Any]:
    """Rough print time estimate from mesh geometry.

    Algorithm:
        1. Compute bounding-box height → number of layers.
        2. Approximate total surface area of the mesh.
        3. Estimate perimeter per layer ≈ sqrt(surface_area / height).
        4. Total toolpath length ≈ perimeter * layers.
        5. Time ≈ toolpath / speed + per-layer overhead.

    This is a *rough* estimate — actual time depends on infill,
    supports, acceleration, retraction, and slicer settings.

    Args:
        file_path: Path to mesh file.
        layer_height_mm: Slicing layer height.
        print_speed_mm_s: Average print move speed.
        material: Material hint (used for per-layer overhead).

    Returns:
        Dict with estimated time and layer info.
    """
    if layer_height_mm <= 0:
        raise ValueError("layer_height_mm must be positive.")
    if print_speed_mm_s <= 0:
        raise ValueError("print_speed_mm_s must be positive.")

    path = Path(file_path)
    errors: list[str] = []
    tris = _load_triangles(path, errors)
    if errors or not tris:
        raise ValueError(f"Cannot parse mesh: {errors or ['No geometry']}")

    # Collect all vertices for bounding box
    all_verts: list[tuple[float, ...]] = []
    for tri in tris:
        all_verts.extend(tri)
    bbox = _bounding_box(all_verts)

    height = bbox["z_max"] - bbox["z_min"]
    if height <= 0:
        raise ValueError("Mesh has zero height (flat on Z axis).")

    layers = max(1, int(math.ceil(height / layer_height_mm)))

    # Approximate surface area using triangle areas
    total_surface_area = 0.0
    for tri in tris:
        v0, v1, v2 = tri
        # Cross product of two edge vectors
        ax, ay, az = v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]
        bx, by, bz = v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]
        cx = ay * bz - az * by
        cy = az * bx - ax * bz
        cz = ax * by - ay * bx
        total_surface_area += 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)

    # Perimeter per layer ≈ sqrt(surface_area / height)
    # This approximates the average cross-section perimeter.
    perimeter_per_layer = math.sqrt(total_surface_area / height) if height > 0 else 0.0

    # Total toolpath ≈ perimeter * layers (accounts for walls)
    # Add ~30% for infill estimate (rough)
    infill_factor = 1.3
    total_path_length = perimeter_per_layer * layers * infill_factor

    # Per-layer overhead (homing, z-move, retraction).
    # Slightly higher for materials needing heated bed stabilisation.
    material_lower = material.lower()
    if material_lower in ("abs", "asa", "nylon", "pc"):
        overhead_per_layer_s = 3.0
    else:
        overhead_per_layer_s = 2.0

    travel_time_s = total_path_length / print_speed_mm_s if print_speed_mm_s > 0 else 0.0
    overhead_time_s = layers * overhead_per_layer_s
    total_seconds = travel_time_s + overhead_time_s

    # Human-readable format
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    if hours > 0:
        human = f"{hours}h {minutes}m"
    else:
        human = f"{minutes}m"

    return {
        "estimated_time_seconds": round(total_seconds, 1),
        "estimated_time_human": human,
        "layers": layers,
        "perimeter_per_layer_mm": round(perimeter_per_layer, 2),
        "total_path_length_mm": round(total_path_length, 1),
        "surface_area_mm2": round(total_surface_area, 1),
        "height_mm": round(height, 2),
        "material": material_lower,
        "note": "Rough estimate. Actual time depends on slicer settings, infill, supports, and acceleration.",
    }


# ---------------------------------------------------------------------------
# 3MF model extraction (3MF → STL)
# ---------------------------------------------------------------------------


def extract_model_from_3mf(
    file_path: str,
    *,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Extract embedded 3D model geometry from a .3mf file to STL.

    3MF files (including .gcode.3mf from Bambu Studio) are ZIP archives
    containing an XML model file at ``3D/3dmodel.model``.  This function
    parses the XML, extracts all mesh objects (vertices + triangles), and
    writes a binary STL.

    Handles both standard 3MF and Bambu-style .gcode.3mf files.  When
    multiple objects exist they are merged into a single STL.

    .. note::
        3MF item/component transforms are not applied — geometry is
        extracted as stored.  This is correct for single-model files
        and Bambu .gcode.3mf files where geometry is already in world
        coordinates.

    Args:
        file_path: Path to the .3mf or .gcode.3mf file.
        output_path: Output STL path.  Defaults to ``<stem>.stl`` next
            to the input file.

    Returns:
        Dict with output path, triangle/vertex counts, and dimensions.

    Raises:
        ValueError: If the file is not a valid 3MF or contains no geometry.
        FileNotFoundError: If the input file does not exist.
    """
    import xml.etree.ElementTree as ET

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if not zipfile.is_zipfile(file_path):
        raise ValueError(f"Not a valid ZIP/3MF file: {file_path}")

    # Locate the model XML inside the archive.
    model_xml: str | None = None
    with zipfile.ZipFile(file_path, "r") as zf:
        names = zf.namelist()

        # Prefer the standard path; fall back to any .model file.
        candidates = [
            "3D/3dmodel.model",
            "3d/3dmodel.model",  # case-insensitive fallback
        ]
        for candidate in candidates:
            if candidate in names:
                model_xml = zf.read(candidate).decode("utf-8")
                break

        if model_xml is None:
            # Broader search for any .model file in the archive.
            for name in names:
                if name.lower().endswith(".model"):
                    model_xml = zf.read(name).decode("utf-8")
                    break

    if model_xml is None:
        raise ValueError(
            f"No 3D model found in {file_path}. "
            f"Archive contains: {', '.join(names[:20])}"
        )

    # Parse the XML model.
    root = ET.fromstring(model_xml)

    # Handle XML namespace — 3MF uses a default namespace.
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    # Collect all mesh objects (a 3MF can have multiple objects).
    all_triangles: list[tuple[tuple[float, ...], ...]] = []
    total_vertices = 0

    for obj in root.iter(f"{ns}object"):
        mesh_el = obj.find(f"{ns}mesh")
        if mesh_el is None:
            continue

        verts_el = mesh_el.find(f"{ns}vertices")
        tris_el = mesh_el.find(f"{ns}triangles")
        if verts_el is None or tris_el is None:
            continue

        # Parse vertices.
        vertices: list[tuple[float, ...]] = []
        for v_el in verts_el.findall(f"{ns}vertex"):
            x = float(v_el.get("x", "0"))
            y = float(v_el.get("y", "0"))
            z = float(v_el.get("z", "0"))
            vertices.append((x, y, z))

        total_vertices += len(vertices)

        # Parse triangles (index references into vertices).
        for t_el in tris_el.findall(f"{ns}triangle"):
            v1_idx = int(t_el.get("v1", "0"))
            v2_idx = int(t_el.get("v2", "0"))
            v3_idx = int(t_el.get("v3", "0"))

            if (
                v1_idx < 0
                or v2_idx < 0
                or v3_idx < 0
                or v1_idx >= len(vertices)
                or v2_idx >= len(vertices)
                or v3_idx >= len(vertices)
            ):
                continue  # Skip invalid index references.

            all_triangles.append(
                (vertices[v1_idx], vertices[v2_idx], vertices[v3_idx])
            )

    if not all_triangles:
        raise ValueError(
            f"3MF file contains no mesh geometry: {file_path}"
        )

    # Determine output path.
    if output_path is None:
        # Strip compound extensions like .gcode.3mf → .stl
        stem = path.stem
        if stem.lower().endswith(".gcode"):
            stem = stem[: -len(".gcode")]
        output_path = str(path.parent / f"{stem}.stl")

    _write_binary_stl(all_triangles, output_path)

    # Compute bounding box for dimension reporting.
    xs = [v[0] for tri in all_triangles for v in tri]
    ys = [v[1] for tri in all_triangles for v in tri]
    zs = [v[2] for tri in all_triangles for v in tri]

    dims = {
        "x_mm": round(max(xs) - min(xs), 2),
        "y_mm": round(max(ys) - min(ys), 2),
        "z_mm": round(max(zs) - min(zs), 2),
    }

    return {
        "output_path": output_path,
        "triangle_count": len(all_triangles),
        "vertex_count": total_vertices,
        "dimensions": dims,
        "source_file": file_path,
    }


# ---------------------------------------------------------------------------
# Bambu .gcode.3mf plate object inspection and extraction
# ---------------------------------------------------------------------------


def list_plate_objects(
    file_path: str,
    plate_number: int = 1,
) -> dict[str, Any]:
    """List named objects on the build plate of a Bambu .gcode.3mf file.

    Parses ``Metadata/plate_N.json`` from the archive to enumerate every
    object that was plated when the file was sliced.  Works even when the
    3MF contains no mesh geometry (common for .gcode.3mf exports from
    Bambu Studio / OrcaSlicer).

    Bambu Studio supports multiple plates (plate_1, plate_2, etc.).  Use
    the *plate_number* parameter to select which plate to inspect.  The
    returned dict includes a ``plates_available`` field listing all plate
    numbers found in the archive.

    Each object entry includes:

    * **name** – original STL filename (e.g. ``"TreatHolder - cap.stl"``)
    * **plate_index** – zero-based position in the plate object list
    * **label_id** – the gcode label ID used in ``start/stop printing
      object`` comments (mapped from the gcode header)
    * **bbox** – ``[x_min, y_min, x_max, y_max]`` bounding box on the
      build plate
    * **area_mm2** – footprint area on the plate
    * **layer_height_mm** – per-object layer height

    Also returns plate-level metadata: bed type, filament colours,
    nozzle diameter, and whether sequential printing was enabled.

    Use this to discover available objects before calling
    ``extract_plate_object`` (MCP tool) or
    ``extract_plate_object_gcode`` (Python API) to isolate one.

    .. note::
       Only the first 8 KB of the embedded G-code is read (for header
       parsing), so this function is fast and lightweight regardless of
       the overall file size.

    Args:
        file_path: Path to a ``.3mf`` or ``.gcode.3mf`` file.
        plate_number: Which plate to inspect (1-based).  Defaults to 1.

    Returns:
        Dict with ``objects`` list, plate metadata, and
        ``plates_available`` (sorted list of plate numbers found).

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is not a valid ZIP/3MF or contains no
            plate metadata for the requested plate number.
    """
    import re as _re

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not zipfile.is_zipfile(file_path):
        raise ValueError(f"Not a valid ZIP/3MF file: {file_path}")

    plate_json: dict[str, Any] | None = None
    gcode_header: str = ""

    with zipfile.ZipFile(file_path, "r") as zf:
        names = zf.namelist()

        # Scan for all available plates (plate_N.json in Metadata/)
        plates_available: list[int] = sorted({
            int(m.group(1))
            for name in names
            if (m := _re.search(r"(?i)metadata/plate_(\d+)\.json$", name))
        })

        # Find plate JSON for the requested plate number
        plate_json_name = f"Metadata/plate_{plate_number}.json"
        plate_json_name_lower = f"metadata/plate_{plate_number}.json"
        for candidate in [plate_json_name, plate_json_name_lower]:
            if candidate in names:
                plate_json = _json.loads(zf.read(candidate).decode("utf-8"))
                break

        if plate_json is None:
            if plates_available:
                raise ValueError(
                    f"No plate metadata found for plate {plate_number} in "
                    f"{file_path}. Available plates: {plates_available}"
                )
            raise ValueError(
                f"No plate metadata found in {file_path}. "
                f"This may not be a Bambu Studio / OrcaSlicer .gcode.3mf file."
            )

        # Read gcode header to extract label ID mapping
        gcode_candidates = [
            f"Metadata/plate_{plate_number}.gcode",
            f"metadata/plate_{plate_number}.gcode",
        ]
        for candidate in gcode_candidates:
            if candidate in names:
                # Read only the first 8KB for the header — label IDs are
                # in the first few lines.
                with zf.open(candidate) as gf:
                    gcode_header = gf.read(8192).decode("utf-8", errors="replace")
                break

    # Parse label IDs from gcode header:
    # "; model label id: 724,757"
    label_ids: list[int] = []
    for line in gcode_header.splitlines():
        stripped = line.strip()
        if stripped.startswith("; model label id:"):
            id_str = stripped.split(":", 1)[1].strip()
            label_ids = [int(x.strip()) for x in id_str.split(",") if x.strip()]
            break

    bbox_objects = plate_json.get("bbox_objects", [])

    objects: list[dict[str, Any]] = []
    for idx, obj in enumerate(bbox_objects):
        entry: dict[str, Any] = {
            "name": obj.get("name", f"object_{idx}"),
            "plate_index": idx,
            "label_id": label_ids[idx] if idx < len(label_ids) else None,
            "bbox": obj.get("bbox"),
            "area_mm2": round(obj.get("area", 0), 2),
            "layer_height_mm": round(obj.get("layer_height", 0.2), 3),
        }
        objects.append(entry)

    return {
        "object_count": len(objects),
        "objects": objects,
        "bed_type": plate_json.get("bed_type"),
        "filament_colors": plate_json.get("filament_colors", []),
        "nozzle_diameter_mm": round(plate_json.get("nozzle_diameter", 0.4), 2),
        "is_sequential_print": plate_json.get("is_seq_print", False),
        "plates_available": plates_available,
        "plate_number": plate_number,
        "source_file": file_path,
    }


def extract_plate_object_gcode(
    file_path: str,
    object_name: str,
    *,
    output_path: str | None = None,
    plate_number: int = 1,
) -> dict[str, Any]:
    """Extract a single object's G-code from a Bambu .gcode.3mf file.

    Bambu Studio / OrcaSlicer embed per-object markers in the G-code::

        ; start printing object, unique label id: 757
        ... (moves for this object) ...
        ; stop printing object, unique label id: 757

    This function filters the G-code to keep only the sections belonging
    to the requested object, preserving the machine start-up sequence
    (homing, bed levelling, heating, calibration) and end sequence
    (cool-down, retract, park).

    The resulting file is a standalone ``.gcode`` file that can be sent
    directly to the printer.

    Bambu Studio supports multiple plates.  Use the *plate_number*
    parameter to select which plate's G-code to read from.

    .. warning::
       The entire G-code file is read into memory for filtering.  For
       typical Bambu prints (10-100 MB G-code) this uses roughly
       200-400 MB of RAM.  For very large prints (500 MB+ G-code),
       memory usage may be significant.

       Only ``M83`` (relative extrusion) is supported.  Files that use
       ``M82`` (absolute extrusion) are rejected because per-object
       extraction would corrupt the extrusion distances.

    **Matching logic:** *object_name* is matched case-insensitively
    against the ``name`` field in the plate metadata.  Partial / substring
    matches are accepted so that ``"cap"`` matches
    ``"TreatHolder - cap.stl"``.

    Args:
        file_path: Path to the ``.gcode.3mf`` file.
        object_name: Name (or substring) of the object to extract.
        output_path: Output ``.gcode`` path.  Auto-generated if omitted.
        plate_number: Which plate to read from (1-based).  Defaults to 1.

    Returns:
        Dict with output path, matched object info, and line counts.

    Raises:
        FileNotFoundError: If the input file does not exist.
        ValueError: If no matching object is found, or the file is not a
            valid Bambu .gcode.3mf.
    """
    # First, list objects to find the target label ID
    plate_info = list_plate_objects(file_path, plate_number=plate_number)
    objects = plate_info["objects"]

    if not objects:
        raise ValueError(f"No objects found on the plate in {file_path}")

    # Match object by name (case-insensitive).
    # Priority: exact match > exact match without extension > substring.
    query = object_name.lower()
    query_no_ext = query.rsplit(".", 1)[0] if "." in query else query

    matched: dict[str, Any] | None = None

    # Pass 1: exact match (full name with extension)
    for obj in objects:
        if obj["name"].lower() == query:
            matched = obj
            break

    # Pass 2: exact match without extension
    if matched is None:
        for obj in objects:
            obj_no_ext = obj["name"].lower().rsplit(".", 1)[0]
            if obj_no_ext == query_no_ext:
                matched = obj
                break

    # Pass 3: substring match — collect ALL matches
    if matched is None:
        substring_matches: list[dict[str, Any]] = []
        for obj in objects:
            obj_name_lower = obj["name"].lower()
            obj_no_ext = obj_name_lower.rsplit(".", 1)[0] if "." in obj_name_lower else obj_name_lower
            if query_no_ext in obj_no_ext or query in obj_name_lower:
                substring_matches.append(obj)

        if len(substring_matches) == 1:
            matched = substring_matches[0]
        elif len(substring_matches) > 1:
            match_names = [m["name"] for m in substring_matches]
            raise ValueError(
                f"Ambiguous match: {object_name!r} matches multiple objects: "
                f"{match_names}. Please use a more specific name."
            )

    if matched is None:
        available = [o["name"] for o in objects]
        raise ValueError(
            f"No object matching {object_name!r} found on the plate. "
            f"Available objects: {available}"
        )

    target_label_id = matched["label_id"]
    if target_label_id is None:
        raise ValueError(
            f"Object {matched['name']!r} has no gcode label ID mapping. "
            f"Cannot extract gcode."
        )

    # Read the full gcode from the archive
    gcode_text: str | None = None
    with zipfile.ZipFile(file_path, "r") as zf:
        for candidate in [
            f"Metadata/plate_{plate_number}.gcode",
            f"metadata/plate_{plate_number}.gcode",
        ]:
            if candidate in zf.namelist():
                gcode_text = zf.read(candidate).decode("utf-8", errors="replace")
                break

    if gcode_text is None:
        raise ValueError(f"No gcode found in {file_path}")

    # Safety check: absolute extrusion (M82) would produce wrong E values
    # after filtering.  Bambu Studio always uses M83 (relative), but guard
    # against hand-edited or non-Bambu files.
    has_m82 = "\nM82" in gcode_text or gcode_text.startswith("M82")
    has_m83 = "\nM83" in gcode_text or gcode_text.startswith("M83")
    if has_m82 and not has_m83:
        raise ValueError(
            "File uses absolute extrusion (M82). Object extraction only "
            "supports relative extrusion (M83) as used by Bambu Studio. "
            "Re-slice with relative extrusion enabled."
        )

    lines = gcode_text.splitlines(keepends=True)
    total_lines = len(lines)

    # --- Phase 1: Identify the machine start and end gcode boundaries ---
    start_end_idx = 0  # end of machine start gcode (inclusive)
    end_start_idx = total_lines  # start of machine end gcode

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "; MACHINE_START_GCODE_END":
            start_end_idx = i + 1
        elif stripped == "; MACHINE_END_GCODE_START":
            end_start_idx = i
            break

    if start_end_idx == 0:
        logger.warning(
            "No MACHINE_START_GCODE_END marker found in %s — "
            "the file may not be a Bambu Studio export. "
            "Start gcode may not be correctly preserved.",
            file_path,
        )

    if end_start_idx == total_lines:
        logger.warning(
            "No MACHINE_END_GCODE_START marker found in %s — "
            "end gcode (cooldown, park) may be missing from output.",
            file_path,
        )

    # --- Phase 2: Filter the layer gcode (between start and end) ---
    # Strategy: Walk through the layer section line by line.
    # - Keep everything that's NOT inside another object's print section.
    # - Object sections are bounded by:
    #   "; start printing object, unique label id: XXX"
    #   "; stop printing object, unique label id: XXX"
    # - Layer changes, Z moves, and infrastructure between objects
    #   are kept (they're outside object markers).

    target_marker = f"unique label id: {target_label_id}"
    start_marker_prefix = "; start printing object, unique label id:"
    stop_marker_prefix = "; stop printing object, unique label id:"

    filtered_layer_lines: list[str] = []
    inside_other_object = False
    kept_lines = 0
    skipped_lines = 0

    for i in range(start_end_idx, end_start_idx):
        line = lines[i]
        stripped = line.strip()

        # Check for object boundary markers
        if stripped.startswith(start_marker_prefix):
            if target_marker in stripped:
                # Entering our target object — include its lines
                inside_other_object = False
                filtered_layer_lines.append(line)
                kept_lines += 1
            else:
                # Entering a different object — skip its lines
                inside_other_object = True
                skipped_lines += 1
            continue

        if stripped.startswith(stop_marker_prefix):
            if target_marker in stripped:
                # Leaving our target object
                inside_other_object = False
                filtered_layer_lines.append(line)
                kept_lines += 1
            else:
                # Leaving the other object
                inside_other_object = False
                skipped_lines += 1
            continue

        if inside_other_object:
            skipped_lines += 1
        else:
            filtered_layer_lines.append(line)
            kept_lines += 1

    # --- Phase 2.5: Recalculate M73 progress & time estimates -----------
    # The filtered gcode carries the FULL plate's M73 P (percent) and
    # R (remaining minutes) commands, which are wrong for a single
    # extracted object.  We need to:
    #   1. Find the time range this object's layers span
    #   2. Rescale P to 0-100% and R to count down from object time
    #   3. Update the header comment with correct time/filament estimates
    #
    # This benefits ALL printer brands — Bambu uses M73 for its display,
    # OctoPrint/Moonraker parse M73 for progress reporting, and PrusaLink
    # uses it for time-remaining display.

    _m73_pr_re = re.compile(r"^M73\s+P(\d+)\s+R(\d+)")

    # Collect all M73 P/R pairs from the ORIGINAL full gcode to build
    # a mapping of percent → remaining_minutes for the full plate.
    full_m73_pairs: list[tuple[int, int]] = []
    for line in lines:
        m = _m73_pr_re.match(line.strip())
        if m:
            full_m73_pairs.append((int(m.group(1)), int(m.group(2))))

    # Collect M73 P/R from the filtered (kept) lines to find the
    # percent range our object occupies within the full plate.
    kept_m73_indices: list[int] = []
    for idx, line in enumerate(filtered_layer_lines):
        m = _m73_pr_re.match(line.strip())
        if m:
            kept_m73_indices.append(idx)

    # Estimate this object's print time from the M73 R values.
    # The full plate's M73 goes R=99→R=0.  Our object's kept M73
    # entries span some sub-range (e.g. R=99→R=58 for cap layers).
    # The object time ≈ first_R_kept - last_R_kept.
    object_time_min = 0
    if kept_m73_indices:
        first_kept = _m73_pr_re.match(
            filtered_layer_lines[kept_m73_indices[0]].strip()
        )
        last_kept = _m73_pr_re.match(
            filtered_layer_lines[kept_m73_indices[-1]].strip()
        )
        if first_kept and last_kept:
            first_r = int(first_kept.group(2))
            last_r = int(last_kept.group(2))
            object_time_min = max(1, first_r - last_r)

    # Rewrite M73 P/R commands in the filtered layer lines.
    if kept_m73_indices and object_time_min > 0:
        n_m73 = len(kept_m73_indices)
        for rank, idx in enumerate(kept_m73_indices):
            new_pct = min(100, round(rank / max(1, n_m73 - 1) * 100))
            new_remaining = max(0, round(
                object_time_min * (1.0 - rank / max(1, n_m73 - 1))
            ))
            filtered_layer_lines[idx] = f"M73 P{new_pct} R{new_remaining}\n"

    # Also rewrite any M73 P/R in the start gcode section (before
    # MACHINE_START_GCODE_END) — these set the initial time estimate
    # on the printer display during calibration/homing.
    start_lines_copy = list(lines[:start_end_idx])
    if object_time_min > 0:
        for i, line in enumerate(start_lines_copy):
            m = _m73_pr_re.match(line.strip())
            if m:
                old_p = int(m.group(1))
                # Scale R proportionally to the object's time
                new_r = max(0, round(
                    object_time_min * (1.0 - old_p / 100.0)
                ))
                start_lines_copy[i] = f"M73 P{old_p} R{new_r}\n"

    # --- Phase 2.6: Update header comments --------------------------------
    # Fix the time/filament header lines so monitoring tools, printer
    # displays, and Kiln's own cost estimator show correct values.
    _time_comment_re = re.compile(
        r"^;\s*model printing time:.*?total estimated time:\s*(.+)",
        re.IGNORECASE,
    )
    _total_time_re = re.compile(
        r"^;\s*total estimated time:\s*(.+)", re.IGNORECASE,
    )

    object_time_sec = object_time_min * 60
    hours = object_time_sec // 3600
    mins = (object_time_sec % 3600) // 60
    secs = object_time_sec % 60
    if hours > 0:
        time_str = f"{hours}h {mins}m {secs}s"
    else:
        time_str = f"{mins}m {secs}s"

    for i, line in enumerate(start_lines_copy):
        stripped = line.strip()
        # Update combined time line (Bambu format)
        if _time_comment_re.match(stripped):
            start_lines_copy[i] = (
                f"; model printing time: {time_str}; "
                f"total estimated time: {time_str}\n"
            )
        # Update layer count if it reflects the full plate
        elif stripped.startswith("; total layer number:"):
            # Layer count is actually correct — extraction preserves
            # all layers (just removes other objects' toolpaths within
            # each layer).  No change needed.
            pass

    # --- Phase 3: Assemble the final gcode ---
    result_lines: list[str] = []

    # Machine start gcode (with corrected M73 + header)
    result_lines.extend(start_lines_copy)

    # Filtered layer gcode (only our target object, with rescaled M73)
    result_lines.extend(filtered_layer_lines)

    # Machine end gcode (cool-down, retract, park)
    result_lines.extend(lines[end_start_idx:])

    # --- Phase 4: Write output ---
    if output_path is None:
        stem = Path(file_path).stem
        if stem.lower().endswith(".gcode"):
            stem = stem[: -len(".gcode")]
        # Sanitise the object name for use in a filename
        safe_name = matched["name"].rsplit(".", 1)[0]  # strip .stl
        safe_name = "".join(
            c if c.isalnum() or c in " _-" else "_" for c in safe_name
        ).strip()
        if not safe_name:
            safe_name = "extracted_object"
        output_path = str(Path(file_path).parent / f"{safe_name}.gcode")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.writelines(result_lines)

    return {
        "output_path": output_path,
        "matched_object": matched,
        "total_source_lines": total_lines,
        "kept_lines": kept_lines + start_end_idx + (total_lines - end_start_idx),
        "skipped_lines": skipped_lines,
        "all_objects": [o["name"] for o in objects],
        "source_file": file_path,
        "estimated_time_minutes": object_time_min,
        "estimated_time_human": time_str,
    }


# ---------------------------------------------------------------------------
# Geometry-level mesh repair: thicken, fillet, chamfer
# ---------------------------------------------------------------------------


def _compute_vertex_normals(
    triangles: list[tuple[tuple[float, ...], ...]],
) -> dict[tuple[float, ...], tuple[float, float, float]]:
    """Compute area-weighted vertex normals.

    For each vertex, accumulates the cross-product normals of all
    incident triangles (weighted by triangle area).  Returns a mapping
    from vertex coordinate tuple to a unit normal ``(nx, ny, nz)``.
    """
    import math

    accum: dict[tuple[float, ...], list[float]] = {}

    for tri in triangles:
        v0, v1, v2 = tri
        # Edge vectors
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        # Cross product (normal * 2*area)
        nx = e1[1] * e2[2] - e1[2] * e2[1]
        ny = e1[2] * e2[0] - e1[0] * e2[2]
        nz = e1[0] * e2[1] - e1[1] * e2[0]

        for v in tri:
            if v not in accum:
                accum[v] = [0.0, 0.0, 0.0]
            accum[v][0] += nx
            accum[v][1] += ny
            accum[v][2] += nz

    normals: dict[tuple[float, ...], tuple[float, float, float]] = {}
    for v, n in accum.items():
        mag = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
        if mag > 1e-12:
            normals[v] = (n[0] / mag, n[1] / mag, n[2] / mag)
        else:
            normals[v] = (0.0, 0.0, 1.0)

    return normals


def thicken_walls(
    file_path: str,
    *,
    amount_mm: float = 0.5,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Thicken thin walls by offsetting vertices outward along normals.

    Detects thin-wall regions by measuring the local thickness between
    opposing faces at each vertex.  Vertices in regions thinner than
    ``2 * amount_mm`` are pushed outward along their averaged normals
    by ``amount_mm``.  Vertices in already-thick regions are untouched.

    This is a geometry-level fix — the mesh is surgically modified
    instead of regenerating from scratch.  Works on the vertex soup
    representation without requiring a half-edge data structure.

    Args:
        file_path: Path to the STL file.
        amount_mm: Offset distance in mm (default 0.5).
        output_path: Output path.  Defaults to ``<name>_thickened.stl``.

    Returns:
        Dict with thickening statistics.

    Raises:
        ValueError: If the STL is invalid or amount is non-positive.
    """
    import math

    if amount_mm <= 0:
        raise ValueError("amount_mm must be positive")

    path = Path(file_path)
    errors: list[str] = []
    triangles, vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    # Compute vertex normals
    vnormals = _compute_vertex_normals(triangles)

    # Thickness threshold: walls thinner than this get thickened
    thickness_threshold = amount_mm * 4.0

    thin_vertices: set[tuple[float, ...]] = set()

    # For performance, skip ray-casting on large meshes — offset all
    # boundary vertices instead (cheaper, slightly less precise).
    if len(triangles) > 50000:
        # Large mesh: offset all vertices uniformly.  Still useful
        # because agents typically pre-filter with predict_print_failures.
        thin_vertices = set(vnormals.keys())
    else:
        # Build a simple spatial lookup for thickness estimation.
        # Sample approach: for each vertex, check if any other vertex
        # within thickness_threshold distance shares a roughly opposing
        # normal (dot < -0.5), indicating a thin wall.
        vtx_list = list(vnormals.keys())
        vtx_normals_list = [vnormals[v] for v in vtx_list]

        for i, v in enumerate(vtx_list):
            n = vtx_normals_list[i]
            for j, v2 in enumerate(vtx_list):
                if i == j:
                    continue
                dx = v2[0] - v[0]
                dy = v2[1] - v[1]
                dz = v2[2] - v[2]
                dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                if dist > thickness_threshold or dist < 1e-6:
                    continue
                # Check if normals are roughly opposing
                n2 = vtx_normals_list[j]
                dot = n[0] * n2[0] + n[1] * n2[1] + n[2] * n2[2]
                if dot < -0.3:
                    thin_vertices.add(v)
                    thin_vertices.add(v2)
                    break

    if not thin_vertices:
        # No thin walls detected — copy file as-is
        if output_path is None:
            output_path = str(path.with_name(f"{path.stem}_thickened.stl"))
        _write_binary_stl(triangles, output_path)
        return {
            "path": output_path,
            "vertices_modified": 0,
            "total_vertices": len(vertices),
            "amount_mm": amount_mm,
            "triangle_count": len(triangles),
        }

    # Offset thin vertices outward
    vertex_map: dict[tuple[float, ...], tuple[float, ...]] = {}
    for v in thin_vertices:
        n = vnormals[v]
        vertex_map[v] = (
            v[0] + n[0] * amount_mm,
            v[1] + n[1] * amount_mm,
            v[2] + n[2] * amount_mm,
        )

    # Rebuild triangles with offset vertices
    thickened: list[tuple[tuple[float, ...], ...]] = []
    for tri in triangles:
        new_tri = tuple(vertex_map.get(v, v) for v in tri)
        thickened.append(new_tri)

    if output_path is None:
        output_path = str(path.with_name(f"{path.stem}_thickened.stl"))

    _write_binary_stl(thickened, output_path)

    return {
        "path": output_path,
        "vertices_modified": len(thin_vertices),
        "total_vertices": len(vertices),
        "amount_mm": amount_mm,
        "triangle_count": len(thickened),
    }


def add_fillet(
    file_path: str,
    *,
    radius_mm: float = 1.0,
    angle_threshold_deg: float = 60.0,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Add fillets (rounded transitions) at sharp edges.

    Detects edges where adjacent faces meet at an angle sharper than
    ``angle_threshold_deg`` and inserts intermediate triangles to
    approximate a smooth fillet of the given radius.  The original
    sharp edge is replaced by a chamfered bevel subdivided into
    fillet segments.

    This strengthens parts by reducing stress concentrations at
    corners and improves print quality by eliminating sharp overhangs.

    Args:
        file_path: Path to the STL file.
        radius_mm: Fillet radius in mm (default 1.0).
        angle_threshold_deg: Edges sharper than this get filleted
            (default 60 degrees — catches most stress risers).
        output_path: Output path.  Defaults to ``<name>_filleted.stl``.

    Returns:
        Dict with fillet statistics.

    Raises:
        ValueError: If the STL is invalid or parameters are invalid.
    """
    import math

    if radius_mm <= 0:
        raise ValueError("radius_mm must be positive")
    if angle_threshold_deg <= 0 or angle_threshold_deg >= 180:
        raise ValueError("angle_threshold_deg must be between 0 and 180")

    path = Path(file_path)
    errors: list[str] = []
    triangles, vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    cos_threshold = math.cos(math.radians(angle_threshold_deg))

    # Build edge → face normals map
    # An edge is a pair of vertex tuples; each edge maps to the normals
    # of the two faces sharing it.
    edge_faces: dict[
        tuple[tuple[float, ...], tuple[float, ...]],
        list[tuple[float, float, float]],
    ] = {}

    face_normals: list[tuple[float, float, float]] = []
    for tri in triangles:
        v0, v1, v2 = tri
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        nx = e1[1] * e2[2] - e1[2] * e2[1]
        ny = e1[2] * e2[0] - e1[0] * e2[2]
        nz = e1[0] * e2[1] - e1[1] * e2[0]
        mag = math.sqrt(nx * nx + ny * ny + nz * nz)
        if mag > 1e-12:
            fn = (nx / mag, ny / mag, nz / mag)
        else:
            fn = (0.0, 0.0, 1.0)
        face_normals.append(fn)

        for i in range(3):
            va = tri[i]
            vb = tri[(i + 1) % 3]
            edge = (min(va, vb), max(va, vb))
            if edge not in edge_faces:
                edge_faces[edge] = []
            edge_faces[edge].append(fn)

    # Find sharp edges
    sharp_edges: list[tuple[tuple[float, ...], tuple[float, ...]]] = []
    for edge, normals in edge_faces.items():
        if len(normals) != 2:
            continue
        n1, n2 = normals[0], normals[1]
        dot = n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2]
        if dot < cos_threshold:
            sharp_edges.append(edge)

    if not sharp_edges:
        if output_path is None:
            output_path = str(path.with_name(f"{path.stem}_filleted.stl"))
        _write_binary_stl(triangles, output_path)
        return {
            "path": output_path,
            "sharp_edges_found": 0,
            "fillet_triangles_added": 0,
            "triangle_count": len(triangles),
            "radius_mm": radius_mm,
        }

    # Generate fillet geometry at each sharp edge.
    # Strategy: for each sharp edge, compute the bisector direction
    # and add a strip of triangles that bridges the gap with a
    # curved profile.
    fillet_tris: list[tuple[tuple[float, ...], ...]] = []
    segments = max(2, min(6, int(radius_mm * 3)))

    for edge in sharp_edges:
        va, vb = edge
        normals = edge_faces[edge]
        if len(normals) != 2:
            continue
        n1, n2 = normals[0], normals[1]

        # Bisector normal (average of the two face normals)
        bx = (n1[0] + n2[0]) / 2.0
        by = (n1[1] + n2[1]) / 2.0
        bz = (n1[2] + n2[2]) / 2.0
        bmag = math.sqrt(bx * bx + by * by + bz * bz)
        if bmag < 1e-12:
            continue
        bx /= bmag
        by /= bmag
        bz /= bmag

        # Generate offset points along the edge for the fillet strip
        for seg in range(segments):
            t0 = seg / segments
            t1 = (seg + 1) / segments
            # Interpolate between n1 and bisector direction
            offset0 = radius_mm * t0
            offset1 = radius_mm * t1

            # Points on fillet surface at va
            pa0 = (
                va[0] + bx * offset0,
                va[1] + by * offset0,
                va[2] + bz * offset0,
            )
            pa1 = (
                va[0] + bx * offset1,
                va[1] + by * offset1,
                va[2] + bz * offset1,
            )
            # Points on fillet surface at vb
            pb0 = (
                vb[0] + bx * offset0,
                vb[1] + by * offset0,
                vb[2] + bz * offset0,
            )
            pb1 = (
                vb[0] + bx * offset1,
                vb[1] + by * offset1,
                vb[2] + bz * offset1,
            )

            # Two triangles per segment (quad strip)
            fillet_tris.append((pa0, pb0, pa1))
            fillet_tris.append((pa1, pb0, pb1))

    combined = list(triangles) + fillet_tris

    if output_path is None:
        output_path = str(path.with_name(f"{path.stem}_filleted.stl"))

    _write_binary_stl(combined, output_path)

    return {
        "path": output_path,
        "sharp_edges_found": len(sharp_edges),
        "fillet_triangles_added": len(fillet_tris),
        "triangle_count": len(combined),
        "radius_mm": radius_mm,
        "angle_threshold_deg": angle_threshold_deg,
    }


def add_chamfer(
    file_path: str,
    *,
    distance_mm: float = 0.5,
    angle_threshold_deg: float = 60.0,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Add chamfers (flat bevels) at sharp edges.

    Detects edges where adjacent faces meet at an angle sharper than
    ``angle_threshold_deg`` and bevels them by inserting a flat
    transition face.  Chamfers are faster to print than fillets and
    reduce stress concentration at sharp corners.

    Args:
        file_path: Path to the STL file.
        distance_mm: Chamfer distance from edge in mm (default 0.5).
        angle_threshold_deg: Edges sharper than this get chamfered
            (default 60 degrees).
        output_path: Output path.  Defaults to ``<name>_chamfered.stl``.

    Returns:
        Dict with chamfer statistics.

    Raises:
        ValueError: If the STL is invalid or parameters are invalid.
    """
    import math

    if distance_mm <= 0:
        raise ValueError("distance_mm must be positive")
    if angle_threshold_deg <= 0 or angle_threshold_deg >= 180:
        raise ValueError("angle_threshold_deg must be between 0 and 180")

    path = Path(file_path)
    errors: list[str] = []
    triangles, vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL contains no geometry.")

    cos_threshold = math.cos(math.radians(angle_threshold_deg))

    # Build edge → face normals map (same as add_fillet)
    edge_faces: dict[
        tuple[tuple[float, ...], tuple[float, ...]],
        list[tuple[float, float, float]],
    ] = {}

    for tri in triangles:
        v0, v1, v2 = tri
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        nx = e1[1] * e2[2] - e1[2] * e2[1]
        ny = e1[2] * e2[0] - e1[0] * e2[2]
        nz = e1[0] * e2[1] - e1[1] * e2[0]
        mag = math.sqrt(nx * nx + ny * ny + nz * nz)
        fn = (nx / mag, ny / mag, nz / mag) if mag > 1e-12 else (0.0, 0.0, 1.0)

        for i in range(3):
            va = tri[i]
            vb = tri[(i + 1) % 3]
            edge = (min(va, vb), max(va, vb))
            if edge not in edge_faces:
                edge_faces[edge] = []
            edge_faces[edge].append(fn)

    # Find sharp edges
    sharp_edges: list[tuple[tuple[float, ...], tuple[float, ...]]] = []
    for edge, normals in edge_faces.items():
        if len(normals) != 2:
            continue
        n1, n2 = normals[0], normals[1]
        dot = n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2]
        if dot < cos_threshold:
            sharp_edges.append(edge)

    if not sharp_edges:
        if output_path is None:
            output_path = str(path.with_name(f"{path.stem}_chamfered.stl"))
        _write_binary_stl(triangles, output_path)
        return {
            "path": output_path,
            "sharp_edges_found": 0,
            "chamfer_triangles_added": 0,
            "triangle_count": len(triangles),
            "distance_mm": distance_mm,
        }

    # Generate chamfer geometry: for each sharp edge, add a flat bevel
    # strip offset along both face normals.
    chamfer_tris: list[tuple[tuple[float, ...], ...]] = []

    for edge in sharp_edges:
        va, vb = edge
        normals = edge_faces[edge]
        if len(normals) != 2:
            continue
        n1, n2 = normals[0], normals[1]

        # Offset points along each face normal
        va_off1 = (
            va[0] + n1[0] * distance_mm,
            va[1] + n1[1] * distance_mm,
            va[2] + n1[2] * distance_mm,
        )
        va_off2 = (
            va[0] + n2[0] * distance_mm,
            va[1] + n2[1] * distance_mm,
            va[2] + n2[2] * distance_mm,
        )
        vb_off1 = (
            vb[0] + n1[0] * distance_mm,
            vb[1] + n1[1] * distance_mm,
            vb[2] + n1[2] * distance_mm,
        )
        vb_off2 = (
            vb[0] + n2[0] * distance_mm,
            vb[1] + n2[1] * distance_mm,
            vb[2] + n2[2] * distance_mm,
        )

        # Two triangles forming the chamfer quad
        chamfer_tris.append((va_off1, vb_off1, va_off2))
        chamfer_tris.append((va_off2, vb_off1, vb_off2))

    combined = list(triangles) + chamfer_tris

    if output_path is None:
        output_path = str(path.with_name(f"{path.stem}_chamfered.stl"))

    _write_binary_stl(combined, output_path)

    return {
        "path": output_path,
        "sharp_edges_found": len(sharp_edges),
        "chamfer_triangles_added": len(chamfer_tris),
        "triangle_count": len(combined),
        "distance_mm": distance_mm,
        "angle_threshold_deg": angle_threshold_deg,
    }


def detect_mesh_pockets(
    file_path: str,
    *,
    min_depth_mm: float = 0.3,
    face_normal_tolerance: float = 0.15,
) -> dict[str, Any]:
    """Detect pockets and cavities on the top/bottom faces of a mesh.

    Analyzes an STL file for recessed regions (circular or rectangular
    pockets) by clustering Z-up and Z-down face normals at different
    height levels.  Useful for pre-composition audit before placing
    overlay geometry (QR pads, logos) into a base model's pockets.

    :param file_path: Path to the STL file.
    :param min_depth_mm: Minimum pocket depth to report.
    :param face_normal_tolerance: How close to ±1.0 the Z-component
        of a face normal must be to count as a top/bottom face.
    :returns: Dict with pocket list, main surface heights, and bounding box.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    errors: list[str] = []
    triangles, vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"STL parse errors: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("STL file contains no triangles.")

    bbox = _bounding_box(vertices)
    overall_height = bbox["z_max"] - bbox["z_min"]

    # -- Compute face normals and classify triangles -------------------------
    z_up_threshold = 1.0 - face_normal_tolerance
    z_down_threshold = -(1.0 - face_normal_tolerance)

    # Each entry: (avg_z, tri_vertices)
    z_up_faces: list[tuple[float, tuple[tuple[float, ...], ...]]] = []
    z_down_faces: list[tuple[float, tuple[tuple[float, ...], ...]]] = []

    for tri in triangles:
        v0, v1, v2 = tri
        # Cross product (v1-v0) x (v2-v0)
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        nx = e1[1] * e2[2] - e1[2] * e2[1]
        ny = e1[2] * e2[0] - e1[0] * e2[2]
        nz = e1[0] * e2[1] - e1[1] * e2[0]
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length < 1e-12:
            continue
        nz_norm = nz / length

        avg_z = (v0[2] + v1[2] + v2[2]) / 3.0

        if nz_norm > z_up_threshold:
            z_up_faces.append((avg_z, tri))
        elif nz_norm < z_down_threshold:
            z_down_faces.append((avg_z, tri))

    # -- Cluster faces by Z height -------------------------------------------
    _Z_CLUSTER_TOL = 0.05  # mm

    def _cluster_z_levels(
        faces: list[tuple[float, tuple[tuple[float, ...], ...]]],
    ) -> dict[float, list[tuple[tuple[float, ...], ...]]]:
        """Group faces into Z-height clusters within tolerance."""
        if not faces:
            return {}
        sorted_faces = sorted(faces, key=lambda f: f[0])
        clusters: dict[float, list[tuple[tuple[float, ...], ...]]] = {}
        current_z = sorted_faces[0][0]
        current_zs: list[float] = [current_z]
        current_tris: list[tuple[tuple[float, ...], ...]] = [sorted_faces[0][1]]

        for avg_z, tri in sorted_faces[1:]:
            if abs(avg_z - current_z) <= _Z_CLUSTER_TOL:
                current_zs.append(avg_z)
                current_tris.append(tri)
            else:
                representative_z = sum(current_zs) / len(current_zs)
                clusters[representative_z] = current_tris
                current_z = avg_z
                current_zs = [avg_z]
                current_tris = [tri]

        representative_z = sum(current_zs) / len(current_zs)
        clusters[representative_z] = current_tris
        return clusters

    # -- Identify pockets from clustered face levels -------------------------
    def _pocket_shape(
        tris: list[tuple[tuple[float, ...], ...]],
    ) -> tuple[str, float | None, float, float, float, float]:
        """Determine pocket shape from its triangles.

        Returns (shape, radius_or_none, width, height, center_x, center_y).
        """
        xs: list[float] = []
        ys: list[float] = []
        for tri in tris:
            for v in tri:
                xs.append(v[0])
                ys.append(v[1])
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        width = x_max - x_min
        height = y_max - y_min
        cx = (x_min + x_max) / 2.0
        cy = (y_min + y_max) / 2.0

        # Circular if width ≈ height within 20%
        avg_dim = (width + height) / 2.0
        if avg_dim > 0 and abs(width - height) / avg_dim < 0.2:
            return "circular", avg_dim / 2.0, width, height, cx, cy
        return "rectangular", None, width, height, cx, cy

    def _find_pockets(
        faces: list[tuple[float, tuple[tuple[float, ...], ...]]],
        face_label: str,
    ) -> tuple[list[dict[str, Any]], float]:
        """Find pockets in a set of same-direction faces.

        For 'top' faces, the highest cluster is the main surface and lower
        clusters are pockets.  For 'bottom' faces, the lowest cluster is
        the main surface and higher clusters are pockets (recessed upward).

        Returns (pocket_list, main_surface_z).
        """
        clusters = _cluster_z_levels(faces)
        if not clusters:
            return [], 0.0

        sorted_levels = sorted(clusters.keys())

        if face_label == "top":
            main_z = sorted_levels[-1]
            pocket_levels = sorted_levels[:-1]
        else:
            main_z = sorted_levels[0]
            pocket_levels = sorted_levels[1:]

        pockets: list[dict[str, Any]] = []
        for level_z in pocket_levels:
            if face_label == "top":
                depth = main_z - level_z
            else:
                depth = level_z - main_z

            if depth < min_depth_mm:
                continue

            tris = clusters[level_z]
            shape, radius, w, h, cx, cy = _pocket_shape(tris)
            pockets.append({
                "face": face_label,
                "center_x": round(cx, 3),
                "center_y": round(cy, 3),
                "floor_z": round(level_z, 3),
                "depth_mm": round(depth, 3),
                "shape": shape,
                "radius_mm": round(radius, 3) if radius is not None else None,
                "width_mm": round(w, 3),
                "height_mm": round(h, 3),
                "triangle_count": len(tris),
            })

        return pockets, main_z

    top_pockets, main_top_z = _find_pockets(z_up_faces, "top")
    bottom_pockets, main_bottom_z = _find_pockets(z_down_faces, "bottom")

    # Fallback if no Z-up/Z-down faces found
    if not z_up_faces:
        main_top_z = bbox["z_max"]
    if not z_down_faces:
        main_bottom_z = bbox["z_min"]

    return {
        "pockets": top_pockets + bottom_pockets,
        "main_top_z": round(main_top_z, 3),
        "main_bottom_z": round(main_bottom_z, 3),
        "overall_height_mm": round(overall_height, 3),
        "bounding_box": {k: round(v, 3) for k, v in bbox.items()},
    }


# ---------------------------------------------------------------------------
# Cylindrical hole detection
# ---------------------------------------------------------------------------
#
# Holes are detected by finding clusters of triangles whose face normals are
# perpendicular to one principal axis (X/Y/Z) — these are the cylindrical
# inner-wall faces of a hole drilled along that axis.  Inside each axis
# bucket, we collect every vertex from the candidate triangles, project to
# the plane perpendicular to that axis, and cluster the 2D points into
# circles via a fixed-tolerance grid: any cluster whose points fit on a
# circle to within ``circular_tolerance_mm`` and whose face normals all
# point inward (toward the cluster center) is a cylindrical hole.
#
# The algorithm is geometry-only — no topology walk required — so it works
# uniformly on STL, OBJ, and GLB inputs once they parse, and degrades to
# the empty list when the mesh is too coarse to recover a usable ring (a
# 12-triangle cube has no hole but also no false positives).
#
# Coordinate convention matches the rest of this module: axis ``"x"`` means
# the hole's axis runs along the world X direction, so the radial profile
# sits in the YZ plane.


def detect_holes(
    file_path: str,
    *,
    min_diameter_mm: float = 0.8,
    max_diameter_mm: float = 50.0,
    min_depth_mm: float = 0.5,
    circular_tolerance_mm: float = 0.25,
    axis_normal_tolerance: float = 0.15,
    diagnostics: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Detect cylindrical hole features in an STL mesh.

    Recovers each hole's axis from its own geometry, so a part rotated
    arbitrarily (slicer auto-orient, hand-tilted CAD export, etc.) is
    treated identically to one laid flat against a principal axis.

    Algorithm:

    1. Flood-fill the mesh's triangles into clusters by edge adjacency,
       allowing an edge to cross only when the two triangles' face
       normals agree within ~60° (cosine ≥ 0.5).  A box edge has 90°
       between adjacent normals (cosine 0) so the BFS breaks there; a
       cylinder's adjacent wall triangles disagree by ~360°/segments
       (well above the threshold) so they stay one cluster.
    2. For each cluster, recover the cylinder axis as the eigenvector
       of the smallest eigenvalue of the unit-normal second-moment
       matrix.  Inward-facing radial normals are perpendicular to the
       axis, so the smallest eigenvector IS the axis regardless of
       part orientation.
    3. Project triangle centroids into the plane perpendicular to that
       axis and run the radius-bounds, octant-coverage, circularity,
       and inward-normal gates against the (u, v) coordinates.

    The detector is intentionally conservative — partial rings,
    elliptical relief cuts, and chamfered hole entries do not register
    as holes unless they keep a circular profile within
    ``circular_tolerance_mm`` of the radius.  False positives are worse
    than false negatives here: a missed hole means a missed warning,
    but a phantom hole means a misleading printability finding.

    :param file_path: Path to an STL file (binary or ASCII).
    :param min_diameter_mm: Smallest diameter to report.  Defaults to
        0.8 mm — below this a single-perimeter 0.4 mm nozzle can't
        reliably print the hole regardless of material, so the absence
        of a finding at that size is itself a signal.
    :param max_diameter_mm: Largest diameter to consider.  Defaults to
        50 mm — clusters wider than this are usually outer shells, not
        through-holes; raising it picks up large counter-bores at the
        cost of more false positives on tubular parts.
    :param min_depth_mm: Minimum extent along the hole's axis required
        to register a hole.  Defaults to 0.5 mm so a chamfered edge
        doesn't read as a shallow hole.
    :param circular_tolerance_mm: How far (in mm) a candidate vertex's
        distance from the fitted center may deviate before the cluster
        is rejected as non-circular.
    :param axis_normal_tolerance: After a cluster's candidate axis is
        recovered (via PCA on the cluster's unit-normal second-moment
        matrix), triangles are filtered to those whose face normal is
        perpendicular to that axis — formally,
        ``|n · axis| < axis_normal_tolerance``.  This filter removes
        chamfered hole entries and annular caps that BFS pulled into
        the cluster, so the cylinder wall reads as a clean cylinder
        when the validation gates run.  The 0.15 default (≈ 81° wedge
        either side of the perpendicular plane) matches the v1.1.x
        per-axis-filter behavior; raising it admits steeper chamfers
        at the cost of mixing them into the radius fit, lowering it
        excludes anything but pure-radial wall triangles.
    :param diagnostics: When supplied, the detector populates this
        mutable dict in-place with rejection counters so callers can
        emit informational notices about features that "looked like
        a hole but didn't qualify."  Keys (all int):

        * ``"sub_floor_clusters"`` — a SMOOTH round bore below
          ``min_diameter_mm`` (> 8 distinct facet directions in the
          perpendicular plane).  Typical signal: a designed hole
          below FDM's practical floor for a 0.4 mm nozzle, which
          won't print reliably.  The printability layer surfaces a
          "drill after printing" recommendation for these.
        * ``"sub_floor_polygonal_clusters"`` — a POLYGONAL pocket
          below ``min_diameter_mm`` (≤ 8 distinct facet directions
          — hex nut traps, octagonal sockets).  Same dimensional
          concern, but no drill-after-printing workaround, so the
          recommendation wording differs.
        * ``"non_circular_clusters"`` — axis-perpendicular clusters
          whose radius spread exceeded ``circular_tolerance_mm``.
          Typical signals: slots, elliptical reliefs, chamfered or
          tapered hole entries.
        * ``"oversize_clusters"`` — clusters larger than
          ``max_diameter_mm``; usually outer shells of tubes.
        * ``"partial_arc_clusters"`` — clusters covering < 6 of 8
          octants, or clusters that span the mesh AABB (the latter is
          typically an annulus + outer-wall artefact, not a real
          feature).
        * ``"pillar_clusters"`` — outward-facing cylinders (solid
          pillars rather than holes).

        Pass ``None`` (default) to skip diagnostic accounting — preserves
        backwards compatibility for callers that don't need notices.

    :returns: List of dicts, each with the keys:

        * ``position`` — dict with ``x_mm`` / ``y_mm`` / ``z_mm`` of the
          hole's geometric center along its axis.
        * ``diameter_mm`` — float, fitted diameter of the cylinder.
        * ``depth_mm`` — float, extent along the hole's axis.
        * ``axis`` — ``"x"``, ``"y"``, or ``"z"``: the world axis the
          recovered cylinder axis is closest to (largest |component|).
          Holes whose tilt is far from any principal axis still report
          via this best-match label; downstream callers that need the
          exact axis direction should rely on the geometry, not the
          label.
        * ``triangle_count`` — int, triangles contributing to the ring.
        * ``facet_segments`` — int, distinct face-normal directions
          around the cylinder, snapped to 10° bins.  A smoothly
          tessellated 5 mm round hole exported from CAD has
          24-48 facets; a hex nut trap has 6; an octagonal pocket has
          8.  The geometry of a "6-segment circle" and a hex pocket
          is identical, so the detector cannot tell intent apart from
          the mesh — but ``facet_segments`` lets downstream callers
          (printability recommendations, kiln-pro overlays) treat a
          ≤ 8-facet detection as "polygonal pocket, not a drilled
          hole" and suppress hole-diameter warnings accordingly.

        Empty list when no holes are recovered (no triangles, mesh too
        coarse, or every candidate failed the circularity / depth
        gates).

    :raises FileNotFoundError: When ``file_path`` doesn't exist.
    :raises ValueError: When the mesh cannot be parsed.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if min_diameter_mm <= 0:
        raise ValueError("min_diameter_mm must be positive")
    if max_diameter_mm <= min_diameter_mm:
        raise ValueError("max_diameter_mm must exceed min_diameter_mm")

    errors: list[str] = []
    triangles, _vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"STL parse errors: {'; '.join(errors)}")
    if not triangles:
        return []

    # Per-triangle: unit face normal + centroid.
    tri_normals: list[tuple[float, float, float]] = []
    tri_centroids: list[tuple[float, float, float]] = []
    for tri in triangles:
        v0, v1, v2 = tri
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        nx = e1[1] * e2[2] - e1[2] * e2[1]
        ny = e1[2] * e2[0] - e1[0] * e2[2]
        nz = e1[0] * e2[1] - e1[1] * e2[0]
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length < 1e-12:
            tri_normals.append((0.0, 0.0, 0.0))
            tri_centroids.append((
                (v0[0] + v1[0] + v2[0]) / 3.0,
                (v0[1] + v1[1] + v2[1]) / 3.0,
                (v0[2] + v1[2] + v2[2]) / 3.0,
            ))
            continue
        tri_normals.append((nx / length, ny / length, nz / length))
        tri_centroids.append((
            (v0[0] + v1[0] + v2[0]) / 3.0,
            (v0[1] + v1[1] + v2[1]) / 3.0,
            (v0[2] + v1[2] + v2[2]) / 3.0,
        ))

    # Drop degenerate triangles (zero normal) — they'd otherwise match
    # everything in the cohesion BFS.
    candidate_idx: list[int] = [
        i
        for i, n in enumerate(tri_normals)
        if (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) > 0.25
    ]
    if not candidate_idx:
        return []

    # Mesh AABB extents — used by the cluster gate to filter mesh-spanning
    # clusters (annulus + outer-wall BFS bleed-through) that look like a
    # slot but are really part-bounding geometry.
    all_x = [v[0] for tri in triangles for v in tri]
    all_y = [v[1] for tri in triangles for v in tri]
    all_z = [v[2] for tri in triangles for v in tri]
    mesh_extent_xyz = (
        max(all_x) - min(all_x),
        max(all_y) - min(all_y),
        max(all_z) - min(all_z),
    )

    # Single global pass: edge-adjacency flood fill, with a normal-cohesion
    # gate on every edge crossing.  Cylinder walls (consecutive normals
    # differing by ~360°/segments) flow together; box edges (90° between
    # neighbouring face normals → cosine 0) break the BFS so the cylinder
    # cluster never merges with the surrounding flat geometry.  Each
    # triangle belongs to exactly one cluster, so the per-axis-pass
    # "claiming" trick the old detector needed is gone too.
    #
    # Threshold 0.45 sits comfortably below cos(60°) = 0.5 — the
    # adjacent-triangle angle a six-segment cylinder produces — so the
    # ``0.4999…`` floating-point noise that lands right at the cos(60°)
    # boundary doesn't shatter sparse cylinders.  Below ~0.4 the BFS
    # starts bridging chamfered box edges (a 60° chamfer to its
    # adjacent flat face has cos 0.5), which would re-introduce the
    # cluster-contamination problem we solve via the per-cluster
    # axis-perpendicularity filter inside ``_cluster_circular_holes``.
    raw_clusters = _bfs_cluster_by_normal_cohesion(
        candidate_idx,
        triangles,
        tri_normals,
        cos_threshold=0.45,
    )

    holes: list[dict[str, Any]] = []
    min_radius = min_diameter_mm / 2.0
    max_radius = max_diameter_mm / 2.0
    for cluster in raw_clusters:
        if len(cluster) < 3:
            continue
        result = _cluster_circular_holes(
            cluster,
            tri_normals,
            tri_centroids,
            triangles,
            min_radius=min_radius,
            max_radius=max_radius,
            circular_tol=circular_tolerance_mm,
            min_depth_mm=min_depth_mm,
            axis_perp_tolerance=axis_normal_tolerance,
            mesh_extent_xyz=mesh_extent_xyz,
            diagnostics=diagnostics,
        )
        if result is not None:
            holes.append(result)

    return holes


# Edge adjacency in ``_cluster_circular_holes`` keys on the (snapped)
# vertex tuples shared by two triangles.  Raw float-tuple equality works
# for STLs parsed straight off disk because adjacent triangles produce
# literally-equal floats — but a mesh that has been rotated, scaled, or
# decimated develops sub-micrometre drift that breaks tuple equality,
# silently turning every triangle into its own component of size 1.
# Snapping each vertex to a 10 nm integer grid before keying preserves
# adjacency under realistic transforms while still keeping distinct
# vertices (printer resolution is ~0.1 mm — ten thousand snap cells
# coarser than the snap tolerance, so collapse is implausible).
_HOLE_EDGE_SNAP_TOL_MM: float = 1e-5


def _snap_vertex(
    v: tuple[float, ...],
    tol: float = _HOLE_EDGE_SNAP_TOL_MM,
) -> tuple[int, int, int]:
    """Map a 3-float vertex to an integer-grid coordinate.

    Used as the dict key for edge-adjacency lookups in
    ``_bfs_cluster_by_normal_cohesion`` so that two triangles sharing
    an edge in a transformed mesh still collide on the same key despite
    small floating-point drift.
    """
    return (
        round(v[0] / tol),
        round(v[1] / tol),
        round(v[2] / tol),
    )


def _bfs_cluster_by_normal_cohesion(
    candidate_idx: list[int],
    triangles: list[tuple[tuple[float, ...], ...]],
    tri_normals: list[tuple[float, float, float]],
    *,
    cos_threshold: float,
) -> list[list[int]]:
    """Edge-adjacency flood fill with a normal-cohesion gate.

    Two adjacent candidate triangles join the same cluster only when
    the cosine between their (unit) face normals is ≥
    ``cos_threshold``.  Box edges (cosine 0 between perpendicular
    faces) break the BFS; cylinder walls (cosine = cos(2π/segments),
    typically ≥ 0.87 for segments ≥ 12) flow through it freely.  That
    keeps a hole's cylindrical wall cluster separate from the
    surrounding flat geometry it shares vertices with at the bore mouth
    — no per-axis filter required.
    """
    candidate_set = set(candidate_idx)
    edge_to_tris: dict[
        tuple[tuple[int, int, int], tuple[int, int, int]], list[int]
    ] = {}
    for ti in candidate_idx:
        tri = triangles[ti]
        for i in range(3):
            va = _snap_vertex(tri[i])
            vb = _snap_vertex(tri[(i + 1) % 3])
            edge = (min(va, vb), max(va, vb))
            edge_to_tris.setdefault(edge, []).append(ti)

    visited: set[int] = set()
    clusters: list[list[int]] = []
    for seed in candidate_idx:
        if seed in visited:
            continue
        cluster: list[int] = []
        stack = [seed]
        while stack:
            ti = stack.pop()
            if ti in visited:
                continue
            visited.add(ti)
            cluster.append(ti)
            n_a = tri_normals[ti]
            tri = triangles[ti]
            for i in range(3):
                va = _snap_vertex(tri[i])
                vb = _snap_vertex(tri[(i + 1) % 3])
                edge = (min(va, vb), max(va, vb))
                for neighbor in edge_to_tris.get(edge, []):
                    if neighbor in visited or neighbor not in candidate_set:
                        continue
                    n_b = tri_normals[neighbor]
                    cos_ab = (
                        n_a[0] * n_b[0]
                        + n_a[1] * n_b[1]
                        + n_a[2] * n_b[2]
                    )
                    if cos_ab >= cos_threshold:
                        stack.append(neighbor)
        clusters.append(cluster)
    return clusters


def _eigvec_from_eigval_3x3(
    m00: float, m01: float, m02: float,
    m11: float, m12: float, m22: float,
    eigval: float,
) -> tuple[float, float, float] | None:
    """Unit eigenvector of a 3x3 symmetric matrix for the given
    eigenvalue.

    Returns ``None`` when the matrix is rank-deficient at this
    eigenvalue and no two rows of ``M - eigval I`` cross to a
    non-zero vector (degenerate / repeated-eigenvalue corner).
    """
    a00 = m00 - eigval
    a11 = m11 - eigval
    a22 = m22 - eigval
    rows = (
        (a00, m01, m02),
        (m01, a11, m12),
        (m02, m12, a22),
    )
    norms = [r[0] * r[0] + r[1] * r[1] + r[2] * r[2] for r in rows]
    order = sorted(range(3), key=lambda i: -norms[i])
    r1 = rows[order[0]]
    for j in (1, 2):
        r2 = rows[order[j]]
        vx = r1[1] * r2[2] - r1[2] * r2[1]
        vy = r1[2] * r2[0] - r1[0] * r2[2]
        vz = r1[0] * r2[1] - r1[1] * r2[0]
        mag = math.sqrt(vx * vx + vy * vy + vz * vz)
        if mag > 1e-12:
            return (vx / mag, vy / mag, vz / mag)
    return None


def _all_eigvecs_3x3(
    m00: float, m01: float, m02: float,
    m11: float, m12: float, m22: float,
) -> list[tuple[float, float, float]]:
    """Return up to three unit eigenvectors of a 3x3 symmetric matrix,
    sorted by eigenvalue ascending.

    Closed-form (Smith's trigonometric formula for the cubic roots),
    so no numpy dependency.  Returns an empty list when the matrix
    is numerically a multiple of the identity — all eigenvectors
    indistinguishable.

    The cluster's unit-normal second-moment matrix on a true cylinder
    has eigenvalues (λ, λ, 0) with the zero eigenvector ALONG the
    axis; on a cluster contaminated by annulus + chamfer the axis
    can instead show up as the LARGEST eigenvector (cylinder
    contributes ~equally to two XY-plane eigenvalues, annulus piles
    Z² onto the third).  Returning all three lets the validator try
    each candidate axis in turn.
    """
    trace = m00 + m11 + m22
    q = trace / 3.0
    d00 = m00 - q
    d11 = m11 - q
    d22 = m22 - q
    p1_sq = m01 * m01 + m02 * m02 + m12 * m12
    p2_sq = d00 * d00 + d11 * d11 + d22 * d22 + 2.0 * p1_sq
    if p2_sq < 1e-20:
        return []
    p = math.sqrt(p2_sq / 6.0)
    b00 = d00 / p
    b11 = d11 / p
    b22 = d22 / p
    b01 = m01 / p
    b02 = m02 / p
    b12 = m12 / p
    det_b = (
        b00 * (b11 * b22 - b12 * b12)
        - b01 * (b01 * b22 - b12 * b02)
        + b02 * (b01 * b12 - b11 * b02)
    )
    r = max(-1.0, min(1.0, det_b / 2.0))
    phi = math.acos(r) / 3.0
    eig1 = q + 2.0 * p * math.cos(phi)
    eig3 = q + 2.0 * p * math.cos(phi + 2.0 * math.pi / 3.0)
    eig2 = trace - eig1 - eig3
    eigvals_sorted = sorted([eig1, eig2, eig3])

    out: list[tuple[float, float, float]] = []
    for ev in eigvals_sorted:
        v = _eigvec_from_eigval_3x3(m00, m01, m02, m11, m12, m22, ev)
        if v is None:
            continue
        # Deduplicate near-collinear vectors (multiplicity > 1).
        if any(abs(v[0] * u[0] + v[1] * u[1] + v[2] * u[2]) > 0.999
               for u in out):
            continue
        out.append(v)
    return out


# A detected hole with this many or fewer distinct facet directions
# (snapped to 10° bins around the recovered axis) is classified as a
# polygonal pocket rather than a smooth bore.  Standard CAD exports
# tessellate round holes at 24-48 segments; nut traps and intentional
# polygonal pockets land at 6-8.  The boundary at 8 captures both hex
# (6) and octagonal (8) pockets while leaving 12-segment "coarse-but-
# round" holes on the smooth side.
_POLYGONAL_FACET_THRESHOLD: int = 8


def _count_facet_directions_uv(
    triangles_idx: list[int],
    tri_normals: list[tuple[float, float, float]],
    u_hat: tuple[float, float, float],
    v_hat: tuple[float, float, float],
    *,
    snap_deg: float = 10.0,
) -> int:
    """Distinct face-normal directions in the cluster's (u, v) plane.

    Used both by the public ``facet_segments`` field on detected holes
    and by the sub-floor diagnostic split (polygonal pockets routed to
    ``sub_floor_polygonal_clusters`` so printability can word the
    recommendation accurately).
    """
    bins: set[int] = set()
    for ti in triangles_idx:
        n = tri_normals[ti]
        n_u = n[0] * u_hat[0] + n[1] * u_hat[1] + n[2] * u_hat[2]
        n_v = n[0] * v_hat[0] + n[1] * v_hat[1] + n[2] * v_hat[2]
        angle_deg = math.atan2(n_v, n_u) * 180.0 / math.pi
        bins.add(round(angle_deg / snap_deg))
    return len(bins)


def _basis_perpendicular_to(
    axis: tuple[float, float, float],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Two orthonormal vectors perpendicular to ``axis`` (unit input).

    Picks whichever world basis vector has the smallest |dot| with
    ``axis`` to cross against, which keeps the first cross-product
    far from the zero vector even for near-axis-aligned inputs.
    """
    ax, ay, az = axis
    abs_ax, abs_ay, abs_az = abs(ax), abs(ay), abs(az)
    if abs_ax <= abs_ay and abs_ax <= abs_az:
        ux, uy, uz = 0.0, az, -ay
    elif abs_ay <= abs_az:
        ux, uy, uz = -az, 0.0, ax
    else:
        ux, uy, uz = ay, -ax, 0.0
    mag = math.sqrt(ux * ux + uy * uy + uz * uz)
    ux /= mag
    uy /= mag
    uz /= mag
    vx = ay * uz - az * uy
    vy = az * ux - ax * uz
    vz = ax * uy - ay * ux
    return (ux, uy, uz), (vx, vy, vz)


def _cluster_circular_holes(
    cluster: list[int],
    tri_normals: list[tuple[float, float, float]],
    tri_centroids: list[tuple[float, float, float]],
    triangles: list[tuple[tuple[float, ...], ...]],
    *,
    min_radius: float,
    max_radius: float,
    circular_tol: float,
    min_depth_mm: float,
    axis_perp_tolerance: float,
    mesh_extent_xyz: tuple[float, float, float],
    diagnostics: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Validate one cohesion-BFS cluster as a cylindrical hole.

    Cohesion BFS at threshold 0.45 occasionally pulls a chamfered hole
    entry or an annular cap into the cylinder wall's cluster — the
    chamfer-to-wall and chamfer-to-annulus transitions both sit at
    cos 0.707, well above the BFS threshold.  When that happens the
    raw cluster has cylinder + chamfer + annulus normals mixed
    together, and the smallest eigenvector of the unit-normal
    second-moment matrix doesn't land on the cylinder axis any more
    (annulus piles Z² onto one diagonal element until the axis
    direction has the LARGEST eigenvalue instead).

    Fix: try every PCA eigenvector as a candidate axis.  For each,
    filter the cluster to triangles whose face normal is perpendicular
    to that axis (``|n · axis| < axis_perp_tolerance``) and run the
    validation gates against just those triangles.  The first axis
    that yields a hole wins — for a clean cylinder that's the smallest
    eigenvector (axis lies in the null-space of the normal covariance);
    for a chamfered/annulus-dominated cluster it's the largest.

    Diagnostic counters fire only when EVERY candidate axis fails —
    bumping them on the first attempt would spuriously flag a
    chamfered hole as a non-circular feature in the user-facing
    recommendations.

    Validation gates (run once per candidate axis):

    1. Filter cluster to triangles perpendicular to the candidate
       axis.  Skip the axis when fewer than 3 triangles survive.
    2. Project filtered centroids into the plane perpendicular to the
       axis; fit center as mean (u, v).
    3. Octant coverage (≥ 6 of 8 — rejects partial arcs / flat
       projections).
    4. Mesh-spanning gate (annulus + outer-wall BFS bleed-through).
    5. Radius bounds (sub-floor vs oversize).
    6. Circularity (radius spread within ``circular_tol``).
    7. Inward-normal check (rejects solid pillars).
    8. Depth along the axis ≥ ``min_depth_mm``.
    """
    # PCA on the FULL cluster's unit-normal second-moment matrix —
    # gives us the candidate axes regardless of cluster contamination.
    m00 = m01 = m02 = m11 = m12 = m22 = 0.0
    for ti in cluster:
        nx, ny, nz = tri_normals[ti]
        m00 += nx * nx
        m01 += nx * ny
        m02 += nx * nz
        m11 += ny * ny
        m12 += ny * nz
        m22 += nz * nz
    n_inv = 1.0 / float(len(cluster))
    m00 *= n_inv
    m01 *= n_inv
    m02 *= n_inv
    m11 *= n_inv
    m12 *= n_inv
    m22 *= n_inv
    candidates = _all_eigvecs_3x3(m00, m01, m02, m11, m12, m22)
    if not candidates:
        return None

    # Diagnostics policy: a cluster that succeeds on ANY candidate axis
    # is a real hole — bumping diagnostics on the failed attempts would
    # spam user-facing notices on contaminated chamfered-hole clusters
    # (the smallest-eigvec attempt fails circularity, then the largest
    # succeeds — the user shouldn't see "1 non-circular feature" for a
    # hole that's actually detected).  When ALL axes fail, the
    # smallest-eigvec attempt is the most informative diagnostic
    # because for true slots / partial arcs the smallest eigenvector
    # IS the slot's bore axis, so its gate failure is the right one
    # to surface.
    first_attempt_diag: dict[str, int] = {}
    for idx, axis in enumerate(candidates):
        diag_target = first_attempt_diag if idx == 0 else None
        result = _validate_cluster_against_axis(
            cluster,
            axis,
            tri_normals,
            tri_centroids,
            triangles,
            min_radius=min_radius,
            max_radius=max_radius,
            circular_tol=circular_tol,
            min_depth_mm=min_depth_mm,
            axis_perp_tolerance=axis_perp_tolerance,
            mesh_extent_xyz=mesh_extent_xyz,
            diagnostics=diag_target,
        )
        if result is not None:
            return result
    if diagnostics is not None:
        for key, count in first_attempt_diag.items():
            diagnostics[key] = diagnostics.get(key, 0) + count
    return None


def _validate_cluster_against_axis(
    cluster: list[int],
    axis: tuple[float, float, float],
    tri_normals: list[tuple[float, float, float]],
    tri_centroids: list[tuple[float, float, float]],
    triangles: list[tuple[tuple[float, ...], ...]],
    *,
    min_radius: float,
    max_radius: float,
    circular_tol: float,
    min_depth_mm: float,
    axis_perp_tolerance: float,
    mesh_extent_xyz: tuple[float, float, float],
    diagnostics: dict[str, int] | None,
) -> dict[str, Any] | None:
    """Validate a cluster against a specific candidate axis.

    See ``_cluster_circular_holes`` for the gate order.  Returns a
    hole dict on accept, ``None`` on reject (with the matching
    diagnostic counter bumped in-place).
    """
    # Step 1: filter to triangles whose normal is perpendicular to
    # the candidate axis.  This is what excludes chamfer + annulus
    # contributions and leaves just the cylinder wall in the
    # filtered subset.
    filtered: list[int] = []
    for ti in cluster:
        n = tri_normals[ti]
        n_dot_axis = n[0] * axis[0] + n[1] * axis[1] + n[2] * axis[2]
        if abs(n_dot_axis) < axis_perp_tolerance:
            filtered.append(ti)
    if len(filtered) < 3:
        return None

    u_hat, v_hat = _basis_perpendicular_to(axis)

    # Step 2: project filtered centroids onto the (u, v) plane.
    proj_uv: list[tuple[float, float]] = []
    for ti in filtered:
        c = tri_centroids[ti]
        u = c[0] * u_hat[0] + c[1] * u_hat[1] + c[2] * u_hat[2]
        v = c[0] * v_hat[0] + c[1] * v_hat[1] + c[2] * v_hat[2]
        proj_uv.append((u, v))
    n_filtered = len(proj_uv)
    c_u = sum(p[0] for p in proj_uv) / n_filtered
    c_v = sum(p[1] for p in proj_uv) / n_filtered

    radii: list[float] = []
    angles: list[float] = []
    for u, v in proj_uv:
        du = u - c_u
        dv = v - c_v
        radii.append(math.sqrt(du * du + dv * dv))
        angles.append(math.atan2(dv, du))
    r_mean = sum(radii) / n_filtered

    # Step 3: octant-coverage gate.
    octant_hits = [0] * 8
    for ang in angles:
        octant_hits[int((ang + math.pi) / (math.pi / 4.0)) % 8] = 1
    if sum(octant_hits) < 6:
        if diagnostics is not None:
            diagnostics["partial_arc_clusters"] = (
                diagnostics.get("partial_arc_clusters", 0) + 1
            )
        return None

    # Step 4: mesh-spanning + would-fail-circularity gate.
    spread = max(radii) - min(radii)
    would_fail_circ = spread > circular_tol * 2.0
    if would_fail_circ:
        u_min = min(p[0] for p in proj_uv)
        u_max = max(p[0] for p in proj_uv)
        v_min = min(p[1] for p in proj_uv)
        v_max = max(p[1] for p in proj_uv)
        mesh_u_extent = (
            abs(u_hat[0]) * mesh_extent_xyz[0]
            + abs(u_hat[1]) * mesh_extent_xyz[1]
            + abs(u_hat[2]) * mesh_extent_xyz[2]
        )
        mesh_v_extent = (
            abs(v_hat[0]) * mesh_extent_xyz[0]
            + abs(v_hat[1]) * mesh_extent_xyz[1]
            + abs(v_hat[2]) * mesh_extent_xyz[2]
        )
        u_fraction = (
            (u_max - u_min) / mesh_u_extent if mesh_u_extent > 0 else 0.0
        )
        v_fraction = (
            (v_max - v_min) / mesh_v_extent if mesh_v_extent > 0 else 0.0
        )
        if u_fraction >= 0.9 or v_fraction >= 0.9:
            if diagnostics is not None:
                diagnostics["partial_arc_clusters"] = (
                    diagnostics.get("partial_arc_clusters", 0) + 1
                )
            return None

    # Step 5: radius bounds.  Sub-floor rejects split by facet count
    # so the printability layer can word the recommendation
    # accurately — a 0.5 mm round bore needs "drill after printing,"
    # a 0.5 mm hex pocket can't be drilled and is probably an
    # intentional setscrew socket.
    if r_mean < min_radius:
        if diagnostics is not None:
            facet_count = _count_facet_directions_uv(
                filtered, tri_normals, u_hat, v_hat,
            )
            key = (
                "sub_floor_polygonal_clusters"
                if facet_count <= _POLYGONAL_FACET_THRESHOLD
                else "sub_floor_clusters"
            )
            diagnostics[key] = diagnostics.get(key, 0) + 1
        return None
    if r_mean > max_radius:
        if diagnostics is not None:
            diagnostics["oversize_clusters"] = (
                diagnostics.get("oversize_clusters", 0) + 1
            )
        return None

    # Step 6: circularity.
    if spread > circular_tol * 2.0:
        if diagnostics is not None:
            diagnostics["non_circular_clusters"] = (
                diagnostics.get("non_circular_clusters", 0) + 1
            )
        return None

    # Step 7: inward-normal check (rejects outward-facing pillars).
    inward_dot_sum = 0.0
    for ti, (u, v) in zip(filtered, proj_uv, strict=True):
        n_vec = tri_normals[ti]
        du = u - c_u
        dv = v - c_v
        r_mag = math.sqrt(du * du + dv * dv)
        if r_mag < 1e-9:
            continue
        rad_x = (du * u_hat[0] + dv * v_hat[0]) / r_mag
        rad_y = (du * u_hat[1] + dv * v_hat[1]) / r_mag
        rad_z = (du * u_hat[2] + dv * v_hat[2]) / r_mag
        inward_dot_sum += (
            n_vec[0] * rad_x + n_vec[1] * rad_y + n_vec[2] * rad_z
        )
    if inward_dot_sum >= 0.0:
        if diagnostics is not None:
            diagnostics["pillar_clusters"] = (
                diagnostics.get("pillar_clusters", 0) + 1
            )
        return None

    # Step 8: depth along the axis.
    a_min = a_max = None
    for ti in filtered:
        for v_pt in triangles[ti]:
            a = v_pt[0] * axis[0] + v_pt[1] * axis[1] + v_pt[2] * axis[2]
            if a_min is None or a < a_min:
                a_min = a
            if a_max is None or a > a_max:
                a_max = a
    if a_min is None or a_max is None:
        return None
    depth = a_max - a_min
    if depth < min_depth_mm:
        if diagnostics is not None:
            diagnostics["shallow_clusters"] = (
                diagnostics.get("shallow_clusters", 0) + 1
            )
        return None
    a_center = (a_max + a_min) / 2.0

    # Step 9: reconstruct 3D position.
    px = c_u * u_hat[0] + c_v * v_hat[0] + a_center * axis[0]
    py = c_u * u_hat[1] + c_v * v_hat[1] + a_center * axis[1]
    pz = c_u * u_hat[2] + c_v * v_hat[2] + a_center * axis[2]

    # Step 10: axis label = closest world axis.
    abs_x, abs_y, abs_z = abs(axis[0]), abs(axis[1]), abs(axis[2])
    if abs_x >= abs_y and abs_x >= abs_z:
        axis_label = "x"
    elif abs_y >= abs_z:
        axis_label = "y"
    else:
        axis_label = "z"

    # Step 11: facet-segment count.  Tells callers whether the cluster
    # is a smoothly-tessellated round bore (many distinct facet
    # directions) or a polygonal pocket (a few — typically 6 for a
    # nut trap).  The dict field is purely informational; the gates
    # don't change behavior based on it, because the geometry of a
    # 6-segment "circle" and a 6-segment hex pocket is identical and
    # the intent isn't recoverable from the mesh alone.
    facet_count = _count_facet_directions_uv(
        filtered, tri_normals, u_hat, v_hat,
    )

    return {
        "position": {
            "x_mm": round(px, 3),
            "y_mm": round(py, 3),
            "z_mm": round(pz, 3),
        },
        "diameter_mm": round(2.0 * r_mean, 3),
        "depth_mm": round(depth, 3),
        "axis": axis_label,
        "triangle_count": len(filtered),
        "facet_segments": facet_count,
    }
