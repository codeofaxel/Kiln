"""Printability analysis engine for 3D models.

Analyzes STL/OBJ meshes for FDM printing readiness: overhang detection,
thin wall analysis, bridging assessment, bed adhesion surface estimation,
and support volume estimation. Uses only stdlib (struct, math) -- no
external mesh libraries.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from kiln import _vec
from kiln.generation.validation import _parse_obj, _parse_stl

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
class BridgingAnalysis:
    """Results of bridging assessment."""

    max_bridge_length_mm: float
    bridge_count: int
    needs_supports_for_bridges: bool

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
    """Results of support volume estimation."""

    estimated_support_volume_mm3: float
    support_percentage: float  # % of model volume
    support_regions: list[dict[str, float]]

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
    """Force-balance adhesion prediction."""

    adhesion_force_n: float  # estimated adhesion force in Newtons
    peel_force_n: float  # estimated thermal peel force in Newtons
    force_ratio: float  # adhesion / peel — >1.0 means adhesion wins
    will_detach: bool  # True if peel_force > adhesion_force
    risk_level: str  # "secure", "marginal", "likely_detach"
    score_deduction: int  # 0 to -10
    recommendations: list[str]

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
    model_height_mm: float = 0.0
    recommendations: list[str] = field(default_factory=list)
    estimated_print_time_modifier: float = 1.0  # 1.0 = normal
    # Detected cylindrical-hole features.  Each entry is a dict with
    # keys ``position`` (dict[x_mm,y_mm,z_mm]), ``diameter_mm``,
    # ``depth_mm``, ``axis`` (one of "x"/"y"/"z"), ``triangle_count``.
    # Populated by ``analyze_printability`` and consumed by the kiln-pro
    # printability overlay's hole-too-small material rule + the
    # per-machine "hole" calibration feature class.  Free-tier installs
    # see the list but no Pro tuning; Pro+ installs feed it into the
    # overlay enrichment pass.
    holes: list[dict[str, Any]] = field(default_factory=list)
    # Optional kiln-pro overlay block.  Populated by
    # ``analyze_printability`` when the kiln-pro package is installed
    # (Pro+ tier); absent on free / public installs.  See kiln3d.com
    # for tier details.
    enrichment: dict[str, Any] | None = None

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
            printable=bool(findings.get("printable", score >= 50)),
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
_HIGH_WARP_MATERIALS: frozenset[str] = frozenset({
    "ABS", "ASA", "PA", "PA6", "PA12", "PC", "ABS-CF", "ASA-CF",
})

# Known bed-slinger printers where Y-axis bed movement worsens adhesion.
_BEDSLINGER_PRINTERS: frozenset[str] = frozenset({
    "bambu_a1", "bambu_a1_mini",
    "ender3", "ender3_v2", "ender3_s1", "ender3_neo",
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
# the ``printability_judgment`` overlay (kiln_pro/data/
# printability_judgment_pro_overlay.json).

_WARPING_PUBLIC_DEFAULTS: dict[str, Any] = {
    "geometry_score_rules": [
        {"metric": "flat_area_total_mm2",  "operator": ">", "threshold": 20000.0, "score": 2},
        {"metric": "flat_area_total_mm2",  "operator": ">", "threshold": 2000.0,  "score": 1},
        {"metric": "height_to_base_ratio", "operator": ">", "threshold": 5.0,     "score": 2},
        {"metric": "height_to_base_ratio", "operator": ">", "threshold": 3.0,     "score": 1},
        {"metric": "sharp_corners_at_base","operator": ">", "threshold": 10,      "score": 1},
    ],
    "material_multipliers": {"low": 0.5, "moderate": 1.0, "high": 1.5, "very_high": 2.0},
    "risk_thresholds":      {"critical": 3.0, "high": 2.0, "moderate": 1.0},
    "score_deductions":     {"critical": -20, "high": -12, "moderate": -6, "low": 0},
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
    "risk_thresholds":  {"critical": 6.0, "high": 4.0, "moderate": 2.5},
    "score_deductions": {"critical": -15, "high": -10, "moderate": -5, "low": 0},
    "stress_zone_ratio_threshold": 2.0,
    "max_zones_tracked": 20,
    "max_zones_emitted": 10,
    "recommendation_rules": [
        {"metric": "risk_level",            "operator": "in", "threshold": ["high", "critical"],
         "template": "Significant cross-section changes detected (max ratio {max_ratio:.1f}x). Gradual geometry transitions reduce internal stress — consider adding fillets or chamfers."},
        {"metric": "risk_level",            "operator": "==", "threshold": "critical",
         "template": "Critical thermal stress risk. Slow print speed in transition zones and increase layer cooling fan gradually."},
        {"metric": "amplification_active",  "operator": "==", "threshold": True,
         "template": "Material ({material}) amplifies thermal stress (factor {stress_factor:.1f}x). Print in an enclosure and reduce temperature gradients."},
        {"metric": "risk_level",            "operator": "==", "threshold": "moderate",
         "template": "Moderate cross-section variation. Adding transition layers or adjusting print speed at geometry changes can help."},
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
    "recommendation_rules": [
        {"metric": "risk_level", "operator": "==", "threshold": "likely_detach",
         "template": "Part will likely detach during printing. Use a brim (8mm+), glue stick, or raft."},
        {"metric": "risk_level", "operator": "==", "threshold": "marginal",
         "template": "Adhesion is borderline. Adding a 5mm brim is recommended."},
        {"metric": "is_poor_adhesion_material", "operator": "==", "threshold": True,
         "template": "Material ({material}) has very poor adhesion on standard build surfaces. Use a specialized build sheet (e.g., Garolite for nylon, PP sheet for PP)."},
    ],
}


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


def _normalize_triangle_winding(
    triangles: list[tuple[tuple[float, ...], ...]],
) -> list[tuple[tuple[float, ...], ...]]:
    """Orient triangle winding outward using a mesh-center heuristic.

    STL files often contain inconsistent or inverted winding, which causes
    naive normal-based overhang and bridge analysis to treat top surfaces as
    unsupported ceilings.  For printability heuristics we only need a stable
    approximation, so we flip triangles whose normals point toward the mesh
    center rather than away from it.
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


