"""Public Kiln -> Kiln's servers: a declared mechanism played through its cycle.

An assembly record may declare a mechanism (:class:`kiln.assembly.Mechanism`:
one driver and the joints that turn together).  Composing it
(:func:`kiln.assembly.compose_assembly`) asks Kiln's servers to play it: every
part's pose at each driver pose over one period, each slide's stroke, and
whether any two parts meet anywhere in the cycle, as the ``kiln.mechanism.v1``
block the stage plays (:mod:`kiln.mechanism_stage`).  Asked on every plan,
once per compose; with kiln-pro on this computer the same answer is made
here (:data:`MECHANISM_LOCAL`).  The parts' parsed meshes go up, gzipped,
never the files as they are on disk; nothing is kept on either side.

A miss (offline, signed out, no answer, refused) is said once in the
compose's result (:func:`play`): the parts are shown as modelled and
nothing is said about where they meet in the cycle, never that they are
clear; a paying caller is never pitched the plan they hold.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error, local_fault, sentence

logger = logging.getLogger(__name__)

#: The served wire that plays a declared mechanism; with kiln-pro on this
#: computer it answers here.
MECHANISM_TOOL = "play_mechanism"
MECHANISM_LOCAL = "kiln_pro.joints.mechanism_wire"
#: A whole cycle is solved and every pair of parts swept; the compose waits
#: this long for it.
_MECHANISM_TIMEOUT_S: float = 300.0


def _here() -> bool:
    """Whether kiln-pro answers on this computer."""
    try:
        from kiln_pro.joints.mechanism_wire import answer  # noqa: F401
    except ImportError:
        return False
    return True


def request_for(assembly: Any, spans: dict[str, tuple[int, int]], *, here: bool) -> dict[str, Any] | str:
    """The ``mechanism_request/1`` document for *assembly*, or the clause
    saying why none could be sent.  *spans*: each part's triangle run in
    the composed file.  *here*: kiln-pro answers on this computer, so files
    go by path; otherwise each part's parsed mesh travels as a binary STL,
    gzipped."""
    import base64
    import gzip
    from pathlib import Path

    from kiln._pro_assembly_bridge import JOINT_SWEEP_MAX_GZ_BYTES, _mesh_bytes

    parts = []
    carried = 0
    for part in assembly.parts:
        path = Path(str(part.file_path))
        if not path.is_file():
            return f"there is no file at {part.file_path} for part '{part.part_id}'"
        if here:
            sent: dict[str, Any] = {"path": str(path)}
        else:
            mesh = _mesh_bytes(path)
            if isinstance(mesh, str):
                return f"part '{part.part_id}' was not sent: {mesh}"
            body = gzip.compress(mesh)
            carried += len(body)
            if carried > JOINT_SWEEP_MAX_GZ_BYTES:
                return (
                    "the parts' files are too large to send for the mechanism "
                    f"(more than {JOINT_SWEEP_MAX_GZ_BYTES // (1024 * 1024)} MB compressed)"
                )
            sent = {"name": f"part-{len(parts)}.stl", "gz_b64": base64.b64encode(body).decode("ascii")}
        entry: dict[str, Any] = {
            "part_id": part.part_id,
            "file": sent,
            "position_mm": [float(c) for c in getattr(part, "position_mm", (0.0, 0.0, 0.0))],
            "rotation_deg": [float(c) for c in getattr(part, "rotation_deg", (0.0, 0.0, 0.0))],
            "material": str(getattr(part, "material", "") or ""),
        }
        if part.part_id in spans:
            first, count = spans[part.part_id]
            entry["spans"] = [[int(first), int(count)]]
        parts.append(entry)
    return {
        "schema": "mechanism_request/1",
        "parts": parts,
        "interfaces": [iface.to_dict() for iface in assembly.interfaces],
        "mechanism": assembly.mechanism.to_dict(),
    }


def play(assembly: Any, spans: dict[str, tuple[int, int]]) -> tuple[dict[str, Any] | None, str | None]:
    """The ``kiln.mechanism.v1`` block for *assembly*, or ``(None, why)``,
    *why* one sentence for the person: this computer's own reason (a file
    missing, too much to send), or the miss worded in the served door's one
    shape."""
    if getattr(assembly, "mechanism", None) is None:
        return None, None
    here = _here()
    request = request_for(assembly, spans, here=here)
    if isinstance(request, str):
        return None, f"The mechanism was not played: {request}."
    miss: Miss | None
    reply: Any = None
    if here:
        try:
            from kiln_pro.joints.mechanism_wire import answer as local_answer

            reply = local_answer(request, local_files=True)
        except Exception as exc:  # noqa: BLE001 -- a compose never fails for want of the player
            return None, _said(local_fault(exc))
    else:
        try:
            from kiln.server import _pro_api_call
        except Exception:  # noqa: BLE001
            return None, _said(Miss("unanswered", detail="this install could not open its connection"))
        try:
            reply = _pro_api_call(MECHANISM_TOOL, _timeout=_MECHANISM_TIMEOUT_S, _asked_by_user=True, **request)
        except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
            return None, _said(classify_transport_error(exc))
    miss = classify_answer(reply) if isinstance(reply, dict) else Miss("unanswered", detail="no answer")
    if miss is not None:
        return None, _said(miss)
    if not isinstance(reply, dict) or reply.get("schema") != "mechanism_answer/1":
        return None, _said(Miss("unanswered", detail="an answer that is not a mechanism"))
    block = reply.get("mechanism")
    if not isinstance(block, dict) or block.get("kind") != "kiln.mechanism.v1":
        return None, _said(Miss("unanswered", detail="an answer with no mechanism block"))
    return block, None


def _said(miss: Miss) -> str:
    return sentence(
        miss,
        feature="the mechanism player",
        on_the_line="Kiln plays a mechanism's motion from Kiln's servers",
        cannot="play this mechanism",
        wont="shows the parts as modelled and says nothing about where they meet in the cycle",
        then="compose again",
    )


__all__ = ["MECHANISM_LOCAL", "MECHANISM_TOOL", "play", "request_for"]
