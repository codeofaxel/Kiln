"""Fleet management tools plugin.

Extracts fleet-domain MCP tools from server.py into a focused plugin
module.  Tools that have cross-tool callers (``fleet_status``,
``fleet_set_speed``) remain in server.py.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _FleetToolsPlugin:
    """Fleet analytics, site grouping, routing, and orchestration tools.

    Tools:
        - fleet_analytics
        - list_fleet_sites
        - fleet_status_by_site
        - update_printer_site
        - route_print_job
        - fleet_submit_job
        - fleet_job_status
        - fleet_utilization
    """

    @property
    def name(self) -> str:
        return "fleet_tools"

    @property
    def description(self) -> str:
        return "Fleet analytics, site grouping, routing, and orchestration tools"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register fleet tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # fleet_analytics
        # ------------------------------------------------------------------

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.BUSINESS)
        def fleet_analytics() -> dict:
            """Get fleet historical analytics: per-printer success rates, utilization, job throughput.

            For live printer status (current state/temps), use ``fleet_status``.
            Returns statistics for every registered printer including total prints,
            success rate, average print duration, and total print hours.  Also
            includes fleet-wide aggregate metrics.

            Requires Kiln Pro or Business license.
            """
            try:
                if _srv._get_registry().count == 0:
                    return {
                        "success": True,
                        "printers": [],
                        "fleet_totals": {"total_prints": 0, "total_hours": 0.0, "avg_success_rate": 0.0},
                        "message": "No printers registered.",
                    }

                db = _srv.get_db()
                printer_stats = []
                total_prints = 0
                total_hours = 0.0
                success_sum = 0.0
                printers_with_data = 0

                for name in _srv._get_registry().list_names():
                    stats = db.get_printer_stats(name)
                    printer_stats.append(stats)
                    total_prints += stats["total_prints"]
                    total_hours += stats["total_print_hours"]
                    if stats["total_prints"] > 0:
                        success_sum += stats["success_rate"]
                        printers_with_data += 1

                avg_success = round(success_sum / printers_with_data, 4) if printers_with_data > 0 else 0.0

                # Queue stats
                queue_counts = _srv._get_queue().summary()

                return {
                    "success": True,
                    "printers": printer_stats,
                    "fleet_totals": {
                        "total_prints": total_prints,
                        "total_hours": round(total_hours, 2),
                        "avg_success_rate": avg_success,
                        "printer_count": _srv._get_registry().count,
                    },
                    "queue": queue_counts,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in fleet_analytics")
                return _srv._error_dict(f"Unexpected error in fleet_analytics: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # list_fleet_sites
        # ------------------------------------------------------------------

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.ENTERPRISE)
        def list_fleet_sites() -> dict:
            """List all fleet sites/locations with printer counts.

            Returns the distinct sites defined across registered printers.
            Useful for multi-site fleet dashboards.

            Requires Enterprise license.
            """
            try:
                sites = _srv._get_registry().list_sites()
                site_data = []
                for site in sites:
                    printers = _srv._get_registry().get_printers_by_site(site)
                    site_data.append({"site": site, "printer_count": len(printers), "printers": printers})
                return {"success": True, "sites": site_data, "count": len(site_data)}
            except Exception as exc:
                _logger.exception("Unexpected error in list_fleet_sites")
                return _srv._error_dict(f"Unexpected error in list_fleet_sites: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # fleet_status_by_site
        # ------------------------------------------------------------------

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.ENTERPRISE)
        def fleet_status_by_site() -> dict:
            """Get fleet status grouped by physical site/location.

            Returns printer statuses organized by site, making it easy to see
            which printers are idle, busy, or offline at each location.
            Printers without a site are grouped under ``"unassigned"``.

            Requires Enterprise license.
            """
            try:
                grouped = _srv._get_registry().get_fleet_status_by_site()
                result = {}
                for site, statuses in grouped.items():
                    result[site] = {
                        "printers": statuses,
                        "count": len(statuses),
                        "idle": [p["name"] for p in statuses if str(p.get("state", "")).lower() == "idle"],
                        "busy": [
                            p["name"]
                            for p in statuses
                            if str(p.get("state", "")).lower() in {"printing", "busy", "paused"}
                        ],
                    }
                return {"success": True, "sites": result, "site_count": len(result)}
            except Exception as exc:
                _logger.exception("Unexpected error in fleet_status_by_site")
                return _srv._error_dict(f"Unexpected error in fleet_status_by_site: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # update_printer_site
        # ------------------------------------------------------------------

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.ENTERPRISE)
        def update_printer_site(
            name: str,
            site: str,
            tags: str | None = None,
        ) -> dict:
            """Assign a printer to a physical site/location with optional tags.

            Args:
                name: Registered printer name.
                site: Physical site or location label (e.g. ``"nyc-lab"``,
                    ``"chicago-floor-2"``).
                tags: Comma-separated key=value pairs for metadata
                    (e.g. ``"building=A,floor=3,owner=team-alpha"``).

            Requires Enterprise license.
            """
            if err := _srv._check_auth("admin"):
                return err
            try:
                parsed_tags: dict[str, str] | None = None
                if tags:
                    parsed_tags = {}
                    for pair in tags.split(","):
                        pair = pair.strip()
                        if "=" in pair:
                            k, v = pair.split("=", 1)
                            parsed_tags[k.strip()] = v.strip()

                _srv._get_registry().update_printer_metadata(name, site=site, tags=parsed_tags)
                meta = _srv._get_registry().get_metadata(name)
                return {
                    "success": True,
                    "message": f"Printer {name!r} assigned to site {site!r}.",
                    "metadata": meta.to_dict(),
                }
            except _srv.PrinterNotFoundError:
                return _srv._error_dict(f"Printer {name!r} not registered.", code="NOT_FOUND")
            except Exception as exc:
                _logger.exception("Unexpected error in update_printer_site")
                return _srv._error_dict(f"Unexpected error in update_printer_site: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # route_print_job
        # ------------------------------------------------------------------

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.BUSINESS)
        def route_print_job(
            file_path: str,
            *,
            material: str | None = None,
            quality: str | None = None,
            priority: str | None = None,
        ) -> dict:
            """Route a print job to the best available printer in the fleet.

            Scores each printer based on material match, build volume, availability,
            and quality/speed preference, then recommends the optimal assignment.

            Args:
                file_path: Path to the file to print.
                material: Required filament material (e.g. "PLA", "PETG").
                quality: Quality preference — "draft", "standard", or "fine".
                priority: Job priority — "low", "normal", or "high".
            """
            if err := _srv._check_auth("print"):
                return err

            try:
                from kiln.job_router import get_job_router

                router = get_job_router()
                result = router.route_job(
                    file_path=file_path,
                    material=material,
                    quality=quality,
                    priority=priority,
                )
                return {"success": True, "routing": result.to_dict()}
            except Exception as exc:
                _logger.exception("Error in route_print_job")
                return _srv._error_dict(f"Failed to route print job: {exc}", code="ROUTING_ERROR")

        # ------------------------------------------------------------------
        # fleet_submit_job
        # ------------------------------------------------------------------

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.BUSINESS)
        def fleet_submit_job(
            file_path: str,
            *,
            printer_name: str | None = None,
            material: str | None = None,
            priority: str | None = None,
        ) -> dict:
            """Submit a print job to the fleet orchestrator.

            If no printer is specified, the orchestrator auto-assigns to the best
            available printer. Tracks the job through completion.

            Args:
                file_path: Path to the file to print.
                printer_name: Specific printer to assign to (auto-routes if None).
                material: Required filament material.
                priority: Job priority (low, normal, high).
            """
            if err := _srv._check_auth("print"):
                return err

            try:
                from kiln.fleet_orchestrator import get_fleet_orchestrator

                orch = get_fleet_orchestrator()
                job = orch.submit_job(
                    file_path=file_path,
                    printer_name=printer_name,
                    material=material,
                    priority=priority,
                )
                return {"success": True, "job": job.to_dict()}
            except Exception as exc:
                _logger.exception("Error in fleet_submit_job")
                return _srv._error_dict(f"Failed to submit fleet job: {exc}", code="FLEET_ERROR")

        # ------------------------------------------------------------------
        # fleet_job_status
        # ------------------------------------------------------------------

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.BUSINESS)
        def fleet_job_status(job_id: str) -> dict:
            """Get the status of a fleet-managed print job.

            Args:
                job_id: The orchestrated job's identifier.
            """
            try:
                from kiln.fleet_orchestrator import get_fleet_orchestrator

                orch = get_fleet_orchestrator()
                job = orch.get_job_status(job_id)
                if job is None:
                    return _srv._error_dict(f"Job {job_id!r} not found", code="NOT_FOUND")
                return {"success": True, "job": job.to_dict()}
            except Exception as exc:
                _logger.exception("Error in fleet_job_status")
                return _srv._error_dict(f"Failed to get fleet job status: {exc}", code="FLEET_ERROR")

        # ------------------------------------------------------------------
        # fleet_utilization
        # ------------------------------------------------------------------

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.BUSINESS)
        def fleet_utilization() -> dict:
            """Get fleet utilization metrics — busy/idle/offline counts and utilization %.

            Lightweight overview of fleet capacity. For full printer details, use
            ``fleet_status``. For historical analytics, use ``fleet_analytics``.
            """
            try:
                from kiln.fleet_orchestrator import get_fleet_orchestrator

                orch = get_fleet_orchestrator()
                util = orch.get_fleet_utilization()
                return {"success": True, "utilization": util}
            except Exception as exc:
                _logger.exception("Error in fleet_utilization")
                return _srv._error_dict(f"Failed to get fleet utilization: {exc}", code="FLEET_ERROR")


plugin = _FleetToolsPlugin()
