"""Material Inventory Engine — consumption tracking, forecasting, restock
suggestions, fleet optimization, and material sufficiency checks.

Ties together the persistence layer (spools, printer_materials, print_history)
with intelligence about usage patterns and fleet-wide material allocation.

All public functions accept a ``db`` parameter (a :class:`~kiln.persistence.KilnDB`
instance) as their first argument.  Imports from other Kiln modules are lazy
to avoid circular dependencies.

Usage::

    from kiln.persistence import get_db
    from kiln.material_inventory import get_fleet_material_summary, forecast_consumption

    db = get_db()
    summary = get_fleet_material_summary(db)
    forecast = forecast_consumption(db, material_type="PLA")
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Standard 1.75 mm filament cross-section area (mm^2).
_FILAMENT_DIAMETER_MM: float = 1.75
_FILAMENT_RADIUS_MM: float = _FILAMENT_DIAMETER_MM / 2.0
_FILAMENT_CROSS_SECTION_MM2: float = math.pi * _FILAMENT_RADIUS_MM**2

# Approximate density (g/cm^3) for common materials.  Used when converting
# filament_used_mm to grams when no cost_estimator profile is available.
_DEFAULT_DENSITIES: dict[str, float] = {
    "PLA": 1.24,
    "PETG": 1.27,
    "ABS": 1.04,
    "TPU": 1.21,
    "ASA": 1.07,
    "NYLON": 1.14,
    "PC": 1.20,
}
_FALLBACK_DENSITY: float = 1.24  # PLA as default


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

    Uses standard 1.75 mm filament diameter and material density.
    """
    density = _DEFAULT_DENSITIES.get(
        (material_type or "").upper(), _FALLBACK_DENSITY
    )
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
    """Generate purchase URLs.  Lazy-imports material_catalog to avoid circular deps."""
    try:
        from kiln.material_catalog import find_matching_entry, get_purchase_urls

        entry = find_matching_entry(vendor=brand, material_type=material_type)
        if entry:
            return get_purchase_urls(entry.id, color=color)
    except ImportError:
        pass
    # Fallback: generate generic Amazon search URL
    parts = [material_type]
    if brand:
        parts.insert(0, brand)
    parts.append("1.75mm")
    if color:
        parts.append(color)
    query = "+".join(parts)
    return {"amazon": f"https://www.amazon.com/s?k={query}"}


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
# Public API
# ---------------------------------------------------------------------------


def get_fleet_material_summary(db: Any) -> list[FleetMaterialSummary]:
    """Aggregate material inventory across all printers and spools.

    Combines ``list_spools()`` and all ``printer_materials`` rows to produce
    a per-material-type summary of total stock, spool counts, loaded printers,
    and available colours.
    """
    # Gather data from spools
    spools = db.list_spools()
    # Gather data from loaded materials across all printers
    loaded_materials = _get_all_materials(db)

    # Group by material_type (normalised uppercase)
    agg: dict[str, dict[str, Any]] = {}

    for spool in spools:
        mt = (spool.get("material_type") or "").upper()
        if not mt:
            continue
        bucket = agg.setdefault(mt, {
            "total_grams": 0.0,
            "spool_count": 0,
            "printers": set(),
            "colors": set(),
        })
        bucket["total_grams"] += spool.get("remaining_grams", 0.0) or 0.0
        bucket["spool_count"] += 1
        color = spool.get("color")
        if color:
            bucket["colors"].add(color)

    for mat in loaded_materials:
        mt = (mat.get("material_type") or "").upper()
        if not mt:
            continue
        bucket = agg.setdefault(mt, {
            "total_grams": 0.0,
            "spool_count": 0,
            "printers": set(),
            "colors": set(),
        })
        printer = mat.get("printer_name", "")
        if printer:
            bucket["printers"].add(printer)
        color = mat.get("color")
        if color:
            bucket["colors"].add(color)
        # Only add remaining_grams from loaded materials if there is no
        # linked spool (to avoid double-counting).
        if not mat.get("spool_id") and mat.get("remaining_grams"):
            bucket["total_grams"] += mat["remaining_grams"]

    results: list[FleetMaterialSummary] = []
    for mt in sorted(agg):
        b = agg[mt]
        results.append(FleetMaterialSummary(
            material_type=mt,
            total_grams=round(b["total_grams"], 2),
            spool_count=b["spool_count"],
            printers_loaded=tuple(sorted(b["printers"])),
            colors=tuple(sorted(b["colors"])),
        ))
    return results


