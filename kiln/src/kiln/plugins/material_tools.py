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


#: How a Bambu names a tray in ``tray_now`` / ``tray_pre`` / ``tray_tar`` is
#: decided in :mod:`kiln.bambu_trays` (unit 1's first tray is 4, an AMS HT's
#: only tray is its unit id, 254 the external spool, 255 no tray).  The
#: trays ``get_ams_status`` reports carry their unit's OWN slot id, so a
#: reading is resolved to ``(unit, slot)`` before it is matched.


def _feeding_tray(value: Any) -> tuple[int, int] | None:
    """``(unit, slot)`` when *value* names a tray on a unit; ``None`` otherwise."""
    from kiln.bambu_trays import read_tray_id

    ref = read_tray_id(value)
    if ref is None or not ref.loaded_tray:
        return None
    return (ref.unit, ref.slot)


def _is_external_spool(value: Any) -> bool:
    from kiln.bambu_trays import read_tray_id

    ref = read_tray_id(value)
    return ref is not None and ref.external


def _report_feeding(ams: dict[str, Any]) -> tuple[Any, str]:
    """``(the id the report says feeds, which field said so)``.

    The adapter's ``feeding`` record wins; a report without the key is
    read from ``tray_now``.  The id is the printer's own tray id, 254 for
    the external spool, or ``None``.
    """
    if "feeding" in ams:
        feeding = ams.get("feeding")
        if isinstance(feeding, dict):
            return feeding.get("tray_id"), str(feeding.get("source") or "feeding")
        return None, "feeding"
    return ams.get("tray_now"), "tray_now"


