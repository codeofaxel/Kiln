"""Mesh validation pipeline for generated 3D models.

Validates STL and OBJ files for 3D-printing readiness: parseable
geometry, reasonable dimensions, manifold checks, and polygon counts.
Uses only the Python standard library (``struct`` for binary STL
parsing) — no external mesh libraries required.
"""

from __future__ import annotations

import contextlib
import json as _json
import logging
import struct
from pathlib import Path
from typing import Any

from kiln.generation.base import MeshValidationResult

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
        input_path: Path to the input OBJ or GLB file.
        output_path: Path for the output STL file.  Defaults to
            replacing the extension with ``.stl``.

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

    raise ValueError(f"convert_to_stl expects .obj or .glb input, got {ext!r}")


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
                            idx = int(p.split("/")[0]) - 1
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


def _check_manifold(
    triangles: list[tuple[tuple[float, ...], ...]],
    warnings: list[str],
) -> bool:
    """Check if the mesh is manifold (watertight).

    A manifold mesh has every edge shared by exactly two triangles.
    Uses a dict to count edge occurrences in O(n) time.

    Returns:
        True if manifold, False otherwise (with a warning appended).
    """
    if not triangles:
        return False

    edge_count: dict[tuple[tuple[float, ...], tuple[float, ...]], int] = {}

    for tri in triangles:
        for i in range(3):
            v_a = tri[i]
            v_b = tri[(i + 1) % 3]
            # Canonical edge order for undirected comparison.
            edge = (min(v_a, v_b), max(v_a, v_b))
            edge_count[edge] = edge_count.get(edge, 0) + 1

    non_manifold = sum(1 for c in edge_count.values() if c != 2)

    if non_manifold > 0:
        warnings.append(
            f"Mesh is not manifold (watertight): {non_manifold:,} edges are "
            f"not shared by exactly 2 triangles.  Most slicers can handle "
            f"this, but print quality may be affected."
        )
        return False

    return True


# ---------------------------------------------------------------------------
# STL writing
# ---------------------------------------------------------------------------


