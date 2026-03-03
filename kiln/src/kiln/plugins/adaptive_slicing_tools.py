"""Adaptive slicing tool plugin.

Extracts the adaptive slicing MCP tools into a focused plugin module.
Provides tools for geometry analysis, material profiles, adaptive plan
generation, slicer config export, and time savings estimation.

Discovered and registered automatically by
:func:`~kiln.plugin_loader.register_all_plugins`.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Standalone functions — importable for direct calls and testing.
# ---------------------------------------------------------------------------


def analyze_model_geometry(
    model_path: str | None = None,
    model_stats: dict[str, Any] | None = None,
) -> dict:
    """Detect geometric regions in a 3D model that affect slicing.

    Analyzes model geometry to identify overhangs, bridges, thin walls,
    top/bottom surfaces, fine details, and curved surfaces.  Each region
    gets optimized slicing parameters in the adaptive plan.

    Args:
        model_path: Path to an STL or 3MF file for analysis.
        model_stats: Pre-computed geometry statistics dict (from slicer
            preview or external tool).  Keys include ``height_mm``,
            ``overhangs``, ``bridges``, ``thin_walls``, etc.

    Provide either ``model_path`` or ``model_stats`` (or both —
    ``model_stats`` takes precedence).
    """
    from kiln.adaptive_slicer import get_adaptive_slicer

    try:
        slicer = get_adaptive_slicer()
        regions = slicer.analyze_geometry(
            model_path=model_path,
            model_stats=model_stats,
        )
        return {
            "success": True,
            "regions": [r.to_dict() for r in regions],
            "region_count": len(regions),
        }
    except Exception as exc:
        _logger.exception("Error in analyze_model_geometry")
        return {"success": False, "error": str(exc)}


def get_material_slicing_profile(
    material: str,
    nozzle_diameter_mm: float = 0.4,
) -> dict:
    """Get material-specific slicing constraints for adaptive slicing.

    Returns layer height limits, bridge/overhang parameters, fan speeds,
    and other material-tuned values used by the adaptive slicer.

    Args:
        material: Material name — PLA, PETG, ABS, TPU, ASA, Nylon, PC,
            PVA, or HIPS.
        nozzle_diameter_mm: Nozzle diameter in mm (affects layer height
            limits).  Default 0.4mm.
    """
    from kiln.adaptive_slicer import get_adaptive_slicer

    try:
        slicer = get_adaptive_slicer()
        profile = slicer.get_material_profile(material, nozzle_diameter_mm=nozzle_diameter_mm)
        return {
            "success": True,
            "profile": profile.to_dict(),
        }
    except Exception as exc:
        _logger.exception("Error in get_material_slicing_profile")
        return {"success": False, "error": str(exc)}


def generate_adaptive_slicing_plan(
    regions: list[dict[str, Any]],
    material: str,
    model_height_mm: float,
    model_name: str = "",
    printer: str | None = None,
    nozzle_diameter_mm: float = 0.4,
    mode: str = "balanced",
) -> dict:
    """Generate a per-layer adaptive slicing plan.

    Creates a layer-by-layer plan with variable heights, speeds, and
    cooling based on detected geometry regions and material constraints.

    Args:
        regions: List of region dicts from ``analyze_model_geometry``.
            Each dict needs ``region_type``, ``z_start_mm``,
            ``z_end_mm``, and ``area_pct``.
        material: Material name (PLA, PETG, ABS, etc.).
        model_height_mm: Total model height in mm.
        model_name: Optional model name for record keeping.
        printer: Optional printer identifier.
        nozzle_diameter_mm: Nozzle diameter in mm.
        mode: Adaptive strategy — "balanced" (default), "quality_first",
            "speed_first", or "material_optimized".
    """
    from kiln.adaptive_slicer import (
        AdaptiveMode,
        AdaptiveSlicerError,
        get_adaptive_slicer,
    )

    try:
        slicer = get_adaptive_slicer()
        mat_profile = slicer.get_material_profile(material, nozzle_diameter_mm=nozzle_diameter_mm)

        try:
            adaptive_mode = AdaptiveMode(mode.lower())
        except ValueError:
            valid = ", ".join(m.value for m in AdaptiveMode)
            return {
                "success": False,
                "error": f"Invalid mode '{mode}'. Valid: {valid}.",
            }

        from kiln.adaptive_slicer import GeometryRegion, RegionType

        geo_regions: list[GeometryRegion] = []
        for item in regions:
            try:
                rtype = RegionType(item["region_type"])
            except (KeyError, ValueError):
                continue
            geo_regions.append(
                GeometryRegion(
                    region_type=rtype,
                    z_start_mm=float(item.get("z_start_mm", 0.0)),
                    z_end_mm=float(item.get("z_end_mm", model_height_mm)),
                    area_pct=float(item.get("area_pct", 10.0)),
                    min_feature_size_mm=(float(item["min_feature_size_mm"]) if "min_feature_size_mm" in item else None),
                    overhang_angle=(float(item["overhang_angle"]) if "overhang_angle" in item else None),
                    bridge_length_mm=(float(item["bridge_length_mm"]) if "bridge_length_mm" in item else None),
                    wall_thickness_mm=(float(item["wall_thickness_mm"]) if "wall_thickness_mm" in item else None),
                )
            )

        plan = slicer.generate_plan(
            geo_regions,
            mat_profile,
            mode=adaptive_mode,
            model_height_mm=model_height_mm,
            model_name=model_name,
            printer=printer,
            nozzle_diameter_mm=nozzle_diameter_mm,
        )
        return {
            "success": True,
            "plan": plan.to_dict(),
        }
    except AdaptiveSlicerError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        _logger.exception("Error in generate_adaptive_slicing_plan")
        return {"success": False, "error": str(exc)}


def export_adaptive_slicer_config(
    plan_data: dict[str, Any],
    slicer: str = "prusaslicer",
) -> dict:
    """Export an adaptive slicing plan as slicer-compatible configuration.

    Converts a plan to the target slicer's format — PrusaSlicer and
    OrcaSlicer use variable layer height data, Cura uses adaptive
    layers plugin format.

    Args:
        plan_data: Plan dict from ``generate_adaptive_slicing_plan``.
        slicer: Target slicer — "prusaslicer", "orcaslicer", "cura",
            or "generic".
    """
    from kiln.adaptive_slicer import (
        AdaptiveLayerPlan,
        AdaptiveMode,
        RegionType,
        SlicerTarget,
        get_adaptive_slicer,
    )

    try:
        try:
            target = SlicerTarget(slicer.lower())
        except ValueError:
            valid = ", ".join(s.value for s in SlicerTarget)
            return {
                "success": False,
                "error": f"Invalid slicer '{slicer}'. Valid: {valid}.",
            }

        # Reconstruct plan from dict.
        plan = AdaptiveLayerPlan(
            plan_id=plan_data.get("plan_id", "export"),
            model_name=plan_data.get("model_name", ""),
            material=plan_data.get("material", "PLA"),
            printer=plan_data.get("printer"),
            mode=AdaptiveMode(plan_data.get("mode", "balanced")),
            nozzle_diameter_mm=float(plan_data.get("nozzle_diameter_mm", 0.4)),
            total_layers=int(plan_data.get("total_layers", 0)),
            total_height_mm=float(plan_data.get("total_height_mm", 0.0)),
            min_layer_height_mm=float(plan_data.get("min_layer_height_mm", 0.08)),
            max_layer_height_mm=float(plan_data.get("max_layer_height_mm", 0.32)),
            layer_heights=plan_data.get("layer_heights", []),
            layer_regions=[
                [RegionType(r) for r in layer_regions] for layer_regions in plan_data.get("layer_regions", [])
            ],
            layer_speeds=plan_data.get("layer_speeds", []),
            layer_cooling=plan_data.get("layer_cooling", []),
            estimated_time_minutes=plan_data.get("estimated_time_minutes"),
            estimated_savings_pct=plan_data.get("estimated_savings_pct"),
            created_at=plan_data.get("created_at", ""),
        )

        adaptive = get_adaptive_slicer()
        config = adaptive.export_config(plan, slicer=target)
        return {
            "success": True,
            "config": config.to_dict(),
        }
    except Exception as exc:
        _logger.exception("Error in export_adaptive_slicer_config")
        return {"success": False, "error": str(exc)}


def estimate_adaptive_time_savings(
    plan_data: dict[str, Any],
    uniform_height_mm: float = 0.2,
) -> dict:
    """Compare adaptive plan time savings vs uniform layer height.

    Shows layer count reduction, estimated time savings, and percentage
    improvement.

    Args:
        plan_data: Plan dict from ``generate_adaptive_slicing_plan``.
        uniform_height_mm: Reference uniform layer height for comparison
            (default 0.2mm).
    """
    from kiln.adaptive_slicer import (
        AdaptiveLayerPlan,
        AdaptiveMode,
        RegionType,
        get_adaptive_slicer,
    )

    try:
        plan = AdaptiveLayerPlan(
            plan_id=plan_data.get("plan_id", "estimate"),
            model_name=plan_data.get("model_name", ""),
            material=plan_data.get("material", "PLA"),
            mode=AdaptiveMode(plan_data.get("mode", "balanced")),
            nozzle_diameter_mm=float(plan_data.get("nozzle_diameter_mm", 0.4)),
            total_layers=int(plan_data.get("total_layers", 0)),
            total_height_mm=float(plan_data.get("total_height_mm", 0.0)),
            min_layer_height_mm=float(plan_data.get("min_layer_height_mm", 0.08)),
            max_layer_height_mm=float(plan_data.get("max_layer_height_mm", 0.32)),
            layer_heights=plan_data.get("layer_heights", []),
            layer_regions=[
                [RegionType(r) for r in layer_regions] for layer_regions in plan_data.get("layer_regions", [])
            ],
            layer_speeds=plan_data.get("layer_speeds", []),
            layer_cooling=plan_data.get("layer_cooling", []),
            estimated_time_minutes=plan_data.get("estimated_time_minutes"),
        )

        adaptive = get_adaptive_slicer()
        savings = adaptive.estimate_time_savings(plan, uniform_height_mm=uniform_height_mm)
        return {
            "success": True,
            "savings": savings,
        }
    except Exception as exc:
        _logger.exception("Error in estimate_adaptive_time_savings")
        return {"success": False, "error": str(exc)}


def quick_adaptive_plan(
    material: str,
    model_height_mm: float,
    model_name: str = "",
    nozzle_diameter_mm: float = 0.4,
    mode: str = "balanced",
    printer: str | None = None,
    regions: list[dict[str, Any]] | None = None,
) -> dict:
    """All-in-one adaptive slicing: analyze geometry + generate plan.

    Convenience tool that combines geometry analysis and plan generation
    in a single call.  Ideal when you have basic model info and want a
    quick adaptive plan without multiple tool calls.

    Args:
        material: Material name (PLA, PETG, ABS, etc.).
        model_height_mm: Total model height in mm.
        model_name: Optional model name.
        nozzle_diameter_mm: Nozzle diameter (default 0.4mm).
        mode: Adaptive strategy — "balanced", "quality_first",
            "speed_first", or "material_optimized".
        printer: Optional printer identifier.
        regions: Optional list of region dicts.  If omitted, a default
            STANDARD region spanning the full height is used.
    """
    from kiln.adaptive_slicer import AdaptiveMode, AdaptiveSlicerError, get_adaptive_slicer

    try:
        try:
            adaptive_mode = AdaptiveMode(mode.lower())
        except ValueError:
            valid = ", ".join(m.value for m in AdaptiveMode)
            return {
                "success": False,
                "error": f"Invalid mode '{mode}'. Valid: {valid}.",
            }

        slicer = get_adaptive_slicer()
        plan = slicer.quick_plan(
            material=material,
            model_height_mm=model_height_mm,
            model_name=model_name,
            nozzle_diameter_mm=nozzle_diameter_mm,
            mode=adaptive_mode,
            printer=printer,
            regions=regions,
        )
        return {
            "success": True,
            "plan": plan.to_dict(),
        }
    except AdaptiveSlicerError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        _logger.exception("Error in quick_adaptive_plan")
        return {"success": False, "error": str(exc)}


def list_supported_materials() -> dict:
    """List all materials with adaptive slicing profiles.

    Returns material names with their key slicing constraints
    (layer height limits, bridge parameters, overhang angles).
    """
    from kiln.adaptive_slicer import get_adaptive_slicer

    try:
        slicer = get_adaptive_slicer()
        materials = slicer.list_supported_materials()
        summaries: list[dict[str, Any]] = []
        for mat in materials:
            profile = slicer.get_material_profile(mat)
            summaries.append(
                {
                    "material": mat,
                    "layer_range_mm": f"{profile.min_layer_height_mm}-{profile.max_layer_height_mm}",
                    "optimal_mm": profile.optimal_layer_height_mm,
                    "bridge_mm": profile.bridge_layer_height_mm,
                    "overhang_max_angle": profile.overhang_max_angle,
                }
            )
        return {
            "success": True,
            "materials": summaries,
            "count": len(summaries),
        }
    except Exception as exc:
        _logger.exception("Error in list_supported_materials")
        return {"success": False, "error": str(exc)}


def get_adaptive_plan_summary(plan_data: dict[str, Any]) -> dict:
    """Generate a human-readable summary of an adaptive slicing plan.

    Args:
        plan_data: Plan dict from ``generate_adaptive_slicing_plan``
            or ``quick_adaptive_plan``.

    Returns a structured summary with key metrics and region breakdown.
    """
    try:
        heights = plan_data.get("layer_heights", [])
        if not heights:
            return {
                "success": False,
                "error": "Plan has no layer heights.",
            }

        avg_height = sum(heights) / len(heights)
        min_h = min(heights)
        max_h = max(heights)

        # Count region occurrences.
        region_counts: dict[str, int] = {}
        for layer_regions in plan_data.get("layer_regions", []):
            for r in layer_regions:
                region_counts[r] = region_counts.get(r, 0) + 1

        summary_lines = [
            f"Model: {plan_data.get('model_name', 'Unknown')}",
            f"Material: {plan_data.get('material', 'Unknown')}",
            f"Mode: {plan_data.get('mode', 'balanced')}",
            f"Total layers: {len(heights)}",
            f"Total height: {plan_data.get('total_height_mm', 0):.2f} mm",
            f"Layer heights: {min_h:.3f} - {max_h:.3f} mm (avg {avg_height:.3f})",
        ]

        if plan_data.get("estimated_time_minutes"):
            summary_lines.append(f"Estimated time: {plan_data['estimated_time_minutes']:.1f} min")
        if plan_data.get("estimated_savings_pct") is not None:
            summary_lines.append(f"Estimated savings: {plan_data['estimated_savings_pct']:.1f}%")

        return {
            "success": True,
            "summary": "\n".join(summary_lines),
            "stats": {
                "total_layers": len(heights),
                "avg_layer_height_mm": round(avg_height, 4),
                "min_layer_height_mm": round(min_h, 4),
                "max_layer_height_mm": round(max_h, 4),
                "region_counts": region_counts,
            },
        }
    except Exception as exc:
        _logger.exception("Error in get_adaptive_plan_summary")
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Plugin class — registers standalone functions as MCP tools.
# ---------------------------------------------------------------------------


class _AdaptiveSlicingToolsPlugin:
    """MCP tools for adaptive slicing with geometry and material intelligence."""

    @property
    def name(self) -> str:
        return "adaptive_slicing_tools"

    @property
    def description(self) -> str:
        return "Adaptive slicing tools (geometry analysis, material profiles, plan generation, export)"

    def register(self, mcp: Any) -> None:
        """Register adaptive slicing tools with the MCP server."""

        mcp.tool()(analyze_model_geometry)
        mcp.tool()(get_material_slicing_profile)
        mcp.tool()(generate_adaptive_slicing_plan)
        mcp.tool()(export_adaptive_slicer_config)
        mcp.tool()(estimate_adaptive_time_savings)
        mcp.tool()(quick_adaptive_plan)
        mcp.tool()(list_supported_materials)
        mcp.tool()(get_adaptive_plan_summary)

        _logger.debug("Registered adaptive slicing tools")


plugin = _AdaptiveSlicingToolsPlugin()
