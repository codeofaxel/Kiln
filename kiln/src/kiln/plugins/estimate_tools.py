"""Estimation tools plugin — cost, time, progress, and material estimates.

Provides MCP tools for estimating print costs, time, progress, and
material usage.  Includes ``slice_and_estimate`` (slice + report without
printing) and four tools extracted from ``server.py``:

- ``estimate_cost`` — filament + electricity cost from G-code
- ``estimate_print_time`` — time/filament from model or G-code
- ``estimate_material_cost`` — material weight/cost from mesh geometry
- ``estimate_print_progress`` — phase-aware progress prediction

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRINTABLE_EXTENSIONS = {".stl", ".obj", ".3mf"}


def _format_time(seconds: int | None) -> str:
    """Convert seconds to a human-readable duration string.

    Args:
        seconds: Duration in seconds, or ``None``.

    Returns:
        A string like ``"1h 30m"``, ``"45m"``, or ``"unknown"``.
    """
    if seconds is None:
        return "unknown"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class _EstimateToolsPlugin:
    """Estimation tools — cost, time, progress, material, slice-and-estimate.

    Tools:
        - slice_and_estimate
        - estimate_cost
        - estimate_print_time
        - estimate_material_cost
        - estimate_print_progress
    """

    @property
    def name(self) -> str:
        return "estimate_tools"

    @property
    def description(self) -> str:
        return "Cost, time, progress, material, and slice-and-estimate tools"

    def register(self, mcp: Any) -> None:
        """Register estimate tools with the MCP server."""

        @mcp.tool()
        def slice_and_estimate(
            input_path: str,
            printer_id: str | None = None,
            profile: str | None = None,
            material: str = "PLA",
        ) -> dict:
            """Slice a 3D model and return estimates WITHOUT printing.

            Slices the model using PrusaSlicer or OrcaSlicer, parses the
            output G-code for time and filament metadata, runs printability
            analysis (for STL/OBJ/3MF inputs), and returns adhesion
            recommendations — all without uploading or starting a print.

            Use this tool to answer "how long will this take?" or "how much
            filament will I use?" before committing to a print job.

            Args:
                input_path: Path to the input file (STL, OBJ, 3MF, STEP, AMF).
                printer_id: Optional printer model ID for bundled profile
                    auto-selection (e.g. ``"bambu_a1"``, ``"prusa_mini"``).
                profile: Path to a slicer profile/config file (.ini or .json).
                    Takes precedence over ``printer_id`` auto-selection.
                material: Filament material for weight and adhesion estimates
                    (e.g. ``"PLA"``, ``"PETG"``, ``"ABS"``).  Default is
                    ``"PLA"``.
            """
            import kiln.server as _srv
            from kiln.gcode_metadata import extract_metadata
            from kiln.printability import (
                analyze_printability,
                is_bedslinger,
                recommend_adhesion,
            )
            from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file

            try:
                # 1. Resolve the slicer profile
                effective_printer_id, effective_profile = _srv._resolve_slice_profile_context(
                    profile=profile,
                    printer_id=printer_id,
                )

                # 2. Slice the model
                result = slice_file(input_path, profile=effective_profile)

                # 3. Parse gcode metadata
                meta = None
                if result.output_path and os.path.isfile(result.output_path):
                    try:
                        meta = extract_metadata(result.output_path)
                    except Exception as exc:
                        _logger.debug("Could not extract gcode metadata: %s", exc)

                # 4. Build estimate dict
                mat_upper = material.upper() if material else "PLA"
                filament_mm = meta.filament_used_mm if meta else None
                filament_g: float | None = None
                if filament_mm is not None:
                    filament_g = round(filament_mm * 0.003, 1)

                time_sec = meta.estimated_time_seconds if meta else None
                time_human = _format_time(time_sec)
                slicer_name = (meta.slicer if meta and meta.slicer else None) or result.slicer

                estimate: dict[str, Any] = {
                    "estimated_time_seconds": time_sec,
                    "estimated_time_human": time_human,
                    "filament_used_mm": filament_mm,
                    "filament_used_grams": filament_g,
                    "material": mat_upper,
                    "slicer": slicer_name,
                }

                # 5. Printability analysis (STL/OBJ/3MF only)
                ext = os.path.splitext(input_path)[1].lower()
                printability_dict: dict[str, Any] | None = None
                adhesion_dict: dict[str, Any] | None = None
                adhesion_rationale: str | None = None

                if ext in _PRINTABLE_EXTENSIONS:
                    try:
                        report = analyze_printability(input_path)
                        printability_dict = report.to_dict()

                        # 6. Adhesion recommendation
                        if report.bed_adhesion is not None:
                            has_enclosure = False
                            is_bs = False
                            if effective_printer_id:
                                is_bs = is_bedslinger(effective_printer_id)
                                try:
                                    from kiln.printer_intelligence import get_printer_intel

                                    intel = get_printer_intel(effective_printer_id)
                                    if intel:
                                        has_enclosure = intel.get("has_enclosure", False)
                                except Exception:
                                    pass

                            rec = recommend_adhesion(
                                report.bed_adhesion,
                                material=mat_upper,
                                has_enclosure=has_enclosure,
                                is_bedslinger_printer=is_bs,
                                model_height_mm=report.model_height_mm,
                            )
                            adhesion_dict = rec.to_dict()
                            adhesion_rationale = rec.rationale
                    except Exception as exc:
                        _logger.debug("Printability/adhesion analysis failed: %s", exc)

                # 7. Build human-readable summary message
                parts: list[str] = [f"Estimated {time_human}"]
                if filament_g is not None:
                    parts[0] += f", {filament_g}g {mat_upper}"
                if printability_dict:
                    score = printability_dict.get("score")
                    grade = printability_dict.get("grade")
                    if score is not None and grade:
                        parts.append(f"Printability: {grade} ({score}/100)")
                if adhesion_rationale:
                    parts.append(adhesion_rationale)
                message = ". ".join(parts) + "."

                # 8. Assemble response
                response: dict[str, Any] = {
                    "success": True,
                    "slice": result.to_dict(),
                    "estimate": estimate,
                    "printability": printability_dict,
                    "adhesion": adhesion_dict,
                    "printer_id": effective_printer_id,
                    "profile_path": effective_profile,
                    "message": message,
                }
                return response

            except SlicerNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to slice model: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.",
                    code="SLICER_NOT_FOUND",
                )
            except SlicerError as exc:
                return _srv._error_dict(
                    f"Failed to slice model: {exc}",
                    code="SLICER_ERROR",
                )
            except FileNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to slice model: {exc}",
                    code="FILE_NOT_FOUND",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in slice_and_estimate")
                return _srv._error_dict(
                    f"Unexpected error in slice_and_estimate: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # estimate_cost
        # ------------------------------------------------------------------

        @mcp.tool()
        def estimate_cost(
            file_path: str,
            material: str = "PLA",
            electricity_rate: float = 0.12,
            printer_wattage: float = 200.0,
        ) -> dict:
            """Estimate the cost of a print job from a G-code file.

            Analyses G-code extrusion commands to calculate filament usage,
            material weight, filament cost, electricity cost, and total.

            Args:
                file_path: Path to the G-code file.
                material: Filament material (PLA, PETG, ABS, TPU, ASA, NYLON, PC).
                electricity_rate: Cost per kWh in USD (default 0.12).
                printer_wattage: Printer power consumption in watts (default 200).
            """
            import kiln.server as _srv

            try:
                estimate = _srv._get_cost_estimator().estimate_from_file(
                    file_path,
                    material=material,
                    electricity_rate=electricity_rate,
                    printer_wattage=printer_wattage,
                )
                return {"success": True, "estimate": estimate.to_dict()}
            except FileNotFoundError as exc:
                return _srv._error_dict(f"Failed to estimate cost: {exc}", code="FILE_NOT_FOUND")
            except Exception as exc:
                _logger.exception("Unexpected error in estimate_cost")
                return _srv._error_dict(
                    f"Unexpected error in estimate_cost: {exc}", code="INTERNAL_ERROR"
                )

        # ------------------------------------------------------------------
        # estimate_print_time
        # ------------------------------------------------------------------

        @mcp.tool()
        def estimate_print_time(
            file_path: str,
            profile: str = "",
            printer_id: str = "",
            slicer_path: str = "",
        ) -> dict:
            """Estimate print time and filament usage for a model.

            Slices the model and parses the G-code for print time, filament
            length/weight, layer count, and cost estimates.

            For **already-sliced** G-code files, pass the ``.gcode`` path
            directly — it will be parsed without re-slicing.

            :param file_path: Path to STL/3MF/OBJ or .gcode file.
            :param profile: Optional slicer profile path.
            :param printer_id: Optional printer model ID for bundled profile
                (e.g. ``"bambu_a1"``).  Used when no explicit profile is given.
            :param slicer_path: Optional explicit slicer binary path.
            :returns: Dict with time, filament, and layer estimates.
            """
            import kiln.server as _srv

            try:
                from kiln.slicer import _parse_gcode_estimates

                # If already a gcode file, just parse it directly
                if file_path.lower().endswith((".gcode", ".gco", ".g")):
                    result = _parse_gcode_estimates(file_path)
                    return {"success": True, **result}

                # Otherwise, slice first with the right profile
                from kiln.slicer import estimate_print

                resolved_profile = profile or None
                if not resolved_profile and printer_id:
                    from kiln.slicer_profiles import get_profile_for_printer

                    resolved_profile = get_profile_for_printer(printer_id)

                result = estimate_print(
                    file_path,
                    profile=resolved_profile,
                    slicer_path=slicer_path or None,
                )
                return {"success": True, **result}
            except Exception as exc:
                return _srv._error_dict(f"Print estimation failed: {exc}", code="ESTIMATE_ERROR")

        # ------------------------------------------------------------------
        # estimate_material_cost
        # ------------------------------------------------------------------

        @mcp.tool()
        def estimate_material_cost(
            file_path: str,
            material: str = "pla",
            infill_pct: float = 20.0,
            wall_layers: int = 3,
            cost_per_kg: float = 0.0,
        ) -> dict:
            """Estimate material usage and cost for printing a mesh.

            Computes filament weight, length, and cost based on mesh volume,
            infill percentage, wall shell count, and material density.

            Supported materials: pla, petg, abs, tpu, asa, nylon, pc, pla+,
            carbon_fiber_pla.

            :param file_path: Path to mesh file (.stl, .obj, or .glb).
            :param material: Material type (default "pla").
            :param infill_pct: Interior fill percentage 0-100 (default 20).
            :param wall_layers: Number of perimeter shells (default 3).
            :param cost_per_kg: Override material cost in $/kg (0 = use default).
            :returns: Dict with weight, filament length, and cost.
            """
            import kiln.server as _srv

            try:
                from kiln.generation.validation import (
                    estimate_material_cost as _estimate_cost,
                )

                return {
                    "success": True,
                    **_estimate_cost(
                        file_path,
                        material=material,
                        infill_pct=infill_pct,
                        wall_layers=wall_layers,
                        cost_per_kg=cost_per_kg if cost_per_kg > 0 else None,
                    ),
                }
            except Exception as exc:
                return _srv._error_dict(f"Cost estimation failed: {exc}")

        # ------------------------------------------------------------------
        # estimate_print_progress
        # ------------------------------------------------------------------

        @mcp.tool()
        def estimate_print_progress(
            printer_name: str,
            *,
            elapsed_seconds: float | None = None,
            total_layers: int | None = None,
            current_layer: int | None = None,
        ) -> dict:
            """Estimate print progress with phase-aware time prediction.

            Breaks a print into phases -- preparing, printing, cooling, and
            post-processing -- and uses historical data from the print outcomes
            database to estimate time remaining.  Typically more accurate than
            raw firmware estimates for predicting true completion time.

            Supply ``elapsed_seconds``, ``total_layers``, and ``current_layer``
            when available; any omitted values will be read from the printer's
            live status.

            :param printer_name: Printer running the job.
            :param elapsed_seconds: Seconds elapsed since print start.  Omit to
                read from printer status.
            :param total_layers: Total layer count for the job.  Omit to read
                from printer/G-code metadata.
            :param current_layer: Current layer being printed.  Omit to read
                from printer status.

            See also: ``printer_status()``, ``get_print_outcomes()``.
            """
            import kiln.server as _srv

            try:
                from kiln.progress import get_progress_estimator

                estimator = get_progress_estimator()
                estimate = estimator.estimate(
                    printer_name=printer_name,
                    elapsed_seconds=elapsed_seconds,
                    total_layers=total_layers,
                    current_layer=current_layer,
                )
                return {"success": True, "progress": estimate.to_dict()}
            except Exception as exc:
                _logger.exception("Error in estimate_print_progress")
                return _srv._error_dict(
                    f"Failed to estimate print progress: {exc}", code="PROGRESS_ERROR"
                )

        _logger.debug("Registered estimate tools")


plugin = _EstimateToolsPlugin()
