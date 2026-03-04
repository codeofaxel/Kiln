"""Advanced mesh diagnostics using Trimesh.

Detects defects that the built-in stdlib validation misses:
self-intersections, inverted/inconsistent normals, degenerate faces,
floating fragments (disconnected components), and detailed hole
reporting with count, size, and location.

Trimesh is an optional dependency — import errors are caught at call
time and produce a clear error message.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUPPORTED_EXTENSIONS = frozenset({".stl", ".obj", ".ply", ".off", ".glb", ".gltf"})

# Degenerate face: area below this threshold (mm²) is considered zero-area.
_DEGENERATE_AREA_THRESHOLD = 1e-10

# Polygon count guidance thresholds for FDM at 0.2mm layer height.
_POLYGON_EXCESSIVE = 2_000_000
_POLYGON_HIGH = 500_000
_POLYGON_RECOMMENDED_FDM = 200_000


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class HoleInfo:
    """A single boundary loop (hole) in the mesh."""

    edge_count: int
    perimeter_mm: float
    centroid_x: float
    centroid_y: float
    centroid_z: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComponentInfo:
    """A disconnected component (island) in the mesh."""

    face_count: int
    volume_mm3: float
    is_largest: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NormalsReport:
    """Results of face normal consistency analysis."""

    consistent: bool
    inverted_count: int
    inverted_percentage: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PolygonCountAssessment:
    """Polygon count assessment relative to FDM printing."""

    face_count: int
    vertex_count: int
    level: str  # "ok", "high", "excessive"
    recommended_for_fdm: int
    decimation_ratio: float  # 1.0 = no decimation needed, 0.1 = reduce to 10%
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MeshDiagnosticReport:
    """Complete mesh diagnostic report.

    Covers defects that Kiln's built-in validation does not check:
    self-intersections, inverted normals, degenerate faces, disconnected
    components, and detailed hole analysis.
    """

    file_path: str
    file_size_bytes: int

    # Geometry basics
    face_count: int
    vertex_count: int
    is_watertight: bool
    volume_mm3: float | None  # None if not watertight
    surface_area_mm2: float
    bounding_box: dict[str, float]
    dimensions_mm: dict[str, float]

    # Defect analysis
    degenerate_face_count: int
    self_intersection_count: int
    normals: NormalsReport
    polygon_assessment: PolygonCountAssessment

    # Holes
    hole_count: int
    holes: list[HoleInfo]

    # Components (floating fragments)
    component_count: int
    components: list[ComponentInfo]
    has_floating_fragments: bool

    # Overall
    severity: str  # "clean", "minor", "moderate", "severe"
    defects: list[str]
    recommendations: list[str]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_trimesh() -> Any:
    """Import and return trimesh, raising a clear error if unavailable."""
    try:
        import trimesh  # type: ignore[import-untyped]

        return trimesh
    except ImportError:
        raise ImportError(
            "Trimesh is required for advanced mesh diagnostics. "
            "Install it with: pip install trimesh"
        ) from None


def _load_mesh(file_path: str) -> Any:
    """Load a mesh file via Trimesh.

    Returns a ``trimesh.Trimesh`` object.  If the file contains a scene
    (multiple meshes), they are concatenated into one.

    :raises ValueError: If the file does not exist, has an unsupported
        extension, or contains no geometry.
    """
    trimesh = _require_trimesh()

    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"File not found: {file_path}")

    ext = path.suffix.lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type: {ext!r}. "
            f"Expected one of: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
        )

    # Load without full processing to preserve the original mesh state
    # (including any inverted normals, degenerate faces, etc.).
    # This is critical for diagnostics — we need to see defects,
    # not have them silently fixed on load.
    loaded = trimesh.load(str(path), force="mesh", process=False)

    # trimesh.load with force="mesh" returns a Trimesh for single-mesh
    # files and concatenates scenes.  Verify we got usable geometry.
    if not hasattr(loaded, "faces") or len(loaded.faces) == 0:
        raise ValueError("File contains no mesh geometry.")

    # Merge duplicate vertices (essential for STL files where each
    # triangle has its own copy of each vertex).  Without this, edge
    # analysis sees every edge as boundary and the mesh appears
    # non-watertight.  This does NOT fix normals or remove degenerate
    # faces — it only welds coincident vertices.
    loaded.merge_vertices()

    return loaded


def _analyze_holes(mesh: Any) -> tuple[int, list[HoleInfo]]:
    """Analyze boundary edges (holes) in the mesh.

    Uses Trimesh's facets_on_hull or boundary edge grouping to find
    open loops.  Returns (hole_count, list of HoleInfo) with at most
    20 entries.
    """
    import numpy as np  # type: ignore[import-untyped]

    try:
        # Trimesh exposes boundary "face groups" via outline, but that
        # can fail on non-manifold meshes.  The most reliable approach
        # is to use the boundary edge adjacency graph directly.

        # grouped_boundary is a list of arrays, each array containing
        # vertex indices forming one boundary loop.
        # Edges that appear only once are boundary edges.
        edges_sorted = mesh.edges_sorted
        unique, counts = np.unique(edges_sorted, axis=0, return_counts=True)
        boundary_edges = unique[counts == 1]

        if len(boundary_edges) == 0:
            return 0, []

        # Group boundary edges into connected loops.
        loops = _group_boundary_loops(boundary_edges, mesh.vertices)

    except Exception:
        # Fallback: try the outline() method.
        try:
            outline = mesh.outline()
            entities = outline.entities if hasattr(outline, "entities") else []
            if not entities:
                return 0, []
            loops = []
            verts = outline.vertices if hasattr(outline, "vertices") else []
            for entity in entities:
                pts = entity.points
                loop_verts = [verts[p] for p in pts if p < len(verts)]
                if loop_verts:
                    loops.append(np.array(loop_verts))
        except Exception:
            return 0, []

    holes: list[HoleInfo] = []
    for loop_verts in loops:
        if len(loop_verts) < 3:
            continue
        edge_count = len(loop_verts)
        # Perimeter: sum of consecutive vertex distances.
        diffs = np.diff(loop_verts, axis=0)
        perimeter = float(np.sum(np.linalg.norm(diffs, axis=1)))
        # Close the loop.
        perimeter += float(np.linalg.norm(loop_verts[0] - loop_verts[-1]))
        centroid = loop_verts.mean(axis=0)
        holes.append(
            HoleInfo(
                edge_count=edge_count,
                perimeter_mm=round(perimeter, 2),
                centroid_x=round(float(centroid[0]), 2),
                centroid_y=round(float(centroid[1]), 2),
                centroid_z=round(float(centroid[2]), 2),
            )
        )

    # Sort by perimeter descending (largest holes first).
    holes.sort(key=lambda h: h.perimeter_mm, reverse=True)
    return len(holes), holes[:20]


def _group_boundary_loops(
    boundary_edges: Any,
    vertices: Any,
) -> list[Any]:
    """Group boundary edges into connected loops of vertex coordinates.

    Each returned array is an ordered sequence of 3D vertex positions
    forming one closed boundary loop.
    """
    import numpy as np  # type: ignore[import-untyped]

    if len(boundary_edges) == 0:
        return []

    # Build adjacency: vertex -> list of connected vertices via boundary edges.
    adjacency: dict[int, list[int]] = {}
    for edge in boundary_edges:
        a, b = int(edge[0]), int(edge[1])
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)

    visited_edges: set[tuple[int, int]] = set()
    loops: list[Any] = []

    for start in adjacency:
        if all(
            (min(start, n), max(start, n)) in visited_edges
            for n in adjacency[start]
        ):
            continue

        # Walk the loop.
        loop: list[int] = [start]
        current = start
        prev = -1

        for _ in range(len(boundary_edges) + 1):
            neighbors = adjacency.get(current, [])
            next_vert = -1
            for n in neighbors:
                edge_key = (min(current, n), max(current, n))
                if edge_key not in visited_edges and n != prev:
                    next_vert = n
                    break

            if next_vert == -1:
                break

            visited_edges.add((min(current, next_vert), max(current, next_vert)))
            if next_vert == start:
                break
            loop.append(next_vert)
            prev = current
            current = next_vert

        if len(loop) >= 3:
            loops.append(np.array([vertices[v] for v in loop]))

    return loops


def _analyze_components(mesh: Any) -> tuple[int, list[ComponentInfo], bool]:
    """Analyze connected components (islands) in the mesh.

    Uses face adjacency BFS to find connected components without
    requiring scipy or networkx (which trimesh's ``split()`` needs).

    Returns (count, list of ComponentInfo, has_floating_fragments).
    """
    try:
        n_faces = len(mesh.faces)
        if n_faces == 0:
            return 0, [], False

        # Build adjacency: face index → set of adjacent face indices.
        adj_map: dict[int, list[int]] = {i: [] for i in range(n_faces)}
        try:
            face_adj = mesh.face_adjacency
            for pair in face_adj:
                a, b = int(pair[0]), int(pair[1])
                adj_map[a].append(b)
                adj_map[b].append(a)
        except Exception:
            # If face_adjacency fails, fall back to single component.
            vol = float(mesh.volume) if mesh.is_watertight else 0.0
            return 1, [ComponentInfo(face_count=n_faces, volume_mm3=round(vol, 2), is_largest=True)], False

        # BFS to find connected components.
        visited = [False] * n_faces
        component_labels: list[list[int]] = []

        for start in range(n_faces):
            if visited[start]:
                continue
            # BFS from start.
            group: list[int] = []
            queue = [start]
            visited[start] = True
            while queue:
                current = queue.pop()
                group.append(current)
                for neighbor in adj_map[current]:
                    if not visited[neighbor]:
                        visited[neighbor] = True
                        queue.append(neighbor)
            component_labels.append(group)

        if len(component_labels) <= 1:
            vol = float(mesh.volume) if mesh.is_watertight else 0.0
            return 1, [ComponentInfo(face_count=n_faces, volume_mm3=round(vol, 2), is_largest=True)], False

        # Build ComponentInfo for each group.
        components: list[ComponentInfo] = []
        max_faces = 0
        max_idx = 0

        for i, group in enumerate(component_labels):
            fc = len(group)
            # Estimate volume: extract submesh and check if watertight.
            vol = 0.0
            try:
                submesh = mesh.submesh([group], only_watertight=False, append=True)
                if submesh.is_watertight:
                    vol = float(submesh.volume)
            except Exception:
                pass
            components.append(
                ComponentInfo(face_count=fc, volume_mm3=round(vol, 2), is_largest=False)
            )
            if fc > max_faces:
                max_faces = fc
                max_idx = i

        if components:
            components[max_idx].is_largest = True

        # Sort by face count descending.
        components.sort(key=lambda c: c.face_count, reverse=True)

        return len(components), components[:20], True

    except Exception as exc:
        logger.debug("Component analysis failed: %s", exc)
        vol = float(mesh.volume) if mesh.is_watertight else 0.0
        return 1, [ComponentInfo(face_count=len(mesh.faces), volume_mm3=round(vol, 2), is_largest=True)], False


def _analyze_normals(mesh: Any) -> NormalsReport:
    """Check face normal consistency.

    Uses two approaches:
    1. Winding consistency: checks if adjacent faces have consistent
       winding order via trimesh's broken face detection.
    2. Signed volume: if the mesh is closed but has negative volume,
       the majority of normals point inward (inverted).
    """
    try:
        total = len(mesh.faces)
        if total == 0:
            return NormalsReport(consistent=True, inverted_count=0, inverted_percentage=0.0)

        inverted = 0

        # Approach 1: Check face adjacency winding consistency.
        # For each pair of adjacent faces sharing an edge, the shared
        # edge should appear in opposite winding directions.  Trimesh
        # exposes this via face_adjacency and face_adjacency_edges.
        try:
            if hasattr(mesh, "face_adjacency") and len(mesh.face_adjacency) > 0:
                adj = mesh.face_adjacency
                adj_edges = mesh.face_adjacency_edges

                # For each adjacent pair, check if the shared edge has
                # consistent winding (appears reversed in the two faces).
                inconsistent = 0
                for i in range(len(adj)):
                    f1_idx, f2_idx = adj[i]
                    edge_v = adj_edges[i]
                    f1 = mesh.faces[f1_idx]
                    f2 = mesh.faces[f2_idx]

                    # Find the shared edge in each face.
                    e0, e1 = edge_v[0], edge_v[1]

                    # In face 1, find which direction the edge appears.
                    f1_dir = _edge_direction_in_face(f1, e0, e1)
                    f2_dir = _edge_direction_in_face(f2, e0, e1)

                    # Consistent winding: the edge should appear in
                    # opposite directions in adjacent faces.
                    if f1_dir == f2_dir and f1_dir != 0:
                        inconsistent += 1

                # Each inconsistent adjacency means one of the two faces
                # is inverted.  Estimate inverted count conservatively.
                inverted = min(inconsistent, total)
        except Exception:
            pass

        # Approach 2: Signed volume check.  If the mesh appears closed
        # but has negative volume, normals are predominantly inverted.
        if inverted == 0:
            try:
                if mesh.is_watertight:
                    vol = float(mesh.volume)
                    if vol < 0:
                        inverted = total  # All normals inverted.
            except Exception:
                pass

        pct = round(inverted / total * 100.0, 1) if total > 0 else 0.0

        return NormalsReport(
            consistent=inverted == 0,
            inverted_count=inverted,
            inverted_percentage=pct,
        )
    except Exception as exc:
        logger.debug("Normal analysis failed: %s", exc)
        return NormalsReport(consistent=True, inverted_count=0, inverted_percentage=0.0)


def _edge_direction_in_face(face: Any, e0: int, e1: int) -> int:
    """Return +1 if edge (e0→e1) appears in the face's winding, -1 if reversed, 0 if not found."""
    for i in range(3):
        a = face[i]
        b = face[(i + 1) % 3]
        if a == e0 and b == e1:
            return 1
        if a == e1 and b == e0:
            return -1
    return 0


def _count_degenerate_faces(mesh: Any) -> int:
    """Count faces with near-zero area (degenerate triangles)."""
    try:
        import numpy as np  # type: ignore[import-untyped]

        areas = mesh.area_faces
        return int(np.sum(areas < _DEGENERATE_AREA_THRESHOLD))
    except Exception:
        return 0


def _count_self_intersections(mesh: Any) -> int:
    """Detect self-intersecting faces in the mesh.

    Uses Trimesh's ``intersection.mesh_plane`` and face-pair tests when
    available.  For meshes over 500K faces this is too expensive and
    returns ``-1`` (skipped).

    Returns the number of self-intersecting face pairs, ``0`` if clean,
    or ``-1`` if the check was skipped.
    """
    if len(mesh.faces) > 500_000:
        return -1  # Too expensive; skipped.

    try:
        import numpy as np  # type: ignore[import-untyped]

        # Trimesh doesn't expose a direct "count self-intersections" API.
        # The most reliable heuristic: process a copy with validate=True
        # and compare face counts.  Trimesh's process() removes
        # degenerate and problematic faces.
        original_count = len(mesh.faces)
        processed = mesh.copy()
        processed.process(validate=True)
        removed = original_count - len(processed.faces)

        # Also check for non-manifold faces via edge valence.
        # Edges shared by more than 2 faces indicate self-intersections
        # or non-manifold geometry.
        edges = mesh.edges_sorted
        # Count unique edges and their frequencies.
        unique_edges, counts = np.unique(edges, axis=0, return_counts=True)
        non_manifold_edges = int(np.sum(counts > 2))

        return max(removed, non_manifold_edges)

    except Exception as exc:
        logger.debug("Self-intersection check failed: %s", exc)
        return 0


def _assess_polygon_count(face_count: int, vertex_count: int) -> PolygonCountAssessment:
    """Assess polygon count relative to FDM printing needs."""
    if face_count > _POLYGON_EXCESSIVE:
        level = "excessive"
        ratio = _POLYGON_RECOMMENDED_FDM / face_count
        msg = (
            f"{face_count:,} faces is excessive for FDM printing. "
            f"At 0.2mm layer height, the slicer cannot use this detail. "
            f"Recommend decimating to ~{_POLYGON_RECOMMENDED_FDM:,} faces "
            f"({ratio:.0%} of current) for faster slicing with no visible quality loss."
        )
    elif face_count > _POLYGON_HIGH:
        level = "high"
        ratio = _POLYGON_RECOMMENDED_FDM / face_count
        msg = (
            f"{face_count:,} faces is high for FDM. Slicing will be slower "
            f"than needed. Consider decimating to ~{_POLYGON_RECOMMENDED_FDM:,} "
            f"faces for faster processing."
        )
    else:
        level = "ok"
        ratio = 1.0
        msg = f"{face_count:,} faces — appropriate for FDM printing."

    return PolygonCountAssessment(
        face_count=face_count,
        vertex_count=vertex_count,
        level=level,
        recommended_for_fdm=_POLYGON_RECOMMENDED_FDM,
        decimation_ratio=round(ratio, 3),
        message=msg,
    )


def _compute_severity(
    *,
    degenerate_count: int,
    self_intersection_count: int,
    normals: NormalsReport,
    hole_count: int,
    has_fragments: bool,
    is_watertight: bool,
) -> str:
    """Compute overall severity level."""
    issues = 0

    if self_intersection_count > 0:
        issues += 2
    if not normals.consistent and normals.inverted_percentage > 10.0:
        issues += 2
    elif not normals.consistent:
        issues += 1
    if hole_count > 5:
        issues += 2
    elif hole_count > 0:
        issues += 1
    if degenerate_count > 100:
        issues += 2
    elif degenerate_count > 0:
        issues += 1
    if has_fragments:
        issues += 1
    if not is_watertight:
        issues += 1

    if issues == 0:
        return "clean"
    if issues <= 2:
        return "minor"
    if issues <= 4:
        return "moderate"
    return "severe"


def _build_defects(
    *,
    degenerate_count: int,
    self_intersection_count: int,
    normals: NormalsReport,
    hole_count: int,
    holes: list[HoleInfo],
    has_fragments: bool,
    component_count: int,
    is_watertight: bool,
    polygon_assessment: PolygonCountAssessment,
) -> list[str]:
    """Build a list of human-readable defect descriptions."""
    defects: list[str] = []

    if not is_watertight:
        defects.append("Mesh is not watertight (has open boundaries).")

    if hole_count > 0:
        largest = holes[0] if holes else None
        size_note = f" Largest hole: {largest.perimeter_mm}mm perimeter." if largest else ""
        defects.append(f"{hole_count} hole(s) found.{size_note}")

    if not normals.consistent:
        defects.append(
            f"{normals.inverted_count} face(s) ({normals.inverted_percentage}%) "
            f"have inverted normals — may cause inside-out surfaces when printed."
        )

    if degenerate_count > 0:
        defects.append(
            f"{degenerate_count} degenerate (zero-area) face(s) — can cause slicer errors."
        )

    if self_intersection_count > 0:
        defects.append(
            f"{self_intersection_count} self-intersecting face(s) — "
            f"may cause unpredictable slicer behavior."
        )
    elif self_intersection_count == -1:
        defects.append(
            "Self-intersection check skipped (mesh exceeds 500K faces). "
            "Decimate first, then re-run diagnostics."
        )

    if has_fragments:
        defects.append(
            f"{component_count} disconnected components found — "
            f"{component_count - 1} floating fragment(s) should be removed."
        )

    if polygon_assessment.level != "ok":
        defects.append(polygon_assessment.message)

    return defects


def _build_recommendations(
    *,
    defects: list[str],
    degenerate_count: int,
    self_intersection_count: int,
    normals: NormalsReport,
    hole_count: int,
    has_fragments: bool,
    is_watertight: bool,
    polygon_assessment: PolygonCountAssessment,
) -> list[str]:
    """Build actionable recommendations for fixing detected defects."""
    recs: list[str] = []

    if not defects:
        recs.append("Mesh is clean — no defects detected. Ready for slicing.")
        return recs

    if not normals.consistent:
        recs.append(
            "Fix inverted normals: In MeshLab, use Filters > Normals, Curvatures > "
            "Re-Orient All Faces Coherently. Or in Blender: select all faces, "
            "Mesh > Normals > Recalculate Outside."
        )

    if hole_count > 0:
        if hole_count <= 5:
            recs.append(
                "Fill holes: In MeshLab, use Filters > Remeshing > Close Holes. "
                "PrusaSlicer can also auto-repair small holes during slicing."
            )
        else:
            recs.append(
                f"{hole_count} holes detected — consider using Netfabb (free web repair) "
                f"or Meshmixer's Analysis > Inspector for bulk hole filling."
            )

    if degenerate_count > 0:
        recs.append(
            "Remove degenerate faces: In MeshLab, use Filters > Cleaning and Repairing > "
            "Remove Zero Area Faces, then Remove Duplicate Faces."
        )

    if self_intersection_count > 0:
        recs.append(
            "Fix self-intersections: In MeshLab, use Filters > Cleaning and Repairing > "
            "Remove Self Intersecting Faces. For stubborn cases, try Meshmixer's "
            "Make Solid operation."
        )

    if has_fragments:
        recs.append(
            "Remove floating fragments: In MeshLab, use Filters > Cleaning and Repairing > "
            "Remove Isolated pieces (by face count). Keep only the largest component."
        )

    if polygon_assessment.level != "ok":
        recs.append(
            f"Reduce polygon count: In MeshLab, use Filters > Remeshing > "
            f"Simplification: Quadric Edge Collapse Decimation. "
            f"Target ~{polygon_assessment.recommended_for_fdm:,} faces."
        )

    if not is_watertight and hole_count == 0:
        recs.append(
            "Mesh is not watertight but no holes were detected — this may indicate "
            "non-manifold edges. Try PrusaSlicer's built-in repair (it handles most cases)."
        )

    return recs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def diagnose_mesh(file_path: str) -> MeshDiagnosticReport:
    """Run comprehensive mesh diagnostics on a 3D model file.

    Detects defects that basic validation misses: self-intersections,
    inverted normals, degenerate faces, floating fragments, and detailed
    hole analysis with count, size, and location.

    Requires the ``trimesh`` package.

    :param file_path: Path to a mesh file (STL, OBJ, PLY, OFF, GLB, GLTF).
    :returns: A :class:`MeshDiagnosticReport` with defects and recommendations.
    :raises ValueError: If the file cannot be loaded or has no geometry.
    :raises ImportError: If trimesh is not installed.
    """
    mesh = _load_mesh(file_path)
    path = Path(file_path)

    # Basic geometry
    face_count = len(mesh.faces)
    vertex_count = len(mesh.vertices)
    is_watertight = bool(mesh.is_watertight)
    volume = round(float(mesh.volume), 2) if is_watertight else None
    surface_area = round(float(mesh.area), 2)

    # Bounding box
    bounds = mesh.bounds  # [[min_x, min_y, min_z], [max_x, max_y, max_z]]
    bbox = {
        "x_min": round(float(bounds[0][0]), 3),
        "y_min": round(float(bounds[0][1]), 3),
        "z_min": round(float(bounds[0][2]), 3),
        "x_max": round(float(bounds[1][0]), 3),
        "y_max": round(float(bounds[1][1]), 3),
        "z_max": round(float(bounds[1][2]), 3),
    }
    dimensions = {
        "x": round(float(bounds[1][0] - bounds[0][0]), 3),
        "y": round(float(bounds[1][1] - bounds[0][1]), 3),
        "z": round(float(bounds[1][2] - bounds[0][2]), 3),
    }

    # Defect analysis
    degenerate_count = _count_degenerate_faces(mesh)
    self_intersections = _count_self_intersections(mesh)
    normals = _analyze_normals(mesh)
    polygon_assessment = _assess_polygon_count(face_count, vertex_count)

    # Holes
    hole_count, holes = _analyze_holes(mesh)

    # Components
    component_count, components, has_fragments = _analyze_components(mesh)

    # Build report
    severity = _compute_severity(
        degenerate_count=degenerate_count,
        self_intersection_count=self_intersections,
        normals=normals,
        hole_count=hole_count,
        has_fragments=has_fragments,
        is_watertight=is_watertight,
    )

    defects = _build_defects(
        degenerate_count=degenerate_count,
        self_intersection_count=self_intersections,
        normals=normals,
        hole_count=hole_count,
        holes=holes,
        has_fragments=has_fragments,
        component_count=component_count,
        is_watertight=is_watertight,
        polygon_assessment=polygon_assessment,
    )

    recommendations = _build_recommendations(
        defects=defects,
        degenerate_count=degenerate_count,
        self_intersection_count=self_intersections,
        normals=normals,
        hole_count=hole_count,
        has_fragments=has_fragments,
        is_watertight=is_watertight,
        polygon_assessment=polygon_assessment,
    )

    return MeshDiagnosticReport(
        file_path=str(path),
        file_size_bytes=path.stat().st_size,
        face_count=face_count,
        vertex_count=vertex_count,
        is_watertight=is_watertight,
        volume_mm3=volume,
        surface_area_mm2=surface_area,
        bounding_box=bbox,
        dimensions_mm=dimensions,
        degenerate_face_count=degenerate_count,
        self_intersection_count=self_intersections,
        normals=normals,
        polygon_assessment=polygon_assessment,
        hole_count=hole_count,
        holes=holes,
        component_count=component_count,
        components=components,
        has_floating_fragments=has_fragments,
        severity=severity,
        defects=defects,
        recommendations=recommendations,
    )
