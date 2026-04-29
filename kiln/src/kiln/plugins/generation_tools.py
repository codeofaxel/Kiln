"""Model generation tools plugin.

Extracts text-to-3D model generation MCP tools from server.py into a focused
plugin module.  Provides tools for listing providers, submitting generation
jobs, polling status, downloading results, and running full generate-to-print
pipelines.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)


def _resolve_tool_build_volume(
    printer_id: str | None,
) -> tuple[str, tuple[float, float, float]] | None:
    if not printer_id:
        return None
    from kiln.printers.bed_fit import resolve_build_volume

    resolved = resolve_build_volume(printer_id)
    if resolved is None:
        raise ValueError(
            f"Unknown printer model {printer_id!r}; omit the printer id and "
            "pass explicit build_volume_x/y/z, or use a supported printer "
            "model id."
        )
    return resolved


class _GenerationToolsPlugin:
    """Text-to-3D and image-to-3D model generation tools.

    Tools:
        - generate_original_design
        - preview_generated_model
        - validate_and_prepare_mesh
        - generate_texture
    """

    @property
    def name(self) -> str:
        return "generation_tools"

    @property
    def description(self) -> str:
        return (
            "Text-to-3D and image-to-3D model generation tools "
            "(Meshy, Gemini, OpenSCAD, Tripo3D, Stability), including "
            "image-to-3D via Gemini and closed-loop original design generation"
        )

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register generation tools with the MCP server."""

        @mcp.tool()
        def generate_original_design(
            requirements: str,
            provider: str = "auto",
            material: str | None = None,
            printer_model: str | None = None,
            style: str | None = None,
            output_dir: str | None = None,
            build_volume_x: float | None = None,
            build_volume_y: float | None = None,
            build_volume_z: float | None = None,
            nozzle_diameter: float = 0.4,
            layer_height: float = 0.2,
            max_overhang_angle: float = 45.0,
            timeout: int = 600,
            max_attempts: int = 2,
        ) -> dict:
            """Generate and harshly audit an original printable design.

            This is the highest-level original-creation tool in Kiln. It takes
            a natural-language design brief, chooses the best available
            idea-to-3D backend, generates a candidate, audits the result for
            printability and design correctness, and can perform one or more
            corrective retries using feedback from failed attempts.

            Provider notes:
            - ``auto`` prefers Gemini for idea-to-CAD when available.
            - ``openscad`` is intentionally rejected here because it compiles
              code; it does not turn a natural-language idea into geometry.

            Args:
                requirements: Natural-language description of the part to create.
                provider: ``auto``, ``gemini``, ``meshy``, ``tripo3d``, or ``stability``.
                material: Optional material target (e.g. ``"petg"``).
                printer_model: Optional printer model ID (e.g. ``"bambu_a1"``).
                style: Optional style hint for providers that support it.
                output_dir: Optional directory for generated files.
                build_volume_x: Optional build volume X override in mm.
                build_volume_y: Optional build volume Y override in mm.
                build_volume_z: Optional build volume Z override in mm.
                nozzle_diameter: Printer nozzle diameter in mm.
                layer_height: Printer layer height in mm.
                max_overhang_angle: Supportless overhang threshold in degrees.
                timeout: Max seconds to wait per generation attempt.
                max_attempts: Max corrective generation attempts.
            """
            import kiln.server as _srv
            from kiln.generation import GenerationError
            from kiln.original_design import generate_original_design as _generate_original

            if err := _srv._check_auth("generate"):
                return err
            try:
                build_volume = None
                resolved_model_id = None
                if (
                    build_volume_x is not None
                    and build_volume_y is not None
                    and build_volume_z is not None
                ):
                    build_volume = (
                        build_volume_x,
                        build_volume_y,
                        build_volume_z,
                    )
                elif printer_model:
                    resolved = _resolve_tool_build_volume(printer_model)
                    if resolved:
                        resolved_model_id, build_volume = resolved

                session = _generate_original(
                    requirements,
                    provider=provider,
                    material=material,
                    printer_model=printer_model,
                    style=style,
                    output_dir=output_dir,
                    build_volume=build_volume,
                    nozzle_diameter=nozzle_diameter,
                    layer_height=layer_height,
                    max_overhang_angle=max_overhang_angle,
                    timeout=timeout,
                    max_attempts=max_attempts,
                )
                result = session.to_dict()
                result["status"] = "success"
                result["message"] = session.summary
                if resolved_model_id:
                    result["bed_size_source"] = "printer_intelligence"
                    result["bed_size_model_id"] = resolved_model_id
                    result["bed_dims_mm"] = list(build_volume)
                return result
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_INPUT")
            except GenerationError as exc:
                return _srv._error_dict(
                    str(exc),
                    code=exc.code or "GENERATION_ERROR",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in generate_original_design")
                return _srv._error_dict(
                    f"Unexpected error in generate_original_design: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def preview_generated_model(file_path: str) -> dict:
            """Specialized post-generation verification renderer. For general-purpose
            model viewing, use ``visualize_model`` instead. This tool adds
            generation-specific checks (floating geometry, thin walls) on top of
            the standard multi-angle renders.

            Render a 3D model to multi-angle PNG previews for visual inspection.

            **REQUIRED** before printing any generated model.  You MUST call this
            tool after generating a model and BEFORE printing.  View ALL rendered
            angles to check for:
            - Missing or simplified features (e.g., plain box instead of pattern)
            - Incorrect proportions or dimensions
            - Floating/disconnected geometry
            - Thin walls that won't print
            - Overhangs that need supports
            - Non-manifold artifacts
            - Bottom surface not flat (check bottom view for bed adhesion)
            - Elephant's foot risk on first layer

            **Required workflow:**
            1. Call ``generate_model`` or ``generate_model_from_image``
            2. **MUST** call ``preview_generated_model`` — view ALL angles
            3. If the model doesn't match the request, regenerate with refined prompt
            4. Call ``validate_generated_mesh`` for structural checks
            5. Only then proceed to print

            Args:
                file_path: Path to an STL or 3MF file to render.  Colored 3MF
                    files are rendered with per-face colors via the PIL-based
                    colored renderer; STL and colorless 3MF use OpenSCAD.
            """
            import kiln.server as _srv

            if not os.path.isfile(file_path):
                return _srv._error_dict(
                    f"File not found: {file_path}",
                    code="FILE_NOT_FOUND",
                )

            ext = os.path.splitext(file_path)[1].lower()

            # ---- Colored 3MF fast path ----
            if ext == ".3mf":
                try:
                    from kiln.colored_renderer import render_colored_mesh_multi_angle
                    from kiln.threemf_parser import parse_colored_3mf

                    mesh = parse_colored_3mf(file_path)
                    if mesh.colors_found:
                        _logger.debug(
                            "3MF has per-face colors (%d unique) — using colored renderer",
                            mesh.color_count,
                        )
                        views = render_colored_mesh_multi_angle(
                            mesh.triangles,
                            angles=["isometric", "front", "right", "top", "bottom"],
                        )
                        return {
                            "success": True,
                            "previews": [
                                {"angle": v["angle"], "path": v["path"]}
                                for v in views if v.get("path")
                            ],
                            "message": (
                                f"Rendered {len([v for v in views if v.get('path')])} "
                                "colored preview angles (iso, front, side, top, bottom). "
                                "View ALL angles to evaluate the model before printing. "
                                "Check the BOTTOM view to verify flat bed adhesion surface. "
                                "Check for: thin walls, floating geometry, missing features, "
                                "incorrect proportions, and overhangs needing supports. "
                                "Use recommend_material and get_design_constraints from the "
                                "intelligence tools for material and printability guidance."
                            ),
                        }
                    # No colors — fall through to VisualVerifier / OpenSCAD
                    _logger.debug("3MF has no per-face colors — using OpenSCAD path")
                except ImportError:
                    _logger.debug("Colored renderer not available — using OpenSCAD path")
                except Exception:  # noqa: BLE001
                    _logger.debug(
                        "Colored 3MF parse/render failed — using OpenSCAD path",
                        exc_info=True,
                    )

            try:
                from kiln.generation.visual_verify import VisualVerifier

                verifier = VisualVerifier(
                    api_key="unused",  # Not needed for rendering
                    model="unused",
                )
                paths = verifier.render_multi_angle(file_path)

                return {
                    "success": True,
                    "previews": [
                        {"angle": "isometric", "path": paths[0]},
                        {"angle": "front", "path": paths[1]},
                        {"angle": "right_side", "path": paths[2]},
                        {"angle": "top", "path": paths[3]},
                        {"angle": "bottom", "path": paths[4]},
                    ],
                    "message": (
                        "Rendered 5 preview angles (iso, front, side, top, bottom). "
                        "View ALL angles to evaluate the model before printing. "
                        "Check the BOTTOM view to verify flat bed adhesion surface. "
                        "Check for: thin walls, floating geometry, missing features, "
                        "incorrect proportions, and overhangs needing supports. "
                        "Use recommend_material and get_design_constraints from the "
                        "intelligence tools for material and printability guidance."
                    ),
                }
            except Exception as exc:
                _logger.exception("Failed to render preview")
                return _srv._error_dict(
                    f"Failed to render preview: {exc}",
                    code="RENDER_ERROR",
                )

        @mcp.tool()
        def validate_and_prepare_mesh(
            file_path: str,
            material: str = "PLA",
            nozzle_diameter: float = 0.4,
            layer_height: float = 0.2,
            build_volume_x: float | None = None,
            build_volume_y: float | None = None,
            build_volume_z: float | None = None,
            printer_id: str = "",
            auto_repair: bool = True,
            auto_scale: bool = False,
            min_printability_score: int = 40,
        ) -> dict:
            """Full validation pipeline: validate, repair, analyze, and prepare a mesh for printing.

            **See also:** ``validate_and_prepare`` for a more comprehensive
            10-step pipeline (format, mesh, scale, repair, printability,
            structural, bed-fit, material, and cost estimation).

            Runs every AI-generated mesh through Kiln's engineering review before
            it reaches the slicer or printer.  Chains validation → auto-repair →
            printability analysis → build volume check into a single quality gate.

            **Use this instead of ``validate_generated_mesh`` when you want the
            full pipeline** — repair, printability scoring, build volume checks,
            and actionable recommendations.

            The mesh file may be modified in place if ``auto_repair`` or
            ``auto_scale`` is enabled.  The response includes the final file
            path (which may differ from the input if repairs created a new file).

            Args:
                file_path: Path to an STL, OBJ, or GLB file.
                material: Filament material for printability analysis (default PLA).
                nozzle_diameter: Printer nozzle diameter in mm (default 0.4).
                layer_height: Print layer height in mm (default 0.2).
                build_volume_x: Optional X build dimension (mm).
                build_volume_y: Optional Y build dimension (mm).
                build_volume_z: Optional Z build dimension (mm).
                printer_id: Optional supported printer model id.  When
                    provided, printer intelligence supplies the build volume.
                auto_repair: Auto-repair non-manifold meshes (default True).
                auto_scale: Auto-scale if mesh exceeds build volume (default False).
                min_printability_score: Minimum score (0-100) to pass (default 40).
            """
            import kiln.server as _srv

            try:
                from kiln.mesh_validation_pipeline import run_validation_pipeline

                build_volume = None
                resolved_model_id = None
                if (
                    build_volume_x is not None
                    and build_volume_y is not None
                    and build_volume_z is not None
                ):
                    build_volume = (
                        build_volume_x,
                        build_volume_y,
                        build_volume_z,
                    )
                elif printer_id:
                    resolved = _resolve_tool_build_volume(printer_id)
                    if resolved:
                        resolved_model_id, build_volume = resolved

                result = run_validation_pipeline(
                    file_path,
                    material=material,
                    nozzle_diameter=nozzle_diameter,
                    layer_height=layer_height,
                    build_volume=build_volume,
                    auto_repair=auto_repair,
                    auto_scale=auto_scale,
                    min_printability_score=min_printability_score,
                )
                return {
                    "success": True,
                    "passed": result.passed,
                    "result": result.to_dict(),
                    "message": result.summary,
                    **(
                        {
                            "bed_size_source": "printer_intelligence",
                            "bed_size_model_id": resolved_model_id,
                            "bed_dims_mm": list(build_volume),
                        }
                        if resolved_model_id and build_volume
                        else {}
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in validate_and_prepare_mesh")
                return _srv._error_dict(
                    f"Unexpected error in validate_and_prepare_mesh: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def generate_texture(
            mesh_path: str,
            prompt: str,
            style: str = "realistic",
            provider: str = "meshy",
        ) -> dict:
            """Cloud AI texture generation (requires Meshy API key + internet).

            For instant offline textures without any API, use
            ``apply_procedural_texture`` (multicolor) or
            ``apply_geometric_texture`` (relief) from kiln-pro.

            Takes any untextured mesh (STL, OBJ, GLB) and generates a
            UV-mapped texture based on the text description.  The textured
            model can then be processed with ``auto_multicolor_from_texture``
            (Kiln Pro) for multi-material printing.

            Requires a Meshy API key (``KILN_MESHY_API_KEY``).

            Args:
                mesh_path: Absolute path to the mesh file (STL, OBJ, GLB, FBX).
                prompt: Text description of the desired texture (max 600 chars).
                    E.g. "smooth matte wood grain with dark walnut finish".
                style: Art style — ``"realistic"`` (default) or ``"2.5d-cartoon"``.
                provider: Generation provider.  Currently only ``"meshy"`` supports
                    retexturing.
            """
            import kiln.server as _srv
            from kiln.generation import GenerationAuthError, GenerationError

            if err := _srv._check_auth("generate"):
                return err

            if not os.path.isfile(mesh_path):
                return _srv._error_dict(
                    f"Mesh file not found: {mesh_path}",
                    code="FILE_NOT_FOUND",
                )

            if provider != "meshy":
                return _srv._error_dict(
                    f"Provider {provider!r} does not support retexturing. "
                    "Only 'meshy' is supported.",
                    code="UNSUPPORTED_PROVIDER",
                )

            try:
                gen = _srv._get_generation_provider("meshy")
                job = gen.retexture(mesh_path, prompt, style=style)
                # Telemetry: count as decoration (textures are a decoration subtype)
                try:
                    from kiln.daily_stats import record_event
                    record_event("decorations", detail="ai_texture")
                except Exception:
                    pass

                return {
                    "success": True,
                    "job": job.to_dict(),
                    "message": (
                        f"Retexture job submitted to {gen.display_name}. "
                        f"Use generation_status('{job.id}', provider='meshy') to poll, "
                        f"then download_generated_model('{job.id}', provider='meshy') "
                        "to retrieve the textured model with OBJ + MTL + PNG textures."
                    ),
                    "next_steps": [
                        f"generation_status('{job.id}', provider='meshy')",
                        f"download_generated_model('{job.id}', provider='meshy')",
                        "auto_multicolor_from_texture(<obj_path>) for multi-material printing (Kiln Pro)",
                    ],
                }
            except GenerationAuthError as exc:
                return _srv._error_dict(
                    f"Failed to generate texture (auth): {exc}. "
                    "Check that KILN_MESHY_API_KEY is set.",
                    code="AUTH_ERROR",
                )
            except GenerationError as exc:
                return _srv._error_dict(
                    f"Failed to generate texture: {exc}",
                    code=exc.code or "GENERATION_ERROR",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in generate_texture")
                return _srv._error_dict(
                    f"Unexpected error in generate_texture: {exc}",
                    code="INTERNAL_ERROR",
                )

        _logger.debug("Registered generation tools")


plugin = _GenerationToolsPlugin()
