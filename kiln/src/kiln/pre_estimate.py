"""Pre-generation print estimation — time, cost, and material usage from dimensions alone.

Answers "how long / how much / how many grams?" **before** any model is
generated or sliced.  Works entirely from geometry math and printer speed
profiles — no files, no slicer, no mesh required.

The core function :func:`estimate_from_dimensions` accepts a bounding box
(width × depth × height in mm), material(s), infill%, and optional printer
model.  It returns a :class:`PreEstimate` dataclass with time, cost, and
per-filament breakdown.

For convenience, :func:`estimate_from_template` accepts a design template
ID (e.g. ``"phone_stand"``) and resolves dimensions from its default
parameters automatically.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Printer speed profiles (from slicer_profiles.json, cached lazily)
# ---------------------------------------------------------------------------

# Effective print speed is a weighted average of perimeter/infill/first-layer
# speeds.  Weights reflect typical time distribution for a standard print:
# ~35% perimeters, ~40% infill, ~15% first layer, ~10% travel/retract.
_SPEED_WEIGHT_PERIMETER = 0.35
_SPEED_WEIGHT_INFILL = 0.40
_SPEED_WEIGHT_FIRST_LAYER = 0.15
_SPEED_WEIGHT_OVERHEAD = 0.10  # travel, retract, acceleration ramps

# Fallback speeds when no printer profile is available (mm/s).
_DEFAULT_PERIMETER_SPEED = 60.0
_DEFAULT_INFILL_SPEED = 80.0
_DEFAULT_FIRST_LAYER_SPEED = 25.0
_DEFAULT_TRAVEL_SPEED = 150.0

# Per-printer tool change times (seconds per swap).
# Includes unload + load + prime + purge + wipe + park.
# Sourced from community benchmarks, official firmware release notes,
# and slicer default flush volumes.  See slicer_profiles.json
# tool_change field for the authoritative per-printer values.
#
# Key data sources:
# - Bambu AMS: BambuStudio GitHub #5445 (~99s with default 400mm³ flush)
# - Bambu AMS Lite: Similar mechanism, slightly shorter PTFE path (~90s)
# - Prusa MMU3: Prusa blog (2025 FW update, ~42s mechanical + purge = ~65s)
# - Prusa MMU2S: Community benchmarks (~80s average, high failure rate)
# - Voron ERCF V2: Community builds, Happy Hare firmware (~45s average)
# - Manual M600: Human-dependent pause, ~90s conservative average
_DEFAULT_TOOL_CHANGE_SECONDS = 90  # fallback if no printer-specific data

# Manual tool change (M600 pause): includes human intervention.
_MANUAL_TOOL_CHANGE_SECONDS = 90

# Travel overhead multiplier: accounts for non-printing moves, acceleration
# ramps, retraction, z-hops, and firmware processing.  Empirically tuned
# against real Bambu A1 prints — a 120x120x15mm coaster takes ~45 min,
# pure extrusion math gives ~35 min, so 1.3x is the right ballpark.
_TRAVEL_OVERHEAD_MULTIPLIER = 1.30

# ---------------------------------------------------------------------------
# Material database (re-uses cost_estimator.BUILTIN_MATERIALS)
# ---------------------------------------------------------------------------


def _get_material_profile(material: str) -> dict[str, Any]:
    """Look up material density and cost, falling back to PLA."""
    from kiln.cost_estimator import BUILTIN_MATERIALS

    profile = BUILTIN_MATERIALS.get(material.upper())
    if profile is None:
        _logger.debug("Unknown material '%s', using PLA defaults", material)
        profile = BUILTIN_MATERIALS["PLA"]
    return {
        "name": profile.name,
        "density_g_per_cm3": profile.density_g_per_cm3,
        "cost_per_kg_usd": profile.cost_per_kg_usd,
        "filament_diameter_mm": profile.filament_diameter_mm,
    }


# ---------------------------------------------------------------------------
# Printer speed lookup
# ---------------------------------------------------------------------------

_slicer_profiles_cache: dict[str, dict[str, Any]] | None = None


def _load_slicer_profiles() -> dict[str, dict[str, Any]]:
    """Lazy-load slicer_profiles.json."""
    global _slicer_profiles_cache  # noqa: PLW0603
    if _slicer_profiles_cache is not None:
        return _slicer_profiles_cache

    profiles_path = os.path.join(
        os.path.dirname(__file__), "data", "slicer_profiles.json"
    )
    try:
        with open(profiles_path) as fh:
            _slicer_profiles_cache = json.load(fh)
    except Exception:
        _logger.debug("Could not load slicer_profiles.json, using defaults")
        _slicer_profiles_cache = {}
    return _slicer_profiles_cache


def _get_printer_speeds(printer_id: str | None) -> dict[str, float]:
    """Return effective speeds (mm/s) for a printer model.

    Returns a dict with keys: perimeter, infill, first_layer, travel.
    """
    if printer_id:
        profiles = _load_slicer_profiles()
        profile = profiles.get(printer_id, {})
        settings = profile.get("settings", {})
        if settings:
            return {
                "perimeter": float(settings.get("perimeter_speed", _DEFAULT_PERIMETER_SPEED)),
                "infill": float(settings.get("infill_speed", _DEFAULT_INFILL_SPEED)),
                "first_layer": float(settings.get("first_layer_speed", _DEFAULT_FIRST_LAYER_SPEED)),
                "travel": float(settings.get("travel_speed", _DEFAULT_TRAVEL_SPEED)),
            }

    return {
        "perimeter": _DEFAULT_PERIMETER_SPEED,
        "infill": _DEFAULT_INFILL_SPEED,
        "first_layer": _DEFAULT_FIRST_LAYER_SPEED,
        "travel": _DEFAULT_TRAVEL_SPEED,
    }


def _get_printer_layer_height(printer_id: str | None) -> float:
    """Return default layer height for a printer model."""
    if printer_id:
        profiles = _load_slicer_profiles()
        profile = profiles.get(printer_id, {})
        settings = profile.get("settings", {})
        if settings:
            return float(settings.get("layer_height", 0.2))
    return 0.2


def _get_printer_infill(printer_id: str | None) -> float:
    """Return default infill percentage for a printer model."""
    if printer_id:
        profiles = _load_slicer_profiles()
        profile = profiles.get(printer_id, {})
        settings = profile.get("settings", {})
        if settings:
            fill_str = settings.get("fill_density", "20%")
            return float(fill_str.replace("%", ""))
    return 20.0


def _get_printer_tool_change(printer_id: str | None) -> dict[str, Any]:
    """Return tool change data for a printer model from slicer_profiles.json.

    Returns a dict with keys:
        tool_change_seconds (int): Total per-swap wall-clock time including purge.
        tool_changer (str): "ams", "ams_lite", "mmu3", "mmu2s", "ercf", "none".
        has_auto_tool_change (bool): True if the printer has an auto changer.
    """
    if printer_id:
        profiles = _load_slicer_profiles()
        profile = profiles.get(printer_id, {})
        tc = profile.get("tool_change", {})
        if tc:
            changer = tc.get("tool_changer", "none")
            return {
                "tool_change_seconds": int(tc.get("tool_change_seconds", _DEFAULT_TOOL_CHANGE_SECONDS)),
                "tool_changer": changer,
                "has_auto_tool_change": changer != "none",
            }

    return {
        "tool_change_seconds": _DEFAULT_TOOL_CHANGE_SECONDS,
        "tool_changer": "none",
        "has_auto_tool_change": False,
    }


# ---------------------------------------------------------------------------
# Design template dimension extraction
# ---------------------------------------------------------------------------


def _resolve_template_dimensions(
    template_id: str,
    param_overrides: dict[str, Any] | None = None,
) -> tuple[float, float, float]:
    """Resolve a template ID to approximate (width, depth, height) in mm.

    Uses the template's default parameters to compute the bounding box.
    ``param_overrides`` can override any default parameter value.

    Returns:
        A (width_mm, depth_mm, height_mm) tuple.

    Raises:
        ValueError: If the template is not found.
    """
    templates_path = os.path.join(
        os.path.dirname(__file__), "data", "design_templates.json"
    )
    try:
        with open(templates_path) as fh:
            templates = json.load(fh)
    except Exception as exc:
        raise ValueError(f"Could not load design templates: {exc}") from exc

    tpl = templates.get(template_id)
    if not tpl:
        available = [k for k in templates if not k.startswith("_")]
        raise ValueError(
            f"Template '{template_id}' not found. "
            f"Available: {', '.join(sorted(available)[:10])}..."
        )

    # Merge defaults with overrides
    params: dict[str, Any] = {}
    for pname, pdef in tpl.get("parameters", {}).items():
        params[pname] = pdef.get("default", 0)
    if param_overrides:
        params.update(param_overrides)

    # Heuristic: extract width, depth, height from parameter names.
    # Templates use various naming conventions.
    width = _extract_dim(params, ["width", "inner_width", "plate_width", "phone_width", "diameter"])
    depth = _extract_dim(params, ["depth", "inner_depth", "base_depth", "length", "arm_length", "wall_length"])
    height = _extract_dim(params, ["height", "inner_height", "plate_height", "lip_height", "total_height"])

    # Minimum bounds — no dimension should be zero
    width = max(width, 10.0)
    depth = max(depth, 10.0)
    height = max(height, 5.0)

    return (width, depth, height)


def _extract_dim(params: dict[str, Any], candidates: list[str]) -> float:
    """Return the first matching dimension from params, or 0."""
    for name in candidates:
        val = params.get(name)
        if val is not None and float(val) > 0:
            return float(val)
    return 0.0


# ---------------------------------------------------------------------------
# Core estimation engine
# ---------------------------------------------------------------------------


@dataclass
class FilamentUsage:
    """Per-filament material usage and cost."""

    material: str
    weight_grams: float
    length_meters: float
    cost_usd: float
    volume_fraction: float  # 0.0–1.0, portion of total print volume
    role: str  # "body", "accent", "surface_detail", etc.

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PreEstimate:
    """Pre-generation estimate result — no files needed."""

    # Dimensions
    width_mm: float
    depth_mm: float
    height_mm: float
    volume_mm3: float

    # Time
    estimated_time_seconds: int
    estimated_time_human: str
    tool_changes: int
    tool_change_time_seconds: int
    tool_change_type: str  # "ams", "mmu", "manual", "none"

    # Filament breakdown
    filaments: list[FilamentUsage]
    total_weight_grams: float
    total_filament_meters: float

    # Cost
    filament_cost_usd: float
    electricity_cost_usd: float
    total_cost_usd: float

    # Metadata
    printer_id: str | None
    infill_percent: float
    layer_height_mm: float
    nozzle_mm: float
    confidence: str  # "high", "medium", "low"
    confidence_notes: list[str]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["filaments"] = [f.to_dict() for f in self.filaments]
        return d


def estimate_from_dimensions(
    width_mm: float,
    depth_mm: float,
    height_mm: float,
    *,
    materials: list[str] | None = None,
    material_fractions: list[float] | None = None,
    material_roles: list[str] | None = None,
    infill_percent: float | None = None,
    layer_height_mm: float | None = None,
    nozzle_mm: float = 0.4,
    wall_layers: int = 3,
    printer_id: str | None = None,
    electricity_rate: float = 0.12,
    printer_wattage: float = 200.0,
) -> PreEstimate:
    """Estimate print time, cost, and material usage from dimensions alone.

    This is the core estimation function.  It computes shell volume,
    infill volume, material weight, filament length, print time, tool
    swap overhead, electricity cost, and total cost — all from geometry
    math and printer speed profiles.

    Args:
        width_mm: Part width (X) in mm.
        depth_mm: Part depth (Y) in mm.
        height_mm: Part height (Z) in mm.
        materials: List of filament materials.  Default ``["PLA"]``.
            For multi-material prints, list all materials in order
            (e.g. ``["PLA", "PLA"]`` for two-color same-material,
            or ``["PLA", "PETG"]`` for mixed).
        material_fractions: Volume fraction for each material (0.0–1.0).
            Must sum to 1.0.  Default: first material gets 0.85 (body),
            remaining materials split the rest equally.
        material_roles: Human-readable role for each material
            (e.g. ``["body", "accent"]``).  Default: first is "body",
            rest are "accent_1", "accent_2", etc.
        infill_percent: Interior fill density.  Default: from printer
            profile, or 20%.
        layer_height_mm: Layer height.  Default: from printer profile,
            or 0.2mm.
        nozzle_mm: Nozzle diameter (default 0.4mm).
        wall_layers: Number of perimeter shells (default 3).
        printer_id: Optional printer model ID for speed/setting lookup
            (e.g. ``"bambu_a1"``, ``"prusa_mk4"``).
        electricity_rate: Cost per kWh in USD (default 0.12).
        printer_wattage: Printer power draw in watts (default 200).

    Returns:
        :class:`PreEstimate` with full breakdown.

    Raises:
        ValueError: If dimensions are non-positive or fractions invalid.
    """
    # --- Validate inputs ---
    if width_mm <= 0 or depth_mm <= 0 or height_mm <= 0:
        raise ValueError(
            f"All dimensions must be positive, got "
            f"{width_mm} × {depth_mm} × {height_mm} mm"
        )

    if materials is None:
        materials = ["PLA"]

    num_materials = len(materials)

    # Default fractions: body gets 85%, rest split equally
    if material_fractions is None:
        if num_materials == 1:
            material_fractions = [1.0]
        else:
            body_fraction = 0.85
            accent_fraction = (1.0 - body_fraction) / (num_materials - 1)
            material_fractions = [body_fraction] + [accent_fraction] * (num_materials - 1)

    if len(material_fractions) != num_materials:
        raise ValueError(
            f"material_fractions length ({len(material_fractions)}) must match "
            f"materials length ({num_materials})"
        )

    frac_sum = sum(material_fractions)
    if abs(frac_sum - 1.0) > 0.01:
        raise ValueError(
            f"material_fractions must sum to 1.0, got {frac_sum:.3f}"
        )

    # Default roles
    if material_roles is None:
        if num_materials == 1:
            material_roles = ["body"]
        else:
            material_roles = ["body"] + [f"accent_{i}" for i in range(1, num_materials)]

    if len(material_roles) != num_materials:
        raise ValueError(
            f"material_roles length ({len(material_roles)}) must match "
            f"materials length ({num_materials})"
        )

    # --- Resolve printer defaults ---
    effective_layer_height = layer_height_mm or _get_printer_layer_height(printer_id)
    effective_infill = infill_percent if infill_percent is not None else _get_printer_infill(printer_id)
    speeds = _get_printer_speeds(printer_id)

    confidence = "high"
    confidence_notes: list[str] = []
    warnings: list[str] = []

    if not printer_id:
        confidence = "medium"
        confidence_notes.append("No printer specified — using generic speed/infill defaults.")

    # --- Geometry: bounding box → volume estimate ---
    # Treat the part as a solid box, then compute shell + infill.
    # This is an approximation — real parts have varying cross-sections.
    # For most parametric products (trays, boxes, stands), the bounding
    # box is a good proxy.  For organic/sculpted models, it overestimates.
    bbox_volume_mm3 = width_mm * depth_mm * height_mm

    # For most FDM prints, the actual part volume is ~40-70% of the
    # bounding box (hollowed parts like trays, boxes with walls).
    # Use 0.55 as a middle ground.  Solid objects (nameplates, brackets)
    # are closer to 0.30 (mostly shell).
    fill_ratio = 0.55
    if height_mm <= 5.0:
        # Very thin/flat objects (nameplates, magnets) are mostly solid
        fill_ratio = 0.85
    elif height_mm < width_mm * 0.3 and height_mm < depth_mm * 0.3:
        # Flat objects (trays, coasters, plaques)
        fill_ratio = 0.70

    estimated_part_volume_mm3 = bbox_volume_mm3 * fill_ratio

    confidence_notes.append(
        f"Volume estimated at {fill_ratio:.0%} of bounding box "
        f"({bbox_volume_mm3:.0f} mm³ bbox → {estimated_part_volume_mm3:.0f} mm³ part)."
    )

    # --- Shell and infill volumes ---
    surface_area_mm2 = 2.0 * (
        width_mm * depth_mm + width_mm * height_mm + depth_mm * height_mm
    )
    shell_thickness_mm = wall_layers * nozzle_mm
    shell_volume_mm3 = surface_area_mm2 * shell_thickness_mm

    # Clamp shell to not exceed part volume
    shell_volume_mm3 = min(shell_volume_mm3, estimated_part_volume_mm3)

    interior_volume_mm3 = max(0.0, estimated_part_volume_mm3 - shell_volume_mm3)
    infill_volume_mm3 = interior_volume_mm3 * (effective_infill / 100.0)
    total_plastic_mm3 = shell_volume_mm3 + infill_volume_mm3

    # --- Per-filament breakdown ---
    filaments: list[FilamentUsage] = []
    total_weight_g = 0.0
    total_length_m = 0.0
    total_filament_cost = 0.0

    for i in range(num_materials):
        mat = _get_material_profile(materials[i])
        frac = material_fractions[i]
        mat_volume_mm3 = total_plastic_mm3 * frac

        # Weight: volume(mm³) / 1000 = cm³, × density = grams
        weight_g = (mat_volume_mm3 / 1000.0) * mat["density_g_per_cm3"]

        # Filament length: volume / cross-section area
        radius_mm = mat["filament_diameter_mm"] / 2.0
        cross_section_mm2 = math.pi * radius_mm * radius_mm
        length_mm = mat_volume_mm3 / cross_section_mm2 if cross_section_mm2 > 0 else 0.0
        length_m = length_mm / 1000.0

        # Cost
        cost = (weight_g / 1000.0) * mat["cost_per_kg_usd"]

        filaments.append(FilamentUsage(
            material=mat["name"],
            weight_grams=round(weight_g, 1),
            length_meters=round(length_m, 2),
            cost_usd=round(cost, 2),
            volume_fraction=round(frac, 3),
            role=material_roles[i],
        ))

        total_weight_g += weight_g
        total_length_m += length_m
        total_filament_cost += cost

    # --- Print time estimation ---
    # Effective print speed: weighted average of perimeter/infill/first-layer.
    effective_speed = (
        speeds["perimeter"] * _SPEED_WEIGHT_PERIMETER
        + speeds["infill"] * _SPEED_WEIGHT_INFILL
        + speeds["first_layer"] * _SPEED_WEIGHT_FIRST_LAYER
        + speeds["travel"] * _SPEED_WEIGHT_OVERHEAD
    )

    # Linear distance from volume and extrusion cross-section
    extrusion_width = nozzle_mm * 1.1  # slight over-extrusion typical
    extrusion_cross_section = extrusion_width * effective_layer_height
    linear_distance_mm = total_plastic_mm3 / extrusion_cross_section if extrusion_cross_section > 0 else 0.0

    # Time = distance / speed, with overhead multiplier
    extrusion_time_s = linear_distance_mm / effective_speed if effective_speed > 0 else 0.0
    print_time_s = extrusion_time_s * _TRAVEL_OVERHEAD_MULTIPLIER

    # --- Tool change overhead ---
    tool_changes = 0
    tool_change_time_s = 0
    tool_change_type = "none"

    if num_materials > 1:
        # Estimate tool changes: happens every time the extruder switches.
        # For multi-color, it's roughly proportional to the number of layers
        # that have multiple materials.  Estimate: number of layers × fraction
        # of layers with tool changes × (num_materials - 1) changes per layer.
        total_layers = int(height_mm / effective_layer_height) if effective_layer_height > 0 else 1

        # Heuristic: for surface-only decoration (accent on top face),
        # tool changes happen only on the top ~5 layers.
        # For full-body multi-color, tool changes happen on ~60% of layers.
        min_frac = min(material_fractions)
        if min_frac < 0.10:
            # Surface detail: only top/bottom layers get tool swaps
            multicolor_layers = max(1, int(total_layers * 0.05))
        elif min_frac < 0.30:
            # Accent color: ~30% of layers
            multicolor_layers = max(1, int(total_layers * 0.30))
        else:
            # Roughly even split: ~60% of layers
            multicolor_layers = max(1, int(total_layers * 0.60))

        tool_changes = multicolor_layers * (num_materials - 1)

        # Look up per-printer tool change data from slicer_profiles.json
        tc_info = _get_printer_tool_change(printer_id)
        tc_seconds = tc_info["tool_change_seconds"]
        tool_change_type = tc_info["tool_changer"]

        if tc_info["has_auto_tool_change"]:
            tool_change_time_s = tool_changes * tc_seconds
        elif printer_id:
            # Printer exists but has no auto tool changer — manual M600
            tool_change_type = "manual"
            tool_change_time_s = tool_changes * _MANUAL_TOOL_CHANGE_SECONDS
            warnings.append(
                f"Printer '{printer_id}' has no automatic tool changer. "
                f"Estimated {tool_changes} manual filament swaps at ~{_MANUAL_TOOL_CHANGE_SECONDS}s each."
            )
        else:
            # No printer specified — assume generic auto changer
            tool_change_type = "auto"
            tool_change_time_s = tool_changes * _DEFAULT_TOOL_CHANGE_SECONDS

    total_time_s = int(print_time_s + tool_change_time_s)

    # --- Electricity cost ---
    electricity_cost = 0.0
    if total_time_s > 0:
        hours = total_time_s / 3600.0
        kwh = (printer_wattage / 1000.0) * hours
        electricity_cost = kwh * electricity_rate

    # --- Total cost ---
    total_cost = total_filament_cost + electricity_cost

    # --- Confidence assessment ---
    if num_materials > 2:
        confidence = "medium" if confidence == "high" else confidence
        confidence_notes.append(
            "3+ materials increases tool change estimate uncertainty."
        )

    return PreEstimate(
        width_mm=width_mm,
        depth_mm=depth_mm,
        height_mm=height_mm,
        volume_mm3=round(estimated_part_volume_mm3, 1),
        estimated_time_seconds=total_time_s,
        estimated_time_human=_format_time(total_time_s),
        tool_changes=tool_changes,
        tool_change_time_seconds=tool_change_time_s,
        tool_change_type=tool_change_type,
        filaments=filaments,
        total_weight_grams=round(total_weight_g, 1),
        total_filament_meters=round(total_length_m, 2),
        filament_cost_usd=round(total_filament_cost, 2),
        electricity_cost_usd=round(electricity_cost, 2),
        total_cost_usd=round(total_cost, 2),
        printer_id=printer_id,
        infill_percent=effective_infill,
        layer_height_mm=effective_layer_height,
        nozzle_mm=nozzle_mm,
        confidence=confidence,
        confidence_notes=confidence_notes,
        warnings=warnings,
    )


def estimate_from_template(
    template_id: str,
    *,
    param_overrides: dict[str, Any] | None = None,
    materials: list[str] | None = None,
    material_fractions: list[float] | None = None,
    material_roles: list[str] | None = None,
    infill_percent: float | None = None,
    layer_height_mm: float | None = None,
    nozzle_mm: float = 0.4,
    wall_layers: int = 3,
    printer_id: str | None = None,
    electricity_rate: float = 0.12,
    printer_wattage: float = 200.0,
) -> PreEstimate:
    """Estimate from a named design template.

    Resolves template dimensions, then delegates to
    :func:`estimate_from_dimensions`.

    Args:
        template_id: Template ID from ``design_templates.json``
            (e.g. ``"phone_stand"``, ``"box_with_lid"``).
        param_overrides: Override template parameter defaults
            (e.g. ``{"phone_width": 85}``).
        materials: List of filament materials.
        material_fractions: Volume fractions per material.
        material_roles: Role labels per material.
        infill_percent: Infill percentage override.
        layer_height_mm: Layer height override.
        nozzle_mm: Nozzle diameter.
        wall_layers: Number of perimeter shells.
        printer_id: Printer model ID.
        electricity_rate: USD per kWh.
        printer_wattage: Printer watts.

    Returns:
        :class:`PreEstimate` with full breakdown.
    """
    width, depth, height = _resolve_template_dimensions(template_id, param_overrides)

    return estimate_from_dimensions(
        width,
        depth,
        height,
        materials=materials,
        material_fractions=material_fractions,
        material_roles=material_roles,
        infill_percent=infill_percent,
        layer_height_mm=layer_height_mm,
        nozzle_mm=nozzle_mm,
        wall_layers=wall_layers,
        printer_id=printer_id,
        electricity_rate=electricity_rate,
        printer_wattage=printer_wattage,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_time(seconds: int) -> str:
    """Convert seconds to human-readable duration."""
    if seconds <= 0:
        return "unknown"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
