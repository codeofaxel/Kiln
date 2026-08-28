"""Track which mesh faces a decoration carve actually created.

The deboss/emboss pipeline constructs the cut geometry, so at compile
time it knows exactly which output triangles exist only because of the
decoration (recess floors and side-walls, or raised emboss geometry).
Until now that knowledge was discarded the moment the boolean finished,
and painting had to re-derive it by geometric guessing — crease-angle
segmentation cannot tell a recess FLOOR from an untouched surface ISLAND
enclosed by the same crease edges, which is how a debossed logo with a
closed outline gets "filled in" when its region is painted.

This module records the answer instead of guessing it later:

``compute_decoration_faces``
    Diff the decorated mesh against the pre-carve mesh.  Two passes:

    1. *Hash pass* — triangles preserved verbatim by the boolean (the
       Manifold backend keeps every non-intersected input triangle
       bit-identical; CGAL jitters in the last decimals, which the
       rounding tolerance absorbs) are original surface, full stop.
    2. *Distance pass* — a triangle that is new-by-hash is either real
       carve geometry or an original-surface fragment the boolean merely
       re-triangulated around the cut.  Fragments still LIE ON the old
       surface, so the centroid's distance to the pre-carve mesh decides:
       on-surface (≤ eps) → fragment, off-surface (> eps) → decoration.

    The distance pass is what protects enclosed islands: an untouched
    patch of wall surrounded by carved strokes sits at distance ~0 and is
    never claimed as decoration, no matter how the boolean re-triangulated
    it.

``record_decoration_faces``
    Compute the diff and persist it in a ``<mesh>.decoration_faces.json``
    sidecar next to the decorated mesh, keyed to the mesh file's sha256 so
    staleness is detectable.  Best-effort by design: it must never break a
    carve that already succeeded.

``load_decoration_faces``
    Read a sidecar back and verify the mesh hash.  A mismatch is a loud
    refusal — painting the wrong faces silently is the exact failure this
    module exists to end.

``record_paint_event``
    Painting's write-back: after the recorded faces are painted, the
    sidecar gains a ``painted`` block (color, target, output 3MF), so the
    record tells the whole story — carved, then painted what color.

The single caller is :func:`kiln.emboss_generator.compile_embossed_model`,
the one chokepoint every decoration door in both repos already funnels
through (``decorate_surface``, ``apply_decoration``, preset apply,
``smart_decorate``, ``batch_decorate``, wall text, procedural-texture
carves).  Hooking there means every door records face provenance via ONE
shared helper — no per-door branches to drift.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: Sidecar schema version — bump on breaking layout changes.
SCHEMA_VERSION = 1

#: Suffix appended to the decorated mesh path to form the sidecar path.
SIDECAR_SUFFIX = ".decoration_faces.json"

#: Vertex-coordinate rounding (decimal places) for the hash pass.  1e-4 mm
#: is far below FDM resolution and absorbs the CGAL backend's last-decimal
#: float jitter (measured: at 4 decimals CGAL matches Manifold's preserved
#: set; at 5-6 decimals CGAL marks everything new).
HASH_DECIMALS = 4

#: A new-by-hash triangle whose centroid is farther than this from the
#: pre-carve surface is decoration geometry; nearer is a re-triangulation
#: fragment of the original surface.  Measured on a 64-facet cylinder
#: deboss: fragments sit at ~0 mm, real carve faces at ≥ 0.49 mm — the
#: separation is bimodal, so the exact value is not delicate.
DISTANCE_EPS_MM = 0.05

#: Skip recording above this many output triangles: the diff is O(n) but a
#: procedural-texture carve can produce meshes where even O(n) numpy work
#: plus a JSON sidecar of face indices stops being free.  Skipping is
#: logged and the carve is untouched.
MAX_TRACKED_FACES = 400_000

#: Voxel edge for the distance pass's spatial prefilter, in mm.  Points
#: only test triangles binned within one cell of their own, so distances
#: are exact up to this radius — far above ``DISTANCE_EPS_MM``, which is
#: all the classification compares against.
_GRID_CELL_MM = 2.0

#: Floors vs walls: a decoration face whose normal is within ~45 deg of
#: (anti)parallel to the decorated face's normal is a floor (or emboss
#: cap); the rest are side-walls.
_FLOOR_NORMAL_DOT = 0.7071

#: Env kill switch — set KILN_DECORATION_FACE_TRACKING=0/off/false to
#: disable recording entirely (diagnostic escape hatch, not a config).
_ENV_SWITCH = "KILN_DECORATION_FACE_TRACKING"


def tracking_enabled() -> bool:
    """False when the env kill switch disables face tracking."""
    return os.environ.get(_ENV_SWITCH, "1").strip().lower() not in (
        "0", "off", "false", "no",
    )


def sidecar_path_for(mesh_path: str) -> str:
    """The sidecar path for a decorated mesh (``<mesh>.decoration_faces.json``)."""
    return mesh_path + SIDECAR_SUFFIX


def mesh_sha256(mesh_path: str) -> str:
    """Content hash of the mesh file, streamed."""
    h = hashlib.sha256()
    with open(mesh_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_mesh_triangles(mesh_path: str) -> np.ndarray:
    """Load a mesh as an ``(n, 3, 3)`` float64 triangle array in file order.

    ``process=False`` keeps trimesh from merging vertices or dropping
    degenerate triangles, so index *i* here is triangle *i* of the file —
    the invariant that lets a sidecar's face indices address the mesh, and
    that the painting door relies on when it hands the same triangles to
    ``compose_painted_3mf``.
    """
    import trimesh

    mesh = trimesh.load(mesh_path, force="mesh", process=False)
    return np.asarray(mesh.triangles, dtype=np.float64)


def _triangle_keys(triangles: np.ndarray, decimals: int = HASH_DECIMALS) -> list[bytes]:
    """Order-independent per-triangle geometric keys.

    Vertices are rounded, then sorted within each triangle, so a triangle
    survives vertex-rotation and tiny float jitter but any real geometric
    change produces a different key.
    """
    v = np.round(np.asarray(triangles, dtype=np.float64), decimals)
    # +0.0 folds -0.0 into +0.0 so the two round-trip to identical bytes.
    v = v + 0.0
    order = np.lexsort((v[:, :, 2], v[:, :, 1], v[:, :, 0]), axis=1)
    v_sorted = np.take_along_axis(v, order[:, :, None], axis=1)
    flat = np.ascontiguousarray(v_sorted.reshape(-1, 9))
    return [row.tobytes() for row in flat]


def _point_triangle_distances(points: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Exact distance from ``points[i]`` to ``tris[i]`` (paired), vectorized.

    Standard closest-point-on-triangle region decomposition (Ericson,
    *Real-Time Collision Detection* §5.1.5) done in numpy across all pairs
    at once.
    """
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    p = points
    ab, ac, ap = b - a, c - a, p - a
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)

    nearest = np.empty_like(p)
    done = np.zeros(len(p), dtype=bool)

    m = (d1 <= 0) & (d2 <= 0)
    nearest[m] = a[m]
    done |= m
    m = (~done) & (d3 >= 0) & (d4 <= d3)
    nearest[m] = b[m]
    done |= m
    m = (~done) & (d6 >= 0) & (d5 <= d6)
    nearest[m] = c[m]
    done |= m

    vc = d1 * d4 - d3 * d2
    m = (~done) & (vc <= 0) & (d1 >= 0) & (d3 <= 0)
    denom = np.where(d1 - d3 == 0, 1.0, d1 - d3)
    t = d1 / denom
    nearest[m] = a[m] + t[m, None] * ab[m]
    done |= m

    vb = d5 * d2 - d1 * d6
    m = (~done) & (vb <= 0) & (d2 >= 0) & (d6 <= 0)
    denom = np.where(d2 - d6 == 0, 1.0, d2 - d6)
    t = d2 / denom
    nearest[m] = a[m] + t[m, None] * ac[m]
    done |= m

    va = d3 * d6 - d5 * d4
    m = (~done) & (va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0)
    denom = (d4 - d3) + (d5 - d6)
    denom = np.where(denom == 0, 1.0, denom)
    t = (d4 - d3) / denom
    nearest[m] = b[m] + t[m, None] * (c[m] - b[m])
    done |= m

    m = ~done
    denom = np.where(va + vb + vc == 0, 1.0, va + vb + vc)
    v = (vb / denom)[m]
    w = (vc / denom)[m]
    nearest[m] = a[m] + v[:, None] * ab[m] + w[:, None] * ac[m]
    return np.linalg.norm(p - nearest, axis=1)


