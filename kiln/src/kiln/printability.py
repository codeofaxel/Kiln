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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Literal

import numpy as np

from kiln import _vec, step_import
from kiln.generation.validation import (
    _SUPPORTED_MESH_FORMATS,
    _bed_threshold_z,
    _is_bed_supported_triangle,
    _mesh_bed_z,
    _parse_mesh_file,
    in_geometric_order,
    unprintable_geometry_reason,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


#: The score's own arithmetic: how many points each check took off, and the
#: factor the thermal check scaled its charge by.  Never served, at any
#: tier.  The score and the grade are the answer; a reply says what each
#: check found and what to do, and nothing reads the points behind it
#: (2026-10-23).  The in-process report keeps them for the checks that
#: reconcile the score (``tests/test_printability_score_is_its_stated_deductions.py``).
_SCORE_ARITHMETIC: frozenset[str] = frozenset({
    "score_deduction", "stop_deduction", "material_stress_factor",
})


def _served(analysis: Any) -> dict[str, Any]:
    """*analysis* as a reply carries it: what every ``to_dict`` below returns.

    The score's arithmetic (:data:`_SCORE_ARITHMETIC`) is never served.  A
    check on public Kiln's own floor serves everything else; its measurements
    are this file's.  A check Kiln Pro tuned (``analysis.tuned``) keeps its
    verdict, its reasons and what to do about them, and leaves out the
    numbers the tuning worked them out from (the class's ``_WORKED_OUT``),
    which stay in the process that worked them out.  ``tuned`` itself is
    never served.
    """
    out = asdict(analysis)
    out.pop("tuned", None)
    for key in _SCORE_ARITHMETIC:
        out.pop(key, None)
    if analysis.tuned:
        for key in analysis._WORKED_OUT:
            out.pop(key, None)
    return out


@dataclass
class OverhangAnalysis:
    """Results of overhang detection."""

    max_overhang_angle: float  # degrees from vertical; 90 = horizontal ceiling
    overhang_triangle_count: int
    overhang_percentage: float  # % of total triangles
    needs_supports: bool
    #: One entry per overhanging AREA (triangles sharing an edge), the
    #: largest first, at most five: ``{x, y, z, angle, area_mm2}``.
    worst_regions: list[dict[str, float]]
    #: Steepest overhang that actually hangs in free air: excludes
    #: faces of self-supporting regions (short bridges, lateral
    #: closes, boolean seams, bed-proximate ceilings) AND flat bridge
    #: decks, which are judged by span, not angle.  This is the number
    #: per-material overhang limits should be compared against —
    #: ``max_overhang_angle`` reads 90 on any part with any ceiling.
    max_free_air_overhang_deg: float = 0.0
    #: Always 0: the underside this block finds is charged once, by area,
    #: on the supports block (see _score_terms).
    score_deduction: int = 0
    #: Whether Kiln Pro tuned this check; see :func:`_served`.
    tuned: bool = field(default=False, repr=False, compare=False)

    _WORKED_OUT: ClassVar[tuple[str, ...]] = ("score_deduction",)

    def to_dict(self) -> dict[str, Any]:
        return _served(self)


@dataclass
class ThinWallAnalysis:
    """Results of thin wall detection.

    Every count and region in the block is relative to ONE threshold,
    ``threshold_mm``: the nozzle width, or — when kiln-pro supplies one
    (https://kiln3d.com) — the material's minimum wall, so this block and
    a material wall rule never disagree about whether a wall is thin.
    ``threshold_basis`` says which (``"nozzle"`` or ``"material"``).  A
    reply about a check Kiln Pro tuned says the basis and not the number
    (:func:`_served`).

    ``problematic_regions`` names every distinct thin feature — one
    thickness at one place — thinnest first, each with a position
    (``x``/``y``/``z``), its ``thickness_mm`` and how many measurement
    ``samples`` landed on it.  ``min_wall_thickness_mm`` is the thinnest
    wall measured anywhere, thin or not.
    """

    min_wall_thickness_mm: float
    thin_wall_count: int  # measured points thinner than threshold_mm
    thin_wall_percentage: float
    problematic_regions: list[dict[str, float]]
    threshold_mm: float = 0.0
    threshold_basis: str = "nozzle"
    score_deduction: int = 0  # what this block took off the score; see _score_terms
    #: Whether Kiln Pro tuned this check; see :func:`_served`.
    tuned: bool = field(default=False, repr=False, compare=False)

    _WORKED_OUT: ClassVar[tuple[str, ...]] = ("score_deduction", "threshold_mm")

    def to_dict(self) -> dict[str, Any]:
        return _served(self)


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
    sentinel when no cavities are detected on the mesh).  The kiln-pro
    overlay (https://kiln3d.com) flags a cavity narrower than the
    material's printable floor as a recess too fine to resolve — the
    feature fills in or loses its edges during printing.  It is never
    a wall verdict.  A void narrower than the slicer's crack-closing width,
    or one that tapers to nothing within the face it is measured from,
    is a tessellation crack rather than a cavity and does not register.
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
    score_deduction: int = 0  # what this block took off the score; see _score_terms
    #: Whether Kiln Pro tuned this check; see :func:`_served`.
    tuned: bool = field(default=False, repr=False, compare=False)

    _WORKED_OUT: ClassVar[tuple[str, ...]] = ("score_deduction",)

    def to_dict(self) -> dict[str, Any]:
        return _served(self)


@dataclass
class BedAdhesionAnalysis:
    """Results of bed adhesion surface estimation."""

    contact_area_mm2: float
    contact_percentage: float  # % of bounding box footprint
    adhesion_risk: str  # "low", "medium", "high"
    score_deduction: int = 0  # what this block took off the score; see _score_terms
    # % of the bounding box footprint that the contact's own bounding
    # rectangle covers: near 100 for a tube or a frame, whose contact runs
    # around its outline, and small for a part standing on one spot.
    # ``None`` on reports built directly by a client.
    contact_span_percentage: float | None = None
    # % of the bounding box footprint the contact holds down, the figure
    # ``adhesion_risk`` and the brim decision are read from.  A patch of
    # contact that runs around an outline (a tube's rim, a frame, a cookie
    # cutter's edge) holds the area inside it when that patch can carry the
    # part's height.  For a part on one spot or on a few small feet, and
    # for one whose contact already reads as good, it is
    # ``contact_percentage``.  ``None`` on reports built directly by a
    # client; read it through :func:`graded_contact`.
    graded_contact_percentage: float | None = None
    #: Whether Kiln Pro tuned this check; see :func:`_served`.
    tuned: bool = field(default=False, repr=False, compare=False)

    _WORKED_OUT: ClassVar[tuple[str, ...]] = ("score_deduction",)

    def to_dict(self) -> dict[str, Any]:
        return _served(self)


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

    The score charges for needing supports here, once, by
    ``support_area_percentage``: how much of the underside supports hold
    up.  The volume is reported and not charged, because it clamps at
    100 % under any wide ceiling and so says little about one part against
    another.
    """

    estimated_support_volume_mm3: float
    support_percentage: float  # % of model volume
    support_regions: list[dict[str, float]]
    likely_substituted_by_bridge: bool = False
    score_deduction: int = 0  # what this block took off the score; see _score_terms
    #: Whether Kiln Pro tuned this check; see :func:`_served`.
    tuned: bool = field(default=False, repr=False, compare=False)

    _WORKED_OUT: ClassVar[tuple[str, ...]] = ("score_deduction",)
    #: The part's underside as seen from below: every downward face's area
    #: projected onto the plate, the faces it stands on included.
    underside_area_mm2: float | None = None
    #: The part of that underside supports must hold up: the overhang faces
    #: left once the undersides a slicer bridges or reaches sideways are
    #: set aside, projected the same way.
    support_area_mm2: float | None = None
    #: ``support_area_mm2`` as a % of ``underside_area_mm2``: the figure the
    #: score charges for needing supports.  ``None`` on a block built
    #: without the mesh (a client's own report).
    support_area_percentage: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return _served(self)


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
    sharp_corners_at_base: int  # sharp outside corners of the footprint (_footprint_sharp_corners)
    height_to_base_ratio: float  # bbox height / min(width, depth)
    material_warping_tendency: str  # from materials.json
    recommendations: list[str]
    #: Whether Kiln Pro tuned this check; see :func:`_served`.
    tuned: bool = field(default=False, repr=False, compare=False)

    _WORKED_OUT: ClassVar[tuple[str, ...]] = ("score_deduction",)

    def to_dict(self) -> dict[str, Any]:
        return _served(self)


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
    #: Whether Kiln Pro tuned this check; see :func:`_served`.
    tuned: bool = field(default=False, repr=False, compare=False)

    _WORKED_OUT: ClassVar[tuple[str, ...]] = ("score_deduction", "material_stress_factor")

    def to_dict(self) -> dict[str, Any]:
        return _served(self)


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
    will_detach: bool  # True if the peel or the push test (tipping_ratio) loses
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
    # How well the part's bed contact holds against a sideways push at the
    # top of the part, over what that push asks of it: below 1.0 the part
    # is pushed off.  The verdict is the worse of this and ``force_ratio``.
    tipping_ratio: float | None = None
    #: The plastic Kiln does not call secure on a standard build surface
    #: (:data:`_UNVOUCHED_ON_A_STANDARD_BED`), when that alone kept this
    #: verdict from ``secure``; empty otherwise.
    secure_withheld_for: str = ""
    #: Whether Kiln Pro tuned this check; see :func:`_served`.  A tuned
    #: estimate's reply keeps ``risk_level``, ``will_detach``,
    #: ``model_confidence`` and ``recommendations``.
    tuned: bool = field(default=False, repr=False, compare=False)

    _WORKED_OUT: ClassVar[tuple[str, ...]] = (
        "score_deduction", "adhesion_force_n", "peel_force_n", "force_ratio", "tipping_ratio",
    )

    def to_dict(self) -> dict[str, Any]:
        return _served(self)


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
    #: Where ``weight_grams`` and ``filament_length_meters`` came from, in
    #: the cost estimator's vocabulary: ``"mesh"`` — the mesh volume at
    #: the ``assumptions`` below, not a slice.  A slicer's figure for the
    #: same part (``slice_and_estimate``'s ``estimate.filament_used_grams``)
    #: is the better number whenever both are shown.
    filament_source: str = "mesh"
    assumptions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Stable name a placement fault for geometry below the build plate once
#: carried.  No analysis raises it any more: measured 2026-10-23, PrusaSlicer
#: and OrcaSlicer both set a cube written 12 mm below z = 0 on the plate
#: and print all of it, and Kiln's slice gate moves such a mesh onto the plate
#: before slicing, so a part under the plate is judged resting on it
#: (:attr:`PlacementAnalysis.below_plate_mm`).  Kept so a reader that names it
#: still imports.
PLACEMENT_FAULT_OFF_BED = "off_bed"

#: Stable name for a part larger than the build volume it was checked
#: against.  Unlike ``off_bed`` this verdict is only as good as the bed
#: that was resolved, so a consumer that cannot state WHICH bed should
#: prefer the prose in :attr:`PrintabilityReport.recommendations`.
PLACEMENT_FAULT_EXCEEDS_BED = "exceeds_bed"


@dataclass
class PlacementFault:
    """One placement fault: a stable name beside the text a user reads.

    ``message`` is the exact string that lands in
    :attr:`PrintabilityReport.recommendations` — the two are the same
    fault, so a consumer can show the words or branch on the name
    without parsing English out of a sentence.
    """

    name: str  # PLACEMENT_FAULT_* — stable across releases
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlacementAnalysis:
    """Where the part SITS, as opposed to how it is shaped.

    Structured sibling to the placement lines the safety floor pushes to
    the top of :attr:`PrintabilityReport.recommendations`: same faults,
    same order (worst first), plus a machine-readable name each.
    """

    faults: list[PlacementFault] = field(default_factory=list)

    #: The verdict this part would have received if it were placed
    #: correctly -- score, grade and printable flag as they stood before
    #: the floor clamped them.
    #:
    #: Without these a reader cannot tell a badly SHAPED part from one
    #: that is merely in the wrong PLACE: the floor writes both outcomes
    #: into the same ``score``, ``grade`` and ``printable``.  That
    #: conflation is not academic -- a caller deciding what to say to the
    #: user needs to know whether moving the part onto the plate would
    #: end the problem, and the clamped fields cannot answer it.
    #:
    #: ``None`` on a report built before this block existed.  Equal to
    #: the clamped values when there are no faults.
    score_if_placed: int | None = None
    grade_if_placed: str | None = None
    printable_if_placed: bool | None = None

    #: How far below the plate the file put the part's lowest point, when
    #: it was more than :data:`_PLACEMENT_Z_TOLERANCE_MM` under it.  Not a
    #: fault: every slicer Kiln drives, and Kiln's own slice gate, set the
    #: part on the plate, so it is judged resting there and said once.
    below_plate_mm: float | None = None

    @property
    def fault_names(self) -> list[str]:
        """Fault names, worst first.  Empty when the part sits fine."""
        return [fault.name for fault in self.faults]

    @property
    def off_bed(self) -> bool:
        """True when an off-bed fault was raised, which no analysis does now
        (see :data:`PLACEMENT_FAULT_OFF_BED`)."""
        return PLACEMENT_FAULT_OFF_BED in self.fault_names

    @property
    def exceeds_bed(self) -> bool:
        """True when the part is larger than the bed it was checked against."""
        return PLACEMENT_FAULT_EXCEEDS_BED in self.fault_names

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PiecesAnalysis:
    """How the part's pieces print: as one solid, or as separate parts.

    ``piece_count`` and ``solid_count`` are :mod:`kiln.mesh_bodies`' reading
    (pieces that share a surface or sink into one another print as one
    solid).  Each solid past the first is a separate part, and ``parts_mm``
    gives every part's size (width, depth, height), largest first.  The
    pieces that start above the bed with nothing under them and nothing
    holding them are ``floating_bodies``, and pieces held inside the part
    without touching it are ``captive_bodies``, as :mod:`kiln.fusion_check`
    reads them.  ``floating`` is what is said about the floating pieces (a
    reason the part will not print as it is, :func:`stops`) and ``note``
    what is said about separate parts that do print (:func:`verdict`); each
    is ``""`` when there is nothing to say.
    """

    piece_count: int
    solid_count: int
    parts_mm: list[list[float]] = field(default_factory=list)
    floating_bodies: int = 0
    captive_bodies: int = 0
    floating: str = ""
    note: str = ""

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
    # The overhang angle, from vertical, past which a face was counted as
    # an overhang: what ``overhangs`` and :func:`support_verdict` were
    # read against, and what a slice's support threshold is written from
    # (:mod:`kiln.slicer_supports`).  0.0 for a report built by hand.
    overhang_threshold_deg: float = 0.0
    # Number of separate pieces in the mesh (:mod:`kiln.mesh_bodies`).  A
    # single closed body is 1; a multi-body mesh is N, two bodies laid on
    # each other included.  Exposed for the kiln-pro overlay.
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
    # Placement verdict — where the part sits relative to the plate.
    # ``None`` on reports built directly by a client; an empty
    # ``PlacementAnalysis`` means the part was checked and sits fine.
    # The prose for each fault stays in ``recommendations`` exactly as
    # before; this block only adds the machine-readable name beside it.
    placement: PlacementAnalysis | None = None
    # The one brim / raft decision for this part (see
    # :func:`recommend_adhesion`).  Every brim sentence in the report comes
    # from here; ``None`` only on reports built directly by a client.
    adhesion: AdhesionRecommendation | None = None
    # The nozzle size every check above ran with, and where Kiln got it
    # (:mod:`kiln.assumed_nozzle`).  ``None`` only on reports built directly
    # by a client.
    nozzle: dict[str, Any] | None = None
    # The question the part's size raises about the file's units, in the
    # one judgement every door uses
    # (:func:`kiln.generation.validation.unit_verdict`), when it reads
    # smaller than anything a printer makes; ``""`` otherwise.  A hard
    # stop, and the first: no other size in the report means what it says
    # until it is settled.
    units_question: str = ""
    # What the hard stops took off the score (:func:`_hard_stops`), beside
    # the blocks' own ``score_deduction`` and the placement floor, so the
    # score stays the sum of what the report says it charged.
    stop_deduction: int = 0
    # Whether the part prints as one solid or as separate parts, and which
    # pieces float (:class:`PiecesAnalysis`).  ``None`` when the mesh's
    # pieces could not be read, and on reports built directly by a client.
    pieces: PiecesAnalysis | None = None
    #: Whether Kiln Pro tuned this report (:func:`_mark_tuned`).  A tuned
    #: report's reply keeps the verdict, the score and grade, every check's
    #: risk and reasons, and what to do; see :func:`_served`.
    tuned: bool = field(default=False, repr=False, compare=False)

    _WORKED_OUT: ClassVar[tuple[str, ...]] = ("stop_deduction", "overhang_threshold_deg")

    def to_dict(self) -> dict[str, Any]:
        out = _served(self)
        # Each check is served as itself, never as asdict's copy of it.
        for name, check in _checks(self):
            out[name] = check.to_dict()
        return out


def _checks(report: PrintabilityReport) -> list[tuple[str, Any]]:
    """The checks in *report* that Kiln Pro can tune, by their key: every
    field holding a block that declares what it leaves out of a reply."""
    found = []
    for f in fields(report):
        value = getattr(report, f.name)
        if hasattr(type(value), "_WORKED_OUT"):
            found.append((f.name, value))
    return found


def _mark_tuned(report: PrintabilityReport) -> None:
    """Mark *report* and every check in it as tuned by Kiln Pro, so each is
    served without the numbers the tuning worked it out from."""
    report.tuned = True
    for _name, check in _checks(report):
        check.tuned = True


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
    Pro+ tier: per-material curated values come from the kiln-pro
    overlay.
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
        #   sharp outside corners are where a high-CTE part lifts
        #   first.  A plain block has 4, so past 8 the footprint is
        #   mostly corners (a star, a part on several square feet) and
        #   curling becomes the dominant failure mode.  The count is
        #   read on the footprint's outline (_footprint_sharp_corners);
        #   this rule moved no calibration verdict when it changed.
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
    # Rules describe the risk; the brim itself is decided once, by
    # recommend_adhesion, which reads this block's risk_level.  A rule that
    # named its own brim here sat beside "no brim needed" in one report.
    "recommendation_rules": [
        {"metric": "flat_area_total_mm2",  "operator": ">", "threshold": 2000.0,
         "template": "Large flat surface detected ({flat_area_total_mm2:.0f}mm²): its corners can lift as it cools."},
        {"metric": "height_to_base_ratio", "operator": ">", "threshold": 3.0,
         "template": "Tall/narrow geometry (ratio {height_to_base_ratio:.1f}). Consider splitting into shorter sections or adding a wider base."},
        {"metric": "material_tendency",    "operator": "in", "threshold": ["high", "very_high"],
         "template": "Material ({material}) has {material_tendency} warping tendency. Print in an enclosed chamber and increase bed temperature to reduce thermal gradients."},
        # Speaks only for a part whose warping risk is moderate or above
        # (sharp_corners_at_risk is the count then, 0 otherwise), so four
        # corners on a part that will not warp are not something to act on.
        {"metric": "sharp_corners_at_risk","operator": ">=", "threshold": 1,
         "template": "The footprint has {sharp_corners_phrase}: corners are where a part that warps lifts first, so rounding them in the design helps it stay down."},
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
         "template": "Material ({material}) amplifies thermal stress. Print in an enclosed chamber, increase bed temperature 5-10°C above the default, and avoid sudden layer cooling."},
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
    # while PLA / PETG (a low stress factor) see modest impact.
    #
    # Calibration: thermal_z_scale=100 (mm) chosen against the
    # 31-case calibration matrix — catches all 8 truly-risky cases
    # (100% catch rate) with zero false positives on the 23 safe-
    # print sample.  The PLA candleholder 4x4x200 (aspect 50) lands
    # just above the 3.0 secure threshold — that's the only
    # borderline case in the sample, and it's correctly classified.
    # Set to ``None`` in the overlay to disable.  Lower values (e.g. 50) over-flag
    # warp-prone PLA; higher values (e.g. 200) miss ABS tall thin.
    # Awaiting empirical recalibration from outcome_tracker data.
    "peel_thermal_z_scale": 100.0,
    # The push test: the sideways push (N) the nozzle can give the top of a
    # part as it prints, which the part's bed contact has to hold at the
    # part's full height.  A floor: tuning may raise it, never lower it.
    "nozzle_push_force_n": 0.1,
    # As with warping: the verdict is stated here, the brim decided once
    # in recommend_adhesion from this block's risk_level.
    "recommendation_rules": [
        {"metric": "risk_level", "operator": "==", "threshold": "likely_detach",
         "template": "Part will likely detach during printing: its footprint holds too little for its height."},
        {"metric": "risk_level", "operator": "==", "threshold": "marginal",
         "template": "Adhesion is borderline for this part's height and footprint."},
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


def overhang_threshold_deg(material: str | None = None) -> float:
    """The overhang angle, from vertical, past which a face of *material*
    counts as an overhang here -- the angle :func:`analyze_printability`
    reads its verdict against, and the one a slice's support threshold is
    written from.  Per-material values reach it through the kiln-pro
    overlay; without one, the 45 degree floor."""
    from kiln.design_intelligence import load_pro_overlay_or_empty

    return _resolve_overhang_threshold(None, material, load_pro_overlay_or_empty("printability_judgment"))


def judge_supports(
    file_path: str,
    *,
    nozzle_diameter: float | None = None,
    layer_height: float = 0.2,
    max_overhang_angle: float | None = None,
    material: str = "pla",
    printer_id: str | None = None,
) -> SupportJudgement:
    """The support verdict's inputs for *file_path*, and nothing else.

    The same mesh loading, the same threshold resolution and the same
    judgement as :func:`analyze_printability` -- one engine -- without the
    thin-wall, cavity, warping, thermal and cost analyses a slice never
    reads (on a 200,000-face part those are fifteen seconds).  Raises
    ``ValueError`` as the full analysis does for a file that is not a solid,
    and runs where that analysis runs: through kiln-pro's memory-capped
    runner when there is one (a ``MemoryError`` is the cap), else here.
    """
    kwargs = dict(
        nozzle_diameter=nozzle_diameter, layer_height=layer_height,
        max_overhang_angle=max_overhang_angle, material=material, printer_id=printer_id,
    )
    runner = _analysis_runner()
    if runner is None:
        return _judge_supports_here(file_path, **kwargs)
    return runner(_judge_supports_here, file_path, **kwargs)


def _judge_supports_here(
    file_path: str,
    *,
    nozzle_diameter: float | None = None,
    layer_height: float = 0.2,
    max_overhang_angle: float | None = None,
    material: str = "pla",
    printer_id: str | None = None,
) -> SupportJudgement:
    """:func:`judge_supports`'s work, wherever it is run."""
    from kiln.assumed_nozzle import assumed_nozzle
    from kiln.design_intelligence import load_pro_overlay_or_empty

    nozzle = assumed_nozzle(printer_id, stated=nozzle_diameter)
    mesh_path = _mesh_path_for_analysis(file_path)
    triangles, _vertices, bbox, z_min = _load_mesh_for_analysis(mesh_path)
    overlay = load_pro_overlay_or_empty("printability_judgment")
    threshold = _resolve_overhang_threshold(max_overhang_angle, material, overlay)
    judged = _judge_supports(
        triangles, z_min, bbox,
        resolved_threshold=threshold, layer_height=layer_height,
        material=material, overlay=overlay,
    )
    judged.nozzle = nozzle.to_dict()
    return judged


def support_verdict(report: PrintabilityReport | SupportJudgement) -> str | None:
    """Why *report* says the part needs supports, or ``None`` when it needs none.

    The one answer every door gives -- the printability report, the
    pre-print check, the support assessment, the volume estimate and the
    slice itself (:mod:`kiln.slicer_supports`) -- so a report cannot say
    "needs supports" over a file sliced without them.  It is the report's
    own overhang verdict, which already excludes the undersides a slicer
    bridges or reaches sideways, plus a bridge too long for the material;
    there is no size floor, because a 10 mm cantilever on a large, finely
    meshed part is a real overhang at a tenth of a percent of its faces.
    """
    reasons: list[str] = []
    if report.overhangs.needs_supports:
        pct = report.overhangs.overhang_percentage
        # A share that rounds to nothing is said as a count: four faces on
        # a 200,000-face part are "0.0%" and still an arm in the air.
        reasons.append(
            f"overhangs={pct:.1f}%" if pct >= 0.1
            else f"overhangs={report.overhangs.overhang_triangle_count} face(s)"
        )
    if report.bridging.needs_supports_for_bridges:
        reasons.append(f"bridges={report.bridging.max_bridge_length_mm:.1f}mm")
    return ", ".join(reasons) if reasons else None


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
                dot = sum(a * b for a, b in zip(n_base, n_other, strict=True))
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
    """Parse an STL, OBJ, GLB, or 3MF file, returning (triangles, vertices).

    :raises ValueError: If the file is not a supported format or cannot
        be parsed.
    """
    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"File not found: {file_path}")

    ext = path.suffix.lower()
    errors: list[str] = []

    if ext not in _SUPPORTED_MESH_FORMATS:
        raise ValueError(
            f"Unsupported file type: {ext!r}.  "
            f"Expected one of {', '.join(_SUPPORTED_MESH_FORMATS)}."
        )
    triangles, vertices = _parse_mesh_file(path, errors)

    problem = unprintable_geometry_reason(triangles, vertices, parse_errors=errors)
    if problem:
        raise ValueError(problem)

    # In an order fixed by the geometry, so no reading below depends on the
    # order the file wrote its triangles in (kiln.generation.validation).
    return in_geometric_order(triangles), vertices


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------


#: How near the overhang limit a face may lean and still count as at it.  An
#: STL stores its corners as 32-bit floats, so a slope drawn exactly at the
#: limit reads a few hundred-thousandths of a degree either side of it,
#: differently wherever the part sits (measured 2026-10-23: a 45-degree
#: chamfer read 44.99997 to 45.00001 degrees).  A thousandth of a degree is
#: below anything a printer can tell apart and above that rounding.
_OVERHANG_LIMIT_SLACK_DEG = 1e-3


