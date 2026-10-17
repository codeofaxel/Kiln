"""Public-Kiln → kiln-pro bridge for ONE printer's catalogue record.

The limits a print on a catalogued printer is held to, the curated hardware
variants that may raise one, the spec sheet and the per-material recipes,
the design capability profile and the tuned slicer profile are kiln-pro's
(https://kiln3d.com): one record per printer, served one printer at a time.
Public Kiln keeps the ROSTER -- which printers exist and what they are
called -- the ``motion`` block, the multi-material add-on table and the
generic ``default`` rows.  Its four loaders (:mod:`kiln.safety_profiles`,
:mod:`kiln.printer_intelligence`, :mod:`kiln.design_intelligence`,
:mod:`kiln.slicer_profiles`) ask here for a row they do not hold and parse
their block of the answer into their own caches, so every door downstream
resolves exactly as it did when the rows were bundled.

A record comes from one of four places, tried in order:

1. **kiln-pro importable** (and the hosted server, which is one):
   :func:`kiln_pro.device_intelligence.printer_catalogue.printer_record`
   answers on this computer.  Nothing is kept on disk: kiln-pro is the disk.
2. **kept on this computer**, fresh: the answer the servers gave when the
   printer was set up, in ``~/.kiln/printer_catalogue/<id>.json``.  The
   catalogue changes only when Kiln is deployed, so a kept record is read
   for a week before it is asked about again.
3. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked through
   the door every served tool uses (:func:`kiln.server._pro_api_call`), and
   the answer is kept (2) -- this is what makes print-time safety work
   offline after setup.
4. **kept, however old**: on any miss (offline, signed out, the service
   silent) the record this computer was last told is still the record.

No record is a miss, worded by :func:`why_unanswered` from the four served
causes (:mod:`kiln.served_answer`), and the loaders fall back to the generic
floor: a ceiling below every catalogued printer's, never above one.  A
printer the catalogue has no row for is a SETTLED answer (``known: false``),
remembered for the process so an unknown machine is not asked about on every
command, and never kept on disk, so a row added later is found.

One record per call, never a roster: the catalogue is served the way every
other curated table is.  Nothing here raises.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error, clause

logger = logging.getLogger(__name__)

#: The served tool that answers for one printer.
WIRE_TOOL = "get_printer_record"
#: The four blocks a record carries; each public loader parses one of them
#: (safety_profiles / printer_intelligence / design_intelligence /
#: slicer_profiles).  A kept record from before a block existed fails
#: :func:`_is_record` and is asked for again, never read half.
BLOCKS = ("safety", "intel", "design", "slicer")
#: Where the same answer is read from when kiln-pro IS installed here
#: (imported by name in :func:`_local`, never by a string, so a static
#: reader of the call graph sees the crossing).
LOCAL_MODULE = "kiln_pro.device_intelligence.printer_catalogue"
#: Setting a printer up is a person waiting; a command on the way to a
#: print must not wait longer than this for a record it can do without.
_CONSULT_TIMEOUT_S: float = 4.0
#: After the service fails to answer, how long it is left alone.  The same
#: backoff the nozzle, blade and melt bridges use.
SERVICE_BACKOFF_S: float = 300.0
#: How long a kept record is read without asking again.  The catalogue is
#: corrected by a deploy, not by the hour.
FRESH_FOR_S: float = 7 * 24 * 3600.0
#: Where a served record is kept on an install without kiln-pro.
KEEP_DIR = Path.home() / ".kiln" / "printer_catalogue"

_service_down_until: float = 0.0
_service_down_miss: Miss | None = None
#: spelling (as asked) -> the record, or ``None`` for a settled "no row".
_answers: dict[str, dict[str, Any] | None] = {}
#: spelling -> why the last ask for it had no answer; cleared by an answer.
_last_miss: dict[str, Miss] = {}
#: spelling -> when it may be asked about again after a miss that was not
#: the service's (signed out, refused).  Every command on the way to a
#: print asks for the record; without this a signed-out install would read
#: its token file on each one.  Short, so a sign-in is noticed within it.
_retry_after: dict[str, float] = {}
MISS_RETRY_S: float = 15.0
#: A miss with nothing asked of Kiln's servers: kiln-pro on this computer failed.
_NOT_ASKED = "NOT_ASKED"


def available() -> bool:
    """True when kiln-pro is installed here WITH its catalogue file.

    The reader alone is not enough: kiln-pro's wheel leaves the file out,
    so a pip-installed kiln-pro has the reader and nothing to read, and
    trusting it would hand every printer the floor and never ask the
    servers.  The Fly deploy and a repo checkout have the file.
    """
    try:
        from kiln_pro.device_intelligence.printer_catalogue import catalogue_present

        return bool(catalogue_present())
    except Exception:  # noqa: BLE001 -- an older kiln-pro, or none: the served door
        return False


def _key(printer_id: Any) -> str:
    return str(printer_id or "").strip().lower() if isinstance(printer_id, str) else ""


def _is_record(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("printer_id"), str)
        and bool(value["printer_id"])
        and all(isinstance(value.get(block), dict) for block in BLOCKS)
    )


def printer_record(printer_id: Any) -> dict[str, Any] | None:
    """ONE printer's record for *printer_id*, or ``None``.

    ``{"printer_id", "matched_by", "display_name", "safety", "intel",
    "design", "slicer"}`` --
    ``printer_id`` is the catalogue's own id, which may differ from the
    spelling asked (``creality_k1c`` is answered as ``k1c``), and is the key
    a loader caches under.  ``None`` is one of two things, told apart by
    :func:`why_unanswered`: the catalogue has no row (``""``), or nothing
    answered (a served cause).  Never raises.
    """
    key = _key(printer_id)
    if not key:
        return None
    if available():
        record, miss = _local(key)
        if miss is None:
            _last_miss.pop(key, None)
            return record
        _last_miss[key] = miss
        return None
    if key in _answers:
        return _answers[key]
    kept = _kept(key)
    if kept is not None and kept[1] <= FRESH_FOR_S:
        _last_miss.pop(key, None)
        _answers[key] = kept[0]
        return kept[0]
    if time.monotonic() < _retry_after.get(key, 0.0):
        return kept[0] if kept is not None else None
    answer, miss = _served(key)
    if miss is None:
        _last_miss.pop(key, None)
        _retry_after.pop(key, None)
        _answers[key] = answer
        if answer is not None:
            _keep(key, answer)
        return answer
    _last_miss[key] = miss
    _retry_after[key] = time.monotonic() + MISS_RETRY_S
    if kept is not None:
        return kept[0]
    return None


def _local(key: str) -> tuple[dict[str, Any] | None, Miss | None]:
    try:
        from kiln_pro.device_intelligence.printer_catalogue import printer_record as local_record

        record = local_record(key)
    except Exception as exc:  # noqa: BLE001 -- a loader never fails for want of it
        logger.debug("printer catalogue on this computer failed", exc_info=True)
        return None, Miss("unanswered", _NOT_ASKED, f"kiln-pro on this computer stopped: {exc}"[:400])
    return (record if _is_record(record) else None), None


def _served(key: str) -> tuple[dict[str, Any] | None, Miss | None]:
    """The hosted record (``None`` for a settled "no row"), or a miss."""
    global _service_down_until, _service_down_miss
    if time.monotonic() < _service_down_until:
        return None, _service_down_miss or Miss("unanswered", "SERVER_UNREACHABLE")
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return None, Miss("unanswered", _NOT_ASKED, "this install could not open its connection to Kiln's servers")
    try:
        answer = _pro_api_call(WIRE_TOOL, _timeout=_CONSULT_TIMEOUT_S, _asked_by_user=False, printer_model=key)
    except Exception as exc:  # noqa: BLE001 -- the network is a degrade, never a refusal to go on
        logger.debug("printer record not served", exc_info=True)
        miss = classify_transport_error(exc)
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
        return None, miss
    if isinstance(answer, dict) and answer.get("success"):
        if answer.get("known") is False:
            return None, None
        if _is_record(answer):
            return {
                "printer_id": answer["printer_id"],
                "matched_by": str(answer.get("matched_by") or ""),
                "display_name": str(answer.get("display_name") or answer["printer_id"]),
                **{block: dict(answer[block]) for block in BLOCKS},
            }, None
        return None, Miss("unanswered", detail="an answer with no record in it")
    miss = classify_answer(answer) or Miss("unanswered", detail="an answer that did not say it succeeded")
    if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
    return None, miss


# ---------------------------------------------------------------------------
# Kept on this computer (an install without kiln-pro only)
# ---------------------------------------------------------------------------


def _keep_path(key: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "_-." else "_" for ch in key)[:96]
    return KEEP_DIR / f"{safe}.json"


def _kept(key: str) -> tuple[dict[str, Any], float] | None:
    """``(record, age in seconds)`` kept for *key*, or ``None``."""
    path = _keep_path(key)
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- an unreadable record is no record
        logger.debug("kept printer record unreadable", exc_info=True)
        return None
    record = raw.get("record") if isinstance(raw, dict) else None
    if not _is_record(record):
        return None
    try:
        kept_at = float(raw.get("kept_at") or 0.0)
    except (TypeError, ValueError):
        kept_at = 0.0
    return record, max(0.0, time.time() - kept_at)


def _keep(key: str, record: dict[str, Any]) -> None:
    try:
        KEEP_DIR.mkdir(parents=True, exist_ok=True)
        path = _keep_path(key)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"kept_at": time.time(), "record": record}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 -- not kept costs one more ask later
        logger.debug("printer record not kept", exc_info=True)


def kept_record(printer_id: Any) -> dict[str, Any] | None:
    """The record this computer was last told for *printer_id*, however old,
    or ``None``.  Never asks anything."""
    kept = _kept(_key(printer_id))
    return kept[0] if kept else None


# ---------------------------------------------------------------------------
# Misses
# ---------------------------------------------------------------------------


def last_miss(printer_id: Any) -> Miss | None:
    """Why the last ask for *printer_id* had no answer, or ``None``."""
    return _last_miss.get(_key(printer_id))


def why_unanswered(printer_id: Any) -> str:
    """Which served cause left the last ask for *printer_id* without an
    answer: ``offline`` / ``signed_out`` / ``unanswered`` / ``refused``, or
    ``""`` when it was answered (a record, or a settled "no row")."""
    miss = _last_miss.get(_key(printer_id))
    return str(getattr(miss, "cause", "") or "") if miss is not None else ""


def unanswered(printer_id: Any) -> str | None:
    """Why this printer's record could not be had, as a clause, or ``None``.

    Worded to follow the generic floor's own reason in a door's note.
    """
    miss = _last_miss.get(_key(printer_id))
    if miss is None:
        return None
    if miss.code == _NOT_ASKED:
        return f"Kiln can't read this printer's record ({miss.detail.rstrip('. ')})"
    return clause(miss, feature="servers", cannot="get this printer's record", then="set it up again")


def forget() -> None:
    """Drop every remembered answer and miss (tests, and a deliberate reload).
    Records kept on disk stay."""
    global _service_down_until, _service_down_miss
    _answers.clear()
    _last_miss.clear()
    _retry_after.clear()
    _service_down_until, _service_down_miss = 0.0, None


__all__ = [
    "BLOCKS",
    "FRESH_FOR_S",
    "KEEP_DIR",
    "MISS_RETRY_S",
    "SERVICE_BACKOFF_S",
    "WIRE_TOOL",
    "available",
    "forget",
    "kept_record",
    "last_miss",
    "printer_record",
    "unanswered",
    "why_unanswered",
]
