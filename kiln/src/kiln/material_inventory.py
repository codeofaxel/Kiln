"""Material inventory — what is on hand, how fast it is used, when it runs
out, whether a print has enough, and which spool a job draws from.

Reads the persistence layer (spools, printer_materials, print_history).  On
an install with one machine every answer is worked out here, offline.  A
question across several machines is Kiln Business and is answered on Kiln's
servers from the rows this install sends (:func:`inventory_answer`, the one
router every door calls); which printer takes each job and which spool moves
where are planned there and nowhere in this package.

All public functions accept a ``db`` parameter (a :class:`~kiln.persistence.KilnDB`
instance) as their first argument.  Imports from other Kiln modules are lazy
to avoid circular dependencies.

Usage::

    from kiln.persistence import get_db
    from kiln.material_inventory import get_fleet_material_summary, forecast_consumption

    db = get_db()
    summary, refused = inventory_answer("summary", db, feature="Material summary")
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from kiln.cost_estimator import BUILTIN_MATERIALS, DEFAULT_MATERIAL, resolve_material

logger = logging.getLogger(__name__)

# Standard 1.75 mm filament cross-section area (mm^2).
_FILAMENT_DIAMETER_MM: float = 1.75
_FILAMENT_RADIUS_MM: float = _FILAMENT_DIAMETER_MM / 2.0
_FILAMENT_CROSS_SECTION_MM2: float = math.pi * _FILAMENT_RADIUS_MM**2

# Each table row's density, and PLA's, for callers that read densities as a
# mapping.  Derived from the one material table, never restated.
_DEFAULT_DENSITIES: dict[str, float] = {
    name: row.density_g_per_cm3 for name, row in BUILTIN_MATERIALS.items()
}
_FALLBACK_DENSITY: float = BUILTIN_MATERIALS[DEFAULT_MATERIAL].density_g_per_cm3


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FleetMaterialSummary:
    """Aggregated material view across all printers."""

    material_type: str
    total_grams: float
    spool_count: int
    printers_loaded: tuple[str, ...]
    colors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_type": self.material_type,
            "total_grams": self.total_grams,
            "spool_count": self.spool_count,
            "printers_loaded": list(self.printers_loaded),
            "colors": list(self.colors),
        }


@dataclass(frozen=True)
class ConsumptionRecord:
    """Material usage over a time period."""

    material_type: str
    grams_used: float
    print_count: int
    period_days: int
    daily_rate_grams: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_type": self.material_type,
            "grams_used": self.grams_used,
            "print_count": self.print_count,
            "period_days": self.period_days,
            "daily_rate_grams": self.daily_rate_grams,
        }


@dataclass(frozen=True)
class ConsumptionForecast:
    """Projected material needs."""

    material_type: str
    current_stock_grams: float
    daily_rate_grams: float
    days_until_empty: float | None
    restock_recommended: bool
    urgency: str  # "ok", "low", "critical"

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_type": self.material_type,
            "current_stock_grams": self.current_stock_grams,
            "daily_rate_grams": self.daily_rate_grams,
            "days_until_empty": self.days_until_empty,
            "restock_recommended": self.restock_recommended,
            "urgency": self.urgency,
        }


@dataclass(frozen=True)
class MaterialCheck:
    """Result of checking if there's enough material for a print."""

    sufficient: bool
    loaded_grams: float | None
    required_grams: float
    shortfall_grams: float
    printer_name: str
    material_type: str | None
    suggestions: tuple[str, ...]
    alternative_printers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sufficient": self.sufficient,
            "loaded_grams": self.loaded_grams,
            "required_grams": self.required_grams,
            "shortfall_grams": self.shortfall_grams,
            "printer_name": self.printer_name,
            "material_type": self.material_type,
            "suggestions": list(self.suggestions),
            "alternative_printers": list(self.alternative_printers),
        }


@dataclass(frozen=True)
class RestockSuggestion:
    """A material that needs restocking with purchase info."""

    material_type: str
    brand: str | None
    color: str | None
    current_grams: float
    monthly_usage_grams: float
    days_until_empty: float | None
    urgency: str  # "low", "critical"
    purchase_urls: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_type": self.material_type,
            "brand": self.brand,
            "color": self.color,
            "current_grams": self.current_grams,
            "monthly_usage_grams": self.monthly_usage_grams,
            "days_until_empty": self.days_until_empty,
            "urgency": self.urgency,
            "purchase_urls": dict(self.purchase_urls),
        }


