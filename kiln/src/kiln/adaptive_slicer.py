"""Adaptive slicing based on geometry and material intelligence.

Analyzes model geometry and material properties to generate per-region
slicing parameters instead of uniform layer heights across an entire
print.  Thin walls and fine details get thinner layers for quality;
bulk infill gets thicker layers for speed; overhangs get adjusted
cooling and speed; top surfaces get finer layers for appearance.

The material intelligence knowledge base informs layer height limits
(e.g. PETG bridges need thinner layers than PLA).

Usage::

    from kiln.adaptive_slicer import get_adaptive_slicer

    slicer = get_adaptive_slicer()
    profile = slicer.get_material_profile("PLA")
    regions = slicer.analyze_geometry(model_stats={"height_mm": 30, ...})
    plan = slicer.generate_plan(
        regions, profile, model_height_mm=30.0, model_name="benchy"
    )
    config = slicer.export_config(plan, slicer=SlicerTarget.PRUSASLICER)
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RegionType(str, Enum):
    """Types of geometric regions that affect slicing parameters."""

    FINE_DETAIL = "fine_detail"
    STANDARD = "standard"
    BULK = "bulk"
    OVERHANG = "overhang"
    BRIDGE = "bridge"
    TOP_SURFACE = "top_surface"
    BOTTOM_SURFACE = "bottom_surface"
    THIN_WALL = "thin_wall"
    CURVED_SURFACE = "curved_surface"
    SUPPORT_CONTACT = "support_contact"


class AdaptiveMode(str, Enum):
    """Strategy for adaptive layer height computation."""

    QUALITY_FIRST = "quality_first"
    SPEED_FIRST = "speed_first"
    BALANCED = "balanced"
    MATERIAL_OPTIMIZED = "material_optimized"


class SlicerTarget(str, Enum):
    """Target slicer for config export."""

    PRUSASLICER = "prusaslicer"
    ORCASLICER = "orcaslicer"
    CURA = "cura"
    GENERIC = "generic"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AdaptiveSlicerError(Exception):
    """Raised when adaptive slicing operations fail."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MaterialSlicingProfile:
    """Material-specific slicing constraints."""

    material: str
    min_layer_height_mm: float
    max_layer_height_mm: float
    optimal_layer_height_mm: float
    bridge_layer_height_mm: float
    overhang_max_angle: float
    overhang_speed_factor: float
    overhang_fan_pct: float
    bridge_speed_mm_s: float
    bridge_fan_pct: float
    first_layer_height_mm: float
    first_layer_speed_factor: float
    top_surface_layers: int
    top_surface_speed_factor: float
    cooling_threshold_layer_time_s: float
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "material": self.material,
            "min_layer_height_mm": self.min_layer_height_mm,
            "max_layer_height_mm": self.max_layer_height_mm,
            "optimal_layer_height_mm": self.optimal_layer_height_mm,
            "bridge_layer_height_mm": self.bridge_layer_height_mm,
            "overhang_max_angle": self.overhang_max_angle,
            "overhang_speed_factor": self.overhang_speed_factor,
            "overhang_fan_pct": self.overhang_fan_pct,
            "bridge_speed_mm_s": self.bridge_speed_mm_s,
            "bridge_fan_pct": self.bridge_fan_pct,
            "first_layer_height_mm": self.first_layer_height_mm,
            "first_layer_speed_factor": self.first_layer_speed_factor,
            "top_surface_layers": self.top_surface_layers,
            "top_surface_speed_factor": self.top_surface_speed_factor,
            "cooling_threshold_layer_time_s": self.cooling_threshold_layer_time_s,
            "notes": self.notes,
        }


@dataclass
class GeometryRegion:
    """A detected region in the model geometry."""

    region_type: RegionType
    z_start_mm: float
    z_end_mm: float
    area_pct: float
    min_feature_size_mm: float | None = None
    overhang_angle: float | None = None
    bridge_length_mm: float | None = None
    wall_thickness_mm: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "region_type": self.region_type.value,
            "z_start_mm": self.z_start_mm,
            "z_end_mm": self.z_end_mm,
            "area_pct": self.area_pct,
        }
        if self.min_feature_size_mm is not None:
            d["min_feature_size_mm"] = self.min_feature_size_mm
        if self.overhang_angle is not None:
            d["overhang_angle"] = self.overhang_angle
        if self.bridge_length_mm is not None:
            d["bridge_length_mm"] = self.bridge_length_mm
        if self.wall_thickness_mm is not None:
            d["wall_thickness_mm"] = self.wall_thickness_mm
        return d


@dataclass
class AdaptiveLayerPlan:
    """Layer-by-layer slicing plan with adaptive heights."""

    plan_id: str
    model_name: str
    material: str
    printer: str | None = None
    mode: AdaptiveMode = AdaptiveMode.BALANCED
    nozzle_diameter_mm: float = 0.4
    total_layers: int = 0
    total_height_mm: float = 0.0
    min_layer_height_mm: float = 0.08
    max_layer_height_mm: float = 0.32
    layer_heights: list[float] = field(default_factory=list)
    layer_regions: list[list[RegionType]] = field(default_factory=list)
    layer_speeds: list[float] = field(default_factory=list)
    layer_cooling: list[float] = field(default_factory=list)
    estimated_time_minutes: float | None = None
    estimated_savings_pct: float | None = None
    regions_detected: list[GeometryRegion] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "model_name": self.model_name,
            "material": self.material,
            "printer": self.printer,
            "mode": self.mode.value,
            "nozzle_diameter_mm": self.nozzle_diameter_mm,
            "total_layers": self.total_layers,
            "total_height_mm": self.total_height_mm,
            "min_layer_height_mm": self.min_layer_height_mm,
            "max_layer_height_mm": self.max_layer_height_mm,
            "layer_heights": list(self.layer_heights),
            "layer_regions": [[r.value for r in regions] for regions in self.layer_regions],
            "layer_speeds": list(self.layer_speeds),
            "layer_cooling": list(self.layer_cooling),
            "estimated_time_minutes": self.estimated_time_minutes,
            "estimated_savings_pct": self.estimated_savings_pct,
            "regions_detected": [r.to_dict() for r in self.regions_detected],
            "created_at": self.created_at,
        }


