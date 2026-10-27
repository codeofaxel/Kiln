"""Which way up a model arrived, and standing it on the bed.

A model that arrives from outside Kiln (a generator's download, a
marketplace file, an upload, an import) can lie on its back or its side:
most 3D modelling tools and every AI generator write their files with up
along Y, STL and OBJ carry no up axis at all, and an agent that guesses an
axis can turn a part the wrong way with nothing recording it.  On the 3D
stage that is the first thing a person sees.

This module is the floor every arrival door shares:

* :func:`measure` reads one part in each of its six axis poses -- as
  written, upside down, and the four turns that put another axis up -- and
  returns what a person could measure with a ruler: how tall it is, how much
  of it lies flat on the bed, how wide its footing is and whether its centre
  sits over that footing;
* :func:`stand_up` is the door's call.  It measures, asks Kiln's judgement of
  those measurements (:mod:`kiln._pro_arrival_pose_bridge`: in process on
  Kiln's servers, served to a signed-in install), applies a turn the
  judgement is sure of, records what was done beside the file
  (:mod:`kiln.arrival`) and returns the record for the door's reply;
* :func:`turn_file` rewrites a mesh file turned, in its own format, so the
  textures of an OBJ and the objects of a 3MF survive.

Which pose is right is decided on Kiln's servers.  A miss (offline, signed
out, no answer, refused) is a part shown exactly as it arrived, and the
record says so; nothing here raises on a miss.  The frame a file FORMAT
names (glTF's up-along-Y) is handled before this, at
:func:`kiln.mesh_frame.load_mesh`; a turn here is about the part, not the
format.
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

#: The record's layout; an older record reads as none.
SCHEMA_VERSION = 1

#: The six axis poses, as ``rotate_model`` would make them: ``(up, x, y)`` --
#: which axis of the file becomes up, and the turn about X then Y (degrees)
#: that does it.  The first is the part as written.  Every turn is a
#: rotation, never a mirror, so threads and handedness survive it.
AXIS_POSES: tuple[tuple[str, float, float], ...] = (
    ("+Z", 0.0, 0.0),
    ("-Z", 180.0, 0.0),
    ("+Y", 90.0, 0.0),
    ("-Y", -90.0, 0.0),
    ("+X", 0.0, -90.0),
    ("-X", 0.0, 90.0),
)

#: How a person would describe a turn about each axis.  One home for the
#: words; the design history's rotation line reads them from here.
TURN_WORDS: dict[str, str] = {
    "x": "front over back",
    "y": "side over side",
    "z": "flat on the bed",
}

#: Faces this close to the lowest point, as a share of the part's height,
#: are "on the bed".  A real flat base is within it; a curved underside
#: resting on one facet is not.
_BED_SHARE = 0.005
#: Points this close to the lowest point, as a share of the height, make
#: the footing: the feet, wheels or rim a part rests on, whether or not any
#: face is flat.
_FOOTING_SHARE = 0.01
#: A face counts as flat on the bed when its normal is within this many
#: degrees of straight down (or up: a file written inside out is read the
#: same way, since nothing can lie below the lowest point).
_FLAT_DEGREES = 2.0

#: Formats :func:`turn_file` rewrites in place.
TURNABLE_TYPES = frozenset({"stl", "obj", "ply", "3mf"})

#: The most vertices and triangles a file may declare and still be judged
#: as it arrives.  Judging reads the whole mesh; past this the part is shown
#: as it arrived and the record says so, rather than holding the door open.
#: A 64 MB binary STL, the upload doors' cap, is about 1.3 million triangles.
MAX_JUDGED_MESH_ENTRIES = 2_000_000


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def rotation(x_deg: float, y_deg: float) -> Any:
    """The 3x3 matrix of a turn of *x_deg* about X, then *y_deg* about Y --
    the order ``rotate_model`` applies them in."""
    import numpy as np

    a, b = math.radians(x_deg), math.radians(y_deg)
    rx = np.array([[1.0, 0.0, 0.0], [0.0, math.cos(a), -math.sin(a)], [0.0, math.sin(a), math.cos(a)]])
    ry = np.array([[math.cos(b), 0.0, math.sin(b)], [0.0, 1.0, 0.0], [-math.sin(b), 0.0, math.cos(b)]])
    return ry @ rx


def _hull(points: Any) -> Any | None:
    """The convex hull of 2-D *points*, counter-clockwise, or ``None`` for
    fewer than three distinct points.  Reduced to a grid first so a base
    with a million vertices costs what a base with a thousand does."""
    import numpy as np

    pts = np.asarray(points, dtype=float)
    if len(pts) < 3:
        return None
    span = float(max(np.ptp(pts[:, 0]), np.ptp(pts[:, 1])))
    if span <= 0.0:
        return None
    if len(pts) > 4096:
        cell = span / 1024.0
        keyed = np.round((pts - pts.min(axis=0)) / cell).astype(np.int64)
        _, keep = np.unique(keyed, axis=0, return_index=True)
        pts = pts[keep]
    try:
        from scipy.spatial import ConvexHull  # type: ignore

        hull = ConvexHull(pts)
        return pts[hull.vertices]
    except Exception:  # noqa: BLE001 -- scipy is optional; the chain below is exact
        pass
    pts = np.unique(pts, axis=0)
    if len(pts) < 3:
        return None
    order = np.lexsort((pts[:, 1], pts[:, 0]))
    pts = pts[order]

    def cross(o: Any, a: Any, b: Any) -> float:
        return float((a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]))

    lower: list[Any] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Any] = []
    for p in pts[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    out = np.array(lower[:-1] + upper[:-1])
    return out if len(out) >= 3 else None


def _hull_area(hull: Any) -> float:
    x, y = hull[:, 0], hull[:, 1]
    import numpy as np

    return 0.5 * abs(float(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1))))


def _hull_widths(hull: Any) -> tuple[float, float]:
    """``(narrowest, widest)`` width of *hull* over every direction an edge
    gives -- the sides of the tightest rectangle around it."""
    import numpy as np

    narrow, wide = math.inf, 0.0
    n = len(hull)
    for i in range(n):
        edge = hull[(i + 1) % n] - hull[i]
        length = math.hypot(float(edge[0]), float(edge[1]))
        if length < 1e-12:
            continue
        normal = np.array([-edge[1], edge[0]]) / length
        along = edge / length
        w = float(np.ptp(hull @ normal))
        l_ = float(np.ptp(hull @ along))
        if w < narrow:
            narrow, wide = w, l_
    if not math.isfinite(narrow):
        return 0.0, 0.0
    return narrow, wide


def _balance(hull: Any, point: Any) -> float:
    """How far inside *hull* *point* sits (mm): positive inside, negative
    outside, measured to the nearest edge."""
    n = len(hull)
    inside = True
    nearest = math.inf
    for i in range(n):
        a, b = hull[i], hull[(i + 1) % n]
        ex, ey = float(b[0] - a[0]), float(b[1] - a[1])
        length = math.hypot(ex, ey)
        if length < 1e-12:
            continue
        side = (ex * (float(point[1]) - float(a[1])) - ey * (float(point[0]) - float(a[0]))) / length
        if side < 0:
            inside = False
        nearest = min(nearest, abs(side))
    if not math.isfinite(nearest):
        return -1.0
    return nearest if inside else -nearest


def measure(mesh: Any) -> dict[str, Any]:
    """What a ruler would say about *mesh* in each of :data:`AXIS_POSES`.

    *mesh* is a ``trimesh.Trimesh`` read through :func:`kiln.mesh_frame
    .load_mesh`.  Per pose: ``height_mm``, ``extents_mm`` (x, y, z in that
    pose), ``footprint_mm2`` (the box it covers on the bed), ``contact_mm2``
    (faces lying flat on the bed) with ``base_size_mm`` (the long and short
    sides of the tightest rectangle round them), ``footing_mm2`` and
    ``footing_width_mm`` (the polygon of everything resting on the bed and
    its narrowest width) and ``balance_mm`` (how far inside that polygon the
    part's centre sits; negative when it would tip).  Units are the file's
    own.
    """
    import numpy as np

    vertices = np.asarray(mesh.vertices, dtype=float)
    faces = np.asarray(mesh.faces)
    if len(faces) == 0 or len(vertices) == 0:
        raise ValueError("the mesh has no triangles")
    normals = np.asarray(mesh.face_normals, dtype=float)
    areas = np.asarray(mesh.area_faces, dtype=float)
    centre = np.asarray(mesh.centroid, dtype=float)
    flat_cos = math.cos(math.radians(_FLAT_DEGREES))
    poses: list[dict[str, Any]] = []
    for up, x_deg, y_deg in AXIS_POSES:
        r = rotation(x_deg, y_deg)
        z = vertices @ r[2]
        zmin = float(z.min())
        height = float(z.max() - zmin)
        xy = None  # rotated, only when needed
        tri_rise = (z[faces] - zmin).max(axis=1)
        flat = np.abs(normals @ r[2]) >= flat_cos
        on_bed = (tri_rise <= _BED_SHARE * height) & flat
        contact = float(areas[on_bed].sum())
        ext_x = float(np.ptp(vertices @ r[0]))
        ext_y = float(np.ptp(vertices @ r[1]))
        footprint = ext_x * ext_y
        base_size = (0.0, 0.0)
        if contact > 0.0:
            idx = np.unique(faces[on_bed])
            base_pts = np.column_stack((vertices[idx] @ r[0], vertices[idx] @ r[1]))
            base_hull = _hull(base_pts)
            if base_hull is not None:
                narrow, wide = _hull_widths(base_hull)
                base_size = (wide, narrow)
        near = z - zmin <= _FOOTING_SHARE * height
        footing_mm2, footing_width, balance = 0.0, 0.0, -1.0
        if int(near.sum()) >= 3:
            foot_pts = np.column_stack((vertices[near] @ r[0], vertices[near] @ r[1]))
            foot_hull = _hull(foot_pts)
            if foot_hull is not None:
                footing_mm2 = _hull_area(foot_hull)
                footing_width = _hull_widths(foot_hull)[0]
                balance = _balance(foot_hull, (float(centre @ r[0]), float(centre @ r[1])))
        poses.append({
            "up": up,
            "turn": {"x": x_deg, "y": y_deg, "z": 0.0},
            "height_mm": round(height, 4),
            "extents_mm": [round(ext_x, 4), round(ext_y, 4), round(height, 4)],
            "footprint_mm2": round(footprint, 4),
            "contact_mm2": round(contact, 4),
            "base_size_mm": [round(base_size[0], 3), round(base_size[1], 3)],
            "footing_mm2": round(footing_mm2, 4),
            "footing_width_mm": round(footing_width, 4),
            "balance_mm": round(balance, 4),
        })
    return {
        "schema": SCHEMA_VERSION,
        "faces": int(len(faces)),
        "extents_mm": [round(float(v), 4) for v in np.ptp(vertices, axis=0)],
        "poses": poses,
    }


def measure_file(path: str) -> dict[str, Any]:
    """:func:`measure` for the mesh file at *path*, read in Kiln's frame
    (:func:`kiln.mesh_frame.load_mesh`), with ``format`` (the suffix) and
    ``frame_known`` (whether that format names its own up axis, so no turn
    about the file's frame is in question).

    :raises ValueError: an unreadable mesh, or one with no triangles.
    """
    import trimesh

    from kiln.mesh_frame import is_y_up, load_mesh

    loaded = load_mesh(path, force="mesh")
    if isinstance(loaded, trimesh.Scene):
        loaded = loaded.to_mesh()
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise ValueError(f"{os.path.basename(path)} holds no mesh")
    kind = os.path.splitext(path)[1].lower().lstrip(".")
    facts = measure(loaded)
    facts["format"] = kind
    facts["frame_known"] = is_y_up(path) or kind == "3mf"
    return facts


# ---------------------------------------------------------------------------
# turning the file
# ---------------------------------------------------------------------------


def turn_words(turn: dict[str, Any]) -> str:
    """The turn as a person would say it: ``90° front over back``."""
    parts = []
    for axis in ("x", "y", "z"):
        try:
            deg = float(turn.get(axis) or 0.0)
        except (TypeError, ValueError):
            deg = 0.0
        if deg % 360:
            parts.append(f"{deg:g}° {TURN_WORDS[axis]}")
    return ", then ".join(parts)


def undo_of(turn: dict[str, Any]) -> dict[str, float]:
    """The ``rotate_model`` arguments that put a part turned by *turn* back
    (one axis at a time, as every arrival turn is)."""
    return {f"rotation_{axis}": -float(turn.get(axis) or 0.0) for axis in ("x", "y", "z") if float(turn.get(axis) or 0.0) % 360}


def _turn_obj(path: str, r: Any, dz: float) -> None:
    """Rewrite an OBJ turned by *r*: positions and normals only, so faces,
    texture coordinates, groups and the material reference stay as written
    and the textures still map.  Positions are lifted by *dz* so the part
    rests on the bed."""
    tmp = f"{path}.turning"
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        for line in src:
            parts = line.split()
            if len(parts) >= 4 and parts[0] in (b"v", b"vn"):
                try:
                    x, y, z = (float(p) for p in parts[1:4])
                except ValueError:
                    dst.write(line)
                    continue
                tx = r[0][0] * x + r[0][1] * y + r[0][2] * z
                ty = r[1][0] * x + r[1][1] * y + r[1][2] * z
                tz = r[2][0] * x + r[2][1] * y + r[2][2] * z
                if parts[0] == b"v":
                    tz += dz
                turned = [parts[0], repr(float(tx)).encode(), repr(float(ty)).encode(), repr(float(tz)).encode()]
                dst.write(b" ".join(turned + parts[4:]) + b"\n")
            else:
                dst.write(line)
    os.replace(tmp, path)


def turn_file(path: str, turn: dict[str, Any]) -> None:
    """Rewrite the mesh at *path* turned by *turn* (``{"x", "y", "z"}``
    degrees, X first) and resting on the bed, in its own format.

    :raises ValueError: a format outside :data:`TURNABLE_TYPES`, or a mesh
        that cannot be read.
    """
    import numpy as np

    kind = os.path.splitext(path)[1].lower().lstrip(".")
    if kind not in TURNABLE_TYPES:
        raise ValueError(f"Kiln does not turn a .{kind} file in place")
    x_deg = float(turn.get("x") or 0.0)
    y_deg = float(turn.get("y") or 0.0)
    z_deg = float(turn.get("z") or 0.0)
    if kind == "3mf":
        from kiln.auto_orient import rotate_3mf_file

        tmp = f"{path}.turning.3mf"
        rotate_3mf_file(path, tmp, rotation_x=x_deg, rotation_y=y_deg, rotation_z=z_deg)
        os.replace(tmp, path)
        return
    r = rotation(x_deg, y_deg)
    if z_deg % 360:
        c, s = math.cos(math.radians(z_deg)), math.sin(math.radians(z_deg))
        r = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]) @ r
    if kind == "obj":
        from kiln.mesh_frame import load_mesh

        loaded = load_mesh(path, force="mesh")
        zmin = float((np.asarray(loaded.vertices, dtype=float) @ r[2]).min())
        _turn_obj(path, r, -zmin)
        return
    import trimesh

    from kiln.mesh_frame import load_mesh

    loaded = load_mesh(path, force="mesh")
    if isinstance(loaded, trimesh.Scene):
        loaded = loaded.to_mesh()
    transform = np.eye(4)
    transform[:3, :3] = r
    loaded.apply_transform(transform)
    loaded.apply_translation((0.0, 0.0, -float(loaded.bounds[0][2])))
    tmp = f"{path}.turning.{kind}"
    loaded.export(tmp, file_type=kind)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# the door's call
# ---------------------------------------------------------------------------

#: What ``status`` can be.
TURNED = "turned"        # Kiln turned the file
STANDING = "standing"    # it stands as it arrived
UNSURE = "unsure"        # Kiln could not tell; shown as it arrived
ADVISED = "advised"      # a turn is called for, but this door does not write the file
UNJUDGED = "unjudged"    # the judgement was not reached (offline, signed out, ...)


def stand_up(path: str, *, may_turn: bool = True, source: str = "") -> dict[str, Any]:
    """Judge which way up the mesh at *path* arrived and, when sure and
    *may_turn*, turn the file so it stands.

    Returns the pose record, also written beside the file
    (:func:`kiln.arrival.record_pose`): ``status`` (:data:`TURNED`,
    :data:`STANDING`, :data:`UNSURE`, :data:`ADVISED`, :data:`UNJUDGED`),
    ``said`` (the sentence for the person), ``turn`` (what was, or should
    be, applied; ``rotate_model``'s axes), ``undo`` (the arguments that put
    a turned part back), ``candidates`` (when unsure, the poses it could
    stand in, each with its turn), and ``by`` (``"kiln"``).  *source* is
    who wrote the file when the door knows (``"meshy"``, ``"upload"``).
    Never raises: a part Kiln cannot read, or a judgement that did not
    arrive, is a part shown as it arrived, and the record says why.
    """
    from kiln import _pro_arrival_pose_bridge as bridge

    record: dict[str, Any] = {"schema": SCHEMA_VERSION, "by": "kiln", "judged_at": time.time(), "source": source or ""}
    try:
        from kiln.mesh_frame import declared_mesh_entries

        declared = declared_mesh_entries(path)
    except Exception:  # noqa: BLE001 -- an uncounted file is measured below
        declared = None
    if declared is not None and declared > MAX_JUDGED_MESH_ENTRIES:
        record.update(
            status=UNJUDGED,
            turn=None,
            said=(
                f"{os.path.basename(path)} is too large to judge which way up it stands as it "
                "arrives, so it is shown exactly as it arrived; rotate_model turns it."
            ),
        )
        return record
    try:
        facts = measure_file(path)
    except Exception as exc:  # noqa: BLE001 -- an unreadable file is the door's to report
        logger.debug("arrival pose: %s not measured", path, exc_info=True)
        record.update(status=UNJUDGED, said=f"Kiln could not read {os.path.basename(path)} to tell which way up it goes; it is shown as it arrived.", turn=None)
        return record
    facts["source"] = source or ""
    verdict, miss = bridge.judge(facts)
    if verdict is None:
        record.update(status=UNJUDGED, turn=None, said=bridge.miss_sentence(miss))
        record.update(bridge.miss_fields(miss))
        if may_turn:
            _note(path, record)
        return record
    record["said"] = verdict.said
    record["candidates"] = verdict.candidates
    if verdict.turn is None:
        record.update(status=STANDING if verdict.verdict == "keep" else UNSURE, turn=None)
        if may_turn:
            _note(path, record)
        return record
    record["turn"] = verdict.turn
    if not may_turn:
        # A door that only shows writes nothing beside the file either: the
        # record rides on its reply, and says what to DO, never what was
        # done, since nothing was.
        record.update(status=ADVISED, said=verdict.advice or _advice(verdict))
        return record
    record["undo"] = undo_of(verdict.turn)
    try:
        from kiln.arrival import read as _read_arrival
        from kiln.arrival import record as _record_arrival

        came_from = _read_arrival(path)
        turn_file(path, verdict.turn)
        if came_from is not None:
            # The note is bound to the file's bytes, which just changed.
            _record_arrival(path, came_from)
    except Exception as exc:  # noqa: BLE001 -- the part is shown as it arrived, and says so
        logger.warning("arrival pose: %s not turned: %s", path, exc)
        record.pop("undo", None)
        record.update(status=ADVISED, said=f"{verdict.advice or _advice(verdict)} (Kiln could not rewrite this file itself.)")
        _note(path, record)
        return record
    record["status"] = TURNED
    _note(path, record)
    return record


def _advice(verdict: Any) -> str:
    """What to do about a turn Kiln did not apply, in rotate_model's terms."""
    args = ", ".join(
        f"rotation_{axis}={float(verdict.turn.get(axis) or 0.0):g}"
        for axis in ("x", "y", "z")
        if float(verdict.turn.get(axis) or 0.0) % 360
    )
    return f"It is shown as it arrived; rotate_model({args}) stands it up ({turn_words(verdict.turn)})."


def record_pose_for(path: str, record: dict[str, Any]) -> None:
    """Keep *record* beside *path* (:func:`kiln.arrival.record_pose`) for a
    door that applied the turn to a copy of its own.  Never raises."""
    _note(path, record)


def _note(path: str, record: dict[str, Any]) -> None:
    try:
        from kiln.arrival import record_pose

        record_pose(path, record)
    except Exception:  # noqa: BLE001 -- the turn stands without its note
        logger.debug("arrival pose note not written for %s", path, exc_info=True)
