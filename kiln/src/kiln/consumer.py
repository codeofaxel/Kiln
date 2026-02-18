"""Consumer workflow utilities for users without 3D printers.

Handles the complete journey from 'I need a custom phone stand' to
receiving a manufactured product — address validation, material
recommendations, timeline estimates, and onboarding guidance.
"""

from __future__ import annotations

import enum
import re
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------

# ISO 3166-1 alpha-2 codes for countries supported by fulfillment providers.
_SUPPORTED_COUNTRIES: dict[str, str] = {
    "US": "United States",
    "CA": "Canada",
    "GB": "United Kingdom",
    "DE": "Germany",
    "FR": "France",
    "NL": "Netherlands",
    "BE": "Belgium",
    "AT": "Austria",
    "CH": "Switzerland",
    "AU": "Australia",
    "NZ": "New Zealand",
    "JP": "Japan",
    "KR": "South Korea",
    "SG": "Singapore",
    "IE": "Ireland",
    "IT": "Italy",
    "ES": "Spain",
    "PT": "Portugal",
    "SE": "Sweden",
    "NO": "Norway",
    "DK": "Denmark",
    "FI": "Finland",
    "PL": "Poland",
    "CZ": "Czech Republic",
}

# US ZIP code pattern: 5 digits or 5+4.
_US_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")
# Canadian postal code: A1A 1A1
_CA_POSTAL_RE = re.compile(r"^[A-Z]\d[A-Z]\s?\d[A-Z]\d$", re.IGNORECASE)
# UK postcode: various formats
_UK_POSTAL_RE = re.compile(
    r"^[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}$",
    re.IGNORECASE,
)


