"""Adaptive slicing based on geometry and a material's slicing settings.

Analyzes model geometry to generate per-region slicing parameters instead
of uniform layer heights across an entire print.  Thin walls and fine
details get thinner layers for quality; bulk infill gets thicker layers for
speed; overhangs get adjusted cooling and speed; top surfaces get finer
layers for appearance.

The settings a plan is built from come in a :class:`MaterialSlicingProfile`.
Settings tuned to each material are Kiln's (https://kiln3d.com), asked for
through :mod:`kiln._pro_adaptive_bridge`; :func:`generic_profile` gives
settings for any material from the nozzle size alone, for a plan made when
the tuned settings cannot be asked for.

Usage::

    from kiln.adaptive_slicer import generic_profile, get_adaptive_slicer

    slicer = get_adaptive_slicer()
    profile = generic_profile("PLA", nozzle_diameter_mm=0.4)
    regions = slicer.analyze_geometry(model_stats={"height_mm": 30, ...})
    plan = slicer.generate_plan(
        regions, profile, model_height_mm=30.0, model_name="benchy"
    )
    config = slicer.export_config(plan, slicer=SlicerTarget.PRUSASLICER)
"""

from __future__ import annotations

import functools
import logging
import math
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
    """The slicing settings a plan is built from.

    ``top_surface_layers`` and ``cooling_threshold_layer_time_s`` are not
    read by the plan; generic settings leave them unstated (``None``).
    """

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
    top_surface_layers: int | None
    top_surface_speed_factor: float
    cooling_threshold_layer_time_s: float | None
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
    #: Built from :func:`generic_profile`, not from settings tuned to the
    #: material.  Said in the record only when true.
    basic: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
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
        if self.basic:
            d["basic"] = True
        return d


@dataclass
class SlicerConfig:
    """Exported slicer configuration with adaptive parameters."""

    slicer: SlicerTarget
    config_format: str
    config_data: dict[str, Any] = field(default_factory=dict)
    variable_layer_height_data: list[tuple[float, float]] | None = None
    notes: list[str] = field(default_factory=list)
    #: Exported from a plan built with generic settings; its first note
    #: says so.  Said in the record only when true.
    basic: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "slicer": self.slicer.value,
            "config_format": self.config_format,
            "config_data": dict(self.config_data),
            "notes": list(self.notes),
        }
        if self.variable_layer_height_data is not None:
            d["variable_layer_height_data"] = [list(pair) for pair in self.variable_layer_height_data]
        if self.basic:
            d["basic"] = True
        return d


# ---------------------------------------------------------------------------
# Nozzle-based layer height constraints
# ---------------------------------------------------------------------------

_MIN_LAYER_NOZZLE_RATIO = 0.25
_MAX_LAYER_NOZZLE_RATIO = 0.75

# Default base speed used for time estimation (mm/s).
_DEFAULT_PRINT_SPEED_MM_S = 60.0

# Average travel distance per layer for time estimation (mm).
_DEFAULT_LAYER_TRAVEL_MM = 200.0

#: The fan a layer runs at unless one of its regions asks for more.
_BASELINE_FAN_PCT = 50.0


# ---------------------------------------------------------------------------
# What a plan is made for: every plan is bounded before its first layer
# ---------------------------------------------------------------------------

#: The nozzle sizes, in mm, a plan or a material's settings are made for.
_PLAN_NOZZLE_MM = (0.1, 2.0)
#: The tallest part, in mm, planned when the printer catalogue cannot be read.
_FALLBACK_BUILD_HEIGHT_MM = 2000.0
#: The most layers one plan holds.
_MAX_PLAN_LAYERS = 100_000
#: The most regions one plan reads.
_MAX_PLAN_REGIONS = 200
#: The most region names one plan's layers list between them: what bounds
#: the plan's size when many regions span many layers.
_MAX_PLAN_REGION_ENTRIES = 250_000
#: The longest material name or mode a plan reads, and the most of anything
#: a caller sent that a refusal repeats.
MAX_TEXT_CHARS = 64


