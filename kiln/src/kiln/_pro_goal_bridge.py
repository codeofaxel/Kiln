"""Public Kiln -> Kiln's servers: a model made on this computer joins its goal.

``design_session`` keeps a goal -- what was asked, the numbers settled, the
picture shown -- in the person's Kiln account, and on an install without
kiln-pro it runs on Kiln's servers (https://kiln3d.com).  A make on THIS
computer that names its goal -- a marketplace download, an import, an
image-to-3D job, a compile the servers did not answer -- reaches the goal
here: the model is sent up through the same served tool, as
``design_session(verb="join")``, and the goal records it as its next
version, keeps where it came from, and compares it with the goal's pictures.
That answer lands in the make's own reply, under the keys a make on Kiln's
servers carries (``saved_goal``, ``reference_check``).

A miss (offline, signed out, no answer, refused) is said once in the reply:
the make stands as asked, it was not added to the goal, and it was not
compared with the goal's picture -- never read as joined or as matching.
Nothing here raises.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: The served tool a goal lives behind.
WIRE_TOOL = "design_session"
#: Its step that takes a model made somewhere else.
JOIN_VERB = "join"
#: A join sends a model and may draw a comparison; a person is waiting on
#: the make's reply.
CONSULT_TIMEOUT_S: float = 60.0
#: The model parameter the step takes (its manifest ``inputs``).
_MESH_PARAM = "mesh_path"
#: Where a make's envelope keeps its file, when the door did not hand it.
_MESH_KEYS = ("local_path", "stl_path", "output_path", "path", "best_result_path", "produced_file")


def _mesh_of(envelope: dict[str, Any], mesh_path: str | None) -> str | None:
    for candidate in (mesh_path, *(envelope.get(k) for k in _MESH_KEYS)):
        if isinstance(candidate, str) and candidate and os.path.isfile(candidate):
            return candidate
    return None


def _made_with(envelope: dict[str, Any], mesh: str) -> dict[str, str]:
    """What the goal is told about the model: the name it is kept under
    here, the version it already is (a design version, a job), and where it
    came from, read from the note Kiln left beside it."""
    out: dict[str, str] = {"file_name": os.path.basename(mesh)}
    for owner in (envelope, envelope.get("design")):
        if isinstance(owner, dict) and isinstance(owner.get("version_id"), str) and owner["version_id"]:
            out["version_id"] = owner["version_id"]
            break
    try:
        from kiln.arrival import goal_record

        out.update(goal_record(mesh))
    except Exception:  # noqa: BLE001 -- an unreadable note names no source
        pass
    return out


def join(brief_id: str, envelope: dict[str, Any], mesh_path: str | None = None) -> None:
    """Join the make in *envelope* to goal *brief_id* on Kiln's servers, in place.

    Sets ``envelope["saved_goal"]`` -- what the servers recorded, or why
    nothing was -- and, when the goal holds a picture, the comparison under
    ``envelope["reference_check"]``.  A miss sets ``reference_check`` to
    "not compared" with the reason.  Never raises.
    """
    from kiln.front_door import GOAL_NOTES, SAVED_GOAL_KEY
    from kiln.reference_compare import REFERENCE_KEY

    mesh = _mesh_of(envelope, mesh_path)
    if mesh is None:
        envelope[SAVED_GOAL_KEY] = {
            "brief_id": brief_id,
            "joined": False,
            "note": GOAL_NOTES["no_file"].format(brief_id=brief_id),
        }
        return
    answer, miss = _ask(brief_id, mesh, _made_with(envelope, mesh))
    if miss is not None:
        _say_miss(brief_id, envelope, miss)
        return
    if answer.get("code") == "NOT_FOUND":
        envelope[SAVED_GOAL_KEY] = {
            "brief_id": brief_id,
            "joined": False,
            "note": GOAL_NOTES["not_in_account"].format(brief_id=brief_id),
        }
        return
    block = answer.get(SAVED_GOAL_KEY)
    if not isinstance(block, dict) or not isinstance(block.get("note"), str):
        from kiln.served_answer import Miss

        _say_miss(brief_id, envelope, Miss("unanswered", detail="no goal in the answer"))
        return
    envelope[SAVED_GOAL_KEY] = {
        k: block[k]
        for k in ("brief_id", "joined", "saved", "version", "version_id", "came_from", "note")
        if k in block
    }
    envelope[SAVED_GOAL_KEY]["joined_on"] = "kiln_servers"
    compared = answer.get(REFERENCE_KEY)
    if isinstance(compared, dict):
        # The servers' comparison replaces a "not compared" an earlier door
        # left (an image-to-3D make says so before it joins), never a real one.
        held = envelope.get(REFERENCE_KEY)
        if not isinstance(held, dict) or held.get("status") == _not_compared_status():
            envelope[REFERENCE_KEY] = compared


def _not_compared_status() -> str:
    from kiln.reference_compare import NOT_COMPARED

    return NOT_COMPARED


def _ask(brief_id: str, mesh: str, made_with: dict[str, str]) -> tuple[dict[str, Any], Any]:
    """``(answer, None)`` from the servers, or ``({}, miss)``."""
    from kiln import served_answer, served_makes

    try:
        import kiln.server as _srv

        bearer, expired = _srv._served_bearer()
        if not bearer:
            code = served_answer.SESSION_EXPIRED_CODE if expired is not None else "KILN_ACCOUNT_NOT_PAIRED"
            return {}, served_answer.Miss("signed_out", code)
        kwargs, refusal = served_makes.send_inputs(WIRE_TOOL, {_MESH_PARAM: mesh}, {"mesh": _MESH_PARAM})
        if refusal is not None:
            return {}, served_answer.classify_answer(refusal) or served_answer.Miss("unanswered")
        answer = _srv._pro_api_call(
            WIRE_TOOL,
            _timeout=CONSULT_TIMEOUT_S,
            # The person named this goal on the make: sending the make to it
            # is what they asked for.
            _asked_by_user=True,
            verb=JOIN_VERB,
            goal_id=brief_id,
            made_with=made_with,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("goal join request failed", exc_info=True)
        import kiln.server as _srv

        return {}, served_answer.classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None))
    if not isinstance(answer, dict):
        return {}, served_answer.Miss("unanswered", detail="no answer")
    if answer.get("code") == "NOT_FOUND":
        return answer, None
    if answer.get("status") != "success":
        return {}, served_answer.classify_answer(answer) or served_answer.Miss("unanswered", detail="no goal in the answer")
    return served_makes.arrive(WIRE_TOOL, answer), None


def _say_miss(brief_id: str, envelope: dict[str, Any], miss: Any) -> None:
    """The make stands; the goal and the picture were not reached: said once."""
    from kiln import served_answer
    from kiln.front_door import SAVED_GOAL_KEY
    from kiln.reference_compare import NOT_COMPARED, REFERENCE_KEY

    said = served_answer.sentence(
        miss,
        feature="servers",
        on_the_line=f"Saved goal {brief_id} lives in your Kiln account",
        cannot="add this make to it",
        wont="did not add it to the goal or compare it with any picture kept there",
        safe_remedy=(
            "The make is here as asked; if the person gave you a picture, look at "
            "it beside the result and say how they differ"
        ),
        then="make it again with the same goal",
    )
    envelope[SAVED_GOAL_KEY] = {
        "brief_id": brief_id,
        "joined": False,
        "note": said,
        **served_answer.fields(miss),
    }
    held = envelope.get(REFERENCE_KEY)
    if not isinstance(held, dict) or held.get("status") == NOT_COMPARED:
        envelope[REFERENCE_KEY] = {
            "status": NOT_COMPARED,
            "references": list((held or {}).get("references") or []),
            "reason": said,
        }


__all__ = ["CONSULT_TIMEOUT_S", "JOIN_VERB", "WIRE_TOOL", "join"]
