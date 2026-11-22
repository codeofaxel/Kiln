"""Public-Kiln → Kiln's servers: Kiln Pro's printability check of one part.

Every install judges a part on this computer (:mod:`kiln.printability`).
When the signed-in plan includes Kiln Pro (https://kiln3d.com/pricing), the
same part is also judged on Kiln's servers, where the check is the one Kiln's
hosted ``analyze_printability`` gives that account, and the report takes the
servers' reading of each check (:func:`kiln.printability.analyze_printability`).
This module asks for that reading, through the door the served
``analyze_printability`` tool already uses (:func:`kiln.served_public.ask`),
so the part is sent the same way and the servers answer with the same tool.

It asks only when all of these hold: kiln-pro is not on this computer (where
it is, the check already runs here), the signed-in plan includes Kiln Pro
(read on this computer, no network, so a free install never waits on a check
its plan does not include), and the call is not one Kiln's servers were
already asked for and missed (:func:`kiln.served_public.already_missed`).
An answer is kept for the rest of the process per file content and
arguments, so a door that judges the same part twice sends it once.

A miss (offline, signed out, no answer, refused) leaves the check made on this
computer standing, and the report says so (:func:`miss_note`).  After Kiln's
servers leave an ask unanswered, they are left alone for a while.  Nothing
here raises.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from kiln.served_answer import Miss, classify_answer, sentence

logger = logging.getLogger(__name__)

#: The served tool whose answer carries the servers' reading.
WIRE_TOOL = "analyze_printability"
#: How long one ask waits for the servers' reading.
ASK_TIMEOUT_S: float = 60.0
#: After the servers leave an ask unanswered, how long nothing is asked.
SERVICE_BACKOFF_S: float = 300.0
#: How many parts' readings are kept in the process.
_KEPT = 32

_lock = threading.Lock()
_kept: OrderedDict[str, dict[str, Any]] = OrderedDict()
_down_until: float = 0.0
_down_miss: Miss | None = None


def reading_or_miss(file_path: str, arguments: dict[str, Any]) -> tuple[dict[str, Any] | None, Miss | None]:
    """The servers' report for *file_path* judged with *arguments* (the
    ``analyze_printability`` tool's own parameters), or why it did not come.

    ``(report, None)`` when it came; ``(None, miss)`` when the plan includes
    Kiln Pro and the ask missed; ``(None, None)`` when nothing was asked
    (kiln-pro is here, the plan does not include Kiln Pro, or this build has
    no served door for the tool).  Never raises.
    """
    try:
        return _reading_or_miss(file_path, arguments)
    except Exception as exc:  # noqa: BLE001 -- the check made here still stands
        logger.debug("printability reading not asked: %s", exc, exc_info=True)
        return None, None


def _reading_or_miss(file_path: str, arguments: dict[str, Any]) -> tuple[dict[str, Any] | None, Miss | None]:
    import sys

    from kiln import served_public

    if sys.modules.get("kiln_pro") is not None or not _plan_includes_the_check():
        return None, None
    entry = served_public._tools.get(WIRE_TOOL)
    if not entry:
        return None, None
    call = {"file_path": file_path, **{k: v for k, v in arguments.items() if v is not None}}
    if served_public.already_missed():
        # The servers were asked for this call and missed: that miss is the answer.
        return served_public.answer_or_missed(WIRE_TOOL, call)
    key = _key(file_path, call)
    if key is not None:
        with _lock:
            if key in _kept:
                _kept.move_to_end(key)
                return _kept[key], None
    if time.monotonic() < _down_until:
        # The servers just went unanswered: said again with the wait, under
        # the cause that started it (offline stays offline).
        held = _down_miss or Miss("unanswered", "SERVER_UNREACHABLE")
        return None, dataclasses.replace(held, retry_after_s=_down_until - time.monotonic())
    answer, miss = served_public.ask(WIRE_TOOL, dict(call), entry, timeout_s=ASK_TIMEOUT_S)
    if answer is None:
        miss = miss or Miss("unanswered")
        _note(miss)
        return None, miss
    report = answer.get("report") if isinstance(answer, dict) else None
    if not (isinstance(answer, dict) and answer.get("success") is True and isinstance(report, dict)):
        miss = classify_answer(answer) or Miss("unanswered", "NO_READING")
        _note(miss)
        return None, miss
    if key is not None:
        with _lock:
            _kept[key] = report
            while len(_kept) > _KEPT:
                _kept.popitem(last=False)
    return report, None


def miss_note(miss: Miss) -> str:
    """What the report says when the check the plan includes did not come:
    what was on the line, why, what Kiln did instead and the fix."""
    return sentence(
        miss,
        feature="servers",
        on_the_line=(
            "Your plan includes Kiln Pro's printability check, made on Kiln's servers "
            "(warping, cooling stress, bed hold and the brim)"
        ),
        cannot="make that check",
        wont="judged this part with the basic check on this computer instead, so the risks and the brim here may read lower than Kiln Pro's",
        then="check the part again",
    )


def _plan_includes_the_check() -> bool:
    """The signed-in account's plan covers Kiln Pro (read on this computer,
    no network).  Anything unreadable is the free plan."""
    try:
        from kiln.licensing import LicenseTier, check_tier

        covered, _why = check_tier(LicenseTier.PRO)
        return bool(covered)
    except Exception:  # noqa: BLE001
        return False


def _key(file_path: str, call: dict[str, Any]) -> str | None:
    """The part's content and the call's arguments, as one key; ``None``
    when the file cannot be read (the ask then names the miss itself)."""
    try:
        digest = hashlib.sha256(Path(file_path).expanduser().read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None
    rest = {k: v for k, v in call.items() if k != "file_path"}
    return digest + ":" + json.dumps(rest, sort_keys=True, default=str)


def _note(miss: Miss) -> None:
    global _down_until, _down_miss
    if miss.no_answer:
        _down_until = time.monotonic() + SERVICE_BACKOFF_S
        _down_miss = miss
    logger.debug("printability reading not served (%s %s)", miss.cause, miss.code)


__all__ = ["ASK_TIMEOUT_S", "WIRE_TOOL", "miss_note", "reading_or_miss"]