def _write_binary_stl(
    triangles: list[tuple[tuple[float, ...], ...]],
    output_path: str,
) -> None:
    """Write triangles to a binary STL file.

    Each triangle is a tuple of three ``(x, y, z)`` vertex tuples.
    A zero normal is written for every facet (slicers recompute normals).
    """
    with open(output_path, "wb") as fh:
        # 80-byte header (blank).
        fh.write(b"\x00" * _STL_HEADER_SIZE)
        # Triangle count as uint32 LE.
        fh.write(struct.pack("<I", len(triangles)))

        for tri in triangles:
            # Normal (0, 0, 0) — slicers will recompute.
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
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
    """Parse a binary glTF 2.0 (.glb) file into triangles and vertices.

    Reads the GLB header, JSON chunk, and BIN chunk.  Extracts
    POSITION attributes and optional indices from the first mesh
    primitive.  Returns the same (triangles, vertices) format used
    by the STL and OBJ parsers.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        errors.append(f"Could not read GLB file: {exc}")
        return [], []

    if len(data) < 12:
        errors.append("GLB file too small (< 12 bytes).")
        return [], []

    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != _GLB_MAGIC:
        errors.append(f"Not a valid GLB file (bad magic: 0x{magic:08X}).")
        return [], []

    # Parse chunks
    json_data: dict[str, Any] = {}
    bin_data: bytes = b""
    offset = 12
    while offset + 8 <= len(data):
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_len
        if chunk_type == _GLB_JSON_CHUNK:
            try:
                json_data = _json.loads(data[chunk_start:chunk_end])
            except (ValueError, UnicodeDecodeError) as exc:
                errors.append(f"GLB JSON chunk parse error: {exc}")
                return [], []
        elif chunk_type == _GLB_BIN_CHUNK:
            bin_data = data[chunk_start:chunk_end]
        offset = chunk_end

    if not json_data:
        errors.append("GLB contains no JSON chunk.")
        return [], []

    meshes = json_data.get("meshes", [])
    if not meshes:
        errors.append("GLB contains no meshes.")
        return [], []

    accessors = json_data.get("accessors", [])
    buffer_views = json_data.get("bufferViews", [])

    all_triangles: list[tuple[tuple[float, ...], ...]] = []
    all_vertices: list[tuple[float, ...]] = []
    vertex_set: set[tuple[float, ...]] = set()

    for mesh in meshes:
        for prim in mesh.get("primitives", []):
            pos_idx = prim.get("attributes", {}).get("POSITION")
            if pos_idx is None:
                continue

            positions = _read_glb_accessor(accessors, buffer_views, bin_data, pos_idx)
            if not positions:
                continue

            indices_idx = prim.get("indices")
            if indices_idx is not None:
                raw_indices = _read_glb_accessor_scalar(accessors, buffer_views, bin_data, indices_idx)
            else:
                raw_indices = list(range(len(positions)))

            # Build triangles from index list
            for i in range(0, len(raw_indices) - 2, 3):
                i0, i1, i2 = raw_indices[i], raw_indices[i + 1], raw_indices[i + 2]
                if i0 < len(positions) and i1 < len(positions) and i2 < len(positions):
                    v0 = positions[i0]
                    v1 = positions[i1]
                    v2 = positions[i2]
                    all_triangles.append((v0, v1, v2))
                    vertex_set.add(v0)
                    vertex_set.add(v1)
                    vertex_set.add(v2)

    all_vertices = list(vertex_set)
    return all_triangles, all_vertices


def _read_glb_accessor(
    accessors: list[dict[str, Any]],
    buffer_views: list[dict[str, Any]],
    bin_data: bytes,
    accessor_idx: int,
) -> list[tuple[float, ...]]:
    """Read a VEC3 float accessor (POSITION data) from GLB binary chunk."""
    if accessor_idx >= len(accessors):
        return []
    acc = accessors[accessor_idx]
    bv_idx = acc.get("bufferView", 0)
    if bv_idx >= len(buffer_views):
        return []
    bv = buffer_views[bv_idx]

    byte_offset = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    count = acc.get("count", 0)
    comp_type = acc.get("componentType", 5126)
    acc_type = acc.get("type", "VEC3")
    stride = bv.get("byteStride", 0)

    if acc_type != "VEC3" or comp_type != 5126:
        return []

    element_size = 12  # 3 * 4 bytes (float)
    if stride == 0:
        stride = element_size

    result: list[tuple[float, ...]] = []
    for i in range(count):
        off = byte_offset + i * stride
        if off + element_size > len(bin_data):
            break
        x, y, z = struct.unpack_from("<3f", bin_data, off)
        result.append((x, y, z))

    return result


def _read_glb_accessor_scalar(
    accessors: list[dict[str, Any]],
    buffer_views: list[dict[str, Any]],
    bin_data: bytes,
    accessor_idx: int,
) -> list[int]:
    """Read a SCALAR accessor (index data) from GLB binary chunk."""
    if accessor_idx >= len(accessors):
        return []
    acc = accessors[accessor_idx]
    bv_idx = acc.get("bufferView", 0)
    if bv_idx >= len(buffer_views):
        return []
    bv = buffer_views[bv_idx]

    byte_offset = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    count = acc.get("count", 0)
    comp_type = acc.get("componentType", 5123)

    fmt_info = _COMPONENT_FMT.get(comp_type)
    if not fmt_info:
        return []
    fmt_char, fmt_size = fmt_info
    stride = bv.get("byteStride", 0) or fmt_size

    result: list[int] = []
    for i in range(count):
        off = byte_offset + i * stride
        if off + fmt_size > len(bin_data):
            break
        val = struct.unpack_from(f"<{fmt_char}", bin_data, off)[0]
        result.append(int(val))

    return result


def _convert_glb_to_stl(
    path: Path,
    output_path: str | None = None,
) -> str:
    """Convert a GLB file to binary STL."""
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


def rescale_stl(
    file_path: str,
    *,
    target_height_mm: float | None = None,
    scale_factor: float | None = None,
    max_dimension_mm: float | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Rescale an STL file to meet dimensional targets.

    Exactly one of ``target_height_mm``, ``scale_factor``, or
    ``max_dimension_mm`` must be provided.

    Args:
        file_path: Path to the input STL file.
        target_height_mm: Desired Z-axis height in mm.
        scale_factor: Uniform scale multiplier (e.g., 2.0 = double).
        max_dimension_mm: Scale down so the largest axis fits this limit.
        output_path: Output file path.  Defaults to overwriting input.

    Returns:
        Dict with ``path``, ``scale_applied``, ``original_dimensions``,
        and ``new_dimensions``.
    """
    opts = sum(x is not None for x in (target_height_mm, scale_factor, max_dimension_mm))
    if opts != 1:
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

    # Compute scale
    if target_height_mm is not None:
        current_h = bbox["z_max"] - bbox["z_min"]
        if current_h < 0.001:
            raise ValueError("Model has near-zero height, cannot scale to target.")
        sf = target_height_mm / current_h
    elif max_dimension_mm is not None:
        largest = max(
            bbox["x_max"] - bbox["x_min"],
            bbox["y_max"] - bbox["y_min"],
            bbox["z_max"] - bbox["z_min"],
        )
        if largest < 0.001:
            raise ValueError("Model has near-zero dimensions, cannot scale.")
        sf = max_dimension_mm / largest if largest > max_dimension_mm else 1.0
    else:
        sf = scale_factor  # type: ignore[assignment]

    # Apply uniform scale to all vertices
    scaled_triangles: list[tuple[tuple[float, ...], ...]] = []
    for tri in triangles:
        scaled_tri = tuple((v[0] * sf, v[1] * sf, v[2] * sf) for v in tri)
        scaled_triangles.append(scaled_tri)

    out = output_path or file_path
    _write_binary_stl(scaled_triangles, out)

    new_dims = {
        "width_mm": round(orig_dims["width_mm"] * sf, 2),
        "depth_mm": round(orig_dims["depth_mm"] * sf, 2),
        "height_mm": round(orig_dims["height_mm"] * sf, 2),
    }

    return {
        "path": out,
        "scale_applied": round(sf, 4),
        "original_dimensions": orig_dims,
        "new_dimensions": new_dims,
    }
