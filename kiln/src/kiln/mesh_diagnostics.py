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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Every mesh container trimesh can hand back as a single body.  3MF belongs
# here for a plain reason: this tool's own advice is to run it BEFORE slicing,
# and 3MF is one of the two formats slicers primarily eat — so the checkup
# refusing the file people are about to print was the one gap that made the
# advice unfollowable.  A 3MF carrying several objects concatenates into one
# body on load (``force="mesh"``), which is the right reading for diagnostics:
# the question is whether the PLATE is printable, and a defect is a defect
# whichever object on it holds the defect.
_SUPPORTED_EXTENSIONS = frozenset(
    {".stl", ".obj", ".ply", ".off", ".glb", ".gltf", ".3mf"}
)

# Degenerate face: area below this fraction of the part's squared diagonal is
# considered zero-area -- 1e-10 mm2 on a 100 mm part, the same in any unit, the
# fraction the repair passes use (kiln.generation.validation).
_DEGENERATE_AREA_FRACTION = 1e-14

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

    # A substantial secondary component sitting within a fraction of a
    # millimeter of the main body: an attachment that never fused, NOT
    # debris to delete.  Telling the user to "remove isolated pieces"
    # here would delete their handle (2026-08-29 audit).
    detached_attachment: bool = False

    # Findings that are not defects, said so the reader does not mistake
    # them for one: separate pieces sitting on each other.
    notes: list[str] = field(default_factory=list)

    # The pieces themselves (kiln.mesh_bodies): how many, which are sealed,
    # and which touch.  The same reading every door that counts pieces uses.
    pieces: dict[str, Any] = field(default_factory=dict)

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
    _require_trimesh()  # raises with an install hint when trimesh is missing
    from kiln.mesh_frame import load_mesh

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
    loaded = load_mesh(str(path), force="mesh", process=False)

    # A load with force="mesh" returns a Trimesh for single-mesh
    # files and concatenates scenes.  Verify we got usable geometry.
    if not hasattr(loaded, "faces") or len(loaded.faces) == 0:
        raise ValueError("File contains no mesh geometry.")

    # Join the corners the file wrote more than once (an STL repeats every
    # corner per triangle; a glTF or OBJ splits corners along its texture
    # seams).  Without this, edge analysis sees every edge as boundary and
    # the mesh appears non-watertight.  The radius is a fraction of the
    # part's own size (kiln.mesh_bodies), so a model reads the same whether
    # it was written in meters or millimeters; a fixed length welded the
    # meter spelling of a model differently from its millimeter spelling.
    # This does NOT fix normals or remove degenerate faces.
    import trimesh  # type: ignore[import-untyped]

    from kiln.mesh_bodies import weld

    vertices, faces, _tol = weld(loaded.vertices, loaded.faces)
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


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


def _analyze_components(
    mesh: Any, bodies: Any,
) -> tuple[int, list[ComponentInfo], bool]:
    """The mesh's separate pieces, from :mod:`kiln.mesh_bodies`.

    One reading for every door that counts pieces.  It used to be a walk of
    trimesh's face adjacency, which leaves out every edge more than two
    triangles share: two pieces laid on each other (a coat on a body) were
    cut into hundreds of fragments along their contact, and how many
    depended on the unit the file was written in.

    Returns (count, up to 20 ComponentInfo largest first, more than one
    piece).  Whether any piece is scrap is the fusion check's to say.
    """
    import numpy as np  # type: ignore[import-untyped]

    count = bodies.piece_count
    if count == 0:
        return 0, [], False
    # Volume per piece from the signed volume of its triangles, kept only
    # for a piece that is a closed solid (an open one encloses nothing).
    tris = np.asarray(mesh.triangles, dtype=np.float64)
    signed = np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])) / 6.0
    labels = bodies.face_piece
    live = labels >= 0
    volumes = np.zeros(count)
    np.add.at(volumes, labels[live], signed[live])
    components = [
        ComponentInfo(
            face_count=piece.faces,
            volume_mm3=round(abs(float(volumes[i])), 2) if piece.sealed else 0.0,
            is_largest=i == 0,
        )
        for i, piece in enumerate(bodies.pieces[:20])
    ]
    return count, components, count > 1


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
                # A face with no area has no side to face, so its winding
                # says nothing about its neighbour's: counted here, every
                # zero-area sliver of a bevel read as an inverted normal
                # too, one defect said twice (measured 2026-10-23 on a
                # Blender donut: all 960 "inverted" faces were beside its
                # 1,920 zero-area ones).  The degenerate count says them.
                flat = _flat_faces(mesh)

                # For each adjacent pair, check if the shared edge has
                # consistent winding (appears reversed in the two faces).
                inconsistent = 0
                for i in range(len(adj)):
                    f1_idx, f2_idx = adj[i]
                    if flat[f1_idx] or flat[f2_idx]:
                        continue
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


