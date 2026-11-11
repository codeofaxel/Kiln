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
to it instead of opening another.

A model Kiln made HERE that names no goal opens one through the same step
(:func:`_open_for_made_here`).  It has no source to state, so it says the
tool that made it, its fingerprint and the words its call named it by,
cleaned and capped (:mod:`kiln.make_words`); never the model, its source or
a folder.  Its goal is noted beside the file the same way.  This ask waits a
few seconds at most, and a signed-out install is told once per session what
signing in adds.

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
    compared = answer.get(REFERENCE_KEY)
    if isinstance(compared, dict):
        # The servers' comparison replaces a "not compared" an earlier door
        # left (an image-to-3D make says so before it joins), never a real one.
        held = envelope.get(REFERENCE_KEY)
        if not isinstance(held, dict) or held.get("status") == _not_compared_status():
            envelope[REFERENCE_KEY] = compared


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

    A file with no arrival note (Kiln made it here) opens one from its
    words instead (:func:`_open_for_made_here`); a make with no file is left
    alone.  A model whose note already names the goal
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
        _open_for_made_here(envelope, mesh)
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
    opened = block.get("brief_id")
    if block.get("opened") is True and isinstance(opened, str) and opened:
        arrival.note_goal(mesh, opened)


def _open_for_made_here(envelope: dict[str, Any], mesh: str) -> None:
    """Open a goal for a model Kiln made here that names none, in place.

    Only inside a make tool's own call.  Signed out, nothing is sent and the
    first make of a session says what signing in adds.  Signed in, the step
    is sent the tool's name, the made file's name, the model's fingerprint
    and the call's naming words (:func:`kiln.make_words.made_with`), with
    the goal noted beside the file when an earlier make opened one, so the
    same model is that goal's version again and never a second goal.  The
    answer lands under ``saved_goal`` with the goal's safety notes; a miss
    is said once per session and the make stands.  Never changes the make.
    """
    from kiln import arrival, make_words, served_answer, served_public
    from kiln.front_door import MAKE_DOOR_TOOLS, SAVED_GOAL_KEY
    from kiln.tool_context import current_tool_arguments, current_tool_name

    door = current_tool_name()
    if door not in MAKE_DOOR_TOOLS:
        return
    import kiln.server as _srv

    bearer, expired = _srv._served_bearer()
    if not bearer:
        code = served_answer.SESSION_EXPIRED_CODE if expired is not None else "KILN_ACCOUNT_NOT_PAIRED"
        make_words.say(envelope, served_answer.Miss("signed_out", code))
        return
    if served_public.already_missed() or make_words.servers_quiet():
        # Kiln's servers were just asked and did not answer: the cause was
        # said where it was met, and this make is not sent after it.
        return
    sha = _fingerprint(mesh)
    if not sha:
        return
    said = make_words.made_with(door, current_tool_arguments(), mesh, envelope, sha)
    noted = arrival.noted_goal(mesh)
    answer, miss = _ask(noted, mesh, said, False, send_file=False, timeout=make_words.CONSULT_TIMEOUT_S)
    if miss is None and noted and answer.get("code") == "NOT_FOUND":
        # Noted under another account: this one gets a goal of its own.
        answer, miss = _ask("", mesh, said, False, send_file=False, timeout=make_words.CONSULT_TIMEOUT_S)
    block = answer.get(SAVED_GOAL_KEY) if miss is None else None
    if miss is None and not (isinstance(block, dict) and isinstance(block.get("note"), str)):
        miss = served_answer.Miss("unanswered", detail="no goal in the answer")
    if miss is not None:
        make_words.note_miss(miss)
        make_words.say(envelope, miss)
        return
    envelope[SAVED_GOAL_KEY] = {
        k: block[k]
        for k in ("brief_id", "joined", "opened", "saved", "version", "version_id", "note")
        if k in block
    }
    envelope[SAVED_GOAL_KEY]["joined_on"] = "kiln_servers"
    _carry_notes(answer, envelope)
    opened = block.get("brief_id")
    if block.get("opened") is True and isinstance(opened, str) and opened:
        arrival.note_goal(mesh, opened, made_here=True)


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
    timeout: float = CONSULT_TIMEOUT_S,
) -> tuple[dict[str, Any], Any]:
    """``(answer, None)`` from the servers, or ``({}, miss)``.  An empty
    *brief_id* asks the step to open a goal for the model.  Without
    *send_file* nothing but *made_with* leaves this computer.  *timeout* is
    how long the make's reply waits on it."""
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
        answer = _srv._pro_api_call(
            WIRE_TOOL,
            _timeout=timeout,
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