@dataclass
class SlicerConfig:
    """Exported slicer configuration with adaptive parameters."""

    slicer: SlicerTarget
    config_format: str
    config_data: dict[str, Any] = field(default_factory=dict)
    variable_layer_height_data: list[tuple[float, float]] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "slicer": self.slicer.value,
            "config_format": self.config_format,
            "config_data": dict(self.config_data),
            "notes": list(self.notes),
        }
        if self.variable_layer_height_data is not None:
            d["variable_layer_height_data"] = [list(pair) for pair in self.variable_layer_height_data]
        return d


# ---------------------------------------------------------------------------
# Built-in material profiles
# ---------------------------------------------------------------------------

_MATERIAL_PROFILES: dict[str, dict[str, Any]] = {
    "PLA": {
        "min_layer_height_mm": 0.08,
        "max_layer_height_mm": 0.32,
        "optimal_layer_height_mm": 0.20,
        "bridge_layer_height_mm": 0.16,
        "overhang_max_angle": 55.0,
        "overhang_speed_factor": 0.6,
        "overhang_fan_pct": 100.0,
        "bridge_speed_mm_s": 25.0,
        "bridge_fan_pct": 100.0,
        "first_layer_height_mm": 0.25,
        "first_layer_speed_factor": 0.5,
        "top_surface_layers": 4,
        "top_surface_speed_factor": 0.7,
        "cooling_threshold_layer_time_s": 8.0,
        "notes": "Excellent bridging and overhang performance. High fan for overhangs.",
    },
    "PETG": {
        "min_layer_height_mm": 0.10,
        "max_layer_height_mm": 0.30,
        "optimal_layer_height_mm": 0.20,
        "bridge_layer_height_mm": 0.12,
        "overhang_max_angle": 45.0,
        "overhang_speed_factor": 0.5,
        "overhang_fan_pct": 50.0,
        "bridge_speed_mm_s": 20.0,
        "bridge_fan_pct": 50.0,
        "first_layer_height_mm": 0.25,
        "first_layer_speed_factor": 0.4,
        "top_surface_layers": 5,
        "top_surface_speed_factor": 0.6,
        "cooling_threshold_layer_time_s": 10.0,
        "notes": "Bridges need thinner layers than PLA. Moderate fan to avoid delamination.",
    },
    "ABS": {
        "min_layer_height_mm": 0.10,
        "max_layer_height_mm": 0.30,
        "optimal_layer_height_mm": 0.20,
        "bridge_layer_height_mm": 0.15,
        "overhang_max_angle": 45.0,
        "overhang_speed_factor": 0.5,
        "overhang_fan_pct": 0.0,
        "bridge_speed_mm_s": 25.0,
        "bridge_fan_pct": 0.0,
        "first_layer_height_mm": 0.25,
        "first_layer_speed_factor": 0.4,
        "top_surface_layers": 5,
        "top_surface_speed_factor": 0.6,
        "cooling_threshold_layer_time_s": 12.0,
        "notes": "No fan for overhangs/bridges — warping risk. Enclosed printer recommended.",
    },
    "TPU": {
        "min_layer_height_mm": 0.12,
        "max_layer_height_mm": 0.28,
        "optimal_layer_height_mm": 0.20,
        "bridge_layer_height_mm": 0.15,
        "overhang_max_angle": 40.0,
        "overhang_speed_factor": 0.4,
        "overhang_fan_pct": 50.0,
        "bridge_speed_mm_s": 15.0,
        "bridge_fan_pct": 50.0,
        "first_layer_height_mm": 0.25,
        "first_layer_speed_factor": 0.3,
        "top_surface_layers": 4,
        "top_surface_speed_factor": 0.5,
        "cooling_threshold_layer_time_s": 15.0,
        "notes": "Flexible — slow speeds critical for overhangs and bridges. Direct drive preferred.",
    },
    "ASA": {
        "min_layer_height_mm": 0.10,
        "max_layer_height_mm": 0.30,
        "optimal_layer_height_mm": 0.20,
        "bridge_layer_height_mm": 0.15,
        "overhang_max_angle": 45.0,
        "overhang_speed_factor": 0.5,
        "overhang_fan_pct": 30.0,
        "bridge_speed_mm_s": 25.0,
        "bridge_fan_pct": 30.0,
        "first_layer_height_mm": 0.25,
        "first_layer_speed_factor": 0.4,
        "top_surface_layers": 5,
        "top_surface_speed_factor": 0.6,
        "cooling_threshold_layer_time_s": 12.0,
        "notes": "UV-resistant ABS alternative. Minimal fan to reduce warping.",
    },
    "NYLON": {
        "min_layer_height_mm": 0.10,
        "max_layer_height_mm": 0.28,
        "optimal_layer_height_mm": 0.20,
        "bridge_layer_height_mm": 0.12,
        "overhang_max_angle": 40.0,
        "overhang_speed_factor": 0.5,
        "overhang_fan_pct": 30.0,
        "bridge_speed_mm_s": 20.0,
        "bridge_fan_pct": 30.0,
        "first_layer_height_mm": 0.25,
        "first_layer_speed_factor": 0.4,
        "top_surface_layers": 5,
        "top_surface_speed_factor": 0.6,
        "cooling_threshold_layer_time_s": 12.0,
        "notes": "Hygroscopic — must be dry. Thin bridge layers for good adhesion.",
    },
    "PC": {
        "min_layer_height_mm": 0.12,
        "max_layer_height_mm": 0.28,
        "optimal_layer_height_mm": 0.20,
        "bridge_layer_height_mm": 0.15,
        "overhang_max_angle": 40.0,
        "overhang_speed_factor": 0.5,
        "overhang_fan_pct": 0.0,
        "bridge_speed_mm_s": 20.0,
        "bridge_fan_pct": 0.0,
        "first_layer_height_mm": 0.25,
        "first_layer_speed_factor": 0.4,
        "top_surface_layers": 5,
        "top_surface_speed_factor": 0.6,
        "cooling_threshold_layer_time_s": 15.0,
        "notes": "High-temp material. No fan — enclosed printer mandatory. Warp-prone.",
    },
    "PVA": {
        "min_layer_height_mm": 0.10,
        "max_layer_height_mm": 0.25,
        "optimal_layer_height_mm": 0.15,
        "bridge_layer_height_mm": 0.12,
        "overhang_max_angle": 50.0,
        "overhang_speed_factor": 0.5,
        "overhang_fan_pct": 100.0,
        "bridge_speed_mm_s": 20.0,
        "bridge_fan_pct": 100.0,
        "first_layer_height_mm": 0.20,
        "first_layer_speed_factor": 0.4,
        "top_surface_layers": 4,
        "top_surface_speed_factor": 0.6,
        "cooling_threshold_layer_time_s": 10.0,
        "notes": "Water-soluble support material. Must be kept very dry.",
    },
    "HIPS": {
        "min_layer_height_mm": 0.10,
        "max_layer_height_mm": 0.30,
        "optimal_layer_height_mm": 0.20,
        "bridge_layer_height_mm": 0.15,
        "overhang_max_angle": 50.0,
        "overhang_speed_factor": 0.5,
        "overhang_fan_pct": 50.0,
        "bridge_speed_mm_s": 25.0,
        "bridge_fan_pct": 50.0,
        "first_layer_height_mm": 0.25,
        "first_layer_speed_factor": 0.4,
        "top_surface_layers": 4,
        "top_surface_speed_factor": 0.6,
        "cooling_threshold_layer_time_s": 10.0,
        "notes": "Limonene-soluble support. Similar to ABS but easier to print.",
    },
}

