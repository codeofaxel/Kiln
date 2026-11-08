"""The moving joints a composed mesh carries to Kiln's 3D stage.

``compose_assembly`` merges an assembly's parts into one file, and a joint
declared with a motion (:class:`kiln.assembly.JointMotion`) would be lost in
it: the file is triangles.  This module keeps the joints beside the file, the
way :mod:`kiln.arrival` keeps where a file came from:

* :func:`record` writes ``<file>.joints.json`` beside the composed mesh,
  bound to the file's bytes, so a different file later saved under the same
  name is never played as this one;
* :func:`stage_block` gives the stage a ``kiln.joints.v1`` block: for each
  joint, the part that moves (its run of triangles in the file, in the file's
  own order), the motion, and the sweep of it (the joint posed through its
  range by Kiln's servers, :mod:`kiln._pro_assembly_bridge`), so the stage
  can play the joint and show the pose where its parts meet;
* :func:`shift_block` moves the block with the geometry when the stage
  centres it on the plate (:func:`kiln.stage_plate.stand_on_plate`), and
  :func:`withhold` keeps the record while saying why it cannot be played.

THE BLOCK (``kiln.joints.v1``)
------------------------------
Coordinates are millimetres, Z up, in the frame of the geometry the block
travels with: on the stage payload, the frame of ``bbox`` (the plate step
moves both by one offset); beside a served file, that file's own frame.  A
renderer turns an axis point exactly as it turns a vertex of that geometry,
and an axis direction by the rotation alone.  Each joint::

    {
      "part_a": "frame", "part_b": "leaf",
      "motion": {"kind": "hinge", "axis_point_mm": [x, y, z],
                 "axis_direction": [x, y, z], "range": [lo, hi],
                 "unit": "deg", "swing_axis": null},
      "moving": {"spans": [[i, n], ...], "parts": ["leaf", ...]},
      "sweep": {...},            # the served sweep, its readings thinned
                                 # to "profile": [[pose, gap_mm, meets]...]
      "sweep_why_not": null,     # why there is no sweep, when there is none
      "playable": true,
      "why_not": null            # why the stage cannot play it, when it cannot
    }

Part B moves, and with it every part joined to it (``moving.parts``; each
one's run of triangles in ``moving.spans``, ``[first, count]`` in the file's
own triangle order); every other part stays.  A hinge or gear turns ``pose``
degrees about the line through ``axis_point_mm`` along ``axis_direction``,
right-handed; a
slider moves ``pose`` millimetres along it; a ball swings ``pose`` degrees
about the line through ``axis_point_mm`` along ``swing_axis`` (the direction
its sweep found the parts meet first, otherwise the direction read first).
A renderer copies the spans' triangles out rather than moving shared
vertices.  The parts meet outside ``sweep.free_range``.

A block crosses machines, so every reader takes it as data:
:func:`checked_block` is the shape and the sizes Kiln writes, and Kiln's
servers keep no block that fails it.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

#: Bumped on a breaking change to the note's layout; an older note reads as
#: no note, never as a wrong one.
SCHEMA_VERSION = 1

#: Appended to the file's own path: ``lid.stl`` -> ``lid.stl.joints.json``.
NOTE_SUFFIX = ".joints.json"

#: The stage's name for the block.  A renderer reads only a kind it knows.
STAGE_KIND = "kiln.joints.v1"

#: Readings a stage block carries per joint, evenly thinned; the edges of
#: the free range and the tightest reading are always kept.
MAX_PROFILE = 256

#: The largest block sent beside a served file, bytes; Kiln's servers hold
#: the same ceiling.  A block over it is not sent, and the file is still.
MAX_SIDECAR_BYTES = 262_144

#: The most joints one block names, and the most runs of triangles (or
#: parts) one joint moves: far past any assembly Kiln composes, so a block
#: over either was not written by Kiln.
MAX_JOINTS = 64
MAX_SPANS = 1024

#: The longest name or sentence a block carries, characters.
MAX_TEXT = 4000

#: The farthest a slide's range may reach from where it was modelled, mm.
MAX_SLIDE_MM = 100_000.0

#: The largest note read from beside a file, bytes.
_MAX_NOTE_BYTES = 8 * 1024 * 1024

GEAR_NOT_PLAYED = (
    "a gear turns together with its mate, and the stage moves one part"
)


def note_path_for(file_path: str) -> str:
    return f"{file_path}{NOTE_SUFFIX}"


def _identity(file_path: str) -> str | None:
    """The file's content hash, as the print gate and the arrival note
    identify a file."""
    from kiln.preview_gate import hash_file

    digest = hash_file(file_path)
    return None if digest.startswith("NO_FILE:") else digest


# ---------------------------------------------------------------------------
# Building the record at compose time
# ---------------------------------------------------------------------------


def _profile(sweep: dict[str, Any]) -> list[list[Any]]:
    """A served sweep's readings as ``[pose, gap_mm, meets]``, thinned to
    :data:`MAX_PROFILE` with the poses that carry the verdict kept."""
    readings = [r for r in sweep.get("readings") or [] if isinstance(r, dict)]
    if len(readings) > MAX_PROFILE:
        keep_poses = {sweep.get("tightest_pose"), sweep.get("binding_pose")}
        if sweep.get("free_range"):
            keep_poses.update(sweep["free_range"])
        stride = len(readings) / float(MAX_PROFILE)
        picked = {int(i * stride) for i in range(MAX_PROFILE)}
        readings = [
            r for i, r in enumerate(readings) if i in picked or r.get("pose") in keep_poses
        ]
    return [[r.get("pose"), r.get("gap_mm"), r.get("meets")] for r in readings]


def joint_entry(
    interface: Any,
    spans: dict[str, tuple[int, int]],
    parts: Any,
    interfaces: Any,
    *,
    swept: tuple[dict[str, Any] | None, str | None] = (None, None),
) -> dict[str, Any]:
    """One joint's record: the motion, where everything that moves with it
    sits in the composed file (``spans``, each part's run of triangles), and
    *swept*, the sweep Kiln's servers gave for it (or why there is none)."""
    from kiln.joint_record import moving_group, pose_transform

    motion = interface.motion
    swing_axis = None
    sweep, why = swept
    if isinstance(sweep, dict) and sweep.get("moving_parts"):
        moving = [str(name) for name in sweep["moving_parts"]]
    else:
        order = [p.part_id for p in parts] if not isinstance(parts, dict) else list(parts)
        moving = moving_group(interface, interfaces or [interface], order)[0] or [interface.part_b_id]
    runs = [[int(spans[pid][0]), int(spans[pid][1])] for pid in moving if pid in spans]
    sweep_dict: dict[str, Any] | None = None
    if isinstance(sweep, dict):
        sweep_dict = {key: value for key, value in sweep.items() if key != "readings"}
        sweep_dict["profile"] = _profile(sweep)
    if motion.kind == "ball":
        azimuth = (sweep_dict or {}).get("binding_azimuth") or 0.0
        turn = pose_transform(motion, 1.0, azimuth_deg=float(azimuth))[:3, :3]
        swing_axis = _rotation_axis(turn)
    return {
        "part_a": interface.part_a_id,
        "part_b": interface.part_b_id,
        "motion": {
            "kind": motion.kind,
            "axis_point_mm": [float(c) for c in motion.axis_point_mm],
            "axis_direction": [float(c) for c in motion.axis_direction],
            "range": [float(motion.range_min), float(motion.range_max)],
            "unit": motion.range_unit,
            "swing_axis": swing_axis,
        },
        "moving": {"spans": runs, "parts": moving},
        "sweep": sweep_dict,
        "sweep_why_not": why,
    }


def _rotation_axis(rotation: Any) -> list[float]:
    """The unit axis of a small rotation matrix (the swing direction)."""
    import numpy as np

    r = np.asarray(rotation, dtype=float)
    axis = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]])
    return [float(c) for c in axis / np.linalg.norm(axis)]