def _at_overhang_limit(angle_deg: float, limit_deg: float) -> bool:
    """Whether a face leaning *angle_deg* from vertical is at or past the
    overhang limit *limit_deg*: a slope exactly at the limit is an overhang."""
    return angle_deg + _OVERHANG_LIMIT_SLACK_DEG >= limit_deg


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
    picked: list[int] = []  # indices of the overhanging triangles
    angles: list[float] = []

    for index, tri in enumerate(triangles):
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
        if not _at_overhang_limit(overhang_angle, max_overhang_angle):
            continue

        overhang_count += 1
        if overhang_angle > max_angle:
            max_angle = overhang_angle
        picked.append(index)
        angles.append(overhang_angle)

    overhang_pct = (overhang_count / total * 100.0) if total > 0 else 0.0

    return OverhangAnalysis(
        max_overhang_angle=round(max_angle, 1),
        overhang_triangle_count=overhang_count,
        overhang_percentage=round(overhang_pct, 1),
        needs_supports=overhang_count > 0,
        worst_regions=_overhang_regions(triangles, picked, angles),
    )


#: How many overhang regions the report names: enough to ring every
#: underside of an ordinary part, few enough to read as a caption.
_WORST_REGIONS_NAMED = 5


def _overhang_regions(
    triangles: list[tuple[tuple[float, ...], ...]], picked: list[int], angles: list[float],
) -> list[dict[str, float]]:
    """The overhanging AREAS of a part, largest first, at most five.

    Overhanging triangles that share an edge are one region, so a cap's
    whole underside is one entry however finely it is tessellated, and a
    T-shape's two wings are two.  Each region carries its area-weighted
    centroid (where to draw its ring), its steepest angle, and its area in
    mm2, which is what ranks it: a wide underside is the one worth naming.
    Before 2026-10-24 this list held the first ten overhanging triangles in
    file order, so one underside read as several "areas".
    """
    if not picked:
        return []
    corners = np.asarray([triangles[i] for i in picked], dtype=np.float64).reshape(len(picked), 3, 3)
    # Weld corners that are the same point by their 32-bit pattern, the way
    # an STL stores them (adding 0.0 turns -0.0 into 0.0 first).
    bits = (corners.reshape(-1, 3).astype(np.float32) + np.float32(0.0)).view(np.uint32).astype(np.uint64)
    _, point_id = np.unique(
        (bits[:, 0] << np.uint64(32)) | bits[:, 1], return_inverse=True,
    )
    _, point_id = np.unique(point_id.astype(np.uint64) << np.uint64(32) | bits[:, 2], return_inverse=True)
    faces = point_id.reshape(len(picked), 3)
    region = _edge_connected_labels(faces)

    v0, v1, v2 = corners[:, 0], corners[:, 1], corners[:, 2]
    area = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    centroid = corners.mean(axis=1)
    labels, label_of = np.unique(region, return_inverse=True)
    region_area = np.bincount(label_of, weights=area)
    weighted = np.stack([np.bincount(label_of, weights=area * centroid[:, k]) for k in range(3)], axis=1)
    steepest = np.zeros(len(labels))
    np.maximum.at(steepest, label_of, np.asarray(angles, dtype=np.float64))
    # A region of zero-area triangles has nowhere to be drawn and is nothing
    # to support; it is left out rather than placed at the origin.
    drawn = region_area > 0
    order = np.argsort(-region_area[drawn], kind="stable")[:_WORST_REGIONS_NAMED]
    out: list[dict[str, float]] = []
    for k in np.nonzero(drawn)[0][order]:
        x, y, z = weighted[k] / region_area[k]
        out.append({
            "x": round(float(x), 2),
            "y": round(float(y), 2),
            "z": round(float(z), 2),
            "angle": round(float(steepest[k]), 1),
            "area_mm2": round(float(region_area[k]), 2),
        })
    return out


def _edge_connected_labels(faces: np.ndarray) -> np.ndarray:
    """One label per face; faces that share an edge share a label.

    Whole-array work: every pass hangs each root that touches a lower root
    under it and follows parents to the root, so a region of any size
    settles in a few passes and nothing walks the faces one at a time.
    """
    count = len(faces)
    edges = faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    key = np.minimum(edges[:, 0], edges[:, 1]).astype(np.int64) * (int(faces.max()) + 1) + np.maximum(edges[:, 0], edges[:, 1])
    face_of_edge = np.repeat(np.arange(count), 3)
    order = np.argsort(key, kind="stable")
    same = key[order][1:] == key[order][:-1]
    face_a = face_of_edge[order[:-1][same]]
    face_b = face_of_edge[order[1:][same]]
    parent = np.arange(count)
    while True:
        while True:
            grand = parent[parent]
            if np.array_equal(grand, parent):
                break
            parent = grand
        root_a, root_b = parent[face_a], parent[face_b]
        apart = root_a != root_b
        if not apart.any():
            break
        high = np.maximum(root_a[apart], root_b[apart])
        low = np.minimum(root_a[apart], root_b[apart])
        hung, pick = np.unique(high, return_index=True)
        parent[hung] = low[pick]
    return parent


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

# How many chords the support check verifies before it looks for one that
# holds (see ``_supported_chord_mask``).  It sets how far past the first
# holding chord the check reads, not how much memory a cast takes.
_CHORD_VERIFY_BATCH: int = 64

# Working memory one batch of the ray-triangle intersection may hold.  The
# kernel keeps ~200 bytes per (ray, triangle) pair live at its peak -- three
# (rays, triangles, 3) float arrays and the (rays, triangles) ones beside them
# -- so a fixed 64 rays against the 100k-triangle target cap held 1.2 GB
# (measured 2026-10-01), and a whole analysis of a 328k-triangle part reached
# ~2 GB: on a 2 GB hosted server, enough for the kernel to kill it.  Each ray's
# distance comes from its own row alone, so the batch size moves memory and
# nothing else; at this budget a batch is 6 rays at the cap, ~120 MB, measured
# at the same speed as 64.
_RAYCAST_BATCH_BYTES: int = 128 * 1024 * 1024
_RAYCAST_BYTES_PER_PAIR: int = 210

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

# Crack floor for cavity measurements.  A void narrower than this is
# closed by the slicer before any perimeter is planned: PrusaSlicer and
# OrcaSlicer ship ``slice_closing_radius = 0.049`` mm in their bundled
# profiles and fill every crack narrower than twice that radius while
# slicing the mesh.  Such voids come from tessellation, not design — a
# swept feature whose flat chord faces sit a few hundredths off the
# faceted cylinder it was unioned with leaves a 0–0.15 mm sliver of air
# between the two — and no engraved groove or slot is drawn that narrow
# on purpose.
_CAVITY_CRACK_FLOOR_MM: float = 0.1

# A chord measured from one point on a face says nothing about the rest
# of the face.  Before a chord counts, it is re-cast from three more
# points on the same face, this fraction of the way from the centroid
# to each corner, and every one of the four must clear the floor.  A
# crack that tapers to nothing within the face fails near the corner it
# tapers toward; a wall or groove of real width holds everywhere.  Set
# close to the corners so the wide end of a wedge cannot pass on its
# centroid alone, but strictly inside the face so a probe never sits on
# an edge shared with a differently-oriented neighbour.
_CHORD_SUPPORT_FRACTION: float = 0.9

# How far outside a triangle's own edges, as a fraction of the triangle, a
# line may pass and still count as crossing it (``_nearest_crossings``).
_LINE_EDGE_SLACK: float = 1e-7

# A chord is tested as a corner (see ``_corner_chords``) when the face it
# lands on leans toward the face it left by at least this much: the sine of
# one degree.  Faces nearer parallel than that stay apart over any distance
# a part spans.
_CORNER_MIN_LEAN_SIN: float = 0.0175
# Where a corner's run is read, as fractions of the way to the line where
# its two faces would meet; the last is past that line.
_CORNER_STEPS: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0, 1.5)
# Toward that line a corner's run is thinner than the chord measured; a run
# this close to the chord's own length has not thinned, whatever the faces'
# lean says.
_CORNER_THINNING: float = 0.9
# The widest triangles along x (above this share of them) are looked at by
# every corner's walk; the rest are found by where they start.
_CORNER_WIDE_QUANTILE: float = 0.99
# A corner's walk reads crossings this many chords either side of its run
# (and never fewer millimetres than the second): far enough to see the run
# end to end, near enough to keep the triangles it reads few.  A point with
# no crossing that near is read against the whole part.
_CORNER_WINDOW_CHORDS: float = 4.0
_CORNER_WINDOW_MIN_MM: float = 0.5
# Where a corner's run ends is found by halving the step it ended in this
# many times.  Just before that end the run is no wider than this fraction
# of the nozzle (an edge, however finely it is flattened), or than this
# fraction of the chord measured: the two faces met.
_CORNER_EDGE_SPLITS: int = 5
_CORNER_EDGE_NOZZLE_FRACTION: float = 0.25
_CORNER_EDGE_FRACTION: float = 0.25
# What the corner walks of one analysis may read, in triangles looked at:
# each triangle a walk's path is tested against, and each one a point on it
# is read against.  A corner is only read on a component the cast reads
# whole (``_THIN_WALL_INTERSECTION_TRI_CAP`` triangles at most), so no one
# reading is larger.  A walk that does not fit is not taken: its chord stays
# the wall or gap it measured, never cleared.
_CORNER_READ_BUDGET: int = 50_000_000



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