# ---------------------------------------------------------------------------
# Nozzle-based layer height constraints
# ---------------------------------------------------------------------------

_MIN_LAYER_NOZZLE_RATIO = 0.25
_MAX_LAYER_NOZZLE_RATIO = 0.75

# Default base speed used for time estimation (mm/s).
_DEFAULT_PRINT_SPEED_MM_S = 60.0

# Average travel distance per layer for time estimation (mm).
_DEFAULT_LAYER_TRAVEL_MM = 200.0


# ---------------------------------------------------------------------------
# AdaptiveSlicer
# ---------------------------------------------------------------------------


class AdaptiveSlicer:
    """Generates per-region adaptive slicing parameters.

    Analyzes model geometry and material properties to produce a
    layer-by-layer plan with variable heights, speeds, and cooling.
    Thread-safe via an internal lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # -- public API ---------------------------------------------------------

    def analyze_geometry(
        self,
        *,
        model_path: str | None = None,
        model_stats: dict[str, Any] | None = None,
    ) -> list[GeometryRegion]:
        """Analyze model to detect geometric region types.

        Can work from a file path (basic STL bounding-box analysis) or
        from a pre-computed stats dict (from slicer preview or external
        analysis tool).

        :param model_path: Path to an STL/3MF file for basic analysis.
        :param model_stats: Pre-computed geometry statistics dict.
        :returns: List of detected :class:`GeometryRegion` instances.
        :raises AdaptiveSlicerError: If neither source is provided.
        """
        if model_stats is None and model_path is None:
            raise AdaptiveSlicerError("Either model_path or model_stats must be provided.")

        stats = model_stats or {}

        if model_path is not None and not model_stats:
            stats = _parse_model_file(model_path)

        regions: list[GeometryRegion] = []
        height_mm = float(stats.get("height_mm", 0.0))
        if height_mm <= 0:
            return regions

        regions.extend(self._detect_overhangs(stats))
        regions.extend(self._detect_bridges(stats))
        regions.extend(self._detect_thin_walls(stats))
        regions.extend(self._detect_top_surfaces(stats, height_mm))
        regions.extend(_detect_bottom_surfaces(stats, height_mm))
        regions.extend(_detect_fine_details(stats, height_mm))
        regions.extend(_detect_curved_surfaces(stats, height_mm))

        # If no special regions detected, mark everything as STANDARD.
        if not regions:
            regions.append(
                GeometryRegion(
                    region_type=RegionType.STANDARD,
                    z_start_mm=0.0,
                    z_end_mm=height_mm,
                    area_pct=100.0,
                )
            )

        return regions

    def get_material_profile(
        self,
        material: str,
        *,
        nozzle_diameter_mm: float = 0.4,
    ) -> MaterialSlicingProfile:
        """Load material-specific slicing constraints.

        Built-in profiles exist for PLA, PETG, ABS, TPU, ASA, Nylon,
        PC, PVA, and HIPS.  Nozzle diameter affects min/max layer
        heights (typically 25%-75% of nozzle).

        :param material: Material name (case-insensitive).
        :param nozzle_diameter_mm: Nozzle diameter for layer height limits.
        :returns: A :class:`MaterialSlicingProfile`.
        :raises AdaptiveSlicerError: If the material is unknown.
        """
        key = material.upper().strip()
        raw = _MATERIAL_PROFILES.get(key)
        if raw is None:
            available = ", ".join(sorted(_MATERIAL_PROFILES.keys()))
            raise AdaptiveSlicerError(f"Unknown material '{material}'. Available: {available}.")

        nozzle_min = round(nozzle_diameter_mm * _MIN_LAYER_NOZZLE_RATIO, 3)
        nozzle_max = round(nozzle_diameter_mm * _MAX_LAYER_NOZZLE_RATIO, 3)

        return MaterialSlicingProfile(
            material=key,
            min_layer_height_mm=max(raw["min_layer_height_mm"], nozzle_min),
            max_layer_height_mm=min(raw["max_layer_height_mm"], nozzle_max),
            optimal_layer_height_mm=_clamp(
                raw["optimal_layer_height_mm"],
                max(raw["min_layer_height_mm"], nozzle_min),
                min(raw["max_layer_height_mm"], nozzle_max),
            ),
            bridge_layer_height_mm=_clamp(
                raw["bridge_layer_height_mm"],
                max(raw["min_layer_height_mm"], nozzle_min),
                min(raw["max_layer_height_mm"], nozzle_max),
            ),
            overhang_max_angle=raw["overhang_max_angle"],
            overhang_speed_factor=raw["overhang_speed_factor"],
            overhang_fan_pct=raw["overhang_fan_pct"],
            bridge_speed_mm_s=raw["bridge_speed_mm_s"],
            bridge_fan_pct=raw["bridge_fan_pct"],
            first_layer_height_mm=_clamp(
                raw["first_layer_height_mm"],
                max(raw["min_layer_height_mm"], nozzle_min),
                min(raw["max_layer_height_mm"], nozzle_max),
            ),
            first_layer_speed_factor=raw["first_layer_speed_factor"],
            top_surface_layers=raw["top_surface_layers"],
            top_surface_speed_factor=raw["top_surface_speed_factor"],
            cooling_threshold_layer_time_s=raw["cooling_threshold_layer_time_s"],
            notes=raw.get("notes", ""),
        )

    def generate_plan(
        self,
        regions: list[GeometryRegion],
        material_profile: MaterialSlicingProfile,
        *,
        mode: AdaptiveMode = AdaptiveMode.BALANCED,
        model_height_mm: float,
        model_name: str = "",
        printer: str | None = None,
        nozzle_diameter_mm: float = 0.4,
    ) -> AdaptiveLayerPlan:
        """Generate a per-layer adaptive height plan.

        This is the core method.  It walks from Z=0 to
        ``model_height_mm``, choosing per-layer heights based on the
        detected regions, material constraints, and chosen mode.

        :param regions: Geometry regions from :meth:`analyze_geometry`.
        :param material_profile: Material constraints from
            :meth:`get_material_profile`.
        :param mode: Adaptive strategy.
        :param model_height_mm: Total model height in mm.
        :param model_name: Optional model name for the plan record.
        :param printer: Optional printer name.
        :param nozzle_diameter_mm: Nozzle diameter in mm.
        :returns: An :class:`AdaptiveLayerPlan`.
        :raises AdaptiveSlicerError: If model_height_mm <= 0.
        """
        if model_height_mm <= 0:
            raise AdaptiveSlicerError(f"Model height must be positive, got {model_height_mm}.")

        with self._lock:
            layer_heights: list[float] = []
            layer_regions_list: list[list[RegionType]] = []
            layer_speeds: list[float] = []
            layer_cooling: list[float] = []

            z = 0.0
            layer_idx = 0

            while z < model_height_mm:
                height = self._compute_layer_height(z, regions, material_profile, mode)
                height = self._clamp_layer_height(height, material_profile, nozzle_diameter_mm)

                # First layer override.
                if layer_idx == 0:
                    height = material_profile.first_layer_height_mm

                # Don't overshoot the model.
                remaining = model_height_mm - z
                if remaining < height:
                    # If remainder is too thin, merge with previous layer.
                    if remaining < material_profile.min_layer_height_mm and layer_heights:
                        layer_heights[-1] += remaining
                        break
                    height = max(remaining, material_profile.min_layer_height_mm)

                speed = self._compute_layer_speed(z, regions, material_profile)
                if layer_idx == 0:
                    speed = material_profile.first_layer_speed_factor

                cooling = self._compute_layer_cooling(z, regions, material_profile)

                active_regions = _regions_at_z(z, regions)

                layer_heights.append(round(height, 4))
                layer_regions_list.append(active_regions)
                layer_speeds.append(round(speed, 3))
                layer_cooling.append(round(cooling, 1))

                z += height
                layer_idx += 1

            plan = AdaptiveLayerPlan(
                plan_id=str(uuid.uuid4())[:12],
                model_name=model_name,
                material=material_profile.material,
                printer=printer,
                mode=mode,
                nozzle_diameter_mm=nozzle_diameter_mm,
                total_layers=len(layer_heights),
                total_height_mm=round(sum(layer_heights), 4),
                min_layer_height_mm=material_profile.min_layer_height_mm,
                max_layer_height_mm=material_profile.max_layer_height_mm,
                layer_heights=layer_heights,
                layer_regions=layer_regions_list,
                layer_speeds=layer_speeds,
                layer_cooling=layer_cooling,
                regions_detected=list(regions),
                created_at=datetime.now(tz=timezone.utc).isoformat(),
            )

            # Estimate time.
            plan.estimated_time_minutes = _estimate_time(plan)

            return plan

    def export_config(
        self,
        plan: AdaptiveLayerPlan,
        *,
        slicer: SlicerTarget = SlicerTarget.PRUSASLICER,
    ) -> SlicerConfig:
        """Export an adaptive plan as slicer-compatible configuration.

        PrusaSlicer/OrcaSlicer use the variable layer height format.
        Cura uses the adaptive layers plugin format.

        :param plan: The adaptive layer plan to export.
        :param slicer: Target slicer software.
        :returns: A :class:`SlicerConfig`.
        """
        exporters = {
            SlicerTarget.PRUSASLICER: self._export_prusaslicer,
            SlicerTarget.ORCASLICER: self._export_orcaslicer,
            SlicerTarget.CURA: self._export_cura,
            SlicerTarget.GENERIC: self._export_generic,
        }
        return exporters[slicer](plan)

    def estimate_time_savings(
        self,
        plan: AdaptiveLayerPlan,
        *,
        uniform_height_mm: float = 0.2,
    ) -> dict[str, Any]:
        """Compare an adaptive plan against uniform layer height.

        :param plan: The adaptive plan.
        :param uniform_height_mm: Reference uniform layer height.
        :returns: Dict with comparison metrics.
        """
        if uniform_height_mm <= 0:
            raise AdaptiveSlicerError(f"Uniform height must be positive, got {uniform_height_mm}.")

        import math

        uniform_layers = math.ceil(plan.total_height_mm / uniform_height_mm)
        adaptive_layers = plan.total_layers

        uniform_time = _estimate_time_uniform(plan.total_height_mm, uniform_height_mm)
        adaptive_time = plan.estimated_time_minutes or _estimate_time(plan)

        savings_pct = 0.0
        if uniform_time > 0:
            savings_pct = round((1.0 - adaptive_time / uniform_time) * 100.0, 1)

        plan.estimated_savings_pct = savings_pct

        return {
            "uniform_layers": uniform_layers,
            "adaptive_layers": adaptive_layers,
            "layer_reduction": uniform_layers - adaptive_layers,
            "uniform_time_minutes": round(uniform_time, 1),
            "adaptive_time_minutes": round(adaptive_time, 1),
            "time_saved_minutes": round(uniform_time - adaptive_time, 1),
            "savings_pct": savings_pct,
            "uniform_height_mm": uniform_height_mm,
            "avg_adaptive_height_mm": round(plan.total_height_mm / max(adaptive_layers, 1), 4),
        }

    def quick_plan(
        self,
        *,
        material: str,
        model_height_mm: float,
        model_name: str = "",
        nozzle_diameter_mm: float = 0.4,
        mode: AdaptiveMode = AdaptiveMode.BALANCED,
        printer: str | None = None,
        regions: list[dict[str, Any]] | None = None,
    ) -> AdaptiveLayerPlan:
        """Convenience method combining analyze + plan in one call.

        :param material: Material name.
        :param model_height_mm: Total model height.
        :param model_name: Optional model name.
        :param nozzle_diameter_mm: Nozzle diameter.
        :param mode: Adaptive strategy.
        :param printer: Optional printer identifier.
        :param regions: Optional pre-defined region dicts.  If omitted,
            a default STANDARD region is used.
        :returns: An :class:`AdaptiveLayerPlan`.
        """
        mat_profile = self.get_material_profile(material, nozzle_diameter_mm=nozzle_diameter_mm)

        if regions:
            geo_regions = _parse_region_dicts(regions, model_height_mm)
        else:
            geo_regions = [
                GeometryRegion(
                    region_type=RegionType.STANDARD,
                    z_start_mm=0.0,
                    z_end_mm=model_height_mm,
                    area_pct=100.0,
                )
            ]

        return self.generate_plan(
            geo_regions,
            mat_profile,
            mode=mode,
            model_height_mm=model_height_mm,
            model_name=model_name,
            printer=printer,
            nozzle_diameter_mm=nozzle_diameter_mm,
        )

    def list_supported_materials(self) -> list[str]:
        """Return sorted list of all supported material names."""
        return sorted(_MATERIAL_PROFILES.keys())

    # -- private: layer height computation ----------------------------------

    def _compute_layer_height(
        self,
        z_mm: float,
        regions: list[GeometryRegion],
        material: MaterialSlicingProfile,
        mode: AdaptiveMode,
    ) -> float:
        """Determine layer height for a specific Z position."""
        active_types = {r.region_type for r in _regions_at_z_raw(z_mm, regions)}

        if not active_types:
            active_types = {RegionType.STANDARD}

        if mode == AdaptiveMode.QUALITY_FIRST:
            return self._height_quality_first(active_types, material, regions, z_mm)
        if mode == AdaptiveMode.SPEED_FIRST:
            return self._height_speed_first(active_types, material)
        if mode == AdaptiveMode.MATERIAL_OPTIMIZED:
            return self._height_material_optimized(active_types, material)
        # BALANCED (default)
        return self._height_balanced(active_types, material)

    def _height_quality_first(
        self,
        types: set[RegionType],
        mat: MaterialSlicingProfile,
        regions: list[GeometryRegion],
        z_mm: float,
    ) -> float:
        """QUALITY_FIRST: minimise layer lines everywhere."""
        # Fine detail and thin wall always get minimum.
        if types & {RegionType.FINE_DETAIL, RegionType.THIN_WALL}:
            # Use feature size if available.
            for r in _regions_at_z_raw(z_mm, regions):
                if (
                    r.region_type in {RegionType.FINE_DETAIL, RegionType.THIN_WALL}
                    and r.min_feature_size_mm is not None
                ):
                    return min(mat.optimal_layer_height_mm, r.min_feature_size_mm / 3.0)
            return mat.min_layer_height_mm

        if RegionType.TOP_SURFACE in types:
            return mat.min_layer_height_mm

        if types & {RegionType.OVERHANG, RegionType.BRIDGE}:
            return mat.bridge_layer_height_mm

        if RegionType.CURVED_SURFACE in types:
            return min(mat.optimal_layer_height_mm, mat.min_layer_height_mm * 1.5)

        # Everything else gets optimal or below.
        return min(mat.optimal_layer_height_mm, mat.min_layer_height_mm * 2.0)

    def _height_speed_first(
        self,
        types: set[RegionType],
        mat: MaterialSlicingProfile,
    ) -> float:
        """SPEED_FIRST: thick layers by default, thin only where needed."""
        if types & {RegionType.FINE_DETAIL, RegionType.THIN_WALL}:
            return mat.min_layer_height_mm

        if RegionType.TOP_SURFACE in types:
            return mat.optimal_layer_height_mm

        if types & {RegionType.BRIDGE}:
            return mat.bridge_layer_height_mm

        if RegionType.OVERHANG in types:
            return mat.optimal_layer_height_mm

        # STANDARD, BULK, and everything else: max height.
        return mat.max_layer_height_mm

    def _height_balanced(
        self,
        types: set[RegionType],
        mat: MaterialSlicingProfile,
    ) -> float:
        """BALANCED: sensible defaults with region-specific adjustments."""
        if types & {RegionType.FINE_DETAIL, RegionType.THIN_WALL}:
            return mat.optimal_layer_height_mm * 0.6

        if RegionType.TOP_SURFACE in types:
            return mat.min_layer_height_mm

        if types & {RegionType.OVERHANG, RegionType.BRIDGE}:
            return mat.bridge_layer_height_mm

        if RegionType.CURVED_SURFACE in types:
            return mat.optimal_layer_height_mm * 0.8

        if RegionType.BULK in types:
            return min(mat.optimal_layer_height_mm * 1.5, mat.max_layer_height_mm)

        if RegionType.BOTTOM_SURFACE in types:
            return mat.first_layer_height_mm

        # STANDARD and fallback.
        return mat.optimal_layer_height_mm

    def _height_material_optimized(
        self,
        types: set[RegionType],
        mat: MaterialSlicingProfile,
    ) -> float:
        """MATERIAL_OPTIMIZED: conservative bridge/overhang values."""
        if types & {RegionType.BRIDGE}:
            # Most conservative: use thinner of bridge height and min.
            return max(mat.bridge_layer_height_mm * 0.8, mat.min_layer_height_mm)

        if RegionType.OVERHANG in types:
            return max(mat.bridge_layer_height_mm * 0.9, mat.min_layer_height_mm)

        if types & {RegionType.FINE_DETAIL, RegionType.THIN_WALL}:
            return mat.min_layer_height_mm

        if RegionType.TOP_SURFACE in types:
            return mat.min_layer_height_mm

        if RegionType.CURVED_SURFACE in types:
            return mat.optimal_layer_height_mm * 0.7

        if RegionType.BULK in types:
            return mat.optimal_layer_height_mm

        return mat.optimal_layer_height_mm

    def _compute_layer_speed(
        self,
        z_mm: float,
        regions: list[GeometryRegion],
        material: MaterialSlicingProfile,
    ) -> float:
        """Speed factor for a layer (1.0 = nominal speed)."""
        active = _regions_at_z_raw(z_mm, regions)
        active_types = {r.region_type for r in active}

        if not active_types:
            return 1.0

        # Pick the most restrictive speed factor.
        factor = 1.0

        if active_types & {RegionType.BRIDGE}:
            factor = min(
                factor,
                material.bridge_speed_mm_s / _DEFAULT_PRINT_SPEED_MM_S,
            )
        if active_types & {RegionType.OVERHANG}:
            factor = min(factor, material.overhang_speed_factor)
        if active_types & {RegionType.TOP_SURFACE}:
            factor = min(factor, material.top_surface_speed_factor)
        if active_types & {RegionType.THIN_WALL, RegionType.FINE_DETAIL}:
            factor = min(factor, 0.6)
        if active_types & {RegionType.CURVED_SURFACE}:
            factor = min(factor, 0.8)

        return round(max(factor, 0.1), 3)

    def _compute_layer_cooling(
        self,
        z_mm: float,
        regions: list[GeometryRegion],
        material: MaterialSlicingProfile,
    ) -> float:
        """Fan speed percentage for a layer."""
        active_types = {r.region_type for r in _regions_at_z_raw(z_mm, regions)}

        if not active_types:
            return 100.0  # Default full fan for unknown regions.

        # Pick the highest fan speed needed.
        fan = 50.0  # Baseline.

        if RegionType.BRIDGE in active_types:
            fan = max(fan, material.bridge_fan_pct)
        if RegionType.OVERHANG in active_types:
            fan = max(fan, material.overhang_fan_pct)
        if RegionType.TOP_SURFACE in active_types:
            fan = max(fan, 80.0)
        if active_types & {RegionType.FINE_DETAIL, RegionType.THIN_WALL}:
            fan = max(fan, 80.0)
        if RegionType.BOTTOM_SURFACE in active_types:
            fan = 0.0  # First layers: no fan for adhesion.

        return round(min(fan, 100.0), 1)

    # -- private: geometry detection ----------------------------------------

    def _detect_overhangs(self, model_stats: dict[str, Any]) -> list[GeometryRegion]:
        """Find overhang regions from model stats."""
        overhangs_raw = model_stats.get("overhangs", [])
        regions: list[GeometryRegion] = []
        for oh in overhangs_raw:
            if not isinstance(oh, dict):
                continue
            regions.append(
                GeometryRegion(
                    region_type=RegionType.OVERHANG,
                    z_start_mm=float(oh.get("z_start_mm", 0.0)),
                    z_end_mm=float(oh.get("z_end_mm", 0.0)),
                    area_pct=float(oh.get("area_pct", 5.0)),
                    overhang_angle=float(oh.get("angle", 45.0)),
                )
            )
        return regions

    def _detect_bridges(self, model_stats: dict[str, Any]) -> list[GeometryRegion]:
        """Find bridge regions from model stats."""
        bridges_raw = model_stats.get("bridges", [])
        regions: list[GeometryRegion] = []
        for br in bridges_raw:
            if not isinstance(br, dict):
                continue
            regions.append(
                GeometryRegion(
                    region_type=RegionType.BRIDGE,
                    z_start_mm=float(br.get("z_start_mm", 0.0)),
                    z_end_mm=float(br.get("z_end_mm", 0.0)),
                    area_pct=float(br.get("area_pct", 3.0)),
                    bridge_length_mm=float(br.get("length_mm", 10.0)),
                )
            )
        return regions

    def _detect_thin_walls(self, model_stats: dict[str, Any]) -> list[GeometryRegion]:
        """Find thin wall regions from model stats."""
        thin_walls_raw = model_stats.get("thin_walls", [])
        regions: list[GeometryRegion] = []
        for tw in thin_walls_raw:
            if not isinstance(tw, dict):
                continue
            regions.append(
                GeometryRegion(
                    region_type=RegionType.THIN_WALL,
                    z_start_mm=float(tw.get("z_start_mm", 0.0)),
                    z_end_mm=float(tw.get("z_end_mm", 0.0)),
                    area_pct=float(tw.get("area_pct", 2.0)),
                    wall_thickness_mm=float(tw.get("thickness_mm", 0.4)),
                    min_feature_size_mm=float(tw.get("thickness_mm", 0.4)),
                )
            )
        return regions

    def _detect_top_surfaces(self, model_stats: dict[str, Any], model_height: float) -> list[GeometryRegion]:
        """Find top surface regions."""
        top_surfaces_raw = model_stats.get("top_surfaces", [])
        if top_surfaces_raw:
            regions: list[GeometryRegion] = []
            for ts in top_surfaces_raw:
                if not isinstance(ts, dict):
                    continue
                regions.append(
                    GeometryRegion(
                        region_type=RegionType.TOP_SURFACE,
                        z_start_mm=float(ts.get("z_start_mm", model_height - 1.0)),
                        z_end_mm=float(ts.get("z_end_mm", model_height)),
                        area_pct=float(ts.get("area_pct", 100.0)),
                    )
                )
            return regions

        # Default heuristic: top 1mm or 5% of height, whichever is larger.
        top_zone = max(1.0, model_height * 0.05)
        if model_height <= top_zone:
            return []
        return [
            GeometryRegion(
                region_type=RegionType.TOP_SURFACE,
                z_start_mm=model_height - top_zone,
                z_end_mm=model_height,
                area_pct=100.0,
            )
        ]

    @staticmethod
    def _clamp_layer_height(
        height: float,
        material: MaterialSlicingProfile,
        nozzle: float,
    ) -> float:
        """Enforce min/max layer height from material and nozzle limits."""
        nozzle_min = nozzle * _MIN_LAYER_NOZZLE_RATIO
        nozzle_max = nozzle * _MAX_LAYER_NOZZLE_RATIO
        lo = max(material.min_layer_height_mm, nozzle_min)
        hi = min(material.max_layer_height_mm, nozzle_max)
        # Ensure lo <= hi even if material profile is weird.
        if lo > hi:
            lo = hi
        return round(_clamp(height, lo, hi), 4)

    # -- private: slicer export ---------------------------------------------

    def _export_prusaslicer(self, plan: AdaptiveLayerPlan) -> SlicerConfig:
        """Export as PrusaSlicer variable layer height data."""
        vlh_data: list[tuple[float, float]] = []
        z = 0.0
        for h in plan.layer_heights:
            vlh_data.append((round(z, 4), round(h, 4)))
            z += h

        config: dict[str, Any] = {
            "layer_height": plan.layer_heights[0] if plan.layer_heights else 0.2,
            "variable_layer_height": 1,
            "variable_layer_height_profile": _vlh_to_prusaslicer_string(vlh_data),
        }

        return SlicerConfig(
            slicer=SlicerTarget.PRUSASLICER,
            config_format="ini",
            config_data=config,
            variable_layer_height_data=vlh_data,
            notes=[
                f"Adaptive plan with {plan.total_layers} layers",
                f"Material: {plan.material}, Mode: {plan.mode.value}",
                "Import via PrusaSlicer variable layer height editor",
            ],
        )

    def _export_orcaslicer(self, plan: AdaptiveLayerPlan) -> SlicerConfig:
        """Export as OrcaSlicer config (similar to PrusaSlicer)."""
        vlh_data: list[tuple[float, float]] = []
        z = 0.0
        for h in plan.layer_heights:
            vlh_data.append((round(z, 4), round(h, 4)))
            z += h

        config: dict[str, Any] = {
            "layer_height": plan.layer_heights[0] if plan.layer_heights else 0.2,
            "adaptive_layer_height": True,
            "variable_layer_height_profile": _vlh_to_prusaslicer_string(vlh_data),
        }

        return SlicerConfig(
            slicer=SlicerTarget.ORCASLICER,
            config_format="json",
            config_data=config,
            variable_layer_height_data=vlh_data,
            notes=[
                f"Adaptive plan with {plan.total_layers} layers",
                f"Material: {plan.material}, Mode: {plan.mode.value}",
                "Apply via OrcaSlicer adaptive layer height settings",
            ],
        )

    def _export_cura(self, plan: AdaptiveLayerPlan) -> SlicerConfig:
        """Export as Cura adaptive layers plugin format."""
        layers: list[dict[str, Any]] = []
        z = 0.0
        for i, h in enumerate(plan.layer_heights):
            layers.append(
                {
                    "layer": i,
                    "z_mm": round(z, 4),
                    "height_mm": round(h, 4),
                    "speed_factor": plan.layer_speeds[i] if i < len(plan.layer_speeds) else 1.0,
                    "fan_pct": plan.layer_cooling[i] if i < len(plan.layer_cooling) else 100.0,
                }
            )
            z += h

        config: dict[str, Any] = {
            "adaptive_layers_enabled": True,
            "layer_height": plan.layer_heights[0] if plan.layer_heights else 0.2,
            "adaptive_layers_variation": round(plan.max_layer_height_mm - plan.min_layer_height_mm, 3),
            "adaptive_layers_threshold": 0.5,
            "layers": layers,
        }

        return SlicerConfig(
            slicer=SlicerTarget.CURA,
            config_format="json",
            config_data=config,
            notes=[
                f"Adaptive plan with {plan.total_layers} layers",
                f"Material: {plan.material}, Mode: {plan.mode.value}",
                "Configure Cura adaptive layers plugin with these values",
            ],
        )

    def _export_generic(self, plan: AdaptiveLayerPlan) -> SlicerConfig:
        """Export as generic Z-height pairs."""
        pairs: list[tuple[float, float]] = []
        z = 0.0
        for h in plan.layer_heights:
            pairs.append((round(z, 4), round(h, 4)))
            z += h

        config: dict[str, Any] = {
            "total_layers": plan.total_layers,
            "total_height_mm": plan.total_height_mm,
            "layer_data": [{"z": p[0], "height": p[1]} for p in pairs],
        }

        return SlicerConfig(
            slicer=SlicerTarget.GENERIC,
            config_format="json",
            config_data=config,
            variable_layer_height_data=pairs,
            notes=[
                f"Generic adaptive plan: {plan.total_layers} layers",
                f"Material: {plan.material}, Mode: {plan.mode.value}",
            ],
        )


# ---------------------------------------------------------------------------
# Module-level helpers (private)
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* between *lo* and *hi*."""
    return max(lo, min(value, hi))


