"""Advanced mesh diagnostic tools plugin.

Provides an MCP tool for deep mesh defect analysis using Trimesh:
self-intersections, inverted normals, degenerate faces, floating
fragments, and detailed hole reporting.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _MeshDiagnosticToolsPlugin:
    """Advanced mesh diagnostic tools.

    Tools:
        - diagnose_mesh
    """

    @property
    def name(self) -> str:
        return "mesh_diagnostic_tools"

    @property
    def description(self) -> str:
        return "Advanced mesh defect analysis (self-intersections, normals, holes, fragments)"

    def register(self, mcp: Any) -> None:
        """Register mesh diagnostic tools with the MCP server."""

        @mcp.tool()
        def diagnose_mesh(
            file_path: str,
        ) -> dict:
            """Deep mesh defect analysis — self-intersections, holes, normals, fragments (defect-focused).

            Goes deeper than ``analyze_mesh_geometry`` (which focuses on printability
            scoring and overhang detection). Use this when you suspect mesh defects
            or when ``repair_mesh`` didn't fix the issue.

            Analyzes: self-intersections, inverted/inconsistent normals, degenerate
            (zero-area) faces, floating fragments, detailed hole reporting (count,
            size, location), and polygon count assessment for FDM printing.

            Returns a structured report with severity level, defect list, and
            actionable fix recommendations (specific MeshLab/Blender steps).

            Requires the optional ``trimesh`` package (``pip install trimesh``).
            Supports STL, OBJ, PLY, OFF, GLB, and GLTF formats.

            Use this BEFORE slicing to catch problems that would cause print
            failures or slicer errors.  Complements ``validate_generated_mesh``
            (basic checks) and ``analyze_printability`` (print-readiness scoring).

            Args:
                file_path: Path to a mesh file (STL, OBJ, PLY, OFF, GLB, GLTF).
            """
            import kiln.server as _srv

            try:
                from kiln.mesh_diagnostics import diagnose_mesh as _diagnose

                report = _diagnose(file_path)

                # Build a concise summary message for the agent.
                if report.severity == "clean":
                    summary = (
                        f"Mesh is clean. {report.face_count:,} faces, "
                        f"{report.dimensions_mm['x']:.1f} x "
                        f"{report.dimensions_mm['y']:.1f} x "
                        f"{report.dimensions_mm['z']:.1f} mm. "
                        f"No defects detected — ready for slicing."
                    )
                else:
                    defect_summary = "; ".join(report.defects[:3])
                    summary = (
                        f"Severity: {report.severity}. "
                        f"{report.face_count:,} faces, "
                        f"{len(report.defects)} defect(s) found: {defect_summary}"
                    )

                return {
                    "success": True,
                    "report": report.to_dict(),
                    "message": summary,
                }

            except ImportError as exc:
                return _srv._error_dict(
                    str(exc),
                    code="MISSING_DEPENDENCY",
                    retryable=False,
                )
            except ValueError as exc:
                return _srv._error_dict(
                    f"Failed to diagnose mesh: {exc}",
                    code="VALIDATION_ERROR",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in diagnose_mesh")
                return _srv._error_dict(
                    f"Unexpected error in diagnose_mesh: {exc}",
                    code="INTERNAL_ERROR",
                )

        _logger.debug("Registered mesh diagnostic tools")


plugin = _MeshDiagnosticToolsPlugin()
