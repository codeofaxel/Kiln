"""Material inventory tools plugin — fleet tracking, forecasting, assignment.

Registers MCP tools for fleet-wide material inventory management:
consumption tracking, stock forecasting, sufficiency checks, restock
suggestions, printer-material lookup, fleet job assignment, and spool
swap optimisation.

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
                from kiln.material_inventory import (
                    get_fleet_material_summary as _summary,
                )
                from kiln.persistence import get_db

                db = get_db()
                results = _summary(db)
                return {
                    "success": True,
                    "summary": [r.to_dict() for r in results],
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
                from kiln.material_inventory import get_consumption_history
                from kiln.persistence import get_db

                db = get_db()
                results = get_consumption_history(db, days=days)
                return {
                    "success": True,
                    "history": [r.to_dict() for r in results],
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
                from kiln.material_inventory import forecast_consumption
                from kiln.persistence import get_db

                db = get_db()
                forecast = forecast_consumption(
                    db,
                    material_type=material_type,
                    days_ahead=days_ahead,
                )
                return {"success": True, "forecast": forecast.to_dict()}
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

            When material is insufficient, generates actionable suggestions
            including alternative printers with enough stock, shelf spool
            availability, pause-and-swap hints, and purchase links.

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
                from kiln.persistence import get_db

                db = get_db()
                check = _check(
                    db,
                    printer_name=printer_name,
                    required_grams=required_grams,
                    material_type=material_type,
                )
                return {"success": True, "check": check.to_dict()}
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
                from kiln.material_inventory import (
                    get_restock_suggestions as _restock,
                )
                from kiln.persistence import get_db

                db = get_db()
                suggestions = _restock(db)
                return {
                    "success": True,
                    "suggestions": [s.to_dict() for s in suggestions],
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
                from kiln.material_inventory import (
                    find_printers_with_material as _find,
                )
                from kiln.persistence import get_db

                db = get_db()
                results = _find(
                    db,
                    material_type=material_type,
                    color=color,
                    min_grams=min_grams,
                )
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
            ``required_grams``, and optionally ``color``.  Returns optimal
            printer assignments that minimise spool swaps and prefer
            colour matches.

            Args:
                jobs: List of job dicts to assign.
            """
            import kiln.server as _srv

            try:
                from kiln.material_inventory import (
                    optimize_fleet_assignment as _assign,
                )
                from kiln.persistence import get_db

                db = get_db()
                assignments = _assign(db, jobs=jobs)
                return {
                    "success": True,
                    "assignments": [a.to_dict() for a in assignments],
                    "count": len(assignments),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in optimize_fleet_assignment")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def suggest_spool_swaps(jobs: list[dict[str, Any]]) -> dict:
            """Suggest minimal spool swaps to run all queued jobs.

            Analyses which jobs need which materials, compares against
            what is currently loaded on each printer, and suggests the
            fewest physical spool changes needed.

            Args:
                jobs: List of job dicts with ``material_type`` and ``required_grams``.
            """
            import kiln.server as _srv

            try:
                from kiln.material_inventory import suggest_spool_swaps as _swaps
                from kiln.persistence import get_db

                db = get_db()
                suggestions = _swaps(db, jobs=jobs)
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
