"""Fusion check — is a composed mesh actually ONE welded body?

An attachment that ends flush against a curved host (a handle ending
exactly on a lathed wall) touches it along a line, so the two shells
share no volume.  Every classical mesh check passes it: a boolean union
leaves it as two disconnected bodies with zero warnings, and two
disjoint closed shells are watertight and edge-manifold.  Visually the
part reads as detached, structurally the junction is a sub-millimeter
air channel, and a slicer joins the two by a sliver at most.

Pieces that share a surface, or where one sinks into the other, are one
solid (:mod:`kiln.mesh_bodies`): a coat modelled on a body, letters set on
a sign, sprinkles sunk in icing.  A slicer prints them as one part, so they
are never a finding here and never float.

This module is the shared detector every composition door calls — one
helper, never a per-door reimplementation.  It never raises and never
blocks on its own; each door decides whether a finding is a refusal
(``attach_part_feature``) or a structured warning (the merge tools,
where multi-body output is often deliberate).

Vocabulary matches the pairwise contact relations used elsewhere in the
stack: ``touching`` (gap at or under ``TOUCH_GAP_MM``), ``near`` (under
``NEAR_GAP_MM`` — an accidental near-miss or a designed clearance),
``separated``.  Two relations are about the bed rather than a pair.  A
plate of parts has every part on the bed; a separate body that starts
above it is one of three things, and the geometry decides which:

* a **void** — the inverted inner shell of a hollow part.  Part of the
  host, not a piece; never a finding.
* a **captive** piece — a body held within the envelope of the rest of the
  part (a ball in a cage, a print-in-place pin in its knuckles).  It prints
  on supports inside the part and is said as a warning.
* a **floating** piece — surrounded by air, joined to nothing.  Scrap: it
  prints as a loose part on supports, or not at all, so the doors that
  compile geometry refuse the make and name the gap to the nearest
  surface.  (2026-10-19: an agent's honeycomb phone stand put 61 cells on
  a tilted plane that never met the base; every one was "separated", so
  every make door stayed quiet and the person was shown scrap.)
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

_logger = logging.getLogger(__name__)

# Gap at or under this reads as "touching": half a typical first-layer
# height, i.e. below anything a printer can resolve as a real clearance.
TOUCH_GAP_MM = 0.05
# Under this the gap is either a designed clearance or an authoring
# accident; for a piece that was meant to be fused it is an air channel.
NEAR_GAP_MM = 0.8
# Minimum overlap that makes a weld real: about two extrusion widths.
EMBEDMENT_FLOOR_MM = 0.4
# A body whose lowest point is within this of the part's floor sits on the
# bed (one first layer, with slack for a coarse export).  Higher, and there
# is air under it.
BED_LIFT_MM = 0.3
# The floor is the lowest point of a SUBSTANTIAL body: one stray sliver at
# z=0 must not make the whole model read as floating.  Substantial = at
# least this fraction of the largest body's volume.
_SUBSTANTIAL_VOLUME_FRACTION = 0.01
# A piece is captive when the part covers it from above AND below at this
# share of its sample points: it can neither fall out nor be lifted out.
# A convex envelope was tried first and is too loose -- a front lip makes
# the envelope a wedge, and 25 of the incident's 61 scrap cells sat inside
# it (measured 2026-10-19 on the real file).
_CAPTIVE_COVERED_FRACTION = 0.75
# Sample points per piece for the cover test (vertices, evenly strided).
_COVER_SAMPLES = 32
# Slack on a box-containment test, mm.
_BOUNDS_TOLERANCE_MM = 0.05
# Each airborne piece is measured against the grounded part (touching /
# near / clear, and the gap a floating one reports) up to this many pieces;
# past it a piece is judged by the cover test alone and the gap range says
# how many were measured.
_MAX_GAP_MEASURED = 96
# Cover tests (captive or floating) run for this many pieces; past it a
# piece reads as floating, which is the conservative answer for a lattice
# of hundreds of separate cells.
_MAX_COVER_TESTS = 64
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
        # Seeded: the same body gives the same points, so a gap measured
        # either side of a threshold always lands on the same side.
        sampled, _fid = trimesh.sample.sample_surface(mesh, _GAP_SAMPLES, seed=0)
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
    :returns: ``{"checked", "body_count", "fused", "grounded_bodies",
        "floating_bodies", "captive_bodies", "void_shells", "findings":
        [{"bodies", "gap_mm", "relation", "message"}, ...]}`` —
        ``checked: False`` (with ``reason``) when the measurement could not
        run; never a fake pass, never an exception.  Bodies with air under
        them that no contact finding already names are classified (see the
        module docstring): voids draw nothing, captive pieces gather into
        ONE ``captive`` finding, floating pieces into ONE ``floating``
        finding carrying ``lift_mm`` (above the floor), ``gap_mm`` (to the
        nearest grounded surface, min/max) and ``largest_mm`` — in either
        mode, because a plate of parts sits on the bed.
    """
    try:
        from kiln.mesh_frame import load_mesh
    except Exception:  # noqa: BLE001 — optional dep
        return {"checked": False, "reason": "trimesh not available"}
    try:
        mesh = load_mesh(mesh_path, force="mesh")
        if getattr(mesh, "is_empty", False) or len(getattr(mesh, "faces", [])) == 0:
            return {"checked": False, "reason": "mesh has no geometry"}
        bodies, solid_of = _pieces(mesh)
    except Exception as exc:  # noqa: BLE001
        return {"checked": False, "reason": f"mesh could not be loaded: {exc}"}
    return _judge(bodies, solid_of, expect_single_body=expect_single_body, max_pairs=max_pairs)