def _min_distance_to_mesh(
    points: np.ndarray,
    triangles: np.ndarray,
    *,
    cell: float = _GRID_CELL_MM,
) -> np.ndarray:
    """Min distance from each point to any mesh triangle, voxel-prefiltered.

    Triangles are binned into every grid cell their bounding box touches;
    each point tests only triangles from its 3x3x3 cell neighborhood.
    Exact for distances up to ``cell``; a point with no nearby candidate
    reports ``inf`` — unambiguously off-surface, which is all the caller's
    epsilon comparison needs.
    """
    lo = triangles.min(axis=(0, 1)) - 1e-9
    tri_lo = np.floor((triangles.min(axis=1) - lo) / cell).astype(np.int64)
    tri_hi = np.floor((triangles.max(axis=1) - lo) / cell).astype(np.int64)
    grid: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for i in range(len(triangles)):
        l3, h3 = tri_lo[i], tri_hi[i]
        for x in range(l3[0], h3[0] + 1):
            for y in range(l3[1], h3[1] + 1):
                for z in range(l3[2], h3[2] + 1):
                    grid[(x, y, z)].append(i)

    pt_cell = np.floor((points - lo) / cell).astype(np.int64)
    out = np.full(len(points), np.inf)
    pair_points: list[np.ndarray] = []
    pair_tris: list[np.ndarray] = []
    for pi in range(len(points)):
        px, py, pz = pt_cell[pi]
        cands: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    cands.extend(grid.get((px + dx, py + dy, pz + dz), ()))
        if cands:
            uniq = np.unique(np.asarray(cands, dtype=np.int64))
            pair_points.append(np.full(len(uniq), pi, dtype=np.int64))
            pair_tris.append(uniq)
    if pair_points:
        pp = np.concatenate(pair_points)
        tt = np.concatenate(pair_tris)
        d = _point_triangle_distances(points[pp], triangles[tt])
        np.minimum.at(out, pp, d)
    return out


