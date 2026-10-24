"""The marks Kiln carved into or raised onto a mesh, kept beside it.

A mesh's record lives at ``<mesh>.marks.json``.  A compiler that built the
mesh from a source names that source (:func:`note_source`).  Every emboss or
deboss compiled onto a mesh copies its input's record forward and adds its own
mark (:func:`note_mark`), so the record beside a decorated mesh says which
source it began as and every mark since, in order.
:func:`kiln.scad_main.with_marks` turns that into one source that compiles to
the decorated mesh.

A mark is ``{"op", "scad", "kind"}``: the boolean (``"difference"`` for a
deboss, ``"union"`` for an emboss), the OpenSCAD statement that draws the
mark in the mesh's own frame, and what it is (``"text"``, ``"svg"``,
``"heightmap"``).  Recording is best-effort: a record that cannot be read or
written is absent, never an error, and an absent record only means nobody can
say what made the mesh.  Pinned by ``tests/test_mesh_marks.py``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

SUFFIX = ".marks.json"


def _record_path(mesh_path: str) -> Path:
    return Path(f"{mesh_path}{SUFFIX}")


def read(mesh_path: str) -> dict[str, Any] | None:
    """The record beside *mesh_path*, or ``None`` when there is none."""
    try:
        data = json.loads(_record_path(mesh_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("marks"), list):
        return None
    return data


def _write(mesh_path: str, record: dict[str, Any]) -> None:
    try:
        _record_path(mesh_path).write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        _logger.debug("mesh_marks: record not written for %s", mesh_path, exc_info=True)


def note_source(mesh_path: str, source_path: str) -> None:
    """Record that *mesh_path* is *source_path* compiled, with no marks yet."""
    _write(mesh_path, {"source": os.path.abspath(source_path), "marks": []})


def note_mark(input_mesh: str, output_mesh: str, mark: dict[str, Any]) -> None:
    """Record that *output_mesh* is *input_mesh* with *mark* applied.

    The input's record is carried forward; an input with none starts the
    record with no source, which still lists the marks but names nothing
    they can be applied to.
    """
    before = read(input_mesh) or {"source": None, "marks": []}
    entry = {key: mark[key] for key in ("op", "scad", "kind") if key in mark}
    _write(output_mesh, {"source": before.get("source"), "marks": [*before["marks"], entry]})
