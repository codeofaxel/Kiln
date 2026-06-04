"""Design intelligence tools plugin — knowledge, constraints, recommendations.

Gives AI agents access to structured design knowledge so they can reason
about what makes a design *good* before generating geometry.  Agents query
material properties, design patterns, and functional constraints to produce
designs that are structurally sound, manufacturable, and fit for purpose.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _DesignToolsPlugin:
    """Design intelligence tools — knowledge, constraints, recommendations.

    Tools (FDM desktop):
        - analyze_design_requirements
        - build_generation_prompt
        - audit_original_design
        - get_material_design_profile
        - list_design_materials
        - recommend_design_material
        - estimate_structural_load
        - check_material_environment
        - get_printer_design_capabilities
        - list_printer_design_profiles
        - get_design_template_info
        - list_design_templates_catalog
        - find_design_templates
        - match_design_requirements
        - validate_design_for_requirements
        - troubleshoot_print_issue
        - check_printer_material_compatibility
        - get_post_processing_guide
        - check_multi_material_pairing
        - get_print_diagnostic
        - estimate_print_cost_from_mesh
        - build_parametric_prompt
        - parse_scad_parameters
        - update_scad_parameter
        - validate_scad_parameters
        - list_design_components
        - match_design_components
        - compile_scad
        - tweak_and_compile_scad
        - analyze_scad_code
        - modify_scad_module
        - insert_into_scad
        - cache_design_with_source
        - get_design_source
    """

    @property
    def name(self) -> str:
        return "design_tools"

    @property
    def description(self) -> str:
        return "Design intelligence tools for constraint-aware design reasoning"

    def register(self, mcp: Any) -> None:
        """Register design intelligence tools with the MCP server."""

        @mcp.tool()
        def analyze_design_requirements(
            requirements: str,
            material: str | None = None,
        ) -> dict:
            """Analyze a functional requirement and return technical recommendations.

            This is the internal-lookup tool that resolves a natural-language
            requirement into material recommendations, applicable design
            patterns, dimensional constraints, print orientation rules, and
            expert guidance notes.

            For the user-facing flow — capturing what a user is making at the
            duty / environment / materials / safety layer and producing a
            saved goal that drives generation, the audit, and the post-print
            review — call ``design_session(verb="start", idea="...")`` first.
            That tool internally calls this one for technical lookups; agents
            calling ``analyze_design_requirements`` directly should treat it
            as a pre-design analysis pass, not the user-facing entry point.

            Examples:
                "shelf bracket that holds 10 lbs of books"
                "outdoor planter that holds water"
                "phone mount for car dashboard, survives summer heat"
                "snap-fit enclosure for a Raspberry Pi"
                "flexible phone case that absorbs drops"
                "cookie cutter, food safe"
                "decorative vase, looks premium"

            Args:
                requirements: Natural language description of what the object
                    needs to do — functional needs, environment, loads, etc.
                material: Optional material override (e.g. "petg"). If not
                    provided, the system recommends the best material.
            """
            from kiln.design_intelligence import get_design_constraints

            try:
                analysis = get_design_constraints(
                    requirements,
                    material=material,
                )
                result = analysis.to_dict()
                result["success"] = True
                return result
            except Exception as exc:
                _logger.error(
                    "analyze_design_requirements failed: %s", exc, exc_info=True,
                )
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def build_generation_prompt(
            requirements: str,
            material: str | None = None,
            printer_model: str | None = None,
            provider: str | None = None,
        ) -> dict:
            """Build a design-aware generation prompt for original 3D creation.

            This is the best pre-generation tool for original designs. It takes
            a natural-language idea and appends manufacturing constraints,
            printer-fit limits, and material guidance so text-to-3D backends
            receive a prompt grounded in real printability constraints.

            When provider is specified, the prompt length is optimized for that
            backend. Use provider="openscad" for maximum constraint injection
            (100K chars), "meshy" for lean prompts (600 chars), or omit for
            the default limit.

            Args:
                requirements: Natural language description of the desired part.
                material: Optional material override (e.g. "petg").
                printer_model: Optional printer model ID (e.g. "bambu_a1").
                provider: Optional generation provider (e.g. "openscad", "meshy",
                    "gemini"). Controls prompt length budget.
            """
            from kiln.generation_feedback import enhance_prompt_with_design_intelligence

            try:
                prompt = enhance_prompt_with_design_intelligence(
                    requirements,
                    material=material,
                    printer_model=printer_model,
                    provider=provider,
                )
                return {
                    "success": True,
                    "prompt": prompt.to_dict(),
                    "message": (
                        f"Built a design-aware prompt with "
                        f"{len(prompt.constraints_added)} constraints"
                        f"{f' for {provider} provider' if provider else ''}."
                    ),
                }
            except Exception as exc:
                _logger.error("Build generation prompt failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def audit_original_design(
            file_path: str,
            requirements: str,
            material: str | None = None,
            printer_model: str | None = None,
            build_volume_x: float | None = None,
            build_volume_y: float | None = None,
            build_volume_z: float | None = None,
            nozzle_diameter: float = 0.4,
            layer_height: float = 0.2,
            max_overhang_angle: float = 45.0,
        ) -> dict:
            """Run a ruthless audit of an original design before printing.

            Combines design briefing, prompt enhancement, mesh validation,
            printability scoring, orientation analysis, advanced diagnostics,
            and regeneration feedback into a single report.

            Use this after generating or modeling a new part to answer:
            "Is this genuinely ready to print, and if not, what exact changes
            should the agent make next?"

            Args:
                file_path: Path to STL or OBJ file.
                requirements: Functional requirements the design must satisfy.
                material: Optional material constraint (e.g. "petg").
                printer_model: Optional printer model ID (e.g. "bambu_a1").
                build_volume_x: Optional build volume X override in mm.
                build_volume_y: Optional build volume Y override in mm.
                build_volume_z: Optional build volume Z override in mm.
                nozzle_diameter: Printer nozzle diameter in mm.
                layer_height: Layer height in mm.
                max_overhang_angle: Supportless overhang threshold in degrees.
            """
            from kiln.original_design import audit_original_design as _audit

            try:
                build_volume = None
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

                audit = _audit(
                    file_path,
                    requirements,
                    material=material,
                    printer_model=printer_model,
                    build_volume=build_volume,
                    nozzle_diameter=nozzle_diameter,
                    layer_height=layer_height,
                    max_overhang_angle=max_overhang_angle,
                )
                result = audit.to_dict()
                result["success"] = True
                result["message"] = (
                    f"Original design readiness: {audit.readiness_score}/100 "
                    f"({audit.readiness_grade}). "
                    f"{'Ready for print.' if audit.ready_for_print else 'Not ready for print.'}"
                )
                return result
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            except Exception as exc:
                _logger.error("Original design audit failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def get_material_design_profile(material: str) -> dict:
            """Get full engineering properties for a 3D printing material.

            Returns mechanical properties (tensile/flexural strength, impact
            resistance, creep behavior), thermal limits (glass transition,
            max service temp), chemical properties (UV/moisture resistance,
            food safety), dimensional design limits (min wall thickness,
            max overhang, snap-fit tolerances), use-case ratings, and
            expert guidance notes.

            Use this to understand a specific material deeply — what it's
            good at, what it's bad at, and what design limits to respect.

            Args:
                material: Material ID — one of: pla, petg, abs, tpu, asa,
                    nylon, polycarbonate.
            """
            from kiln.design_intelligence import get_material_profile

            try:
                profile = get_material_profile(material)
                if profile is None:
                    return {
                        "success": False,
                        "error": f"Unknown material: {material}. "
                        "Available: pla, petg, abs, tpu, asa, nylon, polycarbonate.",
                    }
                result = profile.to_dict()
                result["success"] = True
                return result
            except Exception as exc:
                _logger.error("Material profile failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def list_design_materials() -> dict:
            """List all available materials with summary properties.

            Returns a compact overview of every material in the knowledge
            base — name, category, key strengths, key weaknesses, and
            best-fit use cases.  Use this to compare materials at a glance
            before diving deeper with get_material_design_profile.
            """
            from kiln.design_intelligence import list_material_profiles

            try:
                profiles = list_material_profiles()
                summaries = []
                for p in profiles:
                    summaries.append(
                        {
                            "material_id": p.material_id,
                            "display_name": p.display_name,
                            "category": p.category,
                            "tensile_strength_mpa": p.mechanical.get(
                                "tensile_strength_mpa"
                            ),
                            "max_service_temp_c": p.thermal.get("max_service_temp_c"),
                            "impact_resistance": p.mechanical.get("impact_resistance"),
                            "layer_adhesion": p.mechanical.get("layer_adhesion"),
                            "uv_resistance": p.chemical.get("uv_resistance"),
                            "food_safe": p.chemical.get("food_safe"),
                            "ease_of_print": p.thermal.get("warping_tendency"),
                            "top_guidance": p.agent_guidance[0]
                            if p.agent_guidance
                            else "",
                        }
                    )
                return {
                    "success": True,
                    "materials": summaries,
                    "count": len(summaries),
                }
            except Exception as exc:
                _logger.error("List materials failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def recommend_design_material(
            requirements: str,
            printer_has_enclosure: bool = False,
            printer_has_direct_drive: bool = True,
            max_hotend_temp_c: int = 300,
        ) -> dict:
            """Recommend material for engineering/functional parts (strength, heat, environment).

            Analyzes functional requirements and recommends the optimal
            material considering mechanical needs, environmental exposure,
            printer capabilities, and ease of printing.  Returns the top
            recommendation with reasoning, warnings, and alternatives.

            **Which material tool to use:**

            - Designing a part and need engineering specs? → ``recommend_design_material`` (this tool)
            - Quick intent-based pick for your own printer? → ``recommend_material``
            - Ordering a print from a service? → ``suggest_material_for_order``

            Args:
                requirements: What the object needs to do (e.g. "hold 5 kg
                    of books on an outdoor shelf").
                printer_has_enclosure: Whether the printer has an enclosed
                    build chamber (needed for ABS, ASA, Nylon, PC).
                printer_has_direct_drive: Whether the printer has a direct
                    drive extruder (needed for TPU).
                max_hotend_temp_c: Maximum hotend temperature in Celsius.
            """
            from kiln.design_intelligence import recommend_material_for_design

            try:
                rec = recommend_material_for_design(
                    requirements,
                    printer_has_enclosure=printer_has_enclosure,
                    printer_has_direct_drive=printer_has_direct_drive,
                    max_hotend_temp_c=max_hotend_temp_c,
                )
                result = rec.to_dict()
                result["success"] = True
                # Food-safety overlay (kiln-pro feature; free-tier silently skips):
                # when the requirements imply food / pet / mouth contact,
                # warn or override if the top pick isn't food_safe and limit
                # the alternatives list to food-safe options.
                try:
                    from kiln_pro.material_safety import (  # noqa: WPS433
                        assess_food_safety,
                        filter_materials_by_food_safety,
                        use_case_implies_food_contact,
                    )
                except ImportError:
                    use_case_implies_food_contact = None  # type: ignore[assignment]
                if (
                    use_case_implies_food_contact is not None
                    and use_case_implies_food_contact(requirements)
                ):
                    result["food_contact_use_case_detected"] = True

                    def _slug(obj):
                        """Extract a material slug from the recommendation
                        result's varied shapes (dict with material_id /
                        material / name keys, or a bare string)."""
                        if isinstance(obj, dict):
                            return (
                                obj.get("material_id")
                                or obj.get("material")
                                or obj.get("name")
                                or obj.get("display_name")
                            )
                        if isinstance(obj, str):
                            return obj
                        return None

                    primary_obj = (
                        result.get("material") or result.get("recommended_material")
                    )
                    primary_slug = _slug(primary_obj)
                    if primary_slug:
                        verdict = assess_food_safety(primary_slug)
                        result["food_safety"] = {
                            "primary_material": primary_slug,
                            "primary_material_verdict": verdict["verdict"],
                            "primary_material_warning": verdict["user_warning"],
                            "approved_alternatives": verdict["approved_alternatives"],
                        }
                        if verdict["verdict"] == "refuse":
                            warnings_list = result.setdefault("warnings", [])
                            warnings_list.insert(
                                0,
                                "FOOD-SAFETY OVERRIDE: " + verdict["user_warning"],
                            )
                            alts = verdict["approved_alternatives"]
                            if alts:
                                result["recommended_override"] = {
                                    "original_recommendation": primary_slug,
                                    "override_to": alts[0],
                                    "reason": "food_contact_safety",
                                }
                    raw_alts = result.get("alternatives") or []
                    if raw_alts and isinstance(raw_alts, list):
                        slugs = [_slug(a) for a in raw_alts]
                        safe_set = set(
                            filter_materials_by_food_safety(
                                [s for s in slugs if s], require="yes_or_conditional"
                            )
                        )
                        result["alternatives_food_safe_only"] = [
                            a for a, s in zip(raw_alts, slugs, strict=False) if s and s in safe_set
                        ]
                return result
            except Exception as exc:
                _logger.error("Material recommendation failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def estimate_structural_load(
            material: str,
            cross_section_mm2: float,
            cantilever_length_mm: float,
            load_across_layers: bool = True,
        ) -> dict:
            """Estimate safe structural load for a cantilevered section.

            Args:
                material: Material ID (e.g. "petg", "nylon", "polycarbonate").
                cross_section_mm2: Effective load-bearing cross section in mm^2.
                cantilever_length_mm: Cantilever length in mm.
                load_across_layers: True when load is oriented across layers
                    (stronger); False when along layer bonds (weaker).
            """
            from kiln.design_intelligence import estimate_load_capacity

            try:
                estimate = estimate_load_capacity(
                    material,
                    cross_section_mm2,
                    cantilever_length_mm,
                    load_across_layers=load_across_layers,
                )
                if estimate is None:
                    return {
                        "success": False,
                        "error": f"Unknown load table material: {material}. "
                        "Available: pla, petg, abs, nylon, polycarbonate.",
                    }
                result = estimate.to_dict()
                result["success"] = True
                # estimate_structural_load is definitionally a load-bearing
                # question — always surface the heuristic-grade upgrade nudge.
                from kiln.load_bearing_detector import attach_load_bearing_nudge

                return attach_load_bearing_nudge(result, force=True)
            except Exception as exc:
                _logger.error("Load estimation failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def check_material_environment(material: str, environment: str) -> dict:
            """Check whether a material is compatible with an environment.

            Args:
                material: Material ID from the design knowledge base.
                environment: Natural language environment description.
            """
            from kiln.design_intelligence import check_environment_compatibility

            try:
                report = check_environment_compatibility(material, environment)
                if report is None:
                    return {
                        "success": False,
                        "error": f"Unknown material: {material}. "
                        "Available: pla, petg, abs, tpu, asa, nylon, polycarbonate.",
                    }
                result = report.to_dict()
                result["success"] = True
                return result
            except Exception as exc:
                _logger.error(
                    "Environment compatibility check failed: %s",
                    exc,
                    exc_info=True,
                )
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def get_printer_design_capabilities(printer_id: str) -> dict:
            """Get the design capability profile for a printer.

            Args:
                printer_id: Printer profile ID (e.g. "bambu_x1c", "voron_2").
            """
            from kiln.design_intelligence import (
                get_printer_design_profile,
                list_printer_profiles,
            )

            try:
                profile = get_printer_design_profile(printer_id)
                if profile is None:
                    available = [p.printer_id for p in list_printer_profiles()]
                    return {
                        "success": False,
                        "error": f"Unknown printer: {printer_id}. "
                        f"Available: {', '.join(available)}.",
                    }
                result = profile.to_dict()
                result["success"] = True
                return result
            except Exception as exc:
                _logger.error(
                    "Printer capability lookup failed: %s",
                    exc,
                    exc_info=True,
                )
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def list_printer_design_profiles() -> dict:
            """List all known printer design capability profiles."""
            from kiln.design_intelligence import list_printer_profiles

            try:
                profiles = list_printer_profiles()
                return {
                    "success": True,
                    "profiles": [p.to_dict() for p in profiles],
                    "count": len(profiles),
                }
            except Exception as exc:
                _logger.error(
                    "List printer profiles failed: %s",
                    exc,
                    exc_info=True,
                )
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def get_design_template_info(template: str) -> dict:
            """Get detailed design rules for a functional design template.

            Returns dimensional constraints, material compatibility,
            print orientation rules, and expert tips for templates like
            snap fits, press fits, living hinges, gears, brackets, threads,
            watertight containers, and electronics enclosures.

            Args:
                template: Template ID — one of: snap_fit_cantilever, press_fit,
                    living_hinge, threaded_connection, cantilever_bracket,
                    watertight_container, enclosure_box, gear.
            """
            from kiln.design_intelligence import get_design_template

            try:
                t = get_design_template(template)
                if t is None:
                    from kiln.design_intelligence import list_design_templates

                    available = [dt.template_id for dt in list_design_templates()]
                    return {
                        "success": False,
                        "error": f"Unknown template: {template}. Available: {', '.join(available)}.",
                    }
                result = t.to_dict()
                result["success"] = True
                return result
            except Exception as exc:
                _logger.error("Design template failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def list_design_templates_catalog() -> dict:
            """List all available design templates with descriptions.

            Returns every functional design template in the knowledge base
            with a brief description and applicable use cases.  Use this
            to discover which templates are relevant before getting detailed
            rules with get_design_template_info.
            """
            from kiln.design_intelligence import list_design_templates

            try:
                templates = list_design_templates()
                summaries = []
                for t in templates:
                    summaries.append(
                        {
                            "template_id": t.template_id,
                            "display_name": t.display_name,
                            "description": t.description,
                            "use_cases": t.use_cases,
                            "best_materials": t.material_compatibility.get(
                                "excellent", []
                            ),
                        }
                    )
                return {
                    "success": True,
                    "templates": summaries,
                    "count": len(summaries),
                }
            except Exception as exc:
                _logger.error("List templates failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def find_design_templates(use_case: str) -> dict:
            """Find design templates that apply to a specific use case.

            Searches the template library for templates whose use-case tags
            match the query.  Returns matching templates with full rules.

            Args:
                use_case: What you're designing (e.g. "enclosure",
                    "gear train", "battery cover", "vase").
            """
            from kiln.design_intelligence import find_templates_for_use_case

            try:
                templates = find_templates_for_use_case(use_case)
                return {
                    "success": True,
                    "templates": [t.to_dict() for t in templates],
                    "count": len(templates),
                }
            except Exception as exc:
                _logger.error("Find templates failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def match_design_requirements(description: str) -> dict:
            """Identify which functional requirements apply to a design task.

            Scans natural language for requirement triggers (load bearing,
            watertight, outdoor, food safe, heat resistant, flexible, impact
            resistant, precision, aesthetic) and returns matched constraint
            sets with rules and guidance.

            Use this to understand WHAT constraints apply before getting
            the full design brief.

            Args:
                description: What the object needs to do (e.g. "outdoor
                    hook that holds a heavy hanging planter").
            """
            from kiln.design_intelligence import match_requirements

            try:
                matched = match_requirements(description)
                return {
                    "success": True,
                    "matched_requirements": [m.to_dict() for m in matched],
                    "count": len(matched),
                }
            except Exception as exc:
                _logger.error("Match requirements failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}


        @mcp.tool()
        def validate_design_for_requirements(
            file_path: str,
            requirements: str,
            material: str | None = None,
        ) -> dict:
            """Validate a 3D model against functional design requirements.

            Checks that a generated STL/OBJ model meets the structural,
            dimensional, and manufacturability constraints implied by the
            requirements.  Returns pass/fail per check with specific fix
            suggestions for any failures.

            Call this AFTER generating a model and BEFORE printing it.
            If validation fails, use the fix suggestions to improve the
            generation prompt and regenerate.

            Args:
                file_path: Path to STL or OBJ file.
                requirements: Same requirements text used for analyze_design_requirements.
                material: Optional material (e.g. "petg").
            """
            from kiln.design_validator import validate_design

            try:
                report = validate_design(
                    file_path,
                    requirements,
                    material=material,
                )
                result = report.to_dict()
                result["success"] = True
                return result
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            except Exception as exc:
                _logger.error("Design validation failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}


        # ── Troubleshooting, compatibility, and diagnostics ──────────────

        @mcp.tool()
        def troubleshoot_print_issue(
            material: str,
            symptom: str | None = None,
        ) -> dict:
            """Diagnose a 3D printing problem by material and symptom.

            Searches the troubleshooting knowledge base for matching issues
            and returns root causes, prioritised fixes, prevention tips, and
            storage/drying requirements.  When no symptom is given, returns
            all known issues for the material sorted by severity.

            Use this when a user reports a print failure, quality issue, or
            asks "why is my print doing X?"

            Examples:
                material="pla", symptom="stringing"
                material="petg", symptom="poor layer adhesion"
                material="abs", symptom="warping"
                material="nylon" (no symptom — returns all known issues)

            Args:
                material: Material ID (e.g. "pla", "petg", "abs", "tpu",
                    "nylon", "polycarbonate", "asa", "cf_nylon").
                symptom: Optional symptom keywords to search for (e.g.
                    "stringing", "warping", "clog", "brittle").
            """
            from kiln.design_intelligence import troubleshoot_print_issue as _troubleshoot

            try:
                result = _troubleshoot(material, symptom)
                if result is None:
                    from kiln.design_intelligence import list_troubleshooting_materials

                    available = ", ".join(list_troubleshooting_materials())
                    return {
                        "success": False,
                        "error": f"No troubleshooting data for '{material}'. "
                        f"Available: {available}",
                    }
                out = result.to_dict()
                out["success"] = True
                out["match_count"] = len(result.matched_issues)
                return out
            except Exception as exc:
                _logger.error("Troubleshoot failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def check_printer_material_compatibility(
            printer: str,
            material: str | None = None,
        ) -> dict:
            """Check if a specific printer can handle a material.

            Returns compatibility status (compatible / needs_upgrade /
            not_compatible), any required hardware upgrades (enclosure,
            hardened nozzle, dry box), and practical notes.

            When no material is specified, returns the full compatibility
            matrix for the printer across all known materials.

            Use this when a user asks "can my Ender 3 print nylon?" or
            "what materials work on my Bambu A1 Mini?"

            Args:
                printer: Printer model ID (e.g. "ender3", "bambu_x1c",
                    "prusa_mk4", "voron_2"). Use underscores, lowercase.
                material: Optional material to check (e.g. "nylon", "abs").
                    If omitted, returns all materials for this printer.
            """
            from kiln.design_intelligence import (
                check_printer_material_compatibility as _check_compat,
            )
            from kiln.design_intelligence import (
                list_compatibility_printers,
            )

            try:
                report = _check_compat(printer, material)
                if report is None:
                    available = ", ".join(list_compatibility_printers())
                    return {
                        "success": False,
                        "error": f"No compatibility data for printer '{printer}'. "
                        f"Available: {available}",
                    }
                out = report.to_dict()
                out["success"] = True

                # Add summary counts for the full-matrix case
                if material is None:
                    statuses = [
                        v.get("status", "unknown")
                        for v in report.materials.values()
                    ]
                    out["summary"] = {
                        "compatible": statuses.count("compatible"),
                        "needs_upgrade": statuses.count("needs_upgrade"),
                        "not_compatible": statuses.count("not_compatible"),
                    }
                return out
            except Exception as exc:
                _logger.error("Compatibility check failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def get_post_processing_guide(material: str) -> dict:
            """Get post-processing techniques for finishing a 3D printed part.

            Returns surface finishing techniques (sanding, painting, epoxy),
            paintability info (primer type, compatible paint types), and
            strengthening methods (annealing, epoxy infusion) with step-by-step
            procedures, required tools, difficulty ratings, and safety notes.

            Use this when a user asks "how do I make this print look better?"
            or "can I paint PLA?" or "how to strengthen my PETG part?"

            Args:
                material: Material ID (e.g. "pla", "abs", "petg", "nylon").
            """
            from kiln.design_intelligence import get_post_processing as _get_pp

            try:
                guide = _get_pp(material)
                if guide is None:
                    return {
                        "success": False,
                        "error": f"No post-processing data for '{material}'.",
                    }
                out = guide.to_dict()
                out["success"] = True
                return out
            except Exception as exc:
                _logger.error("Post-processing guide failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def check_multi_material_pairing(
            material_a: str,
            material_b: str,
        ) -> dict:
            """Check if two materials can be co-printed in dual extrusion.

            Returns compatibility (yes/no), interface adhesion quality,
            notes on temperature management, and soluble support dissolution
            instructions when applicable.

            Use this when a user asks "can I print PLA with TPU?" or
            "what support material works with ABS?" or planning any
            multi-material / dual-extrusion print.

            Args:
                material_a: First material (e.g. "pla", "abs").
                material_b: Second material (e.g. "tpu", "hips", "pva").
            """
            from kiln.design_intelligence import check_multi_material_compatibility

            try:
                report = check_multi_material_compatibility(material_a, material_b)
                out = report.to_dict()
                out["success"] = True
                return out
            except Exception as exc:
                _logger.error("Multi-material check failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def get_print_diagnostic(
            material: str,
            symptom: str | None = None,
            printer: str | None = None,
        ) -> dict:
            """Get a comprehensive print diagnostic combining multiple knowledge sources.

            This is the PRIMARY tool for debugging print problems.  Combines
            troubleshooting data (symptom matching, root causes, fixes),
            printer compatibility (upgrade requirements, known issues),
            storage requirements (drying temps, humidity limits), and
            post-processing tips (strengthening options) into a single
            actionable response.

            Call this FIRST when a user reports any print quality problem.
            It cross-references all knowledge sources so the agent doesn't
            need to make multiple tool calls.

            Examples:
                material="petg", symptom="stringing", printer="ender3"
                material="abs", symptom="warping", printer="bambu_a1"
                material="nylon", symptom="brittle"

            Args:
                material: Material being printed (e.g. "pla", "petg").
                symptom: What's going wrong (e.g. "stringing", "warping",
                    "poor adhesion", "clog", "brittle").
                printer: Optional printer model for compatibility context
                    (e.g. "ender3", "bambu_x1c").
            """
            from kiln.design_intelligence import (
                get_print_diagnostic as _get_diagnostic,
            )
            from kiln.design_intelligence import (
                list_troubleshooting_materials,
            )

            try:
                result = _get_diagnostic(
                    material,
                    symptom=symptom,
                    printer_id=printer,
                )
                if result is None:
                    available = ", ".join(list_troubleshooting_materials())
                    return {
                        "success": False,
                        "error": f"No data for material '{material}'. "
                        f"Available: {available}",
                    }
                out = result.to_dict()
                out["success"] = True
                out["issue_count"] = len(result.matched_issues)
                return out
            except Exception as exc:
                _logger.error("Print diagnostic failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        # ── Cost estimation ─────────────────────────────────────────────────

        @mcp.tool()
        def estimate_print_cost_from_mesh(
            file_path: str,
            material: str = "pla",
            infill_percent: float = 20.0,
            wall_layers: int = 3,
            layer_height_mm: float = 0.2,
            nozzle_mm: float = 0.4,
            include_supports: bool = False,
            support_density: float = 15.0,
            adhesion_type: str = "none",
            electricity_rate: float = 0.12,
            printer_wattage: float = 200.0,
        ) -> dict:
            """Estimate total print cost from a 3D model file.

            Calculates material, support, adhesion, and electricity costs directly
            from mesh geometry — no G-code or slicing required. Includes a detailed
            cost breakdown and actionable recommendations to reduce cost.

            Supported materials: pla, pla+, petg, abs, tpu, asa, nylon, pc,
            cf-pla, silk-pla, hips, pva, pp, peek.

            Args:
                file_path: Path to mesh file (.stl, .obj, or .3mf).
                material: Material type (default "pla").
                infill_percent: Interior fill percentage 0-100 (default 20).
                wall_layers: Number of perimeter shells (default 3).
                layer_height_mm: Layer height in mm (default 0.2).
                nozzle_mm: Nozzle diameter in mm (default 0.4).
                include_supports: Estimate support material cost (default False).
                support_density: Support infill percentage (default 15).
                adhesion_type: Bed adhesion type: "none", "brim", or "raft".
                electricity_rate: Electricity cost in $/kWh (default 0.12).
                printer_wattage: Printer power consumption in watts (default 200).
            """
            from kiln.cost_estimator import CostEstimator

            try:
                estimator = CostEstimator()
                estimate = estimator.estimate_from_mesh(
                    file_path,
                    material=material,
                    infill_percent=infill_percent,
                    wall_layers=wall_layers,
                    layer_height_mm=layer_height_mm,
                    nozzle_mm=nozzle_mm,
                    include_supports=include_supports,
                    support_density=support_density,
                    adhesion_type=adhesion_type,
                    electricity_rate=electricity_rate,
                    printer_wattage=printer_wattage,
                )
                result = estimate.to_dict()
                result["success"] = True
                return result
            except FileNotFoundError as exc:
                _logger.error("Cost estimation file not found: %s", exc)
                return {"success": False, "error": str(exc)}
            except ValueError as exc:
                _logger.error("Cost estimation invalid input: %s", exc)
                return {"success": False, "error": str(exc)}
            except Exception as exc:
                _logger.error("Cost estimation failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        # ── Parametric OpenSCAD tools ──────────────────────────────────────

        @mcp.tool()
        def build_parametric_prompt(
            requirements: str,
            material: str | None = None,
            printer_model: str | None = None,
        ) -> dict:
            """Build a prompt optimized for parametric OpenSCAD code generation.

            Returns an enhanced prompt with OpenSCAD-specific instructions that
            guide AI to produce well-structured parametric code with named
            variables, descriptive comments, and material-aware design limits.

            Use this instead of build_generation_prompt when you want the AI to
            generate editable OpenSCAD code rather than a mesh file.

            Args:
                requirements: Natural language description of the desired part.
                material: Optional material override (e.g. "petg").
                printer_model: Optional printer model ID (e.g. "bambu_a1").
            """
            from kiln.generation_feedback import build_parametric_generation_prompt

            try:
                prompt = build_parametric_generation_prompt(
                    requirements,
                    material=material,
                    printer_model=printer_model,
                )
                return {
                    "success": True,
                    "prompt": prompt.to_dict(),
                    "message": (
                        f"Built parametric OpenSCAD prompt with "
                        f"{len(prompt.constraints_added)} design constraints."
                    ),
                }
            except Exception as exc:
                _logger.error("Build parametric prompt failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def parse_scad_parameters(scad_code: str) -> dict:
            """Parse parameter variables from OpenSCAD code.

            Extracts named dimension variables from the top of an OpenSCAD
            file, including their values, units, descriptions, and valid
            ranges (if annotated in comments).

            Use this after generating OpenSCAD code to discover which
            parameters can be adjusted.

            Args:
                scad_code: OpenSCAD source code string.
            """
            from kiln.parametric import parse_openscad_parameters

            try:
                params = parse_openscad_parameters(scad_code)
                return {
                    "success": True,
                    "parameters": [p.to_dict() for p in params],
                    "count": len(params),
                    "message": f"Found {len(params)} adjustable parameters.",
                }
            except Exception as exc:
                _logger.error("Parse SCAD parameters failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def update_scad_parameter(
            scad_code: str,
            parameter_name: str,
            new_value: float,
        ) -> dict:
            """Update a parameter value in OpenSCAD code.

            Finds the named variable declaration and replaces its value,
            preserving comments and formatting. Use this to tweak dimensions
            without regenerating the entire model.

            Args:
                scad_code: OpenSCAD source code string.
                parameter_name: Name of the variable to update.
                new_value: New numeric value for the parameter.
            """
            from kiln.parametric import update_openscad_parameter

            try:
                updated = update_openscad_parameter(scad_code, parameter_name, new_value)
                return {
                    "success": True,
                    "updated_code": updated,
                    "message": f"Updated {parameter_name} to {new_value}.",
                }
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            except Exception as exc:
                _logger.error("Update SCAD parameter failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def validate_scad_parameters(
            scad_code: str,
            material: str | None = None,
        ) -> dict:
            """Validate OpenSCAD parameters against material design limits.

            Checks if parameter values (wall thickness, hole diameter, etc.)
            violate the design limits for the specified material. Catches
            issues before compilation and printing.

            Args:
                scad_code: OpenSCAD source code string.
                material: Optional material ID (e.g. "pla", "petg") to check
                    against material-specific limits.
            """
            from kiln.parametric import validate_openscad_parameters

            try:
                warnings = validate_openscad_parameters(scad_code, material=material)
                return {
                    "success": True,
                    "warnings": [w.to_dict() for w in warnings],
                    "count": len(warnings),
                    "valid": len(warnings) == 0,
                    "message": (
                        "All parameters within limits."
                        if not warnings
                        else f"Found {len(warnings)} parameter warnings."
                    ),
                }
            except Exception as exc:
                _logger.error("Validate SCAD parameters failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        # ── Library component tools ─────────────────────────────────────

        @mcp.tool()
        def list_design_components(category: str | None = None) -> dict:
            """List available pre-built OpenSCAD components from bundled libraries.

            Kiln bundles BOSL2 OpenSCAD libraries with pre-built
            components for gears, threads, screws, bearings, hinges, and more.
            These components produce proven geometry — much better than
            generating complex mechanical parts from scratch.

            Categories: mechanical, fasteners, electronics

            Args:
                category: Optional filter by category. If not provided, lists
                    all available components.
            """
            from kiln.components import list_components

            try:
                components = list_components(category=category)
                return {
                    "success": True,
                    "components": [c.to_dict() for c in components],
                    "count": len(components),
                    "message": (
                        f"Found {len(components)} available components"
                        f"{f' in category {category!r}' if category else ''}."
                    ),
                }
            except Exception as exc:
                _logger.error("List components failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def match_design_components(description: str) -> dict:
            """Find pre-built library components matching a design description.

            Given a natural language description of what you want to build,
            identifies which bundled OpenSCAD library components can be used.
            Returns import lines, example usage, parameters, and guidance
            for each matching component.

            Examples:
                "hand crank with a gear" → finds spur_gear
                "box with a hinge" → finds knuckle_hinge
                "mounting bracket with screw holes" → finds screw_hole

            Args:
                description: Natural language description of the design.
            """
            from kiln.components import match_components

            try:
                matches = match_components(description)
                return {
                    "success": True,
                    "matches": [m.to_dict() for m in matches],
                    "count": len(matches),
                    "message": (
                        f"Found {len(matches)} matching components."
                        if matches
                        else "No matching components found. Design will use pure OpenSCAD."
                    ),
                }
            except Exception as exc:
                _logger.error("Match components failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}


        @mcp.tool()
        def compile_scad(scad_code: str = "", scad_path: str = "", timeout: int = 300) -> dict:
            """Compile OpenSCAD code into an STL file.

            Takes OpenSCAD source code OR a path to a .scad file, compiles
            it using the local OpenSCAD binary, and returns the path to the
            generated STL. Supports Kiln's bundled BOSL2 library.

            For surface() heightmap operations (photo emboss, lithophane),
            increase timeout to 600+ seconds.

            Args:
                scad_code: Valid OpenSCAD source code (provide this OR scad_path).
                scad_path: Path to a .scad file (provide this OR scad_code).
                timeout: Maximum compilation time in seconds (default 300).
            """
            import os

            from kiln.parametric import compile_scad_code

            try:
                code = scad_code
                if not code and scad_path:
                    if not os.path.isfile(scad_path):
                        return {"success": False, "error": f"File not found: {scad_path}"}
                    with open(scad_path, encoding="utf-8") as f:
                        code = f.read()
                if not code:
                    return {"success": False, "error": "Provide scad_code or scad_path"}

                stl_path = compile_scad_code(code, timeout=timeout)
                return {
                    "success": True,
                    "stl_path": stl_path,
                    "message": f"Compiled to {stl_path}",
                }
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            except Exception as exc:
                _logger.error("Compile SCAD failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def tweak_and_compile_scad(
            scad_code: str,
            parameter_name: str,
            new_value: float,
            material: str | None = None,
        ) -> dict:
            """Update a parameter in OpenSCAD code and recompile to STL.

            The complete parametric tweaking workflow: changes a dimension
            variable, validates against material limits, and compiles a new
            STL — all in one step. Perfect for "make it 5mm wider" requests.

            Args:
                scad_code: OpenSCAD source code.
                parameter_name: Variable name to update (e.g. "wall_thickness").
                new_value: New numeric value.
                material: Optional material for limit validation (e.g. "pla").
            """
            from kiln.parametric import tweak_and_compile

            try:
                result = tweak_and_compile(
                    scad_code, parameter_name, new_value, material=material,
                )
                warnings = result.get("warnings", [])
                suffix = f" with {len(warnings)} warnings" if warnings else ""
                return {
                    "success": True,
                    **result,
                    "message": (
                        f"Updated {parameter_name}={new_value}, compiled to STL{suffix}."
                    ),
                }
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            except Exception as exc:
                _logger.error(
                    "Tweak and compile failed: %s", exc, exc_info=True
                )
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def analyze_scad_code(scad_code: str) -> dict:
            """Analyze the structure of OpenSCAD code.

            Parses parameters, modules, and library imports to understand
            the code's architecture. Use this before modifying code to know
            what modules exist and what each one does.

            Args:
                scad_code: OpenSCAD source code.
            """
            from kiln.parametric import analyze_scad_structure

            try:
                structure = analyze_scad_structure(scad_code)
                return {
                    "success": True,
                    "structure": structure.to_dict(),
                    "message": (
                        f"Found {len(structure.parameters)} parameters and "
                        f"{len(structure.modules)} modules."
                    ),
                }
            except Exception as exc:
                _logger.error(
                    "Analyze SCAD failed: %s", exc, exc_info=True
                )
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def modify_scad_module(
            scad_code: str,
            module_name: str,
            new_module_code: str,
        ) -> dict:
            """Replace a module in OpenSCAD code with new implementation.

            Finds the named module and replaces its body entirely. Use for
            major modifications like redesigning a component.

            Args:
                scad_code: OpenSCAD source code.
                module_name: Module to replace (e.g. "top_panel").
                new_module_code: Complete new module code including the
                    module declaration and braces.
            """
            from kiln.parametric import modify_scad_module as _modify

            try:
                updated = _modify(scad_code, module_name, new_module_code)
                return {
                    "success": True,
                    "updated_code": updated,
                    "message": f"Replaced module {module_name!r}.",
                }
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            except Exception as exc:
                _logger.error(
                    "Modify SCAD module failed: %s", exc, exc_info=True
                )
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def insert_into_scad(
            scad_code: str,
            module_name: str,
            code_to_insert: str,
            position: str = "end",
        ) -> dict:
            """Insert code into an OpenSCAD module without replacing it.

            Adds geometry or operations inside a module. Use for targeted
            additions like "add ventilation holes to the top panel" or
            "add screw holes to the base."

            Args:
                scad_code: OpenSCAD source code.
                module_name: Module to modify (e.g. "base_plate").
                code_to_insert: OpenSCAD code to insert.
                position: "end" (before closing brace) or "start" (after
                    opening brace). Default: "end".
            """
            from kiln.parametric import insert_into_scad_module

            try:
                updated = insert_into_scad_module(
                    scad_code, module_name, code_to_insert, position=position,
                )
                return {
                    "success": True,
                    "updated_code": updated,
                    "message": f"Inserted code into module {module_name!r} at {position}.",
                }
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            except Exception as exc:
                _logger.error(
                    "Insert into SCAD failed: %s", exc, exc_info=True
                )
                return {"success": False, "error": str(exc)}


        # ── Design DNA ───────────────────────────────────────────────

        @mcp.tool()
        def cache_design_with_source(
            file_path: str,
            scad_source: str,
            generation_prompt: str = "",
            provider: str = "openscad",
            tags: list[str] | None = None,
            filament_type: str | None = None,
        ) -> dict:
            """Cache a design file alongside its parametric source code.

            Stores the STL/3MF file in the design cache and attaches the
            OpenSCAD source code and generation prompt so the design can
            be re-generated or tweaked later.

            Args:
                file_path: Path to the design file (STL, 3MF, etc.).
                scad_source: OpenSCAD source code that produced this file.
                generation_prompt: The prompt used to generate the design.
                provider: Generation provider name (e.g. "openscad", "gemini").
                tags: Optional tags for search.
                filament_type: Material type (e.g. "PLA", "PETG").
            """
            from kiln.design_cache import get_design_cache

            try:
                cache = get_design_cache()
                entry = cache.add(
                    file_path,
                    tags=tags,
                    filament_type=filament_type,
                    scad_source=scad_source,
                    generation_prompt=generation_prompt,
                    provider=provider,
                )
                return {
                    "success": True,
                    "design_id": entry.id,
                    "file_hash": entry.file_hash,
                    "has_source": entry.scad_source is not None,
                    "provider": entry.provider,
                    "message": f"Cached design {entry.id} with parametric source.",
                }
            except (FileNotFoundError, ValueError) as exc:
                return {"success": False, "error": str(exc)}
            except Exception as exc:
                _logger.error("cache_design_with_source failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def get_design_source(design_id: str) -> dict:
            """Retrieve the parametric source code for a cached design.

            Returns the OpenSCAD source, generation prompt, and provider
            so the design can be re-generated, tweaked, or inspected.

            Args:
                design_id: ID of the cached design.
            """
            from kiln.design_cache import get_design_cache

            try:
                cache = get_design_cache()
                source_info = cache.get_source(design_id)
                if source_info is None:
                    return {"success": False, "error": f"Design {design_id!r} not found."}
                if source_info["scad_source"] is None:
                    return {
                        "success": True,
                        "design_id": design_id,
                        "has_source": False,
                        "message": "Design exists but has no parametric source attached.",
                    }
                return {
                    "success": True,
                    "design_id": design_id,
                    "has_source": True,
                    "scad_source": source_info["scad_source"],
                    "generation_prompt": source_info["generation_prompt"],
                    "provider": source_info["provider"],
                }
            except Exception as exc:
                _logger.error("get_design_source failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def analyze_warping_risk(
            file_path: str,
            material: str = "pla",
        ) -> dict:
            """Analyze warping risk for a 3D model based on geometry and material.

            Examines the mesh for warping risk factors: large flat surfaces that
            tend to curl at corners, tall/narrow geometry prone to thermal
            contraction pulling, and sharp base corners that lift. Cross-references
            with the material's known warping tendency (from thermal properties).

            Returns a risk assessment with:
            - risk_level: "low", "moderate", "high", or "critical"
            - score_deduction: impact on overall printability score
            - large_flat_surfaces: detected flat areas prone to warping
            - height_to_base_ratio: geometry aspect ratio risk factor
            - material_warping_tendency: material's inherent warp behavior
            - recommendations: actionable mitigation advice

            Args:
                file_path: Path to STL or OBJ file to analyze.
                material: Material ID (e.g. "pla", "abs", "petg"). Defaults to PLA.
                    Used to look up thermal warping tendency.

            Examples:
                analyze_warping_risk("/path/to/model.stl", material="abs")
                analyze_warping_risk("/path/to/plate.stl")  # defaults to PLA
            """
            from kiln.printability import analyze_printability

            try:
                report = analyze_printability(file_path, material=material)
                if report.warping is not None:
                    result = report.warping.to_dict()
                    result["success"] = True
                    result["overall_score"] = report.score
                    result["overall_grade"] = report.grade
                    return result
                return {
                    "success": False,
                    "error": "Warping analysis not available for this model.",
                }
            except Exception as exc:
                _logger.error("Warping analysis failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        # ---------------------------------------------------------------
        # Brand filament profiles
        # ---------------------------------------------------------------

        @mcp.tool()
        def get_brand_filament_profile(profile_id: str) -> dict:
            """Get printing parameters for a specific brand filament.

            Returns brand-specific nozzle temp, bed temp, speed, density,
            drying requirements, and hardware requirements (hardened nozzle,
            enclosure, AMS compatibility).

            Args:
                profile_id: Brand filament ID (e.g. "bambu_pla_basic",
                    "prusament_petg", "polymaker_polyterra_pla")
            """
            from kiln.design_intelligence import get_brand_filament_profile as _get

            profile = _get(profile_id)
            if profile is None:
                return {"error": f"Brand filament profile '{profile_id}' not found", "status": "error"}
            return {"status": "success", "data": profile.to_dict()}

        @mcp.tool()
        def list_brand_filament_profiles(
            brand: str = "",
            parent_material: str = "",
        ) -> dict:
            """List available brand-specific filament profiles.

            Returns all known brand filament profiles, optionally filtered
            by brand name or parent material type.

            Args:
                brand: Filter by brand (e.g. "Bambu", "Prusament", "Polymaker")
                parent_material: Filter by parent material (e.g. "pla", "petg", "abs")
            """
            from kiln.design_intelligence import list_brand_filament_profiles as _list

            profiles = _list(
                brand=brand or None,
                parent_material=parent_material or None,
            )
            return {
                "status": "success",
                "count": len(profiles),
                "data": [p.to_dict() for p in profiles],
            }

        # ---------------------------------------------------------------
        # resolve_filament — unified filament resolver
        # ---------------------------------------------------------------

        @mcp.tool()
        def resolve_filament_profile(
            material_or_brand: str,
            printer_id: str = "",
        ) -> dict:
            """Resolve a material name or brand ID to a unified filament profile.

            Accepts EITHER a generic material (``"PLA"``, ``"TPU"``) OR a
            specific brand profile ID (``"bambu_pla_basic"``,
            ``"prusament_tpu_95a"``).  Brand profiles return manufacturer-exact
            specs (density, temps, drying, nozzle/enclosure requirements).
            Generic materials return conservative defaults.

            When ``printer_id`` is provided, also checks compatibility and
            returns warnings (e.g. "needs hardened nozzle", "needs enclosure",
            "not AMS compatible").

            Use this BEFORE slicing or printing to get exact filament specs.
            Pass the result to ``estimate_before_design`` for brand-accurate
            cost/time estimates.

            :param material_or_brand: Material name (``"PLA"``) or brand
                profile ID (``"bambu_petg_cf"``).
            :param printer_id: Optional printer model for compatibility checks.
            """
            import kiln.server as _srv

            try:
                from kiln.design_intelligence import resolve_filament

                eff_printer = printer_id if printer_id.strip() else None
                resolved = resolve_filament(
                    material_or_brand,
                    printer_id=eff_printer,
                )
                return {
                    "success": True,
                    "filament": resolved.to_dict(),
                    "is_brand_specific": resolved.is_brand_specific,
                    "warnings": resolved.warnings,
                }
            except Exception as exc:
                _logger.exception("Error in resolve_filament_profile")
                return _srv._error_dict(
                    f"Failed to resolve filament: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ---------------------------------------------------------------
        # Template decoration profiles and design styles are Pro features.
        # See kiln_pro/plugins/template_decoration_tools.py and
        # kiln_pro/plugins/design_styles_tools.py.

        _logger.debug("Registered design tools")


plugin = _DesignToolsPlugin()