def _is_bed_supported_triangle(
    tri: tuple[tuple[float, ...], ...],
    z_min: float,
    layer_height: float,
) -> bool:
    """Return True when a triangle is effectively resting on the build plate."""
    threshold = z_min + layer_height * 2.0
    return all(v[2] <= threshold for v in tri)


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
    max_overhang_angle: float = 45.0,
    z_min: float | None = None,
    layer_height: float = 0.2,
    normalize_winding: bool = True,
) -> OverhangAnalysis:
    """Detect overhanging triangles.

    A triangle is an overhang if its normal points downward (negative Z
    component) and the face angle from vertical exceeds
    ``max_overhang_angle``.
    """
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
        if overhang_angle < max_overhang_angle:
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


def _analyze_thin_walls(
    triangles: list[tuple[tuple[float, ...], ...]],
    vertices: list[tuple[float, ...]],
    *,
    nozzle_diameter: float = 0.4,
) -> ThinWallAnalysis:
    """Detect thin walls using edge-length approximation.

    Analyzes the shortest edge in each triangle as a proxy for wall
    thickness.  True ray-casting would require a spatial index, so we
    use edge lengths as an approximation that works well for common
    FDM geometries.
    """
    thin_count = 0
    min_thickness = float("inf")
    problematic: list[dict[str, float]] = []
    total = len(triangles)

    for tri in triangles:
        # Compute the three edge lengths.
        e1 = _vertex_distance(tri[0], tri[1])
        e2 = _vertex_distance(tri[1], tri[2])
        e3 = _vertex_distance(tri[2], tri[0])
        shortest = min(e1, e2, e3)

        if shortest < nozzle_diameter:
            thin_count += 1
            if shortest < min_thickness:
                min_thickness = shortest
            centroid = _triangle_centroid(tri[0], tri[1], tri[2])
            if len(problematic) < 10:
                problematic.append(
                    {
                        "x": round(centroid[0], 2),
                        "y": round(centroid[1], 2),
                        "z": round(centroid[2], 2),
                        "thickness_mm": round(shortest, 3),
                    }
                )

    if min_thickness == float("inf"):
        min_thickness = nozzle_diameter  # No thin walls found

    thin_pct = (thin_count / total * 100.0) if total > 0 else 0.0

    return ThinWallAnalysis(
        min_wall_thickness_mm=round(min_thickness, 3),
        thin_wall_count=thin_count,
        thin_wall_percentage=round(thin_pct, 1),
        problematic_regions=problematic[:5],
    )


