"""The mechanism note beside a composed file: the ``kiln.mechanism.v1`` block.

Composing an assembly whose record declares a mechanism asks Kiln's servers to
play it (:mod:`kiln._pro_mechanism_bridge`); the block that comes back is kept
beside the composed file as ``<file>.mechanism.json`` so the stage can play
the whole assembly: every part's transform at each driver pose over one
period, each slide's stroke and dead centres, and whether any two parts meet
anywhere in the cycle.  Block coordinates are in the composed file's own
frame; a stage turns a frame's translation and the clash points exactly as it
turns the file's vertices.  The note names the file's bytes, so a note left
beside a re-composed file is not read.

The block reaches a stage two ways, as the joints do: on the local stage's
payload under ``mechanism`` (:func:`attach_to_payload`), and beside the file
handed to a hosted stage page (:func:`sidecar_for_mesh`).  A stage that
does not draw the file's own triangles, or draws them turned, keeps what the
mechanism says and plays nothing (:func:`withhold`); one that slides the
part across the plate is told by how far (:func:`shift_block`).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from kiln.joint_stage import _identity

logger = logging.getLogger(__name__)

KIND = "kiln.mechanism.v1"
SCHEMA_VERSION = "kiln.mechanism_note.v1"
_MAX_NOTE_BYTES = 64 * 1024 * 1024
#: The most a block may weigh on a stage payload, which is sent whole to the
#: panel beside the mesh; a larger one says what the mechanism does and plays
#: nothing there.
MAX_PAYLOAD_BLOCK_BYTES = 2 * 1024 * 1024


#: What the note adds to its file's name.
NOTE_SUFFIX = ".mechanism.json"


def note_path_for(file_path: str) -> str:
    return f"{file_path}{NOTE_SUFFIX}"


def names_file(bound: Any, sha256_hex: str) -> bool:
    """Whether a note's ``file`` field (*bound*: the leading digits of its
    file's SHA-256, as :func:`kiln.preview_gate.hash_file` writes them) names
    the bytes whose whole SHA-256 is *sha256_hex*."""
    return (isinstance(bound, str) and len(bound) >= 32 and isinstance(sha256_hex, str)
            and sha256_hex.lower().startswith(bound.lower()))


def block_from_note(raw: bytes, sha256_hex: str) -> dict[str, Any] | None:
    """The ``kiln.mechanism.v1`` block out of a note's own bytes, for a file
    whose SHA-256 is *sha256_hex*: what a copy of the file kept away from
    its folder (a design in an account) is shown with.  ``None`` for
    anything that is not this file's note."""
    try:
        if len(raw) > _MAX_NOTE_BYTES:
            return None
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("schema") != SCHEMA_VERSION:
            return None
        if not names_file(body.get("file"), sha256_hex):
            return None
        block = body.get("mechanism")
        return block if isinstance(block, dict) and block.get("kind") == KIND else None
    except Exception:  # noqa: BLE001 -- not a note reads as no mechanism
        return None


def record(file_path: str, block: dict[str, Any]) -> str | None:
    """Write the mechanism note beside *file_path*.  Returns its path, or
    ``None`` when it could not be written.  Never raises: a composed file
    is never lost for the sake of its note."""
    try:
        identity = _identity(file_path)
        if identity is None or not isinstance(block, dict) or block.get("kind") != KIND:
            return None
        note = note_path_for(file_path)
        with open(note, "w", encoding="utf-8") as fh:
            json.dump({"schema": SCHEMA_VERSION, "file": identity, "mechanism": block}, fh)
        return note
    except Exception:  # noqa: BLE001
        logger.debug("mechanism note not written for %s", file_path, exc_info=True)
        return None


def block_for(file_path: str) -> dict[str, Any] | None:
    """The ``kiln.mechanism.v1`` block beside *file_path*, or ``None``: no
    note, unreadable, another layout, or written for other bytes."""
    try:
        path = note_path_for(file_path)
        if os.path.getsize(path) > _MAX_NOTE_BYTES:
            return None
        with open(path, encoding="utf-8") as fh:
            body = json.load(fh)
        if not isinstance(body, dict) or body.get("schema") != SCHEMA_VERSION:
            return None
        if body.get("file") != _identity(file_path):
            return None
        block = body.get("mechanism")
        return block if isinstance(block, dict) and block.get("kind") == KIND else None
    except Exception:  # noqa: BLE001 -- no note reads as no mechanism
        return None


