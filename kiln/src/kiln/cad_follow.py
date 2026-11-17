"""A part's CAD file follows a resize, a turn or a mirror of its mesh.

Some changes to a part move every point of it the same way: an even resize,
a turn, a mirror, a move.  When the part's CAD file is at hand (the STEP file
a Kiln-converted mesh came from, sitting beside it), the same change is made
to the exact solid, and the changed CAD is put beside the changed mesh.  The
part then stays a CAD part: the next edit that works on CAD finds it.

CONTRACT
- :func:`watch` is called BEFORE a door changes a mesh, because the door may
  overwrite its input.  It reads the input's size and finds its CAD file; it
  never raises and never changes a file.
- :meth:`Watched.follow` is called AFTER the door wrote its output.  It
  returns the reply fields to merge in: ``step_path``, ``cad_kept`` True and
  ``cad_change`` (the map from the CAD as it was to the CAD as it is, and
  the hash of the CAD it was applied to) when the CAD followed; ``cad_kept`` False and a ``cad_note`` sentence when
  the part had CAD and this change could not be made to it; nothing at all
  for a part with no CAD.  It never raises: the mesh edit stands either way.
- The CAD is only kept when the changed solid measures the same as the
  changed mesh, is a valid solid, and holds the volume the change implies.

INVARIANT (pinned by ``tests/test_cad_follows_the_change.py``): a change that
is not the same in every direction (a stretch along one axis) never keeps
the CAD, and a result that does not match the mesh is never kept.

The kernel runs in a child interpreter (:mod:`kiln.child_interpreter`), as
Kiln's other CAD work does.  This file is that child's script too.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CHILD_TIMEOUT_S = 120
#: The changed solid's box and the changed mesh's box agree within this, mm.
_SAME_BOX_MM = 0.05
#: A resize is even when its three axes agree within this share.
_EVEN_SCALE_TOLERANCE = 1e-6
#: The volume the change implies, within this share.
_VOLUME_TOLERANCE = 1e-6

_NOT_EVEN = (
    "This change stretched the part differently along different axes, which round holes "
    "and curved faces do not survive as exact CAD, so it was made on the mesh only and "
    "this file has no CAD. An even resize keeps the CAD."
)
_NOT_FOLLOWED = (
    "This change was made on the mesh; the part's CAD file could not be changed the same "
    "way ({why}), so this file has no CAD. The CAD of the part as it was is unchanged."
)

_IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
_MIRRORS = {
    "x": ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "y": ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "z": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0)),
}


class CadFollowError(RuntimeError):
    """The kernel could not make this change to the solid; the message says why."""


@dataclass(frozen=True)
class Watched:
    """A mesh as it was before a door changed it, with its CAD file if it has one."""

    source: str | None
    bounds: tuple[tuple[float, float, float], tuple[float, float, float]] | None

    def follow(
        self,
        output_path: str,
        *,
        turn: Any = None,
        mirror: str | None = None,
        resized: bool = False,
    ) -> dict[str, Any]:
        """Make the door's change to the CAD and keep it beside *output_path*.

        *turn* is the 3x3 rotation the door applied (rows), *mirror* the axis
        it reflected along, *resized* whether it changed the size.  A door
        that only moved the part passes none of them.
        """
        if self.source is None or self.bounds is None:
            return {}
        try:
            return self._follow(output_path, turn=turn, mirror=mirror, resized=resized)
        except CadFollowError as exc:
            return {"cad_kept": False, "cad_note": _NOT_FOLLOWED.format(why=str(exc))}
        except Exception:  # noqa: BLE001 -- the mesh edit stands whatever happens here
            logger.warning("the CAD did not follow the change to %s", output_path, exc_info=True)
            return {"cad_kept": False, "cad_note": _NOT_FOLLOWED.format(why="the CAD kernel stopped")}

    def _follow(self, output_path: str, *, turn: Any, mirror: str | None, resized: bool) -> dict[str, Any]:
        from kiln.child_interpreter import run_in_child
        from kiln.step_import import keep_step_beside

        assert self.source is not None and self.bounds is not None
        after = _bounds(output_path)
        if after is None:
            raise CadFollowError("the changed mesh could not be read")
        scale = 1.0
        if resized:
            scale = _even_scale(self.bounds, after)
            if scale is None:
                return {"cad_kept": False, "cad_note": _NOT_EVEN}
        linear = [list(row) for row in (turn if turn is not None else _IDENTITY)]
        if mirror is not None:
            flip = _MIRRORS[mirror.lower()]
            linear = [[sum(flip[i][k] * linear[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

        scratch = tempfile.mkdtemp(prefix="kiln_cad_follow_")
        try:
            scratch_step = os.path.join(scratch, "followed.step")
            made = run_in_child(
                __file__,
                {
                    "step": os.path.abspath(self.source),
                    "out_step": scratch_step,
                    "linear": linear,
                    "scale": scale,
                    "box": [list(after[0]), list(after[1])],
                    "same_box_mm": _SAME_BOX_MM,
                    "volume_tolerance": _VOLUME_TOLERANCE,
                },
                timeout_s=_CHILD_TIMEOUT_S,
                what="the CAD kernel",
                error=CadFollowError,
            )
            plain = Path(output_path).with_suffix(".step")
            over_its_own_cad = plain.exists() and os.path.samefile(plain, self.source)
            step_path, beside = keep_step_beside(scratch_step, output_path, self.source)
            if over_its_own_cad:
                # The file left alone is the part's own CAD as it was: nothing to explain.
                beside = ""
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if not step_path:
            raise CadFollowError("the changed CAD could not be written")
        _mark_as_converted(output_path)
        return {
            "cad_kept": True,
            "step_path": step_path,
            # The one map that takes the part's CAD as it was to the CAD beside
            # the changed mesh: a 4x4, row by row, in mm.
            "cad_change": {
                "kind": "placed",
                "matrix": [v for row in made["matrix"] for v in row],
                "scale": made["scale"],
                "mirrored": mirror is not None,
                "from_sha256": _sha256(self.source),
            },
            "cad_note": f"The part's exact CAD followed this change: {os.path.basename(step_path)}, beside the mesh.{beside}",
        }


def watch(mesh_path: str, *, given: str | None = None) -> Watched:
    """What a door needs to remember about *mesh_path* before it changes it.

    *given* is the file the caller handed the door, when the door converted
    it to *mesh_path* itself: a STEP file given directly is the part's CAD.
    """
    try:
        from kiln.step_import import is_step_file, step_converted_from

        if is_step_file(mesh_path) or not os.path.isfile(mesh_path):
            return Watched(None, None)
        if given is not None and is_step_file(given) and os.path.isfile(given):
            source: str | None = given
        else:
            source = step_converted_from(mesh_path)
        if source is None:
            return Watched(None, None)
        return Watched(source, _bounds(mesh_path))
    except Exception:  # noqa: BLE001 -- a part with no readable CAD is a mesh part
        logger.debug("no CAD watched for %s", mesh_path, exc_info=True)
        return Watched(None, None)


def _bounds(mesh_path: str) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    try:
        from kiln.mesh_frame import load_mesh

        low, high = load_mesh(mesh_path, force="mesh").bounds
        return (tuple(float(v) for v in low), tuple(float(v) for v in high))  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 -- unreadable is "no size", said by the caller
        return None


def _even_scale(before: Any, after: Any) -> float | None:
    """The one factor *before* was resized by to give *after*, or ``None`` when its axes disagree."""
    ratios = []
    for i in range(3):
        was = before[1][i] - before[0][i]
        now = after[1][i] - after[0][i]
        if was > 1e-9:
            ratios.append((was, now / was))
    if not ratios:
        return None
    # The longest side measures the factor best; the others must agree with it.
    factor = max(ratios)[1]
    if factor <= 0 or any(abs(r - factor) > _EVEN_SCALE_TOLERANCE * max(1.0, factor) + 1e-6 / was for was, r in ratios):
        return None
    return factor


def _sha256(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _mark_as_converted(mesh_path: str) -> None:
    """The changed mesh carries the mark of a mesh that has its CAD beside it."""
    from kiln.step_import import _stamp_binary_stl

    _stamp_binary_stl(Path(mesh_path))


# ---------------------------------------------------------------------------
# The child: everything below runs in its own interpreter.
# ---------------------------------------------------------------------------


def _child(request: dict[str, Any]) -> dict[str, Any]:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.gp import gp_Trsf, gp_Vec

    from kiln import cad_kernel as k

    solid, why_not = k.read_one_solid(request["step"], verb="changes")
    if solid is None:
        return {"refused": why_not}

    scale = float(request["scale"])
    m = request["linear"]
    change = gp_Trsf()
    change.SetValues(
        m[0][0], m[0][1], m[0][2], 0.0,
        m[1][0], m[1][1], m[1][2], 0.0,
        m[2][0], m[2][1], m[2][2], 0.0,
    )
    if scale != 1.0:
        sized = gp_Trsf()
        sized.SetScaleFactor(scale)
        change = sized.Multiplied(change)
    result = BRepBuilderAPI_Transform(solid, change, True).Shape()

    # Placed where the changed mesh is: the two boxes share a centre.
    low, high = request["box"]
    now = k.box(result)
    shift = [(low[i] + high[i]) / 2.0 - (now[i] + now[i + 3]) / 2.0 for i in range(3)]
    move = gp_Trsf()
    move.SetTranslation(gp_Vec(*shift))
    result = BRepBuilderAPI_Transform(result, move, True).Shape()

    got = k.box(result)
    off = max(abs(got[i] - low[i]) for i in range(3)), max(abs(got[i + 3] - high[i]) for i in range(3))
    if max(off) > float(request["same_box_mm"]):
        return {"refused": f"the changed solid sits {max(off):.3f} mm away from the changed mesh"}
    if not BRepCheck_Analyzer(result).IsValid():
        return {"refused": "the changed solid is not a valid solid"}
    before, after = k.volume(solid), k.volume(result)
    implied = before * scale**3
    if implied <= 0 or abs(after - implied) > float(request["volume_tolerance"]) * implied + 1e-9:
        return {"refused": "the changed solid does not hold the volume the change implies"}
    if not k.write_step(result, request["out_step"]):
        return {"refused": "the changed CAD could not be written"}
    matrix = [
        [scale * m[i][0], scale * m[i][1], scale * m[i][2], shift[i]] for i in range(3)
    ] + [[0.0, 0.0, 0.0, 1.0]]
    return {"scale": scale, "volume_mm3": after, "box": list(got), "matrix": matrix}


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(json.dumps(_child(json.load(fh))))
