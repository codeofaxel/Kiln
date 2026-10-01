"""Public-Kiln → kiln-pro closest-filament bridge.

A colouring tool chooses its colours as codes (``#F72323``).  Which
filaments a person can buy come closest to each of them is kiln-pro's
answer (https://kiln3d.com).  This module hands kiln-pro the palette and
attaches whatever comes back, verbatim, under one field:
``closest_filaments``.  Nothing here reads, checks or reshapes the answer;
a door that finds itself interpreting it is on the wrong side of this file.

An answer comes from one of two places, tried in order:

1. **kiln-pro importable**: ``kiln_pro.filament_colours.closest_filaments``
   answers on this computer, and returns the column itself.
2. **served**: the signed-in user's Kiln asks Kiln's servers
   (:data:`WIRE_TOOL`) through the door every served tool uses
   (:func:`kiln.server._pro_api_call`).

Both are asked the same thing, by keyword::

    colours    the palette as ``#RRGGBB`` codes, each colour once, in the
               order the tool chose them, at most MAX_COLOURS of them
    material   the filament material to look within, or None when the
               tool named none (sent to the servers as "")

A served answer is a dict with ``success`` true and the column, a dict,
under ``closest_filaments``.  Anything else is a miss, and
:func:`unanswered` says why in the shared voice (:mod:`kiln.served_answer`).
When no answer comes, :func:`attach_closest_filaments` sets the field to
``None`` and adds that sentence to the response's warnings, so a missing
answer never reads as an empty one.  A palette with no colour code in it
asks nothing and attaches nothing.  The colouring tools in
:mod:`kiln.plugins.color_tools` call :func:`attach_closest_filaments`
through one helper; no function here raises.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from kiln.colour_distance import normalize_hex
from kiln.served_answer import Miss, classify_answer, classify_transport_error, fields, sentence

logger = logging.getLogger(__name__)

#: The served tool that answers for a palette's closest filaments.
WIRE_TOOL = "find_closest_filaments"
#: The most colours one ask carries; the served tool refuses more.
MAX_COLOURS: int = 16
#: A colouring is not a print start, and nothing else waits on this: a short
#: wait, and the backoff below stops a dead link costing it every colouring.
_ASK_TIMEOUT_S: float = 10.0
#: After the service fails to answer, how long it is left alone.  Same
#: backoff the cost, nozzle and cutter bridges use.
SERVICE_BACKOFF_S: float = 300.0
_service_down_until: float = 0.0
_service_down_miss: Miss | None = None
#: Why the last ask had no answer (a :class:`kiln.served_answer.Miss`),
#: cleared by an answer and by a palette with nothing in it to ask about.
_last_miss: Miss | None = None

#: The code a miss carries when nothing was asked of Kiln's servers at all:
#: the answer on this computer failed, or the connection to the servers
#: could not be opened.  Its sentence is its own detail, never one of the
#: served causes' fixes.
_NOT_ASKED = "NOT_ASKED"
_WITHOUT_IT = "so these colours come without the closest filaments you can buy"


def available() -> bool:
    """True when kiln-pro on this computer can answer for a palette."""
    try:
        from kiln_pro.filament_colours import closest_filaments  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 -- missing or broken, it cannot answer here
        return False


def closest_filaments(colours: Iterable[Any] | None, *, material: str | None = None) -> dict[str, Any] | None:
    """kiln-pro's closest-filament column for *colours*, or ``None``.

    From kiln-pro on this computer when it is installed, else from Kiln's
    servers.  ``None`` when nothing answered, and then :func:`unanswered`
    says why; ``None`` with nothing to say when *colours* holds no colour
    code.  Only the first :data:`MAX_COLOURS` distinct colours are asked
    about.
    """
    return _consult(colours, material)[0]


def _consult(colours: Iterable[Any] | None, material: str | None) -> tuple[dict[str, Any] | None, Miss | None]:
    """The column and why it is missing, the miss kept as the last one."""
    global _last_miss
    try:
        palette = _palette(colours)[:MAX_COLOURS]
        if not palette:
            answer, miss = None, None
        elif available():
            answer, miss = _local(palette, material)
        else:
            answer, miss = _served(palette, material)
    except Exception:  # noqa: BLE001 -- a colouring never fails for want of it
        logger.debug("closest filaments: the ask could not be made", exc_info=True)
        answer, miss = None, Miss("unanswered", _NOT_ASKED, "Kiln could not put the question together")
    _last_miss = miss
    return answer, miss


def _palette(colours: Iterable[Any] | None) -> list[str]:
    """*colours* as ``#RRGGBB`` codes, each once, in order; non-colours dropped."""
    if isinstance(colours, str):
        colours = [colours]
    out: list[str] = []
    for raw in colours or ():
        hex6 = normalize_hex(raw)
        if hex6 is not None and f"#{hex6}" not in out:
            out.append(f"#{hex6}")
    return out