@dataclass(frozen=True)
class OnHandMaterial:
    """One material type the user physically has — loaded and/or shelved."""

    material_type: str
    loaded_on: tuple[dict[str, Any], ...]
    shelf_spools: tuple[dict[str, Any], ...]
    total_grams: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_type": self.material_type,
            "loaded_on": [dict(r) for r in self.loaded_on],
            "shelf_spools": [dict(s) for s in self.shelf_spools],
            "total_grams": self.total_grams,
        }


@dataclass(frozen=True)
class FleetAssignment:
    """Optimal job-to-printer assignment by material."""

    job_file: str
    recommended_printer: str
    reason: str
    material_match: bool
    remaining_after_print_grams: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_file": self.job_file,
            "recommended_printer": self.recommended_printer,
            "reason": self.reason,
            "material_match": self.material_match,
            "remaining_after_print_grams": self.remaining_after_print_grams,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _mm_to_grams(filament_mm: float, material_type: str | None = None) -> float:
    """Convert extruded filament length (mm) to weight (grams).

    Uses standard 1.75 mm filament diameter and the material's density from
    the one material table, PLA's when the table has no row for it.
    """
    density = (resolve_material(material_type) or BUILTIN_MATERIALS[DEFAULT_MATERIAL]).density_g_per_cm3
    volume_cm3 = (filament_mm * _FILAMENT_CROSS_SECTION_MM2) / 1000.0
    return volume_cm3 * density


def _get_all_materials(db: Any) -> list[dict[str, Any]]:
    """Fetch all printer_materials rows across all printers."""
    rows = db._conn.execute(
        "SELECT * FROM printer_materials ORDER BY printer_name, tool_index"
    ).fetchall()
    return [dict(r) for r in rows]


def _get_purchase_urls(
    material_type: str,
    brand: str | None,
    color: str | None,
) -> dict[str, str]:
    """A store search link for re-buying this spool, built from what the spool record says."""
    parts = [material_type]
    if brand:
        parts.insert(0, brand)
    parts.append("1.75mm")
    if color:
        parts.append(color)
    query = "+".join(parts)
    return {"amazon": f"https://www.amazon.com/s?k={query}"}


def fleet_scope_verdict(feature: str) -> dict[str, Any] | None:
    """Refuse a FLEET-WIDE material answer below Business; ``None`` allows.

    Every tool here that answers ACROSS machines calls this one helper —
    a per-door branch is how the drift starts, and the printer cap already
    taught us that lesson at ``register_printer`` (2026-07-27).

    What is gated is the cross-machine ANSWER.  Registering printers and
    printing on them stays free at every tier — serial use of two owned
    machines is honest single-machine use, and the print path never
    consults this gate.  But a view that spans machines ("which of mine
    holds PETG") only means anything on a fleet, and a fleet is Business:
    an install with more registered MACHINES than its tier's cap is
    refused here even when it drives them one at a time.  Per-machine
    questions stay open at every tier — scope to one printer and this
    gate never fires.

    Two failure modes, deliberately opposite:

    * **Tier unresolvable** (``kiln.licensing`` absent — public Kiln with
      no kiln-pro) → treated as FREE, the most restrictive cap.  Fails
      CLOSED, so a missing licence module can never unlock a paid view.
    * **Registry unreadable / any unexpected error** → allowed.  Fails
      OPEN, because a broken registry is our fault and must never be the
      reason a user cannot see their own materials.
    """
    try:
        from kiln.registry import get_registry

        # Fast path — one machine can never be a fleet.  Costs nothing,
        # which is what nearly every install pays here.
        machines = get_registry().count
        if machines <= 1:
            return None

        try:
            from kiln.licensing import get_tier, max_printers_for_tier

            cap = max_printers_for_tier(get_tier())
        except Exception:
            cap = 1  # free-tier fallback: kiln-pro absent
        if cap is None or cap <= 0 or machines <= cap:
            return None

        verdict = {
            "success": False,
            "error": (
                f"{feature} answers across all {machines} of your machines, "
                f"and fleet views are a Kiln Business feature."
            ),
            "code": "TIER_FLEET_SCOPE",
            "machines": machines,
            "upgrade_hint": (
                "Ask about one printer to stay on your current plan, or see "
                "https://kiln3d.com/pricing for fleet-wide answers."
            ),
        }
        # Structured twin of the sentences above, for a surface that renders
        # rather than prints.  Additive: the per-printer free path stays the
        # first thing named, and every door that funnels here inherits the
        # block without knowing it exists.
        try:
            from kiln.tiers_and_terms import upgrade_nudge_block

            verdict.setdefault(
                "upgrade_nudge",
                upgrade_nudge_block(
                    variant="cross_machine_inventory",
                    tier="business",
                    feature="Cross-machine material inventory",
                    headline=(
                        "Answer this material question across every machine "
                        "at once."
                    ),
                    outcome_preview=(
                        "Kiln Business would compare spools and capability "
                        "across the registered machines and prepare one "
                        "cross-machine answer."
                    ),
                    free_included="The per-printer answer stays available.",
                    moment="resource_threshold",
                ),
            )
        except Exception:  # noqa: BLE001 — the refusal matters more than chrome
            logger.debug("inventory nudge attach skipped", exc_info=True)
        return verdict
    except Exception:  # noqa: BLE001 — never block a user from their own data
        logger.debug("fleet-scope gate soft-passed", exc_info=True)
        return None


