"""Design reasoning, structural analysis, and plate arrangement tools plugin.

Extracts design-reasoning-domain MCP tools from server.py into a focused
plugin module.  All tools delegate to helpers in ``kiln.design_reasoning``,
``kiln.generation.validation``, ``kiln.design_intelligence``, and
``kiln.multicolor_3mf`` — accessed via lazy imports inside function bodies.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


def _attach_brief_to_iteration_result(
    brief_id: str, best_result: dict[str, Any],
) -> None:
    """Write a ``design_brief:<id>`` intent sidecar next to the best mesh.

    Best-effort: when kiln-pro isn't installed, the saved goal can't be
    loaded, or the sidecar write fails for any reason, silently no-op
    — the iteration result is still valid.  Same pattern as the audit
    honor-gate hook in ``kiln.original_design``.

    Reads the mesh path out of either ``best_result["result"]["local_path"]``
    (download_result envelope) or ``best_result["job"]["local_path"]``.
    """
    mesh_path: str | None = None
    for nested in ("result", "job"):
        block = best_result.get(nested)
        if isinstance(block, dict):
            v = block.get("local_path")
            if isinstance(v, str) and v:
                mesh_path = v
                break
    if not mesh_path:
        return
    try:
        from kiln_pro.design_brief.explicit_attach import (
            attach_brief_id_to_result,
        )
        attach_brief_id_to_result(brief_id, {"local_path": mesh_path})
    except Exception:
        _logger.debug(
            "iterate_design: brief sidecar attach skipped (best-effort)",
            exc_info=True,
        )


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
            "explicit bed/plate dimensions, or use a supported printer model id."
        )
    return resolved


class _DesignReasoningToolsPlugin:
    """Structural analysis, design reinforcement, plate arrangement, and
    template optimisation tools.

    Tools:
        - analyze_structural_risks
        - recommend_design_reinforcements
        - assess_load_bearing
        - design_improvement_plan
        - apply_design_reinforcements
        - infer_print_settings
        - design_advisor
        - arrange_parts_on_plate
        - auto_arrange_parts_on_plate
        - optimize_template_params
        - solve_template_constraints
        - iterate_design
        - optimize_print_orientation
        - check_print_readiness
        - estimate_support_material
    """

    @property
    def name(self) -> str:
        return "design_reasoning_tools"

    @property
    def description(self) -> str:
        return "Structural analysis, design reinforcement, plate arrangement, and template optimisation tools"

    def register(self, mcp: Any) -> None:  # noqa: C901, PLR0915
        """Register design reasoning tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # analyze_structural_risks
        # ------------------------------------------------------------------

        @mcp.tool()
        def analyze_structural_risks(
            file_path: str,
            min_cross_section_mm2: float = 4.0,
            sharp_angle_threshold_deg: float = 60.0,
        ) -> dict:
            """Analyze an STL mesh for structural weak points.

            Goes beyond printability to find **structural** risks:
            - **thin_neck**: narrow cross-sections that will snap under load
            - **stress_concentration**: abrupt section changes that focus stress
            - **cantilever**: unsupported overhanging geometry
            - **sharp_corner**: concave edges that initiate cracks
            - **insufficient_base**: topple risk from height-to-base ratio
            - **weak_layer_adhesion**: overhangs in structurally critical areas

            Returns risk locations as (x, y, z) coordinates in mm so agents
            can reason about *where* problems are, not just *that* they exist.

            :param file_path: Path to the STL file.
            :param min_cross_section_mm2: Minimum safe cross-section area (default 4).
            :param sharp_angle_threshold_deg: Angle for sharp edge detection (default 60).
            :returns: Dict with ``risks`` list, each containing location, severity, and description.
            """
            try:
                from kiln.design_reasoning import analyze_structural_risks as _analyze

                risks = _analyze(
                    file_path,
                    min_cross_section_mm2=min_cross_section_mm2,
                    sharp_angle_threshold_deg=sharp_angle_threshold_deg,
                )
                return {
                    "success": True,
                    "risk_count": len(risks),
                    "critical_count": sum(1 for r in risks if r.severity == "critical"),
                    "warning_count": sum(1 for r in risks if r.severity == "warning"),
                    "risks": [r.to_dict() for r in risks],
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _srv._error_dict(f"Structural analysis failed: {exc}")

        # ------------------------------------------------------------------
        # recommend_design_reinforcements
        # ------------------------------------------------------------------

        @mcp.tool()
        def recommend_design_reinforcements(
            file_path: str,
            min_cross_section_mm2: float = 4.0,
        ) -> dict:
            """Recommend specific reinforcements for an STL mesh.

            Analyzes geometry to find structural risks, then generates actionable
            recommendations with **specific locations** and **estimated strength gains**:
            - **gusset**: triangular support at cantilever bases (3-10x stronger)
            - **fillet**: smooth transitions at stress concentrations (30-60% gain)
            - **thicken_wall**: add material at thin necks (2-5x gain)
            - **add_base**: widen the base for stability
            - **reorient**: change print orientation for layer strength

            Each recommendation includes the coordinates where the reinforcement
            should be applied and which Kiln tool to use (e.g., ``add_mesh_fillet()``).

            :param file_path: Path to the STL file.
            :param min_cross_section_mm2: Minimum safe cross-section area.
            :returns: Dict with ``reinforcements`` list.
            """
            try:
                from kiln.design_reasoning import recommend_reinforcements as _recommend

                recs = _recommend(file_path, min_cross_section_mm2=min_cross_section_mm2)
                return {
                    "success": True,
                    "recommendation_count": len(recs),
                    "reinforcements": [r.to_dict() for r in recs],
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _srv._error_dict(f"Reinforcement analysis failed: {exc}")

        # ------------------------------------------------------------------
        # assess_load_bearing
        # ------------------------------------------------------------------

        @mcp.tool()
        def assess_load_bearing(file_path: str) -> dict:
            """Analyze load-bearing characteristics of a mesh from its geometry.

            Infers structural behavior by analyzing surface normals, shape type,
            and cross-section distribution:
            - **primary_load_axis**: which direction the part resists force
            - **load_surfaces**: which surfaces bear load (with area fractions)
            - **weak_axis**: the most vulnerable direction for failure
            - **recommended_print_orientation**: how to orient for maximum strength
            - **layer_direction_concern**: how FDM layers affect structural integrity

            This is the difference between "PLA is good for prototypes" (lookup)
            and "this bracket should be printed on its side because the load path
            crosses layer boundaries" (geometric reasoning).

            :param file_path: Path to the STL file.
            :returns: Dict with load analysis.
            """
            try:
                from kiln.design_reasoning import assess_load_bearing as _assess

                analysis = _assess(file_path)
                return {
                    "success": True,
                    **analysis.to_dict(),
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _srv._error_dict(f"Load analysis failed: {exc}")

        # ------------------------------------------------------------------
        # design_improvement_plan
        # ------------------------------------------------------------------

        @mcp.tool()
        def design_improvement_plan(
            file_path: str,
            min_cross_section_mm2: float = 4.0,
            sharp_angle_threshold_deg: float = 60.0,
        ) -> dict:
            """Generate a complete structural improvement plan for a design.

            The **full design reasoning pipeline** — combines risk analysis,
            reinforcement recommendations, and load analysis into one actionable
            report with an overall structural score (0-100, A-F grade).

            This is the tool that makes Kiln a **design advisor**, not just a
            geometry validator. It answers: "This bracket needs a gusset at the
            load point" — not just "the part has thin walls."

            The plan includes:
            1. **Risks**: all structural weak points with locations and severity
            2. **Reinforcements**: specific fixes with estimated strength gains
            3. **Load analysis**: how the part handles forces, best print orientation
            4. **Score**: overall structural grade with summary

            :param file_path: Path to the STL file.
            :param min_cross_section_mm2: Minimum safe cross-section area.
            :param sharp_angle_threshold_deg: Angle for sharp edge detection.
            :returns: Complete improvement plan as dict.
            """
            try:
                from kiln.design_reasoning import generate_improvement_plan as _plan

                plan = _plan(
                    file_path,
                    min_cross_section_mm2=min_cross_section_mm2,
                    sharp_angle_threshold_deg=sharp_angle_threshold_deg,
                )
                return {
                    "success": True,
                    **plan.to_dict(),
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _srv._error_dict(f"Improvement plan failed: {exc}")

        # ------------------------------------------------------------------
        # apply_design_reinforcements
        # ------------------------------------------------------------------

        @mcp.tool()
        def apply_design_reinforcements(
            file_path: str,
            output_path: str = "",
            fillet_radius_mm: float = 1.5,
            wall_thicken_mm: float = 0.6,
            base_height_mm: float = 2.0,
        ) -> dict:
            """Analyze a mesh for structural risks, then auto-apply fixes.

            This is the **one-step design hardening tool** — it runs the full
            structural analysis pipeline, then applies every applicable fix:

            - **Thin necks** → thickened walls (+material at narrow sections)
            - **Sharp corners** → filleted edges (stress concentration eliminated)
            - **Insufficient base** → wider base plate (stabilizing geometry added)
            - **Cantilevers** → triangular gusset ribs (deflection reduced 3-10x)

            Returns a before/after structural score so agents can see the
            improvement.  Reinforcements that can't be auto-applied (like
            ``reorient``) are listed in ``skipped`` with guidance.

            Requires OpenSCAD for base plate and gusset operations.

            :param file_path: Path to the STL file to reinforce.
            :param output_path: Output path (defaults to ``<name>_reinforced.stl``).
            :param fillet_radius_mm: Fillet radius for sharp corners (default 1.5).
            :param wall_thicken_mm: Amount to add to thin walls (default 0.6).
            :param base_height_mm: Height of stabilizing base plate (default 2.0).
            :returns: Dict with before/after scores, applied/skipped reinforcements.
            """
            if err := _srv._check_auth("generate"):
                return err
            try:
                from kiln.design_reasoning import apply_reinforcements

                result = apply_reinforcements(
                    file_path,
                    output_path=output_path or None,
                    fillet_radius_mm=fillet_radius_mm,
                    wall_thicken_mm=wall_thicken_mm,
                    base_height_mm=base_height_mm,
                )
                response = {"success": True, **result.to_dict()}
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(response, level="quick")
                except ImportError:
                    return response
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _srv._error_dict(f"Reinforcement application failed: {exc}")

        # ------------------------------------------------------------------
        # infer_print_settings
        # ------------------------------------------------------------------

        @mcp.tool()
        def infer_print_settings(
            file_path: str,
            material: str = "PLA",
        ) -> dict:
            """Infer optimal slicer settings from structural analysis.

            Bridges the gap between **design analysis** and **print success**.
            Analyzes the mesh for structural risks, then recommends concrete
            slicer parameters (perimeters, infill, supports, brim, layer height)
            tuned to compensate for the design's weaknesses.

            **Examples of what it catches:**

            - Thin neck detected → increase perimeters to 4+
            - Cantilever overhangs → enable tree supports
            - High center of gravity → add brim for bed adhesion
            - Stress concentrations → switch to gyroid infill at 50%+
            - Sharp corners → fine layer height for detail

            Material-specific: defaults vary by PLA/PETG/ABS/Nylon/TPU/ASA/PC.

            :param file_path: Path to the STL file.
            :param material: Filament type (PLA, PETG, ABS, Nylon, TPU, ASA, PC).
            :returns: Dict with perimeters, infill, supports, brim, layer height,
                      orientation, special notes, and confidence level.
            """
            try:
                from kiln.design_reasoning import infer_print_settings as _infer

                result = _infer(file_path, material=material)
                return {"success": True, **result.to_dict()}
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _srv._error_dict(f"Print settings inference failed: {exc}")

        # ------------------------------------------------------------------
        # design_advisor
        # ------------------------------------------------------------------

        @mcp.tool()
        def design_advisor(
            prompt: str, printer_model: str = "", material: str = "",
        ) -> dict:
            """Ask which generation method to use for a design idea (triage tool — call FIRST).

            Analyzes the design prompt and recommends:
            - Which generation approach to use (template, OpenSCAD, or AI)
            - Which template matches (if any)
            - Material recommendations
            - Key constraints to consider
            - Estimated complexity

            :param prompt: Text description of the desired object.
            :param printer_model: Optional printer model for constraints.
            :param material: Optional material the user has already named
                ("for ABS") — tunes the constraint analysis to it.
            :returns: Dict with recommendations.
            """
            prompt_lower = prompt.lower()
            recommendations: dict[str, Any] = {"prompt": prompt}

            # Check for template matches.
            #
            # This used to carry its own six-entry keyword table, which
            # could reach 4 of the 65 parametric parts — and two of its
            # six ids, "box_with_lid" and "nameplate", were not in the
            # library at all, so a prompt saying "box" recommended a
            # template that generate_from_template rejects with
            # NOT_FOUND.  It now goes through the same search every
            # other discovery door uses, so the whole library is
            # reachable here and a recommended id is always renderable.
            try:
                from kiln.design_intelligence import (
                    find_generatable_design_templates,
                )

                matching_templates: list[dict[str, str]] = [
                    {
                        "template_id": tpl["template_id"],
                        "display_name": tpl["display_name"],
                        "description": tpl["description"],
                    }
                    for tpl in find_generatable_design_templates(prompt)[:5]
                ]
                recommendations["matching_templates"] = matching_templates
            except Exception:
                recommendations["matching_templates"] = []

            # Determine best approach
            is_geometric = any(
                w in prompt_lower
                for w in [
                    "box",
                    "bracket",
                    "mount",
                    "holder",
                    "clip",
                    "hook",
                    "shelf",
                    "stand",
                    "frame",
                    "enclosure",
                    "gear",
                    "hinge",
                    "screw",
                    "nut",
                    "bolt",
                    "washer",
                    "spacer",
                    "bushing",
                ]
            )
            is_organic = any(
                w in prompt_lower
                for w in [
                    "figure",
                    "sculpture",
                    "animal",
                    "character",
                    "face",
                    "statue",
                    "bust",
                    "organic",
                    "creature",
                    "dragon",
                    "plant",
                    "flower",
                    "tree",
                    "body",
                ]
            )
            is_simple = len(prompt.split()) < 8 and not is_organic

            if recommendations["matching_templates"]:
                approach = "template"
                approach_reason = (
                    f"Template '{recommendations['matching_templates'][0]['template_id']}' "
                    f"matches your request. Templates produce reliable, parameterized designs."
                )
                confidence = "high"
            elif is_geometric and not is_organic:
                approach = "openscad"
                approach_reason = (
                    "Geometric/mechanical objects work best with OpenSCAD parametric code. "
                    "Have the AI write OpenSCAD code, then compile it locally."
                )
                confidence = "high"
            elif is_organic:
                approach = "meshy"
                approach_reason = (
                    "Organic/sculptural objects work best with AI mesh generation. "
                    "Meshy or similar providers excel at organic shapes."
                )
                confidence = "medium"
            else:
                approach = "openscad" if is_simple else "meshy"
                approach_reason = (
                    "Could work with either approach. OpenSCAD for precise dimensions, "
                    "Meshy for complex/artistic shapes."
                )
                confidence = "low"

            recommendations["recommended_approach"] = approach
            recommendations["approach_reason"] = approach_reason
            recommendations["confidence"] = confidence

            # Complexity estimate
            word_count = len(prompt.split())
            if word_count < 5:
                complexity = "simple"
            elif word_count < 15:
                complexity = "moderate"
            else:
                complexity = "complex"
            recommendations["estimated_complexity"] = complexity

            # Material recommendations
            try:
                from kiln.design_intelligence import get_design_constraints

                brief = get_design_constraints(
                    prompt,
                    material=material or None,
                    printer_model=printer_model or None,
                )
                if brief.recommended_material:
                    mat = brief.recommended_material
                    recommendations["recommended_material"] = {
                        "name": mat.material.display_name if mat.material else mat.material_id,
                        "reason": mat.reason,
                    }
                if brief.combined_rules:
                    rules = brief.combined_rules
                    key_constraints = []
                    if rules.get("min_wall_thickness_mm"):
                        key_constraints.append(f"Min wall: {rules['min_wall_thickness_mm']}mm")
                    if rules.get("max_unsupported_overhang_deg"):
                        key_constraints.append(
                            f"Max overhang: {rules['max_unsupported_overhang_deg']}\u00b0"
                        )
                    recommendations["key_constraints"] = key_constraints
            except Exception:
                pass

            # Suggested workflow
            if approach == "template":
                tid = recommendations["matching_templates"][0]["template_id"]
                recommendations["suggested_workflow"] = [
                    f"1. list_design_templates() \u2014 review '{tid}' parameters",
                    f"2. generate_from_template('{tid}', ...) \u2014 customize parameters",
                    "3. analyze_mesh_geometry(file) \u2014 verify printability",
                    "4. estimate_print_time(file) \u2014 check time/filament",
                    "5. Upload and print",
                ]
            elif approach == "openscad":
                recommendations["suggested_workflow"] = [
                    "1. Write OpenSCAD code for the design",
                    "2. validate_openscad_code(code) \u2014 check for errors",
                    "3. generate_model(code, provider='openscad') \u2014 compile",
                    "4. analyze_mesh_geometry(file) \u2014 verify printability",
                    "5. optimize_print_orientation(file) \u2014 if needed",
                    "6. estimate_print_time(file) \u2014 check time/filament",
                    "7. Upload and print",
                ]
            else:
                recommendations["suggested_workflow"] = [
                    "1. generate_model(prompt, provider='meshy') \u2014 AI generation",
                    "2. await_generation(job_id) \u2014 wait for completion",
                    "3. download_generated_model(job_id) \u2014 auto-converts to STL",
                    "4. analyze_mesh_geometry(file) \u2014 verify printability",
                    "5. repair_mesh(file) \u2014 fix common issues",
                    "6. optimize_print_orientation(file) \u2014 minimize supports",
                    "7. estimate_print_time(file) \u2014 check time/filament",
                    "8. Upload and print",
                ]

            return {"success": True, **recommendations}

        # ------------------------------------------------------------------
        # arrange_parts_on_plate
        # ------------------------------------------------------------------

        @mcp.tool()
        def arrange_parts_on_plate(
            file_paths: str,
            plate_width_mm: float = 256.0,
            plate_depth_mm: float = 256.0,
            spacing_mm: float = 5.0,
            copies: str = "",
            printer_id: str = "",
        ) -> dict:
            """Pack multiple STL files onto a virtual build plate.

            Uses greedy bottom-left bin-packing (largest parts first) to
            efficiently arrange parts with configurable spacing. Reports
            which parts fit, which overflow, and plate utilization.

            Supports printing multiple copies of parts via the copies parameter.

            :param file_paths: JSON array of file paths, e.g. ``["/tmp/a.stl", "/tmp/b.stl"]``.
            :param plate_width_mm: Build plate width in mm (default 256).
            :param plate_depth_mm: Build plate depth in mm (default 256).
            :param spacing_mm: Minimum gap between parts in mm (default 5).
            :param copies: Optional JSON dict of filename->count, e.g. ``{"part.stl": 3}``.
            :param printer_id: Optional supported printer model id.  When
                provided, printer intelligence supplies the plate size.
                Keep passing it downstream — a later
                ``compose_multicolor_3mf`` without it judges the layout
                against a 256mm default plate.
            :returns: Dict with arranged_parts, overflow_parts, plate_utilization, summary.
            """
            _srv._check_auth("design:arrange")
            try:
                import json as _json

                from kiln.design_reasoning import arrange_on_plate

                parsed_paths = _json.loads(file_paths)
                parsed_copies = _json.loads(copies) if copies else None
                resolved_model_id = None
                resolved_volume = _resolve_tool_build_volume(printer_id)
                if resolved_volume:
                    resolved_model_id, build_volume = resolved_volume
                    plate_width_mm, plate_depth_mm = build_volume[0], build_volume[1]

                result = arrange_on_plate(
                    parsed_paths,
                    plate_width_mm=plate_width_mm,
                    plate_depth_mm=plate_depth_mm,
                    spacing_mm=spacing_mm,
                    copies=parsed_copies,
                )
                response = {"success": True, **result.to_dict()}
                if resolved_model_id:
                    response["bed_size_source"] = "printer_intelligence"
                    response["bed_size_model_id"] = resolved_model_id
                    response["bed_dims_mm"] = [plate_width_mm, plate_depth_mm]
                return response
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _srv._error_dict(f"Plate arrangement failed: {exc}")

        # ------------------------------------------------------------------
        # auto_arrange_parts_on_plate
        # ------------------------------------------------------------------

        @mcp.tool()
        def auto_arrange_parts_on_plate(
            part_specs: list[dict],
            plate_width: float = 256.0,
            plate_depth: float = 256.0,
            gap_mm: float = 5.0,
            printer_id: str = "",
        ) -> dict:
            """Calculate non-overlapping XY positions for multiple parts on a print plate.

            Use this **before** :func:`compose_multicolor_3mf` when you have multiple
            separate objects (e.g., two coasters) to print in one job.  Parts that
            share the same ``group`` index are treated as a multi-color unit and
            placed at the *same* XY position (they overlap intentionally).

            Returns a list of positioned part specs ready to pass directly to
            ``compose_multicolor_3mf`` — pass the SAME ``printer_id`` to that
            call: the composer re-centres a group it judges off ITS plate
            (256mm by default), so composing a layout packed for a different
            bed without the printer undoes this placement.

            Arrangement strategy (free tier): simple left-to-right row layout.  For
            maximum plate density (2D bin-packing), use kiln-pro.

            Example -- two coasters, each with a body + QR layer::

                positioned = auto_arrange_parts_on_plate(part_specs=[
                    {"stl_path": "/tmp/c1_body.stl", "extruder": 1, "group": 0, "material": "PLA Grey"},
                    {"stl_path": "/tmp/c1_qr.stl",   "extruder": 2, "group": 0, "material": "PLA Black"},
                    {"stl_path": "/tmp/c2_body.stl",  "extruder": 1, "group": 1, "material": "PLA Grey"},
                    {"stl_path": "/tmp/c2_qr.stl",    "extruder": 2, "group": 1, "material": "PLA Black"},
                ], plate_width=256, plate_depth=256, gap_mm=5)
                # -> each part now has "x", "y" set; pass to compose_multicolor_3mf

            Args:
                part_specs: List of dicts, each with:

                    * ``stl_path`` (str) -- absolute path to the STL
                    * ``extruder`` (int) -- 1-indexed AMS slot
                    * ``group`` (int, optional) -- parts sharing a group get the same
                      XY position (multi-color unit).  Default: each part is its own group.
                    * ``name`` (str, optional) -- label in slicer
                    * ``color`` (str, optional) -- hex preview color
                    * ``material`` (str, optional) -- filament label (also triggers
                      compatibility checks when passed to compose_multicolor_3mf)

                plate_width: Print plate X dimension in mm (default 256 for
                    legacy callers without a printer id).
                plate_depth: Print plate Y dimension in mm (default 256 for
                    legacy callers without a printer id).
                gap_mm: Minimum spacing between groups in mm.
                printer_id: Optional supported printer model id.  When
                    provided, printer intelligence supplies the plate size.

            Returns:
                Dict with ``success``, ``parts`` list (each part has ``x``, ``y`` set),
                ``group_count``, and ``message``.
            """
            _srv._check_auth("design:compose")

            try:
                from kiln.multicolor_3mf import auto_arrange_parts as _arrange
            except ImportError as exc:
                return {"success": False, "error": f"multicolor_3mf module unavailable: {exc}"}

            try:
                resolved_model_id = None
                resolved_volume = _resolve_tool_build_volume(printer_id)
                if resolved_volume:
                    resolved_model_id, build_volume = resolved_volume
                    plate_width, plate_depth = build_volume[0], build_volume[1]
                arranged = _arrange(
                    part_specs,
                    plate_width=plate_width,
                    plate_depth=plate_depth,
                    gap_mm=gap_mm,
                )
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_ARGS")

            groups_seen: set = set()
            for spec in part_specs:
                g = spec.get("group")
                if g is not None:
                    groups_seen.add(int(g))

            result_parts = [
                {
                    "stl_path": p.stl_path,
                    "extruder": p.extruder,
                    "name": p.name,
                    "color": p.color,
                    "material": p.material,
                    "x": p.x,
                    "y": p.y,
                    "z": p.z,
                }
                for p in arranged
            ]

            group_count = len(groups_seen) if groups_seen else len(arranged)
            response = {
                "success": True,
                "parts": result_parts,
                "group_count": group_count,
                "message": (
                    f"Arranged {len(arranged)} parts ({group_count} groups) "
                    f"on a {plate_width}\u00d7{plate_depth}mm plate with {gap_mm}mm gap. "
                    f"Pass 'parts' directly to compose_multicolor_3mf()."
                ),
            }
            if resolved_model_id:
                response["bed_size_source"] = "printer_intelligence"
                response["bed_size_model_id"] = resolved_model_id
                response["bed_dims_mm"] = [plate_width, plate_depth]
            return response

        # ------------------------------------------------------------------
        # optimize_template_params
        # ------------------------------------------------------------------

        @mcp.tool()
        def optimize_template_params(
            template_id: str,
            samples_per_param: int = 3,
            max_variants: int = 27,
            constraints: str = "",
            output_dir: str = "",
        ) -> dict:
            """Find the structurally strongest version of a parametric template.

            Sweeps each template parameter across its [min, max] range at
            evenly spaced sample points, generates every combination via
            OpenSCAD, runs structural analysis on each variant, and returns
            the configuration with the highest structural score.

            Use this when you want to **automatically** find optimal dimensions
            for a functional part -- e.g. "what wall thickness and bracket
            height give the strongest shelf bracket?"

            :param template_id: Template ID from the design template library.
            :param samples_per_param: Sample points per parameter (default 3).
            :param max_variants: Maximum total variants to test (default 27).
            :param constraints: JSON string of constraints, e.g.
                   ``{"max_width_mm": 100, "max_height_mm": 50}``.
            :param output_dir: Directory for generated STLs (temp dir if empty).
            :returns: Dict with best_params, best_score, best_grade, best_stl_path,
                      variants_tested, all_scores, and summary.
            """
            _srv._check_auth("design:optimize")
            try:
                import json as _json

                from kiln.design_reasoning import optimize_template_params as _optimize

                parsed_constraints = None
                if constraints:
                    parsed_constraints = _json.loads(constraints)

                result = _optimize(
                    template_id,
                    samples_per_param=samples_per_param,
                    max_variants=max_variants,
                    constraints=parsed_constraints,
                    output_dir=output_dir or None,
                )
                response = {"success": True, **result.to_dict()}
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", source_path=response.get("best_stl_path"),
                    )
                except ImportError:
                    return response
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _srv._error_dict(f"Template optimization failed: {exc}")

        # ------------------------------------------------------------------
        # solve_template_constraints
        # ------------------------------------------------------------------

        @mcp.tool()
        def solve_template_constraints(
            template_id: str,
            constraints: str,
        ) -> dict:
            """Solve parametric constraints to find valid template parameters.

            Given a template and constraints (min/max/equals/ratio), iteratively
            adjusts parameters to satisfy all constraints while staying within
            the template's declared parameter ranges.

            :param template_id: Template identifier (e.g., "shelf_bracket").
            :param constraints: JSON object mapping param names to constraint dicts.
                Example: ``{"width": {"min": 20, "max": 50}, "height": {"equals": 30}}``
                Supported keys: min, max, equals, ratio (e.g. ``{"ratio": ["width", 0.5]}``).
            :returns: Dict with solved_params, satisfied/violated constraints.
            """
            _srv._check_auth("design:optimize")
            import json as _json

            try:
                constraint_dict = (
                    _json.loads(constraints) if isinstance(constraints, str) else constraints
                )
            except _json.JSONDecodeError:
                return _srv._error_dict("constraints must be valid JSON.", code="INVALID_ARGS")

            try:
                from kiln.design_reasoning import solve_constraints

                result = solve_constraints(template_id, constraint_dict)
                return {"success": True, **result.to_dict()}
            except Exception as exc:
                return _srv._error_dict(f"Constraint solving failed: {exc}")

        # ------------------------------------------------------------------
        # iterate_design
        # ------------------------------------------------------------------

        @mcp.tool()
        def iterate_design(
            prompt: str,
            provider: str = "openscad",
            max_iterations: int = 3,
            material: str = "",
            printer_model: str = "",
            brief_id: str = "",
        ) -> dict:
            """Automated design iteration: generate -> validate -> improve -> regenerate.

            Runs a closed loop that generates a model, validates it for
            printability issues, and if issues are found, improves the prompt
            and regenerates.  Stops when the model passes validation or
            max_iterations is reached.  Returns the best result.

            :param prompt: Text description or OpenSCAD code.
            :param provider: Generation provider (default ``"openscad"``).
            :param max_iterations: Maximum improvement attempts (1-5).
            :param material: Optional material for design intelligence.
            :param printer_model: Optional printer model for constraints.
            :param brief_id: Optional saved-goal id from ``design_session``.
                When supplied AND the best iteration produced a mesh, a
                ``design_brief:<id>`` intent sidecar is written next to
                the produced file so the audit's "matches what you
                asked for" gate, the brief failure_history wiring, and
                the ``compare_design_versions`` intent diff all light
                up against the saved goal — without the user having to
                re-attach the brief after every iteration round.
                Best-effort: kiln-pro not installed silently skips.
            :returns: Dict with the best result and iteration history.
            """
            if err := _srv._check_auth("generate"):
                return err
            from kiln.generation.validation import analyze_mesh
            from kiln.generation_feedback import (
                analyze_for_feedback,
                enhance_prompt_with_design_intelligence,
                generate_improved_prompt,
                get_provider_prompt_limit,
            )

            max_iterations = max(1, min(5, max_iterations))
            iterations: list[dict[str, Any]] = []
            best_result: dict[str, Any] | None = None
            best_score = -1

            try:
                gen = _srv._get_generation_provider(provider)
            except Exception as exc:
                return _srv._error_dict(f"Provider error: {exc}", code="PROVIDER_ERROR")

            current_prompt = prompt

            # Pre-enrich with design intelligence (skip for OpenSCAD raw code)
            if provider != "openscad":
                try:
                    limit = get_provider_prompt_limit(provider)
                    enriched = enhance_prompt_with_design_intelligence(
                        current_prompt,
                        material=material or None,
                        printer_model=printer_model or None,
                        max_length=limit,
                    )
                    current_prompt = enriched.improved_prompt
                except Exception:
                    pass

            for i in range(max_iterations):
                iteration: dict[str, Any] = {"iteration": i + 1, "prompt": current_prompt[:200]}

                # Generate
                try:
                    job = gen.generate(current_prompt, format="stl")
                except Exception as exc:
                    iteration["status"] = "generation_failed"
                    iteration["error"] = str(exc)
                    iterations.append(iteration)
                    continue

                if job.status.value == "failed":
                    iteration["status"] = "generation_failed"
                    iteration["error"] = job.error
                    iterations.append(iteration)
                    continue

                # For async providers, we'd need to poll -- skip for OpenSCAD
                if job.status.value != "succeeded":
                    iteration["status"] = "pending"
                    iteration["job_id"] = job.id
                    iterations.append(iteration)
                    # Can't iterate further on async providers in a sync loop
                    best_result = {"job": job.to_dict()}
                    break

                # Download and analyze
                try:
                    dl = gen.download_result(job.id)
                    analysis = analyze_mesh(dl.local_path)
                    iteration["analysis"] = {
                        "printability_score": analysis.printability_score,
                        "issues": analysis.printability_issues,
                        "triangles": analysis.triangle_count,
                        "volume_mm3": analysis.volume_mm3,
                        "dimensions_mm": analysis.dimensions_mm,
                    }
                    iteration["status"] = "succeeded"
                    iteration["file_path"] = dl.local_path

                    if analysis.printability_score > best_score:
                        best_score = analysis.printability_score
                        best_result = {
                            "job": job.to_dict(),
                            "result": dl.to_dict(),
                            "analysis": analysis.to_dict(),
                        }

                    # If score is good enough, stop iterating
                    if analysis.printability_score >= 80:
                        iteration["outcome"] = "passed"
                        iterations.append(iteration)
                        break

                    # Generate improved prompt
                    if i < max_iterations - 1:
                        feedback = analyze_for_feedback(
                            dl.local_path,
                            original_prompt=current_prompt,
                            printability_report=analysis.to_dict(),
                        )
                        if feedback:
                            improved = generate_improved_prompt(
                                prompt,  # Use original, not enriched
                                feedback,
                                iteration=i + 2,
                                provider=provider,
                            )
                            current_prompt = improved.improved_prompt
                            iteration["outcome"] = "needs_improvement"
                            iteration["feedback_applied"] = len(feedback)
                        else:
                            iteration["outcome"] = "no_feedback"

                except Exception as exc:
                    iteration["status"] = "analysis_failed"
                    iteration["error"] = str(exc)

                iterations.append(iteration)

            if best_result is None:
                return _srv._error_dict("All iterations failed.", code="ITERATION_EXHAUSTED")

            # B11 brief passthrough: if the caller supplied a saved-goal id
            # AND the winning iteration produced a mesh, write the
            # ``design_brief:<id>`` intent sidecar next to the best file so
            # downstream audit honor + failure_history + compare-versions
            # all light up against the saved goal.  Best-effort — kiln-pro
            # not installed silently skips, same pattern as the audit's
            # honor-gate hook in original_design.py.
            if brief_id:
                _attach_brief_to_iteration_result(brief_id, best_result)

            response = {
                "success": True,
                "iterations": iterations,
                "iteration_count": len(iterations),
                "best_score": best_score,
                "brief_id": brief_id or None,
                **best_result,
            }
            try:
                from kiln_pro.plugins.git_render_tools import (
                    attach_inspect_bundle,
                )

                return attach_inspect_bundle(
                    response,
                    level="full",
                    source_path=best_result.get("result", {}).get("local_path"),
                )
            except ImportError:
                return response

        # ------------------------------------------------------------------
        # optimize_print_orientation
        # ------------------------------------------------------------------

        @mcp.tool()
        def optimize_print_orientation(file_path: str, output_path: str = "") -> dict:
            """Auto-rotate a mesh to minimize overhangs and maximize bed contact.

            Tests multiple candidate orientations and picks the one with the
            best printability score.  Re-orients the mesh and places it flat
            on the build plate (z_min = 0).

            :param file_path: Path to the STL file.
            :param output_path: Output path.  Defaults to overwriting the input.
            :returns: Dict with rotation angles, overhang stats, and new dimensions.
            """
            if err := _srv._check_auth("generate"):
                return err
            try:
                from kiln.generation.validation import optimize_orientation

                result = optimize_orientation(file_path, output_path=output_path or None)
                response = {"success": True, **result}
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", source_path=response.get("path"),
                    )
                except ImportError:
                    return response
            except Exception as exc:
                return _srv._error_dict(
                    f"Orientation optimization failed: {exc}", code="ORIENT_ERROR"
                )

        # ------------------------------------------------------------------
        # check_print_readiness
        # ------------------------------------------------------------------

        @mcp.tool()
        def check_print_readiness(
            file_path: str,
            auto_fix: bool = False,
            output_path: str = "",
            bed_x_mm: float = 256.0,
            bed_y_mm: float = 256.0,
            bed_z_mm: float = 256.0,
            printer_id: str = "",
        ) -> dict:
            """Single-call print readiness check with optional auto-repair.

            Runs the full validation battery: parseable, manifold, no floating
            regions, overhangs within limits, fits build plate, no degenerate
            triangles.

            With ``auto_fix=True``, automatically repairs degenerate triangles,
            closes holes, and removes floating regions.

            :param file_path: Path to mesh file.
            :param auto_fix: Attempt automatic repairs (default False).
            :param output_path: Where to write the fixed file.
            :param bed_x_mm: Build plate X dimension (default 256).
            :param bed_y_mm: Build plate Y dimension (default 256).
            :param bed_z_mm: Build plate Z dimension (default 256).
            :param printer_id: Optional supported printer model id.  When
                provided, printer intelligence supplies the build volume.
            :returns: Dict with can_print verdict, issues, and actions taken.
            """
            if auto_fix and (err := _srv._check_auth("generate")):
                return err
            try:
                from kiln.generation.validation import can_print_now

                resolved_model_id = None
                resolved_volume = _resolve_tool_build_volume(printer_id)
                if resolved_volume:
                    resolved_model_id, build_volume = resolved_volume
                    bed_x_mm, bed_y_mm, bed_z_mm = build_volume

                response = {
                    "success": True,
                    **can_print_now(
                        file_path,
                        auto_fix=auto_fix,
                        output_path=output_path or None,
                        printer_bed_mm=(bed_x_mm, bed_y_mm, bed_z_mm),
                    ),
                }
                if resolved_model_id:
                    response["bed_size_source"] = "printer_intelligence"
                    response["bed_size_model_id"] = resolved_model_id
                    response["bed_dims_mm"] = [bed_x_mm, bed_y_mm, bed_z_mm]
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", stl_keys=("fixed_file",),
                    )
                except ImportError:
                    return response
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_ARGS")
            except Exception as exc:
                return _srv._error_dict(f"Print readiness check failed: {exc}")

        # ------------------------------------------------------------------
        # estimate_support_material
        # ------------------------------------------------------------------

        @mcp.tool()
        def estimate_support_material(file_path: str) -> dict:
            """Estimate support material needed for a mesh.

            Analyzes overhang triangles and projects them to the build plate
            to estimate the volume and weight of support material required.

            :param file_path: Path to .stl, .obj, or .glb file.
            :returns: Dict with support volume (mm\u00b3), weight (g), and overhang stats.
            """
            # STEP in, mesh out — the one shared door, never a per-tool
            # branch, so the CAD format engineering customers actually send
            # works here instead of failing several layers down.
            from kiln.step_import import resolve_mesh_input

            file_path, _conversion, _refusal = resolve_mesh_input(file_path)
            if _refusal:
                return _refusal

            try:
                from kiln.generation.validation import estimate_support_volume

                result = estimate_support_volume(file_path)
                return {"success": True, **result}
            except Exception as exc:
                return _srv._error_dict(
                    f"Support estimation failed: {exc}", code="SUPPORT_ERROR"
                )

        _logger.debug("Registered design reasoning tools")


plugin = _DesignReasoningToolsPlugin()
