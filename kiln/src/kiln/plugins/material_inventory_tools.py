"""Material inventory tools plugin — stock, use, runout, sufficiency, jobs.

Registers MCP tools for material inventory: what is on hand, consumption,
runout forecasts, sufficiency checks, restock suggestions, which printer
holds a material, which spool takes each job, and spool swaps.  On an
install with one machine every one answers from this install's own records,
offline.  Across several machines the answer is Kiln Business and comes from
Kiln's servers; every door asks through one router,
:func:`kiln.material_inventory.inventory_answer`.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _MaterialInventoryToolsPlugin:
    """Material inventory tools — fleet tracking and forecasting.

    Tools:
        - get_fleet_material_summary
        - get_material_consumption_history
        - forecast_material_consumption
        - check_material_sufficiency
        - get_restock_suggestions
        - find_printers_with_material
        - optimize_fleet_assignment
        - suggest_spool_swaps
    """

    @property
    def name(self) -> str:
        return "material_inventory_tools"

    @property
    def description(self) -> str:
        return "Fleet material inventory, forecasting, and assignment tools"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register material inventory tools with the MCP server."""

        @mcp.tool()
        def get_fleet_material_summary() -> dict:
            """Aggregate material inventory across all printers and spools.

            Returns a per-material-type summary of total stock in grams,
            spool counts, which printers have it loaded, and available
            colours.
            """
            import kiln.server as _srv

            try:
                from kiln.material_inventory import inventory_answer
                from kiln.persistence import get_db

                results, refused = inventory_answer("summary", get_db(), feature="Fleet material summary")
                if refused is not None:
                    return refused
                return {
                    "success": True,
                    "summary": results,
                    "material_types": len(results),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in get_fleet_material_summary")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def get_material_consumption_history(days: int = 30) -> dict:
            """Get material consumption history from completed prints.

            Aggregates filament usage per material type over the given
            window, converting filament length to grams using standard
            material densities.

            Args:
                days: Number of days to look back (default 30).
            """
            import kiln.server as _srv

            try:
                from kiln.material_inventory import inventory_answer
                from kiln.persistence import get_db

                results, refused = inventory_answer(
                    "history", get_db(), feature="Fleet consumption history", days=days,
                )
                if refused is not None:
                    return refused
                return {
                    "success": True,
                    "history": results,
                    "period_days": days,
                    "material_types": len(results),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in get_material_consumption_history")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def forecast_material_consumption(
            material_type: str,
            days_ahead: int = 30,
        ) -> dict:
            """Forecast when a material type will run out.

            Combines current stock with historical consumption rate to
            estimate remaining days and urgency level (ok/low/critical).

            Args:
                material_type: Material type to forecast (e.g. ``"PLA"``).
                days_ahead: Days of history for rate estimation (default 30).
            """
            import kiln.server as _srv

            try:
                from kiln.material_inventory import inventory_answer
                from kiln.persistence import get_db

                forecast, refused = inventory_answer(
                    "forecast", get_db(), feature="Fleet consumption forecasting",
                    material_type=material_type, days_ahead=days_ahead,
                )
                if refused is not None:
                    return refused
                return {"success": True, "forecast": forecast}
            except Exception as exc:
                _logger.exception("Unexpected error in forecast_material_consumption")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def check_material_sufficiency(
            printer_name: str,
            required_grams: float,
            material_type: str | None = None,
        ) -> dict:
            """Check if a printer has enough material for a print job.

            When material is insufficient, says what the person can do with
            what they have: a spool on the shelf, a pause-and-swap.  Only
            when nothing they have will do does it end with one offer of
            help finding a filament.  Relay that offer as written and never
            suggest buying filament unless the person asks or says yes;
            then use ``find_closest_filaments``.  Suggestions that point at
            OTHER machines are a fleet-wide answer and need Kiln Business;
            the check itself — does THIS printer have enough — works on
            every tier.

            Args:
                printer_name: Name of the printer to check.
                required_grams: Amount of material needed in grams.
                material_type: Optional material type filter.
            """
            import kiln.server as _srv

            try:
                from kiln.material_inventory import (
                    check_material_sufficiency as _check,
                )
                from kiln.material_inventory import (
                    fleet_scope_verdict,
                    inventory_answer,
                    registered_machines,
                )
                from kiln.persistence import get_db

                db = get_db()
                # The single-machine verdict is never gated — a user must
                # always be able to ask whether the printer in front of
                # them has enough filament.  Only the cross-machine half
                # ("printer B has 800g") is the fleet answer: on Business it
                # comes from Kiln's servers through the one router, asked
                # only when this printer falls short; below it it is counted
                # here and withheld, as before.
                many = registered_machines() > 1
                withheld = many and fleet_scope_verdict("Alternative printers") is not None
                others = None
                unanswered = None
                if many and not withheld:
                    first = _check(
                        db, printer_name=printer_name, required_grams=required_grams,
                        material_type=material_type, other_machines=[],
                    )
                    others = []
                    if not first.sufficient and first.material_type:
                        others, miss = inventory_answer(
                            "alternatives", db, feature="Alternative printers",
                            material_type=first.material_type, min_grams=required_grams,
                        )
                        if miss is not None:
                            others, unanswered = [], miss
                check = _check(
                    db,
                    printer_name=printer_name,
                    required_grams=required_grams,
                    material_type=material_type,
                    other_machines=others,
                )
                payload = check.to_dict()
                if unanswered is not None and not payload.get("sufficient"):
                    payload["fleet_alternatives"] = {
                        k: unanswered[k] for k in ("error", "code", "why", "why_code", "why_detail") if k in unanswered
                    }
                if withheld:
                    hidden = len(payload.get("alternative_printers") or [])
                    payload["alternative_printers"] = []
                    payload["suggestions"] = [
                        s for s in payload.get("suggestions", [])
                        if "Printer " not in s
                    ]
                    if hidden:
                        payload["fleet_alternatives_available"] = hidden
                        payload["upgrade_hint"] = (
                            f"{hidden} other machine(s) of yours have this "
                            f"material — fleet answers are a Kiln Business "
                            f"feature (https://kiln3d.com/pricing)."
                        )
                return {"success": True, "check": payload}
            except Exception as exc:
                _logger.exception("Unexpected error in check_material_sufficiency")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def get_restock_suggestions() -> dict:
            """Find materials running low and generate purchase links.

            Examines all material types in inventory and returns restock
            suggestions for any projected to run out within 30 days, sorted
            by urgency (critical first).
            """
            import kiln.server as _srv

            try:
                from kiln.material_inventory import inventory_answer
                from kiln.persistence import get_db

                suggestions, refused = inventory_answer("restock", get_db(), feature="Fleet restock suggestions")
                if refused is not None:
                    return refused
                return {
                    "success": True,
                    "suggestions": suggestions,
                    "count": len(suggestions),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in get_restock_suggestions")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def find_printers_with_material(
            material_type: str,
            color: str | None = None,
            min_grams: float = 0.0,
        ) -> dict:
            """Find printers that have a specific material loaded.

            Returns printers with the matching material, sorted by
            remaining stock (most first).  Optionally filter by colour
            and minimum remaining grams.

            Args:
                material_type: Material type to find (e.g. ``"PLA"``).
                color: Optional colour filter.
                min_grams: Minimum remaining grams (default 0).
            """
            import kiln.server as _srv

            try:
                from kiln.material_inventory import inventory_answer
                from kiln.persistence import get_db

                results, refused = inventory_answer(
                    "find", get_db(), feature="Finding which printers hold a material",
                    material_type=material_type, color=color, min_grams=min_grams,
                )
                if refused is not None:
                    return refused
                return {
                    "success": True,
                    "printers": results,
                    "count": len(results),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in find_printers_with_material")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def optimize_fleet_assignment(jobs: list[dict[str, Any]]) -> dict:
            """Assign print jobs to printers by material availability.

            Each job dict should contain ``file_name``, ``material_type``,
            ``required_grams``, and optionally ``color``.  On one machine it
            says which jobs its loaded spool can take, in order; across
            several machines it picks a printer for each job, preferring
            colour matches and fewer spool swaps (Kiln Business,
            https://kiln3d.com/pricing).

            Args:
                jobs: List of job dicts to assign.
            """
            import kiln.server as _srv

            try:
                from kiln.material_inventory import inventory_answer
                from kiln.persistence import get_db

                assignments, refused = inventory_answer(
                    "assign", get_db(), feature="Fleet job assignment", jobs=jobs,
                )
                if refused is not None:
                    return refused
                return {
                    "success": True,
                    "assignments": assignments,
                    "count": len(assignments),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in optimize_fleet_assignment")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def suggest_spool_swaps(jobs: list[dict[str, Any]]) -> dict:
            """Suggest minimal spool swaps to run all queued jobs.

            Compares what the jobs need with what is loaded and says which
            shelf spools to load, and where.  On one machine it answers for
            that machine; across several machines it plans the fewest
            physical spool changes between them (Kiln Business,
            https://kiln3d.com/pricing).

            Args:
                jobs: List of job dicts with ``material_type`` and ``required_grams``.
            """
            import kiln.server as _srv

            try:
                from kiln.material_inventory import inventory_answer
                from kiln.persistence import get_db

                suggestions, refused = inventory_answer(
                    "swaps", get_db(), feature="Fleet spool-swap planning", jobs=jobs,
                )
                if refused is not None:
                    return refused
                return {
                    "success": True,
                    "swap_suggestions": suggestions,
                    "count": len(suggestions),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in suggest_spool_swaps")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        _logger.debug("Registered material inventory tools")


plugin = _MaterialInventoryToolsPlugin()
