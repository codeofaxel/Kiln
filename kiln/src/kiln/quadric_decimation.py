"""Quadric edge-collapse decimation, run in a process of its own.

:func:`decimate` collapses a mesh's edges, cheapest quadric error first, until
about the triangle count asked for remains (the ``fast-simplification``
package).  That package keeps the last mesh it was given in module state for
the life of the process, so each call runs in a child interpreter
(:mod:`kiln.child_interpreter`): the memory goes back when the child ends,
and calls made at the same time never share that state.

This file is the child's script too; below the line it imports numpy and the
package alone.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from typing import Any

#: How long one decimation may take before it is stopped.
_CHILD_TIMEOUT_S = 180


class DecimationStopped(Exception):
    """The decimation gave no result; the message says why and ``cause``
    (:mod:`kiln.child_interpreter`'s words) which way it stopped."""

    cause: str | None = None


def available() -> bool:
    """Whether the decimation package is installed."""
    return importlib.util.find_spec("fast_simplification") is not None


def decimate(vertices: Any, faces: Any, count: int) -> tuple[Any, Any]:
    """``(vertices, faces)`` of the mesh collapsed to about *count* triangles.

    :raises DecimationStopped: the child was stopped, crashed or refused.
    """
    import numpy as np

    from kiln.child_interpreter import run_in_child

    workdir = tempfile.mkdtemp(prefix="kiln_decimate_")
    try:
        paths = {name: os.path.join(workdir, f"{name}.npy") for name in ("vertices", "faces", "out_vertices", "out_faces")}
        np.save(paths["vertices"], np.ascontiguousarray(vertices, dtype=np.float64))
        index = np.int32 if len(vertices) < np.iinfo(np.int32).max else np.int64
        np.save(paths["faces"], np.ascontiguousarray(faces, dtype=index))
        run_in_child(
            __file__, {**paths, "count": int(count)},
            timeout_s=_CHILD_TIMEOUT_S, what="the simplifier", error=DecimationStopped,
        )
        return np.load(paths["out_vertices"]), np.load(paths["out_faces"]).astype(np.int64)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# The child
# ---------------------------------------------------------------------------


def _child(request: dict[str, Any]) -> dict[str, Any]:
    import fast_simplification
    import numpy as np

    points, faces = fast_simplification.simplify(
        np.load(request["vertices"]), np.load(request["faces"]), target_count=request["count"],
    )
    np.save(request["out_vertices"], np.asarray(points, dtype=np.float64))
    np.save(request["out_faces"], np.asarray(faces))
    return {"triangles": int(len(faces))}


if __name__ == "__main__":
    import json

    with open(sys.argv[1], encoding="utf-8") as _fh:
        print(json.dumps(_child(json.load(_fh))))