def floating_pieces(triangles: Any) -> list[int]:
    """Which pieces of a triangle soup float: in the air, joined to nothing,
    held by nothing.  Indices are the pieces of
    :func:`kiln.mesh_bodies.read_triangles` on the same soup, so a door that
    removes scrap removes exactly what this check calls scrap and never a
    piece of the part.
    """
    from kiln.mesh_bodies import read_triangles

    tri = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
    reading = read_triangles(tri)
    if reading.piece_count < 2:
        return []
    report = judge_reading(tri, reading)
    return sorted(
        i - 1 for f in report["findings"] if f["relation"] == "floating" for i in f["bodies"]
    )


def judge_reading(triangles: Any, reading: Any) -> dict[str, Any]:
    """:func:`check_fusion`'s report on a triangle soup that
    :func:`kiln.mesh_bodies.read_triangles` already read as *reading*: bodies
    are numbered as the reading's pieces (from 1), and nothing is read twice.
    A plate of parts sits on the bed, so only the contact and air findings
    are made (``expect_single_body=None``)."""
    import trimesh

    tri = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
    bodies = []
    for i in range(reading.piece_count):
        own = tri[reading.face_piece == i]
        bodies.append(trimesh.Trimesh(
            vertices=own.reshape(-1, 3), faces=np.arange(len(own) * 3).reshape(-1, 3),
            process=True,
        ))
    return _judge(bodies, list(reading.solid_of), expect_single_body=None, max_pairs=_MAX_PAIRS)


