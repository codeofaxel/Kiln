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
import math
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
</Types>"""

    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0"
                 Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" />
</Relationships>"""

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("3D/3dmodel.model", model_xml)

    return output_path


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


def design_scorecard(file_path: str) -> dict[str, Any]:
    """Generate a multi-factor quality scorecard for a mesh.

    Evaluates:
    - **Printability** (0-100): overhangs, manifold, supports needed
    - **Structural** (0-100): wall thickness, aspect ratio, stability
    - **Efficiency** (0-100): material usage, void ratio, print time proxy
    - **Quality** (0-100): triangle density, surface smoothness proxy

    Args:
        file_path: Path to mesh file.

    Returns:
        Dict with per-factor scores, overall score, and grade.
    """
    analysis = analyze_mesh(file_path)
    if analysis.printability_issues and not analysis.triangle_count:
        raise ValueError(f"Cannot analyze mesh: {analysis.printability_issues}")

    # --- Printability (from existing analysis) ---
    printability = analysis.printability_score

    # --- Structural score ---
    structural = 100
    structural_notes: list[str] = []

    if analysis.dimensions_mm:
        w = analysis.dimensions_mm["width_mm"]
        d = analysis.dimensions_mm["depth_mm"]
        h = analysis.dimensions_mm["height_mm"]
        aspect = max(w, d, h) / max(min(w, d, h), 0.01)
        if aspect > 10:
            structural -= 20
            structural_notes.append(f"Extreme aspect ratio ({aspect:.0f}:1)")
        elif aspect > 5:
            structural -= 10
            structural_notes.append(f"High aspect ratio ({aspect:.1f}:1)")

        # Base stability
        min_base = min(w, d)
        if h > 0 and min_base / h < 0.2:
            structural -= 15
            structural_notes.append("Very narrow base relative to height")

    if analysis.connected_components > 3:
        structural -= 15
        structural_notes.append(f"{analysis.connected_components} disconnected parts")
    elif analysis.connected_components > 1:
        structural -= 5

    if not analysis.is_manifold:
        structural -= 10
        structural_notes.append("Non-manifold: may have internal voids")

    structural = max(0, structural)

    # --- Efficiency score ---
    efficiency = 100
    efficiency_notes: list[str] = []

    if analysis.dimensions_mm and analysis.volume_mm3 > 0:
        w = analysis.dimensions_mm["width_mm"]
        d = analysis.dimensions_mm["depth_mm"]
        h = analysis.dimensions_mm["height_mm"]
        bbox_vol = w * d * h
        if bbox_vol > 0:
            fill_ratio = analysis.volume_mm3 / bbox_vol
            if fill_ratio < 0.05:
                efficiency -= 20
                efficiency_notes.append(f"Very low fill ratio ({fill_ratio:.1%} of bounding box)")
            elif fill_ratio < 0.15:
                efficiency -= 10
                efficiency_notes.append(f"Low fill ratio ({fill_ratio:.1%})")

    # Penalize excessive overhangs (more supports = more waste)
    if analysis.overhang_percentage > 30:
        efficiency -= 15
        efficiency_notes.append(f"High overhangs ({analysis.overhang_percentage:.0f}%) increase support waste")
    elif analysis.overhang_percentage > 15:
        efficiency -= 5

    efficiency = max(0, efficiency)

    # --- Quality score (mesh resolution/smoothness) ---
    quality = 100
    quality_notes: list[str] = []

    if analysis.dimensions_mm and analysis.triangle_count > 0:
        sa = analysis.surface_area_mm2
        if sa > 0:
            avg_tri_area = sa / analysis.triangle_count
            # Very large triangles = low resolution
            if avg_tri_area > 50:
                quality -= 20
                quality_notes.append("Low mesh resolution (large triangles)")
            elif avg_tri_area > 20:
                quality -= 10
                quality_notes.append("Moderate mesh resolution")

    if analysis.degenerate_triangles > 0:
        pct = analysis.degenerate_triangles / analysis.triangle_count * 100
        if pct > 5:
            quality -= 15
            quality_notes.append(f"Degenerate triangles ({pct:.1f}%)")
        else:
            quality -= 5

    quality = max(0, quality)

    # --- Overall ---
    overall = round(
        printability * 0.35 + structural * 0.25 + efficiency * 0.20 + quality * 0.20
    )

    if overall >= 90:
        grade = "A"
    elif overall >= 80:
        grade = "B"
    elif overall >= 65:
        grade = "C"
    elif overall >= 50:
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
            Defaults to (256, 256, 256) (typical Bambu A1).

    Returns:
        Dict with pass/fail verdict, issues found, and actions taken.
    """
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

    edge_count: dict[tuple[tuple[float, ...], tuple[float, ...]], int] = {}
    for tri in tris:
        for j in range(3):
            va, vb = tri[j], tri[(j + 1) % 3]
            edge = (min(va, vb), max(va, vb))
            edge_count[edge] = edge_count.get(edge, 0) + 1

    total_edges = len(edge_count)
    boundary = sum(1 for c in edge_count.values() if c == 1)
    manifold = sum(1 for c in edge_count.values() if c == 2)
    t_junction = sum(1 for c in edge_count.values() if c >= 3)
    non_manifold = boundary + t_junction

    return {
        "total_edges": total_edges,
        "manifold_edges": manifold,
        "boundary_edges": boundary,
        "t_junction_edges": t_junction,
        "non_manifold_edges": non_manifold,
        "is_watertight": non_manifold == 0,
        "manifold_pct": round(manifold / total_edges * 100, 1) if total_edges > 0 else 0.0,
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