def _iter_ams_trays(ams: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """``(unit_id, tray)`` for every tray the reading carries, in order."""
    trays: list[tuple[int, dict[str, Any]]] = []
    units = ams.get("units", [])
    if not isinstance(units, list):
        return trays
    for position, unit in enumerate(units):
        if not isinstance(unit, dict):
            continue
        try:
            unit_id = int(unit.get("unit_id", position))
        except (TypeError, ValueError):
            unit_id = position
        raw_trays = unit.get("trays", [])
        if not isinstance(raw_trays, list):
            continue
        for tray in raw_trays:
            if isinstance(tray, dict):
                trays.append((unit_id, tray))
    return trays


def _loaded_ams_trays(ams: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (unit_id, tray)
        for unit_id, tray in _iter_ams_trays(ams)
        if str(tray.get("tray_type", "") or "").strip()
    ]


def _find_tray(
    trays: list[tuple[int, dict[str, Any]]], unit: int, slot: int,
) -> dict[str, Any] | None:
    for unit_id, tray in trays:
        try:
            tray_slot = int(tray.get("slot", -1))
        except (TypeError, ValueError):
            continue
        if unit_id == unit and tray_slot == slot:
            return tray
    return None


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
            """Get the filament physically active in the AMS hardware right now (Bambu Lab).

            Reads live tray data from the AMS hardware. For the software-tracked
            material (what was told to Kiln via ``set_material``), use
            ``get_material`` instead.

            For Bambu Lab printers with an AMS, reads the active tray and
            returns its type, colour, remaining percentage, and temperature
            range.  For non-Bambu printers (or printers without AMS), the
            material is reported as ``"unknown"``.

            ``tray_now`` is the printer's own tray id (``unit * 4 + slot``
            on a chained unit, the unit id itself on an AMS HT); ``254``
            is the external spool and ``255`` no tray.  An A1 / AMS Lite
            can report ``255`` with trays loaded; then Kiln falls back to
            the selected/target tray fields or returns the loaded
            candidates instead of claiming the external spool.

            Args:
                printer_name: Named printer to query.  Omit to use the
                    default printer.
            """
            import kiln.server as _srv
            from kiln.printers.base import PrinterError
            from kiln.registry import PrinterNotFoundError

            try:
                adapter = _srv._resolve_adapter(printer_name)
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

            from kiln.bambu_trays import describe_tray_id, tray_id, tray_name

            tray_now: str = str(ams.get("tray_now", "255")).strip()
            all_trays = _iter_ams_trays(ams)
            loaded_trays = _loaded_ams_trays(ams)

            feeding_id, active_source = _report_feeding(ams)
            if _is_external_spool(feeding_id):
                return {
                    "success": True,
                    "material": "unknown",
                    "source": "external_spool",
                    "tray_now": tray_now,
                    "message": "Active material unknown — the external spool is feeding (no RFID/AMS data).",
                }
            active = _feeding_tray(feeding_id)
            # The legacy fields (tray_now, tray_pre, tray_tar) are trusted
            # only when the report's own feeding field did not speak.
            block_spoke = str(ams.get("feeding_source") or "") == "extruder"
            if active is None and not block_spoke:
                for field in ("active_tray", "tray_pre", "tray_tar"):
                    candidate = _feeding_tray(ams.get(field))
                    if candidate is None:
                        continue
                    if _find_tray(loaded_trays, *candidate) is not None:
                        active = candidate
                        active_source = field
                        break
            nothing_feeding = active is None and (block_spoke or tray_now == "255")

            if nothing_feeding and loaded_trays:
                materials = sorted({
                    str(tray.get("tray_type", "") or "").strip()
                    for _unit, tray in loaded_trays
                    if str(tray.get("tray_type", "") or "").strip()
                })
                colors = [
                    str(tray.get("tray_color", "") or "").strip()
                    for _unit, tray in loaded_trays
                    if str(tray.get("tray_color", "") or "").strip()
                ]
                # The printer's own ids, and Studio's names for them, so a
                # caller can pass one straight to start_print's ams_mapping.
                loaded_slots: list[int] = []
                loaded_slot_names: list[str] = []
                for unit_id, tray in loaded_trays:
                    try:
                        loaded_slots.append(tray_id(unit_id, int(tray.get("slot", 0))))
                        loaded_slot_names.append(tray_name(unit_id, int(tray.get("slot", 0))))
                    except (TypeError, ValueError):
                        continue
                material = materials[0] if len(materials) == 1 else "unknown"
                result: dict[str, Any] = {
                    "success": True,
                    "material": material,
                    "source": "ams_loaded_unknown_slot",
                    "tray_now": tray_now,
                    "loaded_slots": loaded_slots,
                    "loaded_slot_names": loaded_slot_names,
                    "candidate_materials": materials,
                    "message": (
                        "AMS trays are loaded, but the printer did not report "
                        "a current slot. A1/AMS Lite firmware can report "
                        "tray_now=255 in this state; pass an explicit "
                        "ams_mapping to start_print when color matters."
                    ),
                }
                if colors:
                    result["candidate_colors"] = colors
                return result

            if nothing_feeding:
                # No tray feeding and none loaded: the external spool holder
                # is the only place filament can be coming from.
                return {
                    "success": True,
                    "material": "unknown",
                    "source": "external_spool",
                    "message": "Active material unknown — external spool in use (no RFID/AMS data).",
                }

            if active is None:
                return {
                    "success": True,
                    "material": "unknown",
                    "source": "unknown",
                    "message": f"Could not parse AMS tray index: {tray_now!r}.",
                }

            unit_index, tray_index = active
            slot_index = tray_id(unit_index, tray_index)
            slot_name = tray_name(unit_index, tray_index)
            tray_data = _find_tray(all_trays, unit_index, tray_index)

            if tray_data is None:
                return {
                    "success": True,
                    "material": "unknown",
                    "source": f"ams_slot_{slot_index}",
                    "active_slot": slot_index,
                    "active_slot_name": slot_name,
                    "active_unit": unit_index,
                    "active_tray": tray_index,
                    "message": (
                        f"AMS {describe_tray_id(slot_index)} is active but its tray "
                        f"data is unavailable."
                    ),
                }

            material: str = tray_data.get("tray_type", "unknown") or "unknown"
            color: str | None = tray_data.get("tray_color")
            remaining: int | None = tray_data.get("remain")
            # `remain` is a real reading only for an RFID-tagged spool —
            # the AMS has no scale.  AMS Lite and untagged spools report a
            # placeholder; the adapter flags those as remaining_known=False.
            remaining_known: bool = bool(tray_data.get("remaining_known"))
            nozzle_temp_min: int | None = tray_data.get("nozzle_temp_min")
            nozzle_temp_max: int | None = tray_data.get("nozzle_temp_max")

            # Build a human-friendly summary.
            parts: list[str] = [f"Active material: {material}"]
            if color:
                parts.append(f"color #{color}")
            if remaining is not None and remaining_known:
                parts.append(f"{remaining}% remaining")
            # "tray 5 (slot B2)": the printer's id first, Studio's name
            # beside it, so a person with two units is never told "slot 2"
            # and left to guess which unit's.
            parts.append(f"from AMS {describe_tray_id(slot_index)}")
            message = f"{', '.join(parts)}."

            # ``active_slot`` is the printer's own tray id -- the one
            # ``ams_mapping`` on start_print takes; ``active_slot_name`` is
            # what Bambu Studio calls it; ``active_unit`` / ``active_tray``
            # are the unit and its own slot.
            result: dict[str, Any] = {
                "success": True,
                "material": material,
                "source": f"ams_slot_{slot_index}",
                "active_slot": slot_index,
                "active_slot_name": slot_name,
                "active_unit": unit_index,
                "active_tray": tray_index,
                "active_slot_source": active_source,
                "message": message,
            }
            if color is not None:
                result["color"] = color
            if remaining is not None and remaining_known:
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
            from kiln.printers.base import PrinterError
            from kiln.registry import PrinterNotFoundError

            checks: dict[str, dict[str, str]] = {}
            anomalies: list[str] = []

            # ------------------------------------------------------------------
            # 1. Resolve adapter
            # ------------------------------------------------------------------
            try:
                adapter = _srv._resolve_adapter(printer_name)
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

                from kiln.printers.base import UNREACHABLE_STATES

                if not state.connected or state.state in UNREACHABLE_STATES:
                    # The remedy the adapter worked out, rather than one
                    # word for four different problems.
                    detail = state.remedy or "Printer is offline or not connected."
                    checks["printer_connected"] = {
                        "status": "critical",
                        "detail": detail,
                    }
                    anomalies.append(detail)
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

                    report = _analyze(
                        model_path,
                        material=material or "pla",
                        printer_id=printer_id or None,
                    )
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
