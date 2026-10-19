"""Public-Kiln → Kiln's servers: Kiln Pro's ranking of one part's print poses.

``kiln.auto_orient.find_optimal_orientation`` measures every candidate pose
of a part on this computer (bed contact, support, height, overhang) and ranks
them.  Every install ranks by bed contact, the safe floor.  Kiln Pro
(https://kiln3d.com/pricing) ranks them with Kiln's own scorer, and this
module is where that ranking is asked for, once per part:

1. **kiln-pro on this computer** (the hosted server, and an install with
   kiln-pro): every pose gets Kiln's own score, imported by name in
   :func:`_local`, never by a string, so a static reader of the call graph
   sees the crossing.
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL` with the
   poses' ORDER, best first -- never scores -- asked through the door every
   served tool uses (:func:`kiln.server._pro_api_call`), and only when the
   signed-in plan includes Kiln Pro, so a free install never waits on a
   network call for a ranking its plan does not include.

A miss (offline, signed out, no answer, refused) is the floor ranking.  The
ranking is asked on the way to a print, so the served ask waits briefly and,
after the servers fail to answer, is left alone for a while.  Nothing here
raises.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error

logger = logging.getLogger(__name__)

#: The served tool that answers the order of one part's poses.
WIRE_TOOL = "orientation_rank"
#: Where the same ranking is worked out when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.orientation_scoring"
#: The facts a pose is ranked on, as the wire takes them.
FACTS = ("bed_contact_pct", "support_pct", "print_height_mm", "overhang_pct")
#: A ranking is asked on the way to a print; the floor answers if this passes.
_CONSULT_TIMEOUT_S: float = 4.0
#: After the servers fail to answer, how long the ranking is not asked for.
SERVICE_BACKOFF_S: float = 300.0

_service_down_until: float = 0.0


@dataclass(frozen=True)
class Ranking:
    """Kiln Pro's ranking of the poses asked about: a score per pose when
    kiln-pro ranked them here, else their order best first from Kiln's
    servers (``scores`` is then ``None``)."""

    scores: list[float] | None = None
    order: list[int] | None = None


def rank(poses: list[dict[str, float]]) -> Ranking | None:
    """Kiln Pro's ranking of *poses* (each with the measured :data:`FACTS`,
    other keys ignored), or ``None`` for the floor.  Never raises."""
    if not poses:
        return None
    local = _local(poses)
    if local is not _NOT_HERE:
        return local
    if not _plan_includes_the_ranking():
        return None
    return _served(poses)


def withheld_note() -> str | None:
    """The line for a caller Kiln's servers just ranked on the floor because
    the ranking is not in their plan, or ``None``.  kiln-pro words it where
    it decides; a plain install says nothing."""
    try:
        from kiln_pro.orientation_scoring import ranking_withheld_note
    except ImportError:
        return None
    try:
        note = ranking_withheld_note()
    except Exception:  # noqa: BLE001 -- a note never breaks the answer
        logger.debug("ranking note unavailable", exc_info=True)
        return None
    return note if isinstance(note, str) and note.strip() else None


#: ``_local``'s answer when kiln-pro is not on this computer.
_NOT_HERE = object()


def _local(poses: list[dict[str, float]]) -> Any:
    try:
        from kiln_pro.orientation_scoring import score_orientation
    except ImportError:
        return _NOT_HERE
    scores: list[float] = []
    for pose in poses:
        try:
            score = score_orientation(pose)
        except Exception as exc:  # noqa: BLE001 -- the bed-contact floor still answers
            logger.warning("kiln-pro orientation scorer failed, ranking by bed contact: %s", exc)
            return None
        if score is None:
            return None
        scores.append(float(score))
    return Ranking(scores=scores)


def _plan_includes_the_ranking() -> bool:
    """The signed-in account's plan covers Kiln Pro (read on this computer,
    no network).  Anything unreadable is the free plan."""
    try:
        from kiln.licensing import LicenseTier, check_tier

        covered, _why = check_tier(LicenseTier.PRO)
        return bool(covered)
    except Exception:  # noqa: BLE001
        return False


def _served(poses: list[dict[str, float]]) -> Ranking | None:
    global _service_down_until
    if time.monotonic() < _service_down_until:
        return None
    sent: list[dict[str, float]] = []
    for pose in poses:
        row = {key: float(pose.get(key, 0.0)) for key in FACTS}
        if not all(math.isfinite(v) for v in row.values()):
            return None
        sent.append(row)
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return None
    try:
        answer = _pro_api_call(
            WIRE_TOOL,
            _timeout=_CONSULT_TIMEOUT_S,
            # The person asked how to lay the part down; the ranking is what
            # they reached for.
            _asked_by_user=True,
            candidates=sent,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        _note_miss(classify_transport_error(exc))
        return None
    if not (isinstance(answer, dict) and answer.get("success") is True and answer.get("status") == "ok"):
        _note_miss(classify_answer(answer) if isinstance(answer, dict) else None)
        return None
    order = answer.get("order")
    if answer.get("known") is not True or not _is_order(order, len(sent)):
        return None
    return Ranking(order=list(order))


def _is_order(order: Any, size: int) -> bool:
    """Every pose's index exactly once."""
    return (
        isinstance(order, list)
        and all(isinstance(i, int) and not isinstance(i, bool) for i in order)
        and sorted(order) == list(range(size))
    )


def _note_miss(miss: Miss | None) -> None:
    """The servers did not answer (not a refusal): leave the ranking alone for
    a while so the next part does not wait on it again."""
    global _service_down_until
    if miss is not None and miss.cause in ("offline", "unanswered"):
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
    logger.debug("orientation ranking not served (%s); ranking by bed contact", miss and miss.cause)


__all__ = ["FACTS", "LOCAL_MODULE", "WIRE_TOOL", "Ranking", "rank", "withheld_note"]
