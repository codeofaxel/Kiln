"""Fusion check — is a composed mesh actually ONE welded body?

An attachment that ends flush against a curved host (a handle ending
exactly on a lathed wall) touches it along a line, so the two shells
share no volume.  Every classical mesh check passes it: a boolean union
leaves it as two disconnected bodies with zero warnings, and two
disjoint closed shells are watertight and edge-manifold.  Visually the
part reads as detached, structurally the junction is a sub-millimeter
air channel, and a slicer prints two separate shells.

This module is the shared detector every composition door calls — one
helper, never a per-door reimplementation.  It never raises and never
blocks on its own; each door decides whether a finding is a refusal
(``attach_part_feature``) or a structured warning (the merge tools,
where multi-body output is often deliberate).

Vocabulary matches the pairwise contact relations used elsewhere in the
stack: ``touching`` (gap at or under ``TOUCH_GAP_MM``), ``near`` (under
``NEAR_GAP_MM`` — an accidental near-miss or a designed clearance),
``separated``.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

# Gap at or under this reads as "touching": half a typical first-layer
# height, i.e. below anything a printer can resolve as a real clearance.
TOUCH_GAP_MM = 0.05
# Under this the gap is either a designed clearance or an authoring
# accident; for a piece that was meant to be fused it is an air channel.
NEAR_GAP_MM = 0.8
# Minimum overlap that makes a weld real: about two extrusion widths.
EMBEDMENT_FLOOR_MM = 0.4
# Surface points sampled per body for the gap measurement.
_GAP_SAMPLES = 1500
# Bound the pairwise gap work on pathological many-body meshes.
_MAX_PAIRS = 8


def embedment_floor_mm(contact_radius_mm: float = 0.0) -> float:
    """How deep an attachment should sink into its host to truly fuse.

    For a rounded attachment (tube of radius r) meeting a curved host,
    sinking the end by depth d turns line contact into a fused band of
    half-width ~sqrt(2*R*d); scaling the floor with the contact radius
    keeps that band proportionate to the attachment.  The absolute
    floor covers thin contacts: two extrusion widths of shared
    material.
    """
    return max(EMBEDMENT_FLOOR_MM, 0.3 * max(contact_radius_mm, 0.0))


def _sample_points(mesh: Any) -> Any:
    """Surface samples plus vertices — vertices catch sharp features
    (a tangent line runs through mesh vertices) that uniform sampling
    can straddle."""
    import numpy as np
    import trimesh

    points = [mesh.vertices]
    try:
        sampled, _fid = trimesh.sample.sample_surface(mesh, _GAP_SAMPLES)
        points.append(sampled)
    except Exception:  # noqa: BLE001 — vertices alone still work
        pass
    pts = np.vstack(points)
    if len(pts) > 2 * _GAP_SAMPLES:
        idx = np.linspace(0, len(pts) - 1, 2 * _GAP_SAMPLES).astype(int)
        pts = pts[idx]
    return pts


def _nearest_centroid_ids(points: Any, centroids: Any, k: int) -> Any:
    """Indices of the ``k`` nearest triangle centroids per point.

    scipy's KD-tree when available; otherwise a chunked brute-force
    numpy pass (scipy is not a base dependency of this package, and the
    check must not silently no-op on a clean install).  Chunk size caps
    the temporary distance matrix at a few million elements.
    """
    import numpy as np

    try:
        from scipy.spatial import cKDTree

        _dist, idx = cKDTree(centroids).query(points, k=k)
        return idx.reshape(len(points), -1)
    except ImportError:
        idx = np.empty((len(points), k), dtype=np.int64)
        chunk = max(1, int(4_000_000 // max(len(centroids), 1)))
        for s in range(0, len(points), chunk):
            d2 = (
                (points[s : s + chunk, None, :] - centroids[None, :, :]) ** 2
            ).sum(axis=2)
            idx[s : s + chunk] = np.argpartition(d2, k - 1, axis=1)[:, :k]
        return idx


def _points_to_surface_min_mm(points: Any, mesh: Any, k: int = 24) -> float:
    """Exact min distance from sample points to a mesh's surface.

    ``k`` candidate triangles per point (nearest centroids), then exact
    point-to-triangle distance on the shortlist — the same shape of
    measurement the assembly contact analysis uses.
    """
    import numpy as np
    from trimesh.triangles import closest_point as _tri_closest

    triangles = mesh.triangles
    centroids = triangles.mean(axis=1)
    k = min(k, len(centroids))
    idx = _nearest_centroid_ids(points, centroids, k)
    flat_tris = triangles[idx.ravel()]
    flat_pts = np.repeat(points, idx.shape[1], axis=0)
    closest = _tri_closest(flat_tris, flat_pts)
    d = np.sqrt(((closest - flat_pts) ** 2).sum(axis=1))
    return float(d.reshape(len(points), -1).min())


def _pair_gap_mm(body_a: Any, body_b: Any) -> float:
    """Smallest surface-to-surface distance between two bodies (mm).

    Symmetric: samples each body's surface (plus vertices) and measures
    exactly against the other body's triangles.
    """
    return min(
        _points_to_surface_min_mm(_sample_points(body_a), body_b),
        _points_to_surface_min_mm(_sample_points(body_b), body_a),
    )


def check_fusion(
    mesh_path: str,
    *,
    expect_single_body: bool | None = None,
    max_pairs: int = _MAX_PAIRS,
) -> dict[str, Any]:
    """Measure whether a composed mesh is one fused body.

    :param mesh_path: Path to the composed mesh (anything trimesh loads).
    :param expect_single_body: ``True`` — the caller promised ONE part,
        so any extra body is a finding regardless of gap.  ``None`` —
        multi-body output may be deliberate (a plate of parts), so only
        ``touching``/``near`` pairs are findings: parts that all but
        touch were almost certainly meant to be one.
    :param max_pairs: Cap on pairwise gap measurements.
    :returns: ``{"checked", "body_count", "fused", "findings":
        [{"bodies", "gap_mm", "relation", "message"}, ...]}`` —
        ``checked: False`` (with ``reason``) when the measurement could
        not run; never a fake pass, never an exception.
    """
    try:
        import trimesh
    except Exception:  # noqa: BLE001 — optional dep
        return {"checked": False, "reason": "trimesh not available"}
    try:
        mesh = trimesh.load(mesh_path, force="mesh")
        if getattr(mesh, "is_empty", False) or len(getattr(mesh, "faces", [])) == 0:
            return {"checked": False, "reason": "mesh has no geometry"}
        bodies = mesh.split(only_watertight=False)
    except Exception as exc:  # noqa: BLE001
        return {"checked": False, "reason": f"mesh could not be loaded: {exc}"}

    result: dict[str, Any] = {
        "checked": True,
        "body_count": len(bodies),
        "fused": len(bodies) <= 1,
        "findings": [],
    }
    if len(bodies) <= 1:
        return result

    # Attachments meet a host: measure each smaller body against the
    # largest first, then adjacent pairs while the budget lasts.
    order = sorted(range(len(bodies)), key=lambda i: -abs(float(bodies[i].area)))
    pairs = [(order[0], i) for i in order[1:]]
    pairs += [(a, b) for a, b in zip(order[1:], order[2:], strict=False)]
    for a, b in pairs[:max_pairs]:
        try:
            gap = _pair_gap_mm(bodies[a], bodies[b])
        except Exception as exc:  # noqa: BLE001
            # An unmeasured gap must not read as "no finding" — say so.
            _logger.debug("fusion gap measurement failed: %s", exc)
            result["gap_unmeasured"] = True
            continue
        relation = (
            "touching" if gap <= TOUCH_GAP_MM
            else "near" if gap <= NEAR_GAP_MM
            else "separated"
        )
        if relation == "separated" and not expect_single_body:
            continue
        floor = embedment_floor_mm()
        if relation == "touching":
            message = (
                f"bodies {a + 1} and {b + 1} touch (gap {gap:.3f}mm) but share "
                "no material — flush/tangent contact welds nothing, so they "
                "will print as separate shells. If they are meant to be one "
                f"part, sink the attachment at least {floor}mm into the host "
                "and re-compose."
            )
        elif relation == "near":
            message = (
                f"bodies {a + 1} and {b + 1} are {gap:.3f}mm apart — a "
                "sub-millimeter air channel. If they are meant to be one "
                f"part, sink the attachment at least {floor}mm into the host; "
                "if this is a designed clearance, no action needed."
            )
        else:
            message = (
                f"bodies {a + 1} and {b + 1} are {gap:.1f}mm apart — the "
                "output is not a single part."
            )
        result["findings"].append(
            {
                "bodies": [a + 1, b + 1],
                "gap_mm": round(gap, 3),
                "relation": relation,
                "message": message,
            }
        )
    return result


def attach_fusion_report(
    response: dict[str, Any],
    mesh_path: str | None,
    *,
    expect_single_body: bool | None = None,
) -> dict[str, Any]:
    """Run the fusion check on a composed output and annotate the response.

    Adds a ``fusion`` block plus one plain-language line per finding in
    ``response["warnings"]``.  Deliberately non-blocking: multi-body
    output is legitimate on the merge tools, and a door that must
    refuse (``attach_part_feature``) owns its own refusal.  Never
    raises; on any failure the response is returned untouched.
    """
    if not mesh_path:
        return response
    try:
        report = check_fusion(mesh_path, expect_single_body=expect_single_body)
        if not report.get("checked"):
            return response
        if report["findings"] or (expect_single_body and not report["fused"]):
            response["fusion"] = report
            for finding in report["findings"]:
                response.setdefault("warnings", []).append(
                    f"unfused geometry: {finding['message']}"
                )
            if expect_single_body and not report["fused"] and not report["findings"]:
                response.setdefault("warnings", []).append(
                    f"unfused geometry: output is {report['body_count']} "
                    "disconnected bodies where one part was expected."
                )
    except Exception as exc:  # noqa: BLE001 — annotation must never break a compose
        _logger.debug("fusion report skipped: %s", exc)
    return response
