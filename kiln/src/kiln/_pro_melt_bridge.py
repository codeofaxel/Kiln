"""Public-Kiln → kiln-pro bridge for a printer's own melt rates.

Every slice sets the most plastic per second it may ask for each material
(:mod:`kiln.slicer_material`).  Public Kiln's own figure is the cautious one:
the lowest any slicer maker gives the material, which holds on every printer
and slows a fast one.  The figure for one printer and nozzle -- what that
printer's own slicer presets give -- is kiln-pro's (https://kiln3d.com), and
comes from one of two places, tried in order:

1. **kiln-pro importable**:
   :func:`kiln_pro.device_intelligence.melt_rate_intelligence.melt_rate`
   answers on this computer.
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked through
   the door every served tool uses (:func:`kiln.server._pro_api_call`).

An answer is one material's figure for one printer and nozzle -- one cell at a
time, never a printer's whole record.  A served answer is kept for the life of
the process (the table changes only when Kiln is deployed), so a session asks
once per printer, nozzle and material.  No answer is a miss, worded by
:func:`unanswered`, and the slice keeps the cautious figure: slower than the
printer could go, never faster.  Nothing here raises.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error, clause

logger = logging.getLogger(__name__)

#: The served tool that answers for one printer, nozzle and material.
WIRE_TOOL = "get_printer_melt_rate"
#: A slice waits this long, at most, for the figure; the cautious one is
#: always there to fall back on.
_CONSULT_TIMEOUT_S: float = 4.0
#: After the service fails to answer, how long it is left alone.  Same backoff
#: the nozzle and cutter bridges use.
SERVICE_BACKOFF_S: float = 300.0
_service_down_until: float = 0.0
_service_down_miss: Miss | None = None
#: (printer, nozzle, material) -> the cell as answered: (mm³/s or None, basis).
_answers: dict[tuple[str, str, str], tuple[float | None, str]] = {}
#: (printer, nozzle) -> why the last ask for it had no answer; cleared by an answer.
_last_miss: dict[tuple[str, str], Miss] = {}
#: A miss with nothing asked of Kiln's servers: kiln-pro on this computer failed.
_NOT_ASKED = "NOT_ASKED"


def available() -> bool:
    """True when kiln-pro is installed with its melt-rate table reader."""
    try:
        import kiln_pro.device_intelligence.melt_rate_intelligence  # noqa: F401

        return True
    except ImportError:
        return False


def _key(printer_id: str, nozzle_mm: float) -> tuple[str, str]:
    return printer_id, f"{float(nozzle_mm):g}"


def printer_melt_rate(printer_id: str, nozzle_mm: float, material_id: str) -> tuple[float, str] | None:
    """``(mm³/s, basis)`` for *material_id* on *printer_id* at *nozzle_mm*, or ``None``.

    *basis* is ``"maker"`` (the printer maker's own slicer) or
    ``"slicer_presets"`` (other slicers' presets for the printer).  ``None``
    when the table has no figure, or when no answer came (:func:`unanswered`).
    """
    key = _key(printer_id, nozzle_mm)
    cell_key = (*key, material_id)
    # Only a SERVED answer is kept: it belongs to the one person this
    # computer serves.  In a process that holds kiln-pro -- the hosted server
    # among them -- every ask reads afresh (the raw file is cached below it),
    # so one caller's answer is never handed to the next.
    local = available()
    if not local and cell_key in _answers:
        cell = _answers[cell_key]
    else:
        cell, miss = (_local if local else _served)(printer_id, float(nozzle_mm), material_id)
        if miss is not None:
            _last_miss[key] = miss
            return None
        _last_miss.pop(key, None)
        if not local:
            _answers[cell_key] = cell
    mm3s, basis = cell
    return (mm3s, basis) if mm3s else None


def _cell(answer: dict[str, Any]) -> tuple[float | None, str]:
    mm3s = answer.get("mm3s")
    return (float(mm3s) if mm3s else None), str(answer.get("basis") or "")


def _local(printer_id: str, nozzle_mm: float, material_id: str) -> tuple[tuple[float | None, str], Miss | None]:
    try:
        from kiln_pro.device_intelligence.melt_rate_intelligence import melt_rate

        return _cell(melt_rate(printer_id, material_id, nozzle_mm)), None
    except Exception as exc:  # noqa: BLE001 -- a slice never fails for want of it
        logger.debug("melt rates on this computer failed", exc_info=True)
        return (None, ""), Miss("unanswered", _NOT_ASKED, f"kiln-pro on this computer stopped: {exc}"[:400])


def _served(printer_id: str, nozzle_mm: float, material_id: str) -> tuple[tuple[float | None, str], Miss | None]:
    global _service_down_until, _service_down_miss
    none: tuple[float | None, str] = (None, "")
    if time.monotonic() < _service_down_until:
        return none, _service_down_miss or Miss("unanswered", "SERVER_UNREACHABLE")
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return none, Miss("unanswered", _NOT_ASKED, "this install could not open its connection to Kiln's servers")
    try:
        answer = _pro_api_call(
            WIRE_TOOL, _timeout=_CONSULT_TIMEOUT_S, _asked_by_user=False,
            printer_model=printer_id, nozzle_mm=nozzle_mm, material=material_id,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a degrade, never a slice failure
        logger.debug("melt rate not served", exc_info=True)
        miss = classify_transport_error(exc)
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
        return none, miss
    if isinstance(answer, dict) and answer.get("success"):
        return _cell(answer), None
    miss = classify_answer(answer) or Miss("unanswered", detail="an answer that did not say it succeeded")
    if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
    return none, miss


def unanswered(printer_id: str, nozzle_mm: float) -> str | None:
    """Why this printer's own figures could not be had, as a clause, or ``None``.

    Worded to follow the cautious figure's own reason in a slice's note.
    """
    miss = _last_miss.get(_key(printer_id, nozzle_mm))
    if miss is None:
        return None
    if miss.code == _NOT_ASKED:
        return f"Kiln can't read this printer's own figures ({miss.detail.rstrip('. ')})"
    return clause(miss, feature="servers", cannot="get this printer's own figures", then="slice again")


def forget() -> None:
    """Drop every remembered answer and miss (tests, and a deliberate reload)."""
    global _service_down_until, _service_down_miss
    _answers.clear()
    _last_miss.clear()
    _service_down_until, _service_down_miss = 0.0, None
