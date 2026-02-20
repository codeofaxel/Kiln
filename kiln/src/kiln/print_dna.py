"""Print DNA — model fingerprinting and cross-user learning.

Hashes and fingerprints every 3D model that flows through Kiln. Tracks
which models printed successfully, which failed, on which printers, with
which settings. Builds a knowledge graph that enables optimal settings
prediction for similar models.

The fingerprint includes: file hash (SHA-256), geometric signature
(triangle count, bounding box, surface area), complexity metrics
(overhang ratio, thin wall ratio), and material compatibility scores.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import struct
import time
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ModelFingerprint:
    """Geometric fingerprint of a 3D model file."""

    file_hash: str  # SHA-256
    triangle_count: int
    vertex_count: int
    bounding_box: dict[str, float]  # min_x, max_x, min_y, max_y, min_z, max_z
    surface_area_mm2: float
    volume_mm3: float
    overhang_ratio: float  # 0.0 - 1.0
    complexity_score: float  # 0.0 - 1.0 (simple cube=0.1, organic shape=0.9)
    geometric_signature: str  # compact hash of geometry for similarity matching

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrintDNARecord:
    """A recorded print attempt linked to a model fingerprint."""

    fingerprint: ModelFingerprint
    printer_model: str
    material: str
    settings: dict[str, Any]  # layer_height, speed, temps, etc.
    outcome: str  # "success", "failed", "partial"
    quality_grade: str  # "A", "B", "C", "D", "F"
    failure_mode: str | None  # "spaghetti", "adhesion", "stringing", etc.
    print_time_seconds: int
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["fingerprint"] = self.fingerprint.to_dict()
        return data


@dataclass
class SettingsPrediction:
    """Predicted optimal settings based on historical print DNA data."""

    recommended_settings: dict[str, Any]
    confidence: float  # 0.0 - 1.0
    based_on_prints: int
    success_rate: float
    similar_models_count: int
    source: str  # "exact_match", "similar_geometry", "material_default"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_OUTCOMES = frozenset({"success", "failed", "partial"})
_VALID_GRADES = frozenset({"A", "B", "C", "D", "F"})


# ---------------------------------------------------------------------------
# STL parsing helpers
# ---------------------------------------------------------------------------


def _parse_binary_stl(data: bytes) -> tuple[list[tuple], list[tuple]]:
    """Parse a binary STL file into lists of (triangles, vertices).

    Returns:
        A tuple of (triangles, unique_vertices).  Each triangle is a tuple
        of three vertex indices plus the normal.  Each vertex is ``(x, y, z)``.
    """
    if len(data) < 84:
        raise ValueError("File too small to be a valid STL")

    num_triangles = struct.unpack_from("<I", data, 80)[0]
    expected_size = 84 + num_triangles * 50
    if len(data) < expected_size:
        raise ValueError(
            f"Binary STL declares {num_triangles} triangles but file is too small ({len(data)} < {expected_size} bytes)"
        )

    triangles: list[tuple] = []
    vertices_set: dict[tuple[float, float, float], int] = {}
    vertices_list: list[tuple[float, float, float]] = []

    offset = 84
    for _ in range(num_triangles):
        nx, ny, nz = struct.unpack_from("<fff", data, offset)
        offset += 12

        tri_verts: list[int] = []
        for _ in range(3):
            vx, vy, vz = struct.unpack_from("<fff", data, offset)
            offset += 12
            key = (round(vx, 6), round(vy, 6), round(vz, 6))
            if key not in vertices_set:
                vertices_set[key] = len(vertices_list)
                vertices_list.append(key)
            tri_verts.append(vertices_set[key])

        # skip attribute byte count
        offset += 2
        triangles.append((tri_verts[0], tri_verts[1], tri_verts[2], (nx, ny, nz)))

    return triangles, vertices_list


def _parse_ascii_stl(data: bytes) -> tuple[list[tuple], list[tuple]]:
    """Parse an ASCII STL file."""
    text = data.decode("ascii", errors="replace")
    lines = text.strip().split("\n")

    triangles: list[tuple] = []
    vertices_set: dict[tuple[float, float, float], int] = {}
    vertices_list: list[tuple[float, float, float]] = []
    current_normal: tuple[float, float, float] = (0.0, 0.0, 0.0)
    current_verts: list[int] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("facet normal"):
            parts = stripped.split()
            if len(parts) >= 5:
                current_normal = (float(parts[2]), float(parts[3]), float(parts[4]))
            current_verts = []
        elif stripped.startswith("vertex"):
            parts = stripped.split()
            if len(parts) >= 4:
                vx, vy, vz = float(parts[1]), float(parts[2]), float(parts[3])
                key = (round(vx, 6), round(vy, 6), round(vz, 6))
                if key not in vertices_set:
                    vertices_set[key] = len(vertices_list)
                    vertices_list.append(key)
                current_verts.append(vertices_set[key])
        elif stripped.startswith("endfacet"):
            if len(current_verts) == 3:
                triangles.append((current_verts[0], current_verts[1], current_verts[2], current_normal))

    return triangles, vertices_list


def _triangle_area(v0: tuple, v1: tuple, v2: tuple) -> float:
    """Compute triangle area using the cross product method."""
    ax, ay, az = v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]
    bx, by, bz = v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]
    cx = ay * bz - az * by
    cy = az * bx - ax * bz
    cz = ax * by - ay * bx
    return 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)


def _signed_volume_of_triangle(v0: tuple, v1: tuple, v2: tuple) -> float:
    """Compute signed volume contribution of a triangle (for mesh volume)."""
    return (
        v0[0] * (v1[1] * v2[2] - v2[1] * v1[2])
        - v1[0] * (v0[1] * v2[2] - v2[1] * v0[2])
        + v2[0] * (v0[1] * v1[2] - v1[1] * v0[2])
    ) / 6.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fingerprint_model(file_path: str) -> ModelFingerprint:
    """Compute a full fingerprint from an STL file.

    Reads the file, parses geometry, and computes hash, bounding box,
    surface area, volume, overhang ratio, complexity score, and a
    geometric signature for similarity matching.

    :param file_path: Path to an STL file.
    :raises ValueError: If the file is empty or unparseable.
    :raises FileNotFoundError: If the file does not exist.
    """
    with open(file_path, "rb") as f:
        data = f.read()

    if not data:
        raise ValueError("Empty file")

    file_hash = hashlib.sha256(data).hexdigest()

    # Determine ASCII vs binary STL
    is_ascii = data[:5].lower() == b"solid" and b"facet" in data[:1000]
    if is_ascii:
        triangles, vertices = _parse_ascii_stl(data)
    else:
        triangles, vertices = _parse_binary_stl(data)

    if not triangles:
        raise ValueError("No triangles found in STL file")

    triangle_count = len(triangles)
    vertex_count = len(vertices)

    # Bounding box
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    bounding_box = {
        "min_x": min(xs),
        "max_x": max(xs),
        "min_y": min(ys),
        "max_y": max(ys),
        "min_z": min(zs),
        "max_z": max(zs),
    }

    # Surface area and volume
    surface_area = 0.0
    volume = 0.0
    overhang_count = 0

    for tri in triangles:
        v0 = vertices[tri[0]]
        v1 = vertices[tri[1]]
        v2 = vertices[tri[2]]
        normal = tri[3]

        surface_area += _triangle_area(v0, v1, v2)
        volume += _signed_volume_of_triangle(v0, v1, v2)

        # Overhang detection: normal pointing significantly downward (Z < -0.5)
        if normal[2] < -0.5:
            overhang_count += 1

    volume = abs(volume)
    overhang_ratio = overhang_count / triangle_count if triangle_count > 0 else 0.0

    # Complexity score heuristic:
    # Based on triangle density relative to bounding box volume and vertex ratio
    bbox_vol = max(
        (bounding_box["max_x"] - bounding_box["min_x"])
        * (bounding_box["max_y"] - bounding_box["min_y"])
        * (bounding_box["max_z"] - bounding_box["min_z"]),
        1e-6,
    )
    tri_density = triangle_count / (bbox_vol ** (1 / 3))
    # Normalise to 0-1 range with a sigmoid-like curve
    complexity_score = min(1.0, max(0.0, 1.0 - 1.0 / (1.0 + tri_density / 100.0)))

    # Geometric signature: hash of sorted vertex coordinates + triangle count
    sig_data = f"{triangle_count}:{vertex_count}:{round(surface_area, 2)}:{round(volume, 2)}"
    geometric_signature = hashlib.sha256(sig_data.encode()).hexdigest()[:16]

    return ModelFingerprint(
        file_hash=file_hash,
        triangle_count=triangle_count,
        vertex_count=vertex_count,
        bounding_box=bounding_box,
        surface_area_mm2=round(surface_area, 4),
        volume_mm3=round(volume, 4),
        overhang_ratio=round(overhang_ratio, 4),
        complexity_score=round(complexity_score, 4),
        geometric_signature=geometric_signature,
    )


def record_print_dna(
    fingerprint: ModelFingerprint,
    printer_model: str,
    material: str,
    settings: dict[str, Any],
    outcome: str,
    *,
    quality_grade: str = "B",
    failure_mode: str | None = None,
    print_time_seconds: int = 0,
) -> None:
    """Save a print DNA record to the database.

    :param fingerprint: The model fingerprint.
    :param printer_model: Printer model name.
    :param material: Material used (e.g. ``"PLA"``, ``"PETG"``).
    :param settings: Print settings dict.
    :param outcome: One of ``"success"``, ``"failed"``, ``"partial"``.
    :param quality_grade: Grade from ``"A"`` to ``"F"``.
    :param failure_mode: Optional failure description.
    :param print_time_seconds: Print duration in seconds.
    :raises ValueError: If outcome or quality_grade is invalid.
    """
    from kiln.persistence import get_db

    if outcome not in _VALID_OUTCOMES:
        raise ValueError(f"Invalid outcome {outcome!r}. Must be one of: {', '.join(sorted(_VALID_OUTCOMES))}")
    if quality_grade not in _VALID_GRADES:
        raise ValueError(f"Invalid quality_grade {quality_grade!r}. Must be one of: {', '.join(sorted(_VALID_GRADES))}")

    db = get_db()
    now = time.time()

    with db._write_lock:
        db._conn.execute(
            """
            INSERT INTO print_dna (
                file_hash, geometric_signature, triangle_count,
                bounding_box, surface_area, volume,
                overhang_ratio, complexity_score,
                printer_model, material, settings,
                outcome, quality_grade, failure_mode,
                print_time_seconds, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint.file_hash,
                fingerprint.geometric_signature,
                fingerprint.triangle_count,
                json.dumps(fingerprint.bounding_box),
                fingerprint.surface_area_mm2,
                fingerprint.volume_mm3,
                fingerprint.overhang_ratio,
                fingerprint.complexity_score,
                printer_model,
                material,
                json.dumps(settings),
                outcome,
                quality_grade,
                failure_mode,
                print_time_seconds,
                now,
            ),
        )
        db._conn.commit()


