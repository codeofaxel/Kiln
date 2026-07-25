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


def _public_template_summary(template: dict[str, Any]) -> dict[str, Any]:
    """Project a public template to its discovery fields."""
    compatibility = template.get("material_compatibility") or {}
    return {
        "template_id": template.get("template_id"),
        "display_name": template.get("display_name"),
        "description": template.get("description"),
        "use_cases": list(template.get("use_cases") or []),
        "best_materials": list(compatibility.get("excellent") or []),
    }


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
            """Compatibility lookup for a material's public design floor.

            Returns the same public safety and process fields as
            ``get_material_properties``. For deeper engineering guidance,
            ask one specific question with ``answer_material_question``.

            Args:
                material: Material ID (for example ``"pla"`` or ``"petg"``).
            """
            from kiln.design_intelligence import get_public_material_profile

            try:
                profile = get_public_material_profile(material)
                if profile is None:
                    return {
                        "success": False,
                        "error": f"Unknown material: {material}.",
                    }
                result = profile.to_dict()
                result["success"] = True
                return result
            except Exception as exc:
                _logger.error("Material profile failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def list_design_materials() -> dict:
            """List public material identifiers and safety/process summaries.

            The list is sourced only from public Kiln data and does not
            include optional engineering enrichment.
            """
            from kiln.design_intelligence import list_public_material_profiles

            try:
                profiles = list_public_material_profiles()
                summaries = []
                for p in profiles:
                    summaries.append(
                        {
                            "material_id": p.material_id,
                            "display_name": p.display_name,
                            "category": p.category,
                            "max_service_temp_c": p.thermal.get("max_service_temp_c"),
                            "food_safe": p.chemical.get("food_safe"),
                            "ease_of_print": p.thermal.get("warping_tendency"),
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

                primary_slug = _slug(
                    result.get("material") or result.get("recommended_material")
                )

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

                # Skin-contact floor (worn / handled against skin).  Free +
                # offline: the caution comes from the public skin_contact.json
                # floor, so it reaches every install without kiln-pro; when
                # kiln-pro is present the per-exposure verdict is layered on.
                # Advisory only — never asserts skin-safe, never blocks.
                from kiln.design_intelligence import (
                    get_skin_contact_floor,
                    use_case_implies_skin_contact,
                )
                if primary_slug and use_case_implies_skin_contact(requirements):
                    floor = get_skin_contact_floor(primary_slug)
                    if floor is not None:
                        sc = {
                            "material": floor.display_name,
                            "concern_level": floor.concern_level,
                            "honesty_note": floor.honesty_note,
                            "named_hazards": floor.named_hazards,
                            "refer_to_medical": floor.refer_to_medical,
                            "never_skin_safe": True,
                        }
                        # Say when the caution is inherited rather than written
                        # about this exact grade — the user should know which
                        # they are reading, and an inherited answer is still
                        # infinitely better than the silence this replaced.
                        if floor.is_uncharacterized:
                            sc["record_basis"] = "uncharacterized"
                        elif floor.inherited_from:
                            sc["record_basis"] = "inherited"
                            sc["inherited_from"] = floor.inherited_from
                        # Pro enrichment: the per-exposure verdict when
                        # kiln-pro is installed; free tier stops at the floor.
                        try:
                            from kiln_pro.skin_contact import engine as _skin  # noqa: WPS433

                            adv = _skin.free_floor_advisory(primary_slug, requirements)
                            if adv is not None:
                                sc["verdict"] = adv.get("verdict")
                                sc["exposure"] = adv.get("exposure")
                        except Exception:  # noqa: BLE001 — enrichment is best-effort; the floor stands
                            pass
                        result["skin_contact"] = sc
                        result.setdefault("warnings", []).append(
                            "SKIN CONTACT: no 3D-printed part is skin-safe, "
                            "hypoallergenic, or biocompatible. "
                            + (floor.honesty_note or "")
                            + " For any mouth, eye, broken-skin, piercing, or "
                            "implant use, see a medical professional."
                        )

                # Bonding caveat (reverse-link to the adhesive intelligence),
                # surfaced at material-selection time.  Two tiers from the
                # merged profile: Pro (overlay supplied a precise verdict) gets
                # the full block + caveat pointing at recommend_adhesive; free
                # (only the public common-knowledge `hard_to_bond` floor flag)
                # gets a minimal block + an upgrade nudge.  The warning fires on
                # difficulty (hard/very_hard), never on primer_required alone,
                # so a flexible material (TPU) still warns.  Enrichment only —
                # never break the recommendation.
                try:
                    from kiln.design_intelligence import get_material_profile

                    profile = (
                        get_material_profile(primary_slug) if primary_slug else None
                    )
                    if profile is not None and profile.bonding:
                        b = profile.bonding
                        if b.get("bonding_difficulty"):  # Pro overlay merged
                            result["bonding"] = {
                                "material": primary_slug,
                                "difficulty": b.get("bonding_difficulty"),
                                "primer_required": b.get("primer_required", False),
                                "recommended_primer": b.get("recommended_primer"),
                                "note": b.get("bonding_note"),
                                "for_details_use": "recommend_adhesive",
                            }
                        elif b.get("hard_to_bond"):  # free common-knowledge floor
                            result["bonding"] = {
                                "material": primary_slug,
                                "hard_to_bond": True,
                                "upgrade_url": "https://kiln3d.com/pricing",
                            }
                        caveat = profile.bonding_caveat()
                        if caveat:
                            result.setdefault("warnings", []).insert(0, caveat)
                except Exception:  # noqa: BLE001 - enrichment must never break the rec
                    pass
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
                load_across_layers: True when the load pulls the layer
                    interfaces apart (load along the build/Z direction —
                    the WEAK direction for FDM; capacity is derated).
                    False when the load acts within the layer planes
                    (e.g. a bracket printed lying flat — the strong
                    direction; full table value). If unsure, leave True:
                    it is the conservative default.
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
                # Disclose the flat safety margin: the lookup uses a fixed 3x
                # regardless of use, so it is NOT enough for high-consequence
                # parts (life-safety/lifting want 5-10x — see design_for_load).
                result["safety_basis"] = (
                    "Uses a general 3x safety margin, not tuned to your "
                    "application. Life-safety and lifting parts need more "
                    "(typically 5-10x) — size those with an engineer."
                )
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
                # The `chemicals` map in this report is coarse per-class
                # (fuels/solvents/acids…).  Per-reagent cited verdicts are
                # served one query at a time by check_chemical_resistance
                # (kiln-pro, locally or via the hosted API) — this surface
                # carries only the pointer, never the curated matrix.
                result["cited_chemical_resistance"] = (
                    "For a specific reagent (diesel, acetone, bleach, vinegar, "
                    "UV …) with a cited verdict and the as-printed caveat, use "
                    "check_chemical_resistance. Safety warnings are free; "
                    "curated verdicts are a kiln-pro feature — "
                    "https://kiln3d.com/pricing."
                )
                return result
            except Exception as exc:
                _logger.error(
                    "Environment compatibility check failed: %s",
                    exc,
                    exc_info=True,
                )
                return {"success": False, "error": str(exc)}

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
            """Get one template's public discovery and safety-floor fields.

            Args:
                template: Template identifier.
            """
            from kiln.design_intelligence import get_public_design_template, list_public_design_templates

            try:
                record = get_public_design_template(template)
                if record is None:
                    available = [
                        item["template_id"]
                        for item in list_public_design_templates()
                    ]
                    return {
                        "success": False,
                        "error": f"Unknown template: {template}. Available: {', '.join(available)}.",
                    }
                result = dict(record)
                result["success"] = True
                return result
            except Exception as exc:
                _logger.error("Design template failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def list_design_templates_catalog() -> dict:
            """List public design-template discovery summaries."""
            from kiln.design_intelligence import list_public_design_templates

            try:
                summaries = [
                    _public_template_summary(template)
                    for template in list_public_design_templates()
                ]
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
            """Find public design-template summaries for a use case.

            Args:
                use_case: What you're designing (e.g. "enclosure",
                    "gear train", "battery cover", "vase").
            """
            from kiln.design_intelligence import find_public_design_templates

            try:
                templates = find_public_design_templates(use_case)
                return {
                    "success": True,
                    "templates": [
                        _public_template_summary(template)
                        for template in templates
                    ],
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
            symptom: str,
        ) -> dict:
            """Diagnose a 3D printing problem by material and symptom.

            Searches the troubleshooting knowledge base for matching issues
            and returns root causes, prioritised fixes, prevention tips, and
            storage/drying requirements for that specific symptom.

            Use this when a user reports a print failure, quality issue, or
            asks "why is my print doing X?"

            Examples:
                material="pla", symptom="stringing"
                material="petg", symptom="poor layer adhesion"
                material="abs", symptom="warping"
            Args:
                material: Material ID (e.g. "pla", "petg", "abs", "tpu",
                    "nylon", "polycarbonate", "asa", "cf_nylon").
                symptom: Symptom keywords to search for (e.g.
                    "stringing", "warping", "clog", "brittle").
            """
            from kiln.design_intelligence import troubleshoot_print_issue as _troubleshoot

            try:
                if not isinstance(symptom, str) or not symptom.strip():
                    return {
                        "success": False,
                        "error": "symptom is required for troubleshooting.",
                    }
                result = _troubleshoot(material, symptom.strip())
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
            material: str,
        ) -> dict:
            """Check if a specific printer can handle a material.

            Returns compatibility status (compatible / needs_upgrade /
            not_compatible), any required hardware upgrades (enclosure,
            hardened nozzle, dry box), and practical notes.

            Use this when a user asks "can my Ender 3 print nylon?" or
            another specific printer/material question.

            Args:
                printer: Printer model ID (e.g. "ender3", "bambu_x1c",
                    "prusa_mk4", "voron_2"). Use underscores, lowercase.
                material: Material to check (e.g. "nylon", "abs").
            """
            from kiln.design_intelligence import (
                check_printer_material_compatibility as _check_compat,
            )
            from kiln.design_intelligence import (
                list_compatibility_printers,
            )

            try:
                if not isinstance(material, str) or not material.strip():
                    return {
                        "success": False,
                        "error": "material is required for compatibility checks.",
                    }
                report = _check_compat(printer, material.strip())
                if report is None:
                    available = ", ".join(list_compatibility_printers())
                    return {
                        "success": False,
                        "error": f"No compatibility data for printer '{printer}'. "
                        f"Available: {available}",
                    }
                out = report.to_dict()
                out["success"] = True
                return out
            except Exception as exc:
                _logger.error("Compatibility check failed: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def get_post_processing_guide(
            material: str,
            goal: str | None = None,
        ) -> dict:
            """Get bounded public post-processing help for one goal.

            Pass ``surface_finish``, ``paint``, ``strengthen``, or the name of
            one listed technique. Omitting ``goal`` returns a compact
            compatibility overview rather than the complete guide.

            Args:
                material: Material ID (e.g. "pla", "abs", "petg", "nylon").
                goal: Optional finishing goal or technique name.
            """
            from kiln.design_intelligence import (
                get_public_post_processing as _get_pp,
            )

            try:
                guide = _get_pp(material)
                if guide is None:
                    return {
                        "success": False,
                        "error": f"No post-processing data for '{material}'.",
                    }

                technique_overview = [
                    {
                        "name": technique.get("name"),
                        "difficulty": technique.get("difficulty"),
                    }
                    for technique in guide.techniques
                    if isinstance(technique, dict)
                ]
                strengthening_overview = [
                    {
                        "method": item.get("method"),
                        "applicable": item.get("applicable"),
                    }
                    if isinstance(item, dict)
                    else {"method": str(item), "applicable": None}
                    for item in guide.strengthening
                ]

                if goal is None:
                    return {
                        "success": True,
                        "material": guide.material,
                        "available_goals": [
                            "surface_finish",
                            "paint",
                            "strengthen",
                        ],
                        "techniques": technique_overview,
                        "paintability": {
                            "available": guide.paintability is not None,
                        },
                        "strengthening": strengthening_overview,
                        "upgrade_hint": guide.upgrade_hint,
                    }

                normalized_goal = goal.strip().lower().replace("-", "_").replace(" ", "_")
                if normalized_goal in {"surface", "finish", "finishing", "surface_finish"}:
                    answer: Any = {"techniques": guide.techniques}
                    resolved_goal = "surface_finish"
                elif normalized_goal in {"paint", "painting", "coat", "coating"}:
                    answer = {"paintability": guide.paintability}
                    resolved_goal = "paint"
                elif normalized_goal in {
                    "strength",
                    "strengthen",
                    "strengthening",
                    "anneal",
                    "annealing",
                }:
                    answer = {"strengthening": guide.strengthening}
                    resolved_goal = "strengthen"
                else:
                    match = next(
                        (
                            technique
                            for technique in guide.techniques
                            if isinstance(technique, dict)
                            and str(technique.get("name", "")).strip().lower()
                            == goal.strip().lower()
                        ),
                        None,
                    )
                    if match is None:
                        names = [
                            str(item["name"])
                            for item in technique_overview
                            if item.get("name")
                        ]
                        return {
                            "success": False,
                            "error": (
                                f"Unknown post-processing goal '{goal}'. "
                                "Use surface_finish, paint, strengthen, or one "
                                f"of: {', '.join(names)}"
                            ),
                        }
                    answer = {"technique": match}
                    resolved_goal = str(match.get("name"))

                return {
                    "success": True,
                    "material": guide.material,
                    "goal": resolved_goal,
                    "answer": answer,
                    "upgrade_hint": guide.upgrade_hint,
                }
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
            symptom: str,
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
                if not isinstance(symptom, str) or not symptom.strip():
                    return {
                        "success": False,
                        "error": "symptom is required for print diagnostics.",
                    }
                result = _get_diagnostic(
                    material,
                    symptom=symptom.strip(),
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
                response = {
                    "success": True,
                    "stl_path": stl_path,
                    "message": f"Compiled to {stl_path}",
                }
                # Surface an outdated-OpenSCAD notice at make-time (not just a
                # buried log) so a maker who skipped get_started still finds out
                # their engine is slow / SVG-broken and how to upgrade.
                from kiln.emboss_generator import openscad_version_warning

                _osw = openscad_version_warning()
                if _osw:
                    response["openscad_warning"] = _osw
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(response, level="quick")
                except ImportError:
                    return response
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
                response = {
                    "success": True,
                    **result,
                    "message": (
                        f"Updated {parameter_name}={new_value}, compiled to STL{suffix}."
                    ),
                }
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(response, level="quick")
                except ImportError:
                    return response
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
                    "material": resolved.material_id,
                    "display_name": resolved.display_name,
                    "is_brand_specific": resolved.is_brand_specific,
                    "print_settings": {
                        "nozzle_temp_c": {
                            "target": resolved.nozzle_temp_optimal_c,
                            "range": list(resolved.nozzle_temp_range_c),
                        },
                        "bed_temp_c": {
                            "target": resolved.bed_temp_optimal_c,
                            "range": list(resolved.bed_temp_range_c),
                        },
                        "max_volumetric_speed_mm3s": (
                            resolved.max_volumetric_speed_mm3s
                        ),
                        "max_print_speed_mms": resolved.max_print_speed_mms,
                    },
                    "preparation": {
                        "drying_temp_c": resolved.drying_temp_c,
                        "drying_time_hours": resolved.drying_time_hours,
                        "enclosure_required": resolved.enclosure_required,
                        "hardened_nozzle_required": (
                            resolved.hardened_nozzle_required
                        ),
                        "ams_compatible": resolved.ams_compatible,
                    },
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
