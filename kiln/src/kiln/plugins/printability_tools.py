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

        _logger.debug("Registered printability tools")


plugin = _PrintabilityToolsPlugin()
