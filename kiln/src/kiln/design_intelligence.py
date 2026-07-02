"""Design intelligence engine for AI agents.

Gives agents structured knowledge about materials, design patterns, and
manufacturing constraints so they can reason about what makes a design
*good* — not just generate geometry.

The knowledge base is domain-extensible: FDM desktop printing today,
construction / medical / CNC tomorrow.  Same query interface, different
data files.

Public API:
    get_material_profile      — full property sheet for a material
    list_material_profiles    — all materials in a domain
    recommend_material        — best material for functional requirements
    estimate_load_capacity    — safe load estimate for cantilever geometry
    check_environment_compatibility — survivability check by environment
    get_printer_design_profile — capability profile for a printer
    list_printer_profiles     — all known printer capability profiles
    get_design_template       — constraints for a design template
    list_design_templates     — all templates in a domain
    get_design_constraints    — decompose functional requirements into rules
    match_requirements        — find which requirement profiles match text
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).parent / "data" / "design_knowledge"


# ---------------------------------------------------------------------------
# kiln-pro overlay (lazy, optional)
# ---------------------------------------------------------------------------


def _merge_pro_overlay_if_available(
    public_data: dict[str, dict[str, Any]],
    kind: str,
) -> dict[str, dict[str, Any]]:
    """Merge the kiln-pro overlay into ``public_data``.

    When kiln-pro is not installed, returns ``public_data`` as-is.
    When kiln-pro is installed and the license is valid, kiln-pro
    fetches the overlay from
    ``api.kiln3d.com/api/internal/overlay/<kind>`` at runtime,
    deep-merges it into each material/pattern record, and restores
    the full record (mechanical, design_limits, use_case_ratings,
    agent_guidance, brand-tunings, curated guidance).

    The overlay is NOT bundled in the kiln-pro wheel (closes the
    on-disk-grep leak vector for installs that carry kiln-pro on
    disk); it lives server-side and is fetched per process with an
    encrypted local cache.  See ``kiln_pro.data_overlays`` for the
    full caching behavior (24h TTL, 7d offline grace, license-key-
    derived cache encryption).  We never import kiln-pro at module
    load — only on first use — so the public package keeps working
    when kiln-pro isn't installed.

    :param public_data: Safety-floor data loaded from public Kiln's
        ``data/design_knowledge/<kind>.json``.
    :param kind: ``"materials"`` or ``"design_patterns"`` — selects
        which overlay file to load.
    :returns: Either the unmodified safety-floor dict (no overlay)
        or the deep-merged full record (overlay loaded).
    """
    try:
        from kiln_pro.data_overlays import load_overlay  # type: ignore[import-not-found]
    except ImportError:
        return public_data

    try:
        overlay = load_overlay(kind)
    except KeyError as exc:
        # Unknown overlay kind — programming error, not a runtime
        # failure.  Log loudly so we catch it early.
        logger.error(
            "kiln-pro overlay loader rejected kind=%r: %s. "
            "Update _OVERLAY_FILES in kiln_pro/data_overlays.py?",
            kind, exc,
        )
        return public_data
    except Exception as exc:
        # Catches OverlayUnavailableError (network / license / cache),
        # FileNotFoundError, ValueError, anything else.  No-overlay
        # behavior: silently fall back to safety-floor data, log at
        # warning level so the operator can see something happened.
        logger.warning(
            "kiln-pro %s overlay unavailable, falling back to safety-floor: %s",
            kind, exc,
        )
        return public_data

    return _deep_merge_dicts(public_data, overlay)


def _engineering_overlay_loaded() -> bool:
    """Probe whether the kiln-pro engineering overlay actually merged.

    Used by free-tier-honest consumer tools (``troubleshoot_print_issue``,
    ``get_post_processing``, ``check_environment_compatibility``, the
    ``troubleshoot_printer`` MCP wrapper) to attach an ``upgrade_hint``
    field when the response shape is shallow because the overlay didn't
    merge — so a free user (or an agent acting on their behalf) sees an
    honest "here's where to find the depth" signal instead of an empty
    array with no context.

    Returns False when kiln-pro is absent, the license is invalid, the
    network is past the offline-grace window, or the overlay endpoint
    is down.  Returns True only when the merge actually produced the
    curated content — verified structurally via ``pla.agent_guidance``,
    which only exists in the overlay-merged record.

    Cheap: a single dict lookup.  No I/O on the hot path.
    """
    try:
        kb = _get_kb()
        return bool(kb.materials.get("pla", {}).get("agent_guidance"))
    except Exception:
        return False


# Free-tier upgrade-hint copy.  Kept short and terminal-friendly so MCP
# agents can surface it verbatim; ends with the canonical pricing URL
# so a user (or the agent itself) can act on it without further lookup.
_UPGRADE_HINT_TROUBLESHOOTING = (
    "Kiln Pro adds per-symptom diagnostic playbooks "
    "(root cause + ordered fixes + prevention). "
    "See https://kiln3d.com/pricing"
)
_UPGRADE_HINT_POST_PROCESSING = (
    "Kiln Pro adds step-by-step procedures, safety notes, "
    "and strengthening tradeoffs. "
    "See https://kiln3d.com/pricing"
)
_UPGRADE_HINT_ENVIRONMENT = (
    "Kiln Pro adds curated SME notes on what each rating means "
    "and what to do about it. "
    "See https://kiln3d.com/pricing"
)
# Free-tier bonding nudge: shown when a recommended material carries the
# public common-knowledge `hard_to_bond` floor flag but no Pro overlay (so no
# precise verdict).  States the risk and points at the paid fix without
# telling the user to abandon gluing.  `{name}` = material display name.
_BONDING_FLOOR_NUDGE = (
    "{name} bonds poorly with common glues. Kiln Pro names the adhesive "
    "and surface prep that will actually hold it. See https://kiln3d.com/pricing"
)


def load_pro_overlay_or_empty(kind: str) -> dict[str, Any]:
    """Load a kiln-pro parameter-bag overlay, or ``{}`` on any failure.

    Public modules that need PARAMETER overlays — orientation scoring
    weights, structural thresholds, scorecard deduction rules,
    printability judgment tables — call this and branch on whether
    the returned dict is empty:

    - ``{}`` → no overlay loaded (kiln-pro not installed, no license,
      network down past the grace window, or unknown kind).  Caller
      uses its safe defaults.
    - non-empty → overlay loaded successfully.  Caller uses the
      curated values inside.

    Unlike :func:`_merge_pro_overlay_if_available`, this helper does
    not deep-merge — parameter overlays aren't entity-keyed; they're
    flat parameter groups the caller reads with ``.get()``.

    Never raises; silently returns ``{}`` when no overlay is loaded.
    """
    try:
        from kiln_pro.data_overlays import load_overlay  # type: ignore[import-not-found]
    except ImportError:
        return {}
    try:
        return load_overlay(kind)
    except KeyError as exc:
        # Unknown kind — programming error, not a runtime failure.
        # Log loudly so we catch typos in the caller's kind argument.
        logger.error(
            "kiln-pro overlay loader rejected kind=%r: %s. "
            "Update _KNOWN_KINDS in kiln_pro/data_overlays.py?",
            kind, exc,
        )
        return {}
    except Exception as exc:
        # OverlayUnavailableError, FileNotFoundError, ValueError, etc.
        # No-overlay equivalent: silently fall back to safe defaults,
        # log so operators can see something happened.
        logger.warning(
            "kiln-pro %s overlay unavailable, falling back to safe "
            "defaults: %s",
            kind, exc,
        )
        return {}


def _deep_merge_dicts(
    base: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base``, returning a new dict.

    Overlay values win on conflict.  Lists are replaced wholesale (not
    extended) — agent_guidance overlays cleanly without producing a
    safety-then-curated frankenstein.  Special keys starting with ``_``
    in either side are preserved (metadata).
    """
    result = dict(base)
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


# Rating scale for use-case compatibility
_RATING_ORDER = {
    "outstanding": 6,
    "excellent": 5,
    "good": 4,
    "moderate": 3,
    "conditional": 2,
    "poor": 1,
    "no": 0,
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MaterialProfile:
    """Full material property sheet for design reasoning.

    When the kiln-pro engineering overlay isn't loaded,
    ``mechanical``, ``design_limits``, ``use_case_ratings``,
    ``agent_guidance``, and ``bonding`` may be empty; consumers MUST
    treat these as optional and fall back to safety-floor inference
    when absent.  See :func:`has_engineering_data` for the canonical
    check.
    """

    material_id: str
    display_name: str
    category: str
    thermal: dict[str, Any]
    chemical: dict[str, Any]
    mechanical: dict[str, Any] = field(default_factory=dict)
    design_limits: dict[str, Any] = field(default_factory=dict)
    use_case_ratings: dict[str, Any] = field(default_factory=dict)
    agent_guidance: list[str] = field(default_factory=list)
    # kiln-pro adhesive-intelligence reverse-link: how hard this material
    # is to glue (bonding_difficulty / primer_required / recommended_primer
    # / compatible_adhesive_chemistries / bonding_note).  Present only when
    # the overlay merged (Pro+); empty for free tier.  See bonding_caveat().
    bonding: dict[str, Any] = field(default_factory=dict)

    def has_engineering_data(self) -> bool:
        """True when the kiln-pro engineering overlay is loaded.

        Returns False when the kiln-pro engineering overlay isn't
        loaded — consumers should fall back to safety-floor
        inference (see :func:`_recommend_from_safety_floor`) and
        emit the upgrade nudge in their response.
        """
        return bool(self.mechanical) and bool(self.use_case_ratings)

    def bonding_caveat(self) -> str:
        """A material-selection bonding warning, or ``""`` when none is warranted.

        Two tiers, by what the merged ``bonding`` block carries:

        * **Pro** (overlay supplied a ``bonding_difficulty``): a precise
          caveat that fires on ``hard``/``very_hard`` — never on
          ``primer_required`` alone, so a flexible material like TPU (hard
          to bond because rigid glue peels off, not because it needs a
          primer) still warns.  The ``bonding_note`` carries the how-to and
          ``recommend_adhesive`` has the full per-adhesive matrix.
        * **Free** (only the public common-knowledge ``hard_to_bond`` floor
          flag, no precise verdict): a generic nudge that states the risk
          and points at the paid fix.

        Returns ``""`` for an easy material or one with no ``bonding`` block.
        """
        difficulty = self.bonding.get("bonding_difficulty")
        if difficulty in ("hard", "very_hard"):
            primer = " — needs a primer" if self.bonding.get("primer_required") else ""
            note = self.bonding.get("bonding_note") or ""
            head = f"{self.display_name} is {difficulty.replace('_', ' ')} to bond{primer}."
            note_part = f" {note}" if note else ""
            return (
                f"{head}{note_part} "
                "Use recommend_adhesive for specific adhesives and surface prep."
            )
        if self.bonding.get("hard_to_bond"):
            return _BONDING_FLOOR_NUDGE.format(name=self.display_name)
        return ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DesignTemplate:
    """A functional design template with constraints and guidance.

    When the kiln-pro engineering overlay isn't loaded,
    ``design_rules``, ``print_orientation_reason``, and
    ``agent_guidance`` may be empty; consumers MUST treat these as
    optional and fall back to discovery-only behavior when absent.
    See :func:`has_engineering_data` for the canonical check.
    """

    template_id: str
    display_name: str
    description: str
    use_cases: list[str]
    material_compatibility: dict[str, list[str]]
    print_orientation: str
    design_rules: dict[str, Any] = field(default_factory=dict)
    print_orientation_reason: str = ""
    agent_guidance: list[str] = field(default_factory=list)

    def has_engineering_data(self) -> bool:
        """True when the kiln-pro engineering overlay is loaded.

        Returns False when the kiln-pro engineering overlay isn't
        loaded — consumers should fall back to discovery-only
        output (display_name, use_cases, material compatibility,
        print orientation label) and emit the upgrade nudge in
        their response.
        """
        return bool(self.design_rules) and bool(self.agent_guidance)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DesignConstraintSet:
    """A set of design constraints derived from functional requirements."""

    requirement_id: str
    display_name: str
    matched_triggers: list[str]
    constraint_rules: dict[str, Any]
    agent_guidance: list[str]
    caution: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SkinContactFloor:
    """The always-free skin-contact safety floor for one material.

    Worn-against-skin advisory ONLY — never a certification: no 3D-printed part
    is skin-safe, hypoallergenic, or biocompatible.  Holds only the free floor
    fields (honesty note, named hazards, refer-to-medical); the deeper
    engineering analysis is a Kiln Pro feature (https://kiln3d.com/pricing).
    """

    material_id: str
    display_name: str
    concern_level: str = ""
    concern_basis: str = ""
    honesty_note: str = ""
    named_hazards: list[str] = field(default_factory=list)
    refer_to_medical: str = ""

    def has_engineering_data(self) -> bool:
        # The free floor never carries the deeper analysis (a Kiln Pro
        # feature), so this is always False.
        return False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MaterialRecommendation:
    """A material recommendation with reasoning for design context."""

    material: MaterialProfile
    score: float
    reasons: list[str]
    warnings: list[str]
    design_limits_summary: dict[str, Any]
    alternatives: list[dict[str, Any]]
    recommended_brands: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["material"] = self.material.to_dict()
        return data


@dataclass
class LoadEstimate:
    """Estimated safe load capacity for a specific cantilever geometry."""

    material: str
    max_load_n: float
    safety_factor: float
    derating_applied: float
    reasoning: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnvironmentReport:
    """Material survivability report for a described environment.

    ``upgrade_hint`` is set (with the canonical Kiln Pro upsell + pricing
    URL) only when the kiln-pro engineering overlay didn't merge — i.e.
    free tier, missing license, or overlay endpoint unreachable past
    grace.  Pro+ tier with a valid license sees this field empty.  Lets
    an MCP agent surface the upgrade path verbatim without having to
    invent the copy.
    """

    material: str
    environment: str
    per_category_ratings: dict[str, Any]
    warnings: list[str]
    overall_verdict: str
    upgrade_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrinterDesignProfile:
    """Printer capability profile for design-for-manufacturing decisions.

    ``agent_notes`` is a curated field — the public file
    ships the spec sheet (build volume, temps, materials, layer heights)
    while the curated agent-facing notes move to the kiln-pro overlay
    (see Phase 2 catalog split in ``data/design_knowledge/_split_note``).
    ``agent_notes`` is therefore optional and defaults to an empty list
    when the overlay is not loaded.
    """

    printer_id: str
    display_name: str
    manufacturer: str
    build_volume_mm: dict[str, int]
    max_hotend_temp_c: int
    max_bed_temp_c: int
    has_enclosure: bool
    has_direct_drive: bool
    supported_materials: list[str]
    typical_tolerance_mm: float
    max_print_speed_mm_s: int
    default_layer_heights_mm: list[float]
    agent_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DesignBrief:
    """Design requirements analysis — output type of ``analyze_design_requirements``.

    Holds the combined material recommendation, applicable patterns,
    dimensional constraints, and guidance notes derived from a natural-
    language requirements string.  An agent uses this as the technical
    lookup before generating geometry.

    Internal class name retained for backward import compatibility.
    User-facing surfaces (JSON keys, tool docstrings, agent prompts)
    refer to this as "design requirements" to avoid colliding with
    kiln-pro's ``DesignBrief`` (the saved goal captured by
    ``design_session``, which is a different artifact entirely).
    """

    functional_constraints: list[DesignConstraintSet]
    recommended_material: MaterialRecommendation | None
    applicable_patterns: list[DesignTemplate]
    combined_guidance: list[str]
    combined_rules: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "functional_constraints": [c.to_dict() for c in self.functional_constraints],
            "recommended_material": self.recommended_material.to_dict()
            if self.recommended_material
            else None,
            "applicable_patterns": [p.to_dict() for p in self.applicable_patterns],
            "combined_guidance": self.combined_guidance,
            "combined_rules": self.combined_rules,
        }


