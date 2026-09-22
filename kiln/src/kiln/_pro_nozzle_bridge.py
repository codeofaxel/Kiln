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
    return compute_print_capacity_for_nozzle(
        state=state,
        planned_grams=float(planned_grams or 0),
        baseline=baseline,
    )


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
        answer = _pro_api_call(WIRE_TOOL, _timeout=_CONSULT_TIMEOUT_S, **kwargs)
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
    "SERVICE_BACKOFF_S",
    "WIRE_TOOL",
    "available",
    "consult_capacity",
    "nozzle_unchecked",
    "consult_abrasive_escalation",
    "consult_nozzle_summary",
    "record_print_odometer",
]
