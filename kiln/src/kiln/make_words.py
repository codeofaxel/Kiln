"""The words a make sends about a model Kiln made here, cleaned and capped.

On an install without kiln-pro a model that names no goal opens one in the
person's Kiln account (:func:`kiln._pro_goal_bridge.open_for`).  A model
from outside Kiln says where it came from.  A model Kiln made HERE has no
source to state, so it says the tool that made it, its fingerprint and what
its call named or described it as; the account keeps it in a goal of its
own and the reply carries the safety notes the object calls for.  This
module builds exactly what is said (:func:`made_with`) and words what a make
is told when nothing was (:func:`say`).  It sends nothing itself.

**What is said.**  The tool's name, the made file's own name, the model's
sha256 fingerprint, the id the make is kept under when it has one, and the
values of the call's naming arguments (:data:`WORD_ARGUMENTS`, and no other
argument): at most :data:`MAX_WORDS` texts of at most
:data:`MAX_WORD_CHARS` characters each.  Never the model, never the code a
model was made from, never a folder, never a printer's name or a key.  Each
text is cleaned here (:func:`_word`): what locates or identifies a person
or a machine is replaced by the rules in :mod:`kiln.redaction` (a home
folder's owner, a network or web address, a local host name, a device path,
an email address, a phone number, a secret), a path keeps only its last
name, every run of seven or more digits is dropped whatever surrounds it
(the words are read for what the object is, never for its numbers), and a
value that reads as OpenSCAD is left out.  The ask then goes through the
served door as one the person did not make, so the door passes every value
through :func:`kiln.outbound_guard.scrub` again.

**When no answer comes.**  The make is returned as it was.  Signed out,
nothing is sent, and the first make of a session says in one line what
signing in adds; later makes say nothing.  Offline, unanswered or refused,
the first make for that cause says why, sharing the once-per-session pacing
of :func:`kiln.served_public.say_once`.  A miss never reads as "no safety
note": the line says none was looked for.

**A person waiting on a make is never held.**  The ask waits
:data:`CONSULT_TIMEOUT_S`, and after Kiln's servers fail to answer they are
left alone for :data:`SERVICE_BACKOFF_S`.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

#: A make's reply is waiting on the ask these words ride.
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
#: The key the tool that made the model is said under.
DOOR_KEY = "door"
#: The made file's own name, and the model's fingerprint: the keys every
#: model sent without its file is said under.
FILE_KEY, FINGERPRINT_KEY, VERSION_KEY = "file_name", "sha256", "version_id"
#: Where the call's own ``file_name`` argument goes when it is not the made
#: file's name, which :data:`FILE_KEY` already says.
ASKED_FILE_KEY = "filename"
#: How much of one value is sent, and how many values.
MAX_WORD_CHARS = 500
MAX_WORDS = 12
#: How much of one value is read before it is cleaned.
_MAX_READ_CHARS = 4000
#: A run of seven or more digits, with the marks people put inside a number.
#: No such run is sent, whatever stands around it: nothing these words are
#: read for needs one, and a phone number the shared rules do not know by
#: its shape is one.
_DIGIT_RUN = re.compile(r"\d(?:[ .()\-]*\d){6,}")
#: The once-per-session key of the signed-out line (its own, so a note
#: another tool said about its own answer never uses it up).
_SIGNED_OUT_SAID = "make_session:signed_out"


def _word(value: str, limit: int = MAX_WORD_CHARS) -> str:
    """*value* as it may leave this computer: nothing that locates or
    identifies a person or a machine, no folders, no long run of digits,
    one line, *limit* characters at most."""
    from kiln import redaction

    text = value.strip()[:_MAX_READ_CHARS]
    if ("/" in text or "\\" in text) and len(text) < 1024 and os.path.lexists(text):
        text = os.path.basename(text.rstrip("\\/"))
    text = redaction.without_folders(redaction.redact_machine_text(text))
    text = _DIGIT_RUN.sub("", text)
    return " ".join(text.split())[:limit]


def _is_source(value: str) -> bool:
    """Whether *value* reads as OpenSCAD rather than words.  Unsure means
    yes: it is left out."""
    try:
        from kiln.original_design import _looks_like_openscad_code

        return bool(_looks_like_openscad_code(value))
    except Exception:  # noqa: BLE001 -- unsure is not sent
        return True


def made_with(
    door: str, arguments: dict[str, Any], mesh: str, envelope: dict[str, Any], fingerprint: str,
) -> dict[str, str]:
    """Everything said about one model Kiln made here: the door, the made
    file's name, its fingerprint, the id it is kept under when the make
    names one, and its naming words (the arguments in
    :data:`WORD_ARGUMENTS`, cleaned and capped)."""
    made_file = _word(os.path.basename(mesh))
    out: dict[str, str] = {DOOR_KEY: door, FILE_KEY: made_file or "model", FINGERPRINT_KEY: fingerprint}
    for owner in (envelope, envelope.get("design")):
        if isinstance(owner, dict) and isinstance(owner.get(VERSION_KEY), str) and owner[VERSION_KEY]:
            kept_as = _word(owner[VERSION_KEY], 120)
            if kept_as:
                out[VERSION_KEY] = kept_as
            break
    said = 1  # the made file's name
    for key in WORD_ARGUMENTS:
        value = arguments.get(key)
        if said >= MAX_WORDS or not isinstance(value, str) or not value.strip() or _is_source(value):
            continue
        word = _word(value)
        if not word or (key == FILE_KEY and word == made_file):
            continue
        out[ASKED_FILE_KEY if key == FILE_KEY else key] = word
        said += 1
    return out


def servers_quiet() -> bool:
    """Whether Kiln's servers just failed to answer and are being left alone."""
    return time.monotonic() < _servers_quiet_until


def note_miss(miss: Any) -> None:
    """Leave Kiln's servers alone for a while after they did not answer."""
    global _servers_quiet_until
    if getattr(miss, "code", "") == "SERVER_UNREACHABLE":
        _servers_quiet_until = time.monotonic() + SERVICE_BACKOFF_S


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
    """What a make that reached no goal is told, for *miss*'s cause: signed
    out, what signing in adds; any other cause, why in the one voice every
    unanswered ask of Kiln's servers is worded in."""
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


def say(envelope: dict[str, Any], miss: Any) -> None:
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
    "ASKED_FILE_KEY",
    "CONSULT_TIMEOUT_S",
    "DOOR_KEY",
    "MAX_WORDS",
    "MAX_WORD_CHARS",
    "SERVICE_BACKOFF_S",
    "SESSION_EXPIRED_LINE",
    "SIGNED_OUT_LINE",
    "WORD_ARGUMENTS",
    "line_for",
    "made_with",
    "note_miss",
    "say",
    "servers_quiet",
]