def _determine_urgency(days_until_empty: float | None) -> str:
    """Map days-until-empty to urgency level."""
    if days_until_empty is None:
        return "ok"
    if days_until_empty < 7:
        return "critical"
    if days_until_empty < 30:
        return "low"
    return "ok"


# ---------------------------------------------------------------------------
# Reads: this install's own rows
# ---------------------------------------------------------------------------
#
# Each read is a sum or a filter over the rows it is handed.  On an install
# with one machine they run here, over this install's rows; across machines
# the same question is Kiln Business and is answered on Kiln's servers from
# the rows this install sends (:func:`inventory_answer`).


def _spool_rows(db: Any) -> list[dict[str, Any]]:
    """The spool records, cut down to what a material answer reads."""
    return [
        {k: s.get(k) for k in ("id", "material_type", "color", "brand", "remaining_grams")}
        for s in db.list_spools()
    ]


def _loaded_rows(db: Any) -> list[dict[str, Any]]:
    """What each printer has loaded, cut down to what a material answer reads."""
    return [
        {k: m.get(k) for k in ("printer_name", "tool_index", "material_type", "color", "spool_id", "remaining_grams")}
        for m in _get_all_materials(db)
    ]


def _history_rows(db: Any) -> list[dict[str, Any]]:
    """The completed prints, cut down to what consumption reads."""
    rows: list[dict[str, Any]] = []
    for rec in db.list_print_history(status="completed", limit=10000):
        meta = rec.get("metadata")
        filament_mm = meta.get("filament_used_mm") if isinstance(meta, dict) else None
        rows.append({
            "material_type": rec.get("material_type"),
            "completed_at": rec.get("completed_at"),
            "filament_used_mm": filament_mm,
        })
    return rows


def summarize(spools: list[dict[str, Any]], loaded: list[dict[str, Any]]) -> list[FleetMaterialSummary]:
    """Per-material totals over *spools* and *loaded* rows: stock in grams,
    spool count, the printers it is loaded on, its colours."""
    agg: dict[str, dict[str, Any]] = {}

    def _bucket(mt: str) -> dict[str, Any]:
        return agg.setdefault(mt, {"total_grams": 0.0, "spool_count": 0, "printers": set(), "colors": set()})

    for spool in spools:
        mt = (spool.get("material_type") or "").upper()
        if not mt:
            continue
        bucket = _bucket(mt)
        bucket["total_grams"] += spool.get("remaining_grams", 0.0) or 0.0
        bucket["spool_count"] += 1
        color = spool.get("color")
        if color:
            bucket["colors"].add(color)

    for mat in loaded:
        mt = (mat.get("material_type") or "").upper()
        if not mt:
            continue
        bucket = _bucket(mt)
        printer = mat.get("printer_name", "")
        if printer:
            bucket["printers"].add(printer)
        color = mat.get("color")
        if color:
            bucket["colors"].add(color)
        # A loaded row linked to a spool is counted through the spool.
        if not mat.get("spool_id") and mat.get("remaining_grams"):
            bucket["total_grams"] += mat["remaining_grams"]

    return [
        FleetMaterialSummary(
            material_type=mt,
            total_grams=round(agg[mt]["total_grams"], 2),
            spool_count=agg[mt]["spool_count"],
            printers_loaded=tuple(sorted(agg[mt]["printers"])),
            colors=tuple(sorted(agg[mt]["colors"])),
        )
        for mt in sorted(agg)
    ]


