"""What a moving joint's declaration means, read the same way everywhere.

A joint that moves is declared on an assembly interface
(:class:`kiln.assembly.JointMotion`: a kind, an axis, a range).  This module
is the record's own vocabulary, shared by the joint check, the composed
file's joint note (:mod:`kiln.joint_stage`) and the 3D stage: how a pose
places the moving part (:func:`pose_transform`), which parts move with a
joint (:func:`moving_group`), and when a record contradicts itself
(:class:`RecordContradiction`).  Where the parts meet through the range is
not worked out here: Kiln's servers sweep the joint
(:mod:`kiln._pro_assembly_bridge`) and the answer travels in the record.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

#: Joint kinds Kiln's servers sweep through their range.  A gear turns with
#: its mate, so turning one against a mate held still meets a tooth at the
#: first step.
SWEPT_KINDS: frozenset[str] = frozenset({"hinge", "slider", "ball"})

GEAR_NOT_SWEPT = (
    "a gear turns together with its mate, so it is not swept against a mate "
    "held still; its mesh is judged by its backlash"
)

#: Joints that move asked about in one check; the rest are said, not asked.
#: On Kiln's servers one check shares the machine with everyone else's.
MAX_JOINTS_PER_CHECK = 8

#: What each motion kind is called in a sentence.
JOINT_WORDS = {"hinge": "hinge", "slider": "slider", "ball": "ball joint", "gear": "gear"}


def _unit(vector: Any) -> np.ndarray:
    v = np.asarray(vector, dtype=float)
    return v / np.linalg.norm(v)


def _perpendicular(direction: Any) -> np.ndarray:
    d = _unit(direction)
    seed = np.array([1.0, 0.0, 0.0]) if abs(d[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    p = np.cross(d, seed)
    return p / np.linalg.norm(p)


def _turn(point: Any, direction: Any, angle_deg: float) -> np.ndarray:
    from trimesh.transformations import rotation_matrix

    return rotation_matrix(math.radians(angle_deg), _unit(direction), np.asarray(point, float))


def pose_transform(motion: Any, pose: float, *, azimuth_deg: float = 0.0) -> np.ndarray:
    """The 4x4 transform that puts part B at ``pose`` along ``motion``.

    A hinge or gear turns about the axis (right-handed about
    ``axis_direction``, degrees); a slider moves along it (mm); a ball
    swings its stem ``pose`` degrees away from ``axis_direction``, toward
    the direction ``azimuth_deg`` names around the stem.
    """
    point = np.asarray(motion.axis_point_mm, dtype=float)
    direction = _unit(motion.axis_direction)
    if motion.kind == "slider":
        transform = np.eye(4)
        transform[:3, 3] = direction * float(pose)
        return transform
    if motion.kind == "ball":
        swing_axis = _perpendicular(direction)
        if azimuth_deg:
            swing_axis = _turn(point, direction, azimuth_deg)[:3, :3] @ swing_axis
        return _turn(point, swing_axis, float(pose))
    return _turn(point, direction, float(pose))


def _same_motion(first: Any, second: Any) -> bool:
    """Whether two interfaces declare one motion: the same kind about (or
    along) the same line, whichever way each names its direction."""
    a, b = getattr(first, "motion", None), getattr(second, "motion", None)
    if a is None or b is None or a.kind != b.kind:
        return False
    da, db = _unit(a.axis_direction), _unit(b.axis_direction)
    if np.linalg.norm(np.cross(da, db)) > 1e-6:
        return False
    if a.kind == "slider":
        return True  # parallel slides are one slide wherever the line sits
    offset = np.asarray(b.axis_point_mm, float) - np.asarray(a.axis_point_mm, float)
    return bool(np.linalg.norm(offset - (offset @ da) * da) <= 1e-3)


def moving_group(
    interface: Any, interfaces: Any, part_ids: list[str],
) -> tuple[list[str], list[str] | None]:
    """The parts that move with a joint, in assembly order, and the path that
    joins its moving part back to its mate when there is one.

    Part B moves; so does every part joined to it by any other interface, at
    that joint's own rest pose (a latch on a lid, the next link of a chain).
    When part A is among them the joint closes a loop and cannot move alone:
    the second value is the path from part B to part A.  The same joint
    declared again between the same two parts (the same kind about the same
    line) is not a second path.
    """
    adjacency: dict[str, set[str]] = {pid: set() for pid in part_ids}
    pair = {interface.part_a_id, interface.part_b_id}
    for other in interfaces or ():
        if other is interface:
            continue
        if {other.part_a_id, other.part_b_id} == pair and _same_motion(other, interface):
            continue  # the same joint declared again (one per knuckle, say): no loop
        adjacency.setdefault(other.part_a_id, set()).add(other.part_b_id)
        adjacency.setdefault(other.part_b_id, set()).add(other.part_a_id)
    came_from: dict[str, str | None] = {interface.part_b_id: None}
    queue = [interface.part_b_id]
    while queue:
        here = queue.pop(0)
        for there in sorted(adjacency.get(here, ())):
            if there not in came_from:
                came_from[there] = here
                queue.append(there)
    if interface.part_a_id in came_from:
        path = [interface.part_a_id]
        while came_from[path[-1]] is not None:
            path.append(came_from[path[-1]])  # type: ignore[arg-type]
        return [], list(reversed(path))
    return [pid for pid in part_ids if pid in came_from], None


class RecordContradiction(str):
    """Why a joint was not swept, when the reason is the record contradicting
    itself (a loop, one part on both sides): the joint cannot move as
    declared, which is a fault in the design, not a gap in what Kiln checked."""