@dataclass
class AddressValidation:
    """Result of validating a shipping address."""

    valid: bool
    address: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    normalized: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_address(address: dict[str, str]) -> AddressValidation:
    """Validate and normalize a shipping address.

    Required fields: street, city, country.
    Optional but recommended: state, postal_code.

    Args:
        address: Dict with keys street, city, state, postal_code, country.

    Returns:
        AddressValidation with errors/warnings and normalized address.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Required fields
    street = (address.get("street") or "").strip()
    city = (address.get("city") or "").strip()
    country = (address.get("country") or "").strip().upper()
    state = (address.get("state") or "").strip()
    postal_code = (address.get("postal_code") or address.get("zip") or "").strip()

    if not street:
        errors.append("Street address is required.")
    if not city:
        errors.append("City is required.")
    if not country:
        errors.append("Country code is required (e.g. 'US', 'GB', 'DE').")
    elif country not in _SUPPORTED_COUNTRIES:
        errors.append(
            f"Country '{country}' is not supported for fulfillment shipping. "
            f"Supported: {', '.join(sorted(_SUPPORTED_COUNTRIES))}."
        )

    # Postal code validation per country
    if country and postal_code:
        if country == "US" and not _US_ZIP_RE.match(postal_code):
            errors.append(f"Invalid US ZIP code: '{postal_code}'. Expected 5 digits (e.g. 90210).")
        elif country == "CA" and not _CA_POSTAL_RE.match(postal_code):
            errors.append(f"Invalid Canadian postal code: '{postal_code}'. Expected A1A 1A1 format.")
        elif country == "GB" and not _UK_POSTAL_RE.match(postal_code):
            errors.append(f"Invalid UK postcode: '{postal_code}'.")
    elif country in ("US", "CA", "GB") and not postal_code:
        warnings.append(f"Postal code is strongly recommended for {_SUPPORTED_COUNTRIES.get(country, country)}.")

    # State warnings
    if country == "US" and not state:
        warnings.append("State is recommended for US addresses.")

    normalized = {
        "street": street,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "country": country,
    }

    return AddressValidation(
        valid=len(errors) == 0,
        address=address,
        warnings=warnings,
        errors=errors,
        normalized=normalized if not errors else {},
    )


# ---------------------------------------------------------------------------
# Material recommendation engine
# ---------------------------------------------------------------------------


class UseCase(enum.Enum):
    """Common consumer use cases for 3D printed objects."""

    DECORATIVE = "decorative"
    FUNCTIONAL = "functional"
    MECHANICAL = "mechanical"
    PROTOTYPE = "prototype"
    MINIATURE = "miniature"
    JEWELRY = "jewelry"
    ENCLOSURE = "enclosure"
    WEARABLE = "wearable"
    OUTDOOR = "outdoor"
    FOOD_SAFE = "food_safe"


@dataclass
class MaterialRecommendation:
    """A recommended material with reasoning."""

    technology: str  # FDM, SLA, SLS, MJF, etc.
    material_name: str  # PLA, PETG, Nylon, Resin, etc.
    reason: str  # Why this material fits the use case
    price_tier: str  # budget, mid, premium
    strength: str  # low, medium, high
    detail_level: str  # low, medium, high
    weather_resistant: bool
    food_safe: bool
    typical_lead_days: int  # Typical production lead time
    recommended_provider: str  # craftcloud, sculpteo, 3dos

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MaterialGuide:
    """Full material recommendation result for a consumer query."""

    use_case: str
    recommendations: list[MaterialRecommendation]
    best_pick: MaterialRecommendation
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["recommendations"] = [r.to_dict() for r in self.recommendations]
        data["best_pick"] = self.best_pick.to_dict()
        return data


# Knowledge base: use case -> ranked material recommendations.
_MATERIAL_KNOWLEDGE: dict[str, list[MaterialRecommendation]] = {
    UseCase.DECORATIVE.value: [
        MaterialRecommendation(
            technology="FDM",
            material_name="PLA",
            reason="Excellent surface finish, wide color range, lowest cost. Perfect for display items.",
            price_tier="budget",
            strength="low",
            detail_level="medium",
            weather_resistant=False,
            food_safe=False,
            typical_lead_days=5,
            recommended_provider="craftcloud",
        ),
        MaterialRecommendation(
            technology="SLA",
            material_name="Standard Resin",
            reason="Ultra-fine detail, smooth surface. Ideal for figurines, art pieces, and detailed decorations.",
            price_tier="mid",
            strength="low",
            detail_level="high",
            weather_resistant=False,
            food_safe=False,
            typical_lead_days=7,
            recommended_provider="sculpteo",
        ),
        MaterialRecommendation(
            technology="MJF",
            material_name="Nylon PA12",
            reason="Professional finish, strong and durable. Good for gifts and premium decorative items.",
            price_tier="premium",
            strength="high",
            detail_level="high",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=10,
            recommended_provider="sculpteo",
        ),
    ],
    UseCase.FUNCTIONAL.value: [
        MaterialRecommendation(
            technology="FDM",
            material_name="PETG",
            reason="Strong, impact-resistant, and slightly flexible. Great for everyday functional parts.",
            price_tier="budget",
            strength="medium",
            detail_level="medium",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=5,
            recommended_provider="craftcloud",
        ),
        MaterialRecommendation(
            technology="SLS",
            material_name="Nylon PA12",
            reason="Industrial-grade strength without support marks. Perfect for functional assemblies.",
            price_tier="mid",
            strength="high",
            detail_level="medium",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=8,
            recommended_provider="sculpteo",
        ),
        MaterialRecommendation(
            technology="FDM",
            material_name="ABS",
            reason="Heat-resistant and machinable. Good for functional parts that need post-processing.",
            price_tier="budget",
            strength="medium",
            detail_level="low",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=5,
            recommended_provider="craftcloud",
        ),
    ],
    UseCase.MECHANICAL.value: [
        MaterialRecommendation(
            technology="SLS",
            material_name="Nylon PA12",
            reason="Best strength-to-weight ratio. Ideal for gears, brackets, and mechanical assemblies.",
            price_tier="mid",
            strength="high",
            detail_level="medium",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=8,
            recommended_provider="sculpteo",
        ),
        MaterialRecommendation(
            technology="MJF",
            material_name="Nylon PA12",
            reason="Consistent mechanical properties and fine detail. Superior for snap-fits and hinges.",
            price_tier="premium",
            strength="high",
            detail_level="high",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=10,
            recommended_provider="sculpteo",
        ),
        MaterialRecommendation(
            technology="FDM",
            material_name="PETG",
            reason="Budget-friendly for mechanical prototyping. Good layer adhesion for load-bearing parts.",
            price_tier="budget",
            strength="medium",
            detail_level="medium",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=5,
            recommended_provider="craftcloud",
        ),
    ],
    UseCase.PROTOTYPE.value: [
        MaterialRecommendation(
            technology="FDM",
            material_name="PLA",
            reason="Cheapest and fastest option. Ideal for visual prototypes and fit-checks.",
            price_tier="budget",
            strength="low",
            detail_level="medium",
            weather_resistant=False,
            food_safe=False,
            typical_lead_days=3,
            recommended_provider="craftcloud",
        ),
        MaterialRecommendation(
            technology="SLA",
            material_name="Standard Resin",
            reason="High-detail prototype that looks like a finished product. Great for client presentations.",
            price_tier="mid",
            strength="low",
            detail_level="high",
            weather_resistant=False,
            food_safe=False,
            typical_lead_days=5,
            recommended_provider="sculpteo",
        ),
    ],
    UseCase.MINIATURE.value: [
        MaterialRecommendation(
            technology="SLA",
            material_name="High-Detail Resin",
            reason="Best possible detail resolution. Industry standard for tabletop miniatures.",
            price_tier="mid",
            strength="low",
            detail_level="high",
            weather_resistant=False,
            food_safe=False,
            typical_lead_days=7,
            recommended_provider="sculpteo",
        ),
        MaterialRecommendation(
            technology="MJF",
            material_name="Nylon PA12",
            reason="Durable miniatures that survive handling. Good detail at scale.",
            price_tier="premium",
            strength="high",
            detail_level="high",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=10,
            recommended_provider="sculpteo",
        ),
    ],
    UseCase.JEWELRY.value: [
        MaterialRecommendation(
            technology="SLA",
            material_name="Castable Resin",
            reason="Designed for lost-wax casting into metal. Industry standard for custom jewelry.",
            price_tier="premium",
            strength="low",
            detail_level="high",
            weather_resistant=False,
            food_safe=False,
            typical_lead_days=10,
            recommended_provider="sculpteo",
        ),
        MaterialRecommendation(
            technology="SLS",
            material_name="Nylon PA12",
            reason="Lightweight fashion jewelry that's durable. Can be dyed in multiple colors.",
            price_tier="mid",
            strength="high",
            detail_level="medium",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=8,
            recommended_provider="sculpteo",
        ),
    ],
    UseCase.ENCLOSURE.value: [
        MaterialRecommendation(
            technology="FDM",
            material_name="PETG",
            reason="Impact-resistant, easy to post-process. Standard for electronics enclosures.",
            price_tier="budget",
            strength="medium",
            detail_level="medium",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=5,
            recommended_provider="craftcloud",
        ),
        MaterialRecommendation(
            technology="SLS",
            material_name="Nylon PA12",
            reason="Professional enclosures with snap-fits and living hinges. No visible layer lines.",
            price_tier="mid",
            strength="high",
            detail_level="medium",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=8,
            recommended_provider="sculpteo",
        ),
    ],
    UseCase.WEARABLE.value: [
        MaterialRecommendation(
            technology="SLS",
            material_name="TPU",
            reason="Flexible, comfortable against skin. Ideal for wristbands, custom orthotics.",
            price_tier="mid",
            strength="medium",
            detail_level="medium",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=8,
            recommended_provider="sculpteo",
        ),
        MaterialRecommendation(
            technology="FDM",
            material_name="TPU",
            reason="Budget flexible option. Good for simple wearable prototypes.",
            price_tier="budget",
            strength="medium",
            detail_level="low",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=5,
            recommended_provider="craftcloud",
        ),
    ],
    UseCase.OUTDOOR.value: [
        MaterialRecommendation(
            technology="FDM",
            material_name="ASA",
            reason="UV-resistant, weather-proof. The go-to for outdoor functional parts.",
            price_tier="budget",
            strength="medium",
            detail_level="low",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=5,
            recommended_provider="craftcloud",
        ),
        MaterialRecommendation(
            technology="SLS",
            material_name="Nylon PA12",
            reason="Industrial-grade weather resistance. Won't degrade in sun or rain.",
            price_tier="mid",
            strength="high",
            detail_level="medium",
            weather_resistant=True,
            food_safe=False,
            typical_lead_days=8,
            recommended_provider="sculpteo",
        ),
    ],
    UseCase.FOOD_SAFE.value: [
        MaterialRecommendation(
            technology="SLS",
            material_name="Nylon PA12 (food-safe)",
            reason="FDA-compliant when post-processed. Used for cookie cutters and kitchen tools.",
            price_tier="mid",
            strength="high",
            detail_level="medium",
            weather_resistant=True,
            food_safe=True,
            typical_lead_days=10,
            recommended_provider="sculpteo",
        ),
        MaterialRecommendation(
            technology="FDM",
            material_name="PETG (food-safe)",
            reason="Budget food-safe option. Must be sealed/coated for full food safety.",
            price_tier="budget",
            strength="medium",
            detail_level="low",
            weather_resistant=True,
            food_safe=True,
            typical_lead_days=5,
            recommended_provider="craftcloud",
        ),
    ],
}


def recommend_material(
    use_case: str,
    *,
    budget: str | None = None,
    need_weather_resistant: bool = False,
    need_food_safe: bool = False,
    need_high_detail: bool = False,
    need_high_strength: bool = False,
) -> MaterialGuide:
    """Recommend materials based on use case and constraints.

    Args:
        use_case: One of the UseCase enum values (e.g. 'functional', 'decorative').
        budget: Price tier preference: 'budget', 'mid', 'premium', or None for any.
        need_weather_resistant: Filter to weather-resistant materials only.
        need_food_safe: Filter to food-safe materials only.
        need_high_detail: Prefer high-detail materials.
        need_high_strength: Prefer high-strength materials.

    Returns:
        MaterialGuide with ranked recommendations and a best-pick.

    Raises:
        ValueError: If use_case is not recognized.
    """
    use_case_lower = use_case.lower().strip()

    # Try to match use case, with fallback to functional
    candidates = _MATERIAL_KNOWLEDGE.get(use_case_lower)
    if candidates is None:
        valid = ", ".join(sorted(_MATERIAL_KNOWLEDGE))
        raise ValueError(f"Unknown use case '{use_case}'. Valid options: {valid}")

    # Apply filters
    filtered = list(candidates)  # Start with all

    if need_weather_resistant:
        weather_only = [r for r in filtered if r.weather_resistant]
        if weather_only:
            filtered = weather_only

    if need_food_safe:
        food_only = [r for r in filtered if r.food_safe]
        if food_only:
            filtered = food_only

    if budget:
        budget_match = [r for r in filtered if r.price_tier == budget.lower()]
        if budget_match:
            filtered = budget_match

    if need_high_detail:
        detail_match = [r for r in filtered if r.detail_level == "high"]
        if detail_match:
            filtered = detail_match

    if need_high_strength:
        strength_match = [r for r in filtered if r.strength == "high"]
        if strength_match:
            filtered = strength_match

    # If all filters eliminated everything, fall back to original candidates
    if not filtered:
        filtered = list(candidates)

    best = filtered[0]

    # Build explanation
    constraints = []
    if budget:
        constraints.append(f"budget: {budget}")
    if need_weather_resistant:
        constraints.append("weather-resistant")
    if need_food_safe:
        constraints.append("food-safe")
    if need_high_detail:
        constraints.append("high detail")
    if need_high_strength:
        constraints.append("high strength")

    constraint_str = f" (filters: {', '.join(constraints)})" if constraints else ""
    explanation = (
        f"For {use_case_lower} use{constraint_str}, we recommend "
        f"{best.material_name} via {best.technology}. {best.reason}"
    )

    return MaterialGuide(
        use_case=use_case_lower,
        recommendations=filtered,
        best_pick=best,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Timeline estimation
# ---------------------------------------------------------------------------


@dataclass
class TimelineStage:
    """A stage in the order-to-delivery timeline."""

    stage: str
    description: str
    estimated_days: int
    status: str = "pending"  # pending, in_progress, completed

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrderTimeline:
    """Full order-to-delivery timeline with stage breakdowns."""

    stages: list[TimelineStage]
    total_days: int
    estimated_delivery_date: str  # ISO date string
    confidence: str  # low, medium, high

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stages"] = [s.to_dict() for s in self.stages]
        return data


# Baseline days per stage, keyed by technology.
_STAGE_BASELINES: dict[str, dict[str, int]] = {
    "FDM": {"quote_review": 0, "production": 3, "quality_check": 1, "packaging": 1},
    "SLA": {"quote_review": 0, "production": 4, "quality_check": 1, "packaging": 1},
    "SLS": {"quote_review": 1, "production": 5, "quality_check": 1, "packaging": 1},
    "MJF": {"quote_review": 1, "production": 5, "quality_check": 1, "packaging": 1},
    "DMLS": {"quote_review": 1, "production": 8, "quality_check": 2, "packaging": 1},
}

_STAGE_DESCRIPTIONS = {
    "quote_review": "Order confirmed and sent to manufacturer",
    "production": "Part is being manufactured",
    "quality_check": "Quality inspection and post-processing",
    "packaging": "Packaging for shipment",
    "shipping": "In transit to delivery address",
}


def estimate_timeline(
    technology: str,
    *,
    shipping_days: int | None = None,
    quantity: int = 1,
    country: str = "US",
) -> OrderTimeline:
    """Estimate the full order-to-delivery timeline.

    Args:
        technology: Manufacturing technology (FDM, SLA, SLS, MJF, DMLS).
        shipping_days: Estimated shipping days (from quote, or default by region).
        quantity: Number of parts (larger quantities add production time).
        country: Destination country for shipping estimate fallback.

    Returns:
        OrderTimeline with per-stage breakdown and total days.
    """
    tech = technology.upper()
    baselines = _STAGE_BASELINES.get(tech, _STAGE_BASELINES["FDM"])

    # Scale production time for quantity
    production_days = baselines["production"]
    if quantity > 5:
        production_days += (quantity - 5) // 5 + 1
    elif quantity > 1:
        production_days += 1

    # Estimate shipping if not provided
    if shipping_days is None:
        shipping_days = _estimate_shipping_days(country)

    stages = [
        TimelineStage(
            stage="quote_review",
            description=_STAGE_DESCRIPTIONS["quote_review"],
            estimated_days=baselines["quote_review"],
        ),
        TimelineStage(
            stage="production",
            description=_STAGE_DESCRIPTIONS["production"],
            estimated_days=production_days,
        ),
        TimelineStage(
            stage="quality_check",
            description=_STAGE_DESCRIPTIONS["quality_check"],
            estimated_days=baselines["quality_check"],
        ),
        TimelineStage(
            stage="packaging",
            description=_STAGE_DESCRIPTIONS["packaging"],
            estimated_days=baselines["packaging"],
        ),
        TimelineStage(
            stage="shipping",
            description=_STAGE_DESCRIPTIONS["shipping"],
            estimated_days=shipping_days,
        ),
    ]

    total = sum(s.estimated_days for s in stages)

    # Calculate estimated delivery date
    import datetime

    delivery_date = datetime.date.today() + datetime.timedelta(days=total)

    # Confidence based on technology maturity and shipping reliability
    confidence = "high" if tech in ("FDM", "SLA") else "medium"
    if shipping_days > 14:
        confidence = "low"

    return OrderTimeline(
        stages=stages,
        total_days=total,
        estimated_delivery_date=delivery_date.isoformat(),
        confidence=confidence,
    )


def _estimate_shipping_days(country: str) -> int:
    """Estimate shipping days based on destination country."""
    domestic = {"US", "CA"}
    europe = {"GB", "DE", "FR", "NL", "BE", "AT", "CH", "IE", "IT", "ES", "PT", "SE", "NO", "DK", "FI", "PL", "CZ"}
    asia_pacific = {"JP", "KR", "SG", "AU", "NZ"}

    country = country.upper()
    if country in domestic:
        return 5
    elif country in europe:
        return 7
    elif country in asia_pacific:
        return 12
    else:
        return 14


# ---------------------------------------------------------------------------
# Instant price estimation (before full API quote)
# ---------------------------------------------------------------------------


@dataclass
class PriceEstimate:
    """Quick price estimate before requesting a full API quote."""

    estimated_price_low: float
    estimated_price_high: float
    currency: str
    technology: str
    material: str
    volume_cm3: float | None
    confidence: str  # low, medium, high
    note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Rough pricing per cm3 by technology (USD).
_PRICE_PER_CM3: dict[str, dict[str, float]] = {
    "FDM": {"low": 0.10, "high": 0.30},
    "SLA": {"low": 0.30, "high": 0.80},
    "SLS": {"low": 0.50, "high": 1.20},
    "MJF": {"low": 0.60, "high": 1.50},
    "DMLS": {"low": 3.00, "high": 8.00},
}

# Base fee added to every order (setup/handling).
_BASE_FEE: dict[str, float] = {
    "FDM": 5.0,
    "SLA": 10.0,
    "SLS": 15.0,
    "MJF": 20.0,
    "DMLS": 50.0,
}


def estimate_price(
    technology: str,
    *,
    volume_cm3: float | None = None,
    dimensions_mm: dict[str, float] | None = None,
    quantity: int = 1,
) -> PriceEstimate:
    """Quick price estimate without calling the fulfillment API.

    Either volume_cm3 or dimensions_mm (with keys x, y, z) must be provided.
    If dimensions_mm is given, volume is estimated from bounding box with a
    40% fill factor (typical for 3D printed parts).

    Args:
        technology: Manufacturing technology (FDM, SLA, SLS, etc.).
        volume_cm3: Estimated part volume in cubic centimeters.
        dimensions_mm: Bounding box with keys x, y, z in millimeters.
        quantity: Number of copies.

    Returns:
        PriceEstimate with low/high range.

    Raises:
        ValueError: If neither volume_cm3 nor dimensions_mm is provided.
    """
    tech = technology.upper()
    pricing = _PRICE_PER_CM3.get(tech)
    if pricing is None:
        valid = ", ".join(sorted(_PRICE_PER_CM3))
        raise ValueError(f"Unknown technology '{technology}'. Valid: {valid}")

    if volume_cm3 is None and dimensions_mm is not None:
        x = dimensions_mm.get("x", 0)
        y = dimensions_mm.get("y", 0)
        z = dimensions_mm.get("z", 0)
        if x <= 0 or y <= 0 or z <= 0:
            raise ValueError("All dimensions (x, y, z) must be positive.")
        # Bounding box volume with 40% fill factor
        bbox_cm3 = (x * y * z) / 1000.0  # mm3 -> cm3
        volume_cm3 = bbox_cm3 * 0.4
    elif volume_cm3 is None:
        raise ValueError("Provide either volume_cm3 or dimensions_mm (with keys x, y, z).")

    base = _BASE_FEE.get(tech, 10.0)
    low = (volume_cm3 * pricing["low"] + base) * quantity
    high = (volume_cm3 * pricing["high"] + base) * quantity

    confidence = "medium"
    if volume_cm3 > 500:
        confidence = "low"  # Large parts have more variable pricing
    elif volume_cm3 < 50:
        confidence = "high"  # Small parts are more predictable

    return PriceEstimate(
        estimated_price_low=round(low, 2),
        estimated_price_high=round(high, 2),
        currency="USD",
        technology=tech,
        material=f"Typical {tech} material",
        volume_cm3=round(volume_cm3, 2) if volume_cm3 else None,
        confidence=confidence,
        note=(
            f"Rough estimate for {tech}. Request a full quote with fulfillment_quote for exact pricing with shipping."
        ),
    )


# ---------------------------------------------------------------------------
# Consumer onboarding
# ---------------------------------------------------------------------------


@dataclass
class OnboardingStep:
    """A step in the consumer onboarding workflow."""

    step: int
    title: str
    description: str
    tool: str  # MCP tool name to invoke
    example: str  # Example invocation

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConsumerOnboarding:
    """Guided onboarding for users without a 3D printer."""

    steps: list[OnboardingStep]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["steps"] = [s.to_dict() for s in self.steps]
        return data


def get_onboarding() -> ConsumerOnboarding:
    """Return the guided onboarding workflow for no-printer users.

    Returns a step-by-step guide covering: find/generate a model,
    get material recommendations, estimate price, get a real quote,
    validate the address, place the order, and track delivery.
    """
    steps = [
        OnboardingStep(
            step=1,
            title="Describe what you need",
            description=(
                "Tell us what you want to make. We'll either find an existing "
                "model or generate one from your description."
            ),
            tool="search_all_models or generate_model",
            example='search_all_models(query="phone stand") or generate_model(prompt="phone stand with cable slot")',
        ),
        OnboardingStep(
            step=2,
            title="Get material recommendations",
            description=(
                "Based on your item's purpose, we recommend the best material "
                "and manufacturing technology for your needs and budget."
            ),
            tool="recommend_material",
            example='recommend_material(use_case="functional", budget="budget")',
        ),
        OnboardingStep(
            step=3,
            title="Get a quick price estimate",
            description=(
                "Before committing, see an instant price range for your part "
                "based on size and technology — no API call needed."
            ),
            tool="estimate_price",
            example='estimate_price(technology="FDM", dimensions_mm={"x": 80, "y": 60, "z": 40})',
        ),
        OnboardingStep(
            step=4,
            title="Get a real manufacturing quote",
            description=(
                "Upload your model file to get exact pricing, lead time, and "
                "shipping options from professional manufacturers."
            ),
            tool="fulfillment_quote",
            example='fulfillment_quote(file_path="/path/to/model.stl", material_id="pla-white")',
        ),
        OnboardingStep(
            step=5,
            title="Validate your shipping address",
            description=(
                "We check your address format and postal code before placing the order to prevent delivery issues."
            ),
            tool="validate_shipping_address",
            example='validate_shipping_address(street="123 Main St", city="Austin", state="TX", postal_code="78701", country="US")',
        ),
        OnboardingStep(
            step=6,
            title="Place your order",
            description=(
                "Confirm the quote, select shipping speed, and place the "
                "manufacturing order. Your part goes into production."
            ),
            tool="fulfillment_order",
            example='fulfillment_order(quote_id="q-123", shipping_option_id="standard")',
        ),
        OnboardingStep(
            step=7,
            title="Track your delivery",
            description=(
                "Monitor your order through production, quality check, and shipping — from factory to your door."
            ),
            tool="fulfillment_order_status",
            example='fulfillment_order_status(order_id="ord-123")',
        ),
    ]

    return ConsumerOnboarding(
        steps=steps,
        summary=(
            "Kiln handles everything: find or create a 3D model, pick the "
            "right material, get a quote from professional manufacturers, "
            "and track your order to delivery. No 3D printer needed."
        ),
    )


# ---------------------------------------------------------------------------
# Supported countries helper
# ---------------------------------------------------------------------------


def list_supported_countries() -> dict[str, str]:
    """Return the dict of supported shipping countries (code -> name)."""
    return dict(_SUPPORTED_COUNTRIES)
