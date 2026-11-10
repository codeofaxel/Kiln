"""Which triangles are each part of a composed assembly, and which parts print
as one piece.

``compose_assembly`` merges an assembly's parts into one file, and the file is
triangles: which part each triangle belongs to, and which parts come off the
printer as one unit, would be lost.  This module keeps both beside the file,
the way :mod:`kiln.joint_stage` keeps its joints:

* :func:`record` writes ``<file>.assembly.json`` beside the composed mesh,
  bound to the file's bytes and written again on every compose, so a
  different file later saved under the same name is never read as this one;
* :func:`attach_guide` adds, per part, the direction it goes in and the step
  that places it, from an assembly guide;
* :func:`stage_block` gives the stage a ``kiln.assembly.v1`` block, and
  :func:`sidecar_for_mesh` / :func:`attach_to_payload` carry it to a hosted
  link and to the local stage.

Which parts print as one piece is answered by Kiln's servers when the
assembly is composed (:meth:`kiln._pro_assembly_bridge.Asking.pieces`, free
on every plan); this module checks the answer's shape (:func:`accepted`) and
keeps it.  When no answer came, the note keeps the parts and says the pieces
are not known, no block is made from it, and the 3D stage shows the file the
way it shows any other.

THE BLOCK (``kiln.assembly.v1``)
--------------------------------
::

    {
      "kind": "kiln.assembly.v1",
      "frame": "mesh",
      "total_triangles": 6612,
      "parts": [
        {"id": "box_body", "spans": [[0, 3420]], "insert": null},
        {"id": "box_lid", "spans": [[3420, 3192]],
         "insert": {"direction": [0.0, 0.0, -1.0], "step": 2}},
        ...
      ],
      "pieces": [
        {"parts": ["box_body", "box_lid"],
         "joined": [{"parts": ["box_body", "box_lid"], "why": "captive"}]},
        ...
      ],
      "kept_apart": [],
      "take_apart": [{"piece": 1, "way_out": [0.0, 0.0, 1.0]}, ...]
    }

``parts``: every part, in the file's order, with its runs of triangles
(``[first, count]``, in the file's own triangle order) and ``insert``: the
direction the part travels into place when it is put together (a unit vector
in the block's frame) and the guide step that places it, or ``null`` when no
guide gave one.  A direction may be ``null`` where the guide names the step
alone (the first part, set down).

``pieces``: what comes off the printer as one unit.  Every part is in exactly
one piece.  ``joined`` says why the parts of a piece are one: ``"touching"``
(they touch), ``"captive"`` (they do not touch and do not come apart, as with
a hinge printed in place), or ``"unread"`` (it could not be told, so they
are kept together rather than shown apart).

``kept_apart``: each pair of parts that are separate pieces because the
record declares a fit between them that is made after printing,
``{"parts": [a, b], "fit": "snap_fit"}``; with ``"gap_all_round_mm"`` when a
declared press or interference fit was found with a gap all round.

``take_apart`` (optional): the pieces in an order they come apart, the first
one off first, each as ``{"piece": <its place in pieces>, "way_out": [x, y,
z]}``: the direction it leaves in, a unit vector in the block's frame.  An
entry carries ``"fit": true`` where the piece leaves past a part the record
says it is fitted to.  A piece that is not listed has no direction on record.
Assembly reads the list from its end, with every direction reversed.

A stage pulls apart a design of more than one piece, and never one that
prints as one.

Coordinates follow :mod:`kiln.joint_stage`: millimetres, Z up, in the frame of
the geometry the block travels with.  A renderer turns a direction by the
rotation it applies to that geometry, never by its offset.

A block crosses machines, so every reader takes it as data:
:func:`checked_block` is the shape and the sizes Kiln writes.
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)

#: Bumped on a breaking change to the note's layout; an older note reads as
#: no note, never as a wrong one.
SCHEMA_VERSION = 1

#: Appended to the file's own path: ``kit.stl`` -> ``kit.stl.assembly.json``.
NOTE_SUFFIX = ".assembly.json"

#: The stage's name for the block.  A renderer reads only a kind it knows.
STAGE_KIND = "kiln.assembly.v1"

#: Why the parts of a piece are one.
JOINED_WHY = ("touching", "captive", "unread")

#: The fits a kept-apart pair may name: the assembly record's joint types
#: that are made after printing, and hardware.
KEPT_APART_FITS = frozenset({
    "press_fit", "interference_fit", "snap_fit", "threaded", "glued", "magnetic", "fastener",
})
#: The kept-apart fits whose parts share volume when made.
PRESSED_FITS = frozenset({"press_fit", "interference_fit"})

#: The most parts, and runs of triangles, a block carries.  Far past any
#: assembly Kiln composes; a bound for data from another machine, not a cap
#: on what a stage shows.
MAX_PARTS = 2048
MAX_SPANS = 8192
MAX_TEXT = 4000
MAX_STEP = 100_000

#: The largest block sent beside a served file, bytes.
MAX_SIDECAR_BYTES = 1_048_576
#: The record is handed back in the compose reply when it is no larger than
#: this as JSON; a larger one stays beside the file.
MAX_REPLY_RECORD_CHARS = 32_000

_MAX_NOTE_BYTES = 8 * 1024 * 1024


def note_path_for(file_path: str) -> str:
    return f"{file_path}{NOTE_SUFFIX}"


def _identity(file_path: str) -> str | None:
    from kiln.preview_gate import hash_file

    digest = hash_file(file_path)
    return None if digest.startswith("NO_FILE:") else digest


# ---------------------------------------------------------------------------
# The pieces, as answered
# ---------------------------------------------------------------------------


def accepted(answer: Any, part_ids: list[str]) -> dict[str, Any] | None:
    """*answer* as ``{"pieces": [...], "kept_apart": [...]}`` when it names
    exactly *part_ids*, each in one piece, in the shape this module writes;
    else ``None``.  An answer comes over a wire, so it is read as data."""
    if not isinstance(answer, dict):
        return None
    pieces, kept = answer.get("pieces"), answer.get("kept_apart", [])
    named = _pieces_ok(pieces)
    if named is None or set(named) != set(part_ids) or len(named) != len(part_ids):
        return None
    if not _kept_apart_ok(kept, named):
        return None
    out = {"pieces": pieces, "kept_apart": kept}
    if "take_apart" in answer:
        if not _take_apart_ok(answer["take_apart"], len(pieces)):
            return None
        out["take_apart"] = answer["take_apart"]
    return out


# ---------------------------------------------------------------------------
# The note
# ---------------------------------------------------------------------------


def record(
    file_path: str,
    assembly: Any,
    spans: dict[str, tuple[int, int]],
    *,
    total_triangles: int,
    pieces: Any = None,
    pieces_why_not: str | None = None,
) -> dict[str, Any] | None:
    """Write the note beside *file_path* for an assembly of two or more parts.

    *pieces* is the answer to which parts print as one piece
    (:meth:`kiln._pro_assembly_bridge.Asking.pieces`), checked here; when it
    is missing or not in shape the note keeps the parts and *pieces_why_not*,
    and no stage block is made from it.  Returns what the compose reply says
    (the part and piece counts, why parts were kept together and how many
    pieces have a way to come apart on record, with the record itself as
    ``record`` when the kit is more than one piece and the record is small
    enough for a reply; or why the pieces are not known), or ``None`` when there is nothing to write or it could not be
    written.  Never raises: a composed file is never lost for
    the sake of its note.
    """
    try:
        parts = [p for p in assembly.parts if p.part_id in spans]
        if len(parts) < 2:
            return None
        identity = _identity(file_path)
        if identity is None:
            return None
        read = accepted(pieces, [p.part_id for p in parts])
        why_not = None
        if read is None:
            why_not = (pieces_why_not or "The answer did not name this assembly's parts, so the pieces are left unsaid.")[:MAX_TEXT]
        body = {
            "schema": SCHEMA_VERSION,
            "file": identity,
            "total_triangles": int(total_triangles),
            "parts": [
                {"id": p.part_id, "spans": [list(spans[p.part_id])], "insert": None}
                for p in parts
            ],
            "pieces": read["pieces"] if read else None,
            "kept_apart": read["kept_apart"] if read else [],
        }
        if read and read.get("take_apart"):
            body["take_apart"] = read["take_apart"]
        if why_not:
            body["pieces_why_not"] = why_not
        _write(file_path, body)
        if read is None:
            return {"parts": len(parts), "pieces": None, "pieces_why_not": why_not}
        why: dict[str, int] = {}
        for piece in read["pieces"]:
            for pair in piece["joined"]:
                why[pair["why"]] = why.get(pair["why"], 0) + 1
        said: dict[str, Any] = {
            "parts": len(parts),
            "pieces": len(read["pieces"]),
            "kept_together": why,
            "kept_apart_by_fit": len(read["kept_apart"]),
        }
        if len(read["pieces"]) > 1:
            # How the pieces come apart, in counts: those with a way out on
            # record, the one the others come off (when every other piece has
            # a way out), and any with none.
            listed = len(read.get("take_apart") or [])
            stays = 1 if listed == len(read["pieces"]) - 1 else 0
            said["take_apart"] = {
                "with_a_way_out": listed,
                "stays_put": stays,
                "no_way_on_record": len(read["pieces"]) - listed - stays,
            }
            # The record itself, so whoever composed the kit can hand it on
            # (a guide's pictures then come apart as the stage's do).
            block = stage_block(file_path)
            if block is not None and len(json.dumps(block)) <= MAX_REPLY_RECORD_CHARS:
                said["record"] = block
        checks = [
            f"The {pair['fit'].replace('_', ' ')} declared between {pair['parts'][0]} and {pair['parts'][1]} "
            f"has a {pair['gap_all_round_mm']:.2f} mm gap all round, so it cannot be pressed; they are kept "
            "apart as declared. If they print together as one, declare a clearance fit instead."
            for pair in read["kept_apart"]
            if pair.get("gap_all_round_mm") is not None and pair["fit"] in PRESSED_FITS
        ]
        if checks:
            said["check"] = checks
        return said
    except Exception:  # noqa: BLE001
        logger.debug("assembly note not written for %s", file_path, exc_info=True)
        return None


def _write(file_path: str, body: dict[str, Any]) -> None:
    with open(note_path_for(file_path), "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2)


def _body(file_path: str) -> dict[str, Any] | None:
    """The note's body, or ``None``: no note, unreadable, another layout, or
    written for different bytes than the file now holds."""
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
        if not isinstance(body.get("parts"), list):
            return None
        return body
    except Exception:  # noqa: BLE001 -- no note reads as no record
        return None


def _unit(value: Any) -> list[float] | None:
    if not (isinstance(value, (list, tuple)) and len(value) == 3 and all(_finite(c) for c in value)):
        return None
    length = math.sqrt(sum(float(c) * float(c) for c in value))
    if length < 1e-9:
        return None
    return [float(c) / length for c in value]


def attach_guide(file_path: str, steps: list[dict[str, Any]]) -> bool:
    """Keep, per part, the direction it goes in and the step that places it,
    from an assembly guide's steps (``{"part", "direction", "step"}``; the
    first step naming a part is its own).  A part the note does not have is
    skipped.  Returns whether the note was updated.  Never raises."""
    try:
        body = _body(file_path)
        if body is None:
            return False
        by_id = {p.get("id"): p for p in body["parts"] if isinstance(p, dict)}
        seen: set[str] = set()
        changed = False
        for entry in steps:
            if not isinstance(entry, dict):
                continue
            part = by_id.get(entry.get("part"))
            step = entry.get("step")
            if part is None or part["id"] in seen or not (_count(step) and step <= MAX_STEP):
                continue
            seen.add(part["id"])
            raw = entry.get("direction")
            direction = _unit(raw)
            if raw is not None and direction is None:
                continue
            part["insert"] = {"direction": direction, "step": int(step)}
            changed = True
        if changed:
            _write(file_path, body)
        return changed
    except Exception:  # noqa: BLE001
        logger.debug("assembly guide not kept for %s", file_path, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# The stage block
# ---------------------------------------------------------------------------


def stage_block(file_path: str) -> dict[str, Any] | None:
    """The ``kiln.assembly.v1`` block for *file_path*, in the file's own
    frame, or ``None`` when the file carries no record or its record does
    not say which parts print as one piece."""
    body = _body(file_path)
    if body is None or body.get("pieces") is None:
        return None
    block = {
        "kind": STAGE_KIND,
        "frame": "mesh",
        "total_triangles": body.get("total_triangles"),
        "parts": body["parts"],
        "pieces": body.get("pieces"),
        "kept_apart": body.get("kept_apart", []),
    }
    if body.get("take_apart"):
        block["take_apart"] = body["take_apart"]
    return checked_block(block)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _insert_ok(insert: Any) -> bool:
    if insert is None:
        return True
    if not isinstance(insert, dict):
        return False
    step = insert.get("step")
    if not (_count(step) and step <= MAX_STEP):
        return False
    direction = insert.get("direction")
    if direction is None:
        return True
    unit = _unit(direction)
    return unit is not None and all(abs(a - float(b)) < 1e-6 for a, b in zip(unit, direction, strict=True))


def checked_block(block: Any) -> dict[str, Any] | None:
    """*block* when it has the shape and the sizes Kiln writes, else ``None``.

    A run of triangles past the file's end, two parts claiming one triangle,
    a direction that is not a unit vector, a part in no piece or in two, a
    reason Kiln does not give, a kept-apart pair inside one piece, a piece
    taken apart twice or one that is not there, or more parts, runs or text
    than Kiln writes, and the whole block is refused: it was not written by
    Kiln, or not for this file.
    """
    if not isinstance(block, dict) or block.get("kind") != STAGE_KIND:
        return None
    if not isinstance(block.get("frame"), str) or len(block["frame"]) > MAX_TEXT:
        return None
    total = block.get("total_triangles")
    parts = block.get("parts")
    if not _count(total) or not isinstance(parts, list) or len(parts) > MAX_PARTS:
        return None
    named = _pieces_ok(block.get("pieces"))
    if named is None or not _kept_apart_ok(block.get("kept_apart", []), named):
        return None
    if "take_apart" in block and not _take_apart_ok(block["take_apart"], len(block["pieces"])):
        return None
    if not parts:
        why = block.get("parts_why_not")
        return block if isinstance(why, str) and 0 < len(why) <= MAX_TEXT else None
    if len(parts) < 2:
        return None
    runs: list[tuple[int, int]] = []
    ids: set[str] = set()
    for part in parts:
        if not isinstance(part, dict):
            return None
        pid = part.get("id")
        if not isinstance(pid, str) or not pid or len(pid) > MAX_TEXT or pid in ids:
            return None
        ids.add(pid)
        spans = part.get("spans")
        if not (isinstance(spans, list) and spans):
            return None
        for span in spans:
            if not (isinstance(span, list) and len(span) == 2 and _count(span[0]) and _count(span[1])):
                return None
            if span[1] < 1 or span[0] + span[1] > total:
                return None
            runs.append((span[0], span[1]))
        if not _insert_ok(part.get("insert")):
            return None
    if len(runs) > MAX_SPANS or ids != set(named):
        return None
    runs.sort()
    for (a, n), (b, _m) in zip(runs, runs[1:], strict=False):
        if b < a + n:
            return None
    return block


def _kept_apart_ok(kept: Any, piece_of: dict[str, int]) -> bool:
    """Each pair a declared fit kept apart: two named parts in different
    pieces, a fit Kiln names, and a gap only as a non-negative number."""
    if not isinstance(kept, list) or len(kept) > MAX_SPANS:
        return False
    for pair in kept:
        if not isinstance(pair, dict) or pair.get("fit") not in KEPT_APART_FITS:
            return False
        two = pair.get("parts")
        if not (isinstance(two, list) and len(two) == 2 and all(isinstance(p, str) and p in piece_of for p in two)):
            return False
        if piece_of[two[0]] == piece_of[two[1]]:
            return False
        gap = pair.get("gap_all_round_mm")
        if gap is not None and not (_finite(gap) and gap >= 0):
            return False
    return True


def _take_apart_ok(order: Any, pieces: int) -> bool:
    """The pieces in the order they come apart: each a piece that is there,
    listed once, with a unit direction; ``fit`` only as true or false."""
    if not isinstance(order, list) or len(order) > pieces:
        return False
    seen: set[int] = set()
    for entry in order:
        if not isinstance(entry, dict):
            return False
        piece = entry.get("piece")
        if not _count(piece) or piece >= pieces or piece in seen:
            return False
        seen.add(piece)
        way = entry.get("way_out")
        unit = _unit(way)
        if unit is None or any(abs(a - float(b)) > 1e-6 for a, b in zip(unit, way, strict=True)):
            return False
        if "fit" in entry and not isinstance(entry["fit"], bool):
            return False
    return True


def _pieces_ok(pieces: Any) -> dict[str, int] | None:
    """Every part named in exactly one piece, each joined pair inside its
    piece with a reason Kiln gives; each name's piece, or ``None``."""
    if not (isinstance(pieces, list) and 1 <= len(pieces) <= MAX_PARTS):
        return None
    named: dict[str, int] = {}
    for index, piece in enumerate(pieces):
        if not isinstance(piece, dict):
            return None
        members = piece.get("parts")
        if not (isinstance(members, list) and members):
            return None
        for pid in members:
            if not isinstance(pid, str) or not pid or len(pid) > MAX_TEXT or pid in named:
                return None
            if len(named) >= MAX_PARTS:
                return None
            named[pid] = index
        joined = piece.get("joined")
        if not isinstance(joined, list) or len(joined) > MAX_SPANS:
            return None
        inside = set(members)
        for pair in joined:
            if not isinstance(pair, dict) or pair.get("why") not in JOINED_WHY:
                return None
            two = pair.get("parts")
            if not (isinstance(two, list) and len(two) == 2 and all(isinstance(p, str) for p in two)
                    and set(two) <= inside):
                return None
    return named


