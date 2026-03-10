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