def record(file_path: str, joints: list[dict[str, Any]], *, total_triangles: int) -> str | None:
    """Write the joints note beside *file_path*.  Returns its path, or
    ``None`` when there is nothing to write or it could not be written.
    Never raises: a composed file is never lost for the sake of its note."""
    try:
        if not joints:
            return None
        identity = _identity(file_path)
        if identity is None:
            return None
        note = note_path_for(file_path)
        body = {
            "schema": SCHEMA_VERSION,
            "file": identity,
            "total_triangles": int(total_triangles),
            "joints": joints,
        }
        with open(note, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
        return note
    except Exception:  # noqa: BLE001
        logger.debug("joints note not written for %s", file_path, exc_info=True)
        return None


def _body(file_path: str) -> dict[str, Any] | None:
    """The note's body, or ``None``: no note, unreadable, another layout, or
    written for different bytes than the file now holds."""
    try:
        path = note_path_for(file_path)
        import os

        if os.path.getsize(path) > _MAX_NOTE_BYTES:
            return None
        with open(path, encoding="utf-8") as fh:
            body = json.load(fh)
        if not isinstance(body, dict) or body.get("schema") != SCHEMA_VERSION:
            return None
        if body.get("file") != _identity(file_path):
            return None
        if not isinstance(body.get("joints"), list):
            return None
        return body
    except Exception:  # noqa: BLE001 -- no note reads as no joints
        return None


# ---------------------------------------------------------------------------
# The stage block
# ---------------------------------------------------------------------------


def stage_block(file_path: str) -> dict[str, Any] | None:
    """The ``kiln.joints.v1`` block for *file_path*, in the file's own frame,
    or ``None`` when the file carries no joints."""
    body = _body(file_path)
    if body is None:
        return None
    joints = []
    for entry in body["joints"]:
        if not isinstance(entry, dict):
            continue
        joint = dict(entry)
        kind = (joint.get("motion") or {}).get("kind")
        joint["playable"], joint["why_not"] = (
            (False, GEAR_NOT_PLAYED) if kind == "gear" else (True, None)
        )
        joints.append(joint)
    if not joints:
        return None
    return checked_block({
        "kind": STAGE_KIND,
        "frame": "mesh",
        "total_triangles": body.get("total_triangles"),
        "joints": joints,
    })


# ---------------------------------------------------------------------------
# The block as data
# ---------------------------------------------------------------------------


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _point(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_finite(c) for c in value)


def _text(value: Any) -> bool:
    return value is None or (isinstance(value, str) and len(value) <= MAX_TEXT)


def _names(value: Any) -> bool:
    return (isinstance(value, list) and len(value) <= MAX_SPANS
            and all(isinstance(n, str) and len(n) <= MAX_TEXT for n in value))


def _motion_ok(motion: Any, playable: bool) -> bool:
    """The motion a joint record carries: the kind, and when the joint plays,
    an axis and a range :class:`kiln.assembly.JointMotion` itself accepts."""
    from kiln.assembly import JointMotion

    if not isinstance(motion, dict) or motion.get("kind") not in ("hinge", "slider", "ball", "gear"):
        return False
    if not playable and set(motion) <= {"kind"}:
        return True
    point, direction, span = motion.get("axis_point_mm"), motion.get("axis_direction"), motion.get("range")
    if not (_point(point) and _point(direction) and isinstance(span, list) and len(span) == 2
            and all(_finite(v) for v in span)):
        return False
    try:
        declared = JointMotion(kind=motion["kind"], axis_point_mm=tuple(point),
                               axis_direction=tuple(direction), range_min=span[0], range_max=span[1])
    except (TypeError, ValueError):
        return False
    if motion.get("unit") != declared.range_unit:
        return False
    if declared.kind == "slider" and max(abs(span[0]), abs(span[1])) > MAX_SLIDE_MM:
        return False
    swing = motion.get("swing_axis")
    return swing is None or (_point(swing) and any(swing))


def _moving_ok(moving: Any, total: int) -> bool:
    """Runs of triangles inside the file, no more of them than it holds."""
    if not isinstance(moving, dict) or not _names(moving.get("parts", [])):
        return False
    spans = moving.get("spans")
    if not (isinstance(spans, list) and 1 <= len(spans) <= MAX_SPANS):
        return False
    held = 0
    for run in spans:
        if not (isinstance(run, list) and len(run) == 2 and all(_count(v) for v in run)):
            return False
        first, count = run
        if count == 0 or first + count > total:
            return False
        held += count
    return held <= total


_SWEEP_NUMBERS = (
    "binding_pose", "binding_azimuth", "rest_gap_mm", "min_gap_mm", "tightest_pose",
    "gap_floor_mm", "start_pose", "printed_gap_mm", "contact_pose",
)


def _pairs(value: Any) -> bool:
    return (isinstance(value, list) and len(value) <= MAX_PROFILE
            and all(isinstance(p, list) and len(p) == 2 and all(_finite(v) for v in p) for p in value))


def _sweep_ok(sweep: Any) -> bool:
    if sweep is None:
        return True
    if not isinstance(sweep, dict) or not _text(sweep.get("summary")):
        return False
    if any(sweep.get(key) is not None and not _finite(sweep[key]) for key in _SWEEP_NUMBERS):
        return False
    free, contact = sweep.get("free_range"), sweep.get("contact_point_mm")
    if free is not None and not _pairs([free]):
        return False
    if contact is not None and not _point(contact):
        return False
    hitting = sweep.get("hitting")
    if hitting is not None and not (_names(hitting) and len(hitting) == 2):
        return False
    if not (_pairs(sweep.get("unread", [])) and _names(sweep.get("moving_parts", []))
            and _names(sweep.get("fixed_parts", []))):
        return False
    profile = sweep.get("profile", [])
    return isinstance(profile, list) and len(profile) <= MAX_PROFILE and all(
        isinstance(row, list) and len(row) == 3 and _finite(row[0])
        and (row[1] is None or _finite(row[1])) and row[2] in (True, False, None)
        for row in profile
    )


def checked_block(block: Any) -> dict[str, Any] | None:
    """*block* when it has the shape and the sizes Kiln writes, else ``None``.

    A number that is not finite, a range :class:`kiln.assembly.JointMotion`
    refuses, a run of triangles past the file's end, or more joints, runs,
    readings or text than Kiln writes, and the whole block is refused: it was
    not written by Kiln, or not for this file.  Fields Kiln does not write are
    left alone (a newer writer's), bounded by the size the block travels at.
    """
    if not isinstance(block, dict) or block.get("kind") != STAGE_KIND or not _text(block.get("frame")):
        return None
    total = block.get("total_triangles")
    if total is not None and not _count(total):
        return None
    joints = block.get("joints")
    if not (isinstance(joints, list) and 1 <= len(joints) <= MAX_JOINTS):
        return None
    for joint in joints:
        if not isinstance(joint, dict) or joint.get("playable") not in (True, False):
            return None
        playable = joint["playable"]
        if not all(_text(joint.get(key)) for key in ("part_a", "part_b", "why_not", "sweep_why_not")):
            return None
        if not _motion_ok(joint.get("motion"), playable) or not _sweep_ok(joint.get("sweep")):
            return None
        if (playable or joint.get("moving") is not None) and not (
            total is not None and _moving_ok(joint.get("moving"), total)
        ):
            return None
    return block


TOO_LARGE_TO_SEND = (
    "the joint record is too large to send beside the file, so this stage cannot play it"
)


def sidecar_for_mesh(file_path: str) -> bytes | None:
    """The block for *file_path* as the JSON a served copy of the file
    carries beside it, in the file's own frame, or ``None`` when the file
    carries no joints.

    Within :data:`MAX_SIDECAR_BYTES` it is the whole block.  Over it, the
    gap profiles go first (the joint still plays, without a gap readout);
    then each joint is sent unplayable with its sentence and the reason, so
    the page says a joint was recorded rather than nothing.  ``None`` only
    when not even that fits.
    """
    block = stage_block(file_path)
    if block is None:
        return None

    def fits(candidate: dict[str, Any]) -> bytes | None:
        data = json.dumps(candidate, sort_keys=True).encode("utf-8")
        return data if len(data) <= MAX_SIDECAR_BYTES else None

    whole = fits(block)
    if whole is not None:
        return whole
    for joint in block["joints"]:
        if isinstance(joint.get("sweep"), dict):
            joint["sweep"]["profile"] = []
    lighter = fits(block)
    if lighter is not None:
        return lighter
    said = {
        "kind": STAGE_KIND,
        "frame": block.get("frame"),
        "joints": [
            {
                "part_a": joint.get("part_a"),
                "part_b": joint.get("part_b"),
                "motion": {"kind": (joint.get("motion") or {}).get("kind")},
                "sweep": {"summary": (joint.get("sweep") or {}).get("summary")},
                "sweep_why_not": joint.get("sweep_why_not"),
                "playable": False,
                "why_not": TOO_LARGE_TO_SEND,
            }
            for joint in block["joints"]
        ],
    }
    return fits(said)


def withhold(block: dict[str, Any] | None, why: str) -> None:
    """Keep the joints on the block but mark none playable, saying why (the
    stage shows a simplified mesh, or the part turned as the slicer placed
    it).  The sweep's sentence still reaches the person."""
    if not isinstance(block, dict):
        return
    for joint in block.get("joints") or []:
        if isinstance(joint, dict) and joint.get("playable"):
            joint["playable"] = False
            joint["why_not"] = why


def shift_block(block: dict[str, Any] | None, dx: float, dy: float) -> None:
    """Move every point the block holds (each axis point, each contact point)
    by the plate step's offset (mesh X and Y), in place, the same slide the
    geometry and its ``bbox`` take."""
    if not isinstance(block, dict):
        return

    def slid(point: Any) -> Any:
        if isinstance(point, list) and len(point) == 3:
            return [round(float(point[0]) + dx, 6), round(float(point[1]) + dy, 6), point[2]]
        return point

    for joint in block.get("joints") or []:
        if not isinstance(joint, dict):
            continue
        motion = joint.get("motion")
        if isinstance(motion, dict):
            motion["axis_point_mm"] = slid(motion.get("axis_point_mm"))
        sweep = joint.get("sweep")
        if isinstance(sweep, dict) and sweep.get("contact_point_mm") is not None:
            sweep["contact_point_mm"] = slid(sweep["contact_point_mm"])


def attach_to_payload(payload: dict[str, Any] | None, file_path: str) -> dict[str, Any] | None:
    """Put the block for *file_path* on a ``kiln.mesh.v1`` payload, in place,
    before the plate step moves it.  A payload whose triangles are not the
    file's own (simplified, or only a size card) keeps the record and plays
    nothing.  Never raises."""
    try:
        if not isinstance(payload, dict):
            return payload
        block = stage_block(file_path)
        if block is None:
            return payload
        payload["joints"] = block
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        if payload.get("downgraded"):
            withhold(block, "the stage shows a size card, not the mesh")
        elif payload.get("decimated_from"):
            withhold(block, "the stage shows a simplified mesh, so the moving part's "
                            "triangles are not where the record says")
        elif counts.get("triangles") != block.get("total_triangles"):
            withhold(block, "the stage's mesh does not have the triangles the "
                            "record was made from")
    except Exception:  # noqa: BLE001 -- the stage draws the part either way
        logger.debug("joints not attached for %s", file_path, exc_info=True)
    return payload