def _judge(
    bodies: list[Any],
    solid_of: list[int],
    *,
    expect_single_body: bool | None,
    max_pairs: int,
) -> dict[str, Any]:
    """:func:`check_fusion` on pieces already read."""
    # The bed is wherever the part's lowest SUBSTANTIAL point is, not z=0:
    # a model exported at z=50 is not "all floating", and one stray sliver
    # at the floor does not make everything above it float.
    volumes = [_signed_volume(b) for b in bodies]
    biggest = max(abs(v) for v in volumes) or 1.0
    substantial = [
        i for i, v in enumerate(volumes)
        if abs(v) >= _SUBSTANTIAL_VOLUME_FRACTION * biggest
    ] or list(range(len(bodies)))
    floor_z = min(float(bodies[i].bounds[0][2]) for i in substantial)
    lifts = [float(b.bounds[0][2]) - floor_z for b in bodies]
    airborne = [i for i, lift in enumerate(lifts) if lift > BED_LIFT_MM]
    # A piece in one solid with a piece on the bed is held up by it: a coat
    # on a body, a letter on its sign, a sprinkle sunk in the icing.
    on_bed = {solid_of[i] for i in range(len(bodies)) if i not in airborne}
    held = {i for i in airborne if solid_of[i] in on_bed}
    solids = len(set(solid_of))
    result: dict[str, Any] = {
        "checked": True,
        "body_count": len(bodies),
        "solid_count": solids,
        "fused": solids <= 1,
        "grounded_bodies": len(bodies) - len(airborne),
        # The bodies (from 1) in the air that no solid on the bed holds up.
        "unheld_bodies": [i + 1 for i in airborne if i not in held],
        "floating_bodies": 0,
        "captive_bodies": 0,
        "void_shells": 0,
        "findings": [],
    }
    if solids <= 1 and not any(volumes[i] < 0 for i in airborne):
        return result

    # Attachments meet a host: measure each smaller body against the
    # largest first, then adjacent pairs while the budget lasts.  Two pieces
    # of one solid share material, so there is nothing to measure between
    # them.
    order = sorted(range(len(bodies)), key=lambda i: -abs(float(bodies[i].area)))
    pairs = [(order[0], i) for i in order[1:]]
    pairs += [(a, b) for a, b in zip(order[1:], order[2:], strict=False)]
    pairs = [(a, b) for a, b in pairs if solid_of[a] != solid_of[b]]
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
                f"bodies {a + 1} and {b + 1} touch (gap {gap:.3f}mm) but barely "
                "share material: they meet along a line, at a point or at a "
                "small patch, so a slicer joins them by a sliver at most and the "
                "joint will not hold. If they are meant to be one part, sink the "
                f"attachment at least {floor}mm into the host and re-compose."
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
                "volumes_mm3": [round(abs(volumes[a]), 3), round(abs(volumes[b]), 3)],
            }
        )

    # Pieces in the air.  A body a contact finding already names is said
    # once, there; the rest are a void, a captive piece or a floating one.
    named = {
        i
        for f in result["findings"]
        if f["relation"] in ("touching", "near")
        for i in f["bodies"]
    }
    shells = [i for i, v in enumerate(volumes) if v < 0 and _within_bounds(bodies[i], bodies)]
    # A held piece is part of a solid on the bed, so it is never floating;
    # an inner wall is still said as the void it is.
    unplaced = [i for i in airborne if i + 1 not in named and (i not in held or i in shells)]
    if not unplaced:
        return result
    grounded = [
        i for i in range(len(bodies))
        if (i not in airborne or i in held) and i not in shells
    ]
    try:
        host = _concatenate([bodies[i] for i in grounded]) if grounded else None
    except Exception as exc:  # noqa: BLE001 — gaps then go unmeasured, never invented
        _logger.debug("fusion host unavailable: %s", exc)
        host = None
    # Every piece is measured against the grounded part before it is
    # judged: the pairwise budget above stops at ``max_pairs``, and the
    # ninth of twenty raised letters on a sign touches the plate exactly as
    # the first eight do.  One-sided (piece samples to host surface) is
    # enough to tell touching from near from clear.
    voids: list[int] = []
    resting: list[int] = []
    close: list[int] = []
    captive: list[int] = []
    floating: list[int] = []
    gaps: dict[int, float] = {}
    tris_by_body = [np.asarray(b.triangles, dtype=float) for b in bodies] if unplaced else []
    cover_tests = 0
    for i in unplaced:
        if i in shells:
            voids.append(i)
            continue
        gap = None
        if host is not None and len(gaps) < _MAX_GAP_MEASURED:
            try:
                gap = _points_to_surface_min_mm(_sample_points(bodies[i]), host)
                gaps[i] = gap
            except Exception as exc:  # noqa: BLE001 — unmeasured, never invented
                _logger.debug("floating gap measurement failed: %s", exc)
                result["gap_unmeasured"] = True
        if gap is not None and gap <= TOUCH_GAP_MM:
            resting.append(i)
            continue
        if gap is not None and gap <= NEAR_GAP_MM:
            close.append(i)
            continue
        others = [tris_by_body[j] for j in range(len(bodies)) if j != i and j not in shells]
        if others and cover_tests < _MAX_COVER_TESTS and _covered_above_and_below(bodies[i], others):
            captive.append(i)
        else:
            floating.append(i)
        cover_tests += 1
    result["void_shells"] = len(voids)
    if resting:
        result["findings"].append(_contact_group_finding(resting, "touching"))
    if close:
        result["findings"].append(_contact_group_finding(close, "near"))
    if captive:
        result["captive_bodies"] = len(captive)
        result["findings"].append(_captive_finding(bodies, captive))
    if floating:
        result["floating_bodies"] = len(floating)
        measured = [gaps[i] for i in floating if i in gaps]
        gap_block = (
            {"min": round(min(measured), 2), "max": round(max(measured), 2), "measured": len(measured)}
            if measured else None
        )
        result["findings"].append(_floating_finding(bodies, lifts, floating, gap_block))
    return result


