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


def _survey_plates(
    file_path: str, names: list[str], adapters: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any] | None]:
    """``(plates, None)`` -- one plate block per named printer from kiln-pro's
    fleet survey -- or ``(None, error_dict)``.

    The survey is kiln-pro's (``kiln.placement_fleet.for_fleet``): the
    single-machine placement verdict asked once per machine, each plate read
    as Kiln reads its own candidates.  This door reads the job's envelope
    from the file (public Kiln's own reader) and hands it over; the tier
    gate is the survey's, one gate, and its refusal is relayed as it is.
    """
    import kiln.server as _srv
    from kiln import _pro_placement_bridge as bridge

    try:
        from kiln.placement_fleet import for_fleet
    except ImportError:
        return None, _srv._error_dict(
            "Fleet plate placement requires kiln-pro, which is not installed on this server "
            "or is older than this Kiln release.",
            code="ROUTING_UNAVAILABLE",
        )
    survey = for_fleet(bridge.job_envelope(file_path), names, adapters=adapters)
    if not isinstance(survey, dict) or not survey.get("ok"):
        gate = survey.get("gate") if isinstance(survey, dict) else None
        if isinstance(gate, dict):
            return None, dict(gate)
        return None, _srv._error_dict("Kiln could not survey the fleet's plates.", code="ROUTING_ERROR")
    plates = survey.get("plates")
    return (dict(plates) if isinstance(plates, dict) else {}), None


def _no_room_refusal(plates: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The refusal when no surveyed plate has room, with each machine's own
    sentences; ``None`` when at least one does."""
    import kiln.server as _srv

    if not plates or any(isinstance(b, dict) and b.get("has_room") for b in plates.values()):
        return None
    per_machine = {
        name: list(block.get("refusals") or []) or ["no room for this part on the plate as it stands"]
        for name, block in plates.items() if isinstance(block, dict)
    }
    rows = "; ".join(f"{name}: {' '.join(why)}" for name, why in per_machine.items())
    resp = _srv._error_dict(
        f"No printer in the fleet can take this print as the plates stand -- {rows}. Nothing was queued.",
        code="NO_ROOM",
    )
    resp["per_machine"] = per_machine
    resp["plates"] = {name: dict(block) for name, block in plates.items() if isinstance(block, dict)}
    return resp


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

            Requires Business license.
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
                from kiln.printers.base import row_run_state, status_is_occupied

                grouped = _srv._get_registry().get_fleet_status_by_site()
                result = {}
                for site, statuses in grouped.items():
                    result[site] = {
                        "printers": statuses,
                        "count": len(statuses),
                        "idle": [p["name"] for p in statuses if str(p.get("state", "")).lower() == "idle"],
                        # Through the shared classifier, so this listing
                        # cannot drift from the rest of the product about
                        # which states mean "not free to take work" — a
                        # "stale" machine is busy here, not idle.
                        "busy": [
                            p["name"]
                            for p in statuses
                            if status_is_occupied(row_run_state(p))
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
            best assignment with scored alternatives.  Every candidate also
            carries a ``plate`` block -- whether the plate as it stands has
            room for this part and how a print there would start.  A machine
            with no room, or a plate Kiln has no record of, is never
            recommended; when none has room the answer is a refusal
            (``NO_ROOM``) with each machine's own sentence.  A print that
            would start the quiet way beside a part still on the plate is
            scored lower unless ``priority`` is high.

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

            # Public Kiln's own module — a failure here is a real bug and
            # must not be reported as "kiln-pro is missing".
            from kiln.routing_candidates import collect_routing_candidates

            # kiln.job_router is provided by kiln-pro.  Imported outside
            # the main try so its absence gets a clear answer rather than
            # a laundered ImportError, and so the handler names below
            # still resolve.
            try:
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
            # The same rule for the plate survey: kiln-pro's, or an honest
            # refusal.  Never a routing answer that ignored the plates.
            try:
                import kiln.placement_fleet  # noqa: F401
            except ImportError:
                return _srv._error_dict(
                    "Fleet routing requires kiln-pro, which is not installed "
                    "on this server or is older than this Kiln release.",
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
                candidates = collect_routing_candidates(
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

                # Each candidate's plate, as it stands: the fleet survey is
                # kiln-pro's, and the router reads the block it writes.
                plates, plate_err = _survey_plates(
                    file_path, [str(c["printer_id"]) for c in candidates], adapters,
                )
                if plate_err is not None:
                    return plate_err
                for candidate in candidates:
                    candidate["plate"] = (plates or {}).get(str(candidate["printer_id"]))

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
                # not downgraded to a guess — the refusal carries why.  No
                # room anywhere is its own refusal, with each machine's
                # sentences beside it.
                per_machine = getattr(exc, "per_machine", None)
                if isinstance(per_machine, dict):
                    resp = _srv._error_dict(f"{exc}. Nothing was routed.", code="NO_ROOM")
                    resp["per_machine"] = {k: list(v) for k, v in per_machine.items()}
                    resp["plates"] = {
                        str(c["printer_id"]): c["plate"] for c in candidates if isinstance(c.get("plate"), dict)
                    }
                    return resp
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
            preview_token: str | None = None,
        ) -> dict:
            """Submit a print job to the fleet orchestrator.

            If no printer is specified, the orchestrator auto-assigns to the best
            available printer. Tracks the job through completion.

            Before anything is queued, every candidate plate is surveyed as it
            stands (the named printer's, or the whole fleet's): a job no plate
            has room for is refused here, with each machine's own sentence
            (``NO_ROOM``), never queued to be refused at the printer later.

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

            # The orchestrator starts this print unattended, so the gate
            # is here, at the door, and the sign-off rides the job for the
            # orchestrator to re-grant at dispatch.
            if block := _srv._preview_gate_error(
                "fleet_submit_job", file_path, preview_token, printer_name=printer_name,
            ):
                return block

            # The plates, before the sign-off is spent: a refusal here leaves
            # the person's yes standing for the retry that clears a plate.
            try:
                registry = _srv._get_registry()
                names = [printer_name] if printer_name else list(registry.list_names())
                adapters: dict[str, Any] = {}
                for name in names:
                    try:
                        adapters[name] = registry.get(name)
                    except Exception:  # noqa: BLE001 -- the survey says "not registered" in its own row
                        continue
            except Exception:  # noqa: BLE001
                names, adapters = ([printer_name] if printer_name else []), {}
            if names:
                plates, plate_err = _survey_plates(file_path, names, adapters)
                if plate_err is not None:
                    return plate_err
                if refusal := _no_room_refusal(plates or {}):
                    return refusal

            from kiln import print_signoff

            signoff = print_signoff.record_for(print_signoff.current())
            print_signoff.clear()
            try:
                from kiln.fleet_orchestrator import get_fleet_orchestrator

                orch = get_fleet_orchestrator()
                # The orchestrator schedules on an integer priority
                # (higher = more urgent); this tool speaks the
                # low/normal/high vocabulary the apps present.
                priority_rank = {"low": -1, "normal": 0, "high": 1}.get(
                    (priority or "normal").lower(), 0
                )
                metadata: dict = {}
                if material:
                    metadata["material"] = material
                if signoff:
                    metadata["preview_signoff"] = signoff
                job, replayed = orch.submit_job_result(
                    file_path,
                    submitted_by="mcp-agent",
                    priority=priority_rank,
                    preferred_printer=printer_name,
                    metadata=metadata or None,
                    idempotency_key=idempotency_key,
                )
                return {
                    "success": True,
                    "job": job.to_dict(),
                    "submission": "replayed" if replayed else "queued",
                    "plates": plates if names else {},
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
