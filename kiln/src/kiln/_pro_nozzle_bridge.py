"""Public-Kiln → kiln-pro nozzle-intelligence bridge.

Free-tier users see this file with no kiln-pro installed — the
helpers below return ``None`` cleanly so consumers can branch
without try/except scaffolding scattered across every call site.

kiln-pro tier-gating ("pro+ feature") happens INSIDE the kiln-pro
verdict functions; this bridge just provides the import + the
silent-degrade contract.  When a Pro+ verdict fires, the response
shape is documented in the kiln-pro module's docstring.  Free
tier sees ``None`` here and falls through to the existing
material-only / population-baseline logic.

Why a bridge file rather than try/except at each call site:

- Single import path means consumers branch on one boolean
  (`bridge.available`), not 5 different exception patterns.
- One place to extend when new nozzle-aware verdicts ship.
- Easier to test — one mock target.
- The kiln-pro discovery rule ("Kiln depends on kiln-pro never
  the reverse" — see ``CLAUDE.md``) is respected because every
  call here is ``try: import``'d and silently degrades.

Used by:
- ``preflight_check`` (kiln/src/kiln/server.py) — surfaces nozzle
  capacity check before slicing.
- ``recommend_settings`` (kiln/src/kiln/plugins/learning_tools.py)
  — warns when active nozzle is incompatible with the recommended
  material.
- ``recommend_material`` (kiln/src/kiln/material_routing.py) —
  warns when a recommended material would hit the abrasive
  threshold against the active nozzle.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: The hosted door for the pre-print capacity verdict, asked when kiln-pro
#: is not installed here -- the same way the blade consult asks for its
#: status.  A print start must not wait on a slow network for it.
WIRE_TOOL = "check_nozzle_capacity_for_print"
_CONSULT_TIMEOUT_S: float = 4.0
#: After the service fails to answer, how long it is left alone.  Same
#: backoff the cutter bridge uses.
SERVICE_BACKOFF_S: float = 300.0
_service_down_until: float = 0.0
_service_down_miss: Any = None
#: Why the last capacity consult for each machine had no verdict (a
#: :class:`kiln.served_answer.Miss`), cleared by an answer.  A pre-flight
#: and a start read it to say what was not checked.
_last_miss: dict[str, Any] = {}
#: The hosted door that holds Kiln's record of a printer's nozzle, asked for
#: the pre-flight's record comparison when kiln-pro is not installed here.
RECORD_TOOL = "get_nozzle_state"
#: How long a SERVED record lookup is remembered, answered or not.  A design
#: check is asked many times in a row and each ask is a network call; the
#: tools that write the record clear this (:func:`forget_recorded_nozzle`).
#: An install that asks the hosted door is one person's, so the memory is
#: theirs alone.
RECORD_MEMO_S: float = 120.0
_record_memo: dict[str, tuple[float, dict[str, Any]]] = {}
#: Why the last record comparison for each machine had no answer, the same way.
_last_record_miss: dict[str, Any] = {}


def available() -> bool:
    """True when kiln-pro is installed AND the nozzle module loaded."""
    try:
        import kiln_pro.nozzle_intelligence  # noqa: F401
        return True
    except ImportError:
        return False


def consult_capacity(
    printer_id: str,
    planned_grams: float,
    filament_material: str = "",
    printer_model: str = "",
) -> dict[str, Any] | None:
    """Run the pre-print nozzle-capacity verdict for the active printer.

    The verdict dict (``status``, ``narrative``, ``percent_used``, ...):
    from kiln-pro locally when it is installed, else from the hosted
    door, the same one the blade consult uses, with a short timeout.
    ``None`` when nothing answered -- and then :func:`nozzle_unchecked`
    says why, so a pre-flight or a start can say what it could not
    check instead of going quiet.  Caller decides whether to surface
    the verdict's narrative + status alongside the existing signals.
    """
    if not printer_id or not isinstance(printer_id, str):
        return None
    try:
        from kiln_pro.data_overlays import load_overlay
        from kiln_pro.nozzle_intelligence.capacity import (
            compute_print_capacity_for_nozzle,
            resolve_capacity_baseline,
        )
        from kiln_pro.nozzle_intelligence.store_resolver import (
            resolve_backend,
            resolve_state_or_factory_default,
        )
    except ImportError:
        return _served_capacity(printer_id, planned_grams, filament_material, printer_model)
    _last_miss.pop(printer_id, None)

    backend, _nudge = resolve_backend(tool_name="preflight_capacity")
    if backend is None:
        return None
    state = resolve_state_or_factory_default(
        backend, printer_id,
        printer_model=printer_model or None,
    )
    if state is None:
        return None
    try:
        overlay = load_overlay("nozzle_wear_thresholds")
    except Exception:  # noqa: BLE001
        overlay = None
    filament = (filament_material or "PLA").strip()
    baseline = resolve_capacity_baseline(
        filament_material=filament,
        nozzle_material=state.material.value,
        overlay=overlay,
    )
    verdict = compute_print_capacity_for_nozzle(
        state=state,
        planned_grams=float(planned_grams or 0),
        baseline=baseline,
    )
    if isinstance(verdict, dict):
        # The same identity the hosted verdict carries, so a milestone is
        # remembered per NOZZLE wherever the verdict came from.
        verdict.setdefault("nozzle_material", state.material.value)
        verdict.setdefault("nozzle_grams_through_before", state.grams_through)
    return verdict


def _declared_model(printer_id: str) -> str | None:
    try:
        from kiln.printer_model_resolver import resolve_printer_model_for

        return (resolve_printer_model_for(printer_id) or "").strip().lower() or None
    except Exception:  # noqa: BLE001
        return None


def _served_capacity(
    printer_id: str, planned_grams: float, filament_material: str, printer_model: str,
) -> dict[str, Any] | None:
    """The hosted capacity verdict, or ``None`` with why in :data:`_last_miss`."""
    global _service_down_until, _service_down_miss
    from kiln.served_answer import Miss, classify_answer, classify_transport_error

    if time.monotonic() < _service_down_until:
        if _service_down_miss is not None:
            _last_miss[printer_id] = _service_down_miss
        return None
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        _last_miss[printer_id] = Miss("unanswered", detail="the served door could not be opened on this install")
        return None
    kwargs: dict[str, Any] = {
        "printer_id": printer_id,
        "planned_grams": float(planned_grams or 0),
        "filament_material": (filament_material or "").strip(),
    }
    model = (printer_model or "").strip() or _declared_model(printer_id)
    if model:
        kwargs["printer_model"] = model
    try:
        answer = _pro_api_call(WIRE_TOOL, _timeout=_CONSULT_TIMEOUT_S, _asked_by_user=False, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- the network is a degrade, never a print
        logger.debug("nozzle capacity not served", exc_info=True)
        miss = classify_transport_error(exc)
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
        _last_miss[printer_id] = miss
        return None
    if isinstance(answer, dict) and answer.get("success") and answer.get("status"):
        _last_miss.pop(printer_id, None)
        return answer
    miss = classify_answer(answer) or Miss("unanswered", detail="an answer with no verdict in it")
    if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
    _last_miss[printer_id] = miss
    return None


def nozzle_unchecked(printer_id: str, *, at: str = "preflight") -> dict[str, Any] | None:
    """Why the last capacity consult for *printer_id* could not be made, as
    the line a pre-flight (or a start) carries, or ``None`` when it was
    answered.

    ``{"word": "unchecked", "line", "why", "why_code", "why_detail"}``.  A
    nozzle whose life could not be checked is named as such, so "not
    checked" never reads the same as "fine".  With kiln-pro installed the
    consult never misses, and this stays ``None``.
    """
    if not printer_id or not isinstance(printer_id, str):
        return None
    miss = _last_miss.get(printer_id)
    if miss is None:
        return None
    from kiln.served_answer import fields, sentence

    if at == "start":
        line = sentence(
            miss, feature="servers",
            on_the_line="Before a print starts, Kiln checks the nozzle has the life left for it",
            cannot=f"check {printer_id}'s nozzle", wont="started this one without that check",
            then="have it checked before the next print",
        )
    else:
        line = sentence(
            miss, feature="servers",
            on_the_line="This pre-flight says whether the nozzle has the life left for this print",
            cannot=f"check {printer_id}'s nozzle", wont="says nothing about it",
            safe_remedy="Print as usual", then="run the pre-flight again",
        )
    return {"word": "unchecked", "line": line, **fields(miss)}


def consult_recorded_nozzle(printer_id: str) -> dict[str, Any]:
    """The nozzle size on record for *printer_id*, when somebody recorded one.

    ``{"diameter_mm": float | None, "answered": bool}``.  ``diameter_mm`` is
    set only for a record a person or the machine stated; a catalogue
    default is nobody's record and comes back ``None``.  ``answered`` is
    ``False`` when the record could not be asked at all (offline, signed
    out, no answer), so a caller can tell "no record" from "could not ask".
    From kiln-pro locally when it is installed, asked every time; else
    from the hosted door with a short timeout, and only that answer is
    remembered, for :data:`RECORD_MEMO_S`.
    """
    pid = (printer_id or "").strip() if isinstance(printer_id, str) else ""
    if not pid:
        return {"diameter_mm": None, "answered": True}
    if available():
        # Asked of the store each time, never remembered: the read is local,
        # the record can change between two checks, and a process that
        # answers for more than one account must not hand one caller's
        # nozzle to the next.
        summary = consult_nozzle_summary(pid)
        found = summary is not None and summary.get("trusted_for_verdicts")
        return {"diameter_mm": summary.get("diameter_mm") if found else None, "answered": True}
    now = time.monotonic()
    memo = _record_memo.get(pid)
    if memo is not None and now - memo[0] < RECORD_MEMO_S:
        return memo[1]
    answer = _served_recorded_nozzle(pid)
    _record_memo[pid] = (now, answer)
    return answer


def _served_recorded_nozzle(printer_id: str) -> dict[str, Any]:
    global _service_down_until, _service_down_miss
    from kiln.served_answer import classify_transport_error

    unanswered = {"diameter_mm": None, "answered": False}
    if time.monotonic() < _service_down_until:
        return unanswered
    try:
        from kiln.server import _pro_api_call

        answer = _pro_api_call(RECORD_TOOL, _timeout=_CONSULT_TIMEOUT_S, printer_id=printer_id)
    except Exception as exc:  # noqa: BLE001 -- the network is a degrade, never a check
        logger.debug("nozzle record not served", exc_info=True)
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = classify_transport_error(exc)
        return unanswered
    if not (isinstance(answer, dict) and answer.get("success")):
        return unanswered
    stated = answer.get("found") and answer.get("trusted_for_verdicts")
    return {"diameter_mm": answer.get("diameter_mm") if stated else None, "answered": True}


#: The memo key for :func:`consult_only_recorded_nozzle`; no printer is named so.
_ONLY_RECORD_KEY = "*"


def consult_only_recorded_nozzle() -> dict[str, Any]:
    """The one nozzle on record for this account, when there is exactly one.

    ``{"printer_id": str | None, "diameter_mm": float | None, "answered": bool}``.
    For a check nobody told which printer a part is for.  ``printer_id`` is
    set only when exactly one recorded nozzle exists; none or several is no
    answer, never a pick.  Asked and remembered the way
    :func:`consult_recorded_nozzle` is.
    """
    nothing = {"printer_id": None, "diameter_mm": None, "answered": True}
    if available():
        try:
            from kiln_pro.nozzle_intelligence.store_resolver import only_recorded_nozzle
        except ImportError:  # a kiln-pro from before this door existed
            return nothing
        try:
            state = only_recorded_nozzle(tool_name="list_nozzle_states")
        except Exception:  # noqa: BLE001 -- one rung of a check, never the check
            logger.debug("only recorded nozzle lookup failed", exc_info=True)
            return nothing
        if state is None:
            return nothing
        return {"printer_id": state.printer_id, "diameter_mm": state.diameter_mm, "answered": True}
    now = time.monotonic()
    memo = _record_memo.get(_ONLY_RECORD_KEY)
    if memo is not None and now - memo[0] < RECORD_MEMO_S:
        return memo[1]
    answer = _served_only_recorded_nozzle()
    _record_memo[_ONLY_RECORD_KEY] = (now, answer)
    return answer


def _served_only_recorded_nozzle() -> dict[str, Any]:
    global _service_down_until, _service_down_miss
    from kiln.served_answer import classify_transport_error

    unanswered = {"printer_id": None, "diameter_mm": None, "answered": False}
    if time.monotonic() < _service_down_until:
        return unanswered
    try:
        from kiln.server import _pro_api_call

        answer = _pro_api_call("list_nozzle_states", _timeout=_CONSULT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 -- the network is a degrade, never a check
        logger.debug("nozzle records not served", exc_info=True)
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = classify_transport_error(exc)
        return unanswered
    if not (isinstance(answer, dict) and answer.get("success")):
        return unanswered
    stated = [
        s for s in (answer.get("states") or [])
        if isinstance(s, dict) and s.get("trusted_for_verdicts") and s.get("printer_id")
    ]
    if len(stated) != 1:
        return {"printer_id": None, "diameter_mm": None, "answered": True}
    return {"printer_id": stated[0]["printer_id"], "diameter_mm": stated[0].get("diameter_mm"), "answered": True}


def forget_recorded_nozzle() -> None:
    """Drop every remembered record lookup: the record was just written."""
    _record_memo.clear()


def consult_sliced_file(printer_id: str, file_nozzle_mm: float) -> dict[str, Any] | None:
    """How the nozzle a file was sliced for sits against the nozzle Kiln has
    on record for *printer_id*.

    The answer is kiln-pro's (https://kiln3d.com), free at every tier: a
    ``verdict``, the sizes it compared, and a ``summary`` to show the
    person.  Asked locally when kiln-pro is installed, else of the hosted
    door, with this machine's own nozzle reading sent along and a short
    timeout.  ``None`` when nothing answered -- and then
    :func:`sliced_file_unchecked` says why, so a pre-flight can say what it
    could not check.  The local comparison of the file with the printer's
    own setting (:mod:`kiln.nozzle_size_check`) does not depend on this.
    """
    if not printer_id or not isinstance(printer_id, str) or not file_nozzle_mm:
        return None
    try:
        from kiln_pro.nozzle_intelligence.printer_reading import sliced_file_for
    except ImportError:
        return _served_sliced_file(printer_id, float(file_nozzle_mm))
    _last_record_miss.pop(printer_id, None)
    try:
        return sliced_file_for(printer_id, float(file_nozzle_mm))
    except Exception:  # noqa: BLE001 -- a comparison beside the check, never the check
        logger.debug("nozzle record comparison failed", exc_info=True)
        return None


def _served_sliced_file(printer_id: str, file_nozzle_mm: float) -> dict[str, Any] | None:
    """The hosted record comparison, or ``None`` with why in :data:`_last_record_miss`."""
    global _service_down_until, _service_down_miss
    from kiln.served_answer import Miss, classify_answer, classify_transport_error

    if time.monotonic() < _service_down_until:
        if _service_down_miss is not None:
            _last_record_miss[printer_id] = _service_down_miss
        return None
    try:
        from kiln.printer_nozzle_reading import with_local_reading
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        _last_record_miss[printer_id] = Miss("unanswered", detail="the served door could not be opened on this install")
        return None
    kwargs = with_local_reading(RECORD_TOOL, {"printer_id": printer_id, "file_nozzle_mm": file_nozzle_mm})
    try:
        answer = _pro_api_call(RECORD_TOOL, _timeout=_CONSULT_TIMEOUT_S, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- the network is a degrade, never a print
        logger.debug("nozzle record comparison not served", exc_info=True)
        miss = classify_transport_error(exc)
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
        _last_record_miss[printer_id] = miss
        return None
    block = answer.get("sliced_file") if isinstance(answer, dict) and answer.get("success") else None
    if isinstance(block, dict) and block.get("verdict"):
        _last_record_miss.pop(printer_id, None)
        return block
    miss = classify_answer(answer) or Miss("unanswered", detail="an answer with no comparison in it")
    if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
    _last_record_miss[printer_id] = miss
    return None


def sliced_file_unchecked(printer_id: str) -> dict[str, Any] | None:
    """Why the last record comparison for *printer_id* could not be made, as
    the line a pre-flight carries, or ``None`` when it was answered.

    ``{"word": "unchecked", "line", "why", "why_code", "why_detail"}``.
    With kiln-pro installed the consult never misses, and this stays ``None``.
    """
    if not printer_id or not isinstance(printer_id, str):
        return None
    miss = _last_record_miss.get(printer_id)
    if miss is None:
        return None
    from kiln.served_answer import fields, sentence

    line = sentence(
        miss, feature="servers",
        on_the_line="This pre-flight says whether the file was sliced for the nozzle Kiln has on record",
        cannot=f"compare the file with {printer_id}'s nozzle record", wont="says nothing about the record",
        safe_remedy="The check against the printer's own setting above still stands", then="run the pre-flight again",
    )
    return {"word": "unchecked", "line": line, **fields(miss)}


def consult_clumping_detection(
    *,
    printer_model: str | None,
    reading: dict[str, Any] | None,
    file: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """What a nozzle-clumping-detection switch reading MEANS on this model.

    Returns the block from
    ``kiln_pro.device_intelligence.detection_intelligence.clumping_switch_block``
    -- the composed statement, ``warnings`` (a part placed in the probe's
    detection area, a slicing mode the probe does not run in, a file with
    no tower behind an ON switch), the detection area and the defeating
    modes -- or ``None`` without kiln-pro.  *reading* is the
    ``nozzle_clumping_detection`` status block public Kiln reads off the
    machine; *file* is :func:`kiln.nozzle_clumping_detection.file_facts`.
    """
    try:
        from kiln_pro.device_intelligence.detection_intelligence import (
            clumping_switch_block,
        )
    except ImportError:
        return None
    facts = file or {}
    try:
        return clumping_switch_block(
            printer_model,
            reading,
            footprint=facts.get("footprint"),
            print_mode=facts.get("print_mode"),
            prime_tower_in_file=facts.get("prime_tower_in_file"),
        )
    except Exception:  # noqa: BLE001 -- a sentence beside the check, never the check
        return None


def consult_abrasive_escalation(
    filament_material: str,
    printer_id: str,
) -> dict[str, Any] | None:
    """Run the abrasive-escalation verdict for (filament, active nozzle).

    Returns the verdict from
    ``kiln_pro.nozzle_intelligence.verdicts.abrasive_escalation``
    when kiln-pro is present + a nozzle state exists.  Returns
    ``None`` otherwise.

    The caller uses this to warn when a recommended material would
    hit the abrasive ceiling against the active nozzle (e.g.
    "Polymaker PolyTerra-CF on brass — expect ~360 g lifetime").
    """
    if not filament_material or not printer_id:
        return None
    try:
        from kiln_pro.nozzle_intelligence.store_resolver import (
            resolve_backend,
            resolve_state_or_factory_default,
        )
        from kiln_pro.nozzle_intelligence.verdicts import (
            abrasive_escalation,
        )
    except ImportError:
        return None

    backend, _nudge = resolve_backend(tool_name="recommend_material_abrasive")
    if backend is None:
        return None
    state = resolve_state_or_factory_default(backend, printer_id)
    if state is None:
        return None
    return abrasive_escalation(
        material=filament_material,
        nozzle=state,
    )


def consult_nozzle_summary(printer_id: str) -> dict[str, Any] | None:
    """Return a compact nozzle summary for the active printer.

    Used by ``recommend_settings`` to surface "your printer's
    current nozzle is brass — settings tuned accordingly" /
    "...nozzle wear ~70% — settings include a softer first-layer
    bias to compensate."

    Returns ``{material, diameter_mm, provenance, grams_through,
    trusted_for_verdicts}`` or ``None`` when the lookup fails.
    """
    if not printer_id:
        return None
    try:
        from kiln_pro.nozzle_intelligence.store_resolver import (
            resolve_backend,
            resolve_state_or_factory_default,
        )
    except ImportError:
        return None

    backend, _nudge = resolve_backend(tool_name="recommend_settings_nozzle")
    if backend is None:
        return None
    state = resolve_state_or_factory_default(backend, printer_id)
    if state is None:
        return None
    return {
        "material": state.material.value,
        "diameter_mm": state.diameter_mm,
        "provenance": state.provenance.value,
        "grams_through": state.grams_through,
        "trusted_for_verdicts": state.trusted_for_verdicts(),
    }


def record_print_odometer(
    printer_id: str,
    file_name: str | None,
    *,
    grams: float | None = None,
) -> dict[str, Any] | None:
    """Advance the pro nozzle odometer for one started print.

    Called from ``PrinterAdapter.start_print`` — the chokepoint every
    print passes through, success OR failure — so nozzle wear counts
    for every print, not just the ones something watched to completion.
    A cancelled print over-counts its planned filament; that is the
    safe direction (a nozzle retired early beats a worn nozzle read as
    fresh).  No-op without kiln-pro.
    """
    try:
        from kiln_pro.nozzle_intelligence.odometer import record_print_filament
    except ImportError:
        return None
    try:
        return record_print_filament(
            printer_id,
            file_path=file_name,
            grams=grams,
            dedupe_key=file_name,
        )
    except Exception:  # noqa: BLE001 — wear bookkeeping never blocks a print
        return None


__all__ = [
    "RECORD_MEMO_S",
    "RECORD_TOOL",
    "SERVICE_BACKOFF_S",
    "WIRE_TOOL",
    "available",
    "consult_capacity",
    "consult_only_recorded_nozzle",
    "consult_recorded_nozzle",
    "consult_sliced_file",
    "forget_recorded_nozzle",
    "nozzle_unchecked",
    "sliced_file_unchecked",
    "consult_abrasive_escalation",
    "consult_nozzle_summary",
    "record_print_odometer",
]
