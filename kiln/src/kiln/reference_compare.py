"""A make from a person's picture is compared with that picture, or says it was not.

THE CONTRACT
============
When a person hands over a picture of what they want ("make a 3D model of
this"), the picture is kept on the make's GOAL -- the saved goal
``design_session`` keeps -- and every make that joins the goal is compared
with it, round after round.  Comparing round three with round two instead
is how a sequence of honest improvements walks away from the picture.

The comparison itself is kiln-pro's.  This module is the one place public
Kiln asks for it, so every door says the same thing:

* :func:`compare_with_goal` -- called by :func:`kiln.front_door.join_goal`,
  the helper every make door calls, for a make that names its goal.  With
  kiln-pro installed it attaches ``result["reference_check"]`` for the
  pictures kept on that goal (nothing when the goal has none).
* :func:`compare_with_picture` -- for a make whose subject is the picture it
  was made from (an image-to-3D job), compared with that picture directly.
* :func:`picture_plan` -- for the door that TAKES the picture but makes no
  mesh itself: what will be compared, and where.

On an install without kiln-pro nothing here can read a goal or draw a
comparison, so the result carries ``reference_check`` with ``status``
:data:`NOT_COMPARED` and the sentence the agent needs.  Never silence: a
result with no block reads the same as one nobody asked to match a picture,
and that sameness is the failure this exists to end.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The key a make's result carries its comparison under.
REFERENCE_KEY = "reference_check"

#: The key the picture-taking door says its plan under.
PICTURE_PLAN_KEY = "reference_picture"

#: ``reference_check["status"]`` when no comparison could be drawn on this
#: install.  kiln-pro's own statuses (a measured comparison, an unreadable
#: picture, no renderer) are its to set.
NOT_COMPARED = "not_compared"

#: A make that names a goal, on an install that cannot read goals.
NOT_COMPARED_WITH_GOAL = (
    "Kiln on this computer cannot read this saved goal, so this result was not "
    "compared with any picture kept on it. If the person gave you a picture, "
    "look at it beside the result and say how they differ before presenting it."
)

#: A make from a picture, on an install that cannot compare.
NOT_COMPARED_WITH_PICTURE = (
    "Kiln on this computer cannot compare a result with a picture, so this one "
    "was not compared with the picture it was made from. Look at the picture "
    "beside the result and say how they differ before presenting it."
)

#: The comparison was asked for and could not be made.
COMPARISON_FAILED = (
    "Kiln could not compare this result with the picture it was meant to look "
    "like. Look at the picture beside the result and say how they differ "
    "before presenting it."
)

#: The picture-taking door's plan, where the comparison is available.
PICTURE_ON_THE_GOAL = (
    "Give this picture to design_session as reference_images: it is kept on "
    "the goal, and every make that joins the goal is compared with it."
)

#: The same, on an install that cannot compare.
PICTURE_NOT_COMPARED = (
    "Kiln on this computer cannot compare a make with a picture. Look at this "
    "picture beside every result and say how they differ before presenting it."
)


def _not_compared(result: dict[str, Any], reason: str, pictures: list[str]) -> None:
    result[REFERENCE_KEY] = {
        "status": NOT_COMPARED,
        "references": pictures,
        "reason": reason,
    }


def compare_with_goal(brief_id: str, result: Any, mesh_path: str | None = None) -> None:
    """Compare ``result`` with the pictures kept on goal ``brief_id``, in place.

    A no-op without a goal, for a result that is not a dict, and for one that
    already carries a comparison.  Never raises: a make is never lost to its
    comparison.
    """
    if not brief_id or not isinstance(result, dict) or REFERENCE_KEY in result:
        return
    try:
        from kiln_pro.design_brief.explicit_attach import (  # type: ignore[import-not-found]
            compare_with_goal_reference,
        )
    except ImportError:
        _not_compared(result, NOT_COMPARED_WITH_GOAL, [])
        return
    try:
        compare_with_goal_reference(brief_id, result, mesh_path)
    except Exception:  # noqa: BLE001 -- the make stands; the miss is said
        logger.warning("reference comparison with goal %s failed", brief_id, exc_info=True)
        _not_compared(result, COMPARISON_FAILED, [])


def compare_with_picture(
    result: Any, picture: str, mesh_path: str | None = None
) -> None:
    """Compare ``result`` with ``picture``, the picture it was made from, in place.

    Through the same comparison kiln-pro's inspect bundle draws for
    ``reference_images``, so a door that calls this before the bundle gets
    one comparison, not two.  A no-op without a picture or when the result
    already carries a comparison.  Never raises.
    """
    if not picture or not isinstance(result, dict) or REFERENCE_KEY in result:
        return
    try:
        from kiln_pro.design_brief.explicit_attach import (  # type: ignore[import-not-found]
            compare_with_pictures,
        )
    except ImportError:
        _not_compared(result, NOT_COMPARED_WITH_PICTURE, [picture])
        return
    try:
        compare_with_pictures(result, [picture], mesh_path)
    except Exception:  # noqa: BLE001 -- the make stands; the miss is said
        logger.warning("reference comparison with a picture failed", exc_info=True)
        _not_compared(result, COMPARISON_FAILED, [picture])


def comparison_available() -> bool:
    """Whether this install can compare a make with a picture."""
    try:
        from kiln_pro.design_brief.explicit_attach import (  # type: ignore[import-not-found]  # noqa: F401
            compare_with_pictures,
        )
    except ImportError:
        return False
    return True


def picture_plan(picture: str, *, arrives_through: str = "") -> dict[str, Any]:
    """What happens to ``picture`` -- for a door that takes a picture and makes
    no mesh itself.  ``arrives_through`` names the door the mesh will arrive
    through, when this door started a job that makes one."""
    if not comparison_available():
        return {"picture": picture, "compared": False, "note": PICTURE_NOT_COMPARED}
    note = PICTURE_ON_THE_GOAL
    if arrives_through:
        note = (
            f"The model arrives through {arrives_through}, which compares it "
            f"with this picture. {note}"
        )
    return {"picture": picture, "compared": True, "note": note}
