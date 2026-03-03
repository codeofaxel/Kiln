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
    get_design_pattern        — constraints for a design pattern
    list_design_patterns      — all patterns in a domain
    get_design_constraints    — decompose functional requirements into rules
    match_requirements        — find which requirement profiles match text
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).parent / "data" / "design_knowledge"

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
    """Full material property sheet for design reasoning."""

    material_id: str
    display_name: str
    category: str
    mechanical: dict[str, Any]
    thermal: dict[str, Any]
    chemical: dict[str, Any]
    design_limits: dict[str, Any]
    use_case_ratings: dict[str, Any]
    agent_guidance: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DesignPattern:
    """A functional design pattern with constraints and guidance."""

    pattern_id: str
    display_name: str
    description: str
    use_cases: list[str]
    design_rules: dict[str, Any]
    material_compatibility: dict[str, list[str]]
    print_orientation: str
    print_orientation_reason: str
    agent_guidance: list[str]

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
    """Material survivability report for a described environment."""

    material: str
    environment: str
    per_category_ratings: dict[str, Any]
    warnings: list[str]
    overall_verdict: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrinterDesignProfile:
    """Printer capability profile for design-for-manufacturing decisions."""

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
    agent_notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DesignBrief:
    """Complete design brief combining all constraint sources.

    This is what an agent uses before generating geometry — the full set
    of constraints, material recommendations, applicable patterns, and
    guidance notes for the design task.
    """

    functional_constraints: list[DesignConstraintSet]
    recommended_material: MaterialRecommendation | None
    applicable_patterns: list[DesignPattern]
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
    """Matched print issues with fixes for a material+symptom query."""

    material: str
    matched_issues: list[dict[str, Any]]
    storage_requirements: dict[str, Any] | None
    break_in_tips: list[str]

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
    """Post-processing techniques, paintability, and strengthening for a material."""

    material: str
    techniques: list[dict[str, Any]]
    paintability: dict[str, Any] | None
    strengthening: list[dict[str, Any]]

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


# ---------------------------------------------------------------------------
# Knowledge base loader (lazy singleton)
# ---------------------------------------------------------------------------