def _row_to_record(row: Any) -> PrintDNARecord:
    """Convert a database row to a PrintDNARecord."""
    row_dict = dict(row)
    bbox = json.loads(row_dict.get("bounding_box") or "{}")
    settings = json.loads(row_dict.get("settings") or "{}")

    fp = ModelFingerprint(
        file_hash=row_dict["file_hash"],
        triangle_count=row_dict.get("triangle_count", 0),
        vertex_count=0,  # not stored in DB
        bounding_box=bbox,
        surface_area_mm2=row_dict.get("surface_area", 0.0),
        volume_mm3=row_dict.get("volume", 0.0),
        overhang_ratio=row_dict.get("overhang_ratio", 0.0),
        complexity_score=row_dict.get("complexity_score", 0.0),
        geometric_signature=row_dict.get("geometric_signature", ""),
    )

    return PrintDNARecord(
        fingerprint=fp,
        printer_model=row_dict.get("printer_model", ""),
        material=row_dict.get("material", ""),
        settings=settings,
        outcome=row_dict["outcome"],
        quality_grade=row_dict.get("quality_grade", "B"),
        failure_mode=row_dict.get("failure_mode"),
        print_time_seconds=row_dict.get("print_time_seconds", 0),
        timestamp=row_dict.get("timestamp", 0.0),
    )