def _contact_group_finding(members: list[int], relation: str) -> dict[str, Any]:
    """One finding for the pieces past the pairwise budget that touch, or all
    but touch, the grounded part -- the same two relations, said once."""
    n = len(members)
    floor = embedment_floor_mm()
    if relation == "touching":
        message = (
            f"{n} more piece{'s' if n != 1 else ''} touch the part but barely "
            "share material: they meet it along a line, at a point or at a "
            "small patch, so a slicer joins them by a sliver at most and the "
            f"joint will not hold. If they are meant to be one part, sink them "
            f"at least {floor}mm into it."
        )
    else:
        message = (
            f"{n} more piece{'s' if n != 1 else ''} sit under {NEAR_GAP_MM} mm "
            "from the part — a sub-millimeter air channel. If they are meant to "
            f"be one part, sink them at least {floor}mm into it; if this is a "
            "designed clearance, no action needed."
        )
    return {"bodies": [i + 1 for i in members], "relation": relation, "message": message}


def _pieces(mesh: Any) -> tuple[list[Any], list[int]]:
    """The mesh's pieces as every door reads them (:mod:`kiln.mesh_bodies`),
    each as its own mesh, and the solid each prints in.

    A face-adjacency split cuts two pieces laid on each other into hundreds
    of fragments along their contact (2026-10-23: a Blender donut of 62
    pieces read as 1,465 bodies, 1,052 of them "floating"), and how many
    depended on the unit the file was written in.
    """
    import numpy as np

    from kiln.mesh_bodies import read_mesh

    reading = read_mesh(mesh)
    labels = reading.face_piece
    order = np.argsort(labels, kind="stable")
    bounds = np.searchsorted(labels[order], np.arange(reading.piece_count + 1))
    groups = [order[bounds[i] : bounds[i + 1]] for i in range(reading.piece_count)]
    bodies = list(mesh.submesh(groups, append=False, only_watertight=False))
    return bodies, list(reading.solid_of) or list(range(len(bodies)))


def _signed_volume(body: Any) -> float:
    """Signed volume: negative for an inverted shell (a hollow part's inner
    wall), which is how a void tells itself apart from a piece."""
    try:
        return float(body.volume)
    except Exception:  # noqa: BLE001 — an open shell: treat as a small positive body
        return 0.0


def _concatenate(parts: list[Any]) -> Any:
    import trimesh

    return parts[0] if len(parts) == 1 else trimesh.util.concatenate(parts)


def _cover_samples(body: Any) -> Any:
    """Up to :data:`_COVER_SAMPLES` of the body's vertices plus its centroid."""
    import numpy as np

    pts = np.asarray(body.vertices, dtype=float)
    if len(pts) > _COVER_SAMPLES:
        pts = pts[np.linspace(0, len(pts) - 1, _COVER_SAMPLES).astype(int)]
    return np.vstack([pts, np.asarray(body.centroid, dtype=float)[None, :]])