def _triangle_normals(triangles: np.ndarray) -> np.ndarray:
    """Unit normals per triangle (zero vector for degenerates)."""
    n = np.cross(
        triangles[:, 1] - triangles[:, 0],
        triangles[:, 2] - triangles[:, 0],
    )
    length = np.linalg.norm(n, axis=1, keepdims=True)
    length = np.where(length == 0, 1.0, length)
    return n / length


def _triangle_areas(triangles: np.ndarray) -> np.ndarray:
    n = np.cross(
        triangles[:, 1] - triangles[:, 0],
        triangles[:, 2] - triangles[:, 0],
    )
    return 0.5 * np.linalg.norm(n, axis=1)


def compute_decoration_faces(
    original_mesh_path: str,
    decorated_mesh_path: str,
    *,
    face_normal: list[float] | tuple[float, float, float] | None = None,
    distance_eps: float = DISTANCE_EPS_MM,
    decimals: int = HASH_DECIMALS,
) -> dict[str, Any]:
    """Identify the decorated mesh's triangles that the carve created.

    :param original_mesh_path: The pre-carve mesh the boolean imported.
    :param decorated_mesh_path: The boolean's output mesh.
    When the pre-carve mesh itself carries a valid face sidecar (it was
    already decorated), the prior carve's faces are carried forward into
    this result: preserved-verbatim prior faces are remapped by triangle
    hash, and prior-carve geometry the new boolean merely re-triangulated
    — new by hash but lying ON the prior decoration — is rescued by
    distance.  Without the carry, only the LAST carve of a chain (a
    two-line nameplate, a logo plus a border) survives in the final
    record, and "paint everything I carved" paints half the plate while
    reporting success.

    :param face_normal: Normal of the decorated face, when the caller
        knows it.  Enables the floors/walls split: decoration faces whose
        normal is near-(anti)parallel to it are floors (or emboss caps),
        the rest side-walls.
    :param distance_eps: On/off-surface threshold in mm.
    :param decimals: Rounding for the hash pass.
    :returns: ``{"face_indices": [...], "floor_indices": [...],
        "wall_indices": [...], "triangle_count": n, "stats": {...}}`` —
        indices address the decorated mesh's triangles in file order.
        ``face_indices`` is the union of every carve in the chain; when
        faces were carried forward, ``prior_decorations`` lists the
        earlier steps.  Raises on unreadable meshes; never mutates either
        file.
    """
    t0 = time.monotonic()
    original = load_mesh_triangles(original_mesh_path)
    decorated = load_mesh_triangles(decorated_mesh_path)
    if len(original) == 0 or len(decorated) == 0:
        raise ValueError(
            f"empty mesh: original={len(original)} decorated={len(decorated)} triangles"
        )

    original_keys = _triangle_keys(original, decimals)
    original_key_set = set(original_keys)
    decorated_keys = _triangle_keys(decorated, decimals)
    new_idx = np.asarray(
        [i for i, key in enumerate(decorated_keys) if key not in original_key_set],
        dtype=np.int64,
    )

    if len(new_idx) == 0:
        face_indices = np.asarray([], dtype=np.int64)
        fragment_idx = np.asarray([], dtype=np.int64)
    else:
        centroids = decorated[new_idx].mean(axis=1)
        distances = _min_distance_to_mesh(centroids, original)
        face_indices = new_idx[distances > distance_eps]
        fragment_idx = new_idx[distances <= distance_eps]

    carried = _carry_forward_prior_faces(
        original_mesh_path,
        original,
        original_keys,
        decorated_keys,
        decorated,
        fragment_idx,
        distance_eps=distance_eps,
    )

    own_count = int(len(face_indices))
    own_face_indices = face_indices
    if carried is not None and len(carried["face_indices"]):
        face_indices = np.unique(
            np.concatenate([face_indices, carried["face_indices"]])
        )

    floor_indices: np.ndarray | None = None
    wall_indices: np.ndarray | None = None
    if face_normal is not None and len(face_indices):
        fn = np.asarray(face_normal, dtype=np.float64)
        norm = np.linalg.norm(fn)
        if norm > 0:
            fn = fn / norm
            dots = np.abs(_triangle_normals(decorated[face_indices]) @ fn)
            floor_mask = dots >= _FLOOR_NORMAL_DOT
            floor_indices = face_indices[floor_mask]
            wall_indices = face_indices[~floor_mask]
    if (
        carried is not None
        and len(carried["face_indices"])
        and not carried["prior_had_split"]
        and floor_indices is not None
    ):
        # The prior step recorded no floors/walls split, so a split here
        # would silently omit the prior carve's floors from a "floors"
        # paint — the half-paint bug one target deeper.  Omit it.
        floor_indices = None
        wall_indices = None

    result: dict[str, Any] = {
        "face_indices": face_indices.tolist(),
        "triangle_count": int(len(decorated)),
        "stats": {
            "original_triangles": int(len(original)),
            "new_by_hash": int(len(new_idx)),
            "resurfaced_fragments": int(
                len(new_idx) - own_count
                - (carried["rescued_count"] if carried is not None else 0)
            ),
            "decoration_area_mm2": round(
                float(_triangle_areas(decorated[face_indices]).sum()), 3
            ) if len(face_indices) else 0.0,
            "distance_eps_mm": distance_eps,
            "compute_seconds": round(time.monotonic() - t0, 3),
        },
    }
    if carried is not None:
        result["stats"]["carried_forward"] = int(len(carried["face_indices"]))
        result["stats"]["own_faces"] = own_count
        result["prior_decorations"] = carried["prior_decorations"]
        result["own_face_indices"] = own_face_indices.tolist()
    if floor_indices is not None:
        result["floor_indices"] = floor_indices.tolist()
        result["wall_indices"] = wall_indices.tolist()
    return result


