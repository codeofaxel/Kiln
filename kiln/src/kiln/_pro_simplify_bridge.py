"""Public-Kiln -> kiln-pro bridge for one thing: simplifying a mesh.

``simplify_mesh_model`` reduces a mesh's triangle count while keeping the
part, and Kiln's servers do the work (kiln-pro, https://kiln3d.com), free on
every plan.  With kiln-pro installed here (Kiln's servers) it runs in this
process's care.  On a signed-in install without it, the file goes up, the
same tool runs on Kiln's servers, and the simplified file comes back to
where the call asked for it (:mod:`kiln.served_public`).

This computer has no simplifier of its own.  Signed out, offline, the
servers silent or saying no, nothing is written, the file given is left as
it was, and the answer says which of the four it was and what fixes it
(:mod:`kiln.served_answer`).  A file that is not there is said here, without
asking anyone.
"""

from __future__ import annotations

import os
from typing import Any

#: The tool Kiln's servers run for an install without kiln-pro.
WIRE_TOOL = "simplify_mesh_model"
#: Where the same work is done when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.part_simplify"
#: The code on an answer that says the servers were not reached.
NOT_SIMPLIFIED_CODE = "MESH_NOT_SIMPLIFIED"

__all__ = ["LOCAL_MODULE", "NOT_SIMPLIFIED_CODE", "WIRE_TOOL", "simplify"]


def simplify(file_path: str, *, target_ratio: float = 0.5, output_path: str = "") -> dict[str, Any]:
    """Simplify the mesh at *file_path*; the reply ``simplify_mesh_model`` gives.

    ``success`` True with ``path`` and the measured facts, or ``success``
    False with a sentence saying why nothing was written: the simplifier's
    own refusal (``code`` and ``message``), or a miss worded with its cause
    (``error``, ``why``).

    :raises kiln.mesh_errors.MeshFileMissing: no file at *file_path*.
    """
    if not os.path.isfile(file_path):
        from kiln.mesh_errors import MeshFileMissing

        raise MeshFileMissing(f"No file at {file_path}")
    try:
        from kiln_pro.part_simplify import simplify_file
    except ImportError:
        return _served(file_path, target_ratio, output_path)
    return simplify_file(file_path, target_ratio=target_ratio, output_path=output_path or None)


def _served(file_path: str, target_ratio: float, output_path: str) -> dict[str, Any]:
    """The servers' answer, or the miss in words."""
    from kiln import served_public
    from kiln.served_answer import fields, sentence

    answer, miss = served_public.answer_or_missed(
        WIRE_TOOL, {"file_path": file_path, "target_ratio": target_ratio, "output_path": output_path},
    )
    if answer is not None:
        return answer
    name = os.path.basename(file_path) or "this file"
    out: dict[str, Any] = {
        "status": "error",
        "success": False,
        "error": sentence(
            miss,
            feature="servers",
            on_the_line="Kiln simplifies a mesh on its servers, free on every plan",
            cannot=f"simplify {name}",
            wont=f"wrote nothing and left {name} as it was",
            safe_remedy="Your slicer or the program that made the file can simplify it too",
        ),
        "code": miss.code or NOT_SIMPLIFIED_CODE,
        "retryable": miss.cause != "refused",
        **fields(miss),
    }
    if miss.cause == "signed_out":
        from kiln.tiers_and_terms import signin_hint_fields

        out.update(signin_hint_fields())
    return out