def _component_targets(
    cid: int,
    target_v0: np.ndarray,
    target_e1: np.ndarray,
    target_e2: np.ndarray,
    target_areas: np.ndarray,
    target_comps: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """The triangles of component *cid* a cast is run against, or ``None``
    for a component of fewer than 4 triangles (it cannot enclose space)."""
    target_mask = target_comps == cid
    if target_mask.sum() < 4:
        return None
    # Component-local target subsampling: apply the global cap to each
    # component so a single huge body still benefits from the memory
    # budget, but small components keep all their triangles.  Seeded per
    # call so a support probe casts against exactly the triangles its
    # candidate chord was measured against.
    tv0 = target_v0[target_mask]
    te1 = target_e1[target_mask]
    te2 = target_e2[target_mask]
    tareas = target_areas[target_mask]
    if tv0.shape[0] > _THIN_WALL_INTERSECTION_TRI_CAP:
        rng_target = np.random.default_rng(1)
        t_idx = rng_target.choice(
            tv0.shape[0],
            size=_THIN_WALL_INTERSECTION_TRI_CAP,
            replace=False,
            p=tareas / tareas.sum(),
        )
        tv0 = tv0[t_idx]
        te1 = te1[t_idx]
        te2 = te2[t_idx]
    return tv0, te1, te2


def _cast_within_components(
    origins: np.ndarray,
    directions: np.ndarray,
    origin_comps: np.ndarray,
    target_v0: np.ndarray,
    target_e1: np.ndarray,
    target_e2: np.ndarray,
    target_areas: np.ndarray,
    target_comps: np.ndarray,
    *,
    with_normals: bool = False,
) -> np.ndarray | tuple[np.ndarray, np.ndarray]:
    """Ray-cast each origin against the triangles of its own component.

    Shared by the wall and cavity measurements and by the support probes
    both run on their candidate chords.  Iterates the small set of
    components touched by the rays: a single-body mesh is one iteration
    (a plain global cast); a lattice of N struts casts each strut's rays
    only against that strut's triangles, so a joint overlap with a
    neighbour never reads as a wall or a cavity.

    Returns one distance per ray, ``inf`` for a miss; with
    *with_normals*, also the unit normal (by winding) of the triangle each
    ray hit, zero for a miss.
    """
    dist = np.full(origins.shape[0], np.inf, dtype=np.float64)
    normals = np.zeros((origins.shape[0], 3), dtype=np.float64)
    for cid in np.unique(origin_comps):
        ray_mask = origin_comps == cid
        targets = _component_targets(cid, target_v0, target_e1, target_e2, target_areas, target_comps)
        if targets is None:
            continue
        tv0, te1, te2 = targets
        found, hit = _raycast_min_distances(
            origins[ray_mask],
            directions[ray_mask],
            tv0,
            te1,
            te2,
            min_abs_perpendicular_dot=_THIN_WALL_MIN_PERPENDICULAR_DOT,
            return_hit=True,
        )
        dist[ray_mask] = found
        if with_normals:
            face = np.cross(te1, te2)
            face /= np.maximum(np.linalg.norm(face, axis=1), 1e-300)[:, None]
            normals[ray_mask] = np.where((hit >= 0)[:, None], face[np.maximum(hit, 0)], 0.0)
    if with_normals:
        return dist, normals
    return dist


def _nearest_crossings(
    points: np.ndarray,
    directions: np.ndarray,
    v0: np.ndarray,
    edge1: np.ndarray,
    edge2: np.ndarray,
    *,
    window: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Where each point lies, read along the line through it in its
    direction: the nearest crossing of the triangles ahead of it and behind
    it within *window* (Möller-Trumbore, every angle counted), and whether
    each is a way into the solid or out of it.

    Returns ``(state, ahead, behind)``: ``state`` is ``1`` inside the solid,
    ``-1`` outside it (no crossing within the window counts as outside),
    ``0`` when the crossings either side disagree; ``ahead`` and ``behind``
    are the distances to those crossings (``inf`` for none), so the run of
    solid (or of air) that holds the point is ``ahead + behind`` long.
    """
    n_pts = points.shape[0]
    state = np.zeros(n_pts, dtype=np.int64)
    ahead_d = np.full(n_pts, np.inf, dtype=np.float64)
    behind_d = np.full(n_pts, np.inf, dtype=np.float64)
    if n_pts == 0 or v0.shape[0] == 0:
        state[:] = -1
        return state, ahead_d, behind_d
    face = np.cross(edge1, edge2)
    chunk = max(1, _RAYCAST_BATCH_BYTES // (_RAYCAST_BYTES_PER_PAIR * max(1, v0.shape[0])))
    for start in range(0, n_pts, chunk):
        end = min(start + chunk, n_pts)
        o = points[start:end]
        d = directions[start:end]
        h = np.cross(d[:, None, :], edge2[None, :, :])
        a = np.einsum("tj,ctj->ct", edge1, h)
        f = 1.0 / np.where(np.abs(a) < _THIN_WALL_RAY_EPS_DET, 1.0, a)
        s = o[:, None, :] - v0[None, :, :]
        u = f * np.einsum("ctj,ctj->ct", s, h)
        q = np.cross(s, edge1[None, :, :])
        v = f * np.einsum("cj,ctj->ct", d, q)
        t = f * np.einsum("tj,ctj->ct", edge2, q)
        # A line through an edge two triangles share crosses one of them,
        # whatever the rounding: the bounds are widened by a hair, and a
        # crossing counted twice is still the one nearest.
        edge = _LINE_EDGE_SLACK
        hit = (np.abs(a) >= _THIN_WALL_RAY_EPS_DET) & (u >= -edge) & (v >= -edge) & ((u + v) <= 1.0 + edge)
        # Crossing a face along the line's direction leaves the solid when
        # the face's outward normal points the same way.
        leaving = np.einsum("cj,tj->ct", d, face) > 0.0
        rows = np.arange(end - start)
        ahead_t = np.where(hit & (t > 0.0) & (t <= window), t, np.inf)
        behind_t = np.where(hit & (t < 0.0) & (t >= -window), -t, np.inf)
        ahead = ahead_t.argmin(axis=1)
        behind = behind_t.argmin(axis=1)
        ta, tb = ahead_t[rows, ahead], behind_t[rows, behind]
        has_a, has_b = np.isfinite(ta), np.isfinite(tb)
        # Inside: the next crossing ahead leaves, or the last one behind
        # entered.  Outside: the reverse, or no crossing at all.
        inside_a = has_a & leaving[rows, ahead]
        inside_b = has_b & ~leaving[rows, behind]
        outside_a = has_a & ~leaving[rows, ahead]
        outside_b = has_b & leaving[rows, behind]
        inside = (inside_a | inside_b) & ~(outside_a | outside_b)
        outside = (outside_a | outside_b | ~(has_a | has_b)) & ~(inside_a | inside_b)
        state[start:end] = np.where(inside, 1, np.where(outside, -1, 0))
        ahead_d[start:end] = ta
        behind_d[start:end] = tb
    return state, ahead_d, behind_d


class _CornerBudget:
    """What the corner walks of one analysis may still read
    (``_CORNER_READ_BUDGET``).  Once a reading does not fit, none is taken."""

    __slots__ = ("left",)

    def __init__(self, reads: int | None = None) -> None:
        self.left = _CORNER_READ_BUDGET if reads is None else reads

    def take(self, reads: int) -> bool:
        if reads > self.left:
            self.left = 0
            return False
        self.left -= reads
        return True


def _corner_chords(
    origins: np.ndarray,
    directions: np.ndarray,
    chord: np.ndarray,
    hit_normals: np.ndarray,
    origin_comps: np.ndarray,
    targets: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    *,
    solid: bool,
    edge: float,
    budget: _CornerBudget,
) -> np.ndarray:
    """Mask of chords that measure a corner rather than a wall or a gap.

    A chord from one face to another face that leans toward it measures a
    corner when the two faces close into an edge of the part.  The run (of
    solid for a wall, of air for a gap) is followed from the chord's middle
    in steps toward the line where the faces would meet
    (``_CORNER_STEPS``), kept at the middle of the run at every step: at the
    first step it is still there and thinner than the chord, it never grows
    back to the chord's length, once it ends it stays gone, and it has ended
    by the last step, past that line.  Where it ends is then found to within
    a small part of a step, and just before that it is no wider than *edge*
    (or ``_CORNER_EDGE_FRACTION`` of the chord, if that is wider): the faces
    met in an edge.  A run cut off while still wide (a thin wall's flat top)
    is no corner, and nor is a wall or a gap whose faces stay apart.

    *targets* are the cast's own (``v0, edge1, edge2, area, component`` per
    triangle).  A chord is followed only on a component the cast reads whole
    (``_THIN_WALL_INTERSECTION_TRI_CAP``): against the triangles of that
    component near its path, all of them, and a point no nearby triangle
    tells about is read against the whole component.  Every triangle looked
    at is charged to *budget*.  A chord on a bigger component, or whose walk
    does not fit the budget, is no corner: the wall or gap it measured stands.
    """
    corner = np.zeros(chord.shape[0], dtype=bool)
    along = (directions * hit_normals).sum(axis=1)
    toward = hit_normals * np.sign(along)[:, None]
    cos = np.abs(along)
    sin = np.sqrt(np.clip(1.0 - cos * cos, 0.0, 1.0))
    candidate = np.isfinite(chord) & (cos > 0.0) & (sin >= _CORNER_MIN_LEAN_SIN)
    if not candidate.any():
        return corner
    v0, edge1, edge2, areas, comps = targets
    sizes = np.bincount(comps)
    kind = 1 if solid else -1
    for cid in np.unique(origin_comps[candidate]):
        # Past the cap the cast reads a sample of the component, and a walk
        # read against a sample can find a wall's run ending where it goes on.
        if sizes[cid] > _THIN_WALL_INTERSECTION_TRI_CAP:
            continue
        whole = _component_targets(int(cid), v0, edge1, edge2, areas, comps)
        if whole is None:
            continue
        cv0, ce1, ce2 = whole
        low = np.minimum(cv0, np.minimum(cv0 + ce1, cv0 + ce2))
        high = np.maximum(cv0, np.maximum(cv0 + ce1, cv0 + ce2))
        # The component's triangles by where they start along x, so the ones
        # near a path are a slice; the widest few are looked at every time.
        reach = high[:, 0] - low[:, 0]
        is_wide = reach > np.quantile(reach, _CORNER_WIDE_QUANTILE)
        narrow = np.where(~is_wide)[0]
        narrow = narrow[np.argsort(low[narrow, 0], kind="stable")]
        narrow_x = low[narrow, 0]
        narrow_reach = float(reach[narrow].max()) if narrow.size else 0.0
        wide = np.where(is_wide)[0]
        for i in np.where(candidate & (origin_comps == cid))[0]:
            d = directions[i]
            closing = (toward[i] - cos[i] * d) / sin[i]
            meet = chord[i] * cos[i] / sin[i]
            start = origins[i] + 0.5 * chord[i] * d
            window = max(_CORNER_WINDOW_CHORDS * chord[i], _CORNER_WINDOW_MIN_MM)
            far = start + _CORNER_STEPS[-1] * meet * closing
            box_low = np.minimum(start, far) - window
            box_high = np.maximum(start, far) + window
            first = np.searchsorted(narrow_x, box_low[0] - narrow_reach, side="left")
            last = np.searchsorted(narrow_x, box_high[0], side="right")
            maybe = np.concatenate([narrow[first:last], wide])
            if not budget.take(maybe.size):
                return corner
            near = maybe[np.all(high[maybe] >= box_low, axis=1) & np.all(low[maybe] <= box_high, axis=1)]
            corner[i] = _walks_to_an_edge(
                start, d, closing, meet, chord[i], (cv0[near], ce1[near], ce2[near]), whole,
                kind=kind, edge=max(edge, _CORNER_EDGE_FRACTION * chord[i]), window=window,
                budget=budget,
            )
    return corner


def _walks_to_an_edge(
    start: np.ndarray,
    d: np.ndarray,
    closing: np.ndarray,
    meet: float,
    chord: float,
    near: tuple[np.ndarray, np.ndarray, np.ndarray],
    whole: tuple[np.ndarray, np.ndarray, np.ndarray],
    *,
    kind: int,
    edge: float,
    window: float,
    budget: _CornerBudget,
) -> bool:
    """One chord's walk for :func:`_corner_chords`: *near* are the triangles
    by its path, *whole* every triangle of its component.  A reading that
    does not fit *budget* ends the walk, and the chord is no corner."""
    at, at_step, last_span = start, 0.0, chord

    def read(point: np.ndarray, triangles: tuple[np.ndarray, ...], reach: float):
        if not budget.take(triangles[0].shape[0]):
            return None
        return _nearest_crossings(point[None], d[None], *triangles, window=reach)

    def step_to(fraction: float) -> tuple[bool, bool] | None:
        nonlocal at, at_step, last_span
        point = at + (fraction - at_step) * meet * closing
        found = read(point, near, window)
        if found is not None and not (np.isfinite(found[1][0]) or np.isfinite(found[2][0])):
            # Nothing nearby says which side the point is on: ask the whole part.
            found = read(point, whole, np.inf)
        if found is None:
            return None
        state, ahead, behind = found
        span = float(ahead[0] + behind[0])
        inside = state[0] == kind and np.isfinite(span) and 0.0 < span < _CORNER_THINNING * chord
        if inside:
            at = point + 0.5 * float(ahead[0] - behind[0]) * d
            at_step, last_span = fraction, span
        return inside, state[0] == -kind

    first_out = None
    for step in _CORNER_STEPS:
        took = step_to(step)
        if took is None:
            return False
        inside, gone = took
        if first_out is None:
            if gone:
                first_out = step
            elif not inside:
                return False
        elif not gone:
            return False
    if first_out is None or at_step == 0.0:
        return False
    for _ in range(_CORNER_EDGE_SPLITS):
        middle = 0.5 * (at_step + first_out)
        took = step_to(middle)
        if took is None:
            return False
        if not took[0]:
            first_out = middle
    return last_span <= edge


def _supported_chord_mask(
    chord: np.ndarray,
    faces: np.ndarray,
    directions: np.ndarray,
    origin_comps: np.ndarray,
    cast: Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray],
    *,
    floor: float,
    ceiling: float,
    verify_below: float,
    corners: Callable[[np.ndarray], np.ndarray] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Mask of chords that hold across the face they were measured from,
    and the mask of chords found to measure a corner.

    ``chord`` is one distance per ray (``inf`` for a miss; anything at
    or above ``ceiling`` also counts as a miss), ``faces`` the
    ``(N, 3, 3)`` triangles the rays left from, ``directions`` their
    directions.  ``cast`` re-runs the same component-scoped cast for
    new origins.

    A chord is *supported* when it and three more chords, cast from
    points ``_CHORD_SUPPORT_FRACTION`` of the way from the face's
    centroid to each corner, all land between ``floor`` and
    ``ceiling``.  Sub-tolerance geometry fails this: a sliver of air
    between two surfaces that were meant to coincide, or a sliver of
    solid where a curved surface grazes a plane, tapers to nothing
    within the face, so at least one probe reads below the floor or
    misses.  A wall or groove of real width holds at every probe.

    With ``corners`` (indices in, a mask of corner chords out, see
    :func:`_corner_chords`), a verified chord must also not measure a
    corner, so the smallest surviving chord is a wall (or a gap), never
    the tip of a corner.

    Every chord below ``verify_below`` is verified.  Above it, chords
    are verified in ascending order until one holds, so the smallest
    surviving chord is always a verified one and the larger chords are
    taken at face value.  This keeps the extra casts proportional to
    the number of chords that could be mistaken for a defect rather
    than to the sample count.
    """
    n = chord.shape[0]
    measured = np.isfinite(chord) & (chord < ceiling)
    keep = measured.copy()
    verified = np.zeros(n, dtype=bool)
    corner_mask = np.zeros(n, dtype=bool)

    def verify(idx: np.ndarray) -> None:
        ok = chord[idx] >= floor
        if corners is not None:
            at_corner = corners(idx)
            corner_mask[idx] = at_corner
            ok &= ~at_corner
        # The face probes, for the chords still in question.
        probed = idx[ok]
        held = np.ones(probed.size, dtype=bool)
        centroids = faces[probed].mean(axis=1)
        for corner in range(3):
            probes = (
                centroids
                + _CHORD_SUPPORT_FRACTION * (faces[probed, corner, :] - centroids)
                + _THIN_WALL_RAY_ORIGIN_OFFSET * directions[probed]
            )
            d = cast(probes, directions[probed], origin_comps[probed])
            held &= np.isfinite(d) & (d < ceiling) & (d >= floor)
        ok[ok] = held
        keep[idx] = ok
        verified[idx] = True

    thin = np.where(measured & (chord < verify_below))[0]
    if thin.size:
        verify(thin)
    if not keep[verified].any():
        rest = np.where(measured & ~verified)[0]
        rest = rest[np.argsort(chord[rest])]
        for start in range(0, rest.size, _CHORD_VERIFY_BATCH):
            batch = rest[start:start + _CHORD_VERIFY_BATCH]
            verify(batch)
            if keep[batch].any():
                break
    return keep, corner_mask


# Thin samples within this many millimetres of a band's thinnest sample
# are the same wall: CAD walls come in discrete thicknesses, and a band
# kept this narrow still separates a 1.4 mm standoff from a 1.5 mm boss.
_THIN_FEATURE_BAND_MM: float = 0.05

# Samples of one band closer than this share a place.  A fifth of the
# part's diagonal, clamped: at least a nozzle-scale 5 mm, and never so
# far that two separate features on a large part merge into one.
_THIN_FEATURE_LINK_FRACTION: float = 0.2
_THIN_FEATURE_LINK_MIN_MM: float = 5.0
_THIN_FEATURE_LINK_MAX_MM: float = 25.0

# Bound on the regions one block reports.  Far above any part a person
# fixes by hand; its job is only to keep a pathological mesh's payload
# finite.
_THIN_FEATURE_CAP: int = 50


def _union_root(parent: np.ndarray, k: int) -> int:
    """The root of *k* in a union-find forest, halving the path as it goes."""
    while parent[k] != k:
        parent[k] = parent[parent[k]]
        k = parent[k]
    return k


def _thin_wall_features(
    points: np.ndarray,
    thickness: np.ndarray,
    *,
    link_mm: float,
) -> list[dict[str, float]]:
    """Group thin measurement points into distinct features.

    A feature is one wall thickness at one place.  Points are banded by
    thickness first (a band spans :data:`_THIN_FEATURE_BAND_MM` from its
    thinnest point), then joined within a band when closer than
    ``link_mm``; each group is reported once, at its thinnest point.
    Banding first is what keeps a slightly thicker feature from hiding
    behind a thinner one nearby — the report used to be the five
    thinnest POINTS, which on one enclosure all sat on one lid ledge and
    left its standoffs and bosses for later rounds to find.
    """
    features: list[dict[str, float]] = []
    order = np.argsort(thickness, kind="stable")
    i = 0
    while i < order.size:
        start = thickness[order[i]]
        j = i
        while j < order.size and thickness[order[j]] <= start + _THIN_FEATURE_BAND_MM:
            j += 1
        band = order[i:j]
        # Single-linkage clustering within the band (union-find over the
        # pairs closer than ``link_mm``).
        parent = np.arange(band.size)
        pts = points[band]
        near = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=2) < link_mm
        for a, b in zip(*np.nonzero(np.triu(near, k=1)), strict=True):
            ra, rb = _union_root(parent, int(a)), _union_root(parent, int(b))
            if ra != rb:
                parent[rb] = ra
        groups: dict[int, list[int]] = {}
        for k in range(band.size):
            groups.setdefault(_union_root(parent, k), []).append(int(band[k]))
        for members in groups.values():
            idx = np.asarray(members)
            best = idx[np.argmin(thickness[idx])]
            features.append({
                "x": round(float(points[best, 0]), 2),
                "y": round(float(points[best, 1]), 2),
                "z": round(float(points[best, 2]), 2),
                "thickness_mm": round(float(thickness[best]), 3),
                "samples": len(members),
            })
        i = j
    features.sort(key=lambda f: (f["thickness_mm"], f["x"], f["y"], f["z"]))
    return features[:_THIN_FEATURE_CAP]


def _analyze_thin_walls(
    triangles: list[tuple[tuple[float, ...], ...]],
    vertices: list[tuple[float, ...]],
    *,
    nozzle_diameter: float = 0.4,
    thin_below_mm: float | None = None,
) -> ThinWallAnalysis:
    """Measure thin walls; see :func:`_measure_thin_walls`."""
    return _measure_thin_walls(
        triangles, vertices,
        nozzle_diameter=nozzle_diameter, thin_below_mm=thin_below_mm,
    )[0]


def _measure_thin_walls(
    triangles: list[tuple[tuple[float, ...], ...]],
    vertices: list[tuple[float, ...]],
    *,
    nozzle_diameter: float = 0.4,
    thin_below_mm: float | None = None,
    corner_budget: _CornerBudget | None = None,
    tris: np.ndarray | None = None,
    comp_ids: np.ndarray | None = None,
) -> tuple[ThinWallAnalysis, float]:
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

    Walls with measured thickness below the threshold are flagged as
    thin: ``nozzle_diameter``, or ``thin_below_mm`` when that is larger
    (the material's minimum wall, when one is known).  Every point
    below the threshold is verified the same way, and every distinct thin
    feature is reported with a position (:func:`_thin_wall_features`).
    ``min_wall_thickness_mm`` is the absolute smallest measurement on the
    mesh; the 0.0 sentinel is reserved for measurement failure on
    degenerate meshes.

    Returns the block and, separately, the percentage of points thinner
    than the nozzle itself — what the score deducts for on every tier,
    whatever threshold the block reports against.

    Known limitation: helical features (threaded rods, springs) form a
    single component, so per-component scoping does not help — rays
    cast from helical faces near the rod's end caps can hit the cap
    from very close, returning sub-millimetre tessellation artifacts
    on a structurally-thick part.  Cylindrical hole-bore tessellation
    artifacts on round holes are similarly out-of-scope.  Engraved-
    groove widths are measured separately by
    :func:`_analyze_cavity_widths`.  Every chord that counts has been
    verified to hold across the face it was measured from — see
    :func:`_supported_chord_mask` — so a sliver that tapers to nothing
    within one face is dropped rather than reported at its centroid, and a
    chord that measures a corner (:func:`_corner_chords`) is no wall.  The
    corners are read within *corner_budget*, which one analysis shares
    between its walls and its gaps (a fresh :class:`_CornerBudget` when none
    is given).
    *tris* is the same triangles as a ``(T, 3, 3)`` float array and *comp_ids*
    their component labels, when the caller already holds them; each is
    built here otherwise.  Either way the measurement reads the same numbers.
    """
    threshold = max(nozzle_diameter, thin_below_mm or 0.0)
    basis = "material" if threshold > nozzle_diameter else "nozzle"

    def unmeasured() -> tuple[ThinWallAnalysis, float]:
        return ThinWallAnalysis(0.0, 0, 0.0, [], threshold, basis), 0.0

    total = len(triangles)
    if total < 4:
        # Degenerate input — no closed surface to measure walls on.
        return unmeasured()

    if tris is None:
        tris = np.asarray(triangles, dtype=np.float64)  # (T, 3, 3)
    if tris.ndim != 3 or tris.shape[1] != 3 or tris.shape[2] != 3:
        return unmeasured()

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
        return unmeasured()

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
    if comp_ids is None:
        comp_ids = _label_mesh_components(tris)  # (T,)
    origin_comps = comp_ids[valid_indices[sample_idx]]  # one per ray
    valid_face_comps = comp_ids[valid_indices]          # one per valid target

    target_v0 = v0[valid_face]
    target_e1 = edge1[valid_face]
    target_e2 = edge2[valid_face]

    targets = (target_v0, target_e1, target_e2, valid_areas, valid_face_comps)

    def cast(o: np.ndarray, d: np.ndarray, c: np.ndarray) -> np.ndarray:
        return _cast_within_components(o, d, c, *targets)

    exit_dist, hit_normals = _cast_within_components(
        origins, directions, origin_comps, *targets, with_normals=True,
    )

    finite_mask = np.isfinite(exit_dist)
    if not finite_mask.any():
        return unmeasured()

    budget = corner_budget if corner_budget is not None else _CornerBudget()

    def corners(idx: np.ndarray) -> np.ndarray:
        return _corner_chords(
            origins[idx], directions[idx], exit_dist[idx], hit_normals[idx], origin_comps[idx],
            targets, solid=True,
            edge=max(_SLIVER_CHORD_FLOOR_MM, _CORNER_EDGE_NOZZLE_FRACTION * nozzle_diameter),
            budget=budget,
        )

    # A chord counts only when it holds across the face it was measured
    # from and clears the sliver floor (50 µm; well under any nozzle).
    # Sub-floor chords come from boundary slivers where curved surfaces
    # graze the bounding box at a thin angle (round-4 topology audit,
    # gyroid clipping); the support probes drop the wider end of the
    # same slivers.  A chord that measures a corner is not a wall
    # (``_corner_chords``).  Fall through to every finite chord that is
    # not a corner when nothing holds (degenerate mesh) so the consumer
    # sees the anomaly rather than a silenced no-signal.
    supported, at_corner = _supported_chord_mask(
        exit_dist,
        tris[valid_indices[sample_idx]],
        directions,
        origin_comps,
        cast,
        floor=_SLIVER_CHORD_FLOOR_MM,
        ceiling=np.inf,
        verify_below=threshold,
        corners=corners,
    )
    counted = supported if supported.any() else finite_mask & ~at_corner
    if not counted.any():
        return unmeasured()
    measured_min = float(exit_dist[counted].min())

    # Judged at the precision every thickness is reported at (µm), so a
    # wall reported as "1.2 mm" is never thin against a 1.2 mm floor.  The
    # raw chords carry float32 STL noise: a CAD 1.2 mm ledge measures
    # 1.1999 mm, and comparing raw counted it thin while the material
    # rule, reading the reported 1.2, did not.
    reported = np.round(exit_dist, 3)
    thin_mask = counted & (reported < threshold)
    thin_count = int(thin_mask.sum())
    thin_pct = thin_count / n_sample * 100.0
    sub_nozzle_pct = int((counted & (reported < nozzle_diameter)).sum()) / n_sample * 100.0

    # ``min_wall_thickness_mm`` carries the absolute smallest measured
    # wall thickness on the mesh, regardless of the nozzle threshold —
    # the kiln-pro per-material overlay needs this number to compare
    # against material-specific structural floors (e.g. flag a 1.0 mm
    # PLA wall against the 1.2 mm PLA structural floor even when it's
    # above the 0.4 mm nozzle).  The 0.0 sentinel is reserved for
    # measurement failure on degenerate meshes.
    problematic: list[dict[str, float]] = []
    if thin_count > 0:
        diagonal = float(np.linalg.norm(
            tris.reshape(-1, 3).max(axis=0) - tris.reshape(-1, 3).min(axis=0)
        ))
        link_mm = min(
            _THIN_FEATURE_LINK_MAX_MM,
            max(_THIN_FEATURE_LINK_MIN_MM, _THIN_FEATURE_LINK_FRACTION * diagonal),
        )
        problematic = _thin_wall_features(
            origins[thin_mask], exit_dist[thin_mask], link_mm=link_mm,
        )

    return ThinWallAnalysis(
        min_wall_thickness_mm=round(measured_min, 3),
        thin_wall_count=thin_count,
        thin_wall_percentage=round(thin_pct, 1),
        problematic_regions=problematic,
        threshold_mm=round(threshold, 3),
        threshold_basis=basis,
    ), sub_nozzle_pct


def _analyze_cavity_widths(
    triangles: list[tuple[tuple[float, ...], ...]],
    vertices: list[tuple[float, ...]],
    *,
    nozzle_diameter: float = 0.4,
    corner_budget: _CornerBudget | None = None,
    tris: np.ndarray | None = None,
    comp_ids: np.ndarray | None = None,
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
    accordingly.  The air at a corner of a recess is no gap
    (:func:`_corner_chords`, read within *corner_budget* as the walls are).
    """
    total = len(triangles)
    if total < 4:
        return CavityAnalysis(
            min_cavity_width_mm=0.0,
            cavity_sample_count=0,
            problematic_regions=[],
        )

    if tris is None:
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
    if comp_ids is None:
        comp_ids = _label_mesh_components(tris)
    origin_comps = comp_ids[valid_indices[sample_idx]]
    valid_face_comps = comp_ids[valid_indices]

    target_v0 = v0[valid_face]
    target_e1 = edge1[valid_face]
    target_e2 = edge2[valid_face]

    targets = (target_v0, target_e1, target_e2, valid_areas, valid_face_comps)

    def cast(o: np.ndarray, d: np.ndarray, c: np.ndarray) -> np.ndarray:
        return _cast_within_components(o, d, c, *targets)

    hit_dist, hit_normals = _cast_within_components(
        origins, directions, origin_comps, *targets, with_normals=True,
    )

    budget = corner_budget if corner_budget is not None else _CornerBudget()

    def corners(idx: np.ndarray) -> np.ndarray:
        return _corner_chords(
            origins[idx], directions[idx], hit_dist[idx], hit_normals[idx], origin_comps[idx],
            targets, solid=False,
            edge=max(_CAVITY_CRACK_FLOOR_MM, _CORNER_EDGE_NOZZLE_FRACTION * nozzle_diameter),
            budget=budget,
        )

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

    # A void counts as a cavity only when it holds across the face it
    # was measured from and is at least the slicer's crack-closing
    # width.  A sliver of air between two surfaces that were meant to
    # coincide — a swept thread's flat root chords against the faceted
    # cylinder they were unioned with — tapers to nothing within the
    # face and is closed by the slicer before it plans a perimeter; it
    # is tessellation, not an engraved feature.  The air at a corner of a
    # recess is not a gap either (``_corner_chords``).  No fall-through
    # here: a mesh with nothing but cracks has no cavities.
    cavity_mask, _at_corner = _supported_chord_mask(
        hit_dist,
        tris[valid_indices[sample_idx]],
        directions,
        origin_comps,
        cast,
        floor=_CAVITY_CRACK_FLOOR_MM,
        ceiling=cavity_max_dist,
        verify_below=nozzle_diameter,
        corners=corners,
    )
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
    return_hit: bool = False,
) -> np.ndarray | tuple[np.ndarray, np.ndarray]:
    """Vectorized Möller-Trumbore ray-triangle intersection.

    For each ray, return the minimum positive hit distance against the
    triangle set (``inf`` for rays that miss every triangle); with
    *return_hit*, also the index of the triangle hit (``-1`` for a miss).

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
    min_hit = np.full(n_rays, -1, dtype=np.int64)
    chunk = max(1, _RAYCAST_BATCH_BYTES // (_RAYCAST_BYTES_PER_PAIR * max(1, v0.shape[0])))

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
        nearest = t_masked.argmin(axis=1)
        found = t_masked[np.arange(end - start), nearest]
        min_dist[start:end] = found
        min_hit[start:end] = np.where(np.isfinite(found), nearest, -1)

    if return_hit:
        return min_dist, min_hit
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
    tris: np.ndarray | None = None,
) -> list[_DownwardRegion]:
    """Aggregate support-needing downward faces into connected regions
    and measure each region's worst bridgeable span (see
    :class:`_DownwardRegion`).  *tris* is the same triangles as a
    ``(T, 3, 3)`` array when the caller holds one; built here otherwise."""

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
    _tris_arr: np.ndarray | None = tris

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
    (normal z ≤ -0.9) ceiling faces.  The score reads the span, never
    this count (see :func:`_score_terms`).
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


def _adhesion_risk_for_contact(contact_pct: float) -> str:
    """The adhesion band for a contact percentage: ``low`` / ``medium`` / ``high``.

    The one place the bands are drawn.  :func:`_analyze_bed_adhesion`
    labels a part with it and :func:`recommend_adhesion` decides the brim
    from that label, so the label and the advice cannot describe two
    different parts.
    """
    if contact_pct > 30.0:
        return "low"
    if contact_pct > 10.0:
        return "medium"
    return "high"


def graded_contact(bed: BedAdhesionAnalysis) -> float:
    """The share of the footprint *bed*'s contact holds down, in percent: the
    figure its ``adhesion_risk`` and the brim decision are read from.

    The one reader of it, so a door judging bed contact by a number of its
    own reads the same number the grade did.  A report built directly by a
    client has only ``contact_percentage``, and that is the answer.
    """
    graded = getattr(bed, "graded_contact_percentage", None)
    return bed.contact_percentage if graded is None else graded


#: How much a patch of bed contact must hold per millimetre of the part's
#: height before the area its outline runs around counts as held down: the
#: patch's weakest section modulus (mm^3) over the part's height (mm).
_OUTLINE_HOLD_MM2 = 6.0


def _contact_patches(tris: np.ndarray) -> list[np.ndarray]:
    """*tris*, the bed contact as ``(n, 3, 2)`` XY triangles, split into the
    patches whose triangles share a corner."""
    labels = _contact_patch_labels(tris)
    order = np.argsort(labels, kind="stable")
    cuts = np.flatnonzero(np.diff(labels[order])) + 1
    return [tris[group] for group in np.split(order, cuts)]


def _corner_ids(points: np.ndarray) -> np.ndarray:
    """One id per point of *points* ``(n, 2)``, shared by points that round
    to the same 0.1 µm of the bed: how the contact's triangles are matched
    corner to corner."""
    grid = np.round(points * 1e4).astype(np.int64)
    grid -= grid.min(axis=0)
    keys = grid[:, 0] * (int(grid[:, 1].max()) + 1) + grid[:, 1]
    return np.unique(keys, return_inverse=True)[1].reshape(-1)


def _linked_labels(count: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """One label per item of *count* items, the same label for any two that
    a row of *a*, *b* links, directly or through others; numbered from 0 in
    the order of each group's lowest item."""
    # Hook each root onto the lowest one it meets, then point every item
    # straight at its root.
    label = np.arange(count)
    while True:
        la, lb = label[a], label[b]
        if np.array_equal(la, lb):
            break
        low = np.minimum(la, lb)
        np.minimum.at(label, la, low)
        np.minimum.at(label, lb, low)
        while True:
            jumped = label[label]
            if np.array_equal(jumped, label):
                break
            label = jumped
    return np.unique(label, return_inverse=True)[1].reshape(-1)


def _contact_patch_labels(tris: np.ndarray, ids: np.ndarray | None = None) -> np.ndarray:
    """Which patch (:func:`_contact_patches`) each row of *tris* belongs to,
    numbered from 0 in the order the patches are listed; *ids* are its
    corners' :func:`_corner_ids`, when they are already known."""
    ids = (_corner_ids(tris.reshape(-1, 2)) if ids is None else ids).reshape(-1, 3)
    corners = _linked_labels(
        int(ids.max()) + 1,
        np.concatenate([ids[:, 0], ids[:, 1]]),
        np.concatenate([ids[:, 1], ids[:, 2]]),
    )
    return np.unique(corners[ids[:, 0]], return_inverse=True)[1].reshape(-1)


def _outline_corners(points: np.ndarray) -> np.ndarray:
    """The points of *points* ``(n, 2)`` that can be corners of their convex
    hull, sorted by x then y.  Every other point lies inside the polygon
    through the extreme points in 64 directions, so it cannot be one."""
    pts = np.round(points, 6)
    if len(pts) <= 64:
        return np.unique(pts, axis=0)
    angles = np.linspace(0.0, 2.0 * np.pi, 64, endpoint=False)
    xs, ys = pts[:, 0], pts[:, 1]
    extreme = [int(np.argmax(xs * math.cos(a) + ys * math.sin(a))) for a in angles]
    ring = [int(i) for k, i in enumerate(extreme) if k == 0 or i != extreme[k - 1]]
    if len(ring) > 1 and ring[-1] == ring[0]:
        ring.pop()
    if len(ring) < 3:
        return np.unique(pts, axis=0)
    poly = pts[ring]
    inside = np.ones(len(pts), dtype=bool)
    for corner, edge in zip(poly, np.roll(poly, -1, axis=0) - poly, strict=True):
        inside &= edge[0] * (pts[:, 1] - corner[1]) - edge[1] * (pts[:, 0] - corner[0]) > 1e-9
    return np.unique(pts[~inside], axis=0)


def _hull_area(corners: np.ndarray) -> float:
    """Area of the convex hull of *corners*, sorted by x then y."""
    pts = [tuple(p) for p in corners.tolist()]
    if len(pts) < 3:
        return 0.0

    def half(seq: list[tuple[float, float]]) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for p in seq:
            while len(out) >= 2 and (
                (out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])
            ) <= 0:
                out.pop()
            out.append(p)
        return out

    hull = np.array(half(pts)[:-1] + half(pts[::-1])[:-1])
    if len(hull) < 3:
        return 0.0
    xs, ys = hull[:, 0], hull[:, 1]
    return float(abs(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1))) / 2.0)


def _patch_hold(patch: np.ndarray, corners: np.ndarray) -> tuple[float, float]:
    """``(area, weakest section modulus)`` of one patch of contact, in mm^2
    and mm^3: how much it resists the part being tipped off it, in the
    direction it resists least.  *corners* holds its outline's corners
    (:func:`_outline_corners`), the farthest any of it reaches."""
    p0, p1, p2 = patch[:, 0], patch[:, 1], patch[:, 2]
    e1, e2 = p1 - p0, p2 - p0
    areas = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) / 2.0
    area = float(areas.sum())
    if area <= 0.0:
        return 0.0, 0.0
    centre = ((p0 + p1 + p2) / 3.0 * areas[:, None]).sum(axis=0) / area
    q0, q1, q2 = p0 - centre, p1 - centre, p2 - centre
    x0, x1, x2, y0, y1, y2 = q0[:, 0], q1[:, 0], q2[:, 0], q0[:, 1], q1[:, 1], q2[:, 1]
    ixx = float((areas / 6.0 * (x0 * x0 + x1 * x1 + x2 * x2 + x0 * x1 + x1 * x2 + x2 * x0)).sum())
    iyy = float((areas / 6.0 * (y0 * y0 + y1 * y1 + y2 * y2 + y0 * y1 + y1 * y2 + y2 * y0)).sum())
    ixy = float((areas / 12.0 * (
        2 * (x0 * y0 + x1 * y1 + x2 * y2) + x0 * y1 + x1 * y0 + x0 * y2 + x2 * y0 + x1 * y2 + x2 * y1
    )).sum())
    angles = np.radians(np.arange(0.0, 180.0, 5.0))
    c, s = np.cos(angles), np.sin(angles)
    reach = np.abs((corners - centre) @ np.vstack([c, s])).max(axis=0)
    second = c * c * ixx + 2 * c * s * ixy + s * s * iyy
    held = reach > 0.0
    return area, float((second[held] / reach[held]).min()) if held.any() else 0.0


def _held_area(tris: np.ndarray, height: float) -> float:
    """The bed area *tris* holds down: their own area, plus the area inside
    the outline of the one patch that runs around the most of it, when that
    patch holds enough for *height*.  A part on a few small feet stands on
    the feet, wherever they are, so only one patch's outline counts and
    only a patch strong enough for the part's height."""
    total = 0.0
    inside = 0.0
    for patch in _contact_patches(tris):
        corners = _outline_corners(patch.reshape(-1, 2))
        area, modulus = _patch_hold(patch, corners)
        total += area
        if height > 0.0 and modulus / height >= _OUTLINE_HOLD_MM2:
            inside = max(inside, _hull_area(corners) - area)
    return total + inside