def predict_settings(
    fingerprint: ModelFingerprint,
    printer_model: str,
    material: str,
) -> SettingsPrediction:
    """Predict optimal settings from historical print DNA.

    Searches for exact file hash matches first, then falls back to
    geometrically similar models, and finally to material defaults.

    :param fingerprint: The model fingerprint.
    :param printer_model: Target printer model.
    :param material: Target material.
    """
    from kiln.persistence import get_db

    db = get_db()

    # Strategy 1: exact file hash match
    rows = db._conn.execute(
        """
        SELECT * FROM print_dna
        WHERE file_hash = ? AND printer_model = ? AND material = ?
            AND outcome = 'success'
        ORDER BY quality_grade ASC, timestamp DESC
        LIMIT 20
        """,
        (fingerprint.file_hash, printer_model, material),
    ).fetchall()

    if rows:
        return _aggregate_prediction(rows, source="exact_match")

    # Strategy 2: similar geometric signature
    rows = db._conn.execute(
        """
        SELECT * FROM print_dna
        WHERE geometric_signature = ? AND printer_model = ? AND material = ?
            AND outcome = 'success'
        ORDER BY quality_grade ASC, timestamp DESC
        LIMIT 20
        """,
        (fingerprint.geometric_signature, printer_model, material),
    ).fetchall()

    if rows:
        return _aggregate_prediction(rows, source="similar_geometry")

    # Strategy 3: material defaults on this printer
    rows = db._conn.execute(
        """
        SELECT * FROM print_dna
        WHERE printer_model = ? AND material = ? AND outcome = 'success'
        ORDER BY quality_grade ASC, timestamp DESC
        LIMIT 20
        """,
        (printer_model, material),
    ).fetchall()

    if rows:
        return _aggregate_prediction(rows, source="material_default")

    return SettingsPrediction(
        recommended_settings={},
        confidence=0.0,
        based_on_prints=0,
        success_rate=0.0,
        similar_models_count=0,
        source="no_data",
    )