@dataclass
class TroubleshootingResult:
    """Matched print issues with fixes for a material+symptom query.

    ``upgrade_hint`` is set (with the canonical Kiln Pro upsell + pricing
    URL) only when the kiln-pro engineering overlay didn't merge — i.e.
    free tier, missing license, or overlay endpoint unreachable past
    grace.  Pro+ tier with a valid license sees this field empty.  Lets
    an MCP agent surface the upgrade path verbatim without having to
    invent the copy.
    """

    material: str
    matched_issues: list[dict[str, Any]]
    storage_requirements: dict[str, Any] | None
    break_in_tips: list[str]
    upgrade_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrinterCompatibilityReport:
    """Whether a printer can handle a specific material (or all materials)."""

    printer_id: str
    materials: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PostProcessingGuide:
    """Post-processing techniques, paintability, and strengthening for a material.

    ``upgrade_hint`` is set (with the canonical Kiln Pro upsell + pricing
    URL) only when the kiln-pro engineering overlay didn't merge — i.e.
    free tier, missing license, or overlay endpoint unreachable past
    grace.  Pro+ tier with a valid license sees this field empty.  Lets
    an MCP agent surface the upgrade path verbatim without having to
    invent the copy.
    """

    material: str
    techniques: list[dict[str, Any]]
    paintability: dict[str, Any] | None
    strengthening: list[dict[str, Any]]
    upgrade_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MultiMaterialReport:
    """Co-print compatibility report between two materials."""

    material_a: str
    material_b: str
    compatible: bool
    interface_adhesion: str
    notes: str
    support_pair: dict[str, Any] | None
    general_rules: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrintDiagnostic:
    """Cross-file diagnostic combining troubleshooting, compatibility, and tips."""

    material: str
    printer_id: str | None
    symptom: str | None
    matched_issues: list[dict[str, Any]]
    printer_compatibility: dict[str, Any] | None
    storage_requirements: dict[str, Any] | None
    post_processing_tips: list[str]
    combined_guidance: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BrandFilamentProfile:
    """Brand-specific filament profile with printing parameters."""

    profile_id: str
    brand: str
    product_name: str
    parent_material: str
    nozzle_temp_range_c: list[int]
    nozzle_temp_optimal_c: int
    bed_temp_range_c: list[int]
    bed_temp_optimal_c: int | None
    max_volumetric_speed_mm3s: float | None
    max_print_speed_mms: int | None
    density_g_cm3: float | None
    drying_temp_c: int | None
    drying_time_hours: int | None
    enclosure_required: bool
    hardened_nozzle_required: bool
    ams_compatible: bool | None
    notes: str | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Knowledge base loader (lazy singleton)
# ---------------------------------------------------------------------------


