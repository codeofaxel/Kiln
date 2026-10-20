"""Public-Kiln → kiln-pro bridge for ONE material's catalogue record.

The verified brand filament profiles (a brand's nozzle and bed ranges and
optimum, enclosure, hardened nozzle, AMS) are kiln-pro's
(https://kiln3d.com), part of Kiln Pro: one record per material, served one
material at a time.  Below Pro the servers refuse, the loaders fall to the
material's own range and the warning says so.  Public Kiln keeps the FLOOR --
the material's own temperatures and ranges, warping, the environment words,
the design limits, the hazards -- and the ROSTER of brand profiles (which
exist, what they are called, which family they belong to).  Its loaders in
:mod:`kiln.design_intelligence` ask here for a family's figures and merge
them onto the roster rows, so every door downstream resolves exactly as it
did when the figures were bundled.

A record carries the brand figures only.  How much weight a part can carry
is answered one part at a time (:mod:`kiln._pro_load_bridge`), so no load
figure of any material comes here or is kept here; a record kept before that
change is read without its load block, and rewritten without it.

A record comes from one of four places, tried in order:

1. **kiln-pro importable** (and the hosted server, which is one):
   :func:`kiln_pro.material_catalogue.material_record` answers on this
   computer.  Nothing is kept on disk: kiln-pro is the disk.
2. **kept on this computer**, fresh: the answer the servers gave the last
   time this family was asked about, in
   ``~/.kiln/material_catalogue/<id>.json``, read for a week before it is
   asked about again.
3. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked through
   the door every served tool uses (:func:`kiln.server._pro_api_call`), and
   the answer is kept (2).
4. **kept, however old**: on a miss that is not a refusal (offline, signed
   out, the service silent) the record this computer was last told is still
   the record.  A REFUSAL (the plan no longer covers it) drops the kept
   record: a lapsed subscription does not keep the figures.

No record is a miss, worded by :func:`why_unanswered` from the four served
causes (:mod:`kiln.served_answer`), and the loaders fall back to the floor:
the material's own range, never a brand's.  A material the catalogue has no
row for is a SETTLED answer (``known: false``), remembered for the process
and never kept on disk, so a row added later is found.

One record per call, never a roster.  Nothing here raises.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from kiln.served_answer import LOCAL_FAULT_CODE, Miss, classify_answer, classify_transport_error, clause, local_fault

logger = logging.getLogger(__name__)

#: The served tool that answers for one material.
WIRE_TOOL = "get_material_record"
#: The block a record carries: the brand profiles' figures.  A kept record
#: from before a block existed fails :func:`_is_record` and is asked for
#: again, never read half; a block this list does not name is dropped
#: (:func:`_only_blocks`), from what is answered and from what is kept.
BLOCKS = ("brands",)
#: Where the same answer is read from when kiln-pro IS installed here
#: (imported by name in :func:`_local`, never by a string, so a static
#: reader of the call graph sees the crossing).
LOCAL_MODULE = "kiln_pro.material_catalogue"
#: A material question is a person waiting; a door on the way to a print
#: must not wait longer than this for figures it can do without.
_CONSULT_TIMEOUT_S: float = 4.0
#: After the service fails to answer, how long it is left alone.
SERVICE_BACKOFF_S: float = 300.0
#: How long a kept record is read without asking again.  The catalogue is
#: corrected by a deploy, not by the hour.
FRESH_FOR_S: float = 7 * 24 * 3600.0
#: Where a served record is kept on an install without kiln-pro.
KEEP_DIR = Path.home() / ".kiln" / "material_catalogue"

_service_down_until: float = 0.0
_service_down_miss: Miss | None = None
#: spelling (as asked) -> the record, or ``None`` for a settled "no row".
_answers: dict[str, dict[str, Any] | None] = {}
#: spelling -> why the last ask for it had no answer; cleared by an answer.
_last_miss: dict[str, Miss] = {}
#: spelling -> when it may be asked about again after a miss that was not
#: the service's (signed out, refused).  Short, so a sign-in is noticed.
_retry_after: dict[str, float] = {}
MISS_RETRY_S: float = 15.0
#: A miss with nothing asked of Kiln's servers: kiln-pro on this computer failed.
_NOT_ASKED = LOCAL_FAULT_CODE


def available() -> bool:
    """True when kiln-pro is installed here WITH its catalogue file.

    The reader alone is not enough: kiln-pro's wheel leaves the file out,
    so a pip-installed kiln-pro has the reader and nothing to read, and
    trusting it would hand every material the floor and never ask the
    servers.  The Fly deploy and a repo checkout have the file.
    """
    try:
        from kiln_pro.material_catalogue import catalogue_present

        return bool(catalogue_present())
    except Exception:  # noqa: BLE001 -- an older kiln-pro, or none: the served door
        return False


def _key(material_id: Any) -> str:
    return str(material_id or "").strip().lower() if isinstance(material_id, str) else ""


def _is_record(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("material_id"), str)
        and bool(value["material_id"])
        and all(isinstance(value.get(block), dict) for block in BLOCKS)
    )


def material_record(material_id: Any) -> dict[str, Any] | None:
    """ONE material's record for *material_id*, or ``None``.

    ``{"material_id", "matched_by", "display_name", "brands"}`` --
    ``material_id`` is the catalogue's own id, which may differ from the
    spelling asked (``pc`` is answered as ``polycarbonate``; a brand profile
    id is answered as its family).  ``None`` is one of two things, told
    apart by :func:`why_unanswered`: the catalogue has no row (``""``), or
    nothing answered (a served cause).  Never raises.
    """
    key = _key(material_id)
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
    if miss.cause == "refused":
        # The servers answered and said no: the plan does not cover the
        # record.  A kept copy is not this install's to read any more.
        _forget_kept(key)
        return None
    if kept is not None:
        return kept[0]
    return None


#: The hosted boundary's own exception for a read below the record's tier
#: (kiln-pro's ``HostedIntelligencePolicyError``), known by name and wording
#: because public Kiln cannot import the type.
_TIER_REFUSAL = "not entitled"
_PRO_REQUIRED = "Kiln Pro has the brand's own tune"


def _local(key: str) -> tuple[dict[str, Any] | None, Miss | None]:
    try:
        from kiln_pro.material_catalogue import material_record as local_record

        record = local_record(key)
    except Exception as exc:  # noqa: BLE001 -- a loader never fails for want of it
        if type(exc).__name__ == "HostedIntelligencePolicyError" and _TIER_REFUSAL in str(exc):
            # On the hosted server the caller's plan is below the record's:
            # a refusal, worded as one, never "kiln-pro stopped".
            return None, Miss("refused", "TIER_REQUIRED", _PRO_REQUIRED)
        return None, local_fault(exc)
    return (_only_blocks(record) if _is_record(record) else None), None


def _only_blocks(record: dict[str, Any]) -> dict[str, Any]:
    """*record* with nothing but the identity fields and :data:`BLOCKS`."""
    return {
        "material_id": record["material_id"],
        "matched_by": str(record.get("matched_by") or ""),
        "display_name": str(record.get("display_name") or record["material_id"]),
        **{block: dict(record[block]) for block in BLOCKS},
    }


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
        answer = _pro_api_call(WIRE_TOOL, _timeout=_CONSULT_TIMEOUT_S, _asked_by_user=False, material=key)
    except Exception as exc:  # noqa: BLE001 -- the network is a degrade, never a refusal to go on
        logger.debug("material record not served", exc_info=True)
        miss = classify_transport_error(exc)
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
        return None, miss
    if isinstance(answer, dict) and answer.get("success"):
        if answer.get("known") is False:
            return None, None
        if _is_record(answer):
            return _only_blocks(answer), None
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
        logger.debug("kept material record unreadable", exc_info=True)
        return None
    record = raw.get("record") if isinstance(raw, dict) else None
    if not _is_record(record):
        return None
    try:
        kept_at = float(raw.get("kept_at") or 0.0)
    except (TypeError, ValueError):
        kept_at = 0.0
    trimmed = _only_blocks(record)
    if trimmed != record:
        # Kept before the load figures stopped leaving Kiln's servers: the
        # extra block is not this install's to keep, so the file is rewritten
        # without it (same age, so it is asked for again when it would have been).
        _keep(key, trimmed, kept_at=kept_at)
    return trimmed, max(0.0, time.time() - kept_at)


def _keep(key: str, record: dict[str, Any], *, kept_at: float | None = None) -> None:
    try:
        KEEP_DIR.mkdir(parents=True, exist_ok=True)
        path = _keep_path(key)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"kept_at": time.time() if kept_at is None else kept_at, "record": _only_blocks(record)},
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 -- not kept costs one more ask later
        logger.debug("material record not kept", exc_info=True)


def _forget_kept(key: str) -> None:
    try:
        _keep_path(key).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 -- a copy that would not delete is read again next ask and refused again
        logger.debug("kept material record not removed", exc_info=True)


def kept_record(material_id: Any) -> dict[str, Any] | None:
    """The record this computer was last told for *material_id*, however
    old, or ``None``.  Never asks anything."""
    kept = _kept(_key(material_id))
    return kept[0] if kept else None


# ---------------------------------------------------------------------------
# Misses
# ---------------------------------------------------------------------------


def last_miss(material_id: Any) -> Miss | None:
    """Why the last ask for *material_id* had no answer, or ``None``."""
    return _last_miss.get(_key(material_id))


def why_unanswered(material_id: Any) -> str:
    """Which served cause left the last ask for *material_id* without an
    answer: ``offline`` / ``signed_out`` / ``unanswered`` / ``refused``, or
    ``""`` when it was answered (a record, or a settled "no row")."""
    miss = _last_miss.get(_key(material_id))
    return str(getattr(miss, "cause", "") or "") if miss is not None else ""


def unanswered(material_id: Any) -> str | None:
    """Why this material's figures could not be had, as a clause, or ``None``.

    Worded to follow the floor's own reason in a door's note.
    """
    miss = _last_miss.get(_key(material_id))
    if miss is None:
        return None
    if miss.code == _NOT_ASKED:
        return f"Kiln can't read this material's record ({miss.detail.rstrip('. ')})"
    if miss.cause == "refused" and miss.code == "TIER_REQUIRED":
        # A plan answer, not a transient one: said plainly, without the
        # pricing paragraph the servers attach to a refusal.
        return "the brand's own tune is part of Kiln Pro"
    if miss.cause == "signed_out":
        return clause(miss, feature="servers", cannot="get this material's figures", then="sign in to a Kiln Pro account and ask again")
    return clause(miss, feature="servers", cannot="get this material's figures", then="ask again")


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
    "material_record",
    "unanswered",
    "why_unanswered",
]
