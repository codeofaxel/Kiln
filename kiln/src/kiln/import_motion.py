"""Imported files that move: whether a copy goes to Kiln's servers so the
way the parts move can be worked out, and what the person is told.

On a signed-in install, a model that came from outside Kiln (an import, a
download, a generator's result) and holds two or more separate parts is
sent to Kiln's servers as a copy, with nothing said about how it moves.  The
answer carries what plays it on a stage, which is kept beside the file here
(:mod:`kiln._pro_goal_bridge`).  A model in one piece is never sent for this.

The person is told once, the first time a copy goes, what was sent, that it
is not one of their designs, when Kiln deletes it, and how to turn it off.
The choice belongs to this install (:func:`set_sending`) and stands until it
is changed.

Kiln Business and Enterprise start with it off, so nothing a team works on
leaves its computers unasked; the first such file is followed by one line
saying how to turn it on.

Every function here answers rather than raises: a setting that cannot be
read is the plan's default, and a file that cannot be read has no parts to
send.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: This install's own choice, ``"on"`` or ``"off"``; absent, the plan decides.
SETTING_KEY = "import_motion_sending"
#: Which of the lines below this install has been told, one key each.
TOLD_SENT_KEY = "import_motion_told_sent"
TOLD_OFF_KEY = "import_motion_told_off"

#: Where a make's reply says what happened to its file's copy.
REPLY_KEY = "about_the_copy"

#: The words a person says to change the choice, as the lines below quote them.
SAY_OFF = "keep my imports on this computer"
SAY_ON = "work out how my imports move"

#: Said once, the first time a copy is sent.
SENT_LINE = (
    "To work out how it moves, Kiln sent a copy of this file to its servers. "
    "It isn't one of your designs, and Kiln deletes it within about a month. "
    f'Say "{SAY_OFF}" to turn this off.'
)
#: Said once, the first time a file could have gone and the plan kept it here.
OFF_LINE = (
    "Kiln can work out how this file's parts move by sending a copy to its servers. "
    f'That is off on your plan; say "{SAY_ON}" to turn it on.'
)


def _db() -> Any:
    from kiln.persistence import get_db

    return get_db()


def chosen() -> bool | None:
    """This install's own choice, or ``None`` when nobody has made one."""
    try:
        value = _db().get_setting(SETTING_KEY)
    except Exception:  # noqa: BLE001 -- an unreadable choice is no choice
        logger.debug("import motion: the choice could not be read", exc_info=True)
        return None
    return {"on": True, "off": False}.get(str(value or ""))


def plan_default() -> bool:
    """On for Free and Kiln Pro; off for Kiln Business and Enterprise."""
    try:
        from kiln.account_plan import LicenseTier, get_tier

        return not get_tier() >= LicenseTier.BUSINESS
    except Exception:  # noqa: BLE001 -- a plan that cannot be read sends nothing
        logger.debug("import motion: the plan could not be read", exc_info=True)
        return False


def sending() -> bool:
    """Whether a copy of an outside file with separate parts goes, unasked."""
    mine = chosen()
    return plan_default() if mine is None else mine


def set_sending(on: bool) -> None:
    """Keep this install's choice.  Raises only when the store cannot be
    written, which the caller says."""
    _db().set_setting(SETTING_KEY, "on" if on else "off")


def _once(key: str) -> bool:
    """``True`` the first time *key* is asked about on this install, and
    remembered from then on.  ``False`` when the store cannot be written:
    a line that cannot be remembered is not said over and over."""
    try:
        db = _db()
        if db.get_setting(key):
            return False
        db.set_setting(key, "1")
        return True
    except Exception:  # noqa: BLE001
        logger.debug("import motion: %s could not be remembered", key, exc_info=True)
        return False


def tell_sent(envelope: dict[str, Any]) -> None:
    """Say, the first time only, that a copy went and how to stop it."""
    if REPLY_KEY not in envelope and _once(TOLD_SENT_KEY):
        envelope[REPLY_KEY] = SENT_LINE


def tell_off(envelope: dict[str, Any]) -> None:
    """Say, the first time only, that the plan kept the file here and how to
    turn sending on.  Never after the person made their own choice."""
    if chosen() is None and REPLY_KEY not in envelope and _once(TOLD_OFF_KEY):
        envelope[REPLY_KEY] = OFF_LINE


def has_separate_parts(mesh: str) -> bool:
    """Whether the model at *mesh* holds two or more pieces that do not
    touch, counted as every door counts pieces (:mod:`kiln.mesh_bodies`),
    on any install Kiln runs on.  A file too large to send, or one that
    cannot be read, holds none."""
    try:
        from kiln.served_makes import MAX_MODEL_BYTES

        if os.path.getsize(mesh) > MAX_MODEL_BYTES:
            return False
        from kiln import mesh_bodies, mesh_frame

        model = mesh_frame.load_mesh(mesh, process=False, force="mesh")
        if len(model.faces) < 2:
            return False
        return mesh_bodies.read_mesh(model).piece_count >= 2
    except Exception:  # noqa: BLE001 -- unreadable is "nothing to send"
        logger.debug("import motion: %s could not be read for its parts", mesh, exc_info=True)
        return False


def state() -> dict[str, Any]:
    """What this install does with outside files that have separate parts,
    as a tool answers it."""
    mine = chosen()
    on = plan_default() if mine is None else mine
    if on:
        says = (
            "Kiln sends a copy of an imported file with separate parts to its servers to work out how "
            "the parts move. The copy isn't one of your designs and is deleted within about a month. "
            f'Say "{SAY_OFF}" to stop.'
        )
    else:
        says = (
            "Imported files stay on this computer, and Kiln does not work out how their parts move. "
            f'Say "{SAY_ON}" to turn it on.'
        )
    return {"sending": on, "chosen_here": mine is not None, "says": says}