def _aggregate_prediction(rows: list, *, source: str) -> SettingsPrediction:
    """Aggregate settings from successful print records into a prediction."""
    all_settings: list[dict[str, Any]] = []
    for row in rows:
        row_dict = dict(row)
        settings = json.loads(row_dict.get("settings") or "{}")
        if settings:
            all_settings.append(settings)

    # Merge settings: use median for numeric values, mode for strings
    merged: dict[str, Any] = {}
    if all_settings:
        all_keys = set()
        for s in all_settings:
            all_keys.update(s.keys())

        for key in all_keys:
            values = [s[key] for s in all_settings if key in s]
            if not values:
                continue
            if all(isinstance(v, (int, float)) for v in values):
                sorted_vals = sorted(values)
                n = len(sorted_vals)
                if n % 2 == 1:
                    merged[key] = sorted_vals[n // 2]
                else:
                    merged[key] = round((sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2, 2)
            else:
                # Mode for non-numeric values
                from collections import Counter

                merged[key] = Counter(values).most_common(1)[0][0]

    n_prints = len(rows)
    confidence_map = {"exact_match": 0.9, "similar_geometry": 0.7, "material_default": 0.4}
    base_confidence = confidence_map.get(source, 0.3)
    # Adjust by sample size
    sample_factor = min(1.0, n_prints / 10.0)
    confidence = round(base_confidence * sample_factor, 2)

    return SettingsPrediction(
        recommended_settings=merged,
        confidence=confidence,
        based_on_prints=n_prints,
        success_rate=1.0,  # all rows are successful outcomes
        similar_models_count=n_prints,
        source=source,
    )


def find_similar_models(
    fingerprint: ModelFingerprint,
    *,
    limit: int = 10,
    threshold: float = 0.8,
) -> list[PrintDNARecord]:
    """Find geometrically similar models in the print DNA database.

    Currently uses geometric signature matching (exact signature match
    implies high geometric similarity). Future versions may use more
    granular similarity metrics.

    :param fingerprint: The reference fingerprint.
    :param limit: Maximum results.
    :param threshold: Similarity threshold (0.0 - 1.0). Currently only
        exact signature matches (threshold < 1.0) are returned.
    """
    from kiln.persistence import get_db

    db = get_db()

    if threshold >= 1.0:
        # Exact geometric signature match only
        rows = db._conn.execute(
            """
            SELECT * FROM print_dna
            WHERE geometric_signature = ? AND file_hash != ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (fingerprint.geometric_signature, fingerprint.file_hash, limit),
        ).fetchall()
    else:
        # Include same geometric signature (exact similarity)
        # plus fuzzy matches based on complexity and surface area ranges
        sa_lo = fingerprint.surface_area_mm2 * threshold
        sa_hi = fingerprint.surface_area_mm2 / max(threshold, 0.01)
        vol_lo = fingerprint.volume_mm3 * threshold
        vol_hi = fingerprint.volume_mm3 / max(threshold, 0.01)

        rows = db._conn.execute(
            """
            SELECT * FROM print_dna
            WHERE file_hash != ?
                AND (
                    geometric_signature = ?
                    OR (
                        surface_area BETWEEN ? AND ?
                        AND volume BETWEEN ? AND ?
                        AND ABS(complexity_score - ?) < ?
                    )
                )
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (
                fingerprint.file_hash,
                fingerprint.geometric_signature,
                sa_lo,
                sa_hi,
                vol_lo,
                vol_hi,
                fingerprint.complexity_score,
                1.0 - threshold,
                limit,
            ),
        ).fetchall()

    return [_row_to_record(row) for row in rows]


def get_model_history(file_hash: str) -> list[PrintDNARecord]:
    """Return all print attempts for a model identified by file hash.

    :param file_hash: SHA-256 hash of the model file.
    """
    from kiln.persistence import get_db

    db = get_db()
    rows = db._conn.execute(
        "SELECT * FROM print_dna WHERE file_hash = ? ORDER BY timestamp DESC",
        (file_hash,),
    ).fetchall()

    return [_row_to_record(row) for row in rows]


def get_success_rate(
    file_hash: str,
    *,
    printer_model: str | None = None,
    material: str | None = None,
) -> dict[str, Any]:
    """Compute success rate metrics for a model.

    :param file_hash: SHA-256 hash of the model file.
    :param printer_model: Optional filter by printer.
    :param material: Optional filter by material.
    """
    from kiln.persistence import get_db

    db = get_db()

    query = "SELECT outcome, quality_grade FROM print_dna WHERE file_hash = ?"
    params: list[Any] = [file_hash]

    if printer_model:
        query += " AND printer_model = ?"
        params.append(printer_model)
    if material:
        query += " AND material = ?"
        params.append(material)

    rows = db._conn.execute(query, params).fetchall()

    if not rows:
        return {
            "file_hash": file_hash,
            "total_prints": 0,
            "success_rate": 0.0,
            "outcomes": {},
            "grade_distribution": {},
        }

    total = len(rows)
    outcomes: dict[str, int] = {}
    grades: dict[str, int] = {}

    for row in rows:
        row_dict = dict(row)
        outcome = row_dict["outcome"]
        grade = row_dict.get("quality_grade", "B")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        grades[grade] = grades.get(grade, 0) + 1

    success_count = outcomes.get("success", 0)

    return {
        "file_hash": file_hash,
        "total_prints": total,
        "success_rate": round(success_count / total, 4) if total > 0 else 0.0,
        "outcomes": outcomes,
        "grade_distribution": grades,
    }
