"""Public-Kiln → kiln-pro cost-intelligence bridge.

A cost estimate for a print file is public Kiln's own arithmetic: each
filament's grams and price, the time, the electricity.  Cost intelligence
is kiln-pro's (https://kiln3d.com).  This module hands kiln-pro the print
file and the estimate's own numbers, and attaches whatever comes back,
verbatim, under one field: ``cost_intelligence``.  Nothing here reads,
checks or reshapes the answer; a door that finds itself interpreting it is
on the wrong side of this file.

An answer comes from one of two places, tried in order:

1. **kiln-pro importable**: ``kiln_pro.cost_intelligence.consult`` answers
   on this computer, given the file's path.
2. **served**: the signed-in user's Kiln asks Kiln's servers
   (:data:`WIRE_TOOL`) through the door every served tool uses
   (:func:`kiln.server._pro_api_call`).

The served request, by keyword::

    gcode_gz_b64            the file's G-code, gzipped then base64-encoded:
                            a .gcode/.gco/.g file itself, or the plate a
                            sliced 3MF prints; never sent when it is over
                            MAX_SENT_BYTES before compression
    file_name               the file's base name
    filaments               the estimate's per-filament entries
    total_cost_usd          the estimate's total, a float
    estimated_time_seconds  the estimate's time, an int, or None
    printer_id              the machine the estimate is for, or ""

An answer is a dict with ``success`` true.  Anything else is a miss, and
:func:`unanswered` says why in the shared voice (:mod:`kiln.served_answer`).
When no answer comes, :func:`attach_cost_intelligence` sets the field to
``None`` and adds that sentence to the estimate's warnings, so a missing
answer never reads as an empty one.  Every door that produces a print
file's cost estimate calls :func:`attach_cost_intelligence`; no function
here raises.
"""

from __future__ import annotations

import base64
import gzip
import logging
import os
import time
import zipfile
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error, fields, sentence

logger = logging.getLogger(__name__)

#: The served tool that answers for a print file's cost estimate.
WIRE_TOOL = "print_cost_intelligence"
#: Long enough to carry a large plate's G-code up; an estimate is not a print
#: start, and the backoff below stops a dead link costing this twice.
_CONSULT_TIMEOUT_S: float = 30.0
#: A file whose G-code is larger than this, before compression, is not sent.
MAX_SENT_BYTES: int = 64 * 1024 * 1024
#: After the service fails to answer, how long it is left alone.  Same
#: backoff the nozzle and cutter bridges use.
SERVICE_BACKOFF_S: float = 300.0
_service_down_until: float = 0.0
_service_down_miss: Miss | None = None
#: Why the last consult had no answer (a :class:`kiln.served_answer.Miss`),
#: cleared by an answer.
_last_miss: Miss | None = None

_GCODE_SUFFIXES = (".gcode", ".gco", ".g")
#: The code a miss carries when nothing was asked of Kiln's servers at all:
#: the file could not be sent, or the answer on this computer failed.  Its
#: sentence is its own detail, never one of the served causes' fixes.
_NOT_ASKED = "NOT_ASKED"
_TOO_LARGE = "the file is too large to send"


def available() -> bool:
    """True when kiln-pro is installed with its cost intelligence."""
    try:
        import kiln_pro.cost_intelligence  # noqa: F401

        return True
    except ImportError:
        return False


def consult_print_cost(
    file_path: str,
    *,
    filaments: list[dict[str, Any]],
    total_cost_usd: float,
    estimated_time_seconds: int | None,
    printer_id: str = "",
) -> dict[str, Any] | None:
    """kiln-pro's cost intelligence for *file_path*, or ``None``.

    From kiln-pro on this computer when it is installed, else from Kiln's
    servers.  ``None`` when nothing answered, and then :func:`unanswered`
    says why.
    """
    global _last_miss
    if available():
        answer, miss = _local(
            file_path,
            filaments=filaments,
            total_cost_usd=total_cost_usd,
            estimated_time_seconds=estimated_time_seconds,
            printer_id=printer_id,
        )
    else:
        answer, miss = _served(
            file_path,
            filaments=filaments,
            total_cost_usd=total_cost_usd,
            estimated_time_seconds=estimated_time_seconds,
            printer_id=printer_id,
        )
    _last_miss = miss
    return answer


def _local(
    file_path: str,
    *,
    filaments: list[dict[str, Any]],
    total_cost_usd: float,
    estimated_time_seconds: int | None,
    printer_id: str,
) -> tuple[dict[str, Any] | None, Miss | None]:
    try:
        from kiln_pro.cost_intelligence import consult

        answer = consult(
            file_path=file_path,
            filaments=filaments,
            total_cost_usd=total_cost_usd,
            estimated_time_seconds=estimated_time_seconds,
            printer_id=printer_id,
        )
    except Exception as exc:  # noqa: BLE001 -- an estimate never fails for want of it
        logger.debug("cost intelligence on this computer failed", exc_info=True)
        return None, Miss("unanswered", _NOT_ASKED, f"cost intelligence on this computer stopped: {exc}"[:400])
    if isinstance(answer, dict):
        return answer, None
    return None, Miss("unanswered", _NOT_ASKED, "cost intelligence on this computer gave no answer")


