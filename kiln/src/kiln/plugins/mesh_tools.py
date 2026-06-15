"""Mesh manipulation and analysis tool plugin.

Migrated from server.py to reduce its size.  Covers mesh repair,
analysis, composition, boolean operations, scaling, mirroring,
hollowing, filleting, chamfering, splitting, merging, and more.

Each module exposes a module-level ``plugin`` variable implementing the
:class:`~kiln.plugin_loader.ToolPlugin` protocol.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
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
            f"Unknown printer_id {printer_id!r}; omit printer_id and pass "
            "explicit bed/build-volume dimensions, or use a supported "
            "printer model id."
        )
    return resolved


class _MeshToolsPlugin:
    """Mesh manipulation, analysis, and transformation tools.

    Covers:
    - Mesh validation and quality scoring
    - Mesh repair (basic and advanced)
    - Mesh composition (merge, boolean ops, primitives)
    - Mesh transformation (scale, mirror, hollow, fillet, chamfer)
    - Mesh analysis (geometry, pockets, non-manifold, cross-section)
    - Mesh splitting and floating region removal
    - Mesh export/import (3MF, STL)
    - Mesh estimation (weight, print time)
    """

    @property
    def name(self) -> str:
        return "mesh_tools"

    @property
    def description(self) -> str:
        return "Mesh manipulation, analysis, repair, composition, and transformation"

    def register(self, mcp: Any) -> None:  # noqa: C901
        """Register mesh tools with the MCP server."""

        # ---------------------------------------------------------------
        # Mesh validation
        # ---------------------------------------------------------------

        @mcp.tool()
        def validate_generated_mesh(file_path: str) -> dict:
            """Validate a 3D mesh file for printing readiness.

            Checks that the file is a valid STL, OBJ, or GLB, has reasonable
            dimensions, an acceptable polygon count, and is manifold
            (watertight).

            Args:
                file_path: Path to an STL, OBJ, or GLB file.
            """
            from kiln.generation.validation import validate_mesh
            from kiln.server import _error_dict, logger

            try:
                result = validate_mesh(file_path)
                return {
                    "success": True,
                    "validation": result.to_dict(),
                    "message": "Mesh is valid." if result.valid else f"Mesh has issues: {'; '.join(result.errors)}",
                }
            except Exception as exc:
                logger.exception("Unexpected error in validate_generated_mesh")
                return _error_dict(f"Unexpected error in validate_generated_mesh: {exc}", code="INTERNAL_ERROR")

        # ---------------------------------------------------------------
        # Mesh scaling
        # ---------------------------------------------------------------

        @mcp.tool()
        def rescale_model(
            file_path: str,
            target_height_mm: float | None = None,
            scale_factor: float | None = None,
            max_dimension_mm: float | None = None,
            scale_x: float | None = None,
            scale_y: float | None = None,
            scale_z: float | None = None,
        ) -> dict:
            """Rescale an STL model to meet dimensional targets.

            Useful when a generated model is the wrong size for the printer's
            build volume or doesn't match the desired dimensions.

            **Uniform scaling** -- provide exactly ONE of:

            - ``target_height_mm``: Scale so Z-axis equals this value.
            - ``scale_factor``: Uniform multiplier (2.0 = double size).
            - ``max_dimension_mm``: Scale down so largest axis fits this limit.

            **Per-axis scaling** -- provide ``scale_x``, ``scale_y``, and/or
            ``scale_z``.  Omitted axes default to 1.0 (no change).

            Cannot combine uniform and per-axis options.

            Args:
                file_path: Path to the STL file to rescale (modified in-place).
                target_height_mm: Desired Z-axis height in mm.
                scale_factor: Uniform scale multiplier.
                max_dimension_mm: Maximum dimension on any axis.
                scale_x: Per-axis X scale factor.
                scale_y: Per-axis Y scale factor.
                scale_z: Per-axis Z scale factor.
            """
            from kiln.server import _check_auth, _error_dict, logger

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import rescale_stl

                result = rescale_stl(
                    file_path,
                    target_height_mm=target_height_mm,
                    scale_factor=scale_factor,
                    max_dimension_mm=max_dimension_mm,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    scale_z=scale_z,
                )
                response = {
                    "success": True,
                    **result,
                    "message": (
                        f"Model rescaled by x={result['scale_applied']['x']}, "
                        f"y={result['scale_applied']['y']}, z={result['scale_applied']['z']}"
                        if isinstance(result["scale_applied"], dict)
                        else f"Model rescaled by {result['scale_applied']}x."
                    ),
                }
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except ValueError as exc:
                return _error_dict(str(exc), code="INVALID_INPUT")
            except Exception as exc:
                logger.exception("Unexpected error in rescale_model")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        # ---------------------------------------------------------------
        # Mesh analysis
        # ---------------------------------------------------------------

        @mcp.tool()
        def analyze_mesh_geometry(file_path: str) -> dict:
            """Deep geometric and printability analysis of a 3D mesh.

            Goes beyond basic validation to compute volume, surface area,
            center of mass, overhang detection, connected components (floating
            parts), degenerate triangles, and a composite printability score
            (0-100).

            Use this after generating a model to understand its geometry and
            identify printability issues before sending to the slicer.

            :param file_path: Path to .stl, .obj, or .glb file.
            :returns: Dict with full mesh analysis metrics.
            """
            from kiln.server import _error_dict

            try:
                from kiln.generation.validation import analyze_mesh

                result = analyze_mesh(file_path)
                return {"success": True, **result.to_dict()}
            except Exception as exc:
                return _error_dict(f"Mesh analysis failed: {exc}", code="ANALYSIS_ERROR")

        @mcp.tool()
        def detect_mesh_pockets(
            file_path: str,
            min_depth_mm: float = 0.3,
        ) -> dict:
            """Detect pockets and cavities in a mesh before multi-part composition.

            Analyzes a base model to find recessed regions (circular or rectangular
            pockets) on top and bottom faces. Call this before compose_models or
            multi_material_print to know pocket dimensions for overlay geometry.

            :param file_path: Path to the STL file to analyze.
            :param min_depth_mm: Minimum pocket depth to report (default 0.3mm).
            :returns: Dict with pocket list, dimensions, and positions.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import detect_mesh_pockets as _detect

                result = _detect(file_path, min_depth_mm=min_depth_mm)
                return {"success": True, **result}
            except Exception as exc:
                return _error_dict(f"Pocket detection failed: {exc}", code="DETECT_ERROR")

        @mcp.tool()
        def analyze_non_manifold_edges(file_path: str) -> dict:
            """Count and classify non-manifold edges in a mesh.

            Reports boundary edges (shared by 1 triangle), T-junction edges
            (shared by 3+ triangles), and manifold edges (shared by exactly 2).

            This is the diagnostic version of the manifold check -- use it
            to understand exactly how many edges are problematic before
            deciding whether to repair.

            :param file_path: Path to mesh file (.stl, .obj, or .glb).
            :returns: Dict with edge count breakdown and watertight status.
            """
            from kiln.server import _error_dict

            try:
                from kiln.generation.validation import count_non_manifold_edges

                return {"success": True, **count_non_manifold_edges(file_path)}
            except Exception as exc:
                return _error_dict(f"Edge analysis failed: {exc}")

        @mcp.tool()
        def cross_section_view(
            file_path: str,
            plane: str = "z",
            offset_ratio: float = 0.5,
            offset_mm: str = "",
        ) -> dict:
            """Compute a 2D cross-section of a mesh at a cutting plane.

            Slices the mesh perpendicular to the chosen axis and returns
            contour polygons and cross-sectional area.  Useful for inspecting
            internal geometry (e.g., wall thickness, hole placement).

            :param file_path: Path to STL file.
            :param plane: Axis perpendicular to the cut -- "x", "y", or "z".
            :param offset_ratio: Fractional position 0.0-1.0 (default 0.5 = midpoint).
            :param offset_mm: If set, absolute position in mm (overrides offset_ratio).
            :returns: Dict with contour_count, contour_points, cross_section_area_mm2.
            """
            from kiln.server import _check_auth, _error_dict

            _check_auth("design:analyze")
            try:
                from kiln.design_reasoning import cross_section_at_plane

                kwargs: dict[str, Any] = {
                    "plane": plane,
                    "offset_ratio": offset_ratio,
                }
                if offset_mm:
                    kwargs["offset_mm"] = float(offset_mm)

                result = cross_section_at_plane(file_path, **kwargs)
                return {"success": True, **result.to_dict()}
            except FileNotFoundError as exc:
                return _error_dict(str(exc), code="FILE_NOT_FOUND")
            except ValueError as exc:
                return _error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _error_dict(f"Cross-section failed: {exc}")

        @mcp.tool()
        def mesh_quality_scorecard(file_path: str) -> dict:
            """Generate a multi-factor quality scorecard for a mesh.

            Evaluates four dimensions:
            - **Printability** (35%): overhangs, manifold, support needs
            - **Structural** (25%): aspect ratio, base stability, component count
            - **Efficiency** (20%): fill ratio, support waste
            - **Quality** (20%): triangle density, degenerate count

            Returns per-factor scores, an overall 0-100 score, and a letter
            grade (A-F).

            :param file_path: Path to mesh file (.stl, .obj, or .glb).
            :returns: Dict with scores, grade, and per-factor notes.
            """
            from kiln.server import _error_dict

            try:
                from kiln.generation.validation import design_scorecard

                return {"success": True, **design_scorecard(file_path)}
            except Exception as exc:
                return _error_dict(f"Scorecard generation failed: {exc}")

        @mcp.tool()
        def compare_mesh_versions(file_a: str, file_b: str) -> dict:
            """Compare two mesh files and report geometric differences.

            Computes volume change, surface area change, dimension deltas,
            center-of-mass shift, printability delta, and an approximate
            Hausdorff distance showing how far the meshes differ spatially.

            Useful for verifying that a repair, rescale, or regeneration
            actually improved the model.

            :param file_a: Path to the reference (original) mesh.
            :param file_b: Path to the modified mesh.
            :returns: Dict with comparison metrics and ``meshes_identical`` flag.
            """
            from kiln.server import _error_dict

            try:
                from kiln.generation.validation import compare_meshes

                return {"success": True, **compare_meshes(file_a, file_b)}
            except Exception as exc:
                return _error_dict(f"Mesh comparison failed: {exc}")

        # ---------------------------------------------------------------
        # Mesh repair
        # ---------------------------------------------------------------

        @mcp.tool()
        def repair_mesh(file_path: str, output_path: str = "") -> dict:
            """Basic mesh repair: fix degenerate triangles and bad normals (fast, safe).

            For deeper repair with hole closing and boundary edge fixes, use
            ``repair_mesh_advanced``. Removes zero-area triangles and recomputes
            face normals.  Use this on meshes from AI generation providers before
            slicing.

            :param file_path: Path to the STL file to repair.
            :param output_path: Output path.  Defaults to overwriting the input.
            :returns: Dict with repair statistics.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import repair_stl

                result = repair_stl(file_path, output_path=output_path or None)
                response = {"success": True, **result}
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _error_dict(f"Mesh repair failed: {exc}", code="REPAIR_ERROR")

        @mcp.tool()
        def repair_mesh_advanced(
            file_path: str,
            output_path: str = "",
            close_holes: bool = True,
        ) -> dict:
            """Deep mesh repair: degenerate removal + hole closing + boundary edge fixes.

            Use when ``repair_mesh`` (basic) is not enough -- e.g. mesh has open
            holes or boundary edges.  Goes beyond basic repair by finding boundary
            edges (edges shared by only one triangle) and closing small holes via
            fan triangulation.

            :param file_path: Path to the STL file.
            :param output_path: Output path.  Defaults to overwriting the input.
            :param close_holes: Whether to attempt closing holes (default True).
            :returns: Dict with repair statistics.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import repair_stl_advanced

                result = repair_stl_advanced(
                    file_path,
                    output_path=output_path or None,
                    close_holes=close_holes,
                )
                response = {"success": True, **result}
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _error_dict(f"Advanced repair failed: {exc}", code="REPAIR_ERROR")

        # ---------------------------------------------------------------
        # Mesh manipulation (transform, splice, hollow, thicken, etc.)
        # ---------------------------------------------------------------

        @mcp.tool()
        def splice_mesh_at_z(
            top_path: str,
            bottom_path: str,
            z_plane: float,
            output_path: str = "",
        ) -> dict:
            """Splice two meshes at a z-plane: top from one STL, bottom from another.

            Takes geometry ABOVE *z_plane* from *top_path* and geometry BELOW
            *z_plane* from *bottom_path*.  Triangles crossing the boundary are
            clipped cleanly.  No boolean ops -- works on non-manifold meshes.

            **Use case:** Combine a body with the correct top (e.g. logo from
            v5.3) with a body that has the correct bottom (e.g. larger pocket
            from v5.4) to create the next design iteration.

            :param top_path: STL providing geometry above z_plane.
            :param bottom_path: STL providing geometry below z_plane.
            :param z_plane: Z height (mm) where the splice happens.
            :param output_path: Output STL path. Auto-generated if empty.
            :returns: Dict with splice stats and output path.
            """
            import os
            import tempfile

            from kiln.server import _check_auth, _error_dict, logger

            if err := _check_auth("design:merge"):
                return err
            try:
                from kiln.generation.validation import splice_mesh_at_z as _splice

                if not output_path:
                    _fd, output_path = tempfile.mkstemp(suffix=".stl", prefix="kiln_splice_")
                    os.close(_fd)

                result = _splice(top_path, bottom_path, z_plane, output_path)
                response = {"success": True, **result}
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                logger.exception("splice_mesh_at_z failed")
                return _error_dict(f"Splice failed: {exc}", code="SPLICE_ERROR")

        @mcp.tool()
        def mirror_mesh_model(file_path: str, axis: str = "x", output_path: str = "") -> dict:
            """Mirror (reflect) a mesh along an axis.

            Creates a mirror image by negating coordinates on the chosen axis
            and reversing triangle winding order to preserve correct normals.

            :param file_path: Path to the STL file.
            :param axis: Axis to mirror ("x", "y", or "z", default "x").
            :param output_path: Output path (defaults to overwriting input).
            :returns: Dict with mirror info.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import mirror_mesh

                response = {
                    "success": True,
                    **mirror_mesh(file_path, axis=axis, output_path=output_path or None),
                }
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _error_dict(f"Mirror failed: {exc}")

        @mcp.tool()
        def hollow_mesh_model(
            file_path: str,
            wall_thickness_mm: float = 2.0,
            output_path: str = "",
        ) -> dict:
            """Create a hollow version of a mesh to save material.

            Generates an inner offset shell and combines it with the outer
            surface.  Reports estimated material savings.

            :param file_path: Path to the STL file.
            :param wall_thickness_mm: Wall thickness in mm (default 2.0).
            :param output_path: Output path (defaults to ``<name>_hollow.stl``).
            :returns: Dict with hollowing stats and material savings.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import hollow_mesh

                response = {
                    "success": True,
                    **hollow_mesh(
                        file_path,
                        wall_thickness_mm=wall_thickness_mm,
                        output_path=output_path or None,
                    ),
                }
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _error_dict(f"Hollowing failed: {exc}")

        @mcp.tool()
        def thicken_mesh_walls(
            file_path: str,
            amount_mm: float = 0.5,
            output_path: str = "",
        ) -> dict:
            """Thicken thin walls in a mesh by offsetting vertices outward.

            Detects thin-wall regions and pushes vertices outward along their
            averaged normals.  This is a **geometry-level fix** -- the mesh is
            surgically modified instead of regenerating from scratch.

            Use after ``predict_print_failures()`` detects ``thin_walls`` or
            after ``design_scorecard()`` flags wall thickness issues.

            :param file_path: Path to the STL file.
            :param amount_mm: Offset distance in mm (default 0.5).
            :param output_path: Output path (defaults to ``<name>_thickened.stl``).
            :returns: Dict with number of vertices modified, amounts, and output path.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import thicken_walls

                response = {
                    "success": True,
                    **thicken_walls(
                        file_path,
                        amount_mm=amount_mm,
                        output_path=output_path or None,
                    ),
                }
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _error_dict(f"Wall thickening failed: {exc}")

        @mcp.tool()
        def add_mesh_fillet(
            file_path: str,
            radius_mm: float = 1.0,
            angle_threshold_deg: float = 60.0,
            output_path: str = "",
        ) -> dict:
            """Add fillets (rounded transitions) at sharp edges.

            Detects edges where adjacent faces meet at a sharp angle and
            inserts intermediate triangles to approximate a smooth fillet.
            Reduces stress concentration at corners and improves printability.

            Use after ``design_scorecard()`` flags sharp corners or
            ``predict_print_failures()`` detects stress risers.

            :param file_path: Path to the STL file.
            :param radius_mm: Fillet radius in mm (default 1.0).
            :param angle_threshold_deg: Edges sharper than this get filleted (default 60).
            :param output_path: Output path (defaults to ``<name>_filleted.stl``).
            :returns: Dict with sharp edge count, triangles added, and output path.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import add_fillet

                response = {
                    "success": True,
                    **add_fillet(
                        file_path,
                        radius_mm=radius_mm,
                        angle_threshold_deg=angle_threshold_deg,
                        output_path=output_path or None,
                    ),
                }
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _error_dict(f"Fillet failed: {exc}")

        @mcp.tool()
        def add_mesh_chamfer(
            file_path: str,
            distance_mm: float = 0.5,
            angle_threshold_deg: float = 60.0,
            output_path: str = "",
        ) -> dict:
            """Add chamfers (flat bevels) at sharp edges.

            Detects edges where adjacent faces meet at a sharp angle and
            bevels them with a flat transition face.  Chamfers are faster
            to print than fillets and reduce stress concentration.

            :param file_path: Path to the STL file.
            :param distance_mm: Chamfer distance from edge in mm (default 0.5).
            :param angle_threshold_deg: Edges sharper than this get chamfered (default 60).
            :param output_path: Output path (defaults to ``<name>_chamfered.stl``).
            :returns: Dict with sharp edge count, triangles added, and output path.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import add_chamfer

                response = {
                    "success": True,
                    **add_chamfer(
                        file_path,
                        distance_mm=distance_mm,
                        angle_threshold_deg=angle_threshold_deg,
                        output_path=output_path or None,
                    ),
                }
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _error_dict(f"Chamfer failed: {exc}")

        @mcp.tool()
        def scale_mesh_to_fit(
            file_path: str,
            max_x_mm: float = 256.0,
            max_y_mm: float = 256.0,
            max_z_mm: float = 256.0,
            printer_id: str = "",
            output_path: str = "",
        ) -> dict:
            """Auto-scale a mesh to fit within a build volume while maintaining aspect ratio.

            Useful when a model is too large for your printer -- this uniformly
            shrinks it to the largest size that fits.

            :param file_path: Path to mesh file (.stl).
            :param max_x_mm: Maximum X dimension of build volume.
            :param max_y_mm: Maximum Y dimension of build volume.
            :param max_z_mm: Maximum Z dimension of build volume.
            :param printer_id: Optional supported printer model id.  When
                provided, printer intelligence supplies the build volume.
            :param output_path: Output path. Defaults to overwriting input.
            :returns: Dict with original/new dimensions and scale factor.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import scale_to_fit
                resolved_model_id = None
                resolved_volume = _resolve_tool_build_volume(printer_id)
                if resolved_volume:
                    resolved_model_id, build_volume = resolved_volume
                    max_x_mm, max_y_mm, max_z_mm = build_volume

                response = {
                    "success": True,
                    **scale_to_fit(
                        file_path,
                        max_x_mm=max_x_mm,
                        max_y_mm=max_y_mm,
                        max_z_mm=max_z_mm,
                        output_path=output_path or None,
                    ),
                }
                if resolved_model_id:
                    response["bed_size_source"] = "printer_intelligence"
                    response["bed_size_model_id"] = resolved_model_id
                    response["bed_dims_mm"] = [max_x_mm, max_y_mm, max_z_mm]
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except ValueError as exc:
                return _error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _error_dict(f"Scale failed: {exc}")

        @mcp.tool()
        def center_model_on_bed(
            file_path: str,
            bed_x_mm: float = 256.0,
            bed_y_mm: float = 256.0,
            printer_id: str = "",
            output_path: str = "",
        ) -> dict:
            """Center a mesh on the build plate and place at z=0.

            Translates the model so it sits centered on the bed with its
            lowest point touching the build plate.

            :param file_path: Path to the STL file.
            :param bed_x_mm: Build plate X dimension (default 256).
            :param bed_y_mm: Build plate Y dimension (default 256).
            :param printer_id: Optional supported printer model id.  When
                provided, printer intelligence supplies the bed size.
            :param output_path: Output path (defaults to overwriting input).
            :returns: Dict with translation applied.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import center_on_bed
                resolved_model_id = None
                resolved_volume = _resolve_tool_build_volume(printer_id)
                if resolved_volume:
                    resolved_model_id, build_volume = resolved_volume
                    bed_x_mm, bed_y_mm = build_volume[0], build_volume[1]

                response = {
                    "success": True,
                    **center_on_bed(
                        file_path,
                        bed_x_mm=bed_x_mm,
                        bed_y_mm=bed_y_mm,
                        output_path=output_path or None,
                    ),
                }
                if resolved_model_id:
                    response["bed_size_source"] = "printer_intelligence"
                    response["bed_size_model_id"] = resolved_model_id
                    response["bed_dims_mm"] = [bed_x_mm, bed_y_mm]
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except ValueError as exc:
                return _error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _error_dict(f"Centering failed: {exc}")

        # ---------------------------------------------------------------
        # Mesh composition (merge, boolean, primitives)
        # ---------------------------------------------------------------

        @mcp.tool()
        def compose_models(file_paths: list[str], output_path: str) -> dict:
            """Merge multiple mesh files into a single combined model.

            Concatenates all triangle geometry from the input files into one
            output STL.  No boolean operations — bodies are simply combined.
            Useful for multi-part assemblies or adding components to a design.

            **See also:** ``merge_mesh_files`` for the same operation with
            a different parameter style, or ``merge_stl`` for positional
            offset support.

            :param file_paths: List of .stl/.obj/.glb file paths to merge.
            :param output_path: Path for the combined output STL.
            :returns: Dict with merge statistics.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import compose_stls

                result = compose_stls(file_paths, output_path)
                response = {"success": True, **result}
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _error_dict(f"Composition failed: {exc}", code="COMPOSE_ERROR")

        @mcp.tool()
        def merge_mesh_files(
            file_paths: list[str],
            output_path: str,
        ) -> dict:
            """Combine multiple STL files into a single mesh file (simple concatenation).

            For positioning parts with x/y/z offsets, use ``merge_stl`` instead.
            Useful for composing multi-part designs into one printable file.

            :param file_paths: List of STL file paths to merge.
            :param output_path: Destination path for the merged file.
            :returns: Dict with merge statistics.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import merge_stl_files

                response = {"success": True, **merge_stl_files(file_paths, output_path=output_path)}
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _error_dict(f"Merge failed: {exc}")

        @mcp.tool()
        def boolean_mesh_op(
            operation: str,
            file_paths: list[str],
            output_path: str = "",
        ) -> dict:
            """Perform a CSG boolean operation on two or more STL meshes.

            Uses OpenSCAD's boolean engine to compute:
            - **union**: combine multiple bodies into one
            - **difference**: subtract subsequent bodies from the first
            - **intersection**: keep only the overlapping region

            Requires OpenSCAD installed on the system.

            **Use cases:**
            - Subtract a cylinder from a block to create a hole
            - Combine multiple parts into a single printable body
            - Create complex shapes from simple primitives

            :param operation: ``"union"``, ``"difference"``, or ``"intersection"``.
            :param file_paths: List of STL file paths (minimum 2).
            :param output_path: Output path (defaults to a temp file).
            :returns: Dict with result path, operation, and triangle count.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.openscad import boolean_mesh_operation

                response = {
                    "success": True,
                    **boolean_mesh_operation(
                        operation,
                        file_paths,
                        output_path=output_path or None,
                    ),
                }
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except FileNotFoundError as exc:
                return _error_dict(str(exc), code="FILE_NOT_FOUND")
            except ValueError as exc:
                return _error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _error_dict(f"Boolean operation failed: {exc}")

        @mcp.tool()
        def compose_part_from_primitives(
            operations: list[dict],
            output_path: str = "",
            center_on_bed: bool = True,
            bed_x_mm: float = 256.0,
            bed_y_mm: float = 256.0,
            printer_id: str = "",
        ) -> dict:
            """Build a functional part by composing geometric primitives with booleans.

            The **CAD-aware generation path** -- instead of asking text-to-mesh AI
            to guess at geometry, describe parts as a tree of primitives combined
            with boolean operations. Produces exact, deterministic, functional parts.

            **SAFETY DEFAULT (changed 2026-04-15):** ``center_on_bed=True`` is the
            default.  OpenSCAD primitives are natively centered on the model origin
            (``cylinder(h,r)`` produces geometry centered on X/Y = (0,0), which
            means half the geometry lives at NEGATIVE X/Y).  Sending such an STL
            to most FDM printers (Bambu, Prusa, Ender, Creality) — whose bed
            origin is the front-left corner — causes the nozzle to drive off-bed
            into the purge/wipe assembly on layer 1.  This happened once on a
            Bambu A1 (incident #0, 2026-04-15, nearly damaged the printer).

            With ``center_on_bed=True`` the output STL is translated so it sits
            centered on the build plate and its lowest point touches z=0.  Set
            ``center_on_bed=False`` only if your downstream flow expects
            origin-centered geometry (e.g. further CAD composition).

            **Operation format** -- each item is either a primitive or boolean:

            Primitive: ``{"type": "primitive", "shape": "<shape>",
            "params": {...}, "translate": [x,y,z], "rotate": [rx,ry,rz]}``

            Boolean: ``{"type": "boolean", "operation": "union|difference|intersection",
            "children": [op1, op2, ...]}``

            **Primitive shapes and params:**
            - cube: ``{"size": [x,y,z]}`` or ``{"size": scalar}``
            - cylinder: ``{"h": height, "r": radius}`` or ``{"h", "r1", "r2"}``
            - sphere: ``{"r": radius}``
            - cone: ``{"h": height, "r1": bottom_r, "r2": top_r}``
            - torus: ``{"major_r": ring_radius, "minor_r": tube_radius}``
            - wedge: ``{"width": w, "depth": d, "height": h}``
            - hex_prism: ``{"r": radius, "h": height}``  -- hexagonal (for nuts)
            - text: ``{"text": "string", "size": 10, "depth": 2}``
            - rounded_cube: ``{"size": [x,y,z], "radius": 1}``
            - pipe: ``{"h": height, "outer_r": 10, "inner_r": 8}``

            Requires OpenSCAD installed on the system.

            :param operations: List of operation dicts (primitive/boolean tree).
            :param output_path: Output path (defaults to temp file).
            :param center_on_bed: Translate output to bed-center (default True).
            :param bed_x_mm: Build plate X dimension for centering (default 256).
            :param bed_y_mm: Build plate Y dimension for centering (default 256).
            :param printer_id: Optional supported printer model id.  When
                provided, printer intelligence supplies the bed size.
            :returns: Dict with result path, SCAD code, triangle count, and
                (if centered) ``bed_centered=True`` + applied translation.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.openscad import compose_from_primitives
                resolved_model_id = None
                resolved_volume = _resolve_tool_build_volume(printer_id)
                if resolved_volume:
                    resolved_model_id, build_volume = resolved_volume
                    bed_x_mm, bed_y_mm = build_volume[0], build_volume[1]

                result = compose_from_primitives(
                    operations,
                    output_path=output_path or None,
                )
                response: dict = {"success": True, **result}

                if center_on_bed:
                    stl_path = result.get("path")
                    if stl_path:
                        try:
                            from kiln.generation.validation import center_on_bed as _center
                            centered = _center(
                                stl_path,
                                bed_x_mm=bed_x_mm,
                                bed_y_mm=bed_y_mm,
                                output_path=None,  # overwrite
                            )
                            response["bed_centered"] = True
                            response["bed_dims_mm"] = [bed_x_mm, bed_y_mm]
                            if resolved_model_id:
                                response["bed_size_source"] = "printer_intelligence"
                                response["bed_size_model_id"] = resolved_model_id
                            response["translation_applied"] = centered.get(
                                "translation"
                            ) or centered.get("translate")
                        except Exception as exc:
                            # Centering is best-effort; leave STL untouched
                            # and surface a warning.  Downstream slicer gate
                            # will still catch off-bed geometry.
                            response["bed_centered"] = False
                            response.setdefault("warnings", []).append(
                                f"bed-centering failed: {exc}"
                            )
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except ValueError as exc:
                return _error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _error_dict(f"Composition failed: {exc}")

        # ---------------------------------------------------------------
        # Mesh splitting and cleanup
        # ---------------------------------------------------------------

        @mcp.tool()
        def split_mesh_by_component(
            file_path: str,
            output_dir: str = "",
        ) -> dict:
            """Split a multi-component mesh into separate STL files.

            Identifies disconnected bodies (components) using shared-edge
            analysis and writes each as a separate file.

            :param file_path: Path to mesh file (.stl).
            :param output_dir: Directory for output files. Defaults to input directory.
            :returns: Dict with component count and file paths.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                import os

                from kiln.generation.validation import split_by_component

                response = {
                    "success": True,
                    **split_by_component(
                        file_path,
                        output_dir=output_dir or None,
                    ),
                }
                # Render the LARGEST split component as the canonical
                # preview — that's the dominant piece and usually what
                # the user means when they ask "what did we split into?".
                # Other components live in response["file_paths"] for the
                # user to inspect_design individually.  Falls back to the
                # input mesh if no components were emitted (defensive;
                # shouldn't happen on the success path).
                _file_paths = response.get("file_paths") or []
                _preview_source = (
                    max(_file_paths, key=os.path.getsize)
                    if _file_paths
                    else file_path
                )
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", source_path=_preview_source,
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _error_dict(f"Split failed: {exc}")

        @mcp.tool()
        def remove_mesh_floating_regions(
            file_path: str,
            output_path: str = "",
            keep_largest: bool = True,
            min_triangle_pct: float = 1.0,
        ) -> dict:
            """Remove small disconnected components (floating geometry).

            Downloads and marketplace models often contain support pillars,
            internal fragments, or other floating geometry.  This tool
            identifies connected components and removes the small ones.

            :param file_path: Path to the STL file.
            :param output_path: Output path (defaults to overwriting input).
            :param keep_largest: Keep only the largest component (default True).
            :param min_triangle_pct: Min triangle % to keep (when keep_largest=False).
            :returns: Dict with removal statistics.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import remove_floating_regions

                response = {
                    "success": True,
                    **remove_floating_regions(
                        file_path,
                        output_path=output_path or None,
                        keep_largest=keep_largest,
                        min_triangle_pct=min_triangle_pct,
                    ),
                }
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _error_dict(f"Floating region removal failed: {exc}")

        @mcp.tool()
        def simplify_mesh_model(
            file_path: str,
            target_ratio: float = 0.5,
            output_path: str = "",
        ) -> dict:
            """Reduce mesh triangle count for faster preview or smaller files.

            Uses vertex-clustering decimation to merge nearby vertices.
            The result is a lower-resolution version of the same shape.

            :param file_path: Path to the STL file.
            :param target_ratio: Target fraction of original triangles (0.01-1.0).
            :param output_path: Output path (defaults to ``<name>_simplified.stl``).
            :returns: Dict with original/simplified triangle counts and reduction percentage.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import simplify_mesh

                response = {
                    "success": True,
                    **simplify_mesh(
                        file_path,
                        target_ratio=target_ratio,
                        output_path=output_path or None,
                    ),
                }
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("path",),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _error_dict(f"Mesh simplification failed: {exc}")

        # ---------------------------------------------------------------
        # Mesh export / import
        # ---------------------------------------------------------------

        @mcp.tool()
        def export_model_3mf(file_path: str, output_path: str = "") -> dict:
            """Export a mesh to 3MF format (preferred by modern slicers).

            Converts STL/OBJ/GLB to 3MF, a ZIP-based XML format used by
            PrusaSlicer, OrcaSlicer, and Bambu Studio.  3MF is more compact
            and supports metadata better than STL.

            :param file_path: Path to the input mesh file.
            :param output_path: Output 3MF path.  Auto-generated if empty.
            :returns: Dict with the output file path.
            """
            import os

            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import export_3mf

                out = export_3mf(file_path, output_path=output_path or None)
                file_size = os.path.getsize(out)
                return {
                    "success": True,
                    "path": out,
                    "file_size_bytes": file_size,
                    "message": f"Exported to 3MF ({file_size} bytes).",
                }
            except Exception as exc:
                return _error_dict(f"3MF export failed: {exc}", code="EXPORT_ERROR")

        @mcp.tool()
        def extract_model_from_3mf(file_path: str, output_path: str = "") -> dict:
            """Extract the embedded 3D model from a .3mf or .gcode.3mf file to STL.

            3MF files are ZIP archives containing XML mesh geometry.  This tool
            parses the embedded model, extracts all mesh objects, and writes a
            binary STL file ready for slicing, multi-copy printing, or further
            mesh operations.

            .. note::
                For extracting a single object's **G-code** from a multi-object
                Bambu .gcode.3mf file, use ``extract_plate_object`` instead.
                Use ``list_plate_objects`` to discover available objects.

            Works with both standard 3MF files and Bambu Studio .gcode.3mf files
            (which bundle both G-code and the source model).  When multiple
            objects exist they are merged into a single STL.

            :param file_path: Path to the .3mf or .gcode.3mf file.
            :param output_path: Output STL path (auto-generated if empty).
            :returns: Dict with output path, triangle/vertex counts, and dimensions.
            """
            from kiln.server import _check_auth, _error_dict

            if err := _check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import (
                    extract_model_from_3mf as _extract,
                )

                result = _extract(file_path, output_path=output_path or None)
                response = {"success": True, **result}
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(response, level="quick")
                except ImportError:
                    return response
            except FileNotFoundError as exc:
                return _error_dict(str(exc), code="FILE_NOT_FOUND")
            except Exception as exc:
                msg = f"3MF extraction failed: {exc}"
                result = _error_dict(msg, code="EXTRACT_ERROR")
                if "no mesh geometry" in str(exc).lower():
                    result["hint"] = (
                        "This .gcode.3mf has no mesh data. Use list_plate_objects() "
                        "to see what objects are on the plate, then "
                        "extract_plate_object() to extract one object's G-code."
                    )
                return result

        # ---------------------------------------------------------------
        # Mesh estimation (weight, print time)
        # ---------------------------------------------------------------

        @mcp.tool()
        def estimate_mesh_weight(
            file_path: str,
            material: str = "pla",
            infill_percent: float = 20.0,
            wall_thickness_mm: float = 1.2,
        ) -> dict:
            """Estimate the printed weight of an STL file.

            Uses the divergence theorem to compute mesh volume, then applies
            material density, infill ratio, and shell fraction for a realistic
            weight estimate.

            :param file_path: Path to an STL file.
            :param material: Material name (pla, abs, petg, tpu, nylon, etc.).
            :param infill_percent: Infill percentage 0-100 (default 20).
            :param wall_thickness_mm: Perimeter wall thickness in mm (default 1.2).
            :returns: Dict with volume, weight estimates, bounding box.
            """
            from kiln.server import _check_auth, _error_dict

            _check_auth("design:analyze")
            try:
                from kiln.design_reasoning import estimate_weight

                result = estimate_weight(
                    file_path,
                    material=material,
                    infill_percent=infill_percent,
                    wall_thickness_mm=wall_thickness_mm,
                )
                return {"success": True, **result.to_dict()}
            except FileNotFoundError as exc:
                return _error_dict(str(exc), code="FILE_NOT_FOUND")
            except ValueError as exc:
                return _error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _error_dict(f"Weight estimation failed: {exc}")

        @mcp.tool()
        def estimate_mesh_print_time(
            file_path: str,
            layer_height_mm: float = 0.2,
            print_speed_mm_s: float = 60.0,
            material: str = "pla",
        ) -> dict:
            """Rough print time estimate from mesh geometry (STL/OBJ/GLB).

            Uses model height, surface area, and layer count to approximate
            print duration. This is a ballpark estimate -- actual time depends
            on slicer settings, infill density, supports, and acceleration.

            Unlike estimate_print_time (which uses slicer profiles), this
            works directly on mesh files before slicing.

            :param file_path: Path to mesh file.
            :param layer_height_mm: Layer height for slicing.
            :param print_speed_mm_s: Average print speed in mm/s.
            :param material: Material hint (affects per-layer overhead).
            :returns: Dict with estimated time, layer count, and note.
            """
            from kiln.server import _error_dict

            try:
                from kiln.generation.validation import estimate_print_time_from_mesh

                return {
                    "success": True,
                    **estimate_print_time_from_mesh(
                        file_path,
                        layer_height_mm=layer_height_mm,
                        print_speed_mm_s=print_speed_mm_s,
                        material=material,
                    ),
                }
            except Exception as exc:
                return _error_dict(f"Time estimate failed: {exc}")


plugin = _MeshToolsPlugin()