def _regions_at_z(z_mm: float, regions: list[GeometryRegion]) -> list[RegionType]:
    """Return region types active at a given Z height."""
    return [r.region_type for r in _regions_at_z_raw(z_mm, regions)]


def _regions_at_z_raw(z_mm: float, regions: list[GeometryRegion]) -> list[GeometryRegion]:
    """Return full region objects active at a given Z height."""
    active: list[GeometryRegion] = []
    for r in regions:
        if r.z_start_mm <= z_mm < r.z_end_mm:
            active.append(r)
    return active


def _detect_bottom_surfaces(model_stats: dict[str, Any], model_height: float) -> list[GeometryRegion]:
    """Detect bottom surface regions (first layers / bed contact)."""
    bottom_raw = model_stats.get("bottom_surfaces", [])
    if bottom_raw:
        regions: list[GeometryRegion] = []
        for bs in bottom_raw:
            if not isinstance(bs, dict):
                continue
            regions.append(
                GeometryRegion(
                    region_type=RegionType.BOTTOM_SURFACE,
                    z_start_mm=float(bs.get("z_start_mm", 0.0)),
                    z_end_mm=float(bs.get("z_end_mm", 0.5)),
                    area_pct=float(bs.get("area_pct", 100.0)),
                )
            )
        return regions

    # Default heuristic: first 0.5mm.
    bottom_zone = min(0.5, model_height)
    if bottom_zone <= 0:
        return []
    return [
        GeometryRegion(
            region_type=RegionType.BOTTOM_SURFACE,
            z_start_mm=0.0,
            z_end_mm=bottom_zone,
            area_pct=100.0,
        )
    ]