def runs_for(file_path: str) -> list[tuple[int, int]] | None:
    """Each part's runs of triangles in *file_path*, in the file's order,
    for a payload that must simplify the mesh and keep the parts apart, or
    ``None`` when the file carries no mechanism that plays."""
    block = block_for(file_path)
    parts = block.get("parts") if isinstance(block, dict) and block.get("playable") else None
    if not isinstance(parts, dict) or not parts:
        return None
    try:
        return sorted((int(run[0]), int(run[1])) for part in parts.values() for run in part["spans"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def sidecar_for_mesh(file_path: str) -> bytes | None:
    """The block as the bytes a hosted stage page is handed, or ``None``."""
    block = block_for(file_path)
    return None if block is None else json.dumps(block, sort_keys=True).encode("utf-8")


def _triangles(block: dict[str, Any]) -> int | None:
    """How many triangles the block's parts cover, or ``None`` when a part
    carries no runs to count."""
    total = 0
    parts = block.get("parts")
    if not isinstance(parts, dict) or not parts:
        return None
    for part in parts.values():
        runs = part.get("spans") if isinstance(part, dict) else None
        if not isinstance(runs, list) or not runs:
            return None
        for run in runs:
            if not isinstance(run, list) or len(run) != 2:
                return None
            total += int(run[1])
    return total


def _follow(block: dict[str, Any], kept_runs: Any, triangles: Any) -> bool:
    """Point each part's runs of triangles at where a payload simplified one
    part at a time now has them (its ``kept_runs``: ``[first, count, first
    now, count now]``), in place.  ``False``, with the block untouched, when
    any run is not accounted for or the runs do not add up to the payload's
    *triangles*."""
    parts = block.get("parts")
    if not isinstance(kept_runs, list) or not isinstance(parts, dict) or not parts:
        return False
    now: dict[tuple[int, int], list[int]] = {}
    for row in kept_runs:
        if not (isinstance(row, list) and len(row) == 4
                and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in row)):
            return False
        now[(row[0], row[1])] = [row[2], row[3]]
    moved: dict[str, list[list[int]]] = {}
    total = 0
    for part_id, part in parts.items():
        runs = part.get("spans") if isinstance(part, dict) else None
        if not isinstance(runs, list) or not runs:
            return False
        spans = []
        for run in runs:
            if not isinstance(run, list) or len(run) != 2:
                return False
            kept = now.get((run[0], run[1]))
            if kept is None or kept[1] < 1:
                return False
            spans.append(list(kept))
            total += kept[1]
        moved[part_id] = spans
    if total != triangles:
        return False
    for part_id, spans in moved.items():
        parts[part_id]["spans"] = spans
    return True


def withhold(block: dict[str, Any] | None, why: str) -> None:
    """Keep what the mechanism says on the block but play nothing, saying
    why, in place: the poses, the parts' frames and the points in the file's
    frame are dropped, since the stage's triangles are not the ones they
    were made for."""
    if not isinstance(block, dict) or not block.get("playable"):
        return
    block["playable"] = False
    block["why_not"] = why
    for key in ("poses", "parts", "clashes", "followers", "slides", "limits", "sweep"):
        block.pop(key, None)


def shift_block(block: dict[str, Any] | None, dx: float, dy: float) -> None:
    """Record the plate step's slide (mesh X and Y) on the block, in place:
    ``frame`` becomes ``"plate"`` and ``plate_offset_mm`` the slide, summed
    over repeated moves.  The block's own coordinates stay in the file's
    frame; a stage drawing the slid payload plays a part's frame about the
    file's origin and then moves the result, and every point, by the slide."""
    if not isinstance(block, dict) or (dx == 0.0 and dy == 0.0):
        return
    prior = block.get("plate_offset_mm")
    px, py = prior if isinstance(prior, list) and len(prior) == 2 else (0.0, 0.0)
    block["frame"] = "plate"
    block["plate_offset_mm"] = [round(float(px) + dx, 6), round(float(py) + dy, 6)]


def attach_to_payload(payload: dict[str, Any] | None, file_path: str) -> dict[str, Any] | None:
    """Put the block for *file_path* on a ``kiln.mesh.v1`` payload under
    ``mechanism``, in place, before the plate step slides it.  A payload
    simplified one part at a time (``kept_runs``) carries each part's
    triangles where they are now, and the mechanism plays there; one whose
    triangles are otherwise not the file's own (simplified whole, or only a
    size card) keeps what the mechanism says and plays nothing.  Never
    raises."""
    try:
        if not isinstance(payload, dict):
            return payload
        block = block_for(file_path)
        if block is None:
            return payload
        block.setdefault("frame", "mesh")
        payload["mechanism"] = block
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        if payload.get("downgraded"):
            withhold(block, "the stage shows a size card, not the mesh")
        elif payload.get("decimated_from"):
            # Simplified one part at a time, the payload says where each
            # part's triangles are now, and the block follows them: a part
            # is still that part, and moves as it did.
            if not _follow(block, payload.get("kept_runs"), counts.get("triangles")):
                withhold(block, "the stage shows a simplified mesh, so the parts' "
                                "triangles are not where the mechanism says")
            elif len(json.dumps(block)) > MAX_PAYLOAD_BLOCK_BYTES:
                withhold(block, "the mechanism's poses are too large to send to this stage")
        elif counts.get("triangles") != _triangles(block):
            withhold(block, "the stage's mesh does not have the triangles the "
                            "mechanism was played with")
        elif len(json.dumps(block)) > MAX_PAYLOAD_BLOCK_BYTES:
            withhold(block, "the mechanism's poses are too large to send to this stage")
    except Exception:  # noqa: BLE001 -- the stage draws the part either way
        logger.debug("mechanism not attached for %s", file_path, exc_info=True)
    return payload
