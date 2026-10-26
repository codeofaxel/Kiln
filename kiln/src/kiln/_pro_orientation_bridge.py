"""Public-Kiln → Kiln's servers: Kiln Pro's ranking of one part's print poses.

``kiln.auto_orient.find_optimal_orientation`` measures every candidate pose
of a part on this computer (bed contact, support, height, overhang) and ranks
them.  Every install ranks by bed contact, the safe floor.  Kiln Pro
(https://kiln3d.com/pricing) ranks them its own way, and this module is where
that ranking is asked for, once per part.  The answer is the poses' ORDER,
best first, and only ever the order, wherever it is worked out:

1. **kiln-pro on this computer** (the hosted server, and an install with
   kiln-pro): asked in process of the same function that answers the wire
   (``rank_for_the_wire``), imported by name in :func:`_local`, never by a
   string, so a static reader of the call graph sees the crossing.
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked through
   the door every served tool uses (:func:`kiln.server._pro_api_call`), and
   only when the signed-in plan includes Kiln Pro, so a free install never
   waits on a network call for a ranking its plan does not include.

No score crosses either way: a score per pose, beside the facts it was worked
out from, would hand a caller the formula.  So nothing public Kiln shows about
a pose is Kiln Pro's arithmetic; only which pose comes first is Kiln Pro's.

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
#: The facts that are shares of something, so a measurement over 100 is the
#: whole of it: bodies an export left overlapping, un-merged, count the same
#: patch of bed once per body (two 20 mm squares, one shifted 5 mm along X and
#: along Y, read 128%).
_SHARES = ("bed_contact_pct", "support_pct", "overhang_pct")
#: A ranking is asked on the way to a print; the floor answers if this passes.
_CONSULT_TIMEOUT_S: float = 4.0
#: After the servers fail to answer, how long the ranking is not asked for.
SERVICE_BACKOFF_S: float = 300.0

_service_down_until: float = 0.0


@dataclass(frozen=True)
class Ranking:
    """Kiln Pro's ranking of the poses asked about: their indices, best
    first.  There is no score to carry, whether kiln-pro ranked them here or
    Kiln's servers did."""

    order: list[int]


def rank(poses: list[dict[str, float]]) -> Ranking | None:
    """Kiln Pro's order of *poses* (each with the measured :data:`FACTS`,
    other keys ignored), or ``None`` for the floor.  Never raises."""
    if not poses:
        return None
    rows = _rows(poses)
    if rows is None:
        return None
    local = _local(rows)
    if local is not _NOT_HERE:
        return local
    if not _plan_includes_the_ranking():
        return None
    return _served(rows)


def _rows(poses: list[dict[str, float]]) -> list[dict[str, float]] | None:
    """*poses* as the ranking takes them: the four :data:`FACTS`, a share
    held to 0..100 (:data:`_SHARES`).  ``None`` when a fact is not a finite
    number, which no measurement gives."""
    rows: list[dict[str, float]] = []
    for pose in poses:
        try:
            row = {key: float(pose.get(key, 0.0)) for key in FACTS}
        except (AttributeError, TypeError, ValueError):
            return None
        if not all(math.isfinite(v) for v in row.values()):
            return None
        for key in _SHARES:
            row[key] = min(100.0, max(0.0, row[key]))
        rows.append(row)
    return rows


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


def _local(rows: list[dict[str, float]]) -> Any:
    """kiln-pro's order of *rows*, asked here exactly as the wire asks it."""
    try:
        from kiln_pro.orientation_scoring import rank_for_the_wire
    except ImportError:
        return _NOT_HERE
    try:
        answer = rank_for_the_wire(rows)
    except Exception as exc:  # noqa: BLE001 -- the bed-contact floor still answers
        logger.warning("kiln-pro orientation ranking failed, ranking by bed contact: %s", exc)
        return None
    return _ranking_in(answer, len(rows))


def _plan_includes_the_ranking() -> bool:
    """The signed-in account's plan covers Kiln Pro (read on this computer,
    no network).  Anything unreadable is the free plan."""
    try:
        from kiln.licensing import LicenseTier, check_tier

        covered, _why = check_tier(LicenseTier.PRO)
        return bool(covered)
    except Exception:  # noqa: BLE001
        return False


def _served(rows: list[dict[str, float]]) -> Ranking | None:
    if time.monotonic() < _service_down_until:
        return None
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
            candidates=rows,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        _note_miss(classify_transport_error(exc))
        return None
    if not (isinstance(answer, dict) and answer.get("success") is True and answer.get("status") == "ok"):
        _note_miss(classify_answer(answer) if isinstance(answer, dict) else None)
        return None
    return _ranking_in(answer, len(rows))


def _ranking_in(answer: Any, size: int) -> Ranking | None:
    """The order in a ranking answer, from kiln-pro here or from the wire;
    ``None`` unless it is known and names every pose exactly once.  Only the
    order is read, so nothing else the answer carried can reach a reply."""
    if not (isinstance(answer, dict) and answer.get("success") is True and answer.get("known") is True):
        return None
    order = answer.get("order")
    return Ranking(order=list(order)) if _is_order(order, size) else None


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