def _detect_fine_details(model_stats: dict[str, Any], model_height: float) -> list[GeometryRegion]:
    """Detect fine detail regions from model stats."""
    fine_raw = model_stats.get("fine_details", [])
    regions: list[GeometryRegion] = []
    for fd in fine_raw:
        if not isinstance(fd, dict):
            continue
        regions.append(
            GeometryRegion(
                region_type=RegionType.FINE_DETAIL,
                z_start_mm=float(fd.get("z_start_mm", 0.0)),
                z_end_mm=float(fd.get("z_end_mm", model_height)),
                area_pct=float(fd.get("area_pct", 5.0)),
                min_feature_size_mm=float(fd.get("feature_size_mm", 0.5)),
            )
        )
    return regions


def _detect_curved_surfaces(model_stats: dict[str, Any], model_height: float) -> list[GeometryRegion]:
    """Detect curved/organic surface regions."""
    curved_raw = model_stats.get("curved_surfaces", [])
    regions: list[GeometryRegion] = []
    for cs in curved_raw:
        if not isinstance(cs, dict):
            continue
        regions.append(
            GeometryRegion(
                region_type=RegionType.CURVED_SURFACE,
                z_start_mm=float(cs.get("z_start_mm", 0.0)),
                z_end_mm=float(cs.get("z_end_mm", model_height)),
                area_pct=float(cs.get("area_pct", 10.0)),
            )
        )
    return regions