def get_consumption_history(db: Any, *, days: int = 30) -> list[ConsumptionRecord]:
    """Calculate material consumption from print history.

    Scans completed prints within the given window and aggregates filament
    usage per material type.  Filament length (mm) from the ``metadata``
    JSON field is converted to grams using standard densities.
    """
    cutoff = time.time() - (days * 86400)
    records = db.list_print_history(status="completed", limit=10000)

    # Group usage by material type
    usage: dict[str, dict[str, Any]] = {}

    for rec in records:
        completed_at = rec.get("completed_at")
        if completed_at is not None and completed_at < cutoff:
            continue

        mt = (rec.get("material_type") or "UNKNOWN").upper()
        bucket = usage.setdefault(mt, {"grams": 0.0, "count": 0})
        bucket["count"] += 1

        # Try to extract filament_used_mm from metadata
        meta = rec.get("metadata")
        if isinstance(meta, dict):
            filament_mm = meta.get("filament_used_mm")
            if filament_mm is not None:
                bucket["grams"] += _mm_to_grams(float(filament_mm), mt)

    results: list[ConsumptionRecord] = []
    for mt in sorted(usage):
        b = usage[mt]
        daily_rate = b["grams"] / max(days, 1)
        results.append(ConsumptionRecord(
            material_type=mt,
            grams_used=round(b["grams"], 2),
            print_count=b["count"],
            period_days=days,
            daily_rate_grams=round(daily_rate, 4),
        ))
    return results


def forecast_consumption(
    db: Any,
    *,
    material_type: str,
    days_ahead: int = 30,
) -> ConsumptionForecast:
    """Project when a material type will run out.

    Combines current stock from :func:`get_fleet_material_summary` with the
    historical consumption rate to estimate remaining days.

    :param material_type: Material type to forecast (e.g. ``"PLA"``).
    :param days_ahead: How many days of history to use for the rate estimate.
    """
    mt_upper = material_type.upper()

    # Current stock
    summaries = get_fleet_material_summary(db)
    current_stock = 0.0
    for s in summaries:
        if s.material_type == mt_upper:
            current_stock = s.total_grams
            break

    # Consumption rate
    history = get_consumption_history(db, days=days_ahead)
    daily_rate = 0.0
    for h in history:
        if h.material_type == mt_upper:
            daily_rate = h.daily_rate_grams
            break

    # Forecast
    if daily_rate <= 0:
        days_until_empty = None
    else:
        days_until_empty = round(current_stock / daily_rate, 1)

    urgency = _determine_urgency(days_until_empty)
    restock = urgency in ("low", "critical")

    return ConsumptionForecast(
        material_type=mt_upper,
        current_stock_grams=round(current_stock, 2),
        daily_rate_grams=round(daily_rate, 4),
        days_until_empty=days_until_empty,
        restock_recommended=restock,
        urgency=urgency,
    )


def check_material_sufficiency(
    db: Any,
    *,
    printer_name: str,
    required_grams: float,
    material_type: str | None = None,
) -> MaterialCheck:
    """Check if a printer has enough material for a print.

    When the loaded material is insufficient, generates actionable suggestions
    including alternative printers, shelf stock, and purchase links.
    """
    materials = db.list_materials(printer_name)
    loaded_grams: float | None = None
    loaded_type: str | None = material_type
    loaded_color: str | None = None

    # Find the primary tool material (tool_index 0)
    for mat in materials:
        if mat.get("tool_index", 0) == 0:
            loaded_grams = mat.get("remaining_grams")
            loaded_type = mat.get("material_type") or material_type
            loaded_color = mat.get("color")
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
        # Find alternative printers with enough material
        candidates = find_printers_with_material(
            db,
            material_type=loaded_type,
            min_grams=required_grams,
        )
        for cand in candidates:
            cname = cand["printer_name"]
            if cname != printer_name:
                alt_printers.append(cname)
                remaining = cand.get("remaining_grams", 0.0)
                suggestions.append(
                    f"Printer {cname} has {remaining:.0f}g of {loaded_type} loaded"
                )

        # Check shelf stock (spools not loaded on any printer)
        spools = db.list_spools()
        loaded_spool_ids = {
            m.get("spool_id") for m in _get_all_materials(db) if m.get("spool_id")
        }
        for spool in spools:
            spool_mt = (spool.get("material_type") or "").upper()
            if spool_mt != loaded_type.upper():
                continue
            if spool["id"] in loaded_spool_ids:
                continue
            rem = spool.get("remaining_grams", 0.0) or 0.0
            if rem >= required_grams:
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

        # Purchase links
        if not alt_printers:
            urls = _get_purchase_urls(loaded_type, None, loaded_color)
            for store, url in urls.items():
                suggestions.append(f"Purchase more on {store}: {url}")

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