def _local(palette: list[str], material: str | None) -> tuple[dict[str, Any] | None, Miss | None]:
    try:
        from kiln_pro.filament_colours import closest_filaments as lookup

        answer = lookup(list(palette), material=material)
    except Exception:  # noqa: BLE001 -- a colouring never fails for want of it
        logger.debug("closest filaments on this computer failed", exc_info=True)
        return None, Miss("unanswered", _NOT_ASKED, "Kiln's filament lookup on this computer stopped")
    if isinstance(answer, dict):
        return answer, None
    return None, Miss("unanswered", _NOT_ASKED, "Kiln's filament lookup on this computer gave no answer")


def _served(palette: list[str], material: str | None) -> tuple[dict[str, Any] | None, Miss | None]:
    global _service_down_until, _service_down_miss
    if time.monotonic() < _service_down_until:
        return None, _service_down_miss or Miss("unanswered", "SERVER_UNREACHABLE")
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        logger.debug("closest filaments: no served door", exc_info=True)
        return None, Miss("unanswered", _NOT_ASKED, "This install could not open its connection to Kiln's servers")
    try:
        # Nobody asked for this column; it rides along with a colouring.
        answer = _pro_api_call(
            WIRE_TOOL,
            _timeout=_ASK_TIMEOUT_S,
            _asked_by_user=False,
            colours=list(palette),
            material=material or "",
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a degrade, never a failed colouring
        logger.debug("closest filaments not served", exc_info=True)
        miss = classify_transport_error(exc)
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
        return None, miss
    if isinstance(answer, dict) and answer.get("success"):
        column = answer.get("closest_filaments")
        if isinstance(column, dict):
            return column, None
        return None, Miss("unanswered", detail="an answer with no closest filaments in it")
    miss = classify_answer(answer) or Miss("unanswered", detail="an answer that did not say it succeeded")
    if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
    return None, miss


def _message(miss: Miss) -> str:
    if miss.code == _NOT_ASKED:
        detail = miss.detail.strip().rstrip(".") or "Nothing was asked"
        return f"{detail[:1].upper()}{detail[1:]}, {_WITHOUT_IT}."
    return sentence(
        miss,
        feature="servers",
        on_the_line="A colouring names the closest filaments you can buy for each of its colours when Kiln's servers answer",
        cannot="look up the closest filaments for these colours",
        wont="gives the colouring without them",
        safe_remedy="The colouring itself stands",
        then="colour it again",
    )


def unanswered() -> dict[str, Any] | None:
    """Why the last ask had no answer, or ``None`` when it answered.

    ``{"message", "why", "why_code", "why_detail"}``: the sentence a door
    shows, with the cause and its code beside it (never inside it).
    """
    miss = _last_miss
    if miss is None:
        return None
    return {"message": _message(miss), **fields(miss)}


def attach_closest_filaments(
    response: dict[str, Any],
    colours: Iterable[Any] | None,
    *,
    material: str | None = None,
) -> None:
    """Ask for the closest filaments to *colours* and attach what comes back.

    The one helper a colouring calls, after its response is built.  A
    palette with no colour code in it sets nothing.  An answer lands in
    ``response["closest_filaments"]`` verbatim.  No answer sets it to
    ``None`` and adds one sentence to ``response["warnings"]`` saying why.
    A palette of more than :data:`MAX_COLOURS` colours is asked about its
    first ones, and a warning says so.  Never raises.
    """
    try:
        palette = _palette(colours)
        if not palette:
            return
        answer, miss = _consult(palette, material)
        if answer is not None:
            response["closest_filaments"] = answer
            if len(palette) > MAX_COLOURS:
                _warn(
                    response,
                    f"The closest filaments you can buy are listed for the first {MAX_COLOURS} of these "
                    f"{len(palette)} colours, the most Kiln looks up at once.",
                )
            return
        response["closest_filaments"] = None
        _warn(response, _message(miss or Miss("unanswered", _NOT_ASKED, "No answer came back")))
    except Exception:  # noqa: BLE001 -- a colouring is never lost to this
        logger.debug("closest filaments not attached", exc_info=True)


def _warn(response: dict[str, Any], text: str) -> None:
    warnings = response.get("warnings")
    if not isinstance(warnings, list):
        warnings = [warnings] if warnings else []
        response["warnings"] = warnings
    warnings.append(text)


__all__ = [
    "MAX_COLOURS",
    "SERVICE_BACKOFF_S",
    "WIRE_TOOL",
    "attach_closest_filaments",
    "available",
    "closest_filaments",
    "unanswered",
]