def _is_number(value: Any) -> bool:
    """A finite int or float (a bool is not a size; nor is a whole number too
    large to be one)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _shown(value: Any) -> str:
    """*value* as a refusal may repeat it: at most :data:`MAX_TEXT_CHARS`
    characters of it."""
    try:
        text = repr(value)
    except ValueError:  # a whole number too long to write out
        return "a number too large to write out"
    return text if len(text) <= MAX_TEXT_CHARS else f"{text[: MAX_TEXT_CHARS - 3]}..."


def text_refusal(material: Any, mode: Any = None) -> str | None:
    """Why a plan or a material's settings are refused for *material* or
    *mode* before anything is asked, or ``None``.

    A material that is not text or longer than :data:`MAX_TEXT_CHARS`; a mode
    (when given) that is not text or longer than that.  A mode of the right
    shape that the planner does not know is refused where a plan reads it.
    """
    if not isinstance(material, str):
        return "Invalid material: name the material as text, e.g. 'PLA'."
    if len(material) > MAX_TEXT_CHARS:
        return f"Invalid material: a material name is at most {MAX_TEXT_CHARS} characters."
    if mode is not None:
        valid = ", ".join(m.value for m in AdaptiveMode)
        if not isinstance(mode, str):
            return f"Invalid mode: name the mode as text. Valid: {valid}."
        if len(mode) > MAX_TEXT_CHARS:
            return f"Invalid mode: a mode is at most {MAX_TEXT_CHARS} characters. Valid: {valid}."
    return None


@functools.lru_cache(maxsize=1)
def max_build_height_mm() -> float:
    """The tallest rated build height in Kiln's printer catalogue, in mm:
    the tallest part a plan is made for.  The fallback stands in when the
    catalogue cannot be read."""
    try:
        from kiln.printer_intelligence import _read_public_json

        rows = _read_public_json()
    except Exception:  # noqa: BLE001 -- an unreadable catalogue is the fallback, never a crash
        return _FALLBACK_BUILD_HEIGHT_MM
    heights: list[float] = []
    for row in rows.values() if isinstance(rows, dict) else ():
        if not isinstance(row, dict):
            continue
        volume = row.get("rated_build_volume_mm") or row.get("build_volume_mm")
        if isinstance(volume, dict):
            volume = [volume.get("x"), volume.get("y"), volume.get("z")]
        if isinstance(volume, (list, tuple)) and len(volume) >= 3 and _is_number(volume[2]) and volume[2] > 0:
            heights.append(float(volume[2]))
    return max(heights) if heights else _FALLBACK_BUILD_HEIGHT_MM


def check_nozzle(nozzle_diameter_mm: Any) -> float:
    """*nozzle_diameter_mm* as a float, when it is a size a plan is made for.

    :raises AdaptiveSlicerError: for a value that is not a finite number, or
        one outside :data:`_PLAN_NOZZLE_MM`.
    """
    if not _is_number(nozzle_diameter_mm):
        raise AdaptiveSlicerError(
            f"Nozzle diameter must be a finite number of millimetres, got {_shown(nozzle_diameter_mm)}."
        )
    low, high = _PLAN_NOZZLE_MM
    if not low <= nozzle_diameter_mm <= high:
        raise AdaptiveSlicerError(
            f"Nozzle diameter must be between {low:g} and {high:g} mm, got {_shown(nozzle_diameter_mm)}."
        )
    return float(nozzle_diameter_mm)


def check_plan_inputs(model_height_mm: Any, nozzle_diameter_mm: Any) -> None:
    """Refuse a height or nozzle no plan is made for, before any layer.

    The height must be a finite number above zero and no taller than
    :func:`max_build_height_mm`; the nozzle must pass :func:`check_nozzle`.

    :raises AdaptiveSlicerError: naming what is wrong.
    """
    if not _is_number(model_height_mm):
        raise AdaptiveSlicerError(
            f"Model height must be a finite number of millimetres, got {_shown(model_height_mm)}."
        )
    if model_height_mm <= 0:
        raise AdaptiveSlicerError(f"Model height must be positive, got {_shown(model_height_mm)}.")
    tallest = max_build_height_mm()
    if model_height_mm > tallest:
        raise AdaptiveSlicerError(
            f"Model height {_shown(model_height_mm)} mm is taller than any printer Kiln knows of ({tallest:g} mm)."
        )
    check_nozzle(nozzle_diameter_mm)


# ---------------------------------------------------------------------------
# Generic settings: any material, from the nozzle size alone
# ---------------------------------------------------------------------------

#: Generic settings lay a layer at this share of the nozzle's width, inside
#: the window the two ratios above draw.
_GENERIC_LAYER_NOZZLE_RATIO = 0.5

#: Generic settings run overhangs, bridges, the first layer and top surfaces
#: at this share of the plan's base speed.
_GENERIC_SLOW_FACTOR = 0.5

#: What generic settings say they are.
GENERIC_NOTES = "Generic settings worked out from the nozzle size, not tuned to any material."


def generic_profile(material: str = "", *, nozzle_diameter_mm: float = 0.4) -> MaterialSlicingProfile:
    """Slicing settings for any material, worked out from the nozzle alone.

    The settings a plan is built from when Kiln's settings tuned to the
    material cannot be asked for.  The layer window is the nozzle's own (a
    quarter to three quarters of its width, every other layer height at
    half of it); overhangs, bridges, the first layer and top surfaces run at
    a fixed share of the base speed; overhangs and bridges keep the baseline
    fan; the overhang angle is printability's for every material
    (:func:`kiln.printability._resolve_overhang_threshold` with no
    material).  *material* is carried as a name and changes nothing else.
    The two fields a plan never reads are left unstated.

    :raises AdaptiveSlicerError: for a nozzle :func:`check_nozzle` refuses.
    """
    from kiln.printability import _resolve_overhang_threshold

    nozzle_diameter_mm = check_nozzle(nozzle_diameter_mm)
    low = round(nozzle_diameter_mm * _MIN_LAYER_NOZZLE_RATIO, 3)
    high = round(nozzle_diameter_mm * _MAX_LAYER_NOZZLE_RATIO, 3)
    middle = round(nozzle_diameter_mm * _GENERIC_LAYER_NOZZLE_RATIO, 3)
    return MaterialSlicingProfile(
        material=str(material or "").upper().strip(),
        min_layer_height_mm=low,
        max_layer_height_mm=high,
        optimal_layer_height_mm=middle,
        bridge_layer_height_mm=middle,
        overhang_max_angle=_resolve_overhang_threshold(None, None, None),
        overhang_speed_factor=_GENERIC_SLOW_FACTOR,
        overhang_fan_pct=_BASELINE_FAN_PCT,
        bridge_speed_mm_s=round(_DEFAULT_PRINT_SPEED_MM_S * _GENERIC_SLOW_FACTOR, 1),
        bridge_fan_pct=_BASELINE_FAN_PCT,
        first_layer_height_mm=middle,
        first_layer_speed_factor=_GENERIC_SLOW_FACTOR,
        top_surface_layers=None,
        top_surface_speed_factor=_GENERIC_SLOW_FACTOR,
        cooling_threshold_layer_time_s=None,
        notes=GENERIC_NOTES,
    )


def generic_label(material: str) -> str:
    """The note an export of a generic plan carries first."""
    name = str(material or "").strip() or "the material"
    return f"Generic settings worked out from the nozzle size, not tuned to {name}"


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
        :param material_profile: The settings to plan with: Kiln's settings
            tuned to the material, or :func:`generic_profile`.
        :param mode: Adaptive strategy.
        :param model_height_mm: Total model height in mm.
        :param model_name: Optional model name for the plan record.
        :param printer: Optional printer name.
        :param nozzle_diameter_mm: Nozzle diameter in mm.
        :returns: An :class:`AdaptiveLayerPlan`.
        :raises AdaptiveSlicerError: For a height or nozzle
            :func:`check_plan_inputs` refuses, more than
            :data:`_MAX_PLAN_REGIONS` regions, a layer that would not be
            thicker than zero, a plan past :data:`_MAX_PLAN_LAYERS` layers, or
            one whose layers would name more than
            :data:`_MAX_PLAN_REGION_ENTRIES` regions between them.
        """
        check_plan_inputs(model_height_mm, nozzle_diameter_mm)
        if len(regions) > _MAX_PLAN_REGIONS:
            raise AdaptiveSlicerError(
                f"A plan reads at most {_MAX_PLAN_REGIONS} regions, got {len(regions)}."
            )

        with self._lock:
            layer_heights: list[float] = []
            layer_regions_list: list[list[RegionType]] = []
            layer_speeds: list[float] = []
            layer_cooling: list[float] = []

            z = 0.0
            layer_idx = 0
            region_entries = 0

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

                if not height > 0:
                    raise AdaptiveSlicerError(
                        f"These settings lay a layer {height} mm thick at {round(z, 4)} mm; "
                        "every layer must be thicker than zero."
                    )
                if layer_idx >= _MAX_PLAN_LAYERS:
                    raise AdaptiveSlicerError(
                        f"This plan would take more than {_MAX_PLAN_LAYERS} layers; "
                        "check the model height and the layer settings."
                    )

                speed = self._compute_layer_speed(z, regions, material_profile)
                if layer_idx == 0:
                    speed = material_profile.first_layer_speed_factor

                cooling = self._compute_layer_cooling(z, regions, material_profile)

                active_regions = _regions_at_z(z, regions)
                region_entries += len(active_regions)
                if region_entries > _MAX_PLAN_REGION_ENTRIES:
                    raise AdaptiveSlicerError(
                        f"This plan's layers would name more than {_MAX_PLAN_REGION_ENTRIES:,} regions "
                        "between them; send fewer regions, or regions that cover less of the part's height."
                    )

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

        A plan built with generic settings exports marked ``basic``, its
        first note saying so.

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
        config = exporters[slicer](plan)
        if plan.basic:
            config.basic = True
            config.notes.insert(0, generic_label(plan.material))
        return config

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
        material_profile: MaterialSlicingProfile,
        model_height_mm: float,
        model_name: str = "",
        nozzle_diameter_mm: float = 0.4,
        mode: AdaptiveMode = AdaptiveMode.BALANCED,
        printer: str | None = None,
        regions: list[dict[str, Any]] | None = None,
    ) -> AdaptiveLayerPlan:
        """Convenience method combining analyze + plan in one call.

        :param material_profile: The settings to plan with.
        :param model_height_mm: Total model height.
        :param model_name: Optional model name.
        :param nozzle_diameter_mm: Nozzle diameter.
        :param mode: Adaptive strategy.
        :param printer: Optional printer identifier.
        :param regions: Optional pre-defined region dicts.  If omitted,
            a default STANDARD region is used.
        :returns: An :class:`AdaptiveLayerPlan`.
        """
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
            material_profile,
            mode=mode,
            model_height_mm=model_height_mm,
            model_name=model_name,
            printer=printer,
            nozzle_diameter_mm=nozzle_diameter_mm,
        )

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
        fan = _BASELINE_FAN_PCT

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

    from kiln.mesh_frame import is_3mf

    if not os.path.isfile(model_path):
        raise AdaptiveSlicerError(f"Model file not found: {model_path}")

    # Attempt minimal binary STL bounding-box parse; a 3MF is read by Kiln's
    # one mesh loader, never as STL bytes.
    stats: dict[str, Any] = {"source": model_path}
    try:
        stats.update(_mesh_height(model_path) if is_3mf(model_path) else _parse_binary_stl_bbox(model_path))
    except Exception as exc:
        # No fallback height.  A height derived from anything but the part
        # (its file size, say) is not a measurement of the part at all, and
        # it would arrive downstream indistinguishable from a real one.  A
        # caller that cannot be told the height must find that out, not be
        # handed a number nobody took.
        logger.debug("Could not parse geometry from %s: %s", model_path, exc)
        raise AdaptiveSlicerError(
            f"Could not read geometry from this file: {exc}"
        ) from exc

    return stats