def _analyze_bridging(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    *,
    layer_height: float = 0.2,
    normalize_winding: bool = True,
) -> BridgingAnalysis:
    """Detect unsupported horizontal spans (bridges).

    Identifies triangles with normals pointing nearly straight down
    that are above the first layer (not bed-touching).  Measures the
    longest edge of such triangles as the bridge length.
    """
    if normalize_winding:
        triangles = _normalize_triangle_winding(triangles)

    bridge_count = 0
    max_bridge_len = 0.0

    bed_threshold = z_min + layer_height * 2

    for tri in triangles:
        if _is_bed_supported_triangle(tri, z_min, layer_height):
            continue

        # Skip triangles near the bed (they're supported).
        centroid = _triangle_centroid(tri[0], tri[1], tri[2])
        if centroid[2] <= bed_threshold:
            continue

        n = _triangle_normal(tri[0], tri[1], tri[2])
        nn = _normalize(n)

        # Bridge: normal points nearly straight down (nz < -0.9).
        if nn[2] > -0.9:
            continue

        # Measure the longest edge as the bridge span.
        e1 = _vertex_distance(tri[0], tri[1])
        e2 = _vertex_distance(tri[1], tri[2])
        e3 = _vertex_distance(tri[2], tri[0])
        longest = max(e1, e2, e3)

        bridge_count += 1
        if longest > max_bridge_len:
            max_bridge_len = longest

    # Bridges > 10mm typically need supports.
    needs_supports = max_bridge_len > 10.0

    return BridgingAnalysis(
        max_bridge_length_mm=round(max_bridge_len, 2),
        bridge_count=bridge_count,
        needs_supports_for_bridges=needs_supports,
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


def _analyze_supports(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    *,
    max_overhang_angle: float = 45.0,
    layer_height: float = 0.2,
    normalize_winding: bool = True,
) -> SupportAnalysis:
    """Estimate support volume.

    For each overhang triangle, projects it downward to the build plate
    and estimates the support column volume as area x height.
    """
    if normalize_winding:
        triangles = _normalize_triangle_winding(triangles)

    support_volume = 0.0
    support_regions: list[dict[str, float]] = []

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
        if overhang_angle < max_overhang_angle:
            continue

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

    support_pct = (support_volume / model_volume * 100.0) if model_volume > 0 else 0.0

    return SupportAnalysis(
        estimated_support_volume_mm3=round(support_volume, 2),
        support_percentage=round(support_pct, 1),
        support_regions=support_regions[:5],
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
    material_multiplier = material_multipliers.get(tendency, 1.0)
    final_risk = geometry_score * material_multiplier

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

    for tri in triangles:
        centroid = _triangle_centroid(tri[0], tri[1], tri[2])
        bucket = int((centroid[2] - z_min) / layer_height)
        bucket = max(0, min(bucket, num_layers - 1))

        normal = _triangle_normal(tri[0], tri[1], tri[2])
        nn = _normalize(normal)
        area = _triangle_area(tri[0], tri[1], tri[2])
        xy_area = abs(nn[2]) * area
        layer_areas[bucket] += xy_area

    stress_zones: list[dict[str, float]] = []
    max_ratio = 1.0

    for i in range(1, num_layers):
        a_prev = layer_areas[i - 1]
        a_curr = layer_areas[i]
        bigger = max(a_prev, a_curr)
        smaller = min(a_prev, a_curr)
        ratio = bigger / max(smaller, 0.01)
        if ratio > max_ratio:
            max_ratio = ratio
        if ratio > zone_ratio_threshold and len(stress_zones) < max_tracked:
            stress_zones.append({
                "z_mm": round(z_min + i * layer_height, 2),
                "area_change_ratio": round(ratio, 2),
                "layer_area_mm2": round(a_curr, 2),
            })

    stress_zones.sort(key=lambda z: z["area_change_ratio"], reverse=True)
    stress_zones = stress_zones[:max_emitted]

    stress_factor = _material_stress_factor(material)
    combined_score = max_ratio * stress_factor

    risk_thresholds = cfg.get("risk_thresholds", {})
    if combined_score >= risk_thresholds.get("critical", 6.0):
        risk_level = "critical"
    elif combined_score >= risk_thresholds.get("high", 4.0):
        risk_level = "high"
    elif combined_score >= risk_thresholds.get("moderate", 2.5):
        risk_level = "moderate"
    else:
        risk_level = "low"

    score_deduction = int(cfg.get("score_deductions", {}).get(risk_level, 0))

    rec_metrics: dict[str, Any] = {
        "risk_level": risk_level,
        "max_ratio": max_ratio,
        "material": material,
        "stress_factor": stress_factor,
        "amplification_active": (stress_factor > 1.0 and risk_level != "low"),
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

    adhesion_force = contact_area_mm2 * adhesion_strength
    longest_xy = max(x_span, y_span)
    peel_force = shrinkage_strain * longest_xy * z_span * peel_scale
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
    min_base_dim = min(x_span, y_span)
    aspect_ratio = (z_span / min_base_dim) if min_base_dim > 0.1 else 0.0
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

    return AdhesionForceEstimate(
        adhesion_force_n=round(adhesion_force, 3),
        peel_force_n=round(peel_force, 3),
        force_ratio=round(force_ratio, 2),
        will_detach=will_detach,
        risk_level=risk_level,
        score_deduction=score_deduction,
        recommendations=recommendations,
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
) -> int:
    """Compute a printability score from 0-100.

    Starts at 100 and deducts points for each issue found.
    """
    score = 100

    # Overhang deductions (max -30)
    if overhangs.needs_supports:
        score -= min(30, int(overhangs.overhang_percentage * 0.5))

    # Thin wall deductions (max -25)
    if thin_walls.thin_wall_count > 0:
        score -= min(25, int(thin_walls.thin_wall_percentage * 0.5))

    # Bridging deductions (max -15)
    if bridging.bridge_count > 0:
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
    max_overhang_angle: float = 45.0,
    build_volume: tuple[float, float, float] | None = None,
    material: str = "pla",
    infill_percent: float = 20.0,
    include_hole_detection: bool = True,
    printer_id: str | None = None,
) -> PrintabilityReport:
    """Run a full printability analysis on a mesh file.

    :param file_path: Path to an STL or OBJ file.
    :param nozzle_diameter: Printer nozzle diameter in mm.
    :param layer_height: Print layer height in mm.
    :param max_overhang_angle: Max overhang angle (degrees) before
        supports are needed.
    :param build_volume: Optional (X, Y, Z) build volume in mm.  If
        provided, the report will warn if the model exceeds it.
    :param material: Material ID for warping and cost analysis (default ``"pla"``).
    :param infill_percent: Interior infill density (0-100) for cost estimation.
    :param include_hole_detection: When True (default), also run
        :func:`kiln.generation.validation.detect_holes` and surface the
        result on ``report.holes``.  Set False on perf-critical paths
        that don't need the per-hole list — hole detection re-parses
        the mesh internally, which roughly doubles the parse cost.
    :param printer_id: When supplied and kiln-pro is installed, the
        Pro+ enrichment pass consults the per-machine calibration log
        for measured drift on each wired feature class (wall /
        overhang / bridge / hole) and shifts the matching threshold
        before evaluating the report.  Absent calibration data leaves
        thresholds untouched; absent kiln-pro leaves the report
        unchanged.
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
    z_min = bbox["z_min"]

    # Soft tier seam: load the printability_judgment overlay once and
    # pass it to each material-derived sub-analysis. Empty overlay ->
    # free-tier safe defaults; populated -> curated thresholds + tuned
    # recommendation templates from Pro+.
    judgment_overlay = load_pro_overlay_or_empty("printability_judgment")

    overhangs = _analyze_overhangs(
        triangles,
        max_overhang_angle=max_overhang_angle,
        z_min=z_min,
        layer_height=layer_height,
        normalize_winding=False,
    )
    thin_walls = _analyze_thin_walls(triangles, vertices, nozzle_diameter=nozzle_diameter)
    bridging = _analyze_bridging(
        triangles,
        z_min,
        layer_height=layer_height,
        normalize_winding=False,
    )
    bed_adhesion = _analyze_bed_adhesion(triangles, z_min, bbox, layer_height=layer_height)
    supports = _analyze_supports(
        triangles,
        z_min,
        max_overhang_angle=max_overhang_angle,
        layer_height=layer_height,
        normalize_winding=False,
    )

    # Detect cylindrical-hole features.  Wrapped in try/except — a
    # malformed mesh or coarse triangulation can raise inside the
    # detector, but a hole-detection failure must never break the
    # wider printability path.  Empty list is the documented degraded
    # output, matching the contract the kiln-pro overlay engine
    # expects when reading ``report["holes"]``.
    holes: list[dict[str, Any]] = []
    if include_hole_detection:
        from kiln.generation.validation import detect_holes
        try:
            holes = detect_holes(file_path)
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
    )
    grade = _score_to_grade(score)
    recommendations = _build_recommendations(
        overhangs, thin_walls, bridging, bed_adhesion, supports,
        warping=warping, thermal_stress=thermal_stress, adhesion_force=adhesion_force,
    )

    # Free-tier upsell hook: when the judgment overlay is absent, the
    # warping / thermal-stress / adhesion-force recommendations come
    # from the public safety-floor templates. Pro+ supplies curated /
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

    # Build volume check.
    if build_volume is not None:
        bx, by, bz = build_volume
        dx = bbox["x_max"] - bbox["x_min"]
        dy = bbox["y_max"] - bbox["y_min"]
        dz = bbox["z_max"] - bbox["z_min"]
        if dx > bx or dy > by or dz > bz:
            recommendations.insert(
                0,
                f"Model ({dx:.1f} x {dy:.1f} x {dz:.1f} mm) exceeds build volume ({bx:.0f} x {by:.0f} x {bz:.0f} mm).",
            )
            score = max(0, score - 20)
            grade = _score_to_grade(score)

    printable = score >= 50

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
        model_height_mm=round(model_height, 2),
        recommendations=recommendations,
        estimated_print_time_modifier=round(time_mod, 2),
        holes=holes,
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
                )
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
