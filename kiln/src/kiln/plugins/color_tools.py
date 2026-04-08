"""Procedural color assignment plugin — geometry-based multicolor without cloud APIs.

Splits a 3D model into color zones based on geometry (Z-height bands,
face normal clustering, or random assignment) and produces separate STL
files for each zone.  Optionally composes them into a multicolor 3MF
for AMS/MMU printers.

Zero cloud dependencies.  No Meshy, no Gemini, no GPU required.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
import math
import os
import random
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PILImage = None  # type: ignore[assignment,misc]
    _PIL_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STL_HEADER_SIZE = 80
_STL_TRIANGLE_SIZE = 50  # 12 (normal) + 36 (3 vertices) + 2 (attr)

_DEFAULT_PALETTE = ["#FFFFFF", "#F72323", "#161616", "#898989"]

_PLA_DENSITY_G_PER_CM3 = 1.24
_DEFAULT_INFILL_FACTOR = 0.30

# Rough FDM print-time estimate: weight * this factor (minutes per gram at
# standard speed / 0.2 mm layers / 20 % infill).
_PRINT_TIME_MIN_PER_G = 20.0

# Human-readable zone labels for the "normal" method.
_NORMAL_ZONE_LABELS: dict[int, str] = {
    0: "top",
    1: "bottom",
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _Triangle:
    """A single STL triangle with normal, vertices, and attribute byte."""

    normal: tuple[float, float, float]
    v0: tuple[float, float, float]
    v1: tuple[float, float, float]
    v2: tuple[float, float, float]
    attr: int = 0

    @property
    def centroid_z(self) -> float:
        return (self.v0[2] + self.v1[2] + self.v2[2]) / 3.0

    @property
    def centroid(self) -> tuple[float, float, float]:
        return (
            (self.v0[0] + self.v1[0] + self.v2[0]) / 3.0,
            (self.v0[1] + self.v1[1] + self.v2[1]) / 3.0,
            (self.v0[2] + self.v1[2] + self.v2[2]) / 3.0,
        )

    @property
    def area(self) -> float:
        """Triangle area via cross product (mm^2)."""
        ux = self.v1[0] - self.v0[0]
        uy = self.v1[1] - self.v0[1]
        uz = self.v1[2] - self.v0[2]
        vx = self.v2[0] - self.v0[0]
        vy = self.v2[1] - self.v0[1]
        vz = self.v2[2] - self.v0[2]
        cx = uy * vz - uz * vy
        cy = uz * vx - ux * vz
        cz = ux * vy - uy * vx
        return 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)

    def raw_bytes(self) -> bytes:
        """Serialize to 50-byte binary STL triangle record."""
        return struct.pack(
            "<12fH",
            *self.normal,
            *self.v0,
            *self.v1,
            *self.v2,
            self.attr,
        )


@dataclass
class _ColorZone:
    """Accumulates triangles assigned to one color zone."""

    index: int
    color: str
    triangles: list[_Triangle] = field(default_factory=list)

    @property
    def face_count(self) -> int:
        return len(self.triangles)

    @property
    def total_area_mm2(self) -> float:
        return sum(t.area for t in self.triangles)


# ---------------------------------------------------------------------------
# STL parsing / writing (binary + ASCII, stdlib)
# ---------------------------------------------------------------------------


def _parse_ascii_stl(file_path: str) -> list[_Triangle]:
    """Parse an ASCII STL file into a list of triangles.

    Handles both ``solid name`` and bare ``solid`` headers.  Each facet block
    must contain exactly three ``vertex`` lines.

    :raises ValueError: If the file is malformed or no triangles are found.
    """
    triangles: list[_Triangle] = []
    normal: tuple[float, float, float] = (0.0, 0.0, 0.0)
    vertices: list[tuple[float, float, float]] = []

    with open(file_path) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("facet normal"):
                parts = line.split()
                # "facet normal nx ny nz"
                normal = (float(parts[2]), float(parts[3]), float(parts[4]))
                vertices = []
            elif line.startswith("vertex"):
                parts = line.split()
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif line.startswith("endfacet"):
                if len(vertices) == 3:
                    triangles.append(
                        _Triangle(
                            normal=normal,
                            v0=vertices[0],
                            v1=vertices[1],
                            v2=vertices[2],
                        )
                    )
                vertices = []

    return triangles


def _parse_binary_stl(file_path: str) -> list[_Triangle]:
    """Parse a binary or ASCII STL file into a list of triangles.

    Tries binary parsing first.  If the header starts with ``solid`` and the
    file size does not match the expected binary layout, falls back to
    :func:`_parse_ascii_stl` automatically.

    :raises ValueError: If the file is too small or truncated.
    """
    path = Path(file_path)
    size = path.stat().st_size

    if size < _STL_HEADER_SIZE + 4:
        raise ValueError(f"File too small for binary STL: {size} bytes")

    triangles: list[_Triangle] = []
    with open(path, "rb") as fh:
        header = fh.read(_STL_HEADER_SIZE)

        count_bytes = fh.read(4)
        tri_count = struct.unpack("<I", count_bytes)[0]

        expected = _STL_HEADER_SIZE + 4 + tri_count * _STL_TRIANGLE_SIZE
        if header[:5] == b"solid" and expected != size:
            # ASCII STL — delegate to the text parser
            _logger.debug("ASCII STL detected, switching to ASCII parser: %s", file_path)
            return _parse_ascii_stl(file_path)

        if size < expected:
            raise ValueError(
                f"Truncated STL: expected {expected} bytes, got {size}"
            )

        for _ in range(tri_count):
            data = fh.read(_STL_TRIANGLE_SIZE)
            if len(data) < _STL_TRIANGLE_SIZE:
                break
            floats = struct.unpack_from("<12f", data, 0)
            attr = struct.unpack_from("<H", data, 48)[0]
            triangles.append(
                _Triangle(
                    normal=(floats[0], floats[1], floats[2]),
                    v0=(floats[3], floats[4], floats[5]),
                    v1=(floats[6], floats[7], floats[8]),
                    v2=(floats[9], floats[10], floats[11]),
                    attr=attr,
                )
            )

    return triangles


def _write_binary_stl(triangles: list[_Triangle], output_path: str) -> None:
    """Write triangles to a binary STL file."""
    with open(output_path, "wb") as fh:
        # 80-byte header
        fh.write(b"Kiln color_tools" + b"\0" * (_STL_HEADER_SIZE - 16))
        # Triangle count
        fh.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            fh.write(tri.raw_bytes())


# ---------------------------------------------------------------------------
# Color assignment strategies
# ---------------------------------------------------------------------------


def _assign_z_height(
    triangles: list[_Triangle],
    num_colors: int,
) -> list[int]:
    """Assign each triangle to a color zone by Z-height band."""
    if not triangles:
        return []

    z_values = [t.centroid_z for t in triangles]
    z_min = min(z_values)
    z_max = max(z_values)
    z_range = z_max - z_min

    if z_range < 1e-9:
        return [0] * len(triangles)

    band_size = z_range / num_colors
    assignments: list[int] = []
    for z in z_values:
        band = int((z - z_min) / band_size)
        # Clamp the top-edge case
        if band >= num_colors:
            band = num_colors - 1
        assignments.append(band)
    return assignments


def _assign_normal(
    triangles: list[_Triangle],
    num_colors: int,
) -> list[int]:
    """Assign each triangle to a color zone by face normal direction.

    Strategy:
      - Zone 0: top-facing (normal Z > threshold)
      - Zone 1: bottom-facing (normal Z < -threshold)
      - Remaining zones: side faces divided by azimuth angle
    """
    if not triangles:
        return []

    threshold = 0.5  # ~60 deg from horizontal
    side_zones = max(1, num_colors - 2)
    assignments: list[int] = []

    for tri in triangles:
        nz = tri.normal[2]
        if nz > threshold:
            assignments.append(0)  # top
        elif nz < -threshold:
            assignments.append(min(1, num_colors - 1))  # bottom
        else:
            # Side face — divide by azimuth
            angle = math.atan2(tri.normal[1], tri.normal[0])  # -pi..pi
            normalized = (angle + math.pi) / (2 * math.pi)  # 0..1
            zone = int(normalized * side_zones)
            if zone >= side_zones:
                zone = side_zones - 1
            # Offset by 2 (top=0, bottom=1, sides=2..)
            zone_idx = zone + 2
            if zone_idx >= num_colors:
                zone_idx = num_colors - 1
            assignments.append(zone_idx)

    return assignments


def _assign_random(
    triangles: list[_Triangle],
    num_colors: int,
    *,
    seed: int = 42,
) -> list[int]:
    """Random face assignment — artistic/abstract prints."""
    rng = random.Random(seed)
    return [rng.randint(0, num_colors - 1) for _ in triangles]


# ---------------------------------------------------------------------------
# Weight estimation
# ---------------------------------------------------------------------------


def _estimate_weight_g(triangles: list[_Triangle]) -> float:
    """Estimate filament weight (grams) for a set of triangles.

    Uses surface-area-based estimate: each triangle contributes
    ``area * layer_height`` of material volume.  This works for open
    shells (color zones are rarely watertight) and is proportional to
    the actual printed material.

    :param triangles: Triangles in the zone.
    :returns: Estimated weight in grams (PLA, 0.2 mm layer height).
    """
    _LAYER_HEIGHT_MM = 0.2
    total_area_mm2 = sum(t.area for t in triangles)
    volume_mm3 = total_area_mm2 * _LAYER_HEIGHT_MM
    volume_cm3 = volume_mm3 / 1000.0
    return round(volume_cm3 * _PLA_DENSITY_G_PER_CM3, 2)


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------


_MIN_BAND_HEIGHT_MM = 3.0  # < 15 layers at 0.2 mm — warn below this threshold
_LAYER_HEIGHT_REFERENCE_MM = 0.2


def _band_height_warning(z_range: float, num_colors: int) -> str | None:
    """Return a warning string if any Z-height band is too thin for FDM.

    At 0.2 mm layer height, 15 layers = 3 mm.  Bands thinner than 3 mm
    produce only a few colour layers and may not show a visible colour
    change on the finished print.

    :param z_range: Total Z extent of the model in mm.
    :param num_colors: Number of requested colour bands.
    :returns: Warning string, or ``None`` if bands are tall enough.
    """
    if num_colors < 2:
        return None
    band_height = z_range / num_colors
    if band_height < _MIN_BAND_HEIGHT_MM:
        layers = round(band_height / _LAYER_HEIGHT_REFERENCE_MM)
        return (
            f"Band height is {band_height:.1f} mm (~{layers} layers at "
            f"{_LAYER_HEIGHT_REFERENCE_MM} mm).  Bands thinner than "
            f"{_MIN_BAND_HEIGHT_MM} mm may not show a visible colour "
            "change on the finished print.  Consider using fewer colours "
            "or a taller model."
        )
    return None


def _split_and_write(
    triangles: list[_Triangle],
    assignments: list[int],
    num_colors: int,
    palette: list[str],
    output_dir: str,
    base_name: str,
) -> list[_ColorZone]:
    """Bucket triangles by assignment, write per-zone STLs."""
    zones: list[_ColorZone] = []
    for i in range(num_colors):
        color = palette[i] if i < len(palette) else palette[i % len(palette)]
        zones.append(_ColorZone(index=i, color=color))

    for tri, zone_idx in zip(triangles, assignments, strict=True):
        zones[zone_idx].triangles.append(tri)

    for zone in zones:
        stl_path = os.path.join(output_dir, f"{base_name}_zone{zone.index}.stl")
        _write_binary_stl(zone.triangles, stl_path)

    return zones


def _try_compose_3mf(
    zones: list[_ColorZone],
    output_dir: str,
    base_name: str,
) -> tuple[str | None, str | None]:
    """Attempt to compose a multicolor 3MF.

    :returns: ``(path, error)`` — path on success, error message on failure.
    """
    try:
        from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf
    except ImportError:
        _logger.debug("multicolor_3mf not available — skipping 3MF composition")
        return None, None

    parts: list[ColorPart] = []
    for zone in zones:
        if zone.face_count == 0:
            continue
        stl_path = os.path.join(output_dir, f"{base_name}_zone{zone.index}.stl")
        parts.append(
            ColorPart(
                stl_path=stl_path,
                extruder=zone.index + 1,
                name=f"zone_{zone.index}",
                color=zone.color,
            )
        )

    if not parts:
        return None, None

    out_3mf = os.path.join(output_dir, f"{base_name}_multicolor.3mf")
    try:
        result = compose_multicolor_3mf(parts, output_path=out_3mf)
        return result.get("output_path", out_3mf), None
    except (ImportError, OSError, ValueError, TypeError) as exc:
        _logger.exception("Failed to compose multicolor 3MF")
        return None, str(exc)


def _hex_to_color_name(hex_color: str) -> str:
    """Return a human-readable color name for common hex values.

    Falls back to the raw hex string for unrecognized colors.
    """
    _COLOR_MAP: dict[str, str] = {
        "#ffffff": "white",
        "#f72323": "red",
        "#161616": "black",
        "#898989": "grey",
        "#000000": "black",
        "#ff0000": "red",
        "#00ff00": "green",
        "#0000ff": "blue",
        "#ffff00": "yellow",
        "#ff8800": "orange",
        "#ff00ff": "magenta",
        "#00ffff": "cyan",
        "#808080": "gray",
        "#c0c0c0": "silver",
        "#ffa500": "orange",
        "#800000": "maroon",
        "#008000": "dark green",
        "#000080": "navy",
        "#800080": "purple",
        "#ffc0cb": "pink",
        "#a52a2a": "brown",
    }
    return _COLOR_MAP.get(hex_color.lower(), hex_color)


def _zone_label(zone_index: int, method: str) -> str:
    """Return a human-readable label for a zone based on method and index."""
    if method == "normal":
        return _NORMAL_ZONE_LABELS.get(zone_index, f"side{zone_index - 1}")
    return f"zone{zone_index}"


def _build_summary(
    active_zones: list[_ColorZone],
    zone_weights: list[float],
    total_weight: float,
    method: str,
) -> str:
    """Build a one-line summary of the color assignment result.

    Example: "4 color zones: top (white, 30%), sides (red, 40%), ..."
    """
    if not active_zones or total_weight < 1e-9:
        return f"{len(active_zones)} color zones"

    parts: list[str] = []
    for zone, weight in zip(active_zones, zone_weights, strict=True):
        label = _zone_label(zone.index, method)
        color_name = _hex_to_color_name(zone.color)
        pct = round(weight / total_weight * 100)
        parts.append(f"{label} ({color_name}, {pct}%)")

    count = len(active_zones)
    return f"{count} color zone{'s' if count != 1 else ''}: {', '.join(parts)}"


def _build_result(
    zones: list[_ColorZone],
    output_dir: str,
    base_name: str,
    method: str,
    total_faces: int,
    threemf_path: str | None,
    *,
    compose_3mf_error: str | None = None,
    band_warning: str | None = None,
) -> dict[str, Any]:
    """Build the standard return dict."""
    # Only include zones that have actual geometry — skip empties.
    active_zones = [z for z in zones if z.face_count > 0]

    zone_weights = [_estimate_weight_g(z.triangles) for z in active_zones]
    total_weight_g = round(sum(zone_weights), 2)
    print_time_estimate_min = round(total_weight_g * _PRINT_TIME_MIN_PER_G, 1)

    zone_details: list[dict[str, Any]] = []
    for slot_num, (zone, weight) in enumerate(
        zip(active_zones, zone_weights, strict=True), start=1
    ):
        stl_path = os.path.join(output_dir, f"{base_name}_zone{zone.index}.stl")
        zone_details.append({
            "zone": zone.index,
            "color": zone.color,
            "face_count": zone.face_count,
            "stl_path": stl_path,
            "ams_slot": slot_num,
            "estimated_weight_g": weight,
        })

    result: dict[str, Any] = {
        "success": True,
        "method": method,
        "total_faces": total_faces,
        "num_colors": len(active_zones),
        "total_weight_g": total_weight_g,
        "print_time_estimate_min": print_time_estimate_min,
        "summary": _build_summary(active_zones, zone_weights, total_weight_g, method),
        "zones": zone_details,
        "ams_mapping": {
            f"slot_{i + 1}": z.color
            for i, z in enumerate(active_zones)
        },
    }

    if threemf_path:
        result["multicolor_3mf"] = threemf_path
        result["next_step"] = (
            f"Multicolor 3MF is ready at {threemf_path}.  "
            "Call slice_model(input_path=<multicolor_3mf>) to slice it, "
            "then upload_file() + start_print() to send to your AMS printer."
        )
        result["next_action"] = {
            "tool": "slice_model",
            "args": {"input_path": threemf_path},
        }
    else:
        result["next_step"] = (
            "Call compose_multicolor_3mf with the zone STL paths to build a "
            "multicolor 3MF, then slice_model() to slice, then "
            "upload_file() + start_print() to print."
        )
        result["next_action"] = {
            "hint": "Use compose_multicolor_3mf to combine zone STLs, "
            "or slice each zone separately.",
        }

    if band_warning:
        result["warning"] = band_warning

    if compose_3mf_error:
        result["compose_3mf_error"] = compose_3mf_error

    return result


# ---------------------------------------------------------------------------
# OBJ / MTL parsing + texture sampling
# ---------------------------------------------------------------------------


@dataclass
class _ObjFace:
    """A parsed OBJ face with vertex indices, UV indices, and material name."""

    vertex_indices: list[int]
    uv_indices: list[int]
    material: str


def _parse_obj(obj_path: str) -> tuple[
    list[tuple[float, float, float]],
    list[tuple[float, float]],
    list[_ObjFace],
]:
    """Parse an OBJ file into vertices, UV coords, and faces.

    :param obj_path: Path to the .obj file.
    :returns: ``(vertices, uvs, faces)`` where vertices are 3-tuples,
        uvs are 2-tuples, and faces carry vertex/UV indices (0-based)
        plus the active material name.
    :raises ValueError: If the file cannot be read or is empty.
    """
    vertices: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    faces: list[_ObjFace] = []
    current_material = ""

    try:
        with open(obj_path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                parts = line.split()
                tag = parts[0]

                if tag == "v" and len(parts) >= 4:
                    vertices.append(
                        (float(parts[1]), float(parts[2]), float(parts[3]))
                    )
                elif tag == "vt" and len(parts) >= 3:
                    uvs.append((float(parts[1]), float(parts[2])))
                elif tag == "usemtl":
                    current_material = parts[1] if len(parts) > 1 else ""
                elif tag == "f":
                    v_idx: list[int] = []
                    uv_idx: list[int] = []
                    for token in parts[1:]:
                        # Formats: v, v/vt, v/vt/vn, v//vn
                        components = token.split("/")
                        v_idx.append(int(components[0]) - 1)  # OBJ is 1-based
                        if len(components) >= 2 and components[1]:
                            uv_idx.append(int(components[1]) - 1)
                    faces.append(
                        _ObjFace(
                            vertex_indices=v_idx,
                            uv_indices=uv_idx,
                            material=current_material,
                        )
                    )
    except OSError as exc:
        raise ValueError(f"Cannot read OBJ file: {exc}") from exc

    return vertices, uvs, faces


def _parse_mtl(mtl_path: str) -> dict[str, str]:
    """Parse an MTL file to extract texture image paths per material.

    Looks for ``map_Kd`` (diffuse texture map) directives.

    :param mtl_path: Path to the .mtl file.
    :returns: ``{material_name: texture_path}`` — paths are relative
        to the MTL file's directory.
    """
    textures: dict[str, str] = {}
    current_name = ""

    try:
        with open(mtl_path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("newmtl "):
                    current_name = line.split(None, 1)[1]
                elif line.startswith("map_Kd ") and current_name:
                    tex_file = line.split(None, 1)[1]
                    textures[current_name] = tex_file
    except OSError:
        _logger.debug("Cannot read MTL file: %s", mtl_path)

    return textures


def _find_mtl_path(obj_path: str) -> str | None:
    """Discover the MTL file referenced by an OBJ.

    Checks the ``mtllib`` directive inside the OBJ first, then falls
    back to ``<basename>.mtl`` in the same directory.
    """
    obj_dir = os.path.dirname(obj_path)

    try:
        with open(obj_path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("mtllib "):
                    mtl_name = line.split(None, 1)[1]
                    candidate = os.path.join(obj_dir, mtl_name)
                    if os.path.isfile(candidate):
                        return candidate
    except OSError:
        pass

    # Fallback: same stem with .mtl extension
    stem = Path(obj_path).stem
    fallback = os.path.join(obj_dir, f"{stem}.mtl")
    if os.path.isfile(fallback):
        return fallback

    return None


def _sample_face_color(
    face: _ObjFace,
    uvs: list[tuple[float, float]],
    img: Any,
    img_width: int,
    img_height: int,
) -> tuple[int, int, int]:
    """Sample the texture at the centroid UV of a face.

    :returns: ``(r, g, b)`` tuple in 0-255 range.
    """
    if not face.uv_indices or not uvs:
        return (128, 128, 128)  # neutral grey fallback

    # Average UV coordinates of the face
    u_sum = 0.0
    v_sum = 0.0
    count = 0
    for idx in face.uv_indices:
        if 0 <= idx < len(uvs):
            u_sum += uvs[idx][0]
            v_sum += uvs[idx][1]
            count += 1

    if count == 0:
        return (128, 128, 128)

    u = u_sum / count
    v = v_sum / count

    # Wrap to [0, 1] via modulo (standard UV tiling behavior)
    u = u % 1.0
    v = v % 1.0

    # OBJ UV: (0,0) is bottom-left; image pixels: (0,0) is top-left
    px = int(u * (img_width - 1))
    py = int((1.0 - v) * (img_height - 1))
    px = max(0, min(img_width - 1, px))
    py = max(0, min(img_height - 1, py))

    pixel = img.getpixel((px, py))
    if isinstance(pixel, (tuple, list)):
        return (pixel[0], pixel[1], pixel[2])
    # Greyscale
    return (pixel, pixel, pixel)


def _quantize_colors(
    face_colors: list[tuple[int, int, int]],
    num_colors: int,
) -> tuple[list[int], list[tuple[int, int, int]]]:
    """Assign each face to one of ``num_colors`` dominant color clusters.

    Uses a simple iterative k-means on RGB values.  No external
    dependencies beyond stdlib + the face color list.

    :returns: ``(assignments, centroids)`` where assignments[i] is the
        cluster index for face i, and centroids are the final RGB centers.
    """
    if not face_colors:
        return [], []

    num_colors = min(num_colors, len(face_colors))

    # Seed centroids: pick evenly spaced samples from the face list
    step = max(1, len(face_colors) // num_colors)
    centroids = [face_colors[i * step] for i in range(num_colors)]

    # Deduplicate centroids if step caused collisions
    seen: set[tuple[int, int, int]] = set()
    unique_centroids: list[tuple[int, int, int]] = []
    for c in centroids:
        if c not in seen:
            seen.add(c)
            unique_centroids.append(c)
    # If fewer unique colors than requested, clamp — don't pad with
    # synthetic grey values that would steal faces from real clusters.
    num_colors = len(unique_centroids)
    centroids = unique_centroids

    assignments = [0] * len(face_colors)

    for _iteration in range(20):
        # Assign each face to the nearest centroid
        changed = False
        for i, color in enumerate(face_colors):
            best_dist = float("inf")
            best_idx = 0
            for j, cent in enumerate(centroids):
                dr = color[0] - cent[0]
                dg = color[1] - cent[1]
                db = color[2] - cent[2]
                dist = dr * dr + dg * dg + db * db
                if dist < best_dist:
                    best_dist = dist
                    best_idx = j
            if assignments[i] != best_idx:
                changed = True
                assignments[i] = best_idx

        if not changed:
            break

        # Update centroids
        sums = [[0, 0, 0] for _ in range(num_colors)]
        counts = [0] * num_colors
        for i, color in enumerate(face_colors):
            ci = assignments[i]
            sums[ci][0] += color[0]
            sums[ci][1] += color[1]
            sums[ci][2] += color[2]
            counts[ci] += 1

        for j in range(num_colors):
            if counts[j] > 0:
                centroids[j] = (
                    sums[j][0] // counts[j],
                    sums[j][1] // counts[j],
                    sums[j][2] // counts[j],
                )

    return assignments, centroids


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    """Convert an (R, G, B) tuple to a ``#RRGGBB`` hex string."""
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def _obj_face_to_triangle(
    face: _ObjFace,
    vertices: list[tuple[float, float, float]],
) -> list[_Triangle]:
    """Convert an OBJ face (possibly a quad+) into triangles via fan triangulation.

    :returns: List of triangles. Empty if face has fewer than 3 vertices.
    """
    idx = face.vertex_indices
    if len(idx) < 3:
        return []

    v = [vertices[i] for i in idx if 0 <= i < len(vertices)]
    if len(v) < 3:
        return []

    tris: list[_Triangle] = []
    for i in range(1, len(v) - 1):
        # Compute face normal via cross product
        ux = v[i][0] - v[0][0]
        uy = v[i][1] - v[0][1]
        uz = v[i][2] - v[0][2]
        vx = v[i + 1][0] - v[0][0]
        vy = v[i + 1][1] - v[0][1]
        vz = v[i + 1][2] - v[0][2]
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length > 1e-12:
            nx /= length
            ny /= length
            nz /= length

        tris.append(
            _Triangle(
                normal=(nx, ny, nz),
                v0=v[0],
                v1=v[i],
                v2=v[i + 1],
            )
        )
    return tris


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class _ColorToolsPlugin:
    """Procedural color assignment — geometry-based multicolor for AMS/MMU.

    Tools:
        - auto_color_by_height
        - auto_color_by_region
        - auto_multicolor_from_texture
    """

    @property
    def name(self) -> str:
        return "color_tools"

    @property
    def description(self) -> str:
        return "Procedural color assignment — geometry-based multicolor without cloud APIs"

    def register(self, mcp: Any) -> None:
        """Register color assignment tools with the MCP server."""

        @mcp.tool()
        def auto_color_by_height(
            input_path: str,
            num_colors: int = 4,
            color_palette: list[str] | None = None,
        ) -> dict:
            """Split a 3D model into horizontal color zones by Z-height.

            Divides the model's Z-range into N equal bands and assigns
            each face to the band containing its centroid.  Produces
            separate STL files per zone and (if available) a multicolor
            3MF ready for AMS/MMU printers.

            Zero cloud dependencies — pure geometry.

            :param input_path: Path to a binary STL file.
            :param num_colors: Number of color zones (default 4).
            :param color_palette: List of hex colors (e.g.
                ``["#FF0000", "#00FF00"]``).  Defaults to white/red/black/grey.
            :returns: Dict with zone STL paths, hex colors, face counts,
                AMS slot mapping, weight estimates, and optional 3MF path.
            """
            path = Path(input_path)
            if not path.exists():
                return {"success": False, "error": f"File not found: {input_path}"}

            if num_colors < 1:
                return {"success": False, "error": "num_colors must be >= 1"}

            palette = color_palette or _DEFAULT_PALETTE
            if len(palette) < num_colors:
                # Cycle palette to fill
                palette = [palette[i % len(palette)] for i in range(num_colors)]

            try:
                triangles = _parse_binary_stl(input_path)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}

            if not triangles:
                return {"success": False, "error": "No triangles found in STL"}

            output_dir = tempfile.mkdtemp(prefix="kiln_color_")
            base_name = path.stem

            assignments = _assign_z_height(triangles, num_colors)
            zones = _split_and_write(
                triangles, assignments, num_colors, palette, output_dir, base_name,
            )
            threemf_path, compose_err = _try_compose_3mf(zones, output_dir, base_name)

            z_values = [t.centroid_z for t in triangles]
            z_range = max(z_values) - min(z_values)
            warn = _band_height_warning(z_range, num_colors)

            return _build_result(
                zones, output_dir, base_name, "z_height",
                len(triangles), threemf_path,
                compose_3mf_error=compose_err,
                band_warning=warn,
            )

        @mcp.tool()
        def auto_color_by_region(
            input_path: str,
            num_colors: int = 4,
            method: str = "z_height",
            color_palette: list[str] | None = None,
        ) -> dict:
            """Split a 3D model into color zones by geometric region.

            Supports multiple assignment methods:
              - ``"z_height"``: horizontal bands by Z-height (default)
              - ``"normal"``: group by face normal direction
                (top / bottom / sides)
              - ``"random"``: random face assignment for artistic prints

            Zero cloud dependencies — pure geometry.

            :param input_path: Path to a binary STL file.
            :param num_colors: Number of color zones (default 4).
            :param method: Assignment method — ``"z_height"``,
                ``"normal"``, or ``"random"``.
            :param color_palette: List of hex colors.  Defaults to
                white/red/black/grey.
            :returns: Dict with zone STL paths, hex colors, face counts,
                AMS slot mapping, weight estimates, and optional 3MF path.
            """
            valid_methods = {"z_height", "normal", "random"}
            if method not in valid_methods:
                return {
                    "success": False,
                    "error": f"Unknown method '{method}'. Choose from: {sorted(valid_methods)}",
                }

            path = Path(input_path)
            if not path.exists():
                return {"success": False, "error": f"File not found: {input_path}"}

            if num_colors < 1:
                return {"success": False, "error": "num_colors must be >= 1"}

            palette = color_palette or _DEFAULT_PALETTE
            if len(palette) < num_colors:
                palette = [palette[i % len(palette)] for i in range(num_colors)]

            try:
                triangles = _parse_binary_stl(input_path)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}

            if not triangles:
                return {"success": False, "error": "No triangles found in STL"}

            output_dir = tempfile.mkdtemp(prefix="kiln_color_")
            base_name = path.stem

            if method == "z_height":
                assignments = _assign_z_height(triangles, num_colors)
            elif method == "normal":
                assignments = _assign_normal(triangles, num_colors)
            else:
                assignments = _assign_random(triangles, num_colors)

            zones = _split_and_write(
                triangles, assignments, num_colors, palette, output_dir, base_name,
            )
            threemf_path, compose_err = _try_compose_3mf(zones, output_dir, base_name)

            warn: str | None = None
            if method == "z_height":
                z_values = [t.centroid_z for t in triangles]
                z_range = max(z_values) - min(z_values)
                warn = _band_height_warning(z_range, num_colors)

            return _build_result(
                zones, output_dir, base_name, method,
                len(triangles), threemf_path,
                compose_3mf_error=compose_err,
                band_warning=warn,
            )

        @mcp.tool()
        def auto_multicolor_from_texture(
            obj_path: str,
            num_colors: int = 4,
        ) -> dict:
            """Convert a textured OBJ into per-color STL zones for multicolor printing.

            Takes an OBJ file with MTL + PNG textures (e.g. from Meshy
            retexture/refine) and splits it into separate STL files per
            dominant color, then composes into a multicolor 3MF ready for
            AMS/MMU printers.

            Requires Pillow (``pip install Pillow``) for texture sampling.

            :param obj_path: Path to the textured ``.obj`` file.  The MTL
                and texture PNG(s) must be in the same directory.
            :param num_colors: Number of color zones to extract (default 4).
            :returns: Dict with zone STL paths, hex colors, face counts,
                AMS slot mapping, weight estimates, and optional 3MF path.
            """
            if not _PIL_AVAILABLE:
                return {
                    "success": False,
                    "error": (
                        "Pillow is required for texture-based multicolor. "
                        "Install with: pip install Pillow"
                    ),
                }

            path = Path(obj_path)
            if not path.exists():
                return {"success": False, "error": f"File not found: {obj_path}"}

            if num_colors < 1:
                return {"success": False, "error": "num_colors must be >= 1"}

            # Parse OBJ
            try:
                vertices, uvs, faces = _parse_obj(obj_path)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}

            if not faces:
                return {"success": False, "error": "No faces found in OBJ file"}

            if not vertices:
                return {"success": False, "error": "No vertices found in OBJ file"}

            # Find and parse MTL for texture paths
            mtl_path = _find_mtl_path(obj_path)
            tex_map: dict[str, str] = {}
            if mtl_path:
                tex_map = _parse_mtl(mtl_path)

            # Load texture images per material
            obj_dir = os.path.dirname(os.path.abspath(obj_path))
            loaded_textures: dict[str, Any] = {}
            for mat_name, tex_file in tex_map.items():
                tex_path = os.path.join(obj_dir, tex_file)
                if os.path.isfile(tex_path):
                    try:
                        loaded_textures[mat_name] = _PILImage.open(tex_path).convert("RGB")
                    except (OSError, ValueError):
                        _logger.debug("Failed to open texture: %s", tex_path)

            # If no textures loaded, try to find any PNG in the directory
            if not loaded_textures:
                for fname in os.listdir(obj_dir):
                    if fname.lower().endswith((".png", ".jpg", ".jpeg")):
                        try:
                            img = _PILImage.open(os.path.join(obj_dir, fname)).convert("RGB")
                            loaded_textures["_default"] = img
                            break
                        except (OSError, ValueError):
                            continue

            if not loaded_textures:
                return {
                    "success": False,
                    "error": (
                        "No texture images found. Ensure MTL + PNG files "
                        "are in the same directory as the OBJ."
                    ),
                }

            # Sample face colors from texture
            face_colors: list[tuple[int, int, int]] = []
            for face in faces:
                # Pick the texture for this face's material
                img = loaded_textures.get(face.material)
                if img is None:
                    # Fall back to first available texture
                    img = next(iter(loaded_textures.values()))
                w, h = img.size
                color = _sample_face_color(face, uvs, img, w, h)
                face_colors.append(color)

            # Quantize to dominant colors
            assignments, centroids = _quantize_colors(face_colors, num_colors)
            palette = [_rgb_to_hex(c) for c in centroids]

            # Convert OBJ faces to triangles and bucket by color zone
            output_dir = tempfile.mkdtemp(prefix="kiln_texture_color_")
            base_name = path.stem

            zones: list[_ColorZone] = []
            for i in range(num_colors):
                color = palette[i] if i < len(palette) else palette[i % len(palette)]
                zones.append(_ColorZone(index=i, color=color))

            total_faces = 0
            for face, zone_idx in zip(faces, assignments, strict=True):
                tris = _obj_face_to_triangle(face, vertices)
                zones[zone_idx].triangles.extend(tris)
                total_faces += len(tris)

            # Write per-zone STLs
            for zone in zones:
                stl_path = os.path.join(output_dir, f"{base_name}_zone{zone.index}.stl")
                _write_binary_stl(zone.triangles, stl_path)

            # Compose 3MF
            threemf_path, compose_err = _try_compose_3mf(zones, output_dir, base_name)

            return _build_result(
                zones, output_dir, base_name, "texture",
                total_faces, threemf_path,
                compose_3mf_error=compose_err,
            )

        _logger.debug("Registered color tools")


plugin = _ColorToolsPlugin()