def _flat_faces(mesh: Any) -> Any:
    """Which faces have no area to speak of, for the part's own size."""
    import numpy as np  # type: ignore[import-untyped]

    diagonal_sq = float(np.square(np.asarray(mesh.extents, dtype=float)).sum())
    return np.asarray(mesh.area_faces) < _DEGENERATE_AREA_FRACTION * diagonal_sq


def _count_degenerate_faces(mesh: Any) -> int:
    """Count faces with near-zero area (degenerate triangles)."""
    try:
        return int(_flat_faces(mesh).sum())
    except Exception:
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


def _fusion_report(file_path: str) -> dict[str, Any] | None:
    """The fusion check's reading of the pieces (:mod:`kiln.fusion_check`):
    which float, which only meet along a line, and which make one solid.
    ``None`` when it could not run."""
    try:
        from kiln.fusion_check import check_fusion

        report = check_fusion(file_path)
    except Exception as exc:  # noqa: BLE001 — diagnostics must not crash
        logger.debug("fusion reading skipped: %s", exc)
        return None
    return report if report.get("checked") else None


def _detect_detached_attachment(
    components: list[ComponentInfo],
    has_fragments: bool,
    fusion: dict[str, Any] | None,
) -> bool:
    """Is a secondary component an unfused ATTACHMENT rather than debris?

    Two signals together: a non-largest component that is substantial
    (not a support sliver or internal fragment), and a body pair that
    TOUCHES — a gap no printer can resolve (the fusion check's exact
    measurement).  Either alone stays classified as ordinary floating
    fragments; both together mean "remove the isolated piece" would
    delete part of the user's model.

    Deliberately narrower than the fusion check's ``near`` band, which
    also covers designed clearances: a print-in-place hinge sits a few
    tenths of a millimeter from its housing on purpose, and telling
    someone to fuse it would destroy the design.  A pair at ~0mm cannot
    be a designed clearance — nothing prints at that gap — so this is
    the one relation that carries fix-it advice unqualified.
    """
    if not has_fragments or len(components) < 2:
        return False
    # Substantial by VOLUME first (a coarse primitive host can have very
    # few faces), face count as the fallback for unclosed components.
    vols = sorted((c.volume_mm3 or 0.0 for c in components), reverse=True)
    faces = sorted((c.face_count for c in components), reverse=True)
    substantial = (
        vols[1] >= max(25.0, 0.005 * vols[0])
        or faces[1] >= max(50, 0.03 * faces[0])
    )
    if not substantial or fusion is None:
        return False
    # The touching pair has to be the substantial one: a sprinkle skimming
    # the icing of a donut is not the icing coming off it.
    floor = max(25.0, 0.005 * vols[0])
    return any(
        f.get("relation") == "touching"
        and min(f.get("volumes_mm3") or [floor]) >= floor
        for f in fusion.get("findings", [])
    )


def _units_defect(dimensions: dict[str, float]) -> str:
    """The units question a size under anything a printer makes raises,
    in the one judgement every door uses
    (:func:`kiln.generation.validation.unit_verdict`), or ``""``."""
    from kiln.generation.validation import unit_verdict

    largest = max((float(v) for v in dimensions.values()), default=0.0)
    verdict = unit_verdict(largest)
    return verdict.describe_unchanged() if verdict.below_printable else ""


