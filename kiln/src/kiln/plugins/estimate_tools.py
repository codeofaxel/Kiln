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
        - estimate_before_design
        - list_multi_material_addons
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
            printer_name: str | None = None,
        ) -> dict:
            """Primary estimation tool — slice a 3D model and return time, filament, cost, and printability analysis.

            For G-code files (already sliced), use ``estimate_cost`` instead.
            For quick volume-based estimates without slicing, use ``estimate_material_cost``.

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
                printer_name: Registered printer this estimate is FOR.  Omit
                    for the default printer.  An estimate is only as true as
                    the profile behind it, so naming a second machine costs
                    the job on that machine rather than on the default.
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
                    printer_name=printer_name,
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
                        report = analyze_printability(
                            input_path,
                            material=material,
                            printer_id=printer_id or None,
                        )
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
            """Estimate the cost of a print job from a G-code file (already-sliced only).

            For STL/OBJ files, use ``slice_and_estimate`` instead — it slices
            and estimates in one step.

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
            material: str = "",
        ) -> dict:
            """Estimate print time and filament usage for a model.

            Slices the model and parses the G-code for print time, filament
            length/weight, and layer count.

            For **already-sliced** G-code files, pass the ``.gcode`` path
            directly — it will be parsed without re-slicing.

            Weight needs a filament density, which lives on a filament
            profile; Kiln's bundled profiles describe a PRINTER and name no
            filament, so pass ``material`` to get a weight.  Without it the
            weight is reported as absent rather than guessed — a key missing
            from the result means the slicer could not answer it, never that
            the answer is zero.

            **See also:** ``estimate_material_cost`` for weight and cost from
            a mesh with your own price per kg, and ``slice_and_estimate`` for
            a fuller analysis with printability scoring.

            :param file_path: Path to STL/3MF/OBJ or .gcode file.
            :param profile: Optional slicer profile path.
            :param printer_id: Optional printer model ID for bundled profile
                (e.g. ``"bambu_a1"``).  Used when no explicit profile is given.
            :param slicer_path: Optional explicit slicer binary path.
            :param material: Optional filament family (``"PLA"``, ``"PETG"``,
                …) used only as a density source for the weight.
            :returns: Dict with time, filament, and layer estimates.
            """
            import kiln.server as _srv

            try:
                from kiln.slicer import _parse_gcode_estimates, derive_filament_weight

                # If already a gcode file, just parse it directly.  The weight
                # is derived here too: a pre-sliced file is missing it for the
                # same reason, and a caller who named a material should not get
                # a different answer for having sliced first.
                if file_path.lower().endswith((".gcode", ".gco", ".g")):
                    result = _parse_gcode_estimates(file_path)
                    derive_filament_weight(result, material or None)
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
                    material=material or None,
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

            **See also:** ``estimate_print_cost_from_mesh`` for a richer
            estimate that includes support material, adhesion, and electricity.

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

        # ------------------------------------------------------------------
        # estimate_before_design
        # ------------------------------------------------------------------

        @mcp.tool()
        def estimate_before_design(
            width_mm: float = 0.0,
            depth_mm: float = 0.0,
            height_mm: float = 0.0,
            template_id: str = "",
            template_overrides: str = "",
            materials: str = "PLA",
            material_fractions: str = "",
            material_roles: str = "",
            infill_percent: float = -1.0,
            layer_height_mm: float = 0.0,
            nozzle_mm: float = 0.4,
            wall_layers: int = 3,
            printer_id: str = "",
            tool_changer_addon: str = "",
            electricity_rate: float = 0.12,
            printer_wattage: float = 200.0,
        ) -> dict:
            """Estimate print time, cost, and filament usage BEFORE generating a model.

            Works from dimensions alone — no file, no slicing, no generation
            needed.  Use this to answer "how long will it take?", "how much
            will it cost?", and "how much filament?" before committing to a
            design.

            **Two ways to specify dimensions:**

            1. **Direct dimensions** — provide ``width_mm``, ``depth_mm``,
               ``height_mm`` explicitly.
            2. **Template** — provide ``template_id`` (e.g. ``"phone_stand"``,
               ``"box_with_lid"``) and optional ``template_overrides`` to use
               the template's default dimensions.

            **Multi-material prints:** Pass comma-separated materials
            (e.g. ``"PLA,PLA"`` for two-color) with optional fractions
            (e.g. ``"0.85,0.15"`` for body + accent).  The tool estimates
            per-filament usage and tool change overhead automatically.

            Returns time estimate, per-filament weight/length/cost breakdown,
            electricity cost, total cost, and tool swap count.

            :param width_mm: Part width (X) in mm.  Required if no template.
            :param depth_mm: Part depth (Y) in mm.  Required if no template.
            :param height_mm: Part height (Z) in mm.  Required if no template.
            :param template_id: Design template ID (e.g. ``"phone_stand"``).
                Resolves dimensions from template defaults.  Overrides
                width/depth/height if provided.
            :param template_overrides: JSON string of template parameter
                overrides (e.g. ``'{"phone_width": 85}'``).
            :param materials: Comma-separated material names
                (e.g. ``"PLA"`` or ``"PLA,PLA"`` for two-color).
            :param material_fractions: Comma-separated volume fractions
                (e.g. ``"0.85,0.15"``).  Must match materials count and sum
                to 1.0.  Default: body gets 85%, accents split the rest.
            :param material_roles: Comma-separated role labels
                (e.g. ``"body,accent"``).  Default: auto-generated.
            :param infill_percent: Infill density override (0-100).
                Default: from printer profile or 20%.  Pass ``-1`` for auto.
            :param layer_height_mm: Layer height override.  ``0`` = auto.
            :param nozzle_mm: Nozzle diameter in mm (default 0.4).
            :param wall_layers: Number of perimeter shells (default 3).
            :param printer_id: Printer model for speed/setting lookup
                (e.g. ``"bambu_a1"``, ``"prusa_mk4"``).
            :param tool_changer_addon: Optional multi-material add-on ID.
                Overrides the printer's built-in tool change timing.
                Examples: ``"creality_cfs"`` (K1 series),
                ``"mosaic_palette3"`` (universal), ``"coprint_kcm"``
                (Klipper printers), ``"chameleon_mk4"`` (universal),
                ``"elegoo_canvas"`` (Centauri Carbon 2).
                Use ``list_multi_material_addons`` to see all options.
            :param electricity_rate: Cost per kWh in USD (default 0.12).
            :param printer_wattage: Printer power in watts (default 200).
            """
            import json as _json

            import kiln.server as _srv

            try:
                from kiln.pre_estimate import (
                    estimate_from_dimensions,
                    estimate_from_template,
                )

                # Parse comma-separated inputs
                mat_list = [m.strip() for m in materials.split(",") if m.strip()]
                if not mat_list:
                    mat_list = ["PLA"]

                frac_list: list[float] | None = None
                if material_fractions.strip():
                    frac_list = [float(f.strip()) for f in material_fractions.split(",")]

                role_list: list[str] | None = None
                if material_roles.strip():
                    role_list = [r.strip() for r in material_roles.split(",")]

                eff_infill: float | None = None if infill_percent < 0 else infill_percent
                eff_layer: float | None = layer_height_mm if layer_height_mm > 0 else None
                eff_printer: str | None = printer_id if printer_id else None
                eff_addon: str | None = tool_changer_addon if tool_changer_addon.strip() else None

                # Parse template overrides
                tpl_overrides: dict[str, Any] | None = None
                if template_overrides.strip():
                    tpl_overrides = _json.loads(template_overrides)

                # Route to template or direct estimation
                if template_id.strip():
                    est = estimate_from_template(
                        template_id.strip(),
                        param_overrides=tpl_overrides,
                        materials=mat_list,
                        material_fractions=frac_list,
                        material_roles=role_list,
                        infill_percent=eff_infill,
                        layer_height_mm=eff_layer,
                        nozzle_mm=nozzle_mm,
                        wall_layers=wall_layers,
                        printer_id=eff_printer,
                        tool_changer_addon=eff_addon,
                        electricity_rate=electricity_rate,
                        printer_wattage=printer_wattage,
                    )
                else:
                    if width_mm <= 0 or depth_mm <= 0 or height_mm <= 0:
                        return _srv._error_dict(
                            "Provide positive width_mm/depth_mm/height_mm "
                            "or a template_id.",
                            code="MISSING_DIMENSIONS",
                        )
                    est = estimate_from_dimensions(
                        width_mm,
                        depth_mm,
                        height_mm,
                        materials=mat_list,
                        material_fractions=frac_list,
                        material_roles=role_list,
                        infill_percent=eff_infill,
                        layer_height_mm=eff_layer,
                        nozzle_mm=nozzle_mm,
                        wall_layers=wall_layers,
                        printer_id=eff_printer,
                        tool_changer_addon=eff_addon,
                        electricity_rate=electricity_rate,
                        printer_wattage=printer_wattage,
                    )

                # Build human-readable summary
                parts: list[str] = [
                    f"Estimated {est.estimated_time_human}",
                    f"{est.total_weight_grams}g total filament",
                    f"${est.total_cost_usd:.2f} total cost",
                ]
                if est.tool_changes > 0:
                    changer_label = est.tool_changer_addon_name or est.tool_change_type
                    parts.append(
                        f"{est.tool_changes} tool swaps "
                        f"({changer_label}, "
                        f"+{_format_time(est.tool_change_time_seconds)})"
                    )

                filament_summary = []
                for f in est.filaments:
                    filament_summary.append(
                        f"{f.material} ({f.role}): {f.weight_grams}g, "
                        f"{f.length_meters}m, ${f.cost_usd:.2f}"
                    )

                return {
                    "success": True,
                    "estimate": est.to_dict(),
                    "message": " | ".join(parts),
                    "filament_summary": filament_summary,
                }

            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_INPUT")
            except Exception as exc:
                _logger.exception("Unexpected error in estimate_before_design")
                return _srv._error_dict(
                    f"Unexpected error: {exc}", code="INTERNAL_ERROR"
                )

        # ------------------------------------------------------------------
        # list_multi_material_addons
        # ------------------------------------------------------------------

        @mcp.tool()
        def list_multi_material_addons(
            printer_id: str = "",
        ) -> dict:
            """List available multi-material add-on systems for 3D printers.

            Returns a catalog of optional multi-material add-ons (Creality CFS,
            Mosaic Palette, Co Print KCM, 3D Chameleon, Elegoo CANVAS) with
            their tool change times, color capacity, and compatibility info.

            When a ``printer_id`` is provided, only add-ons compatible with
            that printer are returned.  Universal add-ons (Palette, Chameleon)
            appear for all printers.  Klipper-only add-ons (KCM) appear only
            for Klipper-based printers.

            Use the returned ``id`` values as the ``tool_changer_addon``
            parameter in ``estimate_before_design`` to model multi-material
            prints on printers that don't have a built-in tool changer.

            :param printer_id: Optional printer model to filter by compatibility
                (e.g. ``"k1"``, ``"ender3"``, ``"voron_2"``).
            """
            import kiln.server as _srv

            try:
                from kiln.pre_estimate import list_addons

                eff_printer: str | None = printer_id if printer_id.strip() else None
                addons = list_addons(printer_id=eff_printer)

                return {
                    "success": True,
                    "count": len(addons),
                    "addons": addons,
                    "message": (
                        f"{len(addons)} add-on(s) available"
                        + (f" for {printer_id}" if eff_printer else "")
                        + ". Pass the 'id' as tool_changer_addon in estimate_before_design."
                    ),
                }
            except Exception as exc:
                _logger.exception("Error in list_multi_material_addons")
                return _srv._error_dict(
                    f"Failed to list add-ons: {exc}", code="INTERNAL_ERROR"
                )

        _logger.debug("Registered estimate tools")


plugin = _EstimateToolsPlugin()
