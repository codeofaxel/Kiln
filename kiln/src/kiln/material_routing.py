"""Smart material routing for 3D printing.

Translates user intent ("make it strong", "make it pretty", "make it cheap")
into optimal material + settings combinations based on historical data,
material properties, and printer capabilities.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class MaterialProperties:
    """Physical and practical properties of a 3D printing material."""

    name: str
    display_name: str
    strength: float  # 0-1
    flexibility: float  # 0-1
    heat_resistance: float  # 0-1
    surface_quality: float  # 0-1
    ease_of_print: float  # 0-1
    cost_per_kg_usd: float
    typical_hotend_temp: int
    typical_bed_temp: int
    requires_enclosure: bool
    requires_heated_bed: bool
    suitable_for: list[str]  # ["functional", "decorative", "prototyping", etc.]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MaterialRecommendation:
    """A scored material recommendation for a given intent."""

    material: MaterialProperties
    score: float  # 0-100
    reasoning: str
    settings: dict[str, Any]  # layer_height, speed, temps
    estimated_cost_usd: float | None
    success_rate: float | None  # from print DNA if available
    alternatives: list[dict[str, Any]]  # other options

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["material"] = self.material.to_dict()
        return data


@dataclass
class IntentMapping:
    """Maps a user intent keyword to attribute weights."""

    intent: str  # user's words
    primary_attribute: str  # "strength", "surface_quality", etc.
    weights: dict[str, float]  # attribute weights

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Built-in material database
# ---------------------------------------------------------------------------

_MATERIALS: dict[str, MaterialProperties] = {
    "pla": MaterialProperties(
        name="pla",
        display_name="PLA",
        strength=0.5,
        flexibility=0.2,
        heat_resistance=0.2,
        surface_quality=0.8,
        ease_of_print=0.95,
        cost_per_kg_usd=20,
        typical_hotend_temp=200,
        typical_bed_temp=60,
        requires_enclosure=False,
        requires_heated_bed=False,
        suitable_for=["decorative", "prototyping", "cosplay"],
    ),
    "petg": MaterialProperties(
        name="petg",
        display_name="PETG",
        strength=0.7,
        flexibility=0.4,
        heat_resistance=0.5,
        surface_quality=0.6,
        ease_of_print=0.8,
        cost_per_kg_usd=22,
        typical_hotend_temp=235,
        typical_bed_temp=80,
        requires_enclosure=False,
        requires_heated_bed=True,
        suitable_for=["functional", "outdoor", "food_safe"],
    ),
    "abs": MaterialProperties(
        name="abs",
        display_name="ABS",
        strength=0.7,
        flexibility=0.3,
        heat_resistance=0.7,
        surface_quality=0.5,
        ease_of_print=0.5,
        cost_per_kg_usd=20,
        typical_hotend_temp=240,
        typical_bed_temp=100,
        requires_enclosure=True,
        requires_heated_bed=True,
        suitable_for=["functional", "automotive", "enclosures"],
    ),
    "tpu": MaterialProperties(
        name="tpu",
        display_name="TPU",
        strength=0.4,
        flexibility=0.95,
        heat_resistance=0.4,
        surface_quality=0.5,
        ease_of_print=0.4,
        cost_per_kg_usd=30,
        typical_hotend_temp=225,
        typical_bed_temp=50,
        requires_enclosure=False,
        requires_heated_bed=False,
        suitable_for=["flexible", "wearable", "phone_cases", "gaskets"],
    ),
    "asa": MaterialProperties(
        name="asa",
        display_name="ASA",
        strength=0.7,
        flexibility=0.3,
        heat_resistance=0.7,
        surface_quality=0.6,
        ease_of_print=0.5,
        cost_per_kg_usd=25,
        typical_hotend_temp=240,
        typical_bed_temp=100,
        requires_enclosure=True,
        requires_heated_bed=True,
        suitable_for=["outdoor", "uv_resistant", "functional"],
    ),
    "nylon": MaterialProperties(
        name="nylon",
        display_name="Nylon (PA)",
        strength=0.9,
        flexibility=0.6,
        heat_resistance=0.6,
        surface_quality=0.5,
        ease_of_print=0.3,
        cost_per_kg_usd=40,
        typical_hotend_temp=260,
        typical_bed_temp=80,
        requires_enclosure=True,
        requires_heated_bed=True,
        suitable_for=["functional", "mechanical", "gears", "high_strength"],
    ),
    "pc": MaterialProperties(
        name="pc",
        display_name="Polycarbonate",
        strength=0.95,
        flexibility=0.3,
        heat_resistance=0.9,
        surface_quality=0.5,
        ease_of_print=0.2,
        cost_per_kg_usd=45,
        typical_hotend_temp=280,
        typical_bed_temp=110,
        requires_enclosure=True,
        requires_heated_bed=True,
        suitable_for=["high_strength", "high_temp", "optical", "safety"],
    ),
    "pla_plus": MaterialProperties(
        name="pla_plus",
        display_name="PLA+",
        strength=0.6,
        flexibility=0.3,
        heat_resistance=0.3,
        surface_quality=0.8,
        ease_of_print=0.9,
        cost_per_kg_usd=22,
        typical_hotend_temp=210,
        typical_bed_temp=60,
        requires_enclosure=False,
        requires_heated_bed=False,
        suitable_for=["prototyping", "functional_light", "decorative"],
    ),
}


# ---------------------------------------------------------------------------
# Intent mapping
# ---------------------------------------------------------------------------

_INTENT_MAP: dict[str, IntentMapping] = {
    "strong": IntentMapping(
        intent="strong",
        primary_attribute="strength",
        weights={
            "strength": 0.5,
            "heat_resistance": 0.2,
            "flexibility": 0.1,
            "ease_of_print": 0.1,
            "cost_per_kg_usd": 0.1,
        },
    ),
    "pretty": IntentMapping(
        intent="pretty",
        primary_attribute="surface_quality",
        weights={
            "surface_quality": 0.5,
            "ease_of_print": 0.2,
            "cost_per_kg_usd": 0.1,
            "strength": 0.1,
            "flexibility": 0.1,
        },
    ),
    "cheap": IntentMapping(
        intent="cheap",
        primary_attribute="cost_per_kg_usd",
        weights={
            "cost_per_kg_usd": 0.5,
            "ease_of_print": 0.3,
            "surface_quality": 0.1,
            "strength": 0.1,
        },
    ),
    "flexible": IntentMapping(
        intent="flexible",
        primary_attribute="flexibility",
        weights={
            "flexibility": 0.5,
            "ease_of_print": 0.2,
            "strength": 0.1,
            "cost_per_kg_usd": 0.1,
            "surface_quality": 0.1,
        },
    ),
    "durable": IntentMapping(
        intent="durable",
        primary_attribute="strength",
        weights={
            "strength": 0.3,
            "heat_resistance": 0.3,
            "flexibility": 0.2,
            "ease_of_print": 0.1,
            "cost_per_kg_usd": 0.1,
        },
    ),
    "easy": IntentMapping(
        intent="easy",
        primary_attribute="ease_of_print",
        weights={
            "ease_of_print": 0.5,
            "cost_per_kg_usd": 0.2,
            "surface_quality": 0.2,
            "strength": 0.1,
        },
    ),
    "outdoor": IntentMapping(
        intent="outdoor",
        primary_attribute="heat_resistance",
        weights={
            "heat_resistance": 0.4,
            "strength": 0.3,
            "surface_quality": 0.1,
            "ease_of_print": 0.1,
            "cost_per_kg_usd": 0.1,
        },
    ),
    "food_safe": IntentMapping(
        intent="food_safe",
        primary_attribute="surface_quality",
        weights={
            "surface_quality": 0.3,
            "ease_of_print": 0.3,
            "cost_per_kg_usd": 0.2,
            "strength": 0.2,
        },
    ),
}

# Default weights when no intent matches
_DEFAULT_WEIGHTS: dict[str, float] = {
    "strength": 0.2,
    "flexibility": 0.1,
    "heat_resistance": 0.1,
    "surface_quality": 0.2,
    "ease_of_print": 0.2,
    "cost_per_kg_usd": 0.2,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_intent(user_text: str) -> IntentMapping:
    """Fuzzy-match user text to a known intent.

    Checks for substring matches of known intent keywords within the
    user's text.  Falls back to a balanced default mapping if no match.

    :param user_text: Natural language user input (e.g. ``"make it strong"``).
    """
    lower = user_text.lower()

    for keyword, mapping in _INTENT_MAP.items():
        if keyword in lower:
            return mapping

    return IntentMapping(
        intent="balanced",
        primary_attribute="ease_of_print",
        weights=_DEFAULT_WEIGHTS,
    )


def _score_material(
    mat: MaterialProperties,
    weights: dict[str, float],
) -> float:
    """Score a material against the given attribute weights (0-100)."""
    score = 0.0

    for attr, weight in weights.items():
        if attr == "cost_per_kg_usd":
            # Invert cost: lower cost = higher score
            # Normalise to 0-1 range using max cost of 50 USD/kg
            cost_score = max(0.0, 1.0 - mat.cost_per_kg_usd / 50.0)
            score += cost_score * weight
        else:
            val = getattr(mat, attr, 0.0)
            score += val * weight

    return round(score * 100, 2)


def _default_settings(mat: MaterialProperties) -> dict[str, Any]:
    """Generate default print settings for a material."""
    return {
        "hotend_temp": mat.typical_hotend_temp,
        "bed_temp": mat.typical_bed_temp,
        "layer_height": 0.2,
        "speed": 50 if mat.ease_of_print >= 0.7 else 35,
        "fan_speed": 100 if mat.name in ("pla", "pla_plus") else 50,
        "retraction": 1.0 if mat.name == "tpu" else 0.5,
    }


def recommend_material(
    intent: str,
    *,
    printer_capabilities: dict[str, Any] | None = None,
    budget_usd: float | None = None,
    model_fingerprint: dict[str, Any] | None = None,
) -> MaterialRecommendation:
    """Recommend a material based on user intent and constraints.

    Maps the intent string to attribute weights via fuzzy matching,
    scores each material, filters by printer capabilities and budget,
    and returns the top recommendation with alternatives.

    :param intent: User intent text (e.g. ``"make it strong"``).
    :param printer_capabilities: Optional dict with ``has_enclosure`` and
        ``has_heated_bed`` keys.
    :param budget_usd: Optional max budget per kg in USD.
    :param model_fingerprint: Optional fingerprint dict to check Print DNA
        for historical success rates.
    """
    mapping = parse_intent(intent)
    candidates = list(_MATERIALS.values())

    # Filter by printer capabilities
    if printer_capabilities:
        has_enclosure = printer_capabilities.get("has_enclosure", False)
        has_heated_bed = printer_capabilities.get("has_heated_bed", True)

        filtered = []
        for mat in candidates:
            if mat.requires_enclosure and not has_enclosure:
                continue
            if mat.requires_heated_bed and not has_heated_bed:
                continue
            filtered.append(mat)

        if filtered:
            candidates = filtered

    # Filter by budget
    if budget_usd is not None:
        budget_filtered = [m for m in candidates if m.cost_per_kg_usd <= budget_usd]
        if budget_filtered:
            candidates = budget_filtered

    # Score all candidates
    scored: list[tuple[float, MaterialProperties]] = []
    for mat in candidates:
        score = _score_material(mat, mapping.weights)
        scored.append((score, mat))

    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored:
        # Fallback to PLA if everything got filtered out
        pla = _MATERIALS["pla"]
        return MaterialRecommendation(
            material=pla,
            score=50.0,
            reasoning="Defaulting to PLA — all other materials were filtered out by constraints.",
            settings=_default_settings(pla),
            estimated_cost_usd=None,
            success_rate=None,
            alternatives=[],
        )

    top_score, top_mat = scored[0]

    # Build reasoning
    reasoning = (
        f"{top_mat.display_name} scores highest for '{mapping.intent}' intent "
        f"(primary attribute: {mapping.primary_attribute}). "
        f"Score: {top_score}/100."
    )

    # Check Print DNA for success rate if fingerprint provided
    success_rate: float | None = None
    if model_fingerprint:
        try:
            from kiln.print_dna import get_success_rate

            file_hash = model_fingerprint.get("file_hash", "")
            if file_hash:
                rate_data = get_success_rate(file_hash, material=top_mat.name)
                if rate_data["total_prints"] > 0:
                    success_rate = rate_data["success_rate"]
        except Exception:
            logger.debug("Could not check Print DNA for success rate", exc_info=True)

    # Build alternatives (up to 3, excluding the top pick)
    alternatives: list[dict[str, Any]] = []
    for alt_score, alt_mat in scored[1:4]:
        alternatives.append(
            {
                "material": alt_mat.display_name,
                "name": alt_mat.name,
                "score": alt_score,
                "settings": _default_settings(alt_mat),
            }
        )

    return MaterialRecommendation(
        material=top_mat,
        score=top_score,
        reasoning=reasoning,
        settings=_default_settings(top_mat),
        estimated_cost_usd=top_mat.cost_per_kg_usd,
        success_rate=success_rate,
        alternatives=alternatives,
    )


def list_materials() -> list[MaterialProperties]:
    """Return all available materials sorted by name."""
    return sorted(_MATERIALS.values(), key=lambda m: m.name)


def get_material(name: str) -> MaterialProperties | None:
    """Look up a material by name.

    :param name: Material name (case-insensitive).
    """
    return _MATERIALS.get(name.lower())