def _parse_model_file(model_path: str) -> dict[str, Any]:
    """Basic STL/3MF analysis extracting bounding box and height.

    This is intentionally minimal — real geometry analysis requires a
    mesh processing library.  The returned stats dict has at least
    ``height_mm``.
    """
    import os

    if not os.path.isfile(model_path):
        raise AdaptiveSlicerError(f"Model file not found: {model_path}")

    # Attempt minimal binary STL bounding-box parse.
    stats: dict[str, Any] = {"source": model_path}
    try:
        stats.update(_parse_binary_stl_bbox(model_path))
    except Exception as exc:
        logger.debug("Could not parse STL bounding box from %s: %s", model_path, exc)
        # Fallback: use file size as rough proxy (1KB ~ 1mm height, very rough).
        size = os.path.getsize(model_path)
        stats["height_mm"] = max(1.0, size / 10000.0)

    return stats


def _parse_binary_stl_bbox(path: str) -> dict[str, Any]:
    """Parse a binary STL file and extract bounding box."""
    import struct

    with open(path, "rb") as fh:
        header = fh.read(80)
        if header[:5] == b"solid":
            # Might be ASCII STL — treat as unknown.
            raise AdaptiveSlicerError("ASCII STL not supported for bbox parsing")

        count_bytes = fh.read(4)
        if len(count_bytes) < 4:
            raise AdaptiveSlicerError("Truncated STL file")

        num_triangles = struct.unpack("<I", count_bytes)[0]
        if num_triangles == 0:
            return {"height_mm": 0.0}

        z_min = float("inf")
        z_max = float("-inf")

        for _ in range(min(num_triangles, 100000)):
            data = fh.read(50)  # 12 normal + 36 vertices + 2 attribute
            if len(data) < 50:
                break
            # 3 vertices, each 3 floats (x, y, z).
            for v in range(3):
                offset = 12 + v * 12
                _x, _y, z = struct.unpack_from("<fff", data, offset)
                z_min = min(z_min, z)
                z_max = max(z_max, z)

        if z_min == float("inf"):
            return {"height_mm": 0.0}

        return {"height_mm": round(z_max - z_min, 3)}