def _served(
    file_path: str,
    *,
    filaments: list[dict[str, Any]],
    total_cost_usd: float,
    estimated_time_seconds: int | None,
    printer_id: str,
) -> tuple[dict[str, Any] | None, Miss | None]:
    global _service_down_until, _service_down_miss
    if time.monotonic() < _service_down_until:
        return None, _service_down_miss or Miss("unanswered", "SERVER_UNREACHABLE")
    body, why_not = _gcode_bytes(file_path)
    if body is None:
        return None, Miss("unanswered", _NOT_ASKED, why_not)
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return None, Miss("unanswered", _NOT_ASKED, "this install could not open its connection to Kiln's servers")
    try:
        answer = _pro_api_call(
            WIRE_TOOL,
            _timeout=_CONSULT_TIMEOUT_S,
            gcode_gz_b64=base64.b64encode(gzip.compress(body, compresslevel=6)).decode("ascii"),
            file_name=os.path.basename(file_path),
            filaments=filaments,
            total_cost_usd=float(total_cost_usd),
            estimated_time_seconds=int(estimated_time_seconds) if estimated_time_seconds is not None else None,
            printer_id=printer_id,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a degrade, never an estimate
        logger.debug("cost intelligence not served", exc_info=True)
        miss = classify_transport_error(exc)
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
        return None, miss
    if isinstance(answer, dict) and answer.get("success"):
        return answer, None
    miss = classify_answer(answer) or Miss("unanswered", detail="an answer that did not say it succeeded")
    if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
    return None, miss


def _gcode_bytes(file_path: str) -> tuple[bytes | None, str]:
    """The G-code *file_path* prints, as bytes, or ``None`` and why not.

    A ``.gcode`` / ``.gco`` / ``.g`` file is itself; a sliced 3MF's is the
    plate member :func:`kiln.gcode_metadata.sliced_gcode_member` names.
    Over :data:`MAX_SENT_BYTES` is refused before it is read, whenever the
    size is known first.
    """
    lower = file_path.lower()
    try:
        if lower.endswith(".3mf"):
            from kiln.gcode import _MAX_SCAN_BYTES
            from kiln.gcode_metadata import read_member_text, sliced_gcode_member

            with zipfile.ZipFile(file_path) as zf:
                member = sliced_gcode_member(zf)
                if member is None:
                    return None, "the file holds a model, not sliced G-code"
                if zf.getinfo(member).file_size > MAX_SENT_BYTES:
                    return None, _TOO_LARGE
                body = read_member_text(zf, member, _MAX_SCAN_BYTES).encode("utf-8")
        elif lower.endswith(_GCODE_SUFFIXES):
            if os.path.getsize(file_path) > MAX_SENT_BYTES:
                return None, _TOO_LARGE
            with open(file_path, "rb") as fh:
                body = fh.read()
        else:
            return None, "the file is not a G-code file or a sliced 3MF"
    except (OSError, ValueError, zipfile.BadZipFile):
        logger.debug("cost intelligence: %s could not be read", os.path.basename(file_path), exc_info=True)
        return None, "the file could not be read"
    if len(body) > MAX_SENT_BYTES:
        return None, _TOO_LARGE
    return body, ""


def _message(miss: Miss) -> str:
    if miss.code == _NOT_ASKED:
        detail = miss.detail.strip().rstrip(".") or "nothing was asked"
        return f"{detail[:1].upper()}{detail[1:]}, so this estimate comes without it."
    return sentence(
        miss,
        feature="servers",
        on_the_line="A print file's cost estimate carries cost intelligence when Kiln's servers answer",
        cannot="get cost intelligence for this print",
        wont="gives the estimate without it",
        safe_remedy="The estimate itself stands",
        then="estimate again",
    )


def unanswered() -> dict[str, Any] | None:
    """Why the last consult had no answer, or ``None`` when it answered.

    ``{"message", "why", "why_code", "why_detail"}``: the sentence a door
    shows, with the cause and its code beside it (never inside it).
    """
    miss = _last_miss
    if miss is None:
        return None
    return {"message": _message(miss), **fields(miss)}


def attach_cost_intelligence(
    estimate: dict[str, Any],
    file_path: str,
    *,
    printer_id: str = "",
    filament_count: int | None = None,
    filaments: list[dict[str, Any]] | None = None,
    estimated_time_seconds: int | None = None,
) -> None:
    """Ask for cost intelligence on *estimate* and attach what comes back.

    The one helper every door calls, after its estimate dict is built.  It
    asks only for a file of two or more filaments, or when *printer_id*
    names the machine the estimate is for; otherwise it sets nothing.  An
    answer lands in ``estimate["cost_intelligence"]`` verbatim.  No answer
    sets it to ``None`` and adds one sentence to ``estimate["warnings"]``
    saying why.  Never raises.

    *filaments*, *filament_count* and *estimated_time_seconds* stand in for
    the estimate's own ``filaments`` / ``estimated_time_seconds`` when a
    door's estimate dict does not carry them (the pre-flight's).
    """
    try:
        entries = filaments if filaments is not None else (estimate.get("filaments") or [])
        count = filament_count if filament_count is not None else len(entries)
        if (count < 2 and not printer_id) or not file_path:
            return
        seconds = estimated_time_seconds if estimated_time_seconds is not None else estimate.get("estimated_time_seconds")
        answer = consult_print_cost(
            str(file_path),
            filaments=list(entries),
            total_cost_usd=float(estimate.get("total_cost_usd") or 0.0),
            estimated_time_seconds=seconds,
            printer_id=printer_id or "",
        )
        if answer is not None:
            estimate["cost_intelligence"] = answer
            return
        gap = unanswered()
        message = gap["message"] if gap else "No answer came back, so this estimate comes without it."
        estimate["cost_intelligence"] = None
        warnings = estimate.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
            estimate["warnings"] = warnings
        warnings.append(f"Kiln could not ask for cost intelligence: {message}")
    except Exception:  # noqa: BLE001 -- an estimate is never lost to this
        logger.debug("cost intelligence not attached", exc_info=True)


__all__ = [
    "MAX_SENT_BYTES",
    "SERVICE_BACKOFF_S",
    "WIRE_TOOL",
    "attach_cost_intelligence",
    "available",
    "consult_print_cost",
    "unanswered",
]