def _carry_forward_prior_faces(
    original_mesh_path: str,
    original: np.ndarray,
    original_keys: list[bytes],
    decorated_keys: list[bytes],
    decorated: np.ndarray,
    fragment_idx: np.ndarray,
    *,
    distance_eps: float,
) -> dict[str, Any] | None:
    """Map an earlier carve's recorded faces into the newly carved mesh.

    ``None`` when the pre-carve mesh has no valid sidecar (single-step
    carve — the common case, zero extra work).  Otherwise a dict with the
    prior faces addressed in the NEW mesh's index space:

    - *Hash remap*: prior decoration triangles the new boolean preserved
      verbatim match by the same geometric key the diff's hash pass uses.
    - *Fragment rescue*: new-by-hash triangles that lie ON the pre-carve
      surface were classified as re-triangulation fragments — but a
      fragment within ``distance_eps`` of the PRIOR DECORATION's own
      triangles is prior-carve geometry the boolean re-cut, not original
      surface.  Without the rescue those faces vanish from both records.
    """
    prior, _err = load_decoration_faces(original_mesh_path)
    if prior is None or not prior.get("face_indices"):
        return None
    prior_faces = np.asarray(prior["face_indices"], dtype=np.int64)
    prior_faces = prior_faces[(prior_faces >= 0) & (prior_faces < len(original))]
    if len(prior_faces) == 0:
        return None

    decorated_idx_by_key: dict[bytes, list[int]] = defaultdict(list)
    for i, key in enumerate(decorated_keys):
        decorated_idx_by_key[key].append(i)

    def _remap(indices: np.ndarray) -> np.ndarray:
        return np.asarray(
            [
                j
                for fi in indices
                for j in decorated_idx_by_key.get(original_keys[fi], ())
            ],
            dtype=np.int64,
        )

    # Per-step history: a prior record that was itself a chained carve
    # lists its steps (each with its own face set); a single-step record
    # contributes one.  Steps without per-step faces (older chained
    # records) collapse into a single step over the prior union.
    prior_chain = prior.get("decorations")
    if (
        isinstance(prior_chain, list)
        and prior_chain
        and all(isinstance(s.get("face_indices"), list) for s in prior_chain)
    ):
        step_sources = [
            (
                s,
                np.asarray(s["face_indices"], dtype=np.int64),
            )
            for s in prior_chain
        ]
    else:
        step_sources = [
            (
                {
                    "decoration": prior.get("decoration") or {},
                    "recorded_unix": (prior.get("_meta") or {}).get(
                        "created_unix"
                    ),
                },
                prior_faces,
            )
        ]

    rescued = np.asarray([], dtype=np.int64)
    rescue_step = np.asarray([], dtype=np.int64)
    if len(fragment_idx):
        frag_centroids = decorated[fragment_idx].mean(axis=1)
        d_prior = _min_distance_to_mesh(frag_centroids, original[prior_faces])
        rescued = fragment_idx[d_prior <= distance_eps]
        if len(rescued):
            # Attribute each rescued fragment to the nearest prior step.
            per_step = np.stack(
                [
                    _min_distance_to_mesh(
                        decorated[rescued].mean(axis=1), original[src]
                    )
                    for _meta, src in step_sources
                ]
            )
            rescue_step = per_step.argmin(axis=0)

    prior_steps: list[dict[str, Any]] = []
    carried_sets: list[np.ndarray] = []
    for si, (meta, src_faces) in enumerate(step_sources):
        step_faces = _remap(src_faces)
        if len(rescued):
            step_faces = np.concatenate([step_faces, rescued[rescue_step == si]])
        step_faces = np.unique(step_faces)
        carried_sets.append(step_faces)
        prior_steps.append(
            {
                "decoration": meta.get("decoration") or {},
                "face_count": int(len(step_faces)),
                "recorded_unix": meta.get("recorded_unix"),
                "face_indices": step_faces.tolist(),
            }
        )

    carried = (
        np.unique(np.concatenate(carried_sets))
        if carried_sets
        else np.asarray([], dtype=np.int64)
    )

    return {
        "face_indices": carried,
        "rescued_count": int(len(rescued)),
        "prior_decorations": prior_steps,
        "prior_had_split": isinstance(prior.get("floor_indices"), list),
    }


