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

from typing import Any


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

    Returns the verdict dict from
    ``kiln_pro.plugins.nozzle_tools.check_nozzle_capacity_for_print``
    when kiln-pro is present, else ``None``.  Caller decides whether
    to surface the verdict's narrative + status alongside the
    existing preflight signals.
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
        return None

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
    "available",
    "consult_capacity",
    "consult_abrasive_escalation",
    "consult_nozzle_summary",
    "record_print_odometer",
]
