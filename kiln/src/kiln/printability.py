"""Printability analysis engine for 3D models.

Analyzes STL/OBJ meshes for FDM printing readiness: overhang detection,
thin wall analysis, bridging assessment, bed adhesion surface estimation,
and support volume estimation.

Thin-wall analysis uses a vectorized ray-cast measurement (numpy
Möller-Trumbore) — every other analyzer remains stdlib-only.  numpy is
a required dependency so the measurement runs out of the box on every
install rather than requiring an opt-in extra.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np

from kiln import _vec
from kiln.generation.validation import (
    _bed_threshold_z,
    _is_bed_supported_triangle,
    _mesh_bed_z,
    _parse_obj,
    _parse_stl,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class OverhangAnalysis:
    """Results of overhang detection."""

    max_overhang_angle: float  # degrees from vertical; 90 = horizontal ceiling
    overhang_triangle_count: int
    overhang_percentage: float  # % of total triangles
    needs_supports: bool
    worst_regions: list[dict[str, float]]  # [{x, y, z, angle}]
    #: Steepest overhang that actually hangs in free air: excludes
    #: faces of self-supporting regions (short bridges, lateral
    #: closes, boolean seams, bed-proximate ceilings) AND flat bridge
    #: decks, which are judged by span, not angle.  This is the number
    #: per-material overhang limits should be compared against —
    #: ``max_overhang_angle`` reads 90 on any part with any ceiling.
    max_free_air_overhang_deg: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ThinWallAnalysis:
    """Results of thin wall detection."""

    min_wall_thickness_mm: float
    thin_wall_count: int  # walls below nozzle diameter
    thin_wall_percentage: float
    problematic_regions: list[dict[str, float]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CavityAnalysis:
    """Results of cavity / engrave / slot width detection.

    Sibling to ``ThinWallAnalysis`` but measures CAVITY DIMENSIONS rather
    than wall thicknesses.  Rays cast OUTWARD from each surface triangle's
    centroid; hits within a bbox-scaled cavity cap register a cavity
    width.  Outer-surface rays hit nothing (they travel into open space
    and get culled by the max-distance threshold), so only meaningful
    cavities — engraved grooves, debossed text, narrow slots,
    pocketed/inset features — produce signal.

    ``min_cavity_width_mm`` is the smallest measured cavity (or 0.0
    sentinel when no cavities are detected on the mesh).  Sub-perimeter
    cavities flag as 'unprintable' in the kiln-pro overlay (the slicer
    cannot reproduce a sub-extrusion gap; the feature closes up during
    printing).
    """

    min_cavity_width_mm: float
    cavity_sample_count: int  # samples whose outward ray hit within the cap
    problematic_regions: list[dict[str, float]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BridgingAnalysis:
    """Results of bridging assessment."""

    max_bridge_length_mm: float
    bridge_count: int
    needs_supports_for_bridges: bool
    #: Longest span whose filament genuinely crosses air (two-sided
    #: bridges, lateral closes, support-needing gaps).  Regions exempt
    #: by mechanism — solid directly below, ceilings a hair off the
    #: bed — contribute nothing here even though
    #: ``max_bridge_length_mm`` still reports them honestly.  This is
    #: the number per-material bridge limits should be compared
    #: against.
    max_free_air_span_mm: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BedAdhesionAnalysis:
    """Results of bed adhesion surface estimation."""

    contact_area_mm2: float
    contact_percentage: float  # % of bounding box footprint
    adhesion_risk: str  # "low", "medium", "high"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SupportAnalysis:
    """Results of support volume estimation.

    ``estimated_support_volume_mm3`` is the naive area×height projection
    of every overhang triangle to the build plate — useful as an upper
    bound but typically over-estimates real slicer extrusion by 3-8×
    (Grid) or 8-15× (Organic). Pro+ tier callers get a calibrated
    follow-up via ``report.enrichment.supports_calibration``.

    ``likely_substituted_by_bridge`` is True when at least one overhang
    region in this report is geometrically positioned such that
    PrusaSlicer's auto-support will probably choose to bridge across it
    instead of generating supports — common for horizontal undersides
    above 4-corner-leg topologies (tabletop), short-span U-shapes, and
    square bridges where the gap is <30mm. The user may want to FORCE
    supports for surface-quality reasons even when the slicer wouldn't
    generate them.
    """

    estimated_support_volume_mm3: float
    support_percentage: float  # % of model volume
    support_regions: list[dict[str, float]]
    likely_substituted_by_bridge: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AdhesionRecommendation:
    """Recommended brim/raft settings for a model + material + printer.

    Produced by :func:`recommend_adhesion` and consumable directly as
    slicer profile overrides via ``resolve_slicer_profile(overrides=rec.slicer_overrides)``.
    """

    brim_width_mm: int
    use_raft: bool
    adhesion_risk: str  # "low", "medium", "high" — from BedAdhesionAnalysis
    contact_percentage: float
    rationale: str
    slicer_overrides: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrintFailureDiagnosis:
    """Synthesised diagnosis from physical state + model analysis.

    Produced by :func:`diagnose_from_signals`.  The ``confidence`` field
    tells agents whether to auto-act (>0.7) or surface for human review.
    """

    failure_category: str  # "adhesion", "thermal", "geometry", "mechanical", "unknown"
    probable_causes: list[str]
    recommended_fixes: list[str]
    confidence: float  # 0.0-1.0
    signals: dict[str, Any] = field(default_factory=dict)
    slicer_overrides: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WarpingAnalysis:
    """Results of warping risk assessment."""

    risk_level: str  # "low", "moderate", "high", "critical"
    score_deduction: int  # 0 to -20
    large_flat_surfaces: list[dict[str, float]]  # [{area_mm2, centroid_x/y/z}]
    sharp_corners_at_base: int  # corners with angle < 90° in bottom 5mm
    height_to_base_ratio: float  # bbox height / min(width, depth)
    material_warping_tendency: str  # from materials.json
    recommendations: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ThermalStressAnalysis:
    """Results of thermal stress concentration analysis."""

    risk_level: str  # "low", "moderate", "high", "critical"
    score_deduction: int  # 0 to -15
    max_area_change_ratio: float  # largest layer-to-layer area change ratio
    stress_concentration_zones: list[dict[str, float]]  # [{z_mm, area_change_ratio, layer_area_mm2}]
    layer_count_analyzed: int
    material_stress_factor: float  # multiplier from material thermal properties
    recommendations: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AdhesionForceEstimate:
    """Force-balance adhesion prediction.

    ``risk_level`` is a best-effort approximation from a static
    force-balance model.  It is reliable at the extremes (clear
    detach vs. clear secure) and approximate in the middle range
    where dynamic peel stress, thermal cycling, and material-
    specific bed-chemistry effects dominate.  For high-aspect-ratio
    prints in warp-prone materials (PP, Nylon, ABS), treat a
    ``secure`` verdict as ``plausible`` rather than ``verified``.
    The ``model_confidence`` field exposes this band per result so
    callers can decide how much to trust it.

    Empirical work to recalibrate the model against real outcomes
    (see ``outcome_tracker``) is tracked as a separate project.
    """

    adhesion_force_n: float  # estimated adhesion force in Newtons
    peel_force_n: float  # estimated thermal peel force in Newtons
    force_ratio: float  # adhesion / peel — >1.0 means adhesion wins
    will_detach: bool  # True if peel_force > adhesion_force
    risk_level: str  # "secure", "marginal", "likely_detach"
    score_deduction: int  # 0 to -10
    recommendations: list[str]
    # Confidence band on the verdict itself.  ``high`` for clear
    # extremes (ratio outside [0.5, 10] OR an absolute likely_detach);
    # ``approximate`` for the messy middle.  Callers SHOULD branch
    # on this when surfacing a verdict to a user — e.g. soften an
    # ``approximate``+``secure`` to "looks OK but worth a brim" in
    # an agent reply, vs. trust a ``high``+``secure`` as-is.
    model_confidence: str = "high"  # "high" | "approximate"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CostAnalysis:
    """Cost breakdown integrated with printability analysis."""

    estimated_cost_usd: float
    material_cost_usd: float
    support_cost_usd: float
    adhesion_cost_usd: float
    electricity_cost_usd: float
    weight_grams: float
    filament_length_meters: float
    cost_breakdown: dict[str, float]
    cost_summary: dict[str, float]
    cost_saving_recommendations: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrintabilityReport:
    """Full printability analysis report."""

    printable: bool
    score: int  # 0-100
    grade: str  # A/B/C/D/F
    overhangs: OverhangAnalysis
    thin_walls: ThinWallAnalysis
    bridging: BridgingAnalysis
    bed_adhesion: BedAdhesionAnalysis
    supports: SupportAnalysis
    warping: WarpingAnalysis | None = None
    thermal_stress: ThermalStressAnalysis | None = None
    adhesion_force: AdhesionForceEstimate | None = None
    cost: CostAnalysis | None = None
    # Cavity-width analysis — sibling to ``thin_walls`` measuring the
    # dimensions of cavities cut INTO the mesh (engraved grooves,
    # debossed text, narrow slots) via outward ray-casting.  Defaults
    # to ``None`` for clients that construct the report directly.
    cavities: CavityAnalysis | None = None
    # Bounding-box dimensions.  Keys: ``width_mm``, ``depth_mm``,
    # ``height_mm``.  Exposed for the kiln-pro overlay.
    dimensions_mm: dict[str, float] = field(default_factory=dict)
    model_height_mm: float = 0.0
    recommendations: list[str] = field(default_factory=list)
    estimated_print_time_modifier: float = 1.0  # 1.0 = normal
    # Detected cylindrical-hole features.  Each entry is a dict with
    # keys ``position`` (dict[x_mm,y_mm,z_mm]), ``diameter_mm``,
    # ``depth_mm``, ``axis`` (one of "x"/"y"/"z"), ``triangle_count``.
    # Populated by ``analyze_printability``; exposed for the kiln-pro
    # overlay and per-machine calibration.
    holes: list[dict[str, Any]] = field(default_factory=list)
    # Optional kiln-pro overlay block.  Populated by
    # ``analyze_printability`` when the kiln-pro package is installed
    # (Pro+ tier); absent on free / public installs.  See kiln3d.com
    # for tier details.
    enrichment: dict[str, Any] | None = None
    # Total triangle count in the parsed mesh.  Exposed so downstream
    # consumers can gauge mesh-density confidence — a coarse mesh below
    # ~500 triangles with ``thin_walls.thin_wall_count == 0`` is a "low
    # mesh density" signal, not a "no thin walls" signal.  Defaults to 0
    # for clients that construct PrintabilityReport directly without
    # going through ``analyze_printability``.
    triangle_count: int = 0
    # Number of disjoint connected components in the mesh (triangle
    # islands joined by shared-vertex adjacency).  A single closed body
    # is 1; a multi-body mesh is N.  Exposed for the kiln-pro overlay.
    # Defaults to 0 for clients that construct PrintabilityReport
    # directly.
    connected_components: int = 0
    # Component-size uniformity: 0.0–1.0 scalar (rounded to 3 places).
    # 1.0 = every component has the same bbox volume; near 0.0 = one big
    # component plus tiny ones.  Trivially 1.0 for single-component
    # meshes.  Exposed for the kiln-pro overlay.  Defaults to 0.0.
    component_size_uniformity: float = 0.0
    # Topological genus, summed across components, derived from the
    # Euler characteristic χ = V − E + F — the count of independent
    # "holes through the body" (a closed solid is 0, a torus is 1).
    # Non-closed / non-manifold meshes produce anomalous values that
    # don't match the physical handle count; consumers should not treat
    # the number as authoritative when ``is_manifold`` is False or the
    # mesh is otherwise known-open.  Exposed for the kiln-pro overlay.
    # Defaults to 0.
    genus: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BundlePrintabilityFindings:
    """The minimal printability surface readable from an inspection bundle.

    :class:`PrintabilityReport` carries detail-rich sub-analyses
    (overhangs, thin walls, bridging, bed adhesion, supports, warping,
    thermal stress, adhesion force, cost) produced by running
    :func:`analyze_printability` against a mesh.  When a caller already
    has an inspection bundle attached upstream (the
    ``inspection_bundle`` field from ``attach_inspect_bundle`` in
    kiln-pro), it can read the printability summary out of the bundle's
    channels without re-running those analyses — same answer, near-zero
    marginal cost.

    This adapter exposes only the summary fields that downstream
    consumers (audit, validation pipelines) actually read: ``printable``,
    ``score``, ``grade``, ``recommendations``, and ``to_dict()``.  Field
    names and types match :class:`PrintabilityReport` exactly, so
    callers can branch on which one they got without changing how they
    read.  ``to_dict()`` returns the raw bundle-channel findings so the
    audit's ``details`` payload stays bundle-faithful rather than
    flattening to the adapter's narrower view.
    """

    printable: bool
    score: int  # 0-100
    grade: str  # A/B/C/D/F
    recommendations: list[str] = field(default_factory=list)
    _findings: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_bundle_findings(
        cls, findings: dict[str, Any]
    ) -> BundlePrintabilityFindings:
        score = int(findings.get("score") or 0)
        return cls(
            printable=bool(
                findings.get("printable", score >= _PRINTABLE_SCORE_MIN)
            ),
            score=score,
            grade=str(findings.get("grade") or "F"),
            recommendations=list(findings.get("recommendations") or []),
            _findings=dict(findings),
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self._findings)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Materials with high warping tendency — need wider brims and may need rafts.
# Extended 2026-05-17 to include materials whose curated warping_factor
# is >= 0.85 but were previously excluded from the open-frame warning:
# PP, PEEK, PA6_GF, CF_NYLON, PC_ABS, HIPS, NYLON, POLYCARBONATE.  Cross-
# wired with kiln-pro's ``_WARP_PRONE_MATERIALS`` set.
_HIGH_WARP_MATERIALS: frozenset[str] = frozenset({
    "ABS", "ASA", "PA", "PA6", "PA12", "PC", "ABS-CF", "ASA-CF",
    "NYLON", "POLYCARBONATE", "PC-ABS", "PC_ABS",
    "PA6-GF", "PA6_GF", "PA-CF", "CF-NYLON", "CF_NYLON",
    "PP", "PEEK", "HIPS",
})

# Known bed-slinger printers where Y-axis bed movement worsens adhesion.
_BEDSLINGER_PRINTERS: frozenset[str] = frozenset({
    "bambu_a1", "bambu_a1_mini",
    "ender3", "ender3_v2", "ender3_s1", "ender3_s1_pro", "ender3_neo",
    "cr10", "cr10_v2", "cr10_v3",
    "prusa_mk3s", "prusa_mini",
    "anycubic_mega", "anycubic_kobra",
    "artillery_sidewinder",
})

# Conservative single defaults for per-material physics constants.
#
# Public Kiln keeps a single conservative default for each constant —
# chosen so a free-tier report is safe, but uniform across materials.
# When kiln-pro is installed, the bridge helpers below consult
# ``kiln_pro.printability_overlay.lookup_material`` for SME-tuned
# per-material values.  https://kiln3d.com — per-material
# differentiation is a kiln-pro Pro+ feature.
_DEFAULT_STRESS_FACTOR: float = 1.0
_DEFAULT_ADHESION_STRENGTH: float = 0.10  # N/mm²
_DEFAULT_SHRINKAGE_STRAIN: float = 0.005  # mm/mm


def _material_physics_from_overlay(material: str | None) -> dict[str, float]:
    """Pull per-material physics fields from the kiln-pro overlay.

    Returns ``{}`` when kiln-pro is not installed or the overlay has
    no entry for ``material``.  Callers branch on truthiness and
    fall back to the conservative public default.  This is the
    single bridge point used by the three helpers below — keeping
    the import + isinstance plumbing in one place.
    """
    if not material:
        return {}
    try:
        from kiln_pro.bridge import pro_features  # type: ignore[import-not-found]
    except ImportError:
        return {}
    if not pro_features.is_available("printability_overlay"):
        return {}
    try:
        entry = pro_features.printability_overlay.lookup_material(material)
    except Exception:  # noqa: BLE001 — overlay failure must not break public path
        return {}
    return entry if isinstance(entry, dict) else {}


def _material_stress_factor(material: str | None) -> float:
    """Return the thermal-stress multiplier for ``material``.

    Free tier (no kiln-pro): the conservative default ``1.0`` for
    every material — high enough to flag genuine stress risks
    without being so high that every PLA print looks dangerous.
    Pro+ tier: per-material curated values (PLA 0.6, ABS 1.5,
    Nylon 1.6, etc.) come from the kiln-pro overlay.
    """
    overlay_entry = _material_physics_from_overlay(material)
    value = overlay_entry.get("stress_factor")
    if isinstance(value, (int, float)):
        return float(value)
    return _DEFAULT_STRESS_FACTOR


def _material_adhesion_strength(material: str | None) -> float:
    """Return bed-adhesion strength (N/mm²) for ``material``.

    Free tier: the conservative default ``0.10 N/mm²``, midway
    between PLA (good adhesion) and Nylon (poor adhesion).  Pro+
    tier: per-material curated values from the kiln-pro overlay.
    """
    overlay_entry = _material_physics_from_overlay(material)
    value = overlay_entry.get("adhesion_strength")
    if isinstance(value, (int, float)):
        return float(value)
    return _DEFAULT_ADHESION_STRENGTH


def _material_shrinkage_strain(material: str | None) -> float:
    """Return linear shrinkage strain (mm/mm) for ``material``.

    Free tier: the conservative default ``0.005`` (~0.5% linear
    shrinkage) — a typical mid-range value.  Pro+ tier: per-material
    curated values from the kiln-pro overlay.
    """
    overlay_entry = _material_physics_from_overlay(material)
    value = overlay_entry.get("shrinkage_strain")
    if isinstance(value, (int, float)):
        return float(value)
    return _DEFAULT_SHRINKAGE_STRAIN



# ---------------------------------------------------------------------------
# Printability-judgment tier seam — public safe-default rule lists
# ---------------------------------------------------------------------------
#
# Soft seam. Free tier (overlay returns {}) uses the _*_PUBLIC_DEFAULTS
# below — same rule shapes + values as the pre-seam code, kept here as
# the public floor. Pro+ overlay supplies curated / tuned versions via
# the ``printability_judgment`` overlay.

_WARPING_PUBLIC_DEFAULTS: dict[str, Any] = {
    "geometry_score_rules": [
        # Textbook-geometric safety-floor thresholds.  No per-material
        # curation here — Pro overlay can override the same shape with
        # tighter values; these are the floor any FDM practitioner
        # would agree on.  Tightened 2026-05-17:
        # - flat>2000 -> flat>4000 score 1.  The pre-rework 2000 floor
        #   fired score 1 for compact prints (40x40x?, 60x30x?) at
        #   3000-4000 mm² flat area, producing "moderate" verdicts on
        #   tall PLA vases / small ASA brackets / compact ABS cubes
        #   that PLA-family / ASA-family prints handle fine.  4000 is
        #   the threshold above which flat-area genuinely matters even
        #   for forgiving materials (any FDM textbook agrees).
        # - h/b>3 -> h/b>2.4 score 1.  Tall-thin geometry concentrates
        #   warping stress at the base regardless of material; 2.4 is
        #   the threshold above which the practical "tall thin" label
        #   applies (CNC Kitchen / Stefan Hermann convention).
        # - sharp_corners>10 -> sharp_corners>8 score 1.  Same idea:
        #   sharp corners curl at the base in any high-CTE material;
        #   8 corners (2 per side on a typical box base) is the floor
        #   above which curling becomes the dominant failure mode.
        {"metric": "flat_area_total_mm2",  "operator": ">", "threshold": 20000.0, "score": 2},
        {"metric": "flat_area_total_mm2",  "operator": ">", "threshold": 4000.0,  "score": 1},
        {"metric": "height_to_base_ratio", "operator": ">", "threshold": 5.0,     "score": 2},
        {"metric": "height_to_base_ratio", "operator": ">", "threshold": 2.4,     "score": 1},
        {"metric": "sharp_corners_at_base","operator": ">", "threshold": 8,       "score": 1},
    ],
    "material_multipliers": {"low": 0.5, "moderate": 1.0, "high": 1.5, "very_high": 2.0},
    "risk_thresholds":      {"critical": 3.0, "high": 2.0, "moderate": 1.0},
    "score_deductions":     {"critical": -20, "high": -12, "moderate": -6, "low": 0},
    # ``material_baseline_risk`` and ``material_specific_multipliers``
    # are intentionally absent in the free-tier safety floor.  The
    # formula ``(baseline + geometry_score) * material_multiplier``
    # reads both as ``cfg.get(..., {})``-defaulted dicts; absent ->
    # empty -> baseline 0.0 and multiplier falls through to the
    # tendency mapping above.  Free tier = geometric risk + textbook
    # tendency labels.  The Pro overlay supplies the curated
    # per-material baselines + multiplier overrides.
    "recommendation_rules": [
        {"metric": "flat_area_total_mm2",  "operator": ">", "threshold": 2000.0,
         "template": "Large flat surface detected ({flat_area_total_mm2:.0f}mm²). Add a 5-8mm brim to resist corner lifting."},
        {"metric": "height_to_base_ratio", "operator": ">", "threshold": 3.0,
         "template": "Tall/narrow geometry (ratio {height_to_base_ratio:.1f}). Consider splitting into shorter sections or adding a wider base."},
        {"metric": "material_tendency",    "operator": "in", "threshold": ["high", "very_high"],
         "template": "Material ({material}) has {material_tendency} warping tendency. Print in an enclosed chamber and increase bed temperature to reduce thermal gradients."},
        {"metric": "sharp_corners_at_base","operator": ">", "threshold": 5,
         "template": "Sharp corners at the base are prone to curling. Add mouse-ear supports or a brim."},
    ],
}

_THERMAL_STRESS_PUBLIC_DEFAULTS: dict[str, Any] = {
    # Thresholds re-calibrated post wall-vs-face fix.  The buggy pre-fix
    # model over-reported ratios (cubes hit ~40 000x because top/bottom
    # face triangles dumped huge area into boundary buckets); thresholds
    # had been raised to {critical:6, high:4, moderate:2.5} to mask the
    # noise.  Corrected model produces real ratios on legit geometries
    # in the 1.0-5.0 range, so thresholds come down accordingly.
    "risk_thresholds":  {"critical": 5.0, "high": 3.0, "moderate": 1.8},
    "score_deductions": {"critical": -15, "high": -10, "moderate": -5, "low": 0},
    "stress_zone_ratio_threshold": 1.8,
    "max_zones_tracked": 20,
    "max_zones_emitted": 10,
    "recommendation_rules": [
        {"metric": "risk_level",            "operator": "in", "threshold": ["high", "critical"],
         "template": "Cross-section changes abruptly between layers (peak ratio {max_ratio:.1f}x at z={peak_zone_z:.1f}mm). Add a 2-4mm chamfer or fillet at the transition to spread thermal contraction stress."},
        {"metric": "risk_level",            "operator": "==", "threshold": "critical",
         "template": "Critical thermal stress risk. Slow print speed to 30-40mm/s through the transition zone, and increase fan ramp-up to avoid sudden cooling at the geometry change."},
        {"metric": "many_zones",            "operator": "==", "threshold": True,
         "template": "Multiple stress concentration zones detected ({stress_zones_count} layers above threshold). Consider splitting the part at the worst transition and printing as two pieces."},
        {"metric": "amplification_active",  "operator": "==", "threshold": True,
         "template": "Material ({material}) amplifies thermal stress (factor {stress_factor:.1f}x). Print in an enclosed chamber, increase bed temperature 5-10°C above the default, and avoid sudden layer cooling."},
        {"metric": "risk_level",            "operator": "==", "threshold": "moderate",
         "template": "Moderate cross-section variation (max ratio {max_ratio:.1f}x). Adding transition layers (3-5mm chamfer at the geometry change) typically eliminates the stress concentration."},
    ],
}

_ADHESION_FORCE_PUBLIC_DEFAULTS: dict[str, Any] = {
    "risk_thresholds":  {"secure": 3.0, "marginal": 1.5},
    "score_deductions": {"secure": 0, "marginal": -3, "likely_detach": -10},
    "peel_force_scale": 0.01,
    "poor_adhesion_materials": ["pp", "nylon", "pa", "peek"],
    # Geometry-only adhesion guard.  The force-balance model uses
    # static material constants and undercounts dynamic peel stress
    # on extreme aspect ratios — even Pro-tier per-material values
    # rate a 2x2x250mm PP tower as "secure" via force ratio alone,
    # despite real-world detach risk.  When the bounding-box aspect
    # ratio (z / min(x,y)) exceeds this threshold, the geometry
    # itself is enough to warrant a "marginal" verdict regardless of
    # the force-balance result.  Set to ``None`` in the overlay to
    # disable.  Tracked separately from the model rework planned for
    # the next release; this is the surgical pre-rework guard.
    "aspect_ratio_extreme_threshold": 50.0,
    # Aspect-ratio multiplier on peel force.  Tall-narrow geometry
    # concentrates peel stress at the base nonlinearly — the static
    # model linear in z systematically undercounts this for aspect
    # ratios above ~10.  Multiplies peel_force by
    # ``max(1.0, (aspect_ratio / 10) ** exponent)`` so the verdict
    # for messy-middle geometries shifts toward "marginal" without
    # affecting compact prints (aspect <= 10 → no change).
    #
    # Exponent calibration: 1.5 chosen against the 24-case sweep
    # (see kiln/tests/test_adhesion_force.py calibration matrix +
    # kiln-pro tasks.md adhesion model section).  Strict improvement
    # over no multiplier: catches PP narrow column 10x10x200 with
    # zero false positives on the 23 safe-print sample.  Set to
    # ``None`` in the overlay to disable; tune to a different value
    # (e.g. 2.0 catches one more case but introduces a false
    # positive on PLA candleholder 4x4x200).  Awaiting empirical
    # recalibration from outcome_tracker data — see Layer 3 in
    # kiln-pro tasks.md.
    "aspect_ratio_peel_exponent": 1.5,
    # Thermal-stress contribution to peel force.  Warp-prone
    # materials (high CTE — ABS, ASA, Nylon, PP, PEEK) generate
    # cyclic peel stress as each layer cools and contracts against
    # the constrained base.  This stress accumulates with print
    # height roughly linearly until thermal equilibrium.  Multiplies
    # peel by ``(1.0 + stress_factor * z / thermal_z_scale)`` so
    # warp-prone tall prints see proportionally more peel pressure
    # while PLA / PETG (low stress_factor 0.3-0.7) see modest impact.
    #
    # Calibration: thermal_z_scale=100 (mm) chosen against the
    # 31-case calibration matrix — catches all 8 truly-risky cases
    # (100% catch rate) with zero false positives on the 23 safe-
    # print sample.  The PLA candleholder 4x4x200 (aspect 50,
    # PLA stress=0.6) lands at ratio 3.05, just above the 3.0
    # secure threshold — that's the only borderline case in the
    # sample, and it's correctly classified.  Set to ``None`` in
    # the overlay to disable.  Lower values (e.g. 50) over-flag
    # warp-prone PLA; higher values (e.g. 200) miss ABS tall thin.
    # Awaiting empirical recalibration from outcome_tracker data.
    "peel_thermal_z_scale": 100.0,
    "recommendation_rules": [
        {"metric": "risk_level", "operator": "==", "threshold": "likely_detach",
         "template": "Part will likely detach during printing. Use a brim (8mm+), glue stick, or raft."},
        {"metric": "risk_level", "operator": "==", "threshold": "marginal",
         "template": "Adhesion is borderline. Adding a 5mm brim is recommended."},
        {"metric": "is_poor_adhesion_material", "operator": "==", "threshold": True,
         "template": "Material ({material}) has very poor adhesion on standard build surfaces. Use a specialized build sheet (e.g., Garolite for nylon, PP sheet for PP)."},
    ],
}

# Public floor: the universal 45° rule that every cited 3D-printing
# source treats as the safety-baseline angle below which any FDM
# material prints cleanly without supports.  The free tier sees this
# floor for every material.  Pro+ overlay (``printability_judgment``
# overlay's ``overhangs`` block) supplies per-material limits keyed by
# filament identifier; warp-prone materials (TPU, PP) drop below 45°,
# forgiving materials (PLA, pla_plus) can sit above it.  The lookup
# falls back to ``default_limit_deg`` when a material is absent from
# the overlay's ``material_limits_deg``, and to the public 45° rule
# when the overlay itself is absent.
_OVERHANGS_PUBLIC_DEFAULTS: dict[str, Any] = {
    "default_limit_deg": 45.0,
    # Free tier ships zero per-material overrides — the universal 45°
    # rule is the only floor.  Pro+ overlay provides the per-material
    # SME-tuned values; see kiln3d.com for tier details.
    "material_limits_deg": {},
}


def _resolve_overhang_threshold(
    explicit: float | None,
    material: str | None,
    overlay: dict[str, Any] | None,
) -> float:
    """Centralized lookup: caller > overlay material > overlay default > 45°.

    Single source of truth for the effective overhang threshold so the
    overhang detector and the support-volume estimator stay synchronized.
    Without this, ``_analyze_supports`` would silently use a different
    threshold than ``_analyze_overhangs`` (which IS material-aware),
    producing the contradictory report ``needs_supports=True`` with
    ``estimated_support_volume_mm3=0`` for materials whose per-material
    threshold falls below the supports estimator's 45° default.

    Mirrors the lookup pattern inside ``_analyze_overhangs`` but exposes
    it for callers that need the resolved value upstream (i.e.
    ``analyze_printability``, which feeds both sub-analyses).
    """
    if explicit is not None:
        return float(explicit)
    cfg = (overlay or {}).get("overhangs") or _OVERHANGS_PUBLIC_DEFAULTS
    material_limits = cfg.get("material_limits_deg") or {}
    default_limit = cfg.get(
        "default_limit_deg",
        _OVERHANGS_PUBLIC_DEFAULTS["default_limit_deg"],
    )
    if material is not None:
        normalized_target = material.strip().lower().replace(
            "-", "_"
        ).replace(" ", "_")
        for key, val in material_limits.items():
            if key.strip().lower().replace("-", "_").replace(" ", "_") == normalized_target:
                return float(val)
    return float(default_limit)


def _check_rule_op(op: str, value: Any, threshold: Any) -> bool:
    """Compare ``value`` to ``threshold`` per ``op``.  False on any
    operator/value mismatch (forward-compat: unknown operators are
    silently skipped, not raised)."""
    try:
        if op == ">":
            return value > threshold
        if op == "<":
            return value < threshold
        if op == ">=":
            return value >= threshold
        if op == "<=":
            return value <= threshold
        if op == "==":
            return value == threshold
        if op == "!=":
            return value != threshold
        if op == "in":
            return value in threshold
    except TypeError:
        return False
    return False


def _sum_score_rules(rules: list[dict[str, Any]], metrics: dict[str, Any]) -> int:
    """Sum ``score`` for each matching rule.  Per-metric first-match
    semantics — multiple rules for the same metric fire at most once
    (mirrors the elif-chain semantics of the pre-seam code)."""
    total = 0
    fired_metrics: set[str] = set()
    for rule in rules:
        metric = rule.get("metric")
        if metric is None or metric in fired_metrics:
            continue
        if metric not in metrics:
            continue
        if not _check_rule_op(rule.get("operator", ">"), metrics[metric], rule.get("threshold")):
            continue
        total += int(rule.get("score", 0))
        fired_metrics.add(metric)
    return total


def _apply_recommendation_rules(
    rules: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> list[str]:
    """Emit the formatted template for each rule that matches, in order."""
    out: list[str] = []
    for rule in rules:
        metric = rule.get("metric")
        if metric is None or metric not in metrics:
            continue
        if not _check_rule_op(rule.get("operator", ">"), metrics[metric], rule.get("threshold")):
            continue
        template = rule.get("template")
        if not template:
            continue
        try:
            out.append(template.format(**metrics))
        except (KeyError, IndexError, ValueError):
            out.append(template)
    return out


# ---------------------------------------------------------------------------
# Internal geometry helpers
# ---------------------------------------------------------------------------


_normalize = _vec.normalize


def _triangle_normal(
    v1: tuple[float, ...],
    v2: tuple[float, ...],
    v3: tuple[float, ...],
) -> tuple[float, float, float]:
    """Compute the (unnormalized) normal vector of a triangle via cross product."""
    return _vec.cross(_vec.sub(v2, v1), _vec.sub(v3, v1))


def _triangle_area(
    v1: tuple[float, ...],
    v2: tuple[float, ...],
    v3: tuple[float, ...],
) -> float:
    """Compute the area of a triangle from its vertices."""
    n = _triangle_normal(v1, v2, v3)
    return 0.5 * math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)


def _triangle_centroid(
    v1: tuple[float, ...],
    v2: tuple[float, ...],
    v3: tuple[float, ...],
) -> tuple[float, float, float]:
    """Compute the centroid of a triangle."""
    return (
        (v1[0] + v2[0] + v3[0]) / 3.0,
        (v1[1] + v2[1] + v3[1]) / 3.0,
        (v1[2] + v2[2] + v3[2]) / 3.0,
    )


def _signed_volume_of_triangle(
    v1: tuple[float, ...],
    v2: tuple[float, ...],
    v3: tuple[float, ...],
) -> float:
    """Compute the signed volume contribution of a triangle to a mesh volume.

    Uses the divergence theorem: V = (1/6) * sum(dot(v1, cross(v2, v3)))
    for each triangle.
    """
    return (
        v1[0] * (v2[1] * v3[2] - v2[2] * v3[1])
        + v1[1] * (v2[2] * v3[0] - v2[0] * v3[2])
        + v1[2] * (v2[0] * v3[1] - v2[1] * v3[0])
    ) / 6.0


def _vertex_distance(
    a: tuple[float, ...],
    b: tuple[float, ...],
) -> float:
    """Euclidean distance between two 3D points."""
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _signed_volume_total(
    triangles: list[tuple[tuple[float, ...], ...]],
) -> float:
    """Sum of signed tetrahedral volumes formed by each triangle with the
    origin. For a closed manifold mesh, this equals +V (enclosed volume)
    when winding points outward and -V when winding points inward.
    For a winding-inconsistent mesh, contributions partially cancel and
    the magnitude shrinks toward zero — that's the signal we use to
    decide whether to trust the winding or fall back to the heuristic.
    """
    total = 0.0
    for a, b, c in triangles:
        # det([a; b; c]) / 6 = signed volume of tetrahedron (origin, a, b, c)
        total += (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            + a[1] * (b[2] * c[0] - b[0] * c[2])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        ) / 6.0
    return total


def _bbox_volume(
    triangles: list[tuple[tuple[float, ...], ...]],
) -> float:
    """Axis-aligned bounding-box volume of a triangle list."""
    if not triangles:
        return 0.0
    xs = [v[0] for tri in triangles for v in tri]
    ys = [v[1] for tri in triangles for v in tri]
    zs = [v[2] for tri in triangles for v in tri]
    return (max(xs) - min(xs)) * (max(ys) - min(ys)) * (max(zs) - min(zs))


# Threshold on |signed_volume| / bbox_volume to classify a mesh as
# winding-consistent. Real CAD-exported solids run 0.15-1.0 (cantilever
# brackets at 0.15, solid cubes at 1.0). Winding-inconsistent meshes
# partially cancel and run < 0.05. The 0.05 floor cleanly separates the
# regimes without false-positive-ing thin or hollow geometry.
_WINDING_CONSISTENCY_THRESHOLD = 0.05


def _drop_membrane_pairs(
    triangles: list[tuple[tuple[float, ...], ...]],
) -> list[tuple[tuple[float, ...], ...]]:
    """Remove zero-thickness membrane artifacts from a triangle soup.

    Exactly-coincident stacked solids sometimes survive OpenSCAD's
    union unfused: the shared plane is exported as two coplanar faces
    over the same vertices, one facing up and one facing down.  The
    membrane encloses no volume, but the downward half reads as a real
    interior ceiling and poisons the overhang, bridging, and support
    verdicts (2026-08-25: a barbed hose adapter graded C on 480 such
    phantom faces).

    A pair is dropped only when two triangles share the same three
    vertices (to 0.1 um) with opposing normals; a same-winding exact
    duplicate keeps one copy.  Genuine geometry never loses faces.
    """
    groups: dict[frozenset, list[int]] = {}
    for idx, tri in enumerate(triangles):
        key = frozenset(tuple(round(c, 4) for c in v) for v in tri)
        if len(key) == 3:
            groups.setdefault(key, []).append(idx)

    drop: set[int] = set()
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        pool = [i for i in idxs]
        while len(pool) >= 2:
            base = pool.pop(0)
            n_base = _normalize(_triangle_normal(*triangles[base]))
            mate = None
            for other in pool:
                n_other = _normalize(_triangle_normal(*triangles[other]))
                dot = sum(a * b for a, b in zip(n_base, n_other))
                if dot < -0.999:      # opposing membrane pair
                    mate = other
                    drop.add(base)
                    drop.add(other)
                    break
                if dot > 0.999:       # exact duplicate face
                    mate = other
                    drop.add(other)
                    break
            if mate is not None:
                pool.remove(mate)

    if not drop:
        return triangles
    return [t for i, t in enumerate(triangles) if i not in drop]


def _normalize_triangle_winding_centroid(
    triangles: list[tuple[tuple[float, ...], ...]],
) -> list[tuple[tuple[float, ...], ...]]:
    """Legacy mesh-center heuristic — kept as a fallback when the
    signed-volume assessment can't trust the winding.

    Flips triangles whose normals point toward the mesh bbox center.
    This works on isolated convex shapes near the bottom of the bbox
    (a single cube on the bed) but breaks on compound geometries where
    legitimate overhangs sit above the mesh centroid (T-shape bar
    underside, mushroom cap underside, tabletop underside, etc.) —
    those overhangs' centroids are above the bbox center, so their
    correct -Z normals get a negative dot with the centroid-pointing
    radial and the heuristic flips them, silently deleting the overhang
    from downstream analysis.

    The 2026-05-17 support-volume audit measured this as 25/64 false
    negatives on real overhang geometry; the fix is to use
    :func:`_normalize_triangle_winding` (which checks signed-volume
    first and only falls back to this heuristic for genuinely
    inconsistent input).
    """
    if not triangles:
        return triangles

    xs = [v[0] for tri in triangles for v in tri]
    ys = [v[1] for tri in triangles for v in tri]
    zs = [v[2] for tri in triangles for v in tri]
    center = (
        (min(xs) + max(xs)) / 2.0,
        (min(ys) + max(ys)) / 2.0,
        (min(zs) + max(zs)) / 2.0,
    )

    oriented: list[tuple[tuple[float, ...], ...]] = []
    for tri in triangles:
        normal = _triangle_normal(tri[0], tri[1], tri[2])
        centroid = _triangle_centroid(tri[0], tri[1], tri[2])
        radial = (
            centroid[0] - center[0],
            centroid[1] - center[1],
            centroid[2] - center[2],
        )
        if (
            normal[0] * radial[0]
            + normal[1] * radial[1]
            + normal[2] * radial[2]
        ) < 0.0:
            oriented.append((tri[0], tri[2], tri[1]))
        else:
            oriented.append(tri)

    return oriented


def _normalize_triangle_winding(
    triangles: list[tuple[tuple[float, ...], ...]],
) -> list[tuple[tuple[float, ...], ...]]:
    """Orient triangle winding outward.

    Pipeline: check the signed-volume-to-bbox-volume ratio first.
    For real CAD-exported solids the ratio is large and positive
    (winding consistent + outward) so the function returns the
    triangles unchanged. Inverted-but-consistent winding shows up as
    a large-magnitude NEGATIVE ratio — we flip every triangle once.
    Genuinely inconsistent winding (mixed-orientation triangles, often
    from hand-edited or scanned STLs) partially cancels and the ratio
    falls below :data:`_WINDING_CONSISTENCY_THRESHOLD` — we then fall
    back to :func:`_normalize_triangle_winding_centroid`, the legacy
    heuristic, which is the best we can do without an explicit
    winding-repair pass.

    Before 2026-05-17 this function always ran the centroid heuristic
    on every input — which silently flipped legitimate overhang faces
    on compound geometries (T-shape, mushroom, tabletop, umbrella,
    etc.) whose centroids sat above the mesh midline. The support-
    volume audit measured 25/64 false-negative cases caused by this.
    The signed-volume fast-path fixes those at zero cost on the happy
    path and preserves legacy behavior for genuinely inconsistent
    input.
    """
    if not triangles:
        return triangles

    bbox_vol = _bbox_volume(triangles)
    if bbox_vol < 1e-9:
        # Degenerate / flat input; nothing to assess.
        return _normalize_triangle_winding_centroid(triangles)

    signed_vol = _signed_volume_total(triangles)
    ratio = signed_vol / bbox_vol

    if ratio >= _WINDING_CONSISTENCY_THRESHOLD:
        # Consistent + outward — trust the winding as-is.
        return triangles

    if ratio <= -_WINDING_CONSISTENCY_THRESHOLD:
        # Consistent + inverted — one global flip restores outward.
        return [(tri[0], tri[2], tri[1]) for tri in triangles]

    # Genuinely inconsistent — fall back to the legacy centroid heuristic.
    return _normalize_triangle_winding_centroid(triangles)


def _parse_mesh(
    file_path: str,
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]]]:
    """Parse an STL or OBJ file, returning (triangles, vertices).

    :raises ValueError: If the file is not a supported format or cannot
        be parsed.
    """
    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"File not found: {file_path}")

    ext = path.suffix.lower()
    errors: list[str] = []

    if ext == ".stl":
        triangles, vertices = _parse_stl(path, errors)
    elif ext == ".obj":
        triangles, vertices = _parse_obj(path, errors)
    else:
        raise ValueError(f"Unsupported file type: {ext!r}.  Expected .stl or .obj.")

    if errors:
        raise ValueError(f"Failed to parse mesh: {'; '.join(errors)}")
    if not triangles:
        raise ValueError("Mesh contains no geometry.")

    return triangles, vertices


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------


def _analyze_overhangs(
    triangles: list[tuple[tuple[float, ...], ...]],
    *,
    max_overhang_angle: float | None = None,
    z_min: float | None = None,
    layer_height: float = 0.2,
    normalize_winding: bool = True,
    material: str | None = None,
    overlay: dict[str, Any] | None = None,
) -> OverhangAnalysis:
    """Detect overhanging triangles.

    A triangle is an overhang if its normal points downward (negative Z
    component) and the face angle from vertical exceeds the per-material
    threshold.

    Threshold resolution (first match wins):

    1. ``max_overhang_angle`` explicit kwarg — caller forces a specific
       angle.  Used by tests and by power-users who want a
       single-threshold run regardless of material.
    2. ``material`` + ``overlay`` — per-material lookup in
       ``overlay["overhangs"]["material_limits_deg"][material]``.  The
       Pro+ ``printability_judgment`` overlay supplies SME-tuned
       per-material values (TPU 35°, PLA 50°, …) so warp-prone
       materials get caught earlier and forgiving materials don't get
       false-positive support recommendations.
    3. ``overlay["overhangs"]["default_limit_deg"]`` — overlay-wide
       fallback when the material isn't in ``material_limits_deg``.
    4. ``_OVERHANGS_PUBLIC_DEFAULTS["default_limit_deg"]`` (= 45.0) —
       the universal 45° rule applied to free-tier installs and any
       material the overlay doesn't recognize.

    Soft tier seam: free tier (``overlay`` empty or absent) uses the
    universal 45° floor for every material.  Pro+ overlay supplies
    curated per-material values via the ``printability_judgment``
    overlay's ``overhangs`` block.  Geometry math is identical across
    tiers; only the threshold (which faces get counted) varies.
    """
    cfg = (overlay or {}).get("overhangs") or _OVERHANGS_PUBLIC_DEFAULTS
    if max_overhang_angle is None:
        material_limits = cfg.get("material_limits_deg") or {}
        default_limit = cfg.get("default_limit_deg",
                                _OVERHANGS_PUBLIC_DEFAULTS["default_limit_deg"])
        # Case-insensitive + delimiter-folded lookup.  The overlay JSON
        # mixes UPPERCASE+dash legacy keys ("PLA", "CF-PETG") with
        # lowercase+underscore catalog keys ("pla_plus", "tpu_85a") and
        # callers pass material names in either convention (Kiln tools
        # typically lowercase from materials.json; tests + Pro tools
        # often uppercase).  Without normalization, "tpu" misses the
        # "TPU" overlay entry and falls through to the 45° default —
        # silently disabling the per-material tier seam.  Mirrors the
        # _normalize_material_key helper in
        # kiln_pro/printability_overlay/data_loader.py.
        if material is not None:
            normalized_target = material.strip().lower().replace(
                "-", "_"
            ).replace(" ", "_")
            limit_value = default_limit
            for key, val in material_limits.items():
                if key.strip().lower().replace("-", "_").replace(" ", "_") == normalized_target:
                    limit_value = val
                    break
            max_overhang_angle = float(limit_value)
        else:
            max_overhang_angle = float(default_limit)

    if normalize_winding:
        triangles = _normalize_triangle_winding(triangles)

    total = len(triangles)
    overhang_count = 0
    max_angle = 0.0
    worst_regions: list[dict[str, float]] = []

    for tri in triangles:
        if z_min is not None and _is_bed_supported_triangle(tri, z_min, layer_height):
            continue

        n = _triangle_normal(tri[0], tri[1], tri[2])
        nn = _normalize(n)
        nz = nn[2]

        # Only consider downward-facing normals.
        if nz >= 0:
            continue

        angle_from_down = math.degrees(math.acos(max(-1.0, min(1.0, -nz))))
        overhang_angle = max(0.0, 90.0 - angle_from_down)
        # Epsilon-tolerant comparison so an exact 45.0° slope is
        # classified as an overhang.  math.acos(sqrt(2)/2) returns
        # 45.00000000000001° (one ULP above 45), making
        # `90 - that` land at 44.99999999999999° — below 45.0 by
        # one ULP, so a strict `<` filter skipped real 45° slopes.
        if overhang_angle + 1e-9 < max_overhang_angle:
            continue

        overhang_count += 1
        if overhang_angle > max_angle:
            max_angle = overhang_angle
        centroid = _triangle_centroid(tri[0], tri[1], tri[2])
        if len(worst_regions) < 10:
            worst_regions.append(
                {
                    "x": round(centroid[0], 2),
                    "y": round(centroid[1], 2),
                    "z": round(centroid[2], 2),
                    "angle": round(overhang_angle, 1),
                }
            )

    worst_regions.sort(key=lambda r: r["angle"], reverse=True)

    overhang_pct = (overhang_count / total * 100.0) if total > 0 else 0.0

    return OverhangAnalysis(
        max_overhang_angle=round(max_angle, 1),
        overhang_triangle_count=overhang_count,
        overhang_percentage=round(overhang_pct, 1),
        needs_supports=overhang_count > 0,
        worst_regions=worst_regions[:5],
    )


# Cap the number of sample rays per ``_analyze_thin_walls`` invocation.
# Verified perf on a 100K-triangle hollow box: 1000 rays = 2.55s; 2000
# rays = 5.07s.  1000 keeps every typical analysis under the 5s budget
# while preserving sub-millimetre measurement of the worst wall on any
# mesh dense enough to have one (every sampled face is one independent
# probe; a thin wall covers many faces, so ~1000 samples will land
# multiple probes on it even on huge meshes).
_THIN_WALL_RAY_SAMPLE_CAP: int = 1000

# Cap the number of triangles considered during ray-triangle intersection.
# Above ~150K triangles the Möller-Trumbore broadcast (sample × tri × 3)
# crosses the 200MB intermediate-allocation threshold and gets slow.  We
# subsample the mesh down to this cap for the intersection target.  The
# probe rays still originate from the FULL set of face centroids — the
# subsample is only the target geometry, so distance is still accurate
# (the worst case is a probe missing a sparse far wall; small bias,
# never inflates the measurement).
_THIN_WALL_INTERSECTION_TRI_CAP: int = 100_000

# Per-ray chunk size for the vectorized intersection.  Each chunk
# allocates ~CHUNK × tri_count × 24 bytes of intermediate floats —
# at CHUNK=64 and tri_count=100K that's ~150MB per intermediate
# array (h, s, q), peaking around 600MB.  Verified comfortable on
# typical workstation RAM.
_THIN_WALL_INTERSECTION_CHUNK: int = 64

# Self-hit and parallel-ray epsilons for Möller-Trumbore.
_THIN_WALL_RAY_EPS_DET: float = 1e-6      # parallel-ray determinant cutoff
_THIN_WALL_RAY_EPS_T: float = 1e-3        # self-hit cutoff (mm)
_THIN_WALL_RAY_ORIGIN_OFFSET: float = 1e-4  # mm — nudge origin off the face

# Outward (cavity) ray-cast: minimum max distance considered a "cavity".
# Engraved grooves, debossed text, narrow slots have widths in the
# [0, ~5mm] band; this is the FLOOR — the wall/cavity callers compute a
# bbox-aware cap (half the part's smallest bbox extent) and pick the
# larger of the two, so a 20 mm hollow box's 19 mm cavity registers but
# outward rays from a tiny part still cap at a reasonable distance.
_CAVITY_RAY_MIN_MAX_DIST_MM: float = 10.0  # 10 × default nozzle (0.4 mm) = 4 mm

# Strict-perpendicular dot threshold for ray-cast hits.  A ray that
# enters the body and exits through the opposing wall hits a face whose
# outward normal is approximately aligned (anti-parallel for cavities,
# parallel for walls) with the ray direction — ``|dot| ≈ 1``.  Hits at
# ``|dot| < threshold`` are slanted — typically tessellation artifacts
# (helical thread face hitting the perpendicular end cap from close
# range, ``|dot| ≈ 0.77`` empirically across the audit threaded-rod
# fixtures).  Threshold tuned so legitimate measurements (including
# curved walls on real CAD parts: a 1 mm-radius shell with a 0.5 mm
# wall produces ``|dot| ≈ 0.88``, well above the threshold) pass
# while the thread-cap artifact class is filtered.
_THIN_WALL_MIN_PERPENDICULAR_DOT: float = 0.85

# Sliver-chord floor for the ``min_wall_thickness_mm`` aggregation.
# Chord measurements below this value are physically implausible — the
# smallest commercial FDM nozzle is 0.2 mm (200 µm) and every realistic
# wall is well above the 50 µm floor here.  Sub-floor readings
# typically arise at the boundary of curved surfaces clipped to a
# bounding box (the round-4 topology audit identified this on gyroid
# fixtures, where a 0.5 mm strut produced a 0.05 mm chord reading
# because the curved sheet grazes the box face at a thin angle).
# Filtering them out before computing the per-mesh minimum makes the
# surfaced number reflect actual wall thickness rather than
# measurement artefacts.
#
# If every chord on a mesh is sub-floor (degenerate / non-manifold),
# the filter falls through to the original min so the consumer sees
# the anomaly rather than a silenced no-signal.
_SLIVER_CHORD_FLOOR_MM: float = 0.05



def _compute_mesh_genus(
    triangles: np.ndarray,
    n_components: int,
) -> int:
    """Total topological genus across all components, via Euler char.

    For a closed orientable manifold mesh: χ = V − E + F = 2 − 2g.
    For a disjoint union of ``c`` closed surfaces with genera ``g_i``:
    ``χ_total = sum(2 − 2 g_i) = 2 c − 2 g_total``, so
    ``g_total = c − χ_total / 2``.

    The c-aware formula matters: a cubic strut lattice composed of
    disjoint bars has many components, each genus 0 → ``g_total = 0``.
    Pro's existing ``n_components`` signal already routes those
    through strut semantics.  Genus is the COMPLEMENT signal for
    single-component topologically-complex meshes (curved lattice
    infills — gyroid, Schwarz P / D, and other triply-periodic
    minimal surfaces) where ``n_components == 1`` misses the lattice
    nature and Pro otherwise applies continuous-wall semantics.

    Returns 0 for empty / degenerate meshes.  Returns a non-negative
    integer in the closed-manifold case.  Returns ANOMALOUS values
    (typically 0–2, not matching the physical handle count) on
    non-closed or non-manifold meshes — boundary edges aren't paired
    so V/E/F drift from the closed-formula identity.  Consumers
    should pair this with the ``is_manifold`` flag from the report
    and ignore the genus value when the mesh isn't a closed manifold.

    Pure numpy.  Shares the vertex-dedup + edge-canonicalize scheme
    with ``_label_mesh_components``.  Measured on a 100 k-tri torus:
    ``_compute_mesh_genus`` takes ~235 ms, similar to the existing
    ``_label_mesh_components`` cost (~260 ms) — roughly doubles the
    topology-pass budget rather than the ~50 ms I initially eyeballed
    before benchmarking.
    """
    T = triangles.shape[0]
    if T == 0 or n_components <= 0:
        return 0
    flat = triangles.reshape(-1, 3)
    _, vid = np.unique(flat, axis=0, return_inverse=True)
    V = int(vid.max() + 1) if vid.size > 0 else 0
    tri_verts = vid.reshape(T, 3).astype(np.int64)
    edges = np.stack(
        [tri_verts[:, [0, 1]], tri_verts[:, [1, 2]], tri_verts[:, [2, 0]]],
        axis=1,
    ).reshape(-1, 2)
    edges.sort(axis=1)
    E = int(np.unique(edges, axis=0).shape[0])
    F = int(T)
    chi = V - E + F
    # g_total = c − χ / 2.  Multiply by 2 first to keep integer
    # arithmetic exact; mesh-closed χ is always even so the divide
    # is clean.  Non-closed meshes can produce odd 2*g_total values;
    # the round() handles that gracefully but the negative branch is
    # the real signal that the formula doesn't apply.
    two_g = 2 * n_components - chi
    return int(round(two_g / 2))


def _label_mesh_components(
    triangles: np.ndarray,
) -> np.ndarray:
    """Label triangles by connected component (shared-edge adjacency).

    Triangles that share an edge (two vertices with identical coords)
    belong to the same component.  Vertices are deduplicated by exact
    tuple equality — the same scheme the STL parser uses.

    Returns a ``(T,)`` int array of compact component IDs in ``[0, k)``
    where ``k`` is the component count.  Single-body meshes return
    ``np.zeros(T)``.

    Implementation: ``np.unique``-based vertex dedup + edge sort, then
    union-find on triangle pairs sharing an edge.  Pure numpy; the
    Python loop iterates the union list (one entry per shared edge),
    not per triangle.  ~240 ms on a 100 k-triangle single body.
    """
    T = triangles.shape[0]
    if T == 0:
        return np.empty(0, dtype=np.int64)

    flat = triangles.reshape(-1, 3)  # (3T, 3) — all vertex coords
    _, vid = np.unique(flat, axis=0, return_inverse=True)
    tri_verts = vid.reshape(T, 3).astype(np.int64)

    # Three edges per triangle, with vertex IDs in canonical (u, v) order.
    edges = np.stack(
        [tri_verts[:, [0, 1]], tri_verts[:, [1, 2]], tri_verts[:, [2, 0]]],
        axis=1,
    ).reshape(-1, 2)
    edges.sort(axis=1)

    # Sort all (3T) edges so duplicates are adjacent — duplicates mean
    # two triangles share that edge.
    order = np.lexsort((edges[:, 1], edges[:, 0]))
    sorted_edges = edges[order]
    tri_of_slot = np.repeat(np.arange(T, dtype=np.int64), 3)[order]

    same = (
        (sorted_edges[1:, 0] == sorted_edges[:-1, 0])
        & (sorted_edges[1:, 1] == sorted_edges[:-1, 1])
    )
    union_a = tri_of_slot[:-1][same]
    union_b = tri_of_slot[1:][same]

    parent = np.arange(T, dtype=np.int64)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in zip(union_a.tolist(), union_b.tolist(), strict=True):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    roots = np.fromiter((find(i) for i in range(T)), dtype=np.int64, count=T)
    _, comp_ids = np.unique(roots, return_inverse=True)
    return comp_ids.astype(np.int64)


def _analyze_thin_walls(
    triangles: list[tuple[tuple[float, ...], ...]],
    vertices: list[tuple[float, ...]],
    *,
    nozzle_diameter: float = 0.4,
) -> ThinWallAnalysis:
    """Measure thin walls via per-component vectorized inward ray-casting.

    For each sampled surface triangle, cast a ray from its centroid
    along the inward-pointing normal.  The distance to the first
    intersection with a triangle *in the same connected component* is
    the local wall thickness at that point — insensitive to
    tessellation density (unlike the prior edge-length proxy) and to
    overlap with adjacent bodies (unlike a global first-hit ray-cast).

    Connected-component scoping eliminates the "joint-overlap" artifact
    on lattice / scaffold geometries: each strut is its own component,
    so a ray cast inward from a strut face hits only that strut's
    opposing wall, never an intruding face from a neighbouring strut
    that happens to share volume at the joint.

    Walls with measured thickness below ``nozzle_diameter`` are flagged
    as thin.  ``min_wall_thickness_mm`` is the absolute smallest
    measurement on the mesh; the 0.0 sentinel is reserved for
    measurement failure on degenerate meshes.

    Known limitation: helical features (threaded rods, springs) form a
    single component, so per-component scoping does not help — rays
    cast from helical faces near the rod's end caps can hit the cap
    from very close, returning sub-millimetre tessellation artifacts
    on a structurally-thick part.  Cylindrical hole-bore tessellation
    artifacts on round holes are similarly out-of-scope.  Engraved-
    groove widths are measured separately by
    :func:`_analyze_cavity_widths`.
    """
    total = len(triangles)
    if total < 4:
        # Degenerate input — no closed surface to measure walls on.
        return ThinWallAnalysis(
            min_wall_thickness_mm=0.0,
            thin_wall_count=0,
            thin_wall_percentage=0.0,
            problematic_regions=[],
        )

    tris = np.asarray(triangles, dtype=np.float64)  # (T, 3, 3)
    if tris.ndim != 3 or tris.shape[1] != 3 or tris.shape[2] != 3:
        return ThinWallAnalysis(0.0, 0, 0.0, [])

    v0 = tris[:, 0, :]
    v1 = tris[:, 1, :]
    v2 = tris[:, 2, :]
    edge1 = v1 - v0
    edge2 = v2 - v0

    # Outward normals via cross product; magnitude = 2 × triangle area.
    normals = np.cross(edge1, edge2)
    norm_len = np.linalg.norm(normals, axis=1)
    valid_face = norm_len > 1e-10
    if not valid_face.any():
        return ThinWallAnalysis(0.0, 0, 0.0, [])

    # Sample origins from valid faces only — degenerate ones can't host
    # a probe (no normal to invert).
    centroids_all = (v0 + v1 + v2) / 3.0
    valid_indices = np.where(valid_face)[0]
    valid_centroids = centroids_all[valid_face]
    valid_inward = -(normals[valid_face] / norm_len[valid_face, None])
    valid_areas = norm_len[valid_face] / 2.0

    n_total = len(valid_centroids)
    n_sample = min(_THIN_WALL_RAY_SAMPLE_CAP, n_total)
    if n_total > n_sample:
        # Deterministic area-weighted sampling — same mesh, same answer,
        # regardless of when the analysis runs.
        rng = np.random.default_rng(0)
        probs = valid_areas / valid_areas.sum()
        sample_idx = rng.choice(n_total, size=n_sample, replace=False, p=probs)
    else:
        sample_idx = np.arange(n_total)

    origins = (
        valid_centroids[sample_idx]
        + _THIN_WALL_RAY_ORIGIN_OFFSET * valid_inward[sample_idx]
    )
    directions = valid_inward[sample_idx]

    # Component labelling: identify which connected piece of the mesh
    # each triangle belongs to.  Single-body meshes return all-zeros and
    # behaviour reduces to the prior global ray-cast.
    comp_ids = _label_mesh_components(tris)  # (T,)
    origin_comps = comp_ids[valid_indices[sample_idx]]  # one per ray
    valid_face_comps = comp_ids[valid_indices]          # one per valid target

    exit_dist = np.full(n_sample, np.inf, dtype=np.float64)

    # Iterate the small set of components touched by sampled rays.  For
    # a 1-component mesh this is a single iteration — same work as the
    # prior global cast.  For a lattice of N struts the budget splits
    # across struts; each strut casts only against its own triangles.
    for cid in np.unique(origin_comps):
        ray_mask = origin_comps == cid
        target_mask = valid_face_comps == cid

        # A component with fewer than 4 triangles can't enclose space —
        # rays from it would miss everything anyway.  Skip the cast.
        if target_mask.sum() < 4:
            continue

        # Component-local target subsampling: apply the global cap to
        # each component so a single huge body still benefits from the
        # memory budget, but small components keep all their triangles.
        comp_target_v0 = v0[valid_face][target_mask]
        comp_target_e1 = edge1[valid_face][target_mask]
        comp_target_e2 = edge2[valid_face][target_mask]
        comp_target_areas = valid_areas[target_mask]
        if comp_target_v0.shape[0] > _THIN_WALL_INTERSECTION_TRI_CAP:
            rng_target = np.random.default_rng(1)
            t_probs = comp_target_areas / comp_target_areas.sum()
            t_idx = rng_target.choice(
                comp_target_v0.shape[0],
                size=_THIN_WALL_INTERSECTION_TRI_CAP,
                replace=False,
                p=t_probs,
            )
            comp_target_v0 = comp_target_v0[t_idx]
            comp_target_e1 = comp_target_e1[t_idx]
            comp_target_e2 = comp_target_e2[t_idx]

        comp_origins = origins[ray_mask]
        comp_directions = directions[ray_mask]

        comp_dist = _raycast_min_distances(
            comp_origins,
            comp_directions,
            comp_target_v0,
            comp_target_e1,
            comp_target_e2,
            min_abs_perpendicular_dot=_THIN_WALL_MIN_PERPENDICULAR_DOT,
        )
        exit_dist[ray_mask] = comp_dist

    finite_mask = np.isfinite(exit_dist)
    if not finite_mask.any():
        return ThinWallAnalysis(
            min_wall_thickness_mm=0.0,
            thin_wall_count=0,
            thin_wall_percentage=0.0,
            problematic_regions=[],
        )

    finite_dists = exit_dist[finite_mask]
    # Sliver-chord filter — drop measurements below the physical floor
    # (50 µm; well under any nozzle).  Sub-floor chords typically come
    # from boundary slivers where curved surfaces graze the bounding
    # box at a thin angle (round-4 topology audit, gyroid clipping).
    # Fall through to the raw min when every chord is sub-floor
    # (degenerate mesh) so the consumer sees the anomaly rather than
    # a silenced no-signal.
    non_sliver = finite_dists[finite_dists >= _SLIVER_CHORD_FLOOR_MM]
    if non_sliver.size > 0:
        measured_min = float(non_sliver.min())
    else:
        measured_min = float(finite_dists.min())

    thin_mask = exit_dist < nozzle_diameter
    thin_count = int((thin_mask & finite_mask).sum())
    thin_pct = thin_count / n_sample * 100.0

    # ``min_wall_thickness_mm`` carries the absolute smallest measured
    # wall thickness on the mesh, regardless of the nozzle threshold —
    # the kiln-pro per-material overlay needs this number to compare
    # against material-specific structural floors (e.g. flag a 1.0 mm
    # PLA wall against the 1.2 mm PLA structural floor even when it's
    # above the 0.4 mm nozzle).  The 0.0 sentinel is reserved for
    # measurement failure on degenerate meshes.
    problematic: list[dict[str, float]] = []
    if thin_count > 0:
        thin_dists = exit_dist[thin_mask & finite_mask]
        thin_origins = origins[thin_mask & finite_mask]
        order = np.argsort(thin_dists)[:5]
        for i in order:
            problematic.append(
                {
                    "x": round(float(thin_origins[i, 0]), 2),
                    "y": round(float(thin_origins[i, 1]), 2),
                    "z": round(float(thin_origins[i, 2]), 2),
                    "thickness_mm": round(float(thin_dists[i]), 3),
                }
            )

    return ThinWallAnalysis(
        min_wall_thickness_mm=round(measured_min, 3),
        thin_wall_count=thin_count,
        thin_wall_percentage=round(thin_pct, 1),
        problematic_regions=problematic,
    )


def _analyze_cavity_widths(
    triangles: list[tuple[tuple[float, ...], ...]],
    vertices: list[tuple[float, ...]],
    *,
    nozzle_diameter: float = 0.4,
) -> CavityAnalysis:
    """Measure cavity widths via per-component outward ray-casting.

    Sibling to ``_analyze_thin_walls`` but measures the dimensions of
    cavities cut INTO the mesh (engraved grooves, debossed text, narrow
    slots, pocketed/inset features) rather than the thickness of walls
    forming the mesh.  For each sampled surface triangle, cast a ray
    from its centroid along the OUTWARD normal — restricted to hits
    against triangles in the same connected component, so cavities are
    only measured WITHIN a single body.  Outer-surface rays travel
    into open space, miss everything, and contribute no signal.

    Per-component scoping matters for the same reason it does for
    walls: in a multi-body soup (lattice/scaffold), outward rays from
    one strut's surface can otherwise graze the surface of a
    neighbouring strut at the joint overlap, producing a phantom
    "cavity" reading equal to the strut-half-thickness.  Real cavities
    are by construction inside a single body, so component-scoping
    drops the artifact without affecting any legitimate cavity
    measurement.

    Sub-perimeter cavities (groove widths below the slicer's thinnest
    extrusion) cannot be reproduced during printing and are flagged
    accordingly.
    """
    total = len(triangles)
    if total < 4:
        return CavityAnalysis(
            min_cavity_width_mm=0.0,
            cavity_sample_count=0,
            problematic_regions=[],
        )

    tris = np.asarray(triangles, dtype=np.float64)
    if tris.ndim != 3 or tris.shape[1] != 3 or tris.shape[2] != 3:
        return CavityAnalysis(0.0, 0, [])

    v0 = tris[:, 0, :]
    v1 = tris[:, 1, :]
    v2 = tris[:, 2, :]
    edge1 = v1 - v0
    edge2 = v2 - v0

    normals = np.cross(edge1, edge2)
    norm_len = np.linalg.norm(normals, axis=1)
    valid_face = norm_len > 1e-10
    if not valid_face.any():
        return CavityAnalysis(0.0, 0, [])

    centroids_all = (v0 + v1 + v2) / 3.0
    valid_indices = np.where(valid_face)[0]
    valid_centroids = centroids_all[valid_face]
    # OUTWARD direction (the only direction difference from
    # _analyze_thin_walls).
    valid_outward = normals[valid_face] / norm_len[valid_face, None]
    valid_areas = norm_len[valid_face] / 2.0

    n_total = len(valid_centroids)
    n_sample = min(_THIN_WALL_RAY_SAMPLE_CAP, n_total)
    if n_total > n_sample:
        rng = np.random.default_rng(0)
        probs = valid_areas / valid_areas.sum()
        sample_idx = rng.choice(n_total, size=n_sample, replace=False, p=probs)
    else:
        sample_idx = np.arange(n_total)

    origins = (
        valid_centroids[sample_idx]
        + _THIN_WALL_RAY_ORIGIN_OFFSET * valid_outward[sample_idx]
    )
    directions = valid_outward[sample_idx]

    # Component labelling — same approach as walls (single-body meshes
    # take one iteration; lattices loop per-strut).
    comp_ids = _label_mesh_components(tris)
    origin_comps = comp_ids[valid_indices[sample_idx]]
    valid_face_comps = comp_ids[valid_indices]

    hit_dist = np.full(n_sample, np.inf, dtype=np.float64)

    for cid in np.unique(origin_comps):
        ray_mask = origin_comps == cid
        target_mask = valid_face_comps == cid
        if target_mask.sum() < 4:
            continue

        comp_target_v0 = v0[valid_face][target_mask]
        comp_target_e1 = edge1[valid_face][target_mask]
        comp_target_e2 = edge2[valid_face][target_mask]
        comp_target_areas = valid_areas[target_mask]
        if comp_target_v0.shape[0] > _THIN_WALL_INTERSECTION_TRI_CAP:
            rng_target = np.random.default_rng(1)
            t_probs = comp_target_areas / comp_target_areas.sum()
            t_idx = rng_target.choice(
                comp_target_v0.shape[0],
                size=_THIN_WALL_INTERSECTION_TRI_CAP,
                replace=False,
                p=t_probs,
            )
            comp_target_v0 = comp_target_v0[t_idx]
            comp_target_e1 = comp_target_e1[t_idx]
            comp_target_e2 = comp_target_e2[t_idx]

        comp_origins = origins[ray_mask]
        comp_directions = directions[ray_mask]

        comp_dist = _raycast_min_distances(
            comp_origins,
            comp_directions,
            comp_target_v0,
            comp_target_e1,
            comp_target_e2,
            min_abs_perpendicular_dot=_THIN_WALL_MIN_PERPENDICULAR_DOT,
        )
        hit_dist[ray_mask] = comp_dist

    # Cap at the cavity-distance threshold — anything longer is "ray
    # exited the part into open space," not a cavity measurement.
    # The cap is the LARGER of the static floor (10 × default nozzle,
    # ~4 mm) and the mesh's bbox-aware ceiling (half the smallest bbox
    # extent).  A 20 mm hollow box gets a 10 mm cap so a 9 mm internal
    # cavity registers; a tiny 3 mm part stays at the floor so outward
    # rays from outer faces don't accidentally pick up build-volume
    # artifacts at extreme distances.
    bbox_min = tris[valid_face].reshape(-1, 3).min(axis=0)
    bbox_max = tris[valid_face].reshape(-1, 3).max(axis=0)
    bbox_extent = float((bbox_max - bbox_min).min())
    cavity_max_dist = max(_CAVITY_RAY_MIN_MAX_DIST_MM, bbox_extent / 2.0)
    cavity_mask = (hit_dist < cavity_max_dist) & np.isfinite(hit_dist)
    cavity_count = int(cavity_mask.sum())
    if cavity_count == 0:
        return CavityAnalysis(
            min_cavity_width_mm=0.0,
            cavity_sample_count=0,
            problematic_regions=[],
        )

    cavity_dists = hit_dist[cavity_mask]
    cavity_origins = origins[cavity_mask]
    min_cavity = float(cavity_dists.min())

    problematic: list[dict[str, float]] = []
    order = np.argsort(cavity_dists)[:5]
    for i in order:
        problematic.append(
            {
                "x": round(float(cavity_origins[i, 0]), 2),
                "y": round(float(cavity_origins[i, 1]), 2),
                "z": round(float(cavity_origins[i, 2]), 2),
                "width_mm": round(float(cavity_dists[i]), 3),
            }
        )

    return CavityAnalysis(
        min_cavity_width_mm=round(min_cavity, 3),
        cavity_sample_count=cavity_count,
        problematic_regions=problematic,
    )


def _raycast_min_distances(
    origins: np.ndarray,
    directions: np.ndarray,
    v0: np.ndarray,
    edge1: np.ndarray,
    edge2: np.ndarray,
    *,
    min_abs_perpendicular_dot: float | None = None,
) -> np.ndarray:
    """Vectorized Möller-Trumbore ray-triangle intersection.

    For each ray, return the minimum positive hit distance against the
    triangle set (``inf`` for rays that miss every triangle).

    ``min_abs_perpendicular_dot`` filters hits whose
    ``|dot(ray_direction, hit_face_outward_normal)|`` falls below the
    threshold — i.e. rays that hit the target face at a slanted angle
    rather than approximately head-on.  For a legitimate wall
    measurement (ray exits through the opposite wall), the dot is
    ``≈ +1``; for a cavity measurement (ray exits the cavity through
    its opposite wall), the dot is ``≈ -1``.  Both pass the
    ``|dot| >= threshold`` test.  Grazing artifacts — e.g. a helical
    thread face hitting the perpendicular end cap from very close
    range — sit at ``|dot| ≈ 0.77`` and get filtered.  None (the
    default) skips the filter for back-compat with cavity callers
    that don't need it.
    """
    n_rays = origins.shape[0]
    min_dist = np.full(n_rays, np.inf, dtype=np.float64)
    chunk = _THIN_WALL_INTERSECTION_CHUNK

    if min_abs_perpendicular_dot is not None:
        # Per-target outward normals for the angle filter.  Re-computed
        # from edge1 × edge2 (already-implied geometry) so callers don't
        # have to thread normals through the API.
        target_normals = np.cross(edge1, edge2)
        target_norm_len = np.linalg.norm(target_normals, axis=1)
        target_normals = np.where(
            target_norm_len[:, None] > 1e-10,
            target_normals / np.where(
                target_norm_len[:, None] > 1e-10, target_norm_len[:, None], 1.0
            ),
            0.0,
        )

    for start in range(0, n_rays, chunk):
        end = min(start + chunk, n_rays)
        o = origins[start:end]                       # (C, 3)
        d = directions[start:end]                    # (C, 3)

        h = np.cross(d[:, None, :], edge2[None, :, :])              # (C, T, 3)
        a = np.einsum("tj,ctj->ct", edge1, h)                       # (C, T)
        a_safe = np.where(np.abs(a) < _THIN_WALL_RAY_EPS_DET, 1.0, a)
        f = 1.0 / a_safe

        s = o[:, None, :] - v0[None, :, :]                          # (C, T, 3)
        u = f * np.einsum("ctj,ctj->ct", s, h)                      # (C, T)

        q = np.cross(s, edge1[None, :, :])                          # (C, T, 3)
        v = f * np.einsum("cj,ctj->ct", d, q)                       # (C, T)

        t = f * np.einsum("tj,ctj->ct", edge2, q)                   # (C, T)

        hit = (
            (np.abs(a) >= _THIN_WALL_RAY_EPS_DET)
            & (u >= 0.0)
            & (u <= 1.0)
            & (v >= 0.0)
            & ((u + v) <= 1.0)
            & (t > _THIN_WALL_RAY_EPS_T)
        )

        if min_abs_perpendicular_dot is not None:
            # |dot(direction, target_normal)| ≥ threshold — keeps
            # head-on hits (legitimate wall/cavity), drops slanted ones.
            dn = np.einsum("cj,tj->ct", d, target_normals)
            hit = hit & (np.abs(dn) >= min_abs_perpendicular_dot)

        t_masked = np.where(hit, t, np.inf)
        min_dist[start:end] = t_masked.min(axis=1)

    return min_dist


@dataclass
class _DownwardRegion:
    """One edge-connected patch of support-needing (≥45°) downward faces.

    A patch prints without supports when every sampled deck point
    satisfies at least one of two physical mechanisms:

    - **Two-sided bridging**: some straight chord through the point has
      BOTH endpoints on supported boundary (material continuing below
      the edge) and is ≤ the self-supporting bridge limit.  Slicers
      pick the bridge direction, so a 2 × 20 mm cavity ceiling is a
      2 mm bridge, not a 20 mm one.
    - **Lateral reach**: the point is within a couple of extrusion
      widths of supported boundary — each extruded line anchors to the
      previous one, so a narrow one-side-anchored band (a debossed
      logo's recess ceiling, a small flange lip) closes cleanly even
      though no two-sided chord exists.

    ``needs_supports`` is True when some deck point has neither
    mechanism — a wide cantilever, a dome bottom, an island, or a
    genuinely long bridge.

    ``span_mm`` is the reported worst effective span: per point the
    smaller of (best two-sided chord, 2 × distance-to-supported-
    boundary), maxed over points; ``inf`` for islands with no
    supported boundary at all (callers fall back to
    ``bbox_span_mm``, the legacy conservative XY-bounding-box
    measure).
    """

    triangle_indices: list[int]
    flat_indices: list[int]
    span_mm: float
    needs_supports: bool
    bbox_span_mm: float
    #: Portion of span_mm that genuinely crosses air (see
    #: BridgingAnalysis.max_free_air_span_mm).  Zero for regions
    #: exempt by mechanism.
    free_air_span_mm: float = 0.0


# Membership limit for downward-region aggregation: faces overhanging
# 45° or more (nz ≤ -cos(45°)).  Shallower faces build as ordinary
# stepped layers and can anchor a bridge, so they terminate a region.
_DOWNWARD_REGION_NZ_LIMIT: float = -math.cos(math.radians(45.0))

# Flat-ceiling limit — matches the bridge-candidate filter below.
_BRIDGE_FLAT_NZ_LIMIT: float = -0.9

# Chord probing: 12 directions = 15° steps over the half-circle.  For a
# strip crossed at the worst mis-alignment (7.5°) the measured chord
# overstates the true width by 1/cos(7.5°) ≈ 0.9%, well inside the
# tolerance of the 10 mm rule it feeds.
_BRIDGE_CHORD_DIRECTIONS: int = 12

# Per-region cap on sampled deck points.  Regions are usually tiny;
# the cap bounds pathological meshes (a 10K-facet ceiling) without
# losing the max — a strip's span is position-independent and a disc's
# worst point is interior, which stride sampling still hits.
_BRIDGE_SPAN_SAMPLE_CAP: int = 200

# Ceilings this close to the bed (three 0.2 mm layers) are exempt
# from the needs-supports verdict: the bed itself catches any sag, a
# support could not fit in the gap, and the empirically proven QR
# workflow prints an 84 mm pocket ceiling at 0.5 mm.  Deliberately
# NOT extended further: at millimeter-plus gaps a fused sag starts
# ruining functional clearances rather than cosmetics.
_BED_PROXIMATE_CEILING_MM: float = 0.61

# A boundary edge counts as supported when the neighbouring
# non-region triangle extends below the edge by more than this (mm) —
# i.e. there is a wall descending from the bridge deck for the
# filament to land on.  Neighbours that only rise (the side face of
# the very slab whose underside we are measuring, the continuation of
# a dome) are free edges: bridging toward them has nothing to anchor
# on.
_BRIDGE_SUPPORTED_EDGE_DROP_MM: float = 1e-3

# Two-sided bridge spans up to this print reliably without supports —
# the same universal 10 mm rule the public scoring has always used
# (the pro overlay layers per-material limits on top).
_MAX_SELF_SUPPORTING_BRIDGE_MM: float = 10.0

# One-sided lateral reach: an extruded line anchors to the line laid
# next to it, so deck points within a few extrusion widths (0.4 mm
# nozzle baseline) of supported boundary close cleanly even with no
# opposing anchor.  Beyond this a free edge droops and needs support.
_MAX_LATERAL_REACH_MM: float = 2.0


def _analyze_downward_regions(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    *,
    layer_height: float = 0.2,
) -> list[_DownwardRegion]:
    """Aggregate support-needing downward faces into connected regions
    and measure each region's worst bridgeable span (see
    :class:`_DownwardRegion`)."""

    def _vkey(v: tuple[float, ...]) -> tuple[int, int, int]:
        return (
            int(round(v[0] * 1_000_000)),
            int(round(v[1] * 1_000_000)),
            int(round(v[2] * 1_000_000)),
        )

    def _ekey(a: tuple[float, ...], b: tuple[float, ...]) -> tuple:
        ka, kb = _vkey(a), _vkey(b)
        return (ka, kb) if ka < kb else (kb, ka)

    bed_threshold = _bed_threshold_z(z_min, layer_height)

    member_idx: list[int] = []
    is_flat: list[bool] = []
    for i, tri in enumerate(triangles):
        if _is_bed_supported_triangle(tri, z_min, layer_height):
            continue
        centroid = _triangle_centroid(tri[0], tri[1], tri[2])
        if centroid[2] <= bed_threshold:
            continue
        nn = _normalize(_triangle_normal(tri[0], tri[1], tri[2]))
        if nn[2] > _DOWNWARD_REGION_NZ_LIMIT:
            continue
        member_idx.append(i)
        is_flat.append(nn[2] <= _BRIDGE_FLAT_NZ_LIMIT)

    if not member_idx:
        return []

    member_set = set(member_idx)

    # Union-find over region members by shared edge.
    parent = list(range(len(member_idx)))

    def _find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    edge_owner: dict[tuple, int] = {}
    for local, gi in enumerate(member_idx):
        tri = triangles[gi]
        for e in (
            _ekey(tri[0], tri[1]),
            _ekey(tri[1], tri[2]),
            _ekey(tri[2], tri[0]),
        ):
            if e in edge_owner:
                _union(local, edge_owner[e])
            else:
                edge_owner[e] = local

    # Full-mesh edge map so boundary edges can be classified by their
    # non-region neighbour (supported wall below vs free rise above).
    mesh_edges: dict[tuple, list[int]] = {}
    for i, tri in enumerate(triangles):
        for e in (
            _ekey(tri[0], tri[1]),
            _ekey(tri[1], tri[2]),
            _ekey(tri[2], tri[0]),
        ):
            mesh_edges.setdefault(e, []).append(i)

    # Group members per region root.
    groups: dict[int, list[int]] = {}
    for local in range(len(member_idx)):
        groups.setdefault(_find(local), []).append(local)

    # Lazy full-mesh array for the material-directly-below probe (only
    # built if some deck point fails both the chord and lateral tests).
    _tris_arr: np.ndarray | None = None

    def _solid_directly_below(cx: float, cy: float, cz: float) -> bool:
        """True when the point one-and-a-half layers under the deck is
        inside the solid — the deck face rests on material below (a
        boolean seam, a feature starting flush on a floor) and prints
        as an ordinary layer bond even though the mesh topology reads
        it as an unanchored underside."""
        nonlocal _tris_arr
        if _tris_arr is None:
            _tris_arr = np.asarray(triangles, dtype=np.float64)
        probe = np.array(
            [[cx, cy, cz - 1.5 * layer_height]], dtype=np.float64,
        )
        try:
            return bool(_points_inside_mesh(probe, _tris_arr)[0])
        except (ValueError, IndexError):
            return False

    regions: list[_DownwardRegion] = []
    for locals_ in groups.values():
        tri_indices = [member_idx[lo] for lo in locals_]
        flat_indices = [member_idx[lo] for lo in locals_ if is_flat[lo]]

        # Region XY bbox — the legacy conservative span, kept for
        # unbridgeable regions.
        xs = [v[0] for gi in tri_indices for v in triangles[gi]]
        ys = [v[1] for gi in tri_indices for v in triangles[gi]]
        bbox_span = max(max(xs) - min(xs), max(ys) - min(ys))

        if not flat_indices:
            regions.append(_DownwardRegion(
                triangle_indices=tri_indices,
                flat_indices=[],
                span_mm=0.0,
                needs_supports=False,
                bbox_span_mm=round(bbox_span, 2),
                free_air_span_mm=0.0,
            ))
            continue

        # Boundary edges of the region: edges owned by exactly one
        # region triangle.  Each carries a supported flag from its
        # non-region neighbours.
        edge_count: dict[tuple, int] = {}
        edge_verts: dict[tuple, tuple] = {}
        for gi in tri_indices:
            tri = triangles[gi]
            for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
                e = _ekey(a, b)
                edge_count[e] = edge_count.get(e, 0) + 1
                edge_verts[e] = (a, b)

        seg_a: list[tuple[float, float]] = []
        seg_b: list[tuple[float, float]] = []
        seg_supported: list[bool] = []
        for e, count in edge_count.items():
            if count != 1:
                continue
            a, b = edge_verts[e]
            edge_z = min(a[2], b[2])
            supported = False
            for ni in mesh_edges.get(e, []):
                if ni in member_set:
                    continue
                ntri = triangles[ni]
                n_min_z = min(v[2] for v in ntri)
                if n_min_z < edge_z - _BRIDGE_SUPPORTED_EDGE_DROP_MM:
                    supported = True
                    break
            seg_a.append((a[0], a[1]))
            seg_b.append((b[0], b[1]))
            seg_supported.append(supported)

        if not seg_a:
            regions.append(_DownwardRegion(
                triangle_indices=tri_indices,
                flat_indices=flat_indices,
                span_mm=float("inf"),
                needs_supports=True,
                bbox_span_mm=round(bbox_span, 2),
                free_air_span_mm=round(bbox_span, 2),
            ))
            continue

        pa = np.asarray(seg_a, dtype=np.float64)
        pb = np.asarray(seg_b, dtype=np.float64)
        d_seg = pb - pa
        supported_arr = np.asarray(seg_supported, dtype=bool)
        sup_a = pa[supported_arr]
        sup_d = d_seg[supported_arr]
        sup_len_sq = np.maximum((sup_d ** 2).sum(axis=1), 1e-18)

        samples = flat_indices
        if len(samples) > _BRIDGE_SPAN_SAMPLE_CAP:
            stride = len(samples) / _BRIDGE_SPAN_SAMPLE_CAP
            samples = [
                samples[int(k * stride)]
                for k in range(_BRIDGE_SPAN_SAMPLE_CAP)
            ]

        region_span = 0.0
        region_free_air = 0.0
        region_needs_supports = False
        for gi in samples:
            tri = triangles[gi]
            cx, cy, _cz = _triangle_centroid(tri[0], tri[1], tri[2])

            # Two-sided bridge: shortest chord through the point whose
            # nearest boundary hit on BOTH sides is a supported edge.
            best_chord = float("inf")
            for k in range(_BRIDGE_CHORD_DIRECTIONS):
                theta = math.pi * k / _BRIDGE_CHORD_DIRECTIONS
                ux, uy = math.cos(theta), math.sin(theta)
                # 2D ray/segment intersection, both directions at once:
                # solve p + t*u = a + s*d for each boundary segment.
                denom = ux * d_seg[:, 1] - uy * d_seg[:, 0]
                with np.errstate(divide="ignore", invalid="ignore"):
                    w_x = pa[:, 0] - cx
                    w_y = pa[:, 1] - cy
                    s = (w_x * uy - w_y * ux) / denom
                    t = np.where(
                        np.abs(ux) > np.abs(uy),
                        (pa[:, 0] + s * d_seg[:, 0] - cx) / ux,
                        (pa[:, 1] + s * d_seg[:, 1] - cy) / uy,
                    )
                hit = (np.abs(denom) > 1e-12) & (s >= -1e-9) & (s <= 1.0 + 1e-9)
                fwd = hit & (t > 1e-9)
                bwd = hit & (t < -1e-9)
                if not fwd.any() or not bwd.any():
                    continue
                t_f = np.where(fwd, t, np.inf)
                t_b = np.where(bwd, -t, np.inf)
                i_f = int(np.argmin(t_f))
                i_b = int(np.argmin(t_b))
                if not (supported_arr[i_f] and supported_arr[i_b]):
                    continue
                chord = float(t_f[i_f] + t_b[i_b])
                if chord < best_chord:
                    best_chord = chord

            # One-sided lateral reach: distance to the nearest
            # supported boundary segment.
            if len(sup_a):
                rel = np.array([cx, cy]) - sup_a
                proj = np.clip(
                    (rel * sup_d).sum(axis=1) / sup_len_sq, 0.0, 1.0,
                )
                closest = sup_a + proj[:, None] * sup_d
                diff = np.array([cx, cy]) - closest
                d_sup = float(np.sqrt((diff ** 2).sum(axis=1).min()))
            else:
                d_sup = float("inf")

            # Span selection mirrors the print mechanism: a true
            # two-sided bridge reports its chord; a point that only
            # closes by lateral reach reports twice its distance to
            # the anchor (the width of the band being closed); a
            # point with neither needs supports and reports the
            # chord when one exists (the gap supports must fill).
            if best_chord <= _MAX_SELF_SUPPORTING_BRIDGE_MM:
                point_span = best_chord
                point_free_air = best_chord
            elif d_sup <= _MAX_LATERAL_REACH_MM:
                point_span = min(best_chord, 2.0 * d_sup)
                point_free_air = point_span
            elif _solid_directly_below(cx, cy, _cz):
                point_span = 0.0
                point_free_air = 0.0
            elif _cz - z_min <= _BED_PROXIMATE_CEILING_MM:
                # First-layers recess (a bottom QR pocket, a debossed
                # logo underside): the ceiling bridges with the bed a
                # hair beneath it, so any sag lands harmlessly on the
                # plate instead of drooping into air — the coaster
                # recipe prints an 84 mm pocket ceiling this way and
                # scans.  Supports could not even fit in the gap.  The
                # span still reports; only the verdict is lifted.
                point_span = (
                    best_chord if not math.isinf(best_chord) else 0.0
                )
                point_free_air = 0.0
            else:
                region_needs_supports = True
                point_span = (
                    best_chord if not math.isinf(best_chord)
                    else 2.0 * d_sup
                )
                point_free_air = point_span
            if point_span > region_span:
                region_span = point_span
            if point_free_air > region_free_air:
                region_free_air = point_free_air

        regions.append(_DownwardRegion(
            triangle_indices=tri_indices,
            flat_indices=flat_indices,
            span_mm=(
                region_span if math.isinf(region_span)
                else round(region_span, 2)
            ),
            needs_supports=region_needs_supports,
            bbox_span_mm=round(bbox_span, 2),
            free_air_span_mm=(
                region_free_air if math.isinf(region_free_air)
                else round(region_free_air, 2)
            ),
        ))

    return regions


def _analyze_bridging(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    *,
    layer_height: float = 0.2,
    normalize_winding: bool = True,
    precomputed_regions: list[_DownwardRegion] | None = None,
) -> BridgingAnalysis:
    """Detect unsupported horizontal spans (bridges).

    Identifies triangles with normals pointing nearly straight down
    that are above the first layer (not bed-touching), then
    aggregates them into connected regions before measuring.

    ``max_bridge_length_mm`` is the worst bridgeable span across the
    connected bridge regions — for each region, the shortest supported
    chord at its widest deck point (see :class:`_DownwardRegion`).
    Slicers pick the bridge direction, so the span that matters is the
    distance to the nearest opposing anchors, not the region's overall
    extent: a 2 × 20 mm recess ceiling is a 2 mm bridge, a narrow
    annular relief band is its band width.  The prior XY-bounding-box
    measure charged both as 20 mm+ "long bridges", failing perfectly
    printable parts (and, through the pro overlay's per-material
    bridging limit, deducting a second time for the same phantom span).

    A region needs supports when some deck point can neither bridge
    (no supported chord ≤ 10 mm) nor be reached laterally from a
    supported edge (see :class:`_DownwardRegion`) — wide cantilevers,
    dome bottoms, islands, genuinely long bridges.  Islands with no
    supported boundary at all report the conservative XY-bounding-box
    span.

    ``bridge_count`` is the per-TRIANGLE count of near-flat
    (normal z ≤ -0.9) ceiling faces (back-compat with the public
    scoring formula's ``min(15, 5 + bridge_count)``).
    """
    if normalize_winding:
        triangles = _normalize_triangle_winding(triangles)

    regions = (
        precomputed_regions
        if precomputed_regions is not None
        else _analyze_downward_regions(
            triangles, z_min, layer_height=layer_height,
        )
    )

    bridge_count = sum(len(r.flat_indices) for r in regions)
    if bridge_count == 0:
        return BridgingAnalysis(
            max_bridge_length_mm=0.0,
            bridge_count=0,
            needs_supports_for_bridges=False,
            max_free_air_span_mm=0.0,
        )

    max_bridge_len = 0.0
    max_free_air = 0.0
    needs_supports = False
    for region in regions:
        if not region.flat_indices:
            continue
        if region.needs_supports:
            needs_supports = True
        span = region.span_mm
        if math.isinf(span):
            span = region.bbox_span_mm
        if span > max_bridge_len:
            max_bridge_len = span
        free_air = region.free_air_span_mm
        if math.isinf(free_air):
            free_air = region.bbox_span_mm
        if free_air > max_free_air:
            max_free_air = free_air

    return BridgingAnalysis(
        max_bridge_length_mm=round(max_bridge_len, 2),
        bridge_count=bridge_count,
        needs_supports_for_bridges=needs_supports,
        max_free_air_span_mm=round(max_free_air, 2),
    )


def _analyze_bed_adhesion(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    bbox: dict[str, float],
    *,
    layer_height: float = 0.2,
) -> BedAdhesionAnalysis:
    """Estimate bed contact area.

    Sums the area of triangles whose vertices are all within one layer
    height of the bottom of the mesh.
    """
    contact_threshold = z_min + layer_height
    contact_area = 0.0

    for tri in triangles:
        # All three vertices must be near Z_min.
        if tri[0][2] <= contact_threshold and tri[1][2] <= contact_threshold and tri[2][2] <= contact_threshold:
            contact_area += _triangle_area(tri[0], tri[1], tri[2])

    # Bounding box footprint (XY projection).
    footprint = (bbox["x_max"] - bbox["x_min"]) * (bbox["y_max"] - bbox["y_min"])
    contact_pct = (contact_area / footprint * 100.0) if footprint > 0 else 0.0

    if contact_pct > 30.0:
        risk = "low"
    elif contact_pct > 10.0:
        risk = "medium"
    else:
        risk = "high"

    return BedAdhesionAnalysis(
        contact_area_mm2=round(contact_area, 2),
        contact_percentage=round(contact_pct, 1),
        adhesion_risk=risk,
    )


# Slicers bridge a two-sided gap reliably only up to roughly this
# span; a wider gap sags even when it is genuinely anchored on both
# sides, so the bridge-substitution downgrade must not fire past it.
_MAX_RELIABLE_BRIDGE_SPAN_MM = 30.0


def _points_inside_mesh(
    points: np.ndarray,
    triangles: np.ndarray,
) -> np.ndarray:
    """Even-odd point-in-mesh test for a batch of points, ray cast
    straight down.

    Casts one near-vertical downward ray per point and counts forward
    triangle crossings (Möller-Trumbore).  An odd count means the point
    lies inside the solid the mesh bounds.

    Even-odd parity is robust to ugly triangulation: a fan-triangulated
    concave face whose triangles overlap and spill outside the polygon
    still yields the correct parity, because the spill cancels.  A
    per-triangle footprint or cross-section test does NOT — one large
    face spans the very gap it bounds.

    The downward direction matters: it counts only material *below* each
    point, so an open top face coplanar with a probe point (an idealized
    overhang slab resting on its supports) cannot corrupt the parity.
    Parity stays approximate on meshes that are non-closed below the
    probe plane; the caller uses the result as a conservative heuristic,
    so that is acceptable.

    ``points`` is ``(P, 3)``, ``triangles`` is ``(T, 3, 3)``.  Returns a
    ``(P,)`` bool array.
    """
    P = points.shape[0]
    T = triangles.shape[0]
    if P == 0 or T == 0:
        return np.zeros(P, dtype=bool)

    # Near-vertical, cast downward, with a slight off-axis tilt.  Down
    # excludes everything above the probe plane (the overhang surface
    # and any top faces coplanar with it); the tilt avoids coplanar
    # degeneracies against the axis-aligned faces typical of 3D models.
    d = np.array([0.0511, 0.0337, -1.0], dtype=np.float64)

    v0 = triangles[:, 0, :]                       # (T, 3)
    e1 = triangles[:, 1, :] - v0                  # (T, 3)
    e2 = triangles[:, 2, :] - v0                  # (T, 3)
    h = np.cross(d, e2)                           # (T, 3)
    a = np.einsum("tj,tj->t", e1, h)              # (T,)
    parallel = np.abs(a) < 1e-9
    inv_a = np.where(parallel, 0.0, 1.0 / np.where(parallel, 1.0, a))

    crossings = np.zeros(P, dtype=np.int64)
    chunk = max(1, 1_000_000 // T)                # bound (chunk, T) memory
    for start in range(0, P, chunk):
        pts = points[start:start + chunk]         # (C, 3)
        s = pts[:, None, :] - v0[None, :, :]      # (C, T, 3)
        u = np.einsum("ctj,tj->ct", s, h) * inv_a[None, :]
        q = np.cross(s, e1[None, :, :])           # (C, T, 3)
        v = np.einsum("ctj,j->ct", q, d) * inv_a[None, :]
        t = np.einsum("ctj,tj->ct", q, e2) * inv_a[None, :]
        hit = (
            (~parallel[None, :])
            & (u >= 0.0) & (u <= 1.0)
            & (v >= 0.0) & (u + v <= 1.0)
            & (t > 1e-7)
        )
        crossings[start:start + chunk] = hit.sum(axis=1)
    return (crossings % 2) == 1


def _likely_bridge_substituted(
    overhang_triangles: list[tuple[tuple[float, ...], ...]],
    all_triangles: list[tuple[tuple[float, ...], ...]],
) -> bool:
    """Whether a near-horizontal overhang is a genuine spannable bridge
    a slicer fills on its own — rather than a cantilever or island that
    needs supports.

    A bridge is anchored on TWO opposing sides with an open gap between
    them (a tabletop between legs, a Pi-shape, a picture-frame interior).
    A cantilever is anchored on ONE side — the overhang extends past its
    support (a T-arm, an L) — and physically cannot be bridged: there is
    no second anchor to pull the filament across.  An island is anchored
    nowhere.  Only the true two-sided bridge can safely skip supports.

    Geometric test: probe a grid of points one probe-depth below the
    overhang underside and classify each inside/outside the solid via
    even-odd ray casting.  Per axis, the overhang bridges when solid
    fills the band straddling BOTH footprint edges and the centre band
    is open — the open centre is the gap the slicer bridges.  A central
    post fills the centre, not the edges; a one-sided arm reaches one
    edge only; a solid base fills everything (no gap); each fails.  An
    axis whose span exceeds the reliable bridging length also fails — a
    gap that wide sags even when it is truly two-sided.

    The probe band extends a margin past each footprint edge so an
    anchor abutting the overhang from outside its footprint (the legs of
    a Pi-shape touch the underside edges, not its interior) still
    registers.

    The caller still gates this behind a near-horizontal overhang check
    before acting on it.
    """
    if not overhang_triangles or not all_triangles:
        return False

    oxs = [v[0] for tri in overhang_triangles for v in tri]
    oys = [v[1] for tri in overhang_triangles for v in tri]
    ox_min, ox_max = min(oxs), max(oxs)
    oy_min, oy_max = min(oys), max(oys)
    span_x, span_y = ox_max - ox_min, oy_max - oy_min
    if span_x <= 1e-6 or span_y <= 1e-6:
        return False

    overhang_z = min(v[2] for tri in overhang_triangles for v in tri)
    part_z_min = min(v[2] for tri in all_triangles for v in tri)
    overhang_height = overhang_z - part_z_min
    if overhang_height <= 1e-6:
        return False  # overhang sits on the bed — nothing to bridge over

    tris = np.asarray(all_triangles, dtype=np.float64)
    if tris.ndim != 3 or tris.shape[1:] != (3, 3):
        return False

    # Probe plane: just below the overhang underside, where a real
    # anchor (a wall rising to the overhang) is still solid.  A sub-mm
    # jitter keeps probe points off the axis-aligned faces of the model.
    depth = min(1.0, max(0.25, 0.05 * overhang_height))
    probe_z = overhang_z - depth + 0.0117
    if probe_z <= part_z_min:
        return False  # overhang too close to the bed to probe under

    # Margin past each footprint edge so an edge-abutting anchor registers.
    margin = 3.0

    def _axis_bridges(lo: float, hi: float, span: float, axis: int) -> bool:
        if span > _MAX_RELIABLE_BRIDGE_SPAN_MM:
            return False  # too wide — a true gap this long still sags
        cross_lo, cross_hi = (oy_min, oy_max) if axis == 0 else (ox_min, ox_max)
        cross_span = cross_hi - cross_lo
        # ~1mm steps along the bridged axis; the cross axis is inset so
        # every sample sits strictly inside the footprint, yet dense
        # enough to catch an off-centre anchor (a corner leg).
        n_main = max(9, min(80, int(span + 2 * margin) + 1))
        n_cross = max(3, min(20, int(cross_span) + 1))
        main = np.linspace(lo - margin, hi + margin, n_main)
        cross = np.linspace(
            cross_lo + 0.05 * cross_span, cross_hi - 0.05 * cross_span, n_cross
        )
        mg, cg = np.meshgrid(main, cross, indexing="ij")
        pts = np.empty((mg.size, 3), dtype=np.float64)
        pts[:, axis] = mg.ravel() + 0.0131
        pts[:, 1 - axis] = cg.ravel() + 0.0173
        pts[:, 2] = probe_z
        inside = _points_inside_mesh(pts, tris).reshape(mg.shape)
        # A footprint edge is anchored if ANY cross-axis sample in its
        # band is solid; the centre is open if NO sample there is.
        col_solid = inside.any(axis=1)
        centre = lo + span / 2.0
        left_anchored = bool(col_solid[np.abs(main - lo) <= margin].any())
        right_anchored = bool(col_solid[np.abs(main - hi) <= margin].any())
        mid_band = np.abs(main - centre) <= span / 6.0
        mid_open = mid_band.any() and not bool(col_solid[mid_band].any())
        return left_anchored and right_anchored and mid_open

    return (
        _axis_bridges(ox_min, ox_max, span_x, axis=0)
        or _axis_bridges(oy_min, oy_max, span_y, axis=1)
    )


def _analyze_supports(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    *,
    max_overhang_angle: float = 45.0,
    layer_height: float = 0.2,
    normalize_winding: bool = True,
    bbox: dict[str, float] | None = None,
) -> SupportAnalysis:
    """Estimate support volume.

    For each overhang triangle, projects it downward to the build plate
    and estimates the support column volume as area x height.

    The returned ``estimated_support_volume_mm3`` is a naive area×height
    projection assuming solid pillars — typically 3-8× higher than what
    PrusaSlicer's Grid supports actually extrude (15% infill default).
    Pro+ tier callers get a calibrated number via
    ``enrichment.supports_calibration`` on the full report.

    When ``bbox`` is supplied, the ``likely_substituted_by_bridge``
    flag is set when the part's horizontal envelope is short enough
    that PrusaSlicer's auto-support will probably bridge instead.
    """
    if normalize_winding:
        triangles = _normalize_triangle_winding(triangles)

    support_volume = 0.0
    support_regions: list[dict[str, float]] = []
    overhang_tris: list[tuple[tuple[float, ...], ...]] = []

    for tri in triangles:
        if _is_bed_supported_triangle(tri, z_min, layer_height):
            continue

        n = _triangle_normal(tri[0], tri[1], tri[2])
        nn = _normalize(n)
        nz = nn[2]

        if nz >= 0:
            continue

        angle_from_down = math.degrees(math.acos(max(-1.0, min(1.0, -nz))))
        overhang_angle = max(0.0, 90.0 - angle_from_down)
        # Epsilon-tolerant comparison so an exact 45.0° slope is
        # classified as an overhang.  math.acos(sqrt(2)/2) returns
        # 45.00000000000001° (one ULP above 45), making
        # `90 - that` land at 44.99999999999999° — below 45.0 by
        # one ULP, so a strict `<` filter skipped real 45° slopes.
        if overhang_angle + 1e-9 < max_overhang_angle:
            continue
        overhang_tris.append(tri)

        centroid = _triangle_centroid(tri[0], tri[1], tri[2])
        height = centroid[2] - z_min
        if height <= 0:
            continue

        area = _triangle_area(tri[0], tri[1], tri[2])
        volume = area * height
        support_volume += volume

        if len(support_regions) < 10:
            support_regions.append(
                {
                    "x": round(centroid[0], 2),
                    "y": round(centroid[1], 2),
                    "z": round(centroid[2], 2),
                    "volume_mm3": round(volume, 2),
                }
            )

    # Sort by volume descending.
    support_regions.sort(key=lambda r: r["volume_mm3"], reverse=True)

    # Estimate model volume for percentage calculation.
    model_volume = abs(sum(_signed_volume_of_triangle(tri[0], tri[1], tri[2]) for tri in triangles))

    # Clamp at 100% — the naive support estimate can exceed the model
    # volume on geometries with large horizontal overhangs above small
    # footprints (E01 long_thin_overhang reports 116% pre-clamp).
    # The raw number stays useful as an upper-bound; the percentage is
    # a sanity check, so capping it at 100 prevents nonsense like
    # "your supports are 116% of your model" surfacing to users.
    support_pct = (support_volume / model_volume * 100.0) if model_volume > 0 else 0.0
    support_pct = min(100.0, support_pct)

    bridge_substituted = _likely_bridge_substituted(overhang_tris, triangles)

    return SupportAnalysis(
        estimated_support_volume_mm3=round(support_volume, 2),
        support_percentage=round(support_pct, 1),
        support_regions=support_regions[:5],
        likely_substituted_by_bridge=bridge_substituted,
    )


@lru_cache(maxsize=1)
def _load_materials_json() -> dict[str, Any]:
    """Load the materials.json knowledge base (cached)."""
    path = Path(__file__).resolve().parent / "data" / "design_knowledge" / "materials.json"
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _get_material_warping_tendency(material: str) -> str:
    """Look up the warping tendency for a material from materials.json.

    Returns one of ``"low"``, ``"moderate"``, ``"high"``, or ``"very_high"``.
    Falls back to ``"moderate"`` for unknown materials.
    """
    data = _load_materials_json()
    mat_key = material.lower().strip()
    mat_entry = data.get(mat_key)
    if mat_entry is None:
        return "moderate"
    thermal = mat_entry.get("thermal", {})
    tendency = thermal.get("warping_tendency", "moderate")
    # Normalise "none" to "low" since our risk model uses low/moderate/high/very_high
    if tendency == "none":
        tendency = "low"
    return tendency


def _analyze_warping(
    triangles: list[tuple[tuple[float, ...], ...]],
    vertices: list[tuple[float, ...]],
    bbox: dict[str, float],
    *,
    material: str = "pla",
    overlay: dict[str, Any] | None = None,
) -> WarpingAnalysis:
    """Assess warping risk based on geometry and material properties.

    Soft tier seam: free tier (``overlay`` empty) uses
    :data:`_WARPING_PUBLIC_DEFAULTS`; Pro+ overlay supplies curated /
    tuned versions via the ``printability_judgment`` overlay's
    ``warping`` block.  Geometry facts + material tendency are identical
    across tiers; only the JUDGMENT (which bucket -> which deduction ->
    which message) varies.

    Risk formula: ``final_risk = (material_baseline + geometry_score) *
    material_multiplier``.  The ``material_baseline_risk`` block (free
    tier conservative; Pro tier curated) gives warp-prone materials a
    risk floor that compact geometry alone doesn't surface, while
    keeping low-warp materials (default 0.0) at zero-baseline behavior.
    """
    cfg = (overlay or {}).get("warping") or _WARPING_PUBLIC_DEFAULTS

    z_min = bbox["z_min"]
    z_max = bbox["z_max"]
    x_span = bbox["x_max"] - bbox["x_min"]
    y_span = bbox["y_max"] - bbox["y_min"]
    z_span = z_max - z_min

    flat_area_total = 0.0
    large_flat_surfaces: list[dict[str, float]] = []
    for tri in triangles:
        n = _triangle_normal(tri[0], tri[1], tri[2])
        nn = _normalize(n)
        if abs(nn[2]) > 0.95:
            area = _triangle_area(tri[0], tri[1], tri[2])
            flat_area_total += area
            if area > 100.0 and len(large_flat_surfaces) < 20:
                centroid = _triangle_centroid(tri[0], tri[1], tri[2])
                large_flat_surfaces.append({
                    "area_mm2": round(area, 2),
                    "centroid_x": round(centroid[0], 2),
                    "centroid_y": round(centroid[1], 2),
                    "centroid_z": round(centroid[2], 2),
                })
    large_flat_surfaces.sort(key=lambda s: s["area_mm2"], reverse=True)
    large_flat_surfaces = large_flat_surfaces[:10]

    base_threshold = z_min + 5.0
    _sharp_verts_seen: set[tuple[float, float, float]] = set()
    for tri in triangles:
        centroid = _triangle_centroid(tri[0], tri[1], tri[2])
        if centroid[2] > base_threshold:
            continue
        verts = [tri[0], tri[1], tri[2]]
        for i in range(3):
            v0 = verts[i]
            v1 = verts[(i + 1) % 3]
            v2 = verts[(i + 2) % 3]
            e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
            e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
            dot = e1[0] * e2[0] + e1[1] * e2[1] + e1[2] * e2[2]
            len1 = math.sqrt(e1[0] ** 2 + e1[1] ** 2 + e1[2] ** 2)
            len2 = math.sqrt(e2[0] ** 2 + e2[1] ** 2 + e2[2] ** 2)
            if len1 < 1e-12 or len2 < 1e-12:
                continue
            cos_angle = max(-1.0, min(1.0, dot / (len1 * len2)))
            angle_deg = math.degrees(math.acos(cos_angle))
            if angle_deg < 90.0:
                rounded = (round(v0[0], 4), round(v0[1], 4), round(v0[2], 4))
                _sharp_verts_seen.add(rounded)
    sharp_corners_at_base = len(_sharp_verts_seen)

    base_dim = max(0.1, min(x_span, y_span))
    height_to_base_ratio = z_span / base_dim
    tendency = _get_material_warping_tendency(material)

    metrics: dict[str, Any] = {
        "flat_area_total_mm2": flat_area_total,
        "height_to_base_ratio": height_to_base_ratio,
        "sharp_corners_at_base": sharp_corners_at_base,
        "material_tendency": tendency,
        "material": material,
    }
    geometry_score = _sum_score_rules(cfg.get("geometry_score_rules", []), metrics)
    material_multipliers = cfg.get("material_multipliers", {})
    mat_key = material.lower()

    # Per-material multiplier override.  Materials missing from the
    # public materials.json catalog (PEEK, PA6, PA12, HIPS, ABS-CF,
    # ASA-CF, PC alias) would otherwise fall through to the "moderate"
    # tendency multiplier (1.0) regardless of their real warping
    # tendency.  ``material_specific_multipliers`` lets the overlay
    # explicitly map these materials to their curated multipliers.
    specific_mults = cfg.get("material_specific_multipliers", {})
    if mat_key in specific_mults:
        material_multiplier = float(specific_mults[mat_key])
    else:
        material_multiplier = material_multipliers.get(tendency, 1.0)

    # Per-material baseline-risk floor.  Free tier ships a conservative
    # schedule (see _WARPING_PUBLIC_DEFAULTS) that catches the worst
    # warp-prone materials; Pro overlay supplies a finer-grained schedule
    # via the ``printability_judgment`` overlay's ``material_baseline_risk``
    # block.  Default 0.0 for unknown materials means low-warp (PLA / PETG
    # / TPU / CF-loaded) prints see no baseline contribution.
    baselines = cfg.get("material_baseline_risk", {})
    material_baseline = float(baselines.get(mat_key, 0.0))

    final_risk = (material_baseline + geometry_score) * material_multiplier

    risk_thresholds = cfg.get("risk_thresholds", {})
    if final_risk >= risk_thresholds.get("critical", 3.0):
        risk_level = "critical"
    elif final_risk >= risk_thresholds.get("high", 2.0):
        risk_level = "high"
    elif final_risk >= risk_thresholds.get("moderate", 1.0):
        risk_level = "moderate"
    else:
        risk_level = "low"

    score_deduction = int(cfg.get("score_deductions", {}).get(risk_level, 0))
    recommendations = _apply_recommendation_rules(
        cfg.get("recommendation_rules", []), metrics,
    )

    return WarpingAnalysis(
        risk_level=risk_level,
        score_deduction=score_deduction,
        large_flat_surfaces=large_flat_surfaces,
        sharp_corners_at_base=sharp_corners_at_base,
        height_to_base_ratio=round(height_to_base_ratio, 2),
        material_warping_tendency=tendency,
        recommendations=recommendations,
    )


# Hydraulic-thickness (4V/A) bands for the thermal slenderness gate.
# Calibrated against the 2026-08-25 template sweep: a 1.6 mm latch fin
# reads 3.2 (full risk kept), a 12 mm coat-hook peg reads 12.0 (real
# prints of both confirm only the fin is a genuine cracking risk).
_STURDY_FEATURE_MM = 4.5   # >= this: features can flex/absorb, one level down
_CHUNKY_FEATURE_MM = 8.0   # >= this: differential cooling is negligible

# The gate only applies to materials with curated LOW thermal
# amplification (Pro overlay: PLA 0.6, PETG ~0.8).  High-shrink
# materials (ABS, Nylon, PP, PC, PEEK) crack at chunky transitions
# too — bulk shrink force scales with the section — and the free
# tier's uniform 1.0 factor deliberately keeps the conservative
# safety floor, consistent with the free-vs-Pro seam everywhere else
# in this module.
_FORGIVING_STRESS_FACTOR = 0.8

# The proxy reads the structure IMMEDIATELY above the transition (the
# neck) — not everything above it, or a dumbbell's fat far cap would
# mask its thin neck.
_SUFFIX_BAND_MM = 6.0


def _clip_triangle_above(
    tri: tuple[tuple[float, ...], ...], z0: float,
) -> list[tuple[tuple[float, ...], ...]]:
    """Return the portion of ``tri`` with z >= z0, as 0-2 triangles."""
    above = [v for v in tri if v[2] >= z0]
    if len(above) == 3:
        return [tri]
    if not above:
        return []

    def lerp(a, b):
        t = (z0 - a[2]) / (b[2] - a[2])
        return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), z0)

    # Preserve winding: walk the original vertex order.
    poly: list[tuple[float, ...]] = []
    for i in range(3):
        a, b = tri[i], tri[(i + 1) % 3]
        if a[2] >= z0:
            poly.append(a)
        if (a[2] >= z0) != (b[2] >= z0):
            poly.append(lerp(a, b))
    if len(poly) < 3:
        return []
    return [(poly[0], poly[i], poly[i + 1]) for i in range(1, len(poly) - 1)]


def _volume_above_plane(
    triangles: list[tuple[tuple[float, ...], ...]], z0: float,
) -> float:
    """Exact mesh volume above the plane z=z0.

    Each boundary triangle is clipped at z0 and its signed tetrahedron
    volume taken against an apex ON the plane, so the unknown
    cross-section cap at z0 contributes nothing.
    """
    apex = (0.0, 0.0, z0)
    volume = 0.0
    for tri in triangles:
        for piece in _clip_triangle_above(tri, z0):
            p = tuple(c - a for c, a in zip(piece[0], apex))
            q = tuple(c - a for c, a in zip(piece[1], apex))
            r = tuple(c - a for c, a in zip(piece[2], apex))
            volume += (
                p[0] * (q[1] * r[2] - q[2] * r[1])
                - p[1] * (q[0] * r[2] - q[2] * r[0])
                + p[2] * (q[0] * r[1] - q[1] * r[0])
            ) / 6.0
    return abs(volume)


def _suffix_thickness_mm(
    triangles: list[tuple[tuple[float, ...], ...]], z0: float,
    band: float = _SUFFIX_BAND_MM,
) -> float:
    """Hydraulic thickness (4V/A) of the geometry in the band just
    above ``z0`` — the neck the transition hands the print to.

    Band volume is the difference of two exact above-plane volumes.
    The area term counts wall-like faces only (|nz| < 0.7), clipped to
    the band; flat tops would dilute the thickness reading without
    describing any wall.
    """
    volume = (_volume_above_plane(triangles, z0)
              - _volume_above_plane(triangles, z0 + band))
    wall_area = 0.0
    for tri in triangles:
        for piece in _clip_triangle_above(tri, z0):
            # Clip the band's top by mirroring through its midplane.
            flipped = tuple((v[0], v[1], 2 * z0 + band - v[2]) for v in piece)
            for kept in _clip_triangle_above(flipped, z0):
                n = _normalize(_triangle_normal(*kept))
                if abs(n[2]) < 0.7:
                    wall_area += _triangle_area(*kept)
    if wall_area < 1e-6:
        return 0.0
    return 4.0 * abs(volume) / wall_area


def _analyze_thermal_stress(
    triangles: list[tuple[tuple[float, ...], ...]],
    bbox: dict[str, float],
    *,
    material: str = "pla",
    layer_height: float = 0.2,
    overlay: dict[str, Any] | None = None,
) -> ThermalStressAnalysis:
    """Estimate thermal stress concentration from cross-section area changes.

    Soft tier seam: free tier uses :data:`_THERMAL_STRESS_PUBLIC_DEFAULTS`;
    Pro+ overlay supplies tuned thresholds + recommendation templates
    via the ``printability_judgment`` overlay's ``thermal_stress`` block.
    Material stress factor: free tier uses :data:`_DEFAULT_STRESS_FACTOR`
    (1.0) for every material; kiln-pro overlay supplies per-material
    curated values via :func:`_material_stress_factor`.
    """
    cfg = (overlay or {}).get("thermal_stress") or _THERMAL_STRESS_PUBLIC_DEFAULTS
    zone_ratio_threshold = cfg.get("stress_zone_ratio_threshold", 2.0)
    max_tracked = int(cfg.get("max_zones_tracked", 20))
    max_emitted = int(cfg.get("max_zones_emitted", 10))

    z_min = bbox["z_min"]
    z_max = bbox["z_max"]
    z_span = z_max - z_min

    if z_span < layer_height * 2 or not triangles:
        return ThermalStressAnalysis(
            risk_level="low",
            score_deduction=0,
            max_area_change_ratio=1.0,
            stress_concentration_zones=[],
            layer_count_analyzed=0,
            material_stress_factor=_material_stress_factor(material),
            recommendations=[],
        )

    num_layers = max(2, int(z_span / layer_height))
    layer_areas: list[float] = [0.0] * num_layers

    # Wall-vs-face fix: cross-section change at each Z is proxied by the
    # vertical-wall contribution at that layer, not the horizontal-face
    # contribution (which is the OPPOSITE signal — flat tops and bottoms
    # don't represent cross-section change, they represent the absence
    # of cross-section transition).  We distribute each wall triangle's
    # area across all the layers it z-spans, weighted by overlap, so a
    # uniform-cross-section box produces uniform per-layer wall area
    # (no false-positive critical verdict on a plain 20x20x20 cube).
    # Horizontal faces (|normal_z| ≈ 1) contribute (1 - |nz|) ≈ 0 and
    # are effectively ignored; vertical walls (|nz| ≈ 0) contribute
    # their full area, distributed along Z.
    for tri in triangles:
        normal = _triangle_normal(tri[0], tri[1], tri[2])
        nn = _normalize(normal)
        wall_weight = 1.0 - abs(nn[2])
        if wall_weight <= 1e-4:
            continue
        area = _triangle_area(tri[0], tri[1], tri[2])
        contribution = wall_weight * area

        zs = (tri[0][2], tri[1][2], tri[2][2])
        z_low = min(zs)
        z_high = max(zs)
        tri_span = z_high - z_low
        if tri_span <= 1e-6:
            # Degenerate slim triangle — drop into single bucket by mid-Z.
            bucket = int((z_low - z_min) / layer_height)
            bucket = max(0, min(bucket, num_layers - 1))
            layer_areas[bucket] += contribution
            continue

        bucket_low = max(0, int((z_low - z_min) / layer_height))
        bucket_high = min(num_layers - 1, int((z_high - z_min) / layer_height))
        for b in range(bucket_low, bucket_high + 1):
            layer_z_low = z_min + b * layer_height
            layer_z_high = layer_z_low + layer_height
            overlap = min(z_high, layer_z_high) - max(z_low, layer_z_low)
            if overlap <= 0:
                continue
            layer_areas[b] += contribution * (overlap / tri_span)

    # Median wall-area smooths over single-layer numerical artifacts
    # (one triangle landing alone in a thin layer) while still letting
    # true cross-section changes between adjacent layers fire as zones.
    # Min-floor at a small fraction of the median prevents divide-by-
    # noise ratios from inflating verdicts.
    median_area = (
        sorted(layer_areas)[num_layers // 2]
        if layer_areas else 0.0
    )
    floor = max(0.01, median_area * 0.05)

    stress_zones: list[dict[str, float]] = []
    max_ratio = 1.0

    # Thermal contraction stress needs structure ABOVE the transition
    # to crack: a cross-section step carrying only a sliver of
    # remaining print (a decorative rim detail, a thread run-out, a
    # pyramid tip) has nothing above it for differential cooling to
    # act on.  Ignore transitions where the wall area above the layer
    # is a negligible fraction of the part — without this, a spiky rim
    # feature turns a trivially printable part "critical".
    total_wall_area = sum(layer_areas)
    suffix_area = [0.0] * (num_layers + 1)
    for i in range(num_layers - 1, -1, -1):
        suffix_area[i] = suffix_area[i + 1] + layer_areas[i]
    significance_floor = total_wall_area * 0.02

    for i in range(1, num_layers):
        if suffix_area[i] < significance_floor:
            continue
        a_prev = max(layer_areas[i - 1], floor)
        a_curr = max(layer_areas[i], floor)
        bigger = max(a_prev, a_curr)
        smaller = min(a_prev, a_curr)
        ratio = bigger / smaller
        if ratio > max_ratio:
            max_ratio = ratio
        if ratio > zone_ratio_threshold and len(stress_zones) < max_tracked:
            stress_zones.append({
                "z_mm": round(z_min + i * layer_height, 2),
                "area_change_ratio": round(ratio, 2),
                "layer_area_mm2": round(layer_areas[i], 2),
            })

    stress_zones.sort(key=lambda z: z["area_change_ratio"], reverse=True)
    stress_zones = stress_zones[:max_emitted]

    stress_factor = _material_stress_factor(material)
    combined_score = max_ratio * stress_factor

    risk_thresholds = cfg.get("risk_thresholds", {})
    if combined_score >= risk_thresholds.get("critical", 5.0):
        risk_level = "critical"
    elif combined_score >= risk_thresholds.get("high", 3.0):
        risk_level = "high"
    elif combined_score >= risk_thresholds.get("moderate", 1.8):
        risk_level = "moderate"
    else:
        risk_level = "low"

    # Slenderness gate: the area-change ratio measures how ABRUPT a
    # transition is, not how FRAGILE the continuing structure is.
    # Thermal contraction cracks thin necks — a 1.6 mm latch fin cools
    # fast and concentrates stress across a sliver of weld — but a
    # 12 mm peg above the same plate carries the same ratio and no
    # real risk in a forgiving material.  Gauge the neck's hydraulic
    # thickness (4V/A: a cylinder reads its diameter, a slab twice
    # its wall) just above the peak zone and downgrade the verdict
    # when it is sturdy.  Applies only when the material's curated
    # stress factor is forgiving: high-shrink materials crack chunky
    # transitions too, and the free tier's uniform 1.0 keeps the
    # conservative safety floor.
    if (stress_zones and risk_level != "low"
            and stress_factor <= _FORGIVING_STRESS_FACTOR):
        suffix_mm = _suffix_thickness_mm(triangles, stress_zones[0]["z_mm"])
        if suffix_mm >= _CHUNKY_FEATURE_MM:
            risk_level = {"critical": "moderate",
                          "high": "low", "moderate": "low"}[risk_level]
        elif suffix_mm >= _STURDY_FEATURE_MM:
            risk_level = {"critical": "high",
                          "high": "moderate", "moderate": "low"}[risk_level]

    score_deduction = int(cfg.get("score_deductions", {}).get(risk_level, 0))

    # Peak-zone z-coordinate for the recommendation template: the
    # author of the part wants to know WHERE to add the chamfer.
    peak_zone_z = stress_zones[0]["z_mm"] if stress_zones else 0.0
    stress_zones_count = len(stress_zones)

    rec_metrics: dict[str, Any] = {
        "risk_level": risk_level,
        "max_ratio": max_ratio,
        "material": material,
        "stress_factor": stress_factor,
        # Amplification-active threshold tightened to >= 1.2 (was > 1.0)
        # post wall-vs-face fix.  PETG (Pro stress_factor = 1.0) was
        # tripping the enclosure recommendation under the old gate even
        # though PETG genuinely doesn't need an enclosure.  >= 1.2
        # restricts the recommendation to materials where chamber
        # control is a real engineering necessity (ABS+, Nylon+, PC+).
        "amplification_active": (stress_factor >= 1.2 and risk_level != "low"),
        "peak_zone_z": peak_zone_z,
        "stress_zones_count": stress_zones_count,
        # `many_zones` is a derived flag for the "split the part" rec:
        # fires only when there are MULTIPLE distinct transition zones
        # in a high-risk verdict, not on a single sharp step.
        "many_zones": (stress_zones_count >= 3 and risk_level in ("high", "critical")),
    }
    recommendations = _apply_recommendation_rules(
        cfg.get("recommendation_rules", []), rec_metrics,
    )

    return ThermalStressAnalysis(
        risk_level=risk_level,
        score_deduction=score_deduction,
        max_area_change_ratio=round(max_ratio, 2),
        stress_concentration_zones=stress_zones,
        layer_count_analyzed=num_layers,
        material_stress_factor=stress_factor,
        recommendations=recommendations,
    )


def _estimate_adhesion_force(
    contact_area_mm2: float,
    bbox: dict[str, float],
    *,
    material: str = "pla",
    overlay: dict[str, Any] | None = None,
) -> AdhesionForceEstimate:
    """Predict whether bed adhesion force exceeds warping/peel force.

    Soft tier seam: free tier uses :data:`_ADHESION_FORCE_PUBLIC_DEFAULTS`;
    Pro+ overlay supplies tuned risk thresholds, peel-force scale, and
    recommendation templates via the ``printability_judgment`` overlay's
    ``adhesion_force`` block.  Material adhesion strength + shrinkage
    strain: free tier uses :data:`_DEFAULT_ADHESION_STRENGTH` (0.10
    N/mm²) and :data:`_DEFAULT_SHRINKAGE_STRAIN` (0.005 mm/mm) for
    every material; kiln-pro overlay supplies per-material curated
    values via :func:`_material_adhesion_strength` and
    :func:`_material_shrinkage_strain`.
    """
    cfg = (overlay or {}).get("adhesion_force") or _ADHESION_FORCE_PUBLIC_DEFAULTS
    peel_scale = cfg.get("peel_force_scale", 0.01)

    mat_key = material.lower()
    adhesion_strength = _material_adhesion_strength(material)
    shrinkage_strain = _material_shrinkage_strain(material)

    x_span = bbox["x_max"] - bbox["x_min"]
    y_span = bbox["y_max"] - bbox["y_min"]
    z_span = bbox["z_max"] - bbox["z_min"]

    min_base_dim = min(x_span, y_span)
    aspect_ratio = (z_span / min_base_dim) if min_base_dim > 0.1 else 0.0

    adhesion_force = contact_area_mm2 * adhesion_strength
    longest_xy = max(x_span, y_span)
    peel_force = shrinkage_strain * longest_xy * z_span * peel_scale

    # Aspect-ratio peel multiplier.  Tall-narrow geometry
    # concentrates peel stress at the base in a nonlinear way the
    # static linear-in-z formula doesn't capture.  Multiplies peel
    # by ``max(1.0, (aspect / 10) ** exponent)`` so compact prints
    # (aspect <= 10) get unchanged peel, while tall-narrow prints
    # see proportionally more peel pressure on their small base.
    # Disabled by setting the exponent to ``None`` in the overlay.
    # See _ADHESION_FORCE_PUBLIC_DEFAULTS docstring for calibration
    # rationale (1.5 chosen against the 24-case sweep).
    aspect_exp = cfg.get("aspect_ratio_peel_exponent", 1.5)
    if aspect_exp is not None and aspect_ratio > 10.0:
        peel_force *= (aspect_ratio / 10.0) ** float(aspect_exp)

    # Thermal-stress contribution to peel.  Warp-prone materials
    # (high CTE, high stress_factor) generate cyclic peel stress as
    # each layer cools and contracts against the constrained base.
    # Linear-in-z accumulation until thermal equilibrium; multiplied
    # by per-material stress_factor (PLA ~0.6, ABS ~1.5, PP ~2.0,
    # Nylon ~1.6).  Disabled by setting thermal_z_scale to ``None``
    # in the overlay.  See _ADHESION_FORCE_PUBLIC_DEFAULTS docstring
    # for calibration rationale (z_scale=100 chosen against the
    # 31-case sweep; catches all 8 truly-risky cases with zero
    # false positives).
    thermal_z_scale = cfg.get("peel_thermal_z_scale", 100.0)
    if thermal_z_scale is not None and z_span > 0:
        stress_factor = _material_stress_factor(material)
        peel_force *= 1.0 + float(stress_factor) * (z_span / float(thermal_z_scale))

    force_ratio = adhesion_force / max(peel_force, 0.001)
    will_detach = force_ratio < 1.0

    risk_thresholds = cfg.get("risk_thresholds", {})
    if force_ratio >= risk_thresholds.get("secure", 3.0):
        risk_level = "secure"
    elif force_ratio >= risk_thresholds.get("marginal", 1.5):
        risk_level = "marginal"
    else:
        risk_level = "likely_detach"

    # Geometry-only adhesion guard.  The force-balance model is
    # decent at extremes but undercounts dynamic peel stress on
    # tall-narrow prints in the middle range.  When aspect ratio
    # is extreme, the geometry itself warrants a "marginal" verdict
    # regardless of what the force ratio says — a 2x2x250mm tower
    # detaches in practice even when the static force balance
    # passes.  Only upgrades secure→marginal; never downgrades
    # an already-flagged verdict.  Disabled by setting the
    # threshold to ``None`` in the overlay.
    # Default to 50 when the overlay doesn't override.  Overlay can
    # set this to ``None`` to disable the guard entirely (e.g. a
    # caller who has their own geometry analysis).
    aspect_threshold = cfg.get("aspect_ratio_extreme_threshold", 50.0)
    geometry_extreme = (
        aspect_threshold is not None
        and aspect_ratio > float(aspect_threshold)
    )
    if geometry_extreme and risk_level == "secure":
        risk_level = "marginal"

    score_deduction = int(cfg.get("score_deductions", {}).get(risk_level, 0))

    poor_materials = cfg.get("poor_adhesion_materials", [])
    rec_metrics: dict[str, Any] = {
        "risk_level": risk_level,
        "material": material,
        "is_poor_adhesion_material": (mat_key in poor_materials),
    }
    recommendations = _apply_recommendation_rules(
        cfg.get("recommendation_rules", []), rec_metrics,
    )

    # The geometry guard is a code-level upgrade (not a config-driven
    # rule), so its explanation is appended here rather than via the
    # rules system.  This ensures the recommendation fires for both
    # free tier (public defaults) and Pro tier (overlay), since the
    # overlay's own ``recommendation_rules`` may not include this
    # specific guard.
    if geometry_extreme:
        recommendations = list(recommendations) + [
            f"Tall, narrow geometry (aspect ratio {aspect_ratio:.0f}) "
            "concentrates peel stress at the base regardless of "
            "material. Use a brim or raft and verify your bed "
            "surface is clean and level. The force-balance verdict "
            "alone can understate this risk; Kiln Pro adds per-"
            "material physics on top.",
        ]

    # Model-confidence band.  The static force-balance model is
    # reliable at clear extremes (ratio > 10 = definitively secure;
    # ratio < 0.5 = definitively likely_detach) and approximate in
    # the messy middle range where dynamic peel stress and material-
    # specific bed chemistry dominate.  A geometry-guard upgrade
    # also marks the verdict as approximate, since the guard fired
    # because the force-balance result was suspect for this shape.
    if geometry_extreme:
        model_confidence = "approximate"
    elif force_ratio > 10.0 or force_ratio < 0.5:
        model_confidence = "high"
    else:
        model_confidence = "approximate"

    return AdhesionForceEstimate(
        adhesion_force_n=round(adhesion_force, 3),
        peel_force_n=round(peel_force, 3),
        force_ratio=round(force_ratio, 2),
        will_detach=will_detach,
        risk_level=risk_level,
        score_deduction=score_deduction,
        recommendations=recommendations,
        model_confidence=model_confidence,
    )


def _compute_score(
    overhangs: OverhangAnalysis,
    thin_walls: ThinWallAnalysis,
    bridging: BridgingAnalysis,
    bed_adhesion: BedAdhesionAnalysis,
    supports: SupportAnalysis,
    warping: WarpingAnalysis | None = None,
    thermal_stress: ThermalStressAnalysis | None = None,
    adhesion_force: AdhesionForceEstimate | None = None,
    overhang_scoring_pct: float | None = None,
) -> int:
    """Compute a printability score from 0-100.

    Starts at 100 and deducts points for each issue found.

    ``overhang_scoring_pct`` overrides the overhang percentage used
    for the deduction — the caller passes the percentage of overhangs
    that genuinely need supports when part of the reported set is
    self-supporting (small bridgeable / lateral-reach regions).
    """
    score = 100

    # Overhang deductions (max -30)
    if overhangs.needs_supports:
        pct = (
            overhang_scoring_pct
            if overhang_scoring_pct is not None
            else overhangs.overhang_percentage
        )
        score -= min(30, int(pct * 0.5))

    # Thin wall deductions (max -25)
    if thin_walls.thin_wall_count > 0:
        score -= min(25, int(thin_walls.thin_wall_percentage * 0.5))

    # Bridging deductions (max -15) — only when the bridges actually need
    # support.  ``bridge_count`` alone counts every short, self-supporting
    # span too: a decorative surface texture's grooves register as 1000+
    # sub-millimetre "bridges" (each well under the 10 mm self-support
    # limit, so ``needs_supports_for_bridges`` is False) and used to max
    # this deduction out, dropping a perfectly printable textured part two
    # whole grades for relief that prints fine with no supports.  Gate on
    # ``needs_supports_for_bridges`` — the same > 10 mm span test the
    # "Long bridges detected" recommendation already uses below — so the
    # score and the advice finally agree.
    if bridging.bridge_count > 0 and bridging.needs_supports_for_bridges:
        score -= min(15, 5 + bridging.bridge_count)

    # Bed adhesion deductions (max -15)
    if bed_adhesion.adhesion_risk == "high":
        score -= 15
    elif bed_adhesion.adhesion_risk == "medium":
        score -= 7

    # Support volume deductions (max -15)
    if supports.support_percentage > 50:
        score -= 15
    elif supports.support_percentage > 20:
        score -= 10
    elif supports.support_percentage > 5:
        score -= 5

    if warping is not None:
        score += warping.score_deduction

    if thermal_stress is not None:
        score += thermal_stress.score_deduction

    if adhesion_force is not None:
        score += adhesion_force.score_deduction

    return max(0, min(100, score))


def _score_to_grade(score: int) -> str:
    """Convert a 0-100 score to a letter grade."""
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


# ---------------------------------------------------------------------------
# Placement check
#
# Shape analysis (overhangs, walls, bridging) grades how a part is
# built; it says nothing about WHERE the part sits.  A model hanging
# below the bed or wider than the machine can score an untroubled A and
# still be impossible to print.  Both the direct analysis path and the
# pipeline's bundle-sourced path route through the one helper below so
# they cannot drift apart on that verdict.
# ---------------------------------------------------------------------------

#: Score deducted per placement failure.  Sized so that a single
#: failure lands any report under :data:`_PRINTABLE_SCORE_MIN` from any
#: starting grade — a part off the bed or over the bed's edge does not
#: print at all, so an otherwise clean A must not still read printable.
_PLACEMENT_PENALTY = 50

#: Slack allowed below z=0 before the part counts as off the bed.  Mesh
#: coordinates carry float noise; a vertex at -1e-9 mm is on the bed.
_PLACEMENT_Z_TOLERANCE_MM = 0.001

#: Minimum score for a report to call itself printable.
_PRINTABLE_SCORE_MIN = 50


def _bbox_span(bbox: dict[str, float] | None) -> tuple[float, float, float] | None:
    """Return ``(dx, dy, dz)`` for *bbox*, or ``None`` if unmeasurable."""
    if not bbox:
        return None
    try:
        return (
            bbox["x_max"] - bbox["x_min"],
            bbox["y_max"] - bbox["y_min"],
            bbox["z_max"] - bbox["z_min"],
        )
    except (KeyError, TypeError):
        return None


def _resolve_placement_volume(
    build_volume: tuple[float, float, float] | None,
    printer_id: str | None,
) -> tuple[float, float, float] | None:
    """Resolve the bed to measure against, or ``None`` to skip the check.

    An explicit *build_volume* always wins.  Failing that the printer
    catalogue is consulted, which answers ``None`` for a machine it does
    not know.  A bed that cannot be resolved skips the fit check rather
    than raising — the same "unknown, don't block" contract
    :func:`kiln.printers.bed_fit.get_build_volume` documents.
    """
    if build_volume is not None:
        return build_volume
    if not printer_id:
        return None
    try:
        from kiln.printers.bed_fit import get_build_volume

        return get_build_volume(printer_id)
    except Exception:  # pragma: no cover - catalogue unavailable
        return None


def _placement_faults(
    bbox: dict[str, float] | None,
    *,
    build_volume: tuple[float, float, float] | None = None,
    printer_id: str | None = None,
) -> list[str]:
    """Return a recommendation per placement fault, worst first.

    Pure detection — scores nothing and mutates nothing, so a caller can
    re-check the verdict later without deducting twice.

    Degrades quietly: an absent or malformed *bbox* skips both checks,
    and an unresolvable bed skips the fit check.  Neither raises.
    """
    span = _bbox_span(bbox)
    if span is None or bbox is None:
        return []

    faults: list[str] = []

    dx, dy, dz = span
    volume = _resolve_placement_volume(build_volume, printer_id)
    if volume is not None:
        bx, by, bz = volume
        if dx > bx or dy > by or dz > bz:
            faults.append(
                f"Model ({dx:.1f} x {dy:.1f} x {dz:.1f} mm) exceeds build "
                f"volume ({bx:.0f} x {by:.0f} x {bz:.0f} mm)."
            )

    z_min = bbox.get("z_min")
    if z_min is not None and z_min < -_PLACEMENT_Z_TOLERANCE_MM:
        faults.append(
            f"Part sits {abs(z_min):.1f} mm below the build plate "
            f"(z_min = {z_min:.1f} mm) — anything under the plate is lost "
            f"when it slices. Drop it onto the plate first "
            f"(center_model_on_bed)."
        )

    return faults


def _apply_placement_check(
    score: int,
    recommendations: list[str],
    bbox: dict[str, float] | None,
    *,
    build_volume: tuple[float, float, float] | None = None,
    printer_id: str | None = None,
) -> tuple[int, str, bool, list[str]]:
    """Grade where the part sits, on top of how it is shaped.

    Flags the two placement faults no shape metric can see:

    * a part larger than the resolved build volume
    * geometry below the bed (``z_min`` under 0) — the slicer silently
      clips whatever hangs through the plate

    Each fault goes to the TOP of *recommendations* (mutated in place —
    placement outranks tuning advice) and deducts from the score.  Grade
    is recomputed from what is left.

    ``printable`` is gated on the faults themselves, not on the
    arithmetic: a part that cannot physically print must not come back
    printable just because it started from a high enough score to
    survive the deduction.

    :param score: Score so far, 0-100.
    :param recommendations: Recommendation list, mutated in place.
    :param bbox: Mesh bounding box with ``x_min``/``x_max``/... keys.
    :param build_volume: Explicit ``(x, y, z)`` bed size in mm.
    :param printer_id: Consulted for the bed when *build_volume* is absent.
    :returns: ``(score, grade, printable, faults)``.
    """
    faults = _placement_faults(
        bbox, build_volume=build_volume, printer_id=printer_id,
    )
    if faults:
        recommendations[:0] = faults
        score = max(0, score - _PLACEMENT_PENALTY * len(faults))

    printable = score >= _PRINTABLE_SCORE_MIN and not faults
    return score, _score_to_grade(score), printable, faults


def _build_recommendations(
    overhangs: OverhangAnalysis,
    thin_walls: ThinWallAnalysis,
    bridging: BridgingAnalysis,
    bed_adhesion: BedAdhesionAnalysis,
    supports: SupportAnalysis,
    warping: WarpingAnalysis | None = None,
    thermal_stress: ThermalStressAnalysis | None = None,
    adhesion_force: AdhesionForceEstimate | None = None,
) -> list[str]:
    """Generate actionable recommendations based on analysis results."""
    recs: list[str] = []

    if overhangs.needs_supports:
        recs.append(
            f"Enable supports: {overhangs.overhang_percentage:.0f}% of triangles "
            f"are overhangs.  Consider re-orienting the model to reduce supports."
        )

    if thin_walls.thin_wall_count > 0:
        recs.append(
            f"Thin walls detected ({thin_walls.min_wall_thickness_mm:.2f} mm min).  "
            f"Use a smaller nozzle or increase wall thickness."
        )

    if bridging.needs_supports_for_bridges:
        recs.append(
            f"Long bridges detected ({bridging.max_bridge_length_mm:.1f} mm).  Enable supports or reduce bridge spans."
        )

    if bed_adhesion.adhesion_risk == "high":
        recs.append(
            "Low bed contact area.  Use a brim or raft, or re-orient the model to increase the contact surface."
        )
    elif bed_adhesion.adhesion_risk == "medium":
        recs.append("Moderate bed contact area.  Consider adding a brim for better adhesion.")

    if supports.support_percentage > 20:
        recs.append(
            f"High support volume ({supports.support_percentage:.0f}% of model).  "
            f"Re-orienting the model may reduce material waste."
        )

    if warping is not None and warping.recommendations:
        recs.extend(warping.recommendations)

    if thermal_stress is not None and thermal_stress.recommendations:
        recs.extend(thermal_stress.recommendations)

    if adhesion_force is not None and adhesion_force.recommendations:
        recs.extend(adhesion_force.recommendations)

    if not recs:
        recs.append("Model looks good for printing.  No issues detected.")

    return recs


# ---------------------------------------------------------------------------
# Cost analysis
# ---------------------------------------------------------------------------


def _analyze_cost(
    mesh: Any,
    file_path: str,
    material: str = "PLA",
    infill_percent: float = 20.0,
    needs_supports: bool = False,
    adhesion_risk: str = "low",
) -> CostAnalysis:
    """Compute cost breakdown integrated with printability analysis.

    Uses :class:`~kiln.cost_estimator.CostEstimator` to produce a full
    cost estimate and generates cost-saving recommendations.
    """
    from kiln.cost_estimator import CostEstimator

    estimator = CostEstimator()

    # Map adhesion risk to adhesion type
    adhesion_type = "brim" if adhesion_risk == "high" else "none"

    estimate = estimator.estimate_from_mesh(
        file_path,
        material=material,
        infill_percent=infill_percent,
        include_supports=needs_supports,
        adhesion_type=adhesion_type,
    )

    # --- Cost-saving recommendations ---
    recommendations: list[str] = []

    # Suggestion: lower infill
    if infill_percent > 15.0:
        lower_est = estimator.estimate_from_mesh(
            file_path,
            material=material,
            infill_percent=15.0,
            include_supports=needs_supports,
            adhesion_type=adhesion_type,
        )
        savings = estimate.total_cost_usd - lower_est.total_cost_usd
        if savings > 0.01:
            recommendations.append(
                f"Reducing infill from {infill_percent:.0f}% to 15% would save ~${savings:.2f}"
            )

    # Suggestion: support cost
    if estimate.support_cost_usd > 0:
        recommendations.append(
            f"Support material adds ${estimate.support_cost_usd:.2f}. "
            f"Reorienting the part may reduce overhang area."
        )

    # Suggestion: expensive material
    mat_upper = material.upper()
    profile = estimator.get_material(mat_upper)
    if profile is not None and profile.cost_per_kg_usd > 30.0:
        pla_est = estimator.estimate_from_mesh(
            file_path,
            material="PLA",
            infill_percent=infill_percent,
            include_supports=needs_supports,
            adhesion_type=adhesion_type,
        )
        savings = estimate.total_cost_usd - pla_est.total_cost_usd
        if savings > 0.01:
            recommendations.append(
                f"Using PLA (~$25/kg) instead of {mat_upper} "
                f"(~${profile.cost_per_kg_usd:.0f}/kg) would save ~${savings:.2f}"
            )

    # Suggestion: high infill on expensive prints
    if estimate.total_cost_usd > 5.0 and infill_percent > 20.0:
        low_est = estimator.estimate_from_mesh(
            file_path,
            material=material,
            infill_percent=10.0,
            include_supports=needs_supports,
            adhesion_type=adhesion_type,
        )
        savings = estimate.total_cost_usd - low_est.total_cost_usd
        if savings > 0.01:
            recommendations.append(
                f"For non-structural parts, 10% infill is often sufficient "
                f"— potential savings ~${savings:.2f}"
            )

    cost_summary = {
        "material": round(estimate.filament_cost_usd + estimate.support_cost_usd + estimate.adhesion_cost_usd, 2),
        "electricity": round(estimate.electricity_cost_usd, 2),
    }

    return CostAnalysis(
        estimated_cost_usd=estimate.total_cost_usd,
        material_cost_usd=estimate.filament_cost_usd,
        support_cost_usd=estimate.support_cost_usd,
        adhesion_cost_usd=estimate.adhesion_cost_usd,
        electricity_cost_usd=estimate.electricity_cost_usd,
        weight_grams=estimate.filament_weight_grams,
        filament_length_meters=estimate.filament_length_meters,
        cost_breakdown=estimate.cost_breakdown,
        cost_summary=cost_summary,
        cost_saving_recommendations=recommendations,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_printability(
    file_path: str,
    *,
    nozzle_diameter: float = 0.4,
    layer_height: float = 0.2,
    max_overhang_angle: float | None = None,
    build_volume: tuple[float, float, float] | None = None,
    material: str = "pla",
    infill_percent: float = 20.0,
    include_hole_detection: bool = True,
    printer_id: str | None = None,
    slicer_style: Literal["grid", "snug", "organic", "tree"] = "grid",
) -> PrintabilityReport:
    """Run a full printability analysis on a mesh file.

    :param file_path: Path to an STL or OBJ file.
    :param nozzle_diameter: Printer nozzle diameter in mm.
    :param layer_height: Print layer height in mm.
    :param max_overhang_angle: Max overhang angle (degrees) before
        supports are needed.
    :param build_volume: Optional (X, Y, Z) build volume in mm.  If
        provided, the report will warn if the model exceeds it.  Takes
        precedence over the bed *printer_id* would resolve.
    :param material: Material ID for warping and cost analysis (default ``"pla"``).
    :param infill_percent: Interior infill density (0-100) for cost estimation.
    :param include_hole_detection: When True (default), also run
        :func:`kiln.generation.validation.detect_holes` and surface the
        result on ``report.holes``.  Set False on perf-critical paths
        that don't need the per-hole list — hole detection re-parses
        the mesh internally, which roughly doubles the parse cost.
    :param printer_id: Resolves the build volume for the placement
        check when *build_volume* is not given; an unknown printer
        simply skips that check.  Additionally, when kiln-pro is
        installed, the
        Pro+ enrichment pass consults the per-machine calibration log
        for measured drift on each wired feature class (wall /
        overhang / bridge / hole) and shifts the matching threshold
        before evaluating the report.  Absent calibration data leaves
        thresholds untouched; absent kiln-pro leaves the report
        unchanged.
    :param slicer_style: Support-pattern style the report's
        ``supports`` block should be calibrated for. One of ``"grid"``
        (PrusaSlicer default, OrcaSlicer rectilinear), ``"snug"``,
        ``"organic"`` (PrusaSlicer 2.6+ / OrcaSlicer organic), or
        ``"tree"`` (Cura tree). Public-tier behavior is unaffected —
        public Kiln returns the naive area×height estimate regardless.
        When kiln-pro is installed (Pro+ tier), this hint is forwarded
        to the overlay's supports-calibration module which translates
        the naive number into expected slicer extrusion volume for the
        chosen style (Grid ÷ 2.0, Snug ÷ 3.0, Organic / Tree ÷ 5.0 per
        the 2026-05-17 audit's empirical divisors). See
        ``enrichment.supports_calibration`` on the returned report.
    :returns: A :class:`PrintabilityReport` with scores, grades, and
        recommendations.  When the kiln-pro package is installed (Pro+
        tier), the report is enriched with material-specific tuning
        and the ``enrichment`` field is populated; free / public
        installs see the safety-floor result unchanged.  See
        https://kiln3d.com for tier details.
    :raises ValueError: If the file cannot be parsed.
    """
    from kiln.design_intelligence import load_pro_overlay_or_empty

    triangles, vertices = _parse_mesh(file_path)
    # Membranes must go BEFORE winding normalization: a zero-thickness
    # pair has no meaningful outward orientation to normalize.
    triangles = _drop_membrane_pairs(triangles)
    triangles = _normalize_triangle_winding(triangles)

    # Bounding box.
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    bbox = {
        "x_min": min(xs),
        "x_max": max(xs),
        "y_min": min(ys),
        "y_max": max(ys),
        "z_min": min(zs),
        "z_max": max(zs),
    }
    # The plate the model rests on comes from geometry, not from the
    # parsed vertex list, which may carry vertices no face references.
    z_min = _mesh_bed_z(triangles)

    # Soft tier seam: load the printability_judgment overlay once and
    # pass it to each material-derived sub-analysis. Empty overlay ->
    # free-tier safe defaults; populated -> curated thresholds + tuned
    # recommendation templates from Pro+.
    judgment_overlay = load_pro_overlay_or_empty("printability_judgment")

    # Resolve the effective overhang threshold ONCE so both
    # ``_analyze_overhangs`` and ``_analyze_supports`` see the same
    # value.  Without this, the support-volume estimator runs at the
    # universal 45° rule while the overhang detector uses the
    # per-material threshold — producing the contradictory report
    # "TPU at 38° needs_supports=True, but supports.estimated_support_volume_mm3=0"
    # because 38° is below the supports estimator's 45° floor.  Pin
    # them together so the verdict and the volume estimate agree.
    _resolved_threshold = _resolve_overhang_threshold(
        max_overhang_angle, material, judgment_overlay,
    )
    overhangs = _analyze_overhangs(
        triangles,
        max_overhang_angle=_resolved_threshold,
        z_min=z_min,
        layer_height=layer_height,
        normalize_winding=False,
        material=material,
        overlay=judgment_overlay,
    )
    thin_walls = _analyze_thin_walls(triangles, vertices, nozzle_diameter=nozzle_diameter)
    cavities = _analyze_cavity_widths(triangles, vertices, nozzle_diameter=nozzle_diameter)

    # Connected-component count: one closed body = 1; a multi-body mesh
    # = N components.  Exposed for the kiln-pro overlay.
    #
    # ``component_size_uniformity`` is a 0–1 scalar describing how
    # similar the components are in volume — 1.0 = all components have
    # identical bbox volume; near 0.0 = one big component plus tiny
    # ones.  Exposed for the kiln-pro overlay.
    component_count = 0
    component_size_uniformity = 0.0
    mesh_genus = 0
    # Numpy view of triangles, kept around so the kiln-pro enrichment
    # call can pass it through to the Pro+ rod-feature analyzer
    # (computed there, not here — the algorithm is a Pro-tier wedge).
    tris_arr_for_pro: np.ndarray | None = None
    if len(triangles) > 0:
        try:
            tris_arr = np.asarray(triangles, dtype=np.float64)
            comp_ids = _label_mesh_components(tris_arr)
            component_count = int(comp_ids.max() + 1)
            mesh_genus = _compute_mesh_genus(tris_arr, component_count)
            tris_arr_for_pro = tris_arr
            if component_count >= 2:
                # Per-component bbox volume: max-min along each axis.
                # Coefficient of variation (std / mean) measures spread;
                # uniformity = 1 - clamp(cv, 0, 1) maps to 0–1 with 1 =
                # all components identical.
                tri_min = tris_arr.min(axis=1)  # (T, 3) per-tri min
                tri_max = tris_arr.max(axis=1)  # (T, 3) per-tri max
                comp_bbox_volumes = np.zeros(component_count)
                for cid in range(component_count):
                    mask = comp_ids == cid
                    if not mask.any():
                        continue
                    lo = tri_min[mask].min(axis=0)
                    hi = tri_max[mask].max(axis=0)
                    extents = np.maximum(hi - lo, 1e-12)
                    comp_bbox_volumes[cid] = float(extents.prod())
                mean_vol = float(comp_bbox_volumes.mean())
                if mean_vol > 0:
                    std_vol = float(comp_bbox_volumes.std())
                    cv = std_vol / mean_vol
                    component_size_uniformity = float(
                        max(0.0, min(1.0, 1.0 - cv))
                    )
            else:
                # Single-component mesh: uniformity is trivially 1.0
                # (one component, no spread).  The secondary signal
                # is only meaningful when n_components >= 2.
                component_size_uniformity = 1.0
        except (ValueError, IndexError):
            component_count = 0
            component_size_uniformity = 0.0
            mesh_genus = 0
            tris_arr_for_pro = None
    downward_regions = _analyze_downward_regions(
        triangles, z_min, layer_height=layer_height,
    )
    bridging = _analyze_bridging(
        triangles,
        z_min,
        layer_height=layer_height,
        normalize_winding=False,
        precomputed_regions=downward_regions,
    )
    bed_adhesion = _analyze_bed_adhesion(triangles, z_min, bbox, layer_height=layer_height)
    supports = _analyze_supports(
        triangles,
        z_min,
        max_overhang_angle=_resolved_threshold,
        layer_height=layer_height,
        normalize_winding=False,
        bbox=bbox,
    )

    # Bridge-aware overhang verdict.  When _analyze_supports's
    # ``likely_substituted_by_bridge`` test confirms the overhang is a
    # genuine two-sided bridge — part material anchors it on opposite
    # sides with a spannable gap between them — AND the overhang is
    # horizontal (``max_overhang_angle`` ≥ 89° — bridging only applies
    # to near-flat overhangs), the user does not need supports for it:
    # the slicer will bridge it cleanly without intervention.
    #
    # Flagging ``needs_supports=True`` in that case is a false positive
    # that costs filament + post-processing on a print PrusaSlicer
    # would have nailed.  Two independent gates (the two-sided-anchor
    # geometry test in ``_likely_bridge_substituted`` + the
    # horizontal-overhang check) are conservative — a cantilever,
    # anchored on one side and impossible to bridge, fails the geometry
    # test and correctly keeps ``needs_supports=True``.
    #
    # Note: this global test complements the per-region span analysis
    # below — it confirms whole-part tabletop bridges (which can be a
    # single region wider than the lateral-reach rule allows) via
    # solid-probing under the overhang footprint.
    #
    # Addresses the 5/64 bridge false positives surfaced by the
    # 2026-05-17 PrusaSlicer cross-validation (C03/C04 square_bridge,
    # C09 U_upside_down, F01/F02 tabletop).
    _bridge_substituted_overhang_pct: float | None = None
    if (
        overhangs.needs_supports
        and overhangs.max_overhang_angle >= 89.0
        and supports.likely_substituted_by_bridge
    ):
        from dataclasses import replace
        # Capture the pre-downgrade overhang percentage so the
        # recommendation below can name it ("slicer will bridge the
        # 7% horizontal overhang..."), giving the user visibility
        # into why no supports are needed.  Silent downgrade is
        # worse UX than the pre-fix "needs supports + likely bridge"
        # pair — at least the old version surfaced both signals,
        # even if contradictory.
        _bridge_substituted_overhang_pct = overhangs.overhang_percentage
        overhangs = replace(overhangs, needs_supports=False)

    # Per-region self-supporting exemption.  Overhang triangles that
    # belong to a downward region every deck point of which either
    # bridges (supported chord ≤ 10 mm) or is within lateral reach of
    # a supported edge — thread reliefs, small flange lips, recess
    # ceilings, chamfer undersides — print cleanly with no supports.
    # They stay in the reported ``overhang_triangle_count`` (the
    # geometry is real) but are excluded from the score deduction and,
    # when ALL overhangs are self-supporting, from the
    # ``needs_supports`` verdict.  This is the general form of the
    # micro-feature problem: a model studded with hundreds of tiny
    # self-supporting undersides used to fail with an F while any
    # slicer printed it support-free.
    _self_supporting_overhang_count = 0
    _overhang_scoring_pct: float | None = None
    if overhangs.needs_supports and downward_regions:
        # Only regions with a measured bridge deck qualify: a region
        # with no near-flat faces (a plain steep slope) was never
        # span-verified, so it keeps its support-needing status.
        exempt_indices = [
            gi
            for region in downward_regions
            if region.flat_indices and not region.needs_supports
            for gi in region.triangle_indices
        ]
        for gi in exempt_indices:
            tri = triangles[gi]
            nn = _normalize(_triangle_normal(tri[0], tri[1], tri[2]))
            if nn[2] >= 0:
                continue
            angle_from_down = math.degrees(
                math.acos(max(-1.0, min(1.0, -nn[2])))
            )
            overhang_angle = max(0.0, 90.0 - angle_from_down)
            if overhang_angle + 1e-9 < _resolved_threshold:
                continue
            _self_supporting_overhang_count += 1
        if _self_supporting_overhang_count > 0:
            remaining = max(
                0,
                overhangs.overhang_triangle_count
                - _self_supporting_overhang_count,
            )
            if remaining == 0:
                from dataclasses import replace
                overhangs = replace(overhangs, needs_supports=False)
            elif len(triangles) > 0:
                _overhang_scoring_pct = round(
                    remaining / len(triangles) * 100.0, 1,
                )

    # Free-air overhang angle: the steepest overhang that genuinely
    # hangs in air.  Excludes (a) faces of self-supporting regions
    # (short bridges, lateral closes, boolean seams, bed-proximate
    # ceilings) and (b) flat bridge decks, which are judged by SPAN,
    # not angle.  ``max_overhang_angle`` reads 90 on any part with any
    # ceiling anywhere, so per-material angle limits compared against
    # it fired as noise on trivially printable parts; this is the
    # number they should judge.
    _exempt_overhang_tris = {
        gi
        for region in downward_regions
        if region.flat_indices and not region.needs_supports
        for gi in region.triangle_indices
    }
    _free_air_max_deg = 0.0
    for _gi, _tri in enumerate(triangles):
        if _gi in _exempt_overhang_tris:
            continue
        if _is_bed_supported_triangle(_tri, z_min, layer_height):
            continue
        _nn = _normalize(_triangle_normal(_tri[0], _tri[1], _tri[2]))
        if _nn[2] >= 0 or _nn[2] <= _BRIDGE_FLAT_NZ_LIMIT:
            continue
        _afd = math.degrees(math.acos(max(-1.0, min(1.0, -_nn[2]))))
        _oa = max(0.0, 90.0 - _afd)
        if _oa > _free_air_max_deg:
            _free_air_max_deg = _oa
    from dataclasses import replace as _dc_replace
    overhangs = _dc_replace(
        overhangs, max_free_air_overhang_deg=round(_free_air_max_deg, 1),
    )

    # Detect cylindrical-hole features.  Wrapped in try/except — a
    # malformed mesh or coarse triangulation can raise inside the
    # detector, but a hole-detection failure must never break the
    # wider printability path.  Empty list is the documented degraded
    # output, matching the contract the kiln-pro overlay engine
    # expects when reading ``report["holes"]``.
    holes: list[dict[str, Any]] = []
    hole_diagnostics: dict[str, int] = {}
    if include_hole_detection:
        from kiln.generation.validation import detect_holes
        try:
            holes = detect_holes(file_path, diagnostics=hole_diagnostics)
        except (ValueError, FileNotFoundError) as exc:
            logger.debug(
                "detect_holes failed silently on %s: %s",
                file_path, exc,
            )

    warping = _analyze_warping(
        triangles, vertices, bbox, material=material, overlay=judgment_overlay,
    )
    thermal_stress = _analyze_thermal_stress(
        triangles, bbox, material=material, layer_height=layer_height,
        overlay=judgment_overlay,
    )
    adhesion_force = _estimate_adhesion_force(
        bed_adhesion.contact_area_mm2, bbox, material=material,
        overlay=judgment_overlay,
    )

    score = _compute_score(
        overhangs, thin_walls, bridging, bed_adhesion, supports,
        warping=warping, thermal_stress=thermal_stress, adhesion_force=adhesion_force,
        overhang_scoring_pct=_overhang_scoring_pct,
    )
    grade = _score_to_grade(score)
    recommendations = _build_recommendations(
        overhangs, thin_walls, bridging, bed_adhesion, supports,
        warping=warping, thermal_stress=thermal_stress, adhesion_force=adhesion_force,
    )

    # Surface the bridge-substitution downgrade.  When the
    # ``needs_supports`` verdict was downgraded above, give the user
    # an explicit "the slicer will bridge this" line so they
    # understand WHY no supports are needed despite the horizontal
    # overhang in the geometry.  Silent downgrade is worse UX than
    # "needs supports + slicer will bridge" was — at least the old
    # version told the user something.
    if _self_supporting_overhang_count > 0:
        recommendations.insert(
            0,
            f"{_self_supporting_overhang_count} small overhang face(s) "
            f"sit on self-supporting features (short bridges or "
            f"lateral-reach lips) — the slicer prints these without "
            f"supports; they are excluded from the score."
        )
    if _bridge_substituted_overhang_pct is not None:
        recommendations.insert(
            0,
            f"Slicer will likely bridge the "
            f"{_bridge_substituted_overhang_pct:.0f}% horizontal overhang "
            f"without supports — no action needed.  Force-enable supports "
            f"in your slicer if the underside is a show surface and you "
            f"want a smoother finish."
        )

    # Hole-detection diagnostic notices: surface features the detector
    # silently dropped.  Each fires only when its counter is non-zero.
    # Sub-floor rejects split round vs polygonal so the wording
    # matches what the user actually has: a round bore can be drilled
    # after printing, a hex pocket cannot — so a single "enlarge in
    # CAD or drill" recommendation would be wrong for polygonal
    # pockets.
    sub_floor_round = hole_diagnostics.get("sub_floor_clusters", 0)
    sub_floor_polygonal = hole_diagnostics.get(
        "sub_floor_polygonal_clusters", 0,
    )
    if sub_floor_round > 0 and sub_floor_polygonal > 0:
        recommendations.append(
            f"Detected {sub_floor_round + sub_floor_polygonal} "
            f"feature(s) below the 0.8 mm hole-detection floor "
            f"({sub_floor_round} round bore(s) + "
            f"{sub_floor_polygonal} polygonal pocket(s)). FDM with "
            "a 0.4 mm nozzle cannot reliably print details below "
            "~1 mm; enlarge round bores in CAD (or drill after "
            "printing) and review polygonal pockets — they may be "
            "intentional wrench grips that need to stay tight."
        )
    elif sub_floor_round > 0:
        recommendations.append(
            f"Detected {sub_floor_round} circular feature(s) below "
            "the 0.8 mm hole-detection floor. FDM with a 0.4 mm "
            "nozzle cannot reliably print holes below ~1 mm "
            "regardless of material; enlarge in CAD or drill after "
            "printing."
        )
    elif sub_floor_polygonal > 0:
        recommendations.append(
            f"Detected {sub_floor_polygonal} polygonal pocket(s) "
            "below the 0.8 mm hole-detection floor (likely hex "
            "nut traps or setscrew sockets). Drilling won't help "
            "— either enlarge the pocket in CAD or accept that the "
            "FDM-printed walls won't grip a fastener cleanly."
        )
    non_circular_count = hole_diagnostics.get("non_circular_clusters", 0)
    if non_circular_count > 0:
        recommendations.append(
            f"Detected {non_circular_count} non-circular feature(s) "
            "(slot, elliptical relief, or chamfered hole entry). "
            "Per-material hole-floor warnings only apply to cylindrical "
            "features — verify slot widths against your material's "
            "minimum feature size manually."
        )

    # Upsell hook: when the judgment overlay is absent, the
    # warping / thermal-stress / adhesion-force recommendations come
    # from the public safety-floor templates. With the overlay, curated /
    # printer-tuned / brand-aware guidance. One non-intrusive line.
    if not judgment_overlay:
        recommendations.append(
            "Curated, brand-tuned recommendations (printer-specific bed temps, "
            "enclosure choice, filament profile) are available with Kiln Pro."
        )

    # Estimate print time modifier: supports and bridges add time.
    time_mod = 1.0
    if supports.support_percentage > 0:
        time_mod += supports.support_percentage / 100.0 * 0.5
    if bridging.bridge_count > 0:
        time_mod += 0.05

    # Placement check — where the part sits, not only how it is shaped.
    # The validation pipeline's bundle-sourced path calls this same
    # helper, so a report read from a bundle cannot reach a different
    # placement verdict than one analyzed here.
    score, grade, printable, placement_faults = _apply_placement_check(
        score,
        recommendations,
        bbox,
        build_volume=build_volume,
        printer_id=printer_id,
    )

    model_height = bbox["z_max"] - bbox["z_min"]

    # Cost analysis (gracefully degrades if unavailable).
    cost: CostAnalysis | None = None
    with contextlib.suppress(Exception):
        cost = _analyze_cost(
            None,  # mesh object unused — estimator reloads from file
            file_path,
            material=material,
            infill_percent=infill_percent,
            needs_supports=overhangs.needs_supports,
            adhesion_risk=bed_adhesion.adhesion_risk,
        )

    report = PrintabilityReport(
        printable=printable,
        score=score,
        grade=grade,
        overhangs=overhangs,
        thin_walls=thin_walls,
        bridging=bridging,
        bed_adhesion=bed_adhesion,
        supports=supports,
        warping=warping,
        thermal_stress=thermal_stress,
        adhesion_force=adhesion_force,
        cost=cost,
        cavities=cavities,
        dimensions_mm={
            "width_mm": round(bbox["x_max"] - bbox["x_min"], 3),
            "depth_mm": round(bbox["y_max"] - bbox["y_min"], 3),
            "height_mm": round(bbox["z_max"] - bbox["z_min"], 3),
        },
        model_height_mm=round(model_height, 2),
        recommendations=recommendations,
        estimated_print_time_modifier=round(time_mod, 2),
        holes=holes,
        triangle_count=len(triangles),
        connected_components=component_count,
        component_size_uniformity=round(component_size_uniformity, 3),
        genus=mesh_genus,
    )

    # Optional kiln-pro enrichment: when the kiln-pro package is
    # installed, material-aware thresholds and SME-tuned scoring
    # weights are layered onto the safety-floor result.  The overlay
    # returns the input unchanged for unknown materials, so the call
    # is safe to make unconditionally.  Free / public installs hit
    # the ImportError branch and see the unmodified report.  Pro
    # enrichment is a kiln-pro Pro+ feature — see kiln3d.com.
    try:
        from kiln_pro.bridge import pro_features
    except ImportError:
        pass
    else:
        if pro_features.is_available("printability_overlay"):
            try:
                enriched = pro_features.printability_overlay.enrich_printability_report(
                    report.to_dict(),
                    material=material,
                    printer_id=printer_id,
                    nozzle_diameter_mm=nozzle_diameter,
                    slicer_style=slicer_style,
                    mesh_triangles=tris_arr_for_pro,
                )
            except TypeError:
                # Older kiln-pro that pre-dates one of the newer kwargs.
                # Retry shedding the freshest one first, then the next,
                # so this public surface stays forward-compatible with
                # multiple kiln-pro vintages.  ``mesh_triangles`` is the
                # 2026-05-19 addition; ``nozzle_diameter_mm`` /
                # ``slicer_style`` arrived earlier.  When the installed
                # kiln-pro picks up each parameter, the user's nozzle
                # starts scaling per-material floors, supports_calibration
                # starts shipping in the enrichment block, and rod_features
                # starts populating on rod-like meshes.
                enriched = None
                for kwargs in (
                    {
                        "material": material,
                        "printer_id": printer_id,
                        "nozzle_diameter_mm": nozzle_diameter,
                        "slicer_style": slicer_style,
                    },
                    {
                        "material": material,
                        "printer_id": printer_id,
                    },
                ):
                    try:
                        enriched = pro_features.printability_overlay.enrich_printability_report(
                            report.to_dict(),
                            **kwargs,
                        )
                        break
                    except TypeError:
                        continue
                    except Exception:  # noqa: BLE001
                        enriched = None
                        break
            except Exception:  # noqa: BLE001
                # Overlay failure must never break the public path.
                enriched = None
            if isinstance(enriched, dict) and "enrichment" in enriched:
                report.enrichment = enriched.get("enrichment")
                # Mirror the overlay's recomputed top-level fields onto
                # the dataclass so dict-consumers and dataclass-consumers
                # agree.  Other nested analysis blocks (overhangs,
                # thin_walls, etc.) remain authoritative on the dataclass.
                if "score" in enriched:
                    report.score = int(enriched["score"])
                if "grade" in enriched:
                    report.grade = str(enriched["grade"])
                if "printable" in enriched:
                    report.printable = bool(enriched["printable"])
                if isinstance(enriched.get("recommendations"), list):
                    report.recommendations = list(enriched["recommendations"])

    # Safety floor.  The overlay above recomputes score / grade /
    # printable from its own analysis and writes them straight onto the
    # report — which silently undid the placement verdict and handed an
    # off-bed or oversized part back as a printable A.  Placement is
    # physics, not tuning, so it gets the last word on every tier.
    # Clamping (never raising) keeps this idempotent: re-applying the
    # floor cannot deduct twice.
    if placement_faults:
        report.score = max(0, min(report.score, score))
        report.grade = _score_to_grade(report.score)
        report.printable = False
        for fault in reversed(placement_faults):
            if fault not in report.recommendations:
                report.recommendations.insert(0, fault)

    return report


# ---------------------------------------------------------------------------
# Adhesion intelligence
# ---------------------------------------------------------------------------


def is_bedslinger(printer_id: str | None) -> bool:
    """Return True if *printer_id* is a known bed-slinger printer."""
    if not printer_id:
        return False
    return printer_id.lower().replace("-", "_").strip() in _BEDSLINGER_PRINTERS


def recommend_adhesion(
    bed_adhesion: BedAdhesionAnalysis,
    *,
    material: str = "PLA",
    has_enclosure: bool = False,
    is_bedslinger_printer: bool = False,
    model_height_mm: float = 0.0,
) -> AdhesionRecommendation:
    """Recommend brim/raft settings based on model geometry + material + printer.

    Uses the contact percentage and adhesion risk from
    :class:`BedAdhesionAnalysis` combined with material warping tendency
    and printer type to produce actionable slicer overrides.

    :param bed_adhesion: Output from ``_analyze_bed_adhesion()``.
    :param material: Filament type (PLA, ABS, PETG, etc.).
    :param has_enclosure: Whether the printer has an enclosure.
    :param is_bedslinger_printer: Whether the printer is a bed-slinger.
    :param model_height_mm: Model height for tall-part brim logic.
    :returns: :class:`AdhesionRecommendation` with slicer overrides.
    """
    mat_upper = material.upper()
    is_warp_material = mat_upper in _HIGH_WARP_MATERIALS
    pct = bed_adhesion.contact_percentage
    risk = bed_adhesion.adhesion_risk

    brim = 0
    raft = False
    rationale = ""

    # Decision matrix — first match wins
    if pct < 2.0:
        brim = 8
        raft = is_warp_material
        rationale = (
            f"Tiny contact area ({pct:.1f}% of footprint) — extreme adhesion risk. "
            f"{'Raft recommended for warping material.' if raft else '8mm brim mandatory.'}"
        )
    elif pct < 5.0 and is_warp_material:
        brim = 8
        raft = True
        rationale = f"Low contact ({pct:.1f}%) with {mat_upper} (high warp) — raft recommended."
    elif pct < 5.0 and (is_bedslinger_printer or not has_enclosure):
        brim = 8
        rationale = f"Low contact ({pct:.1f}%) on {'bed-slinger' if is_bedslinger_printer else 'open-frame'} printer — 8mm brim."
    elif pct < 5.0:
        brim = 5
        rationale = f"Low contact area ({pct:.1f}%) — 5mm brim recommended."
    elif risk == "medium" and is_warp_material:
        brim = 8
        rationale = f"Moderate contact with {mat_upper} (high warp) — wide 8mm brim."
    elif risk == "medium" and is_bedslinger_printer:
        brim = 5
        rationale = "Moderate contact on bed-slinger — 5mm brim for safety."
    elif risk == "medium":
        brim = 3
        rationale = f"Moderate bed contact ({pct:.1f}%) — 3mm brim recommended."
    elif is_warp_material and model_height_mm > 50.0:
        brim = 5
        rationale = f"Tall model ({model_height_mm:.0f}mm) with {mat_upper} — precautionary 5mm brim."
    else:
        rationale = f"Good bed contact ({pct:.1f}%), no brim needed."

    # Build slicer overrides
    overrides: dict[str, str] = {}
    if brim > 0:
        overrides["brim_width"] = str(brim)
        overrides["brim_type"] = "outer_only"
    if raft:
        overrides["raft_layers"] = "3"

    return AdhesionRecommendation(
        brim_width_mm=brim,
        use_raft=raft,
        adhesion_risk=risk,
        contact_percentage=pct,
        rationale=rationale,
        slicer_overrides=overrides,
    )


# ---------------------------------------------------------------------------
# Failure diagnosis
# ---------------------------------------------------------------------------


def diagnose_from_signals(
    signals: dict[str, Any],
    *,
    printer_id: str | None = None,
    material: str | None = None,
) -> PrintFailureDiagnosis:
    """Produce a failure diagnosis from collected physical signals.

    This is pure logic — no I/O, no adapter calls — making it easy to test.
    The ``signals`` dict is assembled by the MCP tool from printer state,
    model analysis, gcode metadata, and printer intelligence.

    :param signals: Dict of signal values (see source for expected keys).
    :param printer_id: Printer model identifier for context.
    :param material: Effective material string (e.g. "PLA", "ABS").
    :returns: :class:`PrintFailureDiagnosis`.
    """
    causes: list[str] = []
    fixes: list[str] = []
    category = "unknown"
    confidence = 0.3
    slicer_overrides: dict[str, str] = {}

    mat_upper = (material or "").upper()
    is_warp = mat_upper in _HIGH_WARP_MATERIALS

    # --- Signal extraction (safe defaults) ---
    adhesion_risk = signals.get("adhesion_risk")
    contact_pct = signals.get("contact_percentage")
    tool_actual = signals.get("tool_temp_actual")
    tool_target = signals.get("tool_temp_target")
    print_error = signals.get("print_error")
    overhang_pct = signals.get("overhang_pct", 0.0)
    max_bridge = signals.get("max_bridge_mm", 0.0)
    has_enclosure = signals.get("printer_has_enclosure")
    intel_modes: list[dict[str, str]] = signals.get("failure_modes_from_intel") or []

    # --- Priority 1: Adhesion failure ---
    if adhesion_risk == "high" or (contact_pct is not None and contact_pct < 5.0):
        category = "adhesion"
        confidence = 0.85 if (contact_pct is not None and contact_pct < 3.0) else 0.70
        pct_str = f"{contact_pct:.1f}%" if contact_pct is not None else "unknown"
        causes.append(
            f"Insufficient bed contact area ({pct_str} of bounding box footprint). "
            f"Model likely has small or lattice-like contact points."
        )
        fixes.append("Add a brim (5-8mm) to increase first-layer adhesion surface.")
        fixes.append("Re-orient the model to maximize the flat base area.")
        if is_warp:
            fixes.append(f"{mat_upper} is prone to warping — consider a raft or enclosed printer.")
            confidence = min(confidence + 0.10, 0.95)

        # Compute slicer override
        if contact_pct is not None and contact_pct < 5.0:
            slicer_overrides["brim_width"] = "8"
        else:
            slicer_overrides["brim_width"] = "5"
        slicer_overrides["brim_type"] = "outer_only"

    # --- Priority 2: Thermal anomaly ---
    elif (
        tool_actual is not None
        and tool_target is not None
        and abs(tool_actual - tool_target) > 10.0
    ) or (print_error is not None and print_error != 0):
        category = "thermal"
        confidence = 0.75
        if tool_actual is not None and tool_target is not None:
            delta = tool_actual - tool_target
            causes.append(
                f"Hotend temperature anomaly: actual {tool_actual:.0f}°C vs target {tool_target:.0f}°C "
                f"(delta {delta:+.0f}°C)."
            )
            if delta < 0:
                fixes.append("Check heater cartridge and thermistor connections.")
                fixes.append("PID tune the hotend for stable temperature.")
            else:
                fixes.append("Check for thermistor fault or thermal runaway condition.")
        if print_error is not None and print_error != 0:
            causes.append(f"Printer error code: {print_error}.")
            fixes.append("Check printer display for specific error details.")

    # --- Priority 3: Geometry-induced failure ---
    elif overhang_pct > 30.0 or max_bridge > 15.0:
        category = "geometry"
        confidence = 0.65
        if overhang_pct > 30.0:
            causes.append(f"High overhang percentage ({overhang_pct:.0f}%) — unsupported areas may droop or fail.")
            fixes.append("Enable supports in slicer settings.")
            fixes.append("Re-orient the model to reduce overhangs below 45°.")
        if max_bridge > 15.0:
            causes.append(f"Long bridging span ({max_bridge:.1f}mm) — may sag or fail mid-air.")
            fixes.append("Enable supports for bridge areas.")
            fixes.append("Reduce bridge spans by re-orienting or splitting the model.")

    # --- Priority 4: Material-environment mismatch ---
    elif is_warp and has_enclosure is False:
        category = "mechanical"
        confidence = 0.70
        causes.append(
            f"{mat_upper} on an open-frame printer — drafts and ambient cooling "
            f"cause warping, layer splitting, and adhesion failure."
        )
        fixes.append(f"Use an enclosure for {mat_upper} printing.")
        fixes.append("Increase bed temperature by 5-10°C for better adhesion.")
        fixes.append("Add a wide brim (8mm) to counteract warping forces.")
        slicer_overrides["brim_width"] = "8"
        slicer_overrides["brim_type"] = "outer_only"

    # --- Fallback: surface printer intelligence failure modes ---
    if not causes and intel_modes:
        for mode in intel_modes[:3]:
            causes.append(mode.get("cause", mode.get("symptom", "Unknown cause")))
            fix = mode.get("fix")
            if fix:
                fixes.append(fix)
        if causes:
            confidence = 0.50

    if not causes:
        causes.append("No clear failure cause identified from available signals.")
        fixes.append("Capture a photo of the failed print for visual diagnosis.")
        fixes.append("Check bed leveling and first-layer calibration.")

    return PrintFailureDiagnosis(
        failure_category=category,
        probable_causes=causes,
        recommended_fixes=fixes,
        confidence=confidence,
        signals=signals,
        slicer_overrides=slicer_overrides,
    )
