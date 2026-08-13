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
            material: str,
            quality: str | None = None,
            priority: str | None = None,
        ) -> dict:
            """Route a print job to the best available printer in the fleet.

            Scores each registered printer on material match, availability,
            queue depth, and historical success rate, then recommends the
            best assignment with scored alternatives.

            Args:
                file_path: Path to the file to print.
                material: Required filament material (e.g. "PLA", "PETG").
                quality: Quality preference — "draft", "standard", or "fine".
                priority: Job urgency — "low", "normal", or "high".
            """
            if err := _srv._check_auth("print"):
                return err

            if not material or not material.strip():
                return _srv._error_dict(
                    "material is required — routing scores printers on what "
                    "they can run, so it cannot recommend one without knowing "
                    "the material.",
                    code="INVALID_INPUT",
                )

            # Imported outside the try below: if kiln-pro (which provides
            # kiln.job_router) is absent, the handler names must still
            # resolve — and the caller gets a clear answer, not a
            # laundered ImportError.
            try:
                from kiln.cli.main import _collect_routing_candidates
                from kiln.job_router import (
                    RoutingCriteria,
                    RoutingValidationError,
                    get_job_router,
                )
            except ImportError:
                return _srv._error_dict(
                    "Fleet routing requires kiln-pro, which is not installed "
                    "on this server.",
                    code="ROUTING_UNAVAILABLE",
                )

            try:
                import os

                from kiln.queue import JobStatus

                registry = _srv._get_registry()
                names = registry.list_names()
                if not names:
                    return _srv._error_dict(
                        "No printers registered. Register printers before "
                        "routing jobs across a fleet.",
                        code="NO_PRINTERS",
                    )

                # Per-printer pending counts feed the router's wait
                # estimates; the same numbers queue_summary reports.
                pending: dict[str, int] = {}
                for job in _srv._get_queue().list_jobs(status=JobStatus.QUEUED):
                    if job.printer_name:
                        pending[job.printer_name] = pending.get(job.printer_name, 0) + 1

                # Same candidate builder the CLI's routing path uses —
                # one engine, two doors — fed from the live registry
                # instead of on-disk printer configs.
                adapters = {name: registry.get(name) for name in names}
                candidates = _collect_routing_candidates(
                    adapters=adapters,
                    material=material,
                    pending_counts=pending,
                    file_extension=os.path.splitext(file_path)[1],
                )
                if not candidates:
                    return _srv._error_dict(
                        "No registered printer can accept this file type.",
                        code="NO_ELIGIBLE_PRINTERS",
                    )

                # quality picks how much the score favours reliability;
                # priority picks how much it favours getting started fast.
                # Both map onto the router's 1-5 weight knobs, defaulting
                # to its neutral 3.
                criteria = RoutingCriteria(
                    material=material.strip(),
                    quality_priority={"draft": 1, "standard": 3, "fine": 5}.get(
                        (quality or "standard").lower(), 3
                    ),
                    speed_priority={"low": 1, "normal": 3, "high": 5}.get(
                        (priority or "normal").lower(), 3
                    ),
                )
                result = get_job_router().route_job(criteria, candidates)
                return {"success": True, "routing": result.to_dict()}
            except RoutingValidationError as exc:
                # A recommendation the engine cannot back with scores is
                # not downgraded to a guess — the refusal carries why.
                return _srv._error_dict(str(exc), code="ROUTING_ERROR")
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
            idempotency_key: str | None = None,
        ) -> dict:
            """Submit a print job to the fleet orchestrator.

            If no printer is specified, the orchestrator auto-assigns to the best
            available printer. Tracks the job through completion.

            Args:
                file_path: Path to the file to print.
                printer_name: Specific printer to assign to (auto-routes if None).
                material: Required filament material.
                priority: Job priority (low, normal, high).
                idempotency_key: Optional opaque key (e.g. a UUID you
                    generate) naming this one submission.  If the call
                    fails in a way where you cannot tell whether the job
                    was queued, retry with the SAME key to get the
                    original job back (``submission: "replayed"``)
                    instead of queuing a duplicate print.  Use a new key
                    for each job you genuinely want printed.
            """
            if err := _srv._check_auth("print"):
                return err

            # Import outside the try: this is public Kiln's own module,
            # and the handler below must be resolvable even when the
            # kiln-pro import inside the try fails.
            from kiln.queue import IdempotencyConflict

            try:
                from kiln.fleet_orchestrator import get_fleet_orchestrator

                orch = get_fleet_orchestrator()
                # The orchestrator schedules on an integer priority
                # (higher = more urgent); this tool speaks the
                # low/normal/high vocabulary the apps present.
                priority_rank = {"low": -1, "normal": 0, "high": 1}.get(
                    (priority or "normal").lower(), 0
                )
                job, replayed = orch.submit_job_result(
                    file_path,
                    submitted_by="mcp-agent",
                    priority=priority_rank,
                    preferred_printer=printer_name,
                    metadata={"material": material} if material else None,
                    idempotency_key=idempotency_key,
                )
                return {
                    "success": True,
                    "job": job.to_dict(),
                    "submission": "replayed" if replayed else "queued",
                    **(
                        {
                            "message": (
                                f"Job {job.job_id} was already submitted with "
                                "this idempotency key. No duplicate was queued."
                            )
                        }
                        if replayed
                        else {}
                    ),
                }
            except AttributeError:
                # An older kiln-pro build predates submit_job_result.
                # Refuse honestly rather than guessing at its contract.
                _logger.exception("fleet orchestrator is older than this Kiln release")
                return _srv._error_dict(
                    "The installed kiln-pro is older than this Kiln release "
                    "and cannot accept fleet submissions from it. Upgrade "
                    "kiln-pro to matching versions.",
                    code="FLEET_VERSION_MISMATCH",
                )
            except IdempotencyConflict as exc:
                return _srv._error_dict(
                    f"Idempotency key already used by job {exc.existing_job_id!r} "
                    "with different parameters (file, printer, or priority). "
                    "Retries must repeat the original submission exactly; a new "
                    "job needs a new key.",
                    code="IDEMPOTENCY_CONFLICT",
                )
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