def get_restock_suggestions(db: Any) -> list[RestockSuggestion]:
    """Find materials running low and generate purchase links.

    Examines all material types in the inventory and returns suggestions
    for any that are projected to run out within 30 days, sorted by urgency
    (critical first).
    """
    summaries = get_fleet_material_summary(db)
    history = get_consumption_history(db, days=30)

    rate_map: dict[str, float] = {}
    for h in history:
        rate_map[h.material_type] = h.daily_rate_grams

    suggestions: list[RestockSuggestion] = []
    for s in summaries:
        daily_rate = rate_map.get(s.material_type, 0.0)
        if daily_rate <= 0:
            continue
        days_left = s.total_grams / daily_rate
        if days_left >= 30:
            continue

        urgency = _determine_urgency(days_left)

        # Find the most-used brand/color from spools of this type
        spools = db.list_spools()
        brand: str | None = None
        color: str | None = None
        for spool in spools:
            if (spool.get("material_type") or "").upper() == s.material_type:
                brand = spool.get("brand") or brand
                color = spool.get("color") or color
                break

        urls = _get_purchase_urls(s.material_type, brand, color)

        suggestions.append(RestockSuggestion(
            material_type=s.material_type,
            brand=brand,
            color=color,
            current_grams=s.total_grams,
            monthly_usage_grams=round(daily_rate * 30, 2),
            days_until_empty=round(days_left, 1),
            urgency=urgency,
            purchase_urls=urls,
        ))

    # Sort critical first, then low
    urgency_order = {"critical": 0, "low": 1}
    suggestions.sort(key=lambda s: urgency_order.get(s.urgency, 2))
    return suggestions


def find_printers_with_material(
    db: Any,
    *,
    material_type: str,
    color: str | None = None,
    min_grams: float = 0.0,
) -> list[dict[str, Any]]:
    """Find all printers that have a specific material loaded.

    Returns a list of dicts with keys ``printer_name``, ``remaining_grams``,
    ``color``, and ``spool_id``.
    """
    mt_upper = material_type.upper()
    all_materials = _get_all_materials(db)

    results: list[dict[str, Any]] = []
    for mat in all_materials:
        mat_type = (mat.get("material_type") or "").upper()
        if mat_type != mt_upper:
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

    # Sort by remaining grams descending (most stock first)
    results.sort(key=lambda r: r["remaining_grams"], reverse=True)
    return results


def optimize_fleet_assignment(
    db: Any,
    *,
    jobs: list[dict[str, Any]],
) -> list[FleetAssignment]:
    """Assign jobs to printers optimally by material availability.

    Each job dict should contain:
    - ``file_name``: G-code file name.
    - ``material_type``: Required material type.
    - ``required_grams``: Material needed.
    - ``color`` (optional): Preferred colour.

    For each job the function finds the best printer — one that has the right
    material loaded with enough remaining stock.  Ties are broken by choosing
    the printer with the most remaining grams (to minimise spool swaps).
    """
    if not jobs:
        return []

    all_materials = _get_all_materials(db)

    # Build a lookup: printer_name -> list of material rows
    printer_mats: dict[str, list[dict[str, Any]]] = {}
    for mat in all_materials:
        pn = mat.get("printer_name", "")
        printer_mats.setdefault(pn, []).append(mat)

    # Track remaining capacity per printer (modified as jobs are assigned)
    capacity: dict[str, float] = {}
    for pn, mats in printer_mats.items():
        for m in mats:
            if m.get("tool_index", 0) == 0:
                capacity[pn] = m.get("remaining_grams") or 0.0
                break

    assignments: list[FleetAssignment] = []
    for job in jobs:
        file_name = job.get("file_name", "unknown")
        mt = (job.get("material_type") or "").upper()
        required = job.get("required_grams", 0.0)
        preferred_color = job.get("color")

        best_printer: str | None = None
        best_remaining: float = -1.0
        best_match = False

        for pn, mats in printer_mats.items():
            for m in mats:
                if m.get("tool_index", 0) != 0:
                    continue
                mat_type = (m.get("material_type") or "").upper()
                if mat_type != mt:
                    continue
                remaining = capacity.get(pn, 0.0)
                if remaining < required:
                    continue
                # Colour match bonus (prefer exact match but accept any)
                color_match = (
                    not preferred_color
                    or (m.get("color") or "").lower() == preferred_color.lower()
                )
                # Prefer colour match, then most remaining
                if color_match and not best_match:
                    best_printer = pn
                    best_remaining = remaining
                    best_match = True
                elif color_match == best_match and remaining > best_remaining:
                    best_printer = pn
                    best_remaining = remaining
                    best_match = color_match

        if best_printer is not None:
            remaining_after = round(best_remaining - required, 2)
            capacity[best_printer] = remaining_after
            reason = f"Has {mt} loaded with {best_remaining:.0f}g remaining"
            if best_match and preferred_color:
                reason += f" (colour match: {preferred_color})"
            assignments.append(FleetAssignment(
                job_file=file_name,
                recommended_printer=best_printer,
                reason=reason,
                material_match=True,
                remaining_after_print_grams=remaining_after,
            ))
        else:
            # No suitable printer found
            assignments.append(FleetAssignment(
                job_file=file_name,
                recommended_printer="",
                reason=f"No printer found with {mt} and {required:.0f}g available",
                material_match=False,
                remaining_after_print_grams=None,
            ))

    return assignments