def get_fleet_material_summary(db: Any) -> list[FleetMaterialSummary]:
    """Per-material totals over this install's spools and loaded materials."""
    return summarize(_spool_rows(db), _loaded_rows(db))


def on_hand(
    spools: list[dict[str, Any]],
    loaded: list[dict[str, Any]],
    *,
    printer_name: str | None = None,
) -> list[OnHandMaterial]:
    """Materials physically on hand, grouped by type: loaded (and on which
    printer) and on the shelf.  With *printer_name*, the loaded half is that
    one printer's and a spool loaded on another printer is left out; the
    shelf always counts, since a shelved spool can be swapped in."""
    loaded_spool_ids = {m.get("spool_id") for m in loaded if m.get("spool_id")}
    scoped = [m for m in loaded if m.get("printer_name") == printer_name] if printer_name is not None else loaded
    scoped_spool_ids = {m.get("spool_id") for m in scoped if m.get("spool_id")}

    agg: dict[str, dict[str, Any]] = {}

    def _bucket(mt: str) -> dict[str, Any]:
        return agg.setdefault(mt, {"loaded": [], "shelf": [], "grams": 0.0})

    for mat in scoped:
        mt = (mat.get("material_type") or "").upper()
        if not mt:
            continue
        bucket = _bucket(mt)
        bucket["loaded"].append({
            "printer_name": mat.get("printer_name", ""),
            "tool_index": mat.get("tool_index", 0),
            "remaining_grams": mat.get("remaining_grams"),
            "color": mat.get("color"),
            "spool_id": mat.get("spool_id"),
        })
        # Unlinked rows carry their own grams; linked rows count through the spool.
        if not mat.get("spool_id") and mat.get("remaining_grams"):
            bucket["grams"] += mat["remaining_grams"]

    for spool in spools:
        mt = (spool.get("material_type") or "").upper()
        if not mt:
            continue
        rem = spool.get("remaining_grams", 0.0) or 0.0
        if spool.get("id") in loaded_spool_ids:
            if spool.get("id") in scoped_spool_ids:
                _bucket(mt)["grams"] += rem
            continue
        bucket = _bucket(mt)
        bucket["grams"] += rem
        bucket["shelf"].append({
            "spool_id": spool.get("id"),
            "brand": spool.get("brand"),
            "color": spool.get("color"),
            "remaining_grams": rem,
        })

    return [
        OnHandMaterial(
            material_type=mt,
            loaded_on=tuple(agg[mt]["loaded"]),
            shelf_spools=tuple(agg[mt]["shelf"]),
            total_grams=round(agg[mt]["grams"], 2),
        )
        for mt in sorted(agg)
    ]


def get_on_hand_materials(db: Any, *, printer_name: str | None = None) -> list[OnHandMaterial]:
    """What this install physically has, grouped by type (:func:`on_hand`)."""
    return on_hand(_spool_rows(db), _loaded_rows(db), printer_name=printer_name)


def consumption(history: list[dict[str, Any]], *, days: int, now: float) -> list[ConsumptionRecord]:
    """Filament used per material over the last *days* of completed prints
    (*history* rows: ``material_type``, ``completed_at``, ``filament_used_mm``),
    converted to grams by the material's density."""
    cutoff = now - (days * 86400)
    usage: dict[str, dict[str, Any]] = {}
    for rec in history:
        completed_at = rec.get("completed_at")
        if completed_at is not None and completed_at < cutoff:
            continue
        mt = (rec.get("material_type") or "UNKNOWN").upper()
        bucket = usage.setdefault(mt, {"grams": 0.0, "count": 0})
        bucket["count"] += 1
        filament_mm = rec.get("filament_used_mm")
        if filament_mm is not None:
            bucket["grams"] += _mm_to_grams(float(filament_mm), mt)
    return [
        ConsumptionRecord(
            material_type=mt,
            grams_used=round(usage[mt]["grams"], 2),
            print_count=usage[mt]["count"],
            period_days=days,
            daily_rate_grams=round(usage[mt]["grams"] / max(days, 1), 4),
        )
        for mt in sorted(usage)
    ]