def _vlh_to_prusaslicer_string(
    vlh_data: list[tuple[float, float]],
) -> str:
    """Convert variable layer height pairs to PrusaSlicer string format.

    PrusaSlicer expects semicolon-delimited ``z;height`` pairs.
    """
    parts: list[str] = []
    for z, h in vlh_data:
        parts.append(f"{z:.4f};{h:.4f}")
    return ";".join(parts)


def _estimate_time(plan: AdaptiveLayerPlan) -> float:
    """Rough time estimate for an adaptive plan, in minutes."""
    total_seconds = 0.0
    for i, _h in enumerate(plan.layer_heights):
        speed_factor = plan.layer_speeds[i] if i < len(plan.layer_speeds) else 1.0
        effective_speed = _DEFAULT_PRINT_SPEED_MM_S * max(speed_factor, 0.1)
        # Estimate travel per layer proportional to layer height
        # (thicker layers = less layers but same XY travel).
        layer_time = _DEFAULT_LAYER_TRAVEL_MM / effective_speed
        total_seconds += layer_time
    return round(total_seconds / 60.0, 1)


def _estimate_time_uniform(height_mm: float, layer_height: float) -> float:
    """Rough time estimate for uniform layer height, in minutes."""
    import math

    layers = math.ceil(height_mm / layer_height)
    per_layer = _DEFAULT_LAYER_TRAVEL_MM / _DEFAULT_PRINT_SPEED_MM_S
    return round((layers * per_layer) / 60.0, 1)