def runs_for(file_path: str) -> list[tuple[int, int]] | None:
    """Each part's run of triangles in *file_path*, in the file's order, for
    a payload that must simplify the mesh to keep the parts apart
    (:func:`kiln.mesh_payload.mesh_to_viewer_payload`'s ``keep_runs``), or
    ``None`` when the file carries no record."""
    block = stage_block(file_path)
    if block is None:
        return None
    return sorted((int(first), int(count)) for part in block["parts"] for first, count in part["spans"])


def _moved_parts(block: dict[str, Any], kept_runs: Any) -> list[dict[str, Any]] | None:
    """The block's parts with each run of triangles where a simplified
    payload now has it (its ``kept_runs``), or ``None`` when any run is not
    accounted for."""
    if not isinstance(kept_runs, list):
        return None
    now: dict[tuple[int, int], list[int]] = {}
    for row in kept_runs:
        if not (isinstance(row, list) and len(row) == 4 and all(_count(v) for v in row)):
            return None
        now[(row[0], row[1])] = [row[2], row[3]]
    parts = []
    for part in block["parts"]:
        spans = [now.get((span[0], span[1])) for span in part["spans"]]
        if any(span is None or span[1] < 1 for span in spans):
            return None
        parts.append({**part, "spans": spans})
    return parts