def get_consumption_history(db: Any, *, days: int = 30) -> list[ConsumptionRecord]:
    """Filament used per material over this install's last *days* of prints."""
    return consumption(_history_rows(db), days=days, now=time.time())


def forecast(
    summaries: list[FleetMaterialSummary],
    history: list[ConsumptionRecord],
    material_type: str,
) -> ConsumptionForecast:
    """When *material_type* runs out: its stock over its daily use."""
    mt_upper = material_type.upper()
    current_stock = next((s.total_grams for s in summaries if s.material_type == mt_upper), 0.0)
    daily_rate = next((h.daily_rate_grams for h in history if h.material_type == mt_upper), 0.0)
    days_until_empty = None if daily_rate <= 0 else round(current_stock / daily_rate, 1)
    urgency = _determine_urgency(days_until_empty)
    return ConsumptionForecast(
        material_type=mt_upper,
        current_stock_grams=round(current_stock, 2),
        daily_rate_grams=round(daily_rate, 4),
        days_until_empty=days_until_empty,
        restock_recommended=urgency in ("low", "critical"),
        urgency=urgency,
    )


def forecast_consumption(db: Any, *, material_type: str, days_ahead: int = 30) -> ConsumptionForecast:
    """When *material_type* runs out on this install.

    :param days_ahead: How many days of history set the rate.
    """
    return forecast(get_fleet_material_summary(db), get_consumption_history(db, days=days_ahead), material_type)


def restock(
    summaries: list[FleetMaterialSummary],
    history: list[ConsumptionRecord],
    spools: list[dict[str, Any]],
) -> list[RestockSuggestion]:
    """Materials that run out within 30 days at their current use, critical
    first, each with a store search link built from its spool's brand and
    colour."""
    rate_map = {h.material_type: h.daily_rate_grams for h in history}
    suggestions: list[RestockSuggestion] = []
    for s in summaries:
        daily_rate = rate_map.get(s.material_type, 0.0)
        if daily_rate <= 0:
            continue
        days_left = s.total_grams / daily_rate
        if days_left >= 30:
            continue
        brand: str | None = None
        color: str | None = None
        for spool in spools:
            if (spool.get("material_type") or "").upper() == s.material_type:
                brand = spool.get("brand") or brand
                color = spool.get("color") or color
                break
        suggestions.append(RestockSuggestion(
            material_type=s.material_type,
            brand=brand,
            color=color,
            current_grams=s.total_grams,
            monthly_usage_grams=round(daily_rate * 30, 2),
            days_until_empty=round(days_left, 1),
            urgency=_determine_urgency(days_left),
            purchase_urls=_get_purchase_urls(s.material_type, brand, color),
        ))
    urgency_order = {"critical": 0, "low": 1}
    suggestions.sort(key=lambda s: urgency_order.get(s.urgency, 2))
    return suggestions


def get_restock_suggestions(db: Any) -> list[RestockSuggestion]:
    """This install's materials that run out within 30 days."""
    return restock(get_fleet_material_summary(db), get_consumption_history(db, days=30), _spool_rows(db))


def holding(
    loaded: list[dict[str, Any]],
    *,
    material_type: str,
    color: str | None = None,
    min_grams: float = 0.0,
) -> list[dict[str, Any]]:
    """The loaded rows of *material_type* (and *color*) with at least
    *min_grams* left, most stock first."""
    mt_upper = material_type.upper()
    results: list[dict[str, Any]] = []
    for mat in loaded:
        if (mat.get("material_type") or "").upper() != mt_upper:
            continue
        remaining = mat.get("remaining_grams") or 0.0
        if remaining < min_grams:
            continue
        if color and (mat.get("color") or "").lower() != color.lower():
            continue
        results.append({
            "printer_name": mat["printer_name"],
            "remaining_grams": remaining,
            "color": mat.get("color"),
            "spool_id": mat.get("spool_id"),
        })
    results.sort(key=lambda r: r["remaining_grams"], reverse=True)
    return results


