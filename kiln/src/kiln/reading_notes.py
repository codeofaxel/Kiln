"""What reading a file showed about it, said once in the reply of the call.

A file in the wrong unit is the commonest thing wrong with a real export: a
scene exported in metres, the default of the programs that write most of
them, reads as a part a thousandth of its size.  The judgement of what a size
says about units is one function (:func:`kiln.generation.validation.unit_verdict`),
but a door says it only if the door asks, and the doors that read a mesh
number in the dozens.  Most of them reported a 0.088 mm bounding box and said
nothing else.

So the asking moved to the readers.  Kiln's mesh readers note the size of
every file they read (:func:`note_size`); a size below anything a printer
makes becomes the units sentence, kept on the running call
(:func:`kiln.tool_context.call_notes`).  The tool-dispatch chokepoint hands
each reply to :func:`attach`, which adds the sentences the reply does not
already say under ``units_question``, once each.  A door that states the units
question in its own words (the printability report, the validation pipeline,
the slicer) names the size it is about, and that is how :func:`attach` knows
it was said.

The sentence is about the file as it arrived and says nothing about what the
door then did, so it is true on every door it reaches.  Outside a tool call
nothing is collected and nothing is measured.

The second thing a reader knows that its door may not say: a door that edits
a mesh writes STL, so editing a GLB, OBJ, 3MF or STEP "in place" writes the
result beside it as ``.stl`` (:func:`note_written`), and the reply says which
file that is under ``written_as``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

#: The reply key the sentences go under: the name the printability report
#: already gives the same judgement.
REPLY_KEY = "units_question"

#: The reply key for where an edit of a non-STL file was written.
WRITTEN_KEY = "written_as"

#: A format's name as a person knows it, by suffix.
_FORMAT_NAMES = {
    ".glb": "glTF", ".gltf": "glTF", ".obj": "OBJ", ".3mf": "3MF", ".step": "STEP", ".stp": "STEP",
}


@dataclass(frozen=True)
class _UnitsNote:
    largest_mm: float
    sentence: str

    @property
    def said_as(self) -> str:
        """The text a reply that already says this question contains: every
        units sentence names the size it is about this way."""
        return f"{self.largest_mm:g}mm"


@dataclass(frozen=True)
class _WrittenNote:
    requested: str
    written: str

    @property
    def sentence(self) -> str:
        suffix = os.path.splitext(self.requested)[1].lower()
        fmt = _FORMAT_NAMES.get(suffix, suffix.lstrip(".").upper())
        kept = (
            "your STEP file is unchanged" if fmt == "STEP"
            else f"{os.path.basename(self.requested)} is unchanged, and its colours and materials are not carried"
        )
        return (
            f"Written as STL to {self.written}, the format every slicer reads, rather than as "
            f"{fmt} over {os.path.basename(self.requested)}: {kept}."
        )


def _collecting() -> list[Any] | None:
    try:
        from kiln.tool_context import call_notes
    except ImportError:  # pragma: no cover - the module ships beside this one
        return None
    return call_notes()


def note_size(path: Any, vertices: Any) -> None:
    """Note what the size of a file just read says about its units.

    *vertices* is anything ``numpy.asarray`` reads as points.  Nothing is
    measured outside a tool call, and a file that reads as a printable size
    leaves no note.
    """
    notes = _collecting()
    if notes is None:
        return
    try:
        import numpy as np

        points = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
        if not len(points):
            return
        largest = float((points.max(axis=0) - points.min(axis=0)).max())
    except Exception:  # noqa: BLE001 - a size nobody can measure says nothing
        return
    note_largest(path, largest)


def note_largest(path: Any, largest_mm: float) -> None:
    """:func:`note_size` for a size already measured."""
    notes = _collecting()
    if notes is None:
        return
    from kiln.generation.validation import unit_verdict

    name = os.path.basename(str(path)) or "this file"
    sentence = unit_verdict(largest_mm).describe_reading(name)
    if not sentence:
        return
    note = _UnitsNote(largest_mm, sentence)
    # The same size read twice (the input, then a copy written from it) is
    # one question.
    if any(isinstance(n, _UnitsNote) and n.said_as == note.said_as for n in notes):
        return
    notes.append(note)


def note_written(requested: str, written: str) -> None:
    """Note that a result asked for at *requested* (another format's path) was
    written as STL at *written*."""
    notes = _collecting()
    if notes is not None:
        notes.append(_WrittenNote(str(requested), str(written)))


def _texts(node: Any) -> list[str]:
    out: list[str] = []
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return out


def attach(reply: Any) -> Any:
    """*reply* with the units sentences it does not already say, or *reply*
    unchanged.  Only a dict can carry them."""
    notes = _collecting()
    if not notes or not isinstance(reply, dict):
        return reply
    added: dict[str, str] = {}
    units = [n for n in notes if isinstance(n, _UnitsNote)]
    if units:
        text = "\n".join(_texts(reply))
        unsaid = [n.sentence for n in units if n.said_as not in text]
        if unsaid:
            added[REPLY_KEY] = "  ".join(unsaid)
    written = list(dict.fromkeys(n.sentence for n in notes if isinstance(n, _WrittenNote)))
    if written:
        added[WRITTEN_KEY] = "  ".join(written)
    return {**reply, **added} if added else reply