def _mesh_height(path: str) -> dict[str, Any]:
    """The height of the mesh at *path*, read by :func:`kiln.mesh_frame.load_mesh`
    with every placement the file makes applied."""
    from kiln.mesh_frame import load_mesh

    mesh = load_mesh(path, force="mesh")
    if len(mesh.faces) == 0:
        return {"height_mm": 0.0}
    low, high = mesh.bounds
    return {"height_mm": round(float(high[2] - low[2]), 3)}


def _parse_binary_stl_bbox(path: str) -> dict[str, Any]:
    """Parse a binary STL file and extract bounding box.

    Raises rather than guessing when the file is not a binary STL: a STEP
    file, an ASCII STL, or a file too short for the triangle count in its
    header is refused, never unpacked as coordinates.

    A wrong number that announces itself as a measurement is worse than a
    refusal, because nothing downstream can tell it apart from a real one.
    """
    import os
    import struct

    from kiln.step_import import looks_like_step

    # Identity first, arithmetic second.  Whether this is CAD is a fact about
    # the file, not a conclusion to be reached from a byte count that happens
    # to look wrong — and saying so plainly is the difference between a user
    # converting their file and a user re-exporting a file that was fine.
    if looks_like_step(path):
        raise AdaptiveSlicerError(
            "This is a STEP (CAD) file, not a mesh, and nothing is wrong with "
            "it: this check reads meshes. Import it with import_step_file "
            '(output_format="stl") and run this on the STL it hands back, or '
            "export the part as STL from your CAD program."
        )

    with open(path, "rb") as fh:
        header = fh.read(80)
        if header[:5] == b"solid":
            # Might be ASCII STL — treat as unknown.
            raise AdaptiveSlicerError("ASCII STL not supported for bbox parsing")

        count_bytes = fh.read(4)
        if len(count_bytes) < 4:
            raise AdaptiveSlicerError("Truncated STL file")

        num_triangles = struct.unpack("<I", count_bytes)[0]

        # A binary STL is exactly 84 + 50n bytes, with no padding and no
        # optional sections, so the declared count is CHECKABLE against the
        # file on disk, and a file too short for its count is refused before
        # any byte is read as a coordinate.  Only the too-short direction is
        # refused, matching the check in generation/validation.py: some
        # exporters append trailing bytes to a perfectly good STL, and
        # refusing those would trade one wrong answer for a different one.
        declared = 84 + 50 * num_triangles
        actual = os.path.getsize(path)
        if actual < declared:
            raise AdaptiveSlicerError(
                f"Not a readable binary STL: the header declares "
                f"{num_triangles} triangles ({declared} bytes) but the file "
                f"is {actual} bytes."
            )

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


