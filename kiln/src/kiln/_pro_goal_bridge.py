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
servers carries (``saved_goal``, ``reference_check``), with the goal's safety
notes for the part (``safety_notes``), the notes the person added themselves
(``owner_safety_notes``, always under their own key) and the one question the
goal is waiting on (``open_question``) when the answer holds them.

A model that came from OUTSIDE Kiln (a generator, a marketplace download:
anything with a :mod:`kiln.arrival` note beside it) and names no goal opens
one the same way (:func:`open_for`): the step is sent the model, where it
came from and what was asked for it, with no goal id, and the servers open
a goal whose first version it is.  Only the goal is kept there -- the idea,
the source, the version -- never the model, which stays on this computer.
The goal is noted beside the file, so the next make of the same model adds
to it instead of opening another.  A file Kiln made here opens nothing.

A miss (offline, signed out, no answer, refused) is said once in the reply:
the make stands as asked, it was not added to the goal, and it was not
compared with the goal's picture -- never read as joined or as matching.
An install that was never signed in is told in one line what signing in
keeps, and nothing is sent.  Nothing here raises.
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
#: A join whose make states how it moves also waits on that being checked.
MOTION_TIMEOUT_S: float = 120.0
#: The model parameter the step takes (its manifest ``inputs``).
_MESH_PARAM = "mesh_path"
#: The goal's safety notes for the part, as sentences.
SAFETY_NOTES_KEY = "safety_notes"
#: The safety notes the person added themselves.  Never merged into
#: :data:`SAFETY_NOTES_KEY`: the two are shown apart.
OWNER_NOTES_KEY = "owner_safety_notes"
#: The one question the goal is waiting on the person to answer.
QUESTION_KEY = "open_question"
#: Where a make's envelope keeps its file, when the door did not hand it.
_MESH_KEYS = ("local_path", "stl_path", "output_path", "path", "best_result_path", "produced_file")


def _mesh_of(envelope: dict[str, Any], mesh_path: str | None) -> str | None:
    for candidate in (mesh_path, *(envelope.get(k) for k in _MESH_KEYS)):
        if isinstance(candidate, str) and candidate and os.path.isfile(candidate):
            return candidate
    return None


#: How much of what was asked of a generator goes with a model that opens a
#: goal: enough to be the goal's idea.
_PROMPT_CHARS = 500