def find_printers_with_material(
    db: Any,
    *,
    material_type: str,
    color: str | None = None,
    min_grams: float = 0.0,
) -> list[dict[str, Any]]:
    """This install's printers with *material_type* loaded (:func:`holding`)."""
    return holding(_loaded_rows(db), material_type=material_type, color=color, min_grams=min_grams)


def check_material_sufficiency(
    db: Any,
    *,
    printer_name: str,
    required_grams: float,
    material_type: str | None = None,
    other_machines: list[dict[str, Any]] | None = None,
) -> MaterialCheck:
    """Check if a printer has enough material for a print.

    When the loaded material is insufficient, says what the person can do
    with what they have: another printer that has enough loaded, a spool on
    the shelf, a pause-and-swap.  Only when none of those will do does it
    offer help finding a filament, as one question: never a store, a link
    or a list unasked.

    :param other_machines: The printers holding enough of the material, as
        :func:`holding` lists them, when they come from elsewhere (Kiln's
        servers, across machines).  ``None`` reads this install's own rows.
    """
    materials = db.list_materials(printer_name)
    loaded_grams: float | None = None
    loaded_type: str | None = material_type

    for mat in materials:
        if mat.get("tool_index", 0) == 0:
            loaded_grams = mat.get("remaining_grams")
            loaded_type = mat.get("material_type") or material_type
            break

    if loaded_grams is None:
        shortfall = required_grams
        sufficient = False
    else:
        shortfall = max(0.0, required_grams - loaded_grams)
        sufficient = shortfall <= 0

    shortfall = round(shortfall, 2)

    suggestions: list[str] = []
    alt_printers: list[str] = []

    if not sufficient and loaded_type:
        candidates = (
            other_machines
            if other_machines is not None
            else find_printers_with_material(db, material_type=loaded_type, min_grams=required_grams)
        )
        for cand in candidates:
            cname = cand["printer_name"]
            if cname != printer_name:
                alt_printers.append(cname)
                remaining = cand.get("remaining_grams", 0.0)
                suggestions.append(
                    f"Printer {cname} has {remaining:.0f}g of {loaded_type} loaded"
                )

        # Spools on the shelf (not loaded on any printer)
        loaded_spool_ids = {
            m.get("spool_id") for m in _get_all_materials(db) if m.get("spool_id")
        }
        on_the_shelf = False
        for spool in db.list_spools():
            spool_mt = (spool.get("material_type") or "").upper()
            if spool_mt != loaded_type.upper():
                continue
            if spool["id"] in loaded_spool_ids:
                continue
            rem = spool.get("remaining_grams", 0.0) or 0.0
            if rem >= required_grams:
                on_the_shelf = True
                brand = spool.get("brand", "")
                color = spool.get("color", "")
                label = " ".join(filter(None, [brand, color]))
                suggestions.append(
                    f"You have an unused spool of {loaded_type} ({label}, {rem:.0f}g)"
                )

        # Partial sufficiency — pause-and-swap hint
        if loaded_grams is not None and loaded_grams > 0 and loaded_grams < required_grams:
            suggestions.append(
                "Slice with pause-and-swap at the estimated depletion layer"
            )

        # Kiln never reads as if it is selling filament.  When the person
        # has nothing that will do (no other printer, no spool on the
        # shelf) they get one offer of help: no store, no link, no list.
        # A yes is answered by the tools they reach by asking.
        if not alt_printers and not on_the_shelf:
            suggestions.append(
                f"Want help finding a {loaded_type} filament that suits this print?"
            )

    return MaterialCheck(
        sufficient=sufficient,
        loaded_grams=loaded_grams,
        required_grams=required_grams,
        shortfall_grams=shortfall,
        printer_name=printer_name,
        material_type=loaded_type,
        suggestions=tuple(suggestions),
        alternative_printers=tuple(alt_printers),
    )


# ---------------------------------------------------------------------------
# One machine's jobs and spools
# ---------------------------------------------------------------------------
#
# Which of several printers takes each job, and which spool moves to which
# printer, are fleet questions: Kiln Business, planned on Kiln's servers.  On
# one machine the questions are smaller -- can its loaded spool take these
# jobs, in order, and what has to come off the shelf -- and are answered here.