def _compute_severity(
    *,
    units_unsettled: bool = False,
    degenerate_count: int,
    self_intersection_count: int,
    normals: NormalsReport,
    hole_count: int,
    has_fragments: bool,
    is_watertight: bool,
    detached_attachment: bool = False,
) -> str:
    """Compute overall severity level."""
    issues = 0

    if units_unsettled:
        # Smaller than anything a printer makes: nothing else in the report
        # means what it says until the units are settled.
        issues += 3
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
    if detached_attachment:
        # Part of the MODEL is unfused — the object prints in pieces and
        # the junction the user drew does not exist.  Worse than stray
        # debris (which prints fine once removed), so this alone must
        # clear "minor": a handle that falls off is not a cosmetic note.
        issues += 3
    elif has_fragments:
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
    units: str = "",
    bodies: Any = None,
    degenerate_count: int,
    self_intersection_count: int,
    normals: NormalsReport,
    hole_count: int,
    holes: list[HoleInfo],
    has_fragments: bool,
    component_count: int,
    is_watertight: bool,
    polygon_assessment: PolygonCountAssessment,
    detached_attachment: bool = False,
    floating: int = 0,
) -> list[str]:
    """Build a list of human-readable defect descriptions."""
    defects: list[str] = []

    if units:
        defects.append(units)

    if not is_watertight:
        if bodies is not None and bodies.open_edges == 0:
            defects.append(
                f"{bodies.unsealed_count} of {bodies.piece_count} piece(s) are not "
                "sealed solids: a piece meets itself along a line."
            )
        else:
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
        where = ""
        if bodies is not None and bodies.pinch_at:
            where = " (for example at " + ", ".join(
                "(" + ", ".join(f"{c:g}" for c in p) + ")" for p in bodies.pinch_at
            ) + ")"
        defects.append(
            f"{self_intersection_count} pinched edge(s): a piece meets itself "
            f"along a line, with 3 or more triangles on one edge{where} — "
            f"may cause unpredictable slicer behavior."
        )

    if detached_attachment:
        defects.append(
            f"{component_count} disconnected components found — a substantial "
            "component meets the main body only along a line or at a point. "
            "It reads as an attachment that never fused: it shares no "
            "material, so a slicer joins it by a sliver at most."
        )
    if has_fragments:
        defects.append(
            f"{floating or component_count - 1} floating fragment(s): pieces "
            "with nothing under them that join nothing, which print as loose "
            "scrap or not at all."
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
    detached_attachment: bool = False,
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
            "Fix pinched edges: they are not holes, so hole filling cannot touch "
            "them. In MeshLab, use Filters > Cleaning and Repairing > Repair "
            "non Manifold Edges; for stubborn cases, try Meshmixer's Make Solid "
            "operation, or rebuild the surface where it was made."
        )

    if detached_attachment:
        recs.append(
            "Fuse the detached attachment: sink it at least 0.4mm into the "
            "main body and re-compose — a boolean union of parts that merely "
            "touch does not weld them. Only if the extra piece is genuinely "
            "leftover support or debris, remove it instead (MeshLab: "
            "Filters > Cleaning and Repairing > Remove Isolated pieces)."
        )
    if has_fragments:
        recs.append(
            "Remove floating fragments: In MeshLab, use Filters > Cleaning and Repairing > "
            "Remove Isolated pieces (by face count). If a piece belongs to the part, "
            "extend it until it overlaps the part instead."
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

    :param file_path: Path to a mesh file (STL, 3MF, OBJ, PLY, OFF, GLB,
        GLTF).  A multi-object 3MF is read as one body — see
        :data:`_SUPPORTED_EXTENSIONS`.
    :returns: A :class:`MeshDiagnosticReport` with defects and recommendations.
    :raises ValueError: If the file cannot be loaded or has no geometry.
    :raises ImportError: If trimesh is not installed.
    """
    mesh = _load_mesh(file_path)
    path = Path(file_path)

    # Pieces first: whether the mesh is closed is a question about each
    # piece, and two pieces sitting on each other are each still closed.
    from kiln.mesh_bodies import read_mesh

    bodies = read_mesh(mesh, tolerance=0.0)  # already welded on load

    # Basic geometry
    face_count = len(mesh.faces)
    vertex_count = len(mesh.vertices)
    is_watertight = bodies.sealed
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

    # Defect analysis.  The units question is read off the unrounded size.
    units_note = _units_defect(
        {axis: float(bounds[1][i] - bounds[0][i]) for i, axis in enumerate("xyz")}
    )
    degenerate_count = _count_degenerate_faces(mesh)
    # Edges where a piece meets itself.  This field used to count every
    # edge more than two triangles share and call each a self-intersection,
    # which read two pieces in contact as thousands of defects of one.
    self_intersections = bodies.pinch_edges
    normals = _analyze_normals(mesh)
    polygon_assessment = _assess_polygon_count(face_count, vertex_count)

    # Holes
    hole_count, holes = _analyze_holes(mesh)

    # Components.  More than one piece is not a defect: pieces that share a
    # surface or sink into one another print as one solid, and pieces on the
    # bed are a plate.  What is scrap is a piece in the air that joins
    # nothing, which the fusion check measures (once, for both questions).
    component_count, components, multi_piece = _analyze_components(mesh, bodies)
    fusion = _fusion_report(file_path) if multi_piece else None
    floating = int(fusion.get("floating_bodies") or 0) if fusion is not None else 0
    has_fragments = bool(floating) if fusion is not None else multi_piece
    detached_attachment = _detect_detached_attachment(components, multi_piece, fusion)

    # Build report
    severity = _compute_severity(
        units_unsettled=bool(units_note),
        degenerate_count=degenerate_count,
        self_intersection_count=self_intersections,
        normals=normals,
        hole_count=hole_count,
        has_fragments=has_fragments,
        is_watertight=is_watertight,
        detached_attachment=detached_attachment,
    )

    defects = _build_defects(
        units=units_note,
        bodies=bodies,
        degenerate_count=degenerate_count,
        self_intersection_count=self_intersections,
        normals=normals,
        hole_count=hole_count,
        holes=holes,
        has_fragments=has_fragments,
        component_count=component_count,
        is_watertight=is_watertight,
        polygon_assessment=polygon_assessment,
        detached_attachment=detached_attachment,
        floating=floating,
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
        detached_attachment=detached_attachment,
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
        detached_attachment=detached_attachment,
        notes=[
            *(line for line in bodies.describe() if "sit on each other" in line),
            *([bodies.solids_sentence()] if bodies.solids_sentence() else []),
        ],
        pieces=bodies.to_dict(),
    )