def _analyze_bed_adhesion(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    bbox: dict[str, float],
    *,
    layer_height: float = 0.2,
) -> BedAdhesionAnalysis:
    """Estimate bed contact area.

    Sums the area of triangles whose vertices are all within one layer
    height of the bottom of the mesh, measures how much of the footprint
    that contact spans (its own bounding rectangle), and how much of the
    footprint it holds down (:func:`graded_contact`), which is what the
    adhesion band is read from.
    """
    contact_threshold = z_min + layer_height
    contact_area = 0.0
    x_lo = y_lo = math.inf
    x_hi = y_hi = -math.inf
    contact_xy: list[tuple[tuple[float, float], ...]] = []

    for tri in triangles:
        # All three vertices must be near Z_min.
        if tri[0][2] <= contact_threshold and tri[1][2] <= contact_threshold and tri[2][2] <= contact_threshold:
            contact_area += _triangle_area(tri[0], tri[1], tri[2])
            contact_xy.append(tuple((v[0], v[1]) for v in tri))
            for vx, vy, *_ in tri:
                x_lo, x_hi = min(x_lo, vx), max(x_hi, vx)
                y_lo, y_hi = min(y_lo, vy), max(y_hi, vy)

    # Bounding box footprint (XY projection).
    footprint = (bbox["x_max"] - bbox["x_min"]) * (bbox["y_max"] - bbox["y_min"])
    contact_pct = (contact_area / footprint * 100.0) if footprint > 0 else 0.0
    span = (x_hi - x_lo) * (y_hi - y_lo) if x_hi >= x_lo else 0.0
    span_pct = min(100.0, span / footprint * 100.0) if footprint > 0 else 0.0
    graded_pct = contact_pct
    # A contact that already reads as good needs no outline to say so.
    if contact_xy and footprint > 0 and _adhesion_risk_for_contact(contact_pct) != "low":
        held = _held_area(np.asarray(contact_xy, dtype=float), bbox["z_max"] - bbox["z_min"])
        graded_pct = min(100.0, max(contact_pct, held / footprint * 100.0))

    return BedAdhesionAnalysis(
        contact_area_mm2=round(contact_area, 2),
        contact_percentage=round(contact_pct, 1),
        adhesion_risk=_adhesion_risk_for_contact(graded_pct),
        contact_span_percentage=round(span_pct, 1),
        graded_contact_percentage=round(graded_pct, 1),
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
        if not _at_overhang_limit(overhang_angle, max_overhang_angle):
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
        with open(path, encoding="utf-8") as f:
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


#: The stretch of footprint outline a corner is read over, in mm.  Where the
#: outline follows a circle of radius R it turns by 1/R radians in each mm of
#: it, however many facets the circle was cut into, so a circle or a rounded
#: corner reads as a corner only when R is under 1 mm / 1.536 = 0.65 mm (the
#: turn below): a round part has no corners at any resolution, and a corner
#: rounded off by less than that still prints as one.  It is also the length
#: a slicer simplifies a first-layer outline to before it looks for corners to
#: put brim ears on.  Fixed geometry, not tied to the nozzle.
_CORNER_ARC_MM = 1.0

#: How far the outline must turn toward the material within that stretch to
#: be a sharp corner: 88 degrees, an inside angle of 92 or less.  A square's
#: corners turn exactly 90, so they count with room left for a file's
#: rounding; a pentagon's turn 72 and a chamfer's 45, so they do not.
_SHARP_CORNER_TURN_DEG = 88.0

#: The warping verdicts the corner advice speaks for.
_CORNERS_WORTH_SAYING = ("moderate", "high", "critical")


def _outline_rings(edges: np.ndarray, ids: np.ndarray) -> list[np.ndarray]:
    """The outline *edges* of one patch of contact, with their ends' *ids*
    (:func:`_patch_outline`, the patch on each edge's left), joined end to end
    into closed rings of points: outer rings run counter-clockwise, holes
    clockwise.  Where two parts of the patch touch at one point the ring takes
    the sharpest left turn there, which keeps it on the part it came round, so
    each part is a ring of its own and no ring crosses itself.

    Linear in the edges but for one sort.  The edges leaving each point are
    put in heading order once, and the sharpest left turn from an edge
    arriving at a point is the edge leaving it whose heading comes next below
    the way straight back: one binary search, for every edge at once.  A
    point the whole outline runs through (thousands of thin triangles meeting
    at one corner) costs its share of the sort, not a scan on every visit.
    """
    run = edges[:, 1] - edges[:, 0]
    heading = np.arctan2(run[:, 1], run[:, 0])  # in (-pi, pi]
    back = np.where(heading > 0.0, heading - np.pi, heading + np.pi)  # straight back, in (-pi, pi]
    # Every leaving edge under one sort key: the point it leaves times 8, plus
    # its heading shifted into (0, 2 pi], so each point's edges sit together
    # and in heading order.
    key = ids[:, 0] * 8.0 + (heading + np.pi)
    order = np.argsort(key, kind="stable")
    key = key[order]
    at = ids[:, 1] * 8.0
    lo = np.searchsorted(key, at)
    hi = np.searchsorted(key, at + 8.0)
    pick = np.searchsorted(key, at + (back + np.pi), side="right") - 1
    pick = np.where(pick >= lo, pick, hi - 1)  # nothing below the way back: round to the top
    after = np.where(lo < hi, order[np.clip(pick, 0, len(order) - 1)], -1).tolist()
    used = [False] * len(edges)
    rings: list[np.ndarray] = []
    for start in range(len(edges)):
        if used[start]:
            continue
        walk, k = [], start
        while k >= 0 and not used[k]:
            used[k] = True
            walk.append(k)
            k = after[k]
            if k >= 0 and used[k] and k != start:
                # Two edges arriving at one point can be handed the same edge
                # leaving it only where the outline runs over itself (two faces
                # lying on the bed one over the other, a part thinner than a
                # layer).  The second takes the sharpest left turn still free
                # there, the next one down that point's list: a scan of the
                # edges leaving that one point, and only for a repeated edge.
                lo_k, hi_k, j = int(lo[walk[-1]]), int(hi[walk[-1]]), int(pick[walk[-1]])
                free = (int(order[lo_k + (j - lo_k - q) % (hi_k - lo_k)]) for q in range(hi_k - lo_k))
                k = next((e for e in free if not used[e]), -1)
        if len(walk) >= 3:
            rings.append(edges[walk, 0])
    return rings


def _ring_layout(sizes: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For rings of *sizes* points laid end to end: the ring each point is on,
    where each ring starts, and each point's place on its own ring."""
    ring_of = np.repeat(np.arange(len(sizes)), sizes)
    first = np.r_[0, np.cumsum(sizes)[:-1]]
    return ring_of, first, np.arange(int(sizes.sum())) - first[ring_of]


def _first_largest(values: np.ndarray, start: np.ndarray, stop: np.ndarray) -> np.ndarray:
    """The index of the largest of *values* in each window ``[start, stop)``,
    the first on a tie.  A table of the best index over every run of 1, 2, 4,
    ... values is built a doubling at a time, and each window is read as the
    better of the two runs of its own size that cover it: about log2 of the
    longest window passes over *values*, whatever the number of windows."""
    level = np.frexp((stop - start).astype(np.float64))[1] - 1  # the largest run that fits each window
    best = np.arange(len(values))
    out = np.empty(len(start), dtype=np.int64)
    width = 1
    for k in range(int(level.max()) + 1):
        here = level == k
        left, right = best[start[here]], best[stop[here] - width]
        out[here] = np.where(values[right] > values[left], right, left)
        later = np.r_[best[width:], best[len(best) - width:]]  # past the end: never read
        best = np.where(values[later] > values[best], later, best)
        width *= 2
    return out


def _sharp_corners_of_rings(rings: list[np.ndarray]) -> int:
    """How many sharp corners the closed *rings*, material on their left, turn
    round: places where a ring turns toward the material by
    :data:`_SHARP_CORNER_TURN_DEG` or more within :data:`_CORNER_ARC_MM` of
    outline.  A right turn takes away from a left one, so an inside corner
    never counts, nor does any corner of a hole, whose ring runs the other
    way.  Turns closer together than that stretch are one corner, and a ring
    no longer than two stretches is a dot with none.

    Every ring is read in one pass, so a contact of many small islands costs
    what one ring of as many points does."""
    if not rings:
        return 0
    pts = np.concatenate(rings)
    sizes = np.array([len(r) for r in rings])
    ring_of, first, local = _ring_layout(sizes)
    ahead = first[ring_of] + (local + 1) % sizes[ring_of]
    keep = np.hypot(*(pts[ahead] - pts).T) > 1e-6  # a point repeating the next has no direction
    sizes = np.bincount(ring_of[keep], minlength=len(sizes))
    keep &= sizes[ring_of] >= 3
    pts, sizes = pts[keep], sizes[sizes >= 3]
    if not len(sizes):
        return 0
    ring_of, first, local = _ring_layout(sizes)
    n = sizes[ring_of]
    run = pts[first[ring_of] + (local + 1) % n] - pts  # edge i runs from point i to the next
    came = run[first[ring_of] + (local - 1) % n]
    turn = np.arctan2(came[:, 0] * run[:, 1] - came[:, 1] * run[:, 0], (came * run).sum(axis=1))
    length = np.hypot(run[:, 0], run[:, 1])
    perimeter = np.bincount(ring_of, weights=length)
    along = np.cumsum(length) - length
    at = along - along[first][ring_of]  # how far along its ring each point is
    # Each ring twice round, one ring after another, so a stretch may run on
    # past its ring's start and never into the next ring.
    base = np.r_[0.0, np.cumsum(2.0 * perimeter)[:-1]][ring_of]
    copy = 2 * first[ring_of] + local
    pos, turns = np.empty(2 * len(pts)), np.empty(2 * len(pts))
    pos[copy], pos[copy + n] = base + at, base + perimeter[ring_of] + at
    turns[copy], turns[copy + n] = turn, turn
    end = np.searchsorted(pos, base + at + _CORNER_ARC_MM, side="right")
    turned = np.r_[0.0, np.cumsum(turns)]
    sharp = (perimeter[ring_of] > 2.0 * _CORNER_ARC_MM) & (
        turned[end] - turned[copy] >= math.radians(_SHARP_CORNER_TURN_DEG)
    )
    if not sharp.any():
        return 0
    # Each sharp stretch names the point in it that turns most; points closer
    # together than the stretch are one corner, read round the ring.
    ring = ring_of[sharp]
    peak = first[ring] + (_first_largest(turns, copy[sharp], end[sharp]) - 2 * first[ring]) % sizes[ring]
    peak = np.unique(peak)  # in ring order, and along each ring
    ring, where = ring_of[peak], at[peak]
    rings_here, lead = np.unique(ring, return_index=True)
    same = np.r_[ring[1:] == ring[:-1], False]
    gap = np.where(
        same,
        np.r_[where[1:], 0.0] - where,
        where[lead[np.searchsorted(rings_here, ring)]] + perimeter[ring] - where,
    )
    apart = np.bincount(ring[gap >= _CORNER_ARC_MM], minlength=len(sizes))
    return int(np.maximum(apart[rings_here], 1).sum())


def _footprint_sharp_corners(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    *,
    layer_height: float = 0.2,
) -> int:
    """How many sharp outside corners the part's footprint has: the outline of
    its bed contact (the triangles within one layer of the bottom, the contact
    the bed-adhesion grade reads), read by :func:`_sharp_corners_of_rings`.
    Each patch of contact is its own rings, since patches share no point.  It
    counts the shape, not how the file cut it into triangles; a part with no
    contact, a cone on its tip, has none."""
    threshold = z_min + layer_height
    contact = [
        (tri[0][:2], tri[1][:2], tri[2][:2])
        for tri in triangles
        if tri[0][2] <= threshold and tri[1][2] <= threshold and tri[2][2] <= threshold
    ]
    if not contact:
        return 0
    tris = np.asarray(contact, dtype=np.float64)
    e1, e2 = tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
    # A wall shorter than a layer lies flat on the bed as a line: no area, no outline.
    tris = tris[np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) / 2.0 > 1e-12]
    if not len(tris):
        return 0
    ids = _corner_ids(tris.reshape(-1, 2)).reshape(-1, 3)
    return _sharp_corners_of_rings(_outline_rings(*_patch_outline(tris, ids)))


def _analyze_warping(
    triangles: list[tuple[tuple[float, ...], ...]],
    vertices: list[tuple[float, ...]],
    bbox: dict[str, float],
    *,
    material: str = "pla",
    overlay: dict[str, Any] | None = None,
    bed_z: float | None = None,
    layer_height: float = 0.2,
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

    The base's corners are read on the bed contact within *layer_height* of
    *bed_z*, the plate the part rests on (the bounding box's bottom when it
    is not given).
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
            if area > 100.0:
                centroid = _triangle_centroid(tri[0], tri[1], tri[2])
                large_flat_surfaces.append({
                    "area_mm2": round(area, 2),
                    "centroid_x": round(centroid[0], 2),
                    "centroid_y": round(centroid[1], 2),
                    "centroid_z": round(centroid[2], 2),
                })
    # The ten largest, whichever triangles of the file they are.
    large_flat_surfaces.sort(
        key=lambda s: (-s["area_mm2"], s["centroid_x"], s["centroid_y"], s["centroid_z"]),
    )
    large_flat_surfaces = large_flat_surfaces[:10]

    sharp_corners_at_base = _footprint_sharp_corners(
        triangles, z_min if bed_z is None else bed_z, layer_height=layer_height,
    )

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
    # Read after the verdict, so a rule about corners can speak only for a
    # part at risk of warping and still be one metric against one threshold,
    # the shape every rule in the public list and the overlay has.
    metrics["sharp_corners_at_risk"] = (
        sharp_corners_at_base if risk_level in _CORNERS_WORTH_SAYING else 0
    )
    metrics["sharp_corners_phrase"] = (
        f"{sharp_corners_at_base} sharp outside corner{'' if sharp_corners_at_base == 1 else 's'}"
    )
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
# amplification (the Pro overlay's forgiving materials).  High-shrink
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
            p = tuple(c - a for c, a in zip(piece[0], apex, strict=True))
            q = tuple(c - a for c, a in zip(piece[1], apex, strict=True))
            r = tuple(c - a for c, a in zip(piece[2], apex, strict=True))
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

    # What a recommendation template may say.  The material's factor is
    # not among them: a sentence is a reply, and the factor is a number
    # the check is worked out from (``ThermalStressAnalysis._WORKED_OUT``),
    # so no template, the overlay's included, can print it.
    rec_metrics: dict[str, Any] = {
        "risk_level": risk_level,
        "max_ratio": max_ratio,
        "material": material,
        # Amplification-active threshold tightened to >= 1.2 (was > 1.0)
        # post wall-vs-face fix: under the old gate PETG was tripping the
        # enclosure recommendation even though PETG genuinely doesn't need
        # an enclosure.  >= 1.2 restricts the recommendation to materials
        # where chamber control is a real engineering necessity (ABS+,
        # Nylon+, PC+).
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


#: Directions the bed contact is tried in for the axis it tips over most
#: easily: every 5 degrees across a half turn.
_TIP_DIRECTIONS = 36

#: How much of a long footing holds a sideways push at one point of the
#: part's top: a stretch of it this many mm long per mm of the part's
#: height when the footing is thin across, rising to the second figure
#: when it is as wide across as the part is tall.
_HELD_STRETCH_THIN = 0.68
_HELD_STRETCH_WIDE = 0.96
#: Places along a long footing that stretch is tried at, in each direction,
#: its two ends among them.
_HELD_STRETCH_STEPS = 49
#: The most work the stretch check spends on one part, every direction
#: together, counted in edges read and edge-against-place readings.  Past it
#: the stretch is not measured and the contact is read as holding nothing,
#: which can only make the answer more cautious, never less.
_HELD_STRETCH_WORK = 40_000_000
#: Readings held in memory at once.
_HELD_STRETCH_BLOCK = 250_000
#: What one patch costs in one direction before any of its edges are read.
_HELD_STRETCH_PATCH_COST = 1_000


def _held_stretch(height: float, width: float) -> float:
    """The length (mm) of footing, *width* mm across, that holds a sideways
    push at one point of a part *height* mm tall; ``inf`` for a footing
    wider across than the part is tall, which is judged whole."""
    if height <= 0.0 or width > height:
        return math.inf
    share = width / height
    return height * (_HELD_STRETCH_THIN + (_HELD_STRETCH_WIDE - _HELD_STRETCH_THIN) * share)


def _patch_outline(tris: np.ndarray, ids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The outline of one patch of contact, *tris* ``(n, 3, 2)`` with its
    corners' :func:`_corner_ids` *ids* ``(n, 3)``: its edges ``(m, 2, 2)``,
    each running with the patch on its left, and the ids of their ends."""
    turn = (tris[:, 1, 0] - tris[:, 0, 0]) * (tris[:, 2, 1] - tris[:, 0, 1]) - (
        tris[:, 2, 0] - tris[:, 0, 0]
    ) * (tris[:, 1, 1] - tris[:, 0, 1])
    order = np.where((turn < 0.0)[:, None], np.array([0, 2, 1]), np.array([0, 1, 2]))
    ids = np.take_along_axis(ids, order, axis=1)
    head, tail = ids.reshape(-1), np.roll(ids, -1, axis=1).reshape(-1)
    count = int(ids.max()) + 1
    outer = ~np.isin(head * count + tail, tail * count + head)
    row, at = np.divmod(np.flatnonzero(outer), 3)
    edges = np.stack([tris[row, order[row, at]], tris[row, order[row, (at + 1) % 3]]], axis=1)
    return edges, np.stack([head[outer], tail[outer]], axis=1)


def _straightened(edges: np.ndarray, ids: np.ndarray) -> np.ndarray:
    """*edges*, an outline with its ends' ids *ids* (:func:`_patch_outline`),
    with each run of edges that carry on along one straight line joined into
    one edge.  The outline bounds exactly the same region: only corners
    where it does not turn are taken out."""
    if len(edges) < 2:
        return edges
    count = int(ids.max()) + 1
    leaving = np.bincount(ids[:, 0], minlength=count)
    arriving = np.bincount(ids[:, 1], minlength=count)
    follows = np.full(count, -1, dtype=np.int64)
    follows[ids[:, 0]] = np.arange(len(edges))
    after = follows[ids[:, 1]]
    run = edges[:, 1] - edges[:, 0]
    nxt = run[np.maximum(after, 0)]
    straight = (
        (after >= 0)
        & (leaving[ids[:, 1]] == 1)
        & (arriving[ids[:, 1]] == 1)
        & (run[:, 0] * nxt[:, 1] - run[:, 1] * nxt[:, 0] == 0.0)
        & ((run * nxt).sum(axis=1) > 0.0)
    )
    continued = np.zeros(len(edges), dtype=bool)
    continued[after[straight]] = True
    last = np.where(straight, after, np.arange(len(edges)))
    while True:
        jumped = last[last]
        if np.array_equal(jumped, last):
            break
        last = jumped
    heads = np.flatnonzero(~continued)
    return np.stack([edges[heads, 0], edges[last[heads], 1]], axis=1)


def _edge_moments(
    n0: np.ndarray, da: np.ndarray, dn: np.ndarray, lo: np.ndarray | float, hi: np.ndarray | float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """What the stretch of each outline edge from *lo* to *hi* of the way
    along it adds to a region's area and first and second moments along
    ``n``; the edge starts at *n0* and changes by *da*, *dn*."""
    u, v = n0 + lo * dn, n0 + hi * dn
    run = da * (hi - lo)
    uu, uv, vv = u * u, u * v, v * v
    return (
        run * (u + v) / 2.0,
        run * (uu + uv + vv) / 6.0,
        run * (uu * u + uu * v + u * vv + vv * v) / 12.0,
    )


def _readings_at_places(
    a0: np.ndarray,
    n0: np.ndarray,
    da: np.ndarray,
    dn: np.ndarray,
    whole: tuple[np.ndarray, ...],
    first: float,
    gap: float,
    count: int,
    budget: int,
) -> tuple[tuple[np.ndarray, ...] | None, int]:
    """At each of *count* places along ``a``, ``first + k * gap``: the area
    and the first and second moments along ``n`` of the part of a region at
    or below that place, and the least and most ``n`` its outline reaches
    there; and the work it took.

    The region is given by its outline edges, starting at ``(a0, n0)`` and
    changing by ``(da, dn)``, the region on each one's left in the
    ``(n, a)`` frame, with what each whole edge adds (:func:`_edge_moments`)
    in *whole*.  An edge reaching past a place adds all of itself; only
    an edge a place cuts through is read there.  ``None`` in place of the
    readings when that would take more than *budget*.
    """
    near, far = np.minimum(a0, a0 + da), np.maximum(a0, a0 + da)
    done = np.clip(np.ceil((far - first) / gap), 0, count).astype(np.int64)
    begin = np.clip(np.floor((near - first) / gap) + 1, 0, count).astype(np.int64)
    cuts = np.maximum(done - begin, 0)
    work = len(a0) + int(cuts.sum())
    if work > budget:
        return None, work
    readings = [np.cumsum(np.bincount(done, weights=m, minlength=count + 1))[:count] for m in whole]
    lo, hi = np.full(count, np.inf), np.full(count, -np.inf)
    cut = np.flatnonzero(cuts)
    if len(cut):
        ends = np.cumsum(cuts[cut])
        block = 0
        while block < len(cut):
            stop = int(np.searchsorted(ends, ends[block] - cuts[cut[block]] + _HELD_STRETCH_BLOCK, side="right"))
            edges = cut[block : max(stop, block + 1)]
            block = max(stop, block + 1)
            spans = cuts[edges]
            e = np.repeat(edges, spans)
            k = begin[e] + np.arange(len(e)) - np.repeat(np.cumsum(spans) - spans, spans)
            t = np.clip((first + k * gap - a0[e]) / da[e], 0.0, 1.0)
            rising = da[e] > 0.0
            part = _edge_moments(n0[e], da[e], dn[e], np.where(rising, 0.0, t), np.where(rising, t, 1.0))
            for total, piece in zip(readings, part, strict=True):
                total += np.bincount(k, weights=piece, minlength=count)
            at = n0[e] + t * dn[e]
            np.minimum.at(lo, k, at)
            np.maximum.at(hi, k, at)
    return (readings[0], readings[1], readings[2], lo, hi), work


def _range_extremes(
    first: np.ndarray, last: np.ndarray, values: np.ndarray, count: int,
) -> tuple[np.ndarray, np.ndarray]:
    """For each slot ``0 .. count - 1``, the least and most of *values* whose
    slot range ``first .. last`` (inclusive) holds it; ``inf`` / ``-inf``
    where none does."""
    first, last = np.maximum(first, 0), np.minimum(last, count - 1)
    keep = first <= last
    first, last, values = first[keep], last[keep], values[keep]
    levels = max(1, (count - 1).bit_length() + 1)
    lo, hi = np.full((levels, count), np.inf), np.full((levels, count), -np.inf)
    level = np.zeros(count + 1, dtype=np.int64)
    for size in range(2, count + 1):
        level[size] = level[size // 2] + 1
    span = level[last - first + 1]
    for at in (first, last - (1 << span) + 1):
        np.minimum.at(lo.reshape(-1), span * count + at, values)
        np.maximum.at(hi.reshape(-1), span * count + at, values)
    for q in range(levels - 1, 0, -1):
        half = 1 << (q - 1)
        lo[q - 1] = np.minimum(lo[q - 1], lo[q])
        hi[q - 1] = np.maximum(hi[q - 1], hi[q])
        lo[q - 1, half:] = np.minimum(lo[q - 1, half:], lo[q, : count - half])
        hi[q - 1, half:] = np.maximum(hi[q - 1, half:], hi[q, : count - half])
    return lo[0], hi[0]


def _held_stretch_modulus(pts: np.ndarray, height: float, budget: int) -> tuple[float, int]:
    """The weakest section modulus (mm³) of the contact when a push at one
    point is held, on each patch of it long enough for this to matter, by
    only a stretch of that patch (:func:`_held_stretch`), every other patch
    counted whole; over every direction and at every one of
    :data:`_HELD_STRETCH_STEPS` places along each such patch, its two ends
    among them.  ``inf`` when no patch is long enough.  With it, the work
    it spent of *budget*.

    *pts* is the contact as ``(T, 3, 2)`` triangles.  A stretch wider across
    than the part is tall is left to the whole contact.

    Bounded: the work is linear in the outline's edges plus one reading for
    each place an edge is cut at, and no more than :data:`_HELD_STRETCH_BLOCK`
    readings are held at once.  Past *budget* the answer is ``0.0``, the
    contact read as holding nothing, never a reading taken at fewer places,
    so a part too costly to measure can only be told it may come off the
    bed, never that it is secure.
    """
    weakest = math.inf
    if not len(pts):
        return weakest, 0
    shortest = _held_stretch(height, 0.0)
    corners = _corner_ids(pts.reshape(-1, 2))
    labels = _contact_patch_labels(pts, corners)
    pts = pts - pts.reshape(-1, 2).mean(axis=0)
    patches = int(labels.max()) + 1
    flat = pts.reshape(-1, 2)
    lo_xy = np.full((patches, 2), np.inf)
    hi_xy = np.full((patches, 2), -np.inf)
    corner_patch = np.repeat(labels, 3)
    np.minimum.at(lo_xy, corner_patch, flat)
    np.maximum.at(hi_xy, corner_patch, flat)
    long = np.flatnonzero(np.hypot(*(hi_xy - lo_xy).T) > shortest)
    if not len(long):
        return weakest, 0
    if len(long) * _TIP_DIRECTIONS * _HELD_STRETCH_PATCH_COST > budget:
        return 0.0, budget  # the patches alone would spend the budget
    # Each patch's area moments about the origin, for any direction at once.
    x, y = pts[:, :, 0], pts[:, :, 1]
    area = np.abs((x[:, 1] - x[:, 0]) * (y[:, 2] - y[:, 0]) - (x[:, 2] - x[:, 0]) * (y[:, 1] - y[:, 0])) / 2.0
    sums = np.stack([
        np.bincount(labels, weights=w, minlength=patches)
        for w in (
            area,
            area * x.sum(axis=1) / 3.0,
            area * y.sum(axis=1) / 3.0,
            area * ((x * x).sum(axis=1) + x[:, 0] * x[:, 1] + x[:, 1] * x[:, 2] + x[:, 2] * x[:, 0]) / 6.0,
            area * (
                2.0 * (x * y).sum(axis=1)
                + x[:, 0] * y[:, 1] + x[:, 1] * y[:, 0] + x[:, 0] * y[:, 2]
                + x[:, 2] * y[:, 0] + x[:, 1] * y[:, 2] + x[:, 2] * y[:, 1]
            ) / 12.0,
            area * ((y * y).sum(axis=1) + y[:, 0] * y[:, 1] + y[:, 1] * y[:, 2] + y[:, 2] * y[:, 0]) / 6.0,
        )
    ])
    totals = sums.sum(axis=1)
    order = np.argsort(labels, kind="stable")
    bounds = np.searchsorted(labels[order], np.stack([long, long + 1]))
    corners = corners.reshape(-1, 3)
    outlines = {}
    for i, lo, hi in zip(long, *bounds, strict=True):
        rows = order[lo:hi]
        outlines[int(i)] = _straightened(*_patch_outline(pts[rows], corners[rows]))
    work = 0
    for theta in np.pi * np.arange(_TIP_DIRECTIONS) / _TIP_DIRECTIONS:
        c, s = math.cos(theta), math.sin(theta)
        frame = np.array([[-s, c], [c, s]]).T

        def moments(m: np.ndarray, c: float = c, s: float = s) -> np.ndarray:
            return np.array([m[0], c * m[1] + s * m[2], c * c * m[3] + 2.0 * c * s * m[4] + s * s * m[5]])

        if patches > 1:
            across = flat @ np.array([c, s])
            reach_lo = np.full(patches, np.inf)
            reach_hi = np.full(patches, -np.inf)
            np.minimum.at(reach_lo, corner_patch, across)
            np.maximum.at(reach_hi, corner_patch, across)
            work += len(flat)
        for i in long:
            edges = outlines[int(i)]
            work += _HELD_STRETCH_PATCH_COST + len(edges)
            if work > budget:
                return 0.0, work
            if not len(edges):
                continue
            start, stop = edges[:, 0] @ frame, edges[:, 1] @ frame
            step = stop - start
            a_lo, a_hi = float(start[:, 0].min()), float(start[:, 0].max())
            if a_hi - a_lo <= shortest:
                continue
            gap = (a_hi - a_lo - shortest) / (_HELD_STRETCH_STEPS - 1)
            # A corner on a place's edge, to within rounding, is in that place.
            lo, hi = _range_extremes(
                np.ceil((start[:, 0] - a_lo - shortest) / gap - 1e-9).astype(np.int64),
                np.floor((start[:, 0] - a_lo) / gap + 1e-9).astype(np.int64),
                start[:, 1], _HELD_STRETCH_STEPS,
            )
            # The outline's corners alone already reach wider than the part
            # is tall in every place: no stretch of this patch is judged.
            if np.all(hi - lo > height):
                continue
            whole = _edge_moments(start[:, 1], step[:, 0], step[:, 1], 0.0, 1.0)
            readings = []
            for first in (a_lo, a_lo + shortest):
                read, spent = _readings_at_places(
                    start[:, 0], start[:, 1], step[:, 0], step[:, 1], whole,
                    first, gap, _HELD_STRETCH_STEPS, budget - work,
                )
                work += spent
                if read is None:
                    return 0.0, work
                readings.append(read)
            (a_low, f_low, s_low, x_lo_low, x_hi_low), (a_high, f_high, s_high, x_lo_high, x_hi_high) = readings
            lo = np.minimum(lo, np.minimum(x_lo_low, x_lo_high))
            hi = np.maximum(hi, np.maximum(x_hi_low, x_hi_high))
            stretch = np.array([_held_stretch(height, float(w)) for w in hi - lo])
            judged = (hi > lo) & np.isfinite(stretch)
            if not judged.any():
                continue
            sign = 1.0 if float(whole[0].sum()) >= 0.0 else -1.0
            piece = [sign * (high - low) for high, low in ((a_high, a_low), (f_high, f_low), (s_high, s_low))]
            held = judged & (piece[0] > 0.0)
            if not held.any():
                continue
            scale = np.minimum(stretch[held], a_hi - a_lo) / shortest
            rest = moments(totals) - moments(sums[:, i])
            area_, first_, second_ = (m[held] * scale + r for m, r in zip(piece, rest, strict=True))
            lo, hi = lo[held], hi[held]
            if patches > 1:
                lo = np.minimum(lo, np.delete(reach_lo, i).min())
                hi = np.maximum(hi, np.delete(reach_hi, i).max())
            centre = first_ / area_
            inertia = np.maximum(second_ - area_ * centre * centre, 0.0)
            reach = np.maximum(hi - centre, centre - lo)
            weakest = min(weakest, float((inertia / reach).min()))
    return weakest, work


#: The part is cut at heights between the heights its corners lie at, the
#: only heights its section can change at, to find where its separate
#: footings join: at most this many cuts.
_JOIN_LEVELS = 64
#: The most work the join reading spends, counted in triangles cut and in
#: footing points tested against outline segments.  Past it a footing not
#: yet found joined is read as standing alone up to the part's full height,
#: which can only make the answer more cautious.
_JOIN_WORK = 20_000_000


def _cut_loops(tris: np.ndarray, z: float) -> tuple[np.ndarray, np.ndarray] | None:
    """The outline of the mesh *tris* ``(T, 3, 3)`` cut at height *z*, no
    corner of the mesh at that height: the cut's segments ``(m, 2, 2)`` in XY
    and the closed loop each belongs to ``(m,)``, numbered from 0.  ``None``
    when the cut does not close everywhere."""
    below = tris[:, :, 2] < z
    rows = np.flatnonzero(below.any(axis=1) & ~below.all(axis=1))
    if not len(rows):
        return np.empty((0, 2, 2)), np.empty(0, dtype=np.int64)
    t = tris[rows]
    ends = []
    for a, b in ((0, 1), (1, 2), (2, 0)):
        u, v = t[:, a], t[:, b]
        # The edge's ends in one fixed order, so both triangles on it cut
        # it at the same point.
        swap = (u[:, 0] > v[:, 0]) | (
            (u[:, 0] == v[:, 0]) & ((u[:, 1] > v[:, 1]) | ((u[:, 1] == v[:, 1]) & (u[:, 2] > v[:, 2])))
        )
        lo, hi = np.where(swap[:, None], v, u), np.where(swap[:, None], u, v)
        crosses = (lo[:, 2] < z) != (hi[:, 2] < z)
        with np.errstate(divide="ignore", invalid="ignore"):
            s = (z - lo[:, 2]) / (hi[:, 2] - lo[:, 2])
            at = lo[:, :2] + s[:, None] * (hi[:, :2] - lo[:, :2])
        ends.append(np.where(crosses[:, None], at, np.nan))
    pts = np.stack(ends, axis=1)  # (c, 3, 2)
    hit = ~np.isnan(pts[:, :, 0])
    if not np.all(hit.sum(axis=1) == 2):
        return None
    seg = pts[hit].reshape(-1, 2, 2)
    ids = _corner_ids(seg.reshape(-1, 2)).reshape(-1)
    if np.any(np.bincount(ids) != 2):
        return None
    # The two segments meeting at each end are one loop.
    pair = (np.argsort(ids, kind="stable") // 2).reshape(-1, 2)
    return seg, _linked_labels(len(seg), pair[:, 0], pair[:, 1])


def _inside_loops(points: np.ndarray, seg: np.ndarray, loops: np.ndarray, count: int) -> np.ndarray:
    """Which of *count* loops, made of the segments *seg* ``(m, 2, 2)`` with
    the loop of each in *loops*, each of *points* ``(P, 2)`` lies inside:
    ``(P, count)``, a point inside a loop when a ray from it crosses the
    loop an odd number of times.  No more than :data:`_HELD_STRETCH_BLOCK`
    point-against-segment readings are held at once."""
    inside = np.zeros((len(points), count), dtype=bool)
    if not len(seg) or not len(points):
        return inside
    ax, ay, bx, by = seg[:, 0, 0], seg[:, 0, 1], seg[:, 1, 0], seg[:, 1, 1]
    step = max(1, _HELD_STRETCH_BLOCK // len(seg))
    for lo in range(0, len(points), step):
        px, py = points[lo : lo + step, 0][:, None], points[lo : lo + step, 1][:, None]
        spans = (ay > py) != (by > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_at = ax + (py - ay) * (bx - ax) / (by - ay)
        hits = spans & (px < x_at)
        rows = len(hits)
        slot = loops[None, :] + count * np.arange(rows)[:, None]
        crossings = np.bincount(slot[hits], minlength=rows * count).reshape(rows, count)
        inside[lo : lo + rows] = crossings % 2 == 1
    return inside


def _footing_join_heights(
    tris: np.ndarray, z_min: float, points: np.ndarray, height: float, budget: int,
) -> np.ndarray:
    """For each footing standing at one of *points* ``(P, 2)``, how far above
    the bed (mm) a layer of the mesh *tris* ``(T, 3, 3)`` first joins it to
    another footing; *height*, the part's height, for one no layer joins.

    Footings are joined at a height when their points lie inside the same
    loops of the part's section there, and inside the part.  The section is
    read at up to :data:`_JOIN_LEVELS` heights between the heights the mesh
    has corners at; when every such gap is read, the join is placed at the
    bottom of the gap it is first seen in, else at the height it is first
    seen at, which can only be higher than where it happened.  Past *budget*,
    or where a cut does not close, nothing more is joined.
    """
    heights = np.full(len(points), float(height))
    corners = np.unique(tris[:, :, 2])
    corners = corners[(corners > z_min) & (corners < z_min + height)]
    edges = np.concatenate(([z_min], corners, [z_min + height]))
    exact = len(edges) - 1 <= _JOIN_LEVELS
    bounds = edges if exact else edges[np.unique(np.linspace(0, len(edges) - 1, _JOIN_LEVELS + 1).round().astype(int))]
    joined = np.zeros(len(points), dtype=bool)
    work = 0
    for k in range(len(bounds) - 1):
        # Cut midway through the gap between corners this height lies in.
        i = int(np.searchsorted(edges, (bounds[k] + bounds[k + 1]) / 2.0, side="right"))
        level = (edges[i - 1] + edges[i]) / 2.0
        work += len(tris)
        if work > budget:
            break
        cut = _cut_loops(tris, level)
        if cut is None:
            continue
        seg, loops = cut
        count = int(loops.max()) + 1 if len(loops) else 0
        work += len(points) * len(seg)
        if work > budget:
            break
        if count == 0:
            continue
        inside = _inside_loops(points, seg, loops, count)
        # Footings inside the same loops, an odd number of them (inside the
        # part), stand in one piece at this height.
        _, group, size = np.unique(inside, axis=0, return_inverse=True, return_counts=True)
        together = (inside.sum(axis=1) % 2 == 1) & (size[group.reshape(-1)] > 1)
        new = together & ~joined
        if new.any():
            above = (bounds[k] if exact else level) - z_min
            heights[new] = above if above > 0.0 else level - z_min
            joined |= new
        if joined.all():
            break
    return heights


def _contact_modulus(pts: np.ndarray, height: float, budget: int) -> tuple[float, int]:
    """The section modulus (mm³) of the contact *pts* ``(T, 3, 2)`` about the
    axis it tips over most easily: the whole of it and, for a part *height*
    mm tall, the stretch of each long patch that holds a push at one point
    (:func:`_held_stretch_modulus`), whichever is weaker; and the work spent
    of *budget*.  ``0.0`` when nothing touches the bed."""
    x, y = pts[:, :, 0], pts[:, :, 1]
    a = np.abs(
        (x[:, 1] - x[:, 0]) * (y[:, 2] - y[:, 0]) - (x[:, 2] - x[:, 0]) * (y[:, 1] - y[:, 0])
    ) / 2.0
    area = float(a.sum())
    if area <= 0.0:
        return 0.0, 0
    # Area moments of each triangle, summed: first, then second about the origin.
    cx = float((a * x.sum(axis=1)).sum()) / (3.0 * area)
    cy = float((a * y.sum(axis=1)).sum()) / (3.0 * area)

    def _second(u: np.ndarray, v: np.ndarray) -> float:
        cross = (
            2.0 * (u * v).sum(axis=1)
            + u[:, 0] * v[:, 1] + u[:, 1] * v[:, 0]
            + u[:, 0] * v[:, 2] + u[:, 2] * v[:, 0]
            + u[:, 1] * v[:, 2] + u[:, 2] * v[:, 1]
        )
        return float((a * cross).sum()) / 12.0

    jxx = _second(x, x) - area * cx * cx
    jyy = _second(y, y) - area * cy * cy
    jxy = _second(x, y) - area * cx * cy
    theta = np.pi * np.arange(_TIP_DIRECTIONS) / _TIP_DIRECTIONS
    nx, ny = np.cos(theta), np.sin(theta)
    inertia = np.maximum(nx * nx * jxx + 2.0 * nx * ny * jxy + ny * ny * jyy, 0.0)
    corners = pts.reshape(-1, 2) - (cx, cy)
    reach = np.empty(_TIP_DIRECTIONS)
    chunk = max(1, _HELD_STRETCH_BLOCK // len(corners))
    for lo in range(0, _TIP_DIRECTIONS, chunk):
        reach[lo : lo + chunk] = np.abs(corners @ np.vstack((nx[lo : lo + chunk], ny[lo : lo + chunk]))).max(axis=0)
    reachable = reach > 0.0
    if not reachable.any():
        return 0.0, 0
    whole = float((inertia[reachable] / reach[reachable]).min())
    if height <= 0.0:
        return whole, 0
    stretch, spent = _held_stretch_modulus(pts, height, budget)
    if stretch <= 0.0:
        return 0.0, spent
    side, more = _held_side_modulus(pts, height, budget - spent)
    return min(whole, stretch, side), spent + more


#: A straight side of a footing's outline whose two ends are held by walls
#: at right angles, as a frame's or an open box's sides are, holds a push at
#: one point of its top with a stretch of itself this many mm long per mm of
#: the part's height when the side is thin across, rising to the second
#: figure when it is as thick across as the part is tall; a side thicker
#: than that is judged whole.
_HELD_SIDE_THIN = 1.85
_HELD_SIDE_WIDE = 1.96
#: How much of a long side's concentration a push near a held end makes, by
#: the push's distance from that end in heights (the middle of a side is
#: half its length from each end); linear between, and a long side beyond.
_HELD_SIDE_NEAR_END = ((0.0, 0.0), (0.5, 0.69), (1.0, 0.96), (2.0, 1.0))
#: An outline holds the end of a side when, within this many heights of path
#: past that end, it moves at least one height away from the side's line.
_HELD_SIDE_HOLD_PATH = 1.5
#: Sides shorter than this share of the part's height are not read: held at
#: both ends, they are stronger than any side that is.
_HELD_SIDE_SHORTEST = 0.125
#: A side is a run of outline edges that turn less than this in all; where
#: the outline has turned this much, a side ends.
_HELD_SIDE_RUN_TURN = math.radians(5.0)


def _outline_runs(edges: np.ndarray, nxt: np.ndarray) -> list[list[int]]:
    """The edges of the outline *edges* (``nxt`` the edge after each) in
    runs, each a list of edge indices in order along its loop, that turn less
    than :data:`_HELD_SIDE_RUN_TURN` in all; each loop's runs begin at its
    sharpest corner."""
    seen = np.zeros(len(edges), dtype=bool)
    runs: list[list[int]] = []
    for start in range(len(edges)):
        if seen[start]:
            continue
        loop: list[int] = []
        e = start
        while not seen[e]:
            seen[e] = True
            loop.append(e)
            e = int(nxt[e])
        d = edges[loop, 1] - edges[loop, 0]
        angle = np.arctan2(d[:, 1], d[:, 0])
        turn = np.abs((np.roll(angle, -1) - angle + np.pi) % (2.0 * np.pi) - np.pi)  # after each edge
        sharpest = int(np.argmax(turn))
        begin = (sharpest + 1) % len(loop) if turn[sharpest] >= _HELD_SIDE_RUN_TURN else 0
        run, bent = [loop[begin]], 0.0
        for j in range(1, len(loop)):
            k = (begin + j) % len(loop)
            bent += float(turn[k - 1])
            if bent >= _HELD_SIDE_RUN_TURN:
                runs.append(run)
                run, bent = [loop[k]], 0.0
            else:
                run.append(loop[k])
        runs.append(run)
    return runs


def _outline_departs(
    edges: np.ndarray, step: np.ndarray, at: int, a: np.ndarray, along: np.ndarray, height: float,
) -> tuple[bool, int]:
    """Whether the outline *edges*, walked from edge *at* by *step* (the
    index of the edge after, or before, each), moves at least *height* away
    from the line through *a* along *along* within :data:`_HELD_SIDE_HOLD_PATH`
    heights of path; and the edges walked."""
    norm = float(np.hypot(*along))
    if norm <= 0.0:
        return False, 0
    path, walked, e = 0.0, 0, int(step[at])
    while e >= 0 and e != at and path < _HELD_SIDE_HOLD_PATH * height:
        walked += 1
        run = edges[e, 1] - edges[e, 0]
        path += float(np.hypot(*run))
        for p in edges[e]:
            if abs(along[0] * (p[1] - a[1]) - along[1] * (p[0] - a[0])) / norm >= height:
                return True, walked
        e = int(step[e])
    return False, walked


def _held_side_modulus(pts: np.ndarray, height: float, budget: int) -> tuple[float, int]:
    """The weakest section modulus (mm³) of the contact where a straight
    side of a patch's outline (:func:`_outline_runs`), held at both ends by
    walls at right angles (:func:`_outline_departs`), holds a push at one
    point of its top by only the stretch of itself such a side holds it
    with: a strip as long as that stretch and as thick as the wall is there;
    ``inf`` when no patch has such a side.  With it, the work spent of
    *budget*; past the budget ``0.0``, the contact read as holding nothing.

    *pts* is the contact as ``(T, 3, 2)`` triangles.  A side thicker across
    than the part is tall is left to the whole contact; a curved outline has
    no such side, and is left to the whole contact too.
    """
    weakest = math.inf
    if not len(pts) or height <= 0.0:
        return weakest, 0
    corners = _corner_ids(pts.reshape(-1, 2))
    labels = _contact_patch_labels(pts, corners)
    corners = corners.reshape(-1, 3)
    patches = int(labels.max()) + 1
    order = np.argsort(labels, kind="stable")
    starts = np.searchsorted(labels[order], np.arange(patches + 1))
    work = 0
    shortest = _HELD_SIDE_SHORTEST * height
    flat = pts.reshape(-1, 2)
    corner_patch = np.repeat(labels, 3)
    lo_xy = np.full((patches, 2), np.inf)
    hi_xy = np.full((patches, 2), -np.inf)
    np.minimum.at(lo_xy, corner_patch, flat)
    np.maximum.at(hi_xy, corner_patch, flat)
    for i in np.flatnonzero(np.hypot(*(hi_xy - lo_xy).T) >= shortest):
        rows = order[starts[i] : starts[i + 1]]
        work += len(rows)
        if work > budget:
            return 0.0, work
        edges = _straightened(*_patch_outline(pts[rows], corners[rows]))
        if len(edges) < 3:
            continue
        ends = _corner_ids(edges.reshape(-1, 2)).reshape(-1, 2)
        count = int(ends.max()) + 1
        after = np.full(count, -1, dtype=np.int64)
        after[ends[:, 0]] = np.arange(len(edges))
        before = np.full(count, -1, dtype=np.int64)
        before[ends[:, 1]] = np.arange(len(edges))
        nxt, prv = after[ends[:, 1]], before[ends[:, 0]]
        if (nxt < 0).any() or (prv < 0).any():
            continue  # an outline that does not close is not read
        work += len(edges)
        if work > budget:
            return 0.0, work
        a, d = edges[:, 0], edges[:, 1] - edges[:, 0]
        for run in _outline_runs(edges, nxt):
            first, last = run[0], run[-1]
            chord = edges[last, 1] - edges[first, 0]
            length = float(np.hypot(*chord))
            if length < shortest:
                continue
            held, walked = _outline_departs(edges, nxt, last, edges[first, 0], chord, height)
            work += walked
            if held:
                held, walked = _outline_departs(edges, prv, first, edges[first, 0], chord, height)
                work += walked
            if work > budget:
                return 0.0, work
            if not held:
                continue
            # The wall's thickness under this side: from the side's middle,
            # into the patch, to the first outline edge met.
            middle = (edges[first, 0] + edges[last, 1]) / 2.0
            inward = np.array([-chord[1], chord[0]]) / length
            work += len(edges)
            if work > budget:
                return 0.0, work
            denom = inward[0] * d[:, 1] - inward[1] * d[:, 0]
            with np.errstate(divide="ignore", invalid="ignore"):
                s = ((a[:, 0] - middle[0]) * d[:, 1] - (a[:, 1] - middle[1]) * d[:, 0]) / denom
                u = ((a[:, 0] - middle[0]) * inward[1] - (a[:, 1] - middle[1]) * inward[0]) / denom
            hit = (np.abs(denom) > 1e-12) & (s > 1e-9) & (u >= -1e-9) & (u <= 1.0 + 1e-9)
            hit[run] = False
            if not hit.any():
                continue
            thick = float(s[hit].min())
            if thick >= height:
                continue
            share = float(np.interp(
                length / (2.0 * height), [p[0] for p in _HELD_SIDE_NEAR_END], [p[1] for p in _HELD_SIDE_NEAR_END],
            ))
            if share <= 0.0:
                continue
            stretch = height * (_HELD_SIDE_THIN + (_HELD_SIDE_WIDE - _HELD_SIDE_THIN) * thick / height) / share
            weakest = min(weakest, stretch * thick * thick / 6.0)
    return weakest, work


def _footing_section_modulus(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    *,
    layer_height: float = 0.2,
    height: float | None = None,
    tris: np.ndarray | None = None,
) -> float:
    """The section modulus (mm³) of the part's bed contact about the axis
    it tips over most easily; ``0.0`` when nothing touches the bed.

    The contact is the triangles lying within one layer of the bottom of
    the mesh, the same contact the bed-adhesion analysis measures, taken as
    their outline on the bed.  With *height*, the part's height, a patch of
    it longer than the stretch that holds a push at one point
    (:func:`_held_stretch`), and narrower across than the part is tall, is
    also judged with only that stretch of it, wherever along it the push
    lands (:func:`_held_stretch_modulus`, which is bounded in work and
    memory and reads a contact too costly to measure as holding nothing).

    A part standing on more than one patch of contact prints each patch as
    a column of its own until a layer joins it to another
    (:func:`_footing_join_heights`), so each patch is also judged alone, at
    the height that layer lies at, and the answer is scaled to the part's
    height so that one figure stands for the whole part; a patch no layer
    joins is judged alone at the part's full height.  Each patch's reading
    is charged to the same work budget, :data:`_HELD_STRETCH_PATCH_COST`
    and up, so a part on more patches than the budget can read, or whose
    patches spend it, is read as holding nothing, never as secure.
    """
    threshold = z_min + layer_height
    contact = [
        (tri[0][:2], tri[1][:2], tri[2][:2])
        for tri in triangles
        if tri[0][2] <= threshold and tri[1][2] <= threshold and tri[2][2] <= threshold
    ]
    if not contact:
        return 0.0
    pts = np.asarray(contact, dtype=np.float64)  # (T, 3, 2)
    height = float(height) if height is not None and height > 0.0 else 0.0
    modulus, spent = _contact_modulus(pts, height, _HELD_STRETCH_WORK)
    if height <= 0.0 or modulus <= 0.0:
        return modulus
    labels = _contact_patch_labels(pts)
    patches = int(labels.max()) + 1
    if patches < 2:
        return modulus
    budget = _HELD_STRETCH_WORK - spent - patches * _HELD_STRETCH_PATCH_COST
    if budget < 0:
        return 0.0  # the patches alone would spend the budget
    x, y = pts[:, :, 0], pts[:, :, 1]
    a = np.abs(
        (x[:, 1] - x[:, 0]) * (y[:, 2] - y[:, 0]) - (x[:, 2] - x[:, 0]) * (y[:, 1] - y[:, 0])
    )
    # The rows of each patch, grouped once; and each patch's largest
    # triangle, whose middle is the point the sections above are read at.
    order = np.argsort(labels, kind="stable")
    starts = np.searchsorted(labels[order], np.arange(patches + 1))
    points = pts[np.lexsort((a, labels))[starts[1:] - 1]].mean(axis=1)
    if tris is None:
        tris = np.asarray(triangles, dtype=np.float64)
    joins = _footing_join_heights(tris[:, :, :3], z_min, points, height, _JOIN_WORK)
    for i in range(patches):
        alone, used = _contact_modulus(pts[order[starts[i] : starts[i + 1]]], float(joins[i]), budget)
        budget -= used
        modulus = min(modulus, alone * height / float(joins[i]))
        if modulus <= 0.0:
            break
    return modulus


def _estimate_adhesion_force(
    contact_area_mm2: float,
    bbox: dict[str, float],
    *,
    material: str = "pla",
    overlay: dict[str, Any] | None = None,
    footing_modulus_mm3: float | None = None,
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

    Two tests, and the worse one decides the verdict: the peel balance
    above (``force_ratio``) and the push test (``tipping_ratio``), whether
    the part's bed contact holds the sideways push the nozzle can give the
    top of the part.  Tuning may make the push test stricter, never looser:
    the push is never taken below the public floor's, nor the hold above
    it.  *footing_modulus_mm3* is the contact's
    :func:`_footing_section_modulus`; without it the contact is read as a
    solid strip as long as the part, of which the stretch that holds a push
    at one point (:func:`_held_stretch`) is what counts.
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
    # by the material's stress factor.  Disabled by setting
    # thermal_z_scale to ``None`` in the overlay.  See
    # _ADHESION_FORCE_PUBLIC_DEFAULTS docstring for calibration
    # rationale (z_scale=100 chosen against the 31-case sweep; catches
    # all 8 truly-risky cases with zero false positives).
    thermal_z_scale = cfg.get("peel_thermal_z_scale", 100.0)
    if thermal_z_scale is not None and z_span > 0:
        stress_factor = _material_stress_factor(material)
        peel_force *= 1.0 + float(stress_factor) * (z_span / float(thermal_z_scale))

    force_ratio = adhesion_force / max(peel_force, 0.001)

    # The push test, read against the public floor whatever the tier.
    floor_push_n = float(_ADHESION_FORCE_PUBLIC_DEFAULTS["nozzle_push_force_n"])
    push_n = max(float(cfg.get("nozzle_push_force_n") or floor_push_n), floor_push_n)
    hold_strength = min(adhesion_strength, _DEFAULT_ADHESION_STRENGTH)
    if footing_modulus_mm3 is None:
        width = contact_area_mm2 / longest_xy if longest_xy > 0 else 0.0
        footing_modulus_mm3 = width * width * min(longest_xy, _held_stretch(z_span, width)) / 6.0
    tipping_ratio = hold_strength * footing_modulus_mm3 / max(push_n * z_span, 1e-9)

    deciding_ratio = min(force_ratio, tipping_ratio)
    will_detach = deciding_ratio < 1.0

    risk_thresholds = cfg.get("risk_thresholds", {})
    if deciding_ratio >= risk_thresholds.get("secure", 3.0):
        risk_level = "secure"
    elif deciding_ratio >= risk_thresholds.get("marginal", 1.5):
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

    # A plastic whose hold on a standard build surface Kiln does not vouch
    # for is never called secure, whatever its footing holds, on any tier.
    unvouched = _unvouched_on_a_standard_bed(material)
    withheld = unvouched is not None and risk_level == "secure"
    if withheld:
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
            "material: verify your bed surface is clean and level. "
            "The force-balance verdict "
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
    elif deciding_ratio > 10.0 or deciding_ratio < 0.5:
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
        recommendations=(
            list(recommendations)
            + [f"{unvouched} holds a standard build surface weakly, so Kiln does not call this part's hold on one secure."]
            if unvouched is not None else recommendations
        ),
        model_confidence=model_confidence,
        tipping_ratio=round(tipping_ratio, 3),
        secure_withheld_for=unvouched if withheld else "",
    )


#: Plastics whose hold on a standard build surface Kiln does not vouch for,
#: by family, with the name each is called by: a part in one is never
#: called secure, whatever its footing.
_UNVOUCHED_ON_A_STANDARD_BED: dict[str, str] = {
    "pp": "PP", "nylon": "Nylon", "pa": "Nylon", "peek": "PEEK", "pc": "PC", "polycarbonate": "PC", "abs": "ABS",
}


def _unvouched_on_a_standard_bed(material: str | None) -> str | None:
    """The name of the family in :data:`_UNVOUCHED_ON_A_STANDARD_BED` that
    *material* belongs to (``abs_esd``, ``pc_abs``, ``pa6_gf`` and
    ``cf_nylon`` among them), or ``None``."""
    if not material:
        return None
    for token in material.strip().lower().replace("-", "_").replace("/", "_").replace(" ", "_").split("_"):
        if token in _UNVOUCHED_ON_A_STANDARD_BED:
            return _UNVOUCHED_ON_A_STANDARD_BED[token]
        if token.startswith("pa") and token[2:].isdigit():
            return _UNVOUCHED_ON_A_STANDARD_BED["pa"]
    return None
#: What needing supports costs, by the share of the underside they hold up:
#: nothing for the first tenth, then in a straight line to 30 points for the
#: whole underside.  30 was the cap of the triangle-count charge alone, and
#: half the 60 that charge, the bridge charge and the support-volume charge
#: once added up to for one underside.  So a ceiling over 60 % of the
#: underside costs 16 points and a cap on a stem (nearly all of it) 29-30.
#: The first tenth is free because the old charges let a small overhang go
#: for nothing (a share of the faces under 2 % rounded to 0 points, and a
#: support volume under 5 % of the part's cost none), and charging it now
#: would lower scores that this way of charging exists only to correct
#: upward (measured 2026-10-24: a 64-sided cylinder lying on its side, 9.5 %
#: of its underside held up, was charged nothing before).
_SUPPORT_FREE_PCT = 10.0
_SUPPORT_MAX_POINTS = 30


def _score_terms(
    overhangs: OverhangAnalysis,
    thin_walls: ThinWallAnalysis,
    bridging: BridgingAnalysis,
    bed_adhesion: BedAdhesionAnalysis,
    supports: SupportAnalysis,
    warping: WarpingAnalysis | None = None,
    thermal_stress: ThermalStressAnalysis | None = None,
    adhesion_force: AdhesionForceEstimate | None = None,
    overhang_scoring_pct: float | None = None,
    thin_wall_scoring_pct: float | None = None,
) -> dict[str, int]:
    """What each analysis takes off the score, keyed by its block in the
    report: 0 or a negative number of points.

    Every block the score charges states its own share as
    ``score_deduction``, so the score is ``100 + sum(terms)``, clamped, and
    nothing else.  Anyone reading a report -- a person, an agent, or code
    that re-scores the part from its blocks -- can account for every point.

    Needing supports is charged once, on the ``supports`` block, by the
    share of the underside supports hold up
    (``supports.support_area_percentage``).  The overhangs block finds those
    faces and charges nothing itself.  A block built without the mesh
    carries no area, and is charged by its overhang share of the triangles
    instead: ``overhang_scoring_pct`` when given (the share left once the
    undersides a slicer prints unaided are set aside), else
    ``overhangs.overhang_percentage``.

    ``thin_wall_scoring_pct`` is the share of measured points thinner
    than the nozzle.  The thin-wall block may report against a material's
    higher floor; the score deducts only for walls a nozzle cannot lay
    at all, so naming more thin walls never lowers anyone's score.
    """
    held_up = supports.support_area_percentage
    if held_up is None:
        held_up = (
            overhang_scoring_pct
            if overhang_scoring_pct is not None
            else overhangs.overhang_percentage
        )
    thin_pct = (
        thin_wall_scoring_pct
        if thin_wall_scoring_pct is not None
        else thin_walls.thin_wall_percentage if thin_walls.thin_wall_count > 0
        else 0.0
    )
    span = bridging.max_bridge_length_mm
    terms = {
        # Thin walls (max -25)
        "thin_walls": -min(25, int(thin_pct * 0.5)) if thin_pct > 0 else 0,
        # Needing supports (max -30), once, by how much of the underside
        # they hold up.  A count of triangles moved with the mesh (one
        # mushroom cap cost 4 points at one resolution and 8 at another);
        # an area does not.
        "supports": (
            -min(_SUPPORT_MAX_POINTS, int(
                _SUPPORT_MAX_POINTS * max(0.0, held_up - _SUPPORT_FREE_PCT) / (100.0 - _SUPPORT_FREE_PCT)
            ))
            if overhangs.needs_supports
            else 0
        ),
        # A bridge too long to span unaided (max -15), by how far it runs
        # past the self-support limit: 5 points just past it, 10 at twice
        # it, 15 at three times or more.  Only a bridge the supports charge
        # has not already paid for: a bridge deck that needs supports is a
        # face the overhang check counts, so when the overhangs are charged
        # its area is in theirs.  That leaves the bridge whose overhang
        # verdict was lifted because a slicer will likely bridge the whole
        # span, but which is longer than the limit.  A span at or under the
        # limit costs nothing: ``needs_supports_for_bridges`` is also set by
        # a short region too far from a supported edge to be reached
        # sideways, which is an overhang and not a long bridge.
        # The span, not the count of flat triangles: the same 30 mm arch
        # once scored 71, 65 and 63 at three mesh resolutions (2026-10-20).
        "bridging": (
            -min(15, 5 + max(0, int(round(
                5.0 * (span / _MAX_SELF_SUPPORTING_BRIDGE_MM - 1.0)
            ))))
            if bridging.needs_supports_for_bridges
            and not overhangs.needs_supports
            and span > _MAX_SELF_SUPPORTING_BRIDGE_MM
            else 0
        ),
        # Bed adhesion (max -15)
        "bed_adhesion": {"high": -15, "medium": -7}.get(bed_adhesion.adhesion_risk, 0),
    }
    # The verdicts that grade themselves carry their deduction already.
    for key, verdict in (
        ("warping", warping), ("thermal_stress", thermal_stress), ("adhesion_force", adhesion_force),
    ):
        if verdict is not None:
            terms[key] = verdict.score_deduction
    return terms


def _score_from_terms(terms: dict[str, int]) -> int:
    """100 less every deduction in *terms*, clamped to 0-100."""
    return max(0, min(100, 100 + sum(terms.values())))


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
    thin_wall_scoring_pct: float | None = None,
) -> int:
    """Compute a printability score from 0-100: 100 less every deduction
    :func:`_score_terms` finds."""
    return _score_from_terms(_score_terms(
        overhangs, thin_walls, bridging, bed_adhesion, supports,
        warping=warping, thermal_stress=thermal_stress, adhesion_force=adhesion_force,
        overhang_scoring_pct=overhang_scoring_pct, thin_wall_scoring_pct=thin_wall_scoring_pct,
    ))


#: Lowest score for each letter above F, best first.  Below the last one is F.
_GRADE_FLOORS: tuple[tuple[str, int], ...] = (("A", 90), ("B", 80), ("C", 70), ("D", 60))


def _score_to_grade(score: int) -> str:
    """Convert a 0-100 score to a letter grade."""
    for letter, floor in _GRADE_FLOORS:
        if score >= floor:
            return letter
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

#: Score deducted per placement failure, and per hard stop
#: (:func:`_hard_stops`).  Sized so that a single failure lands any report
#: under :data:`_PRINTABLE_SCORE_MIN` from any starting grade — a part over
#: the bed's edge, resting on a point, or 1000 times too small does not
#: print at all, so an otherwise clean A must not still read as one.
_PLACEMENT_PENALTY = 50

#: Slack allowed below z=0 before the part counts as off the bed.
#:
#: Sized against the PROCESS, not against float precision.  The old 0.001mm
#: was written for float noise, but a mesh does not arrive carrying float
#: noise — it arrives carrying GEOMETRIC noise, orders of magnitude larger:
#: a surface extracted on a voxel grid and then smoothed lands its vertices
#: wherever the interpolation put them.  A generated statue measured 0.014mm
#: below the plate and was failed as "off the bed" at 0 / F, with a message
#: that rounded to "0.0 mm below the build plate" — a fatal verdict on a
#: part that prints perfectly.
#:
#: The floor that matters is the thinnest layer any supported machine can
#: lay down, which this repo's own slicer catalogue puts at 0.08mm
#: (``adaptive_slicer`` min_layer_height_mm).  Half of that is material the
#: process cannot resolve in the first place, so clipping it costs nothing
#: real.  Deliberately loose in the safe direction: a part 0.03mm low loses
#: a slab far under one layer, while a false fatal teaches users that the
#: printability grade is noise.
_PLACEMENT_Z_TOLERANCE_MM = 0.04

#: Minimum score for a report to call itself printable: the lowest score that
#: grades above F, read off the same ladder as the letter.  It was a separate
#: 50 until 2026-10-19, so a 50-59 part read "grade F. Printable." and agents
#: were told ``prints: true`` beside an F.  Neither number was measured
#: against real prints; this only makes the two agree.
_PRINTABLE_SCORE_MIN = _GRADE_FLOORS[-1][1]


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
    if printer_id:
        try:
            from kiln.printers.bed_fit import get_build_volume

            # A model id, else a printer by the name its owner gave it:
            # that printer's model is the bed (the one resolver's answer).
            volume = get_build_volume(printer_id)
        except Exception:  # pragma: no cover - catalogue unavailable
            volume = None
        if volume is not None:
            return volume
    # No printer named: fall back to the bed THIS INSTALL is configured
    # for, resolved through the same helper the 3D stage draws its plate
    # from, so the check and the picture agree about the machine.
    #
    # Without this the fit check was dead on the path that needs it
    # most: generate_from_template and the template grading sweep call
    # analyze_printability with no printer_id at all, so a 400 x 200 mm
    # riser default graded a clean A against a 256 mm bed.  resolve_stage_plate
    # answers the reference plate for an install with no printer
    # configured, which is a stated bed rather than a guess.
    try:
        from kiln.stage_plate import resolve_stage_plate

        plate = resolve_stage_plate()
    except Exception:  # pragma: no cover - resolver unavailable
        return None
    if not isinstance(plate, dict):
        return None
    try:
        return (
            float(plate["x_mm"]), float(plate["y_mm"]), float(plate["z_mm"]),
        )
    except (KeyError, TypeError, ValueError):  # pragma: no cover
        return None


def _placement_fault_records(
    bbox: dict[str, float] | None,
    *,
    build_volume: tuple[float, float, float] | None = None,
    printer_id: str | None = None,
) -> list[PlacementFault]:
    """Detect placement faults, worst first, each with a stable name.

    The single placement detector: :func:`_placement_faults` is the text
    view of this list, so the words a user reads and the name a client
    branches on can never disagree about what was found.

    Pure detection — scores nothing and mutates nothing, so a caller can
    re-check the verdict later without deducting twice.

    Degrades quietly: an absent or malformed *bbox* skips both checks,
    and an unresolvable bed skips the fit check.  Neither raises.
    """
    span = _bbox_span(bbox)
    if span is None or bbox is None:
        return []

    faults: list[PlacementFault] = []

    dx, dy, dz = span
    volume = _resolve_placement_volume(build_volume, printer_id)
    if volume is not None:
        bx, by, bz = volume
        if dx > bx or dy > by or dz > bz:
            faults.append(
                PlacementFault(
                    name=PLACEMENT_FAULT_EXCEEDS_BED,
                    message=(
                        f"Model ({dx:.1f} x {dy:.1f} x {dz:.1f} mm) exceeds build "
                        f"volume ({bx:.0f} x {by:.0f} x {bz:.0f} mm)."
                    ),
                )
            )

    # Below the plate is where the file put the part, not a fault of it:
    # the slicer and Kiln's slice gate set it on the plate
    # (:func:`_below_plate_mm`, said once by :func:`_apply_placement_check`).
    return faults


def _below_plate_mm(bbox: dict[str, float] | None) -> float | None:
    """How far under the plate *bbox* reaches, past the tolerance, or ``None``."""
    z_min = (bbox or {}).get("z_min")
    if z_min is None or z_min >= -_PLACEMENT_Z_TOLERANCE_MM:
        return None
    return round(-float(z_min), 2)


def below_plate_note(below_mm: float | None) -> str:
    """The one sentence a report says about a part the file put under the
    plate, or ``""``.  2 decimals, so a dip of a few hundredths does not
    read as "0.0 mm"."""
    if below_mm is None:
        return ""
    return (
        f"As written, the part's lowest point is {below_mm:.2f} mm below the build "
        "plate (exporters often put the origin at the middle of the part). It was "
        "judged resting on the plate, which is where the slicer and Kiln's slice "
        "step put it; the file itself was not moved."
    )


def _placement_faults(
    bbox: dict[str, float] | None,
    *,
    build_volume: tuple[float, float, float] | None = None,
    printer_id: str | None = None,
) -> list[str]:
    """Return a recommendation per placement fault, worst first.

    Text view of :func:`_placement_fault_records` — same faults, same
    order, message only.  Kept as-is for the callers that splice these
    lines straight into a recommendation list.
    """
    return [
        fault.message
        for fault in _placement_fault_records(
            bbox, build_volume=build_volume, printer_id=printer_id,
        )
    ]


def _apply_placement_check(
    score: int,
    recommendations: list[str],
    bbox: dict[str, float] | None,
    *,
    build_volume: tuple[float, float, float] | None = None,
    printer_id: str | None = None,
) -> tuple[int, str, bool, list[str]]:
    """Grade where the part sits, on top of how it is shaped.

    Flags the placement fault no shape metric can see — a part larger
    than the resolved build volume — and says once when the file put the
    part below the plate, which the slicer settles by setting it on the
    plate (:func:`below_plate_note`), so it is no fault.

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
    # Said last: it is a fact about the file, never the reason to look.
    note = below_plate_note(_below_plate_mm(bbox))
    if note and note not in recommendations:
        recommendations.append(note)
    if faults:
        recommendations[:0] = faults
        score = max(0, score - _PLACEMENT_PENALTY * len(faults))

    printable = score >= _PRINTABLE_SCORE_MIN and not faults
    return score, _score_to_grade(score), printable, faults


def adhesion_advice(adhesion: AdhesionRecommendation | None) -> list[str]:
    """The brim / raft sentence the adhesion decision makes, if it made one.

    Empty when the decision declined a brim and a raft.  The report's
    recommendations read it, and so does any door that serves one block of
    the report on its own — the warping and adhesion-force blocks state
    their risk and name no brim, so such a door carries the decision with
    this or the brim is in the reply nowhere.
    """
    if adhesion is not None and (adhesion.brim_width_mm > 0 or adhesion.use_raft):
        return [adhesion.rationale]
    return []


def _build_recommendations(
    overhangs: OverhangAnalysis,
    thin_walls: ThinWallAnalysis,
    bridging: BridgingAnalysis,
    bed_adhesion: BedAdhesionAnalysis,
    supports: SupportAnalysis,
    warping: WarpingAnalysis | None = None,
    thermal_stress: ThermalStressAnalysis | None = None,
    adhesion_force: AdhesionForceEstimate | None = None,
    adhesion: AdhesionRecommendation | None = None,
    thin_walls_below_nozzle: bool | None = None,
) -> list[str]:
    """Generate actionable recommendations based on analysis results.

    Brim and raft advice comes only from ``adhesion`` — the one decision
    (:func:`recommend_adhesion`) — so the report never both prescribes
    and declines one.  The nozzle advice fires only for walls thinner
    than the nozzle (``thin_walls_below_nozzle``); a wall above the
    nozzle but under a material's floor is the material rule's to name.
    """
    recs: list[str] = []

    if overhangs.needs_supports:
        recs.append(
            f"Enable supports: {overhangs.overhang_percentage:.0f}% of triangles "
            f"are overhangs.  Consider re-orienting the model to reduce supports."
        )

    below_nozzle = (
        thin_walls_below_nozzle
        if thin_walls_below_nozzle is not None
        else thin_walls.thin_wall_count > 0
    )
    if below_nozzle:
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
            "Low bed contact area.  Re-orienting the model can increase the contact surface."
        )
    recs.extend(adhesion_advice(adhesion))

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


# Perimeters the mesh-volume estimate assumes; recorded on the estimate.
_COST_WALL_LAYERS: int = 3


def _analyze_cost(
    mesh: Any,
    file_path: str,
    material: str = "PLA",
    infill_percent: float = 20.0,
    needs_supports: bool = False,
    adhesion_type: str = "none",
    layer_height_mm: float = 0.2,
    nozzle_mm: float = 0.4,
) -> CostAnalysis:
    """Compute cost breakdown integrated with printability analysis.

    Uses :class:`~kiln.cost_estimator.CostEstimator` to produce a full
    cost estimate and generates cost-saving recommendations.  The weight
    is estimated from the mesh volume, never sliced; ``filament_source``
    and ``assumptions`` on the result say so, so it cannot be mistaken
    for a slicer's figure printed beside it.  ``adhesion_type`` is the
    brim or raft the adhesion decision chose.
    """
    from kiln.cost_estimator import CostEstimator
    from kiln.generation.validation import analyze_mesh

    estimator = CostEstimator()
    # Read the file once for every estimate below: the second read of a
    # 300k-triangle model was a quarter of the whole analysis (2026-10-26),
    # and the PLA and low-infill alternatives each read it again until
    # they were handed this reading too.
    mesh_analysis = analyze_mesh(file_path)

    estimate = estimator.estimate_from_mesh(
        file_path,
        material=material,
        infill_percent=infill_percent,
        wall_layers=_COST_WALL_LAYERS,
        layer_height_mm=layer_height_mm,
        nozzle_mm=nozzle_mm,
        include_supports=needs_supports,
        adhesion_type=adhesion_type,
        analysis=mesh_analysis,
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
            analysis=mesh_analysis,
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
    pla = estimator.get_material("PLA")
    if profile is not None and pla is not None and profile.cost_per_kg_usd > pla.cost_per_kg_usd:
        pla_est = estimator.estimate_from_mesh(
            file_path,
            material="PLA",
            infill_percent=infill_percent,
            include_supports=needs_supports,
            adhesion_type=adhesion_type,
            analysis=mesh_analysis,
        )
        savings = estimate.total_cost_usd - pla_est.total_cost_usd
        if savings > 0.01:
            recommendations.append(
                f"Using PLA (~${pla.cost_per_kg_usd:.0f}/kg) instead of {mat_upper} "
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
            analysis=mesh_analysis,
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
        filament_source=estimate.filament_source,
        assumptions={
            "infill_percent": infill_percent,
            "wall_layers": _COST_WALL_LAYERS,
            "layer_height_mm": layer_height_mm,
            "nozzle_mm": nozzle_mm,
            "adhesion": adhesion_type,
        },
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _material_wall_floor(
    material: str,
    *,
    printer_id: str | None,
    nozzle_diameter: float,
    connected_components: int,
    component_size_uniformity: float,
    genus: int,
) -> float | None:
    """The material's minimum wall, when kiln-pro has one to give.

    Asked BEFORE the walls are measured, so the thin-wall block reports
    against the same floor kiln-pro's material wall rule judges by
    (https://kiln3d.com) — one measurement that both read, never a
    second pass re-measured to agree.  ``None`` — no kiln-pro, a kiln-pro
    that predates the question, no floor for this material or this
    caller — leaves the nozzle as the threshold, the safety floor every
    tier gets.
    """
    try:
        from kiln_pro.bridge import pro_features
    except ImportError:
        return None
    try:
        if not pro_features.is_available("printability_overlay"):
            return None
        resolve = getattr(pro_features.printability_overlay, "resolve_wall_floor", None)
        if resolve is None:
            return None
        floor = resolve(
            material,
            printer_id=printer_id,
            nozzle_diameter_mm=nozzle_diameter,
            connected_components=connected_components,
            component_size_uniformity=component_size_uniformity,
            genus=genus,
        )
        floor = float(floor) if floor is not None else 0.0
    except Exception:  # noqa: BLE001 — the overlay must never break the public path
        logger.debug("material wall floor unavailable", exc_info=True)
        return None
    return floor if floor > 0.0 else None


def _tell_whoever_runs_this_machine(file_path: str, stopped: Exception) -> None:
    """Pass a stopped hole check to the operator's alert, when one is installed.

    A machine with an allowance is someone's server; they need to hear that
    a model went past it, or the allowance quietly becomes a size limit
    nobody chose.  The alert carries why the check stopped, since memory,
    time and a crash are tuned in different places, and the model's triangle
    count only when the file states one; any other file is described by its
    format and size.  Never raises: an alert that fails changes no answer.
    """
    try:
        from kiln_pro.bridge import pro_features  # type: ignore[import-not-found]

        tell = getattr(pro_features, "ops_alert", None)
        if tell is not None:
            from kiln.hole_check import stated_triangles

            facts: dict[str, Any] = {
                "cause": getattr(stopped, "cause", None),
                "held_mb": getattr(stopped, "held_mb", None),
                "allowed_mb": getattr(stopped, "allowed_mb", None),
                "time_limit_s": getattr(stopped, "time_limit_s", None),
            }
            triangles = stated_triangles(file_path)
            if triangles is not None:
                facts["triangles"] = triangles
            else:
                facts["format"] = Path(file_path).suffix.lstrip(".").upper() or None
                facts["size_mb"] = round(Path(file_path).stat().st_size / 2**20, 1)
            tell("hole_check_stopped", facts)
    except Exception:  # noqa: BLE001 -- see the docstring
        logger.debug("no operator alert for a stopped hole check", exc_info=True)


def analyze_printability(
    file_path: str,
    *,
    nozzle_diameter: float | None = None,
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

    :param file_path: Path to a mesh (STL, OBJ, GLB, 3MF) or a STEP file,
        which is analysed as Kiln's mesh of it.
    :param nozzle_diameter: Printer nozzle diameter in mm.  Left unsaid,
        the size is the one :func:`kiln.assumed_nozzle.assumed_nozzle`
        picks for *printer_id* -- the nozzle on record, else the printer's
        own setting, else the model's stock size, else 0.4 -- and
        ``report.nozzle`` says which.
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
        to Kiln Pro's support advice for the chosen style; see
        ``enrichment.supports_calibration`` on the returned report.
    :returns: A :class:`PrintabilityReport` with scores, grades, and
        recommendations.  When the kiln-pro package is installed (Pro+
        tier), the report is enriched with material-specific tuning
        and the ``enrichment`` field is populated -- a finding there may
        lower the score, never raise it; free / public installs see the
        safety-floor result unchanged.  A report Kiln Pro tuned keeps
        every verdict, risk, reason and fix in its reply and leaves out
        the numbers its checks were worked out from (:func:`_served`).
        See https://kiln3d.com for tier details.
    :raises ValueError: If the file cannot be parsed -- including a STEP
        file that could not be turned into a mesh, whose message is the
        converter's own and whose ``__cause__`` is the
        :class:`kiln.step_import.StepImportError` (a
        :class:`~kiln.step_import.NoBackendError` carries ``remedy``).
        Raised, never swallowed: a CAD file nobody could read must not come
        back looking analysed.

    Where it runs: with kiln-pro installed, the analysis goes through
    kiln-pro's ``run_mesh_analysis``, which may run it outside the calling
    process on a server that answers many people at once, so that one
    large mesh cannot take that server's memory with it.  The arguments
    and the report are the same either way.  Without kiln-pro, or without
    that runner, it runs here.
    """
    kwargs: dict[str, Any] = {
        "nozzle_diameter": nozzle_diameter,
        "layer_height": layer_height,
        "max_overhang_angle": max_overhang_angle,
        "build_volume": build_volume,
        "material": material,
        "infill_percent": infill_percent,
        "include_hole_detection": include_hole_detection,
        "printer_id": printer_id,
        "slicer_style": slicer_style,
    }
    runner = _analysis_runner()
    if runner is None:
        return _analyze_printability_here(file_path, **kwargs)
    return runner(_analyze_printability_here, file_path, **kwargs)


def _analysis_runner() -> Callable[..., Any] | None:
    """kiln-pro's runner for a whole-mesh analysis, or ``None``.

    The contract: ``runner(fn, file_path, **kwargs)`` returns what
    ``fn(file_path, **kwargs)`` returns and raises what it raises -- a
    ``MemoryError`` when the analysis needed more memory than the machine
    that ran it could give.  ``None`` on a plain install and on a kiln-pro
    that has no runner, and the analysis then runs in this process.
    """
    try:
        from kiln_pro.bridge import pro_features  # type: ignore[import-not-found]
    except ImportError:
        return None
    return getattr(pro_features, "run_mesh_analysis", None)


@dataclass
class RegionSummary:
    """One downward region of the part, as a person would point at it.

    Where it is, how big, how steep, how high, how far it spans, whether
    the verdict says it needs supports, and whether a support under it
    would have to stand on the model (``over_model``) rather than the
    plate -- the fact that decides whether plate-only supports reach it
    at all.
    """

    centroid: tuple[float, float, float]
    area_mm2: float
    max_angle_deg: float
    height_above_bed_mm: float
    span_mm: float
    free_air_span_mm: float
    needs_supports: bool
    over_model: bool
    triangle_count: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["centroid"] = [round(v, 2) for v in self.centroid]
        for key in ("area_mm2", "max_angle_deg", "height_above_bed_mm", "span_mm", "free_air_span_mm"):
            value = d[key]
            d[key] = None if value == math.inf else round(value, 2)
        return d


@dataclass
class SupportJudgement:
    """Everything the support verdict is read from, for one part.

    The overhangs (after the bridge and self-supporting exemptions), the
    bridges, the support volume, the angle they were judged against, and
    each downward region as a person would point at it (:class:`RegionSummary`).
    :func:`analyze_printability` builds its report on one of these;
    :func:`judge_supports` returns one on its own, for a door that needs
    the verdict and nothing else (the slice), or the regions (a support
    plan).
    """

    overhangs: OverhangAnalysis
    bridging: BridgingAnalysis
    supports: SupportAnalysis
    #: The overhang angle, from vertical, the verdict was read against.
    overhang_threshold_deg: float
    #: The nozzle the part was judged for, where it came from.
    nozzle: dict[str, Any] = field(default_factory=dict)
    triangle_count: int = 0
    #: Every downward region, needing supports or not, largest first.
    regions: list[RegionSummary] = field(default_factory=list)
    # The report's own bookkeeping, for its score and recommendations.
    downward_regions: list[_DownwardRegion] = field(default_factory=list)
    overhang_scoring_pct: float | None = None
    self_supporting_overhang_count: int = 0
    bridge_substituted_overhang_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "overhangs": self.overhangs.to_dict(),
            "bridging": self.bridging.to_dict(),
            "supports": self.supports.to_dict(),
            "overhang_threshold_deg": self.overhang_threshold_deg,
            "nozzle": dict(self.nozzle),
            "triangle_count": self.triangle_count,
            "regions": [region.to_dict() for region in self.regions],
        }


#: Samples per region for the stands-on question: enough to cover an arm
#: that leaves the model halfway along, few enough to stay cheap.
_REGION_SAMPLE_CAP: int = 48


def _summarize_regions(
    triangles: list[tuple[tuple[float, ...], ...]],
    regions: list[_DownwardRegion],
    z_min: float,
    *,
    layer_height: float = 0.2,
    tris: np.ndarray | None = None,
) -> list[RegionSummary]:
    """Each downward region as a person would point at it, largest first.

    ``over_model`` is read with rays straight down from points across the
    region: a face of the part below one of them, outside the region and
    more than a layer away, means a support there stands on the model --
    which plate-only supports never do (measured 2026-10-19: plate-only
    supports placed nothing under a 40 mm cap on a post over a 60 mm
    base).  A face touching the region from below (the post an arm sits
    on) is the part holding itself and asks for nothing.
    """
    if not regions or not triangles:
        return []
    if tris is None:
        tris = np.asarray(triangles, dtype=np.float64)  # (T, 3, 3)
    edge1 = tris[:, 1] - tris[:, 0]
    edge2 = tris[:, 2] - tris[:, 0]
    normals = np.cross(edge1, edge2)
    areas = 0.5 * np.linalg.norm(normals, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        unit_nz = np.where(areas > 0, normals[:, 2] / (2.0 * areas), 0.0)
    centroids = tris.mean(axis=1)

    out: list[RegionSummary] = []
    for region in regions:
        idx = np.asarray(region.triangle_indices, dtype=np.int64)
        if idx.size == 0:
            continue
        weights = areas[idx]
        total = float(weights.sum())
        if total <= 0:
            continue
        centre = (centroids[idx] * weights[:, None]).sum(axis=0) / total
        angles = np.degrees(np.arccos(np.clip(-unit_nz[idx], -1.0, 1.0)))
        max_angle = float(np.max(np.maximum(0.0, 90.0 - angles)))
        height = max(0.0, float(centroids[idx][:, 2].min()) - z_min)
        stride = max(1, idx.size // _REGION_SAMPLE_CAP)
        samples = centroids[idx[::stride]]
        over_model = any(
            (gap := _gap_to_model_below(tris, point, exclude=idx)) is not None and gap > layer_height
            for point in samples
        )
        out.append(RegionSummary(
            centroid=(float(centre[0]), float(centre[1]), float(centre[2])),
            area_mm2=total,
            max_angle_deg=max_angle,
            height_above_bed_mm=height,
            span_mm=float(region.span_mm),
            free_air_span_mm=float(region.free_air_span_mm),
            needs_supports=bool(region.needs_supports),
            over_model=over_model,
            triangle_count=int(idx.size),
        ))
    out.sort(key=lambda r: r.area_mm2, reverse=True)
    return out


def _gap_to_model_below(tris: np.ndarray, point: np.ndarray, *, exclude: np.ndarray) -> float | None:
    """The drop from *point* to the nearest face of the part straight below
    it (an XY point-in-triangle test, then the face's height at that XY),
    ignoring the faces in *exclude*; ``None`` when only the plate is below."""
    px, py, pz = float(point[0]), float(point[1]), float(point[2])
    mask = np.ones(len(tris), dtype=bool)
    mask[exclude] = False
    sub = tris[mask]
    if sub.size == 0:
        return None
    ax, ay = sub[:, 0, 0], sub[:, 0, 1]
    bx, by = sub[:, 1, 0], sub[:, 1, 1]
    cx, cy = sub[:, 2, 0], sub[:, 2, 1]
    det = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
    with np.errstate(invalid="ignore", divide="ignore"):
        u = ((px - ax) * (cy - ay) - (cx - ax) * (py - ay)) / det
        v = ((bx - ax) * (py - ay) - (px - ax) * (by - ay)) / det
    inside = (np.abs(det) > 1e-12) & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1.0 + 1e-9)
    if not inside.any():
        return None
    hit = sub[inside]
    u_h, v_h = u[inside], v[inside]
    z_at = hit[:, 0, 2] + u_h * (hit[:, 1, 2] - hit[:, 0, 2]) + v_h * (hit[:, 2, 2] - hit[:, 0, 2])
    below = z_at[z_at <= pz + 1e-6]
    if below.size == 0:
        return None
    return float(pz - below.max())


def _support_area(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    *,
    threshold_deg: float,
    layer_height: float,
    exempt: set[int],
    tris: np.ndarray | None = None,
) -> tuple[float, float]:
    """``(underside, held_up)``: the part's underside and the part of it
    supports must hold, in mm2, each face's area projected onto the plate.

    The faces held up are the ones :func:`_analyze_overhangs` counts (facing
    down at or past *threshold_deg*, not resting on the plate) less *exempt*,
    the faces of undersides a slicer bridges or reaches sideways.  Measured
    by area because a count of triangles moves with the mesh: the same
    mushroom cap read 8 % and 17 % of its faces at two resolutions.  The
    underside includes the faces the part stands on, so a small lip on a
    part standing flat is a small share and a cap on a stem nearly all of it.
    """
    if not triangles:
        return 0.0, 0.0
    if tris is None:
        tris = np.asarray(triangles, dtype=np.float64)
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    twice_area = np.linalg.norm(normals, axis=1)
    downward = normals[:, 2] < 0
    seen_from_below = np.where(downward, -normals[:, 2] / 2.0, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        unit_nz = np.where(twice_area > 0, normals[:, 2] / twice_area, 0.0)
    overhang_deg = np.maximum(0.0, 90.0 - np.degrees(np.arccos(np.clip(-unit_nz, -1.0, 1.0))))
    on_plate = (tris[:, :, 2] <= _bed_threshold_z(z_min, layer_height)).all(axis=1)
    held = downward & ~on_plate & (overhang_deg + _OVERHANG_LIMIT_SLACK_DEG >= threshold_deg)
    if exempt:
        held[np.fromiter(exempt, dtype=np.int64, count=len(exempt))] = False
    return float(seen_from_below.sum()), float(seen_from_below[held].sum())


def _judge_supports(
    triangles: list[tuple[tuple[float, ...], ...]],
    z_min: float,
    bbox: dict[str, float],
    *,
    resolved_threshold: float,
    layer_height: float,
    material: str,
    overlay: dict[str, Any] | None,
    tris: np.ndarray | None = None,
) -> SupportJudgement:
    """The support half of :func:`analyze_printability`, on a loaded mesh:
    the overhangs, the bridges, the support volume, and the exemptions
    that turn a flagged underside a slicer prints anyway back into "no
    supports".  One body for the report and for :func:`judge_supports`,
    so the two cannot drift.  *tris* is the same triangles as a
    ``(T, 3, 3)`` array when the caller holds one, handed to each reading
    below that would otherwise build its own."""
    overhangs = _analyze_overhangs(
        triangles,
        max_overhang_angle=resolved_threshold,
        z_min=z_min,
        layer_height=layer_height,
        normalize_winding=False,
        material=material,
        overlay=overlay,
    )
    downward_regions = _analyze_downward_regions(
        triangles, z_min, layer_height=layer_height, tris=tris,
    )
    bridging = _analyze_bridging(
        triangles,
        z_min,
        layer_height=layer_height,
        normalize_winding=False,
        precomputed_regions=downward_regions,
    )
    supports = _analyze_supports(
        triangles,
        z_min,
        max_overhang_angle=resolved_threshold,
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
            if not _at_overhang_limit(overhang_angle, resolved_threshold):
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
    underside, held_up = _support_area(
        triangles, z_min, threshold_deg=resolved_threshold,
        layer_height=layer_height, exempt=_exempt_overhang_tris, tris=tris,
    )
    supports = _dc_replace(
        supports,
        underside_area_mm2=round(underside, 2),
        support_area_mm2=round(held_up, 2),
        support_area_percentage=round(100.0 * held_up / underside, 1) if underside > 0 else 0.0,
    )
    return SupportJudgement(
        overhangs=overhangs,
        bridging=bridging,
        supports=supports,
        overhang_threshold_deg=resolved_threshold,
        triangle_count=len(triangles),
        regions=_summarize_regions(triangles, downward_regions, z_min, layer_height=layer_height, tris=tris),
        downward_regions=downward_regions,
        overhang_scoring_pct=_overhang_scoring_pct,
        self_supporting_overhang_count=_self_supporting_overhang_count,
        bridge_substituted_overhang_pct=_bridge_substituted_overhang_pct,
    )


def _mm_said(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") if value < 10 else f"{value:.0f}"


def _size_said(size: list[float]) -> str:
    return " x ".join(_mm_said(v) for v in size) + " mm"


def _judge_pieces(triangles: list[tuple[tuple[float, ...], ...]], reading: Any) -> PiecesAnalysis | None:
    """Whether *reading*'s pieces print as one solid, which separate parts
    they make and which of them float (:class:`PiecesAnalysis`).

    Pieces in the air are judged as :mod:`kiln.fusion_check` judges them,
    and said in its words, so the make doors that refuse a floating piece
    and this report name the same pieces.  ``None`` when the mesh has no
    pieces to read.
    """
    if reading is None or reading.piece_count == 0:
        return None
    solid_of = list(reading.solid_of) or list(range(reading.piece_count))
    lows: dict[int, np.ndarray] = {}
    highs: dict[int, np.ndarray] = {}
    for piece, solid in zip(reading.pieces, solid_of, strict=True):
        centre, half = np.asarray(piece.center), np.asarray(piece.size) / 2.0
        lows[solid] = np.minimum(lows.get(solid, centre - half), centre - half)
        highs[solid] = np.maximum(highs.get(solid, centre + half), centre + half)
    size_of = {s: [round(float(v), 2) for v in highs[s] - lows[s]] for s in lows}
    largest_first = sorted(size_of, key=lambda s: (-float(np.prod(size_of[s])), size_of[s]))
    block = PiecesAnalysis(
        piece_count=reading.piece_count,
        solid_count=len(size_of),
        parts_mm=[size_of[s] for s in largest_first[:_PARTS_LISTED]],
    )
    if block.solid_count < 2:
        return block

    from kiln.fusion_check import judge_reading

    try:
        fusion = judge_reading(triangles, reading)
    except Exception:  # noqa: BLE001 -- the parts are still counted; the air goes unjudged
        logger.debug("pieces in the air unjudged", exc_info=True)
        block.note = f"It prints as {block.solid_count} separate parts, not one."
        return block
    findings = fusion.get("findings") or []
    block.floating_bodies = int(fusion.get("floating_bodies") or 0)
    block.captive_bodies = int(fusion.get("captive_bodies") or 0)
    floating = next((f for f in findings if f.get("relation") == "floating"), None)
    if floating is not None:
        block.floating = str(floating["message"])
        return block
    unheld = set(fusion.get("unheld_bodies") or [])
    in_air = {solid_of[body - 1] for body in unheld}
    standing = [s for s in largest_first if s not in in_air]
    said: list[str] = []
    if len(standing) >= 2:
        largest, rest = size_of[standing[0]], [size_of[s] for s in standing[1:]]
        across = [max(size[0], size[1]) for size in rest]
        if len(rest) == 1:
            others = f"and one {_size_said(rest[0])}"
        elif max(across) - min(across) < 0.05:
            others = f"and {len(rest)} more, each {_mm_said(min(across))} mm across"
        else:
            others = f"and {len(rest)} more, {_mm_said(min(across))} to {_mm_said(max(across))} mm across"
        said.append(
            f"It prints as {len(standing)} separate parts on the plate, not one: the largest is "
            f"{_size_said(largest)} {others}, and each comes off the plate on its own. "
            "If they belong together, join them so they overlap."
        )
    # The pieces in the air that do print, by how the fusion check found
    # them: resting on the part, a clearance away from it, held inside it.
    resting: set[int] = set()
    close: set[int] = set()
    for finding in findings:
        named = unheld & set(finding.get("bodies") or [])
        if finding.get("relation") == "touching":
            resting |= named
        elif finding.get("relation") == "near":
            close |= named
    close -= resting
    if resting:
        said.append(_in_the_air_said(len(resting), resting=True))
    if close:
        said.append(_in_the_air_said(len(close), resting=False))
    captive = next((f for f in findings if f.get("relation") == "captive"), None)
    if captive is not None:
        said.append(str(captive["message"]))
    block.note = " ".join(said)
    return block


def _in_the_air_said(count: int, *, resting: bool) -> str:
    """What is said of *count* pieces off the plate that rest on the part
    without being joined to it (*resting*), or sit a clearance away from it."""
    from kiln.fusion_check import NEAR_GAP_MM, embedment_floor_mm

    one = count == 1
    them, they = ("it", "it") if one else ("them", "they")
    belong = "belongs" if one else "belong"
    sink = f"If {they} {belong} to the part, sink {them} at least {embedment_floor_mm():g} mm into it."
    pieces = "1 piece" if one else f"{count} pieces"
    if resting:
        rest = "rests" if one else "rest"
        return (
            f"{pieces} {rest} on the part without being joined to it: touching it along a "
            f"line, at a point or at a small patch, so a slicer joins {them} by a sliver at "
            f"most. {sink}"
        )
    sit = "sits" if one else "sit"
    return (
        f"{pieces} {sit} less than {NEAR_GAP_MM:g} mm from the part, joined to nothing, "
        f"so {they} {'prints' if one else 'print'} loose on it: fine for a designed "
        f"clearance. {sink}"
    )


#: The parts a report lists by size, largest first; the count says the rest.
_PARTS_LISTED: int = 20


def _mesh_path_for_analysis(file_path: str) -> str:
    """A CAD file is analysed as Kiln's mesh of it, through the shared door
    (cached by content; anything already a mesh passes straight through).
    A failed conversion keeps the analysis's one error type, so every
    caller that handles an unreadable mesh handles this too; a path that
    is not a file is left for the parser, which says so in the same words
    whatever the extension."""
    if step_import.is_step_file(file_path) and Path(file_path).is_file():
        try:
            file_path, _conversion_note = step_import.ensure_mesh_path(file_path)
        except step_import.NoBackendError as exc:
            raise ValueError(str(exc)) from exc
        except step_import.StepImportError as exc:
            raise ValueError(f"Could not read that CAD file: {exc}") from exc
    return file_path


def _load_mesh_for_analysis(
    file_path: str,
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]], dict[str, float], float]:
    """The mesh as every analysis here reads it: parsed, membranes dropped,
    winding normalised, with its bounding box and the plate it rests on."""
    triangles, vertices = _parse_mesh(file_path)
    return _prepare_parsed(triangles, vertices)


def _prepare_parsed(
    triangles: list[tuple[tuple[float, ...], ...]],
    vertices: list[tuple[float, ...]],
) -> tuple[list[tuple[tuple[float, ...], ...]], list[tuple[float, ...]], dict[str, float], float]:
    """:func:`_load_mesh_for_analysis` on a mesh already parsed."""
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
    return triangles, vertices, bbox, z_min


def _analyze_printability_here(
    file_path: str,
    *,
    nozzle_diameter: float | None = None,
    layer_height: float = 0.2,
    max_overhang_angle: float | None = None,
    build_volume: tuple[float, float, float] | None = None,
    material: str = "pla",
    infill_percent: float = 20.0,
    include_hole_detection: bool = True,
    printer_id: str | None = None,
    slicer_style: Literal["grid", "snug", "organic", "tree"] = "grid",
) -> PrintabilityReport:
    """:func:`analyze_printability`'s work, wherever it is run."""
    from kiln.assumed_nozzle import assumed_nozzle
    from kiln.design_intelligence import load_pro_overlay_or_empty

    # One size for every check below, and the report says where it came from.
    nozzle = assumed_nozzle(printer_id, stated=nozzle_diameter)
    nozzle_diameter = nozzle.diameter_mm

    # A CAD file is analysed as Kiln's mesh of it, through the shared door
    # (cached by content; anything already a mesh passes straight through).
    # Here rather than in each caller: until 2026-09-30 a STEP was refused
    # below as an unsupported type, so every door that sliced one either
    # skipped this analysis without a word or grew its own conversion.
    # A failed conversion keeps this function's one error type, so every
    # caller that already handles an unreadable mesh handles this too.
    # A path that is not a file is left for the parser below, which says so
    # in the same words whatever the extension.
    file_path = _mesh_path_for_analysis(file_path)

    # Holes first, before this function holds the mesh itself: on a machine
    # that gives the hole check an allowance (kiln.hole_check) the check runs
    # in a process of its own, and the two are then never held at once.
    # A hole-detection failure must never break the wider printability
    # path; an empty list is the documented degraded output.  A check the
    # machine stopped is a different thing from no holes, and is said.
    holes: list[dict[str, Any]] = []
    hole_diagnostics: dict[str, int] = {}
    holes_not_checked: str | None = None  # why the check stopped, when it did
    if include_hole_detection:
        from kiln.hole_check import HoleCheckStopped, find_holes

        try:
            holes = find_holes(file_path, diagnostics=hole_diagnostics)
        except (ValueError, OSError) as exc:
            # A path that cannot be read is the parser's to refuse, just below.
            logger.debug("detect_holes failed silently on %s: %s", file_path, exc)
        except HoleCheckStopped as exc:
            holes_not_checked = exc.cause or ""
            logger.warning("hole check stopped on %s: %s", Path(file_path).name, exc)
            _tell_whoever_runs_this_machine(file_path, exc)

    parsed, parsed_vertices = _parse_mesh(file_path)
    # Pieces as every door that counts them reads them (kiln.mesh_bodies),
    # on the file's own triangles: corners joined at a fraction of the
    # part's size, so a meter export counts the same as a millimeter one,
    # and two pieces laid on each other stay two.  Read before the
    # membranes are dropped and the winding is evened out, which both
    # rewrite the very contact that tells the pieces apart, and before the
    # analysis below holds its own copy of the mesh.
    pieces = None
    if parsed:
        from kiln.mesh_bodies import read_triangles

        with contextlib.suppress(ValueError, IndexError, MemoryError):
            pieces = read_triangles(parsed)
    # Which separate parts those pieces print as, and which float.
    parts = _judge_pieces(parsed, pieces)
    triangles, vertices, bbox, z_min = _prepare_parsed(parsed, parsed_vertices)
    del parsed, parsed_vertices

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
    # The same array, and the component label of each triangle, built once
    # here and handed to every reading below that reads them; each would
    # otherwise rebuild both from the same triangles (measured 2026-10-26:
    # three labellings and six array builds of one 327,680-triangle mesh).
    shared_tris: np.ndarray | None = None
    shared_comp_ids: np.ndarray | None = None
    if len(triangles) > 0:
        try:
            tris_arr = np.asarray(triangles, dtype=np.float64)
            component_count = pieces.piece_count if pieces is not None else 0
            # Genus is Euler arithmetic on this mesh's own vertices, edges
            # and faces, so it takes the component count of the same
            # scheme (shared edges, exact corners), not the piece count.
            comp_labels = _label_mesh_components(tris_arr)
            mesh_genus = _compute_mesh_genus(tris_arr, int(comp_labels.max() + 1))
            tris_arr_for_pro = tris_arr
            shared_tris, shared_comp_ids = tris_arr, comp_labels
            if component_count >= 2 and pieces is not None:
                # Per-component bbox volume.  Coefficient of variation
                # (std / mean) measures spread; uniformity = 1 - clamp(cv,
                # 0, 1) maps to 0–1 with 1 = all components identical.
                comp_bbox_volumes = np.array([
                    float(np.prod(np.maximum(piece.size, 1e-12))) for piece in pieces.pieces
                ])
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
            shared_tris = shared_comp_ids = None
    # Walls are measured once, against the floor they will be judged by:
    # the material's minimum wall when kiln-pro supplies one, the nozzle
    # otherwise.  The topology is part of the question; the answer can
    # depend on it.
    wall_floor = _material_wall_floor(
        material,
        printer_id=printer_id,
        nozzle_diameter=nozzle_diameter,
        connected_components=component_count,
        component_size_uniformity=round(component_size_uniformity, 3),
        genus=mesh_genus,
    )
    # The walls and the gaps share one budget for the corners they read.
    corner_budget = _CornerBudget()
    thin_walls, sub_nozzle_wall_pct = _measure_thin_walls(
        triangles, vertices,
        nozzle_diameter=nozzle_diameter, thin_below_mm=wall_floor, corner_budget=corner_budget,
        tris=shared_tris, comp_ids=shared_comp_ids,
    )
    cavities = _analyze_cavity_widths(
        triangles, vertices, nozzle_diameter=nozzle_diameter, corner_budget=corner_budget,
        tris=shared_tris, comp_ids=shared_comp_ids,
    )
    bed_adhesion = _analyze_bed_adhesion(triangles, z_min, bbox, layer_height=layer_height)
    judged = _judge_supports(
        triangles, z_min, bbox,
        resolved_threshold=_resolved_threshold, layer_height=layer_height,
        material=material, overlay=judgment_overlay, tris=shared_tris,
    )
    overhangs, bridging, supports = judged.overhangs, judged.bridging, judged.supports
    _overhang_scoring_pct = judged.overhang_scoring_pct
    _self_supporting_overhang_count = judged.self_supporting_overhang_count
    _bridge_substituted_overhang_pct = judged.bridge_substituted_overhang_pct

    warping = _analyze_warping(
        triangles, vertices, bbox, material=material, overlay=judgment_overlay,
        bed_z=z_min, layer_height=layer_height,
    )
    thermal_stress = _analyze_thermal_stress(
        triangles, bbox, material=material, layer_height=layer_height,
        overlay=judgment_overlay,
    )
    adhesion_force = _estimate_adhesion_force(
        bed_adhesion.contact_area_mm2, bbox, material=material,
        overlay=judgment_overlay,
        footing_modulus_mm3=_footing_section_modulus(
            triangles, z_min, layer_height=layer_height, height=bbox["z_max"] - bbox["z_min"],
            tris=shared_tris,
        ),
    )

    score_terms = _score_terms(
        overhangs, thin_walls, bridging, bed_adhesion, supports,
        warping=warping, thermal_stress=thermal_stress, adhesion_force=adhesion_force,
        overhang_scoring_pct=_overhang_scoring_pct,
        thin_wall_scoring_pct=sub_nozzle_wall_pct,
    )
    for key, block in (
        ("thin_walls", thin_walls), ("bridging", bridging),
        ("bed_adhesion", bed_adhesion), ("supports", supports),
    ):
        block.score_deduction = score_terms[key]
    score = _score_from_terms(score_terms)
    grade = _score_to_grade(score)
    # The one brim / raft decision for this part.  Contact, the adhesion
    # force balance and the warping verdict are its inputs; none of them
    # prescribes a brim on its own any more, so a report cannot say "no
    # brim needed" and "add a brim" at once.
    has_enclosure, on_bedslinger = _printer_adhesion_context(printer_id)
    adhesion = recommend_adhesion(
        bed_adhesion,
        material=material,
        has_enclosure=has_enclosure,
        is_bedslinger_printer=on_bedslinger,
        model_height_mm=bbox["z_max"] - bbox["z_min"],
        warping=warping,
        adhesion_force=adhesion_force,
    )
    recommendations = _build_recommendations(
        overhangs, thin_walls, bridging, bed_adhesion, supports,
        warping=warping, thermal_stress=thermal_stress, adhesion_force=adhesion_force,
        adhesion=adhesion,
        thin_walls_below_nozzle=sub_nozzle_wall_pct > 0,
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

    if holes_not_checked is not None:
        from kiln.child_interpreter import MEMORY, TIME_LIMIT

        # Only a memory stop is about the model's size; a check past its
        # time limit is often a busy machine, and a crash is neither.
        if holes_not_checked == MEMORY:
            why = f"{len(triangles):,} triangles is more than the hole check is given on this machine"
            remedy = "run it with Kiln installed on your own computer, where the check has no limit"
        elif holes_not_checked == TIME_LIMIT:
            why = "the check ran past the time this machine gives it"
            remedy = "try again, or run it with Kiln installed on your own computer, where the check has no limit"
        else:
            why = "the check stopped before it finished"
            remedy = "try again, or run it with Kiln installed on your own computer"
        recommendations.append(
            f"Kiln did not check this model's holes: {why}, so no hole here has been counted or sized. "
            f"Everything else in this report stands. To have the holes checked, {remedy}."
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
    # Read before the clamp: this is the verdict on the part's SHAPE,
    # which the floor is about to overwrite with a verdict on its shape
    # AND its placement.  Recomputing it afterwards is impossible.
    score_if_placed = score
    score, grade, printable, placement_faults = _apply_placement_check(
        score,
        recommendations,
        bbox,
        build_volume=build_volume,
        printer_id=printer_id,
    )
    # Same faults the line above turned into recommendations, kept in
    # structured form so a consumer can name the fault without reading
    # the sentence.  Pure detection — re-reading deducts nothing.
    placement = PlacementAnalysis(
        faults=_placement_fault_records(
            bbox, build_volume=build_volume, printer_id=printer_id,
        ),
        score_if_placed=score_if_placed,
        grade_if_placed=_score_to_grade(score_if_placed),
        printable_if_placed=score_if_placed >= _PRINTABLE_SCORE_MIN,
        below_plate_mm=_below_plate_mm(bbox),
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
            adhesion_type=(
                "raft" if adhesion.use_raft
                else "brim" if adhesion.brim_width_mm > 0
                else "none"
            ),
            layer_height_mm=layer_height,
            nozzle_mm=nozzle_diameter,
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
        overhang_threshold_deg=_resolved_threshold,
        recommendations=recommendations,
        estimated_print_time_modifier=round(time_mod, 2),
        holes=holes,
        triangle_count=len(triangles),
        connected_components=component_count,
        component_size_uniformity=round(component_size_uniformity, 3),
        genus=mesh_genus,
        placement=placement,
        adhesion=adhesion,
        nozzle=nozzle.to_dict(),
        units_question=units_question(bbox),
        pieces=parts,
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
                # An overlay adds findings to this verdict; it does not
                # grade the part afresh.  Its score is taken only where it
                # is LOWER -- a finding this analysis could not make -- and
                # the grade and ``printable`` are then read off that score
                # by the rules above.  A higher score from elsewhere, taken
                # as given, once handed a part that needs supports under
                # all of it back as a printable A.  Other nested analysis
                # blocks (overhangs, thin_walls, etc.) remain authoritative
                # on the dataclass.
                try:
                    overlay_score = int(enriched["score"])
                except (KeyError, TypeError, ValueError):
                    overlay_score = report.score
                if overlay_score < report.score:
                    report.score = max(0, overlay_score)
                    report.grade = _score_to_grade(report.score)
                    report.printable = report.printable and report.score >= _PRINTABLE_SCORE_MIN
                if isinstance(enriched.get("recommendations"), list):
                    report.recommendations = list(enriched["recommendations"])

    # Safety floor.  An overlay once wrote its own score / grade /
    # printable straight onto the report — which silently undid the
    # placement verdict and handed an off-bed or oversized part back as a
    # printable A.  The mirror above no longer lets any overlay raise the
    # verdict; this stays as the second lock on the one failure that
    # cannot print at all.  Placement is physics, not tuning, so it gets
    # the last word on every tier.  Clamping (never raising) keeps this
    # idempotent: re-applying the floor cannot deduct twice.
    # Re-asserted here, not only at construction, so the structured
    # verdict is governed by the same last-word rule as the score: no
    # overlay, on any tier, can hand back a report whose placement block
    # disagrees with the floor that was just applied.
    report.placement = placement
    if placement_faults:
        report.score = max(0, min(report.score, score))
        report.grade = _score_to_grade(report.score)
        report.printable = False
        for fault in reversed(placement_faults):
            if fault not in report.recommendations:
                report.recommendations.insert(0, fault)

    # Two more floors of the same kind: physics, not tuning, so they get
    # the last word on every tier and no overlay can raise them.  Measured
    # 2026-10-20 against PrusaSlicer on 51 parts: a cone on its tip
    # (0 mm2 of bed contact; the slicer refuses it, "no extrusions in the
    # first layer") and a box with 0.2 mm walls on a 0.4 mm nozzle (the
    # slicer lays down almost nothing) both came back "printable" at C,
    # because the thin-wall and adhesion deductions are capped and the
    # score alone cannot reach the floor.
    # A part that will not print as it is cannot read as an A beside the
    # sentence saying so: each stop costs what a placement fault costs, on
    # the score and on the score it would have if placed.
    stops = _hard_stops(report, nozzle_diameter)
    if stops:
        cost = _PLACEMENT_PENALTY * len(stops)
        report.stop_deduction = -min(cost, report.score)
        report.score = max(0, report.score - cost)
        report.grade = _score_to_grade(report.score)
        if report.placement is not None and report.placement.score_if_placed is not None:
            report.placement.score_if_placed = max(0, report.placement.score_if_placed - cost)
            report.placement.grade_if_placed = _score_to_grade(report.placement.score_if_placed)
            report.placement.printable_if_placed = False
    for stop in reversed(stops):
        report.printable = False
        if stop not in report.recommendations:
            report.recommendations.insert(0, stop)

    # A report any of Kiln Pro's data shaped is served without the numbers
    # that data worked out; one on the floor alone is served whole
    # (:func:`_served`).
    if (
        judgment_overlay
        or wall_floor is not None
        or report.enrichment is not None
        or _material_physics_from_overlay(material)
    ):
        _mark_tuned(report)
    return report


def units_question(bbox: dict[str, float]) -> str:
    """The units question the part's size raises, or ``""``: asked only when
    it reads smaller than anything a printer makes, in the words of the one
    judgement every door uses."""
    from kiln.generation.validation import unit_verdict

    largest = max(
        bbox["x_max"] - bbox["x_min"],
        bbox["y_max"] - bbox["y_min"],
        bbox["z_max"] - bbox["z_min"],
    )
    verdict = unit_verdict(float(largest))
    if not verdict.below_printable:
        return ""
    return (
        verdict.describe_unchanged()
        + "  Until it is, no other measurement in this report describes the part "
        "you meant."
    )


def _hard_stops(report: PrintabilityReport, nozzle_diameter: float) -> list[str]:
    """What makes *report*'s part unprintable as it is, beyond placement.

    Read off fields the analysis already measured, so :func:`verdict` can
    re-derive the same answer from a finished report.  A stop is a
    physical fact a slicer cannot work around: the part is smaller than
    anything a printer makes (its units are in question), nothing touches
    the bed, a piece floats with nothing under it, or a wall is thinner than
    the nozzle can extrude.  Cones and pyramids measure a zero-thickness
    apex without listing a thin REGION, so the wall rule reads the regions,
    not the minimum.
    """
    # The units first, and alone: at a thousandth of its size every other
    # measurement is of a part nobody meant, so its stops would send the
    # reader to fix the wrong thing ("lay it on a flat face").
    if report.units_question:
        return [report.units_question]
    stops: list[str] = []
    bed = report.bed_adhesion
    # Less than one nozzle-width square touching the bed is a point or an
    # edge: the first layer has nowhere to go.
    if bed is not None and bed.contact_area_mm2 < nozzle_diameter * nozzle_diameter:
        stops.append(
            f"Almost nothing touches the bed ({bed.contact_area_mm2:.2f} mm2): the "
            "part rests on a point or an edge, so the first layer has nowhere to "
            "go. Lay it on a flat face, or add a base."
        )
    # A piece in the air that nothing joins or holds prints as a loose part
    # on supports, or not at all (kiln.fusion_check's own sentence).
    if report.pieces is not None and report.pieces.floating:
        stops.append(report.pieces.floating)
    walls = report.thin_walls
    thin = [
        r for r in (walls.problematic_regions if walls is not None else [])
        if float(r.get("thickness_mm", nozzle_diameter)) < nozzle_diameter
    ]
    if thin:
        thinnest = min(float(r["thickness_mm"]) for r in thin)
        stops.append(
            f"A wall measures {thinnest:.2f} mm, thinner than the {nozzle_diameter:g} mm "
            f"nozzle can lay down; a slicer drops it, so that part of the model will "
            f"be missing. Thicken it to at least {2 * nozzle_diameter:g} mm."
        )
    return stops


def stops(report: PrintabilityReport) -> list[str]:
    """Why *report*'s part will not print as it is, worst first: its
    placement faults, then its hard stops (:func:`_hard_stops`).  Empty when
    nothing stops it.  The one reading :func:`verdict` and every pipeline
    that passes or fails a part take, so none can pass what this refuses."""
    faults = [f.message for f in report.placement.faults] if report.placement else []
    # The nozzle the analysis ran for.  Pro may set the thin-wall
    # threshold to a material minimum, which is tuning, not this floor.
    nozzle = float((report.nozzle or {}).get("diameter_mm") or 0.0)
    if nozzle <= 0.0 and report.thin_walls is not None and report.thin_walls.threshold_basis == "nozzle":
        nozzle = float(report.thin_walls.threshold_mm or 0.0)
    return faults + _hard_stops(report, nozzle or 0.4)


def _mm2_said(area: float) -> str:
    return f"{area:.0f}" if area >= 10.0 else f"{area:.1f}".rstrip("0").rstrip(".")


def footing(report: PrintabilityReport) -> str:
    """What the way *report*'s part stands on the bed asks of the print, as
    one sentence, or ``""``.

    Said after an answer that the part prints, so that answer is never an
    all-clear for a part that comes off the bed.  It speaks when the part
    stands on too little of the bed: its contact is in the ``high`` band.
    The fix is the report's own brim or raft decision
    (:func:`recommend_adhesion`), the one the slice applies.  A tube, a ring
    or a frame touches the bed only along its outline yet stands as steadily
    as it is wide, so it is not named: a report whose band reads the contact
    the outline holds down (``graded_contact_percentage``) already grades it
    out of the ``high`` band, and on one without that figure the share of
    the footprint the contact spans must be in the ``high`` band too.

    It also speaks when the adhesion-force verdict expects the part to come
    off the bed partway through (``likely_detach``) or says it may
    (``marginal``): a tall part on a narrow footing stands on all of its
    footprint, so the contact bands never see it.  When that verdict was
    kept from ``secure`` only by the plastic (``secure_withheld_for``), the
    sentence names the plastic and its weak hold on a standard build
    surface, not the part's height.  The words are the same on every tier;
    only the report's own verdict and brim decision vary.
    """
    bed = report.bed_adhesion
    if bed is not None and bed.adhesion_risk == "high" and _on_a_small_spot(bed):
        dims = report.dimensions_mm or {}
        width_mm, depth_mm = dims.get("width_mm"), dims.get("depth_mm")
        under = f" under a part {width_mm:.0f} x {depth_mm:.0f} mm across" if width_mm and depth_mm else ""
        return f"It stands on only {_mm2_said(bed.contact_area_mm2)} mm2 of the bed{under}, so {_hold_down(report)}."
    force = report.adhesion_force
    level = getattr(force, "risk_level", "") if force is not None else ""
    if level == "likely_detach":
        return f"It is tall for what it stands on and is likely to come off the bed before it finishes, so {_hold_down(report)}."
    if level == "marginal":
        weak = getattr(force, "secure_withheld_for", "")
        if weak:
            return f"{weak} holds a standard build surface weakly, so it could come off the bed before it finishes; {_hold_down(report)}."
        return f"It is tall for what it stands on and could come off the bed before it finishes, so {_hold_down(report)}."
    return ""


def _on_a_small_spot(bed: BedAdhesionAnalysis) -> bool:
    """Whether *bed*, already in the ``high`` band, stands on a small spot
    rather than around a wide outline: a band read from the held-down
    contact has told the two apart; otherwise the span of the contact does."""
    if getattr(bed, "graded_contact_percentage", None) is not None:
        return True
    span = getattr(bed, "contact_span_percentage", None)
    return span is not None and _adhesion_risk_for_contact(span) == "high"


def _hold_down(report: PrintabilityReport) -> str:
    """How *report*'s part is held down, from its own brim or raft decision."""
    rec = report.adhesion
    if rec is not None and rec.use_raft:
        return "print it on a raft"
    if rec is not None and rec.brim_width_mm > 0:
        width = rec.brim_width_mm
        article = "an" if str(width).startswith("8") or width in (11, 18) else "a"
        return f"print it with {article} {width} mm brim"
    return "print it with a brim"


def verdict(report: PrintabilityReport) -> tuple[str, str]:
    """The one thing the check says about a part, as ``(key, sentence)``.

    Three answers, never a number first: ``ready`` ("Ready to print."),
    ``prints_with_supports`` (the part is fine with supports, which is a
    normal print, not a warning), ``wont_print`` (a placement fault or a
    hard stop, named), and ``check`` for a score under the printable
    line with no single cause to point at.  The score stays on the
    report for comparing versions; it is not the headline.  An answer that
    the part prints, or asks for a check, goes on to say how it must be held
    down when it stands on one small spot or is expected to come off the bed
    (:func:`footing`), after its first sentence, and then that it prints as
    separate parts when it does (:class:`PiecesAnalysis`).
    """
    found = stops(report)
    if found:
        return "wont_print", f"Won't print as it is: {found[0]}"
    notes = (footing(report), report.pieces.note if report.pieces is not None else "")
    reason = support_verdict(report)
    if reason:
        return "prints_with_supports", " ".join(filter(None, (f"Prints with supports ({reason}).", *notes)))
    if report.printable:
        return "ready", " ".join(filter(None, ("Ready to print.", *notes)))
    first = report.recommendations[0] if report.recommendations else "see the report"
    return "check", " ".join(filter(None, (f"Check before printing: {first}", *notes)))


def verdict_from_findings(findings: dict[str, Any]) -> tuple[str, str]:
    """:func:`verdict` for a part known only by its summary findings (an
    inspection bundle's printability channel, a
    :class:`BundlePrintabilityFindings`).

    The findings carry the verdict when their producer read it
    (``verdict`` / ``verdict_text``).  Older findings have only the summary
    fields, so it is rebuilt from them in the order :func:`verdict` takes,
    after whatever stops the part, which a caller that can see the part's
    placement says first.
    """
    if findings.get("verdict") and findings.get("verdict_text"):
        return str(findings["verdict"]), str(findings["verdict_text"])
    if findings.get("needs_supports"):
        return "prints_with_supports", "Prints with supports."
    if findings.get("printable"):
        return "ready", "Ready to print."
    recs = findings.get("recommendations") or []
    return "check", f"Check before printing: {recs[0] if recs else 'see the report'}"


def verdict_of(report: PrintabilityReport | BundlePrintabilityFindings) -> tuple[str, str]:
    """:func:`verdict` for either kind of report a door may hold.

    The verdict is a reading of the report, never a reason to lose it: a
    report it cannot read (a field missing or of the wrong kind) falls back
    to the report's own printable flag, and the debug log says why.
    """
    if isinstance(report, BundlePrintabilityFindings):
        return verdict_from_findings(report.to_dict())
    try:
        if not isinstance(report, PrintabilityReport):
            raise TypeError(f"not a printability report: {type(report).__name__}")
        return verdict(report)
    except Exception:  # noqa: BLE001 -- the report stands without its verdict
        logger.debug("printability verdict unavailable", exc_info=True)
        if getattr(report, "printable", False):
            return "ready", "Ready to print."
        return "check", "Check before printing: see the report."


def verdict_block(report: PrintabilityReport | BundlePrintabilityFindings) -> dict[str, str]:
    """The two keys every door that judges a part carries in its reply.

    ``printability_verdict`` is the key (``ready``, ``prints_with_supports``,
    ``wont_print`` or ``check``) and ``printability_verdict_text`` the
    sentence that says it, both from :func:`verdict_of`, so every door
    names the same verdict in the same words.  Never raises.
    """
    key, text = verdict_of(report)
    return {"printability_verdict": key, "printability_verdict_text": text}


#: The verdicts a part passes a printability gate with: it prints as it is,
#: or with the supports a slicer adds, which is a normal print.
PRINTS: frozenset[str] = frozenset({"ready", "prints_with_supports"})


def passes(verdict_key: str, score: int, *, bar: int, required: int | None = None) -> bool:
    """Whether a gate passes a part :func:`verdict` called *verdict_key*.

    The verdict decides.  A part that prints, as it is or with supports,
    passes whatever its score, and one that won't print (or was never
    judged) fails whatever its score: the score is for comparing versions
    of a part, not for telling a person whether it prints.  Only ``check``
    (under the printable line with no single cause to name) has nothing but
    the score to go on, so there the gate's own *bar* decides.

    *required* is a minimum the CALLER asked for, a quality bar of their
    own: it holds for every part, one that prints included.  One rule for
    every gate, so no door fails a part another door told the person prints
    unless the person asked for more.
    """
    if verdict_key == "wont_print" or not verdict_key:
        return False
    if required is not None and score < required:
        return False
    if verdict_key in PRINTS:
        return True
    return score >= bar


# ---------------------------------------------------------------------------
# Adhesion intelligence
# ---------------------------------------------------------------------------


def is_bedslinger(printer_id: str | None) -> bool:
    """Return True if *printer_id* is a known bed-slinger printer."""
    if not printer_id:
        return False
    return printer_id.lower().replace("-", "_").strip() in _BEDSLINGER_PRINTERS


def _printer_adhesion_context(printer_id: str | None) -> tuple[bool, bool]:
    """``(has_enclosure, is_bedslinger)`` for the adhesion decision.

    An unknown printer reads as open-frame and not a bed-slinger — the
    same defaults the decision takes when no printer is named.  The
    catalogue's ``"default"`` profile, which stands in for any printer it
    does not know, is unknown, not a statement about that printer.

    (The three doors that each resolved this used to read the profile as
    a dict; it is a :class:`~kiln.printer_intelligence.PrinterIntel`, so
    the read always failed into the ``except`` and every printer — an
    enclosed one included — was judged open-frame.)
    """
    if not printer_id:
        return False, False
    has_enclosure = False
    try:
        from kiln.printer_intelligence import get_printer_intel

        intel = get_printer_intel(printer_id)
        if getattr(intel, "id", "default") != "default":
            has_enclosure = bool(getattr(intel, "has_enclosure", False))
    except Exception:  # noqa: BLE001 — a missing catalogue row is not an error
        logger.debug("printer intel unavailable for %s", printer_id, exc_info=True)
    return has_enclosure, is_bedslinger(printer_id)


def recommend_adhesion(
    bed_adhesion: BedAdhesionAnalysis,
    *,
    material: str = "PLA",
    has_enclosure: bool = False,
    is_bedslinger_printer: bool = False,
    model_height_mm: float = 0.0,
    warping: WarpingAnalysis | None = None,
    adhesion_force: AdhesionForceEstimate | None = None,
) -> AdhesionRecommendation:
    """Recommend brim/raft settings based on model geometry + material + printer.

    The one brim / raft decision for a part.  Uses the share of the
    footprint the contact holds down (:func:`graded_contact`) and the
    adhesion risk read from it, from :class:`BedAdhesionAnalysis`, combined with
    material warping tendency and printer type, then raises the brim when
    the part's own warping or adhesion-force verdict calls for one.  Those
    two analyses describe their risk and leave the brim to this function,
    so a report can never say "no brim needed" and "add a brim" at once.

    :param bed_adhesion: Output from ``_analyze_bed_adhesion()``.
    :param material: Filament type (PLA, ABS, PETG, etc.).
    :param has_enclosure: Whether the printer has an enclosure.
    :param is_bedslinger_printer: Whether the printer is a bed-slinger.
    :param model_height_mm: Model height for tall-part brim logic.
    :param warping: The part's warping verdict; ``high`` asks for a 5 mm
        brim, ``critical`` for 8 mm.
    :param adhesion_force: The part's force-balance verdict;
        ``marginal`` asks for a 5 mm brim, ``likely_detach`` for 8 mm.
    :returns: :class:`AdhesionRecommendation` with slicer overrides.
    """
    mat_upper = material.upper()
    is_warp_material = mat_upper in _HIGH_WARP_MATERIALS
    # The share the contact holds down, which the risk band was read from;
    # a part standing on its outline is told that, not handed a share of
    # its footprint it barely touches.
    pct = graded_contact(bed_adhesion)
    risk = bed_adhesion.adhesion_risk
    outlined = pct > bed_adhesion.contact_percentage
    said = f"{pct:.1f}% of the footprint, inside the outline it stands on" if outlined else f"{pct:.1f}%"

    brim = 0
    raft = False
    rationale = ""

    # Decision matrix — first match wins.  The rows run from least contact
    # to most and every risk band has one, so a part never gets less help
    # for having less of itself on the bed, and only a ``low`` reading can
    # reach the "no brim needed" row at the bottom.
    if pct < 2.0:
        brim = 8
        raft = is_warp_material
        rationale = (
            f"Tiny contact area ({said if outlined else f'{pct:.1f}% of footprint'}) — extreme adhesion risk. "
            f"{'Raft recommended for warping material.' if raft else '8mm brim mandatory.'}"
        )
    elif pct < 5.0 and is_warp_material:
        brim = 8
        raft = True
        rationale = f"Low contact ({said}) with {mat_upper} (high warp) — raft recommended."
    elif pct < 5.0 and (is_bedslinger_printer or not has_enclosure):
        brim = 8
        rationale = f"Low contact ({said}) on {'bed-slinger' if is_bedslinger_printer else 'open-frame'} printer — 8mm brim."
    elif pct < 5.0:
        brim = 5
        rationale = f"Low contact area ({said}) — 5mm brim recommended."
    elif risk == "high" and is_warp_material:
        # The rest of the ``high`` band: 5% and up.  No narrower than the
        # ``medium`` rows below it and no wider than the rows above.
        brim = 8
        rationale = f"Low contact ({said}) with {mat_upper} (high warp) — wide 8mm brim."
    elif risk == "high":
        brim = 5
        rationale = f"Low bed contact ({said}) — 5mm brim recommended."
    elif risk == "medium" and is_warp_material:
        brim = 8
        rationale = f"Moderate contact with {mat_upper} (high warp) — wide 8mm brim."
    elif risk == "medium" and is_bedslinger_printer:
        brim = 5
        rationale = "Moderate contact on bed-slinger — 5mm brim for safety."
    elif risk == "medium":
        brim = 3
        rationale = f"Moderate bed contact ({said}) — 3mm brim recommended."
    elif is_warp_material and model_height_mm > 50.0:
        brim = 5
        rationale = f"Tall model ({model_height_mm:.0f}mm) with {mat_upper} — precautionary 5mm brim."
    elif is_warp_material and not has_enclosure:
        # An unknown printer counts as open-frame here, as it does for the
        # open-frame warping warning: a brim is cheap where it proves
        # unneeded, and a lifted corner on an open frame is not.
        brim = 5
        rationale = f"{mat_upper} warps without an enclosure — precautionary 5mm brim."
    else:
        rationale = f"Good bed contact ({said}), no brim needed."

    # The part's own verdicts can raise the brim, never lower it, and a
    # raft already covers what a brim would.  The widths are the ones
    # each verdict's own advice used to name before it stopped naming one.
    if not raft:
        raises: list[tuple[int, str]] = []
        if adhesion_force is not None:
            if adhesion_force.risk_level == "likely_detach":
                raises.append((8, (
                    "8mm brim: the adhesion-force check expects this part to "
                    "come off the bed at its height and footprint (a raft or "
                    "glue stick also works)."
                )))
            elif adhesion_force.risk_level == "marginal":
                weak = getattr(adhesion_force, "secure_withheld_for", "")
                raises.append((5, (
                    f"5mm brim: {weak} holds a standard build surface weakly." if weak else
                    "5mm brim: adhesion is borderline for this part's height and footprint."
                )))
        if warping is not None and warping.risk_level in ("high", "critical"):
            width = 8 if warping.risk_level == "critical" else 5
            raises.append((width, (
                f"{width}mm brim: {warping.risk_level} warping risk for "
                f"{mat_upper} — a brim holds the corners down."
            )))
        for width, reason in raises:
            if width > brim:
                brim, rationale = width, reason
        if brim == 0 and warping is not None and warping.risk_level == "moderate":
            rationale = (
                f"Good bed contact ({said}), no brim needed: the warping "
                f"risk for {mat_upper} is moderate, below the level that "
                f"calls for one."
            )

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
        contact_percentage=bed_adhesion.contact_percentage,
        rationale=rationale,
        slicer_overrides=overrides,
    )


# ---------------------------------------------------------------------------
# Failure diagnosis
# ---------------------------------------------------------------------------


def failure_symptom_queries(signals: dict[str, Any]) -> list[str]:
    """Symptom strings to search a printer's curated failure modes with.

    One builder for every diagnosis door.  The live diagnosis and the
    retry each kept their own copy, and the copies drifted (only one asked
    about thermal runaway); both also named the warp-prone materials by
    hand, four of the twenty :data:`_HIGH_WARP_MATERIALS` lists.
    """
    queries: list[str] = []

    risk = signals.get("adhesion_risk")
    if risk == "high":
        queries.append("bed adhesion failure")
        queries.append("print detached from bed")
    elif risk == "medium":
        queries.append("poor bed adhesion")

    tool_actual = signals.get("tool_temp_actual")
    tool_target = signals.get("tool_temp_target")
    if tool_actual is not None and tool_target is not None and abs(tool_actual - tool_target) > 10:
        queries.append("temperature fluctuation")
        queries.append("thermal runaway")

    if signals.get("print_error"):
        # The screen's form, not the raw field.  Bambu reports print_error as
        # a 32-bit decimal (302022663), and the machine's own display and
        # every searchable reference render that as 1200-8007.  The decimal
        # matched nothing and is not a string any user or catalog would
        # recognise; the screen form is what the failure modes claim.
        from kiln.printers.base import format_error_code

        queries.append(
            format_error_code(signals["print_error"]) or str(signals["print_error"])
        )

    if signals.get("overhang_pct", 0) > 30:
        queries.append("overhang failure")
    if signals.get("max_bridge_mm", 0) > 15:
        queries.append("bridge failure")

    if (
        str(signals.get("material") or "").upper() in _HIGH_WARP_MATERIALS
        and not signals.get("printer_has_enclosure")
    ):
        queries.append("warping")
        queries.append("layer splitting")

    if not queries:
        queries.append("print failure")
    return queries


def collect_failure_signals(
    *,
    state: Any = None,
    report: PrintabilityReport | None = None,
    printer_id: str | None = None,
    material: str | None = None,
) -> dict[str, Any]:
    """The signals :func:`diagnose_from_signals` reads, gathered one way.

    Every door that diagnoses a failure builds this dict here: the live
    diagnosis and the retry each built their own, and both copies carried
    the same two silent faults — the printer profile read as a dict (it is
    a :class:`~kiln.printer_intelligence.PrinterIntel`, so the read raised
    into a best-effort ``except`` and no diagnosis heard the enclosure or
    the printer's failure modes) and ``bridging.max_bridge_length`` (the
    field is ``max_bridge_length_mm``, so a long bridge never reached a
    verdict).  The material is set before the failure-mode search, which
    asks about it.

    :param state: The printer's state (``PrinterState``) or ``None``.
    :param report: The failed model's printability report, or ``None``.
    :param printer_id: Catalogue id of the machine being diagnosed.  An id
        the catalogue does not know comes back as its ``"default"``
        stand-in, which says nothing about this printer, so it stays
        unknown: no enclosure claim and no stand-in failure modes.
    :param material: The material that was printed.
    """
    signals: dict[str, Any] = {}
    if state is not None:
        signals["tool_temp_actual"] = state.tool_temp_actual
        signals["tool_temp_target"] = state.tool_temp_target
        signals["bed_temp_actual"] = state.bed_temp_actual
        signals["bed_temp_target"] = state.bed_temp_target
        if state.print_error:
            signals["print_error"] = state.print_error
    if report is not None:
        signals["adhesion_risk"] = report.bed_adhesion.adhesion_risk
        # The share the grade read, so the diagnosis and the report it
        # diagnoses describe the same contact.
        signals["contact_percentage"] = graded_contact(report.bed_adhesion)
        signals["overhang_pct"] = report.overhangs.overhang_percentage
        signals["max_bridge_mm"] = report.bridging.max_bridge_length_mm
    if material:
        signals["material"] = material.upper()
    if printer_id:
        try:
            from kiln.printer_intelligence import diagnose_issue, get_printer_intel

            intel = get_printer_intel(printer_id)
            if getattr(intel, "id", "default") != "default":
                signals["printer_has_enclosure"] = bool(intel.has_enclosure)
                modes: list[dict[str, str]] = []
                for symptom in failure_symptom_queries(signals):
                    modes.extend(diagnose_issue(printer_id, symptom))
                if modes:
                    signals["failure_modes_from_intel"] = modes
        except Exception:  # noqa: BLE001 — a catalogue miss is not a diagnosis error
            logger.debug("printer intelligence unavailable for %s", printer_id, exc_info=True)
    return signals


def diagnose_from_signals(
    signals: dict[str, Any],
    *,
    printer_id: str | None = None,
    material: str | None = None,
) -> PrintFailureDiagnosis:
    """Produce a failure diagnosis from collected physical signals.

    This is pure logic — no I/O, no adapter calls — making it easy to test.
    The ``signals`` dict comes from :func:`collect_failure_signals`.

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