def _parse_region_dicts(raw: list[dict[str, Any]], model_height: float) -> list[GeometryRegion]:
    """Parse a list of region dicts into GeometryRegion objects."""
    regions: list[GeometryRegion] = []
    for item in raw:
        try:
            rtype = RegionType(item["region_type"])
        except (KeyError, ValueError):
            continue
        regions.append(
            GeometryRegion(
                region_type=rtype,
                z_start_mm=float(item.get("z_start_mm", 0.0)),
                z_end_mm=float(item.get("z_end_mm", model_height)),
                area_pct=float(item.get("area_pct", 10.0)),
                min_feature_size_mm=(float(item["min_feature_size_mm"]) if "min_feature_size_mm" in item else None),
                overhang_angle=(float(item["overhang_angle"]) if "overhang_angle" in item else None),
                bridge_length_mm=(float(item["bridge_length_mm"]) if "bridge_length_mm" in item else None),
                wall_thickness_mm=(float(item["wall_thickness_mm"]) if "wall_thickness_mm" in item else None),
            )
        )
    return regions


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: AdaptiveSlicer | None = None
_singleton_lock = threading.Lock()


def get_adaptive_slicer() -> AdaptiveSlicer:
    """Return the module-level :class:`AdaptiveSlicer` singleton.

    Thread-safe lazy initialization.
    """
    global _instance
    if _instance is not None:
        return _instance
    with _singleton_lock:
        if _instance is None:
            _instance = AdaptiveSlicer()
    return _instance


def _reset_singleton() -> None:
    """Reset the singleton (for testing only)."""
    global _instance
    with _singleton_lock:
        _instance = None