def suggest_spool_swaps(
    db: Any,
    *,
    jobs: list[dict[str, Any]],
) -> list[str]:
    """Suggest minimal spool swaps to run all queued jobs.

    Analyses which jobs need which materials, compares against what is
    currently loaded on each printer, and suggests moves to minimise the
    total number of physical spool changes.

    Each job dict should contain ``material_type`` and ``required_grams``.
    """
    if not jobs:
        return []

    all_materials = _get_all_materials(db)

    # What each printer currently has loaded (tool_index 0)
    loaded: dict[str, dict[str, Any]] = {}
    for mat in all_materials:
        if mat.get("tool_index", 0) == 0:
            loaded[mat["printer_name"]] = mat

    # Aggregate demand by material type
    demand: dict[str, float] = {}
    for job in jobs:
        mt = (job.get("material_type") or "").upper()
        demand[mt] = demand.get(mt, 0.0) + job.get("required_grams", 0.0)

    # Check what's already satisfied
    satisfied: dict[str, float] = {}
    for _pn, mat in loaded.items():
        mt = (mat.get("material_type") or "").upper()
        remaining = mat.get("remaining_grams") or 0.0
        satisfied[mt] = satisfied.get(mt, 0.0) + remaining

    suggestions: list[str] = []

    for mt, needed in sorted(demand.items()):
        available = satisfied.get(mt, 0.0)
        if available >= needed:
            continue

        shortfall = needed - available

        # Find printers with different materials that could be swapped
        # and spools on the shelf that have this material
        spools = db.list_spools()
        loaded_spool_ids = {
            m.get("spool_id") for m in all_materials if m.get("spool_id")
        }

        shelf_spools = []
        for spool in spools:
            spool_mt = (spool.get("material_type") or "").upper()
            if spool_mt != mt:
                continue
            if spool["id"] in loaded_spool_ids:
                continue
            rem = spool.get("remaining_grams", 0.0) or 0.0
            if rem > 0:
                shelf_spools.append(spool)

        # Sort shelf spools by remaining grams descending
        shelf_spools.sort(
            key=lambda s: s.get("remaining_grams", 0.0), reverse=True
        )

        # Find printers that don't have a demand for their current material
        # (good candidates for swapping)
        swap_candidates: list[str] = []
        for pn, mat in loaded.items():
            current_mt = (mat.get("material_type") or "").upper()
            if current_mt == mt:
                continue
            if current_mt not in demand:
                swap_candidates.append(pn)

        for spool in shelf_spools:
            if shortfall <= 0:
                break
            rem = spool.get("remaining_grams", 0.0) or 0.0
            brand = spool.get("brand", "")
            color = spool.get("color", "")
            label = " ".join(filter(None, [brand, color, mt]))

            if swap_candidates:
                target = swap_candidates.pop(0)
                suggestions.append(
                    f"Load {label} spool ({rem:.0f}g) onto {target}"
                )
                shortfall -= rem
            else:
                suggestions.append(
                    f"Swap a printer to {label} spool ({rem:.0f}g) — "
                    f"no idle slot identified"
                )
                shortfall -= rem

    return suggestions
