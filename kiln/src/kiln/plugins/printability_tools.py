"""Printability analysis and auto-orientation tools plugin.

Provides MCP tools for analyzing 3D model printability, finding optimal
print orientations, and estimating support requirements.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _PrintabilityToolsPlugin:
    """Printability analysis and auto-orientation tools.

    Tools:
        - analyze_printability
        - auto_orient_model
        - estimate_supports
    """

    @property
    def name(self) -> str:
        return "printability_tools"

    @property
    def description(self) -> str:
        return "Printability analysis and auto-orientation tools"

    def register(self, mcp: Any) -> None:
        """Register printability tools with the MCP server."""

        @mcp.tool()
        def analyze_printability(
            file_path: str,
            nozzle_diameter: float = 0.4,
            layer_height: float = 0.2,
            max_overhang_angle: float = 45.0,
            build_volume_x: float | None = None,
            build_volume_y: float | None = None,
            build_volume_z: float | None = None,
        ) -> dict:
            """Analyze a 3D model for FDM printing readiness.

            Performs deep analysis of an STL or OBJ mesh including overhang
            detection, thin wall analysis, bridging assessment, bed adhesion
            surface estimation, and support volume estimation.  Returns a
            printability score (0-100), letter grade (A-F), and actionable
            recommendations.

            Args:
                file_path: Path to an STL or OBJ mesh file.
                nozzle_diameter: Printer nozzle diameter in mm (default 0.4).
                layer_height: Print layer height in mm (default 0.2).
                max_overhang_angle: Maximum overhang angle in degrees before
                    supports are needed (default 45).
                build_volume_x: Optional build volume X dimension in mm.
                build_volume_y: Optional build volume Y dimension in mm.
                build_volume_z: Optional build volume Z dimension in mm.
            """
            import kiln.server as _srv
            from kiln.printability import analyze_printability as _analyze

            try:
                build_volume = None
                if build_volume_x is not None and build_volume_y is not None and build_volume_z is not None:
                    build_volume = (build_volume_x, build_volume_y, build_volume_z)

                report = _analyze(
                    file_path,
                    nozzle_diameter=nozzle_diameter,
                    layer_height=layer_height,
                    max_overhang_angle=max_overhang_angle,
                    build_volume=build_volume,
                )
                return {
                    "success": True,
                    "report": report.to_dict(),
                    "message": (
                        f"Printability score: {report.score}/100 (grade {report.grade}).  "
                        f"{'Printable' if report.printable else 'Not recommended for printing'}."
                    ),
                }
            except ValueError as exc:
                return _srv._error_dict(
                    f"Failed to analyze printability: {exc}",
                    code="VALIDATION_ERROR",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in analyze_printability")
                return _srv._error_dict(
                    f"Unexpected error in analyze_printability: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def auto_orient_model(
            file_path: str,
            candidates: int = 24,
            nozzle_diameter: float = 0.4,
            apply: bool = False,
            output_path: str | None = None,
        ) -> dict:
            """Find the optimal print orientation for a 3D model.

            Evaluates multiple rotations of the model and scores each based
            on bed adhesion, support requirements, print height, and
            overhang coverage.  Optionally applies the best orientation and
            writes a reoriented STL file.

            Args:
                file_path: Path to an STL or OBJ mesh file.
                candidates: Number of candidate orientations to evaluate
                    (default 24).
                nozzle_diameter: Printer nozzle diameter in mm (default 0.4).
                apply: If True, apply the best orientation and write the
                    reoriented STL to disk.
                output_path: Output path for the reoriented STL.  Only used
                    when ``apply`` is True.  Defaults to
                    ``<input>_oriented.stl``.
            """
            import kiln.server as _srv
            from kiln.auto_orient import (
                apply_orientation,
                find_optimal_orientation,
            )

            try:
                result = find_optimal_orientation(
                    file_path,
                    candidates=candidates,
                    nozzle_diameter=nozzle_diameter,
                )

                resp: dict[str, Any] = {
                    "success": True,
                    "result": result.to_dict(),
                    "message": (
                        f"Best orientation: rotate X={result.best.rotation_x}deg "
                        f"Y={result.best.rotation_y}deg (score {result.best.score}).  "
                        f"Original score: {result.original_score}.  "
                        f"Improvement: {result.improvement_percentage}%."
                    ),
                }

                if apply:
                    oriented_path = apply_orientation(
                        file_path,
                        result.best.rotation_x,
                        result.best.rotation_y,
                        result.best.rotation_z,
                        output_path=output_path,
                    )
                    resp["oriented_file"] = oriented_path
                    resp["message"] += f"  Reoriented file: {oriented_path}"

                return resp
            except ValueError as exc:
                return _srv._error_dict(
                    f"Failed to auto-orient model: {exc}",
                    code="VALIDATION_ERROR",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in auto_orient_model")
                return _srv._error_dict(
                    f"Unexpected error in auto_orient_model: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def estimate_supports(
            file_path: str,
            max_overhang_angle: float = 45.0,
        ) -> dict:
            """Estimate support volume for a 3D model.

            Analyzes the mesh for overhangs and estimates the volume of
            support material needed to print the model in its current
            orientation.

            Args:
                file_path: Path to an STL or OBJ mesh file.
                max_overhang_angle: Maximum overhang angle in degrees
                    before supports are needed (default 45).
            """
            import kiln.server as _srv
            from kiln.auto_orient import estimate_supports as _estimate

            try:
                estimate = _estimate(
                    file_path,
                    max_overhang_angle=max_overhang_angle,
                )
                return {
                    "success": True,
                    "estimate": estimate.to_dict(),
                    "message": (
                        f"Estimated support volume: {estimate.estimated_support_volume_mm3:.1f} mm3 "
                        f"({estimate.support_percentage:.1f}% of model).  "
                        f"{'Supports needed.' if estimate.needs_supports else 'No supports needed.'}"
                    ),
                }
            except ValueError as exc:
                return _srv._error_dict(
                    f"Failed to estimate supports: {exc}",
                    code="VALIDATION_ERROR",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in estimate_supports")
                return _srv._error_dict(
                    f"Unexpected error in estimate_supports: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def recommend_adhesion_settings(
            model_path: str,
            material: str = "PLA",
            printer_id: str | None = None,
        ) -> dict:
            """Recommend brim/raft settings for a 3D model based on geometry + material.

            Analyzes the model's bed contact area, material warp tendency, and
            printer type to produce a concrete brim width and optional raft
            recommendation.  Returns ``slicer_overrides`` ready to pass to
            ``slice_model`` or ``slice_and_print``.

            Args:
                model_path: Path to an STL or OBJ mesh file.
                material: Filament material (e.g. ``"PLA"``, ``"ABS"``,
                    ``"PETG"``).  Affects warp risk calculation.
                printer_id: Optional printer model ID (e.g. ``"bambu_a1"``).
                    Used to detect bed-slinger printers that need wider brims.
            """
            import kiln.server as _srv
            from kiln.printability import (
                analyze_printability as _analyze,
            )
            from kiln.printability import (
                is_bedslinger,
                recommend_adhesion,
            )

            try:
                report = _analyze(model_path)
                if report.bed_adhesion is None:
                    return {
                        "success": True,
                        "recommendation": None,
                        "message": "Could not analyze bed adhesion for this model.",
                    }

                has_enclosure = False
                is_bs = False
                if printer_id:
                    is_bs = is_bedslinger(printer_id)
                    try:
                        from kiln.printer_intelligence import get_printer_intel

                        intel = get_printer_intel(printer_id)
                        if intel:
                            has_enclosure = intel.get("has_enclosure", False)
                    except Exception:
                        pass

                rec = recommend_adhesion(
                    report.bed_adhesion,
                    material=material,
                    has_enclosure=has_enclosure,
                    is_bedslinger_printer=is_bs,
                    model_height_mm=report.model_height_mm,
                )
                return {
                    "success": True,
                    "recommendation": rec.to_dict(),
                    "message": rec.rationale,
                }
            except ValueError as exc:
                return _srv._error_dict(
                    f"Failed to analyze adhesion: {exc}",
                    code="VALIDATION_ERROR",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in recommend_adhesion_settings")
                return _srv._error_dict(
                    f"Unexpected error in recommend_adhesion_settings: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def diagnose_print_failure_live(
            printer_name: str | None = None,
            model_path: str | None = None,
            material: str | None = None,
            printer_id: str | None = None,
        ) -> dict:
            """Diagnose a print failure using live printer state + model geometry.

            Unlike ``analyze_print_failure`` (which requires a job_id and
            analyzes historical data), this tool works in real-time by
            reading the current printer state and optionally analyzing
            the model that was being printed.

            Combines printer temperature deltas, bed adhesion analysis,
            overhang geometry, material properties, and printer intelligence
            to produce a ranked diagnosis with actionable fixes.

            Args:
                printer_name: Printer to diagnose.  Omit for the default printer.
                model_path: Path to the model file that was being printed.
                    Enables geometry-based diagnosis (adhesion, overhangs).
                material: Filament material (e.g. ``"ABS"``, ``"PLA"``).
                printer_id: Printer model ID (e.g. ``"bambu_a1"``).
                    Enables printer-specific intelligence lookup.
            """
            import kiln.server as _srv
            from kiln.printability import diagnose_from_signals

            try:
                # --- Gather signals ---
                signals: dict[str, Any] = {}

                # 1. Printer state (mandatory if printer available)
                state_data: dict[str, Any] | None = None
                try:
                    if printer_name:
                        adapter = _srv._registry.get(printer_name)
                    else:
                        adapter = _srv._get_adapter()
                    state = adapter.get_state()
                    state_data = state.to_dict()
                    signals["tool_temp_actual"] = state.tool_temp_actual
                    signals["tool_temp_target"] = state.tool_temp_target
                    signals["bed_temp_actual"] = state.bed_temp_actual
                    signals["bed_temp_target"] = state.bed_temp_target
                    if state.print_error:
                        signals["print_error"] = state.print_error
                except Exception as exc:
                    _logger.debug("Could not get printer state: %s", exc)

                # 2. Model analysis (optional)
                model_analysis: dict[str, Any] | None = None
                if model_path:
                    try:
                        from kiln.printability import analyze_printability as _analyze

                        report = _analyze(model_path)
                        model_analysis = report.to_dict()
                        if report.bed_adhesion:
                            signals["adhesion_risk"] = report.bed_adhesion.adhesion_risk
                            signals["contact_percentage"] = report.bed_adhesion.contact_percentage
                        if report.overhangs:
                            signals["overhang_pct"] = report.overhangs.overhang_percentage
                        if report.bridging:
                            signals["max_bridge_mm"] = report.bridging.max_bridge_length
                    except Exception as exc:
                        _logger.debug("Could not analyze model: %s", exc)

                # 3. Printer intelligence (optional)
                effective_pid = printer_id
                if not effective_pid:
                    effective_pid = _srv._map_printer_hint_to_profile_id(_srv._PRINTER_MODEL)
                if effective_pid:
                    try:
                        from kiln.printer_intelligence import (
                            diagnose_issue,
                            get_printer_intel,
                        )

                        intel = get_printer_intel(effective_pid)
                        if intel:
                            signals["printer_has_enclosure"] = intel.get("has_enclosure", False)
                            # Build symptom queries from state
                            symptom_queries = _build_symptom_queries(state_data, signals)
                            modes: list[dict[str, str]] = []
                            for symptom in symptom_queries:
                                modes.extend(diagnose_issue(effective_pid, symptom))
                            if modes:
                                signals["failure_modes_from_intel"] = modes
                    except Exception as exc:
                        _logger.debug("Could not query printer intelligence: %s", exc)

                # 4. Material
                if material:
                    signals["material"] = material.upper()

                # --- Run diagnosis ---
                diagnosis = diagnose_from_signals(
                    signals,
                    printer_id=effective_pid,
                    material=material,
                )

                result: dict[str, Any] = {
                    "success": True,
                    "diagnosis": diagnosis.to_dict(),
                    "message": (
                        f"Diagnosis: {diagnosis.failure_category} failure "
                        f"(confidence {diagnosis.confidence:.0%}).  "
                        f"Top cause: {diagnosis.probable_causes[0] if diagnosis.probable_causes else 'unknown'}."
                    ),
                }
                if state_data:
                    result["printer_state"] = state_data
                if model_analysis:
                    result["model_analysis"] = model_analysis
                return result

            except Exception as exc:
                _logger.exception("Unexpected error in diagnose_print_failure_live")
                return _srv._error_dict(
                    f"Unexpected error in diagnose_print_failure_live: {exc}",
                    code="INTERNAL_ERROR",
                )

        _logger.debug("Registered printability tools")


def _build_symptom_queries(
    state_data: dict[str, Any] | None,
    signals: dict[str, Any],
) -> list[str]:
    """Build symptom query strings for printer intelligence lookup."""
    queries: list[str] = []

    # Adhesion-related
    risk = signals.get("adhesion_risk")
    if risk == "high":
        queries.append("bed adhesion failure")
        queries.append("print detached from bed")
    elif risk == "medium":
        queries.append("poor bed adhesion")

    # Thermal
    tool_actual = signals.get("tool_temp_actual")
    tool_target = signals.get("tool_temp_target")
    if tool_actual is not None and tool_target is not None:
        delta = abs(tool_actual - tool_target)
        if delta > 10:
            queries.append("temperature fluctuation")
            queries.append("thermal runaway")

    # Error state
    if signals.get("print_error"):
        queries.append(str(signals["print_error"]))

    # Geometry
    if signals.get("overhang_pct", 0) > 30:
        queries.append("overhang failure")
    if signals.get("max_bridge_mm", 0) > 15:
        queries.append("bridge failure")

    # Material
    mat = signals.get("material", "")
    if mat.upper() in {"ABS", "ASA", "PA", "PC"} and not signals.get("printer_has_enclosure"):
        queries.append("warping")
        queries.append("layer splitting")

    if not queries:
        queries.append("print failure")

    return queries


plugin = _PrintabilityToolsPlugin()
