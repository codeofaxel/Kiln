"""Public Kiln -> Kiln's servers: a make that names no goal is kept in one.

On an install without kiln-pro, a make that names its goal joins it in the
person's Kiln account (:mod:`kiln._pro_goal_bridge`).  A make that names
none is told to Kiln's servers here, in WORDS: the tool that made it, what
its call named or described it as, and the made file's own name.  The
account keeps the make in a design session of its own, and the answer lands
in the make's reply under the keys a make on Kiln's servers carries:
``saved_goal`` (the session's id, to pass on the next make), the safety
notes the object calls for (``safety_notes``), the notes the person added
themselves (``owner_safety_notes``) and the one question the session is
waiting on (``open_question``).

**What is sent.**  The tool's name, the id the make is kept under, the
values of the call's naming arguments (:data:`WORD_ARGUMENTS`, and no other
argument) and the made file's own name: at most :data:`MAX_WORDS` texts of
at most :data:`MAX_WORD_CHARS` characters each.  Never the model, never the
code a model was made from, never a folder, never a printer's name or a
key.  Each text is cleaned here before it is handed over
(:func:`_word`): what locates or identifies a person or a machine is
replaced by the rules in :mod:`kiln.redaction` (a home folder's owner, a
network or web address, a local host name, a device path, an email address,
a phone number, a secret), a path keeps only its last name, and a value
that reads as OpenSCAD is left out.  Nobody asked for this by name, so it
then goes through the served door as an ask the person did not make
(``_asked_by_user=False``): the door passes every value through
:func:`kiln.outbound_guard.scrub` again, and a signed-out install, which
sends nothing at all, is not counted as someone reaching for a feature.

**When no answer comes.**  The make is returned as it was.  Signed out, the
first make of a session says in one line what signing in adds; later makes
say nothing.  Offline, unanswered or refused, the first make for that cause
says why, sharing the once-per-session pacing of
:func:`kiln.served_public.say_once`, and a make the dispatcher already sent
to Kiln's servers is not sent again.  A miss never reads as "no safety
note": the line says none was looked for.

**A person waiting on a make is never held.**  The ask waits
:data:`CONSULT_TIMEOUT_S`, and after Kiln's servers fail to answer they are
left alone for :data:`SERVICE_BACKOFF_S`.  Nothing here raises.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

#: The served tool a goal lives behind, and its step that takes a make from
#: somewhere else.
WIRE_TOOL = "design_session"
JOIN_VERB = "join"
#: A make's reply is waiting on this ask.
CONSULT_TIMEOUT_S: float = 4.0
#: After Kiln's servers fail to answer, how long they are left alone.  The
#: same backoff the camera, nozzle and blade bridges keep.
SERVICE_BACKOFF_S: float = 300.0
_servers_quiet_until: float = 0.0

#: The call arguments whose value names or describes the object.
WORD_ARGUMENTS: tuple[str, ...] = (
    "name", "title", "description", "prompt", "idea", "notes",
    "design_id", "template_id", "product_type", "file_name",
)
#: How much of one value is sent, and how many values.
MAX_WORD_CHARS = 500
MAX_WORDS = 12
#: How much of one value is read before it is cleaned.
_MAX_READ_CHARS = 4000
#: The once-per-session key of the signed-out line (its own, so a note
#: another tool said about its own answer never uses it up).
_SIGNED_OUT_SAID = "make_session:signed_out"


def _word(value: str, limit: int = MAX_WORD_CHARS) -> str:
    """*value* as it may leave this computer: nothing that locates or
    identifies a person or a machine, no folders, one line, *limit*
    characters at most."""
    from kiln import redaction

    text = value.strip()[:_MAX_READ_CHARS]
    if ("/" in text or "\\" in text) and len(text) < 1024 and os.path.lexists(text):
        text = os.path.basename(text.rstrip("\\/"))
    text = redaction.without_folders(redaction.redact_machine_text(text))
    return " ".join(text.split())[:limit]


def _is_source(value: str) -> bool:
    """Whether *value* reads as OpenSCAD rather than words.  Unsure means
    yes: it is left out."""
    try:
        from kiln.original_design import _looks_like_openscad_code

        return bool(_looks_like_openscad_code(value))
    except Exception:  # noqa: BLE001 -- unsure is not sent
        return True


def words_for(door: str, arguments: dict[str, Any], mesh: str, envelope: dict[str, Any]) -> dict[str, str]:
    """Everything this module sends about one make: the door, the id the make
    is kept under on this computer, its naming words (the arguments in
    :data:`WORD_ARGUMENTS`, cleaned and capped) and the made file's name."""
    made_file = _word(os.path.basename(mesh))
    out: dict[str, str] = {"door": door}
    version_id = ""
    for owner in (envelope, envelope.get("design")):
        if isinstance(owner, dict) and isinstance(owner.get("version_id"), str) and owner["version_id"]:
            version_id = owner["version_id"]
            break
    out["version_id"] = _word(version_id, 120) or os.path.splitext(made_file)[0][:120]
    said = 0
    for key in WORD_ARGUMENTS:
        value = arguments.get(key)
        if said >= MAX_WORDS - 1 or not isinstance(value, str) or not value.strip() or _is_source(value):
            continue
        word = _word(value)
        if word:
            out[key] = word
            said += 1
    if made_file and out.get("file_name") != made_file:
        out["made_file" if "file_name" in out else "file_name"] = made_file
    return out


