"""Material and print-health tools plugin.

Provides MCP tools for inspecting the active filament loaded in the printer
and performing a single-shot health assessment of an in-progress print.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _MaterialToolsPlugin:
    """Material inspection and print-health tools.

    Tools:
        - get_active_material
        - check_print_health
    """

    @property
    def name(self) -> str:
        return "material_tools"

    @property
    def description(self) -> str:
        return "Material inspection and single-shot print health check tools"

    def register(self, mcp: Any) -> None:
        """Register material tools with the MCP server."""

        @mcp.tool()
        def get_active_material(
            printer_name: str | None = None,
        ) -> dict:
            """Return the filament currently loaded and active on the printer.

            For Bambu Lab printers with an AMS, reads the active tray and
            returns its type, colour, remaining percentage, and temperature
            range.  For non-Bambu printers (or printers without AMS), the
            material is reported as ``"unknown"``.

            ``tray_now == "255"`` means an external spool is in use rather
            than an AMS slot.

            Args:
                printer_name: Named printer to query.  Omit to use the
                    default printer.
            """
            import kiln.server as _srv
            from kiln.printers.base import PrinterError
            from kiln.registry import PrinterNotFoundError

            try:
                if printer_name:
                    adapter = _srv._registry.get(printer_name)
                else:
                    adapter = _srv._get_adapter()
            except PrinterNotFoundError:
                return _srv._error_dict(
                    f"Printer '{printer_name}' not found in registry.",
                    code="NOT_FOUND",
                )
            except Exception as exc:
                return _srv._error_dict(
                    f"Could not connect to printer: {exc}",
                    code="CONNECTION_ERROR",
                )

            if not hasattr(adapter, "get_ams_status"):
                return {
                    "success": True,
                    "material": "unknown",
                    "source": "unknown",
                    "message": "Active material unknown — printer does not support AMS status queries.",
                }

            try:
                ams = adapter.get_ams_status()
            except PrinterError as exc:
                return _srv._error_dict(
                    f"Failed to query AMS status: {exc}",
                    code="PRINTER_ERROR",
                )
            except Exception as exc:
                _logger.exception("Unexpected error querying AMS status")
                return _srv._error_dict(
                    f"Unexpected error querying AMS status: {exc}",
                    code="INTERNAL_ERROR",
                )

            tray_now: str = str(ams.get("tray_now", "255"))

            if tray_now == "255":
                return {
                    "success": True,
                    "material": "unknown",
                    "source": "external_spool",
                    "message": "Active material unknown — external spool in use (no RFID/AMS data).",
                }

            # Locate the matching tray in the first AMS unit.
            try:
                slot_index = int(tray_now)
            except (ValueError, TypeError):
                return {
                    "success": True,
                    "material": "unknown",
                    "source": "unknown",
                    "message": f"Could not parse AMS tray index: {tray_now!r}.",
                }

            units: list[dict[str, Any]] = ams.get("units", [])
            tray_data: dict[str, Any] | None = None
            if units:
                for tray in units[0].get("trays", []):
                    if tray.get("slot") == slot_index:
                        tray_data = tray
                        break

            if tray_data is None:
                return {
                    "success": True,
                    "material": "unknown",
                    "source": f"ams_slot_{slot_index}",
                    "message": f"AMS slot {slot_index} is active but tray data is unavailable.",
                }

            material: str = tray_data.get("tray_type", "unknown") or "unknown"
            color: str | None = tray_data.get("tray_color")
            remaining: int | None = tray_data.get("remain")
            nozzle_temp_min: int | None = tray_data.get("nozzle_temp_min")
            nozzle_temp_max: int | None = tray_data.get("nozzle_temp_max")

            # Build a human-friendly summary.
            parts: list[str] = [f"Active material: {material}"]
            if color:
                parts.append(f"color #{color}")
            if remaining is not None:
                parts.append(f"{remaining}% remaining")
            parts.append(f"from AMS slot {slot_index}")
            message = f"{', '.join(parts)}."

            result: dict[str, Any] = {
                "success": True,
                "material": material,
                "source": f"ams_slot_{slot_index}",
                "message": message,
            }
            if color is not None:
                result["color"] = color
            if remaining is not None:
                result["remaining_percent"] = remaining
            if nozzle_temp_min is not None and nozzle_temp_max is not None:
                result["nozzle_temp_range"] = [nozzle_temp_min, nozzle_temp_max]

            return result

        @mcp.tool()
        def check_print_health(
            printer_name: str | None = None,
            model_path: str | None = None,
            material: str | None = None,
            printer_id: str | None = None,
        ) -> dict:
            """Perform a single-shot health assessment of the current print.

            Unlike ``watch_print`` (which starts a background monitoring
            thread), this tool runs one check cycle and returns immediately.
            It is designed for quick "is the print OK right now?" queries
            from an agent without starting persistent background tasks.

            Checks performed:

            * **Printer connectivity** — is the printer online?
            * **Temperature** — are hot-end and bed within 15 °C of target?
            * **Print progress** — current completion, layer count, ETA.
            * **Error state** — any active firmware error codes.

            If *model_path* is supplied, adhesion risk is also evaluated via
            ``analyze_printability``.

            Args:
                printer_name: Named printer to query.  Omit for the default.
                model_path: Optional path to the model being printed.
                    Enables geometry-based adhesion risk analysis.
                material: Filament material (e.g. ``"PLA"``, ``"ABS"``).
                    Passed to adhesion analysis when *model_path* is provided.
                printer_id: Printer model ID (e.g. ``"bambu_a1"``).
                    Used for printer-intelligence lookups.
            """
            import kiln.server as _srv
            from kiln.printers.base import PrinterError, PrinterStatus
            from kiln.registry import PrinterNotFoundError

            checks: dict[str, dict[str, str]] = {}
            anomalies: list[str] = []

            # ------------------------------------------------------------------
            # 1. Resolve adapter
            # ------------------------------------------------------------------
            try:
                if printer_name:
                    adapter = _srv._registry.get(printer_name)
                else:
                    adapter = _srv._get_adapter()
            except PrinterNotFoundError:
                return _srv._error_dict(
                    f"Printer '{printer_name}' not found in registry.",
                    code="NOT_FOUND",
                )
            except Exception as exc:
                return _srv._error_dict(
                    f"Could not resolve printer adapter: {exc}",
                    code="CONNECTION_ERROR",
                )

            # ------------------------------------------------------------------
            # 2. Printer state
            # ------------------------------------------------------------------
            state_dict: dict[str, Any] | None = None
            try:
                state = adapter.get_state()
                state_dict = state.to_dict()

                if not state.connected or state.state == PrinterStatus.OFFLINE:
                    checks["printer_connected"] = {
                        "status": "critical",
                        "detail": "Printer is offline or not connected.",
                    }
                    anomalies.append("Printer is offline.")
                else:
                    checks["printer_connected"] = {
                        "status": "ok",
                        "detail": f"Connected — state: {state.state.value}.",
                    }

                # Temperature check
                tool_actual = state.tool_temp_actual
                tool_target = state.tool_temp_target
                bed_actual = state.bed_temp_actual
                bed_target = state.bed_temp_target

                temp_parts: list[str] = []
                temp_status = "ok"

                if tool_actual is not None and tool_target is not None:
                    temp_parts.append(f"Tool: {tool_actual:.0f}/{tool_target:.0f} °C")
                    if abs(tool_actual - tool_target) > 15:
                        temp_status = "warning"
                        anomalies.append(
                            f"Tool temperature drift: {tool_actual:.0f} °C vs target {tool_target:.0f} °C."
                        )
                elif tool_actual is not None:
                    temp_parts.append(f"Tool: {tool_actual:.0f} °C (no target set)")

                if bed_actual is not None and bed_target is not None:
                    temp_parts.append(f"Bed: {bed_actual:.0f}/{bed_target:.0f} °C")
                    if abs(bed_actual - bed_target) > 15:
                        temp_status = "warning"
                        anomalies.append(
                            f"Bed temperature drift: {bed_actual:.0f} °C vs target {bed_target:.0f} °C."
                        )
                elif bed_actual is not None:
                    temp_parts.append(f"Bed: {bed_actual:.0f} °C (no target set)")

                checks["temperature"] = {
                    "status": temp_status,
                    "detail": ", ".join(temp_parts) if temp_parts else "No temperature data available.",
                }

                # Error state check
                if state.print_error is not None and state.print_error != 0:
                    checks["error_state"] = {
                        "status": "critical",
                        "detail": f"Firmware error code: {state.print_error}.",
                    }
                    anomalies.append(f"Firmware error active: code {state.print_error}.")
                else:
                    checks["error_state"] = {
                        "status": "ok",
                        "detail": "No errors reported.",
                    }

            except PrinterError as exc:
                checks["printer_connected"] = {
                    "status": "critical",
                    "detail": f"Failed to read printer state: {exc}",
                }
                anomalies.append(f"Could not read printer state: {exc}")

            # ------------------------------------------------------------------
            # 3. Job progress
            # ------------------------------------------------------------------
            job_dict: dict[str, Any] | None = None
            try:
                job = adapter.get_job()
                job_dict = job.to_dict()

                progress_parts: list[str] = []
                if job.completion is not None:
                    progress_parts.append(f"{job.completion:.1f}% complete")
                if job.current_layer is not None and job.total_layers is not None:
                    progress_parts.append(f"layer {job.current_layer}/{job.total_layers}")
                if job.print_time_left_seconds is not None:
                    minutes_left = job.print_time_left_seconds // 60
                    hours_left, mins_left = divmod(minutes_left, 60)
                    if hours_left:
                        progress_parts.append(f"~{hours_left}h {mins_left}m remaining")
                    else:
                        progress_parts.append(f"~{mins_left}m remaining")
                if job.file_name:
                    progress_parts.append(f"file: {job.file_name}")

                checks["progress"] = {
                    "status": "ok",
                    "detail": ", ".join(progress_parts) if progress_parts else "No active job.",
                }

            except PrinterError as exc:
                checks["progress"] = {
                    "status": "warning",
                    "detail": f"Could not read job progress: {exc}",
                }

            # ------------------------------------------------------------------
            # 4. Optional: adhesion risk from model geometry
            # ------------------------------------------------------------------
            if model_path:
                try:
                    from kiln.printability import analyze_printability as _analyze

                    report = _analyze(model_path)
                    if report.bed_adhesion is not None:
                        risk = report.bed_adhesion.adhesion_risk
                        adhesion_status = "ok" if risk == "low" else ("warning" if risk == "medium" else "critical")
                        checks["adhesion_risk"] = {
                            "status": adhesion_status,
                            "detail": (
                                f"Bed adhesion risk: {risk} "
                                f"(contact area: {report.bed_adhesion.contact_percentage:.1f}%)."
                            ),
                        }
                        if risk in ("medium", "high"):
                            anomalies.append(f"Adhesion risk is {risk} for this model geometry.")
                    else:
                        checks["adhesion_risk"] = {
                            "status": "ok",
                            "detail": "Adhesion analysis not available for this model.",
                        }
                except Exception as exc:
                    _logger.debug("Could not analyze model adhesion: %s", exc)
                    checks["adhesion_risk"] = {
                        "status": "warning",
                        "detail": f"Model analysis failed: {exc}",
                    }

            # ------------------------------------------------------------------
            # 5. Aggregate overall health
            # ------------------------------------------------------------------
            statuses = {c["status"] for c in checks.values()}
            if "critical" in statuses:
                health = "critical"
            elif "warning" in statuses:
                health = "warning"
            else:
                health = "healthy"

            # Build summary message.
            if health == "healthy":
                job_progress = job_dict.get("completion") if job_dict else None
                if job_progress is not None:
                    message = f"Print is healthy: {job_progress:.1f}% complete, temperatures nominal."
                else:
                    message = "Print is healthy: all checks passed."
            elif health == "warning":
                message = f"Print has warnings: {'; '.join(anomalies)}"
            else:
                message = f"Print is in a critical state: {'; '.join(anomalies)}"

            result: dict[str, Any] = {
                "success": True,
                "health": health,
                "checks": checks,
                "anomalies": anomalies,
                "message": message,
            }
            if state_dict is not None:
                result["printer_state"] = state_dict
            if job_dict is not None:
                result["job_progress"] = job_dict

            return result

        _logger.debug("Registered material tools")


plugin = _MaterialToolsPlugin()