def record_decoration_faces(
    original_mesh_path: str,
    decorated_mesh_path: str,
    *,
    decoration: dict[str, Any] | None = None,
    face_normal: list[float] | tuple[float, float, float] | None = None,
) -> dict[str, Any] | None:
    """Compute the carve's face set and persist it in a sidecar.

    Best-effort: returns the record on success, ``None`` on any failure
    (logged at debug) — recording provenance must never break a carve
    that already succeeded.  Recording is skipped (also ``None``) when
    the env kill switch is set or the mesh exceeds
    :data:`MAX_TRACKED_FACES`.

    :param decoration: Free-form metadata about what was applied (mode,
        depth, content type…), stored verbatim for provenance.
    """
    if not tracking_enabled():
        return None
    try:
        computed = compute_decoration_faces(
            original_mesh_path,
            decorated_mesh_path,
            face_normal=face_normal,
        )
        if computed["triangle_count"] > MAX_TRACKED_FACES:
            logger.debug(
                "decoration face tracking skipped: %d triangles > cap %d",
                computed["triangle_count"],
                MAX_TRACKED_FACES,
            )
            return None
        record: dict[str, Any] = {
            "_meta": {
                "schema": "kiln.decoration_faces",
                "version": SCHEMA_VERSION,
                "created_unix": int(time.time()),
            },
            "mesh_sha256": mesh_sha256(decorated_mesh_path),
            "source_mesh": os.path.basename(original_mesh_path),
            "decoration": decoration or {},
            **computed,
        }
        prior_steps = record.pop("prior_decorations", None)
        own_faces = record.pop("own_face_indices", None)
        if prior_steps:
            # Chained carve: the record's faces span every step, and the
            # history says which carves contributed — each step keeping
            # its OWN face set (in this mesh's index space) so painting
            # can give every decoration its own color.
            record["decorations"] = [
                *prior_steps,
                {
                    "decoration": decoration or {},
                    "face_count": record["stats"].get(
                        "own_faces", len(record["face_indices"])
                    ),
                    "recorded_unix": record["_meta"]["created_unix"],
                    "face_indices": (
                        own_faces
                        if own_faces is not None
                        else record["face_indices"]
                    ),
                },
            ]
        sidecar = sidecar_path_for(decorated_mesh_path)
        tmp = sidecar + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, separators=(",", ":"))
        os.replace(tmp, sidecar)
        record["sidecar_path"] = sidecar
        return record
    except Exception:
        logger.debug(
            "decoration face tracking failed for %s",
            decorated_mesh_path,
            exc_info=True,
        )
        return None