def _fingerprint(mesh: str) -> str:
    """The sha256 of the model's bytes, or ``""`` when it cannot be read."""
    import hashlib

    digest = hashlib.sha256()
    try:
        with open(mesh, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _made_with(
    envelope: dict[str, Any], mesh: str, *, asked: str = "", fingerprint: bool = False,
) -> dict[str, str]:
    """What the goal is told about the model: the name it is kept under
    here, the version it already is (a design version, a job), and where it
    came from, read from the note Kiln left beside it.  *asked* is what the
    person asked a generator for, sent only when the model opens a goal.
    With *fingerprint* the model's sha256 goes in place of the model."""
    out: dict[str, str] = {"file_name": os.path.basename(mesh)}
    if fingerprint:
        sha = _fingerprint(mesh)
        if sha:
            out["sha256"] = sha
    if asked.strip():
        out["prompt"] = asked.strip()[:_PROMPT_CHARS]
    for owner in (envelope, envelope.get("design")):
        if isinstance(owner, dict) and isinstance(owner.get("version_id"), str) and owner["version_id"]:
            out["version_id"] = owner["version_id"]
            break
    try:
        from kiln.arrival import goal_record

        out.update(goal_record(mesh))
        if fingerprint:
            # Kiln's own ask (a goal it opens or finds), which the guard on
            # the way out scrubs: the listing a download came from travels as
            # its public page, judged by site there; any other link stays here.
            from kiln.outbound_guard import LISTING_LINK_KEY, is_listing_link

            link = out.pop("url", "")
            if is_listing_link(link):
                out[LISTING_LINK_KEY] = link
    except Exception:  # noqa: BLE001 -- an unreadable note names no source
        pass
    return out


def join(
    brief_id: str,
    envelope: dict[str, Any],
    mesh_path: str | None = None,
    *,
    asked_by_user: bool = True,
    send_file: bool = True,
) -> None:
    """Join the make in *envelope* to goal *brief_id* on Kiln's servers, in place.

    *asked_by_user* is whether the person named the goal on the make; a
    goal Kiln noted beside the file on its own is not their ask.  With
    *send_file* false the model stays on this computer and only its
    fingerprint goes (a goal Kiln opened on its own holds no pictures to
    compare it with).

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
    answer, miss = _ask(brief_id, mesh, _made_with(envelope, mesh, fingerprint=not send_file), asked_by_user,
                        send_file=send_file)
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
    _carry_notes(answer, envelope)
    _carry_motion(answer, envelope, mesh)
    compared = answer.get(REFERENCE_KEY)
    if isinstance(compared, dict):
        # The servers' comparison replaces a "not compared" an earlier door
        # left (an image-to-3D make says so before it joins), never a real one.
        held = envelope.get(REFERENCE_KEY)
        if not isinstance(held, dict) or held.get("status") == _not_compared_status():
            envelope[REFERENCE_KEY] = compared


#: The most a statement of how a part moves may weigh to be sent with a make.
_MAX_MOTION_BYTES = 64 * 1024
MOTION_KEY = "motion"


def _stated_motion() -> dict[str, Any] | None:
    """How the person said the part moves, from the running make's own
    ``motion`` argument, as it is sent with the file: their statement, and
    the ask that what plays it comes back with the answer (the file the
    servers read is a copy; the one on this computer is what a stage shows).
    ``None`` when the make states no motion."""
    import json

    from kiln import tool_context

    theirs = tool_context.current_tool_arguments().get(MOTION_KEY)
    if isinstance(theirs, str):
        theirs = theirs.strip()
    if not theirs or not isinstance(theirs, (dict, str)):
        return None
    try:
        if len(json.dumps(theirs)) > _MAX_MOTION_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    return {"statement": theirs, "note": "reply"}


def _carry_motion(answer: dict[str, Any], envelope: dict[str, Any], mesh: str) -> None:
    """Put what the servers found about the stated motion into the make's
    reply, and keep what plays it beside the file on this computer, so the
    stage here plays it.  The reply carries the sentences only."""
    said = answer.get(MOTION_KEY)
    if not isinstance(said, dict) or MOTION_KEY in envelope:
        return
    shown = {k: said[k] for k in ("plays", "says", "detail", "still_checking", "upgrade") if k in said}
    note = said.get("note")
    if shown.get("plays") is True:
        from kiln import mechanism_stage

        kept = isinstance(note, dict) and mechanism_stage.record(mesh, note) is not None
        shown["on_this_computer"] = kept
        # Only what was kept here is still being read here.
        shown.pop("still_checking", None)
    if shown:
        envelope[MOTION_KEY] = shown


def _carry_notes(answer: dict[str, Any], envelope: dict[str, Any]) -> None:
    """Put the goal's safety notes, the person's own notes and the open
    question into the make's reply, as the servers sent them: each list
    under its own key, sentences added once each, and a question only
    where the reply holds none."""
    for key in (SAFETY_NOTES_KEY, OWNER_NOTES_KEY):
        notes = answer.get(key)
        if isinstance(notes, list):
            held = envelope.get(key)
            merged = [n for n in held if isinstance(n, str)] if isinstance(held, list) else []
            merged.extend(n for n in notes if isinstance(n, str) and n and n not in merged)
            if merged:
                envelope[key] = merged
    question = answer.get(QUESTION_KEY)
    if (
        isinstance(question, dict)
        and isinstance(question.get("question"), str)
        and question["question"]
        and QUESTION_KEY not in envelope
    ):
        envelope[QUESTION_KEY] = dict(question)


def open_for(envelope: dict[str, Any], mesh_path: str | None = None) -> None:
    """Open a goal in the person's Kiln account for a make that names none,
    when its model came from outside Kiln; in place.

    A file with no arrival note (Kiln made it here) is left alone, and so
    is a make with no file.  A model whose note already names the goal
    opened for it joins that goal.  Otherwise the servers are asked to open
    one, and ``envelope["saved_goal"]`` says what happened: the goal and
    the version (``opened``), the one line about signing in when this
    install never has, or the miss.  Never raises, and never changes the
    make.
    """
    from kiln import arrival, served_answer
    from kiln.front_door import GOAL_NOTES, SAVED_GOAL_KEY

    mesh = _mesh_of(envelope, mesh_path)
    if mesh is None:
        return
    try:
        came_from = arrival.read(mesh)
        noted = arrival.noted_goal(mesh) if came_from is not None else ""
    except Exception:  # noqa: BLE001 -- an unreadable note names no source
        return
    if came_from is None:
        return
    if noted:
        join(noted, envelope, mesh, asked_by_user=False, send_file=False)
        block = envelope.get(SAVED_GOAL_KEY)
        gone = GOAL_NOTES["not_in_account"].format(brief_id=noted)
        if not (isinstance(block, dict) and block.get("note") == gone):
            return
        # Noted under another account: this one gets a goal of its own.
        envelope.pop(SAVED_GOAL_KEY, None)
    # Only the goal is opened, in the person's account: the model itself
    # stays on this computer and its fingerprint goes in its place.
    answer, miss = _ask(
        "", mesh, _made_with(envelope, mesh, asked=came_from.prompt, fingerprint=True), False, send_file=False,
    )
    if miss is None:
        block = answer.get(SAVED_GOAL_KEY)
        if not (isinstance(block, dict) and isinstance(block.get("note"), str)):
            miss = served_answer.Miss("unanswered", detail="no goal in the answer")
    if miss is not None:
        _say_not_opened(envelope, miss)
        return
    envelope[SAVED_GOAL_KEY] = {
        k: block[k]
        for k in ("brief_id", "joined", "opened", "saved", "version", "version_id", "came_from", "note")
        if k in block
    }
    envelope[SAVED_GOAL_KEY]["joined_on"] = "kiln_servers"
    _carry_notes(answer, envelope)
    _carry_motion(answer, envelope, mesh)
    opened = block.get("brief_id")
    if block.get("opened") is True and isinstance(opened, str) and opened:
        arrival.note_goal(mesh, opened)


def _say_not_opened(envelope: dict[str, Any], miss: Any) -> None:
    """No goal was opened for a model from outside: said once.  Never having
    signed in is not a fault of anything, so it gets one plain line."""
    from kiln import served_answer
    from kiln.front_door import GOAL_NOTES, SAVED_GOAL_KEY

    if miss.cause == "signed_out" and miss.code != served_answer.SESSION_EXPIRED_CODE:
        said = GOAL_NOTES["sign_in_keeps_source"]
    else:
        said = served_answer.sentence(
            miss,
            feature="servers",
            on_the_line=(
                "This model came from outside Kiln, and where it came from is kept "
                "in your Kiln account's design history"
            ),
            cannot="open a goal for it there",
            wont="did not record it in your history",
            safe_remedy="The model is here as made",
            then="bring it in again",
        )
    envelope[SAVED_GOAL_KEY] = {
        "brief_id": "",
        "joined": False,
        "opened": False,
        "note": said,
        **served_answer.fields(miss),
    }


def _not_compared_status() -> str:
    from kiln.reference_compare import NOT_COMPARED

    return NOT_COMPARED


def _ask(
    brief_id: str, mesh: str, made_with: dict[str, str], asked_by_user: bool, *, send_file: bool = True,
) -> tuple[dict[str, Any], Any]:
    """``(answer, None)`` from the servers, or ``({}, miss)``.  An empty
    *brief_id* asks the step to open a goal for the model.  Without
    *send_file* nothing but *made_with* leaves this computer."""
    from kiln import served_answer, served_makes

    try:
        import kiln.server as _srv

        bearer, expired = _srv._served_bearer()
        if not bearer:
            code = served_answer.SESSION_EXPIRED_CODE if expired is not None else "KILN_ACCOUNT_NOT_PAIRED"
            return {}, served_answer.Miss("signed_out", code)
        kwargs: dict[str, Any] = {}
        if send_file:
            kwargs, refusal = served_makes.send_inputs(WIRE_TOOL, {_MESH_PARAM: mesh}, {"mesh": _MESH_PARAM})
            if refusal is not None:
                return {}, served_answer.classify_answer(refusal) or served_answer.Miss("unanswered")
            stated = _stated_motion()
            if stated is not None:
                kwargs["motion"] = stated
        answer = _srv._pro_api_call(
            WIRE_TOOL,
            _timeout=MOTION_TIMEOUT_S if MOTION_KEY in kwargs else CONSULT_TIMEOUT_S,
            # A goal the person named on the make is their ask; a goal Kiln
            # opens or finds for a model on its own is not, and what this
            # install read beside the file is then scrubbed on the way out.
            _asked_by_user=asked_by_user,
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


__all__ = [
    "CONSULT_TIMEOUT_S",
    "JOIN_VERB",
    "OWNER_NOTES_KEY",
    "QUESTION_KEY",
    "SAFETY_NOTES_KEY",
    "WIRE_TOOL",
    "join",
    "open_for",
]