def _covered_above_and_below(body: Any, others: list[Any]) -> bool:
    """True when the rest of the part (``others``: its triangle arrays) lies
    both above and below most of the piece: a vertical line through a sample
    point meets the part on both sides.  Pure numpy on the triangles' XY footprints -- no ray library --
    so it runs on a bare install.  A ball in a closed cage or inside a
    hollow part is covered; a cell over an open base, or a ball in a cage
    of thin bars with the sky above it, is not.
    """
    import numpy as np

    pts = _cover_samples(body)
    tris = np.vstack(others)
    if len(tris) == 0:
        return False
    above = np.zeros(len(pts), dtype=bool)
    below = np.zeros(len(pts), dtype=bool)
    a, b, c = tris[:, 0, :], tris[:, 1, :], tris[:, 2, :]
    # 2D barycentric test of each point against each triangle's footprint.
    d = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
    ok = np.abs(d) > 1e-12
    a, b, c, d = a[ok], b[ok], c[ok], d[ok]
    chunk = max(1, int(2_000_000 // max(len(a), 1)))
    for s in range(0, len(pts), chunk):
        p = pts[s : s + chunk]
        px, py = p[:, 0][:, None], p[:, 1][:, None]
        l1 = ((b[:, 1] - c[:, 1])[None, :] * (px - c[:, 0][None, :])
              + (c[:, 0] - b[:, 0])[None, :] * (py - c[:, 1][None, :])) / d[None, :]
        l2 = ((c[:, 1] - a[:, 1])[None, :] * (px - c[:, 0][None, :])
              + (a[:, 0] - c[:, 0])[None, :] * (py - c[:, 1][None, :])) / d[None, :]
        l3 = 1.0 - l1 - l2
        inside = (l1 >= -1e-9) & (l2 >= -1e-9) & (l3 >= -1e-9)
        z_hit = l1 * a[:, 2][None, :] + l2 * b[:, 2][None, :] + l3 * c[:, 2][None, :]
        pz = p[:, 2][:, None]
        above[s : s + chunk] = (inside & (z_hit > pz + _BOUNDS_TOLERANCE_MM)).any(axis=1)
        below[s : s + chunk] = (inside & (z_hit < pz - _BOUNDS_TOLERANCE_MM)).any(axis=1)
    return float((above & below).mean()) >= _CAPTIVE_COVERED_FRACTION


def _within_bounds(body: Any, bodies: Any) -> bool:
    """True when ``body``'s box sits inside some OTHER body's box — the
    shape an inner shell has."""
    lo, hi = body.bounds
    for other in bodies:
        if other is body:
            continue
        olo, ohi = other.bounds
        if (olo <= lo + _BOUNDS_TOLERANCE_MM).all() and (ohi >= hi - _BOUNDS_TOLERANCE_MM).all():
            return True
    return False


def _span_text(low: float, high: float) -> str:
    return f"{low:.1f} mm" if high - low < 0.05 else f"{low:.1f} to {high:.1f} mm"


def _captive_finding(bodies: Any, captive: list[int]) -> dict[str, Any]:
    n = len(captive)
    biggest = max(captive, key=lambda i: abs(_signed_volume(bodies[i])))
    ext = bodies[biggest].bounds[1] - bodies[biggest].bounds[0]
    if n == 1:
        message = (
            "1 piece is held inside the part without touching it (a captive "
            "piece): it prints on supports inside the part, which have to be "
            "cleared out afterwards. Fine if that is the design; if it was "
            "meant to be joined, extend it until it overlaps."
        )
    else:
        message = (
            f"{n} pieces are held inside the part without touching it "
            "(captive pieces): they print on supports inside the part, which "
            "have to be cleared out afterwards. Fine if that is the design; "
            "if they were meant to be joined, extend them until they overlap."
        )
    return {
        "bodies": [i + 1 for i in captive],
        "relation": "captive",
        "largest_mm": [round(float(v), 2) for v in ext],
        "message": message,
    }


def _floating_finding(
    bodies: Any, lifts: list[float], floating: list[int], gaps: dict[str, float] | None,
) -> dict[str, Any]:
    """The one finding for every piece surrounded by air."""
    low = min(lifts[i] for i in floating)
    high = max(lifts[i] for i in floating)
    biggest = max(floating, key=lambda i: abs(_signed_volume(bodies[i])))
    ext = bodies[biggest].bounds[1] - bodies[biggest].bounds[0]
    n = len(floating)
    height = _span_text(low, high)
    floor = embedment_floor_mm()
    nearest = ""
    if gaps:
        nearest = (
            f" and the nearest part of the model is {_span_text(gaps['min'], gaps['max'])} away"
        )
    if n == 1:
        message = (
            f"1 piece floats with nothing under it, {height} above the bed"
            f"{nearest}: nothing joins it to the rest, so it would print as a "
            "loose part on supports, or not at all. If it belongs to the "
            f"part, extend it until it overlaps by at least {floor} mm; if it "
            "is a separate part, set it on the bed."
        )
    else:
        message = (
            f"{n} pieces float with nothing under them, {height} above the "
            f"bed{nearest}: nothing joins them to the rest, so they would "
            "print as loose parts on supports, or not at all. If they belong "
            f"to the part, extend them until they overlap it by at least "
            f"{floor} mm; if they are separate parts, set them on the bed."
        )
    finding: dict[str, Any] = {
        "bodies": [i + 1 for i in floating],
        "relation": "floating",
        "lift_mm": {"min": round(low, 2), "max": round(high, 2)},
        "largest_mm": [round(float(v), 2) for v in ext],
        "message": message,
    }
    if gaps:
        finding["gap_mm"] = gaps
    return finding


def floating_summary(report: dict[str, Any]) -> str:
    """One short sentence for a response's ``message`` line, or ``""``.

    The agents that relay a make to the user most tersely repeat that one
    line and nothing else (2026-10-19, measured on the hosted connector),
    so a piece in the air has to be in it.
    """
    n = int(report.get("floating_bodies") or 0)
    if n <= 0:
        return ""
    if n == 1:
        return "Warning: 1 piece floats above the bed with nothing under it — see warnings."
    return f"Warning: {n} pieces float above the bed with nothing under them — see warnings."


#: The refusal code a make door answers with when pieces float and the
#: caller did not say they were meant to.
FLOATING_REFUSAL_CODE = "FLOATING_PIECES"


def refuse_for_floating(response: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Turn a make with pieces in the air into a refusal, in place.

    A printed part with a piece hanging free is not a part with a caveat:
    it is scrap, and an agent that reads only the first line ships it.  So
    a door that compiles geometry refuses it outright -- ``success`` false,
    ``code`` :data:`FLOATING_REFUSAL_CODE`, the finding as ``error`` with
    the fix -- and no preview is drawn, no version recorded, nothing put in
    front of the person.  The file path stays on the response so the agent
    can look at what it wrote.  There is no flag to pass instead: a piece
    meant to be held by the part is captive (inside its envelope) and is
    not refused, so the geometry is the only way through, on purpose.
    """
    finding = next(
        (f for f in report.get("findings", []) if f.get("relation") == "floating"),
        None,
    )
    if finding is None:
        return response
    response["success"] = False
    if "status" in response:
        response["status"] = "error"
    response["code"] = FLOATING_REFUSAL_CODE
    response["error"] = (
        f"Not made: {finding['message']} Fix the geometry and compile again. "
        "A piece meant to be held by the part (a ball in a cage, a pin in its "
        "knuckles) is not refused when it sits inside the part's envelope."
    )
    response["message"] = response["error"]
    return response


def attach_fusion_report(
    response: dict[str, Any],
    mesh_path: str | None,
    *,
    expect_single_body: bool | None = None,
    refuse_floating: bool = False,
) -> dict[str, Any]:
    """Run the fusion check on a composed output and annotate the response.

    Adds a ``fusion`` block plus one plain-language line per finding in
    ``response["warnings"]``.  Deliberately non-blocking for contact
    findings: multi-body output is legitimate on the merge tools, and a
    door that must refuse (``attach_part_feature``) owns its own refusal.
    With ``refuse_floating`` a make with pieces in the air is refused
    through :func:`refuse_for_floating` -- the doors that compile geometry
    pass it, so a floating piece never reaches a preview.  Never raises; on
    any failure the response is returned untouched.
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
                label = {
                    "floating": "floating pieces",
                    "captive": "captive pieces",
                }.get(finding["relation"], "unfused geometry")
                response.setdefault("warnings", []).append(
                    f"{label}: {finding['message']}"
                )
            if expect_single_body and not report["fused"] and not report["findings"]:
                response.setdefault("warnings", []).append(
                    f"unfused geometry: output is {report['body_count']} "
                    "disconnected bodies where one part was expected."
                )
            summary = floating_summary(report)
            if summary and isinstance(response.get("message"), str):
                response["message"] = f"{response['message'].rstrip()} {summary}"
            if refuse_floating and report.get("floating_bodies"):
                refuse_for_floating(response, report)
    except Exception as exc:  # noqa: BLE001 — annotation must never break a compose
        _logger.debug("fusion report skipped: %s", exc)
    return response
