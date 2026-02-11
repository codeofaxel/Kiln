"""Mesh validation pipeline for generated 3D models.

Validates STL and OBJ files for 3D-printing readiness: parseable
geometry, reasonable dimensions, manifold checks, and polygon counts.
Uses only the Python standard library (``struct`` for binary STL
parsing) — no external mesh libraries required.
"""

from __future__ import annotations

import logging
import os
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def convert_to_stl(input_path: str, output_path: str | None = None) -> str:
    """Convert an OBJ file to binary STL.

    Parses the OBJ geometry and writes a binary STL with the same
    triangles.  Quads and higher polygons are triangulated.  Only
    geometry is preserved — textures, normals, and materials are
    discarded (not needed for 3D printing).

    Args:
        input_path: Path to the input OBJ file.
        output_path: Path for the output STL file.  Defaults to
            replacing the ``.obj`` extension with ``.stl``.

    Returns:
        The path to the written STL file.

    Raises:
        ValueError: If the input is not an OBJ file or has no geometry.
    """
    path = Path(input_path)
    if path.suffix.lower() != ".obj":
        raise ValueError(f"convert_to_stl expects .obj input, got {path.suffix!r}")

    errors: List[str] = []
    triangles, vertices = _parse_obj(path, errors)
    if errors:
        raise ValueError(f"Failed to parse OBJ: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("OBJ file contains no geometry to convert.")

    if output_path is None:
        output_path = str(path.with_suffix(".stl"))

    _write_binary_stl(triangles, output_path)
    return output_path


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
    errors: List[str] = []
    warnings: List[str] = []

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
    if ext not in (".stl", ".obj"):
        return MeshValidationResult(
            valid=False,
            errors=[f"Unsupported file type: {ext!r}.  Expected .stl or .obj."],
        )

    # --- parse geometry ---
    try:
        if ext == ".stl":
            triangles, vertices = _parse_stl(path, errors)
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
            f"Triangle count ({tri_count:,}) exceeds maximum "
            f"({_MAX_TRIANGLES:,}).  Model is too complex for slicing."
        )

    if tri_count > _WARN_TRIANGLES and tri_count <= _MAX_TRIANGLES:
        warnings.append(
            f"High triangle count ({tri_count:,}).  "
            f"Slicing may be slow."
        )

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
    errors: List[str],
) -> Tuple[List[Tuple[Tuple[float, ...], ...]], List[Tuple[float, ...]]]:
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
    errors: List[str],
) -> Tuple[List[Tuple[Tuple[float, ...], ...]], List[Tuple[float, ...]]]:
    """Parse a binary STL file."""
    with open(path, "rb") as fh:
        fh.read(_STL_HEADER_SIZE)  # skip header
        count_bytes = fh.read(_STL_COUNT_SIZE)
        if len(count_bytes) < _STL_COUNT_SIZE:
            errors.append("Binary STL file is truncated (missing triangle count).")
            return [], []

        tri_count = struct.unpack("<I", count_bytes)[0]
        expected_size = (
            _STL_HEADER_SIZE + _STL_COUNT_SIZE + _STL_TRIANGLE_SIZE * tri_count
        )
        actual_size = path.stat().st_size
        if actual_size < expected_size:
            errors.append(
                f"Binary STL truncated: header says {tri_count} triangles "
                f"({expected_size} bytes) but file is {actual_size} bytes."
            )
            return [], []

        triangles = []
        vertex_set: set[Tuple[float, ...]] = set()

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
    errors: List[str],
) -> Tuple[List[Tuple[Tuple[float, ...], ...]], List[Tuple[float, ...]]]:
    """Parse an ASCII STL file."""
    triangles = []
    vertex_set: set[Tuple[float, ...]] = set()
    current_verts: List[Tuple[float, ...]] = []

    try:
        with open(path, "r", errors="replace") as fh:
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
    errors: List[str],
) -> Tuple[List[Tuple[Tuple[float, ...], ...]], List[Tuple[float, ...]]]:
    """Parse a Wavefront OBJ file (vertices and faces only)."""
    vertices: List[Tuple[float, ...]] = []
    triangles: List[Tuple[Tuple[float, ...], ...]] = []

    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("v "):
                    parts = stripped.split()
                    if len(parts) >= 4:
                        try:
                            vertices.append(
                                (float(parts[1]), float(parts[2]), float(parts[3]))
                            )
                        except ValueError:
                            pass
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
                                triangles.append(
                                    (vertices[i0], vertices[i1], vertices[i2])
                                )
    except Exception as exc:
        errors.append(f"Could not read OBJ file: {exc}")

    return triangles, vertices


# ---------------------------------------------------------------------------
# Geometry analysis
# ---------------------------------------------------------------------------


def _bounding_box(vertices: List[Tuple[float, ...]]) -> Dict[str, float]:
    """Compute axis-aligned bounding box from vertex list."""
    if not vertices:
        return {
            "x_min": 0.0, "x_max": 0.0,
            "y_min": 0.0, "y_max": 0.0,
            "z_min": 0.0, "z_max": 0.0,
        }

    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]

    return {
        "x_min": min(xs), "x_max": max(xs),
        "y_min": min(ys), "y_max": max(ys),
        "z_min": min(zs), "z_max": max(zs),
    }


def _check_manifold(
    triangles: List[Tuple[Tuple[float, ...], ...]],
    warnings: List[str],
) -> bool:
    """Check if the mesh is manifold (watertight).

    A manifold mesh has every edge shared by exactly two triangles.
    Uses a dict to count edge occurrences in O(n) time.

    Returns:
        True if manifold, False otherwise (with a warning appended).
    """
    if not triangles:
        return False

    edge_count: Dict[Tuple[Tuple[float, ...], Tuple[float, ...]], int] = {}

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