def record_paint_event(
    decorated_mesh_path: str,
    *,
    color: str,
    target: str,
    output_path: str,
    step_colors: dict[int, str] | None = None,
) -> dict[str, Any] | None:
    """Record that the carve's faces were painted, in the same sidecar.

    Painting reads the face record; this writes the answer back, so the
    provenance chain carries the whole story — carved, then painted what
    color — instead of stopping at the carve.  The block replaces any
    previous one (latest paint wins: repainting while iterating is the
    normal flow, and a history of abandoned colors is noise).

    The sidecar stays keyed to the SOURCE mesh's sha256 — painting writes
    a new 3MF and never touches the mesh it read, so the existing hash
    gate keeps working unchanged.  Best-effort like recording: returns
    the updated record, or ``None`` when there is no valid sidecar to
    annotate (logged at debug, never raised).

    :param decorated_mesh_path: The carved mesh whose sidecar to annotate.
    :param color: Hex color painted onto the carve faces.
    :param target: Which faces were painted (``all``/``floors``/``walls``).
    :param output_path: The painted 3MF that was produced.
    :param step_colors: Per-decoration-step colors, when the paint gave
        each step of a chained carve its own color.
    """
    try:
        record, err = load_decoration_faces(decorated_mesh_path)
        if record is None:
            logger.debug(
                "paint event not recorded for %s: %s", decorated_mesh_path, err
            )
            return None
        painted: dict[str, Any] = {
            "color": color,
            "target": target,
            "output": os.path.basename(output_path),
            "painted_at_unix": int(time.time()),
        }
        if step_colors:
            painted["step_colors"] = {
                str(k): v for k, v in step_colors.items()
            }
        if os.path.isfile(output_path):
            painted["output_sha256"] = mesh_sha256(output_path)
        record["painted"] = painted
        sidecar = sidecar_path_for(decorated_mesh_path)
        tmp = sidecar + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, separators=(",", ":"))
        os.replace(tmp, sidecar)
        return record
    except Exception:
        logger.debug(
            "paint event recording failed for %s",
            decorated_mesh_path,
            exc_info=True,
        )
        return None


def load_decoration_faces(
    decorated_mesh_path: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load and validate the face sidecar for a decorated mesh.

    :returns: ``(record, None)`` when the sidecar exists and matches the
        mesh's current content hash; ``(None, reason)`` otherwise.  A
        hash mismatch is reported as stale — the caller must refuse to
        paint rather than color faces recorded for different geometry.
    """
    sidecar = sidecar_path_for(decorated_mesh_path)
    if not os.path.isfile(decorated_mesh_path):
        return None, f"mesh not found: {decorated_mesh_path}"
    if not os.path.isfile(sidecar):
        return None, (
            "no decoration face record found for this mesh (expected "
            f"{os.path.basename(sidecar)} beside it). Only meshes decorated "
            "since face tracking shipped carry one — re-run the decoration "
            "to record it."
        )
    try:
        with open(sidecar, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"decoration face record unreadable: {exc}"
    if not isinstance(record, dict) or not isinstance(
        record.get("face_indices"), list
    ):
        return None, "decoration face record malformed (no face_indices)"
    stored = record.get("mesh_sha256")
    actual = mesh_sha256(decorated_mesh_path)
    if stored != actual:
        return None, (
            "decoration face record is STALE: the mesh content changed since "
            "the decoration was recorded (sha256 mismatch). Painting these "
            "face indices would color the wrong triangles — re-run the "
            "decoration on the current mesh to refresh the record."
        )
    return record, None
