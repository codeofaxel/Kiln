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


def note_path_for(file_path: str) -> str:
    return f"{file_path}.mechanism.json"


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


def sidecar_for_mesh(file_path: str) -> bytes | None:
    """The block as the bytes a hosted stage page is handed, or ``None``."""
    block = block_for(file_path)
    return None if block is None else json.dumps(block, sort_keys=True).encode("utf-8")