class _DesignKnowledgeBase:
    """Loads and indexes the design knowledge JSON files."""

    def __init__(self, domain: str = "fdm") -> None:
        self.domain = domain
        self._materials: dict[str, dict[str, Any]] = {}
        self._patterns: dict[str, dict[str, Any]] = {}
        self._requirements: dict[str, dict[str, Any]] = {}
        self._load_tables: dict[str, dict[str, Any]] = {}
        self._environment: dict[str, dict[str, Any]] = {}
        self._printers: dict[str, dict[str, Any]] = {}
        self._troubleshooting: dict[str, dict[str, Any]] = {}
        self._printer_compatibility: dict[str, dict[str, Any]] = {}
        self._post_processing: dict[str, dict[str, Any]] = {}
        self._multi_material: dict[str, Any] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return

        materials_path = _DATA_DIR / "materials.json"
        patterns_path = _DATA_DIR / "design_patterns.json"
        requirements_path = _DATA_DIR / "functional_requirements.json"
        load_tables_path = _DATA_DIR / "load_tables.json"
        environment_path = _DATA_DIR / "environment_compatibility.json"
        printers_path = _DATA_DIR / "printer_profiles.json"

        if materials_path.exists():
            raw = json.loads(materials_path.read_text(encoding="utf-8"))
            self._materials = {
                k: v for k, v in raw.items() if not k.startswith("_")
            }

        if patterns_path.exists():
            raw = json.loads(patterns_path.read_text(encoding="utf-8"))
            self._patterns = {
                k: v for k, v in raw.items() if not k.startswith("_")
            }

        if requirements_path.exists():
            raw = json.loads(requirements_path.read_text(encoding="utf-8"))
            self._requirements = {
                k: v for k, v in raw.items() if not k.startswith("_")
            }

        if load_tables_path.exists():
            raw = json.loads(load_tables_path.read_text(encoding="utf-8"))
            self._load_tables = {
                k: v for k, v in raw.items() if not k.startswith("_")
            }

        if environment_path.exists():
            raw = json.loads(environment_path.read_text(encoding="utf-8"))
            self._environment = {
                k: v for k, v in raw.items() if not k.startswith("_")
            }

        if printers_path.exists():
            raw = json.loads(printers_path.read_text(encoding="utf-8"))
            self._printers = {
                k: v for k, v in raw.items() if not k.startswith("_")
            }

        troubleshooting_path = _DATA_DIR / "material_troubleshooting.json"
        if troubleshooting_path.exists():
            raw = json.loads(troubleshooting_path.read_text(encoding="utf-8"))
            self._troubleshooting = {
                k: v for k, v in raw.items() if not k.startswith("_")
            }

        compatibility_path = _DATA_DIR / "printer_material_compatibility.json"
        if compatibility_path.exists():
            raw = json.loads(compatibility_path.read_text(encoding="utf-8"))
            self._printer_compatibility = {
                k: v for k, v in raw.items() if not k.startswith("_")
            }

        post_processing_path = _DATA_DIR / "post_processing.json"
        if post_processing_path.exists():
            raw = json.loads(post_processing_path.read_text(encoding="utf-8"))
            self._post_processing = {
                k: v for k, v in raw.items() if not k.startswith("_")
            }

        multi_material_path = _DATA_DIR / "multi_material_pairing.json"
        if multi_material_path.exists():
            raw = json.loads(multi_material_path.read_text(encoding="utf-8"))
            self._multi_material = {
                k: v for k, v in raw.items() if not k.startswith("_")
            }

        self._loaded = True
        logger.info(
            "Design knowledge loaded: %d materials, %d patterns, %d requirements, "
            "%d load tables, %d environments, %d printers, %d troubleshooting, "
            "%d printer-compat, %d post-processing",
            len(self._materials),
            len(self._patterns),
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
    def patterns(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._patterns

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


# Module-level lazy singleton
_kb: _DesignKnowledgeBase | None = None


def _get_kb() -> _DesignKnowledgeBase:
    global _kb
    if _kb is None:
        _kb = _DesignKnowledgeBase()
    return _kb


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
        return None

    return MaterialProfile(
        material_id=material_id.lower(),
        display_name=data["display_name"],
        category=data["category"],
        mechanical=data["mechanical"],
        thermal=data["thermal"],
        chemical=data["chemical"],
        design_limits=data["design_limits"],
        use_case_ratings=data["use_case_ratings"],
        agent_guidance=data["agent_guidance"],
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
                mechanical=data["mechanical"],
                thermal=data["thermal"],
                chemical=data["chemical"],
                design_limits=data["design_limits"],
                use_case_ratings=data["use_case_ratings"],
                agent_guidance=data["agent_guidance"],
            )
        )
    return profiles


def recommend_material_for_design(
    requirements_text: str,
    *,
    printer_has_enclosure: bool = False,
    printer_has_direct_drive: bool = True,
    max_hotend_temp_c: int = 300,
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
    """
    kb = _get_kb()
    matched = match_requirements(requirements_text)

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
        assert pla is not None
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
    assert top_profile is not None

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

    return MaterialRecommendation(
        material=top_profile,
        score=round(top_score, 1),
        reasons=top_reasons,
        warnings=top_warnings,
        design_limits_summary=top_profile.design_limits,
        alternatives=alternatives,
    )


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

    uv_keywords = ("uv", "sun", "sunlight", "outdoor", "weather")
    moisture_keywords = (
        "water",
        "wet",
        "moisture",
        "humidity",
        "rain",
        "immersion",
        "submerged",
        "wash",
        "dishwasher",
        "marine",
    )
    heat_keywords = (
        "heat",
        "hot",
        "temperature",
        "engine",
        "dashboard",
        "thermal",
        "summer",
        "oven",
    )
    cold_keywords = ("cold", "freez", "winter", "subzero", "ice")
    vibration_keywords = ("vibration", "vibrate", "fatigue", "cyclic", "oscillation")
    abrasion_keywords = ("abrasion", "wear", "friction", "rubbing", "sliding", "scratch")

    if any(k in env_text for k in uv_keywords):
        uv = material_data.get("uv_resistance", {})
        rating = str(uv.get("rating", "conditional")).lower()
        per_category["uv_resistance"] = rating
        has_fail, has_warning = _accumulate_rating_outcome(
            category="uv_resistance",
            rating=rating,
            has_fail=has_fail,
            has_warning=has_warning,
            warnings=warnings,
        )

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

    if any(k in env_text for k in moisture_keywords):
        moisture = material_data.get("moisture", {})
        rating = str(moisture.get("rating", "conditional")).lower()
        per_category["moisture"] = rating
        has_fail, has_warning = _accumulate_rating_outcome(
            category="moisture",
            rating=rating,
            has_fail=has_fail,
            has_warning=has_warning,
            warnings=warnings,
        )
        immersion_keywords = ("immersion", "submerged", "underwater", "continuous water")
        immersion_safe = bool(moisture.get("immersion_safe", False))
        if any(k in env_text for k in immersion_keywords) and not immersion_safe:
            has_fail = True
            warnings.append("Material is not rated immersion-safe for this environment.")

    chemical_map = {
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

    if any(k in env_text for k in vibration_keywords):
        vibration = material_data.get("vibration_fatigue", {})
        rating = str(vibration.get("rating", "conditional")).lower()
        per_category["vibration_fatigue"] = rating
        has_fail, has_warning = _accumulate_rating_outcome(
            category="vibration_fatigue",
            rating=rating,
            has_fail=has_fail,
            has_warning=has_warning,
            warnings=warnings,
        )

    if any(k in env_text for k in abrasion_keywords):
        abrasion = material_data.get("abrasion_resistance", {})
        rating = str(abrasion.get("rating", "conditional")).lower()
        per_category["abrasion_resistance"] = rating
        has_fail, has_warning = _accumulate_rating_outcome(
            category="abrasion_resistance",
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
    )


def get_printer_design_profile(printer_id: str) -> PrinterDesignProfile | None:
    """Get design capabilities for a specific printer."""
    kb = _get_kb()
    printer_key = printer_id.lower()
    data = kb.printers.get(printer_key)
    if data is None:
        return None

    return PrinterDesignProfile(
        printer_id=printer_key,
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
        agent_notes=data["agent_notes"],
    )


def list_printer_profiles() -> list[PrinterDesignProfile]:
    """List all known printer profiles."""
    kb = _get_kb()
    profiles = []
    for printer_id, data in sorted(kb.printers.items()):
        profiles.append(
            PrinterDesignProfile(
                printer_id=printer_id,
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
                agent_notes=data["agent_notes"],
            )
        )
    return profiles


# ---------------------------------------------------------------------------
# Public API — Design Patterns
# ---------------------------------------------------------------------------


def get_design_pattern(pattern_id: str) -> DesignPattern | None:
    """Get a design pattern by ID.

    :param pattern_id: Pattern key (e.g. ``"snap_fit_cantilever"``).
    """
    kb = _get_kb()
    data = kb.patterns.get(pattern_id)
    if data is None:
        return None

    return DesignPattern(
        pattern_id=pattern_id,
        display_name=data["display_name"],
        description=data["description"],
        use_cases=data["use_cases"],
        design_rules=data["design_rules"],
        material_compatibility=data["material_compatibility"],
        print_orientation=data["print_orientation"],
        print_orientation_reason=data["print_orientation_reason"],
        agent_guidance=data["agent_guidance"],
    )


def list_design_patterns() -> list[DesignPattern]:
    """Return all design patterns sorted by name."""
    kb = _get_kb()
    patterns = []
    for pid, data in sorted(kb.patterns.items()):
        patterns.append(
            DesignPattern(
                pattern_id=pid,
                display_name=data["display_name"],
                description=data["description"],
                use_cases=data["use_cases"],
                design_rules=data["design_rules"],
                material_compatibility=data["material_compatibility"],
                print_orientation=data["print_orientation"],
                print_orientation_reason=data["print_orientation_reason"],
                agent_guidance=data["agent_guidance"],
            )
        )
    return patterns


def find_patterns_for_use_case(use_case: str) -> list[DesignPattern]:
    """Find design patterns that match a use case.

    :param use_case: Use case keyword (e.g. ``"enclosures"``, ``"gears"``).
    """
    kb = _get_kb()
    lower = use_case.lower()
    results = []

    for pid, data in kb.patterns.items():
        cases = [c.lower() for c in data.get("use_cases", [])]
        if any(lower in c or c in lower for c in cases):
            pattern = get_design_pattern(pid)
            if pattern:
                results.append(pattern)

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
                )
            )

    return results


def get_design_constraints(
    requirements_text: str,
    *,
    material: str | None = None,
    printer_model: str | None = None,
) -> DesignBrief:
    """Decompose functional requirements into a complete design brief.

    This is the main entry point for agents.  Given a natural language
    description of what the user needs, returns a :class:`DesignBrief`
    with material recommendation, applicable patterns, combined
    constraints, and guidance notes.

    :param requirements_text: What the object needs to do (e.g.
        ``"phone mount for car dashboard, holds phone securely, survives
        summer heat"``).
    :param material: Optional material override (skip recommendation).
    :param printer_model: Optional printer model for capability lookup.
    """
    # 1. Match functional requirements
    constraints = match_requirements(requirements_text)

    # 2. Recommend material (unless overridden)
    recommendation: MaterialRecommendation | None = None
    if material:
        profile = get_material_profile(material)
        if profile:
            recommendation = MaterialRecommendation(
                material=profile,
                score=100.0,
                reasons=["User-specified material."],
                warnings=[],
                design_limits_summary=profile.design_limits,
                alternatives=[],
            )
    else:
        recommendation = recommend_material_for_design(requirements_text)

    # 3. Find applicable patterns
    patterns = _find_patterns_from_text(requirements_text)

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

    # Add pattern guidance
    for pattern in patterns:
        combined_guidance.extend(pattern.agent_guidance)

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


def _find_patterns_from_text(text: str) -> list[DesignPattern]:
    """Find design patterns relevant to the given text."""
    kb = _get_kb()
    lower = text.lower()
    results = []
    seen: set[str] = set()

    # Check use_case matches
    for pid, data in kb.patterns.items():
        cases = [c.lower().replace("_", " ") for c in data.get("use_cases", [])]
        name_words = data.get("display_name", "").lower().split()

        matched = any(c in lower for c in cases) or any(
            w in lower for w in name_words if len(w) > 3
        )
        if matched and pid not in seen:
            pattern = get_design_pattern(pid)
            if pattern:
                results.append(pattern)
                seen.add(pid)

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
# Construction domain — dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ConstructionMaterialProfile:
    """Material profile for construction-scale 3D printing."""

    material_id: str
    display_name: str
    category: str
    mechanical: dict[str, Any]
    thermal: dict[str, Any]
    process: dict[str, Any]
    design_limits: dict[str, Any]
    cost: dict[str, Any]
    compliance: dict[str, Any]
    agent_guidance: list[str]
    sustainability: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d.get("sustainability") is None:
            d.pop("sustainability", None)
        return d


@dataclass
class ConstructionPattern:
    """Architectural design pattern for structural printing."""

    pattern_id: str
    display_name: str
    description: str
    use_cases: list[str]
    design_rules: dict[str, Any]
    agent_guidance: list[str]
    wall_profiles: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d.get("wall_profiles") is None:
            d.pop("wall_profiles", None)
        return d


@dataclass
class ConstructionRequirement:
    """Building program requirement for construction-scale design."""

    requirement_id: str
    display_name: str
    program_requirements: dict[str, Any]
    structural_constraints: dict[str, Any]
    print_planning: dict[str, Any]
    agent_guidance: list[str]
    code_requirements: dict[str, Any] | None = None
    compliance_requirements: dict[str, Any] | None = None
    triggers: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("code_requirements", "compliance_requirements", "triggers"):
            if d.get(key) is None:
                d.pop(key, None)
        return d


@dataclass
class ConstructionDesignBrief:
    """Complete design brief for construction-scale projects."""

    requirement: ConstructionRequirement | None
    materials: list[ConstructionMaterialProfile]
    applicable_patterns: list[ConstructionPattern]
    combined_guidance: list[str]
    combined_rules: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement.to_dict() if self.requirement else None,
            "materials": [m.to_dict() for m in self.materials],
            "applicable_patterns": [p.to_dict() for p in self.applicable_patterns],
            "combined_guidance": self.combined_guidance,
            "combined_rules": self.combined_rules,
        }


# ---------------------------------------------------------------------------
# Construction domain — knowledge base loader
# ---------------------------------------------------------------------------


class _ConstructionKnowledgeBase:
    """Loads construction-domain design knowledge."""

    def __init__(self) -> None:
        self._materials: dict[str, dict[str, Any]] = {}
        self._patterns: dict[str, dict[str, Any]] = {}
        self._requirements: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return

        for name, target in (
            ("construction_materials.json", "_materials"),
            ("construction_patterns.json", "_patterns"),
            ("construction_requirements.json", "_requirements"),
        ):
            path = _DATA_DIR / name
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                setattr(
                    self,
                    target,
                    {k: v for k, v in raw.items() if not k.startswith("_")},
                )

        self._loaded = True
        logger.info(
            "Construction knowledge loaded: %d materials, %d patterns, %d requirements",
            len(self._materials),
            len(self._patterns),
            len(self._requirements),
        )

    @property
    def materials(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._materials

    @property
    def patterns(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._patterns

    @property
    def requirements(self) -> dict[str, dict[str, Any]]:
        self._load()
        return self._requirements


_construction_kb: _ConstructionKnowledgeBase | None = None


def _get_construction_kb() -> _ConstructionKnowledgeBase:
    global _construction_kb
    if _construction_kb is None:
        _construction_kb = _ConstructionKnowledgeBase()
    return _construction_kb


# ---------------------------------------------------------------------------
# Construction domain — public API
# ---------------------------------------------------------------------------


def get_construction_material(material_id: str) -> ConstructionMaterialProfile | None:
    """Get full construction material profile.

    :param material_id: Material key (e.g. ``"standard_concrete_mix"``,
        ``"icon_carbonx"``).
    """
    kb = _get_construction_kb()
    data = kb.materials.get(material_id.lower())
    if data is None:
        return None

    return ConstructionMaterialProfile(
        material_id=material_id.lower(),
        display_name=data["display_name"],
        category=data["category"],
        mechanical=data["mechanical"],
        thermal=data["thermal"],
        process=data["process"],
        design_limits=data["design_limits"],
        cost=data["cost"],
        compliance=data["compliance"],
        agent_guidance=data["agent_guidance"],
        sustainability=data.get("sustainability"),
    )


def list_construction_materials() -> list[ConstructionMaterialProfile]:
    """Return all construction material profiles."""
    kb = _get_construction_kb()
    profiles = []
    for mid, data in sorted(kb.materials.items()):
        profiles.append(
            ConstructionMaterialProfile(
                material_id=mid,
                display_name=data["display_name"],
                category=data["category"],
                mechanical=data["mechanical"],
                thermal=data["thermal"],
                process=data["process"],
                design_limits=data["design_limits"],
                cost=data["cost"],
                compliance=data["compliance"],
                agent_guidance=data["agent_guidance"],
                sustainability=data.get("sustainability"),
            )
        )
    return profiles


def get_construction_pattern(pattern_id: str) -> ConstructionPattern | None:
    """Get a construction design pattern by ID.

    :param pattern_id: Pattern key (e.g. ``"load_bearing_wall"``).
    """
    kb = _get_construction_kb()
    data = kb.patterns.get(pattern_id)
    if data is None:
        return None

    return ConstructionPattern(
        pattern_id=pattern_id,
        display_name=data["display_name"],
        description=data["description"],
        use_cases=data["use_cases"],
        design_rules=data["design_rules"],
        agent_guidance=data["agent_guidance"],
        wall_profiles=data.get("wall_profiles"),
    )


def list_construction_patterns() -> list[ConstructionPattern]:
    """Return all construction design patterns."""
    kb = _get_construction_kb()
    patterns = []
    for pid, data in sorted(kb.patterns.items()):
        patterns.append(
            ConstructionPattern(
                pattern_id=pid,
                display_name=data["display_name"],
                description=data["description"],
                use_cases=data["use_cases"],
                design_rules=data["design_rules"],
                agent_guidance=data["agent_guidance"],
                wall_profiles=data.get("wall_profiles"),
            )
        )
    return patterns


def get_construction_requirement(
    requirement_id: str,
) -> ConstructionRequirement | None:
    """Get a construction building program requirement.

    :param requirement_id: Requirement key (e.g.
        ``"single_family_residential"``, ``"military_defense"``).
    """
    kb = _get_construction_kb()
    data = kb.requirements.get(requirement_id)
    if data is None:
        return None

    return ConstructionRequirement(
        requirement_id=requirement_id,
        display_name=data["display_name"],
        program_requirements=data.get("program_requirements", {}),
        structural_constraints=data.get("structural_constraints", {}),
        print_planning=data.get("print_planning", {}),
        agent_guidance=data.get("agent_guidance", []),
        code_requirements=data.get("code_requirements"),
        compliance_requirements=data.get("compliance_requirements"),
        triggers=data.get("triggers"),
    )


def list_construction_requirements() -> list[ConstructionRequirement]:
    """Return all construction building program requirements."""
    kb = _get_construction_kb()
    reqs = []
    for rid, data in sorted(kb.requirements.items()):
        reqs.append(
            ConstructionRequirement(
                requirement_id=rid,
                display_name=data["display_name"],
                program_requirements=data.get("program_requirements", {}),
                structural_constraints=data.get("structural_constraints", {}),
                print_planning=data.get("print_planning", {}),
                agent_guidance=data.get("agent_guidance", []),
                code_requirements=data.get("code_requirements"),
                compliance_requirements=data.get("compliance_requirements"),
                triggers=data.get("triggers"),
            )
        )
    return reqs


def match_construction_requirements(
    text: str,
) -> list[ConstructionRequirement]:
    """Match natural language to construction building program types.

    Scans input text for trigger words from each building program type
    and returns all matches.

    :param text: Description of the building project (e.g.
        ``"affordable housing for 50 families"``).
    """
    kb = _get_construction_kb()
    lower = text.lower()
    results = []

    for req_id, data in kb.requirements.items():
        triggers = data.get("triggers", [])
        matched_triggers = [t for t in triggers if t.lower() in lower]
        if matched_triggers:
            req = get_construction_requirement(req_id)
            if req:
                results.append(req)

    return results


def get_construction_design_brief(
    requirements_text: str,
    *,
    material: str | None = None,
) -> ConstructionDesignBrief:
    """Get a complete construction design brief from requirements text.

    This is the main entry point for agents doing construction-scale
    design.  Given a natural language description of the building project,
    returns matched building program, applicable materials, structural
    patterns, and combined guidance.

    :param requirements_text: Description of the building project (e.g.
        ``"single family home, 1500 sqft, affordable"``).
    :param material: Optional material override (e.g.
        ``"geopolymer_concrete"``).
    """
    # 1. Match building program requirements
    matched_reqs = match_construction_requirements(requirements_text)
    primary_req = matched_reqs[0] if matched_reqs else None

    # 2. Select materials
    materials: list[ConstructionMaterialProfile] = []
    if material:
        mat = get_construction_material(material)
        if mat:
            materials = [mat]
    else:
        materials = list_construction_materials()

    # 3. Find applicable patterns from text
    patterns = _find_construction_patterns_from_text(requirements_text)

    # 4. Combine guidance and rules
    combined_guidance: list[str] = []
    combined_rules: dict[str, Any] = {}

    if primary_req:
        combined_guidance.extend(primary_req.agent_guidance)
        # Merge structural constraints as rules
        for key, value in primary_req.structural_constraints.items():
            combined_rules[key] = value
        # Merge program requirements
        for key, value in primary_req.program_requirements.items():
            combined_rules[f"program_{key}"] = value

    for mat in materials:
        combined_guidance.extend(mat.agent_guidance)

    for pat in patterns:
        combined_guidance.extend(pat.agent_guidance)
        for key, value in pat.design_rules.items():
            if key.startswith("min_") and key in combined_rules:
                if isinstance(value, (int, float)):
                    combined_rules[key] = max(combined_rules[key], value)
                else:
                    combined_rules[key] = value
            elif key.startswith("max_") and key in combined_rules:
                if isinstance(value, (int, float)):
                    combined_rules[key] = min(combined_rules[key], value)
                else:
                    combined_rules[key] = value
            else:
                combined_rules[key] = value

    return ConstructionDesignBrief(
        requirement=primary_req,
        materials=materials,
        applicable_patterns=patterns,
        combined_guidance=combined_guidance,
        combined_rules=combined_rules,
    )


def _find_construction_patterns_from_text(
    text: str,
) -> list[ConstructionPattern]:
    """Find construction patterns relevant to the given text."""
    kb = _get_construction_kb()
    lower = text.lower()
    results = []
    seen: set[str] = set()

    for pid, data in kb.patterns.items():
        cases = [c.lower().replace("_", " ") for c in data.get("use_cases", [])]
        name_words = data.get("display_name", "").lower().split()

        matched = any(c in lower for c in cases) or any(
            w in lower for w in name_words if len(w) > 3
        )
        if matched and pid not in seen:
            pattern = get_construction_pattern(pid)
            if pattern:
                results.append(pattern)
                seen.add(pid)

    return results


# ---------------------------------------------------------------------------
# Module reset (for testing)
# ---------------------------------------------------------------------------


def _reset_knowledge_base() -> None:
    """Reset the singleton — for testing only."""
    global _kb, _construction_kb
    _kb = None
    _construction_kb = None