class _DesignKnowledgeBase:
    """Loads and indexes the design knowledge JSON files."""

    def __init__(self, domain: str = "fdm") -> None:
        self.domain = domain
        self._materials: dict[str, dict[str, Any]] = {}
        self._templates: dict[str, dict[str, Any]] = {}
        self._requirements: dict[str, dict[str, Any]] = {}
        self._load_tables: dict[str, dict[str, Any]] = {}
        self._environment: dict[str, dict[str, Any]] = {}
        self._printers: dict[str, dict[str, Any]] = {}
        self._troubleshooting: dict[str, dict[str, Any]] = {}
        self._printer_compatibility: dict[str, dict[str, Any]] = {}
        self._post_processing: dict[str, dict[str, Any]] = {}
        self._multi_material: dict[str, Any] = {}
        self._skin_contact: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return

        _tables: list[tuple[str, str]] = [
            ("materials.json", "_materials"),
            ("design_templates.json", "_templates"),
            ("functional_requirements.json", "_requirements"),
            ("load_tables.json", "_load_tables"),
            ("environment_compatibility.json", "_environment"),
            ("printer_profiles.json", "_printers"),
            ("material_troubleshooting.json", "_troubleshooting"),
            ("printer_material_compatibility.json", "_printer_compatibility"),
            ("post_processing.json", "_post_processing"),
            ("multi_material_pairing.json", "_multi_material"),
            ("skin_contact.json", "_skin_contact"),
        ]

        for filename, attr in _tables:
            path = _DATA_DIR / filename
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                setattr(self, attr, {k: v for k, v in raw.items() if not k.startswith("_")})

        # Single choke point: merge the kiln-pro overlays
        # if present.  Without the overlay, the loader sees safety-floor
        # + discovery only; with the overlay, the full record
        # (mechanical + design_limits + use_case_ratings + agent_guidance
        # + brand-tuning + curated guidance for materials; design_rules
        # + agent_guidance + failure_modes + sources + variant tables
        # + Phase 4 depth for design_patterns) is restored via deep
        # merge.  Curated content is in kiln-pro; this loader never
        # imports kiln-pro at module load — only at first use.
        self._materials = _merge_pro_overlay_if_available(
            self._materials, "materials"
        )
        self._templates = _merge_pro_overlay_if_available(
            self._templates, "design_templates"
        )

        # Phase 2 catalog splits (2026-05-17) — public files carry the
        # safety floor + textbook math / spec sheets; the curated SME prose
        # (troubleshooting playbooks, post-processing procedures, per-(printer,
        # material) notes, environment notes, requirement worked examples,
        # load-table caveats, multi-material chemistry notes, printer
        # agent_notes) ships via these overlays when kiln-pro is installed.
        # See kiln_pro/data/DESIGN_KNOWLEDGE_LEAK_AUDIT.md for the field-by-
        # field classification.  printer_intelligence.json has its own loader
        # (kiln/printer_intelligence.py); its merge call lives there.
        self._troubleshooting = _merge_pro_overlay_if_available(
            self._troubleshooting, "material_troubleshooting"
        )
        self._post_processing = _merge_pro_overlay_if_available(
            self._post_processing, "post_processing"
        )
        self._printer_compatibility = _merge_pro_overlay_if_available(
            self._printer_compatibility, "printer_material_compatibility"
        )
        self._environment = _merge_pro_overlay_if_available(
            self._environment, "environment_compatibility"
        )
        self._requirements = _merge_pro_overlay_if_available(
            self._requirements, "functional_requirements"
        )
        self._load_tables = _merge_pro_overlay_if_available(
            self._load_tables, "load_tables"
        )
        self._multi_material = _merge_pro_overlay_if_available(
            self._multi_material, "multi_material_pairing"
        )
        self._printers = _merge_pro_overlay_if_available(
            self._printers, "printer_profiles"
        )
        # skin_contact's deeper analysis is a Kiln Pro feature delivered by the
        # skin-contact tools, not through this design-knowledge merge.  The
        # merge is wired for uniformity with every other kind; for skin_contact
        # it adds nothing to the public floor, and the SkinContactFloor
        # consumer exposes free-floor fields only.
        self._skin_contact = _merge_pro_overlay_if_available(
            self._skin_contact, "skin_contact"
        )

        self._loaded = True
        logger.info(
            "Design knowledge loaded: %d materials, %d templates, %d requirements, "
            "%d load tables, %d environments, %d printers, %d troubleshooting, "
            "%d printer-compat, %d post-processing",
            len(self._materials),
            len(self._templates),
            len(self._requirements),
            len(self._load_tables),
            len(self._environment),
            len(self._printers),
            len(self._troubleshooting),
            len(self._printer_compatibility),
            len(self._post_processing),
        )

    @property
    def materials(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._materials

    @property
    def templates(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._templates

    @property
    def requirements(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._requirements

    @property
    def load_tables(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._load_tables

    @property
    def environment(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._environment

    @property
    def printers(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._printers

    @property
    def troubleshooting(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._troubleshooting

    @property
    def printer_compatibility(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._printer_compatibility

    @property
    def post_processing(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._post_processing

    @property
    def multi_material(self) -> dict[str, Any]:
        self._load()
        return self._multi_material

    @property
    def skin_contact(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._skin_contact


# Module-level lazy singleton
_kb: _DesignKnowledgeBase | None = None


def _get_kb() -> _DesignKnowledgeBase:
    global _kb
    if _kb is None:
        _kb = _DesignKnowledgeBase()
    return _kb


# ---------------------------------------------------------------------------
# Public API — Skin contact (worn / handled against skin)
# ---------------------------------------------------------------------------


def get_skin_contact_floor(material_id: str) -> "SkinContactFloor | None":
    """The always-free skin-contact safety floor for a material, or ``None``.

    Worn-against-skin advisory only — never a skin-safe certification.  Returns
    only the free floor fields (honesty note, named hazards, refer-to-medical).
    """
    kb = _get_kb()
    rec = kb.skin_contact.get((material_id or "").lower())
    # Require a real material record: a merged overlay may add non-material
    # top-level keys (e.g. a standards cross-reference) that carry no floor.
    if not isinstance(rec, dict) or "safety_floor" not in rec:
        return None
    floor = rec.get("safety_floor") or {}
    return SkinContactFloor(
        material_id=(material_id or "").lower(),
        display_name=rec.get("display_name") or material_id,
        concern_level=rec.get("concern_level") or "",
        concern_basis=rec.get("concern_basis") or "",
        honesty_note=floor.get("honesty_note") or "",
        named_hazards=list(floor.get("named_hazards") or []),
        refer_to_medical=floor.get("refer_to_medical") or "",
    )


def use_case_implies_skin_contact(use_case: str) -> bool:
    """True when *use_case* describes a worn / handled-against-skin item.

    Offline-capable: matches against the ``against_skin`` functional-requirement
    profile's ``triggers`` (public, on-disk) and suppresses the homographs in
    its ``trigger_exclusions`` (a napkin ring, a band saw, a watch stand), so
    the caution fires without kiln-pro and stays credible.
    """
    if not isinstance(use_case, str) or not use_case.strip():
        return False
    prof = _get_kb().requirements.get("against_skin") or {}
    low = use_case.lower()
    if any(str(x).lower() in low for x in (prof.get("trigger_exclusions") or [])):
        return False
    return any(str(t).lower() in low for t in (prof.get("triggers") or []))


# ---------------------------------------------------------------------------
# Public API — Materials
# ---------------------------------------------------------------------------


def get_material_profile(material_id: str) -> MaterialProfile | None:
    """Get full material property sheet.

    :param material_id: Material key (e.g. ``"petg"``, ``"nylon"``).
    """
    kb = _get_kb()
    data = kb.materials.get(material_id.lower())
    if data is None:
        # Not in the curated catalog. A kiln-pro Business+ user may have
        # ingested this material's datasheet into their local library; resolve
        # it through the bridge when kiln-pro is installed. Free tier (or any
        # error) falls through to None. See https://kiln3d.com/pricing.
        ingested = None
        try:
            from kiln_pro.bridge import pro_features

            ingested = pro_features.get_ingested_material_profile(material_id)
        except Exception:  # noqa: BLE001 — kiln-pro absent/erroring → free-tier fallback
            ingested = None
        if ingested:
            # Fail closed: a malformed bridge payload (missing/extra keys) must
            # degrade to safety-floor inference, never raise out of a lookup.
            try:
                return MaterialProfile(**ingested)
            except (TypeError, KeyError) as exc:
                logger.debug(
                    "ingested material %r had an unexpected shape: %s",
                    material_id, exc,
                )
        return None

    return MaterialProfile(
        material_id=material_id.lower(),
        display_name=data["display_name"],
        category=data["category"],
        thermal=data.get("thermal", {}),
        chemical=data.get("chemical", {}),
        mechanical=data.get("mechanical", {}),
        design_limits=data.get("design_limits", {}),
        use_case_ratings=data.get("use_case_ratings", {}),
        agent_guidance=data.get("agent_guidance", []),
        bonding=data.get("bonding", {}),
    )


def list_material_profiles() -> list[MaterialProfile]:
    """Return all material profiles sorted by name."""
    kb = _get_kb()
    profiles = []
    for mid, data in sorted(kb.materials.items()):
        profiles.append(
            MaterialProfile(
                material_id=mid,
                display_name=data["display_name"],
                category=data["category"],
                thermal=data.get("thermal", {}),
                chemical=data.get("chemical", {}),
                mechanical=data.get("mechanical", {}),
                design_limits=data.get("design_limits", {}),
                use_case_ratings=data.get("use_case_ratings", {}),
                agent_guidance=data.get("agent_guidance", []),
                bonding=data.get("bonding", {}),
            )
        )
    return profiles


# ---------------------------------------------------------------------------
# Safety-floor recommendation fallback
# ---------------------------------------------------------------------------
#
# Public Kiln's materials.json carries only the safety floor: thermal
# limits, chemical safety (UV/food/outgassing), process-floor design
# limits (min wall thickness, overhangs), and brand identification +
# safety-relevant tunings.  The curated layer (mechanical
# properties, design_limits beyond process floor, use_case_ratings,
# agent_guidance paragraphs) ships in kiln-pro's overlay.
#
# When the overlay isn't loaded, the curated
# ``recommend_material_for_design`` path is missing the
# use_case_ratings + mechanical signals it relies on for fine-grained
# scoring.  This fallback uses ONLY the safety-floor fields plus a
# small set of public-domain DIY heuristics (UV, food, heat, load,
# outgassing, flexibility) to produce a useful recommendation —
# never as good as the engineering-overlay path, but honest about
# what it can and can't see.
#
# Inputs are public-domain trigger keywords + material datasheet
# common knowledge.  No curated data ships in this function.

# Heat-exposure trigger words (parsed from requirements_text alongside
# the matched ``heat_exposure`` requirement profile).  Public-domain.
_HEAT_KEYWORDS: frozenset[str] = frozenset({
    "heat", "hot", "oven", "dishwasher", "car", "summer",
    "engine", "thermal", "warm", "near heat",
})

# Default target service temperature when the user implies "hot
# environment" without a number.  60C = hot car interior in summer
# shade / typical near-electronics ambient — the threshold at which
# PLA starts visibly creeping.
_DEFAULT_HOT_TARGET_C = 60

# Outdoor / UV-exposure triggers.  Same vocabulary the
# ``outdoor_use`` requirement profile uses; we re-parse here so the
# fallback works even when the requirement profile didn't match.
_OUTDOOR_KEYWORDS: frozenset[str] = frozenset({
    "outdoor", "outside", "sun", "sunlight", "uv",
    "weather", "garden", "patio", "yard", "balcony",
})

# Food / drink contact triggers.
_FOOD_KEYWORDS: frozenset[str] = frozenset({
    "food", "drink", "kitchen", "cookie", "cup", "bowl",
    "utensil", "plate", "mug",
})

# Indoor-air / outgassing-sensitivity triggers.
_INDOOR_KEYWORDS: frozenset[str] = frozenset({
    "indoor", "office", "bedroom", "nursery", "classroom",
    "living room",
})

# Flexibility / elastomer triggers.
_FLEXIBLE_KEYWORDS: frozenset[str] = frozenset({
    "flexible", "rubber", "soft", "bendy", "squishy",
    "gasket", "seal", "grip",
})

# Sustained-load / hanging-weight triggers (creep concern, even when
# the load-bearing detector itself doesn't fire).
_SUSTAINED_LOAD_KEYWORDS: frozenset[str] = frozenset({
    "shelf", "bracket", "hold", "weight", "hang",
    "hanger", "mount", "support",
})

# Cosmetic / fine-detail triggers — bias toward PLA when neither
# load-bearing nor environmental constraints fire.
_COSMETIC_KEYWORDS: frozenset[str] = frozenset({
    "decorative", "cosmetic", "figurine", "ornament", "display",
    "art", "sculpture", "miniature", "gift", "show piece",
    "showpiece", "pretty", "beautiful",
})

# Material families recognized by id-prefix when the safety-floor
# ``category`` field is inconsistent (TPU 95A / TPU 85A are tagged
# "thermoplastic" in materials.json; PLA family includes pla, pla_plus,
# pla_matte, pla_tough, silk_pla, cf_pla).
_TPU_FAMILY_PREFIXES: tuple[str, ...] = ("tpu",)
_PLA_FAMILY_IDS: frozenset[str] = frozenset({
    "pla", "pla_plus", "pla_matte", "pla_tough", "silk_pla",
    "wood_pla", "cf_pla", "pvb",  # PVB behaves like PLA thermally
})


def _is_tpu_family(material_id: str) -> bool:
    """True if the material id belongs to the TPU/elastomer family."""
    return material_id.lower().startswith(_TPU_FAMILY_PREFIXES)


def _is_pla_family(material_id: str) -> bool:
    """True if the material id is a PLA variant (low-Tg, brittle, creeps)."""
    return material_id.lower() in _PLA_FAMILY_IDS


def _any_keyword(text: str, keywords: frozenset[str]) -> bool:
    """True if any keyword from the set appears as a whole word in text."""
    for kw in keywords:
        if " " in kw:
            if kw in text:
                return True
        elif re.search(rf"\b{re.escape(kw)}\b", text):
            return True
    return False


def _recommend_from_safety_floor(
    requirements_text: str,
    materials: dict[str, dict[str, Any]],
    *,
    printer_has_enclosure: bool = False,
    printer_has_direct_drive: bool = True,
    max_hotend_temp_c: int = 300,
    supported_materials: list[str] | None = None,
) -> MaterialRecommendation:
    """Recommend a material using ONLY safety-floor fields.

    Fallback for ``recommend_material_for_design`` when the kiln-pro
    engineering overlay isn't loaded.  Scores materials against
    thermal limits, chemical safety (UV/food/outgassing), and a
    small set of public-domain DIY heuristics (heat / outdoor /
    food / flexibility / sustained-load / cosmetic).

    Always runs the load-bearing detector and, when it trips,
    attaches the upgrade-nudge dict to the result's ``warnings`` list
    so callers without the overlay see the path to the engineering-
    grade analysis.

    :param requirements_text: Natural-language description.
    :param materials: Safety-floor materials dict (from
        ``_get_kb().materials`` — overlay assumed NOT loaded).
    :param printer_has_enclosure: Whether the printer has an enclosed
        build chamber (gates ABS / ASA / PC / Nylon).
    :param printer_has_direct_drive: Direct-drive extruder presence
        (required for TPU).
    :param max_hotend_temp_c: Hotend ceiling (gates PC / Nylon / CF).
    :param supported_materials: Optional allowlist from a printer
        profile; materials outside this set are heavily penalized.
    :returns: :class:`MaterialRecommendation`.  When the load-bearing
        detector trips, ``warnings`` includes a single human-readable
        upgrade-nudge string (the full structured nudge is in
        ``upgrade_recommendation`` if callers want to surface it
        programmatically).
    """
    # Lazy import — load_bearing_detector lives in the same package
    # but we keep the import here to make the cross-module dependency
    # obvious to readers.
    from kiln.load_bearing_detector import detect_load_bearing

    text = requirements_text.lower()
    matched = match_requirements(requirements_text)
    matched_ids = {cs.requirement_id for cs in matched}

    # ---- Detect what kind of constraint set is in play -----------------
    needs_heat = (
        "heat_exposure" in matched_ids
        or _any_keyword(text, _HEAT_KEYWORDS)
    )
    needs_outdoor = (
        "outdoor_use" in matched_ids
        or _any_keyword(text, _OUTDOOR_KEYWORDS)
    )
    needs_food = (
        "food_contact" in matched_ids
        or _any_keyword(text, _FOOD_KEYWORDS)
    )
    needs_low_outgassing = _any_keyword(text, _INDOOR_KEYWORDS)
    needs_flexible = (
        "flexibility_required" in matched_ids
        or _any_keyword(text, _FLEXIBLE_KEYWORDS)
    )
    is_cosmetic = (
        "aesthetic_decorative" in matched_ids
        or _any_keyword(text, _COSMETIC_KEYWORDS)
    )

    # Load-bearing detector — the trip authoritatively tells us this is
    # a sustained-load case.  We also OR in keyword sustained-load
    # signals so "shelf for my books" still flags creep without
    # tripping the heuristic.
    verdict = detect_load_bearing(requirements_text)
    needs_sustained_load = (
        verdict.is_load_bearing
        or _any_keyword(text, _SUSTAINED_LOAD_KEYWORDS)
    )
    needs_hot_load = needs_heat and needs_sustained_load

    # Heat target temperature — if the user mentioned a number (e.g.
    # "must survive 80C"), prefer that over the default 60C floor.
    target_temp_c = _DEFAULT_HOT_TARGET_C
    temp_match = re.search(r"(\d+)\s*°?\s*[cC]\b", requirements_text)
    if temp_match:
        with contextlib.suppress(ValueError):
            target_temp_c = max(target_temp_c, int(temp_match.group(1)))

    supported = (
        {m.lower() for m in supported_materials}
        if supported_materials else None
    )

    # ---- Score each material against the constraints -------------------
    scores: list[tuple[float, str, list[str], list[str]]] = []
    fired_filters: list[str] = []  # rationale building blocks

    for mid, mdata in materials.items():
        score = 50.0  # neutral baseline
        reasons: list[str] = []
        warnings: list[str] = []

        thermal = mdata.get("thermal", {})
        chemical = mdata.get("chemical", {})
        max_service_temp = thermal.get("max_service_temp_c", 0) or 0
        warping = thermal.get("warping_tendency", "low")
        uv_resistance = chemical.get("uv_resistance", "moderate")
        food_safe = chemical.get("food_safe", "no")
        outgassing = chemical.get("outgassing", "moderate")
        min_print_temp = (thermal.get("print_temp_range_c", [0, 0]) or [0, 0])[0]

        # --- Hard exclusions (skip the material entirely) -----------
        # Flexibility: only TPU family qualifies.  Kill everything else
        # so the recommendation can't accidentally hand back PETG for
        # a phone case.
        if needs_flexible and not _is_tpu_family(mid):
            continue

        # Outside flexibility, never propose TPU as the primary
        # recommendation — it's a niche material.
        if not needs_flexible and _is_tpu_family(mid):
            continue

        # Food contact: must be food_safe yes/conditional.
        if needs_food and food_safe not in ("yes", "conditional"):
            continue

        # --- Soft scoring per fired filter --------------------------
        if needs_heat:
            if max_service_temp >= target_temp_c + 10:
                score += 15
                reasons.append(
                    f"survives target temperature ({max_service_temp}C >= {target_temp_c}C + margin)."
                )
            elif max_service_temp >= target_temp_c:
                score += 5
                reasons.append(
                    f"marginal at target temperature ({max_service_temp}C ≈ {target_temp_c}C)."
                )
            else:
                score -= 30
                warnings.append(
                    f"deforms at target temperature "
                    f"(max service {max_service_temp}C < {target_temp_c}C)."
                )

        if needs_outdoor:
            if uv_resistance == "excellent":
                score += 25
                reasons.append("excellent UV resistance for sustained sun exposure.")
            elif uv_resistance == "moderate":
                score += 5
                reasons.append("moderate UV resistance — survives 1-2 outdoor seasons.")
            else:  # poor
                score -= 30
                warnings.append(
                    "poor UV resistance — becomes brittle in direct sunlight within weeks."
                )

        if needs_food:
            if food_safe == "yes":
                score += 20
                reasons.append("food-safe polymer family.")
            elif food_safe == "conditional":
                score += 5
                reasons.append("conditionally food-safe (single-use only — FDM layer lines harbor bacteria).")

        if needs_low_outgassing:
            if outgassing == "minimal":
                score += 8
                reasons.append("minimal outgassing — safe for indoor / occupied spaces.")
            elif outgassing == "low":
                score += 4
                reasons.append("low outgassing — acceptable for indoor spaces with ventilation.")
            else:  # moderate / high
                score -= 10
                warnings.append("moderate outgassing — print and cure with ventilation.")

        if needs_sustained_load:
            # Creep + thermal floor — PLA family creeps badly under
            # sustained load even at room temperature.  PETG / ASA /
            # ABS / Nylon / PC are all engineered options.
            if _is_pla_family(mid):
                score -= 35
                warnings.append(
                    "PLA family creeps under sustained load — not safe long-term for shelves, hooks, or hangers."
                )
            elif max_service_temp >= 65 and warping in ("low", "moderate", "none"):
                score += 12
                reasons.append("dimensional stability suitable for sustained-load applications.")

        if needs_hot_load and (_is_pla_family(mid) or mid.startswith("petg")):
            # Hot environment + load — eliminate PLA AND PETG (PETG
            # softens at 65C and creeps under load).
            score -= 25
            warnings.append(
                "softens under combined heat and sustained load — choose ABS / ASA / PC / Nylon."
            )

        if needs_flexible:
            # We've already filtered to TPU family — boost it strongly.
            score += 30
            reasons.append("TPU family — the only FDM material with genuine flexibility.")

        if (
            is_cosmetic
            and not (
                needs_heat
                or needs_outdoor
                or needs_food
                or needs_sustained_load
                or needs_flexible
            )
            and _is_pla_family(mid)
        ):
            # Pure cosmetic case — PLA wins on surface finish + ease
            # of print (no warping, low temp, smooth layer adhesion).
            score += 18
            reasons.append("PLA family — best surface finish for cosmetic prints.")

        # --- Printer capability filters -----------------------------
        if supported is not None and mid not in supported:
            warnings.append("not in the target printer's supported material set.")
            score -= 60

        if min_print_temp and min_print_temp > max_hotend_temp_c:
            warnings.append(
                f"requires {min_print_temp}C hotend — printer max is {max_hotend_temp_c}C."
            )
            score -= 50

        needs_enclosure = warping in ("high", "very_high")
        if needs_enclosure and not printer_has_enclosure:
            warnings.append("requires enclosure for reliable printing — printer does not have one.")
            score -= 30

        if _is_tpu_family(mid) and not printer_has_direct_drive:
            warnings.append("requires direct drive extruder — bowden setups cannot reliably print TPU.")
            score -= 40

        # Mild ease-of-print bias when nothing else is firing — PLA
        # is the easiest to print, so it should win ties for vague
        # prompts.
        if (
            not needs_heat and not needs_outdoor and not needs_food
            and not needs_sustained_load and not needs_flexible
            and warping == "low"
        ):
            score += 2

        scores.append((score, mid, reasons, warnings))

    scores.sort(key=lambda x: x[0], reverse=True)

    # Track which filters fired for the top-level rationale string.
    if needs_flexible:
        fired_filters.append("flexibility")
    if needs_food:
        fired_filters.append("food contact")
    if needs_outdoor:
        fired_filters.append("outdoor / UV exposure")
    if needs_heat:
        fired_filters.append("heat exposure")
    if needs_sustained_load:
        fired_filters.append("sustained load")
    if needs_low_outgassing:
        fired_filters.append("indoor air quality")
    if is_cosmetic and not fired_filters:
        fired_filters.append("cosmetic / display")

    if not scores:
        # Absolute fallback — e.g. flexibility required but no TPU in
        # the dict.  Never raise when the overlay isn't loaded;
        # return PLA with the full warning set so the user knows we
        # couldn't satisfy it.
        pla_data = materials.get("pla")
        if pla_data is None:
            raise RuntimeError("PLA material profile not found in safety-floor knowledge base")
        pla_profile = MaterialProfile(
            material_id="pla",
            display_name=pla_data["display_name"],
            category=pla_data["category"],
            thermal=pla_data.get("thermal", {}),
            chemical=pla_data.get("chemical", {}),
        )
        warnings = ["No material in the safety-floor catalogue satisfied your constraints — falling back to PLA."]
        if verdict.is_load_bearing:
            warnings.append(_format_upgrade_nudge(verdict))
        return MaterialRecommendation(
            material=pla_profile,
            score=50.0,
            reasons=[],
            warnings=warnings,
            design_limits_summary=pla_data.get("design_limits", {}),
            alternatives=[],
        )

    top_score, top_mid, top_reasons, top_warnings = scores[0]
    top_data = materials[top_mid]
    top_profile = MaterialProfile(
        material_id=top_mid,
        display_name=top_data["display_name"],
        category=top_data["category"],
        thermal=top_data.get("thermal", {}),
        chemical=top_data.get("chemical", {}),
    )

    # Compose the rationale prefix from the fired filters.
    if fired_filters:
        prefix = (
            f"Recommended for: {', '.join(fired_filters)}. "
            f"{top_profile.display_name} chosen — "
            f"{top_reasons[0] if top_reasons else 'best matches the safety-floor constraints.'}"
        )
    else:
        prefix = (
            f"{top_profile.display_name} recommended — no specific functional "
            f"constraints detected; defaulted to easiest-to-print material."
        )
    final_reasons = [prefix] + top_reasons[1:]

    # Always attach the upgrade nudge when the load detector tripped,
    # so callers without the overlay see the path to engineering-
    # grade analysis.
    final_warnings = list(top_warnings)
    if verdict.is_load_bearing:
        final_warnings.append(_format_upgrade_nudge(verdict))

    # Build alternatives from the next 2-3 ranked materials.
    alternatives: list[dict[str, Any]] = []
    for alt_score, alt_mid, alt_reasons, alt_warnings in scores[1:4]:
        alt_data = materials[alt_mid]
        alternatives.append(
            {
                "material_id": alt_mid,
                "display_name": alt_data["display_name"],
                "score": round(alt_score, 1),
                "reasons": alt_reasons,
                "warnings": alt_warnings,
            }
        )

    return MaterialRecommendation(
        material=top_profile,
        score=round(top_score, 1),
        reasons=final_reasons,
        warnings=final_warnings,
        design_limits_summary=top_profile.design_limits,
        alternatives=alternatives,
    )


def _format_upgrade_nudge(verdict: Any) -> str:
    """Render the load-bearing upgrade nudge as a single user-facing string.

    The structured ``upgrade_recommendation`` dict from the verdict is
    designed for programmatic consumers; this function flattens the
    key copy into one line that fits naturally inside
    ``MaterialRecommendation.warnings``.  Funnel-allowed per
    CLAUDE.md "Trademark + cross-repo discipline" — naming kiln-pro
    and linking kiln3d.com from public Kiln is a funnel, not a leak.
    """
    return (
        "This appears to be a load-bearing application; the safety-floor "
        "recommendation above doesn't account for cross-section shape, "
        "buckling, fatigue, creep, or FDM anisotropy. For real-engineering "
        "math (beam mechanics with FoS control, ISO 286 fits, fatigue + "
        "creep derating, calibrated tolerances), see "
        "https://kiln3d.com/pricing."
    )


def recommend_material_for_design(
    requirements_text: str,
    *,
    printer_has_enclosure: bool = False,
    printer_has_direct_drive: bool = True,
    max_hotend_temp_c: int = 300,
    supported_materials: list[str] | None = None,
) -> MaterialRecommendation:
    """Recommend the best material for a set of functional requirements.

    Matches requirement text against known functional requirement
    profiles, then scores each material based on constraint compatibility,
    use-case ratings, and printer capability filtering.

    :param requirements_text: Natural language description of what the
        object needs to do (e.g. ``"hold 5 kg of books outdoors"``).
    :param printer_has_enclosure: Whether the printer has an enclosed
        build chamber.
    :param printer_has_direct_drive: Whether the printer has a direct
        drive extruder (required for TPU).
    :param max_hotend_temp_c: Maximum hotend temperature the printer
        can reach.
    :param supported_materials: Optional allowlist from the target printer
        profile. Materials outside this set are heavily penalized.
    """
    kb = _get_kb()

    # No-overlay dispatch: when the kiln-pro engineering overlay
    # isn't loaded, ``mechanical`` and ``use_case_ratings`` are empty
    # across all materials.  The curated path below relies on those
    # fields for fine-grained scoring; without them it produces
    # degraded answers (e.g. recommends PLA for load-bearing because
    # it can't see structural_load_bearing="poor").  Route those
    # callers to the safety-floor fallback, which uses ONLY thermal
    # / chemical / process-floor design_limits + a small set of
    # public-domain DIY heuristics.  Probe a representative material
    # (PLA — always present) for the overlay marker.
    sample = kb.materials.get("pla") or next(iter(kb.materials.values()), None)
    if sample is None or not sample.get("mechanical"):
        return _recommend_from_safety_floor(
            requirements_text,
            kb.materials,
            printer_has_enclosure=printer_has_enclosure,
            printer_has_direct_drive=printer_has_direct_drive,
            max_hotend_temp_c=max_hotend_temp_c,
            supported_materials=supported_materials,
        )

    matched = match_requirements(requirements_text)
    supported = {m.lower() for m in supported_materials} if supported_materials else None

    # Collect material constraints from all matched requirements
    preferred: set[str] = set()
    required: set[str] = set()
    excluded: set[str] = set()

    for cs in matched:
        rules = cs.constraint_rules
        if "material_prefer" in rules:
            preferred.update(rules["material_prefer"])
        if "material_require" in rules:
            required.update(rules["material_require"])
        if "material_exclude" in rules:
            excluded.update(rules["material_exclude"])

    # Score each material
    scores: list[tuple[float, str, list[str], list[str]]] = []
    for mid, mdata in kb.materials.items():
        score = 50.0  # baseline
        reasons: list[str] = []
        warnings: list[str] = []

        # Hard exclusion
        if mid in excluded:
            continue

        if supported is not None and mid not in supported:
            warnings.append("Not in the target printer's supported material set.")
            score -= 60

        # Requirement match bonuses
        if mid in required:
            score += 30
            reasons.append("Required by functional constraints.")
        elif mid in preferred:
            score += 20
            reasons.append("Preferred for these requirements.")

        # Use-case rating scoring
        for cs in matched:
            req_id = cs.requirement_id
            rating_key = _requirement_to_rating_key(req_id)
            if rating_key and rating_key in mdata.get("use_case_ratings", {}):
                rating = mdata["use_case_ratings"][rating_key]
                rating_score = _RATING_ORDER.get(rating, 2)
                score += rating_score * 3
                if rating_score <= 1:
                    warnings.append(
                        f"{mdata['display_name']} rated '{rating}' for {cs.display_name}."
                    )

        # Printer capability filtering
        thermal = mdata.get("thermal", {})
        min_print_temp = thermal.get("print_temp_range_c", [0, 0])[0]
        needs_enclosure = thermal.get("warping_tendency", "low") in (
            "high",
            "very_high",
        )

        if min_print_temp > max_hotend_temp_c:
            warnings.append(
                f"Requires {min_print_temp}C hotend — printer max is {max_hotend_temp_c}C."
            )
            score -= 50  # heavy penalty but don't exclude

        if needs_enclosure and not printer_has_enclosure:
            warnings.append("Requires enclosure — printer does not have one.")
            score -= 30

        if mid == "tpu" and not printer_has_direct_drive:
            warnings.append("Requires direct drive extruder.")
            score -= 40

        # Ease of print bonus (mild — prefer easier materials all else equal)
        ease = mdata.get("mechanical", {}).get("layer_adhesion", "")
        if ease == "excellent":
            score += 3
        elif ease == "good":
            score += 2

        scores.append((score, mid, reasons, warnings))

    scores.sort(key=lambda x: x[0], reverse=True)

    if not scores:
        # Absolute fallback
        pla = get_material_profile("pla")
        if pla is None:
            raise RuntimeError("PLA material profile not found in knowledge base")
        return MaterialRecommendation(
            material=pla,
            score=50.0,
            reasons=["Fallback — all materials filtered out."],
            warnings=["PLA may not meet your requirements."],
            design_limits_summary=pla.design_limits,
            alternatives=[],
        )

    top_score, top_mid, top_reasons, top_warnings = scores[0]
    top_profile = get_material_profile(top_mid)
    if top_profile is None:
        raise RuntimeError(f"Material profile {top_mid!r} not found in knowledge base")

    # Build alternatives
    alternatives: list[dict[str, Any]] = []
    for alt_score, alt_mid, alt_reasons, alt_warnings in scores[1:4]:
        alt_profile = get_material_profile(alt_mid)
        if alt_profile:
            alternatives.append(
                {
                    "material_id": alt_mid,
                    "display_name": alt_profile.display_name,
                    "score": round(alt_score, 1),
                    "reasons": alt_reasons,
                    "warnings": alt_warnings,
                }
            )

    # Build brand recommendations for the top material
    recommended_brands: list[dict[str, Any]] = []
    try:
        brand_profiles = list_brand_filament_profiles(parent_material=top_mid)
        for bp in brand_profiles:
            brand_entry: dict[str, Any] = {
                "profile_id": bp.profile_id,
                "brand": bp.brand,
                "product_name": bp.product_name,
                "nozzle_temp_optimal_c": bp.nozzle_temp_optimal_c,
                "bed_temp_optimal_c": bp.bed_temp_optimal_c,
                "density_g_cm3": bp.density_g_cm3,
            }
            if bp.hardened_nozzle_required:
                brand_entry["hardened_nozzle_required"] = True
            if bp.enclosure_required:
                brand_entry["enclosure_required"] = True
            if bp.ams_compatible is not None:
                brand_entry["ams_compatible"] = bp.ams_compatible
            if bp.drying_temp_c:
                brand_entry["drying"] = f"{bp.drying_temp_c}°C / {bp.drying_time_hours}h"
            recommended_brands.append(brand_entry)
    except Exception:
        pass  # Brand profiles not available — continue without

    return MaterialRecommendation(
        material=top_profile,
        score=round(top_score, 1),
        reasons=top_reasons,
        warnings=top_warnings,
        design_limits_summary=top_profile.design_limits,
        alternatives=alternatives,
        recommended_brands=recommended_brands,
    )


# ---------------------------------------------------------------------------
# Public API — Brand filament profiles
# ---------------------------------------------------------------------------


def get_brand_filament_profile(profile_id: str) -> BrandFilamentProfile | None:
    """Get a specific brand filament profile by ID.

    :param profile_id: Profile key (e.g. ``"bambu_pla_basic"``, ``"prusament_petg"``).
    """
    kb = _get_kb()
    for _mid, mat_data in kb.materials.items():
        brand_profiles = mat_data.get("brand_profiles", {})
        if profile_id.lower() in brand_profiles:
            return _build_brand_profile(profile_id.lower(), _mid, brand_profiles[profile_id.lower()])
    return None


def list_brand_filament_profiles(
    *,
    brand: str | None = None,
    parent_material: str | None = None,
) -> list[BrandFilamentProfile]:
    """List brand filament profiles, optionally filtered by brand or parent material.

    :param brand: Filter by brand name (case-insensitive partial match).
    :param parent_material: Filter by parent material ID (e.g. ``"pla"``, ``"petg"``).
    """
    kb = _get_kb()
    results: list[BrandFilamentProfile] = []
    for mid, mat_data in sorted(kb.materials.items()):
        if parent_material and mid != parent_material.lower():
            continue
        for pid, pdata in sorted(mat_data.get("brand_profiles", {}).items()):
            if brand and brand.lower() not in pdata.get("brand", "").lower():
                continue
            results.append(_build_brand_profile(pid, mid, pdata))
    return results


def _build_brand_profile(
    profile_id: str,
    parent_material: str,
    data: dict[str, Any],
) -> BrandFilamentProfile:
    """Construct a BrandFilamentProfile from raw JSON data."""
    return BrandFilamentProfile(
        profile_id=profile_id,
        brand=data["brand"],
        product_name=data["product_name"],
        parent_material=parent_material,
        nozzle_temp_range_c=data["nozzle_temp_range_c"],
        nozzle_temp_optimal_c=data["nozzle_temp_optimal_c"],
        bed_temp_range_c=data["bed_temp_range_c"],
        bed_temp_optimal_c=data.get("bed_temp_optimal_c"),
        max_volumetric_speed_mm3s=data.get("max_volumetric_speed_mm3s"),
        max_print_speed_mms=data.get("max_print_speed_mms"),
        density_g_cm3=data.get("density_g_cm3"),
        drying_temp_c=data.get("drying_temp_c"),
        drying_time_hours=data.get("drying_time_hours"),
        enclosure_required=data.get("enclosure_required", False),
        hardened_nozzle_required=data.get("hardened_nozzle_required", False),
        ams_compatible=data.get("ams_compatible"),
        notes=data.get("notes"),
        source=data.get("source", ""),
    )


# ---------------------------------------------------------------------------
# Public API — Unified filament resolver
# ---------------------------------------------------------------------------


@dataclass
class ResolvedFilament:
    """Unified filament profile — brand-specific when available, parent fallback.

    This is the single source of truth for filament properties anywhere in
    the pipeline.  Consumers (estimator, preflight, slicer, recommender)
    should call :func:`resolve_filament` instead of looking up materials
    or brand profiles directly.
    """

    # Identity
    material_id: str  # parent material key (e.g. "pla", "tpu")
    brand_profile_id: str | None  # brand key if resolved (e.g. "bambu_pla_basic")
    display_name: str  # "Bambu Lab PLA Basic" or "PLA (generic)"
    is_brand_specific: bool

    # Physical properties (brand overrides parent when available)
    density_g_per_cm3: float
    cost_per_kg_usd: float
    filament_diameter_mm: float

    # Printing parameters (brand-specific or parent defaults)
    nozzle_temp_optimal_c: int
    nozzle_temp_range_c: list[int]
    bed_temp_optimal_c: int
    bed_temp_range_c: list[int]
    max_volumetric_speed_mm3s: float | None
    max_print_speed_mms: int | None

    # Drying
    drying_temp_c: int | None
    drying_time_hours: int | None

    # Printer requirements
    enclosure_required: bool
    hardened_nozzle_required: bool
    ams_compatible: bool | None

    # Warnings for preflight
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_filament(
    material_or_brand: str,
    *,
    printer_id: str | None = None,
) -> ResolvedFilament:
    """Resolve a material name or brand profile ID to a unified filament profile.

    Accepts either:
    - A parent material ID (e.g. ``"PLA"``, ``"tpu"``, ``"PETG"``) → returns
      generic parent properties from ``cost_estimator.BUILTIN_MATERIALS``.
    - A brand profile ID (e.g. ``"bambu_pla_basic"``, ``"prusament_tpu_95a"``)
      → returns brand-specific properties with higher accuracy.

    When ``printer_id`` is provided, generates compatibility warnings
    (enclosure, nozzle, AMS) for the resolved filament.

    :param material_or_brand: Material ID or brand profile ID.
    :param printer_id: Optional printer model for compatibility warnings.
    :returns: :class:`ResolvedFilament` with unified properties.
    """
    from kiln.cost_estimator import BUILTIN_MATERIALS

    key = material_or_brand.strip().lower()
    warnings: list[str] = []

    # --- Try brand profile first ---
    brand = get_brand_filament_profile(key)
    if brand is not None:
        # Get parent material cost as fallback (brand profiles don't store cost)
        parent_mat = BUILTIN_MATERIALS.get(brand.parent_material.upper())
        cost = parent_mat.cost_per_kg_usd if parent_mat else 25.0
        diameter = parent_mat.filament_diameter_mm if parent_mat else 1.75

        # Build printer compatibility warnings
        if printer_id:
            warnings = _check_filament_printer_compat(brand, printer_id)

        return ResolvedFilament(
            material_id=brand.parent_material,
            brand_profile_id=brand.profile_id,
            display_name=f"{brand.brand} {brand.product_name}",
            is_brand_specific=True,
            density_g_per_cm3=brand.density_g_cm3 or (parent_mat.density_g_per_cm3 if parent_mat else 1.24),
            cost_per_kg_usd=cost,
            filament_diameter_mm=diameter,
            nozzle_temp_optimal_c=brand.nozzle_temp_optimal_c,
            nozzle_temp_range_c=brand.nozzle_temp_range_c,
            bed_temp_optimal_c=brand.bed_temp_optimal_c or 60,
            bed_temp_range_c=brand.bed_temp_range_c,
            max_volumetric_speed_mm3s=brand.max_volumetric_speed_mm3s,
            max_print_speed_mms=brand.max_print_speed_mms,
            drying_temp_c=brand.drying_temp_c,
            drying_time_hours=brand.drying_time_hours,
            enclosure_required=brand.enclosure_required,
            hardened_nozzle_required=brand.hardened_nozzle_required,
            ams_compatible=brand.ams_compatible,
            warnings=warnings,
        )

    # --- Fall back to parent material ---
    mat_upper = key.upper().replace("-", "_")
    parent = BUILTIN_MATERIALS.get(mat_upper)
    if parent is None:
        # Try common aliases (PLA_PLUS → PLA+, CF_PLA → CF-PLA, etc.)
        _ALIASES = {
            "PLA_PLUS": "PLA+",
            "CF_PLA": "CF-PLA",
            "SILK_PLA": "SILK-PLA",
        }
        parent = BUILTIN_MATERIALS.get(_ALIASES.get(mat_upper, mat_upper))

    if parent is None:
        parent = BUILTIN_MATERIALS["PLA"]

    return ResolvedFilament(
        material_id=key,
        brand_profile_id=None,
        display_name=f"{parent.name} (generic)",
        is_brand_specific=False,
        density_g_per_cm3=parent.density_g_per_cm3,
        cost_per_kg_usd=parent.cost_per_kg_usd,
        filament_diameter_mm=parent.filament_diameter_mm,
        nozzle_temp_optimal_c=int(parent.tool_temp_default),
        nozzle_temp_range_c=[int(parent.tool_temp_default) - 20, int(parent.tool_temp_default) + 20],
        bed_temp_optimal_c=int(parent.bed_temp_default),
        bed_temp_range_c=[int(parent.bed_temp_default) - 10, int(parent.bed_temp_default) + 10],
        max_volumetric_speed_mm3s=None,
        max_print_speed_mms=None,
        drying_temp_c=None,
        drying_time_hours=None,
        enclosure_required=False,
        hardened_nozzle_required=False,
        ams_compatible=None,
        warnings=["Generic material profile — pass a brand ID (e.g. 'bambu_pla_basic') for exact specs."],
    )


def _check_filament_printer_compat(
    brand: BrandFilamentProfile,
    printer_id: str,
) -> list[str]:
    """Check brand filament compatibility with a specific printer.

    Returns a list of human-readable warnings (empty = all clear).
    """
    warnings: list[str] = []

    try:
        from kiln.printer_intelligence import get_printer_intel

        intel = get_printer_intel(printer_id)
    except Exception:
        return warnings

    if intel is None:
        return warnings

    # Enclosure check
    if brand.enclosure_required:
        has_enclosure = getattr(intel, "has_enclosure", False)
        if not has_enclosure:
            warnings.append(
                f"{brand.brand} {brand.product_name} requires an enclosed printer. "
                f"'{printer_id}' may not have an enclosure."
            )

    # Hardened nozzle check
    if brand.hardened_nozzle_required:
        warnings.append(
            f"{brand.brand} {brand.product_name} requires a hardened steel nozzle "
            f"(HRC >= 40). Printing with brass will destroy the nozzle rapidly."
        )

    # AMS compatibility check (Bambu printers only)
    if brand.ams_compatible is False and "bambu" in printer_id.lower():
        warnings.append(
            f"{brand.brand} {brand.product_name} is NOT AMS compatible. "
            f"Feed directly, not through AMS."
        )

    # Temperature check
    max_hotend = getattr(intel, "max_hotend_temp", None)
    if max_hotend and brand.nozzle_temp_optimal_c > max_hotend:
        warnings.append(
            f"{brand.brand} {brand.product_name} needs {brand.nozzle_temp_optimal_c}°C "
            f"but '{printer_id}' max hotend is {max_hotend}°C."
        )

    return warnings


# ---------------------------------------------------------------------------
# Public API — Structural and environmental reasoning
# ---------------------------------------------------------------------------


def estimate_load_capacity(
    material_id: str,
    cross_section_mm2: float,
    cantilever_length_mm: float,
    *,
    load_across_layers: bool = True,
) -> LoadEstimate | None:
    """Estimate max safe load for a given cantilever geometry."""
    kb = _get_kb()
    material_key = material_id.lower()
    material_data = kb.load_tables.get(material_key)
    if material_data is None:
        return None

    reasoning: list[str] = []

    if cross_section_mm2 <= 0:
        reasoning.append("Cross-section must be positive. Returning zero safe load.")
        return LoadEstimate(
            material=material_key,
            max_load_n=0.0,
            safety_factor=3.0,
            derating_applied=0.0,
            reasoning=reasoning,
        )

    length_tables = sorted(
        material_data.get("cross_section_vs_load", []),
        key=lambda row: float(row.get("cantilever_length_mm", 0.0)),
    )
    if not length_tables:
        return None

    lower_row, upper_row = _select_length_rows(length_tables, cantilever_length_mm)
    lower_length = float(lower_row.get("cantilever_length_mm", 0.0))
    upper_length = float(upper_row.get("cantilever_length_mm", 0.0))

    lower_load = _interpolate_cross_section_load(
        lower_row.get("entries", []),
        cross_section_mm2,
    )
    upper_load = _interpolate_cross_section_load(
        upper_row.get("entries", []),
        cross_section_mm2,
    )

    if lower_length == upper_length:
        base_load = lower_load
        reasoning.append(f"Used lookup row at {lower_length:.0f} mm cantilever.")
    else:
        ratio = (cantilever_length_mm - lower_length) / (upper_length - lower_length)
        base_load = lower_load + (upper_load - lower_load) * ratio
        reasoning.append(
            f"Interpolated between {lower_length:.0f} mm and "
            f"{upper_length:.0f} mm cantilever tables."
        )

    if cantilever_length_mm < lower_length:
        reasoning.append(
            f"Requested cantilever ({cantilever_length_mm:.1f} mm) is shorter than table "
            f"minimum ({lower_length:.0f} mm); estimate uses conservative minimum row."
        )
    elif cantilever_length_mm > upper_length:
        reasoning.append(
            f"Requested cantilever ({cantilever_length_mm:.1f} mm) exceeds table maximum "
            f"({upper_length:.0f} mm); estimate uses conservative maximum row."
        )

    orientation_derating = material_data.get("layer_orientation_derating", {})
    orientation_key = "across_layers" if load_across_layers else "along_layers"
    derating = float(orientation_derating.get(orientation_key, 1.0))
    max_load_n = max(0.0, base_load * derating)

    tensile_capacity = material_data.get("tensile_capacity_n_per_mm2")
    if tensile_capacity is not None:
        reasoning.append(
            f"Base material tension capacity: {tensile_capacity} N/mm^2 "
            "(already includes safety factor)."
        )
    reasoning.append(
        f"Applied layer-orientation derating ({orientation_key}) = {derating:.2f}."
    )
    reasoning.extend(material_data.get("notes", []))

    return LoadEstimate(
        material=material_key,
        max_load_n=round(max_load_n, 2),
        safety_factor=3.0,
        derating_applied=derating,
        reasoning=reasoning,
    )


def check_environment_compatibility(
    material_id: str,
    environment: str,
) -> EnvironmentReport | None:
    """Check if a material survives in a described environment."""
    kb = _get_kb()
    material_key = material_id.lower()
    material_data = kb.environment.get(material_key)
    if material_data is None:
        return None

    env_text = environment.lower()
    per_category: dict[str, Any] = {}
    warnings: list[str] = []
    has_fail = False
    has_warning = False

    # --- Simple keyword-to-rating factors (same pattern each) ---
    _simple_factors: list[tuple[str, tuple[str, ...]]] = [
        ("uv_resistance", ("uv", "sun", "sunlight", "outdoor", "weather")),
        (
            "moisture",
            (
                "water", "wet", "moisture", "humidity", "rain",
                "immersion", "submerged", "wash", "dishwasher", "marine",
            ),
        ),
        ("vibration_fatigue", ("vibration", "vibrate", "fatigue", "cyclic", "oscillation")),
        ("abrasion_resistance", ("abrasion", "wear", "friction", "rubbing", "sliding", "scratch")),
    ]
    for data_key, keywords in _simple_factors:
        if any(k in env_text for k in keywords):
            section = material_data.get(data_key, {})
            rating = str(section.get("rating", "conditional")).lower()
            per_category[data_key] = rating
            has_fail, has_warning = _accumulate_rating_outcome(
                category=data_key,
                rating=rating,
                has_fail=has_fail,
                has_warning=has_warning,
                warnings=warnings,
            )

    # Moisture immersion sub-check (only when moisture was already matched)
    if "moisture" in per_category:
        moisture_data = material_data.get("moisture", {})
        immersion_keywords = ("immersion", "submerged", "underwater", "continuous water")
        if any(k in env_text for k in immersion_keywords) and not moisture_data.get(
            "immersion_safe", False
        ):
            has_fail = True
            warnings.append("Material is not rated immersion-safe for this environment.")

    # --- Temperature (special: numeric extraction + heat/cold sub-branches) ---
    heat_keywords = (
        "heat", "hot", "temperature", "engine", "dashboard", "thermal", "summer", "oven",
    )
    cold_keywords = ("cold", "freez", "winter", "subzero", "ice")
    detected_temps = _extract_temperatures_c(environment)
    if detected_temps or any(k in env_text for k in heat_keywords + cold_keywords):
        temp = material_data.get("temperature_range", {})
        min_service = float(temp.get("min_service_c", -273.0))
        max_service = float(temp.get("max_service_c", 1000.0))
        per_category["temperature_range"] = {
            "min_service_c": min_service,
            "max_service_c": max_service,
        }
        if detected_temps:
            out_of_range = [t for t in detected_temps if t < min_service or t > max_service]
            if out_of_range:
                has_fail = True
                warnings.append(
                    "Temperature demand outside service range: "
                    f"{out_of_range} C not within [{min_service:.0f}, {max_service:.0f}] C."
                )
        else:
            if any(k in env_text for k in heat_keywords) and max_service < 70:
                has_warning = True
                warnings.append(
                    f"Heat-exposed use is conditional; max service temperature is {max_service:.0f}C."
                )
            if any(k in env_text for k in cold_keywords) and min_service > -15:
                has_warning = True
                warnings.append(
                    f"Cold-exposed use is conditional; minimum service temperature is {min_service:.0f}C."
                )

    # --- Chemicals (sub-map with per-chemical-class keywords) ---
    chemical_map: dict[str, tuple[str, ...]] = {
        "household_cleaners": ("cleaner", "detergent", "bleach", "soap", "ammonia"),
        "oils_greases": ("oil", "grease", "lubricant"),
        "fuels": ("fuel", "gasoline", "diesel", "petrol", "kerosene"),
        "solvents": ("solvent", "acetone", "ipa", "isopropyl", "thinner", "mek"),
        "acids": ("acid", "vinegar", "citric"),
    }
    chemical_data = material_data.get("chemicals", {})
    for chemical_key, keywords in chemical_map.items():
        if any(k in env_text for k in keywords):
            rating = str(chemical_data.get(chemical_key, "conditional")).lower()
            per_category[f"chemicals_{chemical_key}"] = rating
            has_fail, has_warning = _accumulate_rating_outcome(
                category=f"chemicals_{chemical_key}",
                rating=rating,
                has_fail=has_fail,
                has_warning=has_warning,
                warnings=warnings,
            )

    if not per_category:
        # Keep output useful when environment text is vague.
        per_category = {
            "uv_resistance": material_data.get("uv_resistance", {}).get(
                "rating",
                "conditional",
            ),
            "moisture": material_data.get("moisture", {}).get("rating", "conditional"),
            "vibration_fatigue": material_data.get("vibration_fatigue", {}).get(
                "rating",
                "conditional",
            ),
            "abrasion_resistance": material_data.get("abrasion_resistance", {}).get(
                "rating",
                "conditional",
            ),
        }
        has_warning = True
        warnings.append(
            "No specific environment factors detected; returned baseline survivability ratings."
        )

    if has_fail:
        verdict = "not_recommended"
    elif has_warning:
        verdict = "conditional"
    else:
        verdict = "recommended"

    return EnvironmentReport(
        material=material_key,
        environment=environment,
        per_category_ratings=per_category,
        warnings=warnings,
        overall_verdict=verdict,
        upgrade_hint="" if _engineering_overlay_loaded() else _UPGRADE_HINT_ENVIRONMENT,
    )


# Map a printer_id prefix to its manufacturer for intel-derived profiles.
# Order matters: more specific prefixes first.
_MANUFACTURER_PREFIXES: list[tuple[str, str]] = [
    ("bambu_", "Bambu Lab"),
    ("elegoo_", "Elegoo"),
    ("prusa_", "Prusa Research"),
    ("voron_", "Voron"),
    ("anker", "AnkerMake"),
    ("artillery", "Artillery"),
    ("flashforge", "FlashForge"),
    ("qidi", "QIDI"),
    ("ratrig", "RatRig"),
    ("sovol", "Sovol"),
    ("creality_hi", "Creality"),
    ("cr10", "Creality"),
    ("ender", "Creality"),
    ("k1", "Creality"),
    ("k2", "Creality"),
    ("sparkx", "SparkX"),
    ("klipper_generic", "Generic"),
]


def _derive_manufacturer(printer_id: str, display_name: str) -> str:
    pid = printer_id.lower()
    for prefix, mfr in _MANUFACTURER_PREFIXES:
        if pid.startswith(prefix):
            return mfr
    return display_name.split()[0] if display_name.strip() else "Unknown"


# Fallback estimates for printers without a curated printer_profiles.json
# record. Input-shaping machines (CoreXY / modern fast bedslingers) get a
# tighter tolerance and higher default speed than open-loop bedslingers.
# Curated records override all of these where a hand-tuned value exists.
_DERIVED_SPEED_WITH_IS_MM_S = 250
_DERIVED_SPEED_DEFAULT_MM_S = 150
_DERIVED_TOLERANCE_WITH_IS_MM = 0.15
_DERIVED_TOLERANCE_DEFAULT_MM = 0.2
_DERIVED_LAYER_HEIGHTS_MM = [0.08, 0.12, 0.16, 0.2, 0.28]  # standard 0.4mm-nozzle ladder


def _design_profile_from_intel(
    printer_id: str, raw: dict[str, Any]
) -> PrinterDesignProfile:
    """Synthesize a design-capability profile from a printer_intelligence
    entry, for printers without a hand-curated ``printer_profiles.json``
    record.  Fields the spec sheet doesn't carry (tolerance, layer-height
    ladder) use standard 0.4mm-nozzle FDM defaults; max speed is read from
    the curated speed table when present, else estimated from input-shaping.
    """
    from kiln.printer_intelligence import _SPEED_CAPABILITIES

    bv = raw.get("build_volume_mm") or [0, 0, 0]
    if isinstance(bv, dict):
        build = {"x": int(bv.get("x", 0)), "y": int(bv.get("y", 0)), "z": int(bv.get("z", 0))}
    else:
        build = {"x": int(bv[0]), "y": int(bv[1]), "z": int(bv[2])} if len(bv) >= 3 else {"x": 0, "y": 0, "z": 0}

    has_is = bool(raw.get("has_input_shaping"))
    caps = _SPEED_CAPABILITIES.get(printer_id)
    max_speed = (
        int(caps["max_speed"]) if caps and caps.get("max_speed")
        else (_DERIVED_SPEED_WITH_IS_MM_S if has_is else _DERIVED_SPEED_DEFAULT_MM_S)
    )
    materials = sorted({str(m).lower() for m in (raw.get("materials") or {})})

    return PrinterDesignProfile(
        printer_id=printer_id,
        display_name=raw.get("display_name", printer_id),
        manufacturer=_derive_manufacturer(printer_id, raw.get("display_name", "")),
        build_volume_mm=build,
        max_hotend_temp_c=int(raw.get("max_hotend_temp", 0)),
        max_bed_temp_c=int(raw.get("max_bed_temp", 0)),
        has_enclosure=bool(raw.get("has_enclosure")),
        has_direct_drive=raw.get("extruder_type") == "direct_drive",
        supported_materials=materials,
        typical_tolerance_mm=_DERIVED_TOLERANCE_WITH_IS_MM if has_is else _DERIVED_TOLERANCE_DEFAULT_MM,
        max_print_speed_mm_s=max_speed,
        default_layer_heights_mm=list(_DERIVED_LAYER_HEIGHTS_MM),
        agent_notes=[],
    )


def _all_design_profiles() -> dict[str, PrinterDesignProfile]:
    """Build the full design-capability map: an entry for every printer
    Kiln supports.  ``printer_intelligence.json`` (the canonical supported-
    printer set) is the base; a curated ``printer_profiles.json`` record,
    when present, overrides the intel-derived one (hand-tuned specs + the
    kiln-pro ``agent_notes`` overlay).  This keeps the design tools in sync
    with the rest of the system instead of stranding ~80% of supported
    printers behind an "Unknown printer" error.
    """
    from kiln import printer_intelligence as _pi

    _pi._load_raw()
    profiles: dict[str, PrinterDesignProfile] = {}
    for pid, raw in _pi._raw_cache.items():
        if pid == "default":  # sentinel fallback, not a real model
            continue
        profiles[pid] = _design_profile_from_intel(pid, raw)

    # Curated records win on their own id (richer + carry agent_notes).
    for cid, data in _get_kb().printers.items():
        profiles[cid] = PrinterDesignProfile(
            printer_id=cid,
            display_name=data["display_name"],
            manufacturer=data["manufacturer"],
            build_volume_mm=data["build_volume_mm"],
            max_hotend_temp_c=data["max_hotend_temp_c"],
            max_bed_temp_c=data["max_bed_temp_c"],
            has_enclosure=data["has_enclosure"],
            has_direct_drive=data["has_direct_drive"],
            supported_materials=data["supported_materials"],
            typical_tolerance_mm=data["typical_tolerance_mm"],
            max_print_speed_mm_s=data["max_print_speed_mm_s"],
            default_layer_heights_mm=data["default_layer_heights_mm"],
            agent_notes=list(data.get("agent_notes", [])),
        )
    return profiles


def get_printer_design_profile(printer_id: str) -> PrinterDesignProfile | None:
    """Get design capabilities for a specific printer.

    Covers every supported printer: a curated ``printer_profiles.json``
    record when one exists, otherwise a profile derived from
    ``printer_intelligence.json``.  Returns ``None`` only for genuinely
    unknown printer ids.
    """
    return _all_design_profiles().get(printer_id.lower())


def list_printer_profiles() -> list[PrinterDesignProfile]:
    """List design profiles for every supported printer (curated where
    available, otherwise derived from printer_intelligence)."""
    return [profile for _, profile in sorted(_all_design_profiles().items())]


# ---------------------------------------------------------------------------
# Public API — Design Templates
# ---------------------------------------------------------------------------


def get_design_template(template_id: str) -> DesignTemplate | None:
    """Get a design template by ID.

    :param template_id: Template key (e.g. ``"snap_fit_cantilever"``).
    """
    kb = _get_kb()
    data = kb.templates.get(template_id)
    if data is None:
        return None

    return DesignTemplate(
        template_id=template_id,
        display_name=data["display_name"],
        description=data["description"],
        use_cases=data["use_cases"],
        material_compatibility=data["material_compatibility"],
        print_orientation=data["print_orientation"],
        design_rules=data.get("design_rules", {}),
        print_orientation_reason=data.get("print_orientation_reason", ""),
        agent_guidance=data.get("agent_guidance", []),
    )


def list_design_templates() -> list[DesignTemplate]:
    """Return all design templates sorted by name."""
    kb = _get_kb()
    templates = []
    for tid, data in sorted(kb.templates.items()):
        templates.append(
            DesignTemplate(
                template_id=tid,
                display_name=data["display_name"],
                description=data["description"],
                use_cases=data["use_cases"],
                material_compatibility=data["material_compatibility"],
                print_orientation=data["print_orientation"],
                design_rules=data.get("design_rules", {}),
                print_orientation_reason=data.get("print_orientation_reason", ""),
                agent_guidance=data.get("agent_guidance", []),
            )
        )
    return templates


def find_templates_for_use_case(use_case: str) -> list[DesignTemplate]:
    """Find design templates that match a use case.

    :param use_case: Use case keyword (e.g. ``"enclosures"``, ``"gears"``).
    """
    kb = _get_kb()
    lower = use_case.lower()
    results = []

    for tid, data in kb.templates.items():
        cases = [c.lower() for c in data.get("use_cases", [])]
        if any(lower in c or c in lower for c in cases):
            template = get_design_template(tid)
            if template:
                results.append(template)

    return results


# ---------------------------------------------------------------------------
# Public API — Functional Requirements
# ---------------------------------------------------------------------------


def match_requirements(text: str) -> list[DesignConstraintSet]:
    """Match natural language text to functional requirement profiles.

    Scans the input text for trigger words/phrases from each known
    requirement profile and returns all matches with their constraint
    rules.

    :param text: Natural language description of what the object needs
        to do (e.g. ``"outdoor shelf bracket that holds 10 lbs"``).
    """
    kb = _get_kb()
    lower = text.lower()
    results = []

    for req_id, data in kb.requirements.items():
        # A profile may suppress its own homographs so a match stays credible
        # (against_skin: a napkin ring / band saw / watch stand is not worn).
        exclusions = data.get("trigger_exclusions", [])
        if any(str(x).lower() in lower for x in exclusions):
            continue
        triggers = data.get("triggers", [])
        matched_triggers = [t for t in triggers if t.lower() in lower]
        if matched_triggers:
            results.append(
                DesignConstraintSet(
                    requirement_id=req_id,
                    display_name=data["display_name"],
                    matched_triggers=matched_triggers,
                    constraint_rules=data.get("constraint_rules", {}),
                    agent_guidance=data.get("agent_guidance", []),
                    caution=data.get("caution", ""),
                )
            )

    return results


def get_design_constraints(
    requirements_text: str,
    *,
    material: str | None = None,
    printer_model: str | None = None,
) -> DesignBrief:
    """Decompose functional requirements into a complete design-requirements analysis.

    Internal entry point that ``analyze_design_requirements`` (the MCP
    tool) and ``design_session`` (the user-facing saved-goal flow) both
    call into.  Given a natural language description of what the user
    needs, returns a :class:`DesignBrief` with material recommendation,
    applicable patterns, combined constraints, and guidance notes.

    :param requirements_text: What the object needs to do (e.g.
        ``"phone mount for car dashboard, holds phone securely, survives
        summer heat"``).
    :param material: Optional material override (skip recommendation).
    :param printer_model: Optional printer model for capability lookup.
    """
    # 1. Match functional requirements
    constraints = match_requirements(requirements_text)
    printer_profile = (
        get_printer_design_profile(printer_model)
        if printer_model
        else None
    )

    # 2. Recommend material (unless overridden)
    recommendation: MaterialRecommendation | None = None
    if material:
        profile = get_material_profile(material)
        if profile:
            warnings: list[str] = []
            if printer_profile:
                material_key = profile.material_id.lower()
                supported = {m.lower() for m in printer_profile.supported_materials}
                min_print_temp = profile.thermal.get("print_temp_range_c", [0, 0])[0]
                warping = profile.thermal.get("warping_tendency", "").lower()

                if material_key not in supported:
                    warnings.append(
                        f"{printer_profile.display_name} is not profiled for {profile.display_name}."
                    )
                if min_print_temp > printer_profile.max_hotend_temp_c:
                    warnings.append(
                        f"{profile.display_name} needs {min_print_temp}C hotend temperature, "
                        f"but {printer_profile.display_name} is capped at "
                        f"{printer_profile.max_hotend_temp_c}C."
                    )
                if material_key == "tpu" and not printer_profile.has_direct_drive:
                    warnings.append(
                        f"{printer_profile.display_name} does not have direct drive, "
                        "so TPU reliability will be poor."
                    )
                if warping in {"high", "very_high"} and not printer_profile.has_enclosure:
                    warnings.append(
                        f"{profile.display_name} benefits from an enclosure, "
                        f"but {printer_profile.display_name} is open-frame."
                    )

            recommendation = MaterialRecommendation(
                material=profile,
                score=100.0,
                reasons=["User-specified material."],
                warnings=warnings,
                design_limits_summary=profile.design_limits,
                alternatives=[],
            )
    else:
        if printer_profile:
            recommendation = recommend_material_for_design(
                requirements_text,
                printer_has_enclosure=printer_profile.has_enclosure,
                printer_has_direct_drive=printer_profile.has_direct_drive,
                max_hotend_temp_c=printer_profile.max_hotend_temp_c,
                supported_materials=printer_profile.supported_materials,
            )
        else:
            recommendation = recommend_material_for_design(requirements_text)

    # 3. Find applicable patterns
    patterns = _find_templates_from_text(requirements_text)

    # 4. Combine all guidance
    combined_guidance: list[str] = []
    combined_rules: dict[str, Any] = {}

    for cs in constraints:
        combined_guidance.extend(cs.agent_guidance)
        # Merge constraint rules (later constraints override earlier)
        for key, value in cs.constraint_rules.items():
            if key.startswith("min_") and key in combined_rules:
                # For minimums, take the larger value
                if isinstance(value, (int, float)):
                    combined_rules[key] = max(combined_rules[key], value)
                else:
                    combined_rules[key] = value
            elif key.startswith("max_") and key in combined_rules:
                # For maximums, take the smaller value
                if isinstance(value, (int, float)):
                    combined_rules[key] = min(combined_rules[key], value)
                else:
                    combined_rules[key] = value
            else:
                combined_rules[key] = value

    # Add material-specific guidance
    if recommendation and recommendation.material:
        combined_guidance.extend(recommendation.material.agent_guidance)
        # Merge material design limits
        for key, value in recommendation.material.design_limits.items():
            limit_key = f"material_{key}"
            combined_rules[limit_key] = value

    if printer_profile:
        combined_guidance.extend(printer_profile.agent_notes)
        combined_rules["printer_build_volume_mm"] = dict(printer_profile.build_volume_mm)
        combined_rules["printer_typical_tolerance_mm"] = printer_profile.typical_tolerance_mm
        combined_rules["printer_default_layer_heights_mm"] = list(
            printer_profile.default_layer_heights_mm
        )
        combined_rules["printer_supported_materials"] = list(
            printer_profile.supported_materials
        )

    # Add pattern guidance
    for pattern in patterns:
        combined_guidance.extend(pattern.agent_guidance)

    # Preserve order while removing duplicates.
    combined_guidance = list(dict.fromkeys(combined_guidance))

    return DesignBrief(
        functional_constraints=constraints,
        recommended_material=recommendation,
        applicable_patterns=patterns,
        combined_guidance=combined_guidance,
        combined_rules=combined_rules,
    )


# ---------------------------------------------------------------------------
# Public API — Troubleshooting
# ---------------------------------------------------------------------------


def troubleshoot_print_issue(
    material_id: str,
    symptom: str | None = None,
) -> TroubleshootingResult | None:
    """Search for print issues by material and optional symptom keywords.

    Returns matching issues sorted by severity (major first), with
    fixes sorted by priority.  When no symptom is given, returns all
    known issues for the material.

    :param material_id: Material key (e.g. ``"petg"``, ``"abs"``).
    :param symptom: Optional symptom keywords (e.g. ``"stringing"``,
        ``"warping"``, ``"poor adhesion"``).
    """
    kb = _get_kb()
    material_key = material_id.lower()
    data = kb.troubleshooting.get(material_key)
    if data is None:
        return None

    issues = data.get("common_issues", [])

    if symptom:
        symptom_lower = symptom.lower()
        symptom_words = symptom_lower.split()
        matched = []
        for issue in issues:
            issue_text = (
                issue.get("symptom", "").lower()
                + " "
                + issue.get("root_cause", "").lower()
            )
            if any(w in issue_text for w in symptom_words):
                matched.append(issue)
        issues = matched

    # Sort by severity: major > moderate > minor
    severity_order = {"major": 0, "moderate": 1, "minor": 2}
    issues = sorted(
        issues,
        key=lambda i: severity_order.get(i.get("severity", "minor"), 2),
    )

    return TroubleshootingResult(
        material=material_key,
        matched_issues=issues,
        storage_requirements=data.get("storage_requirements"),
        break_in_tips=data.get("break_in_tips", []),
        upgrade_hint="" if _engineering_overlay_loaded() else _UPGRADE_HINT_TROUBLESHOOTING,
    )


def list_troubleshooting_materials() -> list[str]:
    """Return all material IDs that have troubleshooting data."""
    kb = _get_kb()
    return sorted(kb.troubleshooting.keys())


# ---------------------------------------------------------------------------
# Public API — Printer-Material Compatibility
# ---------------------------------------------------------------------------


def check_printer_material_compatibility(
    printer_id: str,
    material_id: str | None = None,
) -> PrinterCompatibilityReport | None:
    """Check if a printer can handle a material (or list all compatible materials).

    :param printer_id: Printer key (e.g. ``"ender3"``, ``"bambu_x1c"``).
    :param material_id: Optional material to check specifically. If omitted,
        returns compatibility for all known materials on this printer.
    """
    kb = _get_kb()
    printer_key = printer_id.lower()

    # Try exact match, then prefix match, then 'default' fallback
    compat_data = kb.printer_compatibility.get(printer_key)
    if compat_data is None:
        for key in kb.printer_compatibility:
            if key.startswith(printer_key) or printer_key.startswith(key):
                compat_data = kb.printer_compatibility[key]
                printer_key = key
                break
    if compat_data is None:
        compat_data = kb.printer_compatibility.get("default")
        if compat_data is None:
            return None
        printer_key = "default"

    if material_id:
        mat_key = material_id.lower()
        mat_data = compat_data.get(mat_key)
        if mat_data is None:
            return PrinterCompatibilityReport(
                printer_id=printer_key,
                materials={mat_key: {"status": "unknown", "notes": "No data available."}},
            )
        return PrinterCompatibilityReport(
            printer_id=printer_key,
            materials={mat_key: mat_data},
        )

    return PrinterCompatibilityReport(
        printer_id=printer_key,
        materials=compat_data,
    )


def list_compatibility_printers() -> list[str]:
    """Return all printer IDs that have compatibility data."""
    kb = _get_kb()
    return sorted(kb.printer_compatibility.keys())


# ---------------------------------------------------------------------------
# Public API — Post-Processing
# ---------------------------------------------------------------------------


def get_post_processing(material_id: str) -> PostProcessingGuide | None:
    """Get post-processing techniques for a material.

    :param material_id: Material key (e.g. ``"pla"``, ``"abs"``).
    """
    kb = _get_kb()
    material_key = material_id.lower()
    data = kb.post_processing.get(material_key)
    if data is None:
        return None

    return PostProcessingGuide(
        material=material_key,
        techniques=data.get("techniques", []),
        paintability=data.get("paintability"),
        strengthening=data.get("strengthening", []),
        upgrade_hint="" if _engineering_overlay_loaded() else _UPGRADE_HINT_POST_PROCESSING,
    )


# ---------------------------------------------------------------------------
# Public API — Multi-Material Compatibility
# ---------------------------------------------------------------------------


def check_multi_material_compatibility(
    material_a: str,
    material_b: str,
) -> MultiMaterialReport:
    """Check if two materials can be co-printed in a dual-extrusion setup.

    Looks up the co-print compatibility matrix and support pair data.
    Returns compatibility status, interface adhesion rating, and
    dissolution info if applicable.

    :param material_a: First material (e.g. ``"pla"``).
    :param material_b: Second material (e.g. ``"tpu"``).
    """
    kb = _get_kb()
    a = material_a.lower()
    b = material_b.lower()
    mm = kb.multi_material

    co_compat = mm.get("co_print_compatibility", {})
    support_pairs = mm.get("support_pairs", [])
    rules = mm.get("general_rules", [])

    # Check co-print compatibility (try both directions)
    pair_data: dict[str, Any] | None = None
    if a in co_compat and b in co_compat[a]:
        pair_data = co_compat[a][b]
    elif b in co_compat and a in co_compat[b]:
        pair_data = co_compat[b][a]

    # Check support pair data
    support_match: dict[str, Any] | None = None
    for sp in support_pairs:
        model = sp.get("model_material", "").lower()
        support = sp.get("support_material", "").lower()
        if (a == model and b == support) or (b == model and a == support):
            support_match = sp
            break

    if pair_data:
        return MultiMaterialReport(
            material_a=a,
            material_b=b,
            compatible=pair_data.get("compatible", False),
            interface_adhesion=pair_data.get("interface_adhesion", "unknown"),
            notes=pair_data.get("notes", ""),
            support_pair=support_match,
            general_rules=rules,
        )

    # No explicit data — use support pair if available
    if support_match:
        adhesion = support_match.get("interface_adhesion", "unknown")
        return MultiMaterialReport(
            material_a=a,
            material_b=b,
            compatible=adhesion not in ("none", "poor"),
            interface_adhesion=adhesion,
            notes=support_match.get("notes", ""),
            support_pair=support_match,
            general_rules=rules,
        )

    # No data at all
    return MultiMaterialReport(
        material_a=a,
        material_b=b,
        compatible=False,
        interface_adhesion="unknown",
        notes=f"No compatibility data for {a} + {b}. Check general rules.",
        support_pair=None,
        general_rules=rules,
    )


def get_support_material_options(model_material: str) -> list[dict[str, Any]]:
    """Get all viable soluble support material options for a model material.

    :param model_material: The model material (e.g. ``"pla"``, ``"abs"``).
    """
    kb = _get_kb()
    model_key = model_material.lower()
    support_pairs = kb.multi_material.get("support_pairs", [])

    results = []
    for sp in support_pairs:
        if sp.get("model_material", "").lower() == model_key:
            results.append(sp)
    return results


# ---------------------------------------------------------------------------
# Public API — Cross-File Diagnostic
# ---------------------------------------------------------------------------


def get_print_diagnostic(
    material_id: str,
    *,
    symptom: str | None = None,
    printer_id: str | None = None,
) -> PrintDiagnostic | None:
    """Cross-file diagnostic combining troubleshooting, compatibility, and guidance.

    This is the primary tool for agents diagnosing print problems.  It
    pulls from troubleshooting data, printer compatibility, storage
    requirements, and post-processing tips to give a comprehensive
    answer in one call.

    :param material_id: Material being printed (e.g. ``"petg"``).
    :param symptom: What's going wrong (e.g. ``"stringing"``, ``"warping"``).
    :param printer_id: Optional printer model for compatibility context.
    """
    ts_result = troubleshoot_print_issue(material_id, symptom)
    if ts_result is None:
        return None

    # Printer compatibility context
    compat: dict[str, Any] | None = None
    if printer_id:
        compat_report = check_printer_material_compatibility(
            printer_id, material_id
        )
        if compat_report:
            mat_key = material_id.lower()
            compat = compat_report.materials.get(mat_key)

    # Post-processing quick tips
    pp_tips: list[str] = []
    pp_guide = get_post_processing(material_id)
    if pp_guide and pp_guide.strengthening:
        for s in pp_guide.strengthening:
            if s.get("applicable"):
                pp_tips.append(
                    f"{s.get('method', 'Unknown')}: ~{s.get('strength_gain_pct', 0)}% "
                    f"strength gain. {s.get('tradeoffs', '')}"
                )

    # Build combined guidance
    combined: list[str] = []
    if ts_result.break_in_tips:
        combined.extend(ts_result.break_in_tips[:3])
    if compat and compat.get("status") == "needs_upgrade":
        upgrades = compat.get("upgrades_needed", [])
        combined.append(
            f"Printer needs upgrades for this material: {', '.join(upgrades)}. "
            f"{compat.get('notes', '')}"
        )
    if compat and compat.get("status") == "not_compatible":
        combined.append(
            f"WARNING: Printer is not compatible with this material. "
            f"{compat.get('notes', '')}"
        )
    if ts_result.storage_requirements:
        sr = ts_result.storage_requirements
        if sr.get("humidity_sensitive"):
            combined.append(
                f"Storage: {sr.get('storage_method', 'sealed container')}. "
                f"Max humidity {sr.get('max_humidity_pct', 'N/A')}%. "
                f"Dry at {sr.get('drying_temp_c', 'N/A')}C for "
                f"{sr.get('drying_time_hours', 'N/A')} hours if wet."
            )

    return PrintDiagnostic(
        material=material_id.lower(),
        printer_id=printer_id,
        symptom=symptom,
        matched_issues=ts_result.matched_issues,
        printer_compatibility=compat,
        storage_requirements=ts_result.storage_requirements,
        post_processing_tips=pp_tips,
        combined_guidance=combined,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _select_length_rows(
    rows: list[dict[str, Any]],
    cantilever_length_mm: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the two rows that bound a requested cantilever length."""
    if not rows:
        return {}, {}

    if cantilever_length_mm <= float(rows[0].get("cantilever_length_mm", 0.0)):
        return rows[0], rows[0]

    if cantilever_length_mm >= float(rows[-1].get("cantilever_length_mm", 0.0)):
        return rows[-1], rows[-1]

    for idx in range(len(rows) - 1):
        current_len = float(rows[idx].get("cantilever_length_mm", 0.0))
        next_len = float(rows[idx + 1].get("cantilever_length_mm", 0.0))
        if current_len <= cantilever_length_mm <= next_len:
            return rows[idx], rows[idx + 1]

    return rows[-1], rows[-1]


def _interpolate_cross_section_load(
    entries: list[dict[str, Any]],
    cross_section_mm2: float,
) -> float:
    """Interpolate or extrapolate max load from section-area lookup points."""
    if not entries:
        return 0.0

    points = sorted(
        (
            float(row.get("cross_section_mm2", 0.0)),
            float(row.get("max_load_n", 0.0)),
        )
        for row in entries
    )
    points = [(x, y) for x, y in points if x > 0]
    if not points or cross_section_mm2 <= 0:
        return 0.0

    first_x, first_y = points[0]
    if cross_section_mm2 <= first_x:
        return first_y * (cross_section_mm2 / first_x)

    for idx in range(len(points) - 1):
        x0, y0 = points[idx]
        x1, y1 = points[idx + 1]
        if x0 <= cross_section_mm2 <= x1:
            if x1 == x0:
                return y0
            t = (cross_section_mm2 - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)

    last_x, last_y = points[-1]
    return last_y * (cross_section_mm2 / last_x)


def _extract_temperatures_c(text: str) -> list[float]:
    """Extract explicit Celsius temperatures from text."""
    matches = re.findall(r"(-?\d+(?:\.\d+)?)\s*(?:°\s*)?c\b", text.lower())
    return [float(m) for m in matches]


def _accumulate_rating_outcome(
    *,
    category: str,
    rating: str,
    has_fail: bool,
    has_warning: bool,
    warnings: list[str],
) -> tuple[bool, bool]:
    """Track pass/conditional/fail status for environment categories."""
    rating_score = _RATING_ORDER.get(rating.lower(), 2)
    if rating_score <= 1:
        warnings.append(f"{category} rating is '{rating}', which is not suitable.")
        return True, True
    if rating_score <= 3:
        warnings.append(f"{category} rating is '{rating}'; use with caution.")
        return has_fail, True
    return has_fail, has_warning


def _find_templates_from_text(text: str) -> list[DesignTemplate]:
    """Find design templates relevant to the given text."""
    kb = _get_kb()
    lower = text.lower()
    results = []
    seen: set[str] = set()

    # Check use_case matches
    for tid, data in kb.templates.items():
        cases = [c.lower().replace("_", " ") for c in data.get("use_cases", [])]
        name_words = data.get("display_name", "").lower().split()

        matched = any(c in lower for c in cases) or any(
            w in lower for w in name_words if len(w) > 3
        )
        if matched and tid not in seen:
            template = get_design_template(tid)
            if template:
                results.append(template)
                seen.add(tid)

    return results


def _requirement_to_rating_key(requirement_id: str) -> str | None:
    """Map a requirement ID to the corresponding use_case_ratings key."""
    mapping = {
        "load_bearing": "structural_load_bearing",
        "watertight": "water_tight",
        "outdoor_use": "outdoor_use",
        "food_contact": "food_contact",
        "heat_exposure": "high_temp_environment",
        "flexibility_required": "repeated_flexing",
        "impact_resistant": "impact_resistance",
        "precision_fit": "dimensional_accuracy",
        "aesthetic_decorative": "cosmetic_finish",
    }
    return mapping.get(requirement_id)


# ---------------------------------------------------------------------------
# Module reset (for testing)
# ---------------------------------------------------------------------------


def _reset_knowledge_base() -> None:
    """Reset the singleton — for testing only."""
    global _kb
    _kb = None