def _machine_slot(loaded: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The machine's first-tool slot: the one a print draws from.  A row
    left under another printer's name (renamed, or never registered) does
    not stand in for it when the registered machine has one."""
    slots = [m for m in loaded if m.get("tool_index", 0) == 0]
    if not slots:
        return None
    try:
        from kiln.registry import get_registry

        names = set(get_registry().list_names())
    except Exception:  # noqa: BLE001 -- no registry: the first slot
        names = set()
    return next((m for m in slots if m.get("printer_name") in names), slots[0])


def plan_jobs_for_one_machine(db: Any, *, jobs: list[dict[str, Any]]) -> list[FleetAssignment]:
    """Which of *jobs* the machine's loaded spool can take, in order.

    Each job: ``file_name``, ``material_type``, ``required_grams``, optional
    ``color``.  A job is taken when the first-tool spool is that material
    and has enough left after the jobs before it; a colour asked for and
    matched is said.
    """
    if not jobs:
        return []
    slot = _machine_slot(_get_all_materials(db))
    capacity = (slot.get("remaining_grams") or 0.0) if slot is not None else 0.0
    out: list[FleetAssignment] = []
    for job in jobs:
        file_name = job.get("file_name", "unknown")
        mt = (job.get("material_type") or "").upper()
        required = job.get("required_grams", 0.0)
        preferred = job.get("color")
        if slot is not None and (slot.get("material_type") or "").upper() == mt and capacity >= required:
            colour_match = not preferred or (slot.get("color") or "").lower() == preferred.lower()
            remaining_after = round(capacity - required, 2)
            reason = f"Has {mt} loaded with {capacity:.0f}g remaining"
            if colour_match and preferred:
                reason += f" (colour match: {preferred})"
            out.append(FleetAssignment(
                job_file=file_name,
                recommended_printer=slot["printer_name"],
                reason=reason,
                material_match=True,
                remaining_after_print_grams=remaining_after,
            ))
            capacity = remaining_after
        else:
            out.append(FleetAssignment(
                job_file=file_name,
                recommended_printer="",
                reason=f"No printer found with {mt} and {required:.0f}g available",
                material_match=False,
                remaining_after_print_grams=None,
            ))
    return out


def swaps_for_one_machine(db: Any, *, jobs: list[dict[str, Any]]) -> list[str]:
    """What has to come off the shelf for the machine to run *jobs*.

    Each job: ``material_type`` and ``required_grams``.  For each material
    the loaded spool cannot cover, the shelf spools of it, largest first,
    until the shortfall is met; the machine itself is named as where one goes
    when what it has loaded is not needed by these jobs.
    """
    if not jobs:
        return []
    all_loaded = _get_all_materials(db)
    slot = _machine_slot(all_loaded)
    slot_mt = (slot.get("material_type") or "").upper() if slot is not None else ""

    demand: dict[str, float] = {}
    for job in jobs:
        mt = (job.get("material_type") or "").upper()
        demand[mt] = demand.get(mt, 0.0) + job.get("required_grams", 0.0)

    covered = {slot_mt: slot.get("remaining_grams") or 0.0} if slot is not None else {}
    loaded_spool_ids = {m.get("spool_id") for m in all_loaded if m.get("spool_id")}

    suggestions: list[str] = []
    for mt, needed in sorted(demand.items()):
        available = covered.get(mt, 0.0)
        if available >= needed:
            continue
        shortfall = needed - available
        shelf = [
            s for s in db.list_spools()
            if (s.get("material_type") or "").upper() == mt
            and s["id"] not in loaded_spool_ids
            and (s.get("remaining_grams", 0.0) or 0.0) > 0
        ]
        shelf.sort(key=lambda s: s.get("remaining_grams", 0.0), reverse=True)
        target = slot["printer_name"] if slot is not None and slot_mt != mt and slot_mt not in demand else None
        for spool in shelf:
            if shortfall <= 0:
                break
            rem = spool.get("remaining_grams", 0.0) or 0.0
            label = " ".join(filter(None, [spool.get("brand", ""), spool.get("color", ""), mt]))
            if target:
                suggestions.append(f"Load {label} spool ({rem:.0f}g) onto {target}")
                target = None
            else:
                suggestions.append(f"Swap a printer to {label} spool ({rem:.0f}g) — no idle slot identified")
            shortfall -= rem
    return suggestions


# ---------------------------------------------------------------------------
# The one router every material-inventory door calls
# ---------------------------------------------------------------------------

#: The questions a door asks, and what each needs sent when it is answered
#: across machines (Kiln's servers): spool rows, loaded rows, completed
#: prints, the jobs.  Nothing else of the install leaves.
FLEET_QUESTIONS: dict[str, tuple[str, ...]] = {
    "summary": ("spools", "loaded"),
    "history": ("history",),
    "forecast": ("spools", "loaded", "history"),
    "restock": ("spools", "loaded", "history"),
    "find": ("loaded",),
    "alternatives": ("loaded",),
    "assign": ("loaded", "jobs"),
    "swaps": ("loaded", "spools", "jobs"),
    "on_hand": ("spools", "loaded"),
}


def registered_machines() -> int:
    """How many machines are registered here; ``1`` when the registry cannot
    be read, so a broken registry never stands between a person and their
    own materials (the same posture as :func:`fleet_scope_verdict`)."""
    try:
        from kiln.registry import get_registry

        return int(get_registry().count)
    except Exception:  # noqa: BLE001
        logger.debug("registry unreadable; answering for one machine", exc_info=True)
        return 1


def _one_machine(question: str, db: Any, params: dict[str, Any]) -> Any:
    if question == "summary":
        return [s.to_dict() for s in get_fleet_material_summary(db)]
    if question == "history":
        return [r.to_dict() for r in get_consumption_history(db, days=params["days"])]
    if question == "forecast":
        return forecast_consumption(db, material_type=params["material_type"], days_ahead=params["days_ahead"]).to_dict()
    if question == "restock":
        return [s.to_dict() for s in get_restock_suggestions(db)]
    if question in ("find", "alternatives"):
        return find_printers_with_material(
            db, material_type=params["material_type"], color=params.get("color"), min_grams=params.get("min_grams", 0.0),
        )
    if question == "assign":
        return [a.to_dict() for a in plan_jobs_for_one_machine(db, jobs=params["jobs"])]
    if question == "swaps":
        return swaps_for_one_machine(db, jobs=params["jobs"])
    if question == "on_hand":
        return [m.to_dict() for m in get_on_hand_materials(db)]
    raise ValueError(f"not a material question: {question!r}")


def inventory_answer(question: str, db: Any, *, feature: str, **params: Any) -> tuple[Any, dict[str, Any] | None]:
    """``(answer, None)``, or ``(None, refusal)`` for a door to return as it is.

    The one router: on an install with one machine the answer is worked out
    here from this install's rows, offline and with no sign-in.  Across
    machines it is a fleet answer: below the plan that covers the machines
    (:func:`fleet_scope_verdict`, Kiln Business) the refusal comes back and
    nothing is asked; on that plan Kiln's servers answer from the rows the
    question needs (:data:`FLEET_QUESTIONS`, via
    :mod:`kiln._pro_fleet_materials_bridge`), and a miss is said in the
    shared served-answer voice.
    """
    if question not in FLEET_QUESTIONS:
        raise ValueError(f"not a material question: {question!r}")
    if registered_machines() <= 1:
        return _one_machine(question, db, params), None
    refused = fleet_scope_verdict(feature)
    if refused is not None:
        return None, refused
    from kiln import _pro_fleet_materials_bridge as bridge

    rows: dict[str, Any] = {}
    needs = FLEET_QUESTIONS[question]
    if "spools" in needs:
        rows["spools"] = _spool_rows(db)
    if "loaded" in needs:
        rows["loaded"] = _loaded_rows(db)
    if "history" in needs:
        rows["history"] = _history_rows(db)
    if "jobs" in needs:
        rows["jobs"] = [
            {k: job.get(k) for k in ("file_name", "material_type", "required_grams", "color") if k in job}
            for job in (params.get("jobs") or [])
            if isinstance(job, dict)
        ]
    asked = {k: v for k, v in params.items() if k != "jobs"}
    if "history" in needs:
        asked["now"] = time.time()
    return bridge.fleet_answer(question, rows=rows, params=asked, feature=feature)