def sidecar_for_mesh(file_path: str) -> bytes | None:
    """The block for *file_path* as the JSON a served copy of the file
    carries beside it, in the file's own frame, or ``None`` when the file
    carries no record or the record is too large to send."""
    block = stage_block(file_path)
    if block is None:
        return None
    data = json.dumps(block, sort_keys=True).encode("utf-8")
    return data if len(data) <= MAX_SIDECAR_BYTES else None


def attach_to_payload(payload: dict[str, Any] | None, file_path: str) -> dict[str, Any] | None:
    """Put the block for *file_path* on a ``kiln.mesh.v1`` payload, in place.
    The pieces always travel.  A payload simplified one part at a time
    (``kept_runs``) carries each part's triangles where they are now; one
    whose triangles are otherwise not the file's own (simplified whole, or
    only a size card) carries no runs of triangles, and says why.  Never
    raises."""
    try:
        if not isinstance(payload, dict):
            return payload
        block = stage_block(file_path)
        if block is None:
            return payload
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        why = None
        if payload.get("downgraded"):
            why = "the stage shows a size card, not the mesh"
        elif payload.get("decimated_from"):
            # Simplified one part at a time, the payload says where each
            # part's triangles are now, and the block follows them.
            moved = _moved_parts(block, payload.get("kept_runs"))
            followed = None
            if moved is not None:
                followed = checked_block({**block, "parts": moved, "total_triangles": counts.get("triangles")})
            if followed is not None:
                block = followed
            else:
                why = "the stage shows a simplified mesh, so the parts' triangles are not where the record says"
        elif counts.get("triangles") != block.get("total_triangles"):
            why = "the stage's mesh does not have the triangles the record was made from"
        if why:
            block = {**block, "parts": [], "parts_why_not": why}
        payload["assembly"] = block
    except Exception:  # noqa: BLE001 -- the stage draws the part either way
        logger.debug("assembly record not attached for %s", file_path, exc_info=True)
    return payload


def turned(block: dict[str, Any] | None) -> None:
    """The stage shows the part turned (the slicer's placement): the
    directions no longer hold in the block's frame, so they go (each part's
    way in, and the order the pieces come apart in with its ways out), and
    each part keeps its step."""
    if not isinstance(block, dict):
        return
    block.pop("take_apart", None)
    for part in block.get("parts") or []:
        insert = part.get("insert") if isinstance(part, dict) else None
        if isinstance(insert, dict):
            insert["direction"] = None