def _region_number(value: Any) -> float:
    """One number of a region, or a refusal that repeats at most
    :data:`MAX_TEXT_CHARS` characters of what was sent."""
    try:
        return float(value)
    except ValueError as exc:
        if isinstance(value, str):
            raise AdaptiveSlicerError(f"could not convert string to float: {_shown(value)}") from exc
        raise AdaptiveSlicerError(_shown(str(exc))) from exc
    except (TypeError, OverflowError) as exc:
        raise AdaptiveSlicerError(str(exc)) from exc


def _parse_region_dicts(raw: list[dict[str, Any]], model_height: float) -> list[GeometryRegion]:
    """Parse a list of region dicts into GeometryRegion objects.

    :raises AdaptiveSlicerError: for more than :data:`_MAX_PLAN_REGIONS`
        regions (before any is read) or a region number that is not one.
    """
    if isinstance(raw, (list, tuple)) and len(raw) > _MAX_PLAN_REGIONS:
        raise AdaptiveSlicerError(f"A plan reads at most {_MAX_PLAN_REGIONS} regions, got {len(raw)}.")
    regions: list[GeometryRegion] = []
    for item in raw:
        try:
            rtype = RegionType(item["region_type"])
        except (KeyError, ValueError):
            continue
        regions.append(
            GeometryRegion(
                region_type=rtype,
                z_start_mm=_region_number(item.get("z_start_mm", 0.0)),
                z_end_mm=_region_number(item.get("z_end_mm", model_height)),
                area_pct=_region_number(item.get("area_pct", 10.0)),
                min_feature_size_mm=(
                    _region_number(item["min_feature_size_mm"]) if "min_feature_size_mm" in item else None
                ),
                overhang_angle=(_region_number(item["overhang_angle"]) if "overhang_angle" in item else None),
                bridge_length_mm=(_region_number(item["bridge_length_mm"]) if "bridge_length_mm" in item else None),
                wall_thickness_mm=(
                    _region_number(item["wall_thickness_mm"]) if "wall_thickness_mm" in item else None
                ),
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