def keep(envelope: dict[str, Any], mesh_path: str | None = None) -> None:
    """Keep the make in *envelope*, which named no goal, in a design session
    in the person's Kiln account, in place.  Never raises."""
    try:
        _keep(envelope, mesh_path)
    except Exception:  # noqa: BLE001 -- the make is returned whatever this does
        logger.debug("make session: the make was left as it came", exc_info=True)


def _keep(envelope: dict[str, Any], mesh_path: str | None) -> None:
    global _servers_quiet_until
    from kiln import served_answer, served_public
    from kiln._pro_goal_bridge import _carry_notes, _mesh_of
    from kiln.front_door import MAKE_DOOR_TOOLS, SAVED_GOAL_KEY
    from kiln.tool_context import current_tool_arguments, current_tool_name

    door = current_tool_name()
    mesh = _mesh_of(envelope, mesh_path)
    if door not in MAKE_DOOR_TOOLS or mesh is None:
        return
    import kiln.server as _srv

    bearer, expired = _srv._served_bearer()
    if not bearer:
        code = served_answer.SESSION_EXPIRED_CODE if expired is not None else "KILN_ACCOUNT_NOT_PAIRED"
        _say(envelope, served_answer.Miss("signed_out", code))
        return
    if served_public.already_missed() or time.monotonic() < _servers_quiet_until:
        # Kiln's servers were just asked and did not answer: the cause was
        # said where it was met, and this make is not sent after it.
        return
    made_with = words_for(door, current_tool_arguments(), mesh, envelope)
    try:
        answer = _srv._pro_api_call(
            WIRE_TOOL,
            _timeout=CONSULT_TIMEOUT_S,
            # The person asked for a make, not for this.
            _asked_by_user=False,
            verb=JOIN_VERB,
            made_with=made_with,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("make session request failed", exc_info=True)
        _servers_quiet_until = time.monotonic() + SERVICE_BACKOFF_S
        _say(envelope, served_answer.classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None)))
        return
    block = answer.get(SAVED_GOAL_KEY) if isinstance(answer, dict) else None
    if (
        not isinstance(answer, dict)
        or answer.get("status") != "success"
        or not isinstance(block, dict)
        or not isinstance(block.get("note"), str)
    ):
        miss = (
            served_answer.classify_answer(answer) if isinstance(answer, dict) else None
        ) or served_answer.Miss("unanswered", detail="no session in the answer")
        if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
            _servers_quiet_until = time.monotonic() + SERVICE_BACKOFF_S
        _say(envelope, miss)
        return
    envelope[SAVED_GOAL_KEY] = {
        k: block[k]
        for k in ("brief_id", "joined", "saved", "opened", "version", "version_id", "note")
        if k in block
    }
    envelope[SAVED_GOAL_KEY]["joined_on"] = "kiln_servers"
    _carry_notes(answer, envelope)


#: What signing in adds for a make that names no goal, and what its absence
#: means for this one.  One line, said once per session.
_WHAT_SIGNING_IN_ADDS = (
    "a make that names no goal is kept in a goal of its own and the reply says "
    "any safety note the object calls for. This make was made on this computer "
    "only: no safety note here means none was looked for."
)
SIGNED_OUT_LINE = f"Signed in to a free Kiln account, {_WHAT_SIGNING_IN_ADDS}"
SESSION_EXPIRED_LINE = f"Your Kiln session has expired. Signed back in, {_WHAT_SIGNING_IN_ADDS}"


def line_for(miss: Any) -> str:
    """What a make that reached no session is told, for *miss*'s cause:
    signed out, what signing in adds; any other cause, why in the one voice
    every unanswered ask of Kiln's servers is worded in."""
    from kiln import served_answer

    if miss.cause == "signed_out":
        return SESSION_EXPIRED_LINE if miss.code == served_answer.SESSION_EXPIRED_CODE else SIGNED_OUT_LINE
    return served_answer.sentence(
        miss,
        feature="servers",
        on_the_line=(
            "Signed in to Kiln, a make that names no goal is kept in a goal of its "
            "own and the reply says any safety note the object calls for"
        ),
        cannot="do either for this make",
        wont="kept it in no goal and looked for no safety note",
        safe_remedy="The make is here as asked; no safety note in this reply means none was looked for",
        then="make it again",
    )


def _say(envelope: dict[str, Any], miss: Any) -> None:
    """Say *miss* in the make's reply, the first time this session: signed
    out under its own pacing, every other cause under the one a public tool
    answered on this computer uses."""
    from kiln import served_answer, served_public
    from kiln.front_door import SAVED_GOAL_KEY

    signed_out = miss.cause == "signed_out"
    if not served_public.first_this_session(_SIGNED_OUT_SAID if signed_out else miss.cause):
        return
    block: dict[str, Any] = {
        "brief_id": "",
        "joined": False,
        "note": line_for(miss),
        **served_answer.fields(miss),
    }
    if signed_out:
        from kiln.tiers_and_terms import signin_hint_fields

        block.update(signin_hint_fields())
    envelope[SAVED_GOAL_KEY] = block


def _reset_for_tests() -> None:
    global _servers_quiet_until
    _servers_quiet_until = 0.0


__all__ = [
    "CONSULT_TIMEOUT_S",
    "JOIN_VERB",
    "MAX_WORDS",
    "MAX_WORD_CHARS",
    "SERVICE_BACKOFF_S",
    "SESSION_EXPIRED_LINE",
    "SIGNED_OUT_LINE",
    "WIRE_TOOL",
    "WORD_ARGUMENTS",
    "keep",
    "line_for",
    "words_for",
]
